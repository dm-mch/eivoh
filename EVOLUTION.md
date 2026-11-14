# Eivoh evolution

The goal of evolution is to solve new tasks more effectively through reflection on experience. Testing of changes, A/B tests, baselines, experiments and comparison of approaches are not part of the process. This is the target way of working; what is already implemented is stated in [ROADMAP.md](ROADMAP.md).

# The evolution cycle

1. Save the expectations. Before execution, record the task, the significant context, the strategy and the model. For this combination, forecast the probability of success, the quality, the cost and the difficulty, stating the uncertainty. Do not treat unknown costs as zero.
2. Get the result. Save the observed outcome of the ordinary task, the evidence, the errors and the actual or estimated costs. Separate what is known from what is unconfirmed; do not replace an unknown result with the expectation or with the executor's self-assessment. The main signal is the human's reaction: they accepted the result, asked for a redo, fixed it themselves or did not use it. Record the absence of a reaction as unknown, not as approval.
3. Analyse the difference. Compare the result with the forecast for success, quality and cost. Note what was unexpected and what data is missing. Do not equate forecast accuracy with the ability to solve tasks.
4. Formulate explanations. Save the causal hypotheses, their grounds, the alternatives, the confidence and the conditions under which they apply. Success alone, or agreement with the forecast, does not prove causation. Keep conclusions in proportion to the evidence; do not count a retelling as a new observation. New evidence appears in ordinary tasks.
5. Choose a change, or change nothing. Refine the knowledge, the beliefs, the forecast, the strategy, the assembly and selection of context, a skill or the design of the agent. Keep the scale of the change in proportion to the evidence. Repeated errors and costs deserve attention but do not prove that a particular change is useful. It is acceptable to keep a hypothesis without acting on it.
6. Make the change. Memory, strategies and skills can be updated; code and modules can be changed and new ones created. Record the change and its grounds, following the rules in the `AGENTS.md` of the agent's home and, for the public core, the repository rules in [AGENTS.md](AGENTS.md). Write down what should be observed in future ordinary tasks if the change is useful, and in which tasks it will show.
7. Save the experience. Link the task, the forecast, the observations, the hypotheses, the decision and the change, or the decision not to make it. Build up a picture of competence and of the cost of solving tasks, taking into account the specific model, the context and the conditions under which it applies. When a task comes up in which the change should show, compare what is observed with the expectation from the change and mark it as confirmed, refuted or unverified. This is a check during ordinary work, not a separate experiment. Use the experience and revise conclusions when new data arrives, without presenting an expected benefit as a fact.

# Resources and autonomy

The depth of reflection depends on the expected usefulness and the available budget. A separate, expensive LLM evaluation of every step is not required. The agent regulates the amount of work on its own, according to subscriptions, resources and remaining limits, allowing for incomplete accounting. The frequency of reflection and numeric thresholds are not fixed.

Resources do not extend permissions. The agent does not extend its own rights and does not remove the ability to stop it and to restore a previous version.

In a message about a change, the agent distinguishes the change itself, the expected benefit and the evidence it has. The contents and location of an experience record are described in [EXPERIENCE.md](EXPERIENCE.md).

# Bootstrap and recovery

We create the initial Markdown version. On its basis and on accumulated experience, the agent takes part in creating the next versions of itself, changing memory, ways of working, skills, code and the set of modules.

The history keeps the changes, the grounds from reflection and the evidence. The previous version can be restored even if the agent is broken. Rollback of data is separate from rollback of code. Traceability means that changes and their grounds can be followed.

# Open questions

* How should confidence in forecasts and hypotheses be represented, and refined when observations conflict?
* How should the depth of reflection and the attention given to different tasks be chosen within the available resources?
* How should the conditions under which skills and conclusions apply be marked, so that a single experience is not generalised too widely?

Specific algorithms for updating beliefs and choosing strategies, including probabilistic models and contextual bandits, have not been chosen. Weights, frequencies and thresholds remain open.
