"""Shared, scene-first input contract for quote and chapter image prompts."""

from __future__ import annotations

from typing import Any


def build_scene_context(
    *,
    focus_text: str,
    book_profile: dict[str, Any],
    context_before: str = "",
    context_after: str = "",
    source_kind: str,
) -> dict[str, Any]:
    """Keep narrative evidence separate from optional book-wide visual guidance.

    The complete management profile deliberately must not be sent to the scene
    model.  Summaries, spoilers, general cast descriptions and world synopses are
    useful elsewhere, but are common sources of unrelated people, places and
    actions in a concrete image prompt.
    """
    focus = str(focus_text).strip()
    if not focus:
        raise ValueError("Scene focus text is empty")

    support = {
        key: value
        for key in ("genre", "mood")
        if (value := str(book_profile.get(key) or "").strip())
    }
    art_direction = str(book_profile.get("image_prompt_base") or "").strip()
    return {
        "scene_source": {
            "kind": source_kind,
            "context_before": str(context_before or "").strip(),
            "focus_text": focus,
            "context_after": str(context_after or "").strip(),
        },
        "supporting_book_context": support,
        "global_art_direction": {
            "style_source": art_direction,
            "allowed_use": (
                "medium, rendering style, color palette, lighting, contrast, texture, atmosphere"
            ),
            "forbidden_use": (
                "people, creatures, locations, objects, poses, actions, plot events"
            ),
        },
    }
