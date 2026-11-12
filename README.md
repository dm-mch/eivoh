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

The target system runs continuously on a VM, supports interchangeable communication channels and native CLI executors, and keeps its own tasks and memory. Telegram will probably become the main channel, but it does not define how the system is built. Python and SQLite are being considered; the stack is not yet approved.

# Private and public

The public core contains the shared documents and code. The private/ folder is the private part, kept in a separate repository: experience, correspondence, archives and state. The agent makes core changes on the working branch `work`; they reach `main` through a PR. The rules for changes and publication are in [AGENTS.md](AGENTS.md).
