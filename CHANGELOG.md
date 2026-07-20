# Changelog

## Unreleased

### Strict structured outputs

- Codex output schemas are now generated from closed Pydantic models, require every object
  property, reject arbitrary-key maps locally, and omit unsupported defaults.
- Structured-output schema digests now participate in call-cache identity, and packaged schemas
  are generated and checked against the same model authority.
- Provider `invalid_json_schema` failures are reported as non-retryable schema compatibility
  errors with the saved schema path instead of `CODEX_PROCESS_CRASH` retries.

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

- Raised the default Codex `max_parallel_agents` ceiling from 3 to 8; the separate web-agent
  ceiling remains configurable and defaults to 2.
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
