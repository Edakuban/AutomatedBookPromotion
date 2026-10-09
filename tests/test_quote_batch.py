from io import BytesIO

from fastapi.testclient import TestClient

from bookpromo.characters import CharacterStore
from bookpromo.reel_content import ReelCopy
from bookpromo.reel_worker import run_reel_once
from bookpromo.reels import ReelJobStore, ReelStore
from bookpromo.scene_plan import ScenePlan
from bookpromo.web import create_app
from test_analysis import setup
from test_management import analyzed
from test_reels import png_bytes, wav_bytes


def chapter_fixture(setup):
    settings, uploads, book, _, _ = setup
    management = analyzed(setup).get(book.id)
    quote = management["quotes"][0]["quote"]
    base = f"/books/local/{book.id}/chapters/{quote.chapter_id}"
    return settings, uploads, book, quote, base


def test_chapter_page_has_four_batch_actions_and_text_job_is_durable(setup, monkeypatch):
    settings, uploads, book, quote, base = chapter_fixture(setup)
    app = create_app(settings, start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        page = client.get(base)
        assert page.status_code == 200
        for label in (
            "Alle Texte erstellen", "Bilder mit Beschreibung",
            "Bilder mit Charakter", "Freigegebene Reels erstellen",
        ):
            assert label in page.text
        response = client.post(base + "/quotes/batch/text", follow_redirects=False)
        assert response.status_code == 303 and "batch=text" in response.headers["location"]

    reels, jobs = ReelStore(uploads), ReelJobStore(ReelStore(uploads))
    draft = reels.list_drafts(book.id)[0]
    assert any(item["kind"] == "prompt" and item["state"] == "queued"
               for item in jobs.status(draft.id))

    plan = ScenePlan(
        setting="A station at night", composition="Vertical medium shot",
        art_direction="Cinematic", actors=[{"name": "Mara", "pose": "Waits by a lamp"}],
    )

    async def fake_copy(*args, **kwargs):
        return ReelCopy(
            addition="Was wartet hinter dem letzten Zug?",
            image_prompt="Mara waits beside a station lamp at night.",
            caption=quote.text + "\n\nWas wartet hinter dem letzten Zug?",
            scene_direction="Mara stands beside the lamp.", scene_plan=plan,
        )

    monkeypatch.setattr("bookpromo.reel_worker.create_text_client", lambda *a, **k: object())
    monkeypatch.setattr("bookpromo.reel_worker.generate_reel_copy", fake_copy)
    assert run_reel_once(uploads, settings)
    draft = reels.get_draft(draft.id)
    assert draft.final_caption and draft.image_prompt
    assert draft.character_ids == (CharacterStore(uploads).list(book.id)[0].id,)

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.post(base + "/quotes/batch/images", data={"mode": "text"},
                               follow_redirects=False)
        assert response.status_code == 303 and "batch=image_text" in response.headers["location"]
    assert any(item["kind"] == "image" and item["state"] == "queued"
               for item in jobs.status(draft.id))


def test_only_reviewed_image_enters_batch_reel_and_motion_chains_video(setup, monkeypatch):
    settings, uploads, book, quote, base = chapter_fixture(setup)
    app = create_app(settings, start_worker=False)
    reel_url = f"{base}/quotes/{quote.id}/reel"
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.get(reel_url).status_code == 200

    reels, jobs = ReelStore(uploads), ReelJobStore(ReelStore(uploads))
    draft = reels.list_drafts(book.id)[0]
    draft = reels.update_draft(
        draft.id, draft.revision,
        caption_addition="Eine Frage.", final_caption=quote.text + "\n\nEine Frage.",
        image_prompt="A cinematic station at night",
    )
    reels.save_audio(book.id, wav_bytes(20), "song.wav")
    jobs.enqueue(draft.id, "image")
    image_job = jobs.claim(kinds={"image"}, now=10)
    image_path, image_hash = reels.save_artifact(
        draft.id, "image", BytesIO(png_bytes().getvalue()), "scene.png",
    )
    assert jobs.finish(image_job, result={"path": image_path, "sha256": image_hash}, now=11)
    draft = reels.get_draft(draft.id)
    assert draft.selected_image_path and not draft.image_approved

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        page = client.get(base)
        assert "Bild freigeben" in page.text
        approved = client.post(reel_url + "/image/approve", data={
            "revision": str(draft.revision), "image_sha256": draft.selected_image_sha256,
        }, follow_redirects=False)
        assert approved.status_code == 303
        queued = client.post(base + "/quotes/batch/reels", follow_redirects=False)
        assert queued.status_code == 303 and "batch=reels" in queued.headers["location"]

    draft = reels.get_draft(draft.id)
    assert draft.image_approved and draft.audio_track_id and draft.audio_start_ms == 0

    async def fake_motion(*args, **kwargs):
        return "The camera arcs gently as the station light pulses to the rhythm."

    monkeypatch.setattr("bookpromo.reel_worker.create_text_client", lambda *a, **k: object())
    monkeypatch.setattr("bookpromo.reel_worker.generate_motion_prompt", fake_motion)
    assert run_reel_once(uploads, settings)
    draft = reels.get_draft(draft.id)
    assert draft.video_prompt
    assert any(item["kind"] == "video" and item["state"] == "queued"
               for item in jobs.status(draft.id))
