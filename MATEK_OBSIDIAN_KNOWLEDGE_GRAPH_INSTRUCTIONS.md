# MATEK Knowledge Graph Integration Instructions

**Purpose:** Implement a persistent, Obsidian-compatible knowledge graph for each MATEK research problem.

**Important:** The research orchestration in the current codebase may differ from earlier MATEK designs. Adapt these instructions to the existing architecture. The only required orchestration assumption is that a **central research coordinator agent** manages and delegates work to subagents.

---

## 1. Objective

For every research problem, MATEK should maintain a persistent knowledge graph that:

1. records mathematically meaningful progress across runs;
2. helps the central research coordinator identify and assign useful follow-up work;
3. preserves failed, blocked, and superseded approaches so they are not repeatedly rediscovered;
4. links claims, proofs, dependencies, audits, sources, experiments, formalizations, and open tasks;
5. survives incomplete or interrupted MATEK runs;
6. can be inspected and navigated by a human in Obsidian;
7. remains usable without Obsidian installed.

Obsidian is the recommended human interface, but **must not be the only database or runtime dependency**. The authoritative representation should be portable Markdown files with typed YAML frontmatter. A derived SQLite or JSON index may be used for fast machine queries and should be rebuildable from the Markdown vault.

---

## 2. Per-problem workspace

Create or extend one persistent named graph per problem inside the project workspace:

```text
<problem-workspace>/
├── problem.md
├── matek.toml
└── .matek/
    └── knowledge/
        └── <graph-name>/
            ├── Home.md
            ├── Definitions/
            ├── Claims/
            ├── Proofs/
            ├── Approaches/
            ├── Counterexamples/
            ├── Experiments/
            ├── Sources/
            ├── Tasks/
            ├── Audits/
            ├── Formalizations/
            ├── Runs/
            ├── Dashboards/
            ├── graph-schema.json
            ├── graph-index.sqlite
            ├── graph-state.json
            ├── snapshots/
            ├── locks/
            └── .obsidian/       # Optional Obsidian configuration
```

The default graph name is the normalized problem filename without its extension. The knowledge
graph must persist independently of individual run directories. Later runs on the same problem
should load, validate, and extend the existing graph, while different problem filenames use
separate graphs by default. A user may explicitly select an existing graph for related or
follow-up work; an unknown explicit name must fail rather than create a new graph implicitly.

---

## 3. Typed node model

Implement at least these node types:

```text
problem
definition
claim
proof
approach
task
counterexample
experiment
source
audit
formalization
run
artifact
human_note
```

A `claim` should support subtypes such as:

```text
lemma
theorem
conjecture
corollary
equivalence
algorithm
lower_bound
upper_bound
classification
```

Claims, proofs, audits, and Lean formalizations must be separate nodes. A claim may have multiple candidate proofs, rejected proofs, audits, or formalizations.

Each node must have an immutable stable ID, independent of its filename or title, for example:

```text
PRB-...
CLM-...
PRF-...
APR-...
TSK-...
AUD-...
FRM-...
```

Recommended filename format:

```text
CLM-<id>--short-readable-slug.md
```

---

## 4. Typed relations

Support directed typed relations, including at least:

```text
depends_on
proves
disproves
refutes
strengthens
weakens
specializes
generalizes
equivalent_to
contradicts
motivates
blocks
blocked_by
resolves
tests
cites
audits
formalizes
supersedes
created_during
related_to
```

Enforce basic relation constraints where possible, for example:

```text
proof --proves--> claim
audit --audits--> proof or claim
claim --depends_on--> claim or definition
formalization --formalizes--> claim
counterexample --refutes--> claim
task --targets--> node
```

The mathematical dependency relation should normally form a DAG. General relations such as `related_to`, `contradicts`, and `equivalent_to` may contain cycles.

---

## 5. Node schema and statuses

Use flat YAML frontmatter because Obsidian properties do not handle deeply nested structures well.

Example:

```markdown
---
matek_id: CLM-01...
node_type: claim
claim_type: lemma
problem_id: PRB-01...
title: Bounded Completion Lemma
epistemic_status: proved_informally
workflow_status: active
statement_version: 3
created_in_run: RUN-...
last_modified_run: RUN-...
depends_on:
  - "[[CLM-...--kernel-learning-lemma]]"
proved_by:
  - "[[PRF-...--completion-proof]]"
audited_by:
  - "[[AUD-...--foundational-audit]]"
formalized_by: []
tags:
  - matek/claim
  - matek/lemma
---

# Bounded Completion Lemma

## Exact statement

...

## Scope and conventions

...

## Current significance

...
```

Use separate status axes.

### Epistemic status

```text
open
conjectured
candidate
proved_informally
audit_passed
lean_verified
refuted
inconsistent
stale
```

### Workflow status

```text
queued
active
in_progress
blocked
dormant
abandoned
superseded
complete
```

Only deterministic Lean verification may assign `lean_verified`.

A proposed proof submitted by an agent must not automatically promote a claim beyond `candidate` or `proved_informally`, according to the existing audit policy.

---

## 6. Coordinator and subagent interaction

The central research coordinator should use the graph as persistent research memory and a task-planning substrate.

### Required behavior

Before assigning work, the coordinator should query the graph for:

- unresolved claims relevant to the main target;
- candidate proofs awaiting audit;
- blocked approaches that may be reopened by a new mechanism;
- contradictions or unresolved inconsistencies;
- missing dependencies;
- high-value open tasks;
- results from prior runs;
- previously refuted or unproductive routes.

The coordinator should create or update `task` nodes and assign graph-scoped work to subagents.

### Context slices

Do not provide the entire vault to every subagent. Build a bounded graph slice containing:

- the main problem statement;
- the target node;
- relevant definitions;
- dependency ancestors;
- downstream claims affected by the target;
- prior attempted proofs;
- nearby counterexamples;
- relevant sources and audits;
- the exact requested task.

### Graph patches

Subagents must not directly mutate the shared vault concurrently. Require structured proposed patches:

```json
{
  "base_graph_revision": "...",
  "run_id": "...",
  "task_id": "...",
  "create_nodes": [],
  "update_nodes": [],
  "add_edges": [],
  "remove_edges": [],
  "proposed_status_changes": [],
  "evidence": [],
  "unresolved_obligations": []
}
```

The coordinator or a deterministic graph service must:

1. validate the patch schema;
2. verify node IDs and relation types;
3. detect conflicting edits;
4. detect likely duplicates;
5. enforce permitted status transitions;
6. request audit when required;
7. merge atomically;
8. rebuild/update the derived index;
9. save a snapshot or Git commit.

Use optimistic concurrency through `base_graph_revision` and node-content hashes.

---

## 7. Persistence and invalidation

Every graph change should record:

- run ID;
- agent role or human author;
- timestamp;
- previous and new content hashes;
- reason for change;
- source artifacts or evidence;
- graph revision.

Track exact dependency versions used by proofs and audits.

If a dependency is changed, weakened, refuted, or deleted, automatically mark affected downstream nodes as appropriate:

```text
stale
dependency_changed
dependency_refuted
requires_reaudit
```

Lean verification must be tied to:

- exact claim ID and statement version;
- statement hash;
- Lean theorem declaration;
- source-file hash;
- Lean version;
- mathlib revision;
- build result;
- axiom report.

Changing the informal statement must invalidate the prior `lean_verified` status unless exact equivalence is re-established.

---

## 8. Failed and partial work

Preserve mathematically useful failed work so later runs do not repeat it.

A blocked or refuted approach note should contain:

- exact route attempted;
- proposed invariant, construction, or missing lemma;
- strongest valid partial result;
- exact failure point;
- whether the route is refuted or merely stalled;
- explicit counterexample when available;
- condition under which the route should be reopened.

Recommended classifications:

```text
blocked_local_gap
blocked_theorem_strength_gap
refuted_by_counterexample
superseded
duplicate
abandoned_low_value
```

Do not convert raw agent transcripts into permanent first-class graph nodes. Store full transcripts in run artifacts and write distilled mathematical summaries into the graph.

Incomplete runs must still merge valid partial results and leave a usable research frontier for future runs.

---

## 9. Human auditability in Obsidian

Generate a useful `Home.md` containing:

- exact main problem;
- overall status;
- strongest established results;
- current proof architecture;
- unresolved main obligations;
- active tasks;
- blocked or refuted routes;
- unresolved contradictions;
- recent run summaries;
- Lean verification status;
- links to dashboards.

Generate Obsidian-compatible dashboards or Bases views when practical:

```text
Open Claims
Candidate Proofs Awaiting Audit
Audit-Passed Results
Lean-Verified Results
Active Tasks
Blocked Approaches
Unresolved Contradictions
Unverified Sources
Recent Changes
```

Use ordinary Obsidian links and backlinks throughout the notes.

Optionally generate a few curated Canvas files:

```text
Main Proof Architecture.canvas
Active Research Routes.canvas
Dependency Bottlenecks.canvas
Formalization Map.canvas
```

Do not attempt to place the entire graph into one Canvas.

The graph must remain valid and queryable when Obsidian is not installed.

---

## 10. Human editing policy

Humans may edit the vault, but establish ownership rules:

- `matek_*` frontmatter fields are machine-managed;
- prose outside generated blocks is human-editable;
- generated sections use explicit markers;
- editing a claim statement increments its version;
- editing an audited proof marks relevant audits stale;
- `lean_verified` cannot be assigned manually;
- node deletion should use tombstones or a graph command;
- unknown Markdown notes may be indexed as `human_note`.

Before a run, validate the vault and report conflicting or malformed manual changes instead of silently overwriting them.

---

## 11. Integration with manuscripts and Lean

The knowledge graph must feed the existing manuscript and Lean stages.

The manuscript generator should consume:

- accepted claim nodes;
- accepted proof nodes;
- definitions;
- source nodes;
- audits;
- dependency order.

Record mappings such as:

```text
CLM-... -> Theorem 1.2
CLM-... -> Lemma 3.4
PRF-... -> Section 4
SRC-... -> BibTeX key
```

Formalization nodes should link:

```text
claim
-> challenge.lean statement
-> Lean declaration
-> source file
-> build record
-> axiom report
```

Do not weaken any existing bibliography, proof-audit, manuscript-compilation, or Lean-verification gate.

---

## 12. CLI additions

Add commands appropriate to the current CLI architecture:

```bash
matek graph list
matek graph init <graph-name>
matek graph validate --knowledge-graph <graph-name>
matek graph status --knowledge-graph <graph-name>
matek graph frontier --knowledge-graph <graph-name>
matek graph rebuild-index --knowledge-graph <graph-name>
matek graph open --knowledge-graph <graph-name>
matek graph export --knowledge-graph <graph-name>
matek graph diff <revision-a> <revision-b> --knowledge-graph <graph-name>
```

Useful optional commands:

```bash
matek graph show <node-id>
matek graph dependencies <node-id>
matek graph downstream <node-id>
matek graph stale
matek graph tasks
```

`matek graph open` may open the vault in Obsidian when available, but must fail gracefully and print the vault path when Obsidian is absent.

Graph query and maintenance commands may auto-select when exactly one graph exists, but must
require `--knowledge-graph` when multiple graphs would make the operation ambiguous.

---

## 13. Recommended implementation phases

### Phase 1: Persistent typed vault

Implement:

- schemas;
- stable IDs;
- note templates;
- graph parser;
- typed relations;
- SQLite or equivalent derived index;
- validation;
- snapshots;
- `Home.md`;
- basic dashboards;
- graph CLI commands.

### Phase 2: Coordinator-driven graph research

Implement:

- graph frontier queries;
- task nodes;
- bounded context slices;
- structured graph patches;
- atomic patch merging;
- conflict and duplicate detection;
- partial-run persistence;
- adaptive follow-up tasks.

### Phase 3: Epistemic integrity

Implement:

- status-transition rules;
- dependency hashes;
- stale-state propagation;
- contradiction tracking;
- audit-linked promotion;
- Lean statement/version linkage;
- manuscript mappings.

### Phase 4: Human exploration

Implement:

- richer dashboards;
- curated Canvas generation;
- proof-tree and dependency-path views;
- change summaries;
- optional Graphviz/Mermaid export.

Do not require a custom Obsidian plugin for the initial release. Consider one only after the Markdown-based graph is stable.

---

## 14. Acceptance criteria

The work is complete when:

- [ ] Every problem can create and persist a typed knowledge vault.
- [ ] Later runs load and extend the same graph.
- [ ] Claims, proofs, audits, and formalizations are separate nodes.
- [ ] Stable IDs survive renaming.
- [ ] The coordinator can query a research frontier and assign graph-scoped tasks.
- [ ] Subagents return structured patches rather than editing the shared graph.
- [ ] Patch merges are validated and atomic.
- [ ] Concurrent conflicting edits are detected.
- [ ] Failed and blocked approaches are retained in distilled form.
- [ ] Dependency changes propagate staleness.
- [ ] Lean verification is attached to an exact statement version.
- [ ] Incomplete runs leave useful persistent progress.
- [ ] Obsidian can open and navigate the vault.
- [ ] MATEK remains functional without Obsidian installed.
- [ ] Human edits are preserved under explicit ownership rules.
- [ ] Existing research, manuscript, source-verification, audit, and Lean gates remain intact.
- [ ] Unit tests cover schema validation, patch merging, conflicts, invalidation, and persistence.
- [ ] An end-to-end test demonstrates two separate MATEK runs extending the same problem graph.

---

## 15. Non-negotiable design rules

1. Obsidian is the human-facing view, not the sole database.
2. Markdown plus typed frontmatter is the portable source of truth.
3. The graph is persistent across runs.
4. Claims and evidence are not conflated.
5. Subagents do not concurrently edit the shared vault.
6. The central coordinator remains responsible for task assignment and graph integration.
7. Status promotion follows explicit epistemic rules.
8. Dependency changes can invalidate downstream work.
9. Failed approaches are preserved in distilled form.
10. The system remains usable without Obsidian.
