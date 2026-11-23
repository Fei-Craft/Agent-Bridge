from __future__ import annotations

import getpass
import hashlib
import json
import logging
import os
import re
import warnings
from pathlib import Path
from typing import Any

import keyring

SERVICE = 'agent-bridge'
log = logging.getLogger(__name__)
_memory: dict[str, str] = {}
_known: set[str] = set()
_warned: set[str] = set()


def prompt_secret(prompt: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise ValueError('A secure terminal is required; refusing to echo secret input.') from None


def remember(value: str) -> str:
    if value:
        _known.add(value)
    return value


def _redact_text(value: str, short_exact: bool = True) -> str:
    value = re.sub(r'(?:abr|abu|abs)_[A-Za-z0-9_-]{43}', '[REDACTED]', value)
    for secret in sorted(_known, key=len, reverse=True):
        for form in (secret, json.dumps(secret, ensure_ascii=False)[1:-1], json.dumps(secret)[1:-1]):
            if len(secret) >= 8:
                value = value.replace(form, '[REDACTED]')
            elif short_exact and value == form:
                value = '[REDACTED]'
    return value


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            safe_key = _redact_text(key, short_exact=False) if isinstance(key, str) else key
            if safe_key != key:
                suffix = 1
                while safe_key in value or safe_key in result:
                    safe_key = f'[REDACTED-{suffix}]'
                    suffix += 1
            result[safe_key] = redact(item)
        return result
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def _warn(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(message)


def _backend():
    backend = keyring.get_keyring()
    candidates = getattr(backend, 'backends', [backend])
    for candidate in candidates:
        module = type(candidate).__module__
        if getattr(candidate, 'agent_bridge_memory_only', False) or module.startswith((
            'keyring.backends.SecretService', 'keyring.backends.kwallet',
            'keyring.backends.macOS', 'keyring.backends.Windows', 'keyring.backends.libsecret',
        )):
            return candidate
    raise RuntimeError('no secure keyring backend')


def get(name: str) -> str | None:
    try:
        value = _backend().get_password(SERVICE, name)
        if value:
            _memory[name] = remember(value)
    except Exception:
        _warn('backend', 'No usable secure keyring; secrets stay in memory only and are unavailable to other processes/restarts. No plaintext fallback is written.')
    return _memory.get(name)


def set_secret(name: str, value: str) -> bool:
    _memory[name] = remember(value)
    try:
        _backend().set_password(SERVICE, name, value)
        return True
    except Exception:
        _warn('backend', 'No usable secure keyring; secrets stay in memory only and are unavailable to other processes/restarts. No plaintext fallback is written.')
        return False


def delete(name: str) -> None:
    _memory.pop(name, None)
    try:
        backend = _backend()
        if backend.get_password(SERVICE, name) is not None:
            backend.delete_password(SERVICE, name)
    except Exception:
        _warn('delete', 'Keyring deletion unavailable; remove the entry in your OS keychain when it is unlocked.')


def hub_key(home: Path) -> str:
    return 'hub:' + hashlib.sha256(str(home.resolve()).encode()).hexdigest()


def callback_key(home: Path, task_id: str) -> str:
    return 'callback:' + hashlib.sha256(str(home.resolve()).encode()).hexdigest() + ':' + task_id


def cloud_key(provider: str, env_name: str) -> str | None:
    value = get('cloud:' + provider)
    if value:
        return value
    value = os.environ.get(env_name)
    if value:
        _warn('env:' + env_name, f'Cloud credential uses explicit environment fallback {env_name}; prefer agent-bridge secrets set {provider}.')
        return remember(value)
    return None


def migrate(home: Path) -> None:
    discovery = home / 'hub.json'
    if discovery.exists():
        try:
            info = json.loads(discovery.read_text(encoding='utf-8'))
            old_token = info.pop('token', None)
            if old_token:
                remember(old_token)
                discovery.unlink()
        except (ValueError, OSError):
            try:
                discovery.unlink(missing_ok=True)
            except OSError:
                log.warning('Could not remove legacy discovery file %s', discovery)
    directory = home / 'callback-secrets'
    if directory.is_dir():
        try:
            legacy = list(directory.iterdir())
        except OSError:
            legacy = []
            log.warning('Could not list legacy callback secrets %s', directory)
        for path in legacy:
            try:
                if path.is_file():
                    set_secret(callback_key(home, path.name), path.read_text(encoding='utf-8'))
                    path.unlink()
            except (OSError, UnicodeError):
                log.warning('Could not migrate legacy callback secret %s', path)
        try:
            directory.rmdir()
        except OSError:
            log.warning('Could not remove legacy callback directory %s', directory)
    from agent_bridge.persist import atomic_write_json, atomic_write_text

    paths = [home / 'state.json', home / 'pids.json']
    for name in ('transcripts', 'results', 'logs'):
        directory = home / name
        if directory.is_dir():
            try:
                paths.extend(path for path in directory.iterdir() if path.is_file())
            except OSError:
                log.warning('Could not list migration directory %s', directory)
    for path in paths:
        try:
            if path.is_file():
                original = path.read_text(encoding='utf-8')
                if path.suffix == '.json':
                    payload = json.loads(original)
                    if payload != redact(payload):
                        atomic_write_json(path, payload)
                    continue
                cleaned = redact(original)
                if original != cleaned:
                    atomic_write_text(path, cleaned)
        except (OSError, ValueError):
            log.warning('Could not scrub legacy file %s', path)
