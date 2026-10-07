"""Shared quote/chapter image conditioning, without portrait staging leakage."""
from __future__ import annotations

import asyncio

from .identity_context import reference_appearance_hints
from .scene_plan import (
    compile_scene_plan, decode_saved_plan, encode_saved_plan, generate_scene_plan,
    validate_reference_bindings,
)
from .text_ai import create_text_client, provider_endpoint_hash, provider_missing, provider_model_id


def description_scene_prompt(plan, characters) -> str:
    """Descriptions only: never attach image pixels or canonical prose from the book."""
    validate_reference_bindings(plan, characters)
    prompt = compile_scene_plan(plan)
    for character in characters:
        appearance = reference_appearance_hints(character.image_prompt)
        if appearance:
            prompt += f"\nAPPEARANCE OF {character.name}: {appearance}"
    prompt += (
        "\nThe scene determines actions, pose and expression. Appearance descriptions only determine "
        "each named figure's face, build, species and outfit. Do not add held objects from descriptions."
    )
    if len(prompt) > 20000:
        raise ValueError("Der gemeinsame Bildauftrag ist zu lang.")
    return prompt


def prepare_simple_plan(settings, payload, draft, characters, fingerprint):
    """Use one explicit provider call, or reuse exactly the current durable plan."""
    if payload.get("input_fingerprint") != fingerprint:
        raise ValueError("Szene, Buchstil oder Charaktere wurden geändert. Bitte das Bild erneut anfordern.")
    encoded = payload.get("scene_plan_json")
    if encoded:
        saved = decode_saved_plan(encoded)
        if saved.fingerprint != fingerprint:
            raise ValueError("Der gespeicherte Szenenauftrag ist veraltet.")
        validate_reference_bindings(saved.plan, characters)
        compile_scene_plan(saved.plan)
        return saved
    provider = payload.get("ai_provider")
    if provider not in {"openwebui", "comfyui_qwen"} or provider_missing(settings, provider):
        raise ValueError("Der ausgewählte Text-KI-Provider ist nicht verfügbar. Es erfolgt kein Providerwechsel.")
    if (payload.get("ai_endpoint_hash") != provider_endpoint_hash(settings, provider)
            or ("image_preset" not in payload
                and payload.get("ai_model_id") != provider_model_id(settings, provider))):
        raise ValueError("Die Text-KI-Konfiguration wurde geändert. Bitte erneut anfordern.")
    context = payload.get("source_context")
    if (not isinstance(context, dict) or set(context) != {"quote", "context_before", "context_after"}
            or context.get("quote") != draft.quote_text
            or any(not isinstance(value, str) for value in context.values())
            or len(context["quote"]) > 14000
            or len(context["context_before"]) > 4000 or len(context["context_after"]) > 4000):
        raise ValueError("Der Buchkontext für den Szenenauftrag fehlt oder ist ungültig.")
    client = create_text_client(settings, provider, model_id=payload["ai_model_id"])
    plan = asyncio.run(generate_scene_plan(
        client, **context, image_prompt=draft.image_prompt, scene_direction=draft.scene_direction,
        art_direction=payload["art_direction"],
        selected_cast=True,
        characters=tuple({"name": character.name, "aliases": list(character.aliases)} for character in characters),
    ))
    validate_reference_bindings(plan, characters)
    return decode_saved_plan(encode_saved_plan(plan, fingerprint))
