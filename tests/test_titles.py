"""Task naming never participates in the task's conversation or delivery."""
import asyncio
import json
import sqlite3

import pytest

from conftest import FakeChannel, inbound, until
from test_tasks import HeldExecutor
from eivoh.executors.base import Event, Record
from eivoh.gateway import Gateway
from eivoh.storage import MIGRATIONS, Storage
from eivoh.titles import ClaudeTitleExecutor, CodexTitleExecutor, MODELS, valid_title


class NameExecutor(HeldExecutor):
    def __init__(self, outcome='success', held=False):
        super().__init__('title')
        self.outcome, self.held = outcome, held
        self.usage = []

    async def start(self, run_id, prompt, model=None):
        self.calls.append((run_id, prompt, model))
        self.releases[run_id] = asyncio.Event()
        yield Record(None, {}, (Event('session_started', {}, None, 'isolated-title-session'),))
        for data in self.usage:
            yield Record(None, {}, (Event('usage', data, None),))
        if self.held:
            await self.releases[run_id].wait()
        if self.outcome == 'exception':
            raise RuntimeError('sensitive auth failure must not appear in chat or journal')
        if self.outcome == 'error':
            yield Record(None, {}, (Event('error', {'message': '403 auth failed'}, None),))
        else:
            yield Record(None, {}, (Event('turn_completed', {'status': 'completed',
                         'text': 'Поиск по заметкам' if self.outcome == 'success' else 'invalid'}, None),))

    def resume(self, *args, **kwargs):
        raise AssertionError('Titles must not resume any session')


@pytest.fixture
async def naming(tmp_path):
    store = Storage(tmp_path / 'titles.db')
    normal, namer, channel = HeldExecutor(), NameExecutor(), FakeChannel()
    gateway = Gateway(store, normal, {'fake': channel}, edit_interval=0,
                      title_executors={'claude': namer, 'codex': namer})
    yield gateway, store, normal, namer, channel, tmp_path
    await gateway.shutdown()
    store.close()


async def complete(gateway, normal, mid, text='Найди заметки о проекте'):
    await gateway.receive(inbound(mid, text))
    await until(lambda: len(normal.calls) > 0 and normal.calls[-1][0] in normal.releases)
    turn_id = gateway.storage.turn_for(mid)['id']
    await until(lambda: turn_id in normal.releases)
    normal.releases[turn_id].set()
    await gateway.idle()


def outcomes(store):
    return [json.loads(row[0]) for row in store.db.execute("SELECT payload FROM raw_event WHERE stream='meta'")
            if json.loads(row[0]).get('type') == 'task_title']


@pytest.mark.parametrize('backend', ['claude', 'codex'])
async def test_names_use_explicit_backend_model_and_isolated_session(naming, backend):
    gateway, store, normal, namer, channel, _ = naming
    gateway.executors['codex'] = normal
    await gateway.receive(inbound(1, f'/new {backend}'))
    await complete(gateway, normal, 2)
    await until(lambda: not gateway.title_jobs)
    task = store.active_task(1)
    assert task['title'] == 'Поиск по заметкам' and task['title_source'] == 'generated'
    run_id, prompt, model = namer.calls[0]
    assert run_id == -task['id'] and model == MODELS[backend]
    assert 'Найди заметки о проекте' in prompt and 'Answer 1' in prompt
    assert store.session(task['id'], backend)['session_id'] == 'claude-1'
    assert store.turn_for(2)['session_id'] == 'claude-1'
    assert store.stored_result(1) == ('Answer 1', None)
    assert 'Поиск по заметкам' not in store.transcript(task['id'], 99)[0]
    assert not any('Поиск по заметкам' in text for _, text in channel.sent)
    records = outcomes(store)
    assert [r['outcome'] for r in records] == ['started', 'generated']
    assert all(r['title_executor'] == backend and r['title_model'] == MODELS[backend] for r in records)
    assert all('executor' not in r and 'model' not in r and 'title_usage' not in r for r in records)
    assert records[0]['first_inbound_id'] == 2 and records[0]['turn_id'] == 1
    await complete(gateway, normal, 3, 'followup')
    assert len(namer.calls) == 1 and normal.calls[-1][3] == 'claude-1'


@pytest.mark.parametrize('command', ['/new claude My explicit title', '/title My explicit title'])
async def test_explicit_title_never_starts_generation(naming, command):
    gateway, store, normal, namer, _, _ = naming
    await gateway.receive(inbound(1, '/new'))
    await gateway.receive(inbound(2, command))
    await complete(gateway, normal, 3)
    assert store.active_task(1)['title'] == 'My explicit title'
    assert not namer.calls and not outcomes(store)


async def test_manual_rename_wins_generation_race_and_response_does_not_wait(naming):
    gateway, store, normal, namer, channel, _ = naming
    namer.held = True
    await asyncio.wait_for(complete(gateway, normal, 1), 1)
    await until(lambda: namer.calls)
    displayed = [text for _, text in channel.sent] + [text for _, _, text in channel.edits]
    assert gateway.title_jobs and 'Answer 1' in displayed
    await gateway.receive(inbound(2, '/title Manually chosen name'))
    namer.releases[-1].set()
    await until(lambda: not gateway.title_jobs)
    assert store.task(1, 1)['title'] == 'Manually chosen name'
    assert outcomes(store)[-1]['outcome'] == 'superseded'


@pytest.mark.parametrize('outcome', ['error', 'exception', 'invalid'])
async def test_failure_is_quiet_and_not_retried(naming, outcome):
    gateway, store, normal, namer, channel, _ = naming
    namer.outcome = outcome
    await complete(gateway, normal, 1)
    await until(lambda: not gateway.title_jobs)
    assert store.task(1, 1)['title'] == 'Найди заметки о проекте'
    assert outcomes(store)[-1]['outcome'] in {'failed', 'invalid_or_failed'}
    await complete(gateway, normal, 2, 'try another message')
    assert len(namer.calls) == 1
    assert not any('failed' in text for _, text in channel.sent)
    assert 'sensitive auth' not in json.dumps(outcomes(store))


async def test_timeout_keeps_fallback_cancels_and_releases_slot(naming):
    gateway, store, normal, namer, _, _ = naming
    gateway.title_timeout, namer.held = 0.05, True
    await complete(gateway, normal, 1)
    await until(lambda: not gateway.title_jobs)
    assert outcomes(store)[-1]['outcome'] == 'timeout'
    assert -1 in namer.cancelled
    assert store.task(1, 1)['title_source'] == 'fallback'
    assert gateway.slots._value == 3


@pytest.mark.parametrize('command', ['/stop', '/close', '/stop all', 'shutdown'])
async def test_title_job_cancelled_with_task_and_never_retried_after_reopen(naming, command):
    gateway, store, normal, namer, _, _ = naming
    namer.held = True
    await complete(gateway, normal, 1)
    await until(lambda: namer.calls)
    if command == 'shutdown':
        await gateway.shutdown()
    else:
        await gateway.receive(inbound(2, command))
    assert not gateway.title_jobs and -1 in namer.cancelled
    assert outcomes(store)[-1]['outcome'] == 'cancelled'
    assert store.task(1, 1)['title_source'] == 'fallback'
    if command != 'shutdown':
        await gateway.receive(inbound(3, '/task 1'))
        await complete(gateway, normal, 4, 'continued task')
        assert len(namer.calls) == 1


async def test_title_waits_within_shared_limit_and_can_cancel_before_launch(naming):
    gateway, store, normal, namer, _, _ = naming
    gateway.slots = asyncio.Semaphore(1)
    # Another task is already waiting when first completes; it takes the slot.
    await gateway.receive(inbound(1, 'first request'))
    await until(lambda: normal.calls)
    await gateway.receive(inbound(2, '/new Second'))
    await gateway.receive(inbound(3, 'hold slot'))
    normal.releases[1].set()
    await until(lambda: len(normal.calls) == 2 and 1 in gateway.title_jobs)
    assert not namer.calls
    await gateway.receive(inbound(4, '/stop 1'))
    assert not namer.calls and outcomes(store)[-1]['outcome'] == 'cancelled'
    await gateway.receive(inbound(5, '/stop all'))


async def test_restart_preserves_generated_or_attempted_title(naming):
    gateway, store, normal, namer, channel, path = naming
    await complete(gateway, normal, 1)
    await until(lambda: not gateway.title_jobs)
    await gateway.shutdown()
    reopened = Storage(path / 'titles.db')
    replacement = Gateway(reopened, normal, {'fake': channel}, title_executors={'claude': namer})
    try:
        await replacement.start()
        assert reopened.task(1, 1)['title'] == 'Поиск по заметкам'
        assert len(namer.calls) == 1 and not replacement.title_jobs
    finally:
        await replacement.shutdown()
        reopened.close()


async def test_restart_does_not_repeat_an_attempt_interrupted_by_crash(naming):
    gateway, store, normal, namer, channel, path = naming
    gateway.title_executors = {}
    await complete(gateway, normal, 1)
    # Persist the attempt, as a process might immediately before a hard kill.
    assert store.claim_title(1, MODELS) is not None
    await gateway.shutdown()
    reopened = Storage(path / 'titles.db')
    replacement = Gateway(reopened, normal, {'fake': channel}, title_executors={'claude': namer})
    try:
        await replacement.start()
        assert reopened.task(1, 1)['title'] == 'Найди заметки о проекте'
        assert not namer.calls and not replacement.title_jobs
        assert [r['outcome'] for r in outcomes(reopened)] == ['started']
    finally:
        await replacement.shutdown()
        reopened.close()


async def test_restart_schedules_finished_turn_without_an_attempt_and_bounds_input(naming):
    gateway, store, normal, namer, _, _ = naming
    gateway.title_executors = {}
    # A failed first message still supplies the naming context on later success.
    row = store.journal(inbound(1, '[Voice message, speech-to-text]\n' + 'я' * 5000))
    turn, _ = store.claim(row['id'], 3)
    store.finish(turn['id'], 'failed', 'unavailable')
    await complete(gateway, normal, 2, 'second message')
    gateway.title_executors = {'claude': namer}
    await gateway.start()
    await until(lambda: not gateway.title_jobs)
    prompt = namer.calls[0][1]
    assert 'я' * 4000 in prompt and 'я' * 4001 not in prompt
    assert 'Voice message' not in prompt and 'second message' not in prompt
    assert outcomes(store)[0]['turn_id'] == 2


async def test_failed_normal_turn_does_not_start_or_consume_title_attempt(naming):
    gateway, store, normal, namer, _, _ = naming
    gateway.executor = NameExecutor(outcome='error')
    await gateway.receive(inbound(1, 'first request failed'))
    await gateway.idle()
    assert store.turn_for(1)['status'] == 'failed'
    assert not namer.calls and not outcomes(store)
    assert store.task(1, 1)['title_attempted'] == 0
    gateway.executor = normal
    await complete(gateway, normal, 2, 'successful retry')
    await until(lambda: not gateway.title_jobs)
    assert 'first request failed' in namer.calls[0][1]
    assert outcomes(store)[0]['turn_id'] == 2


async def test_model_switch_during_turn_does_not_change_title_backend(naming):
    gateway, store, normal, namer, _, _ = naming
    await gateway.receive(inbound(1, 'first request'))
    await until(lambda: normal.calls)
    command = store.journal(inbound(2, '/model gpt-6-luna'))
    store.set_model(command['id'], 'codex', 'gpt-6-luna')
    normal.releases[1].set()
    await gateway.idle()
    await until(lambda: not gateway.title_jobs)
    assert store.task(1, 1)['executor'] == 'codex'
    assert namer.calls[0][2] == 'haiku'
    assert outcomes(store)[0]['title_executor'] == 'claude'


@pytest.mark.parametrize('backend', ['claude', 'codex'])
@pytest.mark.parametrize('ending', ['success', 'error', 'cancel', 'timeout'])
async def test_observed_title_usage_keeps_identity_and_survives_failure(naming, backend, ending):
    from eivoh.executors.claude import parse_line
    from eivoh.executors.codex import CodexParser
    gateway, store, normal, namer, _, _ = naming
    if backend == 'claude':
        record = parse_line(json.dumps({'type': 'result', 'is_error': False,
                            'usage': {'input_tokens': 123, 'output_tokens': 7}, 'total_cost_usd': 0.002}))
    else:
        record = CodexParser()(json.dumps({'type': 'turn.completed',
                              'usage': {'input_tokens': 123, 'cached_input_tokens': 100, 'output_tokens': 7}}))
    observed = next(event.data for event in record.events if event.kind == 'usage')
    # Snapshots must not be added together or confused with the main turn usage.
    namer.usage = [{'usage': {'input_tokens': 100}}, observed]
    namer.outcome = 'error' if ending == 'error' else 'success'
    namer.held = ending in {'cancel', 'timeout'}
    gateway.title_timeout = 0.1
    gateway.executors['codex'] = normal
    await gateway.receive(inbound(1, f'/new {backend}'))
    await complete(gateway, normal, 2)
    if ending == 'cancel':
        await until(lambda: namer.calls)
        await gateway.receive(inbound(3, '/stop'))
    await until(lambda: not gateway.title_jobs)
    events = outcomes(store)
    assert 'title_usage' not in events[0]
    assert events[-1]['title_usage'] == observed
    assert events[-1]['title_executor'] == backend and events[-1]['title_model'] == MODELS[backend]
    assert events[-1]['outcome'] == {'success': 'generated', 'error': 'invalid_or_failed',
                                    'cancel': 'cancelled', 'timeout': 'timeout'}[ending]
    assert store.stored_result(1) == ('Answer 1', None)
    # No naming usage/session/result becomes a normalized main-turn event.
    normalized = [event for row in store.db.execute('SELECT events FROM raw_event') for event in json.loads(row[0])]
    assert not any(event['kind'] == 'usage' for event in normalized)


@pytest.mark.parametrize('state', ['manual', 'generated', 'attempted', 'closed'])
async def test_ineligible_title_does_not_parse_stored_turns(naming, monkeypatch, state):
    gateway, store, normal, _, _, _ = naming
    gateway.title_executors = {}
    await complete(gateway, normal, 1)
    if state in {'manual', 'generated'}:
        store.db.execute('UPDATE task SET title_source=? WHERE id=1', (state,))
    elif state == 'attempted':
        store.db.execute('UPDATE task SET title_attempted=1 WHERE id=1')
    else:
        store.db.execute("UPDATE task SET status='closed' WHERE id=1")
    def forbidden_parse(turn_id):
        raise AssertionError('Ineligible tasks must not parse their raw event history')
    monkeypatch.setattr(store, 'stored_result', forbidden_parse)
    assert store.claim_title(1, MODELS) is None


async def test_empty_completed_answer_does_not_block_later_text_answer(naming):
    gateway, store, normal, namer, _, _ = naming
    row = store.journal(inbound(1, 'original naming context'))
    turn, _ = store.claim(row['id'], 3)
    store.append(turn['id'], Record(None, {}, (Event('turn_completed', {'status': 'completed', 'text': ''}, None),)))
    store.finish(turn['id'], 'completed')
    await gateway.start()
    assert not namer.calls and not store.task(1, 1)['title_attempted']
    command = store.journal(inbound(2, '/model gpt-6-luna'))
    store.set_model(command['id'], 'codex', 'gpt-6-luna')
    gateway.executors['codex'] = normal
    await complete(gateway, normal, 3, 'later success')
    await until(lambda: not gateway.title_jobs)
    assert len(namer.calls) == 1 and namer.calls[0][2] == 'gpt-6-luna'
    assert 'original naming context' in namer.calls[0][1]
    assert outcomes(store)[0]['turn_id'] == 2


def test_title_prompt_only_uses_bounded_beginnings():
    from eivoh.titles import prompt
    text = prompt('u' * 4000 + 'SECRET_USER_TAIL', 'a' * 2000 + 'SECRET_ANSWER_TAIL')
    assert 'u' * 4000 in text and 'a' * 2000 in text
    assert 'SECRET_USER_TAIL' not in text and 'SECRET_ANSWER_TAIL' not in text


def test_migration_preserves_all_existing_title_provenance(tmp_path):
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:3]:
            for statement in migration:
                db.execute(statement)
        db.execute('PRAGMA user_version=3')
        db.execute("INSERT INTO conversation VALUES (1,'fake','10',NULL)")
        for title in ('explicit title', 'looks like first message', None):
            db.execute('INSERT INTO task(conversation_id,title) VALUES (1,?)', (title,))
    store = Storage(path)
    try:
        tasks = store.db.execute('SELECT title,title_source FROM task ORDER BY id').fetchall()
        assert [tuple(t) for t in tasks] == [('explicit title', 'manual'),
                                            ('looks like first message', 'manual'), (None, 'manual')]
        assert not store.db.execute('PRAGMA foreign_key_check').fetchall()
    finally:
        store.close()


@pytest.mark.parametrize('text', ['one two', 'one two three four five six seven', 'one\ntwo three',
                                 'one\u202etwo three four', '# one two three', 'x' * 121 + ' two three'])
def test_invalid_title_output(text):
    assert valid_title(text) is None


def test_disposable_title_cli_has_no_task_authority(tmp_path):
    claude = ClaudeTitleExecutor(tmp_path, extra_args=['--tools', 'Bash'])
    codex = CodexTitleExecutor(tmp_path, extra_args=['--dangerously-bypass-approvals-and-sandbox'])
    cc = claude.command('never-resume', 'opus')
    cx = codex.command('never-resume', 'gpt-large')
    assert '--resume' not in cc and 'resume' not in cx and 'never-resume' not in cc + cx
    assert cc[cc.index('--model') + 1] == 'haiku' and cc[cc.index('--tools') + 1] == ''
    assert '--safe-mode' in cc and '--no-session-persistence' in cc and '--strict-mcp-config' in cc
    assert cx[cx.index('-m') + 1] == 'gpt-6-luna' and '--ephemeral' in cx
    assert '--ignore-user-config' in cx and '--strict-config' in cx
    assert cx[cx.index('--sandbox') + 1] == 'read-only'
    assert '--dangerously-bypass-approvals-and-sandbox' not in cx
    assert 'approval_policy="never"' in cx and 'web_search="disabled"' in cx
    assert set(cx[i + 1] for i, item in enumerate(cx) if item == '--disable') >= {
        'shell_tool', 'unified_exec', 'multi_agent', 'apps', 'plugins', 'skill_search',
        'image_generation', 'browser_use', 'computer_use', 'view_image', 'code_mode', 'code_mode_host'}
