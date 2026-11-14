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

# Routing/display metadata is not content. Unknown fields default to a notice.
_MESSAGE_METADATA = frozenset("""
message_id date chat from from_user sender_chat sender_boost_count sender_business_bot
business_connection_id via_bot author_signature sender_tag guest_bot_caller_user guest_bot_caller_chat
guest_query_id reply_to_message reply_to_story reply_to_checklist_task_id reply_to_poll_option_id
external_reply quote forward_origin edit_date media_group_id message_thread_id is_topic_message
is_automatic_forward has_protected_content is_from_offline effect_id show_caption_above_media
has_media_spoiler entities caption_entities reply_markup link_preview_options paid_star_count
is_paid_post direct_messages_topic suggested_post_info
""".split())

# PTB 22.8 Message service events: chat administration, payments/gifts and state
# changes need no answer. User-supplied web_app_data/users_shared/chat_shared do.
_SERVICE_FIELDS = frozenset("""
new_chat_members left_chat_member new_chat_title new_chat_photo delete_chat_photo
group_chat_created supergroup_chat_created channel_chat_created migrate_to_chat_id
migrate_from_chat_id pinned_message message_auto_delete_timer_changed connected_website
proximity_alert_triggered video_chat_scheduled video_chat_started video_chat_ended
video_chat_participants_invited forum_topic_created forum_topic_closed forum_topic_reopened
forum_topic_edited general_forum_topic_hidden general_forum_topic_unhidden write_access_allowed
boost_added chat_background_set successful_payment refunded_payment gift unique_gift
gift_upgrade_sent giveaway_created giveaway_completed paid_message_price_changed
direct_message_price_changed checklist_tasks_done checklist_tasks_added suggested_post_approved
suggested_post_approval_failed suggested_post_declined suggested_post_paid suggested_post_refunded
chat_owner_changed chat_owner_left poll_option_added poll_option_deleted managed_bot_created
""".split())


# The API allows 16 block levels; 128 JSON levels leave room for arrays and text.
_RICH_DEPTH = 128
_RICH_BLOCKS = 4096
_RICH_NODES = 262144  # Above any payload within Telegram's 32768-character limit.


def _sequence(value):
    if not isinstance(value, (list, tuple)):
        raise ValueError('Invalid array')
    return value


def _text(value):
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ValueError('Invalid text')
    value.encode('utf-8')
    return value


def _unsupported(fields):
    return dict(kind='unsupported', content_fields=sorted(fields) or ['unknown'])


def _rich_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return ''.join(_rich_text(part) for part in value)
    if not isinstance(value, dict):
        return ''
    kind = value.get('type')
    if kind == 'custom_emoji':
        return _rich_text(value.get('alternative_text'))
    if kind == 'mathematical_expression':
        return _rich_text(value.get('expression'))
    if kind == 'button':
        return _rich_text(value.get('button'))
    text = _rich_text(value.get('text'))
    url = value.get('url')
    if kind == 'url' and url is not None:
        url = _text(url)
        if url != text:
            text += f' ({url})'
    return text


def _descriptor(media, kind, album=None):
    get = media.get if isinstance(media, dict) else lambda key: getattr(media, key, None)
    file_id, name, size, mime = get('file_id'), get('file_name'), get('file_size'), get('mime_type')
    if not isinstance(file_id, str) or not file_id:
        raise ValueError('Invalid file ID')
    if size is not None and (type(size) is not int or size < 0):
        raise ValueError('Invalid file size')
    for value in (file_id, name, mime, album):
        _text(value)
    return dict(file_id=file_id, kind=kind, name=name, size=size, mime_type=mime, album=album)


def _rich_message(value):
    # PTB 22.8 keeps Bot API 10.1+ rich_message in api_kwargs; newer PTB may
    # expose TelegramObjects. to_dict recursively normalizes those as well.
    if hasattr(value, 'to_dict'):
        value = value.to_dict()
    nodes = 0
    def validate(value, depth=0):
        nonlocal nodes
        nodes += 1
        if depth > _RICH_DEPTH or nodes > _RICH_NODES:
            raise ValueError('Rich message traversal limit')
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise ValueError('Invalid field name')
                validate(child, depth + 1)
        elif isinstance(value, (list, tuple)):
            for child in value:
                validate(child, depth + 1)
        elif isinstance(value, str):
            _text(value)
        elif value is not None and not isinstance(value, (int, float, bool)):
            raise ValueError('Invalid value')
    validate(value)
    if not isinstance(value, dict):
        raise ValueError('Invalid rich message')
    attachments, count = [], 0
    kinds = dict(photo='photo', document='document', voice_note='voice', audio='voice',
                 video='unsupported', animation='unsupported')

    def blocks(values):
        return '\n'.join(text for value in _sequence(values) if (text := block(value)))

    def block(value):
        nonlocal count
        count += 1
        if count > _RICH_BLOCKS:
            raise ValueError('Too many rich blocks')
        if not isinstance(value, dict):
            return ''
        kind = value.get('type')
        kind = kind if isinstance(kind, str) else ''
        parts = [_rich_text(value.get('summary')), _rich_text(value.get('text'))]
        if kind == 'mathematical_expression':
            parts.append(_rich_text(value.get('expression')))
        if kind in kinds:
            try:
                media = value.get(kind)
                if kind == 'photo':
                    sizes = _sequence(media)
                    for photo in sizes:
                        if not isinstance(photo, dict) or any(type(photo.get(key)) is not int or photo[key] <= 0
                                                             for key in ('width', 'height')):
                            raise ValueError('Invalid photo dimensions')
                        _descriptor(photo, 'photo')
                    media = max(sizes, key=lambda item: (item['width'] * item['height'],
                                                        item.get('file_size') or 0), default=None)
                item = _descriptor(media, kinds[kind])
            except ValueError:
                item = _unsupported(['rich_message'])
            # A rich message containing any media block is never a command.
            attachments.append(item)
            label = 'image' if kind == 'photo' else 'file' if kind == 'document' else kind
            name = ': ' + item['name'] if item.get('name') else ''
            parts.append(f'[{label} {len(attachments)}{name}]')
        if kind == 'list':
            for item in _sequence(value.get('items')):
                if isinstance(item, dict):
                    parts.append((_rich_text(item.get('label')) + ' ' + blocks(item.get('blocks'))).rstrip())
        elif kind == 'table':
            for row in _sequence(value.get('cells')):
                parts.append(' | '.join(_rich_text(cell.get('text')) if isinstance(cell, dict) else ''
                                        for cell in _sequence(row)))
        elif kind == 'buttons':
            parts.append(' '.join(_rich_text(button) for button in _sequence(value.get('buttons'))))
        if 'blocks' in value or kind in {'blockquote', 'collage', 'slideshow', 'details'}:
            parts.append(blocks(value.get('blocks')))
        caption = value.get('caption')
        parts.append(_rich_text(caption))
        if isinstance(caption, dict):
            parts.append(_rich_text(caption.get('credit')))
        parts.append(_rich_text(value.get('credit')))
        return '\n'.join(part for part in parts if part)

    return blocks(value.get('blocks')), attachments


def _message_fields(message):
    try:
        values = message.to_dict(recursive=False) if hasattr(message, 'to_dict') else vars(message)
        if not isinstance(values, dict):
            raise ValueError('Invalid field map')
    except Exception:
        # Introspection must also survive odd library objects; inspect names only.
        values = {}
        for cls in type(message).__mro__:
            slots = cls.__dict__.get('__slots__', ())
            for name in ([slots] if isinstance(slots, str) else slots):
                if not name.startswith('_'):
                    try:
                        values[name] = getattr(message, name, None)
                    except Exception:
                        values[name] = True
    api = getattr(message, 'api_kwargs', None)
    api_keys = {key for key in api if isinstance(key, str)} if isinstance(api, dict) or hasattr(api, 'keys') else set()
    fields = {key for key, value in values.items() if isinstance(key, str) and value is not None
              and value is not False and value != '' and value != () and value != [] and key != 'api_kwargs'} - api_keys
    return fields, api_keys


def _warn_update(update, message, reason):
    try:
        fields, api_keys = _message_fields(message) if message is not None else (set(), set())
    except Exception:
        fields, api_keys = set(), set()
    log.warning('%s Telegram update %s: message fields=%s; api_kwargs keys=%s',
                reason, update.update_id, sorted(fields), sorted(api_keys))


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
                if item.get('content_fields'):
                    raise AttachmentError('This Telegram message type is not supported: ' + ', '.join(item['content_fields']))
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
                ("effort", "Show or change reasoning effort"),
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
                if not user or user.id != self.owner_id or not chat or chat.type != "private":
                    if user and user.id == self.owner_id:
                        _warn_update(update, message, 'Dropped non-private')
                    else:
                        log.info("Ignored Telegram update from non-owner or non-private chat")
                    if self.storage:
                        with self.storage.db:
                            self.storage.receipt('telegram', update.update_id)
                elif callback:
                    try:
                        await callback.answer()
                    except TelegramError:
                        log.warning("Could not acknowledge Telegram callback")
                    try:
                        data = callback.data
                        valid = isinstance(data, str) and 0 < len(data.encode('utf-8')) <= 64
                        message_id = f'cb:{callback.id}'
                    except Exception:
                        valid = False
                    if valid:
                        accepted = await receive(Inbound('telegram', str(chat.id), str(user.id),
                                              message_id, data, button=True, update_id=update.update_id))
                        if accepted is False:
                            return
                    else:
                        _warn_update(update, message, 'Dropped callback')
                        if self.storage:
                            with self.storage.db:
                                self.storage.receipt('telegram', update.update_id)
                elif message:
                    legacy_media, reply_to = None, None
                    try:
                        replied = getattr(message, 'reply_to_message', None)
                        reply_to = str(replied.message_id) if replied is not None else None
                        rich = getattr(message, 'rich_message', None)
                        if rich is None:
                            rich = (getattr(message, 'api_kwargs', None) or {}).get('rich_message')
                        if rich is not None:
                            text, items = _rich_message(rich)
                            if any(item.get('content_fields') for item in items):
                                _warn_update(update, message, 'Unparseable media in')
                        else:
                            items = self._media(message)
                            text = _text(getattr(message, 'caption', None) if items else message.text)
                            if not self.storage:
                                legacy_media = getattr(message, 'voice', None) or getattr(message, 'audio', None)
                        if not text and not items:
                            fields, api_keys = _message_fields(message)
                            content = (fields | api_keys) - _MESSAGE_METADATA - _SERVICE_FIELDS
                            if content or not (fields | api_keys) & _SERVICE_FIELDS:
                                items = [_unsupported(content)]
                                _warn_update(update, message, 'Unsupported')
                            else:
                                _warn_update(update, message, 'Dropped service')
                    except Exception:
                        # Only synchronous content parsing is guarded. Transport,
                        # durable admission and shutdown refusal remain outside.
                        try:
                            fields, api_keys = _message_fields(message)
                            content = (fields | api_keys) - _MESSAGE_METADATA - _SERVICE_FIELDS
                        except Exception:
                            content = {'message'}
                        # reply_to keeps a target parsed before the failure.
                        text, items, legacy_media = '', [_unsupported(content)], None
                        _warn_update(update, message, 'Unparseable')
                    if legacy_media is not None:
                        # Legacy unbound channels have no durable preparation path.
                        text = await self._transcribe(str(chat.id), legacy_media)
                        if text:
                            accepted = await receive(Inbound('telegram', str(chat.id), str(user.id), str(message.message_id),
                                                  VOICE_PREFIX + text, reply_to, update_id=update.update_id))
                            if accepted is False:
                                return
                        else:
                            _warn_update(update, message, 'Dropped')
                    elif text or items:
                        # Admission commits routing and descriptors before downloads.
                        accepted = await receive(Inbound('telegram', str(chat.id), str(user.id), str(message.message_id),
                            text, reply_to, attachments=tuple(items), update_id=update.update_id))
                        if accepted is False:
                            return
                    elif self.storage:
                        with self.storage.db:
                            self.storage.receipt('telegram', update.update_id)
                else:
                    _warn_update(update, message, 'Dropped')
                    if self.storage:
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
        return [_descriptor(media, kind, getattr(message, 'media_group_id', None))]

    async def _transcribe(self, chat, media):
        if self.transcriber is None:
            await self._request(chat, self.bot.send_message, text="Voice messages are not configured (set EIVOH_STT).")
            return None
        await self.typing(chat)
        try:
            file = await self.bot.get_file(media.file_id)
            audio = bytes(await file.download_as_bytearray())
            text = _text(await self.transcriber.transcribe(audio, getattr(media, "file_name", None) or "voice.ogg"))
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

    async def react(self, chat_id, message_id, emoji):
        try:
            await self.bot.set_message_reaction(chat_id=chat_id, message_id=int(message_id), reaction=emoji)
        except TelegramError as exc:
            log.debug("Reaction not sent: %s", type(exc).__name__)
