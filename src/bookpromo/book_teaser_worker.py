"""Durable background rendering for long assembled book teasers."""

from __future__ import annotations

from contextlib import closing
import sqlite3
import threading
import time
from uuid import uuid4

from .book_teasers import BookTeaserStore, TeaserRenderSegment, render_book_teaser
from .reel_generation import ReelGenerationError
from .reels import ReelStore
from .uploads import UploadError


LEASE_SECONDS = 30
MAX_ATTEMPTS = 3


class BookTeaserJobStore:
    def __init__(self, uploads):
        self.uploads = uploads

    def connection(self):
        connection = sqlite3.connect(self.uploads.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def schema(connection):
        connection.executescript("""
            create table if not exists local_book_teaser_jobs (
                id text primary key, project_id text not null, book_id text not null,
                input_revision integer not null,
                state text not null check(state in ('queued','running','done','failed','stale')),
                stage text not null, progress_ms integer not null default 0,
                total_ms integer not null, attempts integer not null default 0,
                token text, lease_until real, error text,
                created_at real not null, updated_at real not null
            );
            create unique index if not exists local_book_teaser_jobs_active
                on local_book_teaser_jobs(project_id)
                where state in ('queued','running');
        """)

    def enqueue(self, project) -> str:
        included = [item for item in project.segments if item.included]
        total_ms = max(
            0,
            sum(item.duration_ms for item in included)
            - project.transition_ms * max(0, len(included) - 1),
        )
        if not included or total_ms <= 0:
            raise UploadError("Der Book-Teaser enthält keine renderbaren Kapitel.")
        with closing(self.connection()) as connection, connection:
            self.schema(connection)
            connection.execute("begin immediate")
            active = connection.execute(
                """select id from local_book_teaser_jobs where project_id=?
                and state in ('queued','running')""", (project.id,),
            ).fetchone()
            if active:
                return active["id"]
            current = connection.execute(
                "select revision from local_book_teasers where id=?", (project.id,)
            ).fetchone()
            if current is None or current["revision"] != project.revision:
                raise UploadError("Der Book-Teaser-Schnitt wurde zwischenzeitlich geändert.", 409)
            job_id, now = str(uuid4()), time.time()
            connection.execute(
                """insert into local_book_teaser_jobs
                (id,project_id,book_id,input_revision,state,stage,total_ms,created_at,updated_at)
                values (?,?,?,?,'queued','Wartet auf langen Export',?,?,?)""",
                (job_id, project.id, project.book_id, project.revision, total_ms, now, now),
            )
        return job_id

    def claim(self, *, now=None):
        if not self.uploads.db_path.is_file():
            return None
        now = time.time() if now is None else now
        with closing(self.connection()) as connection, connection:
            self.schema(connection)
            connection.execute("begin immediate")
            expired = connection.execute(
                """select id,attempts from local_book_teaser_jobs
                where state='running' and lease_until<=?""", (now,),
            ).fetchall()
            for row in expired:
                failed = row["attempts"] >= MAX_ATTEMPTS
                connection.execute(
                    """update local_book_teaser_jobs set state=?,stage=?,token=null,
                    lease_until=null,error=?,updated_at=? where id=?""",
                    ("failed" if failed else "queued",
                     "Export mehrfach unterbrochen" if failed else "Export wird fortgesetzt",
                     "Der lange Export wurde mehrfach unterbrochen." if failed else None,
                     now, row["id"]),
                )
            connection.execute(
                """update local_book_teaser_jobs set state='stale',stage='Schnitt wurde geändert',
                token=null,lease_until=null,updated_at=? where state='queued' and not exists (
                    select 1 from local_book_teasers p where p.id=project_id
                    and p.revision=input_revision
                )""", (now,),
            )
            if connection.execute(
                "select 1 from local_book_teaser_jobs where state='running'"
            ).fetchone():
                return None
            row = connection.execute(
                """select * from local_book_teaser_jobs where state='queued'
                order by created_at,id limit 1"""
            ).fetchone()
            if row is None:
                return None
            token = str(uuid4())
            connection.execute(
                """update local_book_teaser_jobs set state='running',stage='FFmpeg-Export läuft',
                attempts=attempts+1,token=?,lease_until=?,error=null,updated_at=? where id=?""",
                (token, now + LEASE_SECONDS, now, row["id"]),
            )
            return {**dict(row), "token": token}

    @staticmethod
    def _owned(connection, job, now):
        return connection.execute(
            """select 1 from local_book_teaser_jobs where id=? and token=?
            and state='running' and lease_until>?""", (job["id"], job["token"], now),
        ).fetchone() is not None

    def heartbeat(self, job):
        now = time.time()
        with closing(self.connection()) as connection, connection:
            if not self._owned(connection, job, now):
                return False
            connection.execute(
                "update local_book_teaser_jobs set lease_until=?,updated_at=? where id=?",
                (now + LEASE_SECONDS, now, job["id"]),
            )
        return True

    def progress(self, job, completed_ms, total_ms):
        now = time.time()
        with closing(self.connection()) as connection, connection:
            if not self._owned(connection, job, now):
                return False
            connection.execute(
                """update local_book_teaser_jobs set progress_ms=?,total_ms=?,
                lease_until=?,updated_at=? where id=?""",
                (max(0, int(completed_ms)), max(1, int(total_ms)),
                 now + LEASE_SECONDS, now, job["id"]),
            )
        return True

    def finish(self, job, error=None):
        now = time.time()
        with closing(self.connection()) as connection, connection:
            if not self._owned(connection, job, now):
                return False
            current = connection.execute(
                "select progress_ms,total_ms from local_book_teaser_jobs where id=?",
                (job["id"],),
            ).fetchone()
            connection.execute(
                """update local_book_teaser_jobs set state=?,stage=?,progress_ms=?,
                token=null,lease_until=null,error=?,updated_at=? where id=?""",
                ("failed" if error else "done",
                 "Export fehlgeschlagen" if error else "Langer Book-Teaser fertig",
                 current["progress_ms"] if error else current["total_ms"],
                 (str(error)[:2000] if error else None), now, job["id"]),
            )
        return True

    def latest(self, book_id: str):
        if not self.uploads.db_path.is_file():
            return None
        with closing(self.uploads._read_connection()) as connection:
            if not connection.execute(
                "select 1 from sqlite_master where type='table' and name='local_book_teaser_jobs'"
            ).fetchone():
                return None
            row = connection.execute(
                """select * from local_book_teaser_jobs where book_id=?
                order by updated_at desc,rowid desc limit 1""", (book_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["active"] = result["state"] in {"queued", "running"}
        result.pop("token", None)
        return result


def run_book_teaser_once(uploads, settings, stop=None) -> bool:
    jobs = BookTeaserJobStore(uploads)
    job = jobs.claim()
    if job is None:
        return False
    teasers = BookTeaserStore(uploads)
    reels = ReelStore(
        uploads,
        max_audio_bytes=settings.reel_max_audio_mb * 1024 * 1024,
        max_artifact_bytes=settings.reel_max_video_mb * 1024 * 1024,
    )
    done, lost = threading.Event(), threading.Event()

    def heartbeat():
        while not done.wait(5):
            try:
                if (stop and stop.is_set()) or not jobs.heartbeat(job):
                    lost.set()
                    return
            except (OSError, sqlite3.Error):
                lost.set()
                return

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    project = None
    try:
        project = teasers.get(job["book_id"])
        if project is None or project.id != job["project_id"] or project.revision != job["input_revision"]:
            raise UploadError("Der Book-Teaser-Schnitt wurde vor dem Export geändert.", 409)
        render_segments = []
        for segment in project.segments:
            if not segment.included:
                continue
            draft = reels.get_draft(segment.draft_id)
            if (
                draft is None or draft.book_id != project.book_id or draft.video_stale
                or draft.selected_video_sha256 != segment.video_sha256
            ):
                raise UploadError(
                    f"Das Reel für Kapitel {segment.position} wurde geändert.", 409,
                )
            path = reels.artifact_path(draft, "video")
            if path is None:
                raise UploadError(f"Das Reel für Kapitel {segment.position} fehlt.", 409)
            render_segments.append(TeaserRenderSegment(
                path, segment.start_ms / 1000, segment.duration_ms / 1000,
                segment.focus_x / 100, segment.focus_y / 100,
            ))
        track = next(
            (item for item in reels.list_audio(project.book_id)
             if item.id == project.audio_track_id), None,
        )
        if track is None:
            raise UploadError("Der ausgewählte Book-Teaser-Song fehlt.", 409)

        def report(completed_ms, total_ms):
            if lost.is_set() or not jobs.progress(job, completed_ms, total_ms):
                lost.set()

        output = teasers.render_path(project)
        duration_ms = render_book_teaser(
            render_segments, reels.audio_path(track), output,
            aspect=project.aspect, transition_ms=project.transition_ms, progress=report,
        )
        if lost.is_set():
            return True
        teasers.mark_rendered(project.id, project.revision, output, duration_ms)
        jobs.finish(job)
    except (UploadError, ReelGenerationError, OSError, ValueError) as exc:
        if not lost.is_set():
            if project is not None:
                teasers.mark_failed(project.id, project.revision, str(exc))
            jobs.finish(job, str(exc))
    except Exception:
        if not lost.is_set():
            message = "Der lange Book-Teaser konnte nicht erzeugt werden."
            if project is not None:
                teasers.mark_failed(project.id, project.revision, message)
            jobs.finish(job, message)
    finally:
        done.set()
        thread.join(timeout=3)
    return True
