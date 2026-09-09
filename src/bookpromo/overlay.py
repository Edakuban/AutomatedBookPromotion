"""Render and persist the book-title layer used by the promotion workflow."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO
from functools import lru_cache
import os
from pathlib import Path
import re
import tempfile
from uuid import UUID

from PIL import Image, ImageDraw, ImageFont

from .management import BookDetails


CANVAS_SIZE = (1080, 1350)
OVERLAY_BUCKET = "book-promotion-assets"
OVERLAY_MARGIN = 72
MAX_TITLE_WIDTH = 800
MAX_TITLE_HEIGHT = 240


class OverlayError(Exception):
    """The selected local font cannot produce a promotion overlay."""


@dataclass(frozen=True)
class OverlayAsset:
    data: bytes
    digest: str


def _font_key(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


@lru_cache(maxsize=1024)
def _font_label(path: Path) -> str:
    """Prefer the font's family name over Windows' often cryptic filename."""
    try:
        family, style = ImageFont.truetype(str(path), size=12).getname()
    except OSError:
        return path.stem
    family = family.strip() or path.stem
    style = style.strip()
    return family if _font_key(style) in {"", "regular", "normal", "book", "roman"} else f"{family} {style}"


def _add_font(fonts: dict[str, Path], name: str, path: Path) -> None:
    if name.strip():
        fonts.setdefault(_font_key(name), path)


def _font_directories(font_directory: Path | None) -> list[Path]:
    if font_directory is not None:
        return [font_directory]
    directories = [
        Path(os.environ.get("WINDIR", r"C:\\Windows")) / "Fonts",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts",
    ]
    return list(dict.fromkeys(directory for directory in directories if directory.is_dir()))


def installed_fonts(font_directory: Path | None = None) -> dict[str, Path]:
    """Return local font display names mapped to their actual files."""
    directories = _font_directories(font_directory)
    if not directories:
        return {}
    fonts: dict[str, Path] = {}
    for directory in directories:
        try:
            paths = list(directory.iterdir())
        except OSError:
            continue
        for path in paths:
            if path.is_file() and path.suffix.casefold() in {".ttf", ".otf", ".ttc"}:
                _add_font(fonts, path.stem, path)
                _add_font(fonts, _font_label(path), path)
    try:
        import winreg

        keys = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
        ]
        for hive, registry_path in keys:
            with winreg.OpenKey(hive, registry_path) as key:
                index = 0
                while True:
                    try:
                        display_name, filename, _ = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    index += 1
                    if not isinstance(filename, str):
                        continue
                    path = Path(filename)
                    if not path.is_absolute():
                        path = next((directory / path for directory in directories if (directory / path).is_file()), path)
                    if path.is_file() and path.suffix.casefold() in {".ttf", ".otf", ".ttc"}:
                        name = re.sub(r"\s*\([^)]*\)\s*$", "", display_name)
                        _add_font(fonts, name, path)
                        _add_font(fonts, _font_label(path), path)
    except (ImportError, OSError):
        pass
    return fonts


def font_names(fonts: dict[str, Path] | None = None) -> list[str]:
    available = fonts if fonts is not None else installed_fonts()
    return sorted({_font_label(path) for path in available.values()}, key=str.casefold)


def resolve_font(name: str, fonts: dict[str, Path] | None = None) -> Path:
    available = fonts if fonts is not None else installed_fonts()
    path = available.get(_font_key(name))
    if path is None:
        raise OverlayError("Die gewählte Schriftart ist lokal nicht installiert. Bitte einen Namen aus der Auswahlliste wählen.")
    return path


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    words = re.sub(r"\s+", " ", text).strip().split(" ")
    lines: list[str] = []
    line = ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if not line or draw.textlength(candidate, font=font) <= width:
            line = candidate
            continue
        lines.append(line)
        line = word
    if line:
        lines.append(line)
    return lines or [""]


def render_title_overlay(details: BookDetails, *, fonts: dict[str, Path] | None = None) -> OverlayAsset | None:
    """Create a transparent 4:5 title layer; n8n adds the chapter label later."""
    if not details.overlay_title_font:
        return None
    font_path = resolve_font(details.overlay_title_font, fonts)
    canvas = Image.new("RGBA", CANVAS_SIZE, (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    selected_font: ImageFont.FreeTypeFont | None = None
    lines: list[str] = []
    for size in range(80, 29, -2):
        candidate_font = ImageFont.truetype(str(font_path), size=size)
        candidate_lines = _wrap(draw, details.title, candidate_font, MAX_TITLE_WIDTH)
        box = draw.multiline_textbbox((0, 0), "\n".join(candidate_lines), font=candidate_font, spacing=8)
        if len(candidate_lines) <= 3 and box[3] - box[1] <= MAX_TITLE_HEIGHT:
            selected_font, lines = candidate_font, candidate_lines
            break
    if selected_font is None:
        raise OverlayError("Der Buchtitel ist für die gewählte Schrift zu lang.")
    position = (OVERLAY_MARGIN, OVERLAY_MARGIN)
    text = "\n".join(lines)
    draw.multiline_text((position[0] + 2, position[1] + 2), text, font=selected_font, fill=(0, 0, 0, 150), spacing=8)
    draw.multiline_text(position, text, font=selected_font, fill=details.overlay_title_color, spacing=8)
    encoded = BytesIO()
    canvas.save(encoded, format="PNG", optimize=True)
    data = encoded.getvalue()
    return OverlayAsset(data=data, digest=hashlib.sha256(data).hexdigest())


class OverlayStore:
    def __init__(self, uploads):
        self.root = uploads.root / "overlays"

    def path(self, book_id: str | UUID) -> Path:
        return self.root / f"{UUID(str(book_id))}.png"

    def preview_exists(self, book_id: str | UUID) -> bool:
        return self.path(book_id).is_file()

    def prepare(self, details: BookDetails) -> OverlayAsset | None:
        return render_title_overlay(details)

    def save(self, book_id: str | UUID, asset: OverlayAsset) -> Path:
        target = self.path(book_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".part", delete=False) as temporary:
            temporary.write(asset.data)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        try:
            os.replace(temporary_path, target)
        finally:
            temporary_path.unlink(missing_ok=True)
        return target

    def load(self, book_id: str | UUID) -> OverlayAsset | None:
        path = self.path(book_id)
        if not path.is_file():
            return None
        data = path.read_bytes()
        return OverlayAsset(data=data, digest=hashlib.sha256(data).hexdigest())
