"""Telegram's durable physical sends. Unknown initial writes are never replayed."""
import asyncio
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import stat

import httpx

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError

from .formatting import plain_text, render_chunks


def checked_snapshot(payload):
    """Open and hash on a worker thread; the returned handle owns that inode."""
    blob = os.fdopen(os.open(payload['path'], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb')
    try:
        info = os.fstat(blob.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != payload['size']:
            raise ValueError('Snapshot unavailable')
        digest = hashlib.sha256()
        while chunk := blob.read(64 * 1024):
            digest.update(chunk)
        if digest.hexdigest() != payload['sha256']:
            raise ValueError('Snapshot changed')
        blob.seek(0)
        return blob
    except BaseException:
        blob.close()
        raise


async def open_snapshot(payload):
    job = asyncio.create_task(asyncio.to_thread(checked_snapshot, payload))
    try:
        return await asyncio.shield(job)
    except asyncio.CancelledError:
        # The finite hash read may finish after /stop. Its worker owns the
        # handle until then; never close it concurrently with that read.
        def close_finished(done):
            if not done.cancelled() and done.exception() is None:
                done.result().close()
        job.add_done_callback(close_finished)
        raise


class TelegramDelivery:
    def __init__(self, channel, storage):
        self.channel, self.storage = channel, storage

    async def text(self, chat, logical, text, task_id=None, turn_id=None, buttons=None):
        self.prepare_text(chat, logical, text, task_id, turn_id, buttons)
        await self.flush(chat, logical)
        rows = self.storage.delivery_parts('telegram', chat, logical)
        return next((row['message_id'] for row in rows if row['message_id']), None)

    def prepare_text(self, chat, logical, text, task_id=None, turn_id=None, buttons=None, *, commit=True):
        """Save desired content without waiting for an older physical request."""
        chunks = render_chunks(text)
        for index, chunk in enumerate(chunks):
            payload = {'kind': 'text', 'text': chunk, 'parse_mode': 'HTML'}
            if buttons and index == len(chunks) - 1:
                payload['buttons'] = buttons
            self.storage.prepare_delivery('telegram', chat, logical, str(index), payload, task_id, turn_id, commit=commit)
        for row in self.storage.delivery_parts('telegram', chat, logical):
            if row['part'].isdecimal() and int(row['part']) >= len(chunks):
                self.storage.prepare_delivery('telegram', chat, logical, row['part'], {'kind': 'delete'}, task_id, turn_id, commit=commit)

    def prepare_final(self, chat, progress, final, text, task_id, turn_id):
        """One final identity, adopting confirmed preview ids where available.

        An uncertain preview can already contain the whole final part (a fast
        executor, or a plaintext formatting fallback). Such a part stays unknown;
        only positively distinct preview content permits a fresh final send.
        """
        with self.storage.db:
            self._prepare_final(chat, progress, final, text, task_id, turn_id)

    def _prepare_final(self, chat, progress, final, text, task_id, turn_id):
        existing = {row['part'] for row in self.storage.delivery_parts('telegram', chat, final)}
        self.prepare_text(chat, final, text, task_id, turn_id, commit=False)
        previews = {row['part']: row for row in self.storage.delivery_parts('telegram', chat, progress)}
        uncertain = []
        missing_evidence = False
        for preview in previews.values():
            if preview['state'] not in {'sending', 'unknown'} or preview['message_id'] is not None:
                continue
            attempts = self.storage.db.execute("SELECT payload FROM delivery_attempt WHERE delivery_id=? AND operation='send' "
                "AND state IN ('sending','unknown')", (preview['id'],)).fetchall()
            missing_evidence |= not attempts
            uncertain.extend(self._content(json.loads(item['payload']).get('text', '')) for item in attempts)
        finals = self.storage.delivery_parts('telegram', chat, final)
        for row in finals:
            if row['part'] in existing:
                continue
            target = self._content(json.loads(row['payload']).get('text', ''))
            # The final answer may replace/reflow a longer preview, so an
            # uncertain different-index part can already contain this answer.
            if missing_evidence or any(target and target in body for body in uncertain):
                self.storage.db.execute("UPDATE delivery SET state='unknown',detail='An earlier send may already contain this final part' WHERE id=?", (row['id'],))
            elif row['part'] in previews and previews[row['part']]['message_id'] is not None:
                self.storage.db.execute("UPDATE delivery SET message_id=? WHERE id=?", (previews[row['part']]['message_id'], row['id']))
        for part, preview in previews.items():
            if part not in {row['part'] for row in finals} and preview['message_id'] is not None:
                row = self.storage.prepare_delivery('telegram', chat, final, part, {'kind': 'delete'}, task_id, turn_id, commit=False)
                if part not in existing:
                    self.storage.db.execute('UPDATE delivery SET message_id=? WHERE id=?', (preview['message_id'], row['id']))

    @staticmethod
    def _content(text):
        text = plain_text(text).strip()
        # Task labels are display context and may change while a task runs.
        return re.sub(r'^#t\d+ [^\n]*\n', '', text).strip()

    async def files(self, chat, logical, artifacts, task_id, turn_id):
        for index, item in enumerate(artifacts):
            self.storage.prepare_delivery('telegram', chat, logical, f'file:{index}', dict(item), task_id, turn_id)
        await self.flush(chat, logical)

    async def flush(self, chat, logical):
        # Serialise a whole logical update. Polling, typing and execution are
        # independent; /stop cancellation interrupts flood waits immediately.
        async with self.channel.locks.setdefault(chat, asyncio.Lock()):
            await self._flush(chat, logical)

    async def _flush(self, chat, logical):
        text_blocked = False
        for row in self.storage.delivery_parts('telegram', chat, logical):
            if text_blocked and row['part'].isdecimal():
                continue
            await self._part(self.storage.delivery(row['id']))
            current = self.storage.delivery(row['id'])
            if current['state'] in {'unknown', 'pending', 'sending'} and row['part'].isdecimal():
                text_blocked = True  # Never send an answer's tail after an uncertain prefix.

    async def _part(self, row):
        if row['cancelled'] or row['state'] in {'sent', 'failed'} or (row['state'] == 'unknown' and not row['message_id']):
            return
        payload = json.loads(row['payload'])
        kind = payload['kind']
        if kind == 'delete' and row['message_id'] is None:
            # Nothing known was ever posted, so there is no delete to perform.
            with self.storage.db:
                self.storage.db.execute("UPDATE delivery SET state='sent' WHERE id=? AND state='pending'", (row['id'],))
            return
        for retry in range(3):
            wait = self.channel.blocked_until.get(row['chat_id'], 0) - asyncio.get_running_loop().time()
            if wait > 0:
                await asyncio.sleep(wait)
            operation = 'delete' if kind == 'delete' else 'edit' if kind == 'text' and row['message_id'] else 'send'
            kwargs = {'chat_id': row['chat_id']}
            blob = None
            try:
                if kind == 'text':
                    method = self.channel.bot.edit_message_text if row['message_id'] else self.channel.bot.send_message
                    kwargs['text'] = payload['text']
                    if payload.get('parse_mode'):
                        kwargs['parse_mode'] = payload['parse_mode']
                    if row['message_id']:
                        kwargs['message_id'] = int(row['message_id'])
                    if payload.get('buttons'):
                        kwargs['reply_markup'] = InlineKeyboardMarkup([
                            [InlineKeyboardButton(label, callback_data=data) for label, data in group] for group in payload['buttons']])
                elif kind == 'delete':
                    method = self.channel.bot.delete_message
                    kwargs['message_id'] = int(row['message_id'])
                else:
                    # The snapshot is stable; reject missing/tampered bytes on
                    # recovery instead of sending a different local file.
                    blob = await open_snapshot(payload)
                    method = self.channel.bot.send_photo if kind == 'photo' else self.channel.bot.send_document
                    kwargs[kind] = InputFile(blob, filename=payload['name'], read_file_handle=False)
                    kwargs['caption'] = f"#t{row['task_id']} · {payload['name']}"[:1024]
                    kwargs['write_timeout'] = min(180, 30 + payload['size'] / 512_000)
            except (OSError, ValueError):
                if blob:
                    blob.close()
                attempt = self.storage.begin_delivery(row['id'], operation, payload, row['revision'])
                self.storage.delivery_outcome(row['id'], attempt, 'failed', detail='Prepared file is unavailable or changed')
                return
            attempt = self.storage.begin_delivery(row['id'], operation, payload, row['revision'])
            try:
                result = await method(**kwargs)
            except RetryAfter as exc:
                seconds = exc.retry_after.total_seconds() if isinstance(exc.retry_after, timedelta) else float(exc.retry_after)
                self.channel.blocked_until[row['chat_id']] = asyncio.get_running_loop().time() + seconds + .1
                self.channel.log_flood(method, seconds)
                self.storage.delivery_outcome(row['id'], attempt, 'pending', detail='Flood control refused request')
                continue
            except BadRequest as exc:
                reason = str(exc).lower()
                if 'message is not modified' in reason or kind == 'delete' and 'message to delete not found' in reason:
                    self.storage.delivery_outcome(row['id'], attempt, 'sent', row['message_id'])
                    return
                if kind == 'text' and row['message_id'] and 'message to edit not found' in reason:
                    self.storage.delivery_outcome(row['id'], attempt, 'pending', detail='Known message no longer exists')
                    with self.storage.db:
                        self.storage.db.execute('UPDATE delivery SET message_id=NULL WHERE id=?', (row['id'],))
                    return await self._part(self.storage.delivery(row['id']))
                if kind == 'text' and payload.get('parse_mode') and any(word in reason for word in ('parse entities', "can't find end", 'unsupported start tag')):
                    self.storage.delivery_outcome(row['id'], attempt, 'pending', detail='Markup refused')
                    payload = {**payload, 'text': plain_text(payload['text'])}
                    payload.pop('parse_mode', None)
                    continue
                if kind == 'photo' and any(word in reason for word in ('photo', 'image', 'dimension', 'ratio')):
                    self.storage.delivery_outcome(row['id'], attempt, 'pending', detail='Photo refused; sending as document')
                    kind = payload['kind'] = 'document'
                    continue
                self.storage.delivery_outcome(row['id'], attempt, 'failed', detail='Telegram rejected the request')
                return
            except (TelegramError, asyncio.CancelledError) as exc:
                # Existing message ids permit idempotent edit/delete retries.
                # No Bot API idempotency token exists for a fresh send.
                unsent = isinstance(exc.__cause__, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
                state = 'pending' if operation in {'edit', 'delete'} or unsent else 'unknown'
                self.storage.delivery_outcome(row['id'], attempt, state, detail=type(exc).__name__)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return
            except Exception:
                self.storage.delivery_outcome(row['id'], attempt, 'pending' if operation != 'send' else 'unknown', detail='Transport failed')
                return
            else:
                mid = str(result.message_id) if hasattr(result, 'message_id') else row['message_id']
                self.storage.delivery_outcome(row['id'], attempt, 'sent', mid)
                return
            finally:
                if blob:
                    blob.close()

    def settled(self, chat, logical):
        return all(row['state'] == 'sent' for row in self.storage.delivery_parts('telegram', chat, logical))

    def problems(self, chat, logical):
        rows = self.storage.delivery_parts('telegram', chat, logical)
        unknown = sum(row['state'] == 'unknown' for row in rows)
        failed = sum(row['state'] == 'failed' for row in rows)
        pending = sum(row['state'] in {'pending', 'sending'} for row in rows)
        return unknown, failed, pending
