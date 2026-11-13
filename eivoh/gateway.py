"""Channel-neutral routing, durable queues and executor lifecycle."""
import asyncio
from contextlib import aclosing, suppress
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
    def __init__(self, channel, chat_id, interval):
        self.channel, self.chat_id, self.interval = channel, chat_id, interval
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
            if text and text != self.sent:
                if self.message_id is None:
                    self.message_id = await safe_call(self.channel.send, self.chat_id, text)
                    # Do not retry ambiguous send failures: Telegram may have posted it.
                    if self.message_id is None:
                        return
                else:
                    await safe_call(self.channel.edit, self.chat_id, self.message_id, text)
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
                 edit_interval=1.5, typing_interval=4, max_recoveries=3, executors=None):
        # `executor` is the default one; `executors` adds named alternatives.
        self.default_executor = storage.default_executor
        self.executors = {**(executors or {}), self.default_executor: executor}
        self.storage, self.channels = storage, channels
        self.edit_interval, self.typing_interval = edit_interval, typing_interval
        self.max_recoveries = max_recoveries
        self.workers = {}
        self.locks = {}
        self.model_catalogs = {}  # Numbers refer to the last list shown in this process.
        self.closing = False

    async def start(self):
        # Apply durable priority commands before dispatching any recovered work.
        for row in self.storage.pending():
            if command(row["text"]) == "/stop":
                await self._stop(row)
        for row in self.storage.pending():
            self._kick(row["conversation_id"])

    async def receive(self, message: Inbound):
        if self.closing:
            return
        row = self.storage.journal(message)  # Commit before any await/processing.
        if row is None:
            return
        conv = row["conversation_id"]
        async with self.locks.setdefault(conv, asyncio.Lock()):
            if command(message.text) == "/stop":
                await self._stop(row)
            self._kick(conv)

    @property
    def executor(self):
        return self.executors[self.default_executor]

    @executor.setter
    def executor(self, value):
        self.executors[self.default_executor] = value

    async def _cancel_run(self, run_id):
        # Run ids are turn ids, unique across executors; cancel is a no-op elsewhere.
        await asyncio.gather(*(e.cancel(run_id) for e in self.executors.values()))

    def _kick(self, conv):
        if not self.closing and (conv not in self.workers or self.workers[conv].done()):
            self.workers[conv] = asyncio.create_task(self._work(conv))

    async def _stop(self, row):
        active, queued = self.storage.stop(row["id"])
        worker = self.workers.get(row["conversation_id"])
        if worker and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        for run_id, _ in active:
            await self._cancel_run(run_id)
        description = f"Stopped task(s) {', '.join(str(task_id) for _, task_id in active)}." if active else "No running task to stop."
        await safe_call(self.channels[row["channel"]].send, row["chat_id"],
                        f"{description} Cancelled {queued} queued message(s). Send a new message to continue, or /new for a fresh session.")

    async def _work(self, conv):
        while not self.closing:
            pending = self.storage.pending(conv)
            if not pending:
                return
            row = pending[0]
            channel = self.channels[row["channel"]]
            if command(row["text"]) == "/stop":
                # A concurrent receive() journaled it while waiting for the
                # priority-command lock. Its handler owns cancellation.
                return
            if command(row["text"]) == "/new":
                parts = row["text"].split()
                name = parts[1].lower() if len(parts) > 1 else self.default_executor
                if name not in self.executors:
                    self.storage.finish_command(row["id"])
                    await safe_call(channel.send, row["chat_id"], f"Unknown executor `{name}`. Available: {', '.join(sorted(self.executors))}.")
                    continue
                self.storage.new_task(row["id"], name)
                await safe_call(channel.send, row["chat_id"], f"New task on {name}. Your next message starts its executor session.")
                continue
            if command(row["text"]) == "/model":
                await self._model(row, channel)
                continue
            pending_turn = self.storage.turn_for(row["id"])
            result = self.storage.stored_result(pending_turn["id"])
            if result is not None:
                text, error = result
                self.storage.finish(pending_turn["id"], "failed" if error else "completed", error)
                reply = Reply(channel, row["chat_id"], self.edit_interval)
                reply.update((text + f"\n\nTask failed: {error}" if error else text) or "Task completed without a text answer.")
                try:
                    await reply.finish()
                finally:
                    await reply.cancel()
                continue
            turn, recovering = self.storage.claim(row["id"], self.max_recoveries)
            if turn is None:
                await safe_call(channel.send, row["chat_id"], "Interrupted task failed: restart recovery limit reached. Send a new message to retry.")
                continue
            await self._run(row, turn, recovering)

    async def _model(self, row, channel):
        parts = row["text"].split(maxsplit=1)
        conv = row["conversation_id"]
        if len(parts) == 1:
            task = self.storage.active_task(conv)
            name = task["executor"] if task else self.default_executor
            model = task["model"] if task else None
            choices = catalog()
            self.model_catalogs[conv] = choices
            lines = [f"Model: {name} / {model or 'default'}."]
            number = 0
            for executor, models in choices.items():
                lines.append(f"\n{executor}:")
                for model_id, description in models:
                    number += 1
                    lines.append(f"{number}. {model_id}" + (f" — {description}" if description else ""))
                if not models:
                    lines.append("No models available.")
            lines.append("\nChoose with /model <name or number>.")
            reply = "\n".join(lines)
            self.storage.finish_command(row["id"])
        else:
            try:
                choice = parts[1].strip()
                choices = None
                if choice.isdecimal():
                    choices = self.model_catalogs.get(conv)
                    if choices is None:
                        raise ValueError("Use /model to list numbered choices first (also after a restart).")
                name, model = resolve(choice, choices)
                if name not in self.executors:
                    raise ValueError(f"Executor {name} is not configured.")
            except ValueError as exc:
                self.storage.finish_command(row["id"])
                reply = str(exc)
            else:
                task, switched = self.storage.set_model(row["id"], name, model)
                if switched:
                    _, count, truncated = self.storage.transcript(task["id"], row["id"])
                    reply = f"Switched to {name} / {model}; context will be passed as a transcript of {count} earlier turns."
                    if truncated:
                        reply += " Transcript truncated: older turns or parts of the newest turn omitted."
                elif task["session_id"]:
                    reply = f"Model: {name} / {model}. Context kept (same session)."
                else:
                    reply = f"Model: {name} / {model}. Your next message starts its executor session."
        await safe_call(channel.send, row["chat_id"], reply)

    async def _typing(self, channel, chat):
        # Studied reference: nanobot (MIT), nanobot/channels/telegram/runtime.py.
        # Own generic implementation; no copied source.
        while True:
            await safe_call(channel.typing, chat)
            await asyncio.sleep(self.typing_interval)

    async def _run(self, row, turn, recovering):
        channel, chat = self.channels[row["channel"]], row["chat_id"]
        reply = Reply(channel, chat, self.edit_interval)
        typing = asyncio.create_task(self._typing(channel, chat))
        text, tool, error = "", "", None
        completed = False
        current_deltas = False
        try:
            prompt = self.storage.prepare_prompt(turn, row['text'])
            if recovering and turn['resume_id']:
                prompt = f"{RESTART_PROMPT}\n\nOriginal request:\n{prompt}"
            if recovering:
                await safe_call(channel.send, chat, f"Service restarted; recovering task {turn['task_id']} (attempt {turn['recovery_count']}/{self.max_recoveries}).")
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
            await safe_call(channel.send, chat, f"Task {turn['task_id']} failed: {exc}")
        finally:
            typing.cancel()
            await asyncio.gather(typing, return_exceptions=True)
            await reply.cancel()
            await self._cancel_run(turn["id"])

    async def idle(self):
        """Wait for accepted work; useful to embedding callers and tests."""
        while any(not job.done() for job in self.workers.values()):
            await asyncio.gather(*list(self.workers.values()))

    def begin_shutdown(self):
        # Called synchronously by the signal handler, before awaiting poll cleanup.
        self.closing = True

    async def shutdown(self):
        self.begin_shutdown()
        for job in self.workers.values():
            job.cancel()
        await asyncio.gather(*self.workers.values(), return_exceptions=True)
        await asyncio.gather(*(e.shutdown() for e in self.executors.values()))
