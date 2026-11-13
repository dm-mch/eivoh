"""PTB transport; no Telegram types escape into the gateway.

Studied reference: nanobot (MIT), nanobot/channels/telegram/runtime.py;
original splitting/formatting/typing and dispatch implementation, no copied code.
"""
import asyncio
from datetime import timedelta
import logging

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError

from ..speech import TranscriptionError
from .base import Inbound
from .formatting import plain_text, render_chunks

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
        offset = None
        while True:
            try:
                updates = await self.bot.get_updates(offset=offset, timeout=30, read_timeout=35,
                                                     allowed_updates=["message", "callback_query"])
            except RetryAfter as exc:
                delay = exc.retry_after
                delay = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
                await asyncio.sleep(delay + 0.1)
                continue
            except NetworkError:
                log.warning("Telegram polling network error; retrying")
                await asyncio.sleep(2)
                continue
            for update in updates:
                message = update.effective_message
                user = update.effective_user
                chat = update.effective_chat
                callback = getattr(update, 'callback_query', None)
                replied = getattr(message, 'reply_to_message', None)
                reply_to = str(replied.message_id) if replied else None
                if not user or user.id != self.owner_id or not chat or chat.type != "private":
                    log.info("Ignored Telegram update from non-owner or non-private chat")
                elif callback:
                    try:
                        await callback.answer()
                    except TelegramError:
                        log.warning("Could not acknowledge Telegram callback")
                    if isinstance(callback.data, str):
                        await receive(Inbound("telegram", str(chat.id), str(user.id),
                                              f"cb:{callback.id}", callback.data, button=True))
                elif message and message.text:
                    # Do not request the next offset (acknowledge the batch) until
                    # the gateway has committed each accepted inbound to SQLite.
                    await receive(Inbound("telegram", str(chat.id), str(user.id),
                                          str(message.message_id), message.text, reply_to))
                elif message and (message.voice or message.audio):
                    # Transcribed before journaling: a crash here leaves the update
                    # unacknowledged, so it is fetched and transcribed again.
                    text = await self._transcribe(str(chat.id), message.voice or message.audio)
                    if text:
                        await receive(Inbound("telegram", str(chat.id), str(user.id),
                                              str(message.message_id), VOICE_PREFIX + text, reply_to))
                offset = update.update_id + 1

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
