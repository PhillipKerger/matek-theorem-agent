# Product Requirements

## Product summary

**MATEK** (**Multi-Agent Theorem Exploration through Knowledge-Graph Memory**) is a local CLI that accepts a research-level mathematical problem,
compiles it into a rigorous research prompt, runs an adaptive multi-agent research process,
audits any candidate solution, writes a publication-oriented LaTeX manuscript, attempts Lean
formalization, and generates a reproducible final report.

## Personas

- A mathematician with a problem statement and an existing Lean project.
- A researcher who wants a research-only run without Lean.
- A maintainer evaluating and improving a reusable mathematical research methodology.

## Functional requirements

### FR-0 Model backend and authentication

- Default to the official Codex CLI and reuse its saved **Sign in with ChatGPT** authentication.
- Do not require `OPENAI_API_KEY` for the default Codex path.
- Preserve the direct OpenAI Responses API as an advanced, explicitly selected backend.
- Resolve backend selection from CLI, `MATEK_BACKEND`, project configuration, then the `codex`
  built-in default, and freeze the result in run state.
- Never silently fall back from Codex to API billing.
- Feature-detect installed Codex capabilities and determine only a coarse authentication class
  with `codex login status`; never inspect credential files.

### FR-1 Problem intake

- Accept UTF-8 `.md` or `.txt` input.
- Accept concise problem descriptions when they uniquely identify the mathematical setting,
  target, and essential constraints; do not require a user-supplied literature review or proof
  plan.
- Preserve the original bytes and a normalized copy.
- Record a content hash, timestamp, CLI arguments, config snapshot, and tool versions.
- Reject empty input and provide a useful diagnostic.

### FR-2 Framework compilation

- Load `resources/prompts/research_prompt_framework.txt` verbatim.
- Expected bundled SHA-256: `bd724294a261f4bc2e5da2191813e40c1340bc6ee039c753cb5c60276e7a512c`.
- Use xhigh reasoning and web search by default.
- Provide an explicit `--no-web-search` override that disables search across all model stages
  and MATEK's identifier-resolution HTTP calls without weakening citation gates.
- Produce both a complete adapted prompt and structured metadata.
- Front-load a compact research-mandate snapshot containing the exact target, boundary cases,
  insufficient outcomes, adaptive independent search, persistence, adversarial review, public
  search boundary, and audited completion condition before the expanded protocol.
- Fill every applicable bracketed placeholder; explicitly remove or mark inapplicable
  optional branches rather than leaving template placeholders unresolved.
- Verify literature/background claims used in the compiled prompt.
- Classify whether the exact target is unknown in the checked literature, has no exact match
  found, is partially resolved, or is fully resolved by an existing theorem. Verify any claimed
  match against authoritative sources and compare its exact hypotheses and conclusion.
- If the exact target is already known, preserve that provenance and prohibit unsupported novelty
  claims while allowing proof reconstruction, exposition, and formalization.
- If the input does not uniquely identify a target, stop before research, persist a clarification
  request and focused questions, report the outcome to the user, and require a new run from a
  clarified problem file.
- Save source citations and search evidence separately from the prompt text.

### FR-3 Adaptive research

- Approximate the behavior of giving the complete main research prompt to a GPT 5.6 Sol Ultra
  research session through explicit application-level orchestration. `Ultra` is a product/session
  label, not a model-backend or Responses API primitive. The Responses API adapter defaults the
  logical coordinator to `gpt-5.6-sol` with `reasoning.mode = "pro"` and
  `reasoning.effort = "max"`, and workers to the same model and mode with
  `reasoning.effort = "xhigh"`. The default Codex adapter requests `gpt-5.6-sol` with `max`
  coordinator effort and `xhigh` worker effort through Codex CLI's reasoning-effort control; it
  does not present the Responses API `reasoning.mode` field as a Codex setting. Every role remains
  configurable within the selected backend's capabilities.
- Start or resume one durable logical research coordinator with the complete, unabridged compiled
  prompt and exact claim contract. Provider calls may use fresh contexts; correctness must come
  from application-owned state rather than a surviving provider conversation.
- Have the coordinator create sixteen initial assignments by default, spanning at least four
  materially different approaches unless the configured budget is lower.
- Keep initial workers independent; do not reveal the favored route to all workers.
- Run research as a completion-driven event loop rather than fixed rounds. Atomically preserve
  every assignment and full raw worker report, then atomically write one immutable zero-padded
  completion-event file and refresh the materialized mailbox snapshot. Activate the coordinator
  as useful events arrive.
- Give every coordinator activation the complete main prompt and claim contract, all
  not-yet-acknowledged mailbox events, the approach registry, audit obligations, and direct access
  to the complete raw reports. Derived summaries or continuity snapshots must never replace or
  truncate the underlying reports.
- Refill useful work dynamically after completions instead of waiting for a batch barrier. Permit
  up to 32 total open assignments (queued plus running) by default. Permit up to 32 of that open
  set to be active research workers, subject to backend and budget limits.
- Maintain an approach registry containing mechanism, result, assumptions, bottleneck,
  counterexamples, dependencies, and status.
- Support cost, token, active wall-clock, total-open-assignment, concurrency,
  coordinator-decision, and explicit call-count limits without turning any limit into a
  synchronization barrier.
- Name the primary scheduler controls `research.maximum_pending_assignments` and
  `research.maximum_coordinator_decisions`. Migrate legacy fixed-round settings and
  `--max-rounds` into scaled decision budgets for compatibility without restoring round
  semantics.
- Do not impose a cumulative research-worker count ceiling. Do not impose a global Codex
  call-count limit by default; retain explicit configurable limits.
- Persist `research/coordinator/state.json` as the canonical atomic scheduler checkpoint. Use its
  pending-event field as a write-ahead record: checkpoint the transition and proposed event first,
  create the immutable zero-padded event evidence, then clear the pending field. Validate the
  checkpoint against immutable coordinator decisions, full raw reports, per-assignment source
  verification, event hashes, and candidate/audit evidence on resume. Treat the mailbox,
  assignment files, registry, and continuity view as materialized navigation/delivery views; the
  continuity view separates promising routes, partial results, ruled-out directions and
  counterexamples, blocked routes and exact gaps, dependencies, prior directives, and audit repair
  obligations. Ordinary resume must fail truthfully if the canonical scheduler checkpoint is
  missing or invalid rather than claiming it can be reconstructed from evidence alone.
- Launch targeted counterexample and lemma-audit tasks when promising claims arise.
- Produce a candidate proof package when the coordinator recommends it or a worker explicitly
  reports a full proof of the exact success criterion.
- When a candidate is triggered, pause admission of new research workers and run the full
  independent acceptance gate immediately without waiting for unrelated active workers. Preserve
  any reports that finish while admission is paused in the mailbox. Advance only if the candidate
  passes; otherwise append the complete failed-audit reports and exact repair obligations as
  high-priority events, reactivate the coordinator immediately, and refill the live pool.
- Expose a total active wall-clock limit for the complete run, persist elapsed time across resume,
  and use the remaining allowance to bound in-flight model calls. Keep this limit disabled by
  default and require explicit user configuration.

### FR-3A Persistent mathematical knowledge graph

- MATEK shall maintain named, project-scoped persistent graphs beneath
  `.matek/knowledge/<graph-name>/`, independent of run directories. The default graph name is
  derived from the source problem filename without its extension, so different problem files do
  not share memory by default and later runs of the same file reuse it.
- A user may explicitly attach a related or follow-up problem to an existing graph with
  `--knowledge-graph NAME`. Unknown explicit names shall fail instead of creating a graph, and the
  selected graph shall be frozen in run metadata for resume.
- Portable Markdown with flat typed YAML frontmatter is authoritative. The SQLite index is a
  disposable acceleration layer rebuildable from Markdown.
- The graph shall represent problem, definition, claim, proof, approach, task, counterexample,
  experiment, source, audit, formalization, run, artifact, and human-note nodes with immutable
  stable IDs and typed, constraint-checked relations.
- Epistemic and workflow statuses are separate. Only deterministic Lean verification may assign
  `lean_verified`; worker proposals cannot bypass research/audit gates.
- The coordinator shall query a research frontier and create graph-scoped tasks. Workers receive
  bounded context slices and return structured optimistic-concurrency patches rather than
  mutating the shared vault.
- Patch merges shall validate types, IDs, relation constraints, dependency acyclicity, duplicate
  likelihood, node hashes, status transitions, and base revisions before an atomic commit and
  snapshot/index update.
- Dependency and exact-statement changes shall propagate staleness. Lean evidence is bound to an
  exact claim ID, statement version/hash, declaration, source hash, toolchain, mathlib revision,
  build result, and axiom report.
- Distilled failed/blocked work and valid partial results shall persist across incomplete runs;
  raw transcripts remain run artifacts rather than first-class graph nodes.
- Humans may rename notes and edit prose outside generated blocks. Exact-statement/proof edits
  trigger versioning/re-audit; machine-field conflicts fail validation instead of being silently
  overwritten.
- The graph shall be navigable in Obsidian through Home, dashboards, links/backlinks, and curated
  canvases, while all validation/query operations remain usable without Obsidian.

### FR-4 Research acceptance gate

- Run fresh-context foundational, domain-specialist, hostile counterexample, and, when
  relevant, complexity/quantitative audits.
- Require the candidate package to classify quantitative or algorithmic content explicitly, and
  require the independent foundational auditor to block a false negative so the packager cannot
  bypass an applicable complexity audit.
- Run a final judge that sees the problem contract, candidate package, and audit reports.
- Accept only if the exact target is established, all mandatory audits pass, and unresolved
  theorem-strength obligations are empty.
- Preserve valuable partial results under truthful statuses.

### FR-5 Manuscript

- Run only after research acceptance.
- Generate `paper.tex`, `references.bib`, a claim map, and a proof dependency map.
- Include a thorough introduction and related-work discussion.
- Explain how the result differs from and advances existing work.
- Include a Statement of AI Usage naming MATEK with GPT 5.6 and cite both the canonical MATEK
  GitHub repository and MATEK whitepaper arXiv preprint.
- Do not cite a source solely because another model asserted that it exists.
- Compile with `latexmk` or a configurable LaTeX command.

### FR-6 Bibliography verification gate

For every cited work, independently verify:

- existence;
- exact title;
- author list;
- year/date;
- venue or publication status;
- DOI, arXiv identifier, ISBN, or stable source URL where available;
- that the manuscript's characterization of the result is supported by the source;
- that the cited theorem is applied under its actual hypotheses when used in a proof.

Produce `bibliography_audit.json` and `bibliography_audit.md`. Any unverified entry blocks
Lean progression unless the citation is removed and the manuscript is regenerated.

### FR-7 Lean feasibility and statement alignment

- After the compiled manuscript and verified bibliography are durable, ask the interactive user
  whether to proceed with formal Lean verification. A negative answer skips every Lean stage and
  produces the final report. If no answer is received within five minutes, proceed automatically.
- Persist the decision so resume never repeats the prompt or completed manuscript work.
- Assess whether full or main-result formalization is realistically attainable.
- Generate `challenge.lean` as the human-auditable target theorem statement.
- Generate a plain-language back-translation and a field-by-field claim alignment report.
- Audit quantifiers, domains, finiteness, exceptional cases, equality notions, hidden
  typeclass assumptions, and use of classical axioms.
- Begin implementation only after statement alignment passes.

### FR-8 Lean formalization

- Invoke Codex CLI non-interactively through an adapter.
- Give Codex bounded, auditable tasks and exact file permissions.
- Iterate edits and Lean diagnostics up to configured budgets.
- Store Codex JSONL, prompts, patches/diffs, commands, and compiler output.
- Do not modify user files outside the run directory without explicit opt-in.

### FR-9 Deterministic Lean verification

A `LEAN_VERIFIED` result requires:

- configured Lean/Lake command exits 0;
- no `sorry`, `admit`, `by?`, unresolved `TODO` placeholders, or equivalent escape hatches;
- no unapproved axioms or declarations that encode the target;
- audited target theorem name and statement are unchanged;
- `#print axioms` output is captured and matches the allowlist;
- imported generated files are scanned too;
- the proof compiles from a clean run.

### FR-10 Reporting

Generate machine-readable and human-readable reports with:

- original problem;
- compiled prompt;
- research configuration and usage;
- strongest proved result;
- exact status and unresolved obligations;
- audit summaries;
- manuscript build and bibliography status;
- Lean alignment and verification status;
- tool/model versions;
- costs/tokens when available;
- reproducibility instructions;
- relative links to artifacts.

## Status taxonomy

At minimum:

```text
RECEIVED
PROMPT_COMPILED
RESEARCH_RUNNING
RESEARCH_PARTIAL
RESEARCH_REJECTED
RESEARCH_ACCEPTED_FOR_MANUSCRIPT
MANUSCRIPT_FAILED
MANUSCRIPT_COMPILED
BIBLIOGRAPHY_REJECTED
BIBLIOGRAPHY_VERIFIED
LEAN_NOT_REQUESTED
LEAN_INFEASIBLE
LEAN_STATEMENT_ONLY
LEAN_PARTIAL
LEAN_FAILED
LEAN_VERIFIED_WITH_APPROVED_AXIOMS
LEAN_VERIFIED
REPORT_COMPLETE
```

## Nonfunctional requirements

- Resumable and idempotent at stage boundaries.
- Atomic state writes.
- No paid step repeats after a successful checkpoint unless forced.
- Offline unit tests.
- Secret redaction.
- Clear terminal progress without exposing private chain-of-thought.
- Structured logs and optional verbose diagnostics.
- Configurable budgets and concurrency.
- Graceful interruption on Ctrl-C.
