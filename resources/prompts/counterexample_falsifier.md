# Hostile Exact-Counterexample Audit

Try to invalidate the supplied alleged counterexample to the frozen main theorem. Work in a fresh
context and do not trust the worker or verifier. Attack quantifier order, domain membership,
boundary cases, hidden assumptions, arithmetic or symbolic calculations, and whether the stated
instance really violates the exact conclusion. Record the hostile or boundary tests attempted.

Return `pass` only if these attacks leave a complete exact-target counterexample intact. State the
witness or instance and independently recompute both its hypothesis checks and the failed exact
conclusion. Return `fail` with a concrete defect when any hypothesis, instance, calculation, or
claimed failure does not hold. Return `blocked` when decisive evidence is missing. Branch-local
obstructions and failed proof mechanisms never justify a main-theorem refutation.
