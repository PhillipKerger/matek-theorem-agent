from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from matek_theorem_agent.knowledge_graph.admission import build_scientific_admission
from matek_theorem_agent.knowledge_graph.migration import plan_legacy_graph_backfill
from matek_theorem_agent.knowledge_graph.models import (
    GraphNode,
    NodeType,
    RelationType,
    WorkflowStatus,
)
from matek_theorem_agent.scientific import BranchOutcome
from matek_theorem_agent.stages.research import load_research_worker_report_json

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "legacy_worker_reports"
NOW = datetime(2026, 8, 3, tzinfo=UTC)


def _object_from_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def test_synthetic_derived_three_archive_compatibility_corpus() -> None:
    manifest = _object_from_json(FIXTURE_ROOT / "manifest.json")
    provenance = cast(dict[str, Any], manifest["provenance"])
    cases = cast(list[dict[str, Any]], manifest["cases"])

    assert provenance["classification"] == "synthetic-derived"
    assert provenance["actual_archive_replay"] is False
    assert provenance["original_archives_available"] is False
    assert {case["snapshot_reference"] for case in cases} == {
        "00000315-96a705c853a887d3",
        "00000285-5d876a716ce013ce",
        "00000369-846a4dd539f2cac3",
    }

    schema_rejections: list[str] = []
    normalized_reports = []
    for case in cases:
        worker_path = FIXTURE_ROOT / cast(str, case["worker_report"])
        try:
            report = load_research_worker_report_json(worker_path.read_text(encoding="utf-8"))
        except ValidationError as exc:
            schema_rejections.append(f"{case['slug']}: {exc}")
            continue
        normalized_reports.append((case, report))

    assert schema_rejections == []
    assert len(normalized_reports) == 3
    assert all(
        "graph_patch" not in report.model_dump(mode="json") for _, report in normalized_reports
    )

    for case, report in normalized_reports:
        graph_payload = _object_from_json(FIXTURE_ROOT / cast(str, case["graph_fixture"]))
        assert graph_payload["fixture_kind"] == "synthetic-derived-backfill"
        assert graph_payload["snapshot_reference"] == case["snapshot_reference"]
        nodes = [GraphNode.model_validate(item) for item in graph_payload["nodes"]]
        problem_id = cast(str, graph_payload["problem_id"])
        target_id = cast(str, graph_payload["main_target_id"])
        run_id = cast(str, graph_payload["run_id"])
        source_artifact = (
            f"tests/fixtures/legacy_worker_reports/{case['slug']}.worker-report-v1.json"
        )

        first = build_scientific_admission(
            existing_nodes=nodes,
            problem_id=problem_id,
            main_target_id=target_id,
            run_id=run_id,
            assignment_id=report.assignment_id,
            task_id=cast(str, graph_payload["task_id"]),
            approach_id=cast(str, graph_payload["approach_id"]),
            results=report.results,
            unresolved_obligations=report.unresolved_obligations,
            source_artifact=source_artifact,
            now=NOW,
        )

        retry = build_scientific_admission(
            existing_nodes=[*nodes, *first.nodes],
            problem_id=problem_id,
            main_target_id=target_id,
            run_id=run_id,
            assignment_id=report.assignment_id,
            task_id=cast(str, graph_payload["task_id"]),
            approach_id=cast(str, graph_payload["approach_id"]),
            results=report.results,
            unresolved_obligations=report.unresolved_obligations,
            source_artifact=source_artifact,
            now=NOW,
        )

        assert retry.nodes == []
        assert all(record.already_applied for record in retry.records)
        assert [record.admission_identity for record in retry.records] == [
            record.admission_identity for record in first.records
        ]
        assert [record.payload_sha256 for record in retry.records] == [
            record.payload_sha256 for record in first.records
        ]

        if report.branch_outcome is BranchOutcome.REFUTED:
            counterexamples = [
                node for node in first.nodes if node.node_type is NodeType.COUNTEREXAMPLE
            ]
            assert len(counterexamples) == 1
            assert not any(
                edge.relation is RelationType.REFUTES and edge.target_id == target_id
                for edge in counterexamples[0].relations
            )
            assert any(
                edge.relation is RelationType.REFUTES
                and edge.target_id == graph_payload["approach_id"]
                for edge in counterexamples[0].relations
            )
        else:
            proof_attempts = [
                node for node in first.nodes if node.node_type is NodeType.PROOF_ATTEMPT
            ]
            obligations = [node for node in first.nodes if node.node_type is NodeType.OBLIGATION]
            assert proof_attempts
            assert all(node.workflow_status is WorkflowStatus.BLOCKED for node in proof_attempts)
            assert obligations
            assert not any(node.node_type is NodeType.DERIVATION for node in first.nodes)
            assert all(not record.canonical_ledger_admitted for record in first.records)
            assert any(
                str(node.metadata.get("matek_obligation_local_key", "")).startswith(
                    "legacy-dependency-"
                )
                for node in obligations
            )

        archive_before = [node.model_dump(mode="json") for node in nodes]
        backfill = plan_legacy_graph_backfill(
            nodes,
            graph_revision=cast(str, case["snapshot_reference"]),
            problem_id=problem_id,
            target_claim_id=target_id,
        )
        reordered_backfill = plan_legacy_graph_backfill(
            list(reversed(nodes)),
            graph_revision=cast(str, case["snapshot_reference"]),
            problem_id=problem_id,
            target_claim_id=target_id,
        )
        expected = cast(dict[str, list[str]], graph_payload["expected_backfill"])

        assert backfill == reordered_backfill
        assert [node.model_dump(mode="json") for node in nodes] == archive_before
        assert [
            item.proof_node_id for item in backfill.proof_attempt_reclassifications
        ] == expected["proof_attempt_reclassifications"]
        assert [item.refutation_node_id for item in backfill.refutation_quarantines] == expected[
            "refutation_quarantines"
        ]
        for quarantine in backfill.refutation_quarantines:
            assert quarantine.archive_preserved is True
            assert quarantine.candidate_branch_target_ids == [graph_payload["approach_id"]]
