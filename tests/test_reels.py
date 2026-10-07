from io import BytesIO
import hashlib
import sqlite3
import struct
import time
from uuid import uuid4
import wave

import pytest
from PIL import Image

from bookpromo.characters import CharacterStore
from bookpromo.reels import REEL_LEASE_SECONDS, ReelJobStore, ReelStore
from bookpromo.scene_plan import ScenePlan, encode_saved_plan
from bookpromo.uploads import LocalUploadStore, UploadError


def wav_bytes(seconds=2, rate=8_000):
    output = BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(struct.pack("<h", 100) * int(seconds * rate))
    output.seek(0)
    return output


def png_bytes(size=(64, 96)):
    output = BytesIO()
    Image.new("RGB", size, "#234567").save(output, "PNG")
    output.seek(0)
    return output


@pytest.fixture
def setup(tmp_path):
    uploads = LocalUploadStore(tmp_path / "data", 1024 * 1024)
    uploads.root.mkdir(parents=True)
    book_id = str(uuid4())
    with sqlite3.connect(uploads.db_path) as connection, connection:
        connection.execute("""create table local_books (
            id text primary key, version_id text, job_id text, title text, filename text,
            file_sha256 text, size_bytes integer, source_path text, created_at text)""")
        connection.execute(
            "insert into local_books values (?,?,?,?,?,?,?,?,?)",
            (book_id, str(uuid4()), str(uuid4()), "Roman", "Roman.docx", "a" * 64,
             1, "originals/source.docx", "2026-01-01T00:00:00+00:00"),
        )
    reels = ReelStore(uploads, max_audio_bytes=1024 * 1024)
    return uploads, book_id, reels, ReelJobStore(reels)


def new_draft(reels, book_id):
    return reels.get_or_create_draft(book_id, "b" * 64, "run-1", "quote-1", "Wörtliches Zitat")


def saved_scene_plan(fingerprint="a" * 64):
    return encode_saved_plan(ScenePlan(
        setting="Library", composition="Wide shot", art_direction="Watercolor",
        actors=[{"name": "Aster", "pose": "Leans towards window", "free_parts": ["left paw"]}],
    ), fingerprint)


def test_scene_plan_save_is_durable_fenced_and_preserves_reviewed_media(setup):
    uploads, _, reels, jobs = setup
    before = current_scene(setup)
    saved = saved_scene_plan()
    after = reels.save_scene_plan(before.id, before.revision, saved)
    assert after.scene_plan_json == saved
    assert after.revision == before.revision + 1
    assert ReelStore(uploads).get_draft(after.id) == after
    for key in ("state", "error", "image_prompt", "scene_direction", "scene_image_path", "scene_image_sha256",
                "selected_image_source", "selected_image_path", "selected_image_sha256",
                "selected_video_path", "selected_video_sha256", "image_stale", "video_stale"):
        assert getattr(after, key) == getattr(before, key)
    original_jobs = jobs.status(after.id)
    with pytest.raises(UploadError) as conflict:
        reels.save_scene_plan(before.id, before.revision, saved_scene_plan("b" * 64))
    assert conflict.value.status == 409
    assert reels.get_draft(before.id) == after
    assert jobs.status(after.id) == original_jobs


def test_scene_plan_seed_is_only_used_for_new_drafts(setup):
    _, book_id, reels, _ = setup
    original = reels.get_or_create_draft(book_id, "b" * 64, "run-1", "quote-1", "Zitat",
                                          scene_plan_json=saved_scene_plan())
    assert original.scene_plan_json == saved_scene_plan()
    existing = reels.get_or_create_draft(book_id, "b" * 64, "run-1", "quote-1", "Zitat",
                                          scene_plan_json=saved_scene_plan("b" * 64))
    assert existing == original


@pytest.mark.parametrize("running", [False, True], ids=["queued", "running"])
def test_scene_plan_save_rejects_jobs_enqueued_during_plan_generation(setup, running):
    _, _, reels, jobs = setup
    before = current_scene(setup)
    before = reels.save_scene_plan(before.id, before.revision, saved_scene_plan())
    jobs.enqueue(before.id, "image")
    if running:
        jobs.claim()
    before = reels.get_draft(before.id)
    original_jobs = jobs.status(before.id)
    with pytest.raises(UploadError, match="Abschluss abwarten") as conflict:
        reels.save_scene_plan(before.id, before.revision, saved_scene_plan("b" * 64))
    assert conflict.value.status == 409
    assert reels.get_draft(before.id) == before
    assert jobs.status(before.id) == original_jobs


@pytest.mark.parametrize("field", ["image_prompt", "scene_direction", "character_ids"])
@pytest.mark.parametrize("changed", [False, True])
def test_scene_plan_invalidates_only_changed_scene_inputs(setup, field, changed):
    uploads, book_id, reels, _ = setup
    before = current_scene(setup)
    before = reels.save_scene_plan(before.id, before.revision, saved_scene_plan())
    if field == "character_ids":
        value = [CharacterStore(uploads).create(book_id, name="Aster").id] if changed else []
    else:
        value = "Changed input" if changed else getattr(before, field)
    after = reels.update_draft(before.id, before.revision, **{field: value})
    assert after.scene_plan_json == ("" if changed else before.scene_plan_json)


def test_scene_plan_survives_caption_video_and_duration_edits(setup):
    _, _, reels, _ = setup
    before = current_scene(setup)
    before = reels.save_scene_plan(before.id, before.revision, saved_scene_plan())
    after = reels.update_draft(before.id, before.revision, final_caption="A new caption",
                              video_prompt="Camera pans", duration_ms=12_000)
    assert after.scene_plan_json == before.scene_plan_json


def test_planned_scene_enqueue_snapshots_saved_plan_without_changing_media_or_revision(setup):
    _, _, reels, jobs = setup
    before = current_scene(setup)
    before = reels.save_scene_plan(before.id, before.revision, saved_scene_plan())
    job = jobs.enqueue_planned_scene(before.id, before.revision, expected_fingerprint="a" * 64)
    assert job.input_revision == before.revision
    assert job.payload == {"operation": "optimize", "strategy": "planned_scene",
                           "scene_plan_json": before.scene_plan_json}
    assert reels.get_draft(before.id) == before


@pytest.mark.parametrize("failure", ["revision", "fingerprint", "busy", "blank", "malformed", "invalid-inventory"])
def test_planned_scene_enqueue_rejects_invalid_or_stale_plan_atomically(setup, failure):
    _, _, reels, jobs = setup
    before = current_scene(setup)
    before = reels.save_scene_plan(before.id, before.revision, saved_scene_plan())
    revision, fingerprint = before.revision, "a" * 64
    if failure == "revision":
        revision -= 1
    elif failure == "fingerprint":
        fingerprint = "b" * 64
    elif failure == "busy":
        jobs.enqueue(before.id, "prompt")
    else:
        corrupt = {"blank": "", "malformed": "{bad-json",
                   "invalid-inventory": before.scene_plan_json.replace(
                       '"contacts":[]',
                       '"contacts":[{"object_id":"missing","part":"left paw","action":"holds"}]',
                   )}[failure]
        with reels.connection() as connection, connection:
            connection.execute("update local_reel_drafts set scene_plan_json=? where id=?", (corrupt, before.id))
    before = reels.get_draft(before.id)
    original_jobs = jobs.status(before.id)
    with pytest.raises(UploadError):
        jobs.enqueue_planned_scene(before.id, revision, expected_fingerprint=fingerprint)
    assert reels.get_draft(before.id) == before
    assert jobs.status(before.id) == original_jobs


def test_scene_plan_reads_legacy_rows_without_a_read_migration(setup):
    _, book_id, reels, _ = setup
    before = new_draft(reels, book_id)
    assert before.scene_plan_json == ""
    with reels.connection() as connection, connection:
        connection.execute("alter table local_reel_drafts drop column scene_plan_json")
    assert reels.get_draft(before.id) == before
    assert reels.find_draft(book_id, "b" * 64, "run-1") == before
    assert reels.list_drafts(book_id) == [before]
    with reels.connection() as connection:
        assert "scene_plan_json" not in {
            row[1] for row in connection.execute("pragma table_info(local_reel_drafts)")
        }
    after = reels.update_draft(before.id, before.revision, final_caption="A caption")
    assert after.scene_plan_json == ""
    with reels.connection() as connection, connection:
        ReelStore.schema(connection)
        ReelStore.schema(connection)
        assert "scene_plan_json" in {
            row[1] for row in connection.execute("pragma table_info(local_reel_drafts)")
        }


@pytest.mark.parametrize("invalid", [None, 17, [], {}, "", "x" * 65537, "ü" * 32769],
                         ids=["none", "number", "list", "mapping", "blank", "oversized-ascii", "oversized-utf8"])
def test_scene_plan_save_rejects_missing_nontext_or_oversized_plan_without_mutation(setup, invalid):
    _, book_id, reels, jobs = setup
    before = new_draft(reels, book_id)
    with pytest.raises(UploadError):
        reels.save_scene_plan(before.id, before.revision, invalid)
    assert reels.get_draft(before.id) == before
    assert jobs.status(before.id) == []


def test_scene_direction_seed_is_durable_and_never_overwrites_an_existing_draft(setup):
    uploads, book_id, reels, _ = setup
    seeded = reels.get_or_create_draft(book_id, "b" * 64, "run-1", "quote-1", "Zitat",
                                      scene_direction="  The fox leaps over a stream; no held props. \n")
    assert seeded.scene_direction == "The fox leaps over a stream; no held props."
    assert ReelStore(uploads).get_draft(seeded.id) == seeded
    edited = reels.update_draft(seeded.id, seeded.revision, scene_direction="Manual pose correction")
    existing = reels.get_or_create_draft(book_id, "b" * 64, "run-1", "quote-1", "Zitat",
                                        scene_direction="A different AI suggestion")
    assert existing == edited
    assert existing.scene_direction == "Manual pose correction"


def test_scene_direction_reads_legacy_rows_without_migration_until_next_write(setup):
    uploads, book_id, reels, _ = setup
    original = new_draft(reels, book_id)
    with reels.connection() as connection, connection:
        connection.execute("alter table local_reel_drafts drop column scene_direction")
    assert reels.get_draft(original.id).scene_direction == ""
    assert reels.find_draft(book_id, "b" * 64, "run-1").scene_direction == ""
    assert reels.list_drafts(book_id)[0].scene_direction == ""
    with reels.connection() as connection:
        assert "scene_direction" not in {row[1] for row in connection.execute("pragma table_info(local_reel_drafts)")}
    # Additive migration is idempotent and does not replace old rows or seed them implicitly.
    existing = reels.get_or_create_draft(book_id, "b" * 64, "run-1", "quote-1", "Zitat",
                                        scene_direction="New analysis suggestion")
    assert existing == original
    with reels.connection() as connection, connection:
        ReelStore.schema(connection)
        ReelStore.schema(connection)
        columns = {row[1] for row in connection.execute("pragma table_info(local_reel_drafts)")}
    assert "scene_direction" in columns
    updated = reels.update_draft(existing.id, existing.revision, scene_direction="Saved after migration")
    assert ReelStore(uploads).get_draft(existing.id) == updated


@pytest.mark.parametrize("invalid", [None, 17, [], {}, "x" * 2001, " " * 2001])
def test_scene_direction_validation_rejects_invalid_seeds_and_updates_without_mutation(setup, invalid):
    _, book_id, reels, _ = setup
    with pytest.raises(UploadError, match="2000"):
        reels.get_or_create_draft(book_id, "b" * 64, "run-1", "quote-1", "Zitat", scene_direction=invalid)
    assert reels.list_drafts(book_id) == []
    before = new_draft(reels, book_id)
    with pytest.raises(UploadError, match="2000"):
        reels.update_draft(before.id, before.revision, scene_direction=invalid)
    assert reels.get_draft(before.id) == before


def test_scene_direction_updates_are_fenced_but_do_not_invalidate_existing_media(setup):
    _, book_id, reels, jobs = setup
    draft = new_draft(reels, book_id)
    track, _ = reels.save_audio(book_id, wav_bytes(20), "song.wav")
    draft = reels.update_draft(draft.id, draft.revision, image_prompt="A watercolor scene",
                              video_prompt="Camera pans gently", final_caption="Zitat mit Begleittext",
                              audio_track_id=track.id, audio_start_ms=0)
    image_path, image_hash = reels.save_artifact(draft.id, "image", png_bytes(), "scene.png")
    jobs.enqueue(draft.id, "image")
    jobs.finish(jobs.claim(), result={"path": image_path, "sha256": image_hash})
    video_path, video_hash = reels.save_artifact(draft.id, "video",
                                               BytesIO(b"\x00\x00\x00\x18ftypisomvideo"), "reel.mp4")
    jobs.enqueue(draft.id, "video")
    jobs.finish(jobs.claim(), result={"path": video_path, "sha256": video_hash})
    before = reels.get_draft(draft.id)
    assert not before.image_stale and not before.video_stale
    updated = reels.update_draft(before.id, before.revision, scene_direction="  Both paws forward.  ")
    assert updated.scene_direction == "Both paws forward."
    assert updated.revision == before.revision + 1
    for key in ("image_prompt", "video_prompt", "scene_image_path", "scene_image_sha256",
                "selected_image_source", "selected_image_path", "selected_image_sha256",
                "selected_video_path", "selected_video_sha256", "image_stale", "video_stale"):
        assert getattr(updated, key) == getattr(before, key)
    with pytest.raises(UploadError) as conflict:
        reels.update_draft(before.id, before.revision, scene_direction="Obsolete edit")
    assert conflict.value.status == 409
    cleared = reels.update_draft(updated.id, updated.revision, scene_direction=" \n ")
    assert cleared.scene_direction == ""
    boundary = reels.update_draft(cleared.id, cleared.revision, scene_direction="x" * 2000)
    assert len(boundary.scene_direction) == 2000


@pytest.mark.parametrize("existing", ["", "User-selected pose"])
@pytest.mark.parametrize("result_direction", [None, "  Fresh AI pose  "])
def test_prompt_completion_only_fills_blank_scene_direction_and_supports_legacy_results(setup, existing, result_direction):
    _, book_id, reels, jobs = setup
    draft = new_draft(reels, book_id)
    if existing:
        draft = reels.update_draft(draft.id, draft.revision, scene_direction=existing)
    jobs.enqueue(draft.id, "prompt")
    job = jobs.claim()
    result = {"image_prompt": "A forest scene"}
    if result_direction is not None:
        result["scene_direction"] = result_direction
    assert jobs.finish(job, result=result)
    after = reels.get_draft(draft.id)
    assert after.scene_direction == (existing or (result_direction or "").strip())
    assert after.image_prompt == "A forest scene"


@pytest.mark.parametrize("invalid", [None, 17, [], "x" * 2001])
def test_invalid_prompt_scene_direction_cannot_partially_commit_caption_or_pose(setup, invalid):
    _, book_id, reels, jobs = setup
    draft = new_draft(reels, book_id)
    jobs.enqueue(draft.id, "prompt")
    job = jobs.claim()
    before = reels.get_draft(draft.id)
    with pytest.raises(ValueError, match="2000"):
        jobs.finish(job, result={"scene_direction": invalid, "image_prompt": "Must not commit"})
    assert reels.get_draft(draft.id) == before
    assert jobs.status(draft.id)[0]["state"] == "running"


def test_scene_direction_user_edit_fences_running_prompt_result(setup):
    _, book_id, reels, jobs = setup
    draft = new_draft(reels, book_id)
    jobs.enqueue(draft.id, "prompt")
    job = jobs.claim()
    edited = reels.update_draft(draft.id, draft.revision, scene_direction="User correction during AI run")
    assert jobs.finish(job, result={"scene_direction": "Late AI correction", "image_prompt": "Late scene"})
    assert reels.get_draft(draft.id) == edited
    assert jobs.status(draft.id)[0]["state"] == "stale"


def current_scene(setup, direction="Saved pose"):
    _, book_id, reels, jobs = setup
    draft = new_draft(reels, book_id)
    draft = reels.update_draft(draft.id, draft.revision, image_prompt="A cinematic forest scene",
                              scene_direction=direction)
    path, digest = reels.save_artifact(draft.id, "image", png_bytes(), "scene.png")
    jobs.enqueue(draft.id, "image")
    jobs.finish(jobs.claim(), result={"path": path, "sha256": digest})
    return reels.get_draft(draft.id)


@pytest.mark.parametrize("direction,expected,changed", [
    (None, "Saved pose", False), ("  Saved pose  ", "Saved pose", False),
    ("  New pose  ", "New pose", True), ("", "", True),
])
def test_atomic_direction_enqueue_snapshots_saved_direction_and_preserves_media(setup, direction, expected, changed):
    _, _, reels, jobs = setup
    before = current_scene(setup)
    payload = {"operation": "optimize", "strategy": "reference_scene", "scene_direction": "Not canonical"}
    job = jobs.enqueue_with_direction(before.id, before.revision, payload, scene_direction=direction)
    after = reels.get_draft(before.id)
    assert after.scene_direction == expected
    assert after.revision == before.revision + int(changed)
    assert job.input_revision == after.revision
    assert job.payload.get("scene_direction", "") == expected
    assert payload["scene_direction"] == "Not canonical"  # Caller dictionary remains unchanged.
    for key in ("image_prompt", "video_prompt", "scene_image_path", "scene_image_sha256",
                "selected_image_source", "selected_image_path", "selected_image_sha256",
                "selected_video_path", "selected_video_sha256", "image_stale", "video_stale"):
        assert getattr(after, key) == getattr(before, key)


@pytest.mark.parametrize("direction", [None, ""])
def test_atomic_masked_enqueue_preserves_saved_direction_and_legacy_payload(setup, direction):
    _, _, reels, jobs = setup
    before = current_scene(setup)
    job = jobs.enqueue_with_direction(before.id, before.revision, {"operation": "optimize"},
                                      scene_direction=direction)
    assert job.payload == {"operation": "optimize"}
    assert job.input_revision == before.revision
    assert reels.get_draft(before.id) == before


@pytest.mark.parametrize("invalid_revision", [True, "4", None, 0, -1, 1])
def test_atomic_direction_enqueue_rejects_invalid_or_old_revisions(setup, invalid_revision):
    _, _, reels, jobs = setup
    before = current_scene(setup)
    original_jobs = jobs.status(before.id)
    with pytest.raises(UploadError) as conflict:
        jobs.enqueue_with_direction(before.id, invalid_revision,
                                    {"operation": "optimize", "strategy": "reference_scene"},
                                    scene_direction="Must not save")
    assert conflict.value.status == 409
    assert reels.get_draft(before.id) == before
    assert jobs.status(before.id) == original_jobs


@pytest.mark.parametrize("active_kind,running", [("prompt", False), ("image", False), ("prompt", True)])
def test_atomic_direction_enqueue_rejects_any_active_draft_job(setup, active_kind, running):
    _, _, reels, jobs = setup
    before = current_scene(setup)
    jobs.enqueue(before.id, active_kind)
    if running:
        assert jobs.claim()
    before = reels.get_draft(before.id)
    original_jobs = jobs.status(before.id)
    with pytest.raises(UploadError) as conflict:
        jobs.enqueue_with_direction(before.id, before.revision,
                                    {"operation": "optimize", "strategy": "reference_scene"},
                                    scene_direction="Must not save while busy")
    assert conflict.value.status == 409
    assert reels.get_draft(before.id) == before
    assert jobs.status(before.id) == original_jobs


@pytest.mark.parametrize("payload", [
    {"operation": "optimize", "strategy": "reference_scene", "unserializable": {1}},
    {"operation": "optimize", "strategy": "reference_scene", "oversized": "x" * 70000},
    {"operation": "optimize", "strategy": "reference_scene", "character_ids": ["invalid"]},
])
def test_atomic_direction_enqueue_rolls_back_direction_if_queue_validation_fails(setup, payload):
    _, _, reels, jobs = setup
    before = current_scene(setup)
    original_jobs = jobs.status(before.id)
    with pytest.raises((UploadError, ValueError)):
        jobs.enqueue_with_direction(before.id, before.revision, payload, scene_direction="Must roll back")
    assert reels.get_draft(before.id) == before
    assert jobs.status(before.id) == original_jobs


def test_atomic_direction_enqueue_rolls_back_when_current_scene_is_missing(setup):
    _, book_id, reels, jobs = setup
    before = new_draft(reels, book_id)
    before = reels.update_draft(before.id, before.revision, image_prompt="Scene without an existing image")
    with pytest.raises(UploadError) as conflict:
        jobs.enqueue_with_direction(before.id, before.revision,
                                    {"operation": "optimize", "strategy": "reference_scene"},
                                    scene_direction="Must roll back with no scene")
    assert conflict.value.status == 409
    assert reels.get_draft(before.id) == before
    assert jobs.status(before.id) == []


@pytest.mark.parametrize("payload,direction", [
    ({"operation": "scene"}, "New pose"),
    ({"operation": "optimize", "strategy": "unknown"}, "New pose"),
    ({"operation": "optimize", "strategy": []}, "New pose"),
    ({"operation": "optimize", "strategy": "masked"}, "Forbidden pose"),
    ({"operation": "optimize", "strategy": "reference_scene"}, 17),
    ({"operation": "optimize", "strategy": "reference_scene"}, "x" * 2001),
])
def test_atomic_direction_enqueue_rejects_invalid_inputs_without_mutation(setup, payload, direction):
    _, _, reels, jobs = setup
    before = current_scene(setup)
    original_jobs = jobs.status(before.id)
    with pytest.raises(UploadError):
        jobs.enqueue_with_direction(before.id, before.revision, payload, scene_direction=direction)
    assert reels.get_draft(before.id) == before
    assert jobs.status(before.id) == original_jobs


def test_local_carousel_crop_is_image_scoped_fenced_and_does_not_change_reels(setup):
    _, book_id, reels, jobs = setup
    draft = new_draft(reels, book_id)
    assert reels.carousel_crop(draft) == (.5, .5, 0)
    draft = reels.save_uploaded_image(draft.id, draft.revision, png_bytes(), "source.png")
    draft = reels.select_image(draft.id, draft.revision, "upload")
    reels.save_carousel_crop(draft.id, draft.revision, draft.selected_image_sha256, 0, .2, .9)
    assert reels.get_draft(draft.id) == draft
    assert jobs.status(draft.id) == []
    assert ReelStore(reels.uploads).carousel_crop(draft) == (.2, .9, 1)
    with pytest.raises(UploadError) as conflict:
        reels.save_carousel_crop(draft.id, draft.revision, draft.selected_image_sha256, 0, .3, .7)
    assert conflict.value.status == 409
    with pytest.raises(UploadError):
        reels.save_carousel_crop(draft.id, draft.revision - 1, draft.selected_image_sha256, 1, .5, .5)
    with pytest.raises(UploadError):
        reels.save_carousel_crop(draft.id, draft.revision, "a" * 64, 1, .5, .5)
    with pytest.raises(UploadError):
        reels.save_carousel_crop(draft.id, draft.revision, draft.selected_image_sha256, 1, float('nan'), .5)
    changed = reels.save_uploaded_image(draft.id, draft.revision, png_bytes((70, 140)), "new.png")
    changed = reels.select_image(changed.id, changed.revision, "upload")
    assert reels.carousel_crop(changed) == (.5, .5, 0)
    reels.delete_book_assets(book_id)
    with reels.connection() as connection:
        assert connection.execute('select count(*) from local_carousel_crops').fetchone()[0] == 0


def test_audio_is_streamed_validated_deduplicated_and_book_scoped(setup):
    uploads, book_id, reels, _ = setup
    track, duplicate = reels.save_audio(book_id, wav_bytes(), "song.wav", title="Song")
    assert not duplicate
    assert track.duration_ms == 2_000
    assert track.duration_seconds == 2.0
    assert track.sample_rate == 8_000 and track.channels == 1 and track.sample_width == 2
    assert (uploads.root / track.relative_path).is_file()
    assert reels.audio_path(track) == uploads.root / track.relative_path
    assert reels.audio_path(track.id) == uploads.root / track.relative_path

    same, duplicate = reels.save_audio(book_id, wav_bytes(), "copy.wav")
    assert duplicate and same.id == track.id
    other, duplicate = reels.save_audio(book_id, wav_bytes(3), "other.wav")
    assert not duplicate and other.id != track.id
    assert [item.id for item in reels.list_audio(book_id)] == [track.id, other.id]

    with pytest.raises(UploadError, match="PCM-WAV"):
        reels.save_audio(book_id, BytesIO(b"not wave"), "bad.wav")
    with pytest.raises(UploadError) as error:
        reels.save_audio(str(uuid4()), wav_bytes(), "song.wav")
    assert error.value.status == 404


def test_audio_size_limit_and_named_cues_are_revision_safe(setup):
    _, book_id, reels, _ = setup
    reels.max_audio_bytes = 64
    with pytest.raises(UploadError) as error:
        reels.save_audio(book_id, wav_bytes(), "song.wav")
    assert error.value.status == 413
    assert not list((reels.root / "pending").glob("*.part"))

    reels.max_audio_bytes = 1024 * 1024
    track, _ = reels.save_audio(book_id, wav_bytes(12), "song.wav")
    cue = reels.save_cue(track.id, "Refrain", 2_000, 8_000)
    assert cue.revision == 1
    cue = reels.save_cue(track.id, "Refrain neu", 3_000, 7_000,
                          cue_id=cue.id, revision=cue.revision)
    assert cue.revision == 2 and reels.list_cues(track.id) == [cue]
    with pytest.raises(UploadError) as conflict:
        reels.save_cue(track.id, "Veraltet", 0, 5_000, cue_id=cue.id, revision=1)
    assert conflict.value.status == 409
    with pytest.raises(UploadError, match="außerhalb"):
        reels.save_cue(track.id, "Zu spät", 10_000, 5_000)


def test_draft_keeps_quote_snapshot_and_tracks_dependency_staleness(setup):
    _, book_id, reels, _ = setup
    draft = new_draft(reels, book_id)
    assert draft.duration_ms == 10_000 and draft.quote_text == "Wörtliches Zitat"
    assert reels.get_or_create_draft(
        book_id, "b" * 64, "run-1", "new-id", "geänderter Text"
    ).id == draft.id
    assert reels.find_draft(book_id, "b" * 64, "run-1") == draft
    assert reels.find_draft(book_id, "c" * 64, "run-1") is None
    assert reels.list_drafts(book_id) == [draft]

    track, _ = reels.save_audio(book_id, wav_bytes(20), "song.wav")
    cue = reels.save_cue(track.id, "Refrain", 5_000, 10_000)
    draft = reels.update_draft(
        draft.id, draft.revision,
        duration_ms=8_000, audio_track_id=track.id, audio_cue_id=cue.id,
        audio_start_ms=5_000, caption_addition="Ein Begleittext",
        final_caption="Zitat\n\nEin Begleittext", image_prompt="A cinematic scene",
        video_prompt="Camera arcs around the subject",
    )
    assert draft.revision == 2 and draft.audio_start_ms == 5_000
    assert draft.image_stale and draft.video_stale
    with pytest.raises(UploadError) as conflict:
        reels.update_draft(draft.id, 1, final_caption="veraltet")
    assert conflict.value.status == 409
    with pytest.raises(UploadError, match="außerhalb"):
        reels.update_draft(draft.id, draft.revision, audio_start_ms=15_000)


def test_successful_artifacts_survive_failed_regeneration(setup):
    _, book_id, reels, jobs = setup
    draft = new_draft(reels, book_id)
    draft = reels.update_draft(draft.id, draft.revision, image_prompt="A cinematic image")
    image_path, image_hash = reels.save_artifact(
        draft.id, "image", BytesIO(b"\x89PNG\r\n\x1a\nimage-one"), "image.png"
    )
    jobs.enqueue(draft.id, "image")
    job = jobs.claim(now=100)
    assert job.kind == "image" and job.token
    assert jobs.finish(job, result={"path": image_path, "sha256": image_hash}, now=101)
    successful = reels.get_draft(draft.id)
    assert successful.selected_image_path == image_path and not successful.image_stale
    assert reels.artifact_path(successful, "image") == reels.root / image_path

    jobs.enqueue(draft.id, "image", {"seed": 2})
    failed_job = jobs.claim(now=102)
    assert jobs.finish(failed_job, error="ComfyUI nicht erreichbar", now=103)
    failed = reels.get_draft(draft.id)
    assert failed.state == "failed"
    assert failed.selected_image_path == image_path
    assert failed.selected_image_sha256 == image_hash


def test_scene_optimized_and_uploaded_images_remain_selectable(setup):
    _, book_id, reels, jobs = setup
    draft = new_draft(reels, book_id)
    draft = reels.update_draft(draft.id, draft.revision, image_prompt="A cinematic image")

    scene_path, scene_hash = reels.save_artifact(
        draft.id, "image", BytesIO(b"\x89PNG\r\n\x1a\nscene"), "scene.png",
    )
    jobs.enqueue(draft.id, "image", {"operation": "scene"})
    jobs.finish(jobs.claim(now=1), result={
        "path": scene_path, "sha256": scene_hash, "candidate": "scene",
    }, now=2)
    scene = reels.get_draft(draft.id)
    assert scene.scene_image_path == scene_path
    assert scene.selected_image_source == "scene"

    optimized_path, optimized_hash = reels.save_artifact(
        draft.id, "image", BytesIO(b"\x89PNG\r\n\x1a\noptimized"), "optimized.png",
    )
    jobs.enqueue(draft.id, "image", {"operation": "optimize"})
    jobs.finish(jobs.claim(now=3), result={
        "path": optimized_path, "sha256": optimized_hash, "candidate": "optimized",
    }, now=4)
    optimized = reels.get_draft(draft.id)
    assert optimized.scene_image_path == scene_path
    assert optimized.optimized_image_path == optimized_path
    assert optimized.selected_image_source == "scene"

    selected_optimized = reels.select_image(optimized.id, optimized.revision, "optimized")
    assert selected_optimized.selected_image_path == optimized_path
    assert selected_optimized.selected_image_source == "optimized" and selected_optimized.video_stale
    selected_scene = reels.select_image(
        selected_optimized.id, selected_optimized.revision, "scene",
    )
    assert selected_scene.selected_image_path == scene_path
    assert selected_scene.selected_image_source == "scene" and selected_scene.video_stale

    uploaded = reels.save_uploaded_image(
        selected_scene.id, selected_scene.revision,
        png_bytes(), "mine.png",
    )
    assert uploaded.selected_image_source == "scene"
    assert uploaded.uploaded_image_path != uploaded.selected_image_path
    assert [source for source, _, _ in uploaded.image_candidates] == ["scene", "optimized", "upload"]
    assert reels.candidate_image_path(uploaded, "optimized") == reels.root / optimized_path

    selected_upload = reels.select_image(uploaded.id, uploaded.revision, "upload")
    assert selected_upload.selected_image_source == "upload"
    assert selected_upload.uploaded_image_path == selected_upload.selected_image_path

    edited = reels.update_draft(
        selected_upload.id, selected_upload.revision, image_prompt="A changed cinematic image",
    )
    assert edited.selected_image_source == "upload" and not edited.image_stale
    assert edited.scene_image_path is None and edited.optimized_image_path is None
    assert edited.uploaded_image_path == uploaded.uploaded_image_path


@pytest.mark.parametrize("kind", ["prompt", "image"])
def test_claiming_stale_queued_job_preserves_latest_draft_state_and_error(setup, kind):
    _, book_id, reels, jobs = setup
    draft = new_draft(reels, book_id)
    draft = reels.update_draft(draft.id, draft.revision, image_prompt="Old image prompt")
    queued = jobs.enqueue(draft.id, kind)
    updated = reels.update_draft(draft.id, draft.revision, image_prompt="New image prompt")
    with reels.connection() as connection, connection:
        connection.execute("update local_reel_drafts set state='failed',error=? where id=?",
                           ("A failure belonging to the latest revision", updated.id))
    before = reels.get_draft(updated.id)
    claimed = jobs.claim(now=100)
    assert claimed.id == queued.id
    assert claimed.input_revision != before.revision
    assert reels.get_draft(before.id) == before
    assert jobs.finish(claimed, result={}, now=101)
    assert jobs.status(before.id)[0]["state"] == "stale"
    assert reels.get_draft(before.id) == before


def test_job_completion_is_fenced_by_lease_and_draft_revision(setup):
    _, book_id, reels, jobs = setup
    draft = new_draft(reels, book_id)
    draft = reels.update_draft(draft.id, draft.revision, image_prompt="An old prompt")
    jobs.enqueue(draft.id, "image")
    stale_worker = jobs.claim(now=100)
    assert not jobs.heartbeat(stale_worker, now=100 + REEL_LEASE_SECONDS)

    fresh_worker = jobs.claim(now=100 + REEL_LEASE_SECONDS + 1)
    assert fresh_worker.token != stale_worker.token
    assert not jobs.finish(stale_worker, error="late", now=222)

    path, digest = reels.save_artifact(
        draft.id, "image", BytesIO(b"\x89PNG\r\n\x1a\nlate-image"), "image.png"
    )
    edited = reels.update_draft(draft.id, draft.revision, image_prompt="A newer prompt")
    assert jobs.finish(fresh_worker, result={"path": path, "sha256": digest}, now=223)
    assert reels.get_draft(draft.id).selected_image_path is None
    assert jobs.status(draft.id)[0]["state"] == "stale"
    assert edited.image_prompt == "A newer prompt"


def test_prompt_video_and_upload_jobs_form_a_ready_stocked_draft(setup):
    _, book_id, reels, jobs = setup
    draft = new_draft(reels, book_id)
    track, _ = reels.save_audio(book_id, wav_bytes(20), "song.wav")
    draft = reels.update_draft(
        draft.id, draft.revision, audio_track_id=track.id, audio_start_ms=2_000
    )
    jobs.enqueue(draft.id, "prompt")
    prompt = jobs.claim(now=10)
    assert jobs.finish(prompt, result={
        "caption_addition": "Atmosphärischer Zusatz",
        "final_caption": "Wörtliches Zitat\n\nAtmosphärischer Zusatz",
        "image_prompt": "A rain-soaked street",
        "video_prompt": "Camera pushes through the rain while reflections pulse",
    }, now=11)
    draft = reels.get_draft(draft.id)
    assert draft.final_caption.startswith("Wörtliches Zitat")

    image_path, image_hash = reels.save_artifact(
        draft.id, "image", BytesIO(b"\x89PNG\r\n\x1a\ncover"), "cover.png"
    )
    jobs.enqueue(draft.id, "image")
    assert jobs.finish(jobs.claim(now=12), result={"path": image_path, "sha256": image_hash}, now=13)
    draft = reels.get_draft(draft.id)
    video_bytes = b"\x00\x00\x00\x18ftypisom" + b"video"
    video_path, video_hash = reels.save_artifact(
        draft.id, "video", BytesIO(video_bytes), "reel.mp4"
    )
    jobs.enqueue(draft.id, "video")
    assert jobs.finish(jobs.claim(now=14), result={"path": video_path, "sha256": video_hash}, now=15)
    ready = reels.get_draft(draft.id)
    assert ready.state == "ready" and not ready.image_stale and not ready.video_stale

    jobs.enqueue(ready.id, "upload")
    assert jobs.finish(jobs.claim(now=16), result={
        "remote_video_path": f"{book_id}/reels/{video_hash}.mp4",
        "remote_cover_path": f"{book_id}/reels/{image_hash}.png",
    }, now=17)
    stocked = reels.get_draft(draft.id)
    assert stocked.state == "stocked"
    assert stocked.remote_video_path.endswith(".mp4")


def test_book_reel_cleanup_is_exact_and_removes_rows(setup):
    uploads, book_id, reels, jobs = setup
    other_book = str(uuid4())
    with sqlite3.connect(uploads.db_path) as connection, connection:
        connection.execute(
            "insert into local_books values (?,?,?,?,?,?,?,?,?)",
            (other_book, str(uuid4()), str(uuid4()), "Zwei", "Zwei.docx", "c" * 64,
             1, "originals/two.docx", "2026-01-01T00:00:00+00:00"),
        )
    track, _ = reels.save_audio(book_id, wav_bytes(), "song.wav")
    other_track, _ = reels.save_audio(other_book, wav_bytes(3), "other.wav")
    cue = reels.save_cue(track.id, "Hook", 0, 1_000)
    draft = new_draft(reels, book_id)
    draft = reels.update_draft(draft.id, draft.revision, image_prompt="A scene")
    jobs.enqueue(draft.id, "image")
    reels.delete_book_assets(book_id)

    assert not (uploads.root / "audio" / book_id).exists()
    assert reels.get_draft(draft.id) is None
    assert reels.list_audio(book_id) == [] and reels.list_cues(track.id) == []
    assert reels.audio_path(other_track).is_file()
    with sqlite3.connect(uploads.db_path) as connection:
        assert connection.execute(
            "select count(*) from local_reel_jobs where draft_id=?", (draft.id,)
        ).fetchone()[0] == 0
        assert connection.execute(
            "select count(*) from local_audio_cues where id=?", (cue.id,)
        ).fetchone()[0] == 0


def test_confirmed_direct_transfer_marks_only_a_ready_revision_stocked(setup):
    _, book_id, reels, jobs = setup
    draft = new_draft(reels, book_id)
    track, _ = reels.save_audio(book_id, wav_bytes(20), "song.wav")
    draft = reels.update_draft(
        draft.id, draft.revision, audio_track_id=track.id, audio_start_ms=0,
        final_caption="Wörtliches Zitat\n\nText", image_prompt="A vertical cinematic scene",
        video_prompt="The camera tracks laterally while rain crosses the visible street.",
    )
    image_path, image_hash = reels.save_artifact(
        draft.id, "image", BytesIO(b"\x89PNG\r\n\x1a\nimage"), "image.png",
    )
    jobs.enqueue(draft.id, "image")
    jobs.finish(jobs.claim(now=1), result={"path": image_path, "sha256": image_hash}, now=2)
    draft = reels.get_draft(draft.id)
    video_path, video_hash = reels.save_artifact(
        draft.id, "video", BytesIO(b"\x00\x00\x00\x18ftypisomvideo"), "video.mp4",
    )
    jobs.enqueue(draft.id, "video")
    jobs.finish(jobs.claim(now=3), result={"path": video_path, "sha256": video_hash}, now=4)
    ready = reels.get_draft(draft.id)
    stocked = reels.mark_stocked(ready.id, ready.revision, f"{ready.id}/{video_hash}.mp4")
    assert stocked.state == "stocked" and stocked.remote_video_path.endswith(".mp4")
    with pytest.raises(UploadError) as stale:
        reels.mark_stocked(ready.id, ready.revision, stocked.remote_video_path)
    assert stale.value.status == 409

    repeated_path = f"{uuid4()}/{video_hash}.mp4"
    repeated = reels.mark_stocked(stocked.id, stocked.revision, repeated_path)
    assert repeated.state == "stocked"
    assert repeated.remote_video_path == repeated_path
    assert repeated.revision == stocked.revision + 1
