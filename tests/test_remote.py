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
from starlette.requests import Request

from agent_bridge import cli
from agent_bridge.adapters.fake import FakeAdapter
from agent_bridge.baselines import Baseline
from agent_bridge.config import AgentConfig, AppConfig, HubConfig, RemoteConfig, load_config
from agent_bridge.desktop_ipc import DesktopIPC
from agent_bridge.hub import create_app
from agent_bridge.models import ProcState, Session, Task, TaskStatus
from agent_bridge.registry import Registry
from agent_bridge.remote import Approval, create_remote_app
from agent_bridge.tunnel import run_tunnel
from agent_bridge.worker_env import build_worker_env


@pytest.fixture
async def gateway(bridge_home, tmp_path):
    config = load_config(bridge_home)
    config.agents['dsh'].env['DSH_PERMISSION_MODE'] = 'workspace-write'
    config.hub.remote = RemoteConfig(enabled=True, public_url='http://127.0.0.1:8766',
        require_approval=False,
        remote_callable_agents=['fake', 'cloud', 'dsh'], remote_workspaces={'project': str(tmp_path)})
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


async def test_remote_dsh_starts_without_local_default_baseline(gateway, monkeypatch):
    client, registry, scope, _ = gateway
    cwd = scope.workspaces['project']
    registry.baselines.add(Baseline(baseline_id='private', name='Private', agent='dsh',
        backend='desktop', cwd=cwd, native_session_id='private-native', source_session_id='private-source'))
    registry.baselines.set_default(cwd, 'dsh', 'private')
    monkeypatch.setattr(DesktopIPC, 'connected', property(lambda self: True))
    monkeypatch.setattr(registry, '_adapter_for', lambda session: FakeAdapter(registry.config.get('fake'), registry.home))

    result = (await client.post('/v1/agents/dsh/invoke', json=payload(agent='dsh'))).json()
    assert result['ok']
    assert result['baseline_id'] is None
    assert registry.sessions[result['session_id']].fork_source_id is None
    result = await client.post('/v1/tasks', json=payload(agent='dsh', baseline='private'))
    assert result.status_code == 400


@pytest.mark.parametrize('turn_timeout', [0, 30, 86400])
async def test_remote_token_default_ttl_independent_of_turn_timeout(gateway, turn_timeout):
    _, registry, _, _ = gateway
    assert RemoteConfig().token_ttl_sec == 3600
    registry.config.get('cloud').turn_timeout_sec = turn_timeout
    before = time.time()
    scope, _ = registry.remote.issue(registry.tasks['task_origin'])
    assert before + 3600 <= scope.expires <= time.time() + 3600


async def test_remote_token_explicit_ttl(gateway):
    _, registry, _, _ = gateway
    registry.remote.config.token_ttl_sec = 90
    before = time.time()
    scope, _ = registry.remote.issue(registry.tasks['task_origin'])
    assert before + 90 <= scope.expires <= time.time() + 90


@pytest.mark.parametrize('ttl', [None, 0, -1])
def test_remote_token_ttl_must_be_positive(ttl):
    with pytest.raises(ValidationError):
        RemoteConfig(token_ttl_sec=ttl)


@pytest.mark.parametrize('flag', ['--dangerously-skip-permissions', '--auto', '--yolo',
    '--full-auto', '--dangerously-bypass-approvals-and-sandbox', '--force', '--auto=true'])
@pytest.mark.parametrize('location', ['command', 'fallback_commands', 'cli'])
def test_remote_unsafe_permissions_require_explicit_opt_in(flag, location, caplog):
    spec = {'name': 'unsafe', 'protocol': 'cli', 'command': ['worker']}
    spec[location] = {'extra_args': [flag]} if location == 'cli' else ([['worker', flag]] if location == 'fallback_commands' else ['worker', flag])
    agent = AgentConfig.model_validate(spec)
    remote = HubConfig(remote={'enabled': True, 'remote_callable_agents': ['unsafe']})
    with pytest.raises(ValueError, match='remote_allow_unsafe_permissions'):
        AppConfig(agents={'unsafe': agent}, hub=remote)
    agent.remote_allow_unsafe_permissions = True
    AppConfig(agents={'unsafe': agent}, hub=remote)
    assert 'unsafe explicitly allows unsafe permissions' in caplog.text


@pytest.mark.parametrize(('value', 'unsafe'), [
    ('danger-full-access', True), (' danger-full-access ', True), ('\tDaNgEr-FuLl-AcCeSs\n', True),
    (' workspace-write ', False), ('', False),
])
def test_dsh_permissions_normalized_like_worker_launch(value, unsafe):
    agent = AgentConfig(name='dsh', protocol='acp', command=['worker'], env={'DSH_PERMISSION_MODE': value})
    launched = build_worker_env(agent.env, base={}, log_fill=False)
    assert agent.unsafe_remote_permissions() is unsafe
    assert launched.get('DSH_PERMISSION_MODE', '') == value.strip().casefold()
    hub = HubConfig(remote={'enabled': True, 'remote_callable_agents': ['dsh']})
    if unsafe:
        with pytest.raises(ValueError, match='skips permissions'):
            AppConfig(agents={'dsh': agent}, hub=hub)
        agent.remote_allow_unsafe_permissions = True
    AppConfig(agents={'dsh': agent}, hub=hub)


def test_remote_defaults_and_preset_validation(bridge_home):
    assert RemoteConfig().require_approval is True
    config = load_config(bridge_home)
    config.hub.remote = RemoteConfig(enabled=True)
    config.validate_remote_agents()
    for name in ('opencode', 'claude', 'dsh'):
        config.hub.remote.remote_callable_agents = [name]
        with pytest.raises(ValueError, match='skips permissions'):
            config.validate_remote_agents()
        config.agents[name].remote_allow_unsafe_permissions = True
        config.validate_remote_agents()
    bridge_home.mkdir(exist_ok=True)
    (bridge_home / 'agents.toml').write_text('[hub.remote]\nenabled=true\nremote_callable_agents=["claude"]\n', encoding='utf-8')
    with pytest.raises(ValueError, match='skips permissions'):
        load_config(bridge_home)


async def test_remote_approval_is_required_by_default(gateway):
    client, registry, _, _ = gateway
    registry.remote.config = RemoteConfig(enabled=True)
    pending = asyncio.create_task(client.post('/v1/tasks', json=payload()))
    try:
        for _ in range(100):
            if registry.remote.approvals:
                break
            await asyncio.sleep(0.01)
        assert registry.remote.approvals and not pending.done()
        assert not any(session.remote_origin for session in registry.sessions.values())
        approval_id = next(iter(registry.remote.approvals))
        registry.remote.decide(approval_id, False)
        assert (await pending).status_code == 403
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


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


@pytest.mark.parametrize('field', ['model', 'effort'])
@pytest.mark.parametrize('value', ['', '--dir=/', 'not-allowlisted', 'x' * 101, 'x\n', 'x y', 'x"'])
async def test_remote_selection_rejected_before_dispatch(gateway, field, value):
    client, registry, scope, _ = gateway
    response = await client.post('/v1/tasks', json=payload(**{field: value}))
    assert response.status_code == 400
    assert not scope.owned_tasks and scope.calls == 0
    assert len(registry.tasks) == 1


async def test_remote_selection_allowlists(gateway):
    client, registry, _, _ = gateway
    agent = registry.config.get('fake')
    agent.remote_allowed_models = ['provider/model:v1@latest']
    agent.remote_allowed_efforts = ['high']
    response = await client.post('/v1/tasks', json=payload(model='provider/model:v1@latest', effort='high'))
    assert response.status_code == 200
    task = registry.tasks[response.json()['task_id']]
    assert task.model == 'provider/model:v1@latest' and task.effort == 'high'


@pytest.mark.parametrize('field', ['remote_allowed_models', 'remote_allowed_efforts'])
def test_remote_selection_allowlist_config_validation(field):
    with pytest.raises(ValidationError):
        AgentConfig(name='bad', protocol='fake', command=['fake'], **{field: ['--dir=/']})


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
    if os.name != 'nt':
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
    assert (await client.post('/v1/tasks', json=payload(), headers={'Cf-Access-Jwt-Assertion': 'present'})).status_code == 200
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


async def test_approval_expiry(gateway, monkeypatch):
    client, registry, scope, _ = gateway
    registry.remote.config.require_approval = True
    scope.expires = time.time() + 3600
    approve = registry.remote.approve

    async def expire_approval(*args):
        scope.expires = time.time() - 1
        await approve(*args)

    monkeypatch.setattr(registry.remote, 'approve', expire_approval)
    response = await client.post('/v1/tasks', json=payload())
    assert response.status_code == 403
    assert response.json()['error'] == 'remote approval expired'
    assert not registry.remote.approvals


async def test_full_approval_message_is_visible(gateway):
    _, registry, scope, _ = gateway
    message = 'safe-looking prefix ' * 100 + 'DESTRUCTIVE SUFFIX MUST BE VISIBLE'
    registry.remote.approvals['approval_long'] = Approval('approval_long', scope, 'fake', 'project', message)
    assert registry.remote.list_approvals()['approvals'][0]['message'] == message


async def test_unauthenticated_audit_is_rate_limited(gateway):
    client, registry, _, _ = gateway
    for _ in range(50):
        assert (await client.get('/unknown')).status_code == 404
    assert len((registry.home / 'remote-audit.jsonl').read_text().splitlines()) <= 20


async def test_audit_storage_is_bounded(gateway, monkeypatch):
    _, registry, scope, _ = gateway
    monkeypatch.setattr('agent_bridge.remote.AUDIT_MAX_BYTES', 1024, raising=False)
    request = Request({'type': 'http', 'headers': [], 'client': ('127.0.0.1', 1)})
    for _ in range(50):
        registry.remote.audit(request, 'dispatch', scope, 'fake', 'project', 'accepted')
    files = list(registry.home.glob('remote-audit.jsonl*'))
    assert len(files) <= 2
    assert all(path.stat().st_size <= 1024 for path in files)
    if os.name != 'nt':
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in files)


async def test_shallow_caller_cannot_reset_saved_session_depth(gateway, tmp_path):
    _, registry, scope, _ = gateway
    caller = registry.sessions[scope.session_id]
    first = await registry.dispatch_task('fake', 'deep', cwd=str(tmp_path))
    await registry.wait_task(first['task_id'], 5)
    session = registry.sessions[first['session_id']]
    session.call_depth = 3
    caller.call_depth = 0
    resumed = await registry.dispatch_task(
        'fake', 'resume', cwd=str(tmp_path), session_id=session.session_id,
        caller_session_id=caller.session_id,
    )
    assert registry.tasks[resumed['task_id']].call_depth == 3
    assert session.call_depth == 3


async def test_shutdown_releases_pending_approvals(gateway, monkeypatch):
    client, registry, _, _ = gateway
    registry.config.hub.remote.require_approval = True
    ready = asyncio.Event()
    original = registry.remote.approve

    async def approve(*args):
        ready.set()
        await original(*args)

    monkeypatch.setattr(registry.remote, 'approve', approve)
    pending = asyncio.create_task(client.post('/v1/tasks', json=payload()))
    try:
        await asyncio.wait_for(ready.wait(), 2)
        assert registry.remote.approvals
        await registry.stop()
        response = await asyncio.wait_for(pending, 0.2)
        assert response.status_code == 403
        assert not registry.remote.approvals
        assert not any(task.agent == 'fake' for task in registry.tasks.values())
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


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
