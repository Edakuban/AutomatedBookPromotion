"""Separate durable worker for long-running local ComfyUI reel renders."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

from .comfy import ComfyClient, load_workflow
from .characters import (
    CharacterStore, character_forbidden_features, character_mask_selector, character_mention_index,
    character_scene_prompt, order_scene_characters,
)
from .config import Settings, load_settings
from .extraction_store import ExtractionStore
from .management import ManagementStore
from .image_flow import description_scene_prompt, prepare_simple_plan
from .image_presets import checked_preset, check_preset_available, identity_edit_prompts
from .text_ai import create_text_client, provider_endpoint_hash, provider_missing, provider_model_id
from .reel_content import generate_motion_prompt, generate_reel_copy
from .reel_generation import (
    CHARACTER_IDENTITY_STRATEGY, PLANNED_SCENE_STRATEGY, REFERENCE_SCENE_STRATEGY,
    CharacterReferenceSpec, ReelGenerator, build_planned_reference_prompt, build_simple_reference_prompt,
    build_reference_scene_prompt, ffmpeg_binary, split_audio_segment,
    _build_reference_scene_workflow,
)
from .reels import ReelJobStore, ReelStore
from .scene_plan import compile_scene_plan, decode_saved_plan, encode_saved_plan, scene_plan_fingerprint, validate_reference_bindings
from .uploads import LocalUploadStore


def _safe_error(exc: Exception) -> str:
    # Generation exceptions contain technical state, not quote/book content.
    text = " ".join(str(exc).split())[:700]
    return text or "Die Reel-Generierung konnte nicht abgeschlossen werden."


def _check_image_source(uploads, draft, payload):
    snapshot = payload.get("source_snapshot")
    if snapshot is None:  # Old/internal callers still render an immutable captured scene.
        return
    record = ExtractionStore(uploads).get(draft.book_id)
    chapter = next((item for item in record.result.chapters
                    if item.id == snapshot.get("chapter_id")), None) if record and record.result else None
    if (record is None or record.revision != snapshot.get("extraction_revision") or chapter is None
            or hashlib.sha256(chapter.source_text.encode("utf-8")).hexdigest() != snapshot.get("chapter_sha256")):
        raise ValueError("Der Buchtext wurde seit dem Bildklick geändert. Bitte erneut anfordern.")


def _prompt_client(settings: Settings, payload: dict):
    provider = payload.get("ai_provider")
    if provider not in {"openwebui", "comfyui_qwen"} or provider_missing(settings, provider):
        raise ValueError("Der ausgewählte Text-KI-Provider ist nicht verfügbar.")
    model_id = payload.get("ai_model_id")
    if (payload.get("ai_endpoint_hash") != provider_endpoint_hash(settings, provider)
            or model_id != provider_model_id(settings, provider)):
        raise ValueError("Die Text-KI-Konfiguration wurde geändert. Bitte erneut anfordern.")
    return create_text_client(settings, provider, model_id=model_id)


def _prompt_character_ids(characters, draft, generated_prompt: str, scene_plan) -> list[str]:
    planned = []
    for actor in scene_plan.actors if scene_plan is not None else ():
        actor_key = " ".join(actor.name.casefold().split())
        matches = [
            character for character in characters
            if actor_key in {
                " ".join(label.casefold().split())
                for label in (character.name, *character.aliases)
            }
        ]
        if len(matches) == 1 and matches[0].id not in planned:
            planned.append(matches[0].id)
    if planned:
        return planned[:4]
    context = " ".join((draft.quote_text, generated_prompt)).casefold()
    detected = [
        character for character in characters
        if character_mention_index(character, context) is not None
    ]
    return [character.id for character in order_scene_characters(detected, context)[:4]]


def _identity_context(snapshot) -> str:
    return " ".join(
        f"{item.get('name', 'Named character')} keeps the same visible identity."
        for item in snapshot if isinstance(item, dict)
    )


def run_reel_once(uploads: LocalUploadStore, settings: Settings, stop=None) -> bool:
    reels = ReelStore(
        uploads,
        max_audio_bytes=settings.reel_max_audio_mb * 1024 * 1024,
        max_artifact_bytes=settings.reel_max_video_mb * 1024 * 1024,
    )
    jobs = ReelJobStore(reels)
    from .chapter_teaser_worker import reconcile_chapter_teaser_media
    reconciled = reconcile_chapter_teaser_media(uploads, reels, jobs)
    job = jobs.claim(kinds={"prompt", "image", "video"})
    if job is None:
        from .book_teaser_worker import run_book_teaser_once
        return run_book_teaser_once(uploads, settings, stop) or reconciled
    done, lost = threading.Event(), threading.Event()

    def heartbeat() -> None:
        while not done.wait(10):
            try:
                if (stop and stop.is_set()) or not jobs.heartbeat(job):
                    lost.set()
                    return
            except (OSError, sqlite3.Error):
                lost.set()
                return

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        draft = reels.get_draft(job.draft_id)
        if draft is None:
            jobs.finish(job, error="Der Reel-Entwurf wurde nicht gefunden.")
            return True
        operation = job.payload.get("operation", "scene")
        if job.kind == "prompt":
            client = _prompt_client(settings, job.payload)
            profile = ManagementStore(uploads).get(draft.book_id)
            if not profile:
                raise ValueError("Das Kreativprofil des Buchs fehlt.")
            if operation == "copy":
                context = job.payload.get("source_context")
                if (not isinstance(context, dict)
                        or set(context) != {"quote", "context_before", "context_after"}
                        or context.get("quote") != draft.quote_text
                        or any(not isinstance(value, str) for value in context.values())
                        or len(context["quote"]) > 14_000
                        or len(context["context_before"]) > 4_000
                        or len(context["context_after"]) > 4_000):
                    raise ValueError("Der Buchkontext für den Textauftrag fehlt oder ist ungültig.")
                characters = CharacterStore(uploads).list(draft.book_id)
                generated = asyncio.run(generate_reel_copy(
                    client, quote=draft.quote_text,
                    book_profile=profile["details"].model_dump(),
                    context_before=context["context_before"],
                    context_after=context["context_after"],
                    characters=tuple({"name": item.name, "aliases": list(item.aliases)}
                                     for item in characters),
                    scene_direction=draft.scene_direction,
                ))
                character_ids = _prompt_character_ids(
                    characters, draft, generated.image_prompt, generated.scene_plan,
                )
                direction = draft.scene_direction or generated.scene_direction
                selected = [item for item in characters if item.id in character_ids]
                proposed = replace(
                    draft, image_prompt=generated.image_prompt, scene_direction=direction,
                    character_ids_json=json.dumps(character_ids),
                )
                fingerprint = scene_plan_fingerprint(
                    quote=proposed.quote_text, image_prompt=proposed.image_prompt,
                    scene_direction=proposed.scene_direction,
                    art_direction=profile["details"].image_prompt_base,
                    characters=selected,
                )
                result = {
                    "caption_addition": generated.addition,
                    "final_caption": generated.caption,
                    "image_prompt": generated.image_prompt,
                    "scene_direction": direction,
                    "character_ids": character_ids,
                }
                if generated.scene_plan is not None:
                    result["scene_plan_json"] = encode_saved_plan(generated.scene_plan, fingerprint)
                jobs.finish(job, result=result)
                return True
            if operation == "motion":
                if not draft.image_approved:
                    raise ValueError("Das ausgewählte Bild wurde noch nicht freigegeben.")
                prompt = asyncio.run(generate_motion_prompt(
                    client,
                    image_prompt=(f"{_identity_context(draft.character_snapshot)} "
                                  f"{draft.image_prompt}").strip(),
                    quote=draft.quote_text,
                    genre=profile["details"].genre,
                    mood=profile["details"].mood,
                    duration_seconds=draft.duration_ms / 1000,
                    intensity="dynamic",
                ))
                if jobs.finish(job, result={"video_prompt": prompt}) and job.payload.get("enqueue_video"):
                    current = reels.get_draft(draft.id)
                    if current is not None:
                        jobs.enqueue(current.id, "video")
                return True
            raise ValueError("Der angeforderte Textschritt ist ungültig.")
        strategy = job.payload.get("strategy", "masked")
        simple = job.kind == "image" and operation == "simple"
        resume_prompt = job.payload.get("comfy_prompt_id") if simple else None
        if operation == "simple" and (not simple or strategy not in {"simple_plain", "simple_text", "simple_references"}):
            raise ValueError("Die angeforderte Bildaktion ist ungültig.")
        preset = job.payload.get("image_preset") if job.kind == "image" else None
        if preset is not None:
            checked_preset(preset)
        if strategy == "planned_scene" and not (job.kind == "image" and operation == "optimize"):
            raise ValueError("Szenenpläne sind nur für die Neuinszenierung verfügbar.")
        if strategy == "scene_plan" and not (job.kind == "image" and operation == "scene"):
            raise ValueError("Dieser Szenenplan ist nur für ein Szenenbild verfügbar.")
        if (simple or strategy in {"planned_scene", "scene_plan"}) and draft.revision != job.input_revision:
            # Fingerprints track scene inputs, but any revision edit supersedes
            # this queued job. Do not spend a render on output finish would discard.
            jobs.finish(job)
            return True
        scene_direction = ""
        if "scene_direction" in job.payload:
            raw_direction = job.payload["scene_direction"]
            if not isinstance(raw_direction, str):
                raise ValueError("Die Szenenregie muss als Text angegeben werden.")
            if len(raw_direction) > 2_000:
                raise ValueError("Die Szenenregie darf höchstens 2000 Zeichen enthalten.")
            scene_direction = raw_direction.strip()
            if scene_direction and not (
                job.kind == "image" and operation == "optimize" and strategy == "reference_scene"
            ):
                raise ValueError("Szenenregie ist nur für die referenzgestützte Neuinszenierung verfügbar.")
        saved_plan = None
        if job.kind == "image":
            character_store = CharacterStore(uploads)
            available = {item.id: item for item in character_store.list(draft.book_id)}
            selected = [available[item] for item in draft.character_ids if item in available]
            selected = order_scene_characters(
                selected, f"{draft.image_prompt}\n{draft.quote_text}",
            )
            references = []
            for character in selected:
                path = character_store.reference_path(character)
                if path is not None:
                    references.append((character, path))
            snapshot = [
                {
                    "id": character.id,
                    "name": character.name,
                    "revision": character.revision,
                    "description": character.description,
                    "image_prompt": character.image_prompt,
                    "reference_image_sha256": character.reference_image_sha256,
                    "mask_selector": character_mask_selector(character),
                    "position": "semantic mask target" if any(
                        item.id == character.id for item, _ in references
                    ) else "prompt-only character without reference portrait",
                }
                for character in selected
            ]
            if simple:
                _check_image_source(uploads, draft, job.payload)
                if settings.comfyui_url is None:
                    raise ValueError("ComfyUI ist nicht eingerichtet. Die Szenenplanung wurde nicht gestartet.")
                if resume_prompt and (
                    not job.payload.get("scene_plan_json")
                    or not isinstance(job.payload.get("effective_image_prompt"), str)
                    or not job.payload["effective_image_prompt"].strip()
                    or len(job.payload["effective_image_prompt"]) > 20000
                ):
                    raise ValueError("Der ursprüngliche Bildauftrag fehlt. Der Lauf wird nicht erneut gestartet.")
                if job.payload.get("phase") in {"planning_started", "render_started"} and not (
                    resume_prompt and job.payload.get("phase") == "render_started"
                ):
                    raise ValueError(
                        "Dieser Bildlauf wurde während eines KI-Schritts unterbrochen. "
                        "Es wird kein weiterer Lauf automatisch gestartet. Bitte den Lauf in ComfyUI "
                        "prüfen und anschließend bewusst erneut anfordern."
                    )
                if len(selected) != len(draft.character_ids):
                    raise ValueError("Die ausgewählten Charaktere fehlen. Bitte die Auswahl prüfen.")
                if strategy == "simple_references":
                    if not references or len(references) != len(selected):
                        raise ValueError("Für jede ausgewählte Figur wird ein gültiges Referenzbild benötigt.")
                    for character, path in references:
                        with path.open("rb") as image:
                            if hashlib.file_digest(image, "sha256").hexdigest() != character.reference_image_sha256:
                                raise ValueError("Ein Charakterreferenzbild ist beschädigt. Bitte die Referenz erneuern.")
                    # Reject unsupported native profiles before an optional paid text call.
                    if not resume_prompt and preset is None:
                        _build_reference_scene_workflow(
                            load_workflow(settings.reel_reference_workflow), width=736, height=1312,
                            reference_images=[f"reference-{index}.png" for index in range(len(references))],
                            prompt="Reference scene profile validation", filename_prefix="BookPromo/preflight",
                        )
                elif not resume_prompt and preset is None and not settings.reel_image_workflow.is_file():
                    raise ValueError("Der Bildworkflow fehlt. Die Szenenplanung wurde nicht gestartet.")
                if preset is not None and not resume_prompt:
                    check_preset_available(ComfyClient(str(settings.comfyui_url)), preset)
                profile = ManagementStore(uploads).get(draft.book_id)
                style = profile["details"].image_prompt_base if profile else ""
                fingerprint = scene_plan_fingerprint(
                    quote=draft.quote_text, image_prompt=draft.image_prompt,
                    scene_direction=draft.scene_direction, art_direction=style, characters=selected,
                )
                if job.payload.get("input_fingerprint") != fingerprint or job.payload.get("art_direction") != style:
                    raise ValueError("Buchstil oder Charaktere wurden geändert. Bitte erneut anfordern.")
                if not job.payload.get("scene_plan_json"):
                    if not jobs.checkpoint_simple_image(job, phase="planning_started"):
                        return True
                saved_plan = prepare_simple_plan(settings, job.payload, draft, selected, fingerprint)
                if not resume_prompt and not jobs.checkpoint_simple_image(
                    job, phase="plan_ready", scene_plan_json=encode_saved_plan(saved_plan.plan, fingerprint),
                ):
                    return True
                # Re-check references/style/revision after text AI, before spending a media render.
                fresh = reels.get_draft(draft.id)
                fresh_characters = {item.id: item for item in character_store.list(draft.book_id)}
                fresh_selected = [fresh_characters[item] for item in draft.character_ids if item in fresh_characters]
                fresh_profile = ManagementStore(uploads).get(draft.book_id)
                _check_image_source(uploads, draft, job.payload)
                if (fresh is None or fresh.revision != job.input_revision or lost.is_set()
                        or scene_plan_fingerprint(
                            quote=fresh.quote_text, image_prompt=fresh.image_prompt,
                            scene_direction=fresh.scene_direction,
                            art_direction=fresh_profile["details"].image_prompt_base if fresh_profile else "",
                            characters=fresh_selected,
                        ) != fingerprint):
                    raise ValueError("Die Bildvorgaben wurden während der Planung geändert. Bitte erneut anfordern.")
                specs = [CharacterReferenceSpec(
                    name=character.name, reference_image_path=path,
                    selector_prompt=character_mask_selector(character), identity_prompt=character.image_prompt,
                    forbidden_features=character_forbidden_features(character), aliases=tuple(character.aliases),
                ) for character, path in references]
                if strategy == "simple_references":
                    for character, path in references:
                        with path.open("rb") as image:
                            if hashlib.file_digest(image, "sha256").hexdigest() != character.reference_image_sha256:
                                raise ValueError("Ein Referenzbild wurde während der Planung verändert. Bitte erneuern.")
                if resume_prompt:
                    # A submitted graph owns its conditioning. Workflow/compiler changes
                    # affect only new renders, never recovery or the old prompt audit.
                    planned_prompt = job.payload["effective_image_prompt"]
                elif preset is not None:
                    planned_prompt = (description_scene_prompt(saved_plan.plan, selected)
                                      if strategy == "simple_text" else compile_scene_plan(saved_plan.plan))
                else:
                    planned_prompt = (build_simple_reference_prompt(saved_plan.plan, specs)
                                      if strategy == "simple_references" else description_scene_prompt(saved_plan.plan, selected))
            if strategy in {"planned_scene", "scene_plan"}:
                saved_plan = decode_saved_plan(job.payload.get("scene_plan_json"))
                if len(selected) != len(draft.character_ids):
                    raise ValueError("Die Charakterauswahl wurde geändert. Bitte den Szenenplan aktualisieren.")
                validate_reference_bindings(saved_plan.plan, selected)
                if strategy == "planned_scene" and (len(references) != len(draft.character_ids) or not references):
                    raise ValueError("Für den Szenenplan braucht jeder ausgewählte Charakter ein gültiges Referenzbild.")
                for character, path in (references if strategy == "planned_scene" else ()):
                    with path.open("rb") as image:
                        if hashlib.file_digest(image, "sha256").hexdigest() != character.reference_image_sha256:
                            raise ValueError("Ein gespeichertes Charakterreferenzbild ist beschädigt. Bitte die Referenz erneuern.")
                profile = ManagementStore(uploads).get(draft.book_id)
                fingerprint = scene_plan_fingerprint(
                    quote=draft.quote_text,
                    image_prompt=draft.image_prompt,
                    scene_direction=draft.scene_direction,
                    art_direction=profile["details"].image_prompt_base if profile else "",
                    characters=[{
                        "id": character.id, "name": character.name,
                        "aliases": list(character.aliases), "revision": character.revision,
                        "reference_image_sha256": character.reference_image_sha256,
                    } for character in selected],
                )
                if saved_plan.fingerprint != fingerprint:
                    raise ValueError("Der Szenenplan ist veraltet. Bitte den Szenenplan aktualisieren.")
                specs = [CharacterReferenceSpec(
                    name=character.name, reference_image_path=path,
                    selector_prompt=character_mask_selector(character), identity_prompt=character.image_prompt,
                    forbidden_features=character_forbidden_features(character), aliases=tuple(character.aliases),
                ) for character, path in references]
                # Validate identity bindings and final conditioning before constructing
                # the media generator or uploading any reference image.
                planned_prompt = (build_planned_reference_prompt(saved_plan.plan, specs)
                                  if strategy == "planned_scene" else compile_scene_plan(saved_plan.plan))
                if strategy == "planned_scene" and reels.candidate_image_path(draft, "scene") is None:
                    raise ValueError("Das zu optimierende Szenenbild wurde nicht gefunden.")
        media_binary = ffmpeg_binary()
        generator = ReelGenerator(
            ComfyClient(str(settings.comfyui_url)),
            image_workflow_path=settings.reel_image_workflow,
            reference_workflow_path=settings.reel_reference_workflow,
            video_workflow_path=settings.reel_video_workflow,
            output_root=reels.root / "reel-work",
            ffmpeg_binary=media_binary,
        )
        if job.kind == "image":
            effective_prompt = None
            if simple:
                effective_prompt = planned_prompt
                edits = []
                if preset is not None and strategy == "simple_references":
                    edits = (job.payload.get("identity_edit_prompts") if resume_prompt
                             else identity_edit_prompts(saved_plan.plan, selected))
                    if not isinstance(edits, list) or len(edits) != len(specs):
                        raise ValueError("Die ursprüngliche Referenzzuordnung fehlt. Kein neuer Lauf wird gestartet.")
                endpoint_hash = provider_endpoint_hash(settings, "comfyui_qwen")
                if strategy == "simple_references":
                    for index, item in enumerate(snapshot, 1):
                        item.update(position=("sequential identity edit; scene outfit and pose preserved" if preset else
                                              "ordered identity reference for restaged scene"), reference_image_index=index)
                if resume_prompt:
                    if job.payload.get("render_endpoint_hash") != endpoint_hash:
                        raise ValueError("Der ComfyUI-Server wurde geändert. Der alte Lauf wird nicht erneut gestartet.")
                    effective_prompt = job.payload.get("effective_image_prompt")
                    if (not isinstance(effective_prompt, str) or not effective_prompt.strip()
                            or len(effective_prompt) > 20000):
                        raise ValueError("Der ursprüngliche Bildauftrag fehlt. Der Lauf wird nicht erneut gestartet.")
                    generated = generator.resume_image(reel_id=draft.id, prompt_id=resume_prompt)
                    candidate = "optimized" if strategy == "simple_references" else "scene"
                else:
                    if not jobs.checkpoint_simple_image(
                        job, phase="render_started", effective_image_prompt=effective_prompt,
                        **({"identity_edit_prompts": edits} if preset is not None else {}),
                    ):
                        return True
                    def submitted(prompt_id):
                        if not jobs.checkpoint_simple_image(
                            job, phase="render_started", prompt_id=prompt_id, render_endpoint_hash=endpoint_hash,
                        ):
                            raise RuntimeError("Der Bildlauf gehört nicht mehr zu diesem Auftrag; kein zweiter Lauf wird gestartet.")
                if not resume_prompt and preset is not None:
                    generated = generator.generate_preset_image(
                        reel_id=draft.id, image_prompt=effective_prompt, preset=preset,
                        references=specs if strategy == "simple_references" else (),
                        edit_prompts=edits, progress=submitted,
                    )
                    candidate = "optimized" if strategy == "simple_references" else "scene"
                elif not resume_prompt and strategy == "simple_references":
                    generated = generator.restage_character_references(
                        reel_id=draft.id, references=specs, scene_plan=saved_plan.plan, target_size=(736, 1312),
                        simple_scene=True, progress=submitted,
                    )
                    candidate = "optimized"
                elif not resume_prompt:
                    generated = generator.generate_image(reel_id=draft.id, image_prompt=effective_prompt, progress=submitted)
                    candidate = "scene"
            elif operation == "scene":
                # Copy/chapter analysis already incorporates the book's rendering
                # style. Never append the raw book basis here: legacy bases may
                # describe a competing location/action that overrides this scene.
                effective_prompt = planned_prompt if saved_plan else character_scene_prompt(draft.image_prompt, selected)
                if preset is not None:
                    check_preset_available(ComfyClient(str(settings.comfyui_url)), preset)
                    generated = generator.generate_preset_image(
                        reel_id=draft.id, image_prompt=effective_prompt, preset=preset,
                    )
                else:
                    generated = generator.generate_image(reel_id=draft.id, image_prompt=effective_prompt)
                candidate = "scene"
            elif operation == "optimize":
                if strategy not in {"masked", "reference_scene", "planned_scene"}:
                    jobs.finish(job, error="Die angeforderte Charakteroptimierung ist ungültig.")
                    return True
                if not references:
                    jobs.finish(job, error="Für die ausgewählten Charaktere fehlt ein Referenzbild.")
                    return True
                scene = reels.candidate_image_path(draft, "scene")
                if scene is None:
                    jobs.finish(job, error="Das zu optimierende Szenenbild wurde nicht gefunden.")
                    return True
                specs = [
                    CharacterReferenceSpec(
                        name=character.name,
                        reference_image_path=path,
                        selector_prompt=character_mask_selector(character),
                        identity_prompt=character.image_prompt,
                        forbidden_features=character_forbidden_features(character),
                        aliases=tuple(character.aliases),
                    )
                    for character, path in references
                ]
                if strategy in {"reference_scene", "planned_scene"}:
                    if len(references) != len(draft.character_ids):
                        jobs.finish(job, error=(
                            "Für die Neuinszenierung braucht jeder ausgewählte Charakter "
                            "ein gültiges Referenzbild. Bitte Referenzen ergänzen oder die Auswahl prüfen."
                        ))
                        return True
                    if saved_plan is not None:
                        conditioning_kwargs = {"scene_plan": saved_plan.plan}
                        effective_prompt = planned_prompt
                    else:
                        conditioning_kwargs = {"scene_prompt": draft.image_prompt}
                        if scene_direction:
                            conditioning_kwargs["scene_direction"] = scene_direction
                        effective_prompt = build_reference_scene_prompt(
                            draft.image_prompt, specs,
                            **({"scene_direction": scene_direction} if scene_direction else {}),
                        )
                    for index, (item, spec) in enumerate(zip(snapshot, specs, strict=True), 1):
                        item["position"] = "ordered identity reference for restaged scene"
                        item["reference_image_index"] = index
                        item["aliases"] = list(spec.aliases)
                    generated = generator.restage_character_references(
                        reel_id=draft.id, scene_image_path=scene,
                        references=specs, **conditioning_kwargs,
                    )
                else:
                    generated = generator.apply_character_references_masked(
                        reel_id=draft.id, scene_image_path=scene, references=specs,
                    )
                candidate = "optimized"
            else:
                jobs.finish(job, error="Der angeforderte Bildschritt ist ungültig.")
                return True
            if lost.is_set():
                return True
            with generated.open("rb") as source:
                relative, digest = reels.save_artifact(draft.id, "image", source, generated.name)
            result = {
                "path": relative, "sha256": digest, "candidate": candidate,
                "character_snapshot": snapshot, "effective_image_prompt": effective_prompt,
                "optimization_strategy": None, "identity_transfer_strategy": None,
            }
            if preset is not None:
                result["image_preset"] = preset
            if saved_plan is not None and operation == "scene":
                result.update(scene_generation_strategy="scene-plan-v1",
                              scene_plan_json=job.payload["scene_plan_json"],
                              scene_plan_fingerprint=saved_plan.fingerprint)
            if simple:
                result.update(
                    scene_generation_strategy=strategy, scene_plan_json=job.payload["scene_plan_json"],
                    scene_plan_fingerprint=saved_plan.fingerprint,
                    identity_transfer_strategy=PLANNED_SCENE_STRATEGY if strategy == "simple_references" else None,
                    ai_provider=job.payload.get("ai_provider"), ai_model_id=job.payload.get("ai_model_id"),
                )
                if preset is not None:
                    result.update(image_preset=preset, identity_edit_prompts=edits,
                                  identity_transfer_strategy="sequential-reference-identity-only-v1"
                                  if strategy == "simple_references" else None)
            if operation == "optimize":
                if saved_plan is not None:
                    result.update(
                        optimization_strategy="planned_scene", identity_transfer_strategy=PLANNED_SCENE_STRATEGY,
                        scene_plan_json=job.payload["scene_plan_json"], scene_plan_fingerprint=saved_plan.fingerprint,
                    )
                elif strategy == "reference_scene":
                    result.update(optimization_strategy="reference_scene",
                                  identity_transfer_strategy=REFERENCE_SCENE_STRATEGY)
                    if scene_direction:
                        result["scene_direction"] = scene_direction
                else:
                    result.update(optimization_strategy="semantic-masks-sequential",
                                  identity_transfer_strategy=CHARACTER_IDENTITY_STRATEGY,
                                  mask_validation_version=2)
            jobs.finish(job, result=result)
            return True

        track = next((item for item in reels.list_audio(draft.book_id) if item.id == draft.audio_track_id), None)
        if track is None or draft.audio_start_ms is None:
            jobs.finish(job, error="Der ausgewählte Song oder Audioausschnitt fehlt.")
            return True
        audio_dir = reels.root / "reel-work" / draft.id
        audio_clip = audio_dir / f"audio-{job.input_revision}.wav"
        split_audio_segment(
            reels.audio_path(track),
            start_seconds=draft.audio_start_ms / 1000,
            duration_seconds=draft.duration_ms / 1000,
            output_path=audio_clip,
            source_root=reels.root,
            output_root=reels.root,
            ffmpeg_binary=media_binary,
        )
        image_path = reels.artifact_path(draft, "image")
        generated = generator.generate_video(
            reel_id=draft.id,
            image_path=image_path,
            audio_clip_path=audio_clip,
            duration_seconds=draft.duration_ms / 1000,
            video_prompt=draft.video_prompt,
        )
        if lost.is_set():
            return True
        with generated.final_video_path.open("rb") as source:
            relative, digest = reels.save_artifact(
                draft.id, "video", source, generated.final_video_path.name,
            )
        jobs.finish(job, result={"path": relative, "sha256": digest})
        return True
    except Exception as exc:
        if not lost.is_set():
            jobs.finish(job, error=_safe_error(exc))
        return True
    finally:
        done.set()
        thread.join(timeout=3)


def reel_worker_main(data_dir: str, max_bytes: int, reader, env_path=None) -> None:
    from .worker import StopSignal, parent_alive

    stop = StopSignal(reader)
    settings = (
        load_settings(Path(env_path))
        if env_path
        else Settings(_env_file=None, app_data_dir=Path(data_dir))
    )
    settings.app_data_dir = Path(data_dir)
    uploads = LocalUploadStore(settings.app_data_dir, max_bytes)
    while not stop.is_set() and parent_alive():
        try:
            worked = run_reel_once(uploads, settings, stop)
        except (OSError, sqlite3.Error):
            worked = False
        if not worked:
            stop.wait(1)
