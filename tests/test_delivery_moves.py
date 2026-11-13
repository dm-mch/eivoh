import asyncio
import itertools
import json
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, RetryAfter, TimedOut

from eivoh.channels.base import Inbound
from eivoh.channels.telegram import TelegramChannel
from eivoh.gateway import Gateway
from eivoh.storage import Storage
from test_delivery import setup
from test_tasks import HeldExecutor


def moving_setup(tmp_path):
    storage, bot, channel = setup(tmp_path)
    ids = itertools.count(10, 10)
    bot.send_message.side_effect = lambda **kwargs: SimpleNamespace(message_id=next(ids))
    return storage, bot, channel


def owner(storage, mid='11', chat='10', channel='telegram'):
    return storage.journal(Inbound(channel, chat, '1', mid, '/tasks'))


def text_parts(storage, logical='turn:1'):
    return [row for row in storage.delivery_parts('telegram', '10', logical) if row['part'].isdecimal()]


async def test_owner_displaces_only_on_change_then_edits_new_id_and_routes_reply(tmp_path):
    storage, bot, channel = moving_setup(tmp_path)
    assert await channel.deliver('10', 'Working', 'turn:1', 1, 1) == '10'
    owner(storage)
    await channel.deliver('10', 'Working', 'turn:1', 1, 1)
    assert bot.send_message.await_count == 1 and not bot.delete_message.called
    assert await channel.deliver('10', 'More progress', 'turn:1', 1, 1) == '20'
    bot.delete_message.assert_awaited_once_with(chat_id='10', message_id=10)
    assert not bot.edit_message_text.called
    await channel.deliver('10', 'Still working', 'turn:1', 1, 1)
    assert bot.edit_message_text.call_args.kwargs['message_id'] == 20
    gateway = Gateway(storage, HeldExecutor(), {'telegram': channel})
    gateway._remember(storage.inbound(1), '20', 1, 1)
    assert channel.parts('10', '20') == ['20']
    assert storage.reply_task(1, '20')['id'] == 1
    reply = storage.journal(Inbound('telegram', '10', '1', '21', 'follow up', reply_to='20'))
    assert storage.turn_for(reply['id'])['task_id'] == 1
    assert storage.reply_task(1, '10')['id'] == 1  # Old mappings remain harmless.
    storage.close()


@pytest.mark.parametrize('source', ['other_task', 'notice', 'file'])
async def test_other_delivery_displaces_preview(tmp_path, source):
    storage, bot, channel = moving_setup(tmp_path)
    await channel.deliver('10', 'Working', 'turn:1', 1, 1)
    if source == 'other_task':
        command = storage.journal(Inbound('telegram', '10', '1', '2', '/new'))
        task = storage.new_task(command['id'])
        await channel.deliver('10', 'Other task', 'turn:2', task['id'])
    elif source == 'notice':
        await channel.deliver('10', 'Notice', 'notice', 1)
    else:
        row = storage.prepare_delivery('telegram', '10', 'other:files', 'file:0', {'kind': 'document'}, 1)
        attempt = storage.begin_delivery(row['id'], 'send', {'kind': 'document'})
        storage.delivery_outcome(row['id'], attempt, 'sent', '20')
    assert storage.delivery_displaced('telegram', '10', 'turn:1')
    await channel.deliver('10', 'More progress', 'turn:1', 1, 1)
    assert bot.delete_message.call_args.kwargs['message_id'] == 10
    assert not bot.edit_message_text.called
    storage.close()


@pytest.mark.parametrize('source', ['callback', 'other_chat', 'other_channel', 'own_final', 'older_owner'])
async def test_unrelated_ids_do_not_displace(tmp_path, source):
    storage, bot, channel = moving_setup(tmp_path)
    await channel.deliver('10', 'Working', 'turn:1', 1, 1)
    if source == 'callback':
        owner(storage, 'cb:999')
    elif source == 'other_chat':
        owner(storage, '999', chat='20')
    elif source == 'other_channel':
        owner(storage, '999', channel='fake')
    elif source == 'older_owner':
        owner(storage, '2')
    else:
        row = storage.prepare_delivery('telegram', '10', 'turn:1:final', '0', {'kind': 'text', 'text': 'Final'}, 1, 1)
        attempt = storage.begin_delivery(row['id'], 'send', json.loads(row['payload']))
        storage.delivery_outcome(row['id'], attempt, 'sent', '999')
    assert not storage.delivery_displaced('telegram', '10', 'turn:1')
    await channel.deliver('10', 'More progress', 'turn:1', 1, 1)
    assert bot.send_message.await_count == 1 and not bot.delete_message.called
    assert bot.edit_message_text.call_args.kwargs['message_id'] == 10
    storage.close()


async def test_multipart_moves_all_parts_in_numeric_order_before_later_edits(tmp_path):
    storage, bot, channel = moving_setup(tmp_path)
    long = 'abc\n' * 2500
    await channel.deliver('10', long, 'turn:1', 1, 1)
    owner(storage, '31')
    operations = []
    async def delete(**kwargs):
        operations.append(('delete', kwargs['message_id']))
        return True
    async def send(**kwargs):
        assert bot.delete_message.await_count == 3
        mid = 40 + 10 * sum(kind == 'send' for kind, _ in operations)
        operations.append(('send', kwargs['text']))
        return SimpleNamespace(message_id=mid)
    bot.delete_message.side_effect = delete
    bot.send_message.side_effect = send
    await channel.deliver('10', long + 'tail', 'turn:1', 1, 1)
    assert operations[:3] == [('delete', 10), ('delete', 20), ('delete', 30)]
    assert [body for kind, body in operations[3:]] == [json.loads(row['payload'])['text'] for row in text_parts(storage)]
    assert channel.parts('10', '40') == ['40', '50', '60']
    assert all(storage.reply_task(1, mid)['id'] == 1 for mid in ('40', '50', '60'))
    await channel.deliver('10', long + 'tail again', 'turn:1', 1, 1)
    assert bot.send_message.await_count == 6 and bot.delete_message.await_count == 3
    assert bot.edit_message_text.call_args.kwargs['message_id'] == 60
    storage.close()


async def test_displaced_preview_growing_only_a_new_part_moves_unchanged_prefix(tmp_path):
    storage, bot, channel = moving_setup(tmp_path)
    await channel.deliver('10', 'x' * 4096, 'turn:1', 1, 1)
    owner(storage)
    await channel.deliver('10', 'x' * 4096 + 'tail', 'turn:1', 1, 1)
    bot.delete_message.assert_awaited_once_with(chat_id='10', message_id=10)
    assert [call.kwargs['text'] for call in bot.send_message.call_args_list] == ['x' * 4096, 'x' * 4096, 'tail']
    assert channel.parts('10', '20') == ['20', '30']
    storage.close()


@pytest.mark.parametrize('displaced', [False, True])
async def test_final_identical_preview_moves_only_if_displaced(tmp_path, displaced):
    storage, bot, channel = moving_setup(tmp_path)
    await channel.deliver('10', 'Complete answer', 'turn:1', 1, 1)
    if displaced:
        owner(storage)
    channel.prepare_final('10', 'Complete answer', 1, 1)
    await channel.deliver_files('10', [], 'turn:1:final', 1, 1)
    assert bot.send_message.await_count == 1 + displaced
    assert bot.delete_message.await_count == displaced
    assert bot.edit_message_text.await_count == (not displaced)
    assert channel.parts('10', '20' if displaced else '10') == ['20' if displaced else '10']
    assert channel.delivery.settled('10', 'turn:1:final')
    # Retrying a completed final does not churn on a later owner message.
    owner(storage, '100')
    channel.prepare_final('10', 'Complete answer', 1, 1)
    await channel.deliver_files('10', [], 'turn:1:final', 1, 1)
    assert bot.send_message.await_count == 1 + displaced
    storage.close()


@pytest.mark.parametrize('finish', [False, True])
async def test_restart_during_delete_recovers_before_send_and_final_inherits_cleanup(tmp_path, finish):
    storage, bot, channel = moving_setup(tmp_path)
    await channel.deliver('10', 'Working', 'turn:1', 1, 1)
    owner(storage)
    channel.prepare_reply('10', 'More progress', 'turn:1', 1, 1)
    assert storage.move_delivery('telegram', '10', 'turn:1')
    delete = storage.delivery_parts('telegram', '10', 'turn:1')[-1]
    storage.begin_delivery(delete['id'], 'delete', json.loads(delete['payload']))
    storage.close()
    storage = Storage(tmp_path / 'delivery.db')
    channel.bind(storage)
    storage.recover_deliveries()
    bot.delete_message.side_effect = BadRequest('Message to delete not found')
    if finish:
        channel.prepare_final('10', 'Final', 1, 1)
        await channel.deliver_files('10', [], 'turn:1:final', 1, 1)
        assert storage.delivery(delete['id'])['cancelled']
    else:
        await channel.deliver('10', 'More progress', 'turn:1', 1, 1)
    bot.delete_message.assert_awaited_once_with(chat_id='10', message_id=10)
    assert bot.send_message.await_count == 2
    assert storage.db.execute("SELECT COUNT(*) FROM delivery_attempt WHERE operation='delete' AND state='sent'").fetchone()[0] == 1
    storage.recover_deliveries()
    await channel.delivery.flush('10', 'turn:1:final' if finish else 'turn:1')
    assert bot.send_message.await_count == 2 and bot.delete_message.await_count == 1
    storage.close()


@pytest.mark.parametrize('preview', [False, True])
@pytest.mark.parametrize('crash', [False, True])
async def test_unknown_replacement_never_replayed_and_final_reports_it(tmp_path, preview, crash):
    storage, bot, channel = moving_setup(tmp_path)
    await channel.deliver('10', 'Working', 'turn:1', 1, 1)
    owner(storage)
    if preview:
        logical = 'turn:1'
        channel.prepare_reply('10', 'Final answer', logical, 1, 1)
    else:
        logical = 'turn:1:final'
        channel.prepare_final('10', 'Final answer', 1, 1)
    if crash:
        assert storage.move_delivery('telegram', '10', logical)
        delete = storage.delivery_parts('telegram', '10', logical)[-1]
        await channel.delivery._part(delete)
        row = text_parts(storage, logical)[0]
        storage.begin_delivery(row['id'], 'send', json.loads(row['payload']))
    else:
        bot.send_message.side_effect = TimedOut('replacement may have posted')
        await channel.delivery.flush('10', logical)
    assert bot.delete_message.await_count == 1
    calls = bot.send_message.await_count
    storage.save_output(1, 'Final answer', [])
    storage.finish(1, 'completed')
    storage.close()
    storage = Storage(tmp_path / 'delivery.db')
    channel.bind(storage)
    storage.recover_deliveries()
    bot.send_message.side_effect = lambda **kwargs: SimpleNamespace(message_id=30)
    gateway = Gateway(storage, HeldExecutor(), {'telegram': channel}, delivery_retry_delays=())
    try:
        await gateway._deliver_output(storage.inbound(1), 1, 1)
        await asyncio.gather(*(job for jobs in gateway.notice_jobs.values() for job in jobs))
        assert text_parts(storage, 'turn:1:final')[0]['state'] == 'unknown'
        assert not storage.output(1)['delivered']
        assert bot.send_message.await_count == calls + 1  # Only the warning.
        assert 'unconfirmed delivery and were not resent' in bot.send_message.call_args.kwargs['text']
        channel.prepare_final('10', 'Final answer', 1, 1)
        await channel.deliver_files('10', [], 'turn:1:final', 1, 1)
        assert bot.send_message.await_count == calls + 1
    finally:
        await gateway.shutdown()
        storage.close()


@pytest.mark.parametrize('stop', [False, True])
async def test_flood_wait_during_move_serializes_and_stop_survives_restart(tmp_path, monkeypatch, stop):
    monkeypatch.setenv('PTB_TIMEDELTA', '1')
    storage, bot, channel = moving_setup(tmp_path)
    await channel.deliver('10', 'Working', 'turn:1', 1, 1)
    owner(storage)
    bot.delete_message.side_effect = [RetryAfter(5), True]
    entered, release = asyncio.Event(), asyncio.Event()
    async def sleep(delay):
        entered.set()
        await release.wait()
    monkeypatch.setattr('eivoh.channels.delivery.asyncio.sleep', sleep)
    job = asyncio.create_task(channel.deliver('10', 'More progress', 'turn:1', 1, 1))
    await entered.wait()
    assert channel.locks['10'].locked()
    assert bot.send_message.await_count == 1
    if stop:
        command = owner(storage, '12')
        storage.stop(command['id'], 1)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        storage.close()
        storage = Storage(tmp_path / 'delivery.db')
        channel.bind(storage)
        storage.recover_deliveries()
        await channel.deliver('10', 'More progress', 'turn:1', 1, 1)
        assert bot.send_message.await_count == 1 and bot.delete_message.await_count == 1
        assert all(row['cancelled'] for row in storage.delivery_parts('telegram', '10', 'turn:1'))
    else:
        release.set()
        await job
        assert bot.delete_message.await_count == 2 and bot.send_message.await_count == 2
        assert channel.delivery.settled('10', 'turn:1')
    storage.close()


async def test_owner_arriving_during_edit_flood_wait_moves_before_retry(tmp_path, monkeypatch):
    monkeypatch.setenv('PTB_TIMEDELTA', '1')
    storage, bot, channel = moving_setup(tmp_path)
    await channel.deliver('10', 'Working', 'turn:1', 1, 1)
    bot.edit_message_text.side_effect = RetryAfter(5)
    async def sleep(delay):
        owner(storage)
        channel.blocked_until['10'] = 0
    monkeypatch.setattr('eivoh.channels.delivery.asyncio.sleep', sleep)
    await channel.deliver('10', 'More progress', 'turn:1', 1, 1)
    assert bot.edit_message_text.await_count == 1
    assert bot.delete_message.await_count == 1 and bot.send_message.await_count == 2
    storage.close()


async def test_gateway_stop_cancels_moving_final_without_resurrection(tmp_path, monkeypatch):
    monkeypatch.setenv('PTB_TIMEDELTA', '1')
    storage, bot, channel = moving_setup(tmp_path)
    await channel.deliver('10', 'Working', 'turn:1', 1, 1)
    owner(storage)
    storage.save_output(1, 'Final', [])
    storage.finish(1, 'completed')
    bot.delete_message.side_effect = RetryAfter(5)
    entered, release = asyncio.Event(), asyncio.Event()
    async def sleep(delay):
        entered.set()
        await release.wait()
    monkeypatch.setattr('eivoh.channels.delivery.asyncio.sleep', sleep)
    gateway = Gateway(storage, HeldExecutor(), {'telegram': channel})
    try:
        gateway._schedule_output(storage.inbound(1), 1, 1)
        await entered.wait()
        await asyncio.wait_for(gateway.receive(Inbound('telegram', '10', '1', '12', '/stop')), 1)
        assert storage.output(1)['cancelled'] and not gateway.output_jobs
        assert bot.send_message.await_count == 1
    finally:
        await gateway.shutdown()
        storage.close()
    storage = Storage(tmp_path / 'delivery.db')
    channel.bind(storage)
    storage.recover_deliveries()
    await channel.deliver_files('10', [], 'turn:1:final', 1, 1)
    assert bot.send_message.await_count == 1 and bot.delete_message.await_count == 1
    assert not storage.pending_outputs()
    storage.close()


async def test_unknown_moved_preview_with_distinct_final_allows_fresh_final(tmp_path):
    storage, bot, channel = moving_setup(tmp_path)
    await channel.deliver('10', 'Working', 'turn:1', 1, 1)
    owner(storage)
    bot.send_message.side_effect = TimedOut('uncertain moved preview')
    await channel.deliver('10', 'More progress', 'turn:1', 1, 1)
    bot.send_message.side_effect = lambda **kwargs: SimpleNamespace(message_id=30)
    channel.prepare_final('10', 'Distinct final', 1, 1)
    await channel.deliver_files('10', [], 'turn:1:final', 1, 1)
    assert bot.send_message.await_count == 3
    assert bot.send_message.call_args.kwargs['text'] == 'Distinct final'
    assert channel.delivery.settled('10', 'turn:1:final')
    storage.close()


async def test_unknown_moved_tail_covers_final_but_known_prefixes_are_deleted(tmp_path):
    storage, bot, channel = moving_setup(tmp_path)
    await channel.deliver('10', 'x' * 8192 + 'Complete answer', 'turn:1', 1, 1)
    owner(storage, '31')
    bot.send_message.side_effect = [SimpleNamespace(message_id=40), SimpleNamespace(message_id=50), TimedOut('tail unknown')]
    await channel.deliver('10', 'y' * 8192 + 'Complete answer', 'turn:1', 1, 1)
    assert bot.send_message.await_count == 6
    channel.prepare_final('10', 'Complete answer', 1, 1)
    await channel.deliver_files('10', [], 'turn:1:final', 1, 1)
    assert bot.send_message.await_count == 6
    assert text_parts(storage, 'turn:1:final')[0]['state'] == 'unknown'
    assert [call.kwargs['message_id'] for call in bot.delete_message.call_args_list] == [10, 20, 30, 40, 50]
    storage.recover_deliveries()
    channel.prepare_final('10', 'Complete answer', 1, 1)
    await channel.deliver_files('10', [], 'turn:1:final', 1, 1)
    assert bot.send_message.await_count == 6 and bot.delete_message.await_count == 5
    storage.close()


async def test_reflow_during_cleanup_does_not_send_text_after_unknown_prefix(tmp_path):
    storage, bot, channel = moving_setup(tmp_path)
    row = storage.prepare_delivery('telegram', '10', 'turn:1:final', '0', {'kind': 'text', 'text': 'Uncertain'}, 1, 1)
    attempt = storage.begin_delivery(row['id'], 'send', json.loads(row['payload']))
    storage.delivery_outcome(row['id'], attempt, 'unknown')
    for part, mid in [('1', '10'), ('2', '20')]:
        row = storage.prepare_delivery('telegram', '10', 'turn:1:final', part, {'kind': 'delete'}, 1, 1)
        with storage.db:
            storage.db.execute('UPDATE delivery SET message_id=? WHERE id=?', (mid, row['id']))
    async def delete(**kwargs):
        # A concurrent desired revision turns the next tombstone into text.
        storage.prepare_delivery('telegram', '10', 'turn:1:final', '2', {'kind': 'text', 'text': 'New tail'}, 1, 1)
        return True
    bot.delete_message.side_effect = delete
    await channel.delivery.flush('10', 'turn:1:final')
    # Both old ids were journaled for cleanup before the concurrent reflow.
    assert bot.delete_message.await_count == 2
    assert not bot.send_message.called and not bot.edit_message_text.called
    assert text_parts(storage, 'turn:1:final')[2]['state'] == 'pending'
    storage.close()


async def test_move_transaction_rolls_back_delete_and_reset_together(tmp_path, monkeypatch):
    storage, bot, channel = moving_setup(tmp_path)
    await channel.deliver('10', 'abc\n' * 2500, 'turn:1', 1, 1)
    owner(storage, '31')
    channel.prepare_reply('10', 'abc\n' * 2500 + 'tail', 'turn:1', 1, 1)
    original = storage.prepare_delivery
    def interrupt(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[3] == 'delete:20':
            raise RuntimeError('crash during move')
        return result
    monkeypatch.setattr(storage, 'prepare_delivery', interrupt)
    with pytest.raises(RuntimeError, match='crash during move'):
        storage.move_delivery('telegram', '10', 'turn:1')
    assert [row['message_id'] for row in text_parts(storage)] == ['10', '20', '30']
    assert len(storage.delivery_parts('telegram', '10', 'turn:1')) == 3
    storage.close()
