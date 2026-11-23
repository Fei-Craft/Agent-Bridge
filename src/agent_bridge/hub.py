from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import secrets
import tempfile
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from agent_bridge.api import BridgeService, _error, wait_for_result
from agent_bridge.hub_client import ROUTES
from agent_bridge.paths import ensure_home
from agent_bridge.registry import Registry
from agent_bridge.worker_env import caller_session_context, hub_worker_context


def validate_host(host: str, allow_remote: bool = False) -> None:
    try:
        loopback = host == 'localhost' or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback and not allow_remote:
        raise ValueError('non-loopback host requires --allow-remote')


def create_app(registry: Registry | None = None, *, url: str = 'http://127.0.0.1:8765', token: str | None = None) -> Starlette:
    registry = registry or Registry.create(ensure_home(), runtime_context='coordinator')
    registry.runtime_context = 'coordinator'
    registry.dispatch_enabled = True
    registry.config.server.idle_exit_sec = 0
    secret = token or secrets.token_urlsafe(32)
    registry.deliveries.worker_context = {'AGENT_BRIDGE_HUB_URL': url, 'AGENT_BRIDGE_HUB_TOKEN': secret}
    call_depth: ContextVar[int] = ContextVar('call_depth', default=0)

    class HubService(BridgeService):
        async def dispatch_task(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            result = await super().dispatch_task(*args, **kwargs)
            if result['ok']:
                sid = result['session_id']
                self.registry.sessions[sid].call_depth = max(self.registry.sessions[sid].call_depth, call_depth.get())
                self.registry.save()
            return result

    service = HubService(registry)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        path = registry.home / 'hub.json'
        await registry.start()
        try:
            with tempfile.NamedTemporaryFile(mode='w', dir=registry.home, delete=False) as file:
                temporary = file.name
                json.dump(dict(url=url, pid=os.getpid(), token=secret, started_at=datetime.now(UTC).isoformat()), file)
            os.replace(temporary, path)
            yield
        finally:
            try:
                if path.exists() and json.loads(path.read_text()).get('token') == secret:
                    path.unlink()
            finally:
                await registry.stop()

    def endpoint(tool: str):
        async def handle(request: Request):
            if not secrets.compare_digest(request.headers.get('authorization', ''), f'Bearer {secret}'):
                return JSONResponse(_error(PermissionError('invalid bearer token')), status_code=401)
            if tool == 'health':
                return JSONResponse({'ok': True})
            try:
                args: dict[str, Any] = dict(request.query_params)
                if request.method in ('POST', 'PUT'):
                    body = await request.body()
                    if body:
                        args.update(json.loads(body))
                args.update(request.path_params)
                for key in ('cursor', 'max_chars', 'offset', 'limit'):
                    if key in args:
                        args[key] = int(args[key])
                if 'timeout_sec' in args:
                    args['timeout_sec'] = float(args['timeout_sec'])
                if 'active_only' in args:
                    if args['active_only'] not in ('true', 'false'):
                        raise ValueError('active_only must be true or false')
                    args['active_only'] = args['active_only'] == 'true'
                caller = request.headers.get('x-agent-bridge-caller')
                depth = 0
                if caller and tool in ('dispatch_task', 'invoke'):
                    if caller not in registry.sessions:
                        raise ValueError('unknown caller session')
                    if args.get('session_id') == caller:
                        raise ValueError('cannot dispatch into caller session')
                    caller_task = registry._busy_task(caller)
                    if caller_task and caller_task.notification_source_task_id:
                        args['notify_caller'] = False
                        args.pop('callback_url', None)
                        args.pop('callback_secret', None)
                    depth = registry.sessions[caller].call_depth + 1
                    if depth > registry.config.hub.max_depth:
                        raise ValueError('hub max_depth exceeded')
                context = hub_worker_context.set({'AGENT_BRIDGE_HUB_URL': url, 'AGENT_BRIDGE_HUB_TOKEN': secret})
                depth_context = call_depth.set(depth)
                caller_context = caller_session_context.set(caller)
                try:
                    if tool == 'invoke':
                        timeout = args.pop('timeout_sec', None)
                        if timeout is not None and timeout < 0:
                            raise ValueError('timeout_sec must be non-negative')
                        dispatched = await service.dispatch_task(**args)
                        if not dispatched['ok']:
                            return JSONResponse(dispatched, status_code=400)

                        async def stream():
                            waiting = asyncio.create_task(wait_for_result(service, dispatched['task_id'], timeout))
                            try:
                                while not waiting.done():
                                    yield b' '
                                    await asyncio.wait({waiting}, timeout=15)
                                yield json.dumps(await waiting, ensure_ascii=False).encode()
                            finally:
                                if not waiting.done():
                                    waiting.cancel()
                                await asyncio.gather(waiting, return_exceptions=True)
                        return StreamingResponse(stream(), media_type='application/json', headers={'X-Agent-Bridge-Task-Id': dispatched['task_id']})
                    result = await getattr(service, tool)(**args)
                finally:
                    hub_worker_context.reset(context)
                    call_depth.reset(depth_context)
                    caller_session_context.reset(caller_context)
                if tool == 'list_agents':
                    result.update(backend='hub', hub_url=url)
            except Exception as exc:
                result = _error(exc)
            return JSONResponse(result, status_code=200 if result['ok'] else 400)
        return handle

    routes = [Route('/v1/health', endpoint('health'))]
    routes.extend(Route(path, endpoint(tool), methods=[method]) for tool, (method, path, _) in ROUTES.items())
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.token = secret
    app.state.registry = registry
    return app


def serve(host: str | None = None, port: int | None = None, allow_remote: bool = False) -> None:
    registry = Registry.create(runtime_context='coordinator')
    host = host or registry.config.hub.host
    port = port if port is not None else registry.config.hub.port
    validate_host(host, allow_remote)
    url_host = f'[{host}]' if ':' in host else host
    app = create_app(registry, url=f'http://{url_host}:{port}')
    uvicorn.run(app, host=host, port=port)
