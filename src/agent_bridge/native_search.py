from __future__ import annotations

import ipaddress
import json
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from agent_bridge.diagnostics import redact_diagnostic
from agent_bridge.models import SourceReference, Task
from agent_bridge.secrets import redact

MAX_SOURCES = 32
MAX_RAW_CHARS = 64 * 1024
SEARCH_TOOLS = {"web_search", "web_fetch"}
SEARCH_PROTOCOLS = {"codex": "codex", "grok": "acp", "minimax": "acp"}
FORBIDDEN_TOOLS = {"image_gen", "image_edit", "image_to_video", "reference_to_video"}


def observe_call(task: Task, call_id: Any, name: str) -> bool:
    if not isinstance(call_id, str) or not call_id or len(call_id) > 128:
        return False
    prior = task.native_calls.get(call_id)
    if prior is not None:
        return prior == name
    if len(task.native_calls) >= 64:
        raise ValueError("native capability call evidence limit exceeded")
    task.native_calls[call_id] = name
    return True


def safe_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 2048 or any(ord(c) < 32 for c in value):
        return None
    if redact(value) != value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        if parsed.hostname.lower().rstrip(".") == "localhost":
            return None
        try:
            if not ipaddress.ip_address(parsed.hostname).is_global:
                return None
        except ValueError:
            pass
        if any(
            any(mark in key.lower() for mark in ("token", "signature", "credential", "password", "authorization"))
            or key.lower() in {"sig", "key", "api_key", "expires"}
            or key.lower().startswith("x-amz-")
            for key, _ in parse_qsl(parsed.query)
        ):
            return None
    except ValueError:
        return None
    return value


def add_source(into: list[SourceReference], value: Any, call_id: str) -> None:
    if not isinstance(value, dict) or not call_id or len(call_id) > 128 or len(into) >= MAX_SOURCES:
        return
    url = safe_url(value.get("url"))
    if url is None or any(s.url == url and s.tool_call_id == call_id for s in into):
        return
    title = value.get("title")
    snippet = value.get("snippet")
    into.append(SourceReference(
        title=redact_diagnostic(title[:500]) if isinstance(title, str) and title else url,
        url=url, snippet=redact_diagnostic(snippet[:2000]) if isinstance(snippet, str) else None,
        tool_call_id=call_id,
    ))


def extract_codex_sources(item: dict[str, Any], into: list[SourceReference]) -> None:
    call_id = item.get("id")
    action = item.get("action")
    if not isinstance(call_id, str):
        return
    results = item.get("results")
    if isinstance(results, list):
        for result in results[:MAX_SOURCES]:
            if isinstance(result, dict) and result.get("type") == "text_result":
                add_source(into, result, call_id)
    if isinstance(action, dict) and action.get("type") in {"open_page", "find_in_page", "openPage", "findInPage"}:
        add_source(into, {"url": action.get("url")}, call_id)


def bounded_output(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        if len(value) > MAX_RAW_CHARS:
            return None
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict):
        return None
    pending: list[tuple[Any, int]] = [(value, 0)]
    budget = MAX_RAW_CHARS
    nodes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > 4096 or depth > 12:
            return None
        if isinstance(current, str):
            budget -= len(current) * 6 + 2
        elif isinstance(current, dict):
            if len(current) > 4096:
                return None
            budget -= 2 + len(current)
            for key, nested in current.items():
                if not isinstance(key, str):
                    return None
                pending.extend(((key, depth + 1), (nested, depth + 1)))
        elif isinstance(current, list):
            if len(current) > 4096:
                return None
            budget -= 2 + len(current)
            pending.extend((nested, depth + 1) for nested in current)
        elif current is None or isinstance(current, (int, float, bool)):
            budget -= 32
        else:
            return None
        if budget < 0:
            return None
    return value


def observe_codex_search(task: Task, item: dict[str, Any]) -> None:
    action = item.get("action")
    if not isinstance(action, dict):
        return
    kind = action.get("type")
    call_id = item.get("id")
    if kind == "search":
        name = "web_search"
    elif kind in ("open_page", "find_in_page", "openPage", "findInPage"):
        name = "web_fetch"
    else:
        return
    if observe_call(task, call_id, name):
        extract_codex_sources(item, task.sources)


class AcpNativeSearch:
    def __init__(self, task: Task, native_id: str | None) -> None:
        self.task = task
        self.native_id = native_id
        self.names: dict[str, str] = {}
        self.urls: dict[str, str] = {}
        self.completed: set[str] = set()

    def tool_name(self, update: dict[str, Any]) -> str | None:
        call_id = update.get("toolCallId")
        if not isinstance(call_id, str) or not call_id or len(call_id) > 128:
            return None
        field = "toolName" if self.task.agent == "grok" else "name"
        name = update.get(field)
        raw_input = update.get("rawInput")
        if not isinstance(name, str) and self.task.agent == "grok" and isinstance(raw_input, dict):
            variant_names = {
                "WebSearch": "web_search", "WebFetch": "web_fetch",
                "ImageGen": "image_gen", "ImageEdit": "image_edit",
                "ImageToVideo": "image_to_video", "ReferenceToVideo": "reference_to_video",
            }
            variant = raw_input.get("variant")
            name = variant_names.get(variant) if isinstance(variant, str) else None
        if not isinstance(name, str) and self.task.agent == "grok":
            vendor_meta = update.get("_meta")
            if isinstance(vendor_meta, dict):
                xai_tool = vendor_meta.get("x.ai/tool")
                if isinstance(xai_tool, dict):
                    name = xai_tool.get("name")
        if not isinstance(name, str):
            return self.names.get(call_id)
        if name not in SEARCH_TOOLS | FORBIDDEN_TOOLS:
            return None
        if call_id not in self.names and len(self.names) < 256:
            self.names[call_id] = name
        return name if self.names.get(call_id) == name else None

    def consume(self, update: dict[str, Any]) -> str | None:
        name = self.tool_name(update)
        if name is None:
            return None
        call_id = update["toolCallId"]
        if name in FORBIDDEN_TOOLS:
            observe_call(self.task, call_id, name)
            return name
        raw_input = update.get("rawInput")
        if isinstance(raw_input, dict) and name == "web_fetch" and (url := safe_url(raw_input.get("url"))):
            self.urls[call_id] = url
        if update.get("status") == "completed" and call_id not in self.completed:
            output = bounded_output(update.get("rawOutput"))
            if output is not None and observe_call(self.task, call_id, name):
                self._search(name, call_id, output)
                self.completed.add(call_id)
        return name

    def _search(self, name: str, call_id: str, output: dict[str, Any]) -> None:
        if self.task.agent == "grok":
            variant = output.get("type")
            if name == "web_search" and variant == "WebSearch":
                citations = output.get("citations")
                if isinstance(citations, list):
                    for citation in citations[:MAX_SOURCES]:
                        add_source(self.task.sources, {"url": citation} if isinstance(citation, str) else citation, call_id)
            if name == "web_fetch" and variant == "WebFetch":
                # Historical native fixtures use a Content variant. Unknown
                # current variants do not silently become successful fetches.
                content = output.get("Content")
                if isinstance(content, dict) and isinstance(content.get("status_code"), int) and 200 <= content["status_code"] < 400:
                    url = content.get("url") or self.urls.get(call_id)
                    text = content.get("content")
                    snippet = text[:2000] if isinstance(text, str) and "base64" not in text[:2000] else None
                    add_source(self.task.sources, {"url": url, "snippet": snippet}, call_id)
        elif name == "web_search":
            # Accept the official search service body only inside an identified
            # successful ACP tool. Do not infer citations from arbitrary text.
            body = output
            content = output.get("content")
            if isinstance(content, list) and content and isinstance(content[0], dict):
                decoded = bounded_output(content[0].get("text"))
                if decoded is not None:
                    body = decoded
            if "code" in body and body["code"] != 0:
                return
            results = body.get("results")
            if isinstance(results, list):
                for result in results[:MAX_SOURCES]:
                    if isinstance(result, dict):
                        add_source(self.task.sources, {
                            "url": result.get("url") or result.get("href") or result.get("link"),
                            "title": result.get("title"), "snippet": result.get("snippet"),
                        }, call_id)
