"""Deterministic rendering and caching for carousel end slides."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
from uuid import UUID

from PIL import Image, ImageCms, ImageDraw, ImageFilter, ImageFont, ImageOps, UnidentifiedImageError

from .book_assets import BookAssetStore
from .overlay import CANVAS_SIZE, OverlayAsset, resolve_font
from .uploads import UploadError


CTA_RENDERER_VERSION = "4"
CTA_FONT_NAME = "Arial"
JPEG_LIMIT = 8 * 1024 * 1024
SRGB_PROFILE = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


@dataclass(frozen=True)
class CarouselRender:
    data: bytes
    digest: str


@dataclass(frozen=True)
class CarouselImageMetadata:
    width: int
    height: int
    media_type: str
    size_bytes: int


def carousel_end_cache_path(asset_store: BookAssetStore, book_id: str | UUID, digest: str) -> Path:
    if not len(digest) == 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("Invalid carousel digest")
    return asset_store.asset_directory(book_id) / "carousel" / f"end-{digest}.jpg"


def _load_cached_carousel(asset_store: BookAssetStore, book_id: str | UUID, digest: str) -> bytes | None:
    path = carousel_end_cache_path(asset_store, book_id, digest)
    if not path.is_file():
        return None
    try:
        data = path.read_bytes()
        validate_carousel_end_slide(data)
    except (OSError, UploadError):
        return None
    return data


def _save_cached_carousel(asset_store: BookAssetStore, book_id: str | UUID, render: CarouselRender) -> Path:
    validate_carousel_end_slide(render.data)
    target = carousel_end_cache_path(asset_store, book_id, render.digest)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".part", delete=False) as temporary:
        temporary.write(render.data)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    try:
        os.replace(temporary_path, target)
    finally:
        temporary_path.unlink(missing_ok=True)
    return target


def _solve(matrix: list[list[float]], values: list[float]) -> list[float]:
    size = len(values)
    rows = [matrix[index][:] + [values[index]] for index in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(rows[row][column]))
        if abs(rows[pivot][column]) < 1e-12:
            raise ValueError("Perspective transform is singular")
        rows[column], rows[pivot] = rows[pivot], rows[column]
        divisor = rows[column][column]
        rows[column] = [value / divisor for value in rows[column]]
        for row in range(size):
            if row == column:
                continue
            factor = rows[row][column]
            rows[row] = [left - factor * right for left, right in zip(rows[row], rows[column])]
    return [rows[index][-1] for index in range(size)]


def _perspective_coefficients(
    destination: list[tuple[float, float]], source: list[tuple[float, float]]
) -> list[float]:
    matrix: list[list[float]] = []
    values: list[float] = []
    for (x, y), (u, v) in zip(destination, source):
        matrix.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        values.append(u)
        matrix.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        values.append(v)
    return _solve(matrix, values)


def render_cover_mockup(front_cover: Image.Image) -> Image.Image:
    """Create a deterministic 2.5D book from a front-cover image only."""
    cover = ImageOps.exif_transpose(front_cover).convert("RGBA")
    cover.thumbnail((460, 720), Image.Resampling.LANCZOS)
    source_width, source_height = cover.size
    page_depth = max(12, round(source_width * 0.03))
    tilt = max(20, min(38, round(source_height * 0.045)))
    output_width = source_width + page_depth + 88
    output_height = source_height + tilt + page_depth + 78
    top = 22
    front_left = 34
    destination = [
        (front_left, top + tilt),
        (front_left + source_width - 1, top),
        (front_left + source_width - 1, top + source_height - 1),
        (front_left, top + source_height + tilt - 1),
    ]
    source = [(0, 0), (source_width - 1, 0), (source_width - 1, source_height - 1), (0, source_height - 1)]
    coefficients = _perspective_coefficients(destination, source)
    transformed = cover.transform(
        (output_width, output_height),
        Image.Transform.PERSPECTIVE,
        coefficients,
        resample=Image.Resampling.BICUBIC,
        fillcolor=(0, 0, 0, 0),
    )

    book = Image.new("RGBA", (output_width, output_height), (0, 0, 0, 0))
    book_draw = ImageDraw.Draw(book)
    paper = (229, 219, 194, 255)
    paper_shadow = (169, 158, 136, 255)
    right_pages = [
        destination[1],
        (destination[1][0] + page_depth, destination[1][1] + 6),
        (destination[2][0] + page_depth, destination[2][1] + 6),
        destination[2],
    ]
    bottom_pages = [
        destination[3],
        destination[2],
        (destination[2][0] + page_depth, destination[2][1] + 6),
        (destination[3][0] + page_depth, destination[3][1] + 6),
    ]
    book_draw.polygon(right_pages, fill=paper_shadow)
    book_draw.polygon(bottom_pages, fill=paper)
    book.alpha_composite(transformed)

    edge_draw = ImageDraw.Draw(book)
    edge_draw.line((destination[0], destination[3]), fill=(8, 12, 18, 115), width=2)
    edge_draw.line((destination[3], destination[2]), fill=(250, 247, 237, 190), width=2)
    edge_draw.line((destination[1], right_pages[1]), fill=(250, 244, 226, 210), width=2)
    edge_draw.line((destination[2], right_pages[2]), fill=(116, 108, 92, 180), width=2)
    for offset in range(3, page_depth, 4):
        edge_draw.line(
            ((destination[1][0] + offset, destination[1][1] + 2),
             (destination[2][0] + offset, destination[2][1] + 5)),
            fill=(205, 196, 175, 110), width=1,
        )

    shadow_mask = book.getchannel("A").filter(ImageFilter.GaussianBlur(22))
    shadow = Image.new("RGBA", book.size, (0, 0, 0, 0))
    shadow.putalpha(shadow_mask.point(lambda value: round(value * 0.46)))
    dark_shadow = Image.new("RGBA", book.size, (0, 0, 0, 255))
    dark_shadow.putalpha(shadow.getchannel("A"))
    result = Image.new("RGBA", book.size, (0, 0, 0, 0))
    result.alpha_composite(dark_shadow, (18, 24))
    result.alpha_composite(book)
    return result


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    paragraphs = text.splitlines() or [text]
    lines: list[str] = []
    for paragraph in paragraphs:
        words = paragraph.split()
        if not words:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if current and draw.textlength(candidate, font=font) <= width:
                current = candidate
                continue
            if current:
                lines.append(current)
                current = ""
            while draw.textlength(word, font=font) > width and len(word) > 1:
                split = 1
                while (split < max(2, len(word) - 4)
                       and draw.textlength(word[: split + 1] + "-", font=font) <= width):
                    split += 1
                lines.append(word[:split] + "-")
                word = word[split:]
            current = word
        if current:
            lines.append(current)
    return lines or [""]


def validate_carousel_end_slide(data: bytes) -> CarouselImageMetadata:
    if not data:
        raise UploadError("Die gerenderte Schlussseite ist leer.")
    if len(data) > JPEG_LIMIT:
        raise UploadError("Die gerenderte Schlussseite überschreitet 8 MB.", 413)
    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            if (image.format != "JPEG" or image.mode != "RGB" or image.size != CANVAS_SIZE
                or not image.info.get("icc_profile")):
                raise UploadError("Die Schlussseite muss ein RGB-JPEG mit 1080 × 1350 Pixeln sein.")
    except UploadError:
        raise
    except (UnidentifiedImageError, OSError, ValueError):
        raise UploadError("Die gerenderte Schlussseite ist keine gültige JPEG-Datei.") from None
    return CarouselImageMetadata(
        width=CANVAS_SIZE[0], height=CANVAS_SIZE[1], media_type="image/jpeg", size_bytes=len(data)
    )


def _prepared_sources(
    details,
    front_cover: Image.Image,
    logo: Image.Image,
    title_overlay: OverlayAsset,
    fonts: dict[str, Path] | None,
) -> tuple[Image.Image, Image.Image, Path, str]:
    cover = ImageOps.exif_transpose(front_cover).convert("RGBA")
    normalized_logo = ImageOps.exif_transpose(logo).convert("RGBA")
    font_path = resolve_font(CTA_FONT_NAME, fonts)
    digest = hashlib.sha256()
    for value in (
        CTA_RENDERER_VERSION,
        json.dumps({"text": str(getattr(details, "carousel_end_text", "")).strip()},
                   ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        title_overlay.digest,
        hashlib.sha256(font_path.read_bytes()).hexdigest(),
    ):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    for image in (cover, normalized_logo):
        digest.update(image.mode.encode("ascii"))
        digest.update(f"{image.width}x{image.height}".encode("ascii"))
        digest.update(image.tobytes())
        digest.update(b"\0")
    return cover, normalized_logo, font_path, digest.hexdigest()


def _render_prepared_end_slide(
    details,
    front_cover: Image.Image,
    logo: Image.Image,
    title_overlay: OverlayAsset,
    font_path: Path,
    digest: str,
) -> CarouselRender:
    if not str(getattr(details, "carousel_end_text", "")).strip():
        raise UploadError("Bitte einen Text für die Schlussseite eintragen.")
    if len(title_overlay.data) > 1024 * 1024:
        raise UploadError("Das Buchtitel-Overlay ist zu groß.")
    try:
        with Image.open(BytesIO(title_overlay.data)) as opened:
            opened.load()
            overlay = opened.convert("RGBA")
    except (UnidentifiedImageError, OSError):
        raise UploadError("Das Buchtitel-Overlay ist beschädigt.") from None
    if overlay.size != CANVAS_SIZE:
        raise UploadError("Das Buchtitel-Overlay hat nicht das erwartete 4:5-Format.")
    canvas = Image.new("RGB", CANVAS_SIZE)
    draw = ImageDraw.Draw(canvas)
    top_color = (31, 39, 54)
    bottom_color = (8, 13, 23)
    for y in range(CANVAS_SIZE[1]):
        ratio = y / (CANVAS_SIZE[1] - 1)
        color = tuple(round(start + (end - start) * ratio) for start, end in zip(top_color, bottom_color))
        draw.line((0, y, CANVAS_SIZE[0], y), fill=color)
    mockup = render_cover_mockup(front_cover)
    mockup.thumbnail((535, 800), Image.Resampling.LANCZOS)
    canvas.paste(mockup, (CANVAS_SIZE[0] - mockup.width - 35, 330), mockup)

    text = str(details.carousel_end_text).strip()
    selected_font: ImageFont.FreeTypeFont | None = None
    selected_lines: list[str] = []
    for size in range(58, 31, -2):
        candidate = ImageFont.truetype(str(font_path), size=size)
        lines = _wrap(draw, text, candidate, 350)
        box = draw.multiline_textbbox((0, 0), "\n".join(lines), font=candidate, spacing=14)
        if len(lines) <= 10 and box[3] - box[1] <= 590:
            selected_font = candidate
            selected_lines = lines
            break
    if selected_font is None:
        raise UploadError("Der Text für die Schlussseite ist für das Layout zu lang.")
    rendered_text = "\n".join(selected_lines)
    text_box = draw.multiline_textbbox((0, 0), rendered_text, font=selected_font, spacing=14)
    text_height = text_box[3] - text_box[1]
    text_y = 735 - text_height // 2
    accent_color = getattr(details, "overlay_title_color", "#FFFFFF")
    draw.rounded_rectangle((68, text_y - 8, 76, text_y + text_height + 8), radius=4, fill=accent_color)
    draw.multiline_text(
        (105, text_y), rendered_text, font=selected_font, fill=(255, 255, 255), spacing=14,
        stroke_width=1, stroke_fill=(7, 11, 18),
    )

    logo.thumbnail((230, 120), Image.Resampling.LANCZOS)
    logo_x = CANVAS_SIZE[0] - logo.width - 72
    logo_y = CANVAS_SIZE[1] - logo.height - 64
    canvas.paste(logo, (logo_x, logo_y), logo)
    canvas_rgba = canvas.convert("RGBA")
    canvas_rgba.alpha_composite(overlay)

    encoded = BytesIO()
    canvas_rgba.convert("RGB").save(
        encoded, format="JPEG", quality=90, optimize=True, progressive=False, icc_profile=SRGB_PROFILE
    )
    data = encoded.getvalue()
    validate_carousel_end_slide(data)
    return CarouselRender(data=data, digest=digest)


def render_carousel_end_slide(
    details,
    front_cover: Image.Image,
    logo: Image.Image,
    title_overlay: OverlayAsset,
    *,
    fonts: dict[str, Path] | None = None,
) -> CarouselRender:
    cover, normalized_logo, font_path, digest = _prepared_sources(
        details, front_cover, logo, title_overlay, fonts
    )
    return _render_prepared_end_slide(details, cover, normalized_logo, title_overlay, font_path, digest)


def render_book_carousel_end_slide(
    asset_store: BookAssetStore,
    book_id: str | UUID,
    details,
    title_overlay: OverlayAsset,
    *,
    fonts: dict[str, Path] | None = None,
) -> CarouselRender:
    assets = asset_store.list(book_id)
    missing = [kind for kind in ("cover_front", "logo") if kind not in assets]
    if missing:
        raise UploadError("Bitte zuerst Frontcover und Logo hochladen.", 409)
    cover = asset_store.open_image(assets["cover_front"])
    logo = asset_store.open_image(assets["logo"])
    cover, logo, font_path, digest = _prepared_sources(details, cover, logo, title_overlay, fonts)
    cached = _load_cached_carousel(asset_store, book_id, digest)
    if cached is not None:
        return CarouselRender(data=cached, digest=digest)
    rendered = _render_prepared_end_slide(details, cover, logo, title_overlay, font_path, digest)
    _save_cached_carousel(asset_store, book_id, rendered)
    return rendered
