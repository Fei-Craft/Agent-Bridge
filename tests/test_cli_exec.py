from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import psutil
import pytest

from agent_bridge.adapters import build_adapter
from agent_bridge.adapters.cli import CliAdapter
from agent_bridge.cli_exec import CliTurnState, build_cli_argv
from agent_bridge.config import AgentConfig, CliConfig, EnvConfig, load_config
from agent_bridge.models import Session, Task
from agent_bridge.probes import command_exists, probe_agent
from agent_bridge.registry import Registry
from agent_bridge.transcript import read_events
from agent_bridge.worker_env import hub_worker_context

FAKE = Path(__file__).with_name('fake_cli.py')


def configuration(**kwargs):
    return AgentConfig(name='cli-test', protocol='cli', command=[sys.executable, str(FAKE)],
                       revivable=True, cli=CliConfig(parser='jsonl', resume_args=['--resume', '{session}'],
                           model_args=['--model', '{model}'], effort_args=['--effort', '{effort}'], **kwargs))


def session_and_task(tmp_path, message='hello'):
    session = Session(session_id='cli-session', agent='cli-test', cwd=str(tmp_path))
    task = Task(task_id='cli-task', session_id=session.session_id, agent=session.agent,
                message=message, cwd=str(tmp_path))
    return session, task


@pytest.mark.parametrize('parser', ['claude', 'cursor'])
def test_stream_json_snapshot_and_delta(parser):
    state = CliTurnState(parser)
    events = [
        {'type': 'system', 'session_id': 'native'},
        {'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta': {'text': 'hello'}}},
        {'type': 'assistant', 'message': {'model': 'native-model', 'content': [{'type': 'text', 'text': 'hello'}]}},
        {'type': 'result', 'result': 'hello', 'usage': {'input_tokens': 2}},
    ]
    for event in events:
        state.feed(json.dumps(event))
    assert state.text == 'hello'
    assert state.chunks == ['hello']
    assert state.session_id == 'native'
    assert state.model == 'native-model'
    assert state.usage == {'input_tokens': 2}
    state.feed(json.dumps({'type': 'result', 'is_error': True, 'result': 'auth required'}))
    assert state.error == 'auth required'


def test_opencode_parser():
    state = CliTurnState('opencode')
    assert state.feed(json.dumps({'type': 'step_start', 'sessionID': 'ses_123'})) == 'raw'
    assert state.feed(json.dumps({'type': 'tool_use', 'part': {'tool': 'bash'}})) == 'tool_call'
    state.feed(json.dumps({'type': 'text', 'part': {'text': 'answer'}}))
    for _ in range(2):
        state.feed(json.dumps({'type': 'step_finish', 'part': {'tokens': {'input': 2, 'output': 3,
                                                                       'cache': {'read': 5}}, 'cost': 0.1}}))
    assert state.text == 'answer'
    assert state.session_id == 'ses_123'
    assert state.usage == {'input': 4, 'output': 6, 'cache': {'read': 10}, 'cost': 0.2}
    state.feed('{"type":"error","error":{"message":"quota exceeded"}}')
    assert 'quota exceeded' in state.error


@pytest.mark.parametrize('parser', ['text', 'jsonl', 'claude', 'cursor', 'opencode'])
def test_parser_empty_and_malformed(parser):
    state = CliTurnState(parser)
    assert state.text == '' and state.session_id is None and state.error is None
    assert state.feed('not json\n') == ('message_chunk' if parser == 'text' else 'raw')
    assert state.text == ('not json\n' if parser == 'text' else '')
    if parser != 'text':
        state.feed('[]')
        assert state.events == 0


def test_jsonl_and_text_parsers():
    state = CliTurnState('jsonl')
    state.feed('{"type":"delta","text":"partial"}')
    state.feed('{"type":"result","result":"final","session_id":"s","model":"m","usage":{"tokens":4}}')
    assert state.text == 'final' and state.session_id == 's' and state.model == 'm'
    assert state.usage == {'tokens': 4}
    state.feed('{"type":"error","error":"failure"}')
    assert state.error == 'failure'
    text = CliTurnState('text')
    text.feed('hello ')
    text.feed('world\n')
    assert text.text == 'hello world\n'


def test_cli_argv_and_missing_options(tmp_path):
    session, task = session_and_task(tmp_path, 'hello; $(do-not-execute)')
    cfg = configuration(extra_args=['--cwd={cwd}'])
    task.model, task.effort = 'my/model', 'high'
    cmd = build_cli_argv(cfg, session, task)
    assert cmd[-1] == task.message
    assert '--model' in cmd and '--effort' in cmd
    assert '--resume' not in cmd
    session.native_session_id = 'native'
    assert '--resume' in build_cli_argv(cfg, session, task)
    cfg.cli.model_args = []
    with pytest.raises(ValueError, match='model_args'):
        build_cli_argv(cfg, session, task)
    task.model, task.effort = None, None
    cfg.cli.resume_args = []
    with pytest.raises(ValueError, match='resume_args'):
        build_cli_argv(cfg, session, task)
    session.native_session_id, session.turns = None, 1
    cfg.cli.resume_args = ['--resume', '{session}']
    with pytest.raises(ValueError, match='cannot resume'):
        build_cli_argv(cfg, session, task)


@pytest.mark.parametrize('prompt_args', [['{prompt}'], ['-p', '{prompt}'], ['--dir={prompt}']])
def test_cli_rejects_unprotected_prompt_slots(tmp_path, prompt_args):
    session, task = session_and_task(tmp_path, '--dir=/')
    with pytest.raises(ValueError, match='prompt must use stdin'):
        build_cli_argv(configuration(prompt_args=prompt_args), session, task)


@pytest.mark.parametrize('stdin', [False, True])
async def test_option_like_prompt_reaches_cli_as_text(tmp_path, stdin):
    cfg = configuration(prompt_stdin=stdin, prompt_args=[] if stdin else ['--', '{prompt}'])
    session, task = session_and_task(tmp_path, '--dir=/')
    adapter = build_adapter(cfg, tmp_path / 'home')
    try:
        result = await adapter.run_turn(session, task)
        assert json.loads(result.text)['prompt'] == '--dir=/'
    finally:
        await adapter.shutdown(session)


@pytest.mark.parametrize('preset', ['opencode', 'claude', 'cursor'])
def test_preset_prompt_transport(tmp_path, monkeypatch, preset):
    cfg = load_config(tmp_path).get(preset)
    monkeypatch.setattr('agent_bridge.cli_exec.cli_command', lambda agent: agent.cli.command)
    session, task = session_and_task(tmp_path, '--dir=/')
    cmd = build_cli_argv(cfg, session, task)
    if preset == 'claude':
        assert cfg.cli.prompt_stdin and task.message not in cmd
    else:
        assert cmd[-2:] == ['--', '--dir=/']


@pytest.mark.parametrize('field', ['model', 'effort'])
def test_cli_validates_mutated_selections(tmp_path, field):
    session, task = session_and_task(tmp_path)
    setattr(task, field, '--dir=/')
    with pytest.raises(ValueError, match=field):
        build_cli_argv(configuration(), session, task)


@pytest.mark.asyncio
async def test_cli_turn_resume_stdin_and_hub_env(tmp_path):
    home = tmp_path / 'home'
    adapter = build_adapter(configuration(prompt_stdin=True, prompt_args=[]), home)
    assert isinstance(adapter, CliAdapter) and not adapter.resident
    session, task = session_and_task(tmp_path)
    task.model, task.effort = 'model/name', 'high'
    token = hub_worker_context.set({'AGENT_BRIDGE_HUB_URL': 'http://hub',
        'AGENT_BRIDGE_HUB_TOKEN': 'private-token', 'AGENT_BRIDGE_CALLER_SESSION': session.session_id})
    try:
        result = await adapter.run_turn(session, task)
        assert result.stop_reason == 'end_turn'
        assert result.native_session_id == 'native-123'
        assert result.usage['input_tokens'] == 3
        output = json.loads(result.text)
        assert output == dict(prompt='hello', resume=None, model='model/name', effort='high',
                              caller=session.session_id, hub_url='http://hub')
        result = await adapter.run_turn(session, task)
        assert json.loads(result.text)['resume'] == 'native-123'
        assert (tmp_path / 'cli.txt').read_text() == 'created by CLI\n'
        events = read_events(session.session_id, home)
        assert any(e['type'] == 'message_chunk' for e in events)
        assert events[-1]['type'] == 'turn_end'
    finally:
        hub_worker_context.reset(token)
        await adapter.shutdown(session)


@pytest.mark.asyncio
async def test_cli_nonzero_and_empty_result(tmp_path):
    adapter = CliAdapter(configuration(), tmp_path / 'home')
    session, task = session_and_task(tmp_path, 'fail')
    result = await adapter.run_turn(session, task)
    assert result.stop_reason == 'error' and result.error == 'worker failed'
    assert task.failure.exit_code == 7
    assert 'never-persist-this' not in task.failure.stderr_summary
    task.message = 'empty'
    result = await adapter.run_turn(session, task)
    assert result.stop_reason == 'end_turn' and result.text == ''


@pytest.mark.asyncio
async def test_cli_cancel_process_tree(tmp_path):
    adapter = CliAdapter(configuration(), tmp_path / 'home')
    session, task = session_and_task(tmp_path, 'cancel')
    running = asyncio.create_task(adapter.run_turn(session, task))
    for _ in range(200):
        if (tmp_path / 'child.pid').exists():
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail('fake CLI did not start')
    pid = int((tmp_path / 'child.pid').read_text())
    await adapter.cancel(session)
    result = await asyncio.wait_for(running, 10)
    assert result.stop_reason == 'cancelled'
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    assert not adapter._procs


@pytest.mark.asyncio
async def test_cli_text_stream_and_invalid_json_output(tmp_path):
    cfg = AgentConfig(name='text-cli', protocol='cli', command=[sys.executable, '-c',
        'import os,time; os.write(1,b"\\xe4"); time.sleep(0.05); os.write(1,b"\\xbd\\xa0")'],
        cli=CliConfig(parser='text', prompt_args=[]))
    adapter = CliAdapter(cfg, tmp_path / 'home')
    session, task = session_and_task(tmp_path)
    result = await adapter.run_turn(session, task)
    assert result.text == '你' and result.stop_reason == 'end_turn'
    cfg.cli.parser = 'jsonl'
    result = await adapter.run_turn(session, task)
    assert result.stop_reason == 'error' and 'no JSON object events' in result.error


@pytest.mark.asyncio
async def test_cli_probe_uses_cli_command(tmp_path):
    cfg = AgentConfig(name='opencode', protocol='cli', command=['nonexistent-acp-command'],
                      cli=CliConfig(command=[sys.executable], parser='text'))
    assert command_exists(cfg)
    assert (await probe_agent(cfg))['available']
    cfg.cli.command = ['also-nonexistent-command']
    assert not command_exists(cfg)
    assert not (await probe_agent(cfg))['available']


@pytest.mark.asyncio
async def test_cli_final_environment_deny_gate(tmp_path, monkeypatch):
    original = asyncio.create_subprocess_exec
    seen = []

    async def capture(*args, **kwargs):
        seen.append(kwargs['env'])
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', capture)
    adapter = CliAdapter(configuration(), tmp_path / 'home', EnvConfig(deny=['PWD', 'PRIVATE_*']))
    monkeypatch.setenv('PRIVATE_KEY', 'must-not-reach-worker')
    session, task = session_and_task(tmp_path)
    await adapter.run_turn(session, task)
    assert 'PWD' not in seen[0] and 'PRIVATE_KEY' not in seen[0]


@pytest.mark.asyncio
async def test_cli_registry_stall_and_files_changed(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_BRIDGE_ENABLE_FAKE', '1')
    registry = Registry.create(tmp_path / 'home')
    registry.config.agents['cli-test'] = configuration()
    registry.config.agents['cli-test'].stall_timeout_sec = 1
    await registry.start()
    try:
        dispatched = await registry.dispatch_task(agent='cli-test', message='hello', cwd=str(tmp_path))
        result = await registry.wait_task(dispatched['task_id'], timeout_sec=10)
        assert result['status'] == 'completed'
        assert 'cli.txt' in result['files_changed']
        dispatched = await registry.dispatch_task(agent='cli-test', message='heartbeat', cwd=str(tmp_path))
        result = await registry.wait_task(dispatched['task_id'], timeout_sec=10)
        assert result['status'] == 'completed'
        dispatched = await registry.dispatch_task(agent='cli-test', message='stall', cwd=str(tmp_path))
        result = await registry.wait_task(dispatched['task_id'], timeout_sec=15)
        assert result['status'] == 'failed' and result['stop_reason'] == 'stalled'
    finally:
        await registry.stop()


@pytest.mark.parametrize('agent', ['opencode', 'claude', 'cursor'])
def test_cli_protocol_only_overlay(tmp_path, agent):
    (tmp_path / 'agents.toml').write_text(f'[agents.{agent}]\nprotocol="cli"\n')
    cfg = load_config(tmp_path).agents[agent]
    assert cfg.protocol == 'cli' and cfg.cli.command and cfg.cli.resume_args
    assert cfg.command != cfg.cli.command
    (tmp_path / 'agents.toml').write_text(f'[agents.{agent}]\nprotocol="cli"\n[agents.{agent}.cli]\nextra_args=["custom"]\n')
    cfg = load_config(tmp_path).agents[agent]
    assert cfg.cli.extra_args == ['custom'] and cfg.cli.resume_args
