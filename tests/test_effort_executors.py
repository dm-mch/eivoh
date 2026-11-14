import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conftest import FakeChannel, inbound, until
from test_steering import live_rig, ready
from eivoh.gateway import Gateway
from eivoh.storage import Storage
from eivoh.executors.claude import ClaudeExecutor, ClaudeStreamExecutor
from eivoh.executors.codex_app import CodexAppExecutor
from eivoh.titles import CodexTitleExecutor


@pytest.mark.parametrize('adapter', [ClaudeExecutor, ClaudeStreamExecutor])
@pytest.mark.parametrize('configured', [('--effort', 'low'), ('--effort=low',),
                                       ('--effort', 'low', '--effort=medium')])
def test_claude_task_effort_wins_config_without_mutating_it(tmp_path, adapter, configured):
    extra = ('--settings', '{}', *configured, '--debug')
    executor = adapter(tmp_path, extra_args=extra)
    defaults = executor.command(None)
    assert all(arg in defaults for arg in extra)
    for session in (None, 'same-session'):
        args = executor.command(session, 'opus', 'high')
        assert args.count('--effort') == 1
        assert args[args.index('--effort') + 1] == 'high'
        assert '--effort=low' not in args and '--effort=medium' not in args
        assert 'low' not in args and '--settings' in args and '--debug' in args
    assert executor.extra_args == extra and executor.command(None) == defaults


async def test_cli_start_resume_journal_effort(rig):
    _, _, executor, _, path = rig
    first = [record async for record in executor.start(101, 'first', model='opus', effort='low')]
    sid = next(event.session_id for record in first for event in record.events if event.kind == 'session_started')
    second = [record async for record in executor.resume(102, sid, 'second', model='opus', effort='high')]
    calls = [json.loads(line) for line in (path / 'prompts.jsonl').read_text().splitlines()]
    for records, call, effort in zip((first, second), calls, ('low', 'high')):
        metadata = next(record.payload for record in records if record.stream == 'meta')
        assert metadata['requested_effort'] == effort
        assert metadata['requested_effort_source'] == 'turn'
        assert call['args'][call['args'].index('--effort') + 1] == effort


@pytest.mark.parametrize('session', [None, 'original-thread'])
@pytest.mark.parametrize('effort', [None, 'low', 'high'])
async def test_codex_turn_request_effort_and_model(tmp_path, session, effort):
    executor = CodexAppExecutor(tmp_path, model='configured-model')
    executor.rpc = AsyncMock(side_effect=[{}, {'thread': {'id': session or 'new-thread'}}, {'turn': {'id': 'turn'}}])
    run = SimpleNamespace(session_id=session, cwd=None, write=AsyncMock(), ready=SimpleNamespace(set=lambda: None))
    await executor.initialize(run, 'prompt', 'gpt-6.1-sol', effort)
    thread_call, turn_call = executor.rpc.call_args_list[1:]
    assert thread_call.args[1] == ('thread/resume' if session else 'thread/start')
    assert thread_call.args[2]['model'] == 'gpt-6.1-sol'
    assert turn_call.args[1] == 'turn/start'
    assert turn_call.args[2] == {'threadId': session or 'new-thread', 'model': 'gpt-6.1-sol',
                               'input': [{'type': 'text', 'text': 'prompt'}],
                               **({'effort': effort} if effort is not None else {})}


@pytest.mark.parametrize('adapter', [ClaudeStreamExecutor, CodexAppExecutor])
async def test_interactive_start_resume_effort_is_journaled(tmp_path, adapter):
    source = Path(__file__).parent / 'fixtures' / 'fake_interactive.py'
    binary = tmp_path / 'fake-interactive'
    binary.write_text('#!' + sys.executable + '\n' + source.read_text().split('\n', 1)[1])
    binary.chmod(0o700)
    executor = adapter(tmp_path, binary, grace=.02)
    try:
        first = [record async for record in executor.start(101, 'first', model='model', effort='low')]
        sid = next(event.session_id for record in first for event in record.events if event.kind == 'session_started')
        second = [record async for record in executor.resume(102, sid, 'second', model='model', effort='high')]
        for records, effort in zip((first, second), ('low', 'high')):
            metadata = next(record.payload for record in records if record.stream == 'meta')
            assert metadata['requested_effort'] == effort and metadata['requested_effort_source'] == 'turn'
        if adapter is CodexAppExecutor:
            requests = [json.loads(line) for line in (tmp_path / 'wire-input.jsonl').read_text().splitlines()]
            assert [r['params']['effort'] for r in requests if r.get('method') == 'turn/start'] == ['low', 'high']
    finally:
        await executor.shutdown()


def test_title_codex_keeps_fixed_low_effort(tmp_path):
    executor = CodexTitleExecutor(tmp_path)
    args = executor.command(None, effort='high')
    assert 'model_reasoning_effort="low"' in args
    assert 'model_reasoning_effort="high"' not in args


async def test_gateway_effort_barrier_and_restart_use_original_launch(live_rig):
    gateway, storage, adapter, channel, path = live_rig
    await gateway.receive(inbound(1, '/effort low'))
    await gateway.receive(inbound(2, 'held'))
    await ready(live_rig)
    await until(lambda: storage.session(1, storage.default_executor) is not None)
    first = storage.turn_for(2)
    assert first['effort'] == 'low'
    await gateway.receive(inbound(3, '/effort high'))
    await gateway.receive(inbound(4, 'after effort change'))
    await gateway.receive(inbound(5, 'later clarification'))
    assert storage.steering(4) is storage.steering(5) is None
    assert storage.turn_for(2)['effort'] == 'low'
    assert storage.turn_for(4)['executor'] is storage.turn_for(4)['effort'] is None
    await gateway.shutdown()
    reopened = Storage(path / 'state.db', default_executor=storage.default_executor)
    replacement = Gateway(reopened, adapter, {'fake': FakeChannel()}, edit_interval=.01)
    try:
        (path / 'release').touch()
        await replacement.start()
        await replacement.idle()
        assert reopened.turn_for(2)['status'] == 'completed'
        assert reopened.turn_for(2)['effort'] == 'low'
        assert reopened.turn_for(2)['recovery_count'] == 1
        assert reopened.turn_for(4)['status'] == reopened.turn_for(5)['status'] == 'completed'
        assert reopened.turn_for(4)['effort'] == reopened.turn_for(5)['effort'] == 'high'
        launches = [json.loads(row[0]) for row in reopened.db.execute(
            "SELECT payload FROM raw_event WHERE turn_id=? AND stream='meta' AND json_extract(payload,'$.executor') IS NOT NULL ORDER BY id",
            (first['id'],))]
        assert [record['requested_effort'] for record in launches] == ['low', 'low']
        assert all(record['requested_effort_source'] == 'turn' for record in launches)
    finally:
        await replacement.shutdown()
        reopened.close()
