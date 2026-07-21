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
--max-rounds INTEGER
--max-agents INTEGER
--max-research-subagents INTEGER
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

`--backend api` is explicit consent to use separately billed Platform API access. `--dry-run`
validates and prints the resolved backend and stage plan without a model call.

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

`--max-research-subagents N` caps logical research-worker assignments across all adaptive rounds.
It is distinct from `--max-agents`, which caps simultaneous workers. The environment form is
`ASCEND_MAX_RESEARCH_SUBAGENTS=N`.

Generated run directories use
`run-<problem-file-stem>[-<run-name>]-<UTC-timestamp>-<random-suffix>`. The problem stem and
optional run name are normalized to portable, lowercase filesystem-safe components.

During `run` and active `resume` operations, ASCEND prints sparse progress lines with stable
high-level milestone numbers. It does not stream model reasoning, per-call diagnostics, or every
worker completion. A full run may show:

```text
ASCENSION 0: Fetching problem.
ASCENSION 1: Formulating technical research prompt.
ASCENSION 2: Planning research round 1.
ASCENSION 3: Launching 8 research agents for round 1.
ASCENSION 4: Packaging the candidate solution for independent audits.
ASCENSION 5: Writing manuscript and verifying bibliography.
ASCENSION 6: Assessing and verifying the Lean formalization.
ASCENSION 7: Preparing final report.
```

Ascensions 2 and 3 repeat for each adaptive research round. Skipped or already checkpointed stages
do not print misleading progress lines.

After a manuscript compiles and its bibliography is verified, an interactive full run asks:

```text
The verified manuscript is ready. Proceed with formal Lean verification? [Y/n]
```

`n` skips Lean and prepares the final report. An empty/affirmative answer proceeds. If the user
does not answer within five minutes, ASCEND proceeds automatically. Noninteractive invocations
also proceed immediately rather than hanging. The decision is durable and is not asked again on
ordinary resume.

## `ascend status [RUN_ID]`

Shows the selected backend, nonsecret authentication class, Codex/backend version, requested
model and effort, stage table, usage, elapsed time, and artifact paths. API runs may show
calculated dollar cost; Codex runs must not invent a dollar cost for subscription allowance.
If the run ID is omitted, use the latest run in the current project.

## `ascend resume [RUN_ID]`

Resumes the first incomplete stage with the provider stored in run state. Options include
`--backend codex|api`, `--force-stage STAGE`, and backend-appropriate budget increases.

An omitted backend always means “use the frozen provider.” An explicit different provider must
produce a warning, record the switch and reason in provenance, and never happen merely because
Codex is unavailable or rate-limited.

Completed provider work is durably checkpointed before its stage checkpoint when supported.
`--force-stage` creates a fresh provider/cache generation while retaining prior records. A
fully completed run is a no-op.

## `ascend report [RUN_ID]`

Regenerates report products from existing artifacts without changing upstream scientific
artifacts. It is offline by default. `--rewrite` is the only model-assisted report option and
uses the run's selected provider; model prose cannot override deterministic statuses or hashes.

## `ascend verify [RUN_ID]`

Re-runs deterministic LaTeX, bibliography consistency, file-integrity, and Lean checks without
calling either model backend. These subprocess checks currently use the native command backend,
even when the frozen run used Docker.

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
