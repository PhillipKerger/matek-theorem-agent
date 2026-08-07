# CLI Specification

MATEK uses Typer for typed command parsing and Rich for readable output. All commands present
Codex first as the recommended/default model backend and the direct API as advanced/optional.

## Backend selection

Model-backend resolution is:

```text
explicit --backend codex|api
  -> MATEK_BACKEND
  -> [backend].provider in matek.toml
  -> codex
```

The chosen provider is persisted in run state. MATEK never changes a provider implicitly and
never falls back from Codex to API billing. Resume uses the frozen provider unless a user
explicitly requests a provenance-changing migration.

## `matek init`

Creates a schema-v2 `matek.toml` with `[backend] provider = "codex"`, `.matek/.gitignore`, and
an example problem. It must not overwrite existing files without confirmation or `--force`.
Legacy API configurations are migrated without discarding settings and receive a one-time
notice.

## `matek doctor`

The default command performs no model call and groups output as follows.

### MATEK environment

- supported Python version;
- resolved project/configuration and selected default backend;
- write permissions under `.matek/`; and
- prompt-framework integrity hash.

### Codex backend — recommended/default

- configured `codex` executable and version;
- `codex exec` and `codex login`;
- JSONL, final-output, JSON Schema, sandbox, approval, working-directory, search, model, and
  configuration flags;
- `codex exec resume` when session persistence is enabled; and
- authentication class from `codex login status` only.

MATEK classifies authentication as ChatGPT, API key, access token, authenticated/unknown,
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

`matek doctor --deep` explicitly opts into one minimal live Codex structured-output call with
search enabled. It may consume Codex allowance or credits. Probe artifacts live only in a
temporary directory and are deleted afterward. Ordinary `doctor` never runs this probe.

Every failure includes an exact remediation command. In particular, an unsigned-in user is
directed to run `codex login`, choose **Sign in with ChatGPT**, and rerun `matek doctor`.
Once a run and selected backend exist, workflow errors additionally receive one best-effort
plain-language explanation and suggested recovery from the configured diagnostic model (GPT 5.6
Terra, medium effort by default). The call is accounted, never changes the deterministic error,
and never switches providers. Ordinary `doctor` makes no such call.

## `matek run PROBLEM_FILE`

Without a backend flag, a new installation uses Codex. Important options:

```text
--backend codex|api
--config PATH
--framework PATH
--run-name TEXT
--budget-usd FLOAT
--max-coordinator-decisions INTEGER
--num-first-level-agents INTEGER
--subagents-per-agent INTEGER
--max-concurrent-agents INTEGER
--hierarchical
--flat
--time-limit-minutes INTEGER
--no-web-search
--no-lean
--research-only
--knowledge-graph NAME
--migrate-target REASON
--sandbox native|docker
--allow-project-edits
--dry-run
--yes
--verbose
```

`--max-rounds INTEGER` remains accepted as a deprecated compatibility input. MATEK translates
each historical round into the applicable open-work-capacity number of coordinator decisions (32
under historical defaults); it never creates rounds or a wait-for-all synchronization barrier.
Supplying both the legacy and current decision options is an error.

`--backend api` is explicit consent to use separately billed Platform API access. `--dry-run`
validates and prints the resolved backend and stage plan without a model call.

Before an ordinary `matek run` makes its first model call, it prints a concise resolved
configuration table. The table identifies the backend and no-fallback boundary, backend-aware
models and reasoning settings for the compiler, coordinator, research workers, and audits, the
per-role web policy, effective worker concurrency after backend ceilings, initial and pending pool
sizes, coordinator/time/usage limits, graph, downstream stages, execution sandbox, and project-edit
policy. `--dry-run` prints the same values together with validated input paths and creates no run.

The research defaults use `gpt-5.6-sol`, max coordinator effort, and xhigh worker effort. The
Responses API adapter sends `reasoning.mode = "pro"` for those roles; the Codex adapter uses the
Codex CLI model and reasoning-effort controls and has no separate MATEK `pro` switch. No
`--ultra` option exists: Ultra-like research behavior comes from the durable application-level
coordinator and live pool, not a provider parameter.

`--no-web-search` disables web search in every model stage and disables MATEK's deterministic
public-identifier HTTP resolver. Search remains enabled by default. The resolved setting is
saved with the run; initial research workers and later refills share the same derived first-level
pool and search policy. The same flag on `matek resume` disables it for all
remaining stages.
Unverifiable citations remain unverified, so this option never weakens the bibliography gate and
a fully offline run should normally also use `--research-only`.

Unless `--knowledge-graph` is supplied, the problem filename without its extension is normalized
to a portable graph name (`My Problem.md` becomes `my-problem`) and the run uses
`.matek/knowledge/<graph-name>/`. `--knowledge-graph NAME` is an intentional reuse operation for
related or follow-up work: the named graph must already exist. The choice is recorded in run state
and resume always uses that frozen graph.

The normalized source-problem SHA-256 keys the graph's canonical target. The first aligned
statement, claim contract, and compiled prompt are frozen; later runs with unchanged source bytes
reuse them even when a new compiler call paraphrases the target. With the contract unchanged, that
incoming wording is recorded as a cosmetic paraphrase and does not replace the frozen bytes; a
contract change fails closed.
`--migrate-target REASON` is the only material-migration authorization: it appears in the resolved
plan, requires interactive confirmation unless `--yes` is present, records a versioned reason,
and invalidates affected proof evidence. The authorization is persisted with the new run and
feeds its durable target-migration event; ordinary resume does not ask again.

`--time-limit-minutes N` sets the total active wall-clock allowance across prompt compilation,
research, manuscript work, and formal verification. Elapsed active time is stored in run state
and carried into resume; time while MATEK is not running is excluded. The remaining allowance
also bounds each in-flight model call. The default is 900 minutes (15 active hours).
`MATEK_TIME_LIMIT_MINUTES=N` is the environment form.

`--num-first-level-agents N` sets the independent bootstrap portfolio size; the default is eight
and the safety floor is four. `--max-concurrent-agents N` sets an across-tier research-agent
capacity; the default is 24. In hierarchical mode each admitted first-level worker conservatively
reserves one parent slot plus its complete `--subagents-per-agent` allowance because internal
Codex descendant activity is not visible to MATEK's application semaphore. The default child
allowance is four, so the 24-slot capacity admits four first-level workers at once while all eight
bootstrap assignments remain queued. `--max-agents` remains a deprecated compatibility input for
the former first-level-only ceiling.

`research.max_pending_assignments` defaults to a high 1,024-open-assignment safety ceiling, and
`research.max_coordinator_decisions` defaults to 100,000 event-indexed decisions. The derived
first-level concurrency limit controls the active subset of that open set. None of these settings
imposes a separate cumulative logical-worker limit. Codex global call-count limits remain
configurable in TOML but are unset by default.

Hierarchical execution is the Codex default. `--hierarchical` explicitly enables it and `--flat`
disables nested delegation.
`--subagents-per-agent X` sets the maximum concurrently open Codex subagents available to each
first-level worker. The hierarchical default is four nested agents per first-level worker; zero
means that workers are explicitly instructed to operate as regular subagents and Codex nested
tools are not enabled. The coordinator and worker inputs contain both resolved limits. Nested
agents inherit the parent worker's sandbox and web policy, cannot delegate further under MATEK's
contract, and must be checked and synthesized by the parent into its ordinary durable report.
The Responses API adapter has no nested-agent tool and visibly resolves to portable flat
execution.

`research.max_coordinator_context_characters` defaults to 800,000 and applies to the final
serialized provider input rather than raw report text alone. Compact requests reserve at least 5%
or 40,000 characters, so the normal target is at most 760,000. `matek run` displays this ceiling
and the bounded on-demand evidence-request limit. Exhaustive artifact and graph indexes stay on
disk; transport carries high-priority entries plus authenticated descriptors and capped summaries.
Unrequested full graph nodes have a separate configurable 120,000-character default section cap;
explicit requests bypass that section cap while remaining subject to the 800,000-character
provider-input ceiling.
Every optional section is prunable. Only the exact prompt/claim plus provider instructions, output
contract, and envelope may pause as `MANDATORY_CONTEXT_TOO_LARGE`; repeated provider rejection is
reported separately with a smaller resumable generation.

The worker pool is additionally bounded by the active scientific phase. MATEK persists
`explore`, `consolidate`, `bottleneck`, `adversarial_audit`, and `synthesize` state, merges exact
duplicate plans, redirects near-duplicate mechanisms, and selects one durable cut obligation for a
bottleneck portfolio. Across activations it rotates independent prover, hostile falsifier, small-
case computation, transfer auditor, and synthesizer roles against that obligation. Phase changes
retire assignments still queued under the old contract. Thresholds and phase concurrency are
configured under `[research.scientific_phase]`; they do not weaken the overall worker/backend
ceilings.

```toml
[research.scientific_phase]
no_audited_progress_assignments = 8
unchanged_cut_snapshots = 4
repeated_gap_threshold = 3
similarity_threshold = 0.86
blocked_or_refuted_ratio = 0.60
bottleneck_maximum_size = 3
bottleneck_attempts_before_audit = 5
explore_concurrency = 8
consolidate_concurrency = 4
bottleneck_concurrency = 3
adversarial_concurrency = 2
```

Synthesis is serialized at one active assignment so concurrent workers cannot produce competing
terminal assemblies from the same audited premises.

`--sandbox native|docker` selects the deterministic command backend used for Lean, LaTeX, and
computation-certificate replay; it never moves the host Codex CLI into Docker. Collection and CAS
storage are backend-independent, but trusted replay currently requires restricted Docker's
attested filesystem confinement and disabled networking. Native mode records an unsafe-backend
verdict and leaves the computation outside the ledger; the API adapter has no worker filesystem
tool authority. Even a passing Docker replay yields proposed evidence pending mathematical/domain
audit.

Generated run directories use
`run-<problem-file-stem>[-<run-name>]-<UTC-timestamp>-<random-suffix>`. The problem stem and
optional run name are normalized to portable, lowercase filesystem-safe components.

During `run` and active `resume` operations, MATEK prints sparse progress lines with stable
high-level milestone numbers. It does not stream model reasoning, per-call diagnostics, or every
worker completion. A full run may show:

```text
ASCENSION 0: Fetching problem.
ASCENSION 1: Formulating technical research prompt.
ASCENSION 2: Starting continuous research coordinator.
ASCENSION 3: Managing adaptive research pool: 8 initial assignments, up to 4 active
first-level agents, each with up to 4 nested subagents, within 24 reserved agent slots.
ASCENSION 4: Packaging the candidate solution for independent audits.
ASCENSION 5: Writing manuscript and verifying bibliography.
ASCENSION 6: Assessing and verifying the Lean formalization.
ASCENSION 7: Preparing final report.
```

In hierarchical mode Ascension 3 appends the per-agent nested limit.

On resume, Ascension 2 prints `Resuming continuous research coordinator at event N.` using the
canonical checkpoint's event cursor. Ascension 3 then uses the same adaptive-pool wording with the
persisted initial count and effective concurrency. They do not repeat at artificial batch
boundaries; candidate-audit milestones may recur for distinct candidate attempts. Skipped or
already checkpointed stages do not print misleading progress lines.

After `run` or `resume` returns a finalized report, MATEK prints a deterministic terminal summary
derived from that report. It answers whether the exact problem passed the scientific acceptance
gate, identifies where execution stopped, summarizes worker/coordinator/audit activity and stage
coverage, shows the strongest retained result and next action, lists remaining obligations, and
prints complete paths to the report and run artifacts. Terminal summarization does not make an
additional model call.

After research is accepted and a safe manuscript draft is durable, an interactive full run asks.
Publication warnings or a failed bibliography gate do not misrepresent the scientific result and
do not independently block statement-aligned formalization:

```text
The accepted result is ready. Proceed with formal Lean verification? [Y/n]
```

`n` skips Lean and prepares the final report. An empty/affirmative answer proceeds. If the user
does not answer within five minutes, MATEK proceeds automatically. Noninteractive invocations
also proceed immediately rather than hanging. The decision is durable and is not asked again on
ordinary resume.

## `matek status [RUN_ID]`

Shows one backend summary, a `Research roles:` line with configured coordinator/worker models and
efforts, run-owned graph/workspace paths, requested and effective capacity, the stage table,
aggregate usage and elapsed time, and recorded artifact paths. When the
canonical research checkpoint exists, it also prints `Research coordinator:` with phase, decision
count, the acknowledged-through event cursor, and queued, active, and completed assignment counts.
That `phase` is the coordinator scheduler lifecycle, not the separate scientific phase stored in
`research/coordinator/scientific-phase.json`. API runs may show calculated dollar cost; Codex runs
must not invent a dollar cost for subscription allowance. If the run ID is omitted, use the latest
run in the current project and first list every active run independently. MATEK has no hidden
process-global capacity pool, so status reports `global wait none`; any future explicit global cap
must instead report its effective allocation and wait reason.

The status header prints `Scientific:` and `Workflow:` separately. For a paused candidate attempt
it also shows completed and missing mandatory audits, so `CANDIDATE_AWAITING_AUDIT` is not confused
with scientific rejection and `PAUSED_RETRIABLE` has a concrete resume target.

## `matek resume [RUN_ID_OR_PROBLEM]`

Resumes the first incomplete stage with the provider stored in run state. Options include
`--backend codex|api`, `--force-stage STAGE`, and backend-appropriate budget increases.

The positional selector may be a run ID or a `.md`/`.txt` problem path. For a problem path, MATEK
matches the immutable absolute source path in each run's intake record and resumes the newest
matching run. It does not guess from a similar run-name slug.

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

## `matek report [RUN_ID]`

Regenerates report products from existing artifacts without changing upstream scientific
artifacts. It is offline by default. `--rewrite` is the only model-assisted report option and
uses the run's selected provider; model prose cannot override deterministic statuses or hashes.

## `matek verify [RUN_ID]`

Re-runs deterministic LaTeX, bibliography consistency, file-integrity, and Lean checks without
calling either model backend. These subprocess checks currently use the native command backend,
even when the frozen run used Docker.

## `matek graph`

Graph commands are local and model-free:

- `matek graph list` lists initialized graph names and vault paths.
- `matek graph init GRAPH_NAME` creates `.matek/knowledge/<graph-name>/`, its schema/state,
  initial snapshot, navigation, canvases, and SQLite index. `matek init` does not create an
  identity-free graph.
- `matek graph validate` checks Markdown parsing, stable IDs, machine ownership, endpoint/type
  constraints, dependency cycles, hashes, and index revision; invalid graphs exit 6.
- `matek graph status` renders a typed machine-readable summary. `frontier [--problem-id ID]`
  includes `main_target`, `live_derivations`, `strongest_audited_results`, `open_obligations`,
  `smallest_known_open_cut`, and `open_cut_search_capped`.
- `matek graph doctor [--repair] [--problem-id ID]` inspects generated source metadata without
  model calls. `--repair` applies only whitelisted local invariants in one recoverable graph
  transaction, records an append-only before/after artifact, and rebuilds derived projections.
  It never changes the canonical target or mathematical proof dependencies.
- `matek graph rebuild-index` recreates SQLite from authoritative Markdown.
- `matek graph open` attempts Obsidian and otherwise succeeds gracefully while printing the
  vault path for manual opening.
- `matek graph export [--format json|graphviz|mermaid] [--output PATH]` exports without Obsidian.
- `matek graph diff REVISION_A REVISION_B` compares immutable snapshots.
- `matek graph reconstruct REVISION [--output PATH]` integrity-checks and reconstructs one full
  snapshot; legacy revisions preserve their exact stored bytes.
- `matek graph verify-snapshots [REVISION]` verifies one revision or the complete snapshot history,
  including manifest/parent roots, checkpoints, blob digests, graph records, and revision identity.
- `matek graph migrate-legacy [--problem-id ID] [--target-claim-id ID]
  [--audit-nomination-limit N] [--output PATH] [--dry-run] [-g NAME]` is the default read-only
  planning form. It emits an integrity-protected report and refuses output beneath
  `.matek/knowledge/`.
- `matek graph migrate-legacy --apply-plan PLAN [--yes] [-g NAME]` applies the exact externally
  reviewed plan. `PLAN` must be a nonsymlinked regular file outside `.matek/knowledge/`; `--output`
  cannot be combined with this form. MATEK verifies integrity, graph identity, source revision,
  archive digest/count, claim versions, and graph constraints, then asks for confirmation unless
  `--yes` is present. A stale, wrong-graph, or tampered plan fails without mutation.
- `show`, `dependencies`, `downstream`, `stale`, and `tasks` provide focused graph queries.
- `tombstone NODE_ID --reason TEXT` preserves an obsolete identity and invalidates dependents;
  managed notes must not be deleted directly.

Every query/maintenance command accepts `--knowledge-graph NAME` or `-g NAME`. It auto-selects
only when exactly one initialized graph exists; with multiple graphs an explicit selection is
required, and an unknown name is an error.

The vault lives beneath `.matek/` so these commands do not imply consent to edit project source.
Migration-plan output is user-selected, must remain outside every graph vault, and contains
proposals rather than applied trust. Confirmed application is one recoverable/idempotent graph
commit that preserves legacy nodes and all earlier snapshots as archive evidence. It creates only
proposed typed derivations and queued verifier/falsifier audit tasks, makes no model call, and
records the result at `ledgers/migrations/<plan-sha256>.application.json`.

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
Recoverable provider, schema, evidence, scientific, and resource failures likewise return a
truthful paused or partial status after checkpointing. Artifact/state corruption, unsafe paths,
security failures, and unauthorized writes retain hard-failure exit semantics.
An input with several plausible mathematical targets proceeds under the most likely explicitly
stated assumption. The terminal output, status, and report surface that assumption and warning.
