from io import BytesIO
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

from bookpromo.config import Settings
from bookpromo.extraction import Boundary, apply_boundaries, extract_docx
from bookpromo.extraction_store import ExtractionStore
from bookpromo.uploads import LocalUploadStore, UploadError
from bookpromo.web import create_app
from bookpromo.worker import run_once
from test_uploads import docx_bytes

NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def p(text, props=""):
    return f'<w:p><w:pPr>{props}</w:pPr><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def package(body, *, styles=None, ns=NS, extra=None):
    parts = dict(extra or {})
    if styles is not None:
        parts["word/styles.xml"] = f'<w:styles xmlns:w="{ns}">{styles}</w:styles>'
    return docx_bytes(main=f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>', extra=parts)


def extract(tmp_path, body, **kwargs):
    path = tmp_path / "fixture.docx"
    path.write_bytes(package(body, **kwargs))
    return extract_docx(path, "5c3bd2b8-9ad4-4815-a4f2-41265e40953d")


def test_initial_split_heading_and_exact_source_offsets(tmp_path):
    result = extract(tmp_path, p("") + p("KAPITEL 1") + p("BLUTIGE KUNST")
        + p("D", '<w:framePr w:dropCap="drop" w:lines="3"/>')
        + p("er Motor läuft.  Zwei Leerzeichen bleiben.") + p("Sarah wartet.")
        + p("Kapitel 2: Heimkehr") + p("Ende."))
    assert not result.needs_review
    first, second = result.chapters
    assert first.title == "KAPITEL 1 · BLUTIGE KUNST"
    assert first.source_text == "Der Motor läuft.  Zwei Leerzeichen bleiben.\n\nSarah wartet."
    assert second.source_text == "Ende."
    initial = result.paragraphs[2]
    assert initial.id == "p000004"
    assert [s.paragraph_id for s in initial.sources] == ["p000004", "p000005"]
    assert [(s.start, s.end) for s in initial.sources] == [(0, 1), (1, len(initial.text))]
    assert first.source_text[first.paragraphs[0]["start"]:first.paragraphs[0]["end"]] == initial.text
    again = extract(tmp_path, p("") + p("KAPITEL 1") + p("BLUTIGE KUNST")
        + p("D", '<w:framePr w:dropCap="drop" w:lines="3"/>')
        + p("er Motor läuft.  Zwei Leerzeichen bleiben.") + p("Sarah wartet.")
        + p("Kapitel 2: Heimkehr") + p("Ende."))
    assert result.model_dump() == again.model_dump()


def test_same_paragraph_initial_runs_breaks_and_unicode(tmp_path):
    body = p("KAPITEL: 1\t\nBLUTIGE KUNST") + '<w:p><w:r><w:t>D</w:t></w:r><w:r><w:t>er 🐈 schläft.</w:t><w:br/><w:t>„Still“</w:t><w:tab/><w:t>hier.</w:t></w:r></w:p>'
    result = extract(tmp_path, body)
    assert result.chapters[0].source_text == 'Der 🐈 schläft.\n„Still“\thier.'
    assert result.chapters[0].paragraphs[0]["end"] == len(result.chapters[0].source_text)
    assert "BLUTIGE KUNST" in result.chapters[0].title
    assert not result.needs_review


def test_uppercase_prose_after_full_title_is_not_removed(tmp_path):
    result = extract(tmp_path, p("Kapitel: 1 – Eine Nacht") + p("ALLES WAR STILL") + p("Dann begann es."))
    assert result.chapters[0].source_text == "ALLES WAR STILL\n\nDann begann es."


def test_uppercase_title_guess_requires_review(tmp_path):
    result = extract(tmp_path, p("Kapitel 1") + p("EINE NACHT") + p("Es regnete."))
    assert result.needs_review
    assert "EINE NACHT" in result.chapters[0].title


def test_initial_without_frame_is_not_silently_joined(tmp_path):
    result = extract(tmp_path, p("Kapitel 1") + p("D") + p("er Motor läuft."))
    assert result.chapters[0].source_text == "D\n\ner Motor läuft."
    assert any(w.code == "initial" for w in result.warnings)


def test_inline_alternate_content_is_not_duplicated(tmp_path):
    content = '<w:p xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"><w:r><w:t>Normal.</w:t><mc:AlternateContent><mc:Choice><w:t>Doppelt</w:t></mc:Choice><mc:Fallback><w:t>Doppelt</w:t></mc:Fallback></mc:AlternateContent></w:r></w:p>'
    result = extract(tmp_path, p("Kapitel 1") + content)
    assert result.chapters[0].source_text == "Normal."
    assert result.needs_review


def test_deep_xml_is_bounded(tmp_path):
    with pytest.raises(UploadError, match="verschachtelt"):
        extract(tmp_path, '<w:customXml>' * 102 + p("Text.") + '</w:customXml>' * 102)


def test_concurrent_extraction_reuses_snapshot(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store, book, _ = setup_store(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: ExtractionStore(store).extract(book), range(2)))
    assert results[0] == results[1]
    assert results[0].revision == 1


@pytest.mark.parametrize("ns", [NS, "http://purl.oclc.org/ooxml/wordprocessingml/main"])
def test_inherited_headings_and_subheadings(tmp_path, ns):
    styles = '<w:style w:styleId="Heading1"><w:name w:val="heading 1"/><w:pPr><w:outlineLvl w:val="0"/></w:pPr></w:style><w:style w:styleId="MeinKapitel"><w:basedOn w:val="Heading1"/></w:style>'
    result = extract(tmp_path, p("Ankunft", '<w:pStyle w:val="MeinKapitel"/>') + p("Text.")
        + p("Zwischenüberschrift", '<w:outlineLvl w:val="1"/>') + p("Mehr Text.")
        + p("Abreise", '<w:pStyle w:val="MeinKapitel"/>') + p("Ende."), styles=styles, ns=ns)
    assert [c.title for c in result.chapters] == ["Ankunft", "Abreise"]
    assert "Zwischenüberschrift" in result.chapters[0].source_text


def test_toc_fields_styles_and_content_controls_excluded(tmp_path):
    body = '<w:sdt><w:sdtPr><w:docPartObj><w:docPartGallery w:val="Table of Contents"/></w:docPartObj></w:sdtPr><w:sdtContent>' + p("Kapitel 999") + '</w:sdtContent></w:sdt>'
    body += p("Kapitel 888", '<w:pStyle w:val="TOC1"/>')
    body += '<w:p><w:r><w:fldChar w:fldCharType="begin"/><w:instrText> TO</w:instrText><w:instrText>C \\o "1-3"</w:instrText><w:fldChar w:fldCharType="separate"/></w:r></w:p>'
    body += p("Kapitel 777") + '<w:p><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
    body += p("Kapitel 1") + p("Nur der Haupttext.")
    result = extract(tmp_path, body, extra={"word/header1.xml": f'<w:hdr xmlns:w="{NS}">{p("Kopfzeile")}</w:hdr>'})
    assert len(result.chapters) == 1
    assert result.chapters[0].source_text == "Nur der Haupttext."
    assert all("777" not in paragraph.text for paragraph in result.paragraphs)


@pytest.mark.parametrize("content,code", [
    ('<w:p><w:r><w:t>Text.</w:t><w:pict><w:txbxContent>' + p("Textfeld") + '</w:txbxContent></w:pict></w:r></w:p>', "objects"),
    ('<w:p><w:ins><w:r><w:t>Neu.</w:t></w:r></w:ins><w:del><w:r><w:delText>Alt.</w:delText></w:r></w:del></w:p>', "tracked_changes"),
    ('<w:tbl><w:tr><w:tc>' + p("Zelle A") + '</w:tc><w:tc>' + p("Zelle B") + '</w:tc></w:tr></w:tbl>', "tables"),
    (p("D", '<w:framePr w:dropCap="margin"/>') + p("Großgeschrieben."), "initial"),
], ids=["textbox", "revisions", "table", "ambiguous-initial"])
def test_unsupported_content_stays_blocked_after_boundary_confirmation(tmp_path, content, code):
    result = extract(tmp_path, p("Kapitel 1") + content)
    assert result.needs_review and any(w.code == code and w.blocking for w in result.warnings)
    corrected = apply_boundaries(result, result.boundaries)
    assert corrected.needs_review
    assert "Textfeld" not in corrected.chapters[0].source_text
    assert "Alt." not in corrected.chapters[0].source_text


def test_no_headings_requires_review_and_manual_boundaries_preserve_text(tmp_path):
    result = extract(tmp_path, p("Erster Absatz.") + p("Zweiter Absatz.") + p("Dritter Absatz."))
    assert result.needs_review
    corrected = apply_boundaries(result, [Boundary(paragraph_id="p000001", title="Anfang"), Boundary(paragraph_id="p000003", title="Ende")])
    assert not corrected.needs_review
    assert [c.source_text for c in corrected.chapters] == ["Erster Absatz.\n\nZweiter Absatz.", "Dritter Absatz."]
    for invalid in ([Boundary(paragraph_id="p000002", title="Überspringen")],
                    [Boundary(paragraph_id="p000001", title="A"), Boundary(paragraph_id="p000001", title="B")],
                    [Boundary(paragraph_id="p000001", title="A"), Boundary(paragraph_id="p999999", title="B")]):
        with pytest.raises(UploadError): apply_boundaries(result, invalid)


def test_frontmatter_and_empty_chapters_never_disappear(tmp_path):
    result = extract(tmp_path, p("Widmung") + p("Kapitel 1") + p("Kapitel 2") + p("Ende."))
    assert result.needs_review
    assert result.chapters[0].source_text == "Widmung"
    assert "Kapitel 1" in result.chapters[1].source_text


def test_bad_styles_and_no_body_text_are_safe_errors(tmp_path):
    with pytest.raises(UploadError, match="Formatvorlagen"):
        extract(tmp_path, p("Text."), styles="<broken>")
    with pytest.raises(UploadError, match="Kein lesbarer Text"):
        extract(tmp_path, p(""))


def setup_store(tmp_path):
    store = LocalUploadStore(tmp_path / "data", 1024 * 1024)
    book, _ = store.save(BytesIO(package(p("Absatz eins.") + p("Absatz zwei."))), "Roman.docx")
    return store, book, ExtractionStore(store)


def test_persistence_old_uploads_corrections_and_stale_revision(tmp_path):
    store, book, extractor = setup_store(tmp_path)
    assert extractor.get(book.id) is None
    original = extractor.extract(book)
    boundaries = [Boundary(paragraph_id="p000001", title="Ein Kapitel")]
    extractor.correct(book.id, original.revision, boundaries)
    restarted = ExtractionStore(LocalUploadStore(store.root, store.max_bytes))
    corrected = restarted.get(book.id)
    assert corrected.result.reviewed and corrected.result.chapters[0].title == "Ein Kapitel"
    assert restarted.extract(book) == corrected  # repeat upload cannot overwrite edits
    with pytest.raises(UploadError) as exc: restarted.correct(book.id, original.revision, boundaries)
    assert exc.value.status == 409


def test_missing_original_retries_and_changed_source_is_rejected(tmp_path):
    store, book, extractor = setup_store(tmp_path)
    original = (store.root / book.source_path).read_bytes()
    (store.root / book.source_path).unlink()
    assert extractor.extract(book).error
    store.save(BytesIO(original), "Roman.docx")
    assert extractor.extract(book).result is not None
    other, _ = store.save(BytesIO(package(p("Neu."))), "Neu.docx")
    (store.root / other.source_path).write_bytes(original)
    assert "verändert" in extractor.extract(other).error


def test_web_upload_review_chapter_and_csrf(tmp_path, monkeypatch):
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    settings = Settings(_env_file=None, app_data_dir=tmp_path / "data")
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        response = client.post("/uploads", files={"file": ("Roman.docx", package(p("&lt;script&gt;Text&lt;/script&gt;") + p("Weiter.")))}, headers={"Accept": "application/json"})
        assert response.status_code == 201
        payload = response.json()
        assert run_once(LocalUploadStore(settings.app_data_dir, 1024 * 1024))
        detail = client.get(payload["url"])
        assert detail.headers["referrer-policy"] == "same-origin"
        assert "Prüfung nötig" in detail.text
        record = ExtractionStore(LocalUploadStore(settings.app_data_dir, 1024 * 1024)).get(payload["book_id"])
        data = {"revision": record.revision, "start": "1", "title": "Bestätigtes Kapitel", "heading_count": "0"}
        action = payload["url"] + "/chapters"
        for headers in (
            {"Origin": "https://foreign.example"},
            {"Origin": "null", "Sec-Fetch-Site": "cross-site"},
            {"Origin": "http://127.0.0.1:8001", "Sec-Fetch-Site": "same-site"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            assert client.post(action, data=data, headers=headers).status_code == 403
        assert ExtractionStore(LocalUploadStore(settings.app_data_dir, 1024 * 1024)).get(payload["book_id"]).revision == record.revision
        assert client.post(action, data={**data, "start": "999"}).status_code == 400
        saved = client.post(action, data=data, headers={
            "Origin": "http://127.0.0.1:8000", "Sec-Fetch-Site": "same-origin",
        })
        assert saved.status_code == 200 and "Text eingelesen" in saved.text
        persisted = ExtractionStore(LocalUploadStore(settings.app_data_dir, 1024 * 1024)).get(payload["book_id"])
        assert persisted.result.reviewed
        assert persisted.result.chapters[0].title == "Bestätigtes Kapitel"
        assert client.post(action, data=data).status_code == 409
        chapter_url = action + "/" + record.result.chapters[0].id
        chapter = client.get(chapter_url)
        assert chapter.status_code == 200 and "Bestätigtes Kapitel" in chapter.text
        assert "&lt;script&gt;Text" in chapter.text and "<script>Text" not in chapter.text
        assert client.get(action + "/" + str(uuid4())).status_code == 404
        assert client.post(payload["url"] + "/extract").status_code == 200


def test_old_upload_can_be_extracted_from_web(tmp_path, monkeypatch):
    for field in Settings.model_fields: monkeypatch.delenv(field.upper(), raising=False)
    store, book, _ = setup_store(tmp_path)
    with TestClient(create_app(Settings(_env_file=None, app_data_dir=store.root), start_worker=False), base_url="http://127.0.0.1:8000") as client:
        url = f"/books/local/{book.id}"
        assert "Text und Kapitel einlesen" in client.get(url).text
        assert "Import im Hintergrund" in client.post(url + "/extract").text
        assert run_once(store)
        assert "Kapitelaufteilung prüfen und korrigieren" in client.get(url).text


def numbering_part(fmt="decimal", label="%1.", override=""):
    return {'word/numbering.xml': f'<w:numbering xmlns:w="{NS}"><w:abstractNum w:abstractNumId="0">'
        f'<w:lvl w:ilvl="0"><w:numFmt w:val="{fmt}"/><w:lvlText w:val="{label}"/></w:lvl>'
        f'</w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/>{override}</w:num></w:numbering>'}


NUM_PROPS = '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'


def test_numbered_headings_do_not_block_but_numbered_body_does(tmp_path):
    result = extract(tmp_path, p('Kapitel 1', NUM_PROPS) + p('Buchtext.'), extra=numbering_part())
    assert not result.needs_review
    assert result.numbered_paragraph_ids == ['p000001']
    assert result.chapters[0].source_text == 'Buchtext.'
    corrected = apply_boundaries(result, [Boundary(paragraph_id='p000001', title='Mit Überschrift', heading_count=0)])
    assert corrected.needs_review
    assert any(w.blocking and 'Absatz 1' in w.message for w in corrected.warnings)
    assert not apply_boundaries(corrected, result.boundaries).needs_review


@pytest.mark.parametrize('fmt,label,blocking', [('bullet','•',False),('bullet','\uf0b7',False),('decimal','%1.',True),('bullet','Wichtiger Text',True)])
def test_bullets_preserve_list_text_and_unknown_markers_stay_blocked(tmp_path, fmt, label, blocking):
    result = extract(tmp_path, p('Kapitel 1') + p('Ein Listeneintrag.', NUM_PROPS), extra=numbering_part(fmt,label))
    assert result.needs_review is blocking
    assert result.chapters[0].source_text == 'Ein Listeneintrag.'


def test_numbering_override_and_explicit_removal(tmp_path):
    override = '<w:lvlOverride w:ilvl="0"><w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl></w:lvlOverride>'
    result = extract(tmp_path, p('Kapitel 1') + p('Text.', NUM_PROPS), extra=numbering_part('bullet','•',override))
    assert result.needs_review
    styles = f'<w:style w:styleId="List"><w:pPr>{NUM_PROPS}</w:pPr></w:style>'
    result = extract(tmp_path, p('Kapitel 1') + p('Text.', '<w:pStyle w:val="List"/><w:numPr><w:numId w:val="0"/></w:numPr>'), styles=styles)
    assert not result.needs_review and not result.numbered_paragraph_ids


@pytest.mark.parametrize('inside,blocking', [('<w:p/>',False),(p('Text im Objekt'),True),('<w:p><w:r><w:sym w:char="0041"/></w:r></w:p>',True)])
def test_alternate_empty_drawing_vs_actual_text(tmp_path, inside, blocking):
    alternate = ('<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
                 f'<mc:Choice Requires="w"><w:drawing><w:txbxContent>{inside}</w:txbxContent></w:drawing></mc:Choice>'
                 '<mc:Fallback><w:pict/></mc:Fallback></mc:AlternateContent>')
    result = extract(tmp_path, p('Kapitel 1') + p('Buchtext.') + alternate)
    assert result.needs_review is blocking
    assert result.chapters[0].source_text == 'Buchtext.'
