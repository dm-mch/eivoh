"""Effort follows the same immutable launch/recovery boundary as the model."""
import pytest

from conftest import inbound
from eivoh.executors.base import Event, Record
from eivoh.storage import Storage


def set_effort(store, mid, effort):
    row = store.journal(inbound(mid, '/effort ' + (effort or 'default')))
    return store.set_effort(row['id'], effort)


def test_v7_migration_preserves_all_existing_columns(tmp_path, monkeypatch):
    import eivoh.storage as module
    path = tmp_path / 'v7.db'
    with monkeypatch.context() as patch:
        patch.setattr(module, 'MIGRATIONS', module.MIGRATIONS[:7])
        store = Storage(path)
        store.db.execute("INSERT INTO conversation(id,channel,chat_id,active_task_id) VALUES (1,'fake','10',NULL)")
        store.db.execute("INSERT INTO task(id,conversation_id,executor,model,title) VALUES (1,1,'claude','opus','Keep title')")
        store.db.execute("UPDATE conversation SET active_task_id=1 WHERE id=1")
        store.db.execute("INSERT INTO inbound(id,conversation_id,channel,chat_id,user_id,message_id,text,state) VALUES (1,1,'fake','10','1','1','keep request','running')")
        store.db.execute("INSERT INTO turn(id,inbound_id,conversation_id,task_id,status,executor,model) VALUES (1,1,1,1,'running','claude','opus')")
        turn = store.turn_for(1)
        store.append(turn['id'], Record(None, {'requested_model': 'opus'},
                     (Event('session_started', {}, {}, 'keep-session'),), stream='meta'))
        store.prepare_prompt(turn, 'keep request')
        store.remember_outbound('fake', '10', ['bot:1'], 1, 1)
        tables = [row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        columns = {table: ','.join(row['name'] for row in store.db.execute(f'PRAGMA table_info({table})')) for table in tables}
        before = {table: [tuple(row) for row in store.db.execute(f'SELECT * FROM {table} ORDER BY rowid')] for table in tables}
        store.close()
    store = Storage(path)
    try:
        assert store.db.execute('PRAGMA user_version').fetchone()[0] == 8
        for table in tables:
            assert [tuple(row) for row in store.db.execute(f'SELECT {columns[table]} FROM {table} ORDER BY rowid')] == before[table]
        assert store.task(1, 1)['effort'] is None
        assert store.turn_for(1)['effort'] is None
        assert not store.db.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        store.close()


@pytest.mark.parametrize('original', [None, 'low'])
def test_claim_and_reopen_keep_effort_while_task_changes(tmp_path, original):
    path = tmp_path / 'state.db'
    store = Storage(path)
    set_effort(store, 1, original)
    store.journal(inbound(2, 'running'))
    turn, _ = store.claim(2, 3)
    prompt = store.prepare_prompt(turn, 'running')
    store.append(turn['id'], Record(None, {}, (Event('session_started', {}, {}, 'kept-session'),)))
    store.journal(inbound(3, 'already queued'))
    set_effort(store, 4, 'high')
    assert store.turn_for(2)['effort'] == original
    assert store.turn_for(3)['executor'] is None
    store.close()
    store = Storage(path)
    try:
        recovered, recovering = store.claim(2, 3)
        assert recovering and recovered['effort'] == original
        assert recovered['resume_id'] == 'kept-session'
        assert store.prepare_prompt(recovered, 'must not replace prompt') == prompt
        store.finish(turn['id'], 'completed')
        queued, recovering = store.claim(3, 3)
        assert not recovering and queued['effort'] == 'high'
    finally:
        store.close()


def test_effort_change_is_steering_barrier_and_buffered_ack_keeps_parent(tmp_path):
    store = Storage(tmp_path / 'state.db')
    try:
        set_effort(store, 1, 'low')
        store.journal(inbound(2, 'running'))
        parent, _ = store.claim(2, 3)
        store.journal(inbound(3, 'accepted before change'))
        input_id = store.reserve_steer(parent['id'], 3)
        assert store.turn_for(3)['effort'] == 'low'
        store.steer_outcome(3, 'unknown')
        assert store.turn_for(3)['effort'] is None
        set_effort(store, 4, 'high')
        store.journal(inbound(5, 'new effort'))
        assert store.reserve_steer(parent['id'], 5) is None
        store.append(parent['id'], Record(None, {}, (Event('input_accepted', {'input_id': input_id}, {}),)))
        assert store.turn_for(3)['effort'] == 'low'
        assert store.steering(3)['state'] == 'accepted'
        store.finish(parent['id'], 'completed')
        queued, _ = store.claim(5, 3)
        assert queued['effort'] == 'high'
    finally:
        store.close()


def test_unknown_steering_recovery_clears_effort_for_new_claim(tmp_path):
    store = Storage(tmp_path / 'state.db')
    try:
        set_effort(store, 1, 'low')
        store.journal(inbound(2, 'running'))
        parent, _ = store.claim(2, 3)
        store.journal(inbound(3, 'uncertain'))
        assert store.reserve_steer(parent['id'], 3)
        store.recover_steering()
        assert store.turn_for(3)['executor'] is store.turn_for(3)['effort'] is None
        set_effort(store, 4, 'high')
        store.finish(parent['id'], 'completed')
        child, _ = store.claim(3, 3)
        assert child['effort'] == 'high'
    finally:
        store.close()


@pytest.mark.parametrize('explicit', [None, 'claude', 'codex'])
@pytest.mark.parametrize('active_executor', [None, 'claude', 'codex'])
def test_new_inherits_current_settings_only_for_same_executor(tmp_path, explicit, active_executor):
    store = Storage(tmp_path / 'state.db')
    try:
        if active_executor:
            row = store.journal(inbound(1, '/model chosen'))
            store.set_model(row['id'], active_executor, 'chosen')
            set_effort(store, 2, 'high')
        row = store.journal(inbound(3, '/new Title'))
        task = store.new_task(row['id'], explicit, 'Title')
        inherits = active_executor is not None and explicit in (None, active_executor)
        assert task['executor'] == (explicit or active_executor or 'claude')
        assert task['model'] == ('chosen' if inherits else None)
        assert task['effort'] == ('high' if inherits else None)
        assert task['title'] == 'Title'
        assert store.session(task['id'], task['executor']) is None
    finally:
        store.close()


def test_model_reset_and_targeted_effort_do_not_change_current_task(tmp_path):
    store = Storage(tmp_path / 'state.db')
    try:
        set_effort(store, 1, 'max')
        row = store.journal(inbound(2, '/new'))
        store.new_task(row['id'])
        row = store.journal(inbound(3, '/model codex model'))
        task, _ = store.set_model(row['id'], 'codex', 'model', 1, reset_effort=True)
        assert task['effort'] is None
        assert store.active_task(1)['id'] == 2
        row = store.journal(inbound(4, '/effort high'))
        task = store.set_effort(row['id'], 'high', 1)
        assert task['effort'] == 'high' and store.active_task(1)['effort'] == 'max'
        assert store.active_task(1)['id'] == 2
    finally:
        store.close()
