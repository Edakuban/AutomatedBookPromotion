from io import BytesIO
from types import SimpleNamespace

from PIL import Image
import pytest

from bookpromo.management import BookDetails
from bookpromo.overlay import OverlayError, OverlayStore, font_names, installed_fonts, render_title_overlay, resolve_font


def available_font():
    fonts = installed_fonts()
    if not fonts:
        pytest.skip("No local fonts are available for overlay rendering")
    name, path = next(iter(fonts.items()))
    return name, path


def test_title_overlay_is_instagram_portrait_transparent_and_deterministic():
    name, path = available_font()
    details = BookDetails(title="Verzerrte Wahrheit", overlay_title_font=name, overlay_title_color="#102030")
    fonts = {name: path}
    first = render_title_overlay(details, fonts=fonts)
    second = render_title_overlay(details, fonts=fonts)
    assert first is not None and first.digest == second.digest and first.data == second.data
    image = Image.open(BytesIO(first.data))
    assert image.mode == "RGBA" and image.size == (1080, 1350)
    bounds = image.getchannel("A").getbbox()
    assert bounds is not None
    assert bounds[0] >= 72 and bounds[2] <= 872
    assert bounds[1] >= 72 and bounds[3] <= 312


def test_overlay_store_writes_and_reloads_local_preview(tmp_path):
    name, path = available_font()
    asset = render_title_overlay(BookDetails(title="Buch", overlay_title_font=name), fonts={name: path})
    assert asset is not None
    store = OverlayStore(SimpleNamespace(root=tmp_path))
    stored = store.save("8af1df25-c4c1-49d6-9b0f-80ea3b783c9e", asset)
    assert stored.is_file() and store.preview_exists("8af1df25-c4c1-49d6-9b0f-80ea3b783c9e")
    assert store.load("8af1df25-c4c1-49d6-9b0f-80ea3b783c9e") == asset


def test_unknown_font_is_not_resolved_from_the_system():
    details = BookDetails(title="Buch", overlay_title_font="definitely not a font")
    with pytest.raises(OverlayError):
        render_title_overlay(details, fonts={})


def test_font_picker_uses_font_family_names_and_resolves_them():
    fonts = installed_fonts()
    if not fonts:
        pytest.skip("No local fonts are available for overlay rendering")
    names = font_names(fonts)
    assert names and all("_" not in name for name in names)
    assert resolve_font(names[0], fonts).is_file()
