"""Run explicitly queued AI work in the existing supervised worker process."""

import asyncio
from contextlib import suppress
import multiprocessing
import threading

from .analysis import AnalysisError, AnalysisOptions, PROMPT_VERSION, analyze_book
from .analysis_store import AnalysisStore, AnalysisLeaseLost, Checkpoints, endpoint_hash
from .config import load_settings
from .extraction_store import ExtractionStore
from .openwebui import OpenWebUIClient, OpenWebUIError


def run_analysis_once(uploads, env_path=None, stop=None, *, settings=None, api_factory=OpenWebUIClient):
    store = AnalysisStore(uploads)
    job = store.claim()
    if job is None: return False
    finished, lost = threading.Event(), threading.Event()

    def cancelled():
        parent = multiprocessing.parent_process()
        return lost.is_set() or (stop is not None and stop.is_set()) or (parent is not None and not parent.is_alive())

    def heartbeat():
        while not finished.wait(2):
            try:
                if cancelled() or not store.heartbeat(job):
                    lost.set()
                    return
            except Exception:
                lost.set()
                return

    heart = threading.Thread(target=heartbeat, daemon=True)
    heart.start()
    try:
        if settings is None:
            if env_path is None: raise AnalysisError("Keine ENV-Datei für den KI-Worker zugeordnet. Bitte über start.bat starten.")
            settings = load_settings(env_path)
        if job["prompt_version"] != PROMPT_VERSION or endpoint_hash(settings) != job["endpoint_hash"]:
            raise AnalysisError("Die Analysekonfiguration wurde geändert. Bitte mit der aktuellen Konfiguration einen neuen Analyselauf starten.")
        options = AnalysisOptions.model_validate_json(job["options_json"])
        source = ExtractionStore._decode(job["source_json"])
        api = api_factory(settings.model_copy(update={"openwebui_model": job["model_id"]}))
        checkpoints = Checkpoints(store, job, options, cancelled)

        async def execute():
            task = asyncio.create_task(analyze_book(source, api, checkpoints, options))
            try:
                while True:
                    done, _ = await asyncio.wait([task], timeout=.5)
                    if cancelled(): raise AnalysisLeaseLost()
                    if done: return task.result()
            finally:
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError): await task

        result = asyncio.run(execute())
        if not cancelled(): store.finish(job, result=result)
    except AnalysisLeaseLost:
        pass
    except (AnalysisError, OpenWebUIError) as exc:
        if not cancelled(): store.finish(job, error=str(exc))
    except Exception:
        if not cancelled(): store.finish(job, error="Die KI-Analyse konnte nicht abgeschlossen werden. Konfiguration und Datensicherung prüfen; fertige Zwischenstände bleiben erhalten.")
    finally:
        finished.set()
        heart.join(timeout=3)
    return True
