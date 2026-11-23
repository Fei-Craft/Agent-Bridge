from __future__ import annotations

import asyncio
import json
import sys
import uuid
from unittest.mock import AsyncMock

import pytest

from agent_bridge.adapters.codex import CodexAdapter
from agent_bridge.api import BridgeService
from agent_bridge.config import AgentConfig, AppConfig
from agent_bridge.models import Session, Task, TurnResult
from agent_bridge.native_search import AcpNativeSearch, observe_codex_search, safe_url
from agent_bridge.registry import Registry


def search_args(tmp_path):
    return dict(agent="codex", capability="search", prompt="Find the source",
                cwd=str(tmp_path), request_id=str(uuid.uuid4()), user_requested=True)


def search_config():
    cfg = AgentConfig(name="codex", protocol="codex", command=["unused"], stall_timeout_sec=0)
    return AppConfig(agents={"codex": cfg})


@pytest.mark.parametrize("url", [
    "https://example.org/?X-Amz-Signature=abc",
    "https://user:secret@example.org/",
    "http://127.0.0.1/",
    "http://localhost./",
    "https://example.org/?access_token=abc",
])
def test_source_urls_do_not_export_credentials_or_signed_links(url):
    assert safe_url(url) is None


@pytest.mark.parametrize("agent,identity,output", [
    ("grok", {"toolName": "web_search"}, {"type": "WebSearch", "citations": ["https://example.org/"]}),
    ("grok", {"rawInput": {"variant": "WebSearch"}}, {"type": "WebSearch", "citations": ["https://example.org/"]}),
    ("grok", {"_meta": {"x.ai/tool": {"name": "web_search"}}}, {"type": "WebSearch", "citations": ["https://example.org/"]}),
    ("minimax", {"name": "web_search"}, {"content": [{"text": json.dumps({"code": 0, "results": [
        {"href": "https://example.org/", "title": "Native"}
    ]})}]}),
])
def test_acp_search_requires_tool_identity_and_success(tmp_path, agent, identity, output):
    task = Task(task_id="test", session_id="sess", agent=agent, cwd=str(tmp_path), message="test", native_search=True)
    collector = AcpNativeSearch(task, "native-sess")
    update = {"toolCallId": "search", "status": "completed", "rawOutput": output}
    collector.consume({**update, "toolCallId": "unverified", "title": "web_search"})
    collector.tool_name({"toolCallId": "search", **identity})
    collector.consume({**update, "status": "failed"})
    assert not task.native_calls and not task.sources
    collector.consume(update)
    collector.consume(update)
    assert task.native_calls == {"search": "web_search"}
    assert [(source.url, source.tool_call_id) for source in task.sources] == [("https://example.org/", "search")]


def test_codex_search_override_is_per_capability(tmp_path, monkeypatch):
    cfg = search_config().get("codex")
    cfg.config_overrides = ['web_search="cached"']
    adapter = CodexAdapter(cfg, tmp_path)
    monkeypatch.setattr(adapter, "_base_cmd", lambda env: ["codex"])
    session = Session(session_id="sess", agent="codex", cwd=str(tmp_path))
    task = Task(task_id="test", session_id="sess", agent="codex", cwd=str(tmp_path), message="test")
    ordinary = adapter._build_cmd(session, task)
    task.native_search = True
    search = adapter._build_cmd(session, task)
    assert 'web_search="live"' not in ordinary
    assert search.index('web_search="cached"') < search.index('web_search="live"')
    assert cfg.config_overrides == ['web_search="cached"']


async def test_codex_search_replay_continuation_and_sources_survive_restart(tmp_path, monkeypatch):
    script = tmp_path / "offline_search.py"
    script.write_text(
        "import json, os, pathlib, sys\n"
        "assert 'AGENT_BRIDGE_HUB_TOKEN' not in os.environ\n"
        "assert os.environ['AGENT_BRIDGE_HUB'] == 'off'\n"
        "assert 'open at least one relevant result page' in sys.stdin.read()\n"
        "with pathlib.Path('search-submissions.log').open('a') as log:\n"
        "    log.write(('resume' if 'resume' in sys.argv else 'new') + '\\n')\n"
        "events = [\n"
        "  {'type':'thread.started','thread_id':'thread_search'},\n"
        "  {'type':'item.completed','item':{'type':'web_search','id':'search_native',\n"
        "   'action':{'type':'search','query':'offline search'}}},\n"
        "  {'type':'item.completed','item':{'type':'web_search','id':'native_page',\n"
        "   'action':{'type':'open_page','url':'https://example.org/native'}}},\n"
        "  {'type':'item.completed','item':{'type':'agent_message','text':'Offline search findings'}},\n"
        "  {'type':'turn.completed'},\n"
        "]\n"
        "for event in events: print(json.dumps(event), flush=True)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_BRIDGE_HUB_TOKEN", "not-a-real-token")
    monkeypatch.setattr(CodexAdapter, "_base_cmd", lambda self, env=None: [sys.executable, str(script)])
    config = search_config()
    home = tmp_path / "bridge"
    registry = Registry.create(home, config, runtime_context="coordinator")
    args = search_args(tmp_path)
    await registry.start()
    try:
        service = BridgeService(registry)
        call = await service.call_capability(**args)
        result = await service.get_capability_result(call["call_id"], wait_sec=5)
        assert result["status"] == "completed"
        assert not result["pending"] and result["persisted"]
        assert result["sources"] == [{
            "title": "https://example.org/native", "url": "https://example.org/native", "tool_call_id": "native_page",
        }]
        assert result["native_calls"] == {"search_native": "web_search", "native_page": "web_fetch"}
        assert result["result_text"] == "Offline search findings"
        assert registry.list_sessions() == []
        sid = registry.tasks[call["call_id"]].session_id
        assert not (await service.dispatch_task("codex", "ordinary", str(tmp_path), session_id=sid,
                                               user_requested=True))["ok"]
        assert not (await service.call_capability(**(args | {"prompt": "changed"})))["ok"]
    finally:
        await registry.stop()

    registry = Registry.create(home, config, runtime_context="coordinator")
    await registry.start()
    try:
        service = BridgeService(registry)
        replay = await service.call_capability(**args)
        assert replay["reused"] and replay["call_id"] == call["call_id"]
        recovered = await service.get_capability_result(replay["call_id"])
        assert recovered["sources"] == result["sources"]
        followup = await service.call_capability(**(
            args | {"request_id": str(uuid.uuid4()), "previous_call_id": replay["call_id"]}
        ))
        final = await service.get_capability_result(followup["call_id"], wait_sec=5)
        assert final["status"] == "completed" and not final["pending"]
        assert registry.tasks[followup["call_id"]].session_id == sid
        assert (tmp_path / "search-submissions.log").read_text().splitlines() == ["new", "resume"]
    finally:
        await registry.stop()


async def test_native_failed_state_flush_submits_zero_calls(tmp_path, monkeypatch):
    registry = Registry.create(tmp_path / "bridge", search_config(), runtime_context="coordinator")
    await registry.start()
    worker = AsyncMock()
    monkeypatch.setattr(CodexAdapter, "run_turn", worker)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(registry, "flush_state", AsyncMock(return_value=False))
            service = BridgeService(registry)
            call = await service.call_capability(**search_args(tmp_path))
            result = await service.get_capability_result(call["call_id"], wait_sec=5)
            assert not call["persisted"]
            assert result["status"] == "failed" and not result["pending"]
            assert "not submitted" in result["error"]
            worker.assert_not_awaited()
    finally:
        await registry.stop()


@pytest.mark.parametrize(("action", "structured_results", "expected_status"), [
    ("search", True, "completed"),
    ("search", False, "failed"),
    ("open_page", False, "failed"),
])
async def test_pending_through_worker_cleanup_and_requires_native_sources(
    tmp_path, monkeypatch, action, structured_results, expected_status,
):
    registry = Registry.create(tmp_path / "bridge", search_config(), runtime_context="coordinator")
    entered = asyncio.Event()
    gate = asyncio.Event()

    async def run_turn(self, session, task):
        observe_codex_search(task, {
            "id": "tool", "action": {"type": action, "url": "https://example.org/"},
            "results": [{"type": "text_result", "url": "https://example.org/"}] if structured_results else [],
        })
        session.native_session_id = "thread"
        return TurnResult(text="Prose claims a source at https://example.org/")

    async def shutdown(self, session):
        entered.set()
        await gate.wait()

    monkeypatch.setattr(CodexAdapter, "run_turn", run_turn)
    monkeypatch.setattr(CodexAdapter, "shutdown", shutdown)
    await registry.start()
    try:
        service = BridgeService(registry)
        args = search_args(tmp_path)
        call = await service.call_capability(**args)
        await asyncio.wait_for(entered.wait(), timeout=5)
        result = await service.get_capability_result(call["call_id"])
        assert result["pending"]
        followup = args | {"request_id": str(uuid.uuid4()), "previous_call_id": call["call_id"]}
        assert not (await service.call_capability(**followup))["ok"]
        gate.set()
        result = await service.get_capability_result(call["call_id"], wait_sec=5)
        assert not result["pending"]
        assert result["status"] == expected_status
    finally:
        gate.set()
        await registry.stop()


async def test_cancel_keeps_search_pending_until_worker_closes(tmp_path, monkeypatch):
    registry = Registry.create(tmp_path / "bridge", search_config(), runtime_context="coordinator")
    started = asyncio.Event()
    closing = asyncio.Event()
    release = asyncio.Event()

    async def run_turn(self, session, task):
        started.set()
        await asyncio.Future()

    async def shutdown(self, session):
        closing.set()
        await release.wait()

    monkeypatch.setattr(CodexAdapter, "run_turn", run_turn)
    monkeypatch.setattr(CodexAdapter, "shutdown", shutdown)
    await registry.start()
    cancelling = None
    try:
        service = BridgeService(registry)
        call = await service.call_capability(**search_args(tmp_path))
        await asyncio.wait_for(started.wait(), timeout=5)
        cancelling = asyncio.create_task(service.cancel_capability(call["call_id"]))
        await asyncio.wait_for(closing.wait(), timeout=5)
        result = await service.get_capability_result(call["call_id"])
        assert result["status"] == "cancelled" and result["pending"]
        release.set()
        result = await cancelling
        assert result["status"] == "cancelled" and not result["pending"]
        assert result["remote_cancellation_supported"] is False
    finally:
        release.set()
        if cancelling is not None:
            await cancelling
        await registry.stop()
