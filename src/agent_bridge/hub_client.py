from __future__ import annotations

import ipaddress
import json
import os
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2
import psutil

from agent_bridge.api import _error, wait_for_result
from agent_bridge.paths import bridge_home
from agent_bridge.secrets import get, hub_key, remember

# tool: (HTTP method, path, path argument)
ROUTES = {
    'create_ui_login': ('POST', '/v1/ui/login-code', None),
    'list_approvals': ('GET', '/v1/approvals', None),
    'approve_approval': ('POST', '/v1/approvals/{approval_id}/approve', 'approval_id'),
    'deny_approval': ('POST', '/v1/approvals/{approval_id}/deny', 'approval_id'),
    'set_remote_public_url': ('PUT', '/v1/remote/public-url', None),
    'get_remote_status': ('GET', '/v1/remote', None),
    'list_agents': ('GET', '/v1/agents', None),
    'set_preferences': ('PUT', '/v1/preferences', None),
    'dispatch_task': ('POST', '/v1/tasks', None),
    'check_task': ('GET', '/v1/tasks/{task_id}', 'task_id'),
    'wait_task': ('GET', '/v1/tasks/{task_id}/wait', 'task_id'),
    'get_result': ('GET', '/v1/tasks/{task_id}/result', 'task_id'),
    'cancel_task': ('POST', '/v1/tasks/{task_id}/cancel', 'task_id'),
    'list_sessions': ('GET', '/v1/sessions', None),
    'get_transcript': ('GET', '/v1/sessions/{session_id}/transcript', 'session_id'),
    'end_session': ('DELETE', '/v1/sessions/{session_id}', 'session_id'),
    'invoke': ('POST', '/v1/agents/{agent}/invoke', 'agent'),
}


class HubClient:
    def __init__(self, url: str, token: str = '') -> None:
        self.url = url.rstrip('/')
        self.token = remember(token)
        self.host_manual_mode = False

    async def call(self, tool: str, **kwargs: Any) -> dict[str, Any]:
        try:
            if self.host_manual_mode and tool in {'dispatch_task', 'invoke'} and not kwargs.get('user_requested'):
                raise RuntimeError('coordinator mode is manual: retry with user_requested=true only when the user explicitly requested a worker')
            return await self._call(tool, **kwargs)
        except Exception as exc:
            return _error(exc)

    async def _call(self, tool: str, **kwargs: Any) -> dict[str, Any]:
        if 'callback_secret' in kwargs:
            raise ValueError('raw callback_secret is not accepted; use callback_secret_name from the keyring')
        if tool == 'invoke':
            timeout = kwargs.pop('timeout_sec', None)
            if timeout is not None and timeout < 0:
                raise ValueError('timeout_sec must be non-negative')
            dispatched = await self.call('dispatch_task', **kwargs)
            if not dispatched['ok']:
                return dispatched
            return await wait_for_result(self, dispatched['task_id'], timeout)
        method, path, key = ROUTES[tool]
        if key:
            path = path.format(**{key: kwargs.pop(key)})
        headers = {'Authorization': f'Bearer {self.token}'}
        async with httpx2.AsyncClient(trust_env=False, timeout=None) as client:
            options: dict[str, Any] = {'params': kwargs} if method == 'GET' else {'json': kwargs}
            response = await client.request(method, self.url + path, headers=headers,
                                            **options)
            result: dict[str, Any] = response.json()
        if tool == 'list_agents':
            result.update(backend='hub', hub_url=self.url)
        if self.host_manual_mode and 'coordinator' in result:
            result['coordinator']['hub_mode'] = result['coordinator']['mode']
            result['coordinator']['mode'] = 'manual'
        return result

    def __getattr__(self, name: str) -> Any:
        async def forward(**kwargs: Any) -> dict[str, Any]:
            return await self.call(name, **kwargs)
        if name not in ROUTES:
            raise AttributeError(name)
        return forward


async def discover(home: Path | None = None) -> HubClient | None:
    if os.environ.get('AGENT_BRIDGE_HUB', '').lower() == 'off':
        return None
    url = os.environ.get('AGENT_BRIDGE_HUB_URL')
    token = os.environ.get('AGENT_BRIDGE_HUB_TOKEN', '')
    try:
        if not url:
            info = json.loads(((home or bridge_home()) / 'hub.json').read_text())
            if not psutil.pid_exists(int(info['pid'])):
                return None
            url = info['url']
        parsed = urlsplit(str(url))
        loopback = parsed.hostname == 'localhost'
        with suppress(ValueError):
            loopback = loopback or ipaddress.ip_address(parsed.hostname or '').is_loopback
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.scheme not in {'http', 'https'} or (parsed.scheme == 'http' and not loopback)):
            return None
        _ = parsed.port
        token = token or get(hub_key(home or bridge_home())) or ''
        async with httpx2.AsyncClient(trust_env=False, timeout=2) as client:
            response = await client.get(str(url).rstrip('/') + '/v1/health', headers={'Authorization': f'Bearer {token}'})
            if response.status_code != 200 or not response.json().get('ok'):
                return None
        return HubClient(str(url), token)
    except (OSError, ValueError, KeyError, httpx2.HTTPError):
        return None
