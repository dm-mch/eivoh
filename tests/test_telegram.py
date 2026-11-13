import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

from eivoh.channels.telegram import TelegramChannel


def make_bot():
    return SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=5)),
                           set_my_commands=AsyncMock(return_value=True),
                           edit_message_text=AsyncMock(return_value=True),
                           delete_message=AsyncMock(return_value=True),
                           send_chat_action=AsyncMock(return_value=True))


async def test_parse_fallback_not_modified_and_ambiguous_send():
    bot = make_bot()
    channel = TelegramChannel('unused', 1, bot)
    bot.send_message.side_effect = [BadRequest("Can't parse entities"), SimpleNamespace(message_id=5)]
    assert await channel.send('10', '**bold**') == '5'
    assert bot.send_message.call_args_list[0].kwargs['parse_mode'] == 'HTML'
    assert bot.send_message.call_args_list[1].kwargs['text'] == 'bold'
    assert 'parse_mode' not in bot.send_message.call_args_list[1].kwargs
    bot.edit_message_text.side_effect = BadRequest('Message is not modified')
    await channel.edit('10', '5', 'bold')
    bot.send_message.reset_mock()
    bot.send_message.side_effect = TimedOut('ambiguous')
    assert await channel.send('10', 'no retry') is None
    assert bot.send_message.call_count == 1


async def test_flood_backoff_serializes_chat(monkeypatch):
    bot = make_bot()
    channel = TelegramChannel('unused', 1, bot)
    monkeypatch.setenv('PTB_TIMEDELTA', '1')
    bot.edit_message_text.side_effect = [RetryAfter(0), True]
    entered, release = asyncio.Event(), asyncio.Event()
    sleeps = []
    async def sleep(delay):
        sleeps.append(delay)
        entered.set()
        await release.wait()
    monkeypatch.setattr('eivoh.channels.telegram.asyncio.sleep', sleep)
    edit = asyncio.create_task(channel.edit('10', '5', 'new'))
    await entered.wait()
    send = asyncio.create_task(channel.send('10', 'waits behind the back-off'))
    await channel.typing('10')  # The sign of life is not queued behind deliveries.
    assert bot.send_chat_action.call_count == 1 and bot.send_message.call_count == 0
    release.set()
    await asyncio.gather(edit, send)
    assert bot.edit_message_text.call_count == 2 and bot.send_message.call_count == 1
    bot.send_chat_action.side_effect = RetryAfter(5)
    await channel.typing('10')  # Best-effort: a refused indicator is dropped.
    assert sleeps[0] > 0


async def test_typing_survives_task_switch_during_edit_flood(tmp_path, monkeypatch, caplog):
    from conftest import until
    from test_tasks import HeldExecutor
    from eivoh.channels.base import Inbound
    from eivoh.gateway import Gateway
    from eivoh.storage import Storage
    monkeypatch.setenv('PTB_TIMEDELTA', '1')
    bot = make_bot()
    channel = TelegramChannel('unused', 1, bot)
    executor = HeldExecutor()
    store = Storage(tmp_path / 'typing.db')
    gateway = Gateway(store, executor, {'telegram': channel}, edit_interval=0, typing_interval=0.01)
    entered, release = asyncio.Event(), asyncio.Event()
    real_sleep = asyncio.sleep
    async def flood_sleep(delay):
        if delay >= 1:
            entered.set()
            await release.wait()
        else:
            await real_sleep(delay)
    monkeypatch.setattr('eivoh.channels.telegram.asyncio.sleep', flood_sleep)
    def message(mid, text):
        return Inbound('telegram', '10', '1', str(mid), text)
    switching = None
    try:
        await gateway.receive(message(1, 'long running task'))
        await until(lambda: bot.send_message.call_count and bot.send_chat_action.call_count)
        bot.edit_message_text.side_effect = [RetryAfter(5), True]
        await gateway.receive(message(2, '/new second task'))
        await asyncio.wait_for(entered.wait(), 1)
        # Switch is durable even though its confirmation waits behind the edit.
        switching = asyncio.create_task(gateway.receive(message(3, '/task 1')))
        await until(lambda: store.active_task(1)['id'] == 1)
        previous = bot.send_chat_action.call_count
        # A refused typing action is dropped, then the next cycle tries again.
        bot.send_chat_action.side_effect = [RetryAfter(5), True, True, True]
        await until(lambda: bot.send_chat_action.call_count >= previous + 2)
        bot.send_chat_action.side_effect = None
        # The confirmation is background delivery; polling can admit the next
        # command while both the edit and confirmation wait on flood control.
        assert switching.done() and store.turn_for(1)['status'] == 'running'
        assert 'Telegram flood control' in caplog.text and 'waiting 5 s' in caplog.text
        release.set()
        await switching
        await gateway.receive(message(4, '/stop'))
        assert not gateway.workers
    finally:
        release.set()
        if switching:
            await switching
        await gateway.shutdown()
        store.close()


async def test_chunk_edit_grows_and_shrinks():
    bot = make_bot()
    counter = 0
    async def send(**kwargs):
        nonlocal counter
        counter += 1
        return SimpleNamespace(message_id=counter)
    bot.send_message.side_effect = send
    channel = TelegramChannel('unused', 1, bot)
    mid = await channel.send('10', 'short')
    await channel.edit('10', mid, '```\n' + 'x' * 9000 + '\n```')
    assert bot.send_message.call_count == 3
    await channel.edit('10', mid, 'short again')
    assert bot.delete_message.call_count == 2


async def test_poll_owner_private_and_ack_after_journal(rig):
    gateway, storage, _, _, _ = rig
    bot = make_bot()
    channel = TelegramChannel('unused', 1, bot)
    def update(uid, owner, chat_type, mid, text):
        return SimpleNamespace(update_id=uid, effective_user=SimpleNamespace(id=owner),
                               effective_chat=SimpleNamespace(id=10, type=chat_type),
                               effective_message=SimpleNamespace(message_id=mid, text=text))
    calls = []
    async def updates(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return [update(100, 2, 'private', 1, 'ignored'),
                    update(101, 1, 'group', 2, 'ignored'),
                    update(102, 1, 'private', 3, 'accepted')]
        assert kwargs['offset'] == 103
        assert storage.db.execute("SELECT text FROM inbound").fetchone()[0] == 'accepted'
        raise asyncio.CancelledError
    bot.get_updates = updates
    # No real Telegram transport is needed to test polling admission/ack.
    async def receive(message):
        storage.journal(message)
    with pytest.raises(asyncio.CancelledError):
        await channel.poll(receive)
    assert len(storage.pending()) == 1


async def test_voice_is_transcribed_echoed_and_journaled(rig):
    from eivoh.channels.telegram import VOICE_PREFIX
    from eivoh.speech import TranscriptionError
    _, storage, _, _, _ = rig
    bot = make_bot()
    bot.get_file = AsyncMock(return_value=SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b'ogg'))))
    transcriber = SimpleNamespace(transcribe=AsyncMock(side_effect=['привет', TranscriptionError('Soniox down'), '']))
    channel = TelegramChannel('unused', 1, bot, transcriber=transcriber)
    def voice(uid, mid):
        return SimpleNamespace(update_id=uid, effective_user=SimpleNamespace(id=1),
                               effective_chat=SimpleNamespace(id=10, type='private'),
                               effective_message=SimpleNamespace(message_id=mid, text=None, audio=None,
                                                                 voice=SimpleNamespace(file_id=f'f{mid}')))
    calls = []
    async def updates(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return [voice(1, 1), voice(2, 2), voice(3, 3)]
        assert kwargs['offset'] == 4
        raise asyncio.CancelledError
    bot.get_updates = updates
    async def receive(message):
        storage.journal(message)
    with pytest.raises(asyncio.CancelledError):
        await channel.poll(receive)
    assert transcriber.transcribe.await_args_list[0].args[0] == b'ogg'
    assert [r['text'] for r in storage.pending()] == [VOICE_PREFIX + 'привет']
    sent = [c.kwargs['text'] for c in bot.send_message.call_args_list]
    assert sent == ['🎤 привет', 'Could not transcribe the voice message: Soniox down',
                    'The voice message transcript is empty.']
    assert all('parse_mode' not in c.kwargs for c in bot.send_message.call_args_list)


async def test_voice_without_transcriber_is_refused(rig):
    _, storage, _, _, _ = rig
    bot = make_bot()
    channel = TelegramChannel('unused', 1, bot)
    msg = SimpleNamespace(message_id=1, text=None, audio=None, voice=SimpleNamespace(file_id='f'))
    assert await channel._transcribe('10', msg.voice) is None
    assert 'not configured' in bot.send_message.call_args.kwargs['text']


@pytest.mark.parametrize('registration_error', [None, NetworkError('offline')])
async def test_commands_registered_once_and_failure_does_not_stop_poll(registration_error, caplog):
    bot = make_bot()
    bot.set_my_commands.side_effect = registration_error
    bot.get_updates = AsyncMock(side_effect=[[], asyncio.CancelledError()])
    channel = TelegramChannel('unused', 1, bot)
    with pytest.raises(asyncio.CancelledError):
        await channel.poll(AsyncMock())
    bot.set_my_commands.assert_awaited_once()
    assert [name for name, _ in bot.set_my_commands.call_args.args[0]] == ['new', 'tasks', 'model', 'stop', 'q', 'close', 'title']
    assert bot.get_updates.await_count == 2
    if registration_error:
        assert 'command registration failed' in caplog.text


async def test_buttons_on_last_chunk_and_label_is_literal():
    from eivoh.channels.formatting import plain_text, utf16_len
    bot = make_bot()
    channel = TelegramChannel('unused', 1, bot)
    buttons = [[('3 · Title', '/task 3')], [('Older ›', '/tasks 2')]]
    text = '#t3 **Title**\n' + '😀' * 6000
    await channel.send('10', text, buttons)
    calls = bot.send_message.call_args_list
    assert len(calls) > 1
    assert all('reply_markup' not in call.kwargs for call in calls[:-1])
    keyboard = calls[-1].kwargs['reply_markup'].inline_keyboard
    assert [[(button.text, button.callback_data) for button in row] for row in keyboard] == buttons
    assert ''.join(plain_text(call.kwargs['text']) for call in calls) == text
    assert all(utf16_len(call.kwargs['text']) <= 4096 for call in calls)


async def test_poll_callbacks_authorization_ack_and_stable_id():
    bot = make_bot()
    received = []
    callbacks = []
    def update(uid, owner=1, kind='private', data='/task 3', failure=False):
        callback = SimpleNamespace(id='press-' + str(uid), data=data,
                                   answer=AsyncMock(side_effect=NetworkError('offline') if failure else None))
        callbacks.append(callback)
        return SimpleNamespace(update_id=uid, effective_user=SimpleNamespace(id=owner),
                               effective_chat=SimpleNamespace(id=10, type=kind),
                               effective_message=SimpleNamespace(message_id=20, text='Task list'),
                               callback_query=callback)
    updates = [update(1, owner=2), update(2, kind='group'), update(3), update(4, failure=True), update(5, data='bad payload')]
    updates.append(updates[2])  # Same callback id remains deduplicable on redelivery.
    bot.get_updates = AsyncMock(side_effect=[updates, asyncio.CancelledError()])
    async def receive(message):
        received.append(message)
    with pytest.raises(asyncio.CancelledError):
        await TelegramChannel('unused', 1, bot).poll(receive)
    assert [msg.message_id for msg in received] == ['cb:press-3', 'cb:press-4', 'cb:press-5', 'cb:press-3']
    assert [msg.text for msg in received] == ['/task 3', '/task 3', 'bad payload', '/task 3']
    assert all(msg.reply_to is None for msg in received)
    assert callbacks[0].answer.await_count == callbacks[1].answer.await_count == 0
    assert all(callback.answer.await_count for callback in callbacks[2:])
    assert bot.get_updates.call_args.kwargs['allowed_updates'] == ['message', 'callback_query']


@pytest.mark.parametrize('voice', [False, True])
async def test_reply_to_forwarded_for_text_and_voice(voice):
    bot = make_bot()
    bot.get_file = AsyncMock(return_value=SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b'ogg'))))
    transcriber = SimpleNamespace(transcribe=AsyncMock(return_value='spoken'))
    message = SimpleNamespace(message_id=9, text=None if voice else 'typed', audio=None,
                              voice=SimpleNamespace(file_id='audio') if voice else None,
                              reply_to_message=SimpleNamespace(message_id=7))
    update = SimpleNamespace(update_id=1, effective_user=SimpleNamespace(id=1),
                             effective_chat=SimpleNamespace(id=10, type='private'), effective_message=message)
    bot.get_updates = AsyncMock(side_effect=[[update], asyncio.CancelledError()])
    receive = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await TelegramChannel('unused', 1, bot, transcriber).poll(receive)
    assert receive.call_args.args[0].reply_to == '7'
