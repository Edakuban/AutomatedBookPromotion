import copy
from dataclasses import replace
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from bookpromo.ai_settings import AISettingsStore
from bookpromo.config import Settings
from bookpromo.image_presets import (
    PRESETS, build_preset_workflow, checked_preset, check_preset_available,
    identity_edit_prompts, preset_snapshot,
)
from bookpromo.reel_worker import run_reel_once
from bookpromo.scene_plan import ScenePlan
from bookpromo.uploads import UploadError
from bookpromo.web import create_app
from test_reels import setup, png_bytes
from test_simple_image_flow import prepare, fake_generator


@pytest.mark.parametrize("preset_id", PRESETS)
def test_fixed_bundle_and_sequential_reference_pixel_bindings(preset_id):
    snapshot = preset_snapshot(preset_id)
    graph = build_preset_workflow(snapshot, "Scene only", reference_images=["a.png", "b.png"],
                                  edit_prompts=["Replace A", "Replace B"], seed=123)
    assert graph["model"]["inputs"]["unet_name"] == snapshot["model"]
    assert graph["clip"]["inputs"]["clip_name"] == snapshot["encoder"]
    assert graph["vae"]["inputs"]["vae_name"] == snapshot["vae"]
    assert graph["scene-sigmas"]["inputs"]["steps"] == snapshot["steps"]
    assert graph["edit-1-guider"]["inputs"]["cfg"] == snapshot["cfg"]
    assert graph["edit-0-ref0scale"]["inputs"]["image"] == ["scene-decode", 0]
    assert graph["edit-0-ref1scale"]["inputs"]["image"] == ["identity-0", 0]
    assert graph["edit-1-ref0scale"]["inputs"]["image"] == ["edit-0-decode", 0]
    assert graph["identity-1"]["inputs"]["image"] == "b.png"
    assert graph["save"]["inputs"]["images"] == ["edit-1-decode", 0]
    assert sum(node["class_type"] == "SaveImage" for node in graph.values()) == 1
    for node in graph.values():
        for value in node["inputs"].values():
            if isinstance(value, list):
                assert value[0] in graph  # No dangling nested-workflow links.


def test_no_free_loader_or_sampling_combinations():
    for field, value in [("model", "other"), ("steps", 8), ("vae", "other")]:
        snapshot = preset_snapshot("flux2-klein-4b-distilled")
        snapshot[field] = value
        with pytest.raises(ValueError):
            checked_preset(snapshot)
    with pytest.raises(ValueError):
        preset_snapshot("arbitrary-model")


@pytest.mark.parametrize("reference_count", [0, 1, 2])
def test_new_9b_empty_negative_text_in_scene_and_each_reference_stage(reference_count):
    snapshot = preset_snapshot("flux2-klein-9b-base")
    assert snapshot["version"] == 2
    graph = build_preset_workflow(snapshot, "Scene", seed=123,
        reference_images=[f"ref-{i}.png" for i in range(reference_count)],
        edit_prompts=[f"Replace actor {i}" for i in range(reference_count)])
    assert not any(n["class_type"] == "ConditioningZeroOut" for n in graph.values())
    for prefix in ["scene-", *(f"edit-{i}-" for i in range(reference_count))]:
        assert graph[prefix + "zero"] == {"class_type": "CLIPTextEncode",
            "inputs": {"clip": ["clip", 0], "text": ""}}
        assert graph[prefix + "sigmas"]["inputs"]["steps"] == 50
        assert graph[prefix + "guider"]["inputs"]["cfg"] == 4
        if prefix == "scene-":
            assert graph[prefix + "guider"]["inputs"]["negative"] == [prefix + "zero", 0]
        else:
            assert graph[prefix + "ref0negative"]["inputs"]["conditioning"] == [prefix + "zero", 0]
            assert graph[prefix + "ref1negative"]["inputs"]["conditioning"] == [prefix + "ref0negative", 0]
            assert graph[prefix + "guider"]["inputs"]["negative"] == [prefix + "ref1negative", 0]


def test_distilled_4b_and_frozen_9b_keep_original_negative_policy():
    for preset_id in PRESETS:
        snapshot = preset_snapshot(preset_id)
        snapshot["version"] = 1
        assert checked_preset(snapshot).version == 1
        graph = build_preset_workflow(snapshot, "Scene", seed=123,
            reference_images=["ref.png"], edit_prompts=["Replace actor"])
        for prefix in ("scene-", "edit-0-"):
            assert graph[prefix + "zero"] == {"class_type": "ConditioningZeroOut",
                "inputs": {"conditioning": [prefix + "text", 0]}}
    for preset_id in PRESETS:
        snapshot = preset_snapshot(preset_id)
        snapshot["version"] = 999
        with pytest.raises(ValueError):
            checked_preset(snapshot)


@pytest.mark.parametrize("preset_id,suffix", [("flux2-klein-4b-distilled", "VocaVid"),
                                            ("flux2-klein-9b-base", "BookPromo")])
def test_display_labels_are_clean_without_invalidating_old_queued_presets(preset_id, suffix):
    snapshot = preset_snapshot(preset_id)
    assert suffix not in snapshot["label"] and " · " not in snapshot["label"]
    snapshot["label"] += " · " + suffix
    assert checked_preset(snapshot).id == preset_id
    snapshot["steps"] += 1
    with pytest.raises(ValueError):
        checked_preset(snapshot)


def test_identity_mapping_has_no_description_imageprompt_or_species_injection():
    plan = ScenePlan(setting="room", composition="two figures", art_direction="paint",
                     actors=[{"name": "B", "pose": "on the RIGHT"}, {"name": "A", "pose": "on the LEFT"}])
    chars = [SimpleNamespace(name="A", aliases=[], image_prompt="SECRET_APPEARANCE", description="SECRET_DESCRIPTION"),
             SimpleNamespace(name="B", aliases=[], image_prompt="SECRET_2", description="SECRET_3")]
    prompts = identity_edit_prompts(plan, chars)
    assert "reference image of A" in prompts[0] and "The target is A" in prompts[0]
    assert "reference image of B" in prompts[1] and "The target is B" in prompts[1]
    assert all("SECRET" not in prompt and "clothing" in prompt for prompt in prompts)


def test_missing_model_never_substitutes_other_bundle():
    client = SimpleNamespace(base_url="http://local", transport=SimpleNamespace(get_json=lambda url: {}))
    with pytest.raises(ValueError, match="Kein Ersatzmodell"):
        check_preset_available(client, preset_snapshot("flux2-klein-4b-distilled"))


def test_global_ai_settings_revision_and_persistence(setup):
    uploads, _, _, _ = setup
    settings = Settings(_env_file=None)
    store = AISettingsStore(uploads)
    assert store.get(settings).revision == 0
    saved = store.save(settings, 0, "flux2-klein-4b-distilled", "comfyui_qwen")
    assert AISettingsStore(uploads).get(settings) == saved
    with pytest.raises(UploadError, match="geändert"):
        store.save(settings, 0, "flux2-klein-9b-base", "openwebui")
    with pytest.raises(UploadError):
        store.save(settings, 1, "other", "openwebui")
    assert store.get(settings) == saved


def test_global_ai_route_has_two_fixed_presets_and_conflict(setup):
    uploads, _, _, _ = setup
    settings = Settings(_env_file=None, app_data_dir=uploads.root)
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        html = client.get('/settings').text
        assert 'name="image_preset"' in html and 'name="text_provider"' in html
        assert '50 Schritte' in html and '4 Schritte' in html
        fields = {'revision': 0, 'image_preset': 'flux2-klein-4b-distilled', 'text_provider': 'comfyui_qwen'}
        assert client.post('/settings/ai', data=fields, follow_redirects=False).status_code == 303
        assert client.post('/settings/ai', data=fields, follow_redirects=False).status_code == 409
        assert client.post('/settings/ai', data=dict(fields, revision=1, image_preset='free')).status_code == 400


@pytest.mark.parametrize("mode", ["plain", "text", "references"])
@pytest.mark.parametrize("preset_id", PRESETS)
def test_worker_three_modes_keep_conditioning_separate(setup, monkeypatch, tmp_path, mode, preset_id):
    uploads, _, reels, jobs = setup
    draft, _, settings, _, inputs = prepare(setup)
    inputs['payload']['image_preset'] = preset_snapshot(preset_id)
    fake_generator(monkeypatch, tmp_path)
    generated = tmp_path / 'preset.png'
    generated.write_bytes(png_bytes((96, 128)).read())
    calls = []
    def render(self, **kwargs):
        calls.append(kwargs)
        kwargs['progress']('comfy-fixed-preset')
        return generated
    monkeypatch.setattr('bookpromo.reel_worker.check_preset_available', lambda *a: None)
    monkeypatch.setattr('bookpromo.reel_worker.ReelGenerator.generate_preset_image', render, raising=False)
    jobs.enqueue_simple_image(draft.id, draft.revision, mode=mode, **inputs)
    # Changing global choices cannot alter this captured image bundle.
    other_preset = next(p for p in PRESETS if p != preset_id)
    AISettingsStore(uploads).save(settings, 0, other_preset, 'comfyui_qwen')
    assert run_reel_once(uploads, settings)
    assert jobs.status(draft.id)[0]['state'] == 'done'
    assert len(calls) == 1 and calls[0]['preset'] == preset_snapshot(preset_id)
    assert ('silver fox' in calls[0]['image_prompt']) == (mode == 'text')
    assert bool(calls[0]['references']) == (mode == 'references')
    assert bool(calls[0]['edit_prompts']) == (mode == 'references')
    assert all('silver fox' not in p and 'black ears' not in p for p in calls[0]['edit_prompts'])
    with sqlite3.connect(uploads.db_path) as db:
        payload = json.loads(db.execute('select payload_json from local_reel_jobs where draft_id=?', (draft.id,)).fetchone()[0])
    assert payload['identity_edit_prompts'] == calls[0]['edit_prompts']


def test_submitted_preset_pipeline_resumes_without_any_preflight_or_new_stages(setup, monkeypatch, tmp_path):
    uploads, _, _, jobs = setup
    draft, _, settings, _, inputs = prepare(setup)
    inputs['payload']['image_preset'] = preset_snapshot('flux2-klein-4b-distilled')
    jobs.enqueue_simple_image(draft.id, draft.revision, mode='references', **inputs)
    job = jobs.claim()
    jobs.checkpoint_simple_image(job, phase='render_started', prompt_id='one-comfy-pipeline',
        render_endpoint_hash=__import__('bookpromo.text_ai', fromlist=['provider_endpoint_hash']).provider_endpoint_hash(settings, 'comfyui_qwen'),
        effective_image_prompt='Frozen scene', identity_edit_prompts=['Frozen reference mapping'])
    with sqlite3.connect(uploads.db_path) as db:
        db.execute('update local_reel_jobs set lease_until=0 where id=?', (job.id,))
    calls = fake_generator(monkeypatch, tmp_path)
    monkeypatch.setattr('bookpromo.reel_worker.check_preset_available', lambda *a: pytest.fail('No new preflight'))
    monkeypatch.setattr('bookpromo.reel_worker.identity_edit_prompts', lambda *a: pytest.fail('No new prompts'))
    assert run_reel_once(uploads, settings)
    assert calls[0][0] == 'resume' and len(calls) == 1


def test_missing_preset_files_fail_before_optional_text_ai(setup, monkeypatch, tmp_path):
    uploads, _, _, jobs = setup
    draft, _, settings, _, inputs = prepare(setup, plan=False)
    inputs['payload']['image_preset'] = preset_snapshot('flux2-klein-4b-distilled')
    def missing(*args):
        raise ValueError('Preset missing; no fallback')
    monkeypatch.setattr('bookpromo.reel_worker.check_preset_available', missing)
    monkeypatch.setattr('bookpromo.image_flow.create_text_client', lambda *a, **k: pytest.fail('No paid call'))
    calls = fake_generator(monkeypatch, tmp_path)
    jobs.enqueue_simple_image(draft.id, draft.revision, mode='references', **inputs)
    assert run_reel_once(uploads, settings)
    assert not calls and jobs.status(draft.id)[0]['state'] == 'failed'
