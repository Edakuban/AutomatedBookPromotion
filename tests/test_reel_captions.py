from io import BytesIO
from pathlib import Path
import subprocess

from PIL import Image, ImageChops
import pytest

from bookpromo.reel_captions import (
    CANVAS_SIZE, render_caption_overlay, render_captioned_reel,
)
from bookpromo.reel_generation import ffmpeg_binary
from bookpromo.book_teasers import _video_encoder_args


def test_caption_overlay_is_deterministic_transparent_and_readable():
    text = "Die Tür bleibt verschlossen. Ängste, Größe und süßes Schweigen."
    first = render_caption_overlay(text)
    second = render_caption_overlay(text)
    assert first == second
    with Image.open(BytesIO(first)) as image:
        assert image.mode == "RGBA" and image.size == CANVAS_SIZE
        alpha = image.getchannel("A")
        assert alpha.getbbox() is not None
        assert alpha.crop((0, 0, 1080, 850)).getbbox() is None
        assert alpha.crop((0, 900, 1080, 1700)).getbbox() is not None


def test_captioned_reel_preserves_source_and_builds_overlay_command(tmp_path):
    source = tmp_path / "clean.mp4"
    source.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32)
    target = tmp_path / "with-text.mp4"
    commands = []
    overlays = []

    def runner(command, **kwargs):
        if "-encoders" in command:
            return subprocess.CompletedProcess(command, 0, " V..... libx264 H.264\n", "")
        commands.append(command)
        overlay = Path(command[command.index("-i", command.index("-i") + 1) + 1])
        overlays.append(overlay.read_bytes())
        Path(command[-1]).write_bytes(b"\x00\x00\x00\x18ftypisom-captioned")
        return subprocess.CompletedProcess(command, 0, "", "")

    result = render_captioned_reel(
        source, "Ein kurzer Teasertext.", target,
        media_binary="ffmpeg-test", runner=runner,
    )
    assert result == target.resolve() and target.is_file()
    assert source.read_bytes().endswith(b"\x00" * 32)
    assert overlays[0].startswith(b"\x89PNG\r\n\x1a\n")
    filters = commands[0][commands[0].index("-filter_complex") + 1]
    assert "scale2ref=w=iw:h=ih" in filters
    assert "main_w" not in filters
    assert commands[0][commands[0].index("-map") + 1] == "[out]"
    assert not list(tmp_path.glob(".*.png")) and not list(tmp_path.glob(".*.mp4"))


@pytest.mark.parametrize("text", ["", "x" * 601])
def test_caption_overlay_rejects_missing_or_oversized_text(text):
    with pytest.raises(ValueError):
        render_caption_overlay(text)


@pytest.mark.parametrize("width,height", [(512, 896), (1080, 1920)])
def test_real_ffmpeg_burns_visible_text_at_actual_video_resolution(tmp_path, width, height):
    binary = ffmpeg_binary()
    try:
        subprocess.run([binary, "-version"], check=True, capture_output=True)
    except FileNotFoundError:
        pytest.skip("FFmpeg is not installed")
    source = tmp_path / "clean.mp4"
    target = tmp_path / "text.mp4"
    subprocess.run([
        binary, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r=12:d=0.5",
        *_video_encoder_args(binary, subprocess.run), "-pix_fmt", "yuv420p", str(source),
    ], check=True, capture_output=True)
    original = source.read_bytes()
    render_captioned_reel(source, "Sichtbarer Kapiteltext. Eine zweite Zeile.", target,
                          media_binary=binary)

    def frame(path):
        result = subprocess.run([
            binary, "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", "0.1",
            "-i", str(path), "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-",
        ], check=True, capture_output=True)
        return Image.open(BytesIO(result.stdout)).convert("RGB")

    clean, captioned = frame(source), frame(target)
    assert clean.size == captioned.size == (width, height)
    difference = ImageChops.difference(clean, captioned)
    safe_area = difference.crop((0, int(height * 0.58), width, int(height * 0.87)))
    assert safe_area.getbbox() is not None
    assert sum(safe_area.convert("L").histogram()[201:]) > 100
    assert source.read_bytes() == original
