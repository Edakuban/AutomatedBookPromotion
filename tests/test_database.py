import asyncio
from datetime import datetime, timezone
import hashlib
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr
import pytest

from bookpromo.cli import main
from bookpromo.config import Settings
from bookpromo.database import SCHEMA_VERSION, DatabaseError, SupabaseRepository
from bookpromo.web import create_app


@pytest.fixture
def settings(monkeypatch, tmp_path):
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
        monkeypatch.delenv(field.lower(), raising=False)
    return Settings(_env_file=None, supabase_enabled=True, app_data_dir=tmp_path / "data",
                    supabase_url="https://example.supabase.co",
                    supabase_secret_key=SecretStr("sb_secret_TEST_PRIVATE"))


def book(**changes):
    value = dict(id=str(uuid4()), title="Ein Buch", author="Autor", active=False,
                 displayed_version_id=None, status="new", chapter_count=0,
                 quote_count=0, last_published_at=None)
    return value | changes


def test_disabled_mode_never_creates_http_client(settings, monkeypatch, tmp_path, capsys):
    settings.supabase_enabled = False
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: pytest.fail("Unexpected network client"))
    with TestClient(create_app(settings, start_worker=False)) as client:
        assert "3.2 deaktiviert" in client.get("/").text
        assert client.get("/health").json()["database_connected"] is False
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_TEST_PRIVATE")
    assert main(["check-db"]) == 2
    assert "deaktiviert" in capsys.readouterr().err


@pytest.mark.parametrize("key,bearer", [("sb_secret_TEST_PRIVATE", False), ("eyJ.legacy.signature", True)])
def test_auth_headers_and_pagination(settings, key, bearer):
    settings.supabase_secret_key = SecretStr(key)
    rows = [book() for _ in range(3)]
    def respond(request):
        assert request.headers["apikey"] == key
        assert ("authorization" in request.headers) is bearer
        if bearer:
            assert request.headers["authorization"] == f"Bearer {key}"
        assert request.url.params["offset"] == "2"
        assert request.url.params["limit"] == "3"
        assert request.method == "GET"
        assert key not in str(request.url)
        return httpx.Response(200, json=rows)
    repo = SupabaseRepository(settings, transport=httpx.MockTransport(respond))
    result, more = asyncio.run(repo.list_books(page=2, page_size=2))
    assert len(result) == 2 and more


@pytest.mark.parametrize("status,code", [(401,"credentials"),(403,"credentials"),(404,"schema"),
                                      (400,"schema"),(429,"unavailable"),(500,"unavailable"),(302,"unavailable")])
def test_remote_errors_do_not_leak_content_or_follow_redirects(settings, status, code):
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(status, json={"message": "PRIVATE_RESPONSE"},
                              headers={"Location": "https://do-not-contact.example"})
    repo = SupabaseRepository(settings, transport=httpx.MockTransport(respond))
    with pytest.raises(DatabaseError) as error:
        asyncio.run(repo.check_schema())
    assert error.value.code == code
    assert "PRIVATE" not in str(error.value)
    assert len(seen) == 1


def test_timeout_is_sanitized(settings):
    def respond(request):
        raise httpx.ReadTimeout("PRIVATE_TIMEOUT_DETAIL", request=request)
    repo = SupabaseRepository(settings, transport=httpx.MockTransport(respond))
    with pytest.raises(DatabaseError, match="nicht erreichbar"):
        asyncio.run(repo.check_schema())


@pytest.mark.parametrize("payload", [[], [{"version":1}], [{"version":True}], {"version":1}, [{"version":1},{"version":1}]])
def test_wrong_or_missing_schema_rejected(settings, payload):
    repo = SupabaseRepository(settings, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    with pytest.raises(DatabaseError):
        asyncio.run(repo.check_schema())


def test_full_check_reads_all_objects_without_writes(settings):
    seen = []
    def respond(request):
        assert request.method == "GET"
        seen.append(request.url.path.rsplit("/",1)[-1])
        return httpx.Response(200, json=[{"version": SCHEMA_VERSION}] if seen[-1] == "bookpromo_schema" else [])
    repo = SupabaseRepository(settings, transport=httpx.MockTransport(respond))
    asyncio.run(repo.check_schema(full=True))
    assert len(seen) == 12
    assert "posts" in seen and "post_media" in seen and "quote_overview" in seen


def test_book_page_escapes_text_and_formats_berlin_time(settings):
    def respond(request):
        if request.url.path.endswith("bookpromo_schema"):
            return httpx.Response(200, json=[{"version": SCHEMA_VERSION}])
        return httpx.Response(200, json=[book(title="<script>bad()</script>", chapter_count=2,
            quote_count=7, last_published_at="2026-09-07T04:00:00Z")])
    repo = SupabaseRepository(settings, transport=httpx.MockTransport(respond))
    with TestClient(create_app(settings, repository=repo, start_worker=False)) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "<script>bad()" not in response.text
        assert "&lt;script&gt;" in response.text
        assert "07.09.2026, 06:00" in response.text
        assert "sb_secret_" not in response.text


def test_invalid_rows_are_not_rendered(settings):
    repo = SupabaseRepository(settings, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=[book(title="PRIVATE_VALUE", chapter_count=-1)])))
    with pytest.raises(DatabaseError) as exc:
        asyncio.run(repo.list_books())
    assert "PRIVATE_VALUE" not in str(exc.value)


def test_promotion_settings_and_eligible_books_are_read(settings):
    settings_id, book_id = uuid4(), uuid4()
    updated_at = "2026-09-13T08:15:00+00:00"

    def respond(request):
        relation = request.url.path.rsplit("/", 1)[-1]
        if relation == "promotion_settings":
            assert request.url.params["limit"] == "2"
            return httpx.Response(200, json=[{
                "id": str(settings_id), "active": True, "mode": "fixed_book",
                "fixed_book_id": str(book_id), "updated_at": updated_at,
            }])
        assert relation == "book_overview"
        assert request.url.params["active"] == "eq.true"
        return httpx.Response(200, json=[{"id": str(book_id), "title": "Sonderbuch", "author": "Autorin"}])

    repo = SupabaseRepository(settings, transport=httpx.MockTransport(respond))
    promotion = asyncio.run(repo.get_promotion_settings())
    books = asyncio.run(repo.list_promotion_books())
    assert promotion is not None and promotion.fixed_book_id == book_id
    assert books[0].title == "Sonderbuch"


def test_promotion_settings_update_is_validated_and_optimistic(settings):
    settings_id, book_id = uuid4(), uuid4()
    revision = datetime(2026, 9, 13, 8, 15, tzinfo=timezone.utc)
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "GET":
            assert request.url.params["id"] == f"eq.{book_id}"
            assert request.url.params["active"] == "eq.true"
            return httpx.Response(200, json=[{"id": str(book_id)}])
        assert request.method == "PATCH"
        assert request.headers["prefer"] == "return=representation"
        assert request.url.params["id"] == f"eq.{settings_id}"
        assert request.url.params["updated_at"] == "eq.2026-09-13T08:15:00+00:00"
        assert request.url.params["select"] == "id,active,mode,fixed_book_id,updated_at"
        assert request.content == ('{"mode":"fixed_book","fixed_book_id":"' + str(book_id) + '"}').encode()
        return httpx.Response(200, json=[{
            "id": str(settings_id), "active": True, "mode": "fixed_book",
            "fixed_book_id": str(book_id), "updated_at": "2026-09-13T08:16:00+00:00",
        }])

    repo = SupabaseRepository(settings, transport=httpx.MockTransport(respond))
    saved = asyncio.run(repo.update_promotion_settings(settings_id, revision, "fixed_book", book_id))
    assert saved.fixed_book_id == book_id and len(requests) == 2


def test_promotion_settings_update_rejects_stale_revision(settings):
    repo = SupabaseRepository(settings, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=[])
    ))
    with pytest.raises(DatabaseError) as exc:
        asyncio.run(repo.update_promotion_settings(
            uuid4(), datetime(2026, 9, 13, tzinfo=timezone.utc), "random_book", None,
        ))
    assert exc.value.code == "conflict"


def test_promotion_selection_can_be_changed_on_settings_page(settings):
    settings_id, book_id = uuid4(), uuid4()
    state = {
        "id": str(settings_id), "active": True, "mode": "random_book",
        "fixed_book_id": None, "updated_at": "2026-09-13T08:15:00+00:00",
    }

    def respond(request):
        relation = request.url.path.rsplit("/", 1)[-1]
        if relation == "bookpromo_schema":
            return httpx.Response(200, json=[{"version": SCHEMA_VERSION}])
        if relation == "promotion_settings":
            if request.method == "PATCH":
                payload = __import__("json").loads(request.content)
                state.update(payload, updated_at="2026-09-13T08:16:00+00:00")
            return httpx.Response(200, json=[state])
        if request.url.params.get("select") == "id":
            return httpx.Response(200, json=[{"id": str(book_id)}])
        return httpx.Response(200, json=[{"id": str(book_id), "title": "Sonderbuch", "author": "Autorin"}])

    repo = SupabaseRepository(settings, transport=httpx.MockTransport(respond))
    with TestClient(create_app(settings, repository=repo, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        page = client.get("/settings")
        assert page.status_code == 200
        assert "Zufällig aus allen aktivierten Büchern" in page.text
        assert "Sonderbuch · Autorin" in page.text
        assert f'name="revision" value="{state["updated_at"]}"' in page.text

        blocked = client.post("/settings/promotion", data={
            "settings_id": str(settings_id), "revision": state["updated_at"],
            "mode": "fixed_book", "fixed_book_id": str(book_id),
        }, headers={"Origin": "https://foreign.example"})
        assert blocked.status_code == 403 and state["mode"] == "random_book"

        saved = client.post("/settings/promotion", data={
            "settings_id": str(settings_id), "revision": state["updated_at"],
            "mode": "fixed_book", "fixed_book_id": str(book_id),
        })
        assert saved.status_code == 200
        assert "Buchauswahl gespeichert" in saved.text
        assert state["mode"] == "fixed_book" and state["fixed_book_id"] == str(book_id)


def test_fixed_promotion_requires_an_eligible_book(settings):
    settings_id = uuid4()
    state = {
        "id": str(settings_id), "active": True, "mode": "random_book",
        "fixed_book_id": None, "updated_at": "2026-09-13T08:15:00+00:00",
    }

    def respond(request):
        relation = request.url.path.rsplit("/", 1)[-1]
        if relation == "bookpromo_schema":
            return httpx.Response(200, json=[{"version": SCHEMA_VERSION}])
        if relation == "promotion_settings":
            return httpx.Response(200, json=[state])
        return httpx.Response(200, json=[])

    repo = SupabaseRepository(settings, transport=httpx.MockTransport(respond))
    with TestClient(create_app(settings, repository=repo, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        response = client.post("/settings/promotion", data={
            "settings_id": str(settings_id), "revision": state["updated_at"],
            "mode": "fixed_book", "fixed_book_id": "",
        })
        assert response.status_code == 400
        assert "muss ein Buch ausgewählt werden" in response.text


def test_upload_overlay_uses_private_write_and_returns_object_path(settings):
    payload = b"\x89PNG\r\n\x1a\nimage"
    book_id = str(uuid4())
    digest = hashlib.sha256(payload).hexdigest()
    object_path = f"{book_id}/{digest}.png"

    def respond(request):
        assert request.method == "POST"
        assert request.url.path == "/storage/v1/object/book-promotion-assets/" + object_path
        assert request.headers["apikey"] == "sb_secret_TEST_PRIVATE"
        assert request.headers["content-type"] == "image/png"
        assert request.headers["x-upsert"] == "true"
        assert request.content == payload
        return httpx.Response(200, json={"Key": object_path})

    repo = SupabaseRepository(settings, transport=httpx.MockTransport(respond))
    url = asyncio.run(repo.upload_overlay(object_path, payload))
    assert url == object_path


def test_upload_carousel_end_slide_uses_jpeg_type_and_digest_path(settings):
    payload = b"\xff\xd8carousel-jpeg\xff\xd9"
    book_id = str(uuid4())
    digest = hashlib.sha256(payload).hexdigest()
    object_path = f"{book_id}/carousel/{digest}.jpg"

    def respond(request):
        assert request.method == "POST"
        assert request.url.path == "/storage/v1/object/book-promotion-assets/" + object_path
        assert request.headers["content-type"] == "image/jpeg"
        assert request.headers["x-upsert"] == "true"
        assert request.content == payload
        return httpx.Response(200, json={"Key": object_path})

    repo = SupabaseRepository(settings, transport=httpx.MockTransport(respond))
    assert asyncio.run(repo.upload_carousel_end_slide(object_path, payload)) == object_path


@pytest.mark.parametrize("kind,path,data", [
    ("overlay", "../outside.png", b"\x89PNG\r\n\x1a\nimage"),
    ("overlay", f"{uuid4()}/{'0' * 64}.png", b"\x89PNG\r\n\x1a\nimage"),
    ("carousel", f"{uuid4()}/carousel/{'0' * 64}.jpg", b"not-a-jpeg"),
])
def test_private_asset_upload_rejects_invalid_path_digest_or_media(settings, kind, path, data):
    repo = SupabaseRepository(settings, transport=httpx.MockTransport(
        lambda request: pytest.fail("Invalid assets must not reach Storage")
    ))
    operation = repo.upload_overlay if kind == "overlay" else repo.upload_carousel_end_slide
    with pytest.raises(ValueError):
        asyncio.run(operation(path, data))
