"""Durable local queue with bounded recovery and fenced, atomic checkpoints."""

from contextlib import closing
import sqlite3
import time
from uuid import uuid4

from .extraction_store import ExtractionStore
from .uploads import LocalBook, LocalUploadStore, UploadError

LEASE_SECONDS = 30
MAX_ATTEMPTS = 3
LABELS = {"queued": "Wartet", "running": "Wird eingelesen", "done": "Text eingelesen",
          "needs_review": "Prüfung nötig", "failed": "Einlesen fehlgeschlagen"}


class JobStore:
    def __init__(self, uploads: LocalUploadStore):
        self.uploads = uploads

    def connection(self):
        connection = sqlite3.connect(self.uploads.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def schema(connection):
        connection.execute("""create table if not exists local_import_jobs (
            id text primary key, book_id text not null unique,
            state text not null check(state in ('queued','running','done','needs_review','failed')),
            stage text not null, completed integer not null default 0, total integer not null default 0,
            attempts integer not null default 0, token text, lease_until real,
            chapter_count integer, error text, created_at real not null, updated_at real not null
        )""")
        connection.execute("create index if not exists local_import_jobs_queue on local_import_jobs(state,created_at)")

    def enqueue(self, book: LocalBook, *, retry=False):
        with closing(self.connection()) as connection, connection:
            self.schema(connection)
            connection.execute("begin immediate")
            existing = connection.execute("select state from local_import_jobs where book_id=?", (book.id,)).fetchone()
            if existing:
                if retry and existing["state"] == "failed":
                    connection.execute("update local_import_jobs set state='queued',stage='Wartet auf Verarbeitung',completed=0,total=0,attempts=0,error=null,updated_at=? where book_id=?", (time.time(), book.id))
                return
            state = "queued"
            error, count = None, None
            try:
                record = ExtractionStore(self.uploads).get(book.id)
            except UploadError as exc:
                record, state, error = None, "failed", str(exc)
            if record:
                error = record.error
                state = "failed" if error else "needs_review" if record.result.needs_review else "done"
                count = len(record.result.chapters) if record.result else None
                if retry and error:
                    state, error = "queued", None
            now = time.time()
            connection.execute("insert into local_import_jobs (id,book_id,state,stage,chapter_count,error,created_at,updated_at) values (?,?,?,?,?,?,?,?)",
                (book.job_id, book.id, state, "Wartet auf Verarbeitung" if state == "queued" else LABELS[state], count, error, now, now))

    def reconcile(self):
        """Recover uploads committed just before a crash, and adopt old snapshots."""
        if not self.uploads.db_path.is_file(): return
        with closing(self.connection()) as connection, connection:
            if not connection.execute("select 1 from sqlite_master where name='local_books'").fetchone(): return
            self.schema(connection)
            rows = connection.execute("select b.* from local_books b left join local_import_jobs j on j.book_id=b.id where j.id is null limit 20").fetchall()
        for row in rows:
            self.enqueue(LocalBook.from_row(row))

    def claim(self, *, now=None):
        if not self.uploads.db_path.is_file(): return None
        now = time.time() if now is None else now
        with closing(self.connection()) as connection, connection:
            self.schema(connection)
            connection.execute("begin immediate")
            connection.execute("""update local_import_jobs set state=case when attempts>=? then 'failed' else 'queued' end,
                stage='Verarbeitung unterbrochen',token=null,lease_until=null,updated_at=?,
                error=case when attempts>=? then 'Der Import wurde mehrfach unterbrochen. Bitte erneut starten.' else null end
                where state='running' and lease_until<=?""", (MAX_ATTEMPTS, now, MAX_ATTEMPTS, now))
            # One book at a time, even with several worker processes.
            if connection.execute("select 1 from local_import_jobs where state='running'").fetchone(): return None
            if connection.execute("select 1 from sqlite_master where name='local_analysis_runs'").fetchone():
                if connection.execute("select 1 from local_analysis_runs where state='running' and lease_until>?", (now,)).fetchone(): return None
            row = connection.execute("select * from local_import_jobs where state='queued' order by created_at,id limit 1").fetchone()
            if row is None: return None
            token = str(uuid4())
            connection.execute("update local_import_jobs set state='running',stage='Datei prüfen',completed=0,total=0,attempts=attempts+1,token=?,lease_until=?,updated_at=? where id=?",
                (token, now + LEASE_SECONDS, now, row["id"]))
            return {"id": row["id"], "book_id": row["book_id"], "token": token}

    def heartbeat(self, job, *, now=None):
        now = time.time() if now is None else now
        with closing(self.connection()) as connection, connection:
            return connection.execute("update local_import_jobs set lease_until=?,updated_at=? where id=? and token=? and state='running' and lease_until>?",
                (now + LEASE_SECONDS, now, job["id"], job["token"], now)).rowcount == 1

    def progress(self, job, stage, completed=0, total=0):
        now = time.time()
        with closing(self.connection()) as connection, connection:
            return connection.execute("update local_import_jobs set stage=?,completed=?,total=?,updated_at=? where id=? and token=? and state='running' and lease_until>?",
                (stage, completed, total, now, job["id"], job["token"], now)).rowcount == 1

    def finish(self, job, result, error, *, now=None):
        now = time.time() if now is None else now
        with closing(self.connection()) as connection, connection:
            connection.execute("begin immediate")
            if not connection.execute("select 1 from local_import_jobs where id=? and token=? and state='running' and lease_until>?",
                                      (job["id"], job["token"], now)).fetchone(): return False
            ExtractionStore._table(connection)
            old = connection.execute("select revision,result_json from local_extractions where book_id=?", (job["book_id"],)).fetchone()
            # A successful snapshot (including manual edits) is already a checkpoint.
            if old and old["result_json"]:
                result, error = ExtractionStore._decode(old["result_json"]), None
            else:
                connection.execute("insert or replace into local_extractions values (?,?,?,?)",
                    (job["book_id"], old["revision"] + 1 if old else 1, result.model_dump_json() if result else None, error))
            state = "failed" if error else "needs_review" if result.needs_review else "done"
            connection.execute("update local_import_jobs set state=?,stage=?,completed=0,total=0,token=null,lease_until=null,chapter_count=?,error=?,updated_at=? where id=?",
                (state, LABELS[state], len(result.chapters) if result else None, error, now, job["id"]))
        return True

    def statuses(self, book_ids):
        if not book_ids or not self.uploads.db_path.is_file(): return {}
        with closing(self.uploads._read_connection()) as connection:
            if not connection.execute("select 1 from sqlite_master where name='local_import_jobs'").fetchone(): return {}
            rows = connection.execute(f"select book_id,state,stage,completed,total,attempts,chapter_count,error from local_import_jobs where book_id in ({','.join('?' for _ in book_ids)})", tuple(book_ids)).fetchall()
        return {row["book_id"]: {**dict(row), "label": LABELS[row["state"]], "active": row["state"] in {"queued", "running"}} for row in rows}
