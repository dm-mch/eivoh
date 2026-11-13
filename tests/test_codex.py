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


def test_effective_model(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    assert CodexExecutor(tmp_path, 'codex', model='m').effective_model() == 'm'
    assert CodexExecutor(tmp_path, 'codex').effective_model() is None
    (tmp_path / 'config.toml').write_text('model = "gpt-test"\n')
    assert CodexExecutor(tmp_path, 'codex').effective_model() == 'gpt-test (codex config)'


def test_command_and_environment(tmp_path, monkeypatch):
    executor = CodexExecutor(tmp_path, 'codex', model='gpt-x', extra_args=('-c', 'k="v"'))
    assert executor.command(None) == ['codex', 'exec', '--json', '--dangerously-bypass-approvals-and-sandbox',
                                      '--skip-git-repo-check', '-m', 'gpt-x', '-c', 'k="v"', '-']
    # Options stay before `resume`, so start-only exec options also work on resume.
    assert executor.command('abc') == executor.command(None)[:-1] + ['resume', 'abc', '-']
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
    assert 'resume' not in calls[0]['args'] and calls[1]['args'][-3] == 'resume'
    assert calls[1]['args'][-2] == first['session_id'] and not any(c['openai'] for c in calls)
    final = channel.edits[-1][2] if channel.edits else channel.sent[-1][1]
    assert 'echo: again resumed=True' in final and 'Working on it' not in final
    assert storage.stored_result(second['id']) == ('echo: again resumed=True', None)
    meta = storage.db.execute("SELECT stream, payload FROM raw_event WHERE turn_id=? ORDER BY sequence LIMIT 1", (second['id'],)).fetchone()
    payload = json.loads(meta['payload'])
    assert meta['stream'] == 'meta' and payload['executor'] == 'CodexExecutor'
    assert payload['argv'][-2:] == [first['session_id'], '-'] and 'again' not in json.dumps(payload)


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


async def test_codex_turn_model_override_and_meta(codex_rig):
    _, _, codex, _, log = codex_rig
    codex.model = 'configured-default'
    first = [r async for r in codex.start(100, 'one', model='gpt-6.1-sol')]
    sid = next(e.session_id for r in first for e in r.events if e.kind == 'session_started')
    second = [r async for r in codex.resume(101, sid, 'two', model='gpt-6-astra')]
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    for records, call, model in zip([first, second], calls, ['gpt-6.1-sol', 'gpt-6-astra']):
        assert call['args'][call['args'].index('-m') + 1] == model
        assert next(r.payload['model'] for r in records if r.stream == 'meta') == model
    assert calls[1]['args'][-3:] == ['resume', sid, '-']
    assert codex.model == 'configured-default'
    assert 'configured-default' in codex.command(None)


def model_cache(path):
    path.write_text(json.dumps({'models': [
        {'slug': slug, 'visibility': 'list', 'description': slug}
        for slug in ['gpt-6-astra', 'gpt-6.1-sol', 'gpt-6-sol']]}))


async def test_model_cross_executor_handoff_and_resume(codex_rig, tmp_path):
    gateway, storage, _, channel, log = codex_rig
    model_cache(tmp_path / 'models_cache.json')
    await gateway.receive(inbound(1, 'remember violet'))
    await gateway.idle()
    original = storage.turn_for(1)
    await gateway.receive(inbound(2, '/model astra'))
    await gateway.idle()
    task = storage.active_task(original['conversation_id'])
    assert task['id'] == original['task_id']
    assert task['executor'] == 'codex' and task['model'] == 'gpt-6-astra'
    assert task['session_id'] is None
    assert storage.turn_for(2) is None
    assert any('transcript of 1 earlier turns' in text for _, text in channel.sent)
    await gateway.receive(inbound(3, 'what word?'))
    await gateway.receive(inbound(4, '/model gpt-6.1-sol'))
    await gateway.receive(inbound(5, 'another question'))
    await gateway.idle()
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert 'resume' not in calls[0]['args']
    assert 'User:\nremember violet' in calls[0]['prompt']
    assert 'Assistant:\nWorking done: remember violet' in calls[0]['prompt']
    assert calls[0]['prompt'].endswith('Current request:\nwhat word?')
    assert calls[1]['prompt'] == 'another question'
    assert calls[1]['args'][-3:] == ['resume', storage.turn_for(3)['session_id'], '-']
    assert calls[0]['args'][calls[0]['args'].index('-m') + 1] == 'gpt-6-astra'
    assert calls[1]['args'][calls[1]['args'].index('-m') + 1] == 'gpt-6.1-sol'
    assert storage.turn_for(3)['session_id'] == storage.turn_for(5)['session_id']
    assert storage.turn_for(1)['session_id'] == original['session_id']
    # Switching back also starts a new native session, carrying the task history.
    await gateway.receive(inbound(6, '/model sonnet'))
    await gateway.receive(inbound(7, 'back on claude'))
    await gateway.idle()
    claude_calls = [json.loads(line) for line in (tmp_path / 'prompts.jsonl').read_text().splitlines()]
    assert claude_calls[-1]['resume'] is None
    assert 'User:\nremember violet' in claude_calls[-1]['prompt']
    assert 'Assistant:\necho:' in claude_calls[-1]['prompt']
    assert claude_calls[-1]['prompt'].endswith('Current request:\nback on claude')


@pytest.mark.parametrize('started', [False, True])
async def test_model_handoff_survives_reopen_before_session(codex_rig, tmp_path, started):
    from eivoh.storage import Storage
    gateway, storage, codex, channel, log = codex_rig
    model_cache(tmp_path / 'models_cache.json')
    await gateway.receive(inbound(1, 'remember violet'))
    await gateway.receive(inbound(2, '/model astra'))
    await gateway.idle()
    row = storage.journal(inbound(3, 'continue after restart'))
    if started:
        storage.claim(row['id'], 3)  # Crash before the new CLI emits a session.
    await gateway.shutdown()
    reopened = Storage(tmp_path / 'state.db')
    new = Gateway(reopened, gateway.executor, {'fake': channel}, executors={'codex': codex}, edit_interval=0)
    try:
        await new.start()
        await new.idle()
        call = json.loads(log.read_text())
        assert 'resume' not in call['args'] and 'gpt-6-astra' in call['args']
        assert 'User:\nremember violet' in call['prompt']
        assert call['prompt'].endswith('Current request:\ncontinue after restart')
        assert reopened.turn_for(3)['status'] == 'completed'
        assert reopened.turn_for(3)['recovery_count'] == int(started)
    finally:
        await new.shutdown()
        reopened.close()


async def test_model_errors_leave_task_and_queue_intact(codex_rig, tmp_path):
    gateway, storage, _, channel, log = codex_rig
    model_cache(tmp_path / 'models_cache.json')
    await gateway.receive(inbound(1, '/model opus'))
    await gateway.idle()
    original = dict(storage.active_task(1))
    await gateway.receive(inbound(2, '/model sol'))
    await gateway.receive(inbound(3, '/model missing'))
    await gateway.receive(inbound(4, '/model 9'))
    await gateway.idle()
    assert dict(storage.active_task(1)) == original
    assert not storage.pending() and not log.exists()
    assert any('Ambiguous' in text and 'gpt-6.1-sol' in text for _, text in channel.sent)
    assert any('Unknown model' in text for _, text in channel.sent)
    assert any('list numbered choices first' in text for _, text in channel.sent)
