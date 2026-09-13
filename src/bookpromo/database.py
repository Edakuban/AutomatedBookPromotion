"""Supabase Data and private Storage adapter; schema installation is never automatic."""

import hashlib
import re
from typing import Literal
from uuid import UUID

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from .config import Settings
from .overlay import OVERLAY_BUCKET

SCHEMA_VERSION = 5
MESSAGES = {
    "disabled": "Supabase ist bis Schritt 3.2 deaktiviert.",
    "configuration": "Supabase-URL und Server-Schlüssel fehlen oder sind ungeeignet.",
    "credentials": "Supabase hat den Zugriff abgelehnt. Server-Schlüssel und Berechtigungen prüfen.",
    "schema": "Das Supabase-Schema ist nicht auf Carousel-Version 5. Bitte zuerst die vorbereitete v5-Migration einspielen.",
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

    async def rpc(self, name: str, payload: dict) -> dict:
        """Explicit, atomic writes. A timeout can be retried using the same snapshot hash."""
        if name != "bookpromo_sync": raise ValueError("Unbekannte Schreiboperation.")
        try:
            async with httpx.AsyncClient(base_url=self._url, headers=self._headers, timeout=60,
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

    async def check_schema(self, *, full: bool = False) -> None:
        rows = await self._read("bookpromo_schema", {"select": "version", "limit": "2"})
        if len(rows) != 1 or type(rows[0].get("version")) is not int or rows[0]["version"] != SCHEMA_VERSION:
            raise DatabaseError("schema")
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
            }
            for relation, columns in relations.items():
                await self._read(relation, {"select": columns, "limit": "0"})

    async def list_books(self, *, page: int = 1, page_size: int = 50) -> tuple[list[BookSummary], bool]:
        if page < 1 or not 1 <= page_size <= 100:
            raise ValueError("Ungültige Seitengröße oder Seitennummer.")
        rows = await self._read("book_overview", {
            "select": "id,title,author,active,displayed_version_id,status,chapter_count,quote_count,last_published_at",
            "order": "created_at.desc,id.asc", "offset": str((page - 1) * page_size),
            "limit": str(page_size + 1),
        })
        try:
            books = [BookSummary.model_validate(row) for row in rows[:page_size]]
        except ValidationError:
            raise DatabaseError("response") from None
        return books, len(rows) > page_size

    async def quote_usage(self, ids: list[str]) -> dict[str, QuoteUsage]:
        if not ids: return {}
        if len(ids)>100: raise ValueError('Zu viele Zitate für eine Statusabfrage.')
        ids=[str(UUID(value)) for value in ids]
        rows=await self._read('quote_overview',{'select':'id,last_published_at,reserved','id':'in.('+','.join(ids)+')','limit':str(len(ids))})
        try:
            values=[QuoteUsage.model_validate(row) for row in rows]
        except ValidationError: raise DatabaseError('response') from None
        return {str(row.id):row for row in values}
