from __future__ import annotations

import asyncio
import codecs
import contextlib
import subprocess
import sys
from typing import Any

from agent_bridge.adapters.base import STDIO_LIMIT
from agent_bridge.adapters.codex import CodexAdapter
from agent_bridge.cli_exec import CliTurnState, build_cli_argv
from agent_bridge.diagnostics import redact_diagnostic
from agent_bridge.models import Session, Task, TaskFailure, TurnResult
from agent_bridge.processes import (
    drop_pid,
    kill_tree,
    process_create_time,
    process_image_name,
    reap_subprocess,
    record_pid,
)
from agent_bridge.transcript import append_event


class CliAdapter(CodexAdapter):
    async def cancel(self, session: Session) -> None:
        self._cancelled.add(session.session_id)
        proc = self._procs.get(session.session_id)
        if proc is not None:
            kill_tree(proc.pid, handle=proc, force=True)
            await reap_subprocess(proc)

    async def _drain_stderr(self, proc: asyncio.subprocess.Process, session_id: str) -> str:
        assert proc.stderr is not None
        tail = ''
        while chunk := await proc.stderr.read(4096):
            text = redact_diagnostic(chunk.decode('utf-8', errors='replace'))
            tail = (tail + text)[-16000:]
            append_event(session_id, 'raw', {'stderr': text}, self.home)
        return tail

    async def run_turn(self, session: Session, task: Task) -> TurnResult:
        cmd = build_cli_argv(self.agent, session, task)
        env = self._worker_env()
        env['PWD'] = task.cwd or session.cwd
        env = self._enforce_spawn_env(env)
        kwargs: dict[str, Any] = {}
        if sys.platform == 'win32':
            kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
        append_event(session.session_id, 'prompt_sent', {'text': task.message}, self.home)
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, cwd=task.cwd or session.cwd,
            limit=STDIO_LIMIT, **kwargs,
        )
        self._procs[session.session_id] = proc
        if self.worker_tokens is not None and env.get('AGENT_BRIDGE_HUB_TOKEN', '').startswith('abw_'):
            self.worker_tokens.processes[session.session_id] = proc
        self._cancelled.discard(session.session_id)
        record_pid(self.home, session.session_id, proc.pid, process_create_time(proc.pid),
                   process_image_name(proc.pid))
        state = CliTurnState(self.agent.cli.parser, session_id=session.native_session_id)
        stderr_task = asyncio.create_task(self._drain_stderr(proc, session.session_id))
        writer = asyncio.create_task(self._write_prompt(proc, task.message if self.agent.cli.prompt_stdin else ''))
        reason = 'end_turn'
        error = None
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        try:
            assert proc.stdout is not None
            while True:
                chunk = (await proc.stdout.read(4096) if self.agent.cli.parser == 'text'
                         else await proc.stdout.readline())
                if not chunk:
                    break
                raw = (decoder.decode(chunk) if self.agent.cli.parser == 'text'
                       else chunk.decode('utf-8', errors='replace'))
                event = state.feed(raw)
                append_event(session.session_id, event, {'text': redact_diagnostic(raw[:4000])}, self.home)
            if self.agent.cli.parser == 'text':
                state.feed(decoder.decode(b'', final=True))
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except TimeoutError:
                await reap_subprocess(proc)
                error = 'CLI closed stdout but did not exit'
            stderr = await stderr_task
            await writer
            if session.session_id in self._cancelled:
                reason = 'cancelled'
            else:
                error = error or state.error
                if proc.returncode:
                    error = error or f'CLI exited with code {proc.returncode}: {stderr}'
                if self.agent.cli.parser != 'text' and not state.events:
                    error = error or 'CLI produced no JSON object events; check cli.parser and output flags'
                if error:
                    reason = 'error'
                    error = redact_diagnostic(error)
                    task.failure = TaskFailure(operation='run_turn', kind='cli_error',
                                               prompt_may_have_been_sent=True, exit_code=proc.returncode,
                                               stderr_summary=stderr or None)
        except asyncio.CancelledError:
            await self.cancel(session)
            reason = 'cancelled'
        except Exception:
            await self.cancel(session)
            raise
        finally:
            for pending in (writer, stderr_task):
                if not pending.done():
                    pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending
            self._procs.pop(session.session_id, None)
            self._cancelled.discard(session.session_id)
            drop_pid(self.home, session.session_id)
        if state.session_id:
            session.native_session_id = state.session_id
        append_event(session.session_id, 'turn_end', {'stop_reason': reason}, self.home)
        return TurnResult(text=state.text, native_session_id=state.session_id, usage=state.usage,
                          stop_reason=reason, error=error, observed_model=state.model or task.model or session.model,
                          observed_effort=task.effort or session.effort)
