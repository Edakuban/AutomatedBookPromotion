"""Restartable worker for whole-chapter teaser analysis and media chaining."""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import sqlite3
import threading
from uuid import uuid4

from .chapter_teasers import (
    ChapterTeaserError, ChapterTeaserStore, ChapterTeaserSuggestion, PROMPT_VERSION,
    analyze_whole_chapter, chapter_quote_id, chapter_source_key, compose_chapter_caption,
)
from .characters import CharacterStore, character_mention_index, order_scene_characters
from .config import load_settings
from .extraction_store import ExtractionStore
from .management import ManagementStore
from .openwebui import OpenWebUIError
from .reels import ReelJobStore, ReelStore
from .reel_captions import render_captioned_reel
from .scene_plan import (
    describe_scene_direction, encode_saved_plan, scene_plan_fingerprint, validate_reference_bindings,
)
from .text_ai import TextAIError, create_text_client, provider_endpoint_hash


def _cancelled(stop, lost: threading.Event) -> bool:
    parent = multiprocessing.parent_process()
    return lost.is_set() or (stop is not None and stop.is_set()) or (
        parent is not None and not parent.is_alive()
    )


def _caption(teaser_text: str, context: dict) -> str:
    return compose_chapter_caption(teaser_text, context)


def _plan_characters(plan, characters):
    """Bind known identities only to participants in the resolved visible moment."""
    actor_names = {" ".join(actor.name.casefold().split()) for actor in plan.actors}
    selected = [character for character in characters if actor_names & {
        " ".join(label.casefold().split()) for label in (character.name, *character.aliases)
    }]
    validate_reference_bindings(plan, selected)
    positions = {" ".join(actor.name.casefold().split()): index for index, actor in enumerate(plan.actors)}
    selected.sort(key=lambda character: min(
        positions[label] for value in (character.name, *character.aliases)
        if (label := " ".join(value.casefold().split())) in positions
    ))
    return selected[:4]


def _scene_image_payload(draft, preset=None, art_direction=""):
    if preset is not None and draft.scene_plan_json:
        from .scene_plan import decode_saved_plan
        saved = decode_saved_plan(draft.scene_plan_json)
        return {"operation": "simple", "strategy": "simple_plain", "phase": "plan_ready",
                "image_preset": preset, "scene_plan_json": draft.scene_plan_json,
                "input_fingerprint": saved.fingerprint,
                "art_direction": art_direction,
                "source_context": {"quote": draft.quote_text, "context_before": "", "context_after": ""}}
    if preset is not None:
        return {"operation": "scene", "image_preset": preset}
    if draft.scene_plan_json:
        return {"operation": "scene", "strategy": "scene_plan", "scene_plan_json": draft.scene_plan_json}
    return {"operation": "scene"}


def run_chapter_teaser_once(
    uploads,
    env_path=None,
    stop=None,
    *,
    settings=None,
    api_factory=None,
) -> bool:
    store = ChapterTeaserStore(uploads)
    job = store.claim()
    if job is None:
        return False
    finished, lost = threading.Event(), threading.Event()

    def heartbeat():
        while not finished.wait(2):
            try:
                if _cancelled(stop, lost) or not store.heartbeat(job):
                    lost.set()
                    return
            except (OSError, sqlite3.Error):
                lost.set()
                return

    heart = threading.Thread(target=heartbeat, daemon=True)
    heart.start()
    try:
        if settings is None:
            if env_path is None:
                raise ChapterTeaserError("Keine ENV-Datei für den Kapitel-Teaser-Worker zugeordnet.")
            settings = load_settings(env_path)
        provider = job.get("provider") or "openwebui"
        expected_endpoints = {provider_endpoint_hash(settings, provider)}
        if provider == "openwebui":
            # Runs created before provider selection stored the legacy URL-only hash.
            from .analysis_store import endpoint_hash
            expected_endpoints.add(endpoint_hash(settings))
        if (
            job["prompt_version"] != PROMPT_VERSION
            or job["endpoint_hash"] not in expected_endpoints
        ):
            raise ChapterTeaserError(
                "Die Kapitel-Teaser-Konfiguration wurde geändert. Bitte einen neuen Lauf starten."
            )
        source = ExtractionStore._decode(job["source_json"])
        context = json.loads(job["context_json"])
        image_preset = context.pop("image_preset", None)
        if image_preset is not None:
            from .comfy import ComfyClient
            from .image_presets import check_preset_available
            check_preset_available(ComfyClient(str(settings.comfyui_url)), image_preset)
        if api_factory is None:
            api = create_text_client(settings, provider, model_id=job["model_id"])
        else:
            api = api_factory(settings.model_copy(update={"openwebui_model": job["model_id"]}))
        reels = ReelStore(
            uploads,
            max_audio_bytes=settings.reel_max_audio_mb * 1024 * 1024,
            max_artifact_bytes=settings.reel_max_video_mb * 1024 * 1024,
        )
        reel_jobs = ReelJobStore(reels)
        track = next(
            (item for item in reels.list_audio(job["book_id"]) if item.id == job["audio_track_id"]),
            None,
        )
        if track is None:
            raise ChapterTeaserError("Der ausgewählte Book-Teaser-Song fehlt.")
        pending = set(store.pending_positions(job))

        async def execute():
            for chapter in source.chapters:
                if chapter.position not in pending:
                    continue
                if _cancelled(stop, lost):
                    raise ChapterTeaserError("Die Kapitelanalyse wurde unterbrochen.")
                characters = CharacterStore(uploads).list(job["book_id"])
                relevant_characters = [character for character in characters
                                       if character_mention_index(character, chapter.source_text) is not None]
                management = ManagementStore(uploads).get(job["book_id"])
                art_direction = management["details"].image_prompt_base if management else ""
                current_context = {**context, "image_prompt_base": art_direction}
                store.reserve_call(job, chapter.position)
                try:
                    suggestion = await analyze_whole_chapter(
                        api, chapter=chapter, book_context=current_context, characters=relevant_characters,
                        duration_seconds=job["duration_ms"] / 1000,
                    )
                except OpenWebUIError as exc:
                    if exc.code not in {"structured", "truncated"}:
                        raise
                    store.fail_plan(job, chapter.id, chapter.position, str(exc))
                    continue
                except TextAIError as exc:
                    if exc.code != "structured":
                        raise
                    store.fail_plan(job, chapter.id, chapter.position, str(exc))
                    continue
                except ChapterTeaserError as exc:
                    store.fail_plan(job, chapter.id, chapter.position, str(exc))
                    continue
                if suggestion.scene_plan is not None:
                    try:
                        selected = _plan_characters(suggestion.scene_plan, characters)
                    except ValueError as exc:
                        store.fail_plan(job, chapter.id, chapter.position, str(exc))
                        continue
                else:
                    # Legacy suggestions keep their existing mention-based selection.
                    scene_context = (
                        f"{suggestion.source_excerpt}\n{suggestion.scene_summary}\n{suggestion.image_prompt}"
                    ).casefold()
                    selected = [
                        character for character in characters
                        if character_mention_index(character, scene_context) is not None
                    ]
                    selected = order_scene_characters(selected, scene_context)[:4]
                draft = reels.get_or_create_draft(
                    job["book_id"], chapter_source_key(source.version_id, chapter.id),
                    job["id"], chapter_quote_id(chapter.id), suggestion.source_excerpt,
                )
                step_ms = max(0, job["duration_ms"] - job["transition_ms"])
                audio_start_ms = min(
                    (chapter.position - 1) * step_ms,
                    max(0, track.duration_ms - job["duration_ms"]),
                )
                draft = reels.update_draft(
                    draft.id, draft.revision,
                    duration_ms=job["duration_ms"],
                    audio_track_id=track.id,
                    audio_cue_id=None,
                    audio_start_ms=audio_start_ms,
                    caption_addition=suggestion.teaser_text,
                    final_caption=_caption(suggestion.teaser_text, context),
                    image_prompt=suggestion.image_prompt,
                    video_prompt=suggestion.video_prompt,
                    character_ids=[character.id for character in selected],
                    scene_direction=(describe_scene_direction(suggestion.scene_plan)
                                     if suggestion.scene_plan is not None else ""),
                )
                if suggestion.scene_plan is not None:
                    plan_json = encode_saved_plan(suggestion.scene_plan, scene_plan_fingerprint(
                        quote=draft.quote_text, image_prompt=draft.image_prompt,
                        scene_direction=draft.scene_direction, art_direction=art_direction,
                        characters=selected,
                    ))
                    draft = reels.save_scene_plan(draft.id, draft.revision, plan_json)
                store.save_plan(job, chapter.id, suggestion, draft.id)
                image_payload = _scene_image_payload(draft, image_preset, art_direction)
                if image_payload.get("operation") == "simple":
                    image_payload["source_snapshot"] = {
                        "extraction_revision": job["extraction_revision"], "chapter_id": chapter.id,
                        "chapter_sha256": hashlib.sha256(chapter.source_text.encode("utf-8")).hexdigest(),
                    }
                reel_jobs.enqueue(draft.id, "image", image_payload)
                store.set_plan_state(job["id"], draft.id, "image_queued")

        asyncio.run(execute())
        if not _cancelled(stop, lost):
            store.finish_analysis(job)
    except (ChapterTeaserError, OpenWebUIError, TextAIError, ValueError) as exc:
        if not _cancelled(stop, lost):
            store.fail(job, str(exc))
    except Exception:
        if not _cancelled(stop, lost):
            store.fail(
                job,
                "Die automatische Kapitel-Reel-Produktion konnte nicht abgeschlossen werden. "
                "Fertige Kapitelstände bleiben erhalten.",
            )
    finally:
        finished.set()
        heart.join(timeout=3)
    return True


def reconcile_chapter_teaser_media(uploads, reels: ReelStore, jobs: ReelJobStore) -> bool:
    """Advance durable image -> video chains without coupling ReelJobStore to this feature."""
    store = ChapterTeaserStore(uploads)
    rows = store.reconciliation_rows()
    if not rows:
        return False
    touched_runs: set[str] = set()
    changed = False
    for row in rows:
        touched_runs.add(row["run_id"])
        draft = reels.get_draft(row["draft_id"]) if row["draft_id"] else None
        if draft is None:
            store.set_plan_state(row["run_id"], row["draft_id"], "failed", "Der Reel-Entwurf fehlt.")
            changed = True
            continue
        statuses = jobs.status(draft.id)
        if row["state"] == "analyzed":
            if row.get("manual_image_review"):
                # A saved prompt is not authorization to run AI, even when another
                # chapter reopens this run's media reconciliation loop.
                continue
            if draft.selected_image_path and not draft.image_stale:
                # Explicit review checkpoint: keep the initial scene selected until
                # the user optionally creates/selects a character-optimized image.
                continue
            else:
                context = json.loads(row.get("context_json") or "{}")
                jobs.enqueue(draft.id, "image", _scene_image_payload(
                    draft, context.get("image_preset"), context.get("image_prompt_base", ""),
                ))
                store.set_plan_state(row["run_id"], draft.id, "image_queued")
            changed = True
            continue
        if row["state"] == "image_queued":
            latest = next((item for item in statuses if item["kind"] == "image"), None)
            if latest and latest["state"] in {"queued", "running"}:
                continue
            if draft.selected_image_path and not draft.image_stale:
                store.set_plan_state(row["run_id"], draft.id, "analyzed")
                changed = True
                continue
            if latest and latest["state"] in {"failed", "cancelled", "stale"}:
                store.set_plan_state(
                    row["run_id"], draft.id, "failed",
                    latest.get("error") or "Das Szenenbild konnte nicht erzeugt werden.",
                )
                changed = True
        elif row["state"] == "video_queued":
            latest = next((item for item in statuses if item["kind"] == "video"), None)
            if latest and latest["state"] in {"queued", "running"}:
                continue
            if (latest and latest["state"] in {"failed", "cancelled", "stale"}
                    and (not draft.selected_video_path or draft.video_stale)):
                store.set_plan_state(
                    row["run_id"], draft.id, "failed",
                    latest.get("error") or "Das Kapitelvideo konnte nicht erzeugt werden.",
                )
                changed = True
                continue
            if draft.selected_video_path and not draft.video_stale:
                try:
                    existing = store.text_video_path(
                        row, book_id=draft.book_id, draft_id=draft.id,
                    )
                    if existing is None:
                        clean = reels.artifact_path(draft, "video")
                        if clean is None:
                            raise ChapterTeaserError("Das textfreie Kapitel-Reel fehlt.")
                        work = reels.root / "reel-work" / draft.id
                        work.mkdir(parents=True, exist_ok=True)
                        captioned = work / f"captioned-{uuid4().hex}.mp4"
                        try:
                            render_captioned_reel(clean, draft.caption_addition, captioned)
                            with captioned.open("rb") as source:
                                relative, digest = reels.save_artifact(
                                    draft.id, "video", source, captioned.name,
                                )
                            store.save_text_video(row["run_id"], draft.id, relative, digest)
                        finally:
                            captioned.unlink(missing_ok=True)
                    store.set_plan_state(row["run_id"], draft.id, "done")
                except Exception as exc:
                    message = " ".join(str(exc).split())[:700]
                    store.set_plan_state(
                        row["run_id"], draft.id, "failed",
                        message or "Die Reel-Textfassung konnte nicht erzeugt werden.",
                    )
                changed = True
                continue
    for run_id in touched_runs:
        store.refresh_run(run_id)
    return changed
