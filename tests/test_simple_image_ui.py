"""Both simple image buttons save and queue one action, never call AI in HTTP."""
import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from bookpromo import web
from test_analysis import setup
from test_chapter_scene_plan_ui import chapter_fixture, assert_media_preserved, plan_json_for
from test_reel_restage_ui import prepare


def quote_fixture(setup):
    settings, *rest = setup
    workflows = Path(__file__).resolve().parents[1] / "workflows"
    settings = settings.model_copy(update={"reel_image_workflow": workflows / "reel-image.json",
                                          "reel_reference_workflow": workflows / "reel-reference.json"})
    return prepare((settings, *rest))


def fields(draft, mode="text", **updates):
    values = {"revision": draft.revision, "image_prompt": draft.image_prompt,
              "scene_direction": draft.scene_direction, "character_ids": list(draft.character_ids),
              "mode": mode, "ai_provider": "openwebui"}
    values.update(updates)
    return values


def queued_payload(reels, draft):
    with reels.connection() as connection:
        return json.loads(connection.execute(
            "select payload_json from local_reel_jobs where draft_id=? and state='queued'",
            (draft.id,),
        ).fetchone()[0])


def no_http_ai(monkeypatch):
    monkeypatch.setattr(web, "create_text_client", lambda *args, **kwargs: pytest.fail("HTTP must only enqueue"))


class SimpleForms(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.depth = 0
        self.simple = False
        self.forms = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            assert self.depth == 0, "Forms must not be nested"
            self.depth += 1
            self.simple = "data-simple-image-form" in attrs
            if self.simple:
                self.forms.append({"action": attrs["action"], "buttons": [], "inputs": set()})
        if self.simple and tag == "button" and attrs.get("name") == "mode":
            self.forms[-1]["buttons"].append(attrs["value"])
            assert "disabled" not in attrs, "Missing plans must not disable primary image actions"
        if self.simple and attrs.get("name"):
            self.forms[-1]["inputs"].add(attrs["name"])

    def handle_endtag(self, tag):
        if tag == "form":
            self.depth -= 1
            self.simple = False


@pytest.mark.parametrize("mode", ["plain", "text", "references"])
def test_quote_simple_action_saves_inputs_and_queues_without_plan(setup, monkeypatch, mode):
    app, url, reels, jobs, _, before = quote_fixture(setup)
    no_http_ai(monkeypatch)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        page = client.get(url).text
        simple = SimpleForms(page).forms
        assert len(simple) == 1
        assert simple[0]["buttons"] == ["plain", "text", "references"]
        assert {"image_prompt", "scene_direction", "character_ids"} <= simple[0]["inputs"]
        assert "ai_provider" not in simple[0]["inputs"]
        assert "Bild mit Beschreibung erzeugen" in page and "Bild mit Referenzbild erzeugen" in page
        assert "Erweiterte Einstellungen" in page and "/image/optimize" in page
        response = client.post(url + "/image/create", data=fields(
            before, mode, image_prompt="Aster leaps across the stream.", scene_direction="Aster looks down."))
        assert response.status_code == 200
        assert client.post(url + "/image/create", data=fields(before, mode)).status_code == 409
    after = reels.get_draft(before.id)
    assert after.image_prompt == "Aster leaps across the stream."
    assert after.scene_direction == "Aster looks down."
    assert after.revision == before.revision + 1
    assert_media_preserved(reels, before, after)
    payload = queued_payload(reels, before)
    assert payload["operation"] == "simple" and payload["strategy"] == f"simple_{mode}"
    assert payload["ai_provider"] == "openwebui" and payload["ai_model_id"]
    assert len(payload["ai_endpoint_hash"]) == 64
    assert payload["source_context"]["quote"] == before.quote_text
    assert not payload.get("scene_plan_json")
    assert len([job for job in jobs.status(before.id) if job["state"] == "queued"]) == 1


def test_reference_quote_needs_no_prior_scene_or_caption(setup, monkeypatch):
    app, url, reels, _, _, before = quote_fixture(setup)
    no_http_ai(monkeypatch)
    with reels.connection() as connection, connection:
        connection.execute("""update local_reel_drafts set scene_image_path=null,scene_image_sha256=null,
                           selected_image_path=null,selected_image_sha256=null,final_caption='',image_stale=0
                           where id=?""", (before.id,))
    before = reels.get_draft(before.id)
    assert before.scene_image_path is None and not before.final_caption
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.post(url + "/image/create", data=fields(before, "references")).status_code == 200
    assert queued_payload(reels, before)["strategy"] == "simple_references"


@pytest.mark.parametrize("problem", ["missing", "file", "hash", "foreign", "provider", "emptycast"])
def test_reference_preflight_has_no_write_or_http_ai(setup, monkeypatch, problem):
    app, url, reels, jobs, characters, before = quote_fixture(setup)
    no_http_ai(monkeypatch)
    character = characters.list(before.book_id)[0]
    values = fields(before, "references")
    if problem == "missing":
        missing = characters.create(before.book_id, name="Companion", image_prompt="A small bird.")
        values["character_ids"] = [missing.id]
    elif problem == "file":
        characters.reference_path(character).unlink()
    elif problem == "hash":
        characters.reference_path(character).write_bytes(b"not the stored portrait")
    elif problem == "foreign":
        values["character_ids"] = ["11111111-1111-4111-8111-111111111111"]
    elif problem == "provider":
        values["ai_provider"] = "not-a-provider"
    else:
        values["character_ids"] = []
    original_jobs = jobs.status(before.id)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.post(url + "/image/create", data=values).status_code in {400, 409}
    assert reels.get_draft(before.id) == before
    assert jobs.status(before.id) == original_jobs


def test_text_mode_allows_description_only_characters(setup, monkeypatch):
    app, url, reels, _, characters, before = quote_fixture(setup)
    no_http_ai(monkeypatch)
    description_only = characters.create(before.book_id, name="Companion", image_prompt="A blue mechanical bird.")
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.post(url + "/image/create", data=fields(
            before, "text", character_ids=[description_only.id])).status_code == 200
    assert reels.get_draft(before.id).character_ids == (description_only.id,)


def test_current_plan_reuses_without_provider_or_http_ai(setup, monkeypatch):
    app, url, reels, _, _, before = quote_fixture(setup)
    before = reels.save_scene_plan(before.id, before.revision, plan_json_for(reels.uploads, before))
    no_http_ai(monkeypatch)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.post(url + "/image/create", data=fields(before, "references", ai_provider="")).status_code == 200
    payload = queued_payload(reels, before)
    assert payload["scene_plan_json"] == before.scene_plan_json
    assert payload["ai_provider"] == "openwebui"  # Global snapshot, even when cached planning skips AI.
    assert payload["image_preset"]["id"] == "flux2-klein-9b-base"


@pytest.mark.parametrize("mode", ["plain", "text", "references"])
def test_chapters_share_three_actions_and_preserve_media(setup, monkeypatch, mode):
    settings, _, book, record, reels, store, jobs, run, base = chapter_fixture(setup, video=True)
    checkpoint = run["plans"][0]
    before = reels.get_draft(checkpoint["draft_id"])
    other = reels.get_draft(run["plans"][1]["draft_id"])
    no_http_ai(monkeypatch)
    with TestClient(web.create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        page = client.get(base).text
        assert len(SimpleForms(page).forms) == len(run["plans"])
        assert all(form["buttons"] == ["plain", "text", "references"] for form in SimpleForms(page).forms)
        route = base + f"/chapters/{checkpoint['chapter_id']}/image/create"
        assert client.post(route, data=fields(before, mode), follow_redirects=False).status_code == 303
        assert client.post(route, data=fields(before, mode), follow_redirects=False).status_code == 409
    assert_media_preserved(reels, before, reels.get_draft(before.id))
    assert reels.get_draft(other.id) == other
    payload = queued_payload(reels, before)
    assert payload["strategy"] == f"simple_{mode}"
    assert payload["source_context"]["quote"] == before.quote_text
    chapter = next(chapter for chapter in record.result.chapters if chapter.id == checkpoint["chapter_id"])
    assert before.quote_text in chapter.source_text
    assert store.latest(book.id, include_plans=True)["plans"][0]["state"] == "image_queued"


@pytest.mark.parametrize("change", [{"mode": "bad"}, {"revision": "bad"}, {"image_prompt": ""},
                                    {"scene_direction": "x" * 2001}, {"unexpected": "field"}])
def test_simple_invalid_form_does_not_mutate_or_queue(setup, monkeypatch, change):
    app, url, reels, jobs, _, before = quote_fixture(setup)
    no_http_ai(monkeypatch)
    original = jobs.status(before.id)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.post(url + "/image/create", data=fields(before, **change)).status_code == 400
    assert reels.get_draft(before.id) == before and jobs.status(before.id) == original


def test_simple_duplicate_mode_is_rejected(setup):
    app, url, reels, jobs, _, before = quote_fixture(setup)
    values = [(key, item) for key, value in fields(before).items()
              for item in (value if isinstance(value, list) else [value])]
    values.append(("mode", "references"))
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.post(url + "/image/create", content=urlencode(values),
                           headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 400
    assert reels.get_draft(before.id) == before
    assert not any(job["state"] == "queued" for job in jobs.status(before.id))


@pytest.mark.parametrize("mode", ["plain", "text", "references"])
@pytest.mark.parametrize("newline", ["\r\n", "\r"])
@pytest.mark.parametrize("encoding", ["urlencoded", "multipart"])
def test_quote_simple_action_accepts_browser_and_pasted_line_endings(setup, monkeypatch, mode, newline, encoding):
    app, url, reels, _, _, before = quote_fixture(setup)
    no_http_ai(monkeypatch)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        values = fields(before, mode,
            image_prompt=newline.join(["Aster by the window.", "Soft evening light."]),
            scene_direction=newline.join(["Aster: both hands free.", "Companion: seated."]))
        if encoding == "multipart":
            # AJAX uses FormData; filename=None makes these text fields, not uploads.
            parts = [(key, (None, str(item))) for key, value in values.items()
                     for item in (value if isinstance(value, list) else [value])]
            response = client.post(url + "/image/create", files=parts)
        else:
            response = client.post(url + "/image/create", data=values)
        assert response.status_code == 200, response.text
    after = reels.get_draft(before.id)
    assert after.image_prompt == "Aster by the window.\nSoft evening light."
    assert after.scene_direction == "Aster: both hands free.\nCompanion: seated."
    assert_media_preserved(reels, before, after)
    assert queued_payload(reels, before)["strategy"] == f"simple_{mode}"


@pytest.mark.parametrize("mode", ["plain", "text", "references"])
def test_chapter_simple_action_accepts_browser_line_endings(setup, monkeypatch, mode):
    settings, _, book, _, reels, _, _, run, base = chapter_fixture(setup, video=True)
    checkpoint = run["plans"][0]
    before = reels.get_draft(checkpoint["draft_id"])
    no_http_ai(monkeypatch)
    route = base + f"/chapters/{checkpoint['chapter_id']}/image/create"
    with TestClient(web.create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        response = client.post(route, data=fields(before, mode,
            image_prompt="Two figures by the window.\r\nSoft light.",
            scene_direction="Aster: standing.\r\nCompanion: sitting."), follow_redirects=False)
        assert response.status_code == 303
    after = reels.get_draft(before.id)
    assert "\r" not in after.image_prompt + after.scene_direction
    assert_media_preserved(reels, before, after)


@pytest.mark.parametrize("control", ["\x00", "\x01", "\x0b", "\x1f"])
def test_simple_line_ending_normalization_keeps_other_control_chars_invalid(setup, monkeypatch, control):
    app, url, reels, jobs, _, before = quote_fixture(setup)
    no_http_ai(monkeypatch)
    original = jobs.status(before.id)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.post(url + "/image/create", data=fields(before,
            scene_direction=f"Aster: standing.\r\nBad{control}input."))
        assert response.status_code == 400
    assert reels.get_draft(before.id) == before
    assert jobs.status(before.id) == original
