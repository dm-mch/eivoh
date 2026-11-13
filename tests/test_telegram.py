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
    typing = asyncio.create_task(channel.typing('10'))
    assert bot.send_chat_action.call_count == 0
    release.set()
    await asyncio.gather(edit, typing)
    assert bot.edit_message_text.call_count == 2
    assert bot.send_chat_action.call_count == 1
    assert sleeps[0] > 0


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
    assert [name for name, _ in bot.set_my_commands.call_args.args[0]] == ['new', 'model', 'stop']
    assert bot.get_updates.await_count == 2
    if registration_error:
        assert 'command registration failed' in caplog.text
