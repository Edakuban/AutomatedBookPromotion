from io import BytesIO
import hashlib
import sqlite3
import struct
import time
from uuid import uuid4
import wave

import pytest

from bookpromo.reels import REEL_LEASE_SECONDS, ReelJobStore, ReelStore
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
