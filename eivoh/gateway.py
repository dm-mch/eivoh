"""Channel-neutral routing, durable queues and executor lifecycle."""
import asyncio
from contextlib import aclosing, suppress
from datetime import datetime, timezone
import logging

from .channels.base import Channel, Inbound
from .executors.base import Executor
from .models import catalog, resolve
from .storage import Storage, command

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
    def __init__(self, channel, chat_id, interval, prefix=lambda: "", sent_callback=lambda mid: None):
        self.channel, self.chat_id, self.interval = channel, chat_id, interval
        self.prefix, self.sent_callback = prefix, sent_callback
        self.text = ""
        self.tool = ""
        self.sent = None
        self.message_id = None
        self.changed = asyncio.Event()
        self.closed = False
        self.job = asyncio.create_task(self.run())

    def update(self, text, tool=""):
        self.text, self.tool = text, tool
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
                if self.message_id is None:
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
                 edit_interval=1.5, typing_interval=4, max_recoveries=3, executors=None, max_parallel=3):
        # `executor` is the default one; `executors` adds named alternatives.
        self.default_executor = storage.default_executor
        self.executors = {**(executors or {}), self.default_executor: executor}
        self.storage, self.channels = storage, channels
        self.edit_interval, self.typing_interval = edit_interval, typing_interval
        self.max_recoveries = max_recoveries
        if not isinstance(max_parallel, int) or max_parallel < 1:
            raise ValueError("max_parallel must be an integer >= 1")
        self.slots = asyncio.Semaphore(max_parallel)
        self.replies = set()
        self.workers = {}
        self.locks = {}
        self.model_catalogs = {}  # Numbers refer to the last list shown in this process.
        self.closing = False

    async def start(self):
        # Commands have no turn. Replay them in journal order before any launch.
        for row in self.storage.pending():
            if self.storage.turn_for(row['id']) is None:
                await self._command(row)
        for row in self.storage.pending():
            self._kick(self.storage.turn_for(row['id'])['task_id'])

    async def receive(self, message: Inbound):
        if self.closing:
            return
        # Include journaling under the lock: an arrival during /stop must bind
        # after earlier commands have taken effect, and never be rebound later.
        key = (message.channel, message.chat_id)
        async with self.locks.setdefault(key, asyncio.Lock()):
            if self.closing:
                return
            row = self.storage.journal(message)
            if row is None:
                return
            turn = self.storage.turn_for(row['id'])
            if turn is None:
                await self._command(row)
            else:
                self._kick(turn['task_id'])
            for reply in self.replies:
                reply.changed.set()  # A second task or switch may change labels.

    @property
    def executor(self):
        return self.executors[self.default_executor]

    @executor.setter
    def executor(self, value):
        self.executors[self.default_executor] = value

    async def _cancel_run(self, run_id):
        # Run ids are turn ids, unique across executors; cancel is a no-op elsewhere.
        await asyncio.gather(*(e.cancel(run_id) for e in self.executors.values()))

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

    async def _send(self, row, text, task_id=None, buttons=None):
        if task_id is not None:
            text = self._label(row, task_id) + text
        args = (row['chat_id'], text) + ((buttons,) if buttons is not None else ())
        mid = await safe_call(self.channels[row['channel']].send, *args)
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
                      lambda mid: self._remember(row, mid, task_id, turn_id))
        self.replies.add(reply)
        return reply

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
        return f"{task['id']} · {literal(task_title(task))} — {task['executor']}/{task['model'] or 'default'} — {self._state(task)}"

    async def _command(self, row):
        cmd = command(row['text'])
        parts = row['text'].split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ''
        task = None
        try:
            if cmd == '/new':
                words = arg.split(maxsplit=1)
                name = self.default_executor
                if words and words[0].lower() in self.executors:
                    name = words[0].lower()
                    arg = words[1] if len(words) > 1 else ''
                task = self.storage.new_task(row['id'], name, ' '.join(arg.split()) or None)
                await self._send(row, f"New task {self._describe(task)}.", task['id'])
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
                await self._send(row, '\n'.join(lines), task['id'])
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
            elif cmd == '/model':
                task = self._target(row)
                await self._model(row, task, arg)
            else:
                raise ValueError('Unknown command. Use /tasks, /new, /task, /model, /stop, /close or /title.')
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
            lines.append(f"\n{marker} {task['id']} · {literal(task_title(task))}\n    {task['executor']}/{task['model'] or 'default'}"
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
        active, queued = self.storage.stop(row['id'], task_id, close)
        targets = [task_id] if task else [t['id'] for t in self.storage.tasks(row['conversation_id'], True)]
        jobs = [self.workers[t] for t in targets if t in self.workers]
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        for run_id, _ in active:
            await self._cancel_run(run_id)
        description = f"Closed task {task_id}." if close else f"Stopped task {task_id}." if task else "Stopped all tasks."
        await self._send(row, f"{description} Cancelled {queued} queued message(s).", task_id)

    async def _work(self, task_id):
        while not self.closing:
            pending = self.storage.pending_task(task_id)
            if not pending:
                return
            row = pending[0]
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
                result = self.storage.stored_result(pending_turn['id'])
                if result is not None:
                    text, error = result
                    self.storage.finish(pending_turn['id'], 'failed' if error else 'completed', error)
                    release()
                    reply = self._reply(row, task_id, pending_turn['id'])
                    reply.update((text + f"\n\nTask failed: {error}" if error else text) or 'Task completed without a text answer.')
                    try:
                        await reply.finish()
                    finally:
                        await reply.cancel()
                        self.replies.discard(reply)
                    continue
                turn, recovering = self.storage.claim(row['id'], self.max_recoveries)
                if turn is None:
                    await self._send(row, 'Interrupted task failed: restart recovery limit reached. Send a new message to retry.', task_id)
                    continue
                await self._run(row, turn, recovering, release)
            finally:
                release()

    async def _model(self, row, task, arg):
        conv = row['conversation_id']
        if not arg:
            name = task['executor'] if task else self.default_executor
            model = task['model'] if task else None
            choices = catalog()
            self.model_catalogs[conv] = choices
            lines = [f"Model: {name} / {model or 'default'}."]
            number = 0
            for executor, models in choices.items():
                lines.append(f"\n{executor}:")
                for model_id, description in models:
                    number += 1
                    lines.append(f"{number}. {model_id}" + (f" — {description}" if description else ''))
                if not models:
                    lines.append('No models available.')
            lines.append('\nChoose with /model <name or number>.')
            reply = '\n'.join(lines)
            self.storage.finish_command(row['id'])
        else:
            choices = None
            if arg.isdecimal():
                choices = self.model_catalogs.get(conv)
                if choices is None:
                    raise ValueError('Use /model to list numbered choices first (also after a restart).')
            name, model = resolve(arg, choices)
            if name not in self.executors:
                raise ValueError(f'Executor {name} is not configured.')
            task, _ = self.storage.set_model(row['id'], name, model, task['id'] if task else None)
            _, count, truncated = self.storage.missed_transcript(task['id'], name, row['id'])
            reply = f"Model: {name} / {model}. "
            reply += (f"Context will be passed as a transcript of {count} earlier turns." if count else 'Context kept (same session).')
            if truncated:
                reply += ' Transcript truncated: older turns or parts of the newest turn omitted.'
        await self._send(row, reply, task['id'] if task else None)

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
            prompt = self.storage.prepare_prompt(turn, row['text'])
            if recovering and turn['resume_id']:
                prompt = f"{RESTART_PROMPT}\n\nOriginal request:\n{prompt}"
            if recovering:
                await self._send(row, f"Service restarted; recovering task {turn['task_id']} (attempt {turn['recovery_count']}/{self.max_recoveries}).", turn['task_id'])
            executor = self.executors.get(turn["executor"])
            if executor is None:
                raise RuntimeError(f"Executor {turn['executor']} is not configured")
            stream = (executor.resume(turn["id"], turn["resume_id"], prompt, model=turn["model"])
                      if turn["resume_id"] else executor.start(turn["id"], prompt, model=turn["model"]))
            async def consume():
                nonlocal text, tool, error, completed, current_deltas
                async with aclosing(stream):
                    async for record in stream:
                        self.storage.append(turn["id"], record, allow_result=not self.closing)
                        for event in record.events:
                            data = event.data
                            if event.kind == "text_delta":
                                text += data["text"]
                                current_deltas = True
                            elif event.kind == "message":
                                if not current_deltas and data["text"]:
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
                                text = data.get("text") or text
                                tool = ""
                        reply.update(text, tool)

            consumer = asyncio.create_task(consume())
            try:
                await asyncio.shield(consumer)
            except asyncio.CancelledError:
                # Let the reader persist all buffered lines after terminating the
                # tree. Cancelling the iterator itself would discard its queue.
                await executor.cancel(turn["id"])
                await consumer
                raise
            if self.closing:
                return  # Exit/error during shutdown is recoverable, not terminal.
            if not completed and error is None:
                error = "Executor ended without a completed turn"
            self.storage.finish(turn["id"], "failed" if error else "completed", error)
            release()
            if error:
                text += f"\n\nTask failed: {error}"
            reply.update(text or "Task completed without a text answer.")
            await reply.finish()
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
            typing.cancel()
            await asyncio.gather(typing, return_exceptions=True)
            await reply.cancel()
            self.replies.discard(reply)
            await self._cancel_run(turn["id"])

    async def idle(self):
        """Wait for accepted work; useful to embedding callers and tests."""
        while any(not job.done() for job in self.workers.values()):
            results = await asyncio.gather(*list(self.workers.values()), return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    raise result

    def begin_shutdown(self):
        # Called synchronously by the signal handler, before awaiting poll cleanup.
        self.closing = True

    async def shutdown(self):
        self.begin_shutdown()
        for job in self.workers.values():
            job.cancel()
        await asyncio.gather(*self.workers.values(), return_exceptions=True)
        await asyncio.gather(*(e.shutdown() for e in self.executors.values()))


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
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


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
