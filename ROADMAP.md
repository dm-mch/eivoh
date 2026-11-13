# Eivoh development plan

Current stage: V2. V0 and V1 are done (2026-11-13). No dates are set. The architecture describes the target system; this file describes the order in which it appears and is the only place where the current stage is stated.

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

* Codex CLI adapter through `codex exec --json` and `codex exec resume`, one process per turn, cancel by terminating the process tree.
* Choice of executor per task from the chat; several tasks in one chat with a list and switching. No studied project has a model for several tasks in one chat, so this is our own design.
* Voice messages in, files and images in and out. Voice in is built early: Telegram voice and audio go through a provider-neutral transcriber (`EIVOH_STT`; the first provider is the Soniox async API, `stt-async-v6`) before journaling, and the transcript is echoed to the chat. On 15–93 s notes it took 3–6 s; the Soniox real-time WebSocket took about as long as the audio, so it is not used for recorded files.
* Reliability: one consumer per bot token with a local lock and recovery from a polling conflict, removal of duplicate updates after a restart, sending rules that do not post duplicates.

Reuse:

* Reference for the Codex event format and process-tree termination: ouroboros `orchestrator/codex_cli_runtime.py` and `providers/codex_cli_stream.py` (MIT). Reference for the richer but experimental `codex app-server` path, if `exec` is not enough: hermes-agent `agent/transports/codex_app_server.py` (MIT).
* Routing schema modelled on letta-code `src/channels/types.ts` (Apache-2.0) and openclaw `src/routing/session-key.ts` (MIT): identity, conversation, task, active task of a conversation, executor session, inbound journal. Group chats never share a key with private chats.
* Reference: hermes-agent for the token lock, conflict recovery and the processed-update journal.

Confirm by test: the Codex event names and the thread id field in the installed version; how sandbox and approval settings are passed on resume; that Codex uses the subscription login.

Done when: one chat holds a task on Claude and a task on Codex and the owner switches between them; a voice message becomes a task; a second process started with the same token does not break the first.

# V3. Experience and reflection

Goal: experience is recorded and processed without reminders, and later tasks receive the relevant part of it.

Build:

* An experience record created automatically for each task from its raw events, with the forecast made before the task and the owner's reaction in chat attached: accepted, asked for a redo, fixed it themselves, did not use it, or unknown.
* Result status based on evidence: confirmed needs an observed fact, refuted needs an observed failure, everything else stays unverified.
* Background reflection in batches: it processes new records, maintains the index and the topical notes, keeps a list of expectations from changes and checks them when a matching task appears.
* Selection of relevant experience into the context of a new task; skills loaded progressively: description first, body on demand.

Reuse:

* Mechanism to re-implement: nanobot `agent/memory.py` (MIT), where the processing position moves forward only after a clean run and unprocessed entries are never compacted away. Add a status per record, because a single position does not prevent double processing after a retry; keep the processing state out of the versioned notes.
* Prompts to adapt: nanobot `templates/agent/dream.md` (MIT) and letta-code `src/agent/subagents/builtin/reflection.md` (Apache-2.0) for what to keep, what to drop and when a lesson becomes a skill; the knowledge-wiki `analyzer_system.md` and `organizer_system.md` from claude-quickstarts (MIT) for the index, topical notes, duplicates and contradictions.
* Concept: the three evidence states in ouroboros `evaluation/models.py` (MIT).
* Ours to write: none of these prompts has a forecast, a human reaction or an expected observation; these parts come from EVOLUTION.md and EXPERIENCE.md.

Done when: records appear without a reminder; a failed or interrupted reflection run neither loses nor double-counts records; at least one expectation from a change is closed as confirmed or refuted; a task visibly uses a lesson from an earlier record.

# V4. Forecast and choice of strategy

Goal: experience changes how tasks are approached, not only what is remembered.

Build:

* A typed forecast before each task: success, quality, cost, difficulty, with uncertainty.
* A calibration report built from the records: forecasts against observed results, by task type, executor and model.
* Choice of executor, model and skills for a task on the basis of experience.
* Tracking of subscription limits and costs, with unknown consumption marked as unknown.

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
