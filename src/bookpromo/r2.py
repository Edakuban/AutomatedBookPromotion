"""Minimal Cloudflare R2 client using AWS Signature Version 4."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
from pathlib import PurePosixPath
import re
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx

from .config import Settings


class R2Error(RuntimeError):
    pass


def _valid_key(key: str) -> str:
    if (not key or len(key) > 1024 or key.startswith("/") or "\\" in key
            or any(part in {"", ".", ".."} for part in PurePosixPath(key).parts)):
        raise ValueError("Ungültiger R2-Objektpfad.")
    return key


class R2Client:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        if settings.missing_for("r2"):
            raise R2Error("R2-Zugangsdaten, Endpoint oder Konto-ID fehlen.")
        self.account_id = settings.r2_account_id
        self.access_key = settings.r2_access_key_id.get_secret_value()
        self.secret_key = settings.r2_secret_access_key.get_secret_value()
        self.bucket = settings.r2_bucket
        self.endpoint = str(settings.r2_endpoint).rstrip("/")
        self.public_base_url = str(settings.r2_public_base_url).rstrip("/") if settings.r2_public_base_url else None
        self.ttl = settings.r2_signed_url_ttl_seconds
        self._transport = transport
        parsed = urlsplit(self.endpoint)
        if parsed.path not in {"", "/"}:
            raise R2Error("Der R2-Endpoint darf keinen Pfad enthalten.")
        if self.account_id not in parsed.hostname:
            raise R2Error("Der R2-Endpoint gehört nicht zur konfigurierten Konto-ID.")

    def _uri(self, key: str) -> str:
        key = _valid_key(key)
        return "/" + quote(self.bucket, safe="") + "/" + "/".join(quote(part, safe="") for part in key.split("/"))

    def public_url(self, key: str) -> str | None:
        if not self.public_base_url:
            return None
        return self.public_base_url + "/" + "/".join(quote(part, safe="") for part in _valid_key(key).split("/"))

    def _headers(self, method: str, key: str, body: bytes, now: datetime, content_type: str | None) -> dict[str, str]:
        parsed = urlsplit(self.endpoint)
        payload_hash = hashlib.sha256(body).hexdigest()
        amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
        values = {"host": parsed.netloc, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
        if content_type:
            values["content-type"] = content_type
        names = ";".join(sorted(values))
        canonical_headers = "".join(f"{name}:{values[name].strip()}\n" for name in sorted(values))
        canonical = "\n".join((method, self._uri(key), "", canonical_headers, names, payload_hash))
        scope = f"{day}/auto/s3/aws4_request"
        string_to_sign = "AWS4-HMAC-SHA256\n" + amz_date + "\n" + scope + "\n" + hashlib.sha256(canonical.encode()).hexdigest()
        key_date = hmac.new(("AWS4" + self.secret_key).encode(), day.encode(), hashlib.sha256).digest()
        key_region = hmac.new(key_date, b"auto", hashlib.sha256).digest()
        key_service = hmac.new(key_region, b"s3", hashlib.sha256).digest()
        signing_key = hmac.new(key_service, b"aws4_request", hashlib.sha256).digest()
        signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
        result = {name: value for name, value in values.items() if name != "host"}
        result["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, SignedHeaders={names}, Signature={signature}"
        )
        return result

    async def _request(self, method: str, key: str, *, body: bytes = b"", content_type: str | None = None) -> httpx.Response:
        now = datetime.now(timezone.utc)
        url = self.endpoint + self._uri(key)
        headers = self._headers(method, key, body, now, content_type)
        try:
            async with httpx.AsyncClient(timeout=120, follow_redirects=False, trust_env=False,
                                         transport=self._transport) as client:
                response = await client.request(method, url, headers=headers, content=body)
        except httpx.HTTPError:
            raise R2Error("Cloudflare R2 ist derzeit nicht erreichbar.") from None
        if response.status_code in {401, 403}:
            raise R2Error("Cloudflare R2 hat die Zugangsdaten abgelehnt.")
        if not 200 <= response.status_code < 300:
            raise R2Error("Cloudflare R2 hat die Speicheroperation abgelehnt.")
        return response

    async def upload_reel(self, key: str, data: bytes, digest: str, *,
                          max_bytes: int = 50 * 1024 * 1024) -> str:
        if not 12 <= max_bytes <= 300 * 1024 * 1024:
            raise ValueError("Ungültiges Video-Upload-Limit.")
        if (not re.fullmatch(r"[0-9a-f]{64}", digest) or hashlib.sha256(data).hexdigest() != digest
                or len(data) < 12 or len(data) > max_bytes or data[4:8] != b"ftyp"):
            raise ValueError("Ungültiges Reel-Video.")
        await self._request("PUT", key, body=data, content_type="video/mp4")
        return key

    async def upload_carousel_image(self, key: str, data: bytes, digest: str) -> str:
        if (not re.fullmatch(r"[0-9a-f]{64}", digest)
                or hashlib.sha256(data).hexdigest() != digest
                or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9")
                or len(data) > 8 * 1024 * 1024
                or not re.fullmatch(
                    r"carousel-sources/(?:quotes|chapters)/[0-9a-f-]{36}/[0-9a-f-]{36}/"
                    + digest + r"\.jpg", key,
                )):
            raise ValueError("Ungültiges Carousel-Quellbild.")
        await self._request("PUT", key, body=data, content_type="image/jpeg")
        return key

    async def delete(self, key: str) -> None:
        await self._request("DELETE", key)

    async def check(self) -> None:
        key = f"bookpromo-probe/{uuid4()}.txt"
        await self._request("PUT", key, body=b"bookpromo-r2-check", content_type="text/plain")
        try:
            await self._request("HEAD", key)
        finally:
            await self.delete(key)
