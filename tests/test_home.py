"""Home seeding: missing files only, placeholder filled, template changes reported."""
import os
from pathlib import Path
import subprocess

from eivoh.home import main

ROOT = Path(__file__).resolve().parents[1]


def run(capsys, home, templates, core, commit):
    code = main(['seed', '--home', str(home), '--templates', str(templates), '--core', str(core), '--commit', commit])
    return code, capsys.readouterr().out


def git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t',
               GIT_COMMITTER_EMAIL='t@t', GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_SYSTEM='/dev/null')
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()


def test_real_template_has_core_placeholder():
    assert '{{EIVOH_CORE}}' in (ROOT / 'templates/home/AGENTS.md').read_text()


def test_seeds_missing_files_only(tmp_path, capsys):
    templates, home, core = tmp_path / 'templates', tmp_path / 'home', tmp_path / 'core'
    (templates / 'experience').mkdir(parents=True)
    (templates / 'skills').mkdir()
    core.mkdir()
    (templates / 'AGENTS.md').write_text('Core is at {{EIVOH_CORE}}; again {{EIVOH_CORE}}.\n')
    (templates / '.hidden').write_text('dot\n')
    (templates / 'experience' / 'index.md').write_text('new index\n')
    (templates / 'README.md').write_text('template readme\n')
    (templates / 'notes.md').write_text('template notes\n')
    (templates / 'skills' / 'one.md').write_text('skill\n')
    (templates / 'link.md').symlink_to(templates / 'README.md')  # Template symlinks are not files to seed.
    (home / 'experience').mkdir(parents=True)
    (home / 'experience' / 'index.md').write_text('my index\n')
    (home / 'README.md').symlink_to(tmp_path / 'nowhere')  # Dangling symlink still counts as existing.
    (home / 'notes.md').mkdir()
    (home / 'skills').symlink_to(tmp_path / 'elsewhere')  # Never write through a symlinked directory.
    code, out = run(capsys, home, templates, core, 'c1')
    assert code == 0
    assert (home / 'AGENTS.md').read_text() == f'Core is at {core}; again {core}.\n'
    assert (home / '.hidden').read_text() == 'dot\n'
    assert (home / 'experience' / 'index.md').read_text() == 'my index\n'
    assert (home / 'README.md').is_symlink() and not (tmp_path / 'nowhere').exists()
    assert (home / 'notes.md').is_dir() and not (tmp_path / 'elsewhere').exists()
    assert not (home / 'link.md').exists()
    assert sorted(line for line in out.splitlines() if line.startswith('Seeded')) == [
        f'Seeded {home / ".hidden"}', f'Seeded {home / "AGENTS.md"}']
    assert (home / '.eivoh-template').read_text() == 'c1\n'
    assert 'should be a private git repository (git init)' in out and not (home / '.git').exists()
    (home / 'AGENTS.md').write_text('edited\n')
    (home / '.git').mkdir()
    code, out = run(capsys, home, templates, core, 'c1')
    assert code == 0 and out == '' and (home / 'AGENTS.md').read_text() == 'edited\n'


def test_template_changes_are_reported_not_applied(tmp_path, capsys):
    core, home = tmp_path / 'core', tmp_path / 'home'
    templates = core / 'templates' / 'home'
    templates.mkdir(parents=True)
    (core / 'code.py').write_text('1\n')
    (templates / 'AGENTS.md').write_text('v1\n')
    git(core, 'init', '-q')
    git(core, 'add', '.')
    git(core, 'commit', '-qm', 'one')
    first = git(core, 'rev-parse', 'HEAD')
    (home / '.git').mkdir(parents=True)
    assert run(capsys, home, templates, core, first) == (0, f'Seeded {home / "AGENTS.md"}\n')
    # A core change outside the templates: the state moves silently.
    (core / 'code.py').write_text('2\n')
    git(core, 'commit', '-qam', 'two')
    second = git(core, 'rev-parse', 'HEAD')
    assert run(capsys, home, templates, core, second) == (0, '')
    assert (home / '.eivoh-template').read_text() == second + '\n'
    # A template change: reported, nothing written, the state stays.
    (templates / 'AGENTS.md').write_text('v2\n')
    git(core, 'commit', '-qam', 'three')
    third = git(core, 'rev-parse', 'HEAD')
    code, out = run(capsys, home, templates, core, third)
    assert code == 0
    assert second in out and third in out and 'templates/home/AGENTS.md' in out
    assert 'home was not modified' in out and 'Changing yourself' in out
    assert (home / 'AGENTS.md').read_text() == 'v1\n'
    assert (home / '.eivoh-template').read_text() == second + '\n'
    # Unknown old commit: cannot compare, state unchanged.
    (home / '.eivoh-template').write_text('0' * 40 + '\n')
    code, out = run(capsys, home, templates, core, third)
    assert code == 0 and 'could not compare' in out
    assert (home / '.eivoh-template').read_text() == '0' * 40 + '\n'


def test_not_a_repository_cannot_compare(tmp_path, capsys):
    templates, home = tmp_path / 'templates', tmp_path / 'home'
    templates.mkdir()
    (home / '.git').mkdir(parents=True)
    (home / '.eivoh-template').write_text('old\n')
    code, out = run(capsys, home, templates, tmp_path / 'not-a-repo', 'new')
    assert code == 0 and 'could not compare' in out
    assert (home / '.eivoh-template').read_text() == 'old\n'


def test_real_errors_fail(tmp_path, capsys):
    blocker = tmp_path / 'file'
    blocker.write_text('')
    templates = tmp_path / 'templates'
    templates.mkdir()
    assert main(['seed', '--home', str(tmp_path / 'home'), '--templates', str(tmp_path / 'missing'),
                 '--core', str(tmp_path), '--commit', 'c']) == 1
    assert main(['seed', '--home', str(blocker / 'home'), '--templates', str(templates),
                 '--core', str(tmp_path), '--commit', 'c']) == 1
    assert 'Home seeding failed' in capsys.readouterr().err
