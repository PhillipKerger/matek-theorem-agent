# Release Checklist

## Installation and publication

- [x] Assign the canonical GitHub owner/URL and replace every `OWNER` placeholder.
- [ ] Publish the MATEK whitepaper on arXiv and replace every `ARXIV_ID` placeholder.
- [ ] Test `pipx install git+https://...` from the published repository.
- [ ] Test `uv tool install git+https://...` from the published repository.
- [ ] Complete native macOS and WSL2 validation (Linux quality gates pass locally).
- [x] README documents current official Codex standalone, npm, Homebrew, and Windows methods.
- [x] Repository publication and platform-validation caveats remain explicit.
- [x] License selected.

## Citation and manuscript disclosure

- [x] README says MATEK must be cited in every scholarly, technical, or public work using it.
- [x] README requires the GitHub citation now and the MATEK arXiv whitepaper citation once its
  canonical metadata exists.
- [x] Missing whitepaper metadata uses an honest repository/local-report fallback and persists
  `matek_whitepaper_citation_pending`; it never fabricates metadata or deliberately breaks LaTeX.
- [ ] Replace the honest citation placeholders with canonical software and preprint metadata.
- [x] Generated manuscripts are gated on the required Statement of AI Usage naming MATEK with
  GPT 5.6 and the citations available for the current metadata state (validated with
  non-placeholder fixture metadata).

## Default Codex experience

- [x] New configuration defaults to schema v2 and `[backend] provider = "codex"`.
- [x] README quickstart leads with Codex installation and **Sign in with ChatGPT**.
- [x] Documentation explains that Codex mode needs no Platform API key but is not offline, free,
  or unlimited.
- [x] API setup appears later as an advanced, separately billed, explicit selection.
- [x] Documentation states that MATEK never silently falls back to API billing.
- [x] Ordinary doctor logic consumes no model allowance and separates Codex from optional API
  diagnostics.
- [x] CLI exposes and renders `matek doctor --deep` as the explicit live Codex probe.
- [ ] Opt-in live Codex smoke tests pass with an authenticated disposable environment.

## Compatibility and migration

- [x] Legacy v0.1 API-shaped configuration migrates without losing model, budget, or pricing
  settings.
- [x] Migration infers `provider = "api"` and emits a one-time notice.
- [x] Existing API end-to-end fixture still passes through the explicit API backend.
- [x] Codex end-to-end fixture passes the same stage, artifact, and gate checks.
- [x] Resume preserves the frozen backend and records any explicit provider migration.
- [x] Normalized source hashes reuse the exact frozen target/contract/prompt, clause alignment
  fails closed, and confirmed `--migrate-target REASON` migrations persist across resume and
  invalidate affected evidence.
- [x] Legacy graph snapshots remain byte-preserving/readable while new content-addressed
  revisions, checkpoint replay, reconstruction, and corruption rejection are covered offline.
- [x] `matek graph migrate-legacy` defaults to an external integrity-protected plan with no graph
  edits; confirmed `--apply-plan` rejects stale/tampered/wrong-graph input, commits idempotently,
  preserves legacy snapshots/nodes as archive evidence, and queues fresh audits without model calls.
- [ ] Replay the actual latest ATSP, matroid-secretary, and k-server archived worker reports when
  those original multi-gigabyte archives are available. The checked-in sanitized corpus is
  synthetic-derived and must not be represented as the unavailable archive replay.

## CLI and recovery

- [x] All original commands in `CLI_SPEC.md` are implemented.
- [x] Doctor gives exact remediation and checks installed Codex capabilities rather than only a
  hard-coded version.
- [x] `--dry-run`, `--no-lean`, deterministic report regeneration, and resume work.
- [x] Ctrl-C leaves resumable state and completed call records.
- [x] `run` and `resume` expose explicit `--backend codex|api` selection.
- [x] Codex failures checkpoint with actionable resume guidance and never initiate API calls.

## Research integrity

- [x] Framework preserved verbatim and hash checked.
- [x] Adaptive research registry and independent audit suite implemented.
- [x] Recoverable worker/provider/source failures are durable coordinator events; integrity
  failures alone hard-stop research.
- [x] Candidate audits checkpoint independently and resume retries only missing checks.
- [x] Schema-v2 workers cannot submit graph patches or persistence identities; deterministic
  application admission cannot discard an already validated scientific report.
- [x] Coordinator inputs are deterministically budgeted, manifest-bound, and compacted without
  truncating evidence; provider size rejection creates a smaller distinct resumable request.
- [x] Coordinator schema v3 ranks and digests graph evidence scientifically, caps optional full
  graph nodes, deduplicates exact repeats, records section/score/headroom evidence, and defers
  consequential actions until cited full evidence is visible while replaying legacy manifests.
- [x] Schema-invalid and schema-repair provider attempts are usage-accounted.
- [x] Reductions and weaker variants cannot terminate research or pass candidate acceptance as a
  substitute for the frozen exact claim; scientific no-progress stop requests are durably
  declined.
- [x] No manuscript is generated after a rejected proof.
- [x] Related-work requirement and independent bibliography verification are mandatory.
- [x] False citations and unsupported theorem hypotheses block promotion at their applicable trust
  boundaries without erasing accepted research.
- [x] LaTeX compilation and the independent publication-readiness gate are implemented.
- [x] Repairable manuscript findings consume configured revision rounds, preserve every draft,
  and do not independently block bibliography auditing or Lean statement alignment.
- [x] Persistent typed Markdown graph extends the same problem across runs and keeps claims,
  proof attempts, derivations, obligations, audits, sources, tasks, counterexamples, and
  formalizations separate.
- [x] The complete Markdown archive is distinguished from the integrity-protected canonical
  claim/derivation/obligation ledger; AND/OR trust, ambiguity quarantine, and bounded smallest-
  known-open-cut reporting are covered offline.
- [x] Obligation logical versions cover statement, conclusion, quantifiers, hypotheses,
  dependency/target IDs, scope, notation version, and falsification evidence; assumption-bearing
  and partial results remain archive-only with explicit obligations and cannot support candidates
  or either audit lane.
- [x] Canonical source identity, arXiv revision/alias retention, unverified-source quarantine, and
  explicit worker-result-source `CITES` integration are covered offline.
- [x] Durable scientific phases, duplicate/near-duplicate handling, configurable thresholds and
  concurrency, one-obligation five-role bottleneck rotation, queued-work retirement, and audited-
  premise synthesis survive resume.
- [x] Private computation collection rejects unsafe files, uses application-computed CAS hashes,
  and creates only proposed support after independent restricted-Docker replay; native replay is
  refused, mathematical/domain audit remains required, and an unrelated replay cannot satisfy the
  exact-main candidate gate.
- [x] Blind intermediate-lemma verifier/falsifier transactions bind exact source/dependency
  versions and complete target-obligation contracts, use distinct schema-v2 execution contexts and
  optional sanitized provider sessions, resume only missing roles, archive v1 evidence byte-for-
  byte before a mandatory two-role v2 rerun, preserve digest-addressed retry checkpoints, recover
  only monotone gate/accounting progress, and can never accept the main target or authorize a
  manuscript.
- [x] Retryable exact-counterexample audits reuse their frozen nomination across unrelated graph
  revisions, while a genuine canonical-support change durably supersedes the old audit with a
  reason and artifacts and creates a fresh audit ID.
- [x] Manuscript and formalization use one bounded trusted-context policy that prioritizes accepted
  main-proof support, excludes informal/unverified/archive-only evidence, and reports explicit cap
  and omission metadata.
- [x] Problem filename stems select isolated default graphs; explicit existing-graph reuse,
  unknown-name rejection, frozen resume identity, listing, and multi-graph CLI selection are
  covered by offline tests.
- [x] Coordinator frontier queries, bounded worker contexts, deterministic typed-report admission,
  application-resolved local-result DAGs, atomic commits, partial-work retention, and dependency
  invalidation are covered by offline tests.
- [x] Obsidian Home/dashboards/canvases are generated, while every graph command remains usable
  without Obsidian.
- [x] Human editing ownership and exact-statement/proof invalidation fail closed on conflicts.
- [x] The fixture end-to-end suite covers target-migration interruption/resume, scientific-phase
  resume, blind missing-role lemma resume, and Docker-versus-native computation admission.

## Lean

- [x] Existing Lean project is detected and reused.
- [x] Writes are confined to the run directory by default.
- [x] `challenge.lean` alignment audit implemented.
- [x] Codex noninteractive formalization adapter implemented.
- [x] Deterministic build, placeholder, statement, and axiom checks implemented.
- [x] Lean graph records bind exact claim version/hash, declaration, source hash, toolchain,
  mathlib revision, build result, and axiom report.

## Security

- [x] No credential-file inspection is used by doctor diagnostics.
- [x] Prompts use stdin and user-derived commands never use `shell=True`.
- [x] Secret redaction and credential-minimal subprocess environments are tested.
- [x] Docker command execution disables network and implicit pulls.
- [x] Default Codex backend tests prove ambient API credentials cannot change provider/billing.
- [x] Write-capable Codex backend tests prove unauthorized changes are rejected.
- [x] Timeout and cancellation tests prove Codex process trees are terminated and checkpointed.

## Engineering and examples

- [x] Offline unit suite passes on Linux without live accounts or network calls.
- [x] Strict static checks (`ruff`, formatting, and `mypy`) are configured.
- [x] Successful, partial, and rejected example reports are included.
- [x] Contributor setup, quality gates, trust boundaries, and pull-request expectations are
  documented.
- [x] Project integrity checks detect version drift and missing, stale, or unexpected schemas.
- [x] Re-run all quality gates after the scientific-ledger/snapshot changes and rebuild/install
  wheel and sdist artifacts.
