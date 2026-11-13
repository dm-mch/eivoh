"""Codex CLI adapter: one `codex exec --json` process per turn.

Event format from probes of codex-cli 0.167.2 on 2026-11-13 (not from docs):
thread.started{thread_id}, turn.started, item.started/item.completed{item},
turn.completed{usage}, turn.failed{error}, top-level error{message}. Item types
seen: agent_message, command_execution, file_change, web_search, error (a
warning, the turn continues). There are no text deltas: messages arrive whole.
Reference for the exec path: ouroboros (MIT), studied, no copied code.

Probed lifecycle: SIGTERM ends codex at once but leaves its running command
orphaned, so the per-turn subreaper supervisor is required. A session resumes
after SIGTERM or SIGKILL. `exec resume` has no -C, so the cwd selects the
workspace for both start and resume.
"""
import json
import os
from pathlib import Path
import tomllib

from .base import Event, Record
from .cli import CliExecutor

TOOL_ITEMS = {"command_execution", "file_change", "web_search", "mcp_tool_call"}


def tool_name(item):
    kind = item.get("type")
    if kind == "command_execution":
        return f"shell: {item.get('command', '')}"[:120]
    if kind == "mcp_tool_call":
        return f"mcp: {item.get('server', '')}.{item.get('tool', '')}"
    return kind


class CodexParser:
    """Per-turn state: the final answer is the last agent message."""
    def __init__(self):
        self.session_id = None
        self.last_message = ""

    def __call__(self, line: str) -> Record:
        try:
            raw = json.loads(line)
        except (ValueError, TypeError):
            return Record(line, None, (Event("error", {"message": "Invalid executor JSON"}, line),))
        if not isinstance(raw, dict):
            return Record(line, raw)
        try:
            return Record(line, raw, tuple(self._events(raw)))
        except (TypeError, KeyError, AttributeError, ValueError) as exc:
            return Record(line, raw, (Event("error", {"message": f"Unsupported event shape: {exc}"}, raw),))

    def _events(self, raw):
        kind = raw.get("type")
        if kind == "thread.started":
            self.session_id = raw["thread_id"]
            yield Event("session_started", {"session_id": self.session_id}, raw, self.session_id)
            return
        sid = self.session_id
        if kind in {"item.started", "item.completed"}:
            item = raw.get("item") or {}
            itype = item.get("type")
            if itype in TOOL_ITEMS:
                state = "tool_started" if kind == "item.started" else "tool_finished"
                yield Event(state, {"name": tool_name(item), "id": item.get("id"), "status": item.get("status")}, raw, sid)
            elif itype == "agent_message" and kind == "item.completed":
                self.last_message = item.get("text", "")
                # Whole messages are concatenated by the gateway; keep them apart.
                yield Event("message", {"text": self.last_message.rstrip() + "\n\n", "message_id": item.get("id")}, raw, sid)
            # item "error" is a warning (the turn continues): journaled only.
        elif kind == "turn.completed":
            yield Event("usage", {"usage": raw.get("usage", {})}, raw, sid)
            yield Event("turn_completed", {"status": "completed", "text": self.last_message}, raw, sid)
        elif kind == "turn.failed":
            message = (raw.get("error") or {}).get("message") or "Codex turn failed"
            yield Event("error", {"message": message}, raw, sid)
            yield Event("turn_completed", {"status": "failed", "text": self.last_message}, raw, sid)
        # Top-level "error" precedes turn.failed and may be a retry notice:
        # journaled only; a turn without turn.completed is failed by the runner.


class CodexExecutor(CliExecutor):
    def __init__(self, workdir: Path, binary="codex", model=None, grace=0.5, extra_args=()):
        super().__init__(workdir, binary, model, grace, extra_args)

    def command(self, session_id, model=None):
        # The bubblewrap sandbox cannot start on hosts that forbid unprivileged
        # user namespaces; Eivoh runs the executor like Claude's bypass mode.
        args = [self.binary, "exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check"]
        model = model or self.model
        if model:
            args += ["-m", model]
        # All options precede `resume`: `exec resume` rejects some exec options
        # (--add-dir, -p, --color), but accepts them before the subcommand.
        args += list(self.extra_args)
        if session_id:
            args += ["resume", session_id]
        return args + ["-"]  # Prompt from stdin.

    def environment(self):
        # An inherited API key would switch Codex from the ChatGPT subscription.
        return {k: v for k, v in os.environ.items() if not (k.startswith("OPENAI_") or k == "CODEX_API_KEY")}

    def parser(self):
        return CodexParser()

    def effective_model(self, model=None):
        if model or self.model:
            return model or self.model
        # Without -m, codex uses the model from its own config.
        path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
        try:
            model = tomllib.loads(path.read_text()).get("model")
        except (OSError, tomllib.TOMLDecodeError):
            return None
        return f"{model} (codex config)" if model else None
