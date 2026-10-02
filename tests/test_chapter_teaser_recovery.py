import sqlite3

from fastapi.testclient import TestClient
import pytest

from bookpromo.chapter_teaser_worker import reconcile_chapter_teaser_media, run_chapter_teaser_once
from bookpromo.openwebui import OpenWebUIError
from bookpromo.reels import ReelJobStore
from bookpromo.uploads import UploadError
from bookpromo.web import create_app
from test_analysis import setup
from test_chapter_teasers import ChapterAPI, prepare_run, ready_chapter_images
from test_reels import png_bytes


def fail_all_analyses(setup):
    settings, uploads, book, record, reels, track, store, run_id = prepare_run(setup)
    settings.reel_image_workflow.parent.mkdir(parents=True, exist_ok=True)
    settings.reel_image_workflow.write_text("{}", encoding="utf-8")

    class InvalidSchemaAPI(ChapterAPI):
        async def complete_json(self, *args, **kwargs):
            raise OpenWebUIError("structured")

    assert run_chapter_teaser_once(
        uploads, settings=settings, api_factory=lambda _: InvalidSchemaAPI(record.result.chapters),
    )
    assert store.latest(book.id)["state"] == "partial"
    return settings, uploads, book, record, reels, store, run_id


def finish_image(uploads, reels):
    jobs = ReelJobStore(reels)
    job = jobs.claim(kinds={"image"})
    assert job is not None
    path, digest = reels.save_artifact(job.draft_id, "image", png_bytes(), "scene.png")
    assert jobs.finish(job, result={"path": path, "sha256": digest, "candidate": "scene"})
    assert reconcile_chapter_teaser_media(uploads, reels, jobs)
    assert jobs.claim(kinds={"image"}) is None


def test_retry_analysis_route_repairs_only_selected_failed_chapter(setup):
    settings, uploads, book, record, reels, store, run_id = fail_all_analyses(setup)
    before = store.latest(book.id, include_plans=True)
    first, second = before["plans"]
    url = f"/books/local/{book.id}/teaser/chapters/{first['chapter_id']}/analysis/retry"
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        html = client.get(f"/books/local/{book.id}/teaser").text
        assert "Kapitel erneut analysieren" in html
        assert first["error"] in html
        assert client.post(url, data={"run_id": "outdated"}, follow_redirects=False).status_code == 409
        assert client.post(url, data={"run_id": run_id}, follow_redirects=False).status_code == 303
        assert client.post(url, data={"run_id": run_id}, follow_redirects=False).status_code == 409
    api = ChapterAPI(record.result.chapters)
    assert run_chapter_teaser_once(uploads, settings=settings, api_factory=lambda _: api)
    assert [call[1]["chapter"]["position"] for call in api.calls] == [first["position"]]
    finish_image(uploads, reels)
    after = store.latest(book.id, include_plans=True)
    assert after["state"] == "partial"
    assert after["plans"][0]["state"] == "analyzed"
    assert after["plans"][1] == second


def test_retry_one_chapter_in_aborted_run_leaves_other_pending_chapters_untouched(setup):
    settings, uploads, book, record, reels, _, store, run_id = prepare_run(setup)
    with sqlite3.connect(uploads.db_path) as connection, connection:
        connection.execute("update local_chapter_teaser_runs set state='failed' where id=?", (run_id,))
    before = store.latest(book.id, include_plans=True)
    first, second = before["plans"]
    store.retry_analysis(book.id, run_id, first["chapter_id"])
    api = ChapterAPI(record.result.chapters)
    assert run_chapter_teaser_once(uploads, settings=settings, api_factory=lambda _: api)
    assert len(api.calls) == 1
    finish_image(uploads, reels)
    after = store.latest(book.id, include_plans=True)
    assert after["state"] == "partial" and not after["active"]
    assert after["completed"] == 1
    assert after["plans"][1] == second
    store.retry_analysis(book.id, run_id, second["chapter_id"])
    assert run_chapter_teaser_once(uploads, settings=settings, api_factory=lambda _: api)
    assert len(api.calls) == 2
    finish_image(uploads, reels)
    assert store.latest(book.id)["state"] == "done"


def test_busy_run_keeps_image_buttons_visible_and_allows_idle_chapter_video(setup):
    settings, _, book, _, _, store, _, run = ready_chapter_images(setup, with_reference=True)
    first, second = run["plans"]
    store.reopen_image_review(book.id, run["id"], [first["draft_id"]], regenerate=True)
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        html = client.get(f"/books/local/{book.id}/teaser").text
    row = html.split(f'data-page-position-anchor="chapter-{second["chapter_id"]}"', 1)[1].split("</tr>", 1)[0]
    for action in ("image/regenerate", "image/optimize"):
        form = row.split(f'/teaser/chapters/{second["chapter_id"]}/{action}', 1)[1].split("</form>", 1)[0]
        assert "disabled" in form
    video = row.split(f'/teaser/chapters/{second["chapter_id"]}/video/start', 1)[1].split("</form>", 1)[0]
    assert "disabled" not in video
    assert "nach dem laufenden Kapitel-Schritt" in row


def test_failed_run_allows_valid_existing_chapter_image_to_be_repaired(setup):
    settings, _, book, _, _, store, jobs, run = ready_chapter_images(setup)
    first = run["plans"][0]
    with sqlite3.connect(store.uploads.db_path) as connection, connection:
        connection.execute("update local_chapter_teaser_runs set state='failed' where id=?", (run["id"],))
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        url = f"/books/local/{book.id}/teaser/chapters/{first['chapter_id']}/image/regenerate"
        assert client.post(url, follow_redirects=False).status_code == 303
    assert jobs.claim(kinds={"image"}).draft_id == first["draft_id"]


def test_analysis_retry_rejects_stale_extraction_and_chapters_with_existing_drafts(setup):
    _, _, book, _, _, store, _, run = ready_chapter_images(setup)
    with pytest.raises(UploadError, match="keine erneute Analyse"):
        store.retry_analysis(book.id, run["id"], run["plans"][0]["chapter_id"])
    with sqlite3.connect(store.uploads.db_path) as connection, connection:
        connection.execute("update local_extractions set revision=revision+1 where book_id=?", (book.id,))
    with pytest.raises(UploadError, match="zwischenzeitlich"):
        store.retry_analysis(book.id, run["id"], run["plans"][0]["chapter_id"])


@pytest.mark.parametrize("other_job", ["image", "video"])
def test_single_video_can_be_queued_while_another_chapter_is_processing(setup, other_job):
    settings, _, book, _, reels, store, jobs, run = ready_chapter_images(setup)
    first, second = run["plans"]
    if other_job == "image":
        store.reopen_image_review(book.id, run["id"], [first["draft_id"]], regenerate=True)
    else:
        store.start_video(book.id, run["id"], first["draft_id"])
    before = store.latest(book.id, include_plans=True)["plans"][0]
    draft = reels.get_draft(second["draft_id"])
    url = f"/books/local/{book.id}/teaser/chapters/{second['chapter_id']}/video/start"
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        assert client.post(url, data={"revision": draft.revision}, follow_redirects=False).status_code == 303
        assert client.post(url, data={"revision": draft.revision}, follow_redirects=False).status_code == 409
    after = store.latest(book.id, include_plans=True)
    assert after["state"] == "rendering"
    assert after["plans"][0] == before
    assert after["plans"][1]["state"] == "video_queued"
    assert any(job["state"] == "queued" for job in jobs.status(first["draft_id"]))
    assert len([job for job in jobs.status(second["draft_id"]) if job["kind"] == "video"]) == 1


@pytest.mark.parametrize("blocked,reason", [
    ("own_image_job", "Für dieses Kapitel läuft bereits ein Bild- oder Videojob."),
    ("stale_image", "Das Bild ist veraltet."),
    ("analysis_running", "Bitte zuerst die laufende Kapitelanalyse abschließen lassen."),
    ("workflow_missing", "Der ComfyUI-Video-Workflow ist nicht eingerichtet."),
])
def test_single_video_still_blocks_unsafe_states_and_explains_why(setup, blocked, reason):
    settings, _, book, _, reels, store, jobs, run = ready_chapter_images(setup)
    first = run["plans"][0]
    draft = reels.get_draft(first["draft_id"])
    if blocked == "own_image_job":
        store.reopen_image_review(book.id, run["id"], [draft.id], optimize=True)
    elif blocked == "stale_image":
        with sqlite3.connect(store.uploads.db_path) as connection, connection:
            connection.execute("update local_reel_drafts set image_stale=1 where id=?", (draft.id,))
    elif blocked == "analysis_running":
        with sqlite3.connect(store.uploads.db_path) as connection, connection:
            connection.execute("update local_chapter_teaser_runs set state='running' where id=?", (run["id"],))
    else:
        settings.reel_video_workflow.unlink()
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        assert reason in client.get(f"/books/local/{book.id}/teaser").text
        url = f"/books/local/{book.id}/teaser/chapters/{first['chapter_id']}/video/start"
        assert client.post(url, data={"revision": draft.revision}, follow_redirects=False).status_code == 409
    assert jobs.claim(kinds={"video"}) is None
