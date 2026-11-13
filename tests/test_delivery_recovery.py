import asyncio
import json
import io
import threading
from types import SimpleNamespace

import httpx
import pytest
from telegram.error import BadRequest, NetworkError, TimedOut

from conftest import until
from eivoh.channels.base import Inbound
from eivoh.channels.telegram import TelegramChannel
from eivoh.gateway import Gateway
from eivoh.storage import Storage
from test_delivery import setup
from test_tasks import HeldExecutor
from test_telegram import make_bot


@pytest.mark.parametrize('cause', [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
async def test_definitely_unsent_progress_does_not_hide_final(tmp_path, cause):
    storage, bot, channel = setup(tmp_path)
    error = NetworkError('connection unavailable')
    error.__cause__ = cause('synthetic')
    bot.send_message.side_effect = [error, SimpleNamespace(message_id=7)]
    await channel.deliver('10', 'Working', 'turn:1', 1, 1)
    assert storage.delivery_parts('telegram', '10', 'turn:1')[0]['state'] == 'pending'
    channel.prepare_final('10', 'Actual answer', 1, 1)
    await channel.deliver_files('10', [], 'turn:1:final', 1, 1)
    assert bot.send_message.call_args.kwargs['text'] == 'Actual answer'
    assert channel.delivery.settled('10', 'turn:1:final')
    storage.close()


async def test_distinct_unknown_preview_allows_one_stable_final_identity(tmp_path):
    storage, bot, channel = setup(tmp_path)
    bot.send_message.side_effect = TimedOut('ambiguous')
    await channel.deliver('10', 'Working', 'turn:1', 1, 1)
    channel.prepare_final('10', 'Actual answer', 1, 1)
    await channel.deliver_files('10', [], 'turn:1:final', 1, 1)
    assert bot.send_message.await_count == 2
    assert storage.delivery_parts('telegram', '10', 'turn:1:final')[0]['state'] == 'unknown'
    storage.close()
    storage = Storage(tmp_path / 'delivery.db')
    channel.bind(storage)
    storage.recover_deliveries()
    bot.send_message.side_effect = None
    channel.prepare_final('10', 'Actual answer', 1, 1)
    await channel.deliver_files('10', [], 'turn:1:final', 1, 1)
    assert bot.send_message.await_count == 2  # An uncertain final never gets a third identity.
    storage.close()


@pytest.mark.parametrize('other_part', [False, True])
async def test_unknown_plaintext_final_in_preview_is_not_resent(tmp_path, other_part):
    storage, bot, channel = setup(tmp_path)
    # Its actual attempted body has no HTML and includes a tool-status suffix.
    part = '1' if other_part else '0'
    row = storage.prepare_delivery('telegram', '10', 'turn:1', part,
        {'kind': 'text', 'text': '#t1 Old label\nComplete answer\n\nTool: shell'}, 1, 1)
    attempt = storage.begin_delivery(row['id'], 'send', json.loads(row['payload']))
    storage.delivery_outcome(row['id'], attempt, 'unknown')
    channel.prepare_final('10', '**Complete answer**', 1, 1)
    await channel.deliver_files('10', [], 'turn:1:final', 1, 1)
    assert bot.send_message.await_count == 0
    assert storage.delivery_parts('telegram', '10', 'turn:1:final')[0]['state'] == 'unknown'
    storage.close()


@pytest.mark.parametrize('known', [False, True])
async def test_final_identity_creation_and_preview_adoption_are_atomic(tmp_path, monkeypatch, known):
    storage, bot, channel = setup(tmp_path)
    preview = storage.prepare_delivery('telegram', '10', 'turn:1', '0', {'kind': 'text', 'text': 'Final'}, 1, 1)
    attempt = storage.begin_delivery(preview['id'], 'send', json.loads(preview['payload']))
    storage.delivery_outcome(preview['id'], attempt, 'sent' if known else 'unknown', '7' if known else None)
    original = storage.prepare_delivery
    def interrupt(*args, **kwargs):
        row = original(*args, **kwargs)
        if args[2] == 'turn:1:final':
            raise RuntimeError('interrupted before preview adoption')
        return row
    monkeypatch.setattr(storage, 'prepare_delivery', interrupt)
    with pytest.raises(RuntimeError):
        channel.prepare_final('10', 'Final', 1, 1)
    assert not storage.delivery_parts('telegram', '10', 'turn:1:final')
    monkeypatch.setattr(storage, 'prepare_delivery', original)
    storage.close()
    storage = Storage(tmp_path / 'delivery.db')
    channel.bind(storage)
    channel.prepare_final('10', 'Final', 1, 1)
    await channel.deliver_files('10', [], 'turn:1:final', 1, 1)
    assert bot.send_message.await_count == 0
    assert (bot.edit_message_text.await_count == 1) == known
    storage.close()


async def test_final_edit_retries_while_process_stays_alive(tmp_path):
    storage = Storage(tmp_path / 'state.db')
    bot = make_bot()
    channel = TelegramChannel('unused', 1, bot)
    executor = HeldExecutor()
    gateway = Gateway(storage, executor, {'telegram': channel}, edit_interval=0, delivery_retry_delays=(.02, .04, .08))
    error = NetworkError('temporary')
    error.__cause__ = httpx.ConnectError('temporary')
    try:
        await gateway.receive(Inbound('telegram', '10', '1', '1', 'hello'))
        await until(lambda: bot.send_message.await_count == 1)
        bot.edit_message_text.side_effect = [error, error, True]
        executor.releases[1].set()
        await gateway.idle()
        assert storage.output(1)['delivered'] == 1
        assert bot.edit_message_text.await_count == 3
        assert bot.send_message.await_count == 1  # No transient pending-status notice.
    finally:
        await gateway.shutdown()
        storage.close()


async def test_slow_preview_ack_after_executor_completion_is_drained(tmp_path):
    storage = Storage(tmp_path / 'state.db')
    bot = make_bot()
    entered, release = asyncio.Event(), asyncio.Event()
    async def send(**kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(message_id=7)
    bot.send_message.side_effect = send
    channel = TelegramChannel('unused', 1, bot)
    executor = HeldExecutor()
    gateway = Gateway(storage, executor, {'telegram': channel}, edit_interval=0)
    try:
        await gateway.receive(Inbound('telegram', '10', '1', '1', 'hello'))
        await entered.wait()
        executor.releases[1].set()
        await until(lambda: storage.turn_for(1)['status'] == 'completed')
        release.set()
        await gateway.idle()
        assert bot.send_message.await_count == 1
        assert storage.output(1)['delivered'] == 1
        assert storage.delivery_parts('telegram', '10', 'turn:1:final')[0]['message_id'] == '7'
        assert not storage.db.execute("SELECT 1 FROM delivery_attempt WHERE state='unknown'").fetchone()
    finally:
        release.set()
        await gateway.shutdown()
        storage.close()


async def test_commands_admit_during_upload_and_pending_reply_recovers(tmp_path):
    path = tmp_path / 'state.db'
    storage = Storage(path)
    bot = make_bot()
    channel = TelegramChannel('unused', 1, bot)
    executor = HeldExecutor()
    gateway = Gateway(storage, executor, {'telegram': channel}, edit_interval=0)
    lock = channel.locks.setdefault('10', asyncio.Lock())
    await lock.acquire()
    try:
        await asyncio.wait_for(gateway.receive(Inbound('telegram', '10', '1', '1', '/new task')), 1)
        await asyncio.wait_for(gateway.receive(Inbound('telegram', '10', '1', '2', '/tasks')), 1)
        await asyncio.wait_for(gateway.receive(Inbound('telegram', '10', '1', '3', '/stop')), 1)
        assert all(storage.inbound(i)['state'] == 'done' for i in (1, 2, 3))
        assert bot.send_message.await_count == 0
        assert storage.delivery_parts('telegram', '10', 'inbound:2:reply')[0]['state'] == 'pending'
    finally:
        await gateway.shutdown()
        lock.release()
        storage.close()
    storage = Storage(path)
    channel = TelegramChannel('unused', 1, bot)
    restored = Gateway(storage, executor, {'telegram': channel}, edit_interval=0)
    try:
        await restored.start()
        await until(lambda: bot.send_message.await_count >= 1)
        assert len(storage.tasks(1)) == 1 and not executor.calls
        assert storage.delivery_parts('telegram', '10', 'inbound:2:reply')[0]['state'] == 'sent'
    finally:
        await restored.shutdown()
        storage.close()


async def test_prepared_terminal_failure_does_not_rerun_after_crash(tmp_path):
    storage, bot, channel = setup(tmp_path)
    storage.claim(1, 3)
    storage.save_output(1, 'Task failed: executor ended without a completed turn', [])
    executor = HeldExecutor()
    gateway = Gateway(storage, executor, {'telegram': channel}, edit_interval=0)
    try:
        await gateway.start()
        await gateway.idle()
        assert not executor.calls
        assert storage.turn_for(1)['status'] == 'failed' and storage.output(1)['delivered'] == 1
        assert 'executor ended' in bot.send_message.call_args.kwargs['text']
    finally:
        await gateway.shutdown()
        storage.close()


async def test_stop_during_background_snapshot_never_publishes_late_output(tmp_path, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def snapshot(*args):
        entered.set()
        release.wait(5)
        finished.set()
        return 'late result', []
    monkeypatch.setattr('eivoh.gateway.snapshot_outputs', snapshot)
    storage = Storage(tmp_path / 'state.db')
    bot = make_bot()
    channel = TelegramChannel('unused', 1, bot)
    executor = HeldExecutor()
    gateway = Gateway(storage, executor, {'telegram': channel}, edit_interval=0)
    try:
        await gateway.receive(Inbound('telegram', '10', '1', '1', 'hello'))
        await until(lambda: executor.calls)
        executor.releases[1].set()
        await until(entered.is_set)
        await asyncio.wait_for(gateway.receive(Inbound('telegram', '10', '1', '2', '/stop')), 1)
        assert storage.turn_for(1)['status'] == 'cancelled' and storage.output(1) is None
        release.set()
        await until(finished.is_set)
        await asyncio.sleep(0)
        assert storage.output(1) is None
    finally:
        release.set()
        await gateway.shutdown()
        storage.close()


async def test_cancelled_hash_worker_owns_handle_until_read_finishes(monkeypatch):
    from eivoh.channels.delivery import open_snapshot
    entered, release = threading.Event(), threading.Event()
    blob = io.BytesIO(b'snapshot')
    def checked(payload):
        entered.set()
        release.wait(5)
        assert not blob.closed
        return blob
    monkeypatch.setattr('eivoh.channels.delivery.checked_snapshot', checked)
    task = asyncio.create_task(open_snapshot({}))
    try:
        await until(entered.is_set)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not blob.closed
        release.set()
        await until(lambda: blob.closed)
    finally:
        release.set()


async def test_unsupported_media_caption_is_journaled_and_reported(tmp_path):
    from unittest.mock import AsyncMock
    from test_polling_reliability import update
    storage = Storage(tmp_path / 'state.db')
    bot = make_bot()
    item = update(1, 1)
    item.effective_message.video = SimpleNamespace(file_id='video')
    item.effective_message.caption = '/stop is a caption'
    bot.get_updates = AsyncMock(side_effect=[[item], asyncio.CancelledError()])
    channel = TelegramChannel('unused', 1, bot)
    executor = HeldExecutor()
    gateway = Gateway(storage, executor, {'telegram': channel}, edit_interval=0)
    try:
        with pytest.raises(asyncio.CancelledError):
            await channel.poll(gateway.receive)
        await gateway.idle()
        assert storage.inbound(1)['text'] == '/stop is a caption'
        assert storage.turn_for(1)['status'] == 'failed' and not executor.calls
        assert 'not supported' in bot.send_message.call_args.kwargs['text']
    finally:
        await gateway.shutdown()
        storage.close()
