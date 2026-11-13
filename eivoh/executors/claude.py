"""Own subprocess/JSONL adapter, based on the installed CLI probe, not an SDK."""
import json
import os
from pathlib import Path

from .base import Event, Record
from .cli import CliExecutor, ProcessTree, process_table  # noqa: F401  (re-exported)


def _parse_line(line: str) -> Record:
    try:
        raw = json.loads(line)
    except (ValueError, TypeError):
        return Record(line, None, (Event("error", {"message": "Invalid executor JSON"}, line),))
    if not isinstance(raw, dict):
        return Record(line, raw)
    sid = raw.get("session_id")
    events = []

    def add(kind, **data):
        events.append(Event(kind, data, raw, sid))

    kind = raw.get("type")
    if kind == "system" and raw.get("subtype") == "init" and not raw.get("parent_tool_use_id"):
        add("session_started", session_id=sid, observed_model=raw.get("model"),
            observed_model_source="system.init")
    elif kind == "stream_event":
        event = raw.get("event", {})
        # Subagent output is journaled but must not become the main answer.
        if not raw.get("parent_tool_use_id"):
            if event.get("type") == "content_block_delta" and event.get("delta", {}).get("type") == "text_delta":
                add("text_delta", text=event["delta"].get("text", ""))
            elif event.get("type") == "content_block_start" and event.get("content_block", {}).get("type") == "tool_use":
                add("tool_started", **event["content_block"])
    elif kind in {"assistant", "user"}:
        message = raw.get("message", {})
        if kind == "assistant" and not raw.get("parent_tool_use_id"):
            add("message", text="".join(b.get("text", "") for b in message.get("content", []) if b.get("type") == "text"),
                message_id=message.get("id"), observed_model=message.get("model"),
                observed_model_source="assistant.message")
        for block in message.get("content", []):
            if block.get("type") == "tool_use":
                add("tool_started", **block)
            elif block.get("type") == "tool_result":
                add("tool_finished", **block)
    elif kind == "result":
        add("usage", usage=raw.get("usage", {}), total_cost_usd=raw.get("total_cost_usd"))
        if raw.get("permission_denials"):
            # Observed result field; no invented interactive approval protocol.
            add("approval_requested", denials=raw["permission_denials"], actionable=False)
        if raw.get("is_error"):
            add("error", message=raw.get("result") or raw.get("errors") or "Executor failed")
        add("turn_completed", status="failed" if raw.get("is_error") else "completed", text=raw.get("result", ""))
    elif kind == "error":
        add("error", message=raw.get("error", raw))
    return Record(line, raw, tuple(events))


def parse_line(line: str) -> Record:
    try:
        return _parse_line(line)
    except (TypeError, KeyError, AttributeError, ValueError) as exc:
        # A new/malformed schema must not stop the reader and lose later lines.
        raw = json.loads(line)
        return Record(line, raw, (Event("error", {"message": f"Unsupported event shape: {exc}"}, raw),))


class ClaudeExecutor(CliExecutor):
    def __init__(self, workdir: Path, binary="claude", model=None,
                 permission_mode="bypassPermissions", grace=0.5, extra_args=()):
        super().__init__(workdir, binary, model, grace, extra_args)
        self.permission_mode = permission_mode

    def command(self, session_id, model=None):
        args = [self.binary, "-p", "--output-format", "stream-json", "--verbose",
                "--include-partial-messages", "--permission-mode", self.permission_mode]
        if session_id:
            args += ["--resume", session_id]
        model = model or self.model
        if model:
            args += ["--model", model]
        return args + list(self.extra_args)

    def environment(self):
        # Inherited API keys would switch the CLI from the subscription to API billing.
        return {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC"))}

    def parser(self):
        return parse_line
