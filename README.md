# ASCEND: An Orchestrator for Agentic Mathematical Research with Lean Verification

ASCEND (Autonomous System for Conjecture ExploratioN and verified Deduction) is a local,
auditable workflow for mathematical research and formal verification. Starting from a concise
problem description, it coordinates independent research and adversarial review, writes and
validates a LaTeX manuscript, and attempts Lean verification of the accepted main result.

> [!IMPORTANT]
> ASCEND **must be cited in any scholarly, technical, or public work in which it is used**.
> Generated manuscripts must also contain a Statement of AI Usage naming the ASCEND system
> with GPT 5.6. See [Citation and AI-usage disclosure](#citation-and-ai-usage-disclosure).

| At a glance | Default behavior |
| --- | --- |
| Model access | Official Codex CLI with ChatGPT sign-in; no OpenAI API key required |
| Run outputs | `.ascend/runs/<run-id>/` inside your project |
| Persistent memory | Typed Markdown knowledge graph in `.ascend/knowledge/`, shared across runs |
| Research breadth | A continuous logical coordinator starts 16 diverse workers, then refills a live pool up to 32 active workers; there is no cumulative worker-count cap |
| Parallelism | Configurable; the default Codex run admits up to 32 web-enabled research agents at once |
| Research roles | GPT 5.6 Sol with max coordinator effort and independent xhigh workers; the API adapter also requests pro mode |
| Write boundary | `.ascend/` only, unless `--allow-project-edits` is explicitly supplied |
| Verification | Independent source checks, LaTeX compilation, and deterministic Lean checks |

ASCEND never accepts a model's claim of success as verification. Manuscript generation follows
the research acceptance gate, every citation must pass independent source checks, and
`LEAN_VERIFIED` is issued only after deterministic Lean compiler and placeholder/axiom checks.

The standalone [ASCEND methodology technical report](technical-report/ascend_methodology.pdf)
describes the orchestration, its relationship to the public Cycle Double Cover prompt, the
`challenge.lean` trust boundary, and the framework's current evaluation limitations.

## Quickstart

You need Python 3.11 or newer, Git, and the official Codex CLI. A complete run also requires an
existing Lean/Lake project and a LaTeX distribution with `latexmk`; both can be omitted for a
research-only run.

To have a coding agent prepare and verify the environment, point it to
[`setup-instructions-for-agent.md`](setup-instructions-for-agent.md) and ask it to follow that
document. The guide keeps interactive authentication and privileged changes under user control,
runs the offline-first diagnostics, and does not start a paid or allowance-consuming research run.

### 1. Install and sign in to Codex

On macOS or Linux, use the official standalone installer:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
codex login
codex login status
```

Choose **Sign in with ChatGPT** when prompted. Other official installation methods include
`npm install -g @openai/codex` and, on macOS, `brew install --cask codex`. Windows users should
run ASCEND through WSL2. Consult the current [Codex CLI installation
guide](https://developers.openai.com/codex/cli) before installing or updating.

ASCEND calls `codex login status` for diagnostics. It never reads, copies, or modifies Codex
credential files.

### 2. Install ASCEND

Install directly from the canonical GitHub repository:

```bash
pipx install 'git+https://github.com/PhillipKerger/ASCEND.git'
# or
uv tool install 'git+https://github.com/PhillipKerger/ASCEND.git'
```

### 3. Initialize your project

Run ASCEND inside the existing Git project that should own the research artifacts. For formal
verification, this should also be the relevant Lean/Lake project.

```bash
cd /path/to/your/project
ascend init
cp problem.example.md problem.md
```

Edit `problem.md`, then check the environment and start the run:

```bash
ascend doctor
ascend run problem.md
```

No `OPENAI_API_KEY` is needed. Common alternatives are:

```bash
# Research and manuscript, but no Lean formalization
ascend run problem.md --no-lean

# Research only; no manuscript or Lean formalization
ascend run problem.md --research-only

# Disable model web search and ASCEND's public-identifier HTTP lookups
ascend run problem.md --no-web-search --research-only

# Cap total active workflow time, including later resume attempts
ascend run problem.md --time-limit-minutes 180

# Resolve configuration and show the stage plan without model calls
ascend run problem.md --dry-run
```

## Choosing the research strength and number of agents

The most important research settings are configurable in `ascend.toml`. ASCEND does not use fixed
rounds or a wait-for-all batch. By default its logical coordinator starts sixteen independent
assignments spanning at least four materially different approach families. As results arrive, it
can immediately redirect work and refill or expand the live pool up to 32 active workers. ASCEND
imposes no cumulative research-worker count limit; open-work, concurrency, coordinator-
decision, model-call, cost, token, and optional time budgets remain independent.

This is the closest reproducible analogue to giving the main research prompt to a GPT 5.6 Sol
Ultra research session. “Ultra” describes product/session behavior, not a model ID or API
reasoning setting. ASCEND implements that behavior explicitly: one durable GPT 5.6 Sol logical
coordinator at max effort manages fresh GPT 5.6 Sol xhigh workers. The Responses API adapter also
requests `reasoning.mode = "pro"`; the default Codex path uses Codex CLI's model and
reasoning-effort controls, which do not expose that Responses API field as a separate ASCEND
setting. The coordinator is restored from ASCEND's canonical on-disk scheduler checkpoint, so it
does not depend on hidden hosted multi-agent state or a surviving provider conversation.

For the default Codex backend, this is a useful starting configuration:

```toml
[codex]
model = "gpt-5.6-sol"
research_coordinator_effort = "max"
research_worker_effort = "xhigh"
audit_effort = "xhigh"        # independent proof auditors
max_parallel_agents = 32      # backend-wide concurrent model-call ceiling
max_parallel_web_agents = 32  # concurrent calls that have web search enabled

[codex.limits]
max_research_coordinator_decisions = 256 # second coordinator-decision ceiling
# max_agent_calls = 512       # optional; no call-count ceiling is imposed by default
# max_codex_threads = 512     # optional second global call-count ceiling

[research]
minimum_initial_agents = 16   # initial assignments; configurable down to the safety floor of 4
maximum_concurrent_agents = 32 # research-worker concurrency ceiling
maximum_pending_assignments = 32 # total open (queued plus running) assignment ceiling
maximum_coordinator_decisions = 256 # event-indexed coordinator-decision ceiling
```

Reasoning effort accepts `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, or `max`, subject
to what the selected Codex model and account support. `max` is the default for coordination and
the final research judgment; `xhigh` is the default for workers and independent audits. These are
Codex reasoning-effort values, not a separate Codex `pro` mode. The direct API backend additionally
defaults `reasoning.mode = "pro"` and configures roles separately under
`[api.models.research_coordinator]`, `[api.models.research_worker]`, and `[api.models.audit]`; it
uses `api.max_parallel_agents`, and its usage is bounded by
`api.limits.maximum_cost_usd`. The final research judge uses the coordinator role settings. Codex
has no global call-count ceiling by default, but users may set
`codex.limits.max_agent_calls` or `codex.limits.max_codex_threads` explicitly.

The effective research concurrency is the lowest applicable ceiling. With the defaults and web
search enabled, that is `min(32, 32, 32) = 32` simultaneous research workers. Raising only
`research.maximum_concurrent_agents` therefore has no effect until the corresponding Codex
backend ceilings are also high enough. With `--no-web-search`, the web-agent ceiling no longer
constrains calls at the backend; the current orchestration nevertheless includes that configured
ceiling when it computes its conservative worker-admission window.

These controls have different expected effects:

| Setting | Expected effect | Main tradeoff |
| --- | --- | --- |
| `minimum_initial_agents` | More independent starting approaches and better route diversity | More model calls and allowance usage at bootstrap; the default is 16 and the safety floor is 4 |
| `maximum_pending_assignments` | Allows a larger total open set of queued plus running assignments | A large open set may become stale as new evidence arrives; default 32 |
| `maximum_coordinator_decisions` | Allows more completion- and audit-driven redirects/refills | Potentially much more elapsed time and total usage; default 256 |
| `maximum_concurrent_agents` | Allows up to 32 research workers to run simultaneously by default when backend limits permit | Does not increase research breadth by itself; high concurrency can encounter provider rate limits |
| `research_coordinator_effort` | Gives global synthesis, prioritization, repair planning, and the final research judgment more reasoning effort | `max` can be slower and more allowance-intensive; stronger results are not guaranteed |
| `research_worker_effort` | Gives each independent proof-search call more reasoning effort | Higher effort is slower and more allowance-intensive; default `xhigh` |
| `audit_effort` | Gives fresh independent proof audits a larger reasoning effort | More verification time and usage, but reducing it can make subtle gaps easier to miss |
| `model` | Selects the Codex model used for model-driven stages | Capability, speed, availability, and allowance consumption depend on the selected model/account |
| `max_agent_calls` | Optional hard cap on model calls across research and the rest of the workflow; unset by default | A low value can stop a promising run before later audits, manuscript work, or Lean work |

For a one-off run, the CLI exposes the two most common scheduling controls:

```bash
# At most 32 research workers active at once and 192 coordinator decisions
ascend run problem.md --max-agents 32 --max-coordinator-decisions 192

# Inspect the resolved ceilings, model, and effort without starting any agents
ascend run problem.md --max-agents 32 --max-coordinator-decisions 192 --dry-run
```

The old `--max-rounds` flag and `maximum_rounds`/`maximum_assignments_per_round` configuration
keys are deprecated compatibility inputs. ASCEND translates them into coordinator-decision and
open-assignment budgets for existing scripts; they never restore fixed rounds or a batch
barrier. New configurations should use the names above.

Despite its short name, `--max-agents` sets the maximum *concurrent* research workers; it does
not set a total worker count. Change `minimum_initial_agents`, model/effort levels, backend
parallelism, and call limits in `ascend.toml`. More agents or higher effort can improve coverage,
but neither guarantees a proof; ASCEND still requires every candidate to pass the same
independent audits and final acceptance gate.

### Exactly how research work is assigned

ASCEND has two orchestration layers. The outer `WorkflowRunner` is deterministic application
code: it moves between prompt compilation, research, manuscript, Lean, and reporting. Inside the
research stage, a separate model-driven **logical research coordinator** continuously manages
mathematical search through durable application state. The outer workflow does not itself invent
worker routes.

The prompt flow is:

1. The prompt compiler turns `problem.md` and the preserved framework into the complete compiled
   research prompt (the “big prompt”) and an exact machine-readable claim contract.
2. The GPT 5.6 Sol max-effort coordinator receives that prompt unchanged, the claim contract, and
   the scheduler constraints. The API adapter also requests pro mode. Its first decision creates
   sixteen precise, materially different assignments by default.
3. Every independent GPT 5.6 Sol xhigh worker receives the complete big prompt, the exact
   claim contract, and one assignment containing its route, inputs, expected output, and stopping
   condition, plus a bounded graph slice containing its stable task/target IDs and relevant prior
   dependencies, proofs, counterexamples, sources, and audits. The assignment narrows the route
   but cannot change the target. Workers do not see or coordinate with concurrent workers and
   return structured graph patches instead of editing the vault.
4. When any worker finishes, ASCEND saves its complete raw report and associated
   `research/source-verification/<assignment-id>.json` first, checkpoints the transition with its
   pending-event write-ahead record, creates a sequenced immutable event such as
   `research/events/00000001.json`, clears the pending record, and refreshes the derived
   `research/coordinator/mailbox.json`. It does not wait for all other workers.
5. The coordinator consumes newly useful events together with the unchanged big prompt and claim
   contract, assignment lifecycle state, approach registry, audit obligations, and complete raw
   reports. It can immediately redirect, retire, or add assignments, and the scheduler refills
   available live-pool slots.
6. `research/continuity.json` remains a convenient index of promising, partial, ruled-out, and
   blocked routes, exact gaps, dependencies, prior directives, and audit repairs. The canonical
   scheduler checkpoint, immutable event evidence, and full reports remain available; the index
   never compresses them away.

The coordinator is “continuous” as a logical actor, not one never-ending Codex/API request. Each
activation may use a fresh provider context. `research/coordinator/state.json` is the canonical
atomic scheduler checkpoint; its pending-event write-ahead field makes interrupted event
publication finishable on resume. Immutable event/decision files and hashed raw reports validate
that checkpoint. `research/coordinator/mailbox.json`, assignment files, the registry, and the
continuity index are materialized views. ASCEND does not claim it can reconstruct a deleted or
invalid canonical scheduler checkpoint from evidence alone.

`--max-agents N` controls how many research workers may run concurrently. The initial portfolio
contains 16 assignments by default; the coordinator may refill or expand the live pool to 32
active workers with the default Codex settings, including when web search is enabled. The default
`maximum_pending_assignments = 32` caps the total open set (queued plus running), so 32 active
workers leave no additional queued capacity. There is no separate cumulative research-worker
count cap.
Research-coordinator, worker,
candidate-packager, auditor, and final-judge calls use role-isolated execution contexts; Codex
traces record those roles explicitly. Each worker starts in a fresh context.

If a worker reports a complete proof, ASCEND pauses admission of new workers and packages the
triggering proof immediately; it does not wait for unrelated active workers. A package that still
declares unresolved proof steps fails closed before independent judging. Every structurally
complete package runs all mandatory independent audits plus the final judge. Reports that finish
during this work are saved in the mailbox. If the gate passes, queued work is never launched and
the workflow advances. If the gate fails, the complete available audit reports and exact repair
obligations become high-priority events, the coordinator reacts immediately, and admission
resumes/refills. The result records
`research_subagents_assigned` separately from `research_subagents_used`; these are telemetry, not
cumulative limits. A worker's self-declared success therefore changes scheduling but never
verifies its own proof.

The proof package must explicitly say whether the result is quantitative or algorithmic. The
foundational auditor checks that classification independently and blocks a false negative; an
applicable complexity audit therefore cannot be skipped merely because the packager mislabeled the
candidate.

## Writing `problem.md`

The problem file does **not** need to be a thorough research brief, literature review, or
proposed proof. A short description is enough when it uniquely identifies:

- the mathematical objects and setting;
- the exact question or conclusion to establish; and
- any essential constraints or nonstandard definitions.

A standard problem name, citation, or link is helpful when available. The prompt compiler can
research definitions, background, and existing results before producing the detailed research
prompt used by later agents. It will not guess between materially different targets.

If ASCEND cannot identify one unique problem and success criterion, it exits before launching
research agents. The terminal response and saved run report explain the ambiguity, ask focused
clarification questions, and instruct you to revise the problem file before starting a new run.

## Finding the results

Every invocation gets a unique directory named
`run-<problem-name>[-<run-name>]-<UTC-timestamp>-<random-suffix>`, so the source problem is
recognizable while separate problems and repeated attempts cannot overwrite one another:

```text
.ascend/runs/<run-id>/
├── input/          # preserved problem and resolved invocation/configuration
├── prompts/        # compiled research prompt or clarification request
├── research/       # coordinator state/events, full worker reports, candidates, and audits
├── research-history/ # prior research trees archived by a forced generation/provider migration
├── manuscript/     # paper.tex, references.bib, paper.pdf, and build log
├── lean/           # challenge.lean, Main.lean, iterations, and diagnostics
├── report/         # REPORT.md, report.json, and verification certificate
├── traces/         # run-scoped Codex/API execution traces
└── state.json      # resumable workflow checkpoint
```

The persistent problem memory is deliberately outside every run but still inside ASCEND's safe
write boundary:

```text
.ascend/
├── knowledge/              # Obsidian-compatible Markdown source of truth and dashboards
├── graph-schema.json       # typed node/edge and patch schema
├── graph-index.sqlite      # disposable, rebuildable query index
├── graph-state.json        # revision, hashes, ownership, and operation journal
├── snapshots/              # immutable revision snapshots used by diff/conflict detection
└── locks/graph.lock        # cross-process graph serialization
```

Use `ascend status` for the latest run or `ascend status <run-id>` for a specific run. By
default, ASCEND writes only beneath `.ascend/`; editing project source requires the explicit
`--allow-project-edits` option.

### Persistent knowledge graph and Obsidian

Every run loads and validates the graph for its source problem, creates a distinct run node, and
extends the same stable problem node used by earlier runs. Claims, proofs, audits,
counterexamples, formalizations, sources, tasks, and artifacts remain separate typed notes with
immutable IDs. The Markdown notes and flat YAML frontmatter are authoritative; SQLite is only a
rebuildable index, and ASCEND works normally when Obsidian is not installed.

Open `.ascend/knowledge/` as an Obsidian vault to use `Home.md`, backlinks, typed properties,
dashboard notes, and the four curated canvases. Human prose outside `ASCEND:GENERATED` markers and
note filenames may be edited. Changing an exact claim statement increments its version and marks
dependent proofs/formalizations stale; changing an audited proof requires re-audit. Fields named
`ascend_*` and other typed frontmatter are machine-owned. Conflicting edits fail validation rather
than being overwritten.

Useful offline commands include:

```bash
ascend graph status
ascend graph frontier
ascend graph validate
ascend graph show CLM-...
ascend graph dependencies CLM-...
ascend graph downstream CLM-...
ascend graph tombstone CLM-... --reason "Superseded by the corrected statement"
ascend graph diff REVISION_A REVISION_B
ascend graph export --format mermaid
ascend graph rebuild-index
ascend graph open
```

## How the workflow works

1. **Problem compilation:** identifies the exact target, success criterion, definitions, and
   relevant existing literature.
2. **Research:** starts a diverse parallel portfolio, then adapts the live pool on completion and
   audit events without waiting for fixed rounds.
3. **Adversarial review:** checks proof steps, novelty claims, assumptions, and source metadata
   before accepting a candidate.
4. **Manuscript:** writes the paper only after research acceptance, verifies the bibliography,
   adds the required AI-usage disclosure, and compiles the LaTeX.
5. **Lean confirmation:** asks whether to proceed with formal verification. Answering `n` skips
   Lean and prepares the report; no answer within five minutes proceeds automatically. A
   noninteractive run also proceeds immediately rather than hanging.
6. **Formalization:** audits theorem-statement alignment, attempts Lean formalization, and runs
   deterministic compiler and placeholder/axiom checks.
7. **Reporting:** preserves the evidence, provenance, diagnostics, and authoritative outcome in
   human- and machine-readable reports.

### If the problem is already solved

Prompt compilation explicitly checks whether existing literature fully or partially resolves
the exact target. Any claimed match must have verified primary-source metadata and an exact
comparison of hypotheses and conclusions. Failure to find a match is not evidence of novelty.

When an existing theorem already solves the problem, ASCEND may continue by independently
verifying the match, reconstructing or explaining the proof, preparing an appropriately labeled
manuscript, and attempting Lean formalization. The report and manuscript identify the result as
known literature; an independent reconstruction or formalization is not presented as a new
mathematical theorem.

### Truthful outcomes

Scientific rejection is a valid workflow result, not necessarily a process failure. Reports
distinguish research rejection, accepted proof, manuscript or bibliography failure,
statement-only or partial Lean work, approved-axiom verification, and axiom-free
`LEAN_VERIFIED`. Example reports are available in [`examples/reports`](examples/reports).

## Citation and AI-usage disclosure

Any scholarly, technical, or public work in which ASCEND is used must cite both:

1. the [ASCEND GitHub software repository](https://github.com/PhillipKerger/ASCEND); and
2. the ASCEND whitepaper preprint on arXiv.

The arXiv identifier has not yet been assigned. Do not invent it; replace `ARXIV_ID` with the
canonical identifier after the preprint is published:

```text
ASCEND contributors. ASCEND: Autonomous System for Conjecture Exploration and
Verified Deduction. Software repository,
https://github.com/PhillipKerger/ASCEND.

ASCEND contributors. ASCEND: Autonomous System for Conjecture Exploration and
Verified Deduction. arXiv preprint arXiv:ARXIV_ID.
```

Generated manuscripts must include a **Statement of AI Usage** that names the ASCEND system
with GPT 5.6 and cites both items. For example:

> **Statement of AI Usage.** The ASCEND system with GPT 5.6 was used in the research,
> manuscript-development, and formal-verification workflow for this work. ASCEND's GitHub
> repository and whitepaper preprint on arXiv are cited above.

## Technical reference

### Requirements and platform support

For the recommended Codex setup you need:

- Python 3.11 or newer;
- Git;
- the official Codex CLI;
- a ChatGPT account or workspace with Codex access;
- an existing Lean/Lake project, Lean, and Lake for formal verification; and
- a LaTeX distribution with `latexmk` for manuscript compilation.

Codex access, available models, rate limits, and credits depend on the user's current account
or workspace and may change. Consult the official [Codex pricing and availability
page](https://chatgpt.com/codex/pricing/) rather than assuming a fixed or unlimited allowance.
Research-only runs may omit Lean, Lake, and LaTeX. Runs using `--no-lean` still build the
manuscript and therefore require LaTeX unless manuscript generation is disabled in
configuration.

ASCEND is primarily validated on Linux. On WSL2, install and run both Codex and ASCEND inside
the Linux distribution and keep the Lean project in the Linux filesystem. Homebrew packages for
`git` and `latexmk` support a native macOS setup. Native macOS and WSL2 release validation remain
outstanding; see [`RELEASE_CHECKLIST.md`](RELEASE_CHECKLIST.md).

For development from a source checkout:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

### Model access and billing boundaries

By default, ASCEND invokes the locally installed official Codex CLI and reuses the login created
by `codex login`. With **Sign in with ChatGPT**, ASCEND neither needs nor stores an OpenAI
Platform API key.

This is not offline or local-model execution: Codex communicates with OpenAI and consumes the
Codex allowance or credits available to the signed-in ChatGPT account or workspace. ASCEND does
not promise free, unlimited, or plan-specific usage.

ASCEND never silently switches from Codex mode to API mode. A missing installation, failed
login, unavailable model, usage limit, search failure, or Codex runtime error checkpoints the
run and returns an actionable error; it cannot unexpectedly create Platform API charges.

### CLI reference

| Command | Purpose |
| --- | --- |
| `ascend init [--force]` | Create `ascend.toml`, `.ascend/`, and `problem.example.md` |
| `ascend doctor [--deep] [--online]` | Check local capabilities, login, and optional live backends |
| `ascend run PROBLEM_FILE [options]` | Start a new research workflow |
| `ascend status [RUN_ID]` | Show the latest or selected run status |
| `ascend resume [RUN_ID] [options]` | Continue a checkpointed run |
| `ascend report [RUN_ID] [--rewrite]` | Regenerate the deterministic report, optionally rewriting prose |
| `ascend verify [RUN_ID]` | Re-run integrity, bibliography, LaTeX, and Lean checks without a model |
| `ascend graph COMMAND` | Validate, query, diff, export, rebuild, or open persistent graph memory |

Important `run` options include `--backend codex|api`, `--max-agents`,
`--max-coordinator-decisions`, `--time-limit-minutes`, `--no-web-search`, `--no-lean`,
`--research-only`, `--dry-run`, `--sandbox native|docker`, and `--allow-project-edits`.
Deprecated `--max-rounds` is accepted only to migrate existing scripts to a decision budget; it
does not select round-based execution.

Ordinary `ascend doctor` sends no model prompt. `--deep` explicitly opts into one minimal live
Codex structured-output probe and may consume Codex allowance. `--online` separately probes the
advanced API backend and requires `OPENAI_API_KEY`; it is not needed for Codex mode.

`resume` uses the backend recorded for the run and refuses an accidental provider change. An
explicitly requested provider change is recorded as a provenance event. Successfully returned
calls are checkpointed atomically. `--force-stage STAGE` starts a new call-cache generation
while retaining prior records as audit history; resuming a completed run is a no-op.

Web search remains enabled by default. `ascend run --no-web-search` disables search for every
model stage and disables ASCEND's separate DOI/arXiv/ISBN/URL resolver; the choice is frozen in
the run configuration. `ascend resume RUN_ID --no-web-search` applies the same restriction to
all remaining stages. `ASCEND_NO_WEB_SEARCH=true` is the environment equivalent. Because the
bibliography gate must independently verify every citation, a full run intentionally stops at
that gate when search is disabled; use `--research-only` when an entirely search-free workflow
is desired. ASCEND never treats missing online evidence as verified.

`--time-limit-minutes N` sets one active wall-clock allowance for the complete workflow. The
same limit applies to Codex and API execution, elapsed active time is checkpointed across
resume attempts, and an in-flight model call is cancelled when the remaining allowance expires.
Paused time between commands is not charged. There is no time limit by default;
`ASCEND_TIME_LIMIT_MINUTES=N` is equivalent to the CLI option. Existing configurations that set
`codex.limits.max_wall_clock_minutes` or `api.limits.maximum_wall_clock_hours` remain supported.

`report` is deterministic and offline by default. `--rewrite` is an explicit model call, but its
prose cannot change authoritative statuses, hashes, links, or certificates. `verify` is always a
model-free rerun of deterministic checks.

### Configuration

`ascend init` creates a configuration with these Codex defaults:

```toml
config_version = 2

[backend]
provider = "codex"
allow_automatic_fallback = false

[codex]
executable = "codex"
model = "gpt-5.6-sol"
research_coordinator_effort = "max"
research_worker_effort = "xhigh"
audit_effort = "xhigh"
manuscript_effort = "high"
formalization_effort = "xhigh"
max_parallel_agents = 32
max_parallel_web_agents = 32
persist_sessions = true

[graph]
maximum_context_nodes = 40
maximum_context_characters = 60000
```

See [Choosing the research strength and number of agents](#choosing-the-research-strength-and-number-of-agents)
for the research portfolio, live-pool, open-work, coordinator-decision, effort, model, and
usage-limit controls and how their ceilings interact.

Backend selection precedence is an explicit `--backend` flag, `ASCEND_BACKEND`, project
configuration, and finally the built-in `codex` default. Accepted values are `codex` and `api`.

Legacy v0.1 configurations containing the previous top-level API model or budget sections are
migrated to the namespaced `[api]` layout and retain API behavior. ASCEND prints a one-time
migration notice and does not discard settings or run state.

For safety, `codex.extra_args` accepts only the documented presentation allowlist; ASCEND owns
authentication, workspace, output, sandbox, approval, search, model, and effort flags. Broader
write access cannot be enabled in TOML or the environment. It requires
`--allow-project-edits`, and that consent is recorded with the run.

### Advanced: direct OpenAI API backend

The Responses API backend is available for users who need direct provider control, usage-based
automation, or institutional Platform billing. Select it explicitly:

```bash
export OPENAI_API_KEY='your-platform-api-key'
ascend doctor --online
ascend run problem.md --backend api
```

Or configure it persistently:

```toml
[backend]
provider = "api"
```

OpenAI Platform API billing is separate from ChatGPT subscription billing. The API backend uses
the models, concurrency, budgets, and dated pricing entries under `[api]`; every selected model
must have a pricing entry. Review those entries against the official [API pricing
page](https://developers.openai.com/api/docs/pricing). Never put the key in `ascend.toml`.

### Safety, provenance, and reproducibility

- ASCEND writes only inside `.ascend/` unless `--allow-project-edits` is supplied and recorded.
- Run artifacts never contain credentials or hidden chain-of-thought. Traces retain visible
  outputs, request configuration, public tool/citation metadata, session or response identifiers,
  and usage.
- Completed calls and stage state are saved atomically so interrupted work can be resumed when
  the selected backend supports replay.
- The bundled `resources/prompts/research_prompt_framework.txt` is integrity checked at runtime.
  A modified bundled framework is rejected. Select an intentional custom framework with
  `--framework PATH`; its hash is recorded.
- Failed verification gates remain failed. ASCEND does not silently weaken bibliography,
  manuscript, theorem-alignment, or Lean checks.

See [`SECURITY.md`](SECURITY.md) before changing filesystem, subprocess, authentication, or
logging behavior.

### Optional Docker command sandbox

The optional Docker execution backend applies to configured Lean and LaTeX commands, not to the
host Codex CLI. It uses the image named by `lean.docker_image` (default
`ascend-math-agent:latest`) with networking disabled, a read-only container filesystem, and
`--pull=never`. Build or load the image before selecting `--sandbox docker`; `doctor` verifies
that it is already present.

Each command mounts its resolved working directory at `/workspace`. A concrete stage directory
under `.ascend/runs/<run-id>/` is writable; the project root and other directories are read-only.
The image must already contain the configured LaTeX compiler, Lean/Lake toolchain, and packages.
`ascend verify` currently re-runs frozen deterministic checks natively.

### Troubleshooting

- **`codex` not found:** install or update it with the official Codex CLI guide, ensure it is on
  `PATH`, then run `ascend doctor`.
- **Not signed in:** run `codex login`, choose **Sign in with ChatGPT**, confirm with
  `codex login status`, then rerun `ascend doctor`.
- **Unsupported Codex CLI:** ASCEND checks the exact noninteractive, JSONL, schema, sandbox,
  search, model, configuration, and session capabilities it uses. Update Codex using an official
  installation method; a version string alone is not sufficient.
- **Model unavailable or reasoning effort rejected:** choose an explicit model and effort
  available to the workspace, then resume the checkpointed run. ASCEND does not inherit a
  mutable Codex model default because the executed model is part of durable request identity.
- **Rate, allowance, or credit limit reached:** completed artifacts remain saved. Wait until
  access is available and run `ascend resume RUN_ID`; ASCEND will not switch to API billing.
- **Run time limit reached:** completed calls and artifacts remain checkpointed. Increase the
  frozen allowance explicitly with `ascend resume RUN_ID --time-limit-minutes N` if desired.
- **Live search unavailable:** source-dependent stages stop rather than weakening bibliography
  checks. Restore Codex search or network access, then resume. To intentionally prohibit all
  research-side web access, use `--no-web-search` (normally together with `--research-only`).
- **Git repository required:** run ASCEND inside the intended Git project. Power users may set
  `codex.skip_git_repo_check = true`, but doing so weakens change provenance.
- **Lean/Lake or LaTeX missing:** install the tools reported by `ascend doctor`, use `--no-lean`
  when formalization is intentionally excluded, or use `--research-only` to omit both.
- **WSL2:** install Codex and ASCEND inside WSL2 and keep the project in its Linux filesystem.
- **Use API mode intentionally:** configure `OPENAI_API_KEY` and pass `--backend api`; there is
  no automatic fallback from Codex.

## Support

If `ascend doctor` and the troubleshooting guide do not resolve an issue, open a report in the
[GitHub issue tracker](https://github.com/PhillipKerger/ASCEND/issues). Include the command you
ran, the relevant diagnostic message, and the run's authoritative status. Remove private problem
content and never post credentials. ASCEND is maintained through the canonical GitHub repository.

## Project documentation

- [`ARCHITECTURE.md`](ARCHITECTURE.md) explains the components, state machine, and trust
  boundaries.
- [`CLI_SPEC.md`](CLI_SPEC.md) is the complete command-line contract.
- [`WORKFLOW_SPEC.md`](WORKFLOW_SPEC.md) describes the research and verification stages.
- [`ARTIFACT_CONTRACT.md`](ARTIFACT_CONTRACT.md) defines the run-directory artifacts.
- [`SECURITY.md`](SECURITY.md) documents the threat model and security invariants.
- [`CHANGELOG.md`](CHANGELOG.md) records user-visible changes.

## Development

The default test suite makes no network or model calls:

```bash
ruff check .
ruff format --check .
mypy src
pytest -q
python scripts/verify_project.py
```

Live smoke tests require explicit opt-in and may consume Codex allowance or API funds:

```bash
ASCEND_CODEX_LIVE_TESTS=1 pytest -q -m codex_live
# Also exercise built-in live search in the minimal probe:
ASCEND_CODEX_LIVE_TESTS=1 ASCEND_CODEX_LIVE_SEARCH=1 pytest -q -m codex_live
```

The implementation follows the official [Codex CLI](https://developers.openai.com/codex/cli),
[authentication](https://developers.openai.com/codex/auth), and [non-interactive
mode](https://developers.openai.com/codex/non-interactive-mode) documentation. The advanced API
adapter follows the official [Responses structured-output
guide](https://developers.openai.com/api/docs/guides/structured-outputs) and [web-search
guide](https://developers.openai.com/api/docs/guides/tools-web-search).

## License

ASCEND is available under the MIT License. See [`LICENSE`](LICENSE).
