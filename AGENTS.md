# Developing Eivoh

Instructions for work on this repository, the public core of Eivoh. The agent's own rules — how to approach any task, memory, experience, changing itself, privacy — are in the `AGENTS.md` of the agent's home (`EIVOH_HOME`, by default `~/eivoh-home`). They come first; this file adds what is specific to the core. A home is seeded from [templates/home/](templates/home/AGENTS.md).

A task opened before the home existed still runs in this directory: read the home's `AGENTS.md` first.

# Current stage

See [ROADMAP.md](ROADMAP.md) for the current stage and what already works. The components and mechanisms in ARCHITECTURE.md, EVOLUTION.md and EXPERIENCE.md describe the target system; check in the repository whether they exist. Do not require services, storage, integrations or commands that have not been created yet.

# Before a task

1. Read this file and, in [ROADMAP.md](ROADMAP.md), the section "How the plan works" and the section of the current stage. Read the rest as the task requires; for a substantial task, or when in doubt, read everything.
   * Goals, values, choice of direction: [VISION.md](VISION.md).
   * Components, channels, executors, code: [ARCHITECTURE.md](ARCHITECTURE.md).
   * Reflection, self-modification, checking changes, recovery: [EVOLUTION.md](EVOLUTION.md).
   * Memory design, record contents and a sample record: [EXPERIENCE.md](EXPERIENCE.md).
2. Check the actual structure, the state of changes and the instructions in force in the repository. Preserve other people's edits. Do not treat open architectural questions as decisions already made.

# Work and verification

* Make minimal, related changes. Build on what exists; do not add a stack, module, algorithm or dependency as if it were already approved. Label a proposal as a proposal.
* For Markdown, check that the instructions are clear, the documents are consistent, the links work and no capabilities are invented. For code, use the existing checks relevant to the task. Report what was done and what remains unverified.
* Ordinary checks of quality, correctness and safety are allowed. Do not introduce mandatory A/B tests, baselines or comparative experiments as a stage of evolution; the procedure is described in EVOLUTION.md.
* Use the current instructions and the relevant experience when creating the next version of Eivoh.

# Home templates

* [templates/home/](templates/home/) is what deployment seeds a home from: missing files only, an existing file is never overwritten.
* The live rules of an installation are changed in its home. A rule that would serve any installation is changed in the template, generalised and without private data.
* A template change reaches an already seeded home by a merge done as an ordinary task; the procedure is in the home's `AGENTS.md`, under "Changing yourself".

# Repositories and completion

* This section is the only place for the rules on publishing this repository; other documents refer to it. The private part is the agent's home, a separate private repository outside this one: the agent's rules, experience, archives, personal skills and notes. A `private` entry in this checkout, when present, is a link to the home kept for tasks opened before the move, and is ignored by the core.
* Before a public PR, apply the privacy rule of the home's `AGENTS.md`, section "Privacy", to all the material to be published.
* A task that changes code while other tasks may work in this repository uses its own git worktree on its own branch, created from `origin/work`, outside the main checkout; the release checkout of the running service is never touched. The branch is merged into `work` in the main checkout, then the worktree is removed. The gateway does not create worktrees; the task does. The procedure for an installation belongs in its home's skills.
* Commit core changes to the working branch `work` and use them immediately. Prepare the PR from it into `main` on your own, after the privacy check. A merge into `main` is possible only after a human approves: the approval concerns the public version, not the agent's work on its own branch.
* In the final message, state the changes made, the checks performed, the limitations and the decisions that remain open. Do not claim that a component is implemented or that something improved without evidence.
