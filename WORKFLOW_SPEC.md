# Workflow Specification

The CLI reports sparse `ASCENSION n` progress at the stage boundaries below. During adaptive
research, `ASCENSION 2` starts or resumes the durable logical coordinator and `ASCENSION 3`
announces management of its live worker pool. Candidate-audit packaging is reported separately.
These updates are operational milestones only; model reasoning and per-call details are not
streamed to the user.

## Stage 0 — Intake

Inputs: problem file, optional framework override, config, CLI flags.
Outputs: original problem, normalized problem, hashes, environment snapshot, initial state.

Derive the default graph name from the problem filename stem, or validate an explicitly selected
existing graph supplied with `--knowledge-graph`. Load and validate only that named graph,
reconcile permitted human edits, map the source file to one stable problem ID, and create an
idempotent run node. Freeze the graph name in run state for resume. Conflicting manual edits stop
the run; different files never share a graph unless the user explicitly requests reuse.

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
`needs_clarification` with a reason and focused questions. MATEK persists that request, skips
all research/manuscript/Lean stages, writes the final report, and asks the user to revise the
problem file and start a new run.

Otherwise it returns a full adapted prompt plus a formal claim contract, a source ledger, and a
literature classification: `unknown`, `no_exact_match_found`, `partially_resolved`, or
`fully_resolved`. Placeholder validation flags only strong editorial markers; ordinary
mathematical bracket notation, citations, links, code, and LaTeX are protected. MATEK persists
the compiled result and validation diagnostics before attempting one bounded, sentence-only
repair. An unresolved marker blocks the workflow only in the exact target or success criterion;
an optional sentence is removed with a recorded warning. Partial/full resolution claims require
verified sources and an exact statement-and-hypothesis comparison.

Before admission, deterministic target alignment interprets each clause from its explicit key and
records a hash-bound check. It blocks only an explicit high-confidence contradiction: reversed
symbolic quantifiers or polarity, opposing qualifiers, a changed structured numeric value, or
drift in a compact formal comparison. Missing token overlap in generated prose is nonblocking;
negative examples such as “no `+β` is permitted” are never converted into positive requirements.
This guard is deliberately narrow because the same frontier compiler authors the statement and
contract. Later research, manuscript, and Lean gates still audit the actual mathematical claim.

Every source states whether it identifies the target or supports a literature claim. Failure to
verify target-identification evidence pauses for clarification. Failure to resolve literature-only
evidence instead preserves the source-verification report, marks the source and dependent claims
unverified, qualifies or removes those claims from the compiled prompt, and continues research.
Both `export.arxiv.org` and `arxiv.org/abs/<id>` are deterministic arXiv resolution routes.
Quarantined literature evidence remains ineligible for candidate acceptance and bibliography use.

The adapted prompt front-loads a compact research-mandate snapshot modeled at a high level on
the public Cycle Double Cover prompt: exact target, boundary conventions, near-misses, adaptive
independent search, persistence, adversarial review, search policy, and a proof-only completion
condition. The longer framework then expands each item into MATEK's auditable protocol.

## Stage 2 — Adaptive research

Before every non-replayed coordinator request, query the graph frontier. Before initial
delegation, an existing graph is explicitly marked as requiring review, and the coordinator uses
its overview, prior results, failures, gaps, audits, and tasks to shape the portfolio. Coordinator
assignments name stable target IDs and are materialized as graph task nodes before worker
reservations. Each worker request freezes a bounded graph context and base revision. A worker
returns a schema-v2 typed scientific report and cannot author persistence identities, relation
directions, status promotions, or vault writes. Once its raw report and independent source
verification are durable, the deterministic graph service constructs and commits the application-
owned admission plan, then publishes the worker event. Valid partial and blocked results therefore
survive interruption. The scientific report transaction and graph admission are separate: an
admission failure produces a durable warning while the proof attempt, counterexample, or partial
result remains available. MATEK binds graph content hashes from the frozen revision rather than
trusting hashes supplied by a worker.

Every new coordinator payload includes an activation context. It distinguishes bootstrap,
existing-graph bootstrap, continuation, and process resume; says explicitly that no provider
conversation memory may be assumed; and identifies the current and previously observed graph
revisions. The coordinator reconstructs the current branch map from the canonical scheduler,
events, registry, continuity state, audits, and graph frontier, then includes the reviewed
revision in its decision rationale. This applies to same-run resume as well as graph reuse across
runs.

Graph-scoped assignments fail closed unless they name at least one existing live target belonging
to the selected problem. MATEK never silently replaces an unknown, cross-problem, tombstoned, or
non-research target with the main claim. A top-level route normally targets the main claim; a
continuation or sub-branch targets the claim, proof, approach, counterexample, source, audit, or
task that actually defines its scope.

Research is a nested orchestration boundary. The deterministic outer workflow starts or resumes
one application-owned logical research coordinator and gives it the complete, unabridged compiled
research prompt and exact claim contract. This is MATEK's closest reproducible analogue of giving
the main prompt to a GPT 5.6 Sol Ultra research session: `Ultra` is not treated as an API setting.
The Responses API defaults the coordinator to `gpt-5.6-sol` pro/max and research workers to
`gpt-5.6-sol` pro/xhigh. The default Codex path selects the same model and requests max coordinator
effort and xhigh worker effort; `reasoning.mode = "pro"` is a Responses API control, not a separate
Codex CLI setting in MATEK. Models, modes, and efforts remain configurable where supported by the
selected backend.

The coordinator is logically continuous but need not be one indefinitely open provider call.
MATEK may use fresh calls while presenting the same logical actor's canonical durable checkpoint
and evidence. Hidden provider memory and a surviving provider conversation are never required for
correctness.

### Bootstrap portfolio

The first coordinator decision creates eight independent assignments by default, spanning at
least four materially distinct approach families. Suggested roles are not fixed quotas; examples
include direct proof, alternative structural formulation, hostile counterexample search,
literature/known-theorem mapping, computation, and formalization-aware lemma decomposition.

If the compiler found that existing literature resolves the target, the portfolio emphasizes
independent source verification, hypothesis matching, proof reconstruction, and formalization.
Known results must remain labeled as known rather than novel.

### Default hierarchical workers

With the default Codex hierarchical mode, the
coordinator still creates and observes the durable first-level assignments, while each
first-level research-worker process receives Codex's collaboration tools and a configured
per-session nested-thread limit. The defaults, equivalent to
`--num-first-level-agents 8 --subagents-per-agent 4 --max-concurrent-agents 24`, create eight
independent bootstrap assignments and admit up to four hierarchical MATEK workers at once. Each
active worker may use up to four nested agents for bounded parts of its assignment. MATEK reserves
one parent slot plus the complete child allowance because Codex descendant activity is internal
to the parent session. Nested agents inherit the parent's
sandbox and search policy. The first-level worker must tell its children not to delegate further,
validate their work, and return one normal scientific report. MATEK checkpoints that report and
the aggregate provider usage at the existing worker boundary.

Both tiers receive the resolved limits. `--flat` or a zero nested limit gives workers the regular
subagent contract and leaves Codex nested-agent controls disabled. The Responses API adapter
visibly resolves to this portable flat path because it has no nested-agent tool. Nested outputs
are never independent proof audits and never relax candidate acceptance.

### Completion-driven coordinator loop

MATEK runs a durable event loop with no round barrier:

1. Validate and persist each coordinator decision and assignment before admitting work.
2. Admit useful queued assignments while the applicable concurrency, backend, and budget ceilings
   have capacity. The live pool starts from the diverse eight and refills up to the derived active
   first-level limit—four by default—with up to four nested agents per worker. The high
   `max_pending_assignments` safety ceiling limits the total open set—queued plus running—to
   1,024 by default, while concurrency limits the active subset.
3. When any worker finishes, atomically preserve its complete raw `ResearchWorkerReport`, hash and
   per-assignment source verification. Atomically checkpoint the scheduler transition with the
   event in its pending-event write-ahead field, create the next immutable event file under
   `research/events/`, clear the pending field, and refresh the derived mailbox view.
4. Activate the coordinator on newly useful events without waiting for every other active worker.
   Near-simultaneous completions may be delivered together, but coalescing must not become a batch
   synchronization barrier.
5. Persist the coordinator's acknowledgement cursor and next decision, update the registry, and
   immediately retire, redirect, or refill work as directed.

Provider, schema, and worker-execution failures are isolated to their assignments. `collect_tasks`
returns accepted reports, candidate IDs, and structured issues; it never discards concurrent
successes by raising the first recoverable worker exception. MATEK records
`worker_execution_failed`, permits one bounded repair generation, and then lets the coordinator
reassign or retire the route. Integrity failures still stop the scheduler.

On every activation, the coordinator receives the original complete prompt and claim contract,
all unacknowledged mailbox events, the current assignment lifecycle state, the approach registry,
and all audit repair obligations. `CoordinatorContextBuilder` measures the final serialized
provider input against an 800,000-character default ceiling. It deterministically prioritizes new
and candidate-producing reports, structured summaries, and complete requested evidence; repetitive
issues are aggregated with counts, affected assignments, and paths. Omitted raw reports remain
addressable by stable ID, validated relative path, and frozen SHA-256. Codex may inspect those
paths, while API coordinators request a bounded set for the next activation.

Every context has an immutable manifest recording its event cursor, mode, inclusion reasons,
omissions, per-section measured characters, token estimate, reserved headroom, and hash. Compact
mode targets at most 95% of the configured ceiling and at least 40,000 characters of headroom.
Schema-v3 contexts order the exact target and contracts first, reserve events/open work and
explicit retrievals before current full reports and ranked summaries, and keep current deltas,
selected evidence, and a deterministic decision brief near the end. They record scientific graph
scores, frontier categories, section positions, unused headroom, deduplicated characters, and the
unrequested full-graph section size. That section defaults to a 120,000-character ceiling.
Only new/current/candidate/audit/requested catalog entries remain inline; one authenticated
descriptor addresses the exhaustive on-disk catalog. Graph transport contains one compact
root/revision/index/count descriptor plus capped selected node summaries. Provider
`input_too_large` responses produce a distinct, smaller request rather than an identical retry.
If ordinary compact state cannot fit, indexed mode makes every cumulative section optional and
independently capped. `MANDATORY_CONTEXT_TOO_LARGE` is reserved for the exact prompt/claim plus
provider instructions, output contract, and envelope; repeated provider rejection has a separate
retriable diagnosis. `research/continuity.json` remains a derived navigation view, never a lossy
replacement for reports or the immutable event ledger.

Candidate packaging, contradiction resolution, and retirement of a promising branch cite their
supporting artifact or graph-node IDs. If a cited item is only summarized or omitted, MATEK turns
the activation into retrieval-only work and defers the consequential action until the hash-bound
full evidence is visible.

Each decision may add assignments, retire or redirect work, request hostile checks or lemma
completion, recommend candidate packaging, or report an actual budget/resource boundary. The
exact frozen claim contract is the only terminal scientific target. Reductions, special cases,
weaker variants, additional-hypothesis results, reformulations, and isolated lemmas are retained
as intermediate evidence only. A scientific no-progress or reduction-based stop recommendation
is declined, persisted as `coordinator_scientific_stop_declined`, and returned to the coordinator
with an exact recovery obligation. Research continues until the exact claim is accepted, exactly
refuted, or an explicit resource/provider boundary is reached. Workers must return concrete formal
content using `ResearchWorkerReport`; a blocked route must identify the exact missing statement
and any counterexample found.

The approach registry and graph preserve one entry per assignment branch rather than collapsing
all work sharing an approach-family label. Blocked and ruled-out branches retain their exact gap,
counterexamples, and reopen condition even when another branch in the same family remains
productive. Automatically admitted worker counterexamples are linked to their approach branch,
not promoted to refutations of the main theorem. A claim-level refutation requires a complete
typed main-scope scientific result matching the frozen target and a deterministic, independently
recomputed verifier/hostile-falsifier gate; only application code may add the main `REFUTES` edge.

There is no cumulative research-worker ceiling. Total-open-assignment, active-concurrency,
coordinator-decision, model-call, cost, token, and optional active-wall-clock limits remain
independent controls. None introduces a wait-for-all barrier. Explicit Codex call-count limits
remain available but are unset by default. Public scheduler controls are
`research.max_pending_assignments` (default safety ceiling 1,024) and
`research.max_coordinator_decisions` (default safety ceiling 100,000). Legacy round controls are converted to a
scaled decision budget only; they do not change event-driven execution. Initial workers and later
refills use the same worker settings and across-tier capacity. Web search is enabled for both by
default and disabled only by the frozen global `--no-web-search` policy.

## Stage 3 — Candidate proof and audits

A candidate package includes theorem statement, definitions, lemma dependency graph, full
proofs, imported theorems, exceptional cases, parameter bookkeeping, unresolved items, and an
explicit required classification of whether the claim is quantitative or algorithmic. The
foundational auditor independently checks that classification; falsely clearing it is blocking,
so the packager cannot suppress an applicable complexity audit. The package and every audit are
also bound to the exact-target policy: a proof of a reduced or weaker problem cannot pass even when
that result is mathematically valid in its own right.

Before packaging, MATEK validates every triggering report's acyclic `dependency_result_keys` DAG.
Every replay-backed computation must be in the transitive closure of a separate exact-main lemma or
reduction. With a knowledge graph active, the frozen support binding includes each closure result's
application-resolved premise edges and versions plus the computation derivation and canonical
manifest/replay artifact pair. An unrelated replay or a live obligation anywhere in that bound
slice stops the gate before a model audit.

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

When a worker returns `candidate_complete`, or the coordinator requests packaging, MATEK pauses
admission of new research workers and packages that specific proof immediately. It does not wait
for unrelated active workers. A package that exposes unresolved proof steps fails closed before
independent judging; every structurally complete package immediately runs every mandatory
independent audit plus the final judge. Reports that finish while admission is paused are durably
appended to the mailbox and remain available to the coordinator.

Each audit is an independent checkpoint. Its JSON, provider response ID, and hash are committed as
soon as that audit finishes. A failed audit is scientific feedback; a crashed or unavailable audit
is an execution/evidence issue that leaves the candidate in `AWAITING_AUDITS` and pauses the
workflow retriably. Resume launches only missing audits. The final judge is not called, and the
candidate cannot be accepted, until every frozen mandatory audit and every imported theorem is
independently verified.

Acceptance stops the research scheduler, cancels work that no longer needs to start, and advances
the workflow. If the gate does not pass, MATEK appends the full failed-audit reports, judge
verdict, and exact repair obligations as high-priority mailbox events; it then reactivates the
coordinator immediately and resumes/refills admission. This feedback path does not wait for the
rest of a former batch. With the Responses API, independent auditors use fresh `gpt-5.6-sol`
pro/xhigh contexts by default and the research final judge uses pro/max. The Codex path requests
xhigh auditor effort and max final-judge effort for the same model; all are configurable within
backend capabilities. A worker's status alone is never treated as proof verification.

The `--time-limit-minutes` allowance covers active execution across all stages and is
carried across resume attempts. It bounds in-flight model calls as well as pre-call and stage
boundary checks; paused time between CLI invocations is not counted. The default allowance is
900 minutes (15 active hours).

## Stage 4 — Manuscript and bibliography

The manuscript writer receives only the frozen accepted proof package, claim contract, audit
reports, verified source ledger, dependency-ordered accepted graph slice, and manuscript prompt.
It must not silently change the
result. It must include a Statement of AI Usage disclosing MATEK with GPT 5.6 and cite the
canonical MATEK GitHub repository plus an available local technical report. A missing canonical
whitepaper arXiv identifier is recorded as pending publication metadata, never guessed.

After the stage, graph nodes record claim/section and source/BibTeX mappings plus manuscript
artifact nodes. Existing research, bibliography, and LaTeX gates remain authoritative.

The bibliography verifier runs in a fresh context with web search. It checks every item and
every substantive related-work characterization. It creates a correction plan. Deterministic
presentation, bibliography, and LaTeX findings are fed back to the writer up to the configured
revision limit. Every draft and its validation is checkpointed; bibliography auditing and safe
LaTeX compilation run even when presentation checks need repair.

Only a fully verified bibliography may be promoted to publication-ready. An exhausted but safe
draft is retained as `DRAFT_WITH_WARNINGS`; claim drift, fabricated citations, unsafe output, or
irreparable LaTeX produce `PUBLICATION_BLOCKED`.

`--no-web-search` also disables MATEK's deterministic public-identifier HTTP resolver. Any
evidence that cannot be established from persisted provider metadata remains unavailable; gates
fail truthfully instead of treating offline status as verification. Consequently, a fully
search-free invocation is primarily intended for `--research-only` runs and cannot bypass the
verified-bibliography prerequisite for publication readiness.

Compile every safe draft deterministically. Undefined references, missing citations, compilation
errors, or bibliography mismatches become repair findings. Irreparable LaTeX after all configured
attempts blocks publication and downstream work; bibliography and layout defects do not block Lean.

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

Report scientific state independently from execution state. For example, an unavailable mandatory
audit is `Scientific: CANDIDATE_AWAITING_AUDIT` and `Workflow: PAUSED_RETRIABLE`. The report derives
the strongest candidate, completed workers, committed audit progress, missing checks, issue trace
paths, and exact resume action from the canonical scheduler checkpoint.

Update the persistent run node with the strongest result, unresolved obligations, and terminal or
incomplete status. Report metadata links the graph name and selection mode, stable problem ID,
current revision, Home note, and derived index without folding mutable cross-run graph files into
the run certificate.
