from __future__ import annotations

import ipaddress
import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, model_validator

from agent_bridge.paths import bridge_home, bundled_agents_toml
from agent_bridge.persist import atomic_write_text

DEFAULT_INHERIT_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "GROK_WEB_FETCH_PROXY",
    "AGENT_BRIDGE_HTTP_PROXY",
    "AGENT_BRIDGE_HTTPS_PROXY",
    "AGENT_BRIDGE_NO_PROXY",
    "DSH_HOME",
    "KIMI_CODE_HOME",
    "KIMI_SHELL_PATH",
    "KIMI_API_KEY",
    "KIMI_BASE_URL",
    "KIMI_CODE_BASE_URL",
    "KIMI_CODE_OAUTH_HOST",
    "KIMI_OAUTH_HOST",
    "KIMI_MODEL_BASE_URL",
    "KIMI_MODEL_NAME",
    "KIMI_MODEL_API_KEY",
    "KIMI_MODEL_PROVIDER_TYPE",
    "MOONSHOT_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENCODE_API_KEY",
    "XAI_API_KEY",
    "GROK_API_KEY",
    "GROK_HOME",
    "GROK_AUTH_PATH",
    "GROK_CLI_CHAT_PROXY_BASE_URL",
    "GROK_XAI_API_BASE_URL",
    "GROK_MODELS_BASE_URL",
    "GROK_MODELS_LIST_URL",
    "GROK_OIDC_ISSUER",
    "GROK_OIDC_CLIENT_ID",
    "GROK_OAUTH2_ISSUER",
    "GROK_OAUTH2_CLIENT_ID",
    "GROK_LOCAL_AUTH",
    "GROK_AUTH_PROVIDER_COMMAND",
    "GROK_MANAGED_CONFIG_URL",
    "GROK_CODE_XAI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "CODEX_API_KEY",
    "CODEX_CLI_PATH",
    "CODEX_HOME",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
    "OPENROUTER_API_KEY",
    "WINDSURF_API_KEY",
    "ZCODE_HOME",
    "ZCODE_BIN",
    "ZCODE_NODE",
    "ZCODE_MODEL",
    "ZCODE_PROVIDER",
    "ZCODE_BASE_URL",
    "ZCODE_ACP_MODE",
    "ZCODE_ACP_DEBUG",
    "MINIMAX_DATA_DIR",
    "MAVIS_DATA_DIR",
    "MAVIS_REGION",
    "MAVIS_BUILD_ENV",
    "MAVIS_PROFILE",
    "MCODE_CONFIG_DIR",
    "MCODE_API_BASE_URL",
    "MCODE_AUTH_BASE_URL",
    "MCODE_REGION",
    "MCODE_CLIENT_ID",
    "MCODE_SCOPE",
    "MCODE_AUTH_PROVIDER",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "CLOUDFLARE_API_TOKEN",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "PYTHONUTF8",
    "PYTHONIOENCODING",
    "LANG",
    "LC_ALL",
)


class CliConfig(BaseModel):
    command: list[str] = Field(default_factory=list)
    prompt_args: list[str] = Field(default_factory=lambda: ['{prompt}'])
    prompt_stdin: bool = False
    resume_args: list[str] = Field(default_factory=list)
    model_args: list[str] = Field(default_factory=list)
    effort_args: list[str] = Field(default_factory=list)
    extra_args: list[str] = Field(default_factory=list)
    parser: Literal['text', 'jsonl', 'claude', 'opencode', 'cursor'] = 'text'


class CloudConfig(BaseModel):
    provider: Literal['cursor', 'devin']
    api_key_env: str = ''
    base_url: str = ''
    org_id: str | None = None
    org_id_env: str = 'DEVIN_ORG_ID'
    repo: str | None = None
    ref: str | None = None
    auto_create_pr: bool = False
    model: str | None = None
    devin_mode: str | None = None
    max_acu_limit: int | None = Field(default=None, gt=0)
    poll_interval_sec: float = Field(default=5, gt=0)

    @model_validator(mode='after')
    def defaults(self) -> CloudConfig:
        self.api_key_env = self.api_key_env or f'{self.provider.upper()}_API_KEY'
        self.base_url = (self.base_url or {'cursor': 'https://api.cursor.com',
                                         'devin': 'https://api.devin.ai'}[self.provider]).rstrip('/')
        url = urlsplit(self.base_url)
        if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('cloud base_url must be HTTP(S), without credentials, query or fragment')
        return self


class AgentConfig(BaseModel):
    name: str
    protocol: str
    command: list[str] = Field(default_factory=list)
    cloud: CloudConfig | None = None
    cli: CliConfig = Field(default_factory=CliConfig)
    fallback_commands: list[list[str]] = Field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    session_meta: dict[str, Any] = Field(default_factory=dict)
    revivable: bool = False
    idle_unload_sec: int = 0
    stall_timeout_sec: int = Field(default=1800, ge=0)
    # Hard cap on one turn's wall-clock length (0 = disabled). Catches a
    # worker that stays chatty forever without ever going silent —
    # stall_timeout_sec alone cannot see that case (H-16).
    turn_timeout_sec: int = Field(default=0, ge=0)
    print_timeout: str = "120m"
    # How Bridge answers worker permission prompts when no human is present:
    # "allow_once" grants each request for that call only (default, least
    # privilege), "allow_always" persists the grant inside the worker,
    # "deny" cancels every request.
    permission_policy: str = "allow_once"
    # Codex only: each item is passed as `codex exec -c <item>` (raw TOML
    # key=value). No key validation, no filtering, user config.toml is
    # never read.
    config_overrides: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def validate_protocol(self) -> AgentConfig:
        if self.protocol == 'cloud':
            if self.cloud is None:
                raise ValueError('cloud protocol requires a cloud configuration')
            if 'stall_timeout_sec' not in self.model_fields_set:
                self.stall_timeout_sec = 0
            if 'turn_timeout_sec' not in self.model_fields_set:
                self.turn_timeout_sec = 86400
        elif not self.command:
            raise ValueError('command is required for local agents')
        return self


class EnvConfig(BaseModel):
    """How Bridge rebuilds the environment Codex (or another MCP host) stripped."""

    inherit: list[str] = Field(default_factory=lambda: list(DEFAULT_INHERIT_KEYS))
    discover_proxy: bool = True
    set: dict[str, str] = Field(default_factory=dict)
    proxy_url: str | None = None
    no_proxy: str | None = None
    # Glob patterns of env keys a worker subprocess must never see; keys
    # explicitly assigned via ``set``/agent ``env`` always survive.
    deny: list[str] = Field(default_factory=list)


class ServerConfig(BaseModel):
    """Process-level server behavior (optional idle self-exit)."""

    idle_exit_sec: int = 0


class ModelsConfig(BaseModel):
    """Cache lifetime and timeout for on-demand model discovery."""

    cache_sec: float = Field(default=3600.0, ge=0)
    timeout_sec: float = Field(default=30.0, gt=0)


class QuotaConfig(BaseModel):
    """Remaining-quota lookup that rides along with ``list_agents``.

    ``timeout_sec`` bounds one worker's lookup; ``cache_sec`` is how long a
    reading is reused before the CLI is asked again. ``experimental`` unlocks
    the workers whose only quota source is the private endpoint their own
    ``/usage`` command calls (Grok Build, Claude Code).
    """

    enabled: bool = True
    timeout_sec: float = Field(default=4.0, gt=0)
    cache_sec: float = Field(default=300.0, ge=0)
    experimental: bool = False


COORDINATOR_MODES = ("manual", "auto", "eager")
# The notes that inspired this used safe/yolo; yolo already means
# "auto-approve tool calls" for Kimi and Grok, so the canonical names differ.
_MODE_ALIASES = {"safe": "manual", "yolo": "eager"}

# Sent back on every list_agents call so the coordinator re-reads the policy
# at the moment it matters, instead of relying on rules it saw turns ago.
COORDINATOR_MODE_HINTS = {
    "manual": (
        "dispatch only when the user explicitly asked for a worker; "
        "dispatch_task is blocked unless user_requested=true"
    ),
    "auto": "dispatch at your own judgment; weigh dispatch overhead against doing it yourself",
    "eager": (
        "prefer dispatching workers over doing multi-step work yourself; "
        "you keep architecture decisions and acceptance"
    ),
}


def normalize_coordinator_mode(raw: str | None, *, strict: bool = False) -> str:
    text = (raw or "").strip().lower()
    text = _MODE_ALIASES.get(text, text)
    if text in COORDINATOR_MODES:
        return text
    if strict:
        raise ValueError(
            f"unknown coordinator mode {raw!r}; use one of "
            f"{', '.join(COORDINATOR_MODES)} (aliases: safe, yolo)"
        )
    return "auto"


class CoordinatorConfig(BaseModel):
    """How eagerly the coordinator should dispatch, plus user routing preferences.

    Bridge does not interpret ``instructions``; it relays the text through
    ``list_agents`` for the coordinator LLM to read. ``mode`` is the only key
    Bridge acts on itself: ``manual`` hard-blocks dispatch_task without
    ``user_requested=true``.
    """

    mode: str = "auto"
    instructions: str = ""


class RemoteConfig(BaseModel):
    enabled: bool = False
    host: str = '127.0.0.1'
    port: int = Field(default=8766, ge=1, le=65535)
    remote_callable_agents: list[str] = Field(default_factory=list)
    remote_workspaces: dict[str, str] = Field(default_factory=dict)
    token_ttl_sec: float | None = Field(default=None, gt=0)
    result_fetch_grace_sec: float = Field(default=30, ge=0, le=300)
    max_calls_per_token: int = Field(default=16, ge=1)
    max_concurrent_per_token: int = Field(default=2, ge=1)
    rate_limit_per_min: int = Field(default=60, ge=1)
    max_body_bytes: int = Field(default=65536, ge=1)
    require_approval: bool = False
    public_url: str = ''
    access_note: str = ''
    require_cf_access: bool = False

    @model_validator(mode='after')
    def validate_remote(self) -> RemoteConfig:
        if self.host == 'localhost':
            self.host = '127.0.0.1'
        try:
            if not ipaddress.ip_address(self.host).is_loopback:
                raise ValueError('remote gateway must bind a loopback address')
        except ValueError:
            raise ValueError('remote gateway must bind a loopback address') from None
        for name, path in self.remote_workspaces.items():
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name):
                raise ValueError('remote workspace names must be simple names, never paths')
            if not Path(path).expanduser().is_absolute():
                raise ValueError('remote workspace paths must be absolute')
        return self


class HubConfig(BaseModel):
    host: str = '127.0.0.1'
    port: int = Field(default=8765, ge=1, le=65535)
    max_depth: int = Field(default=3, ge=0)
    remote: RemoteConfig = Field(default_factory=RemoteConfig)

    @model_validator(mode='after')
    def separate_remote_port(self) -> HubConfig:
        if self.remote.enabled and self.remote.port == self.port:
            raise ValueError('remote gateway and admin must use separate ports')
        return self


class AppConfig(BaseModel):
    hub: HubConfig = Field(default_factory=HubConfig)
    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    env: EnvConfig = Field(default_factory=EnvConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    coordinator: CoordinatorConfig = Field(default_factory=CoordinatorConfig)
    quota: QuotaConfig = Field(default_factory=QuotaConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    warnings: list[str] = Field(default_factory=list)

    def get(self, name: str) -> AgentConfig:
        if name not in self.agents:
            known = ", ".join(sorted(self.agents)) or "(none)"
            raise KeyError(f"unknown agent {name!r}; known: {known}")
        return self.agents[name]


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _raw_agents(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    block = raw.get("agents") or {}
    out: dict[str, dict[str, Any]] = {}
    for name, spec in block.items():
        if isinstance(spec, dict):
            out[name] = dict(spec)
    return out


def _merge_agent_spec(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if value in (None, [], {}):
            continue
        if key in {"env", "cli", "cloud"} and isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key] = {**out[key], **value}
        else:
            out[key] = value
    return out


def _coerce_env(raw: dict[str, Any]) -> dict[str, Any]:
    block = raw.get("env")
    if not isinstance(block, dict):
        return {}
    raw_proxy = block.get("proxy")
    proxy: dict[str, Any] = raw_proxy if isinstance(raw_proxy, dict) else {}
    out: dict[str, Any] = {}
    if "inherit" in block and block["inherit"] is not None:
        raw_inherit = block["inherit"]
        # A bare string is one name, not an iterable of characters.
        out["inherit"] = [raw_inherit] if isinstance(raw_inherit, str) else [str(item) for item in raw_inherit]
    if "discover_proxy" in block:
        out["discover_proxy"] = bool(block["discover_proxy"])
    if isinstance(block.get("set"), dict):
        out["set"] = {str(key): str(value) for key, value in block["set"].items() if value is not None}
    url = proxy.get("url") or block.get("proxy_url")
    if url:
        out["proxy_url"] = str(url).strip()
    no_proxy = proxy.get("no_proxy") or proxy.get("no") or block.get("no_proxy")
    if no_proxy:
        out["no_proxy"] = str(no_proxy).strip()
    if "deny" in block and block["deny"] is not None:
        raw_deny = block["deny"]
        # A bare string is one glob, not an iterable of characters.
        out["deny"] = [raw_deny] if isinstance(raw_deny, str) else [str(item) for item in raw_deny]
    return out


def _merge_env(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if key == "set" and isinstance(value, dict):
            raw_set = out.get("set")
            current: dict[str, Any] = raw_set if isinstance(raw_set, dict) else {}
            out["set"] = {**current, **value}
        elif key == "deny" and isinstance(value, list):
            raw_deny = out.get("deny")
            deny_list: list[Any] = raw_deny if isinstance(raw_deny, list) else []
            out["deny"] = [*deny_list, *value]
        else:
            out[key] = value
    return out


def _coerce_server(raw: dict[str, Any]) -> dict[str, Any]:
    block = raw.get("server")
    if not isinstance(block, dict):
        return {}
    out: dict[str, Any] = {}
    if "idle_exit_sec" in block and block["idle_exit_sec"] is not None:
        out["idle_exit_sec"] = int(block["idle_exit_sec"])
    return out


def _coerce_quota(raw: dict[str, Any]) -> dict[str, Any]:
    block = raw.get("quota")
    if not isinstance(block, dict):
        return {}
    out: dict[str, Any] = {}
    if block.get("enabled") is not None:
        out["enabled"] = bool(block["enabled"])
    if block.get("timeout_sec") is not None:
        out["timeout_sec"] = float(block["timeout_sec"])
    if block.get("cache_sec") is not None:
        out["cache_sec"] = float(block["cache_sec"])
    if block.get("experimental") is not None:
        out["experimental"] = bool(block["experimental"])
    return out


def _coerce_coordinator(raw: dict[str, Any]) -> dict[str, Any]:
    block = raw.get("coordinator")
    if not isinstance(block, dict):
        return {}
    out: dict[str, Any] = {}
    if block.get("mode") is not None:
        out["mode"] = str(block["mode"])
    if block.get("instructions") is not None:
        out["instructions"] = str(block["instructions"]).strip()
    return out


def _toml_string(value: str) -> str:
    if not value:
        return '""'
    if "\n" not in value and '"' not in value and "\\" not in value:
        return f'"{value}"'
    # Literal multiline: no escape processing, so Windows paths survive.
    if "'''" not in value and not value.endswith("'"):
        return f"'''\n{value}\n'''"
    return json.dumps(value, ensure_ascii=False)


_COORDINATOR_HEADER = re.compile(r"(?m)^\[coordinator\][ \t]*(#[^\n]*)?\n?")
_NEXT_TABLE = re.compile(r"(?m)^\[")


def _trim_trailing_blank_and_comment_lines(text: str, start: int, end: int) -> int:
    lines = text[start:end].splitlines(keepends=True)
    while lines and (not lines[-1].strip() or lines[-1].lstrip().startswith("#")):
        lines.pop()
    return start + sum(len(line) for line in lines)


def _coordinator_span(text: str) -> tuple[int, int] | None:
    """Return the [coordinator] table span, skipping `[` lines inside strings."""
    header = _COORDINATOR_HEADER.search(text)
    if header is None:
        return None
    start = header.start()
    header_end = header.end()
    ends = [header_end + match.start() for match in _NEXT_TABLE.finditer(text[header_end:])]
    ends.append(len(text))
    for end in ends:
        block = text[start:end]
        rest = text[:start] + text[end:]
        try:
            parsed_block = tomllib.loads(block)
            parsed_rest = tomllib.loads(rest) if rest.strip() else {}
        except tomllib.TOMLDecodeError:
            continue
        if set(parsed_block) == {"coordinator"} and "coordinator" not in parsed_rest:
            trimmed = _trim_trailing_blank_and_comment_lines(text, start, end)
            try:
                parsed_trimmed = tomllib.loads(text[start:trimmed])
            except tomllib.TOMLDecodeError:
                return (start, end)
            if set(parsed_trimmed) == {"coordinator"}:
                return (start, trimmed)
            return (start, end)
    raise ValueError("could not isolate the [coordinator] table")


def write_coordinator_overlay(
    home: Path,
    *,
    mode: str | None = None,
    instructions: str | None = None,
) -> Path:
    """Rewrite only the [coordinator] section of the user overlay, keeping the rest.

    Values not passed here keep whatever the overlay already pins (or stay
    unset so repo defaults keep flowing). Raises on a malformed overlay instead
    of clobbering a file the user hand-edited.
    """
    path = home / "agents.toml"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    try:
        original = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path} is not valid TOML ({exc}); fix it by hand first") from exc

    existing = original.get("coordinator", {}) if isinstance(original.get("coordinator"), dict) else {}
    new_mode = mode if mode is not None else existing.get("mode")
    new_instructions = instructions if instructions is not None else existing.get("instructions")

    lines = ["[coordinator]"]
    written_mode: str | None = None
    if new_mode is not None:
        written_mode = normalize_coordinator_mode(str(new_mode), strict=True)
        lines.append(f'mode = "{written_mode}"')
    if new_instructions is not None:
        lines.append(f"instructions = {_toml_string(str(new_instructions))}")
    block = "\n".join(lines) + "\n"

    try:
        span = _coordinator_span(text)
    except ValueError as exc:
        raise ValueError(f"could not isolate the [coordinator] table in {path}") from exc
    if span:
        start, end = span
        new_text = text[:start] + block + text[end:]
    else:
        new_text = block if not text.strip() else text.rstrip() + "\n\n" + block

    try:
        parsed = tomllib.loads(new_text)
        coord = parsed.get("coordinator")
        if not isinstance(coord, dict):
            raise ValueError("missing coordinator table")
        if written_mode is not None and coord.get("mode") != written_mode:
            raise ValueError("mode mismatch")
        if new_instructions is not None:
            got = (coord.get("instructions") or "").strip()
            expected = str(new_instructions or "").strip()
            if got != expected:
                raise ValueError("instructions mismatch")
        original_other = {key: value for key, value in original.items() if key != "coordinator"}
        parsed_other = {key: value for key, value in parsed.items() if key != "coordinator"}
        if original_other != parsed_other:
            raise ValueError("other tables changed")
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        raise ValueError(
            "refusing to write agents.toml: rewritten [coordinator] block failed self-check"
        ) from exc

    atomic_write_text(path, new_text)
    return path


def _merge_server(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    out.update(overlay)
    return out


def load_config(home: Path | None = None) -> AppConfig:
    bundled_raw = _load_toml(bundled_agents_toml())
    user_home = home or bridge_home()
    overlay_path = user_home / "agents.toml"
    try:
        overlay_raw = _load_toml(overlay_path)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(
            f"{overlay_path} is not valid TOML: {exc}. "
            "Fix or delete the file, then restart the Bridge."
        ) from exc
    supported_sections = {"agents", "env", "server", "coordinator", "quota", "hub", "models"}
    unsupported = sorted(set(overlay_raw) - supported_sections)
    warnings = []
    if unsupported:
        warnings.append(
            "unsupported agents.toml section(s) ignored: " + ", ".join(unsupported)
        )
    bundled = _raw_agents(bundled_raw)
    overlay = _raw_agents(overlay_raw)
    merged: dict[str, dict[str, Any]] = {name: dict(spec) for name, spec in bundled.items()}
    for name, spec in overlay.items():
        merged[name] = _merge_agent_spec(merged.get(name, {}), spec)
    agents = {
        name: AgentConfig.model_validate({**spec, "name": name})
        for name, spec in merged.items()
    }
    if os.environ.get("AGENT_BRIDGE_ENABLE_FAKE") == "1":
        agents["fake"] = AgentConfig(
            name="fake",
            protocol="fake",
            command=["fake"],
            revivable=True,
            idle_unload_sec=0,
        )
    env = EnvConfig.model_validate(_merge_env(_coerce_env(bundled_raw), _coerce_env(overlay_raw)))
    server = ServerConfig.model_validate(
        _merge_server(_coerce_server(bundled_raw), _coerce_server(overlay_raw))
    )
    coord_raw = {**_coerce_coordinator(bundled_raw), **_coerce_coordinator(overlay_raw)}
    # Per-host override: each MCP host entry can set its own mode via env
    # (e.g. Codex manual, Cursor eager) without a second config file.
    env_mode = os.environ.get("AGENT_BRIDGE_MODE")
    if env_mode:
        coord_raw["mode"] = env_mode
    coord_raw["mode"] = normalize_coordinator_mode(coord_raw.get("mode"))
    coordinator = CoordinatorConfig.model_validate(coord_raw)
    quota = QuotaConfig.model_validate({**_coerce_quota(bundled_raw), **_coerce_quota(overlay_raw)})
    models_raw: dict[str, Any] = {}
    for raw in (bundled_raw, overlay_raw):
        block = raw.get("models", {})
        if not isinstance(block, dict):
            raise ValueError("[models] must be a TOML table")
        models_raw.update(block)
    models = ModelsConfig.model_validate(models_raw)
    return AppConfig(
        hub=HubConfig.model_validate({**bundled_raw.get('hub', {}), **overlay_raw.get('hub', {})}),
        agents=agents,
        env=env,
        server=server,
        coordinator=coordinator,
        quota=quota,
        models=models,
        warnings=warnings,
    )
