"""A task's working folder reaches every executor; existing tasks keep theirs."""
import json
from pathlib import Path
import sys

import pytest

from conftest import FakeChannel, inbound
from eivoh.executors.claude import ClaudeExecutor, ClaudeStreamExecutor
from eivoh.executors.codex_app import CodexAppExecutor
from eivoh.gateway import Gateway
from eivoh.storage import Storage

FIXTURES = Path(__file__).parent / 'fixtures'


def fake(tmp_path, name):
    binary = tmp_path / ('bin-' + name)
    binary.write_text('#!' + sys.executable + '\n' + (FIXTURES / name).read_text().split('\n', 1)[1])
    binary.chmod(0o700)
    return binary.resolve()


def meta(records):
    return next(record.payload for record in records if record.stream == 'meta')


def session(records):
    return next(e.session_id for r in records for e in r.events if e.kind == 'session_started')


async def test_cli_turn_runs_in_task_folder(tmp_path):
    other = tmp_path / 'project'
    other.mkdir()
    executor = ClaudeExecutor(tmp_path, fake(tmp_path, 'fake_claude.py'), grace=0.1)
    first = [r async for r in executor.start(1, 'first', cwd=str(other))]
    [r async for r in executor.resume(2, session(first), 'second', cwd=str(other))]
    default = [r async for r in executor.start(3, 'default')]
    assert meta(first)['cwd'] == str(other) and meta(default)['cwd'] == str(tmp_path)
    assert [json.loads(line)['prompt'] for line in (other / 'prompts.jsonl').read_text().splitlines()] == ['first', 'second']
    assert [json.loads(line)['prompt'] for line in (tmp_path / 'prompts.jsonl').read_text().splitlines()] == ['default']


@pytest.mark.parametrize('adapter', [ClaudeStreamExecutor, CodexAppExecutor])
async def test_interactive_turn_runs_in_task_folder(tmp_path, adapter):
    other = tmp_path / 'project'
    other.mkdir()
    executor = adapter(tmp_path, fake(tmp_path, 'fake_interactive.py'), grace=.02)
    try:
        first = [r async for r in executor.start(1, 'first', cwd=str(other))]
        second = [r async for r in executor.resume(2, session(first), 'second', cwd=str(other))]
        default = [r async for r in executor.start(3, 'default')]
    finally:
        await executor.shutdown()
    assert meta(first)['cwd'] == meta(second)['cwd'] == str(other) and meta(default)['cwd'] == str(tmp_path)
    assert (other / 'wire-input.jsonl').exists() and (tmp_path / 'wire-input.jsonl').exists()
    if adapter is CodexAppExecutor:
        def threads(path):
            rows = [json.loads(line) for line in (path / 'wire-input.jsonl').read_text().splitlines()]
            return [(r['method'], r['params']['cwd']) for r in rows if r.get('method', '').startswith('thread/')]
        assert threads(other) == [('thread/start', str(other)), ('thread/resume', str(other))]
        assert threads(tmp_path) == [('thread/start', str(tmp_path))]


@pytest.mark.parametrize('adapter', [ClaudeExecutor, ClaudeStreamExecutor, CodexAppExecutor])
async def test_missing_task_folder_is_an_error_event(tmp_path, adapter):
    missing = tmp_path / 'gone'
    executor = adapter(tmp_path, fake(tmp_path, 'fake_interactive.py'), grace=.02)
    records = [r async for r in executor.resume(1, 'sid', 'x', cwd=str(missing))]
    assert [(e.kind, e.data['message']) for r in records for e in r.events] == [
        ('error', f'Working folder {missing} of this task does not exist')]
    assert not executor.running and not executor.starting


async def test_gateway_keeps_each_task_in_its_folder(tmp_path):
    first_home, second_home, legacy = tmp_path / 'home1', tmp_path / 'home2', tmp_path / 'legacy'
    for path in (first_home, second_home, legacy):
        path.mkdir()
    executor = ClaudeExecutor(tmp_path, fake(tmp_path, 'fake_claude.py'), grace=0.1)
    prompts = lambda path: [json.loads(line)['prompt'] for line in (path / 'prompts.jsonl').read_text().splitlines()]

    storage = Storage(tmp_path / 'state.db', task_workdir=first_home, legacy_workdir=legacy)
    gateway = Gateway(storage, executor, {'fake': FakeChannel()}, edit_interval=0.03, typing_interval=0.03)
    await gateway.start()
    await gateway.receive(inbound(1, 'old task'))
    await gateway.idle()
    await gateway.shutdown()
    storage.close()

    # A later home applies to new tasks only; the first task resumes where it began.
    storage = Storage(tmp_path / 'state.db', task_workdir=second_home, legacy_workdir=legacy)
    gateway = Gateway(storage, executor, {'fake': FakeChannel()}, edit_interval=0.03, typing_interval=0.03)
    await gateway.start()
    try:
        await gateway.receive(inbound(2, 'new task', chat='20'))
        await gateway.receive(inbound(3, 'old task again'))
        await gateway.idle()
    finally:
        await gateway.shutdown()
    assert [storage.turn_for(i)['status'] for i in (1, 2, 3)] == ['completed'] * 3
    assert prompts(first_home)[0] == 'old task' and prompts(first_home)[1].endswith('old task again')
    assert json.loads((first_home / 'prompts.jsonl').read_text().splitlines()[1])['resume']
    assert prompts(second_home) == ['new task']
    assert not (legacy / 'prompts.jsonl').exists() and not (tmp_path / 'prompts.jsonl').exists()
    storage.close()


async def test_gateway_reports_missing_task_folder(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    channel = FakeChannel()
    storage = Storage(tmp_path / 'state.db', task_workdir=home)
    gateway = Gateway(storage, ClaudeExecutor(tmp_path, fake(tmp_path, 'fake_claude.py'), grace=0.1),
                      {'fake': channel}, edit_interval=0.03, typing_interval=0.03)
    home.rmdir()
    await gateway.start()
    try:
        await gateway.receive(inbound(1, 'work'))
        await gateway.idle()
    finally:
        await gateway.shutdown()
    turn = storage.turn_for(1)
    assert turn['status'] == 'failed' and turn['error'] == f'Working folder {home} of this task does not exist'
    assert any('does not exist' in text for _, text in channel.sent + [(c, t) for c, _, t in channel.edits])
    storage.close()
