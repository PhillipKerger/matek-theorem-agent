from __future__ import annotations

import json

import pytest

from matek_theorem_agent.scientific import (
    AlgorithmRandomization,
    ArrivalRandomness,
    FeasibilityRequirement,
    TargetClauseCategory,
    TargetPolarity,
    ValueGuarantee,
    classify_requested_polarity,
    validate_target_contract,
)


@pytest.mark.parametrize(
    ("contract", "expected_category"),
    [
        ({"domain": "planar graphs"}, TargetClauseCategory.DOMAIN),
        (
            {"edge_cases": "including empty instances"},
            TargetClauseCategory.EDGE_CASES,
        ),
    ],
)
def test_missing_domain_or_edge_prose_is_not_treated_as_a_contradiction(
    contract: dict[str, str],
    expected_category: TargetClauseCategory,
) -> None:
    alignment = validate_target_contract("Prove P for every object.", contract)

    assert alignment.passed is True
    assert len(alignment.checks) == 1
    check = alignment.checks[0]
    assert check.category is expected_category
    assert check.passed is True
    assert check.detail == "No high-confidence contradiction detected."


def test_structured_and_list_clauses_align_with_visible_material_and_numeric_constant() -> None:
    contract = {
        "domain": json.dumps(["connected planar graphs"]),
        "edge_cases": json.dumps({"included": ["empty graphs", "one vertex graphs"]}),
        "constants": json.dumps({"factor": 3}),
    }

    alignment = validate_target_contract(
        (
            "For every connected planar graph, including the empty graph and one-vertex graphs, "
            "P holds with factor 3."
        ),
        contract,
    )

    assert alignment.passed is True
    assert all(check.passed for check in alignment.checks)


@pytest.mark.parametrize(
    "statement",
    [
        "For every connected graph, including the empty graph and one-vertex graphs, "
        "P holds with factor 3.",
        "For every connected planar graph and every one-vertex graph, P holds with factor 3.",
    ],
)
def test_structured_prose_omissions_do_not_block_without_an_explicit_conflict(
    statement: str,
) -> None:
    contract = {
        "domain": json.dumps(["connected planar graphs"]),
        "edge_cases": json.dumps({"included": ["empty graphs", "one vertex graphs"]}),
        "constants": json.dumps({"factor": 3}),
    }

    alignment = validate_target_contract(statement, contract)

    assert alignment.passed is True


def test_structured_numeric_change_is_preserved_as_a_diagnostic_warning() -> None:
    alignment = validate_target_contract(
        (
            "For every connected planar graph, including the empty graph and one-vertex graphs, "
            "P holds with factor 2."
        ),
        {"constants": json.dumps({"factor": 3})},
    )

    assert alignment.passed is True
    assert not alignment.blocking_issues
    assert alignment.alignment_warnings
    assert "numeric value 3" in " ".join(alignment.warnings)


def test_existing_quantitative_domain_contract_remains_aligned() -> None:
    alignment = validate_target_contract(
        (
            "Prove that on arbitrary metric spaces, for every k there exists beta such that "
            "cost_ALG <= k * OPT + beta."
        ),
        {
            "quantifiers": "for every k there exists beta",
            "domain": "arbitrary metric spaces",
            "additive_terms": "cost_ALG <= k * OPT + beta",
            "polarity": "prove",
            "conclusion": "cost_ALG <= k * OPT + beta",
        },
    )

    assert alignment.passed is True


def test_comparison_clause_reports_an_extra_material_rhs_term() -> None:
    alignment = validate_target_contract(
        "For every k, cost_ALG <= k * OPT + gamma + beta.",
        {"conclusion": "cost_ALG <= k * OPT + beta"},
    )

    assert alignment.passed is True
    assert not alignment.blocking_issues
    assert alignment.alignment_warnings
    assert "compact formal comparison" in " ".join(alignment.warnings)


@pytest.mark.parametrize(
    "contract",
    [
        {"qualifiers": json.dumps({"randomized": False})},
        {"randomized": json.dumps(False)},
    ],
)
@pytest.mark.parametrize(
    ("statement", "expected_passed"),
    [
        ("For every input, a deterministic algorithm succeeds.", True),
        ("For every input, a randomized algorithm succeeds.", False),
    ],
)
def test_structured_false_qualifier_is_a_negative_requirement(
    contract: dict[str, str],
    statement: str,
    expected_passed: bool,
) -> None:
    alignment = validate_target_contract(statement, contract)

    assert alignment.passed is True
    if not expected_passed:
        assert not alignment.blocking_issues
        assert alignment.alignment_warnings
        assert "not randomized" in " ".join(alignment.warnings)


@pytest.mark.parametrize(
    ("contract_value", "reversed_statement"),
    [
        (
            json.dumps({"forall": ["metric space X"]}),
            "There exists a metric space X for which P holds.",
        ),
        (
            json.dumps({"exists": ["graph G"]}),
            "For every graph G, P holds.",
        ),
        (
            json.dumps({"kind": "forall", "variable": "X"}),
            "There exists an X for which P holds.",
        ),
        (
            json.dumps({"exists": True}),
            "For every graph, P holds.",
        ),
    ],
)
def test_structured_quantifier_polarity_cannot_be_reversed(
    contract_value: str,
    reversed_statement: str,
) -> None:
    alignment = validate_target_contract(
        reversed_statement,
        {"quantifiers": contract_value},
    )

    assert alignment.passed is True
    assert not alignment.blocking_issues
    assert alignment.alignment_warnings
    assert "quantifier" in " ".join(alignment.warnings).casefold()


def test_matroid_secretary_paraphrase_does_not_fail_on_explanatory_prose() -> None:
    statement = (
        "Prove the following affirmative theorem. There exist one real universal constant C "
        "with 1≤C<∞ and one randomized online algorithm ALG such that, for every finite labeled "
        "matroid M=(E,I)—including E=∅, rank zero or one, loops, parallel elements, and nonunique "
        "bases—and every arbitrary weight function w:E→R_{\x1c22650} with zeros and ties allowed, "
        "the following holds. The entire matroid M, E, and |E| are known to ALG in advance, "
        "equivalently through exact unrestricted independence access, but weights are not known "
        "until arrival. An oblivious adversary may choose w after seeing M and ALG but must fix w "
        "before the uniformly random arrival permutation \x1c03c0 and ALG's private random coins R "
        "are realized. Each element arrives exactly once according to \x1c03c0; upon seeing its "
        "identity and weight, ALG must immediately and irrevocably accept or reject it using only "
        "M, the revealed history, and R. Every accepted prefix and the final accepted set I_ALG "
        "must lie in I in every realization. For all M and w, "
        "E_{\x1c03c0,R}[Σ_{e∈I_ALG}w(e)]≥(1/C)OPT(M,w), where "
        "OPT(M,w)=max_{J∈I}Σ_{e∈J}w(e). The constant C and algorithmic guarantee have no "
        "dependence on M, |E|, rank(M), w, weight spread, or any structural parameter, and contain "
        "no additive term, exceptional set, or failure-probability relaxation. The expectation is "
        "jointly over the uniform order and internal randomness, while feasibility is pointwise. "
        "The OPT=0 case is included."
    )
    contract = {
        "quantifiers": (
            "There must exist one universal real constant C and one randomized online algorithm "
            "ALG such that, for every finite matroid M=(E,I) and every weight function "
            "w:E→R_{\x1c22650} fixed before any randomness is realized, the stated feasibility "
            "and expected-value guarantees hold."
        ),
        "constants": (
            "C must satisfy 1≤C<∞ and be independent of M, |E|, rank(M), w, the weight spread, "
            "and every other instance parameter."
        ),
        "additive_terms": (
            "No additive term, including no -β or +β, asymptotic error, exceptional loss, or "
            "failure-probability allowance "
            "is permitted; the guarantee is purely multiplicative: E[w(I_ALG)]≥OPT(M,w)/C."
        ),
        "domain": (
            "All finite matroids are covered. The labeled ground set E and the complete matroid "
            "are known in advance; equivalently, ALG has exact unrestricted independence-oracle "
            "access to M. Elements arrive exactly once in a uniformly random permutation. Weights "
            "are arbitrary nonnegative real numbers and are revealed only on arrival."
        ),
        "edge_cases": (
            "The theorem includes the empty matroid, rank-zero and rank-one matroids, loops, "
            "parallel elements, zero weights, tied weights, and OPT(M,w)=0. Any tie-breaking used "
            "by ALG must be specified without access to unrevealed weights."
        ),
        "conclusion": (
            "For every admissible M and w, every accepted prefix and the final set I_ALG must "
            "belong to I in every realization, and "
            "E_{\x1c03c0,R}[Σ_{e∈I_ALG}w(e)]≥(1/C)max_{J∈I}Σ_{e∈J}w(e), where "
            "\x1c03c0 is the uniform arrival permutation and R is ALG's private randomness."
        ),
        "online_information": (
            "ALG knows M, E, and |E| before arrivals, but not any weight until its element "
            "arrives. "
            "Each accept/reject decision is immediate and irrevocable and may depend only on M, "
            "the revealed history, and private randomness."
        ),
    }

    alignment = validate_target_contract(statement, contract)

    assert alignment.passed is True
    assert all(check.passed for check in alignment.checks)


def test_negated_online_decision_prohibitions_preserve_immediate_irrevocable_mode() -> None:
    alignment = validate_target_contract(
        (
            "ALG makes immediate irrevocable decisions, keeps every accepted prefix "
            "independent, and returns the required expected-value guarantee."
        ),
        {
            "online_decisions": (
                "When an element's identity and weight are revealed, the algorithm must "
                "immediately and irrevocably accept or reject it. It may not revoke, exchange, "
                "buffer, shortlist, or defer decisions."
            )
        },
    )

    assert alignment.passed is True
    assert alignment.blocking_issues == []
    check = alignment.checks[0]
    assert check.contract_values == {
        "timing": "immediate",
        "revision": "irrevocable",
    }
    assert check.statement_values == {
        "timing": "immediate",
        "revision": "irrevocable",
    }


@pytest.mark.parametrize(
    ("prohibition", "field", "expected"),
    [
        ("The algorithm may not defer decisions.", "timing", "immediate"),
        ("The algorithm cannot buffer or shortlist decisions.", "timing", "immediate"),
        ("The algorithm may not revoke an accepted element.", "revision", "irrevocable"),
        ("The algorithm must not exchange accepted elements.", "revision", "irrevocable"),
    ],
)
def test_online_decision_prohibitions_are_not_read_as_permissions(
    prohibition: str,
    field: str,
    expected: str,
) -> None:
    alignment = validate_target_contract(
        "ALG makes immediate and irrevocable accept-or-reject decisions.",
        {"online_decisions": prohibition},
    )

    assert alignment.passed is True
    assert alignment.checks[0].contract_values[field] == expected


def test_nearby_unrelated_negation_does_not_hide_permitted_deferral_or_revocation() -> None:
    alignment = validate_target_contract(
        "ALG makes immediate and irrevocable accept-or-reject decisions.",
        {
            "online_decisions": (
                "Future weights may not be known before arrival, but decisions may be deferred "
                "and accepted elements may be revoked."
            )
        },
    )

    assert alignment.passed is True
    assert not alignment.blocking_issues
    assert alignment.alignment_warnings
    check = alignment.checks[0]
    assert check.contract_values == {
        "timing": "deferred",
        "revision": "revocable",
    }
    assert {"timing", "revision"} <= {
        conflict.partition(":")[0] for conflict in check.material_conflicts
    }


def test_long_prose_conclusion_reports_reversed_comparison() -> None:
    alignment = validate_target_contract(
        (
            "A separate normalization has C >= 1. For every finite matroid M and weight function "
            "w, the randomized algorithm ALG returns a feasible set I_ALG and "
            "E[w(I_ALG)] <= (1/C) OPT(M,w)."
        ),
        {
            "conclusion": (
                "For every admissible finite matroid M and nonnegative weight function w, every "
                "accepted prefix is feasible and the final set I_ALG satisfies "
                "E[w(I_ALG)] >= (1/C) OPT(M,w)."
            )
        },
    )

    assert alignment.passed is True
    assert not alignment.blocking_issues
    assert alignment.alignment_warnings
    assert "reversed comparison direction" in " ".join(alignment.warnings)


# --- Structured polarity alignment (P0 reliability fix) ---------------------------------


_INCIDENT_POLARITY_CLAUSE = (
    "The requested outcome is an affirmative proof and algorithm. A survey, a "
    "restricted-family barrier theorem, a weaker variant, or a counterexample to a "
    "restricted algorithm class does not satisfy it."
)
_INCIDENT_STATEMENT = (
    "Affirmatively prove the following exact theorem. There exist one real universal "
    "constant C and one randomized online algorithm ALG such that, for every finite matroid, "
    "the expected weight of ALG is at least OPT/C. The requested polarity is an affirmative "
    "proof and algorithm, not a survey, restricted-family barrier, or weaker variant. A "
    "counterexample to a restricted algorithm class does not resolve the target."
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("prove", TargetPolarity.AFFIRMATIVE_PROOF),
        ("Affirmatively prove the following exact theorem.", TargetPolarity.AFFIRMATIVE_PROOF),
        (_INCIDENT_POLARITY_CLAUSE, TargetPolarity.AFFIRMATIVE_PROOF),
        (_INCIDENT_STATEMENT, TargetPolarity.AFFIRMATIVE_PROOF),
        ("affirmative_proof", TargetPolarity.AFFIRMATIVE_PROOF),
        ("Disprove the stated conjecture.", TargetPolarity.DISPROOF),
        ("refute the conjecture by a counterexample", TargetPolarity.DISPROOF),
        ("disproof", TargetPolarity.DISPROOF),
        ("Prove or disprove the conjecture.", TargetPolarity.INVESTIGATION),
        ("Classify all finite simple groups with the property.", TargetPolarity.CLASSIFICATION),
        ("Construct an explicit family of expanders.", TargetPolarity.CONSTRUCTION),
        (
            "Prove the theorem; a counterexample to an intermediate lemma is only progress.",
            TargetPolarity.AFFIRMATIVE_PROOF,
        ),
        ("The remaining obstruction is unclear.", TargetPolarity.AMBIGUOUS),
    ],
)
def test_classify_requested_polarity_uses_leading_directive(
    text: str,
    expected: TargetPolarity,
) -> None:
    assert classify_requested_polarity(text) is expected


def test_incident_affirmative_polarity_passes_despite_excluded_counterexample() -> None:
    alignment = validate_target_contract(
        _INCIDENT_STATEMENT,
        {"polarity": _INCIDENT_POLARITY_CLAUSE},
    )

    assert alignment.passed is True
    assert alignment.blocking_issues == []
    assert alignment.polarity is not None
    assert alignment.polarity.contract_polarity is TargetPolarity.AFFIRMATIVE_PROOF
    assert alignment.polarity.statement_polarity is TargetPolarity.AFFIRMATIVE_PROOF
    assert alignment.polarity.material_contradiction is False
    assert alignment.polarity.gate == "target_polarity_alignment"


@pytest.mark.parametrize(
    "clause_value",
    [
        "counterexample",
        "A counterexample to a natural intermediate conjecture is an insufficient outcome.",
        "The affirmative proof stands even if a formal barrier for a method family is disproved.",
        "Prove the target; excluded outcomes include a refuted method family and a barrier.",
    ],
)
def test_excluded_or_framework_disproof_words_alone_do_not_fail_polarity(
    clause_value: str,
) -> None:
    alignment = validate_target_contract(
        "Affirmatively prove the following exact theorem for every finite matroid.",
        {"polarity": clause_value},
    )

    assert alignment.passed is True


def test_explicit_structured_polarity_mismatch_is_diagnostic() -> None:
    alignment = validate_target_contract(
        "Disprove the stated conjecture by exhibiting a counterexample.",
        {"polarity": "affirmative_proof"},
    )

    assert alignment.passed is True
    assert not alignment.blocking_issues
    assert alignment.alignment_warnings
    assert "refute/disprove polarity" in " ".join(alignment.warnings)
    assert alignment.polarity is not None
    assert alignment.polarity.material_contradiction is True
    assert alignment.polarity.contract_polarity is TargetPolarity.AFFIRMATIVE_PROOF
    assert alignment.polarity.statement_polarity is TargetPolarity.DISPROOF


def test_non_material_polarity_uncertainty_warns_and_passes() -> None:
    alignment = validate_target_contract(
        "Construct an explicit algorithm achieving the guarantee.",
        {"polarity": "Prove the guarantee holds."},
    )

    assert alignment.passed is True
    assert alignment.warnings
    assert alignment.polarity is not None
    assert alignment.polarity.material_contradiction is False
    assert alignment.polarity.contract_polarity is TargetPolarity.AFFIRMATIVE_PROOF
    assert alignment.polarity.statement_polarity is TargetPolarity.CONSTRUCTION


# --- Structured randomness/feasibility alignment (P0 reliability fix) ------------------


_RANDOMNESS_CONTRACT = {
    "randomness": (
        "ALG is a randomized online policy with private random coins. Arrivals form a uniformly "
        "random permutation. The value guarantee is in expectation jointly over the arrival "
        "order and ALG's internal randomness, while feasibility holds deterministically "
        "conditional on every realization. Weights are fixed before randomness by an oblivious "
        "adversary."
    )
}


def test_randomness_incident_distinguishes_pathwise_feasibility_from_algorithm_type() -> None:
    statement = (
        "There exist one universal constant C and one randomized causal online policy ALG. "
        "Let pi be a uniformly random arrival permutation and let R be ALG's internal "
        "randomness. Weights are fixed before the permutation and coins are realized. Every "
        "accepted set is feasible for every realization. "
        "E_{pi,R}[w(I_ALG)] >= OPT/C."
    )

    alignment = validate_target_contract(statement, _RANDOMNESS_CONTRACT)

    assert alignment.passed is True
    assert alignment.randomness is not None
    assert alignment.randomness.material_contradiction is False
    assert alignment.randomness.contract.algorithm_randomization is (
        AlgorithmRandomization.ALLOWED_OR_REQUIRED
    )
    assert alignment.randomness.statement.algorithm_randomization is (
        AlgorithmRandomization.ALLOWED_OR_REQUIRED
    )
    assert alignment.randomness.contract.feasibility_requirement is FeasibilityRequirement.PATHWISE
    assert alignment.randomness.statement.feasibility_requirement is FeasibilityRequirement.PATHWISE
    assert alignment.randomness.statement.expectation_over == [
        "arrival_order",
        "algorithm_coins",
    ]
    check = alignment.checks[0]
    assert check.category is TargetClauseCategory.RANDOMNESS
    assert check.material_conflicts == []


def test_persisted_structured_randomness_object_is_compared_directly() -> None:
    contract = {
        "randomness": json.dumps(
            {
                "algorithm_randomization": "allowed_or_required",
                "arrival_randomness": "uniform_random_permutation",
                "weight_adversary": "oblivious_before_randomness",
                "expectation_over": ["arrival_order", "algorithm_coins"],
                "feasibility_requirement": "pathwise",
                "value_guarantee": "in_expectation",
            }
        )
    }
    statement = (
        "A randomized online algorithm uses private coins under a uniformly random arrival "
        "permutation. An oblivious adversary fixes weights before randomness. Feasibility is "
        "pathwise, and expected value over arrival order and algorithm coins is at least OPT/C."
    )

    alignment = validate_target_contract(statement, contract)

    assert alignment.passed is True
    assert alignment.randomness is not None
    assert alignment.randomness.contract.model_dump(mode="json") == json.loads(
        contract["randomness"]
    )


@pytest.mark.parametrize(
    "orthogonal_condition",
    [
        "Feasibility holds deterministically and pathwise for every realization.",
        "ALG uses deterministic tie-breaking inside the randomized policy.",
        "ALG performs deterministic preprocessing before using its private coins.",
        "The proof conditions on the realized random seed.",
        "The adversary fixes all weights before randomness is realized.",
    ],
)
def test_deterministic_execution_conditions_do_not_negate_randomized_policy(
    orthogonal_condition: str,
) -> None:
    statement = (
        "There is a randomized online policy ALG with private random coins under a uniformly "
        "random arrival permutation. The expected value over the arrival order and algorithm "
        f"coins is at least OPT/C. {orthogonal_condition}"
    )

    alignment = validate_target_contract(statement, _RANDOMNESS_CONTRACT)

    assert alignment.passed is True
    assert alignment.randomness is not None
    assert alignment.randomness.statement.algorithm_randomization is (
        AlgorithmRandomization.ALLOWED_OR_REQUIRED
    )


def test_explicit_deterministic_only_algorithm_reports_structured_randomness_conflict() -> None:
    alignment = validate_target_contract(
        (
            "ALG must be deterministic and may not use random coins. Arrivals are a uniformly "
            "random permutation. Its expected value over arrival order is at least OPT/C."
        ),
        _RANDOMNESS_CONTRACT,
    )

    assert alignment.passed is True
    assert not alignment.blocking_issues
    assert alignment.alignment_warnings
    assert alignment.randomness is not None
    assert alignment.randomness.material_contradiction is True
    assert alignment.randomness.contract.algorithm_randomization is (
        AlgorithmRandomization.ALLOWED_OR_REQUIRED
    )
    assert alignment.randomness.statement.algorithm_randomization is (
        AlgorithmRandomization.DETERMINISTIC_ONLY
    )
    check = alignment.checks[0]
    assert check.contract_values["algorithm_randomization"] == "allowed_or_required"
    assert check.statement_values["algorithm_randomization"] == "deterministic_only"
    assert "algorithm_randomization" in check.detail
    assert "Compared contract values" in check.detail


def test_adversarial_arrival_replacement_reports_compared_structured_values() -> None:
    alignment = validate_target_contract(
        (
            "There is a randomized online policy ALG with private coins for an adversarial "
            "arrival order. Its expected value over algorithm coins is at least OPT/C."
        ),
        _RANDOMNESS_CONTRACT,
    )

    assert alignment.passed is True
    assert not alignment.blocking_issues
    assert alignment.alignment_warnings
    assert alignment.randomness is not None
    assert alignment.randomness.contract.arrival_randomness is (
        ArrivalRandomness.UNIFORM_RANDOM_PERMUTATION
    )
    assert alignment.randomness.statement.arrival_randomness is (
        ArrivalRandomness.ADVERSARIAL_OR_DETERMINISTIC_ORDER
    )
    assert "arrival_randomness" in " ".join(alignment.warnings)


def test_expected_value_replaced_by_pathwise_value_is_diagnosed_separately() -> None:
    alignment = validate_target_contract(
        (
            "There is a randomized online policy with private coins under a uniformly random "
            "arrival permutation. Feasibility holds pathwise. The value guarantee holds "
            "pathwise for every realization."
        ),
        _RANDOMNESS_CONTRACT,
    )

    assert alignment.passed is True
    assert not alignment.blocking_issues
    assert alignment.alignment_warnings
    assert alignment.randomness is not None
    assert alignment.randomness.contract.value_guarantee is ValueGuarantee.IN_EXPECTATION
    assert alignment.randomness.statement.value_guarantee is ValueGuarantee.PATHWISE
    assert "value_guarantee" in " ".join(alignment.warnings)


def test_uncertain_randomness_alignment_warns_and_uses_frozen_contract() -> None:
    alignment = validate_target_contract(
        "Prove that ALG attains the stated guarantee while always remaining feasible.",
        _RANDOMNESS_CONTRACT,
    )

    assert alignment.passed is True
    assert alignment.randomness is not None
    assert alignment.randomness.material_contradiction is False
    assert alignment.randomness.warnings
    assert any("Structured randomness uncertainty" in warning for warning in alignment.warnings)


@pytest.mark.parametrize(
    ("key", "contract", "statement"),
    [
        ("quantifiers", "for every n", "For every n, P(n) holds."),
        ("constants", "C is universal", "There is a universal constant C."),
        ("domain", "all finite matroids", "The theorem covers all finite matroids."),
        (
            "information_model",
            "Decisions have no access to unseen weights.",
            "Each decision has no access to unseen weights.",
        ),
        (
            "online_decisions",
            "Each decision is immediate and irrevocable.",
            "Each decision is immediate and irrevocable.",
        ),
        (
            "feasibility",
            "Feasibility holds pathwise for every realization.",
            "Feasibility holds pathwise for every realization.",
        ),
        (
            "randomness",
            "Arrivals are a uniformly random permutation.",
            "Arrivals are a uniformly random permutation.",
        ),
        ("conclusion", "value_ALG >= OPT/C", "value_ALG >= OPT/C"),
        ("polarity", "affirmative_proof", "Prove the theorem."),
    ],
)
def test_clause_specific_structured_alignment_corpus(
    key: str,
    contract: str,
    statement: str,
) -> None:
    alignment = validate_target_contract(statement, {key: contract})

    assert alignment.passed is True
    check = alignment.checks[0]
    assert check.passed is True
    assert check.contract_values
    assert check.statement_values


@pytest.mark.parametrize(
    ("key", "contract", "statement", "field"),
    [
        ("quantifiers", "for every n", "there exists n", "forall n"),
        ("constants", "C is universal", "C may depend on the instance", "scope"),
        ("domain", "all finite matroids", "all infinite matroids", "finiteness"),
        (
            "information_model",
            "Decisions have no access to unseen weights.",
            "Decisions may inspect future unseen weights.",
            "unseen_information",
        ),
        (
            "online_decisions",
            "Each decision is immediate and irrevocable.",
            "Each decision may be delayed and revocable.",
            "timing",
        ),
        (
            "feasibility",
            "Feasibility holds pathwise for every realization.",
            "The algorithm is feasible only in expectation.",
            "feasibility_requirement",
        ),
        (
            "randomness",
            "Arrivals are a uniformly random permutation.",
            "Arrivals use an adversarial order.",
            "arrival_randomness",
        ),
        ("conclusion", "value_ALG >= OPT/C", "value_ALG <= OPT/C", "comparison"),
        ("polarity", "affirmative_proof", "Disprove the conjecture.", "polarity"),
    ],
)
def test_clause_specific_structured_conflict_corpus(
    key: str,
    contract: str,
    statement: str,
    field: str,
) -> None:
    alignment = validate_target_contract(statement, {key: contract})

    assert alignment.passed is True
    assert not alignment.blocking_issues
    assert alignment.alignment_warnings
    check = alignment.checks[0]
    assert check.passed is False
    assert check.contract_values
    assert check.statement_values
    assert field in check.detail
    assert "Compared contract values" in check.detail
