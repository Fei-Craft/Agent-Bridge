from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, Field

SELECTION_PATTERN = r'^[A-Za-z0-9._:/@][A-Za-z0-9._:/@-]{0,99}$'
MODEL_PATTERN = r'^[A-Za-z0-9._:/@][A-Za-z0-9._:/@#-]{0,99}$'
Selection = Annotated[str, Field(pattern=MODEL_PATTERN)]
EffortSelection = Annotated[str, Field(pattern=SELECTION_PATTERN)]


def validate_selection(value: str | None, name: str) -> None:
    pattern = MODEL_PATTERN if name == 'model' else SELECTION_PATTERN
    if value is not None and (not isinstance(value, str) or not re.fullmatch(pattern, value)):
        raise ValueError(f'{name} must match {pattern}')


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat()


class TaskStatus(StrEnum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class FilesChangedState(StrEnum):
    pending = "pending"
    collected = "collected"
    unavailable = "unavailable"


class ProcState(StrEnum):
    spawning = "spawning"
    ready = "ready"
    busy = "busy"
    idle_unloaded = "idle_unloaded"
    dead = "dead"


TERMINAL_STATUSES = {TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled}
DEFAULT_WAIT_SEC = 180.0
EFFORTS = ("off", "low", "medium", "high", "max")


def normalize_effort(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip().lower()
    aliases = {"l": "low", "m": "medium", "med": "medium", "h": "high"}
    value = aliases.get(value, value)
    if value not in EFFORTS:
        raise ValueError(f"effort must be one of off, low, medium, high, max (got {raw!r})")
    return value


def agy_effort(effort: str | None) -> str | None:
    if effort in {None, "low", "medium", "high"}:
        return effort
    if effort == "off":
        return "low"
    if effort == "max":
        return "high"
    return None


def dsh_effort(effort: str | None) -> str | None:
    if effort in {None, "off", "low", "high", "max"}:
        return effort
    if effort == "medium":
        return "high"
    return None


def grok_effort(effort: str | None) -> str | None:
    """Map Bridge effort onto Grok `_meta.reasoningEffort` wire values.

    Grok Build (`ReasoningEffort::from_str`) accepts
    ``none|minimal|low|medium|high|xhigh|max``. Bridge ``off`` is Grok
    ``none``. Bridge ``max`` maps to ``xhigh``: that is the top tier the
    grok-4.6 catalog actually advertises, whereas ``max`` is not listed for
    any current model and gets clamped by the worker.
    """
    if effort in {None, "low", "medium", "high"}:
        return effort
    if effort == "max":
        return "xhigh"
    if effort == "off":
        return "none"
    return None


class Session(BaseModel):
    caller_session_id: str | None = None
    remote_origin: bool = False
    call_depth: int = 0
    notification_receipts: dict[str, str] = Field(default_factory=dict)
    session_id: str
    agent: str
    cwd: str
    repo: str | None = None
    ref: str | None = None
    remote: dict[str, Any] = Field(default_factory=dict)
    native_session_id: str | None = None
    proc_state: ProcState = ProcState.idle_unloaded
    model: Selection | None = None
    effort: EffortSelection | None = None
    title: str | None = None
    turns: int = 0
    created_at: str = Field(default_factory=iso)
    last_active_at: str = Field(default_factory=iso)
    pid: int | None = None
    pid_create_time: float | None = None
    image_name: str | None = None
    owner_pid: int | None = None
    owner_create_time: float | None = None


class TaskFailure(BaseModel):
    operation: str
    kind: str
    prompt_may_have_been_sent: bool
    exit_code: int | None = None
    stderr_summary: str | None = None


class Task(BaseModel):
    call_depth: int = 0
    caller_session_id: str | None = None
    notify_caller: bool = False
    callback_url: str | None = None
    callback_signed: bool = False
    caller_delivery: dict[str, Any] = Field(default_factory=dict)
    callback_delivery: dict[str, Any] = Field(default_factory=dict)
    notification_source_task_id: str | None = None
    task_id: str
    session_id: str
    agent: str
    message: str
    cwd: str
    repo: str | None = None
    ref: str | None = None
    remote: dict[str, Any] = Field(default_factory=dict)
    model: Selection | None = None
    effort: EffortSelection | None = None
    observed_model: str | None = None
    observed_effort: str | None = None
    status: TaskStatus = TaskStatus.queued
    stop_reason: str | None = None
    result_text: str = ""
    result_chars: int = 0
    files_changed: list[str] = Field(default_factory=list)
    files_changed_total: int = 0
    files_changed_truncated: bool = False
    files_changed_state: FilesChangedState = FilesChangedState.pending
    # Paths the worker REPORTED that resolve outside task.cwd. Kept separate
    # so an escape is never silently folded into an in-workspace path (E3).
    files_outside_cwd: list[str] = Field(default_factory=list)
    files_outside_cwd_total: int = 0
    usage: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    failure: TaskFailure | None = None
    warnings: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=iso)
    started_at: str | None = None
    finished_at: str | None = None
    owner_pid: int | None = None
    owner_create_time: float | None = None


class TranscriptEvent(BaseModel):
    ts: str = Field(default_factory=iso)
    type: str
    data: dict[str, Any] = Field(default_factory=dict)


class TurnResult(BaseModel):
    remote: dict[str, Any] = Field(default_factory=dict)
    text: str = ""
    files_changed: list[str] = Field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: dict[str, Any] = Field(default_factory=dict)
    native_session_id: str | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    # Last model/effort the adapter successfully applied this turn. Grok and
    # Kimi observers in Registry overwrite these with on-disk sampler truth.
    observed_model: str | None = None
    observed_effort: str | None = None
