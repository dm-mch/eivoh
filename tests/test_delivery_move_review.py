import itertools
import json
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, TimedOut

from eivoh.channels.base import Inbound
from eivoh.storage import Storage
from test_delivery_moves import moving_setup, owner, text_parts


def running_pair(storage):
    storage.claim(1, 3)
    command = storage.journal(Inbound('telegram', '10', '1', '2', '/new'))
    task = storage.new_task(command['id'])
    message = storage.journal(Inbound('telegram', '10', '1', '3', 'second task'))
    turn, _ = storage.claim(message['id'], 3)
    return task['id'], turn['id']


async def test_running_previews_alternate_without_relocation(tmp_path):
    storage, bot, channel = moving_setup(tmp_path)
    task, turn = running_pair(storage)
    for update in range(25):
        await channel.deliver('10', f'A {update}', 'turn:1', 1, 1)
        await channel.deliver('10', f'B {update}', f'turn:{turn}', task, turn)
    assert bot.send_message.await_count == 2
    assert bot.delete_message.await_count == 0
    assert bot.edit_message_text.await_count == 48
    storage.close()


async def test_final_moves_below_live_preview_then_preview_moves_once(tmp_path):
    storage, bot, channel = moving_setup(tmp_path)
    task, turn = running_pair(storage)
    await channel.deliver('10', 'A', 'turn:1', 1, 1)
    await channel.deliver('10', 'B', f'turn:{turn}', task, turn)
    channel.prepare_final('10', 'A', 1, 1)
    await channel.deliver_files('10', [], 'turn:1:final', 1, 1)
    assert text_parts(storage, 'turn:1:final')[0]['message_id'] == '30'
    for update in range(25):
        await channel.deliver('10', f'B {update}', f'turn:{turn}', task, turn)
        channel.prepare_final('10', 'A', 1, 1)
        await channel.deliver_files('10', [], 'turn:1:final', 1, 1)
    assert bot.send_message.await_count == 4
    assert bot.delete_message.await_count == 2
    assert text_parts(storage, f'turn:{turn}')[0]['message_id'] == '40'
    storage.close()


async def test_refused_move_delete_retains_id_and_settles_without_duplicate(tmp_path):
    storage, bot, channel = moving_setup(tmp_path)
    await channel.deliver('10', 'Complete answer', 'turn:1', 1, 1)
    owner(storage)
    bot.delete_message.side_effect = BadRequest("Message can't be deleted")
    channel.prepare_final('10', 'Complete answer', 1, 1)
    await channel.deliver_files('10', [], 'turn:1:final', 1, 1)
    assert bot.send_message.await_count == 1
    assert bot.edit_message_text.call_args.kwargs['message_id'] == 10
    assert channel.delivery.settled('10', 'turn:1:final')
    assert text_parts(storage, 'turn:1:final')[0]['message_id'] == '10'
    storage.close()


@pytest.mark.parametrize('boundary', ['failed', 'retained', 'edited'])
async def test_refused_preview_delete_recovers_and_final_keeps_retained_id(tmp_path, boundary):
    storage, bot, channel = moving_setup(tmp_path)
    await channel.deliver('10', 'Working', 'turn:1', 1, 1)
    owner(storage)
    channel.prepare_reply('10', 'Updated', 'turn:1', 1, 1)
    assert storage.move_delivery('telegram', '10', 'turn:1')
    cleanup = storage.delivery_parts('telegram', '10', 'turn:1')[-1]
    bot.delete_message.side_effect = BadRequest("Message can't be deleted")
    await channel.delivery._part(cleanup)
    if boundary != 'failed':
        storage.retain_delivery(cleanup['id'])
    if boundary == 'edited':
        await channel.delivery.flush('10', 'turn:1')
    storage.close()
    storage = Storage(tmp_path / 'delivery.db')
    channel.bind(storage)
    storage.recover_deliveries()
    # Finalization may inherit the failed cleanup before the preview resumes.
    channel.prepare_final('10', 'Complete answer', 1, 1)
    await channel.deliver_files('10', [], 'turn:1:final', 1, 1)
    assert bot.send_message.await_count == 1
    assert bot.edit_message_text.call_args.kwargs['message_id'] == 10
    assert channel.delivery.settled('10', 'turn:1:final')
    for _ in range(3):
        channel.prepare_final('10', 'Complete answer', 1, 1)
        await channel.deliver_files('10', [], 'turn:1:final', 1, 1)
    assert bot.send_message.await_count == 1
    assert bot.delete_message.await_count <= 2
    storage.close()


async def test_refused_obsolete_move_delete_retires_text_without_repeating_answer(tmp_path):
    storage, bot, channel = moving_setup(tmp_path)
    await channel.deliver('10', 'x' * 4096 + 'old tail', 'turn:1', 1, 1)
    owner(storage, '21')
    bot.delete_message.side_effect = BadRequest("Message can't be deleted")
    channel.prepare_final('10', 'Complete answer', 1, 1)
    await channel.deliver_files('10', [], 'turn:1:final', 1, 1)
    assert bot.send_message.await_count == 2  # Only the original preview.
    edits = {call.kwargs['message_id']: call.kwargs['text'] for call in bot.edit_message_text.call_args_list}
    assert edits == {10: 'Complete answer', 20: '[Earlier preview superseded.]'}
    assert channel.delivery.settled('10', 'turn:1:final')
    storage.close()


async def test_refused_cleanup_does_not_clear_unknown_final(tmp_path):
    storage, bot, channel = moving_setup(tmp_path)
    await channel.deliver('10', 'x' * 4096, 'turn:1', 1, 1)
    bot.send_message.side_effect = TimedOut('tail may contain final')
    await channel.deliver('10', 'x' * 4096 + 'Complete answer', 'turn:1', 1, 1)
    bot.delete_message.side_effect = BadRequest("Message can't be deleted")
    channel.prepare_final('10', 'Complete answer', 1, 1)
    await channel.deliver_files('10', [], 'turn:1:final', 1, 1)
    assert bot.send_message.await_count == 2
    assert text_parts(storage, 'turn:1:final')[0]['state'] == 'unknown'
    assert channel.delivery.problems('10', 'turn:1:final') == (1, 0, 0)
    assert bot.edit_message_text.call_args.kwargs == {'chat_id': '10', 'message_id': 10, 'text': '[Earlier preview superseded.]'}
    storage.close()


async def test_missing_cleanup_marker_is_settled_without_a_fresh_send(tmp_path):
    storage, bot, channel = moving_setup(tmp_path)
    await channel.deliver('10', 'x' * 4096 + 'old tail', 'turn:1', 1, 1)
    owner(storage, '21')
    bot.delete_message.side_effect = [True, BadRequest("Message can't be deleted")]
    bot.edit_message_text.side_effect = BadRequest('Message to edit not found')
    async def send(**kwargs):
        if kwargs['text'] == '[Earlier preview superseded.]':
            raise TimedOut('unnecessary marker send')
        return SimpleNamespace(message_id=30)
    bot.send_message.side_effect = send
    channel.prepare_final('10', 'Complete answer', 1, 1)
    await channel.deliver_files('10', [], 'turn:1:final', 1, 1)
    assert channel.delivery.settled('10', 'turn:1:final')
    assert bot.send_message.await_count == 3
    assert bot.send_message.call_args.kwargs['text'] == 'Complete answer'
    storage.close()


@pytest.mark.parametrize('kind', ['text', 'document', 'notice'])
async def test_running_preview_exemption_is_only_for_progress_text(tmp_path, kind):
    storage, bot, channel = moving_setup(tmp_path)
    task, turn = running_pair(storage)
    await channel.deliver('10', 'A', 'turn:1', 1, 1)
    logical = f'turn:{turn}' if kind != 'notice' else 'notice'
    row = storage.prepare_delivery('telegram', '10', logical, 'file:0' if kind == 'document' else '0',
        {'kind': 'document' if kind == 'document' else 'text', 'text': 'B'}, task, turn)
    attempt = storage.begin_delivery(row['id'], 'send', json.loads(row['payload']))
    storage.delivery_outcome(row['id'], attempt, 'sent', '20')
    assert storage.delivery_displaced('telegram', '10', 'turn:1') == (kind != 'text')
    storage.finish(turn, 'completed')
    assert storage.delivery_displaced('telegram', '10', 'turn:1')
    storage.close()


async def test_retiring_tail_cannot_mask_displaced_final(tmp_path):
    storage, bot, channel = moving_setup(tmp_path)
    ids = iter([10, 20, 30])
    async def send(**kwargs):
        mid = next(ids)
        if mid == 20:
            owner(storage, '15')
        return SimpleNamespace(message_id=mid)
    bot.send_message.side_effect = send
    await channel.deliver('10', 'x' * 4096 + 'tail', 'turn:1', 1, 1)
    channel.prepare_final('10', 'Short final', 1, 1)
    await channel.deliver_files('10', [], 'turn:1:final', 1, 1)
    assert text_parts(storage, 'turn:1:final')[0]['message_id'] == '30'
    assert [call.kwargs['message_id'] for call in bot.delete_message.call_args_list] == [10, 20]
    assert channel.delivery.settled('10', 'turn:1:final')
    storage.close()


async def test_one_relocation_per_flush_finishes_multipart_batch(tmp_path):
    storage, bot, channel = moving_setup(tmp_path)
    await channel.deliver('10', 'x' * 4096 + 'tail', 'turn:1', 1, 1)
    owner(storage, '31')
    ids = itertools.count(40, 10)
    async def send(**kwargs):
        mid = next(ids)
        if mid < 150:
            owner(storage, str(mid + 1))
        return SimpleNamespace(message_id=mid)
    bot.send_message.side_effect = send
    await channel.deliver('10', 'y' * 4096 + 'tail', 'turn:1', 1, 1)
    assert bot.send_message.await_count == 4
    assert bot.delete_message.await_count == 2
    assert [row['message_id'] for row in text_parts(storage)] == ['40', '50']
    assert channel.delivery.settled('10', 'turn:1')
    storage.close()


@pytest.mark.parametrize('final', [False, True])
async def test_displacement_query_seeks_numeric_indexes(tmp_path, final):
    storage, bot, channel = moving_setup(tmp_path)
    await channel.deliver('10', 'Working', 'turn:1', 1, 1)
    logical = 'turn:1'
    if final:
        channel.prepare_final('10', 'Final', 1, 1)
        logical += ':final'
    queries = []
    storage.db.set_trace_callback(queries.append)
    assert not storage.delivery_displaced('telegram', '10', logical)
    storage.db.set_trace_callback(None)
    query = next(query for query in queries if 'FROM inbound' in query and 'FROM delivery' in query)
    plan = [row['detail'] for row in storage.db.execute('EXPLAIN QUERY PLAN ' + query)]
    for table in ('inbound', 'delivery'):
        assert any(f'USING INDEX {table}_message_number' in detail and '<expr>>?' in detail for detail in plan), plan
    assert all('logical_id=?' in detail or '<expr>>?' in detail for detail in plan if 'SEARCH delivery ' in detail), plan
    storage.close()


async def test_single_part_update_checks_displacement_once(tmp_path, monkeypatch):
    storage, bot, channel = moving_setup(tmp_path)
    await channel.deliver('10', 'Working', 'turn:1', 1, 1)
    checks = []
    original = storage.delivery_displaced
    def check(*args):
        checks.append(args)
        return original(*args)
    monkeypatch.setattr(storage, 'delivery_displaced', check)
    await channel.deliver('10', 'Still working', 'turn:1', 1, 1)
    assert len(checks) == 1
    storage.close()


@pytest.mark.parametrize('retained', [False, True])
@pytest.mark.parametrize('final', [False, True])
@pytest.mark.parametrize('restart', [False, True])
async def test_refused_shrink_retires_tail_once(tmp_path, retained, final, restart):
    storage, bot, channel = moving_setup(tmp_path)
    visible = {}
    async def send(**kwargs):
        mid = 10 * bot.send_message.await_count
        visible[mid] = kwargs['text']
        return SimpleNamespace(message_id=mid)
    async def edit(**kwargs):
        visible[kwargs['message_id']] = kwargs['text']
        return True
    bot.send_message.side_effect = send
    bot.edit_message_text.side_effect = edit
    await channel.deliver('10', 'x' * 4096 + 'Complete answer', 'turn:1', 1, 1)
    bot.delete_message.side_effect = BadRequest("Message can't be deleted")
    if retained:
        owner(storage, '21')
        await channel.deliver('10', 'y' * 4096 + 'Complete answer', 'turn:1', 1, 1)
        assert channel.delivery.settled('10', 'turn:1')
    if restart:
        storage.close()
        storage = Storage(tmp_path / 'delivery.db')
        channel.bind(storage)
        storage.recover_deliveries()
    logical = 'turn:1:final' if final else 'turn:1'
    if final:
        channel.prepare_final('10', 'Complete answer', 1, 1)
    else:
        channel.prepare_reply('10', 'Complete answer', logical, 1, 1)
    if restart:
        storage.close()
        storage = Storage(tmp_path / 'delivery.db')
        channel.bind(storage)
        storage.recover_deliveries()
    await channel.delivery.flush('10', logical)
    assert list(visible.values()).count('Complete answer') == 1
    assert visible == {10: 'Complete answer', 20: '[Earlier preview superseded.]'}
    assert channel.delivery.settled('10', logical)
    assert bot.send_message.await_count == 2
    requests = (bot.send_message.await_count, bot.edit_message_text.await_count, bot.delete_message.await_count)
    if final:
        channel.prepare_final('10', 'Complete answer', 1, 1)
    else:
        channel.prepare_reply('10', 'Complete answer', logical, 1, 1)
    await channel.delivery.flush('10', logical)
    assert requests == (bot.send_message.await_count, bot.edit_message_text.await_count, bot.delete_message.await_count)
    storage.close()


@pytest.mark.parametrize('boundary', ['pending_delete', 'refused_delete', 'pending_marker', 'sent_marker'])
async def test_retained_shrink_cleanup_transfers_to_final_after_restart(tmp_path, boundary):
    storage, bot, channel = moving_setup(tmp_path)
    await channel.deliver('10', 'x' * 4096 + 'Complete answer', 'turn:1', 1, 1)
    owner(storage, '21')
    bot.delete_message.side_effect = BadRequest("Message can't be deleted")
    await channel.deliver('10', 'y' * 4096 + 'Complete answer', 'turn:1', 1, 1)
    channel.prepare_reply('10', 'Complete answer', 'turn:1', 1, 1)
    cleanup = storage.retire_delivery(text_parts(storage)[1]['id'])
    if boundary != 'pending_delete':
        await channel.delivery._part(cleanup)
    if boundary in {'pending_marker', 'sent_marker'}:
        cleanup = storage.retain_delivery(cleanup['id'])
    if boundary == 'sent_marker':
        await channel.delivery._part(cleanup)
    storage.close()
    storage = Storage(tmp_path / 'delivery.db')
    channel.bind(storage)
    storage.recover_deliveries()
    channel.prepare_final('10', 'Complete answer', 1, 1)
    await channel.deliver_files('10', [], 'turn:1:final', 1, 1)
    assert channel.delivery.settled('10', 'turn:1:final')
    assert bot.send_message.await_count == 2
    edits = {call.kwargs['message_id']: call.kwargs['text'] for call in bot.edit_message_text.call_args_list}
    assert edits == {10: 'Complete answer', 20: '[Earlier preview superseded.]'}
    storage.close()


async def test_failed_legacy_numeric_shrink_delete_recovers_through_cleanup(tmp_path):
    storage, bot, channel = moving_setup(tmp_path)
    await channel.deliver('10', 'x' * 4096 + 'Complete answer', 'turn:1', 1, 1)
    channel.prepare_final('10', 'Complete answer', 1, 1)
    # An older run recorded the physical refusal on the numeric part itself.
    row = text_parts(storage, 'turn:1:final')[1]
    attempt = storage.begin_delivery(row['id'], 'delete', json.loads(row['payload']))
    storage.delivery_outcome(row['id'], attempt, 'failed', detail='Telegram rejected the request')
    storage.close()
    storage = Storage(tmp_path / 'delivery.db')
    channel.bind(storage)
    storage.recover_deliveries()
    await channel.deliver_files('10', [], 'turn:1:final', 1, 1)
    assert channel.delivery.settled('10', 'turn:1:final')
    assert bot.delete_message.await_count == 0 and bot.send_message.await_count == 2
    edits = {call.kwargs['message_id']: call.kwargs['text'] for call in bot.edit_message_text.call_args_list}
    assert edits == {10: 'Complete answer', 20: '[Earlier preview superseded.]'}
    storage.close()
