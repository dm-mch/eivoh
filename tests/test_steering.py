import asyncio
from contextlib import aclosing
import json
from pathlib import Path
import sys

import pytest

from conftest import FakeChannel, inbound, until
from eivoh.executors.base import Event, Record, SteerResult
from eivoh.executors.claude import ClaudeStreamExecutor
from eivoh.executors.codex_app import CodexAppExecutor
from eivoh.executors.cli import process_table
from eivoh.gateway import Gateway
from eivoh.storage import Storage


@pytest.fixture(params=['claude', 'codex'])
async def live_rig(request, tmp_path):
    source = Path(__file__).parent / 'fixtures/fake_interactive.py'
    binary = tmp_path / 'fake-interactive'
    binary.write_text('#!' + sys.executable + '\n' + source.read_text().split('\n', 1)[1])
    binary.chmod(0o700)
    name = request.param
    adapter = {'claude': ClaudeStreamExecutor, 'codex': CodexAppExecutor}[name](tmp_path, binary, grace=.02)
    storage = Storage(tmp_path / 'state.db', default_executor=name)
    channel = FakeChannel()
    gateway = Gateway(storage, adapter, {'fake': channel}, edit_interval=.01)
    await gateway.start()
    yield gateway, storage, adapter, channel, tmp_path
    await gateway.shutdown()
    storage.close()


async def ready(rig):
    gateway, storage, adapter, channel, path = rig
    await until(lambda: adapter.runs and next(iter(adapter.runs.values())).ready.is_set())


async def test_busy_multiple_inputs_are_one_result_and_native_resume(live_rig):
    gateway, storage, adapter, channel, path = live_rig
    await gateway.receive(inbound(1, 'held'))
    await ready(live_rig)
    await gateway.receive(inbound(2, 'FIRST'))
    await gateway.receive(inbound(3, 'SECOND', reply_to='1'))
    await gateway.receive(inbound(3, 'SECOND'))  # Telegram duplicate.
    await until(lambda: (path / 'wire-input.jsonl').exists() and (path / 'wire-input.jsonl').read_text().count('SECOND') == 1)
    (path / 'release').touch()
    await asyncio.wait_for(gateway.idle(), 5)
    assert [row['state'] for row in storage.db.execute('SELECT * FROM steering')] == ['accepted', 'accepted']
    assert [storage.turn_for(i)['status'] for i in (1, 2, 3)] == ['completed'] * 3
    assert storage.stored_result(1)[0] == 'ANSWER held FIRST SECOND'
    transcript, count, _ = storage.transcript(1, 4)
    assert count == 1 and transcript.count('Assistant:') == 1
    assert 'User (during the same turn):\nFIRST' in transcript
    assert storage.session(1, storage.default_executor)['synced_inbound_id'] == 3
    assert sum('Added to the running task' in text for _, text in channel.sent) == 2
    await gateway.receive(inbound(4, 'resume'))
    await gateway.idle()
    assert storage.turn_for(4)['session_id'] == storage.turn_for(1)['session_id']


async def test_queue_is_fifo_barrier_and_model_change_queues(live_rig):
    gateway, storage, adapter, channel, path = live_rig
    await gateway.receive(inbound(1, 'held'))
    await ready(live_rig)
    await gateway.receive(inbound(2, '/q QUEUED'))
    await gateway.receive(inbound(3, 'AFTER'))
    assert not list(storage.db.execute('SELECT * FROM steering'))
    (path / 'release').touch()
    await asyncio.wait_for(gateway.idle(), 5)
    assert 'ANSWER QUEUED' in storage.stored_result(2)[0]
    assert '/q' not in storage.stored_result(2)[0]
    assert 'AFTER' in storage.stored_result(storage.turn_for(3)['id'])[0]


async def test_stop_while_steering_cleans_processes(live_rig):
    gateway, storage, adapter, channel, path = live_rig
    await gateway.receive(inbound(1, 'child'))
    await ready(live_rig)
    await until(lambda: (path / 'child.pid').exists())
    pid = int((path / 'child.pid').read_text())
    await gateway.receive(inbound(2, 'clarify'))
    await gateway.receive(inbound(3, '/stop'))
    await gateway.idle()
    assert storage.turn_for(1)['status'] == 'cancelled'
    assert storage.turn_for(2)['status'] == 'cancelled'
    assert not adapter.running and pid not in process_table()


async def test_claude_crosses_result_boundary(live_rig):
    gateway, storage, adapter, channel, path = live_rig
    await gateway.receive(inbound(1, 'boundary'))
    await ready(live_rig)
    await gateway.receive(inbound(2, 'NEW'))
    await gateway.idle()
    expected = ('OLD RESULT\n\n' if isinstance(adapter, ClaudeStreamExecutor) else '') + 'ANSWER boundary NEW'
    assert storage.stored_result(1)[0] == expected
    assert storage.turn_for(2)['status'] == 'completed'
    if isinstance(adapter, ClaudeStreamExecutor):
        records = list(storage.db.execute("SELECT payload FROM raw_event WHERE turn_id=1"))
        assert any('OLD RESULT' in row[0] for row in records)
        transcript = storage.transcript(1, 3)[0]
        assert transcript.count('OLD RESULT') == 1
        assert transcript.count('ANSWER boundary NEW') == 1
        assert any(expected in text for _, _, text in channel.edits)


async def test_unknown_ack_timeout_queues_cautious_prompt(live_rig):
    gateway, storage, adapter, channel, path = live_rig
    adapter.ack_timeout = .05  # Synthetic transport failure, not healthy long tools.
    await gateway.receive(inbound(1, 'noack'))
    await ready(live_rig)
    await gateway.receive(inbound(2, 'clarify'))
    await asyncio.wait_for(gateway.idle(), 5)
    assert storage.steering(2)['state'] == 'unknown'
    assert storage.turn_for(2)['status'] == 'completed'
    assert any('could not be confirmed' in text for _, text in channel.sent)
    assert 'do not repeat completed actions' in storage.stored_result(2)[0]


def setup_pending(storage):
    storage.journal(inbound(1, 'original'))
    turn, _ = storage.claim(1, 3)
    storage.prepare_prompt(turn, 'original')
    storage.append(1, Record(None, {}, (Event('session_started', {}, {}, 'session'),)))
    storage.journal(inbound(2, 'clarification'))
    input_id = storage.reserve_steer(1, 2)
    return turn, input_id


@pytest.mark.parametrize('accepted,result', [(False, False), (True, False), (True, True), (False, True)])
def test_restart_delivery_and_parent_result(tmp_path, accepted, result):
    path = tmp_path / 'state.db'
    storage = Storage(path)
    turn, input_id = setup_pending(storage)
    if accepted:
        storage.append(1, Record('{"ack":true}\n', {'ack': True}, (Event('input_accepted', {'input_id': input_id}, {}),)))
    if result:
        storage.append(1, Record(None, {}, (Event('turn_completed', {'status': 'completed', 'text': 'final'}, {}),)))
    storage.close()
    storage = Storage(path)
    storage.recover_steering()
    assert storage.steering(2)['state'] == ('accepted' if accepted else 'unknown')
    if result:
        assert storage.stored_result(1) == ('final', None)
        storage.finish(1, 'completed')
        assert storage.turn_for(2)['status'] == ('completed' if accepted else 'queued')
    else:
        prompt = storage.recovery_inputs(1)
        assert ('clarification' in prompt) == accepted
    assert storage.input_text(2).startswith('clarification' if accepted else 'Eivoh could not confirm')
    storage.close()


def test_model_change_and_fifo_reservation(tmp_path):
    storage = Storage(tmp_path / 'state.db')
    turn, input_id = setup_pending(storage)
    storage.journal(inbound(3, 'second clarification'))
    assert storage.reserve_steer(1, 3) is not None  # Earlier sending is not a barrier.
    storage.journal(inbound(4, '/model other'))
    storage.set_model(4, 'claude', 'other')
    storage.journal(inbound(5, 'new model request'))
    assert storage.reserve_steer(1, 5) is None
    storage.close()


async def test_model_change_during_run_does_not_steer_old_model(live_rig):
    gateway, storage, adapter, channel, path = live_rig
    await gateway.receive(inbound(1, 'held'))
    await ready(live_rig)
    model = 'haiku' if isinstance(adapter, ClaudeStreamExecutor) else 'gpt-6-luna'
    (path / 'models_cache.json').write_text(json.dumps({'models': [{'slug': 'gpt-6-luna', 'visibility': 'list'}]}))
    await gateway.receive(inbound(2, '/model ' + model))
    await gateway.receive(inbound(3, 'NEW MODEL'))
    assert storage.steering(3) is None
    (path / 'release').touch()
    await gateway.idle()
    assert storage.turn_for(3)['model'] == model
    assert 'NEW MODEL' not in storage.stored_result(1)[0]
    assert 'NEW MODEL' in storage.stored_result(storage.turn_for(3)['id'])[0]


async def test_notice_flood_wait_does_not_hold_executor_slot(live_rig):
    gateway, storage, adapter, channel, path = live_rig
    blocked, release = asyncio.Event(), asyncio.Event()
    send = channel.send
    async def slow_notice(chat, text, *args):
        if 'Added to the running' in text:
            blocked.set()
            await release.wait()
        return await send(chat, text, *args)
    channel.send = slow_notice
    gateway.slots = asyncio.Semaphore(1)
    await gateway.receive(inbound(1, 'held'))
    await ready(live_rig)
    await gateway.receive(inbound(2, 'CLARIFY'))
    (path / 'release').touch()
    await asyncio.wait_for(blocked.wait(), 5)
    await until(lambda: storage.turn_for(1)['status'] == 'completed')
    assert gateway.slots._value == 1
    await gateway.receive(inbound(3, 'FOLLOWUP'))
    await asyncio.wait_for(gateway.idle(), 5)
    assert storage.turn_for(3)['status'] == 'completed'
    release.set()


async def test_multiple_tasks_route_reply_while_steering(live_rig):
    gateway, storage, adapter, channel, path = live_rig
    await gateway.receive(inbound(1, 'held'))
    await ready(live_rig)
    await gateway.receive(inbound(2, '/new'))
    await gateway.receive(inbound(3, 'other task'))
    await gateway.receive(inbound(4, 'FIRST TASK', reply_to='1'))
    await until(lambda: (path / 'wire-input.jsonl').exists() and 'FIRST TASK' in (path / 'wire-input.jsonl').read_text())
    (path / 'release').touch()
    await gateway.idle()
    assert storage.turn_for(4)['task_id'] == 1
    assert storage.turn_for(3)['task_id'] == 2
    assert 'FIRST TASK' in storage.stored_result(1)[0]
    assert 'FIRST TASK' not in storage.stored_result(storage.turn_for(3)['id'])[0]
    assert any('#t1' in text and 'Added to the running' in text for _, text in channel.sent)


async def test_replayed_parent_result_finalizes_accepted_children_without_launch(live_rig):
    gateway, storage, adapter, channel, path = live_rig
    turn, input_id = setup_pending(storage)
    storage.append(1, Record('{"ack":true}\n', {}, (Event('input_accepted', {'input_id': input_id}, {}),)))
    storage.append(1, Record(None, {}, (Event('turn_completed', {'status': 'completed', 'text': 'saved final'}, {}),)))
    await gateway.start()
    await gateway.idle()
    assert not (path / 'wire-input.jsonl').exists()
    assert storage.turn_for(2)['status'] == 'completed'
    assert storage.session(1, storage.default_executor)['synced_inbound_id'] == 2
    assert any('saved final' in text for _, text in channel.sent)


async def test_shutdown_sending_recovers_as_unknown(live_rig):
    gateway, storage, adapter, channel, path = live_rig
    await gateway.receive(inbound(1, 'held'))
    await ready(live_rig)
    await gateway.receive(inbound(2, 'CLARIFY'))
    await until(lambda: storage.steering(2) is not None)
    await gateway.shutdown()
    assert storage.turn_for(1)['status'] == 'running'
    assert storage.steering(2)['state'] in {'unknown', 'accepted'}
    assert not adapter.running and not gateway.steer_jobs
    gateway = Gateway(storage, adapter, {'fake': channel}, edit_interval=.01)
    (path / 'release').touch()
    await gateway.start()
    await asyncio.wait_for(gateway.idle(), 5)
    assert storage.turn_for(1)['status'] == 'completed'
    assert storage.turn_for(2)['status'] == 'completed'
    await gateway.shutdown()


async def test_server_request_is_answered_and_not_hanging(live_rig):
    gateway, storage, adapter, channel, path = live_rig
    await gateway.receive(inbound(1, 'server-request'))
    await asyncio.wait_for(gateway.idle(), 5)
    if isinstance(adapter, CodexAppExecutor):
        lines = [json.loads(line) for line in (path / 'wire-input.jsonl').read_text().splitlines()]
        assert any(line.get('id') == 'server-1' and line['error']['code'] == -32601 for line in lines)
    assert storage.turn_for(1)['status'] == 'completed'


async def test_failure_with_unacknowledged_input_does_not_hang(live_rig):
    gateway, storage, adapter, channel, path = live_rig
    await gateway.receive(inbound(1, 'error'))
    await ready(live_rig)
    await gateway.receive(inbound(2, 'KEEP'))
    await asyncio.wait_for(gateway.idle(), 5)
    assert storage.turn_for(2)['status'] == 'completed'
    if isinstance(adapter, ClaudeStreamExecutor):
        assert storage.steering(2)['state'] == 'unknown'
        assert storage.turn_for(1)['status'] == 'failed'


def test_watermark_and_handoff_include_all_accepted_inputs_once(tmp_path):
    storage = Storage(tmp_path / 'state.db')
    turn, input_id = setup_pending(storage)
    storage.append(1, Record(None, {}, (Event('input_accepted', {'input_id': input_id}, {}),)))
    storage.append(1, Record(None, {}, (Event('turn_completed', {'status': 'completed', 'text': 'final once'}, {}),)))
    storage.finish(1, 'completed')
    storage.journal(inbound(3, '/model gpt-6-luna'))
    storage.set_model(3, 'codex', 'gpt-6-luna')
    storage.journal(inbound(4, 'followup'))
    other, _ = storage.claim(4, 3)
    prompt = storage.prepare_prompt(other, 'followup')
    assert prompt.count('clarification') == 1
    assert prompt.count('final once') == 1
    storage.append(other['id'], Record(None, {}, (Event('session_started', {}, {}, 'codex-session'),)))
    storage.append(other['id'], Record(None, {}, (Event('turn_completed', {'status': 'completed', 'text': 'codex answer'}, {}),)))
    storage.finish(other['id'], 'completed')
    storage.journal(inbound(5, '/model haiku'))
    storage.set_model(5, 'claude', 'haiku')
    storage.journal(inbound(6, 'return'))
    back, _ = storage.claim(6, 3)
    prompt = storage.prepare_prompt(back, 'return')
    assert 'codex answer' in prompt and 'clarification' not in prompt
    assert back['resume_id'] == 'session'
    storage.close()


def test_unknown_before_accepted_input_survives_executor_handoff(tmp_path):
    storage = Storage(tmp_path / 'state.db')
    parent, first = setup_pending(storage)
    storage.journal(inbound(3, 'later accepted'))
    second = storage.reserve_steer(1, 3)
    storage.steer_outcome(2, 'unknown')
    storage.append(1, Record(None, {}, (Event('input_accepted', {'input_id': second}, {}),)))
    storage.append(1, Record(None, {}, (Event('turn_completed', {'status': 'completed', 'text': 'parent final'}, {}),)))
    storage.finish(1, 'completed')
    assert storage.session(1, 'claude')['synced_inbound_id'] == 1
    storage.journal(inbound(4, '/model gpt-6-luna'))
    storage.set_model(4, 'codex', 'gpt-6-luna')
    fallback, _ = storage.claim(2, 3)
    prompt = storage.prepare_prompt(fallback, storage.input_text(2))
    assert prompt.count('later accepted') == 1
    assert prompt.count('parent final') == 1
    storage.append(fallback['id'], Record(None, {}, (Event('session_started', {}, {}, 'codex'),)))
    storage.finish(fallback['id'], 'completed')
    assert storage.session(1, 'codex')['synced_inbound_id'] == 3
    assert storage.missed_transcript(1, 'codex', 5)[0] == ''
    storage.close()


@pytest.mark.parametrize('prompt', ['wrong-ack', 'malformed-ack'])
async def test_invalid_success_reply_never_claims_acceptance(live_rig, prompt):
    gateway, storage, adapter, channel, path = live_rig
    if not isinstance(adapter, CodexAppExecutor):
        return
    await gateway.receive(inbound(1, prompt))
    await ready(live_rig)
    await gateway.receive(inbound(2, 'KEEP'))
    await asyncio.wait_for(gateway.idle(), 5)
    assert storage.steering(2)['state'] == 'unknown'
    assert storage.turn_for(2)['status'] == 'completed'
    assert not any('Added to the running' in text for _, text in channel.sent)
    assert 'do not repeat completed actions' in storage.stored_result(2)[0]


async def test_rejected_initial_codex_input_does_not_consume_handoff(live_rig):
    gateway, storage, adapter, channel, path = live_rig
    if not isinstance(adapter, CodexAppExecutor):
        return
    # Existing Claude history and an existing empty Codex native thread. The
    # latter can be resumed successfully while turn/start rejects its input.
    storage.journal(inbound(1, 'CLAUDE HISTORY'))
    storage.db.execute("UPDATE task SET executor='claude' WHERE id=1")
    turn, _ = storage.claim(1, 3)
    storage.append(turn['id'], Record(None, {}, (Event('session_started', {}, {}, 'claude-session'),)))
    storage.append(turn['id'], Record(None, {}, (Event('turn_completed', {'status': 'completed', 'text': 'CLAUDE ANSWER'}, {}),)))
    storage.finish(turn['id'], 'completed')
    storage.db.execute("INSERT INTO task_session VALUES (1,'codex','existing-codex',0)")
    storage.journal(inbound(2, '/model gpt-6-luna'))
    storage.set_model(2, 'codex', 'gpt-6-luna')
    await gateway.receive(inbound(3, 'REJECT START'))
    await asyncio.wait_for(gateway.idle(), 5)
    assert storage.turn_for(3)['status'] == 'failed'
    assert storage.turn_for(3)['session_id'] == 'existing-codex'
    assert storage.session(1, 'codex')['synced_inbound_id'] == 0
    assert storage.session(1, 'codex')['session_id'] == 'existing-codex'
    await gateway.receive(inbound(4, 'ACCEPT NEXT'))
    await gateway.idle()
    prompt = storage.db.execute("SELECT json_extract(payload,'$.prompt') FROM raw_event WHERE turn_id=? "
                               "AND json_extract(payload,'$.type')='turn_prompt'", (storage.turn_for(4)['id'],)).fetchone()[0]
    assert 'CLAUDE HISTORY' in prompt and 'CLAUDE ANSWER' in prompt
    assert storage.turn_for(4)['status'] == 'completed'
    assert storage.session(1, 'codex')['synced_inbound_id'] == 4
    # Success response plus turn/started produce one reset marker, before text.
    events = [event for row in storage.db.execute('SELECT events FROM raw_event WHERE turn_id=? ORDER BY sequence',
                                                  (storage.turn_for(4)['id'],)) for event in json.loads(row[0])]
    assert sum(event['kind'] == 'session_started' for event in events) == 1
    assert storage.stored_result(storage.turn_for(4)['id'])[0].startswith('ANSWER')


async def test_codex_restart_preserves_thread_before_initial_ack(live_rig):
    gateway, storage, adapter, channel, path = live_rig
    if not isinstance(adapter, CodexAppExecutor):
        return
    await gateway.receive(inbound(1, 'DELAY START'))
    await until(lambda: (path / 'turn-start-written').exists())
    await until(lambda: storage.session(1, 'codex') is not None)
    assert storage.session(1, 'codex')['synced_inbound_id'] == 0
    await gateway.shutdown()
    assert storage.turn_for(1)['status'] == 'running'
    saved = storage.session(1, 'codex')['session_id']
    (path / 'allow-start').touch()
    resumed = Gateway(storage, adapter, {'fake': channel}, edit_interval=.01)
    await resumed.start()
    await asyncio.wait_for(resumed.idle(), 5)
    assert storage.turn_for(1)['status'] == 'completed'
    assert storage.turn_for(1)['recovery_count'] == 1
    assert storage.session(1, 'codex')['session_id'] == saved
    assert storage.session(1, 'codex')['synced_inbound_id'] == 1
    lines = [json.loads(line) for line in (path / 'wire-input.jsonl').read_text().splitlines()]
    assert sum(line.get('method') == 'thread/start' for line in lines) == 1
    assert sum(line.get('method') == 'thread/resume' for line in lines) == 1
    prompts = [line['params']['input'][0]['text'] for line in lines if line.get('method') == 'turn/start']
    assert 'Check existing work before repeating actions' in prompts[-1]
    await resumed.shutdown()


def test_initial_acceptance_fold_uses_latest_attempt(tmp_path):
    storage = Storage(tmp_path / 'state.db', default_executor='codex')
    storage.journal(inbound(1, 'request'))
    turn, _ = storage.claim(1, 3)
    def emit(*events):
        storage.append(turn['id'], Record(None, {}, events))
    emit(Event('session_started', {'input_accepted': False}, {}, 'thread'),
         Event('input_accepted', {'initial': True}, {}, 'thread'))
    emit(Event('session_started', {'input_accepted': False}, {}, 'thread'))
    storage.finish(turn['id'], 'failed')
    assert storage.session(1, 'codex')['synced_inbound_id'] == 0
    storage.close()


def test_buffered_acceptance_restores_parent_settings_after_model_change(tmp_path):
    storage = Storage(tmp_path / 'state.db')
    parent, input_id = setup_pending(storage)
    storage.steer_outcome(2, 'unknown', 'Transport closed before acknowledgement')
    assert storage.turn_for(2)['executor'] is None
    storage.journal(inbound(3, '/model gpt-6-luna'))
    storage.set_model(3, 'codex', 'gpt-6-luna')
    storage.append(parent['id'], Record(None, {}, (Event('input_accepted', {'input_id': input_id}, {}),)))
    assert storage.steering(2)['state'] == 'accepted'
    assert storage.turn_for(2)['executor'] == parent['executor'] == 'claude'
    assert storage.turn_for(2)['model'] == parent['model'] is None
    storage.finish(parent['id'], 'completed')
    assert storage.turn_for(2)['status'] == 'completed'
    storage.close()


async def test_codex_preserves_all_final_answer_items_without_duplicate_ids(live_rig):
    gateway, storage, adapter, channel, path = live_rig
    if not isinstance(adapter, CodexAppExecutor):
        return
    await gateway.receive(inbound(1, 'multi-final'))
    await gateway.idle()
    text = storage.stored_result(1)[0]
    assert text == 'FIRST COMPLETE ANSWER\n\nANSWER multi-final '
    transcript = storage.transcript(1, 2)[0]
    assert transcript.count('FIRST COMPLETE ANSWER') == 1
    assert 'COMMENTARY' not in text and 'CHILD' not in text


@pytest.mark.parametrize('prompt', ['late-rpc', 'late-echo'])
async def test_codex_ack_after_terminal_remains_unknown_and_runs_cautiously(live_rig, prompt):
    gateway, storage, adapter, channel, path = live_rig
    if not isinstance(adapter, CodexAppExecutor):
        return
    await gateway.receive(inbound(1, prompt))
    await ready(live_rig)
    await gateway.receive(inbound(2, 'KEEP THIS INPUT'))
    await asyncio.wait_for(gateway.idle(), 5)
    assert storage.steering(2)['state'] == 'unknown'
    assert storage.turn_for(2)['status'] == 'completed'
    assert 'KEEP THIS INPUT' not in storage.stored_result(1)[0]
    assert 'do not repeat completed actions' in storage.stored_result(2)[0]
    assert not any('Added to the running' in text for _, text in channel.sent)
    assert any('could not be confirmed' in text for _, text in channel.sent)


async def test_codex_matching_rpc_before_terminal_is_acceptance_without_echo(live_rig):
    gateway, storage, adapter, channel, path = live_rig
    if not isinstance(adapter, CodexAppExecutor):
        return
    await gateway.receive(inbound(1, 'rpc-only'))
    await ready(live_rig)
    await gateway.receive(inbound(2, 'ACCEPTED'))
    await gateway.idle()
    assert storage.steering(2)['state'] == 'accepted'
    assert storage.stored_result(1)[0] == 'ANSWER rpc-only ACCEPTED'
    assert storage.stored_result(2) is None
    assert any('Added to the running' in text for _, text in channel.sent)


async def test_unanswered_interrupt_rpc_is_transport_error_not_task_cancellation(live_rig):
    gateway, storage, adapter, channel, path = live_rig
    if not isinstance(adapter, CodexAppExecutor):
        return
    adapter.ack_timeout = .03
    await gateway.receive(inbound(1, 'drop-interrupt'))
    await ready(live_rig)
    await gateway.receive(inbound(2, 'KEEP'))
    await asyncio.wait_for(gateway.idle(), 5)
    assert storage.steering(2)['state'] == 'unknown'
    assert storage.turn_for(2)['status'] == 'completed'
    assert any('could not be confirmed' in text for _, text in channel.sent)
    assert not adapter.running


async def test_exception_parent_wakes_delayed_fallback_without_new_message(tmp_path):
    entered, injected, fail = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Executor:
        supports_steer = True
        prompts = []
        async def start(self, run_id, prompt, model=None):
            self.prompts.append(prompt)
            yield Record(None, {}, (Event('session_started', {}, {}, 'session'),))
            if run_id == 1:
                entered.set()
                await fail.wait()
                raise RuntimeError('synthetic stream failure')
            yield Record(None, {}, (Event('turn_completed', {'status': 'completed', 'text': 'fallback complete'}, {}),))
        def resume(self, run_id, session_id, prompt, model=None):
            return self.start(run_id, prompt, model)
        async def steer(self, run_id, input_id, text):
            injected.set()
            await asyncio.sleep(.15)
            return SteerResult('not_sent')
        async def cancel(self, run_id):
            pass
        async def shutdown(self):
            pass
    storage = Storage(tmp_path / 'state.db')
    executor, channel = Executor(), FakeChannel()
    gateway = Gateway(storage, executor, {'fake': channel}, edit_interval=.01)
    try:
        await gateway.receive(inbound(1, 'original'))
        await entered.wait()
        await gateway.receive(inbound(2, 'fallback'))
        await injected.wait()
        fail.set()
        await until(lambda: not gateway.workers)
        assert storage.steering(2)['state'] == 'sending'
        await until(lambda: storage.turn_for(2)['status'] == 'completed')
        assert executor.prompts == ['original', 'fallback']
        assert storage.steering(2)['state'] == 'queued'
    finally:
        await gateway.shutdown()
        storage.close()


async def test_complete_answer_part_survives_restart_before_terminal_without_prior_delivery(live_rig):
    gateway, storage, adapter, channel, path = live_rig
    blocked = asyncio.Event()
    async def blocked_send(*args):
        await blocked.wait()
        raise AssertionError('Initial response should never be delivered')
    channel.send = blocked_send
    await gateway.receive(inbound(1, 'durable-boundary'))
    await ready(live_rig)
    await gateway.receive(inbound(2, 'CLARIFY'))
    await until(lambda: storage.db.execute("SELECT 1 FROM raw_event WHERE turn_id=1 AND kinds LIKE '%answer_part%'").fetchone())
    await until(lambda: storage.steering(2)['state'] == 'accepted')
    await until(lambda: storage.db.execute("SELECT 1 FROM raw_event WHERE turn_id=1 AND kinds LIKE '%text_delta%' "
                                          "AND events LIKE '%UNFINISHED PARTIAL%'").fetchone())
    assert storage.stored_result(1) is None  # A complete part is not a terminal turn.
    await gateway.shutdown()
    assert storage.turn_for(1)['status'] == 'running'
    assert storage.stored_result(1) is None
    assert not channel.sent
    reopened = Storage(path / 'state.db', default_executor=storage.default_executor)
    resumed_channel = FakeChannel()
    resumed = Gateway(reopened, adapter, {'fake': resumed_channel}, edit_interval=.01)
    try:
        await resumed.start()
        await asyncio.wait_for(resumed.idle(), 5)
        expected = 'OLD COMPLETE ANSWER\n\nFOLLOWUP COMPLETE ANSWER'
        assert reopened.stored_result(1) == (expected, None)
        assert reopened.turn_for(1)['recovery_count'] == 1
        assert reopened.turn_for(2)['status'] == 'completed'
        assert 'UNFINISHED PARTIAL' not in reopened.stored_result(1)[0]
        assert any(expected in text for _, text in resumed_channel.sent) or any(
            expected in text for _, _, text in resumed_channel.edits)
        transcript = reopened.transcript(1, 3)[0]
        assert transcript.count('OLD COMPLETE ANSWER') == 1
        assert transcript.count('FOLLOWUP COMPLETE ANSWER') == 1
        assert 'UNFINISHED PARTIAL' not in transcript
        # Preparing handoff for another model uses the same complete final text.
        reopened.journal(inbound(3, '/model other'))
        other = 'codex' if storage.default_executor == 'claude' else 'claude'
        reopened.set_model(3, other, None)
        reopened.journal(inbound(4, 'continue'))
        turn, _ = reopened.claim(4, 3)
        prompt = reopened.prepare_prompt(turn, 'continue')
        assert expected in prompt
    finally:
        await resumed.shutdown()
        reopened.close()


def test_complete_part_replay_across_attempts_deduplicates_ids_and_preserves_legacy_terminal(tmp_path):
    storage = Storage(tmp_path / 'state.db')
    storage.journal(inbound(1, 'request'))
    turn, _ = storage.claim(1, 3)
    def emit(*events, allow_result=True):
        storage.append(turn['id'], Record(None, {}, events), allow_result=allow_result)
    emit(Event('session_started', {}, {}, 'session'))
    emit(Event('answer_part', {'id': 'part-a', 'text': 'COMPLETE A'}, {}))
    emit(Event('answer_part', {'id': 'part-a', 'text': 'COMPLETE A'}, {}))
    emit(Event('text_delta', {'text': 'PARTIAL'}, {}))
    emit(Event('turn_completed', {'status': 'completed', 'text': 'shutdown output'}, {}), allow_result=False)
    assert storage.stored_result(turn['id']) is None
    emit(Event('session_started', {}, {}, 'session'))
    # Legacy final records lack answer_part_ids and emit no new answer_part.
    emit(Event('turn_completed', {'status': 'completed', 'text': 'COMPLETE B'}, {}))
    assert storage.stored_result(turn['id']) == ('COMPLETE A\n\nCOMPLETE B', None)
    storage.close()


def test_complete_part_replay_does_not_duplicate_current_aggregate(tmp_path):
    storage = Storage(tmp_path / 'state.db')
    storage.journal(inbound(1, 'request'))
    turn, _ = storage.claim(1, 3)
    storage.append(turn['id'], Record(None, {}, (
        Event('session_started', {}, {}, 'session'),
        Event('answer_part', {'id': 'old', 'text': 'OLD'}, {}),
        Event('session_started', {}, {}, 'session'),
        Event('answer_part', {'id': 'new', 'text': 'NEW'}, {}),
        Event('turn_completed', {'status': 'completed', 'text': 'NEW\n\nFINAL',
                                 'answer_part_ids': ['new', 'final']}, {}),
    )))
    assert storage.stored_result(turn['id']) == ('OLD\n\nNEW\n\nFINAL', None)
    storage.close()
