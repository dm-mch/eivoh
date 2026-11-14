"""Exercise release orchestration with command stubs; no git/service mutations."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


# templates=False: a revision before V3.1 (rollback) has no templates/home to seed.
@pytest.mark.parametrize('existing,restart,fail,seed_fail,templates', [
    (False, False, False, False, True), (False, True, False, False, True), (True, False, False, False, True),
    (True, True, True, False, True), (True, True, False, True, True), (True, True, False, False, False),
])
def test_release_worktree_validation_and_explicit_restart(tmp_path, existing, restart, fail, seed_fail, templates):
    release = tmp_path / 'run'
    source = tmp_path / 'source'
    source.mkdir()
    log = tmp_path / 'commands.jsonl'
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    # Substitute only the destination in a copy; never alter HOME or real git.
    script = tmp_path / 'release.sh'
    script.write_text((ROOT / 'deploy/release.sh').read_text().replace(
        'release_dir="$HOME/.local/share/eivoh/run"', f'release_dir="{release}"'))
    stub = '#!' + sys.executable + '\n' + '''
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['TEST_RELEASE_LOG'], 'a') as out:
    out.write(json.dumps([name, *args]) + '\\n')
if name == 'git':
    if '--show-toplevel' in args:
        print(os.environ['TEST_RELEASE_SOURCE'])
    elif '--verify' in args:
        print('candidate-commit')
    elif 'HEAD' in args:
        print('previous-commit')
elif name == 'python' and args[:2] == ['-m', 'eivoh.home']:
    sys.exit(int(os.environ['TEST_RELEASE_SEED_FAIL']))
elif name == 'python':
    assert os.environ['EIVOH_LIVE'] == '0'
    sys.exit(int(os.environ['TEST_RELEASE_FAIL']))
'''
    for name in ['git', 'uv', 'systemctl']:
        binary = binaries / name
        binary.write_text(stub)
        binary.chmod(0o700)
    if existing:
        release.mkdir()
        (release / '.git').write_text('stub worktree')
    # The git worktree/uv stubs create the fake venv when first called.
    git = binaries / 'git'
    git.write_text(git.read_text() + '''
if 'worktree' in args:
    release = Path(os.environ['TEST_RELEASE_DEST'])
    release.mkdir()
    (release / '.git').write_text('stub worktree')
if ('worktree' in args or 'checkout' in args) and os.environ['TEST_RELEASE_TEMPLATES'] == '1':
    (Path(os.environ['TEST_RELEASE_DEST']) / 'templates/home').mkdir(parents=True)
''')
    uv = binaries / 'uv'
    uv.write_text(uv.read_text() + '''
if 'venv' in args:
    binary = Path('.venv/bin/python')
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(Path(os.environ['TEST_RELEASE_PYTHON_STUB']).read_text())
    binary.chmod(0o700)
''')
    python_stub = tmp_path / 'python-stub'
    python_stub.write_text(stub)
    env = dict(os.environ, PATH=f'{binaries}:/usr/bin:/bin',
               TEST_RELEASE_LOG=str(log), TEST_RELEASE_SOURCE=str(source),
               TEST_RELEASE_DEST=str(release), TEST_RELEASE_PYTHON_STUB=str(python_stub),
               TEST_RELEASE_FAIL=str(int(fail)), TEST_RELEASE_SEED_FAIL=str(int(seed_fail)),
               TEST_RELEASE_TEMPLATES=str(int(templates)), EIVOH_HOME=str(tmp_path / 'home'))  # Never the real ~/eivoh-home.
    result = subprocess.run(['bash', str(script), 'requested-commit'] + (['--restart'] if restart else []),
                            env=env, capture_output=True, text=True)
    assert result.returncode == int(fail or seed_fail), result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert any(row[0:3] == ['uv', 'pip', 'sync'] for row in commands)
    seed = ['python', '-m', 'eivoh.home', 'seed', '--home', str(tmp_path / 'home'), '--templates',
            f'{release}/templates/home', '--core', str(source), '--commit', 'candidate-commit']
    pytest_run = ['python', '-m', 'pytest', '-q', '-m', 'not live']
    assert pytest_run in commands
    # Only a validated release with templates touches the home.
    assert (seed in commands) == (templates and not fail)
    if seed in commands:
        assert commands.index(pytest_run) < commands.index(seed)
    assert ('Validated release' in result.stdout) == (not fail and not seed_fail)
    assert any(row[0] == 'systemctl' for row in commands) == (restart and not fail and not seed_fail)
    if existing:
        assert 'Previous commit (rollback): previous-commit' in result.stdout
        assert ['git', '-C', str(release), 'checkout', '--detach', 'candidate-commit'] in commands
    else:
        assert ['git', '-C', str(source), 'worktree', 'add', '--detach', str(release), 'candidate-commit'] in commands


def test_unit_uses_release_and_mixed_shutdown():
    unit = (ROOT / 'deploy/eivoh.service').read_text()
    assert 'WorkingDirectory=%h/.local/share/eivoh/run\n' in unit
    assert 'ExecStart=%h/.local/share/eivoh/run/.venv/bin/python -m eivoh\n' in unit
    assert 'KillMode=mixed\n' in unit
