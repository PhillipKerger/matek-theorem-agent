# Test Plan

## Test strategy

All default tests are offline. Model and Codex integrations use protocol-based fakes and
recorded sanitized fixtures. Live tests require explicit environment flags.

## Unit tests

- Config precedence and validation.
- Backend resolution, Codex default, legacy API migration, and no-silent-fallback policy.
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
- Budget accounting and concurrency limits.
- Retry classification and incomplete API responses.
- Redaction of keys/tokens.
- Path traversal and symlink escape rejection.
- Citation metadata validation.
- LaTeX command result classification.
- Lean placeholder scans and axiom allowlist checks.
- Theorem-statement hash comparison.

## Integration tests with fakes

1. Complete successful run to `LEAN_VERIFIED`.
2. Research rejected: no manuscript or Lean call.
3. Repairable audit: returns to research and eventually succeeds.
4. Bibliography contains nonexistent work: blocks Lean.
5. LaTeX compilation failure: preserves source and truthful status.
6. Lean absent with `--no-lean`: successful research/manuscript report.
7. Codex reaches budget: `LEAN_PARTIAL` with resumable state.
8. Ctrl-C after a worker batch: completed artifacts remain and resume works.
9. Resume does not repeat paid model calls.
10. Framework file modified: doctor/run fail with actionable integrity message unless a
    custom framework is explicitly selected.
11. Ambiguous problem: clarification is reported and all research/manuscript/Lean stages skip.
12. Existing theorem: exact source/hypothesis matching is recorded without a novelty claim.
13. Post-manuscript Lean confirmation: approve, decline, five-minute timeout-to-proceed,
    noninteractive auto-proceed, and crash-safe decision reuse.

## Optional live tests

Guard Codex tests with `ASCEND_CODEX_LIVE_TESTS=1` (and the optional
`ASCEND_CODEX_LIVE_SEARCH=1`) and API tests with the project's explicit API-live switch. Use a
low-cost configuration:

- one ChatGPT-authenticated `codex exec --json` structured-output smoke test in a disposable
  fixture repository;
- one Codex search-enabled probe;
- one explicitly selected Responses API structured/search call;
- tiny Lean theorem compilation;
- tiny LaTeX document compilation.

Never run live tests in ordinary CI without explicit account, allowance/credit, and cost
approval. Ordinary `ascend doctor` and all default tests make no model call.

## Quality gates

Suggested commands:

```bash
ruff check .
ruff format --check .
mypy src
pytest -q
python scripts/verify_project.py
```
