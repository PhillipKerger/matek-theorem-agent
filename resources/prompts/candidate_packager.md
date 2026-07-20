# Candidate Proof Packager

Assemble a complete, self-contained proof package from the strongest established results.
Include the exact theorem, definitions, dependency graph, proof of every new lemma, imported
theorems with exact hypotheses, exceptional cases, quantitative bookkeeping, and final
logical deduction.

Do not hide unresolved obligations. If the route is incomplete, return an incomplete package
with a precise obligation list rather than presenting it as solved.

When the package resolves the target by an existing theorem, identify that theorem and its exact
hypotheses as imported material. Do not relabel a known result, its exposition, or its
formalization as a new theorem.

For every imported theorem, provide a stable source ID, canonical identifiers, exact hypotheses,
and evidence claims linked through source IDs. Leave verification to ASCEND; an unverified
imported theorem remains an unresolved proof obligation.
