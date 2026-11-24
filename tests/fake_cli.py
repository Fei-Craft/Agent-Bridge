from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--resume')
parser.add_argument('--model')
parser.add_argument('--effort')
parser.add_argument('prompt', nargs='?')
args = parser.parse_args()
prompt = args.prompt if args.prompt is not None else sys.stdin.read()


def emit(value):
    print(json.dumps(value), flush=True)


emit({'type': 'session', 'session_id': args.resume or 'native-123'})
if prompt in {'stall', 'cancel'}:
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
    Path('child.pid').write_text(str(child.pid))
    time.sleep(120)
elif prompt == 'fail':
    print('Authorization: Bearer never-persist-this', file=sys.stderr, flush=True)
    emit({'type': 'error', 'message': 'worker failed'})
    raise SystemExit(7)
elif prompt == 'empty':
    pass
elif prompt == 'malformed':
    print('not JSON', flush=True)
elif prompt == 'heartbeat':
    for _ in range(5):
        time.sleep(0.3)
        emit({'type': 'delta', 'text': '.'})
else:
    Path('cli.txt').write_text('created by CLI\n')
    emit({'type': 'result', 'result': json.dumps({
        'prompt': prompt, 'resume': args.resume, 'model': args.model, 'effort': args.effort,
        'caller': os.environ.get('AGENT_BRIDGE_CALLER_SESSION'),
        'hub_url': os.environ.get('AGENT_BRIDGE_HUB_URL'),
    }), 'model': args.model, 'usage': {'input_tokens': 3, 'output_tokens': 2}})
