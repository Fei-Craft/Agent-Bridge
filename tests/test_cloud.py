from __future__ import annotations

import asyncio
import json
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import httpx2
import pytest
from pydantic import ValidationError

from agent_bridge import cli, server
from agent_bridge.adapters.cloud import CloudAdapter
from agent_bridge.config import AgentConfig, CloudConfig, load_config
from agent_bridge.hub import create_app
from agent_bridge.models import TaskStatus
from agent_bridge.probes import probe_agent
from agent_bridge.registry import Registry
from agent_bridge.transcript import flush_pending


class Provider(BaseHTTPRequestHandler):
    requests: ClassVar[list] = []
    turn = 0
    busy = 0
    cursor_status = 'FINISHED'
    devin_status = 'running'
    detail = 'finished'
    echo = ''
    usage_status = 403
    empty = False
    api_error = 0
    devin_id = 'devin-test'
    no_messages = False

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def do_DELETE(self):
        self.respond()

    def respond(self):
        cls = type(self)
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or '{}')
        cls.requests.append((self.command, self.path, body))
        status = 200
        run = {'id': f'run-{cls.turn}', 'status': cls.cursor_status, 'result': '' if cls.empty else 'cursor reply ' + cls.echo,
               'git': {'branches': [{'branch': 'cursor/test', 'repoUrl': 'https://github.com/o/r',
                                     'prUrl': 'https://github.com/o/r/pull/1'}]}}
        session = {'session_id': cls.devin_id, 'url': 'https://app.devin.ai/s/test',
                   'status': cls.devin_status, 'status_detail': cls.detail, 'acus_consumed': 1.25,
                   'pull_requests': [{'pr_url': 'https://github.com/o/r/pull/2', 'pr_state': 'open'}],
                   'structured_output': {'ok': True}}
        if self.path == '/v1/agents':
            cls.turn += 1
            run['id'] = f'run-{cls.turn}'
            data: dict[str, Any] = {'agent': {'id': 'bc-test', 'url': 'https://cursor.com/agents/test'}, 'run': run}
        elif self.path.endswith('/runs'):
            if cls.busy:
                cls.busy -= 1
                status, data = 409, {'error': {'code': 'agent_busy', 'message': cls.echo}}
            else:
                cls.turn += 1
                run['id'] = f'run-{cls.turn}'
                data = {'run': run}
        elif '/usage?' in self.path:
            status, data = cls.usage_status, {'totalUsage': {'inputTokens': 10, 'outputTokens': 2}}
        elif '/runs/' in self.path:
            data = run
        elif self.path.endswith('/archive'):
            data = {'id': 'bc-test'}
        elif '/messages' in self.path and self.command == 'GET':
            # Two pages, including a user message that must not become result text.
            after = 'after=' in self.path
            data = {'items': ([{'event_id': 'user', 'source': 'user', 'message': 'not a reply'}] if not after else [
                {'event_id': f'm-{cls.turn}', 'source': 'devin', 'message': f'devin reply {cls.turn} ' + cls.echo}
            ]), 'has_next_page': not after, 'end_cursor': 'page2' if not after else None}
        elif self.command == 'POST' and '/sessions' in self.path:
            cls.turn += 1
            data = session
        else:
            data = session
        if cls.api_error:
            status, data = cls.api_error, {'error': cls.echo}
        if cls.no_messages and '/messages' in self.path and self.command == 'GET':
            data = {'items': [], 'has_next_page': False}
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def provider():
    class Handler(Provider):
        requests: ClassVar[list] = []

    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{http.server_port}', Handler
    http.shutdown()
    http.server_close()
    thread.join()


def config(provider, name='cursor'):
    return AgentConfig(name=name, protocol='cloud', cloud=CloudConfig(
        provider=name, base_url=provider[0], org_id='org-test', repo='https://github.com/o/r',
        ref='main', poll_interval_sec=0.01, auto_create_pr=True, max_acu_limit=2,
    ))


@pytest.fixture
async def cloud_registry(provider, bridge_home, monkeypatch):
    monkeypatch.setenv('CURSOR_API_KEY', 'fake-secret-cursor-unique')
    monkeypatch.setenv('DEVIN_API_KEY', 'fake-secret-devin-unique')
    registry = Registry.create(bridge_home)
    registry.config.coordinator.mode = 'auto'
    registry.config.quota.enabled = False
    registry.config.agents.update({n: config(provider, n) for n in ('cursor', 'devin')})
    await registry.start()
    yield registry
    await registry.stop()


async def turn(registry, name, **kwargs):
    task = await registry.dispatch_task(name, 'do it', **kwargs)
    await registry.wait_task(task['task_id'], timeout_sec=5)
    return await registry.get_result(task['task_id'])


def progress_event(monkeypatch, predicate):
    event = asyncio.Event()
    original = CloudAdapter._record

    def record(adapter, session, task, **fields):
        original(adapter, session, task, **fields)
        if predicate(task):
            event.set()
    monkeypatch.setattr(CloudAdapter, '_record', record)
    return event


@pytest.mark.parametrize('name', ['cursor', 'devin'])
async def test_fresh_resume_metadata_no_cwd(cloud_registry, provider, name, monkeypatch):
    def no_snapshot(*args):
        pytest.fail('cloud must not snapshot or classify local paths')
    monkeypatch.setattr('agent_bridge.registry.snapshot_workspace', no_snapshot)
    monkeypatch.setattr('agent_bridge.registry.merge_files_changed', no_snapshot)
    result = await turn(cloud_registry, name, repo='https://github.com/o/override', ref='feature')
    assert result['status'] == 'completed'
    assert result['remote']['pr_urls']
    assert result['files_changed'] == []
    sid = result['session_id']
    session = cloud_registry.sessions[sid]
    assert session.native_session_id == ('bc-test' if name == 'cursor' else 'devin-test')
    second = await turn(cloud_registry, name, session_id=sid)
    assert second['status'] == 'completed'
    assert second['remote']['url'] == result['remote']['url']
    if name == 'cursor':
        assert second['remote']['run_id'] == 'run-2'
        assert second['remote']['branches'][0]['branch'] == 'cursor/test'
        assert result['remote']['run_id'] == 'run-1'
    else:
        assert second['usage']['acus_consumed'] == 1.25
        assert 'devin reply 2' in second['result_text']
        assert 'devin reply 1' not in second['result_text']
        assert 'not a reply' not in second['result_text']
        assert '"ok": true' in second['result_text']
    with pytest.raises(ValueError, match='same repository'):
        await cloud_registry.dispatch_task(name, 'bad repo', session_id=sid, repo='other')
    with pytest.raises(ValueError, match='same repository ref'):
        await cloud_registry.dispatch_task(name, 'bad ref', session_id=sid, ref='other')
    await cloud_registry.end_session(sid)
    assert any('/archive' in path if name == 'cursor' else '?archive=false' in path
               for _, path, _ in provider[1].requests)


@pytest.mark.parametrize(('name', 'status', 'detail', 'expected'), [
    ('cursor', 'ERROR', '', 'failed'), ('cursor', 'EXPIRED', '', 'failed'),
    ('cursor', 'CANCELLED', '', 'cancelled'), ('devin', 'running', 'waiting_for_user', 'completed'),
    ('devin', 'running', 'waiting_for_approval', 'completed'),
    ('devin', 'suspended', 'out_of_credits', 'failed'), ('devin', 'error', 'error', 'failed'),
    ('devin', 'exit', 'finished', 'completed'),
])
async def test_status_mapping(cloud_registry, provider, name, status, detail, expected):
    handler = provider[1]
    handler.cursor_status = status
    handler.devin_status = status
    handler.detail = detail
    result = await turn(cloud_registry, name)
    assert result['status'] == expected
    if detail == 'waiting_for_approval':
        assert 'approval' in result['warnings'][0]


@pytest.mark.parametrize('exhaust', [False, True])
async def test_cursor_busy_backoff(cloud_registry, provider, exhaust):
    first = await turn(cloud_registry, 'cursor', model='custom-model')
    provider[1].busy = 10 if exhaust else 2
    second = await turn(cloud_registry, 'cursor', session_id=first['session_id'])
    assert second['status'] == ('failed' if exhaust else 'completed')
    posts = [body for method, path, body in provider[1].requests if method == 'POST' and path.endswith('/runs')]
    assert len(posts) == (4 if exhaust else 3)
    assert posts[0]['model'] == {'id': 'custom-model'}


@pytest.mark.parametrize('name', ['cursor', 'devin'])
async def test_cancel_remote(cloud_registry, provider, name, monkeypatch):
    provider[1].cursor_status = 'RUNNING'
    provider[1].detail = 'working'
    ready = progress_event(monkeypatch, lambda task: task.remote.get('provider'))
    dispatched = await cloud_registry.dispatch_task(name, 'long task')
    task = cloud_registry.tasks[dispatched['task_id']]
    await asyncio.wait_for(ready.wait(), timeout=5)
    await cloud_registry.cancel_task(task.task_id)
    assert task.status == TaskStatus.cancelled
    if name == 'devin':
        assert task.remote['resumable'] is False
        with pytest.raises(ValueError, match='terminated'):
            await cloud_registry.dispatch_task(name, 'resume', session_id=task.session_id)
    else:
        assert any(path.endswith('/cancel') for _, path, _ in provider[1].requests)


@pytest.mark.parametrize('name', ['cursor', 'devin'])
@pytest.mark.parametrize('phase', ['completed', 'running', 'creating'])
async def test_shutdown_preserves_remote(cloud_registry, provider, name, phase, monkeypatch):
    ready = progress_event(monkeypatch, lambda task: task.remote.get('status'))
    release = asyncio.Event()
    created = asyncio.Event()
    original = CloudAdapter._request

    async def request(adapter, method, path, body=None):
        data = await original(adapter, method, path, body)
        if phase == 'creating' and method == 'POST':
            created.set()
            await release.wait()
        return data

    monkeypatch.setattr(CloudAdapter, '_request', request)
    if phase != 'completed':
        provider[1].cursor_status = 'RUNNING'
        provider[1].detail = 'working'
    dispatched = await cloud_registry.dispatch_task(name, 'keep remote alive')
    task = cloud_registry.tasks[dispatched['task_id']]
    if phase == 'completed':
        await cloud_registry.wait_task(task.task_id, timeout_sec=5)
    else:
        await asyncio.wait_for((created if phase == 'creating' else ready).wait(), timeout=5)
    stopping = asyncio.create_task(cloud_registry.stop())
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(stopping, timeout=5)
    assert task.remote['url']
    assert not any(method == 'DELETE' or path.endswith(('/archive', '/cancel'))
                   for method, path, _ in provider[1].requests)
    assert cloud_registry.sessions[task.session_id].remote.get('resumable') is not False


@pytest.mark.parametrize('name', ['cursor', 'devin'])
async def test_idle_shutdown_preserves_remote_and_explicit_end(cloud_registry, provider, name):
    first = await turn(cloud_registry, name)
    sid = first['session_id']
    adapter = cloud_registry._adapters[sid]
    session = cloud_registry.sessions[sid]
    cloud_registry.config.agents[name].idle_unload_sec = 1
    cloud_registry._schedule_idle(sid)
    assert sid not in cloud_registry._idle
    requests = len(provider[1].requests)
    await adapter.shutdown(session)
    assert len(provider[1].requests) == requests
    second = await turn(cloud_registry, name, session_id=sid)
    assert second['status'] == 'completed'
    cloud_registry._adapters.pop(sid)
    await cloud_registry.end_session(sid)
    assert any(path.endswith('/archive') if name == 'cursor' else method == 'DELETE'
               for method, path, _ in provider[1].requests)


async def test_secrets_redacted(cloud_registry, provider, bridge_home, monkeypatch, caplog):
    for name in ('cursor', 'devin'):
        secret = f'fake-secret-{name}-unique'
        provider[1].echo = secret
        result = await turn(cloud_registry, name)
        assert secret not in json.dumps(result)
        assert '[REDACTED]' in result['result_text']
    flush_pending(bridge_home)
    for path in bridge_home.rglob('*'):
        if path.is_file():
            assert b'fake-secret-' not in path.read_bytes()
    assert 'fake-secret-' not in caplog.text


async def test_probe_and_missing_credentials(provider, monkeypatch, cloud_registry):
    cfg = config(provider, 'devin')
    cfg.cloud.org_id = None
    monkeypatch.delenv('DEVIN_API_KEY', raising=False)
    monkeypatch.delenv('DEVIN_ORG_ID', raising=False)
    row = await probe_agent(cfg)
    assert not row['available']
    monkeypatch.setenv('DEVIN_API_KEY', 'not-logged')
    assert not (await probe_agent(cfg))['available']
    monkeypatch.setenv('DEVIN_ORG_ID', 'org-test')
    assert (await probe_agent(cfg))['available']
    assert provider[1].requests == []
    monkeypatch.delenv('CURSOR_API_KEY')
    assert (await turn(cloud_registry, 'cursor'))['status'] == 'failed'
    cloud_registry.config.agents['devin'] = cfg
    monkeypatch.delenv('DEVIN_ORG_ID')
    assert (await turn(cloud_registry, 'devin'))['status'] == 'failed'


async def test_rest_and_mcp_dispatch(cloud_registry):
    tool = next(t for t in await server.mcp.list_tools() if t.name == 'dispatch_task')
    assert 'cwd' not in tool.input_schema['required']
    assert {'repo', 'ref'} <= tool.input_schema['properties'].keys()
    app = create_app(cloud_registry, token='test')
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url='http://hub',
                                 headers={'Authorization': 'Bearer test'}) as client:
        response = await client.post('/v1/agents/cursor/invoke', json={'message': 'rest', 'repo': 'repo'})
        assert response.status_code == 200
        assert response.json()['remote']['agent_id'] == 'bc-test'
        response = await client.post('/v1/tasks', json={'agent': 'cursor', 'message': 'dispatch'})
        assert response.json()['ok']
    ctx = types.SimpleNamespace(request_context=types.SimpleNamespace(lifespan_context=cloud_registry))
    dispatched = await server.dispatch_task(ctx, agent='devin', message='mcp', repo='repo')
    assert dispatched['ok']
    await cloud_registry.wait_task(dispatched['task_id'], timeout_sec=5)
    assert (await cloud_registry.get_result(dispatched['task_id']))['remote']['session_id'] == 'devin-test'


async def test_local_still_requires_cwd(cloud_registry):
    with pytest.raises(ValueError, match='cwd is required'):
        await cloud_registry.dispatch_task('fake', 'local')


def test_cloud_configuration(bridge_home):
    cfg = config(('http://localhost', None))
    assert cfg.command == []
    assert cfg.stall_timeout_sec == 0 and cfg.turn_timeout_sec == 86400
    assert CloudConfig(provider='cursor').api_key_env == 'CURSOR_API_KEY'
    assert CloudConfig(provider='devin').base_url == 'https://api.devin.ai'
    with pytest.raises(ValidationError, match='command'):
        AgentConfig(name='local', protocol='acp')
    with pytest.raises(ValidationError, match='cloud configuration'):
        AgentConfig(name='remote', protocol='cloud')
    bridge_home.mkdir(parents=True)
    (bridge_home / 'agents.toml').write_text('[agents.remote]\nprotocol="cloud"\n[agents.remote.cloud]\nprovider="cursor"\n')
    assert load_config(bridge_home).agents['remote'].command == []


async def test_restart_preserves_remote(cloud_registry, bridge_home):
    result = await turn(cloud_registry, 'cursor')
    task = cloud_registry.tasks[result['task_id']]
    task.status = TaskStatus.running
    cloud_registry.save()
    await cloud_registry.flush_state()
    restored = Registry.create(bridge_home)
    await restored.start()
    try:
        failed = await restored.get_result(task.task_id)
        assert failed['error'] == 'bridge_restarted'
        assert failed['remote']['run_id'] == 'run-1'
        assert failed['remote']['url'].startswith('https://cursor.com/')
    finally:
        await restored.stop()


async def test_empty_result_and_usage(cloud_registry, provider):
    provider[1].empty = True
    provider[1].usage_status = 200
    result = await turn(cloud_registry, 'cursor')
    assert result['result_text'] == ''
    assert result['status'] == 'completed'
    assert result['usage'] == {'inputTokens': 10, 'outputTokens': 2}


async def test_devin_empty_followup_and_id_prefix(cloud_registry, provider):
    provider[1].devin_id = 'bare-session-id'
    provider[1].no_messages = True
    first = await turn(cloud_registry, 'devin')
    second = await turn(cloud_registry, 'devin', session_id=first['session_id'])
    assert second['status'] == 'completed'
    assert second['remote']['session_id'] == 'bare-session-id'
    assert any(path.startswith('/v3/organizations/org-test/sessions/devin-bare-session-id')
               for _, path, _ in provider[1].requests)


@pytest.mark.parametrize(('options', 'mode'), [({'effort': 'high'}, 'ultra'),
    ({'effort': 'low'}, 'lite'), ({'effort': 'high', 'model': 'fast'}, 'fast')])
async def test_devin_mode_mapping(cloud_registry, provider, options, mode):
    result = await turn(cloud_registry, 'devin', **options)
    assert result['status'] == 'completed'
    body = next(body for method, path, body in provider[1].requests
                if method == 'POST' and path.endswith('/sessions'))
    assert body['devin_mode'] == mode
    assert body['max_acu_limit'] == 2
    assert body['repos'] == ['https://github.com/o/r']
    assert 'main' in body['prompt']


async def test_http_error_no_secret(cloud_registry, provider, caplog):
    provider[1].echo = 'fake-secret-cursor-unique'
    provider[1].api_error = 401
    result = await turn(cloud_registry, 'cursor')
    assert result['status'] == 'failed'
    assert 'HTTP 401' in result['error']
    assert provider[1].echo not in caplog.text + json.dumps(result)


async def test_unknown_status_then_completion(cloud_registry, provider, monkeypatch):
    provider[1].cursor_status = 'A_FUTURE_STATUS'
    ready = progress_event(monkeypatch, lambda task: task.remote.get('status') == 'A_FUTURE_STATUS')
    dispatched = await cloud_registry.dispatch_task('cursor', 'unknown status')
    task = cloud_registry.tasks[dispatched['task_id']]
    await asyncio.wait_for(ready.wait(), timeout=5)
    assert task.status == TaskStatus.running
    provider[1].cursor_status = 'FINISHED'
    await cloud_registry.wait_task(task.task_id, timeout_sec=5)
    assert task.status == TaskStatus.completed


def test_ask_cloud_options(monkeypatch, capsys):
    client = types.SimpleNamespace(call=AsyncMock(return_value={'ok': True, 'result_text': 'cloud'}))
    monkeypatch.setattr('agent_bridge.hub_client.discover', AsyncMock(return_value=client))
    cli.main(['ask', 'remote', 'hello', '--repo', 'repo', '--ref', 'main', '--session-id', 'sess-test'])
    assert capsys.readouterr().out == 'cloud\n'
    kwargs = client.call.call_args.kwargs
    assert (kwargs['repo'], kwargs['ref'], kwargs['session_id']) == ('repo', 'main', 'sess-test')
