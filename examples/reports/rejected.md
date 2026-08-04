# MATEK report — `20260719T120000Z-example-rejected-0a1b2c`

This sanitized example shows a truthful scientific rejection after an independently audited
counterexample to the frozen exact theorem. A rejected proof candidate or coordinator declaration
would remain partial and could not produce this status.

## Execution provenance

- Model backend: Codex CLI (the recommended/default provider)
- Authentication class: ChatGPT
- Requested model: Codex account/workspace default
- Automatic API fallback: disabled

## Outcome

| Gate | Status |
| --- | --- |
| Research | `RESEARCH_REJECTED` |
| Workflow | `COMPLETE_WITH_WARNINGS` |
| Manuscript | `NOT_STARTED` |
| Publication | `NOT_ASSESSED` |
| Lean | `NOT_STARTED` |

## Strongest established result

The frozen target asserted `n + 1 = n` for every integer `n`. The exact instance `n = 0` satisfies
the quantified domain, while the conclusion evaluates to `1 = 0`, which is false. Independent
verifier and hostile-falsifier roles recomputed the hypothesis check and failed conclusion from
the complete certificate, and the application-owned gate returned `refutation_verified`.

## Unresolved obligations

None. Any missing audit role, changed request or response, mismatched target, incomplete
certificate, or branch-local obstruction would have blocked terminal rejection.

## Representative artifacts

- [`research/registry.json`](../../.matek/runs/EXAMPLE/research/registry.json)
- [`research/workers/exact-refutation.json`](../../.matek/runs/EXAMPLE/research/workers/exact-refutation.json)
- [`research/counterexample-audits/cex-example/nomination.json`](../../.matek/runs/EXAMPLE/research/counterexample-audits/cex-example/nomination.json)
- [`research/counterexample-audits/cex-example/requests/counterexample-verifier.json`](../../.matek/runs/EXAMPLE/research/counterexample-audits/cex-example/requests/counterexample-verifier.json)
- [`research/counterexample-audits/cex-example/requests/counterexample-falsifier.json`](../../.matek/runs/EXAMPLE/research/counterexample-audits/cex-example/requests/counterexample-falsifier.json)
- [`research/counterexample-audits/cex-example/gate.json`](../../.matek/runs/EXAMPLE/research/counterexample-audits/cex-example/gate.json)
- [`report/verification_certificate.json`](../../.matek/runs/EXAMPLE/report/verification_certificate.json)

No manuscript, bibliography, or Lean stage ran after rejection. The absence of those artifacts is
part of the auditable outcome. In graph-integrated runs, only this verified gate may add a
`REFUTES` edge from the audited counterexample to the main target.
