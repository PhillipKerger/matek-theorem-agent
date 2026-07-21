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
| Research breadth | 8 initial research assignments by default; later agents are chosen adaptively |
| Parallelism | Configurable; the default Codex run admits at most 4 web-enabled research agents at once |
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

The most important research settings are configurable in `ascend.toml`. ASCEND does not use one
fixed agent count: by default it starts with eight assignments spanning at least four materially
different approach families, then may launch targeted agents in later rounds in response to
promising lemmas, counterexamples, and audit findings. The number of agents that run
*simultaneously* is a separate setting from the total number attempted over the run.

For the default Codex backend, this is a useful starting configuration:

```toml
[codex]
model = ""                    # empty: use the current Codex default model
research_effort = "xhigh"     # prompt compiler, coordinator, and research workers
audit_effort = "xhigh"        # independent proof auditors and final judge
max_parallel_agents = 16      # backend-wide concurrent model-call ceiling
max_parallel_web_agents = 4   # concurrent calls that have web search enabled

[codex.limits]
max_agent_calls = 100         # model-call ceiling across the complete workflow
max_research_rounds = 8       # second ceiling on adaptive research rounds

[research]
minimum_initial_agents = 8    # initial assignments; configurable down to the safety floor of 4
maximum_concurrent_agents = 16 # research-worker concurrency ceiling
maximum_research_subagents = 24 # total logical workers across every adaptive round
maximum_assignments_per_round = 24 # coordinator ceiling for initial and later round plans
maximum_rounds = 8            # adaptive research-round ceiling
```

Reasoning effort accepts `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, or `max`, subject
to what the selected Codex model and account support. `xhigh` is the default for Codex research
and audits. The direct API backend instead configures research and audit level/model under
`[api.models.research]` and `[api.models.audit]`, and uses `api.max_parallel_agents`; its total
usage is bounded by `api.limits.maximum_cost_usd` rather than `codex.limits.max_agent_calls`.

The effective research concurrency is the lowest applicable ceiling. With the defaults and web
search enabled, that is `min(16, 16, 4) = 4` simultaneous research workers. Raising only
`research.maximum_concurrent_agents` therefore has no effect until the corresponding Codex
backend ceilings are also high enough. With `--no-web-search`, the web-agent ceiling no longer
constrains calls at the backend; the current orchestration nevertheless includes that configured
ceiling when it computes its conservative worker-admission window.

These controls have different expected effects:

| Setting | Expected effect | Main tradeoff |
| --- | --- | --- |
| `minimum_initial_agents` | More independent starting approaches and better route diversity | More model calls and allowance usage in the first round; the default is 8 and the safety floor is 4 |
| `maximum_research_subagents` | Caps the total logical research workers assigned across the entire adaptive search | A low value can stop before a promising route is repaired; default 24 |
| `maximum_assignments_per_round` | Allows the coordinator to propose more targeted workers in any one round | Raises a ceiling rather than forcing every round to use that many agents; default 24 |
| `maximum_rounds` | More opportunities to repair gaps and pursue audit-directed follow-ups | Potentially much more elapsed time and total usage |
| `maximum_concurrent_agents` | Finishes a given worker batch sooner when backend limits permit | Does not increase research breadth by itself; high concurrency can encounter provider rate limits |
| `research_effort` | Gives compilation, coordination, and proof-search calls a larger reasoning effort | Usually slower and more allowance-intensive; stronger results are not guaranteed |
| `audit_effort` | Gives fresh proof audits and the final judge a larger reasoning effort | More verification time and usage, but reducing it can make subtle gaps easier to miss |
| `model` | Selects the Codex model used for model-driven stages | Capability, speed, availability, and allowance consumption depend on the selected model/account |
| `max_agent_calls` | Hard cap on model calls across research and the rest of the workflow | A low value can stop a promising run before later audits, manuscript work, or Lean work |

For a one-off run, the CLI exposes the two most common scheduling controls:

```bash
# At most 4 research workers active at once, for at most 6 adaptive rounds
ascend run problem.md --max-agents 4 --max-research-subagents 24 --max-rounds 6

# Inspect the resolved ceilings, model, and effort without starting any agents
ascend run problem.md --max-agents 4 --max-research-subagents 24 --max-rounds 6 --dry-run
```

Despite its short name, `--max-agents` sets the maximum *concurrent* research workers; it does
not set a total worker count. Change `minimum_initial_agents`, model/effort levels, backend
parallelism, and call limits in `ascend.toml`. More agents or higher effort can improve coverage,
but neither guarantees a proof; ASCEND still requires every candidate to pass the same
independent audits and final acceptance gate.

### Exactly how research work is assigned

ASCEND has two orchestration layers. The outer `WorkflowRunner` is deterministic application
code: it moves between prompt compilation, research, manuscript, Lean, and reporting. Inside the
research stage, a separate model-driven **research orchestrator** manages mathematical search.
The outer workflow does not itself invent worker routes.

The prompt flow is:

1. The prompt compiler turns `problem.md` and the preserved framework into the complete compiled
   research prompt (the “big prompt”) and an exact machine-readable claim contract.
2. The dedicated research orchestrator receives that complete prompt and claim contract. It also
   receives the remaining total subagent budget and per-round assignment ceiling, then returns a
   structured plan of precise, materially different assignments.
3. Every concurrent research subagent receives the complete big prompt, the exact claim contract,
   and one assignment containing its route, inputs, expected output, and stopping condition. The
   assignment narrows the route but cannot change the target. Workers do not see or coordinate
   with concurrent workers.
4. After a round, ASCEND writes `research/continuity.json` and a round-specific continuity
   snapshot. This explicitly separates promising routes, partial results, ruled-out
   directions and counterexamples, blocked routes with exact gaps, dependencies, prior research
   directives, and audit repair obligations.
5. Every later research-orchestrator call again receives the complete big prompt and claim
   contract, plus that continuity state, the approach registry, and the full visible worker
   reports. This provider-independent handoff preserves mathematical continuity even when the
   backend uses a fresh model context. A ruled-out route should not be restarted without new
   information that changes its status.

`--max-research-subagents N` sets the total logical worker limit across all rounds;
`--max-agents N` only controls how many may run concurrently. The defaults are 24 total logical
research subagents and, with web search enabled, 4 running concurrently. Coordinator, worker,
candidate-packager, auditor, and final-judge calls use role-isolated execution contexts; Codex
traces record those roles explicitly. Each worker starts in a fresh context. A worker claiming a
complete proof triggers the independent acceptance audits; it does not let the research
orchestrator accept its own result.

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
├── research/       # round plans, worker results, candidates, and audits
├── manuscript/     # paper.tex, references.bib, paper.pdf, and build log
├── lean/           # challenge.lean, Main.lean, iterations, and diagnostics
├── report/         # REPORT.md, report.json, and verification certificate
├── traces/         # run-scoped Codex/API execution traces
└── state.json      # resumable workflow checkpoint
```

Use `ascend status` for the latest run or `ascend status <run-id>` for a specific run. By
default, ASCEND writes only beneath `.ascend/`; editing project source requires the explicit
`--allow-project-edits` option.

## How the workflow works

1. **Problem compilation:** identifies the exact target, success criterion, definitions, and
   relevant existing literature.
2. **Research:** sends diverse approaches to parallel workers and adapts later rounds to current
   candidates, failed approaches, and open audit findings.
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

Important `run` options include `--backend codex|api`, `--max-agents`,
`--max-research-subagents`, `--max-rounds`,
`--time-limit-minutes`, `--no-web-search`, `--no-lean`, `--research-only`, `--dry-run`,
`--sandbox native|docker`, and `--allow-project-edits`.

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
model = "" # empty means the user's/current Codex default
research_effort = "xhigh"
audit_effort = "xhigh"
manuscript_effort = "high"
formalization_effort = "xhigh"
max_parallel_agents = 16
max_parallel_web_agents = 4
persist_sessions = true
```

See [Choosing the research strength and number of agents](#choosing-the-research-strength-and-number-of-agents)
for the research portfolio, concurrency, round, effort, model, and usage-limit controls and how
their ceilings interact.

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
- **Model unavailable or reasoning effort rejected:** remove the `codex.model` override to use
  the account's current default, or choose a model and effort available to the workspace, then
  resume the checkpointed run.
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
