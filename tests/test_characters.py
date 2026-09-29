from io import BytesIO
from types import SimpleNamespace

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from bookpromo.analysis import CharacterSuggestion
from bookpromo.analysis_store import AnalysisStore
from bookpromo.analysis_worker import run_analysis_once
from bookpromo.characters import (
    CharacterStore, character_scene_prompt, order_scene_characters,
    stitch_character_references,
)
from bookpromo.reel_generation import build_reference_edit_prompt
from bookpromo.uploads import LocalUploadStore, UploadError
from bookpromo.web import _detected_character_ids, create_app
from test_analysis import FakeAPI, setup
from test_extraction import p, package


def image_file(color, size=(700, 900)) -> BytesIO:
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    output.seek(0)
    return output


def test_generated_image_prompt_contributes_to_character_preselection():
    characters = [
        SimpleNamespace(id="kira", name="Kira", aliases=("Die Wanderin",)),
        SimpleNamespace(id="lukas", name="Lukas", aliases=()),
    ]
    quote = SimpleNamespace(
        context_before="Sie öffnete das Buch.", text="Die Seiten begannen zu leuchten.",
        context_after="Niemand sonst war im Zimmer.",
    )
    assert _detected_character_ids(
        characters, quote,
        generated_prompt="Photorealistic vertical scene of Kira reading in her room.",
    ) == ["kira"]


def test_character_preselection_and_render_order_follow_first_scene_mention():
    characters = [
        SimpleNamespace(id="clara", name="Clara", aliases=()),
        SimpleNamespace(id="kira", name="Kira", aliases=()),
    ]
    quote = SimpleNamespace(
        context_before="", text="Kira entzündete das Feuer mit Tante Clara.", context_after="",
    )
    assert _detected_character_ids(characters, quote) == ["kira", "clara"]
    assert [item.id for item in order_scene_characters(characters, quote.text)] == ["kira", "clara"]


@pytest.fixture
def character_store(tmp_path):
    uploads = LocalUploadStore(tmp_path / "data", 1024 * 1024)
    book, _ = uploads.save(BytesIO(package(p("Kapitel 1") + p("Kira traf Lukas."))), "Band 1.docx")
    return uploads, book, CharacterStore(uploads)


def test_analysis_merge_is_book_scoped_and_never_overwrites_manual_profiles(character_store):
    _, book, store = character_store
    suggestion = CharacterSuggestion(
        name="Kira", aliases=["Ki"], description="Eine junge Frau mit dunklem Haar.",
        image_prompt="Photorealistic young woman with dark hair.", source_evidence="Im Buch beschrieben.",
    )
    store.merge_analysis(book.id, "run-1", [suggestion])
    character = store.list(book.id)[0]
    assert character.name == "Kira" and character.aliases == ("Ki",)
    assert not character.manual and not character.approved

    edited = store.save(
        book.id, character.id, character.revision, name="Kira", aliases="Ki, Wanderin",
        description="Kira in diesem Band.", image_prompt="Approved Kira portrait.", approved=True,
    )
    store.merge_analysis(book.id, "run-2", [suggestion.model_copy(update={"description": "Andere Beschreibung"})])
    preserved = store.get(book.id, edited.id)
    assert preserved.description == "Kira in diesem Band."
    assert preserved.image_prompt == "Approved Kira portrait."
    assert preserved.analysis_run_id == "run-1"

    with pytest.raises(UploadError, match="bereits"):
        store.create(book.id, name=" kira ")


def test_reference_images_are_normalized_and_stitched_left_to_right(character_store, tmp_path):
    _, book, store = character_store
    kira = store.create(book.id, name="Kira", description="Eine junge Wanderin.")
    lukas = store.create(book.id, name="Lukas", description="Ein großer Mann mit Brille.")
    kira = store.save_reference_file(book.id, kira.id, kira.revision, image_file("#cc2233"))
    lukas = store.save_reference_file(book.id, lukas.id, lukas.revision, image_file("#2255cc"))
    assert kira.has_reference and lukas.has_reference

    sheet, context = stitch_character_references(
        [(kira, store.reference_path(kira)), (lukas, store.reference_path(lukas))],
        tmp_path / "sheet.png",
    )
    with Image.open(sheet) as stitched:
        assert stitched.size == (1024, 640)
        assert stitched.getpixel((256, 320))[0] > stitched.getpixel((256, 320))[2]
        assert stitched.getpixel((768, 320))[2] > stitched.getpixel((768, 320))[0]
    assert "left portrait is Kira" in context
    assert "right portrait is Lukas" in context
    assert "left person is Kira" in context
    assert "right person is Lukas" in context
    assert "Eine junge Wanderin" in context
    assert "Ein großer Mann mit Brille" in context
    assert "Never swap" in context

    effective = character_scene_prompt("A rainy station scene.", [kira, lukas])
    assert effective.startswith("A rainy station scene.")
    assert "1. Kira" in effective and "2. Lukas" in effective
    assert "left person is Kira" in effective and "right person is Lukas" in effective

    with pytest.raises(UploadError, match="mindestens"):
        store.save_reference_file(book.id, kira.id, kira.revision, image_file("white", (100, 100)))


def test_four_detailed_references_fit_reference_workflow_prompt(character_store, tmp_path):
    _, book, store = character_store
    references = []
    for index, name in enumerate(("Alex", "Dex", "Lila", "Voros")):
        character = store.create(
            book.id, name=name, description=(f"Detailed book description {index}. " * 150),
            image_prompt=(f"Detailed visual identity {index}. " * 180),
        )
        character = store.save_reference_file(
            book.id, character.id, character.revision,
            image_file((40 + index * 30, 80, 120)),
        )
        references.append((character, store.reference_path(character)))

    _, context = stitch_character_references(references, tmp_path / "four-sheet.png")
    assert len(context) <= 3_900
    assert all(name in context for name in ("Alex", "Dex", "Lila", "Voros"))
    assert "Do not swap identities" in build_reference_edit_prompt(context)


def test_character_settings_create_edit_upload_and_serve_reference(setup):
    settings, uploads, book, _, _ = setup
    url = f"/books/local/{book.id}/settings"
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        created = client.post(
            url + "/characters",
            data={"name": "Kira", "description": "Die Wanderin.", "image_prompt": "Adult woman."},
            follow_redirects=False,
        )
        assert created.status_code == 303 and created.headers["location"].endswith("#characters")
        character = CharacterStore(uploads).list(book.id)[0]
        saved = client.post(
            url + f"/characters/{character.id}",
            data={"revision": character.revision, "name": "Kira", "aliases": "Ki",
                  "description": "Die Wanderin.", "image_prompt": "Adult woman.", "approved": "on"},
            follow_redirects=False,
        )
        assert saved.status_code == 303
        character = CharacterStore(uploads).get(book.id, character.id)
        uploaded = client.post(
            url + f"/characters/{character.id}/upload",
            data={"revision": str(character.revision)},
            files={"file": ("kira.png", image_file("#885533").getvalue(), "image/png")},
            follow_redirects=False,
        )
        assert uploaded.status_code == 303
        character = CharacterStore(uploads).get(book.id, character.id)
        served = client.get(url + f"/characters/{character.id}/image.png")
        assert served.status_code == 200 and served.headers["content-type"] == "image/png"
        page = client.get(url).text
        assert "Kira" in page and "Freigegeben" in page and "Referenzbild" in page


def test_existing_book_character_analysis_only_extracts_characters_and_block_is_last(setup):
    settings, uploads, book, _, _ = setup
    url = f"/books/local/{book.id}/settings"
    api = FakeAPI()
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        page = client.get(url).text
        assert page.index("Carousel-Schlussseite") < page.index("Charaktere dieses Buches")
        assert "Charaktere aus Buch analysieren" in page

        started = client.post(url + "/character-analysis")
        assert started.status_code == 200
        assert client.get(url + "/character-analysis/status").json()["state"] == "queued"
        assert run_analysis_once(uploads, settings=settings, api_factory=lambda _: api)
        assert [name for name, _, _ in api.calls] == [
            "Summary", "Summary", "BookProfile", "CharacterSuggestions",
        ]
        status = client.get(url + "/character-analysis/status").json()
        assert status["state"] == "done" and not status["active"]
        assert [item.name for item in CharacterStore(uploads).list(book.id)] == ["Mara"]
        page = client.get(url).text
        assert "Mara" in page and "Charaktere aus Buch analysieren" not in page

    result = AnalysisStore(uploads).latest(book.id, purpose="characters", include_result=True)
    assert result["result"].quotes == []
