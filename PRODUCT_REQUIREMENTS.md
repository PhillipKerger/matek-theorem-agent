# Product Requirements

## Product summary

**MATEK** (**Multi-Agent Theorem Exploration through Knowledge-Graph Memory**) is a local CLI that accepts a research-level mathematical problem,
compiles it into a rigorous research prompt, runs an adaptive multi-agent research process,
audits any candidate solution, writes a publication-oriented LaTeX manuscript, attempts Lean
formalization, and generates a reproducible final report.

## Personas

- A mathematician with a problem statement and an existing Lean project.
- A researcher who wants a research-only run without Lean.
- A maintainer evaluating and improving a reusable mathematical research methodology.

## Functional requirements

### FR-0 Model backend and authentication

- Default to the official Codex CLI and reuse its saved **Sign in with ChatGPT** authentication.
- Do not require `OPENAI_API_KEY` for the default Codex path.
- Preserve the direct OpenAI Responses API as an advanced, explicitly selected backend.
- Resolve backend selection from CLI, `MATEK_BACKEND`, project configuration, then the `codex`
  built-in default, and freeze the result in run state.
- Never silently fall back from Codex to API billing.
- Feature-detect installed Codex capabilities and determine only a coarse authentication class
  with `codex login status`; never inspect credential files.

### FR-1 Problem intake

- Accept UTF-8 `.md` or `.txt` input.
- Accept concise problem descriptions when they uniquely identify the mathematical setting,
  target, and essential constraints; do not require a user-supplied literature review or proof
  plan.
- Preserve the original bytes and a normalized copy.
- Record the normalized source bytes' SHA-256, timestamp, CLI arguments, config snapshot, and
  tool versions. This source hash is the durable identity of the problem's canonical target.
- Reject empty input and provide a useful diagnostic.

### FR-2 Framework compilation

- Load `resources/prompts/research_prompt_framework.txt` verbatim.
- Expected bundled SHA-256: `bd724294a261f4bc2e5da2191813e40c1340bc6ee039c753cb5c60276e7a512c`.
- Use xhigh reasoning and web search by default.
- Provide an explicit `--no-web-search` override that disables search across all model stages
  and MATEK's identifier-resolution HTTP calls without weakening citation gates.
- Produce both a complete adapted prompt and structured metadata.
- Front-load a compact research-mandate snapshot containing the exact target, boundary cases,
  insufficient outcomes, adaptive independent search, persistence, adversarial review, public
  search boundary, and audited completion condition before the expanded protocol.
- Fill every applicable bracketed placeholder; explicitly remove or mark inapplicable
  optional branches rather than leaving template placeholders unresolved.
- Verify literature/background claims used in the compiled prompt.
- Classify whether the exact target is unknown in the checked literature, has no exact match
  found, is partially resolved, or is fully resolved by an existing theorem. Verify any claimed
  match against authoritative sources and compare its exact hypotheses and conclusion.
- If the exact target is already known, preserve that provenance and prohibit unsupported novelty
  claims while allowing proof reconstruction, exposition, and formalization.
- If the input admits several plausible targets, select the most likely conventional reading,
  persist the exact assumed theorem and alternatives, warn the user, and start research. A useful
  clarification question is never by itself a research-blocking condition.
- Save source citations and search evidence separately from the prompt text.
- Classify sources as target-identification or literature-support evidence. Unavailable
  literature-only evidence must be preserved and quarantined while research continues; it may not
  support acceptance or the final bibliography. Unavailable target-identification evidence is
  downgraded to an unverified lead; the explicit compiled or assumed theorem remains authoritative.
- Resolve arXiv identifiers through both `export.arxiv.org` and `arxiv.org/abs/<id>`.
- Before research, inspect the compiled statement against every applicable claim-contract clause
  and persist typed diagnostic warnings in `prompts/target_alignment.json`. Heuristic, regex, and
  legacy extractors never block research. When they suggest a possible material change, request
  one short context-aware materiality review. Block only when that review returns a structured
  `CONFIRMED_CONFLICT` naming the affected contract clause; reviewer uncertainty, failure, or
  malformed output is warning-only. Randomized algorithms may have pathwise deterministic
  feasibility, deterministic preprocessing or tie-breaking, and proofs conditioned on a realized
  seed; none of those facts makes the algorithm deterministic-only. Negation, clause scope,
  temporal order, and equivalent mathematical formulations must be considered by the reviewer.
- Bind the first aligned statement, canonical contract, and compiled prompt for a normalized
  source hash in `.matek/knowledge/<graph-name>/target-registry.json`. Later runs with the same
  source hash reuse those canonical bytes while refreshing literature separately. A new aligned
  wording with the unchanged contract is recorded as a cosmetic paraphrase and does not replace
  those bytes. Once registry hashes validate, later heuristic semantic revalidation is diagnostic
  and warning-only; it must not reclassify the immutable target or run state as corrupt.
  Contract-changing drift fails closed unless the user explicitly runs with
  `--migrate-target REASON`; that versioned migration requires confirmation unless `--yes` is
  present, persists its authorization through resume, emits the target-migration event, and
  invalidates affected evidence.

### FR-3 Adaptive research

- Approximate the behavior of giving the complete main research prompt to a GPT 5.6 Sol Ultra
  research session through explicit application-level orchestration. `Ultra` is a product/session
  label, not a model-backend or Responses API primitive. The Responses API adapter defaults the
  logical coordinator to `gpt-5.6-sol` with `reasoning.mode = "pro"` and
  `reasoning.effort = "max"`, and workers to the same model and mode with
  `reasoning.effort = "xhigh"`. The default Codex adapter requests `gpt-5.6-sol` with `max`
  coordinator effort and `xhigh` worker effort through Codex CLI's reasoning-effort control; it
  does not present the Responses API `reasoning.mode` field as a Codex setting. Every role remains
  configurable within the selected backend's capabilities.
- Start or resume one durable logical research coordinator with the complete, unabridged compiled
  prompt and exact claim contract. Provider calls may use fresh contexts; correctness must come
  from application-owned state rather than a surviving provider conversation.
- Have the coordinator create eight initial assignments by default, spanning at least four
  materially different approaches unless the configured budget is lower.
- Keep initial workers independent; do not reveal the favored route to all workers.
- Use Codex hierarchical mode by default. The user configures the bootstrap first-level portfolio
  (`num_first_level_agents`, eight by default), the per-worker nested-agent allowance
  (`subagents_per_agent`, four by default), and one conservative across-tier research-agent
  capacity (`max_concurrent_agents`, 24 by default). Give all three limits and the derived
  first-level concurrency to the coordinator and every first-level worker. Because Codex child
  activity is internal to each worker session, reserve one parent slot plus the complete child
  allowance for every admitted hierarchical worker; the defaults therefore admit four first-level
  workers concurrently while retaining all eight bootstrap assignments. A worker with a positive allowance may
  delegate bounded independent subtasks one tier deep, but must check and synthesize them into its
  own `ResearchWorkerReport`; its children cannot bypass MATEK checkpoints or acceptance gates.
  A zero nested allowance makes the worker a regular subagent and the prompt must say so without
  implying that nested delegation is available. Preserve explicit flat mode. Because the API
  adapter has no nested-agent tool, visibly resolve API execution to the backend-portable flat
  path rather than emulating unavailable delegation.
- Run research as a completion-driven event loop rather than fixed rounds. Atomically preserve
  every assignment and full raw worker report, then atomically write one immutable zero-padded
  completion-event file and refresh the materialized mailbox snapshot. Activate the coordinator
  as useful events arrive.
- Build every coordinator activation deterministically under a hard default limit of 800,000
  characters measured on the final serialized provider input. Always include the complete main
  prompt and claim contract, then prioritize new events, candidate/audit/recovery state, compact
  assignment state, structured report summaries, and relevant full reports. Never truncate JSON
  or mathematical prose bytewise.
- Keep every raw report and immutable event authoritative on disk. Compact contexts provide
  authenticated artifact and graph-node references with stable IDs, validated relative paths,
  revisions, and SHA-256 hashes. Codex may read those paths; backends without filesystem access
  may request a bounded evidence set for the next activation.
- Rank graph evidence by explicit retrieval, unresolved contradictions/dependencies, active
  high-value tasks, candidate proofs, target-near unresolved claims, recent diverse evidence, and
  finally blocked/refuted/history. Use graph distance, status, recency, and approach diversity
  before stable ID; retain frontier categories and score components in the context manifest.
- Treat the provider-input bound as a ceiling rather than a fill target. Unrequested full graph
  nodes default to a separate 120,000-character serialized section cap; explicit requests are
  exempt from that section cap but not the unchanged overall input ceiling.
- In compact mode inline catalog entries only for new/current/candidate/audit/requested evidence.
  Represent the exhaustive catalog by its validated relative path, SHA-256, total count, and
  artifact-kind counts. Replace the full graph memory view with graph root, revision, index path,
  node/edge counts, and retrieval instructions; inline only the already selected bounded graph
  summaries so graph state is not duplicated.
- Use bounded typed graph digests for exact statements/results, mechanisms, assumptions,
  dependencies, gaps, counterexample scope, target relation, typed relations, and hash-bound
  provenance. Deduplicate substantive exact repeated content across evidence and navigation views,
  retaining one canonical full representation and authenticated references.
- Preflight before starting a provider process. Persist each context manifest and any omissions.
  A provider `input_too_large` result must reduce the measured budget and create a distinct compact
  request. If cumulative scheduler state cannot fit, automatically rebuild an indexed context
  containing the exact prompt/claim, live controls, open work, newest events, bounded scientific
  summaries, and authenticated ledger/graph/artifact references. Cap every optional serialized
  section and reserve at least 5% or 40,000 characters of headroom. Repeatedly prune the
  lowest-priority optional entries and remeasure. Reserve `MANDATORY_CONTEXT_TOO_LARGE` for the
  exceptional case where the exact prompt/claim plus provider instructions, output contract, and
  envelope cannot fit; report repeated provider rejection separately.
- Refill useful work dynamically after completions instead of waiting for a batch barrier. Use
  1,024 total open assignments (queued plus running) as a high default safety ceiling. Permit up
  to the derived first-level limit of that open set to be active research workers—four under the
  default 24-slot capacity and four-child allowance—subject to scientific-phase, backend, and
  budget limits. The initial portfolio still contains eight independent assignments. Initial workers and
  later refills share that pool and use web search by default; only the explicit global
  `--no-web-search` policy disables search for them.
- Maintain an approach registry containing mechanism, result, assumptions, bottleneck,
  counterexamples, dependencies, and status.
- Require new workers to return only schema-v2 `ResearchWorkerReport` mathematics:
  `ScientificResult` records, explicit obligations, a source ledger, computation declarations,
  and a branch outcome. Same-report premises use an acyclic `dependency_result_keys` graph; stable
  node IDs are reserved for graph records that predate the report. Workers do not return
  `GraphPatch`, graph revisions, status promotions, or relation directions. MATEK validates the
  report and constructs graph admission deterministically from application-owned run, assignment,
  task, approach, and source identity.
- Reserve `kind = "definition"` for dependency-free, branch-scoped explicit notation
  declarations (`Define …`, `Let … denote …`, `… is defined as …`, or `:=`). Mathematical
  assertions and imported facts use claim-bearing result kinds and remain subject to audit.
- Drive durable `explore`, `consolidate`, `bottleneck`, `adversarial_audit`, and `synthesize`
  scientific phases from audited progress, repeated gaps, assignment similarity, and the current
  minimal open cut. Screen exact duplicate assignments for merge and near-duplicate mechanisms
  for redirection. Select one durable exact cut obligation per bottleneck coordinator portfolio,
  rotate prover, falsifier, small-case-computation, transfer-auditor, and synthesizer roles across
  activations, and retire still-queued work when the phase changes. Synthesis is serialized and may
  cite only audited premises. Persist the phase state and configure thresholds/concurrency under
  `[research.scientific_phase]`.
- Give every worker a private `0700` workspace beneath its run. Collect only declared regular
  files under quotas into a run-local content-addressed store, reject traversal, symlinks,
  hardlinks, special or undeclared files, and independently replay computation certificates only
  with attested filesystem confinement and networking disabled. The current trusted replay path is
  Docker; native execution is refused as an unsafe replay backend. Unreplayed computations remain
  experiments outside the canonical proof ledger, while a successful replay creates only a
  proposed derivation that still requires mathematical/domain audit.
- Support cost, token, active wall-clock, total-open-assignment, concurrency,
  coordinator-decision, and explicit call-count limits without turning any limit into a
  synchronization barrier.
- Name the primary scheduler controls `research.max_pending_assignments` and
  `research.max_coordinator_decisions`. Migrate legacy fixed-round settings and older verbose
  scheduler names, including the former first-level-only `maximum_concurrent_agents`, and
  `--max-rounds` into scaled decision budgets for compatibility without restoring round
  semantics.
- Do not impose a cumulative research-worker count ceiling. Do not impose a global Codex
  call-count limit by default; retain explicit configurable limits.
- Preserve the exact user claim as the only terminal scientific target. Reductions, special
  cases, strengthened hypotheses, reformulations, and isolated lemmas may be retained as useful
  intermediate results, but they are never terminal successes or accepted substitutes. A
  coordinator recommendation to stop merely because the exact target remains difficult shall be
  declined and returned as a durable recovery obligation. Continue until the exact claim is
  accepted, exactly refuted, or an explicit resource/provider boundary forces a truthful stop or
  retriable pause.
- Persist `research/coordinator/state.json` as the canonical atomic scheduler checkpoint. Use its
  pending-event field as a write-ahead record: checkpoint the transition and proposed event first,
  create the immutable zero-padded event evidence, then clear the pending field. Validate the
  checkpoint against immutable coordinator decisions, full raw reports, per-assignment source
  verification, event hashes, and candidate/audit evidence on resume. Treat the mailbox,
  assignment files, registry, and continuity view as materialized navigation/delivery views; the
  continuity view separates promising routes, partial results, ruled-out directions and
  counterexamples, blocked routes and exact gaps, dependencies, prior directives, and audit repair
  obligations. Ordinary resume must fail truthfully if the canonical scheduler checkpoint is
  missing or invalid rather than claiming it can be reconstructed from evidence alone.
- Nominate only uniquely admitted, gap-free, non-main intermediate results that explicitly reduce
  the current uncapped smallest open cut. Run a fresh blind verifier and independent falsifier on
  the exact statement, proof steps, dependency versions/hashes, and source artifacts; omit origin
  confidence and desired-verdict fields. Persist each response independently and resume only the
  missing role. A passing gate promotes a reusable intermediate claim/derivation to
  `audit_passed`, but cannot accept the main target, authorize a manuscript, or satisfy the main
  research gate.
- Persist categorized `integrity`, `execution`, `evidence`, `scientific`, and `resource` issues
  with trace paths and recovery obligations. Only integrity/security/state-corruption and
  immutable-artifact failures hard-stop; recoverable worker failures receive one bounded repair
  generation before coordinator reassignment or retirement.
- Produce a candidate proof package when the coordinator recommends it or a worker explicitly
  reports a full proof of the exact success criterion.
- When a candidate is triggered, pause admission of new research workers and run the full
  independent acceptance gate immediately without waiting for unrelated active workers. Preserve
  any reports that finish while admission is paused in the mailbox. Advance only if the candidate
  passes; otherwise append the complete failed-audit reports and exact repair obligations as
  high-priority events, reactivate the coordinator immediately, and refill the live pool.
- Expose a total active wall-clock limit for the complete run, persist elapsed time across resume,
  and use the remaining allowance to bound in-flight model calls. Default to 15 active hours.
  Other scheduler and iteration defaults should be high safety ceilings so time is the resource
  boundary ordinarily expected to stop a default run.

### FR-3A Persistent mathematical knowledge graph

- MATEK shall maintain named, project-scoped persistent graphs beneath
  `.matek/knowledge/<graph-name>/`, independent of run directories. The default graph name is
  derived from the source problem filename without its extension, so different problem files do
  not share memory by default and later runs of the same file reuse it.
- A user may explicitly attach a related or follow-up problem to an existing graph with
  `--knowledge-graph NAME`. Unknown explicit names shall fail instead of creating a graph, and the
  selected graph shall be frozen in run metadata for resume.
- Portable Markdown with flat typed YAML frontmatter is authoritative. The SQLite index is a
  disposable acceleration layer rebuildable from Markdown.
- The graph shall represent problem, definition, claim, proof attempt, derivation, obligation,
  proof, approach, task, counterexample, experiment, source, audit, formalization, run, artifact,
  and human-note nodes with immutable stable IDs and typed, constraint-checked relations.
- Epistemic and workflow statuses are separate. Only deterministic Lean verification may assign
  `lean_verified`; model reports cannot bypass research/audit gates.
- Before initial delegation, the coordinator shall review a problem-scoped graph overview and
  research frontier when prior graph memory exists, then use prior results, failures, gaps,
  audits, and tasks to shape graph-scoped assignments. Workers receive bounded context slices and
  return typed scientific reports rather than persistence mutations or writes to the shared vault.
- Every fresh-context coordinator activation, especially after `resume`, shall receive an explicit
  activation kind, the current and previously observed graph revisions, and a no-hidden-memory
  reconstruction instruction. It shall attest the exact reviewed graph revision in its decision
  and reconsider productive, blocked, and ruled-out branches, open tasks, audit obligations, and
  cross-branch proof-synthesis opportunities before assigning work.
- Every new graph-scoped assignment shall name at least one existing, problem-scoped, live stable
  target node. Unknown, cross-problem, tombstoned, or non-research targets shall fail validation;
  MATEK shall not silently substitute the main claim for an invalid target.
- Scientific admission shall preserve the raw report first, reject unknown dependency/target IDs,
  reject unknown, self-referential, or cyclic local-result dependencies, resolve local keys to
  stable node identities and real relation directions in ordinary Python, and atomically commit
  only application-owned mutations. Admission is idempotent on `(run_id, assignment_id,
  local_key, result_schema_version)` and rejects a changed payload under the same identity. A
  result with an exact gap creates a proof attempt and obligation, never a derivation. A gap-free
  lemma, reduction, or source fact creates a canonical claim, proof attempt, and proposed version-
  bound derivation regardless of branch disposition; a computation does so only after independent
  replay. `proposed_complete` enables nomination or exact-candidate packaging, not trust.
  Counterexamples remain branch-local unless an exact-contract audit authorizes a main-target
  refutation.
- The Markdown vault remains the complete searchable archive. A rebuildable integrity-protected
  projection at `ledgers/<problem-id>/canonical-ledger.json` contains only exact canonical claims,
  AND-premise derivations, OR alternative derivations, and typed obligations. Ambiguous legacy
  prose remains archive evidence and is never guessed into proof support.
- The frontier shall report trusted claims and the deterministic smallest known open cut between
  them and the main target. If bounded antichain search is capped, report that fact instead of
  claiming optimality. Dashboards shall show the main target and cut even when no trusted
  derivation reaches the target.
- Existing schema-v1 full snapshots shall remain immutable and readable. New revisions shall use
  content-addressed immutable node/edge blobs, delta manifests, integrity roots, and periodic full
  checkpoints. Offline commands shall reconstruct any revision deterministically and verify its
  complete live blob set without a model or network call.
- Dependency and exact-statement changes shall propagate staleness. Lean evidence is bound to an
  exact claim ID, statement version/hash, declaration, source hash, toolchain, mathlib revision,
  build result, and axiom report.
- Distilled failed/blocked work and valid partial results shall persist across incomplete runs;
  raw transcripts remain run artifacts rather than first-class graph nodes.
- Verified source records shall use deterministic canonical identity precedence (DOI, base arXiv
  ID, MR, ISBN, then stable URL), retain arXiv revisions and worker aliases, and merge only an
  identical entity key. Same-title works with distinct identifiers remain separate and unverified
  records remain open under a provisional title/author fingerprint. Worker-result `CITES` edges
  are created only from explicit result-source references; separately verified compiler-literature
  citations remain attached to the frozen target. Equivalent DOI syntax shall deduplicate, while
  distinct DOI publication versions shall remain separate. Ambiguous aliases or existing matches
  preserve every usable source and citation, emit a structured provenance decision, and shall not
  block research unless an active proof dependency has no usable source identity.
- `matek graph migrate-legacy` shall default to an integrity-protected, read-only plan outside the
  vault. Applying requires that exact external plan through `--apply-plan`, interactive confirmation
  or `--yes`, and successful integrity, graph-name, source-revision, and archive-digest checks.
  Application shall be one atomic idempotent graph commit: retain legacy nodes and every prior
  snapshot as archive evidence, create typed proof attempts and proposed derivations, record exact-
  statement aliases, quarantine unsafe main refutations, and leave ambiguous dependencies/scope
  conflicts unapplied. Audit nominations become queued verifier/falsifier tasks; migration itself
  makes no model call and grants no audit trust.
- Approach families are taxonomic groupings, not branch identities. Each assignment branch or
  sub-branch shall retain its own approach record and node so a later report in the same family
  cannot overwrite a prior blocked or ruled-out route. Automatically distilled counterexamples
  are branch-local; claim-level refutation requires a typed counterexample result naming the exact
  claim contract and remains subject to independent scientific audit.
- Humans may rename notes and edit prose outside generated blocks. Exact-statement/proof edits
  trigger versioning/re-audit; machine-field conflicts fail validation instead of being silently
  overwritten.
- The graph shall be navigable in Obsidian through Home, dashboards, links/backlinks, and curated
  canvases, while all validation/query operations remain usable without Obsidian.

### FR-4 Research acceptance gate

- Run fresh-context foundational, domain-specialist, hostile counterexample, and, when
  relevant, complexity/quantitative audits.
- Persist a role-specific rationale and nonempty list of checks performed for every audit.
- Require the candidate package to classify quantitative or algorithmic content explicitly, and
  require the independent foundational auditor to block a false negative so the packager cannot
  bypass an applicable complexity audit.
- Run a final judge that sees the problem contract, candidate package, and audit reports.
- Accept only if the exact target is established, all mandatory audits pass, and unresolved
  theorem-strength obligations are empty.
- Checkpoint each completed audit immediately with its response ID and hash. An unavailable audit
  leaves the candidate awaiting audits and resume retries only missing checks; it is neither a
  candidate rejection nor permission to run the final judge.
- Validate and persist the schema-v2 scientific worker output before deterministic graph admission.
  MATEK, not the model, owns persistence identities, graph relations, and frozen content hashes.
- Preserve valuable partial results under truthful statuses.

### FR-5 Manuscript

- Run only after research acceptance.
- Generate `paper.tex`, `references.bib`, a claim map, and a proof dependency map.
- Include a thorough introduction and related-work discussion.
- Explain how the result differs from and advances existing work.
- Include a Statement of AI Usage naming MATEK with GPT 5.6 and cite the canonical MATEK GitHub
  repository, the available local technical report, and the MATEK whitepaper arXiv preprint when
  its canonical identifier exists. Until then, persist `matek_whitepaper_citation_pending` and
  never fabricate metadata or insert a deliberate TeX failure.
- Do not cite a source solely because another model asserted that it exists.
- Compile with `latexmk` or a configurable LaTeX command.
- Classify claim drift, fabricated citations, and unsafe output as terminating trust failures.
  Repair presentation, citation-field, metadata, and LaTeX findings through the configured
  revision rounds; preserve every draft and validation, and keep the strongest exhausted draft.

### FR-6 Bibliography verification gate

For every cited work, independently verify:

- existence;
- exact title;
- author list;
- year/date;
- venue or publication status;
- DOI, arXiv identifier, ISBN, or stable source URL where available;
- that the manuscript's characterization of the result is supported by the source;
- that the cited theorem is applied under its actual hypotheses when used in a proof.

Produce `bibliography_audit.json` and `bibliography_audit.md`. Any unverified entry blocks
publication readiness unless the citation is removed or corrected. It does not change an accepted
research status or independently block Lean statement alignment and formalization.

### FR-7 Lean feasibility and statement alignment

- After accepted research and a manuscript draft without a terminating claim-integrity or unsafe
  output finding are durable, ask the interactive user whether to proceed with formal Lean
  verification. Bibliography and section-layout defects affect publication readiness, not this
  formalization entry. A negative answer skips every Lean stage and produces the final report. If
  no answer is received within five minutes, proceed automatically.
- Persist the decision so resume never repeats the prompt or completed manuscript work.
- Assess whether full or main-result formalization is realistically attainable.
- Generate `challenge.lean` as the human-auditable target theorem statement.
- Generate a plain-language back-translation and a field-by-field claim alignment report.
- Audit quantifiers, domains, finiteness, exceptional cases, equality notions, hidden
  typeclass assumptions, and use of classical axioms.
- Begin implementation only after statement alignment passes.

### FR-8 Lean formalization

- Invoke Codex CLI non-interactively through an adapter.
- Give Codex bounded, auditable tasks and exact file permissions.
- Iterate edits and Lean diagnostics up to configured budgets.
- Store Codex JSONL, prompts, patches/diffs, commands, and compiler output.
- Do not modify user files outside the run directory without explicit opt-in.

### FR-9 Deterministic Lean verification

A `LEAN_VERIFIED` result requires:

- configured Lean/Lake command exits 0;
- no `sorry`, `admit`, `by?`, unresolved `TODO` placeholders, or equivalent escape hatches;
- no unapproved axioms or declarations that encode the target;
- audited target theorem name and statement are unchanged;
- `#print axioms` output is captured and matches the allowlist;
- imported generated files are scanned too;
- the proof compiles from a clean run.

### FR-10 Reporting

Generate machine-readable and human-readable reports with:

- original problem;
- compiled prompt;
- research configuration and usage;
- strongest proved result;
- exact status and unresolved obligations;
- separate scientific and workflow-execution statuses, retriable issue records, audit progress,
  and an exact resume action;
- audit summaries;
- manuscript build and bibliography status;
- Lean alignment and verification status;
- tool/model versions;
- costs/tokens when available;
- reproducibility instructions;
- relative links to artifacts.
- for workflow errors, a best-effort plain-language model explanation and suggested recovery path,
  using the configured diagnostic model without changing the deterministic failure or silently
  switching providers. If that call is unavailable, retain and report the original error alone.

## Status taxonomy

Persist and report independent status dimensions. A downstream formatting or execution outcome
must not overwrite the strongest scientific result:

```text
Scientific:
  RECEIVED
  NEEDS_PROBLEM_CLARIFICATION
  PROMPT_COMPILED
  RESEARCH_RUNNING
  CANDIDATE_AWAITING_AUDIT
  RESEARCH_PARTIAL
  RESEARCH_REJECTED
  RESEARCH_ACCEPTED_FOR_MANUSCRIPT

Workflow:
  RUNNING
  PAUSED_RETRIABLE
  COMPLETE
  COMPLETE_WITH_WARNINGS
  HARD_STOPPED

Manuscript:
  NOT_STARTED
  NOT_REQUESTED
  SKIPPED_PROBLEM_CLARIFICATION
  PUBLICATION_READY
  DRAFT_WITH_WARNINGS
  PUBLICATION_BLOCKED

Publication:
  NOT_ASSESSED
  READY
  BLOCKED_METADATA
  BLOCKED_CONTENT
  BLOCKED_BIBLIOGRAPHY
  BLOCKED_LATEX
  BLOCKED_INTEGRITY

Lean:
  NOT_STARTED
  LEAN_NOT_REQUESTED
  SKIPPED_PROBLEM_CLARIFICATION
  BLOCKED_MANUSCRIPT_INTEGRITY
  LEAN_INFEASIBLE
  LEAN_STATEMENT_ONLY
  LEAN_PARTIAL
  LEAN_FAILED
  LEAN_VERIFIED_WITH_APPROVED_AXIOMS
  LEAN_VERIFIED
```

Readers may accept legacy combined manuscript/bibliography/Lean values when loading older run
state, but new reports do not emit them as the scientific status.

## Nonfunctional requirements

- Resumable and idempotent at stage boundaries.
- Atomic state writes.
- No paid step repeats after a successful checkpoint unless forced.
- Offline unit tests.
- Secret redaction.
- Clear terminal progress without exposing private chain-of-thought.
- Structured logs and optional verbose diagnostics.
- Every terminal provider attempt is usage-accounted before output admission, including
  schema-invalid and schema-repair attempts.
- Configurable budgets and concurrency.
- Graceful interruption on Ctrl-C.
