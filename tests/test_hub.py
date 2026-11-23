from __future__ import annotations

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
from agent_bridge.config import AgentConfig, EnvConfig, load_config
from agent_bridge.hub import create_app, validate_host
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


async def test_depth_and_self_dispatch(hub, tmp_path):
    client, _ = hub
    body = {'agent': 'fake', 'message': 'peer', 'cwd': str(tmp_path)}
    root = (await client.post('/v1/tasks', json=body)).json()
    caller = {'X-Agent-Bridge-Caller': root['session_id']}
    assert (await client.post('/v1/tasks', json={**body, 'session_id': root['session_id']}, headers=caller)).status_code == 400
    peer = (await client.post('/v1/tasks', json=body, headers=caller)).json()
    assert peer['ok']
    too_deep = await client.post('/v1/tasks', json=body, headers={'X-Agent-Bridge-Caller': peer['session_id']})
    assert too_deep.status_code == 400
    assert 'max_depth' in too_deep.json()['error']
    assert (await client.post('/v1/tasks', json=body, headers={'X-Agent-Bridge-Caller': 'unknown'})).status_code == 400


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
    original_env = AcpAdapter._env
    spawned = []

    def capture_env(adapter):
        env = original_env(adapter)
        spawned.append(env)
        return env

    monkeypatch.setattr(AcpAdapter, '_env', capture_env)
    result = (await client.post('/v1/agents/echo/invoke', json={'message': 'hello', 'cwd': str(tmp_path)})).json()
    assert result['result_text'] == 'echo:hello'
    assert spawned[0]['AGENT_BRIDGE_HUB_URL'] == 'http://127.0.0.1:8765'
    assert spawned[0]['AGENT_BRIDGE_HUB_TOKEN'] == 'test-token'
    assert spawned[0]['AGENT_BRIDGE_CALLER_SESSION'] == result['session_id']


def test_hub_config(bridge_home):
    bridge_home.mkdir()
    (bridge_home / 'agents.toml').write_text('[hub]\nhost="localhost"\nport=9000\nmax_depth=2\n')
    config = load_config(bridge_home)
    assert (config.hub.host, config.hub.port, config.hub.max_depth) == ('localhost', 9000, 2)


def test_non_loopback():
    for host in ('127.0.0.1', '::1', 'localhost'):
        validate_host(host)
    for host in ('0.0.0.0', '::', 'example.com'):
        with pytest.raises(ValueError, match='allow-remote'):
            validate_host(host)
        validate_host(host, True)


async def test_discovery_and_proxy(hub, bridge_home, monkeypatch):
    client, _ = hub
    original = httpx2.AsyncClient

    def asgi_client(**kwargs):
        return original(transport=client._transport, **kwargs)

    monkeypatch.setattr(httpx2, 'AsyncClient', asgi_client)
    discovered = await discover(bridge_home)
    assert discovered is not None
    assert (await discovered.call('list_sessions'))['ok']
    monkeypatch.setenv('AGENT_BRIDGE_HUB_URL', 'http://env-hub')
    monkeypatch.setenv('AGENT_BRIDGE_HUB_TOKEN', 'test-token')
    discovered = await discover(bridge_home)
    assert discovered.url == 'http://env-hub'
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
