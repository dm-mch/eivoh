import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from telegram.error import Conflict

from eivoh.channels.base import Inbound
from eivoh.channels.locking import ConsumerConflict, TokenLock
from eivoh.channels.telegram import TelegramChannel
from eivoh.storage import Storage
from test_telegram import make_bot


def test_same_token_process_lock_across_state_directories_and_crash(tmp_path):
    token = 'synthetic-' + uuid.uuid4().hex
    script = ('import sys,time; from eivoh.channels.locking import TokenLock; '
              'lock=TokenLock(sys.argv[1]); lock.__enter__(); print("locked",flush=True); time.sleep(60)')
    process = subprocess.Popen([sys.executable, '-c', script, token], stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'locked'
        # Lock identity has no state_dir parameter: separate DBs cannot create
        # competing consumers. A failed contender leaves the holder's inode.
        for _ in range(2):
            with pytest.raises(ConsumerConflict, match='already consuming'):
                with TokenLock(token):
                    pass
        with TokenLock(token + '-other'):
            pass
        process.kill()
        process.wait(timeout=5)
        with TokenLock(token):
            pass
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def update(uid, mid, text=None, photo=False):
    return SimpleNamespace(update_id=uid, effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=10, type='private'),
        effective_message=SimpleNamespace(message_id=mid, text=text, caption='/stop as a caption',
            photo=[SimpleNamespace(file_id='image', file_size=10)] if photo else [], audio=None, voice=None))


async def test_poll_journals_media_before_download_and_redelivery(tmp_path):
    storage = Storage(tmp_path / 'state.db')
    bot = make_bot()
    bot.get_file = AsyncMock()
    channel = TelegramChannel('unused', 1, bot)
    channel.bind(storage)
    incoming = update(30, 1, photo=True)
    bot.get_updates = AsyncMock(side_effect=[[incoming, incoming], asyncio.CancelledError()])
    async def receive(message):
        storage.journal(message)
        return True
    with pytest.raises(asyncio.CancelledError):
        await channel.poll(receive)
    assert bot.get_updates.call_args.kwargs['offset'] == 31
    assert storage.inbound(1)['preparation'] == 'pending'
    assert storage.turn_for(1) is not None
    assert bot.get_file.await_count == 0
    assert storage.db.execute('SELECT COUNT(*) FROM inbound').fetchone()[0] == 1
    storage.close()


async def test_shutdown_refusal_does_not_ack_update(tmp_path):
    storage = Storage(tmp_path / 'state.db')
    bot = make_bot()
    bot.get_updates = AsyncMock(return_value=[update(30, 1, 'hello')])
    channel = TelegramChannel('unused', 1, bot)
    channel.bind(storage)
    await channel.poll(AsyncMock(return_value=False))
    assert storage.cursor('telegram') is None and not storage.saw_update('telegram', 30)
    storage.close()


async def test_cursor_expires_in_running_loop_and_old_id_is_not_forever_seen(tmp_path):
    storage = Storage(tmp_path / 'state.db')
    storage.advance_cursor('telegram', 900)
    bot = make_bot()
    calls = []
    async def updates(**kwargs):
        calls.append(kwargs['offset'])
        if len(calls) == 1:
            assert kwargs['offset'] == 900
            expired = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(sep=' ')
            with storage.db:
                storage.db.execute('UPDATE poll_cursor SET updated_at=?', (expired,))
                storage.db.execute("INSERT INTO update_receipt VALUES ('telegram',20,?)", (expired,))
            return []
        if len(calls) == 2:
            assert kwargs['offset'] is None
            return [update(20, 1, 'after idle')]
        raise asyncio.CancelledError
    bot.get_updates = updates
    channel = TelegramChannel('unused', 1, bot)
    channel.bind(storage)
    async def receive(message):
        storage.journal(message)
        return True
    with pytest.raises(asyncio.CancelledError):
        await channel.poll(receive)
    assert calls == [900, None, 21] and storage.inbound(1)['text'] == 'after idle'
    storage.close()


async def test_remote_conflicts_backoff_survive_single_success(monkeypatch):
    bot = make_bot()
    bot.get_updates = AsyncMock(side_effect=[Conflict('other'), [], Conflict('other'), [],
        Conflict('other'), [], Conflict('other'), [], Conflict('other')])
    pauses = []
    async def sleep(delay):
        pauses.append(delay)
    monkeypatch.setattr('eivoh.channels.telegram.asyncio.sleep', sleep)
    with pytest.raises(ConsumerConflict, match='repeatedly conflicts'):
        await TelegramChannel('unused', 1, bot).poll(AsyncMock())
    assert pauses == [10, 20, 30, 45]
    assert bot.get_updates.await_count == 9


async def test_lock_happens_before_database_and_network(tmp_path, monkeypatch):
    import eivoh.__main__ as main
    token = 'synthetic-' + uuid.uuid4().hex
    touched = []
    monkeypatch.setattr(main, 'Storage', lambda *args: touched.append('database'))
    with TokenLock(token):
        with pytest.raises(ConsumerConflict):
            await main.serve(SimpleNamespace(telegram_token=token))
    assert touched == []
