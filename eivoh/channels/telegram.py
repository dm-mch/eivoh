"""PTB transport; no Telegram types escape into the gateway.

Studied reference: nanobot (MIT), nanobot/channels/telegram/runtime.py;
original splitting/formatting/typing and dispatch implementation, no copied code.
"""
import asyncio
from datetime import timedelta
import logging

from telegram import Bot
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError

from .base import Inbound
from .formatting import plain_text, render_chunks

log = logging.getLogger(__name__)


class TelegramChannel:
    def __init__(self, token: str, owner_id: int, bot=None):
        self.bot = bot if bot is not None else Bot(token)
        self.owner_id = owner_id
        self.locks = {}
        self.blocked_until = {}
        self.messages = {}  # logical first id -> chunk ids; V1 delivery state is ephemeral

    async def poll(self, receive):
        offset = None
        while True:
            try:
                updates = await self.bot.get_updates(offset=offset, timeout=30, read_timeout=35,
                                                     allowed_updates=["message"])
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
                if not user or user.id != self.owner_id or not chat or chat.type != "private":
                    log.info("Ignored Telegram update from non-owner or non-private chat")
                elif message and message.text:
                    # Do not request the next offset (acknowledge the batch) until
                    # the gateway has committed each accepted inbound to SQLite.
                    await receive(Inbound("telegram", str(chat.id), str(user.id),
                                          str(message.message_id), message.text))
                offset = update.update_id + 1

    async def _request(self, chat, method, **kwargs):
        # One lock covers sends, edits, deletes and typing, including RetryAfter.
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

    async def send(self, chat_id, text):
        ids = []
        for chunk in render_chunks(text):
            message = await self._request(chat_id, self.bot.send_message, text=chunk, parse_mode="HTML")
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

    async def typing(self, chat_id):
        await self._request(chat_id, self.bot.send_chat_action, action="typing")
