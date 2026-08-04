# Synthetic-derived legacy report corpus

The original ATSP, matroid-secretary, and k-server archives referenced by the
implementation brief are not included in this repository and were not available in the
readable workspace when this corpus was created. In particular, these files are **not** a
replay of the multi-GiB snapshots.

These small fixtures are synthetic-derived regression cases. They preserve only the
documented semantic pathologies needed at the v1 compatibility boundary:

- model-authored `graph_patch` dialects that the old graph schema rejected;
- a gapped result that must remain a proof attempt with explicit obligations;
- stable and free-text dependencies, with the latter materialized as obligations;
- a mechanism-only counterexample that must remain branch-local; and
- legacy graph nodes used to exercise proof-attempt backfill and main-target refutation
  quarantine planning.

`manifest.json` records the cited snapshot references, fixture provenance, and expected
behaviour. The JSON report files deliberately contain only the strict archived v1 schema so
they pass through the real compatibility adapter without test-only fields.
