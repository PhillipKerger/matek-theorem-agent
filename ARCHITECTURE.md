# Architecture

## High-level data flow

```text
problem.md + CLI/environment/project configuration
  -> backend resolver
       -> Codex CLI backend (recommended/default; saved ChatGPT authentication)
       -> OpenAI Responses API backend (advanced; explicit API selection)
  -> intake + contract extraction
  -> framework compiler (live search enabled)
       -> clarification request + final report, when no unique target can be identified
       -> compiled_research_prompt.md + compiled_problem.json, otherwise
  -> verified prior-literature classification
  -> durable event-driven research coordinator
       -> coordinator decisions + assignment lifecycle state
       -> canonical atomic scheduler checkpoint + immutable per-event evidence
       -> materialized mailbox and navigation views
       -> live pool of independent workers
       -> full raw reports + approach registry
       -> targeted counterexample/lemma workers
       -> candidate proof package
            -> independent audit suite + final research judge
            `-> failed-audit events return immediately to coordinator
  -> checkpointed LaTeX manuscript writer and bounded repairs
       -> independent bibliography verifier
       `-> LaTeX compiler for every safe draft
  -> durable user confirmation (five-minute default-to-proceed timeout)
  -> Lean feasibility agent
  -> challenge.lean generator
  -> statement alignment auditor
  -> iterative formalization
  -> deterministic Lean verifier
  -> final report
```

The backend changes how model work is executed, not the stage order or acceptance criteria. A
durable manuscript attempt precedes Lean work, but publication readiness is an independent trust
boundary: bibliography, metadata, and section-layout findings do not block statement-aligned
formalization. Claim drift, fabricated citations, unsafe output, or irreparable LaTeX after repair
exhaustion still block downstream promotion as specified. MATEK itself owns agent-role separation,
concurrency, budgets, checkpoints, and independent audits under both providers.

## Model-execution backends

Workflow stages depend on one narrow backend protocol for structured model requests and
results. The application resolves it in this order:

```text
explicit --backend flag
  -> MATEK_BACKEND
  -> [backend].provider in matek.toml
  -> codex
```

Only `codex` and `api` are valid. The selected backend is frozen in run state and provenance.
There is no automatic fallback: a Codex failure cannot initiate an API request or Platform
charge.

### Codex CLI backend — recommended and default

The Codex backend invokes the official CLI noninteractively with an argument array and sends
the prompt on stdin. It reuses the saved authentication managed by `codex login`; MATEK calls
only `codex login status` and never opens a credential file. With ChatGPT authentication, no
Platform API key is required.

The adapter is responsible for:

- installed-capability detection from `codex --help`, `codex exec --help`, and, when used,
  `codex exec resume --help`;
- explicit model and reasoning-effort configuration;
- least-privilege sandbox, approval, working-directory, and search flags;
- run-scoped JSON Schema and final-output paths;
- JSONL event validation, session and usage extraction, redaction, and bounded capture;
- timeout/process-tree cleanup and retryable error classification;
- independent sessions for independent research and audit roles; and
- post-run file-change auditing for write-capable stages.

Ordinary `matek doctor` checks only installation, capabilities, and public login status. It
does not consume model allowance. `matek doctor --deep` is the explicit live probe.

### OpenAI Responses API backend — advanced and optional

The API backend preserves the existing narrow Responses integration. It requires an explicit
`api` provider selection, `OPENAI_API_KEY`, and separately billed Platform access. It owns:

- structured Responses requests;
- model/reasoning/tool configuration;
- provider web-search control and source metadata;
- retry/backoff and incomplete-response handling;
- response IDs, usage, cost accounting, and crash-safe replay; and
- redaction of request and response traces.

No workflow module calls the OpenAI SDK directly.

## Modules

### Configuration

New configurations use schema version 2, `[backend] provider = "codex"`, backend-specific
`[codex]`/`[api]` settings, and `allow_automatic_fallback = false`. Load built-in defaults,
project TOML, environment variables, and CLI overrides with a clear precedence order. Persist
the resolved nonsecret snapshot in every run.

Legacy configurations with the original top-level API model/budget sections migrate to
`provider = "api"` and the namespaced `[api]` layout. Migration retains all values and emits a
one-time notice.

Legacy research keys `maximum_assignments_per_round`, `maximum_rounds`,
`max_research_rounds`, and the CLI `--max-rounds` input migrate to
`maximum_pending_assignments` and a scaled `maximum_coordinator_decisions` budget. Compatibility
never reintroduces fixed-round scheduling or a batch barrier.

### Workspace

Discover the project root, create `.matek/runs/<run-id>/`, enforce path confinement, and write
files atomically. Reports use relative artifact paths. Generated output and provider traces are
untrusted input.

### State machine

Run state includes schema version, frozen backend, backend/authentication class, stage statuses,
attempts, artifact hashes, failure information, provider call/session IDs, and cache generation.
Writes use a temporary file and atomic rename. Resume preserves the original provider unless the
user explicitly requests and records a provenance-changing migration.

A successful prompt-compilation call may terminate with `NEEDS_PROBLEM_CLARIFICATION`. This is a
truthful completed outcome rather than a guessed claim contract: downstream stages are skipped,
clarification questions are persisted, and the final report directs the user to revise the input
and start a new run.

Every stage handoff validates required upstream statuses and recorded artifact hashes before the
next stage can start. The manuscript-to-Lean handoff additionally persists the user's approval,
decline, timeout, or noninteractive default in `lean/consent.json`; resumption reuses that durable
decision.

### Research engine

The research engine is a provider-independent, application-managed actor loop. Its purpose is to
reproduce the useful behavior of a GPT 5.6 Sol Ultra research session without depending on an
`Ultra` API primitive or a hosted multi-agent implementation. The logical coordinator defaults to
`gpt-5.6-sol` with max effort; independent workers default to the same model with xhigh effort.
The Responses API adapter additionally sends `reasoning.mode = "pro"` for both roles. The Codex
adapter uses Codex CLI's model and reasoning-effort controls and does not treat the Responses API
mode field as a Codex setting. Role-specific settings remain configurable within backend
capabilities.

The default Codex hierarchical mode is deliberately inside the existing worker boundary rather
than a second scheduler. MATEK continues to own and checkpoint the first-level pool; a
`research-worker` Codex process alone receives `agents.enabled=true` and
`agents.max_concurrent_threads_per_session=<configured limit>`. Coordinator, audit, manuscript,
and Lean roles do not receive that allowance. Nested agents inherit the parent's sandbox and
search policy, work only one instructed tier deep, and return through the parent worker's single
validated report and aggregate usage record. Explicit flat mode remains available, and the API
adapter visibly resolves to that portable path because it has no nested-agent tool.

`research/coordinator/state.json` is the canonical atomic scheduler checkpoint. Immutable files
under `research/events/<zero-padded-sequence>.json`, immutable coordinator decisions, complete raw
worker/source/audit reports, and their hashes are durable evidence used to validate it. Event
publication is a state-first transaction: the checkpoint temporarily records the complete pending
event, the event file is created idempotently, and a final checkpoint clears the pending field.
`research/coordinator/mailbox.json`, assignment files, the registry, and continuity data are
materialized delivery/navigation views. They can be refreshed from the canonical checkpoint and
evidence, but deleting or corrupting the canonical checkpoint is not advertised as recoverable.
Provider calls may use fresh contexts; application artifacts—not hidden conversation memory—define
the logical coordinator.

The event loop is:

1. Start or restore the logical coordinator with the complete compiled prompt and exact claim
   contract. Its first decision supplies a diverse portfolio of eight assignments by default.
2. Persist and validate the decision, then admit independent workers under research and
   backend-specific semaphores. The default open-work safety ceiling is 1,024
   queued-plus-running assignments; the first-level concurrency limit permits up to eight members
   of that set to be active, each with up to eight nested Codex agents.
3. On each completion, atomically persist the entire raw report and its hash, checkpoint the
   transition with a pending-event write-ahead record, create one monotonically sequenced immutable
   event file, clear the pending record, and refresh the mailbox view. The ordering ensures every
   visible completion points to durable evidence and an interrupted event publication can finish
   idempotently.
   Validate an optional graph proposal only after this scientific transaction. Stale or malformed
   mutations are quarantined as issue events; graph precondition hashes come from the frozen
   revision, never the worker.
4. Wake the coordinator on useful new events. A deterministic context builder always includes the
   original main prompt and claim contract, then packs unacknowledged events, lifecycle and audit
   state, summaries, and prioritized full reports under the measured provider-input ceiling.
   Omitted evidence remains available through hash-bound references and bounded retrieval. The
   coordinator may add, retire, redirect, or package work without waiting for all active workers.
   It may report a real resource boundary or an exact refutation, but an ordinary scientific
   no-progress/reduction stop is durably declined and becomes another coordinator obligation.
5. Persist the zero-padded immutable decision before scheduling its effects, then materialize the
   acknowledgement cursor. Event IDs, decision IDs, assignment IDs, and report hashes make replay
   idempotent after interruption.
6. When a candidate is triggered, pause new admissions and run fresh independent audits plus the
   final judge immediately. In-flight completions still enter the mailbox. Acceptance terminates
   research; failure appends full audit reports and repair obligations as high-priority events,
   wakes the coordinator, and resumes/refills the pool.

Recoverable provider/schema failures are assignment or audit events, not scheduler-wide
exceptions. Workers receive one bounded repair generation. Audits checkpoint independently, so a
candidate with missing checks remains `AWAITING_AUDITS` and resume reuses every committed audit.
Only security, state-corruption, path-confinement, unauthorized-write, and immutable-artifact
integrity failures cross the orchestration boundary as hard stops.

`ResearchContinuityState` is a derived navigation index separating promising, partial, refuted,
and blocked routes with their mathematical evidence. It may help fit a fresh model context, but it
never overwrites or substitutes for the canonical scheduler checkpoint, immutable event evidence,
or full raw reports.

Each coordinator request is preflighted against a conservative 800,000-character hard default
measured after backend framing, while compact/indexed packing reserves at least 5% or 40,000
characters. Its immutable context manifest records the cursor, per-section measurements, included
and omitted artifacts, hashes, character/token estimates, and compaction reason. An exhaustive
artifact catalog is stored once and represented in transport by a path/hash/count descriptor;
graph transport similarly uses one root/revision/index/count descriptor plus capped selected node
summaries. Schema-v3 graph evidence is ranked scientifically before ID tie-breaking, uses typed
digests, caps unrequested full graph nodes at 120,000 serialized characters by default, and
deduplicates substantive exact repeats into authenticated references. The explicit top-level
section order and every score, position, character count, and unused-headroom value are manifest
evidence. Consequential decisions citing omitted evidence become retrieval-only. Legacy manifests
keep their original serialization and replay identity. Knowledge-graph neighborhoods are retrieval
indexes, not proof evidence. Provider size
rejection lowers the effective limit, removes an additional low-priority field, and rebuilds a
smaller request. Compact-state overflow activates an indexed transport view where all cumulative
sections are optional. Only the exact prompt/claim plus provider instructions, output contract,
and envelope can produce `MANDATORY_CONTEXT_TOO_LARGE`; repeated provider rejection has a separate
retriable diagnosis. Candidate packaging
and audits continue to receive complete candidate-specific evidence and retain their strict gates.
There is no cumulative logical-worker ceiling and no fixed-round synchronization barrier.
Total-open-assignment, concurrent-call, coordinator-decision, model-call, cost, token, and
wall-clock limits are separate controls.

The frozen claim contract is the terminal scientific identity throughout this loop. Valid
reductions and special cases remain useful graph/report evidence, but they cannot terminate
research, satisfy candidate packaging, or pass an audit as substitutes for the original problem.

The compiled problem carries a prior-literature classification. Exact known solutions remain
eligible for source verification, proof reconstruction, exposition, and formalization, but must
never be reported as mathematically novel.

Codex internal subagents, when hierarchical mode is enabled, are scoped helpers inside one
first-level worker. They are not substitutes for MATEK's independent roles, durable reports,
auditors, or checkpoints.

### Persistent knowledge graph

The research engine uses a narrow deterministic `KnowledgeGraph` service, not Obsidian. Before
each coordinator activation it queries a typed frontier from authoritative Markdown. Coordinator
assignments become persistent task nodes; each worker receives only a bounded
dependency/evidence slice. Worker output may contain a typed `GraphPatch`, but workers never
write shared notes. The service serializes commits with a project lock, performs optimistic
revision/hash conflict checks, writes a recovery intent, atomically replaces changed notes and
state, saves a revision snapshot, then rebuilds navigation and SQLite views.

Graph nodes distinguish mathematical claims from candidate proofs, audits, counterexamples,
sources, and Lean formalizations. Status promotion and staleness are deterministic application
rules. The manuscript and Lean stages consume accepted graph slices, and their mappings and exact
verification records are written back only after existing gates pass.

Fresh coordinator calls carry an explicit bootstrap/continuation/resume activation context,
current and previously observed graph revisions, and a no-hidden-memory reconstruction contract.
The decision attests the reviewed revision before new tasks are accepted. Assignment targets are
validated as live stable nodes in the selected problem; invalid IDs never fall back silently to
the main claim.

Approach families are labels, while assignment IDs define durable branch identity. Separate
assignments in one family therefore create separate approach nodes and registry entries. A
blocked or refuted branch cannot be overwritten by a later productive sibling. Conservative
automatic distillation links counterexamples to the branch that produced them; claim-level
refutation requires an explicit typed patch and independent scientific review.

Each `.matek/knowledge/<graph-name>/` directory is an ordinary Obsidian-compatible vault and a
separate portable source of truth. New runs normally derive `<graph-name>` from the source
filename stem; `--knowledge-graph NAME` deliberately attaches related work to an already
initialized graph. The chosen name is frozen in run state. Within each vault, `graph-state.json`
stores its name, revision/hashes, ownership baselines, source-problem mappings, processed
operation IDs, and change records; `snapshots/` supports diffs and safe stale rebases.
`graph-index.sqlite` is derived and may be deleted/rebuilt. A pending transaction file plus
`locks/graph.lock` makes each graph's multi-note commits crash-recoverable and cross-process
serialized. This placement preserves the default no-write-outside-`.matek/` boundary and prevents
unrelated problems from sharing memory by default.

### Command execution backends

Model execution and deterministic command execution are separate abstractions:

```python
class ExecutionBackend(Protocol):
    async def run(self, request: CommandRequest) -> CommandResult: ...
```

The native and optional Docker command backends run Lean/LaTeX verification commands. Docker
does not contain the host Codex CLI by default and never enables provider fallback.

### Deterministic verifiers

The LaTeX publication gate and Lean verification gate consume their applicable compiler results,
source scans, hashes, and bibliography evidence. They do not ask a model whether a build or proof
succeeded, and bibliography readiness is not an input to the Lean kernel verdict.

## Dependency direction

```text
CLI -> configuration/backend resolver -> application service -> stages -> domain models
                                                       |-> AgentBackend
                                                       |    |-> Codex CLI adapter
                                                       |    `-> Responses API adapter
                                                       |-> command execution backend
                                                       |-> workspace/state/logging
                                                       `-> deterministic verifiers
```

Domain models do not import the SDK, CLI presentation, or subprocess implementation.

## Resumption semantics

- A stage completes only after its artifacts and integrity hashes are durable.
- Every terminal provider attempt is usage-journaled, including schema-invalid and repair
  attempts; successfully parsed work is then checkpointed before the stage boundary whenever the
  backend supports call/session recovery.
- An interrupted stage preserves completed outputs and diagnostics.
- An interrupted research stage loads the canonical coordinator checkpoint, completes any event in
  its pending-event write-ahead field, and validates its cursor, decisions, completed assignments,
  and hashes against immutable event/evidence files. It refreshes materialized mailbox and index
  views as execution continues. A missing or invalid canonical checkpoint blocks ordinary resume;
  MATEK does not infer scheduler state from the evidence files alone. Completed events are not
  redelivered after acknowledgement, and unacknowledged events are replayed idempotently.
- `resume` starts at the first incomplete stage with the frozen backend.
- `--force-stage NAME` invalidates that boundary and downstream stages while preserving prior
  provider records as audit history.
- A recoverable Codex error checkpoints and pauses with exact resume obligations; an integrity
  error hard-stops. Neither path ever falls through to API billing.
- Completed paid/allowance-consuming stages are not repeated merely because report generation
  failed.
