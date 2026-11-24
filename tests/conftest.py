from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def memory_keyring(monkeypatch):
    from agent_bridge import secrets

    class MemoryKeyring:
        agent_bridge_memory_only = True

        def __init__(self):
            self.values = {}

        def get_password(self, service, username):
            return self.values.get((service, username))

        def set_password(self, service, username, password):
            self.values[service, username] = password

        def delete_password(self, service, username):
            self.values.pop((service, username), None)

    backend = MemoryKeyring()
    secrets._memory.clear()
    secrets._known.clear()
    secrets._warned.clear()
    monkeypatch.setattr(secrets.keyring, 'get_keyring', lambda: backend)
    yield backend
    secrets._memory.clear()
    secrets._known.clear()
    secrets._warned.clear()


@pytest.fixture
def bridge_home(tmp_path, monkeypatch):
    home = tmp_path / "bridge-home"
    monkeypatch.setenv("AGENT_BRIDGE_HOME", str(home))
    monkeypatch.setenv("AGENT_BRIDGE_ENABLE_FAKE", "1")
    return home
