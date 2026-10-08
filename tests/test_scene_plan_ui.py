"""Explicit plan generation and revision-fenced rendering, without real AI/GPU calls."""
from html import escape
import json

import pytest
from fastapi.testclient import TestClient

from bookpromo import web
from bookpromo.management import ManagementStore
from bookpromo.reel_content import ReelCopy
from bookpromo.scene_plan import ScenePlan, decode_saved_plan, describe_scene_direction
from test_analysis import setup
from test_reel_restage_ui import prepare


def example_plan():
    return ScenePlan(setting="A quiet forest clearing", composition="Vertical, low camera",
                     art_direction="Soft watercolor",
                     actors=[{"name": "Aster", "pose": "Crouches beside the stream looking down",
                              "free_parts": ["left forepaw", "right forepaw"], "contacts": []}], props=[])


def generate_mock(monkeypatch, *, callback=None):
    calls = []
    async def generate(client, **kwargs):
        calls.append(kwargs)
        if callback:
            callback()
        return example_plan()
    monkeypatch.setattr(web, "generate_scene_plan", generate)
    monkeypatch.setattr(web, "create_text_client", lambda settings, provider: object())
    return calls


def plan_request(client, url, draft):
    return client.post(url + "/plan/generate", data={"revision": draft.revision, "ai_provider": "openwebui"})


def test_explicit_plan_saves_only_plan_and_render_uses_exact_snapshot(setup, monkeypatch):
    app, url, reels, jobs, _, before = prepare(setup)
    calls = generate_mock(monkeypatch)
    original_jobs = jobs.status(before.id)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert "Szenenplan fehlt oder ist veraltet" in client.get(url).text
        assert not calls
        assert plan_request(client, url, before).status_code == 200
        after = reels.get_draft(before.id)
        saved = decode_saved_plan(after.scene_plan_json)
        assert saved.plan == example_plan()
        page = client.get(url).text
        assert "Szenenplan aktuell" in page and "Gespeicherten Szenenplan prüfen" in page
        assert escape(example_plan().actors[0].pose) in page
        assert 'data-plan-current="true"' in page
        assert jobs.status(before.id) == original_jobs
        for name in ("selected_image_path", "selected_image_sha256", "image_stale", "video_stale",
                     "scene_image_path", "image_prompt", "scene_direction", "character_ids"):
            assert getattr(after, name) == getattr(before, name)
        assert after.revision == before.revision + 1
        response = client.post(url + "/image/optimize", data={
            "revision": after.revision, "strategy": "planned_scene", "scene_direction": after.scene_direction,
        })
        assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0]["characters"] == ({"name": "Aster", "aliases": ["Ast"]},)
    assert calls[0]["scene_direction"] == before.scene_direction
    assert "art_direction" in calls[0]
    with reels.connection() as connection:
        row = connection.execute("select payload_json from local_reel_jobs where state='queued'").fetchone()
    assert json.loads(row[0]) == {"operation": "optimize", "strategy": "planned_scene",
                                "scene_plan_json": after.scene_plan_json}
    assert reels.get_draft(before.id) == after


@pytest.mark.parametrize("change", ["no_revision", "unsaved_direction", "saved_direction", "style", "reference", "missing_plan"])
def test_stale_or_unsaved_plan_cannot_enqueue_or_call_ai(setup, monkeypatch, change):
    app, url, reels, jobs, characters, before = prepare(setup)
    calls = generate_mock(monkeypatch)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert plan_request(client, url, before).status_code == 200
        draft = reels.get_draft(before.id)
        fields = {"revision": draft.revision, "strategy": "planned_scene", "scene_direction": draft.scene_direction}
        if change == "no_revision":
            del fields["revision"]
        elif change == "unsaved_direction":
            fields["scene_direction"] = "Looks up."
        elif change in {"saved_direction", "missing_plan"}:
            draft = reels.update_draft(draft.id, draft.revision, scene_direction="Looks up.")
            fields.update(revision=draft.revision, scene_direction=draft.scene_direction)
        elif change == "style":
            management = ManagementStore(reels.uploads)
            snapshot = management.get(draft.book_id)
            management.save(draft.book_id, snapshot["revision"],
                            snapshot["details"].model_copy(update={"image_prompt_base": "Oil painting"}),
                            snapshot["suggestion_id"])
        elif change == "reference":
            character = characters.list(draft.book_id)[0]
            characters.save(draft.book_id, character.id, character.revision, name=character.name,
                            aliases=character.aliases, description="Changed", image_prompt=character.image_prompt,
                            approved=True)
        before_request = reels.get_draft(before.id)
        original_jobs = jobs.status(before.id)
        assert client.post(url + "/image/optimize", data=fields).status_code == 409
        assert reels.get_draft(before.id) == before_request
        assert jobs.status(before.id) == original_jobs
    assert len(calls) == 1


@pytest.mark.parametrize("fields", [
    {"ai_provider": "openwebui"}, {"revision": "bad", "ai_provider": "openwebui"},
    {"revision": "1", "ai_provider": "bad"},
    {"revision": "1", "ai_provider": "openwebui", "extra": "x"},
])
def test_invalid_plan_form_has_no_effect(setup, monkeypatch, fields):
    app, url, reels, jobs, _, before = prepare(setup)
    calls = generate_mock(monkeypatch)
    original_jobs = jobs.status(before.id)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.post(url + "/plan/generate", data=fields).status_code == 400
    assert not calls
    assert reels.get_draft(before.id) == before
    assert jobs.status(before.id) == original_jobs


@pytest.mark.parametrize("condition", ["revision", "busy", "provider"])
def test_plan_preflight_stops_before_provider_call(setup, monkeypatch, condition):
    app, url, reels, jobs, _, before = prepare(setup)
    calls = generate_mock(monkeypatch)
    if condition == "revision":
        reels.update_draft(before.id, before.revision, scene_direction="Looks up.")
    elif condition == "busy":
        jobs.enqueue(before.id, "image", {"operation": "optimize"})
    else:
        monkeypatch.setattr(web, "provider_missing", lambda *args: True)
    unchanged = reels.get_draft(before.id)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert plan_request(client, url, before).status_code == 409
    assert not calls
    assert reels.get_draft(before.id) == unchanged


def test_late_plan_result_never_overwrites_manual_edit(setup, monkeypatch):
    app, url, reels, _, _, before = prepare(setup)
    generate_mock(monkeypatch, callback=lambda: reels.update_draft(before.id, before.revision,
                                                                 scene_direction="Manual late edit"))
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert plan_request(client, url, before).status_code == 409
    after = reels.get_draft(before.id)
    assert after.scene_direction == "Manual late edit" and not after.scene_plan_json


def test_copy_can_save_generated_plan_without_extra_ai_request(setup, monkeypatch):
    app, url, reels, _, _, before = prepare(setup)
    before = reels.update_draft(before.id, before.revision, scene_direction="")
    async def copy(client, **kwargs):
        assert len(kwargs["context_before"]) >= len(
            next(item["quote"] for item in ManagementStore(reels.uploads).get(before.book_id)["quotes"]
                 if item["quote"].id == before.quote_id).context_before
        )
        return ReelCopy(addition="Text", caption="Caption", image_prompt="Aster crouches near stream",
                        scene_direction=describe_scene_direction(example_plan()), scene_plan=example_plan())
    monkeypatch.setattr(web, "generate_reel_copy", copy)
    monkeypatch.setattr(web, "create_text_client", lambda *args: object())
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.post(url + "/copy", data={"ai_provider": "openwebui"}).status_code == 200
        after = reels.get_draft(before.id)
        assert decode_saved_plan(after.scene_plan_json).plan == example_plan()
        assert "Szenenplan aktuell" in client.get(url).text


def test_plan_cannot_supersede_job_queued_while_ai_is_running(setup, monkeypatch):
    app, url, reels, jobs, _, before = prepare(setup)
    generate_mock(monkeypatch, callback=lambda: jobs.enqueue(before.id, "image", {"operation": "optimize"}))
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert plan_request(client, url, before).status_code == 409
    assert reels.get_draft(before.id) == before
    assert jobs.status(before.id)[0]["state"] == "queued"


def test_copy_keeps_manual_direction_and_adopts_its_generated_plan(setup, monkeypatch):
    app, url, reels, _, _, before = prepare(setup)
    before = reels.update_draft(before.id, before.revision, scene_direction="Aster looks into stream.")
    async def copy(client, **kwargs):
        assert kwargs["scene_direction"] == before.scene_direction
        return ReelCopy(addition="Text", caption="Caption", image_prompt="Aster crouches near stream",
                        scene_direction=describe_scene_direction(example_plan()), scene_plan=example_plan())
    monkeypatch.setattr(web, "generate_reel_copy", copy)
    monkeypatch.setattr(web, "create_text_client", lambda *args: object())
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.post(url + "/copy", data={"ai_provider": "openwebui"}).status_code == 200
        after = reels.get_draft(before.id)
        assert after.scene_direction == before.scene_direction
        assert decode_saved_plan(after.scene_plan_json).plan == example_plan()
        assert "Szenenplan aktuell" in client.get(url).text
