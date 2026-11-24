"""Compare fresh and resumed turns through isolated local hubs; no secrets in artifacts.

Example: uv run python scripts/bench_protocols.py --agent opencode --model opencode/big-pickle --n 3
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import socket
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx2

from agent_bridge.config import load_config
from agent_bridge.transcript import WORKER_ACTIVITY_TYPES


def summarize(values: list[float]) -> dict[str, float | None]:
    return {'median': statistics.median(values) if values else None,
            'min': min(values) if values else None, 'max': max(values) if values else None}


def first_output(events: list[dict[str, Any]], started: float) -> float | None:
    times = [datetime.fromisoformat(e['ts']).timestamp() - started for e in events
             if e.get('type') in WORKER_ACTIVITY_TYPES and e.get('ts')]
    return min((t for t in times if t >= 0), default=None)


async def benchmark(args: argparse.Namespace, protocol: str, root: Path) -> dict[str, Any]:
    folder = root / protocol
    folder.mkdir()
    home = folder / 'home'
    home.mkdir()
    config = load_config().get(args.agent)
    command = config.command
    cli_command = config.cli.command or config.command
    cli_extra = config.cli.extra_args
    if args.agent == 'opencode':
        command = [*command, '--pure', '--log-level', 'ERROR', '--print-logs']
        cli_extra = [*cli_extra, '--pure', '--log-level', 'ERROR', '--print-logs']
    if args.worker_wrapper:
        prefix = [sys.executable, str(args.worker_wrapper.resolve()), '--exec']
        command, cli_command = [*prefix, *command], [*prefix, *cli_command]
    # JSON strings and arrays are valid TOML values. Never serialize the process environment.
    (home / 'agents.toml').write_text(
        '[quota]\nenabled=false\n' + f'[agents.{args.agent}]\nprotocol={json.dumps(protocol)}\n'
        + f'command={json.dumps(command)}\nstall_timeout_sec={args.timeout}\nturn_timeout_sec={args.timeout}\n'
        + f'[agents.{args.agent}.cli]\ncommand={json.dumps(cli_command)}\nextra_args={json.dumps(cli_extra)}\n',
        encoding='utf-8',
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith('AGENT_BRIDGE_')}
    env.update(AGENT_BRIDGE_HOME=str(home), AGENT_BRIDGE_SKIP_SKILL='1')
    if args.opencode_config:
        env['OPENCODE_CONFIG'] = str(args.opencode_config.resolve())
    elif args.agent == 'opencode':
        path = folder / 'opencode.json'
        path.write_text(json.dumps({'model': args.model, 'small_model': args.model,
                                   'share': 'disabled', 'autoupdate': False}), encoding='utf-8')
        env['OPENCODE_CONFIG'] = str(path)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    proc = await asyncio.create_subprocess_exec(
        sys.executable, '-m', 'agent_bridge', 'serve', '--port', str(port), env=env,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    rows = []
    try:
        async with httpx2.AsyncClient(trust_env=False, timeout=190) as client:
            for _ in range(300):
                if proc.returncode is not None:
                    raise RuntimeError('hub exited before readiness')
                if (home / 'hub.json').is_file():
                    info = json.loads((home / 'hub.json').read_text())
                    client.headers['Authorization'] = 'Bearer ' + info['token']
                    with contextlib.suppress(httpx2.HTTPError):
                        if (await client.get(url + '/v1/health')).status_code == 200:
                            break
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError('hub did not become ready')

            async def api(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
                response = await client.request(method, url + path, **kwargs)
                if response.status_code != 200:
                    raise RuntimeError(f'hub HTTP {response.status_code}; raw body suppressed')
                data = response.json()
                if not data.get('ok'):
                    raise RuntimeError('hub returned ok:false; raw body suppressed')
                return data

            async def turn(work: Path, message: str, session_id: str | None = None) -> dict[str, Any]:
                start_wall, start = time.time(), time.monotonic()
                dispatched = await api('POST', '/v1/tasks', json=dict(
                    agent=args.agent, model=args.model, cwd=str(work), message=message,
                    session_id=session_id, user_requested=True))
                deadline = start + args.timeout + 30
                while time.monotonic() < deadline:
                    waited = await api('GET', f'/v1/tasks/{dispatched["task_id"]}/wait?timeout_sec=120')
                    if not waited.get('timed_out'):
                        break
                else:
                    await api('POST', f'/v1/tasks/{dispatched["task_id"]}/cancel')
                    raise RuntimeError('benchmark turn deadline exceeded')
                result = await api('GET', f'/v1/tasks/{dispatched["task_id"]}/result')
                elapsed = time.monotonic() - start
                sessions = await api('GET', '/v1/sessions?active_only=false')
                native_id = next((s.get('native_session_id') for s in sessions['sessions']
                                  if s['session_id'] == dispatched['session_id']), None)
                events = []
                offset = 0
                while True:
                    transcript = await api('GET', f'/v1/sessions/{dispatched["session_id"]}/transcript?limit=1000&offset={offset}')
                    events.extend(transcript['events'])
                    if not transcript['has_more']:
                        break
                    offset = transcript['next_offset']
                return {'session_id': dispatched['session_id'], 'native_session_id': native_id, 'status': result['status'],
                        'seconds': elapsed, 'first_output_seconds': first_output(events, start_wall),
                        'text': result.get('result_text', '')}

            safety = ' Do not inspect environment variables, credentials, or configuration.'
            for index in range(args.n):
                work = folder / f'work-{index + 1}'
                work.mkdir()
                git = await asyncio.create_subprocess_exec('git', 'init', '-q', str(work),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                if await git.wait():
                    raise RuntimeError('git init failed')
                row: dict[str, Any] = {'iteration': index + 1, 'first_ok': False, 'resume_ok': False}
                try:
                    first = await turn(work, 'Use a file or shell tool to create bench.txt containing exactly '
                                       'benchmark-hi followed by a newline, then answer BENCH_OK.' + safety)
                    first_ok = (first['status'] == 'completed' and 'BENCH_OK' in first.pop('text')
                                and (work / 'bench.txt').read_bytes() == b'benchmark-hi\n')
                    first.pop('text', None)
                    row.update(first_ok=first_ok, first=first, resume=None)
                    if first_ok:
                        second = await turn(work, 'Read bench.txt and confirm it still contains benchmark-hi. '
                                            'Answer BENCH_RESUME_OK without modifying any file.' + safety,
                                            first['session_id'])
                        resume_ok = (second['status'] == 'completed' and 'BENCH_RESUME_OK' in second.pop('text')
                                     and bool(first['native_session_id'])
                                     and second['native_session_id'] == first['native_session_id'])
                        second.pop('text', None)
                        row.update(resume_ok=resume_ok, resume=second)
                except Exception as exc:
                    row['error_type'] = type(exc).__name__
                rows.append(row)
                print(json.dumps({'protocol': protocol, **row}), flush=True)
    finally:
        if proc.returncode is None:
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=30)
        except TimeoutError:
            proc.kill()
            await proc.wait()
    valid = [row for row in rows if row['first_ok']]
    resumed = [row for row in valid if row['resume_ok']]
    return {'protocol': protocol, 'attempts': args.n, 'success_count': len(valid),
            'resume_success_count': len(resumed),
            'total_turn_seconds': summarize([row['first']['seconds'] for row in valid]),
            'time_to_first_output_seconds': summarize([row['first']['first_output_seconds'] for row in valid
                                                       if row['first']['first_output_seconds'] is not None]),
            'resume_turn_seconds': summarize([row['resume']['seconds'] for row in resumed]), 'samples': rows}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', default='opencode')
    parser.add_argument('--model', required=True)
    parser.add_argument('--protocols', nargs='+', choices=['acp', 'cli'], default=['acp', 'cli'])
    parser.add_argument('--n', type=int, default=3)
    parser.add_argument('--timeout', type=int, default=900)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--opencode-config', type=Path)
    parser.add_argument('--worker-wrapper', type=Path, help='Python wrapper accepting --exec COMMAND ARGS')
    args = parser.parse_args()
    if args.n < 1 or args.timeout < 1:
        parser.error('--n and --timeout must be positive')
    root = (args.output_dir or Path.home() / 'agent-bridge-bench' / str(time.time_ns())).resolve()
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    reports = [await benchmark(args, protocol, root) for protocol in args.protocols]
    payload = {'agent': args.agent, 'model': args.model, 'reports': reports,
               'first_output_definition': 'first worker transcript event (including metadata/tool events), not first token',
               'artifact_directory': str(root)}
    (root / 'benchmark.json').write_text(json.dumps(payload, indent=2), encoding='utf-8')
    for report in reports:
        print(json.dumps({k: v for k, v in report.items() if k != 'samples'}), flush=True)
    print('Artifacts:', root)
    return 0 if all(r['success_count'] == args.n and r['resume_success_count'] == args.n for r in reports) else 1


if __name__ == '__main__':
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:
        print(json.dumps({'benchmark_error_type': type(exc).__name__, 'raw_details_suppressed': True}))
        raise SystemExit(1) from None
