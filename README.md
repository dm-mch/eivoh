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

# Operating the gateway

Linux and a separately installed Claude Code with subscription login are required.
The gateway is built. Parallel tasks and the new task interface are verified offline;
live use of these changes is unverified.
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
# EIVOH_MAX_PARALLEL=3
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

Model selection is stored per task and applies immediately to turns that have not
been claimed yet. A running or recovering turn keeps its recorded executor/model.
Each task keeps a native session separately for Claude and Codex: switching back
resumes that executor's original session. Eivoh passes only the earlier turns
that executor has missed since its last finished turn (the whole bounded history
for a new session). The transcript contains user texts and stored final answers,
oldest first, capped at 30,000 characters including truncation markers. It prefers
recent whole turns; if the newest turn cannot fit, it keeps the beginning and end
of both user and assistant text with role labels and omission markers. This
transfers conversation text, not native tool state. Turns without a stored final
answer are marked as such. The switch reply reports the currently included turn
count; zero is shown as “Context kept (same session)”. With no current task,
model selection creates one.

Chat commands:

* `/new [claude|codex] [title…]` creates a task and makes it current. The previous
  task stays open and keeps running. An unrecognized first word is part of the title.
* `/tasks [page]` lists open tasks, most recently active first, eight per page.
  Each entry shows its number, title, executor/model, running/queued/idle state
  and time since activity. Buttons switch tasks or change pages; the list reports
  how many other open tasks exist. `/tasks all [page]` includes closed tasks.
* `/task N` switches to task N, reopening it if closed, and shows short excerpts
  of its last user message and stored answer. `/task` lists tasks.
* `/stop [N]` cancels running and queued work for the target task, keeping it open.
  `/stop all` cancels work in every task of this conversation.
* `/close [N]` stops and closes the target task. If it was current, the most recently
  active open task becomes current, or none if there are no open tasks.
* `/title <text>` renames the target task. Otherwise its first message supplies a
  title: voice prefix removed, whitespace collapsed, shortened to 40 characters.
* `/model [name or number]` shows or changes the target task's model as described above.

For `/stop`, `/close`, `/model` and `/title`, an explicit task number (where the
syntax allows one) takes priority, then a replied-to task, then the current task.
The number in `/model` selects a model, not a task. Unknown task numbers and tasks
from another conversation are refused. Tasks are never automatically closed.
Telegram registers new, tasks, model, stop, close and title in its command menu;
inline button commands use the same validation and durable deduplication as typing.
Answers and task-specific notices get a `#tN Title` label when more than one task
is open or the answer belongs to a task other than the current one. Labels are
for display only and are excluded from stored results and transcripts.

Migration, routing, parallel limits, reply targets, task controls, per-executor
sessions and restart recovery have offline fake-executor coverage. Live use of
the parallel task interface and session switching changes is unverified.

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

Messages become turns bound to a task when journaled; that binding never changes.
A reply to a known owner message, or to any part of an answer or task-specific
notice, goes to that task and reopens it if closed. Only the seven commands above
are commands: other text starting with `/` (a path, an executor's own command) is
an ordinary message. Unknown reply targets fall
back to the current task. With neither a known target nor a current task, a new
current task is created. Reply routing itself does not switch the current task.
Each task has its own FIFO queue and worker. Different tasks run concurrently,
bounded globally by `EIVOH_MAX_PARALLEL` (default 3, integer at least 1); turns
waiting for a slot stay queued. Commands run immediately in arrival order under
the conversation lock, create no turns, and never wait behind executor work.
Cancellation is committed before terminating the target task's process tree.
Cancelled messages remain in the journal.

Known limitation: parallel tasks share one working tree (`EIVOH_WORKDIR`) and can
edit the same files. Task isolation, such as a worktree per task, is not built.

SQLite (`eivoh.sqlite3` in the state directory) uses WAL and synchronous FULL for
durable inbound commits before processing and polling acknowledgment. Schema
changes use ordered transactional migrations and `PRAGMA user_version`; migration
1 assumes a new database, with no upgrade from the unreleased prototype layout.
Migration 2 upgrades version 1 in place, adding a nullable task model.
Migration 3 adds task titles/activity timestamps, turn executor/model snapshots,
reply targets, a per-(task, executor) session table with transcript progress, and
a map from bot message IDs (every part of a split answer) to their task and turn
for reply routing. It backfills activity, launch settings and one session for each
executor a task has used, where derivable. Version 2 bound queued messages to a
task only at dispatch, so upgrade when no `/new` is waiting in the queue: a message
queued behind it would stay in the earlier task. The old task session column is
retained but no longer read or updated. This does not make the previous release
compatible: its schema-version guard rejects version 3. A rollback needs a
compatible release or a separately planned database restore.
Conversations point to one current task among their open tasks; turns retain
observed session IDs and raw events. Launch metadata separates
the requested model and its source from a configured-model hint (Codex config)
and the observed model, which is unknown at launch. Claude's init and main assistant
events retain model observations with their respective sources; an init report
does not prove which model generated a response. Model IDs contain no source labels.
Raw stdout
and stderr lines are append-only; invalid UTF-8 uses replacement characters.
SIGTERM marks shutdown before polling cleanup. `KillMode=mixed` lets the gateway
stop executors before systemd's final cgroup kill. Each executor run has a small
Linux subreaper supervisor, which cleans up its own descendants on cancellation
and exit, including double-forked pipe holders. The gateway is not a subreaper.
Killing the supervisor itself with SIGKILL, uninterruptible processes, and work
launched through external services are outside this cleanup guarantee; systemd
provides an additional containment layer. Interrupted turns remain recoverable.

Restart recovery first applies journaled but unfinished commands in ID order,
then starts per-task workers. It finalizes a stored result and delivers its answer
without launching the executor again. Otherwise it uses the turn's recorded
executor/model and that executor's task session, with the original request and a
reminder to check existing work when resuming. The assembled prompt, including
any missed transcript, is journaled before launch and reused on recovery: a session
ID alone does not acknowledge receipt of the first prompt. Legacy pending turns
without prompt metadata reconstruct it using the session's transcript progress.
Three automatic recovery attempts are allowed
per turn before visible failure. Errors/results emitted after shutdown begins
remain in the raw journal but cannot finalize the interrupted turn on replay.
External tool effects are not exactly-once: an action may precede its persisted
result. The first ID of an answer or task-specific notice is durable for reply routing;
split-message edit state and delivery acknowledgments remain ephemeral, so recovery
can still post a new answer. A crash between sending and storing an outbound ID
can leave that message without a reply mapping.
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
