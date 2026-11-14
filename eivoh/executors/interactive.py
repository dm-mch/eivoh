"""Supervised JSONL duplex transports. Business delivery policy lives in Gateway."""
import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
import sys

from .base import Event, Record, SteerResult
from .cli import CliExecutor, ProcessTree, missing_directory, process_table


@dataclass
class Run:
    run_id: int
    proc: asyncio.subprocess.Process
    session_id: str | None = None
    cwd: Path | None = None
    turn_id: str | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    accepting: bool = True
    finished: bool = False
    terminal: bool = False  # Set by the reader before it parses any later line.
    inputs: dict = field(default_factory=dict)
    requests: dict = field(default_factory=dict)
    request_methods: dict = field(default_factory=dict)
    started: bool = False
    configured_model: str | None = None
    next_id: int = 0
    items: dict = field(default_factory=dict)
    last_message: str = ""
    answer_parts: dict = field(default_factory=dict)

    async def write(self, data):
        self.proc.stdin.write((json.dumps(data, ensure_ascii=False) + '\n').encode())
        await self.proc.stdin.drain()


class InteractiveExecutor(CliExecutor):
    supports_steer = True
    ack_timeout = 30
    startup_timeout = 30

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.runs = {}

    async def initialize(self, run, prompt, model, effort=None):
        raise NotImplementedError

    async def receive(self, run, raw):
        raise NotImplementedError

    async def send_steer(self, run, input_id, text):
        raise NotImplementedError

    async def steer(self, run_id, input_id, text):
        run = self.runs.get(run_id)
        if run is None:
            return SteerResult('not_sent')
        written = False
        try:
            async with asyncio.timeout(self.startup_timeout):
                await run.ready.wait()
            async with run.lock:
                if not run.accepting or run.finished or run.proc.returncode is not None:
                    return SteerResult('not_sent')
                if input_id in run.inputs:
                    future = run.inputs[input_id]
                else:
                    future = asyncio.get_running_loop().create_future()
                    run.inputs[input_id] = future
                    # Even a failed drain can leave a complete message in the pipe.
                    written = True
                    await self.send_steer(run, input_id, text)
            async with asyncio.timeout(self.ack_timeout):
                result = await asyncio.shield(future)
            if result.status == 'unknown':
                await self.cancel(run_id)
            return result
        except TimeoutError:
            if not written and input_id not in run.inputs:
                return SteerResult('not_sent')
            # Freeze this transport before the gateway can queue an uncertain input.
            await self.cancel(run_id)
            return SteerResult('unknown', 'Acknowledgement timed out')
        except (BrokenPipeError, ConnectionResetError):
            return SteerResult('unknown' if written else 'not_sent', 'Transport closed')

    async def _turn(self, run_id, prompt, session_id=None, model=None, effort=None, cwd=None):
        if run_id in self.running or run_id in self.starting:
            raise RuntimeError('Run already running')
        cwd = self.workdir if cwd is None else Path(cwd)
        if error := missing_directory(cwd):
            yield error
            return
        args = self.command(session_id, model, effort)
        async def spawn():
            supervisor = Path(__file__).with_name('supervisor.py')
            proc = await asyncio.create_subprocess_exec(sys.executable, str(supervisor), str(self.grace), *args,
                cwd=cwd, env=self.environment(), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
                limit=8 * 1024 * 1024)
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
            tree = await launching
            await tree.cleanup(self.grace)
            self.running.pop(run_id, None)
            raise
        finally:
            self.starting.pop(run_id, None)
        proc = tree.process
        run = Run(run_id, proc, session_id, cwd)
        self.runs[run_id] = run
        queue = asyncio.Queue()
        await queue.put(Record(None, {'executor': type(self).__name__, **self.model_metadata(model, effort),
                                     'argv': args, 'cwd': str(cwd)}, stream='meta'))

        async def read(pipe, stream):
            async def line_record(line):
                text = line.decode('utf-8', errors='replace')
                if stream == 'stderr':
                    return Record(text, None, stream=stream)
                try:
                    raw = json.loads(text)
                    events = await self.receive(run, raw) if isinstance(raw, dict) else ()
                    return Record(text, raw, tuple(events))
                except (ValueError, TypeError, AttributeError, KeyError) as exc:
                    return Record(text, None, (Event('error', {'message': f'Invalid executor event: {exc}'}, text),))
            pending = bytearray()
            try:
                while chunk := await pipe.read(65536):
                    pending.extend(chunk)
                    while (end := pending.find(b'\n')) >= 0:
                        await queue.put(await line_record(pending[:end + 1]))
                        del pending[:end + 1]
                if pending:
                    await queue.put(await line_record(pending))
            except Exception as exc:
                await queue.put(Record(None, None, (Event('error', {'message': str(exc)}, None),)))
            finally:
                await queue.put(None)

        async def initialize():
            try:
                async with asyncio.timeout(self.startup_timeout):
                    await self.initialize(run, prompt, model, effort)
            except Exception as exc:
                await queue.put(Record(None, None, (Event('error', {'message': str(exc) or 'Executor initialization timed out'}, None),)))
                await tree.cleanup(self.grace)

        jobs = [asyncio.create_task(read(proc.stdout, 'stdout')), asyncio.create_task(read(proc.stderr, 'stderr')),
                asyncio.create_task(tree.watch()), asyncio.create_task(initialize())]
        completed, eof = False, 0
        try:
            while eof < 2:
                record = await queue.get()
                if record is None:
                    eof += 1
                    continue
                yield record
                # The consumer has now durably appended raw acknowledgement and
                # normalized input_accepted together, before we acknowledge to UI.
                for event in record.events:
                    if event.kind == 'input_accepted':
                        future = run.inputs.get(event.data.get('input_id'))
                        if future is not None and not future.done():
                            future.set_result(SteerResult('accepted'))
                    elif event.kind == 'turn_completed':
                        completed = True
                        run.finished = True
                        run.accepting = False
                if run.finished:
                    await tree.cleanup(self.grace)
            await jobs[2]
            if not completed:
                yield Record(None, None, (Event('error', {'message': f'Executor exited {proc.returncode} without a result'}, None),))
        finally:
            run.accepting = False
            run.finished = True
            run.ready.set()
            cleanup = asyncio.create_task(tree.cleanup(self.grace))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
            finally:
                for future in run.inputs.values():
                    if not future.done():
                        future.set_result(SteerResult('unknown', 'Executor ended before acknowledgement'))
                for future in run.requests.values():
                    if not future.done():
                        future.set_exception(ConnectionResetError('Executor transport closed'))
                        # A cleanup-cancelled initialization task may no longer
                        # await this future. Still deliver an ordinary transport
                        # error to any waiter, never cancel an unrelated caller.
                        future.exception()
                for job in jobs:
                    job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
                self.runs.pop(run_id, None)
                self.running.pop(run_id, None)

    async def cancel(self, run_id):
        if run := self.runs.get(run_id):
            run.accepting = False
            run.ready.set()
        await super().cancel(run_id)
