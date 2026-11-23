from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from agent_bridge import secrets
from agent_bridge.secrets import redact
from agent_bridge.worker_tokens import WorkerTokens


def test_worker_token_active_turn_and_permanent_revocation():
    tokens = WorkerTokens()
    token = tokens.issue('session')
    assert tokens.scopes == {hashlib.sha256(token.encode()).hexdigest(): 'session'}
    assert tokens.authenticate(token) is None
    tokens.active.add('session')
    assert tokens.authenticate(token) == 'session'
    assert tokens.authenticate('invalid') is None
    tokens.active.clear()
    assert tokens.authenticate(token) is None
    tokens.active.add('session')
    assert tokens.authenticate(token) == 'session'
    tokens.revoke('session')
    assert tokens.authenticate(token) is None
    replacement = tokens.issue('session')
    assert replacement != token and tokens.authenticate(token) is None
    assert tokens.authenticate(replacement) == 'session'
    tokens.close()
    assert tokens.authenticate(replacement) is None
    assert not tokens.scopes and not tokens.active
    with pytest.raises(RuntimeError, match='shutting down'):
        tokens.issue('session')


def test_worker_process_exit_revokes_token_permanently():
    tokens = WorkerTokens()
    token = tokens.issue('session')
    tokens.active.add('session')
    process = SimpleNamespace(returncode=None)
    tokens.processes['session'] = process
    assert tokens.authenticate(token) == 'session'
    process.returncode = 0
    assert tokens.authenticate(token) is None
    process.returncode = None
    assert tokens.authenticate(token) is None
    assert not tokens.scopes


def test_worker_token_pattern_is_redacted_without_registration():
    token = 'abw_' + 'w' * 43
    assert redact({token: 'echo ' + token}) == {'[REDACTED]': 'echo [REDACTED]'}


def test_revoked_worker_tokens_are_forgotten_but_still_redacted():
    tokens = WorkerTokens()
    baseline = set(secrets._known)
    first = tokens.issue('first')
    other = tokens.issue('other')
    replacement = tokens.issue('first')
    assert first not in secrets._known
    assert replacement in secrets._known and other in secrets._known
    tokens.revoke('first')
    assert replacement not in secrets._known and other in secrets._known
    tokens.close()
    assert secrets._known == baseline
    assert redact([first, replacement, other]) == ['[REDACTED]'] * 3
    for index in range(100):
        tokens = WorkerTokens()
        tokens.issue(str(index))
        tokens.close()
    assert secrets._known == baseline
