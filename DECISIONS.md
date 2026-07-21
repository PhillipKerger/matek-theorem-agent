# Locked Product Decisions

These decisions define the current v0.x product line and should not be reopened unless
implementation reveals a concrete blocker.

## Distribution and execution

- Local-first, open GitHub repository, installable Python CLI.
- No hosted service or web UI.
- No Postgres, queue, workflow server, or account system.
- State and artifacts live under `.ascend/` in the current project.
- Native execution is default; Docker is optional.
- Officially support Linux, macOS, and WSL2 initially.

## Orchestration

Persistent research memory uses an application-level typed knowledge graph. Obsidian is the
recommended human view, not a runtime dependency or database. Markdown/frontmatter is
authoritative, SQLite is derived, and the vault stays beneath `.ascend/` to preserve the existing
write boundary. The central coordinator alone creates tasks and accepts validated worker patches;
subagents never mutate shared graph files concurrently.

- Explicit application-level agents are the stable default.
- Run workers concurrently with `asyncio` and bounded concurrency.
- Research uses one durable logical coordinator with a completion-driven mailbox and live worker
  pool, not fixed rounds or wait-for-all worker batches.
- The coordinator creates an initial diverse portfolio of sixteen, then reacts to persisted
  worker/audit events and dynamically refills up to 32 active workers by default.
- The canonical atomic coordinator checkpoint owns scheduler state and a pending-event write-ahead
  record. Full raw reports, per-assignment source verification, and atomically created immutable
  event/decision files validate that checkpoint. The mailbox, assignment files, approach registry,
  and continuity view aid delivery and navigation but may not replace either the checkpoint or its
  evidence; correctness may not depend on hidden provider memory.
- Hosted multi-agent features may later be added as an experimental backend, never as a
  required dependency.

## Model configuration

- The official Codex CLI, authenticated through the user's saved ChatGPT login, is the
  recommended/default model-execution backend.
- The direct OpenAI Responses API remains available only through explicit `api` selection and
  separate Platform billing.
- Never silently fall back between providers.
- All model IDs, backend-supported reasoning modes, efforts, token limits, and tool availability
  are configurable.
- The closest reproducible analogue to a GPT 5.6 Sol Ultra research session is the explicit
  application-level coordinator above. `Ultra session` is a product behavior target, not a Codex
  or Responses API parameter.
- Default research-role targets use `gpt-5.6-sol`; the Responses API sends pro mode while Codex CLI
  uses its model and reasoning-effort controls without a separate Responses API mode field:
  - prompt compiler: xhigh effort, web search on;
  - research coordinator and final research judge: max effort, web search configurable;
  - research workers and independent proof auditors: xhigh effort, web search configurable;
  - manuscript and bibliography agents: high or xhigh effort, web search on;
  - low-risk formatting/status tasks may use a cheaper configurable model later.
- Do not encode ChatGPT product labels such as “Ultra session” as API primitives.

## Lean and Codex

- Reuse an existing Lean project by default.
- Generate files only under `.ascend/runs/<run-id>/lean/`.
- Use Codex CLI's non-interactive mode through a subprocess adapter.
- The common Codex backend can execute research, audits, manuscript work, and formalization.
  Research-only mode omits Lean but still needs the selected model backend. Users without Codex
  may explicitly select the API backend.
- Never claim Lean verification from a model judgment. Run Lean/Lake deterministically.

## Manuscript and citations

- Manuscript creation occurs before Lean.
- A complete related-work section is mandatory.
- References must be verified independently, not merely copied from the research response.
- Any unresolved, contradictory, or likely fabricated citation blocks the manuscript gate.
- Every manuscript includes a Statement of AI Usage naming ASCEND with GPT 5.6 and cites both
  the canonical ASCEND GitHub repository and ASCEND whitepaper arXiv preprint.
- Any scholarly, technical, or public work that uses ASCEND must cite both the software
  repository and whitepaper preprint, whether or not ASCEND generated the final manuscript.
- Prefer primary sources: publisher pages, DOI/Crossref metadata, arXiv records, journal or
  conference proceedings, and authors' official pages where appropriate.

## Safety and integrity

- No project edits outside `.ascend/` without `--allow-project-edits`.
- No API key or Codex credential in files or logs.
- ASCEND never reads Codex credential files; it uses only `codex login status` for diagnostics.
- Truthful terminal statuses; partial work is preserved rather than mislabeled as solved.
- The exact prompt framework remains immutable and its SHA-256 is checked at runtime.
