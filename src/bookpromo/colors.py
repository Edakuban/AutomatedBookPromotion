"""Shared color helpers for locally rendered promotion assets."""

from __future__ import annotations


CAROUSEL_BACKGROUND_COLOR_STRENGTH = 0.4


def darken_hex_color(color: str, strength: float = CAROUSEL_BACKGROUND_COLOR_STRENGTH) -> str:
    """Blend a six-digit hex color with black and return normalized hex."""
    channels = (
        round(int(color[index : index + 2], 16) * strength)
        for index in (1, 3, 5)
    )
    return "#" + "".join(f"{channel:02X}" for channel in channels)
