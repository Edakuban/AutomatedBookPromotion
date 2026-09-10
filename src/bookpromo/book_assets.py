"""Validated and revision-protected local image assets for book projects."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
from io import BytesIO
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import BinaryIO
from uuid import UUID
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from .uploads import UploadError


ASSET_KINDS = {"cover_front", "logo"}
MAX_ASSET_BYTES = 10 * 1024 * 1024
MAX_ASSET_PIXELS = 40_000_000


@dataclass(frozen=True)
class BookImageAsset:
    book_id: str
    kind: str
    revision: int
    original_filename: str
    media_type: str
    sha256: str
    width: int
    height: int
    size_bytes: int
    relative_path: str
    updated_at: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "BookImageAsset":
        return cls(**dict(row))


def _safe_filename(value: str | None) -> str:
    name = (value or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or len(name) > 240 or any(ord(char) < 32 for char in name):
        raise UploadError("Bitte eine Bilddatei mit einem gültigen Namen auswählen.")
    if Path(name).suffix.casefold() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise UploadError("Bitte ein Bild im Format PNG, JPEG oder WebP auswählen.", 415)
    return name


def _has_alpha(image: Image.Image) -> bool:
    return image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info)


def _decode_and_normalize(data: bytes, kind: str) -> tuple[bytes, int, int]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as opened:
                if opened.format not in {"PNG", "JPEG", "WEBP"}:
                    raise UploadError("Bitte ein Bild im Format PNG, JPEG oder WebP auswählen.", 415)
                width, height = opened.size
                if width <= 0 or height <= 0 or width * height > MAX_ASSET_PIXELS:
                    raise UploadError("Das Bild besitzt zu viele Pixel.", 413)
                opened.load()
                image = ImageOps.exif_transpose(opened)
                width, height = image.size
                if kind == "cover_front" and width < 800:
                    raise UploadError("Das Frontcover muss mindestens 800 Pixel breit sein.")
                if kind == "logo" and (width < 64 or height < 32):
                    raise UploadError("Das Logo muss mindestens 64 × 32 Pixel groß sein.")
                mode = "RGBA" if kind == "logo" or _has_alpha(image) else "RGB"
                normalized = image.convert(mode)
    except UploadError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise UploadError("Das Bild besitzt zu viele Pixel.", 413) from None
    except (UnidentifiedImageError, OSError, ValueError):
        raise UploadError("Die Bilddatei ist beschädigt oder hat ein nicht unterstütztes Format.", 415) from None
    encoded = BytesIO()
    normalized.save(encoded, format="PNG", optimize=True, compress_level=9)
    return encoded.getvalue(), normalized.width, normalized.height


class BookAssetStore:
    """Validated, normalized local cover and logo assets."""

    def __init__(self, uploads):
        self.uploads = uploads
        self.root = uploads.root

    @staticmethod
    def schema(connection: sqlite3.Connection) -> None:
        connection.execute("""create table if not exists local_book_assets (
            book_id text not null,
            kind text not null check(kind in ('cover_front','logo')),
            revision integer not null check(revision >= 1),
            original_filename text not null,
            media_type text not null,
            sha256 text not null,
            width integer not null check(width > 0),
            height integer not null check(height > 0),
            size_bytes integer not null check(size_bytes > 0),
            relative_path text not null,
            updated_at real not null,
            primary key(book_id, kind),
            foreign key(book_id) references local_books(id) on delete cascade
        )""")

    @staticmethod
    def _table_exists(connection: sqlite3.Connection) -> bool:
        return connection.execute(
            "select 1 from sqlite_master where type='table' and name='local_book_assets'"
        ).fetchone() is not None

    def list(self, book_id: str | UUID) -> dict[str, BookImageAsset]:
        if not self.uploads.db_path.is_file():
            return {}
        with closing(self.uploads._read_connection()) as connection:
            if not self._table_exists(connection):
                return {}
            rows = connection.execute(
                "select * from local_book_assets where book_id=? order by kind", (str(book_id),)
            ).fetchall()
        return {row["kind"]: BookImageAsset.from_row(row) for row in rows}

    def get(self, book_id: str | UUID, kind: str) -> BookImageAsset | None:
        if kind not in ASSET_KINDS:
            raise ValueError("Unknown book asset kind")
        return self.list(book_id).get(kind)

    def asset_directory(self, book_id: str | UUID) -> Path:
        path = (self.root / "book-assets" / str(UUID(str(book_id)))).resolve()
        try:
            path.relative_to(self.root)
        except ValueError:
            raise UploadError("Der lokale Assetpfad ist ungültig.", 503) from None
        return path

    def path(self, asset: BookImageAsset) -> Path:
        path = (self.root / asset.relative_path).resolve()
        try:
            parent = self.asset_directory(asset.book_id)
            path.relative_to(parent)
        except (UploadError, ValueError):
            raise UploadError("Der gespeicherte Bildpfad ist ungültig.", 503) from None
        expected_name = f"{asset.kind}-{asset.sha256}.png"
        if path.parent != parent or path.name != expected_name:
            raise UploadError("Der gespeicherte Bildpfad ist ungültig.", 503)
        return path

    def open_image(self, asset: BookImageAsset) -> Image.Image:
        path = self.path(asset)
        if not path.is_file():
            raise UploadError("Eine gespeicherte Bilddatei fehlt. Bitte erneut hochladen.", 409)
        try:
            with Image.open(path) as opened:
                opened.load()
                return opened.convert("RGBA")
        except (UnidentifiedImageError, OSError):
            raise UploadError("Eine gespeicherte Bilddatei ist beschädigt. Bitte erneut hochladen.", 409) from None

    def _delete_unreferenced(self, book_id: str, relative_path: str) -> None:
        path = (self.root / relative_path).resolve()
        try:
            path.relative_to(self.asset_directory(book_id))
        except (UploadError, ValueError):
            return
        referenced = False
        if self.uploads.db_path.is_file():
            with closing(self.uploads._read_connection()) as connection:
                if self._table_exists(connection):
                    referenced = connection.execute(
                        "select 1 from local_book_assets where relative_path=?", (relative_path,)
                    ).fetchone() is not None
        if not referenced:
            path.unlink(missing_ok=True)

    def save(
        self,
        book_id: str | UUID,
        kind: str,
        source: BinaryIO,
        original_filename: str | None,
        expected_revision: int,
    ) -> BookImageAsset:
        book_id = str(UUID(str(book_id)))
        if kind not in ASSET_KINDS:
            raise UploadError("Unbekannte Bildart.", 404)
        if type(expected_revision) is not int or expected_revision < 0:
            raise UploadError("Die Bildansicht ist veraltet. Bitte die Seite neu öffnen.", 409)
        name = _safe_filename(original_filename)
        source.seek(0)
        chunks: list[bytes] = []
        size = 0
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_ASSET_BYTES:
                raise UploadError("Die Bilddatei darf höchstens 10 MB groß sein.", 413)
            chunks.append(chunk)
        if not chunks:
            raise UploadError("Die ausgewählte Bilddatei ist leer.")
        normalized, width, height = _decode_and_normalize(b"".join(chunks), kind)
        digest = hashlib.sha256(normalized).hexdigest()
        relative_path = f"book-assets/{book_id}/{kind}-{digest}.png"
        target = (self.root / relative_path).resolve()
        try:
            target.relative_to(self.root)
        except ValueError:
            raise UploadError("Der Zielpfad für das Bild ist ungültig.", 503) from None

        target.parent.mkdir(parents=True, exist_ok=True)
        created_target = False
        if not target.is_file():
            with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".part", delete=False) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(normalized)
                temporary.flush()
                os.fsync(temporary.fileno())
            try:
                os.replace(temporary_path, target)
                created_target = True
            finally:
                temporary_path.unlink(missing_ok=True)

        previous_relative_path: str | None = None
        try:
            with closing(sqlite3.connect(self.uploads.db_path, timeout=15)) as connection, connection:
                connection.row_factory = sqlite3.Row
                connection.execute("pragma foreign_keys=on")
                self.schema(connection)
                connection.execute("begin immediate")
                if connection.execute("select 1 from local_books where id=?", (book_id,)).fetchone() is None:
                    raise UploadError("Das Buch wurde nicht gefunden.", 404)
                current = connection.execute(
                    "select revision,relative_path from local_book_assets where book_id=? and kind=?", (book_id, kind)
                ).fetchone()
                revision = current["revision"] if current else 0
                if revision != expected_revision:
                    raise UploadError("Das Bild wurde zwischenzeitlich geändert. Bitte die Seite neu öffnen.", 409)
                previous_relative_path = current["relative_path"] if current else None
                updated_at = time.time()
                record = BookImageAsset(
                    book_id=book_id,
                    kind=kind,
                    revision=revision + 1,
                    original_filename=name,
                    media_type="image/png",
                    sha256=digest,
                    width=width,
                    height=height,
                    size_bytes=len(normalized),
                    relative_path=relative_path,
                    updated_at=updated_at,
                )
                connection.execute("""insert into local_book_assets values (?,?,?,?,?,?,?,?,?,?,?)
                    on conflict(book_id,kind) do update set
                    revision=excluded.revision,original_filename=excluded.original_filename,
                    media_type=excluded.media_type,sha256=excluded.sha256,width=excluded.width,
                    height=excluded.height,size_bytes=excluded.size_bytes,
                    relative_path=excluded.relative_path,updated_at=excluded.updated_at""",
                    (record.book_id, record.kind, record.revision, record.original_filename,
                     record.media_type, record.sha256, record.width, record.height,
                     record.size_bytes, record.relative_path, record.updated_at))
            if previous_relative_path and previous_relative_path != relative_path:
                self._delete_unreferenced(book_id, previous_relative_path)
            return record
        except Exception:
            if created_target:
                self._delete_unreferenced(book_id, relative_path)
            raise

    def configuration_errors(self, book_id: str | UUID, details) -> list[str]:
        assets = self.list(book_id)
        errors: list[str] = []
        if "cover_front" not in assets:
            errors.append("Frontcover hochladen")
        if "logo" not in assets:
            errors.append("Logo hochladen")
        if not str(getattr(details, "carousel_end_text", "")).strip():
            errors.append("Text für die Schlussseite eintragen")
        if not str(getattr(details, "overlay_title_font", "")).strip():
            errors.append("Schriftart für das Buchtitel-Overlay wählen")
        return errors
