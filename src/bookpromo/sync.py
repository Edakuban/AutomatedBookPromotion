"""Explicit atomic transfer of analyzed local books; no uploads on page views."""
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

from .analysis import AnalysisResult
from .book_assets import BookAssetStore, BookImageAsset
from .carousel_end_slide import CarouselRender, render_carousel_end_slide
from .database import DatabaseError
from .extraction_store import ExtractionStore
from .management import ManagementStore
from .overlay import OverlayAsset, OverlayError, OverlayStore
from .uploads import UploadError


@dataclass(frozen=True)
class SyncAssets:
    overlay: OverlayAsset | None
    carousel_end: CarouselRender | None


class SyncStore:
    def __init__(self, uploads):
        self.uploads=uploads

    def _snapshot(self, connection, book_id):
        management=ManagementStore(self.uploads)
        current=management._snapshot(connection,book_id)
        if not current['suggestion_id']:
            raise UploadError('Bitte zuerst die KI-Analyse für den aktuellen Textstand abschließen.',409)
        book=connection.execute('select * from local_books where id=?',(book_id,)).fetchone()
        source=connection.execute('select * from local_extractions where book_id=?',(book_id,)).fetchone()
        extraction=ExtractionStore._decode(source['result_json'])
        if extraction.needs_review: raise UploadError('Bitte zuerst die offenen Importhinweise klären.',409)
        row=connection.execute('select result_json,prompt_version,model_id from local_analysis_runs where id=?',(current['suggestion_id'],)).fetchone()
        result=AnalysisResult.model_validate_json(row['result_json'])
        # Font and title color are local renderer inputs. n8n receives the
        # resulting private Storage paths, never a local font identifier.
        remote_details = current['details'].model_dump(exclude={'overlay_title_font', 'overlay_title_color'})
        payload={'book':{'id':book_id,**remote_details},
            'version':{'id':book['version_id'],'filename':book['filename'],'file_sha256':book['file_sha256'],
                'extraction_revision':source['revision'],'summary':result.profile.internal_summary,
                'analysis_version':row['prompt_version']+':'+row['model_id']},
            'chapters':[{'id':c.id,'position':c.position,'name':c.title,'source_text':c.source_text,'paragraphs':c.paragraphs,
                'summary':result.chapter_summaries[c.id].summary if c.id in result.chapter_summaries else ''} for c in extraction.chapters],
            'quotes':[{'id':q['quote'].id,'chapter_id':q['quote'].chapter_id,'text':q['quote'].text,
                'source_start':q['quote'].source_start,'source_end':q['quote'].source_end,
                'context':q['quote'].context_before+'['+q['quote'].text+']'+q['quote'].context_after,
                'scores':q['quote'].scores.model_dump(),'reason':q['quote'].reason,'spoiler':q['quote'].spoiler,
                'approved':q['quote'].usable,'blocked':q['blocked']} for q in current['quotes']]}
        encoded=json.dumps(payload,sort_keys=True,ensure_ascii=False).encode('utf-8')
        if len(encoded)>32*1024*1024: raise UploadError('Dieses Buch überschreitet das Übertragungslimit von 32 MB.',413)
        payload['hash']=hashlib.sha256(encoded).hexdigest()
        return payload,current

    def snapshot(self, book_id):
        with closing(self.uploads._read_connection()) as connection:
            connection.execute('begin')
            payload,_=self._snapshot(connection,book_id)
            return payload

    def prepare_transfer(self, book_id):
        """Freeze local settings and assets while rendering the sync artifacts."""
        asset_store=BookAssetStore(self.uploads)
        overlay_store=OverlayStore(self.uploads)
        with closing(sqlite3.connect(self.uploads.db_path,timeout=15)) as connection:
            connection.row_factory=sqlite3.Row
            connection.execute('pragma foreign_keys=on')
            connection.execute('begin immediate')
            payload,current=self._snapshot(connection,book_id)
            details=current['details']
            try:
                overlay=overlay_store.prepare(details)
            except OverlayError as exc:
                raise UploadError(str(exc),409) from None

            assets={}
            if BookAssetStore._table_exists(connection):
                rows=connection.execute(
                    'select * from local_book_assets where book_id=? order by kind',(book_id,)
                ).fetchall()
                assets={row['kind']:BookImageAsset.from_row(row) for row in rows}

            carousel_end=None
            missing=[kind for kind in ('cover_front','logo') if kind not in assets]
            if details.promotion_enabled and overlay is None:
                raise UploadError('Bitte zuerst das Buchtitel-Overlay konfigurieren.',409)
            if details.promotion_enabled and missing:
                raise UploadError('Für die Promotion bitte zuerst: '+', '.join(
                    'Frontcover hochladen' if kind=='cover_front' else 'Logo hochladen' for kind in missing
                )+'.',409)
            if overlay is not None and not missing and details.carousel_end_text.strip():
                try:
                    carousel_end=render_carousel_end_slide(
                        details,asset_store.open_image(assets['cover_front']),
                        asset_store.open_image(assets['logo']),overlay,
                    )
                except OverlayError as exc:
                    raise UploadError(str(exc),409) from None
            elif details.promotion_enabled:
                raise UploadError('Bitte einen Text für die Schlussseite eintragen.',409)

            if overlay is not None:
                overlay_store.save(book_id,overlay)
            return payload,SyncAssets(overlay=overlay,carousel_end=carousel_end)

    def receipt(self,book_id):
        if not self.uploads.db_path.is_file(): return None
        with closing(self.uploads._read_connection()) as connection:
            if not ManagementStore.exists(connection,'local_sync_receipts'): return None
            row=connection.execute('select * from local_sync_receipts where book_id=?',(book_id,)).fetchone()
            return dict(row) if row else None

    async def transfer(self,book_id,repository):
        from starlette.concurrency import run_in_threadpool
        payload,assets=await run_in_threadpool(self.prepare_transfer,book_id)
        payload['book']['overlay_path']=''
        payload['book']['carousel_end_slide_path']=''
        await repository.check_schema()
        if assets.overlay is not None:
            object_path=f"{book_id}/{assets.overlay.digest}.png"
            payload['book']['overlay_path']=await repository.upload_overlay(object_path,assets.overlay.data)
        if assets.carousel_end is not None:
            content_digest=hashlib.sha256(assets.carousel_end.data).hexdigest()
            object_path=f"{book_id}/carousel/{content_digest}.jpg"
            payload['book']['carousel_end_slide_path']=await repository.upload_carousel_end_slide(
                object_path,assets.carousel_end.data
            )
        encoded=json.dumps({key:value for key,value in payload.items() if key!='hash'},sort_keys=True,ensure_ascii=False).encode('utf-8')
        payload['hash']=hashlib.sha256(encoded).hexdigest()
        receipt=await run_in_threadpool(self.receipt,book_id)
        result=await repository.rpc('bookpromo_sync',{'p':payload,'expected_revision':receipt['revision'] if receipt else 0})
        if type(result.get('revision')) is not int or result['revision']<1 or result.get('hash')!=payload['hash']:
            raise DatabaseError('response')
        await run_in_threadpool(self.save_receipt,book_id,result)
        return result

    def save_receipt(self,book_id,result):
        with closing(sqlite3.connect(self.uploads.db_path,timeout=10)) as connection,connection:
            connection.execute('create table if not exists local_sync_receipts (book_id text primary key,revision integer not null,hash text not null,synced_at text not null)')
            connection.execute('''insert into local_sync_receipts values (?,?,?,?) on conflict(book_id) do update
                set revision=excluded.revision,hash=excluded.hash,synced_at=excluded.synced_at
                where excluded.revision>=local_sync_receipts.revision''',
                (book_id,result['revision'],result['hash'],datetime.now(timezone.utc).isoformat()))
