"""Separate durable worker for long-running local ComfyUI reel renders."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from .comfy import ComfyClient
from .characters import (
    CharacterStore, character_scene_prompt, order_scene_characters,
    stitch_character_references,
)
from .config import Settings, load_settings
from .management import ManagementStore
from .reel_content import compose_image_generation_prompt
from .reel_generation import ReelGenerator, ffmpeg_binary, split_audio_segment
from .reels import ReelJobStore, ReelStore
from .uploads import LocalUploadStore


def _safe_error(exc: Exception) -> str:
    # Generation exceptions contain technical state, not quote/book content.
    text = " ".join(str(exc).split())[:700]
    return text or "Die Reel-Generierung konnte nicht abgeschlossen werden."


def run_reel_once(uploads: LocalUploadStore, settings: Settings, stop=None) -> bool:
    reels = ReelStore(
        uploads,
        max_audio_bytes=settings.reel_max_audio_mb * 1024 * 1024,
        max_artifact_bytes=settings.reel_max_video_mb * 1024 * 1024,
    )
    jobs = ReelJobStore(reels)
    job = jobs.claim(kinds={"image", "video"})
    if job is None:
        return False
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
            positions = {
                1: ["sole reference portrait"],
                2: ["left reference portrait", "right reference portrait"],
                3: ["left reference portrait", "center reference portrait", "right reference portrait"],
                4: ["leftmost reference portrait", "second reference portrait from the left",
                    "second reference portrait from the right", "rightmost reference portrait"],
            }[len(references)] if references else []
            position_by_id = {
                character.id: positions[index] for index, (character, _) in enumerate(references)
            }
            snapshot = [
                {
                    "id": character.id,
                    "name": character.name,
                    "revision": character.revision,
                    "description": character.description,
                    "image_prompt": character.image_prompt,
                    "reference_image_sha256": character.reference_image_sha256,
                    "position": position_by_id.get(character.id, "prompt-only character without reference portrait"),
                }
                for character in selected
            ]
            operation = job.payload.get("operation", "scene")
            if operation == "scene":
                book_details = ManagementStore(uploads).get(draft.book_id)["details"]
                effective_prompt = compose_image_generation_prompt(
                    scene_prompt=draft.image_prompt,
                    art_direction=book_details.image_prompt_base,
                )
                generated = generator.generate_image(
                    reel_id=draft.id,
                    image_prompt=character_scene_prompt(effective_prompt, selected),
                )
                candidate = "scene"
            elif operation == "optimize":
                if not references:
                    jobs.finish(job, error="Für die ausgewählten Charaktere fehlt ein Referenzbild.")
                    return True
                scene = reels.candidate_image_path(draft, "scene")
                if scene is None:
                    jobs.finish(job, error="Das zu optimierende Szenenbild wurde nicht gefunden.")
                    return True
                sheet, identity_context = stitch_character_references(
                    references,
                    reels.root / "reel-work" / draft.id / f"character-sheet-{job.input_revision}.png",
                )
                generated = generator.apply_character_references(
                    reel_id=draft.id, scene_image_path=scene,
                    reference_sheet_path=sheet, identity_context=identity_context,
                )
                candidate = "optimized"
            else:
                jobs.finish(job, error="Der angeforderte Bildschritt ist ungültig.")
                return True
            if lost.is_set():
                return True
            with generated.open("rb") as source:
                relative, digest = reels.save_artifact(draft.id, "image", source, generated.name)
            jobs.finish(job, result={"path": relative, "sha256": digest,
                                     "candidate": candidate, "character_snapshot": snapshot})
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
