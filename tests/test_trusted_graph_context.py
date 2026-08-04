from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from matek_theorem_agent.knowledge_graph import (
    ClaimType,
    EpistemicStatus,
    GraphNodeCreate,
    GraphPatch,
    KnowledgeGraph,
    NodeType,
)
from matek_theorem_agent.knowledge_graph import service as graph_service
from matek_theorem_agent.scientific import (
    BranchOutcome,
    ScientificResult,
    ScientificResultDisposition,
    ScientificResultKind,
    ScientificScope,
)
from matek_theorem_agent.stages.common import atomic_write_json
from matek_theorem_agent.stages.research import ResearchWorkerReport


class _AdvancingClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 4, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.value += timedelta(seconds=1)
        return self.value


def _context_node_ids(context: dict[str, object], key: str) -> list[str]:
    raw_nodes = context[key]
    assert isinstance(raw_nodes, list)
    return [str(item["node"]["matek_id"]) for item in raw_nodes]


def _context_selection(context: dict[str, object]) -> dict[str, object]:
    selection = context["selection"]
    assert isinstance(selection, dict)
    return selection


def _trusted_graph(tmp_path: Path) -> tuple[KnowledgeGraph, str, dict[str, str]]:
    project = tmp_path / "project"
    project.mkdir()
    problem = project / "problem.md"
    problem.write_text("Prove that every test object is good.\n", encoding="utf-8")
    graph = KnowledgeGraph(project, "problem", clock=_AdvancingClock())
    problem_id, _ = graph.initialize_problem(
        source_path=problem,
        problem_text=problem.read_text(encoding="utf-8"),
        run_id="run-one",
    )
    graph.record_compiled_problem(
        problem_id=problem_id,
        run_id="run-one",
        compiled_problem={
            "title": "Context theorem",
            "normalized_statement": "Every test object is good.",
            "claim_contract": {"conclusion": "Every test object is good."},
            "literature_status": "open_problem",
            "source_ledger": [
                {
                    "source_id": "verified-reference",
                    "title": "Verified Reference",
                    "authors": ["A. Author"],
                    "identifiers": ["doi:10.1000/matek-context"],
                    "verified": True,
                    "verification_detail": "Resolved against the DOI registry.",
                },
                {
                    "source_id": "archive-reference",
                    "title": "Unverified Archive Reference",
                    "authors": [],
                    "identifiers": [],
                    "verified": False,
                },
            ],
        },
    )
    target_id = graph.main_claim_id(problem_id)
    tasks, _, revision = graph.record_assignment_tasks(
        problem_id=problem_id,
        run_id="run-one",
        decision_id=1,
        assignments=[
            {
                "id": "worker-one",
                "approach_family": "direct",
                "task": "Develop the proof and its exact notation.",
                "expected_output": "A complete proof.",
                "target_node_ids": [target_id],
            }
        ],
    )
    fake_definition_id = "DEF-ARCHIVE01"
    informal_claim_id = "CLM-INFORMAL1"
    merged = graph.merge_patch(
        GraphPatch(
            base_graph_revision=revision,
            run_id="run-one",
            task_id=tasks["worker-one"],
            create_nodes=[
                GraphNodeCreate(
                    matek_id=fake_definition_id,
                    node_type=NodeType.DEFINITION,
                    title="Archive-only definition",
                    body="## Exact statement\n\nDefine ArchiveGood(x) to mean x is archived.",
                ),
                GraphNodeCreate(
                    matek_id=informal_claim_id,
                    node_type=NodeType.CLAIM,
                    claim_type=ClaimType.LEMMA,
                    title="Informally asserted claim",
                    body="## Exact statement\n\nEvery archived object is good.",
                    epistemic_status=EpistemicStatus.PROVED_INFORMALLY,
                ),
            ],
        ),
        problem_id=problem_id,
        operation_id="archive-context-fixture",
    )
    assert merged.committed

    definition = ScientificResult(
        local_key="good-notation",
        kind=ScientificResultKind.DEFINITION,
        exact_statement="Define Good(x) to mean that x has the desired property.",
        scope=ScientificScope.BRANCH,
        proof_or_certificate="This is the notation used by the accepted proof.",
        disposition=ScientificResultDisposition.PROPOSED_COMPLETE,
    )
    report = ResearchWorkerReport(
        assignment_id="worker-one",
        results=[definition],
        branch_outcome=BranchOutcome.PROGRESS,
        mechanism="Fix the notation before proving the main statement.",
    )
    report_artifact = ".matek/runs/run-one/research/workers/worker-one.json"
    atomic_write_json(project / report_artifact, report)
    admitted = graph.integrate_worker_report(
        problem_id=problem_id,
        run_id="run-one",
        assignment={
            "id": "worker-one",
            "approach_family": "direct",
            "task": "Develop the proof and its exact notation.",
            "target_node_ids": [target_id],
        },
        task_id=tasks["worker-one"],
        report=report.model_dump(mode="json"),
        proposed_patch=None,
        source_artifact=report_artifact,
        operation_id="trusted-definition-fixture",
    )
    assert admitted.committed
    accepted = graph.record_research_result(
        problem_id=problem_id,
        run_id="run-one",
        research_result={
            "outcome": "accepted",
            "acceptance_gate": {"accepted": True},
            "candidate": {
                "exact_theorem": "Every test object is good.",
                "full_proof": "Use the defining property directly.",
                "unresolved_items": [],
                "quantitative_or_algorithmic": False,
            },
            "audits": {},
        },
    )
    assert accepted.committed

    nodes = graph.load_nodes()
    ids = {
        "target": target_id,
        "proof": next(node.matek_id for node in nodes if node.title == "Accepted candidate proof"),
        "definition": next(
            node.matek_id for node in nodes if node.title == "Definition: good-notation"
        ),
        "verified_source": next(
            node.matek_id for node in nodes if node.title == "Verified Reference"
        ),
        "unverified_source": next(
            node.matek_id for node in nodes if node.title == "Unverified Archive Reference"
        ),
        "fake_definition": fake_definition_id,
        "informal_claim": informal_claim_id,
    }
    return graph, problem_id, ids


def test_downstream_contexts_share_canonical_trust_selection(tmp_path: Path) -> None:
    graph, problem_id, ids = _trusted_graph(tmp_path)

    manuscript = graph.manuscript_context(problem_id)
    manuscript_ids = set(_context_node_ids(manuscript, "accepted_nodes"))
    assert {
        ids["target"],
        ids["proof"],
        ids["definition"],
        ids["verified_source"],
    } <= manuscript_ids
    assert {
        ids["fake_definition"],
        ids["informal_claim"],
        ids["unverified_source"],
    }.isdisjoint(manuscript_ids)

    formalization = graph.formalization_context(problem_id)
    formalization_ids = set(_context_node_ids(formalization, "statement_nodes"))
    assert {ids["target"], ids["proof"], ids["definition"]} <= formalization_ids
    assert ids["verified_source"] not in formalization_ids
    assert _context_selection(manuscript)["policy"] == _context_selection(formalization)["policy"]


def test_trusted_context_reports_cap_and_keeps_main_proof_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph, problem_id, ids = _trusted_graph(tmp_path)
    monkeypatch.setattr(graph_service, "MANUSCRIPT_CONTEXT_MAXIMUM_NODES", 2)

    context = graph.manuscript_context(problem_id)

    assert _context_node_ids(context, "accepted_nodes") == [ids["target"], ids["proof"]]
    selection = _context_selection(context)
    assert selection["maximum_nodes"] == 2
    assert selection["included_node_count"] == 2
    eligible_count = selection["eligible_node_count"]
    included_count = selection["included_node_count"]
    assert isinstance(eligible_count, int)
    assert isinstance(included_count, int)
    assert eligible_count > 2
    assert selection["omitted_node_count"] == eligible_count - included_count
    assert selection["truncated"] is True


def test_formalization_context_admits_only_deterministically_verified_lean_node(
    tmp_path: Path,
) -> None:
    graph, problem_id, _ = _trusted_graph(tmp_path)
    statement_digest = "a" * 64
    common = {
        "approved_statement_hash": statement_digest,
        "statement_draft": {"theorem_name": "matek_main"},
        "alignment": {"status": "aligned"},
    }
    graph.record_lean_result(
        problem_id=problem_id,
        run_id="run-one",
        lean_result={
            **common,
            "outcome": "LEAN_FAILED",
            "verification": {
                "passed": False,
                "statement_hash_expected": statement_digest,
                "statement_hash_actual": statement_digest,
                "used_axioms": [],
            },
        },
        lean_toolchain="leanprover/lean4:v4.21.0",
        mathlib_revision="0123456789abcdef",
        source_file_hash="b" * 64,
        axiom_report_hash="c" * 64,
    )
    failed_context = graph.formalization_context(problem_id)
    assert all(
        not node_id.startswith("FRM-")
        for node_id in _context_node_ids(failed_context, "statement_nodes")
    )

    source = graph.project_root / "problem.md"
    graph.initialize_problem(
        source_path=source,
        problem_text=source.read_text(encoding="utf-8"),
        run_id="run-two",
    )
    graph.record_lean_result(
        problem_id=problem_id,
        run_id="run-two",
        lean_result={
            **common,
            "outcome": "LEAN_VERIFIED",
            "verification": {
                "passed": True,
                "statement_hash_expected": statement_digest,
                "statement_hash_actual": statement_digest,
                "used_axioms": [],
            },
        },
        lean_toolchain="leanprover/lean4:v4.21.0",
        mathlib_revision="0123456789abcdef",
        source_file_hash="b" * 64,
        axiom_report_hash="c" * 64,
    )
    verified_context = graph.formalization_context(problem_id)
    formalization_ids = [
        node_id
        for node_id in _context_node_ids(verified_context, "statement_nodes")
        if node_id.startswith("FRM-")
    ]
    assert len(formalization_ids) == 1
