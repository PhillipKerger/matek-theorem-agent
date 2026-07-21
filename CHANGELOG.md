# Changelog

## Unreleased

- Added a persistent Obsidian-compatible typed knowledge graph under `.ascend/knowledge/` with
  stable IDs, separate claims/proofs/audits/formalizations, typed relation constraints, portable
  Markdown source, rebuildable SQLite indexing, snapshots, dashboards, and curated canvases.
- Integrated graph frontier memory and graph-scoped tasks into the continuous coordinator.
  Workers receive bounded context slices and return structured optimistic-concurrency patches;
  the deterministic service performs conflict/duplicate/status/DAG checks and atomic idempotent
  merges only after raw worker evidence is durable.
- Added dependency and exact-statement invalidation, human-edit ownership rules, preservation of
  distilled failed/partial routes, manuscript mappings, and exact-version Lean verification
  records. Added `ascend graph` init/validate/status/frontier/rebuild/open/export/diff and focused
  traversal commands; Obsidian remains optional.
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
  `ASCEND_TIME_LIMIT_MINUTES`) as one checkpointed active wall-clock allowance for the entire
  workflow; the remaining allowance now cancels overlong in-flight model calls. The option is
  disabled by default.
- Added `--no-web-search` to `run` and `resume`, disabling live search across all model stages
  and ASCEND's deterministic identifier HTTP resolver while preserving strict citation gates;
  web search remains enabled by default.
- Aligned generated prompts more visibly with the public Cycle Double Cover prompting pattern by
  requiring a compact, problem-specific research mandate before the expanded ASCEND protocol.
- Added a standalone, compiled LaTeX methodology report covering orchestration, provenance,
  stage resilience, bibliography gates, the `challenge.lean` trust boundary, and limitations.
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

- `ascend run` and active resumes now print sparse numbered `ASCENSION` milestones for intake,
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
- Opt-in `ascend doctor --deep` now reports whether the installed Codex JSONL stream exposes
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
  ASCEND reconstructed or formalized them.

### Configuration and documentation

- Earlier development raised the Codex `max_parallel_agents` ceiling from 3 to 8; the current
  doubled defaults are recorded at the top of this release section.
- Documented the per-run `.ascend/runs/<run-id>/` output layout, including manuscript, Lean,
  report, and trace locations.

## 0.2.0 — 2026-07-19

### Codex is now the default backend

- ASCEND now runs structured model stages through the official Codex CLI by default and reuses
  the saved authentication established by `codex login`. ChatGPT-authenticated use does not
  require an OpenAI Platform API key.
- The existing Responses API backend remains supported through explicit `--backend api` or
  `[backend] provider = "api"` selection. ASCEND never silently falls back to API billing.
- Configuration schema v2 adds provider-specific Codex/API settings and conservatively migrates
  legacy API-shaped configuration with a one-time notice.
- Runs now retain provider-scoped call caches, Codex JSONL traces, backend/authentication
  provenance, backend-specific limits, and explicit provider-migration history.
- `ascend doctor` separates Codex and optional API checks; `--deep` is the opt-in live Codex
  structured-output probe.

### Manuscript disclosure

- Generated manuscripts must include a Statement of AI Usage stating that the ASCEND system
  with GPT 5.6 was used and must cite both the canonical ASCEND GitHub repository and ASCEND
  whitepaper arXiv preprint.
- Deterministic manuscript and reproduction checks reject missing disclosures, missing
  citations, and placeholder repository/arXiv identifiers.
