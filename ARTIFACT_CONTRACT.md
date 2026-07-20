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
│   ├── registry.json
│   ├── rounds/<round-id>/plan.json
│   ├── rounds/<round-id>/workers/*.json
│   ├── candidate/
│   │   ├── proof.md
│   │   ├── package.json
│   │   └── dependency_graph.json
│   ├── audits/*.json
│   └── verdict.json
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

## Integrity

Record SHA-256 hashes for immutable inputs, accepted proof package, approved theorem statement,
manuscript source, bibliography, and final verification outputs.

## Model traces

Store visible model outputs, request configuration, response IDs, tool/citation metadata, and
usage. Do not request or store private chain-of-thought. Reasoning summaries may be stored only
when explicitly configured and should not be required for reproducibility.

`config/effective_config.toml` is the resume source. It changes only after an explicit,
confirmed provider migration; `backend_manifest.json` and the final report retain the provider,
nonsecret authentication class, CLI/SDK version, requested model/effort, sessions, and observed
usage. A provider migration starts a new cache generation and is recorded in run history.

## Sensitive data

Never persist API keys, bearer tokens, authentication headers, home-directory secrets, or
full environment dumps. Environment capture must use an allowlist.
