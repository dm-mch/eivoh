import asyncio
import hashlib
import json
import itertools
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, RetryAfter, TimedOut

from conftest import until
from eivoh.attachments import output_directory, snapshot_outputs
from eivoh.channels.base import Inbound
from eivoh.channels.telegram import TelegramChannel
from eivoh.gateway import Gateway
from eivoh.storage import Storage
from test_tasks import HeldExecutor
from test_telegram import make_bot


def setup(tmp_path):
    storage = Storage(tmp_path / 'delivery.db')
    storage.journal(Inbound('telegram', '10', '1', '1', 'request'))
    bot = make_bot()
    bot.send_document = AsyncMock(return_value=SimpleNamespace(message_id=20))
    bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=21))
    channel = TelegramChannel('unused', 1, bot)
    channel.bind(storage)
    return storage, bot, channel


async def test_split_ids_committed_per_send_and_restart_edits(tmp_path):
    storage, bot, channel = setup(tmp_path)
    ids = itertools.count(7)
    async def send(**kwargs):
        mid = next(ids)
        if mid > 7:
            assert storage.reply_task(1, str(mid - 1))['id'] == 1
        return SimpleNamespace(message_id=mid)
    bot.send_message.side_effect = send
    long = 'abc\n' * 2500
    await channel.deliver('10', long, 'turn:1', 1, 1)
    assert bot.send_message.await_count == 3
    storage.close()
    storage = Storage(tmp_path / 'delivery.db')
    restored = TelegramChannel('unused', 1, bot)
    restored.bind(storage)
    storage.recover_deliveries()
    await restored.deliver('10', 'finished', 'turn:1', 1, 1)
    assert bot.send_message.await_count == 3
    assert bot.edit_message_text.call_args.kwargs['message_id'] == 7
    assert bot.delete_message.await_count == 2
    await restored.deliver('10', long, 'turn:1', 1, 1)
    # Deleted parts are new physical sends, never edits of removed ids.
    assert bot.send_message.await_count == 5
    assert restored.delivery.settled('10', 'turn:1')
    storage.close()


async def test_ambiguous_send_keeps_final_intent_and_no_tail_replay(tmp_path):
    storage, bot, channel = setup(tmp_path)
    bot.send_message.side_effect = TimedOut('may have posted')
    await channel.deliver('10', 'x' * 9000, 'turn:1', 1, 1)
    assert bot.send_message.await_count == 1
    await channel.deliver('10', 'final result', 'turn:1', 1, 1)
    storage.recover_deliveries()
    bot.send_message.side_effect = None
    await channel.deliver('10', 'final result', 'turn:1', 1, 1)
    assert bot.send_message.await_count == 1
    first = storage.delivery_parts('telegram', '10', 'turn:1')[0]
    assert first['state'] == 'unknown' and 'final result' in first['payload']
    storage.close()


async def test_shrink_retires_never_sent_tail(tmp_path):
    storage, bot, channel = setup(tmp_path)
    storage.prepare_delivery('telegram', '10', 'turn:1', '0', {'kind': 'text', 'text': 'progress'}, 1, 1)
    storage.prepare_delivery('telegram', '10', 'turn:1', '1', {'kind': 'text', 'text': 'unsent tail'}, 1, 1)
    await channel.deliver('10', 'short final', 'turn:1', 1, 1)
    assert channel.delivery.settled('10', 'turn:1')
    assert bot.send_message.await_count == 1 and bot.delete_message.await_count == 0
    storage.close()


@pytest.mark.parametrize('crash', [False, True])
async def test_final_revision_during_initial_send_and_restart_window(tmp_path, crash):
    storage, bot, channel = setup(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    async def send(**kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(message_id=7)
    bot.send_message.side_effect = send
    first = asyncio.create_task(channel.deliver('10', 'progress', 'turn:1', 1, 1))
    await entered.wait()
    final = asyncio.create_task(channel.deliver('10', 'final', 'turn:1', 1, 1))
    await until(lambda: storage.delivery_parts('telegram', '10', 'turn:1')[0]['revision'] == 2)
    if crash:
        first.cancel()
        final.cancel()
        await asyncio.gather(first, final, return_exceptions=True)
        storage.recover_deliveries()
        row = storage.delivery_parts('telegram', '10', 'turn:1')[0]
        assert row['state'] == 'unknown' and row['message_id'] is None
        await channel.deliver('10', 'final', 'turn:1', 1, 1)
        assert bot.send_message.await_count == 1
    else:
        release.set()
        await asyncio.gather(first, final)
        assert bot.send_message.await_count == 1
        assert bot.edit_message_text.call_args.kwargs['text'] == 'final'
        assert channel.delivery.settled('10', 'turn:1')
    storage.close()


@pytest.mark.parametrize('stale_retry', [False, True])
def test_hard_crash_after_new_revision_before_send_ack(tmp_path, stale_retry):
    storage, _, _ = setup(tmp_path)
    row = storage.prepare_delivery('telegram', '10', 'turn:1', '0', {'kind': 'text', 'text': 'progress'}, 1, 1)
    attempt = storage.begin_delivery(row['id'], 'send', json.loads(row['payload']), row['revision'])
    if stale_retry:
        storage.delivery_outcome(row['id'], attempt, 'pending', detail='Flood refusal')
    storage.prepare_delivery('telegram', '10', 'turn:1', '0', {'kind': 'text', 'text': 'final'}, 1, 1)
    if stale_retry:
        storage.begin_delivery(row['id'], 'send', json.loads(row['payload']), row['revision'])
    storage.close()
    storage = Storage(tmp_path / 'delivery.db')
    storage.recover_deliveries()
    assert storage.delivery(row['id'])['state'] == 'unknown'
    assert storage.db.execute('SELECT state FROM delivery_attempt ORDER BY id DESC LIMIT 1').fetchone()[0] == 'unknown'
    storage.close()


async def test_files_fallback_ambiguous_upload_and_reply_route(tmp_path):
    storage, bot, channel = setup(tmp_path)
    root = output_directory(tmp_path, 1)
    (root / 'picture.png').write_bytes(b'png bytes')
    _, files = snapshot_outputs(tmp_path, 1, 'EIVOH_ARTIFACT {"path":"picture.png","kind":"photo"}')
    bot.send_photo.side_effect = BadRequest('PHOTO_INVALID_DIMENSIONS')
    async def document(**kwargs):
        assert kwargs['document'].input_file_content.read() == b'png bytes'
        assert storage.db.execute("SELECT COUNT(*) FROM delivery_attempt WHERE state='sending'").fetchone()[0] == 1
        return SimpleNamespace(message_id=20)
    bot.send_document.side_effect = document
    await channel.deliver_files('10', files, 'turn:1', 1, 1)
    assert bot.send_photo.await_count == bot.send_document.await_count == 1
    assert storage.reply_task(1, '20')['id'] == 1
    await channel.deliver_files('10', files, 'turn:1', 1, 1)
    assert bot.send_document.await_count == 1
    bot.send_photo.side_effect = TimedOut('ambiguous upload')
    await channel.deliver_files('10', files, 'turn:other', 1, 1)
    storage.recover_deliveries()
    await channel.deliver_files('10', files, 'turn:other', 1, 1)
    assert bot.send_photo.await_count == 2 and bot.send_document.await_count == 1
    storage.close()


async def test_completed_output_recovery_and_stop_suppresses_restart_upload(tmp_path):
    storage, bot, channel = setup(tmp_path)
    root = output_directory(tmp_path, 1)
    (root / 'report.txt').write_text('report')
    clean, files = snapshot_outputs(tmp_path, 1, 'Answer\nEIVOH_ARTIFACT {"path":"report.txt","kind":"document"}')
    storage.save_output(1, clean, files)
    storage.finish(1, 'completed')
    entered = asyncio.Event()
    async def upload(**kwargs):
        entered.set()
        await asyncio.Event().wait()
    bot.send_document.side_effect = upload
    executor = HeldExecutor()
    gateway = Gateway(storage, executor, {'telegram': channel}, edit_interval=0)
    try:
        await gateway.start()
        await entered.wait()
        await asyncio.wait_for(gateway.receive(Inbound('telegram', '10', '1', '2', '/stop')), 1)
        assert storage.output(1)['cancelled'] == 1
        assert not executor.calls and not gateway.output_jobs
        assert not storage.pending_outputs()
    finally:
        await gateway.shutdown()
    restored = Gateway(storage, executor, {'telegram': channel}, edit_interval=0)
    try:
        await restored.start()
        await restored.idle()
        assert bot.send_document.await_count == 1
    finally:
        await restored.shutdown()
        storage.close()


async def test_completed_output_survives_before_first_delivery(tmp_path):
    storage, bot, channel = setup(tmp_path)
    storage.save_output(1, 'retained final', [])
    storage.finish(1, 'completed')
    executor = HeldExecutor()
    gateway = Gateway(storage, executor, {'telegram': channel}, edit_interval=0)
    try:
        await gateway.start()
        await gateway.idle()
        assert not executor.calls
        assert bot.send_message.call_args.kwargs['text'] == 'retained final'
        assert storage.output(1)['delivered'] == 1
    finally:
        await gateway.shutdown()
        storage.close()


async def test_final_intent_survives_reply_timeout_after_progress_ack(tmp_path):
    storage, bot, channel = setup(tmp_path)
    executor = HeldExecutor()
    gateway = Gateway(storage, executor, {'telegram': channel}, edit_interval=0)
    row = storage.inbound(1)
    try:
        # Simulate a progress request already in flight when execution finishes.
        part = storage.prepare_delivery('telegram', '10', 'turn:1', '0', {'kind': 'text', 'text': 'progress'}, 1, 1)
        attempt = storage.begin_delivery(part['id'], 'send', json.loads(part['payload']), part['revision'])
        await gateway._prepare_output(row, 1, 'authoritative final', prepare_reply=False)
        storage.finish(1, 'completed')
        # Old progress succeeds, then Reply.finish times out before its final loop.
        storage.delivery_outcome(part['id'], attempt, 'sent', '7')
        gateway._prepare_final_reply(row, 1, 1, 'authoritative final')
        final = storage.delivery_parts('telegram', '10', 'turn:1:final')[0]
        assert final['state'] == 'pending' and final['message_id'] == '7'
        class TimedOutReply:
            def update(self, *args, **kwargs):
                pass
            async def finish(self):
                pass  # Reply.finish's timeout path returns after cancelling its job.
        await gateway._deliver_output(row, 1, 1, TimedOutReply())
        assert bot.edit_message_text.call_args.kwargs['text'] == 'authoritative final'
        assert bot.send_message.await_count == 0
        assert storage.output(1)['delivered'] == 1
    finally:
        await gateway.shutdown()
        storage.close()
