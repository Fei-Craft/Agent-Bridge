from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import httpx2
import pytest

from agent_bridge import cli, server
from agent_bridge import secrets as vault
from agent_bridge.adapters.acp import AcpAdapter
from agent_bridge.adapters.antigravity import AgyAdapter
from agent_bridge.adapters.cli import CliAdapter
from agent_bridge.adapters.codex import CodexAdapter
from agent_bridge.adapters.fake import FakeAdapter
from agent_bridge.config import AgentConfig, CliConfig, EnvConfig, load_config
from agent_bridge.hub import create_app, serve, validate_host
from agent_bridge.hub_client import HubClient, discover
from agent_bridge.registry import Registry
from agent_bridge.worker_env import build_worker_env, enforce_env_deny, hub_worker_context


@pytest.fixture
async def hub(bridge_home):
    registry = Registry.create(bridge_home)
    registry.config.hub.max_depth = 1
    app = create_app(registry, token='test-token')
    async with app.router.lifespan_context(app), httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url='http://hub',
        headers={'Authorization': 'Bearer test-token'}
    ) as client:
        yield client, registry


async def test_routes(hub, bridge_home, tmp_path):
    client, registry = hub
    info = json.loads((bridge_home / 'hub.json').read_text())
    assert info['pid'] == os.getpid()
    assert 'token' not in info
    if os.name != 'nt':
        assert (bridge_home / 'hub.json').stat().st_mode & 0o777 == 0o600
    assert registry.runtime_context == 'coordinator'
    assert registry.config.server.idle_exit_sec == 0
    assert (await client.get('/v1/health')).json() == {'ok': True}
    assert (await client.get('/v1/health', headers={'Authorization': ''})).status_code == 401
    assert (await client.get('/v1/sessions')).json()['sessions'] == []
    assert (await client.put('/v1/preferences', json={'mode': 'auto'})).json()['ok']
    body = {'agent': 'fake', 'message': 'echo', 'cwd': str(tmp_path)}
    dispatched = (await client.post('/v1/tasks', json=body)).json()
    tid, sid = dispatched['task_id'], dispatched['session_id']
    assert (await client.get(f'/v1/tasks/{tid}')).json()['ok']
    waited = (await client.get(f'/v1/tasks/{tid}/wait?timeout_sec=5')).json()
    assert not waited['timed_out']
    result = (await client.get(f'/v1/tasks/{tid}/result')).json()
    assert result['result_text'] == '[fake:fake] echo'
    assert (await client.get(f'/v1/sessions/{sid}/transcript?kinds=message_chunk')).json()['ok']
    assert (await client.get('/v1/sessions?active_only=true')).json()['ok']
    assert (await client.post(f'/v1/tasks/{tid}/cancel')).json()['ok']
    invoked = (await client.post('/v1/agents/fake/invoke', json={'message': 'invoke', 'cwd': str(tmp_path)})).json()
    assert invoked['result_text'] == '[fake:fake] invoke'
    assert (await client.delete(f'/v1/sessions/{sid}')).json()['ok']
    assert (await client.get('/v1/tasks/missing')).status_code == 400
    assert (await client.post('/v1/tasks', json={})).status_code == 400
    assert (await client.get('/v1/tasks/invalid/result?cursor=no')).status_code == 400
    assert (await client.post('/v1/tasks', content='{')).status_code == 400


@pytest.fixture
async def worker(hub, tmp_path, monkeypatch):
    client, registry = hub
    started, release = asyncio.Event(), asyncio.Event()
    environment = {}
    original = FakeAdapter.run_turn

    async def run(adapter, session, task):
        if task.message == 'root':
            environment.update(build_worker_env(worker_context=True, log_fill=False))
            started.set()
            await release.wait()
        return await original(adapter, session, task)

    monkeypatch.setattr(FakeAdapter, 'run_turn', run)
    root = (await client.post('/v1/tasks', json={'agent': 'fake', 'message': 'root', 'cwd': str(tmp_path)})).json()
    await asyncio.wait_for(started.wait(), 5)
    try:
        yield client, registry, root, environment
    finally:
        release.set()
        await registry.wait_task(root['task_id'], timeout_sec=5)


async def test_depth_and_self_dispatch(worker, tmp_path):
    client, registry, root, env = worker
    body = {'agent': 'fake', 'message': 'peer', 'cwd': str(tmp_path)}
    caller = {'Authorization': 'Bearer ' + env['AGENT_BRIDGE_HUB_TOKEN'], 'X-Agent-Bridge-Caller': 'forged'}
    assert (await client.post('/v1/tasks', json={**body, 'session_id': root['session_id']}, headers=caller)).status_code == 400
    peer = (await client.post('/v1/tasks', json=body, headers=caller)).json()
    assert peer['ok']
    assert registry.tasks[peer['task_id']].caller_session_id == root['session_id']
    assert registry.sessions[peer['session_id']].call_depth == 1
    registry.sessions[root['session_id']].call_depth = 1
    too_deep = await client.post('/v1/tasks', json=body, headers=caller)
    assert too_deep.status_code == 400
    assert 'max_depth' in too_deep.json()['error']
    owner = (await client.post('/v1/tasks', json=body, headers={'X-Agent-Bridge-Caller': 'unknown'})).json()
    assert owner['ok'] and registry.tasks[owner['task_id']].caller_session_id is None
    assert registry.sessions[owner['session_id']].call_depth == 0


@pytest.mark.parametrize('route', ['/v1/tasks', '/v1/agents/fake/invoke'])
async def test_worker_cannot_use_owner_callback_secret(worker, tmp_path, route):
    client, registry, _, env = worker
    await registry.deliveries.stop()
    vault.store_callback_secret('owner', 'owner-signing-secret' * 3)
    body = {'message': 'child', 'cwd': str(tmp_path), 'callback_url': 'https://8.8.8.8',
        'callback_secret_name': 'owner'}
    if route == '/v1/tasks':
        body['agent'] = 'fake'
    before = set(registry.tasks)
    response = await client.post(route, json=body, headers={'Authorization': 'Bearer ' + env['AGENT_BRIDGE_HUB_TOKEN']})
    assert response.status_code == 403 and 'admin-only' in response.json()['error']
    assert set(registry.tasks) == before
    response = await client.post(route, json=body)
    assert response.json()['ok']
    await registry.wait_task(response.json()['task_id'], timeout_sec=5)
    assert registry.tasks[response.json()['task_id']].callback_signed


@pytest.mark.parametrize('route', ['/v1/tasks', '/v1/agents/fake/invoke'])
async def test_worker_turn_ends_while_reading_body(worker, tmp_path, monkeypatch, route):
    client, registry, root, env = worker
    authenticated = asyncio.Event()
    authenticate = registry.worker_tokens.authenticate

    def check(token):
        result = authenticate(token)
        if result is not None:
            authenticated.set()
        return result

    monkeypatch.setattr(registry.worker_tokens, 'authenticate', check)
    before = set(registry.tasks)

    async def body():
        await asyncio.wait_for(authenticated.wait(), 5)
        registry.worker_tokens.active.discard(root['session_id'])
        yield json.dumps({'agent': 'fake', 'message': 'late child', 'cwd': str(tmp_path)}).encode()

    response = await client.post(route, content=body(), headers={'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + env['AGENT_BRIDGE_HUB_TOKEN']})
    assert response.status_code == 401
    assert set(registry.tasks) == before


@pytest.mark.parametrize(('method', 'path', 'body'), [
    ('POST', '/v1/approvals/forged/approve', {}),
    ('POST', '/v1/approvals/forged/deny', {}),
    ('GET', '/v1/approvals', None),
    ('PUT', '/v1/preferences', {'mode': 'auto'}),
    ('PUT', '/v1/remote/public-url', {'public_url': 'https://example.com'}),
    ('GET', '/v1/remote', None),
    ('POST', '/v1/ui/login-code', {}),
    ('GET', '/v1/sessions', None),
    ('DELETE', '/v1/sessions/other', None),
    ('GET', '/v1/tasks/other', None),
    ('GET', '/v1/tasks/other/wait', None),
    ('GET', '/v1/tasks/other/result', None),
    ('POST', '/v1/tasks/other/cancel', {}),
    ('GET', '/v1/sessions/other/transcript', None),
])
async def test_worker_cannot_access_admin_or_unowned_tasks(worker, method, path, body):
    client, _, _, env = worker
    response = await client.request(method, path, json=body,
        headers={'Authorization': 'Bearer ' + env['AGENT_BRIDGE_HUB_TOKEN']})
    assert response.status_code == 403


async def test_worker_owned_task_access_and_notify(worker, tmp_path):
    client, registry, root, env = worker
    token = env['AGENT_BRIDGE_HUB_TOKEN']
    headers = {'Authorization': 'Bearer ' + token, 'X-Agent-Bridge-Caller': 'victim'}
    assert token.startswith('abw_') and token != 'test-token'
    assert token not in registry.worker_tokens.scopes
    assert registry.worker_tokens.scopes[hashlib.sha256(token.encode()).hexdigest()] == root['session_id']
    assert 'AGENT_BRIDGE_HUB_TOKEN' not in registry.deliveries.worker_context
    assert 'AGENT_BRIDGE_CALLER_SESSION' not in env
    assert (await client.get('/v1/health', headers=headers)).status_code == 200
    assert (await client.get('/v1/agents', headers=headers)).status_code == 200
    assert (await client.get('/v1/tasks/' + root['task_id'], headers=headers)).status_code == 403
    assert (await client.get('/v1/sessions/' + root['session_id'] + '/transcript', headers=headers)).status_code == 403
    victim = (await client.post('/v1/tasks', json={'agent': 'fake', 'message': 'victim', 'cwd': str(tmp_path)})).json()
    await registry.wait_task(victim['task_id'], timeout_sec=5)
    headers['X-Agent-Bridge-Caller'] = victim['session_id']
    assert (await client.get('/v1/tasks/' + victim['task_id'], headers=headers)).status_code == 403
    assert (await client.post('/v1/tasks', headers=headers, json={'agent': 'fake', 'message': 'inject',
        'cwd': str(tmp_path), 'session_id': victim['session_id']})).status_code == 403
    child = (await client.post('/v1/tasks', headers=headers, json={
        'agent': 'fake', 'message': 'child', 'cwd': str(tmp_path), 'notify_caller': True})).json()
    assert child['ok'] and registry.tasks[child['task_id']].caller_session_id == root['session_id']
    for suffix in ('', '/wait?timeout_sec=5', '/result'):
        assert (await client.get('/v1/tasks/' + child['task_id'] + suffix, headers=headers)).status_code == 200
    assert (await client.get('/v1/sessions/' + child['session_id'] + '/transcript', headers=headers)).status_code == 200
    assert (await client.post('/v1/tasks/' + child['task_id'] + '/cancel', headers=headers, json={})).status_code == 200
    invoked = (await client.post('/v1/agents/fake/invoke', headers=headers,
        json={'message': 'invoked', 'cwd': str(tmp_path)})).json()
    assert invoked['status'] == 'completed'
    assert registry.tasks[invoked['task_id']].caller_session_id == root['session_id']
    followup = (await client.post('/v1/tasks', headers=headers, json={'agent': 'fake', 'message': 'followup',
        'cwd': str(tmp_path), 'session_id': invoked['session_id']})).json()
    assert followup['ok']
    await registry.wait_task(followup['task_id'], timeout_sec=5)
    registry._drop_task(invoked['task_id'])
    assert (await client.get('/v1/sessions/' + invoked['session_id'] + '/transcript', headers=headers)).status_code == 200
    owner_turn = (await client.post('/v1/tasks', json={'agent': 'fake', 'message': 'private owner followup',
        'cwd': str(tmp_path), 'session_id': invoked['session_id']})).json()
    await registry.wait_task(owner_turn['task_id'], timeout_sec=5)
    registry._drop_task(owner_turn['task_id'])
    assert (await client.get('/v1/sessions/' + invoked['session_id'] + '/transcript', headers=headers)).status_code == 403
    rejected = await client.post('/v1/tasks', json={'agent': 'fake', 'message': 'owner notify',
        'cwd': str(tmp_path), 'notify_caller': True}, headers={'X-Agent-Bridge-Caller': root['session_id']})
    assert rejected.status_code == 400 and 'caller session' in rejected.json()['error']
    await registry.flush_state()
    for path in registry.home.rglob('*'):
        if path.is_file():
            if os.name == 'nt' and path.name == 'hub.lock':
                assert path.stat().st_size == 1
                continue
            assert token.encode() not in path.read_bytes()


async def test_notification_turn_cannot_notify_again(worker, tmp_path):
    client, registry, root, env = worker
    registry.tasks[root['task_id']].notification_source_task_id = 'source'
    result = (await client.post('/v1/tasks', json={'agent': 'fake', 'message': 'child',
        'cwd': str(tmp_path), 'notify_caller': True},
        headers={'Authorization': 'Bearer ' + env['AGENT_BRIDGE_HUB_TOKEN']})).json()
    assert result['ok'] and not registry.tasks[result['task_id']].notify_caller


async def test_worker_single_turn_token_revoked_after_completion(hub, tmp_path, monkeypatch):
    client, registry = hub
    captured = []
    original = FakeAdapter.run_turn

    async def run(adapter, session, task):
        env = build_worker_env(worker_context=True, log_fill=False)
        captured.append(env['AGENT_BRIDGE_HUB_TOKEN'])
        assert registry.worker_tokens.authenticate(captured[-1]) == session.session_id
        return await original(adapter, session, task)

    monkeypatch.setattr(FakeAdapter, 'run_turn', run)
    result = (await client.post('/v1/agents/fake/invoke', json={'message': 'first', 'cwd': str(tmp_path)})).json()
    assert result['status'] == 'completed'
    assert (await client.get('/v1/health', headers={'Authorization': 'Bearer ' + captured[0]})).status_code == 401
    assert not registry.worker_tokens.scopes


@pytest.mark.parametrize(('protocol', 'adapter_type', 'script'), [
    ('cli', CliAdapter, 'fake_cli.py'),
    ('codex', CodexAdapter, 'fake_codex.py'),
    ('agy', AgyAdapter, 'fake_agy.py'),
])
async def test_one_shot_process_exit_revokes_token_before_turn_returns(hub, tmp_path, monkeypatch, protocol, adapter_type, script):
    client, registry = hub
    command = [sys.executable, str(Path(__file__).with_name(script))]
    registry.config.agents['one-shot'] = AgentConfig(name='one-shot', protocol=protocol,
        command=command, cli=CliConfig(parser='jsonl', prompt_stdin=True))
    monkeypatch.setattr(CodexAdapter, '_base_cmd', lambda self, env=None: command)
    spawn = asyncio.create_subprocess_exec
    captured = []

    async def capture(*args, **kwargs):
        captured.append(kwargs['env']['AGENT_BRIDGE_HUB_TOKEN'])
        return await spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', capture)
    run_turn = adapter_type.run_turn

    async def run(adapter, session, task):
        result = await run_turn(adapter, session, task)
        assert session.session_id in registry.worker_tokens.active
        assert registry.worker_tokens.processes[session.session_id].returncode is not None
        assert registry.worker_tokens.authenticate(captured[-1]) is None
        return result

    monkeypatch.setattr(adapter_type, 'run_turn', run)
    result = (await client.post('/v1/agents/one-shot/invoke', json={'message': 'hello', 'cwd': str(tmp_path)})).json()
    assert result['status'] == 'completed'
    assert captured[0].startswith('abw_') and captured[0] != 'test-token'
    assert not registry.worker_tokens.scopes


async def test_hub_client_does_not_send_caller_header(monkeypatch):
    original = httpx2.AsyncClient

    def handle(request):
        assert 'X-Agent-Bridge-Caller' not in request.headers
        return httpx2.Response(200, json={'ok': True})

    monkeypatch.setenv('AGENT_BRIDGE_CALLER_SESSION', 'forged')
    monkeypatch.setattr(httpx2, 'AsyncClient', lambda **kwargs: original(transport=httpx2.MockTransport(handle), **kwargs))
    assert (await HubClient('http://hub', 'abw_' + 'h' * 43).call('list_agents'))['ok']


async def test_lifecycle_cleanup(bridge_home):
    app = create_app(Registry.create(bridge_home))
    async with app.router.lifespan_context(app):
        assert (bridge_home / 'hub.json').exists()
    assert not (bridge_home / 'hub.json').exists()


async def test_second_hub_cannot_replace_live_discovery(hub, bridge_home):
    client, _ = hub
    before = (bridge_home / 'hub.json').read_bytes()
    token = vault.get(vault.hub_key(bridge_home))
    other = create_app(Registry.create(bridge_home))
    with pytest.raises((RuntimeError, TimeoutError)):
        async with other.router.lifespan_context(other):
            pass
    assert (bridge_home / 'hub.json').read_bytes() == before
    assert vault.get(vault.hub_key(bridge_home)) == token
    assert (await client.get('/v1/health')).status_code == 200


async def test_acp_echo_and_spawn_environment(hub, tmp_path, monkeypatch):
    client, registry = hub
    registry.config.agents['echo'] = AgentConfig(
        name='echo', protocol='acp', command=[sys.executable, str(Path(__file__).with_name('echo_agent.py'))],
    )
    original_spawn = asyncio.create_subprocess_exec
    spawned = []

    async def capture_env(*args, **kwargs):
        spawned.append(dict(kwargs['env']))
        return await original_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', capture_env)
    result = (await client.post('/v1/agents/echo/invoke', json={'message': 'hello', 'cwd': str(tmp_path)})).json()
    assert result['result_text'] == 'echo:hello'
    assert spawned[0]['AGENT_BRIDGE_HUB_URL'] == 'http://127.0.0.1:8765'
    token = spawned[0]['AGENT_BRIDGE_HUB_TOKEN']
    assert token.startswith('abw_') and token != 'test-token'
    assert 'AGENT_BRIDGE_CALLER_SESSION' not in spawned[0]
    headers = {'Authorization': 'Bearer ' + token}
    assert (await client.get('/v1/health', headers=headers)).status_code == 401
    original_turn = AcpAdapter.run_turn

    async def followup(adapter, session, task):
        assert (await client.get('/v1/health', headers=headers)).status_code == 200
        return await original_turn(adapter, session, task)

    monkeypatch.setattr(AcpAdapter, 'run_turn', followup)
    again = (await client.post('/v1/agents/echo/invoke', json={
        'message': 'again', 'cwd': str(tmp_path), 'session_id': result['session_id']})).json()
    assert again['result_text'] == 'echo:again' and len(spawned) == 1
    assert (await client.get('/v1/health', headers=headers)).status_code == 401
    adapter = registry._adapters[result['session_id']]
    process = registry.worker_tokens.processes[result['session_id']]
    process.kill()
    await process.wait()
    registry.worker_tokens.active.add(result['session_id'])
    assert (await client.get('/v1/health', headers=headers)).status_code == 401
    registry.worker_tokens.active.discard(result['session_id'])
    monkeypatch.setattr(AcpAdapter, 'run_turn', original_turn)
    restarted = (await client.post('/v1/agents/echo/invoke', json={
        'message': 'restart', 'cwd': str(tmp_path), 'session_id': result['session_id']})).json()
    assert restarted['result_text'] == 'echo:restart' and len(spawned) == 2
    assert spawned[1]['AGENT_BRIDGE_HUB_TOKEN'] != token
    assert adapter is registry._adapters[result['session_id']]
    await registry.end_session(result['session_id'])
    assert hashlib.sha256(token.encode()).hexdigest() not in registry.worker_tokens.scopes
    assert not registry.worker_tokens.scopes


def test_hub_config(bridge_home):
    bridge_home.mkdir()
    (bridge_home / 'agents.toml').write_text('[hub]\nhost="localhost"\nport=9000\nmax_depth=2\n')
    config = load_config(bridge_home)
    assert (config.hub.host, config.hub.port, config.hub.max_depth) == ('localhost', 9000, 2)


def test_non_loopback():
    for host in ('127.0.0.1', '::1', 'localhost'):
        validate_host(host)
    for host in ('0.0.0.0', '::', 'example.com'):
        with pytest.raises(ValueError, match='loopback'):
            validate_host(host)


def test_allow_remote_cli_flag_is_removed(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(['serve', '--allow-remote'])
    assert exc.value.code == 2
    assert 'unrecognized arguments: --allow-remote' in capsys.readouterr().err


async def test_discovery_and_proxy(hub, bridge_home, monkeypatch):
    client, _ = hub
    original = httpx2.AsyncClient

    def asgi_client(**kwargs):
        return original(transport=client._transport, **kwargs)

    monkeypatch.setattr(httpx2, 'AsyncClient', asgi_client)
    discovered = await discover(bridge_home)
    assert discovered is not None
    assert (await discovered.call('list_sessions'))['ok']
    monkeypatch.setenv('AGENT_BRIDGE_HUB_URL', 'https://env-hub')
    monkeypatch.setenv('AGENT_BRIDGE_HUB_TOKEN', 'test-token')
    discovered = await discover(bridge_home)
    assert discovered.url == 'https://env-hub'
    async with server.lifespan(server.mcp) as backend:
        assert isinstance(backend, HubClient)
        ctx = types.SimpleNamespace(request_context=types.SimpleNamespace(lifespan_context=backend))
        assert (await server.list_sessions(ctx))['ok']
    monkeypatch.setenv('AGENT_BRIDGE_HUB', 'off')
    assert await discover(bridge_home) is None
    monkeypatch.delenv('AGENT_BRIDGE_HUB')
    monkeypatch.delenv('AGENT_BRIDGE_HUB_URL')
    monkeypatch.delenv('AGENT_BRIDGE_HUB_TOKEN')
    monkeypatch.setattr('agent_bridge.hub_client.psutil.pid_exists', lambda pid: False)
    assert await discover(bridge_home) is None


async def test_discovery_empty_and_unhealthy(bridge_home, monkeypatch):
    assert await discover(bridge_home) is None
    monkeypatch.setenv('AGENT_BRIDGE_HUB_URL', 'http://127.0.0.1:1')
    assert await discover(bridge_home) is None


@pytest.mark.parametrize('source', ['env', 'file'])
@pytest.mark.parametrize(('url', 'allowed'), [
    ('http://127.0.0.1:8765', True), ('http://127.22.3.4', True),
    ('http://[::1]:8765', True), ('http://localhost', True), ('https://hub.example', True),
    ('http://hub.example', False), ('http://192.168.1.2', False), ('file:///etc/passwd', False),
    ('https://user:password@hub.example', False), ('https://hub.example/#fragment', False),
    ('http://localhost.evil.example', False), ('', False),
])
async def test_discovery_validates_url_before_sending_token(bridge_home, monkeypatch, source, url, allowed):
    monkeypatch.delenv('AGENT_BRIDGE_HUB_URL', raising=False)
    monkeypatch.setenv('AGENT_BRIDGE_HUB_TOKEN', 'test-secret')
    if source == 'env':
        monkeypatch.setenv('AGENT_BRIDGE_HUB_URL', url)
    else:
        bridge_home.mkdir(exist_ok=True)
        (bridge_home / 'hub.json').write_text(json.dumps({'pid': os.getpid(), 'url': url}), encoding='utf-8')
    sent = []
    original = httpx2.AsyncClient

    def handle(request):
        sent.append(request)
        return httpx2.Response(200, json={'ok': True})

    monkeypatch.setattr(httpx2, 'AsyncClient', lambda **kwargs: original(transport=httpx2.MockTransport(handle), **kwargs))
    assert (await discover(bridge_home) is not None) == allowed
    assert bool(sent) == allowed


async def test_mcp_proxy_honors_manual_host_mode(hub, monkeypatch, tmp_path):
    client, registry = hub
    original = httpx2.AsyncClient
    monkeypatch.setattr(httpx2, 'AsyncClient', lambda **kwargs: original(transport=client._transport, **kwargs))
    monkeypatch.setenv('AGENT_BRIDGE_MODE', 'manual')
    async with server.lifespan(server.mcp) as backend:
        assert isinstance(backend, HubClient)
        blocked = await backend.call('dispatch_task', agent='fake', message='no request', cwd=str(tmp_path))
        assert blocked['ok'] is False
        assert 'manual' in blocked['error']
        assert registry.tasks == {}
        assert (await backend.call('list_agents'))['coordinator']['mode'] == 'manual'
        accepted = await backend.call('dispatch_task', agent='fake', message='requested', cwd=str(tmp_path), user_requested=True)
        assert accepted['ok'] is True
        assert not (await backend.call('set_preferences', mode='invalid'))['ok']
        assert backend.host_manual_mode
        changed = await backend.call('set_preferences', mode='auto')
        assert changed['ok'] and backend.host_manual_mode
        assert changed['coordinator']['mode'] == 'manual'
        assert changed['coordinator']['hub_mode'] == 'auto'
        listed = (await backend.call('list_agents'))['coordinator']
        assert listed['mode'] == 'manual' and listed['hub_mode'] == 'auto'
        automatic = await backend.call('dispatch_task', agent='fake', message='automatic', cwd=str(tmp_path))
        assert not automatic['ok']
        changed = await backend.call('set_preferences', mode='manual')
        assert changed['ok'] and changed['coordinator']['mode'] == 'manual'
        assert changed['coordinator']['hub_mode'] == 'manual'
        assert not (await backend.call('dispatch_task', agent='fake', message='blocked again', cwd=str(tmp_path)))['ok']


async def test_mcp_proxies_follow_current_shared_preferences(hub, monkeypatch, tmp_path):
    client, _ = hub
    original = httpx2.AsyncClient
    monkeypatch.setattr(httpx2, 'AsyncClient', lambda **kwargs: original(transport=client._transport, **kwargs))
    monkeypatch.delenv('AGENT_BRIDGE_MODE', raising=False)
    async with server.lifespan(server.mcp) as first, server.lifespan(server.mcp) as second:
        assert (await first.call('set_preferences', mode='manual'))['ok']
        assert (await first.call('list_agents'))['coordinator']['mode'] == 'manual'
        assert not (await first.call('dispatch_task', agent='fake', message='blocked', cwd=str(tmp_path)))['ok']
        changed = await second.call('set_preferences', mode='auto')
        assert changed['ok'] and changed['coordinator']['mode'] == 'auto'
        assert 'hub_mode' not in changed['coordinator']
        assert (await first.call('list_agents'))['coordinator']['mode'] == 'auto'
        invoked = await first.call('invoke', agent='fake', message='automatic', cwd=str(tmp_path), timeout_sec=5)
        assert invoked['ok'] and invoked['status'] == 'completed'


async def test_client_errors():
    result = await HubClient('http://127.0.0.1:1').call('list_agents')
    assert result['ok'] is False
    assert (await HubClient('http://hub').call('invalid_tool'))['error_type'] == 'KeyError'


def test_worker_environment():
    values = dict(AGENT_BRIDGE_HUB_URL='http://hub', AGENT_BRIDGE_HUB_TOKEN='secret', AGENT_BRIDGE_CALLER_SESSION='sess')
    token = hub_worker_context.set(values)
    try:
        config = EnvConfig(deny=['AGENT_BRIDGE_*'])
        env = enforce_env_deny(build_worker_env(config=config, worker_context=True, log_fill=False), config)
        for key, value in values.items():
            assert env[key] == value
    finally:
        hub_worker_context.reset(token)


def test_cli_call_ask(monkeypatch, capsys, tmp_path):
    client = types.SimpleNamespace(call=AsyncMock(return_value={'ok': True, 'result_text': 'answer'}))
    monkeypatch.setattr('agent_bridge.hub_client.discover', AsyncMock(return_value=client))
    cli.main(['call', 'list_sessions', '--active_only', 'true'])
    assert json.loads(capsys.readouterr().out)['ok']
    client.call.assert_awaited_with('list_sessions', active_only=True)
    cli.main(['call', 'invoke', '--agent', 'opencode', '--model', 'opendesign/deepseek-v4.1-flash-eco'])
    assert json.loads(capsys.readouterr().out)['ok']
    client.call.assert_awaited_with('invoke', agent='opencode', model='opendesign/deepseek-v4.1-flash-eco')
    client.call.return_value = {'ok': True, 'task_id': 'task-test'}
    client.wait_task = AsyncMock(return_value={'ok': True, 'status': 'completed'})
    client.get_result = AsyncMock(return_value={'ok': True, 'status': 'completed', 'result_text': 'answer'})
    cli.main(['ask', 'fake', 'hello', '--cwd', str(tmp_path), '--timeout', '5'])
    assert capsys.readouterr().out == 'answer\n'
    client.call.assert_awaited_with('dispatch_task', agent='fake', message='hello', cwd=str(tmp_path))
    monkeypatch.setattr('agent_bridge.hub_client.discover', AsyncMock(return_value=None))
    with pytest.raises(RuntimeError, match='no running hub'):
        cli.main(['call', 'list_agents'])


@pytest.mark.parametrize('model,effort', [('opendesign/deepseek-v4.1-flash-eco', 'high'), ('', '')])
def test_cli_ask_model_effort(monkeypatch, capsys, tmp_path, model, effort):
    client = types.SimpleNamespace(call=AsyncMock(return_value={'ok': True, 'task_id': 'task-test'}),
        wait_task=AsyncMock(return_value={'ok': True, 'status': 'completed'}),
        get_result=AsyncMock(return_value={'ok': True, 'status': 'completed', 'result_text': 'answer'}))
    monkeypatch.setattr('agent_bridge.hub_client.discover', AsyncMock(return_value=client))
    cli.main(['ask', 'opencode', 'hello', '--cwd', str(tmp_path), '--model', model, '--effort', effort])
    assert capsys.readouterr().out == 'answer\n'
    client.call.assert_awaited_with('dispatch_task', agent='opencode', message='hello', cwd=str(tmp_path),
                                   model=model, effort=effort)


@pytest.mark.parametrize('option', ['--model', '--effort'])
def test_cli_ask_missing_option_value(monkeypatch, capsys, option):
    discover = AsyncMock()
    monkeypatch.setattr('agent_bridge.hub_client.discover', discover)
    with pytest.raises(SystemExit) as exc:
        cli.main(['ask', 'opencode', 'hello', option])
    assert exc.value.code == 2
    assert f'argument {option}: expected one argument' in capsys.readouterr().err
    discover.assert_not_awaited()


@pytest.mark.parametrize('remote', [False, True])
def test_all_listeners_disable_access_logs(bridge_home, monkeypatch, remote):
    registry = Registry.create(bridge_home)
    registry.config.hub.remote.enabled = remote
    monkeypatch.setattr(Registry, 'create', lambda **_: registry)
    configurations = []

    def run(app, **kwargs):
        configurations.append(kwargs)

    class Server:
        def __init__(self, config):
            configurations.append({'access_log': config.access_log})

        async def serve(self):
            return

    monkeypatch.setattr('agent_bridge.hub.uvicorn.run', run)
    monkeypatch.setattr('agent_bridge.hub._EmbeddedServer', Server)
    serve()
    assert len(configurations) == (2 if remote else 1)
    assert all(config['access_log'] is False for config in configurations)
