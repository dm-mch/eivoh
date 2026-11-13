import json
import os
import sys
from pathlib import Path

import pytest

from conftest import FakeChannel, inbound, until
from eivoh.executors.codex import CodexExecutor, CodexParser
from eivoh.executors.claude import process_table
from eivoh.gateway import Gateway

FIXTURES = Path(__file__).parent / 'fixtures'


def parse(name):
    parser = CodexParser()
    return [parser(line) for line in (FIXTURES / name).read_text().splitlines(True)]


def kinds(records):
    return [e.kind for r in records for e in r.events]


def test_parser_maps_probed_stream():
    records = parse('codex_stream.jsonl')
    assert kinds(records) == ['session_started', 'message', 'tool_started', 'tool_finished', 'tool_started',
                              'tool_finished', 'tool_started', 'tool_finished', 'tool_started', 'tool_finished',
                              'message', 'usage', 'turn_completed']
    events = [e for r in records for e in r.events]
    assert all(e.session_id == '01a1d36c-3613-7651-aea9-29383601cfb1' for e in events)
    assert events[2].data['name'] == "shell: /bin/bash -lc 'ls -la'"
    assert events[1].data['text'].endswith('`a.txt`.\n\n')
    final = events[-1].data
    assert final['status'] == 'completed' and final['text'].startswith('Ran `ls -la`')


def test_parser_failure_and_warnings():
    records = parse('codex_failed.jsonl')
    # Item errors are warnings and top-level errors precede turn.failed: journaled only.
    assert kinds(records) == ['session_started', 'error', 'turn_completed']
    assert 'not supported' in records[-1].events[0].data['message']
    assert records[-1].events[1].data['status'] == 'failed'
    bad = CodexParser()('not json\n')
    assert bad.events[0].kind == 'error'


def test_command_and_environment(tmp_path, monkeypatch):
    executor = CodexExecutor(tmp_path, 'codex', model='gpt-x', extra_args=('-c', 'k="v"'))
    assert executor.command(None) == ['codex', 'exec', '--json', '--dangerously-bypass-approvals-and-sandbox',
                                      '--skip-git-repo-check', '-m', 'gpt-x', '-c', 'k="v"', '-']
    assert executor.command('abc')[:2] == ['codex', 'exec'] and executor.command('abc')[2] == 'resume'
    assert executor.command('abc')[-2:] == ['abc', '-']
    monkeypatch.setenv('OPENAI_API_KEY', 'secret')
    monkeypatch.setenv('CODEX_API_KEY', 'secret')
    env = executor.environment()
    assert 'OPENAI_API_KEY' not in env and 'CODEX_API_KEY' not in env and 'PATH' in env
    with pytest.raises(ValueError):
        list(executor.resume(1, '-bad', 'x').__anext__().__await__())


@pytest.fixture
async def codex_rig(rig, tmp_path, monkeypatch):
    gateway, storage, claude, channel, _ = rig
    binary = tmp_path / 'fake-codex'
    binary.write_text('#!' + sys.executable + '\n' + (FIXTURES / 'fake_codex.py').read_text().split('\n', 1)[1])
    binary.chmod(0o700)
    log = tmp_path / 'codex.log'
    monkeypatch.setenv('FAKE_CODEX_LOG', str(log))
    monkeypatch.setenv('OPENAI_API_KEY', 'must-not-leak')
    codex = CodexExecutor(tmp_path, binary.resolve(), grace=0.1)
    gateway.executors['codex'] = codex
    return gateway, storage, codex, channel, log


async def test_new_codex_task_runs_and_resumes(codex_rig):
    gateway, storage, codex, channel, log = codex_rig
    await gateway.receive(inbound(1, '/new codex'))
    await gateway.receive(inbound(2, 'hello'))
    await gateway.receive(inbound(3, 'again'))
    await gateway.idle()
    assert any('New task on codex' in text for _, text in channel.sent)
    first, second = storage.turn_for(2), storage.turn_for(3)
    assert first['status'] == second['status'] == 'completed'
    task = storage.db.execute('SELECT * FROM task WHERE id=?', (first['task_id'],)).fetchone()
    assert task['executor'] == 'codex' and task['session_id'] == first['session_id'] == second['session_id']
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls[0]['args'][1] != 'resume' and calls[1]['args'][1:2] == ['resume']
    assert calls[1]['args'][-2] == first['session_id'] and not any(c['openai'] for c in calls)
    final = channel.edits[-1][2] if channel.edits else channel.sent[-1][1]
    assert 'echo: again resumed=True' in final and 'Working on it' not in final
    assert storage.stored_result(second['id']) == ('echo: again resumed=True', None)


async def test_codex_failure_is_reported(codex_rig):
    gateway, storage, codex, channel, log = codex_rig
    await gateway.receive(inbound(1, '/new codex'))
    await gateway.receive(inbound(2, 'please fail'))
    await gateway.idle()
    turn = storage.turn_for(2)
    assert turn['status'] == 'failed' and 'fake failure' in turn['error']


async def test_codex_stop_leaves_no_orphans(codex_rig):
    gateway, storage, codex, channel, log = codex_rig
    await gateway.receive(inbound(1, '/new codex'))
    await gateway.receive(inbound(2, 'sleep please'))
    tracked = {}
    def sleeping():
        for tree in codex.running.values():
            table = tree.snapshot()
            tracked.update(tree.known)
            if any(pid in table and table[pid][1] == start and pid != tree.process.pid
                   and Path(f'/proc/{pid}/cmdline').read_bytes().find(b'time.sleep(60)') >= 0
                   for pid, start in tree.known.items()):
                return True
        return False
    await until(sleeping, timeout=10)
    await gateway.receive(inbound(3, '/stop'))
    await gateway.idle()
    assert storage.turn_for(2)['status'] == 'cancelled'
    def gone():
        table = process_table()
        return all(pid not in table or table[pid][1] != start or table[pid][2] == 'Z' for pid, start in tracked.items())
    await until(gone)


async def test_unknown_executor_is_refused(codex_rig):
    gateway, storage, codex, channel, log = codex_rig
    await gateway.receive(inbound(1, '/new gemini'))
    await gateway.idle()
    assert any('Unknown executor `gemini`' in text for _, text in channel.sent)
    assert storage.pending() == []
