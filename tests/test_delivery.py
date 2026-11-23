from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import types
from unittest.mock import AsyncMock

import httpx2
import pytest

from agent_bridge import cli, server
from agent_bridge.api import BridgeService, wait_for_result
from agent_bridge.hub import create_app
from agent_bridge.hub_client import HubClient
from agent_bridge.models import TaskStatus
from agent_bridge.registry import Registry
from agent_bridge.secrets import callback_key, callback_secret_key, get, load_callback_secret, store_callback_secret


@pytest.fixture
async def registry(bridge_home):
    registry = Registry.create(bridge_home)
    registry.config.coordinator.mode = 'auto'
    await registry.start()
    await registry.deliveries.stop()
    yield registry
    await registry.stop()


async def dispatch(registry, tmp_path, **kwargs):
    result = await registry.dispatch_task('fake', kwargs.pop('message', 'work'), cwd=str(tmp_path), **kwargs)
    await registry.wait_task(result['task_id'], timeout_sec=5)
    return registry.tasks[result['task_id']]


async def test_invoke_unbounded_and_explicit_timeout(registry, tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_BRIDGE_FAKE_DELAY', '0.1')
    service = BridgeService(registry)
    bounded = await service.invoke('fake', 'bounded', str(tmp_path), timeout_sec=0.001)
    assert bounded['status'] in {'queued', 'running'}
    assert bounded['task_id']
    result = await service.invoke('fake', 'unbounded', str(tmp_path))
    assert result['status'] == 'completed'
    assert result['result_text'].endswith('unbounded')
    invalid = await service.invoke('fake', 'invalid', str(tmp_path), timeout_sec=-1)
    assert not invalid['ok']


async def test_wait_multiple_old_caps_and_progress():
    service = types.SimpleNamespace(
        wait_task=AsyncMock(side_effect=[{'ok': True, 'status': 'running'}] * 20 + [{'ok': True, 'status': 'completed'}]),
        get_result=AsyncMock(return_value={'ok': True, 'status': 'completed'}))
    progress = AsyncMock()
    result = await wait_for_result(service, 'task-id', progress=progress)
    assert result['status'] == 'completed'
    assert service.wait_task.await_count == 21  # 315 seconds of bounded waits, beyond the old cap.
    assert progress.await_count == 20
    assert all(call.kwargs['timeout_sec'] == 15 for call in service.wait_task.call_args_list)


async def test_mcp_progress(registry, tmp_path, monkeypatch):
    ctx = types.SimpleNamespace(request_context=types.SimpleNamespace(lifespan_context=registry), report_progress=AsyncMock())
    service = BridgeService(registry)
    wait = service.wait_task
    calls = 0

    async def waiting(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {'ok': True, 'status': 'running'}
        return await wait(**kwargs)

    monkeypatch.setattr(service, 'wait_task', waiting)
    monkeypatch.setattr(server, '_service', lambda _: service)
    result = await server.invoke_agent(ctx, 'fake', 'hello', str(tmp_path))
    assert result['status'] == 'completed'
    assert ctx.report_progress.await_count >= 2


async def test_idle_notify_order_and_dedupe(registry, tmp_path):
    caller = await dispatch(registry, tmp_path)
    sid = caller.session_id
    children = [await dispatch(registry, tmp_path, message=f'child {n}', notify_caller=True, caller_session_id=sid) for n in range(2)]
    for child in children:
        await registry.deliveries.tick()
        receipt = registry.sessions[sid].notification_receipts[child.task_id]
        await registry.wait_task(receipt, timeout_sec=5)
        delivered = registry.tasks[receipt]
        assert delivered.notification_source_task_id == child.task_id
        assert child.task_id in delivered.message
        assert 'Fetch more:' in delivered.message
        assert not delivered.notify_caller and delivered.callback_url is None
        assert child.caller_delivery['status'] == 'delivered'
    count = len(registry.tasks)
    children[0].caller_delivery = {'status': 'pending'}  # Lost acknowledgment, persisted receipt survives.
    await registry.deliveries.tick()
    assert len(registry.tasks) == count
    assert children[0].caller_delivery['status'] == 'delivered'


async def test_busy_caller_queues(registry, tmp_path, monkeypatch):
    caller = await dispatch(registry, tmp_path)
    child = await dispatch(registry, tmp_path, notify_caller=True, caller_session_id=caller.session_id)
    monkeypatch.setenv('AGENT_BRIDGE_FAKE_DELAY', '0.2')
    busy = await registry.dispatch_task('fake', 'busy', cwd=str(tmp_path), session_id=caller.session_id)
    await registry.deliveries.tick()
    assert child.caller_delivery['status'] == 'pending'
    await registry.wait_task(busy['task_id'], timeout_sec=5)
    await registry.deliveries.tick()
    assert child.caller_delivery['status'] == 'delivered'


async def test_dispatch_order_waits_for_earlier_callee(registry, tmp_path):
    caller = await dispatch(registry, tmp_path)
    first = await dispatch(registry, tmp_path, notify_caller=True, caller_session_id=caller.session_id)
    second = await dispatch(registry, tmp_path, notify_caller=True, caller_session_id=caller.session_id)
    first.status = TaskStatus.running
    await registry.deliveries.tick()
    assert second.caller_delivery['status'] == 'pending'
    first.status = TaskStatus.completed
    await registry.deliveries.tick()
    assert first.caller_delivery['status'] == 'delivered'
    assert second.caller_delivery['status'] == 'pending'


async def test_hub_client_invoke_bounded_wait_loop(monkeypatch):
    client = HubClient('http://hub')
    original = client._call
    waits = 0

    async def call(tool, **kwargs):
        nonlocal waits
        if tool == 'dispatch_task':
            return {'ok': True, 'task_id': 'task-long'}
        if tool == 'wait_task':
            waits += 1
            assert kwargs['timeout_sec'] == 15
            return {'ok': True, 'status': 'running' if waits < 20 else 'completed'}
        return {'ok': True, 'status': 'completed', 'result_text': 'long result'}

    monkeypatch.setattr(client, '_call', call)
    assert (await original('invoke', agent='fake', message='hello'))['result_text'] == 'long result'
    assert waits == 20


async def test_no_caller_invalid_callback_and_no_loops(registry, tmp_path):
    with pytest.raises(ValueError, match='caller session'):
        await dispatch(registry, tmp_path, notify_caller=True)
    for url in ('file:///etc/passwd', 'ftp://example.org', 'https://user:password@example.org'):
        with pytest.raises(ValueError, match='HTTP'):
            await dispatch(registry, tmp_path, callback_url=url)
    with pytest.raises(ValueError, match='requires callback_url'):
        store_callback_secret('test', 's' * 32)
        await dispatch(registry, tmp_path, callback_secret_name='test')
    caller = await dispatch(registry, tmp_path)
    delivery = await registry.dispatch_task('fake', 'result', cwd=str(tmp_path), session_id=caller.session_id,
        notification_source_task_id='source')
    child = await registry.dispatch_task('fake', 'no loop', cwd=str(tmp_path), notify_caller=True,
        caller_session_id=caller.session_id, callback_url='http://listener')
    task = registry.tasks[child['task_id']]
    assert not task.notify_caller and task.callback_url is None
    await registry.wait_task(delivery['task_id'], timeout_sec=5)


@pytest.mark.parametrize('secret', ['', 'short', 'x' * 31])
async def test_callback_rejects_short_signing_secrets(registry, tmp_path, secret):
    with pytest.raises(ValueError, match='at least 32 characters'):
        store_callback_secret('test', secret)
        await dispatch(registry, tmp_path, callback_url='http://listener', callback_secret_name='test')
    assert not registry.tasks


async def test_callback_accepts_minimum_signing_secret(registry, tmp_path):
    store_callback_secret('test', 'x' * 32)
    task = await dispatch(registry, tmp_path, callback_url='http://listener', callback_secret_name='test')
    assert get(callback_key(registry.home, task.task_id)) == 'x' * 32


async def test_missing_callback_secret_reference_rejected(registry, tmp_path):
    with pytest.raises(ValueError, match='not found'):
        await dispatch(registry, tmp_path, callback_url='http://listener', callback_secret_name='missing')
    assert not registry.tasks


async def test_raw_callback_secret_rejected_on_public_surfaces(registry, tmp_path):
    app = create_app(registry, token='test')
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url='http://hub',
            headers={'Authorization': 'Bearer test'}) as client:
        response = await client.post('/v1/tasks', json={'agent': 'fake', 'message': 'work', 'cwd': str(tmp_path),
            'callback_url': 'http://listener', 'callback_secret': 'x' * 32})
        assert response.status_code == 400 and 'use callback_secret_name' in response.json()['error']
    result = await HubClient('http://hub').call('dispatch_task', callback_secret='x' * 32)
    assert not result['ok'] and 'use callback_secret_name' in result['error']
    result = await server.dispatch_task(None, 'fake', 'work', callback_secret='x' * 32)
    assert not result['ok'] and 'use callback_secret_name' in result['error']
    assert not registry.tasks


def test_callback_secret_cli_and_ephemeral_ask(monkeypatch, capsys):
    monkeypatch.setattr('agent_bridge.secrets.prompt_secret', lambda _: 's' * 32)
    cli.main(['callback-secret', 'set', 'receiver'])
    cli.main(['callback-secret', 'list'])
    output = capsys.readouterr().out
    assert 'receiver' in output and 's' * 32 not in output
    captured = []

    async def call(tool, **kwargs):
        assert 'callback_secret' not in kwargs
        name = kwargs['callback_secret_name']
        captured.append(name)
        assert load_callback_secret(name) == 's' * 32
        return {'ok': True, 'task_id': 'task-test'}

    monkeypatch.setattr('agent_bridge.hub_client.discover', AsyncMock(return_value=types.SimpleNamespace(call=call)))
    cli.main(['ask', 'fake', 'work', '--async', '--callback-url', 'http://listener', '--callback-secret'])
    assert get(callback_secret_key(captured[0])) is None
    cli.main(['callback-secret', 'delete', 'receiver'])
    assert get(callback_secret_key('receiver')) is None


@pytest.mark.parametrize('failures', [0, 2, 4])
async def test_callback_signature_retries_and_failure(registry, tmp_path, monkeypatch, failures, caplog):
    requests = []
    real_client = httpx2.AsyncClient

    def handler(request):
        requests.append(request)
        assert request.headers['X-Agent-Bridge-Signature'] == 'sha256=' + hmac.new(b'hidden-callback-secret' * 2, request.content, hashlib.sha256).hexdigest()
        payload = json.loads(request.content)
        assert payload['status'] == 'completed'
        assert payload['ok']
        return httpx2.Response(503 if len(requests) <= failures else 200)

    monkeypatch.setattr('agent_bridge.delivery.httpx2.AsyncClient', lambda **kwargs: real_client(transport=httpx2.MockTransport(handler), **kwargs))
    store_callback_secret('test', 'hidden-callback-secret' * 2)
    task = await dispatch(registry, tmp_path, callback_url='http://listener/result', callback_secret_name='test')
    assert get(callback_key(registry.home, task.task_id)) is not None
    assert not (registry.home / 'callback-secrets').exists()
    for _ in range(4):
        await registry.deliveries.callback(task)
        if task.callback_delivery['status'] == 'delivered':
            break
    assert len(requests) == min(4, failures + 1)
    assert task.callback_delivery['status'] == ('failed' if failures == 4 else 'delivered')
    assert get(callback_key(registry.home, task.task_id)) is None
    result = await registry.get_result(task.task_id)
    assert result['callback_delivery'] == task.callback_delivery
    assert 'hidden-callback-secret' not in json.dumps(result) + (registry.home / 'state.json').read_text() + caplog.text


async def test_restart_pending_and_bridge_restarted(registry, tmp_path):
    caller = await dispatch(registry, tmp_path)
    child = await dispatch(registry, tmp_path, notify_caller=True, caller_session_id=caller.session_id)
    child.status = TaskStatus.running
    registry.save()
    await registry.flush_state()
    restored = Registry.create(registry.home)
    await restored.start()
    try:
        await restored.deliveries.stop()
        await restored.deliveries.tick()
        task = restored.tasks[child.task_id]
        assert task.error == 'bridge_restarted'
        assert task.caller_delivery['status'] == 'delivered'
        receipt = restored.sessions[caller.session_id].notification_receipts[child.task_id]
        assert 'bridge_restarted' in restored.tasks[receipt].message
    finally:
        await restored.stop()


async def test_restart_signed_callback(registry, tmp_path, monkeypatch):
    store_callback_secret('test', 'restart-secret' * 3)
    task = await dispatch(registry, tmp_path, callback_url='http://listener', callback_secret_name='test')
    registry.save()
    await registry.flush_state()
    real_client = httpx2.AsyncClient
    received = []

    def handler(request):
        received.append(request)
        assert request.headers['X-Agent-Bridge-Signature'] == 'sha256=' + hmac.new(b'restart-secret' * 3, request.content, hashlib.sha256).hexdigest()
        return httpx2.Response(200)

    monkeypatch.setattr('agent_bridge.delivery.httpx2.AsyncClient', lambda **kwargs: real_client(transport=httpx2.MockTransport(handler), **kwargs))
    restored = Registry.create(registry.home)
    await restored.start()
    try:
        await restored.deliveries.stop()
        await restored.deliveries.callback(restored.tasks[task.task_id])
        assert restored.tasks[task.task_id].callback_delivery['status'] == 'delivered'
        assert len(received) == 1
    finally:
        await restored.stop()


async def test_unsigned_empty_callback_and_backoff(registry, tmp_path, monkeypatch):
    task = await dispatch(registry, tmp_path, callback_url='http://listener')
    real_client = httpx2.AsyncClient
    requests = []

    def handler(request):
        requests.append(request)
        assert 'X-Agent-Bridge-Signature' not in request.headers
        return httpx2.Response(503)

    monkeypatch.setattr('agent_bridge.delivery.httpx2.AsyncClient', lambda **kwargs: real_client(transport=httpx2.MockTransport(handler), **kwargs))
    await registry.deliveries.callback(task)
    assert task.callback_delivery['attempts'] == 1
    assert task.callback_delivery['status'] == 'pending'
    assert task.callback_delivery['retry_at'] > 0
    task.callback_delivery['retry_at'] = float('inf')
    await registry.deliveries.tick()
    assert not registry.deliveries._callbacks
    assert len(requests) == 1


async def test_rest_owner_notify_rejected_and_streaming_invoke(registry, tmp_path):
    app = create_app(registry, token='test')
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url='http://hub',
        headers={'Authorization': 'Bearer test'}) as client:
        result = (await client.post('/v1/agents/fake/invoke', json={'message': 'hello', 'cwd': str(tmp_path)}))
        assert result.json()['status'] == 'completed'
        assert result.headers['X-Agent-Bridge-Task-Id']
        sid = result.json()['session_id']
        child = (await client.post('/v1/tasks', json={'agent': 'fake', 'message': 'notify', 'cwd': str(tmp_path), 'notify_caller': True},
            headers={'X-Agent-Bridge-Caller': sid})).json()
        assert not child['ok']
        assert 'notify_caller requires a hub worker caller session' in child['error']


def test_cli_async_defaults_and_interrupt(monkeypatch, capsys):
    client = types.SimpleNamespace(call=AsyncMock(return_value={'ok': True, 'task_id': 'task-known'}))
    monkeypatch.setattr('agent_bridge.hub_client.discover', AsyncMock(return_value=client))
    monkeypatch.setenv('AGENT_BRIDGE_HUB_TOKEN', 'abw_' + 'x' * 43)
    cli.main(['ask', 'fake', 'hello', '--async'])
    assert 'task-known' in capsys.readouterr().out
    assert client.call.call_args.kwargs['notify_caller'] is True
    client.wait_task = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        cli.main(['ask', 'fake', 'hello'])
    assert 'Interrupted; task continues: task-known' in capsys.readouterr().err
