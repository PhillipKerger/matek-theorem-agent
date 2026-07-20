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
  -> adaptive research coordinator
       -> round plans
       -> parallel independent workers
       -> approach registry
       -> targeted counterexample/lemma workers
  -> candidate proof package
  -> independent audit suite
  -> final research judge
  -> LaTeX manuscript writer
  -> bibliography verifier
  -> LaTeX compiler
  -> durable user confirmation (five-minute default-to-proceed timeout)
  -> Lean feasibility agent
  -> challenge.lean generator
  -> statement alignment auditor
  -> iterative formalization
  -> deterministic Lean verifier
  -> final report
```

The backend changes how model work is executed, not the stage order or acceptance criteria.
The manuscript and bibliography gate always precedes Lean work. ASCEND itself owns agent-role
separation, concurrency, budgets, checkpoints, and independent audits under both providers.

## Model-execution backends

Workflow stages depend on one narrow backend protocol for structured model requests and
results. The application resolves it in this order:

```text
explicit --backend flag
  -> ASCEND_BACKEND
  -> [backend].provider in ascend.toml
  -> codex
```

Only `codex` and `api` are valid. The selected backend is frozen in run state and provenance.
There is no automatic fallback: a Codex failure cannot initiate an API request or Platform
charge.

### Codex CLI backend — recommended and default

The Codex backend invokes the official CLI noninteractively with an argument array and sends
the prompt on stdin. It reuses the saved authentication managed by `codex login`; ASCEND calls
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

Ordinary `ascend doctor` checks only installation, capabilities, and public login status. It
does not consume model allowance. `ascend doctor --deep` is the explicit live probe.

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

### Workspace

Discover the project root, create `.ascend/runs/<run-id>/`, enforce path confinement, and write
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

The application-managed coordinator loop is provider-independent:

1. The coordinator creates a diverse `ResearchRoundPlan` with independent assignments.
2. Workers run concurrently under backend-specific semaphores.
3. The coordinator ingests visible reports and updates the `ApproachRegistry`.
4. It chooses focused follow-up work, candidate packaging, or a budget-aware stop.
5. Fresh independent audits and the final judge gate any candidate.

The compiled problem carries a prior-literature classification. Exact known solutions remain
eligible for source verification, proof reconstruction, exposition, and formalization, but must
never be reported as mathematically novel.

Codex internal subagents are not a substitute for ASCEND's independent roles and checkpoints.

### Command execution backends

Model execution and deterministic command execution are separate abstractions:

```python
class ExecutionBackend(Protocol):
    async def run(self, request: CommandRequest) -> CommandResult: ...
```

The native and optional Docker command backends run Lean/LaTeX verification commands. Docker
does not contain the host Codex CLI by default and never enables provider fallback.

### Deterministic verifiers

LaTeX and Lean gates consume compiler results, source scans, hashes, and bibliography evidence.
They do not ask a model whether a build or proof succeeded.

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
- Successfully returned provider work is checkpointed before the stage checkpoint whenever the
  backend supports call/session recovery.
- An interrupted stage preserves completed outputs and diagnostics.
- `resume` starts at the first incomplete stage with the frozen backend.
- `--force-stage NAME` invalidates that boundary and downstream stages while preserving prior
  provider records as audit history.
- A Codex error checkpoints and stops; it never falls through to API billing.
- Completed paid/allowance-consuming stages are not repeated merely because report generation
  failed.
