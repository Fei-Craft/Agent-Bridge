import asyncio
import contextlib
import json
import os
import sys
import time
import types
from unittest.mock import AsyncMock

import pytest

from agent_bridge.hub_client import HubClient
from agent_bridge.processes import reap_subprocess
from agent_bridge.registry import Registry
from agent_bridge.server import INSTRUCTIONS, _error, _registry, create_baseline, lifespan, list_sessions, mcp


def test_tools_registered():
    names = sorted(mcp._tool_manager._tools)
    assert names == [
        "cancel_task",
        "check_task",
        "create_baseline",
        "dispatch_task",
        "end_session",
        "get_result",
        "get_transcript",
        "invoke_agent",
        "list_agents",
        "list_baselines",
        "list_sessions",
        "set_default_baseline",
        "set_preferences",
        "wait_task",
    ]


def test_handshake_instructions_carry_hard_rules():
    assert mcp.instructions == INSTRUCTIONS
    for phrase in (
        "dispatch_task.cwd",
        "wait_task",
        "coordinator.mode",
        "dispatch_enabled",
        "runtime_context",
        "cancel_task",
        "end_session",
    ):
        assert phrase in INSTRUCTIONS


def test_error_exposes_exception_type():
    assert _error(ValueError("bad cwd")) == {
        "ok": False,
        "error": "ValueError: bad cwd",
        "error_type": "ValueError",
    }


async def test_manual_mcp_host_requires_user_request_to_create_baseline(monkeypatch):
    client = HubClient("http://hub")
    monkeypatch.setenv("AGENT_BRIDGE_MODE", "manual")
    monkeypatch.setattr("agent_bridge.server.discover", AsyncMock(return_value=client))
    forward = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(client, "_call", forward)
    async with lifespan(mcp) as backend:
        ctx = types.SimpleNamespace(request_context=types.SimpleNamespace(lifespan_context=backend))
        refused = await create_baseline(ctx, session_id="sess", name="Baseline")
        assert not refused["ok"] and "coordinator mode is manual" in refused["error"]
        forward.assert_not_awaited()
        assert (await create_baseline(ctx, session_id="sess", name="Baseline", user_requested=True))["ok"]
        forward.assert_awaited_once_with("create_baseline", session_id="sess", name="Baseline",
                                         description=None, user_requested=True)


def test_registry_from_lifespan_context_touches_activity(bridge_home):
    registry = Registry.create(bridge_home)
    registry._last_activity = registry._last_activity - 1
    before = registry._last_activity
    ctx = types.SimpleNamespace(
        request_context=types.SimpleNamespace(lifespan_context=registry)
    )
    got = _registry(ctx)
    assert got is registry
    assert registry._last_activity > before


@pytest.mark.asyncio
async def test_list_sessions_does_not_block_event_loop(bridge_home, monkeypatch):
    def slow_count() -> int:
        time.sleep(0.5)
        return 3

    monkeypatch.setattr("agent_bridge.registry.count_sibling_servers", slow_count)
    ticks = 0

    async def counter() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.02)

    registry = Registry.create(bridge_home)
    ctx = types.SimpleNamespace(
        request_context=types.SimpleNamespace(lifespan_context=registry)
    )
    task = asyncio.create_task(counter())
    try:
        result = await list_sessions(ctx)
        assert result == {
            "ok": True,
            "scope": "current_instance",
            "other_live_instances": 3,
            "sessions": [],
        }
        assert ticks >= 10
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_stdio_server_exits_on_host_eof(bridge_home):
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c",
        "from agent_bridge.server import mcp; mcp.run(transport='stdio')",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "AGENT_BRIDGE_HOME": str(bridge_home), "AGENT_BRIDGE_PARENT_CONTEXT": ""},
    )
    try:
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "eof-test", "version": "1"},
            },
        }
        proc.stdin.write((json.dumps(request) + "\n").encode())
        await proc.stdin.drain()
        response = json.loads(await asyncio.wait_for(proc.stdout.readline(), timeout=10))
        assert response["id"] == 1 and "result" in response
        assert proc.returncode is None
        proc.stdin.close()
        assert await asyncio.wait_for(proc.wait(), timeout=10) == 0
    finally:
        await reap_subprocess(proc)


def test_registry_rejects_dict_lifespan(bridge_home):
    registry = Registry.create(bridge_home)
    ctx = types.SimpleNamespace(
        request_context=types.SimpleNamespace(lifespan_context={"registry": registry})
    )
    with pytest.raises(RuntimeError, match="not available"):
        _registry(ctx)
