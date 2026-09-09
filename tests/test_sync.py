import asyncio
import json
import httpx
from pydantic import SecretStr
import pytest
from fastapi.testclient import TestClient
from bookpromo.database import SCHEMA_VERSION, SupabaseRepository, DatabaseError
from bookpromo.management import ManagementStore
from bookpromo.overlay import OverlayAsset, OverlayError, OverlayStore
from bookpromo.sync import SyncStore
from bookpromo.uploads import UploadError
from bookpromo.web import create_app
from test_analysis import setup
from test_management import analyzed


def repository(settings, handler):
    settings.supabase_enabled=True
    settings.supabase_url='https://example.supabase.co'
    settings.supabase_secret_key=SecretStr('sb_secret_PRIVATE')
    return SupabaseRepository(settings,transport=httpx.MockTransport(handler))


def test_snapshot_requires_current_analysis_and_tracks_local_changes(setup):
    _,uploads,book,_,_=setup
    store=SyncStore(uploads)
    with pytest.raises(UploadError):store.snapshot(book.id)
    managed=analyzed(setup)
    before=store.snapshot(book.id)
    assert before==store.snapshot(book.id)
    q=managed.get(book.id)['quotes'][0]['quote']
    managed.set_blocked(book.id,q.chapter_id,managed.get(book.id)['suggestion_id'],q.id,0,True)
    after=store.snapshot(book.id)
    assert after['hash']!=before['hash'] and any(q['blocked'] for q in after['quotes'])
    assert 'PRIVATE' not in json.dumps(after)
    current = managed.get(book.id)
    managed.save(book.id, current['revision'], current['details'].model_copy(update={
        'overlay_title_font': 'Bebas Neue', 'overlay_title_color': '#102030'}), current['suggestion_id'])
    local_overlay = store.snapshot(book.id)
    assert 'overlay_title_font' not in local_overlay['book']
    assert 'overlay_title_color' not in local_overlay['book']


def test_atomic_transfer_and_idempotent_retry_after_uncertain_response(setup):
    settings,uploads,book,_,_=setup
    analyzed(setup); store=SyncStore(uploads); remote={}; calls=[]
    def handler(request):
        if request.method=='GET':return httpx.Response(200,json=[{'version':SCHEMA_VERSION}])
        data=json.loads(request.content);calls.append(data)
        assert request.url.path.endswith('/rpc/bookpromo_sync')
        assert request.headers['apikey']=='sb_secret_PRIVATE'
        if not remote:
            remote.update(revision=1,hash=data['p']['hash'])
            raise httpx.ReadTimeout('Private details',request=request)
        return httpx.Response(200,json=remote)
    repo=repository(settings,handler)
    with pytest.raises(DatabaseError):asyncio.run(store.transfer(book.id,repo))
    assert store.receipt(book.id) is None
    asyncio.run(store.transfer(book.id,repo))
    assert calls[0]==calls[1]
    assert store.receipt(book.id)['revision']==1


def test_sync_ui_origin_receipt_and_no_upload_on_view(setup):
    settings,uploads,book,_,_=setup
    analyzed(setup);writes=[]
    def handler(request):
        if request.method=='GET':return httpx.Response(200,json=[{'version':SCHEMA_VERSION}])
        data=json.loads(request.content);writes.append(data)
        return httpx.Response(200,json={'revision':1,'hash':data['p']['hash']})
    repo=repository(settings,handler)
    with TestClient(create_app(settings,repository=repo,start_worker=False),base_url='http://127.0.0.1:8000') as client:
        url=f'/books/local/{book.id}'
        assert 'nach Supabase übertragen' in client.get(url).text and not writes
        assert client.post(url+'/sync',headers={'Origin':'https://foreign.example'}).status_code==403
        assert client.post(url+'/sync').status_code==200
        assert 'Übertragung bestätigt' in client.get(url).text and len(writes)==1


def test_transfer_uploads_overlay_then_sends_private_object_path(setup, monkeypatch):
    settings,uploads,book,_,_=setup
    analyzed(setup)
    asset = OverlayAsset(data=b'png-data', digest='a' * 64)
    monkeypatch.setattr(SyncStore, 'overlay_asset', lambda self, _: asset)
    seen = []
    def handler(request):
        seen.append(request)
        if request.url.path.endswith('/bookpromo_schema'):
            return httpx.Response(200, json=[{'version': SCHEMA_VERSION}])
        if request.url.path.endswith('/object/book-promotion-assets/' + str(book.id) + '/' + asset.digest + '.png'):
            assert request.content == asset.data
            return httpx.Response(200, json={})
        payload = json.loads(request.content)['p']
        assert payload['book']['overlay_path'] == str(book.id) + '/' + asset.digest + '.png'
        return httpx.Response(200, json={'revision': 1, 'hash': payload['hash']})
    asyncio.run(SyncStore(uploads).transfer(book.id, repository(settings, handler)))
    assert [request.method for request in seen] == ['POST', 'GET', 'POST']


def test_transfer_reports_a_missing_overlay_font_as_a_local_setting_error(setup, monkeypatch):
    _, uploads, book, _, _ = setup
    analyzed(setup)
    monkeypatch.setattr(
        OverlayStore,
        'prepare',
        lambda self, _: (_ for _ in ()).throw(OverlayError('Die gewählte Schriftart ist lokal nicht installiert.')),
    )
    with pytest.raises(UploadError, match='Schriftart') as error:
        asyncio.run(SyncStore(uploads).transfer(book.id, object()))
    assert error.value.status == 409


def test_rpc_errors_are_sanitized_and_do_not_save_receipt(setup):
    settings,uploads,book,_,_=setup
    analyzed(setup)
    repo=repository(settings,lambda r:httpx.Response(409,json={'message':'PRIVATE'}))
    with pytest.raises(DatabaseError) as error:
        asyncio.run(repo.rpc('bookpromo_sync',{}))
    assert error.value.code=='conflict' and 'PRIVATE' not in str(error.value)
    assert SyncStore(uploads).receipt(book.id) is None


def test_history_shows_only_confirmed_remote_usage(setup):
    settings,uploads,book,_,_=setup
    managed=analyzed(setup)
    q=managed.get(book.id)['quotes'][0]['quote']
    def handler(request):
        if request.url.path.endswith('bookpromo_schema'):return httpx.Response(200,json=[{'version':SCHEMA_VERSION}])
        return httpx.Response(200,json=[{'id':q.id,'last_published_at':'2026-09-08T04:00:00Z','reserved':False}])
    repo=repository(settings,handler)
    with TestClient(create_app(settings,repository=repo,start_worker=False),base_url='http://127.0.0.1:8000') as client:
        url=f'/books/local/{book.id}/chapters/{q.chapter_id}'
        assert '08.09.2026, 06:00' in client.get(url+'?filter=used').text
        assert 'quote-card' not in client.get(url+'?filter=unused').text
    repo=repository(settings,lambda request:httpx.Response(500,json={'private':'error'}))
    with TestClient(create_app(settings,repository=repo,start_worker=False),base_url='http://127.0.0.1:8000') as client:
        page=client.get(url+'?filter=unused').text
        assert 'quote-card' not in page and 'nur bestätigte Daten' in page
