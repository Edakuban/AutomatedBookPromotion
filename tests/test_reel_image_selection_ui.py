"""Candidate previews must not claim an older selected artifact is the new one."""

from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape
import pytest

from test_reels import new_draft, png_bytes, setup


class CandidateFigures(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.figures = []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "figure" and "reel-image-candidate" in attrs.get("class", ""):
            self.current = {"class": attrs["class"], "text": "", "src": ""}
            self.figures.append(self.current)
        if tag == "img" and self.current is not None:
            self.current["src"] = attrs["src"]

    def handle_endtag(self, tag):
        if tag == "figure":
            self.current = None

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"] += data


def render_workshop(draft):
    templates = Path(__file__).resolve().parents[1] / "src/bookpromo/templates"
    environment = Environment(loader=FileSystemLoader(templates), autoescape=select_autoescape())
    environment.globals['global_ai'] = lambda: {
        'preferences': SimpleNamespace(text_provider='openwebui'),
        'preset': SimpleNamespace(label='FLUX.2 Klein 9B Base'),
        'text_label': 'Open WebUI', 'text_model': 'test',
    }
    return environment.get_template("reel_workshop.html").render(
        draft=draft, book=SimpleNamespace(id=draft.book_id),
        chapter=SimpleNamespace(id="chapter"), quote=SimpleNamespace(id="quote", text="Zitat"),
        management=SimpleNamespace(details=SimpleNamespace(title="Buch")),
        publication_defaults=SimpleNamespace(values=SimpleNamespace(selected=lambda: [])),
        url_for=lambda route, **kwargs: f"/{route}", carousel_crop=(.5, .5, 0),
        characters=[], tracks=[], text_ai_providers=[], job=None,
    )


@pytest.mark.parametrize("source,field", [
    ("scene", "scene"), ("optimized", "optimized"), ("upload", "uploaded"),
])
def test_only_exact_source_path_and_hash_are_marked_selected(setup, source, field):
    _, book_id, reels, _ = setup
    base = new_draft(reels, book_id)
    draft = replace(base, **{
        f"{field}_image_path": "reels/current.png", f"{field}_image_sha256": "a" * 64,
        "selected_image_source": source, "selected_image_path": "reels/current.png",
        "selected_image_sha256": "a" * 64,
    })
    assert draft.image_candidate_is_selected(source)
    assert draft.selected_image_is_current_candidate
    page = render_workshop(draft)
    figures = CandidateFigures(page).figures
    assert len(figures) == 1 and "is-selected" in figures[0]["class"]
    assert "Für Video gewählt" in figures[0]["text"]
    assert f"sha256={'a' * 64}" in figures[0]["src"]

    for updates in (
        {f"{field}_image_path": "reels/new.png"},
        {f"{field}_image_sha256": "b" * 64},
        {f"{field}_image_sha256": None},
        {"selected_image_source": "unknown"},
    ):
        changed = replace(draft, **updates)
        assert not changed.image_candidate_is_selected(source)
        assert not changed.selected_image_is_current_candidate
        figures = CandidateFigures(render_workshop(changed)).figures
        assert len(figures) == 2
        assert "is-selected" not in figures[0]["class"]
        assert "Für Video gewählt" not in figures[0]["text"]
        assert "source=selected" in figures[1]["src"]
        assert "is-selected" in figures[1]["class"]
        assert "frühere Version" in figures[1]["text"]


def test_new_optimized_candidate_preserves_active_version_until_explicit_selection(setup):
    _, book_id, reels, jobs = setup
    draft = new_draft(reels, book_id)
    draft = reels.update_draft(draft.id, draft.revision, image_prompt="A cinematic image")

    def finish_image(candidate, size, now):
        path, digest = reels.save_artifact(draft.id, "image", png_bytes(size), f"{candidate}.png")
        jobs.enqueue(draft.id, "image", {"operation": "scene" if candidate == "scene" else "optimize"})
        jobs.finish(jobs.claim(now=now), result={
            "path": path, "sha256": digest, "candidate": candidate,
        }, now=now + 1)
        return reels.get_draft(draft.id)

    draft = finish_image("scene", (64, 96), 1)
    draft = finish_image("optimized", (66, 98), 3)
    selected = reels.select_image(draft.id, draft.revision, "optimized")
    draft = finish_image("optimized", (68, 100), 5)
    assert draft.selected_image_path == selected.selected_image_path
    assert draft.selected_image_sha256 == selected.selected_image_sha256
    assert not draft.selected_image_is_current_candidate
    page = render_workshop(draft)
    figures = CandidateFigures(page).figures
    assert len(figures) == 3
    assert "source=optimized" in figures[1]["src"]
    assert "Für Video gewählt" not in figures[1]["text"]
    assert "Neue Version · noch nicht gewählt" in figures[1]["text"]
    assert "source=selected" in figures[2]["src"]
    assert "frühere Bildversion bleibt ausgewählt" in page
    assert "Charakteroptimiert · neue Version</option>" in page
    assert "Auswahl übernehmen" in page
    assert reels.get_draft(draft.id) == draft  # Rendering/properties are strictly read-only.

    updated = reels.select_image(draft.id, draft.revision, "optimized")
    assert updated.selected_image_path == draft.optimized_image_path
    assert updated.selected_image_sha256 == draft.optimized_image_sha256
    assert updated.selected_image_is_current_candidate
    figures = CandidateFigures(render_workshop(updated)).figures
    assert len(figures) == 2
    assert "Für Video gewählt" in figures[1]["text"]
    assert "source=selected" not in figures[1]["src"]


def test_no_selected_artifact_has_no_chosen_badge(setup):
    _, book_id, reels, _ = setup
    draft = replace(new_draft(reels, book_id), scene_image_path="reels/new.png", scene_image_sha256="a" * 64)
    assert not draft.selected_image_is_current_candidate
    assert "Für Video gewählt" not in render_workshop(draft)
    assert not draft.image_candidate_is_selected("unknown")
