"""Local book settings and manual quote blocks, separate from AI results."""

from contextlib import closing
import hashlib
import json
import re
import sqlite3
import time
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .analysis import AnalysisResult
from .uploads import UploadError


class BookDetails(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)

    title: str = Field(min_length=1, max_length=240)
    author: str = Field(default="", max_length=200)
    target_url: str = Field(default="", max_length=2000)
    promotion_enabled: bool = False
    publication_mode: Literal["review", "auto"] = "review"
    carousel_end_text: str = Field(default="", max_length=500)
    genre: str = Field(default="", max_length=200)
    mood: str = Field(default="", max_length=200)
    internal_summary: str = Field(default="", max_length=8000)
    world: str = Field(default="", max_length=4000)
    characters: str = Field(default="", max_length=6000)
    spoilers: str = Field(default="", max_length=4000)
    image_prompt_base: str = Field(default="", max_length=4000)
    caption_guidelines: str = Field(default="", max_length=4000)
    overlay_title_font: str = Field(default="", max_length=200)
    overlay_title_color: str = "#FFFFFF"

    @field_validator("target_url")
    @classmethod
    def valid_url(cls, value):
        if value:
            try:
                url = urlsplit(value)
                if (url.scheme not in {"http", "https"} or not url.hostname or url.username is not None
                    or url.password is not None or any(c.isspace() or ord(c) < 32 for c in value)):
                    raise ValueError("URL")
                _ = url.port
            except ValueError:
                raise ValueError("Bitte eine vollständige HTTP-/HTTPS-Zieladresse ohne Zugangsdaten angeben.") from None
        return value

    @field_validator("overlay_title_font")
    @classmethod
    def valid_overlay_font(cls, value):
        if any(ord(char) < 32 for char in value) or "/" in value or "\\" in value:
            raise ValueError("Bitte nur den Namen einer lokal installierten Schriftart angeben.")
        return value

    @field_validator("overlay_title_color")
    @classmethod
    def valid_overlay_color(cls, value):
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise ValueError("Bitte eine sechsstellige Hex-Farbe angeben.")
        return value.upper()


FILTERS = {"all": "Alle", "usable": "Nutzbar", "blocked": "Gesperrt", "unsuitable": "KI: nicht geeignet", "unused": "Unbenutzt", "used": "Verwendet"}


def quote_key(version_id, quote):
    # Independent of the model/run and of renamed/repartitioned chapters.
    return hashlib.sha256(json.dumps([version_id, quote.paragraph_ids, quote.text], ensure_ascii=False).encode()).hexdigest()


class ManagementStore:
    def __init__(self, uploads):
        self.uploads = uploads

    def connection(self):
        connection = sqlite3.connect(self.uploads.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def exists(connection, name):
        return connection.execute("select 1 from sqlite_master where type='table' and name=?", (name,)).fetchone() is not None

    @staticmethod
    def schema(connection):
        connection.execute("""create table if not exists local_book_settings (
            book_id text primary key, revision integer not null, details_json text not null, updated_at real not null
        )""")
        connection.execute("""create table if not exists local_quote_controls (
            book_id text not null, source_key text not null, blocked integer not null check(blocked in (0,1)),
            revision integer not null, updated_at real not null, primary key(book_id,source_key)
        )""")

    def _snapshot(self, connection, book_id, *, prefer_ai=False):
        book = connection.execute("select * from local_books where id=?", (book_id,)).fetchone()
        if book is None: raise UploadError("Das Buch wurde nicht gefunden.", 404)
        row = connection.execute("select * from local_book_settings where book_id=?", (book_id,)).fetchone() if self.exists(connection, "local_book_settings") else None
        details = BookDetails.model_validate_json(row["details_json"]) if row else BookDetails(title=book["title"])
        run = None
        result = None
        if self.exists(connection, "local_analysis_runs"):
            run = connection.execute("""select r.id,r.state,r.result_json,r.extraction_revision,e.revision
                from local_analysis_runs r join local_extractions e on e.book_id=r.book_id
                where r.book_id=? and coalesce(json_extract(r.options_json,'$.purpose'),'full')='full'
                order by r.updated_at desc,r.rowid desc limit 1""", (book_id,)).fetchone()
            if run and run["state"] in {"done", "empty"} and run["extraction_revision"] == run["revision"] and run["result_json"]:
                result = AnalysisResult.model_validate_json(run["result_json"])
        suggestion_id = run["id"] if result else ""
        if result and (row is None or prefer_ai):
            profile = result.profile.model_dump()
            profile["characters"] = "\n".join(profile["characters"])
            profile["spoilers"] = "\n".join(profile["spoilers"])
            details = details.model_copy(update=profile)
        controls = {}
        if self.exists(connection, "local_quote_controls"):
            controls = {r["source_key"]: r for r in connection.execute("select * from local_quote_controls where book_id=?", (book_id,))}
        quotes = []
        for quote in result.quotes if result else []:
            control = controls.get(quote_key(book["version_id"], quote))
            blocked = bool(control and control["blocked"])
            quotes.append({"quote": quote, "blocked": blocked, "usable": quote.usable and not blocked,
                           "revision": control["revision"] if control else 0})
        return {"details": details, "revision": row["revision"] if row else 0, "manual": row is not None,
                "suggestion_id": suggestion_id, "preview_ai": prefer_ai, "quotes": quotes,
                "usable_count": sum(q["usable"] for q in quotes), "blocked_count": sum(q["blocked"] for q in quotes),
                "analysis_state": "stale" if run and run["extraction_revision"] != run["revision"] else run["state"] if run else "not_started"}

    def get(self, book_id, *, prefer_ai=False):
        if not self.uploads.db_path.is_file(): raise UploadError("Das Buch wurde nicht gefunden.", 404)
        try:
            with closing(self.uploads._read_connection()) as connection:
                connection.execute("begin")
                return self._snapshot(connection, book_id, prefer_ai=prefer_ai)
        except ValidationError:
            raise UploadError("Die gespeicherten Bucheinstellungen oder Analyseergebnisse sind beschädigt. Bitte die Datensicherung prüfen.", 503) from None

    def save(self, book_id, revision, details: BookDetails, suggestion_id, profile_run_id=""):
        if details.promotion_enabled:
            # Imported lazily so the renderer can use BookDetails without a
            # module-import cycle.
            from .book_assets import BookAssetStore
            from .carousel_end_slide import render_book_carousel_end_slide
            from .overlay import OverlayError, OverlayStore

            asset_store = BookAssetStore(self.uploads)
            missing = asset_store.configuration_errors(book_id, details)
            if missing:
                raise UploadError("Für die Promotion bitte zuerst: " + ", ".join(missing) + ".", 409)
            try:
                overlay = OverlayStore(self.uploads).prepare(details)
                if overlay is None:
                    raise UploadError("Bitte zuerst das Buchtitel-Overlay konfigurieren.", 409)
                render_book_carousel_end_slide(asset_store, book_id, details, overlay)
            except OverlayError as error:
                raise UploadError(str(error), 409) from None
        with closing(self.connection()) as connection, connection:
            self.schema(connection)
            connection.execute("begin immediate")
            current = self._snapshot(connection, book_id)
            if revision != current["revision"]:
                raise UploadError("Die Bucheinstellungen wurden zwischenzeitlich geändert. Bitte die Seite neu öffnen und Änderungen vergleichen.", 409)
            if suggestion_id != current["suggestion_id"]:
                raise UploadError("Der Analysestand wurde inzwischen geändert. Bitte die Bucheinstellungen neu öffnen.", 409)
            if profile_run_id:
                profile = connection.execute("""select 1 from local_analysis_runs r join local_extractions e
                    on e.book_id=r.book_id and e.revision=r.extraction_revision
                    where r.id=? and r.book_id=? and r.state='done'
                    and json_extract(r.options_json,'$.purpose')='profile'""", (profile_run_id, book_id)).fetchone() if self.exists(connection, "local_analysis_runs") else None
                if profile is None:
                    raise UploadError("Der verwendete Profilvorschlag ist veraltet. Bitte das Profil für den aktuellen Textstand neu erstellen.", 409)
            connection.execute("""insert into local_book_settings values (?,?,?,?)
                on conflict(book_id) do update set revision=excluded.revision,details_json=excluded.details_json,updated_at=excluded.updated_at""",
                (book_id, revision+1, details.model_dump_json(), time.time()))
            connection.execute("update local_books set title=? where id=?", (details.title, book_id))

    def set_blocked(self, book_id, chapter_id, run_id, quote_id, revision, blocked):
        with closing(self.connection()) as connection, connection:
            self.schema(connection)
            connection.execute("begin immediate")
            current = self._snapshot(connection, book_id)
            if not run_id or run_id != current["suggestion_id"]:
                raise UploadError("Die Zitatansicht ist veraltet. Bitte das Kapitel neu öffnen.", 409)
            item = next((q for q in current["quotes"] if q["quote"].id == quote_id and q["quote"].chapter_id == chapter_id), None)
            if item is None: raise UploadError("Das Zitat wurde in diesem Kapitel nicht gefunden.", 404)
            if item["revision"] != revision:
                raise UploadError("Die Zitatsperre wurde zwischenzeitlich geändert. Bitte das Kapitel neu öffnen.", 409)
            version_id = connection.execute("select version_id from local_books where id=?", (book_id,)).fetchone()[0]
            connection.execute("""insert into local_quote_controls values (?,?,?,?,?)
                on conflict(book_id,source_key) do update set blocked=excluded.blocked,revision=excluded.revision,updated_at=excluded.updated_at""",
                (book_id, quote_key(version_id, item["quote"]), int(blocked), revision+1, time.time()))
