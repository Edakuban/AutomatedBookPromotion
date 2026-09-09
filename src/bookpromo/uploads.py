"""Bounded DOCX validation and durable local staging, independent of Supabase."""

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import BinaryIO
from uuid import UUID, uuid4
import zipfile
import zlib

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException


class UploadError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def display_filename(value: str | None) -> str:
    name = (value or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or len(name) > 240 or any(ord(c) < 32 for c in name):
        raise UploadError("Bitte eine Datei mit einem gültigen Namen auswählen.")
    if Path(name).suffix.lower() != ".docx":
        raise UploadError("Bitte ein Word-Dokument im Format .docx auswählen.", 415)
    return name


def validate_docx(path: Path) -> None:
    """Check the Word package without extracting files or interpreting book text."""
    max_expanded = 100 * 1024 * 1024
    max_xml = 20 * 1024 * 1024
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if len(entries) > 4096 or sum(entry.file_size for entry in entries) > max_expanded:
                raise UploadError("Das entpackte Dokument ist zu groß oder enthält zu viele Bestandteile.", 413)
            if len(names) != len(set(names)) or any(entry.flag_bits & 1 for entry in entries):
                raise UploadError("Das Word-Dokument ist beschädigt oder verschlüsselt.")
            if any(name.lower().endswith("vbaproject.bin") for name in names):
                raise UploadError("Bitte das Dokument in Word als DOCX ohne Makros speichern.", 415)
            roots = {}
            for name in ("[Content_Types].xml", "_rels/.rels", "word/document.xml"):
                if archive.getinfo(name).file_size > max_xml:
                    raise UploadError("Die XML-Daten des Dokuments sind zu groß.", 413)
                roots[name] = ElementTree.fromstring(archive.read(name), forbid_dtd=True)
            content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
            if not any(node.get("PartName") == "/word/document.xml" and node.get("ContentType") == content_type
                       for node in roots["[Content_Types].xml"]):
                raise UploadError("Die Datei ist kein unterstütztes DOCX-Dokument.")
            if not any(node.get("Type", "").endswith("/officeDocument")
                       and node.get("Target", "").lstrip("/") == "word/document.xml"
                       and node.get("TargetMode", "Internal") == "Internal"
                       for node in roots["_rels/.rels"]):
                raise UploadError("Die Word-Dokumentstruktur ist unvollständig.")
            document = roots["word/document.xml"]
            namespaces = ("http://schemas.openxmlformats.org/wordprocessingml/2006/main",
                          "http://purl.oclc.org/ooxml/wordprocessingml/main")
            if not any(document.tag == f"{{{ns}}}document" and document.find(f"{{{ns}}}body") is not None
                       for ns in namespaces):
                raise UploadError("Das Dokument enthält keinen lesbaren Word-Hauptteil.")
            if archive.testzip() is not None:
                raise UploadError("Die Word-Datei ist beschädigt. Bitte erneut aus Word speichern.")
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, DefusedXmlException,
            RuntimeError, NotImplementedError, zlib.error, EOFError):
        raise UploadError("Das Word-Dokument ist beschädigt, verschlüsselt oder kein gültiges DOCX.") from None


@dataclass(frozen=True)
class LocalBook:
    id: str
    version_id: str
    job_id: str
    title: str
    filename: str
    file_sha256: str
    size_bytes: int
    source_path: str
    created_at: datetime

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "LocalBook":
        values = dict(row)
        values["created_at"] = datetime.fromisoformat(values["created_at"])
        return cls(**values)


class LocalUploadStore:
    """Local inbox with stable book/version/job IDs for later Supabase transfer.

    Read operations do not create directories. A write transaction serializes
    duplicate detection across processes. The source is saved before committing
    metadata, so a completed upload always has its original file.
    """
    def __init__(self, data_dir: Path, max_bytes: int):
        self.root = data_dir.resolve()
        self.db_path = self.root / "uploads.sqlite3"
        self.max_bytes = max_bytes

    def _read_connection(self):
        connection = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def list_books(self, page: int = 1, page_size: int = 50) -> tuple[list[LocalBook], bool]:
        if not self.db_path.is_file():
            return [], False
        with closing(self._read_connection()) as connection:
            rows = connection.execute(
                "select * from local_books order by created_at desc, id limit ? offset ?",
                (page_size + 1, (page - 1) * page_size),
            ).fetchall()
        return [LocalBook.from_row(row) for row in rows[:page_size]], len(rows) > page_size

    def get_book(self, book_id: UUID) -> LocalBook | None:
        if not self.db_path.is_file():
            return None
        with closing(self._read_connection()) as connection:
            row = connection.execute("select * from local_books where id = ?", (str(book_id),)).fetchone()
        return LocalBook.from_row(row) if row else None

    def save(self, source: BinaryIO, original_name: str | None) -> tuple[LocalBook, bool]:
        name = display_filename(original_name)
        pending = self.root / "pending"
        pending.mkdir(parents=True, exist_ok=True)
        temp_path = None
        stored_path = None
        committed = False
        try:
            digest, size = hashlib.sha256(), 0
            source.seek(0)
            with tempfile.NamedTemporaryFile(dir=pending, suffix=".part", delete=False) as temporary:
                temp_path = Path(temporary.name)
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise UploadError(f"Die Datei darf höchstens {self.max_bytes // (1024 * 1024)} MB groß sein.", 413)
                    digest.update(chunk)
                    temporary.write(chunk)
                temporary.flush()
                os.fsync(temporary.fileno())
            if size == 0:
                raise UploadError("Die ausgewählte Datei ist leer.")
            validate_docx(temp_path)
            with closing(sqlite3.connect(self.db_path, timeout=15)) as connection:
                connection.row_factory = sqlite3.Row
                with connection:
                    connection.execute("""create table if not exists local_books (
                        id text primary key, version_id text not null unique, job_id text not null unique,
                        title text not null, filename text not null, file_sha256 text not null unique,
                        size_bytes integer not null, source_path text not null, created_at text not null
                    )""")
                    connection.execute("begin immediate")
                    existing = connection.execute("select * from local_books where file_sha256 = ?", (digest.hexdigest(),)).fetchone()
                    if existing:
                        book = LocalBook.from_row(existing)
                        # Restore a missing original on re-upload while preserving IDs.
                        destination = self.root / "originals" / f"{UUID(book.version_id)}.docx"
                        if not destination.is_file():
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            os.replace(temp_path, destination)
                        return book, True
                    book_id, version_id, job_id = str(uuid4()), str(uuid4()), str(uuid4())
                    relative = f"originals/{version_id}.docx"
                    created = datetime.now(timezone.utc)
                    destination = self.root / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(temp_path, destination)
                    stored_path = destination
                    connection.execute("insert into local_books values (?,?,?,?,?,?,?,?,?)", (
                        book_id, version_id, job_id, Path(name).stem, name, digest.hexdigest(), size, relative, created.isoformat(),
                    ))
                committed = True
            return LocalBook(book_id, version_id, job_id, Path(name).stem, name, digest.hexdigest(), size, relative, created), False
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            if stored_path is not None and not committed:
                stored_path.unlink(missing_ok=True)
