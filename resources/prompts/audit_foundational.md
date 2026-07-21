# Foundational Audit

Audit only the submitted theorem and proof package. Focus on definitions, quantifiers,
domains, boundary and degenerate cases, hidden assumptions, circularity, and whether the final
conclusion exactly matches the claim contract. Provide concrete counterexamples where
possible. Classify every issue by severity and identify the smallest exact repair obligation.
Independently check the package's `quantitative_or_algorithmic` classification. If it is false
but the theorem or proof depends on a quantitative bound, rate, probability, precision, runtime,
sample size, or complexity claim, return a blocking issue requiring the complexity audit.
