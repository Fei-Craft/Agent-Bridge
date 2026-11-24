from __future__ import annotations

import hashlib
import ipaddress
import json
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agent_bridge.secrets import redact

if TYPE_CHECKING:
    from agent_bridge.registry import Registry

COOKIE = 'agent_bridge_ui'
COOKIE_TOOLS = frozenset({'list_agents', 'list_sessions', 'check_task', 'get_result', 'get_transcript',
    'dispatch_task', 'invoke', 'cancel_task', 'list_approvals', 'approve_approval', 'deny_approval', 'dashboard_data'})
HEADERS = {
    'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    'X-Frame-Options': 'DENY', 'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store',
    'Referrer-Policy': 'no-referrer',
}


def loopback(host: str | None) -> bool:
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host or '').is_loopback
    except ValueError:
        return False


class Dashboard:
    def __init__(self, registry: Registry, url: str):
        self.registry = registry
        self.origin = url.rstrip('/')
        self.enabled = loopback(urlsplit(url).hostname)
        self.codes: dict[str, float] = {}
        self.sessions: dict[str, float] = {}

    def local(self, request: Request) -> bool:
        return (self.enabled and request.headers.get('host') == urlsplit(self.origin).netloc
            and request.client is not None and loopback(request.client.host)
            and request.headers.get('origin', self.origin) == self.origin
            and request.headers.get('sec-fetch-site', 'same-origin') not in {'cross-site', 'same-site'})

    def prune(self) -> None:
        now = time.monotonic()
        for store in (self.codes, self.sessions):
            for key in list(store):
                if store[key] <= now:
                    del store[key]

    def issue(self, request: Request) -> dict:
        if not self.local(request):
            raise ValueError('UI is available only on the admin loopback origin')
        self.prune()
        if len(self.codes) >= 128:
            raise ValueError('too many pending UI logins')
        code = 'abu_' + secrets.token_urlsafe(32)
        self.codes[hashlib.sha256(code.encode()).hexdigest()] = time.monotonic() + 60
        return {'ok': True, 'url': self.origin + '/ui#code=' + code, 'expires_in': 60}

    def authorized(self, request: Request, tool: str) -> bool:
        self.prune()
        digest = hashlib.sha256(request.cookies.get(COOKIE, '').encode()).hexdigest()
        return (tool in COOKIE_TOOLS and self.local(request) and request.headers.get('x-agent-bridge-ui') == '1'
            and digest in self.sessions)

    async def handle(self, request: Request) -> Response:
        if not self.local(request):
            return JSONResponse({'ok': False, 'error': 'local origin required'}, status_code=403 if self.enabled else 404)
        path = request.url.path
        if path == '/ui/login':
            if request.headers.get('x-agent-bridge-ui') != '1':
                return JSONResponse({'ok': False, 'error': 'UI header required'}, status_code=403)
            self.prune()
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 2048:
                    return JSONResponse({'ok': False}, status_code=413)
            try:
                code = json.loads(body).get('code')
                if not isinstance(code, str):
                    raise ValueError('invalid code')
            except (ValueError, AttributeError, UnicodeDecodeError, RecursionError):
                return JSONResponse({'ok': False, 'error': 'invalid login code'}, status_code=400)
            expires = self.codes.pop(hashlib.sha256(code.encode()).hexdigest(), 0)
            if expires <= time.monotonic() or len(self.sessions) >= 128:
                return JSONResponse({'ok': False, 'error': 'login code expired or used'}, status_code=401)
            cookie = 'abs_' + secrets.token_urlsafe(32)
            self.sessions[hashlib.sha256(cookie.encode()).hexdigest()] = time.monotonic() + 3600
            response = JSONResponse({'ok': True})
            response.set_cookie(COOKIE, cookie, httponly=True, samesite='strict', path='/',
                secure=urlsplit(self.origin).scheme == 'https')
            return response
        if path in {'/ui/logout', '/ui/data'}:
            if not self.authorized(request, 'dashboard_data'):
                return JSONResponse({'ok': False, 'error': 'UI login required'}, status_code=401)
            if path == '/ui/logout':
                self.sessions.pop(hashlib.sha256(request.cookies[COOKIE].encode()).hexdigest(), None)
                response = JSONResponse({'ok': True})
                response.delete_cookie(COOKIE, path='/', httponly=True, samesite='strict')
                return response
            remote = self.registry.config.hub.remote
            workspaces: dict[str, str | None] = {}
            for name, workspace in remote.remote_workspaces.items():
                try:
                    workspaces[name] = str(Path(workspace).expanduser().resolve())
                except (OSError, RuntimeError):
                    workspaces[name] = None
            audit = self.registry.home / 'remote-audit.jsonl'
            entries = []
            if audit.is_file():
                with audit.open('rb') as stream:
                    stream.seek(max(0, audit.stat().st_size - 65536))
                    for line in stream.read(65536).splitlines()[-30:]:
                        try:
                            entries.append(json.loads(line))
                        except ValueError:
                            continue
            return JSONResponse(redact({'ok': True,
                'tasks': [task.model_dump(mode='json', include={'task_id', 'session_id', 'agent', 'status',
                    'message', 'created_at', 'caller_delivery', 'callback_delivery', 'native_search'})
                    for task in sorted(self.registry.tasks.values(), key=lambda t: t.created_at, reverse=True)][:500],
                'remote': {'enabled': remote.enabled, 'public_url': remote.public_url,
                    'agents': remote.remote_callable_agents,
                    'workspaces': workspaces,
                    'require_approval': remote.require_approval},
                'audit': entries, 'approvals': self.registry.remote.list_approvals()['approvals']}))
        files = {'/ui': ('index.html', 'text/html'), '/ui/': ('index.html', 'text/html'),
            '/ui/app.js': ('app.js', 'application/javascript'), '/ui/style.css': ('style.css', 'text/css')}
        name, media = files[path]
        return Response((Path(__file__).parent / 'share' / 'ui' / name).read_bytes(), media_type=media)

    def routes(self) -> list[Route]:
        return [Route(path, self.handle, methods=[method]) for path, method in [
            ('/ui', 'GET'), ('/ui/', 'GET'), ('/ui/app.js', 'GET'), ('/ui/style.css', 'GET'),
            ('/ui/login', 'POST'), ('/ui/logout', 'POST'), ('/ui/data', 'GET')]]


class DashboardHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.update(HEADERS)
        return response
