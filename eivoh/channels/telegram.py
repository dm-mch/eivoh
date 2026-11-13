"""PTB transport; no Telegram types escape into the gateway.

Studied reference: nanobot (MIT), nanobot/channels/telegram/runtime.py;
original splitting/formatting/typing and dispatch implementation, no copied code.
"""
import asyncio
from datetime import timedelta
import logging
import json
from pathlib import Path

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Conflict, NetworkError, RetryAfter, TelegramError

from ..speech import TranscriptionError
from .base import Inbound
from .formatting import plain_text, render_chunks
from .delivery import TelegramDelivery
from .locking import ConsumerConflict
from ..attachments import AttachmentError, INPUT_LIMIT, MAX_FILES, download_file, input_manifest, input_path

log = logging.getLogger(__name__)


VOICE_PREFIX = "[Voice message, speech-to-text]\n"


class TelegramChannel:
    def __init__(self, token: str, owner_id: int, bot=None, transcriber=None):
        self.bot = bot if bot is not None else Bot(token)
        self.owner_id = owner_id
        self.transcriber = transcriber
        self.locks = {}
        self.blocked_until = {}
        self.messages = {}  # logical first id -> chunk ids; V1 delivery state is ephemeral
        self.storage = None

    def bind(self, storage):
        self.storage = storage
        self.delivery = TelegramDelivery(self, storage)

    async def deliver(self, chat, text, logical, task_id=None, turn_id=None, buttons=None):
        mid = await self.delivery.text(chat, logical, text, task_id, turn_id, buttons)
        if mid is not None:
            self.messages[(chat, mid)] = [row['message_id'] for row in self.storage.delivery_parts('telegram', chat, logical)
                                          if row['part'].isdecimal() and row['message_id'] is not None]
        return mid

    def prepare_reply(self, chat, text, logical, task_id=None, turn_id=None, buttons=None):
        self.delivery.prepare_text(chat, logical, text, task_id, turn_id, buttons)

    def prepare_final(self, chat, text, turn_id, task_id):
        self.delivery.prepare_final(chat, f'turn:{turn_id}', f'turn:{turn_id}:final', text, task_id, turn_id)

    async def deliver_files(self, chat, artifacts, logical, task_id, turn_id):
        await self.delivery.files(chat, logical, artifacts, task_id, turn_id)
        rows = self.storage.delivery_parts('telegram', chat, logical)
        ids = [row['message_id'] for row in rows if row['part'].isdecimal() and row['message_id'] is not None]
        if ids:
            self.messages[(chat, ids[0])] = ids

    @staticmethod
    def log_flood(method, seconds):
        log.warning('Telegram flood control on %s: waiting %.0f s', getattr(method, '__name__', 'request'), seconds)

    async def prepare(self, row):
        items = json.loads(row['attachments'])
        if len(items) > MAX_FILES:
            raise AttachmentError('Too many input files (maximum 10 per message)')
        prepared = []
        text = row['text']
        for index, original in enumerate(items):
            item = dict(original)
            if item['kind'] == 'unsupported':
                raise AttachmentError('This Telegram media type is not supported; send it as a document or photo.')
            if item.get('size') is not None and item['size'] > INPUT_LIMIT:
                raise AttachmentError('Attachment exceeds the 20 MB download limit')
            name = item.get('name') or ('voice.ogg' if item['kind'] == 'voice' else 'image.jpg' if item['kind'] == 'photo' else 'file.bin')
            path = input_path(self.storage.state_dir, row['id'], index, name)
            if path.is_symlink():
                raise AttachmentError('Stored attachment path is unsafe')
            if not path.exists():
                file = await self.bot.get_file(item['file_id'])
                if getattr(file, 'file_size', None) is not None and file.file_size > INPUT_LIMIT:
                    raise AttachmentError('Attachment exceeds the 20 MB download limit')
                url = getattr(file, 'file_path', None)
                # Production Bot.get_file returns an absolute cloud HTTPS URL.
                # Local Bot API servers are not enabled by this application.
                if not isinstance(url, str) or not url.startswith('https://'):
                    raise AttachmentError('Telegram did not provide a downloadable file')
                await download_file(url, path)
            if not path.is_file() or path.stat().st_size > INPUT_LIMIT:
                raise AttachmentError('Stored attachment is unavailable or too large')
            item.update(name=name, path=str(path), size=path.stat().st_size)
            prepared.append(item)
            if item['kind'] == 'voice':
                if self.transcriber is None:
                    raise AttachmentError('Voice messages are not configured (set EIVOH_STT).')
                audio = await asyncio.to_thread(path.read_bytes)
                transcript = await self.transcriber.transcribe(audio, name)
                if not transcript:
                    raise AttachmentError('The voice message transcript is empty.')
                text = VOICE_PREFIX + transcript + ('\n\nCaption: ' + text if text else '')
        ordinary = [item for item in prepared if item['kind'] != 'voice']
        if ordinary:
            text = (text or 'Please inspect the attached file(s).') + input_manifest(ordinary)
        return text, prepared

    async def poll(self, receive):
        try:
            await self.bot.set_my_commands([
                ("new", "Start a new task"),
                ("tasks", "List and switch tasks"),
                ("model", "Show or change the task model"),
                ("stop", "Stop running and queued work"),
                ("q", "Queue a message after the running turn"),
                ("close", "Close a task"),
                ("title", "Rename a task"),
            ])
        except Exception as exc:
            log.warning("Telegram command registration failed: %s", type(exc).__name__)
        offset = self.storage.cursor('telegram') if self.storage else None
        conflicts = []
        while True:
            if self.storage and self.storage.cursor('telegram') is None:
                offset = None  # Also expire offsets during uninterrupted idle polling.
            try:
                updates = await self.bot.get_updates(offset=offset, timeout=30, read_timeout=35,
                                                     allowed_updates=["message", "callback_query"])
            except RetryAfter as exc:
                delay = exc.retry_after
                delay = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
                await asyncio.sleep(delay + 0.1)
                continue
            except Conflict:
                now = asyncio.get_running_loop().time()
                conflicts = [when for when in conflicts if now - when < 300]
                conflicts.append(now)
                if len(conflicts) >= 5:
                    raise ConsumerConflict('Telegram polling repeatedly conflicts with another consumer; stop the other consumer before restarting') from None
                delay = (10, 20, 30, 45)[len(conflicts) - 1]
                log.warning('Telegram polling conflict; retrying in %s s', delay)
                await asyncio.sleep(delay)
                continue
            except NetworkError:
                log.warning("Telegram polling network error; retrying")
                await asyncio.sleep(2)
                continue
            for update in updates:
                if self.storage and self.storage.saw_update('telegram', update.update_id):
                    offset = update.update_id + 1
                    self.storage.advance_cursor('telegram', offset)
                    continue
                message = update.effective_message
                user = update.effective_user
                chat = update.effective_chat
                callback = getattr(update, 'callback_query', None)
                replied = getattr(message, 'reply_to_message', None)
                reply_to = str(replied.message_id) if replied else None
                if not user or user.id != self.owner_id or not chat or chat.type != "private":
                    log.info("Ignored Telegram update from non-owner or non-private chat")
                    if self.storage:
                        with self.storage.db:
                            self.storage.receipt('telegram', update.update_id)
                elif callback:
                    try:
                        await callback.answer()
                    except TelegramError:
                        log.warning("Could not acknowledge Telegram callback")
                    if isinstance(callback.data, str):
                        accepted = await receive(Inbound("telegram", str(chat.id), str(user.id),
                                              f"cb:{callback.id}", callback.data, button=True, update_id=update.update_id))
                        if accepted is False:
                            return
                    elif self.storage:
                        with self.storage.db:
                            self.storage.receipt('telegram', update.update_id)
                elif message and self.storage and self._media(message):
                    accepted = await receive(Inbound('telegram', str(chat.id), str(user.id), str(message.message_id),
                        getattr(message, 'caption', None) or '', reply_to, attachments=tuple(self._media(message)), update_id=update.update_id))
                    if accepted is False:
                        return
                elif message and message.text:
                    # Do not request the next offset (acknowledge the batch) until
                    # the gateway has committed each accepted inbound to SQLite.
                    accepted = await receive(Inbound("telegram", str(chat.id), str(user.id),
                                          str(message.message_id), message.text, reply_to, update_id=update.update_id))
                    if accepted is False:
                        return
                elif message and (message.voice or message.audio):
                    # Transcribed before journaling: a crash here leaves the update
                    # unacknowledged, so it is fetched and transcribed again.
                    text = await self._transcribe(str(chat.id), message.voice or message.audio)
                    if text:
                        await receive(Inbound("telegram", str(chat.id), str(user.id),
                                              str(message.message_id), VOICE_PREFIX + text, reply_to))
                elif self.storage:
                    with self.storage.db:
                        self.storage.receipt('telegram', update.update_id)
                offset = update.update_id + 1
                if self.storage:
                    self.storage.advance_cursor('telegram', offset)

    @staticmethod
    def _media(message):
        media, kind = None, None
        if getattr(message, 'voice', None) or getattr(message, 'audio', None):
            media, kind = getattr(message, 'voice', None) or message.audio, 'voice'
        elif getattr(message, 'document', None):
            media, kind = message.document, 'document'
        elif getattr(message, 'photo', None):
            media, kind = message.photo[-1], 'photo'
        else:
            for field in ('video', 'animation', 'sticker', 'video_note'):
                if getattr(message, field, None):
                    media, kind = getattr(message, field), 'unsupported'
                    break
        if media is None:
            return []
        return [dict(file_id=media.file_id, kind=kind, name=getattr(media, 'file_name', None),
                     size=getattr(media, 'file_size', None), mime_type=getattr(media, 'mime_type', None),
                     album=getattr(message, 'media_group_id', None))]

    async def _transcribe(self, chat, media):
        if self.transcriber is None:
            await self._request(chat, self.bot.send_message, text="Voice messages are not configured (set EIVOH_STT).")
            return None
        await self.typing(chat)
        try:
            file = await self.bot.get_file(media.file_id)
            audio = bytes(await file.download_as_bytearray())
            text = await self.transcriber.transcribe(audio, getattr(media, "file_name", None) or "voice.ogg")
        except Exception as exc:
            log.warning("Voice transcription failed: %s", type(exc).__name__)
            # Only our own messages are shown; others may carry request details.
            reason = str(exc) if isinstance(exc, TranscriptionError) else type(exc).__name__
            await self._request(chat, self.bot.send_message, text=f"Could not transcribe the voice message: {reason}")
            return None
        if not text:
            await self._request(chat, self.bot.send_message, text="The voice message transcript is empty.")
            return None
        # Plain text echo so recognition errors are visible before the answer.
        await self._request(chat, self.bot.send_message, text="🎤 " + (text if len(text) < 4000 else text[:4000] + "…"))
        return text

    async def _request(self, chat, method, **kwargs):
        # One lock covers sends, edits and deletes, including RetryAfter.
        async with self.locks.setdefault(chat, asyncio.Lock()):
            for attempt in range(3):
                delay = self.blocked_until.get(chat, 0) - asyncio.get_running_loop().time()
                if delay > 0:
                    await asyncio.sleep(delay)
                try:
                    return await method(chat_id=chat, **kwargs)
                except RetryAfter as exc:
                    delay = exc.retry_after
                    delay = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
                    self.blocked_until[chat] = asyncio.get_running_loop().time() + delay + 0.1
                    # Visible evidence: deliveries to this chat pause for this long.
                    log.warning("Telegram flood control on %s: waiting %.0f s", getattr(method, "__name__", "request"), delay)
                    if attempt == 2:
                        log.warning("Telegram flood limit persists; delivery skipped")
                except BadRequest as exc:
                    message = str(exc).lower()
                    if "message is not modified" in message:
                        return None
                    if kwargs.get("parse_mode") and ("parse entities" in message or "can't find end" in message or "unsupported start tag" in message):
                        kwargs["text"] = plain_text(kwargs["text"])
                        kwargs.pop("parse_mode", None)
                        continue  # Parse refusal guarantees it was not posted.
                    log.warning("Telegram rejected delivery: %s", type(exc).__name__)
                    return None
                except TelegramError as exc:
                    # Never retry timeouts: the first send may already be posted.
                    log.warning("Telegram delivery failed: %s", type(exc).__name__)
                    return None
        return None

    async def send(self, chat_id, text, buttons=None):
        ids = []
        chunks = render_chunks(text)
        for index, chunk in enumerate(chunks):
            extra = {}
            if buttons and index == len(chunks) - 1:
                extra['reply_markup'] = InlineKeyboardMarkup([
                    [InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in buttons])
            message = await self._request(chat_id, self.bot.send_message, text=chunk, parse_mode="HTML", **extra)
            ids.append(str(message.message_id) if message else None)
        if not ids or ids[0] is None:
            return None
        self.messages[(chat_id, ids[0])] = ids
        return ids[0]

    async def edit(self, chat_id, message_id, text):
        chunks = render_chunks(text)
        ids = self.messages.setdefault((chat_id, message_id), [message_id])
        for index, chunk in enumerate(chunks):
            if index < len(ids):
                if ids[index] is not None:
                    await self._request(chat_id, self.bot.edit_message_text, message_id=int(ids[index]), text=chunk, parse_mode="HTML")
            else:
                message = await self._request(chat_id, self.bot.send_message, text=chunk, parse_mode="HTML")
                ids.append(str(message.message_id) if message else None)
        for mid in ids[len(chunks):]:
            if mid is not None:
                await self._request(chat_id, self.bot.delete_message, message_id=int(mid))
        del ids[len(chunks):]

    def parts(self, chat_id, message_id):
        """All message ids currently carrying one logical message (long answers are split)."""
        return [mid for mid in self.messages.get((chat_id, message_id), [message_id]) if mid is not None]

    async def typing(self, chat_id):
        # Outside the delivery lock and its flood back-off: the indicator is the
        # only sign of life while edits wait, and a lost one needs no retry.
        try:
            await self.bot.send_chat_action(chat_id=chat_id, action="typing")
        except TelegramError as exc:
            log.debug("Typing indicator not sent: %s", type(exc).__name__)
