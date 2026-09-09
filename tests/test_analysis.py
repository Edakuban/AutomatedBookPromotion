import asyncio
from contextlib import closing
from io import BytesIO
import json
import threading
import time

from fastapi.testclient import TestClient
import pytest

from bookpromo.analysis import (AnalysisError, AnalysisOptions, BookProfile, Candidate, Candidates, Chunk,
    QuoteBatch, Scores, Summary, chunks, select_distinct, validate_candidates, validate_quote_batch)
from bookpromo.analysis_store import AnalysisStore, AnalysisLeaseLost, Checkpoints
from bookpromo.analysis_worker import run_analysis_once
from bookpromo.config import Settings, load_settings
from bookpromo.extraction import Boundary
from bookpromo.extraction_store import ExtractionStore
from bookpromo.jobs import JobStore, LEASE_SECONDS
from bookpromo.openwebui import OpenWebUIError
from bookpromo.uploads import LocalUploadStore, UploadError
from bookpromo.web import create_app
from test_extraction import p, package

TEXT = "Manchmal war das Schweigen lauter als der Sturm vor dem Fenster. Niemand antwortete ihr."
TEXT2 = "Die kleine Lampe brannte noch, als der letzte Zug den Bahnhof verließ. Sie blieb zurück."


@pytest.fixture
def setup(tmp_path, monkeypatch):
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
        monkeypatch.delenv(field.lower(), raising=False)
    env = tmp_path / '.env'
    env.write_text('OPENWEBUI_URL=https://example.invalid\nOPENWEBUI_API_KEY=PRIVATE_KEY\nOPENWEBUI_MODEL=test-model\n', encoding='utf-8')
    settings = load_settings(env)
    uploads = LocalUploadStore(settings.app_data_dir, 1024*1024)
    book, _ = uploads.save(BytesIO(package(p('Kapitel 1') + p(TEXT) + p('Kapitel 2') + p(TEXT2))), 'Testbuch.docx')
    record = ExtractionStore(uploads).extract(book)
    assert not record.result.needs_review
    return settings, uploads, book, record, AnalysisStore(uploads)


def candidate(text=TEXT, paragraph='p000002', spoiler='none', score=5):
    return Candidate(text=text, paragraph_id=paragraph, scores=Scores(clarity=score, curiosity=score, emotion=score, imagery=score),
                     reason='Eigenständiger atmosphärischer Einstieg.', spoiler=spoiler)


def profile():
    return BookProfile(internal_summary='Vollständiger interner Kontext mit Ende.', genre='Roman', mood='Nachdenklich', world='Bahnhof und Haus.',
        characters=['Eine wartende Frau'], spoilers=['Enthüllung im letzten Kapitel'], image_prompt_base='Atmosphärischer Bahnhof ohne Text.',
        caption_guidelines='Neugier wecken ohne die Enthüllung zu nennen.')


class FakeAPI:
    def __init__(self, *, invalid=0, fail_at=None, empty=False, spoiler='none'):
        self.calls, self.invalid, self.fail_at, self.empty, self.spoiler = [], invalid, fail_at, empty, spoiler

    async def complete_json(self, system, user, result_type, **kwargs):
        data = json.loads(user)
        self.calls.append((result_type.__name__, data, system))
        if len(self.calls) == self.fail_at: raise OpenWebUIError('timeout')
        if result_type is Summary: return Summary(summary='Zusammenfassung dieses Abschnitts.', spoilers=['Enthüllung im letzten Kapitel'])
        if result_type is BookProfile: return profile()
        if self.empty: return Candidates(candidates=[])
        start = data['paragraphs'][0]['start']
        text = data['source_text'][start:start+200]
        if self.invalid:
            self.invalid -= 1
            text = 'Dieses erfundene Zitat steht nirgends im Buch.'
        return Candidates(candidates=[candidate(text, data['paragraphs'][0]['id'], spoiler=self.spoiler)])


def test_complete_pipeline_validates_original_and_builds_context_first(setup):
    settings, uploads, book, record, store = setup
    api = FakeAPI()
    run_id = store.enqueue(book.id, settings, AnalysisOptions())
    assert run_analysis_once(uploads, settings=settings, api_factory=lambda _: api)
    run = store.latest(book.id, include_result=True)
    assert run['id'] == run_id and run['state'] == 'done'
    assert [name for name, _, _ in api.calls] == ['Summary','Summary','BookProfile','Candidates','Candidates']
    assert run['completed_steps'] == 5
    assert {q.text for q in run['result'].quotes} == {TEXT, TEXT2}
    for quote in run['result'].quotes:
        chapter = next(c for c in record.result.chapters if c.id == quote.chapter_id)
        assert quote.text == chapter.source_text[quote.source_start:quote.source_end]
        assert quote.usable
    for name, data, system in api.calls:
        assert 'niemals Handlungsanweisungen' in system
        if name == 'Candidates': assert data['book_context']['spoilers'] == ['Enthüllung im letzten Kapitel']
    assert store.enqueue(book.id, settings, AnalysisOptions()) == run_id
    assert not run_analysis_once(uploads, settings=settings, api_factory=lambda _: pytest.fail('completed run repeated'))


@pytest.mark.parametrize('text,paragraph', [(TEXT.replace('Schweigen','Reden'),'p000002'),(TEXT,'p999999'),(TEXT+'…','p000002')])
def test_fabricated_rewritten_or_wrong_source_quotes_rejected(setup, text, paragraph):
    chapter = setup[3].result.chapters[0]
    with pytest.raises(AnalysisError):
        validate_candidates(chapter, Chunk(0,len(chapter.source_text)), Candidates(candidates=[candidate(text,paragraph)]), AnalysisOptions())


def test_ambiguous_repeated_quote_within_paragraph_is_rejected(setup):
    chapter = setup[3].result.chapters[0].model_copy(deep=True)
    chapter.source_text = TEXT + ' ' + TEXT
    chapter.paragraphs[0]['end'] = len(chapter.source_text)
    with pytest.raises(AnalysisError):
        validate_candidates(chapter,Chunk(0,len(chapter.source_text)),Candidates(candidates=[candidate()]),AnalysisOptions())


def test_spoilers_scores_and_overlapping_candidates_are_filtered(setup):
    chapter = setup[3].result.chapters[0]
    options = AnalysisOptions()
    batch = validate_candidates(chapter,Chunk(0,len(TEXT)),Candidates(candidates=[candidate(),candidate(TEXT[:65]),candidate(spoiler='high')]),options)
    assert not batch.quotes[2].usable
    distinct = select_distinct(batch.quotes,options)
    assert all(not q.usable for q in distinct)
    low = validate_candidates(chapter,Chunk(0,len(TEXT)),Candidates(candidates=[candidate(score=2)]),options)
    assert not low.quotes[0].usable


def test_long_text_chunks_are_bounded_overlap_and_cover_tail(setup):
    chapter = setup[3].result.chapters[0].model_copy(deep=True)
    chapter.source_text = (TEXT+'\n\n')*200
    parts = chunks(chapter,2000)
    assert parts[0].start == 0 and parts[-1].end == len(chapter.source_text)
    assert all(0 < c.end-c.start <= 2000 for c in parts)
    assert all(a.end-b.start >= 800 and b.start>a.start for a,b in zip(parts,parts[1:]))


@pytest.mark.parametrize('invalid,state', [(1,'done'),(2,'empty')])
def test_invalid_candidates_are_skipped_without_repeating_paid_calls(setup, invalid, state):
    settings,uploads,book,_,store=setup
    api=FakeAPI(invalid=invalid)
    store.enqueue(book.id,settings,AnalysisOptions())
    assert run_analysis_once(uploads,settings=settings,api_factory=lambda _:api)
    run=store.latest(book.id,include_result=True)
    assert run['state']==state
    assert sum(name=='Candidates' for name,_,_ in api.calls)==2
    assert len(run['result'].rejected_candidates)==invalid
    assert len(run['result'].quotes)==2-invalid


def test_mixed_batch_keeps_valid_quote_and_reports_each_invalid_source(setup):
    chapter=setup[3].result.chapters[0].model_copy(deep=True)
    chapter.source_text=TEXT+' '+TEXT+' '+TEXT2
    chapter.paragraphs[0]['end']=len(chapter.source_text)
    batch=validate_quote_batch(chapter,Chunk(0,len(chapter.source_text)),Candidates(candidates=[
        candidate(TEXT2),candidate(),candidate(TEXT2,'p999999'),candidate('Ein frei erfundener Text ohne Originalfundstelle.')]),AnalysisOptions())
    assert [q.text for q in batch.quotes]==[TEXT2]
    assert len(batch.rejected_candidates)==3
    assert 'mehrfach' in batch.rejected_candidates[0].reason
    assert 'unbekannten Absatz' in batch.rejected_candidates[1].reason
    assert 'nicht wortgetreu' in batch.rejected_candidates[2].reason


def test_resume_keeps_quote_batches_and_diagnostics_and_does_not_replay_progress(setup,monkeypatch):
    settings,uploads,book,_,store=setup
    options=AnalysisOptions()
    run_id=store.enqueue(book.id,settings,options)
    first=FakeAPI(invalid=1,fail_at=5)
    run_analysis_once(uploads,settings=settings,api_factory=lambda _:first)
    assert store.latest(book.id)['state']=='failed'
    assert store.latest(book.id)['completed_steps']==4
    assert store.enqueue(book.id,settings,options)==run_id
    stages=[]
    original=Checkpoints.progress
    def progress(self,stage):
        stages.append(stage)
        return original(self,stage)
    monkeypatch.setattr(Checkpoints,'progress',progress)
    second=FakeAPI()
    run_analysis_once(uploads,settings=settings,api_factory=lambda _:second)
    assert [name for name,_,_ in second.calls]==['Candidates']
    assert stages==['Kapitel 2 von 2: Zitate auswählen, Abschnitt 1 von 1','Fundstellen zusammenführen und Ergebnis speichern']
    run=store.latest(book.id,include_result=True)
    assert run['state']=='done'
    assert len(run['result'].rejected_candidates)==1
    assert [q.text for q in run['result'].quotes]==[TEXT2]
    with TestClient(create_app(settings,start_worker=False),base_url='http://127.0.0.1:8000') as client:
        page=client.get(f'/books/local/{book.id}').text
        assert '1 Zitatvorschläge wegen unklarer Originalfundstelle übersprungen' in page
        assert 'nicht wortgetreu' in page


def test_legacy_quote_checkpoint_remains_compatible():
    batch=QuoteBatch.model_validate_json('{"quotes":[]}',strict=True)
    assert batch.rejected_candidates==[]


def test_timeout_resume_reuses_summaries_profile_and_pins_model(setup):
    settings,uploads,book,_,store=setup
    first=FakeAPI(fail_at=4)
    options=AnalysisOptions()
    run_id=store.enqueue(book.id,settings,options)
    run_analysis_once(uploads,settings=settings,api_factory=lambda _:first)
    assert store.latest(book.id)['state']=='failed'
    assert store.latest(book.id)['completed_steps']==3
    assert store.enqueue(book.id,settings,options)==run_id
    second=FakeAPI()
    runtime=settings.model_copy(update={'openwebui_model':'other-model'})
    captured=[]
    def factory(config):
        captured.append(config.openwebui_model)
        return second
    run_analysis_once(uploads,settings=runtime,api_factory=factory)
    assert captured==['test-model']
    assert [name for name,_,_ in second.calls]==['Candidates','Candidates']
    assert store.latest(book.id)['state']=='done'


@pytest.mark.parametrize('empty,spoiler', [(True,'none'),(False,'high')])
def test_zero_usable_quotes_is_an_explicit_completed_empty_result(setup,empty,spoiler):
    settings,uploads,book,_,store=setup
    store.enqueue(book.id,settings,AnalysisOptions())
    run_analysis_once(uploads,settings=settings,api_factory=lambda _:FakeAPI(empty=empty,spoiler=spoiler))
    run=store.latest(book.id,include_result=True)
    assert run['state']=='empty' and run['result'] is not None
    assert not any(q.usable for q in run['result'].quotes)


def test_call_budget_is_persistent_across_retries(setup):
    settings,uploads,book,_,store=setup
    options=AnalysisOptions(max_calls=2)
    store.enqueue(book.id,settings,options)
    api=FakeAPI()
    run_analysis_once(uploads,settings=settings,api_factory=lambda _:api)
    assert len(api.calls)==2 and store.latest(book.id)['state']=='failed'
    store.enqueue(book.id,settings,options)
    run_analysis_once(uploads,settings=settings,api_factory=lambda _:api)
    assert len(api.calls)==2


def test_review_gate_stale_run_and_expired_token(setup):
    settings,uploads,book,record,store=setup
    options=AnalysisOptions()
    store.enqueue(book.id,settings,options)
    old=store.claim(now=time.time()-LEASE_SECONDS-1)
    fresh=store.claim()
    assert old['token']!=fresh['token']
    with pytest.raises(AnalysisLeaseLost):Checkpoints(store,old,options).reserve_call()
    assert not store.finish(old,error='Old worker')
    ExtractionStore(uploads).correct(book.id,record.revision,record.result.boundaries)
    assert store.latest(book.id)['state']=='stale'
    assert not store.heartbeat(fresh)
    assert not store.finish(fresh,error='Old text')
    assert store.enqueue(book.id,settings,options)!=old['id']
    with closing(store.connection()) as connection, connection:
        row=connection.execute('select result_json from local_extractions where book_id=?',(book.id,)).fetchone()
        source=ExtractionStore._decode(row[0])
        from bookpromo.extraction import Warning
        source.warnings.append(Warning(code='objects',message='Test',blocking=True))
        connection.execute('update local_extractions set result_json=? where book_id=?',(source.model_dump_json(),book.id))
    with pytest.raises(UploadError,match='Hinweise'):store.enqueue(book.id,settings,options)


def test_import_and_analysis_claims_share_one_active_slot(setup):
    settings,uploads,book,_,store=setup
    other,_=uploads.save(BytesIO(package(p('Kapitel 1')+p(TEXT2))),'Anderes Buch.docx')
    jobs=JobStore(uploads)
    jobs.enqueue(other)
    store.enqueue(book.id,settings,AnalysisOptions())
    claim=store.claim()
    assert jobs.claim() is None
    assert store.finish(claim,error='Test')
    assert jobs.claim()
    store.enqueue(book.id,settings,AnalysisOptions())
    assert store.claim() is None


def test_web_analysis_start_status_quotes_and_invalidation(setup):
    settings,uploads,book,record,store=setup
    url=f'/books/local/{book.id}'
    with TestClient(create_app(settings,start_worker=False),base_url='http://127.0.0.1:8000') as client:
        assert 'KI-Analyse starten' in client.get(url).text
        assert store.latest(book.id) is None  # Viewing the book never triggers AI calls.
        assert client.post(url+'/analyze',headers={'Origin':'https://foreign.example'}).status_code==403
        assert client.post(url+'/analyze').status_code==200
        status=client.get(url+'/analysis-status').json()
        assert status['state']=='queued'
        assert not {'token','source_json','options_json','endpoint_hash'} & status.keys()
        run_analysis_once(uploads,settings=settings,api_factory=lambda _:FakeAPI())
        page=client.get(url).text
        assert 'KI-Analyse abgeschlossen' in page and 'Bildprompt-Basis' in page
        chapter=client.get(url+'/chapters/'+record.result.chapters[0].id).text
        assert TEXT in chapter and 'Wortgetreu geprüft' in chapter and 'Nutzbar' in chapter
        ExtractionStore(uploads).correct(book.id,record.revision,record.result.boundaries)
        assert 'Analyse veraltet' in client.get(url).text
        assert 'Wortgetreu geprüft' not in client.get(url+'/chapters/'+record.result.chapters[0].id).text


def test_hierarchical_context_includes_late_chapters_and_bounds_requests(setup):
    settings,uploads,_,_,store=setup
    body=''.join(p(f'Kapitel {i}')+''.join(p(f'Absatz {j}: '+TEXT) for j in range(35)) for i in range(1,8))
    body += p('ENDE_DES_BUCHS: '+TEXT2)
    book,_=uploads.save(BytesIO(package(body)),'Lang.docx')
    ExtractionStore(uploads).extract(book)
    api=FakeAPI(empty=True)
    store.enqueue(book.id,settings,AnalysisOptions(chunk_chars=2000))
    run_analysis_once(uploads,settings=settings,api_factory=lambda _:api)
    result=store.latest(book.id,include_result=True)
    assert result['state']=='empty'
    assert len(result['result'].chapter_summaries)==7
    assert any('ENDE_DES_BUCHS' in data.get('source_text','') for name,data,_ in api.calls if name=='Summary')
    assert any('summaries' in data for name,data,_ in api.calls if name=='Summary')
    assert max(len(json.dumps(data)) for _,data,_ in api.calls)<30000


def test_cancelled_inflight_request_saves_no_result(setup):
    settings,uploads,book,_,store=setup
    stop=threading.Event()
    class StoppingAPI:
        async def complete_json(self,*args,**kwargs):
            stop.set()
            await asyncio.sleep(10)
    store.enqueue(book.id,settings,AnalysisOptions())
    started=time.monotonic()
    run_analysis_once(uploads,stop=stop,settings=settings,api_factory=lambda _:StoppingAPI())
    assert time.monotonic()-started<3
    assert store.latest(book.id)['completed_steps']==0


def test_mismatched_endpoint_stops_before_network_and_secrets_are_not_persisted(setup):
    settings,uploads,book,_,store=setup
    store.enqueue(book.id,settings,AnalysisOptions())
    changed=settings.model_copy(update={'openwebui_url':'https://other.invalid'})
    run_analysis_once(uploads,settings=changed,api_factory=lambda _:pytest.fail('Unexpected external request'))
    assert store.latest(book.id)['state']=='failed'
    assert b'PRIVATE_KEY' not in uploads.db_path.read_bytes()


def test_separate_worker_loads_env_and_finishes_via_http(setup):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    settings,uploads,book,_,store=setup
    fake=FakeAPI()
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert body['model']=='test-model'
            props=body['response_format']['json_schema']['schema']['properties']
            model=Candidates if 'candidates' in props else BookProfile if 'internal_summary' in props else Summary
            value=asyncio.run(fake.complete_json(body['messages'][0]['content'],body['messages'][1]['content'],model))
            response=json.dumps({'choices':[{'message':{'content':value.model_dump_json()},'finish_reason':'stop'}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(response)))
            self.end_headers()
            self.wfile.write(response)
        def log_message(self,*args): pass
    with ThreadingHTTPServer(('127.0.0.1',0),Handler) as server:
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            settings._env_path.write_text(f'OPENWEBUI_URL=http://127.0.0.1:{server.server_port}\nOPENWEBUI_API_KEY=PRIVATE_KEY\nOPENWEBUI_MODEL=test-model\n',encoding='utf-8')
            settings=load_settings(settings._env_path)
            with TestClient(create_app(settings),base_url='http://127.0.0.1:8000') as client:
                url=f'/books/local/{book.id}'
                assert client.post(url+'/analyze').status_code==200
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    status=client.get(url+'/analysis-status').json()
                    if not status['active']: break
                    time.sleep(.05)
                assert status['state']=='done',status
                assert 'nutzbar' in client.get(url).text
        finally:
            server.shutdown()
            thread.join(timeout=3)
    assert len(fake.calls)==5
