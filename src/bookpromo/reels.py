"""Durable local assets, reel drafts, and fenced generation jobs.

The module deliberately has no ComfyUI, web, or Supabase dependency.  Workers
consume :class:`ReelJobStore` jobs and put immutable artifacts into
``data/reels``.  Failed or stale jobs never clear the last successful image or
video selected on a draft.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time
from typing import BinaryIO, Literal
from uuid import UUID, uuid4
import wave

from PIL import Image, UnidentifiedImageError

from .uploads import LocalUploadStore, UploadError
from .publication import PublicationDefaults, StoredPublicationDefaults


REEL_LEASE_SECONDS = 120
REEL_MAX_ATTEMPTS = 3
DEFAULT_MAX_AUDIO_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_ARTIFACT_BYTES = 128 * 1024 * 1024

JobKind = Literal["prompt", "image", "video", "upload"]
ArtifactKind = Literal["image", "video", "cover"]


@dataclass(frozen=True)
class AudioTrack:
    id: str
    book_id: str
    title: str
    filename: str
    sha256: str
    size_bytes: int
    relative_path: str
    duration_ms: int
    sample_rate: int
    channels: int
    sample_width: int

    @property
    def duration_seconds(self) -> float:
        return self.duration_ms / 1000

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "AudioTrack":
        return cls(**{key: row[key] for key in cls.__annotations__})


@dataclass(frozen=True)
class AudioCue:
    id: str
    track_id: str
    label: str
    start_ms: int
    duration_ms: int
    revision: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "AudioCue":
        return cls(**{key: row[key] for key in cls.__annotations__})


@dataclass(frozen=True)
class ReelDraft:
    id: str
    book_id: str
    quote_source_key: str
    analysis_run_id: str
    quote_id: str
    quote_text: str
    revision: int
    duration_ms: int
    audio_track_id: str | None
    audio_cue_id: str | None
    audio_start_ms: int | None
    caption_addition: str
    final_caption: str
    image_prompt: str
    video_prompt: str
    character_ids_json: str
    character_snapshot_json: str
    scene_image_path: str | None
    scene_image_sha256: str | None
    optimized_image_path: str | None
    optimized_image_sha256: str | None
    uploaded_image_path: str | None
    uploaded_image_sha256: str | None
    selected_image_source: str
    selected_image_path: str | None
    selected_image_sha256: str | None
    selected_video_path: str | None
    selected_video_sha256: str | None
    image_stale: bool
    video_stale: bool
    state: str
    error: str | None
    remote_video_path: str | None
    remote_cover_path: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ReelDraft":
        present = set(row.keys())
        values = {key: row[key] for key in cls.__annotations__ if key in present}
        # Read-only pages may encounter a database created by an older version
        # before the next write performs the additive migration.
        selected_path = values.get("selected_image_path")
        selected_hash = values.get("selected_image_sha256")
        values.setdefault("scene_image_path", selected_path)
        values.setdefault("scene_image_sha256", selected_hash)
        values.setdefault("optimized_image_path", None)
        values.setdefault("optimized_image_sha256", None)
        values.setdefault("uploaded_image_path", None)
        values.setdefault("uploaded_image_sha256", None)
        values.setdefault("selected_image_source", "scene")
        values["image_stale"] = bool(values["image_stale"])
        values["video_stale"] = bool(values["video_stale"])
        return cls(**values)

    @property
    def character_ids(self) -> tuple[str, ...]:
        return tuple(json.loads(self.character_ids_json or "[]"))

    @property
    def character_snapshot(self) -> tuple[dict, ...]:
        return tuple(json.loads(self.character_snapshot_json or "[]"))

    @property
    def image_candidates(self) -> tuple[tuple[str, str, str], ...]:
        candidates = []
        for source, label, path in (
            ("scene", "Szenenbild", self.scene_image_path),
            ("optimized", "Charakteroptimiert", self.optimized_image_path),
            ("upload", "Eigenes Bild", self.uploaded_image_path),
        ):
            if path:
                candidates.append((source, label, path))
        return tuple(candidates)


@dataclass(frozen=True)
class ReelJob:
    id: str
    draft_id: str
    kind: str
    input_revision: int
    state: str
    payload: dict
    attempts: int
    token: str | None = None


class ReelStore:
    """Book-scoped songs, reusable cues, drafts, and immutable artifacts."""

    def __init__(
        self,
        uploads: LocalUploadStore,
        *,
        max_audio_bytes: int = DEFAULT_MAX_AUDIO_BYTES,
        max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    ):
        self.uploads = uploads
        self.root = uploads.root
        self.max_audio_bytes = max_audio_bytes
        self.max_artifact_bytes = max_artifact_bytes

    def connection(self):
        connection = sqlite3.connect(self.uploads.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def schema(connection: sqlite3.Connection) -> None:
        connection.executescript("""
            create table if not exists local_audio_tracks (
                id text primary key, book_id text not null, title text not null,
                filename text not null, sha256 text not null, size_bytes integer not null,
                relative_path text not null, duration_ms integer not null,
                sample_rate integer not null, channels integer not null,
                sample_width integer not null, created_at real not null,
                unique(book_id,sha256)
            );
            create index if not exists local_audio_tracks_book
                on local_audio_tracks(book_id,created_at,id);
            create table if not exists local_audio_cues (
                id text primary key, track_id text not null, label text not null,
                start_ms integer not null, duration_ms integer not null,
                revision integer not null, created_at real not null, updated_at real not null,
                unique(track_id,label)
            );
            create index if not exists local_audio_cues_track
                on local_audio_cues(track_id,created_at,id);
            create table if not exists local_reel_drafts (
                id text primary key, book_id text not null, quote_source_key text not null,
                analysis_run_id text not null, quote_id text not null, quote_text text not null,
                revision integer not null, duration_ms integer not null,
                audio_track_id text, audio_cue_id text, audio_start_ms integer,
                caption_addition text not null, final_caption text not null,
                image_prompt text not null, video_prompt text not null,
                character_ids_json text not null default '[]',
                character_snapshot_json text not null default '[]',
                scene_image_path text, scene_image_sha256 text,
                optimized_image_path text, optimized_image_sha256 text,
                uploaded_image_path text, uploaded_image_sha256 text,
                selected_image_source text not null default 'scene',
                selected_image_path text, selected_image_sha256 text,
                selected_video_path text, selected_video_sha256 text,
                image_stale integer not null check(image_stale in (0,1)),
                video_stale integer not null check(video_stale in (0,1)),
                state text not null check(state in
                    ('editing','generating','ready','uploading','stocked','published','failed')),
                error text, remote_video_path text, remote_cover_path text,
                created_at real not null, updated_at real not null,
                unique(book_id,quote_source_key,analysis_run_id)
            );
            create index if not exists local_reel_drafts_book
                on local_reel_drafts(book_id,updated_at desc,id);
            create table if not exists local_reel_jobs (
                id text primary key, draft_id text not null,
                kind text not null check(kind in ('prompt','image','video','upload')),
                input_revision integer not null,
                state text not null check(state in
                    ('queued','running','done','failed','cancelled','stale')),
                payload_json text not null, result_json text,
                attempts integer not null default 0, token text, lease_until real,
                error text, created_at real not null, updated_at real not null
            );
            create index if not exists local_reel_jobs_queue
                on local_reel_jobs(state,created_at,id);
            create unique index if not exists local_reel_jobs_active
                on local_reel_jobs(draft_id,kind) where state in ('queued','running');
            create table if not exists local_reel_publication_defaults (
                singleton integer primary key check(singleton=1),
                revision integer not null,
                values_json text not null,
                updated_at real not null
            );
        """)
        columns = {row[1] for row in connection.execute("pragma table_info(local_reel_drafts)")}
        if "character_ids_json" not in columns:
            connection.execute("alter table local_reel_drafts add column character_ids_json text not null default '[]'")
        if "character_snapshot_json" not in columns:
            connection.execute("alter table local_reel_drafts add column character_snapshot_json text not null default '[]'")
        had_scene_image = "scene_image_path" in columns
        for name in (
            "scene_image_path", "scene_image_sha256", "optimized_image_path",
            "optimized_image_sha256", "uploaded_image_path", "uploaded_image_sha256",
        ):
            if name not in columns:
                connection.execute(f"alter table local_reel_drafts add column {name} text")
        if "selected_image_source" not in columns:
            connection.execute(
                "alter table local_reel_drafts add column selected_image_source text not null default 'scene'"
            )
        if not had_scene_image:
            connection.execute(
                """update local_reel_drafts set scene_image_path=selected_image_path,
                scene_image_sha256=selected_image_sha256 where selected_image_path is not null"""
            )

    def get_publication_defaults(self, default_provider: str = "supabase") -> StoredPublicationDefaults:
        if not self.uploads.db_path.is_file():
            return StoredPublicationDefaults(0, PublicationDefaults(storage_provider=default_provider))
        with closing(self.connection()) as connection, connection:
            self.schema(connection)
            row = connection.execute(
                "select revision,values_json from local_reel_publication_defaults where singleton=1"
            ).fetchone()
            if row is None:
                return StoredPublicationDefaults(0, PublicationDefaults(storage_provider=default_provider))
            try:
                values = PublicationDefaults.from_json(row["values_json"])
            except (ValueError, json.JSONDecodeError):
                raise UploadError("Die lokalen Reel-Veröffentlichungseinstellungen sind beschädigt.", 500) from None
            return StoredPublicationDefaults(row["revision"], values)

    def save_publication_defaults(
        self, revision: int, values: PublicationDefaults,
    ) -> StoredPublicationDefaults:
        if type(revision) is not int or revision < 0:
            raise UploadError("Die Einstellungsseite ist veraltet. Bitte neu laden.", 409)
        self.uploads.root.mkdir(parents=True, exist_ok=True)
        with closing(self.connection()) as connection, connection:
            self.schema(connection)
            connection.execute("begin immediate")
            if revision == 0:
                connection.execute(
                    """insert into local_reel_publication_defaults(singleton,revision,values_json,updated_at)
                       values(1,1,?,?) on conflict(singleton) do nothing""",
                    (values.as_json(), time.time()),
                )
                row = connection.execute(
                    "select revision,values_json from local_reel_publication_defaults where singleton=1"
                ).fetchone()
                if row["revision"] == 1 and row["values_json"] == values.as_json():
                    return StoredPublicationDefaults(1, values)
            changed = connection.execute(
                """update local_reel_publication_defaults
                   set revision=revision+1,values_json=?,updated_at=?
                   where singleton=1 and revision=?""",
                (values.as_json(), time.time(), revision),
            ).rowcount
            if changed != 1:
                raise UploadError("Die Reel-Einstellungen wurden zwischenzeitlich geändert. Bitte neu laden.", 409)
            row = connection.execute(
                "select revision,values_json from local_reel_publication_defaults where singleton=1"
            ).fetchone()
        return StoredPublicationDefaults(row["revision"], PublicationDefaults.from_json(row["values_json"]))

    @staticmethod
    def _normalize_book_id(book_id: str) -> str:
        try:
            return str(UUID(str(book_id)))
        except ValueError:
            raise UploadError("Das Buch wurde nicht gefunden.", 404) from None

    @staticmethod
    def _require_book(connection: sqlite3.Connection, book_id: str) -> str:
        book_id = ReelStore._normalize_book_id(book_id)
        row = connection.execute(
            "select 1 from sqlite_master where type='table' and name='local_books'"
        ).fetchone()
        if row is None or connection.execute(
            "select 1 from local_books where id=?", (book_id,)
        ).fetchone() is None:
            raise UploadError("Das Buch wurde nicht gefunden.", 404)
        return book_id

    @staticmethod
    def _filename(value: str | None, suffix: str) -> str:
        name = (value or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
        if (
            not name
            or len(name) > 240
            or any(ord(char) < 32 for char in name)
            or Path(name).suffix.lower() != suffix
        ):
            raise UploadError(f"Bitte eine gültige {suffix.upper()}-Datei auswählen.", 415)
        return name

    @staticmethod
    def _stream_to_temp(source: BinaryIO, destination: Path, limit: int) -> tuple[Path, str, int]:
        destination.mkdir(parents=True, exist_ok=True)
        digest, size = hashlib.sha256(), 0
        source.seek(0)
        path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination, suffix=".part", delete=False) as target:
                path = Path(target.name)
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise UploadError(
                            f"Die Datei darf höchstens {limit // (1024 * 1024)} MB groß sein.", 413
                        )
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        except Exception:
            if path is not None:
                path.unlink(missing_ok=True)
            raise
        assert path is not None
        if not size:
            path.unlink(missing_ok=True)
            raise UploadError("Die ausgewählte Datei ist leer.")
        return path, digest.hexdigest(), size

    @staticmethod
    def _wav_metadata(path: Path) -> tuple[int, int, int, int]:
        try:
            with wave.open(str(path), "rb") as audio:
                channels = audio.getnchannels()
                sample_width = audio.getsampwidth()
                sample_rate = audio.getframerate()
                frames = audio.getnframes()
                if (
                    audio.getcomptype() != "NONE"
                    or not 1 <= channels <= 8
                    or not 1 <= sample_width <= 4
                    or not 8_000 <= sample_rate <= 192_000
                    or frames <= 0
                ):
                    raise UploadError("Die WAV-Datei verwendet ein nicht unterstütztes Audioformat.", 415)
                # Reading through the declared frame count also catches truncated files.
                read_frames = 0
                while data := audio.readframes(min(sample_rate * 10, 1_000_000)):
                    read_frames += len(data) // (channels * sample_width)
                if read_frames != frames:
                    raise UploadError("Die WAV-Datei ist unvollständig oder beschädigt.", 415)
                duration_ms = round(frames * 1000 / sample_rate)
                if not 100 <= duration_ms <= 4 * 60 * 60 * 1000:
                    raise UploadError("Die WAV-Datei hat keine unterstützte Länge.", 415)
                return duration_ms, sample_rate, channels, sample_width
        except (wave.Error, EOFError, OSError):
            raise UploadError("Die Datei ist keine lesbare PCM-WAV-Datei.", 415) from None

    def save_audio(
        self, book_id: str, source: BinaryIO, filename: str | None, *, title: str | None = None
    ) -> tuple[AudioTrack, bool]:
        """Stream, validate, and store one immutable book-scoped PCM WAV."""
        name = self._filename(filename, ".wav")
        clean_title = (title if title is not None else Path(name).stem).strip()
        if not clean_title or len(clean_title) > 240 or any(ord(c) < 32 for c in clean_title):
            raise UploadError("Bitte einen gültigen Audiotitel angeben.")
        pending = self.root / "pending"
        temp, digest, size = self._stream_to_temp(source, pending, self.max_audio_bytes)
        stored: Path | None = None
        committed = False
        try:
            duration, rate, channels, width = self._wav_metadata(temp)
            with closing(self.connection()) as connection, connection:
                self.schema(connection)
                connection.execute("begin immediate")
                normalized_book = self._require_book(connection, book_id)
                old = connection.execute(
                    "select * from local_audio_tracks where book_id=? and sha256=?",
                    (normalized_book, digest),
                ).fetchone()
                if old:
                    return AudioTrack.from_row(old), True
                relative = f"audio/{normalized_book}/{digest}.wav"
                stored = self.root / relative
                stored.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temp, stored)
                track_id, now = str(uuid4()), time.time()
                connection.execute(
                    """insert into local_audio_tracks
                    (id,book_id,title,filename,sha256,size_bytes,relative_path,duration_ms,
                     sample_rate,channels,sample_width,created_at)
                    values (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (track_id, normalized_book, clean_title, name, digest, size, relative,
                     duration, rate, channels, width, now),
                )
                row = connection.execute(
                    "select * from local_audio_tracks where id=?", (track_id,)
                ).fetchone()
            committed = True
            return AudioTrack.from_row(row), False
        finally:
            temp.unlink(missing_ok=True)
            if stored is not None and not committed:
                stored.unlink(missing_ok=True)

    def list_audio(self, book_id: str) -> list[AudioTrack]:
        if not self.uploads.db_path.is_file():
            return []
        normalized = self._normalize_book_id(book_id)
        with closing(self.uploads._read_connection()) as connection:
            if not connection.execute(
                "select 1 from sqlite_master where type='table' and name='local_audio_tracks'"
            ).fetchone():
                return []
            rows = connection.execute(
                "select * from local_audio_tracks where book_id=? order by created_at,id", (normalized,)
            ).fetchall()
        return [AudioTrack.from_row(row) for row in rows]

    def audio_path(self, track: AudioTrack | str) -> Path:
        """Resolve a stored WAV without trusting a caller-supplied filesystem path."""
        if isinstance(track, str):
            if not self.uploads.db_path.is_file():
                raise UploadError("Die Audiodatei wurde nicht gefunden.", 404)
            with closing(self.uploads._read_connection()) as connection:
                if not connection.execute(
                    "select 1 from sqlite_master where type='table' and name='local_audio_tracks'"
                ).fetchone():
                    raise UploadError("Die Audiodatei wurde nicht gefunden.", 404)
                row = connection.execute(
                    "select * from local_audio_tracks where id=?", (track,)
                ).fetchone()
            if row is None:
                raise UploadError("Die Audiodatei wurde nicht gefunden.", 404)
            track = AudioTrack.from_row(row)
        expected = (self.root / "audio" / track.book_id / f"{track.sha256}.wav").resolve()
        path = (self.root / track.relative_path).resolve()
        if path != expected or not path.is_file():
            raise UploadError("Der gespeicherte Audiopfad ist ungültig oder fehlt.", 503)
        return path

    def save_cue(
        self,
        track_id: str,
        label: str,
        start_ms: int,
        duration_ms: int,
        *,
        cue_id: str | None = None,
        revision: int = 0,
    ) -> AudioCue:
        label = label.strip()
        if not label or len(label) > 120 or any(ord(char) < 32 for char in label):
            raise UploadError("Bitte einen gültigen Namen für den Audioausschnitt angeben.")
        if start_ms < 0 or duration_ms < 1_000:
            raise UploadError("Der Audioausschnitt muss mindestens eine Sekunde lang sein.")
        with closing(self.connection()) as connection, connection:
            self.schema(connection)
            connection.execute("begin immediate")
            track = connection.execute(
                "select duration_ms from local_audio_tracks where id=?", (track_id,)
            ).fetchone()
            if track is None:
                raise UploadError("Die Audiodatei wurde nicht gefunden.", 404)
            if start_ms + duration_ms > track["duration_ms"]:
                raise UploadError("Der Audioausschnitt liegt außerhalb der ausgewählten Datei.")
            now = time.time()
            if cue_id is None:
                cue_id = str(uuid4())
                try:
                    connection.execute(
                        "insert into local_audio_cues values (?,?,?,?,?,?,?,?)",
                        (cue_id, track_id, label, start_ms, duration_ms, 1, now, now),
                    )
                except sqlite3.IntegrityError:
                    raise UploadError("Ein Audioausschnitt mit diesem Namen existiert bereits.", 409) from None
            else:
                changed = connection.execute(
                    """update local_audio_cues set label=?,start_ms=?,duration_ms=?,
                    revision=revision+1,updated_at=? where id=? and track_id=? and revision=?""",
                    (label, start_ms, duration_ms, now, cue_id, track_id, revision),
                ).rowcount
                if changed != 1:
                    raise UploadError(
                        "Der Audioausschnitt wurde zwischenzeitlich geändert. Bitte neu laden.", 409
                    )
            row = connection.execute("select * from local_audio_cues where id=?", (cue_id,)).fetchone()
        return AudioCue.from_row(row)

    def list_cues(self, track_id: str) -> list[AudioCue]:
        if not self.uploads.db_path.is_file():
            return []
        with closing(self.uploads._read_connection()) as connection:
            if not connection.execute(
                "select 1 from sqlite_master where type='table' and name='local_audio_cues'"
            ).fetchone():
                return []
            rows = connection.execute(
                "select * from local_audio_cues where track_id=? order by created_at,id", (track_id,)
            ).fetchall()
        return [AudioCue.from_row(row) for row in rows]

    def get_or_create_draft(
        self,
        book_id: str,
        quote_source_key: str,
        analysis_run_id: str,
        quote_id: str,
        quote_text: str,
    ) -> ReelDraft:
        if not re.fullmatch(r"[0-9a-f]{64}", quote_source_key):
            raise UploadError("Der Zitatschlüssel ist ungültig.")
        if not quote_text.strip() or len(quote_text) > 20_000:
            raise UploadError("Das Zitat fehlt oder ist zu lang.")
        if len(analysis_run_id) > 100 or len(quote_id) > 100:
            raise UploadError("Der Analysestand ist ungültig.")
        with closing(self.connection()) as connection, connection:
            self.schema(connection)
            connection.execute("begin immediate")
            book_id = self._require_book(connection, book_id)
            old = connection.execute(
                """select * from local_reel_drafts
                where book_id=? and quote_source_key=? and analysis_run_id=?""",
                (book_id, quote_source_key, analysis_run_id),
            ).fetchone()
            if old:
                return ReelDraft.from_row(old)
            draft_id, now = str(uuid4()), time.time()
            connection.execute(
                """insert into local_reel_drafts
                (id,book_id,quote_source_key,analysis_run_id,quote_id,quote_text,revision,
                 duration_ms,caption_addition,final_caption,image_prompt,video_prompt,
                 character_ids_json,character_snapshot_json,image_stale,video_stale,state,created_at,updated_at)
                values (?,?,?,?,?,?,1,10000,'','','','','[]','[]',1,1,'editing',?,?)""",
                (draft_id, book_id, quote_source_key, analysis_run_id, quote_id, quote_text, now, now),
            )
            row = connection.execute(
                "select * from local_reel_drafts where id=?", (draft_id,)
            ).fetchone()
        return ReelDraft.from_row(row)

    def get_draft(self, draft_id: str) -> ReelDraft | None:
        if not self.uploads.db_path.is_file():
            return None
        with closing(self.uploads._read_connection()) as connection:
            if not connection.execute(
                "select 1 from sqlite_master where type='table' and name='local_reel_drafts'"
            ).fetchone():
                return None
            row = connection.execute(
                "select * from local_reel_drafts where id=?", (draft_id,)
            ).fetchone()
        return ReelDraft.from_row(row) if row else None

    def find_draft(
        self, book_id: str, quote_source_key: str, analysis_run_id: str
    ) -> ReelDraft | None:
        """Find the stable quote/run draft without creating storage as a side effect."""
        if not self.uploads.db_path.is_file():
            return None
        book_id = self._normalize_book_id(book_id)
        with closing(self.uploads._read_connection()) as connection:
            if not connection.execute(
                "select 1 from sqlite_master where type='table' and name='local_reel_drafts'"
            ).fetchone():
                return None
            row = connection.execute(
                """select * from local_reel_drafts where book_id=?
                and quote_source_key=? and analysis_run_id=?""",
                (book_id, quote_source_key, analysis_run_id),
            ).fetchone()
        return ReelDraft.from_row(row) if row else None

    def list_drafts(self, book_id: str) -> list[ReelDraft]:
        if not self.uploads.db_path.is_file():
            return []
        book_id = self._normalize_book_id(book_id)
        with closing(self.uploads._read_connection()) as connection:
            if not connection.execute(
                "select 1 from sqlite_master where type='table' and name='local_reel_drafts'"
            ).fetchone():
                return []
            rows = connection.execute(
                "select * from local_reel_drafts where book_id=? order by updated_at desc,id",
                (book_id,),
            ).fetchall()
        return [ReelDraft.from_row(row) for row in rows]

    def artifact_path(
        self,
        draft: ReelDraft | str,
        kind: Literal["image", "video"],
        relative_path: str | None = None,
    ) -> Path | None:
        """Resolve a current or historical artifact, bounded to this draft's directory."""
        if isinstance(draft, str):
            found = self.get_draft(draft)
            if found is None:
                raise UploadError("Der Reel-Entwurf wurde nicht gefunden.", 404)
            draft = found
        if relative_path is None:
            relative_path = (
                draft.selected_image_path if kind == "image" else draft.selected_video_path
            )
        if relative_path is None:
            return None
        path = (self.root / relative_path).resolve()
        expected_root = (self.root / "reels" / draft.book_id / draft.id / kind).resolve()
        try:
            path.relative_to(expected_root)
        except ValueError:
            raise UploadError("Der gespeicherte Medienpfad ist ungültig.", 503) from None
        if not path.is_file():
            raise UploadError("Die gespeicherte Mediendatei fehlt.", 503)
        return path

    def candidate_image_path(self, draft: ReelDraft | str, source: str = "selected") -> Path | None:
        if isinstance(draft, str):
            found = self.get_draft(draft)
            if found is None:
                raise UploadError("Der Reel-Entwurf wurde nicht gefunden.", 404)
            draft = found
        paths = {
            "selected": draft.selected_image_path,
            "scene": draft.scene_image_path,
            "optimized": draft.optimized_image_path,
            "upload": draft.uploaded_image_path,
        }
        if source not in paths:
            raise UploadError("Die Bildvariante ist ungültig.")
        return self.artifact_path(draft, "image", paths[source])

    def select_image(self, draft_id: str, revision: int, source: str) -> ReelDraft:
        with closing(self.connection()) as connection, connection:
            self.schema(connection)
            connection.execute("begin immediate")
            return self._select_image(connection, draft_id, revision, source)

    def _select_image(
        self, connection: sqlite3.Connection, draft_id: str, revision: int, source: str,
    ) -> ReelDraft:
        """Select an image inside an existing transaction (also used by chapter review)."""
        fields = {
            "scene": ("scene_image_path", "scene_image_sha256"),
            "optimized": ("optimized_image_path", "optimized_image_sha256"),
            "upload": ("uploaded_image_path", "uploaded_image_sha256"),
        }
        if source not in fields:
            raise UploadError("Bitte eine vorhandene Bildvariante auswählen.")
        path_field, hash_field = fields[source]
        draft = connection.execute(
            "select * from local_reel_drafts where id=?", (draft_id,)
        ).fetchone()
        if draft is None:
            raise UploadError("Der Reel-Entwurf wurde nicht gefunden.", 404)
        if draft["revision"] != revision:
            raise UploadError("Der Reel-Entwurf wurde zwischenzeitlich geändert. Bitte neu laden.", 409)
        relative, digest = draft[path_field], draft[hash_field]
        if not relative or not digest:
            raise UploadError("Die ausgewählte Bildvariante ist nicht mehr vorhanden.", 409)
        self._validate_stored_image(draft, relative, digest)
        if (draft["selected_image_source"] == source
                and draft["selected_image_path"] == relative and not draft["image_stale"]):
            return ReelDraft.from_row(draft)
        connection.execute(
            """update local_reel_drafts set selected_image_source=?,selected_image_path=?,
            selected_image_sha256=?,image_stale=0,video_stale=1,state='editing',error=null,
            revision=revision+1,updated_at=? where id=? and revision=?""",
            (source, relative, digest, time.time(), draft_id, revision),
        )
        saved = connection.execute(
            "select * from local_reel_drafts where id=?", (draft_id,)
        ).fetchone()
        return ReelDraft.from_row(saved)

    def save_uploaded_image(
        self, draft_id: str, revision: int, source: BinaryIO, filename: str,
    ) -> ReelDraft:
        relative, digest = self.save_artifact(draft_id, "image", source, filename)
        stored_path = self.root / relative
        try:
            with Image.open(stored_path) as image:
                expected_suffixes = {
                    "PNG": {".png"}, "JPEG": {".jpg", ".jpeg"}, "WEBP": {".webp"},
                }
                if (image.format not in expected_suffixes
                        or stored_path.suffix.casefold() not in expected_suffixes[image.format]
                        or image.width < 64 or image.height < 64
                        or image.width * image.height > 50_000_000):
                    raise UploadError(
                        "Das Bildformat oder die Bildabmessungen werden nicht unterstützt.", 415,
                    )
                image.verify()
        except UploadError:
            stored_path.unlink(missing_ok=True)
            raise
        except (UnidentifiedImageError, OSError, SyntaxError):
            stored_path.unlink(missing_ok=True)
            raise UploadError("Die hochgeladene Bilddatei ist beschädigt.", 415) from None
        with closing(self.connection()) as connection, connection:
            self.schema(connection)
            connection.execute("begin immediate")
            draft = connection.execute(
                "select * from local_reel_drafts where id=?", (draft_id,)
            ).fetchone()
            if draft is None:
                raise UploadError("Der Reel-Entwurf wurde nicht gefunden.", 404)
            if draft["revision"] != revision:
                raise UploadError("Der Reel-Entwurf wurde zwischenzeitlich geändert. Bitte neu laden.", 409)
            self._validate_stored_image(draft, relative, digest)
            connection.execute(
                """update local_reel_drafts set uploaded_image_path=?,uploaded_image_sha256=?,
                error=null,
                revision=revision+1,updated_at=? where id=? and revision=?""",
                (relative, digest, time.time(), draft_id, revision),
            )
            saved = connection.execute(
                "select * from local_reel_drafts where id=?", (draft_id,)
            ).fetchone()
        return ReelDraft.from_row(saved)

    def _validate_stored_image(self, draft, relative: str, digest: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise UploadError("Der gespeicherte Bildhash ist ungültig.", 503)
        path = (self.root / relative).resolve()
        expected_root = (self.root / "reels" / draft["book_id"] / draft["id"] / "image").resolve()
        try:
            path.relative_to(expected_root)
        except ValueError:
            raise UploadError("Der gespeicherte Bildpfad ist ungültig.", 503) from None
        if not path.is_file():
            raise UploadError("Die gespeicherte Bilddatei fehlt.", 503)
        with path.open("rb") as stored:
            if hashlib.file_digest(stored, "sha256").hexdigest() != digest:
                raise UploadError("Die gespeicherte Bilddatei ist beschädigt.", 503)

    def delete_book_assets(self, book_id: str) -> None:
        """Remove this feature's rows and exact book-scoped directories.

        Files are staged before the database commit and restored if that commit
        fails.  The helper can be called from the main book-deletion workflow;
        it never touches the original manuscript or another book's media.
        """
        book_id = self._normalize_book_id(book_id)
        audio = (self.root / "audio" / book_id).resolve()
        reels = (self.root / "reels" / book_id).resolve()
        if audio.parent != (self.root / "audio").resolve() or reels.parent != (self.root / "reels").resolve():
            raise UploadError("Die lokalen Reel-Pfade sind ungültig.", 503)
        staging = self.root / "pending" / f"delete-reels-{book_id}-{uuid4().hex}"
        moved: list[tuple[Path, Path]] = []
        committed = False
        try:
            for source, name in ((audio, "audio"), (reels, "reels")):
                if source.exists():
                    staging.mkdir(parents=True, exist_ok=True)
                    target = staging / name
                    os.replace(source, target)
                    moved.append((source, target))
            if self.uploads.db_path.is_file():
                with closing(self.connection()) as connection, connection:
                    self.schema(connection)
                    connection.execute("begin immediate")
                    connection.execute(
                        "delete from local_reel_jobs where draft_id in "
                        "(select id from local_reel_drafts where book_id=?)", (book_id,)
                    )
                    connection.execute("delete from local_reel_drafts where book_id=?", (book_id,))
                    connection.execute(
                        "delete from local_audio_cues where track_id in "
                        "(select id from local_audio_tracks where book_id=?)", (book_id,)
                    )
                    connection.execute("delete from local_audio_tracks where book_id=?", (book_id,))
            committed = True
        finally:
            if not committed:
                for original, temporary in reversed(moved):
                    if temporary.exists() and not original.exists():
                        original.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(temporary, original)
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=committed)

    def update_draft(self, draft_id: str, revision: int, **changes) -> ReelDraft:
        """Optimistically update editable fields and mark dependencies stale."""
        character_ids = changes.pop("character_ids", None)
        if character_ids is not None:
            try:
                normalized_ids = tuple(dict.fromkeys(str(UUID(str(value))) for value in character_ids))
            except (TypeError, ValueError):
                raise UploadError("Die Charakterauswahl ist ungültig.") from None
            if len(normalized_ids) > 4:
                raise UploadError("Pro Szene können höchstens vier Charaktere ausgewählt werden.")
            changes["character_ids_json"] = json.dumps(normalized_ids)
        allowed = {
            "duration_ms", "audio_track_id", "audio_cue_id", "audio_start_ms",
            "caption_addition", "final_caption", "image_prompt", "video_prompt",
            "character_ids_json",
        }
        if not changes or set(changes) - allowed:
            raise ValueError("Unbekannte oder fehlende Reel-Felder.")
        for field in ("caption_addition", "final_caption", "image_prompt", "video_prompt"):
            if field in changes:
                value = str(changes[field]).strip()
                limit = 2_200 if field == "final_caption" else 8_000
                if len(value) > limit:
                    raise UploadError("Der gespeicherte Reel-Text ist zu lang.")
                changes[field] = value
        if "duration_ms" in changes and not 3_000 <= int(changes["duration_ms"]) <= 60_000:
            raise UploadError("Die Reel-Länge muss zwischen 3 und 60 Sekunden liegen.")

        with closing(self.connection()) as connection, connection:
            self.schema(connection)
            connection.execute("begin immediate")
            current = connection.execute(
                "select * from local_reel_drafts where id=?", (draft_id,)
            ).fetchone()
            if current is None:
                raise UploadError("Der Reel-Entwurf wurde nicht gefunden.", 404)
            if current["revision"] != revision:
                raise UploadError("Der Reel-Entwurf wurde zwischenzeitlich geändert. Bitte neu laden.", 409)

            if "character_ids_json" in changes:
                selected = json.loads(changes["character_ids_json"])
                if selected:
                    if connection.execute(
                        "select 1 from sqlite_master where type='table' and name='local_book_characters'"
                    ).fetchone() is None:
                        raise UploadError("Die ausgewählten Charaktere wurden nicht gefunden.")
                    placeholders = ",".join("?" for _ in selected)
                    count = connection.execute(
                        f"select count(*) from local_book_characters where book_id=? and id in ({placeholders})",
                        (current["book_id"], *selected),
                    ).fetchone()[0]
                    if count != len(selected):
                        raise UploadError("Mindestens ein ausgewählter Charakter gehört nicht zu diesem Buch.")

            track_id = changes.get("audio_track_id", current["audio_track_id"])
            cue_id = changes.get("audio_cue_id", current["audio_cue_id"])
            start_ms = changes.get("audio_start_ms", current["audio_start_ms"])
            duration_ms = int(changes.get("duration_ms", current["duration_ms"]))
            if track_id is not None:
                track = connection.execute(
                    "select book_id,duration_ms from local_audio_tracks where id=?", (track_id,)
                ).fetchone()
                if track is None or track["book_id"] != current["book_id"]:
                    raise UploadError("Die Audiodatei gehört nicht zu diesem Buch.")
                if start_ms is None or start_ms < 0 or start_ms + duration_ms > track["duration_ms"]:
                    raise UploadError("Der gewählte Reel-Ausschnitt liegt außerhalb der Audiodatei.")
                if cue_id is not None:
                    cue = connection.execute(
                        "select track_id from local_audio_cues where id=?", (cue_id,)
                    ).fetchone()
                    if cue is None or cue["track_id"] != track_id:
                        raise UploadError("Der Audioausschnitt gehört nicht zur ausgewählten Datei.")
            elif any(value is not None for value in (cue_id, start_ms)):
                raise UploadError("Bitte zuerst eine Audiodatei auswählen.")

            image_stale = bool(current["image_stale"])
            video_stale = bool(current["video_stale"])
            generated_inputs_changed = (
                "image_prompt" in changes and changes["image_prompt"] != current["image_prompt"]
            ) or (
                "character_ids_json" in changes
                and changes["character_ids_json"] != current["character_ids_json"]
            )
            if generated_inputs_changed:
                changes.update(
                    scene_image_path=None, scene_image_sha256=None,
                    optimized_image_path=None, optimized_image_sha256=None,
                )
                if current["selected_image_source"] != "upload":
                    changes.update(selected_image_path=None, selected_image_sha256=None)
                    image_stale = True
                else:
                    image_stale = False
                video_stale = True
            if ("character_ids_json" in changes
                    and changes["character_ids_json"] != current["character_ids_json"]):
                changes["character_snapshot_json"] = "[]"
            if any(
                key in changes and changes[key] != current[key]
                for key in ("duration_ms", "audio_track_id", "audio_cue_id", "audio_start_ms", "video_prompt")
            ):
                video_stale = True
            assignments = [f"{key}=?" for key in changes]
            values = list(changes.values())
            assignments += ["revision=revision+1", "image_stale=?", "video_stale=?",
                            "state='editing'", "error=null", "updated_at=?"]
            values += [int(image_stale), int(video_stale), time.time(), draft_id, revision]
            changed = connection.execute(
                f"update local_reel_drafts set {','.join(assignments)} where id=? and revision=?",
                values,
            ).rowcount
            if changed != 1:
                raise UploadError("Der Reel-Entwurf wurde zwischenzeitlich geändert. Bitte neu laden.", 409)
            row = connection.execute(
                "select * from local_reel_drafts where id=?", (draft_id,)
            ).fetchone()
        return ReelDraft.from_row(row)

    def mark_stocked(self, draft_id: str, revision: int, remote_video_path: str) -> ReelDraft:
        """Record a confirmed idempotent Supabase transfer without changing reviewed content."""
        if not remote_video_path or len(remote_video_path) > 1_000:
            raise UploadError("Der Supabase-Medienpfad ist ungültig.")
        with closing(self.connection()) as connection, connection:
            self.schema(connection)
            connection.execute("begin immediate")
            row = connection.execute(
                "select * from local_reel_drafts where id=?", (draft_id,)
            ).fetchone()
            if row is None:
                raise UploadError("Der Reel-Entwurf wurde nicht gefunden.", 404)
            if row["revision"] != revision:
                raise UploadError("Der Reel-Entwurf wurde zwischenzeitlich geändert. Bitte neu laden.", 409)
            if row["state"] not in {"ready", "stocked"} or row["video_stale"] or not row["selected_video_path"]:
                raise UploadError("Bitte das Reel samt Posting-Text zuerst fertigstellen.", 409)
            connection.execute(
                """update local_reel_drafts set state='stocked',remote_video_path=?,error=null,
                revision=revision+1,updated_at=? where id=? and revision=?""",
                (remote_video_path, time.time(), draft_id, revision),
            )
            saved = connection.execute(
                "select * from local_reel_drafts where id=?", (draft_id,)
            ).fetchone()
        return ReelDraft.from_row(saved)

    def save_artifact(
        self, draft_id: str, kind: ArtifactKind, source: BinaryIO, filename: str
    ) -> tuple[str, str]:
        """Store an immutable generated result and return relative path and digest."""
        suffix = Path(filename).suffix.lower()
        valid = {
            "image": {".png", ".jpg", ".jpeg", ".webp"},
            "cover": {".png", ".jpg", ".jpeg", ".webp"},
            "video": {".mp4"},
        }
        if suffix not in valid[kind]:
            raise UploadError("Das generierte Medienformat wird nicht unterstützt.", 415)
        draft = self.get_draft(draft_id)
        if draft is None:
            raise UploadError("Der Reel-Entwurf wurde nicht gefunden.", 404)
        temp, digest, _ = self._stream_to_temp(source, self.root / "pending", self.max_artifact_bytes)
        try:
            with temp.open("rb") as artifact:
                head = artifact.read(32)
            if kind in {"image", "cover"} and not (
                head.startswith(b"\x89PNG\r\n\x1a\n")
                or head.startswith(b"\xff\xd8")
                or head.startswith(b"RIFF") and head[8:12] == b"WEBP"
            ):
                raise UploadError("Die erzeugte Bilddatei ist ungültig.", 415)
            if kind == "video" and b"ftyp" not in head[4:16]:
                raise UploadError("Die erzeugte MP4-Datei ist ungültig.", 415)
            relative = f"reels/{draft.book_id}/{draft.id}/{kind}/{digest}{suffix}"
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                temp.unlink()
            else:
                os.replace(temp, target)
            return relative, digest
        finally:
            temp.unlink(missing_ok=True)


class ReelJobStore:
    """A durable leased queue.  Late workers are fenced by token and revision."""

    def __init__(self, reels: ReelStore):
        self.reels = reels

    def connection(self):
        return self.reels.connection()

    def enqueue(self, draft_id: str, kind: JobKind, payload: dict | None = None) -> ReelJob:
        with closing(self.connection()) as connection, connection:
            ReelStore.schema(connection)
            connection.execute("begin immediate")
            return self._enqueue(connection, draft_id, kind, payload)

    def _enqueue(
        self, connection: sqlite3.Connection, draft_id: str, kind: JobKind,
        payload: dict | None = None,
    ) -> ReelJob:
        """Enqueue in a caller-owned transaction, without committing other state changes."""
        if kind not in {"prompt", "image", "video", "upload"}:
            raise ValueError("Unbekannter Reel-Job.")
        payload = payload or {}
        if kind == "image" and payload.get("operation", "scene") not in {"scene", "optimize"}:
            raise ValueError("Unbekannter Bildschritt.")
        try:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            raise ValueError("Der Reel-Job enthält nicht serialisierbare Daten.") from None
        if len(encoded.encode()) > 64 * 1024:
            raise ValueError("Der Reel-Job ist zu groß.")
        draft = connection.execute(
            "select * from local_reel_drafts where id=?", (draft_id,)
        ).fetchone()
        if draft is None:
            raise UploadError("Der Reel-Entwurf wurde nicht gefunden.", 404)
        if kind == "image" and not draft["image_prompt"]:
            raise UploadError("Bitte zuerst einen Bildprompt erzeugen oder eingeben.", 409)
        if (kind == "image" and payload.get("operation") == "optimize"
                and (not draft["scene_image_path"] or draft["image_stale"])):
            raise UploadError("Bitte zuerst ein aktuelles Szenenbild erzeugen.", 409)
        if kind == "video" and (
            not draft["selected_image_path"] or draft["image_stale"]
            or not draft["video_prompt"] or not draft["audio_track_id"]
            or draft["audio_start_ms"] is None
        ):
            raise UploadError(
                "Bitte zuerst Bild, Audiodatei, Audioausschnitt und Videoprompt festlegen.", 409
            )
        if kind == "upload" and draft["state"] != "ready":
            raise UploadError("Bitte das Reel samt Posting-Text zuerst fertigstellen.", 409)
        if kind == "image" and payload.get("operation") == "optimize" and "character_ids" in payload:
            try:
                selected = [str(UUID(str(value))) for value in payload["character_ids"]]
            except (TypeError, ValueError):
                raise UploadError("Bitte gültige Charakterreferenzen auswählen.") from None
            if not 1 <= len(selected) <= 4 or len(set(selected)) != len(selected):
                raise UploadError("Bitte ein bis vier unterschiedliche Charakterreferenzen auswählen.")
            if not connection.execute(
                "select 1 from sqlite_master where type='table' and name='local_book_characters'"
            ).fetchone():
                raise UploadError("Die ausgewählten Charakterreferenzen fehlen.")
            placeholders = ",".join("?" for _ in selected)
            count = connection.execute(
                f"""select count(*) from local_book_characters where book_id=?
                and id in ({placeholders}) and reference_image_path is not null
                and reference_image_sha256 is not null""", (draft["book_id"], *selected),
            ).fetchone()[0]
            if count != len(selected):
                raise UploadError("Bitte Referenzbilder aus diesem Buch auswählen.")
            if tuple(selected) != tuple(json.loads(draft["character_ids_json"])):
                if connection.execute(
                    """select 1 from local_reel_jobs where draft_id=?
                    and state in ('queued','running') limit 1""", (draft_id,),
                ).fetchone():
                    raise UploadError("Für dieses Kapitel läuft noch ein Verarbeitungsschritt.", 409)
                # This is an explicit identity edit of the existing scene, not a new
                # scene prompt. Keep that scene usable and fence the new job revision.
                connection.execute(
                    """update local_reel_drafts set character_ids_json=?,revision=revision+1,
                    updated_at=? where id=?""", (json.dumps(selected), time.time(), draft_id),
                )
                draft = connection.execute(
                    "select * from local_reel_drafts where id=?", (draft_id,),
                ).fetchone()
        active = connection.execute(
            """select * from local_reel_jobs where draft_id=? and kind=?
            and state in ('queued','running')""", (draft_id, kind)
        ).fetchone()
        if active:
            return self._job(active)
        job_id, now = str(uuid4()), time.time()
        connection.execute(
            """insert into local_reel_jobs
            (id,draft_id,kind,input_revision,state,payload_json,created_at,updated_at)
            values (?,?,?,?,'queued',?,?,?)""",
            (job_id, draft_id, kind, draft["revision"], encoded, now, now),
        )
        row = connection.execute("select * from local_reel_jobs where id=?", (job_id,)).fetchone()
        return self._job(row)

    @staticmethod
    def _job(row: sqlite3.Row) -> ReelJob:
        return ReelJob(
            id=row["id"], draft_id=row["draft_id"], kind=row["kind"],
            input_revision=row["input_revision"], state=row["state"],
            payload=json.loads(row["payload_json"]), attempts=row["attempts"], token=row["token"],
        )

    def claim(self, *, kinds: set[str] | None = None, now: float | None = None) -> ReelJob | None:
        if not self.reels.uploads.db_path.is_file():
            return None
        now = time.time() if now is None else now
        with closing(self.connection()) as connection, connection:
            ReelStore.schema(connection)
            connection.execute("begin immediate")
            expired = connection.execute(
                "select id,draft_id,attempts from local_reel_jobs where state='running' and lease_until<=?",
                (now,),
            ).fetchall()
            for row in expired:
                failed = row["attempts"] >= REEL_MAX_ATTEMPTS
                connection.execute(
                    """update local_reel_jobs set state=?,token=null,lease_until=null,error=?,updated_at=?
                    where id=?""",
                    ("failed" if failed else "queued",
                     "Die Verarbeitung wurde mehrfach unterbrochen." if failed else None, now, row["id"]),
                )
                if failed:
                    connection.execute(
                        """update local_reel_drafts set state='failed',error=?,updated_at=?
                        where id=? and revision=(select input_revision from local_reel_jobs where id=?)""",
                        ("Die Verarbeitung wurde mehrfach unterbrochen.", now, row["draft_id"], row["id"]),
                    )
            params: list[object] = []
            clause = ""
            if kinds is not None:
                valid = sorted(set(kinds) & {"prompt", "image", "video", "upload"})
                if not valid:
                    return None
                clause = f" and kind in ({','.join('?' for _ in valid)})"
                params.extend(valid)
            row = connection.execute(
                f"select * from local_reel_jobs where state='queued'{clause} order by created_at,id limit 1",
                params,
            ).fetchone()
            if row is None:
                return None
            token = str(uuid4())
            connection.execute(
                """update local_reel_jobs set state='running',attempts=attempts+1,token=?,
                lease_until=?,updated_at=? where id=? and state='queued'""",
                (token, now + REEL_LEASE_SECONDS, now, row["id"]),
            )
            connection.execute(
                "update local_reel_drafts set state=?,error=null,updated_at=? where id=?",
                ("uploading" if row["kind"] == "upload" else "generating", now, row["draft_id"]),
            )
            claimed = connection.execute(
                "select * from local_reel_jobs where id=?", (row["id"],)
            ).fetchone()
        return self._job(claimed)

    @staticmethod
    def _owned(connection: sqlite3.Connection, job: ReelJob, now: float) -> bool:
        return connection.execute(
            """select 1 from local_reel_jobs where id=? and token=? and state='running'
            and lease_until>?""", (job.id, job.token, now)
        ).fetchone() is not None

    def heartbeat(self, job: ReelJob, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with closing(self.connection()) as connection, connection:
            if not self._owned(connection, job, now):
                return False
            connection.execute(
                "update local_reel_jobs set lease_until=?,updated_at=? where id=?",
                (now + REEL_LEASE_SECONDS, now, job.id),
            )
        return True

    def finish(
        self,
        job: ReelJob,
        *,
        result: dict | None = None,
        error: str | None = None,
        now: float | None = None,
    ) -> bool:
        """Finish an owned job; revision-mismatched output is retained as stale history."""
        if result is not None and error is not None:
            raise ValueError("Ein Job kann nicht gleichzeitig Ergebnis und Fehler haben.")
        now = time.time() if now is None else now
        result = result or {}
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)
        safe_error = (error or "")[:1_000] or None
        with closing(self.connection()) as connection, connection:
            ReelStore.schema(connection)
            connection.execute("begin immediate")
            if not self._owned(connection, job, now):
                return False
            draft = connection.execute(
                "select * from local_reel_drafts where id=?", (job.draft_id,)
            ).fetchone()
            if draft is None:
                connection.execute(
                    "update local_reel_jobs set state='cancelled',token=null,lease_until=null,updated_at=? where id=?",
                    (now, job.id),
                )
                return True
            if error is not None:
                connection.execute(
                    """update local_reel_jobs set state='failed',result_json=null,error=?,token=null,
                    lease_until=null,updated_at=? where id=?""", (safe_error, now, job.id)
                )
                if draft["revision"] == job.input_revision:
                    connection.execute(
                        "update local_reel_drafts set state='failed',error=?,updated_at=? where id=?",
                        (safe_error, now, job.draft_id),
                    )
                return True
            if draft["revision"] != job.input_revision:
                connection.execute(
                    """update local_reel_jobs set state='stale',result_json=?,error=null,token=null,
                    lease_until=null,updated_at=? where id=?""", (encoded, now, job.id)
                )
                return True

            updates: dict[str, object] = {}
            if job.kind == "prompt":
                for key in ("caption_addition", "final_caption", "image_prompt", "video_prompt"):
                    if key in result:
                        value = str(result[key]).strip()
                        limit = 2_200 if key == "final_caption" else 8_000
                        if len(value) > limit:
                            raise ValueError("Das Prompt-Ergebnis ist zu lang.")
                        updates[key] = value
                if "image_prompt" in updates and updates["image_prompt"] != draft["image_prompt"]:
                    updates["image_stale"] = 1
                    updates["video_stale"] = 1
                if "video_prompt" in updates and updates["video_prompt"] != draft["video_prompt"]:
                    updates["video_stale"] = 1
            elif job.kind in {"image", "video"}:
                relative, digest = result.get("path"), result.get("sha256")
                self._validate_artifact_result(draft, job.kind, relative, digest)
                if job.kind == "image":
                    candidate = result.get("candidate", "scene")
                    if candidate not in {"scene", "optimized"}:
                        raise ValueError("Der Bildjob hat eine ungültige Bildvariante geliefert.")
                    if candidate == "scene":
                        updates.update(
                            scene_image_path=relative, scene_image_sha256=digest,
                            optimized_image_path=None, optimized_image_sha256=None,
                            selected_image_source="scene", selected_image_path=relative,
                            selected_image_sha256=digest, image_stale=0, video_stale=1,
                        )
                    else:
                        if not draft["scene_image_path"]:
                            raise ValueError("Dem Optimierungsjob fehlt das Szenenbild.")
                        updates.update(
                            optimized_image_path=relative, optimized_image_sha256=digest,
                        )
                    snapshot = result.get("character_snapshot", [])
                    if not isinstance(snapshot, list) or len(snapshot) > 4:
                        raise ValueError("Der Bildjob hat ungültige Charakterdaten geliefert.")
                    updates["character_snapshot_json"] = json.dumps(snapshot, ensure_ascii=False)
                else:
                    updates.update(selected_video_path=relative, selected_video_sha256=digest,
                                   video_stale=0)
            elif job.kind == "upload":
                remote_video = result.get("remote_video_path")
                remote_cover = result.get("remote_cover_path")
                if not isinstance(remote_video, str) or not remote_video or len(remote_video) > 1_000:
                    raise ValueError("Der Upload hat keinen gültigen Remotepfad geliefert.")
                updates.update(remote_video_path=remote_video,
                               remote_cover_path=remote_cover if isinstance(remote_cover, str) else None,
                               state="stocked")

            updates.setdefault("state", self._resulting_state(draft, updates, job.kind))
            updates.update(error=None, updated_at=now)
            assignments = [f"{key}=?" for key in updates] + ["revision=revision+1"]
            connection.execute(
                f"update local_reel_drafts set {','.join(assignments)} where id=?",
                [*updates.values(), job.draft_id],
            )
            connection.execute(
                """update local_reel_jobs set state='done',result_json=?,error=null,token=null,
                lease_until=null,updated_at=? where id=?""", (encoded, now, job.id)
            )
        return True

    def _validate_artifact_result(self, draft, kind, relative, digest) -> None:
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise ValueError("Der Medienjob hat kein gültiges Ergebnis geliefert.")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Der Medienhash ist ungültig.")
        path = (self.reels.root / relative).resolve()
        reel_root = (self.reels.root / "reels" / draft["book_id"] / draft["id"]).resolve()
        try:
            path.relative_to(reel_root)
        except ValueError:
            raise ValueError("Der Medienpfad liegt außerhalb des Reel-Entwurfs.") from None
        if not path.is_file():
            raise ValueError("Die erzeugte Mediendatei fehlt.")
        with path.open("rb") as source:
            actual = hashlib.file_digest(source, "sha256").hexdigest()
        if actual != digest or f"/{kind}/" not in "/" + Path(relative).as_posix():
            raise ValueError("Die erzeugte Mediendatei stimmt nicht mit dem Ergebnis überein.")

    @staticmethod
    def _resulting_state(draft, updates: dict, kind: str) -> str:
        if kind == "upload":
            return "stocked"
        image = updates.get("selected_image_path", draft["selected_image_path"])
        video = updates.get("selected_video_path", draft["selected_video_path"])
        image_stale = bool(updates.get("image_stale", draft["image_stale"]))
        video_stale = bool(updates.get("video_stale", draft["video_stale"]))
        caption = updates.get("final_caption", draft["final_caption"])
        return (
            "ready"
            if image and video and caption and draft["audio_track_id"]
            and draft["audio_start_ms"] is not None and not image_stale and not video_stale
            else "editing"
        )

    def status(self, draft_id: str) -> list[dict]:
        if not self.reels.uploads.db_path.is_file():
            return []
        with closing(self.reels.uploads._read_connection()) as connection:
            if not connection.execute(
                "select 1 from sqlite_master where type='table' and name='local_reel_jobs'"
            ).fetchone():
                return []
            rows = connection.execute(
                """select id,kind,state,attempts,error,created_at,updated_at,result_json
                from local_reel_jobs where draft_id=? order by created_at desc,id desc""",
                (draft_id,),
            ).fetchall()
        statuses = []
        for row in rows:
            item = dict(row)
            result_json = item.pop("result_json")
            if item["kind"] == "image" and result_json:
                try:
                    result = json.loads(result_json)
                    prompt = result.get("effective_image_prompt") if isinstance(result, dict) else None
                    if isinstance(prompt, str):
                        item["effective_image_prompt"] = prompt
                except (TypeError, ValueError):
                    pass
            statuses.append(item)
        return statuses
