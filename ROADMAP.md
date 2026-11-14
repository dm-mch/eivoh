# Eivoh development plan

Current stage: V3. V0, V1 and V2 are done (2026-11-13). No dates are set. The architecture describes the target system; this file describes the order in which it appears and is the only place where the current stage is stated.

# How the plan works

* Bootstrap. Each stage is built with what the previous stage provides. The work on the next stage is the stream of real tasks for the current one: there are no separate practice tasks.
* Chat first. The owner works with Eivoh through chat. A command line is acceptable as a starting point and for debugging, not as the main interface.
* Reuse. Before a stage starts, look at the open-source candidates named in it, check the licence and current state, and record the decision: depend, copy and adapt, or use as a reference. The studied projects are tied to their own agent loops, so none of them is taken as a whole. Copied code keeps its attribution.
* The V1 stack is confirmed: Python 3.12, stdlib SQLite, asyncio and python-telegram-bot 22.x as an unmodified dependency. Later additions are decided when needed.
* A stage is done when its "Done when" conditions are observed in real use, not when the code is written. After each stage, review the experience records and refine the next stage.

The reuse notes come from reading the code of the candidates. Nothing was run, so formats and behaviour must be confirmed by a test before they are relied on.

# V0. Markdown core

State: a Markdown foundation, not a finished service. Tasks are carried out by a running Codex or Claude using the project documents. The gateway, continuous operation on a VM, channels, storage and automatic reflection belong to later stages; they are not assumed to exist.

* Keep README, VISION, ARCHITECTURE, AGENTS, EVOLUTION, EXPERIENCE and ROADMAP as the public core; keep experience and archives in the private repository.
* Apply AGENTS.md to every task of building V1 and write an experience record by hand for each substantial task.

Done when: V1 is built by following these documents; the experience records of that work exist; at least one expectation from a change to the documents has been checked against a later task.

Done (2026-11-13): V1 was built by following these documents, with experience records for each substantial task. The expectation that experience records appear without a reminder was observed in the later V1 and V2 tasks.

# V1. Chat with one executor

State: code built and tested with a fake executor on Python 3.12.3; an opt-in live test with the real Claude CLI passed (turn, resume, cancel during a tool call without orphans). Storage separates tasks from turns and starts at schema migration 1. Recovery, shutdown ordering and per-turn subreaper cleanup have offline regression coverage. The installed Claude CLI probe confirmed the JSONL/session-id format, resume after a hard kill, prompt delivery with stdin closed, subscription login with API-key variables removed, and a Bash tool call in headless `bypassPermissions` mode. Native SIGTERM/SIGINT descendant cleanup, interactive approval/error shapes and persistence of the last message if killed during a write remain unverified. Plugins enabled globally in Claude Code are loaded by the executor unless disabled with an `EIVOH_CLAUDE_ARGS` settings override. Observed once on the installed service (2026-11-13): messages from Telegram are answered, consecutive messages continue one executor session, a service restart in the middle of a turn resumed the turn and it completed, and deployment through the release worktree script worked. `/stop` during a tool call cancelled the turn and left no process in the service's control group. Work on V2 has gone through the chat: voice messages were built, deployed and checked from it.

Goal: the owner sends a message in Telegram and a real coding CLI does the task; from this point the work on Eivoh itself goes through this chat.

Build:

* Executor adapter with one interface for all executors: start, resume, stream of events, cancel. Events: session started, text delta, message, tool started, tool finished, approval requested, usage, turn completed, error. Claude Code CLI is connected first; the interface is designed for Codex as well.
* Telegram channel behind a generic channel interface: a separate Eivoh bot, long polling, access for the owner only by numeric user id.
* Chat basics: the answer appears progressively by editing a message, long answers are split, Markdown is converted for Telegram with a plain-text fallback, a typing indicator, `/stop` cancels the running task, `/new` starts a new one.
* State in Eivoh's own storage: conversation with active task, task with executor and session id, per-message turns with raw executor events, and a journal of inbound messages written before processing starts.
* A service on the VM with start, stop and restart, running from a separate release checkout while the executor edits the working tree.

Reuse:

* Claude side: our own small adapter on the standard library (subprocess and JSON lines). Reference for the wire format, session id, resume and shutdown order: claude-agent-sdk-python (MIT). It is not a dependency for now: it brings its own bundled CLI and extra packages.
* Telegram library: python-telegram-bot (LGPL-3.0, used as an unmodified dependency). Both studied Python projects use it.
* Own implementations using nanobot (MIT), `nanobot/channels/telegram/runtime.py`, as a studied reference: message splitting that keeps code blocks intact, Markdown to Telegram HTML, the plain-text fallback, the typing loop, and the pattern where `/stop` bypasses the per-conversation queue.
* Reference: hermes-agent (MIT) for edge cases: flood control and back-off on edits, send errors that must not be retried, the systemd unit; openclaw `extensions/anthropic/cli-backend.ts` (MIT) for the Claude arguments and the list of environment variables to clear.

Confirm by test before relying on it:

* The exact stream format of the installed Claude CLI and where the session id appears.
* That resume still works after a hard kill, and that closing input before terminating does not lose the last message.
* That the CLI uses the subscription login: no bare mode, and API-key variables removed from the environment.
* Headless permission settings that never block waiting for an answer.

Done when: the work on V2 is carried out through the chat; a restart of the service in the middle of a task loses neither the message nor the task; `/stop` ends the task and leaves no orphaned processes; every task has its raw events in storage.

Done (2026-11-13): each condition was observed once on the installed service, as described in the state above; every turn so far has its raw events in storage. The unverified points in the state stay open and are checked when they matter.

# V2. Second executor and a comfortable chat

Goal: both executors behind one interface and a chat the owner prefers to any other way of working with Eivoh.

Build:

* Codex ordinary tasks use a local `codex app-server` per run with native thread start/resume, text deltas and turn steering; `codex exec` remains for isolated title generation. Cancellation combines interrupt with supervised process-tree cleanup. Built: the CLI runner is shared with Claude; `/new codex` and `/new claude` choose the executor of a new task. Per-task model choice through `/model` is built: same-executor changes keep the native session, and each task now retains a separate native session per executor. Switching back resumes that executor's session with a bounded transcript of only the turns it missed; a new session receives the whole bounded history. Oversized newest turns retain marked user/assistant excerpts. The assembled prompt and turn executor/model are journaled before launch so recovery retains the handoff and launch settings even after session creation or a later `/model`; older pending turns reconstruct missing prompt metadata when needed. Launch model settings and their sources are separate from observed Claude model reports. Migration, routing, context handoff and recovery have offline fake-executor coverage. Per-task reasoning effort through `/effort` is stored with turn launch settings and preserved on recovery; `/model` and `/effort` have tap-to-choose menus and consistent settings displays. New tasks inherit the current task’s executor/model/effort unless a different executor is explicitly selected, and task switches retain each task’s settings. These additions are verified offline; a live probe through the gateway with a local channel (2026-11-13) completed one turn at effort `low` on Claude Code and on codex-cli 0.167.2, whose thread settings reported that effort. Menus and settings in live Telegram are unverified. Observed once on the installed service (2026-11-13): `/model` by name and by number, a Claude to Codex switch and a switch back, each continuing the task from the passed transcript, and Codex turns resuming one thread. That observation predates per-executor session retention; the new resume-back behavior and handoff recovery after a restart are unverified in live use. A live test with codex-cli 0.167.2 passed (turn, resume with the same thread, `/stop` during a command without orphans).
* Several tasks in one chat are built: immutable journal-time routing, one FIFO worker per task, parallel execution bounded by `EIVOH_MAX_PARALLEL` (default 3), immediate durable commands, scoped stop/close, and recovery of multiple tasks with their recorded executor/model. `/new` leaves earlier tasks open; `/tasks` lists eight per page, most recently active first, with switching/pagination buttons and an `all` view for closed tasks. `/task`, reply routing to owner/bot messages, titles, contextual switch replies and display-only task labels are built. The list shows compact age and lifespan from creation to last activity, including idle gaps. Typing bypasses delivery locks and flood waits; offline regression coverage includes switching while edits are rate limited. Migration 3 stores titles/activity, per-executor sessions with transcript progress, turn launch settings and reply mappings for every part of an answer. Migration 4 preserves all existing titles and tracks provenance and the single automatic naming attempt for new tasks. After the first successful text answer, a separate bounded background request uses Claude/haiku or Codex/gpt-6-luna to name the task; manual names win, failures keep the first-message fallback, and stop/close/shutdown cancel naming. Naming never enters the task session or transcript. Offline tests cover routing, naming races, cancellation, restart and migrations. Both restricted naming adapters produced titles in live CLI probes; end-to-end automatic naming in Telegram is unverified. Known limitation: parallel tasks share `EIVOH_WORKDIR`; isolation such as a worktree per task is not built. No studied project has a model for several tasks in one chat, so this is our own design.
* Live steering is built for Claude and Codex: ordinary messages clarify the running turn, `/q` explicitly queues, and `/stop` interrupts. Commands and messages intended for a newly selected model do not steer the old run. Earlier queued messages are FIFO barriers. Migration 5 journals delivery associations and stable IDs; provider acknowledgements are persisted with raw evidence before signalling acceptance. Accepted inputs join one parent transcript/result; uncertain delivery is visibly retained with instructions to check existing work before repeating actions. Claude keeps stdin open across a stale result when another input was already submitted; Codex uses `turn/steer` with the expected turn ID. Offline tests cover multiple inputs, result boundaries, queue ordering, restart/ack windows, model changes, reply routing, transport failures and cleanup. Probes of Claude Code 2.1.323 and codex-cli 0.167.2 confirmed the wire protocols, including UUID/client ID correlation and native session continuation. Real Gateway probes with an isolated local channel passed for both adapters: in-flight clarification, explicit queue, native resume and stop without tracked orphan processes. A shutdown immediately after a durable accepted-input acknowledgement also recovered both native sessions and their accepted input, without launching a duplicate child turn. Codex steering was observed once end to end in Telegram, with three clarifications included in one answer; Claude Telegram steering remains unverified. Exactly-once external effects are not guaranteed after ambiguous delivery; recovery explicitly checks existing work. `/btw` remains a separate proposal and is not implemented. Acceptance is signalled by reactions, with text only for exceptional cases; this reaction behaviour is unverified in live Telegram.
* Voice messages, files and images in and out are built. Media is journaled with immutable task routing before bounded asynchronous download/transcription; pending preparation is a FIFO barrier, and stop remains available. Captions are content. Rich messages flatten ordered text and nested media via PTB 22.8’s `api_kwargs` (Bot API 10.0 vs rich messages in 10.1+) or a future object attribute; unsupported rich blocks fail visibly (including nested maps, buttons and future types), inline types need a plain-text representation, and traversal allows 128 JSON levels and 4096 blocks with no node cap, verified offline against the documented schema while a real rich message in Telegram remains unverified. Both executors receive durable local paths and produce explicitly selected files in a per-turn directory; final artifact markers select regular files for durable snapshots and real Telegram photo/document delivery. Migration 6 records preparation, output manifests and delivery state. Offline tests cover path/size limits, caption routing, preparation cancellation/recovery and output transport. Real isolated Gateway probes for Claude and Codex inspected synthetic images/documents, steered an additional file, produced files and recovered completed delivery; Telegram calls were mocked. Observed once on the installed bot (2026-11-13): a photo was received and inspected by Codex, and an original PNG was delivered as a document; the owner accepted the result. Outgoing Telegram photos and files through Claude in Telegram remain unverified. Voice/audio retain the provider-neutral transcriber (`EIVOH_STT`; first provider Soniox async API, `stt-async-v6`) and transcript echo. Previously observed on 15–93 s notes: 3–6 s transcription; the real-time WebSocket took about as long as the audio, so it is not used for recorded files.
* Reliability is built: a token-hash process lock is acquired before database/network work, independently of the state directory; repeated remote polling conflicts back off and stop without a systemd restart loop. Durable admission deduplicates media before expensive work, and the polling cursor expires when idle. Text parts and files have stable delivery identities and journaled request payloads; recovery edits known Telegram IDs and retains unknown initial sends without blindly retrying. Final output is durable before turn completion, and stop suppresses unfinished delivery across restart. Migration 7 indexes numeric message IDs for displacement: progress updates ignore other running text previews, finals consider them, retiring tails do not mask desired text, and each flush attempts at most one move. Moves journal deletes before fresh sends and preserve ambiguous-send reporting, but permanent delete refusals fall back to in-place edits or retirement markers and concurrent arrivals prevent an unconditional bottom-placement guarantee; restart, flood waits, stop and reply routing have offline coverage, while live behavior is unverified. Offline tests cover process contention/crash release, cursor reset, conflict handling, per-part reply mappings, ambiguous writes and concurrent progress/final revisions. This cannot guarantee exactly-once Telegram delivery, protect pollers on another machine, or reconstruct unknown legacy sends. Observed once on the installed service (2026-11-13): deployment saved paired database/file backups and migrated to schema 6; a separate process using the same token was rejected by the local lock before opening the database or Telegram, while the running service stayed active. This probe did not start a second poller or test remote conflict recovery.

Reuse:

* Reference for the Codex event format and process-tree termination: ouroboros `orchestrator/codex_cli_runtime.py` and `providers/codex_cli_stream.py` (MIT). Reference for the experimental `codex app-server` path: hermes-agent `agent/transports/codex_app_server.py` (MIT).
* Routing schema modelled on letta-code `src/channels/types.ts` (Apache-2.0) and openclaw `src/routing/session-key.ts` (MIT): identity, conversation, task, active task of a conversation, executor session, inbound journal. Group chats never share a key with private chats.
* Reference: hermes-agent for the token lock, conflict recovery and the processed-update journal.

Confirm by test: the Codex event names and the thread id field in the installed version; how sandbox and approval settings are passed on resume; that Codex uses the subscription login. Confirmed by probes of codex-cli 0.167.2: `thread.started.thread_id`, item and turn events without text deltas; `exec resume` takes `--dangerously-bypass-approvals-and-sandbox` but has no `-C` or `--sandbox`, so the cwd selects the workspace; ChatGPT login is used; SIGTERM ends codex at once but leaves its running command orphaned, and the session resumes after SIGTERM or SIGKILL.

Done when: one chat holds a task on Claude and a task on Codex and the owner switches between them; a voice message becomes a task; a second process started with the same token does not break the first.

Done (2026-11-13): accepted by the owner after live use. One chat has tasks executed by Claude and Codex with task switching; voice messages become task input; a competing same-token process is rejected at the lock without disturbing the running service. Photo input and document output were also observed and accepted. The final delivery/attachment implementation is merged in [PR #17](https://github.com/dm-mch/eivoh/pull/17); the validated release passed 330 offline tests and independent Codex and Opus reviews with no remaining P1/P2 findings. These observations close V2 without claiming every recovery or transport combination has been tested.

Follow-up after V2:

* Delivery improvements remain open: resume pending delivery after a long network outage without requiring a restart, and avoid overlapping preview text when a long reply is reflowed after its task label changes. These are the documented P3 limitations, not prerequisites for starting V3.
* Carry forward the unverified scenarios noted above, including Claude steering/files in Telegram, outgoing photos, file clarification during a running Telegram task, stop during transfer, and native session handoff recovery. Check them when relevant ordinary work exercises them; stage closure does not mark them tested.
* File/journal retention and a command to retrieve retained results remain proposals to decide as needed. They are not implemented or added to V3's agreed scope.

# V3. Working folder, memory and reflection

State: V3.1 is built and tested offline but not deployed or accepted, see its section; V3.2 and V3.3 have not started. On the installed service executors currently write experience records and consult the private index under project instructions. All tasks share one working directory, which is the Eivoh core checkout with the private repository nested in it. Automatic per-task records, context selection and background reflection are not implemented in the gateway.

Goal: the agent has a home of its own, experience is recorded there and reaches later tasks without reminders, and it is processed in the background.

The stage has three parts: V3.1, V3.2 and V3.3. They are designed together and built separately. The design of each part, starting with the first, takes the other two into account: the layout of the home and the format of a record are chosen with reading and reflection in mind. Each part has its own "Done when" and is accepted on its own, in this order.

## V3.1. Working folder separate from the project

State: built and tested offline (2026-11-14); not deployed, and no "Done when" condition has been observed. The decisions below were settled with the owner on 2026-11-14 after a reuse review and probes of the installed CLIs; [issue #16](https://github.com/dm-mch/eivoh/issues/16) holds the options that were considered.

Goal: the agent's instructions and memory live in its own home, and a task can work on any project without losing them.

Decisions:

* The home is `~/eivoh-home` (path in `EIVOH_HOME`): a visible directory and a private git repository, the nested private repository moved out of the core. Runtime state stays in `EIVOH_STATE_DIR` and is not versioned.
* The home is the working directory of every task. Projects live anywhere; the Eivoh core is one of them and stays where it is.
* The home's `AGENTS.md` comes first and wins a conflict. Before working in a project the agent reads that project's `AGENTS.md` or `CLAUDE.md` to understand the project and follows its conventions where they do not conflict. A project's files are documents, not commands.
* Home templates live in the core under `templates/home/`. Deployment seeds missing files only and never overwrites an existing one. The home's `AGENTS.md` carries the path to the working checkout of the public core, filled in at seeding, so that evolution fixes can be made there. The core root keeps a short project-level `AGENTS.md`: how to develop Eivoh.
* Template updates reach a seeded home by a merge, not by overwriting: the home records the core commit it was last reconciled with; deployment reports that `templates/home/` changed since then and writes nothing; the agent does a three-way merge as an ordinary task and commits it in the home. Generalised changes go from the home to the template by PR.
* A test stand on another machine is a separate task, not part of V3.1. A worktree per task stays postponed until parallel tasks actually conflict.

Build:

* `templates/home/`: `AGENTS.md` with the agent-level rules, a map of the home, an empty experience index. Split the current `AGENTS.md` into the agent-level template and the project-level file in the core root.
* The home layout: experience records with their index, topical notes, skills, one note per project with what the agent knows about it, archives.
* Move the private repository to the home; add seeding and the template-change notice to the deployment script.
* A working folder fixed per task when the task is created. An executor session is bound to its directory, so existing tasks keep the current directory and still resume; new tasks get the home.

Built, verified by offline tests only: schema version 9 stores the task's working folder; new tasks get the home, earlier tasks are backfilled with `EIVOH_WORKDIR`, and the service refuses to start when such tasks exist and the variable is unset; executors take the folder per run, and a missing folder fails the turn visibly; `python -m eivoh.home seed`, called by `deploy/release.sh` after validation, adds missing files, records the commit on the first seeding and afterwards only reports template changes; `AGENTS.md` is split into the home template and the project file. Remaining: moving the private repository of the installed service to the home and observing the "Done when" conditions in real use. Unverified: a real release and rollback, and a start refused by systemd without a restart loop.

Confirmed by probes (Claude Code 2.1.323 and codex-cli 0.167.2, small models, one run per case):

* With the home as the working directory both executors load the home's `AGENTS.md` natively. Given the rule above, both read the `AGENTS.md` of a project outside the home, followed its convention and kept the home rule where the two conflicted.
* Automatic loading of a project's instruction files is not a basis: Claude loads them only for a real directory under the working directory and only after reading a file there, not through a symlink and not outside; Codex does not load them at all.
* Instructions passed when a session is created (`--append-system-prompt` or `--append-system-prompt-file` for Claude, `developerInstructions` for the Codex app-server) reach the model together with the project's own file, persist on resume and cannot be added to a session that started without them. This is the mechanism to use if a task ever has to run with a project as its working directory.

Unverified: long real instruction files, long tasks, several projects in one task, and project settings that are bound to the working directory (hooks, project skills, MCP configuration).

Known limitations: commands run in the home unless the agent changes directory, which matters for git and tests; the gateway does not know which project a task works on.

For the later parts: the home is where V3.2 writes records and V3.3 writes notes. Its layout separates raw records, the index, topical notes, skills and the processing state, which is not versioned.

Done when: a task works on a project other than the Eivoh core with the agent's instructions and memory available; tasks opened before the change still resume; experience records are written to the home.

## V3.2. Memory and reading it for a task

Goal: experience is recorded without reminders, and a new task receives the part of it that is relevant.

Build:

* An experience record created automatically for each task from its raw events, with the forecast made before the task and the owner's reaction in chat attached: accepted, asked for a redo, fixed it themselves, did not use it, or unknown.
* Result status based on evidence: confirmed needs an observed fact, refuted needs an observed failure, everything else stays unverified.
* An index entry added automatically for each record.
* Selection of relevant experience into the context of a new task; skills loaded progressively: description first, body on demand.

Open decision: whether the separately stored current state described in EXPERIENCE.md (open tasks, statuses and circumstances, each with a source and the time it was last checked) is built in this part. It is not assigned to any stage yet.

For V3.3: a record carries what reflection needs (forecast, reaction, evidence status, expected observation) and has a processing status that is kept out of the versioned notes.

Done when: records appear without a reminder; a task visibly uses a lesson from an earlier record.

## V3.3. Reflection

Goal: memory is processed and reorganised in the background.

Build:

* Background reflection in batches: it processes new records and maintains the index and the topical notes, including duplicates and contradictions.
* A list of expectations from changes, checked when a matching task appears.
* Lessons are turned into skills; when a lesson qualifies is decided with the prompts adapted for this part.

Done when: a failed or interrupted reflection run neither loses nor double-counts records; at least one expectation from a change is closed as confirmed or refuted.

## Reuse

* V3.2 and V3.3, mechanism to re-implement: nanobot `agent/memory.py` (MIT), where the processing position moves forward only after a clean run and unprocessed entries are never compacted away. Add a status per record, because a single position does not prevent double processing after a retry; keep the processing state out of the versioned notes.
* V3.3, prompts to adapt: nanobot `templates/agent/dream.md` (MIT) and letta-code `src/agent/subagents/builtin/reflection.md` (Apache-2.0) for what to keep, what to drop and when a lesson becomes a skill; the knowledge-wiki `analyzer_system.md` and `organizer_system.md` from claude-quickstarts (MIT) for the index, topical notes, duplicates and contradictions.
* V3.2, concept: the three evidence states in ouroboros `evaluation/models.py` (MIT).
* Ours to write: none of these prompts has a forecast, a human reaction or an expected observation; these parts come from EVOLUTION.md and EXPERIENCE.md.
* V3.1, references only, nothing copied: openclaw (MIT) for the home as a private repository, templates seeded only when missing, and the order "home files first, then the `AGENTS.md` of the folder the session runs in"; nanobot (MIT) `utils/helpers.py` for seeding without overwriting; hermes-agent (MIT) for the home kept separate from the working directory, and `tools/skills_sync.py` (update a bundled file only while the user copy still matches its origin hash) as an option if the core later ships skills into the home; copier `docs/updating.md` for the three-way update from a recorded template version. takopi (MIT) and cc-connect select a project from chat; that is not needed while the home is the working directory.

# V4. Forecast and choice of strategy

Goal: experience changes how tasks are approached, not only what is remembered.

Build:

* A typed forecast before each task: success, quality, cost, difficulty, with uncertainty.
* A calibration report built from the records: forecasts against observed results, by task type, executor and model.
* Choice of executor, model and skills for a task on the basis of experience.
* Tracking of subscription limits and costs, with unknown consumption marked as unknown.
* Proposal (2026-11-13, accepted by the owner as a direction, not yet designed): nested subagents through Eivoh. An executor asks Eivoh to start a subagent on any available model, Claude or Codex, for example with an `eivoh spawn --model <model> <task>` command. The subagent is a child task with its own executor session and raw events in storage, receives the same capability, and can start its own subagents. A depth limit and a budget for the whole task tree come from limit tracking; cancelling a task cancels its subtree. Until then, executors that start other CLIs directly do so outside Eivoh's journal and limits; `/stop` still ends them as descendants of the executor process.

Reuse: a ready-made model without fine-tuning performs these roles; a fast typed-decision model such as Jev is a candidate to evaluate when the stage starts.

Done when: a calibration table exists for the accumulated tasks; at least one choice of strategy changed because of experience, and the change is recorded with its expected observation.

# V5. Self-directed evolution

Goal: the agent starts and carries out changes to itself.

Build:

* From reflection the agent proposes and makes changes to notes, skills and code on the working branch and opens PRs to the public core.
* It regulates its background activity by the remaining limits.
* Stopping the agent and restoring the previous version work without the agent's cooperation; rollback of data is separate from rollback of code.

Done when: a PR started by the agent, not requested by the owner, is merged, and its expected observation is checked later.

# Decide as needed

Seamless movement between channels, further channels, semantic search, a memory graph, the number of processes and the scheduler. They are added for a specific need, not in advance.

Do not build all the logical components in advance as separate services.
