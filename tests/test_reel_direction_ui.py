"""AI staging is durable, editable, explicitly requested and revision-fenced."""
from html import escape
import json
from urllib.parse import urlencode

from fastapi.testclient import TestClient
import pytest

from bookpromo import web
from bookpromo.analysis import AnalysisOptions
from bookpromo.analysis_worker import run_analysis_once
from bookpromo.reels import ReelStore
from bookpromo.uploads import UploadError
from test_analysis import setup, FakeAPI
from test_management import analyzed
from test_reel_restage_ui import prepare


def test_analysis_direction_seeds_new_workshop_without_ai_request(setup):
    settings, uploads, book, _, _ = setup
    quote = analyzed(setup).get(book.id)["quotes"][0]["quote"]
    url = f"/books/local/{book.id}/chapters/{quote.chapter_id}/quotes/{quote.id}/reel"
    with TestClient(web.create_app(settings, start_worker=False), base_url="http://127.0.0.1:8000") as client:
        page = client.get(url)
        assert page.status_code == 200
        assert escape(quote.scene_direction) in page.text
        assert 'Pose &amp; Requisiten mit KI erstellen' in page.text
        assert 'formaction=' in page.text and '/direction/save' in page.text
    draft = ReelStore(uploads).list_drafts(book.id)[0]
    assert draft.scene_direction == quote.scene_direction


def test_direction_save_then_reload_keeps_media_and_scene_prompt(setup):
    app, url, reels, jobs, _, before = prepare(setup)
    original_jobs = jobs.status(before.id)
    direction = '  Aster turns toward the stream; carry no held objects.  '
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.post(url + '/direction/save', data={
            'revision': before.revision, 'strategy': 'reference_scene', 'scene_direction': direction,
        })
        assert response.status_code == 200
        assert escape(direction.strip()) in client.get(url).text
    after = reels.get_draft(before.id)
    assert after.scene_direction == direction.strip()
    assert after.revision == before.revision + 1
    for name in ('selected_image_path', 'selected_image_sha256', 'scene_image_path',
                 'image_prompt', 'selected_video_path', 'image_stale', 'video_stale'):
        assert getattr(after, name) == getattr(before, name)
    assert jobs.status(before.id) == original_jobs


@pytest.mark.parametrize('fields', [
    [('revision', '1')], [('scene_direction', 'Sit.')],
    [('revision', 'bad'), ('scene_direction', 'Sit.')],
    [('revision', '1'), ('scene_direction', 'x' * 2001)],
    [('revision', '1'), ('scene_direction', 'Sit.'), ('scene_direction', 'Run.')],
    [('revision', '1'), ('scene_direction', 'Sit.'), ('unexpected', 'x')],
])
def test_invalid_direction_save_never_mutates(setup, fields):
    app, url, reels, jobs, _, before = prepare(setup)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.post(url+'/direction/save', content=urlencode(fields),
                               headers={'Content-Type': 'application/x-www-form-urlencoded'})
    assert response.status_code == 400
    assert reels.get_draft(before.id) == before


def test_explicit_direction_generation_changes_only_direction(setup, monkeypatch):
    app, url, reels, jobs, _, before = prepare(setup)
    calls = []
    sentinel = object()
    def factory(settings, provider):
        calls.append(provider)
        return sentinel
    async def generate(client, **kwargs):
        assert client is sentinel
        assert kwargs['quote'] == before.quote_text
        assert kwargs['image_prompt'] == before.image_prompt
        assert kwargs['characters'] == ({'name': 'Aster', 'aliases': ['Ast']},)
        calls.append(kwargs)
        return 'Aster crouches beside the water, looking at the reflection. No held objects.'
    monkeypatch.setattr(web, 'create_text_client', factory)
    monkeypatch.setattr(web, 'generate_scene_direction', generate)
    original_jobs = jobs.status(before.id)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.post(url+'/direction/generate', data={
            'revision': before.revision, 'ai_provider': 'openwebui',
        })
        assert response.status_code == 200
    after = reels.get_draft(before.id)
    assert len(calls) == 2 and calls[0] == 'openwebui'
    assert after.scene_direction.startswith('Aster crouches')
    assert after.image_prompt == before.image_prompt
    assert after.selected_image_path == before.selected_image_path
    assert after.selected_image_sha256 == before.selected_image_sha256
    assert after.image_stale == before.image_stale
    assert jobs.status(before.id) == original_jobs


def test_ai_direction_revision_conflict_preserves_manual_change(setup, monkeypatch):
    app, url, reels, jobs, _, before = prepare(setup)
    monkeypatch.setattr(web, 'create_text_client', lambda *args: object())
    async def generate(*args, **kwargs):
        reels.update_draft(before.id, before.revision, scene_direction='Manual edit while AI runs.')
        return 'AI proposal must not overwrite the concurrent edit.'
    monkeypatch.setattr(web, 'generate_scene_direction', generate)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.post(url+'/direction/generate', data={
            'revision': before.revision, 'ai_provider': 'openwebui',
        })
    assert response.status_code == 409
    assert reels.get_draft(before.id).scene_direction == 'Manual edit while AI runs.'


@pytest.mark.parametrize('kind', ['stale', 'busy', 'bad-provider', 'duplicate'])
def test_direction_generation_preflight_never_calls_ai(setup, monkeypatch, kind):
    app, url, reels, jobs, _, before = prepare(setup)
    fields = [('revision', str(before.revision)), ('ai_provider', 'openwebui')]
    expected = 409
    if kind == 'stale':
        fields[0] = ('revision', str(before.revision - 1))
    elif kind == 'busy':
        jobs.enqueue(before.id, 'image', {'operation': 'optimize'})
    elif kind == 'bad-provider':
        fields[1] = ('ai_provider', 'unexpected')
        expected = 400
    else:
        fields.append(('ai_provider', 'openwebui'))
        expected = 400
    monkeypatch.setattr(web, 'create_text_client', lambda *args: pytest.fail('Invalid request called AI'))
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.post(url+'/direction/generate', content=urlencode(fields),
                               headers={'Content-Type': 'application/x-www-form-urlencoded'})
    assert response.status_code == expected
    assert reels.get_draft(before.id) == before


def test_optimization_saves_direction_and_queues_same_snapshot(setup):
    app, url, reels, jobs, _, before = prepare(setup)
    direction = 'Ast leaps over the log, looking towards the path. No held objects.'
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.post(url+'/image/optimize', data={
            'revision': before.revision, 'strategy': 'reference_scene', 'scene_direction': direction,
        })
    assert response.status_code == 200
    after = reels.get_draft(before.id)
    assert after.scene_direction == direction
    assert after.selected_image_path == before.selected_image_path
    job = jobs.claim()
    assert job.input_revision == after.revision
    assert job.payload['scene_direction'] == direction


@pytest.mark.parametrize('submitted_direction', [None, 'Saved field submitted by a browser without JavaScript.'])
def test_masked_optimization_remains_available_with_saved_direction(setup, submitted_direction):
    app, url, reels, jobs, _, before = prepare(setup)
    assert before.scene_direction
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        fields = {
            'revision': before.revision, 'strategy': 'masked',
        }
        if submitted_direction is not None:
            fields['scene_direction'] = submitted_direction
        response = client.post(url+'/image/optimize', data=fields)
    assert response.status_code == 200
    assert reels.get_draft(before.id).scene_direction == before.scene_direction
    assert 'scene_direction' not in jobs.claim().payload


def test_copy_generation_preserves_manually_saved_direction(setup, monkeypatch):
    app, url, reels, _, _, before = prepare(setup)
    before = reels.update_draft(before.id, before.revision, scene_direction='Manual direction.')
    monkeypatch.setattr(web, 'create_text_client', lambda *args: object())
    async def generate(*args, **kwargs):
        from bookpromo.reel_content import ReelCopy
        return ReelCopy(addition='Neugierig?', caption='Zitat\n\nNeugierig?',
                        image_prompt=before.image_prompt, scene_direction='New AI direction.')
    monkeypatch.setattr(web, 'generate_reel_copy', generate)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.post(url+'/copy', data={'ai_provider': 'openwebui'}).status_code == 200
    assert reels.get_draft(before.id).scene_direction == 'Manual direction.'
