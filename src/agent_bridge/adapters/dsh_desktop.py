from __future__ import annotations

import asyncio
import contextlib
import uuid
from pathlib import Path

from agent_bridge.adapters.base import Adapter
from agent_bridge.config import AgentConfig, EnvConfig
from agent_bridge.desktop_ipc import DesktopIPC, DesktopUnavailable
from agent_bridge.models import Session, Task, TurnResult
from agent_bridge.transcript import append_event, mark_worker_activity


def _message_text(event: dict) -> str:
    content = event.get("data", {}).get("message", {}).get("content", [])
    return "".join(part.get("text", "") for part in content if part.get("type") == "text")


class DesktopAdapter(Adapter):
    resident = False

    def __init__(self, agent: AgentConfig, home: Path, env_config: EnvConfig | None, ipc: DesktopIPC) -> None:
        super().__init__(agent, home, env_config)
        self.ipc = ipc

    def can_fork(self) -> bool:
        return True

    async def ensure_session(self, session: Session) -> None:
        if session.native_session_id:
            return
        if session.fork_source_id:
            created = await self.ipc.call("fork", {"sessionId": session.fork_source_id})
        else:
            created = await self.ipc.call("create", {"cwd": session.cwd})
        session.native_session_id = created["sessionId"]
        if session.title or session.fork_label:
            await self.ipc.call("rename", {
                "sessionId": session.native_session_id,
                "title": session.fork_label or session.title,
            })

    async def run_turn(self, session: Session, task: Task) -> TurnResult:
        await self.ensure_session(session)
        native_id = session.native_session_id
        assert native_id is not None
        if task.model or task.effort:
            if task.model:
                if "/" not in task.model:
                    raise ValueError("Desktop model selection must be provider/model")
                provider, model = task.model.split("/", 1)
            else:
                default = (await self.ipc.call("modelCatalog", {}))["default"]
                provider, model = default["provider"], default["model"]
            selection = {"sessionId": native_id, "provider": provider, "model": model}
            if task.effort:
                selection["reasoningEffort"] = task.effort
            await self.ipc.call("selectModel", selection)

        text_parts: list[str] = []
        request_id = str(uuid.uuid4())
        active_turn: int | None = None
        latest_turn: int | None = None
        async with self.ipc.request("follow", {"address": {"kind": "session", "sessionId": native_id}}) as frames:
            opening = await asyncio.wait_for(anext(frames, None), 10)
            if opening != {"type": "snapshot"}:
                raise RuntimeError("DeepSeek Harness follow stream did not open")
            append_event(session.session_id, "prompt_sent", {"text": task.message}, self.home)
            await self.ipc.call("prompt", {"requestId": request_id, "sessionId": native_id,
                                          "mode": "queue", "content": [{"type": "text", "text": task.message}]})
            async for value in frames:
                if value.get("type") != "event":
                    continue
                event = value.get("event", {})
                kind = event.get("type")
                data = event.get("data", {})
                if kind == "turn/start":
                    latest_turn = data.get("turn")
                if kind == "user/message" and data.get("source", {}).get("rpcId") == request_id:
                    active_turn = latest_turn
                if active_turn is None:
                    continue
                mark_worker_activity(session.session_id, self.home)
                if kind == "assistant/message":
                    text_parts.append(_message_text(event))
                elif kind == "turn/end" and data.get("turn") == active_turn:
                    reason = data.get("reason", {})
                    stop = reason.get("kind", "completed")
                    if stop == "aborted":
                        stop = "cancelled"
                    append_event(session.session_id, "turn_end", {"stop_reason": stop}, self.home)
                    return TurnResult(text="".join(text_parts), stop_reason=stop,
                                      native_session_id=native_id, observed_model=task.model,
                                      observed_effort=task.effort,
                                      error=reason.get("error", {}).get("message") if stop == "error" else None)
        raise DesktopUnavailable("DeepSeek Harness follow stream closed before turn ended")

    async def cancel(self, session: Session) -> None:
        if session.native_session_id:
            with contextlib.suppress(DesktopUnavailable):
                await self.ipc.call("cancel", {"sessionId": session.native_session_id})

    async def shutdown(self, session: Session) -> None:
        pass
