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
# EIVOH_STT=soniox
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
these flags are rejected here, including `--flag=value` forms. An explicit task
effort overrides every configured `--effort`/`--effort=value` for that launch;
`/effort default` leaves the configured effort in effect.
Claude Code plugins enabled globally also load in the executor. To disable a
plugin, pass a settings override, for example with this shell command:

```sh
export EIVOH_CLAUDE_ARGS="--settings '{\"enabledPlugins\":{\"example-plugin@example-marketplace\":false}}'"
```

For the systemd environment file, use the same quoted value without `export`.

Codex is the second executor (installed `codex` CLI with ChatGPT login). A task
initially uses the executor chosen by `/new codex` or `/new claude`; plain
`/new` inherits the current task’s executor, model and effort. An explicit executor
also inherits model and effort if it matches the current task’s executor; a
different executor uses its defaults. With no current task, the default executor
is `EIVOH_DEFAULT_EXECUTOR` (default `claude`). Each
turn runs a local `codex app-server --listen stdio://` subprocess in
`EIVOH_WORKDIR`. Eivoh starts or resumes its native thread, streams answer deltas,
and injects clarifications through `turn/steer`. Existing threads created by
`codex exec` are resumed without resetting them. Each run has its own supervised
process; no remote server is involved. The Codex sandbox is bypassed like
Claude's default permission mode. Inherited `OPENAI_*` and `CODEX_API_KEY`
variables are removed so the subscription login is used. `EIVOH_CODEX_ARGS`
(shlex-split, for example `-c model_reasoning_effort="high"`) supplies app-server
options: `-c/--config key=value`, `--enable feature`, `--disable feature`, and
`--strict-config`. Adapter-owned subcommands, transport and model flags are rejected.
When upgrading from the exec adapter, migrate exec-only options such as `-p`,
`--add-dir`, `--search` or `--ignore-user-config` to supported configuration
overrides; unsupported options now fail clearly at configuration loading.
Set model defaults with `EIVOH_CLAUDE_MODEL` or `EIVOH_CODEX_MODEL`.
Codex text is grouped by message ID; all completed final-answer parts are retained
once, in order, while commentary is omitted from the stored final answer.
The isolated title generator continues to use the one-shot `codex exec` runner.

Claude ordinary tasks keep stream-json stdin open and correlate replayed user
messages by stable UUID. The one-shot Claude runner remains available for the
isolated title generator. If a submitted clarification crosses a result boundary,
the combined answer retains the complete answer before it and the follow-up.
Complete answer parts are journaled separately from partial streaming text, so a
restart between parts preserves them in the displayed answer and later history.

`/model` shows the target task’s settings as `claude / opus · high` (or
`· default` when no effort is selected), followed by a numbered catalog grouped by
configured executor. `default` model means the executor’s configured default.
Buttons open each executor’s model list and the effort menu. `/model <executor>`
opens eight models per page; `/model <executor> page <N>` navigates and
`/model <executor> <model>` selects within that executor. Each tap sends a new
message. Model commands exceeding Telegram’s 64-byte callback limit are shown
as numbered text choices. Use `/model <name or number>`, for example `/model claude-opus-5-5`
or `/model astra`. Names match case-insensitively: an exact ID or alias takes
priority over a unique substring; ambiguous and unknown names list candidates.
Numbers refer to the last catalog shown in that chat; list again after a restart.
Claude's built-in catalog contains Fable, Opus, Sonnet and Haiku aliases and the
versioned IDs above. `EIVOH_CLAUDE_MODELS` replaces that list with comma-separated
IDs or aliases. Codex's catalog is read on demand from
`$CODEX_HOME/models_cache.json` (default `~/.codex/models_cache.json`), using only
entries with `visibility: "list"`; an unreadable cache gives an empty list.

`/effort` shows the current reasoning effort and buttons for supported levels plus
`default`. `/effort <level>` selects one; `/effort default` clears the task override.
Codex receives the override on each turn; without it, the executor/model default
applies, including `model_reasoning_effort` in `EIVOH_CODEX_ARGS`. The isolated
Codex title generator keeps its fixed low effort.
Claude supports `low`, `medium`, `high`, `xhigh`, `max`. Codex levels come from the
selected model’s `supported_reasoning_levels` in its local cache; the default model
or a missing cache entry offers `low`, `medium`, `high`, `xhigh`. Unsupported values
are refused. Changing model resets an incompatible effort to default and says so.
Model/effort confirmations, new-task and switch replies include buttons to reopen
both menus. Every settings button carries an explicit task target, for example
`/model #12`, `/model #12 codex page 2`, `/model #12 codex gpt-6.1-sol`, or
`/effort #12 high`. These forms also work as typed commands. The task must belong
to the same conversation; unknown or foreign IDs are refused without falling back
to the current task. This binding works even when Telegram posted a menu but its
send acknowledgement and reply mapping were lost. Settings for a closed task can
change without reopening it. Callback commands do not use the clicked message
as a reply target; old settings buttons without an explicit target are refused.

Menus opened without a task use `#new` (also accepted in typed commands). Browsing
these menus creates nothing; selecting always creates a fresh task with executor
defaults plus the selection, even if another task has become current meanwhile.
A later tap on that original taskless menu creates another fresh task; redelivery
of the same callback is still deduplicated. Model choices too long for a callback
retain numbered text and an explicit command example, including the task target.

Model and effort selection are stored per task and apply to turns that have not
been claimed yet. A running or recovering turn keeps its recorded executor/model/effort.
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
model or effort selection creates one.

Chat commands:

* `/new [claude|codex] [title…]` creates a task and makes it current. The previous
  task stays open and keeps running. Settings inherit as described above; an
  unrecognized first word is part of the title. There is no model argument to
  `/new`; use `/model` afterward so model names remain valid title text.
* `/tasks [page]` lists open tasks, most recently active first, eight per page.
  Each entry shows its number, title, executor/model/effort, running/queued/idle state,
  time since last activity and lifespan from creation to last activity (for example
  `over 3h`). Lifespan includes idle gaps; it is not accumulated executor runtime.
  Ages and lifespans use UTC, rounded down to minutes, hours or days; a lifespan
  under one minute is omitted. Activity means a message, task switch or turn ending.
  Buttons switch tasks or change pages; the list reports
  how many other open tasks exist. `/tasks all [page]` includes closed tasks.
* `/task N` switches to task N, reopening it if closed, and shows short excerpts
  of its last user message and stored answer, with its own stored executor/model/effort.
  `/task` lists tasks.
* `/stop [N]` cancels running and queued work for the target task, keeping it open.
  `/stop all` cancels work in every task of this conversation.
* `/close [N]` stops and closes the target task. If it was current, the most recently
  active open task becomes current, or none if there are no open tasks.
* `/title <text>` renames the target task. Explicit names from `/new` and `/title`
  are preserved. Otherwise its first message supplies a temporary title: voice
  prefix removed, whitespace collapsed, shortened to 40 characters.
* `/model [name or number]` shows or changes the target task's model as described above.
* `/effort [level or default]` shows or changes the target task's reasoning effort.
* `/q <text>` queues a message after the running turn and earlier queued messages.

For `/stop`, `/close`, `/model`, `/effort` and `/title`, an explicit task number (where the
syntax allows one) takes priority, then a replied-to task, then the current task
for typed commands. `/model #N …` and `/effort #N …` explicitly target task N;
a bare number in `/model` selects a model, not a task. Unknown task numbers and tasks
from another conversation are refused. Tasks are never automatically closed.
Telegram registers new, tasks, model, effort, stop, q, close and title in its command menu;
inline button commands use the same validation and durable deduplication as typing.

After a new task's first successful text answer, a background request generates
a 3–6 word title in the first message's language. It uses that first user message
(up to 4,000 characters) and the beginning of the final answer (up to 2,000).
Claude turns use `haiku`; Codex turns use `gpt-6-luna`, independent of the task's
selected model. The answer is delivered without waiting for naming. A concurrent
`/title` always wins. Naming shares the executor concurrency limit and has a
30-second deadline including its slot wait. It is cancelled by `/stop`, `/close`
and shutdown. Failed, invalid, timed-out or interrupted attempts keep the temporary
title and are not automatically retried, including after reopening or restarting.
Existing tasks retain their titles when upgrading.

Naming uses separate ephemeral CLI sessions in an empty temporary directory,
with tools and integrations disabled, and never resumes or changes task sessions.
The outcome and requested naming model are journaled as metadata on the successful
turn, with distinct `title_executor` and `title_model` fields. The last reported
usage snapshot is retained as `title_usage` if the CLI supplies one, including on
failure or cancellation; absent usage stays unknown. Reported provider cost does
not establish subscription spend. Title output does not enter the turn's answer,
transcript or ordinary executor events.
The CLI flags were checked on Claude Code 2.1.323 and codex-cli 0.167.2; unsupported
flags in other versions cause a quiet fallback to the temporary title.

Answers and task-specific notices get a `#tN Title` label when more than one task
is open or the answer belongs to a task other than the current one. Labels are
for display only and are excluded from stored results and transcripts.

Migration, routing, parallel limits, reply targets, task controls, per-executor
sessions and restart recovery have offline fake-executor coverage. Live use of
the parallel task interface and session switching changes is unverified.

Only the numeric owner in a private chat is admitted. Use a dedicated bot token.
Before opening the database or contacting Telegram, the process takes an exclusive
Linux file lock keyed by a hash of the token in `/tmp/eivoh-pollers-<uid>/`.
The directory and lock file are private to the Unix user; the lock inode is never
unlinked. Separate state directories do not bypass this lock, and process exit
releases it. This protects one Unix user on one machine, not other machines or
users. Polling conflicts with a remote consumer retry after 10, 20, 30 and 45
seconds; five conflicts within five minutes stop with exit status 78. A single
successful poll does not reset that budget. The supplied unit excludes status 78
from automatic restarts: stop the competing consumer, then start Eivoh again.

Telegram photos, documents, voice and audio are admitted to SQLite before any
download or transcription. Captions are content, including captions starting with
`/stop`, `/new` or `/q`. Files without captions are requests too. Each album item
is an ordered message with its album identifier retained; it is not combined into
one synthetic turn. Task and reply routing are fixed at admission. Two background
preparations may run at once, with a 180-second deadline; a pending file is a FIFO
barrier for later input to that task. Commands remain usable during preparation.
As with queued text, `/model` can change the executor selected before launch;
prepared input can steer an eligible running turn without changing that turn's
output directory. If the selected model or effort differs from the still-running
turn when preparation completes, a durable queue notice is issued once; it is
omitted if the turn has ended or the settings match again. `/stop` cancels preparation and prevents a late result from
starting work. Restart uses the saved descriptors and existing completed downloads.

Rich messages flatten to plain text in document order, with numbered placeholders
at each media position and the same attachment manifest and limits. Nested media,
captions and credits are retained. Rich messages with media treat all their text
as content; text-only rich messages use exactly the ordinary text command rules
(including the first slash token). Only fully flattened text/structure blocks and
the supported media blocks are accepted; unsupported rich blocks fail visibly,
including maps, button rows and unknown/future block types, even when nested.
They retain named failure descriptors, so a caption such as `/stop` remains content.
Unknown inline RichText types keep their plain `text` when present; inline types
without a text representation also fail visibly. This replaces the earlier
skip-unknown-block behavior. Malformed content and rich parse failures use the
same durable unsupported-message notice; recognized media blocks remain content
even when their descriptors are broken. Rich traversal allows 128 JSON levels
(with margin over the API's 16 block levels) and 4096 blocks, with no node cap;
validation uses an iterative traversal. python-telegram-bot 22.8 supports
Bot API 10.0, while rich messages arrived in Bot API 10.1+: the channel reads
`api_kwargs['rich_message']` and also accepts a future `message.rich_message` object
through `to_dict()`. This fix is verified offline against the documented schema;
a real rich message in Telegram and the Android client's exact block sequence
remain unverified.

Downloads are streamed with a hard 20 MB byte limit, a 90-second download deadline,
and at most ten files per message. Files live under `EIVOH_STATE_DIR/files/inputs/`,
outside the public checkout; keep `EIVOH_STATE_DIR` in private storage. Original
filenames are display metadata, never destination paths. Generated local names,
atomic publication and symlink checks protect stored paths. Claude and Codex get
the same JSON path manifest with instructions to inspect files/images using their
native tools. Prepared input paths and selected output snapshots are retained in
the handoff transcript, subject to its existing length limit. Prior outputs are
labelled as files for reading, without claiming Telegram received them; revisions
belong in the current turn's output directory. An unsupported format is still
available as a file; archives are not automatically extracted. Failed preparation
is reported and does not launch the executor. Voice/audio use `EIVOH_STT=soniox`
with the provider's `SONIOX_API_KEY`; the stored transcript is echoed to the chat.
Transcription after a crash before its result was committed can repeat.

Each launched turn gets a private output directory at
`EIVOH_STATE_DIR/files/outputs/<turn-id>/`. To send a result as an actual attachment,
the executor writes a file there and adds one exact line per artifact to its final
answer:

```text
EIVOH_ARTIFACT {"path":"report.txt","kind":"document"}
EIVOH_ARTIFACT {"path":"figure.png","kind":"photo"}
```

Paths must be relative to that turn's directory. Only final, unfenced marker lines
are interpreted; protocol lines and partial streamed prefixes are hidden from the
chat, while raw executor events retain the evidence. At most ten regular files,
50 MB each and 100 MB total, are selected. Traversal, symlinks and hardlinks are
rejected. Files are copied into durable snapshots with hashes before the turn is
marked complete. Changing an original afterward does not change a queued upload.
JPEG/PNG files up to 10 MB use Telegram photos; other files and definitively refused
photo formats/dimensions fall back to documents. Each delivered attachment maps
back to its task for reply routing. A filesystem path by itself, a file elsewhere,
or an unselected file in the output directory is never uploaded automatically.
Unsupported Telegram media types, including video/sticker messages, receive an
explicit notice; their captions stay in the inbound journal. Send them as documents
to make their bytes available to the executor. Other unsupported owner content
(such as locations, polls, contacts or unknown API fields) receives a durable
failure notice naming the content fields. An explicit list of installed PTB
service fields keeps chat administration, payment/gift and state-change events
silent; invoices, shared users/chats and Web App data are user content and receive
notices. Unhandled owner updates, including silent service drops, log their update
ID and non-empty field/API key names at WARNING, without content values.
Snapshot copying, upload hashing and voice-file reads run off the event loop. Cancellation may leave a bounded local
copy/read finishing in the background, but it cannot publish a stopped task's output.
Telegram's cloud limits are documented in its [file API](https://core.telegram.org/bots/api#getfile),
[photo API](https://core.telegram.org/bots/api#sendphoto) and
[document API](https://core.telegram.org/bots/api#senddocument).
Files and delivery journals currently remain in private state; retention and cleanup
are separate decisions. Restart backups must include both SQLite and `files/`.

Offline transport tests cover these limits, routing, cancellation and delivery
windows. Isolated real Claude and Codex Gateway probes inspected a synthetic image
and document, accepted a file clarification during a running turn, produced file
outputs, and recovered completed delivery without new executor calls. Telegram
network calls were mocked in those probes; real Telegram file transfer remains
unverified.

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
notice, goes to that task and reopens it if closed. Only the commands above
are commands: other text starting with `/` (a path, an executor's own command) is
an ordinary message. Unknown reply targets fall
back to the current task. With neither a known target nor a current task, a new
current task is created. Reply routing itself does not switch the current task.
Ordinary messages to a running task add clarifications to that run when its
executor/model/effort still matches the task's selection. The chat confirms only after
the executor acknowledges the input before the reader sees the turn end; this
confirms receipt, not completion of its instructions. A late acknowledgement stays
uncertain and cannot attach the input to an already finished answer.
Claude may acknowledge only after its current tool finishes. Acceptance is shown
by a 👀 reaction on the owner's message; ordinary queueing uses ✍. Text notices
remain for unconfirmed delivery and queueing due to a changed executor/model/effort.
Channels without reactions stay silent; reactions are best effort and are not
replayed after a restart. This reaction behaviour is unverified in live Telegram.
`/q <text>` keeps a message for the next turn. Later messages cannot jump an
earlier queued input, and messages after a model or effort change use the next turn.
Messages arriving as a run ends safely stay queued. Commands themselves never
steer. `/stop` remains the hard interrupt.

Each task has its own FIFO queue and worker. Different tasks run concurrently,
bounded globally by `EIVOH_MAX_PARALLEL` (default 3, integer at least 1); turns
waiting for a slot stay queued. Commands run immediately in arrival order under
the conversation lock and never wait behind executor work. `/q` journals its
text as a queued turn; other commands create no turns. Steering acknowledgements
and their reactions or exceptional notices run separately from polling and
executor-slot accounting.
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
retained but no longer read or updated.
Migration 4 adds title provenance and a durable one-attempt marker. Because old
titles lack provenance, all existing tasks are treated as manually named.
Migration 5 adds explicit queue intent and steering delivery records linking each
input to its parent turn. Raw acknowledgement and accepted state commit together.
Accepted inputs join the parent transcript with one final answer and are never
launched as separate turns. Sessions and title provenance remain intact.
Migration 6 adds media descriptors/preparation, update receipts and an expiring
poll cursor, final output manifests, delivery parts and request attempts. Existing
known Telegram message IDs for interrupted turns seed the new delivery parts in
numeric order, so recovery can edit them. It preserves task/session/title data.
Completed legacy turns are not replayed; ambiguous legacy sends without a recorded
ID cannot be reconstructed. Restoring an older database does not by itself restore
or remove files.
Migration 7 adds numeric message-ID expression indexes to inbound and delivery
rows for displacement lookups; journal contents and delivery identities stay
unchanged. Migration 8 adds nullable task and turn effort, preserving existing data
and default behavior. Effort is fixed with the turn’s model at claim time and
retained through recovery and accepted steering inputs.
Older releases reject newer schema versions. A rollback needs a
compatible release or a separately planned database restore.
Conversations point to one current task among their open tasks; turns retain
observed session IDs and raw events. Launch metadata separates
the requested model/effort and their sources from a configured-model hint (Codex config)
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
executor/model/effort and that executor's task session, with the original request and a
reminder to check existing work when resuming. The assembled prompt, including
any missed transcript, is journaled before launch and reused on recovery: a session
ID alone does not acknowledge receipt of the first prompt. Legacy pending turns
without prompt metadata reconstruct it using the session's transcript progress.
Real isolated Gateway probes for both adapters covered steering, queue order,
native resume, cleanup on stop, and recovery after durable input acceptance.
These used a local channel. Codex steering was also observed once end to end in
Telegram with three clarifications retained in one answer; Claude Telegram steering
remains unverified.

Inputs with an uncertain delivery outcome remain queued with a visible notice;
the next prompt explicitly asks the executor to check whether they were already
handled. They may have been applied before the crash: exactly-once external
effects cannot be guaranteed across an unacknowledged write. Accepted inputs
remain associated with their parent and are retained in its recovery prompt.
Three automatic recovery attempts are allowed
per turn before visible failure. Errors/results emitted after shutdown begins
remain in the raw journal but cannot finalize the interrupted turn on replay.
External tool effects are not exactly-once: an action may precede its persisted
result. Desired text chunks and attachments have durable logical identities. Each
physical request records its payload and revision before the network call; a
confirmed message ID and its task mapping commit before the next part is sent.
Recovery edits known IDs and sends only known-unsent parts. A newer final answer
cannot be acknowledged by an older in-flight progress update. A fresh send that
times out or loses its acknowledgement becomes `unknown`, including after a crash;
it is never automatically reposted. Confirmed connection establishment or pool
failures are known-unsent and remain retryable. Progress and final replies have
separate stable identities. Final delivery adopts confirmed progress message IDs;
if an uncertain preview contains only different progress content, a new final send
is allowed. Actual attempted visible text is checked across all uncertain preview
parts, including plaintext fallbacks and tool suffixes. A preview that may already
contain the final answer preserves that uncertainty, and an uncertain final never
gets another identity after restart. Final identity creation and adoption commit
together. At most once per flush, updated previews and finals move their known
text parts below newer known messages (ignoring retiring tails and, for previews
only, other running turns' text previews); arrivals during the replacement batch
wait for another update, so bottom placement is not unconditional. Moves journal
deletes before replacement sends, preserve/report ambiguous sends and, on
permanent delete refusal, retain the old text ID for editing or replace obsolete
content with a superseded marker (offline coverage; live behavior unverified).
Later text chunks are held behind an uncertain prefix. File parts retain their
own identities. A separate notice explains an
uncertain/refused/pending delivery; the result and snapshots stay in storage.
This can omit a part that Telegram never received, but avoids blind retries of
possibly posted sends. It is not exactly-once delivery. A message whose ID was
lost cannot have a known reply mapping. In the rare combination of a long answer,
an uncertain preview chunk and a changing task label, text can reflow across chunk
boundaries and a final chunk can repeat overlapping preview content. Definite
markup/photo refusals and flood limits can be retried; known-message edits/deletes
are safe to retry. Final output
is persisted before terminal turn state, so a completed turn awaiting delivery
does not require a new executor call. `/stop` also suppresses its task's unfinished
delivery across restart.
Pending final delivery retries in the background after 1, 2, 4, 8 and 16 seconds,
within a five-minute overall deadline; uploads use write timeouts scaled by size.
After these bounded retries are exhausted, unsent output remains pending until
restart; successful polling or a later network recovery does not restart delivery.
Transient pending delivery produces no immediate error notice. Commands persist
their reply intent under the conversation lock and flush it in tracked background
jobs, so an upload or flood wait cannot block admission of the next `/stop`.
Notice identities use the inbound ID and purpose, independent of changing labels
or task ages. Pending command replies also resume after restart without executing
the command again.
Polling commits accepted update receipts before advancing its cursor. Duplicate
owner messages are removed by their durable channel/chat/message identity, before
media preparation. Cursor/receipt hints expire after six idle days, including during
an uninterrupted process, because Telegram can randomize update IDs after a week.
Polling honors flood-control delays without advancing its offset. The existing
progress request gets up to 60 seconds to acknowledge before final delivery adopts
its known IDs; `/stop` and shutdown cancel both progress and final delivery.
Ambiguous final sends are not retried. Interactive approvals are not implemented;
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
