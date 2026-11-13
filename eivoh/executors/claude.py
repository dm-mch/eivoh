"""Own subprocess/JSONL adapter, based on the installed CLI probe, not an SDK."""
import json
import os
from pathlib import Path
import uuid

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
        content = message.get('content', [])
        if isinstance(content, str):
            content = [{'type': 'text', 'text': content}]
        if kind == "assistant" and not raw.get("parent_tool_use_id"):
            add("message", text="".join(b.get("text", "") for b in content if b.get("type") == "text"),
                message_id=message.get("id"), observed_model=message.get("model"),
                observed_model_source="assistant.message")
        for block in content:
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

    def command(self, session_id, model=None, effort=None):
        args = [self.binary, "-p", "--output-format", "stream-json", "--verbose",
                "--include-partial-messages", "--permission-mode", self.permission_mode]
        if session_id:
            args += ["--resume", session_id]
        model = model or self.model
        if model:
            args += ["--model", model]
        extra = list(self.extra_args)
        if effort is not None:
            # Remove both CLI spellings and duplicates so task settings always win.
            filtered, index = [], 0
            while index < len(extra):
                arg = extra[index]
                if arg == '--effort':
                    index += 2
                    continue
                if not arg.startswith('--effort='):
                    filtered.append(arg)
                index += 1
            extra = filtered + ['--effort', effort]
        return args + extra

    def environment(self):
        # Inherited API keys would switch the CLI from the subscription to API billing.
        return {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC"))}

    def parser(self):
        return parse_line


from .interactive import InteractiveExecutor


class ClaudeStreamExecutor(InteractiveExecutor, ClaudeExecutor):
    """Ordinary Claude tasks with UUID-correlated streamed user input."""
    # Claude echoes only after its current tool completes, which can take hours.
    # Lifecycle/stop ends the wait; a healthy long tool is not an ack timeout.
    ack_timeout = None
    def command(self, session_id, model=None, effort=None):
        return super().command(session_id, model, effort) + ['--input-format', 'stream-json', '--replay-user-messages']

    async def initialize(self, run, prompt, model, effort=None):
        await run.write({'type': 'user', 'uuid': str(uuid.uuid4()),
                         'message': {'role': 'user', 'content': prompt}})
        run.ready.set()

    async def send_steer(self, run, input_id, text):
        await run.write({'type': 'user', 'uuid': input_id,
                         'message': {'role': 'user', 'content': text}})

    async def receive(self, run, raw):
        if run.terminal:
            return ()
        record = parse_line(json.dumps(raw))
        events = list(record.events)
        if raw.get('type') == 'user' and raw.get('isReplay') and raw.get('uuid') in run.inputs:
            run.items[raw['uuid']] = True
            events.append(Event('input_accepted', {'input_id': raw['uuid']}, raw, raw.get('session_id')))
        if raw.get('type') == 'result':
            # A write can cross the result boundary. Claude emits another replay
            # and result for that input; do not kill it at the preceding result.
            run.accepting = False
            if raw.get('result'):
                result_id = 'claude:' + (raw.get('uuid') or str(uuid.uuid4()))
                run.answer_parts[result_id] = raw['result']
            if not raw.get('is_error') and any(input_id not in run.items for input_id in run.inputs):
                events = [event for event in events if event.kind != 'turn_completed']
                if raw.get('result'):
                    events.append(Event('answer_part', {'id': result_id, 'text': raw['result']}, raw, raw.get('session_id')))
            else:
                run.terminal = True
                events = [Event(event.kind, {**event.data, 'text': '\n\n'.join(run.answer_parts.values()),
                                             'answer_part_ids': list(run.answer_parts)},
                                event.raw, event.session_id) if event.kind == 'turn_completed' else event
                          for event in events]
        return events
