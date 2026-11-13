import asyncio
import sys
from pathlib import Path

import pytest

from eivoh.channels.base import Inbound
from eivoh.executors.claude import ClaudeExecutor
from eivoh.gateway import Gateway
from eivoh.storage import Storage


class FakeChannel:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.typings = []

    async def send(self, chat_id, text):
        self.sent.append((chat_id, text))
        return str(len(self.sent))

    async def edit(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))

    async def typing(self, chat_id):
        self.typings.append(chat_id)


def inbound(mid, text, chat="10"):
    return Inbound("fake", chat, "1", str(mid), text)


async def until(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.fixture
async def rig(tmp_path):
    source = Path(__file__).parent / "fixtures" / "fake_claude.py"
    binary = tmp_path / "fake-claude"
    binary.write_text("#!" + sys.executable + "\n" + source.read_text().split("\n", 1)[1])
    binary.chmod(0o700)
    storage = Storage(tmp_path / "state.db")
    executor = ClaudeExecutor(tmp_path, binary.resolve(), grace=0.1)
    channel = FakeChannel()
    gateway = Gateway(storage, executor, {"fake": channel}, edit_interval=0.03, typing_interval=0.03)
    await gateway.start()
    yield gateway, storage, executor, channel, tmp_path
    await gateway.shutdown()
    storage.close()
