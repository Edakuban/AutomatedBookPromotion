"""Regression checks for information grouping without losing existing controls."""

from html.parser import HTMLParser
from io import BytesIO
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from PIL import Image

from bookpromo.book_assets import BookAssetStore
from bookpromo.database import BookSummary, DatabaseError
from bookpromo.management import ManagementStore
from bookpromo.web import create_app
from test_analysis import setup
from test_management import analyzed


class UIContracts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sections = []
        self.forms = []
        self.inputs = []
        self.ids = []
        self.in_form = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if "data-settings-section" in attrs:
            self.sections.append(attrs)
        if tag == "form":
            assert not self.in_form, "nested forms must not lose controls"
            self.in_form = True
            self.forms.append(attrs)
        if tag in {"input", "select", "textarea"}:
            self.inputs.append(attrs)

    def handle_endtag(self, tag):
        if tag == "form":
            self.in_form = False


def parse(html):
    parser = UIContracts()
    parser.feed(html)
    assert not parser.in_form
    assert len(parser.ids) == len(set(parser.ids)), "section navigation requires unique IDs"
    return parser


def test_grouped_settings_keep_complete_save_and_no_js_access(setup):
    settings, _, book, _, _ = setup
    analyzed(setup)
    with TestClient(create_app(settings, start_worker=False)) as client:
        response = client.get(f"/books/local/{book.id}/settings")
    assert response.status_code == 200
    page = parse(response.text)
    assert {section["data-settings-section"] for section in page.sections} == {
        "book-data", "profile", "art-direction", "media", "characters", "publishing",
    }
    assert all("hidden" not in section for section in page.sections)
    assert 'data-unsaved-form' in response.text and 'data-unsaved-status' in response.text
    assert 'unsaved_changes.js' in response.text
    assert len([form for form in page.forms if form.get("class") == "book-settings-form"]) == 1
    names = {field.get("name") for field in page.inputs}
    assert {
        "revision", "suggestion_id", "profile_run_id", "title", "author", "target_url",
        "publication_mode", "promotion_enabled", "genre", "mood", "internal_summary",
        "world", "characters", "spoilers", "image_prompt_base", "caption_guidelines",
        "carousel_end_text", "overlay_title_text", "overlay_title_font", "overlay_title_color",
        "carousel_background_top_color", "carousel_background_bottom_color",
    } <= names


def test_chapter_sidebar_preserves_quote_actions_and_evidence(setup):
    settings, _, book, record, _ = setup
    current = analyzed(setup).get(book.id)
    quote = current["quotes"][0]["quote"]
    with TestClient(create_app(settings, start_worker=False)) as client:
        response = client.get(f"/books/local/{book.id}/chapters/{quote.chapter_id}")
    assert response.status_code == 200
    page = parse(response.text)
    for chapter in record.result.chapters:
        assert f"/books/local/{book.id}/chapters/{chapter.id}" in response.text
    assert "Bewertung, Fundstelle" in response.text
    assert "Originalkontext" in response.text and "Wortgetreu geprüft" in response.text
    assert "Bild für Zitat-Carousels vorbereiten" in response.text
    assert "Medien-Werkstatt öffnen" in response.text
    assert any(form.get("action", "").endswith(f"/quotes/{quote.id}/block") for form in page.forms)
    assert f'href="http://testserver/books/local/{book.id}/chapters/{quote.chapter_id}/quotes/{quote.id}/reel"' in response.text


def test_library_deduplicates_only_matching_ids_and_retains_remote_status(setup):
    settings, _, book, _, _ = setup
    common = dict(author="Remote Author", active=True, displayed_version_id=None,
                  status="ready", chapter_count=7, quote_count=23, last_published_at=None)
    matching = BookSummary(id=UUID(book.id), title="Remote matching title", **common)
    remote_only = BookSummary(id=uuid4(), title=book.title, **common)

    class Repository:
        async def check_schema(self):
            pass

        async def list_books(self, page):
            return [matching, remote_only], True

    with TestClient(create_app(settings, repository=Repository(), start_worker=False)) as client:
        response = client.get("/")
    assert response.status_code == 200
    parse(response.text)
    assert response.text.count('data-library-kind="local"') == 1
    assert response.text.count('data-library-kind="remote"') == 1
    assert "7 Kapitel · 23 Zitate" in response.text
    assert "Remote Author" in response.text and "Letzter Post:" in response.text
    assert "page=2" in response.text


def test_library_shows_remote_activation_and_last_post_without_empty_panel(setup):
    settings, _, book, _, _ = setup

    class Repository:
        active = True

        async def check_schema(self):
            pass

        async def list_books(self, page):
            return [BookSummary(id=UUID(book.id), title=book.title, author="Author",
                active=self.active, displayed_version_id=None, status="ready",
                chapter_count=7, quote_count=23,
                last_published_at=datetime(2026, 10, 3, 7, 11, tzinfo=timezone.utc))], False

    repo = Repository()
    with TestClient(create_app(settings, repository=repo, start_worker=False)) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Supabase: Promotion aktiv" in page.text
        assert "Lokale Auswahl: Pausiert · Telegram-Freigabe" in page.text
        assert "03.10.2026, 09:11" in page.text
        assert 'aria-labelledby="library-title"' not in page.text
        assert "Alle Bücher dieser Supabase-Seite" not in page.text
        assert "7 Kapitel · 23 Zitate" in page.text
        repo.active = False
        assert "Supabase: Promotion pausiert" in client.get("/").text


def test_library_shows_auto_flow_in_local_promotion_summary(setup):
    settings, uploads, book, _, _ = setup
    store = ManagementStore(uploads)
    current = store.get(book.id)
    store.save(
        book.id, current["revision"],
        current["details"].model_copy(update={"publication_mode": "auto"}),
        current["suggestion_id"],
    )

    with TestClient(create_app(settings, start_worker=False)) as client:
        page = client.get("/")
    assert page.status_code == 200
    assert "Lokale Auswahl: Pausiert · Auto-Flow" in page.text


def test_library_keeps_connection_error_visible_without_hiding_local_books(setup):
    settings, _, book, _, _ = setup

    class Repository:
        async def check_schema(self):
            raise DatabaseError("unavailable")

    with TestClient(create_app(settings, repository=Repository(), start_worker=False)) as client:
        page = client.get("/")
    assert page.status_code == 200
    assert book.title in page.text
    assert "Supabase-Status unbekannt" in page.text
    assert "Supabase-Status gerade nicht verfügbar" in page.text
    assert "derzeit nicht erreichbar" in page.text


def test_library_shows_saved_cover_and_keeps_placeholder_without_cover(setup):
    settings, uploads, book, _, _ = setup
    assets = BookAssetStore(uploads)
    with TestClient(create_app(settings, start_worker=False)) as client:
        before = client.get("/")
        assert 'class="book-monogram"' in before.text
        assert 'data-book-cover' not in before.text

        encoded = BytesIO()
        Image.new("RGB", (800, 1200), "navy").save(encoded, format="PNG")
        encoded.seek(0)
        cover = assets.save(book.id, "cover_front", encoded, "cover.png", 0)
        response = client.get("/")
        assert response.status_code == 200
        parse(response.text)
        assert f'/books/local/{book.id}/settings/assets/cover_front.png?revision=1' in response.text
        assert 'data-book-cover' in response.text and 'loading="lazy"' in response.text
        assert client.get(f"/books/local/{book.id}/settings/assets/cover_front.png").status_code == 200

        assets.path(cover).unlink()
        missing = client.get("/")
        assert missing.status_code == 200
        assert 'data-book-cover' not in missing.text
        assert book.title in missing.text and 'class="book-monogram"' in missing.text
