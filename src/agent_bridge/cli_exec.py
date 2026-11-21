from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from agent_bridge.config import AgentConfig
from agent_bridge.models import Session, Task
from agent_bridge.processes import resolve_command


def _sum_usage(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, (int, float)):
            target[key] = target.get(key, 0) + value
        elif isinstance(value, dict):
            nested = target.setdefault(key, {})
            if isinstance(nested, dict):
                _sum_usage(nested, value)


def cli_command(agent: AgentConfig) -> list[str]:
    return resolve_command(agent.cli.command or agent.command,
                           [] if agent.cli.command else agent.fallback_commands)


def build_cli_argv(agent: AgentConfig, session: Session, task: Task) -> list[str]:
    cfg = agent.cli
    values = {'prompt': task.message, 'session': session.native_session_id or '',
              'model': task.model or session.model or '', 'effort': task.effort or session.effort or '',
              'cwd': task.cwd or session.cwd}

    def expand(args: list[str]) -> list[str]:
        return [arg.format_map(values) for arg in args]

    cmd = cli_command(agent) + expand(cfg.extra_args)
    if session.native_session_id:
        if not cfg.resume_args:
            raise ValueError('CLI emitted a session id but cli.resume_args is not configured')
        cmd += expand(cfg.resume_args)
    elif session.turns and cfg.resume_args:
        raise ValueError('CLI did not emit a native session id; cannot resume this conversation')
    for value, args, name in ((values['model'], cfg.model_args, 'model'),
                              (values['effort'], cfg.effort_args, 'effort')):
        if value:
            if not args:
                raise ValueError(f'cli.{name}_args is required to select {name}')
            cmd += expand(args)
    return cmd + expand(cfg.prompt_args)


@dataclass
class CliTurnState:
    parser: str
    chunks: list[str] = field(default_factory=list)
    final: str | None = None
    session_id: str | None = None
    model: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    events: int = 0
    partial: bool = False

    @property
    def text(self) -> str:
        return self.final if self.final is not None else ''.join(self.chunks)

    def feed(self, raw: str) -> str:
        if self.parser == 'text':
            self.chunks.append(raw)
            return 'message_chunk'
        try:
            obj = json.loads(raw)
        except ValueError:
            return 'raw'
        if not isinstance(obj, dict):
            return 'raw'
        self.events += 1
        event = obj.get('type', '')
        session = obj.get('session_id') or obj.get('sessionID')
        if isinstance(session, str):
            self.session_id = session
        model = obj.get('model')
        if isinstance(model, str):
            self.model = model
        usage = obj.get('usage')
        if isinstance(usage, dict):
            self.usage.update(usage)
        if event == 'error' or obj.get('is_error'):
            error = obj.get('error') or obj.get('result') or obj.get('message') or 'CLI reported an error'
            self.error = error if isinstance(error, str) else json.dumps(error)
            return 'error'
        if self.parser == 'opencode':
            part = obj.get('part')
            if isinstance(part, dict):
                if event == 'text' and isinstance(part.get('text'), str):
                    self.chunks.append(part['text'])
                    return 'message_chunk'
                if event == 'step_finish' and isinstance(part.get('tokens'), dict):
                    _sum_usage(self.usage, part['tokens'])
                    if isinstance(part.get('cost'), (int, float)):
                        self.usage['cost'] = self.usage.get('cost', 0) + part['cost']
                if event == 'tool_use':
                    return 'tool_call'
            return 'raw'
        if self.parser in {'claude', 'cursor'}:
            if event == 'stream_event':
                inner = obj.get('event', {})
                if isinstance(inner, dict):
                    delta = inner.get('delta', {})
                    if isinstance(delta, dict) and isinstance(delta.get('text'), str):
                        self.chunks.append(delta['text'])
                        self.partial = True
                        return 'message_chunk'
            message = obj.get('message')
            if event == 'assistant' and isinstance(message, dict):
                if isinstance(message.get('model'), str):
                    self.model = message['model']
                if isinstance(message.get('usage'), dict):
                    self.usage.update(message['usage'])
                content = message.get('content', [])
                if isinstance(content, list):
                    if not self.partial:
                        self.chunks.extend(p['text'] for p in content if isinstance(p, dict)
                                           and p.get('type') == 'text' and isinstance(p.get('text'), str))
                    self.partial = False
                    return 'tool_call' if any(isinstance(p, dict) and p.get('type') == 'tool_use'
                                              for p in content) else 'message_chunk'
            if event == 'result' and isinstance(obj.get('result'), str):
                self.final = obj['result']
                return 'message_chunk'
            return 'raw'
        # Generic JSONL: text/delta chunks, result snapshots, session_id/model/usage metadata.
        text = obj.get('text') or obj.get('delta')
        if isinstance(text, str):
            self.chunks.append(text)
            return 'message_chunk'
        if event == 'result' and isinstance(obj.get('result'), str):
            self.final = obj['result']
            return 'message_chunk'
        return 'raw'
