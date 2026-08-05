Matroid Secretary Conjecture

Let (M=(E,\mathcal I)) be a finite matroid known to an online algorithm. Before the process begins, an adversary assigns a nonnegative weight (w(e)) to every element (e\in E). The elements then arrive in a uniformly random order. When (e) arrives, its weight is revealed, and the algorithm must irrevocably accept or reject it while ensuring that the accepted set always belongs to (\mathcal I). Let[\operatorname{OPT}(M,w)=\max_{I\in\mathcal I}\sum_{e\in I}w(e).]

Open problem

Prove that there are a universal constant (C<\infty) and a randomized online algorithm such that, for every matroid and every adversarial weight assignment, the final accepted set (I_{ALG}) satisfies[\mathbb E[w(I_{ALG})]\ge \frac1C\operatorname{OPT}(M,w),]where the expectation is over the random arrival order and the algorithm's internal randomness. The constant must be independent of the size and rank of the matroid.