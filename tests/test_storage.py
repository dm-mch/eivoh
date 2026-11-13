import json
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
        one, _ = store.claim(first['id'], 3)
        store.append(one['id'], parse_line('{"type":"system","subtype":"init","session_id":"old-session"}\n'))
        store.finish(one['id'], 'completed')
        two, _ = store.claim(second['id'], 3)
        assert one['task_id'] == two['task_id']
        assert two['resume_id'] == 'old-session'
        store.finish(two['id'], 'completed')
        store.new_task(new['id'])
        third = store.journal(inbound(4, 'third'))
        three, _ = store.claim(third['id'], 3)
        assert three['task_id'] != one['task_id'] and three['resume_id'] is None
        tasks = store.db.execute('SELECT * FROM task ORDER BY id').fetchall()
        assert [(t['executor'], t['session_id'], t['status']) for t in tasks] == [
            ('claude', None, 'active'), ('claude', None, 'active')]
        assert store.db.execute('SELECT active_task_id FROM conversation').fetchone()[0] == three['task_id']
        assert store.db.execute('SELECT turn_id FROM raw_event').fetchone()[0] == one['id']
        assert not store.db.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        store.close()


def test_schema_version_reopen_and_future_rejected(tmp_path):
    path = tmp_path / 'version.db'
    store = Storage(path)
    assert store.db.execute('PRAGMA user_version').fetchone()[0] == 4
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
    assert store.db.execute('PRAGMA user_version').fetchone()[0] == len(original) + 2
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


def test_v1_upgrades_in_place(tmp_path):
    from eivoh.storage import MIGRATIONS
    path = tmp_path / 'v1.db'
    with sqlite3.connect(path) as db:
        for statement in MIGRATIONS[0]:
            db.execute(statement)
        db.execute('PRAGMA user_version=1')
        db.execute("INSERT INTO conversation(id,channel,chat_id) VALUES (1,'fake','10')")
        db.execute("INSERT INTO task(id,conversation_id,executor,session_id) VALUES (1,1,'claude','old-session')")
        db.execute('UPDATE conversation SET active_task_id=1')
        db.execute("INSERT INTO inbound(id,conversation_id,channel,chat_id,user_id,message_id,text,state) "
                   "VALUES (1,1,'fake','10','1','1','original','running')")
        db.execute("INSERT INTO turn(id,inbound_id,conversation_id,task_id,status,session_id) "
                   "VALUES (1,1,1,1,'running','old-session')")
    store = Storage(path)
    try:
        assert store.db.execute('PRAGMA user_version').fetchone()[0] == 4
        task = store.active_task(1)
        assert task['model'] is None and task['session_id'] == 'old-session'
        turn, recovering = store.claim(1, 3)
        assert recovering and turn['model'] is None and turn['resume_id'] == 'old-session'
        assert store.inbound(1)['text'] == 'original'
        assert not store.db.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        store.close()


def test_transcript_keeps_newest_whole_turns_and_final_answers(tmp_path):
    import json
    store = Storage(tmp_path / 'transcript.db')
    try:
        for mid, text in enumerate(['old', 'middle', 'latest'], 1):
            row = store.journal(inbound(mid, text))
            turn, _ = store.claim(row['id'], 3)
            store.append(turn['id'], parse_line(json.dumps({
                'type': 'result', 'result': 'answer ' + text, 'is_error': False})))
            store.finish(turn['id'], 'completed')
        full, count, truncated = store.transcript(turn['task_id'], 4)
        assert count == 3 and not truncated
        assert full.index('User:\nold') < full.index('User:\nmiddle') < full.index('User:\nlatest')
        assert 'Assistant:\nanswer latest' in full
        text, count, truncated = store.transcript(turn['task_id'], 4, limit=100)
        assert count == 1 and truncated and 'Transcript truncated' in text
        assert len(text) <= 100
        assert 'User:\nlatest' in text and 'User:\nold' not in text and 'User:\nmiddle' not in text
        assert store.transcript(turn['task_id'], 4, limit=1)[1:] == (0, True)
        assert store.transcript(turn['task_id'] + 1, 4) == ('', 0, False)
    finally:
        store.close()


def test_recovery_keeps_recorded_model_and_executor(tmp_path):
    store = Storage(tmp_path / 'routing.db')
    try:
        row = store.journal(inbound(1, 'interrupted'))
        turn, _ = store.claim(row['id'], 3)
        store.append(turn['id'], parse_line('{"type":"system","subtype":"init","session_id":"old-session"}'))
        command_row = store.journal(inbound(2, '/model astra'))
        store.set_model(command_row['id'], 'codex', 'gpt-6-astra')
        recovered, recovering = store.claim(row['id'], 3)
        assert recovering
        assert recovered['session_id'] == 'old-session'  # Historical evidence only.
        assert recovered['resume_id'] == 'old-session'
        assert (recovered['executor'], recovered['model']) == ('claude', None)
    finally:
        store.close()


@pytest.mark.parametrize('user_size,assistant_size', [(40000, 40), (40, 40000), (40000, 40000)])
def test_transcript_excerpts_oversized_newest_turn(tmp_path, user_size, assistant_size):
    store = Storage(tmp_path / 'transcript.db')
    try:
        user = 'user beginning ' + 'u' * user_size + ' user ending'
        assistant = 'answer beginning ' + 'a' * assistant_size + ' answer ending'
        row = store.journal(inbound(1, user))
        turn, _ = store.claim(row['id'], 3)
        store.append(turn['id'], parse_line(json.dumps({'type': 'result', 'result': assistant})))
        store.finish(turn['id'], 'completed')
        text, count, truncated = store.transcript(turn['task_id'], 2)
        assert len(text) <= 30000 and count == 1 and truncated
        assert 'User:\nuser beginning ' in text and ' user ending\n\nAssistant:' in text
        assert 'Assistant:\nanswer beginning ' in text and ' answer ending\n\n' in text
        assert text.count('[Text truncated]') == int(user_size > 30000) + int(assistant_size > 30000)
        for limit in range(0, 300):
            bounded, count, truncated = store.transcript(turn['task_id'], 2, limit=limit)
            assert len(bounded) <= limit and truncated
            if count:
                assert 'User:\n' in bounded and 'Assistant:\n' in bounded
                assert '[Text truncated]' in bounded
        # A newer small turn takes priority over the oversized older turn.
        row = store.journal(inbound(2, 'recent question'))
        latest, _ = store.claim(row['id'], 3)
        store.append(latest['id'], parse_line('{"type":"result","result":"recent answer"}'))
        store.finish(latest['id'], 'completed')
        text, count, truncated = store.transcript(turn['task_id'], 3)
        assert count == 1 and truncated
        assert 'User:\nrecent question' in text and 'Assistant:\nrecent answer' in text
    finally:
        store.close()


def test_legacy_ordinary_resume_does_not_reinject_history(tmp_path):
    store = Storage(tmp_path / 'resume.db')
    try:
        first = store.journal(inbound(1, 'earlier request'))
        turn, _ = store.claim(first['id'], 3)
        session = parse_line('{"type":"system","subtype":"init","session_id":"same-session"}')
        store.append(turn['id'], session)
        store.append(turn['id'], parse_line('{"type":"result","result":"earlier answer"}'))
        store.finish(turn['id'], 'completed')
        second = store.journal(inbound(2, 'current request'))
        turn, _ = store.claim(second['id'], 3)
        store.append(turn['id'], session)  # No prompt metadata from the old code.
        turn, recovering = store.claim(second['id'], 3)
        assert recovering
        assert store.prepare_prompt(turn, second['text']) == 'current request'
        assert store.prepare_prompt(turn, second['text']) == 'current request'
    finally:
        store.close()


def test_transcript_notice_does_not_evict_only_recent_turn(tmp_path):
    store = Storage(tmp_path / 'boundary.db')
    try:
        for mid in (1, 2):
            row = store.journal(inbound(mid, f'question {mid} ' + 'u' * 120))
            turn, _ = store.claim(row['id'], 3)
            store.append(turn['id'], parse_line(json.dumps({'type': 'result', 'result': f'answer {mid} ' + 'a' * 120})))
            store.finish(turn['id'], 'completed')
        # Newest full turn fits; adding the omission notice requires excerpts.
        text, count, truncated = store.transcript(turn['task_id'], 3, limit=300)
        assert len(text) <= 300 and count == 1 and truncated
        assert 'User:\nquestion 2 ' in text and 'Assistant:\nanswer 2 ' in text
        assert '[Text truncated]' in text and 'question 1' not in text
    finally:
        store.close()
