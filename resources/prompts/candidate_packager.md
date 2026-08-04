# Candidate Proof Packager

Assemble a complete, self-contained proof package from the strongest established results.
Include the exact theorem, definitions, dependency graph, proof of every new lemma, imported
theorems with exact hypotheses, exceptional cases, quantitative bookkeeping, and final
logical deduction.

Explicitly classify whether the result is quantitative or algorithmic. Set
`quantitative_or_algorithmic` to true whenever correctness depends on constants, rates,
probabilities, precision, runtime, sample size, bit complexity, or another quantitative bound;
the field is mandatory and must not be omitted.

Do not hide unresolved obligations. If the route is incomplete, return an incomplete package
with a precise obligation list rather than presenting it as solved.

Follow each triggering report's `dependency_result_keys` DAG exactly. A replayed computation is
usable only when it lies in the exact-main result's bound transitive closure and its canonical
derivation, manifest, and replay artifacts are present; never attach an unrelated calculation to
an otherwise unsupported proof.

There are no allowed terminal reductions. Reject any attempt to package a proper subclass,
weaker conclusion, added hypothesis, equivalent restatement without proof, or unresolved reduced
claim as the exact theorem. A reduction is usable only when the downstream result and complete
transfer to the unchanged claim contract are both proved.

When the package resolves the target by an existing theorem, identify that theorem and its exact
hypotheses as imported material. Do not relabel a known result, its exposition, or its
formalization as a new theorem.

For every imported theorem, provide a stable source ID, canonical identifiers, exact hypotheses,
and evidence claims linked through source IDs. Leave verification to MATEK; an unverified
imported theorem remains an unresolved proof obligation.
