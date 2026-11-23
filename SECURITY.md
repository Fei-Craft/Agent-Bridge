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
