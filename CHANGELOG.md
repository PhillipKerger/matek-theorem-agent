# Changelog

## 0.3.0 — 2026-07-22

- Added deterministic, resumable coordinator context budgeting with an 800,000-character default
  measured on final provider input. Large histories compact into prioritized summaries and
  hash-bound artifact/graph references; API coordinators can request omitted evidence on demand.
  Provider size rejection rebuilds a smaller distinct request, while mandatory-state overflow
  pauses as `CONTEXT_BUDGET_EXHAUSTED` without losing completed research.
- Existing problem graphs now carry an explicit pre-delegation review requirement. The initial
  coordinator receives a problem-scoped overview and frontier and must use prior results,
  failures, gaps, audits, and tasks when shaping its assignment portfolio.
- `matek run` now prints the important resolved configuration before the first model call,
  including backend-specific role models/reasoning, web access, effective research concurrency,
  limits, enabled downstream stages, sandbox, graph, and project-edit policy. Dry runs reuse the
  same summary so their reported plan cannot drift from ordinary execution.
- Made the default 32-worker research capacity explicit for both initial assignments and later
  refills. The coordinator now receives the worker search policy, and regression coverage verifies
  that 32 web-enabled initial workers can occupy the pool concurrently.
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
  `checks_performed` evidence, and graph-patch warnings report only the actual patch defect.
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
- Scientific worker reports are committed before optional graph proposals, workers no longer
  supply trusted graph hashes, and invalid/stale graph mutations cannot discard valid results.
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
  Workers receive bounded context slices and return structured optimistic-concurrency patches;
  the deterministic service performs conflict/duplicate/status/DAG checks and atomic idempotent
  merges only after raw worker evidence is durable.
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
