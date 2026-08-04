# Test Plan

## Test strategy

All default tests are offline. Model and Codex integrations use protocol-based fakes and
recorded sanitized fixtures. Live tests require explicit environment flags.

## Unit tests

- Knowledge-graph schema/type validation, stable IDs, Markdown/frontmatter round trips, and
  relation constraints.
- Deterministic schema-v2 scientific admission, idempotent application-owned graph commits,
  `(run, assignment, local key, result schema)` collision rejection, unknown dependency/target
  rejection, exact-statement-plus-scope claim identity, gap/obligation versus gap-free/proposed-
  derivation classification, real relation directions, invalid status-promotion prevention,
  stable-node and same-report local dependency resolution, unknown/self/cyclic local dependency
  rejection, dependency DAG cycles, and staleness propagation. New worker schemas reject
  model-authored `graph_patch` payloads.
- Human prose/statement edits, statement version increments, proof re-audit, managed-field
  conflicts, unknown Markdown notes, crash recovery, snapshots, diffs, and SQLite rebuilds.
- Content-addressed snapshot deltas, blob deduplication, periodic checkpoints, integrity/content
  roots, deterministic reconstruction, corruption rejection, schema-v1 read compatibility, and
  byte-preserving legacy-to-v2 continuation.
- Frontier selection, graph-scoped task creation, bounded context slices, manuscript mappings,
  and exact-version Lean records.
- Source-hash target registry create/reuse, byte-stable same-source target/contract/prompt reuse,
  cosmetic same-contract paraphrase recording, clause-level quantifier/domain/additive-term/
  polarity alignment failures, explicit versioned migration and invalidation, registry integrity
  rejection, and `--migrate-target` confirmation.
- Archive-to-ledger projection, exact-statement claim aliases, AND-premise/OR-derivation trust,
  typed obligation logical versions covering exact statement, conclusion, quantifiers, hypotheses,
  dependency/target claim IDs, scope, notation version, and falsification evidence; assumption-
  contract and partial-result quarantine; derivation-local staleness; integrity-protected ledger
  persistence; and bounded smallest-known-open-cut computation/capping.
- Blind intermediate-lemma nomination and audit: main/computation/definition/counterexample/gapped
  and assumption-bearing/partial exclusion, unique admission binding, exact cut relevance/leverage,
  complete frozen obligation contracts and dependency/artifact hashes, origin-field stripping,
  schema-v2 distinct verifier/falsifier execution contexts, optional redaction-safe provider
  sessions and collision rejection, missing-role-only resume, persisted request/response/gate re-
  verification, byte-preserving v1 manifest/archive migration and mandatory two-role v2 rerun,
  forged-mapping rejection, deterministic pass/fail/blocked gates, exact-obligation `RESOLVES`
  semantics, and the nonterminal main/manuscript flags.
- Exact-main-counterexample nomination and terminal audit: rejection of branch, intermediate,
  incomplete, assumption-bearing, partial, and target-mismatched results; complete worker-envelope
  and scheduler-response binding; transitive result-DAG and unresolved-obligation closure; exact
  dependency-version, declaration, canonical-graph, and independently replayed computation
  support; frozen official policy/prompt/settings artifacts; distinct role contexts and
  verifier/hostile-falsifier response
  accounting; explicit witness, hypothesis, and failed-conclusion recomputation; frozen-`BLOCKED`
  reuse across unrelated graph revisions and missing-role-only resume; durable supersession plus a
  new audit ID after genuine canonical-support change; tampered/forged artifact rejection;
  deterministic `FAIL`/`BLOCKED` precedence and gate replay before terminal rejection; and graph
  promotion in which only the verified gate adds `REFUTES` to the main target.
- Private computation workspaces, confinement and file quotas, symlink/hardlink/special/undeclared
  rejection, application-computed CAS hashes, immutable manifest reuse/corruption checks, safe-
  backend refusal, restricted-Docker replay pass/mismatch/failure, native unsafe-backend handling,
  canonical evidence-path and declaration-hash binding, forged-mapping rejection, proposed-only
  replay support, unreplayed-ledger exclusion, rejection of unrelated replay, exact-main transitive
  local-result closure binding, and canonical computation derivation/manifest/replay graph support.
- Durable scientific phase transitions, plateau/repeated-gap/cut signals, phase-specific roles and
  concurrency, exact duplicate merge, near-duplicate redirect, complementary cut-target contracts,
  one-obligation five-role bottleneck rotation, old-phase queued-work retirement, audited-premise
  synthesis, current-epoch graph-recorded complementary adversarial evidence, per-transition phase
  epochs that exclude stale signals and queued work, configurable `[research.scientific_phase]`,
  and resume integrity.
- Canonical source identity precedence, base-arXiv/revision handling, alias/evidence/provenance
  merge, cross-run provisional-to-verified upgrades, conflicting stable identifiers on a shared
  URL, distinct same-title identifiers, provisional unverified fingerprints, exact-statement-plus-
  scope claim merging, semantic-near-match review, idempotent integration, and explicit worker-
  result-only `CITES` edges.
- Shared bounded trusted-context selection for manuscript and formalization, accepted-main-proof
  prioritization, canonical trusted-closure enforcement, authenticated definition/proof-route
  checks, verified-source/formalization role filtering, informal/unverified/archive-only exclusion,
  deterministic cap ordering, and explicit eligible/included/omitted/truncation metadata.
- Legacy migration planning with zero graph edits, gapped-proof classification, dependency
  extraction, review-only derivations, branch-refutation quarantine, exact claim groups, audit-
  nomination ranking, ambiguity/scope reports, digest verification, and vault-output refusal.
  Reviewed application covers confirmation bypass, wrong-graph/stale/tampered rejection, archive-
  digest/version revalidation, atomic/idempotent recovery, legacy-node/snapshot preservation,
  proposed-only derivations, queued audit tasks, and zero model calls.
- Bootstrap, existing-graph, continuation, and resume activation metadata; exact reviewed-revision
  attestation; and current-frontier reconstruction without provider conversation memory.
- Explicit assignment branch-target validation, including rejection of empty, unknown,
  cross-problem, tombstoned, and non-research targets without main-claim fallback.
- Distinct same-family assignment branches, durable blocked/refuted outcomes and reopen
  conditions, and conservative branch-local counterexample links that cannot silently refute the
  main claim.
- Graph CLI behavior, including graceful operation when Obsidian is not installed.
- Problem-stem graph naming, isolation between different problem files, explicit existing-graph
  reuse for follow-up work, unknown-name rejection, resume identity freezing, and ambiguous CLI
  selection when multiple graphs exist.

- Config precedence and validation.
- Backend resolution, Codex default, legacy API migration, and no-silent-fallback policy.
- GPT 5.6 Sol max-effort coordinator/final-judge and xhigh worker/auditor defaults, API pro-mode
  requests, Codex reasoning-effort mapping, role isolation, and migration of the former shared
  research model/effort settings.
- Codex capability/auth-status parsing without credential-file access.
- Run ID generation and project discovery.
- Atomic state writes and recovery from truncated temp files.
- Stage transition legality.
- Stage-boundary status and artifact-integrity validation.
- Artifact hash and framework integrity checks.
- Placeholder detection in compiled prompts.
- Ambiguous input produces a clarification request and no downstream model or command calls.
- Fully resolved literature matches require verified primary-source evidence and retain known
  result provenance.
- An unavailable literature-support arXiv source is quarantined and its claims are downgraded while
  research still starts; target-identification evidence remains fail closed.
- Event-driven research configuration defaults and migration of legacy
  `maximum_assignments_per_round`/`maximum_rounds` and `--max-rounds` inputs to
  `maximum_pending_assignments`/`maximum_coordinator_decisions` without reintroducing barriers.
- Budget accounting, the total-open queued-plus-running assignment limit, coordinator-decision
  limits, and active concurrency limits; no separate cumulative research-worker cap.
- Hierarchical configuration and CLI precedence; both tier limits reach coordinator and worker
  inputs, only Codex research-worker commands receive the nested-agent controls, a zero limit
  produces the regular-subagent contract, and positive hierarchy is rejected for the API adapter.
- Zero-padded coordinator decision/event IDs, assignment lifecycle transitions, mailbox
  acknowledgement, replay idempotence, and atomic raw-report/source-verification-before-event
  ordering.
- One immutable atomic file per research event; the canonical state-first pending-event write-ahead
  transaction completes idempotently after interruption, evidence hashes validate the checkpoint,
  and a missing/invalid canonical scheduler checkpoint fails rather than being inferred from event
  evidence.
- Every coordinator activation includes the unchanged main prompt and claim contract,
  unacknowledged events, registry/audit state, and complete referenced raw reports.
- Synthetic checkpoints with at least 833 omitted artifacts stay below the headroom-adjusted
  serialized-input target; the exhaustive catalog becomes one path/hash/count descriptor,
  graph memory is not duplicated, and per-section caps hold. New/candidate reports outrank older
  progress, repeated issues aggregate without losing
  assignment IDs or paths, and every omitted artifact remains hash-addressable.
- Bounded API retrieval services requested artifacts on a later activation. Provider
  `input_too_large` rejection creates a smaller distinct request and preserves the event cursor
  across resume. Oversized cumulative scheduler state falls back to an indexed context below the
  transport limit and continues. Only the exact prompt/claim plus provider instructions, output
  contract, and envelope reports `MANDATORY_CONTEXT_TOO_LARGE`; repeated provider rejection has a
  distinct retriable diagnosis and leaves a smaller request for resume.
- Scientific graph evidence ranking is materially invariant under stable-node ID renaming;
  explicit/new evidence precedes optional history; unrequested full graph nodes stay at or below
  120,000 serialized characters; low-ranked irrelevant additions do not evict relevant evidence
  or fill the provider ceiling; typed digests retain exact statements, gaps, dependencies,
  counterexample scope, typed relations, and hash-bound provenance.
- Schema-v3 top-level section order, exact-content deduplication, redundant-character accounting,
  and legacy manifest replay identity are deterministic. A consequential decision citing omitted
  evidence becomes retrieval-only until the requested full artifact or node is visible.
- A fast worker completion can trigger a decision and refill while slower workers remain active.
- Candidate audit pauses new admission; in-flight completions remain durable, and a failed audit
  becomes an immediate high-priority coordinator event.
- A worker schema failure during candidate auditing becomes a durable issue and does not cancel
  already-running audits. Deterministic admission failure likewise retains valid scientific
  reports and application-owned admission records.
- Two completed audits survive a third audit crash and restart retries only the missing audit;
  acceptance remains impossible with any missing audit or unverified imported theorem.
- Invalid provider outputs and successful schema-repair attempts both count toward usage and call
  limits.
- A coordinator's scientific stop based on a useful reduction is declined; the declined event and
  recovery obligation are durable, a later exact-target assignment may complete the proof, and all
  candidate/audit/judge inputs carry the no-terminal-reductions policy.
- Sparse progress maps Ascension 2 to coordinator start/resume and Ascension 3 to live-pool
  management, with no per-batch repetition.
- Retry classification and incomplete API responses.
- Redaction of keys/tokens.
- Path traversal and symlink escape rejection.
- Citation metadata validation.
- LaTeX command result classification.
- Lean placeholder scans and axiom allowlist checks.
- Theorem-statement hash comparison.

## Integration tests with fakes

- Run two independent `WorkflowRunner.run_new` calls for the same source problem and assert one
  stable problem node, distinct run nodes, increasing revisions, and a valid shared vault.
- Run different source files and assert separate default vaults, then explicitly reuse one vault
  for a follow-up source and assert both stable problem nodes coexist only in that selected graph.
- Assert worker evidence precedes deterministic graph admission and legacy-named application
  admission records/events remain replayable without another paid call.

1. Complete successful run to `LEAN_VERIFIED`.
2. Research rejected: no manuscript or Lean call.
3. Repairable audit: full audit evidence and obligations immediately reactivate the coordinator
   and eventually succeed without waiting for unrelated workers.
4. Bibliography contains nonexistent work: blocks publication and a fabricated citation blocks
   downstream work; incomplete but non-fabricated bibliography metadata still permits Lean.
5. LaTeX compilation failure: consumes configured repairs, preserves every draft, and records a
   truthful publication status.
6. Lean absent with `--no-lean`: successful research/manuscript report.
7. Codex reaches budget: `LEAN_PARTIAL` with resumable state.
8. Ctrl-C after a worker completion: raw report and source verification precede the immutable
   event; the canonical pending-event transaction completes and materialized mailbox/index views
   refresh so resume neither loses nor double-delivers work.
9. Resume does not repeat paid model calls.
10. Framework file modified: doctor/run fail with actionable integrity message unless a
    custom framework is explicitly selected.
11. Ambiguous problem: clarification is reported and all research/manuscript/Lean stages skip.
12. Existing theorem: exact source/hypothesis matching is recorded without a novelty claim.
13. Post-manuscript Lean confirmation: approve, decline, five-minute timeout-to-proceed,
    noninteractive auto-proceed, and crash-safe decision reuse.
14. Live-pool refill: one of eight initial workers finishes while others run, the coordinator
    consumes that completion and admits targeted work up to the eight-worker first-level ceiling
    (with eight nested agents per parent) without a batch
    barrier.
15. More logical workers complete over time than the configured total-open ceiling while open and
    active-concurrency ceilings remain respected, demonstrating that no cumulative worker cap
    exists (the focused fixture scales the ceiling down for fast offline execution).
16. Candidate acceptance while research remains active prevents queued work from starting;
    candidate rejection preserves in-flight reports and immediately reprioritizes audit repairs.
17. Retriable research failure reports expose separate workflow/scientific status, completed
    workers, committed and missing audits, trace paths, and the resume action.
18. Corrupt state, immutable hash mismatch, unsafe path, and unauthorized write still hard-stop.
19. Same-source compiler paraphrase: exact target/contract/prompt bytes are reused; a confirmed
    contract migration persists through interruption/resume and invalidates dependent evidence.
20. Intermediate lemma transaction: one completed blind role survives interruption, resume calls
    only the missing role, and a pass promotes only the intermediate claim/derivation without main
    acceptance or manuscript authorization.
21. Computation transaction: collection produces immutable CAS/manifests, native replay records an
    unsafe backend, Docker replay is independently reproduced, and a pass yields only a proposed
    derivation pending audit.
22. Scientific plateau: persisted cut/gap signals advance phases, exact duplicates merge,
    near-duplicates redirect, all five roles rotate against one durable bottleneck obligation,
    phase transitions retire queued old-phase work, and resume retains the same scientific-phase
    checkpoint.
23. Canonical ledger integration: archive notes rebuild the same AND/OR ledger, Main Result Needs
    shows the pre-acceptance target/cut, capped search remains explicit, and alternative derivation
    staleness is local.
24. Source/claim integration: exact source keys and exact-statement-plus-scope claims merge
    idempotently, distinct identifiers and semantic near-matches remain separate, and only explicit
    worker-result source links create result `CITES` edges.
25. Legacy backfill: planning leaves the vault byte-identical; applying the externally reviewed
    plan creates one new graph revision and application record, a retry is idempotent, ambiguous
    items remain unapplied, and queued verifier/falsifier tasks grant no trust without later audits.
26. Sanitized legacy-envelope acceptance: the synthetic-derived fixture corpus exercises the
    historical report variants documented by the available archive notes. The corpus is explicitly
    not represented as a replay of the unavailable original run archives.
27. Assumption/partial trust boundary: same-statement claims with different assumption contracts do
    not alias; assumed and partial results remain archive-only with obligations and cannot reach
    candidate packaging, lemma nomination, or exact-counterexample nomination/support.
28. Audit compatibility and resume: a schema-v1 lemma pass is byte-preserved under a verified
    `legacy-v1/manifest.json` and both roles rerun in distinct v2 contexts; a frozen incomplete
    counterexample audit survives an unrelated revision, while genuine canonical-support change
    durably supersedes it and creates a new audit. Missing-role retries checkpoint the prior gate
    by digest and recover only monotone response/accounting progress after scheduler lag.
29. Downstream context isolation: manuscript and formalization share the same selection policy,
    exclude informal claims, unverified sources, unauthenticated routes, and archive-only evidence,
    prioritize accepted main-proof support, and report deterministic cap metadata.
30. Prompt-only replay: an unchanged compiled target reuses the archived pre-gate graph and
    candidate-support inputs, creates no duplicate model-call records, and retains the earlier
    research generation as auditable history.

## Optional live tests

Guard Codex tests with `MATEK_CODEX_LIVE_TESTS=1` (and the optional
`MATEK_CODEX_LIVE_SEARCH=1`) and API tests with the project's explicit API-live switch. Use a
low-cost configuration:

- one ChatGPT-authenticated `codex exec --json` structured-output smoke test in a disposable
  fixture repository;
- one Codex search-enabled probe;
- one explicitly selected Responses API structured/search call;
- tiny Lean theorem compilation;
- tiny LaTeX document compilation.

Never run live tests in ordinary CI without explicit account, allowance/credit, and cost
approval. Ordinary `matek doctor` and all default tests make no model call.

## Quality gates

Suggested commands:

```bash
ruff check .
ruff format --check .
mypy src
pytest -q
python scripts/verify_project.py
```
