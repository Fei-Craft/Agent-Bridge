from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('bench_protocols', Path(__file__).parents[1] / 'scripts' / 'bench_protocols.py')
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)


def test_summarize():
    assert bench.summarize([3.0, 1.0, 2.0]) == {'median': 2.0, 'min': 1.0, 'max': 3.0}
    assert bench.summarize([]) == {'median': None, 'min': None, 'max': None}


def test_first_worker_output_excludes_prior_turn_and_bridge_events():
    def event(kind, timestamp):
        return {'type': kind, 'ts': datetime.fromtimestamp(timestamp, UTC).isoformat()}
    events = [event('message_chunk', 99), event('prompt_sent', 100), event('raw', 102), event('message_chunk', 103)]
    assert bench.first_output(events, 100) == 2
    assert bench.first_output([], 100) is None
    with pytest.raises(ValueError):
        bench.first_output([{'type': 'raw', 'ts': 'invalid'}], 100)
