from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import multiprocessing
import threading
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from bookpromo.config import Settings
from bookpromo.extraction import Boundary
from bookpromo.extraction_store import ExtractionStore
from bookpromo.jobs import JobStore, LEASE_SECONDS
from bookpromo.uploads import LocalUploadStore
from bookpromo.web import create_app
from bookpromo.worker import run_once
from test_uploads import docx_bytes


@pytest.fixture
def setup(tmp_path, monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    settings = Settings(_env_file=None, app_data_dir=tmp_path / "data")
    uploads = LocalUploadStore(settings.app_data_dir, 1024 * 1024)
    book, _ = uploads.save(BytesIO(docx_bytes()), "Buch.docx")
    return settings, uploads, book, JobStore(uploads)


def test_upload_queues_without_running_extraction(setup, monkeypatch):
    settings, uploads, book, jobs = setup
    monkeypatch.setattr(ExtractionStore, "prepare", lambda *args: pytest.fail("Extraction inside upload"))
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        response = client.post("/uploads", files={"file": ("Neu.docx", docx_bytes("Neues Buch."))}, headers={"Accept": "application/json"})
        assert response.status_code == 201
        url = response.json()["url"]
        status = client.get(url + "/status").json()
        assert status["state"] == "queued" and status["active"]
        assert not {"token", "lease_until", "result_json", "source_path"} & status.keys()
        assert "Import im Hintergrund" in client.get(url).text
        assert "Wartet" in client.get("/").text


def test_worker_finishes_and_corrections_update_overview(setup):
    settings, uploads, book, jobs = setup
    assert run_once(uploads)
    status = jobs.statuses([book.id])[book.id]
    assert status["state"] == "needs_review" and status["chapter_count"] == 1
    extracts = ExtractionStore(uploads)
    record = extracts.get(book.id)
    extracts.correct(book.id, record.revision, [Boundary(paragraph_id="p000001", title="Bestätigt")])
    assert jobs.statuses([book.id])[book.id]["state"] == "done"
    jobs.enqueue(book, retry=True)
    assert not run_once(uploads)
    assert extracts.get(book.id).result.chapters[0].title == "Bestätigt"


def test_old_snapshots_are_adopted_without_reprocessing(setup, monkeypatch):
    settings, uploads, book, jobs = setup
    original = ExtractionStore(uploads).extract(book)
    monkeypatch.setattr(ExtractionStore, "prepare", lambda *args: pytest.fail("Checkpoint recomputed"))
    assert not run_once(uploads)
    assert ExtractionStore(uploads).get(book.id) == original
    assert jobs.statuses([book.id])[book.id]["chapter_count"] == 1


def test_claims_serialize_books_and_reject_stale_worker_writes(setup):
    settings, uploads, book, jobs = setup
    other, _ = uploads.save(BytesIO(docx_bytes("Ein anderes Buch.")), "Zwei.docx")
    jobs.reconcile()
    now = time.time()
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: jobs.claim(now=now), range(2)))
    assert sum(job is not None for job in claims) == 1
    stale = next(job for job in claims if job)
    fresh = jobs.claim(now=now + LEASE_SECONDS + 1)
    assert fresh["book_id"] == stale["book_id"] and fresh["token"] != stale["token"]
    assert not jobs.heartbeat(stale, now=now + LEASE_SECONDS + 1)
    assert not jobs.finish(stale, None, "STALE", now=now + LEASE_SECONDS + 1)
    assert ExtractionStore(uploads).get(stale["book_id"]) is None
    assert jobs.finish(fresh, None, "Testfehler", now=now + LEASE_SECONDS + 2)
    assert jobs.claim(now=now + LEASE_SECONDS + 3)["book_id"] == other.id


def test_expired_lease_cannot_be_resurrected(setup):
    settings, uploads, book, jobs = setup
    jobs.enqueue(book)
    now = time.time()
    claim = jobs.claim(now=now)
    assert jobs.heartbeat(claim, now=now + 5)
    assert not jobs.heartbeat(claim, now=now + 5 + LEASE_SECONDS)
    assert not jobs.finish(claim, None, "Too late", now=now + 5 + LEASE_SECONDS)


def test_repeated_crashes_stop_after_three_attempts_and_manual_retry_works(setup):
    settings, uploads, book, jobs = setup
    jobs.enqueue(book)
    now = time.time()
    for attempt in range(3):
        assert jobs.claim(now=now + attempt * (LEASE_SECONDS + 1))
    assert jobs.claim(now=now + 3 * (LEASE_SECONDS + 1)) is None
    status = jobs.statuses([book.id])[book.id]
    assert status["state"] == "failed" and status["attempts"] == 3
    jobs.enqueue(book)  # Duplicate upload is not an unbounded automatic retry.
    assert jobs.statuses([book.id])[book.id]["state"] == "failed"
    jobs.enqueue(book, retry=True)
    assert run_once(uploads)
    assert jobs.statuses([book.id])[book.id]["state"] == "needs_review"


def test_unexpected_error_is_safe_and_retry_reuses_job(setup, monkeypatch):
    settings, uploads, book, jobs = setup
    original = ExtractionStore.prepare
    def fail(*args): raise RuntimeError("PRIVATE_KEY_OR_BOOK_TEXT")
    monkeypatch.setattr(ExtractionStore, "prepare", fail)
    assert run_once(uploads)
    status = jobs.statuses([book.id])[book.id]
    assert status["state"] == "failed" and "PRIVATE" not in status["error"]
    monkeypatch.setattr(ExtractionStore, "prepare", original)
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        url = f"/books/local/{book.id}"
        assert "Erneut einlesen" in client.get(url).text
        assert client.post(url + "/extract", headers={"Origin": "https://foreign.example"}).status_code == 403
        assert "Import im Hintergrund" in client.post(url + "/extract").text
        assert run_once(uploads)
        assert client.get(url + "/status").json()["state"] == "needs_review"
        assert client.get(f"/books/local/{uuid4()}/status").status_code == 404


def test_progress_and_web_remain_readable_during_extraction(setup, monkeypatch):
    settings, uploads, book, jobs = setup
    started, release = threading.Event(), threading.Event()
    original = ExtractionStore.prepare
    def slow(self, book, progress):
        progress("Text lesen", 25, 100)
        started.set()
        assert release.wait(5)
        return original(self, book, progress)
    monkeypatch.setattr(ExtractionStore, "prepare", slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_once, uploads)
        try:
            assert started.wait(3)
            with TestClient(create_app(settings, start_worker=False)) as client:
                status = client.get(f"/books/local/{book.id}/status").json()
                assert status["completed"] == 25 and status["total"] == 100
                assert status["state"] == "running"
                assert client.get("/").status_code == 200
        finally:
            release.set()
        assert future.result(timeout=5)


def test_atomic_checkpoint_is_not_overwritten_by_worker(setup):
    settings, uploads, book, jobs = setup
    jobs.enqueue(book)
    claim = jobs.claim()
    extracts = ExtractionStore(uploads)
    record = extracts.extract(book)
    extracts.correct(book.id, record.revision, [Boundary(paragraph_id="p000001", title="Manuell korrigiert")])
    assert jobs.finish(claim, None, "Veralteter Fehler")
    assert extracts.get(book.id).result.chapters[0].title == "Manuell korrigiert"
    assert jobs.statuses([book.id])[book.id]["state"] == "done"


def test_legacy_failed_import_can_be_retried_on_first_request(setup):
    settings, uploads, book, jobs = setup
    (uploads.root / book.source_path).unlink()
    assert ExtractionStore(uploads).extract(book).error
    jobs.enqueue(book, retry=True)
    assert jobs.statuses([book.id])[book.id]["state"] == "queued"


def test_spawned_worker_recovers_jobs_restarts_and_shuts_down(setup):
    settings, uploads, book, jobs = setup
    jobs.enqueue(book)
    assert jobs.claim(now=time.time() - LEASE_SECONDS - 1)  # Simulate an abandoned lease.
    existing = {child.pid for child in multiprocessing.active_children()}

    def wait_for_import(client, url):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = client.get(url + "/status").json()
            if not status["active"]:
                assert status["state"] == "needs_review", status
                assert status["chapter_count"] == 1
                return
            time.sleep(.05)
        pytest.fail("Separate import worker did not complete the job")

    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        wait_for_import(client, f"/books/local/{book.id}")
        assert jobs.statuses([book.id])[book.id]["attempts"] == 2
        assert "Kapitelaufteilung prüfen und korrigieren" in client.get(f"/books/local/{book.id}").text
        owned = [child for child in multiprocessing.active_children() if child.pid not in existing and child.name == "bookpromo-import"]
        assert len(owned) == 1
        owned[0].terminate()
        owned[0].join(timeout=3)
        response = client.post("/uploads", files={"file": ("Nach Neustart.docx", docx_bytes("Text nach Neustart."))}, headers={"Accept": "application/json"})
        assert response.status_code == 201
        wait_for_import(client, response.json()["url"])
    assert not [child for child in multiprocessing.active_children() if child.pid not in existing and child.name == "bookpromo-import"]
