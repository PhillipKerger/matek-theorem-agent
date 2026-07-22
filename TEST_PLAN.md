# Test Plan

## Test strategy

All default tests are offline. Model and Codex integrations use protocol-based fakes and
recorded sanitized fixtures. Live tests require explicit environment flags.

## Unit tests

- Knowledge-graph schema/type validation, stable IDs, Markdown/frontmatter round trips, and
  relation constraints.
- Atomic graph patch merges, idempotency, likely duplicates, stale-base/hash conflicts, invalid
  status promotions, dependency DAG cycles, and staleness propagation.
- Human prose/statement edits, statement version increments, proof re-audit, managed-field
  conflicts, unknown Markdown notes, crash recovery, snapshots, diffs, and SQLite rebuilds.
- Frontier selection, graph-scoped task creation, bounded context slices, manuscript mappings,
  and exact-version Lean records.
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
- Event-driven research configuration defaults and migration of legacy
  `maximum_assignments_per_round`/`maximum_rounds` and `--max-rounds` inputs to
  `maximum_pending_assignments`/`maximum_coordinator_decisions` without reintroducing barriers.
- Budget accounting, the total-open queued-plus-running assignment limit, coordinator-decision
  limits, and active concurrency limits; no separate cumulative research-worker cap.
- Zero-padded coordinator decision/event IDs, assignment lifecycle transitions, mailbox
  acknowledgement, replay idempotence, and atomic raw-report/source-verification-before-event
  ordering.
- One immutable atomic file per research event; the canonical state-first pending-event write-ahead
  transaction completes idempotently after interruption, evidence hashes validate the checkpoint,
  and a missing/invalid canonical scheduler checkpoint fails rather than being inferred from event
  evidence.
- Every coordinator activation includes the unchanged main prompt and claim contract,
  unacknowledged events, registry/audit state, and complete referenced raw reports.
- A fast worker completion can trigger a decision and refill while slower workers remain active.
- Candidate audit pauses new admission; in-flight completions remain durable, and a failed audit
  becomes an immediate high-priority coordinator event.
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
- Assert worker evidence precedes graph integration and graph patch artifacts/events remain
  replayable without another paid call.

1. Complete successful run to `LEAN_VERIFIED`.
2. Research rejected: no manuscript or Lean call.
3. Repairable audit: full audit evidence and obligations immediately reactivate the coordinator
   and eventually succeed without waiting for unrelated workers.
4. Bibliography contains nonexistent work: blocks Lean.
5. LaTeX compilation failure: preserves source and truthful status.
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
14. Live-pool refill: one of sixteen initial workers finishes while others run, the coordinator
    consumes that completion and admits targeted work up to the 32-worker ceiling without a batch
    barrier.
15. More logical workers complete over time than the configured total-open ceiling while open and
    active-concurrency ceilings remain respected, demonstrating that no cumulative worker cap
    exists (the focused fixture scales the ceiling down for fast offline execution).
16. Candidate acceptance while research remains active prevents queued work from starting;
    candidate rejection preserves in-flight reports and immediately reprioritizes audit repairs.

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
