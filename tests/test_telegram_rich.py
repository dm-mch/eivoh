"""Synthetic Bot API 10.1+ messages decoded by the installed PTB, without network."""
import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update

from conftest import until
from eivoh.attachments import INPUT_LIMIT, MAX_FILES
from eivoh.channels.base import Inbound
from eivoh.channels.telegram import TelegramChannel
from eivoh.gateway import Gateway
from eivoh.storage import Storage
from test_tasks import HeldExecutor
from test_telegram import make_bot


def update(uid=30, mid=3, **content):
    return Update.de_json(dict(update_id=uid, message=dict(message_id=mid, date=1,
        chat=dict(id=10, type='private'), **{'from': dict(id=1, is_bot=False, first_name='Owner')},
        **content)), None)


def rich(blocks, **kwargs):
    return update(rich_message=dict(blocks=blocks), **kwargs)


def photo(file_id='image', width=100, height=100, file_size=3):
    return dict(file_id=file_id, file_unique_id=file_id, width=width, height=height, file_size=file_size)


def photo_block(**kwargs):
    return dict(type='photo', photo=[photo()], **kwargs)


async def poll(channel, updates, receive=None):
    if receive is None:
        async def receive(message):
            channel.storage.journal(message)
            return True
    channel.bot.get_updates = AsyncMock(side_effect=[updates, asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await channel.poll(receive)


@pytest.fixture
async def telegram_rig(tmp_path):
    storage = Storage(tmp_path / 'state.db')
    bot = make_bot()
    bot.get_file = AsyncMock()
    channel = TelegramChannel('unused', 1, bot)
    executor = HeldExecutor()
    gateway = Gateway(storage, executor, {'telegram': channel}, edit_interval=0)
    yield channel, storage, bot, gateway, executor
    await gateway.shutdown()
    storage.close()


async def test_rich_photo_admission_reply_preparation_and_redelivery(telegram_rig, monkeypatch):
    channel, storage, bot, _, _ = telegram_rig
    storage.journal(Inbound('telegram', '10', '1', '1', 'original task'))
    new = storage.journal(Inbound('telegram', '10', '1', '2', '/new second'))
    storage.new_task(new['id'])
    message = rich([dict(type='paragraph', text='Before the picture'),
        dict(type='photo', photo=[photo('large', 200, 200), photo('small', 10, 10)],
             caption=dict(text='Caption under it', credit='Photo credit'))],
        reply_to_message=dict(message_id=1, date=1, chat=dict(id=10, type='private'), text='original task'))
    assert message.message.text is None and not message.message.photo
    assert isinstance(message.message.api_kwargs['rich_message'], dict)
    await poll(channel, [message, message])
    row = storage.inbound(3)
    assert row['reply_to'] == '1' and row['preparation'] == 'pending'
    assert storage.turn_for(3)['task_id'] == 1 and storage.active_task(1)['id'] == 2
    assert storage.db.execute('SELECT COUNT(*) FROM inbound').fetchone()[0] == 3
    assert storage.saw_update('telegram', 30) and storage.cursor('telegram') == 31
    bot.get_file.assert_not_awaited()
    assert json.loads(row['attachments']) == [dict(file_id='large', kind='photo', name=None,
        size=3, mime_type=None, album=None)]

    async def get_file(file_id):
        assert file_id == 'large' and storage.turn_for(3)['task_id'] == 1
        assert storage.saw_update('telegram', 30)
        return SimpleNamespace(file_path='https://example.test/photo', file_size=3)
    bot.get_file.side_effect = get_file
    async def download(url, path):
        path.write_bytes(b'jpg')
    monkeypatch.setattr('eivoh.channels.telegram.download_file', download)
    text, items = await channel.prepare(row)
    assert text.startswith('Before the picture\n[image 1]\nCaption under it\nPhoto credit\n\nAttached files')
    assert Path(items[0]['path']).read_bytes() == b'jpg'
    storage.prepared(3, text, items)
    await poll(channel, [message])
    assert storage.inbound(3)['prepared_text'] == text and bot.get_file.await_count == 1
    assert storage.db.execute('SELECT COUNT(*) FROM inbound').fetchone()[0] == 3


async def test_rich_text_only_document_order_and_unknown_types(telegram_rig):
    channel, storage, bot, _, _ = telegram_rig
    await poll(channel, [rich([
        dict(type='heading', size=1, text='Heading'),
        dict(type='paragraph', text=['plain ', dict(type='bold', text=[dict(type='italic', text='nested')]),
            ' ', dict(type='custom_emoji', alternative_text='🙂', custom_emoji_id='1'),
            ' ', dict(type='mathematical_expression', expression='x^2'),
            ' ', dict(type='url', text='link', url='https://example.test'),
            ' ', dict(type='url', text='https://same.test', url='https://same.test'),
            ' ', dict(type='button', button=dict(text='Button', callback_data='hidden')),
            ' ', dict(type='future_text', text=['future ', dict(type='code', text='visible')]),
            dict(type='future_no_text', secret='ignored'), None, 123]),
        dict(type='pre', text='a\n  b', language='python'),
        dict(type='footer', text='Footer'),
        dict(type='blockquote', blocks=[dict(type='paragraph', text='Quoted')], credit='Author'),
        dict(type='expandable_blockquote', text='Expandable', credit='Source'),
        dict(type='pullquote', text='Pull quote', credit='Credit'),
        dict(type='list', items=[dict(label='a)', blocks=[dict(type='paragraph', text='First')]),
                                dict(label='•', blocks=[dict(type='paragraph', text='Second')])]),
        dict(type='details', summary='Summary', blocks=[dict(type='paragraph', text='Inside')]),
        dict(type='table', cells=[[dict(text='A'), dict(text='B')],
                                 [dict(text='C'), dict(text='D')]], caption='Table caption'),
        dict(type='mathematical_expression', expression=r'\sum_i x_i'),
        dict(type='buttons', buttons=[dict(text='One', url='https://one.test'),
                                     dict(text='Two', callback_data='hidden')]),
        dict(type='map', caption=dict(text='Map caption', credit='Map credit')),
        dict(type='future_block', text='Block fallback'), dict(type='future_no_text'),
        dict(type='divider'), dict(type='anchor', name='hidden'), None,
    ])])
    row = storage.inbound(1)
    assert row['text'] == ('Heading\nplain nested 🙂 x^2 link (https://example.test) https://same.test Button future visible\n'
        'a\n  b\nFooter\nQuoted\nAuthor\nExpandable\nSource\nPull quote\nCredit\na) First\n• Second\n'
        'Summary\nInside\nA | B\nC | D\nTable caption\n\\sum_i x_i\nOne Two\nMap caption\nMap credit\nBlock fallback')
    assert row['preparation'] == 'ready' and json.loads(row['attachments']) == []
    assert storage.turn_for(1) is not None
    bot.get_file.assert_not_awaited()


@pytest.mark.parametrize('text', ['/stop', '/stop\n' + 'long formatted text ' * 100,
                                  '/q queued text', '/unknown ordinary text', '/path/to/file'])
async def test_rich_command_classification_matches_plain_text(telegram_rig, text):
    channel, storage, _, _, _ = telegram_rig
    await poll(channel, [rich([dict(type='paragraph', text=dict(type='bold', text=text))])])
    plain = storage.journal(Inbound('telegram', '10', '1', '4', text))
    rich_turn, plain_turn = storage.turn_for(1), storage.turn_for(plain['id'])
    assert storage.inbound(1)['text'] == text
    assert (rich_turn is None) == (plain_turn is None)
    if plain_turn:
        assert rich_turn['force_queue'] == plain_turn['force_queue']


async def test_rich_stop_runs_as_command(telegram_rig):
    channel, storage, bot, gateway, executor = telegram_rig
    await gateway.receive(Inbound('telegram', '10', '1', '1', 'running task'))
    await until(lambda: executor.calls)
    await poll(channel, [rich([dict(type='paragraph', text=dict(type='bold', text='/stop'))])], gateway.receive)
    assert storage.turn_for(1)['status'] == 'cancelled'
    assert storage.turn_for(2) is None and storage.inbound(2)['state'] == 'done'
    assert len(executor.calls) == 1
    bot.get_file.assert_not_awaited()


async def test_rich_nested_media_order_and_multiple_prepared_files(telegram_rig, monkeypatch):
    channel, storage, bot, gateway, executor = telegram_rig
    message = rich([dict(type='details', summary='/stop is content here', blocks=[
        dict(type='list', items=[dict(label='1.', blocks=[dict(type='blockquote', blocks=[
            dict(type='collage', blocks=[photo_block(caption=dict(text='First caption')),
                dict(type='slideshow', blocks=[dict(type='document', document=dict(file_id='pdf',
                     file_unique_id='pdf', file_name='name.pdf', mime_type='application/pdf', file_size=3),
                     caption=dict(text='Second caption', credit='Second credit'))])],
                caption=dict(text='Collage caption', credit='Collage credit'))], credit='Quote credit')])])])])
    async def download(url, path):
        assert storage.inbound(1)['preparation'] == 'pending'
        path.write_bytes(b'abc')
    bot.get_file.return_value = SimpleNamespace(file_path='https://example.test/file', file_size=3)
    monkeypatch.setattr('eivoh.channels.telegram.download_file', download)
    await poll(channel, [message, message], gateway.receive)
    await until(lambda: storage.inbound(1)['preparation'] == 'ready' and executor.calls)
    row = storage.inbound(1)
    items = json.loads(row['attachments'])
    assert [(item['kind'], item['file_id']) for item in items] == [('photo', 'image'), ('document', 'pdf')]
    assert row['text'] == ('/stop is content here\n1. [image 1]\nFirst caption\n[file 2: name.pdf]\n'
        'Second caption\nSecond credit\nCollage caption\nCollage credit\nQuote credit')
    assert row['prepared_text'].startswith(row['text'])
    assert all(item['path'] in executor.calls[0][1] for item in items)
    assert len(executor.calls) == 1 and bot.get_file.await_count == 2


@pytest.mark.parametrize('kind,ordinary', [('photo', 'photo'), ('document', 'document'), ('voice_note', 'voice'),
                                         ('audio', 'audio'), ('video', 'video'), ('animation', 'animation')])
async def test_rich_media_uses_existing_descriptor_semantics(telegram_rig, kind, ordinary):
    channel, storage, _, _, _ = telegram_rig
    media = dict(file_id='file', file_unique_id='file', file_size=3, file_name='name.bin',
                 mime_type='application/octet-stream', width=100, height=100, duration=1)
    if kind == 'photo':
        media = [photo()]
    elif kind == 'voice_note':
        media = {key: media[key] for key in ('file_id', 'file_unique_id', 'file_size', 'mime_type', 'duration')}
    await poll(channel, [rich([dict(type=kind, **{kind: media})])])
    assert json.loads(storage.inbound(1)['attachments']) == channel._media(update(**{ordinary: media}).message)


@pytest.mark.parametrize('blocks,reason', [
    ([photo_block()] * (MAX_FILES + 1), 'Too many input files'),
    ([dict(type='photo', photo=[photo(file_size=INPUT_LIMIT + 1)])], '20 MB'),
    ([dict(type='video', video=dict(file_id='video'))], 'media type is not supported'),
    ([dict(type='animation', animation=dict(file_id='animation'))], 'media type is not supported'),
])
async def test_rich_limits_and_unsupported_media_fail_visibly(telegram_rig, blocks, reason):
    channel, storage, bot, gateway, executor = telegram_rig
    message = rich(blocks)
    await poll(channel, [message, message], gateway.receive)
    await until(lambda: storage.turn_for(1)['status'] == 'failed' and bot.send_message.called)
    assert storage.inbound(1)['preparation'] == 'failed'
    assert reason in storage.inbound(1)['prepared_text']
    assert any(reason in call.kwargs['text'] for call in bot.send_message.call_args_list)
    assert storage.db.execute('SELECT COUNT(*) FROM inbound').fetchone()[0] == 1
    assert not executor.calls
    bot.get_file.assert_not_awaited()


@pytest.mark.parametrize('field,value', [
    ('location', dict(latitude=12.3456, longitude=65.4321)),
    ('contact', dict(phone_number='do-not-log-phone', first_name='do-not-log-name')),
    ('poll', dict(id='do-not-log-id', question='do-not-log-question', options=[], total_voter_count=0,
                  is_closed=False, is_anonymous=True, type='regular', allows_multiple_answers=False,
                  allows_revoting=False, members_only=False)),
    ('new_api_content', dict(text='do-not-log-new-content')),
    ('web_app_data', dict(data='do-not-log-data', button_text='do-not-log-button')),
])
async def test_unsupported_content_is_durable_visible_and_logs_only_names(telegram_rig, caplog, field, value):
    channel, storage, bot, gateway, executor = telegram_rig
    message = update(**{field: value})
    await poll(channel, [message, message], gateway.receive)
    await until(lambda: storage.turn_for(1)['status'] == 'failed' and bot.send_message.called)
    assert storage.saw_update('telegram', 30)
    assert storage.db.execute('SELECT COUNT(*) FROM inbound').fetchone()[0] == 1
    assert field in storage.inbound(1)['prepared_text']
    assert any(field in call.kwargs['text'] and 'not supported' in call.kwargs['text']
               for call in bot.send_message.call_args_list)
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1 and 'update 30' in warnings[0].message and field in warnings[0].message
    assert ('api_kwargs keys=' in warnings[0].message)
    assert 'do-not-log' not in caplog.text and '12.3456' not in caplog.text and '65.4321' not in caplog.text
    assert not executor.calls
    bot.get_file.assert_not_awaited()


@pytest.mark.parametrize('field,value', [
    ('pinned_message', dict(message_id=1, date=1, chat=dict(id=10, type='private'), text='do-not-log-pinned')),
    ('message_auto_delete_timer_changed', dict(message_auto_delete_time=60)),
    ('chat_background_set', dict(type=dict(type='fill', fill=dict(type='solid', color=0), dark_theme_dimming=0))),
    ('write_access_allowed', dict(from_request=True)),
    ('video_chat_started', {}),
    ('successful_payment', dict(currency='XTR', total_amount=1, invoice_payload='do-not-log-payload',
        telegram_payment_charge_id='do-not-log-charge', provider_payment_charge_id='do-not-log-provider')),
    ('gift', dict(gift=dict(id='gift', star_count=1, sticker=dict(file_id='sticker', file_unique_id='sticker',
        width=10, height=10, is_animated=False, is_video=False, type='regular')), text='do-not-log-gift')),
])
async def test_service_messages_stay_silent_but_warn_without_content(telegram_rig, caplog, field, value):
    channel, storage, bot, gateway, executor = telegram_rig
    await poll(channel, [update(**{field: value})], gateway.receive)
    assert storage.saw_update('telegram', 30) and storage.cursor('telegram') == 31
    assert storage.db.execute('SELECT COUNT(*) FROM inbound').fetchone()[0] == 0
    bot.send_message.assert_not_awaited()
    bot.get_file.assert_not_awaited()
    assert not executor.calls and not gateway.media_jobs
    assert len(caplog.records) == 1 and caplog.records[0].levelno == logging.WARNING
    assert 'update 30' in caplog.text and field in caplog.text and 'do-not-log' not in caplog.text


async def test_future_library_rich_message_object(telegram_rig):
    channel, storage, _, _, _ = telegram_rig
    # Simulate a future PTB attribute; to_dict on a TelegramObject normalizes children.
    message = rich([])
    message = SimpleNamespace(update_id=30, effective_user=message.effective_user,
        effective_chat=message.effective_chat, effective_message=SimpleNamespace(message_id=3,
        text=None, rich_message=SimpleNamespace(to_dict=lambda: dict(blocks=[
            dict(type='paragraph', text='Future object'), photo_block()]))))
    await poll(channel, [message])
    assert storage.inbound(1)['text'] == 'Future object\n[image 1]'
    assert json.loads(storage.inbound(1)['attachments'])[0]['file_id'] == 'image'


async def test_service_field_does_not_hide_unknown_content(telegram_rig, caplog):
    channel, storage, bot, gateway, _ = telegram_rig
    message = update(video_chat_started={}, new_api_content=dict(text='do-not-log-content'))
    await poll(channel, [message], gateway.receive)
    await until(lambda: storage.turn_for(1)['status'] == 'failed' and bot.send_message.called)
    assert 'new_api_content' in storage.inbound(1)['prepared_text']
    assert 'video_chat_started' not in storage.inbound(1)['prepared_text']
    assert 'video_chat_started' in caplog.text and 'new_api_content' in caplog.text
    assert 'do-not-log-content' not in caplog.text


async def test_owner_non_private_drop_warns_with_names_only(telegram_rig, caplog):
    channel, storage, bot, gateway, _ = telegram_rig
    message = Update.de_json(dict(update_id=30, message=dict(message_id=3, date=1,
        chat=dict(id=-10, type='group', title='do-not-log-chat'),
        **{'from': dict(id=1, is_bot=False, first_name='do-not-log-name')},
        text='do-not-log-message', new_api_content=dict(text='do-not-log-content'))), None)
    await poll(channel, [message], gateway.receive)
    assert storage.saw_update('telegram', 30)
    assert storage.db.execute('SELECT COUNT(*) FROM inbound').fetchone()[0] == 0
    bot.send_message.assert_not_awaited()
    assert len(caplog.records) == 1 and caplog.records[0].levelno == logging.WARNING
    assert 'update 30' in caplog.text and 'text' in caplog.text and 'new_api_content' in caplog.text
    assert 'do-not-log' not in caplog.text


@pytest.mark.parametrize('block', [
    dict(type='list', items=1),
    dict(type='table', cells=1),
    dict(type='buttons', buttons=1),
    dict(type='photo', photo=[photo(width=None)]),
    dict(type='document', document=dict(file_id='file', file_name=123)),
])
async def test_malformed_rich_types_fail_durably_and_poll_continues(telegram_rig, caplog, block):
    channel, storage, bot, gateway, executor = telegram_rig
    message = rich([dict(type='paragraph', text='do-not-log-content'), block])
    await poll(channel, [message, message, update(uid=31, mid=4, text='next message')], gateway.receive)
    await until(lambda: storage.turn_for(1)['status'] == 'failed' and bot.send_message.called)
    assert storage.inbound(1)['preparation'] == 'failed'
    assert 'not supported' in storage.inbound(1)['prepared_text']
    assert storage.saw_update('telegram', 30) and storage.saw_update('telegram', 31)
    assert storage.cursor('telegram') == 32 and storage.inbound(2)['text'] == 'next message'
    assert storage.db.execute('SELECT COUNT(*) FROM inbound').fetchone()[0] == 2
    assert 'update 30' in caplog.text and 'rich_message' in caplog.text
    assert 'do-not-log-content' not in caplog.text
    bot.get_file.assert_not_awaited()


@pytest.mark.parametrize('shape', ['blocks', 'text', 'wide', 'nodes'])
async def test_rich_traversal_limits_fail_durably(telegram_rig, caplog, shape):
    channel, storage, bot, gateway, executor = telegram_rig
    block = dict(type='paragraph', text='do-not-log-deep')
    if shape == 'blocks':
        for _ in range(400):
            block = dict(type='details', summary='do-not-log-summary', blocks=[block])
    elif shape == 'text':
        text = 'do-not-log-deep'
        for _ in range(1200):
            text = dict(type='bold', text=text)
        block = dict(type='paragraph', text=text)
    if shape == 'nodes':
        block = dict(type='paragraph', text=['do-not-log-part'] * 270000)
    blocks = [block] * 5000 if shape == 'wide' else [block]
    await poll(channel, [rich(blocks)], gateway.receive)
    await until(lambda: storage.inbound(1)['preparation'] == 'failed' and bot.send_message.called)
    assert storage.turn_for(1)['status'] == 'failed' and not executor.calls
    assert storage.saw_update('telegram', 30) and storage.cursor('telegram') == 31
    assert 'update 30' in caplog.text and 'rich_message' in caplog.text and 'do-not-log' not in caplog.text
    bot.get_file.assert_not_awaited()


@pytest.mark.parametrize('broken', [[], [dict(width=100, height=100)]])
async def test_broken_rich_photo_never_executes_stop(telegram_rig, broken):
    channel, storage, bot, gateway, executor = telegram_rig
    await gateway.receive(Inbound('telegram', '10', '1', '1', 'running task'))
    await until(lambda: executor.calls)
    message = rich([dict(type='paragraph', text='/stop'), dict(type='photo', photo=broken)])
    await poll(channel, [message], gateway.receive)
    assert storage.turn_for(1)['status'] == 'running'
    row = storage.inbound(2)
    assert row['text'].startswith('/stop') and json.loads(row['attachments'])[0]['kind'] == 'unsupported'
    assert storage.turn_for(2) is not None
    await until(lambda: storage.inbound(2)['preparation'] == 'failed')
    assert 'not supported' in storage.inbound(2)['prepared_text']
    executor.releases[1].set()
    await until(lambda: storage.turn_for(2)['status'] == 'failed' and
        any('not supported' in call.kwargs.get('text', '') for call in bot.send_message.call_args_list))
    assert storage.turn_for(1)['status'] == 'completed' and len(executor.calls) == 1
    bot.get_file.assert_not_awaited()


@pytest.mark.parametrize('kind', ['voice', 'audio'])
async def test_unbound_voice_refused_admission_does_not_acknowledge(kind):
    bot = make_bot()
    channel = TelegramChannel('unused', 1, bot)
    channel._transcribe = AsyncMock(return_value='spoken request')
    message = update(**{kind: dict(file_id='voice', file_unique_id='voice', duration=1)})
    bot.get_updates = AsyncMock(side_effect=[[message], asyncio.CancelledError()])
    receive = AsyncMock(return_value=False)
    await channel.poll(receive)
    assert bot.get_updates.await_count == 1
    assert receive.call_args.args[0].update_id == 30


@pytest.mark.parametrize('error', [TypeError, RuntimeError, RecursionError])
async def test_rich_unexpected_parse_exception_is_durable(telegram_rig, caplog, error):
    channel, storage, bot, gateway, executor = telegram_rig
    def to_dict():
        raise error('do-not-log-exception')
    real = update()
    message = SimpleNamespace(update_id=30, effective_user=real.effective_user, effective_chat=real.effective_chat,
        effective_message=SimpleNamespace(message_id=3, text=None, rich_message=SimpleNamespace(to_dict=to_dict)))
    await poll(channel, [message], gateway.receive)
    await until(lambda: storage.inbound(1)['preparation'] == 'failed' and bot.send_message.called)
    assert storage.saw_update('telegram', 30) and not executor.calls
    assert 'update 30' in caplog.text and 'rich_message' in caplog.text
    assert 'do-not-log' not in caplog.text


@pytest.mark.parametrize('shape', ['text', 'surrogate', 'document', 'photo', 'field_names', 'reply'])
async def test_nonrich_content_errors_fail_durably(telegram_rig, monkeypatch, caplog, shape):
    from telegram import Message
    channel, storage, bot, gateway, executor = telegram_rig
    if shape == 'text':
        message = update(text=123)
    elif shape == 'surrogate':
        message = update(text='\ud800')
    elif shape == 'document':
        message = update(document=dict(file_id='file', file_unique_id='file', file_name=123))
    else:
        message = update(location=dict(latitude=1, longitude=2))
        if shape == 'photo':
            with message.message._unfrozen():
                message.message.photo = 1
        elif shape == 'reply':
            with message.message._unfrozen():
                message.message.reply_to_message = 1
        else:
            def to_dict(self, **kwargs):
                raise TypeError('do-not-log-field-error')
            monkeypatch.setattr(Message, 'to_dict', to_dict)
    await poll(channel, [message], gateway.receive)
    await until(lambda: storage.inbound(1)['preparation'] == 'failed' and bot.send_message.called)
    assert storage.saw_update('telegram', 30) and not executor.calls
    assert 'update 30' in caplog.text and 'do-not-log' not in caplog.text
    bot.get_file.assert_not_awaited()


async def test_malformed_callback_text_does_not_escape_poll(telegram_rig, monkeypatch, caplog):
    from telegram import CallbackQuery
    channel, storage, bot, gateway, _ = telegram_rig
    monkeypatch.setattr(CallbackQuery, 'answer', AsyncMock())
    message = Update.de_json(dict(update_id=30, callback_query=dict(id='callback', chat_instance='chat',
        **{'from': dict(id=1, is_bot=False, first_name='Owner')},
        message=update(text='do-not-log-menu').message.to_dict(), data='\ud800')), None)
    await poll(channel, [message], gateway.receive)
    assert storage.saw_update('telegram', 30) and storage.cursor('telegram') == 31
    assert storage.db.execute('SELECT COUNT(*) FROM inbound').fetchone()[0] == 0
    assert 'update 30' in caplog.text and 'do-not-log' not in caplog.text


async def test_documented_rich_nesting_with_margin_is_accepted(telegram_rig):
    channel, storage, bot, _, _ = telegram_rig
    text = 'Deep visible text'
    for _ in range(16):
        text = [dict(type='bold', text=text)]
    block = dict(type='paragraph', text=text)
    for _ in range(16):
        block = dict(type='details', summary='Summary', blocks=[block])
    await poll(channel, [rich([block])])
    row = storage.inbound(1)
    assert row['preparation'] == 'ready' and row['text'].endswith('Deep visible text')
    assert row['text'].count('Summary') == 16
    assert not json.loads(row['attachments'])
    bot.get_file.assert_not_awaited()


@pytest.mark.parametrize('malformed', [False, True])
@pytest.mark.parametrize('outcome', ['refused', 'storage_error', 'transport_error'])
async def test_content_guard_does_not_swallow_admission_outcome(telegram_rig, malformed, outcome):
    from telegram.error import NetworkError
    channel, storage, bot, _, _ = telegram_rig
    message = rich([dict(type='list', items=1)]) if malformed else update(text='valid text')
    bot.get_updates = AsyncMock(side_effect=[[message], AssertionError('must not acknowledge')])
    receive = AsyncMock(return_value=False)
    if outcome != 'refused':
        error = NetworkError('transport sentinel') if outcome == 'transport_error' else RuntimeError('storage sentinel')
        receive.side_effect = error
        with pytest.raises(type(error)):
            await channel.poll(receive)
    else:
        await channel.poll(receive)
    assert bot.get_updates.await_count == receive.await_count == 1
    assert receive.call_args.args[0].update_id == 30
    assert storage.cursor('telegram') is None and not storage.saw_update('telegram', 30)


@pytest.mark.parametrize('shape', ['table', 'formatted'])
async def test_rich_payload_within_documented_limits_is_accepted(telegram_rig, shape):
    channel, storage, bot, gateway, executor = telegram_rig
    if shape == 'table':
        block = dict(type='table', cells=[[dict(text='x', align='left', valign='top')] * 20] * 300)
    else:
        block = dict(type='paragraph', text=[dict(type='bold', text='x')] * 6000)
    await poll(channel, [rich([block])], gateway.receive)
    assert storage.inbound(1)['attachments'] == '[]' and storage.inbound(1)['text'].count('x') == 6000


async def test_unparseable_rich_reply_keeps_its_task(telegram_rig):
    channel, storage, bot, gateway, executor = telegram_rig
    await poll(channel, [update(uid=30, mid=3, text='first task')], gateway.receive)
    await until(lambda: executor.calls)
    storage.remember_outbound('telegram', '10', ['7'], 1)
    await poll(channel, [update(uid=31, mid=4, text='/new')], gateway.receive)
    reply = dict(message_id=7, date=1, chat=dict(id=10, type='private'))
    await poll(channel, [rich([dict(type='list', items=1)], uid=32, mid=5, reply_to_message=reply)], gateway.receive)
    assert storage.inbound(3)['reply_to'] == '7' and storage.turn_for(3)['task_id'] == 1
