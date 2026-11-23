from pathlib import Path

from agent_bridge.adapters.acp import AcpAdapter
from agent_bridge.adapters.antigravity import AgyAdapter
from agent_bridge.adapters.base import Adapter
from agent_bridge.adapters.cli import CliAdapter
from agent_bridge.adapters.cloud import CloudAdapter
from agent_bridge.adapters.codex import CodexAdapter
from agent_bridge.adapters.dsh_desktop import DesktopAdapter
from agent_bridge.adapters.fake import FakeAdapter
from agent_bridge.config import AgentConfig, EnvConfig
from agent_bridge.desktop_ipc import DesktopIPC


def build_adapter(cfg: AgentConfig, home: Path, env_config: EnvConfig | None = None,
                  *, backend: str | None = None, desktop: DesktopIPC | None = None) -> Adapter:
    if cfg.protocol == 'cloud':
        return CloudAdapter(cfg, home, env_config)
    if cfg.protocol == 'cli':
        return CliAdapter(cfg, home, env_config)
    if cfg.name == "dsh" and backend == "desktop":
        assert desktop is not None
        return DesktopAdapter(cfg, home, env_config, desktop)
    if cfg.protocol == "fake":
        return FakeAdapter(cfg, home, env_config)
    if cfg.protocol == "agy":
        return AgyAdapter(cfg, home, env_config)
    if cfg.protocol == "codex":
        return CodexAdapter(cfg, home, env_config)
    if cfg.protocol == "acp":
        return AcpAdapter(cfg, home, env_config)
    raise ValueError(f"unsupported protocol {cfg.protocol!r} for agent {cfg.name}")
