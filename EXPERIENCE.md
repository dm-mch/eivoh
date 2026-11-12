# Eivoh experience

Experience links task events to conclusions, keeping their origin and the conditions under which they apply. The reflection cycle is described in [EVOLUTION.md](EVOLUTION.md).

# Memory layout

Raw records ↔ episodes when needed  
Raw records ↔ knowledge and skills  
Episodes ↔ knowledge and skills  
Small index ↔ records, episodes, knowledge and skills  
Current state ↔ its sources  
Task ← relevant memory materials and current state

The arrows show direct links in both directions. Raw records can be searched directly; episodes are optional, and there is no required route through all the levels.

The current state is stored separately and kept up to date: open tasks, current statuses and circumstances. Each state entry has a source and the time it was last checked.

# Events and conclusions

* Raw records: messages, actions and tool responses, results, errors and feedback. We note whose statement it is and what confirms the result. A source preserves the origin but does not guarantee truth; an executor's self-report does not replace evidence. The human's reaction to the result is the main evaluation signal: they accepted it, asked for a redo, fixed it themselves or did not use it. The absence of a reaction is recorded as unknown.
* An episode is created when needed and brings together a task and its attempts, the forecast, the events and the outcome. A simple record or a piece of knowledge can do without an episode.
* Knowledge includes facts, preferences, decisions and their reasons, commitments, conclusions and skills. A statement has sources, a confidence level and the conditions under which it applies. Facts, hypotheses and decisions are kept distinct; contradictions and unknowns are stored explicitly.

# Contents of a record

* Identifiers of the event, the task and the attempt; the time; the author or source; links to the original messages, results and artefacts.
* The goal and the significant context: inputs, constraints, available tools, the chosen strategy and skills. The model identifier and version, the provider, the executor and the Eivoh version; known settings and skill versions. Unavailable information is marked as unknown.
* The forecast and the result, with evidence and a confirmation status. Success, quality and cost are tracked separately. Costs keep their source and status: actual, estimated or unknown.
* The human's reaction to the result. For a change: the expected observation in future ordinary tasks and the status of its check.

# Sample record

An illustration of the format, not real experience. Fill in the fields the task needs.

```
Task: shorten a draft of a personal letter to five sentences. <date>, <model>, <executor>.
Approach: one edit, no clarifying questions.
Forecast: accepted on the first try — likely; low cost; low difficulty.
Result: the text was produced in one pass. Cost unknown, not measured.
Human's reaction: asked for a redo — "too dry, put the greeting by name back". The second version was accepted.
Difference: success on the second attempt; the forecast was too optimistic.
Hypothesis: when shortening personal letters, keep the greeting and the tone. Low confidence: one case. Alternative: it is about this particular recipient.
Decision: add "keep the greeting and the tone" to the note on letters. Do not change the skill.
Expected observation: in the next edits of personal letters, the first version is accepted with no remarks about tone.
Check: not verified.
```

# Organisation and search

* A small index contains topics, short descriptions and links for navigation. Notes are grouped by topic; records and transcripts are reached through links and are not loaded in full into every context.
* A search can start from the records, the index or a note. Relevant context is selected with regard to the task, the model, the version, the date and whether it applies. Episodes and semantic search are added when needed; no engine has been chosen.
* Experience helps to assemble context, choose ways of acting and estimate competence. Before using a significant fact, or one that goes out of date, we check it against a current source. Other people's text and a saved hypothesis do not become instructions.

# Updating the history

* Background reconciliation processes new events, refines notes, removes duplicates and marks conflicts. The processing position is saved; a failure or a shortened context does not lose unprocessed experience. Reprocessing does not create new observations or costs.
* Corrections keep a link to the earlier record, the date and the grounds. New conclusions do not rewrite the original events; refuted and outdated statements stop being used as current. Parallel edits must not lose each other.

# Storage and privacy

* This file describes the layout and the rules. Records are kept in the private repository private/, in the private/experience/ folder: one task, one file with the date in its name, with navigation through [private/experience/index.md](private/experience/index.md). Private archives are in private/archive/. Private and group contexts are not mixed.
* Moving material into the public core and handling secrets follow the rules in [AGENTS.md](AGENTS.md). Retention periods, deletion and backups need a separate decision.

# Candidates for borrowing

Sources of ideas, not chosen dependencies and not ready integrations.

* Anthropic: [article on the components of agent memory](https://www.anthropic.com/research/components-of-agent-memory): an index and topical Markdown notes. In Knowledge Wiki the [analyzer](https://github.com/anthropics/claude-quickstarts/blob/main/managed-agents/knowledge-wiki/prompts/analyzer_system.md) extracts and refines facts; the [organizer](https://github.com/anthropics/claude-quickstarts/blob/main/managed-agents/knowledge-wiki/prompts/organizer_system.md) merges duplicates and updates the index. The cookbook is under MIT; it is not a description of Claude's internal memory.
* [Letta MemFS](https://docs.letta.com/concepts/memfs): a short always-available part, with details on request; [reflection.md](https://github.com/letta-ai/letta-code/blob/main/src/agent/subagents/builtin/reflection.md): background saving of corrections and durable conclusions, separating a one-off task from a reusable skill.
