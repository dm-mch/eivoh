"""Channel-neutral routing, durable queues and executor lifecycle."""
import asyncio
from contextlib import aclosing, suppress
from datetime import datetime, timezone
import logging
import json

from .channels.base import Channel, Inbound
from .executors.base import Executor
from .models import catalog, resolve, effort_levels
from .storage import Storage, command
from . import titles
from .attachments import AttachmentError, output_instructions, snapshot_outputs, visible_text

log = logging.getLogger(__name__)
RESTART_PROMPT = "The service restarted; continue the interrupted task. Check existing work before repeating actions."


async def safe_call(method, *args):
    try:
        return await method(*args)
    except Exception:
        log.exception("Channel delivery failed")
        return None


class Reply:
    """Coalesce display updates without blocking raw event persistence."""
    def __init__(self, channel, chat_id, interval, prefix=lambda: "", sent_callback=lambda mid: None,
                 logical_id=None, task_id=None, turn_id=None):
        self.channel, self.chat_id, self.interval = channel, chat_id, interval
        self.prefix, self.sent_callback = prefix, sent_callback
        self.logical_id, self.task_id, self.turn_id = logical_id, task_id, turn_id
        self.text = ""
        self.tool = ""
        self.sent = None
        self.message_id = None
        self.changed = asyncio.Event()
        self.closed = False
        self.job = asyncio.create_task(self.run())

    def update(self, text, tool="", *, final=False):
        self.text, self.tool = visible_text(text, final=final), tool
        self.changed.set()

    async def run(self):
        last = float("-inf")
        while True:
            await self.changed.wait()
            self.changed.clear()
            await asyncio.sleep(max(0, last + self.interval - asyncio.get_running_loop().time()))
            text = self.text + (f"\n\n_Tool: {self.tool}_" if self.tool else "")
            if text:
                text = self.prefix() + text
            if text and text != self.sent:
                if hasattr(self.channel, 'deliver') and self.logical_id is not None:
                    self.message_id = await safe_call(self.channel.deliver, self.chat_id, text,
                                                     self.logical_id, self.task_id, self.turn_id)
                    self.sent_callback(self.message_id)
                elif self.message_id is None:
                    self.message_id = await safe_call(self.channel.send, self.chat_id, text)
                    # Do not retry ambiguous send failures: Telegram may have posted it.
                    if self.message_id is None:
                        return
                    self.sent_callback(self.message_id)
                else:
                    await safe_call(self.channel.edit, self.chat_id, self.message_id, text)
                    self.sent_callback(self.message_id)  # An edit may add parts.
                self.sent = text
                last = asyncio.get_running_loop().time()
            if self.closed and not self.changed.is_set():
                return

    async def finish(self):
        self.closed = True
        self.changed.set()
        # Allow final flood-control backoff up to 60 s; shutdown cancels this wait.
        try:
            await asyncio.wait_for(self.job, 60)
        except TimeoutError:
            log.warning("Final response delivery timed out")

    async def cancel(self):
        self.job.cancel()
        with suppress(asyncio.CancelledError):
            await self.job


class Gateway:
    def __init__(self, storage: Storage, executor: Executor, channels: dict[str, Channel],
                 edit_interval=1.5, typing_interval=4, max_recoveries=3, executors=None, max_parallel=3,
                 title_executors=None, title_timeout=30, delivery_retry_delays=(1, 2, 4, 8, 16), delivery_timeout=300):
        # `executor` is the default one; `executors` adds named alternatives.
        self.default_executor = storage.default_executor
        self.executors = {**(executors or {}), self.default_executor: executor}
        self.storage, self.channels = storage, channels
        for channel in channels.values():
            if hasattr(channel, 'bind'):
                channel.bind(storage)
        self.edit_interval, self.typing_interval = edit_interval, typing_interval
        self.max_recoveries = max_recoveries
        if not isinstance(max_parallel, int) or max_parallel < 1:
            raise ValueError("max_parallel must be an integer >= 1")
        self.slots = asyncio.Semaphore(max_parallel)
        # Optional injection keeps fake/embedding executors entirely offline.
        self.title_executors = title_executors or {}
        self.title_timeout = title_timeout
        self.delivery_retry_delays, self.delivery_timeout = delivery_retry_delays, delivery_timeout
        self.title_jobs = {}
        self.replies = set()
        self.workers = {}
        self.active_runs = {}
        self.steer_jobs = {}
        self.notice_jobs = {}
        self.reaction_jobs = {}
        self.media_jobs = {}
        self.media_slots = asyncio.Semaphore(2)
        self.output_jobs = {}
        self.locks = {}
        self.model_catalogs = {}  # Numbers refer to the last list shown in this process.
        self.closing = False

    async def start(self):
        self.storage.recover_steering()
        self.storage.recover_deliveries()
        # Commands have no turn. Replay them in journal order before any launch.
        for row in self.storage.pending():
            if row['preparation'] == 'pending':
                self._prepare_media(row)
            if self.storage.turn_for(row['id']) is None:
                await self._command(row)
        for row in self.storage.pending():
            delivery = self.storage.steering(row['id'])
            if delivery and delivery['state'] == 'unknown':
                task_id = self.storage.turn_for(row['id'])['task_id']
                self._notice(row, f"Earlier delivery to task {task_id} could not be confirmed. Kept for recovery with a check for work already done.", task_id, 'recovery')
            self._kick(self.storage.turn_for(row['id'])['task_id'])
        for row in self.storage.pending_outputs():
            self._schedule_output(row, row['task_id'], row['output_turn'])
        # Command/voice/steering notices may have been waiting on flood control
        # after their inbound was finished. Their persisted intent is independent.
        for row in self.storage.db.execute("SELECT DISTINCT channel,chat_id,logical_id,task_id FROM delivery "
                                           "WHERE turn_id IS NULL AND cancelled=0 AND state='pending'").fetchall():
            channel = self.channels.get(row['channel'])
            if not hasattr(channel, 'delivery'):
                continue
            job = asyncio.create_task(self._retry_notice(channel, row['chat_id'], row['logical_id']))
            self.notice_jobs.setdefault(row['task_id'], set()).add(job)
            job.add_done_callback(lambda done, task_id=row['task_id']: self.notice_jobs.get(task_id, set()).discard(done))
        for task in self.storage.title_candidates():
            self._name_task(task['id'])

    async def receive(self, message: Inbound):
        if self.closing:
            return False
        # Include journaling under the lock: an arrival during /stop must bind
        # after earlier commands have taken effect, and never be rebound later.
        key = (message.channel, message.chat_id)
        async with self.locks.setdefault(key, asyncio.Lock()):
            if self.closing:
                return False
            row = self.storage.journal(message)
            if row is None:
                return True
            turn = self.storage.turn_for(row['id'])
            if turn is None:
                await self._command(row, button=message.button)
            else:
                worker = self.workers.get(turn['task_id'])
                busy = worker is not None and not worker.done()
                busy = busy or any(item['id'] < row['id'] for item in self.storage.pending_task(turn['task_id']))
                if row['preparation'] == 'pending':
                    self._prepare_media(row)
                self._kick(turn['task_id'])
                reserved = self._kick_steer(turn['task_id'])
                if turn['force_queue']:
                    if busy:
                        self._react(row, '✍', turn['task_id'])
                elif busy and row['id'] not in reserved:
                    active = self.active_runs.get(turn['task_id'])
                    task = self.storage.task(row['conversation_id'], turn['task_id'])
                    if active and row['preparation'] != 'pending' and any(active[0][key] != task[key] for key in ('executor', 'model', 'effort')):
                        self._notice(row, f"Queued for task {turn['task_id']} after earlier messages, using the selected model and effort.", turn['task_id'], 'admission')
                    else:
                        self._react(row, '✍', turn['task_id'])
            for reply in self.replies:
                reply.changed.set()  # A second task or switch may change labels.
        return True

    def _prepare_media(self, row):
        if row['id'] in self.media_jobs:
            return
        task_id = self.storage.turn_for(row['id'])['task_id']
        async def prepare():
            try:
                async with self.media_slots:
                    async with asyncio.timeout(180):
                        text, items = await self.channels[row['channel']].prepare(row)
                if self.storage.prepared(row['id'], text, items):
                    if text.startswith('[Voice message, speech-to-text]\n'):
                        transcript = text.split('\n', 1)[1]
                        self._notice(row, '🎤 ' + transcript[:4000], task_id, 'voice')
                    self._kick_steer(task_id)
                    self._kick(task_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = str(exc) if isinstance(exc, AttachmentError) else type(exc).__name__
                text = 'Could not prepare the attachment: ' + reason
                if self.storage.prepared(row['id'], text, json.loads(row['attachments']), failed=True):
                    self._kick(task_id)
        job = asyncio.create_task(prepare())
        self.media_jobs[row['id']] = job
        job.add_done_callback(lambda done: self.media_jobs.pop(row['id'], None))

    @property
    def executor(self):
        return self.executors[self.default_executor]

    @executor.setter
    def executor(self, value):
        self.executors[self.default_executor] = value

    async def _cancel_run(self, run_id):
        # Run ids are turn ids, unique across executors; cancel is a no-op elsewhere.
        await asyncio.gather(*(e.cancel(run_id) for e in self.executors.values()))

    def _kick_steer(self, task_id):
        reserved = set()
        if self.closing or task_id not in self.active_runs:
            return reserved
        parent, executor = self.active_runs[task_id]
        if not getattr(executor, 'supports_steer', False):
            return reserved
        rows = [row for row in self.storage.pending_task(task_id) if row['id'] != parent['inbound_id']]
        for row in rows:
            input_id = self.storage.reserve_steer(parent['id'], row['id'])
            if input_id is None:
                break  # FIFO barrier: /q, changed model, or uncertain delivery.
            reserved.add(row['id'])
            job = asyncio.create_task(self._deliver_steering(task_id, parent, executor, row, input_id))
            self.steer_jobs.setdefault(task_id, set()).add(job)
            def forget(done, task_id=task_id, inbound_id=row['id']):
                if done.cancelled():
                    # Covers cancellation before the coroutine's first step.
                    self.storage.steer_outcome(inbound_id, 'unknown', 'Delivery interrupted')
                jobs = self.steer_jobs.get(task_id)
                if jobs is not None:
                    jobs.discard(done)
                    if not jobs:
                        self.steer_jobs.pop(task_id, None)
                if not done.cancelled() and (error := done.exception()):
                    log.error('Task %s steering failed', task_id, exc_info=(type(error), error, error.__traceback__))
            job.add_done_callback(forget)
        return reserved

    async def _deliver_steering(self, task_id, parent, executor, row, input_id):
        try:
            result = await executor.steer(parent['id'], input_id, self.storage.input_text(row['id']))
        except asyncio.CancelledError:
            self.storage.steer_outcome(row['id'], 'unknown', 'Delivery interrupted')
            raise
        except Exception:
            # A failed write is ambiguous. Freeze the transport before fallback.
            await executor.cancel(parent['id'])
            self.storage.steer_outcome(row['id'], 'unknown', 'Delivery failed without acknowledgement')
        else:
            state = {'accepted': 'accepted', 'not_sent': 'queued', 'rejected': 'queued', 'unknown': 'unknown'}[result.status]
            self.storage.steer_outcome(row['id'], state, result.detail)
        delivery = self.storage.steering(row['id'])
        if self.storage.inbound(row['id'])['state'] == 'cancelled' or self.closing:
            return
        if delivery['state'] == 'accepted':
            self._react(row, '👀', task_id)
        else:
            if delivery['state'] == 'unknown':
                self._notice(row, f"Delivery to task {task_id} could not be confirmed. Kept for the next turn; it will check for work already done.", task_id, 'steering')
            else:
                self._react(row, '✍', task_id)
            self._kick(task_id)  # An exceptional parent may have ended while this input was sending.

    def _react(self, row, emoji, task_id):
        channel = self.channels[row['channel']]
        if row['message_id'].startswith('cb:') or not hasattr(channel, 'react') or self.closing:
            return
        previous = self.reaction_jobs.get(row['id'])
        async def react():
            try:
                if previous:
                    await asyncio.gather(previous, return_exceptions=True)
                if not self.closing and self.storage.inbound(row['id'])['state'] != 'cancelled':
                    await channel.react(row['chat_id'], row['message_id'], emoji)
            except Exception as exc:
                log.debug('Reaction not sent: %s', type(exc).__name__)
        job = asyncio.create_task(react())
        self.reaction_jobs[row['id']] = job
        self._track_notice(job, task_id)
        def forget(done):
            if self.reaction_jobs.get(row['id']) is done:
                self.reaction_jobs.pop(row['id'], None)
        job.add_done_callback(forget)

    def _notice(self, row, text, task_id, purpose='notice'):
        # Chat flood control must not hold transport completion or executor slots.
        if hasattr(self.channels[row['channel']], 'prepare_reply'):
            self._queue_send(row, text, task_id, purpose=purpose)
            return
        self._track_notice(asyncio.create_task(self._send(row, text, task_id)), task_id)

    def _track_notice(self, job, task_id):
        self.notice_jobs.setdefault(task_id, set()).add(job)
        def forget(done):
            jobs = self.notice_jobs.get(task_id)
            if jobs is not None:
                jobs.discard(done)
                if not jobs:
                    self.notice_jobs.pop(task_id, None)
        job.add_done_callback(forget)

    def _kick(self, task_id):
        if not self.closing and (task_id not in self.workers or self.workers[task_id].done()):
            job = asyncio.create_task(self._work(task_id))
            self.workers[task_id] = job
            def forget(done):
                if self.workers.get(task_id) is done:
                    self.workers.pop(task_id)
                if not done.cancelled() and (error := done.exception()):
                    log.error("Task %s worker failed", task_id, exc_info=(type(error), error, error.__traceback__))
            job.add_done_callback(forget)

    def _label(self, row, task_id):
        task = self.storage.task(row['conversation_id'], task_id)
        current = self.storage.active_task(row['conversation_id'])
        if len(self.storage.tasks(row['conversation_id'])) > 1 or current is None or current['id'] != task_id:
            return f"#t{task_id} {task_title(task)}\n"
        return ""

    def _queue_send(self, row, text, task_id=None, buttons=None, purpose='reply'):
        channel = self.channels[row['channel']]
        if task_id is not None:
            text = self._label(row, task_id) + text
        logical = f"inbound:{row['id']}:{purpose}"
        channel.prepare_reply(row['chat_id'], text, logical, task_id, None, buttons)
        job = asyncio.create_task(self._retry_notice(channel, row['chat_id'], logical))
        self.notice_jobs.setdefault(task_id, set()).add(job)
        job.add_done_callback(lambda done: self.notice_jobs.get(task_id, set()).discard(done))

    async def _retry_notice(self, channel, chat, logical):
        try:
            async with asyncio.timeout(self.delivery_timeout):
                for delay in (0, *self.delivery_retry_delays):
                    if delay:
                        await asyncio.sleep(delay)
                    await channel.delivery.flush(chat, logical)
                    if not channel.delivery.problems(chat, logical)[2]:
                        return
        except TimeoutError:
            log.warning('Notice delivery remains pending after bounded retries')

    async def _send(self, row, text, task_id=None, buttons=None, purpose='reply'):
        channel = self.channels[row['channel']]
        if hasattr(channel, 'prepare_reply'):
            # Intent is durable under the conversation lock; network delivery
            # never prevents polling from admitting a later /stop command.
            self._queue_send(row, text, task_id, buttons, purpose)
            return
        if task_id is not None:
            text = self._label(row, task_id) + text
        args = (row['chat_id'], text) + ((buttons,) if buttons is not None else ())
        mid = await safe_call(channel.send, *args)
        if task_id is not None:
            self._remember(row, mid, task_id)

    def _remember(self, row, message_id, task_id, turn_id=None):
        if message_id is None:
            return
        # A channel that splits long messages reports every part, so a reply to
        # any part finds the task.
        parts = getattr(self.channels[row['channel']], 'parts', None)
        ids = parts(row['chat_id'], message_id) if parts else [message_id]
        self.storage.remember_outbound(row['channel'], row['chat_id'], ids or [message_id], task_id, turn_id)

    def _reply(self, row, task_id, turn_id):
        reply = Reply(self.channels[row['channel']], row['chat_id'], self.edit_interval,
                      lambda: self._label(row, task_id),
                      lambda mid: self._remember(row, mid, task_id, turn_id), f'turn:{turn_id}', task_id, turn_id)
        self.replies.add(reply)
        return reply

    async def _prepare_output(self, row, turn_id, text, *, prepare_reply=True):
        saved = self.storage.output(turn_id)
        if saved is not None:
            return saved
        if hasattr(self.channels[row['channel']], 'deliver_files'):
            text, artifacts = await asyncio.to_thread(snapshot_outputs, self.storage.state_dir, turn_id, text)
        else:
            artifacts = []
        output = self.storage.save_output(turn_id, text or ('Files attached.' if artifacts else 'Task completed without a text answer.'), artifacts)
        if prepare_reply:
            self._prepare_final_reply(row, self.storage.turn_for(row['id'])['task_id'], turn_id, output['text'])
        return output

    def _prepare_final_reply(self, row, task_id, turn_id, text):
        channel = self.channels[row['channel']]
        if hasattr(channel, 'prepare_final'):
            channel.prepare_final(row['chat_id'], self._label(row, task_id) + text, turn_id, task_id)

    def _schedule_output(self, row, task_id, turn_id):
        if turn_id in self.output_jobs or self.closing:
            return
        job = asyncio.create_task(self._deliver_output(row, task_id, turn_id))
        self.output_jobs[turn_id] = job
        job.add_done_callback(lambda done: self.output_jobs.pop(turn_id, None))

    async def _deliver_output(self, row, task_id, turn_id, reply=None):
        output = self.storage.output(turn_id)
        if output is None or output['cancelled']:
            return
        self._prepare_final_reply(row, task_id, turn_id, output['text'])
        channel = self.channels[row['channel']]
        if hasattr(channel, 'deliver_files'):
            logical = f'turn:{turn_id}:final'
            unknown, failed, pending = 0, 0, 0
            try:
                async with asyncio.timeout(self.delivery_timeout):
                    for delay in (0, *self.delivery_retry_delays):
                        if delay:
                            await asyncio.sleep(delay)
                        if self.storage.output(turn_id)['cancelled']:
                            return
                        await channel.deliver_files(row['chat_id'], json.loads(output['artifacts']), logical, task_id, turn_id)
                        unknown, failed, pending = channel.delivery.problems(row['chat_id'], logical)
                        if not pending:
                            break
            except TimeoutError:
                unknown, failed, pending = channel.delivery.problems(row['chat_id'], logical)
            if channel.delivery.settled(row['chat_id'], logical):
                self.storage.output_delivered(turn_id)
                if channel.delivery.problems(row['chat_id'], f'turn:{turn_id}')[0]:
                    self._notice(row, 'Earlier progress delivery was unconfirmed. The final reply was sent.', task_id, 'preview-status')
            if unknown or failed or pending:
                message = ('Some reply parts have unconfirmed delivery and were not resent. ' if unknown else '')
                message += ('Some reply parts were refused or unavailable. ' if failed else '')
                message += ('Some reply parts are still pending after delivery retries. ' if pending else '')
                message += 'The result is retained in Eivoh.'
                self._notice(row, message, task_id, 'output-status')
            return
        owned = reply is None
        reply = reply or self._reply(row, task_id, turn_id)
        try:
            reply.update(output['text'], final=True)
            await reply.finish()
            self.storage.output_delivered(turn_id)
        finally:
            if owned:
                await reply.cancel()
                self.replies.discard(reply)

    def _target(self, row, explicit=None):
        conv = row['conversation_id']
        if explicit is not None:
            if not explicit.isdecimal() or not 0 < int(explicit) < 2**63:
                raise ValueError("Use a positive task number.")
            task = self.storage.task(conv, int(explicit))
            if task is None:
                raise ValueError(f"Unknown task {explicit} in this conversation.")
            return task
        return self.storage.reply_task(conv, row['reply_to']) or self.storage.active_task(conv)

    def _state(self, task):
        turns = self.storage.pending_task(task['id'])
        statuses = [self.storage.turn_for(row['id'])['status'] for row in turns]
        queued = statuses.count('queued')
        return ', '.join((['running'] if 'running' in statuses else []) +
                         ([f'queued {queued}'] if queued else [])) or 'idle'

    def _describe(self, task):
        return f"{task['id']} · {literal(task_title(task))} — {self._mode(task)} — {self._state(task)}"

    def _mode(self, task):
        name = task['executor'] if task else self.default_executor
        model = task['model'] if task else None
        effort = task['effort'] if task else None
        return f"{name} / {model or 'default'} · {effort or 'default'}"

    def _settings_buttons(self, task):
        model, effort = self._mode(task).rsplit(' · ', 1)
        return [[(model, f"/model #{task['id']}"), (f'Effort: {effort}', f"/effort #{task['id']}")]]

    def _next_launch(self, task):
        return ' Applies from the next turn launch; the running turn keeps its settings.' if 'running' in self._state(task) else ''

    async def _command(self, row, *, button=False):
        cmd = command(row['text'])
        parts = row['text'].split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ''
        task = None
        try:
            if cmd == '/new':
                words = arg.split(maxsplit=1)
                name = None
                if words and words[0].lower() in self.executors:
                    name = words[0].lower()
                    arg = words[1] if len(words) > 1 else ''
                task = self.storage.new_task(row['id'], name, ' '.join(arg.split()) or None)
                await self._send(row, f"New task {self._describe(task)}.", task['id'], buttons=self._settings_buttons(task))
            elif cmd == '/tasks' or (cmd == '/task' and not arg):
                await self._tasks(row, arg)
            elif cmd == '/task':
                task = self._target(row, arg)
                self.storage.switch_task(row['id'], task['id'])
                lines = [f"Current task {self._describe(task)}."]
                user, answer = self.storage.last_exchange(task['id'])
                if user:
                    lines.append('Last message: ' + snippet(strip_voice(user)))
                if answer:
                    lines.append('Last answer: ' + snippet(answer))
                await self._send(row, '\n'.join(lines), task['id'], buttons=self._settings_buttons(task))
            elif cmd in {'/stop', '/close'}:
                if cmd == '/stop' and arg == 'all':
                    task = None
                else:
                    task = self._target(row, arg or None)
                    if task is None:
                        raise ValueError('No current task.')
                await self._stop(row, task, close=cmd == '/close')
            elif cmd == '/title':
                task = self._target(row)
                if task is None:
                    raise ValueError('No current task.')
                if not arg:
                    raise ValueError('Use /title <text>.')
                self.storage.title_task(row['id'], task['id'], ' '.join(arg.split()))
                await self._send(row, f"Task {task['id']} renamed.", task['id'])
            elif cmd in {'/model', '/effort'}:
                words = arg.split(maxsplit=1)
                new_task = bool(words and words[0] == '#new')
                if words and words[0].startswith('#'):
                    task = None if new_task else self._target(row, words[0][1:])
                    arg = words[1] if len(words) > 1 else ''
                elif button or row['message_id'].startswith('cb:'):
                    raise ValueError('This settings button has no task target. Open /model or /effort again.')
                else:
                    task = self._target(row)
                handler = self._model if cmd == '/model' else self._effort
                await handler(row, task, arg, new_task=new_task)
            elif cmd == '/q':
                raise ValueError('Use /q <text> to queue a message after the running turn.')
            else:
                raise ValueError('Unknown command. Use /tasks, /new, /task, /model, /effort, /stop, /close or /title.')
        except ValueError as exc:
            self.storage.finish_command(row['id'])
            await self._send(row, str(exc), task['id'] if task else None)

    async def _tasks(self, row, arg):
        parts = arg.split()
        include_closed = bool(parts and parts[0] == 'all')
        if include_closed:
            parts = parts[1:]
        if len(parts) > 1 or (parts and (not parts[0].isdecimal() or int(parts[0]) < 1)):
            raise ValueError('Use /tasks [all] [page].')
        tasks = self.storage.tasks(row['conversation_id'], include_closed)
        pages = max(1, (len(tasks) + 7) // 8)
        page = min(int(parts[0]) if parts else 1, pages)
        shown = tasks[(page - 1) * 8:page * 8]
        current = self.storage.active_task(row['conversation_id'])
        opened = sum(t['status'] == 'active' for t in tasks)
        lines = [f"Tasks: {opened} open" + (f", page {page}/{pages}" if pages > 1 else '') + '.']
        buttons = []
        for task in shown:
            marker = '▶' if current and current['id'] == task['id'] else '·'
            state = 'closed' if task['status'] == 'closed' else self._state(task)
            lines.append(f"\n{marker} {task['id']} · {literal(task_title(task))}\n    {self._mode(task)}"
                         f" · {state} · {relative_time(task['last_active'])}{lifetime(task)}")
            buttons.append([(f"{task['id']} · {task_title(task)}", f"/task {task['id']}")])
        if not shown:
            lines.append('No tasks. Use /new or send a message.')
        more = opened - sum(t['status'] == 'active' for t in shown)
        if more:
            lines.append(f"\n{more} more open task(s) on other pages.")
        if pages > 1:
            prefix = '/tasks all' if include_closed else '/tasks'
            navigation = []
            if page > 1:
                navigation.append(('‹ Newer', f'{prefix} {page - 1}'))
            if page < pages:
                navigation.append(('Older ›', f'{prefix} {page + 1}'))
            buttons.append(navigation)
        self.storage.finish_command(row['id'])
        await self._send(row, '\n'.join(lines), buttons=buttons)

    async def _stop(self, row, task, close=False):
        task_id = task['id'] if task else None
        current = self.storage.active_task(row['conversation_id']) if close else None
        active, queued = self.storage.stop(row['id'], task_id, close)
        targets = [task_id] if task else [t['id'] for t in self.storage.tasks(row['conversation_id'], True)]
        jobs = [jobs[t] for t in targets for jobs in (self.workers, self.title_jobs) if t in jobs]
        jobs += [job for t in targets for job in self.steer_jobs.get(t, ())]
        jobs += [job for t in targets for job in self.notice_jobs.get(t, ())]
        jobs += [job for inbound_id, job in self.media_jobs.items()
                 if self.storage.turn_for(inbound_id)['task_id'] in targets]
        jobs += [job for turn_id, job in self.output_jobs.items()
                 if self.storage.db.execute('SELECT task_id FROM turn WHERE id=?', (turn_id,)).fetchone()[0] in targets]
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        for run_id, _ in active:
            await self._cancel_run(run_id)
        description = f"Closed task {task_id}." if close else f"Stopped task {task_id}." if task else "Stopped all tasks."
        if current and current['id'] == task_id:
            current = self.storage.active_task(row['conversation_id'])
            description += f" Current task {self._describe(current)}." if current else ' No current task.'
        await self._send(row, f"{description} Cancelled {queued} queued message(s).", task_id)

    async def _work(self, task_id):
        while not self.closing:
            pending = self.storage.pending_task(task_id)
            if not pending:
                return
            row = pending[0]
            if row['preparation'] == 'pending':
                return  # Preparation completion kicks this immutable task again.
            # Do not claim a turn while waiting: queued stays queued, and /model
            # may still change its launch settings until the slot is acquired.
            # The slot bounds executor processes only: it is released before the
            # final delivery, which may wait on the channel's flood control.
            await self.slots.acquire()
            held = [True]
            def release():
                if held.pop() if held else False:
                    self.slots.release()
            try:
                if self.closing:
                    return
                pending_turn = self.storage.turn_for(row['id'])
                if pending_turn['status'] not in {'queued', 'running'}:
                    continue
                if row['preparation'] == 'failed':
                    await self._prepare_output(row, pending_turn['id'], row['prepared_text'])
                    self.storage.finish(pending_turn['id'], 'failed', 'Attachment preparation failed')
                    release()
                    if hasattr(self.channels[row['channel']], 'deliver_files'):
                        self._schedule_output(row, task_id, pending_turn['id'])
                    else:
                        await self._deliver_output(row, task_id, pending_turn['id'])
                    continue
                result = self.storage.stored_result(pending_turn['id'])
                prepared_output = self.storage.output(pending_turn['id'])
                if result is not None or prepared_output is not None:
                    text, error = result if result is not None else (prepared_output['text'], 'Recovered prepared terminal failure')
                    await self._prepare_output(row, pending_turn['id'], (text + f'\n\nTask failed: {error}' if error else text))
                    self.storage.finish(pending_turn['id'], 'failed' if error else 'completed', error)
                    release()
                    if not error:
                        self._name_task(task_id)
                    if hasattr(self.channels[row['channel']], 'deliver_files'):
                        self._schedule_output(row, task_id, pending_turn['id'])
                    else:
                        await self._deliver_output(row, task_id, pending_turn['id'])
                    continue
                turn, recovering = self.storage.claim(row['id'], self.max_recoveries)
                if turn is None:
                    await self._send(row, 'Interrupted task failed: restart recovery limit reached. Send a new message to retry.', task_id)
                    continue
                await self._run(row, turn, recovering, release)
            finally:
                release()

    async def _model_menu(self, row, task, name=None, page=1):
        target = f"#{task['id']}" if task else '#new'
        prefix = f'/model {target}'
        choices = {executor: models for executor, models in catalog().items() if executor in self.executors}
        buttons = []
        lines = [f"Model: {self._mode(task)}."]
        if name is not None:
            choices = {name: choices.get(name, [])}
            models = choices[name]
            pages = max(1, (len(models) + 7) // 8)
            page = min(page, pages)
            start = (page - 1) * 8
            lines.append(f"\n{name}, page {page}/{pages}:")
            for number, (model, description) in enumerate(models[start:start + 8], start + 1):
                lines.append(f"{number}. {model}" + (f" — {description}" if description else ''))
                data = f'{prefix} {name} {model}'
                if len(data.encode('utf-8')) <= 64:
                    buttons.append([(model, data)])
            if not models:
                lines.append('No models available.')
            navigation = []
            if page > 1:
                navigation.append(('‹', f'{prefix} {name} page {page - 1}'))
            if page < pages:
                navigation.append(('›', f'{prefix} {name} page {page + 1}'))
            if navigation:
                buttons.append(navigation)
        else:
            number = 0
            for executor, models in choices.items():
                lines.append(f"\n{executor}:")
                for model, description in models:
                    number += 1
                    lines.append(f"{number}. {model}" + (f" — {description}" if description else ''))
                if not models:
                    lines.append('No models available.')
            buttons = [[(executor, f'{prefix} {executor}')] for executor in self.executors]
            buttons.append([(f"Effort: {task['effort'] or 'default'}" if task else 'Effort: default', f'/effort {target}')])
        self.model_catalogs[row['conversation_id']] = choices
        lines.append(f'\nChoose with {prefix} <name or number>.')
        if task is None:
            lines.append('Selecting creates a new task with executor defaults.')
        self.storage.finish_command(row['id'])
        await self._send(row, '\n'.join(lines), task['id'] if task else None, buttons=buttons)

    async def _model(self, row, task, arg, *, new_task=False):
        parts = arg.split(maxsplit=1)
        name = parts[0].lower() if parts else None
        if not arg:
            return await self._model_menu(row, task)
        choices = None
        if name in self.executors or name in catalog():
            if name not in self.executors:
                raise ValueError(f'Executor {name} is not configured.')
            if len(parts) == 1:
                return await self._model_menu(row, task, name)
            tail = parts[1].split()
            if tail[0] == 'page' and len(tail) > 1:
                if len(tail) != 2 or not tail[1].isdecimal() or int(tail[1]) < 1:
                    raise ValueError('Use /model <executor> page <positive number>.')
                return await self._model_menu(row, task, name, int(tail[1]))
            choices = {name: catalog().get(name, [])}
            arg = parts[1]
        elif arg.isdecimal():
            choices = self.model_catalogs.get(row['conversation_id'])
            if choices is None:
                raise ValueError('Use /model to list numbered choices first (also after a restart).')
        name, model = resolve(arg, choices)
        if name not in self.executors:
            raise ValueError(f'Executor {name} is not configured.')
        reset = bool(task and task['effort'] and task['effort'] not in effort_levels(name, model))
        task, _ = self.storage.set_model(row['id'], name, model, task['id'] if task else None,
                                         reset_effort=reset, new_task=new_task)
        _, count, truncated = self.storage.missed_transcript(task['id'], name, row['id'])
        reply = f"Model: {self._mode(task)}. "
        reply += (f"Context will be passed as a transcript of {count} earlier turns." if count else 'Context kept (same session).')
        if truncated:
            reply += ' Transcript truncated: older turns or parts of the newest turn omitted.'
        if reset:
            reply += ' Effort reset to default because the selected executor/model does not support it.'
        reply += self._next_launch(task)
        await self._send(row, reply, task['id'], buttons=self._settings_buttons(task))

    async def _effort(self, row, task, arg, *, new_task=False):
        name = task['executor'] if task else self.default_executor
        levels = effort_levels(name, task['model'] if task else None)
        prefix = f"/effort #{task['id']}" if task else '/effort #new'
        if not arg:
            self.storage.finish_command(row['id'])
            buttons = [[(level, f'{prefix} {level}')] for level in [*levels, 'default']
                       if len(f'{prefix} {level}'.encode('utf-8')) <= 64]
            await self._send(row, f"Effort: {self._mode(task)}.\nSupported: {', '.join([*levels, 'default'])}."
                             f'\nChoose with {prefix} <level or default>.' +
                             ('\nSelecting creates a new task with executor defaults.' if task is None else ''),
                             task['id'] if task else None, buttons=buttons)
            return
        if arg != 'default' and arg not in levels:
            raise ValueError(f"Unsupported effort `{arg}`. Supported: {', '.join([*levels, 'default'])}.")
        task = self.storage.set_effort(row['id'], None if arg == 'default' else arg, task['id'] if task else None,
                                      new_task=new_task)
        await self._send(row, f"Effort: {self._mode(task)}." + self._next_launch(task), task['id'],
                         buttons=self._settings_buttons(task))

    async def _typing(self, channel, chat):
        # Studied reference: nanobot (MIT), nanobot/channels/telegram/runtime.py.
        # Own generic implementation; no copied source.
        while True:
            await safe_call(channel.typing, chat)
            await asyncio.sleep(self.typing_interval)

    async def _run(self, row, turn, recovering, release=lambda: None):
        channel, chat = self.channels[row["channel"]], row["chat_id"]
        reply = self._reply(row, turn['task_id'], turn['id'])
        typing = asyncio.create_task(self._typing(channel, chat))
        text, tool, error = "", "", None
        completed = False
        current_deltas = False
        try:
            request = self.storage.input_text(row['id'])
            if hasattr(channel, 'deliver_files'):
                request += output_instructions(self.storage.state_dir, turn['id'])
            prompt = self.storage.prepare_prompt(turn, request)
            if recovering and turn['resume_id']:
                prompt = f"{RESTART_PROMPT}\n\nOriginal request:\n{prompt}" + self.storage.recovery_inputs(turn['id'])
            if recovering:
                await self._send(row, f"Service restarted; recovering task {turn['task_id']} (attempt {turn['recovery_count']}/{self.max_recoveries}).", turn['task_id'], purpose='recovery')
            executor = self.executors.get(turn["executor"])
            if executor is None:
                raise RuntimeError(f"Executor {turn['executor']} is not configured")
            stream = (executor.resume(turn["id"], turn["resume_id"], prompt, model=turn["model"], effort=turn["effort"])
                      if turn["resume_id"] else executor.start(turn["id"], prompt, model=turn["model"], effort=turn["effort"]))
            async def consume():
                nonlocal text, tool, error, completed, current_deltas
                async with aclosing(stream):
                    async for record in stream:
                        self.storage.append(turn["id"], record, allow_result=not self.closing)
                        for event in record.events:
                            data = event.data
                            if event.kind == "text_delta":
                                text = data['text'] if data.get('replace') else text + data['text']
                                current_deltas = True
                            elif event.kind == "message":
                                if data.get('replace'):
                                    text = data['text']
                                elif not current_deltas and data["text"]:
                                    text += data["text"]
                                current_deltas = False
                            elif event.kind == "tool_started":
                                tool = data.get("name", "tool")
                            elif event.kind == "tool_finished":
                                tool = ""
                            elif event.kind == "approval_requested":
                                tool = "Permission denied; change executor permissions explicitly if needed."
                            elif event.kind == "error":
                                # Keep the first error: later ones (exit code) are consequences.
                                error = error or str(data.get("message", "Executor error"))
                            elif event.kind == "turn_completed":
                                completed = data["status"] == "completed"
                                # Result is the authoritative final answer (no duplicated deltas).
                                # Replay also includes durable complete parts from
                                # earlier attempts, which the new process cannot know.
                                saved = self.storage.stored_result(turn['id'])
                                text = saved[0] if saved is not None else data.get('text') or text
                                tool = ""
                        reply.update(text, tool)

            consumer = asyncio.create_task(consume())
            self.active_runs[turn['task_id']] = (turn, executor)
            try:
                await asyncio.shield(consumer)
            except asyncio.CancelledError:
                # Let the reader persist all buffered lines after terminating the
                # tree. Cancelling the iterator itself would discard its queue.
                await executor.cancel(turn["id"])
                await consumer
                raise
            self.active_runs.pop(turn['task_id'], None)
            if deliveries := self.steer_jobs.get(turn['task_id']):
                await asyncio.gather(*list(deliveries))
            if self.closing:
                return  # Exit/error during shutdown is recoverable, not terminal.
            if not completed and error is None:
                error = "Executor ended without a completed turn"
            final_text = text + f'\n\nTask failed: {error}' if error else text
            await self._prepare_output(row, turn['id'], final_text, prepare_reply=not hasattr(channel, 'deliver_files'))
            self.storage.finish(turn["id"], "failed" if error else "completed", error)
            release()
            if not error:
                self._name_task(turn['task_id'])
            if hasattr(channel, 'deliver_files'):
                # Allow an in-flight preview to acknowledge before adopting its
                # id. Immediate cancellation could make a normal fast answer
                # ambiguous. The bounded drain remains interruptible by /stop.
                await reply.finish()
                await reply.cancel()
                self._schedule_output(row, turn['task_id'], turn['id'])
            else:
                await self._deliver_output(row, turn['task_id'], turn['id'], reply)
        except asyncio.CancelledError:
            # /stop has already persisted cancellation; shutdown leaves running.
            raise
        except Exception as exc:
            if self.closing:
                return
            log.exception("Turn %s failed", turn["id"])
            self.storage.finish(turn["id"], "failed", str(exc))
            await self._send(row, f"Task {turn['task_id']} failed: {exc}", turn['task_id'])
        finally:
            self.active_runs.pop(turn['task_id'], None)
            typing.cancel()
            await asyncio.gather(typing, return_exceptions=True)
            await reply.cancel()
            self.replies.discard(reply)
            await self._cancel_run(turn["id"])

    def _name_task(self, task_id):
        if self.closing or task_id in self.title_jobs:
            return
        models = {name: titles.MODELS[name] for name in self.title_executors if name in titles.MODELS}
        request = self.storage.claim_title(task_id, models)
        if request is None:
            return
        job = asyncio.create_task(self._generate_title(request))
        self.title_jobs[task_id] = job
        def forget(done):
            self.title_jobs.pop(task_id, None)
            if done.cancelled():
                # Also covers cancellation before the coroutine's first step.
                self.storage.title_event(request, 'cancelled')
            elif done.exception():
                log.error('Task %s title bookkeeping failed', task_id)
        job.add_done_callback(forget)

    async def _generate_title(self, request):
        executor = self.title_executors[request['executor']]
        run_id = -request['task_id']  # Disjoint from normal turn ids, even in fakes.
        def observe_usage(data):
            # Retain the last reported snapshot, including on later cancellation.
            # Do not add repeated totals or infer unknown subscription consumption.
            request['title_usage'] = data
        try:
            # Includes slot wait: background work cannot build an unbounded queue
            # or monopolize the executor budget during a provider outage.
            async with asyncio.timeout(self.title_timeout):
                async with self.slots:
                    try:
                        title = await titles.generate(executor, run_id, request, observe_usage)
                    finally:
                        await executor.cancel(run_id)
            if title:
                self.storage.generated_title(request, title)
            else:
                self.storage.title_event(request, 'invalid_or_failed')
        except TimeoutError:
            self.storage.title_event(request, 'timeout')
        except asyncio.CancelledError:
            raise
        except Exception:
            # CLI errors can contain credentials or pages of provider HTML. Keep
            # only the outcome; the user's successful answer is unaffected.
            self.storage.title_event(request, 'failed')

    async def idle(self):
        """Wait for accepted work; useful to embedding callers and tests."""
        while any(not job.done() for job in [*self.workers.values(), *self.media_jobs.values(), *self.output_jobs.values()]):
            results = await asyncio.gather(*list(self.workers.values()), *list(self.media_jobs.values()), *list(self.output_jobs.values()), return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    raise result

    def begin_shutdown(self):
        # Called synchronously by the signal handler, before awaiting poll cleanup.
        self.closing = True

    async def shutdown(self):
        self.begin_shutdown()
        jobs = [*self.workers.values(), *self.title_jobs.values(),
                *self.media_jobs.values(), *self.output_jobs.values(),
                *(job for jobs in self.steer_jobs.values() for job in jobs),
                *(job for jobs in self.notice_jobs.values() for job in jobs)]
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        await asyncio.gather(*(e.shutdown() for e in (*self.executors.values(), *self.title_executors.values())))


def snippet(text, limit=200):
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def strip_voice(text):
    return text.split('\n', 1)[1] if text.startswith('[Voice message, speech-to-text]\n') else text


def literal(text):
    """Show owner text as written: channels render Markdown, a code span is literal."""
    return '`' + text.replace('`', "'") + '`'


def task_title(task):
    return snippet(task['title'] or 'Untitled', 40)


def _stamp(value):
    parsed = datetime.fromisoformat(value)
    # SQLite CURRENT_TIMESTAMP is naive UTC; ISO timestamps retain their offset.
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def compact(seconds):
    if seconds < 3600:
        return f'{seconds // 60}m'
    if seconds < 86400:
        return f'{seconds // 3600}h'
    return f'{seconds // 86400}d'


def lifetime(task):
    """How long the task has been worked on: from creation to its last activity."""
    seconds = int((_stamp(task['last_active']) - _stamp(task['created_at'])).total_seconds())
    return f' · over {compact(seconds)}' if seconds >= 60 else ''


def relative_time(value):
    seconds = max(0, int((datetime.now(timezone.utc) - _stamp(value)).total_seconds()))
    return 'just now' if seconds < 60 else compact(seconds) + ' ago'
