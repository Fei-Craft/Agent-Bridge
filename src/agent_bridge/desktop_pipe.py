from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import msvcrt
import os
import sys
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pywintypes
import win32api
import win32con
import win32file
import win32pipe
import win32security

from agent_bridge.desktop_ipc import FRAME_LIMIT, DesktopIPC

PIPE_REJECT_REMOTE_CLIENTS = 0x8
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
SECURITY_IDENTIFICATION = 0x00010000
SECURITY_SQOS_PRESENT = 0x00100000
BUFFER_SIZE = 65536


def _user_sid() -> Any:
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()


def _address(home: Path, sid: Any) -> str:
    material = os.path.normcase(str(home.resolve())) + ":" + win32security.ConvertSidToStringSid(sid)
    return "\\\\.\\pipe\\agent-bridge-dsh-" + hashlib.sha256(material.encode()).hexdigest()


def _private_pipe(address: str, sid: Any) -> Any:
    acl = win32security.ACL()
    acl.AddAccessAllowedAce(win32security.ACL_REVISION, win32con.GENERIC_ALL, sid)
    security = pywintypes.SECURITY_ATTRIBUTES()
    security.SECURITY_DESCRIPTOR.SetSecurityDescriptorDacl(True, acl, False)
    security.SECURITY_DESCRIPTOR.SetSecurityDescriptorOwner(sid, False)
    try:
        return win32pipe.CreateNamedPipe(
            address, win32pipe.PIPE_ACCESS_DUPLEX | win32con.FILE_FLAG_OVERLAPPED | FILE_FLAG_FIRST_PIPE_INSTANCE,
            PIPE_REJECT_REMOTE_CLIENTS, 1, BUFFER_SIZE, BUFFER_SIZE, 0, security,
        )
    except pywintypes.error as exc:
        raise OSError("Could not create a current-user Desktop named pipe") from exc


@contextlib.asynccontextmanager
async def serve_pipe(home: Path, ipc: DesktopIPC) -> AsyncIterator[None]:
    if sys.platform != "win32":
        raise OSError("Desktop named pipes are only available on Windows")
    pipe_handle = importlib.import_module("asyncio.windows_utils").PipeHandle

    loop = cast(Any, asyncio.get_running_loop())
    if not hasattr(loop, "_proactor"):
        raise OSError("Desktop named pipe requires the Windows Proactor event loop")
    sid = _user_sid()
    address = _address(home, sid)
    first = _private_pipe(address, sid)

    async def accept() -> None:
        raw = first
        while True:
            pipe = pipe_handle(raw.Detach())
            transport = None
            try:
                await loop._proactor.accept_pipe(pipe)
                reader = asyncio.StreamReader(limit=FRAME_LIMIT)
                protocol = asyncio.StreamReaderProtocol(reader)
                transport = loop._make_duplex_pipe_transport(pipe, protocol)
                writer = asyncio.StreamWriter(transport, protocol, reader, loop)
                await ipc.attach(reader, writer)
            finally:
                if transport is not None:
                    transport.close()
                else:
                    pipe.close()
            raw = _private_pipe(address, sid)

    task = asyncio.create_task(accept())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        first.Close()


def relay_stdio(home: Path) -> None:
    sid = _user_sid()
    try:
        handle = win32file.CreateFile(
            _address(home, sid), win32con.GENERIC_READ | win32con.GENERIC_WRITE | win32con.READ_CONTROL,
            0, None, win32con.OPEN_EXISTING, SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION, None,
        )
    except pywintypes.error:
        return
    try:
        descriptor = win32security.GetSecurityInfo(
            handle, win32security.SE_KERNEL_OBJECT, win32security.OWNER_SECURITY_INFORMATION,
        )
        if descriptor.GetSecurityDescriptorOwner() != sid:
            return
        stdin = sys.stdin.fileno()
        if sys.platform == "win32":
            msvcrt.setmode(stdin, os.O_BINARY)

        def upstream() -> None:
            with contextlib.suppress(OSError, pywintypes.error):
                while chunk := os.read(stdin, BUFFER_SIZE):
                    win32file.WriteFile(handle, chunk)
            os._exit(0)

        threading.Thread(target=upstream, daemon=True).start()
        with contextlib.suppress(OSError, pywintypes.error):
            while True:
                _, data = win32file.ReadFile(handle, BUFFER_SIZE)
                if not data:
                    break
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
    finally:
        handle.Close()
