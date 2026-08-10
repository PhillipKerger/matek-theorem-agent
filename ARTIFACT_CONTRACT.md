# Artifact Contract

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
    ├── {Problems,Definitions,Claims,Proofs,Proof Attempts,Derivations,Obligations,
    │   Approaches,Counterexamples,Experiments,Sources,Tasks,Audits,Formalizations,
    │   Runs,Artifacts,Human Notes,Dashboards}/
    ├── Home.md
    ├── graph-schema.json
    ├── graph-state.json
    ├── target-registry.json
    ├── ledgers/<problem-id>/canonical-ledger.json
    ├── ledgers/<problem-id>/migration-report.json # only for ambiguous legacy projection
    ├── ledgers/migrations/<plan-sha256>.application.json
    ├── graph-index.sqlite
    ├── graph-pending.json       # exists only across an interrupted commit
    ├── snapshots/
    │   ├── <revision>.json                     # immutable schema-v1 snapshots, if present
    │   ├── manifests/<revision>.json           # schema-v2 delta + integrity manifest
    │   ├── checkpoints/<revision>.json         # periodic full content-hash maps
    │   └── blobs/{nodes,edges}/<sha256>.json   # immutable content-addressed records
    └── locks/graph.lock
```

Markdown notes with typed flat frontmatter are authoritative. `graph-state.json` binds their
content, statement, and machine-owned-field hashes to a revision. SQLite, Home, dashboards, and
canvases are derived and rebuildable. Each run report records the selected graph name, selection
mode, problem ID, graph revision, vault path, index path, validation warnings, and graph status
rather than certifying a mutable cross-run tree as a run-local artifact. The selection is frozen
for resume.

Legacy `snapshots/<revision>.json` files are read-only and reconstruct to their exact original
bytes. New writes use schema v2, with a full checkpoint at revision zero, every 64 revisions by
default, and at the first v2 revision after legacy history. A v2 integrity root covers the manifest,
its parent binding, and a content root over every live node and edge blob hash; checkpoint files are
themselves hash-bound. The manifest is published last so an interrupted commit cannot expose a
partially written revision. Reconstructed v2 full snapshots use deterministic sorted JSON whose
exact SHA-256 digest is bound into the manifest. Snapshot integrity proves byte/history
reconstruction only; it does not promote any mathematical status.

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

The Markdown notes remain the authoritative complete archive. Each
`ledgers/<problem-id>/canonical-ledger.json` is an integrity-protected, rebuildable trust projection
with schema version, source graph revision, problem/target IDs, exact claims and logical versions,
derivations and premise versions, typed obligations, ambiguities, and `integrity_sha256`.
An obligation logical version hashes its normalized exact statement, conclusion, ordered
quantifiers and hypotheses, dependency-claim IDs, target-claim IDs, scope, notation-definition
version, and falsification evidence. A blind lemma packet freezes that complete self-validating
contract together with the obligation's statement version and persisted-content SHA-256; a bare
lemma statement cannot silently resolve a richer quantified, hypothesized, or falsified contract.
Derivation premises are jointly required; alternative derivations are independent support paths.
Gapped, partial, assumption-bearing, or ambiguous archive records do not become trusted ledger
support. Application-admitted claims with a missing, malformed, or nonempty normalized assumption
contract are projected as standalone stale/quarantined claims, and their derivations are excluded.
Partial results remain proof attempts with an explicit completion obligation even when they report
no other gap. Neither class can act as a same-report premise, candidate dependency, lemma-audit
nomination/support, or exact-counterexample nomination/support. The persisted evidence is retained;
the quarantine does not erase the research record. `audit_passed`/`lean_verified` are the only
direct trusted claim statuses; an audited derivation propagates trust only from jointly trusted
premises with resolved obligations. If safe projection
of legacy prose is impossible, `ledgers/<problem-id>/migration-report.json` records the ambiguity and
`invented_support = false`. Neither derived ledger file is part of a run certificate. The graph
frontier computes the smallest known open cut from the ledger and records
`open_cut_search_capped` when bounded search cannot certify minimality.

Canonical source notes retain `matek_source_id`, primary identifier, identifiers and arXiv
revisions, source aliases, titles/authors, evidence links, verification provenance, explicit
evidence claims, and verified state. Verified identity follows DOI/base-arXiv/MR/ISBN/URL
precedence; unverified title/author records use provisional fingerprints and remain open. Only
identical entity keys merge. Worker-result `CITES` relations require explicit result-source
references; separately verified compiler sources may cite the frozen target.
Equivalent case, `doi:` prefixes, and DOI resolver URLs normalize to one DOI. A ledger entry with
multiple distinct DOI values materializes one source per DOI; an ambiguous alias or shared
lower-precedence identifier maps conservatively to every retained candidate. The graph transaction
writes `repairs/source-identity-decision-<sha256-prefix>.json` with the normalized DOI values,
aliases, candidate node IDs, context, and `preserve_separate_source_nodes` decision. Its matching
`source_identity_ambiguity` issue is provenance-only and does not turn worker admission or research
scheduling into a failure. Historical mixed-DOI notes are retained, normalized, and marked by
`matek graph doctor --repair` with a before/after audit log so existing evidence is never deleted.
Agent-authored mathematical nodes carry descriptive one-liner IDs (`CLAIM: ...`, `APPROACH: ...`);
the immutable main target claim and operational nodes keep their stable hash IDs. No released
graph contains legacy hash-ID mathematical nodes, so no ID-renaming repair exists.

A legacy-migration plan is a user-selected external artifact, conventionally
`.matek/migration-reports/<graph-name>.json`, and must not be placed beneath
`.matek/knowledge/`. It records `mode = "dry_run"`, graph/revision/problem/target identity, the
complete problem-local archive digest/count, typed proposals and unresolved issues, plus
`integrity_sha256`; planning changes no graph file. `--apply-plan` accepts only an integrity-valid,
matching, current plan and requires confirmation or `--yes`. One recoverable/idempotent graph
commit retains legacy nodes and earlier snapshots while creating proposed proof attempts/
derivations, alias/quarantine metadata, and queued verifier/falsifier audit tasks. It makes no model
call. The resulting `ledgers/migrations/<plan-sha256>.application.json` binds the operation and old/
new revisions, changed/created IDs, queued audit task IDs, unapplied issues, timestamp, and its own
integrity digest. This application record is distinct from the conditional problem-local
`migration-report.json` emitted when ordinary ledger projection encounters ambiguous legacy prose.

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
Such declarations carry no `dependency_node_ids` or `dependency_result_keys`; every admitting
report retains an immutable application-owned binding, and definitions with proof dependencies are
excluded from the canonical ledger.

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

Manuscript and formalization contexts use the same bounded trusted-context selector over the
canonical ledger. It includes only live trusted claims, authenticated definitions, audit-passed
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
