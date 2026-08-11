# Artifact Contract

> **P0 graph-state override:** [GRAPH_ONLY_RESEARCH_STATE.md](GRAPH_ONLY_RESEARCH_STATE.md)
> supersedes the historical graph/ledger layout below. New graphs contain descriptive Markdown,
> optional disposable derived views, incidents, and per-graph transaction scratch only.

Every run must follow this layout:

```text
.matek/runs/<run-id>/
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
│   ├── target_alignment.json
│   ├── prompt_validation.json
│   └── source_ledger.json
├── research/
│   ├── result.json
│   ├── registry.json
│   ├── continuity.json
│   ├── coordinator/
│   │   ├── state.json
│   │   ├── scientific-phase.json
│   │   ├── mailbox.json
│   │   ├── requests/<zero-padded-decision-id>.json
│   │   ├── context-manifests/<decision-id>-<generation>.json
│   │   └── decisions/<zero-padded-decision-id>.json
│   ├── events/<zero-padded-sequence>.json
│   ├── assignments/<assignment-id>.json
│   ├── worker-evidence/<assignment-id>.json
│   ├── workers/<assignment-id>.raw.json
│   ├── workers/<assignment-id>.json          # normalized schema-v2 report
│   ├── source-verification/<assignment-id>.json
│   ├── worker-computation/<assignment-id>.json
│   ├── workspaces/<assignment-id>/scratch/... # mutable private 0700 workspace
│   ├── computations/
│   │   ├── manifests/<assignment-id>.json
│   │   ├── blobs/sha256/<sha256>
│   │   ├── replay-workspaces/<assignment-id>/<manifest-sha256>/... # mutable/non-proof
│   │   └── replays/<assignment-id>/<manifest-sha256>/
│   │       ├── verdict.json
│   │       └── attempts/<eight-digit-attempt>.json
│   ├── lemma-audits/
│   │   ├── selections/<assignment-id>-<selection-digest>.json
│   │   └── <nomination-id>/
│   │       ├── nomination.json
│   │       ├── input.json
│   │       ├── responses/{lemma-verifier,lemma-falsifier}.json
│   │       ├── gate-checkpoints/<gate-sha256>.json
│   │       ├── gate.json
│   │       └── legacy-v1/                 # present after a v1 upgrade
│   │           ├── manifest.json
│   │           ├── input.json
│   │           ├── responses/{lemma-verifier,lemma-falsifier}.json
│   │           └── gate.json
│   ├── counterexample-audits/<audit-id>/
│   │   ├── nomination.json
│   │   ├── policy.json
│   │   ├── requests/{counterexample-verifier,counterexample-falsifier}.json
│   │   ├── responses/{counterexample-verifier,counterexample-falsifier}.json
│   │   ├── gate-checkpoints/<gate-sha256>.json
│   │   └── gate.json
│   ├── graph-patches/<assignment-id>.json    # legacy-named application admission record
│   ├── issues/<issue-id>.json
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
│   ├── drafts/<revision>/
│   │   ├── paper.tex
│   │   ├── references.bib
│   │   ├── validation.json
│   │   ├── bibliography_audit.json
│   │   ├── source_verification.json
│   │   └── build.log
│   ├── paper.tex
│   ├── references.bib
│   ├── claims.json
│   ├── proof_dependency_graph.json
│   ├── bibliography_audit.json
│   ├── bibliography_audit.md
│   ├── validation.json
│   ├── result.json
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
.matek/
└── knowledge/<graph-name>/
    ├── {Problems,Definitions,Claims,Partial Progress,Approaches,Obligations,
    │   Counterexamples,Experiments,Sources,Tasks,Incidents,Dashboards}/
    ├── Home.md
    ├── graph-index.sqlite
    └── .transactions/
        ├── writer.lock
        └── admission-*/manifest.json # present only while recovering an interrupted commit
```

Markdown notes and their descriptive wiki links are the sole durable research authority. SQLite,
Home, and dashboards are derived and rebuildable. The graph writer stages each admission beneath
`.transactions/`, publishes note replacements atomically, and recovers a retained manifest after
interruption. Run-local scheduler checkpoints are not a second graph authority.

## Integrity

Record SHA-256 hashes for immutable inputs, accepted proof package, approved theorem statement,
manuscript source, bibliography, and final verification outputs.

`prompts/target_alignment.json` binds the compiled normalized statement and the canonical sorted
claim contract to separate SHA-256 digests. Schema v2 records every diagnostic clause check,
compared values, typed warning origin, and the optional bounded materiality-review verdict and
provenance. Its dedicated randomness record keeps
algorithm type, arrival model, adversary timing, expectation sources, feasibility mode, and value
mode distinct, so pathwise feasibility cannot negate a randomized algorithm. Extractor conflicts
are preserved for diagnosis but do not block. Only an LLM review recorded as
`CONFIRMED_CONFLICT` blocks research; absent, uncertain, malformed, or unavailable review is
warning-only. Passing alignment does not certify semantic equivalence or mathematical truth.
`target-registry.json` is keyed by the normalized source-problem SHA-256 and integrity-binds the
canonical exact statement, contract JSON, compiled prompt, target node, version, compatibility
observations, and explicit migrations. Same-source reruns re-materialize the canonical statement,
contract, and compiled-prompt bytes into their prompt artifacts. Without an explicit migration, a
same-contract wording change is recorded as a cosmetic paraphrase without replacing them. Only a
confirmed `matek run PROBLEM_FILE --migrate-target REASON` may authorize contract drift; the
authorization is durable across resume and the migration records invalidated evidence.

The Markdown notes remain the authoritative complete archive. Mathematical trust is selected
directly from current note statuses, statements, provenance, and descriptive dependency links.
Gapped, partial, assumption-bearing, or ambiguous findings remain visible but do not become trusted
support. `audit_passed` and `lean_verified` are the only direct trusted claim statuses; proof
support propagates only through currently resolved descriptive dependencies.

Source notes use descriptive citation titles and retain checked identifiers and verification prose
in their bodies. Only exact independently verified identifiers can support a manuscript citation.
Ambiguous identities remain open notes rather than being merged by title similarity.

There is no legacy-migration artifact contract. Retired graphs are restarted as clean semantic
vaults.

Research worker, source-verification, coordinator-decision, candidate-attempt, and audit JSON
artifacts are immutable evidence objects. Their hashes are recorded before a corresponding
monotonically sequenced event becomes visible. Coordinator request payloads are also immutable and
their paths and hashes are bound into the canonical pending-request state before a model call.
Every request is paired with an immutable context manifest recording its event cursor, normal,
compact, or indexed mode, final provider-input character count, per-section serialized sizes,
token estimate, payload hash, inclusion reasons, aggregated events, omitted state sections,
omitted authenticated references, effective limit, packing target, and reserved headroom. Compact
schema-v3 manifests additionally record payload/section-order versions, scientific evidence score
components and frontier categories, full/summary selection locations, section positions, unused
headroom, exact redundant characters removed, and the serialized unrequested `full_graph_nodes`
size. New payloads use an intentional top-level section order; legacy manifests retain their
original alphabetic serialization and exact replay identity. Compact
requests include only high-priority artifact references plus a path/hash/count descriptor for the
complete immutable catalog under `research/coordinator/artifact-catalogs/`. They similarly carry
one compact graph descriptor and the selected bounded graph-node summaries, never a second
exhaustive graph view. Rebuilt
generations use distinct request and manifest paths, so a provider-rejected oversized payload is
never silently replayed unchanged. Each
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
uses its `pending_event` field as a write-ahead record: MATEK first checkpoints the state
transition and complete event payload, creates the immutable event idempotently, then checkpoints
the state with the pending field cleared. Resume completes such a pending publication and validates
the checkpoint against event, decision, assignment, report, and hash evidence.
`research/coordinator/mailbox.json`, `research/assignments/*.json`, `research/registry.json`, and
`research/continuity.json` are materialized delivery/navigation views. They do not supersede the
canonical checkpoint or immutable evidence. Ordinary resume does not promise to reconstruct a
deleted or invalid `research/coordinator/state.json`; that condition fails integrity validation.

The derived registry, scientific-phase state, and continuity indexes never replace, rewrite, or
truncate the full raw reports under `research/workers/`, the full candidate-audit reports under
`research/audits/`, the intermediate audits under `research/lemma-audits/`, or the event evidence
under `research/events/`. New runs use immutable, zero-padded event-indexed coordinator
decisions. A `research/rounds/` tree, when present in an already completed legacy run, is preserved
only so its completed `research/result.json` remains readable; it is not live scheduler state and
is not converted into a resumable continuous checkpoint. The root `candidate/`, latest audit files,
`verdict.json`, and `research/result.json` are materialized accepted/latest/final views.
Attempt-scoped JSON evidence remains immutable; `proof.md` is a readable companion to the package's
embedded full proof. An explicit forced prompt/research generation, or an explicit provider
migration while research is incomplete, moves the prior tree to `research-history/` before
creating a fresh canonical scheduler checkpoint.
For an explicit forced prompt-compilation replay with an unchanged compiled-problem digest, the
archived coordinator, assignment, admission, and candidate-support inputs remain the frozen
pre-gate transaction boundary. This preserves request identity and prevents the graph promotion
performed by the first successful gate from causing duplicate paid candidate/audit calls.
The operational `logs/events.jsonl` and provider trace JSONL files are diagnostics only and are
not the authoritative research-event ledger.

New worker output is `ResearchWorkerReport` schema v2 and contains no `GraphPatch`. The historically
named `research/graph-patches/<assignment-id>.json` is now an application-owned admission record:
it states `admission_mode = "typed_scientific_report_v2"`,
`model_authored_patch = false`, and records the deterministic graph result or warning. Full raw and
normalized scientific evidence is durable first. MATEK injects persistence identity and binds the
frozen graph revision; graph commits remain idempotent by operation ID and scientific results by
`(run_id, assignment_id, local_key, result_schema_version)`. Canonical claim identity uses the
normalized exact statement plus scope. Exact matches share a claim but retain separate proof
attempts; semantic near-matches require audit or an explicit equivalence derivation.
Definition IDs may likewise be shared only for an explicit branch-scoped notation declaration.
Such declarations carry no proof dependencies; every admitting report retains immutable run-local
evidence, and definitions with mathematical proof dependencies are excluded from trusted support.

`research/coordinator/scientific-phase.json` is integrity-protected durable state for phase
transitions, progress snapshots, assignment plans, and merge/redirect dispositions. The launched
plans make the exact focused cut obligation and complementary-role rotation durable across
activations. Its frontier signals derive only from persisted reports, audits, ledger revisions, and
smallest-open-cut IDs; validated coordinator decisions supply the recorded plans and dispositions.
Resume validates and reuses the checkpoint, and phase changes retire queued old-phase assignments.

Private computation scratch is mutable and never trusted directly. Collection commits declared
regular files to `research/computations/blobs/sha256/` and writes an immutable manifest containing
application-computed hashes, quotas, replay commands, expected outputs, and tool versions.
`research/worker-computation/<assignment-id>.json` binds collection and independent replay. Only a
passed replay from the current restricted-Docker, filesystem-confined, network-disabled backend
can support a proposed derivation; native replay is refused. Missing, unsafe, failed, or mismatched
replay remains explicit non-proof evidence. Passing replay attests reproducibility of the declared
certificate, not domain completeness or mathematical truth, so independent audit is still needed.
For candidate use, the computation result must also occur in an exact-main result's declared
transitive `dependency_result_keys` closure. Candidate state binds that mapping, the corresponding
canonical computation derivation, and its manifest/replay graph artifacts; unrelated replay is
blocking evidence, not proof support.

Each `research/lemma-audits/<nomination-id>/input.json` immutably binds the blind mathematical
packet, complete targeted-obligation contracts, role instructions, requests, model settings, and
hashes. Schema v2 binds two distinct application execution-context IDs in the input, response
evidence, and gate. A response may also bind a provider session ID when the provider exposes one;
only a bounded, non-secret, redaction-safe identifier is persisted, and equal provider sessions
fail the independence gate. The verifier and falsifier write separate response evidence; resume
reuses the frozen packet and calls only missing roles. `gate.json` deterministically binds both
response hashes and identity evidence or exact missing/failed obligations. A schema-v1 pass is
never accepted into the graph: MATEK copies every extant v1 input, response, and gate byte-for-byte
under `legacy-v1/`, commits and verifies `legacy-v1/manifest.json`, retires only the canonical v1
copies, and then reruns both roles under schema v2. Even `audit_passed` contains hard-coded false
values for main-target satisfaction and manuscript authorization.
Before retrying a missing role, MATEK copies the current gate to immutable
`gate-checkpoints/<gate-sha256>.json` and records that digest in the event ledger. If a process
stops after committing newer response/gate evidence but before checkpointing scheduler metadata,
resume accepts only a strictly monotone, fully authenticated evidence extension, repairs the
response-accounting binding, and calls only roles that still remain missing.

Each `research/counterexample-audits/<audit-id>/policy.json` is first-write-wins and binds the
official policy version, prompt digests, model settings, and distinct role execution contexts.
`nomination.json` closes the exact result-local dependency DAG, unresolved-obligation set,
dependency versions, artifact declarations, and any current canonical graph and independently
replayed computation support. Requests and completed role responses are immutable; resume calls
only a genuinely missing role. A parsed `BLOCKED` or `FAIL` judgment closes that audit as
`audit_failed`, while `gate.json` may remain resumable only for missing execution evidence. Before
terminal rejection or a main-target `REFUTES` edge, MATEK reloads the worker report, current graph,
computation evidence, official policy, requests, responses, and gate and deterministically
recomputes the complete binding and failure-precedence decision. A retryable `BLOCKED` audit keeps
using its frozen nomination across unrelated graph revisions while its canonical support contract
is unchanged. If that support genuinely changes, the old record and artifacts remain durable but
are marked superseded with a reason and event; MATEK creates a new audit ID, freezes the new support
closure, and reruns the required roles instead of relabeling old evidence.

Manuscript and formalization contexts use the same bounded selector over current Markdown. It
includes only live trusted claims, authenticated definitions, audit-passed
proof routes, independently verified sources where applicable, and deterministic verified
formalizations where applicable. Accepted main-proof support is prioritized. Informal/open claims,
unverified sources, unresolved or unauthenticated derivations, experiments, and archive-only
evidence are excluded. Every context reports the policy, maximum node count, eligible/included/
omitted counts, truncation flag, ledger ambiguity count, and priority order so a cap is explicit
rather than silently dropping evidence.

`research/issues/<issue-id>.json` contains immutable categorized execution, evidence, scientific,
or resource issues, their trace paths, and exact recovery obligations. Each issue is delivered to
the coordinator by a corresponding immutable event. Integrity failures are not quarantined: state
corruption, immutable-artifact mismatch, unsafe paths, security failures, and unauthorized writes
into the user project outside the shared `.matek` state tree remain hard stops. Changes confined
to `.matek` that cannot be attributed to one call — a concurrent run's root, worker workspace,
lock, or knowledge graph — are recorded as warnings, never as a stop or a restore instruction.

Each completed candidate audit is written beneath its attempt directory and bound into the
canonical scheduler checkpoint immediately. An unavailable audit leaves the attempt in
`awaiting_audits`; resume retries only audit names without a committed hash and response ID.

## Model traces

Store visible model outputs, request configuration, response IDs, tool/citation metadata, and
usage. Every terminal provider attempt is added to `logs/usage.jsonl` before its final output is
admitted, including schema-invalid attempts and bounded schema-repair generations. Do not request
or store private chain-of-thought. Reasoning summaries may be stored only when explicitly
configured and should not be required for reproducibility.

In Codex hierarchical mode, each first-level worker trace records its configured
`maximum_subagents` and the exact shell-free Codex arguments enabling that bounded collaboration
pool. Nested work remains inside that provider session; its aggregate terminal usage is charged to
the first-level worker attempt, and only the synthesized `ResearchWorkerReport` crosses MATEK's
durable scientific-report boundary.

`config/effective_config.toml` is the resume source. It changes only after an explicit,
confirmed provider migration. A state-first `pending_backend_migration` intent lets ordinary
resume finish the authorized provider/config switch across either side of a crash;
`backend_manifest.json` and the final report retain the provider,
nonsecret authentication class, CLI/SDK version, requested model/effort, sessions, and observed
usage. A provider migration starts a new cache generation and is recorded in run history.

## Sensitive data

Never persist API keys, bearer tokens, authentication headers, home-directory secrets, or
full environment dumps. Environment capture must use an allowlist.
