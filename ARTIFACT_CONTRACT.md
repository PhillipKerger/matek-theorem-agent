# Artifact Contract

Every run must follow this layout:

```text
.ascend/runs/<run-id>/
├── input/
│   ├── problem.original
│   ├── problem.md
│   ├── invocation.json
│   ├── config.resolved.toml
│   └── environment.json
├── config/
│   ├── effective_config.toml
│   └── backend_manifest.json
├── prompts/
│   ├── framework.txt
│   ├── compiled_research_prompt.md
│   ├── compiled_problem.json
│   ├── prompt_validation.json
│   └── source_ledger.json
├── research/
│   ├── result.json
│   ├── registry.json
│   ├── continuity.json
│   ├── coordinator/
│   │   ├── state.json
│   │   ├── mailbox.json
│   │   ├── requests/<zero-padded-decision-id>.json
│   │   └── decisions/<zero-padded-decision-id>.json
│   ├── events/<zero-padded-sequence>.json
│   ├── assignments/<assignment-id>.json
│   ├── worker-evidence/<assignment-id>.json
│   ├── workers/<assignment-id>.json
│   ├── source-verification/<assignment-id>.json
│   ├── graph-patches/<assignment-id>.json
│   ├── rounds/<round-id>/...  # legacy completed-run compatibility only
│   ├── candidate/
│   │   ├── proof.md
│   │   ├── package.json
│   │   ├── dependency_graph.json
│   │   └── attempts/<candidate-attempt-id>/
│   │       ├── input.json
│   │       ├── evidence.json
│   │       ├── proof.md
│   │       ├── package.json
│   │       ├── source_verification.json
│   │       └── verdict.json
│   ├── audits/
│   │   ├── attempts/<candidate-attempt-id>/*.json
│   │   └── *.json  # materialized latest-attempt views
│   └── verdict.json
├── research-history/  # present after a forced research generation or provider migration
│   └── checkpoint-<generation>[-<suffix>]/...
├── manuscript/
│   ├── paper.tex
│   ├── references.bib
│   ├── claims.json
│   ├── proof_dependency_graph.json
│   ├── bibliography_audit.json
│   ├── bibliography_audit.md
│   ├── paper.pdf
│   └── build.log
├── lean/
│   ├── consent.json
│   ├── FORMALIZATION_INSTRUCTIONS.md
│   ├── formalization.yaml
│   ├── challenge.lean
│   ├── STATEMENT_EXPLANATION.md
│   ├── CLAIM_ALIGNMENT.json
│   ├── Main.lean
│   ├── iterations/<n>/
│   ├── build.log
│   └── axioms.txt
├── report/
│   ├── REPORT.md
│   ├── report.json
│   └── verification_certificate.json
├── logs/
│   ├── events.jsonl
│   ├── usage.jsonl
│   └── redaction.log
├── traces/
│   └── codex/<stage>/<role>/<attempt-id>/
│       ├── schema.json
│       ├── final.json
│       ├── events.jsonl
│       ├── stderr.log
│       └── request.json
└── state.json
```

Persistent graph state is project-scoped and is intentionally not included in a run's immutable
verification-certificate inventory:

```text
.ascend/
├── knowledge/{Problems,Definitions,Claims,Proofs,Approaches,Counterexamples,Experiments,
│   Sources,Tasks,Audits,Formalizations,Runs,Artifacts,Human Notes,Dashboards}/
├── knowledge/Home.md
├── graph-schema.json
├── graph-state.json
├── graph-index.sqlite
├── graph-pending.json       # exists only across an interrupted commit
├── snapshots/<revision>.json
└── locks/graph.lock
```

Markdown notes with typed flat frontmatter are authoritative. `graph-state.json` binds their
content, statement, and machine-owned-field hashes to a revision. SQLite, Home, dashboards, and
canvases are derived and rebuildable. Each run report records the problem ID, graph revision,
vault path, index path, validation warnings, and graph status rather than certifying a mutable
cross-run tree as a run-local artifact.

## Integrity

Record SHA-256 hashes for immutable inputs, accepted proof package, approved theorem statement,
manuscript source, bibliography, and final verification outputs.

Research worker, source-verification, coordinator-decision, candidate-attempt, and audit JSON
artifacts are immutable evidence objects. Their hashes are recorded before a corresponding
monotonically sequenced event becomes visible. Coordinator request payloads are also immutable and
their paths and hashes are bound into the canonical pending-request state before a model call. Each
event is created atomically as one immutable eight-digit file such as
`research/events/00000001.json`; a partial append can therefore never corrupt the entire research
evidence stream.

`research/worker-evidence/<assignment-id>.json` atomically binds the raw worker report, its
provider response ID, and independently checked source-verification result before the separate
worker/source materialized evidence files are published. Likewise, each candidate attempt's
`evidence.json` binds the packaged proof and its source verification before the readable package,
proof, and source files are materialized. Resume replays these committed transactions instead of
rerunning external source checks and risking a different result.

`research/coordinator/state.json` is the canonical atomic scheduler checkpoint. Event publication
uses its `pending_event` field as a write-ahead record: ASCEND first checkpoints the state
transition and complete event payload, creates the immutable event idempotently, then checkpoints
the state with the pending field cleared. Resume completes such a pending publication and validates
the checkpoint against event, decision, assignment, report, and hash evidence.
`research/coordinator/mailbox.json`, `research/assignments/*.json`, `research/registry.json`, and
`research/continuity.json` are materialized delivery/navigation views. They do not supersede the
canonical checkpoint or immutable evidence. Ordinary resume does not promise to reconstruct a
deleted or invalid `research/coordinator/state.json`; that condition fails integrity validation.

The derived registry and continuity indexes never replace, rewrite, or truncate the full raw
reports under `research/workers/`, the full audit reports under `research/audits/`, or the event
evidence under `research/events/`. New runs use immutable, zero-padded event-indexed coordinator
decisions. A `research/rounds/` tree, when present in an already completed legacy run, is preserved
only so its completed `research/result.json` remains readable; it is not live scheduler state and
is not converted into a resumable continuous checkpoint. The root `candidate/`, latest audit files,
`verdict.json`, and `research/result.json` are materialized accepted/latest/final views.
Attempt-scoped JSON evidence remains immutable; `proof.md` is a readable companion to the package's
embedded full proof. An explicit forced prompt/research generation, or an explicit provider
migration while research is incomplete, moves the prior tree to `research-history/` before
creating a fresh canonical scheduler checkpoint.
The operational `logs/events.jsonl` and provider trace JSONL files are diagnostics only and are
not the authoritative research-event ledger.

`research/graph-patches/<assignment-id>.json` records the worker proposal and deterministic merge
result. Full worker evidence is durable before graph integration. Graph commits are idempotent by
operation ID, so resume cannot double-apply a patch; a forced prompt replay reuses the originally
frozen graph memory/context and patch record when it is required to preserve model-call identity.

## Model traces

Store visible model outputs, request configuration, response IDs, tool/citation metadata, and
usage. Do not request or store private chain-of-thought. Reasoning summaries may be stored only
when explicitly configured and should not be required for reproducibility.

`config/effective_config.toml` is the resume source. It changes only after an explicit,
confirmed provider migration. A state-first `pending_backend_migration` intent lets ordinary
resume finish the authorized provider/config switch across either side of a crash;
`backend_manifest.json` and the final report retain the provider,
nonsecret authentication class, CLI/SDK version, requested model/effort, sessions, and observed
usage. A provider migration starts a new cache generation and is recorded in run history.

## Sensitive data

Never persist API keys, bearer tokens, authentication headers, home-directory secrets, or
full environment dumps. Environment capture must use an allowlist.
