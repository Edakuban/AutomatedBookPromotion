"""Separate, restartable local import process. No network credentials are needed."""

import asyncio
from contextlib import asynccontextmanager
import multiprocessing
from pathlib import Path
import sqlite3
import threading
import time
from uuid import UUID

from .extraction_store import ExtractionStore
from .jobs import JobStore
from .uploads import LocalUploadStore


class LeaseLost(Exception):
    pass


def parent_alive():
    parent = multiprocessing.parent_process()
    return parent is None or parent.is_alive()


def run_once(uploads: LocalUploadStore, stop=None):
    jobs = JobStore(uploads)
    jobs.reconcile()
    job = jobs.claim()
    if job is None: return False
    done, lost = threading.Event(), threading.Event()

    def heartbeat():
        while not done.wait(2):
            try:
                if (stop and stop.is_set()) or not parent_alive() or not jobs.heartbeat(job):
                    lost.set()
                    return
            except (OSError, sqlite3.Error):
                lost.set()
                return

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    last_update, last_stage = 0.0, None

    def progress(stage, completed=0, total=0):
        nonlocal last_update, last_stage
        if lost.is_set() or (stop and stop.is_set()): raise LeaseLost()
        now = time.monotonic()
        if stage != last_stage or now - last_update >= .5 or (total and completed == total):
            if not jobs.progress(job, stage, completed, total): raise LeaseLost()
            last_update, last_stage = now, stage

    try:
        book = uploads.get_book(UUID(job["book_id"]))
        if book is None:
            result, error = None, "Das Buchprojekt wurde nicht gefunden. Bitte die lokale Ablage prüfen."
        else:
            extracts = ExtractionStore(uploads)
            record = extracts.get(book.id)
            if record and record.result:
                result, error = record.result, None
            else:
                result, error = extracts.prepare(book, progress)
        progress("Ergebnis speichern")
        jobs.finish(job, result, error)
    except LeaseLost:
        pass  # A replacement worker owns the job, or shutdown was requested.
    except Exception:
        # Never persist exception strings or tracebacks containing document text.
        if not lost.is_set():
            jobs.finish(job, None, "Der Import konnte nicht abgeschlossen werden. Bitte die Datei prüfen und erneut starten.")
    finally:
        done.set()
        thread.join(timeout=3)
    return True


class StopSignal:
    """A pipe remains usable after a killed process; shared Event locks may not."""
    def __init__(self, reader):
        self.reader = reader

    def is_set(self):
        return self.reader.poll()

    def wait(self, timeout):
        return self.reader.poll(timeout)


def worker_main(data_dir: str, max_bytes: int, reader, env_path=None):
    from .analysis_worker import run_analysis_once
    stop = StopSignal(reader)
    uploads = LocalUploadStore(Path(data_dir), max_bytes)
    while not stop.is_set() and parent_alive():
        try:
            worked = run_once(uploads, stop)
            if not worked:
                worked = run_analysis_once(uploads, env_path, stop)
        except (OSError, sqlite3.Error):
            worked = False  # Lease expiry permits recovery after temporary I/O errors.
        if not worked: stop.wait(1)


@asynccontextmanager
async def worker_lifespan(data_dir: Path, max_bytes: int, env_path=None):
    context = multiprocessing.get_context("spawn")
    def launch():
        reader, writer = context.Pipe(duplex=False)
        process = context.Process(target=worker_main, args=(str(data_dir.resolve()), max_bytes, reader, env_path), name="bookpromo-import")
        process.start()
        reader.close()
        return process, writer

    process, control = launch()

    async def supervise():
        nonlocal process, control
        while True:
            await asyncio.sleep(1)
            if not process.is_alive():
                process.join()
                process.close()
                control.close()
                process, control = launch()

    supervisor = asyncio.create_task(supervise())
    try:
        yield
    finally:
        supervisor.cancel()
        try:
            await supervisor
        except asyncio.CancelledError:
            pass
        try:
            control.send_bytes(b"stop")
        except (BrokenPipeError, OSError):
            pass
        control.close()
        await asyncio.to_thread(process.join, 3)
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 5)
        process.close()
