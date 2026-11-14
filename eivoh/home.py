"""Seed the agent's home from the core's templates; never overwrite the home.

python -m eivoh.home seed --home <dir> --templates <dir> --core <path> --commit <sha>
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys

PLACEHOLDER = '{{EIVOH_CORE}}'
STATE = '.eivoh-template'
TEMPLATES = 'templates/home'  # Location of the templates inside the core repository.


def seed(home: Path, templates: Path, core: Path, commit: str):
    core = core.resolve()
    if not templates.is_dir():
        raise OSError(f'Templates directory {templates} is not readable')
    home.mkdir(parents=True, exist_ok=True)
    def fail(exc):
        raise exc
    for directory, _, files in sorted(os.walk(templates, onerror=fail)):  # Does not descend into symlinks.
        for name in sorted(files):
            source = Path(directory, name)
            if source.is_symlink() or not source.is_file():
                continue
            relative = source.relative_to(templates)
            target = home / relative
            # Any existing entry (file, directory, symlink, even a dangling one) wins,
            # also on the way: never write through a symlinked or non-directory parent.
            parents = [home / Path(*relative.parts[:i]) for i in range(1, len(relative.parts))]
            if target.is_symlink() or target.exists() or any(
                    path.is_symlink() or (path.exists() and not path.is_dir()) for path in parents):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            data = source.read_bytes().replace(PLACEHOLDER.encode(), str(core).encode())
            # O_EXCL: never write through a path that appeared meanwhile.
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as out:
                out.write(data)
            print(f'Seeded {target}')
    reconcile(home, core, commit)
    if not (home / '.git').exists():
        print(f'Hint: the home {home} should be a private git repository (git init).')


def reconcile(home: Path, core: Path, commit: str):
    state = home / STATE
    if not state.exists():
        state.write_text(commit + '\n')
        return
    old = state.read_text().strip()
    if old == commit:
        return
    try:
        if old.startswith('-') or commit.startswith('-'):
            raise ValueError('not a commit')  # Never let the state file pass git options.
        result = subprocess.run(['git', '-C', str(core), 'diff', '--name-only', old, commit, '--', TEMPLATES],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise ValueError(result.stderr.strip())
    except (OSError, ValueError) as exc:
        print(f'Notice: could not compare the home templates between {old} and {commit}; {state} left unchanged. {exc}')
        return
    changed = result.stdout.split()
    if not changed:
        state.write_text(commit + '\n')
        return
    print(f'Notice: the home templates changed between {old} and {commit}: {", ".join(changed)}. '
          f'The home was not modified; merge the change as a task (see "Changing yourself" in {home / "AGENTS.md"}).')


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m eivoh.home')
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('seed', help='seed missing home files and report template changes')
    for name in ('--home', '--templates', '--core'):
        command.add_argument(name, required=True, type=Path)
    command.add_argument('--commit', required=True)
    args = parser.parse_args(argv)
    try:
        seed(args.home.expanduser(), args.templates, args.core, args.commit)
    except OSError as exc:
        print(f'Home seeding failed: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
