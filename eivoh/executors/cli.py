"""Generic one-process-per-turn CLI runner: supervisor, JSONL reader, cleanup.

Executors supply the command line, environment and a per-turn line parser.
"""
import asyncio
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import signal
import sys

from .base import Event, Record, SteerResult

SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
WATCH_INTERVAL = 0.2  # Seconds between descendant scans of a running turn.


def process_table() -> dict[int, tuple[int, str, str]]:
    """pid -> (ppid, start ticks, state); only /proc stat, never process secrets.

    Runs on the event loop every few tens of milliseconds while a turn is
    running, so it reads with raw file descriptors: pathlib glob and text I/O
    made one scan several times slower.
    """
    table = {}
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            fd = os.open(f"/proc/{entry.name}/stat", os.O_RDONLY)
            try:
                data = os.read(fd, 4096)
            finally:
                os.close(fd)
            fields = data[data.rindex(b")") + 1:].split()
            table[int(entry.name)] = (int(fields[1]), fields[19].decode(), fields[0].decode())
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

    async def watch(self, interval: float = WATCH_INTERVAL):
        """Track descendants until the CLI exits, then clean the whole tree.

        Orphans are adopted by the per-turn subreaper, so a slower scan does not
        lose them; a normal exit still ends the watch at once. wait() also waits
        for the pipes to close, so the loop checks returncode: a descendant
        holding stdout must not keep the watch from cleaning it up.
        """
        exited = asyncio.ensure_future(self.process.wait())
        try:
            while self.process.returncode is None:
                self.snapshot()
                await asyncio.wait((exited,), timeout=interval)
        finally:
            exited.cancel()
        await self.cleanup(0)  # Includes tool children holding stdout open.

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


class CliExecutor:
    supports_steer = False

    async def steer(self, run_id, input_id, text):
        return SteerResult("not_sent")
    def __init__(self, workdir: Path, binary, model=None, grace=0.5, extra_args=()):
        self.workdir = workdir
        self.binary = str(binary)
        self.model = model
        self.grace = grace
        self.extra_args = tuple(extra_args)
        self.running: dict[int, ProcessTree] = {}
        self.starting: dict[int, asyncio.Task] = {}

    def command(self, session_id: str | None, model=None, effort=None) -> list[str]:
        raise NotImplementedError

    def environment(self) -> dict[str, str]:
        return dict(os.environ)

    def parser(self):
        """Return a callable str -> Record; a new one for each turn."""
        raise NotImplementedError

    def model_metadata(self, model=None, effort=None):
        """Launch settings are requests, not evidence of the model used."""
        return {"requested_model": model or self.model,
                "requested_model_source": "turn" if model else "executor_config" if self.model else None,
                "requested_effort": effort, "requested_effort_source": "turn" if effort else None,
                "configured_model": None, "configured_model_source": None,
                "observed_model": None}

    def start(self, run_id, prompt, model=None, effort=None):
        return self._turn(run_id, prompt, model=model, effort=effort)

    def resume(self, run_id, session_id, prompt, model=None, effort=None):
        if not SESSION_ID.fullmatch(session_id):
            raise ValueError("Invalid executor session id")
        return self._turn(run_id, prompt, session_id, model, effort)

    async def _turn(self, run_id, prompt, session_id=None, model=None, effort=None):
        if run_id in self.running or run_id in self.starting:
            raise RuntimeError("Run already running")
        args = self.command(session_id, model, effort)
        env = self.environment()
        parse_line = self.parser()
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
        # Journal launch settings; observed model IDs belong to executor events.
        # The prompt goes to stdin, so argv holds no message content.
        await queue.put(Record(None, {"executor": type(self).__name__, **self.model_metadata(model, effort),
                                      "argv": args, "cwd": str(self.workdir)}, stream="meta"))

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
                asyncio.create_task(tree.watch()), asyncio.create_task(write())]
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
