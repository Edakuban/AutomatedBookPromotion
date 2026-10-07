"""Restaging is opt-in in durable payloads and does not replace selected images."""

from html.parser import HTMLParser
import json
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from bookpromo.characters import CharacterStore
from bookpromo.reels import ReelJobStore, ReelStore
from bookpromo.web import create_app
from test_analysis import setup
from test_management import analyzed
from test_reels import png_bytes


class Strategies(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.options = {}
        self.strategy = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "select":
            self.strategy = attrs.get("name") == "strategy"
        if tag == "option" and self.strategy:
            self.options[attrs["value"]] = "selected" in attrs

    def handle_endtag(self, tag):
        if tag == "select":
            self.strategy = False


def prepare(setup):
    settings, uploads, book, _, _ = setup
    quote = analyzed(setup).get(book.id)["quotes"][0]["quote"]
    url = f"/books/local/{book.id}/chapters/{quote.chapter_id}/quotes/{quote.id}/reel"
    app = create_app(settings, start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.get(url).status_code == 200
    reels, characters = ReelStore(uploads), CharacterStore(uploads)
    character = characters.create(book.id, name="Aster", image_prompt="Copper fox.")
    character = characters.save(book.id, character.id, character.revision, name=character.name,
                                aliases=["Ast"], description=character.description,
                                image_prompt=character.image_prompt, approved=True)
    character = characters.save_reference_file(book.id, character.id, character.revision, png_bytes((256, 256)))
    draft = reels.list_drafts(book.id)[0]
    draft = reels.update_draft(draft.id, draft.revision,
                              image_prompt="Ast running in a watercolor forest.", character_ids=[character.id])
    jobs = ReelJobStore(reels)
    path, digest = reels.save_artifact(draft.id, "image", png_bytes(), "scene.png")
    jobs.enqueue(draft.id, "image", {"operation": "scene"})
    jobs.finish(jobs.claim(), result={"path": path, "sha256": digest, "candidate": "scene"})
    return app, url, reels, jobs, characters, reels.get_draft(draft.id)


@pytest.mark.parametrize("strategy", [None, "masked", "reference_scene"])
def test_optimize_route_strategy_is_additive_and_preserves_selected_scene(setup, strategy):
    app, url, reels, jobs, _, before = prepare(setup)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        page = client.get(url).text
        assert Strategies(page).options == {"planned_scene": True, "reference_scene": False, "masked": False}
        assert "nicht die Porträtpose" in page
        assert "aktive Auswahl bleiben erhalten" in page
        response = client.post(url + "/image/optimize", data={} if strategy is None else {"strategy": strategy})
    assert response.status_code == 200
    assert reels.get_draft(before.id) == before
    with reels.connection() as connection:
        payload = connection.execute(
            "select payload_json from local_reel_jobs where draft_id=? and state='queued'", (before.id,),
        ).fetchone()[0]
    assert json.loads(payload) == ({"operation": "optimize", "strategy": strategy}
                                   if strategy else {"operation": "optimize"})
    assert jobs.status(before.id)[0]["state"] == "queued"


@pytest.mark.parametrize("fields", [
    [("strategy", "bad")], [("strategy", "")], [("unexpected", "reference_scene")],
    [("strategy", "reference_scene"), ("strategy", "masked")],
    [("strategy", "reference_scene"), ("unexpected", "x")],
    [("scene_direction", "Run through the forest.")],
    [("strategy", "reference_scene"), ("scene_direction", "x" * 2001)],
    [("strategy", "reference_scene"), ("scene_direction", ""), ("scene_direction", "")],
    [("strategy", "masked"), ("scene_direction", "Walk beside the river.")],
])
def test_invalid_strategy_form_never_enqueues(setup, fields):
    app, url, reels, jobs, _, before = prepare(setup)
    original_jobs = jobs.status(before.id)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.post(url + "/image/optimize", content=urlencode(fields),
                               headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code == 400
    assert jobs.status(before.id) == original_jobs
    assert reels.get_draft(before.id) == before


def test_strategy_upload_is_rejected_without_enqueue(setup):
    app, url, reels, jobs, _, before = prepare(setup)
    original_jobs = jobs.status(before.id)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.post(url + "/image/optimize", files={"strategy": ("x.txt", b"reference_scene")})
    assert response.status_code == 400
    assert jobs.status(before.id) == original_jobs
    assert reels.get_draft(before.id) == before


def test_non_form_strategy_cannot_silently_trigger_legacy_mode(setup):
    app, url, reels, jobs, _, before = prepare(setup)
    original_jobs = jobs.status(before.id)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.post(url + "/image/optimize", json={"strategy": "reference_scene"})
    assert response.status_code == 415
    assert jobs.status(before.id) == original_jobs
    assert reels.get_draft(before.id) == before


@pytest.mark.parametrize("strategy,direction", [
    ("reference_scene", "  Ast leaps across a fallen tree; carry no held objects.  "),
    ("reference_scene", "x" * 2000),
    ("reference_scene", ""),
    ("masked", " \n "),
])
def test_scene_direction_is_job_scoped_optional_and_does_not_edit_draft(setup, strategy, direction):
    app, url, reels, jobs, _, before = prepare(setup)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        page = client.get(url).text
        assert "Pose &amp; Requisiten präzisieren (optional)" in page
        assert 'name="scene_direction" maxlength="2000"' in page
        assert "auch bei Tieren, Kreaturen oder mechanischen Figuren" in page
        assert "garantiert keine fehlerfreie Bildanatomie" in page
        assert "Der Bildprompt wird dadurch nicht geändert" in page
        response = client.post(url + "/image/optimize", data={
            "strategy": strategy, "scene_direction": direction,
        })
    assert response.status_code == 200
    assert reels.get_draft(before.id) == before
    with reels.connection() as connection:
        payload = json.loads(connection.execute(
            "select payload_json from local_reel_jobs where draft_id=? and state='queued'", (before.id,),
        ).fetchone()[0])
    expected = {"operation": "optimize", "strategy": strategy}
    if direction.strip():
        expected["scene_direction"] = direction.strip()
    assert payload == expected
    assert jobs.status(before.id)[0]["state"] == "queued"


def test_scene_direction_upload_is_rejected_without_enqueue(setup):
    app, url, reels, jobs, _, before = prepare(setup)
    original_jobs = jobs.status(before.id)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.post(url + "/image/optimize", data={"strategy": "reference_scene"},
                               files={"scene_direction": ("direction.txt", b"Jump.")})
    assert response.status_code == 400
    assert jobs.status(before.id) == original_jobs
    assert reels.get_draft(before.id) == before


def test_restage_requires_every_selected_reference_but_masked_remains_available(setup):
    app, url, reels, jobs, characters, before = prepare(setup)
    missing = characters.create(before.book_id, name="Companion", image_prompt="A bird.")
    before = reels.update_draft(before.id, before.revision,
                               character_ids=[*before.character_ids, missing.id])
    # Changing the cast invalidates media: simulate an existing current scene as usual.
    path, digest = reels.save_artifact(before.id, "image", png_bytes((66, 98)), "current-scene.png")
    jobs.enqueue(before.id, "image", {"operation": "scene"})
    jobs.finish(jobs.claim(), result={"path": path, "sha256": digest, "candidate": "scene"})
    before = reels.get_draft(before.id)
    original_jobs = jobs.status(before.id)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert "jeder ausgewählte Charakter ein Referenzbild" in client.get(url).text
        response = client.post(url + "/image/optimize", data={"strategy": "reference_scene"})
        assert response.status_code == 409
        assert jobs.status(before.id) == original_jobs
        assert reels.get_draft(before.id) == before
        assert client.post(url + "/image/optimize", data={"strategy": "masked"}).status_code == 200
