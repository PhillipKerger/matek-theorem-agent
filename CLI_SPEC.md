# CLI Specification

ASCEND uses Typer for typed command parsing and Rich for readable output. All commands present
Codex first as the recommended/default model backend and the direct API as advanced/optional.

## Backend selection

Model-backend resolution is:

```text
explicit --backend codex|api
  -> ASCEND_BACKEND
  -> [backend].provider in ascend.toml
  -> codex
```

The chosen provider is persisted in run state. ASCEND never changes a provider implicitly and
never falls back from Codex to API billing. Resume uses the frozen provider unless a user
explicitly requests a provenance-changing migration.

## `ascend init`

Creates a schema-v2 `ascend.toml` with `[backend] provider = "codex"`, `.ascend/.gitignore`, and
an example problem. It must not overwrite existing files without confirmation or `--force`.
Legacy API configurations are migrated without discarding settings and receive a one-time
notice.

## `ascend doctor`

The default command performs no model call and groups output as follows.

### ASCEND environment

- supported Python version;
- resolved project/configuration and selected default backend;
- write permissions under `.ascend/`; and
- prompt-framework integrity hash.

### Codex backend — recommended/default

- configured `codex` executable and version;
- `codex exec` and `codex login`;
- JSONL, final-output, JSON Schema, sandbox, approval, working-directory, search, model, and
  configuration flags;
- `codex exec resume` when session persistence is enabled; and
- authentication class from `codex login status` only.

ASCEND classifies authentication as ChatGPT, API key, access token, authenticated/unknown,
not authenticated, or error. It never reads credential files or prints raw status output that
could disclose identity or secret data.

### OpenAI API backend — advanced/optional

- whether `OPENAI_API_KEY` is configured; and
- API connectivity only when `--online` is explicitly passed.

A missing key is a warning in Codex mode and a failure only when the API backend is selected.
The output explicitly says that a key is not required for ChatGPT-authenticated Codex use.

### Research tools

- Git;
- Lean/Lake and project markers when Lean is enabled;
- configured LaTeX compiler when manuscript generation is enabled; and
- Docker/image availability only when Docker command execution is configured.

`ascend doctor --deep` explicitly opts into one minimal live Codex structured-output call with
search enabled. It may consume Codex allowance or credits. Probe artifacts live only in a
temporary directory and are deleted afterward. Ordinary `doctor` never runs this probe.

Every failure includes an exact remediation command. In particular, an unsigned-in user is
directed to run `codex login`, choose **Sign in with ChatGPT**, and rerun `ascend doctor`.

## `ascend run PROBLEM_FILE`

Without a backend flag, a new installation uses Codex. Important options:

```text
--backend codex|api
--config PATH
--framework PATH
--run-name TEXT
--budget-usd FLOAT
--max-coordinator-decisions INTEGER
--max-agents INTEGER
--time-limit-minutes INTEGER
--no-web-search
--no-lean
--research-only
--sandbox native|docker
--allow-project-edits
--dry-run
--yes
--verbose
```

`--max-rounds INTEGER` remains accepted as a deprecated compatibility input. ASCEND translates
each historical round into the applicable open-work-capacity number of coordinator decisions (32
under historical defaults); it never creates rounds or a wait-for-all synchronization barrier.
Supplying both the legacy and current decision options is an error.

`--backend api` is explicit consent to use separately billed Platform API access. `--dry-run`
validates and prints the resolved backend and stage plan without a model call.

The research defaults use `gpt-5.6-sol`, max coordinator effort, and xhigh worker effort. The
Responses API adapter sends `reasoning.mode = "pro"` for those roles; the Codex adapter uses the
Codex CLI model and reasoning-effort controls and has no separate ASCEND `pro` switch. No
`--ultra` option exists: Ultra-like research behavior comes from the durable application-level
coordinator and live pool, not a provider parameter.

`--no-web-search` disables web search in every model stage and disables ASCEND's deterministic
public-identifier HTTP resolver. Search remains enabled by default. The resolved setting is
saved with the run; the same flag on `ascend resume` disables it for all remaining stages.
Unverifiable citations remain unverified, so this option never weakens the bibliography gate and
a fully offline run should normally also use `--research-only`.

`--time-limit-minutes N` sets the total active wall-clock allowance across prompt compilation,
research, manuscript work, and formal verification. Elapsed active time is stored in run state
and carried into resume; time while ASCEND is not running is excluded. The remaining allowance
also bounds each in-flight model call. There is no wall-clock limit by default.
`ASCEND_TIME_LIMIT_MINUTES=N` is the environment form.

`--max-agents N` caps simultaneous research workers. The built-in concurrency default is 32.
`research.maximum_pending_assignments` defaults to 32 total open assignments—queued plus
running—and `research.maximum_coordinator_decisions` defaults to 256 event-indexed decisions. The
concurrency limit controls the active subset of that open set. None of these settings imposes a
separate cumulative logical-worker limit. Codex global call-count limits remain configurable in
TOML but are unset by default.

Generated run directories use
`run-<problem-file-stem>[-<run-name>]-<UTC-timestamp>-<random-suffix>`. The problem stem and
optional run name are normalized to portable, lowercase filesystem-safe components.

During `run` and active `resume` operations, ASCEND prints sparse progress lines with stable
high-level milestone numbers. It does not stream model reasoning, per-call diagnostics, or every
worker completion. A full run may show:

```text
ASCENSION 0: Fetching problem.
ASCENSION 1: Formulating technical research prompt.
ASCENSION 2: Starting continuous research coordinator.
ASCENSION 3: Managing adaptive research pool: 16 initial assignments, up to 32 active agents.
ASCENSION 4: Packaging the candidate solution for independent audits.
ASCENSION 5: Writing manuscript and verifying bibliography.
ASCENSION 6: Assessing and verifying the Lean formalization.
ASCENSION 7: Preparing final report.
```

On resume, Ascension 2 prints `Resuming continuous research coordinator at event N.` using the
canonical checkpoint's event cursor. Ascension 3 then uses the same adaptive-pool wording with the
persisted initial count and effective concurrency. They do not repeat at artificial batch
boundaries; candidate-audit milestones may recur for distinct candidate attempts. Skipped or
already checkpointed stages do not print misleading progress lines.

After a manuscript compiles and its bibliography is verified, an interactive full run asks:

```text
The verified manuscript is ready. Proceed with formal Lean verification? [Y/n]
```

`n` skips Lean and prepares the final report. An empty/affirmative answer proceeds. If the user
does not answer within five minutes, ASCEND proceeds automatically. Noninteractive invocations
also proceed immediately rather than hanging. The decision is durable and is not asked again on
ordinary resume.

## `ascend status [RUN_ID]`

Shows one backend summary, a `Research roles:` line with configured coordinator/worker models and
efforts, the stage table, aggregate usage and elapsed time, and recorded artifact paths. When the
canonical research checkpoint exists, it also prints `Research coordinator:` with phase, decision
count, the acknowledged-through event cursor, and queued, active, and completed assignment counts.
API runs may show calculated dollar cost; Codex runs must not invent a dollar cost for subscription
allowance. If the run ID is omitted, use the latest run in the current project.

## `ascend resume [RUN_ID]`

Resumes the first incomplete stage with the provider stored in run state. Options include
`--backend codex|api`, `--force-stage STAGE`, and backend-appropriate budget increases.

An omitted backend always means “use the frozen provider.” An explicit different provider must
produce a warning, record the switch and reason in provenance, and never happen merely because
Codex is unavailable or rate-limited.

Completed provider work is durably checkpointed before its stage checkpoint when supported. A
research resume loads canonical `research/coordinator/state.json`, completes any event held in its
pending-event write-ahead field, and validates the checkpoint against immutable zero-padded
events/decisions, source verification, and complete raw reports before admitting new work. It does
not need or claim to resume a provider conversation. A missing or invalid canonical research
checkpoint blocks ordinary resume. Forcing the prompt-compilation or research boundary archives
the prior research tree under `research-history/`, creates a fresh provider/cache generation and
scheduler checkpoint, and retains the archived records. An explicit provider migration also
archives an incomplete research scheduler because its outstanding request identities belong to
the old provider. The authorized migration itself is write-ahead and crash-recoverable. A fully
completed run is a no-op.

## `ascend report [RUN_ID]`

Regenerates report products from existing artifacts without changing upstream scientific
artifacts. It is offline by default. `--rewrite` is the only model-assisted report option and
uses the run's selected provider; model prose cannot override deterministic statuses or hashes.

## `ascend verify [RUN_ID]`

Re-runs deterministic LaTeX, bibliography consistency, file-integrity, and Lean checks without
calling either model backend. These subprocess checks currently use the native command backend,
even when the frozen run used Docker.

## `ascend graph`

Graph commands are local and model-free:

- `ascend graph init` creates `.ascend/knowledge/`, its schema/state, initial snapshot,
  navigation, canvases, and SQLite index.
- `ascend graph validate` checks Markdown parsing, stable IDs, machine ownership, endpoint/type
  constraints, dependency cycles, hashes, and index revision; invalid graphs exit 6.
- `ascend graph status` and `frontier [--problem-id ID]` render typed machine-readable summaries.
- `ascend graph rebuild-index` recreates SQLite from authoritative Markdown.
- `ascend graph open` attempts Obsidian and otherwise succeeds gracefully while printing the
  vault path for manual opening.
- `ascend graph export [--format json|graphviz|mermaid] [--output PATH]` exports without Obsidian.
- `ascend graph diff REVISION_A REVISION_B` compares immutable snapshots.
- `show`, `dependencies`, `downstream`, `stale`, and `tasks` provide focused graph queries.
- `tombstone NODE_ID --reason TEXT` preserves an obsolete identity and invalidates dependents;
  managed notes must not be deleted directly.

The vault lives beneath `.ascend/` so these commands do not imply consent to edit project source.

## Exit codes

```text
0 workflow completed (including truthful partial/failure scientific status)
2 invalid CLI/config/input
3 missing dependency/environment or unsupported Codex capability
4 selected-backend authentication/provider failure
5 selected-backend budget or allowance limit before a safe checkpoint
6 artifact/state corruption
7 deterministic verification failure
130 interrupted by user
```

Scientific failure is represented in the report/status, not necessarily as a process crash.
An input that does not uniquely identify a mathematical target similarly completes with
`NEEDS_PROBLEM_CLARIFICATION`: research is not launched, and the terminal output and report ask
the user to revise the problem file and start a new run.
