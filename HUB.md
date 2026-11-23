# Central hub

## Local dashboard

Run `agent-bridge ui` after starting the hub; `agent-bridge ui --no-open` prints
a login URL instead. The Chinese dashboard at `/ui` provides agents, sessions,
task filtering/refresh, transcripts/results, dispatch/cancel, delivery status,
gateway allowlists, recent redacted audit records and approvals. It follows the
system light/dark theme and adapts to small screens. Task list is limited to the
latest 500 in-memory tasks; audit shows up to 30 recent records.

Available **only on the admin loopback listener**, never the remote gateway.
Login links contain a single-use code valid for 60 seconds, not the bearer token.
Opening the browser can expose that code in process arguments during its brief
lifetime; `--no-open` prints the sensitive link instead. Do not save/share it.
The page removes the fragment immediately and exchanges the code for a one-hour
HttpOnly, SameSite=Strict session cookie. Codes/sessions are hashed, RAM-only;
restart or logout invalidates sessions. No localStorage, CDN or frontend build.
Cookie API calls require the UI header and same loopback origin; cookies cannot
change preferences, end sessions, mint login codes or reconfigure the gateway.
Treat printed login links as credentials. Separate CLI discovery needs a usable
secure keyring (or an explicitly supplied in-memory worker environment token).
Browser profiles/history and same-user host compromise remain outside the hub's
security boundary. Never tunnel the admin listener.

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
file contains url, pid and started_at, **never a token**; it is atomically written
with mode 0600 and removed on clean shutdown. The per-start bearer token is kept
in the OS keyring; HubClient uses worker environment credentials first, otherwise
the keyring entry for this Bridge home. Restart coordinators to switch backends.

### Secret storage

The standing rule is **NO plaintext secrets anywhere on disk (keys, hub token,
callback secrets, issued tokens)**. Bridge uses the `agent-bridge` keyring service,
accepting OS-backed secure stores, not plaintext/third-party file backends.
Use `agent-bridge secrets set cursor` or `set devin` (hidden `getpass` input),
`agent-bridge secrets list` (presence only), and `agent-bridge secrets delete cursor`.
Cloud keys are loaded at startup; restart after changing them. Explicit named
environment variables remain a warned fallback. Never put credentials in config,
shell history, argv, URLs or prompts. `ask --callback-secret` prompts securely;
callback signing secrets must contain at least 32 characters.

On a headless/locked machine without a usable keyring, Bridge warns and retains
secrets in RAM only: there is **no file fallback**. Independent CLI/MCP processes
cannot discover a RAM-only hub token; configure/unlock a secure OS keyring first.
Workers launched by that hub still receive their token through the environment.
Signed callbacks whose secret is unavailable after restart fail with
`secret_unavailable_after_restart`; they are never sent unsigned. Startup removes
legacy plaintext hub tokens and migrates/deletes `callback-secrets/` files.
This cannot erase old backups, filesystem snapshots, swap or third-party CLI
credential/log files: secure those separately and rotate legacy credentials.

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

Prefer `agent-bridge call` (automatic keyring authentication). For trusted local
REST scripts only, retrieve the token into memory; never print or persist it:

```sh
export TOKEN=$(python -c 'from agent_bridge.paths import bridge_home; from agent_bridge.secrets import get, hub_key; print(get(hub_key(bridge_home())) or "")')
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
The management listener is loopback-only; `--allow-remote` has been removed.
Never expose this listener through a tunnel. Use the isolated remote gateway.

## Blocking calls and async push-back

`invoke` (REST `/v1/agents/{agent}/invoke` or `call invoke`) and `ask` now wait
until terminal by default, without the old 180-second cap. Explicit
`timeout_sec` / `ask --timeout SEC` returns a running task ID without cancelling.
Worker stall/turn limits still apply. HubClient/CLI use repeated bounded waits
with unbounded HTTP read timeouts; direct REST invoke streams JSON whitespace
keepalives every 15 seconds before the final JSON object. Configure reverse
proxies to allow long streaming responses. `ask` prints the started task ID to
stderr; Ctrl-C stops waiting, not the worker, and prints how to fetch its result.

MCP `invoke_agent` waits with periodic progress notifications. Use it when the
result is needed like a tool result. Some clients impose absolute tool-duration
limits despite progress: then use `dispatch_task` and async notification, or
bounded `wait_task` polling. A top-level coordinator without a worker session
cannot receive automatic follow-up turns; use callbacks or polling instead.

```sh
agent-bridge ask codex 'Review the project' --cwd /absolute/project
# Inside a hub worker: defaults notify_caller=true, prints task ID immediately.
agent-bridge ask codex 'Research this issue' --cwd /absolute/project --async
agent-bridge ask codex 'Review' --cwd /absolute/project --async --callback-url https://trusted.example/result
```

REST/MCP `dispatch_task` accepts `notify_caller`, `callback_url`, and optional
`callback_secret_name` referencing the keyring; raw `callback_secret` is rejected.
Use `agent-bridge callback-secret set NAME` (hidden input), `list`, or `delete NAME`.
`ask --callback-secret` stores a temporary reference, not a secret in its request,
and requires a shared secure keyring. The caller is bound to its worker bearer;
`notify_caller=true` without a
valid hub caller is rejected. Results become concise follow-up turns in that
caller session, queued while busy, in dispatch order. They include agent/task
IDs, status, result preview, files/remote PRs and fetch instructions. Delivery
turns do not increase call depth; calls made during delivery turns cannot
request further notifications/callbacks, preventing automatic notification loops.

Callbacks POST the first-page `get_result` JSON (including `ok:true`) to a trusted
HTTPS endpoint, without redirects. With a secret, verify
`X-Agent-Bridge-Signature: sha256=HEX` as HMAC-SHA256 over
`timestamp + "." + task_id + "." + raw_body` (UTF-8 prefix, unchanged body bytes).
Read `X-Agent-Bridge-Timestamp` (Unix seconds) and `X-Agent-Bridge-Task` from the
headers; reject timestamps more than **300 seconds** from your clock before
accepting the signature. Four attempts use exponential
backoff (1, 2, 4 seconds); `callback_delivery` records attempts/status/error in
`check_task` / `get_result`. `caller_delivery` records follow-up task ID/status.
Callbacks are at-least-once: receivers must deduplicate by task ID. Caller turns
use persisted task-ID receipts, also surviving a lost acknowledgment/restart.
Pending deliveries survive restart, including `bridge_restarted` failures;
delivery means the follow-up was submitted, not that its model turn succeeded.
An ended/missing caller records a delivery failure. Callback secrets are stored
per task in the OS keyring (RAM only if unavailable), never in task
results/logs/state JSON; they are deleted after final delivery/failure or pruning.
Do not put secrets in URLs. DNS is checked at dispatch and every send; all resolved
addresses must be public unicast. Connections are pinned to a checked IP while
preserving the original Host and TLS SNI/certificate hostname. Private, loopback,
link-local, multicast, reserved and unspecified destinations are rejected.
Only `[hub] allow_insecure_callbacks = true` permits loopback (including HTTP),
for local development. It does not enable private-network callbacks.

## Cloud API workers

Cloud workers run alongside local ACP/CLI workers; they do not need a local CLI
or `cwd`. Add the commented `cursor-cloud` / `devin-cloud` presets from
`agents.toml.example` to your user configuration, run `agent-bridge secrets set cursor`
or `agent-bridge secrets set devin`, and restart the hub. Keys use the OS keyring
first, with a warned explicit environment fallback, never `agents.<name>.env`.
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

No admin hub URL/token/caller environment is sent to cloud providers. Cloud workers
cannot call back to the default loopback hub; peer-call env injection applies
only to local workers. `base_url` is a trusted configuration setting: point it
only at the provider or a trusted gateway (local HTTP is useful for tests).
Use HTTPS for real credentials. Never put API keys in URLs, prompts or config.

API references: [Cursor v1](https://cursor.com/docs/cloud-agent/api/endpoints),
[Devin v3](https://docs.devin.ai/api-reference/v3/sessions/post-organizations-sessions).

## Opt-in cloud-to-local gateway

Remote-originated local workers receive **no hub credentials** and have peer
calls disabled (`AGENT_BRIDGE_HUB=off`), including later turns in those sessions.
They cannot use the unrestricted admin API. Hub-managed result delivery back to
the cloud caller remains available; recursively delegating from these local
workers is deliberately unsupported.

Cloud agents can use a **separate, restricted** gateway, not the admin API.
It is off by default. Copy `[hub.remote]` from `agents.toml.example`, with explicit
`remote_callable_agents` and named absolute `remote_workspaces`. Both listeners
must stay on loopback when remote access is enabled; ports must differ.
Workspace names are exact allowlist keys, not paths; raw `cwd`, session ids,
callbacks and arbitrary dispatch parameters are rejected. This is routing
containment, **not an OS/filesystem sandbox**: allowlisted agents can execute
tools with the host user's privileges. Use a dedicated OS account/container and
dedicated worktree, never an unrestricted agent over sensitive files.

Start `agent-bridge serve`, then in another terminal `agent-bridge tunnel`.
The helper runs `cloudflared tunnel --url http://127.0.0.1:8766`, parses the quick
HTTPS URL, and updates the running hub. Install cloudflared separately from its
official releases. `--name EXISTING_TUNNEL` runs an existing named tunnel, never
creates credentials; use `--public-url https://gateway.example.com` when its URL
is not printed. Configure its ingress for the **remote port only**. Authenticate
cloudflared using an externally provisioned in-memory environment token when
needed; do not create plaintext tunnel credential files. Helper output omits
raw cloudflared logs. Ctrl-C terminates the child tunnel. The URL change is
runtime-only; re-run the helper after restarting the hub. Never tunnel port 8765.

Each cloud turn gets a new `abr_` token in its outbound provider prompt, with
gateway URL, curl examples and allowed names. Tokens have 256 bits of entropy;
the gateway retains only SHA-256 hashes/scopes in RAM. No token goes into Bridge
state, transcripts, result files, logs or audit. Provider-side prompts necessarily
contain it: treat cloud history and the provider as credential-bearing trusted
systems. Restart invalidates all gateway tokens. `token_ttl_sec` defaults to
3600 seconds, independent of the cloud turn timeout. End-of-turn revokes
new calls/cancellation immediately; owned status/result GETs retain up to
`result_fetch_grace_sec` (default 30 seconds), never beyond token expiry.

The **only** remote routes are:

| Method | Path | Input / scope |
|---|---|---|
| POST | `/v1/agents/{agent}/invoke` | message, workspace NAME, optional notify_caller, model, effort, timeout_sec; blocking with keepalives |
| POST | `/v1/tasks` | same fields plus agent; asynchronous task id |
| GET | `/v1/tasks/{id}` | only tasks created with this exact token |
| GET | `/v1/tasks/{id}/result` | owned result, optional cursor/max_chars |
| POST | `/v1/tasks/{id}/cancel` | owned task; JSON `{}` |

Everything else, including health, list agents/sessions, transcripts, approvals,
preferences, wait and CORS preflight, is 404. POSTs require JSON and are streamed
under `max_body_bytes`; tokens have call budgets, per-minute rate limits and
concurrent-task limits. Remote calls inherit cloud task depth, capped at three,
and cannot call their own originating agent. No raw user callback URL is allowed
on this surface (avoids remote callback SSRF). Admin callbacks require public HTTPS
destinations, except explicitly enabled loopback callbacks.

`notify_caller: true` submits the local result into the originating Cursor agent
as a follow-up run or Devin session as a message. It uses the durable ordered
delivery queue, waits for the current cloud turn to finish, and suppresses
notification loops. Polling is available while the scoped token is valid. The
underlying local task is not cancelled on HTTP disconnect or token expiry.

`require_approval=true` by default holds submissions before execution.
Use `agent-bridge approvals list`, `approvals approve APPROVAL_ID`, or
`approvals deny APPROVAL_ID`. Pending approvals expire with their token and are
discarded on restart. Admin-only REST: GET `/v1/approvals`, POST
`/v1/approvals/{id}/approve` or `/deny`; PUT `/v1/remote/public-url` with
`{"public_url":"https://..."}`, GET `/v1/remote` for listener metadata.

`remote-audit.jsonl` is mode 0600, recording time, token hash prefix, peer IP,
bounded CF-Ray/CF-Connecting-IP fields, route, agent, workspace and outcome, not
prompts, Authorization or JWT contents. Forwarded CF metadata is advisory.
Behind tunnels `remote_ip` usually identifies the loopback tunnel process;
CF-Connecting-IP can be spoofed without trusted edge validation and a
non-bypassable origin. Do not use either field for authentication/attribution.
Audit rotates at 1 MiB with one backup (both mode 0600); unauthenticated writes
are globally limited to 20/minute. Quotas are per token, not a global compute/DoS cap.

For public use prefer a named tunnel behind **Cloudflare Access**, service-token
policy and edge rate limits. Provide `CF-Access-Client-Id` and
`CF-Access-Client-Secret` from the cloud provider's secret facility, not config,
access_note or prompts. The removed `require_cf_access` option is a configuration
error: the hub does not validate Access JWTs. Configure authentication at the
Access edge and ensure the origin cannot be reached directly.
A quick tunnel alone is not Cloudflare Access. Never expose admin
through either tunnel or a permissive ingress catch-all.

[中文](HUB.zh-CN.md)
