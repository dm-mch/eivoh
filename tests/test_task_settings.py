"""Task settings menus and inheritance, using offline executors only."""
import json

import pytest

from conftest import inbound, until
from test_tasks import HeldExecutor, held
from eivoh.models import effort_levels


def cache(path, entries):
    (path / 'models_cache.json').write_text(json.dumps({'models': entries}))


def model(slug, levels):
    return {'slug': slug, 'visibility': 'list',
            'supported_reasoning_levels': [{'effort': level} for level in levels]}


def buttons(channel):
    return [data for row in channel.buttons[-1] for _, data in row]


def test_supported_efforts_from_cache_and_fallback(tmp_path):
    generic = ['low', 'medium', 'high', 'xhigh']
    assert effort_levels('claude', 'opus') == generic + ['max']
    assert effort_levels('codex') == effort_levels('codex', 'absent') == generic
    cache(tmp_path, [model('limited', ['low', 'high']), model('extended', generic + ['max', 'ultra']),
                     model('none', [])])
    assert effort_levels('codex', 'limited') == ['low', 'high']
    assert effort_levels('codex', 'extended') == generic + ['max', 'ultra']
    assert effort_levels('codex', 'none') == []
    assert effort_levels('codex') == effort_levels('codex', 'absent') == generic
    assert effort_levels('unknown') == []
    (tmp_path / 'models_cache.json').write_bytes(b'\xff')
    assert effort_levels('codex', 'limited') == generic


async def test_effort_no_task_menu_refusal_and_creation(held):
    gateway, storage, _, channel, _ = held
    await gateway.receive(inbound(1, '/effort'))
    assert storage.active_task(1) is None
    assert 'claude / default · default' in channel.sent[-1][1]
    assert buttons(channel) == [f'/effort #new {level}' for level in ['low', 'medium', 'high', 'xhigh', 'max', 'default']]
    await gateway.receive(inbound(2, '/effort ultra'))
    assert storage.active_task(1) is None
    assert 'Unsupported effort' in channel.sent[-1][1]
    assert 'low, medium, high, xhigh, max, default' in channel.sent[-1][1]
    await gateway.receive(inbound(3, '/effort high'))
    assert storage.active_task(1)['effort'] == 'high'
    assert channel.sent[-1][1] == 'Effort: claude / default · high.'
    assert channel.buttons[-1] == [[('claude / default', '/model #1'), ('Effort: high', '/effort #1')]]
    await gateway.receive(inbound(4, '/effort default'))
    assert storage.active_task(1)['effort'] is None
    assert '· default.' in channel.sent[-1][1]
    assert storage.db.execute('SELECT COUNT(*) FROM turn').fetchone()[0] == 0


async def test_model_menus_pagination_and_text_choices(held, monkeypatch):
    gateway, storage, _, channel, path = held
    gateway.executors['codex'] = HeldExecutor('codex')
    cache(path, [model(f'test-{i}', ['low', 'high']) for i in range(1, 11)])
    await gateway.receive(inbound(1, '/model'))
    assert buttons(channel) == ['/model #new claude', '/model #new codex', '/effort #new']
    assert '1. fable' in channel.sent[-1][1] and '9. test-1' in channel.sent[-1][1]
    assert storage.active_task(1) is None
    await gateway.receive(inbound('cb:first', '/model #new codex'))
    assert 'page 1/2' in channel.sent[-1][1]
    assert buttons(channel) == [f'/model #new codex test-{i}' for i in range(1, 9)] + ['/model #new codex page 2']
    await gateway.receive(inbound('cb:page2', '/model #new codex page 2'))
    assert buttons(channel) == ['/model #new codex test-9', '/model #new codex test-10', '/model #new codex page 1']
    assert '9. test-9' in channel.sent[-1][1]
    cache(path, [model('changed-cache', ['low'])])
    await gateway.receive(inbound(4, '/model 9'))
    assert storage.active_task(1)['model'] == 'test-9'
    assert 'codex / test-9 · default' in channel.sent[-1][1]
    await gateway.receive(inbound(5, '/model claude'))
    assert storage.active_task(1)['executor'] == 'codex'  # A list is not a selection.
    await gateway.receive(inbound(6, '/model claude opus'))
    assert 'claude / opus · default' in channel.sent[-1][1]
    assert buttons(channel) == ['/model #1', '/effort #1']
    await gateway.receive(inbound(7, '/model CLAUDE-OPUS-5-5'))
    assert storage.active_task(1)['model'] == 'claude-opus-5-5'
    await gateway.receive(inbound(8, '/model fable-5'))
    assert storage.active_task(1)['model'] == 'claude-fable-5-2'
    await gateway.receive(inbound(9, '/model claude page 0'))
    assert 'positive number' in channel.sent[-1][1]
    assert not storage.pending()


async def test_model_callback_limit_is_bytes_and_text_fallback_works(held, monkeypatch):
    gateway, storage, _, channel, _ = held
    exact = 'x' * (64 - len('/model #new claude '))
    long_ascii = exact + 'x'
    long_unicode = 'м' * 24  # Fits in characters, exceeds 64 UTF-8 bytes with the target.
    monkeypatch.setenv('EIVOH_CLAUDE_MODELS', ','.join([exact, long_ascii, long_unicode]))
    await gateway.receive(inbound(1, '/model claude'))
    assert buttons(channel) == [f'/model #new claude {exact}']
    assert len(buttons(channel)[0].encode()) == 64
    assert f'2. {long_ascii}' in channel.sent[-1][1] and f'3. {long_unicode}' in channel.sent[-1][1]
    await gateway.receive(inbound(2, '/model 3'))
    assert storage.active_task(1)['model'] == long_unicode
    await gateway.receive(inbound(3, f'/model {long_ascii}'))
    assert storage.active_task(1)['model'] == long_ascii
    assert all(len(data.encode()) <= 64 for row in channel.buttons if row for group in row for _, data in group)


async def test_effort_reset_on_incompatible_model_and_executor(held):
    gateway, storage, _, channel, path = held
    gateway.executors['codex'] = HeldExecutor('codex')
    cache(path, [model('wide', ['low', 'high', 'ultra']), model('limited', ['low'])])
    await gateway.receive(inbound(1, '/effort high'))
    await gateway.receive(inbound(2, '/model codex wide'))
    assert storage.active_task(1)['effort'] == 'high'
    await gateway.receive(inbound(3, '/effort ultra'))
    await gateway.receive(inbound(4, '/model opus'))
    assert storage.active_task(1)['effort'] is None
    assert 'Effort reset to default' in channel.sent[-1][1]
    await gateway.receive(inbound(5, '/model codex wide'))
    await gateway.receive(inbound(6, '/effort high'))
    await gateway.receive(inbound(7, '/model codex limited'))
    assert storage.active_task(1)['effort'] is None
    assert 'Effort reset to default' in channel.sent[-1][1]
    await gateway.receive(inbound(8, '/effort'))
    assert buttons(channel) == ['/effort #1 low', '/effort #1 default']
    await gateway.receive(inbound(9, '/effort high'))
    assert storage.active_task(1)['effort'] is None
    assert 'Supported: low, default' in channel.sent[-1][1]


@pytest.mark.parametrize('active', [None, 'claude', 'codex'])
@pytest.mark.parametrize('explicit', [None, 'claude', 'codex'])
@pytest.mark.parametrize('title', ['', 'Chosen title'])
async def test_new_inheritance_matrix(held, active, explicit, title):
    gateway, storage, _, channel, path = held
    gateway.executors['codex'] = HeldExecutor('codex')
    cache(path, [model('test-model', ['low', 'high'])])
    if active:
        await gateway.receive(inbound(1, '/model opus' if active == 'claude' else '/model codex test-model'))
        await gateway.receive(inbound(2, '/effort high'))
    cmd = ' '.join(part for part in ['/new', explicit, title] if part)
    await gateway.receive(inbound(3, cmd))
    task = storage.active_task(1)
    expected_executor = explicit or active or 'claude'
    inherited = bool(active and (not explicit or explicit == active))
    expected_model = ('opus' if active == 'claude' else 'test-model') if inherited else None
    assert (task['executor'], task['model'], task['effort']) == (expected_executor, expected_model, 'high' if inherited else None)
    assert task['title'] == (title or None)
    assert f"{expected_executor} / {expected_model or 'default'} · {'high' if inherited else 'default'}" in channel.sent[-1][1]
    assert buttons(channel) == [f"/model #{task['id']}", f"/effort #{task['id']}"]
    assert not storage.session(task['id'], expected_executor)


async def test_switch_reply_routing_and_settings_buttons_keep_task(held):
    gateway, storage, executor, channel, _ = held
    await gateway.receive(inbound(1, '/model opus'))
    await gateway.receive(inbound(2, '/effort high'))
    await gateway.receive(inbound(3, '/new Second'))
    await gateway.receive(inbound(4, '/model haiku'))
    await gateway.receive(inbound(5, '/effort low'))
    await gateway.receive(inbound(6, '/task 1'))
    assert 'claude / opus · high' in channel.sent[-1][1]
    assert buttons(channel) == ['/model #1', '/effort #1']
    menu_id = 'bot:' + str(len(channel.sent))
    await gateway.receive(inbound('cb:switch', '/task 2'))
    assert 'claude / haiku · low' in channel.sent[-1][1]
    await gateway.receive(inbound('cb:menu', '/model #1', reply_to=menu_id))
    assert 'claude / opus · high' in channel.sent[-1][1]
    menu_id = 'bot:' + str(len(channel.sent))
    await gateway.receive(inbound('cb:effort', '/effort #1 medium', reply_to=menu_id))
    assert storage.task(1, 1)['effort'] == 'medium'
    assert storage.active_task(1)['id'] == 2 and storage.active_task(1)['effort'] == 'low'
    count = len(channel.sent)
    await gateway.receive(inbound('cb:effort', '/effort #1 max', reply_to=menu_id))
    assert len(channel.sent) == count and storage.task(1, 1)['effort'] == 'medium'
    await gateway.receive(inbound(7, 'active task'))
    await until(lambda: executor.calls)
    assert executor.calls[-1][2] == 'haiku' and executor.efforts[executor.calls[-1][0]] == 'low'
    executor.releases[executor.calls[-1][0]].set()
    await gateway.idle()
    await gateway.receive(inbound(8, '/tasks'))
    assert 'claude / opus · medium' in channel.sent[-1][1]
    assert 'claude / haiku · low' in channel.sent[-1][1]
    assert all(data.startswith('/task ') for data in buttons(channel))
    assert not channel.edits or all('· low' not in text for _, _, text in channel.edits)


@pytest.mark.parametrize('held', [1], indirect=True)
async def test_effort_changes_queued_unclaimed_turn_and_keeps_running_turn(held):
    gateway, storage, executor, channel, _ = held
    await gateway.receive(inbound(1, '/effort high'))
    await gateway.receive(inbound(2, 'running'))
    await until(lambda: executor.calls)
    await gateway.receive(inbound(3, 'queued'))
    await gateway.receive(inbound(4, '/effort low'))
    assert 'next turn launch' in channel.sent[-1][1]
    assert storage.turn_for(2)['effort'] == 'high'
    assert storage.turn_for(3)['executor'] is None
    executor.releases[1].set()
    await until(lambda: len(executor.calls) == 2)
    assert executor.efforts == {1: 'high', 2: 'low'}
    executor.releases[2].set()
    await gateway.idle()


async def test_close_current_uses_previous_tasks_settings(held):
    gateway, storage, _, channel, _ = held
    await gateway.receive(inbound(1, '/model opus'))
    await gateway.receive(inbound(2, '/effort high'))
    await gateway.receive(inbound(3, '/new Second'))
    await gateway.receive(inbound(4, '/model haiku'))
    await gateway.receive(inbound(5, '/effort low'))
    await gateway.receive(inbound(6, '/close'))
    assert storage.active_task(1)['id'] == 1
    assert 'Current task 1' in channel.sent[-1][1]
    assert 'claude / opus · high' in channel.sent[-1][1]


async def test_effort_long_cache_value_stays_text_only(held):
    gateway, storage, _, channel, path = held
    gateway.executors['codex'] = HeldExecutor('codex')
    level = 'x' * 57
    cache(path, [model('unusual', ['low', level])])
    await gateway.receive(inbound(1, '/model codex unusual'))
    await gateway.receive(inbound(2, '/effort'))
    assert level in channel.sent[-1][1]
    assert buttons(channel) == ['/effort #1 low', '/effort #1 default']
