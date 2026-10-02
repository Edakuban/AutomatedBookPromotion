from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import subprocess
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from bookpromo.book_teasers import (
    BookTeaserStore, TeaserRenderSegment, TeaserSegment, _run_with_progress,
    complete_chapter_segments,
    render_book_teaser,
)
from bookpromo.book_teaser_worker import BookTeaserJobStore, run_book_teaser_once
from bookpromo.config import Settings
from bookpromo.reels import ReelJobStore, ReelStore
from bookpromo.uploads import LocalUploadStore, UploadError
from bookpromo.web import create_app
from test_analysis import setup
from test_reels import png_bytes, wav_bytes


def test_complete_cut_requires_all_current_chapter_videos_and_orders_them():
    book_id = str(uuid4())
    chapters = [SimpleNamespace(id=str(uuid4()), position=position) for position in (2, 1)]
    plans = [
        {"chapter_id": chapter.id, "state": "done", "media_active": False,
         "draft": SimpleNamespace(
             id=str(uuid4()), book_id=book_id, selected_video_sha256="b" * 64,
             selected_video_path="clip.mp4", duration_ms=12_000,
             video_stale=False, image_stale=False, state="ready",
         )}
        for chapter in chapters
    ]
    reels = SimpleNamespace(artifact_path=lambda draft, kind: Path("clip.mp4"))
    result = complete_chapter_segments(book_id, chapters, plans, reels)
    assert [segment.position for segment in result] == [1, 2]
    assert all(segment.included and segment.start_ms == 0 for segment in result)
    with pytest.raises(UploadError):
        complete_chapter_segments(book_id, chapters, plans[:1], reels)
    plans[0]["draft"].video_stale = True
    with pytest.raises(UploadError):
        complete_chapter_segments(book_id, chapters, plans, reels)
    plans[0]["draft"].video_stale = False
    plans[0]["media_active"] = True
    with pytest.raises(UploadError):
        complete_chapter_segments(book_id, chapters, plans, reels)


def test_complete_cut_rejects_cross_book_or_missing_source():
    book_id, chapter_id = str(uuid4()), str(uuid4())
    chapters = [SimpleNamespace(id=chapter_id, position=1)]
    draft = SimpleNamespace(
        id=str(uuid4()), book_id=str(uuid4()), selected_video_sha256="b" * 64,
        selected_video_path="clip.mp4", duration_ms=12_000,
        video_stale=False, image_stale=False, state="ready",
    )
    plans = [{"chapter_id": chapter_id, "state": "done", "draft": draft}]
    reels = SimpleNamespace(artifact_path=lambda draft, kind: None)
    with pytest.raises(UploadError):
        complete_chapter_segments(book_id, chapters, plans, reels)
    draft.book_id = book_id
    with pytest.raises(UploadError):
        complete_chapter_segments(book_id, chapters, plans, reels)


@pytest.fixture
def teaser_setup(tmp_path):
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
    track, _ = reels.save_audio(book_id, wav_bytes(4), "song.wav", title="Song")
    return uploads, book_id, reels, track


def test_book_teaser_store_is_revision_safe_and_verifies_output(teaser_setup):
    uploads, book_id, _, track = teaser_setup
    store = BookTeaserStore(uploads)
    segment = TeaserSegment(
        chapter_id=str(uuid4()), draft_id=str(uuid4()), video_sha256="b" * 64,
        included=True, position=1, start_ms=500, duration_ms=2000,
    )
    project = store.save(
        book_id, 0, aspect="vertical", audio_track_id=track.id,
        transition_ms=500, segments=[segment],
    )
    assert project.revision == 1 and project.segments == (segment,)
    with pytest.raises(UploadError, match="zwischenzeitlich"):
        store.save(
            book_id, 0, aspect="vertical", audio_track_id=track.id,
            transition_ms=500, segments=[segment],
        )
    output = store.render_path(project)
    output.write_bytes(b"finished teaser")
    ready = store.mark_rendered(project.id, project.revision, output, 2000)
    assert ready.state == "ready" and ready.output_duration_ms == 2000
    assert store.output_path(ready) == output


@pytest.mark.parametrize("aspect,marker", [
    ("horizontal_crop", "crop=1920:1080"),
    ("horizontal_fit", "gblur=sigma=24:steps=2"),
    ("vertical", "crop=1080:1920"),
])
def test_book_teaser_renderer_builds_normalized_crossfade_and_continuous_audio(
    tmp_path, aspect, marker,
):
    first, second = tmp_path / "one.mp4", tmp_path / "two.mp4"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"audio")
    output = tmp_path / "teaser.mp4"
    commands = []

    def runner(command, **kwargs):
        if "-encoders" in command:
            return subprocess.CompletedProcess(command, 0, " V..... libx264 H.264\n", "")
        commands.append(command)
        Path(command[-1]).write_bytes(b"rendered")
        return subprocess.CompletedProcess(command, 0, "", "")

    duration = render_book_teaser([
        TeaserRenderSegment(first, 0, 2, .25, .75),
        TeaserRenderSegment(second, .5, 3, .5, .5),
    ], audio, output, aspect=aspect, transition_ms=500, media_binary="ffmpeg-test", runner=runner)

    assert duration == 4500 and output.read_bytes() == b"rendered"
    command = commands[0]
    filters = command[command.index("-filter_complex") + 1]
    assert command[0] == "ffmpeg-test" and command.count("-i") == 3
    assert command[command.index("-stream_loop") + 1] == "-1"
    assert marker in filters
    assert "xfade=transition=fade:duration=0.500:offset=1.500" in filters
    assert command[command.index("-map") + 1] == "[mix1]"


def test_zero_transition_uses_hard_concat_instead_of_invalid_zero_length_xfade(tmp_path):
    clips = [tmp_path / "one.mp4", tmp_path / "two.mp4"]
    for clip in clips:
        clip.write_bytes(b"clip")
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"audio")
    commands = []

    def runner(command, **kwargs):
        if "-encoders" in command:
            return subprocess.CompletedProcess(command, 0, " V..... libx264 H.264\n", "")
        commands.append(command)
        Path(command[-1]).write_bytes(b"rendered")
        return subprocess.CompletedProcess(command, 0, "", "")

    render_book_teaser(
        [TeaserRenderSegment(path, 0, 2) for path in clips],
        audio, tmp_path / "joined.mp4", aspect="vertical", transition_ms=0,
        media_binary="ffmpeg-test", runner=runner,
    )
    filters = commands[0][commands[0].index("-filter_complex") + 1]
    assert "concat=n=2:v=1:a=0[joined]" in filters and "xfade" not in filters


def test_ffmpeg_progress_is_reported_as_rendered_milliseconds(monkeypatch):
    class Process:
        stdout = iter(["out_time_us=1250000\n", "out_time_ms=3500000\n", "progress=end\n"])

        def wait(self):
            return 0

    commands = []

    def popen(command, **kwargs):
        commands.append((command, kwargs))
        return Process()

    monkeypatch.setattr("bookpromo.book_teasers.subprocess.Popen", popen)
    updates = []
    _run_with_progress(["ffmpeg", "-i", "input.mp4", "output.mp4"], 4_000,
                       lambda completed, total: updates.append((completed, total)))
    assert updates == [(1_250, 4_000), (3_500, 4_000), (4_000, 4_000)]
    assert commands[0][0][-3:] == ["pipe:1", "-nostats", "output.mp4"]


def test_book_teaser_page_is_available_after_chapter_extraction(setup):
    settings, _, book, _, _ = setup
    with TestClient(
        create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000",
    ) as client:
        page = client.get(f"/books/local/{book.id}/teaser")
        assert page.status_code == 200
        assert "Book-Teaser" in page.text
        assert "16:9 Crop mit Fokus" in page.text
        assert page.text.count("Noch kein fertiges Reel") == 2


def test_long_teaser_export_runs_as_durable_background_job(teaser_setup, monkeypatch):
    uploads, book_id, reels, track = teaser_setup
    draft = reels.get_or_create_draft(book_id, "c" * 64, "analysis", "quote", "Ein belegter Text")
    draft = reels.update_draft(
        draft.id, draft.revision, duration_ms=4_000, audio_track_id=track.id,
        audio_cue_id=None, audio_start_ms=0, caption_addition="Teasertext",
        final_caption="Teasertext", image_prompt="A cinematic vertical scene",
        video_prompt="The camera tracks laterally while rain moves across the existing window.",
    )
    reel_jobs = ReelJobStore(reels)
    reel_jobs.enqueue(draft.id, "image")
    image_job = reel_jobs.claim(kinds={"image"})
    relative, digest = reels.save_artifact(draft.id, "image", png_bytes(), "scene.png")
    assert reel_jobs.finish(image_job, result={
        "path": relative, "sha256": digest, "candidate": "scene",
        "character_snapshot": [],
    })
    reel_jobs.enqueue(draft.id, "video")
    video_job = reel_jobs.claim(kinds={"video"})
    relative, digest = reels.save_artifact(
        draft.id, "video", BytesIO(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32), "reel.mp4",
    )
    assert reel_jobs.finish(video_job, result={"path": relative, "sha256": digest})
    draft = reels.get_draft(draft.id)

    teaser_store = BookTeaserStore(uploads)
    project = teaser_store.save(
        book_id, 0, aspect="vertical", audio_track_id=track.id, transition_ms=500,
        segments=[TeaserSegment(
            chapter_id=str(uuid4()), draft_id=draft.id,
            video_sha256=draft.selected_video_sha256, included=True,
            position=1, start_ms=0, duration_ms=4_000,
        )],
    )
    jobs = BookTeaserJobStore(uploads)
    jobs.enqueue(project)
    with pytest.raises(UploadError, match="langen Exports"):
        teaser_store.save(
            book_id, project.revision, aspect="vertical", audio_track_id=track.id,
            transition_ms=500, segments=list(project.segments),
        )

    def fake_render(segments, audio_path, output_path, **kwargs):
        kwargs["progress"](2_000, 4_000)
        Path(output_path).write_bytes(b"rendered teaser")
        kwargs["progress"](4_000, 4_000)
        return 4_000

    monkeypatch.setattr("bookpromo.book_teaser_worker.render_book_teaser", fake_render)
    settings = Settings(_env_file=None, app_data_dir=uploads.root)
    assert run_book_teaser_once(uploads, settings)
    saved = teaser_store.get(book_id)
    job = jobs.latest(book_id)
    assert saved.state == "ready" and saved.output_duration_ms == 4_000
    assert job["state"] == "done" and job["progress_ms"] == job["total_ms"] == 4_000
    assert teaser_store.output_path(saved).read_bytes() == b"rendered teaser"
