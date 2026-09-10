"""Tests for local book assets and the pre-rendered carousel CTA slide."""

from contextlib import closing
from io import BytesIO
import sqlite3

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont
import pytest

import bookpromo.book_assets as book_assets_module
import bookpromo.carousel_end_slide as carousel_end_slide_module
from bookpromo.book_assets import BookAssetStore
from bookpromo.carousel_end_slide import (
    _wrap,
    carousel_end_cache_path,
    render_book_carousel_end_slide,
    render_carousel_end_slide,
    render_cover_mockup,
    validate_carousel_end_slide,
)
from bookpromo.management import BookDetails, ManagementStore
from bookpromo.overlay import font_names, installed_fonts, render_title_overlay
from bookpromo.uploads import UploadError
from bookpromo.web import create_app
from test_analysis import setup


def image_bytes(size, color, *, image_format="PNG", mode="RGB"):
    output = BytesIO()
    Image.new(mode, size, color).save(output, format=image_format)
    return output.getvalue()


def noisy_jpeg(size=(900, 1300)):
    output = BytesIO()
    Image.effect_noise(size, 100).convert("RGB").save(output, format="JPEG", quality=95)
    return output.getvalue()


def available_font():
    fonts = installed_fonts()
    if not fonts:
        pytest.skip("No local fonts are available for carousel rendering")
    name, path = next(iter(fonts.items()))
    return name, path


def render_fonts(name, path):
    return {name: path, "arial": path}


def form_data(snapshot):
    values = snapshot["details"].model_dump()
    if values.pop("promotion_enabled"):
        values["promotion_enabled"] = "on"
    return {**values, "revision": str(snapshot["revision"]), "suggestion_id": snapshot["suggestion_id"]}


def test_asset_reads_do_not_create_tables(setup):
    _, uploads, book, _, _ = setup
    store = BookAssetStore(uploads)
    assert store.list(book.id) == {}
    with closing(uploads._read_connection()) as connection:
        assert not store._table_exists(connection)


def test_assets_are_normalized_revisioned_and_conflict_protected(setup):
    _, uploads, book, _, _ = setup
    store = BookAssetStore(uploads)
    cover = store.save(book.id, "cover_front", BytesIO(image_bytes((900, 1300), "#355070", image_format="JPEG")),
                       "Mein Cover.jpg", 0)
    logo = store.save(book.id, "logo", BytesIO(image_bytes((240, 90), (255, 255, 255, 120), mode="RGBA")),
                      "Logo.png", 0)
    assert cover.revision == logo.revision == 1
    assert cover.media_type == logo.media_type == "image/png"
    assert (cover.width, cover.height) == (900, 1300)
    assert store.path(cover).is_file() and store.path(logo).is_file()
    with Image.open(store.path(logo)) as opened:
        assert opened.mode == "RGBA" and opened.getpixel((0, 0))[3] == 120
    with pytest.raises(UploadError) as conflict:
        store.save(book.id, "logo", BytesIO(image_bytes((240, 90), "black")), "Neu.png", 0)
    assert conflict.value.status == 409
    previous_path = store.path(logo)
    updated = store.save(book.id, "logo", BytesIO(image_bytes((260, 100), "black")), "Neu.png", 1)
    assert updated.revision == 2 and store.get(book.id, "logo") == updated
    assert not previous_path.exists()


@pytest.mark.parametrize("kind,filename,data,status", [
    ("cover_front", "cover.txt", b"not-an-image", 415),
    ("cover_front", "cover.png", b"not-an-image", 415),
    ("cover_front", "cover.png", image_bytes((799, 1200), "navy"), 400),
    ("logo", "logo.png", image_bytes((63, 31), "white"), 400),
])
def test_asset_validation_rejects_invalid_sources(setup, kind, filename, data, status):
    _, uploads, book, _, _ = setup
    with pytest.raises(UploadError) as error:
        BookAssetStore(uploads).save(book.id, kind, BytesIO(data), filename, 0)
    assert error.value.status == status


def test_asset_validation_applies_exif_orientation_and_generated_paths(setup):
    _, uploads, book, _, _ = setup
    source = BytesIO()
    image = Image.new("RGB", (1200, 900), "#223344")
    exif = Image.Exif()
    exif[274] = 6
    image.save(source, format="JPEG", exif=exif)
    asset = BookAssetStore(uploads).save(
        book.id, "cover_front", BytesIO(source.getvalue()), "../../Cover Final.jpg", 0
    )
    assert (asset.width, asset.height) == (900, 1200)
    assert asset.original_filename == "Cover Final.jpg"
    assert asset.relative_path.startswith(f"book-assets/{book.id}/cover_front-")


def test_asset_path_rejects_tampered_database_paths(setup):
    _, uploads, book, _, _ = setup
    store = BookAssetStore(uploads)
    store.save(book.id, "logo", BytesIO(image_bytes((240, 90), "white")), "logo.png", 0)
    with sqlite3.connect(uploads.db_path) as connection, connection:
        connection.execute(
            "update local_book_assets set relative_path='originals/not-an-asset.png' where book_id=? and kind='logo'",
            (str(book.id),),
        )
    with pytest.raises(UploadError, match="Bildpfad"):
        store.path(store.get(book.id, "logo"))


def test_asset_validation_enforces_byte_and_pixel_limits(setup, monkeypatch):
    _, uploads, book, _, _ = setup
    store = BookAssetStore(uploads)
    monkeypatch.setattr(book_assets_module, "MAX_ASSET_BYTES", 64)
    with pytest.raises(UploadError) as too_large:
        store.save(book.id, "logo", BytesIO(b"x" * 65), "logo.png", 0)
    assert too_large.value.status == 413
    monkeypatch.setattr(book_assets_module, "MAX_ASSET_BYTES", 10 * 1024 * 1024)
    monkeypatch.setattr(book_assets_module, "MAX_ASSET_PIXELS", 100)
    with pytest.raises(UploadError) as too_many_pixels:
        store.save(book.id, "logo", BytesIO(image_bytes((11, 10), "white")), "logo.png", 0)
    assert too_many_pixels.value.status == 413


def test_webp_assets_are_supported(setup):
    _, uploads, book, _, _ = setup
    store = BookAssetStore(uploads)
    asset = store.save(
        book.id, "logo", BytesIO(image_bytes((240, 90), (255, 255, 255, 80), image_format="WEBP", mode="RGBA")),
        "logo.webp", 0,
    )
    assert asset.media_type == "image/png" and store.path(asset).is_file()


def test_cover_mockup_and_end_slide_are_deterministic_4_by_5_jpegs():
    name, path = available_font()
    cover = Image.new("RGB", (900, 1400), "#36506c")
    cover_draw = ImageDraw.Draw(cover)
    cover_draw.rectangle((80, 100, 820, 1300), outline="white", width=12)
    logo = Image.new("RGBA", (260, 100), (255, 255, 255, 0))
    logo_draw = ImageDraw.Draw(logo)
    logo_draw.rounded_rectangle((0, 0, 259, 99), radius=20, fill=(240, 180, 55, 255))
    details = BookDetails(title="Verzerrte Wahrheit", carousel_end_text="Jetzt das Buch entdecken\nHeute eintauchen.",
                          overlay_title_font=name, overlay_title_color="#E5B94A")
    fonts = render_fonts(name, path)
    overlay = render_title_overlay(details, fonts=fonts)
    assert overlay is not None
    mockup = render_cover_mockup(cover)
    assert mockup.mode == "RGBA" and mockup.getchannel("A").getbbox() is not None
    first = render_carousel_end_slide(details, cover, logo, overlay, fonts=fonts)
    second = render_carousel_end_slide(details, cover, logo, overlay, fonts=fonts)
    assert first == second and len(first.data) < 8 * 1024 * 1024
    with Image.open(BytesIO(first.data)) as rendered:
        assert rendered.format == "JPEG" and rendered.mode == "RGB" and rendered.size == (1080, 1350)
        assert rendered.info.get("icc_profile")
    metadata = validate_carousel_end_slide(first.data)
    assert metadata.width == 1080 and metadata.height == 1350 and metadata.media_type == "image/jpeg"


@pytest.mark.parametrize("size", [(900, 2600), (1600, 800)])
def test_cover_mockup_handles_unusual_cover_shapes(size):
    mockup = render_cover_mockup(Image.new("RGB", size, "#405060"))
    assert mockup.getchannel("A").getbbox() is not None


def test_end_slide_rejects_text_that_cannot_fit():
    name, path = available_font()
    details = BookDetails(title="Buch", carousel_end_text="W" * 500, overlay_title_font=name)
    fonts = render_fonts(name, path)
    overlay = render_title_overlay(details, fonts=fonts)
    with pytest.raises(UploadError, match="zu lang"):
        render_carousel_end_slide(
            details, Image.new("RGB", (900, 1300)), Image.new("RGBA", (240, 90)),
            overlay, fonts=fonts,
        )


def test_long_cta_words_are_split_with_visible_hyphens():
    _, path = available_font()
    font = ImageFont.truetype(str(path), size=48)
    draw = ImageDraw.Draw(Image.new("RGB", (400, 200)))
    word = "Donaudampfschifffahrtsgesellschaft."
    lines = _wrap(draw, word, font, 180)
    assert any(line.endswith("-") for line in lines[:-1])
    assert len(lines[-1]) >= 4
    assert "".join(line.removesuffix("-") for line in lines) == word


def test_book_renderer_uses_stored_assets_and_cache(setup, monkeypatch):
    _, uploads, book, _, _ = setup
    name, path = available_font()
    store = BookAssetStore(uploads)
    store.save(book.id, "cover_front", BytesIO(image_bytes((900, 1300), "#334455")), "cover.png", 0)
    store.save(book.id, "logo", BytesIO(image_bytes((240, 90), (255, 255, 255, 180), mode="RGBA")), "logo.png", 0)
    details = BookDetails(title="Buch", carousel_end_text="Mehr erfahren", overlay_title_font=name)
    fonts = render_fonts(name, path)
    overlay = render_title_overlay(details, fonts=fonts)
    rendered = render_book_carousel_end_slide(store, book.id, details, overlay, fonts=fonts)
    assert rendered.data[:3] == b"\xff\xd8\xff"
    assert carousel_end_cache_path(store, book.id, rendered.digest).is_file()
    monkeypatch.setattr(
        carousel_end_slide_module, "_render_prepared_end_slide",
        lambda *args, **kwargs: pytest.fail("cache miss for unchanged carousel inputs"),
    )
    assert render_book_carousel_end_slide(store, book.id, details, overlay, fonts=fonts) == rendered


def test_web_uploads_assets_enforces_setup_and_renders_preview(setup):
    settings, uploads, book, _, _ = setup
    name, _ = available_font()
    management = ManagementStore(uploads)
    base = f"/books/local/{book.id}/settings"
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        page = client.get(base)
        assert page.status_code == 200
        assert "Frontcover für 3D-Darstellung" in page.text
        assert 'name="publication_mode"' in page.text
        fields = form_data(management.get(book.id))
        fields.update(promotion_enabled="on", publication_mode="auto", carousel_end_text="Jetzt entdecken",
                      overlay_title_font=name)
        blocked = client.post(base, data=fields)
        assert blocked.status_code == 400 and "Frontcover hochladen" in blocked.text and "Logo hochladen" in blocked.text

        cover = noisy_jpeg()
        assert len(cover) > 256 * 1024
        logo = image_bytes((240, 90), (245, 180, 45, 220), mode="RGBA")
        assert client.post(base + "/assets/cover_front", data={"revision": "0"},
                           files={"file": ("cover.jpg", cover, "image/jpeg")}).status_code == 200
        assert client.post(base + "/assets/logo", data={"revision": "0"},
                           files={"file": ("logo.png", logo, "image/png")}).status_code == 200
        assert client.get(base + "/assets/cover_front.png").headers["content-type"] == "image/png"
        assert client.post(base + "/assets/logo", data={"revision": "0"},
                           files={"file": ("logo.png", logo, "image/png")}).status_code == 409

        saved = client.post(base, data=fields)
        assert saved.status_code == 200 and "Bucheinstellungen gespeichert" in saved.text
        details = management.get(book.id)["details"]
        assert details.promotion_enabled and details.publication_mode == "auto"
        preview = client.get(f"/books/local/{book.id}/carousel-end.jpg")
        assert preview.status_code == 200 and preview.headers["content-type"] == "image/jpeg"
        with Image.open(BytesIO(preview.content)) as rendered:
            assert rendered.size == (1080, 1350)
        assert "Vorgemerkt" in client.get("/").text


def test_asset_upload_requires_local_origin(setup):
    settings, _, book, _, _ = setup
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        response = client.post(
            f"/books/local/{book.id}/settings/assets/logo",
            data={"revision": "0"},
            files={"file": ("logo.png", image_bytes((240, 90), "white"), "image/png")},
            headers={"Origin": "https://foreign.example"},
        )
    assert response.status_code == 403
