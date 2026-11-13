"""Offline task routing and scheduling, with explicit executor release points."""
import asyncio
import json
import sqlite3

import pytest

from conftest import FakeChannel, inbound, until
from eivoh.executors.base import Event, Record
from eivoh.gateway import Gateway, RESTART_PROMPT
from eivoh.storage import MIGRATIONS, Storage


class HeldExecutor:
    def __init__(self, name='claude'):
        self.name = name
        self.calls = []
        self.releases = {}
        self.cancelled = []

    async def start(self, run_id, prompt, model=None, session_id=None):
        session_id = session_id or f'{self.name}-{run_id}'
        release = self.releases.setdefault(run_id, asyncio.Event())
        self.calls.append((run_id, prompt, model, session_id))
        yield Record(None, {}, (Event('session_started', {}, None, session_id),))
        yield Record(None, {}, (Event('text_delta', {'text': 'Working'}, None),))
        await release.wait()
        yield Record(None, {}, (Event('turn_completed', {'status': 'completed', 'text': f'Answer {run_id}'}, None),))

    def resume(self, run_id, session_id, prompt, model=None):
        return self.start(run_id, prompt, model, session_id)

    async def cancel(self, run_id):
        self.cancelled.append(run_id)
        if run_id in self.releases:
            self.releases[run_id].set()

    async def shutdown(self):
        for release in self.releases.values():
            release.set()


@pytest.fixture
async def held(rig, request):
    _, storage, _, channel, path = rig
    executor = HeldExecutor()
    gateway = Gateway(storage, executor, {'fake': channel}, edit_interval=0.01,
                      max_parallel=getattr(request, 'param', 3))
    yield gateway, storage, executor, channel, path
    await gateway.shutdown()


async def two_tasks(held):
    gateway, storage, executor, _, _ = held
    await gateway.receive(inbound(1, 'first'))
    await until(lambda: len(executor.calls) == 1)
    await gateway.receive(inbound(2, '/new Second'))
    await gateway.receive(inbound(3, 'second'))
    await until(lambda: len(executor.calls) == 2)
    return storage.turn_for(1), storage.turn_for(3)


async def test_parallel_answers_labels_and_immutable_fifo(held):
    gateway, storage, executor, channel, _ = held
    first, second = await two_tasks(held)
    assert first['task_id'] != second['task_id']
    assert first['status'] == second['status'] == 'running'
    await gateway.receive(inbound(4, 'first queued', reply_to='1'))
    await gateway.receive(inbound(5, '/task 2'))
    assert storage.turn_for(4)['task_id'] == first['task_id']
    executor.releases[first['id']].set()
    await until(lambda: len(executor.calls) == 3)
    queued = storage.turn_for(4)
    assert queued['task_id'] == first['task_id']
    assert executor.calls[-1][1] == 'first queued'
    assert executor.calls[-1][3] == first['session_id']
    executor.releases[queued['id']].set()
    executor.releases[second['id']].set()
    await gateway.idle()
    displayed = [text for _, text in channel.sent] + [text for _, _, text in channel.edits]
    assert f'#t1 first\nAnswer {first["id"]}' in displayed
    assert f'#t2 Second\nAnswer {second["id"]}' in displayed
    assert storage.stored_result(first['id']) == (f'Answer {first["id"]}', None)
    assert '#t' not in storage.transcript(first['task_id'], 99)[0]
    assert {r['task_id'] for r in storage.db.execute('SELECT * FROM outbound')} == {1, 2}
    assert not gateway.workers


@pytest.mark.parametrize('held', [1], indirect=True)
async def test_global_parallel_limit_leaves_waiters_queued(held):
    gateway, storage, executor, _, _ = held
    await gateway.receive(inbound(1, 'first'))
    await until(lambda: len(executor.calls) == 1)
    # Also bounds other conversations, not just the current chat.
    await gateway.receive(inbound(2, 'second', chat='20'))
    await gateway.receive(inbound(3, '/model opus', chat='20'))
    await asyncio.sleep(0.03)
    assert len(executor.calls) == 1
    assert storage.turn_for(2)['status'] == 'queued'
    assert storage.turn_for(2)['executor'] is None
    assert storage.inbound(3)['state'] == 'done'
    executor.releases[1].set()
    await until(lambda: len(executor.calls) == 2)
    assert executor.calls[-1][2] == 'opus'
    executor.releases[2].set()
    await gateway.idle()


@pytest.mark.parametrize('command,stopped', [('/stop', {2}), ('/stop 1', {1}), ('/stop all', {1, 2})])
async def test_stop_scope_running_and_queued(held, command, stopped):
    gateway, storage, executor, _, _ = held
    first, second = await two_tasks(held)
    await gateway.receive(inbound(4, 'queue one', reply_to='1'))
    await gateway.receive(inbound(5, 'queue two'))
    original_cancel = executor.cancel
    async def cancel(run_id):
        # Cancellation is durable before any signal reaches the executor.
        turn = storage.db.execute('SELECT * FROM turn WHERE id=?', (run_id,)).fetchone()
        assert turn['status'] == 'cancelled'
        await original_cancel(run_id)
    executor.cancel = cancel
    await gateway.receive(inbound(6, command))
    executor.cancel = original_cancel
    for mid in (1, 3, 4, 5):
        turn = storage.turn_for(mid)
        assert (turn['status'] == 'cancelled') == (turn['task_id'] in stopped)
    for turn in (first, second):
        if turn['task_id'] not in stopped:
            assert storage.turn_for(turn['inbound_id'])['status'] == 'running'
    await gateway.receive(inbound(7, '/stop all'))
    await gateway.idle()
    assert not storage.pending()


async def test_close_switch_reopen_and_default_routing(held):
    gateway, storage, executor, channel, _ = held
    await two_tasks(held)
    await gateway.receive(inbound(4, 'queued'))
    last_active = storage.task(1, 1)['last_active']
    await gateway.receive(inbound(5, '/close'))
    assert storage.task(1, 2)['status'] == 'closed'
    assert storage.active_task(1)['id'] == 1
    assert storage.task(1, 1)['last_active'] > last_active
    assert storage.turn_for(3)['status'] == storage.turn_for(4)['status'] == 'cancelled'
    assert storage.turn_for(1)['status'] == 'running'
    assert channel.sent[-1][1].startswith('#t2 Second\nClosed')
    await gateway.receive(inbound(6, '/task 2'))
    assert storage.active_task(1)['id'] == 2
    assert storage.task(1, 2)['status'] == 'active'
    assert 'Last message: queued' in channel.sent[-1][1]
    assert 'Last answer: Answer 2' in channel.sent[-1][1]
    await gateway.receive(inbound(7, '/close 1'))
    assert storage.active_task(1)['id'] == 2
    await gateway.receive(inbound(8, '/close'))
    assert storage.active_task(1) is None
    await gateway.receive(inbound(9, 'reopen by reply', reply_to='1'))
    assert storage.turn_for(9)['task_id'] == 1
    assert storage.task(1, 1)['status'] == 'active'
    # Reply routing reopens its task without changing the current pointer.
    assert storage.active_task(1) is None
    await gateway.receive(inbound(10, 'new current'))
    assert storage.active_task(1)['id'] == 3
    await gateway.receive(inbound(11, '/stop all'))


async def test_reply_to_owner_bot_and_unknown_fallback(held):
    gateway, storage, executor, channel, path = held
    await gateway.receive(inbound(1, 'original'))
    await until(lambda: 1 in executor.releases)
    executor.releases[1].set()
    await gateway.idle()
    answer_id = storage.db.execute('SELECT message_id FROM outbound WHERE task_id=1').fetchone()[0]
    await gateway.receive(inbound(2, '/new another'))
    notice_id = 'bot:' + str(len(channel.sent))
    await gateway.receive(inbound(3, 'owner reply', reply_to='1'))
    await gateway.receive(inbound(4, 'answer reply', reply_to=answer_id))
    await gateway.receive(inbound(5, 'unknown reply', reply_to='unknown'))
    await gateway.receive(inbound(6, 'command reply', reply_to=notice_id))
    assert [storage.turn_for(mid)['task_id'] for mid in (3, 4, 5, 6)] == [1, 1, 2, 2]
    reopened = Storage(path / 'state.db')
    try:
        assert reopened.reply_task(1, answer_id)['id'] == 1
        assert reopened.reply_task(1, notice_id)['id'] == 2
    finally:
        reopened.close()
    await gateway.receive(inbound(7, '/stop all'))


async def test_command_reply_targets_and_explicit_precedence(held):
    gateway, storage, executor, channel, _ = held
    await two_tasks(held)
    await gateway.receive(inbound(4, '/title renamed first', reply_to='1'))
    await gateway.receive(inbound(5, '/model opus', reply_to='1'))
    await gateway.receive(inbound(6, '/model', reply_to='1'))
    assert storage.task(1, 1)['title'] == 'renamed first'
    assert storage.task(1, 1)['model'] == 'opus'
    assert storage.task(1, 2)['model'] is None
    assert channel.sent[-1][1].startswith('#t1 renamed first\nModel: claude / opus')
    await gateway.receive(inbound(7, '/stop', reply_to='1'))
    assert storage.turn_for(1)['status'] == 'cancelled'
    assert storage.turn_for(3)['status'] == 'running'
    await gateway.receive(inbound(8, '/close 2', reply_to='1'))
    assert storage.task(1, 2)['status'] == 'closed'
    assert storage.active_task(1)['id'] == 1
    await gateway.receive(inbound(9, '/close', reply_to='1'))
    assert storage.active_task(1) is None


async def test_task_list_pages_order_buttons_and_closed(held):
    gateway, storage, _, channel, _ = held
    for mid in range(1, 19):
        await gateway.receive(inbound(mid, f'/new Title {mid}'))
    await gateway.receive(inbound(19, '/tasks'))
    text = channel.sent[-1][1]
    assert 'page 1/3' in text and '10 more open' in text
    assert len([line for line in text.splitlines() if 'claude/default' in line]) == 8
    assert channel.buttons[-1] == [[(f'{i} · Title {i}', f'/task {i}')] for i in range(18, 10, -1)] + [[('Older ›', '/tasks 2')]]
    await gateway.receive(inbound(20, '/tasks 2'))
    assert channel.buttons[-1][-1] == [('‹ Newer', '/tasks 1'), ('Older ›', '/tasks 3')]
    await gateway.receive(inbound(21, '/task 1'))
    await gateway.receive(inbound(22, '/tasks'))
    assert channel.buttons[-1][0][0][1] == '/task 1'
    assert '▶ 1 · `Title 1`' in channel.sent[-1][1]
    await gateway.receive(inbound(23, '/close 1'))
    await gateway.receive(inbound(24, '/tasks'))
    assert '/task 1' not in [data for row in channel.buttons[-1] for _, data in row]
    await gateway.receive(inbound(25, '/tasks all'))
    assert '1 · `Title 1`' in channel.sent[-1][1] and 'closed' in channel.sent[-1][1]
    assert channel.buttons[-1][-1] == [('Older ›', '/tasks all 2')]
    await gateway.receive(inbound(26, '/tasks all 2'))
    assert channel.buttons[-1][-1] == [('‹ Newer', '/tasks all 1'), ('Older ›', '/tasks all 3')]
    await gateway.receive(inbound(27, '/task'))
    assert 'Tasks: ' in channel.sent[-1][1]
    assert storage.db.execute('SELECT COUNT(*) FROM turn').fetchone()[0] == 0


async def test_titles_snippets_and_single_task_label(held):
    gateway, storage, executor, channel, _ = held
    text = '[Voice message, speech-to-text]\n  a   long\n' + 'word ' * 60
    await gateway.receive(inbound(1, text))
    title = storage.active_task(1)['title']
    assert len(title) == 40 and title.startswith('a long word') and title.endswith('…')
    await until(lambda: 1 in executor.releases)
    executor.releases[1].set()
    await gateway.idle()
    assert all(not text.startswith('#t') for _, text in channel.sent)
    await gateway.receive(inbound(2, '/title Chosen\n title'))
    assert storage.active_task(1)['title'] == 'Chosen title'
    await gateway.receive(inbound(3, '/task 1'))
    snippet = next(line for line in channel.sent[-1][1].splitlines() if line.startswith('Last message: '))
    assert len(snippet.removeprefix('Last message: ')) <= 200
    await gateway.receive(inbound(4, '/new claude Explicit title'))
    await gateway.receive(inbound(5, 'does not replace title'))
    assert storage.active_task(1)['title'] == 'Explicit title'
    await gateway.receive(inbound(6, '/stop all'))


@pytest.mark.parametrize('text', ['/task 1', '/close 1', '/stop 1', '/task 999', '/task -1', '/task 9999999999999999999999'])
async def test_foreign_and_unknown_task_rejected(held, text):
    gateway, storage, _, channel, _ = held
    await gateway.receive(inbound(1, '/new private to first chat'))
    original = dict(storage.task(1, 1))
    await gateway.receive(inbound(2, text, chat='20'))
    assert dict(storage.task(1, 1)) == original
    assert storage.active_task(2) is None
    assert len(channel.sent[-1][1].splitlines()) == 1
    assert 'Unknown task' in channel.sent[-1][1] or 'positive task number' in channel.sent[-1][1]
    assert storage.turn_for(2) is None


async def test_callbacks_revalidate_and_deduplicate(held):
    gateway, storage, _, channel, _ = held
    await gateway.receive(inbound(1, '/new first'))
    await gateway.receive(inbound(2, '/new second'))
    await gateway.receive(inbound('cb:one', '/task 1'))
    count = len(channel.sent)
    await gateway.receive(inbound('cb:one', '/task 2'))
    assert storage.active_task(1)['id'] == 1 and len(channel.sent) == count
    await gateway.receive(inbound('cb:malformed', 'ordinary text'))
    assert 'Unknown command' in channel.sent[-1][1]
    assert storage.db.execute('SELECT COUNT(*) FROM turn').fetchone()[0] == 0


async def test_receive_lock_covers_journal_binding(held):
    gateway, storage, executor, channel, _ = held
    await two_tasks(held)
    entered, release = asyncio.Event(), asyncio.Event()
    original_send = channel.send
    async def send(chat, text, buttons=None):
        if 'Stopped task 2.' in text:
            entered.set()
            await release.wait()
        return await original_send(chat, text, buttons)
    channel.send = send
    stop = asyncio.create_task(gateway.receive(inbound(4, '/stop')))
    await entered.wait()
    switch = asyncio.create_task(gateway.receive(inbound(5, '/task 1')))
    message = asyncio.create_task(gateway.receive(inbound(6, 'after switch')))
    await asyncio.sleep(0)
    assert storage.inbound(6) is None
    release.set()
    await asyncio.gather(stop, switch, message)
    assert storage.turn_for(6)['task_id'] == 1
    await gateway.receive(inbound(7, '/stop all'))


async def test_unapplied_commands_replayed_in_order_before_workers(held):
    gateway, storage, executor, _, _ = held
    row = storage.journal(inbound(1, 'waiting'))
    storage.claim(row['id'], 3)
    # A restart sees all durable commands before any recovered turn can launch.
    for mid, text in enumerate(['/new second', '/title renamed', '/model opus', '/task 1', '/close', '/tasks'], 2):
        storage.journal(inbound(mid, text))
    await gateway.start()
    await gateway.idle()
    assert not executor.calls
    assert storage.turn_for(1)['status'] == 'cancelled'
    assert storage.task(1, 1)['status'] == 'closed'
    assert storage.active_task(1)['id'] == 2
    assert storage.active_task(1)['title'] == 'renamed'
    assert storage.active_task(1)['model'] == 'opus'
    assert not storage.pending()
    assert storage.db.execute('SELECT COUNT(*) FROM task').fetchone()[0] == 2
    await gateway.start()
    assert storage.db.execute('SELECT COUNT(*) FROM task').fetchone()[0] == 2


async def test_two_running_tasks_recover_original_routing_and_prompt(held):
    old, storage, executor, channel, path = held
    await old.receive(inbound(1, '/model haiku'))
    await old.receive(inbound(2, 'first'))
    await until(lambda: len(executor.calls) == 1)
    await old.receive(inbound(3, '/new Second'))
    await old.receive(inbound(4, 'second'))
    await until(lambda: len(executor.calls) == 2)
    first, second = storage.turn_for(2), storage.turn_for(4)
    # The first turn keeps Claude/haiku even after the task changes executor.
    codex = HeldExecutor('codex')
    old.executors['codex'] = codex
    (path / 'models_cache.json').write_text(json.dumps({'models': [{'slug': 'test-codex', 'visibility': 'list'}]}))
    await old.receive(inbound(5, '/model test-codex', reply_to='2'))
    await old.receive(inbound(6, '/model opus'))
    await old.shutdown()
    assert storage.turn_for(2)['status'] == storage.turn_for(4)['status'] == 'running'
    assert storage.stored_result(first['id']) is None
    assert storage.session(1, 'claude')['synced_inbound_id'] == 0
    reopened = Storage(path / 'state.db')
    replacement = HeldExecutor()
    other = HeldExecutor('codex')
    new = Gateway(reopened, replacement, {'fake': channel}, executors={'codex': other}, edit_interval=0)
    try:
        await new.start()
        await until(lambda: len(replacement.calls) == 2)
        assert not other.calls
        for call, turn in zip(sorted(replacement.calls), (first, second)):
            assert call[2] == turn['model']
            assert call[3] == turn['session_id']
            assert call[1] == RESTART_PROMPT + '\n\nOriginal request:\n' + storage.inbound(turn['inbound_id'])['text']
            replacement.releases[turn['id']].set()
        await new.idle()
        for mid in (2, 4):
            assert reopened.turn_for(mid)['status'] == 'completed'
            assert reopened.turn_for(mid)['recovery_count'] == 1
            assert reopened.turn_for(mid)['executor'] == 'claude'
        assert reopened.session(1, 'claude')['synced_inbound_id'] == 2
        assert reopened.session(1, 'codex') is None
    finally:
        await new.shutdown()
        reopened.close()


def test_migration_three_preserves_v2_sessions_turns_and_legacy_column(tmp_path):
    path = tmp_path / 'v2.db'
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:2]:
            for statement in migration:
                db.execute(statement)
        db.execute('PRAGMA user_version=2')
        db.execute("INSERT INTO conversation VALUES (1,'fake','10',NULL)")
        db.execute("INSERT INTO task(id,conversation_id,executor,session_id,model) VALUES (1,1,'claude','old-session','opus')")
        db.execute('UPDATE conversation SET active_task_id=1')
        for mid, status in [(1, 'completed'), (2, 'running'), (3, 'queued')]:
            db.execute("INSERT INTO inbound(id,conversation_id,channel,chat_id,user_id,message_id,text) VALUES (?,1,'fake','10','1',?,'request')", (mid, str(mid)))
            db.execute("INSERT INTO turn(id,task_id,inbound_id,conversation_id,status,session_id) VALUES (?,1,?,1,?,?)", (mid, mid, status, 'old-session' if mid < 3 else None))
        payload = json.dumps({'executor': 'ClaudeExecutor', 'requested_model': 'haiku'})
        db.execute("INSERT INTO raw_event(turn_id,sequence,stream,payload,kinds,events,created_at) VALUES (2,1,'meta',?,'[]','[]','2099-01-01 00:00:00')", (payload,))
    store = Storage(path)
    try:
        assert store.db.execute('PRAGMA user_version').fetchone()[0] == 3
        assert dict(store.session(1, 'claude')) == {'task_id': 1, 'executor': 'claude', 'session_id': 'old-session', 'synced_inbound_id': 1}
        assert store.task(1, 1)['session_id'] == 'old-session'
        assert store.task(1, 1)['last_active'] == '2099-01-01 00:00:00'
        assert store.turn_for(2)['executor'] == 'claude' and store.turn_for(2)['model'] == 'haiku'
        assert store.turn_for(3)['executor'] is store.turn_for(3)['model'] is None
        turn, recovering = store.claim(2, 3)
        assert recovering and turn['model'] == 'haiku' and turn['resume_id'] == 'old-session'
        assert store.db.execute('SELECT COUNT(*) FROM turn').fetchone()[0] == 3
        assert store.db.execute('SELECT payload FROM raw_event').fetchone()[0] == payload
        assert not store.db.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        store.close()


async def test_task_stop_cleans_only_its_real_fake_process_tree(rig):
    from eivoh.executors.claude import process_table
    gateway, storage, executor, _, path = rig
    await gateway.receive(inbound(1, 'hang'))
    first_pids = []
    def pids_written():
        try:
            first_pids[:] = json.loads((path / 'pids.json').read_text())
            return len(first_pids) == 2
        except (OSError, ValueError):
            return False  # File creation precedes completion of the fixture write.
    await until(pids_written)
    first_pids += [tree.process.pid for tree in executor.running.values()]
    await gateway.receive(inbound(2, '/new Second'))
    await gateway.receive(inbound(3, 'before-init'))
    second_id = storage.turn_for(3)['id']
    await until(lambda: second_id in executor.running)
    other_pid = executor.running[second_id].process.pid
    await gateway.receive(inbound(4, '/stop 1'))
    assert not any(pid in process_table() for pid in first_pids)
    assert other_pid in process_table()
    assert storage.turn_for(3)['status'] == 'running'
    await gateway.receive(inbound(5, '/stop all'))
    await gateway.idle()
    assert other_pid not in process_table()
    assert not executor.running and not executor.starting


@pytest.mark.parametrize('status', ['completed', 'failed', 'cancelled'])
def test_session_progress_advances_on_any_final_status(tmp_path, status):
    store = Storage(tmp_path / 'progress.db')
    try:
        row = store.journal(inbound(1, 'request'))
        turn, _ = store.claim(row['id'], 3)
        store.append(turn['id'], Record(None, {}, (Event('session_started', {}, None, 'original-session'),)))
        change = store.journal(inbound(2, '/model another'))
        store.set_model(change['id'], 'codex', 'another')
        store.finish(turn['id'], status)
        assert store.session(turn['task_id'], 'claude')['synced_inbound_id'] == row['id']
        assert store.session(turn['task_id'], 'codex') is None
        # The obsolete task column is retained, but remains unused.
        assert store.task(1, turn['task_id'])['session_id'] is None
    finally:
        store.close()


async def test_unknown_slash_text_is_an_ordinary_message(held):
    gateway, storage, _, channel, _ = held
    await gateway.receive(inbound(1, '/etc/hosts: what is in this file?'))
    turn = storage.turn_for(1)
    assert turn is not None and storage.task(1, turn['task_id'])['title'].startswith('/etc/hosts')
    assert not any('Unknown command' in text for _, text in channel.sent)


def test_migration_three_keeps_a_session_for_each_used_executor(tmp_path):
    path = tmp_path / 'v2.db'
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:2]:
            for statement in migration:
                db.execute(statement)
        db.execute('PRAGMA user_version=2')
        db.execute("INSERT INTO conversation VALUES (1,'fake','10',NULL)")
        db.execute("INSERT INTO task(id,conversation_id,executor,session_id) VALUES (1,1,'claude','claude-new')")
        turns = [(1, 'ClaudeExecutor', 'claude-old'), (2, 'CodexExecutor', 'codex-thread'), (3, 'ClaudeExecutor', 'claude-new')]
        for mid, executor, session in turns:
            db.execute("INSERT INTO inbound(id,conversation_id,channel,chat_id,user_id,message_id,text) VALUES (?,1,'fake','10','1',?,'request')", (mid, str(mid)))
            db.execute("INSERT INTO turn(id,task_id,inbound_id,conversation_id,status,session_id) VALUES (?,1,?,1,'completed',?)", (mid, mid, session))
            db.execute("INSERT INTO raw_event(turn_id,sequence,stream,payload,kinds,events) VALUES (?,1,'meta',?,'[]','[]')",
                       (mid, json.dumps({'executor': executor})))
    store = Storage(path)
    try:
        assert dict(store.session(1, 'claude')) == {'task_id': 1, 'executor': 'claude', 'session_id': 'claude-new', 'synced_inbound_id': 3}
        assert dict(store.session(1, 'codex')) == {'task_id': 1, 'executor': 'codex', 'session_id': 'codex-thread', 'synced_inbound_id': 2}
    finally:
        store.close()
