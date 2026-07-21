# Workflow Specification

The CLI reports sparse `ASCENSION n` progress at the stage boundaries below. Adaptive research
also reports round planning, agent-batch launch, and candidate-audit packaging. These updates are
operational milestones only; model reasoning and per-call details are not streamed to the user.

## Stage 0 — Intake

Inputs: problem file, optional framework override, config, CLI flags.
Outputs: original problem, normalized problem, hashes, environment snapshot, initial state.

## Stage 1 — Prompt compilation

The compiler receives:

- the complete problem text;
- the verbatim framework;
- instructions in `resources/prompts/prompt_compiler.md`;
- web search (enabled by default, omitted under the global `--no-web-search` policy);
- the structured output schema.

It first decides whether the input uniquely identifies one mathematical target and exact success
criterion. A concise input is sufficient when it does. If choosing a target would require
guessing between materially different interpretations, the compiler returns
`needs_clarification` with a reason and focused questions. ASCEND persists that request, skips
all research/manuscript/Lean stages, writes the final report, and asks the user to revise the
problem file and start a new run.

Otherwise it returns a full adapted prompt plus a formal claim contract, a source ledger, and a
literature classification: `unknown`, `no_exact_match_found`, `partially_resolved`, or
`fully_resolved`. Placeholder validation flags only strong editorial markers; ordinary
mathematical bracket notation, citations, links, code, and LaTeX are protected. ASCEND persists
the compiled result and validation diagnostics before attempting one bounded, sentence-only
repair. An unresolved marker blocks the workflow only in the exact target or success criterion;
an optional sentence is removed with a recorded warning. Partial/full resolution claims require
verified sources and an exact statement-and-hypothesis comparison.

The adapted prompt front-loads a compact research-mandate snapshot modeled at a high level on
the public Cycle Double Cover prompt: exact target, boundary conventions, near-misses, adaptive
independent search, persistence, adversarial review, search policy, and a proof-only completion
condition. The longer framework then expands each item into ASCEND's auditable protocol.

## Stage 2 — Adaptive research

Research is a nested orchestration boundary. The deterministic outer workflow hands the complete
compiled research prompt and exact claim contract to a dedicated model-driven research
orchestrator. That orchestrator proposes structured assignments; ASCEND launches and accounts for
the worker calls. The total number of logical workers across all rounds is bounded by
`research.maximum_research_subagents`, separately from per-round and concurrency ceilings.

### Initial round

A coordinator creates eight initial assignments by default, spanning at least four materially
distinct approach families. Suggested roles are not fixed quotas; examples include direct proof,
alternative structural formulation, hostile counterexample search, literature/known-theorem
mapping, computation, and formalization-aware lemma decomposition.

If the compiler found that existing literature resolves the target, the portfolio emphasizes
independent source verification, hypothesis matching, proof reconstruction, and formalization.
Known results must remain labeled as known rather than novel.

### Later rounds

Every fresh research-orchestrator context receives the complete compiled prompt and claim
contract again. It also receives the current registry, every visible worker report, audit repair
obligations, and a durable `research/continuity.json` handoff that explicitly classifies promising
routes, partial results, refuted directions and counterexamples, blocked routes and exact
gaps, dependencies, and prior directives. It returns:

- new assignments;
- workers to retire or redirect;
- claims requiring counterexample search;
- candidate lemmas requiring proof completion;
- whether a complete candidate package should be assembled;
- a budget-aware stop recommendation.

### Worker outputs

Workers must return concrete formal content using `ResearchWorkerReport`. A worker may report
that a route is blocked, but must identify the exact missing statement and any counterexample
found.

## Stage 3 — Candidate proof and audits

A candidate package includes theorem statement, definitions, lemma dependency graph, full
proofs, imported theorems, exceptional cases, parameter bookkeeping, and unresolved items.

Launch fresh agents for:

- foundational/quantifier audit;
- domain-specialist audit;
- hostile counterexample audit;
- complexity/quantitative audit when applicable;
- source-theorem audit for imported results.

The final judge may output only one of:

```text
accepted_for_manuscript
repairable_and_return_to_research
rejected
partial_result_only
```

A repairable verdict creates a new research round and includes exact obligations.

Workers are admitted through a bounded active window rather than all being queued at the model
backend at once. When a worker returns `candidate_complete`, ASCEND pauses the unfinished window,
packages that specific proof, and immediately runs every mandatory independent audit plus the
final judge. Acceptance cancels routes that never started and advances the workflow. If the gate
does not pass, ASCEND resumes the remaining assignments, retains the failed audit and its exact
obligations, and then evaluates the combined round normally. A worker's status alone is never
treated as proof verification.

The optional `--time-limit-minutes` allowance covers active execution across all stages and is
carried across resume attempts. It bounds in-flight model calls as well as pre-call and stage
boundary checks; paused time between CLI invocations is not counted. No wall-clock limit is
applied by default.

## Stage 4 — Manuscript and bibliography

The manuscript writer receives only the frozen accepted proof package, claim contract, audit
reports, verified source ledger, and manuscript prompt. It must not silently change the
result. It must include a Statement of AI Usage disclosing ASCEND with GPT 5.6 and cite both the
canonical ASCEND GitHub repository and ASCEND whitepaper arXiv preprint.

The bibliography verifier runs in a fresh context with web search. It checks every item and
every substantive related-work characterization. It creates a correction plan. The writer
may regenerate the manuscript, after which verification runs again. Limit cycles by config.

Only a fully verified bibliography may proceed.

`--no-web-search` also disables ASCEND's deterministic public-identifier HTTP resolver. Any
evidence that cannot be established from persisted provider metadata remains unavailable; gates
fail truthfully instead of treating offline status as verification. Consequently, a fully
search-free invocation is primarily intended for `--research-only` runs and cannot bypass the
verified-bibliography prerequisite for manuscript-to-Lean progression.

Compile LaTeX deterministically. Undefined references, missing citations, compilation errors,
or bibliography mismatches fail the stage.

Before entering Lean, an interactive run asks whether to continue with formal verification. A
`no` answer skips all Lean stages and proceeds to the final report. No answer within five minutes
defaults to continuing. Noninteractive runs cannot answer and therefore continue immediately.
The decision is persisted in `lean/consent.json`; resume verifies and reuses it.

## Stage 5 — Lean feasibility

Classify:

```text
full_formalization_recommended
main_theorem_formalization_recommended
verification_plan_only
not_reasonably_attainable
```

Explain the expected mathlib dependencies, difficult components, computational certificates,
and any mismatch between paper proof style and Lean suitability.

## Stage 6 — Lean statement alignment

Generate `challenge.lean`, `STATEMENT_EXPLANATION.md`, and `CLAIM_ALIGNMENT.json`.
A separate auditor compares the Lean proposition to the frozen claim contract. Failure sends
the statement back for revision, not directly to proof implementation.

## Stage 7 — Codex formalization

Codex receives a bounded task, the accepted manuscript/proof, formalization instructions, and
Lean compiler feedback. Each iteration must save:

- prompt;
- Codex JSONL/stdout/stderr;
- file diff;
- commands run;
- Lean diagnostics;
- iteration verdict.

Stop on success, infeasibility discovered, iteration/budget limit, or repeated no-progress.

## Stage 8 — Deterministic verification

Run clean Lean checks and scans. Record exact commands and outputs. The verifier must compare
the final theorem statement hash with the approved `challenge.lean` statement hash.

## Stage 9 — Report

Generate `REPORT.md`, `report.json`, and `verification_certificate.json`. The report must be
truthful even when research or formalization fails.
