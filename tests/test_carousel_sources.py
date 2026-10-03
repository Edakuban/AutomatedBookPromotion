import asyncio
import hashlib
from uuid import uuid4

import httpx
from PIL import Image
from pathlib import Path

from bookpromo.carousel_sources import carousel_source_path, prepare_carousel_source
from bookpromo.config import Settings
from bookpromo.r2 import R2Client


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
