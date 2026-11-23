from __future__ import annotations

import json
import os
import warnings
from pathlib import Path

import httpx2
import pytest

from agent_bridge import cli, secrets
from agent_bridge.adapters.cloud import CloudAdapter
from agent_bridge.config import AgentConfig
from agent_bridge.hub import create_app
from agent_bridge.hub_client import discover
from agent_bridge.models import Session, Task
from agent_bridge.persist import atomic_write_json
from agent_bridge.probes import probe_agent
from agent_bridge.registry import Registry
from agent_bridge.transcript import append_event, flush_pending


@pytest.mark.parametrize('model', [Task, Session])
def test_persisted_models_have_no_credential_fields(model):
    for name, field in model.model_fields.items():
        if name == 'callback_signed':
            assert field.annotation is bool
            continue
        assert not any(part in name.lower() for part in ('secret', 'token', 'key', 'password')), name


def test_callback_secret_observes_external_keyring_changes(memory_keyring, monkeypatch):
    key = secrets.callback_secret_key('owner')
    secrets.store_callback_secret('owner', 'a' * 32)
    assert secrets.load_callback_secret('owner') == 'a' * 32
    memory_keyring.set_password(secrets.SERVICE, key, 'b' * 32)
    assert secrets.load_callback_secret('owner') == 'b' * 32
    memory_keyring.delete_password(secrets.SERVICE, key)
    with pytest.raises(ValueError, match='not found in keyring'):
        secrets.load_callback_secret('owner')
    assert key not in secrets._memory
    secrets.store_callback_secret('owner', 'c' * 32)
    monkeypatch.setattr(secrets.keyring, 'get_keyring', lambda: object())
    with pytest.raises(ValueError, match='not found in keyring'):
        secrets.load_callback_secret('owner')
    assert key not in secrets._memory


def test_keyring_cloud_and_cli(memory_keyring, monkeypatch, capsys, caplog):
    value = 'unique-provider-credential-value'
    monkeypatch.setattr('getpass.getpass', lambda prompt: value)
    cli.main(['secrets', 'set', 'cursor'])
    assert memory_keyring.values[secrets.SERVICE, 'cloud:cursor'] == value
    assert secrets.cloud_key('cursor', 'CURSOR_API_KEY') == value
    cli.main(['secrets', 'list'])
    output = capsys.readouterr().out
    assert 'cursor: set' in output and value not in output
    cli.main(['secrets', 'delete', 'cursor'])
    assert secrets.get('cloud:cursor') is None
    monkeypatch.setenv('CURSOR_API_KEY', value)
    assert secrets.cloud_key('cursor', 'CURSOR_API_KEY') == value
    assert 'explicit environment fallback CURSOR_API_KEY' in caplog.text
    assert value not in caplog.text


def test_unavailable_and_plaintext_backend_refused(monkeypatch, caplog):
    class Plaintext:
        def set_password(self, *args):
            pytest.fail('unsafe backend must never be called')

    monkeypatch.setattr(secrets.keyring, 'get_keyring', Plaintext)
    assert not secrets.set_secret('example', 'memory-only-credential')
    assert secrets.get('example') == 'memory-only-credential'
    secrets._memory.clear()
    assert secrets.get('example') is None
    assert 'No plaintext fallback is written' in caplog.text


async def test_full_flow_no_plaintext_and_discovery(bridge_home, tmp_path, monkeypatch):
    token, callback, key = ('generated-hub-credential-value', 'callback-signing-value' * 2, 'provider-key-value')
    secrets.set_secret('cloud:cursor', key)
    registry = Registry.create(bridge_home)
    registry.config.agents['cursor'] = AgentConfig(name='cursor', protocol='cloud', cloud={'provider': 'cursor'})
    app = create_app(registry, token=token)
    async with app.router.lifespan_context(app):
        await registry.deliveries.stop()
        info = json.loads((bridge_home / 'hub.json').read_text())
        assert 'token' not in info and token not in json.dumps(info)
        original = httpx2.AsyncClient
        monkeypatch.setattr(httpx2, 'AsyncClient', lambda **kw: original(transport=httpx2.ASGITransport(app=app), **kw))
        assert (await discover(bridge_home)).token == token
        secrets.store_callback_secret('test', callback)
        task = await registry.dispatch_task('fake', f'{token} {callback} {key}', cwd=str(tmp_path), callback_url='https://8.8.8.8', callback_secret_name='test')
        await registry.wait_task(task['task_id'], timeout_sec=5)
        append_event(task['session_id'], 'raw', {'value': [token, callback, key]}, bridge_home)
        flush_pending(bridge_home)
        await registry.flush_state()
        for path in bridge_home.rglob('*'):
            if path.is_file():
                if os.name == 'nt' and path.name == 'hub.lock':
                    assert path.stat().st_size == 1
                    continue
                content = path.read_bytes()
                assert all(value.encode() not in content for value in (token, callback, key)), path
        assert not (bridge_home / 'callback-secrets').exists()


async def test_migration_and_unsigned_restart_refused(bridge_home, tmp_path, monkeypatch):
    bridge_home.mkdir()
    (bridge_home / 'hub.json').write_text(json.dumps({'url': 'http://old', 'token': 'old-hub-credential'}))
    directory = bridge_home / 'callback-secrets'
    directory.mkdir()
    (directory / 'old-task').write_text('old-callback-credential')
    secrets.migrate(bridge_home)
    assert not (bridge_home / 'hub.json').exists() and not directory.exists()
    assert secrets.get(secrets.callback_key(bridge_home, 'old-task')) == 'old-callback-credential'
    registry = Registry.create(bridge_home)
    await registry.start()
    await registry.deliveries.stop()
    try:
        secrets.store_callback_secret('test', 'lost-callback-credential' * 2)
        response = await registry.dispatch_task('fake', 'hello', cwd=str(tmp_path), callback_url='https://8.8.8.8', callback_secret_name='test')
        await registry.wait_task(response['task_id'], timeout_sec=5)
        task = registry.tasks[response['task_id']]
    finally:
        await registry.stop()
    monkeypatch.setattr(secrets.keyring, 'get_keyring', lambda: object())
    secrets._memory.clear()
    restored = Registry.create(bridge_home)
    await restored.start()
    await restored.deliveries.stop()
    try:
        task = restored.tasks[task.task_id]
        await restored.deliveries.callback(task)
        assert task.callback_delivery['status'] == 'failed'
        assert task.callback_delivery['error'] == 'secret_unavailable_after_restart'
    finally:
        await restored.stop()


def test_escaped_secret_redaction(tmp_path):
    value = secrets.remember('quoted-credential-"\\\n')
    path = tmp_path / 'state.json'
    atomic_write_json(path, {'value': value})
    assert json.loads(path.read_text()) == {'value': '[REDACTED]'}


def test_migration_uses_utf8_on_ascii_locale(bridge_home, monkeypatch):
    bridge_home.mkdir()
    legacy = bridge_home / 'callback-secrets'
    legacy.mkdir()
    value = '回调签名密钥-credential'
    (legacy / 'task_utf8').write_text(value, encoding='utf-8')
    read = Path.read_text

    def locale_read(path, encoding=None, **kwargs):
        return read(path, encoding=encoding or 'ascii', **kwargs)

    monkeypatch.setattr(Path, 'read_text', locale_read)
    secrets.migrate(bridge_home)
    assert secrets.get(secrets.callback_key(bridge_home, 'task_utf8')) == value
    assert not legacy.exists()


def test_migration_skips_unreadable_file_and_continues(bridge_home, monkeypatch):
    bridge_home.mkdir()
    directory = bridge_home / 'logs'
    directory.mkdir()
    (directory / 'bad.log').write_bytes(b'\xffinvalid UTF8')
    value = secrets.remember('credential-that-must-be-scrubbed')
    good = directory / 'good.log'
    good.write_text(value, encoding='utf-8')
    secrets.migrate(bridge_home)
    assert value not in good.read_text(encoding='utf-8')


def test_json_redaction_preserves_keys(tmp_path):
    secrets.remember('id')
    path = tmp_path / 'state.json'
    atomic_write_json(path, {'session_id': 'sess_good', 'data': {'session_id': 'id'}})
    secrets.migrate(tmp_path)
    assert json.loads(path.read_text()) == {
        'session_id': 'sess_good', 'data': {'session_id': '[REDACTED]'},
    }


def test_structured_secret_keys_are_redacted_without_collisions(tmp_path):
    token = 'abr_' + 'x' * 43
    credential = secrets.remember('long-provider-credential-value')
    payload = {'session_id': 'sess_good', 'remote': {'structured_output': {
        token: 'first', credential: 'second', '[REDACTED]': 'normal', '[REDACTED-1]': 'reserved',
    }}}
    path = tmp_path / 'state.json'
    atomic_write_json(path, payload)
    content = path.read_text()
    assert token not in content and credential not in content
    output = json.loads(content)
    assert output['session_id'] == 'sess_good'
    assert sorted(output['remote']['structured_output'].values()) == ['first', 'normal', 'reserved', 'second']
    assert secrets.redact({}) == {}


def test_short_secret_redaction_does_not_replace_substrings(tmp_path):
    secrets.remember('id')
    path = tmp_path / 'state.json'
    atomic_write_json(path, {'task_id': 'task_valid', 'value': 'id'})
    assert json.loads(path.read_text()) == {'task_id': 'task_valid', 'value': '[REDACTED]'}


async def test_wait_preview_redacts_in_memory_credentials(bridge_home, tmp_path):
    credential = secrets.remember('private-wait-result-credential')
    registry = Registry.create(bridge_home)
    await registry.start()
    try:
        task = await registry.dispatch_task('fake', credential, cwd=str(tmp_path))
        result = await registry.wait_task(task['task_id'], timeout_sec=5)
        assert credential not in json.dumps(result)
        assert '[REDACTED]' in result['result_text']
    finally:
        await registry.stop()


def test_no_echo_fallback(monkeypatch):
    def insecure(prompt):
        warnings.warn('no secure terminal', secrets.getpass.GetPassWarning, stacklevel=1)
        pytest.fail('getpass must not fall back to echoed input')

    monkeypatch.setattr(secrets.getpass, 'getpass', insecure)
    with pytest.raises(ValueError, match='refusing to echo'):
        secrets.prompt_secret('Key: ')


@pytest.mark.parametrize('provider', ['cursor', 'devin'])
async def test_cloud_keyring_preferred_and_probe(provider, tmp_path, monkeypatch):
    value = 'os-keyring-provider-credential'
    env_name = provider.upper() + '_API_KEY'
    monkeypatch.setenv(env_name, 'environment-must-not-be-selected')
    secrets.set_secret('cloud:' + provider, value)
    cfg = AgentConfig(name=provider, protocol='cloud', cloud={'provider': provider, 'org_id': 'org-example'})
    original = httpx2.AsyncClient

    def handler(request):
        assert request.headers['Authorization'] == 'Bearer ' + value
        return httpx2.Response(200, json={'echo': value})

    monkeypatch.setattr(httpx2, 'AsyncClient', lambda **kw: original(transport=httpx2.MockTransport(handler), **kw))
    assert await CloudAdapter(cfg, tmp_path)._request('GET', '/test') == {'echo': '[REDACTED]'}
    monkeypatch.delenv(env_name)
    assert (await probe_agent(cfg))['available']
