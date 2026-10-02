import json
import sqlite3

from bookpromo.characters import CharacterStore, character_scene_prompt
from bookpromo.config import Settings
from bookpromo.management import ManagementStore
from bookpromo.reel_worker import run_reel_once
from test_reels import setup, new_draft, png_bytes


def test_scene_worker_never_injects_legacy_book_location(setup, monkeypatch, tmp_path):
    uploads, book_id, reels, jobs = setup
    draft = new_draft(reels, book_id)
    draft = reels.update_draft(
        draft.id, draft.revision,
        image_prompt="Sam wakes in bed beside Lysandra, inside his fortress. Cinematic dark fantasy.",
    )
    # No book-profile access at the media stage: it could inject an unrelated street scene.
    monkeypatch.setattr(ManagementStore, "get", lambda *args: (_ for _ in ()).throw(
        AssertionError("Image rendering must not append the raw book profile")
    ))
    captured = {}
    generated = tmp_path / "scene.png"
    generated.write_bytes(png_bytes().read())

    class Generator:
        def __init__(self, *args, **kwargs):
            pass

        def generate_image(self, *, reel_id, image_prompt):
            captured["prompt"] = image_prompt
            return generated

    monkeypatch.setattr("bookpromo.reel_worker.ReelGenerator", Generator)
    monkeypatch.setattr("bookpromo.reel_worker.ffmpeg_binary", lambda: "ffmpeg")
    jobs.enqueue(draft.id, "image", {"operation": "scene"})
    assert run_reel_once(uploads, Settings(_env_file=None, app_data_dir=uploads.root))
    assert captured["prompt"] == draft.image_prompt
    with sqlite3.connect(uploads.db_path) as connection:
        result = json.loads(connection.execute(
            "select result_json from local_reel_jobs where draft_id=?", (draft.id,),
        ).fetchone()[0])
    assert result["effective_image_prompt"] == captured["prompt"]


def test_character_references_do_not_force_standing_portraits(setup):
    uploads, book_id, _, _ = setup
    characters = CharacterStore(uploads)
    sam = characters.create(book_id, name="Sam", description="A demon lord.",
                            image_prompt="Red eyes, dark hair.")
    prompt = character_scene_prompt("Sam lies in bed inside a fortress.", [sam])
    assert prompt.startswith("Sam lies in bed inside a fortress.")
    assert "main scene prompt is authoritative" in prompt
    assert "Never replace its scene with a standing portrait" in prompt
    assert "MANDATORY SCENE IDENTITY LAYOUT" not in prompt
    assert "human visual identity" not in prompt
