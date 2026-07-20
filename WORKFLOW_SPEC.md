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
- web search;
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

## Stage 2 — Adaptive research

### Initial round

A coordinator creates at least four materially distinct assignments. Suggested roles are not
fixed quotas; examples include direct proof, alternative structural formulation, hostile
counterexample search, literature/known-theorem mapping, computation, and formalization-aware
lemma decomposition.

If the compiler found that existing literature resolves the target, the portfolio emphasizes
independent source verification, hypothesis matching, proof reconstruction, and formalization.
Known results must remain labeled as known rather than novel.

### Later rounds

The coordinator receives only visible worker reports and the current registry. It returns:

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

## Stage 4 — Manuscript and bibliography

The manuscript writer receives only the frozen accepted proof package, claim contract, audit
reports, verified source ledger, and manuscript prompt. It must not silently change the
result. It must include a Statement of AI Usage disclosing ASCEND with GPT 5.6 and cite both the
canonical ASCEND GitHub repository and ASCEND whitepaper arXiv preprint.

The bibliography verifier runs in a fresh context with web search. It checks every item and
every substantive related-work characterization. It creates a correction plan. The writer
may regenerate the manuscript, after which verification runs again. Limit cycles by config.

Only a fully verified bibliography may proceed.

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
