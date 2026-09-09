import asyncio
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
    assert len(seen) == 11
    assert "posts" in seen and "quote_overview" in seen


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


def test_upload_overlay_uses_private_write_and_returns_object_path(settings):
    payload = b"\x89PNG\r\n\x1a\nimage"

    def respond(request):
        assert request.method == "POST"
        assert request.url.path == "/storage/v1/object/book-promotion-assets/book-id/title.png"
        assert request.headers["apikey"] == "sb_secret_TEST_PRIVATE"
        assert request.headers["content-type"] == "image/png"
        assert request.headers["x-upsert"] == "true"
        assert request.content == payload
        return httpx.Response(200, json={"Key": "book-id/title.png"})

    repo = SupabaseRepository(settings, transport=httpx.MockTransport(respond))
    url = asyncio.run(repo.upload_overlay("book-id/title.png", payload))
    assert url == "book-id/title.png"
