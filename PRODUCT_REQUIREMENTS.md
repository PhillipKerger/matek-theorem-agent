# Product Requirements

## Product summary

**ASCEND** (**Autonomous System for Conjecture Exploration and Verified Deduction**) is a local CLI that accepts a research-level mathematical problem,
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
- Resolve backend selection from CLI, `ASCEND_BACKEND`, project configuration, then the `codex`
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
  and ASCEND's identifier-resolution HTTP calls without weakening citation gates.
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

- Start with a coordinator-generated diverse portfolio of at least four materially different
  approaches unless the configured budget is lower.
- Keep initial workers independent; do not reveal the favored route to all workers.
- Store every worker assignment and result.
- Maintain an approach registry containing mechanism, result, assumptions, bottleneck,
  counterexamples, dependencies, and status.
- Support multiple rounds within cost, token, wall-clock, and agent limits.
- Launch targeted counterexample and lemma-audit tasks when promising claims arise.
- Produce a candidate proof package when the coordinator recommends it or a worker explicitly
  reports a full proof of the exact success criterion.
- When a worker reports a complete proof, pause unfinished work and run the full independent
  acceptance gate immediately. Advance only if it passes; otherwise resume remaining routes
  with the audit obligations preserved.
- Expose a total active wall-clock limit for the complete run, persist elapsed time across resume,
  and use the remaining allowance to bound in-flight model calls. Keep this limit disabled by
  default and require explicit user configuration.

### FR-4 Research acceptance gate

- Run fresh-context foundational, domain-specialist, hostile counterexample, and, when
  relevant, complexity/quantitative audits.
- Run a final judge that sees the problem contract, candidate package, and audit reports.
- Accept only if the exact target is established, all mandatory audits pass, and unresolved
  theorem-strength obligations are empty.
- Preserve valuable partial results under truthful statuses.

### FR-5 Manuscript

- Run only after research acceptance.
- Generate `paper.tex`, `references.bib`, a claim map, and a proof dependency map.
- Include a thorough introduction and related-work discussion.
- Explain how the result differs from and advances existing work.
- Include a Statement of AI Usage naming ASCEND with GPT 5.6 and cite both the canonical ASCEND
  GitHub repository and ASCEND whitepaper arXiv preprint.
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
