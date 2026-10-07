"""Deterministic preparation of reviewed source images for carousel posts."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import math
from pathlib import Path
from typing import Literal

from PIL import Image, ImageOps, UnidentifiedImageError

from .uploads import UploadError

CAROUSEL_SOURCE_SIZE = (1080, 1350)
MAX_CAROUSEL_SOURCE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class PreparedCarouselSource:
    data: bytes
    sha256: str
    width: int = CAROUSEL_SOURCE_SIZE[0]
    height: int = CAROUSEL_SOURCE_SIZE[1]
    mime_type: str = "image/jpeg"

    @property
    def size_bytes(self) -> int:
        return len(self.data)


def prepare_carousel_source(path: Path, centering: tuple[float, float] = (0.5, 0.5)) -> PreparedCarouselSource:
    """Normalize a local reviewed image to the exact Instagram 4:5 source contract."""
    if len(centering) != 2 or any(not math.isfinite(value) or not 0 <= value <= 1 for value in centering):
        raise UploadError("Bitte einen gültigen Bildausschnitt auswählen.", 422)
    try:
        with Image.open(path) as opened:
            opened.load()
            image = ImageOps.exif_transpose(opened)
            if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
            normalized = ImageOps.fit(
                image, CAROUSEL_SOURCE_SIZE, method=Image.Resampling.LANCZOS,
                centering=centering,
            )
            output = BytesIO()
            normalized.save(output, format="JPEG", quality=92, optimize=True, progressive=False,
                            subsampling=0)
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        raise UploadError("Das ausgewählte Bild kann nicht für das Carousel vorbereitet werden.", 422) from None
    data = output.getvalue()
    if (not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9")
            or len(data) > MAX_CAROUSEL_SOURCE_BYTES):
        raise UploadError("Das vorbereitete Carousel-Bild ist ungültig oder zu groß.", 422)
    return PreparedCarouselSource(data=data, sha256=sha256(data).hexdigest())


def carousel_source_path(
    scope: Literal["quote", "chapter"], book_id: str, owner_id: str, digest: str,
) -> str:
    if scope not in {"quote", "chapter"} or len(digest) != 64:
        raise ValueError("Ungültige Carousel-Bildidentität.")
    return f"carousel-sources/{scope}s/{book_id}/{owner_id}/{digest}.jpg"
