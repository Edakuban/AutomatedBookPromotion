"""Render a deterministic teaser-text layer and burn it into a reel copy."""

from __future__ import annotations

from functools import lru_cache
from io import BytesIO
import os
from pathlib import Path
import re
import subprocess
from typing import Callable
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont

from .book_teasers import _video_encoder_args
from .reel_generation import ReelGenerationError, ffmpeg_binary


CANVAS_SIZE = (1080, 1920)
TEXT_MAX_WIDTH = 840
TEXT_MAX_HEIGHT = 560
Runner = Callable[..., subprocess.CompletedProcess]


@lru_cache(maxsize=1)
def _caption_font_source() -> str:
    windows_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    candidates = [
        windows_fonts / "seguisb.ttf",
        windows_fonts / "segoeui.ttf",
        windows_fonts / "arial.ttf",
        "DejaVuSans.ttf",
    ]
    for candidate in candidates:
        try:
            ImageFont.truetype(str(candidate), size=12)
            return str(candidate)
        except OSError:
            continue
    raise ValueError("No Unicode-capable font is available for the teaser text")


def _wrapped_lines(draw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [text]:
        words = re.sub(r"\s+", " ", paragraph).strip().split(" ")
        line = ""
        for word in words:
            if not word:
                continue
            candidate = f"{line} {word}".strip()
            if not line or draw.textlength(candidate, font=font) <= max_width:
                line = candidate
            else:
                lines.append(line)
                line = word
        if line:
            lines.append(line)
    return lines or [""]


def render_caption_overlay(text: str) -> bytes:
    value = " ".join(str(text).split())
    if not 1 <= len(value) <= 600:
        raise ValueError("Teaser text is empty or too long")
    canvas = Image.new("RGBA", CANVAS_SIZE, (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    selected = None
    lines = []
    for size in range(60, 35, -2):
        font = ImageFont.truetype(_caption_font_source(), size=size)
        candidate = _wrapped_lines(draw, value, font, TEXT_MAX_WIDTH)
        box = draw.multiline_textbbox(
            (0, 0), "\n".join(candidate), font=font, spacing=16, stroke_width=2,
        )
        if len(candidate) <= 8 and box[3] - box[1] <= TEXT_MAX_HEIGHT:
            selected, lines = font, candidate
            break
    if selected is None:
        raise ValueError("Teaser text cannot be fitted into the video safe area")

    rendered = "\n".join(lines)
    box = draw.multiline_textbbox(
        (0, 0), rendered, font=selected, spacing=16, align="center", stroke_width=2,
    )
    width, height = box[2] - box[0], box[3] - box[1]
    padding_x, padding_y = 44, 34
    left = (CANVAS_SIZE[0] - width) // 2 - padding_x
    top = min(1160, CANVAS_SIZE[1] - height - padding_y * 2 - 250)
    right = left + width + padding_x * 2
    bottom = top + height + padding_y * 2
    draw.rounded_rectangle(
        (left, top, right, bottom), radius=30, fill=(7, 12, 20, 178),
        outline=(255, 255, 255, 38), width=2,
    )
    draw.multiline_text(
        (CANVAS_SIZE[0] / 2, top + padding_y - box[1]), rendered,
        font=selected, fill=(255, 255, 255, 255), spacing=16, align="center",
        anchor="ma", stroke_width=2, stroke_fill=(0, 0, 0, 210),
    )
    output = BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()


def render_captioned_reel(
    source_path: str | Path,
    text: str,
    output_path: str | Path,
    *,
    media_binary: str | None = None,
    runner: Runner = subprocess.run,
) -> Path:
    source = Path(source_path).resolve(strict=True)
    target = Path(output_path).resolve()
    if source.suffix.casefold() != ".mp4" or target.suffix.casefold() != ".mp4":
        raise ValueError("Captioned reel source and output must be MP4 files")
    if source == target:
        raise ValueError("Captioned reel output must not replace its clean source")
    target.parent.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    overlay = target.with_name(f".{target.stem}.{token}.png")
    temporary = target.with_name(f".{target.stem}.{token}.mp4")
    overlay.write_bytes(render_caption_overlay(text))
    binary = media_binary or ffmpeg_binary()
    encoder = _video_encoder_args(binary, runner)
    command = [
        binary, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-loop", "1", "-i", str(overlay),
        "-filter_complex",
        # scale2ref's main_w/main_h refer to the overlay, not the reference clip.
        # iw/ih are the reference dimensions here; captions must fit e.g. 512x896 too.
        "[1:v][0:v]scale2ref=w=iw:h=ih[caption][base];"
        "[base][caption]overlay=0:0:shortest=1,format=yuv420p[out]",
        "-map", "[out]", "-map", "0:a?", *encoder,
        "-c:a", "copy", "-movflags", "+faststart", "-map_metadata", "-1",
        "-shortest", str(temporary),
    ]
    try:
        try:
            runner(command, check=True, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise ReelGenerationError(
                "FFmpeg wurde nicht gefunden; die Reel-Textfassung konnte nicht erzeugt werden."
            ) from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").strip()
            message = "FFmpeg konnte den Teasertext nicht in das Kapitel-Reel einbrennen."
            if detail:
                message += f" {detail[-500:]}"
            raise ReelGenerationError(message) from exc
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise ReelGenerationError("FFmpeg hat keine Reel-Textfassung erzeugt.")
        temporary.replace(target)
        return target
    finally:
        overlay.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
