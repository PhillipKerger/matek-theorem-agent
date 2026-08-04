from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from matek_theorem_agent.knowledge_graph.ledger import (
    deterministic_ledger_id,
    load_canonical_ledger,
    project_markdown_ledger,
)
from matek_theorem_agent.knowledge_graph.migration import (
    LegacyMigrationError,
    legacy_archive_sha256,
    load_legacy_migration_application,
    load_legacy_migration_report,
    migration_report_sha256,
    plan_legacy_graph_backfill,
    write_legacy_migration_report,
)
from matek_theorem_agent.knowledge_graph.models import (
    ClaimType,
    EpistemicStatus,
    GraphEdge,
    GraphNode,
    NodeType,
    RelationType,
    WorkflowStatus,
)
from matek_theorem_agent.knowledge_graph.service import KnowledgeGraph
from matek_theorem_agent.scientific import ScientificScope

PROBLEM_ID = "PRB-AAAAAAAA"
TARGET_ID = "CLM-BBBBBBBB"
NOW = datetime(2026, 8, 3, tzinfo=UTC)


def _node(
    node_id: str,
    node_type: NodeType,
    *,
    body: str,
    relations: list[GraphEdge] | None = None,
    metadata: dict[str, str | int | bool | list[str] | None] | None = None,
    tags: list[str] | None = None,
    epistemic_status: EpistemicStatus = EpistemicStatus.OPEN,
    workflow_status: WorkflowStatus = WorkflowStatus.ACTIVE,
    source_artifacts: list[str] | None = None,
    evidence: list[str] | None = None,
    tombstone: bool = False,
    problem_id: str = PROBLEM_ID,
) -> GraphNode:
    return GraphNode(
        matek_id=node_id,
        node_type=node_type,
        problem_id=problem_id,
        title=node_id,
        claim_type=ClaimType.LEMMA if node_type is NodeType.CLAIM else None,
        created_in_run="run-legacy",
        last_modified_run="run-legacy",
        created_at=NOW,
        updated_at=NOW,
        body=body,
        relations=relations or [],
        metadata=metadata or {},
        tags=tags or [],
        epistemic_status=epistemic_status,
        workflow_status=workflow_status,
        source_artifacts=source_artifacts or [],
        evidence=evidence or [],
        tombstone=tombstone,
    )


def _target() -> GraphNode:
    return _node(
        TARGET_ID,
        NodeType.CLAIM,
        body="## Exact statement\n\nFor every admissible x, P(x).",
        tags=["matek/main-target"],
        epistemic_status=EpistemicStatus.CONJECTURED,
    )


def test_backfill_reclassifies_gaps_extracts_refs_and_nominates_intermediate() -> None:
    intermediate = _node(
        "CLM-CCCCCCCC",
        NodeType.CLAIM,
        body="## Exact statement\n\nFor every admissible x, Q(x).",
        epistemic_status=EpistemicStatus.CANDIDATE,
        evidence=["archived evidence"],
    )
    alias = _node(
        "CLM-DDDDDDDD",
        NodeType.CLAIM,
        body="## Exact statement\n\nFor   every admissible x, Q(x).",
        epistemic_status=EpistemicStatus.CANDIDATE,
    )
    premise = _node(
        "CLM-HHHHHHHH",
        NodeType.CLAIM,
        body="## Exact statement\n\nEvery minimal x has property R.",
    )
    approach = _node(
        "APR-FFFFFFFF",
        NodeType.APPROACH,
        body="## Exact route attempted\n\nA minimal-counterexample route.",
    )
    archived_support = _node(
        "PRF-GGGGGGGG",
        NodeType.PROOF,
        body="## Proof content\n\nEarlier supporting calculation.",
        tombstone=True,
    )
    proof = _node(
        "PRF-EEEEEEEE",
        NodeType.PROOF,
        body=(
            "## Proof content\n\nApply the minimal case and finish.\n\n"
            "## Exact gap\n\nNo gap declared; independent audit required."
        ),
        relations=[
            GraphEdge(
                source_id="PRF-EEEEEEEE",
                relation=RelationType.PROVES,
                target_id="CLM-DDDDDDDD",
            )
        ],
        metadata={
            "matek_dependencies": [
                "Use CLM-HHHHHHHH, archived PRF-GGGGGGGG, and route APR-FFFFFFFF."
            ]
        },
        epistemic_status=EpistemicStatus.CANDIDATE,
        workflow_status=WorkflowStatus.COMPLETE,
        source_artifacts=["research/workers/result.json"],
    )
    gapped = _node(
        "PRF-IIIIIIII",
        NodeType.PROOF,
        body=(
            "## Proof content\n\nThe induction starts.\n\n"
            "## Exact gap\n\nProve the boundary case without compactness."
        ),
        relations=[
            GraphEdge(
                source_id="PRF-IIIIIIII",
                relation=RelationType.PROVES,
                target_id="CLM-CCCCCCCC",
            )
        ],
    )
    nodes = [_target(), intermediate, alias, premise, approach, archived_support, proof, gapped]
    before = [node.model_dump(mode="json") for node in nodes]

    report = plan_legacy_graph_backfill(
        nodes,
        graph_revision="00000369-846a4dd539f2cac3",
        problem_id=PROBLEM_ID,
        target_claim_id=TARGET_ID,
    )

    assert [item.proof_node_id for item in report.proof_attempt_reclassifications] == [
        "PRF-IIIIIIII"
    ]
    extraction = next(
        item for item in report.dependency_extractions if item.source_node_id == "PRF-EEEEEEEE"
    )
    assert extraction.claim_ids == ["CLM-HHHHHHHH"]
    assert extraction.proof_ids == ["PRF-GGGGGGGG"]
    assert extraction.approach_ids == ["APR-FFFFFFFF"]
    assert extraction.review_blockers == []
    proposal = report.derivation_proposals[0]
    assert proposal.derivation.conclusion_claim_id == "CLM-CCCCCCCC"
    assert proposal.derivation.premise_claim_ids == ["CLM-HHHHHHHH"]
    assert proposal.supporting_archive_node_ids == ["APR-FFFFFFFF", "PRF-GGGGGGGG"]
    assert proposal.disposition == "review_required"
    assert report.claim_alias_groups[0].alias_ids == ["CLM-DDDDDDDD"]
    assert report.claim_alias_groups[0].disposition == "ready_for_review"
    assert report.audit_nominations[0].claim_id == "CLM-CCCCCCCC"
    assert report.audit_nominations[0].independent_lanes == ["verifier", "falsifier"]
    assert report.source_edits_applied is False
    assert [node.model_dump(mode="json") for node in nodes] == before


def test_ambiguous_dependencies_and_scope_conflicts_are_reported_not_guessed() -> None:
    target_alias = _node(
        "CLM-CCCCCCCC",
        NodeType.CLAIM,
        body="## Exact statement\n\nFor every admissible x, P(x).",
        metadata={"matek_scientific_scope": "branch"},
    )
    other = _node(
        "CLM-DDDDDDDD",
        NodeType.CLAIM,
        body="## Exact statement\n\nA reusable exact lemma.",
    )
    proof = _node(
        "PRF-EEEEEEEE",
        NodeType.PROOF,
        body="## Exact gap\n\nNo gap declared.",
        relations=[
            GraphEdge(
                source_id="PRF-EEEEEEEE",
                relation=RelationType.PROVES,
                target_id="CLM-DDDDDDDD",
            )
        ],
        metadata={
            "matek_dependencies": [
                "an unnamed compactness fact",
                "either CLM-CCCCCCCC or CLM-DDDDDDDD",
                "unknown CLM-ZZZZZZZZ",
            ]
        },
    )

    report = plan_legacy_graph_backfill(
        [_target(), target_alias, other, proof],
        graph_revision="revision-one",
        problem_id=PROBLEM_ID,
        target_claim_id=TARGET_ID,
    )

    group = report.claim_alias_groups[0]
    assert group.canonical_candidate_id == TARGET_ID
    assert group.alias_ids == ["CLM-CCCCCCCC"]
    assert group.scopes == [ScientificScope.BRANCH, ScientificScope.MAIN]
    assert group.disposition == "scope_conflict"
    assert {item.code for item in report.scope_conflicts} >= {"exact_claim_scope_conflict"}
    assert {item.code for item in report.ambiguous_dependencies} >= {
        "unresolved_free_text_dependency",
        "disjunctive_dependency",
        "unknown_dependency_reference",
    }
    assert report.derivation_proposals == []


def test_mechanism_only_main_refutation_is_quarantined_without_rewriting_edge() -> None:
    approach = _node(
        "APR-CCCCCCCC",
        NodeType.APPROACH,
        body="## Exact route attempted\n\nTry the stronger invariant.",
    )
    mechanism_failure = _node(
        "CEX-DDDDDDDD",
        NodeType.COUNTEREXAMPLE,
        body=(
            "## Scope\n\nThis refutes only the proposed strengthening mechanism; "
            "it does not refute the main theorem."
        ),
        relations=[
            GraphEdge(
                source_id="CEX-DDDDDDDD",
                relation=RelationType.REFUTES,
                target_id=TARGET_ID,
            ),
            GraphEdge(
                source_id="CEX-DDDDDDDD",
                relation=RelationType.RELATED_TO,
                target_id="APR-CCCCCCCC",
            ),
        ],
    )
    theorem_counterexample = _node(
        "CEX-EEEEEEEE",
        NodeType.COUNTEREXAMPLE,
        body="## Explicit instance\n\nx satisfies every hypothesis and violates P(x).",
        relations=[
            GraphEdge(
                source_id="CEX-EEEEEEEE",
                relation=RelationType.REFUTES,
                target_id=TARGET_ID,
            )
        ],
    )

    report = plan_legacy_graph_backfill(
        [_target(), approach, mechanism_failure, theorem_counterexample],
        graph_revision="revision-two",
        problem_id=PROBLEM_ID,
        target_claim_id=TARGET_ID,
    )

    assert [item.refutation_node_id for item in report.refutation_quarantines] == ["CEX-DDDDDDDD"]
    quarantine = report.refutation_quarantines[0]
    assert quarantine.candidate_branch_target_ids == ["APR-CCCCCCCC"]
    assert quarantine.archive_preserved is True
    assert mechanism_failure.relations[0].relation is RelationType.REFUTES
    assert mechanism_failure.relations[0].target_id == TARGET_ID


def test_audit_nominations_are_ranked_bounded_and_exclude_trusted_claims() -> None:
    candidate = _node(
        "CLM-CCCCCCCC",
        NodeType.CLAIM,
        body="## Exact statement\n\nCandidate lemma.",
        epistemic_status=EpistemicStatus.CANDIDATE,
    )
    trusted = _node(
        "CLM-DDDDDDDD",
        NodeType.CLAIM,
        body="## Exact statement\n\nAlready audited lemma.",
        epistemic_status=EpistemicStatus.AUDIT_PASSED,
    )
    candidate_proof = _node(
        "PRF-EEEEEEEE",
        NodeType.PROOF,
        body="## Proof content\n\nComplete.\n\n## Exact gap\n\nNone declared.",
        relations=[
            GraphEdge(
                source_id="PRF-EEEEEEEE",
                relation=RelationType.PROVES,
                target_id="CLM-CCCCCCCC",
            )
        ],
        workflow_status=WorkflowStatus.COMPLETE,
    )
    trusted_proof = _node(
        "PRF-FFFFFFFF",
        NodeType.PROOF,
        body="## Proof content\n\nComplete.\n\n## Exact gap\n\nNone declared.",
        relations=[
            GraphEdge(
                source_id="PRF-FFFFFFFF",
                relation=RelationType.PROVES,
                target_id="CLM-DDDDDDDD",
            )
        ],
        workflow_status=WorkflowStatus.COMPLETE,
    )

    report = plan_legacy_graph_backfill(
        [_target(), candidate, trusted, candidate_proof, trusted_proof],
        graph_revision="revision-three",
        problem_id=PROBLEM_ID,
        target_claim_id=TARGET_ID,
        audit_nomination_limit=1,
    )

    assert [item.claim_id for item in report.audit_nominations] == ["CLM-CCCCCCCC"]
    assert report.audit_nominations[0].strength_score > 0
    assert len(report.derivation_proposals) == 2


def test_report_is_order_independent_and_integrity_protected(tmp_path: Path) -> None:
    claim = _node(
        "CLM-CCCCCCCC",
        NodeType.CLAIM,
        body="## Exact statement\n\nReusable lemma.",
    )
    nodes = [_target(), claim]
    first = plan_legacy_graph_backfill(
        nodes,
        graph_revision="revision-four",
        problem_id=PROBLEM_ID,
        target_claim_id=TARGET_ID,
    )
    second = plan_legacy_graph_backfill(
        list(reversed(nodes)),
        graph_revision="revision-four",
        problem_id=PROBLEM_ID,
        target_claim_id=TARGET_ID,
    )

    assert first == second
    assert migration_report_sha256(first) == migration_report_sha256(second)
    path = write_legacy_migration_report(tmp_path / "legacy-migration.json", first)
    assert load_legacy_migration_report(path) == first

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["source_graph_revision"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(LegacyMigrationError, match="integrity digest"):
        load_legacy_migration_report(path)


def test_planner_rejects_an_archive_without_the_main_target() -> None:
    with pytest.raises(LegacyMigrationError, match="main target"):
        plan_legacy_graph_backfill(
            [
                _node(
                    "CLM-CCCCCCCC",
                    NodeType.CLAIM,
                    body="## Exact statement\n\nSome lemma.",
                )
            ],
            graph_revision="revision-five",
            problem_id=PROBLEM_ID,
            target_claim_id=TARGET_ID,
        )


def _service_graph_with_legacy_evidence(
    tmp_path: Path,
) -> tuple[KnowledgeGraph, str, str, str]:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    source = project / "legacy.md"
    source.write_text("Prove the migration test theorem.\n", encoding="utf-8")
    graph = KnowledgeGraph(project, "reviewed-legacy")
    problem_id, _ = graph.initialize_problem(
        source_path=source,
        problem_text=source.read_text(encoding="utf-8"),
        run_id="run-legacy",
    )
    graph.record_compiled_problem(
        problem_id=problem_id,
        run_id="run-legacy",
        compiled_problem={
            "title": "Migration test theorem",
            "normalized_statement": "For every admissible x, P(x).",
            "claim_contract": {"target": "P"},
            "literature_status": "unknown",
            "source_ledger": [],
        },
    )
    target_id = graph.main_claim_id(problem_id)
    branch = _node(
        "CLM-CCCCCCCC",
        NodeType.CLAIM,
        body="## Exact statement\n\nFor every admissible x, Q(x).",
        epistemic_status=EpistemicStatus.CANDIDATE,
        problem_id=problem_id,
    )
    alias = _node(
        "CLM-DDDDDDDD",
        NodeType.CLAIM,
        body="## Exact statement\n\nFor   every admissible x, Q(x).",
        epistemic_status=EpistemicStatus.CANDIDATE,
        problem_id=problem_id,
    )
    premise = _node(
        "CLM-HHHHHHHH",
        NodeType.CLAIM,
        body="## Exact statement\n\nEvery minimal x has property R.",
        problem_id=problem_id,
    )
    approach = _node(
        "APR-FFFFFFFF",
        NodeType.APPROACH,
        body="## Exact route attempted\n\nUse a minimal counterexample.",
        problem_id=problem_id,
    )
    proof = _node(
        "PRF-EEEEEEEE",
        NodeType.PROOF,
        body="## Proof content\n\nApply the minimal case.\n\n## Exact gap\n\nNone declared.",
        relations=[
            GraphEdge(
                source_id="PRF-EEEEEEEE",
                relation=RelationType.PROVES,
                target_id=alias.matek_id,
            )
        ],
        metadata={"matek_dependencies": [premise.matek_id, approach.matek_id]},
        epistemic_status=EpistemicStatus.CANDIDATE,
        workflow_status=WorkflowStatus.COMPLETE,
        problem_id=problem_id,
    )
    ambiguous = _node(
        "PRF-JJJJJJJJ",
        NodeType.PROOF,
        body="## Proof content\n\nInvoke an unnamed fact.\n\n## Exact gap\n\nNone declared.",
        relations=[
            GraphEdge(
                source_id="PRF-JJJJJJJJ",
                relation=RelationType.PROVES,
                target_id=branch.matek_id,
            )
        ],
        metadata={"matek_dependencies": ["an unnamed compactness fact"]},
        problem_id=problem_id,
    )
    counterexample = _node(
        "CEX-KKKKKKKK",
        NodeType.COUNTEREXAMPLE,
        body=(
            "## Scope\n\nThis refutes only the strengthening mechanism; "
            "it does not refute the main theorem."
        ),
        relations=[
            GraphEdge(
                source_id="CEX-KKKKKKKK",
                relation=RelationType.REFUTES,
                target_id=target_id,
            ),
            GraphEdge(
                source_id="CEX-KKKKKKKK",
                relation=RelationType.RELATED_TO,
                target_id=approach.matek_id,
            ),
        ],
        problem_id=problem_id,
    )
    additions = [branch, alias, premise, approach, proof, ambiguous, counterexample]
    with graph._locked():
        state = graph._load_state_unlocked()
        nodes = graph._load_nodes_unlocked(include_human_notes=True)
        graph._commit_nodes_unlocked(
            state=state,
            all_nodes=[*nodes, *additions],
            changed_node_ids=[item.matek_id for item in additions],
            run_id="run-legacy",
            author="legacy-fixture",
            reason="Seed reviewed legacy migration evidence.",
            operation_id="seed-reviewed-legacy-evidence",
        )
    return graph, problem_id, target_id, proof.matek_id


def test_reviewed_plan_apply_is_transactional_archival_and_retry_idempotent(
    tmp_path: Path,
) -> None:
    graph, problem_id, target_id, legacy_proof_id = _service_graph_with_legacy_evidence(tmp_path)
    source_state = graph.load_state()
    source_snapshot = graph.reconstruct_snapshot(source_state.revision)
    source_nodes = graph.load_nodes()
    legacy_body = next(item for item in source_nodes if item.matek_id == legacy_proof_id).body
    ambiguous_body = next(item for item in source_nodes if item.matek_id == "PRF-JJJJJJJJ").body
    report = plan_legacy_graph_backfill(
        source_nodes,
        graph_revision=source_state.revision,
        problem_id=problem_id,
        target_claim_id=target_id,
        graph_name=graph.graph_name,
    )

    record = graph.apply_legacy_migration(report)

    assert record.status == "applied"
    assert record.previous_revision == source_state.revision
    assert record.new_revision != source_state.revision
    assert {item.code for item in record.unapplied_issues} >= {"unresolved_free_text_dependency"}
    current = {item.matek_id: item for item in graph.load_nodes()}
    archived = current[legacy_proof_id]
    assert archived.body == legacy_body
    assert archived.metadata["matek_archive_only"] is True
    assert archived.workflow_status is WorkflowStatus.SUPERSEDED
    assert current["PRF-JJJJJJJJ"].body == ambiguous_body
    assert "matek_archive_only" not in current["PRF-JJJJJJJJ"].metadata
    assert len(record.proof_attempt_node_ids) == 1
    attempt_id = record.proof_attempt_node_ids[0]
    assert current[attempt_id].node_type is NodeType.PROOF_ATTEMPT
    assert archived.metadata["matek_superseded_by"] == attempt_id
    derivation = current[record.derivation_node_ids[0]]
    assert derivation.metadata["matek_proof_attempt_id"] == attempt_id
    assert derivation.metadata["matek_premise_versions"]
    assert current["CLM-DDDDDDDD"].metadata["matek_alias_of"] == "CLM-CCCCCCCC"
    quarantine = current["CEX-KKKKKKKK"]
    assert not any(
        edge.relation is RelationType.REFUTES and edge.target_id == target_id
        for edge in quarantine.relations
    )
    assert any(
        edge.relation is RelationType.REFUTES and edge.target_id == "APR-FFFFFFFF"
        for edge in quarantine.relations
    )
    task = current[record.audit_task_node_ids[0]]
    assert task.node_type is NodeType.TASK
    assert task.workflow_status is WorkflowStatus.QUEUED
    assert task.metadata["matek_audit_lanes"] == ["verifier", "falsifier"]
    ledger = load_canonical_ledger(graph.ledgers_root / problem_id / "canonical-ledger.json")
    projected = ledger.derivations[record.derivation_node_ids[0]]
    assert projected.proof_attempt_id == attempt_id
    assert all(ambiguity.source_node_id != legacy_proof_id for ambiguity in ledger.ambiguities)
    ambiguous_derivation_id = deterministic_ledger_id("DRV", problem_id, "PRF-JJJJJJJJ")
    assert ambiguous_derivation_id not in ledger.derivations
    assert any(
        ambiguity.source_node_id == "PRF-JJJJJJJJ" and ambiguity.code == "unadmitted_archive_proof"
        for ambiguity in ledger.ambiguities
    )

    missing_pat_nodes = graph.load_nodes()
    structured = next(
        item for item in missing_pat_nodes if item.matek_id == record.derivation_node_ids[0]
    )
    structured.metadata.pop("matek_proof_attempt_id")
    missing_pat_ledger = project_markdown_ledger(
        missing_pat_nodes,
        graph_revision=record.new_revision,
        problem_id=problem_id,
        target_claim_id=target_id,
    )
    assert structured.matek_id not in missing_pat_ledger.derivations
    assert any(
        ambiguity.source_node_id == structured.matek_id
        and ambiguity.code == "missing_proof_attempt_id"
        for ambiguity in missing_pat_ledger.ambiguities
    )
    assert graph.reconstruct_snapshot(source_state.revision) == source_snapshot
    assert legacy_archive_sha256(source_nodes, problem_id=problem_id) == (
        report.source_archive_sha256
    )

    record_path = (
        graph.ledgers_root / "migrations" / f"{migration_report_sha256(report)}.application.json"
    )
    assert load_legacy_migration_application(record_path) == record
    retry = graph.apply_legacy_migration(report)
    assert retry.status == "already_applied"
    assert retry.new_revision == record.new_revision
    assert graph.load_state().revision == record.new_revision


def test_apply_rejects_stale_archive_digest_and_wrong_graph(tmp_path: Path) -> None:
    graph, problem_id, target_id, _ = _service_graph_with_legacy_evidence(tmp_path)
    state = graph.load_state()
    report = plan_legacy_graph_backfill(
        graph.load_nodes(),
        graph_revision=state.revision,
        problem_id=problem_id,
        target_claim_id=target_id,
        graph_name=graph.graph_name,
    )
    altered = report.model_copy(update={"derivation_proposals": []})
    with pytest.raises(LegacyMigrationError, match="deterministic plan"):
        graph.apply_legacy_migration(altered)
    assert graph.load_state().revision == state.revision

    target = next(item for item in graph.load_nodes() if item.matek_id == target_id)
    assert target.path is not None
    target_path = graph.vault_root / target.path
    target_path.write_text(
        target_path.read_text(encoding="utf-8") + "\nHuman review annotation.\n",
        encoding="utf-8",
    )
    with pytest.raises(LegacyMigrationError, match="archive digest changed"):
        graph.apply_legacy_migration(report)

    other_project = tmp_path / "other"
    other_project.mkdir()
    (other_project / ".git").mkdir()
    other = KnowledgeGraph(other_project, "other-graph")
    other.initialize()
    with pytest.raises(LegacyMigrationError, match="belongs to graph"):
        other.apply_legacy_migration(report)


def test_apply_rejects_a_stale_optimistic_graph_revision(tmp_path: Path) -> None:
    graph, problem_id, target_id, _ = _service_graph_with_legacy_evidence(tmp_path)
    state = graph.load_state()
    report = plan_legacy_graph_backfill(
        graph.load_nodes(),
        graph_revision=state.revision,
        problem_id=problem_id,
        target_claim_id=target_id,
        graph_name=graph.graph_name,
    )
    second_source = graph.project_root / "second.md"
    second_source.write_text("A second archived problem.\n", encoding="utf-8")
    graph.initialize_problem(
        source_path=second_source,
        problem_text=second_source.read_text(encoding="utf-8"),
        run_id="run-after-review",
    )

    with pytest.raises(LegacyMigrationError, match="current graph revision"):
        graph.apply_legacy_migration(report)
