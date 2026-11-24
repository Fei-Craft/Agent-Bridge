from __future__ import annotations

import asyncio
import re
import shutil

from agent_bridge.hub_client import HubClient


async def run_tunnel(client: HubClient, *, name: str | None = None, public_url: str | None = None) -> None:
    executable = shutil.which('cloudflared')
    if not executable:
        raise RuntimeError('cloudflared is not installed; install the official Cloudflare release binary first')
    status = await client.call('get_remote_status')
    if not status.get('ok') or not status.get('enabled'):
        raise ValueError('enable the separate remote gateway before starting a tunnel')
    host = f'[{status["host"]}]' if ':' in status['host'] else status['host']
    command = [executable, 'tunnel', '--url', f'http://{host}:{status["port"]}']
    if name:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
            raise ValueError('invalid tunnel name')
        command.extend(['run', name])
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    configured = False
    try:
        if public_url:
            result = await client.call('set_remote_public_url', public_url=public_url)
            if not result.get('ok'):
                raise RuntimeError('hub rejected tunnel public_url')
            configured = True
            print(f'Remote gateway tunnel URL configured: {result["public_url"]}', flush=True)
        assert process.stdout is not None
        while line := await process.stdout.readline():
            match = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com\b', line.decode(errors='replace'))
            if match and not configured:
                result = await client.call('set_remote_public_url', public_url=match[0])
                if not result.get('ok'):
                    raise RuntimeError('hub rejected tunnel public_url')
                configured = True
                print(f'Remote gateway tunnel URL configured: {match[0]}', flush=True)
        code = await process.wait()
        if code or not configured:
            raise RuntimeError('cloudflared exited or did not publish a tunnel URL; named tunnels may need --public-url')
    finally:
        if process.returncode is None:
            process.terminate()
            await process.wait()
