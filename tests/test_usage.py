import asyncio
import json
import os
import stat
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from conftest import FakeChannel, inbound, until
from eivoh import usage
from eivoh.executors.claude import ClaudeExecutor
from eivoh.executors.cli import process_table
from eivoh.gateway import Gateway
from eivoh.storage import Storage

AMS = ZoneInfo('Europe/Amsterdam')
NOW = datetime(2026, 11, 14, 10, 0, tzinfo=AMS).timestamp()
CLAUDE_TEXT = '''You are currently using your subscription to power your Claude Code usage

Current session: 46% used · resets Nov 14, 11:10am (Europe/Amsterdam)
Current week (all models): 12% used · resets Nov 20, 1pm (Europe/Amsterdam)
Current week (Fable): 19% used · resets Nov 20, 12:59pm (Europe/Amsterdam)

What's contributing to your usage?
Current tip: 80% of something, not a window
'''
CODEX_RESULT = {
    'ordinaryUsageAllowed': True,
    'rateLimits': {'limitId': 'codex', 'limitName': None,
                   'primary': {'usedPercent': 51, 'windowDurationMins': 10080, 'resetsAt': 1795010673},
                   'secondary': None, 'planType': 'prolite', 'rateLimitReachedType': None},
    'rateLimitsByLimitId': {'codex': {'limitId': 'codex', 'limitName': None,
                                      'primary': {'usedPercent': 51, 'windowDurationMins': 10080, 'resetsAt': 1795010673},
                                      'secondary': None, 'planType': 'prolite', 'rateLimitReachedType': None}},
    'rateLimitResetCredits': {'availableCount': 3, 'credits': [{'id': 'credit-secret'}]},
    'accountId': 'account-secret', 'rateLimitUpsell': None}


def claude_result(text=CLAUDE_TEXT, **extra):
    return json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 0,
                       'total_cost_usd': 0, 'modelUsage': {}, 'result': text, **extra})


def test_claude_text_parses_windows_and_reset_times():
    snap = usage.parse_claude(claude_result(), NOW)
    assert snap['source'] == 'live' and snap['error'] is None
    names = [w['name'] for w in snap['windows']]
    assert names == ['session', 'week (all models)', 'week (Fable)']
    session, week, fable = snap['windows']
    assert session['used_percent'] == 46
    assert session['resets_at'] == datetime(2026, 11, 14, 11, 10, tzinfo=AMS).timestamp()
    assert week['resets_at'] == datetime(2026, 11, 20, 13, 0, tzinfo=AMS).timestamp()  # 1pm, no minutes.
    assert fable['resets_at'] == datetime(2026, 11, 20, 12, 59, tzinfo=AMS).timestamp()
    assert week['resets_text'] == 'Nov 20, 1pm (Europe/Amsterdam)'


def test_reset_year_is_nearest_future_and_unknown_formats_keep_text():
    december = datetime(2026, 12, 30, 12, tzinfo=AMS).timestamp()
    assert usage.reset_epoch('Jan 2, 12am (Europe/Amsterdam)', december) == datetime(2027, 1, 2, 0, 0, tzinfo=AMS).timestamp()
    assert usage.reset_epoch('Nov 20, 1pm (Mars/Olympus)', NOW) is None
    assert usage.reset_epoch('in 3 hours', NOW) is None
    snap = usage.parse_claude(claude_result('Current session: 5% used · resets soon-ish'), NOW)
    assert snap['windows'][0]['resets_at'] is None and snap['windows'][0]['resets_text'] == 'soon-ish'


@pytest.mark.parametrize('stdout, reason', [
    ('not json', 'no result'),
    (claude_result('Unknown skill: usage'), 'no usage lines'),
    (claude_result('ignored', is_error=True), 'error'),
    (claude_result(num_turns=1), 'model turn'),
    (claude_result(total_cost_usd=0.01), 'model turn'),
])
def test_claude_garbage_or_model_turn_is_a_failure(stdout, reason):
    with pytest.raises(usage.ProbeError, match=reason):
        usage.parse_claude(stdout, NOW)


def test_codex_result_parsing_without_private_ids():
    snap = usage.parse_codex(CODEX_RESULT, NOW)
    assert snap['windows'] == [usage.window('week', 51, 1795010673, observed_at=NOW)]
    assert snap['plan'] == 'prolite' and snap['notes'] == {'reset_credits': 3}
    assert 'secret' not in json.dumps(snap)


def test_codex_secondary_window_and_several_limits():
    limit = {'limitId': 'codex', 'primary': {'usedPercent': 10, 'windowDurationMins': 300, 'resetsAt': 1},
             'secondary': {'usedPercent': 40.5, 'windowDurationMins': 10080, 'resetsAt': 2}, 'planType': 'plus'}
    only = usage.parse_codex({'rateLimits': limit}, NOW)
    assert [(w['name'], w['used_percent']) for w in only['windows']] == [('5 h', 10), ('week', 40.5)]
    other = {'limitId': 'codex_other', 'limitName': 'Spark', 'primary': {'usedPercent': 100, 'windowDurationMins': 60},
             'rateLimitReachedType': 'primary'}
    both = usage.parse_codex({'rateLimitsByLimitId': {'codex': limit, 'codex_other': other},
                              'ordinaryUsageAllowed': False}, NOW)
    assert [w['name'] for w in both['windows']] == ['codex 5 h', 'codex week', 'Spark 60 min']
    assert both['notes'] == {'reached': ['Spark primary'], 'ordinary_usage_allowed': False}
    with pytest.raises(usage.ProbeError):
        usage.parse_codex({'rateLimits': None}, NOW)


def test_observe_both_event_shapes_and_ignores_other_lines():
    claude = usage.observe({'type': 'rate_limit_event', 'rate_limit_info': {
        'status': 'allowed_warning', 'unifiedWindows': {
            'five_hour': {'utilization': 0.45, 'resetsAt': 1794651000, 'status': 'allowed'},
            'seven_day': {'utilization': 0.12, 'resetsAt': 1795176000, 'status': 'allowed'},
            'seven_day_overage_included': {'utilization': 1, 'resetsAt': 0, 'status': 'rejected'}}}}, NOW)
    assert claude['source'] == 'turn' and claude['notes'] == {'status': 'allowed_warning'}
    assert [(w['name'], w['used_percent'], w['resets_at'], w['status']) for w in claude['windows']] == [
        ('session', 45, 1794651000, 'allowed'), ('week (all models)', 12, 1795176000, 'allowed'),
        ('seven_day_overage_included', 100, None, 'rejected')]
    codex = usage.observe({'method': 'account/rateLimits/updated', 'params': {'rateLimits': CODEX_RESULT['rateLimits']}}, NOW)
    assert codex['executor'] == 'codex' and codex['source'] == 'turn' and codex['windows'][0]['used_percent'] == 51
    assert usage.observe({'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed'}}) is None
    assert usage.observe({'type': 'assistant', 'message': {}}) is None
    assert usage.observe({'method': 'item/agentMessage/delta', 'params': {}}) is None
    assert usage.observe(None) is None and usage.observe([1]) is None


def test_store_keeps_newest_and_passive_older_does_not_overwrite_live(tmp_path):
    store = usage.UsageStore(tmp_path / 'state' / 'usage.json')
    live = usage.snapshot('claude', 'live', NOW, [usage.window('session', 46)])
    assert store.update(live)
    older = usage.snapshot('claude', 'turn', NOW - 60, [usage.window('session', 40)])
    assert not store.update(older)
    assert not store.update(usage.snapshot('claude', 'none', None, error='live probe failed: x'))
    assert store.load()['claude'] == live
    newer = {**older, 'observed_at': NOW + 60, 'windows': [{**older['windows'][0], 'observed_at': NOW + 60}]}
    assert store.update(newer) and store.load()['claude'] == newer
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert [p.name for p in store.path.parent.iterdir() if p.name.startswith('.usage-')] == []
    store.path.write_text('{broken')
    assert store.load() == {}


def test_turn_event_merges_windows_by_name_into_live_probe(tmp_path):
    store = usage.UsageStore(tmp_path / 'usage.json')
    live = usage.parse_claude(claude_result(), NOW)
    live['notes'] = {'billing': 'subscription'}
    store.update(live)
    turn = usage.observe({'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed', 'unifiedWindows': {
        'five_hour': {'utilization': 0.5, 'resetsAt': 1794651000, 'status': 'allowed'},
        'seven_day': {'utilization': 0.13, 'resetsAt': 1795176000, 'status': 'allowed'}}}}, NOW + 600)
    assert store.update(turn)
    merged = store.load()['claude']
    assert merged['source'] == 'turn' and merged['observed_at'] == NOW + 600
    assert merged['notes'] == {'status': 'allowed'}
    assert [(w['name'], w['used_percent'], w['observed_at']) for w in merged['windows']] == [
        ('session', 50, NOW + 600), ('week (all models)', 13, NOW + 600), ('week (Fable)', 19, NOW)]
    # A per-window observation older than the stored one never wins, even in a newer snapshot.
    late = usage.snapshot('claude', 'turn', NOW + 700, [usage.window('session', 1, observed_at=NOW + 300),
                                                         usage.window('week (Fable)', 20)])
    assert store.update(late)
    merged = store.load()['claude']
    assert [(w['name'], w['used_percent']) for w in merged['windows']] == [
        ('session', 50), ('week (all models)', 13), ('week (Fable)', 20)]
    assert merged['observed_at'] == NOW + 700 and merged['notes'] == {'status': 'allowed'}
    # Plan comes from the newest snapshot that has one.
    store.update(usage.parse_codex(CODEX_RESULT, NOW))
    store.update(usage.snapshot('codex', 'turn', NOW + 60, [usage.window('week', 52, 1795010673)]))
    codex = store.load()['codex']
    assert codex['plan'] == 'prolite' and codex['windows'][0]['used_percent'] == 52


def test_old_store_without_window_times_still_loads(tmp_path):
    path = tmp_path / 'usage.json'
    path.write_text(json.dumps({'claude': {'executor': 'claude', 'source': 'live', 'observed_at': NOW,
        'windows': [{'name': 'session', 'used_percent': 46, 'resets_at': None, 'resets_text': None, 'status': None}],
        'plan': None, 'notes': {}, 'error': None}}))
    store = usage.UsageStore(path)
    assert store.load()['claude']['windows'][0]['observed_at'] == NOW
    assert store.update(usage.snapshot('claude', 'turn', NOW + 5, [usage.window('week (all models)', 12)]))
    assert [w['observed_at'] for w in store.load()['claude']['windows']] == [NOW, NOW + 5]


def test_format_live_stale_and_unknown_without_tables():
    live = usage.parse_claude(claude_result(), NOW)
    stale = {**usage.parse_codex(CODEX_RESULT, NOW - 7200), 'source': 'turn',
             'error': 'live probe failed: timed out after 20 s'}
    text = usage.format_usage([live, stale], NOW)
    lines = text.splitlines()
    assert lines[0] == 'Claude: live'
    assert lines[1] == 'session: 54% left (46% used), resets in 1 h 10 min'
    assert 'week (all models): 88% left (12% used), resets in 6 d 3 h' in lines
    assert 'Codex (prolite): last seen in a turn 2 h ago; live probe failed: timed out after 20 s' in lines
    assert 'Limit reset credits available: 3' in lines
    assert '|' not in text
    unknown = usage.snapshot('codex', 'none', None, error='live probe failed: cannot start codex')
    no_epoch = usage.snapshot('claude', 'live', NOW - 600, [usage.window('session', 7.5, resets_text='Nov 14, 3pm')])
    text = usage.format_usage([no_epoch, unknown], NOW)
    assert text == ('Claude: live probe 10 min ago\nsession: 92.5% left (7.5% used), resets Nov 14, 3pm\n\n'
                    'Codex: unknown (live probe failed: cannot start codex).')
    passed = usage.snapshot('claude', 'turn', NOW - 90000, [usage.window('week', 100, NOW - 10, status='rejected')])
    assert 'reset time has passed, data is outdated [rejected]' in usage.format_usage([passed], NOW)
    mixed = usage.snapshot('claude', 'turn', NOW - 120, [usage.window('session', 50),
                           usage.window('week (Fable)', 19, observed_at=NOW - 7200),
                           usage.window('week (all models)', 13, observed_at=NOW - 150)])
    lines = usage.format_usage([mixed], NOW).splitlines()
    assert lines == ['Claude: last seen in a turn 2 min ago', 'session: 50% left (50% used)',
                     'week (Fable): 81% left (19% used) (as of 2 h ago)', 'week (all models): 87% left (13% used)']


def script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text('#!' + sys.executable + '\n' + body)
    path.chmod(0o700)
    return path


def alive(pid):
    entry = process_table().get(pid)
    return entry is not None and entry[2] != 'Z'


async def test_claude_probe_runs_fake_binary_and_strips_api_env(tmp_path, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'must-not-pass')
    log = tmp_path / 'log.json'
    binary = script(tmp_path, 'claude', f'''
import json, os, sys
prompt = sys.stdin.read()
json.dump({{"argv": sys.argv[1:], "prompt": prompt, "cwd": os.getcwd(),
           "env": [k for k in os.environ if k.startswith(("CLAUDE", "ANTHROPIC"))]}}, open({str(log)!r}, "w"))
print({claude_result()!r})
''')
    snap = await usage.probe_claude(binary, timeout=10)
    assert snap['error'] is None and len(snap['windows']) == 3
    seen = json.loads(log.read_text())
    assert seen['prompt'] == '/usage' and seen['env'] == []
    assert seen['argv'][seen['argv'].index('--tools') + 1] == '' and '--no-session-persistence' in seen['argv']
    assert not os.path.exists(seen['cwd'])  # Temporary empty directory, removed afterwards.


async def test_probe_timeout_kills_the_process_group(tmp_path):
    pids = tmp_path / 'pids.json'
    binary = script(tmp_path, 'claude', f'''
import json, os, subprocess, time
child = subprocess.Popen(["sleep", "60"])
json.dump([os.getpid(), child.pid], open({str(pids)!r}, "w"))
time.sleep(60)
''')
    started = time.monotonic()
    snap = await usage.probe_claude(binary, timeout=1)
    assert 'timed out after 1 s' in snap['error'] and snap['windows'] == []
    assert time.monotonic() - started < 5
    await until(lambda: not any(alive(pid) for pid in json.loads(pids.read_text())))


async def test_codex_probe_reads_limits_then_terminates(tmp_path):
    pids = tmp_path / 'pid'
    binary = script(tmp_path, 'codex', f'''
import json, os, sys, time
open({str(pids)!r}, "w").write(str(os.getpid()))
assert sys.argv[1:] == ["app-server", "--listen", "stdio://"]
print(json.dumps({{"method": "configWarning", "params": {{}}}}), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    if message.get("id") == 1:
        print(json.dumps({{"id": 1, "result": {{"userAgent": "fake"}}}}), flush=True)
    elif message.get("id") == 2:
        assert message["method"] == "account/rateLimits/read"
        print(json.dumps({{"id": 2, "result": {CODEX_RESULT!r}}}), flush=True)
        time.sleep(60)  # Must be terminated, not waited for.
''')
    started = time.monotonic()
    snap = await usage.probe_codex(binary, timeout=10)
    assert snap['error'] is None and snap['plan'] == 'prolite'
    assert time.monotonic() - started < 5
    assert not alive(int(pids.read_text()))


async def test_service_falls_back_to_stored_snapshot_on_probe_failure(tmp_path):
    service = usage.UsageService(tmp_path, tmp_path / 'missing-claude', tmp_path / 'missing-codex', timeout=5)
    service.observe({'method': 'account/rateLimits/updated', 'params': {'rateLimits': CODEX_RESULT['rateLimits']}})
    claude, codex = await service.snapshot()
    assert claude['windows'] == [] and claude['error'].startswith('live probe failed: cannot start')
    assert codex['source'] == 'turn' and codex['windows'] and codex['error'].startswith('live probe failed')
    cached = await service.snapshot(live=False)
    assert cached[0]['error'] == 'nothing stored yet' and cached[1]['error'] is None


def test_cli_cached_json_needs_no_telegram_token(tmp_path, monkeypatch, capsys):
    import eivoh.__main__ as entry
    monkeypatch.delenv('EIVOH_TELEGRAM_TOKEN', raising=False)
    monkeypatch.setenv('EIVOH_STATE_DIR', str(tmp_path))
    usage.UsageStore(tmp_path / 'usage.json').update(usage.parse_codex(CODEX_RESULT, time.time()))
    entry.main(['usage', '--cached', '--json'])
    claude, codex = json.loads(capsys.readouterr().out)
    assert claude['error'] == 'nothing stored yet' and codex['plan'] == 'prolite'
    entry.main(['usage', '--cached'])
    assert 'Codex (prolite): live\nweek: 49% left' in capsys.readouterr().out


class FakeUsage:
    def __init__(self):
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.observed = []

    def observe(self, payload):
        self.observed.append(payload)

    async def snapshot(self):
        self.started.set()
        await self.release.wait()
        return [usage.snapshot('claude', 'live', time.time(), [usage.window('session', 46)])]


@pytest.fixture
async def usage_rig(tmp_path):
    fixture = Path(__file__).parent / 'fixtures' / 'fake_claude.py'
    binary = tmp_path / 'fake-claude'
    binary.write_text('#!' + sys.executable + '\n' + fixture.read_text().split('\n', 1)[1])
    binary.chmod(0o700)
    storage = Storage(tmp_path / 'state.db')
    fake = FakeUsage()
    channel = FakeChannel()
    gateway = Gateway(storage, ClaudeExecutor(tmp_path, binary.resolve(), grace=0.1), {'fake': channel},
                      edit_interval=0.03, typing_interval=0.03, usage=fake)
    await gateway.start()
    yield gateway, storage, channel, fake, tmp_path
    await gateway.shutdown()
    storage.close()


async def test_usage_command_answers_in_background_without_holding_the_chat(usage_rig):
    gateway, storage, channel, fake, _ = usage_rig
    await asyncio.wait_for(gateway.receive(inbound(1, '/usage')), 2)
    await asyncio.wait_for(fake.started.wait(), 2)
    assert storage.inbound(1)['state'] == 'done' and storage.turn_for(1) is None
    # The probe is still blocked; the chat lock is free and a new message is journaled and runs.
    await asyncio.wait_for(gateway.receive(inbound(2, 'hello')), 2)
    assert storage.turn_for(2) is not None
    await asyncio.wait_for(gateway.idle(), 5)
    assert not any('left' in text for _, text in channel.sent)
    assert any(p.get('type') == 'rate_limit_event' for p in fake.observed if isinstance(p, dict))
    fake.release.set()
    await until(lambda: any(text.startswith('Claude: live\nsession: 54% left') for _, text in channel.sent))
    assert not gateway.usage_jobs


async def test_usage_observer_errors_never_break_a_turn(usage_rig, monkeypatch):
    gateway, storage, channel, fake, _ = usage_rig
    monkeypatch.setattr(fake, 'observe', lambda payload: (_ for _ in ()).throw(RuntimeError('observer bug')))
    await gateway.receive(inbound(1, 'hello'))
    await asyncio.wait_for(gateway.idle(), 5)
    assert storage.turn_for(1)['status'] == 'completed'


async def test_usage_shutdown_cancels_pending_check_and_unconfigured_answer(usage_rig, tmp_path):
    gateway, storage, channel, fake, _ = usage_rig
    await gateway.receive(inbound(1, '/usage'))
    await fake.started.wait()
    job = next(iter(gateway.usage_jobs))
    await gateway.shutdown()
    assert job.cancelled() and not any('left' in text for _, text in channel.sent)
    plain = Gateway(Storage(tmp_path / 'other.db'), gateway.executor, {'fake': channel})
    await plain.receive(inbound(2, '/usage'))
    assert channel.sent[-1][1] == 'Usage is not configured.'
    assert plain.storage.inbound(1)['state'] == 'done'
    await plain.shutdown()
    plain.storage.close()
