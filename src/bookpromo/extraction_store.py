"""Atomic local extraction snapshots; existing step-4 uploads need no migration."""

from contextlib import closing
from dataclasses import dataclass
import hashlib
import sqlite3
from uuid import UUID
from pydantic import ValidationError

from .extraction import Boundary, Extraction, apply_boundaries, extract_docx
from .uploads import LocalBook, LocalUploadStore, UploadError


@dataclass(frozen=True)
class ExtractionRecord:
    revision: int
    result: Extraction | None
    error: str | None

    @property
    def status(self):
        if self.error: return "Einlesen fehlgeschlagen"
        if self.result and self.result.needs_review: return "Prüfung nötig"
        return "Text eingelesen"


class ExtractionStore:
    def __init__(self, uploads: LocalUploadStore):
        self.uploads = uploads

    @staticmethod
    def _decode(value: str) -> Extraction:
        try:
            return Extraction.model_validate_json(value)
        except ValidationError:
            raise UploadError("Der gespeicherte Textstand ist beschädigt oder inkompatibel. Bitte die lokale Datensicherung prüfen.", 503) from None

    @staticmethod
    def _table(connection):
        connection.execute("""create table if not exists local_extractions (
            book_id text primary key, revision integer not null,
            result_json text, error text
        )""")

    def get(self, book_id: str) -> ExtractionRecord | None:
        if not self.uploads.db_path.is_file(): return None
        with closing(self.uploads._read_connection()) as connection:
            if not connection.execute("select 1 from sqlite_master where type='table' and name='local_extractions'").fetchone():
                return None
            row = connection.execute("select * from local_extractions where book_id=?", (book_id,)).fetchone()
        if row is None: return None
        return ExtractionRecord(row["revision"], self._decode(row["result_json"]) if row["result_json"] else None, row["error"])

    def extract(self, book: LocalBook) -> ExtractionRecord:
        # Serialize extraction and corrections across requests/processes. A killed
        # process rolls back the snapshot; the preserved upload can be retried.
        with closing(sqlite3.connect(self.uploads.db_path, timeout=60)) as connection, connection:
            self._table(connection)
            connection.execute("begin immediate")
            existing = connection.execute("select revision, result_json from local_extractions where book_id=?", (book.id,)).fetchone()
            if existing and existing[1]:
                return ExtractionRecord(existing[0], self._decode(existing[1]), None)
            result, error = self.prepare(book)
            revision = existing[0] + 1 if existing else 1
            connection.execute("insert or replace into local_extractions values (?,?,?,?)",
                (book.id, revision, result.model_dump_json() if result else None, error))
        return ExtractionRecord(revision, result, error)

    def prepare(self, book: LocalBook, progress=None):
        """Compute outside a database transaction; the worker commits with its lease."""
        try:
            if progress: progress("Datei prüfen", 0, 0)
            path = self.uploads.root / "originals" / f"{UUID(book.version_id)}.docx"
            with path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            if digest != book.file_sha256:
                raise UploadError("Die gespeicherte Originaldatei wurde verändert. Bitte die ursprüngliche Datei erneut bereitstellen oder die neue Version hochladen.")
            return extract_docx(path, book.version_id, progress), None
        except UploadError as exc:
            return None, str(exc)
        except OSError:
            return None, "Die Originaldatei konnte nicht gelesen werden. Bitte die Datei erneut hochladen oder die Zugriffsrechte prüfen."

    def correct(self, book_id: str, revision: int, boundaries: list[Boundary]) -> None:
        with closing(sqlite3.connect(self.uploads.db_path, timeout=60)) as connection, connection:
            connection.execute("begin immediate")
            row = connection.execute("select revision, result_json from local_extractions where book_id=?", (book_id,)).fetchone()
            if row is None or row[0] != revision or not row[1]:
                raise UploadError("Der Textstand hat sich geändert. Bitte die Buchseite neu laden und erneut prüfen.", 409)
            result = apply_boundaries(self._decode(row[1]), boundaries)
            connection.execute("update local_extractions set revision=?,result_json=? where book_id=?",
                (revision + 1, result.model_dump_json(), book_id))
            if connection.execute("select 1 from sqlite_master where name='local_import_jobs'").fetchone():
                connection.execute("update local_import_jobs set state=?,chapter_count=? where book_id=? and state in ('done','needs_review')",
                    ("needs_review" if result.needs_review else "done", len(result.chapters), book_id))
            if connection.execute("select 1 from sqlite_master where name='local_analysis_runs'").fetchone():
                connection.execute("update local_analysis_runs set state='stale',token=null,lease_until=null,stage='Kapitelaufteilung wurde geändert' where book_id=? and extraction_revision<>?",
                                   (book_id, revision + 1))
