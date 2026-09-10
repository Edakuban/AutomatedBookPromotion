import json
from io import BytesIO

from fastapi.testclient import TestClient
import pytest

from bookpromo.analysis import AnalysisOptions, PROFILE_FIELDS, PROFILE_FIELD_MODELS, Summary
from bookpromo.analysis_worker import run_analysis_once
from bookpromo.extraction_store import ExtractionStore
from bookpromo.management import ManagementStore
from bookpromo.openwebui import OpenWebUIError
from bookpromo.sync import SyncStore
from bookpromo.uploads import UploadError
from bookpromo.web import create_app
from test_analysis import setup, FakeAPI, profile, TEXT, TEXT2
from test_management import form_data
from test_extraction import package, p


class ProfileAPI:
    def __init__(self, fail_field=None):
        self.calls = []
        self.fail_field = fail_field

    async def complete_json(self, system, user, result_type, **kwargs):
        data = json.loads(user)
        self.calls.append((result_type, data))
        if result_type is Summary:
            return Summary(summary=data['source_text'], spoilers=['Ende im zweiten Kapitel'])
        name = next(name for name, model in PROFILE_FIELD_MODELS.items() if model is result_type)
        if name == self.fail_field:
            raise OpenWebUIError('timeout')
        return result_type.model_validate({name: profile().model_dump()[name]}, strict=True)


def test_separate_profile_reads_all_chapters_and_uses_one_call_per_field(setup):
    settings, uploads, book, _, store = setup
    api = ProfileAPI()
    options = AnalysisOptions(purpose='profile')
    run_id = store.enqueue(book.id, settings, options)
    assert run_analysis_once(uploads, settings=settings, api_factory=lambda _: api)
    result = store.latest(book.id, purpose='profile', include_result=True)
    assert result['state'] == 'done' and result['result'].profile == profile()
    assert result['result'].quotes == []
    assert [data['source_text'] for model, data in api.calls if model is Summary] == [TEXT, TEXT2]
    field_calls = [(model, data) for model, data in api.calls if model is not Summary]
    assert [model for model, _ in field_calls] == list(PROFILE_FIELD_MODELS.values())
    for model, data in field_calls:
        assert len(model.model_fields) == 1
        assert [s['summary'] for s in data['summaries']] == [TEXT, TEXT2]
    assert store.latest(book.id) is None
    assert not ManagementStore(uploads).get(book.id)['manual']
    with pytest.raises(UploadError, match='KI-Analyse'):
        SyncStore(uploads).snapshot(book.id)
    assert store.enqueue(book.id, settings, options) == run_id
    assert not run_analysis_once(uploads, settings=settings, api_factory=lambda _: pytest.fail('repeated'))


def test_profile_resume_preserves_fields_and_quote_analysis(setup):
    settings, uploads, book, _, store = setup
    full_id = store.enqueue(book.id, settings, AnalysisOptions())
    run_analysis_once(uploads, settings=settings, api_factory=lambda _: FakeAPI())
    management = ManagementStore(uploads)
    before = management.get(book.id)
    management.save(book.id, before['revision'], before['details'].model_copy(update={'genre': 'Eigene Angabe'}), before['suggestion_id'])
    options = AnalysisOptions(purpose='profile')
    run_id = store.enqueue(book.id, settings, options)
    with pytest.raises(UploadError, match='andere KI-Analyse'):
        store.enqueue(book.id, settings, AnalysisOptions())
    run_analysis_once(uploads, settings=settings, api_factory=lambda _: ProfileAPI(fail_field='world'))
    failed = store.latest(book.id, purpose='profile')
    assert failed['state'] == 'failed' and failed['completed_steps'] == 5
    assert store.enqueue(book.id, settings, options) == run_id
    api = ProfileAPI()
    run_analysis_once(uploads, settings=settings, api_factory=lambda _: api)
    assert [next(iter(model.model_fields)) for model, _ in api.calls] == list(PROFILE_FIELDS)[3:]
    assert store.latest(book.id)['id'] == full_id
    after = management.get(book.id)
    assert after['details'].genre == 'Eigene Angabe'
    assert after['quotes'] == before['quotes']
    assert len(SyncStore(uploads).snapshot(book.id)['quotes']) == 2


def test_profile_web_preview_explicit_save_and_stale_source(setup):
    settings, uploads, book, record, store = setup
    management = ManagementStore(uploads)
    current = management.get(book.id)
    details = current['details'].model_copy(update={'author': 'Mein Autor', 'target_url': 'https://example.org/buch',
                                                     'genre': 'Eigene Angabe', 'publication_mode': 'auto'})
    management.save(book.id, 0, details, '')
    url = f'/books/local/{book.id}/settings'
    headers = {'Accept': 'application/json', 'Origin': 'http://127.0.0.1:8000'}
    with TestClient(create_app(settings, start_worker=False), base_url='http://127.0.0.1:8000') as client:
        assert 'Profil mit KI ausfüllen' in client.get(url).text
        assert store.latest(book.id, purpose='profile') is None
        assert client.post(url+'/profile', headers={**headers, 'Origin': 'https://foreign.example'}).status_code == 403
        started = client.post(url+'/profile', headers=headers)
        assert started.status_code == 200
        assert client.get(url+'/profile/result', headers=headers).status_code == 409
        assert run_analysis_once(uploads, settings=settings, api_factory=lambda _: ProfileAPI())
        status = client.get(url+'/profile/status').json()
        assert status['state'] == 'done' and 'result' not in status
        suggestion = client.get(url+'/profile/result').json()
        assert set(suggestion['fields']) == set(PROFILE_FIELDS)
        assert 'PRIVATE_KEY' not in json.dumps(suggestion)
        assert management.get(book.id)['details'] == details
        fields = form_data(management.get(book.id))
        fields.update(suggestion['fields'], profile_run_id=suggestion['id'])
        assert client.post(url, data=fields).status_code == 200
        saved = management.get(book.id)['details']
        assert saved.genre == profile().genre
        assert saved.author == details.author and saved.target_url == details.target_url
        assert saved.publication_mode == 'auto' and not saved.promotion_enabled
        fields = form_data(management.get(book.id))
        fields['profile_run_id'] = suggestion['id']
        ExtractionStore(uploads).correct(book.id, record.revision, record.result.boundaries)
        assert client.get(url+'/profile/result', headers=headers).status_code == 409
        assert client.post(url, data=fields, headers=headers).status_code == 409
        assert management.get(book.id)['details'] == saved


def test_profile_refuses_unreviewed_source_and_enforces_budget(setup):
    settings, uploads, book, record, store = setup
    options = AnalysisOptions(purpose='profile', max_calls=3)
    store.enqueue(book.id, settings, options)
    api = ProfileAPI()
    run_analysis_once(uploads, settings=settings, api_factory=lambda _: api)
    result = store.latest(book.id, purpose='profile')
    assert result['state'] == 'failed' and result['calls_started'] == 3
    store.enqueue(book.id, settings, options)
    retry = ProfileAPI()
    run_analysis_once(uploads, settings=settings, api_factory=lambda _: retry)
    assert retry.calls == []
    assert store.latest(book.id, purpose='profile')['calls_started'] == 3
    unchecked, _ = uploads.save(BytesIO(package(p('Text ohne Kapitelüberschrift.'))), 'Ungeprüft.docx')
    ExtractionStore(uploads).extract(unchecked)
    with pytest.raises(UploadError, match='Hinweise'):
        store.enqueue(unchecked.id, settings, options)


def test_context_reused_for_new_profile_prompts_and_quote_analysis(setup):
    settings, uploads, book, record, store = setup
    store.enqueue(book.id, settings, AnalysisOptions(purpose='profile', profile_prompt_version=1))
    run_analysis_once(uploads, settings=settings, api_factory=lambda _: ProfileAPI())
    assert store.latest(book.id, purpose='profile')['state'] == 'stale'
    store.enqueue(book.id, settings, AnalysisOptions(purpose='profile'))
    api = ProfileAPI()
    run_analysis_once(uploads, settings=settings, api_factory=lambda _: api)
    assert len(api.calls) == 8 and all(model is not Summary for model, _ in api.calls)
    assert store.latest(book.id, purpose='profile')['calls_started'] == 8
    assert ExtractionStore(uploads).get(book.id) == record
    store.enqueue(book.id, settings, AnalysisOptions())
    full = FakeAPI()
    run_analysis_once(uploads, settings=settings, api_factory=lambda _: full)
    assert [name for name, _, _ in full.calls] == ['BookProfile', 'Candidates', 'Candidates']
    assert store.latest(book.id)['state'] == 'done'


@pytest.mark.parametrize('change', ['model', 'chunk_size', 'revision'])
def test_context_cache_requires_same_model_chunking_and_source(setup, change):
    settings, uploads, book, record, store = setup
    store.enqueue(book.id, settings, AnalysisOptions(purpose='profile'))
    run_analysis_once(uploads, settings=settings, api_factory=lambda _: ProfileAPI())
    options = AnalysisOptions(purpose='profile')
    if change == 'model': settings = settings.model_copy(update={'openwebui_model': 'different-model'})
    if change == 'chunk_size': options.chunk_chars = 10000
    if change == 'revision': ExtractionStore(uploads).correct(book.id, record.revision, record.result.boundaries)
    store.enqueue(book.id, settings, options)
    api = ProfileAPI()
    run_analysis_once(uploads, settings=settings, api_factory=lambda _: api)
    assert sum(model is Summary for model, _ in api.calls) == 2
