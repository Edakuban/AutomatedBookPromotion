from io import BytesIO
from types import SimpleNamespace

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from bookpromo.analysis import CharacterSuggestion
from bookpromo.analysis_store import AnalysisStore
from bookpromo.analysis_worker import run_analysis_once
from bookpromo.characters import (
    CharacterStore, character_forbidden_features, character_mask_selector,
    character_scene_prompt, order_scene_characters, stitch_character_references,
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


def test_mask_selector_is_generic_and_uses_only_the_reference_prompt(character_store):
    _, book, store = character_store
    character = store.create(
        book.id,
        name="Unit 7",
        description="A plot description that must never drive visual matching.",
        image_prompt=(
            "Unit 7 is a weathered brass automaton with a triangular blue eye and a white spiral shoulder mark. "
            "Full-body reference portrait on a neutral backdrop."
        ),
    )
    selector = character_mask_selector(character)
    assert "brass automaton" in selector
    assert not selector.startswith("Unit 7")
    assert "plot description" not in selector
    assert "neutral backdrop" not in selector
    assert "triangular blue eye" in selector
    assert "white spiral shoulder mark" in selector


def test_mask_selector_keeps_later_visible_traits_and_drops_abstract_title(character_store):
    _, book, store = character_store
    character = store.create(
        book.id, name="Belphegor",
        image_prompt=(
            "Belphegor is the Prince of Sloth, a massive infernal ruler with a bloated, heavy body "
            "and an oppressive demonic presence. His flesh is dark and swollen, and in some places "
            "it seems to dissolve into shadow. Several heavy horns grow from his skull. "
            "His glowing red eyes are the most alert part of him. "
            "Cinematic adult dark fantasy realism, infernal industrial throne room, no text."
        ),
    )
    selector = character_mask_selector(character)
    assert selector.startswith("massive infernal ruler")
    for trait in ("bloated heavy body", "heavy horns", "dark and swollen flesh", "glowing red eyes"):
        assert trait in selector
    for abstract in ("Prince of Sloth", "oppressive", "shadow", "throne room", "most alert"):
        assert abstract not in selector


def test_mask_selector_retains_comma_separated_subject_and_never_turns_negatives_positive(character_store):
    _, book, store = character_store
    character = store.create(
        book.id, name="Samuel",
        image_prompt=(
            "Samuel Hellsworth is a tall, athletic, dark-charismatic man in his early thirties "
            "with a sharp jawline. He has dark brown to black hair, neatly styled back. "
            "His most striking feature is his permanently glowing ruby-red eyes. "
            "He has a lean muscular, battle-hardened body with broad shoulders. "
            "He is still not fully monstrous: no horns, no wings, no fully transformed body. "
            "His clothing includes a black or very dark shirt and fitted dark jacket."
        ),
    )
    selector = character_mask_selector(character)
    assert selector.startswith("tall athletic dark-charismatic man")
    for trait in ("lean muscular battle-hardened body", "dark brown to black hair", "ruby-red eyes"):
        assert trait in selector
    assert "Samuel" not in selector and "Hellsworth" not in selector
    assert "horns" not in selector and "wings" not in selector
    assert len(selector) <= 240


def test_mask_selector_is_generic_preserves_materials_and_ignores_optional_props(character_store):
    _, book, store = character_store
    character = store.create(
        book.id, name="Pearl",
        image_prompt=(
            "Pearl is a small floating beast. Its body is translucent blue glass. "
            "Its fur is opalescent silver. It has curved crystal antlers. "
            "No horns or wings. When transformed, its eyes glow purple. "
            "Small pouches may be attached to its coat. "
            "Full-body reference portrait on a neutral backdrop."
        ),
    )
    selector = character_mask_selector(character)
    assert selector.startswith("small floating beast")
    assert "translucent blue glass body" in selector
    assert "opalescent silver fur" in selector
    assert "curved crystal antlers" in selector
    for unwanted in ("coat", "pouches", "purple", "horns", "wings", "neutral backdrop"):
        assert unwanted not in selector


def test_mask_selector_ignores_contrasted_wardrobe(character_store):
    _, book, store = character_store
    character = store.create(
        book.id, name="Faye",
        image_prompt=(
            "Faye is a powerful human witch. She has medium to long dark brown hair. "
            "She wears practical dark witch clothing rather than ornate fantasy robes. "
            "When she channels power, her eyes ignite with golden flames. "
            "She has no horns or wings."
        ),
    )
    selector = character_mask_selector(character)
    assert "medium to long dark brown hair" in selector
    assert "practical dark witch clothing" in selector
    for unwanted in ("robes", "golden flames", "horns", "wings"):
        assert unwanted not in selector


def test_forbidden_features_extracts_visible_traits_not_rendering_constraints(character_store):
    _, book, store = character_store
    character = store.create(
        book.id,
        name="Rook",
        image_prompt=(
            "Rook is a human-looking mage without horns or wings. "
            "No tail, no fully transformed body, no text, no anime, no watermark."
        ),
    )
    assert character_forbidden_features(character) == ("tail", "horns", "wings")


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
    kira = store.create(
        book.id, name="Kira", description="Eine junge Wanderin.",
        image_prompt="Adult woman with a green hood and a narrow face.",
    )
    lukas = store.create(
        book.id, name="Lukas", description="Ein großer Mann mit Brille.",
        image_prompt="Tall adult man with square glasses and short brown hair.",
    )
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
    assert "green hood and a narrow face" in context
    assert "square glasses and short brown hair" in context
    assert "Eine junge Wanderin" not in context
    assert "Ein großer Mann mit Brille" not in context
    assert "Never swap" in context

    effective = character_scene_prompt("A rainy station scene.", [kira, lukas])
    assert effective.startswith("A rainy station scene.")
    assert "1. Kira" in effective and "2. Lukas" in effective
    assert "left person is Kira" in effective and "right person is Lukas" in effective
    assert "reference-image prompt" in effective
    assert "green hood and a narrow face" in effective
    assert "Eine junge Wanderin" not in effective

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
