import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from conftest import until
from eivoh.attachments import (AttachmentError, download_file, input_manifest, output_directory,
                               snapshot_outputs, visible_text)
from eivoh.channels.base import Inbound
from eivoh.channels.telegram import TelegramChannel
from eivoh.gateway import Gateway
from eivoh.storage import Storage
from test_tasks import HeldExecutor
from test_telegram import make_bot


def media(mid, caption='', reply_to=None, kind='document'):
    return Inbound('telegram', '10', '1', str(mid), caption, reply_to,
                   attachments=({'kind': kind, 'file_id': f'file{mid}', 'name': '../../note.txt', 'size': 3},))


def test_final_markers_snapshots_and_paths(tmp_path):
    root = output_directory(tmp_path, 3)
    (root / 'report.txt').write_bytes(b'original')
    outside = tmp_path / 'secret.txt'
    outside.write_text('not uploaded')
    (root / 'linked.txt').symlink_to(outside)
    os.link(outside, root / 'hard.txt')
    (root / 'dir').symlink_to(tmp_path, target_is_directory=True)
    text = '\n'.join(['Result', 'EIVOH_ARTIFACT {"path":"report.txt","kind":"document"}',
        'EIVOH_ARTIFACT {"path":"report.txt","kind":"document"}',
        *[f'EIVOH_ARTIFACT {json.dumps({"path": path, "kind": "document"})}'
          for path in ['../secret.txt', str(outside), 'linked.txt', 'hard.txt', 'dir/secret.txt']]])
    clean, files = snapshot_outputs(tmp_path, 3, text)
    assert len(files) == 1 and 'EIVOH_ARTIFACT' not in clean and 'File delivery:' in clean
    assert Path(files[0]['path']).read_bytes() == b'original'
    (root / 'report.txt').write_bytes(b'changed')
    assert Path(files[0]['path']).read_bytes() == b'original'
    assert files[0]['sha256'] == hashlib.sha256(b'original').hexdigest()


def test_stream_hides_split_marker_but_preserves_fenced_example(tmp_path):
    for length in range(1, len('EIVOH_ARTIFACT ')):
        assert visible_text('Answer\n' + 'EIVOH_ARTIFACT '[:length]) == 'Answer'
    assert visible_text('Working ') == 'Working '
    assert visible_text('E', final=True) == 'E'
    example = 'Example:\n```\nEIVOH_ARTIFACT {"path":"file.txt","kind":"document"}\n```'
    assert visible_text(example) == example
    assert snapshot_outputs(tmp_path, 1, example) == (example, [])


async def test_download_stream_limit_and_cancellation_cleanup(tmp_path, monkeypatch):
    import eivoh.attachments as module
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b'0123456789'))
    monkeypatch.setattr(module.httpx, 'AsyncClient', lambda **kwargs: original(transport=transport, **kwargs))
    destination = tmp_path / 'file.bin'
    with pytest.raises(AttachmentError):
        await download_file('https://example.test/file', destination, limit=4)
    assert not list(tmp_path.iterdir())

    await download_file('https://example.test/file', destination, limit=10)
    assert destination.read_bytes() == b'0123456789'
    destination.unlink()
    waiting = asyncio.Event()
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'a'
            waiting.set()
            await asyncio.Event().wait()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream()))
    job = asyncio.create_task(download_file('https://example.test/file', destination))
    await waiting.wait()
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert not list(tmp_path.iterdir())


async def test_download_actual_bytes_limit_without_content_length(tmp_path, monkeypatch):
    import eivoh.attachments as module
    original = httpx.AsyncClient
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'a' * 65536
            yield b'b' * 65536
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream()))
    monkeypatch.setattr(module.httpx, 'AsyncClient', lambda **kwargs: original(transport=transport, **kwargs))
    with pytest.raises(AttachmentError, match='download limit'):
        await download_file('https://example.test/file', tmp_path / 'file.bin', limit=70000)
    assert not list(tmp_path.iterdir())


def test_output_count_size_and_total_limits(tmp_path, monkeypatch):
    import eivoh.attachments as module
    root = output_directory(tmp_path, 1)
    (root / 'large').write_bytes(b'x' * 9)
    (root / 'one').write_bytes(b'1234')
    (root / 'two').write_bytes(b'1234')
    (root / 'three').write_bytes(b'1234')
    markers = lambda names: '\n'.join('EIVOH_ARTIFACT ' + json.dumps({'path': name, 'kind': 'document'}) for name in names)
    monkeypatch.setattr(module, 'OUTPUT_LIMIT', 8)
    monkeypatch.setattr(module, 'TOTAL_OUTPUT_LIMIT', 7)
    text, items = snapshot_outputs(tmp_path, 1, markers(['large', 'one', 'two']))
    assert [item['name'] for item in items] == ['one'] and 'File delivery:' in text
    monkeypatch.setattr(module, 'TOTAL_OUTPUT_LIMIT', 100)
    monkeypatch.setattr(module, 'MAX_FILES', 2)
    text, items = snapshot_outputs(tmp_path, 1, markers(['one', 'two', 'three']))
    assert [item['name'] for item in items] == ['one', 'two'] and 'File delivery:' in text


async def test_media_is_durable_before_preparation_stop_and_caption_command(tmp_path):
    bot = make_bot()
    channel = TelegramChannel('unused', 1, bot)
    storage = Storage(tmp_path / 'state.db')
    executor = HeldExecutor()
    gateway = Gateway(storage, executor, {'telegram': channel}, edit_interval=0)
    entered, release = asyncio.Event(), asyncio.Event()
    async def prepare(row):
        entered.set()
        await release.wait()
        return row['text'] + '\nprepared', json.loads(row['attachments'])
    channel.prepare = prepare
    try:
        await gateway.receive(media(1, '/stop'))
        await entered.wait()
        assert storage.turn_for(1)['task_id'] == 1
        assert storage.inbound(1)['preparation'] == 'pending'
        assert not executor.calls
        await gateway.receive(Inbound('telegram', '10', '1', '2', '/new other'))
        assert storage.active_task(1)['id'] == 2
        await asyncio.wait_for(gateway.receive(Inbound('telegram', '10', '1', '3', '/stop 1')), 1)
        assert storage.turn_for(1)['status'] == 'cancelled'
        assert not gateway.media_jobs
        release.set()
        await asyncio.sleep(0)
        assert storage.inbound(1)['prepared_text'] is None and not executor.calls
    finally:
        await gateway.shutdown()
        storage.close()


async def test_prepared_media_restart_original_route_and_no_redownload(tmp_path, monkeypatch):
    bot = make_bot()
    bot.get_file = AsyncMock(return_value=SimpleNamespace(file_path='https://example.test/file', file_size=3))
    downloads = []
    async def download(url, path):
        downloads.append(url)
        path.write_bytes(b'abc')
    monkeypatch.setattr('eivoh.channels.telegram.download_file', download)
    path = tmp_path / 'state.db'
    storage = Storage(path)
    original = storage.journal(media(1, '/new is a caption'))
    new = storage.journal(Inbound('telegram', '10', '1', '2', '/new other'))
    storage.new_task(new['id'])
    storage.close()
    storage = Storage(path)
    channel = TelegramChannel('unused', 1, bot)
    channel.bind(storage)
    text, items = await channel.prepare(storage.inbound(original['id']))
    assert Path(items[0]['path']).parent == tmp_path / 'files' / 'inputs' / '1'
    assert items[0]['name'] == '../../note.txt'
    assert '/new is a caption' in text
    storage.prepared(1, text, items)
    # A restart after atomic file publication but before preparation commit uses
    # the stored bytes rather than spending another download/STT input request.
    await channel.prepare(storage.inbound(1))
    assert len(downloads) == 1
    assert storage.turn_for(1)['task_id'] == 1 and storage.active_task(1)['id'] == 2
    assert storage.input_text(1) == text
    storage.close()


async def test_voice_transcript_saved_before_echo_and_duplicate(tmp_path, monkeypatch):
    bot = make_bot()
    bot.get_file = AsyncMock(return_value=SimpleNamespace(file_path='https://example.test/voice', file_size=3))
    async def download(url, path):
        path.write_bytes(b'ogg')
    monkeypatch.setattr('eivoh.channels.telegram.download_file', download)
    transcriber = SimpleNamespace(transcribe=AsyncMock(return_value='spoken request'))
    channel = TelegramChannel('unused', 1, bot, transcriber)
    storage = Storage(tmp_path / 'voice.db')
    executor = HeldExecutor()
    gateway = Gateway(storage, executor, {'telegram': channel}, edit_interval=0)
    try:
        message = media(1, kind='voice')
        await gateway.receive(message)
        await gateway.receive(message)
        await until(lambda: storage.inbound(1)['preparation'] == 'ready')
        assert storage.inbound(1)['prepared_text'].endswith('spoken request')
        assert bot.get_file.await_count == transcriber.transcribe.await_count == 1
        await until(lambda: any('🎤' in call.kwargs.get('text', '') for call in bot.send_message.call_args_list))
    finally:
        await gateway.shutdown()
        storage.close()


def test_handoff_retains_input_and_generated_output_paths(tmp_path):
    from eivoh.executors.base import Event, Record
    storage = Storage(tmp_path / 'history.db')
    row = storage.journal(media(1, 'read and edit'))
    incoming = tmp_path / 'input.txt'
    incoming.write_text('input')
    manifest = [{'name': 'input.txt', 'path': str(incoming), 'kind': 'document'}]
    storage.prepared(row['id'], row['text'] + input_manifest(manifest), manifest)
    turn, _ = storage.claim(row['id'], 3)
    original = 'Done\nEIVOH_ARTIFACT {"path":"report.txt","kind":"document"}'
    storage.append(turn['id'], Record(None, {}, (Event('turn_completed', {'status': 'completed', 'text': original}, None),)))
    root = output_directory(tmp_path, turn['id'])
    (root / 'report.txt').write_text('output')
    text, files = snapshot_outputs(tmp_path, turn['id'], original)
    storage.save_output(turn['id'], text, files)
    storage.finish(turn['id'], 'completed')
    transcript, _, _ = storage.missed_transcript(1, 'codex', 99)
    assert str(incoming) in transcript and files[0]['path'] in transcript
    assert 'EIVOH_ARTIFACT' not in transcript and 'not a delivery acknowledgement' in transcript
    assert 'current turn output directory' in transcript
    assert storage.stored_result(turn['id'])[0] == original
    storage.close()
