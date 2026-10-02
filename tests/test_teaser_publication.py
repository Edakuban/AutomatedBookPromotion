import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
from uuid import uuid4

import httpx
import pytest

from bookpromo.book_teasers import BookTeaserProject
from bookpromo.book_teasers import _video_encoder_args
from bookpromo.config import Settings
from bookpromo.database import DatabaseError, SupabaseRepository
from bookpromo.publication import PlatformDefault, PublicationDefaults
from bookpromo.teaser_publication import (
    book_teaser_asset_id, book_teaser_platforms, enqueue_book_teaser,
)
from bookpromo.uploads import UploadError
from bookpromo.reel_generation import ffmpeg_binary
from bookpromo.teaser_publication import _media_manifest


@pytest.fixture
def ready(tmp_path, monkeypatch):
    data = b"\x00\x00\x00\x18ftypisom" + b"x" * 64
    path = tmp_path / "trailer.mp4"
    path.write_bytes(data)
    project = BookTeaserProject(
        id=str(uuid4()), book_id=str(uuid4()), revision=3, aspect="horizontal_crop",
        audio_track_id=str(uuid4()), transition_ms=500, segments_json="[]", state="ready",
        output_path=str(path), output_sha256=hashlib.sha256(data).hexdigest(),
        output_duration_ms=235000, error=None,
    )
    manifest = {"streams": [{"codec_type": "video", "width": 1920, "height": 1080},
                             {"codec_type": "audio"}], "format": {"duration": "235.021"}}
    monkeypatch.setattr("bookpromo.teaser_publication.ffmpeg_binary", lambda: "ffmpeg")
    monkeypatch.setattr("bookpromo.teaser_publication.subprocess.run",
                        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(manifest)))
    defaults = PublicationDefaults(youtube=PlatformDefault(enabled=True, account_id="channel"),
                                   instagram=PlatformDefault(enabled=True, account_id="insta"),
                                   facebook=PlatformDefault(enabled=True, account_id="page"))
    return project, path, manifest, defaults


class FakeDB:
    def __init__(self, schema_error=False):
        self.calls = []
        self.schema_error = schema_error

    async def check_schema(self, **kwargs):
        self.calls.append(("schema", kwargs))
        if self.schema_error:
            raise DatabaseError("book_teaser_schema")

    async def upload_book_teaser(self, object_path, data):
        self.calls.append(("upload", object_path, data))

    async def enqueue_reel(self, asset, publications):
        self.calls.append(("enqueue", asset, publications))
        return {"outcome": "enqueued", "asset": asset, "publications": publications}


def queue(ready, db, **kwargs):
    project, path, _, defaults = ready
    values = dict(project=project, path=path, book_profile={"title": "Testbuch"},
                  audio_title="Song", defaults=defaults, platforms=("youtube",),
                  title="Testbuch Trailer", description="Ein vollständiger Buchtrailer.",
                  queue_mode="daily", scheduled_for=None)
    values.update(kwargs)
    return asyncio.run(enqueue_book_teaser(db, Settings(_env_file=None), **values))


def test_full_teaser_freezes_measured_landscape_metadata_and_daily_queue(ready):
    db = FakeDB()
    result = queue(ready, db)
    asset = result["asset"]
    assert asset["source_kind"] == "book_teaser"
    assert asset["quote_id"] is None and asset["chapter_id"] is None
    assert (asset["width"], asset["height"], asset["duration_ms"]) == (1920, 1080, 235021)
    assert asset["book_id"] == ready[0].book_id
    assert asset["id"] == book_teaser_asset_id(ready[0])
    assert result["publications"][0]["queue_mode"] == "daily"
    assert result["publications"][0]["description"] == "Ein vollständiger Buchtrailer."
    assert "#Shorts" not in json.dumps(result)
    assert [call[0] for call in db.calls] == ["schema", "upload", "enqueue"]
    assert db.calls[0][1] == {"book_teasers": True}


def test_scheduled_queue_and_deterministic_retry_identity(ready):
    scheduled = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    result = queue(ready, FakeDB(), queue_mode="scheduled", scheduled_for=scheduled)
    assert result["publications"][0]["scheduled_for"] == scheduled
    assert book_teaser_asset_id(ready[0]) == book_teaser_asset_id(ready[0])
    assert book_teaser_asset_id(replace(ready[0], revision=4)) != book_teaser_asset_id(ready[0])
    assert book_teaser_asset_id(ready[0], requeue=True) != book_teaser_asset_id(ready[0])


@pytest.mark.parametrize("change", ["dimensions", "duration", "audio", "nan"])
def test_invalid_real_metadata_never_uploads(ready, change):
    manifest = ready[2]
    if change == "dimensions":
        manifest["streams"][0]["width"] = 512
    elif change == "duration":
        manifest["format"]["duration"] = "12"
    elif change == "audio":
        manifest["streams"].pop()
    else:
        manifest["format"]["duration"] = "NaN"
    db = FakeDB()
    with pytest.raises(UploadError):
        queue(ready, db)
    assert db.calls == []


def test_invalid_hash_and_old_schema_never_upload(ready):
    db = FakeDB()
    with pytest.raises(UploadError):
        queue(ready, db, project=replace(ready[0], output_sha256="0" * 64))
    assert not db.calls
    db = FakeDB(schema_error=True)
    with pytest.raises(DatabaseError, match="v10"):
        queue(ready, db)
    assert [call[0] for call in db.calls] == ["schema"]


@pytest.mark.parametrize("values", [
    {"platforms": ("facebook",)}, {"platforms": ("instagram",)},
    {"platforms": ("youtube", "youtube")}, {"platforms": ()},
    {"queue_mode": "daily", "scheduled_for": "2027-01-01T00:00:00Z"},
    {"queue_mode": "scheduled", "scheduled_for": "2027-01-01T00:00:00"},
])
def test_unusable_platform_or_schedule_never_uploads(ready, values):
    db = FakeDB()
    with pytest.raises(UploadError):
        queue(ready, db, **values)
    assert not db.calls


def test_eligible_destinations_depend_on_aspect(ready):
    defaults = ready[3]
    assert book_teaser_platforms(defaults, "horizontal_crop") == ("youtube",)
    assert book_teaser_platforms(defaults, "vertical") == ("instagram", "youtube")


@pytest.mark.parametrize("version", [8, 9, 10])
def test_v8_v9_backwards_compatibility_and_v10_gate(version):
    settings = Settings(_env_file=None, supabase_enabled=True,
                        supabase_url="https://test.supabase.co", supabase_secret_key="sb_secret_test")
    repo = SupabaseRepository(settings, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=[{"version": version}])
    ))
    assert asyncio.run(repo.check_schema()) == version
    if version >= 9:
        assert asyncio.run(repo.check_schema(chapters=True)) == version
    if version < 10:
        with pytest.raises(DatabaseError, match="v10"):
            asyncio.run(repo.check_schema(book_teasers=True))
    else:
        assert asyncio.run(repo.check_schema(book_teasers=True)) == 10


def test_v10_migration_preserves_narrow_reel_limits_and_security():
    migration = next((Path(__file__).parents[1] / "supabase/migrations").glob("*_book_teaser_sources.sql"))
    sql = migration.read_text(encoding="utf-8")
    assert "Schema v10 requires schema v9" in sql
    assert "then 600000 else 60000" in sql
    assert "then 314572800 else 52428800" in sql
    assert "source_kind='book_teaser' and quote_id is null and chapter_id is null" in sql
    assert "security definer" not in sql.lower()
    assert "grant" not in sql.lower()


def test_actual_ffprobe_reads_landscape_trailer_without_guessing(tmp_path):
    binary = ffmpeg_binary()
    try:
        subprocess.run([binary, "-version"], check=True, capture_output=True)
    except FileNotFoundError:
        pytest.skip("FFmpeg is not installed")
    path = tmp_path / "actual-trailer.mp4"
    subprocess.run([
        binary, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=12:d=4.2",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        *_video_encoder_args(binary, subprocess.run), "-c:a", "aac",
        "-pix_fmt", "yuv420p", "-t", "4.2", str(path),
    ], check=True, capture_output=True)
    project = BookTeaserProject(
        id=str(uuid4()), book_id=str(uuid4()), revision=0, aspect="horizontal_fit",
        audio_track_id=str(uuid4()), transition_ms=0, segments_json="[]", state="ready",
        output_path=str(path), output_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        output_duration_ms=4200, error=None,
    )
    data, width, height, duration = _media_manifest(path, project, 50 * 1024 * 1024)
    assert data == path.read_bytes()
    assert (width, height) == (1920, 1080)
    assert abs(duration - 4200) <= 250
