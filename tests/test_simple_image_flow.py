"""Both buttons share a fenced scene plan; only one receives reference pixels."""
from dataclasses import replace
import json
import sqlite3

import pytest

from bookpromo.characters import CharacterStore
from bookpromo.config import Settings
from bookpromo.image_flow import description_scene_prompt
from bookpromo.reel_worker import run_reel_once
from bookpromo.scene_plan import ScenePlan, encode_saved_plan, scene_plan_fingerprint
from bookpromo.text_ai import TextAIError, provider_endpoint_hash, provider_model_id
from bookpromo.uploads import UploadError
from test_reels import setup, new_draft, png_bytes


def prepare(setup, *, references=True, plan=True):
    uploads, book, reels, jobs = setup
    characters = CharacterStore(uploads)
    fox = characters.create(book, name="Aster", image_prompt="A silver fox with black ears. Watercolor portrait on white.")
    if references:
        fox = characters.save_reference_file(book, fox.id, fox.revision, png_bytes((256, 256)))
    draft = new_draft(reels, book)
    settings = Settings(_env_file=None, app_data_dir=uploads.root,
                        comfyui_url="http://127.0.0.1:8188",
                        openwebui_url="http://127.0.0.1:12345", openwebui_model="test", openwebui_api_key="test")
    proposed = replace(draft, image_prompt="Aster beside a stream", character_ids_json=json.dumps([fox.id]))
    fingerprint = scene_plan_fingerprint(quote=draft.quote_text, image_prompt=proposed.image_prompt,
                                        scene_direction="", art_direction="", characters=[fox])
    scene = ScenePlan(setting="A forest stream", composition="Medium shot, large visible actor",
                      art_direction="Watercolor", actors=[{"name": "Aster", "pose": "Crouches beside the stream"}])
    payload = {
        "input_fingerprint": fingerprint, "art_direction": "",
        "source_context": {"quote": draft.quote_text, "context_before": "Before", "context_after": "After"},
        "ai_provider": "openwebui", "ai_model_id": provider_model_id(settings, "openwebui"),
        "ai_endpoint_hash": provider_endpoint_hash(settings, "openwebui"),
        "scene_plan_json": encode_saved_plan(scene, fingerprint) if plan else "",
    }
    inputs = dict(image_prompt=proposed.image_prompt, scene_direction="", character_ids=[fox.id], payload=payload)
    return draft, fox, settings, scene, inputs


def fake_generator(monkeypatch, tmp_path, *, fail=False):
    generated = tmp_path / "generated.png"
    generated.write_bytes(png_bytes((96, 128)).read())
    calls = []
    class Generator:
        def __init__(self, *args, **kwargs):
            pass
        def capture(self, mode, kwargs):
            calls.append((mode, kwargs))
            if "progress" in kwargs:
                kwargs["progress"]("submitted-job-1")
            if fail:
                raise RuntimeError("local render failed")
            return generated
        def generate_image(self, **kwargs):
            return self.capture("text", kwargs)
        def restage_character_references(self, **kwargs):
            return self.capture("references", kwargs)
        def resume_image(self, **kwargs):
            return self.capture("resume", kwargs)
    monkeypatch.setattr("bookpromo.reel_worker.ReelGenerator", Generator)
    monkeypatch.setattr("bookpromo.reel_worker.ffmpeg_binary", lambda: "ffmpeg")
    return calls


@pytest.mark.parametrize("mode", ["text", "references"])
def test_new_scene_needs_no_existing_image_or_copy_step(setup, monkeypatch, tmp_path, mode):
    uploads, _, reels, jobs = setup
    draft, fox, settings, scene, inputs = prepare(setup)
    calls = fake_generator(monkeypatch, tmp_path)
    job = jobs.enqueue_simple_image(draft.id, draft.revision, mode=mode, **inputs)
    assert not reels.get_draft(draft.id).scene_image_path
    assert run_reel_once(uploads, settings)
    after = reels.get_draft(draft.id)
    assert jobs.status(draft.id)[0]["state"] == "done"
    assert len(calls) == 1 and calls[0][0] == mode
    kwargs = calls[0][1]
    if mode == "references":
        assert "scene_image_path" not in kwargs
        assert kwargs["references"][0].reference_image_path == CharacterStore(uploads).reference_path(fox)
        assert kwargs["scene_plan"] == scene and kwargs["target_size"] == (736, 1312)
        assert kwargs["simple_scene"] is True
        assert after.selected_image_source == "optimized" and after.optimized_image_path
    else:
        assert "references" not in kwargs
        assert "silver fox" in kwargs["image_prompt"] and "black ears" in kwargs["image_prompt"]
        assert "portrait on white" not in kwargs["image_prompt"]
        assert after.selected_image_source == "scene" and after.scene_image_path
    assert after.selected_image_path and not after.image_stale
    assert json.loads(inputs["payload"]["scene_plan_json"]) == json.loads(after.scene_plan_json)


def test_missing_reference_does_not_block_description_button(setup):
    _, _, reels, jobs = setup
    draft, _, _, _, inputs = prepare(setup, references=False)
    with pytest.raises(UploadError, match="Referenzbild"):
        jobs.enqueue_simple_image(draft.id, draft.revision, mode="references", **inputs)
    assert reels.get_draft(draft.id) == draft and not jobs.status(draft.id)
    assert jobs.enqueue_simple_image(draft.id, draft.revision, mode="text", **inputs)


def test_one_auto_plan_is_durable_and_shared_between_buttons(setup, monkeypatch, tmp_path):
    uploads, _, reels, jobs = setup
    draft, _, settings, scene, inputs = prepare(setup, plan=False)
    plans = []
    async def generate(client, **kwargs):
        plans.append(kwargs)
        return scene
    monkeypatch.setattr("bookpromo.image_flow.generate_scene_plan", generate)
    monkeypatch.setattr("bookpromo.image_flow.create_text_client", lambda *args, **kwargs: object())
    calls = fake_generator(monkeypatch, tmp_path)
    jobs.enqueue_simple_image(draft.id, draft.revision, mode="text", **inputs)
    assert run_reel_once(uploads, settings)
    after = reels.get_draft(draft.id)
    assert len(plans) == 1 and plans[0]["context_before"] == "Before"
    inputs["payload"]["scene_plan_json"] = after.scene_plan_json
    jobs.enqueue_simple_image(after.id, after.revision, mode="references", **inputs)
    assert run_reel_once(uploads, settings)
    final = reels.get_draft(draft.id)
    assert len(plans) == 1 and len(calls) == 2
    assert final.scene_image_path == after.scene_image_path
    assert final.optimized_image_path
    assert final.selected_image_path == after.selected_image_path
    assert final.selected_image_source == "scene"


def test_local_qwen_scene_plan_gets_one_feedback_correction(setup, monkeypatch, tmp_path):
    uploads, _, reels, jobs = setup
    draft, _, settings, scene, inputs = prepare(setup, plan=False)
    settings.comfyui_qwen_model = "qwen-local.safetensors"
    inputs["payload"].update(
        ai_provider="comfyui_qwen",
        ai_model_id=provider_model_id(settings, "comfyui_qwen"),
        ai_endpoint_hash=provider_endpoint_hash(settings, "comfyui_qwen"),
    )
    calls = []

    async def generate(client, *, validation_feedback=(), **kwargs):
        calls.append(validation_feedback)
        if len(calls) == 1:
            raise TextAIError(
                "invalid plan", code="structured",
                validation_issues=("actors.1: contact refers to no prop",),
            )
        return scene

    monkeypatch.setattr("bookpromo.image_flow.generate_scene_plan", generate)
    monkeypatch.setattr("bookpromo.image_flow.create_text_client", lambda *args, **kwargs: object())
    fake_generator(monkeypatch, tmp_path)
    jobs.enqueue_simple_image(draft.id, draft.revision, mode="text", **inputs)

    assert run_reel_once(uploads, settings)
    assert calls == [(), ("actors.1: contact refers to no prop",)]
    assert jobs.status(draft.id)[0]["state"] == "done"
    assert reels.get_draft(draft.id).scene_plan_json


@pytest.mark.parametrize("failure", ["fingerprint", "source", "duplicate", "provider_changed"])
def test_invalid_snapshot_never_spends_image_render(setup, monkeypatch, tmp_path, failure):
    uploads, _, reels, jobs = setup
    draft, _, settings, _, inputs = prepare(setup, plan=False)
    calls = fake_generator(monkeypatch, tmp_path)
    if failure == "fingerprint":
        inputs["payload"]["input_fingerprint"] = "a" * 64
    if failure == "source":
        inputs["payload"]["source_context"]["quote"] = "Other quote"
    if failure in {"fingerprint", "source"}:
        with pytest.raises(UploadError):
            jobs.enqueue_simple_image(draft.id, draft.revision, mode="text", **inputs)
        assert not jobs.status(draft.id) and reels.get_draft(draft.id) == draft
    else:
        jobs.enqueue_simple_image(draft.id, draft.revision, mode="text", **inputs)
        if failure == "duplicate":
            current = reels.get_draft(draft.id)
            with pytest.raises(UploadError, match="läuft"):
                jobs.enqueue_simple_image(current.id, current.revision, mode="text", **inputs)
            assert len(jobs.status(draft.id)) == 1
        else:
            settings.openwebui_model = "other"
            assert run_reel_once(uploads, settings)
            assert jobs.status(draft.id)[0]["state"] == "failed"
            assert "Konfiguration" in jobs.status(draft.id)[0]["error"]
    assert not calls


def test_interrupted_planning_never_repeats_ambiguous_ai_call(setup, monkeypatch, tmp_path):
    uploads, _, _, jobs = setup
    draft, _, settings, _, inputs = prepare(setup, plan=False)
    jobs.enqueue_simple_image(draft.id, draft.revision, mode="text", **inputs)
    job = jobs.claim()
    assert jobs.checkpoint_simple_image(job, phase="planning_started")
    with sqlite3.connect(uploads.db_path) as connection:
        connection.execute("update local_reel_jobs set lease_until=0 where id=?", (job.id,))
    calls = fake_generator(monkeypatch, tmp_path)
    monkeypatch.setattr("bookpromo.image_flow.create_text_client", lambda *a, **k: pytest.fail("No duplicate AI call"))
    assert run_reel_once(uploads, settings)
    assert not calls
    assert "unterbrochen" in jobs.status(draft.id)[0]["error"]


def test_submitted_image_is_resumed_not_submitted_twice(setup, monkeypatch, tmp_path):
    uploads, _, _, jobs = setup
    draft, _, settings, _, inputs = prepare(setup)
    jobs.enqueue_simple_image(draft.id, draft.revision, mode="references", **inputs)
    job = jobs.claim()
    assert jobs.checkpoint_simple_image(job, phase="render_started", prompt_id="existing-id",
                                       render_endpoint_hash=provider_endpoint_hash(settings, "comfyui_qwen"),
                                       effective_image_prompt="Original submitted conditioning before code update")
    with sqlite3.connect(uploads.db_path) as connection:
        connection.execute("update local_reel_jobs set lease_until=0 where id=?", (job.id,))
    calls = fake_generator(monkeypatch, tmp_path)
    settings = settings.model_copy(update={"reel_reference_workflow": tmp_path / "removed-profile.json"})
    monkeypatch.setattr("bookpromo.reel_worker.build_planned_reference_prompt", lambda *a, **k: pytest.fail("Do not recompile an already submitted graph"))
    monkeypatch.setattr("bookpromo.reel_worker.build_simple_reference_prompt", lambda *a, **k: pytest.fail("Do not recompile an already submitted graph"))
    assert run_reel_once(uploads, settings)
    assert calls[0][0] == "resume" and calls[0][1]["prompt_id"] == "existing-id" and len(calls) == 1
    assert jobs.status(draft.id)[0]["state"] == "done"
    with sqlite3.connect(uploads.db_path) as connection:
        result = json.loads(connection.execute(
            "select result_json from local_reel_jobs where id=?", (job.id,),
        ).fetchone()[0])
    assert result["effective_image_prompt"] == "Original submitted conditioning before code update"


def test_frozen_render_prompt_cannot_be_rewritten(setup):
    _, _, _, jobs = setup
    draft, _, _, _, inputs = prepare(setup)
    jobs.enqueue_simple_image(draft.id, draft.revision, mode="references", **inputs)
    job = jobs.claim()
    assert jobs.checkpoint_simple_image(job, phase="render_started", effective_image_prompt="Original prompt")
    with pytest.raises(ValueError, match="cannot be changed"):
        jobs.checkpoint_simple_image(job, phase="render_started", effective_image_prompt="Changed prompt")


def test_render_failure_keeps_last_selected_media_and_cached_plan(setup, monkeypatch, tmp_path):
    uploads, _, reels, jobs = setup
    draft, _, settings, _, inputs = prepare(setup)
    selected = reels.save_uploaded_image(draft.id, draft.revision, png_bytes(), "custom.png")
    selected = reels.select_image(selected.id, selected.revision, "upload")
    calls = fake_generator(monkeypatch, tmp_path, fail=True)
    jobs.enqueue_simple_image(draft.id, selected.revision, mode="references", **inputs)
    assert run_reel_once(uploads, settings)
    after = reels.get_draft(draft.id)
    assert after.selected_image_path == selected.selected_image_path and after.selected_image_source == "upload"
    assert after.scene_plan_json
    assert len(calls) == 1 and jobs.status(draft.id)[0]["state"] == "failed"


def test_changed_reference_during_gpu_retains_only_stale_history(setup, monkeypatch, tmp_path):
    uploads, book, reels, jobs = setup
    draft, fox, settings, _, inputs = prepare(setup)
    fake_generator(monkeypatch, tmp_path)
    original_finish = jobs.finish
    def change_before_finish(self, job, **kwargs):
        if kwargs.get("result"):
            CharacterStore(uploads).save_reference_file(book, fox.id, fox.revision, png_bytes((257, 257)))
        return original_finish(job, **kwargs)
    monkeypatch.setattr(type(jobs), "finish", change_before_finish)
    jobs.enqueue_simple_image(draft.id, draft.revision, mode="references", **inputs)
    assert run_reel_once(uploads, settings)
    after = reels.get_draft(draft.id)
    assert jobs.status(draft.id)[0]["state"] == "stale"
    assert not after.selected_image_path and not after.optimized_image_path
    assert after.state == "editing"


def test_selected_snapshot_and_provenance_follow_image_version(setup, monkeypatch, tmp_path):
    uploads, _, reels, jobs = setup
    draft, fox, settings, scene, inputs = prepare(setup)
    fake_generator(monkeypatch, tmp_path)
    jobs.enqueue_simple_image(draft.id, draft.revision, mode="text", **inputs)
    assert run_reel_once(uploads, settings)
    before = reels.get_draft(draft.id)
    provenance = reels.selected_image_generation(before)
    character_store = CharacterStore(uploads)
    new_fox = character_store.save_reference_file(before.book_id, fox.id, fox.revision, png_bytes((260, 260)))
    fingerprint = scene_plan_fingerprint(quote=before.quote_text, image_prompt=before.image_prompt,
                                        scene_direction="", art_direction="", characters=[new_fox])
    inputs["payload"].update(input_fingerprint=fingerprint, scene_plan_json=encode_saved_plan(scene, fingerprint))
    (tmp_path / "generated.png").write_bytes(png_bytes((97, 129)).read())
    jobs.enqueue_simple_image(before.id, before.revision, mode="references", **inputs)
    assert run_reel_once(uploads, settings)
    after = reels.get_draft(draft.id)
    assert after.character_snapshot == before.character_snapshot
    assert reels.selected_image_generation(after) == provenance
    chosen = reels.select_image(after.id, after.revision, "optimized")
    assert chosen.character_snapshot[0]["reference_image_sha256"] == new_fox.reference_image_sha256
    assert reels.selected_image_generation(chosen)["candidate"] == "optimized"


def test_native_reference_graph_renders_without_source_pixels(tmp_path):
    from test_reel_generation import restage_fixture, resolved_reference_plan
    generator, old_scene, references, captured, _ = restage_fixture(tmp_path, count=2)
    # Prove there is no size/pixel dependency on an old scene, not just an omitted upload.
    old_scene.unlink()
    generator.restage_character_references(
        reel_id="new-independent-scene", references=references,
        scene_plan=resolved_reference_plan([ref.name for ref in references]), target_size=(736, 1312), simple_scene=True,
    )
    graph = captured["jobs"][0][0]
    assert graph["restage-empty"]["inputs"] == {"width": 736, "height": 1312, "batch_size": 1}
    assert len(captured["uploads"]) == 2
    assert len([node for node in graph.values() if node["class_type"] == "LoadImage"]) == 2
    from bookpromo.reel_generation import build_simple_reference_prompt
    assert graph["restage-positive"]["inputs"]["text"] == build_simple_reference_prompt(
        resolved_reference_plan([ref.name for ref in references]), references,
    )


def test_simple_reference_binding_follows_pixel_order_not_actor_order():
    from pathlib import Path
    from bookpromo.reel_generation import CharacterReferenceSpec, build_simple_reference_prompt
    refs = [
        CharacterReferenceSpec("Mira", Path("unused"), "", "A red creature with horns. Standing in a castle.", aliases=("Mi",)),
        CharacterReferenceSpec("Aster", Path("unused"), "", "A silver fox with black ears. Holding a sword."),
    ]
    plan = ScenePlan(setting="A river", composition="Three principal actors", art_direction="Watercolor",
                     actors=[{"name": "Aster", "pose": "Crouches"}, {"name": "Mi", "pose": "Swims"},
                             {"name": "Unnamed fisher", "pose": "Waits on the bank"}])
    prompt = build_simple_reference_prompt(plan, refs)
    assert "exactly 3 principal characters" in prompt
    assert "Aster has the complete visual identity from Image 2" in prompt
    assert "Mi has the complete visual identity from Image 1" in prompt
    assert "The single character Unnamed fisher. Pose" in prompt
    blocks = prompt.split("The single character ")
    assert "silver fox" in blocks[1] and "horns" not in blocks[1]
    assert "horns" in blocks[2] and "silver fox" not in blocks[2]
    assert "Standing in a castle" not in prompt and "Holding a sword" not in prompt
    assert "Identity references:" not in prompt


def test_resume_polls_existing_prompt_without_posting_or_interrupting():
    from bookpromo.comfy import ComfyClient
    from test_comfy import FakeTransport
    transport = FakeTransport(histories=[{}, {"existing": {"status": {"completed": True}, "outputs": {}}}])
    client = ComfyClient("http://127.0.0.1:8188", transport=transport)
    result = client.wait_for_prompt("existing", timeout_sec=5, poll_interval_sec=0)
    assert result.ok and result.prompt_id == "existing"
    assert not transport.posts
