"""Local, revision-bound AI runs and fenced checkpoints. No credentials stored."""

from contextlib import closing
import hashlib
import json
import sqlite3
import time
from uuid import uuid4

from pydantic import ValidationError

from .analysis import AnalysisError, AnalysisOptions, AnalysisResult, PROMPT_VERSION
from .extraction_store import ExtractionStore
from .jobs import LEASE_SECONDS, MAX_ATTEMPTS
from .uploads import UploadError

LABELS = {"queued": "KI-Analyse wartet", "running": "KI-Analyse läuft", "done": "KI-Analyse abgeschlossen",
          "empty": "Keine geeigneten Zitate", "failed": "KI-Analyse fehlgeschlagen", "stale": "Analyse veraltet"}


class AnalysisLeaseLost(Exception):
    pass


def endpoint_hash(settings):
    return hashlib.sha256(str(settings.openwebui_url).rstrip("/").encode()).hexdigest()


class AnalysisStore:
    def __init__(self, uploads):
        self.uploads = uploads

    def connection(self):
        connection = sqlite3.connect(self.uploads.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def schema(connection):
        connection.execute("""create table if not exists local_analysis_runs (
            id text primary key, book_id text not null, extraction_revision integer not null,
            fingerprint text not null, model_id text not null, endpoint_hash text not null,
            options_json text not null, prompt_version text not null, source_json text not null,
            state text not null check(state in ('queued','running','done','empty','failed','stale')),
            stage text not null, attempts integer not null default 0, calls_started integer not null default 0,
            token text, lease_until real, error text, result_json text, created_at real not null, updated_at real not null,
            unique(book_id,fingerprint)
        )""")
        connection.execute("create unique index if not exists local_analysis_active on local_analysis_runs(book_id) where state in ('queued','running')")
        connection.execute("""create table if not exists local_analysis_checkpoints (
            run_id text not null, step_key text not null, payload_json text not null,
            primary key(run_id,step_key)
        )""")

    def enqueue(self, book_id, settings, options: AnalysisOptions):
        if settings.missing_for("openwebui"):
            raise UploadError("Bitte zuerst die Open-WebUI-Verbindung und das Modell einrichten.", 409)
        with closing(self.connection()) as connection, connection:
            self.schema(connection)
            connection.execute("begin immediate")
            if not connection.execute("select 1 from sqlite_master where name='local_extractions'").fetchone():
                raise UploadError("Bitte zuerst Text und Kapitel einlesen.", 409)
            source = connection.execute("select revision,result_json from local_extractions where book_id=?", (book_id,)).fetchone()
            if source is None or not source["result_json"]: raise UploadError("Bitte zuerst Text und Kapitel einlesen.", 409)
            extraction = ExtractionStore._decode(source["result_json"])
            if extraction.needs_review:
                issues = [w.message for w in extraction.warnings if w.blocking or (w.code.startswith("chapter_") and not extraction.reviewed)]
                raise UploadError("Bitte zuerst die offenen Hinweise zur Textextraktion klären. " + " ".join(issues), 409)
            active = connection.execute("select id,options_json from local_analysis_runs where book_id=? and state in ('queued','running')", (book_id,)).fetchone()
            if active:
                if AnalysisOptions.model_validate_json(active["options_json"]).purpose != options.purpose:
                    raise UploadError("Für dieses Buch läuft bereits eine andere KI-Analyse. Bitte deren Abschluss abwarten.", 409)
                return active["id"]
            fingerprint_options = options.model_dump()
            if options.purpose == "full":
                fingerprint_options.pop("purpose")
                fingerprint_options.pop("profile_prompt_version")
            fingerprint = hashlib.sha256(json.dumps([source["revision"], settings.openwebui_model,
                endpoint_hash(settings), fingerprint_options, PROMPT_VERSION], sort_keys=True).encode()).hexdigest()
            old = connection.execute("select id,state from local_analysis_runs where book_id=? and fingerprint=?", (book_id, fingerprint)).fetchone()
            if old:
                connection.execute("update local_analysis_runs set updated_at=? where id=?", (time.time(), old["id"]))
                if old["state"] == "failed":
                    connection.execute("update local_analysis_runs set state='queued',stage='Wird fortgesetzt',attempts=0,error=null,updated_at=? where id=?", (time.time(), old["id"]))
                    self.reuse_context(connection, old["id"])
                return old["id"]
            run_id, now = str(uuid4()), time.time()
            connection.execute("""insert into local_analysis_runs
                (id,book_id,extraction_revision,fingerprint,model_id,endpoint_hash,options_json,prompt_version,source_json,state,stage,created_at,updated_at)
                values (?,?,?,?,?,?,?,?,?,'queued','Wartet auf KI-Verarbeitung',?,?)""",
                (run_id, book_id, source["revision"], fingerprint, settings.openwebui_model, endpoint_hash(settings),
                 options.model_dump_json(), PROMPT_VERSION, source["result_json"], now, now))
            self.reuse_context(connection, run_id)
            return run_id

    @staticmethod
    def reuse_context(connection, run_id):
        # Only context steps share a contract across profile and quote analysis.
        # Field and quote outputs remain scoped to their own run and prompts.
        connection.execute("""insert or ignore into local_analysis_checkpoints (run_id,step_key,payload_json)
            select target.id,c.step_key,c.payload_json from local_analysis_checkpoints c
            join local_analysis_runs r on r.id=c.run_id
            join local_analysis_runs target on target.id=?
            where r.book_id=target.book_id and r.id<>target.id and r.extraction_revision=target.extraction_revision
            and r.model_id=target.model_id and r.endpoint_hash=target.endpoint_hash and r.prompt_version=target.prompt_version
            and r.state in ('done','empty','failed')
            and json_extract(r.options_json,'$.chunk_chars')=json_extract(target.options_json,'$.chunk_chars')
            and (c.step_key like 'summary:%' or c.step_key like 'reduce:%')
            order by r.updated_at desc,r.rowid desc""",
            (run_id,))

    def claim(self, *, now=None):
        if not self.uploads.db_path.is_file(): return None
        now = time.time() if now is None else now
        with closing(self.connection()) as connection, connection:
            if not connection.execute("select 1 from sqlite_master where name='local_analysis_runs'").fetchone(): return None
            connection.execute("begin immediate")
            connection.execute("""update local_analysis_runs set state='stale',token=null,lease_until=null,stage='Textstand wurde geändert'
                where state in ('queued','running') and not exists
                (select 1 from local_extractions e where e.book_id=local_analysis_runs.book_id and e.revision=local_analysis_runs.extraction_revision)""")
            connection.execute("""update local_analysis_runs set state=case when attempts>=? then 'failed' else 'queued' end,
                token=null,lease_until=null,stage='Unterbrochene Analyse',error=case when attempts>=? then 'Die Analyse wurde mehrfach unterbrochen. Bitte manuell fortsetzen.' else null end
                where state='running' and lease_until<=?""", (MAX_ATTEMPTS, MAX_ATTEMPTS, now))
            if connection.execute("select 1 from local_analysis_runs where state='running'").fetchone(): return None
            if connection.execute("select 1 from sqlite_master where name='local_import_jobs'").fetchone():
                if connection.execute("select 1 from local_import_jobs where state='running' and lease_until>?", (now,)).fetchone(): return None
            row = connection.execute("select * from local_analysis_runs where state='queued' order by created_at,id limit 1").fetchone()
            if row is None: return None
            token = str(uuid4())
            connection.execute("update local_analysis_runs set state='running',stage='KI-Analyse vorbereiten',attempts=attempts+1,token=?,lease_until=?,updated_at=? where id=?",
                (token, now+LEASE_SECONDS, now, row["id"]))
            self.reuse_context(connection, row["id"])
            return {**dict(row), "token": token}

    @staticmethod
    def owned(connection, job, now):
        return connection.execute("""select 1 from local_analysis_runs r join local_extractions e
            on e.book_id=r.book_id and e.revision=r.extraction_revision
            where r.id=? and r.token=? and r.state='running' and r.lease_until>?""", (job["id"], job["token"], now)).fetchone() is not None

    def heartbeat(self, job):
        now = time.time()
        with closing(self.connection()) as connection, connection:
            connection.execute("begin immediate")
            if not self.owned(connection, job, now): return False
            connection.execute("update local_analysis_runs set lease_until=?,updated_at=? where id=?", (now+LEASE_SECONDS, now, job["id"]))
        return True

    def finish(self, job, result=None, error=None):
        now = time.time()
        with closing(self.connection()) as connection, connection:
            connection.execute("begin immediate")
            if not self.owned(connection, job, now): return False
            profile_only = AnalysisOptions.model_validate_json(job["options_json"]).purpose == "profile"
            state = "failed" if error else "done" if profile_only or any(q.usable for q in result.quotes) else "empty"
            connection.execute("update local_analysis_runs set state=?,stage=?,result_json=?,error=?,token=null,lease_until=null,updated_at=? where id=?",
                (state, LABELS[state], result.model_dump_json() if result else None, error, now, job["id"]))
        return True

    def latest(self, book_id, *, include_result=False, purpose="full"):
        if not self.uploads.db_path.is_file(): return None
        with closing(self.uploads._read_connection()) as connection:
            if not connection.execute("select 1 from sqlite_master where name='local_analysis_runs'").fetchone(): return None
            fields = "id,book_id,extraction_revision,model_id,state,stage,attempts,calls_started,error,options_json"
            if include_result: fields += ",result_json"
            row = connection.execute(f"select {fields} from local_analysis_runs where book_id=? and coalesce(json_extract(options_json,'$.purpose'),'full')=? order by updated_at desc,rowid desc limit 1", (book_id, purpose)).fetchone()
            if row is None: return None
            result = dict(row)
            saved_options = json.loads(result.pop("options_json"))
            old_profile_prompt = purpose == "profile" and saved_options.get("profile_prompt_version", 1) < AnalysisOptions().profile_prompt_version
            if old_profile_prompt: result["state"] = "stale"
            revision = connection.execute("select revision from local_extractions where book_id=?", (book_id,)).fetchone()
            if revision is None or revision[0] != result["extraction_revision"]: result["state"] = "stale"
            result["completed_steps"] = connection.execute("select count(*) from local_analysis_checkpoints where run_id=?", (row["id"],)).fetchone()[0]
        result["active"] = result["state"] in {"queued", "running"}
        result["label"] = LABELS[result["state"]]
        if purpose == "profile":
            result["label"] = {"done": "Profilvorschläge fertig", "empty": "Profilvorschläge fertig"}.get(result["state"], result["label"].replace("KI-Analyse", "KI-Profilanalyse"))
            if old_profile_prompt:
                result["label"] = "Bitte die Profilfelder mit den verbesserten Textvorgaben neu erstellen. Gespeicherter Buchkontext wird wiederverwendet."
        if include_result:
            raw = result.pop("result_json")
            try: result["result"] = AnalysisResult.model_validate_json(raw) if raw and result["state"] != "stale" else None
            except ValidationError: raise UploadError("Der gespeicherte Analysestand ist beschädigt. Bitte die Datensicherung prüfen.", 503) from None
        return result


class Checkpoints:
    def __init__(self, store, job, options, cancelled=lambda: False):
        self.store, self.job, self.options, self.cancelled = store, job, options, cancelled

    def check(self, connection):
        if self.cancelled() or not self.store.owned(connection, self.job, time.time()): raise AnalysisLeaseLost()

    def get(self, key, result_type):
        with closing(self.store.connection()) as connection:
            self.check(connection)
            row = connection.execute("select payload_json from local_analysis_checkpoints where run_id=? and step_key=?", (self.job["id"], key)).fetchone()
        if row is None: return None
        try: return result_type.model_validate_json(row[0], strict=True)
        except ValidationError: raise AnalysisError("Ein gespeicherter KI-Zwischenstand ist beschädigt. Bitte die Datensicherung prüfen.") from None

    def put(self, key, result):
        with closing(self.store.connection()) as connection, connection:
            connection.execute("begin immediate")
            self.check(connection)
            connection.execute("insert into local_analysis_checkpoints values (?,?,?)", (self.job["id"], key, result.model_dump_json()))

    def reserve_call(self):
        with closing(self.store.connection()) as connection, connection:
            connection.execute("begin immediate")
            self.check(connection)
            count = connection.execute("select calls_started from local_analysis_runs where id=?", (self.job["id"],)).fetchone()[0]
            if count >= self.options.max_calls: raise AnalysisError("Das Aufruflimit dieses Analyselaufs ist erreicht. Analyseumfang und ANALYSIS_MAX_CALLS prüfen.")
            connection.execute("update local_analysis_runs set calls_started=calls_started+1 where id=?", (self.job["id"],))

    def progress(self, stage):
        with closing(self.store.connection()) as connection, connection:
            connection.execute("begin immediate")
            self.check(connection)
            connection.execute("update local_analysis_runs set stage=?,updated_at=? where id=?", (stage, time.time(), self.job["id"]))
