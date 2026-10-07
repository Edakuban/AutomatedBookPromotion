import asyncio
import hashlib
from uuid import uuid4

import httpx
from PIL import Image
from io import BytesIO
import pytest
from pathlib import Path

from bookpromo.carousel_sources import carousel_source_path, prepare_carousel_source
from bookpromo.config import Settings
from bookpromo.r2 import R2Client
from bookpromo.uploads import UploadError


def test_crop_focus_matches_top_bottom_and_landscape(tmp_path):
    for size, axis in [((90, 160), "vertical"), ((160, 90), "horizontal")]:
        source = tmp_path / f"{axis}.png"
        image = Image.new("RGB", size, "blue")
        image.paste("red", (0, 0, size[0] if axis == "vertical" else 30,
                            30 if axis == "vertical" else size[1]))
        image.save(source)
        start = prepare_carousel_source(source, (0, 0))
        end = prepare_carousel_source(source, (1, 1))
        assert start.data != end.data
        with Image.open(BytesIO(start.data)) as framed:
            assert framed.size == (1080, 1350)
            assert framed.getpixel((10, 10))[0] > 240
        with Image.open(BytesIO(end.data)) as framed:
            assert framed.getpixel((10, 10))[2] > 240
        assert prepare_carousel_source(source).data == prepare_carousel_source(source, (.5, .5)).data


@pytest.mark.parametrize("focus", [(-.1, .5), (.5, 1.1), (float('nan'), .5), (.5, float('inf'))])
def test_crop_rejects_invalid_focus(tmp_path, focus):
    with pytest.raises(UploadError):
        prepare_carousel_source(tmp_path / "unused.png", focus)


def test_prepare_carousel_source_is_deterministic_and_exact(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGBA", (400, 400), (10, 20, 30, 128)).save(source)

    first = prepare_carousel_source(source)
    second = prepare_carousel_source(source)

    assert first.data == second.data
    assert first.sha256 == hashlib.sha256(first.data).hexdigest()
    assert (first.width, first.height, first.mime_type) == (1080, 1350, "image/jpeg")
    output = tmp_path / "prepared.jpg"
    output.write_bytes(first.data)
    with Image.open(output) as image:
        assert image.size == (1080, 1350) and image.mode == "RGB"


def test_r2_carousel_upload_uses_immutable_jpeg_contract(tmp_path):
    source = tmp_path / "source.jpg"
    Image.new("RGB", (1080, 1350), "navy").save(source, format="JPEG")
    prepared = prepare_carousel_source(source)
    book_id, owner_id = uuid4(), uuid4()
    key = carousel_source_path("quote", str(book_id), str(owner_id), prepared.sha256)
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200)

    settings = Settings(
        _env_file=None, app_data_dir=tmp_path, reel_storage_provider="cloudflare_r2",
        r2_account_id="a" * 32,
        r2_endpoint=f"https://{'a' * 32}.r2.cloudflarestorage.com",
        r2_access_key_id="access", r2_secret_access_key="secret", r2_bucket="book-media",
    )
    client = R2Client(settings, transport=httpx.MockTransport(respond))
    assert asyncio.run(client.upload_carousel_image(key, prepared.data, prepared.sha256)) == key
    assert seen[0].method == "PUT"
    assert seen[0].headers["content-type"] == "image/jpeg"


def test_v11_migration_freezes_quote_before_chapter_and_is_service_only():
    sql = next((Path(__file__).parents[1] / "supabase/migrations").glob(
        "*_prepared_carousel_images.sql"
    )).read_text(encoding="utf-8")
    quote_select = "where quote_id = picked.id"
    chapter_select = "where chapter_id = picked.chapter_id"
    assert sql.index(quote_select) < sql.index(chapter_select)
    assert "source_image_provider" in sql and "source_image_sha256" in sql
    assert "bookpromo_prepared_image_fail" in sql
    assert "prepared_image_unavailable" in sql
    assert "alter table public.carousel_source_media enable row level security" in sql
    assert "from public, anon, authenticated" in sql
    assert "to service_role" in sql
    assert "update public.bookpromo_schema set version = 11 where version = 10" in sql
