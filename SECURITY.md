# Security

Do not file secrets, tokens, or private paths in GitHub issues.

To report a vulnerability, use [GitHub Security Advisories](https://github.com/FeiZhuLulu/Agent-Bridge/security/advisories/new) for this repository.

## Hub worker credentials

The admin bearer belongs to the owner CLI, dashboard and coordinator MCP host,
not workers. Local hub workers receive a session-bound `abw_` bearer; only its
SHA-256 hash and session binding are retained by the hub's credential registry,
in RAM. Tokens are registered for redaction and never persisted in Bridge state.

A worker bearer is accepted only during an active turn of its bound session.
Resident ACP processes reuse it across turns; it is inactive between turns and
permanently revoked on process exit, session closure or hub shutdown. A leaked
resident-process token can become usable again during that process's next turn.
One-shot workers receive fresh tokens each turn, revoked when the turn ends.

Workers may discover agents, dispatch/invoke peers, and read/wait/cancel their
own child tasks and read child-session transcripts. They cannot manage approvals,
preferences, remote settings, UI login, or sessions. Follow-ups and transcripts
are restricted to sessions containing only their own child tasks. Caller identity,
notification destination and depth come from the token binding, never the
`X-Agent-Bridge-Caller` header. Admin requests have no caller identity, so cannot
request `notify_caller`. Notification turns retain their no-notification-loop rule.
Remote-origin workers still receive no hub credential (`AGENT_BRIDGE_HUB=off`).

This is an API authorization boundary, not an OS sandbox. Same-user code may
still access the owner's keyring or other local credentials; isolate untrusted
workers with a separate OS account or container.

## Remote execution approval

The management API is loopback-only, with no `--allow-remote` escape hatch.
Discovery sends credentials only to HTTP loopback URLs or HTTPS URLs; URLs
with embedded credentials, query strings or fragments are rejected.
Never tunnel the admin listener; only the separate gateway may be exposed.

Remote calls require local approval by default. Enabling the gateway rejects
allowlisted agents with permission-skipping argv/presets (including the bundled
Claude/OpenCode CLI presets) unless that agent explicitly sets
`remote_allow_unsafe_permissions = true`. This opt-in emits a warning and does
not create a sandbox. Same-user workers can read the owner's keyring; use a
separate OS user or container for remote-origin execution.

## CLI input boundaries

CLI prompts use stdin or a standalone positional argument after `--`; custom
unprotected `{prompt}` templates are rejected. Model/effort selections are
restricted to 1–100 characters matching
`^[A-Za-z0-9._:/@][A-Za-z0-9._:/@-]{0,99}$` for effort; model IDs also allow
non-leading `#` for MiniMax variants. Remote callers additionally need
the exact selection in the agent's `remote_allowed_models` or
`remote_allowed_efforts` list (empty by default). These guards prevent option
injection; they do not constrain what tools an agent can execute.

## Remote token lifetime

Callback signing secrets are passed by keyring reference (`callback_secret_name`),
never as REST/MCP/call values. `agent-bridge callback-secret set NAME` prompts
securely; `list` prints names only and `delete NAME` removes the entry.
`ask --callback-secret` creates a short-lived keyring reference and cleans it up
after dispatch; it refuses memory-only storage that another process cannot read.
Signing secrets require at least 32 characters. Tasks keep only a signed boolean.

Remote gateway tokens expire after 3600 seconds by default, independent of cloud
turn timeouts (`hub.remote.token_ttl_sec` overrides this). The token is included
in the cloud prompt and remains in provider-side session history; protect that
history as credential-bearing data. Bridge redaction cannot remove provider copies.
