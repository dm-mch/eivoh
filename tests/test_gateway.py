import asyncio
import json

import pytest

from conftest import FakeChannel, inbound, until
from eivoh.executors.claude import ClaudeExecutor, process_table
from eivoh.gateway import Gateway, RESTART_PROMPT
from eivoh.storage import Storage


def prompts(path):
    return [json.loads(line) for line in (path / 'prompts.jsonl').read_text().splitlines()]


async def test_progressive_fifo_resume_new_and_dedupe(rig):
    gateway, storage, executor, channel, path = rig
    await gateway.receive(inbound(1, 'first'))
    await until(lambda: bool(channel.sent))
    assert storage.turn_for(1)['status'] == 'running'
    assert channel.sent[0][1] == 'Working '
    await gateway.receive(inbound(2, 'second'))
    await gateway.receive(inbound(2, 'redelivered'))
    await gateway.receive(inbound(3, '/new'))
    await gateway.receive(inbound(4, 'third'))
    await asyncio.wait_for(gateway.idle(), 5)
    logs = prompts(path)
    assert [x['prompt'] for x in logs] == ['first', 'second', 'third']
    assert logs[1]['resume'] == logs[0]['sid']
    assert logs[2]['resume'] is None
    assert channel.edits and channel.typings
    assert any(text == 'Working done: first' for _, _, text in channel.edits)
    assert storage.db.execute("SELECT COUNT(*) FROM turn WHERE status='completed'").fetchone()[0] == 3
    assert storage.db.execute("SELECT COUNT(*) FROM raw_event WHERE raw_line LIKE '%rate_limit_event%'").fetchone()[0] == 3
    count = len(channel.typings)
    await asyncio.sleep(0.1)
    assert len(channel.typings) == count
    assert not executor.running


async def test_stop_cleans_setsid_grandchild_and_cancels_queue(rig):
    gateway, storage, executor, channel, path = rig
    await gateway.receive(inbound(1, 'hang'))
    await until(lambda: (path / 'pids.json').exists())
    pids = json.loads((path / 'pids.json').read_text())
    pids += [tree.process.pid for tree in executor.running.values()]
    await gateway.receive(inbound(2, 'queued'))
    await gateway.receive(inbound(3, '/new'))
    await gateway.receive(inbound(4, '/stop'))
    await gateway.idle()
    assert not any(pid in process_table() for pid in pids)
    assert storage.turn_for(1)['status'] == 'cancelled'
    assert storage.turn_for(2)['status'] == 'cancelled'
    assert storage.inbound(3)['state'] == 'cancelled'
    assert any('Cancelled 2 queued' in text for _, text in channel.sent)
    assert len(prompts(path)) == 1
    assert storage.db.execute("SELECT COUNT(*) FROM raw_event WHERE raw_line LIKE '%shutdown_notice%'").fetchone()[0] == 1
    await gateway.receive(inbound(5, 'after'))
    await gateway.idle()
    assert prompts(path)[-1]['prompt'] == 'after'
    assert storage.turn_for(5)['task_id'] == storage.turn_for(1)['task_id']
    assert storage.db.execute('SELECT COUNT(*) FROM task').fetchone()[0] == 1


async def test_abandoned_gateway_recovery_and_redelivery(rig):
    old, storage, executor, channel, path = rig
    await old.receive(inbound(1, 'hang'))
    await until(lambda: (path / 'pids.json').exists() and storage.turn_for(1)['session_id'])
    await old.receive(inbound(2, 'queued'))
    sid = storage.turn_for(1)['session_id']
    pids = json.loads((path / 'pids.json').read_text())
    # Abandon workers mid-turn without the normal gateway shutdown operation.
    for worker in old.workers.values():
        worker.cancel()
    await asyncio.gather(*old.workers.values(), return_exceptions=True)
    await executor.shutdown()
    assert storage.turn_for(1)['status'] == 'running'
    assert not any(pid in process_table() for pid in pids)
    before = storage.db.execute('SELECT COUNT(*) FROM raw_event').fetchone()[0]
    reopened = Storage(path / 'state.db')
    replacement = ClaudeExecutor(path, executor.binary, grace=0.1)
    fake = FakeChannel()
    new = Gateway(reopened, replacement, {'fake': fake}, edit_interval=0.01)
    try:
        await new.start()
        await new.receive(inbound(1, 'hang'))
        await new.receive(inbound(2, 'queued'))
        await asyncio.wait_for(new.idle(), 5)
        logs = prompts(path)
        assert [x['prompt'] for x in logs] == ['hang', RESTART_PROMPT + '\n\nOriginal request:\nhang', 'queued']
        assert logs[1]['resume'] == sid == logs[2]['resume']
        assert reopened.turn_for(1)['recovery_count'] == 1
        assert reopened.turn_for(1)['status'] == 'completed'
        assert reopened.turn_for(2)['status'] == 'completed'
        assert reopened.db.execute('SELECT COUNT(*) FROM turn').fetchone()[0] == 2
        assert reopened.db.execute('SELECT COUNT(*) FROM raw_event').fetchone()[0] > before
        assert any('Service restarted' in text for _, text in fake.sent)
    finally:
        await new.shutdown()
        reopened.close()


async def test_journal_only_recovery_and_cap(rig):
    gateway, storage, executor, channel, path = rig
    first = storage.journal(inbound(1, 'first'))
    task, _ = storage.claim(first['id'], 3)  # Crash before session id was emitted.
    storage.journal(inbound(2, 'next'))
    await gateway.start()
    await gateway.idle()
    assert prompts(path)[0]['prompt'] == 'first'
    assert prompts(path)[0]['resume'] is None
    capped = storage.journal(inbound(3, 'never run'))
    storage.claim(capped['id'], 3)
    with storage.db:
        storage.db.execute('UPDATE turn SET recovery_count=3 WHERE inbound_id=?', (capped['id'],))
    await gateway.start()
    await gateway.idle()
    assert storage.turn_for(capped['id'])['status'] == 'failed'
    assert len(prompts(path)) == 2
    assert any('limit reached' in text for _, text in channel.sent)


async def test_shutdown_retains_running_and_queued(rig):
    gateway, storage, executor, channel, path = rig
    await gateway.receive(inbound(1, 'hang'))
    await until(lambda: (path / 'pids.json').exists())
    pids = json.loads((path / 'pids.json').read_text())
    await gateway.receive(inbound(2, 'next'))
    await gateway.shutdown()
    assert storage.turn_for(1)['status'] == 'running'
    assert storage.turn_for(2)['status'] == 'queued'
    assert not any(pid in process_table() for pid in pids)


@pytest.mark.parametrize('prompt', ['invalid', 'failed', 'crash'])
async def test_failure_does_not_block_queue(rig, prompt):
    gateway, storage, _, _, _ = rig
    await gateway.receive(inbound(1, prompt))
    await gateway.receive(inbound(2, 'next'))
    await asyncio.wait_for(gateway.idle(), 5)
    assert storage.turn_for(1)['status'] == 'failed'
    assert storage.turn_for(2)['status'] == 'completed'


async def test_delivery_errors_do_not_kill_task(rig):
    gateway, storage, _, channel, _ = rig
    async def broken(*args):
        raise RuntimeError('offline delivery')
    channel.send = channel.edit = channel.typing = broken
    await gateway.receive(inbound(1, 'first'))
    await gateway.idle()
    assert storage.turn_for(1)['status'] == 'completed'


async def test_recovered_stop_preempts_recovery(rig):
    gateway, storage, _, channel, path = rig
    row = storage.journal(inbound(1, 'do not run'))
    storage.claim(row['id'], 3)
    storage.journal(inbound(2, '/stop'))
    storage.journal(inbound(3, 'after stop'))
    await gateway.start()
    await gateway.idle()
    assert storage.turn_for(1)['status'] == 'cancelled'
    assert [x['prompt'] for x in prompts(path)] == ['after stop']


async def test_stop_during_spawn(rig):
    gateway, storage, executor, _, path = rig
    await gateway.receive(inbound(1, 'before-init'))
    await until(lambda: bool(executor.starting or executor.running))
    await gateway.receive(inbound(2, '/stop'))
    await gateway.idle()
    assert storage.turn_for(1)['status'] == 'cancelled'
    assert not executor.running and not executor.starting


async def test_stderr_is_drained_and_stored(rig):
    gateway, storage, executor, _, path = rig
    await gateway.receive(inbound(1, 'stderr-flood'))
    await until(lambda: storage.db.execute("SELECT COUNT(*) FROM raw_event WHERE stream='stderr'").fetchone()[0] == 500)
    await gateway.receive(inbound(2, '/stop'))
    await gateway.idle()
    assert storage.turn_for(1)['status'] == 'cancelled'
    assert storage.db.execute("SELECT COUNT(*) FROM raw_event WHERE stream='stderr'").fetchone()[0] == 500
    assert not executor.running


async def test_simultaneous_stop_commands(rig):
    gateway, storage, executor, _, path = rig
    await gateway.receive(inbound(1, 'hang'))
    await until(lambda: (path / 'pids.json').exists())
    await asyncio.gather(gateway.receive(inbound(2, '/stop')),
                         gateway.receive(inbound(3, '/stop')),
                         gateway.receive(inbound(4, 'after')))
    await asyncio.wait_for(gateway.idle(), 5)
    assert storage.turn_for(1)['status'] == 'cancelled'
    assert storage.turn_for(4)['status'] == 'completed'
    assert len(prompts(path)) == 2
    assert not executor.running


async def test_progress_updates_are_throttled():
    from eivoh.gateway import Reply
    channel = FakeChannel()
    times = []
    original_send, original_edit = channel.send, channel.edit
    async def send(*args):
        times.append(asyncio.get_running_loop().time())
        return await original_send(*args)
    async def edit(*args):
        times.append(asyncio.get_running_loop().time())
        return await original_edit(*args)
    channel.send, channel.edit = send, edit
    reply = Reply(channel, '10', 0.05)
    try:
        reply.update('first')
        await until(lambda: bool(channel.sent))
        for index in range(10):
            reply.update(str(index))
            await asyncio.sleep(0.005)
        reply.update('final')
        await reply.finish()
        assert channel.edits[-1][2] == 'final'
        assert all(b - a >= 0.045 for a, b in zip(times, times[1:]))
        assert len(times) < 10
    finally:
        await reply.cancel()


async def test_crash_before_resume_process_emits_id_keeps_original_prompt(rig):
    gateway, storage, _, _, path = rig
    await gateway.receive(inbound(1, 'first'))
    await gateway.idle()
    original_sid = storage.turn_for(1)['session_id']
    row = storage.journal(inbound(2, 'new request before crash'))
    task, _ = storage.claim(row['id'], 3)
    assert task['resume_id'] == original_sid
    assert task['session_id'] is None
    # Service died here, before the second CLI had emitted its session ID.
    await gateway.start()
    await gateway.idle()
    assert prompts(path)[-1]['prompt'] == RESTART_PROMPT + '\n\nOriginal request:\nnew request before crash'
    assert prompts(path)[-1]['resume'] == original_sid
    assert storage.turn_for(2)['status'] == 'completed'
