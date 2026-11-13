from dataclasses import dataclass
import os
from pathlib import Path
import shlex


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
    soniox_api_key: str | None = None

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
        owned = {"--resume", "-p", "--print", "--output-format", "--input-format", "--permission-mode"}
        for arg in claude_args:
            if arg.split("=", 1)[0] in owned or arg.startswith("-p"):
                raise ValueError("EIVOH_CLAUDE_ARGS contains an adapter-owned flag")
        return cls(token, int(owner),
                   Path(os.environ.get("EIVOH_STATE_DIR", "~/.local/state/eivoh")).expanduser().resolve(),
                   workdir, os.environ.get("EIVOH_CLAUDE_MODEL") or None,
                   mode, os.environ.get("EIVOH_CLAUDE_BIN", "claude"), claude_args,
                   os.environ.get("SONIOX_API_KEY", "").strip() or None)
