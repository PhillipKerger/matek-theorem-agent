# ASCEND: An Orchstrator for Agentic Mathematical Research with Lean Verification

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
| Parallelism | Up to 8 agents, including up to 2 web-enabled agents |
| Write boundary | `.ascend/` only, unless `--allow-project-edits` is explicitly supplied |
| Verification | Independent source checks, LaTeX compilation, and deterministic Lean checks |

ASCEND never accepts a model's claim of success as verification. Manuscript generation follows
the research acceptance gate, every citation must pass independent source checks, and
`LEAN_VERIFIED` is issued only after deterministic Lean compiler and placeholder/axiom checks.

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

# Resolve configuration and show the stage plan without model calls
ascend run problem.md --dry-run
```

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

Every invocation gets a unique timestamp-based directory, so separate problems and repeated
attempts do not overwrite one another:

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

Important `run` options include `--backend codex|api`, `--no-lean`, `--research-only`,
`--dry-run`, `--sandbox native|docker`, and `--allow-project-edits`.

Ordinary `ascend doctor` sends no model prompt. `--deep` explicitly opts into one minimal live
Codex structured-output probe and may consume Codex allowance. `--online` separately probes the
advanced API backend and requires `OPENAI_API_KEY`; it is not needed for Codex mode.

`resume` uses the backend recorded for the run and refuses an accidental provider change. An
explicitly requested provider change is recorded as a provenance event. Successfully returned
calls are checkpointed atomically. `--force-stage STAGE` starts a new call-cache generation
while retaining prior records as audit history; resuming a completed run is a no-op.

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
max_parallel_agents = 8
max_parallel_web_agents = 2
persist_sessions = true
```

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
- **Live search unavailable:** source-dependent stages stop rather than weakening bibliography
  checks. Restore Codex search or network access, then resume.
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
