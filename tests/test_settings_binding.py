"""Settings callbacks remain task-bound without delivery acknowledgements."""
import asyncio
import itertools
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import TimedOut

from eivoh.channels.base import Inbound
from eivoh.channels.telegram import TelegramChannel
from eivoh.gateway import Gateway
from eivoh.storage import Storage
from test_tasks import HeldExecutor
from test_telegram import make_bot


@pytest.mark.parametrize('setting,value,menu', [('effort', 'high', '/effort'), ('model', 'opus', '/model claude')])
async def test_ambiguous_menu_send_never_changes_another_task(tmp_path, setting, value, menu):
    storage = Storage(tmp_path / 'state.db')
    bot = make_bot()
    ids = itertools.count(100)
    normal = lambda **kwargs: SimpleNamespace(message_id=next(ids))
    bot.send_message = AsyncMock(side_effect=normal)
    channel = TelegramChannel('unused', 1, bot)
    gateway = Gateway(storage, HeldExecutor(), {'telegram': channel})

    async def receive(mid, text, reply=None, button=False):
        await gateway.receive(Inbound('telegram', '10', '1', str(mid), text, reply, button=button))
        await asyncio.gather(*(job for jobs in list(gateway.notice_jobs.values()) for job in list(jobs)))

    try:
        await receive(1, '/new First')
        first = storage.delivery_parts('telegram', '10', 'inbound:1:reply')[0]['message_id']
        await receive(2, '/new Second')
        bot.send_message.side_effect = TimedOut('sent, acknowledgement lost')
        await receive(3, menu, first)
        part = storage.delivery_parts('telegram', '10', 'inbound:3:reply')[0]
        assert part['task_id'] == 1 and part['state'] == 'unknown' and part['message_id'] is None
        payload = json.loads(part['payload'])
        callback = next(data for row in payload['buttons'] for label, data in row if label == value)
        assert storage.reply_task(1, '999') is None
        bot.send_message.side_effect = normal
        await receive('cb:select', callback, '999', True)
        assert storage.task(1, 1)[setting] == value
        assert storage.task(1, 2)[setting] is None
        assert storage.active_task(1)['id'] == 2
    finally:
        await gateway.shutdown()
        storage.close()


from conftest import inbound
from test_tasks import held


def callback_data(channel):
    return [data for group in channel.buttons[-1] for _, data in group]


@pytest.mark.parametrize('command', ['/model', '/effort'])
@pytest.mark.parametrize('target', ['2', '999', '0', '-1', 'bad', '9223372036854775808'])
@pytest.mark.parametrize('button', [False, True])
async def test_unknown_foreign_and_invalid_targets_never_fall_back(held, command, target, button):
    gateway, storage, _, channel, _ = held
    await gateway.receive(inbound(1, '/new Here'))
    await gateway.receive(inbound(2, '/new Elsewhere', chat='20'))
    before = [dict(storage.task(1, 1)), dict(storage.task(2, 2))]
    value = 'opus' if command == '/model' else 'high'
    await gateway.receive(inbound('cb:bad' if button else 3, f'{command} #{target} {value}'))
    assert [dict(storage.task(1, 1)), dict(storage.task(2, 2))] == before
    assert storage.active_task(1)['id'] == 1
    assert storage.db.execute('SELECT COUNT(*) FROM task').fetchone()[0] == 2
    assert 'Unknown task' in channel.sent[-1][1] or 'positive task number' in channel.sent[-1][1]


async def test_typed_explicit_forms_and_closed_task_keep_target(held, monkeypatch):
    gateway, storage, _, channel, _ = held
    monkeypatch.setenv('EIVOH_CLAUDE_MODELS', 'opus,' + ','.join(f'choice-{i}' for i in range(1, 10)))
    await gateway.receive(inbound(1, '/new First'))
    first_notice = 'bot:1'
    await gateway.receive(inbound(2, '/new Second'))
    second_notice = 'bot:2'
    for mid, text in enumerate(['/model #1', '/model #1 claude', '/model #1 claude page 2',
                                '/model #1 9', '/model #1 claude opus', '/effort #1', '/effort #1 high'], 3):
        await gateway.receive(inbound(mid, text, reply_to=second_notice))
        assert all(data.split()[1] == '#1' for data in callback_data(channel))
        assert storage.active_task(1)['id'] == 2
    assert storage.task(1, 1)['model'] == 'opus' and storage.task(1, 1)['effort'] == 'high'
    assert storage.task(1, 2)['model'] is storage.task(1, 2)['effort'] is None
    await gateway.receive(inbound(10, '/close 1'))
    await gateway.receive(inbound('cb:closed', '/effort #1 low'))
    await gateway.receive(inbound('cb:closed-model', '/model #1 claude choice-1'))
    assert storage.task(1, 1)['status'] == 'closed'
    assert storage.task(1, 1)['model'] == 'choice-1' and storage.task(1, 1)['effort'] == 'low'
    assert storage.active_task(1)['id'] == 2
    # Unqualified typed commands still use reply routing, then the current task.
    await gateway.receive(inbound(11, '/effort medium', reply_to=first_notice))
    await gateway.receive(inbound(12, '/model opus', reply_to=first_notice))
    await gateway.receive(inbound(13, '/effort high'))
    assert storage.task(1, 1)['effort'] == 'medium' and storage.task(1, 1)['model'] == 'opus'
    assert storage.task(1, 1)['status'] == 'closed' and storage.task(1, 2)['effort'] == 'high'


@pytest.mark.parametrize('command,value', [('/model', 'opus'), ('/effort', 'high')])
async def test_taskless_menu_creates_fresh_task_after_another_becomes_current(held, command, value):
    gateway, storage, _, channel, _ = held
    await gateway.receive(inbound(1, command))
    assert storage.active_task(1) is None
    assert all(data.split()[1] == '#new' for data in callback_data(channel))
    await gateway.receive(inbound(2, '/new Intervening'))
    await gateway.receive(inbound(3, '/model haiku'))
    await gateway.receive(inbound(4, '/effort low'))
    before = dict(storage.task(1, 1))
    if command == '/model':
        await gateway.receive(inbound('cb:executor', '/model #new claude'))
        assert all(data.startswith('/model #new ') for data in callback_data(channel))
        assert storage.active_task(1)['id'] == 1
    callback = f'{command} #new {value}'
    await gateway.receive(inbound('cb:select', callback))
    assert dict(storage.task(1, 1)) == before
    task = storage.active_task(1)
    assert task['id'] == 2
    assert (task['model'], task['effort']) == (('opus', None) if command == '/model' else (None, 'high'))
    assert all(data.split()[1] == '#2' for data in callback_data(channel))
    await gateway.receive(inbound('cb:select', callback))
    assert storage.db.execute('SELECT COUNT(*) FROM task').fetchone()[0] == 2
    await gateway.receive(inbound(5, callback))  # Same syntax works as typed text.
    assert storage.active_task(1)['id'] == 3
    assert dict(storage.task(1, 1)) == before


@pytest.mark.parametrize('command', ['/model', '/model opus', '/effort', '/effort high'])
async def test_unbound_settings_buttons_refused_live_and_on_replay(held, command):
    gateway, storage, _, channel, _ = held
    await gateway.receive(inbound(1, '/new First'))
    before = dict(storage.active_task(1))
    await gateway.receive(inbound('cb:legacy', command, reply_to='bot:1'))
    assert dict(storage.active_task(1)) == before
    assert 'no task target' in channel.sent[-1][1]
    # A durable callback from before a restart must also be refused.
    storage.journal(inbound('cb:replay', command))
    await gateway.start()
    assert dict(storage.active_task(1)) == before
    assert 'no task target' in channel.sent[-1][1]
    # Inbound.button is authoritative even for an embedding channel's own ids.
    await gateway.receive(Inbound('fake', '10', '1', 'custom-button', command, button=True))
    assert dict(storage.active_task(1)) == before
    assert 'no task target' in channel.sent[-1][1]


@pytest.mark.parametrize('task_id', [1, 1234567890123456789, 2**63 - 1])
async def test_callback_byte_guard_includes_full_task_id(held, monkeypatch, task_id):
    gateway, storage, _, channel, _ = held
    await gateway.receive(inbound(1, '/new First'))
    if task_id != 1:
        with storage.db:
            storage.db.execute("INSERT INTO task(id,conversation_id,executor,last_active) VALUES (?,1,'claude',CURRENT_TIMESTAMP)", (task_id,))
    prefix = f'/model #{task_id} claude '
    exact = 'x' * (64 - len(prefix.encode()))
    too_long = exact + 'x'
    unicode_long = 'м' * ((64 - len(prefix.encode())) // 2 + 1)
    monkeypatch.setenv('EIVOH_CLAUDE_MODELS', ','.join([exact, too_long, unicode_long]))
    await gateway.receive(inbound(2, f'/model #{task_id} claude'))
    assert callback_data(channel) == [prefix + exact]
    assert len(callback_data(channel)[0].encode()) == 64
    assert f'2. {too_long}' in channel.sent[-1][1] and f'3. {unicode_long}' in channel.sent[-1][1]
    assert f'/model #{task_id} <name or number>' in channel.sent[-1][1]
    await gateway.receive(inbound(3, f'/model #{task_id} 3'))
    assert storage.task(1, task_id)['model'] == unicode_long
    await gateway.receive(inbound(4, f'/effort #{task_id}'))
    assert all(len(data.encode()) <= 64 and data.startswith(f'/effort #{task_id} ') for data in callback_data(channel))


@pytest.mark.parametrize('data,expected', [('/tasks', 'Tasks:'), ('/task 2', 'Current task 2'),
                                         ('/stop', 'Stopped task 2'), ('/stop 1', 'Stopped task 1')])
async def test_telegram_callbacks_do_not_inherit_clicked_message_reply_target(held, data, expected):
    gateway, storage, _, channel, _ = held
    await gateway.receive(inbound(1, '/new First'))
    await gateway.receive(inbound(2, '/new Second'))
    bot = make_bot()
    bot.get_updates = AsyncMock(side_effect=[[
        SimpleNamespace(update_id=1, effective_user=SimpleNamespace(id=1),
                        effective_chat=SimpleNamespace(id=10, type='private'),
                        effective_message=SimpleNamespace(message_id=100, reply_to_message=SimpleNamespace(message_id=200)),
                        callback_query=SimpleNamespace(id='tap', data=data, answer=AsyncMock()))
    ], asyncio.CancelledError()])
    # Both physical ids identify task 1, but a callback is not a typed reply.
    storage.remember_outbound('fake', '10', ['100', '200'], 1)
    async def receive(message):
        assert message.reply_to is None
        await gateway.receive(Inbound('fake', message.chat_id, message.user_id, message.message_id,
                                      message.text, message.reply_to, button=message.button))
    with pytest.raises(asyncio.CancelledError):
        await TelegramChannel('unused', 1, bot).poll(receive)
    assert expected in channel.sent[-1][1]
    assert storage.active_task(1)['id'] == 2
