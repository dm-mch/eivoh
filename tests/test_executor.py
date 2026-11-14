import asyncio
import os
import json
from pathlib import Path

import pytest

from eivoh.executors.claude import ClaudeExecutor, parse_line, process_table
from eivoh.config import Config


def test_real_sanitized_stream():
    lines = (Path(__file__).parent / 'fixtures/claude_stream.jsonl').read_text().splitlines(keepends=True)
    records = [parse_line(line) for line in lines]
    assert [r.raw_line for r in records] == lines
    events = [e for r in records for e in r.events]
    assert {'session_started', 'text_delta', 'message', 'tool_started', 'tool_finished', 'usage', 'turn_completed'} <= {e.kind for e in events}
    assert events[0].session_id == 'session-placeholder'
    assert all(e.raw is r.payload for r in records for e in r.events)
    assert any(r.payload.get('type') == 'rate_limit_event' and not r.events for r in records)
    assert events[-1].data['status'] == 'completed'


def test_parser_failures_and_denials():
    assert parse_line('bad\n').events[0].kind == 'error'
    assert not parse_line('[]\n').events
    record = parse_line(json.dumps({'type': 'result', 'is_error': True, 'permission_denials': [{'tool_name': 'Bash'}], 'result': 'refused'}))
    assert [e.kind for e in record.events] == ['usage', 'approval_requested', 'error', 'turn_completed']
    assert record.events[-1].data['status'] == 'failed'


def test_model_observations_preserve_source_and_actual_ids(tmp_path):
    metadata = ClaudeExecutor(tmp_path, model='opus').model_metadata()
    assert metadata['requested_model'] == 'opus'
    assert metadata['requested_model_source'] == 'executor_config'
    assert metadata['observed_model'] is None
    init = parse_line('{"type":"system","subtype":"init","session_id":"s","model":"init-model"}')
    message = parse_line('{"type":"assistant","message":{"model":"response-model","content":[]}}')
    assert init.events[0].data['observed_model'] == 'init-model'
    assert init.events[0].data['observed_model_source'] == 'system.init'
    assert message.events[0].data['observed_model'] == 'response-model'
    assert message.events[0].data['observed_model_source'] == 'assistant.message'
    missing = parse_line('{"type":"assistant","message":{"content":[]}}')
    assert missing.events[0].data['observed_model'] is None
    subagent = parse_line('{"type":"assistant","parent_tool_use_id":"tool-1","message":{"model":"child-model","content":[]}}')
    assert not subagent.events
    subagent_init = parse_line('{"type":"system","subtype":"init","parent_tool_use_id":"tool-1","session_id":"child-session","model":"child-model"}')
    assert not subagent_init.events


async def test_environment_stdin_and_resume(rig, monkeypatch):
    _, _, executor, _, path = rig
    for key in ('CLAUDECODE', 'CLAUDE_PID', 'CLAUDE_TEST', 'ANTHROPIC_API_KEY', 'ANTHROPIC_TEST'):
        monkeypatch.setenv(key, 'test-only')
    records = [r async for r in executor.start(100, 'first\nline')]
    sid = next(e.session_id for r in records for e in r.events if e.kind == 'session_started')
    await asyncio.sleep(0)
    _ = [r async for r in executor.resume(101, sid, 'second')]
    logs = [json.loads(line) for line in (path / 'prompts.jsonl').read_text().splitlines()]
    assert logs[0]['prompt'] == 'first\nline'
    assert logs[1]['resume'] == sid
    assert all(not row['env_keys'] for row in logs)
    assert '--include-partial-messages' in logs[0]['args']
    assert '--bare' not in logs[0]['args']
    with pytest.raises(ValueError):
        executor.resume(102, '--bad', 'x')


async def test_config_args_reach_fake_executor_on_start_and_resume(rig, monkeypatch):
    _, _, executor, _, path = rig
    settings = '{"enabledPlugins":{"example-plugin@example-marketplace":false}}'
    monkeypatch.setenv('EIVOH_TELEGRAM_TOKEN', 'test-only')
    monkeypatch.setenv('EIVOH_OWNER_ID', '123')
    monkeypatch.setenv('EIVOH_WORKDIR', str(path))
    monkeypatch.setenv('EIVOH_HOME', str(path))
    monkeypatch.setenv('EIVOH_CLAUDE_ARGS', f"--settings '{settings}'")
    config = Config.from_env()
    adapter = ClaudeExecutor(config.workdir, executor.binary, model='test-model',
                             extra_args=config.claude_args, grace=0.1)
    records = [r async for r in adapter.start(100, 'first')]
    sid = next(e.session_id for r in records for e in r.events if e.kind == 'session_started')
    _ = [r async for r in adapter.resume(101, sid, 'second')]
    logs = [json.loads(line) for line in (path / 'prompts.jsonl').read_text().splitlines()]
    built_in = ['-p', '--output-format', 'stream-json', '--verbose',
                '--include-partial-messages', '--permission-mode', 'bypassPermissions']
    assert logs[0]['args'] == built_in + ['--model', 'test-model', '--settings', settings]
    assert logs[1]['args'] == built_in + ['--resume', sid, '--model', 'test-model', '--settings', settings]


@pytest.mark.parametrize('prompt', ['crash', 'complete-with-child'])
async def test_process_exit_cleans_descendants(rig, prompt):
    _, _, executor, _, path = rig
    records = [r async for r in executor.start(100, prompt)]
    pids = json.loads((path / 'pids.json').read_text())
    assert not any(pid in process_table() for pid in pids)
    if prompt == 'crash':
        assert any(e.kind == 'error' for r in records for e in r.events)


async def test_large_and_unknown_shape_lines_do_not_stop_reader(rig):
    _, _, executor, _, _ = rig
    records = [r async for r in executor.start(100, 'large-line')]
    large = next(r for r in records if isinstance(r.payload, dict) and r.payload.get('type') == 'future_event')
    assert len(large.payload['payload']) == 9 * 1024 * 1024
    assert len(large.raw_line) > 9 * 1024 * 1024
    assert not large.events
    assert any(e.kind == 'error' for r in records for e in r.events)
    assert any(e.kind == 'turn_completed' for r in records for e in r.events)


async def test_double_fork_is_discovered_after_ancestry_disappears(rig, monkeypatch):
    import os
    import signal
    from eivoh.executors.claude import ProcessTree
    from conftest import until
    _, _, executor, _, path = rig
    original = ProcessTree.snapshot
    # Deliberately miss every intermediate parent; test the adoption fallback.
    monkeypatch.setattr(ProcessTree, 'snapshot', lambda self: process_table())
    async def consume():
        return [record async for record in executor.start(100, 'double-fork-hang')]
    job = asyncio.create_task(consume())
    pid = None
    try:
        await until(lambda: (path / 'orphan.pid').exists())
        pid = int((path / 'orphan.pid').read_text())
        tree = executor.running[100]
        await asyncio.sleep(0.1)
        monkeypatch.setattr(ProcessTree, 'snapshot', original)
        tree.snapshot()
        assert pid in tree.known
        await executor.cancel(100)
        await asyncio.wait_for(job, 3)
        assert pid not in process_table()
    finally:
        # The unfixed implementation loses this PID: clean up only our fixture.
        if pid in process_table():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        monkeypatch.setattr(ProcessTree, 'snapshot', original)
        await executor.cancel(100)
        await asyncio.wait_for(job, 3)
        if pid:
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass


@pytest.mark.parametrize('prompt', ['double-fork-crash', 'double-fork-complete'])
async def test_double_fork_exit_reaps_pipe_holder(rig, prompt):
    _, _, executor, _, path = rig
    async def consume():
        return [record async for record in executor.start(100, prompt)]
    records = await asyncio.wait_for(consume(), 5)
    pid = int((path / 'orphan.pid').read_text())
    assert pid not in process_table()
    if prompt.endswith('crash'):
        assert any(e.kind == 'error' for r in records for e in r.events)
    else:
        assert any(e.kind == 'turn_completed' for r in records for e in r.events)


async def test_supervisors_isolate_concurrent_turns(rig):
    from conftest import until
    _, _, executor, _, path = rig
    other_path = path / 'other'
    other_path.mkdir()
    other = ClaudeExecutor(other_path, executor.binary, grace=0.1)
    async def consume(adapter, run_id):
        return [r async for r in adapter.start(run_id, 'double-fork-hang')]
    first = asyncio.create_task(consume(executor, 100))
    second = asyncio.create_task(consume(other, 200))
    try:
        await until(lambda: (path / 'orphan.pid').exists() and (other_path / 'orphan.pid').exists())
        own_pid = int((path / 'orphan.pid').read_text())
        other_pid = int((other_path / 'orphan.pid').read_text())
        await executor.cancel(100)
        await asyncio.wait_for(first, 3)
        assert own_pid not in process_table()
        assert other_pid in process_table()
        assert not second.done()
    finally:
        await other.shutdown()
        await executor.shutdown()
        await asyncio.gather(first, second)
    assert other_pid not in process_table()


async def test_turn_model_overrides_default_on_start_and_resume(rig):
    _, _, executor, _, path = rig
    executor.model = 'configured-default'
    records = [r async for r in executor.start(100, 'first', model='claude-haiku-5-5')]
    sid = next(e.session_id for r in records for e in r.events if e.kind == 'session_started')
    resumed = [r async for r in executor.resume(101, sid, 'second', model='claude-sonnet-5-5')]
    logs = [json.loads(line) for line in (path / 'prompts.jsonl').read_text().splitlines()]
    for records, log, model in zip([records, resumed], logs, ['claude-haiku-5-5', 'claude-sonnet-5-5']):
        assert log['args'][log['args'].index('--model') + 1] == model
        assert next(r.payload['requested_model'] for r in records if r.stream == 'meta') == model
    assert logs[1]['resume'] == sid
    assert executor.model == 'configured-default'
    assert executor.command(None)[-2:] == ['--model', 'configured-default']


async def test_watch_cleans_pipe_holder_after_supervisor_death():
    # Process.wait() also waits for pipes; a killed supervisor whose tracked
    # setsid child holds stdout must still end the watch and the child.
    import signal
    import sys
    from eivoh.executors.cli import ProcessTree
    script = ("import os, subprocess, sys, time\n"
              "child = subprocess.Popen(['sleep', '60'], start_new_session=True)\n"
              "print(child.pid, flush=True)\n"
              "time.sleep(60)\n")
    proc = await asyncio.create_subprocess_exec(sys.executable, '-c', script, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, start_new_session=True)
    tree = ProcessTree(proc)
    child, watch = None, None
    try:
        tree.known[proc.pid] = process_table()[proc.pid][1]
        child = int(await asyncio.wait_for(proc.stdout.readline(), 5))
        watch = asyncio.create_task(tree.watch(interval=0.02))
        await asyncio.sleep(0.1)
        assert child in tree.known
        proc.send_signal(signal.SIGKILL)
        await asyncio.wait_for(watch, 5)
        table = process_table()
        assert child not in table or table[child][2] == 'Z'
    finally:
        if watch is not None:
            watch.cancel()
        for pid in (proc.pid, child):
            try:
                if pid is not None:
                    os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await proc.wait()
