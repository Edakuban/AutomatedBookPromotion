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


def test_book_media_download_contains_quotes_selected_image_and_reel(setup):
    settings, uploads, book, record, _ = setup
    management = analyzed(setup).get(book.id)
    quote = next(
        item["quote"] for item in management["quotes"]
        if item["quote"].chapter_id == record.result.chapters[0].id
    )
    app = create_app(settings, start_worker=False)
    url = f"/books/local/{book.id}/media.zip"

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        quotes_only = client.get(url)
        assert quotes_only.status_code == 200
        with ZipFile(BytesIO(quotes_only.content)) as archive:
            text_names = archive.namelist()
            assert len(text_names) == len(management["quotes"])
            assert all(re.fullmatch(r"\d{2}-\d{2}-[a-z0-9-]+\.txt", name)
                       for name in text_names)
            assert sorted(archive.read(name).decode("utf-8") for name in text_names) == sorted(
                item["quote"].text for item in management["quotes"]
            )
        assert "Zitate, Bilder &amp; Reels als ZIP herunterladen" in client.get(
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
        assert len(names) == len(management["quotes"]) + 2
        image_name = next(name for name in names if name.endswith(".png"))
        stem = image_name[:-4]
        assert re.fullmatch(r"01-01-[a-z0-9-]+", stem)
        assert stem + ".txt" in names
        assert stem + ".mp4" in names
        assert archive.read(stem + ".txt").decode("utf-8") == quote.text
        assert archive.read(image_name).startswith(b"\x89PNG")
        assert b"ftypisom" in archive.read(stem + ".mp4")
