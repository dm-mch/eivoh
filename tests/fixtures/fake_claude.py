#!/usr/bin/env python3
"""Offline executable fixture: reads stdin to EOF, logs argv and prompt."""
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import time

prompt = sys.stdin.read()
args = sys.argv[1:]
resume = args[args.index("--resume") + 1] if "--resume" in args else None
sid = resume or f"session-{os.getpid()}"
with open("prompts.jsonl", "a") as f:
    f.write(json.dumps({"prompt": prompt, "resume": resume, "sid": sid, "args": args,
                        "env_keys": [k for k in os.environ if k.startswith(("CLAUDE", "ANTHROPIC"))]}) + "\n")


def emit(kind, **data):
    print(json.dumps({"type": kind, "session_id": sid, **data}), flush=True)


def delta(text):
    emit("stream_event", event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}})


def terminate(signum, frame):
    emit("shutdown_notice", detail="last buffered line")
    sys.exit(0)


signal.signal(signal.SIGTERM, terminate)
if prompt == "before-init":
    time.sleep(60)
emit("system", subtype="init", apiKeySource="none")
emit("rate_limit_event", rate_limit_info={"status": "allowed"})
delta("Working ")
if prompt in {"hang", "crash", "complete-with-child", "stderr-flood"}:
    # Grandchild escapes BOTH parent and CLI sessions. Record only our own PIDs.
    child = subprocess.Popen([sys.executable, "-c", '''
import subprocess, os, json, time
from pathlib import Path
p = subprocess.Popen(["sleep", "60"], start_new_session=True)
Path("pids.json.tmp").write_text(json.dumps([os.getpid(), p.pid]))
os.replace("pids.json.tmp", "pids.json")  # Atomic: tests read it as soon as it exists.
time.sleep(60)
'''], start_new_session=True)
    while not Path("pids.json").exists():
        time.sleep(0.005)
    emit("assistant", message={"id": "tool-message", "content": [{"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "sleep 60"}}]})
    if prompt == "crash":
        time.sleep(0.15)
        sys.exit(2)
    if prompt == "stderr-flood":
        for _ in range(500):
            print("diagnostic " * 100, file=sys.stderr, flush=True)
    if prompt != "complete-with-child":
        time.sleep(60)
if prompt.startswith("double-fork"):
    # Both short-lived ancestors exit before the adapter's next ancestry scan.
    child = subprocess.Popen([sys.executable, "-c", '''
import os, time
from pathlib import Path
if os.fork():
    os._exit(0)
os.setsid()
if os.fork():
    os._exit(0)
Path("orphan.pid").write_text(str(os.getpid()))
time.sleep(60)
'''])
    child.wait()
    while not Path("orphan.pid").exists():
        time.sleep(0.005)
    if prompt == "double-fork-crash":
        sys.exit(2)
    if prompt == "double-fork-hang":
        time.sleep(60)
if prompt == "large-line":
    emit("future_event", payload="x" * (9 * 1024 * 1024))
    print('{"type": "assistant", "message": null}', flush=True)
if prompt == "invalid":
    print("not json", flush=True)
    sys.exit(0)
if prompt == "failed":
    emit("result", subtype="error_during_execution", is_error=True, result="deliberate error", usage={})
    sys.exit(1)
time.sleep(0.12)
delta("done: " + prompt)
emit("assistant", message={"id": "message-1", "content": [{"type": "text", "text": "Working done: " + prompt}]})
time.sleep(0.08)
emit("result", subtype="success", is_error=False, result="Working done: " + prompt, usage={"input_tokens": 1, "output_tokens": 2})
