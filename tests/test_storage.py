import sqlite3

import pytest

from eivoh.executors.claude import parse_line
from eivoh.storage import Storage
from conftest import inbound


def test_journal_dedupe_and_durability(tmp_path):
    path = tmp_path / 'state.db'
    store = Storage(path)
    first = store.journal(inbound(1, 'hello'))
    assert first['state'] == 'queued'
    assert store.journal(inbound(1, 'different redelivery')) is None
    assert store.journal(inbound(1, 'separate chat', chat='20'))
    store.close()
    store = Storage(path)
    assert len(store.pending()) == 2
    task, recovering = store.claim(first['id'], 3)
    assert not recovering and task['status'] == 'running'
    store.append(task['id'], parse_line('{"type":"system","subtype":"init","session_id":"session-1"}\n'))
    store.append(task['id'], parse_line('{"type":"rate_limit_event"}\n'))
    store.append(task['id'], parse_line('unknown malformed line\n'))
    records = store.db.execute('SELECT * FROM raw_event ORDER BY sequence').fetchall()
    assert [r['sequence'] for r in records] == [1, 2, 3]
    assert records[1]['raw_line'] == '{"type":"rate_limit_event"}\n'
    assert records[1]['kinds'] == '[]'
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        store.db.execute("DELETE FROM raw_event")
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        store.db.execute("UPDATE raw_event SET stream='x'")
    assert store.turn_for(first['id'])['session_id'] == 'session-1'
    store.finish(task['id'], 'completed')
    assert store.turn_for(first['id'])['status'] == 'completed'
    assert store.inbound(first['id'])['state'] == 'done'
    assert store.db.execute('PRAGMA synchronous').fetchone()[0] == 2
    assert store.db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    store.close()


@pytest.mark.parametrize('status', ['completed', 'failed', 'cancelled'])
def test_terminal_status(tmp_path, status):
    store = Storage(tmp_path / 'state.db')
    row = store.journal(inbound(1, 'x'))
    task, _ = store.claim(row['id'], 3)
    store.finish(task['id'], status)
    assert store.turn_for(row['id'])['status'] == status
    assert not store.pending()
    store.close()


def test_task_owns_session_across_turns_and_new(tmp_path):
    store = Storage(tmp_path / 'tasks.db')
    try:
        first = store.journal(inbound(1, 'first'))
        second = store.journal(inbound(2, 'second'))
        new = store.journal(inbound(3, '/new'))
        third = store.journal(inbound(4, 'third'))
        one, _ = store.claim(first['id'], 3)
        store.append(one['id'], parse_line('{"type":"system","subtype":"init","session_id":"old-session"}\n'))
        store.finish(one['id'], 'completed')
        two, _ = store.claim(second['id'], 3)
        assert one['task_id'] == two['task_id']
        assert two['resume_id'] == 'old-session'
        store.finish(two['id'], 'completed')
        store.new_task(new['id'])
        three, _ = store.claim(third['id'], 3)
        assert three['task_id'] != one['task_id'] and three['resume_id'] is None
        tasks = store.db.execute('SELECT * FROM task ORDER BY id').fetchall()
        assert [(t['executor'], t['session_id'], t['status']) for t in tasks] == [
            ('claude', 'old-session', 'closed'), ('claude', None, 'active')]
        assert store.db.execute('SELECT active_task_id FROM conversation').fetchone()[0] == three['task_id']
        assert store.db.execute('SELECT turn_id FROM raw_event').fetchone()[0] == one['id']
        assert not store.db.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        store.close()


def test_schema_version_reopen_and_future_rejected(tmp_path):
    path = tmp_path / 'version.db'
    store = Storage(path)
    assert store.db.execute('PRAGMA user_version').fetchone()[0] == 1
    store.journal(inbound(1, 'retained'))
    store.close()
    store = Storage(path)
    assert store.inbound(1)['text'] == 'retained'
    store.db.execute('PRAGMA user_version=99')
    store.close()
    with pytest.raises(ValueError, match='Unsupported schema version 99'):
        Storage(path)


def test_ordered_migrations_rollback_together(tmp_path, monkeypatch):
    import eivoh.storage as module
    path = tmp_path / 'migration.db'
    original = module.MIGRATIONS
    monkeypatch.setattr(module, 'MIGRATIONS', original + ((
        'CREATE TABLE later (id INTEGER)', 'INSERT INTO nonexistent VALUES (1)'),))
    with pytest.raises(sqlite3.OperationalError):
        Storage(path)
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 0
        assert not db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    monkeypatch.setattr(module, 'MIGRATIONS', original + ((
        'CREATE TABLE later (id INTEGER)',), ('INSERT INTO later VALUES (7)',)))
    store = Storage(path)
    assert store.db.execute('PRAGMA user_version').fetchone()[0] == 3
    assert store.db.execute('SELECT id FROM later').fetchone()[0] == 7
    store.close()


def test_stored_result_uses_latest_attempt(tmp_path):
    store = Storage(tmp_path / 'result.db')
    try:
        row = store.journal(inbound(1, 'work'))
        turn, _ = store.claim(row['id'], 3)
        store.append(turn['id'], parse_line('{"type":"error","error":"old interruption"}'))
        store.claim(row['id'], 3)
        store.append(turn['id'], parse_line('{"type":"system","subtype":"init","session_id":"session-1"}'))
        store.append(turn['id'], parse_line('{"type":"result","is_error":false,"result":"recovered"}'))
        assert store.stored_result(turn['id']) == ('recovered', None)
    finally:
        store.close()
