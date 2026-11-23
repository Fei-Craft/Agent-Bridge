from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from agent_bridge import cli
from agent_bridge.dashboard import COOKIE, HEADERS
from agent_bridge.hub import create_app
from agent_bridge.registry import Registry
from agent_bridge.remote import create_remote_app

ORIGIN = 'http://127.0.0.1:8765'
UI = {'X-Agent-Bridge-UI': '1', 'Origin': ORIGIN}
BEARER = {'Authorization': 'Bearer dashboard-admin-credential'}


@pytest.mark.skipif(shutil.which('node') is None, reason='Node is optional; required only for vanilla JS rendering tests')
def test_dashboard_rendering():
    result = subprocess.run(['node', '--test', str(Path(__file__).with_name('dashboard_ui.test.cjs'))],
        capture_output=True, text=True, timeout=20, encoding='utf-8')
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
async def dashboard(bridge_home):
    registry = Registry.create(bridge_home)
    registry.config.agents = {'fake': registry.config.agents['fake']}
    registry.config.quota.enabled = False
    app = create_app(registry, token='dashboard-admin-credential')
    async with app.router.lifespan_context(app), httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url=ORIGIN) as client:
        yield client, app, registry


async def login(client):
    response = await client.post('/v1/ui/login-code', headers=BEARER)
    assert response.status_code == 200
    url = response.json()['url']
    code = parse_qs(urlsplit(url).fragment)['code'][0]
    return code, await client.post('/ui/login', headers=UI, json={'code': code})


async def test_login_single_use_hashed_cookie_logout(dashboard):
    client, app, registry = dashboard
    assert (await client.get('/ui/data', headers=UI)).status_code == 401
    assert (await client.post('/v1/ui/login-code', headers=UI)).status_code == 401
    code, response = await login(client)
    assert response.status_code == 200
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'SameSite=strict' in cookie and 'Path=/' in cookie
    assert 'Max-Age' not in cookie and 'Expires' not in cookie
    raw = client.cookies.get(COOKIE)
    assert raw not in app.state.dashboard.sessions
    assert hashlib.sha256(raw.encode()).hexdigest() in app.state.dashboard.sessions
    assert code not in app.state.dashboard.codes
    assert not app.state.dashboard.codes
    assert (await client.post('/ui/login', headers=UI, json={'code': code})).status_code == 401
    assert (await client.get('/v1/sessions', headers=UI)).status_code == 200
    assert (await client.post('/v1/ui/login-code', headers=UI)).status_code == 401
    assert (await client.put('/v1/preferences', headers=UI, json={'mode': 'auto'})).status_code == 401
    assert (await client.get('/ui/data', headers=UI)).json()['tasks'] == []
    for path in registry.home.rglob('*'):
        if path.is_file():
            if os.name == 'nt' and path.name == 'hub.lock':
                assert path.stat().st_size == 1
                continue
            assert raw.encode() not in path.read_bytes()
            assert code.encode() not in path.read_bytes()
    response = await client.post('/ui/logout', headers=UI, json={})
    assert response.status_code == 200 and not app.state.dashboard.sessions
    assert (await client.get('/v1/sessions', headers=UI)).status_code == 401
    assert (await client.get('/v1/sessions', headers=BEARER)).status_code == 200


async def test_code_and_cookie_expiry(dashboard):
    client, app, _ = dashboard
    await login(client)
    response = await client.post('/v1/ui/login-code', headers=BEARER)
    code = parse_qs(urlsplit(response.json()['url']).fragment)['code'][0]
    app.state.dashboard.codes[hashlib.sha256(code.encode()).hexdigest()] = 0
    assert (await client.post('/ui/login', headers=UI, json={'code': code})).status_code == 401
    app.state.dashboard.sessions = {key: 0 for key in app.state.dashboard.sessions}
    assert (await client.get('/ui/data', headers=UI)).status_code == 401


@pytest.mark.parametrize('headers', [{}, {'X-Agent-Bridge-UI': '0'}, {**UI, 'Origin': 'https://evil.example'},
    {**UI, 'Origin': 'null'}, {**UI, 'Host': 'evil.example'}, {**UI, 'Sec-Fetch-Site': 'cross-site'}])
async def test_cookie_csrf_and_origin(dashboard, headers):
    client, _, _ = dashboard
    await login(client)
    assert (await client.get('/v1/sessions', headers=headers)).status_code == 401
    assert (await client.post('/v1/tasks', headers=headers, json={})).status_code == 401
    assert (await client.post('/ui/logout', headers=headers, json={})).status_code in {401, 403}


@pytest.mark.parametrize('body', [{}, [], {'code': None}, {'code': 'incorrect'}])
async def test_login_invalid_and_header_required(dashboard, body):
    client, _, _ = dashboard
    assert (await client.post('/ui/login', json=body)).status_code == 403
    assert (await client.post('/ui/login', headers=UI, json=body)).status_code in {400, 401}
    assert (await client.post('/ui/login', headers=UI, content='{')).status_code == 400
    assert (await client.post('/ui/login', headers=UI, content='x' * 2049)).status_code == 413


async def test_static_headers_and_isolation(dashboard):
    client, _, registry = dashboard
    for path, content in [('/ui', '本地控制台'), ('/ui/app.js', 'textContent'), ('/ui/style.css', 'color')]:
        response = await client.get(path)
        assert response.status_code == 200 and content in response.text
        assert 'dashboard-admin-credential' not in response.text
        for key, value in HEADERS.items():
            assert response.headers[key] == value
    script = (await client.get('/ui/app.js')).text
    assert 'innerHTML' not in script and 'localStorage' not in script
    assert "history.replaceState" in script
    assert (await client.get('/ui/app.js/../dashboard.py')).status_code == 404
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=create_remote_app(registry.remote)), base_url=ORIGIN) as remote:
        for path in ['/ui', '/ui/app.js', '/ui/data', '/v1/ui/login-code']:
            assert (await remote.get(path)).status_code == 404
    app = create_app(registry, url='http://0.0.0.0:8765', token='dashboard-admin-credential')
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url='http://0.0.0.0:8765') as public:
        assert (await public.get('/ui')).status_code == 404
        assert (await public.post('/v1/ui/login-code', headers=BEARER)).status_code == 400


async def test_ui_task_flow_and_redacted_audit(dashboard, tmp_path):
    client, _, registry = dashboard
    await login(client)
    response = await client.post('/v1/tasks', headers=UI, json={
        'agent': 'fake', 'message': '<img src=x onerror=alert(1)>', 'cwd': str(tmp_path)})
    assert response.json()['ok']
    tid, sid = response.json()['task_id'], response.json()['session_id']
    await registry._done[tid].wait()
    data = (await client.get('/ui/data', headers=UI)).json()
    assert data['tasks'][0]['status'] == 'completed'
    assert (await client.get('/v1/tasks/' + tid + '/result', headers=UI)).json()['result_text']
    assert (await client.get('/v1/sessions/' + sid + '/transcript', headers=UI)).json()['events']
    assert (await client.post('/v1/tasks/' + tid + '/cancel', headers=UI, json={})).status_code == 200
    token = 'abr_' + 'a' * 43
    (registry.home / 'remote-audit.jsonl').write_text('{"cf_ray":"' + token + '"}\n')
    data = (await client.get('/ui/data', headers=UI)).json()
    assert data['audit'][0]['cf_ray'] == '[REDACTED]'
    assert (await client.get('/v1/agents', headers=UI)).json()['agents'][0]['protocol'] == 'fake'


async def test_dashboard_workspace_paths_are_canonical(dashboard, tmp_path, monkeypatch):
    client, _, registry = dashboard
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    project = tmp_path / 'project'
    project.mkdir()
    registry.config.hub.remote.remote_workspaces = {'project': '~/project/../project'}
    await login(client)
    workspaces = (await client.get('/ui/data', headers=UI)).json()['remote']['workspaces']
    assert workspaces == {'project': str(project.resolve())}
    response = await client.post('/v1/tasks', headers=UI, json={
        'agent': 'fake', 'message': 'operator request', 'cwd': workspaces['project'], 'user_requested': True})
    assert response.status_code == 200
    assert registry.tasks[response.json()['task_id']].cwd == str(project.resolve())


@pytest.mark.parametrize('route', ['/v1/tasks', '/v1/agents/fake/invoke'])
async def test_explicit_dashboard_dispatch_in_manual_mode(dashboard, tmp_path, route):
    client, _, registry = dashboard
    registry.config.coordinator.mode = 'manual'
    await login(client)
    body = {'message': 'operator request', 'cwd': str(tmp_path)}
    if route == '/v1/tasks':
        body['agent'] = 'fake'
    assert (await client.post(route, headers=UI, json=body)).status_code == 400
    response = await client.post(route, headers=UI, json={**body, 'user_requested': True})
    assert response.status_code == 200 and response.json()['ok']
    assert 'user_requested: true' in (await client.get('/ui/app.js')).text


def test_cli_open_and_no_open(monkeypatch, capsys):
    client = AsyncMock()
    client.call.return_value = {'ok': True, 'url': ORIGIN + '/ui#code=single-use'}
    monkeypatch.setattr('agent_bridge.hub_client.discover', AsyncMock(return_value=client))
    opened = []
    monkeypatch.setattr('webbrowser.open', lambda url: opened.append(url) or True)
    cli.main(['ui'])
    assert opened == [ORIGIN + '/ui#code=single-use']
    assert not capsys.readouterr().out
    cli.main(['ui', '--no-open'])
    assert capsys.readouterr().out.strip() == ORIGIN + '/ui#code=single-use'
    monkeypatch.setattr('agent_bridge.hub_client.discover', AsyncMock(return_value=None))
    with pytest.raises(RuntimeError, match='no authenticated'):
        cli.main(['ui'])
