# Central hub

Run one long-lived registry for all your local agents:

```text
Coordinator MCP ── HTTP proxy ─┐
Shell (call / ask) ───────────┼─ Hub HTTP API → Registry → local worker CLIs
Workers (peer API / MCP) ─────┘                         ↖ peer calls
```

## Setup

Run `agent-bridge serve` before opening coordinators. Their existing stdio MCP
configuration automatically discovers the hub; no worker processes are spawned
by the MCP proxies. Without a discoverable hub, stdio retains its local registry.
Set `AGENT_BRIDGE_HUB=off` to force local mode.

`serve --host 127.0.0.1 --port 8765` overrides `[hub] host` and `port` in
your bridge home's `agents.toml`. Idle exit is disabled for the hub.

Discovery first checks `AGENT_BRIDGE_HUB_URL` and `AGENT_BRIDGE_HUB_TOKEN`, then
`<bridge home>/hub.json` (live PID and authenticated health check). The discovery
file contains url, pid, token and started_at; it is atomically written with mode
0600 and removed on clean shutdown. Restart coordinators to switch backends.

## REST reference

Every request requires `Authorization: Bearer <token>`, including health.
Responses preserve MCP's `ok` envelope; errors return 4xx and `ok:false`.
JSON request keys match the corresponding MCP tool arguments.

| Method | Path | Tool / parameters |
|---|---|---|
| GET | `/v1/health` | readiness |
| GET | `/v1/agents` | list_agents |
| PUT | `/v1/preferences` | set_preferences: mode, instructions |
| POST | `/v1/tasks` | dispatch_task: agent, message, cwd; optional session_id, model, effort, title, user_requested, request_id |
| GET | `/v1/tasks/{id}` | check_task |
| GET | `/v1/tasks/{id}/wait?timeout_sec=180` | wait_task |
| GET | `/v1/tasks/{id}/result?cursor=0&max_chars=20000` | get_result |
| POST | `/v1/tasks/{id}/cancel` | cancel_task |
| GET | `/v1/sessions?active_only=false` | list_sessions |
| GET | `/v1/sessions/{id}/transcript?offset=0&limit=50&kinds=message_chunk` | get_transcript |
| DELETE | `/v1/sessions/{id}` | end_session |
| POST | `/v1/agents/{agent}/invoke` | dispatch + wait + first result page; message, cwd, optional timeout_sec and dispatch options |

Read the token from your own hub.json; do not paste it into logs or commits:

```sh
export TOKEN=$(python -c 'import json; from agent_bridge.paths import bridge_home; print(json.loads((bridge_home()/"hub.json").read_text())["token"])')
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/v1/health
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"agent":"codex","message":"Review this project","cwd":"/absolute/project"}' \
  http://127.0.0.1:8765/v1/tasks
# Substitute the returned task_id:
curl -H "Authorization: Bearer $TOKEN" 'http://127.0.0.1:8765/v1/tasks/TASK_ID/wait?timeout_sec=180'
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/v1/tasks/TASK_ID/result
agent-bridge call list_sessions --active_only true
agent-bridge ask codex 'Review this project' --cwd /absolute/project --timeout 180
```

A wait timeout is not failure. Invoke returns only the first result page; check
status and retrieve further pages with get_result. Manual coordinator policy
still requires `user_requested:true` on dispatch/invoke (use `call invoke`).

## Peer calls and security

Workers inherit AGENT_BRIDGE_HUB_URL, AGENT_BRIDGE_HUB_TOKEN and
AGENT_BRIDGE_CALLER_SESSION through the worker environment. `call`, `ask`, and
nested MCP proxies send `X-Agent-Bridge-Caller` automatically. A worker may call
another agent via the same API. Root sessions have depth 0; each peer dispatch
adds 1. `[hub] max_depth` defaults to 3. Unknown callers, calls into one's own
session_id and calls exceeding the limit are rejected. Resuming a deeper session
never lowers its recorded depth. Depth records are scoped to the running hub.

This is a trusted-local-user API, not a sandbox or multi-tenant authorization
system: all bearer holders can access sessions, change preferences and spawn
CLIs with your filesystem permissions. Caller headers prevent accidental loops,
not malicious token holders. Never share tokens. Worker outputs are untrusted.
Non-loopback bind requires `--allow-remote`; HTTP is not encrypted, so use a
trusted TLS tunnel and firewall rather than exposing it to the Internet.

[中文](HUB.zh-CN.md)
