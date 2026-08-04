from __future__ import annotations

import json
from pathlib import Path

import pytest

from matek_theorem_agent.knowledge_graph.ledger import (
    CanonicalLedger,
    ClaimStatus,
    Derivation,
    DerivationStatus,
    LedgerClaim,
    LedgerError,
    Obligation,
    ObligationStatus,
    load_canonical_ledger,
    logical_version,
    obligation_logical_version,
    refresh_derivation_staleness,
    smallest_known_open_cut,
    trusted_claim_ids,
    validate_ledger,
    write_canonical_ledger,
)
from matek_theorem_agent.scientific import ScientificScope


def claim(claim_id: str, statement: str, *, status: ClaimStatus) -> LedgerClaim:
    return LedgerClaim(
        claim_id=claim_id,
        exact_statement=statement,
        logical_version=logical_version(statement),
        status=status,
    )


def derivation(
    derivation_id: str,
    conclusion: LedgerClaim,
    premises: list[LedgerClaim],
    *,
    status: DerivationStatus = DerivationStatus.AUDIT_PASSED,
    obligation_ids: list[str] | None = None,
) -> Derivation:
    return Derivation(
        derivation_id=derivation_id,
        conclusion_claim_id=conclusion.claim_id,
        premise_claim_ids=[item.claim_id for item in premises],
        proof_attempt_id=f"PRF-{derivation_id.removeprefix('DRV-')}",
        exact_target_version=conclusion.logical_version,
        premise_versions={item.claim_id: item.logical_version for item in premises},
        obligation_ids=obligation_ids or [],
        status=status,
    )


def ledger(
    claims: list[LedgerClaim],
    derivations: list[Derivation],
    *,
    obligations: list[Obligation] | None = None,
    target: str = "CLM-TARGET001",
) -> CanonicalLedger:
    return CanonicalLedger(
        graph_revision="00000001-0123456789abcdef",
        problem_id="PRB-PROBLEM1",
        target_claim_id=target,
        claims={item.claim_id: item for item in claims},
        derivations={item.derivation_id: item for item in derivations},
        obligations={item.obligation_id: item for item in obligations or []},
    )


def test_audited_derivation_has_and_premises() -> None:
    target = claim("CLM-TARGET001", "For every n, P(n).", status=ClaimStatus.PROPOSED)
    first = claim("CLM-PREMISE1", "P(0).", status=ClaimStatus.AUDIT_PASSED)
    second = claim("CLM-PREMISE2", "P(n) implies P(n+1).", status=ClaimStatus.OPEN)
    route = derivation("DRV-ROUTE001", target, [first, second])
    current = ledger([target, first, second], [route])

    assert target.claim_id not in trusted_claim_ids(current)
    assert smallest_known_open_cut(current).obligation_ids == [second.claim_id]

    resolved_second = second.model_copy(update={"status": ClaimStatus.AUDIT_PASSED})
    resolved = ledger([target, first, resolved_second], [route])
    assert target.claim_id in trusted_claim_ids(resolved)
    assert smallest_known_open_cut(resolved).obligation_ids == []


def test_or_derivation_preserves_trust_when_sibling_becomes_stale() -> None:
    target = claim("CLM-TARGET001", "The exact target.", status=ClaimStatus.PROPOSED)
    stale_route = derivation("DRV-STALE001", target, [])
    stale_route = stale_route.model_copy(
        update={"exact_target_version": logical_version("An obsolete target.")}
    )
    live_route = derivation("DRV-LIVEROUTE", target, [])
    current = ledger([target], [stale_route, live_route])

    refreshed = refresh_derivation_staleness(current)

    assert refreshed.derivations[stale_route.derivation_id].status is DerivationStatus.STALE
    assert refreshed.derivations[live_route.derivation_id].status is DerivationStatus.AUDIT_PASSED
    assert target.claim_id in trusted_claim_ids(refreshed)


def test_open_obligation_participates_in_cut_until_resolved() -> None:
    target = claim("CLM-TARGET001", "The exact target.", status=ClaimStatus.PROPOSED)
    obligation = Obligation(
        obligation_id="OBL-BOUNDARY1",
        exact_statement="Handle the zero boundary case.",
        conclusion="The zero boundary case holds.",
        parent_derivation_ids=["DRV-ROUTE001"],
        logical_version=obligation_logical_version(
            "Handle the zero boundary case.",
            conclusion="The zero boundary case holds.",
        ),
        status=ObligationStatus.OPEN,
        estimated_leverage=90,
    )
    route = derivation(
        "DRV-ROUTE001",
        target,
        [],
        status=DerivationStatus.PROPOSED,
        obligation_ids=[obligation.obligation_id],
    )
    current = ledger([target], [route], obligations=[obligation])

    assert smallest_known_open_cut(current).obligation_ids == [
        route.derivation_id,
        obligation.obligation_id,
    ]

    resolved_obligation = obligation.model_copy(update={"status": ObligationStatus.RESOLVED})
    audited_route = route.model_copy(update={"status": DerivationStatus.AUDIT_PASSED})
    resolved = ledger([target], [audited_route], obligations=[resolved_obligation])
    assert target.claim_id in trusted_claim_ids(resolved)


def test_obligation_logical_version_covers_every_semantic_contract_field() -> None:
    baseline = obligation_logical_version(
        "For every n, prove P(n).",
        conclusion="P(n).",
        quantifiers=["For every natural number n."],
        hypotheses=["n is even."],
        dependency_claim_ids=["CLM-PREMISE1"],
        target_claim_ids=["CLM-TARGET001"],
        scope=ScientificScope.BRANCH,
        notation_definition_version="1",
        falsification_evidence=["The n = 0 case remains unresolved."],
    )
    changed_versions = {
        obligation_logical_version(
            "For every n, prove Q(n).",
            conclusion="P(n).",
            quantifiers=["For every natural number n."],
            hypotheses=["n is even."],
            dependency_claim_ids=["CLM-PREMISE1"],
            target_claim_ids=["CLM-TARGET001"],
            scope=ScientificScope.BRANCH,
            notation_definition_version="1",
            falsification_evidence=["The n = 0 case remains unresolved."],
        ),
        obligation_logical_version(
            "For every n, prove P(n).",
            conclusion="Q(n).",
            quantifiers=["For every natural number n."],
            hypotheses=["n is even."],
            dependency_claim_ids=["CLM-PREMISE1"],
            target_claim_ids=["CLM-TARGET001"],
            scope=ScientificScope.BRANCH,
            notation_definition_version="1",
            falsification_evidence=["The n = 0 case remains unresolved."],
        ),
        obligation_logical_version(
            "For every n, prove P(n).",
            conclusion="P(n).",
            quantifiers=["For every integer n."],
            hypotheses=["n is even."],
            dependency_claim_ids=["CLM-PREMISE1"],
            target_claim_ids=["CLM-TARGET001"],
            scope=ScientificScope.BRANCH,
            notation_definition_version="1",
            falsification_evidence=["The n = 0 case remains unresolved."],
        ),
        obligation_logical_version(
            "For every n, prove P(n).",
            conclusion="P(n).",
            quantifiers=["For every natural number n."],
            hypotheses=["n is odd."],
            dependency_claim_ids=["CLM-PREMISE1"],
            target_claim_ids=["CLM-TARGET001"],
            scope=ScientificScope.BRANCH,
            notation_definition_version="1",
            falsification_evidence=["The n = 0 case remains unresolved."],
        ),
        obligation_logical_version(
            "For every n, prove P(n).",
            conclusion="P(n).",
            quantifiers=["For every natural number n."],
            hypotheses=["n is even."],
            dependency_claim_ids=["CLM-PREMISE2"],
            target_claim_ids=["CLM-TARGET001"],
            scope=ScientificScope.BRANCH,
            notation_definition_version="1",
            falsification_evidence=["The n = 0 case remains unresolved."],
        ),
        obligation_logical_version(
            "For every n, prove P(n).",
            conclusion="P(n).",
            quantifiers=["For every natural number n."],
            hypotheses=["n is even."],
            dependency_claim_ids=["CLM-PREMISE1"],
            target_claim_ids=["CLM-TARGET002"],
            scope=ScientificScope.BRANCH,
            notation_definition_version="1",
            falsification_evidence=["The n = 0 case remains unresolved."],
        ),
        obligation_logical_version(
            "For every n, prove P(n).",
            conclusion="P(n).",
            quantifiers=["For every natural number n."],
            hypotheses=["n is even."],
            dependency_claim_ids=["CLM-PREMISE1"],
            target_claim_ids=["CLM-TARGET001"],
            scope=ScientificScope.REDUCTION,
            notation_definition_version="1",
            falsification_evidence=["The n = 0 case remains unresolved."],
        ),
        obligation_logical_version(
            "For every n, prove P(n).",
            conclusion="P(n).",
            quantifiers=["For every natural number n."],
            hypotheses=["n is even."],
            dependency_claim_ids=["CLM-PREMISE1"],
            target_claim_ids=["CLM-TARGET001"],
            scope=ScientificScope.BRANCH,
            notation_definition_version="2",
            falsification_evidence=["The n = 0 case remains unresolved."],
        ),
        obligation_logical_version(
            "For every n, prove P(n).",
            conclusion="P(n).",
            quantifiers=["For every natural number n."],
            hypotheses=["n is even."],
            dependency_claim_ids=["CLM-PREMISE1"],
            target_claim_ids=["CLM-TARGET001"],
            scope=ScientificScope.BRANCH,
            notation_definition_version="1",
            falsification_evidence=["The n = 1 case fails."],
        ),
    }

    assert baseline not in changed_versions
    assert len(changed_versions) == 9


@pytest.mark.parametrize("link_owner", ["derivation", "obligation"])
def test_derivation_obligation_links_must_be_reciprocal(link_owner: str) -> None:
    target = claim("CLM-TARGET001", "The exact target.", status=ClaimStatus.PROPOSED)
    obligation = Obligation(
        obligation_id="OBL-BLOCKER01",
        exact_statement="Prove the remaining blocker.",
        conclusion="The remaining blocker holds.",
        parent_derivation_ids=(["DRV-ROUTE001"] if link_owner == "obligation" else []),
        logical_version=obligation_logical_version(
            "Prove the remaining blocker.",
            conclusion="The remaining blocker holds.",
        ),
        status=ObligationStatus.OPEN,
    )
    route = derivation(
        "DRV-ROUTE001",
        target,
        [],
        obligation_ids=([obligation.obligation_id] if link_owner == "derivation" else []),
    )
    current = ledger([target], [route], obligations=[obligation])

    with pytest.raises(LedgerError, match="non-reciprocal"):
        validate_ledger(current)
    with pytest.raises(LedgerError, match="non-reciprocal"):
        trusted_claim_ids(current)


def test_no_known_derivation_reports_target_as_open_cut() -> None:
    target = claim("CLM-TARGET001", "The exact target.", status=ClaimStatus.OPEN)
    cut = smallest_known_open_cut(ledger([target], []))

    assert cut.obligation_ids == [target.claim_id]
    assert cut.alternative_cuts == [[target.claim_id]]


def test_standalone_direct_obligations_form_one_joint_open_cut() -> None:
    target = claim("CLM-TARGET001", "The exact target.", status=ClaimStatus.OPEN)
    first_statement = "Prove the first independent boundary condition."
    second_statement = "Prove the second independent boundary condition."
    first = Obligation(
        obligation_id="OBL-FIRST001",
        exact_statement=first_statement,
        conclusion="The first boundary condition holds.",
        target_claim_ids=[target.claim_id],
        logical_version=obligation_logical_version(
            first_statement,
            conclusion="The first boundary condition holds.",
            target_claim_ids=[target.claim_id],
        ),
    )
    second = Obligation(
        obligation_id="OBL-SECOND01",
        exact_statement=second_statement,
        conclusion="The second boundary condition holds.",
        target_claim_ids=[target.claim_id],
        logical_version=obligation_logical_version(
            second_statement,
            conclusion="The second boundary condition holds.",
            target_claim_ids=[target.claim_id],
        ),
    )

    cut = smallest_known_open_cut(ledger([target], [], obligations=[first, second]))

    assert cut.obligation_ids == [first.obligation_id, second.obligation_id]
    assert cut.alternative_cuts == [[first.obligation_id, second.obligation_id]]


def test_proposed_gap_free_derivation_remains_an_audit_cut() -> None:
    target = claim("CLM-TARGET001", "The exact target.", status=ClaimStatus.PROPOSED)
    route = derivation(
        "DRV-ROUTE001",
        target,
        [],
        status=DerivationStatus.PROPOSED,
    )

    cut = smallest_known_open_cut(ledger([target], [route]))

    assert target.claim_id not in trusted_claim_ids(ledger([target], [route]))
    assert cut.obligation_ids == [route.derivation_id]
    assert cut.alternative_cuts == [[route.derivation_id]]


def test_proposed_conditional_route_keeps_both_audit_and_premise_in_cut() -> None:
    target = claim("CLM-TARGET001", "The exact target.", status=ClaimStatus.PROPOSED)
    premise = claim("CLM-PREMISE1", "The missing joint premise.", status=ClaimStatus.OPEN)
    route = derivation(
        "DRV-ROUTE001",
        target,
        [premise],
        status=DerivationStatus.PROPOSED,
    )
    current = ledger([target, premise], [route])

    assert target.claim_id not in trusted_claim_ids(current)
    assert set(smallest_known_open_cut(current).obligation_ids) == {
        route.derivation_id,
        premise.claim_id,
    }


def test_cyclic_derivation_hypergraph_is_rejected() -> None:
    first = claim("CLM-TARGET001", "Claim A.", status=ClaimStatus.PROPOSED)
    second = claim("CLM-PREMISE2", "Claim B.", status=ClaimStatus.PROPOSED)
    route_a = derivation("DRV-ROUTE001", first, [second])
    route_b = derivation("DRV-ROUTE002", second, [first])
    current = ledger([first, second], [route_a, route_b])

    with pytest.raises(LedgerError, match="cyclic derivation support"):
        validate_ledger(current)


def test_ledger_round_trip_and_integrity_check(tmp_path: Path) -> None:
    target = claim("CLM-TARGET001", "The exact target.", status=ClaimStatus.OPEN)
    current = ledger([target], [])
    path = tmp_path / "canonical-ledger.json"

    write_canonical_ledger(path, current)

    assert load_canonical_ledger(path) == current
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["claims"][target.claim_id]["exact_statement"] = "Tampered target."
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(LedgerError, match=r"schema is invalid|integrity digest"):
        load_canonical_ledger(path)
