from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from contextlib import suppress
from typing import Any
from urllib.parse import quote

import httpx2

from agent_bridge.adapters.base import Adapter
from agent_bridge.config import CloudConfig
from agent_bridge.models import Session, Task, TurnResult
from agent_bridge.transcript import append_event


class CloudError(RuntimeError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f'cloud API returned HTTP {status}')


def _scrub(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return value.replace(secret, '[REDACTED]') if secret else value
    if isinstance(value, dict):
        return {_scrub(k, secret): _scrub(v, secret) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, secret) for v in value]
    return value


class CloudAdapter(Adapter):
    resident = False
    on_progress: Callable[[], None] | None = None
    _task: Task | None = None

    def can_revive(self) -> bool:
        return True

    @property
    def cfg(self) -> CloudConfig:
        assert self.agent.cloud is not None
        return self.agent.cloud

    async def ensure_session(self, session: Session) -> None:
        if session.remote.get('resumable') is False:
            raise ValueError('cloud session was terminated and cannot be resumed; start a new session')

    async def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        key = os.environ.get(self.cfg.api_key_env, '')
        if not key:
            raise ValueError(f'missing cloud API key environment variable {self.cfg.api_key_env}')
        try:
            async with httpx2.AsyncClient(timeout=60, trust_env=False, follow_redirects=False) as client:
                response = await client.request(
                    method, self.cfg.base_url + path,
                    headers={'Authorization': f'Bearer {key}'}, json=body,
                )
            if response.status_code >= 300:
                raise CloudError(response.status_code)
            data = _scrub(response.json(), key)
            if not isinstance(data, dict):
                raise ValueError('cloud API response must be an object')
            return data
        except httpx2.HTTPError:
            raise RuntimeError('cloud API network request failed') from None
        except json.JSONDecodeError:
            raise RuntimeError('cloud API returned invalid JSON') from None

    def _record(self, session: Session, task: Task, **fields: Any) -> None:
        previous = task.remote.get('status'), task.remote.get('status_detail')
        task.remote.update(fields)
        session.remote.update(fields)
        current = task.remote.get('status'), task.remote.get('status_detail')
        if previous != current:
            append_event(session.session_id, 'cloud_status', {
                'status': current[0], 'status_detail': current[1],
            }, self.home)
        if self.on_progress:
            self.on_progress()

    async def run_turn(self, session: Session, task: Task) -> TurnResult:
        await self.ensure_session(session)
        self._task = task
        try:
            if self.cfg.provider == 'cursor':
                return await self._cursor(session, task)
            return await self._devin(session, task)
        finally:
            append_event(session.session_id, 'turn_end', {}, self.home)

    async def _create(self, session: Session, task: Task, path: str, body: dict) -> dict:
        request = asyncio.create_task(self._request('POST', path, body))
        cancelled = False
        try:
            data = await asyncio.shield(request)
        except asyncio.CancelledError:
            cancelled = True
            data = await request
        if self.cfg.provider == 'cursor':
            if 'agent' in data:
                session.native_session_id = data['agent']['id']
                self._record(session, task, url=data['agent'].get('url'))
            self._record(session, task, provider='cursor', agent_id=session.native_session_id,
                         run_id=data['run']['id'])
        else:
            session.native_session_id = data['session_id']
            self._record(session, task, provider='devin', session_id=session.native_session_id,
                         url=data.get('url'))
        if cancelled:
            await self.cancel(session)
            raise asyncio.CancelledError
        return data

    async def _cursor(self, session: Session, task: Task) -> TurnResult:
        body: dict[str, Any] = {'prompt': {'text': task.message}}
        model = task.model or self.cfg.model
        if model:
            body['model'] = {'id': model}
        if session.native_session_id:
            path = f'/v1/agents/{quote(session.native_session_id, safe="")}/runs'
            for attempt in range(4):
                try:
                    created = await self._create(session, task, path, body)
                    break
                except CloudError as exc:
                    if exc.status != 409 or attempt == 3:
                        raise
                    await asyncio.sleep(self.cfg.poll_interval_sec * (2 ** attempt))
        else:
            if task.repo:
                body['repos'] = [{'url': task.repo}]
                if task.ref:
                    body['repos'][0]['startingRef'] = task.ref
            body['autoCreatePR'] = self.cfg.auto_create_pr
            created = await self._create(session, task, '/v1/agents', body)
        agent_id = session.native_session_id
        run = created['run']
        run_id = run['id']
        self._record(session, task, provider='cursor', agent_id=agent_id, run_id=run_id)
        path = f'/v1/agents/{quote(str(agent_id), safe="")}/runs/{quote(run_id, safe="")}'
        while True:
            self._record(session, task, status=run['status'])
            if run['status'] in {'FINISHED', 'ERROR', 'EXPIRED', 'CANCELLED'}:
                break
            await asyncio.sleep(self.cfg.poll_interval_sec)
            run = await self._request('GET', path)
        branches = run.get('git', {}).get('branches', [])
        self._record(session, task, branches=branches,
                     pr_urls=[b['prUrl'] for b in branches if b.get('prUrl')])
        text = run.get('result', '')
        if text:
            append_event(session.session_id, 'message_chunk', {'text': text}, self.home)
        usage: dict[str, Any] = {}
        with suppress(CloudError, RuntimeError, ValueError):
            usage = (await self._request('GET', f'/v1/agents/{quote(str(agent_id), safe="")}/usage?runId={quote(run_id, safe="")}')).get('totalUsage', {})
        return TurnResult(
            text=text, remote=dict(task.remote), usage=usage, native_session_id=agent_id,
            observed_model=model,
            stop_reason='cancelled' if run['status'] == 'CANCELLED' else 'end_turn',
            error=run['status'] if run['status'] in {'ERROR', 'EXPIRED'} else None,
        )

    def _devin_root(self) -> str:
        org = self.cfg.org_id or os.environ.get(self.cfg.org_id_env)
        if not org:
            raise ValueError('Devin cloud requires org_id or its org_id_env environment variable')
        return f'/v3/organizations/{quote(org, safe="")}/sessions'

    def _devin_path(self, native_id: str) -> str:
        devin_id = native_id if native_id.startswith('devin-') else f'devin-{native_id}'
        return self._devin_root() + '/' + quote(devin_id, safe='')

    async def _messages(self, path: str) -> list[dict]:
        messages: list[dict] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            query = '?first=200' + (f'&after={quote(cursor, safe="")}' if cursor else '')
            page = await self._request('GET', path + '/messages' + query)
            messages.extend(page.get('items', []))
            if not page.get('has_next_page'):
                return messages
            cursor = page.get('end_cursor')
            if not cursor or cursor in seen:
                raise RuntimeError('cloud messages pagination did not advance')
            seen.add(cursor)

    async def _devin(self, session: Session, task: Task) -> TurnResult:
        root = self._devin_root()
        effort_modes = {'off': 'fast', 'low': 'lite', 'medium': 'normal', 'high': 'ultra', 'max': 'ultra'}
        if not task.model and task.effort and task.effort not in effort_modes:
            raise ValueError('Devin effort must be off, low, medium, high or max; use model for a Devin mode')
        mode = task.model or effort_modes.get(task.effort or '') or self.cfg.devin_mode
        if session.native_session_id:
            path = self._devin_path(session.native_session_id)
            seen = {m['event_id'] for m in await self._messages(path)}
            if mode and mode != session.remote.get('devin_mode'):
                raise ValueError('Devin mode cannot change on a follow-up; start a new session')
            await self._request('POST', path + '/messages', {'message': task.message})
        else:
            seen = set()
            body: dict[str, Any] = {'prompt': task.message}
            if task.repo:
                body['repos'] = [task.repo]
            if task.ref:
                body['prompt'] += f'\nUse repository ref: {task.ref}'
            if mode:
                body['devin_mode'] = mode
            if self.cfg.max_acu_limit is not None:
                body['max_acu_limit'] = self.cfg.max_acu_limit
            data = await self._create(session, task, root, body)
            assert session.native_session_id is not None
            path = self._devin_path(session.native_session_id)
            self._record(session, task, url=data.get('url'), devin_mode=mode)
        self._record(session, task, provider='devin', session_id=session.native_session_id)
        texts: list[str] = []
        failures = {
            'usage_limit_exceeded', 'out_of_credits', 'out_of_quota', 'no_quota_allocation',
            'payment_declined', 'org_usage_limit_exceeded', 'user_usage_limit_exceeded',
            'total_session_limit_exceeded', 'contract_expired', 'error',
        }
        while True:
            data = await self._request('GET', path)
            status, detail = data.get('status'), data.get('status_detail')
            self._record(session, task, status=status, status_detail=detail)
            for message in await self._messages(path):
                event_id = message['event_id']
                if event_id not in seen:
                    seen.add(event_id)
                    if message.get('source') == 'devin':
                        text = message.get('message', '')
                        texts.append(text)
                        append_event(session.session_id, 'message_chunk', {'text': text}, self.home)
            terminal = status in {'exit', 'error'} or detail in failures | {'finished', 'waiting_for_user', 'waiting_for_approval'}
            if terminal:
                break
            await asyncio.sleep(self.cfg.poll_interval_sec)
        prs = data.get('pull_requests') or []
        self._record(session, task, pr_urls=[p['pr_url'] for p in prs],
                     structured_output=data.get('structured_output'))
        if data.get('structured_output') is not None:
            texts.append(json.dumps(data['structured_output'], ensure_ascii=False))
        return TurnResult(
            text='\n'.join(texts), native_session_id=session.native_session_id,
            remote=dict(task.remote), usage={'acus_consumed': data.get('acus_consumed', 0)},
            observed_model=mode,
            error=str(detail or status) if status == 'error' or detail in failures else None,
            warnings=['Devin is waiting for approval'] if detail == 'waiting_for_approval' else [],
        )

    async def cancel(self, session: Session) -> None:
        if not session.native_session_id:
            return
        if self.cfg.provider == 'cursor':
            if session.remote.get('run_id'):
                agent = quote(session.native_session_id, safe='')
                run = quote(session.remote['run_id'], safe='')
                try:
                    await self._request('POST', f'/v1/agents/{agent}/runs/{run}/cancel')
                except CloudError as exc:
                    if exc.status != 409:
                        raise
                else:
                    session.remote['status'] = 'CANCELLED'
                    if self._task is not None:
                        self._record(session, self._task, status='CANCELLED')
        elif session.remote.get('resumable') is not False:
            data = await self._request('DELETE', self._devin_path(session.native_session_id) + '?archive=false')
            session.remote['resumable'] = False
            if self._task is not None:
                self._record(session, self._task, resumable=False, status=data.get('status'),
                             status_detail=data.get('status_detail'))
            if self.on_progress:
                self.on_progress()

    async def shutdown(self, session: Session) -> None:
        if self.cfg.provider == 'cursor' and session.native_session_id:
            await self._request('POST', f'/v1/agents/{quote(session.native_session_id, safe="")}/archive')
        elif self.cfg.provider == 'devin':
            await self.cancel(session)
