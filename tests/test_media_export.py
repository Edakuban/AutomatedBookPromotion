from io import BytesIO
import re
from zipfile import ZipFile

from fastapi.testclient import TestClient

from bookpromo.management import quote_key
from bookpromo.media_export import path_slug
from bookpromo.reels import ReelJobStore, ReelStore
from bookpromo.web import create_app
from test_analysis import setup
from test_management import analyzed
from test_reels import png_bytes, wav_bytes


def test_path_slug_is_short_ascii_and_filesystem_safe():
    assert path_slug("Über Größe & Glück: ein Zitat?", max_length=24) == "ueber-groesse-glueck-ein"
    assert path_slug("...", max_length=24) == "zitat"


def test_book_media_download_contains_current_selected_image_and_reel(setup):
    settings, uploads, book, record, _ = setup
    management = analyzed(setup).get(book.id)
    quote = next(
        item["quote"] for item in management["quotes"]
        if item["quote"].chapter_id == record.result.chapters[0].id
    )
    app = create_app(settings, start_worker=False)
    url = f"/books/local/{book.id}/media.zip"

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        empty = client.get(url, headers={"Accept": "application/json"})
        assert empty.status_code == 409
        assert "noch keine Bilder oder Reels" in empty.json()["error"]
        assert "Bilder &amp; Reels als ZIP herunterladen" in client.get(
            f"/books/local/{book.id}"
        ).text

    reels = ReelStore(uploads)
    draft = reels.get_or_create_draft(
        book.id, quote_key(book.version_id, quote), management["suggestion_id"],
        quote.id, quote.text,
    )
    draft = reels.save_uploaded_image(draft.id, draft.revision, png_bytes(), "auswahl.png")
    draft = reels.select_image(draft.id, draft.revision, "upload")
    track, _ = reels.save_audio(book.id, wav_bytes(12), "musik.wav")
    draft = reels.update_draft(
        draft.id, draft.revision, audio_track_id=track.id, audio_start_ms=0,
        video_prompt="The camera slowly moves towards the subject.",
    )

    jobs = ReelJobStore(reels)
    jobs.enqueue(draft.id, "video")
    job = jobs.claim(kinds={"video"})
    video_path, video_hash = reels.save_artifact(
        draft.id, "video", BytesIO(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32), "reel.mp4",
    )
    assert jobs.finish(job, result={"path": video_path, "sha256": video_hash})

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.get(url)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "testbuch-medien.zip" in response.headers["content-disposition"]
    with ZipFile(BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert len(names) == 2
        assert re.fullmatch(r"01-01-[a-z0-9-]+\.png", names[0])
        assert names[1] == names[0][:-4] + ".mp4"
        assert archive.read(names[0]).startswith(b"\x89PNG")
        assert b"ftypisom" in archive.read(names[1])
