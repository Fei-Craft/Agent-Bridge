"""Minimal ACP echo agent used for offline adapter tests."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from acp import run_agent, update_agent_message_text
from acp.exceptions import RequestError
from acp.schema import (
    AgentCapabilities,
    Implementation,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptCapabilities,
    PromptResponse,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SetSessionConfigOptionResponse,
    Usage,
)


def _record_effect(path: str) -> None:
    with Path(path).open("a", encoding="utf-8") as marker:
        marker.write("executed\n")


class EchoAgent:
    def __init__(self) -> None:
        self._conn: Any = None
        self._session_id = "echo-session"
        self._ready = False
        self._model = "default-model"
        self._effort = "low"
        self._loaded = False
        categorized = os.environ.get("BRIDGE_ECHO_CATEGORIZED") == "1"
        self._model_option = "engine" if categorized else "model"
        self._effort_option = "depth" if categorized else "thought_level"

    def _config_options(self) -> list[SessionConfigOptionSelect] | None:
        if os.environ.get("BRIDGE_ECHO_SELECTION") != "1":
            return None

        def option(
            option_id: str, current: str, values: list[str], category: str,
        ) -> SessionConfigOptionSelect:
            return SessionConfigOptionSelect(
                id=option_id, name=option_id, type="select", current_value=current,
                category=category if self._model_option != "model" else None,
                options=[SessionConfigSelectOption(value=value, name=value) for value in values],
            )

        result = [option(self._model_option, self._model, ["default-model", "family", "family-high"], "model")]
        if self._model == "family":
            result.append(option(self._effort_option, self._effort, ["none", "low", "high", "xhigh"], "thought_level"))
        return result

    def on_connect(self, conn: Any) -> None:
        self._conn = conn

    async def initialize(self, protocol_version: int, **kwargs: Any) -> InitializeResponse:
        failure = os.environ.get("BRIDGE_ECHO_FAILURE")
        if failure == "initialize":
            raise RequestError.auth_required()
        if failure == "initialize_exit":
            os._exit(9)
        if failure == "stderr_flood":
            sys.stderr.write("x" * (17 * 1024 * 1024))
            sys.stderr.write("\nauthentication required\n")
            sys.stderr.flush()
            await asyncio.sleep(0.5)
            raise RequestError.auth_required()
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_capabilities=AgentCapabilities(
                prompt_capabilities=PromptCapabilities(image=False, audio=False, embedded_context=False),
                load_session=True,
            ),
            agent_info=Implementation(name="echo", version="0.0.1"),
        )

    async def new_session(self, cwd: str, mcp_servers=None, **kwargs: Any) -> NewSessionResponse:
        if os.environ.get("BRIDGE_ECHO_FAILURE") == "new":
            raise RequestError.auth_required()
        self._ready = True
        return NewSessionResponse(session_id=self._session_id, config_options=self._config_options())

    async def load_session(
        self, cwd: str, session_id: str, mcp_servers=None, **kwargs: Any
    ) -> LoadSessionResponse | None:
        if os.environ.get("BRIDGE_ECHO_FAILURE") == "load":
            raise RequestError.auth_required()
        if os.environ.get("BRIDGE_ECHO_FAILURE") == "load_internal":
            # Generic failure (not "unavailable"): Bridge should fall back to
            # session/new and mark the switch visibly (H-05).
            raise RequestError(-32603, "native session data corrupt")
        self._session_id = session_id
        self._ready = True
        self._loaded = True
        options = self._config_options()
        return LoadSessionResponse(config_options=options) if options is not None else None

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: Any
    ) -> SetSessionConfigOptionResponse:
        for option in self._config_options() or []:
            if option.id == config_id and any(
                isinstance(item, SessionConfigSelectOption) and item.value == value for item in option.options
            ):
                if config_id == self._model_option:
                    self._model = str(value)
                    if os.environ.get("BRIDGE_ECHO_COERCE_MODEL") == "1":
                        self._model = "family"
                    self._effort = "low"
                else:
                    if value == "high" and os.environ.get("BRIDGE_ECHO_REJECT_HIGH") == "1":
                        raise RequestError(-32602, "rejected thought level")
                    self._effort = str(value)
                    if os.environ.get("BRIDGE_ECHO_COERCE_EFFORT") == "1":
                        self._effort = "xhigh"
                return SetSessionConfigOptionResponse(config_options=self._config_options() or [])
        raise RequestError(-32602, "unsupported config value")

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        if not self._ready:
            raise RequestError.resource_not_found(session_id)
        if os.environ.get("BRIDGE_ECHO_FAILURE") == "prompt_exit":
            await asyncio.to_thread(_record_effect, os.environ["BRIDGE_ECHO_MARKER"])
            print("Connection closed", file=sys.stderr, flush=True)
            os._exit(7)
        if os.environ.get("BRIDGE_ECHO_FAILURE") == "prompt_hang":
            await asyncio.to_thread(_record_effect, os.environ["BRIDGE_ECHO_MARKER"])
            await asyncio.Event().wait()
        text = ""
        for block in prompt:
            piece = getattr(block, "text", None)
            if isinstance(piece, str):
                text += piece
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                text += block["text"]
        if self._conn is not None:
            label = "loaded" if self._loaded and self._config_options() else "echo"
            try:
                await self._conn.session_update(
                    session_id=session_id,
                    update=update_agent_message_text(f"{label}:{text}"),
                )
            except TypeError:
                await self._conn.session_update(
                    session_id,
                    update_agent_message_text(f"{label}:{text}"),
                )
        return PromptResponse(
            stop_reason="end_turn",
            usage=Usage(total_tokens=3, input_tokens=1, output_tokens=2),
        )

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        return None

    async def authenticate(self, method_id: str, **kwargs: Any) -> None:
        return None

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return None


async def _main() -> None:
    await run_agent(EchoAgent())


if __name__ == "__main__":
    asyncio.run(_main())
