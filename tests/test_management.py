from contextlib import closing
from io import BytesIO
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from bookpromo.analysis import AnalysisOptions
from bookpromo.analysis_worker import run_analysis_once
from bookpromo.extraction_store import ExtractionStore
from bookpromo.management import BookDetails, ManagementStore
from bookpromo.reels import ReelStore
from bookpromo.overlay import font_names
from bookpromo.uploads import UploadError
from bookpromo.web import _reel_delivery_asset_id, create_app
from test_analysis import setup, FakeAPI, TEXT
from test_extraction import package, p
from test_reels import wav_bytes


def analyzed(setup, **kwargs):
    settings, uploads, book, record, analysis = setup
    analysis.enqueue(book.id, settings, AnalysisOptions())
    assert run_analysis_once(uploads, settings=settings, api_factory=lambda _: FakeAPI(**kwargs))
    return ManagementStore(uploads)


def form_data(snapshot):
    fields = snapshot['details'].model_dump()
    if fields.pop('promotion_enabled'): fields['promotion_enabled'] = 'on'
    return {**fields, 'revision': str(snapshot['revision']), 'suggestion_id': snapshot['suggestion_id']}


def test_read_does_not_create_management_tables(setup):
    _, uploads, book, _, _ = setup
    store = ManagementStore(uploads)
    current = store.get(book.id)
    assert current['revision'] == 0 and not current['manual']
    assert current['details'].title == book.title and not current['details'].promotion_enabled
    with closing(uploads._read_connection()) as connection:
        assert not store.exists(connection, 'local_book_settings')
        assert not store.exists(connection, 'local_quote_controls')


def test_usable_quote_opens_reel_workshop_and_saves_reviewed_copy(setup):
    settings, uploads, book, _, _ = setup
    settings.supabase_enabled = True
    management_store = analyzed(setup)
    current = management_store.get(book.id)
    quote = current["quotes"][0]["quote"]
    base = f"/books/local/{book.id}/chapters/{quote.chapter_id}"
    reel_url = f"{base}/quotes/{quote.id}/reel"
    with TestClient(
        create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000",
        headers={"Accept": "application/json"},
    ) as client:
        chapter = client.get(base, headers={"Accept": "text/html"})
        assert chapter.status_code == 200 and "Reel erzeugen" in chapter.text
        workshop = client.get(reel_url, headers={"Accept": "text/html"})
        assert workshop.status_code == 200
        assert "Instagram-Text und Bildprompt" in workshop.text
        assert "Dieses Buch wurde noch nicht nach Supabase übertragen" in workshop.text

        draft = ReelStore(uploads).list_drafts(book.id)[0]
        saved = client.post(reel_url + "/copy/save", data={
            "revision": str(draft.revision),
            "addition": "Ein kurzer Begleittext.",
            "image_prompt": "A cinematic vertical portrait without text",
        })
        assert saved.status_code == 200 and saved.json()["workshop_url"].endswith("/reel")
        draft = ReelStore(uploads).get_draft(draft.id)
        assert draft.final_caption.startswith(quote.text + "\n\nEin kurzer Begleittext.")
        assert draft.image_prompt == "A cinematic vertical portrait without text"

        uploaded = client.post(
            reel_url + "/audio/upload",
            data={"title": ""},
            files={"file": ("Kira Theme.wav", wav_bytes().getvalue(), "audio/wav")},
        )
        assert uploaded.status_code == 200
        workshop = client.get(reel_url, headers={"Accept": "text/html"})
        assert "Kira Theme · 2.0 s" in workshop.text
        assert "Songtitel" in workshop.text and "(optional)" in workshop.text
        assert "data-audio-waveform" in workshop.text and "Auswahl wiederholen" in workshop.text
        track = ReelStore(uploads).list_audio(book.id)[0]
        source_url = reel_url + f"/audio/source/{track.id}.wav"
        waveform_url = reel_url + f"/audio/waveform/{track.id}.json"
        source = client.get(source_url)
        assert source.status_code == 200 and source.headers["content-type"].startswith("audio/wav")
        waveform = client.get(waveform_url)
        assert waveform.status_code == 200
        assert waveform.json()["duration_ms"] == 2_000
        assert len(waveform.json()["peaks"]) == 720
        assert client.get(reel_url + f"/audio/waveform/{uuid4()}.json").status_code == 404

        foreign = client.post(
            reel_url + "/image", headers={"Origin": "https://foreign.example"},
        )
        assert foreign.status_code == 403

        with ReelStore(uploads).connection() as connection, connection:
            connection.execute(
                "update local_reel_drafts set state='stocked',remote_video_path=? where id=?",
                (f"{draft.id}/{'a' * 64}.mp4", draft.id),
            )
        stocked_workshop = client.get(reel_url, headers={"Accept": "text/html"})
        assert "Bereits übertragen" in stocked_workshop.text
        assert "Erneut in Reel-Vorrat stellen" in stocked_workshop.text
        assert 'name="requeue" value="1"' in stocked_workshop.text


def test_repeated_reel_transfer_uses_a_fresh_asset_id(setup):
    _, uploads, book, _, _ = setup
    draft = ReelStore(uploads).get_or_create_draft(
        book.id, "a" * 64, str(uuid4()), str(uuid4()), "Zitat",
    )
    assert _reel_delivery_asset_id(draft, requeue=False) == draft.id
    with pytest.raises(UploadError, match="noch nicht übertragen"):
        _reel_delivery_asset_id(draft, requeue=True)

    with ReelStore(uploads).connection() as connection, connection:
        ReelStore.schema(connection)
        connection.execute(
            "update local_reel_drafts set state='stocked',remote_video_path=? where id=?",
            (f"{draft.id}/{'a' * 64}.mp4", draft.id),
        )
    stocked = ReelStore(uploads).get_draft(draft.id)
    repeated_id = _reel_delivery_asset_id(stocked, requeue=True)
    assert repeated_id != stocked.id
    assert UUID(repeated_id)
    with pytest.raises(UploadError, match="bereits übertragen"):
        _reel_delivery_asset_id(stocked, requeue=False)


def test_saved_profile_survives_reanalysis_and_preview_is_read_only(setup):
    settings, uploads, book, _, analysis = setup
    store = analyzed(setup)
    current = store.get(book.id)
    assert current['details'].genre == 'Roman'
    original_run = current['suggestion_id']
    edited = current['details'].model_copy(update={'title': 'Neuer Titel', 'author': 'Autor', 'genre': 'Mein Genre',
                                                 'image_prompt_base': 'Mein Bildstil', 'publication_mode': 'auto',
                                                 'carousel_end_text': 'Jetzt entdecken'})
    store.save(book.id, 0, edited, original_run)
    assert uploads.get_book(UUID(book.id)).title == 'Neuer Titel'
    settings.openwebui_model = 'different-model'
    analysis.enqueue(book.id, settings, AnalysisOptions())
    run_analysis_once(uploads, settings=settings, api_factory=lambda _: FakeAPI())
    current = ManagementStore(uploads).get(book.id)
    assert current['suggestion_id'] != original_run and current['manual']
    assert current['details'] == edited
    preview = store.get(book.id, prefer_ai=True)
    assert preview['details'].genre == 'Roman' and preview['details'].title == 'Neuer Titel'
    assert preview['details'].publication_mode == 'auto'
    assert preview['details'].carousel_end_text == 'Jetzt entdecken'
    assert store.get(book.id)['details'] == edited
    store.save(book.id, current['revision'], preview['details'], preview['suggestion_id'])
    assert store.get(book.id)['details'].genre == 'Roman'


def test_settings_before_ai_and_duplicate_upload_preserve_metadata(setup):
    settings, uploads, book, _, analysis = setup
    store = ManagementStore(uploads)
    details = BookDetails(title='Mein Buch', author='Ich', image_prompt_base='Manuell')
    store.save(book.id, 0, details, '')
    with (uploads.root / book.source_path).open('rb') as original:
        duplicate, exists = uploads.save(original, 'Anderer Name.docx')
    assert exists and duplicate.title == 'Mein Buch'
    analysis.enqueue(book.id, settings, AnalysisOptions())
    run_analysis_once(uploads, settings=settings, api_factory=lambda _: FakeAPI())
    assert store.get(book.id)['details'] == details
    assert store.get(book.id, prefer_ai=True)['details'].genre == 'Roman'


def test_concurrent_forms_and_changed_analysis_cannot_overwrite(setup):
    settings, uploads, book, _, analysis = setup
    store = analyzed(setup)
    current = store.get(book.id)
    store.save(book.id, 0, current['details'], current['suggestion_id'])
    with pytest.raises(UploadError) as conflict:
        store.save(book.id, 0, BookDetails(title='Old tab'), current['suggestion_id'])
    assert conflict.value.status == 409
    settings.openwebui_model = 'new'
    analysis.enqueue(book.id, settings, AnalysisOptions())
    with pytest.raises(UploadError) as conflict:
        store.save(book.id, 1, current['details'], current['suggestion_id'])
    assert conflict.value.status == 409


def test_quote_blocks_survive_reanalysis_and_do_not_change_original(setup):
    settings, uploads, book, _, analysis = setup
    store = analyzed(setup)
    current = store.get(book.id)
    item = current['quotes'][0]
    quote = item['quote']
    store.set_blocked(book.id, quote.chapter_id, current['suggestion_id'], quote.id, 0, True)
    assert store.get(book.id)['usable_count'] == 1
    assert store.get(book.id)['blocked_count'] == 1
    with pytest.raises(UploadError) as conflict:
        store.set_blocked(book.id, quote.chapter_id, current['suggestion_id'], quote.id, 0, False)
    assert conflict.value.status == 409
    settings.openwebui_model = 'new'
    analysis.enqueue(book.id, settings, AnalysisOptions())
    run_analysis_once(uploads, settings=settings, api_factory=lambda _: FakeAPI())
    current = ManagementStore(uploads).get(book.id)
    assert current['quotes'][0]['blocked'] and current['quotes'][0]['quote'] == quote
    store.set_blocked(book.id, quote.chapter_id, current['suggestion_id'], quote.id, 1, False)
    assert store.get(book.id)['usable_count'] == 2


def test_unblocking_does_not_override_spoiler_rejection(setup):
    _, _, book, _, _ = setup
    store = analyzed(setup, spoiler='high')
    current = store.get(book.id)
    quote = current['quotes'][0]['quote']
    store.set_blocked(book.id, quote.chapter_id, current['suggestion_id'], quote.id, 0, True)
    store.set_blocked(book.id, quote.chapter_id, current['suggestion_id'], quote.id, 1, False)
    current = store.get(book.id)
    assert not current['quotes'][0]['blocked'] and not current['quotes'][0]['usable']
    assert current['usable_count'] == 0


def test_wrong_chapter_or_quote_and_stale_source_cannot_be_modified(setup):
    _, uploads, book, record, _ = setup
    store = analyzed(setup)
    current = store.get(book.id)
    quote = current['quotes'][0]['quote']
    for chapter_id, quote_id in [(str(uuid4()), quote.id), (quote.chapter_id, str(uuid4()))]:
        with pytest.raises(UploadError) as error:
            store.set_blocked(book.id, chapter_id, current['suggestion_id'], quote_id, 0, True)
        assert error.value.status == 404
    ExtractionStore(uploads).correct(book.id, record.revision, record.result.boundaries)
    assert store.get(book.id)['quotes'] == []
    with pytest.raises(UploadError) as error:
        store.set_blocked(book.id, quote.chapter_id, current['suggestion_id'], quote.id, 0, True)
    assert error.value.status == 409


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'file:///secret', 'https://user:key@example.org', 'https://', 'https://example.org:bad', 'https://example.org/a b'])
def test_target_url_rejects_unsafe_or_invalid_values(url):
    with pytest.raises(ValidationError): BookDetails(title='Buch', target_url=url)


@pytest.mark.parametrize('font,color', [
    ('Bebas Neue', '#a0B1c2'),
    ('Cormorant Garamond SemiBold', '#FFFFFF'),
])
def test_overlay_settings_are_normalized_and_persisted(font, color):
    details = BookDetails(title='Buch', overlay_title_text='Kurzer\nTitel',
                          overlay_title_font=font, overlay_title_color=color)
    assert details.overlay_title_text == 'Kurzer\nTitel'
    assert details.overlay_title_font == font
    assert details.overlay_title_color == color.upper()
    assert details.carousel_background_top_color == '#000000'
    assert details.carousel_background_bottom_color == {
        '#A0B1C2': '#40474E', '#FFFFFF': '#666666'
    }[color.upper()]


@pytest.mark.parametrize('kwargs', [
    {'overlay_title_font': r'..\\secret.ttf'},
    {'overlay_title_font': 'Font\nName'},
    {'overlay_title_text': 'x' * 501},
    {'overlay_title_color': '#fff'},
    {'overlay_title_color': 'white'},
    {'carousel_background_top_color': '#000'},
    {'carousel_background_bottom_color': 'black'},
])
def test_overlay_settings_reject_unsafe_or_invalid_values(kwargs):
    with pytest.raises(ValidationError): BookDetails(title='Buch', **kwargs)


def test_settings_and_quotes_web_round_trip_filters_and_origin(setup):
    settings, uploads, book, record, _ = setup
    store = analyzed(setup)
    current = store.get(book.id)
    available_fonts = font_names()
    if not available_fonts:
        pytest.skip("No Windows fonts available for overlay rendering")
    selected_font = available_fonts[0]
    url = f'/books/local/{book.id}'
    with TestClient(create_app(settings, start_worker=False), base_url='http://127.0.0.1:8000') as client:
        fields = form_data(current)
        fields.update(title='Neuer Titel <script>alert(1)</script>', author='Autor', target_url='https://example.org/buch',
                      overlay_title_text='Kurzer\nOverlay-Titel', overlay_title_font=selected_font,
                      overlay_title_color='#102030', carousel_background_top_color='#112233',
                      carousel_background_bottom_color='#445566')
        assert client.get(url+'/settings').status_code == 200
        assert client.post(url+'/settings', data=fields, headers={'Origin':'https://foreign.example'}).status_code == 403
        response = client.post(url+'/settings', data=fields)
        assert response.status_code == 200 and 'Bucheinstellungen gespeichert' in response.text
        assert '<script>alert(1)</script>' not in response.text and '&lt;script&gt;' in response.text
        assert not store.get(book.id)['details'].promotion_enabled
        assert store.get(book.id)['details'].overlay_title_text == 'Kurzer\nOverlay-Titel'
        assert store.get(book.id)['details'].overlay_title_font == selected_font
        assert store.get(book.id)['details'].overlay_title_color == '#102030'
        assert store.get(book.id)['details'].carousel_background_top_color == '#112233'
        assert store.get(book.id)['details'].carousel_background_bottom_color == '#445566'
        overlay = client.get(url + '/overlay.png')
        assert overlay.status_code == 200 and overlay.headers['content-type'] == 'image/png'
        quote = current['quotes'][0]['quote']
        chapter = url+'/chapters/'+quote.chapter_id
        action = chapter+'/quotes/'+quote.id+'/block'
        block = {'run_id':current['suggestion_id'], 'revision':'0', 'blocked':'true', 'filter':'all', 'quote_page':'1'}
        assert client.post(action, data=block, headers={'Origin':'https://foreign.example'}).status_code == 403
        response = client.post(action, data=block)
        assert response.status_code == 200 and 'Sperre aufheben' in response.text
        assert '0 nutzbar · 1 gesperrt' in client.get(url).text
        assert 'Sperre aufheben' in client.get(chapter+'?filter=blocked').text
        assert 'quote-card' not in client.get(chapter+'?filter=usable').text
        assert client.get(chapter+'?filter=unknown').status_code == 422
        assert client.post(action, data=block).status_code == 409
        block.update(revision='1', blocked='false')
        assert client.post(action, data=block).status_code == 200
        assert store.get(book.id)['usable_count'] == 2
        assert quote.text in client.get(chapter).text
        assert 'Veröffentlichungshistorie angebunden' in client.get(chapter).text


def test_invalid_form_retains_inputs_without_writing_and_get_does_not_adopt(setup):
    settings, _, book, _, _ = setup
    store = analyzed(setup)
    current = store.get(book.id)
    url = f'/books/local/{book.id}/settings'
    with TestClient(create_app(settings, start_worker=False), base_url='http://127.0.0.1:8000') as client:
        fields = form_data(current)
        fields.update(title='Meine ungespeicherte Änderung', target_url='javascript:bad')
        response = client.post(url, data=fields)
        assert response.status_code == 400 and 'Meine ungespeicherte Änderung' in response.text
        assert not store.get(book.id)['manual']
        assert client.get(url+'?preview=ai').status_code == 200
        assert not store.get(book.id)['manual']
        fields.update(target_url='', secret='wrong')
        assert client.post(url, data=fields).status_code == 400
        assert client.post(url, data={'title':'Missing fields'}).status_code == 400
