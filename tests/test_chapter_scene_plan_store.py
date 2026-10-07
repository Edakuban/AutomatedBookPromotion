import asyncio
import json

import pytest

from bookpromo.chapter_teasers import (
    ChapterTeaserError, ChapterTeaserSuggestion, analyze_whole_chapter,
)
from bookpromo.characters import CharacterStore
from bookpromo.management import BookDetails, ManagementStore
from bookpromo.scene_plan import ScenePlan, encode_saved_plan
from bookpromo.uploads import UploadError
from test_analysis import setup
from test_chapter_teasers import ChapterAPI, ready_chapter_images, suggestion
from test_characters import image_file


def plan_for_draft(store, draft):
    with store.connection() as connection:
        row = connection.execute("select * from local_reel_drafts where id=?", (draft.id,)).fetchone()
        fingerprint, references = store.scene_plan_inputs(connection, row)
    plan = ScenePlan(
        setting="Documented library", composition="Vertical wide shot", art_direction="Watercolor",
        actors=[{"name": ref.name, "pose": "Looks towards the window", "free_parts": ["left hand"]}
                for ref in references],
    )
    return encode_saved_plan(plan, fingerprint)


def test_legacy_chapter_suggestion_remains_readable_but_new_generation_requires_plan(setup):
    chapter = setup[3].result.chapters[0]
    legacy = suggestion(chapter).model_dump()
    legacy.pop("scene_plan")
    assert ChapterTeaserSuggestion.model_validate(legacy).scene_plan is None

    class LegacyAPI(ChapterAPI):
        async def complete_json(self, *args, **kwargs):
            await super().complete_json(*args, **kwargs)
            return ChapterTeaserSuggestion.model_validate(legacy)

    api = LegacyAPI([chapter])
    with pytest.raises(ChapterTeaserError, match="Szenenplan"):
        asyncio.run(analyze_whole_chapter(api, chapter=chapter, book_context={}, duration_seconds=12))
    assert len(api.calls) == 1


def test_chapter_generation_projects_only_name_and_alias_identity_labels(setup):
    chapter = setup[3].result.chapters[0]
    api = ChapterAPI([chapter])
    asyncio.run(analyze_whole_chapter(
        api, chapter=chapter, book_context={}, duration_seconds=12,
        characters=[{"name": "Niemand", "aliases": ["N."], "description": "Do not transmit clothing",
                     "reference_image_path": "Do not transmit path", "image_prompt": "Do not transmit weapons"}],
    ))
    assert api.calls[0][1]["identity_labels"] == [{"name": "Niemand", "aliases": ["N."]}]
    assert "Do not transmit" not in json.dumps(api.calls[0][1])


@pytest.mark.parametrize("change", [None, "revision", "busy", "style", "source"])
def test_scene_plan_preflight_is_read_only_and_rejects_changed_inputs(setup, monkeypatch, change):
    _, _, book, _, reels, store, jobs, run = ready_chapter_images(setup)
    draft = reels.get_draft(run["plans"][0]["draft_id"])
    with store.connection() as connection:
        row = connection.execute("select * from local_reel_drafts where id=?", (draft.id,)).fetchone()
        fingerprint, _ = store.scene_plan_inputs(connection, row)
    if change == "revision":
        reels.update_draft(draft.id, draft.revision, scene_direction="Changed in another tab")
    elif change == "busy":
        jobs.enqueue(draft.id, "image")
    elif change == "style":
        with store.connection() as connection, connection:
            ManagementStore.schema(connection)
            connection.execute("insert into local_book_settings values(?,1,?,0)",
                               (book.id, BookDetails(title=book.title, image_prompt_base="Ink").model_dump_json()))
    elif change == "source":
        with store.connection() as connection, connection:
            connection.execute("update local_extractions set revision=revision+1 where book_id=?", (book.id,))
    before_drafts = reels.list_drafts(book.id)
    before_run = store.latest(book.id, include_plans=True)
    before_jobs = jobs.status(draft.id)
    monkeypatch.setattr(store, "schema", lambda *args: pytest.fail("Preflight must never migrate storage"))
    if change is None:
        assert store.check_scene_plan_inputs(book.id, run["id"], draft.id, draft.revision, fingerprint) is None
    else:
        with pytest.raises(UploadError) as conflict:
            store.check_scene_plan_inputs(book.id, run["id"], draft.id, draft.revision, fingerprint)
        assert conflict.value.status == 409
    assert reels.list_drafts(book.id) == before_drafts
    assert store.latest(book.id, include_plans=True) == before_run
    assert jobs.status(draft.id) == before_jobs


def test_scene_plan_preflight_reads_legacy_draft_without_migrating(setup):
    _, _, book, _, reels, store, _, run = ready_chapter_images(setup)
    draft = reels.get_draft(run["plans"][0]["draft_id"])
    with store.connection() as connection, connection:
        connection.execute("alter table local_reel_drafts drop column scene_direction")
        connection.execute("alter table local_reel_drafts drop column scene_plan_json")
        row = connection.execute("select * from local_reel_drafts where id=?", (draft.id,)).fetchone()
        fingerprint, _ = store.scene_plan_inputs(connection, row)
    before = reels.get_draft(draft.id)
    store.check_scene_plan_inputs(book.id, run["id"], draft.id, draft.revision, fingerprint)
    assert reels.get_draft(draft.id) == before
    with store.connection() as connection:
        columns = {row[1] for row in connection.execute("pragma table_info(local_reel_drafts)")}
    assert "scene_direction" not in columns and "scene_plan_json" not in columns


def test_chapter_save_plan_is_revision_fenced_and_preserves_current_images_and_state(setup):
    _, _, book, _, reels, store, jobs, run = ready_chapter_images(setup)
    before = reels.get_draft(run["plans"][0]["draft_id"])
    plan_json = plan_for_draft(store, before)
    original_jobs = jobs.status(before.id)
    saved = store.save_scene_plan(book.id, run["id"], before.id, before.revision, plan_json)
    assert saved.revision == before.revision + 1
    assert saved.scene_plan_json == plan_json
    for field in ("scene_image_path", "scene_image_sha256", "selected_image_path", "selected_image_sha256",
                  "optimized_image_path", "optimized_image_sha256", "selected_image_source", "image_stale",
                  "video_stale", "selected_video_path", "selected_video_sha256", "state"):
        assert getattr(saved, field) == getattr(before, field)
    assert jobs.status(before.id) == original_jobs
    with pytest.raises(UploadError) as conflict:
        store.save_scene_plan(book.id, run["id"], before.id, before.revision, plan_json)
    assert conflict.value.status == 409
    assert reels.get_draft(before.id) == saved


@pytest.mark.parametrize("running", [False, True], ids=["queued", "running"])
def test_chapter_save_plan_rejects_job_queued_while_ai_was_awaited(setup, running):
    _, _, book, _, reels, store, jobs, run = ready_chapter_images(setup)
    before = reels.get_draft(run["plans"][0]["draft_id"])
    plan_json = plan_for_draft(store, before)
    jobs.enqueue(before.id, "image")
    if running:
        jobs.claim()
    before = reels.get_draft(before.id)
    original_jobs = jobs.status(before.id)
    with pytest.raises(UploadError) as conflict:
        store.save_scene_plan(book.id, run["id"], before.id, before.revision, plan_json)
    assert conflict.value.status == 409
    assert reels.get_draft(before.id) == before
    assert jobs.status(before.id) == original_jobs


@pytest.mark.parametrize("field", ["direction", "cast", "image"])
def test_chapter_scene_edits_invalidate_plan_but_cast_and_direction_preserve_media(setup, field):
    _, uploads, book, _, reels, store, _, run = ready_chapter_images(setup)
    before = reels.get_draft(run["plans"][0]["draft_id"])
    before = store.save_scene_plan(book.id, run["id"], before.id, before.revision, plan_for_draft(store, before))
    with store.connection() as connection, connection:
        connection.execute("update local_reel_drafts set character_snapshot_json=? where id=?",
                           ('[{"name":"Original rendered identity"}]', before.id))
    before = reels.get_draft(before.id)
    changes = {}
    if field == "direction":
        changes["scene_direction"] = "Both hands visibly free"
    elif field == "cast":
        characters = CharacterStore(uploads)
        ref = characters.create(book.id, name="Samuel")
        ref = characters.save_reference_file(book.id, ref.id, ref.revision, image_file("#334455"))
        changes["character_ids"] = [ref.id]
    store.save_prompt(book.id, run["id"], before.id, before.revision,
                      image_prompt="A changed scene" if field == "image" else before.image_prompt,
                      teaser_text=before.caption_addition, **changes)
    after = reels.get_draft(before.id)
    assert after.scene_plan_json == ""
    if field != "image":
        for name in ("scene_image_path", "scene_image_sha256", "selected_image_path", "selected_image_sha256",
                     "optimized_image_path", "selected_image_source", "image_stale", "video_stale", "state",
                     "character_snapshot_json"):
            assert getattr(after, name) == getattr(before, name)
    assert store.latest(book.id, include_plans=True)["plans"][0]["suggestion"].scene_plan is None


def test_unchanged_chapter_cast_direction_and_caption_only_edit_preserve_plan(setup):
    _, _, book, _, reels, store, _, run = ready_chapter_images(setup)
    before = reels.get_draft(run["plans"][0]["draft_id"])
    before = store.save_scene_plan(book.id, run["id"], before.id, before.revision, plan_for_draft(store, before))
    store.save_prompt(book.id, run["id"], before.id, before.revision,
                      image_prompt=before.image_prompt, teaser_text="A new teaser text",
                      scene_direction=before.scene_direction, character_ids=list(before.character_ids))
    assert reels.get_draft(before.id).scene_plan_json == before.scene_plan_json


@pytest.mark.parametrize("strategy", ["planned_scene", "scene_plan"])
def test_planned_chapter_enqueue_uses_exact_saved_envelope(setup, strategy):
    _, _, book, _, reels, store, jobs, run = ready_chapter_images(setup, with_reference=strategy == "planned_scene")
    before = reels.get_draft(run["plans"][0]["draft_id"])
    before = store.save_scene_plan(book.id, run["id"], before.id, before.revision, plan_for_draft(store, before))
    store.reopen_image_review(book.id, run["id"], [before.id], revision=before.revision,
                              optimize=strategy == "planned_scene", regenerate=strategy == "scene_plan",
                              strategy=strategy)
    queued = jobs.claim(kinds={"image"})
    assert queued.payload == {"operation": "optimize" if strategy == "planned_scene" else "scene",
                              "strategy": strategy, "scene_plan_json": before.scene_plan_json}
    assert queued.input_revision == before.revision


@pytest.mark.parametrize("optimize", [False, True], ids=["base-plan", "reference-plan"])
def test_chapter_base_plan_accepts_saved_character_profile_without_portrait(setup, optimize):
    _, uploads, book, _, reels, store, jobs, run = ready_chapter_images(setup)
    draft = reels.get_draft(run["plans"][0]["draft_id"])
    profile = CharacterStore(uploads).create(book.id, name="Samuel")
    store.save_prompt(book.id, run["id"], draft.id, draft.revision,
                      image_prompt=draft.image_prompt, teaser_text=draft.caption_addition,
                      character_ids=[profile.id])
    draft = reels.get_draft(draft.id)
    draft = store.save_scene_plan(book.id, run["id"], draft.id, draft.revision, plan_for_draft(store, draft))
    original_jobs = jobs.status(draft.id)
    if optimize:
        with pytest.raises(UploadError, match="Charakterreferenzen") as conflict:
            store.reopen_image_review(book.id, run["id"], [draft.id], optimize=True, strategy="planned_scene")
        assert conflict.value.status == 409
        assert jobs.status(draft.id) == original_jobs
        assert reels.get_draft(draft.id) == draft
    else:
        store.reopen_image_review(book.id, run["id"], [draft.id], regenerate=True, strategy="scene_plan")
        assert jobs.claim(kinds={"image"}).payload["scene_plan_json"] == draft.scene_plan_json


def test_planned_chapter_optimization_rejects_empty_saved_cast(setup):
    _, _, book, _, reels, store, jobs, run = ready_chapter_images(setup)
    draft = reels.get_draft(run["plans"][0]["draft_id"])
    draft = store.save_scene_plan(book.id, run["id"], draft.id, draft.revision, plan_for_draft(store, draft))
    before_jobs = jobs.status(draft.id)
    with pytest.raises(UploadError, match="Charakterreferenzen"):
        store.reopen_image_review(book.id, run["id"], [draft.id], optimize=True, strategy="planned_scene")
    assert reels.get_draft(draft.id) == draft
    assert jobs.status(draft.id) == before_jobs


def test_checkbox_order_does_not_change_saved_cast_or_invalidate_plan(setup):
    _, uploads, book, _, reels, store, jobs, run = ready_chapter_images(setup)
    draft = reels.get_draft(run["plans"][0]["draft_id"])
    characters = CharacterStore(uploads)
    cast = []
    for name in ("Samuel", "Aster"):
        ref = characters.create(book.id, name=name)
        ref = characters.save_reference_file(book.id, ref.id, ref.revision, image_file("#445566"))
        cast.append(ref.id)
    store.save_prompt(book.id, run["id"], draft.id, draft.revision,
                      image_prompt=draft.image_prompt, teaser_text=draft.caption_addition, character_ids=cast)
    draft = reels.get_draft(draft.id)
    draft = store.save_scene_plan(book.id, run["id"], draft.id, draft.revision, plan_for_draft(store, draft))
    store.save_prompt(book.id, run["id"], draft.id, draft.revision,
                      image_prompt=draft.image_prompt, teaser_text=draft.caption_addition,
                      character_ids=list(reversed(cast)), scene_direction=draft.scene_direction)
    assert reels.get_draft(draft.id) == draft
    store.reopen_image_review(book.id, run["id"], [draft.id], optimize=True, strategy="planned_scene",
                              character_ids=list(reversed(cast)))
    assert reels.get_draft(draft.id).character_ids == tuple(cast)
    assert jobs.claim(kinds={"image"}).payload["scene_plan_json"] == draft.scene_plan_json


@pytest.mark.parametrize("change", ["style", "alias", "reference_hash", "cast", "malformed"])
def test_planned_chapter_batch_rejects_current_metadata_changes_before_any_mutation(setup, change):
    _, uploads, book, _, reels, store, jobs, run = ready_chapter_images(setup, with_reference=True)
    drafts = [reels.get_draft(plan["draft_id"]) for plan in run["plans"]]
    for draft in drafts:
        store.save_scene_plan(book.id, run["id"], draft.id, draft.revision, plan_for_draft(store, draft))
    if change == "style":
        with store.connection() as connection, connection:
            ManagementStore.schema(connection)
            connection.execute("insert into local_book_settings values(?,1,?,0)",
                               (book.id, BookDetails(title=book.title, image_prompt_base="Ink drawing").model_dump_json()))
    elif change in {"alias", "reference_hash"}:
        character = CharacterStore(uploads).list(book.id)[0]
        with store.connection() as connection, connection:
            if change == "alias":
                connection.execute("update local_book_characters set aliases_json='[\"Changed alias\"]' where id=?",
                                   (character.id,))
            else:
                connection.execute("update local_book_characters set reference_image_sha256=? where id=?",
                                   ("b" * 64, character.id))
    else:
        last = reels.get_draft(drafts[-1].id)
        with store.connection() as connection, connection:
            if change == "cast":
                connection.execute("update local_reel_drafts set character_ids_json=? where id=?",
                                   (json.dumps([CharacterStore(uploads).list(book.id)[0].id]), last.id))
            else:
                connection.execute("update local_reel_drafts set scene_plan_json='{}' where id=?", (last.id,))
    before = store.latest(book.id, include_plans=True)
    before_drafts = reels.list_drafts(book.id)
    before_jobs = {draft.id: jobs.status(draft.id) for draft in drafts}
    with pytest.raises(UploadError):
        store.reopen_image_review(book.id, run["id"], [draft.id for draft in drafts],
                                  optimize=True, strategy="planned_scene")
    assert store.latest(book.id, include_plans=True) == before
    assert reels.list_drafts(book.id) == before_drafts
    assert {draft.id: jobs.status(draft.id) for draft in drafts} == before_jobs


def test_planned_chapter_optimization_requires_changed_cast_to_be_saved_and_replanned(setup):
    _, uploads, book, _, reels, store, jobs, run = ready_chapter_images(setup, with_reference=True)
    draft = reels.get_draft(run["plans"][-1]["draft_id"])
    draft = store.save_scene_plan(book.id, run["id"], draft.id, draft.revision, plan_for_draft(store, draft))
    selected = [CharacterStore(uploads).list(book.id)[0].id]
    assert list(draft.character_ids) != selected
    before_jobs = jobs.status(draft.id)
    with pytest.raises(UploadError, match="zuerst speichern") as conflict:
        store.reopen_image_review(book.id, run["id"], [draft.id], optimize=True,
                                  strategy="planned_scene", character_ids=selected)
    assert conflict.value.status == 409
    assert reels.get_draft(draft.id) == draft
    assert jobs.status(draft.id) == before_jobs
