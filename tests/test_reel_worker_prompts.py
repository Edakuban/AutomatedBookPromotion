import json
import sqlite3
from uuid import uuid4

import pytest

from bookpromo.characters import CharacterStore, character_scene_prompt
from bookpromo.config import Settings
from bookpromo.management import ManagementStore
from bookpromo.reel_generation import (
    CHARACTER_IDENTITY_STRATEGY, PLANNED_SCENE_STRATEGY, REFERENCE_SCENE_STRATEGY,
    ReelGenerationError, build_planned_reference_prompt, build_reference_scene_prompt,
)
from bookpromo.reel_worker import run_reel_once
from bookpromo.scene_plan import compile_scene_plan, scene_plan_fingerprint
from test_reel_generation import resolved_reference_plan
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
    assert "reference-image prompt: Red eyes, dark hair." in prompt
    assert "A demon lord" not in prompt
    assert "references are authoritative for identity, body build, clothing and worn accessories" in prompt
    assert "Ignore the references' rendering styles, backgrounds, poses and held props" in prompt
    assert "authoritative for location, action, pose, clothing" not in prompt


def test_failed_character_mask_keeps_existing_images_and_selection(setup, monkeypatch):
    uploads, book_id, reels, jobs = setup
    characters = CharacterStore(uploads)
    sam = characters.create(book_id, name="Sam", image_prompt="A human man with dark hair.")
    sam = characters.save_reference_file(book_id, sam.id, sam.revision, png_bytes((256, 256)))
    draft = new_draft(reels, book_id)
    draft = reels.update_draft(draft.id, draft.revision,
                               image_prompt="Sam inside a fortress.", character_ids=[sam.id])
    for candidate, size in (("scene", (64, 96)), ("optimized", (65, 97))):
        path, digest = reels.save_artifact(draft.id, "image", png_bytes(size), f"{candidate}.png")
        jobs.enqueue(draft.id, "image", {"operation": "scene" if candidate == "scene" else "optimize"})
        assert jobs.finish(jobs.claim(), result={"path": path, "sha256": digest, "candidate": candidate})
    draft = reels.get_draft(draft.id)
    before = reels.select_image(draft.id, draft.revision, "optimized")
    original_bytes = reels.artifact_path(before, "image").read_bytes()

    class Generator:
        def __init__(self, *args, **kwargs):
            pass

        def apply_character_references_masked(self, *, reel_id, scene_image_path, references):
            assert scene_image_path == reels.candidate_image_path(before, "scene")
            assert references[0].reference_image_path == characters.reference_path(sam)
            raise ReelGenerationError(
                "Die Figur Sam wurde im Szenenbild nicht sicher erkannt. "
                "Die Optimierung wurde abgebrochen; das bisherige Bild bleibt erhalten."
            )

    monkeypatch.setattr("bookpromo.reel_worker.ReelGenerator", Generator)
    monkeypatch.setattr("bookpromo.reel_worker.ffmpeg_binary", lambda: "ffmpeg")
    jobs.enqueue(draft.id, "image", {"operation": "optimize"})
    assert run_reel_once(uploads, Settings(_env_file=None, app_data_dir=uploads.root))
    after = reels.get_draft(draft.id)
    assert after.state == "failed"
    assert after.revision == before.revision
    for field in ("scene_image_path", "scene_image_sha256", "optimized_image_path",
                  "optimized_image_sha256", "selected_image_path", "selected_image_sha256",
                  "selected_image_source"):
        assert getattr(after, field) == getattr(before, field)
    assert reels.artifact_path(after, "image").read_bytes() == original_bytes
    failed = [job for job in jobs.status(draft.id) if job["state"] == "failed"]
    assert len(failed) == 1
    assert "bisherige Bild bleibt erhalten" in failed[0]["error"]


@pytest.mark.parametrize("strategy,scene_direction", [
    ("masked", None), ("reference_scene", None),
    ("reference_scene", "  Turn toward the window in a visibly different natural stance. No held objects.  "),
])
def test_reference_jobs_keep_same_named_characters_separate_between_books(setup, monkeypatch, tmp_path, strategy, scene_direction):
    uploads, first_book, reels, jobs = setup
    second_book = str(uuid4())
    with sqlite3.connect(uploads.db_path) as connection, connection:
        row = list(connection.execute("select * from local_books where id=?", (first_book,)).fetchone())
        row[0], row[1], row[2], row[3] = second_book, str(uuid4()), str(uuid4()), "Anderes Buch"
        connection.execute("insert into local_books values (?,?,?,?,?,?,?,?,?)", row)
    characters = CharacterStore(uploads)
    expected, received = {}, {}

    class Generator:
        def __init__(self, *args, **kwargs):
            pass

        def apply_character_references_masked(self, *, reel_id, scene_image_path, references):
            assert strategy == "masked"
            return self.capture(reel_id, references)

        def restage_character_references(self, *, reel_id, scene_image_path, references, scene_prompt, **kwargs):
            assert strategy == "reference_scene"
            assert scene_prompt == "The existing character in the book's scene style."
            expected_kwargs = {"scene_direction": scene_direction.strip()} if scene_direction else {}
            assert kwargs == expected_kwargs
            received[f'{reel_id}-effective'] = build_reference_scene_prompt(scene_prompt, references, **kwargs)
            return self.capture(reel_id, references)

        def capture(self, reel_id, references):
            assert len(references) == 1
            reference = references[0]
            assert (reference.reference_image_path, reference.identity_prompt) == expected[reel_id]
            received[reel_id] = reference.reference_image_path
            path = tmp_path / f"{reel_id}.png"
            path.write_bytes(png_bytes((80, 120)).read())
            return path

    monkeypatch.setattr("bookpromo.reel_worker.ReelGenerator", Generator)
    monkeypatch.setattr("bookpromo.reel_worker.ffmpeg_binary", lambda: "ffmpeg")
    settings = Settings(_env_file=None, app_data_dir=uploads.root)
    for book_id, prompt, size in (
        (first_book, "Older woman with curly silver hair. Watercolor reference portrait.", (256, 256)),
        (second_book, "Copper clockwork fox with green eyes. Photorealistic reference portrait.", (300, 320)),
    ):
        character = characters.create(book_id, name="Alex", image_prompt=prompt)
        character = characters.save_reference_file(book_id, character.id, character.revision, png_bytes(size))
        draft = new_draft(reels, book_id)
        draft = reels.update_draft(draft.id, draft.revision,
                                   image_prompt="The existing character in the book's scene style.",
                                   character_ids=[character.id])
        path, digest = reels.save_artifact(draft.id, "image", png_bytes(), "scene.png")
        jobs.enqueue(draft.id, "image", {"operation": "scene"})
        assert jobs.finish(jobs.claim(), result={"path": path, "sha256": digest, "candidate": "scene"})
        expected[draft.id] = (characters.reference_path(character), prompt)
        before = reels.get_draft(draft.id)
        payload = {"operation": "optimize", "strategy": strategy}
        if scene_direction is not None:
            payload["scene_direction"] = scene_direction
        jobs.enqueue(draft.id, "image", payload)
        assert run_reel_once(uploads, settings)
        with sqlite3.connect(uploads.db_path) as connection:
            result = json.loads(connection.execute(
                "select result_json from local_reel_jobs where draft_id=? "
                "and payload_json like '%optimize%'", (draft.id,),
            ).fetchone()[0])
        if strategy == "masked":
            assert result["identity_transfer_strategy"] == CHARACTER_IDENTITY_STRATEGY
            assert result["optimization_strategy"] == "semantic-masks-sequential"
            assert result["mask_validation_version"] == 2
        else:
            assert result["identity_transfer_strategy"] == REFERENCE_SCENE_STRATEGY
            assert result["optimization_strategy"] == "reference_scene"
            assert "mask_validation_version" not in result
            assert "The existing character in the book's scene style." in result["effective_image_prompt"]
            assert result["effective_image_prompt"] == received[f'{draft.id}-effective']
        if scene_direction:
            assert result["scene_direction"] == scene_direction.strip()
            assert scene_direction.strip() in result["effective_image_prompt"]
        else:
            assert "scene_direction" not in result
        assert result["character_snapshot"][0]["id"] == character.id
        after = reels.get_draft(draft.id)
        assert after.scene_image_path == path and after.selected_image_source == "scene"
        assert after.optimized_image_path and after.optimized_image_path != path
        assert after.image_prompt == before.image_prompt
        assert after.selected_image_path == before.selected_image_path
        assert after.selected_image_sha256 == before.selected_image_sha256
    received_paths = [value for key, value in received.items() if not key.endswith('-effective')]
    assert len(received_paths) == 2 and len(set(received_paths)) == 2


@pytest.mark.parametrize("failure", [None, "render", "missing_reference", "invalid_strategy"])
def test_restage_worker_orders_cast_preserves_selection_and_fails_closed(setup, monkeypatch, tmp_path, failure):
    uploads, book_id, reels, jobs = setup
    characters = CharacterStore(uploads)
    cast = []
    for name, aliases in (("Samuel", ["Sam"]), ("Lysandra", ["Lys"])):
        character = characters.create(book_id, name=name,
                                      image_prompt="Reference portrait in an unrelated style and location.")
        character = characters.save(book_id, character.id, character.revision, name=name,
                                    aliases=aliases, description=character.description,
                                    image_prompt=character.image_prompt, approved=True)
        if not (failure == "missing_reference" and name == "Samuel"):
            character = characters.save_reference_file(book_id, character.id, character.revision, png_bytes((256, 256)))
        cast.append(character)
    draft = new_draft(reels, book_id)
    draft = reels.update_draft(draft.id, draft.revision,
                              image_prompt="Lys and Sam running in a watercolor forest.",
                              character_ids=[character.id for character in cast])
    for candidate, size in (("scene", (64, 96)), ("optimized", (65, 97))):
        path, digest = reels.save_artifact(draft.id, "image", png_bytes(size), f"{candidate}.png")
        jobs.enqueue(draft.id, "image", {"operation": "scene" if candidate == "scene" else "optimize"})
        jobs.finish(jobs.claim(), result={"path": path, "sha256": digest, "candidate": candidate})
    current = reels.get_draft(draft.id)
    before = reels.select_image(current.id, current.revision, "optimized")
    captured = {}
    output = tmp_path / "restaged.png"
    output.write_bytes(png_bytes((80, 120)).read())

    class Generator:
        def __init__(self, *args, **kwargs):
            pass

        def restage_character_references(self, *, reel_id, scene_image_path, references, scene_prompt):
            assert failure not in {"missing_reference", "invalid_strategy"}
            assert scene_image_path == reels.candidate_image_path(before, "scene")
            assert scene_prompt == before.image_prompt
            assert [item.name for item in references] == ["Lysandra", "Samuel"]
            assert [item.aliases for item in references] == [("Lys",), ("Sam",)]
            captured["effective"] = build_reference_scene_prompt(scene_prompt, references)
            if failure == "render":
                raise ReelGenerationError("Neuinszenierung fehlgeschlagen; bestehende Bilder bleiben erhalten.")
            return output

    monkeypatch.setattr("bookpromo.reel_worker.ReelGenerator", Generator)
    monkeypatch.setattr("bookpromo.reel_worker.ffmpeg_binary", lambda: "ffmpeg")
    jobs.enqueue(draft.id, "image", {
        "operation": "optimize", "strategy": "bad" if failure == "invalid_strategy" else "reference_scene",
    })
    assert run_reel_once(uploads, Settings(_env_file=None, app_data_dir=uploads.root))
    after = reels.get_draft(draft.id)
    for field in ("scene_image_path", "scene_image_sha256", "selected_image_source",
                  "selected_image_path", "selected_image_sha256"):
        assert getattr(after, field) == getattr(before, field)
    assert reels.artifact_path(after, "image").read_bytes() == reels.artifact_path(before, "image").read_bytes()
    if failure:
        assert after.optimized_image_path == before.optimized_image_path
        assert jobs.status(draft.id)[0]["state"] == "failed"
    else:
        assert after.optimized_image_path != before.optimized_image_path
        with sqlite3.connect(uploads.db_path) as connection:
            result = json.loads(connection.execute(
                "select result_json from local_reel_jobs where draft_id=? and payload_json like '%reference_scene%'",
                (draft.id,),
            ).fetchone()[0])
        assert result["effective_image_prompt"] == captured["effective"]
        assert result["identity_transfer_strategy"] == REFERENCE_SCENE_STRATEGY
        assert "mask_validation_version" not in result
        assert [item["id"] for item in result["character_snapshot"]] == [cast[1].id, cast[0].id]
        assert [item["reference_image_index"] for item in result["character_snapshot"]] == [1, 2]
        assert [item["aliases"] for item in result["character_snapshot"]] == [["Lys"], ["Sam"]]


def direction_worker_fixture(setup, *, image_prompt='Any Character reads a map in a pastel village.', scene_direction=''):
    uploads, book_id, reels, jobs = setup
    characters = CharacterStore(uploads)
    character = characters.create(book_id, name='Any Character', image_prompt='A book-specific reference.')
    character = characters.save_reference_file(book_id, character.id, character.revision, png_bytes((256, 256)))
    draft = new_draft(reels, book_id)
    draft = reels.update_draft(draft.id, draft.revision,
                               image_prompt=image_prompt, scene_direction=scene_direction, character_ids=[character.id])
    for candidate, size in (('scene', (64, 96)), ('optimized', (65, 97))):
        path, digest = reels.save_artifact(draft.id, 'image', png_bytes(size), f'{candidate}.png')
        jobs.enqueue(draft.id, 'image', {'operation': 'scene' if candidate == 'scene' else 'optimize'})
        jobs.finish(jobs.claim(), result={'path': path, 'sha256': digest, 'candidate': candidate})
    current = reels.get_draft(draft.id)
    before = reels.select_image(current.id, current.revision, 'optimized')
    return uploads, reels, jobs, before


@pytest.mark.parametrize('operation,strategy,direction', [
    ('optimize', 'reference_scene', None),
    ('optimize', 'reference_scene', True),
    ('optimize', 'reference_scene', {'pose': 'different'}),
    ('optimize', 'reference_scene', ['different']),
    ('optimize', 'reference_scene', 12),
    ('optimize', 'reference_scene', 'x' * 2001),
    ('optimize', 'reference_scene', ' ' * 2000 + 'x'),
    ('optimize', 'reference_scene', ' ' * 2001),
    ('optimize', 'masked', 'Turn toward a window.'),
    ('optimize', None, 'Turn toward a window.'),
    ('scene', None, 'Turn toward a window.'),
    ('scene', 'reference_scene', 'Turn toward a window.'),
])
def test_invalid_or_unsupported_scene_direction_fails_before_generation_preserving_every_image(setup, monkeypatch, operation, strategy, direction):
    uploads, reels, jobs, before = direction_worker_fixture(setup)
    images = {
        source: reels.candidate_image_path(before, source).read_bytes()
        for source in ('scene', 'optimized', 'selected')
    }

    class Generator:
        def __init__(self, *args, **kwargs):
            pytest.fail('Invalid scene directions must fail before constructing the generator')

    monkeypatch.setattr('bookpromo.reel_worker.ReelGenerator', Generator)
    payload = {'operation': operation, 'scene_direction': direction}
    if strategy is not None:
        payload['strategy'] = strategy
    jobs.enqueue(before.id, 'image', payload)
    assert run_reel_once(uploads, Settings(_env_file=None, app_data_dir=uploads.root))
    after = reels.get_draft(before.id)
    assert jobs.status(before.id)[0]['state'] == 'failed'
    assert 'Szenenregie' in jobs.status(before.id)[0]['error']
    for field in ('image_prompt', 'scene_image_path', 'scene_image_sha256', 'optimized_image_path',
                  'optimized_image_sha256', 'selected_image_path', 'selected_image_sha256', 'selected_image_source'):
        assert getattr(after, field) == getattr(before, field)
    for source, content in images.items():
        assert reels.candidate_image_path(after, source).read_bytes() == content


def test_revision_only_caption_edit_supersedes_planned_job_before_generator(setup, monkeypatch):
    uploads, reels, jobs, before = direction_worker_fixture(setup)
    saved_json, _ = saved_worker_plan(uploads, before)
    images = {source: reels.candidate_image_path(before, source).read_bytes() for source in ('scene', 'optimized', 'selected')}
    job = jobs.enqueue(before.id, 'image', {
        'operation': 'optimize', 'strategy': 'planned_scene', 'scene_plan_json': saved_json,
    })
    edited = reels.update_draft(before.id, before.revision, caption_addition='Caption edited after enqueue.')
    current_saved_json, _ = saved_worker_plan(uploads, edited)
    assert json.loads(current_saved_json)['fingerprint'] == json.loads(saved_json)['fingerprint']

    class Generator:
        def __init__(self, *args, **kwargs):
            pytest.fail('A revision-superseded planned job must not construct the generator')

    monkeypatch.setattr('bookpromo.reel_worker.ReelGenerator', Generator)
    assert run_reel_once(uploads, Settings(_env_file=None, app_data_dir=uploads.root))
    with sqlite3.connect(uploads.db_path) as connection:
        row = connection.execute('select state,result_json,error from local_reel_jobs where id=?', (job.id,)).fetchone()
    assert row == ('stale', '{}', None)
    after = reels.get_draft(before.id)
    for field in ('revision', 'caption_addition', 'image_prompt', 'scene_direction', 'scene_plan_json',
                  'scene_image_path', 'scene_image_sha256', 'optimized_image_path', 'optimized_image_sha256',
                  'selected_image_path', 'selected_image_sha256', 'selected_image_source'):
        assert getattr(after, field) == getattr(edited, field)
    for source, content in images.items():
        assert reels.candidate_image_path(after, source).read_bytes() == content


@pytest.mark.parametrize('direction', ['', '  \n\t  '])
@pytest.mark.parametrize('strategy', ['masked', 'reference_scene'])
def test_empty_scene_direction_keeps_legacy_call_signatures_and_has_no_audit_field(setup, monkeypatch, tmp_path, direction, strategy):
    uploads, reels, jobs, before = direction_worker_fixture(setup)
    output = tmp_path / 'candidate.png'
    output.write_bytes(png_bytes((80, 120)).read())

    class Generator:
        def __init__(self, *args, **kwargs):
            pass

        def apply_character_references_masked(self, *, reel_id, scene_image_path, references):
            assert strategy == 'masked'
            return output

        def restage_character_references(self, *, reel_id, scene_image_path, references, scene_prompt):
            assert strategy == 'reference_scene'
            assert scene_prompt == before.image_prompt
            return output

    monkeypatch.setattr('bookpromo.reel_worker.ReelGenerator', Generator)
    monkeypatch.setattr('bookpromo.reel_worker.ffmpeg_binary', lambda: 'ffmpeg')
    job = jobs.enqueue(before.id, 'image', {'operation': 'optimize', 'strategy': strategy, 'scene_direction': direction})
    assert run_reel_once(uploads, Settings(_env_file=None, app_data_dir=uploads.root))
    with sqlite3.connect(uploads.db_path) as connection:
        result = json.loads(connection.execute('select result_json from local_reel_jobs where id=?', (job.id,)).fetchone()[0])
    assert 'scene_direction' not in result
    after = reels.get_draft(before.id)
    assert after.image_prompt == before.image_prompt
    assert after.selected_image_path == before.selected_image_path
    assert after.selected_image_sha256 == before.selected_image_sha256


@pytest.mark.parametrize('direction', ['  Shift to the side and study one unfolded map.  ', 'x' * 2000])
def test_scene_direction_audit_matches_render_prompt_and_keeps_previously_selected_optimized_image(setup, monkeypatch, tmp_path, direction):
    uploads, reels, jobs, before = direction_worker_fixture(setup)
    selected_bytes = reels.candidate_image_path(before, 'selected').read_bytes()
    output = tmp_path / 'new-optimized.png'
    output.write_bytes(png_bytes((80, 120)).read())
    captured = {}

    class Generator:
        def __init__(self, *args, **kwargs):
            pass

        def restage_character_references(self, *, reel_id, scene_image_path, references, scene_prompt, scene_direction):
            assert scene_direction == direction.strip()
            assert scene_prompt == before.image_prompt
            assert scene_image_path == reels.candidate_image_path(before, 'scene')
            captured['prompt'] = build_reference_scene_prompt(scene_prompt, references, scene_direction=scene_direction)
            return output

    monkeypatch.setattr('bookpromo.reel_worker.ReelGenerator', Generator)
    monkeypatch.setattr('bookpromo.reel_worker.ffmpeg_binary', lambda: 'ffmpeg')
    job = jobs.enqueue(before.id, 'image', {'operation': 'optimize', 'strategy': 'reference_scene', 'scene_direction': direction})
    assert run_reel_once(uploads, Settings(_env_file=None, app_data_dir=uploads.root))
    with sqlite3.connect(uploads.db_path) as connection:
        result = json.loads(connection.execute('select result_json from local_reel_jobs where id=?', (job.id,)).fetchone()[0])
    assert result['scene_direction'] == direction.strip()
    assert result['effective_image_prompt'] == captured['prompt']
    assert result['identity_transfer_strategy'] == REFERENCE_SCENE_STRATEGY
    after = reels.get_draft(before.id)
    assert after.optimized_image_path != before.optimized_image_path
    assert after.image_prompt == before.image_prompt
    assert after.selected_image_source == before.selected_image_source == 'optimized'
    assert after.selected_image_path == before.selected_image_path
    assert after.selected_image_sha256 == before.selected_image_sha256
    assert reels.candidate_image_path(after, 'selected').read_bytes() == selected_bytes


def saved_worker_plan(uploads, draft, *, names=None):
    selected = [item for item in CharacterStore(uploads).list(draft.book_id) if item.id in draft.character_ids]
    plan = resolved_reference_plan(names or [item.name for item in selected])
    fingerprint = scene_plan_fingerprint(
        quote=draft.quote_text, image_prompt=draft.image_prompt, scene_direction=draft.scene_direction,
        art_direction=ManagementStore(uploads).get(draft.book_id)['details'].image_prompt_base,
        characters=[{
            'id': item.id, 'name': item.name, 'aliases': list(item.aliases),
            'revision': item.revision, 'reference_image_sha256': item.reference_image_sha256,
        } for item in selected],
    )
    return json.dumps({'fingerprint': fingerprint, 'plan': plan.model_dump()}), plan


def test_planned_scene_worker_renders_only_resolved_plan_and_preserves_selected_image(setup, monkeypatch, tmp_path):
    uploads, reels, jobs, before = direction_worker_fixture(
        setup,
        image_prompt='Any Character holds a sword or silver daggers.',
        scene_direction='Both hands hold a cigar. Raw contradictory staging source.',
    )
    selected_bytes = reels.candidate_image_path(before, 'selected').read_bytes()
    saved_json, plan = saved_worker_plan(uploads, before)
    output = tmp_path / 'resolved.png'
    output.write_bytes(png_bytes((80, 120)).read())
    captured = {}

    class Generator:
        def __init__(self, *args, **kwargs):
            pass

        def restage_character_references(self, *, reel_id, scene_image_path, references, scene_plan):
            assert scene_plan == plan
            assert scene_image_path == reels.candidate_image_path(before, 'scene')
            captured['prompt'] = build_planned_reference_prompt(scene_plan, references)
            return output

    monkeypatch.setattr('bookpromo.reel_worker.ReelGenerator', Generator)
    job = jobs.enqueue(before.id, 'image', {
        'operation': 'optimize', 'strategy': 'planned_scene', 'scene_plan_json': saved_json,
    })
    assert run_reel_once(uploads, Settings(_env_file=None, app_data_dir=uploads.root))
    with sqlite3.connect(uploads.db_path) as connection:
        result = json.loads(connection.execute('select result_json from local_reel_jobs where id=?', (job.id,)).fetchone()[0])
    assert result['effective_image_prompt'] == captured['prompt']
    assert before.image_prompt not in captured['prompt']
    assert before.scene_direction not in captured['prompt']
    assert result['scene_plan_json'] == saved_json
    assert result['identity_transfer_strategy'] == PLANNED_SCENE_STRATEGY
    assert result['optimization_strategy'] == 'planned_scene'
    assert 'scene_direction' not in result and 'mask_validation_version' not in result
    after = reels.get_draft(before.id)
    assert after.optimized_image_path != before.optimized_image_path
    assert after.selected_image_source == before.selected_image_source == 'optimized'
    assert after.selected_image_path == before.selected_image_path
    assert after.selected_image_sha256 == before.selected_image_sha256
    assert reels.candidate_image_path(after, 'selected').read_bytes() == selected_bytes


@pytest.mark.parametrize('failure', [
    'missing', 'malformed', 'stale', 'unbound_actor', 'missing_reference',
    'changed_profile', 'changed_character', 'changed_reference', 'changed_direction', 'tampered_reference',
])
def test_planned_scene_worker_preflight_fails_before_generator_and_preserves_all_images(setup, monkeypatch, failure):
    uploads, reels, jobs, before = direction_worker_fixture(setup)
    saved_json, _ = saved_worker_plan(
        uploads, before, names=['Unrelated Character'] if failure == 'unbound_actor' else None,
    )
    images = {source: reels.candidate_image_path(before, source).read_bytes() for source in ('scene', 'optimized', 'selected')}
    payload = {'operation': 'optimize', 'strategy': 'planned_scene', 'scene_plan_json': saved_json}
    if failure == 'missing':
        payload.pop('scene_plan_json')
    elif failure == 'malformed':
        payload['scene_plan_json'] = '{broken json'
    elif failure == 'stale':
        envelope = json.loads(saved_json)
        envelope['fingerprint'] = '0' * 64
        payload['scene_plan_json'] = json.dumps(envelope)
    elif failure == 'missing_reference':
        monkeypatch.setattr(CharacterStore, 'reference_path', lambda *args: None)
    elif failure == 'changed_profile':
        current_profile = ManagementStore(uploads).get(before.book_id)
        current_profile['details'] = current_profile['details'].model_copy(update={'image_prompt_base': 'Changed artistic style'})
        monkeypatch.setattr(ManagementStore, 'get', lambda *args, **kwargs: current_profile)
    elif failure in {'changed_character', 'changed_reference'}:
        characters = CharacterStore(uploads)
        character = characters.list(before.book_id)[0]
        if failure == 'changed_character':
            characters.save(
                before.book_id, character.id, character.revision, name=character.name,
                aliases=['Changed alias'], description=character.description,
                image_prompt=character.image_prompt, approved=True,
            )
        else:
            characters.save_reference_file(
                before.book_id, character.id, character.revision, png_bytes((257, 256)),
            )
    elif failure == 'changed_direction':
        before = reels.update_draft(before.id, before.revision, scene_direction='A newly requested pose.')
    elif failure == 'tampered_reference':
        characters = CharacterStore(uploads)
        character = characters.list(before.book_id)[0]
        characters.reference_path(character).write_bytes(png_bytes((258, 256)).read())

    class Generator:
        def __init__(self, *args, **kwargs):
            pytest.fail('Invalid or stale planned jobs must fail before constructing the generator')

    monkeypatch.setattr('bookpromo.reel_worker.ReelGenerator', Generator)
    jobs.enqueue(before.id, 'image', payload)
    assert run_reel_once(uploads, Settings(_env_file=None, app_data_dir=uploads.root))
    assert jobs.status(before.id)[0]['state'] == 'failed'
    after = reels.get_draft(before.id)
    for field in ('image_prompt', 'scene_direction', 'scene_image_path', 'scene_image_sha256',
                  'optimized_image_path', 'optimized_image_sha256', 'selected_image_path',
                  'selected_image_sha256', 'selected_image_source'):
        assert getattr(after, field) == getattr(before, field)
    for source, content in images.items():
        assert reels.candidate_image_path(after, source).read_bytes() == content


@pytest.mark.parametrize('missing_reference', [False, True])
def test_planned_base_scene_uses_only_compiled_plan_even_without_portrait_reference(setup, monkeypatch, tmp_path, missing_reference):
    uploads, reels, jobs, before = direction_worker_fixture(
        setup, image_prompt='Any Character carries a sword or silver daggers.',
        scene_direction='An old independent direction containing a cigar.',
    )
    saved_json, plan = saved_worker_plan(uploads, before)
    if missing_reference:
        monkeypatch.setattr(CharacterStore, 'reference_path', lambda *args: None)
    output = tmp_path / 'planned-base.png'
    output.write_bytes(png_bytes((80, 120)).read())
    captured = {}

    class Generator:
        def __init__(self, *args, **kwargs):
            pass

        def generate_image(self, *, reel_id, image_prompt):
            captured['prompt'] = image_prompt
            assert image_prompt == compile_scene_plan(plan)
            assert before.image_prompt not in image_prompt
            assert before.scene_direction not in image_prompt
            return output

    monkeypatch.setattr('bookpromo.reel_worker.ReelGenerator', Generator)
    job = jobs.enqueue(before.id, 'image', {
        'operation': 'scene', 'strategy': 'scene_plan', 'scene_plan_json': saved_json,
    })
    assert run_reel_once(uploads, Settings(_env_file=None, app_data_dir=uploads.root))
    with sqlite3.connect(uploads.db_path) as connection:
        result = json.loads(connection.execute('select result_json from local_reel_jobs where id=?', (job.id,)).fetchone()[0])
    assert result['effective_image_prompt'] == captured['prompt']
    assert result['scene_plan_json'] == saved_json
    assert result['candidate'] == 'scene'
    assert result['scene_generation_strategy'] == 'scene-plan-v1'
    assert result['scene_plan_fingerprint'] == json.loads(saved_json)['fingerprint']
    assert result['optimization_strategy'] is None
    assert result['identity_transfer_strategy'] is None
    assert reels.get_draft(before.id).scene_image_path == result['path']


def test_planned_base_scene_supports_environment_without_character_selection(setup, monkeypatch, tmp_path):
    uploads, reels, jobs, before = direction_worker_fixture(setup)
    before = reels.update_draft(before.id, before.revision, character_ids=[])
    saved_json, plan = saved_worker_plan(uploads, before)
    assert not plan.actors
    output = tmp_path / 'environment.png'
    output.write_bytes(png_bytes().read())

    class Generator:
        def __init__(self, *args, **kwargs):
            pass

        def generate_image(self, *, reel_id, image_prompt):
            assert image_prompt == compile_scene_plan(plan)
            return output

    monkeypatch.setattr('bookpromo.reel_worker.ReelGenerator', Generator)
    job = jobs.enqueue(before.id, 'image', {
        'operation': 'scene', 'strategy': 'scene_plan', 'scene_plan_json': saved_json,
    })
    assert run_reel_once(uploads, Settings(_env_file=None, app_data_dir=uploads.root))
    assert jobs.status(before.id)[0]['state'] == 'done'
    assert reels.get_draft(before.id).scene_image_path


@pytest.mark.parametrize('failure', ['missing_plan', 'stale_plan', 'missing_character', 'unbound_actor', 'revision'])
def test_planned_base_scene_invalid_inputs_fail_before_generator_preserving_selected_images(setup, monkeypatch, failure):
    uploads, reels, jobs, before = direction_worker_fixture(setup)
    saved_json, _ = saved_worker_plan(uploads, before, names=['Unrelated Figure'] if failure == 'unbound_actor' else None)
    payload = {'operation': 'scene', 'strategy': 'scene_plan', 'scene_plan_json': saved_json}
    if failure == 'missing_plan':
        payload.pop('scene_plan_json')
    elif failure == 'stale_plan':
        envelope = json.loads(saved_json)
        envelope['fingerprint'] = '0' * 64
        payload['scene_plan_json'] = json.dumps(envelope)
    elif failure == 'missing_character':
        monkeypatch.setattr(CharacterStore, 'list', lambda *args: [])
    if failure == 'unbound_actor':
        monkeypatch.setattr(CharacterStore, 'reference_path', lambda *args: None)
    images = {source: reels.candidate_image_path(before, source).read_bytes() for source in ('scene', 'optimized', 'selected')}
    job = jobs.enqueue(before.id, 'image', payload)
    if failure == 'revision':
        before = reels.update_draft(before.id, before.revision, caption_addition='A new caption after enqueue.')

    class Generator:
        def __init__(self, *args, **kwargs):
            pytest.fail('Invalid planned base scenes must fail before constructing the generator')

    monkeypatch.setattr('bookpromo.reel_worker.ReelGenerator', Generator)
    assert run_reel_once(uploads, Settings(_env_file=None, app_data_dir=uploads.root))
    with sqlite3.connect(uploads.db_path) as connection:
        row = connection.execute('select state,result_json from local_reel_jobs where id=?', (job.id,)).fetchone()
    assert row[0] == ('stale' if failure == 'revision' else 'failed')
    if row[1]:
        assert not json.loads(row[1])
    after = reels.get_draft(before.id)
    for field in ('revision', 'caption_addition', 'scene_image_path', 'scene_image_sha256',
                  'optimized_image_path', 'optimized_image_sha256', 'selected_image_path',
                  'selected_image_sha256', 'selected_image_source'):
        assert getattr(after, field) == getattr(before, field)
    for source, content in images.items():
        assert reels.candidate_image_path(after, source).read_bytes() == content
