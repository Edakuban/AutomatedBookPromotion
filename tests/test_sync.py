import asyncio
import hashlib
from io import BytesIO
import json
import httpx
from PIL import Image
from pydantic import SecretStr
import pytest
from fastapi.testclient import TestClient
from bookpromo.database import SCHEMA_VERSION, SupabaseRepository, DatabaseError
from bookpromo.management import ManagementStore
from bookpromo.carousel_end_slide import CarouselRender
from bookpromo.book_assets import BookAssetStore
from bookpromo.overlay import OverlayAsset, OverlayError, OverlayStore
from bookpromo.sync import SyncAssets, SyncStore
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
    assert local_overlay['book']['publication_mode'] == 'review'
    assert local_overlay['book']['carousel_end_text'] == ''


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
    data = b'\x89PNG\r\n\x1a\nrendered-overlay'
    asset = OverlayAsset(data=data, digest=hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(OverlayStore, 'prepare', lambda self, _: asset)
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
    assert [request.method for request in seen] == ['GET', 'POST', 'POST']


def test_transfer_uploads_overlay_and_carousel_end_before_atomic_sync(setup, monkeypatch):
    settings,uploads,book,_,_=setup
    analyzed(setup)
    store=SyncStore(uploads)
    overlay_data=b'\x89PNG\r\n\x1a\noverlay'
    carousel_data=b'\xff\xd8carousel\xff\xd9'
    overlay=OverlayAsset(data=overlay_data,digest=hashlib.sha256(overlay_data).hexdigest())
    # CarouselRender.digest identifies renderer inputs/cache state and is not
    # necessarily the SHA-256 of the resulting JPEG bytes.
    carousel=CarouselRender(data=carousel_data,digest='f'*64)
    carousel_content_digest=hashlib.sha256(carousel_data).hexdigest()
    payload=store.snapshot(book.id)
    monkeypatch.setattr(
        SyncStore,'prepare_transfer',lambda self,_:(payload,SyncAssets(overlay=overlay,carousel_end=carousel))
    )
    seen=[]

    def handler(request):
        seen.append(request)
        if request.url.path.endswith('/bookpromo_schema'):
            return httpx.Response(200,json=[{'version':SCHEMA_VERSION}])
        if '/storage/v1/object/' in request.url.path:
            return httpx.Response(200,json={})
        synced=json.loads(request.content)['p']
        assert synced['book']['overlay_path']==f'{book.id}/{overlay.digest}.png'
        assert synced['book']['carousel_end_slide_path']==f'{book.id}/carousel/{carousel_content_digest}.jpg'
        assert synced['book']['publication_mode']=='review'
        assert 'cover_front' not in synced['book'] and 'logo' not in synced['book']
        return httpx.Response(200,json={'revision':1,'hash':synced['hash']})

    asyncio.run(store.transfer(book.id,repository(settings,handler)))
    assert [request.method for request in seen]==['GET','POST','POST','POST']
    assert seen[1].headers['content-type']=='image/png'
    assert seen[2].headers['content-type']=='image/jpeg'


def test_prepare_transfer_renders_from_the_frozen_profile_and_local_assets(setup, monkeypatch):
    _,uploads,book,_,_=setup
    managed=analyzed(setup)
    current=managed.get(book.id)
    details=current['details'].model_copy(update={
        'publication_mode':'auto','carousel_end_text':'Jetzt entdecken.',
        'overlay_title_font':'Test Font',
    })
    managed.save(book.id,current['revision'],details,current['suggestion_id'])

    def png(size,color):
        output=BytesIO()
        Image.new('RGB',size,color).save(output,format='PNG')
        return output.getvalue()

    local_assets=BookAssetStore(uploads)
    local_assets.save(book.id,'cover_front',BytesIO(png((800,1200),'navy')),'cover.png',0)
    local_assets.save(book.id,'logo',BytesIO(png((240,90),'white')),'logo.png',0)
    overlay_data=b'\x89PNG\r\n\x1a\nfrozen-overlay'
    overlay=OverlayAsset(data=overlay_data,digest=hashlib.sha256(overlay_data).hexdigest())
    carousel_data=b'\xff\xd8frozen-carousel\xff\xd9'
    carousel=CarouselRender(data=carousel_data,digest=hashlib.sha256(carousel_data).hexdigest())
    monkeypatch.setattr(OverlayStore,'prepare',lambda self,value: overlay)

    def render(value,cover,logo,title_overlay):
        assert value.publication_mode=='auto' and value.carousel_end_text=='Jetzt entdecken.'
        assert cover.size==(800,1200) and logo.size==(240,90)
        assert title_overlay==overlay
        return carousel

    monkeypatch.setattr('bookpromo.sync.render_carousel_end_slide',render)
    payload,prepared=SyncStore(uploads).prepare_transfer(book.id)
    assert payload['book']['publication_mode']=='auto'
    assert payload['book']['carousel_end_text']=='Jetzt entdecken.'
    assert 'overlay_path' not in payload['book'] and 'carousel_end_slide_path' not in payload['book']
    assert prepared==SyncAssets(overlay=overlay,carousel_end=carousel)


def test_schema_is_checked_before_any_storage_upload(setup, monkeypatch):
    settings,uploads,book,_,_=setup
    analyzed(setup)
    store=SyncStore(uploads)
    data=b'\x89PNG\r\n\x1a\noverlay'
    overlay=OverlayAsset(data=data,digest=hashlib.sha256(data).hexdigest())
    payload=store.snapshot(book.id)
    monkeypatch.setattr(
        SyncStore,'prepare_transfer',lambda self,_:(payload,SyncAssets(overlay=overlay,carousel_end=None))
    )
    seen=[]
    def handler(request):
        seen.append(request)
        return httpx.Response(200,json=[{'version':4}])
    with pytest.raises(DatabaseError) as error:
        asyncio.run(store.transfer(book.id,repository(settings,handler)))
    assert error.value.code=='schema'
    assert len(seen)==1 and seen[0].method=='GET'


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
