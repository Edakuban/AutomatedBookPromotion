"""Supabase Data and private Storage adapter; schema installation is never automatic."""

import hashlib
import re
from typing import Literal
from uuid import UUID

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from .config import Settings
from .overlay import OVERLAY_BUCKET

SCHEMA_VERSION = 11
MIN_SCHEMA_VERSION = 8
REEL_BUCKET = "book-promotion-reels"
MESSAGES = {
    "disabled": "Supabase ist bis Schritt 3.2 deaktiviert.",
    "configuration": "Supabase-URL und Server-Schlüssel fehlen oder sind ungeeignet.",
    "credentials": "Supabase hat den Zugriff abgelehnt. Server-Schlüssel und Berechtigungen prüfen.",
    "schema": "Das Supabase-Schema ist nicht kompatibel (unterstützt: v8–v11). Bitte die vorbereiteten Migrationen prüfen.",
    "chapter_schema": "Die Veröffentlichungsqueue für Kapitel-Reels benötigt die noch ausstehende Supabase-Migration auf v9. Lokale Bilder und Videos sind davon unabhängig.",
    "book_teaser_schema": "Die Veröffentlichungsqueue für das Gesamt-Teaservideo benötigt die noch ausstehende Supabase-Migration auf v10 (nach v9). Lokale Bilder und Videos sind davon unabhängig.",
    "carousel_schema": "Vorbereitete Carousel-Bilder benötigen die noch ausstehende Supabase-Migration auf v11. Die bisherige Live-Bilderzeugung bleibt davon unabhängig.",
    "unavailable": "Supabase ist derzeit nicht erreichbar. Verbindung und Server prüfen.",
    "response": "Supabase hat eine unerwartete Antwort geliefert.",
    "conflict": "Supabase enthält einen neueren Stand oder einen offenen Post. Bitte den Datenbankstand prüfen und den offenen Post abschließen.",
}


class DatabaseError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(MESSAGES[code])


class BookSummary(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    id: UUID
    title: str
    author: str
    active: bool
    displayed_version_id: UUID | None
    status: Literal["new", "queued", "extracting", "analyzing", "needs_review", "ready", "failed"]
    chapter_count: int = Field(ge=0)
    quote_count: int = Field(ge=0)
    last_published_at: AwareDatetime | None


class QuoteUsage(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    id: UUID
    last_published_at: AwareDatetime | None
    reserved: bool


class PromotionSettings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    id: UUID
    active: bool
    mode: Literal["fixed_book", "random_book"]
    fixed_book_id: UUID | None
    updated_at: AwareDatetime


class PromotionBook(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    id: UUID
    title: str
    author: str


class CarouselSourceMedia(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    scope: Literal["quote", "chapter"]
    owner_id: UUID
    provider: Literal["supabase", "cloudflare_r2"]
    bucket: str
    path: str
    public_url: str | None = None
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0, le=8 * 1024 * 1024)
    width: Literal[1080]
    height: Literal[1350]
    mime_type: Literal["image/jpeg"]


class SupabaseRepository:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        if not settings.supabase_enabled:
            raise DatabaseError("disabled")
        if settings.missing_for("supabase"):
            raise DatabaseError("configuration")
        key = settings.supabase_secret_key.get_secret_value()
        # Opaque server keys and legacy service-role JWTs are supported.
        # A publishable/anon key cannot access the private application tables.
        if not (key.startswith("sb_secret_") or key.startswith("eyJ")):
            raise DatabaseError("configuration")
        self._url = str(settings.supabase_url).rstrip("/") + "/rest/v1/"
        self._storage_url = str(settings.supabase_url).rstrip("/") + "/storage/v1/"
        self._headers = {"apikey": key, "Accept": "application/json"}
        if key.startswith("eyJ"):
            self._headers["Authorization"] = f"Bearer {key}"
        self._transport = transport

    async def _rpc_json(self, name: str, payload: dict, *, timeout: float = 60) -> dict:
        try:
            async with httpx.AsyncClient(base_url=self._url, headers=self._headers, timeout=timeout,
                follow_redirects=False, trust_env=False, transport=self._transport) as client:
                async with client.stream("POST", "rpc/"+name, json=payload) as response:
                    if response.status_code in (401,403): raise DatabaseError("credentials")
                    if response.status_code == 409: raise DatabaseError("conflict")
                    if response.status_code in (400,404): raise DatabaseError("schema")
                    if not 200 <= response.status_code < 300: raise DatabaseError("unavailable")
                    body=bytearray()
                    async for part in response.aiter_bytes():
                        body.extend(part)
                        if len(body)>1024*1024: raise DatabaseError("response")
            import json
            result=json.loads(body)
            if not isinstance(result,dict): raise DatabaseError("response")
            return result
        except httpx.HTTPError:
            raise DatabaseError("unavailable") from None
        except ValueError:
            raise DatabaseError("response") from None

    async def rpc(self, name: str, payload: dict) -> dict:
        """Explicit, atomic book snapshot writes."""
        if name != "bookpromo_sync": raise ValueError("Unbekannte Schreiboperation.")
        return await self._rpc_json(name, payload)

    async def _upload_book_asset(
        self, object_path: str, data: bytes, *, content_type: str, limit: int, path_pattern: str,
    ) -> str:
        """Upload immutable digest-addressed data with idempotent retry semantics."""
        match = re.fullmatch(path_pattern, object_path)
        if (match is None or not data or len(data) > limit
                or hashlib.sha256(data).hexdigest() != match.group("digest")):
            raise ValueError("Ungültiges Buchasset")
        headers = {**self._headers, "Content-Type": content_type, "x-upsert": "true"}
        try:
            async with httpx.AsyncClient(base_url=self._storage_url, headers=headers, timeout=30,
                follow_redirects=False, trust_env=False, transport=self._transport) as client:
                response = await client.post("object/" + OVERLAY_BUCKET + "/" + object_path, content=data)
        except httpx.HTTPError:
            raise DatabaseError("unavailable") from None
        if response.status_code in (401, 403):
            raise DatabaseError("credentials")
        if response.status_code == 404:
            raise DatabaseError("schema")
        if not 200 <= response.status_code < 300:
            raise DatabaseError("unavailable")
        return object_path

    async def upload_overlay(self, object_path: str, data: bytes) -> str:
        """Upload a private transparent 4:5 title overlay."""
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Ungültiges Overlay")
        return await self._upload_book_asset(
            object_path,data,content_type="image/png",limit=1024*1024,
            path_pattern=r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/(?P<digest>[0-9a-f]{64})\.png",
        )

    async def upload_carousel_end_slide(self, object_path: str, data: bytes) -> str:
        """Upload the deterministic private 4:5 CTA JPEG."""
        if not (data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")):
            raise ValueError("Ungültige Carousel-Schlussseite")
        return await self._upload_book_asset(
            object_path,data,content_type="image/jpeg",limit=8*1024*1024,
            path_pattern=r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/carousel/(?P<digest>[0-9a-f]{64})\.jpg",
        )

    async def _read(self, relation: str, params: dict) -> list[dict]:
        try:
            async with httpx.AsyncClient(
                base_url=self._url, headers=self._headers, timeout=httpx.Timeout(10.0, connect=5.0),
                follow_redirects=False, trust_env=False, transport=self._transport,
            ) as client:
                response = await client.get(relation, params=params)
        except httpx.HTTPError:
            raise DatabaseError("unavailable") from None
        # Never include a remote error message, request URL or response body in errors.
        if response.status_code in (401, 403):
            raise DatabaseError("credentials")
        if response.status_code in (400, 404):
            raise DatabaseError("schema")
        if not 200 <= response.status_code < 300:
            raise DatabaseError("unavailable")
        try:
            value = response.json()
        except ValueError:
            raise DatabaseError("response") from None
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            raise DatabaseError("response")
        return value

    async def _patch(self, relation: str, params: dict, payload: dict) -> list[dict]:
        headers = {**self._headers, "Prefer": "return=representation", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(
                base_url=self._url, headers=headers, timeout=httpx.Timeout(10.0, connect=5.0),
                follow_redirects=False, trust_env=False, transport=self._transport,
            ) as client:
                response = await client.patch(relation, params=params, json=payload)
        except httpx.HTTPError:
            raise DatabaseError("unavailable") from None
        if response.status_code in (401, 403):
            raise DatabaseError("credentials")
        if response.status_code == 409:
            raise DatabaseError("conflict")
        if response.status_code in (400, 404):
            raise DatabaseError("schema")
        if not 200 <= response.status_code < 300:
            raise DatabaseError("unavailable")
        try:
            value = response.json()
        except ValueError:
            raise DatabaseError("response") from None
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            raise DatabaseError("response")
        return value

    async def _insert(self, relation: str, payload: dict) -> list[dict]:
        headers = {**self._headers, "Prefer": "return=representation", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(
                base_url=self._url, headers=headers, timeout=httpx.Timeout(30.0, connect=5.0),
                follow_redirects=False, trust_env=False, transport=self._transport,
            ) as client:
                response = await client.post(relation, json=payload)
        except httpx.HTTPError:
            raise DatabaseError("unavailable") from None
        if response.status_code in (401, 403):
            raise DatabaseError("credentials")
        if response.status_code == 409:
            raise DatabaseError("conflict")
        if response.status_code in (400, 404):
            raise DatabaseError("schema")
        if not 200 <= response.status_code < 300:
            raise DatabaseError("unavailable")
        try:
            value = response.json()
        except ValueError:
            raise DatabaseError("response") from None
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            raise DatabaseError("response")
        return value

    async def check_schema(self, *, full: bool = False, chapters: bool = False,
                           book_teasers: bool = False, carousel_images: bool = False) -> int:
        rows = await self._read("bookpromo_schema", {"select": "version", "limit": "2"})
        if (len(rows) != 1 or type(rows[0].get("version")) is not int
                or not MIN_SCHEMA_VERSION <= rows[0]["version"] <= SCHEMA_VERSION):
            raise DatabaseError("schema")
        version = rows[0]["version"]
        if chapters and version < 9:
            raise DatabaseError("chapter_schema")
        if book_teasers and version < 10:
            raise DatabaseError("book_teaser_schema")
        if carousel_images and version < 11:
            raise DatabaseError("carousel_schema")
        if full:
            # Empty result sets also validate the exposed objects, columns and read grants.
            relations = {
                "books": "id,current_version_id", "book_versions": "id,book_id,file_sha256",
                "chapters": "id,book_version_id,source_text", "quotes": "id,chapter_id,source_start,source_end",
                "import_jobs": "id,status,lease_expires_at",
                "posts": "id,quote_id,published_at,revision,execution_mode,instagram_container_id",
                "post_media": "post_id,manifest_revision,position,kind,storage_path,sha256,status,instagram_container_id",
                "promotion_settings": "id,mode,fixed_book_id",
                "book_overview": "id,title,chapter_count,quote_count,last_published_at",
                "chapter_overview": "id,quote_count,usable_quote_count",
                "quote_overview": "id,last_published_at,reserved",
                "reel_assets": "id,quote_id,book_id,storage_provider,storage_bucket,storage_path,media_status,media_sha256",
                "reel_publications": "id,reel_id,platform,account_id,queue_mode,scheduled_for,status,title,description,options",
            }
            if version >= 9:
                relations["reel_assets"] += ",source_kind,chapter_id"
            if version >= 11:
                relations["posts"] += (
                    ",source_image_scope,source_image_owner_id,source_image_provider,"
                    "source_image_bucket,source_image_path,source_image_public_url,"
                    "source_image_sha256,source_image_size_bytes,source_image_width,"
                    "source_image_height,source_image_mime_type,error_code"
                )
            for relation, columns in relations.items():
                await self._read(relation, {"select": columns, "limit": "0"})
        return version

    async def reel_account_id(self) -> str:
        rows = await self._read("promotion_settings", {
            "select": "account_id", "active": "eq.true", "order": "created_at.asc,id.asc", "limit": "2",
        })
        account = rows[0].get("account_id") if len(rows) == 1 else None
        if not isinstance(account, str) or not account.strip() or len(account) > 255:
            raise DatabaseError("configuration")
        return account.strip()

    async def upload_reel(self, object_path: str, data: bytes) -> str:
        return await self._upload_video(object_path, data, max_bytes=50 * 1024 * 1024)

    async def upload_carousel_source(self, object_path: str, data: bytes) -> str:
        """Upload an immutable, normalized 1080x1350 JPEG to private book assets."""
        if not (data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")):
            raise ValueError("Ungültiges Carousel-Quellbild")
        return await self._upload_book_asset(
            object_path, data, content_type="image/jpeg", limit=8 * 1024 * 1024,
            path_pattern=(
                r"carousel-sources/(?:quotes|chapters)/"
                r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/"
                r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/"
                r"(?P<digest>[0-9a-f]{64})\.jpg"
            ),
        )

    async def delete_carousel_source(self, object_path: str) -> None:
        if not re.fullmatch(
            r"carousel-sources/(?:quotes|chapters)/"
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/"
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/"
            r"[0-9a-f]{64}\.jpg", object_path,
        ):
            raise ValueError("Ungültiger Carousel-Objektpfad")
        try:
            async with httpx.AsyncClient(base_url=self._storage_url, headers=self._headers, timeout=30,
                follow_redirects=False, trust_env=False, transport=self._transport) as client:
                response = await client.delete("object/" + OVERLAY_BUCKET + "/" + object_path)
        except httpx.HTTPError:
            raise DatabaseError("unavailable") from None
        if response.status_code in (401, 403):
            raise DatabaseError("credentials")
        if not 200 <= response.status_code < 300:
            raise DatabaseError("unavailable")

    async def list_carousel_sources(self, book_id: UUID) -> list[CarouselSourceMedia]:
        result = await self._rpc_json("bookpromo_carousel_sources", {"p_book_id": str(book_id)})
        rows = result.get("sources")
        if not isinstance(rows, list):
            raise DatabaseError("response")
        try:
            return [CarouselSourceMedia.model_validate(row) for row in rows]
        except ValidationError:
            raise DatabaseError("response") from None

    async def set_carousel_source(
        self, scope: Literal["quote", "chapter"], owner_id: UUID, media: dict | None,
    ) -> dict:
        result = await self._rpc_json("bookpromo_carousel_source_set", {
            "p_scope": scope, "p_owner_id": str(owner_id), "p_media": media,
        })
        if result.get("outcome") not in {"set", "removed"}:
            raise DatabaseError("response")
        return result

    async def upload_book_teaser(self, object_path: str, data: bytes) -> str:
        """v10 allows larger finished trailers; quote/chapter limits remain unchanged."""
        return await self._upload_video(object_path, data, max_bytes=300 * 1024 * 1024)

    async def _upload_video(self, object_path: str, data: bytes, *, max_bytes: int) -> str:
        match = re.fullmatch(
            r"(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})/"
            r"(?P<digest>[0-9a-f]{64})\.mp4",
            object_path,
        )
        if (match is None or not 12 <= len(data) <= max_bytes
                or data[4:8] != b"ftyp" or hashlib.sha256(data).hexdigest() != match.group("digest")):
            raise ValueError("Ungültiges Reel-Video")
        headers = {**self._headers, "Content-Type": "video/mp4", "x-upsert": "true"}
        try:
            async with httpx.AsyncClient(
                base_url=self._storage_url, headers=headers, timeout=120,
                follow_redirects=False, trust_env=False, transport=self._transport,
            ) as client:
                response = await client.post("object/" + REEL_BUCKET + "/" + object_path, content=data)
        except httpx.HTTPError:
            raise DatabaseError("unavailable") from None
        if response.status_code in (401, 403):
            raise DatabaseError("credentials")
        if response.status_code == 404:
            raise DatabaseError("schema")
        if not 200 <= response.status_code < 300:
            raise DatabaseError("unavailable")
        return object_path

    async def create_reel_publication(self, payload: dict) -> dict:
        """Insert one frozen reviewed Reel, or confirm an identical idempotent retry."""
        reel_id = str(UUID(str(payload.get("id", ""))))
        try:
            rows = await self._insert("reel_publications", payload)
        except DatabaseError as exc:
            if exc.code != "conflict":
                raise
            frozen_fields = (
                "quote_id", "book_id", "account_id", "quote_text", "addition", "caption",
                "book_profile", "image_prompt", "video_prompt", "storage_path", "media_sha256",
                "size_bytes", "duration_ms", "width", "height", "audio_title", "audio_start_ms",
            )
            rows = await self._read("reel_publications", {
                "select": "id,status," + ",".join(frozen_fields),
                "id": f"eq.{reel_id}", "limit": "2",
            })
            if (len(rows) != 1 or rows[0].get("id") != reel_id
                    or any(rows[0].get(field) != payload.get(field) for field in frozen_fields)):
                raise DatabaseError("conflict") from None
            return rows[0]
        if len(rows) != 1 or rows[0].get("id") != reel_id:
            raise DatabaseError("response")
        return rows[0]

    async def enqueue_reel(self, asset: dict, publications: list[dict]) -> dict:
        """Atomically store one frozen asset and all selected destination snapshots."""
        reel_id = str(UUID(str(asset.get("id", ""))))
        if not 1 <= len(publications) <= 4:
            raise ValueError("Mindestens eine Veröffentlichungsplattform auswählen.")
        result = await self._rpc_json(
            "bookpromo_reel_enqueue", {"p_asset": asset, "p_publications": publications}, timeout=60,
        )
        saved = result.get("asset")
        rows = result.get("publications")
        if (result.get("outcome") != "enqueued" or not isinstance(saved, dict)
                or saved.get("id") != reel_id or not isinstance(rows, list)
                or len(rows) != len(publications)):
            raise DatabaseError("response")
        return result

    async def list_books(self, *, page: int = 1, page_size: int = 50) -> tuple[list[BookSummary], bool]:
        if page < 1 or not 1 <= page_size <= 100:
            raise ValueError("Ungültige Seitengröße oder Seitennummer.")
        rows = await self._read("book_overview", {
            "select": "id,title,author,active,displayed_version_id,status,chapter_count,quote_count,last_published_at",
            "order": "title.asc,id.asc", "offset": str((page - 1) * page_size),
            "limit": str(page_size + 1),
        })
        try:
            books = [BookSummary.model_validate(row) for row in rows[:page_size]]
        except ValidationError:
            raise DatabaseError("response") from None
        return books, len(rows) > page_size

    async def get_promotion_settings(self) -> PromotionSettings | None:
        rows = await self._read("promotion_settings", {
            "select": "id,active,mode,fixed_book_id,updated_at", "order": "created_at.asc,id.asc", "limit": "2",
        })
        if not rows:
            return None
        if len(rows) != 1:
            raise DatabaseError("response")
        try:
            return PromotionSettings.model_validate(rows[0])
        except ValidationError:
            raise DatabaseError("response") from None

    async def list_promotion_books(self) -> list[PromotionBook]:
        rows = await self._read("book_overview", {
            "select": "id,title,author", "active": "eq.true", "order": "title.asc,id.asc", "limit": "501",
        })
        if len(rows) > 500:
            raise DatabaseError("response")
        try:
            return [PromotionBook.model_validate(row) for row in rows]
        except ValidationError:
            raise DatabaseError("response") from None

    async def update_promotion_settings(
        self, settings_id: UUID, updated_at: AwareDatetime, mode: Literal["fixed_book", "random_book"],
        fixed_book_id: UUID | None,
    ) -> PromotionSettings:
        if mode == "fixed_book":
            if fixed_book_id is None:
                raise ValueError("Für die feste Promotion muss ein Buch ausgewählt werden.")
            eligible = await self._read("book_overview", {
                "select": "id", "id": f"eq.{fixed_book_id}", "active": "eq.true", "limit": "1",
            })
            if len(eligible) != 1 or eligible[0].get("id") != str(fixed_book_id):
                raise ValueError("Das ausgewählte Buch ist nicht für die Promotion aktiviert oder nicht mehr vorhanden.")
        else:
            fixed_book_id = None

        rows = await self._patch("promotion_settings", {
            "id": f"eq.{settings_id}", "updated_at": f"eq.{updated_at.isoformat()}",
            "select": "id,active,mode,fixed_book_id,updated_at",
        }, {"mode": mode, "fixed_book_id": str(fixed_book_id) if fixed_book_id else None})
        if len(rows) != 1:
            raise DatabaseError("conflict")
        try:
            return PromotionSettings.model_validate(rows[0])
        except ValidationError:
            raise DatabaseError("response") from None

    async def quote_usage(self, ids: list[str]) -> dict[str, QuoteUsage]:
        if not ids: return {}
        if len(ids)>100: raise ValueError('Zu viele Zitate für eine Statusabfrage.')
        ids=[str(UUID(value)) for value in ids]
        rows=await self._read('quote_overview',{'select':'id,last_published_at,reserved','id':'in.('+','.join(ids)+')','limit':str(len(ids))})
        try:
            values=[QuoteUsage.model_validate(row) for row in rows]
        except ValidationError: raise DatabaseError('response') from None
        return {str(row.id):row for row in values}
