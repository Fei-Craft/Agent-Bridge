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
agent-bridge ask opencode 'Review this project' --cwd /absolute/project --model opendesign/deepseek-v4.1-flash-eco --effort high
```

`ask` forwards optional `--model` and `--effort` to invoke/dispatch. Omit them to
use the worker's defaults. Model IDs and supported effort values depend on the
worker/provider. `call invoke` and `call dispatch_task` also accept `--model` and
`--effort` through their generic `--key value` arguments.

A wait timeout is not failure. Invoke returns only the first result page; check
status and retrieve further pages with get_result. Manual coordinator policy
still requires `user_requested:true` on dispatch/invoke (use `call invoke`).

## Peer calls and security

### Headless CLI option and measurements

Hub workers may use `protocol = "cli"` instead of ACP. Set it under
`[agents.opencode]`, `[agents.claude]` or `[agents.cursor]` in the user overlay,
restart the hub and create new sessions. Bundled defaults remain unchanged;
switching back to `"acp"` restores the ACP command. See [CLI configuration and
permission caveats](SETUP.md#optional-headless-cli-workers). HTTP, MCP and peer
`ask --model ...` calls use the same hub-injected environment with either protocol.

Compare protocols (fresh hubs, three fresh sessions per protocol, then one resumed
turn per session; not a provider-throughput benchmark):

```sh
uv run python scripts/bench_protocols.py --agent opencode --model opencode/big-pickle --n 3
```

The script reports success counts and median/min/max seconds for first turns,
first worker output and resumed turns, with per-sample artifacts under
`~/agent-bridge-bench/`. First output means the first worker transcript event,
including metadata/tool events, **not** necessarily the first visible text token.
Only successful samples enter timing summaries; `null` means no usable samples.
Resume success requires the follow-up answer and the same native session ID.
Keep protocol order/cache/network effects in mind: this cannot by itself prove
one protocol is faster. `--protocols cli acp` reverses order.
Use `--opencode-config PATH` for a custom provider configuration whose key is an
environment reference. Optional `--worker-wrapper PATH` wraps commands with
`python PATH --exec COMMAND ARGS` (for credential-redacting wrappers). No environment
or raw error bodies are printed; do not enable CLI debug logging around secrets.

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

## Cloud API workers

Cloud workers run alongside local ACP/CLI workers; they do not need a local CLI
or `cwd`. Add the commented `cursor-cloud` / `devin-cloud` presets from
`agents.toml.example` to your user configuration, bind credentials in the **hub
process environment**, and restart the hub. Keys are read from their named env
variables at request time, never from `agents.<name>.env`, and are not persisted.
An availability probe only checks credential/org presence, not entitlement.

```sh
agent-bridge ask cursor-cloud 'Fix the failing tests' --repo https://github.com/org/repo --ref main --model composer-2 --timeout 3600
agent-bridge ask devin-cloud 'Implement the feature' --repo https://github.com/org/repo --model fast --timeout 3600
agent-bridge ask cursor-cloud 'Add tests too' --session-id sess_FROM_RESULT --timeout 3600
```

Use `call dispatch_task` then `call get_result --task-id ...` for the Bridge
session ID and structured metadata. REST `POST /v1/tasks`, `POST
/v1/agents/{agent}/invoke` and MCP `dispatch_task` accept optional `repo` and
`ref`, overriding config on the first turn. Cloud follow-ups keep their original
repository/ref; a different explicit repository/ref is rejected. Local workers
still require an absolute existing cwd and reject repo/ref. `ask` defaults cwd
to the shell's current directory for compatibility; cloud workers ignore it.

Cursor uses the v1 agents/runs API. `--model` is the Cursor model ID; `--effort`
has no Cursor mapping. Devin uses the v3 organization sessions API: `--model`
takes precedence over `--effort`, then config `devin_mode`; `--model` selects a
Devin mode such as `normal`, `fast`, `lite`, `ultra` or `fusion`, not an upstream
model. Effort maps `off`→`fast`, `low`→`lite`, `medium`→`normal`, and
`high`/`max`→`ultra` (higher modes can cost more).
Devin mode cannot change on a follow-up. Devin's create API has no dedicated
repository-ref field: Bridge adds the ref as a prompt instruction (not a
server-enforced checkout guarantee). Cursor sends `startingRef` directly.

Results include a provider-neutral `remote` dictionary with provider IDs, URL,
status/detail, PR URLs and Cursor branch records; Devin structured output is
included in text and metadata. Local `files_changed` is unavailable for cloud
turns: inspect the remote branch/PR instead. Cursor branch metadata is per-agent,
not a per-turn diff. Cursor token usage is best-effort (early-access usage API
may return 403); Devin reports cumulative session `acus_consumed`, **not tokens
or a per-turn ACU delta**. Cloud agents incur provider costs, including Devin ACUs;
set `max_acu_limit` and monitor the provider's limits.

Cloud defaults: no silence timeout, 24-hour turn timeout, polling every 5 seconds.
Override `stall_timeout_sec` / `turn_timeout_sec` on the agent and
`cloud.poll_interval_sec` as needed. Status changes and new messages count as
progress, unchanged poll responses do not. Cursor follows run terminal statuses;
unknown nonterminal statuses are tolerated. Devin `waiting_for_user` finishes
the Bridge turn; `waiting_for_approval` finishes it with a warning. Credit/limit
details fail the turn.

**Cancellation:** Cursor cancels the current run; ending its Bridge session
archives the agent. Devin has no per-turn stop: **cancel and end_session both
terminate the Devin session with archive=false**, making that session
non-resumable. Hub/Registry shutdown and idle unload only detach locally: they
never archive Cursor agents, cancel remote runs, or terminate Devin sessions.
Remote work and provider billing may continue after the hub stops; use explicit
`end_session` or cancellation to stop it. After an unclean restart, running Bridge tasks fail with
`bridge_restarted`; persisted remote IDs/URL remain available to inspect or stop
the provider job, which may still be running. Bridge does not automatically
reattach to that in-flight turn.

No hub URL/token/caller environment is sent to cloud providers. Cloud workers
cannot call back to the default loopback hub; peer-call env injection applies
only to local workers. `base_url` is a trusted configuration setting: point it
only at the provider or a trusted gateway (local HTTP is useful for tests).
Use HTTPS for real credentials. Never put API keys in URLs, prompts or config.

API references: [Cursor v1](https://cursor.com/docs/cloud-agent/api/endpoints),
[Devin v3](https://docs.devin.ai/api-reference/v3/sessions/post-organizations-sessions).

[中文](HUB.zh-CN.md)
