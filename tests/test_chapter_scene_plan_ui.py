"""Chapter planning is explicit, source-fenced and preserves reviewed media."""
from io import BytesIO
import json
import sqlite3
from urllib.parse import urlencode

from fastapi.testclient import TestClient
import pytest

from bookpromo import web
from bookpromo.characters import CharacterStore
from bookpromo.chapter_teasers import ChapterTeaserStore
from bookpromo.management import ManagementStore
from bookpromo.openwebui import OpenWebUIError
from bookpromo.scene_plan import decode_saved_plan, encode_saved_plan, scene_plan_fingerprint
from test_analysis import setup
from test_chapter_teasers import ready_chapter_images
from test_reel_generation import resolved_reference_plan
from test_reels import png_bytes


MEDIA_FIELDS = (
    'scene_image_path', 'scene_image_sha256', 'optimized_image_path', 'optimized_image_sha256',
    'selected_image_path', 'selected_image_sha256', 'selected_image_source',
    'selected_video_path', 'selected_video_sha256', 'image_stale', 'video_stale',
)


def chapter_fixture(setup, *, save_plans=False, video=False):
    settings, uploads, book, record, reels, store, jobs, run = ready_chapter_images(setup, with_reference=True)
    character = CharacterStore(uploads).list(book.id)[0]
    for checkpoint in run['plans']:
        draft = reels.get_draft(checkpoint['draft_id'])
        store.save_prompt(
            book.id, run['id'], draft.id, draft.revision,
            image_prompt=draft.image_prompt, teaser_text=draft.caption_addition,
            character_ids=[character.id],
        )
        if video:
            draft = reels.get_draft(draft.id)
            jobs.enqueue(draft.id, 'video')
            job = jobs.claim(kinds={'video'})
            path, digest = reels.save_artifact(draft.id, 'video', BytesIO(b'\x00\x00\x00\x18ftypisom-old-clean-clip'), 'old.mp4')
            assert jobs.finish(job, result={'path': path, 'sha256': digest})
        if save_plans:
            save_current_plan(uploads, book, reels, store, run, checkpoint)
    base = f'/books/local/{book.id}/teaser'
    return settings, uploads, book, record, reels, store, jobs, store.latest(book.id, include_plans=True), base


def plan_json_for(uploads, draft):
    selected = [character for character in CharacterStore(uploads).list(draft.book_id) if character.id in draft.character_ids]
    plan = resolved_reference_plan([character.name for character in selected])
    fingerprint = scene_plan_fingerprint(
        quote=draft.quote_text, image_prompt=draft.image_prompt, scene_direction=draft.scene_direction,
        art_direction=ManagementStore(uploads).get(draft.book_id)['details'].image_prompt_base,
        characters=selected,
    )
    return encode_saved_plan(plan, fingerprint)


def save_current_plan(uploads, book, reels, store, run, checkpoint):
    draft = reels.get_draft(checkpoint['draft_id'])
    return store.save_scene_plan(book.id, run['id'], draft.id, draft.revision, plan_json_for(uploads, draft))


def assert_media_preserved(reels, before, after):
    for field in MEDIA_FIELDS:
        assert getattr(after, field) == getattr(before, field), field
    assert reels.artifact_path(after, 'image').read_bytes() == reels.artifact_path(before, 'image').read_bytes()
    if before.selected_video_path:
        assert reels.artifact_path(after, 'video').read_bytes() == reels.artifact_path(before, 'video').read_bytes()


def forbid_ai(monkeypatch):
    monkeypatch.setattr(web, 'create_text_client', lambda *args: pytest.fail('Queueing/saving must not invoke text AI'))


def test_chapter_plan_generation_is_explicit_and_preserves_selected_media(setup, monkeypatch):
    settings, uploads, book, _, reels, store, jobs, run, base = chapter_fixture(setup, video=True)
    checkpoint = run['plans'][0]
    before = reels.get_draft(checkpoint['draft_id'])
    other_before = reels.get_draft(run['plans'][1]['draft_id'])
    calls = []
    sentinel = object()
    monkeypatch.setattr(web, 'create_text_client', lambda *args: sentinel)

    async def generate(client, **kwargs):
        assert client is sentinel
        assert kwargs['quote'] == before.quote_text
        assert kwargs['image_prompt'] == before.image_prompt
        assert kwargs['scene_direction'] == before.scene_direction
        assert kwargs['characters'] == ({'name': 'Niemand', 'aliases': []},)
        calls.append(kwargs)
        return resolved_reference_plan(['Niemand'])

    monkeypatch.setattr(web, 'generate_scene_plan', generate)
    previous_jobs = jobs.status(before.id)
    route = base + f"/chapters/{checkpoint['chapter_id']}/plan/generate"
    with TestClient(web.create_app(settings, start_worker=False), base_url='http://127.0.0.1:8000') as client:
        page = client.get(base)
        assert page.status_code == 200 and route in page.text
        assert '/plan/save' in page.text and 'name="scene_direction"' in page.text
        assert 'value="planned_scene"' in page.text
        assert client.post(route, data={'revision': before.revision, 'ai_provider': 'openwebui'}, follow_redirects=False).status_code == 303
    assert len(calls) == 1
    after = reels.get_draft(before.id)
    assert decode_saved_plan(after.scene_plan_json).plan == resolved_reference_plan(['Niemand'])
    assert_media_preserved(reels, before, after)
    assert jobs.status(before.id) == previous_jobs
    assert reels.get_draft(other_before.id) == other_before


@pytest.mark.parametrize('clear_cast', [False, True])
def test_chapter_direction_and_cast_save_invalidates_plan_preserving_selected_media(setup, monkeypatch, clear_cast):
    settings, uploads, book, _, reels, _, jobs, run, base = chapter_fixture(setup, save_plans=True, video=True)
    checkpoint = run['plans'][0]
    before = reels.get_draft(checkpoint['draft_id'])
    characters = CharacterStore(uploads)
    extra = characters.create(book.id, name='Copper Fox', image_prompt='A copper fox reference.')
    extra = characters.save_reference_file(book.id, extra.id, extra.revision, png_bytes((256, 256)))
    forbid_ai(monkeypatch)
    previous_jobs = jobs.status(before.id)
    with TestClient(web.create_app(settings, start_worker=False), base_url='http://127.0.0.1:8000') as client:
        fields = {
            'revision': before.revision, 'scene_direction': '  Copper Fox leans towards the window.  ',
        }
        if not clear_cast:
            fields['character_id'] = [extra.id]
        response = client.post(base + f"/chapters/{checkpoint['chapter_id']}/plan/save", data=fields, follow_redirects=False)
    assert response.status_code == 303
    after = reels.get_draft(before.id)
    assert after.character_ids == (() if clear_cast else (extra.id,))
    assert after.scene_direction == 'Copper Fox leans towards the window.'
    assert after.scene_plan_json == ''
    assert_media_preserved(reels, before, after)
    assert jobs.status(before.id) == previous_jobs


@pytest.mark.parametrize('batch', [False, True])
def test_chapter_planned_single_and_batch_queue_exact_saved_plans_without_text_ai(setup, monkeypatch, batch):
    settings, _, _, _, reels, store, jobs, run, base = chapter_fixture(setup, save_plans=True)
    before = {checkpoint['draft_id']: reels.get_draft(checkpoint['draft_id']) for checkpoint in run['plans']}
    forbid_ai(monkeypatch)
    route = base + ('/chapters/images/optimize' if batch else f"/chapters/{run['plans'][0]['chapter_id']}/image/optimize")
    fields = {'strategy': 'planned_scene'}
    if not batch:
        fields['revision'] = before[run['plans'][0]['draft_id']].revision
    with TestClient(web.create_app(settings, start_worker=False), base_url='http://127.0.0.1:8000') as client:
        assert client.post(route, data=fields, follow_redirects=False).status_code == 303
    with jobs.connection() as connection:
        queued = connection.execute("select draft_id,payload_json from local_reel_jobs where state='queued' order by draft_id").fetchall()
    expected_ids = set(before) if batch else {run['plans'][0]['draft_id']}
    assert {row['draft_id'] for row in queued} == expected_ids
    for row in queued:
        draft = before[row['draft_id']]
        assert json.loads(row['payload_json']) == {
            'operation': 'optimize', 'strategy': 'planned_scene', 'scene_plan_json': draft.scene_plan_json,
        }
        assert_media_preserved(reels, draft, reels.get_draft(draft.id))


@pytest.mark.parametrize('batch', [False, True])
@pytest.mark.parametrize('invalid', ['missing', 'stale', 'style', 'cast'])
def test_chapter_planned_queue_rejects_missing_or_changed_plan_atomically(setup, monkeypatch, batch, invalid):
    settings, uploads, book, _, reels, _, jobs, run, base = chapter_fixture(setup, save_plans=True)
    # A bad later chapter must roll back the first chapter's bulk enqueue too.
    checkpoint = run['plans'][1 if batch else 0]
    draft = reels.get_draft(checkpoint['draft_id'])
    if invalid in {'missing', 'stale'}:
        value = '' if invalid == 'missing' else encode_saved_plan(decode_saved_plan(draft.scene_plan_json).plan, '0' * 64)
        with sqlite3.connect(uploads.db_path) as connection, connection:
            connection.execute('update local_reel_drafts set scene_plan_json=? where id=?', (value, draft.id))
    elif invalid == 'style':
        original = web.ManagementStore.get

        def changed_style(self, book_id, **kwargs):
            profile = original(self, book_id, **kwargs)
            profile['details'] = profile['details'].model_copy(update={'image_prompt_base': 'Changed rendering style'})
            return profile

        monkeypatch.setattr(web.ManagementStore, 'get', changed_style)
    else:
        characters = CharacterStore(uploads)
        character = characters.list(book.id)[0]
        characters.save(book.id, character.id, character.revision, name=character.name,
                        aliases=['New alias'], description=character.description, image_prompt=character.image_prompt, approved=True)
    forbid_ai(monkeypatch)
    previous = {checkpoint['draft_id']: reels.get_draft(checkpoint['draft_id']) for checkpoint in run['plans']}
    previous_jobs = {draft_id: jobs.status(draft_id) for draft_id in previous}
    route = base + ('/chapters/images/optimize' if batch else f"/chapters/{checkpoint['chapter_id']}/image/optimize")
    fields = {'strategy': 'planned_scene'}
    if not batch:
        fields['revision'] = draft.revision
    with TestClient(web.create_app(settings, start_worker=False), base_url='http://127.0.0.1:8000') as client:
        assert client.post(route, data=fields, follow_redirects=False).status_code == 409
    for draft_id, before in previous.items():
        assert reels.get_draft(draft_id) == before
        assert jobs.status(draft_id) == previous_jobs[draft_id]


@pytest.mark.parametrize('failure', ['revision', 'style', 'cast', 'busy'])
def test_chapter_plan_rejects_changes_during_text_ai_call(setup, monkeypatch, failure):
    settings, uploads, book, _, reels, _, jobs, run, base = chapter_fixture(setup)
    checkpoint = run['plans'][0]
    before = reels.get_draft(checkpoint['draft_id'])
    monkeypatch.setattr(web, 'create_text_client', lambda *args: object())

    async def generate(*args, **kwargs):
        if failure == 'revision':
            reels.update_draft(before.id, before.revision, caption_addition='Concurrent caption edit.')
        elif failure == 'style':
            profile = ManagementStore(uploads).get(book.id)
            ManagementStore(uploads).save(book.id, profile['revision'],
                profile['details'].model_copy(update={'image_prompt_base': 'Concurrent style edit'}), profile['suggestion_id'])
        elif failure == 'cast':
            characters = CharacterStore(uploads)
            character = characters.list(book.id)[0]
            characters.save(book.id, character.id, character.revision, name=character.name,
                            aliases=['Concurrent alias edit'], description=character.description, image_prompt=character.image_prompt, approved=True)
        else:
            jobs.enqueue(before.id, 'image', {'operation': 'scene'})
        return resolved_reference_plan(['Niemand'])

    monkeypatch.setattr(web, 'generate_scene_plan', generate)
    route = base + f"/chapters/{checkpoint['chapter_id']}/plan/generate"
    with TestClient(web.create_app(settings, start_worker=False), base_url='http://127.0.0.1:8000') as client:
        response = client.post(route, data={'revision': before.revision, 'ai_provider': 'openwebui'}, follow_redirects=False)
    assert response.status_code == 409
    after = reels.get_draft(before.id)
    assert after.scene_plan_json == before.scene_plan_json
    assert_media_preserved(reels, before, after)
    if failure == 'revision':
        assert after.caption_addition == 'Concurrent caption edit.'


@pytest.mark.parametrize('fields', [
    [('revision', '1')], [('revision', '1'), ('ai_provider', 'unknown')],
    [('revision', '1'), ('ai_provider', 'openwebui'), ('ai_provider', 'openwebui')],
    [('revision', '1'), ('ai_provider', 'openwebui'), ('unexpected', 'x')],
])
def test_chapter_plan_generation_rejects_invalid_contract_before_ai(setup, monkeypatch, fields):
    settings, _, _, _, reels, _, jobs, run, base = chapter_fixture(setup)
    before = reels.get_draft(run['plans'][0]['draft_id'])
    forbid_ai(monkeypatch)
    with TestClient(web.create_app(settings, start_worker=False), base_url='http://127.0.0.1:8000') as client:
        response = client.post(base + f"/chapters/{run['plans'][0]['chapter_id']}/plan/generate",
            content=urlencode(fields), headers={'Content-Type': 'application/x-www-form-urlencoded'}, follow_redirects=False)
    assert response.status_code == 400
    assert reels.get_draft(before.id) == before


@pytest.mark.parametrize('case', ['stale_revision', 'unsaved_cast', 'busy'])
def test_chapter_planned_single_rejects_stale_revision_unsaved_cast_or_busy_jobs(setup, monkeypatch, case):
    settings, uploads, book, _, reels, _, jobs, run, base = chapter_fixture(setup, save_plans=True)
    checkpoint = run['plans'][0]
    before = reels.get_draft(checkpoint['draft_id'])
    fields = {'revision': before.revision, 'strategy': 'planned_scene'}
    if case == 'stale_revision':
        fields['revision'] = before.revision - 1
    elif case == 'unsaved_cast':
        characters = CharacterStore(uploads)
        extra = characters.create(book.id, name='Copper Fox', image_prompt='A copper fox.')
        extra = characters.save_reference_file(book.id, extra.id, extra.revision, png_bytes((256, 256)))
        fields['character_id'] = [extra.id]
    else:
        jobs.enqueue(before.id, 'image', {'operation': 'scene'})
    forbid_ai(monkeypatch)
    previous_jobs = jobs.status(before.id)
    with TestClient(web.create_app(settings, start_worker=False), base_url='http://127.0.0.1:8000') as client:
        response = client.post(base + f"/chapters/{checkpoint['chapter_id']}/image/optimize", data=fields, follow_redirects=False)
    assert response.status_code == 409
    assert_media_preserved(reels, before, reels.get_draft(before.id))
    assert jobs.status(before.id) == previous_jobs


@pytest.mark.parametrize('case', ['missing_direction', 'long_direction', 'duplicate_direction', 'foreign_character', 'duplicate_character', 'unexpected'])
def test_chapter_plan_save_rejects_invalid_fields_without_mutation_or_ai(setup, monkeypatch, case):
    settings, _, _, _, reels, _, jobs, run, base = chapter_fixture(setup, save_plans=True)
    checkpoint = run['plans'][0]
    before = reels.get_draft(checkpoint['draft_id'])
    fields = [('revision', str(before.revision)), ('scene_direction', 'A new direction.'), ('character_id', before.character_ids[0])]
    if case == 'missing_direction':
        fields = [field for field in fields if field[0] != 'scene_direction']
    elif case == 'long_direction':
        fields[1] = ('scene_direction', 'x' * 2001)
    elif case == 'duplicate_direction':
        fields.append(('scene_direction', 'Conflicting direction.'))
    elif case == 'foreign_character':
        fields[2] = ('character_id', '00000000-0000-4000-8000-000000000001')
    elif case == 'duplicate_character':
        fields.append(fields[2])
    else:
        fields.append(('unexpected', 'x'))
    forbid_ai(monkeypatch)
    previous_jobs = jobs.status(before.id)
    with TestClient(web.create_app(settings, start_worker=False), base_url='http://127.0.0.1:8000') as client:
        response = client.post(base + f"/chapters/{checkpoint['chapter_id']}/plan/save",
            content=urlencode(fields), headers={'Content-Type': 'application/x-www-form-urlencoded'}, follow_redirects=False)
    assert response.status_code == (409 if case == 'foreign_character' else 400)
    assert reels.get_draft(before.id) == before
    assert jobs.status(before.id) == previous_jobs


@pytest.mark.parametrize('needed', [1, 2])
def test_explicit_batch_planning_calls_ai_once_per_missing_plan_and_preserves_media(setup, monkeypatch, needed):
    settings, _, _, _, reels, store, jobs, run, base = chapter_fixture(setup, save_plans=True, video=True)
    targets = run['plans'][-needed:]
    for checkpoint in targets:
        draft = reels.get_draft(checkpoint['draft_id'])
        store.save_prompt(draft.book_id, run['id'], draft.id, draft.revision,
            image_prompt=draft.image_prompt, teaser_text=draft.caption_addition,
            scene_direction=f"Updated saved direction for {checkpoint['chapter_id']}.")
    before = {checkpoint['draft_id']: reels.get_draft(checkpoint['draft_id']) for checkpoint in run['plans']}
    previous_jobs = {draft_id: jobs.status(draft_id) for draft_id in before}
    expected_quotes = [before[checkpoint['draft_id']].quote_text for checkpoint in targets]
    calls = []
    monkeypatch.setattr(web, 'create_text_client', lambda *args: object())

    async def generate(*args, **kwargs):
        calls.append(kwargs['quote'])
        return resolved_reference_plan(['Niemand'])

    monkeypatch.setattr(web, 'generate_scene_plan', generate)
    with TestClient(web.create_app(settings, start_worker=False), base_url='http://127.0.0.1:8000') as client:
        route = base + '/chapters/plans/generate'
        fields = {'run_id': run['id'], 'ai_provider': 'openwebui'}
        assert client.post(route, data=fields, follow_redirects=False).status_code == 303
        assert calls == expected_quotes
        assert client.post(route, data=fields, follow_redirects=False).status_code == 409
        assert calls == expected_quotes
    target_ids = {checkpoint['draft_id'] for checkpoint in targets}
    for draft_id, original in before.items():
        after = reels.get_draft(draft_id)
        assert_media_preserved(reels, original, after)
        assert jobs.status(draft_id) == previous_jobs[draft_id]
        if draft_id in target_ids:
            assert after.scene_plan_json
            assert after.revision == original.revision + 1
        else:
            assert after == original


def test_batch_planning_failure_keeps_prior_plan_and_stops_before_later_work(setup, monkeypatch):
    settings, _, _, _, reels, store, jobs, run, base = chapter_fixture(setup, save_plans=True, video=True)
    for checkpoint in run['plans']:
        draft = reels.get_draft(checkpoint['draft_id'])
        store.save_prompt(draft.book_id, run['id'], draft.id, draft.revision,
            image_prompt=draft.image_prompt, teaser_text=draft.caption_addition,
            scene_direction='A saved revision that needs re-planning.')
    before = {checkpoint['draft_id']: reels.get_draft(checkpoint['draft_id']) for checkpoint in run['plans']}
    previous_jobs = {draft_id: jobs.status(draft_id) for draft_id in before}
    calls = []
    monkeypatch.setattr(web, 'create_text_client', lambda *args: object())

    async def generate(*args, **kwargs):
        calls.append(kwargs['quote'])
        if len(calls) == 2:
            raise OpenWebUIError('unavailable')
        return resolved_reference_plan(['Niemand'])

    monkeypatch.setattr(web, 'generate_scene_plan', generate)
    with TestClient(web.create_app(settings, start_worker=False), base_url='http://127.0.0.1:8000') as client:
        response = client.post(base + '/chapters/plans/generate',
            data={'run_id': run['id'], 'ai_provider': 'openwebui'}, follow_redirects=False)
    assert response.status_code == 503
    assert len(calls) == 2
    first_id, second_id = [checkpoint['draft_id'] for checkpoint in run['plans']]
    assert reels.get_draft(first_id).scene_plan_json
    assert reels.get_draft(second_id).scene_plan_json == ''
    for draft_id, original in before.items():
        assert_media_preserved(reels, original, reels.get_draft(draft_id))
        assert jobs.status(draft_id) == previous_jobs[draft_id]


@pytest.mark.parametrize('invalid', [None, 'stale_plan', 'stale_revision'])
def test_chapter_base_image_regeneration_queues_saved_scene_plan_without_ai(setup, monkeypatch, invalid):
    settings, uploads, _, _, reels, _, jobs, run, base = chapter_fixture(setup, save_plans=True)
    checkpoint = run['plans'][0]
    before = reels.get_draft(checkpoint['draft_id'])
    fields = {'strategy': 'scene_plan', 'revision': before.revision}
    if invalid == 'stale_revision':
        fields['revision'] -= 1
    elif invalid == 'stale_plan':
        value = encode_saved_plan(decode_saved_plan(before.scene_plan_json).plan, '0' * 64)
        with sqlite3.connect(uploads.db_path) as connection, connection:
            connection.execute('update local_reel_drafts set scene_plan_json=? where id=?', (value, before.id))
        before = reels.get_draft(before.id)
    previous_jobs = jobs.status(before.id)
    forbid_ai(monkeypatch)
    with TestClient(web.create_app(settings, start_worker=False), base_url='http://127.0.0.1:8000') as client:
        response = client.post(base + f"/chapters/{checkpoint['chapter_id']}/image/regenerate", data=fields, follow_redirects=False)
    assert response.status_code == (409 if invalid else 303)
    if invalid:
        assert reels.get_draft(before.id) == before
        assert jobs.status(before.id) == previous_jobs
    else:
        with jobs.connection() as connection:
            payload = connection.execute("select payload_json from local_reel_jobs where draft_id=? and state='queued'", (before.id,)).fetchone()[0]
        assert json.loads(payload) == {
            'operation': 'scene', 'strategy': 'scene_plan', 'scene_plan_json': before.scene_plan_json,
        }
        assert reels.get_draft(before.id).selected_image_path == before.selected_image_path
        assert reels.get_draft(before.id).selected_image_sha256 == before.selected_image_sha256


def test_chapter_plan_and_base_scene_allow_selected_character_without_portrait(setup, monkeypatch):
    settings, uploads, book, _, reels, _, jobs, run, base = chapter_fixture(setup, save_plans=True)
    checkpoint = run['plans'][0]
    before = reels.get_draft(checkpoint['draft_id'])
    character = CharacterStore(uploads).create(book.id, name='Copper Fox', image_prompt='A copper fox without a portrait.')
    calls = []
    monkeypatch.setattr(web, 'create_text_client', lambda *args: object())

    async def generate(*args, **kwargs):
        assert kwargs['characters'] == ({'name': character.name, 'aliases': []},)
        calls.append(kwargs)
        return resolved_reference_plan([character.name])

    monkeypatch.setattr(web, 'generate_scene_plan', generate)
    chapter_url = base + f"/chapters/{checkpoint['chapter_id']}"
    with TestClient(web.create_app(settings, start_worker=False), base_url='http://127.0.0.1:8000') as client:
        assert client.post(chapter_url + '/plan/save', data={
            'revision': before.revision, 'scene_direction': 'Copper Fox leans forward.',
            'character_id': character.id,
        }, follow_redirects=False).status_code == 303
        saved = reels.get_draft(before.id)
        assert client.post(chapter_url + '/plan/generate',
            data={'revision': saved.revision, 'ai_provider': 'openwebui'}, follow_redirects=False).status_code == 303
        planned = reels.get_draft(before.id)
        assert planned.scene_plan_json
        assert_media_preserved(reels, before, planned)
        assert client.post(chapter_url + '/image/optimize',
            data={'revision': planned.revision, 'strategy': 'planned_scene'}, follow_redirects=False).status_code == 409
        assert client.post(chapter_url + '/image/regenerate',
            data={'revision': planned.revision, 'strategy': 'scene_plan'}, follow_redirects=False).status_code == 303
    assert len(calls) == 1
    with jobs.connection() as connection:
        payload = connection.execute("select payload_json from local_reel_jobs where draft_id=? and state='queued'", (before.id,)).fetchone()[0]
    assert json.loads(payload)['scene_plan_json'] == planned.scene_plan_json
    assert json.loads(payload)['strategy'] == 'scene_plan'


@pytest.mark.parametrize('change', ['revision', 'queued', 'style', 'source'])
def test_batch_preflight_stops_changed_second_chapter_before_another_ai_call(setup, monkeypatch, change):
    settings, uploads, book, _, reels, store, jobs, run, base = chapter_fixture(setup, save_plans=True, video=True)
    for checkpoint in run['plans']:
        draft = reels.get_draft(checkpoint['draft_id'])
        store.save_prompt(book.id, run['id'], draft.id, draft.revision,
            image_prompt=draft.image_prompt, teaser_text=draft.caption_addition,
            scene_direction='A saved direction requiring an updated plan.')
    first_id, second_id = [checkpoint['draft_id'] for checkpoint in run['plans']]
    before = {draft_id: reels.get_draft(draft_id) for draft_id in (first_id, second_id)}
    original_save = ChapterTeaserStore.save_scene_plan
    calls = []
    changes = {}
    monkeypatch.setattr(web, 'create_text_client', lambda *args: object())

    async def generate(*args, **kwargs):
        calls.append(kwargs['quote'])
        return resolved_reference_plan(['Niemand'])

    def save_then_change_next_chapter(self, book_id, run_id, draft_id, revision, plan_json):
        saved = original_save(self, book_id, run_id, draft_id, revision, plan_json)
        if draft_id == first_id:
            second = reels.get_draft(second_id)
            if change == 'revision':
                reels.update_draft(second_id, second.revision, caption_addition='A caption edited between batch items.')
            elif change == 'queued':
                jobs.enqueue(second_id, 'image', {'operation': 'scene'})
            elif change == 'style':
                profile = ManagementStore(uploads).get(book_id)
                ManagementStore(uploads).save(book_id, profile['revision'],
                    profile['details'].model_copy(update={'image_prompt_base': 'Changed style between batch items'}),
                    profile['suggestion_id'])
            else:
                with sqlite3.connect(uploads.db_path) as connection, connection:
                    connection.execute('update local_extractions set revision=revision+1 where book_id=?', (book_id,))
            changes['second'] = reels.get_draft(second_id)
            changes['jobs'] = jobs.status(second_id)
        return saved

    monkeypatch.setattr(web, 'generate_scene_plan', generate)
    monkeypatch.setattr(ChapterTeaserStore, 'save_scene_plan', save_then_change_next_chapter)
    with TestClient(web.create_app(settings, start_worker=False), base_url='http://127.0.0.1:8000') as client:
        response = client.post(base + '/chapters/plans/generate',
            data={'run_id': run['id'], 'ai_provider': 'openwebui'}, follow_redirects=False)
    assert response.status_code == 409
    assert calls == [before[first_id].quote_text]
    first = reels.get_draft(first_id)
    assert first.scene_plan_json and first.revision == before[first_id].revision + 1
    assert_media_preserved(reels, before[first_id], first)
    assert reels.get_draft(second_id) == changes['second']
    assert jobs.status(second_id) == changes['jobs']
    assert_media_preserved(reels, before[second_id], reels.get_draft(second_id))
