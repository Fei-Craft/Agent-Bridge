from __future__ import annotations

import asyncio
from typing import Any

from agent_bridge.models import DEFAULT_WAIT_SEC
from agent_bridge.registry import RESULT_PAGE_MAX_CHARS, Registry
from agent_bridge.worker_env import caller_session_context


def _error(exc: Exception) -> dict[str, Any]:
    return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "error_type": type(exc).__name__}


async def wait_for_result(service: Any, task_id: str, timeout_sec: float | None = None, progress: Any = None) -> dict[str, Any]:
    if timeout_sec is not None and timeout_sec < 0:
        raise ValueError('timeout_sec must be non-negative')
    loop = asyncio.get_running_loop()
    started = loop.time()
    while True:
        remaining = None if timeout_sec is None else max(0, timeout_sec - (loop.time() - started))
        result = await service.wait_task(task_id=task_id, timeout_sec=15 if remaining is None else min(15, remaining))
        if not result['ok']:
            return result
        if result['status'] in {'completed', 'failed', 'cancelled'} or remaining == 0:
            return await service.get_result(task_id=task_id)
        if progress:
            await progress(loop.time() - started, task_id)


class BridgeService:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    async def invoke(self, agent: str, message: str, cwd: str | None = None, timeout_sec: float | None = None, **kwargs: Any) -> dict[str, Any]:
        if timeout_sec is not None and timeout_sec < 0:
            return _error(ValueError('timeout_sec must be non-negative'))
        dispatched = await self.dispatch_task(agent=agent, message=message, cwd=cwd, **kwargs)
        if not dispatched['ok']:
            return dispatched
        return await wait_for_result(self, dispatched['task_id'], timeout_sec)

    async def list_agents(self) -> dict[str, Any]:
        """List configured workers, the reconstructed host/proxy environment, and the coordinator policy (mode, instructions, runtime_context, dispatch_enabled). Call this first. Each agents[] row also carries quota: status ok | exhausted | unknown, windows[] with remaining_percent / resets_at / resets_in_sec, optional balance when reported by the provider, cached / stale flags, and detail. unknown means the quota could not be read (unsupported CLI, API-key login, timeout) — not that it is empty. Custom API/auth endpoints are unsupported; cached readings expire at window reset. Quota never affects available; treat it as information, routing still follows coordinator.instructions. Claude status covers shared 5h/weekly limits only: before dispatch, check the requested model's weekly:opus or weekly:sonnet remaining_percent even if status is ok. Zero means that model is exhausted; report its reset time or use an alternative allowed by coordinator.instructions. Missing or null readings mean unknown; model-specific windows alone cannot establish shared status. If dispatch_enabled is false, this is a nested worker-inherited instance — do not dispatch or set_preferences."""
        try:
            registry = self.registry
            agents = await registry.list_agents()
            return {
                "ok": True,
                "backend": "local",
                "agents": agents,
                "env": await registry.env_status(),
                "coordinator": registry.coordinator_status(),
            }
        except Exception as exc:
            return _error(exc)

    async def list_models(self, agent: str, cwd: str, refresh: bool = False) -> dict[str, Any]:
        """List models advertised by a supported worker. Pass this conversation's absolute project directory as cwd. Results are cached per worker/project for one hour by default ([models] cache_sec); refresh=true requests an immediate update. default_model reports the query session's advertised default. supported_efforts reports worker-native levels: null means unknown, [] means explicitly no levels. For dispatch_task.effort, use off|low|medium|high|max for adapter mapping. Available when coordinator.dispatch_enabled=true; on a hub it is served only on the loopback admin API, never on the remote gateway."""
        try:
            return {"ok": True, **await self.registry.list_models(agent, cwd, refresh=refresh)}
        except Exception as exc:
            return _error(exc)

    async def set_preferences(
        self,
        mode: str | None = None,
        instructions: str | None = None,
    ) -> dict[str, Any]:
        """Persist the coordinator policy when the user states a lasting preference (e.g. "from now on, research goes to antigravity"). mode is manual/auto/eager; instructions is free routing-preference text that REPLACES the stored text — read coordinator.instructions from list_agents first and write the merged result. Applies to this instance immediately and to others at their next start. Do not call for one-off, this-task-only wishes. Rejected when coordinator.dispatch_enabled is false (nested worker-inherited Bridge)."""
        try:
            result = self.registry.set_preferences(mode=mode, instructions=instructions)
            return {"ok": True, **result}
        except Exception as exc:
            return _error(exc)

    async def dispatch_task(
        self,
        agent: str,
        message: str,
        cwd: str | None = None,
        session_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        title: str | None = None,
        user_requested: bool = False,
        request_id: str | None = None,
        repo: str | None = None,
        ref: str | None = None,
        notify_caller: bool = False,
        callback_url: str | None = None,
        callback_secret_name: str | None = None,
    ) -> dict[str, Any]:
        """Start a worker turn. Local workers require an absolute existing cwd. Cloud workers ignore cwd and accept optional repo/ref overrides; follow-ups keep their repository/ref. Cloud Cursor model is a model ID; Devin model is a mode, or effort maps off/low/medium/high/max to fast/lite/normal/ultra/ultra. Remote results carry provider IDs, URLs and PRs rather than local file changes. Cancelling Devin terminates the remote session permanently. Local model/effort behavior is unchanged. Pass session_id to continue. Set user_requested=true only when the user explicitly asked for a worker (required in manual mode). Rejected when coordinator.dispatch_enabled is false. For retry deduplication, supply a UUID request_id on the first call and replay the same ID and original arguments; identical retries reuse the task, different arguments are rejected. Bindings are lost on restart and are not shared with other instances. Returns immediately."""
        try:
            result = await self.registry.dispatch_task(
                agent=agent,
                message=message,
                cwd=cwd,
                session_id=session_id,
                model=model,
                effort=effort,
                title=title,
                user_requested=user_requested,
                request_id=request_id,
                repo=repo,
                ref=ref,
                notify_caller=notify_caller,
                caller_session_id=caller_session_context.get(),
                callback_url=callback_url,
                callback_secret_name=callback_secret_name,
            )
            return {"ok": True, **result}
        except Exception as exc:
            return _error(exc)

    async def wait_task(self, task_id: str, timeout_sec: float = DEFAULT_WAIT_SEC) -> dict[str, Any]:
        """Wait until a task finishes or timeout_sec elapses (default 180). Timeout is not failure; call wait_task again. Stay under the host MCP tool timeout (Codex tool_timeout_sec, typically 600). Payloads carry silent_for_sec / stall_timeout_sec and files_changed_state (pending, collected, or unavailable)."""
        try:
            result = await self.registry.wait_task(task_id, timeout_sec=timeout_sec)
            return {"ok": True, **result}
        except Exception as exc:
            return _error(exc)

    async def check_task(self, task_id: str) -> dict[str, Any]:
        """Non-blocking status, elapsed time, and recent activity for a task. files_changed_state is pending until the final workspace diff, collected when the list/count are authoritative, or unavailable if collection could not run. files_changed is capped at 200 paths; files_changed_total carries the real count. silent_for_sec is the time since the worker's last output; Bridge fails the task with stop_reason "stalled" once it passes stall_timeout_sec."""
        try:
            return {"ok": True, **await self.registry.check_task(task_id)}
        except Exception as exc:
            return _error(exc)

    async def get_result(
        self,
        task_id: str,
        cursor: int = 0,
        max_chars: int = RESULT_PAGE_MAX_CHARS,
    ) -> dict[str, Any]:
        """Return a page of the complete worker result plus changed files, usage, and requested/observed model. Continue with next_cursor while has_more is true. max_chars is capped at 60000. files_changed_state says whether the list/count were collected; files_changed is capped at 200 paths and files_changed_total carries the real count. For Grok, observed_model is the live sampler; the worker saying it is Grok 4.6 is not. ACP failures may include failure (operation, kind, exit_code, prompt_may_have_been_sent, stderr_summary); error stays a string. Missing evidence or prompt_may_have_been_sent=true must not trigger blind replay; false is not proof of safe retry."""
        try:
            return {
                "ok": True,
                **await self.registry.get_result(task_id, cursor=cursor, max_chars=max_chars),
            }
        except Exception as exc:
            return _error(exc)

    async def get_transcript(
        self,
        session_id: str,
        offset: int = 0,
        limit: int = 50,
        kinds: str | None = None,
    ) -> dict[str, Any]:
        """Paged session transcript. kinds is an optional comma-separated event type list."""
        try:
            kind_list = [item.strip() for item in kinds.split(",") if item.strip()] if kinds else None
            return {
                "ok": True,
                **self.registry.get_transcript(session_id, offset=offset, limit=limit, kinds=kind_list),
            }
        except Exception as exc:
            return _error(exc)

    async def cancel_task(self, task_id: str) -> dict[str, Any]:
        """Cancel an in-flight worker turn. ACP sessions are cancelled; agy processes are killed. Rejected when coordinator.dispatch_enabled is false."""
        try:
            return {"ok": True, **await self.registry.cancel_task(task_id)}
        except Exception as exc:
            return _error(exc)

    async def list_sessions(self, active_only: bool = False) -> dict[str, Any]:
        """List sessions owned by this Bridge instance and report live siblings."""
        try:
            registry = self.registry
            return {
                "ok": True,
                **(await registry.session_scope()),
                "sessions": registry.list_sessions(active_only=active_only),
            }
        except Exception as exc:
            return _error(exc)

    async def end_session(self, session_id: str) -> dict[str, Any]:
        """Shut down a worker session process and mark it dead. Rejected when coordinator.dispatch_enabled is false."""
        try:
            return {"ok": True, **await self.registry.end_session(session_id)}
        except Exception as exc:
            return _error(exc)
