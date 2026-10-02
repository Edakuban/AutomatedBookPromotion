"""Freeze reviewed full trailers into the existing daily/scheduled n8n contract."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess
from uuid import UUID, uuid4, uuid5

from .book_teasers import BookTeaserProject
from .config import Settings
from .database import SupabaseRepository
from .publication import PublicationDefaults
from .r2 import R2Client, R2Error
from .reel_generation import _sibling_ffprobe, ffmpeg_binary
from .uploads import UploadError


def book_teaser_asset_id(project: BookTeaserProject, *, requeue: bool = False) -> str:
    """Normal retries identify the same reviewed render; explicit requeue is new."""
    if requeue:
        return str(uuid4())
    return str(uuid5(UUID(project.id), f"book-teaser:{project.revision}:{project.output_sha256}"))


def book_teaser_platforms(defaults: PublicationDefaults, aspect: str) -> tuple[str, ...]:
    """Disabled long-video branches are not presented as working destinations."""
    return tuple(platform for platform in defaults.selected()
                 if platform == "youtube" or (platform == "instagram" and aspect == "vertical"))


def _media_manifest(path: Path, project: BookTeaserProject, max_bytes: int) -> tuple[bytes, int, int, int]:
    if (project.state != "ready" or not project.output_sha256 or not project.output_duration_ms
            or project.aspect not in {"vertical", "horizontal_crop", "horizontal_fit"}):
        raise UploadError("Bitte das Gesamt-Teaservideo zuerst fertig rendern.", 409)
    if not path.is_file():
        raise UploadError("Das fertige Gesamt-Teaservideo fehlt.", 409)
    if not 12 <= path.stat().st_size <= max_bytes:
        raise UploadError("Das Gesamt-Teaservideo überschreitet das eingestellte Upload-Limit.", 413)
    data = path.read_bytes()
    if (not 12 <= len(data) <= max_bytes or data[4:8] != b"ftyp"
            or hashlib.sha256(data).hexdigest() != project.output_sha256):
        raise UploadError("Das Gesamt-Teaservideo ist verändert oder beschädigt.", 409)
    try:
        probe = subprocess.run(
            [_sibling_ffprobe(ffmpeg_binary()), "-v", "error", "-show_streams", "-show_format",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        manifest = json.loads(probe.stdout)
        streams = manifest["streams"]
        video = next(stream for stream in streams if stream.get("codec_type") == "video")
        if not any(stream.get("codec_type") == "audio" for stream in streams):
            raise ValueError()
        width, height = int(video["width"]), int(video["height"])
        seconds = float(manifest["format"]["duration"])
        if not math.isfinite(seconds):
            raise ValueError()
        duration = round(seconds * 1000)
    except (OSError, subprocess.SubprocessError, KeyError, TypeError, ValueError, StopIteration):
        raise UploadError("Das Gesamt-Teaservideo konnte nicht als vollständige MP4 geprüft werden.", 409) from None
    expected = (1080, 1920) if project.aspect == "vertical" else (1920, 1080)
    if ((width, height) != expected or not 4000 <= duration <= 600000
            or abs(duration - project.output_duration_ms) > 250):
        raise UploadError("Format oder Dauer des Gesamt-Teaservideos stimmen nicht mit dem geprüften Export überein.", 409)
    return data, width, height, duration


async def enqueue_book_teaser(
    db: SupabaseRepository, settings: Settings, *, project: BookTeaserProject, path: Path,
    book_profile: dict, audio_title: str | None, defaults: PublicationDefaults,
    platforms: tuple[str, ...], title: str, description: str, queue_mode: str,
    scheduled_for: str | None, requeue: bool = False,
) -> dict:
    """No generation: validate before any upload, freeze metadata, enqueue atomically.

    The route must verify local-origin, project revision and a current book sync.
    Supabase schema v10 is a prerequisite, never migrated automatically here.
    """
    if (not platforms or len(set(platforms)) != len(platforms)
            or any(platform not in book_teaser_platforms(defaults, project.aspect) for platform in platforms)):
        raise UploadError("Für das Gesamt-Teaservideo sind YouTube und für Hochformat Instagram verfügbar; Facebook/TikTok sind noch nicht unterstützt.", 400)
    title, description = title.strip(), description.strip()
    if not 1 <= len(title) <= 300 or not 1 <= len(description) <= 5000:
        raise UploadError("Titel oder Beschreibung haben eine ungültige Länge.", 400)
    if queue_mode not in {"daily", "scheduled"} or (queue_mode == "daily" and scheduled_for):
        raise UploadError("Bitte tägliche Warteschlange oder einen gültigen Termin auswählen.", 400)
    if queue_mode == "scheduled":
        try:
            scheduled = datetime.fromisoformat(scheduled_for or "")
            if scheduled.tzinfo is None or scheduled <= datetime.now(timezone.utc):
                raise ValueError()
        except ValueError:
            raise UploadError("Der Veröffentlichungszeitpunkt muss mit Zeitzone in der Zukunft liegen.", 400) from None
    max_bytes = settings.book_teaser_max_video_mb * 1024 * 1024
    data, width, height, duration = await asyncio.to_thread(_media_manifest, Path(path), project, max_bytes)
    await db.check_schema(book_teasers=True)
    asset_id = book_teaser_asset_id(project, requeue=requeue)
    object_path = f"{asset_id}/{project.output_sha256}.mp4"
    if defaults.storage_provider == "cloudflare_r2":
        try:
            client = R2Client(settings)
            await client.upload_reel(object_path, data, project.output_sha256, max_bytes=max_bytes)
        except R2Error as exc:
            raise UploadError(str(exc), 503) from None
        bucket, public_url = settings.r2_bucket, client.public_url(object_path)
    else:
        await db.upload_book_teaser(object_path, data)
        bucket, public_url = "book-promotion-reels", None
    asset = {
        "id": asset_id, "source_kind": "book_teaser", "quote_id": None, "chapter_id": None,
        "book_id": project.book_id, "quote_text": title, "addition": "", "title": title,
        "description": description, "book_profile": book_profile,
        "image_prompt": "Reviewed chapter scenes assembled as a whole-book teaser.",
        "video_prompt": f"Whole-book teaser {project.id}, revision {project.revision}, {project.aspect}, crossfade {project.transition_ms} ms.",
        "storage_provider": defaults.storage_provider, "storage_bucket": bucket,
        "storage_path": object_path, "public_url": public_url,
        "media_sha256": project.output_sha256, "size_bytes": len(data),
        "duration_ms": duration, "width": width, "height": height,
        "audio_title": audio_title, "audio_start_ms": 0,
    }
    publications = [{
        "platform": platform, "account_id": defaults.platform(platform).account_id,
        "queue_mode": queue_mode, "scheduled_for": scheduled_for, "priority": 0,
        "title": title, "description": description,
        "options": dict(defaults.platform(platform).options),
    } for platform in platforms]
    return await db.enqueue_reel(asset, publications)
