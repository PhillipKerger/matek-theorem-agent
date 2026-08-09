# Changelog

## Unreleased

- Fixed the post-call Codex integrity guard stopping runs for changes it cannot attribute. A
  bound research worker is no longer re-hashed after its call: Codex's `workspace-write` sandbox
  already confines that process to its private assignment root, and a before/after diff could
  only misattribute other activity (a concurrent run, or Codex's own control directories) to a
  worker that returned a valid report. For other `workspace-write` stages, changes inside the
  shared `.matek` state tree — including sibling run roots, worker workspaces, locks, and
  knowledge graphs — are now recorded as durable warnings (`integrity.json` plus
  `request_metadata["integrity_warnings"]`) instead of a non-retriable stop; only a write into
  the user project outside `.matek` still fails closed. Recovery text no longer advises
  restoring files owned by another run, and `events.jsonl`/`stderr.log` are persisted before any
  integrity failure can discard them.
- Rebased the research-worker workspace on the assignment root
  (`research/workspaces/<assignment-id>/`) instead of only its `scratch/` child. Codex writes
  its `.agents`, `.codex`, and `.git` control directories at its `-C` root, so the bound
  writable domain is now the whole assignment directory; `scratch/` remains the declared area
  whose files are collected as computation evidence. This removes the sandbox-vs-guard mismatch
  that hard-stopped even standalone runs for Codex's own runtime directories.

## 0.7.0 — 2026-08-06

- Isolated every research worker's Codex sandbox and integrity snapshot to the same canonical
  assignment scratch directory. Concurrent runs and distinct knowledge graphs can now write in
  the same project without being misattributed as unauthorized changes; broader, traversal, and
  symlink capabilities remain rejected before execution.
- Made research capacity explicitly run-owned. There is no hidden MATEK-global semaphore, and
  status now shows active runs independently with graph/workspace ownership, requested/effective
  capacity, active/queued assignments, and the global-wait reason. Run locks remain narrow,
  owner-aware, and reclaim stale metadata automatically; graph locks remain scoped to one graph
  and are released by the kernel if their process exits.
- Fixed resume after the paid best-effort error explainer: internal `error_explanation` usage stays
  in the durable budget journal but is no longer misparsed as an unknown resumable workflow stage.
- Made target alignment warning-first. Clause extractors now produce typed, hash-bound diagnostics
  but cannot stop research. A possible conflict gets one short context-aware GPT 5.6 Terra review;
  only `CONFIRMED_CONFLICT` with known clause keys blocks, while uncertainty, malformed output, or
  reviewer failure continues with a warning. The review accounts for negation, scope, temporal
  order, equivalent comparisons, and pathwise-feasibility versus expected-value guarantees.
- Ambiguous problem descriptions now proceed under the most likely explicitly recorded theorem
  interpretation instead of stopping for clarification. Generated prompt placeholders and
  unverified source metadata are removed or downgraded with warnings so the exact statement and
  contract can reach research. Research coordinator and worker prompts were shortened to their
  mathematical and orchestration essentials.
- Workflow errors now receive one best-effort GPT 5.6 Terra medium-effort explanation and suggested
  recovery path through the already selected backend. The explanation is persisted and surfaced in
  status/reports, never replaces the deterministic failure, and cannot trigger API fallback.
- Made online-decision alignment negation-aware: prohibitions such as `may not revoke`,
  `cannot buffer`, and `may not defer` now reinforce irrevocable/immediate semantics instead of
  being read as permissions. Explicitly permitted deferral or revocation still conflicts with an
  immediate/irrevocable statement. Revalidation of an integrity-valid frozen target is now
  warning-only, so a later heuristic disagreement can never falsely become non-retriable
  `StateCorruptionError` or prevent research from starting.
- Preserved conference, journal, and other publication versions as separate canonical source
  nodes when a ledger or historical upgrade contains distinct DOI values. Equivalent DOI syntax
  still deduplicates. Ambiguous aliases and lower-precedence identifiers now retain every source
  and citation, emit a non-blocking structured provenance warning, and transactionally write a
  source-identity decision artifact; graph doctor marks historical mixed-DOI records with the same
  auditable policy instead of blocking research.
- Replaced the remaining broad randomized/deterministic qualifier heuristic with structured
  randomness alignment. Target artifacts now compare algorithm randomization, arrival order,
  weight-adversary timing, expectation sources, pathwise feasibility, and value-guarantee mode as
  orthogonal fields. Randomized policies with deterministic invariants, preprocessing,
  tie-breaking, or seed-conditioned proofs proceed; deterministic-only/no-coin and adversarial-
  order replacements still block with the exact compared values. Clause-specific structured
  comparisons now cover information access and online-decision modes as well, while uncertain
  prose records a warning and continues with the frozen contract.
- Made pre-research polarity alignment structured and local. Requested-outcome polarity is now a
  compact structured value (`affirmative_proof`, `disproof`, `classification`, `construction`,
  `investigation`, or `ambiguous`) derived only from the leading directive of the `polarity`
  clause and the normalized statement. The validator no longer scans framework templates,
  literature summaries, excluded-outcome enumerations, or audit vocabulary, so `counterexample`,
  `disproof`, `refuted`, and `barrier` in intermediate or excluded contexts can never on their own
  fail polarity. A hard stop fires only for an explicit affirmative-proof-versus-disproof mismatch;
  non-material or ambiguous polarity wording records a `prompt_validation_warning`, reuses the
  frozen canonical target, and continues to research without a manual resume. The structured
  polarity decision (gate name, compared fields, decision rule, material flag, and root cause) is
  persisted in `prompts/target_alignment.json` and surfaced in reports. Added a documented
  pre-research gate inventory to `WORKFLOW_SPEC.md`.

## 0.6.0 — 2026-08-05

- Added a deterministic graph-hygiene preflight for generated source metadata. Inconsistent
  primary identifiers are transactionally repaired before prompt-model work, with an append-only
  before/after log and rebuilt graph projections. Sources with no stable identifier remain as
  unverified warnings. `matek graph doctor --repair` exposes the same offline operation, while
  canonical targets and proof dependencies remain outside the repair boundary.
- Reused the graph's live canonical target whenever the normalized user problem is unchanged.
  Repeat runs now rematerialize the previously frozen statement, contract, and compiled prompt;
  stochastic compiler prose or JSON layout cannot trigger a contract-drift pause. A changed
  user-authored problem still requires `--migrate-target REASON`, and reports identify the target
  as created, reused, or migrated with its stable ID and contract hash.
- Split the offline tests into a focused default suite and an explicit `comprehensive` workflow
  tier. Ordinary `pytest -q` now runs fewer than 500 focused checks in roughly a quarter of the
  former workflow-heavy feedback time, while `pytest -q -o addopts=-ra` retains the complete
  offline release suite.

- Replaced brittle prompt-alignment token coverage with a smaller contradiction-only guard. It
  still blocks explicit reversed symbolic quantifiers or polarity, opposing qualifiers, changed
  structured numeric values, and compact formal-comparison drift, while prose omissions,
  paraphrases, abbreviations, and negated examples such as “no `+β`” no longer pause research.
  Clause keys outrank incidental prose, malformed transport Unicode escapes remain normalized for
  comparison, and both reported matroid-secretary compiler artifacts are covered by regression
  tests.
- Clarified every public research-topology control. New configurations use
  `num_first_level_agents`, `subagents_per_agent`, and the across-tier
  `max_concurrent_agents`, with defaults `8`, `4`, and `24`. Hierarchical workers reserve one
  parent plus their complete child allowance, yielding four concurrent first-level workers by
  default. Backend ceilings are now named `max_concurrent_model_calls`; the former configuration
  names and `--max-agents` remain compatibility inputs, while the CLI exposes
  `--num-first-level-agents` and `--max-concurrent-agents`.
- Replaced new full-copy graph snapshots with schema-v2 content-addressed node/edge blobs, compact
  delta manifests, parent/content integrity roots, and periodic full checkpoints. Existing
  schema-v1 snapshots remain byte-preserving and read-only; offline reconstruct and verification
  commands work across both formats, and snapshot corruption now fails graph validation.
- Froze the exact target, canonical contract, and compiled prompt by normalized source SHA-256.
  Deterministic clause alignment now blocks contract-clause drift, while the explicitly confirmed
  `matek run PROBLEM_FILE --migrate-target REASON` path records a versioned migration, survives
  resume, and invalidates affected evidence. Same-contract rewordings are recorded as cosmetic
  paraphrases and do not replace the frozen bytes by default.
- Replaced worker-authored graph patches with `ResearchWorkerReport` schema v2. Workers report
  typed mathematics, obligations, sources, computation declarations, and branch outcomes;
  application code owns identities, relation directions, status rules, and deterministic graph
  admission. The legacy-named `research/graph-patches/` files are admission records with
  `model_authored_patch = false`. Admission is idempotent on run, assignment, local result key, and
  result schema version. Nonempty assumption contracts and partial results are retained as
  quarantined proof attempts with explicit obligations, cannot act as same-report premises, and
  cannot support candidates or either audit lane; only eligible complete, assumption-free results
  create proposed derivations.
- Added an integrity-protected canonical claim/derivation/obligation ledger alongside the complete
  Markdown archive. Trusted closure now models AND-premises and OR derivations, quarantines
  ambiguous, partial, gapped, or assumption-bearing evidence, and exposes a bounded smallest-known-
  open-cut computation without overstating minimality when the search is capped. Obligation logical
  versions now cover exact statement, conclusion, quantifiers, hypotheses, dependency and target
  claim IDs, scope, notation-definition version, and falsification evidence.
- Hardened acceptance against evidence-envelope forgery: exact-target candidates must match the
  frozen normalized theorem, persisted computation manifests and replay verdicts are reverified
  before graph admission, and blind lemma gates are recomputed from their immutable role artifacts.
  Successful lemma audits resolve only the exact live obligation they audited; changed premises,
  fabricated mappings, proposed zero-premise derivations, and model-only refutation stops fail
  closed. Prompt-only forced replay now freezes the original pre-gate candidate-support slice so
  graph promotion from the first pass cannot trigger duplicate candidate/audit calls.
- Added the production exact-counterexample terminal lane. Only a complete typed main-scope result
  matching the frozen theorem can be nominated; official verifier and hostile-falsifier requests,
  independently accounted responses, and the deterministic gate are persisted and hash-bound to
  the immutable worker report, transitive support closure, dependency versions, current canonical
  graph, and independently replayed computations. Policy settings and distinct role contexts are
  frozen; `FAIL` and parsed `BLOCKED` judgments take terminal precedence. Missing or tampered
  evidence pauses or fails closed, resume calls only genuinely missing roles, and only a recomputed
  passing gate may return `RESEARCH_REJECTED` or add a main-target `REFUTES` edge. A frozen
  retryable `BLOCKED` nomination survives unrelated graph revisions; a genuine canonical support
  change durably supersedes the old audit with a reason and artifacts, creates a new audit ID, and
  reruns the required roles.
- Added durable `explore`, `consolidate`, `bottleneck`, `adversarial_audit`, and `synthesize`
  scientific phases, configurable under `[research.scientific_phase]`, with exact-duplicate merge,
  near-duplicate redirection, durable one-obligation bottleneck focus, five-role rotation across
  activations, queued old-phase retirement, and audited-premise synthesis.
- Added live blind verifier/falsifier transactions for exact, gap-free, non-main open-cut lemmas.
  Schema-v2 evidence binds distinct application execution contexts and optional sanitized provider
  session IDs across inputs, responses, and gates. Role evidence resumes independently. Existing
  schema-v1 artifacts are archived byte-for-byte beneath an integrity manifest and both roles must
  rerun as v2 before graph trust; an intermediate pass is hard-coded not to accept the main target
  or authorize manuscript generation. Missing-role retries preserve SHA-addressed gate checkpoints;
  resume adopts only authenticated monotone evidence progress and repairs response accounting.
- Unified manuscript and formalization graph inputs behind one bounded trusted-context selector.
  Accepted main-proof support is prioritized; informal/open claims, unverified sources,
  unauthenticated or unresolved routes, experiments, and archive-only evidence are excluded. Each
  context now reports its policy, cap, eligible/included/omitted counts, truncation state, ledger
  ambiguity count, and priority order.
- Added private per-assignment computation workspaces, quota- and path-safe collection into a
  run-local content-addressed store, and independent replay through restricted Docker with
  filesystem confinement and networking disabled; unsafe native replay is refused. A passing
  replay yields only proposed evidence pending mathematical/domain audit, while unreplayed or
  mismatching computations remain non-proof evidence.
- Canonicalized verified sources by DOI, base arXiv ID, MR, ISBN, then stable URL while retaining
  revisions, aliases, evidence links, and provenance. Distinct identifiers no longer merge by
  title or through a shared URL carrying conflicting stable identifiers, including across runs;
  later verified identifiers upgrade compatible provisional entities without changing their
  stable node identity. Worker-result `CITES` edges require explicit source references. Exact
  normalized statement plus scope defines claim identity; semantic near-duplicates require audit
  or an explicit equivalence derivation instead of fuzzy merging.
- Added `matek graph migrate-legacy`, which defaults to an external integrity-protected read-only
  plan. The explicitly confirmed `--apply-plan` form rejects stale, tampered, or wrong-graph plans
  and performs one atomic idempotent backfill while retaining legacy nodes and snapshots as archive
  evidence. It creates proposed proof attempts/derivations, aliases and refutation quarantines, and
  queued verifier/falsifier tasks without making model calls or granting audit trust.
- Coordinator context schema v3 ranks graph evidence by scientific relevance, emits bounded typed
  node digests, caps unrequested full graph evidence at 120,000 characters, deduplicates exact
  repeated content, and uses an intentional versioned section order with expanded manifest
  accounting. Consequential actions citing omitted evidence now defer into authenticated
  retrieval-only activations; frozen legacy manifests retain exact replay identity.
- Made target-registry publication part of the same recoverable graph transaction as compiled
  target admission, and versioned scientific-phase evidence with a phase epoch so stale audit or
  synthesis signals cannot advance a resumed run.
- Added a sanitized, synthetic-derived legacy worker-report regression corpus. It exercises the
  historical envelope shapes described by the available archive notes without claiming replay of
  the unavailable original multi-gigabyte run archives.

- Added explicit bootstrap/continuation/resume coordinator activation metadata with current and
  previous graph revisions. Fresh coordinator contexts now reconstruct the branch map from
  canonical scheduler, event, audit, continuity, registry, and graph state, and decisions attest
  the reviewed revision.
- Made graph assignment targets fail closed: every new assignment names a live stable node in the
  selected problem, with no silent fallback for empty, unknown, cross-problem, tombstoned, or
  non-research targets.
- Preserved one registry record and graph approach node per assignment branch rather than
  collapsing same-family work. Blocked/refuted branches retain their failure and reopen
  conditions, while automatically distilled counterexamples remain branch-local unless an
  exact-claim scientific refutation result passes independent review.

## 0.3.0 — 2026-07-23

- Made Codex hierarchical research the default: eight first-level workers may each use up to
  eight one-tier nested agents, for 64 nested-agent slots. Users can set first-level concurrency
  with `--max-agents` and a per-worker nested allowance with `--subagents-per-agent`; the
  coordinator and workers see both limits, `--flat` or a zero allowance produces regular workers,
  and only research-worker Codex processes receive the bounded collaboration controls. The API
  adapter visibly resolves to its portable flat mode because it has no nested-agent tool.
- Set the default active-time allowance to 15 hours and raised scheduler, backend, API-spend, and
  formalization safety ceilings so ordinary default runs should encounter the time boundary first.
  The backend-wide concurrency ceiling is now 64; the durable first-level MATEK pool remains eight.
- Added deterministic, resumable coordinator context budgeting with an 800,000-character default
  measured on final provider input. Large histories compact into prioritized summaries and
  hash-bound artifact/graph references; API coordinators can request omitted evidence on demand.
  Compact mode reserves at least 5%/40,000 characters, caps every optional section, replaces
  exhaustive catalogs and graph views with authenticated descriptors, and persists per-section
  measurements. Provider size rejection rebuilds a smaller distinct request. Only the exact
  prompt/claim plus provider/output envelope can produce `MANDATORY_CONTEXT_TOO_LARGE`.
- `matek resume problem.md` now selects the newest run whose immutable intake record names that
  exact source file; run-ID resume remains unchanged.
- Existing problem graphs now carry an explicit pre-delegation review requirement. The initial
  coordinator receives a problem-scoped overview and frontier and must use prior results,
  failures, gaps, audits, and tasks when shaping its assignment portfolio.
- `matek run` now prints the important resolved configuration before the first model call,
  including backend-specific role models/reasoning, web access, effective research concurrency,
  limits, enabled downstream stages, sandbox, graph, and project-edit policy. Dry runs reuse the
  same summary so their reported plan cannot drift from ordinary execution.
- Aligned the public specifications, example reports, and Lean confirmation wording with the
  independent scientific, manuscript, publication, workflow, and Lean statuses. Added a
  contributor guide and included the complete public specification set in source distributions.
- Locked mandatory bibliography, related-work, placeholder, and axiom checks against misleading
  configuration disablement. Project integrity verification now detects distribution/package
  version drift and validates the exact generated schema set, including the final-report schema.
- Manuscript validation now classifies terminating trust failures separately from repairable
  presentation, citation-field, metadata, and LaTeX findings. Configured revisions checkpoint
  every draft and validation; bibliography auditing and safe LaTeX builds continue independently.
- Missing canonical MATEK whitepaper metadata now produces
  `matek_whitepaper_citation_pending` and uses the repository/local technical-report fallback
  without fabricated identifiers or deliberate TeX failures.
- Reports now separate accepted research, manuscript quality, publication readiness, Lean status,
  skipped stages, and retriable actions. Publication-only defects no longer overwrite scientific
  status or prevent statement-aligned Lean formalization.
- Research audit artifacts now carry role-specific rationales and nonempty
  `checks_performed` evidence, and the legacy v0.3 graph-mutation warning reports only the actual
  admission defect. The Unreleased schema-v2 boundary above supersedes that mutation protocol.
- Added a persisted resilience taxonomy (`integrity`, `execution`, `evidence`, `scientific`, and
  `resource`). Only security/state/artifact integrity failures hard-stop; recoverable provider,
  source, worker, graph-mutation, and audit failures now produce warnings or coordinator events.
- Made the frozen user claim the only terminal scientific target. Reductions and weaker results
  remain durable intermediate evidence, while scientific no-progress/reduction stop requests are
  declined and research continues until exact acceptance, exact refutation, or an explicit
  resource/provider boundary.
- `matek run` and `matek resume` now finish with a deterministic terminal report summary covering
  exact-problem resolution, stopping point, completed work, strongest result, remaining
  obligations, next action, and artifact locations without adding another model call.
- Literature-only source outages now quarantine and qualify dependent claims without blocking
  research, with an `arxiv.org/abs/` fallback; strict proof, citation, and bibliography gates are
  unchanged.
- Scientific worker reports are committed before the legacy graph-mutation integration, workers
  do not supply trusted graph hashes, and an invalid/stale graph admission cannot discard valid
  results. The Unreleased schema-v2 boundary removes the optional model proposal entirely.
- Candidate audits checkpoint independently and resume retries only missing checks. Reports and
  `matek status` now separate scientific from workflow state and expose audit progress and resume
  obligations.
- Usage accounting now records every terminal provider attempt, including schema-invalid output
  and successful bounded repair generations.
- Obsidian graph nodes now display note titles: managed notes use title filenames beneath stable-ID
  directories, existing generated paths migrate transactionally, and full relative wikilinks keep
  identities unambiguous. Accepted main results tag their explicit proof-support closure as
  `MAIN_RESULT_NEEDS` and expose it through a dashboard and focused proof-architecture canvas.
- Renamed the pre-release project to MATEK (Multi-Agent Theorem Exploration through Knowledge-Graph
  Memory). The distribution is now `matek-theorem-agent`, the Python package is
  `matek_theorem_agent`, the CLI is `matek`, configuration is `matek.toml`, environment variables
  use `MATEK_`, and project state lives under `.matek/`. No legacy command or state-path alias is
  retained because the project had no released user base.
- Added a persistent Obsidian-compatible typed knowledge graph under `.matek/knowledge/` with
  stable IDs, separate claims/proofs/audits/formalizations, typed relation constraints, portable
  Markdown source, rebuildable SQLite indexing, snapshots, dashboards, and curated canvases.
- Integrated graph frontier memory and graph-scoped tasks into the continuous coordinator.
  Workers receive bounded context slices, and the deterministic service performs
  conflict/duplicate/status/DAG checks and atomic idempotent integration only after raw worker
  evidence is durable. The Unreleased schema-v2 report admission replaces the original v0.3
  model-mutation boundary.
- Added dependency and exact-statement invalidation, human-edit ownership rules, preservation of
  distilled failed/partial routes, manuscript mappings, and exact-version Lean verification
  records. Added `matek graph` init/validate/status/frontier/rebuild/open/export/diff and focused
  traversal commands; Obsidian remains optional.
- Isolated persistent memory into named per-problem vaults at
  `.matek/knowledge/<graph-name>/`. The default name comes from the problem filename stem; related
  or follow-up problems may explicitly reuse an existing graph with `--knowledge-graph NAME`.
  Graph selection is frozen across resume, and the CLI lists graphs and requires a choice when
  maintenance would otherwise be ambiguous.
- Replaced fixed research rounds and wait-for-all batches with a durable, completion-driven
  logical coordinator. Worker completions and failed audits become atomically written immutable
  event files; the coordinator reacts and refills the live pool without waiting for unrelated
  work.
- Preserved every complete raw worker/audit report, assignment lifecycle, coordinator decision,
  source-verification result, and sequenced research event as immutable evidence. The canonical
  atomic coordinator checkpoint uses a pending-event write-ahead transaction; mailbox, assignment,
  registry, and continuity files are materialized views that cannot compress away the evidence.
- Kept a diverse sixteen-assignment bootstrap while allowing dynamic refill/expansion to 32
  active workers within a default total-open ceiling of 32 queued-plus-running assignments. There
  is no separate cumulative research-worker ceiling; global Codex call/thread limits remain
  optional and unset by default.
- Split research roles so the default GPT 5.6 Sol logical coordinator uses max effort while
  independent GPT 5.6 Sol workers use xhigh. The API adapter additionally requests pro mode; Codex
  CLI uses its own model/reasoning-effort controls. This application-level orchestration is the
  reproducible analogue of an Ultra research session; `Ultra` is not encoded as an API primitive.
- Replaced public fixed-round controls with `maximum_pending_assignments` and
  `maximum_coordinator_decisions` (default 256). Legacy round settings and `--max-rounds` are
  migrated to scaled decision budgets for compatibility and never restore a synchronization
  barrier.
- Candidate claims now pause new worker admission and enter the independent gate immediately.
  In-flight reports remain durable; a failed gate feeds its complete reports and exact
  obligations back to the coordinator as high-priority events and resumes/refills the pool.
- Added `--time-limit-minutes` to `run` and `resume` (plus
  `MATEK_TIME_LIMIT_MINUTES`) as one checkpointed active wall-clock allowance for the entire
  workflow; the remaining allowance now cancels overlong in-flight model calls. The option is
  disabled by default.
- Added `--no-web-search` to `run` and `resume`, disabling live search across all model stages
  and MATEK's deterministic identifier HTTP resolver while preserving strict citation gates;
  web search remains enabled by default.
- Aligned generated prompts more visibly with the public Cycle Double Cover prompting pattern by
  requiring a compact, problem-specific research mandate before the expanded MATEK protocol.
- Documented the methodology, orchestration, provenance, stage resilience, bibliography gates,
  the `challenge.lean` trust boundary, and current limitations in the public specification set.
- Added hash-validated stage-boundary guards so downstream work cannot start from incomplete or
  modified upstream checkpoints.
- Added a durable post-manuscript Lean confirmation. Interactive users may decline; five minutes
  without an answer defaults to proceeding, and noninteractive runs proceed without hanging.
- Made compiled-prompt placeholder validation resilient to mathematical interval, index, matrix,
  citation, Markdown, code, and LaTeX notation. Strong editorial markers now receive one bounded
  sentence-only repair; optional unresolved text is downgraded with a persisted warning, while
  target-critical ambiguity still fails closed.
- Persisted `prompts/prompt_validation.json` and the compiled/source artifacts before the
  placeholder gate. Forced prompt-stage recovery reuses successful compiler/source calls and
  refreshes only the bounded repair generation.
- Rendered CLI exception text with Rich markup disabled so bracketed diagnostics remain literal.

### Command-line progress

- `matek run` and active resumes now print sparse numbered `ASCENSION` milestones for intake,
  prompt formulation, coordinator start/resume, live-pool management, candidate audits,
  manuscript generation, Lean, and final reporting without streaming model reasoning or per-call
  noise.

### Strict structured outputs

- Codex output schemas are now generated from closed Pydantic models, require every object
  property, reject arbitrary-key maps locally, and omit unsupported defaults.
- Structured-output schema digests now participate in call-cache identity, and packaged schemas
  are generated and checked against the same model authority.
- Provider `invalid_json_schema` failures are reported as non-retryable schema compatibility
  errors with the saved schema path instead of `CODEX_PROCESS_CRASH` retries.

### Source provenance reliability

- Prompt compilation, adaptive research, and bibliography validation now share typed source
  records, explicit evidence-to-source links, and canonical DOI/arXiv/ISBN/MR/HTTPS identifiers.
- A bounded deterministic resolver verifies identifiers with title checks, retries, redirect
  handling, resolver fallback, and a run-local success cache; provider citation metadata is no
  longer required for workflow completion.
- Prompt compilation performs at most one small source-ledger correction. Optional unresolved
  literature is removed or marked unknown with warnings, while unverified imported theorems
  remain blocking proof obligations.
- Opt-in `matek doctor --deep` now reports whether the installed Codex JSONL stream exposes
  search result URLs. Ordinary doctor remains model-call-free.

### Repository publication cleanup

- Moved the package, tests, resources, documentation, and CI workflow to the repository root.
- Excluded local coding-agent handoff instructions and generated development state from Git.
- Added canonical GitHub project metadata and corrected source-install and example paths.
- Renamed the project integrity check to `scripts/verify_project.py`.

### Problem identification and prior literature

- Concise problem files are explicitly supported when they uniquely identify the target.
- Prompt compilation can now stop with a persisted clarification request instead of guessing an
  ambiguous problem; downstream stages are skipped and the report asks the user to revise the
  input and start a new run.
- Compiled problems now classify their relationship to existing literature. Exact known results
  require verified source and hypothesis matching and cannot be presented as novel merely because
  MATEK reconstructed or formalized them.

### Configuration and documentation

- Earlier development raised the Codex `max_parallel_agents` ceiling from 3 to 8; the current
  doubled defaults are recorded at the top of this release section.
- Documented the per-run `.matek/runs/<run-id>/` output layout, including manuscript, Lean,
  report, and trace locations.

## 0.2.0 — 2026-07-19

### Codex is now the default backend

- MATEK now runs structured model stages through the official Codex CLI by default and reuses
  the saved authentication established by `codex login`. ChatGPT-authenticated use does not
  require an OpenAI Platform API key.
- The existing Responses API backend remains supported through explicit `--backend api` or
  `[backend] provider = "api"` selection. MATEK never silently falls back to API billing.
- Configuration schema v2 adds provider-specific Codex/API settings and conservatively migrates
  legacy API-shaped configuration with a one-time notice.
- Runs now retain provider-scoped call caches, Codex JSONL traces, backend/authentication
  provenance, backend-specific limits, and explicit provider-migration history.
- `matek doctor` separates Codex and optional API checks; `--deep` is the opt-in live Codex
  structured-output probe.

### Manuscript disclosure

- Generated manuscripts must include a Statement of AI Usage stating that the MATEK system
  with GPT 5.6 was used and must cite both the canonical MATEK GitHub repository and MATEK
  whitepaper arXiv preprint.
- Deterministic manuscript and reproduction checks reject missing disclosures, missing
  citations, and placeholder repository/arXiv identifiers.
