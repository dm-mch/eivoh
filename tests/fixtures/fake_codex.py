#!/usr/bin/env python3
"""Fake codex for offline tests: emits the probed `codex exec --json` shapes.

`exec [resume ID] ... -`: the prompt comes from stdin. A prompt containing
"sleep" starts a command in its own session, like codex, and waits for it.
"""
import json
import os
import subprocess
import sys
import uuid

args = sys.argv[1:]
assert args[0] == "exec" and args[-1] == "-", args
resume = len(args) > 1 and args[1] == "resume"
thread = args[-2] if resume else str(uuid.uuid4())
prompt = sys.stdin.read()
with open(os.environ["FAKE_CODEX_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "prompt": prompt, "openai": "OPENAI_API_KEY" in os.environ}) + "\n")


def emit(event):
    print(json.dumps(event), flush=True)


emit({"type": "thread.started", "thread_id": thread})
emit({"type": "turn.started"})
emit({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "Working on it.\n"}})
if "sleep" in prompt:
    command = {"id": "item_1", "type": "command_execution", "command": "sleep 60", "status": "in_progress"}
    emit({"type": "item.started", "item": command})
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    child.wait()
if "fail" in prompt:
    emit({"type": "turn.failed", "error": {"message": "fake failure"}})
    sys.exit(1)
emit({"type": "item.completed", "item": {"id": "item_2", "type": "agent_message", "text": f"echo: {prompt} resumed={resume}"}})
emit({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
