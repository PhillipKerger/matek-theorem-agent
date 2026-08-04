from __future__ import annotations

import json

import pytest

from matek_theorem_agent.scientific import TargetClauseCategory, validate_target_contract


@pytest.mark.parametrize(
    ("contract", "expected_category", "missing_markers"),
    [
        ({"domain": "planar graphs"}, TargetClauseCategory.DOMAIN, {"planar", "graph"}),
        (
            {"edge_cases": "including empty instances"},
            TargetClauseCategory.EDGE_CASES,
            {"empty", "instance"},
        ),
    ],
)
def test_generic_statement_cannot_satisfy_specific_domain_or_edge_case(
    contract: dict[str, str],
    expected_category: TargetClauseCategory,
    missing_markers: set[str],
) -> None:
    alignment = validate_target_contract("Prove P for every object.", contract)

    assert alignment.passed is False
    assert len(alignment.checks) == 1
    check = alignment.checks[0]
    assert check.category is expected_category
    assert check.passed is False
    reported_markers = set(
        check.detail.removeprefix("Missing or incompatible material marker(s): ").split(", ")
    )
    assert missing_markers.issubset(reported_markers)


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
    ("statement", "missing_marker"),
    [
        (
            "For every connected graph, including the empty graph and one-vertex graphs, "
            "P holds with factor 3.",
            "planar",
        ),
        (
            "For every connected planar graph and every one-vertex graph, P holds with factor 3.",
            "empty",
        ),
        (
            "For every connected planar graph, including the empty graph and one-vertex graphs, "
            "P holds with factor 2.",
            "3",
        ),
    ],
)
def test_structured_clause_omissions_fail_closed(statement: str, missing_marker: str) -> None:
    contract = {
        "domain": json.dumps(["connected planar graphs"]),
        "edge_cases": json.dumps({"included": ["empty graphs", "one vertex graphs"]}),
        "constants": json.dumps({"factor": 3}),
    }

    alignment = validate_target_contract(statement, contract)

    assert alignment.passed is False
    assert missing_marker in " ".join(alignment.blocking_issues)


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


def test_comparison_clause_rejects_an_extra_material_rhs_term() -> None:
    alignment = validate_target_contract(
        "For every k, cost_ALG <= k * OPT + gamma + beta.",
        {"conclusion": "cost_ALG <= k * OPT + beta"},
    )

    assert alignment.passed is False
    assert "ordered comparison sides" in " ".join(alignment.blocking_issues)


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

    assert alignment.passed is expected_passed
    if not expected_passed:
        assert "not randomized" in " ".join(alignment.blocking_issues)


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

    assert alignment.passed is False
    assert "quantifier" in " ".join(alignment.blocking_issues).casefold()
