# Eivoh development plan

Current stage: V0. The sequence that follows is proposed for agreement; no dates are set. The architecture describes the target system; this file describes the order in which it appears and is the only place where the current stage is stated.

# V0. Markdown and bootstrap

State: a Markdown foundation, not a finished service. Tasks are carried out by a running Codex or Claude using the project documents. The gateway, continuous operation on a VM, channels, storage and automatic reflection belong to later stages; they are not assumed to exist.

* Agree on README, VISION, ARCHITECTURE, AGENTS, EVOLUTION, EXPERIENCE and ROADMAP; keep them as Markdown in the working repository.
* Separate the public documents from private correspondence and experience. Keep the discussion archive as a private source, not as instructions that apply automatically.
* Apply AGENTS.md to a real task of developing the project through Codex or Claude; record the result and the reflection in private/experience/.

Result: agreed instructions let the agent continue development without requiring services that do not exist yet. Further changes rest on these files and on the experience of applying them.

# Next step. A minimal working loop

* Choose a language and minimal storage for the first task; Python and SQLite remain candidates.
* Connect one native CLI executor: start, session continuation, output and cancellation. Design the adapter interface for both Codex and Claude from the start. Find out separately what support there is for session resume and prompt cache.
* Save the task, the significant context, the events and the result independently of the CLI session. Provide for interruption and recovery.
* Apply brief reflection to ordinary tasks and save the grounds for changes. Do not require a separate, expensive evaluation of every action.

Result: Eivoh takes a real task from request to result, saves its state and can continue after an interruption. It is shown what already works and what remains unverified.

# Continuous operation and channels

* Bring up a continuously running gateway on the VM, with control over start, stop and recovery.
* Connect Telegram and the CLI through a common channel interface. Create a separate Eivoh bot for Telegram. Distinguish the user, the conversation, the task and the executor session; do not mix private and group contexts.
* Add background tasks and tracking of available resources. Change priorities and the amount of activity according to user requests, remaining limits and the incompleteness of the accounting.

Result: tasks are available through the connected channels and are not lost on restart. Seamless movement between channels is developed separately, without blocking the first useful launch.

# Memory and independent development

* Grow the memory according to EXPERIENCE.md: raw records, a small index, topical knowledge, a separate current state; create episodes when needed.
* Add incremental background reconciliation, correction of notes and selection of relevant context. Keep the sources, the contradictions and the conditions under which things apply.
* Develop forecasting, strategy choice and reflection according to EVOLUTION.md, on the experience of ordinary tasks. Change not only memory but also skills, code and modules.
* Automate compliance with the repository rules, the privacy check, stopping and recovery. The agent does not extend its own rights.

Result: experience is available to later tasks, changes have grounds and a history, and the previous version can be restored. The procedure for reflection and checking follows EVOLUTION.md.

# Decide as needed

The number of processes, the specific scheduler, the sources for limits, semantic search, a memory graph, competence algorithms and automatic model selection. New dependencies and the candidates from ARCHITECTURE.md are connected for a specific need, with licences and compatibility taken into account.

After each stage, refine the next step from real experience. Do not build all the logical components in advance as separate services.
