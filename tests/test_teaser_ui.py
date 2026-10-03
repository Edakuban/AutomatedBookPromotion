"""Small, side-effect-free checks for the chapter teaser page's HTML contract."""

from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace as NS

from jinja2 import Environment, FileSystemLoader, select_autoescape


class FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            assert self.current is None, "HTML contains nested forms"
            self.current = {"attrs": attrs, "controls": []}
            self.forms.append(self.current)
        elif self.current is not None and tag in {"input", "button", "select", "textarea"}:
            self.current["controls"].append((tag, attrs))

    def handle_endtag(self, tag):
        if tag == "form":
            assert self.current is not None
            self.current = None


def render_teaser(**overrides):
    templates = Path(__file__).parents[1] / "src" / "bookpromo" / "templates"
    environment = Environment(loader=FileSystemLoader(templates), autoescape=select_autoescape())
    context = dict(
        url_for=lambda name, **kwargs: "/static/" + kwargs["path"] if name == "static" else "/" + name,
        book=NS(id="book", title="Roman"), active_page="books",
        tracks=[], rows=[], project=None, chapter_run=None, render_job=None,
        chapter_teaser_configured=False, chapter_teaser_providers=[],
        chapter_production_active=False, chapter_media_active=False,
        optimizable_count=0, reference_workflow_configured=False,
        chapter_video_configured=False, can_start_chapter_videos=False,
        complete_teaser_ready=False, estimated_duration_ms=0,
        record=NS(result=NS(chapters=[NS(position=1, title="Auftakt")])),
        management=NS(details=NS(title="Roman")),
        publication_defaults=NS(values=NS(
            selected=lambda: ["youtube", "instagram", "facebook"],
            platform=lambda platform: NS(account_id=platform + "-account"),
        )),
        book_teaser_queue_configured=False, book_teaser_queue_platforms=["youtube"],
        chapter_queue_configured=False, supabase_enabled=False, book_synced=False,
        chapter_queue_schema_ready=False,
    )
    context.update(overrides)
    html = environment.get_template("book_teaser.html").render(**context)
    parser = FormParser()
    parser.feed(html)
    assert parser.current is None
    return html, parser.forms


def test_empty_teaser_page_keeps_phase_buttons_visible_but_disabled():
    html, forms = render_teaser()
    for route in (
        "optimize_all_chapter_teaser_images", "start_chapter_teaser_videos",
        "render_complete_book_teaser",
    ):
        form = next(item for item in forms if item["attrs"].get("action") == "/" + route)
        assert any(tag == "button" and "disabled" in attrs for tag, attrs in form["controls"])
    assert '<details class="panel teaser-advanced next-settings">' in html
    assert '<dialog id="chapter-image-lightbox"' in html
    assert 'page_position.js' in html


def test_chapter_prompt_dialog_escapes_content_and_uses_revision_fencing():
    draft = NS(
        revision=7, image_prompt='Bedroom </textarea><script>alert("x")</script>',
        caption_addition="Sam erwacht.", video_prompt="A slow camera move.",
        image_stale=False, video_stale=False,
    )
    plan = dict(
        chapter_id="chapter", position=1, state="analyzed", draft=draft,
        suggestion=NS(scene_summary="Eine ruhige Szene.", teaser_text="Alt"),
        image_sources=[dict(id="scene", label="Szenenbild", selected=True)],
        can_select_image=True, can_edit_prompt=True, can_start_video=True,
        media_active=False, can_regenerate_image=True, can_configure_characters=False,
        has_character_references=False,
    )
    run = dict(
        state="done", plans=[plan], label="Bilder bereit", provider="openwebui",
        model_id="test", completed=1, total=1, stage="Bereit", transition_ms=500,
        audio_track_id="song", images_ready=True,
    )
    html, forms = render_teaser(chapter_run=run, can_start_chapter_videos=True)
    assert '&lt;/textarea&gt;&lt;script&gt;' in html
    assert '<script>alert' not in html
    assert 'data-teaser-image-open' in html
    assert 'data-page-position-anchor="chapter-chapter"' in html
    assert 'aria-labelledby="chapter-prompt-title-chapter"' in html
    editor = next(form for form in forms if form["attrs"].get("action") == "/save_chapter_teaser_prompt")
    controls = {attrs.get("name"): attrs for _, attrs in editor["controls"] if attrs.get("name")}
    assert controls["revision"]["value"] == "7"
    assert set(controls) == {"revision", "image_prompt", "teaser_text", "video_prompt"}
    assert controls["teaser_text"]["maxlength"] == "600"
    video = next(form for form in forms if form["attrs"].get("action") == "/start_chapter_teaser_video")
    assert any(attrs.get("name") == "revision" for _, attrs in video["controls"])
    assert any(attrs.get("name") == "regenerate" and attrs.get("value") == "1"
               for _, attrs in video["controls"])
    assert all("disabled" not in attrs for tag, attrs in video["controls"] if tag == "button")
    draft.selected_video_path = "clean.mp4"
    plan["can_update_video_text"] = True
    plan["effective_image_prompt"] = "Scene plus characters <Samuel>"
    updated, _ = render_teaser(chapter_run=run)
    assert "Video neu erzeugen" in updated
    assert "Nur Textfassung aktualisieren" in updated
    assert "Zuletzt verwendeter Generierungsprompt" in updated
    assert "Scene plus characters &lt;Samuel&gt;" in updated


def test_final_queue_filters_supported_platforms_and_exposes_schema_requirement():
    project = NS(
        revision=4, state="ready", output_path="video.mp4", output_duration_ms=235000,
        aspect="horizontal_fit", audio_track_id="song", transition_ms=500,
    )
    html, forms = render_teaser(project=project, teaser_queued=True)
    queue = next(form for form in forms if form["attrs"].get("action") == "/queue_book_teaser_route")
    platforms = [attrs["value"] for _, attrs in queue["controls"] if attrs.get("name") == "platform"]
    assert platforms == ["youtube"]
    assert any(attrs.get("name") == "project_revision" and attrs["value"] == "4" for _, attrs in queue["controls"])
    assert 'Queue-Schema v10' in html
    assert 'Der finale Book-Teaser wurde in die Veröffentlichungs-Queue gestellt.' in html
    assert 'Facebook und TikTok werden für finale Teaser derzeit nicht unterstützt.' in html
    assert any(attrs.get("name") == "queue_mode" and attrs["value"] == "scheduled" for _, attrs in queue["controls"])
