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
- [x] README requires citations to both the GitHub repository and MATEK arXiv whitepaper.
- [ ] Replace the honest citation placeholders with canonical software and preprint metadata.
- [x] Generated manuscripts are gated on the required Statement of AI Usage naming MATEK with
  GPT 5.6 and distinct repository/preprint citations (validated with non-placeholder fixture
  metadata).

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
- [x] No manuscript is generated after a rejected proof.
- [x] Related-work requirement and independent bibliography verification are mandatory.
- [x] False citations and unsupported theorem hypotheses block progression.
- [x] LaTeX compile/citation gate implemented.
- [x] Persistent typed Markdown graph extends the same problem across runs and keeps claims,
  proofs, audits, sources, tasks, counterexamples, and formalizations separate.
- [x] Problem filename stems select isolated default graphs; explicit existing-graph reuse,
  unknown-name rejection, frozen resume identity, listing, and multi-graph CLI selection are
  covered by offline tests.
- [x] Coordinator frontier queries, bounded worker contexts, structured patches, atomic conflict
  checks, partial-work retention, and dependency invalidation are covered by offline tests.
- [x] Obsidian Home/dashboards/canvases are generated, while every graph command remains usable
  without Obsidian.
- [x] Human editing ownership and exact-statement/proof invalidation fail closed on conflicts.

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
- [x] Re-run all quality gates after the backend migration and rebuild wheel/sdist artifacts.
