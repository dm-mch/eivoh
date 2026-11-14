"""Subscription limits left: free live CLI probes and rate-limit events seen in turns.

Formats observed on Claude Code 2.1.323 and codex-cli 0.167.2 (2026-11-14); in
those versions neither probe started a model turn or spent anything from the limits.
A snapshot is a plain JSON-serializable dict:
{executor, source: live|turn|none, observed_at, windows: [{key, name, used_percent,
resets_at, resets_text, status, observed_at}], plan, notes, error}. The store merges
windows by their stable key (the display name may change), so each window keeps the
time it was last observed.

Probes run under the per-turn subreaper (executors/supervisor.py), which also ends
descendants that left the probe's process group. As for turns, SIGKILL of the
supervisor itself leaves containment to systemd's cgroup.
"""
import asyncio
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import re
import logging
import shutil
import signal
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

from .executors.claude import ClaudeExecutor
from .executors.codex import CodexExecutor

log = logging.getLogger(__name__)
SUPERVISOR = Path(__file__).parent / 'executors' / 'supervisor.py'
GRACE = 0.5  # Supervisor: SIGTERM to the CLI, then SIGKILL after this many seconds.

EXECUTORS = ('claude', 'codex')
CLAUDE_LINE = re.compile(r'^Current (?P<name>.+?):\s*(?P<pct>\d+(?:\.\d+)?)% used'
                         r'(?:\s*[·•|,-]\s*resets\s+(?P<reset>.+?))?\s*$')
RESET_TEXT = re.compile(r'^(?:(?P<month>[A-Za-z]{3})[a-z]*\.? (?P<day>\d{1,2}),?\s+)?(?P<hour>\d{1,2})'
                        r'(?::(?P<minute>\d{2}))?\s*(?P<half>[ap])\.?m\.?\s*\((?P<zone>[^()]+)\)$', re.I)
MONTHS = ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec')
CLAUDE_WINDOWS = {'five_hour': 'session', 'seven_day': 'week (all models)'}  # The live probe's names.


class ProbeError(Exception):
    pass


class StoreBusy(Exception):
    pass


def snapshot(executor, source, observed_at, windows=(), plan=None, notes=None, error=None):
    windows = [{**item, 'key': item.get('key') or item['name'], 'observed_at': item.get('observed_at') or observed_at}
               for item in windows]
    return {'executor': executor, 'source': source, 'observed_at': observed_at, 'windows': windows,
            'plan': plan, 'notes': notes or {}, 'error': error}


def merge(old, new):
    """Windows by key, the newer observation of each wins; absent ones are kept.

    Notes (restrictions, reached limits, status) come only from the newest snapshot,
    even when empty: an absent restriction there means it was cleared.
    """
    windows = {item['key']: item for item in old['windows']}
    for item in new['windows']:
        if item['key'] not in windows or item['observed_at'] >= windows[item['key']]['observed_at']:
            windows[item['key']] = item
    newer, older = (new, old) if new['observed_at'] >= old['observed_at'] else (old, new)
    return {**newer, 'windows': list(windows.values()), 'plan': newer.get('plan') or older.get('plan'),
            'notes': newer.get('notes') or {}, 'error': None}


def window(name, used, resets_at=None, resets_text=None, status=None, observed_at=None, key=None):
    """`key` defaults to the name; `observed_at` to the time of the snapshot it joins."""
    resets_at = int(resets_at) if isinstance(resets_at, (int, float)) and resets_at > 0 else None
    return {'key': key or name, 'name': name, 'used_percent': used, 'resets_at': resets_at, 'resets_text': resets_text, 'status': status,
            **({'observed_at': observed_at} if observed_at else {})}


def reset_epoch(text, now):
    """`Nov 20, 1pm (Europe/Amsterdam)` -> epoch; the year nearest to now, past or future.

    A past result is kept: the formatter says the reset time has passed. An hour
    repeated by a DST change resolves to its first occurrence (at most 1 h off).
    """
    match = RESET_TEXT.match(text.strip())
    if not match:
        return None
    try:
        zone = ZoneInfo(match['zone'].strip())
        hour = int(match['hour']) % 12 + (12 if match['half'].lower() == 'p' else 0)
        minute = int(match['minute'] or 0)
        local = datetime.fromtimestamp(now, zone)
        if match['month']:
            month = MONTHS.index(match['month'][:3].lower()) + 1
            candidates = [datetime(year, month, int(match['day']), hour, minute, tzinfo=zone)
                          for year in (local.year - 1, local.year, local.year + 1)]
        else:  # Time only (not observed): the nearest such day.
            today = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            candidates = [today + timedelta(days=offset) for offset in (-1, 0, 1)]
    except (ValueError, KeyError, ArithmeticError):  # Unknown zone/month or invalid date.
        return None
    return int(min((c.timestamp() for c in candidates), key=lambda value: abs(value - now)))


def parse_claude(stdout, now):
    """`claude -p --output-format json` printing `/usage`; raises ProbeError."""
    result = None
    for line in reversed(stdout.splitlines()):
        try:
            result = json.loads(line)
        except ValueError:
            continue
        if isinstance(result, dict) and result.get('type') == 'result':
            break
        result = None
    if result is None:
        raise ProbeError('no result in Claude output')
    if result.get('is_error') or result.get('subtype') != 'success':
        raise ProbeError('Claude reported an error: ' + str(result.get('result') or result.get('subtype'))[:200])
    if result.get('num_turns') or result.get('total_cost_usd'):
        raise ProbeError('/usage started a model turn instead of a local command')
    windows = []
    for line in str(result.get('result') or '').splitlines():
        if match := CLAUDE_LINE.match(line.strip()):
            reset = match['reset']
            windows.append(window(match['name'], float(match['pct']), reset and reset_epoch(reset, now), reset))
    if not windows:
        raise ProbeError('no usage lines in Claude /usage output')
    return snapshot('claude', 'live', now, windows)


def duration_name(minutes):
    return {300: '5 h', 10080: 'week'}.get(minutes, f'{minutes} min' if minutes else 'window')


def parse_codex(result, now, source='live'):
    """account/rateLimits/read result or an account/rateLimits/updated params."""
    if not isinstance(result, dict):
        raise ProbeError('unexpected Codex rate-limit response')
    limits = result.get('rateLimitsByLimitId')
    if not isinstance(limits, dict) or not limits:
        single = result.get('rateLimits')
        limits = {single.get('limitId') or 'codex': single} if isinstance(single, dict) else {}
    limits = {key: value for key, value in limits.items() if isinstance(value, dict)}
    windows, notes, plan = [], {}, None
    for key, limit in limits.items():
        limit_id = limit.get('limitId') or key
        label = limit.get('limitName') or limit_id
        prefix = f'{label} ' if len(limits) > 1 or limit_id != 'codex' else ''
        plan = plan or limit.get('planType')
        for slot in ('primary', 'secondary'):
            data = limit.get(slot)
            if isinstance(data, dict) and isinstance(data.get('usedPercent'), (int, float)):
                windows.append(window(prefix + duration_name(data.get('windowDurationMins')),
                                      data['usedPercent'], data.get('resetsAt'), key=f'{limit_id}:{slot}'))
        if limit.get('rateLimitReachedType'):
            notes.setdefault('reached', []).append(prefix + str(limit['rateLimitReachedType']))
        if limit.get('spendControlReached'):
            notes['spend_control_reached'] = True
    if not windows:
        raise ProbeError('no rate-limit windows in the Codex response')
    if result.get('ordinaryUsageAllowed') is False:
        notes['ordinary_usage_allowed'] = False
    credits = result.get('rateLimitResetCredits')
    if isinstance(credits, dict) and isinstance(credits.get('availableCount'), int):
        notes['reset_credits'] = credits['availableCount']
    return snapshot('codex', source, now, windows, plan, notes)


def observe(payload, now=None):
    """Snapshot from a rate-limit event in an ordinary turn's stream, else None."""
    if not isinstance(payload, dict):
        return None
    if payload.get('type') == 'rate_limit_event':
        info = payload.get('rate_limit_info')
        unified = info.get('unifiedWindows') if isinstance(info, dict) else None
        if not isinstance(unified, dict):
            return None
        windows = [window(CLAUDE_WINDOWS.get(key, key), round(data['utilization'] * 100, 1),
                          data.get('resetsAt'), status=data.get('status'))
                   for key, data in unified.items()
                   if isinstance(data, dict) and isinstance(data.get('utilization'), (int, float))]
        notes = {'status': info['status']} if isinstance(info.get('status'), str) else {}
        return snapshot('claude', 'turn', now or time.time(), windows, notes=notes) if windows else None
    if payload.get('method') == 'account/rateLimits/updated':
        try:
            return parse_codex(payload.get('params'), now or time.time(), 'turn')
        except ProbeError:
            return None
    return None


async def _exited(proc, timeout):
    """Wait for the exit itself; proc.wait() also waits for the pipes to close."""
    deadline = time.monotonic() + timeout
    while proc.returncode is None and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    return proc.returncode is not None


async def _discard(stream):
    with suppress(Exception):
        while await stream.read(65536):
            pass


async def _terminate(proc):
    """Bounded: stdout is drained so a full pipe cannot keep the transport open."""
    if proc.stdin and not proc.stdin.is_closing():
        proc.stdin.close()
    drain = asyncio.ensure_future(_discard(proc.stdout))
    try:
        if proc.returncode is None:
            # The supervisor stops the CLI (SIGKILL after GRACE) and kills every descendant.
            with suppress(ProcessLookupError):
                proc.send_signal(signal.SIGTERM)
            if not await _exited(proc, GRACE + 2):
                # Not reaped yet, so its process group id cannot have been reused.
                with suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                await _exited(proc, 2)
        with suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), 1)
    finally:
        drain.cancel()
        await asyncio.gather(drain, return_exceptions=True)


async def _session(args, env, timeout, talk):
    if not shutil.which(args[0], path=env.get('PATH')):
        raise ProbeError(f'cannot start {args[0]}: not found')
    with tempfile.TemporaryDirectory(prefix='eivoh-usage-') as cwd:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(SUPERVISOR), str(GRACE), *args, cwd=cwd, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, start_new_session=True, limit=1024 * 1024)
        except OSError as exc:
            raise ProbeError(f'cannot start {args[0]}: {exc.strerror or exc}') from None
        try:
            async with asyncio.timeout(timeout):
                return await talk(proc)
        except TimeoutError:
            raise ProbeError(f'timed out after {timeout:g} s') from None
        except (BrokenPipeError, ConnectionResetError):
            raise ProbeError(f'{args[0]} closed its input') from None
        finally:
            cleanup = asyncio.ensure_future(_terminate(proc))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise


async def probe_claude(binary='claude', timeout=20.0):
    """Run `/usage` as a local command; returns a snapshot, with `error` on failure."""
    # The prompt goes to stdin: `--tools ''` is variadic and would take a positional one.
    # `--model haiku` is a safety net in case /usage ever stops being local.
    args = [str(binary), '-p', '--output-format', 'json', '--no-session-persistence', '--safe-mode',
            '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--tools', '', '--model', 'haiku']
    async def talk(proc):
        proc.stdin.write(b'/usage')
        await proc.stdin.drain()
        proc.stdin.close()
        out = await proc.stdout.read()
        await proc.wait()
        return out
    try:
        out = await _session(args, ClaudeExecutor(Path('.'), binary).environment(), timeout, talk)
        return parse_claude(out.decode('utf-8', 'replace'), time.time())
    except Exception as exc:  # A probe failure must never break /usage or the other probe.
        return snapshot('claude', 'none', None, error=f'live probe failed: {str(exc) or type(exc).__name__}')


async def probe_codex(binary='codex', timeout=20.0):
    """account/rateLimits/read over a local stdio app-server; no thread or turn."""
    async def talk(proc):
        def send(message):
            proc.stdin.write((json.dumps(message) + '\n').encode())
        async def reply(request_id):
            while line := await proc.stdout.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get('id') == request_id and 'method' not in message:
                    if message.get('error'):
                        raise ProbeError('Codex refused the request: ' + str(message['error'])[:200])
                    return message.get('result')
            raise ProbeError('codex app-server exited without a response')
        send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'eivoh', 'version': '0.1.0'},
                                                         'capabilities': {'experimentalApi': True}}})
        await proc.stdin.drain()
        await reply(1)
        send({'method': 'initialized'})
        send({'id': 2, 'method': 'account/rateLimits/read', 'params': None})
        await proc.stdin.drain()
        return await reply(2)
    args = [str(binary), 'app-server', '--listen', 'stdio://']
    try:
        result = await _session(args, CodexExecutor(Path('.'), binary).environment(), timeout, talk)
        return parse_codex(result, time.time())
    except Exception as exc:
        return snapshot('codex', 'none', None, error=f'live probe failed: {str(exc) or type(exc).__name__}')


class UsageStore:
    """Latest snapshot per executor in one JSON file, shared by the service and the CLI."""
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def _locked(self, timeout):
        """Bounded wait: another process (the CLI) may hold the lock or be suspended."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path.with_name(self.path.name + '.lock'), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise StoreBusy(f'{self.path} is locked by another process') from None
                    time.sleep(0.01)
            yield
        finally:
            os.close(fd)

    def load(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        # Files written before per-window keys/times fall back to the name/snapshot's time.
        return {name: {**snap, 'windows': [{**item, 'key': item.get('key') or item['name'],
                                            'observed_at': item.get('observed_at') or snap['observed_at']}
                                           for item in snap['windows'] if isinstance(item, dict) and 'name' in item]}
                for name, snap in data.items() if name in EXECUTORS and isinstance(snap, dict)
                and isinstance(snap.get('observed_at'), (int, float)) and isinstance(snap.get('windows'), list)}

    def update(self, snap, lock_timeout=5.0):
        """Merge a successful snapshot and return the stored result, or None if skipped.

        An older observation never replaces a newer window. Raises StoreBusy.
        """
        if snap.get('error') or not snap.get('windows'):
            return None
        with self._locked(lock_timeout):
            data = self.load()
            old = data.get(snap['executor'])
            merged = merge(old, snap) if old else snap
            if merged == old:
                return old
            data[snap['executor']] = merged
            fd, temp = tempfile.mkstemp(prefix='.usage-', dir=self.path.parent)  # Mode 0600.
            try:
                with os.fdopen(fd, 'w') as file:
                    json.dump(data, file, ensure_ascii=False)
                os.replace(temp, self.path)
            except BaseException:
                with suppress(FileNotFoundError):
                    os.unlink(temp)
                raise
        return merged


class UsageService:
    def __init__(self, state_dir, claude_bin='claude', codex_bin='codex', timeout=20.0):
        self.store = UsageStore(Path(state_dir) / 'usage.json')
        self.claude_bin, self.codex_bin, self.timeout = claude_bin, codex_bin, timeout

    def observe(self, payload):
        """Called on the event loop for every turn line: cheap, and never waits long.

        Writes the store only for a rate-limit event; under lock contention the
        observation is skipped (the next one or a live probe replaces it).
        """
        snap = observe(payload)
        if snap is not None:
            try:
                self.store.update(snap, lock_timeout=0.15)
            except StoreBusy as exc:
                log.warning('Usage observation skipped: %s', exc)
        return snap

    async def snapshot(self, live=True):
        """Live probes in parallel; a failed probe falls back to the last stored snapshot."""
        if not live:
            stored = self.store.load()
            return [stored.get(name) or snapshot(name, 'none', None, error='nothing stored yet') for name in EXECUTORS]
        fresh = await asyncio.gather(probe_claude(self.claude_bin, self.timeout),
                                     probe_codex(self.codex_bin, self.timeout))
        stored = self.store.load()
        result = []
        for snap in fresh:
            if snap['error'] is None:
                try:  # Returns the merged snapshot: a newer turn observation may have arrived meanwhile.
                    merged = await asyncio.to_thread(self.store.update, snap)
                except (StoreBusy, OSError) as exc:
                    log.warning('Usage store not updated: %s', exc)
                    merged = None
                result.append(merged or snap)
            else:
                previous = stored.get(snap['executor'])
                result.append({**previous, 'error': snap['error']} if previous else snap)
        return result


NAMES = {'claude': 'Claude', 'codex': 'Codex'}


def ago(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return 'just now'
    return duration(seconds) + ' ago'


def duration(seconds):
    minutes = int(seconds) // 60
    if minutes < 1:
        return 'under 1 min'
    if minutes < 60:
        return f'{minutes} min'
    if minutes < 1440:
        return f'{minutes // 60} h' + (f' {minutes % 60} min' if minutes % 60 else '')
    return f'{minutes // 1440} d' + (f' {minutes % 1440 // 60} h' if minutes % 1440 // 60 else '')


def percent(value):
    return f'{value:g}' if float(value).is_integer() else f'{value:.1f}'


def format_usage(snapshots, now):
    """Plain text for chat; unknown stays unknown, stale data is marked as such."""
    blocks = []
    for snap in snapshots:
        name = NAMES.get(snap['executor'], snap['executor'])
        if not snap['windows']:
            blocks.append(f"{name}: unknown ({snap['error'] or 'no data'}).")
            continue
        age = now - snap['observed_at']
        if snap['source'] == 'live':
            fresh = 'live' if age < 60 else f'live probe {ago(age)}'
        else:
            fresh = f'last seen in a turn {ago(age)}'
        lines = [f"{name}{' (' + snap['plan'] + ')' if snap.get('plan') else ''}: {fresh}"
                 + (f"; {snap['error']}" if snap.get('error') else '')]
        for item in snap['windows']:
            used = item['used_percent']
            text = f"{item['name']}: {percent(max(0, 100 - used))}% left ({percent(used)}% used)"
            if item.get('resets_at'):
                left = item['resets_at'] - now
                text += f", resets in {duration(left)}" if left > 0 else ', reset time has passed, data is outdated'
            elif item.get('resets_text'):
                text += f", resets {item['resets_text']}"
            if item.get('status') and item['status'] != 'allowed':
                text += f" [{item['status']}]"
            if item.get('observed_at') and snap['observed_at'] - item['observed_at'] > 60:
                text += f" (as of {ago(now - item['observed_at'])})"
            lines.append(text)
        notes = snap.get('notes') or {}
        if notes.get('status') and notes['status'] != 'allowed':
            lines.append(f"Status: {notes['status']}")
        if notes.get('reached'):
            lines.append('Limit reached: ' + ', '.join(notes['reached']))
        if notes.get('ordinary_usage_allowed') is False:
            lines.append('Ordinary usage is not allowed now')
        if notes.get('spend_control_reached'):
            lines.append('Spend control reached')
        if notes.get('reset_credits'):
            lines.append(f"Limit reset credits available: {notes['reset_credits']}")
        blocks.append('\n'.join(lines))
    return '\n\n'.join(blocks)
