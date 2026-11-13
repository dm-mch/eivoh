"""Own subprocess/JSONL adapter, based on the installed CLI probe, not an SDK."""
import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import signal
import sys

from .base import Event, Record


def _parse_line(line: str) -> Record:
    try:
        raw = json.loads(line)
    except (ValueError, TypeError):
        return Record(line, None, (Event("error", {"message": "Invalid executor JSON"}, line),))
    if not isinstance(raw, dict):
        return Record(line, raw)
    sid = raw.get("session_id")
    events = []

    def add(kind, **data):
        events.append(Event(kind, data, raw, sid))

    kind = raw.get("type")
    if kind == "system" and raw.get("subtype") == "init":
        add("session_started", session_id=sid)
    elif kind == "stream_event":
        event = raw.get("event", {})
        # Subagent output is journaled but must not become the main answer.
        if not raw.get("parent_tool_use_id"):
            if event.get("type") == "content_block_delta" and event.get("delta", {}).get("type") == "text_delta":
                add("text_delta", text=event["delta"].get("text", ""))
            elif event.get("type") == "content_block_start" and event.get("content_block", {}).get("type") == "tool_use":
                add("tool_started", **event["content_block"])
    elif kind in {"assistant", "user"}:
        message = raw.get("message", {})
        if kind == "assistant" and not raw.get("parent_tool_use_id"):
            add("message", text="".join(b.get("text", "") for b in message.get("content", []) if b.get("type") == "text"),
                message_id=message.get("id"))
        for block in message.get("content", []):
            if block.get("type") == "tool_use":
                add("tool_started", **block)
            elif block.get("type") == "tool_result":
                add("tool_finished", **block)
    elif kind == "result":
        add("usage", usage=raw.get("usage", {}), total_cost_usd=raw.get("total_cost_usd"))
        if raw.get("permission_denials"):
            # Observed result field; no invented interactive approval protocol.
            add("approval_requested", denials=raw["permission_denials"], actionable=False)
        if raw.get("is_error"):
            add("error", message=raw.get("result") or raw.get("errors") or "Executor failed")
        add("turn_completed", status="failed" if raw.get("is_error") else "completed", text=raw.get("result", ""))
    elif kind == "error":
        add("error", message=raw.get("error", raw))
    return Record(line, raw, tuple(events))


def parse_line(line: str) -> Record:
    try:
        return _parse_line(line)
    except (TypeError, KeyError, AttributeError, ValueError) as exc:
        # A new/malformed schema must not stop the reader and lose later lines.
        raw = json.loads(line)
        return Record(line, raw, (Event("error", {"message": f"Unsupported event shape: {exc}"}, raw),))


def process_table() -> dict[int, tuple[int, str, str]]:
    """pid -> (ppid, start ticks, state); only /proc stat, never process secrets."""
    table = {}
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rsplit(")", 1)[1].split()
            table[int(path.parent.name)] = (int(fields[1]), fields[19], fields[0])
        except (OSError, ValueError, IndexError):
            continue
    return table


@dataclass
class ProcessTree:
    process: asyncio.subprocess.Process
    known: dict[int, str] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def snapshot(self):
        table = process_table()
        # Only follow original processes; PID reuse must never signal a stranger.
        roots = {pid for pid, start in self.known.items() if pid in table and table[pid][1] == start}
        changed = True
        while changed:
            changed = False
            for pid, (ppid, start, _) in table.items():
                if ppid in roots and pid not in roots:
                    self.known[pid] = start
                    roots.add(pid)
                    changed = True
        return table

    def signal(self, pid, sig, table):
        if pid in table and table[pid][1] == self.known.get(pid) and table[pid][2] != "Z":
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass

    async def cleanup(self, grace: float):
        async with self.lock:
            table = self.snapshot()  # BEFORE signalling: setsid children escape killpg.
            self.signal(self.process.pid, signal.SIGTERM, table)
            deadline = asyncio.get_running_loop().time() + grace + 0.5
            while self.process.returncode is None and asyncio.get_running_loop().time() < deadline:
                self.snapshot()
                await asyncio.sleep(0.02)
            # Re-scan until no tracked live process remains, including descendants
            # created during shutdown. systemd's cgroup is a second containment layer.
            while True:
                table = self.snapshot()
                alive = [pid for pid, start in self.known.items()
                         if pid in table and table[pid][1] == start and table[pid][2] != "Z"]
                for pid in alive:
                    self.signal(pid, signal.SIGKILL, table)
                if not alive:
                    break
                await asyncio.sleep(0.02)
            await self.process.wait()
            for pid in self.known:
                if pid != self.process.pid:
                    try:
                        os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        pass


class ClaudeExecutor:
    def __init__(self, workdir: Path, binary="claude", model=None,
                 permission_mode="bypassPermissions", grace=0.5, extra_args=()):
        self.workdir = workdir
        self.binary = str(binary)
        self.model = model
        self.permission_mode = permission_mode
        self.grace = grace
        self.extra_args = tuple(extra_args)
        self.running: dict[int, ProcessTree] = {}
        self.starting: dict[int, asyncio.Task] = {}

    def start(self, run_id, prompt):
        return self._turn(run_id, prompt)

    def resume(self, run_id, session_id, prompt):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", session_id):
            raise ValueError("Invalid executor session id")
        return self._turn(run_id, prompt, session_id)

    async def _turn(self, run_id, prompt, session_id=None):
        if run_id in self.running or run_id in self.starting:
            raise RuntimeError("Run already running")
        args = [self.binary, "-p", "--output-format", "stream-json", "--verbose",
                "--include-partial-messages", "--permission-mode", self.permission_mode]
        if session_id:
            args += ["--resume", session_id]
        if self.model:
            args += ["--model", self.model]
        args.extend(self.extra_args)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC"))}
        async def spawn():
            supervisor = Path(__file__).with_name("supervisor.py")
            proc = await asyncio.create_subprocess_exec(sys.executable, str(supervisor), str(self.grace), *args, cwd=self.workdir, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=8 * 1024 * 1024)
            tree = ProcessTree(proc)
            table = process_table()
            if proc.pid in table:
                tree.known[proc.pid] = table[proc.pid][1]
            self.running[run_id] = tree
            return tree

        launching = asyncio.create_task(spawn())
        self.starting[run_id] = launching
        try:
            tree = await asyncio.shield(launching)
        except asyncio.CancelledError:
            # Subprocess creation may already have forked before cancellation.
            tree = await launching
            await tree.cleanup(self.grace)
            self.running.pop(run_id, None)
            raise
        finally:
            self.starting.pop(run_id, None)
        proc = tree.process
        queue = asyncio.Queue()

        async def read(pipe, stream):
            def record(line):
                text = line.decode("utf-8", errors="replace")
                return parse_line(text) if stream == "stdout" else Record(text, None, stream="stderr")

            pending = bytearray()
            try:
                # read(), rather than readline(), also retains tool-result lines
                # larger than StreamReader's buffer limit.
                while chunk := await pipe.read(65536):
                    pending.extend(chunk)
                    while (end := pending.find(b"\n")) >= 0:
                        await queue.put(record(pending[:end + 1]))
                        del pending[:end + 1]
                if pending:
                    await queue.put(record(pending))
            except Exception as exc:
                await queue.put(Record(None, None, (Event("error", {"message": str(exc)}, None),)))
            finally:
                await queue.put(None)

        async def watch():
            while proc.returncode is None:
                tree.snapshot()
                await asyncio.sleep(0.025)
            await tree.cleanup(0)  # Includes tool children holding stdout open.

        async def write():
            try:
                proc.stdin.write(prompt.encode())
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()

        jobs = [asyncio.create_task(read(proc.stdout, "stdout")),
                asyncio.create_task(read(proc.stderr, "stderr")),
                asyncio.create_task(watch()), asyncio.create_task(write())]
        completed = False
        try:
            eof = 0
            while eof < 2:
                record = await queue.get()
                if record is None:
                    eof += 1
                else:
                    completed |= any(e.kind == "turn_completed" for e in record.events)
                    yield record
            await jobs[2]
            if proc.returncode != 0 or not completed:
                yield Record(None, None, (Event("error", {"message": f"Executor exited {proc.returncode}; result received={completed}"}, None),))
        finally:
            # Shield cleanup from cancellation; the caller waits for it to finish.
            cleanup = asyncio.create_task(tree.cleanup(self.grace))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
            finally:
                for job in jobs:
                    job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
                self.running.pop(run_id, None)

    async def cancel(self, run_id):
        if launching := self.starting.get(run_id):
            await asyncio.shield(launching)
        if tree := self.running.get(run_id):
            await tree.cleanup(self.grace)

    async def shutdown(self):
        await asyncio.gather(*(self.cancel(run_id) for run_id in set(self.running) | set(self.starting)))
