from __future__ import annotations

import asyncio
import hashlib
import secrets

from agent_bridge.secrets import forget, remember


class WorkerTokens:
    def __init__(self) -> None:
        self.scopes: dict[str, str] = {}
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.active: set[str] = set()
        self.closed = False

    def issue(self, session_id: str) -> str:
        if self.closed:
            raise RuntimeError('hub shutting down')
        self.revoke(session_id)
        token = remember('abw_' + secrets.token_urlsafe(32))
        self.scopes[hashlib.sha256(token.encode()).hexdigest()] = session_id
        return token

    def authenticate(self, token: str) -> str | None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        session_id = self.scopes.get(digest)
        if session_id is None:
            return None
        process = self.processes.get(session_id)
        if process is not None and process.returncode is not None:
            self.revoke(session_id)
            return None
        return session_id if session_id in self.active and not self.closed else None

    def revoke(self, session_id: str) -> None:
        for digest, sid in list(self.scopes.items()):
            if sid == session_id:
                del self.scopes[digest]
                forget(digest)
        self.processes.pop(session_id, None)

    def close(self) -> None:
        self.closed = True
        for digest in self.scopes:
            forget(digest)
        self.scopes.clear()
        self.processes.clear()
        self.active.clear()
