# Architecture

> **P0 graph-state override:** [GRAPH_ONLY_RESEARCH_STATE.md](GRAPH_ONLY_RESEARCH_STATE.md) is the
> controlling architecture for research memory. Older ledger, opaque-ID, snapshot, migration, and
> graph-state descriptions below are historical and are not part of new-run behavior.

## High-level data flow

```text
problem.md + CLI/environment/project configuration
  -> backend resolver
       -> Codex CLI backend (recommended/default; saved ChatGPT authentication)
       -> OpenAI Responses API backend (advanced; explicit API selection)
  -> intake + normalized source SHA-256 + contract extraction
  -> framework compiler (live search enabled)
       -> explicit assumed target + warning, when several readings are plausible
       -> compiled_research_prompt.md + compiled_problem.json
  -> warning-first target-clause diagnostics + bounded materiality review + target registry
  -> verified prior-literature classification
  -> durable event-driven research coordinator
       -> durable explore/consolidate/bottleneck/adversarial-audit/synthesize phase state
       -> coordinator decisions + assignment lifecycle state
       -> canonical atomic scheduler checkpoint + immutable per-event evidence
       -> materialized mailbox and navigation views
       -> live pool of independent workers
       -> full raw schema-v2 scientific reports + approach registry
       -> private computation collection/CAS/Docker replay (when declared)
       -> deterministic claim/proof-attempt/derivation/obligation admission
       -> archival Markdown + canonical proof ledger + smallest known open cut
       -> blind intermediate-lemma verifier/falsifier lane
       -> targeted counterexample workers
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
`max_pending_assignments` and a scaled `max_coordinator_decisions` budget. Older scheduler names
also migrate to `num_first_level_agents`, `subagents_per_agent`, and the across-tier
`max_concurrent_agents`. The former `maximum_concurrent_agents` counted only first-level workers;
its migration multiplies by the applicable parent-plus-child reservation so frozen runs retain
their effective concurrency. Compatibility
never reintroduces fixed-round scheduling or a batch barrier.

### Workspace

Discover the project root, create `.matek/runs/<run-id>/`, enforce path confinement, and write
files atomically. Reports use relative artifact paths. Generated output and provider traces are
untrusted input.

Each research assignment also receives a private `0700` root at
`research/workspaces/<assignment-id>/`. A worker-capable Codex client is rebound directly to
that canonical root, so Codex's `workspace-write` sandbox and `-C` root are the same directory
and Codex's own control directories (`.agents`, `.codex`, `.git`) are worker-owned state. Its
`scratch/` child is the declared evidence area: computation files beneath it are collected into
`research/computations/blobs/sha256/`, bound by an immutable manifest, and replayed in a fresh
workspace only through an injected backend that attests both filesystem confinement and disabled
networking. The current trusted replay backend is restricted Docker; native replay is refused.
Mutable scratch and replay workspaces are not proof evidence, and a passing replay supports only a
proposed derivation pending mathematical/domain audit.

Research call capacity is owned by one run invocation. Every adaptive-research call creates its
own semaphore from the frozen run configuration; MATEK has no hidden process-global pool. Run
locks are likewise keyed by run ID, while knowledge-graph transactions lock only their selected
graph. `matek status` reports each active run's graph/workspace ownership, requested and effective
capacity, active and queued assignments, and whether a global constraint exists.

### State machine

Run state includes schema version, frozen backend, backend/authentication class, stage statuses,
attempts, artifact hashes, failure information, provider call/session IDs, and cache generation.
Writes use a temporary file and atomic rename. Resume preserves the original provider unless the
user explicitly requests and records a provenance-changing migration.

A prompt-compiler response that identifies several plausible readings is normalized into one
explicit assumed target: the most likely interpretation is frozen, alternatives and ambiguities
are persisted, a warning is surfaced, and research starts. Historical
`NEEDS_PROBLEM_CLARIFICATION` artifacts remain readable, but new compiler ambiguity does not stop
the workflow.

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
   queued-plus-running assignments. The eight-assignment bootstrap, four-child allowance, and
   24-slot across-tier capacity conservatively permit four members of that set to be active
   first-level workers at once.
3. On each completion, atomically persist the entire raw report and its hash, checkpoint the
   transition with a pending-event write-ahead record, create one monotonically sequenced immutable
   event file, clear the pending record, and refresh the mailbox view. The ordering ensures every
   visible completion points to durable evidence and an interrupted event publication can finish
   idempotently. The provider-visible report contains only schema-v2 scientific results,
   obligations, sources, computation declarations, and branch outcome. MATEK then injects the
   application-owned identities and constructs any graph changes deterministically; the worker
   neither authors a `GraphPatch` nor controls status promotion or relation directions.
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
Only security, state-corruption, path-confinement, and immutable-artifact integrity failures
cross the orchestration boundary as hard stops. A write-capable Codex call that changes the user
project outside its run and `.matek` still fails closed, but changes confined to the shared
`.matek` state tree — including a concurrent run's root, worker workspace, or knowledge graph —
are unattributable to one call and are recorded as warnings, never as a reason to stop a run or
restore another run's files.

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

Scientific scheduling is a second, deterministic constraint on that live pool. Its integrity-
protected checkpoint is `research/coordinator/scientific-phase.json`. Persisted frontier signals
advance through `explore`, `consolidate`, `bottleneck`, `adversarial_audit`, and `synthesize`;
phase-specific concurrency is the active ceiling. Exact duplicate plans merge, near-duplicate
mechanisms redirect, and each bottleneck portfolio selects one durable smallest-open-cut
obligation. Successive activations rotate prover, falsifier, computation, transfer-auditor, and
synthesizer roles against that same obligation; a phase transition retires work that is still
queued under the old contract. Synthesis is serialized and may use only audited premises.
Thresholds and concurrency are configurable under `[research.scientific_phase]`.

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
dependency/evidence slice. A worker returns a typed `ResearchWorkerReport` v2, never a persistence
mutation. After the report and source/computation evidence are durable, the service validates
the acyclic same-report `dependency_result_keys` graph plus declared stable dependencies and
targets. It resolves local keys to application-owned nodes and deterministically constructs
canonical claims, proof attempts, derivations, obligations, counterexamples, artifacts, and real
premise relations from server-owned run/assignment/task/approach identity. `(run_id, assignment_id,
result.local_key,
result.schema_version)` is the idempotency key; a payload-hash mismatch under that key is an
admission failure. Exact normalized statement plus scope defines reusable claim identity, so exact
matches share a claim but keep distinct proof attempts while semantic near-matches require audit or
an equivalence derivation. A gap creates an obligation instead of a derivation; a gap-free eligible
result creates only a proposed derivation. The service serializes commits with a project lock,
writes a recovery intent, atomically replaces changed notes and state, saves a revision snapshot,
then rebuilds navigation, proof-ledger, and SQLite views.

Graph nodes carry two ID formats. Agent-authored mathematical content — claims, definitions,
approaches, proofs, proof attempts, derivations, obligations, counterexamples, and experiments —
is minted with descriptive one-liner IDs such as `CLAIM: Halfspaces through the centroid keep at
least a 1/e volume fraction`, built from the reporting agent's `one_liner` (falling back to the
exact statement). Identical statements still coalesce onto one canonical claim because claim and
definition identity is keyed by the exact-statement fingerprint, scope, and assumption contract,
not by the label; a distinct statement reusing a one-liner receives a numeric ` (n)` suffix.
Operational nodes (problems, runs, tasks, audits, artifacts, sources, formalizations, human notes)
keep deterministic `XXX-########` hash IDs, as does the immutable main target claim so its anchor
never moves. The vault stores descriptive IDs in frontmatter and wikilink labels; note directories
use a portable slug with a short digest of the full ID. There are no legacy hash-ID mathematical
nodes in released graphs: `matek graph doctor` covers source identity metadata only.

Candidate packaging re-establishes this mapping from persisted state. Every replay-backed
computation in a triggering report must lie in an exact-main result's transitive local-result
closure; the frozen graph-support slice includes that computation's claim, proof attempt,
derivation, immutable manifest/replay nodes, resolved premise versions, and linked obligations.
An unrelated successful replay cannot satisfy the candidate gate.

The vault is a durable archive; archive membership is not trust. Trust is calculated directly from
the current Markdown statements, statuses, provenance, and descriptive dependency links. Gapped
reports remain partial-progress notes plus open obligations. Trusted claims still require an
independent audit or Lean verification. A malformed proposed link is quarantined locally and never
turns a recoverable scientific report into a stage-wide failure.

Before research, `prompts/target_alignment.json` hash-binds the compiler's theorem and exact claim
contract and records possible conflicts; it is not a mathematical proof. Clause keys control
interpretation so incidental prose cannot recategorize a constants or conclusion clause. Each
check persists its compared structured values and concrete conflicts as typed warnings. Randomness has a
dedicated record for algorithm randomization, arrival randomness, weight-adversary timing,
expectation sources, feasibility mode, and value-guarantee mode; pathwise feasibility is therefore
not an algorithm-type signal. A possible conflict requests one bounded semantic review over the
complete statement and contract. Only `CONFIRMED_CONFLICT` blocks; `NO_MATERIAL_CONFLICT`, unknown
clause keys, malformed output, or unavailable review warns and continues. Generated paraphrases,
abbreviations, deterministic tie-breaking, and negative examples are too weak a basis for aborting
an otherwise valid run.
The first aligned result for a normalized source hash is frozen in `target-registry.json`;
same-source reruns receive those exact statement, contract, and prompt bytes while literature
refresh stays run-local. A new aligned statement with the same contract is recorded as a cosmetic
paraphrase and the frozen bytes remain authoritative. Contract drift fails closed unless the user
confirms `matek run PROBLEM_FILE --migrate-target REASON`, which creates a versioned migration and
invalidates affected proof evidence.

Negation-aware online-decision extraction distinguishes prohibitions from permissions. Once the
target registry's hashes validate, repeated semantic extraction is diagnostic only: disagreements
become warnings rather than state-corruption failures, because source-hash migration and registry
integrity—not heuristic reparsing—govern canonical-target replacement.

Canonical source nodes use verified entity keys with DOI/base-arXiv/MR/ISBN/URL precedence.
Versions, aliases, titles, evidence links, and verification provenance merge only for the same
entity key; title similarity cannot merge distinct identifiers. Unverified title/author records
remain open under provisional fingerprints. Worker scientific admission creates result `CITES`
edges only for explicit result references; verified compiler literature remains separately linked
to the frozen target. Multi-DOI ledger records split at ingestion into one publication source per
normalized DOI. Ambiguous aliases and lower-precedence identifiers fan evidence out to the retained
candidates and commit a structured source-identity decision artifact; this provenance warning is
non-blocking for research scheduling.

Eligible non-main, gap-free results on the current uncapped smallest open cut enter a separate
lemma-audit transaction under `research/lemma-audits/<nomination-id>/`. Its frozen blind packet
contains exact proof steps, source artifacts, and current dependency versions/hashes but omits
worker confidence/status/desired-verdict metadata. Independent verifier and falsifier responses
are immutable and individually resumable. A deterministic passing gate promotes only that
intermediate claim/derivation to `audit_passed`; the gate hard-codes main-target acceptance and
manuscript authorization to false.

Graph nodes distinguish claims, proof attempts, derivations, obligations, audits,
counterexamples, sources, artifacts, and Lean formalizations. Status promotion and staleness are
deterministic application rules. The manuscript and Lean stages consume accepted graph slices,
and their mappings and exact verification records are written back only after existing gates pass.

Fresh coordinator calls carry an explicit bootstrap/continuation/resume activation context,
current and previously observed graph revisions, and a no-hidden-memory reconstruction contract.
The decision attests the reviewed revision before new tasks are accepted. Assignment targets are
validated as live stable nodes in the selected problem; invalid IDs never fall back silently to
the main claim.

Approach families are labels, while assignment IDs define durable branch identity. Separate
assignments in one family therefore create separate approach nodes and registry entries. A
blocked or refuted branch cannot be overwritten by a later productive sibling. Conservative
automatic distillation links counterexamples to the branch that produced them; claim-level
refutation requires an explicit typed result targeting the exact claim contract and independent
scientific review.

Each `.matek/knowledge/<graph-name>/` directory is an ordinary Obsidian-compatible vault and a
separate portable source of truth. New runs normally derive `<graph-name>` from the source
filename stem; `--knowledge-graph NAME` deliberately attaches related work to an already
initialized graph. The chosen name is frozen in run state. Within each vault, descriptive Markdown
notes are the only durable research state. `graph-index.sqlite`, Home, and dashboards are derived
and may be deleted/rebuilt. A per-graph lock and staged transaction manifest make multi-note commits
crash-recoverable and cross-process serialized. This placement preserves the default
no-write-outside-`.matek/` boundary and prevents unrelated problems from sharing memory by default.

Before prompt compilation spends a model call, graph hygiene scans only source nodes for the
active problem. The initial rule normalizes identifiers and repairs a redundant primary identifier
that is absent from the identifier list. It commits through the ordinary graph transaction, so the
Markdown note, navigation, and SQLite index advance together, and writes an append-only
`repairs/` artifact containing the rule and before/after values. Missing stable identifiers
downgrade the source to an unverified warning. Immutable targets, theorem statements, proof claims,
and dependency semantics are not eligible for this resolver; target changes still require explicit
migration.

Retired graph formats have no import or migration path. A new semantic vault starts from Markdown
and can recover only its own interrupted writer transactions.

### Command execution backends

Model execution and deterministic command execution are separate abstractions:

```python
class ExecutionBackend(Protocol):
    async def run(self, request: CommandRequest) -> CommandResult: ...
```

The native and optional Docker command backends run Lean/LaTeX verification commands. Computation-
certificate replay uses the same abstraction but is trusted only when restricted Docker attests
the assignment-stage write boundary and disabled networking; native replay is refused. Docker does
not contain the host Codex CLI by default and never enables provider fallback.

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
- Research resume also integrity-checks and reuses `research/coordinator/scientific-phase.json`,
  immutable computation manifests/CAS blobs/replay verdicts, and lemma nomination/input/gate
  evidence. It calls only a missing lemma-auditor role and never treats mutable scratch or replay
  workspaces as checkpoints.
- `resume` starts at the first incomplete stage with the frozen backend.
- `--force-stage NAME` invalidates that boundary and downstream stages while preserving prior
  provider records as audit history.
- A recoverable Codex error checkpoints and pauses with exact resume obligations; an integrity
  error hard-stops. Neither path ever falls through to API billing.
- Once a run and selected backend exist, a workflow error triggers one best-effort diagnostic-model
  call for a plain-language explanation and suggested recovery. This call uses GPT 5.6 Terra at
  medium effort by default, is accounted and persisted, cannot alter the failure classification,
  and never causes a Codex-to-API fallback. Failure of the explainer is itself warning-only.
- Completed paid/allowance-consuming stages are not repeated merely because report generation
  failed.
