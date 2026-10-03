import sqlite3
import time
import json
from io import BytesIO

from fastapi.testclient import TestClient
import pytest

from bookpromo.chapter_teaser_worker import reconcile_chapter_teaser_media, run_chapter_teaser_once
from bookpromo.characters import CharacterStore
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
    assert [call[1]["chapter_metadata"]["position"] for call in api.calls] == [first["position"]]
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


def test_busy_run_only_locks_actions_for_its_own_chapter(setup):
    settings, _, book, _, _, store, _, run = ready_chapter_images(setup, with_reference=True)
    first, second = run["plans"]
    store.reopen_image_review(book.id, run["id"], [first["draft_id"]], regenerate=True)
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        html = client.get(f"/books/local/{book.id}/teaser").text
    row = html.split(f'data-page-position-anchor="chapter-{second["chapter_id"]}"', 1)[1].split("</tr>", 1)[0]
    for action in ("image/regenerate", "image/optimize"):
        form = row.split(f'/teaser/chapters/{second["chapter_id"]}/{action}', 1)[1].split("</form>", 1)[0]
        assert "disabled" not in form
    video = row.split(f'/teaser/chapters/{second["chapter_id"]}/video/start', 1)[1].split("</form>", 1)[0]
    assert "disabled" not in video
    assert "vorgemerkt oder läuft gerade" not in row
    busy_row = html.split(f'data-page-position-anchor="chapter-{first["chapter_id"]}"', 1)[1].split("</tr>", 1)[0]
    assert "vorgemerkt oder läuft gerade" in busy_row
    for action in ("image/regenerate", "image/optimize", "video/start"):
        form = busy_row.split(f'/teaser/chapters/{first["chapter_id"]}/{action}', 1)[1].split("</form>", 1)[0]
        assert "disabled" in form


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
    ("stale_extraction", "Der Buchstand wurde geändert. Bitte die Kapitelanalyse aktualisieren."),
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
    elif blocked == "stale_extraction":
        with sqlite3.connect(store.uploads.db_path) as connection, connection:
            connection.execute("update local_extractions set revision=revision+1 where book_id=?", (book.id,))
    else:
        settings.reel_video_workflow.unlink()
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        assert reason in client.get(f"/books/local/{book.id}/teaser").text
        url = f"/books/local/{book.id}/teaser/chapters/{first['chapter_id']}/video/start"
        assert client.post(url, data={"revision": draft.revision}, follow_redirects=False).status_code == 409
    assert jobs.claim(kinds={"video"}) is None


@pytest.mark.parametrize("operation", ["regenerate", "optimize", "prompt", "select"])
@pytest.mark.parametrize("job_state", ["queued", "running"])
def test_idle_chapter_image_actions_work_during_another_chapter_job(setup, operation, job_state):
    _, uploads, book, _, reels, store, jobs, run = ready_chapter_images(setup)
    first, second = run["plans"]
    store.start_video(book.id, run["id"], first["draft_id"])
    if job_state == "running":
        assert jobs.claim(kinds={"video"}).draft_id == first["draft_id"]
    before = store.latest(book.id, include_plans=True)["plans"][0]
    draft = reels.get_draft(second["draft_id"])
    if operation in {"regenerate", "optimize"}:
        store.reopen_image_review(book.id, run["id"], [draft.id], **{operation: True})
        own_jobs = [job for job in jobs.status(draft.id) if job["state"] == "queued"]
        assert len(own_jobs) == 1 and own_jobs[0]["kind"] == "image"
        with pytest.raises(UploadError):
            store.reopen_image_review(book.id, run["id"], [draft.id], regenerate=True)
    elif operation == "prompt":
        store.save_prompt(book.id, run["id"], draft.id, draft.revision,
                          image_prompt="An adult person in the established bedroom.", teaser_text="Ein neuer Text.")
        assert not reconcile_chapter_teaser_media(uploads, reels, jobs)
        assert jobs.claim(kinds={"image"}) is None
    else:
        store.set_plan_state(run["id"], draft.id, "failed", "Earlier failure")
        store.select_image(book.id, run["id"], draft.id, draft.revision, "scene")
    store.refresh_run(run["id"])
    after = store.latest(book.id, include_plans=True)
    assert after["state"] == "rendering" and after["plans"][0] == before
    assert any(job["state"] == job_state for job in jobs.status(first["draft_id"]))


@pytest.mark.parametrize("job_state", ["queued", "running"])
def test_own_chapter_job_blocks_all_mutations(setup, job_state):
    _, _, book, _, reels, store, jobs, run = ready_chapter_images(setup)
    draft = reels.get_draft(run["plans"][0]["draft_id"])
    store.reopen_image_review(book.id, run["id"], [draft.id], optimize=True)
    if job_state == "running":
        assert jobs.claim(kinds={"image"}).draft_id == draft.id
    with pytest.raises(UploadError, match="Verarbeitungsschritt"):
        store.reopen_image_review(book.id, run["id"], [draft.id], regenerate=True)
    with pytest.raises(UploadError, match="Verarbeitungsschritt"):
        store.save_prompt(book.id, run["id"], draft.id, draft.revision,
                          image_prompt=draft.image_prompt, teaser_text="Neuer Text.")
    with pytest.raises(UploadError, match="Verarbeitungsschritt"):
        store.select_image(book.id, run["id"], draft.id, draft.revision, "scene")
    with pytest.raises(UploadError, match="Verarbeitungsschritt"):
        store.start_video(book.id, run["id"], draft.id)


@pytest.mark.parametrize("operation", ["regenerate", "optimize", "prompt", "select", "video"])
def test_ready_chapter_actions_preserve_active_analysis_ownership(setup, operation):
    _, uploads, book, _, reels, store, _, run = ready_chapter_images(setup)
    first, second = run["plans"]
    lease = time.time() + 120
    with sqlite3.connect(uploads.db_path) as connection, connection:
        connection.execute(
            "update local_chapter_teaser_runs set state='running',token='owner',lease_until=?,stage='Analyse',completed=1 where id=?",
            (lease, run["id"]),
        )
        connection.execute("update local_chapter_teaser_plans set state='pending' where run_id=? and chapter_id=?",
                           (run["id"], second["chapter_id"]))
    draft = reels.get_draft(first["draft_id"])
    if operation in {"regenerate", "optimize"}:
        store.reopen_image_review(book.id, run["id"], [draft.id], **{operation: True})
    elif operation == "prompt":
        store.save_prompt(book.id, run["id"], draft.id, draft.revision,
                          image_prompt=draft.image_prompt, teaser_text="Ein neuer Text.")
    elif operation == "select":
        store.set_plan_state(run["id"], draft.id, "failed", "Earlier failure")
        store.select_image(book.id, run["id"], draft.id, draft.revision, "scene")
    else:
        store.start_video(book.id, run["id"], draft.id)
    with sqlite3.connect(uploads.db_path) as connection:
        owned = connection.execute("select state,token,lease_until,stage,completed from local_chapter_teaser_runs where id=?", (run["id"],)).fetchone()
    assert owned == ("running", "owner", lease, "Analyse", 1)
    assert store.heartbeat({"id": run["id"], "token": "owner"})


def test_optimization_job_keeps_run_active_until_it_finishes(setup):
    _, uploads, book, _, reels, store, jobs, run = ready_chapter_images(setup)
    first = run["plans"][0]
    store.reopen_image_review(book.id, run["id"], [first["draft_id"]], optimize=True)
    store.refresh_run(run["id"])
    assert store.latest(book.id)["state"] == "rendering"
    assert store.latest(book.id)["completed"] == 1
    job = jobs.claim(kinds={"image"})
    path, digest = reels.save_artifact(job.draft_id, "image", png_bytes(), "optimized.png")
    assert jobs.finish(job, result={"path": path, "sha256": digest, "candidate": "optimized"})
    reconcile_chapter_teaser_media(uploads, reels, jobs)
    assert store.latest(book.id)["state"] == "done"


def test_bulk_optimization_only_queues_idle_chapters(setup):
    settings, uploads, book, _, _, store, jobs, run = ready_chapter_images(setup, with_reference=True)
    character = CharacterStore(uploads).list(book.id)[0]
    # Both prepared chapter scenes already have a configured character reference.
    with sqlite3.connect(uploads.db_path) as connection, connection:
        for plan in run["plans"]:
            connection.execute("update local_reel_drafts set character_ids_json=? where id=?",
                               (json.dumps([character.id]), plan["draft_id"]))
    first, second = run["plans"]
    store.start_video(book.id, run["id"], first["draft_id"])
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        response = client.post(f"/books/local/{book.id}/teaser/chapters/images/optimize", follow_redirects=False)
        assert response.status_code == 303
    assert jobs.claim(kinds={"image"}).draft_id == second["draft_id"]
    assert jobs.claim(kinds={"image"}) is None
    assert jobs.claim(kinds={"video"}).draft_id == first["draft_id"]


def failed_optimization_with_existing_video(setup):
    settings, uploads, book, _, reels, store, jobs, run = ready_chapter_images(setup)
    first = run["plans"][0]
    jobs.enqueue(first["draft_id"], "video")
    video_job = jobs.claim(kinds={"video"})
    path, digest = reels.save_artifact(
        first["draft_id"], "video", BytesIO(b"\x00\x00\x00\x18ftypisom-old-video"), "old.mp4",
    )
    assert jobs.finish(video_job, result={"path": path, "sha256": digest})
    store.set_plan_state(run["id"], first["draft_id"], "done")
    store.reopen_image_review(book.id, run["id"], [first["draft_id"]], optimize=True)
    optimization = jobs.claim(kinds={"image"})
    assert jobs.finish(optimization, error="Die automatischen Masken überlappen zu stark.")
    reconcile_chapter_teaser_media(uploads, reels, jobs)
    return settings, uploads, book, reels, store, jobs, run, first, path


@pytest.mark.parametrize("regenerate", [False, True])
def test_failed_optimization_allows_scene_video_regeneration_or_text_only_update(setup, regenerate):
    settings, _, book, reels, store, jobs, run, first, old_video = failed_optimization_with_existing_video(setup)
    draft = reels.get_draft(first["draft_id"])
    assert not draft.image_stale and not draft.video_stale
    base = f"/books/local/{book.id}/teaser"
    url = base + f"/chapters/{first['chapter_id']}/video/start"
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        row = client.get(base).text.split(f'data-page-position-anchor="chapter-{first["chapter_id"]}"', 1)[1].split("</tr>", 1)[0]
        video_form = row.split(url, 1)[1].split("</form>", 1)[0]
        assert 'name="regenerate" value="1"' in video_form
        assert "Video neu erzeugen" in video_form
        assert "Nur Textfassung aktualisieren" in video_form
        assert "disabled" not in video_form
        assert "Masken überlappen" in row
        data = {"revision": draft.revision}
        if regenerate:
            data["regenerate"] = "1"
        assert client.post(url, data=data, follow_redirects=False).status_code == 303
        assert client.post(url, data=data, follow_redirects=False).status_code == 409
    updated = reels.get_draft(draft.id)
    assert updated.selected_image_source == "scene"
    assert updated.selected_image_path == draft.scene_image_path
    assert updated.selected_video_path == old_video  # The old artifact is retained.
    assert updated.video_stale == regenerate
    assert store.latest(book.id, include_plans=True)["plans"][0]["state"] == "video_queued"
    job = jobs.claim(kinds={"video"})
    if regenerate:
        assert job is not None and job.draft_id == draft.id and job.input_revision == draft.revision
    else:
        assert job is None


@pytest.mark.parametrize("invalid", ["missing_revision", "stale_revision", "invalid_flag", "unknown_field"])
def test_explicit_video_regeneration_rejects_invalid_or_stale_requests(setup, invalid):
    settings, _, book, reels, _, jobs, _, first, _ = failed_optimization_with_existing_video(setup)
    draft = reels.get_draft(first["draft_id"])
    data = {"revision": draft.revision, "regenerate": "1"}
    if invalid == "missing_revision":
        del data["revision"]
    elif invalid == "stale_revision":
        data["revision"] -= 1
    elif invalid == "invalid_flag":
        data["regenerate"] = "yes"
    else:
        data["unknown"] = "1"
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        response = client.post(f"/books/local/{book.id}/teaser/chapters/{first['chapter_id']}/video/start",
                               data=data, follow_redirects=False)
        assert response.status_code in {400, 409}
    assert jobs.claim(kinds={"video"}) is None
    assert not reels.get_draft(draft.id).video_stale
