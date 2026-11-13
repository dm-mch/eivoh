from dataclasses import dataclass
import os
from pathlib import Path
import shlex

from .speech import provider_from_env


def validate_codex_args(args):
    """Options supported by Eivoh's local stdio app-server, not exec options."""
    owned = {'exec', 'resume', 'app-server', '--listen', '--stdio', '--json', '-C', '--cd', '--model', '-m'}
    index = 0
    while index < len(args):
        arg = args[index]
        flag, equals, value = arg.partition('=')
        if flag in owned or arg.startswith('-m'):
            raise ValueError('EIVOH_CODEX_ARGS contains an adapter-owned argument')
        if arg == '--strict-config':
            index += 1
            continue
        if flag not in {'-c', '--config', '--enable', '--disable'}:
            raise ValueError(f'EIVOH_CODEX_ARGS now configures app-server; option {flag!r} is not supported. '
                             'Migrate exec settings to -c/--config, --enable, --disable or --strict-config.')
        if not equals:
            index += 1
            value = args[index] if index < len(args) else ''
        if not value or value.startswith('-'):
            raise ValueError(f'EIVOH_CODEX_ARGS app-server option {flag} requires a value')
        if flag in {'-c', '--config'} and ('=' not in value or not value.split('=', 1)[0]):
            raise ValueError(f'EIVOH_CODEX_ARGS app-server option {flag} requires key=value')
        index += 1


@dataclass(frozen=True)
class Config:
    telegram_token: str
    owner_id: int
    state_dir: Path
    workdir: Path
    claude_model: str | None = None
    permission_mode: str = "bypassPermissions"
    claude_bin: str = "claude"
    claude_args: tuple[str, ...] = ()
    stt_provider: str | None = None
    default_executor: str = "claude"
    codex_bin: str = "codex"
    codex_model: str | None = None
    codex_args: tuple[str, ...] = ()
    max_parallel: int = 3

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("EIVOH_TELEGRAM_TOKEN", "").strip()
        owner = os.environ.get("EIVOH_OWNER_ID", "")
        if not token or not owner.isdecimal() or int(owner) <= 0:
            raise ValueError("EIVOH_TELEGRAM_TOKEN and a positive numeric EIVOH_OWNER_ID are required")
        if not os.environ.get("EIVOH_WORKDIR", "").strip():
            raise ValueError("EIVOH_WORKDIR must be set explicitly")
        workdir = Path(os.environ["EIVOH_WORKDIR"]).expanduser().resolve()
        if not workdir.is_dir():
            raise ValueError("EIVOH_WORKDIR must be an existing directory")
        mode = os.environ.get("EIVOH_CLAUDE_PERMISSION_MODE", "bypassPermissions")
        if mode not in {"acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"}:
            raise ValueError("Unknown EIVOH_CLAUDE_PERMISSION_MODE")
        try:
            claude_args = tuple(shlex.split(os.environ.get("EIVOH_CLAUDE_ARGS", "")))
        except ValueError as exc:
            raise ValueError("Invalid quoting in EIVOH_CLAUDE_ARGS") from exc
        owned = {"--resume", "-p", "--print", "--output-format", "--input-format", "--permission-mode", "--model", "--replay-user-messages"}
        for arg in claude_args:
            if arg.split("=", 1)[0] in owned or arg.startswith("-p"):
                raise ValueError("EIVOH_CLAUDE_ARGS contains an adapter-owned flag")
        try:
            codex_args = tuple(shlex.split(os.environ.get("EIVOH_CODEX_ARGS", "")))
        except ValueError as exc:
            raise ValueError("Invalid quoting in EIVOH_CODEX_ARGS") from exc
        validate_codex_args(codex_args)
        default_executor = os.environ.get("EIVOH_DEFAULT_EXECUTOR", "claude").strip().lower()
        if default_executor not in {"claude", "codex"}:
            raise ValueError("EIVOH_DEFAULT_EXECUTOR must be claude or codex")
        parallel = os.environ.get("EIVOH_MAX_PARALLEL", "3").strip()
        if not parallel.isdecimal() or int(parallel) < 1:
            raise ValueError("EIVOH_MAX_PARALLEL must be an integer >= 1")
        return cls(token, int(owner),
                   Path(os.environ.get("EIVOH_STATE_DIR", "~/.local/state/eivoh")).expanduser().resolve(),
                   workdir, os.environ.get("EIVOH_CLAUDE_MODEL") or None,
                   mode, os.environ.get("EIVOH_CLAUDE_BIN", "claude"), claude_args,
                   provider_from_env(os.environ), default_executor,
                   os.environ.get("EIVOH_CODEX_BIN", "codex"), os.environ.get("EIVOH_CODEX_MODEL") or None, codex_args, int(parallel))
