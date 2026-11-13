# Eivoh

Evolving Identity, Values, Ontology & Humanity. A general-purpose personal AI agent that develops through experience and reflection: it refines its knowledge and changes its ways of working, its skills and its own code.

# Current state

The current stage and what already works are described in [ROADMAP.md](ROADMAP.md). The other documents describe the target system.

Bootstrap: the current version of the instructions and the accumulated experience are used to create the next versions of Eivoh.

# Documents

* [VISION.md](VISION.md): purpose, long-term direction and guiding principles.
* [ARCHITECTURE.md](ARCHITECTURE.md): target components, their connections and open technical decisions.
* [AGENTS.md](AGENTS.md): instructions for the agent working on the project now.
* [EVOLUTION.md](EVOLUTION.md): reflection and changes to memory, strategies, skills and code.
* [EXPERIENCE.md](EXPERIENCE.md): raw records, knowledge, current state and search.
* [ROADMAP.md](ROADMAP.md): development stages, the current stage and the expected result of each stage.
* private/: a separate private repository that the core ignores. Each Eivoh instance creates it locally: private/experience/ holds experience records and their index, private/archive/ holds archives, which are neither instructions nor agreed decisions.

# Getting started

1. Prepare a working copy of the Markdown files and open it in Codex or Claude. Google Docs are a place for collaborative editing, not the agent's runtime.
2. Follow AGENTS.md: read the documents it names, check the actual state of the project and set a specific task.
3. Make the smallest useful change and save the result, the reasons and the limitations. Put the experience record in private/experience/.

# Development direction

The target system runs continuously on a VM, supports interchangeable communication channels and native CLI executors, and keeps its own tasks and memory. Telegram will probably become the main channel, but it does not define how the system is built. The V1 stack is Python 3.12, stdlib SQLite, asyncio and python-telegram-bot 22.x.

# Private and public

The public core contains the shared documents and code. The private/ folder is the private part, kept in a separate repository: experience, correspondence, archives and state. The agent makes core changes on the working branch `work`; they reach `main` through a PR. The rules for changes and publication are in [AGENTS.md](AGENTS.md).

# Operating V1

Linux and a separately installed Claude Code with subscription login are required.
The gateway is built; live integration and real-use acceptance remain unverified.
From `~/src/eivoh`:

```sh
uv venv --python 3.12
uv pip install -e '.[dev]'
.venv/bin/pytest -q
install -d -m 700 ~/.config/eivoh
touch ~/.config/eivoh/env
chmod 600 ~/.config/eivoh/env
```

Edit `~/.config/eivoh/env` with a dedicated bot token and your numeric Telegram
user ID. Never commit this file:

```ini
EIVOH_TELEGRAM_TOKEN=replace-with-your-bot-token
EIVOH_OWNER_ID=123456789
EIVOH_WORKDIR=/home/your-user/src/eivoh
# Optional overrides:
# EIVOH_STATE_DIR=/home/your-user/.local/state/eivoh
# EIVOH_CLAUDE_MODEL=haiku
# EIVOH_CLAUDE_MODELS=fable,opus,sonnet,haiku,claude-fable-5-2,claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5
# EIVOH_CLAUDE_PERMISSION_MODE=bypassPermissions
# EIVOH_CLAUDE_BIN=claude
# EIVOH_CLAUDE_ARGS=
# EIVOH_DEFAULT_EXECUTOR=claude
# EIVOH_CODEX_BIN=codex
# EIVOH_CODEX_MODEL=
# EIVOH_CODEX_ARGS=
```

Set `EIVOH_WORKDIR` explicitly to the working tree the executor should edit,
normally `~/src/eivoh` (use an absolute path in the environment file). Defaults:
state in `~/.local/state/eivoh`, no model override, permission mode
`bypassPermissions`, binary `claude`.
The default permission mode allows tools without interactive approval; other
modes' headless behavior is unverified. Eivoh removes inherited `CLAUDE*` and
`ANTHROPIC*` variables and does not use bare mode or supply an API key.

`EIVOH_CLAUDE_ARGS` is empty by default. It is split with Python's `shlex.split`
(shell-style quoting, without shell expansion) and appended after all built-in
Claude arguments on both start and resume. The adapter owns `--resume`,
`-p`/`--print`, `--output-format`, `--input-format`, `--permission-mode` and `--model`;
these flags are rejected here, including `--flag=value` forms.
Claude Code plugins enabled globally also load in the executor. To disable a
plugin, pass a settings override, for example with this shell command:

```sh
export EIVOH_CLAUDE_ARGS="--settings '{\"enabledPlugins\":{\"example-plugin@example-marketplace\":false}}'"
```

For the systemd environment file, use the same quoted value without `export`.

Codex is the second executor (installed `codex` CLI with ChatGPT login). A task
initially uses the executor chosen by `/new codex` or `/new claude`; plain
`/new` and the first task use `EIVOH_DEFAULT_EXECUTOR` (default `claude`). Each
turn runs `codex exec --json --dangerously-bypass-approvals-and-sandbox ... -` (or
`codex exec ... resume <thread_id> -`; all options precede `resume`) in `EIVOH_WORKDIR`, with the prompt on
stdin. The Codex sandbox is bypassed like Claude's default permission mode; on
hosts that forbid unprivileged user namespaces its bubblewrap sandbox cannot
start anyway. Inherited `OPENAI_*` and `CODEX_API_KEY` variables are removed so
the subscription login is used. `EIVOH_CODEX_ARGS` (shlex-split, for example
`-c model_reasoning_effort="high"`) is appended on start and resume; `exec`,
`resume`, `--json`, `-C`/`--cd` and `-m`/`--model` are rejected. Set model defaults
with `EIVOH_CLAUDE_MODEL` or `EIVOH_CODEX_MODEL`, so extra arguments cannot override
the task's model selection. Codex sends whole messages, not
text deltas, so its answer grows message by message; the final answer is its
last message.

`/model` shows the active task's executor and selected model (`default` means
the executor's configured default), followed by a numbered catalog grouped by
executor. Use `/model <name or number>`, for example `/model claude-opus-5-5`
or `/model astra`. Names match case-insensitively: an exact ID or alias takes
priority over a unique substring; ambiguous and unknown names list candidates.
Numbers refer to the last catalog shown in that chat; list again after a restart.
Claude's built-in catalog contains Fable, Opus, Sonnet and Haiku aliases and the
versioned IDs above. `EIVOH_CLAUDE_MODELS` replaces that list with comma-separated
IDs or aliases. Codex's catalog is read on demand from
`$CODEX_HOME/models_cache.json` (default `~/.codex/models_cache.json`), using only
entries with `visibility: "list"`; an unreadable cache gives an empty list.

Model selection is stored per task. Within the same executor it keeps the native
session; switching between Claude and Codex clears the session and starts a new
one on the next message. Eivoh passes earlier user texts and stored final answers
as a transcript, oldest first, capped at 30,000 characters by omitting oldest
whole turns and marking truncation. This transfers conversation text, not native
tool/session state; turns without a stored final answer are marked as such.
The switch reply reports the number of included turns. With no active task,
selection creates one. Telegram's `/new`, `/model` and `/stop` menu is registered
once at polling startup, best-effort. Migration, model selection, CLI arguments,
session reuse, transcript handoff, recovery and menu registration are tested
offline with fakes; live use of `/model` remains unverified.

Only the numeric owner in a private chat is admitted. Use one instance and a
dedicated token; multi-instance locking belongs to V2.

The service uses a separate detached release worktree at
`~/.local/share/eivoh/run`, with its own venv. Editing the executor's working tree
does not edit that release. Release a commit containing the implementation before
installing the systemd **user** service (no sudo):

```sh
./deploy/release.sh <commit>
mkdir -p ~/.config/systemd/user
cp deploy/eivoh.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now eivoh.service
systemctl --user stop eivoh.service
systemctl --user start eivoh.service
systemctl --user restart eivoh.service
journalctl --user -u eivoh.service -f
```

`deploy/release.sh <commit>` creates or updates the detached worktree, prints
its previous commit, synchronizes dependencies with uv, and runs the non-live
tests. It refuses a dirty release tree. It restarts the service only when given
`--restart`, after tests pass. For a code rollback, release the printed previous
commit: `./deploy/release.sh <previous-commit> --restart`. The state directory is
untouched; database rollback requires a separate backup/restore decision. A
failed release leaves the candidate checkout for inspection and does not restart
the service; release the previous commit before any manual restart. Dependency
resolution requires package availability and is not locked across releases.

The unit includes `~/.local/bin` in PATH. Persistence across logout depends on
the host's user-manager configuration. Release orchestration is tested with
command stubs; actual worktree creation, uv installation and systemd operation
have not been exercised by this review-fix task.

Messages are turns within the active task and continue its executor session in
FIFO order. `/model` takes its place in that queue without creating a turn and
applies to subsequent messages. `/new` closes the previous task in FIFO order and
creates a new active task with no session. Queued turns bind to the active task
at dispatch. `/stop` bypasses the queue, cancels the running turn and all earlier
queued messages (including `/new` and `/model`), and
reports the task IDs and queued count. Cancelled messages remain in the journal.

SQLite (`eivoh.sqlite3` in the state directory) uses WAL and synchronous FULL for
durable inbound commits before processing and polling acknowledgment. Schema
changes use ordered transactional migrations and `PRAGMA user_version`; migration
1 assumes a new database, with no upgrade from the unreleased prototype layout.
Migration 2 upgrades version 1 in place, adding a nullable task model.
Conversations point to active tasks; tasks own executor/model choice and session ID;
turns retain their observed session ID and raw events. Raw stdout
and stderr lines are append-only; invalid UTF-8 uses replacement characters.
SIGTERM marks shutdown before polling cleanup. `KillMode=mixed` lets the gateway
stop executors before systemd's final cgroup kill. Each executor run has a small
Linux subreaper supervisor, which cleans up its own descendants on cancellation
and exit, including double-forked pipe holders. The gateway is not a subreaper.
Killing the supervisor itself with SIGKILL, uninterruptible processes, and work
launched through external services are outside this cleanup guarantee; systemd
provides an additional containment layer. Interrupted turns remain recoverable.

Restart recovery finalizes a stored result and delivers its answer without
launching the executor again. Otherwise it resumes the task session with the
original request and a reminder to check existing work, or sends the original
request when no session exists. Three automatic recovery attempts are allowed
per turn before visible failure. Errors/results emitted after shutdown begins
remain in the raw journal but cannot finalize the interrupted turn on replay.
External tool effects are not exactly-once: an action may precede its persisted
result. Outbound Telegram IDs are not durable, so recovery can post a new answer.
Polling honors flood-control delays without advancing its offset. Final answer
delivery waits up to 60 seconds, including flood-control backoff, and is cancelled
by shutdown. Ambiguous send timeouts are not retried. Interactive approvals are not implemented;
result permission denials are surfaced. Markdown supports headings, emphasis,
links and code, with balanced HTML chunks conservatively limited to 4096 UTF-16
units including markup. Other syntax remains readable text.

The ordinary tests use no network or real Claude. The review-fix suite runs on
Python 3.12.3 with PTB 22.8.
To opt into a real Claude `haiku` and Codex `gpt-6.1-sol` turn, resume and
sleep-command cancellation/orphan check through a fake channel (no Telegram):

```sh
EIVOH_LIVE=1 .venv/bin/pytest -q tests/test_live.py
```

No source was copied: nanobot's MIT `nanobot/channels/telegram/runtime.py` was the
studied reference for the independently written chat helpers and stop pattern.
