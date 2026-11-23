from __future__ import annotations

import asyncio
import os
import shutil
import socket
import stat
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx2
import pytest

from agent_bridge.adapters import dsh_desktop
from agent_bridge.adapters.fake import FakeAdapter
from agent_bridge.desktop_ipc import DesktopIPC
from agent_bridge.hub import create_app
from agent_bridge.registry import Registry


async def _wait_for_connection(ipc: DesktopIPC, connected: bool) -> None:
    for _ in range(500):
        if ipc.connected == connected:
            return
        await asyncio.sleep(0.02)
    pytest.fail(f"Desktop IPC did not reach connected={connected}")


@pytest.mark.skipif(os.name == "nt", reason="stale Unix domain socket")
async def test_connector_retries_stale_socket():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to run the Desktop connector")
    fixture = Path(__file__).with_name("dsh_desktop_fixture.mjs")
    with TemporaryDirectory(prefix="ab-dsh-") as directory:
        home = Path(directory).resolve()
        ipc_dir = home / "dsh-ipc"
        ipc_dir.mkdir(mode=0o700)
        endpoint = ipc_dir / "desktop.sock"
        with socket.socket(socket.AF_UNIX) as stale:
            stale.bind(str(endpoint))
        endpoint.chmod(0o600)
        proc = await asyncio.create_subprocess_exec(node, str(fixture), str(home))
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(proc.wait(), 1.5)
            registry = Registry.create(home)
            app = create_app(registry, token="fixture-bearer")
            async with app.router.lifespan_context(app):
                await _wait_for_connection(registry.desktop, True)
        finally:
            if proc.returncode is None:
                proc.terminate()
                await proc.wait()


async def test_hub_lifecycle_through_desktop_connector(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to run the Desktop connector")
    fixture = Path(__file__).with_name("dsh_desktop_fixture.mjs")
    with TemporaryDirectory(prefix="ab-dsh-") as directory:
        home = Path(directory).resolve()
        registry = Registry.create(home)
        registry.config.coordinator.mode = "auto"
        app = create_app(registry, token="fixture-bearer")
        async with app.router.lifespan_context(app), httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": "Bearer fixture-bearer"},
        ) as client:
            if os.name != "nt":
                endpoint = home / "dsh-ipc" / "desktop.sock"
                assert stat.S_IMODE(endpoint.parent.stat().st_mode) == 0o700
                assert stat.S_IMODE(endpoint.stat().st_mode) == 0o600
            executable = str(Path(sys.executable).with_name("agent-bridge.exe"))
            proc = await asyncio.create_subprocess_exec(node, str(fixture), str(home), executable)
            try:
                await _wait_for_connection(registry.desktop, True)
                assert (await client.get('/v1/baselines', headers={"Authorization": "invalid"})).status_code == 401
                body = {"agent": "dsh", "cwd": str(tmp_path), "message": "seed"}
                source = (await client.post('/v1/agents/dsh/invoke', json=body)).json()
                assert source["result_text"] == "seed", source
                saved = (await client.post('/v1/baselines', json={
                    "session_id": source["session_id"], "name": "Baseline",
                })).json()
                baseline_id = saved["baseline"]["baseline_id"]
                assert (await client.put('/v1/baselines/default', json={
                    "cwd": str(tmp_path), "baseline_id": baseline_id,
                })).json()["ok"]
                result = (await client.post('/v1/agents/dsh/invoke', json={
                    **body, "message": "child", "fork_label": "Branch",
                })).json()
                assert result["result_text"] == "seed child", result
                assert result["baseline_id"] == baseline_id
                assert result["fork_label"] == "Branch"
                listed = (await client.get('/v1/baselines', params={"cwd": str(tmp_path)})).json()
                assert listed["default_baseline_id"] == baseline_id
                assert (await client.put('/v1/baselines/default', json={
                    "cwd": str(tmp_path), "baseline_id": None,
                })).json()["default_baseline_id"] is None
                assert not (home / "dsh-desktop.json").exists()
            finally:
                proc.terminate()
                await proc.wait()
            await _wait_for_connection(registry.desktop, False)
            refused = (await client.post('/v1/tasks', json={**body, "baseline": baseline_id})).json()
            assert "disconnected" in refused["error"]
        if os.name != "nt":
            assert not endpoint.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows named pipe ACL")
def test_windows_desktop_pipe_acl(tmp_path):
    import win32security

    from agent_bridge.desktop_pipe import _address, _private_pipe, _user_sid

    sid = _user_sid()
    pipe = _private_pipe(_address(tmp_path, sid), sid)
    try:
        descriptor = win32security.GetSecurityInfo(
            pipe, win32security.SE_KERNEL_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.OWNER_SECURITY_INFORMATION,
        )
        assert descriptor.GetSecurityDescriptorOwner() == sid
        acl = descriptor.GetSecurityDescriptorDacl()
        assert acl.GetAceCount() == 1 and acl.GetAce(0)[2] == sid
    finally:
        pipe.Close()


@pytest.mark.asyncio
async def test_disconnected_desktop_falls_back_to_acp_and_disables_fork(monkeypatch, tmp_path):
    home = tmp_path / "bridge"
    home.mkdir()
    (home / "agents.toml").write_text('[agents.dsh]\nprotocol = "acp"\ncommand = ["dsh"]\n', encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    registry = Registry.create(home)
    monkeypatch.setattr(registry, "_adapter_for", lambda session: FakeAdapter(registry.config.get("dsh"), home))
    await registry.start()
    try:
        result = await registry.dispatch_task("dsh", "ordinary", str(project), baseline="empty")
        assert registry.sessions[result["session_id"]].backend is None
        assert (await registry.wait_task(result["task_id"], timeout_sec=5))["status"] == "completed"
        with pytest.raises(ValueError, match="Desktop connection is required"):
            await registry.create_baseline(result["session_id"], "baseline")
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_host_drop_before_create_retries_ordinary_turn_on_acp(monkeypatch, tmp_path):
    home = tmp_path / "bridge"
    home.mkdir()
    (home / "agents.toml").write_text('[agents.dsh]\nprotocol = "acp"\ncommand = ["dsh"]\n', encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(DesktopIPC, "connected", property(lambda self: True))
    registry = Registry.create(home)

    class DroppedDesktop(FakeAdapter):
        async def run_turn(self, session, task):
            raise dsh_desktop.DesktopUnavailable("Host down")

    def adapter_for(session):
        return (DroppedDesktop if session.backend == "desktop" else FakeAdapter)(registry.config.get("dsh"), home)

    monkeypatch.setattr(registry, "_adapter_for", adapter_for)
    await registry.start()
    try:
        sent = await registry.dispatch_task("dsh", "ordinary", str(project), baseline="empty")
        result = await registry.wait_task(sent["task_id"], timeout_sec=5)
        assert result["status"] == "completed"
        assert result["backend"] == "acp"
        assert "used ACP" in result["warnings"][0]
    finally:
        await registry.stop()
