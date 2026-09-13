"""Local DOCX uploads and optional read-only Supabase overview."""

from pathlib import Path
from contextlib import asynccontextmanager
import asyncio
from datetime import datetime
import sqlite3
from uuid import UUID
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
from .management import BookDetails, FILTERS, ManagementStore
from .overlay import OverlayError, OverlayStore, font_names
from .book_assets import BookAssetStore, MAX_ASSET_BYTES
from .carousel_end_slide import render_book_carousel_end_slide
from .sync import SyncStore
from pydantic import ValidationError

PACKAGE_DIR = Path(__file__).resolve().parent


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
    )
    local_store = LocalUploadStore(settings.app_data_dir, max_bytes)
    extraction_store = ExtractionStore(local_store)
    job_store = JobStore(local_store)
    analysis_store = AnalysisStore(local_store)
    management_store = ManagementStore(local_store)
    overlay_store = OverlayStore(local_store)
    asset_store = BookAssetStore(local_store)
    sync_store = SyncStore(local_store)
    sync_lock = asyncio.Lock()
    webui_lock = asyncio.Lock()
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
                    local_page: int = Query(default=1, ge=1, le=100000)):
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
                "show_remote": settings.supabase_enabled or repository is not None,
            },
        )

    def require_local_origin(request: Request):
        origin = request.headers.get("origin")
        if (request.url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or request.headers.get("sec-fetch-site") == "cross-site"
            or (origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}")):
            raise UploadError("Bitte diese Aktion direkt in der lokalen Book-Promotion-Oberfläche ausführen.", 403)

    async def book_settings_context(book, management, details, *, saved=False, form_error=None,
                                    profile_run_id="", asset_saved=""):
        assets = await run_in_threadpool(asset_store.list, str(book.id))
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
                "carousel_preview": carousel_preview}

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
            management = await run_in_threadpool(management_store.get, str(book_id)) if book else None
            sync_receipt = await run_in_threadpool(sync_store.receipt, str(book_id))
        except (OSError, sqlite3.Error):
            raise UploadError("Das lokale Buchprojekt konnte nicht gelesen werden.", 503) from None
        if book is None:
            raise HTTPException(404)
        return templates.TemplateResponse(request=request, name="local_book.html",
            context={"active_page": "books", "book": book, "record": record, "job": jobs.get(str(book_id)),
                     "analysis": analysis, "analysis_configured": not settings.missing_for("openwebui"),
                     "management": management,
                     "sync_receipt": sync_receipt, "sync_enabled": settings.supabase_enabled,
                     "chapter_blocked_counts": {c.id: sum(q["blocked"] and q["quote"].chapter_id == c.id for q in management["quotes"])
                         for c in record.result.chapters} if management and record and record.result else {},
                     "chapter_quote_counts": {c.id: sum(q["usable"] and q["quote"].chapter_id == c.id for q in management["quotes"])
                         for c in record.result.chapters} if analysis and analysis["result"] and record and record.result else {},
                     "result": record.result if record else None})

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
            if await run_in_threadpool(local_store.get_book, book_id) is None: raise HTTPException(404)
            options = AnalysisOptions(chunk_chars=settings.analysis_chunk_chars, min_score=settings.analysis_min_score,
                max_quotes_per_chapter=settings.analysis_max_quotes_per_chapter, max_calls=settings.analysis_max_calls)
            await run_in_threadpool(analysis_store.enqueue, str(book_id), settings, options)
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
        for item in items: item['usage']=usage.get(item['quote'].id)
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

    @app.get("/books/local/{book_id}/settings", response_class=HTMLResponse, name="book_settings")
    async def book_settings(request: Request, book_id: UUID, preview: Literal["saved", "ai"] = "saved", saved: bool = False,
                            asset_saved: Literal["", "cover_front", "logo"] = ""):
        try:
            book = await run_in_threadpool(local_store.get_book, book_id)
            if book is None: raise HTTPException(404)
            management = await run_in_threadpool(management_store.get, str(book_id), prefer_ai=preview == "ai")
        except (OSError, sqlite3.Error):
            raise UploadError("Die Bucheinstellungen konnten nicht gelesen werden.", 503) from None
        context = await book_settings_context(book, management, management["details"], saved=saved, asset_saved=asset_saved)
        return templates.TemplateResponse(request=request, name="book_settings.html", context=context)

    @app.post("/books/local/{book_id}/settings", name="save_book_settings")
    async def save_book_settings(request: Request, book_id: UUID):
        require_local_origin(request)
        try:
            book = await run_in_threadpool(local_store.get_book, book_id)
            if book is None: raise HTTPException(404)
            async with request.form(max_files=0, max_fields=24) as form:
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
                    form_error="Bitte Titel, Feldlängen, Freigabemodus, Schriftname, Titelfarbe und eine vollständige HTTP-/HTTPS-Zieladresse ohne Zugangsdaten prüfen. Deine Eingaben wurden noch nicht gespeichert.")
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
                management = await run_in_threadpool(management_store.get, str(book_id))
                management.update(revision=revision, suggestion_id=suggestion_id)
                context = await book_settings_context(book, management, details, profile_run_id=profile_run_id,
                    form_error="Für die Promotion bitte zuerst: " + ", ".join(missing) + ".")
                return templates.TemplateResponse(request=request, name="book_settings.html", status_code=400, context=context)
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
        return RedirectResponse(str(request.url_for("book_settings", book_id=book_id)) + "?saved=true", status_code=303)

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
            if await run_in_threadpool(local_store.get_book, book_id) is None: raise HTTPException(404)
            options = AnalysisOptions(purpose="profile", chunk_chars=settings.analysis_chunk_chars,
                                      max_calls=settings.analysis_max_calls)
            run_id = await run_in_threadpool(analysis_store.enqueue, str(book_id), settings, options)
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

    async def settings_context(*, promotion_saved=False, promotion_form=None, promotion_form_error=None):
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
            )
        ]
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
        }

    @app.get("/settings", response_class=HTMLResponse, name="settings")
    async def settings_page(request: Request, promotion_saved: bool = False):
        return templates.TemplateResponse(
            request=request, name="settings.html",
            context=await settings_context(promotion_saved=promotion_saved),
        )

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
