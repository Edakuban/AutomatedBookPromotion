from html.parser import HTMLParser

from fastapi.testclient import TestClient
from pydantic import SecretStr
import pytest

from bookpromo.config import Settings
from bookpromo.web import create_app


class PageLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in {"a", "link"} and "href" in attributes:
            self.links.append(attributes["href"])
        if tag == "script" and "src" in attributes:
            self.links.append(attributes["src"])


@pytest.fixture
def settings(monkeypatch, tmp_path):
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
        monkeypatch.delenv(field.lower(), raising=False)
    return Settings(_env_file=None, app_data_dir=tmp_path / "data")


def test_navigation_and_assets_work_outside_project_directory(settings, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with TestClient(create_app(settings, start_worker=False)) as client:
        for path, title in (("/", "Bücher"), ("/settings", "Einstellungen")):
            page = client.get(path)
            assert page.status_code == 200
            assert f"<h1>{title}</h1>" in page.text
            assert '<html lang="de">' in page.text
            parser = PageLinks()
            parser.feed(page.text)
            for url in parser.links:
                if not url.startswith("#"):
                    assert client.get(url).status_code == 200, url
        assert client.get("/static/app.css").headers["content-type"].startswith("text/css")
        assert client.get("/health").json() == {
            "status": "ok", "service": "bookpromo", "database_connected": False,
        }
    assert not list(tmp_path.iterdir())


def test_upload_form_is_available(settings):
    with TestClient(create_app(settings, start_worker=False)) as client:
        page = client.get("/").text
        assert "Word-Dokument hier ablegen" in page
        assert 'type="file"' in page
        assert 'action="http://testserver/uploads"' in page
        assert client.post("/", files={"file": ("book.docx", b"not-a-real-book")}).status_code == 405


def test_settings_status_never_exposes_credentials_or_claims_connectivity(settings):
    settings.supabase_url = "https://PRIVATE_HOST.example"
    settings.supabase_secret_key = SecretStr("PRIVATE_SUPABASE_SECRET")
    settings.openwebui_url = "http://PRIVATE_WEBUI_HOST:3000"
    settings.openwebui_api_key = SecretStr("PRIVATE_WEBUI_SECRET")
    settings.openwebui_model = "PRIVATE_MODEL"
    with TestClient(create_app(settings, start_worker=False)) as client:
        for path in ("/", "/settings", "/health", "/missing?key=PRIVATE_QUERY"):
            response = client.get(path)
            assert "PRIVATE_" not in response.text
            assert response.headers["cache-control"] == "no-store"
        page = client.get("/settings").text
        assert page.count("Vollständig eingetragen") == 2
        assert "noch nicht geprüft" in page
        assert "Fehlt" not in page


def test_missing_settings_are_visible(settings):
    with TestClient(create_app(settings, start_worker=False)) as client:
        page = client.get("/settings").text
        assert page.count("Noch einzurichten") == 2
        assert page.count("Fehlt") == 5


@pytest.mark.parametrize("path", ["/.env", "/data/book.docx", "/static/.env", "/static/%2e%2e/config.py"])
def test_local_files_are_not_served(settings, tmp_path, monkeypatch, path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("PRIVATE_FILE_CONTENT")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "book.docx").write_text("PRIVATE_BOOK_CONTENT")
    with TestClient(create_app(settings, start_worker=False)) as client:
        response = client.get(path)
        assert response.status_code == 404
        assert "PRIVATE_" not in response.text
        assert "Zur Buchübersicht" in response.text
