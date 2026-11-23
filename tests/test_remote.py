from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import socket
import sys
import time
from unittest.mock import AsyncMock

import httpx2
import pytest
from pydantic import ValidationError

from agent_bridge import cli
from agent_bridge.adapters.fake import FakeAdapter
from agent_bridge.config import AgentConfig, HubConfig, RemoteConfig, load_config
from agent_bridge.hub import create_app
from agent_bridge.models import ProcState, Session, Task, TaskStatus
from agent_bridge.registry import Registry
from agent_bridge.remote import create_remote_app
from agent_bridge.tunnel import run_tunnel
from agent_bridge.worker_env import build_worker_env


@pytest.fixture
async def gateway(bridge_home, tmp_path):
    config = load_config(bridge_home)
    config.hub.remote = RemoteConfig(enabled=True, public_url='http://127.0.0.1:8766',
        remote_callable_agents=['fake', 'cloud'], remote_workspaces={'project': str(tmp_path)})
    config.agents['cloud'] = AgentConfig(name='cloud', protocol='cloud', cloud={'provider': 'devin', 'org_id': 'org'})
    registry = Registry.create(bridge_home, config)
    await registry.start()
    await registry.deliveries.stop()
    session = Session(session_id='sess_origin', agent='cloud', cwd='', proc_state=ProcState.busy)
    task = Task(task_id='task_origin', session_id=session.session_id, agent='cloud', message='original', cwd='', status=TaskStatus.running)
    registry.sessions[session.session_id] = session
    registry.tasks[task.task_id] = task
    registry._done[task.task_id] = asyncio.Event()
    scope, instructions = registry.remote.issue(task)
    token = re.search(r'abr_[A-Za-z0-9_-]{43}', instructions)[0]
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=create_remote_app(registry.remote)),
        base_url='http://gateway', headers={'Authorization': 'Bearer ' + token}) as client:
        yield client, registry, scope, token
    await registry.stop()


def payload(**kwargs):
    return {'agent': 'fake', 'message': 'hello', 'workspace': 'project', **kwargs}


async def test_remote_workers_never_receive_admin_credentials(gateway, monkeypatch):
    client, registry, _, _ = gateway
    registry.deliveries.worker_context = {'AGENT_BRIDGE_HUB_URL': 'http://admin', 'AGENT_BRIDGE_HUB_TOKEN': 'unrestricted-admin'}
    monkeypatch.setenv('AGENT_BRIDGE_HUB_TOKEN', 'inherited-admin')
    monkeypatch.setenv('AGENT_BRIDGE_HUB_URL', 'http://admin')
    captured = []
    original = FakeAdapter.run_turn

    async def run(adapter, session, task):
        captured.append(build_worker_env(worker_context=True))
        return await original(adapter, session, task)

    monkeypatch.setattr(FakeAdapter, 'run_turn', run)
    result = (await client.post('/v1/agents/fake/invoke', json=payload())).json()
    assert result['ok']
    assert 'AGENT_BRIDGE_HUB_TOKEN' not in captured[0]
    assert 'AGENT_BRIDGE_HUB_URL' not in captured[0]
    assert captured[0]['AGENT_BRIDGE_HUB'] == 'off'


def test_remote_config_validation(tmp_path):
    for host in ['0.0.0.0', '::', 'public.example', '192.168.1.3']:
        with pytest.raises(ValidationError, match='loopback'):
            RemoteConfig(host=host)
    with pytest.raises(ValidationError, match='separate ports'):
        HubConfig(remote={'enabled': True, 'port': 8765})
    for name in ['../project', '/project', 'a/b', '..']:
        with pytest.raises(ValidationError, match='never paths'):
            RemoteConfig(remote_workspaces={name: str(tmp_path)})
    with pytest.raises(ValidationError, match='absolute'):
        RemoteConfig(remote_workspaces={'project': 'relative'})
    assert not RemoteConfig().enabled


async def test_dispatch_invoke_status_result_cancel_and_audit(gateway, monkeypatch):
    client, registry, scope, token = gateway
    response = await client.post('/v1/agents/fake/invoke', json=payload(message='echo ' + token), headers={'CF-Ray': token})
    result = response.json()
    assert result['ok'] and result['status'] == 'completed'
    assert token not in response.text
    tid = result['task_id']
    assert (await client.get('/v1/tasks/' + tid)).json()['status'] == 'completed'
    assert (await client.get('/v1/tasks/' + tid + '/result')).json()['ok']
    assert registry.tasks[tid].call_depth == 1
    assert registry.sessions[registry.tasks[tid].session_id].call_depth == 1
    assert not any(key.lower().startswith('access-control-') for key in response.headers)
    monkeypatch.setenv('AGENT_BRIDGE_FAKE_DELAY', '5')
    running = (await client.post('/v1/tasks', json=payload())).json()
    assert (await client.post('/v1/tasks/' + running['task_id'] + '/cancel', json={})).json()['ok']
    await registry.flush_state()
    audit = registry.home / 'remote-audit.jsonl'
    assert audit.stat().st_mode & 0o777 == 0o600
    records = [json.loads(line) for line in audit.read_text().splitlines()]
    assert records[0]['token_id'] == hashlib.sha256(token.encode()).hexdigest()[:12]
    assert records[0]['workspace'] == 'project' and records[0]['outcome'] == 'accepted'
    assert records[0]['cf_ray'] == '[REDACTED]'
    assert scope.digest == hashlib.sha256(token.encode()).hexdigest()
    assert token not in repr(scope)
    for path in registry.home.rglob('*'):
        if path.is_file():
            assert token.encode() not in path.read_bytes(), path


@pytest.mark.parametrize('method,path', [
    ('GET', '/v1/health'), ('GET', '/v1/agents'), ('GET', '/v1/sessions'),
    ('GET', '/v1/sessions/sess_origin/transcript'), ('PUT', '/v1/preferences'),
    ('GET', '/v1/approvals'), ('GET', '/v1/remote'), ('GET', '/v1/tasks'),
    ('OPTIONS', '/v1/tasks'), ('DELETE', '/v1/tasks/task_origin'), ('GET', '/v1/tasks/task_origin/wait'),
])
async def test_admin_and_unknown_surface_404(gateway, method, path):
    response = await gateway[0].request(method, path)
    assert response.status_code == 404
    assert not any(key.lower().startswith('access-control-') for key in response.headers)


@pytest.mark.parametrize('kind', ['missing', 'invalid', 'raw', 'expired', 'revoked'])
async def test_auth(gateway, kind):
    client, registry, scope, token = gateway
    headers = {}
    if kind in {'missing', 'invalid'}:
        headers['Authorization'] = '' if kind == 'missing' else 'Bearer abr_' + 'x' * 43
    elif kind == 'raw':
        headers['Authorization'] = token
    elif kind == 'expired':
        scope.expires = time.time() - 1
    else:
        registry.remote.revoke(scope)
    assert (await client.post('/v1/tasks', json=payload(), headers=headers)).status_code == 401


async def test_token_isolation_and_finish_grace(gateway):
    client, registry, scope, token = gateway
    tid = (await client.post('/v1/tasks', json=payload())).json()['task_id']
    other, instructions = registry.remote.issue(registry.tasks['task_origin'])
    other_token = re.search(r'abr_[A-Za-z0-9_-]{43}', instructions)[0]
    assert other is not scope
    assert (await client.get('/v1/tasks/' + tid, headers={'Authorization': 'Bearer ' + other_token})).status_code == 404
    assert (await client.get('/v1/tasks/task_origin')).status_code == 404
    registry.remote.finish('task_origin')
    assert (await client.post('/v1/tasks', json=payload())).status_code == 401
    assert (await client.get('/v1/tasks/' + tid + '/result')).status_code == 200
    scope.fetch_until = time.time() - 1
    assert (await client.get('/v1/tasks/' + tid)).status_code == 401
    assert token not in repr(registry.remote.scopes)


async def test_reused_session_preserves_inherited_depth(gateway):
    _, registry, scope, _ = gateway
    first = await registry.dispatch_task('fake', 'first', cwd=scope.workspaces['project'])
    await registry._done[first['task_id']].wait()
    registry.sessions[scope.session_id].call_depth = 2
    followed = await registry.dispatch_task('fake', 'follow-up', cwd=scope.workspaces['project'],
        session_id=first['session_id'], caller_session_id=scope.session_id)
    assert registry.tasks[followed['task_id']].call_depth == 3
    assert registry.sessions[first['session_id']].call_depth == 3
    await registry._done[followed['task_id']].wait()
    with pytest.raises(ValueError, match='max_depth'):
        await registry.dispatch_task('fake', 'too deep', cwd=scope.workspaces['project'],
            caller_session_id=first['session_id'])


@pytest.mark.parametrize('body,code', [
    (payload(agent='unknown'), 403), (payload(workspace='../project'), 403),
    (payload(workspace='/etc'), 403), (payload(cwd='/etc'), 400),
    (payload(session_id='sess_origin'), 400), (payload(callback_url='http://internal'), 400),
    (payload(message=''), 400), (payload(message=None), 400), (payload(notify_caller='true'), 400),
    (payload(timeout_sec=-1), 400), (payload(model={}), 400),
])
async def test_scope_and_body_validation(gateway, body, code):
    assert (await gateway[0].post('/v1/tasks', json=body)).status_code == code


async def test_limits_json_depth_self_and_access(gateway, monkeypatch):
    client, registry, scope, _ = gateway
    cfg = registry.remote.config
    assert (await client.post('/v1/tasks', content='{}')).status_code == 415
    assert (await client.post('/v1/tasks', content='[]', headers={'Content-Type': 'application/json'})).status_code == 400
    assert (await client.post('/v1/tasks', content='{', headers={'Content-Type': 'application/json'})).status_code == 400
    cfg.max_body_bytes = 5
    assert (await client.post('/v1/tasks', json=payload())).status_code == 413
    cfg.max_body_bytes = 65536
    assert (await client.post('/v1/tasks', json=payload(agent='cloud'))).status_code == 403
    scope.depth_budget = 0
    assert (await client.post('/v1/tasks', json=payload())).status_code == 403
    scope.depth_budget = 3
    cfg.require_cf_access = True
    assert (await client.post('/v1/tasks', json=payload())).status_code == 403
    assert (await client.post('/v1/tasks', json=payload(), headers={'Cf-Access-Jwt-Assertion': 'present'})).status_code == 200
    cfg.require_cf_access = False
    cfg.max_concurrent_per_token = 1
    for tid in list(scope.owned_tasks):
        await registry.wait_task(tid, timeout_sec=5)
    monkeypatch.setenv('AGENT_BRIDGE_FAKE_DELAY', '5')
    task = (await client.post('/v1/tasks', json=payload())).json()
    assert (await client.post('/v1/tasks', json=payload())).status_code == 429
    await registry.cancel_task(task['task_id'])
    scope.max_calls = scope.calls
    assert (await client.post('/v1/tasks', json=payload())).status_code == 429
    cfg.rate_limit_per_min = 1
    assert (await client.get('/v1/tasks/' + task['task_id'])).status_code == 429


@pytest.mark.parametrize('approved', [True, False])
async def test_approval_workflow_via_admin(gateway, monkeypatch, approved):
    client, registry, scope, _ = gateway
    registry.remote.config.require_approval = True
    ready = asyncio.Event()
    original = registry.remote.approve

    async def approve(*args):
        ready.set()
        await original(*args)

    monkeypatch.setattr(registry.remote, 'approve', approve)
    pending = asyncio.create_task(client.post('/v1/tasks', json=payload()))
    await asyncio.wait_for(ready.wait(), 5)
    app = create_app(registry, token='admin-approval-credential')
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url='http://admin',
        headers={'Authorization': 'Bearer admin-approval-credential'}) as admin:
        approvals = (await admin.get('/v1/approvals')).json()['approvals']
        assert len(approvals) == 1 and not scope.owned_tasks
        action = 'approve' if approved else 'deny'
        assert (await admin.post('/v1/approvals/' + approvals[0]['approval_id'] + '/' + action, json={})).json()['ok']
        assert (await pending).status_code == (200 if approved else 403)
        assert (await admin.get('/v1/approvals')).json()['approvals'] == []


async def test_approval_expiry(gateway):
    client, registry, scope, _ = gateway
    registry.remote.config.require_approval = True
    scope.expires = time.time() + 0.02
    assert (await client.post('/v1/tasks', json=payload())).status_code == 403
    assert not registry.remote.approvals


async def test_chunked_body_limit_and_public_url_validation(gateway):
    client, registry, _, _ = gateway
    registry.remote.config.max_body_bytes = 10

    async def chunks():
        yield b'{"message":'
        yield b'"too large"}'

    response = await client.post('/v1/tasks', content=chunks(), headers={'Content-Type': 'application/json'})
    assert response.status_code == 413
    for url in ['', 'http://public.example', 'https://user:password@example.com',
        'https://example.com/?token=bad', 'http://127.0.0.1:8765']:
        with pytest.raises(ValueError):
            registry.remote.set_public_url(url)
    assert registry.remote.set_public_url('https://gateway.example.com')['public_url'] == 'https://gateway.example.com'


@pytest.mark.parametrize('name', [None, 'existing-tunnel'])
async def test_tunnel_fake_process(monkeypatch, name, capsys):
    client = AsyncMock()
    client.call.side_effect = [dict(ok=True, enabled=True, host='127.0.0.1', port=8766),
        dict(ok=True, public_url='https://example.trycloudflare.com')]
    process = AsyncMock()
    process.returncode = 0
    process.stdout.readline.side_effect = [b'url https://example.trycloudflare.com\n', b'']
    process.wait.return_value = 0
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr('agent_bridge.tunnel.shutil.which', lambda name: '/official/cloudflared')
    monkeypatch.setattr('agent_bridge.tunnel.asyncio.create_subprocess_exec', spawn)
    await run_tunnel(client, name=name)
    command = spawn.call_args.args
    assert 'http://127.0.0.1:8766' in command and '8765' not in ' '.join(command)
    assert ('run' in command) == bool(name)
    client.call.assert_awaited_with('set_remote_public_url', public_url='https://example.trycloudflare.com')
    assert 'URL configured' in capsys.readouterr().out


async def test_tunnel_missing_and_disabled(monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr('agent_bridge.tunnel.shutil.which', lambda name: None)
    with pytest.raises(RuntimeError, match='not installed'):
        await run_tunnel(client)
    monkeypatch.setattr('agent_bridge.tunnel.shutil.which', lambda name: '/official/cloudflared')
    client.call.return_value = {'ok': True, 'enabled': False}
    with pytest.raises(ValueError, match='separate remote gateway'):
        await run_tunnel(client)


def test_approvals_cli(monkeypatch, capsys):
    client = AsyncMock()
    client.call.return_value = {'ok': True, 'approvals': []}
    monkeypatch.setattr('agent_bridge.hub_client.discover', AsyncMock(return_value=client))
    cli.main(['approvals', 'list'])
    assert json.loads(capsys.readouterr().out)['approvals'] == []
    cli.main(['approvals', 'approve', 'approval_example'])
    client.call.assert_awaited_with('approve_approval', approval_id='approval_example')


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX subprocess SIGTERM semantics')
async def test_two_listener_signal_shutdown(bridge_home):
    ports = set()
    while len(ports) < 2:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            ports.add(sock.getsockname()[1])
    admin_port, remote_port = ports
    bridge_home.mkdir()
    (bridge_home / 'agents.toml').write_text(f'[hub]\nport={admin_port}\n[hub.remote]\nenabled=true\nport={remote_port}\n')
    code = '''
import keyring
class MemoryKeyring:
    agent_bridge_memory_only = True
    def get_password(self, *args): return None
    def set_password(self, *args): pass
keyring.get_keyring = MemoryKeyring
from agent_bridge.hub import serve
serve()
'''
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', code, env=dict(os.environ),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        async def started():
            assert process.stderr is not None
            listeners = 0
            while listeners < 2:
                line = await process.stderr.readline()
                assert line, 'server exited before binding both listeners'
                listeners += b'Uvicorn running on' in line

        await asyncio.wait_for(started(), 15)
        process.send_signal(signal.SIGTERM)
        await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 0
        assert not (bridge_home / 'hub.json').exists()
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
