import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import RetryAfter

from conftest import inbound
from eivoh.channels.telegram import TelegramChannel
from eivoh.executors.claude import parse_line


@pytest.mark.parametrize('failed', [False, True])
async def test_recovery_uses_stored_result_without_executor(rig, failed):
    gateway, storage, _, channel, path = rig
    row = storage.journal(inbound(1, 'do work once'))
    turn, _ = storage.claim(row['id'], 3)
    import json
    storage.append(turn['id'], parse_line(json.dumps({
        'type': 'result', 'result': 'already done', 'is_error': failed})))
    # A stored result takes priority even when the restart budget is exhausted.
    with storage.db:
        storage.db.execute('UPDATE turn SET recovery_count=3 WHERE id=?', (turn['id'],))
    await gateway.start()
    await gateway.idle()
    assert not (path / 'prompts.jsonl').exists()
    assert any(text.startswith('already done') for _, text in channel.sent)
    assert storage.turn_for(1)['status'] == ('failed' if failed else 'completed')


async def test_recovery_after_init_includes_original_request(rig):
    gateway, storage, _, _, path = rig
    row = storage.journal(inbound(1, 'unique original request'))
    turn, _ = storage.claim(row['id'], 3)
    storage.append(turn['id'], parse_line('{"type":"system","subtype":"init","session_id":"session-1"}\n'))
    await gateway.start()
    await gateway.idle()
    import json
    prompt = json.loads((path / 'prompts.jsonl').read_text())['prompt']
    assert 'unique original request' in prompt
    assert 'Check existing work' in prompt


@pytest.mark.parametrize('delay', [12.5, timedelta(seconds=12.5)])
async def test_poll_retry_after_preserves_offset(monkeypatch, delay):
    monkeypatch.setenv('PTB_TIMEDELTA', '1' if isinstance(delay, timedelta) else '0')
    update = SimpleNamespace(update_id=40, effective_message=None,
                             effective_user=None, effective_chat=None)
    bot = SimpleNamespace(get_updates=AsyncMock(side_effect=[
        [update], RetryAfter(delay), asyncio.CancelledError()]))
    channel = TelegramChannel('unused', 1, bot)
    sleep = AsyncMock()
    monkeypatch.setattr('eivoh.channels.telegram.asyncio.sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        await channel.poll(AsyncMock())
    assert [c.kwargs['offset'] for c in bot.get_updates.call_args_list] == [None, 41, 41]
    assert sleep.call_args.args[0] >= 12.5


@pytest.mark.parametrize('ending', ['failed', 'completed', 'exception', 'error'])
async def test_shutdown_before_executor_exit_stays_recoverable(rig, ending):
    gateway, storage, _, _, _ = rig

    class EndingExecutor:
        async def start(self, run_id, prompt, model=None, effort=None):
            # Signal handler runs before the consumer sees the CLI's exit.
            gateway.begin_shutdown()
            if ending == 'exception':
                raise RuntimeError('CLI exited during shutdown')
            if ending == 'error':
                yield parse_line('{"type":"error","error":"terminated"}\n')
            else:
                yield parse_line('{"type":"result","is_error":' +
                                 ('true' if ending == 'failed' else 'false') +
                                 ',"result":"termination output"}\n')

        cancel = AsyncMock()
        shutdown = AsyncMock()

    gateway.executor = EndingExecutor()
    await gateway.receive(inbound(1, 'keep this request'))
    await gateway.idle()
    turn = storage.turn_for(1)
    assert turn['status'] == 'running'
    assert storage.inbound(1)['state'] == 'running'
    assert storage.stored_result(turn['id']) is None
    records = storage.db.execute('SELECT * FROM raw_event WHERE turn_id=? ORDER BY sequence',
                                 (turn['id'],)).fetchall()
    prompts = [record for record in records if record['stream'] == 'meta']
    assert len(prompts) == 1
    assert json.loads(prompts[0]['payload']) == {
        'type': 'turn_prompt', 'prompt': 'keep this request'}
    physical = [record for record in records if record['stream'] == 'stdout']
    assert len(physical) == (0 if ending == 'exception' else 1)
    for record in physical:
        assert record['raw_line'] is not None
        assert record['sequence'] > prompts[0]['sequence']
        assert not any(event['kind'] in {'error', 'turn_completed'}
                       for event in json.loads(record['events']))


async def test_final_delivery_honours_backoff_over_ten_seconds(monkeypatch):
    from eivoh.gateway import Reply
    # Record the long back-off instead of waiting it out; short sleeps stay real.
    real_sleep, long_waits = asyncio.sleep, []

    async def sleep(delay, *args, **kwargs):
        if delay >= 10:
            long_waits.append(delay)
            delay = 0
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, 'sleep', sleep)
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=[
        RetryAfter(10.1), SimpleNamespace(message_id=5)]))
    channel = TelegramChannel('unused', 1, bot)
    reply = Reply(channel, '10', 0)
    try:
        reply.update('final answer')
        await reply.finish()
        assert bot.send_message.call_count == 2
        assert reply.sent == 'final answer'
        assert len(long_waits) == 1 and long_waits[0] >= 10.1
    finally:
        await reply.cancel()


async def test_shutdown_cancels_final_delivery_backoff(rig):
    gateway, storage, _, channel, _ = rig
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def send(*args):
        entered.set()
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    channel.send = send
    await gateway.receive(inbound(1, 'answer'))
    await entered.wait()
    from conftest import until
    await until(lambda: storage.turn_for(1)['status'] == 'completed')
    await asyncio.wait_for(gateway.shutdown(), 2)
    assert cancelled.is_set()


async def test_final_update_during_inflight_send_is_delivered():
    from conftest import FakeChannel
    from eivoh.gateway import Reply
    channel = FakeChannel()
    entered, release = asyncio.Event(), asyncio.Event()
    original_send = channel.send

    async def send(*args):
        entered.set()
        await release.wait()
        return await original_send(*args)

    channel.send = send
    reply = Reply(channel, '10', 0)
    try:
        reply.update('partial')
        await entered.wait()
        reply.update('final')
        finishing = asyncio.create_task(reply.finish())
        release.set()
        await finishing
        assert channel.edits[-1][2] == 'final'
    finally:
        await reply.cancel()


async def test_final_delivery_has_sixty_second_bound(monkeypatch):
    from conftest import FakeChannel
    from eivoh.gateway import Reply
    channel = FakeChannel()
    channel.send = AsyncMock(side_effect=lambda *args: None)
    entered = asyncio.Event()
    async def blocked_send(*args):
        entered.set()
        await asyncio.Event().wait()
    channel.send = blocked_send
    reply = Reply(channel, '10', 0)
    original_wait_for = asyncio.wait_for
    budgets = []
    async def shortened_wait_for(job, timeout):
        budgets.append(timeout)
        return await original_wait_for(job, 0.01)
    try:
        reply.update('final')
        await entered.wait()
        monkeypatch.setattr('eivoh.gateway.asyncio.wait_for', shortened_wait_for)
        await reply.finish()
        assert budgets == [60]
        assert reply.job.cancelled()
    finally:
        await reply.cancel()
