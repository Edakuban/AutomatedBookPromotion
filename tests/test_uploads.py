from concurrent.futures import ThreadPoolExecutor
import hashlib
from io import BytesIO
import sqlite3
from uuid import UUID, uuid4
import zipfile

from fastapi.testclient import TestClient
import pytest

from bookpromo.config import Settings
from bookpromo.uploads import LocalUploadStore, UploadError
from bookpromo.web import create_app


def docx_bytes(text="Ein Testbuch.", *, main=None, extra=None):
    """Synthetic minimal OPC package; contains no user book content."""
    members = {
        "[Content_Types].xml": '''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>''',
        "_rels/.rels": '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>''',
        "word/document.xml": main or f'''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>''',
    }
    members.update(extra or {})
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in members.items():
            archive.writestr(name, value)
    return output.getvalue()


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
        monkeypatch.delenv(field.lower(), raising=False)
    return Settings(_env_file=None, app_data_dir=tmp_path / "data", app_max_upload_mb=1)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000", headers={"Accept":"application/json"}) as client:
        yield client


def test_upload_creates_durable_project_and_preserves_original(settings, client):
    original = docx_bytes()
    result = client.post("/uploads", files={"file":("Mein Roman.docx", original)} )
    assert result.status_code == 201, result.text
    payload = result.json()
    assert payload["duplicate"] is False
    book_id = UUID(payload["book_id"])
    store = LocalUploadStore(settings.app_data_dir, 1024 * 1024)
    book = store.get_book(book_id)
    assert book.title == "Mein Roman"
    assert book.file_sha256 == hashlib.sha256(original).hexdigest()
    assert (settings.app_data_dir / book.source_path).read_bytes() == original
    assert UUID(book.version_id) != UUID(book.job_id)
    with TestClient(create_app(settings, start_worker=False)) as restarted:
        assert "Mein Roman" in restarted.get("/").text
        detail = restarted.get(payload["url"])
        assert detail.status_code == 200
        assert "Import im Hintergrund" in detail.text
        assert "Noch nicht an Supabase übertragen" in detail.text
    assert not list((settings.app_data_dir / "pending").iterdir())


def test_duplicate_and_renamed_file_open_same_book(client, settings):
    original = docx_bytes()
    first = client.post("/uploads", files={"file":("Roman.docx",original)}).json()
    second = client.post("/uploads", files={"file":("Anderer Name.docx",original)})
    assert second.status_code == 200 and second.json()["duplicate"] is True
    assert first["book_id"] == second.json()["book_id"]
    assert len(list((settings.app_data_dir / "originals").iterdir())) == 1


def test_local_books_are_sorted_by_title_case_insensitively(settings):
    store = LocalUploadStore(settings.app_data_dir, 1024 * 1024)
    for index, name in enumerate(("Zeta.docx", "beta.docx", "Ähre.docx", "Alpha.docx")):
        store.save(BytesIO(docx_bytes(f"Inhalt {index}")), name)

    first_page, more = store.list_books(page=1, page_size=2)
    second_page, second_more = store.list_books(page=2, page_size=2)

    assert [book.title for book in first_page + second_page] == ["Alpha", "Ähre", "beta", "Zeta"]
    assert more and not second_more


def test_concurrent_duplicate_is_stored_once(settings):
    def save(_):
        return LocalUploadStore(settings.app_data_dir, 1024 * 1024).save(BytesIO(docx_bytes()), "Roman.docx")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, range(2)))
    assert results[0][0].id == results[1][0].id
    assert sorted(result[1] for result in results) == [False, True]


@pytest.mark.parametrize("name,data,status", [
    ("Buch.pdf", b"PDF", 415), ("Buch.docx", b"not a zip", 400), ("Leer.docx", b"", 400),
    ("Zu groß.docx", b"x" * (1024 * 1024 + 1), 413),
    ("XML.docx", docx_bytes(main="<broken>"), 400),
    ("Entity.docx", docx_bytes(main='<!DOCTYPE a [<!ENTITY x SYSTEM "file:///private">]><a>&x;</a>'), 400),
    ("Makro.docx", docx_bytes(extra={"word/vbaProject.bin": b"macro"}), 415),
], ids=["wrong-format", "not-zip", "empty", "oversize", "bad-xml", "xml-entity", "macro"])
def test_invalid_uploads_are_rejected_without_saved_book(client, settings, name, data, status):
    response = client.post("/uploads", files={"file":(name,data)})
    assert response.status_code == status, response.text
    assert "error" in response.json()
    assert LocalUploadStore(settings.app_data_dir, 1024 * 1024).list_books()[0] == []
    assert not list(settings.app_data_dir.glob("originals/*"))
    assert not list(settings.app_data_dir.glob("pending/*"))


def test_filename_does_not_control_storage_path_or_html(client, settings):
    response = client.post("/uploads", files={"file":(r'..\..\<img src=x onerror=alert(1)>.docx', docx_bytes())})
    assert response.status_code == 201
    detail = client.get(response.json()["url"])
    assert "<img src=x" not in detail.text
    assert "&lt;img" in detail.text
    files = list(settings.app_data_dir.glob("originals/*.docx"))
    assert len(files) == 1
    UUID(files[0].stem)


def test_multiple_files_and_foreign_origins_rejected(client):
    response = client.post("/uploads", files=[("file",("a.docx",docx_bytes())),("file",("b.docx",docx_bytes()))])
    assert response.status_code == 400
    for origin in ("https://foreign.example", "null", "http://["):
        response = client.post("/uploads", files={"file":("a.docx",docx_bytes())}, headers={"Origin":origin})
        assert response.status_code == 403


def test_plain_form_redirects_to_book(client):
    response = client.post("/uploads", files={"file":("Roman.docx",docx_bytes())},
                           headers={"Accept":"text/html"}, follow_redirects=False)
    assert response.status_code == 303
    assert "/books/local/" in response.headers["location"]


def test_large_request_is_limited_even_without_content_length(client):
    def chunks():
        yield b'--test\r\nContent-Disposition: form-data; name="file"; filename="large.docx"\r\n\r\n'
        for _ in range(4):
            yield b"x" * (1024 * 1024)
        yield b'\r\n--test--\r\n'
    response = client.post("/uploads", content=chunks(), headers={"Content-Type":"multipart/form-data; boundary=test"})
    assert response.status_code == 413, response.text
    assert "error" in response.json()


def test_missing_book_and_original_not_served(client, settings):
    result = client.post("/uploads", files={"file":("Roman.docx",docx_bytes())}).json()
    book = LocalUploadStore(settings.app_data_dir, 1024 * 1024).get_book(UUID(result["book_id"]))
    assert client.get("/data/" + book.source_path).status_code == 404
    assert client.get(f"/books/local/{uuid4()}").status_code == 404


def test_delete_book_requires_idle_project_and_removes_all_local_data(client, settings):
    result = client.post("/uploads", files={"file": ("Roman.docx", docx_bytes())}).json()
    book_id = result["book_id"]
    store = LocalUploadStore(settings.app_data_dir, 1024 * 1024)
    book = store.get_book(UUID(book_id))
    page = client.get(result["url"])
    assert "Danger Zone" in page.text
    assert "Bereits synchronisierte Daten in Supabase bleiben erhalten" in page.text
    assert "data-delete-book-open disabled" in page.text

    blocked = client.post(result["url"] + "/delete", follow_redirects=False)
    assert blocked.status_code == 409
    assert store.get_book(UUID(book_id)) is not None

    asset_directory = settings.app_data_dir / "book-assets" / book_id
    asset_directory.mkdir(parents=True)
    (asset_directory / "cover.png").write_bytes(b"local-image")
    overlay = settings.app_data_dir / "overlays" / f"{book_id}.png"
    overlay.parent.mkdir(parents=True)
    overlay.write_bytes(b"local-overlay")
    with sqlite3.connect(store.db_path) as connection, connection:
        connection.execute("update local_import_jobs set state='done' where book_id=?", (book_id,))
        connection.execute("create table local_analysis_runs (id text primary key, book_id text, state text)")
        connection.execute("create table local_analysis_checkpoints (run_id text, step_key text, payload_json text)")
        connection.execute("insert into local_analysis_runs values ('run',?,'done')", (book_id,))
        connection.execute("insert into local_analysis_checkpoints values ('run','step','{}')")
        connection.execute("create table local_book_assets (book_id text)")
        connection.execute("insert into local_book_assets values (?)", (book_id,))
        connection.execute("create table local_book_settings (book_id text, revision integer, details_json text, updated_at real)")
        connection.execute("insert into local_book_settings values (?,1,'{}',0)", (book_id,))
        connection.execute("create table local_extractions (book_id text)")
        connection.execute("insert into local_extractions values (?)", (book_id,))
        connection.execute("create table local_quote_controls (book_id text, source_key text, blocked integer, revision integer, updated_at real)")
        connection.execute("insert into local_quote_controls values (?,'quote',0,1,0)", (book_id,))
        connection.execute("create table local_sync_receipts (book_id text)")
        connection.execute("insert into local_sync_receipts values (?)", (book_id,))

    deleted = client.post(result["url"] + "/delete", follow_redirects=False)
    assert deleted.status_code == 303
    assert deleted.headers["location"].endswith("/?deleted=true")
    assert store.get_book(UUID(book_id)) is None
    assert not (settings.app_data_dir / book.source_path).exists()
    assert not asset_directory.exists()
    assert not overlay.exists()
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("pragma integrity_check").fetchone()[0] == "ok"
        for table in ("local_analysis_runs", "local_book_assets", "local_book_settings", "local_extractions", "local_import_jobs", "local_quote_controls", "local_sync_receipts"):
            assert connection.execute(f"select count(*) from {table} where book_id=?", (book_id,)).fetchone()[0] == 0
        assert connection.execute("select count(*) from local_analysis_checkpoints").fetchone()[0] == 0
    confirmation = client.get(deleted.headers["location"])
    assert "vollständig gelöscht" in confirmation.text
    assert "Supabase wurde nicht verändert" in confirmation.text


def test_delete_book_rejects_foreign_origin(client):
    result = client.post("/uploads", files={"file": ("Roman.docx", docx_bytes())}).json()
    response = client.post(result["url"] + "/delete", headers={"Origin": "https://foreign.example"})
    assert response.status_code == 403


def test_failed_storage_cleans_up_files(settings, monkeypatch):
    import bookpromo.uploads as module
    monkeypatch.setattr(module.sqlite3, "connect", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("PRIVATE_PATH")))
    store = LocalUploadStore(settings.app_data_dir, 1024 * 1024)
    with pytest.raises(OSError):
        store.save(BytesIO(docx_bytes()), "Roman.docx")
    assert not list(settings.app_data_dir.glob("pending/*"))
