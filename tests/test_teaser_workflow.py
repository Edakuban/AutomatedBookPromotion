from io import BytesIO

from fastapi.testclient import TestClient
import pytest

from bookpromo.book_teasers import BookTeaserStore, complete_chapter_segments
from bookpromo.database import DatabaseError
from bookpromo.publication import PublicationDefaults, PlatformDefault
from bookpromo.sync import SyncStore
from bookpromo.web import create_app
from test_analysis import setup
from test_chapter_teasers import ready_chapter_images


def ready_videos(setup):
    settings, uploads, book, record, reels, store, jobs, run = ready_chapter_images(setup)
    store.begin_videos(book.id, run["id"], [plan["draft_id"] for plan in run["plans"]])
    for _ in record.result.chapters:
        job = jobs.claim(kinds={"video"})
        data = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32
        path, digest = reels.save_artifact(job.draft_id, "video", BytesIO(data), "clean.mp4")
        assert jobs.finish(job, result={"path": path, "sha256": digest})
        text_path, text_digest = reels.save_artifact(
            job.draft_id, "video", BytesIO(data + b"text"), "text.mp4",
        )
        store.save_text_video(run["id"], job.draft_id, text_path, text_digest)
        store.set_plan_state(run["id"], job.draft_id, "done")
    store.refresh_run(run["id"])
    run = store.latest(book.id, include_plans=True)
    for plan in run["plans"]:
        plan["draft"] = reels.get_draft(plan["draft_id"])
    return settings, uploads, book, record, reels, store, jobs, run


def test_top_complete_render_uses_every_chapter_not_quote_fallback(setup):
    settings, uploads, book, _, _, _, _, run = ready_videos(setup)
    route = f"/books/local/{book.id}/teaser/render/complete"
    data = {"revision": "0", "audio_track_id": run["audio_track_id"],
            "aspect": "horizontal_fit", "transition_seconds": "0.5"}
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        page = client.get(f"/books/local/{book.id}/teaser")
        assert page.status_code == 200 and "Alle aktuellen Kapitelvideos sind bereit." in page.text
        assert client.post(route, data=data, follow_redirects=False).status_code == 303
        assert client.post(route, data=data, follow_redirects=False).status_code == 409
    project = BookTeaserStore(uploads).get(book.id)
    assert project.aspect == "horizontal_fit"
    assert [segment.draft_id for segment in project.segments] == [plan["draft_id"] for plan in run["plans"]]
    assert all(segment.included for segment in project.segments)


def test_top_complete_render_refuses_incomplete_chapters(setup):
    settings, uploads, book, _, _, _, _, run = ready_chapter_images(setup)
    route = f"/books/local/{book.id}/teaser/render/complete"
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        response = client.post(route, data={"revision": "0", "audio_track_id": run["audio_track_id"],
                                           "aspect": "vertical", "transition_seconds": "0.5"})
        assert response.status_code == 409
    assert BookTeaserStore(uploads).get(book.id) is None


@pytest.mark.parametrize("queue_mode", ["daily", "scheduled"])
def test_final_queue_route_checks_revision_schema_and_keeps_chapter_states(setup, monkeypatch, queue_mode):
    settings, uploads, book, record, reels, _, _, run = ready_videos(setup)
    store = BookTeaserStore(uploads)
    project = store.save(
        book.id, 0, aspect="horizontal_fit", audio_track_id=run["audio_track_id"],
        transition_ms=500,
        segments=complete_chapter_segments(book.id, record.result.chapters, run["plans"], reels),
    )
    output = store.render_path(project)
    data = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32
    output.write_bytes(data)
    project = store.mark_rendered(project.id, project.revision, output, 30000)
    SyncStore(uploads).save_receipt(book.id, {"revision": 1, "hash": "synced"})
    reels.save_publication_defaults(0, PublicationDefaults(
        youtube=PlatformDefault(enabled=True, account_id="youtube-test"),
    ))
    monkeypatch.setattr("bookpromo.teaser_publication._media_manifest",
                        lambda *args: (data, 1920, 1080, 30000))

    class Repository:
        ready = True
        calls = []

        async def check_schema(self, **kwargs):
            if kwargs.get("book_teasers") and not self.ready:
                raise DatabaseError("book_teaser_schema")
            return 10

        async def upload_book_teaser(self, path, payload):
            self.calls.append(("upload", path, payload))

        async def enqueue_reel(self, asset, publications):
            self.calls.append(("queue", asset, publications))
            return {"reel_id": asset["id"]}

    db = Repository()
    route = f"/books/local/{book.id}/teaser/queue"
    form = {"project_revision": str(project.revision), "title": "Roman · Trailer",
            "description": "Der ganze Trailer.", "platform": "youtube", "requeue": "0",
            "queue_mode": queue_mode,
            "scheduled_for": "2099-01-01T12:00" if queue_mode == "scheduled" else ""}
    before = [(draft.id, draft.revision, draft.state) for draft in reels.list_drafts(book.id)]
    with TestClient(create_app(settings, repository=db, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        stale = dict(form, project_revision="0")
        assert client.post(route, data=stale).status_code == 409
        assert not db.calls
        bad = dict(form, platform="facebook")
        assert client.post(route, data=bad).status_code == 400
        assert not db.calls
        db.ready = False
        assert client.post(route, data=form).status_code == 503
        assert not db.calls
        db.ready = True
        response = client.post(route, data=form, follow_redirects=False)
        assert response.status_code == 303 and "teaser_queued=true" in response.headers["location"]
    assert [call[0] for call in db.calls] == ["upload", "queue"]
    _, asset, publications = db.calls[1]
    assert asset["source_kind"] == "book_teaser" and asset["chapter_id"] is None
    assert asset["width"] == 1920 and asset["duration_ms"] == 30000
    assert publications[0]["queue_mode"] == queue_mode
    if queue_mode == "scheduled":
        assert publications[0]["scheduled_for"].endswith("+01:00")
    assert before == [(draft.id, draft.revision, draft.state) for draft in reels.list_drafts(book.id)]
