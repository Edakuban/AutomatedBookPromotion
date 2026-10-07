import json
from types import SimpleNamespace

import pytest

from bookpromo.chapter_teaser_worker import (
    _plan_characters, _scene_image_payload, reconcile_chapter_teaser_media, run_chapter_teaser_once,
)
from bookpromo.characters import CharacterStore
from bookpromo.management import ManagementStore
from bookpromo.reels import ReelJobStore
from bookpromo.scene_plan import ScenePlan, decode_saved_plan, describe_scene_direction, scene_plan_fingerprint
from test_analysis import setup
from test_chapter_teasers import prepare_run, suggestion
from test_characters import image_file


def scene_plan(names=("Fox",)):
    return ScenePlan(
        setting="Beside the window", composition="Vertical 9:16",
        art_direction="Watercolor, soft daylight",
        actors=[{"name": name, "pose": "Turned towards the window", "free_parts": [], "contacts": []}
                for name in names], props=[],
    )


class PlannedChapterAPI:
    def __init__(self, chapters, names=("Fox",)):
        self.chapters, self.names, self.calls = chapters, names, []

    async def complete_json(self, system, user, result_type, **kwargs):
        data = json.loads(user)
        self.calls.append((system, data, result_type, kwargs))
        chapter = next(chapter for chapter in self.chapters
                       if chapter.position == data["chapter_metadata"]["position"])
        return result_type.model_validate({
            **suggestion(chapter).model_dump(),
            "image_prompt": "Legacy scene mentioning an unrelated character named Other",
            "scene_plan": scene_plan(self.names).model_dump(),
        })


def test_plan_character_binding_follows_actors_and_aliases_instead_of_raw_scene_mentions():
    fox = SimpleNamespace(id="fox", name="Copper Fox", aliases=("FX",))
    robot = SimpleNamespace(id="robot", name="Robot", aliases=())
    other = SimpleNamespace(id="other", name="Other", aliases=())
    assert _plan_characters(scene_plan(("Robot", " FX ")), [fox, other, robot]) == [robot, fox]
    assert _plan_characters(scene_plan(()), [fox, other, robot]) == []


def test_ambiguous_identity_labels_are_rejected_instead_of_binding_two_refs_to_one_actor():
    refs = [SimpleNamespace(name="Fox", aliases=()), SimpleNamespace(name="Other", aliases=("Fox",))]
    with pytest.raises(ValueError):
        _plan_characters(scene_plan(), refs)


def test_initial_job_payload_preserves_legacy_behavior_and_selects_plan_for_new_drafts():
    assert _scene_image_payload(SimpleNamespace(scene_plan_json="")) == {"operation": "scene"}
    assert _scene_image_payload(SimpleNamespace(scene_plan_json="saved-plan")) == {
        "operation": "scene", "strategy": "scene_plan", "scene_plan_json": "saved-plan",
    }


@pytest.mark.parametrize("preset_id", ["flux2-klein-4b-distilled", "flux2-klein-9b-base"])
def test_chapter_initial_images_use_frozen_global_preset_without_character_prose(setup, monkeypatch, preset_id):
    import sqlite3
    from bookpromo.image_presets import preset_snapshot
    settings, uploads, book, record, reels, _, store, run_id = prepare_run(setup)
    preset = preset_snapshot(preset_id)
    with sqlite3.connect(uploads.db_path) as db:
        row = db.execute("select context_json from local_chapter_teaser_runs where id=?", (run_id,)).fetchone()
        context = {**json.loads(row[0]), "image_preset": preset}
        db.execute("update local_chapter_teaser_runs set context_json=? where id=?", (json.dumps(context), run_id))
    monkeypatch.setattr("bookpromo.image_presets.check_preset_available", lambda *a: None)
    api = PlannedChapterAPI(record.result.chapters, names=())
    assert run_chapter_teaser_once(uploads, settings=settings, api_factory=lambda _: api)
    assert store.latest(book.id)["state"] == "rendering"
    assert all("image_preset" not in json.dumps(call[1]) for call in api.calls)
    job = ReelJobStore(reels).claim(kinds={"image"})
    assert job.payload["image_preset"] == preset
    assert job.payload["strategy"] == "simple_plain"
    assert job.payload["phase"] == "plan_ready"
    assert job.payload["source_snapshot"]["chapter_id"] in {c.id for c in record.result.chapters}


def test_worker_persists_plan_and_queues_only_initial_scene_with_current_style_and_refs(setup):
    settings, uploads, book, record, reels, _, store, _ = prepare_run(setup)
    management = ManagementStore(uploads)
    profile = management.get(book.id)
    management.save(book.id, profile["revision"], profile["details"].model_copy(update={
        "image_prompt_base": "Watercolor, current blue palette",
    }), profile["suggestion_id"])
    characters = CharacterStore(uploads)
    fox = characters.create(book.id, name="Fox", description="PRIVATE description",
                            image_prompt="PRIVATE character prompt")
    fox = characters.save_reference_file(book.id, fox.id, fox.revision, image_file("#334455"))
    other = characters.create(book.id, name="Other", description="Unrelated figure")
    api = PlannedChapterAPI(record.result.chapters)

    assert run_chapter_teaser_once(uploads, settings=settings, api_factory=lambda _: api)

    assert len(api.calls) == len(record.result.chapters)
    for _, data, _, _ in api.calls:
        assert data["global_art_direction"]["style_source"] == "Watercolor, current blue palette"
        assert data["identity_labels"] == []
        assert "PRIVATE" not in json.dumps(data)
    run = store.latest(book.id, include_plans=True)
    assert run["state"] == "rendering" and run["calls_started"] == len(record.result.chapters)
    for draft in reels.list_drafts(book.id):
        assert list(draft.character_ids) == [fox.id]
        assert other.id not in draft.character_ids
        saved = decode_saved_plan(draft.scene_plan_json)
        assert draft.scene_direction == describe_scene_direction(saved.plan)
        assert saved.fingerprint == scene_plan_fingerprint(
            quote=draft.quote_text, image_prompt=draft.image_prompt, scene_direction=draft.scene_direction,
            art_direction="Watercolor, current blue palette", characters=[fox],
        )
    jobs = ReelJobStore(reels)
    claimed = jobs.claim(kinds={"image"})
    draft = reels.get_draft(claimed.draft_id)
    assert claimed.payload == _scene_image_payload(draft)
    assert claimed.payload["strategy"] == "scene_plan"
    assert not any(job["kind"] == "video" for item in reels.list_drafts(book.id) for job in jobs.status(item.id))
    assert all(not item.selected_image_path for item in reels.list_drafts(book.id))


def test_environment_only_worker_plans_do_not_select_context_only_characters(setup):
    settings, uploads, book, record, reels, _, store, _ = prepare_run(setup)
    CharacterStore(uploads).create(book.id, name="Other")
    api = PlannedChapterAPI(record.result.chapters, names=())
    assert run_chapter_teaser_once(uploads, settings=settings, api_factory=lambda _: api)
    assert store.latest(book.id)["state"] == "rendering"
    drafts = reels.list_drafts(book.id)
    assert all(not draft.character_ids for draft in drafts)
    assert all(decode_saved_plan(draft.scene_plan_json).plan.actors == [] for draft in drafts)


def test_worker_passes_only_chapter_relevant_identity_labels(setup):
    settings, uploads, book, record, _, _, _, _ = prepare_run(setup)
    characters = CharacterStore(uploads)
    character = characters.create(book.id, name="Window Watcher")
    characters.save(book.id, character.id, character.revision, name=character.name,
                    aliases=["Niemand"], description="PRIVATE", image_prompt="PRIVATE", approved=False)
    characters.create(book.id, name="Unrelated Character")
    api = PlannedChapterAPI(record.result.chapters, names=())
    assert run_chapter_teaser_once(uploads, settings=settings, api_factory=lambda _: api)
    assert api.calls[0][1]["identity_labels"] == [{"name": "Window Watcher", "aliases": ["Niemand"]}]
    assert api.calls[1][1]["identity_labels"] == []


def test_reconciliation_reuses_saved_plan_when_requeuing_initial_images(setup):
    settings, uploads, book, record, reels, _, store, _ = prepare_run(setup)
    api = PlannedChapterAPI(record.result.chapters, names=())
    assert run_chapter_teaser_once(uploads, settings=settings, api_factory=lambda _: api)
    jobs = ReelJobStore(reels)
    with store.connection() as connection, connection:
        connection.execute("update local_reel_jobs set state='cancelled' where state='queued'")
        connection.execute("update local_chapter_teaser_plans set state='analyzed'")
    assert reconcile_chapter_teaser_media(uploads, reels, jobs)
    job = jobs.claim(kinds={"image"})
    assert job is not None
    assert job.payload == _scene_image_payload(reels.get_draft(job.draft_id))
    assert len(api.calls) == len(record.result.chapters)
