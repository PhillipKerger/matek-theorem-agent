from __future__ import annotations

from datetime import UTC, datetime

import pytest

from matek_theorem_agent.knowledge_graph import (
    ClaimType,
    EpistemicStatus,
    GraphEdge,
    GraphNode,
    KnowledgeGraph,
    NodeType,
    RelationType,
)

PROBLEM_ID = "PRB-TRUST001"
TARGET_ID = "CLM-TARGET01"
PREMISE_ID = "CLM-PREMISE1"
PRIMARY_PROOF_ID = "PRF-PRIMARY1"
ALTERNATIVE_PROOF_ID = "PRF-ALTERN01"
PREMISE_PROOF_ID = "PRF-SUPPORT1"
PROOF_ATTEMPT_ID = "PAT-ATTEMPT1"
DERIVATION_ID = "DRV-DERIVED1"
NOW = datetime(2026, 8, 4, tzinfo=UTC)


def _claim(node_id: str, statement: str, status: EpistemicStatus) -> GraphNode:
    return GraphNode(
        matek_id=node_id,
        node_type=NodeType.CLAIM,
        problem_id=PROBLEM_ID,
        title=node_id,
        epistemic_status=status,
        claim_type=ClaimType.THEOREM,
        created_in_run="run-one",
        last_modified_run="run-one",
        created_at=NOW,
        updated_at=NOW,
        body=f"## Exact statement\n\n{statement}",
    )


def _proof(
    node_id: str,
    *,
    premise_ids: list[str],
    status: EpistemicStatus,
    conclusion_id: str = TARGET_ID,
    invalidation_reasons: list[str] | None = None,
) -> GraphNode:
    return GraphNode(
        matek_id=node_id,
        node_type=NodeType.PROOF,
        problem_id=PROBLEM_ID,
        title=node_id,
        epistemic_status=status,
        created_in_run="run-one",
        last_modified_run="run-one",
        created_at=NOW,
        updated_at=NOW,
        body="## Proof content\n\nA complete independently audited argument.",
        invalidation_reasons=invalidation_reasons or [],
        relations=[
            GraphEdge(source_id=node_id, relation=RelationType.PROVES, target_id=conclusion_id),
            *(
                GraphEdge(
                    source_id=node_id,
                    relation=RelationType.DEPENDS_ON,
                    target_id=premise_id,
                )
                for premise_id in premise_ids
            ),
        ],
    )


def _proof_attempt() -> GraphNode:
    return GraphNode(
        matek_id=PROOF_ATTEMPT_ID,
        node_type=NodeType.PROOF_ATTEMPT,
        problem_id=PROBLEM_ID,
        title=PROOF_ATTEMPT_ID,
        epistemic_status=EpistemicStatus.STALE,
        created_in_run="run-one",
        last_modified_run="run-one",
        created_at=NOW,
        updated_at=NOW,
        body="## Proof content\n\nThe edited argument is no longer audited.",
        invalidation_reasons=["proof_changed_requires_reaudit"],
        relations=[
            GraphEdge(
                source_id=PROOF_ATTEMPT_ID,
                relation=RelationType.RELATED_TO,
                target_id=TARGET_ID,
            )
        ],
    )


def _derivation() -> GraphNode:
    return GraphNode(
        matek_id=DERIVATION_ID,
        node_type=NodeType.DERIVATION,
        problem_id=PROBLEM_ID,
        title=DERIVATION_ID,
        epistemic_status=EpistemicStatus.AUDIT_PASSED,
        created_in_run="run-one",
        last_modified_run="run-one",
        created_at=NOW,
        updated_at=NOW,
        body="## Exact conclusion\n\nThe exact target holds.",
        relations=[
            GraphEdge(
                source_id=DERIVATION_ID,
                relation=RelationType.PROVES,
                target_id=TARGET_ID,
            ),
            GraphEdge(
                source_id=DERIVATION_ID,
                relation=RelationType.RELATED_TO,
                target_id=PROOF_ATTEMPT_ID,
            ),
        ],
        metadata={"matek_proof_attempt_id": PROOF_ATTEMPT_ID},
    )


@pytest.mark.parametrize(
    ("premise_status", "conclusion_is_preserved"),
    [
        (EpistemicStatus.OPEN, False),
        (EpistemicStatus.CANDIDATE, False),
        (EpistemicStatus.AUDIT_PASSED, True),
        (EpistemicStatus.LEAN_VERIFIED, True),
    ],
)
def test_alternative_audited_route_preserves_conclusion_only_with_trusted_premises(
    premise_status: EpistemicStatus,
    conclusion_is_preserved: bool,
) -> None:
    target = _claim(TARGET_ID, "The exact target holds.", EpistemicStatus.AUDIT_PASSED)
    premise = _claim(PREMISE_ID, "The required premise holds.", premise_status)
    primary = _proof(
        PRIMARY_PROOF_ID,
        premise_ids=[],
        status=EpistemicStatus.STALE,
        invalidation_reasons=["proof_changed_requires_reaudit"],
    )
    alternative = _proof(
        ALTERNATIVE_PROOF_ID,
        premise_ids=[PREMISE_ID],
        status=EpistemicStatus.AUDIT_PASSED,
    )
    nodes = {node.matek_id: node for node in [target, premise, primary, alternative]}

    changed = KnowledgeGraph._propagate_staleness(
        nodes,
        [PRIMARY_PROOF_ID],
        "proof_changed_requires_reaudit",
    )

    if conclusion_is_preserved:
        assert TARGET_ID not in changed
        assert target.epistemic_status is EpistemicStatus.AUDIT_PASSED
    else:
        assert TARGET_ID in changed
        assert target.epistemic_status is EpistemicStatus.STALE
        assert "proof_changed_requires_reaudit" in target.invalidation_reasons


def test_canonical_trust_closure_allows_an_audited_derived_premise() -> None:
    target = _claim(TARGET_ID, "The exact target holds.", EpistemicStatus.AUDIT_PASSED)
    premise = _claim(PREMISE_ID, "The required premise holds.", EpistemicStatus.CANDIDATE)
    primary = _proof(
        PRIMARY_PROOF_ID,
        premise_ids=[],
        status=EpistemicStatus.STALE,
        invalidation_reasons=["proof_changed_requires_reaudit"],
    )
    premise_support = _proof(
        PREMISE_PROOF_ID,
        premise_ids=[],
        status=EpistemicStatus.AUDIT_PASSED,
        conclusion_id=PREMISE_ID,
    )
    alternative = _proof(
        ALTERNATIVE_PROOF_ID,
        premise_ids=[PREMISE_ID],
        status=EpistemicStatus.AUDIT_PASSED,
    )
    nodes = {
        node.matek_id: node for node in [target, premise, primary, premise_support, alternative]
    }

    changed = KnowledgeGraph._propagate_staleness(
        nodes,
        [PRIMARY_PROOF_ID],
        "proof_changed_requires_reaudit",
    )

    assert TARGET_ID not in changed
    assert target.epistemic_status is EpistemicStatus.AUDIT_PASSED


def test_changed_proof_attempt_invalidates_its_derivation_and_conclusion() -> None:
    target = _claim(TARGET_ID, "The exact target holds.", EpistemicStatus.AUDIT_PASSED)
    attempt = _proof_attempt()
    derivation = _derivation()
    nodes = {node.matek_id: node for node in [target, attempt, derivation]}

    changed = KnowledgeGraph._propagate_staleness(
        nodes,
        [PROOF_ATTEMPT_ID],
        "proof_changed_requires_reaudit",
    )

    assert set(changed) == {DERIVATION_ID, TARGET_ID}
    assert derivation.epistemic_status is EpistemicStatus.STALE
    assert target.epistemic_status is EpistemicStatus.STALE
