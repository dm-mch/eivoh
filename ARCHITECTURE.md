# Eivoh architecture

This is the target system. The components are logical and need not be separate processes. What is already implemented is stated in [ROADMAP.md](ROADMAP.md).

# Components and task flow

Task and context → strategy and its forecast → execution → evaluation → update of knowledge and of the system.

* The Agent Executive chooses between user tasks, self-improvement and waiting, taking the available resources into account.
* The Predictor uses a competence model: before execution it forecasts success, quality, cost and difficulty for the chosen strategy and the task context.
* The Strategy Policy chooses the skills, tools, executor and budget. It is separate from memory and from the competence model.
* The Task Solver carries out the chosen strategy through the LLM Executor and Tools and Environment; it returns the result and the execution events.
* The Evaluator records the result and keeps the available evidence. The main evaluation signal is the human's reaction to the result. Unconfirmed conclusions are not presented as facts; verified, rejected and unverified results are kept distinct.
* The Learning Engine (Learner) compares forecasts with results, updates the estimates of competence and of how useful skills are, and passes changes to memory and to the evolution loop. Success, quality and cost are tracked separately, tied to the task type and the model.

Predictor, Strategy Policy and Learner are roles, not trained models. They are performed by a ready-made model without fine-tuning: an LLM, or a fast typed-decision model that takes unstructured state and returns a value of a given type with a probability. Quality depends on how the context is assembled for it from experience records and on how the answer type is defined. The model's probability is not assumed to be calibrated in advance: calibration shows when forecasts are checked against results.

# Environment, channels and executors

* Continuous operation on a VM. Channels are equal and pluggable; any of them can become the main one. Telegram is probably the main channel and the CLI a secondary one. Eivoh has its own Telegram bot: the token is not shared with other agents or sessions, and a token has a single consumer. The target interface is a single chat, with seamless movement between channels and switching of contexts.
* Routing distinguishes the user, the conversation, the task and the executor's native session. Only explicitly matched accounts are linked; private and group contexts are not mixed.
* The gateway is thin: a channel, routing, an executor adapter and state storage. Memory, skills and tools stay with the executors and in Eivoh's files. Codex and Claude are connected through one adapter interface.
* The adapters for the real Codex and Claude CLIs support the available operations: start, session continuation, streaming output and cancellation. Session resume and prompt cache are reused where possible but accounted for separately: continuing a session does not prove caching. Support and effect are verified against the CLI and the provider.

# Memory and resources

* The Memory System keeps separate layers: identity and state; raw events and task episodes; knowledge and beliefs; skills. Tasks, significant context and memory belong to Eivoh regardless of the model or the CLI session. The design of the layers, the contents of a record, search and updating are described in [EXPERIENCE.md](EXPERIENCE.md).
* The Budget and Safety Controller accounts separately for permissions to act and for the resource budget: subscriptions, limits, costs and forecast consumption. The agent regulates its own activity according to the remaining limits, allowing for delays and uncertainty in the accounting. Unknown consumption is not zero; repeated reports do not double the costs. The controller supports stopping and rollback; the depth of evaluation and reflection is part of the budget.

# System changes

* Evolution changes memory, strategies, skills and code, improves existing modules and creates new ones. ARCHITECTURE.md defines the components and interfaces; the detailed change cycle belongs to EVOLUTION.md.
* Changes are traceable and reversible. The reflection cycle, the checking of expectations from a change, and recovery are described in [EVOLUTION.md](EVOLUTION.md).
* The public core is separate from personal data and experience. The rules on repositories and privacy are in [AGENTS.md](AGENTS.md).

# Candidates for reuse

These are fragments of code that were studied, not ready integrations in Eivoh. They have not been run or tested. The links are pinned to specific commits.

* Hermes: [agent/codex_runtime.py](https://github.com/NousResearch/hermes-agent/blob/c59b667c76dc75e07bb96e98630de78c14d7a008/agent/codex_runtime.py#L599). Lifecycle of the Codex app-server; saving and continuing a native session.
* OpenClaw: [extensions/anthropic/cli-backend.ts](https://github.com/openclaw/openclaw/blob/61db4b336f89957d7916580871b288d0d8c9ba82/extensions/anthropic/cli-backend.ts#L254). Claude CLI, streaming events and the session/resume arguments.
* OpenClaw: [src/routing/session-key.ts](https://github.com/openclaw/openclaw/blob/61db4b336f89957d7916580871b288d0d8c9ba82/src/routing/session-key.ts#L179). Linking identifiers from different channels to one user identity.
* Nanobot: [nanobot/agent/memory.py](https://github.com/HKUDS/nanobot/blob/c5f2ce5089549b42a6ad2a28369791d72ef947d0/nanobot/agent/memory.py#L506). Processing new records in batches and keeping experience that has not been processed yet.
* Hermes: [tools/skills_tool.py](https://github.com/NousResearch/hermes-agent/blob/c59b667c76dc75e07bb96e98630de78c14d7a008/tools/skills_tool.py#L290). Progressive loading of a skill: the description first, then the contents of SKILL.md.
* Q00/Ouroboros: [src/ouroboros/evaluation/models.py](https://github.com/Q00/ouroboros/blob/592c667f076c3dad7ef68d7844e91480d80135d0/src/ouroboros/evaluation/models.py#L568). Separating confirmed, rejected and unverified results on the basis of evidence.
* TypeSafe: [Jev, System One models](https://typesafe.ai/blog/introducing-system-one-models-and-jev). A fast typed-decision model with probabilities; a candidate for the Predictor and Strategy Policy roles. Early access through a waitlist, no open weights; the claims about speed, price and calibration are the authors' own.

# Open decisions

How processes and background work are organised on the VM, storage and recovery, linking of channels, sources for tracking limits, compatibility of CLIs and providers. Python and SQLite remain options, not an approved stack.
