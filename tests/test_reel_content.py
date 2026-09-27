import asyncio

import pytest

from bookpromo.reel_content import (
    compose_caption, compose_image_generation_prompt, generate_motion_prompt, generate_reel_copy,
)


class FakeClient:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    async def complete_json(self, system, user, result_type, **kwargs):
        self.calls.append((system, user, result_type, kwargs))
        return result_type.model_validate(self.values.pop(0))


def test_caption_keeps_quote_exact_and_builds_existing_n8n_shape():
    quote = "Das ist das exakte Originalzitat – mit Gedankenstrich."
    caption = compose_caption(
        quote=quote, addition="Ein kurzer Begleittext.", title="Mein Buch", author="Autor", target_url="https://example.org",
    )
    assert caption == quote + "\n\nEin kurzer Begleittext.\n\nMein Buch · Autor\n\nhttps://example.org"


def test_caption_limit_is_enforced_after_deterministic_assembly():
    with pytest.raises(ValueError):
        compose_caption(quote="q" * 1700, addition="a" * 600, title="Buch")


def test_copy_generation_only_allows_model_to_supply_addition_and_image_prompt():
    client = FakeClient([{"addition": "Neugierig?", "image_prompt": "Vertical cinematic scene"}])
    result = asyncio.run(generate_reel_copy(
        client, quote="Wortgetreues Zitat", book_profile={"title": "Buch", "author": "A", "target_url": "https://example.org"},
    ))
    assert result.caption.startswith("Wortgetreues Zitat\n\nNeugierig?")
    assert result.image_prompt == "Vertical cinematic scene"
    system = " ".join(client.calls[0][0].split())
    assert "9:16" in system
    assert "konkrete Szene des Zitats" in system
    assert "image_prompt_base ist die verbindliche globale Art Direction" in system
    assert "Medium und Rendering-Stil der image_prompt_base" in system


def test_global_art_direction_is_bound_to_scene_without_becoming_scene_content():
    result = compose_image_generation_prompt(
        scene_prompt="Kira stands alone beside the extinguished campfire.",
        art_direction="Dark fantasy graphic novel, inked contours, ember-red accents.",
    )
    assert "highest priority for visual rendering only" in result
    assert "Dark fantasy graphic novel" in result
    assert "Kira stands alone" in result
    assert "Never import people, creatures, locations, objects or actions" in result
    assert compose_image_generation_prompt(scene_prompt="A forest", art_direction="") == "A forest"


def test_motion_prompt_request_forbids_invisible_motion_and_lipsync():
    client = FakeClient([{"video_prompt": "The camera tracks laterally while rain crosses the lit street."}])
    prompt = asyncio.run(generate_motion_prompt(
        client, image_prompt="A person in rain", quote="Zitat", genre="Thriller", mood="düster",
        duration_seconds=10, intensity="dynamic",
    ))
    assert prompt.startswith("The camera tracks")
    system, user, _, _ = client.calls[0]
    assert "subtle movement" in system and "lip-sync" in system
    assert '"motion_intensity": "dynamic"' in user
