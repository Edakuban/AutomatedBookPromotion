import asyncio
import json

import httpx
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict
import pytest

from bookpromo.config import Settings, load_settings
from bookpromo.model_settings import save_model
from bookpromo.openwebui import OpenWebUIClient, OpenWebUIError
from bookpromo.uploads import UploadError
from bookpromo.web import create_app


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
        monkeypatch.delenv(field.lower(), raising=False)
    env = tmp_path / '.env'
    env.write_text('OPENWEBUI_URL=https://webui.example/subpath\nOPENWEBUI_API_KEY=PRIVATE_KEY\nOPENWEBUI_MODEL=model-a\n', encoding='utf-8')
    return load_settings(env)


def client(settings, handler):
    return OpenWebUIClient(settings, transport=httpx.MockTransport(handler))


def answer(content='BOOKPROMO_OK', finish='stop', **extra):
    return httpx.Response(200, json={'choices':[{'message': {'content': content, **extra}, 'finish_reason': finish}]})


def test_model_check_uses_bearer_base_path_and_safe_projection(settings):
    def handler(request):
        assert request.url == 'https://webui.example/subpath/api/models'
        assert request.headers['authorization'] == 'Bearer PRIVATE_KEY'
        return httpx.Response(200, json={'data':[{'id':'model-a', 'name':'A', 'info':{'secret':'DO_NOT_RETURN'}}, {'id':'model-b'}]})
    result = asyncio.run(client(settings, handler).check())
    assert result['model_available']
    assert result['models'] == [{'id':'model-a','name':'A'}, {'id':'model-b','name':'model-b'}]
    assert 'DO_NOT_RETURN' not in str(result)


def test_custom_model_display_names_are_normalized_without_changing_ids(settings):
    api=client(settings,lambda _:httpx.Response(200,json={'data':[{'id':'model-a','name':'Name\nmit\tUmbruch\x7f'}]}))
    assert asyncio.run(api.list_models())[0].model_dump()=={'id':'model-a','name':'Name mit Umbruch'}


def test_text_and_json_are_bounded_and_disable_tools(settings):
    class Result(BaseModel):
        model_config = ConfigDict(extra='forbid')
        ok: bool
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.path.endswith('/api/chat/completions')
        assert body['stream'] is False and body['tool_ids'] == [] and body['tools'] == []
        assert body['features']['web_search'] is False
        assert body['messages'][0]['role'] == 'system'
        return answer('{"ok":true}')
    result = asyncio.run(client(settings, handler).complete_json('System', 'Testtext', Result, max_tokens=100))
    assert result.ok is True
    assert requests[0]['response_format']['type'] == 'json_schema'
    assert requests[0]['max_tokens'] == 100


@pytest.mark.parametrize('status,code', [(401,'credentials'),(403,'credentials'),(404,'endpoint'),(302,'redirect'),(400,'request'),(500,'unavailable')])
def test_errors_do_not_echo_remote_bodies_or_follow_redirects(settings, status, code):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text='PRIVATE_KEY PRIVATE_BOOK', headers={'location':'https://other.example'})
    with pytest.raises(OpenWebUIError) as error:
        asyncio.run(client(settings,handler).complete('System','Text'))
    assert error.value.code == code and 'PRIVATE' not in str(error.value)
    assert len(calls) == 1


def test_transient_errors_retry_with_a_bound(settings, monkeypatch):
    pauses=[]
    async def sleep(delay): pauses.append(delay)
    monkeypatch.setattr(asyncio,'sleep',sleep)
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(503, text='PRIVATE') if len(calls)<3 else answer()
    assert asyncio.run(client(settings,handler).complete('System','Text')) == 'BOOKPROMO_OK'
    assert len(calls)==3 and pauses==[.5,1]
    with pytest.raises(OpenWebUIError, match='ausgelastet'):
        asyncio.run(client(settings,lambda _:httpx.Response(429, headers={'Retry-After':'60'})).complete('System','Text'))


def test_read_timeout_does_not_duplicate_generation(settings):
    calls=[]
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout('PRIVATE_KEY',request=request)
    with pytest.raises(OpenWebUIError) as error:
        asyncio.run(client(settings,handler).complete('System','Text'))
    assert error.value.code=='timeout' and len(calls)==1
    assert 'PRIVATE' not in str(error.value)


@pytest.mark.parametrize('response,code', [
    (answer('partial', 'length'),'truncated'), (answer('', 'stop'),'response'),
    (answer('text', 'tool_calls', tool_calls=[{'function':{'name':'unsafe'}}]),'response'),
    (httpx.Response(200,json={'task_id':'background'}),'response'),
    (httpx.Response(200,text='data: PRIVATE_STREAM\n\n',headers={'content-type':'text/event-stream'}),'response'),
], ids=['truncated','empty','tool-call','background','unexpected-stream'])
def test_invalid_completions_are_rejected(settings,response,code):
    with pytest.raises(OpenWebUIError) as error:
        asyncio.run(client(settings,lambda _:response).complete('System','Text'))
    assert error.value.code==code


def test_invalid_structured_reply_is_not_repaired_or_accepted(settings):
    class Result(BaseModel):
        model_config=ConfigDict(extra='forbid')
        value:int
    for content in ('{"value":"7"}', '```json\n{"value":7}\n```', '{"value":7,"extra":true}'):
        with pytest.raises(OpenWebUIError) as error:
            asyncio.run(client(settings,lambda _:answer(content)).complete_json('System','Text',Result))
        assert error.value.code=='structured'


def test_response_size_limit_and_total_deadline(settings):
    with pytest.raises(OpenWebUIError) as error:
        asyncio.run(client(settings,lambda _:httpx.Response(200,content=b'x'*(2*1024*1024+1))).complete('System','Text'))
    assert error.value.code=='size'
    async def slow(request):
        await asyncio.sleep(1)
        return answer()
    api=client(settings,slow)
    api._timeout=.01
    with pytest.raises(OpenWebUIError) as error:
        asyncio.run(api.complete('System','Text'))
    assert error.value.code=='timeout'


def test_generation_length_hints_do_not_clip_but_local_limits_still_apply(settings):
    from bookpromo.analysis import PROFILE_FIELD_MODELS
    result_type = PROFILE_FIELD_MODELS['mood']
    responses = iter(['Düster, angespannt, rätselhaft', 'x' * 201])
    def handler(request):
        body = json.loads(request.content)
        schema = body['response_format']['json_schema']['schema']
        assert 'maxLength' not in json.dumps(schema)
        assert '200 Zeichen' in schema['properties']['mood']['description']
        return answer(json.dumps({'mood': next(responses)}))
    api = client(settings, handler)
    assert asyncio.run(api.complete_json('System', 'Text', result_type)).mood == 'Düster, angespannt, rätselhaft'
    with pytest.raises(OpenWebUIError) as exc:
        asyncio.run(api.complete_json('System', 'Text', result_type))
    assert exc.value.code == 'structured'
    assert result_type.model_json_schema()['properties']['mood']['maxLength'] == 200


def test_model_save_preserves_other_credentials_and_reloads(settings):
    env=settings._env_path
    raw=b'\xef\xbb\xbf# Private config\r\nOPENWEBUI_API_KEY=PRIVATE_KEY\r\nSUPABASE_SECRET_KEY=PRIVATE_DB\r\nOPENWEBUI_MODEL="old" # comment\r\n'
    env.write_bytes(raw)
    save_model(settings,'provider/model-b:latest')
    assert env.read_bytes()==raw.replace(b'OPENWEBUI_MODEL="old" # comment',b'OPENWEBUI_MODEL="provider/model-b:latest"')
    assert load_settings(env).openwebui_model=='provider/model-b:latest'
    assert settings.openwebui_model=='provider/model-b:latest'
    assert not list(env.parent.glob('.env.*.tmp'))


@pytest.mark.parametrize('model',['bad\nAPP_PORT=1','${SECRET}',"bad'quote",'bad\\path'])
def test_model_save_rejects_env_injection(settings,model):
    before=settings._env_path.read_bytes()
    with pytest.raises(UploadError):save_model(settings,model)
    assert settings._env_path.read_bytes()==before


def test_process_model_override_and_duplicate_env_are_not_silently_overwritten(settings,monkeypatch):
    monkeypatch.setenv('OPENWEBUI_MODEL','process-model')
    with pytest.raises(UploadError,match='Prozessvariable'):save_model(settings,'new')
    monkeypatch.delenv('OPENWEBUI_MODEL')
    with settings._env_path.open('a',encoding='utf-8') as f:f.write('OPENWEBUI_MODEL=duplicate\n')
    with pytest.raises(UploadError,match='mehrfach'):save_model(settings,'new')


def test_web_check_selection_smoke_and_csrf(settings,monkeypatch):
    calls=[]
    def handler(request):
        calls.append(request)
        if request.url.path.endswith('/api/models'):
            return httpx.Response(200,json={'data':[{'id':'model-a'},{'id':'model-b','name':'<script>unsafe</script>'}]})
        return answer()
    monkeypatch.setattr('bookpromo.web.OpenWebUIClient',lambda config:client(config,handler))
    with TestClient(create_app(settings,start_worker=False),base_url='http://127.0.0.1:8000') as web:
        assert 'PRIVATE_KEY' not in web.get('/settings').text and not calls
        assert web.post('/settings/openwebui/check',headers={'Origin':'https://foreign.example'}).status_code==403
        checked=web.post('/settings/openwebui/check').json()
        assert checked['model_available']
        assert 'PRIVATE_KEY' not in str(checked)
        assert web.post('/settings/openwebui/model',data={'model':'missing'}).status_code==503
        response=web.post('/settings/openwebui/model',data={'model':'model-b'})
        assert response.status_code==200
        assert load_settings(settings._env_path).openwebui_model=='model-b'
        assert web.post('/settings/openwebui/test').status_code==200
        assert 'PRIVATE_KEY' not in web.get('/settings').text


def test_web_error_body_is_sanitized(settings,monkeypatch):
    monkeypatch.setattr('bookpromo.web.OpenWebUIClient',lambda config:client(config,lambda _:httpx.Response(401,text='PRIVATE_KEY')))
    with TestClient(create_app(settings,start_worker=False),base_url='http://127.0.0.1:8000') as web:
        response=web.post('/settings/openwebui/check')
        assert response.status_code==503 and response.json()['code']=='credentials'
        assert 'PRIVATE_KEY' not in response.text
