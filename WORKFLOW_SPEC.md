# Workflow Specification

The CLI reports sparse `ASCENSION n` progress at the stage boundaries below. During adaptive
research, `ASCENSION 2` starts or resumes the durable logical coordinator and `ASCENSION 3`
announces management of its live worker pool. Candidate-audit packaging is reported separately.
These updates are operational milestones only; model reasoning and per-call details are not
streamed to the user.

## Stage 0 — Intake

Inputs: problem file, optional framework override, config, CLI flags.
Outputs: original problem, normalized problem, hashes, environment snapshot, initial state.

Load and validate the project graph, reconcile permitted human edits, map the source file to one
stable problem ID, and create an idempotent run node. Conflicting manual edits stop the run.

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

Before every non-replayed coordinator request, query the graph frontier. Coordinator assignments
name stable target IDs and are materialized as graph task nodes before worker reservations. Each
worker request freezes a bounded graph context and base revision. A worker may propose a typed
patch but cannot write the vault. Once its raw report and independent source verification are
durable, the deterministic graph service validates/merges the patch and distilled report, then
publishes the worker event. Valid partial and blocked results therefore survive interruption.

Research is a nested orchestration boundary. The deterministic outer workflow starts or resumes
one application-owned logical research coordinator and gives it the complete, unabridged compiled
research prompt and exact claim contract. This is ASCEND's closest reproducible analogue of giving
the main prompt to a GPT 5.6 Sol Ultra research session: `Ultra` is not treated as an API setting.
The Responses API defaults the coordinator to `gpt-5.6-sol` pro/max and research workers to
`gpt-5.6-sol` pro/xhigh. The default Codex path selects the same model and requests max coordinator
effort and xhigh worker effort; `reasoning.mode = "pro"` is a Responses API control, not a separate
Codex CLI setting in ASCEND. Models, modes, and efforts remain configurable where supported by the
selected backend.

The coordinator is logically continuous but need not be one indefinitely open provider call.
ASCEND may use fresh calls while presenting the same logical actor's canonical durable checkpoint
and evidence. Hidden provider memory and a surviving provider conversation are never required for
correctness.

### Bootstrap portfolio

The first coordinator decision creates sixteen independent assignments by default, spanning at
least four materially distinct approach families. Suggested roles are not fixed quotas; examples
include direct proof, alternative structural formulation, hostile counterexample search,
literature/known-theorem mapping, computation, and formalization-aware lemma decomposition.

If the compiler found that existing literature resolves the target, the portfolio emphasizes
independent source verification, hypothesis matching, proof reconstruction, and formalization.
Known results must remain labeled as known rather than novel.

### Completion-driven coordinator loop

ASCEND runs a durable event loop with no round barrier:

1. Validate and persist each coordinator decision and assignment before admitting work.
2. Admit useful queued assignments while the applicable concurrency, backend, and budget ceilings
   have capacity. The live pool starts from the diverse sixteen and may grow or refill to 32 active
   workers by default. `maximum_pending_assignments` limits the total open set—queued plus
   running—to 32 by default, while the concurrency setting limits the active subset.
3. When any worker finishes, atomically preserve its complete raw `ResearchWorkerReport`, hash and
   per-assignment source verification. Atomically checkpoint the scheduler transition with the
   event in its pending-event write-ahead field, create the next immutable event file under
   `research/events/`, clear the pending field, and refresh the derived mailbox view.
4. Activate the coordinator on newly useful events without waiting for every other active worker.
   Near-simultaneous completions may be delivered together, but coalescing must not become a batch
   synchronization barrier.
5. Persist the coordinator's acknowledgement cursor and next decision, update the registry, and
   immediately retire, redirect, or refill work as directed.

On every activation, the coordinator receives the original complete prompt and claim contract,
all unacknowledged mailbox events, the current assignment lifecycle state, the approach registry,
and all audit repair obligations. It also receives the complete raw reports relevant to those
events and durable references to every earlier raw report. `research/continuity.json` remains a
derived navigation view that explicitly classifies promising routes, partial results, refuted
directions and counterexamples, blocked routes and exact gaps, dependencies, and prior
directives; it is never a lossy replacement for the reports or immutable event ledger.

Each decision may add assignments, retire or redirect work, request hostile checks or lemma
completion, recommend candidate packaging, or recommend a budget-aware stop. Workers must return
concrete formal content using `ResearchWorkerReport`. A blocked route must identify the exact
missing statement and any counterexample found.

There is no cumulative research-worker ceiling. Total-open-assignment, active-concurrency,
coordinator-decision, model-call, cost, token, and optional active-wall-clock limits remain
independent controls. None introduces a wait-for-all barrier. Explicit Codex call-count limits
remain available but are unset by default. Public scheduler controls are
`research.maximum_pending_assignments` (default 32 total open assignments) and
`research.maximum_coordinator_decisions` (default 256). Legacy round controls are converted to a
scaled decision budget only; they do not change event-driven execution.

## Stage 3 — Candidate proof and audits

A candidate package includes theorem statement, definitions, lemma dependency graph, full
proofs, imported theorems, exceptional cases, parameter bookkeeping, unresolved items, and an
explicit required classification of whether the claim is quantitative or algorithmic. The
foundational auditor independently checks that classification; falsely clearing it is blocking,
so the packager cannot suppress an applicable complexity audit.

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

A repairable verdict returns its complete audit reports and exact obligations directly to the
live research coordinator; it does not wait for or create another research round.

When a worker returns `candidate_complete`, or the coordinator requests packaging, ASCEND pauses
admission of new research workers and packages that specific proof immediately. It does not wait
for unrelated active workers. A package that exposes unresolved proof steps fails closed before
independent judging; every structurally complete package immediately runs every mandatory
independent audit plus the final judge. Reports that finish while admission is paused are durably
appended to the mailbox and remain available to the coordinator.

Acceptance stops the research scheduler, cancels work that no longer needs to start, and advances
the workflow. If the gate does not pass, ASCEND appends the full failed-audit reports, judge
verdict, and exact repair obligations as high-priority mailbox events; it then reactivates the
coordinator immediately and resumes/refills admission. This feedback path does not wait for the
rest of a former batch. With the Responses API, independent auditors use fresh `gpt-5.6-sol`
pro/xhigh contexts by default and the research final judge uses pro/max. The Codex path requests
xhigh auditor effort and max final-judge effort for the same model; all are configurable within
backend capabilities. A worker's status alone is never treated as proof verification.

The optional `--time-limit-minutes` allowance covers active execution across all stages and is
carried across resume attempts. It bounds in-flight model calls as well as pre-call and stage
boundary checks; paused time between CLI invocations is not counted. No wall-clock limit is
applied by default.

## Stage 4 — Manuscript and bibliography

The manuscript writer receives only the frozen accepted proof package, claim contract, audit
reports, verified source ledger, dependency-ordered accepted graph slice, and manuscript prompt.
It must not silently change the
result. It must include a Statement of AI Usage disclosing ASCEND with GPT 5.6 and cite both the
canonical ASCEND GitHub repository and ASCEND whitepaper arXiv preprint.

After the stage, graph nodes record claim/section and source/BibTeX mappings plus manuscript
artifact nodes. Existing research, bibliography, and LaTeX gates remain authoritative.

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

Lean receives accepted statement/proof/formalization nodes from the graph. On completion, the
deterministic service creates a separate formalization node linked to the exact claim statement
version/hash, theorem declaration, source-file hash, toolchain, mathlib revision, build result,
and axiom report. Claim promotion to `lean_verified` occurs only for a passed deterministic
verification with aligned hashes and statement audit.

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

Update the persistent run node with the strongest result, unresolved obligations, and terminal or
incomplete status. Report metadata links the stable problem ID, current revision, Home note, and
derived index without folding mutable cross-run graph files into the run certificate.
