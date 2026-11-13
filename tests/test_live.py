"""Explicit opt-in only. Uses subscription Claude; never contacts Telegram."""
import asyncio
import json
import os
from pathlib import Path

import pytest

from conftest import FakeChannel, inbound, until
from eivoh.executors.claude import ClaudeExecutor, process_table
from eivoh.gateway import Gateway
from eivoh.storage import Storage


@pytest.mark.live
@pytest.mark.skipif(os.environ.get('EIVOH_LIVE') != '1', reason='Set EIVOH_LIVE=1 to run real Claude')
async def test_live_turn_resume_and_cancel(tmp_path):
    store = Storage(tmp_path / 'live.db')
    executor = ClaudeExecutor(tmp_path, os.environ.get('EIVOH_CLAUDE_BIN', 'claude'), model='haiku')
    channel = FakeChannel()
    gateway = Gateway(store, executor, {'fake': channel}, edit_interval=0.2)
    try:
        await gateway.start()
        await gateway.receive(inbound(1, 'Remember the word lantern. Reply only: lantern. Do not use tools.'))
        await asyncio.wait_for(gateway.idle(), 120)
        first = store.turn_for(1)
        assert first['status'] == 'completed' and first['session_id']
        await gateway.receive(inbound(2, 'What word did I ask you to remember? Reply only that word. Do not use tools.'))
        await asyncio.wait_for(gateway.idle(), 120)
        assert store.turn_for(2)['status'] == 'completed'
        assert store.turn_for(2)['session_id'] == first['session_id']
        final = store.db.execute("SELECT payload FROM raw_event WHERE turn_id=? AND kinds LIKE '%turn_completed%' ORDER BY sequence DESC LIMIT 1", (store.turn_for(2)['id'],)).fetchone()
        assert 'lantern' in json.loads(final[0])['result'].lower()
        await gateway.receive(inbound(3, 'Use the Bash tool to run exactly this command in the foreground and wait for it: `python3 -c "import time; time.sleep(61)"`. Do not run other commands.'))
        tracked = {}
        def sleeping():
            for tree in executor.running.values():
                table = tree.snapshot()
                tracked.update(tree.known)
                for pid, start in tree.known.items():
                    if pid in table and table[pid][1] == start:
                        try:
                            if Path(f'/proc/{pid}/comm').read_text().strip() == 'python3':
                                return True
                        except OSError:
                            pass
            return False
        await until(sleeping, timeout=120)
        await gateway.receive(inbound(4, '/stop'))
        await gateway.idle()
        assert store.turn_for(3)['status'] == 'cancelled'
        def no_orphans():
            table = process_table()
            return all(pid not in table or table[pid][1] != start for pid, start in tracked.items())
        await until(no_orphans)
    finally:
        await gateway.shutdown()
        store.close()
