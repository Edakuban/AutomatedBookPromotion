"""Local DOCX uploads and optional read-only Supabase overview."""

from pathlib import Path
from contextlib import asynccontextmanager
import asyncio
from datetime import datetime
import re
import sqlite3
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo
from typing import Literal

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException
from starlette.datastructures import UploadFile
from starlette.concurrency import run_in_threadpool

from .config import Settings
from .database import DatabaseError, SupabaseRepository
from .uploads import LocalUploadStore, UploadError
from .upload_http import UploadLimitMiddleware
from .extraction import Boundary
from .extraction_store import ExtractionStore
from .jobs import JobStore
from .worker import worker_lifespan
from .openwebui import OpenWebUIClient, OpenWebUIError
from .model_settings import save_model
from .analysis import AnalysisOptions
from .analysis_store import AnalysisStore
from .management import BookDetails, FILTERS, ManagementStore, quote_key
from .overlay import OverlayError, OverlayStore, font_names
from .book_assets import BookAssetStore, MAX_ASSET_BYTES
from .carousel_end_slide import render_book_carousel_end_slide
from .sync import SyncStore
from .reels import ReelJobStore, ReelStore
from .publication import PLATFORMS, PlatformDefault, PublicationDefaults, platform_options
from .r2 import R2Client, R2Error
from .reel_content import (
    compose_caption, compose_image_generation_prompt, generate_motion_prompt, generate_reel_copy,
)
from .reel_prompts import validate_video_prompt
from .reel_generation import ReelGenerator, split_audio_segment, wav_waveform
from .book_teasers import BookTeaserStore, TeaserSegment, complete_chapter_segments
from .chapter_teasers import ChapterTeaserStore
from .book_teaser_worker import BookTeaserJobStore
from .teaser_publication import book_teaser_platforms
from .comfy import ComfyClient
from .characters import CharacterStore, character_mention_index, order_scene_characters
from .text_ai import (
    TextAIError, configured_providers, create_text_client, provider_missing,
)
from pydantic import ValidationError

PACKAGE_DIR = Path(__file__).resolve().parent


def _detected_character_ids(characters, quote, *, generated_prompt: str = "") -> list[str]:
    context = " ".join(
        (quote.context_before, quote.text, quote.context_after, generated_prompt)
    ).casefold()
    detected = [
        character for character in characters
        if character_mention_index(character, context) is not None
    ]
    return [character.id for character in order_scene_characters(detected, context)[:4]]


def _snapshot_identity_context(snapshot) -> str:
    if not snapshot:
        return ""
    return " ".join(
        f"{item.get('position', f'person {index + 1}')} is {item.get('name', 'the named character')}; "
        f"preserve this exact face and identity throughout."
        for index, item in enumerate(snapshot)
    )


def _reel_delivery_asset_id(draft, *, requeue: bool) -> str:
    """Choose an immutable cloud asset id for an initial or repeated delivery."""
    if draft.state == "stocked":
        if not requeue:
            raise UploadError(
                "Dieses Reel wurde bereits übertragen. Bitte den erneuten Transfer ausdrücklich starten.", 409,
            )
        return str(uuid4())
    if requeue:
        raise UploadError("Das Reel wurde noch nicht übertragen und kann direkt eingestellt werden.", 409)
    return draft.id


def create_app(settings: Settings, *, repository: SupabaseRepository | None = None, start_worker: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            async with worker_lifespan(settings.app_data_dir, settings.app_max_upload_mb * 1024 * 1024, settings._env_path):
                yield
        else:
            yield

    app = FastAPI(title="Book Promotion", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    max_bytes = settings.app_max_upload_mb * 1024 * 1024
    app.add_middleware(
        UploadLimitMiddleware,
        max_bytes=max_bytes + 1024 * 1024,
        asset_max_bytes=MAX_ASSET_BYTES + 1024 * 1024,
        audio_max_bytes=settings.reel_max_audio_mb * 1024 * 1024 + 1024 * 1024,
    )
    local_store = LocalUploadStore(settings.app_data_dir, max_bytes)
    extraction_store = ExtractionStore(local_store)
    job_store = JobStore(local_store)
    analysis_store = AnalysisStore(local_store)
    management_store = ManagementStore(local_store)
    overlay_store = OverlayStore(local_store)
    asset_store = BookAssetStore(local_store)
    sync_store = SyncStore(local_store)
    reel_store = ReelStore(
        local_store,
        max_audio_bytes=settings.reel_max_audio_mb * 1024 * 1024,
        max_artifact_bytes=settings.reel_max_video_mb * 1024 * 1024,
    )
    reel_jobs = ReelJobStore(reel_store)
    teaser_store = BookTeaserStore(local_store)
    chapter_teaser_store = ChapterTeaserStore(local_store)
    teaser_job_store = BookTeaserJobStore(local_store)
    character_store = CharacterStore(local_store)
    sync_lock = asyncio.Lock()
    webui_lock = asyncio.Lock()
    reel_ai_lock = asyncio.Lock()
    comfy_lock = asyncio.Lock()
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    templates.env.filters["berlin_time"] = lambda value: (
        value.astimezone(ZoneInfo("Europe/Berlin")).strftime("%d.%m.%Y, %H:%M") if value else "Noch nie"
    )
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # no-referrer makes browsers send Origin: null on native form POSTs.
        # Preserve the local origin while withholding referrers from other sites.
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    @app.get("/", response_class=HTMLResponse, name="books")
    async def books(request: Request, page: int = Query(default=1, ge=1, le=100000),
                    local_page: int = Query(default=1, ge=1, le=100000), deleted: bool = False):
        local_books, local_more, local_error = [], False, None
        local_jobs = {}
        local_management = {}
        try:
            local_books, local_more = await run_in_threadpool(local_store.list_books, local_page)
            local_jobs = await run_in_threadpool(job_store.statuses, [b.id for b in local_books])
            for book in local_books:
                local_management[book.id] = await run_in_threadpool(management_store.get, book.id)
        except (OSError, sqlite3.Error):
            local_error = "Die lokale Buchablage konnte nicht gelesen werden. Bitte Datenverzeichnis und Zugriffsrechte prüfen."
        items, more, error, connected = [], False, None, False
        try:
            db = repository or SupabaseRepository(settings)
            await db.check_schema()
            items, more = await db.list_books(page=page)
            connected = True
        except DatabaseError as exc:
            error = str(exc)
        return templates.TemplateResponse(
            request=request, name="books.html", context={
                "active_page": "books", "books": items, "database_error": error,
                "database_connected": connected, "page": page, "has_more": more,
                "local_books": local_books, "local_has_more": local_more, "local_page": local_page,
                "local_jobs": local_jobs,
                "local_management": local_management,
                "local_error": local_error, "max_upload_mb": settings.app_max_upload_mb,
                "local_deleted": deleted,
                "show_remote": settings.supabase_enabled or repository is not None,
            },
        )

    def require_local_origin(request: Request):
        origin = request.headers.get("origin")
        if (request.url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or request.headers.get("sec-fetch-site") == "cross-site"
            or (origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}")):
            raise UploadError("Bitte diese Aktion direkt in der lokalen Book-Promotion-Oberfläche ausführen.", 403)

    async def reel_context(book_id: UUID, chapter_id: UUID, quote_id: UUID, *, create: bool = False):
        """Resolve a usable quote from the current analysis; stale URLs never mutate a different quote."""
        book = await run_in_threadpool(local_store.get_book, book_id)
        if book is None:
            raise HTTPException(404)
        record = await run_in_threadpool(extraction_store.get, str(book_id))
        management = await run_in_threadpool(management_store.get, str(book_id))
        chapter = next(
            (item for item in record.result.chapters if item.id == str(chapter_id)), None
        ) if record and record.result else None
        item = next(
            (value for value in management["quotes"]
             if value["quote"].id == str(quote_id) and value["quote"].chapter_id == str(chapter_id)),
            None,
        )
        if chapter is None or item is None:
            raise HTTPException(404)
        if not item["usable"]:
            raise UploadError("Nur freigegebene, nutzbare Zitate können als Reel verarbeitet werden.", 409)
        source_key = quote_key(book.version_id, item["quote"])
        if create:
            draft = await run_in_threadpool(
                reel_store.get_or_create_draft,
                str(book_id), source_key, management["suggestion_id"],
                item["quote"].id, item["quote"].text,
            )
        else:
            draft = await run_in_threadpool(
                reel_store.find_draft, str(book_id), source_key, management["suggestion_id"]
            )
        return book, chapter, item["quote"], management, draft

    def reel_workshop_url(request: Request, book_id: UUID, chapter_id: UUID, quote_id: UUID) -> str:
        return str(request.url_for(
            "reel_workshop", book_id=book_id, chapter_id=chapter_id, quote_id=quote_id,
        ))

    async def workshop_response(request: Request, book_id: UUID, chapter_id: UUID, quote_id: UUID):
        book, chapter, quote, management, draft = await reel_context(
            book_id, chapter_id, quote_id, create=True,
        )
        tracks = await run_in_threadpool(reel_store.list_audio, str(book_id))
        characters = await run_in_threadpool(character_store.list, str(book_id))
        selected_character_ids = set(draft.character_ids)
        publication_defaults = await run_in_threadpool(
            reel_store.get_publication_defaults, settings.reel_storage_provider,
        )
        sync_receipt = await run_in_threadpool(sync_store.receipt, str(book_id))
        statuses = await run_in_threadpool(reel_jobs.status, draft.id)
        latest = statuses[0] if statuses else None
        if latest:
            kind = {"image": "Bild", "video": "Video", "upload": "Supabase-Upload", "prompt": "Text"}.get(
                latest["kind"], "Reel"
            )
            state = latest["state"]
            latest.update(
                active=state in {"queued", "running"},
                stage=f"{kind} {'wartet' if state == 'queued' else 'wird erzeugt' if state == 'running' else 'ist fehlgeschlagen' if state == 'failed' else 'ist fertig'}",
            )
        return templates.TemplateResponse(
            request=request,
            name="reel_workshop.html",
            context={
                "book": book, "chapter": chapter, "quote": quote, "management": management,
                "draft": draft, "tracks": tracks, "characters": characters,
                "has_selected_references": any(
                    character.id in selected_character_ids and character.has_reference
                    for character in characters
                ),
                "job": latest, "message": None,
                "text_ai_providers": configured_providers(settings),
                "default_duration": settings.reel_default_duration_seconds,
                "publication_defaults": publication_defaults,
                "can_queue": draft.state in {"ready", "stocked"} and bool(publication_defaults.values.selected())
                    and (settings.supabase_enabled or repository is not None) and sync_receipt is not None,
                "supabase_enabled": settings.supabase_enabled or repository is not None,
                "book_synced": sync_receipt is not None,
            },
        )

    def action_response(request: Request, book_id: UUID, chapter_id: UUID, quote_id: UUID):
        return JSONResponse({"workshop_url": reel_workshop_url(request, book_id, chapter_id, quote_id)})

    async def requested_text_provider(request: Request):
        try:
            async with request.form(max_files=0, max_fields=1) as form:
                if not form:
                    provider = "openwebui"
                elif set(form) == {"ai_provider"} and len(form.getlist("ai_provider")) == 1:
                    provider = str(form["ai_provider"])
                else:
                    raise ValueError()
            if provider not in {"openwebui", "comfyui_qwen"}:
                raise ValueError()
        except (TypeError, ValueError):
            raise UploadError("Bitte einen gültigen Text-KI-Provider auswählen.") from None
        if provider_missing(settings, provider):
            raise UploadError("Der ausgewählte Text-KI-Provider ist nicht eingerichtet.", 409)
        return provider

    async def teaser_context(book_id: UUID):
        book = await run_in_threadpool(local_store.get_book, book_id)
        if book is None:
            raise HTTPException(404)
        record = await run_in_threadpool(extraction_store.get, str(book_id))
        if record is None or record.result is None:
            raise UploadError("Bitte zuerst Text und Kapitel einlesen.", 409)
        management = await run_in_threadpool(management_store.get, str(book_id))
        drafts = await run_in_threadpool(reel_store.list_drafts, str(book_id))
        tracks = await run_in_threadpool(reel_store.list_audio, str(book_id))
        project = await run_in_threadpool(teaser_store.get, str(book_id))
        render_job = await run_in_threadpool(teaser_job_store.latest, str(book_id))
        characters = await run_in_threadpool(character_store.list, str(book_id))
        chapter_run = await run_in_threadpool(
            chapter_teaser_store.latest, str(book_id), include_plans=True,
        )
        publication_defaults = await run_in_threadpool(
            reel_store.get_publication_defaults, settings.reel_storage_provider,
        )
        sync_receipt = await run_in_threadpool(sync_store.receipt, str(book_id))
        chapter_queue_schema_ready = False
        book_teaser_queue_schema_ready = False
        if (settings.supabase_enabled or repository is not None) and sync_receipt is not None:
            try:
                await (repository or SupabaseRepository(settings)).check_schema(chapters=True)
                chapter_queue_schema_ready = True
            except DatabaseError:
                pass  # Keep the independent local image/video pipeline available.
            if project and project.state == "ready":
                try:
                    await (repository or SupabaseRepository(settings)).check_schema(book_teasers=True)
                    book_teaser_queue_schema_ready = True
                except DatabaseError:
                    pass
        quote_chapters = {
            item["quote"].id: item["quote"].chapter_id for item in management["quotes"]
        }
        generated_chapters = {
            item["draft_id"]: item["chapter_id"]
            for item in (chapter_run or {}).get("plans", []) if item.get("draft_id")
        }
        drafts_by_id = {draft.id: draft for draft in drafts}
        characters_by_id = {character.id: character for character in characters}
        chapter_media_active = False
        optimizable_count = 0
        for plan in (chapter_run or {}).get("plans", []):
            draft = drafts_by_id.get(plan.get("draft_id"))
            plan["draft"] = draft
            statuses = (
                await run_in_threadpool(reel_jobs.status, draft.id) if draft else []
            )
            image_job = next((item for item in statuses if item["kind"] == "image"), None)
            plan["image_job"] = image_job
            plan["effective_image_prompt"] = next(
                (item["effective_image_prompt"] for item in statuses
                 if item.get("effective_image_prompt")), "",
            )
            plan["media_active"] = any(
                item["state"] in {"queued", "running"} for item in statuses
            )
            plan["can_select_image"] = bool(
                chapter_run["state"] in {"queued", "running", "rendering", "done", "partial", "failed"}
                and plan["state"] in {"analyzed", "done", "failed"} and not plan["media_active"]
            )
            if plan["media_active"]:
                chapter_media_active = True
            selected_characters = [
                characters_by_id[value] for value in (draft.character_ids if draft else ())
                if value in characters_by_id
            ]
            plan["has_character_references"] = any(
                character.has_reference for character in selected_characters
            )
            plan["character_options"] = [
                {"id": character.id, "name": character.name,
                 "selected": bool(draft and character.id in draft.character_ids)}
                for character in characters if character.has_reference
            ]
            plan["image_sources"] = [
                {"id": source, "label": label, "selected": (
                    draft.selected_image_source == source and draft.selected_image_path == path
                )}
                for source, label, path in (draft.image_candidates if draft else ())
                if source in {"scene", "optimized"}
            ]
            plan["can_optimize"] = bool(
                draft and plan["can_select_image"] and draft.scene_image_path
                and not draft.image_stale and plan["has_character_references"]
                and settings.reel_reference_workflow.is_file()
            )
            plan["can_configure_characters"] = bool(
                draft and plan["can_select_image"] and draft.scene_image_path
                and not draft.image_stale and plan["character_options"]
                and settings.reel_reference_workflow.is_file()
            )
            plan["can_regenerate_image"] = bool(
                draft and plan["can_select_image"] and plan.get("suggestion")
                and draft.image_prompt and settings.reel_image_workflow.is_file()
            )
            plan["can_edit_prompt"] = bool(draft and plan["can_select_image"] and plan.get("suggestion"))
            plan["can_start_video"] = bool(
                draft and plan["can_select_image"] and draft.selected_image_path
                and not draft.image_stale and any(option["selected"] for option in plan["image_sources"])
                and settings.reel_video_workflow.is_file()
            )
            plan["can_retry_video"] = bool(
                plan["can_start_video"] and plan["state"] == "failed"
            )
            plan["can_update_video_text"] = bool(
                plan["can_start_video"] and plan["state"] != "done"
                and draft.selected_video_path and not draft.video_stale
            )
            plan["video_disabled_reason"] = ""
            if draft and not plan["can_start_video"]:
                if plan["media_active"]:
                    plan["video_disabled_reason"] = "Für dieses Kapitel läuft bereits ein Bild- oder Videojob."
                elif chapter_run["state"] == "stale":
                    plan["video_disabled_reason"] = "Der Buchstand wurde geändert. Bitte die Kapitelanalyse aktualisieren."
                elif draft.image_stale:
                    plan["video_disabled_reason"] = "Das Bild ist veraltet. Bitte zuerst das Bild neu erzeugen."
                elif not draft.selected_image_path or not any(option["selected"] for option in plan["image_sources"]):
                    plan["video_disabled_reason"] = "Bitte zuerst ein gültiges Kapitelbild auswählen."
                elif not settings.reel_video_workflow.is_file():
                    plan["video_disabled_reason"] = "Der ComfyUI-Video-Workflow ist nicht eingerichtet."
                else:
                    plan["video_disabled_reason"] = "Der Bildschritt dieses Kapitels ist noch nicht abgeschlossen."
            plan["image_actions_disabled_reason"] = ""
            if plan["media_active"]:
                plan["image_actions_disabled_reason"] = (
                    "Für dieses Kapitel ist bereits ein Bild- oder Videojob vorgemerkt oder läuft gerade."
                )
            elif chapter_run["state"] == "stale":
                plan["image_actions_disabled_reason"] = (
                    "Der Buchstand wurde geändert. Bitte die Kapitelanalyse aktualisieren."
                )
            elif plan["state"] in {"image_queued", "video_queued"}:
                plan["image_actions_disabled_reason"] = "Der Bildschritt dieses Kapitels ist noch nicht abgeschlossen."
            plan["can_bulk_optimize"] = bool(
                plan["can_optimize"] and not draft.optimized_image_path
            )
            if plan["can_bulk_optimize"]:
                optimizable_count += 1
        image_plans = [
            plan for plan in (chapter_run or {}).get("plans", [])
            if plan["state"] == "analyzed" or (plan["state"] == "failed" and plan["can_start_video"])
        ]
        chapter_video_configured = settings.reel_video_workflow.is_file()
        can_start_chapter_videos = bool(
            chapter_run and chapter_run["state"] in {"done", "partial"} and image_plans
            and not chapter_media_active and chapter_video_configured
            and all(
                plan["draft"] and plan["draft"].selected_image_path
                and not plan["draft"].image_stale
                and any(option["selected"] for option in plan["image_sources"])
                for plan in image_plans
            )
        )
        candidates: dict[str, list] = {chapter.id: [] for chapter in record.result.chapters}
        for draft in drafts:
            chapter_id = quote_chapters.get(draft.quote_id) or generated_chapters.get(draft.id)
            if (
                chapter_id in candidates and draft.selected_video_path
                and draft.selected_video_sha256 and not draft.video_stale
                and draft.state in {"ready", "stocked", "published"}
            ):
                candidates[chapter_id].append(draft)
        stored = {segment.chapter_id: segment for segment in project.segments} if project else {}
        rows = []
        for chapter in record.result.chapters:
            choices = candidates[chapter.id]
            segment = stored.get(chapter.id)
            selected = next(
                (draft for draft in choices if segment and draft.id == segment.draft_id),
                choices[0] if choices else None,
            )
            if segment is None:
                segment = TeaserSegment(
                    chapter_id=chapter.id,
                    draft_id=selected.id if selected else "",
                    video_sha256=selected.selected_video_sha256 if selected else "",
                    included=selected is not None,
                    position=chapter.position,
                    start_ms=0,
                    duration_ms=selected.duration_ms if selected else 0,
                )
            source_stale = bool(
                segment.draft_id and (
                    selected is None or selected.id != segment.draft_id
                    or selected.selected_video_sha256 != segment.video_sha256
                )
            )
            rows.append({
                "chapter": chapter,
                "candidates": choices,
                "segment": segment,
                "selected": selected,
                "source_stale": source_stale,
            })
        transition_ms = project.transition_ms if project else 500
        included = [row["segment"] for row in rows if row["segment"].included]
        estimated_duration_ms = max(
            0,
            sum(segment.duration_ms for segment in included)
            - transition_ms * max(0, len(included) - 1),
        )
        chapter_teaser_providers = configured_providers(settings)
        plans = (chapter_run or {}).get("plans", [])
        for plan in plans:
            plan["show_retry_analysis"] = bool(
                not plan.get("suggestion") and not plan["draft"]
                and plan["state"] in {"pending", "failed"}
            )
            plan["can_retry_analysis"] = bool(
                plan["show_retry_analysis"] and chapter_run["state"] in {"done", "partial", "failed"}
                and not chapter_media_active
                and not provider_missing(settings, chapter_run.get("provider", "openwebui"))
                and settings.reel_image_workflow.is_file()
            )
        complete_teaser_ready = bool(
            chapter_run and chapter_run["state"] in {"done", "partial"}
            and len(plans) == len(record.result.chapters)
            and {plan["chapter_id"] for plan in plans} == {chapter.id for chapter in record.result.chapters}
            and plans and all(
                plan["state"] == "done" and plan["draft"] and not plan["media_active"]
                and not plan["draft"].image_stale and not plan["draft"].video_stale
                and plan["draft"].selected_video_path and plan["draft"].selected_video_sha256
                and plan["draft"].state in {"ready", "stocked", "published"}
                for plan in plans
            )
        )
        final_platforms = book_teaser_platforms(
            publication_defaults.values, project.aspect if project else "vertical",
        )
        return {
            "active_page": "books", "book": book, "record": record,
            "management": management, "tracks": tracks, "project": project,
            "render_job": render_job,
            "rows": rows, "estimated_duration_ms": estimated_duration_ms,
            "chapter_run": chapter_run,
            "chapter_production_active": bool(
                (chapter_run or {}).get("active") or chapter_media_active
            ),
            "chapter_media_active": chapter_media_active,
            "optimizable_count": optimizable_count,
            "can_start_chapter_videos": can_start_chapter_videos,
            "complete_teaser_ready": complete_teaser_ready,
            "book_teaser_queue_platforms": final_platforms,
            "book_teaser_queue_configured": bool(
                project and project.state == "ready" and project.output_path and final_platforms
                and book_teaser_queue_schema_ready and sync_receipt is not None
                and not (render_job and render_job["active"])
                and not any(row["source_stale"] for row in rows if row["segment"].included)
            ),
            "chapter_video_configured": chapter_video_configured,
            "reference_workflow_configured": settings.reel_reference_workflow.is_file(),
            "chapter_teaser_providers": chapter_teaser_providers,
            "publication_defaults": publication_defaults,
            "supabase_enabled": settings.supabase_enabled or repository is not None,
            "book_synced": sync_receipt is not None,
            "chapter_queue_schema_ready": chapter_queue_schema_ready,
            "chapter_queue_configured": bool(publication_defaults.values.selected())
                and (settings.supabase_enabled or repository is not None)
                and sync_receipt is not None and chapter_queue_schema_ready,
            "chapter_teaser_configured": bool(chapter_teaser_providers)
                and settings.reel_image_workflow.is_file(),
        }

    def teaser_segments_from_form(form, rows, transition_ms: int) -> list[TeaserSegment]:
        segments = []
        for row in rows:
            chapter = row["chapter"]
            key = chapter.id
            choices = {draft.id: draft for draft in row["candidates"]}
            draft_id = str(form.get(f"draft_{key}", "")).strip()
            included = f"include_{key}" in form
            draft = choices.get(draft_id) if draft_id else None
            try:
                start_ms = round(float(str(form.get(f"start_{key}", "0"))) * 1000)
                duration_ms = round(float(str(form.get(f"duration_{key}", "0"))) * 1000)
                focus_x = int(str(form.get(f"focus_x_{key}", "50")))
                focus_y = int(str(form.get(f"focus_y_{key}", "50")))
            except (TypeError, ValueError):
                raise UploadError(f"Bitte die Schnittwerte für Kapitel {chapter.position} prüfen.") from None
            if included and draft is None:
                raise UploadError(f"Für Kapitel {chapter.position} ist kein fertiges Reel ausgewählt.")
            if draft and (
                start_ms < 0 or duration_ms < 500
                or start_ms + duration_ms > draft.duration_ms + 50
            ):
                raise UploadError(
                    f"Der Ausschnitt für Kapitel {chapter.position} liegt außerhalb des Reels."
                )
            if not 0 <= focus_x <= 100 or not 0 <= focus_y <= 100:
                raise UploadError(f"Der Fokus für Kapitel {chapter.position} muss zwischen 0 und 100 liegen.")
            if included and duration_ms <= transition_ms:
                raise UploadError(
                    f"Kapitel {chapter.position} muss länger als die gewählte Überblendung sein."
                )
            segments.append(TeaserSegment(
                chapter_id=chapter.id,
                draft_id=draft.id if draft else "",
                video_sha256=draft.selected_video_sha256 if draft else "",
                included=included,
                position=chapter.position,
                start_ms=start_ms if draft else 0,
                duration_ms=duration_ms if draft else 0,
                focus_x=focus_x,
                focus_y=focus_y,
            ))
        if not any(segment.included for segment in segments):
            raise UploadError("Bitte mindestens ein fertiges Kapitel-Reel auswählen.")
        return segments

    async def save_teaser_request(request: Request, book_id: UUID):
        context = await teaser_context(book_id)
        try:
            async with request.form(max_files=0, max_fields=4000) as form:
                revision = int(str(form.get("revision", "0")))
                transition_ms = round(float(str(form.get("transition_seconds", ".5"))) * 1000)
                aspect = str(form.get("aspect", ""))
                audio_track_id = str(form.get("audio_track_id", ""))
                segments = teaser_segments_from_form(form, context["rows"], transition_ms)
        except (TypeError, ValueError):
            raise UploadError("Bitte Format, Song und Überblendung vollständig auswählen.") from None
        saved = await run_in_threadpool(
            teaser_store.save, str(book_id), revision,
            aspect=aspect, audio_track_id=audio_track_id,
            transition_ms=transition_ms, segments=segments,
        )
        return context, saved

    async def book_settings_context(book, management, details, *, saved=False, form_error=None,
                                    profile_run_id="", asset_saved="", promotion_pending=False):
        assets = await run_in_threadpool(asset_store.list, str(book.id))
        characters = await run_in_threadpool(character_store.list, str(book.id))
        missing = (
            await run_in_threadpool(asset_store.configuration_errors, str(book.id), details)
            if isinstance(details, BookDetails) else ["ungespeicherte Einstellungen"]
        )
        carousel_preview = not missing
        return {"active_page": "books", "book": book, "management": management,
                "details": details, "saved": saved, "form_error": form_error,
                "profile_run_id": profile_run_id, "font_names": font_names(),
                "overlay_preview": bool(getattr(details, "overlay_title_font", "")) and overlay_store.preview_exists(book.id),
                "assets": assets, "asset_saved": asset_saved,
                "characters": characters,
                "text_ai_providers": configured_providers(settings),
                "carousel_preview": carousel_preview,
                "promotion_missing": missing if promotion_pending else []}

    @app.post("/uploads", name="upload_book")
    async def upload_book(request: Request):
        require_local_origin(request)
        if not request.headers.get("content-type", "").startswith("multipart/form-data"):
            raise UploadError("Bitte genau eine Word-Datei auswählen.", 415)
        try:
            async with request.form(max_files=1, max_fields=0) as form:
                values = form.getlist("file")
                if len(values) != 1 or len(form) != 1 or not isinstance(values[0], UploadFile):
                    raise UploadError("Bitte genau eine Word-Datei auswählen.")
                book, duplicate = await run_in_threadpool(local_store.save, values[0].file, values[0].filename)
            await run_in_threadpool(job_store.enqueue, book)
        except (OSError, sqlite3.Error):
            raise UploadError("Die Datei konnte nicht gespeichert werden. Bitte freien Speicher und Zugriffsrechte prüfen.", 503) from None
        destination = str(request.url_for("local_book", book_id=book.id))
        if "application/json" not in request.headers.get("accept", ""):
            return RedirectResponse(destination, status_code=303)
        return JSONResponse({"book_id": book.id, "url": destination, "duplicate": duplicate,
            "message": "Diese Datei ist bereits vorhanden. Das bestehende Buch wird geöffnet." if duplicate
                       else "Datei gespeichert. Der Import wird im Hintergrund verarbeitet."}, status_code=200 if duplicate else 201)

    @app.get("/books/local/{book_id}", response_class=HTMLResponse, name="local_book")
    async def local_book(request: Request, book_id: UUID):
        try:
            book = await run_in_threadpool(local_store.get_book, book_id)
            record = await run_in_threadpool(extraction_store.get, str(book_id))
            jobs = await run_in_threadpool(job_store.statuses, [str(book_id)])
            analysis = await run_in_threadpool(analysis_store.latest, str(book_id), include_result=True)
            profile_analysis = await run_in_threadpool(analysis_store.latest, str(book_id), purpose="profile")
            character_analysis = await run_in_threadpool(analysis_store.latest, str(book_id), purpose="characters")
            chapter_teasers = await run_in_threadpool(chapter_teaser_store.latest, str(book_id))
            teaser_export = await run_in_threadpool(teaser_job_store.latest, str(book_id))
            management = await run_in_threadpool(management_store.get, str(book_id)) if book else None
            sync_receipt = await run_in_threadpool(sync_store.receipt, str(book_id))
        except (OSError, sqlite3.Error):
            raise UploadError("Das lokale Buchprojekt konnte nicht gelesen werden.", 503) from None
        if book is None:
            raise HTTPException(404)
        return templates.TemplateResponse(request=request, name="local_book.html",
            context={"active_page": "books", "book": book, "record": record, "job": jobs.get(str(book_id)),
                     "analysis": analysis,
                     "text_ai_providers": configured_providers(settings),
                     "analysis_configured": bool(configured_providers(settings)),
                     "management": management,
                     "sync_receipt": sync_receipt, "sync_enabled": settings.supabase_enabled,
                     "delete_available": True,
                     "delete_blocked": bool(
                         (jobs.get(str(book_id)) or {}).get("active")
                         or (analysis or {}).get("active")
                         or (profile_analysis or {}).get("active")
                         or (character_analysis or {}).get("active")
                         or (chapter_teasers or {}).get("active")
                         or (teaser_export or {}).get("active")
                     ),
                     "chapter_blocked_counts": {c.id: sum(q["blocked"] and q["quote"].chapter_id == c.id for q in management["quotes"])
                         for c in record.result.chapters} if management and record and record.result else {},
                     "chapter_quote_counts": {c.id: sum(q["usable"] and q["quote"].chapter_id == c.id for q in management["quotes"])
                         for c in record.result.chapters} if analysis and analysis["result"] and record and record.result else {},
                     "result": record.result if record else None})

    @app.get(
        "/books/local/{book_id}/teaser", response_class=HTMLResponse, name="book_teaser",
    )
    async def book_teaser(
        request: Request, book_id: UUID, saved: bool = False, rendered: bool = False,
        queued: int = Query(default=0, ge=0, le=100000), teaser_queued: bool = False,
    ):
        try:
            context = await teaser_context(book_id)
        except (OSError, sqlite3.Error):
            raise UploadError("Der lokale Book-Teaser konnte nicht gelesen werden.", 503) from None
        context.update({"saved": saved, "rendered": rendered, "queued": queued,
                        "teaser_queued": teaser_queued})
        return templates.TemplateResponse(
            request=request, name="book_teaser.html", context=context,
        )

    @app.post("/books/local/{book_id}/teaser/audio/upload", name="upload_teaser_audio")
    async def upload_teaser_audio(request: Request, book_id: UUID):
        require_local_origin(request)
        if await run_in_threadpool(local_store.get_book, book_id) is None:
            raise HTTPException(404)
        if not request.headers.get("content-type", "").startswith("multipart/form-data"):
            raise UploadError("Bitte genau eine WAV-Datei auswählen.", 415)
        async with request.form(max_files=1, max_fields=1) as form:
            file = form.get("file")
            title = form.get("title")
            if (
                set(form) != {"file", "title"} or not isinstance(file, UploadFile)
                or not isinstance(title, str) or len(title.strip()) > 200
            ):
                raise UploadError("Bitte eine WAV-Datei und optional einen gültigen Songtitel angeben.")
            await run_in_threadpool(
                reel_store.save_audio, str(book_id), file.file, file.filename,
                title=title.strip() or None,
            )
        return RedirectResponse(request.url_for("book_teaser", book_id=book_id), status_code=303)

    @app.post(
        "/books/local/{book_id}/teaser/chapters/start", name="start_chapter_teasers",
    )
    async def start_chapter_teasers(request: Request, book_id: UUID):
        require_local_origin(request)
        if not settings.reel_image_workflow.is_file():
            raise UploadError("Bitte zuerst den ComfyUI-Szenenbild-Workflow einrichten.", 409)
        context = await teaser_context(book_id)
        try:
            async with request.form(max_files=0, max_fields=3) as form:
                if set(form) != {"audio_track_id", "transition_seconds", "ai_provider"}:
                    raise ValueError()
                audio_track_id = str(UUID(str(form["audio_track_id"])))
                transition_ms = round(float(str(form["transition_seconds"])) * 1000)
                provider = str(form["ai_provider"])
                if provider not in {"openwebui", "comfyui_qwen"}:
                    raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise UploadError(
                "Bitte Text-KI, Song und Überblendung für die Kapitel-Reels auswählen."
            ) from None
        if provider_missing(settings, provider):
            raise UploadError("Der ausgewählte Text-KI-Provider ist nicht eingerichtet.", 409)
        details = context["management"]["details"]
        await run_in_threadpool(
            chapter_teaser_store.enqueue, str(book_id), settings,
            audio_track_id=audio_track_id, transition_ms=transition_ms,
            provider=provider,
            book_context={
                "title": details.title, "author": details.author,
                "target_url": details.target_url, "genre": details.genre,
                "mood": details.mood, "internal_summary": details.internal_summary,
                "world": details.world, "characters": details.characters,
                "spoilers": details.spoilers,
                "image_prompt_base": details.image_prompt_base,
                "caption_guidelines": details.caption_guidelines,
            },
        )
        return RedirectResponse(request.url_for("book_teaser", book_id=book_id), status_code=303)

    @app.get(
        "/books/local/{book_id}/teaser/chapters/status", name="chapter_teaser_status",
    )
    async def chapter_teaser_status(book_id: UUID):
        context = await teaser_context(book_id)
        run = context["chapter_run"]
        if run is None:
            return JSONResponse({
                "state": "not_started", "active": False,
                "label": "Noch keine automatische Kapitel-Reel-Produktion",
                "stage": "", "completed": 0, "total": 0, "calls_started": 0,
                "error": None,
            })
        payload = {key: run.get(key) for key in (
            "state", "active", "label", "stage", "completed", "total",
            "calls_started", "error",
        )}
        payload["active"] = context["chapter_production_active"]
        if context["chapter_media_active"] and not run.get("active"):
            payload["stage"] = "Charakterbilder werden optimiert"
        return JSONResponse(payload)

    @app.get(
        "/books/local/{book_id}/teaser/chapters/{chapter_id}/image.bin",
        name="chapter_teaser_image",
    )
    async def chapter_teaser_image(
        book_id: UUID, chapter_id: UUID, source: Literal["scene", "optimized"] = "scene",
    ):
        context = await teaser_context(book_id)
        plan = next(
            (item for item in (context["chapter_run"] or {}).get("plans", [])
             if item["chapter_id"] == str(chapter_id)), None,
        )
        draft = plan.get("draft") if plan else None
        if draft is None:
            raise HTTPException(404)
        path = await run_in_threadpool(reel_store.candidate_image_path, draft, source)
        if path is None:
            raise HTTPException(404)
        return FileResponse(path)

    @app.post(
        "/books/local/{book_id}/teaser/chapters/{chapter_id}/analysis/retry",
        name="retry_chapter_teaser_analysis",
    )
    async def retry_chapter_teaser_analysis(request: Request, book_id: UUID, chapter_id: UUID):
        require_local_origin(request)
        context = await teaser_context(book_id)
        plan = next((item for item in (context["chapter_run"] or {}).get("plans", [])
                     if item["chapter_id"] == str(chapter_id)), None)
        if plan is None or not plan.get("can_retry_analysis"):
            raise UploadError("Die Kapitelanalyse kann jetzt nicht erneut gestartet werden.", 409)
        async with request.form(max_files=0, max_fields=1) as form:
            if (set(form) != {"run_id"} or len(form.getlist("run_id")) != 1
                    or form["run_id"] != context["chapter_run"]["id"]):
                raise UploadError("Der Kapitel-Lauf wurde zwischenzeitlich geändert. Bitte neu laden.", 409)
        await run_in_threadpool(
            chapter_teaser_store.retry_analysis, str(book_id), context["chapter_run"]["id"], str(chapter_id),
        )
        return RedirectResponse(request.url_for("book_teaser", book_id=book_id), status_code=303)

    @app.post(
        "/books/local/{book_id}/teaser/chapters/{chapter_id}/prompt",
        name="save_chapter_teaser_prompt",
    )
    async def save_chapter_teaser_prompt(request: Request, book_id: UUID, chapter_id: UUID):
        require_local_origin(request)
        context = await teaser_context(book_id)
        plan = next((item for item in (context["chapter_run"] or {}).get("plans", [])
                     if item["chapter_id"] == str(chapter_id)), None)
        if plan is None or not plan.get("can_edit_prompt"):
            raise UploadError("Der Kapitelprompt kann jetzt nicht bearbeitet werden.", 409)
        try:
            async with request.form(max_files=0, max_fields=4) as form:
                if (set(form) - {"revision", "image_prompt", "teaser_text", "video_prompt"}
                        or not {"revision", "image_prompt", "teaser_text"} <= set(form)):
                    raise ValueError()
                revision = int(str(form["revision"]))
                image_prompt, teaser_text = str(form["image_prompt"]), str(form["teaser_text"])
                video_prompt = str(form["video_prompt"]) if "video_prompt" in form else None
        except (KeyError, TypeError, ValueError):
            raise UploadError("Bitte einen gültigen Bildprompt und Teasertext eingeben.") from None
        await run_in_threadpool(
            chapter_teaser_store.save_prompt, str(book_id), context["chapter_run"]["id"],
            plan["draft"].id, revision, image_prompt=image_prompt, teaser_text=teaser_text,
            video_prompt=video_prompt,
        )
        return RedirectResponse(request.url_for("book_teaser", book_id=book_id), status_code=303)

    @app.post(
        "/books/local/{book_id}/teaser/chapters/{chapter_id}/video/start",
        name="start_chapter_teaser_video",
    )
    async def start_chapter_teaser_video(request: Request, book_id: UUID, chapter_id: UUID):
        require_local_origin(request)
        context = await teaser_context(book_id)
        plan = next((item for item in (context["chapter_run"] or {}).get("plans", [])
                     if item["chapter_id"] == str(chapter_id)), None)
        if plan is None or not plan.get("can_start_video"):
            raise UploadError("Bitte zuerst ein aktuelles Kapitelbild erzeugen und auswählen.", 409)
        try:
            async with request.form(max_files=0, max_fields=2) as form:
                if (set(form) - {"revision", "regenerate"}
                        or any(len(form.getlist(key)) != 1 for key in form)
                        or ("regenerate" in form and (form["regenerate"] != "1" or "revision" not in form))):
                    raise ValueError()
                revision = int(str(form["revision"])) if "revision" in form else None
                regenerate = "regenerate" in form
        except (KeyError, TypeError, ValueError):
            raise UploadError("Bitte die Kapitel-Seite neu laden.", 409) from None
        await run_in_threadpool(
            chapter_teaser_store.start_video, str(book_id), context["chapter_run"]["id"],
            plan["draft"].id, revision=revision, regenerate=regenerate,
        )
        return RedirectResponse(request.url_for("book_teaser", book_id=book_id), status_code=303)

    @app.post(
        "/books/local/{book_id}/teaser/chapters/{chapter_id}/image/regenerate",
        name="regenerate_chapter_teaser_image",
    )
    async def regenerate_chapter_teaser_image(
        request: Request, book_id: UUID, chapter_id: UUID,
    ):
        require_local_origin(request)
        context = await teaser_context(book_id)
        plan = next(
            (item for item in (context["chapter_run"] or {}).get("plans", [])
             if item["chapter_id"] == str(chapter_id)), None,
        )
        if plan is None or not plan.get("can_regenerate_image"):
            raise UploadError("Das Kapitelbild kann jetzt nicht neu erzeugt werden.", 409)
        await run_in_threadpool(
            chapter_teaser_store.reopen_image_review, str(book_id),
            context["chapter_run"]["id"], [plan["draft"].id], regenerate=True,
        )
        return RedirectResponse(request.url_for("book_teaser", book_id=book_id), status_code=303)

    @app.post(
        "/books/local/{book_id}/teaser/chapters/{chapter_id}/video/retry",
        name="retry_chapter_teaser_video",
    )
    async def retry_chapter_teaser_video(
        request: Request, book_id: UUID, chapter_id: UUID,
    ):
        require_local_origin(request)
        context = await teaser_context(book_id)
        plan = next(
            (item for item in (context["chapter_run"] or {}).get("plans", [])
             if item["chapter_id"] == str(chapter_id)), None,
        )
        if plan is None or not plan.get("can_retry_video"):
            raise UploadError("Bitte zuerst ein aktuelles Kapitelbild erzeugen und auswählen.", 409)
        await run_in_threadpool(
            chapter_teaser_store.retry_video, str(book_id),
            context["chapter_run"]["id"], plan["draft"].id,
        )
        return RedirectResponse(request.url_for("book_teaser", book_id=book_id), status_code=303)

    @app.post(
        "/books/local/{book_id}/teaser/chapters/{chapter_id}/image/optimize",
        name="optimize_chapter_teaser_image",
    )
    async def optimize_chapter_teaser_image(
        request: Request, book_id: UUID, chapter_id: UUID,
    ):
        require_local_origin(request)
        context = await teaser_context(book_id)
        plan = next(
            (item for item in (context["chapter_run"] or {}).get("plans", [])
             if item["chapter_id"] == str(chapter_id)), None,
        )
        if plan is None or not plan.get("can_configure_characters"):
            raise UploadError(
                "Für dieses Kapitel sind Szenenbild und Charakterreferenzen nicht bereit.", 409,
            )
        revision, selected = None, None
        try:
            async with request.form(max_files=0, max_fields=6) as form:
                if form:
                    if set(form) - {"revision", "character_id"} or "revision" not in form:
                        raise ValueError()
                    revision = int(str(form["revision"]))
                    selected = [str(UUID(str(value))) for value in form.getlist("character_id")]
                    if not 1 <= len(selected) <= 4 or len(selected) != len(set(selected)):
                        raise ValueError()
                elif not plan.get("can_optimize"):
                    raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise UploadError("Bitte ein bis vier Charakterreferenzen für dieses Bild auswählen.") from None
        await run_in_threadpool(
            chapter_teaser_store.reopen_image_review, str(book_id),
            context["chapter_run"]["id"], [plan["draft"].id], optimize=True,
            character_ids=selected, revision=revision,
        )
        return RedirectResponse(request.url_for("book_teaser", book_id=book_id), status_code=303)

    @app.post(
        "/books/local/{book_id}/teaser/chapters/images/optimize",
        name="optimize_all_chapter_teaser_images",
    )
    async def optimize_all_chapter_teaser_images(request: Request, book_id: UUID):
        require_local_origin(request)
        context = await teaser_context(book_id)
        plans = [
            plan for plan in (context["chapter_run"] or {}).get("plans", [])
            if plan.get("can_bulk_optimize")
        ]
        if not plans:
            raise UploadError(
                "Es gibt derzeit keine Kapitelbilder mit nutzbaren Charakterreferenzen.", 409,
            )
        await run_in_threadpool(
            chapter_teaser_store.reopen_image_review, str(book_id),
            context["chapter_run"]["id"], [plan["draft"].id for plan in plans], optimize=True,
        )
        return RedirectResponse(request.url_for("book_teaser", book_id=book_id), status_code=303)

    @app.post(
        "/books/local/{book_id}/teaser/chapters/{chapter_id}/image/select",
        name="select_chapter_teaser_image",
    )
    async def select_chapter_teaser_image(
        request: Request, book_id: UUID, chapter_id: UUID,
    ):
        require_local_origin(request)
        context = await teaser_context(book_id)
        plan = next(
            (item for item in (context["chapter_run"] or {}).get("plans", [])
             if item["chapter_id"] == str(chapter_id)
             and item["state"] in {"analyzed", "done", "failed"}),
            None,
        )
        if plan is None or plan.get("draft") is None:
            raise UploadError("Das Kapitelbild kann jetzt nicht mehr geändert werden.", 409)
        try:
            async with request.form(max_files=0, max_fields=2) as form:
                if set(form) != {"revision", "source"}:
                    raise ValueError()
                revision = int(str(form["revision"]))
                source = str(form["source"])
                if source not in {"scene", "optimized"}:
                    raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise UploadError("Bitte eine gültige Kapitelbild-Variante auswählen.") from None
        await run_in_threadpool(
            chapter_teaser_store.select_image, str(book_id), context["chapter_run"]["id"],
            plan["draft"].id, revision, source,
        )
        return RedirectResponse(request.url_for("book_teaser", book_id=book_id), status_code=303)

    @app.post(
        "/books/local/{book_id}/teaser/chapters/videos/start",
        name="start_chapter_teaser_videos",
    )
    async def start_chapter_teaser_videos(request: Request, book_id: UUID):
        require_local_origin(request)
        context = await teaser_context(book_id)
        run = context["chapter_run"]
        if run is None or not context["can_start_chapter_videos"]:
            raise UploadError(
                "Bitte zuerst alle verfügbaren Kapitelbilder prüfen und auswählen.", 409,
            )
        plans = [plan for plan in run["plans"] if plan["state"] == "analyzed" or (
            plan["state"] == "failed" and plan["can_start_video"]
        )]
        await run_in_threadpool(
            chapter_teaser_store.begin_videos, str(book_id), run["id"],
            [plan["draft"].id for plan in plans],
        )
        return RedirectResponse(request.url_for("book_teaser", book_id=book_id), status_code=303)

    @app.get(
        "/books/local/{book_id}/teaser/chapters/{chapter_id}/video.mp4",
        name="chapter_teaser_video",
    )
    async def chapter_teaser_video(
        book_id: UUID, chapter_id: UUID,
        variant: Literal["clean", "text"] = "clean", download: bool = False,
    ):
        if await run_in_threadpool(local_store.get_book, book_id) is None:
            raise HTTPException(404)
        run = await run_in_threadpool(
            chapter_teaser_store.latest, str(book_id), include_plans=True,
        )
        plan = next(
            (item for item in (run or {}).get("plans", [])
             if item["chapter_id"] == str(chapter_id) and item["state"] == "done"),
            None,
        )
        if plan is None or not plan.get("draft_id"):
            raise HTTPException(404)
        draft = await run_in_threadpool(reel_store.get_draft, plan["draft_id"])
        if draft is None or draft.book_id != str(book_id) or draft.video_stale:
            raise HTTPException(404)
        path = (
            await run_in_threadpool(
                chapter_teaser_store.text_video_path, plan,
                book_id=str(book_id), draft_id=draft.id,
            )
            if variant == "text"
            else await run_in_threadpool(reel_store.artifact_path, draft, "video")
        )
        if path is None:
            raise HTTPException(404)
        suffix = "-mit-text" if variant == "text" else "-ohne-text"
        filename = f"kapitel-{plan['position']:02d}-reel{suffix}.mp4" if download else None
        return FileResponse(path, media_type="video/mp4", filename=filename)

    @app.post(
        "/books/local/{book_id}/teaser/chapters/{chapter_id}/queue",
        name="queue_chapter_teaser_route",
    )
    async def queue_chapter_teaser_route(
        request: Request, book_id: UUID, chapter_id: UUID,
    ):
        require_local_origin(request)
        context = await teaser_context(book_id)
        chapter = next(
            (item for item in context["record"].result.chapters if item.id == str(chapter_id)),
            None,
        )
        plan = next(
            (item for item in (context["chapter_run"] or {}).get("plans", [])
             if item["chapter_id"] == str(chapter_id) and item["state"] == "done"),
            None,
        )
        draft = plan.get("draft") if plan else None
        suggestion = plan.get("suggestion") if plan else None
        if chapter is None or plan is None or draft is None or suggestion is None:
            raise HTTPException(404)
        if draft.state not in {"ready", "stocked"} or draft.video_stale:
            raise UploadError("Das Kapitel-Reel ist nicht mehr veröffentlichungsbereit.", 409)
        if not context["book_synced"]:
            raise UploadError(
                "Bitte das aktuelle Buch zuerst auf der Buchseite nach Supabase übertragen.", 409,
            )

        form_values: dict[str, object] = {}
        try:
            async with request.form(max_files=0, max_fields=11) as form:
                allowed = {
                    "variant", "title", "description", "queue_mode", "scheduled_for",
                    "platform", "requeue",
                }
                if set(form) - allowed:
                    raise ValueError("Das Kapitel-Reel-Formular enthält unbekannte Felder.")
                for name in allowed - {"platform"}:
                    values = form.getlist(name)
                    if len(values) != 1 or not isinstance(values[0], str):
                        raise ValueError("Bitte das vollständige Kapitel-Reel-Formular verwenden.")
                    form_values[name] = values[0]
                platforms = form.getlist("platform")
                if not platforms or len(platforms) != len(set(platforms)) or any(
                    not isinstance(value, str) or value not in PLATFORMS for value in platforms
                ):
                    raise ValueError("Mindestens eine gültige Plattform auswählen.")
                form_values["platforms"] = platforms

            variant = str(form_values["variant"])
            if variant not in {"clean", "text"}:
                raise ValueError("Bitte eine gültige Video-Variante auswählen.")
            title = str(form_values["title"]).strip()
            description = str(form_values["description"]).strip()
            if str(form_values["requeue"]) not in {"0", "1"}:
                raise ValueError("Der Übertragungsmodus ist ungültig.")
            requeue = str(form_values["requeue"]) == "1"
            if not 1 <= len(title) <= 300 or not 1 <= len(description) <= 5000:
                raise ValueError("Titel oder Beschreibung haben eine ungültige Länge.")
            queue_mode = str(form_values["queue_mode"])
            if queue_mode not in {"daily", "scheduled"}:
                raise ValueError("Bitte tägliche Warteschlange oder festen Termin auswählen.")
            scheduled_raw = str(form_values["scheduled_for"]).strip()
            if queue_mode == "daily":
                if scheduled_raw:
                    raise ValueError("Die tägliche Warteschlange hat keinen festen Termin.")
                scheduled_for = None
            else:
                scheduled = datetime.fromisoformat(scheduled_raw)
                if scheduled.tzinfo is None:
                    scheduled = scheduled.replace(tzinfo=ZoneInfo("Europe/Berlin"))
                if scheduled <= datetime.now(ZoneInfo("Europe/Berlin")):
                    raise ValueError("Der Veröffentlichungszeitpunkt muss in der Zukunft liegen.")
                scheduled_for = scheduled.isoformat()
        except ValueError as exc:
            raise UploadError(str(exc), 400) from None

        if variant == "text":
            path = await run_in_threadpool(
                chapter_teaser_store.text_video_path, plan,
                book_id=str(book_id), draft_id=draft.id,
            )
            media_sha256 = plan.get("text_video_sha256")
        else:
            path = await run_in_threadpool(reel_store.artifact_path, draft, "video")
            media_sha256 = draft.selected_video_sha256
        if path is None or not media_sha256:
            raise UploadError("Die gewählte Kapitel-Reel-Variante fehlt.", 409)
        data = await run_in_threadpool(path.read_bytes)
        if len(data) > settings.reel_max_video_mb * 1024 * 1024:
            raise UploadError("Das fertige Reel ist größer als das eingestellte Upload-Limit.", 413)

        db = repository or SupabaseRepository(settings)
        await db.check_schema(chapters=True)
        defaults = await run_in_threadpool(
            reel_store.get_publication_defaults, settings.reel_storage_provider,
        )
        selected = tuple(form_values["platforms"])
        if any(platform not in defaults.values.selected() for platform in selected):
            raise UploadError(
                "Die Plattformeinstellungen wurden geändert. Bitte die Teaser-Seite neu laden.", 409,
            )
        asset_id = _reel_delivery_asset_id(draft, requeue=requeue)
        object_path = f"{asset_id}/{media_sha256}.mp4"
        if defaults.values.storage_provider == "cloudflare_r2":
            try:
                r2 = R2Client(settings)
                await r2.upload_reel(object_path, data, media_sha256)
            except R2Error as exc:
                raise UploadError(str(exc), 503) from None
            storage_bucket = settings.r2_bucket
            public_url = r2.public_url(object_path)
        else:
            await db.upload_reel(object_path, data)
            storage_bucket = "book-promotion-reels"
            public_url = None

        track = next(
            (value for value in context["tracks"] if value.id == draft.audio_track_id), None,
        )
        details = context["management"]["details"]
        asset = {
            "id": asset_id,
            "source_kind": "chapter",
            "quote_id": None,
            "chapter_id": chapter.id,
            "book_id": str(context["book"].id),
            "quote_text": suggestion.source_excerpt,
            "addition": suggestion.teaser_text,
            "title": title,
            "description": description,
            "book_profile": details.model_dump(mode="json"),
            "image_prompt": draft.image_prompt,
            "video_prompt": draft.video_prompt,
            "storage_provider": defaults.values.storage_provider,
            "storage_bucket": storage_bucket,
            "storage_path": object_path,
            "public_url": public_url,
            "media_sha256": media_sha256,
            "size_bytes": len(data),
            "duration_ms": draft.duration_ms,
            "width": 512,
            "height": 896,
            "audio_title": track.title if track else None,
            "audio_start_ms": draft.audio_start_ms,
        }
        publications = [
            {
                "platform": platform,
                "account_id": defaults.values.platform(platform).account_id,
                "queue_mode": queue_mode,
                "scheduled_for": scheduled_for,
                "priority": 0,
                "title": title,
                "description": description,
                "options": defaults.values.platform(platform).options,
            }
            for platform in selected
        ]
        await db.enqueue_reel(asset, publications)
        try:
            await run_in_threadpool(
                reel_store.mark_stocked, draft.id, draft.revision, object_path,
            )
        except UploadError as exc:
            current = await run_in_threadpool(reel_store.get_draft, draft.id)
            if not (
                exc.status == 409 and current and current.state == "stocked"
                and current.remote_video_path == object_path
            ):
                raise
        destination = str(request.url_for("book_teaser", book_id=book_id))
        return RedirectResponse(f"{destination}?queued={chapter.position}", status_code=303)

    @app.post("/books/local/{book_id}/teaser/save", name="save_book_teaser")
    async def save_book_teaser(request: Request, book_id: UUID):
        require_local_origin(request)
        try:
            await save_teaser_request(request, book_id)
        except (OSError, sqlite3.Error):
            raise UploadError("Der Book-Teaser-Schnitt konnte nicht gespeichert werden.", 503) from None
        destination = str(request.url_for("book_teaser", book_id=book_id)) + "?saved=true"
        return RedirectResponse(destination, status_code=303)

    @app.post("/books/local/{book_id}/teaser/render", name="render_book_teaser_route")
    async def render_book_teaser_route(request: Request, book_id: UUID):
        require_local_origin(request)
        try:
            _, project = await save_teaser_request(request, book_id)
            await run_in_threadpool(teaser_job_store.enqueue, project)
        except (OSError, sqlite3.Error):
            raise UploadError("Der Book-Teaser-Export konnte nicht vorgemerkt werden.", 503) from None
        destination = str(request.url_for("book_teaser", book_id=book_id))
        return RedirectResponse(destination, status_code=303)

    @app.post("/books/local/{book_id}/teaser/render/complete", name="render_complete_book_teaser")
    async def render_complete_book_teaser(request: Request, book_id: UUID):
        require_local_origin(request)
        context = await teaser_context(book_id)
        plans = (context["chapter_run"] or {}).get("plans", [])
        segments = await run_in_threadpool(
            complete_chapter_segments, str(book_id), context["record"].result.chapters,
            plans, reel_store,
        )
        try:
            async with request.form(max_files=0, max_fields=4) as form:
                required = {"revision", "audio_track_id", "aspect", "transition_seconds"}
                if set(form) != required or any(
                    len(form.getlist(key)) != 1 or not isinstance(form[key], str)
                    for key in required
                ):
                    raise ValueError()
                revision = int(form["revision"])
                transition_ms = round(float(form["transition_seconds"]) * 1000)
                aspect, audio_track_id = form["aspect"], form["audio_track_id"]
        except (TypeError, ValueError, OverflowError):
            raise UploadError("Bitte Format, Song und Überblendung vollständig auswählen.") from None
        project = await run_in_threadpool(
            teaser_store.save, str(book_id), revision, aspect=aspect,
            audio_track_id=audio_track_id, transition_ms=transition_ms, segments=segments,
        )
        await run_in_threadpool(teaser_job_store.enqueue, project)
        return RedirectResponse(
            str(request.url_for("book_teaser", book_id=book_id)) + "?rendered=true",
            status_code=303,
        )

    @app.post("/books/local/{book_id}/teaser/queue", name="queue_book_teaser_route")
    async def queue_book_teaser_route(request: Request, book_id: UUID):
        require_local_origin(request)
        context = await teaser_context(book_id)
        project = context["project"]
        if project is None or project.state != "ready":
            raise UploadError("Bitte zuerst den fertigen Book-Teaser rendern.", 409)
        if not context["book_synced"]:
            raise UploadError("Bitte das aktuelle Buch zuerst nach Supabase übertragen.", 409)
        if (context["render_job"] and context["render_job"]["active"]) or any(
            row["source_stale"] for row in context["rows"] if row["segment"].included
        ):
            raise UploadError("Die Teaser-Quellen wurden geändert. Bitte den Teaser neu rendern.", 409)
        try:
            async with request.form(max_files=0, max_fields=11) as form:
                fields = {"project_revision", "title", "description", "queue_mode",
                          "scheduled_for", "requeue"}
                if set(form) != fields | {"platform"} or any(
                    len(form.getlist(key)) != 1 or not isinstance(form[key], str) for key in fields
                ):
                    raise ValueError("Bitte das vollständige Teaser-Queue-Formular verwenden.")
                revision = int(form["project_revision"])
                title, description = form["title"].strip(), form["description"].strip()
                platforms = tuple(form.getlist("platform"))
                if not platforms or len(set(platforms)) != len(platforms) or any(
                    not isinstance(platform, str) or platform not in PLATFORMS for platform in platforms
                ):
                    raise ValueError("Mindestens eine gültige Plattform auswählen.")
                if form["requeue"] not in {"0", "1"}:
                    raise ValueError("Der Veröffentlichungsmodus ist ungültig.")
                requeue = form["requeue"] == "1"
                queue_mode = form["queue_mode"]
                scheduled_raw = form["scheduled_for"].strip()
                if queue_mode == "daily":
                    if scheduled_raw:
                        raise ValueError("Die tägliche Queue hat keinen festen Termin.")
                    scheduled_for = None
                elif queue_mode == "scheduled":
                    scheduled = datetime.fromisoformat(scheduled_raw)
                    if scheduled.tzinfo is None:
                        scheduled = scheduled.replace(tzinfo=ZoneInfo("Europe/Berlin"))
                    if scheduled <= datetime.now(ZoneInfo("Europe/Berlin")):
                        raise ValueError("Der Veröffentlichungszeitpunkt muss in der Zukunft liegen.")
                    scheduled_for = scheduled.isoformat()
                else:
                    raise ValueError("Bitte tägliche Queue oder festen Termin auswählen.")
        except (TypeError, ValueError):
            raise UploadError("Bitte Titel, Plattformen und Veröffentlichungszeitpunkt prüfen.") from None
        if revision != project.revision:
            raise UploadError("Der Book-Teaser wurde zwischenzeitlich geändert. Bitte neu laden.", 409)
        path = await run_in_threadpool(teaser_store.output_path, project)
        if path is None:
            raise UploadError("Der fertige Book-Teaser fehlt oder wurde geändert.", 409)
        track = next((track for track in context["tracks"] if track.id == project.audio_track_id), None)
        from .teaser_publication import enqueue_book_teaser
        await enqueue_book_teaser(
            repository or SupabaseRepository(settings), settings, project=project, path=path,
            book_profile=context["management"]["details"].model_dump(mode="json"),
            audio_title=track.title if track else None,
            defaults=context["publication_defaults"].values, platforms=platforms,
            title=title, description=description, queue_mode=queue_mode,
            scheduled_for=scheduled_for, requeue=requeue,
        )
        return RedirectResponse(
            str(request.url_for("book_teaser", book_id=book_id)) + "?teaser_queued=true",
            status_code=303,
        )

    @app.get("/books/local/{book_id}/teaser/render/status", name="book_teaser_render_status")
    async def book_teaser_render_status(book_id: UUID):
        if await run_in_threadpool(local_store.get_book, book_id) is None:
            raise HTTPException(404)
        job = await run_in_threadpool(teaser_job_store.latest, str(book_id))
        if job is None:
            return JSONResponse({
                "state": "not_started", "active": False, "stage": "",
                "progress_ms": 0, "total_ms": 0, "error": None,
            })
        return JSONResponse({key: job.get(key) for key in (
            "state", "active", "stage", "progress_ms", "total_ms", "error",
        )})

    @app.get("/books/local/{book_id}/teaser/video.mp4", name="book_teaser_video")
    async def book_teaser_video(book_id: UUID, download: bool = False):
        project = await run_in_threadpool(teaser_store.get, str(book_id))
        if project is None or project.state != "ready":
            raise HTTPException(404)
        path = await run_in_threadpool(teaser_store.output_path, project)
        if path is None:
            raise HTTPException(404)
        return FileResponse(
            path, media_type="video/mp4", filename="book-teaser.mp4" if download else None,
        )

    @app.post("/books/local/{book_id}/delete", name="delete_book")
    async def delete_book(request: Request, book_id: UUID):
        require_local_origin(request)
        if sync_lock.locked():
            raise UploadError("Während einer Übertragung kann kein Buch gelöscht werden.", 409)
        try:
            await run_in_threadpool(local_store.delete_book, book_id)
        except (OSError, sqlite3.Error):
            raise UploadError(
                "Das lokale Buch konnte nicht vollständig gelöscht werden. Bitte Ablage und Zugriffsrechte prüfen.",
                503,
            ) from None
        return RedirectResponse(str(request.url_for("books")) + "?deleted=true", status_code=303)

    @app.post("/books/local/{book_id}/extract", name="extract_book")
    async def extract_book(request: Request, book_id: UUID):
        require_local_origin(request)
        try:
            book = await run_in_threadpool(local_store.get_book, book_id)
            if book is None: raise HTTPException(404)
            await run_in_threadpool(job_store.enqueue, book, retry=True)
        except (OSError, sqlite3.Error):
            raise UploadError("Der Text konnte nicht gespeichert werden. Bitte Speicher und Zugriffsrechte prüfen.", 503) from None
        return RedirectResponse(request.url_for("local_book", book_id=book_id), status_code=303)

    @app.post("/books/local/{book_id}/sync", name="sync_book")
    async def sync_book(request: Request, book_id: UUID):
        require_local_origin(request)
        if sync_lock.locked(): raise UploadError("Eine Übertragung läuft bereits. Bitte kurz warten.",409)
        try:
            if await run_in_threadpool(local_store.get_book, book_id) is None: raise HTTPException(404)
            async with sync_lock:
                await sync_store.transfer(str(book_id), repository or SupabaseRepository(settings))
        except DatabaseError as exc:
            raise UploadError(str(exc),503 if exc.code != 'conflict' else 409) from None
        except (OSError,sqlite3.Error):
            raise UploadError("Die Übertragung konnte lokal nicht bestätigt werden. Derselbe Buchstand kann erneut übertragen werden.",503) from None
        return RedirectResponse(request.url_for('local_book',book_id=book_id),status_code=303)

    @app.get("/books/local/{book_id}/status", name="import_status")
    async def import_status(book_id: UUID):
        try:
            if await run_in_threadpool(local_store.get_book, book_id) is None: raise HTTPException(404)
            jobs = await run_in_threadpool(job_store.statuses, [str(book_id)])
        except (OSError, sqlite3.Error):
            raise UploadError("Der Importstatus konnte nicht gelesen werden.", 503) from None
        return jobs.get(str(book_id)) or {"state": "uploaded", "label": "Datei gespeichert", "active": False}

    @app.post("/books/local/{book_id}/chapters", name="correct_chapters")
    async def correct_chapters(request: Request, book_id: UUID):
        require_local_origin(request)
        try:
            if await run_in_threadpool(local_store.get_book, book_id) is None: raise HTTPException(404)
            if await run_in_threadpool(extraction_store.get, str(book_id)) is None:
                raise UploadError("Bitte zuerst Text und Kapitel einlesen.", 409)
            async with request.form(max_files=0, max_fields=1001) as form:
                starts, titles, counts = (form.getlist(key) for key in ("start", "title", "heading_count"))
                if not 1 <= len(starts) <= 250 or not len(starts) == len(titles) == len(counts):
                    raise UploadError("Bitte 1 bis 250 vollständige Kapitelzeilen angeben.")
                boundaries = [Boundary(paragraph_id=f"p{int(start):06d}", title=title.strip(), heading_count=int(count))
                              for start, title, count in zip(starts, titles, counts)]
                revision = int(form.get("revision", ""))
            await run_in_threadpool(extraction_store.correct, str(book_id), revision, boundaries)
        except (ValueError, TypeError, ValidationError):
            raise UploadError("Bitte gültige Absatznummern, Kapiteltitel (maximal 200 Zeichen) und 0 bis 2 Überschriftabsätze angeben.") from None
        except (OSError, sqlite3.Error):
            raise UploadError("Die Kapitelaufteilung konnte nicht gespeichert werden.", 503) from None
        return RedirectResponse(request.url_for("local_book", book_id=book_id), status_code=303)

    @app.post("/books/local/{book_id}/analyze", name="analyze_book")
    async def start_analysis(request: Request, book_id: UUID):
        require_local_origin(request)
        if settings._env_path is None:
            raise UploadError("Bitte die Anwendung mit zugeordneter ENV-Datei über start.bat starten.", 409)
        try:
            provider = await requested_text_provider(request)
            if await run_in_threadpool(local_store.get_book, book_id) is None: raise HTTPException(404)
            options = AnalysisOptions(chunk_chars=settings.analysis_chunk_chars, min_score=settings.analysis_min_score,
                max_quotes_per_chapter=settings.analysis_max_quotes_per_chapter, max_calls=settings.analysis_max_calls)
            await run_in_threadpool(
                analysis_store.enqueue, str(book_id), settings, options, provider,
            )
        except (OSError, sqlite3.Error):
            raise UploadError("Die KI-Analyse konnte nicht vorgemerkt werden. Bitte die lokale Ablage prüfen.", 503) from None
        return RedirectResponse(request.url_for("local_book", book_id=book_id), status_code=303)

    @app.get("/books/local/{book_id}/analysis-status", name="analysis_status")
    async def analysis_status(book_id: UUID):
        try:
            if await run_in_threadpool(local_store.get_book, book_id) is None: raise HTTPException(404)
            status = await run_in_threadpool(analysis_store.latest, str(book_id))
        except (OSError, sqlite3.Error):
            raise UploadError("Der Analysestatus konnte nicht gelesen werden.", 503) from None
        return status or {"active": False, "state": "not_started", "label": "Noch nicht analysiert"}

    @app.get("/books/local/{book_id}/chapters/{chapter_id}", response_class=HTMLResponse, name="local_chapter")
    async def local_chapter(request: Request, book_id: UUID, chapter_id: UUID,
                            filter: Literal["all", "usable", "blocked", "unsuitable", "unused", "used"] = "all",
                            quote_page: int = Query(default=1, ge=1, le=100000)):
        try:
            book = await run_in_threadpool(local_store.get_book, book_id)
            record = await run_in_threadpool(extraction_store.get, str(book_id))
            analysis = await run_in_threadpool(analysis_store.latest, str(book_id), include_result=True)
            management = await run_in_threadpool(management_store.get, str(book_id)) if book else None
        except (OSError, sqlite3.Error):
            raise UploadError("Der Kapiteltext konnte nicht gelesen werden.", 503) from None
        chapter = next((c for c in record.result.chapters if c.id == str(chapter_id)), None) if record and record.result else None
        if book is None or chapter is None: raise HTTPException(404)
        items = [q for q in management["quotes"] if q["quote"].chapter_id == chapter.id]
        usage,usage_error={},None
        if settings.supabase_enabled or repository is not None:
            try:
                db=repository or SupabaseRepository(settings)
                await db.check_schema()
                usage=await db.quote_usage([q['quote'].id for q in items])
            except DatabaseError as exc: usage_error=str(exc)
        else: usage_error='Noch keine Veröffentlichungshistorie angebunden.'
        for item in items:
            item['usage']=usage.get(item['quote'].id)
            draft = await run_in_threadpool(
                reel_store.find_draft, str(book_id), quote_key(book.version_id, item["quote"]),
                management["suggestion_id"],
            )
            if draft is not None:
                labels = {
                    "editing": "In Bearbeitung", "generating": "Wird erzeugt",
                    "ready": "Bereit", "uploading": "Wird übertragen",
                    "stocked": "Im Reel-Vorrat", "failed": "Fehler",
                }
                item["reel"] = {"state": draft.state, "label": labels.get(draft.state, draft.state)}
        counts = {"all": len(items), "usable": sum(q["usable"] for q in items),
                  "blocked": sum(q["blocked"] for q in items), "unsuitable": sum(not q["quote"].usable for q in items),
                  "unused":sum(q['usage'] is not None and q['usage'].last_published_at is None for q in items),
                  "used":sum(q['usage'] is not None and q['usage'].last_published_at is not None for q in items)}
        filtered = [q for q in items if filter == "all" or (filter == "usable" and q["usable"])
                    or (filter == "blocked" and q["blocked"]) or (filter == "unsuitable" and not q["quote"].usable)
                    or (filter == 'unused' and q['usage'] is not None and q['usage'].last_published_at is None)
                    or (filter == 'used' and q['usage'] is not None and q['usage'].last_published_at is not None)]
        return templates.TemplateResponse(request=request, name="local_chapter.html",
            context={"active_page": "books", "book": book, "chapter": chapter, "record": record,
                     "analysis": analysis, "management": management, "quote_items": filtered[(quote_page-1)*20:quote_page*20],
                     "usage_error":usage_error,
                     "filters": FILTERS, "filter": filter, "filter_counts": counts, "quote_page": quote_page, "quote_more": len(filtered)>quote_page*20,
                     "summary": analysis["result"].chapter_summaries.get(chapter.id) if analysis and analysis["result"] else None})

    @app.get(
        "/books/local/{book_id}/chapters/{chapter_id}/quotes/{quote_id}/reel",
        response_class=HTMLResponse,
        name="reel_workshop",
    )
    async def reel_workshop(request: Request, book_id: UUID, chapter_id: UUID, quote_id: UUID):
        try:
            return await workshop_response(request, book_id, chapter_id, quote_id)
        except (OSError, sqlite3.Error):
            raise UploadError("Die Reel-Werkstatt konnte nicht geladen werden.", 503) from None

    @app.post(
        "/books/local/{book_id}/chapters/{chapter_id}/quotes/{quote_id}/reel/copy",
        name="generate_reel_copy_route",
    )
    async def generate_reel_copy_route(request: Request, book_id: UUID, chapter_id: UUID, quote_id: UUID):
        require_local_origin(request)
        provider = await requested_text_provider(request)
        _, _, quote, management, draft = await reel_context(book_id, chapter_id, quote_id, create=True)
        characters = await run_in_threadpool(character_store.list, str(book_id))
        if reel_ai_lock.locked():
            raise UploadError("Eine Reel-KI-Anfrage läuft bereits. Bitte kurz warten.", 409)
        async with reel_ai_lock:
            result = await generate_reel_copy(
                create_text_client(settings, provider), quote=quote.text,
                book_profile=management["details"].model_dump(),
                context_before=quote.context_before,
                context_after=quote.context_after,
            )
        selected_ids = _detected_character_ids(
            characters, quote, generated_prompt=result.image_prompt,
        )
        await run_in_threadpool(
            reel_store.update_draft, draft.id, draft.revision,
            caption_addition=result.addition, final_caption=result.caption, image_prompt=result.image_prompt,
            character_ids=selected_ids,
        )
        return action_response(request, book_id, chapter_id, quote_id)

    @app.post(
        "/books/local/{book_id}/chapters/{chapter_id}/quotes/{quote_id}/reel/copy/save",
        name="save_reel_copy_route",
    )
    async def save_reel_copy_route(request: Request, book_id: UUID, chapter_id: UUID, quote_id: UUID):
        require_local_origin(request)
        _, _, quote, management, draft = await reel_context(book_id, chapter_id, quote_id, create=True)
        try:
            async with request.form(max_files=0, max_fields=8) as form:
                required = {"revision", "addition", "image_prompt"}
                if set(form) - (required | {"character_ids"}) or required - set(form) or any(
                    len(form.getlist(key)) != 1 or not isinstance(form[key], str) for key in required
                ):
                    raise ValueError()
                revision = int(form["revision"])
                addition = form["addition"].strip()
                image_prompt = form["image_prompt"].strip()
                character_ids = form.getlist("character_ids")
            if not 1 <= len(addition) <= 800 or not 1 <= len(image_prompt) <= 8000:
                raise ValueError()
            details = management["details"]
            caption = compose_caption(
                quote=quote.text, addition=addition, title=details.title,
                author=details.author, target_url=details.target_url,
            )
        except (TypeError, ValueError):
            raise UploadError("Bitte Begleittext und Bildprompt vollständig und in zulässiger Länge angeben.") from None
        await run_in_threadpool(
            reel_store.update_draft, draft.id, revision,
            caption_addition=addition, final_caption=caption, image_prompt=image_prompt,
            character_ids=character_ids,
        )
        return action_response(request, book_id, chapter_id, quote_id)

    @app.post(
        "/books/local/{book_id}/chapters/{chapter_id}/quotes/{quote_id}/reel/image",
        name="generate_reel_image_route",
    )
    async def generate_reel_image_route(request: Request, book_id: UUID, chapter_id: UUID, quote_id: UUID):
        require_local_origin(request)
        _, _, _, _, draft = await reel_context(book_id, chapter_id, quote_id, create=True)
        await run_in_threadpool(reel_jobs.enqueue, draft.id, "image", {"operation": "scene"})
        return action_response(request, book_id, chapter_id, quote_id)

    @app.post(
        "/books/local/{book_id}/chapters/{chapter_id}/quotes/{quote_id}/reel/image/optimize",
        name="optimize_reel_image_route",
    )
    async def optimize_reel_image_route(request: Request, book_id: UUID, chapter_id: UUID, quote_id: UUID):
        require_local_origin(request)
        _, _, _, _, draft = await reel_context(book_id, chapter_id, quote_id, create=True)
        selected = {
            character.id: character for character in await run_in_threadpool(
                character_store.list, str(book_id)
            )
        }
        if not any(selected.get(item) and selected[item].has_reference for item in draft.character_ids):
            raise UploadError("Für die ausgewählten Charaktere fehlt ein Referenzbild.", 409)
        await run_in_threadpool(reel_jobs.enqueue, draft.id, "image", {"operation": "optimize"})
        return action_response(request, book_id, chapter_id, quote_id)

    @app.post(
        "/books/local/{book_id}/chapters/{chapter_id}/quotes/{quote_id}/reel/image/upload",
        name="upload_reel_image_route",
    )
    async def upload_reel_image_route(request: Request, book_id: UUID, chapter_id: UUID, quote_id: UUID):
        require_local_origin(request)
        _, _, _, _, draft = await reel_context(book_id, chapter_id, quote_id, create=True)
        if not request.headers.get("content-type", "").startswith("multipart/form-data"):
            raise UploadError("Bitte genau eine PNG-, JPEG- oder WebP-Datei auswählen.", 415)
        async with request.form(max_files=1, max_fields=1) as form:
            file = form.get("file")
            revision = form.get("revision")
            if set(form) != {"file", "revision"} or not isinstance(file, UploadFile) or not isinstance(revision, str):
                raise UploadError("Bitte genau eine Bilddatei auswählen.")
            try:
                expected_revision = int(revision)
            except ValueError:
                raise UploadError("Die Reel-Werkstatt ist veraltet. Bitte neu laden.", 409) from None
            await run_in_threadpool(
                reel_store.save_uploaded_image, draft.id, expected_revision,
                file.file, file.filename or "upload",
            )
        return action_response(request, book_id, chapter_id, quote_id)

    @app.post(
        "/books/local/{book_id}/chapters/{chapter_id}/quotes/{quote_id}/reel/image/select",
        name="select_reel_image_route",
    )
    async def select_reel_image_route(request: Request, book_id: UUID, chapter_id: UUID, quote_id: UUID):
        require_local_origin(request)
        _, _, _, _, draft = await reel_context(book_id, chapter_id, quote_id, create=True)
        async with request.form(max_files=0, max_fields=2) as form:
            if set(form) != {"revision", "source"}:
                raise UploadError("Bitte eine Bildvariante auswählen.")
            try:
                revision = int(str(form["revision"]))
                source = str(form["source"])
            except (TypeError, ValueError):
                raise UploadError("Bitte eine gültige Bildvariante auswählen.") from None
        await run_in_threadpool(reel_store.select_image, draft.id, revision, source)
        return action_response(request, book_id, chapter_id, quote_id)

    @app.post(
        "/books/local/{book_id}/chapters/{chapter_id}/quotes/{quote_id}/reel/audio/upload",
        name="upload_reel_audio_route",
    )
    async def upload_reel_audio_route(request: Request, book_id: UUID, chapter_id: UUID, quote_id: UUID):
        require_local_origin(request)
        await reel_context(book_id, chapter_id, quote_id, create=True)
        if not request.headers.get("content-type", "").startswith("multipart/form-data"):
            raise UploadError("Bitte genau eine WAV-Datei auswählen.", 415)
        async with request.form(max_files=1, max_fields=1) as form:
            file = form.get("file")
            title = form.get("title")
            if (set(form) != {"file", "title"} or not isinstance(file, UploadFile)
                    or not isinstance(title, str) or len(title.strip()) > 200):
                raise UploadError("Bitte eine WAV-Datei und optional einen gültigen Songtitel angeben.")
            await run_in_threadpool(
                reel_store.save_audio, str(book_id), file.file, file.filename,
                title=title.strip() or None,
            )
        return action_response(request, book_id, chapter_id, quote_id)

    @app.post(
        "/books/local/{book_id}/chapters/{chapter_id}/quotes/{quote_id}/reel/audio/select",
        name="select_reel_audio_route",
    )
    async def select_reel_audio_route(request: Request, book_id: UUID, chapter_id: UUID, quote_id: UUID):
        require_local_origin(request)
        _, _, _, _, draft = await reel_context(book_id, chapter_id, quote_id, create=True)
        try:
            async with request.form(max_files=0, max_fields=4) as form:
                required = {"revision", "track_id", "audio_start_seconds", "duration_seconds"}
                if set(form) != required or any(
                    len(form.getlist(key)) != 1 or not isinstance(form[key], str) for key in required
                ):
                    raise ValueError()
                revision = int(form["revision"])
                track_id = str(UUID(form["track_id"]))
                start_ms = round(float(form["audio_start_seconds"]) * 1000)
                duration_ms = round(float(form["duration_seconds"]) * 1000)
            if start_ms < 0 or not 4000 <= duration_ms <= 30000:
                raise ValueError()
        except (TypeError, ValueError):
            raise UploadError("Bitte Song, Startpunkt und Reel-Länge gültig auswählen.") from None
        await run_in_threadpool(
            reel_store.update_draft, draft.id, revision,
            audio_track_id=track_id, audio_start_ms=start_ms, audio_cue_id=None,
            duration_ms=duration_ms,
        )
        return action_response(request, book_id, chapter_id, quote_id)

    @app.post(
        "/books/local/{book_id}/chapters/{chapter_id}/quotes/{quote_id}/reel/motion",
        name="generate_reel_motion_route",
    )
    async def generate_reel_motion_route(request: Request, book_id: UUID, chapter_id: UUID, quote_id: UUID):
        require_local_origin(request)
        provider = await requested_text_provider(request)
        _, _, quote, management, draft = await reel_context(book_id, chapter_id, quote_id, create=True)
        if not draft.selected_image_path or draft.image_stale or not draft.audio_track_id:
            raise UploadError("Bitte zuerst Bild und Audioausschnitt festlegen.", 409)
        if reel_ai_lock.locked():
            raise UploadError("Eine Reel-KI-Anfrage läuft bereits. Bitte kurz warten.", 409)
        details = management["details"]
        async with reel_ai_lock:
            prompt = await generate_motion_prompt(
                create_text_client(settings, provider), image_prompt=draft.image_prompt,
                quote=quote.text,
                genre=details.genre, mood=details.mood,
                duration_seconds=draft.duration_ms / 1000,
            )
        identity_context = (
            _snapshot_identity_context(draft.character_snapshot)
            if draft.selected_image_source == "optimized" else ""
        )
        if identity_context:
            prompt = f"{prompt} Identity map: {identity_context}"
        try:
            prompt = validate_video_prompt(prompt)
        except ValueError:
            raise UploadError(
                "Der erzeugte Videoprompt enthält keine geeignete sichtbare Bewegung. Bitte erneut erzeugen.", 409,
            ) from None
        await run_in_threadpool(
            reel_store.update_draft, draft.id, draft.revision, video_prompt=prompt,
        )
        return action_response(request, book_id, chapter_id, quote_id)

    @app.post(
        "/books/local/{book_id}/chapters/{chapter_id}/quotes/{quote_id}/reel/motion/save",
        name="save_reel_motion_route",
    )
    async def save_reel_motion_route(request: Request, book_id: UUID, chapter_id: UUID, quote_id: UUID):
        require_local_origin(request)
        _, _, _, _, draft = await reel_context(book_id, chapter_id, quote_id, create=True)
        try:
            async with request.form(max_files=0, max_fields=2) as form:
                if set(form) != {"revision", "video_prompt"}:
                    raise ValueError()
                revision = int(form["revision"])
                prompt = str(form["video_prompt"]).strip()
            if not 20 <= len(prompt) <= 1800:
                raise ValueError()
            prompt = validate_video_prompt(prompt)
        except (TypeError, ValueError):
            raise UploadError("Bitte einen vollständigen Videoprompt angeben.") from None
        await run_in_threadpool(reel_store.update_draft, draft.id, revision, video_prompt=prompt)
        return action_response(request, book_id, chapter_id, quote_id)

    @app.post(
        "/books/local/{book_id}/chapters/{chapter_id}/quotes/{quote_id}/reel/video",
        name="generate_reel_video_route",
    )
    async def generate_reel_video_route(request: Request, book_id: UUID, chapter_id: UUID, quote_id: UUID):
        require_local_origin(request)
        _, _, _, _, draft = await reel_context(book_id, chapter_id, quote_id, create=True)
        await run_in_threadpool(reel_jobs.enqueue, draft.id, "video")
        return action_response(request, book_id, chapter_id, quote_id)

    @app.get(
        "/books/local/{book_id}/chapters/{chapter_id}/quotes/{quote_id}/reel/image.bin",
        name="reel_image",
    )
    async def reel_image(
        book_id: UUID, chapter_id: UUID, quote_id: UUID,
        source: Literal["selected", "scene", "optimized", "upload"] = "selected",
    ):
        _, _, _, _, draft = await reel_context(book_id, chapter_id, quote_id)
        if draft is None:
            raise HTTPException(404)
        path = await run_in_threadpool(reel_store.candidate_image_path, draft, source)
        if path is None:
            raise HTTPException(404)
        media = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(path.suffix.lower())
        return FileResponse(path, media_type=media)

    @app.get(
        "/books/local/{book_id}/chapters/{chapter_id}/quotes/{quote_id}/reel/audio/source/{track_id}.wav",
        name="reel_audio_source",
    )
    async def reel_audio_source(book_id: UUID, chapter_id: UUID, quote_id: UUID, track_id: UUID):
        await reel_context(book_id, chapter_id, quote_id)
        track = next(
            (value for value in await run_in_threadpool(reel_store.list_audio, str(book_id))
             if value.id == str(track_id)), None,
        )
        if track is None:
            raise HTTPException(404)
        return FileResponse(
            await run_in_threadpool(reel_store.audio_path, track),
            media_type="audio/wav",
        )

    @app.get(
        "/books/local/{book_id}/chapters/{chapter_id}/quotes/{quote_id}/reel/audio/waveform/{track_id}.json",
        name="reel_audio_waveform",
    )
    async def reel_audio_waveform(book_id: UUID, chapter_id: UUID, quote_id: UUID, track_id: UUID):
        await reel_context(book_id, chapter_id, quote_id)
        track = next(
            (value for value in await run_in_threadpool(reel_store.list_audio, str(book_id))
             if value.id == str(track_id)), None,
        )
        if track is None:
            raise HTTPException(404)
        peaks = await run_in_threadpool(wav_waveform, reel_store.audio_path(track))
        return JSONResponse({"duration_ms": track.duration_ms, "peaks": peaks})

    @app.get(
        "/books/local/{book_id}/chapters/{chapter_id}/quotes/{quote_id}/reel/audio.wav",
        name="reel_audio",
    )
    async def reel_audio(book_id: UUID, chapter_id: UUID, quote_id: UUID):
        _, _, _, _, draft = await reel_context(book_id, chapter_id, quote_id)
        if draft is None or draft.audio_track_id is None or draft.audio_start_ms is None:
            raise HTTPException(404)
        track = next(
            (value for value in await run_in_threadpool(reel_store.list_audio, str(book_id))
             if value.id == draft.audio_track_id), None,
        )
        if track is None:
            raise HTTPException(404)
        output = reel_store.root / "reel-work" / draft.id / f"preview-{draft.revision}.wav"
        if not output.is_file():
            await run_in_threadpool(
                split_audio_segment, reel_store.audio_path(track),
                start_seconds=draft.audio_start_ms / 1000,
                duration_seconds=draft.duration_ms / 1000,
                output_path=output, source_root=reel_store.root, output_root=reel_store.root,
            )
        return FileResponse(output, media_type="audio/wav", filename="reel-ausschnitt.wav")

    @app.get(
        "/books/local/{book_id}/chapters/{chapter_id}/quotes/{quote_id}/reel/video.mp4",
        name="reel_video",
    )
    async def reel_video(book_id: UUID, chapter_id: UUID, quote_id: UUID):
        _, _, _, _, draft = await reel_context(book_id, chapter_id, quote_id)
        if draft is None:
            raise HTTPException(404)
        path = await run_in_threadpool(reel_store.artifact_path, draft, "video")
        if path is None:
            raise HTTPException(404)
        return FileResponse(path, media_type="video/mp4")

    @app.post(
        "/books/local/{book_id}/chapters/{chapter_id}/quotes/{quote_id}/reel/queue",
        name="queue_reel_route",
    )
    async def queue_reel_route(request: Request, book_id: UUID, chapter_id: UUID, quote_id: UUID):
        require_local_origin(request)
        book, _, quote, management, draft = await reel_context(book_id, chapter_id, quote_id, create=True)
        if draft.state not in {"ready", "stocked"} or draft.video_stale:
            raise UploadError("Bitte zuerst Posting-Text, Bild, Audio und Video fertigstellen.", 409)
        receipt = await run_in_threadpool(sync_store.receipt, str(book_id))
        if receipt is None:
            raise UploadError("Bitte das aktuelle Buch zuerst auf der Buchseite nach Supabase übertragen.", 409)
        path = await run_in_threadpool(reel_store.artifact_path, draft, "video")
        if path is None:
            raise UploadError("Das fertige Reel-Video fehlt.", 409)
        form_values: dict[str, object] = {}
        try:
            async with request.form(max_files=0, max_fields=10) as form:
                allowed = {"title", "description", "queue_mode", "scheduled_for", "platform", "requeue"}
                if set(form) - allowed:
                    raise ValueError("Das Reel-Formular enthält unbekannte Felder.")
                for name in allowed - {"platform"}:
                    values = form.getlist(name)
                    if len(values) != 1 or not isinstance(values[0], str):
                        raise ValueError("Bitte das vollständige Reel-Formular verwenden.")
                    form_values[name] = values[0]
                platforms = form.getlist("platform")
                if not platforms or len(platforms) != len(set(platforms)) or any(
                    not isinstance(value, str) or value not in PLATFORMS for value in platforms
                ):
                    raise ValueError("Mindestens eine gültige Plattform auswählen.")
                form_values["platforms"] = platforms
            title = str(form_values["title"]).strip()
            description = str(form_values["description"]).strip()
            if str(form_values["requeue"]) not in {"0", "1"}:
                raise ValueError("Der Übertragungsmodus ist ungültig.")
            requeue = str(form_values["requeue"]) == "1"
            if not 1 <= len(title) <= 300 or not 1 <= len(description) <= 5000:
                raise ValueError("Titel oder Beschreibung haben eine ungültige Länge.")
            if quote.text not in description:
                raise ValueError("Die Beschreibung muss das freigegebene Zitat enthalten.")
            queue_mode = str(form_values["queue_mode"])
            if queue_mode not in {"daily", "scheduled"}:
                raise ValueError("Bitte tägliche Warteschlange oder festen Termin auswählen.")
            scheduled_raw = str(form_values["scheduled_for"]).strip()
            if queue_mode == "daily":
                if scheduled_raw:
                    raise ValueError("Die tägliche Warteschlange hat keinen festen Termin.")
                scheduled_for = None
            else:
                scheduled = datetime.fromisoformat(scheduled_raw)
                if scheduled.tzinfo is None:
                    scheduled = scheduled.replace(tzinfo=ZoneInfo("Europe/Berlin"))
                if scheduled <= datetime.now(ZoneInfo("Europe/Berlin")):
                    raise ValueError("Der Veröffentlichungszeitpunkt muss in der Zukunft liegen.")
                scheduled_for = scheduled.isoformat()
        except ValueError as exc:
            raise UploadError(str(exc), 400) from None

        data = await run_in_threadpool(path.read_bytes)
        if len(data) > settings.reel_max_video_mb * 1024 * 1024:
            raise UploadError("Das fertige Reel ist größer als das eingestellte Upload-Limit.", 413)
        db = repository or SupabaseRepository(settings)
        await db.check_schema()
        defaults = await run_in_threadpool(
            reel_store.get_publication_defaults, settings.reel_storage_provider,
        )
        selected = tuple(form_values["platforms"])
        if any(platform not in defaults.values.selected() for platform in selected):
            raise UploadError("Die Plattformeinstellungen wurden geändert. Bitte die Reel-Werkstatt neu laden.", 409)
        asset_id = _reel_delivery_asset_id(draft, requeue=requeue)
        object_path = f"{asset_id}/{draft.selected_video_sha256}.mp4"
        if defaults.values.storage_provider == "cloudflare_r2":
            try:
                r2 = R2Client(settings)
                await r2.upload_reel(object_path, data, draft.selected_video_sha256)
            except R2Error as exc:
                raise UploadError(str(exc), 503) from None
            storage_bucket = settings.r2_bucket
            public_url = r2.public_url(object_path)
        else:
            await db.upload_reel(object_path, data)
            storage_bucket = "book-promotion-reels"
            public_url = None
        tracks = await run_in_threadpool(reel_store.list_audio, str(book_id))
        track = next((value for value in tracks if value.id == draft.audio_track_id), None)
        details = management["details"]
        asset = {
            "id": asset_id,
            "quote_id": quote.id,
            "book_id": str(book.id),
            "quote_text": quote.text,
            "addition": draft.caption_addition,
            "title": title,
            "description": description,
            "book_profile": details.model_dump(mode="json"),
            "image_prompt": draft.image_prompt,
            "video_prompt": draft.video_prompt,
            "storage_provider": defaults.values.storage_provider,
            "storage_bucket": storage_bucket,
            "storage_path": object_path,
            "public_url": public_url,
            "media_sha256": draft.selected_video_sha256,
            "size_bytes": len(data),
            "duration_ms": draft.duration_ms,
            "width": 512,
            "height": 896,
            "audio_title": track.title if track else None,
            "audio_start_ms": draft.audio_start_ms,
        }
        publications = [
            {
                "platform": platform,
                "account_id": defaults.values.platform(platform).account_id,
                "queue_mode": queue_mode,
                "scheduled_for": scheduled_for,
                "priority": 0,
                "title": title,
                "description": description,
                "options": defaults.values.platform(platform).options,
            }
            for platform in selected
        ]
        await db.enqueue_reel(asset, publications)
        try:
            await run_in_threadpool(reel_store.mark_stocked, draft.id, draft.revision, object_path)
        except UploadError as exc:
            current = await run_in_threadpool(reel_store.get_draft, draft.id)
            if not (exc.status == 409 and current and current.state == "stocked"
                    and current.remote_video_path == object_path):
                raise
        return action_response(request, book_id, chapter_id, quote_id)

    @app.get("/books/local/{book_id}/settings", response_class=HTMLResponse, name="book_settings")
    async def book_settings(request: Request, book_id: UUID, preview: Literal["saved", "ai"] = "saved", saved: bool = False,
                            asset_saved: Literal["", "cover_front", "logo"] = "", promotion_pending: bool = False):
        try:
            book = await run_in_threadpool(local_store.get_book, book_id)
            if book is None: raise HTTPException(404)
            management = await run_in_threadpool(management_store.get, str(book_id), prefer_ai=preview == "ai")
        except (OSError, sqlite3.Error):
            raise UploadError("Die Bucheinstellungen konnten nicht gelesen werden.", 503) from None
        context = await book_settings_context(
            book, management, management["details"], saved=saved, asset_saved=asset_saved,
            promotion_pending=promotion_pending,
        )
        return templates.TemplateResponse(request=request, name="book_settings.html", context=context)

    @app.post("/books/local/{book_id}/settings", name="save_book_settings")
    async def save_book_settings(request: Request, book_id: UUID):
        require_local_origin(request)
        try:
            book = await run_in_threadpool(local_store.get_book, book_id)
            if book is None: raise HTTPException(404)
            async with request.form(max_files=0, max_fields=26) as form:
                allowed = set(BookDetails.model_fields) | {"revision", "suggestion_id", "profile_run_id"}
                if (set(form) - allowed or allowed - {"promotion_enabled", "profile_run_id"} - set(form)
                    or any(len(form.getlist(k)) != 1 or not isinstance(form[k], str) for k in form)):
                    raise UploadError("Bitte das vollständige Formular der Bucheinstellungen verwenden.")
                raw = dict(form)
            revision = int(raw.pop("revision", ""))
            suggestion_id = raw.pop("suggestion_id", "")
            profile_run_id = raw.pop("profile_run_id", "")
            if raw.get("promotion_enabled", "off") not in {"on", "off"}: raise ValueError()
            raw["promotion_enabled"] = raw.get("promotion_enabled") == "on"
            try:
                details = BookDetails.model_validate(raw)
            except ValidationError:
                management = await run_in_threadpool(management_store.get, str(book_id))
                management.update(revision=revision, suggestion_id=suggestion_id)
                context = await book_settings_context(book, management, raw, profile_run_id=profile_run_id,
                    form_error="Bitte Titel, Feldlängen, Freigabemodus, Schriftname, Titel- und Verlaufsfarben sowie eine vollständige HTTP-/HTTPS-Zieladresse ohne Zugangsdaten prüfen. Deine Eingaben wurden noch nicht gespeichert.")
                return templates.TemplateResponse(request=request, name="book_settings.html", status_code=400, context=context)
            try:
                overlay = await run_in_threadpool(overlay_store.prepare, details)
            except OverlayError as error:
                management = await run_in_threadpool(management_store.get, str(book_id))
                management.update(revision=revision, suggestion_id=suggestion_id)
                context = await book_settings_context(book, management, raw, profile_run_id=profile_run_id,
                                                      form_error=str(error))
                return templates.TemplateResponse(request=request, name="book_settings.html", status_code=400, context=context)
            missing = await run_in_threadpool(asset_store.configuration_errors, str(book_id), details)
            if details.promotion_enabled and missing:
                # Metadata must not be lost merely because promotion assets are
                # incomplete. Persist everything else and leave activation off.
                details = details.model_copy(update={"promotion_enabled": False})
                promotion_pending = True
            else:
                promotion_pending = False
            if details.promotion_enabled:
                try:
                    await run_in_threadpool(
                        render_book_carousel_end_slide, asset_store, str(book_id), details, overlay
                    )
                except (UploadError, OverlayError) as error:
                    management = await run_in_threadpool(management_store.get, str(book_id))
                    management.update(revision=revision, suggestion_id=suggestion_id)
                    context = await book_settings_context(
                        book, management, details, profile_run_id=profile_run_id,
                        form_error="Die Carousel-Schlussseite konnte nicht erstellt werden: " + str(error),
                    )
                    return templates.TemplateResponse(
                        request=request, name="book_settings.html", status_code=400, context=context
                    )
            await run_in_threadpool(management_store.save, str(book_id), revision, details, suggestion_id, profile_run_id)
            if overlay is not None:
                await run_in_threadpool(overlay_store.save, str(book_id), overlay)
        except (ValueError, TypeError):
            raise UploadError("Bitte die Bucheinstellungen neu öffnen und gültige Werte eintragen.") from None
        except (OSError, sqlite3.Error):
            raise UploadError("Die Bucheinstellungen konnten nicht gespeichert werden.", 503) from None
        query = "?saved=true" + ("&promotion_pending=true" if promotion_pending else "")
        return RedirectResponse(str(request.url_for("book_settings", book_id=book_id)) + query, status_code=303)

    @app.post("/books/local/{book_id}/settings/assets/{kind}", name="upload_book_asset")
    async def upload_book_asset(request: Request, book_id: UUID, kind: Literal["cover_front", "logo"]):
        require_local_origin(request)
        if not request.headers.get("content-type", "").startswith("multipart/form-data"):
            raise UploadError("Bitte genau eine Bilddatei auswählen.", 415)
        try:
            book = await run_in_threadpool(local_store.get_book, book_id)
            if book is None:
                raise HTTPException(404)
            async with request.form(max_files=1, max_fields=1) as form:
                if (set(form) != {"file", "revision"} or len(form.getlist("file")) != 1
                    or len(form.getlist("revision")) != 1 or not isinstance(form["file"], UploadFile)
                    or not isinstance(form["revision"], str)):
                    raise UploadError("Bitte genau eine Bilddatei aus dem aktuellen Formular auswählen.")
                revision = int(form["revision"])
                upload = form["file"]
                await run_in_threadpool(asset_store.save, str(book_id), kind, upload.file, upload.filename, revision)
        except (ValueError, TypeError):
            raise UploadError("Die Bildansicht ist veraltet. Bitte die Seite neu öffnen.", 409) from None
        except (OSError, sqlite3.Error):
            raise UploadError("Das Bild konnte nicht gespeichert werden. Bitte die lokale Ablage prüfen.", 503) from None
        destination = str(request.url_for("book_settings", book_id=book_id)) + f"?asset_saved={kind}"
        return RedirectResponse(destination, status_code=303)

    @app.get("/books/local/{book_id}/settings/assets/{kind}.png", name="book_asset")
    async def book_asset(book_id: UUID, kind: Literal["cover_front", "logo"]):
        try:
            if await run_in_threadpool(local_store.get_book, book_id) is None:
                raise HTTPException(404)
            asset = await run_in_threadpool(asset_store.get, str(book_id), kind)
            if asset is None:
                raise HTTPException(404)
            path = asset_store.path(asset)
            if not path.is_file():
                raise HTTPException(404)
        except (OSError, sqlite3.Error):
            raise UploadError("Das Bild konnte nicht gelesen werden.", 503) from None
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})

    def character_settings_url(request: Request, book_id: UUID) -> str:
        return str(request.url_for("book_settings", book_id=book_id)) + "#characters"

    @app.post("/books/local/{book_id}/settings/characters", name="create_book_character")
    async def create_book_character(request: Request, book_id: UUID):
        require_local_origin(request)
        if await run_in_threadpool(local_store.get_book, book_id) is None:
            raise HTTPException(404)
        async with request.form(max_files=0, max_fields=3) as form:
            if set(form) != {"name", "description", "image_prompt"}:
                raise UploadError("Bitte das vollständige Charakterformular verwenden.")
            await run_in_threadpool(
                character_store.create, str(book_id), name=str(form["name"]),
                description=str(form["description"]), image_prompt=str(form["image_prompt"]),
            )
        return RedirectResponse(character_settings_url(request, book_id), status_code=303)

    @app.post(
        "/books/local/{book_id}/settings/characters/{character_id}",
        name="save_book_character",
    )
    async def save_book_character(request: Request, book_id: UUID, character_id: UUID):
        require_local_origin(request)
        async with request.form(max_files=0, max_fields=6) as form:
            required = {"revision", "name", "aliases", "description", "image_prompt"}
            if set(form) - (required | {"approved"}) or required - set(form):
                raise UploadError("Bitte das vollständige Charakterformular verwenden.")
            try:
                revision = int(form["revision"])
            except (TypeError, ValueError):
                raise UploadError("Die Charakteransicht ist veraltet. Bitte neu öffnen.", 409) from None
            await run_in_threadpool(
                character_store.save, str(book_id), str(character_id), revision,
                name=str(form["name"]), aliases=str(form["aliases"]),
                description=str(form["description"]), image_prompt=str(form["image_prompt"]),
                approved=form.get("approved") == "on",
            )
        return RedirectResponse(character_settings_url(request, book_id), status_code=303)

    @app.post(
        "/books/local/{book_id}/settings/characters/{character_id}/image",
        name="generate_book_character_image",
    )
    async def generate_book_character_image(request: Request, book_id: UUID, character_id: UUID):
        require_local_origin(request)
        async with request.form(max_files=0, max_fields=1) as form:
            if set(form) != {"revision"}:
                raise UploadError("Die Charakteransicht ist veraltet. Bitte neu öffnen.", 409)
            try:
                revision = int(form["revision"])
            except (TypeError, ValueError):
                raise UploadError("Die Charakteransicht ist veraltet. Bitte neu öffnen.", 409) from None
        character = await run_in_threadpool(character_store.get, str(book_id), str(character_id))
        if character is None:
            raise HTTPException(404)
        if character.revision != revision:
            raise UploadError("Der Charakter wurde zwischenzeitlich geändert. Bitte neu öffnen.", 409)
        if not character.image_prompt:
            raise UploadError("Bitte zuerst einen Bildprompt für den Charakter speichern.", 409)
        management = await run_in_threadpool(management_store.get, str(book_id))
        reference_scene = (
            "Neutral character reference portrait, one person only, vertical composition, "
            "face and complete hair clearly visible, natural expression, even cinematic lighting, simple "
            "unobtrusive background, no text, no letters, no logo, no watermark. Character identity: "
            + character.image_prompt
        )
        if not management["details"].image_prompt_base:
            reference_scene = "Photorealistic " + reference_scene
        prompt = compose_image_generation_prompt(
            scene_prompt=reference_scene,
            art_direction=management["details"].image_prompt_base,
        )
        generator = ReelGenerator(
            ComfyClient(str(settings.comfyui_url)),
            image_workflow_path=settings.reel_image_workflow,
            video_workflow_path=settings.reel_video_workflow,
            output_root=character_store.root / "character-work",
        )
        async with comfy_lock:
            generated = await run_in_threadpool(
                generator.generate_image, reel_id=f"character-{character.id}", image_prompt=prompt,
            )
        await run_in_threadpool(
            character_store.save_reference_file, str(book_id), str(character_id), revision, generated,
        )
        return RedirectResponse(character_settings_url(request, book_id), status_code=303)

    @app.post(
        "/books/local/{book_id}/settings/characters/{character_id}/upload",
        name="upload_book_character_image",
    )
    async def upload_book_character_image(request: Request, book_id: UUID, character_id: UUID):
        require_local_origin(request)
        if not request.headers.get("content-type", "").startswith("multipart/form-data"):
            raise UploadError("Bitte genau ein Charakterbild auswählen.", 415)
        async with request.form(max_files=1, max_fields=1) as form:
            if (set(form) != {"file", "revision"} or not isinstance(form.get("file"), UploadFile)
                    or not isinstance(form.get("revision"), str)):
                raise UploadError("Bitte genau ein Charakterbild auswählen.")
            try:
                revision = int(form["revision"])
            except ValueError:
                raise UploadError("Die Charakteransicht ist veraltet. Bitte neu öffnen.", 409) from None
            await run_in_threadpool(
                character_store.save_reference_file, str(book_id), str(character_id), revision,
                form["file"].file,
            )
        return RedirectResponse(character_settings_url(request, book_id), status_code=303)

    @app.get(
        "/books/local/{book_id}/settings/characters/{character_id}/image.png",
        name="book_character_image",
    )
    async def book_character_image(book_id: UUID, character_id: UUID):
        character = await run_in_threadpool(character_store.get, str(book_id), str(character_id))
        if character is None:
            raise HTTPException(404)
        path = await run_in_threadpool(character_store.reference_path, character)
        if path is None:
            raise HTTPException(404)
        return FileResponse(path, media_type="image/png")

    @app.get("/books/local/{book_id}/carousel-end.jpg", name="carousel_end_preview")
    async def carousel_end_preview(book_id: UUID):
        try:
            if await run_in_threadpool(local_store.get_book, book_id) is None:
                raise HTTPException(404)
            management = await run_in_threadpool(management_store.get, str(book_id))
            details = management["details"]
            if await run_in_threadpool(asset_store.configuration_errors, str(book_id), details):
                raise HTTPException(404)
            overlay = await run_in_threadpool(overlay_store.prepare, details)
            if overlay is None:
                raise HTTPException(404)
            rendered = await run_in_threadpool(
                render_book_carousel_end_slide, asset_store, str(book_id), details, overlay
            )
        except OverlayError as error:
            raise UploadError(str(error), 409) from None
        except (OSError, sqlite3.Error):
            raise UploadError("Die Carousel-Vorschau konnte nicht erzeugt werden.", 503) from None
        return Response(rendered.data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/books/local/{book_id}/overlay.png", name="book_overlay")
    async def book_overlay(book_id: UUID):
        try:
            if await run_in_threadpool(local_store.get_book, book_id) is None:
                raise HTTPException(404)
            management = await run_in_threadpool(management_store.get, str(book_id))
            if not management["details"].overlay_title_font:
                raise HTTPException(404)
            path = overlay_store.path(book_id)
            if not path.is_file():
                raise HTTPException(404)
        except OSError:
            raise UploadError("Das Buch-Overlay konnte nicht gelesen werden.", 503) from None
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.post("/books/local/{book_id}/settings/profile", name="start_profile")
    async def start_profile(request: Request, book_id: UUID):
        require_local_origin(request)
        if settings._env_path is None:
            raise UploadError("Bitte die Anwendung über start.bat mit zugeordneter ENV-Datei starten.", 409)
        try:
            provider = await requested_text_provider(request)
            if await run_in_threadpool(local_store.get_book, book_id) is None: raise HTTPException(404)
            options = AnalysisOptions(purpose="profile", chunk_chars=settings.analysis_chunk_chars,
                                      max_calls=settings.analysis_max_calls)
            run_id = await run_in_threadpool(
                analysis_store.enqueue, str(book_id), settings, options, provider,
            )
        except (OSError, sqlite3.Error):
            raise UploadError("Die Profilanalyse konnte nicht vorgemerkt werden. Bitte die lokale Ablage prüfen.", 503) from None
        return {"id": run_id}

    @app.get("/books/local/{book_id}/settings/profile/status", name="profile_status")
    async def profile_status(book_id: UUID):
        try:
            if await run_in_threadpool(local_store.get_book, book_id) is None: raise HTTPException(404)
            status = await run_in_threadpool(analysis_store.latest, str(book_id), purpose="profile")
        except (OSError, sqlite3.Error):
            raise UploadError("Der Profilstatus konnte nicht gelesen werden.", 503) from None
        return status or {"active": False, "state": "not_started", "label": "Noch keine separate Profilanalyse gestartet"}

    @app.get("/books/local/{book_id}/settings/profile/result", name="profile_result")
    async def profile_result(book_id: UUID):
        try:
            if await run_in_threadpool(local_store.get_book, book_id) is None: raise HTTPException(404)
            status = await run_in_threadpool(analysis_store.latest, str(book_id), purpose="profile", include_result=True)
        except (OSError, sqlite3.Error):
            raise UploadError("Die Profilvorschläge konnten nicht gelesen werden.", 503) from None
        if not status or status["state"] != "done" or status["result"] is None:
            raise UploadError("Für den aktuellen Textstand liegen noch keine fertigen Profilvorschläge vor.", 409)
        fields = status["result"].profile.model_dump()
        for key in ("characters", "spoilers"): fields[key] = "\n".join(fields[key])
        return {"id": status["id"], "fields": fields}

    @app.post("/books/local/{book_id}/settings/character-analysis", name="start_character_analysis")
    async def start_character_analysis(request: Request, book_id: UUID):
        require_local_origin(request)
        if settings._env_path is None:
            raise UploadError("Bitte die Anwendung über start.bat mit zugeordneter ENV-Datei starten.", 409)
        try:
            provider = await requested_text_provider(request)
            if await run_in_threadpool(local_store.get_book, book_id) is None:
                raise HTTPException(404)
            options = AnalysisOptions(
                purpose="characters", chunk_chars=settings.analysis_chunk_chars,
                max_calls=settings.analysis_max_calls,
            )
            run_id = await run_in_threadpool(
                analysis_store.enqueue, str(book_id), settings, options, provider,
            )
        except (OSError, sqlite3.Error):
            raise UploadError(
                "Die Charakteranalyse konnte nicht vorgemerkt werden. Bitte die lokale Ablage prüfen.", 503,
            ) from None
        return {"id": run_id}

    @app.get("/books/local/{book_id}/settings/character-analysis/status", name="character_analysis_status")
    async def character_analysis_status(book_id: UUID):
        try:
            if await run_in_threadpool(local_store.get_book, book_id) is None:
                raise HTTPException(404)
            status = await run_in_threadpool(
                analysis_store.latest, str(book_id), purpose="characters",
            )
        except (OSError, sqlite3.Error):
            raise UploadError("Der Charakteranalyse-Status konnte nicht gelesen werden.", 503) from None
        return status or {
            "active": False, "state": "not_started",
            "label": "Noch keine separate Charakteranalyse gestartet",
        }

    @app.post("/books/local/{book_id}/chapters/{chapter_id}/quotes/{quote_id}/block", name="set_quote_block")
    async def set_quote_block(request: Request, book_id: UUID, chapter_id: UUID, quote_id: UUID):
        require_local_origin(request)
        try:
            if await run_in_threadpool(local_store.get_book, book_id) is None: raise HTTPException(404)
            async with request.form(max_files=0, max_fields=5) as form:
                if set(form) != {"run_id", "revision", "blocked", "filter", "quote_page"} or any(len(form.getlist(k)) != 1 or not isinstance(form[k], str) for k in form):
                    raise ValueError()
                run_id, revision = str(UUID(form["run_id"])), int(form["revision"])
                if form["blocked"] not in {"true", "false"} or form["filter"] not in FILTERS: raise ValueError()
                filter, quote_page = form["filter"], int(form["quote_page"])
                if not 1 <= quote_page <= 100000: raise ValueError()
                blocked = form["blocked"] == "true"
            await run_in_threadpool(management_store.set_blocked, str(book_id), str(chapter_id), run_id, str(quote_id), revision, blocked)
        except (ValueError, TypeError):
            raise UploadError("Bitte das Kapitel neu öffnen und den Zitat-Schalter dort verwenden.") from None
        except (OSError, sqlite3.Error):
            raise UploadError("Die Zitatsperre konnte nicht gespeichert werden.", 503) from None
        return RedirectResponse(str(request.url_for("local_chapter", book_id=book_id, chapter_id=chapter_id)) + f"?filter={filter}&quote_page={quote_page}#quotes-title", status_code=303)

    @app.exception_handler(UploadError)
    async def upload_error(request: Request, exc: UploadError):
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"error": str(exc)}, status_code=exc.status)
        return templates.TemplateResponse(request=request, name="error.html",
            context={"active_page": "books", "error_title": "Upload oder Buchablage nicht verfügbar",
                     "error_message": str(exc)}, status_code=exc.status)

    @app.exception_handler(DatabaseError)
    async def database_error(request: Request, exc: DatabaseError):
        status = 409 if exc.code == "conflict" else 503
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"error": str(exc)}, status_code=status)
        return templates.TemplateResponse(
            request=request, name="error.html",
            context={"active_page": "books", "error_title": "Veröffentlichungsdatenbank nicht verfügbar",
                     "error_message": str(exc)}, status_code=status,
        )

    async def settings_context(*, promotion_saved=False, promotion_form=None, promotion_form_error=None,
                               reel_saved=False, reel_form_error=None):
        # Only safe presence flags cross the template boundary. Never pass Settings.
        services = [
            {
                "name": label,
                "complete": not settings.missing_for(service),
                "fields": [
                    {"label": field_label, "present": getattr(settings, field) is not None}
                    for field, field_label in fields
                ],
            }
            for service, label, fields in (
                ("supabase", "Supabase", (
                    ("supabase_url", "Projekt-URL"), ("supabase_secret_key", "Server-Schlüssel"),
                )),
                ("openwebui", "Open WebUI", (
                    ("openwebui_url", "Server-URL"), ("openwebui_api_key", "API-Key"),
                    ("openwebui_model", "Modell"),
                )),
                ("comfyui", "ComfyUI", (
                    ("comfyui_url", "Server-URL"),
                    ("comfyui_qwen_model", "Lokales Qwen-Modell (optional)"),
                    ("comfyui_qwen_workflow", "Qwen-Textworkflow (optional)"),
                    ("reel_image_workflow", "Bild-Workflow"),
                    ("reel_video_workflow", "Video-Workflow"),
                )),
            )
        ]
        services[-1]["complete"] = services[-1]["complete"] and all(
            path.is_file() for path in (settings.reel_image_workflow, settings.reel_video_workflow)
        )
        services[-1]["fields"][2]["present"] = settings.comfyui_qwen_workflow.is_file()
        services[-1]["fields"][3]["present"] = settings.reel_image_workflow.is_file()
        services[-1]["fields"][4]["present"] = settings.reel_video_workflow.is_file()
        reel_defaults = await run_in_threadpool(
            reel_store.get_publication_defaults, settings.reel_storage_provider,
        )
        promotion, promotion_books, promotion_error = None, [], None
        if settings.supabase_enabled or repository is not None:
            try:
                db = repository or SupabaseRepository(settings)
                await db.check_schema()
                promotion = await db.get_promotion_settings()
                promotion_books = await db.list_promotion_books()
            except DatabaseError as exc:
                promotion_error = str(exc)
        if promotion_form is None and promotion is not None:
            promotion_form = {
                "mode": promotion.mode,
                "fixed_book_id": str(promotion.fixed_book_id) if promotion.fixed_book_id else "",
            }
        return {
            "active_page": "settings", "services": services,
            "database_enabled": settings.supabase_enabled or repository is not None,
            "promotion": promotion, "promotion_books": promotion_books,
            "promotion_error": promotion_error, "promotion_form": promotion_form or {},
            "promotion_saved": promotion_saved, "promotion_form_error": promotion_form_error,
            "reel_defaults": reel_defaults, "reel_saved": reel_saved,
            "reel_form_error": reel_form_error,
            "r2_public_url": bool(settings.r2_public_base_url),
        }

    @app.get("/settings", response_class=HTMLResponse, name="settings")
    async def settings_page(request: Request, promotion_saved: bool = False, reel_saved: bool = False):
        return templates.TemplateResponse(
            request=request, name="settings.html",
            context=await settings_context(promotion_saved=promotion_saved, reel_saved=reel_saved),
        )

    @app.post("/settings/reels", response_class=HTMLResponse, name="save_reel_settings")
    async def save_reel_settings(request: Request):
        require_local_origin(request)
        raw: dict[str, str | bool] = {}
        try:
            async with request.form(max_files=0, max_fields=30) as form:
                allowed = {
                    "revision", "storage_provider",
                    *(f"enabled_{platform}" for platform in PLATFORMS),
                    *(f"account_{platform}" for platform in PLATFORMS),
                    "instagram_share_to_feed", "facebook_share_to_feed",
                    "youtube_privacy_status", "youtube_category_id", "youtube_made_for_kids",
                    "youtube_notify_subscribers", "tiktok_privacy_level", "tiktok_allow_comment",
                    "tiktok_allow_duet", "tiktok_allow_stitch",
                }
                if set(form) - allowed or any(len(form.getlist(name)) != 1 for name in form):
                    raise ValueError("Bitte das vollständige Reel-Einstellungsformular verwenden.")
                raw = {name: str(form[name]) for name in form}
            revision = int(raw.get("revision", "-1"))
            provider = str(raw.get("storage_provider", ""))
            if provider not in {"supabase", "cloudflare_r2"}:
                raise ValueError("Bitte einen gültigen Reel-Speicher auswählen.")
            if provider == "cloudflare_r2" and settings.missing_for("r2"):
                raise ValueError("Für Cloudflare R2 fehlen Zugangsdaten oder S3-Endpoint in der ENV-Datei.")
            platforms: dict[str, PlatformDefault] = {}
            for platform in PLATFORMS:
                option_raw: dict[str, str | bool] = {}
                prefix = platform + "_"
                for name, value in raw.items():
                    if name.startswith(prefix):
                        option_raw[name[len(prefix):]] = value
                for boolean in (
                    "share_to_feed", "made_for_kids", "notify_subscribers", "allow_comment",
                    "allow_duet", "allow_stitch",
                ):
                    option_raw[boolean] = f"{platform}_{boolean}" in raw
                platforms[platform] = PlatformDefault(
                    enabled=f"enabled_{platform}" in raw,
                    account_id=str(raw.get(f"account_{platform}", "")),
                    options=platform_options(platform, option_raw),
                )
            values = PublicationDefaults(storage_provider=provider, **platforms)
            if not values.selected():
                raise ValueError("Mindestens eine Veröffentlichungsplattform aktivieren.")
            await run_in_threadpool(reel_store.save_publication_defaults, revision, values)
        except (ValueError, ValidationError) as exc:
            context = await settings_context(reel_form_error=str(exc))
            return templates.TemplateResponse(request=request, name="settings.html", context=context, status_code=400)
        except (OSError, sqlite3.Error, UploadError) as exc:
            context = await settings_context(reel_form_error=str(exc))
            return templates.TemplateResponse(request=request, name="settings.html", context=context, status_code=409)
        return RedirectResponse(str(request.url_for("settings")) + "?reel_saved=true#reel-publishing", status_code=303)

    @app.post("/settings/r2/check", name="check_r2")
    async def check_r2(request: Request):
        require_local_origin(request)
        try:
            await R2Client(settings).check()
        except R2Error as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
        return {"message": "R2-Upload, HEAD-Prüfung und Löschen funktionieren."}

    @app.post("/settings/promotion", response_class=HTMLResponse, name="save_promotion_settings")
    async def save_promotion_settings(request: Request):
        require_local_origin(request)
        form_values = {}
        try:
            async with request.form(max_files=0, max_fields=4) as form:
                required = {"settings_id", "revision", "mode", "fixed_book_id"}
                if set(form) != required or any(
                    len(form.getlist(name)) != 1 or not isinstance(form[name], str) for name in required
                ):
                    raise ValueError("Bitte das vollständige Promotion-Formular verwenden.")
                form_values = {name: form[name] for name in required}
            if form_values["mode"] not in {"random_book", "fixed_book"}:
                raise ValueError("Bitte eine gültige Buchauswahl wählen.")
            settings_id = UUID(form_values["settings_id"])
            if len(form_values["revision"]) > 64:
                raise ValueError("Die Einstellungsseite ist veraltet. Bitte neu laden.")
            revision = datetime.fromisoformat(form_values["revision"])
            if revision.tzinfo is None or revision.utcoffset() is None:
                raise ValueError("Die Einstellungsseite ist veraltet. Bitte neu laden.")
            fixed_book_id = UUID(form_values["fixed_book_id"]) if form_values["fixed_book_id"] else None
            db = repository or SupabaseRepository(settings)
            await db.update_promotion_settings(settings_id, revision, form_values["mode"], fixed_book_id)
        except ValueError as exc:
            context = await settings_context(
                promotion_form={
                    "mode": form_values.get("mode", "random_book"),
                    "fixed_book_id": form_values.get("fixed_book_id", ""),
                },
                promotion_form_error=str(exc),
            )
            return templates.TemplateResponse(request=request, name="settings.html", context=context, status_code=400)
        except DatabaseError as exc:
            context = await settings_context(
                promotion_form={
                    "mode": form_values.get("mode", "random_book"),
                    "fixed_book_id": form_values.get("fixed_book_id", ""),
                },
                promotion_form_error=str(exc),
            )
            status = 409 if exc.code == "conflict" else 503
            return templates.TemplateResponse(request=request, name="settings.html", context=context, status_code=status)
        return RedirectResponse(
            str(request.url_for("settings")) + "?promotion_saved=true#promotion", status_code=303,
        )

    @app.get("/health", include_in_schema=False)
    async def health():
        connected = False
        try:
            db = repository or SupabaseRepository(settings)
            await db.check_schema(full=True)
            connected = True
        except DatabaseError:
            pass
        return {"status": "ok", "service": "bookpromo", "database_connected": connected}

    @app.post("/settings/openwebui/check", name="check_openwebui")
    async def check_openwebui(request: Request):
        require_local_origin(request)
        if webui_lock.locked(): raise UploadError("Eine Open-WebUI-Prüfung läuft bereits. Bitte kurz warten.", 409)
        async with webui_lock:
            result = await OpenWebUIClient(settings).check()
        return {**result, "message": "Zugang erfolgreich geprüft. Modell auswählen oder die Textantwort testen."}

    @app.post("/settings/comfyui/check", name="check_comfyui")
    async def check_comfyui(request: Request):
        require_local_origin(request)
        if comfy_lock.locked():
            raise UploadError("Eine ComfyUI-Prüfung läuft bereits. Bitte kurz warten.", 409)
        if not settings.reel_image_workflow.is_file() or not settings.reel_video_workflow.is_file():
            raise UploadError("Die beiden Reel-Workflow-Dateien wurden nicht gefunden.", 409)
        try:
            async with comfy_lock:
                await run_in_threadpool(ComfyClient(str(settings.comfyui_url)).check)
        except Exception:
            raise UploadError("ComfyUI ist nicht erreichbar oder lieferte keine gültige Antwort.", 503) from None
        qwen = (
            " Der lokale Qwen-Textworkflow ist ebenfalls vorhanden."
            if settings.comfyui_qwen_workflow.is_file()
            else " Der optionale Qwen-Textworkflow fehlt."
        )
        return {"message": "ComfyUI und beide Reel-Workflows sind erreichbar." + qwen}

    @app.post("/settings/openwebui/model", name="select_openwebui_model")
    async def select_openwebui_model(request: Request):
        require_local_origin(request)
        async with request.form(max_files=0, max_fields=1) as form:
            model = form.get("model")
            if len(form) != 1 or len(form.getlist("model")) != 1 or not isinstance(model, str) or not 1 <= len(model) <= 300:
                raise UploadError("Bitte genau ein Modell aus der verfügbaren Modellliste auswählen.")
        if webui_lock.locked(): raise UploadError("Eine Open-WebUI-Prüfung läuft bereits. Bitte kurz warten.", 409)
        async with webui_lock:
            models = await OpenWebUIClient(settings).list_models()
            if not any(item.id == model for item in models): raise OpenWebUIError("model")
            try:
                await run_in_threadpool(save_model, settings, model)
            except OSError:
                raise UploadError("Das Modell konnte nicht in der ENV-Datei gespeichert werden. Bitte Zugriffsrechte prüfen.", 503) from None
        return {"selected_model": model, "message": "Modell gespeichert und für neue KI-Anfragen übernommen."}

    @app.post("/settings/openwebui/test", name="test_openwebui")
    async def test_openwebui(request: Request):
        require_local_origin(request)
        if webui_lock.locked(): raise UploadError("Eine Open-WebUI-Prüfung läuft bereits. Bitte kurz warten.", 409)
        async with webui_lock:
            await OpenWebUIClient(settings).smoke_test()
        return {"message": "Textantwort erfolgreich geprüft. Es wurden keine Buchtexte übertragen."}

    @app.exception_handler(OpenWebUIError)
    async def openwebui_error(request: Request, exc: OpenWebUIError):
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=503)

    @app.exception_handler(TextAIError)
    async def text_ai_error(request: Request, exc: TextAIError):
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=503)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path == "/uploads":
            message = "Die hochgeladene Datei ist zu groß." if exc.status_code == 413 else "Bitte genau eine gültige Word-Datei auswählen."
            return await upload_error(request, UploadError(message, exc.status_code))
        # Do not echo requested paths, query strings, or arbitrary exception details.
        title = "Seite nicht gefunden" if exc.status_code == 404 else "Aktion nicht verfügbar"
        return templates.TemplateResponse(
            request=request, name="error.html",
            context={"active_page": "", "error_title": title},
            status_code=exc.status_code, headers=exc.headers,
        )

    return app
