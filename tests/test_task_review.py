"""Regressions from the review of parallel tasks (2026-11-13)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from eivoh.channels.base import Inbound
from eivoh.channels.formatting import plain_text, render_chunks
from eivoh.channels.telegram import TelegramChannel
from eivoh.executors.base import Event, Record
from eivoh.executors.claude import ClaudeExecutor
from eivoh.gateway import Gateway
from eivoh.storage import Storage


def msg(mid, text, chat='10', reply_to=None, channel='fake'):
    return Inbound(channel, chat, '1', str(mid), text, reply_to)


class Channel:
    def __init__(self):
        self.sent = []
    async def send(self, chat, text, buttons=None):
        self.sent.append((chat, text))
        return f'bot-{len(self.sent)}'
    async def edit(self, *args):
        pass
    async def typing(self, *args):
        pass


class Immediate:
    def __init__(self, answer='answer'):
        self.calls = []
        self.answer = answer
    async def start(self, run, prompt, model=None, session_id=None, effort=None):
        self.calls.append((run, prompt, session_id))
        yield Record(None, {}, (Event('session_started', {}, None, session_id or f'session-{run}'),))
        yield Record(None, {}, (Event('turn_completed', {'status':'completed', 'text':self.answer}, None),))
    def resume(self, run, session_id, prompt, model=None, effort=None):
        return self.start(run, prompt, model, session_id, effort=effort)
    async def cancel(self, run):
        pass
    async def shutdown(self):
        pass


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.001)


def seed_turn(store, mid, text, sid):
    row = store.journal(msg(mid, text))
    turn, _ = store.claim(row['id'], 3)
    store.append(turn['id'], Record(None, {}, (Event('session_started', {}, None, sid),
        Event('turn_completed', {'status':'completed', 'text':f'answer to {text}'}, None))))
    store.finish(turn['id'], 'completed')
    return turn


def change_executor(store, mid, name):
    row = store.journal(msg(mid, '/model ' + name))
    store.set_model(row['id'], name, None)


async def test_failed_launch_keeps_missed_history_for_the_next_turn(tmp_path):
    store = Storage(tmp_path/'state.db')
    seed_turn(store, 1, 'claude baseline', 'claude-original')
    change_executor(store, 2, 'codex')
    seed_turn(store, 3, 'IMPORTANT CODEX DECISION', 'codex-original')
    change_executor(store, 4, 'claude')
    executor = ClaudeExecutor(tmp_path, tmp_path/'nonexistent-executor', grace=.01)
    gateway = Gateway(store, executor, {'fake': Channel()}, edit_interval=0)
    try:
        assert store.missed_transcript(1, 'claude', 5)[1] == 1
        await gateway.receive(msg(5, 'return to Claude'))
        await asyncio.wait_for(gateway.idle(), 5)
        failed = store.turn_for(5)
        assert failed['status'] == 'failed' and failed['session_id'] is None
        assert store.session(1, 'claude')['synced_inbound_id'] == 1
        next_row = store.journal(msg(6, 'try again'))
        next_turn, _ = store.claim(next_row['id'], 3)
        next_prompt = store.prepare_prompt(next_turn, next_row['text'])
        assert next_turn['resume_id'] == 'claude-original'
        assert 'IMPORTANT CODEX DECISION' in next_prompt and next_prompt.endswith('try again')
    finally:
        await gateway.shutdown()
        store.close()


async def test_reply_to_a_later_part_of_a_long_answer_finds_its_task(tmp_path):
    store = Storage(tmp_path/'state.db')
    counter = 100
    async def send(**kwargs):
        nonlocal counter
        counter += 1
        return SimpleNamespace(message_id=counter)
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=send),
        edit_message_text=AsyncMock(), delete_message=AsyncMock(), send_chat_action=AsyncMock())
    channel = TelegramChannel('unused', 1, bot)
    gateway = Gateway(store, Immediate('x'*9000), {'telegram': channel}, edit_interval=0)
    try:
        await gateway.receive(msg(1, 'first', channel='telegram'))
        await asyncio.wait_for(gateway.idle(), 5)
        chunks = channel.messages[('10', '101')]
        assert len(chunks) == 3
        assert store.reply_task(1, chunks[0])['id'] == 1
        assert store.reply_task(1, chunks[-1])['id'] == 1
        assert store.db.execute('SELECT turn_id FROM outbound WHERE message_id=?', (chunks[-1],)).fetchone()[0] == store.turn_for(1)['id']
        await gateway.receive(msg(2, '/new Second', channel='telegram'))
        await gateway.receive(msg(3, 'follow up to first', reply_to=chunks[-1], channel='telegram'))
        assert store.turn_for(3)['task_id'] == 1
    finally:
        await gateway.shutdown()
        store.close()


async def test_final_delivery_does_not_hold_the_executor_slot(tmp_path):
    store = Storage(tmp_path/'state.db')
    entered, release = asyncio.Event(), asyncio.Event()
    class BlockingChannel(Channel):
        async def send(self, chat, text, buttons=None):
            if chat == '10':
                entered.set()
                await release.wait()
            return await super().send(chat, text, buttons)
    executor = Immediate()
    gateway = Gateway(store, executor, {'fake': BlockingChannel()}, edit_interval=0, max_parallel=1)
    try:
        await gateway.receive(msg(1, 'first'))
        await entered.wait()
        assert store.turn_for(1)['status'] == 'completed'
        await gateway.receive(msg(2, 'second', chat='20'))
        await until(lambda: len(executor.calls) == 2)  # While the first answer is still being sent.
        release.set()
        await asyncio.wait_for(gateway.idle(), 5)
        assert gateway.slots._value == 1
    finally:
        release.set()
        await gateway.shutdown()
        store.close()


async def test_task_list_shows_titles_literally(tmp_path):
    store = Storage(tmp_path/'state.db')
    channel = Channel()
    gateway = Gateway(store, Immediate(), {'fake': channel}, edit_interval=0)
    try:
        await gateway.receive(msg(1, '/new __init__'))
        await gateway.receive(msg(2, '/tasks'))
        rendered = ''.join(plain_text(c) for c in render_chunks(channel.sent[-1][1]))
        assert '__init__' in rendered
    finally:
        await gateway.shutdown()
        store.close()
