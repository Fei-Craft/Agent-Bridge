from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import stat
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
FRAME_LIMIT = 16 * 1024 * 1024


class DesktopUnavailable(RuntimeError):
    pass


class DesktopIPC:
    def __init__(self) -> None:
        self._peer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Queue[dict[str, Any] | None]] = {}

    @property
    def connected(self) -> bool:
        return self._peer is not None and not self._peer.is_closing()

    async def attach(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        owns_peer = False
        try:
            hello = json.loads(await asyncio.wait_for(reader.readline(), 3))
            if self._peer is not None or hello != {"dsh_ipc": 1}:
                return
            self._peer = writer
            owns_peer = True
            while line := await reader.readline():
                frame = json.loads(line)
                if not isinstance(frame, dict) or not isinstance(frame.get("id"), str):
                    break
                if queue := self._pending.get(frame["id"]):
                    queue.put_nowait(frame)
        except (OSError, ValueError, TimeoutError):
            pass
        finally:
            writer.close()
            if owns_peer:
                self._peer = None
                for queue in self._pending.values():
                    queue.put_nowait(None)
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    @contextlib.asynccontextmanager
    async def request(self, operation: str, args: dict[str, Any]) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
        peer = self._peer
        if peer is None or peer.is_closing():
            raise DesktopUnavailable("DeepSeek Harness Desktop connector is unavailable")
        request_id = uuid.uuid4().hex
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._pending[request_id] = queue

        async def read() -> AsyncIterator[dict[str, Any]]:
            while True:
                frame = await queue.get()
                if frame is None:
                    raise RuntimeError("DeepSeek Harness Desktop disconnected after the request was sent")
                if frame.get("type") == "error":
                    raise RuntimeError(f"DeepSeek Harness Desktop rejected {operation}")
                if frame.get("type") == "end":
                    return
                value = frame.get("value")
                if frame.get("type") != "value" or not isinstance(value, dict):
                    raise DesktopUnavailable("DeepSeek Harness Desktop sent an invalid IPC frame")
                yield value

        try:
            payload = json.dumps({"id": request_id, "operation": operation, "args": args}).encode()
            if len(payload) >= FRAME_LIMIT:
                raise ValueError("Desktop IPC request exceeds the frame limit")
            peer.write(payload + b"\n")
            await peer.drain()
            yield read()
        except OSError:
            raise RuntimeError("DeepSeek Harness Desktop request delivery could not be confirmed") from None
        finally:
            self._pending.pop(request_id)
            if not peer.is_closing():
                with contextlib.suppress(OSError):
                    peer.write(json.dumps({"id": request_id, "operation": "close"}).encode() + b"\n")

    async def call(self, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        async with asyncio.timeout(10), self.request(operation, args) as frames:
            value = await anext(frames, None)
            if value is None:
                raise RuntimeError("DeepSeek Harness Desktop returned no response")
            return value


@contextlib.asynccontextmanager
async def serve_desktop(home: Path, ipc: DesktopIPC) -> AsyncIterator[None]:
    async with contextlib.AsyncExitStack() as stack:
        try:
            if os.name == "nt":
                from agent_bridge.desktop_pipe import serve_pipe

                await stack.enter_async_context(serve_pipe(home, ipc))
            else:
                await stack.enter_async_context(_serve_socket(home, ipc))
        except OSError as exc:
            log.warning("DSH Desktop IPC unavailable (%s); ordinary new DSH tasks can use ACP", type(exc).__name__)
        try:
            yield
        finally:
            if ipc._peer is not None:
                ipc._peer.close()


@contextlib.asynccontextmanager
async def _serve_socket(home: Path, ipc: DesktopIPC) -> AsyncIterator[None]:
    import fcntl

    directory = home / "dsh-ipc"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise PermissionError("DSH IPC directory must be owned by the current user with mode 0700")
    info = home.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise PermissionError("Bridge home must not be writable by other users")
    lock = os.open(directory / "lock", os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        endpoint = directory / "desktop.sock"
        if endpoint.is_socket() and not endpoint.is_symlink():
            endpoint.unlink()
        server = await asyncio.start_unix_server(ipc.attach, endpoint, limit=FRAME_LIMIT, start_serving=False)
        try:
            endpoint.chmod(0o600)
            await server.start_serving()
            yield
        finally:
            server.close()
            await server.wait_closed()
            endpoint.unlink(missing_ok=True)
    finally:
        os.close(lock)
