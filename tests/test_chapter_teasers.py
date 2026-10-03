import asyncio
import hashlib
from io import BytesIO
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

from bookpromo.chapter_teaser_worker import (
    reconcile_chapter_teaser_media, run_chapter_teaser_once,
)
from bookpromo.chapter_teasers import (
    ChapterTeaserError, ChapterTeaserStore, ChapterTeaserSuggestion,
    analyze_whole_chapter,
)
from bookpromo.characters import CharacterStore
from bookpromo.analysis_store import endpoint_hash
from bookpromo.reels import ReelJobStore, ReelStore
from bookpromo.publication import PlatformDefault, PublicationDefaults
from bookpromo.openwebui import OpenWebUIError
from bookpromo.sync import SyncStore
from bookpromo.uploads import UploadError
from bookpromo.web import create_app
from test_analysis import setup
from test_reels import png_bytes, wav_bytes
from test_characters import image_file


def suggestion(chapter):
    return ChapterTeaserSuggestion(
        scene_summary="Eine atmosphärische Schlüsselszene öffnet eine Frage.",
        source_excerpt=chapter.source_text,
        teaser_text="Etwas wartet in der Stille. Doch nicht mehr lange.",
        image_prompt=(
            "Cinematic vertical frame of the established adult protagonist in the documented "
            "location, dramatic practical light, no text or logos."
        ),
        video_prompt=(
            "The camera tracks laterally past the existing subject while rain moves across the "
            "window and practical light shifts over the documented room. Preserve all identities, "
            "wardrobe, objects, framing, and scene geometry in one continuous shot."
        ),
        motion_intensity="medium",
    )


class ChapterAPI:
    def __init__(self, chapters):
        self.chapters = list(chapters)
        self.calls = []

    async def complete_json(self, system, user, result_type, **kwargs):
        payload = json.loads(user)
        self.calls.append((system, payload, result_type, kwargs))
        chapter = next(
            item for item in self.chapters
            if item.position == payload["chapter_metadata"]["position"]
        )
        return suggestion(chapter)


def prepare_run(setup, *, seconds=30):
    settings, uploads, book, record, _ = setup
    reels = ReelStore(uploads, max_audio_bytes=1024 * 1024)
    track, _ = reels.save_audio(book.id, wav_bytes(seconds), "book-song.wav", title="Book song")
    store = ChapterTeaserStore(uploads)
    run_id = store.enqueue(
        book.id, settings, audio_track_id=track.id, transition_ms=500,
        book_context={"title": book.title, "spoilers": "Das Ende bleibt intern."},
    )
    return settings, uploads, book, record, reels, track, store, run_id


def test_whole_chapter_is_sent_unchunked_and_excerpt_must_be_verbatim(setup):
    chapter = setup[3].result.chapters[0]
    api = ChapterAPI([chapter])
    result = asyncio.run(analyze_whole_chapter(
        api, chapter=chapter, book_context={
            "genre": "Roman", "mood": "Düster",
            "image_prompt_base": "Cinematic chiaroscuro. A stranger waits at a station.",
            "internal_summary": "Geheimes Ende", "world": "Eine andere Stadt",
            "characters": "Eine unbeteiligte Person", "spoilers": "Die Auflösung",
        }, duration_seconds=12,
    ))

    assert result.source_excerpt == chapter.source_text
    assert api.calls[0][1]["scene_source"]["focus_text"] == chapter.source_text
    assert api.calls[0][1]["scene_source"]["kind"] == "chapter"
    assert api.calls[0][1]["supporting_book_context"] == {
        "genre": "Roman", "mood": "Düster",
    }
    serialized = json.dumps(api.calls[0][1], ensure_ascii=False)
    for excluded in ("Geheimes Ende", "Eine andere Stadt", "Eine unbeteiligte Person", "Die Auflösung"):
        assert excluded not in serialized
    assert api.calls[0][3]["max_tokens"] == 3200
    assert "vollständigen Kapiteltext" in api.calls[0][0]

    class FabricatingAPI(ChapterAPI):
        async def complete_json(self, *args, **kwargs):
            value = await super().complete_json(*args, **kwargs)
            return value.model_copy(update={"source_excerpt": "Diese erfundene Passage steht nicht im Kapitel."})

    with pytest.raises(ChapterTeaserError, match="wortgetreu"):
        asyncio.run(analyze_whole_chapter(
            FabricatingAPI([chapter]), chapter=chapter,
            book_context={}, duration_seconds=12,
        ))


def test_worker_creates_one_planned_image_job_per_full_chapter(setup):
    settings, uploads, book, record, reels, track, store, run_id = prepare_run(setup)
    api = ChapterAPI(record.result.chapters)

    assert run_chapter_teaser_once(
        uploads, settings=settings, api_factory=lambda _: api,
    )

    run = store.latest(book.id, include_plans=True)
    assert run["id"] == run_id and run["state"] == "rendering"
    assert [call[1]["scene_source"]["focus_text"] for call in api.calls] == [
        chapter.source_text for chapter in record.result.chapters
    ]
    assert all(plan["state"] == "image_queued" for plan in run["plans"])
    assert all(plan["suggestion"] is not None for plan in run["plans"])

    drafts = reels.list_drafts(book.id)
    assert len(drafts) == len(record.result.chapters)
    assert all(draft.audio_track_id == track.id for draft in drafts)
    assert all(draft.caption_addition == "Etwas wartet in der Stille. Doch nicht mehr lange." for draft in drafts)
    jobs = ReelJobStore(reels)
    assert all(jobs.status(draft.id)[0]["kind"] == "image" for draft in drafts)


def test_unverifiable_scene_fails_only_its_chapter_and_analysis_continues(setup):
    settings, uploads, book, record, reels, _, store, _ = prepare_run(setup)

    class OneBadSceneAPI(ChapterAPI):
        async def complete_json(self, system, user, result_type, **kwargs):
            value = await super().complete_json(system, user, result_type, **kwargs)
            if json.loads(user)["chapter_metadata"]["position"] == 2:
                return value.model_copy(update={
                    "source_excerpt": "Diese erfundene Szene steht nicht im zweiten Kapitel."
                })
            return value

    api = OneBadSceneAPI(record.result.chapters)
    assert run_chapter_teaser_once(
        uploads, settings=settings, api_factory=lambda _: api,
    )

    run = store.latest(book.id, include_plans=True)
    assert len(api.calls) == len(record.result.chapters)
    assert run["state"] == "rendering" and run["active"]
    assert run["plans"][0]["state"] == "image_queued"
    assert run["plans"][1]["state"] == "failed"
    assert "wortgetreu" in run["plans"][1]["error"]
    assert len(reels.list_drafts(book.id)) == 1


def test_structured_provider_error_fails_only_its_chapter(setup):
    settings, uploads, book, record, reels, _, store, _ = prepare_run(setup)

    class OneBadSchemaAPI(ChapterAPI):
        async def complete_json(self, system, user, result_type, **kwargs):
            payload = json.loads(user)
            if payload["chapter_metadata"]["position"] == 1:
                raise OpenWebUIError("structured")
            return await super().complete_json(system, user, result_type, **kwargs)

    assert run_chapter_teaser_once(
        uploads, settings=settings,
        api_factory=lambda _: OneBadSchemaAPI(record.result.chapters),
    )

    run = store.latest(book.id, include_plans=True)
    assert run["state"] == "rendering"
    assert run["plans"][0]["state"] == "failed"
    assert run["plans"][1]["state"] == "image_queued"
    assert len(reels.list_drafts(book.id)) == 1


def test_all_unverifiable_scenes_finish_partial_and_can_be_retried(setup):
    settings, uploads, book, record, _, track, store, run_id = prepare_run(setup)

    class AllBadScenesAPI(ChapterAPI):
        async def complete_json(self, system, user, result_type, **kwargs):
            value = await super().complete_json(system, user, result_type, **kwargs)
            return value.model_copy(update={
                "source_excerpt": "Diese erfundene Szene steht in keinem Kapitel."
            })

    assert run_chapter_teaser_once(
        uploads, settings=settings,
        api_factory=lambda _: AllBadScenesAPI(record.result.chapters),
    )
    run = store.latest(book.id, include_plans=True)
    assert run["state"] == "partial" and not run["active"]
    assert run["completed"] == run["total"] == len(record.result.chapters)
    assert all(plan["state"] == "failed" for plan in run["plans"])

    assert store.enqueue(
        book.id, settings, audio_track_id=track.id, transition_ms=500,
        book_context={"title": book.title, "spoilers": "Das Ende bleibt intern."},
    ) == run_id
    retried = store.latest(book.id, include_plans=True)
    assert retried["state"] == "queued"
    assert all(plan["state"] == "pending" for plan in retried["plans"])


def test_pre_provider_openwebui_run_is_reused_on_retry(setup):
    settings, uploads, book, _, _, track, store, run_id = prepare_run(setup)
    context = {"title": book.title, "spoilers": "Das Ende bleibt intern."}
    with sqlite3.connect(uploads.db_path) as connection, connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "select * from local_chapter_teaser_runs where id=?", (run_id,),
        ).fetchone()
        track_sha = connection.execute(
            "select sha256 from local_audio_tracks where id=?", (track.id,),
        ).fetchone()[0]
        legacy = hashlib.sha256(json.dumps([
            row["extraction_revision"], settings.openwebui_model, endpoint_hash(settings),
            row["prompt_version"], track.id, track_sha, row["transition_ms"],
            row["duration_ms"], context,
        ], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        connection.execute(
            "update local_chapter_teaser_runs set fingerprint=?,endpoint_hash=?,state='failed' "
            "where id=?", (legacy, endpoint_hash(settings), run_id),
        )

    retried = store.enqueue(
        book.id, settings, audio_track_id=track.id, transition_ms=500,
        book_context=context, provider="openwebui",
    )
    assert retried == run_id
    assert store.latest(book.id)["state"] == "queued"


def test_completed_legacy_run_requeues_only_missing_text_variants(setup):
    settings, uploads, book, record, _, track, store, run_id = prepare_run(setup)
    with sqlite3.connect(uploads.db_path) as connection, connection:
        connection.execute(
            """update local_chapter_teaser_runs set state='done',stage='Altbestand',
            completed=total where id=?""", (run_id,),
        )
        connection.execute(
            "update local_chapter_teaser_plans set state='done' where run_id=?", (run_id,)
        )

    assert store.enqueue(
        book.id, settings, audio_track_id=track.id, transition_ms=500,
        book_context={"title": book.title, "spoilers": "Das Ende bleibt intern."},
    ) == run_id
    run = store.latest(book.id, include_plans=True)
    assert run["state"] == "rendering"
    assert run["completed"] == 0 and run["total"] == len(record.result.chapters)
    assert all(plan["state"] == "video_queued" for plan in run["plans"])


def test_media_reconciliation_chains_images_to_both_video_variants_and_completes_run(
    setup, monkeypatch,
):
    settings, uploads, book, record, reels, _, store, _ = prepare_run(setup)
    api = ChapterAPI(record.result.chapters)
    assert run_chapter_teaser_once(uploads, settings=settings, api_factory=lambda _: api)
    jobs = ReelJobStore(reels)

    for _ in record.result.chapters:
        job = jobs.claim(kinds={"image"})
        assert job is not None
        relative, digest = reels.save_artifact(job.draft_id, "image", png_bytes(), "scene.png")
        assert jobs.finish(job, result={
            "path": relative, "sha256": digest, "candidate": "scene",
            "character_snapshot": [],
        })

    assert reconcile_chapter_teaser_media(uploads, reels, jobs)
    image_checkpoint = store.latest(book.id, include_plans=True)
    assert image_checkpoint["state"] == "done" and image_checkpoint["images_ready"]
    assert all(plan["state"] == "analyzed" for plan in image_checkpoint["plans"])

    draft_ids = [plan["draft_id"] for plan in image_checkpoint["plans"]]
    for draft_id in draft_ids:
        jobs.enqueue(draft_id, "video")
    store.begin_videos(book.id, image_checkpoint["id"], draft_ids)

    for _ in record.result.chapters:
        job = jobs.claim(kinds={"video"})
        assert job is not None
        video = BytesIO(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32)
        relative, digest = reels.save_artifact(job.draft_id, "video", video, "reel.mp4")
        assert jobs.finish(job, result={"path": relative, "sha256": digest})

    caption_texts = []

    def fake_caption(source, text, output):
        caption_texts.append(text)
        output.write_bytes(b"\x00\x00\x00\x18ftypisom-captioned" + text.encode())
        return output

    monkeypatch.setattr(
        "bookpromo.chapter_teaser_worker.render_captioned_reel", fake_caption,
    )

    assert reconcile_chapter_teaser_media(uploads, reels, jobs)
    run = store.latest(book.id, include_plans=True)
    assert run["state"] == "done" and run["completed"] == run["total"]
    assert all(plan["state"] == "done" for plan in run["plans"])
    assert all(plan["text_video_path"] for plan in run["plans"])
    assert len(caption_texts) == len(record.result.chapters)
    assert all(draft.state == "ready" for draft in reels.list_drafts(book.id))
    with TestClient(
        create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000",
    ) as client:
        first = run["plans"][0]
        video = client.get(
            f"/books/local/{book.id}/teaser/chapters/{first['chapter_id']}/video.mp4"
        )
        assert video.status_code == 200 and video.headers["content-type"] == "video/mp4"
        download = client.get(
            f"/books/local/{book.id}/teaser/chapters/{first['chapter_id']}/video.mp4?download=true"
        )
        assert "attachment" in download.headers["content-disposition"]
        text_video = client.get(
            f"/books/local/{book.id}/teaser/chapters/{first['chapter_id']}/video.mp4?variant=text"
        )
        assert text_video.status_code == 200 and b"captioned" in text_video.content
        text_download = client.get(
            f"/books/local/{book.id}/teaser/chapters/{first['chapter_id']}/video.mp4"
            "?variant=text&download=true"
        )
        assert "mit-text" in text_download.headers["content-disposition"]
        assert f"/teaser/chapters/{first['chapter_id']}/queue" in client.get(
            f"/books/local/{book.id}/teaser"
        ).text

    draft = reels.get_draft(first["draft_id"])
    with pytest.raises(UploadError, match="zwischenzeitlich"):
        store.select_image(book.id, run["id"], draft.id, draft.revision - 1, "scene")
    unchanged = store.latest(book.id, include_plans=True)
    assert unchanged["plans"][0]["state"] == "done"
    assert unchanged["plans"][0]["text_video_path"] == first["text_video_path"]
    store.select_image(book.id, run["id"], draft.id, draft.revision, "scene")
    assert store.latest(book.id, include_plans=True)["plans"][0]["state"] == "done"

    store.reopen_image_review(book.id, run["id"], [first["draft_id"]])
    reopened = store.latest(book.id, include_plans=True)
    assert reopened["images_ready"] and reopened["plans"][0]["state"] == "analyzed"
    assert reopened["plans"][1]["state"] == "done"
    assert reopened["plans"][0]["text_video_path"] is None
    jobs.enqueue(first["draft_id"], "video")
    store.begin_videos(book.id, reopened["id"], [first["draft_id"]])
    mixed = store.latest(book.id, include_plans=True)
    assert [plan["state"] for plan in mixed["plans"]] == ["video_queued", "done"]
    assert reels.get_draft(first["draft_id"]).video_stale
    assert not reconcile_chapter_teaser_media(uploads, reels, jobs)
    assert store.latest(book.id, include_plans=True)["plans"][0]["state"] == "video_queued"
    assert len(caption_texts) == 2  # No captions generated from the old clean clip.
    store.reopen_image_review(book.id, run["id"], [run["plans"][1]["draft_id"]])
    assert store.latest(book.id)["state"] == "rendering"


def test_chapter_images_pause_for_bulk_character_optimization_selection_and_video_start(
    setup,
):
    settings, uploads, book, record, reels, _, store, _ = prepare_run(setup)
    settings.reel_reference_workflow.parent.mkdir(parents=True, exist_ok=True)
    settings.reel_reference_workflow.write_text("{}", encoding="utf-8")
    settings.reel_video_workflow.write_text("{}", encoding="utf-8")
    character_store = CharacterStore(uploads)
    character = character_store.create(
        book.id, name="Niemand", description="Eine erwachsene Person mit markantem Gesicht.",
        image_prompt="Adult person with a distinctive angular face and short dark hair.",
    )
    character_store.save_reference_file(
        book.id, character.id, character.revision, image_file("#334455"),
    )

    assert run_chapter_teaser_once(
        uploads, settings=settings, api_factory=lambda _: ChapterAPI(record.result.chapters),
    )
    jobs = ReelJobStore(reels)
    for _ in record.result.chapters:
        job = jobs.claim(kinds={"image"})
        relative, digest = reels.save_artifact(job.draft_id, "image", png_bytes(), "scene.png")
        assert jobs.finish(job, result={
            "path": relative, "sha256": digest, "candidate": "scene",
            "character_snapshot": [],
        })
    assert reconcile_chapter_teaser_media(uploads, reels, jobs)
    checkpoint = store.latest(book.id, include_plans=True)
    assert checkpoint["images_ready"] and not checkpoint["videos_ready"]
    assert not any(jobs.claim(kinds={"video"}) for _ in range(1))

    base = f"/books/local/{book.id}/teaser"
    with TestClient(
        create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000",
    ) as client:
        page = client.get(base)
        assert page.status_code == 200
        assert "Charaktere für alle Kapitel optimieren (1)" in page.text
        assert "Videos aus ausgewählten Bildern erzeugen" in page.text
        assert "Dieses Bild verwenden" not in page.text  # only the selected scene exists so far

        optimize = client.post(base + "/chapters/images/optimize", follow_redirects=False)
        assert optimize.status_code == 303
        optimization = jobs.claim(kinds={"image"})
        assert optimization is not None and optimization.payload == {"operation": "optimize"}
        assert client.post(base + "/chapters/videos/start").status_code == 409
        current = reels.get_draft(optimization.draft_id)
        assert client.post(
            base + f"/chapters/{checkpoint['plans'][0]['chapter_id']}/image/select",
            data={"revision": str(current.revision), "source": "scene"},
        ).status_code == 409
        relative, digest = reels.save_artifact(
            optimization.draft_id, "image", image_file("#556677"), "optimized.png",
        )
        assert jobs.finish(optimization, result={
            "path": relative, "sha256": digest, "candidate": "optimized",
            "character_snapshot": [],
        })

        plan = store.latest(book.id, include_plans=True)["plans"][0]
        draft = reels.get_draft(plan["draft_id"])
        assert draft.selected_image_source == "scene"  # Optimization never silently selects itself.
        page = client.get(base)
        assert "Szenenbild" in page.text and "Charakteroptimiert" in page.text
        image = client.get(
            base + f"/chapters/{plan['chapter_id']}/image.bin?source=optimized"
        )
        assert image.status_code == 200 and image.content.startswith(b"\x89PNG")
        selected = client.post(
            base + f"/chapters/{plan['chapter_id']}/image/select",
            data={"revision": str(draft.revision), "source": "optimized"},
            follow_redirects=False,
        )
        assert selected.status_code == 303
        assert reels.get_draft(draft.id).selected_image_source == "optimized"

        videos = client.post(base + "/chapters/videos/start", follow_redirects=False)
        assert videos.status_code == 303
        running = store.latest(book.id, include_plans=True)
        assert running["state"] == "rendering"
        assert all(plan["state"] == "video_queued" for plan in running["plans"])
        assert sum(jobs.claim(kinds={"video"}) is not None for _ in record.result.chapters) == 2


def test_video_start_rolls_back_all_jobs_if_a_chapter_is_not_ready(setup):
    settings, uploads, book, record, reels, _, store, _ = prepare_run(setup)
    assert run_chapter_teaser_once(
        uploads, settings=settings, api_factory=lambda _: ChapterAPI(record.result.chapters),
    )
    jobs = ReelJobStore(reels)
    for _ in record.result.chapters:
        job = jobs.claim(kinds={"image"})
        relative, digest = reels.save_artifact(job.draft_id, "image", png_bytes(), "scene.png")
        assert jobs.finish(job, result={"path": relative, "sha256": digest, "candidate": "scene"})
    assert reconcile_chapter_teaser_media(uploads, reels, jobs)
    run = store.latest(book.id, include_plans=True)
    ids = [plan["draft_id"] for plan in run["plans"]]
    with sqlite3.connect(uploads.db_path) as connection, connection:
        connection.execute("update local_reel_drafts set video_prompt='' where id=?", (ids[1],))
    with pytest.raises(UploadError, match="Videoprompt"):
        store.begin_videos(book.id, run["id"], ids)
    unchanged = store.latest(book.id, include_plans=True)
    assert unchanged["state"] == "done" and unchanged["images_ready"]
    assert all(plan["state"] == "analyzed" for plan in unchanged["plans"])
    assert jobs.claim(kinds={"video"}) is None


def ready_chapter_images(setup, *, with_reference=False):
    settings, uploads, book, record, reels, _, store, _ = prepare_run(setup)
    settings.reel_image_workflow.parent.mkdir(parents=True, exist_ok=True)
    for workflow in (
        settings.reel_image_workflow, settings.reel_reference_workflow,
        settings.reel_video_workflow,
    ):
        workflow.write_text("{}", encoding="utf-8")
    if with_reference:
        characters = CharacterStore(uploads)
        character = characters.create(
            book.id, name="Niemand", description="Eine erwachsene Person.",
            image_prompt="Adult person with short dark hair and an angular face.",
        )
        characters.save_reference_file(book.id, character.id, character.revision, image_file("#224455"))
    assert run_chapter_teaser_once(
        uploads, settings=settings, api_factory=lambda _: ChapterAPI(record.result.chapters),
    )
    jobs = ReelJobStore(reels)
    for _ in record.result.chapters:
        job = jobs.claim(kinds={"image"})
        relative, digest = reels.save_artifact(job.draft_id, "image", png_bytes(), "scene.png")
        assert jobs.finish(job, result={"path": relative, "sha256": digest, "candidate": "scene"})
    assert reconcile_chapter_teaser_media(uploads, reels, jobs)
    return settings, uploads, book, record, reels, store, jobs, store.latest(book.id, include_plans=True)


@pytest.mark.parametrize("changed_field", ["image", "video", "text"])
def test_chapter_prompt_edit_is_atomic_manual_and_invalidates_only_affected_media(setup, changed_field):
    _, uploads, book, _, reels, store, jobs, run = ready_chapter_images(setup)
    first = run["plans"][0]
    draft = reels.get_draft(first["draft_id"])
    jobs.enqueue(draft.id, "video")
    job = jobs.claim(kinds={"video"})
    relative, digest = reels.save_artifact(draft.id, "video", BytesIO(b"\x00\x00\x00\x18ftypisom-clean-clip"), "clean.mp4")
    assert jobs.finish(job, result={"path": relative, "sha256": digest})
    text_path, text_hash = reels.save_artifact(draft.id, "video", BytesIO(b"\x00\x00\x00\x18ftypisom-old-caption"), "text.mp4")
    store.save_text_video(run["id"], draft.id, text_path, text_hash)
    store.set_plan_state(run["id"], draft.id, "done")
    draft = reels.get_draft(draft.id)
    original_image = reels.artifact_path(draft, "image")
    prompt = "Adult Samuel lies in his bed inside the fortress, no street, no text."
    motion = "The camera tracks across the existing bed while light changes over the room."
    changes = {
        "image_prompt": prompt if changed_field == "image" else draft.image_prompt,
        "teaser_text": "Ein neuer Teasertext." if changed_field == "text" else draft.caption_addition,
        "video_prompt": motion if changed_field == "video" else draft.video_prompt,
    }
    with pytest.raises(UploadError, match="zwischenzeitlich"):
        store.save_prompt(book.id, run["id"], draft.id, draft.revision - 1, **changes)
    assert store.latest(book.id, include_plans=True)["plans"][0]["text_video_path"] == text_path
    store.save_prompt(book.id, run["id"], draft.id, draft.revision, **changes)
    saved = reels.get_draft(draft.id)
    assert saved.revision == draft.revision + 1
    assert saved.image_stale == (changed_field == "image")
    assert saved.video_stale == (changed_field != "text")
    assert saved.selected_video_path == relative
    assert original_image.is_file() and (uploads.root / text_path).is_file()
    plan = store.latest(book.id, include_plans=True)["plans"][0]
    assert plan["state"] == "analyzed" and plan["text_video_path"] is None
    assert plan["suggestion"].image_prompt == saved.image_prompt
    assert plan["suggestion"].teaser_text == saved.caption_addition
    assert saved.caption_addition in saved.final_caption
    assert jobs.claim(kinds={"image", "video", "prompt"}) is None
    assert not reconcile_chapter_teaser_media(uploads, reels, jobs)
    store.save_prompt(book.id, run["id"], draft.id, saved.revision, **changes)
    assert reels.get_draft(draft.id).revision == saved.revision  # No-op does not invalidate again.


@pytest.mark.parametrize("bulk", [False, True])
def test_edited_caption_is_rendered_without_regenerating_the_clean_chapter_clip(setup, monkeypatch, bulk):
    _, uploads, book, _, reels, store, jobs, run = ready_chapter_images(setup)
    draft = reels.get_draft(run["plans"][0]["draft_id"])
    jobs.enqueue(draft.id, "video")
    job = jobs.claim(kinds={"video"})
    relative, digest = reels.save_artifact(draft.id, "video", BytesIO(b"\x00\x00\x00\x18ftypisom-clean-clip"), "clean.mp4")
    assert jobs.finish(job, result={"path": relative, "sha256": digest})
    # Mark another chapter done so a bulk operation needs only this edited text.
    store.set_plan_state(run["id"], run["plans"][1]["draft_id"], "done")
    draft = reels.get_draft(draft.id)
    store.save_prompt(book.id, run["id"], draft.id, draft.revision,
                      image_prompt=draft.image_prompt, teaser_text="Nur dieser neue Text soll erscheinen.")
    if bulk:
        store.begin_videos(book.id, run["id"], [draft.id])
    else:
        store.start_video(book.id, run["id"], draft.id, revision=draft.revision + 1)
    assert jobs.claim(kinds={"video"}) is None
    rendered = []

    def fake_caption(source, text, output):
        rendered.append(text)
        output.write_bytes(b"\x00\x00\x00\x18ftypisom-captioned-clip")

    monkeypatch.setattr("bookpromo.chapter_teaser_worker.render_captioned_reel", fake_caption)
    assert reconcile_chapter_teaser_media(uploads, reels, jobs)
    assert rendered == ["Nur dieser neue Text soll erscheinen."]
    saved = reels.get_draft(draft.id)
    assert saved.selected_video_path == relative and saved.selected_video_sha256 == digest
    assert not saved.video_stale
    assert store.latest(book.id, include_plans=True)["plans"][0]["state"] == "done"


@pytest.mark.parametrize("plan_state", ["analyzed", "failed", "done"])
def test_single_chapter_video_start_and_regeneration_preserves_other_chapters(setup, plan_state):
    _, uploads, book, _, reels, store, jobs, run = ready_chapter_images(setup)
    first = run["plans"][0]
    store.set_plan_state(run["id"], first["draft_id"], plan_state)
    draft = reels.get_draft(first["draft_id"])
    with pytest.raises(UploadError, match="zwischenzeitlich"):
        store.start_video(book.id, run["id"], draft.id, revision=draft.revision - 1)
    assert jobs.claim(kinds={"video"}) is None
    store.start_video(book.id, run["id"], draft.id, revision=draft.revision)
    started = store.latest(book.id, include_plans=True)
    assert started["state"] == "rendering"
    assert [plan["state"] for plan in started["plans"]] == ["video_queued", "analyzed"]
    job = jobs.claim(kinds={"video"})
    assert job.draft_id == draft.id
    assert jobs.claim(kinds={"video"}) is None
    with pytest.raises(UploadError, match="bearbeitet|Verarbeitungsschritt"):
        store.save_prompt(book.id, run["id"], draft.id, draft.revision,
                          image_prompt=draft.image_prompt, teaser_text="Anderer Text.")


def test_chapter_prompt_and_single_video_routes_accept_revision_and_reject_stale_posts(setup):
    settings, _, book, _, reels, store, jobs, run = ready_chapter_images(setup)
    first = run["plans"][0]
    draft = reels.get_draft(first["draft_id"])
    chapter = f"/books/local/{book.id}/teaser/chapters/{first['chapter_id']}"
    data = {"revision": str(draft.revision), "image_prompt": draft.image_prompt,
            "teaser_text": "Ein neuer Text für dieses Kapitel."}
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        assert client.post(chapter + "/prompt", data=data, follow_redirects=False).status_code == 303
        assert client.post(chapter + "/prompt", data=data, follow_redirects=False).status_code == 409
        assert client.post(chapter + "/video/start", data={"revision": draft.revision},
                           follow_redirects=False).status_code == 409
        assert client.post(chapter + "/video/start", data={"revision": draft.revision + 1},
                           follow_redirects=False).status_code == 303
    job = jobs.claim(kinds={"video"})
    assert job.draft_id == draft.id and job.input_revision == draft.revision + 1


def test_edited_image_prompt_remains_manual_when_another_chapter_video_runs(setup):
    _, uploads, book, _, reels, store, jobs, run = ready_chapter_images(setup)
    first, second = run["plans"]
    draft = reels.get_draft(first["draft_id"])
    store.save_prompt(book.id, run["id"], draft.id, draft.revision,
                      image_prompt="A new chapter scene inside a bedroom.", teaser_text=draft.caption_addition)
    store.start_video(book.id, run["id"], second["draft_id"])
    assert not reconcile_chapter_teaser_media(uploads, reels, jobs)
    assert jobs.claim(kinds={"image"}) is None
    assert store.latest(book.id, include_plans=True)["plans"][0]["state"] == "analyzed"


def test_done_chapter_start_explicitly_regenerates_even_with_current_clean_video(setup):
    _, uploads, book, _, reels, store, jobs, run = ready_chapter_images(setup)
    first = run["plans"][0]
    draft = reels.get_draft(first["draft_id"])
    jobs.enqueue(draft.id, "video")
    previous = jobs.claim(kinds={"video"})
    relative, digest = reels.save_artifact(draft.id, "video", BytesIO(b"\x00\x00\x00\x18ftypisom-existing"), "old.mp4")
    assert jobs.finish(previous, result={"path": relative, "sha256": digest})
    store.set_plan_state(run["id"], draft.id, "done")
    store.start_video(book.id, run["id"], draft.id)
    replacement = jobs.claim(kinds={"video"})
    assert replacement.id != previous.id and replacement.draft_id == draft.id
    assert reels.get_draft(draft.id).video_stale
    assert not reconcile_chapter_teaser_media(uploads, reels, jobs)


@pytest.mark.parametrize("second_state", ["analyzed", "failed"])
def test_bulk_video_retries_failed_chapters_with_images_and_corrects_progress(setup, second_state):
    settings, uploads, book, _, _, store, jobs, run = ready_chapter_images(setup)
    first, second = run["plans"]
    store.set_plan_state(run["id"], first["draft_id"], "failed", "Video failed.")
    store.set_plan_state(run["id"], second["draft_id"], second_state)
    with sqlite3.connect(uploads.db_path) as connection, connection:
        connection.execute("update local_chapter_teaser_runs set state='partial' where id=?", (run["id"],))
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        response = client.post(f"/books/local/{book.id}/teaser/chapters/videos/start", follow_redirects=False)
        assert response.status_code == 303
    started = store.latest(book.id, include_plans=True)
    assert started["completed"] == 0
    assert [plan["state"] for plan in started["plans"]] == ["video_queued", "video_queued"]
    assert {jobs.claim(kinds={"video"}).draft_id for _ in range(2)} == {first["draft_id"], second["draft_id"]}


@pytest.mark.parametrize("plan_state", ["analyzed", "done", "failed"])
def test_chapter_image_can_regenerate_from_review_finished_or_failed_state(setup, plan_state):
    settings, uploads, book, _, reels, store, jobs, run = ready_chapter_images(setup)
    first = run["plans"][0]
    store.set_plan_state(run["id"], first["draft_id"], plan_state)
    if plan_state == "failed":
        with sqlite3.connect(uploads.db_path) as connection, connection:
            connection.execute("update local_chapter_teaser_runs set state='partial' where id=?", (run["id"],))
    original = reels.artifact_path(reels.get_draft(first["draft_id"]), "image")
    base = f"/books/local/{book.id}/teaser"
    route = base + f"/chapters/{first['chapter_id']}/image/regenerate"
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        page = client.get(base)
        assert route in page.text and "Bild neu erzeugen" in page.text
        assert client.post(route, follow_redirects=False).status_code == 303
        assert client.post(route).status_code == 409
    running = store.latest(book.id, include_plans=True)
    assert running["state"] == "rendering" and running["plans"][0]["state"] == "image_queued"
    assert running["plans"][1]["state"] == "analyzed"
    assert reels.get_draft(first["draft_id"]).image_stale
    assert not reconcile_chapter_teaser_media(uploads, reels, jobs)
    job = jobs.claim(kinds={"image"})
    assert job.draft_id == first["draft_id"] and job.payload == {"operation": "scene"}
    relative, digest = reels.save_artifact(job.draft_id, "image", image_file("#668899"), "replacement.png")
    assert jobs.finish(job, result={"path": relative, "sha256": digest, "candidate": "scene"})
    assert reconcile_chapter_teaser_media(uploads, reels, jobs)
    ready = store.latest(book.id, include_plans=True)
    assert ready["images_ready"] and ready["plans"][0]["state"] == "analyzed"
    assert ready["plans"][0]["suggestion"] == first["suggestion"]
    assert original.is_file()  # Old artifacts remain recoverable.
    assert jobs.claim(kinds={"video"}) is None


@pytest.mark.parametrize("operation", ["optimize", "video_retry"])
def test_failed_chapters_keep_character_and_video_retry_actions(setup, operation, monkeypatch):
    settings, uploads, book, _, reels, store, jobs, run = ready_chapter_images(setup, with_reference=True)
    first = run["plans"][0]
    store.set_plan_state(run["id"], first["draft_id"], "failed", "ComfyUI war nicht erreichbar.")
    with sqlite3.connect(uploads.db_path) as connection, connection:
        connection.execute("update local_chapter_teaser_runs set state='partial' where id=?", (run["id"],))
    base = f"/books/local/{book.id}/teaser"
    chapter = base + f"/chapters/{first['chapter_id']}"
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        page = client.get(base)
        assert chapter + "/image/optimize" in page.text
        assert chapter + "/image/regenerate" in page.text
        assert chapter + "/video/start" in page.text
        endpoint = "/image/optimize" if operation == "optimize" else "/video/retry"
        assert client.post(chapter + endpoint, follow_redirects=False).status_code == 303
    if operation == "optimize":
        job = jobs.claim(kinds={"image"})
        assert job.draft_id == first["draft_id"] and job.payload == {"operation": "optimize"}
        assert store.latest(book.id, include_plans=True)["plans"][0]["state"] == "analyzed"
    else:
        job = jobs.claim(kinds={"video"})
        assert job.draft_id == first["draft_id"]
        assert jobs.claim(kinds={"video"}) is None  # Retry is chapter-specific.
        relative, digest = reels.save_artifact(job.draft_id, "video", BytesIO(b"\x00\x00\x00\x18ftypisom-test"), "reel.mp4")
        assert jobs.finish(job, result={"path": relative, "sha256": digest})

        def fake_caption(source, text, output):
            output.write_bytes(b"\x00\x00\x00\x18ftypisom-text")

        monkeypatch.setattr("bookpromo.chapter_teaser_worker.render_captioned_reel", fake_caption)
        assert reconcile_chapter_teaser_media(uploads, reels, jobs)
        finished = store.latest(book.id, include_plans=True)
        assert not finished["active"] and finished["plans"][0]["state"] == "done"
        assert finished["plans"][1]["state"] == "analyzed"


def test_manual_character_optimization_when_analysis_selected_no_characters(setup):
    settings, uploads, book, _, reels, store, jobs, run = ready_chapter_images(setup, with_reference=True)
    first = run["plans"][0]
    with sqlite3.connect(uploads.db_path) as connection, connection:
        connection.execute("update local_reel_drafts set character_ids_json='[]' where id=?", (first["draft_id"],))
    draft = reels.get_draft(first["draft_id"])
    character = CharacterStore(uploads).list(book.id)[0]
    base = f"/books/local/{book.id}/teaser"
    route = base + f"/chapters/{first['chapter_id']}/image/optimize"
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        page = client.get(base)
        assert route in page.text and 'name="character_id"' in page.text
        assert "Charaktere optimieren" in page.text
        assert client.post(route, data={"revision": str(draft.revision)}).status_code == 400
        assert client.post(route, data={
            "revision": str(draft.revision), "character_id": "00000000-0000-4000-8000-000000000001",
        }).status_code == 400
        assert client.post(route, data={
            "revision": str(draft.revision - 1), "character_id": character.id,
        }).status_code == 409
        assert jobs.claim(kinds={"image"}) is None
        assert reels.get_draft(draft.id).character_ids == ()
        assert client.post(route, data={
            "revision": str(draft.revision), "character_id": character.id,
        }, follow_redirects=False).status_code == 303
    current = reels.get_draft(draft.id)
    assert current.character_ids == (character.id,)
    assert not current.image_stale and current.selected_image_path == draft.selected_image_path
    job = jobs.claim(kinds={"image"})
    assert job.payload == {"operation": "optimize", "character_ids": [character.id]}
    assert job.input_revision == current.revision == draft.revision + 1


def test_finished_chapter_reel_can_queue_text_daily_and_clean_scheduled(setup, monkeypatch):
    settings, uploads, book, record, reels, _, store, _ = prepare_run(setup)
    assert run_chapter_teaser_once(
        uploads, settings=settings, api_factory=lambda _: ChapterAPI(record.result.chapters),
    )
    jobs = ReelJobStore(reels)
    for _ in record.result.chapters:
        job = jobs.claim(kinds={"image"})
        relative, digest = reels.save_artifact(job.draft_id, "image", png_bytes(), "scene.png")
        assert jobs.finish(job, result={"path": relative, "sha256": digest, "candidate": "scene"})
    assert reconcile_chapter_teaser_media(uploads, reels, jobs)
    image_checkpoint = store.latest(book.id, include_plans=True)
    draft_ids = [plan["draft_id"] for plan in image_checkpoint["plans"]]
    for draft_id in draft_ids:
        jobs.enqueue(draft_id, "video")
    store.begin_videos(book.id, image_checkpoint["id"], draft_ids)
    for _ in record.result.chapters:
        job = jobs.claim(kinds={"video"})
        video = BytesIO(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32)
        relative, digest = reels.save_artifact(job.draft_id, "video", video, "reel.mp4")
        assert jobs.finish(job, result={"path": relative, "sha256": digest})

    def fake_caption(source, text, output):
        output.write_bytes(b"\x00\x00\x00\x18ftypisom-captioned" + text.encode())
        return output

    monkeypatch.setattr("bookpromo.chapter_teaser_worker.render_captioned_reel", fake_caption)
    assert reconcile_chapter_teaser_media(uploads, reels, jobs)
    run = store.latest(book.id, include_plans=True)
    first = run["plans"][0]
    draft = reels.get_draft(first["draft_id"])
    reels.save_publication_defaults(0, PublicationDefaults(
        instagram=PlatformDefault(enabled=True, account_id="ig-chapter-test"),
    ))
    SyncStore(uploads).save_receipt(book.id, {"revision": 1, "hash": "synced"})

    class Repository:
        def __init__(self):
            self.uploads = []
            self.enqueued = []

        async def check_schema(self, **kwargs):
            return None

        async def upload_reel(self, object_path, data):
            self.uploads.append((object_path, data))
            return object_path

        async def enqueue_reel(self, asset, publications):
            self.enqueued.append((asset, publications))
            return {"asset": asset, "publications": publications, "outcome": "enqueued"}

    repository = Repository()
    url = f"/books/local/{book.id}/teaser/chapters/{first['chapter_id']}/queue"
    common = {
        "title": f"{book.title} · Kapitel 1",
        "description": draft.final_caption,
        "platform": "instagram",
    }
    with TestClient(
        create_app(settings, repository=repository, start_worker=False),
        base_url="http://127.0.0.1:8000",
    ) as client:
        daily = client.post(url, data={
            **common, "variant": "text", "queue_mode": "daily",
            "scheduled_for": "", "requeue": "0",
        }, follow_redirects=False)
        assert daily.status_code == 303 and daily.headers["location"].endswith("?queued=1")
        text_asset, text_publications = repository.enqueued[0]
        assert text_asset["source_kind"] == "chapter"
        assert text_asset["quote_id"] is None
        assert text_asset["chapter_id"] == first["chapter_id"]
        assert text_asset["media_sha256"] == first["text_video_sha256"]
        assert text_publications[0]["queue_mode"] == "daily"

        scheduled = client.post(url, data={
            **common, "variant": "clean", "queue_mode": "scheduled",
            "scheduled_for": "2099-01-01T12:00", "requeue": "1",
        }, follow_redirects=False)
        assert scheduled.status_code == 303
        clean_asset, clean_publications = repository.enqueued[1]
        assert clean_asset["id"] != text_asset["id"]
        assert clean_asset["media_sha256"] == draft.selected_video_sha256
        assert clean_publications[0]["queue_mode"] == "scheduled"
        assert clean_publications[0]["scheduled_for"].startswith("2099-01-01T12:00")


def test_teaser_page_starts_durable_chapter_production_and_exposes_status(setup):
    settings, uploads, book, _, _ = setup
    settings.reel_image_workflow.parent.mkdir(parents=True, exist_ok=True)
    settings.reel_image_workflow.write_text("{}", encoding="utf-8")
    settings.reel_video_workflow.write_text("{}", encoding="utf-8")
    reels = ReelStore(uploads, max_audio_bytes=1024 * 1024)
    track, _ = reels.save_audio(book.id, wav_bytes(30), "song.wav", title="Song")

    with TestClient(
        create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000",
    ) as client:
        page = client.get(f"/books/local/{book.id}/teaser")
        assert page.status_code == 200
        assert "vollständige Kapiteltext" in page.text
        started = client.post(
            f"/books/local/{book.id}/teaser/chapters/start",
            data={
                "audio_track_id": track.id, "transition_seconds": "0.5",
                "ai_provider": "openwebui",
            },
            follow_redirects=False,
        )
        assert started.status_code == 303
        status = client.get(f"/books/local/{book.id}/teaser/chapters/status").json()
        assert status["state"] == "queued" and status["active"]
        assert status["total"] == 2


def test_delete_is_blocked_while_chapter_reels_are_active(setup):
    settings, uploads, book, _, _ = setup
    reels = ReelStore(uploads, max_audio_bytes=1024 * 1024)
    track, _ = reels.save_audio(book.id, wav_bytes(30), "song.wav")
    ChapterTeaserStore(uploads).enqueue(
        book.id, settings, audio_track_id=track.id, transition_ms=500, book_context={},
    )

    with pytest.raises(UploadError, match="Kapitel-Reel-Produktion"):
        uploads.delete_book(book.id)

    with sqlite3.connect(uploads.db_path) as connection, connection:
        connection.execute(
            "update local_chapter_teaser_runs set state='failed' where book_id=?", (book.id,)
        )
    uploads.delete_book(book.id)
    with sqlite3.connect(uploads.db_path) as connection:
        assert connection.execute(
            "select count(*) from local_chapter_teaser_runs where book_id=?", (book.id,)
        ).fetchone()[0] == 0
