from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from matek_theorem_agent.cli import app
from matek_theorem_agent.config import ModelSettings
from matek_theorem_agent.execution.base import CommandRequest, CommandResult
from matek_theorem_agent.knowledge_graph import (
    ClaimType,
    EpistemicStatus,
    GraphConflictError,
    GraphEdge,
    GraphNodeCreate,
    GraphNodeUpdate,
    GraphPatch,
    GraphStatusChange,
    GraphValidationError,
    KnowledgeGraph,
    NodeType,
    RelationType,
    WorkflowStatus,
    list_graph_names,
    problem_graph_name,
)
from matek_theorem_agent.knowledge_graph.admission import (
    admission_identity,
    admission_payload_sha256,
    encode_admission_binding,
)
from matek_theorem_agent.knowledge_graph.ledger import logical_version, project_markdown_ledger
from matek_theorem_agent.knowledge_graph.markdown import (
    exact_statement,
    new_generated_body,
    render_node_note,
)
from matek_theorem_agent.openai_client import ModelRequest, ModelResult
from matek_theorem_agent.scientific import (
    BranchOutcome,
    ScientificArtifactDeclaration,
    ScientificObligationDeclaration,
    ScientificResult,
    ScientificResultDisposition,
    ScientificResultKind,
    ScientificScope,
)
from matek_theorem_agent.stages.common import StageValidationError, atomic_write_json, sha256_file
from matek_theorem_agent.stages.computation_artifacts import (
    ComputationArtifactStore,
    ComputationReplayIsolation,
    WorkerComputationEvidence,
)
from matek_theorem_agent.stages.counterexample_audit import (
    CounterexampleAuditDecision,
    CounterexampleAuditResponse,
    CounterexampleAuditRole,
    CounterexampleGraphReadSnapshot,
    build_counterexample_support_bundle,
    build_exact_counterexample_nomination,
    run_counterexample_audit,
)
from matek_theorem_agent.stages.lemma_audit import (
    IntermediateResultKind,
    LemmaAuditDecision,
    LemmaAuditResponse,
    LemmaAuditRole,
    LemmaLeverage,
    LemmaNomination,
    LemmaProofStep,
    LemmaScope,
    LemmaSourceArtifact,
    LemmaTargetObligationReference,
    run_lemma_audit,
)
from matek_theorem_agent.stages.research import ResearchWorkerReport


class AdvancingClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 21, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.value += timedelta(seconds=1)
        return self.value


def initialized_graph(tmp_path: Path) -> tuple[KnowledgeGraph, Path, str, str]:
    project = tmp_path / "project"
    project.mkdir()
    problem = project / "problem.md"
    problem.write_text("Prove that every test object has the desired property.\n", encoding="utf-8")
    graph = KnowledgeGraph(project, "problem", clock=AdvancingClock())
    problem_id, first_revision = graph.initialize_problem(
        source_path=problem,
        problem_text=problem.read_text(encoding="utf-8"),
        run_id="run-one",
    )
    graph.record_compiled_problem(
        problem_id=problem_id,
        run_id="run-one",
        compiled_problem={
            "title": "Test theorem",
            "normalized_statement": "For every test object, the desired property holds.",
            "claim_contract": {"target": "the desired property"},
            "literature_status": "open_problem",
            "source_ledger": [],
        },
    )
    return graph, problem, problem_id, first_revision


def graph_task(graph: KnowledgeGraph, problem_id: str) -> tuple[str, str]:
    tasks, contexts, revision = graph.record_assignment_tasks(
        problem_id=problem_id,
        run_id="run-one",
        decision_id=1,
        assignments=[
            {
                "id": "worker-one",
                "approach_family": "induction",
                "task": "Prove a useful intermediate lemma.",
                "expected_output": "An exact lemma and proof.",
                "target_node_ids": [graph.main_claim_id(problem_id)],
            }
        ],
    )
    assert contexts["worker-one"].nodes
    return tasks["worker-one"], revision


class ExactCounterexampleGraphAuditClient:
    async def generate_structured(
        self,
        request: ModelRequest,
        output_type: type[CounterexampleAuditResponse],
    ) -> ModelResult[CounterexampleAuditResponse]:
        assert output_type is CounterexampleAuditResponse
        payload = json.loads(request.input_text)
        role = CounterexampleAuditRole(payload["audit_role"])
        packet = payload["exact_counterexample_packet"]
        return ModelResult(
            parsed=CounterexampleAuditResponse(
                audit_role=role,
                audit_id=packet["audit_id"],
                target_statement_sha256=packet["target_statement_sha256"],
                decision=CounterexampleAuditDecision.PASS,
                statement_aligned=True,
                every_hypothesis_satisfied=True,
                claimed_failure_demonstrated=True,
                certificate_valid=True,
                witness_or_instance="n = 0",
                hypothesis_check="0 is an integer.",
                conclusion_evaluation="0 + 1 = 1, and 1 is not equal to 0.",
                checks_performed=["Recomputed the exact quantified instance."],
                hostile_or_boundary_tests=(
                    ["Attacked domain membership and the boundary calculation."]
                    if role is CounterexampleAuditRole.FALSIFIER
                    else []
                ),
                rationale="The complete exact-target certificate checks independently.",
            ),
            response_id=f"graph-{role.value}",
        )


@pytest.mark.asyncio
async def test_only_persisted_exact_counterexample_gate_adds_refutes_main_edge(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "false.md"
    source.write_text("For every integer n, prove n + 1 = n.\n", encoding="utf-8")
    graph = KnowledgeGraph(project, "false", clock=AdvancingClock())
    problem_id, _ = graph.initialize_problem(
        source_path=source,
        problem_text=source.read_text(encoding="utf-8"),
        run_id="run-one",
    )
    target_statement = "For every integer n, n + 1 = n."
    graph.record_compiled_problem(
        problem_id=problem_id,
        run_id="run-one",
        compiled_problem={
            "title": "False integer identity",
            "normalized_statement": target_statement,
            "claim_contract": {
                "quantifiers": "for every integer n",
                "conclusion": "n + 1 = n",
            },
            "literature_status": "open_problem",
            "source_ledger": [],
        },
    )
    target_id = graph.main_claim_id(problem_id)
    tasks, _, _ = graph.record_assignment_tasks(
        problem_id=problem_id,
        run_id="run-one",
        decision_id=1,
        assignments=[
            {
                "id": "worker-one",
                "approach_family": "counterexample",
                "task": "Find an exact counterexample.",
                "expected_output": "A complete instance.",
                "target_node_ids": [target_id],
            }
        ],
    )
    supporting_definition = ScientificResult(
        local_key="integer-domain",
        kind=ScientificResultKind.DEFINITION,
        exact_statement="Define IntDomain(n) to mean that n is an integer.",
        scope=ScientificScope.BRANCH,
        proof_or_certificate="This fixes the witness domain used by the certificate.",
        disposition=ScientificResultDisposition.PROPOSED_COMPLETE,
    )
    result = ScientificResult(
        local_key="exact-main-counterexample",
        kind=ScientificResultKind.COUNTEREXAMPLE,
        exact_statement=target_statement,
        scope=ScientificScope.MAIN,
        proof_or_certificate=("Take n = 0. It is an integer; 0 + 1 = 1, and 1 is not equal to 0."),
        dependency_result_keys=[supporting_definition.local_key],
        target_node_ids=[target_id],
        disposition=ScientificResultDisposition.REFUTED_MECHANISM,
    )
    report = ResearchWorkerReport(
        assignment_id="worker-one",
        results=[supporting_definition, result],
        branch_outcome=BranchOutcome.REFUTED,
        mechanism="The boundary integer n = 0.",
    )
    research_root = project / ".matek" / "runs" / "run-one" / "research"
    report_path = atomic_write_json(research_root / "workers" / "worker-one.json", report)
    graph.integrate_worker_report(
        problem_id=problem_id,
        run_id="run-one",
        assignment={
            "id": "worker-one",
            "approach_family": "counterexample",
            "task": "Find an exact counterexample.",
        },
        task_id=tasks["worker-one"],
        report=report.model_dump(mode="json"),
        proposed_patch=None,
        source_artifact=".matek/runs/run-one/research/workers/worker-one.json",
        operation_id="typed-counterexample:run-one:worker-one",
    )
    counterexample = next(
        node for node in graph.load_nodes() if node.node_type is NodeType.COUNTEREXAMPLE
    )
    assert not any(
        edge.relation is RelationType.REFUTES and edge.target_id == target_id
        for edge in counterexample.relations
    )

    tampered_nodes = graph.load_nodes(include_human_notes=False)
    tampered_counterexample = next(
        node for node in tampered_nodes if node.node_type is NodeType.COUNTEREXAMPLE
    )
    assert tampered_counterexample.dependency_versions
    tampered_counterexample.dependency_versions = [
        tampered_counterexample.dependency_versions[0].split("@", maxsplit=1)[0] + "@" + "0" * 64
    ]
    with pytest.raises(StageValidationError, match="changed dependency versions"):
        build_counterexample_support_bundle(
            assignment_id="worker-one",
            root_result=result,
            results=report.results,
            knowledge_graph=CounterexampleGraphReadSnapshot(
                graph_name=graph.graph_name,
                state=graph.load_state(),
                nodes=tuple(tampered_nodes),
                main_target_id=target_id,
            ),
            graph_problem_id=problem_id,
            run_id="run-one",
        )

    invalid_type_nodes = graph.load_nodes(include_human_notes=False)
    invalid_counterexample = next(
        node for node in invalid_type_nodes if node.node_type is NodeType.COUNTEREXAMPLE
    )
    definition_node = next(
        node for node in invalid_type_nodes if node.node_type is NodeType.DEFINITION
    )
    task_node = next(node for node in invalid_type_nodes if node.matek_id == tasks["worker-one"])
    invalid_type_result = result.model_copy(update={"dependency_node_ids": [task_node.matek_id]})
    invalid_payload = admission_payload_sha256(invalid_type_result)
    invalid_counterexample.metadata["matek_admission_payload_sha256"] = invalid_payload
    invalid_counterexample.metadata["matek_admission_bindings"] = [
        encode_admission_binding(
            admission_identity(
                "run-one",
                "worker-one",
                invalid_type_result.local_key,
                invalid_type_result.schema_version,
            ),
            invalid_payload,
        ),
    ]
    invalid_counterexample.relations = [
        edge
        for edge in invalid_counterexample.relations
        if edge.relation is not RelationType.DEPENDS_ON
    ] + [
        GraphEdge(
            source_id=invalid_counterexample.matek_id,
            relation=RelationType.DEPENDS_ON,
            target_id=dependency_id,
        )
        for dependency_id in [task_node.matek_id, definition_node.matek_id]
    ]
    invalid_counterexample.dependency_versions = [
        f"{node.matek_id}@{logical_version(exact_statement(node.body))}"
        for node in [task_node, definition_node]
    ]
    with pytest.raises(StageValidationError, match="canonical mathematical claim"):
        build_counterexample_support_bundle(
            assignment_id="worker-one",
            root_result=invalid_type_result,
            results=[supporting_definition, invalid_type_result],
            knowledge_graph=CounterexampleGraphReadSnapshot(
                graph_name=graph.graph_name,
                state=graph.load_state(),
                nodes=tuple(invalid_type_nodes),
                main_target_id=target_id,
            ),
            graph_problem_id=problem_id,
            run_id="run-one",
        )

    obligation_nodes = graph.load_nodes(include_human_notes=False)
    obligation_counterexample = next(
        node for node in obligation_nodes if node.node_type is NodeType.COUNTEREXAMPLE
    )
    edge_obligation = task_node.model_copy(
        deep=True,
        update={
            "matek_id": "OBL-EDGEONLY1",
            "node_type": NodeType.OBLIGATION,
            "title": "Edge-only support obligation",
            "epistemic_status": EpistemicStatus.OPEN,
            "workflow_status": WorkflowStatus.BLOCKED,
            "body": new_generated_body(
                "Edge-only support obligation",
                "## Exact statement\n\nRecheck the counterexample support.\n\n"
                "## Conclusion\n\nThe support is valid.",
            ),
            "relations": [
                GraphEdge(
                    source_id="OBL-EDGEONLY1",
                    relation=RelationType.BLOCKS,
                    target_id=obligation_counterexample.matek_id,
                )
            ],
            "metadata": {
                "matek_parent_derivation_ids": [],
                "matek_dependency_claim_ids": [],
                "matek_target_claim_ids": [],
                "matek_conclusion": "The support is valid.",
                "matek_notation_definition_version": "1",
            },
            "content_hash": None,
        },
    )
    obligation_nodes.append(edge_obligation)
    with pytest.raises(StageValidationError, match="unresolved graph obligation"):
        build_counterexample_support_bundle(
            assignment_id="worker-one",
            root_result=result,
            results=report.results,
            knowledge_graph=CounterexampleGraphReadSnapshot(
                graph_name=graph.graph_name,
                state=graph.load_state(),
                nodes=tuple(obligation_nodes),
                main_target_id=target_id,
            ),
            graph_problem_id=problem_id,
            run_id="run-one",
        )

    support_bundle = build_counterexample_support_bundle(
        assignment_id="worker-one",
        root_result=result,
        results=report.results,
        knowledge_graph=graph,
        graph_problem_id=problem_id,
        run_id="run-one",
    )
    assert support_bundle.graph is not None
    nomination = build_exact_counterexample_nomination(
        assignment_id="worker-one",
        result=result,
        frozen_target_statement=target_statement,
        worker_report_path="workers/worker-one.json",
        worker_report_sha256=sha256_file(report_path),
        main_target_node_id=target_id,
        support_bundle=support_bundle,
    )
    audit_dir = research_root / "counterexample-audits" / nomination.audit_id
    client = ExactCounterexampleGraphAuditClient()
    gate = await run_counterexample_audit(
        nomination,
        audit_dir,
        verifier_client=client,
        falsifier_client=client,
        settings=ModelSettings(web_search=False),
    )
    live_counterexample = next(
        node for node in graph.load_nodes() if node.node_type is NodeType.COUNTEREXAMPLE
    )
    assert live_counterexample.path is not None
    counterexample_path = graph.vault_root / live_counterexample.path
    original_counterexample_bytes = counterexample_path.read_bytes()
    forged_counterexample = live_counterexample.model_copy(
        deep=True,
        update={"author_role": "legacy-untrusted-import"},
    )
    counterexample_path.write_text(render_node_note(forged_counterexample), encoding="utf-8")
    try:
        with pytest.raises(GraphValidationError, match="no longer live"):
            graph.record_counterexample_audit(
                problem_id=problem_id,
                run_id="run-one",
                assignment_id="worker-one",
                result_local_key=result.local_key,
                nomination=nomination.model_dump(mode="json"),
                gate=gate.model_dump(mode="json"),
                source_artifact=(
                    f".matek/runs/run-one/research/counterexample-audits/"
                    f"{nomination.audit_id}/gate.json"
                ),
            )
    finally:
        counterexample_path.write_bytes(original_counterexample_bytes)
    graph.record_counterexample_audit(
        problem_id=problem_id,
        run_id="run-one",
        assignment_id="worker-one",
        result_local_key=result.local_key,
        nomination=nomination.model_dump(mode="json"),
        gate=gate.model_dump(mode="json"),
        source_artifact=(
            f".matek/runs/run-one/research/counterexample-audits/{nomination.audit_id}/gate.json"
        ),
    )

    nodes = graph.load_nodes()
    counterexample = next(node for node in nodes if node.node_type is NodeType.COUNTEREXAMPLE)
    target = next(node for node in nodes if node.matek_id == target_id)
    assert counterexample.epistemic_status is EpistemicStatus.AUDIT_PASSED
    assert target.epistemic_status is EpistemicStatus.REFUTED
    assert any(
        edge.relation is RelationType.REFUTES and edge.target_id == target_id
        for edge in counterexample.relations
    )


def test_counterexample_local_support_cannot_resolve_to_main_target(tmp_path: Path) -> None:
    graph, _, problem_id, _ = initialized_graph(tmp_path)
    task_id, _ = graph_task(graph, problem_id)
    target_statement = "For every test object, the desired property holds."
    purported_theorem = ScientificResult(
        local_key="purported-main-proof",
        kind=ScientificResultKind.LEMMA,
        exact_statement=target_statement,
        scope=ScientificScope.MAIN,
        proof_or_certificate="A purported proof of the theorem being refuted.",
        disposition=ScientificResultDisposition.PROPOSED_COMPLETE,
    )
    counterexample = ScientificResult(
        local_key="circular-counterexample",
        kind=ScientificResultKind.COUNTEREXAMPLE,
        exact_statement=target_statement,
        scope=ScientificScope.MAIN,
        proof_or_certificate="A purported disproof that cites the theorem itself.",
        dependency_result_keys=[purported_theorem.local_key],
        disposition=ScientificResultDisposition.REFUTED_MECHANISM,
    )
    report = ResearchWorkerReport(
        assignment_id="worker-one",
        results=[purported_theorem, counterexample],
        branch_outcome=BranchOutcome.REFUTED,
        mechanism="Circular support must be rejected.",
    )
    graph.integrate_worker_report(
        problem_id=problem_id,
        run_id="run-one",
        assignment={
            "id": "worker-one",
            "approach_family": "counterexample",
            "task": "Test circular support rejection.",
        },
        task_id=task_id,
        report=report.model_dump(mode="json"),
        proposed_patch=None,
        source_artifact=".matek/runs/run-one/research/workers/worker-one.json",
        operation_id="circular-counterexample:run-one:worker-one",
    )

    with pytest.raises(StageValidationError, match="derive and use the main target"):
        build_counterexample_support_bundle(
            assignment_id="worker-one",
            root_result=counterexample,
            results=report.results,
            knowledge_graph=graph,
            graph_problem_id=problem_id,
            run_id="run-one",
        )


def test_typed_gapped_worker_report_creates_attempt_and_open_cut_not_candidate_proof(
    tmp_path: Path,
) -> None:
    graph, _, problem_id, _ = initialized_graph(tmp_path)
    task_id, _ = graph_task(graph, problem_id)
    report = {
        "schema_version": 2,
        "assignment_id": "worker-one",
        "results": [
            {
                "schema_version": 1,
                "local_key": "main-attempt",
                "kind": "lemma",
                "exact_statement": "For every test object, the desired property holds.",
                "scope": "main",
                "assumptions": [],
                "proof_or_certificate": "Reduce to a boundary lemma.",
                "exact_gap": "Prove the boundary lemma for every nonempty object.",
                "dependency_node_ids": [],
                "target_node_ids": [graph.main_claim_id(problem_id)],
                "disposition": "partial",
            }
        ],
        "unresolved_obligations": [],
        "source_ledger": [],
        "artifact_manifest": [],
        "branch_outcome": "blocked",
        "mechanism": "Boundary reduction",
    }
    merged = graph.integrate_worker_report(
        problem_id=problem_id,
        run_id="run-one",
        assignment={
            "id": "worker-one",
            "approach_family": "reduction",
            "task": "Try a boundary reduction.",
        },
        task_id=task_id,
        report=report,
        proposed_patch=None,
        source_artifact=".matek/runs/run-one/research/workers/worker-one.json",
        operation_id="typed-worker:run-one:worker-one",
    )

    assert merged.committed
    nodes = graph.load_nodes()
    assert any(node.node_type is NodeType.PROOF_ATTEMPT for node in nodes)
    obligation = next(node for node in nodes if node.node_type is NodeType.OBLIGATION)
    assert not any(node.node_type is NodeType.DERIVATION for node in nodes)
    candidate_dashboard = (
        graph.vault_root / "Dashboards" / "Candidate Proofs Awaiting Audit.md"
    ).read_text(encoding="utf-8")
    assert "Proof attempt: main-attempt" not in candidate_dashboard
    needs_dashboard = (graph.vault_root / "Dashboards" / "Main Result Needs.md").read_text(
        encoding="utf-8"
    )
    assert obligation.title in needs_dashboard
    frontier = graph.frontier(problem_id)
    assert [item.matek_id for item in frontier.smallest_known_open_cut] == [obligation.matek_id]


def test_shared_definition_preserves_both_admission_bindings_and_resume_order(
    tmp_path: Path,
) -> None:
    graph, _, problem_id, _ = initialized_graph(tmp_path)
    assignments = [
        {
            "id": worker_id,
            "approach_family": "notation",
            "task": "Declare the shared notation conservatively.",
            "expected_output": "One explicit branch-scoped definition.",
            "target_node_ids": [graph.main_claim_id(problem_id)],
        }
        for worker_id in ("worker-one", "worker-two")
    ]
    tasks, _, _ = graph.record_assignment_tasks(
        problem_id=problem_id,
        run_id="run-one",
        decision_id=91,
        assignments=assignments,
    )

    def report(worker_id: str, local_key: str) -> dict[str, object]:
        return {
            "schema_version": 2,
            "assignment_id": worker_id,
            "results": [
                {
                    "schema_version": 1,
                    "local_key": local_key,
                    "kind": "definition",
                    "exact_statement": "Define Boundary(n) to mean that n is a boundary index.",
                    "scope": "branch",
                    "assumptions": [],
                    "proof_or_certificate": "An explicit notation declaration.",
                    "exact_gap": None,
                    "dependency_node_ids": [],
                    "dependency_result_keys": [],
                    "target_node_ids": [],
                    "disposition": "proposed_complete",
                }
            ],
            "unresolved_obligations": [],
            "source_ledger": [],
            "artifact_manifest": [],
            "branch_outcome": "progress",
            "mechanism": "Fix shared boundary notation.",
        }

    reports = {
        "worker-one": report("worker-one", "boundary-one"),
        "worker-two": report("worker-two", "boundary-two"),
    }
    for worker_id in ("worker-two", "worker-one"):
        graph.integrate_worker_report(
            problem_id=problem_id,
            run_id="run-one",
            assignment=assignments[0 if worker_id == "worker-one" else 1],
            task_id=tasks[worker_id],
            report=reports[worker_id],
            proposed_patch=None,
            source_artifact=f".matek/runs/run-one/research/workers/{worker_id}.json",
            operation_id=f"definition:{worker_id}",
        )

    definitions = [node for node in graph.load_nodes() if node.node_type is NodeType.DEFINITION]
    assert len(definitions) == 1
    bindings = definitions[0].metadata.get("matek_admission_bindings")
    assert isinstance(bindings, list)
    assert len(bindings) == 2

    resumed = graph.integrate_worker_report(
        problem_id=problem_id,
        run_id="run-one",
        assignment=assignments[0],
        task_id=tasks["worker-one"],
        report=reports["worker-one"],
        proposed_patch=None,
        source_artifact=".matek/runs/run-one/research/workers/worker-one.json",
        operation_id="definition:worker-one:resume",
    )
    assert resumed.committed
    assert len([node for node in graph.load_nodes() if node.node_type is NodeType.DEFINITION]) == 1


def test_fabricated_passing_lemma_gate_cannot_promote_an_intermediate_derivation(
    tmp_path: Path,
) -> None:
    graph, _, problem_id, _ = initialized_graph(tmp_path)
    task_id, _ = graph_task(graph, problem_id)
    statement = "Every minimal test object has the desired boundary property."
    graph.integrate_worker_report(
        problem_id=problem_id,
        run_id="run-one",
        assignment={
            "id": "worker-one",
            "approach_family": "induction",
            "task": "Prove a useful intermediate lemma.",
        },
        task_id=task_id,
        report={
            "schema_version": 2,
            "assignment_id": "worker-one",
            "results": [
                {
                    "schema_version": 1,
                    "local_key": "boundary-lemma",
                    "kind": "lemma",
                    "exact_statement": statement,
                    "scope": "branch",
                    "assumptions": [],
                    "proof_or_certificate": "A complete minimal-counterexample argument.",
                    "dependency_node_ids": [],
                    "target_node_ids": [graph.main_claim_id(problem_id)],
                    "disposition": "proposed_complete",
                }
            ],
            "unresolved_obligations": [],
            "source_ledger": [],
            "artifact_manifest": [],
            "branch_outcome": "progress",
            "mechanism": "Minimal counterexample",
        },
        proposed_patch=None,
        source_artifact=".matek/runs/run-one/research/workers/worker-one.json",
        operation_id="typed-lemma:run-one:worker-one",
    )
    nodes = graph.load_nodes()
    claim = next(
        node
        for node in nodes
        if node.node_type is NodeType.CLAIM
        and node.metadata.get("matek_result_local_key") == "boundary-lemma"
    )
    derivation = next(
        node
        for node in nodes
        if node.node_type is NodeType.DERIVATION
        and node.metadata.get("matek_result_local_key") == "boundary-lemma"
    )
    nomination_id = "lemma-independent-boundary"
    statement_sha256 = hashlib.sha256(statement.encode()).hexdigest()
    with pytest.raises(GraphValidationError, match="persisted lemma-audit evidence"):
        graph.record_lemma_audit(
            problem_id=problem_id,
            run_id="run-one",
            nomination={
                "nomination_id": nomination_id,
                "statement_id": claim.matek_id,
                "scope": "branch",
                "exact_statement": statement,
                "origin_worker_id": "worker-one",
            },
            gate={
                "audit_id": nomination_id,
                "status": "audit_passed",
                "statement_sha256": statement_sha256,
                "input_sha256": "a" * 64,
                "response_sha256": {
                    "lemma-verifier": "b" * 64,
                    "lemma-falsifier": "c" * 64,
                },
                "accepted_intermediate": {
                    "statement_id": claim.matek_id,
                    "terminal_main_target_satisfied": False,
                    "manuscript_authorized": False,
                },
                "main_target_acceptance_authorized": False,
                "manuscript_authorized": False,
                "obligations": [],
                "falsification_evidence": [],
            },
            source_artifact=(
                f".matek/runs/run-one/research/lemma-audits/{nomination_id}/gate.json"
            ),
        )

    assert graph.show(claim.matek_id).epistemic_status is EpistemicStatus.CANDIDATE
    assert graph.show(derivation.matek_id).epistemic_status is EpistemicStatus.CANDIDATE
    assert (
        graph.show(graph.main_claim_id(problem_id)).epistemic_status is EpistemicStatus.CONJECTURED
    )
    ledger = json.loads(
        (graph.ledgers_root / problem_id / "canonical-ledger.json").read_text(encoding="utf-8")
    )
    assert ledger["derivations"][derivation.matek_id]["audit_ids"] == []
    assert claim.matek_id not in {
        item.matek_id for item in graph.frontier(problem_id).strongest_audited_results
    }


def test_fabricated_failed_lemma_gate_cannot_refute_a_route(
    tmp_path: Path,
) -> None:
    graph, _, problem_id, _ = initialized_graph(tmp_path)
    task_id, _ = graph_task(graph, problem_id)
    statement = "Every minimal test object has a unique boundary witness."
    graph.integrate_worker_report(
        problem_id=problem_id,
        run_id="run-one",
        assignment={"id": "worker-one", "approach_family": "uniqueness"},
        task_id=task_id,
        report={
            "schema_version": 2,
            "assignment_id": "worker-one",
            "results": [
                {
                    "schema_version": 1,
                    "local_key": "false-lemma",
                    "kind": "lemma",
                    "exact_statement": statement,
                    "scope": "branch",
                    "assumptions": [],
                    "proof_or_certificate": "A purported uniqueness argument.",
                    "dependency_node_ids": [],
                    "target_node_ids": [graph.main_claim_id(problem_id)],
                    "disposition": "proposed_complete",
                }
            ],
            "unresolved_obligations": [],
            "source_ledger": [],
            "artifact_manifest": [],
            "branch_outcome": "progress",
            "mechanism": "Uniqueness",
        },
        proposed_patch=None,
        source_artifact="worker-one.json",
        operation_id="typed-false-lemma:run-one:worker-one",
    )
    nodes = graph.load_nodes()
    claim = next(
        node
        for node in nodes
        if node.node_type is NodeType.CLAIM
        and node.metadata.get("matek_result_local_key") == "false-lemma"
    )
    derivation = next(
        node
        for node in nodes
        if node.node_type is NodeType.DERIVATION
        and node.metadata.get("matek_result_local_key") == "false-lemma"
    )
    with pytest.raises(GraphValidationError, match="canonical run artifact"):
        graph.record_lemma_audit(
            problem_id=problem_id,
            run_id="run-one",
            nomination={
                "nomination_id": "lemma-failed-uniqueness",
                "statement_id": claim.matek_id,
                "scope": "branch",
                "exact_statement": statement,
                "origin_worker_id": "worker-one",
            },
            gate={
                "audit_id": "lemma-failed-uniqueness",
                "status": "audit_failed",
                "statement_sha256": hashlib.sha256(statement.encode()).hexdigest(),
                "accepted_intermediate": None,
                "main_target_acceptance_authorized": False,
                "manuscript_authorized": False,
                "obligations": ["Repair the invalid uniqueness step."],
                "falsification_evidence": [],
            },
            source_artifact="lemma-audits/lemma-failed-uniqueness/gate.json",
        )

    assert graph.show(derivation.matek_id).epistemic_status is EpistemicStatus.CANDIDATE
    assert graph.show(claim.matek_id).epistemic_status is not EpistemicStatus.REFUTED


@pytest.mark.asyncio
async def test_passing_lemma_gate_resolves_only_exactly_matching_live_obligations(
    tmp_path: Path,
) -> None:
    graph, _, problem_id, _ = initialized_graph(tmp_path)
    tasks, _, _ = graph.record_assignment_tasks(
        problem_id=problem_id,
        run_id="run-one",
        decision_id=44,
        assignments=[
            {
                "id": "gap-worker",
                "approach_family": "gap-discovery",
                "task": "Expose two exact branch gaps.",
                "expected_output": "Two explicit obligations.",
                "target_node_ids": [graph.main_claim_id(problem_id)],
            },
            {
                "id": "lemma-worker",
                "approach_family": "gap-resolution",
                "task": "Prove the matching restricted lemma.",
                "expected_output": "One complete scoped lemma.",
                "target_node_ids": [graph.main_claim_id(problem_id)],
            },
        ],
    )
    exact_lemma = "Every boundary object has property P."
    unrelated_statement = "Every interior object has property Q."
    gap_report = ResearchWorkerReport(
        assignment_id="gap-worker",
        results=[
            ScientificResult(
                local_key="matching-gap",
                kind=ScientificResultKind.LEMMA,
                exact_statement=exact_lemma,
                scope=ScientificScope.BRANCH,
                proof_or_certificate="A reduction leaving one exact boundary case.",
                exact_gap="Prove the boundary case.",
                target_node_ids=[graph.main_claim_id(problem_id)],
                disposition=ScientificResultDisposition.PARTIAL,
            ),
            ScientificResult(
                local_key="unrelated-gap",
                kind=ScientificResultKind.LEMMA,
                exact_statement=unrelated_statement,
                scope=ScientificScope.BRANCH,
                proof_or_certificate="A different reduction leaving an interior case.",
                exact_gap="Prove the unrelated interior case.",
                target_node_ids=[graph.main_claim_id(problem_id)],
                disposition=ScientificResultDisposition.PARTIAL,
            ),
        ],
        unresolved_obligations=[
            ScientificObligationDeclaration(
                local_key="rich-matching-gap",
                exact_statement=exact_lemma,
                quantifiers=["For every boundary object x."],
                hypotheses=["x lies in the active branch."],
                conclusion=exact_lemma,
                parent_result_keys=["matching-gap"],
                scope=ScientificScope.BRANCH,
            )
        ],
        branch_outcome=BranchOutcome.BLOCKED,
        mechanism="Expose exact local gaps.",
    )
    graph.integrate_worker_report(
        problem_id=problem_id,
        run_id="run-one",
        assignment={"id": "gap-worker", "approach_family": "gap-discovery"},
        task_id=tasks["gap-worker"],
        report=gap_report.model_dump(mode="json"),
        proposed_patch=None,
        source_artifact=".matek/runs/run-one/research/workers/gap-worker.json",
        operation_id="gap-worker-report",
    )
    obligations = {
        str(node.metadata.get("matek_result_local_key")): node
        for node in graph.load_nodes()
        if node.node_type is NodeType.OBLIGATION
    }
    matching_obligation = obligations["matching-gap"]
    unrelated_obligation = obligations["unrelated-gap"]
    rich_matching_obligation = next(
        node
        for node in graph.load_nodes()
        if node.node_type is NodeType.OBLIGATION
        and node.metadata.get("matek_obligation_local_key") == "rich-matching-gap"
    )

    lemma_report = ResearchWorkerReport(
        assignment_id="lemma-worker",
        results=[
            ScientificResult(
                local_key="boundary-lemma",
                kind=ScientificResultKind.LEMMA,
                exact_statement=exact_lemma,
                scope=ScientificScope.BRANCH,
                proof_or_certificate="A complete proof of every boundary case.",
                target_node_ids=[
                    matching_obligation.matek_id,
                    unrelated_obligation.matek_id,
                    rich_matching_obligation.matek_id,
                ],
                disposition=ScientificResultDisposition.PROPOSED_COMPLETE,
            )
        ],
        branch_outcome=BranchOutcome.PROGRESS,
        mechanism="Close the exact boundary case.",
    )
    graph.integrate_worker_report(
        problem_id=problem_id,
        run_id="run-one",
        assignment={"id": "lemma-worker", "approach_family": "gap-resolution"},
        task_id=tasks["lemma-worker"],
        report=lemma_report.model_dump(mode="json"),
        proposed_patch=None,
        source_artifact=".matek/runs/run-one/research/workers/lemma-worker.json",
        operation_id="lemma-worker-report",
    )
    admitted = graph.load_nodes()
    claim = next(
        node
        for node in admitted
        if node.node_type is NodeType.CLAIM
        and node.metadata.get("matek_result_local_key") == "matching-gap"
    )
    derivation = next(
        node
        for node in admitted
        if node.node_type is NodeType.DERIVATION
        and node.metadata.get("matek_result_local_key") == "boundary-lemma"
    )
    attempt_id = str(derivation.metadata["matek_proof_attempt_id"])
    proof_attempt = graph.show(attempt_id)
    nomination_id = "lemma-resolves-exact-obligation"
    source_artifacts = [
        LemmaSourceArtifact(
            artifact_id=proof_attempt.matek_id,
            content=lemma_report.results[0].proof_or_certificate,
            content_sha256=hashlib.sha256(
                lemma_report.results[0].proof_or_certificate.encode()
            ).hexdigest(),
        ),
        LemmaSourceArtifact(
            artifact_id=derivation.matek_id,
            content=derivation.body,
            content_sha256=hashlib.sha256(derivation.body.encode()).hexdigest(),
        ),
    ]
    ledger = project_markdown_ledger(
        admitted,
        graph_revision=graph.load_state().revision,
        problem_id=problem_id,
        target_claim_id=graph.main_claim_id(problem_id),
    )
    obligation_nodes = {
        node.matek_id: node
        for node in (
            matching_obligation,
            unrelated_obligation,
            rich_matching_obligation,
        )
    }
    target_contracts: list[LemmaTargetObligationReference] = []
    for obligation_id in sorted(obligation_nodes):
        obligation = ledger.obligations[obligation_id]
        obligation_node = obligation_nodes[obligation_id]
        assert obligation_node.content_hash is not None
        target_contracts.append(
            LemmaTargetObligationReference(
                obligation_id=obligation.obligation_id,
                exact_statement=obligation.exact_statement,
                quantifiers=obligation.quantifiers,
                hypotheses=obligation.hypotheses,
                conclusion=obligation.conclusion,
                dependency_claim_ids=obligation.dependency_claim_ids,
                target_claim_ids=obligation.target_claim_ids,
                scope=obligation.scope,
                notation_definition_version=obligation.notation_definition_version,
                falsification_evidence=obligation.falsification_evidence,
                logical_version=obligation.logical_version,
                statement_version=obligation_node.statement_version,
                content_sha256=obligation_node.content_hash,
            )
        )
    nomination = LemmaNomination(
        nomination_id=nomination_id,
        statement_id=claim.matek_id,
        canonical_derivation_id=derivation.matek_id,
        result_kind=IntermediateResultKind.LEMMA,
        scope=LemmaScope.BRANCH,
        exact_statement=exact_lemma,
        main_target_statement="For every test object, the desired property holds.",
        target_obligation_ids=[
            matching_obligation.matek_id,
            unrelated_obligation.matek_id,
            rich_matching_obligation.matek_id,
        ],
        target_obligation_contracts=target_contracts,
        relevance_statement="The exact scoped lemma targets live main-result obligations.",
        supports_main_target=True,
        proof_steps=[
            LemmaProofStep(
                step_id="complete-proof",
                statement=exact_lemma,
                justification=lemma_report.results[0].proof_or_certificate,
                source_artifact_ids=[item.artifact_id for item in source_artifacts],
            )
        ],
        conclusion_step_id="complete-proof",
        gap_free=True,
        base_graph_revision=graph.load_state().revision,
        current_graph_revision=graph.load_state().revision,
        source_artifacts=source_artifacts,
        leverage=LemmaLeverage(
            downstream_obligation_ids=[
                matching_obligation.matek_id,
                unrelated_obligation.matek_id,
                rich_matching_obligation.matek_id,
            ],
            estimated_open_cut_reduction=3,
            unlocked_branch_count=0,
            rationale="Both nominated obligations are on the explicit branch frontier.",
        ),
        origin_worker_id="lemma-worker",
    )
    audit_dir = (
        graph.project_root
        / ".matek"
        / "runs"
        / "run-one"
        / "research"
        / "lemma-audits"
        / nomination_id
    )
    atomic_write_json(audit_dir / "nomination.json", nomination)

    class PassingAuditClient:
        def __init__(self, role: LemmaAuditRole) -> None:
            self.role = role

        async def generate_structured(
            self,
            request: ModelRequest,
            output_type: type[LemmaAuditResponse],
        ) -> ModelResult[LemmaAuditResponse]:
            packet = json.loads(request.input_text)["blind_lemma_audit_packet"]
            return ModelResult(
                parsed=LemmaAuditResponse(
                    audit_role=self.role,
                    audit_id=packet["audit_id"],
                    statement_sha256=packet["statement_sha256"],
                    decision=LemmaAuditDecision.PASS,
                    statement_aligned=True,
                    proof_valid=True if self.role is LemmaAuditRole.VERIFIER else None,
                    proof_step_ids_checked=["complete-proof"],
                    source_artifact_ids_checked=[
                        item["artifact_id"] for item in packet["source_artifacts"]
                    ],
                    checks_performed=["Checked every frozen proof step and source artifact."],
                    boundary_or_adversarial_cases=(
                        ["Checked the empty and smallest boundary objects."]
                        if self.role is LemmaAuditRole.FALSIFIER
                        else []
                    ),
                    rationale="The exact restricted lemma passed this independent role.",
                ),
                response_id=f"response-{self.role.value}",
            )

    await run_lemma_audit(
        nomination,
        audit_dir,
        verifier_client=PassingAuditClient(LemmaAuditRole.VERIFIER),
        falsifier_client=PassingAuditClient(LemmaAuditRole.FALSIFIER),
        settings=ModelSettings(web_search=False),
    )
    nomination_payload = json.loads((audit_dir / "nomination.json").read_text(encoding="utf-8"))
    gate_path = audit_dir / "gate.json"
    v2_gate_payload = json.loads(gate_path.read_text(encoding="utf-8"))
    v1_gate_payload = dict(v2_gate_payload)
    v1_gate_payload["schema_version"] = 1
    v1_gate_payload.pop("execution_context_ids")
    v1_gate_payload.pop("provider_session_ids")
    atomic_write_json(gate_path, v1_gate_payload)
    with pytest.raises(
        GraphValidationError,
        match="persisted lemma-audit evidence failed verification",
    ):
        graph.record_lemma_audit(
            problem_id=problem_id,
            run_id="run-one",
            nomination=nomination_payload,
            gate=v1_gate_payload,
            source_artifact=(
                f".matek/runs/run-one/research/lemma-audits/{nomination_id}/gate.json"
            ),
        )

    atomic_write_json(gate_path, v2_gate_payload)
    graph.record_lemma_audit(
        problem_id=problem_id,
        run_id="run-one",
        nomination=nomination_payload,
        gate=v2_gate_payload,
        source_artifact=(f".matek/runs/run-one/research/lemma-audits/{nomination_id}/gate.json"),
    )
    replayed_pass = graph.record_lemma_audit(
        problem_id=problem_id,
        run_id="run-one",
        nomination=nomination_payload,
        gate=v2_gate_payload,
        source_artifact=(f".matek/runs/run-one/research/lemma-audits/{nomination_id}/gate.json"),
    )
    assert replayed_pass.status == "already_applied"

    resolved = graph.show(matching_obligation.matek_id)
    still_open = graph.show(unrelated_obligation.matek_id)
    rich_still_open = graph.show(rich_matching_obligation.matek_id)
    promoted_derivation = graph.show(derivation.matek_id)
    assert resolved.epistemic_status is EpistemicStatus.AUDIT_PASSED
    assert resolved.workflow_status is WorkflowStatus.COMPLETE
    assert still_open.epistemic_status is EpistemicStatus.OPEN
    assert still_open.workflow_status is WorkflowStatus.BLOCKED
    assert rich_still_open.epistemic_status is EpistemicStatus.OPEN
    assert rich_still_open.workflow_status is WorkflowStatus.BLOCKED
    assert any(
        edge.relation is RelationType.RESOLVES and edge.target_id == matching_obligation.matek_id
        for edge in promoted_derivation.relations
    )
    assert not any(
        edge.relation is RelationType.RESOLVES and edge.target_id == unrelated_obligation.matek_id
        for edge in promoted_derivation.relations
    )
    assert not any(
        edge.relation is RelationType.RESOLVES
        and edge.target_id == rich_matching_obligation.matek_id
        for edge in promoted_derivation.relations
    )

    changed_contract = GraphPatch(
        base_graph_revision=graph.load_state().revision,
        run_id="run-one",
        task_id=tasks["gap-worker"],
        update_nodes=[
            GraphNodeUpdate(
                matek_id=rich_matching_obligation.matek_id,
                evidence=["A new concrete boundary failure was observed."],
                reason="Record newly discovered falsification evidence.",
            )
        ],
    )
    assert graph.merge_patch(
        changed_contract,
        problem_id=problem_id,
        operation_id="change-rich-obligation-contract",
    ).committed
    with pytest.raises(
        GraphValidationError,
        match=f"target obligation {rich_matching_obligation.matek_id} changed after lemma audit",
    ):
        graph.record_lemma_audit(
            problem_id=problem_id,
            run_id="run-one",
            nomination=nomination_payload,
            gate=v2_gate_payload,
            source_artifact=(
                f".matek/runs/run-one/research/lemma-audits/{nomination_id}/gate.json"
            ),
        )


def test_typed_worker_sources_dedupe_by_doi_and_cite_only_explicit_result_refs(
    tmp_path: Path,
) -> None:
    graph, _, problem_id, _ = initialized_graph(tmp_path)
    task_id, _ = graph_task(graph, problem_id)
    exact_result = "Every minimal test object has the desired property."
    report = {
        "schema_version": 2,
        "assignment_id": "worker-one",
        "results": [
            {
                "schema_version": 1,
                "local_key": "literature-lemma",
                "kind": "source_fact",
                "exact_statement": exact_result,
                "scope": "branch",
                "assumptions": [],
                "proof_or_certificate": "Apply the theorem exactly as stated in the source.",
                "exact_gap": None,
                "dependency_node_ids": [],
                "target_node_ids": [],
                "disposition": "proposed_complete",
            }
        ],
        "unresolved_obligations": [],
        "source_ledger": [
            {
                "source_id": "paper-doi",
                "title": "A foundational result",
                "identifiers": ["doi:10.1234/matek.7", "arXiv:2401.01234v1"],
                "evidence_claims": [
                    {
                        "claim": "result:literature-lemma",
                        "source_ids": ["paper-doi"],
                    }
                ],
                "purpose": "literature_support",
                "required_for_claim": False,
                "verified": True,
                "verification_detail": "DOI and arXiv version one resolved.",
            },
            {
                "source_id": "paper-revision",
                "title": "The revised foundational result",
                "identifiers": [
                    "https://doi.org/10.1234/matek.7",
                    "https://arxiv.org/abs/2401.01234v3",
                ],
                "evidence_claims": [{"claim": exact_result, "source_ids": ["paper-revision"]}],
                "purpose": "literature_support",
                "required_for_claim": False,
                "verified": True,
                "verification_detail": "DOI and arXiv version three resolved.",
            },
        ],
        "artifact_manifest": [],
        "branch_outcome": "progress",
        "mechanism": "Transfer an exact published lemma.",
    }
    operation_id = "typed-worker-source:run-one:worker-one"
    first = graph.integrate_worker_report(
        problem_id=problem_id,
        run_id="run-one",
        assignment={
            "id": "worker-one",
            "approach_family": "literature",
            "task": "Transfer a published lemma.",
        },
        task_id=task_id,
        report=report,
        proposed_patch=None,
        source_artifact=".matek/runs/run-one/research/workers/worker-one.json",
        operation_id=operation_id,
    )
    replay = graph.integrate_worker_report(
        problem_id=problem_id,
        run_id="run-one",
        assignment={
            "id": "worker-one",
            "approach_family": "literature",
            "task": "Transfer a published lemma.",
        },
        task_id=task_id,
        report=report,
        proposed_patch=None,
        source_artifact=".matek/runs/run-one/research/workers/worker-one.json",
        operation_id=operation_id,
    )

    assert first.committed
    assert replay.status == "already_applied"
    nodes = graph.load_nodes()
    sources = [node for node in nodes if node.node_type is NodeType.SOURCE]
    assert len(sources) == 1
    source = sources[0]
    assert source.metadata["matek_source_id"] == "doi:10.1234/matek.7"
    assert source.metadata["matek_identifiers"] == [
        "arxiv:2401.01234",
        "doi:10.1234/matek.7",
    ]
    assert source.metadata["matek_identifier_revisions"] == [
        "arxiv:2401.01234v1",
        "arxiv:2401.01234v3",
    ]
    assert source.metadata["matek_source_aliases"] == [
        "paper-doi",
        "paper-revision",
    ]
    result_nodes = [
        node for node in nodes if node.metadata.get("matek_result_local_key") == "literature-lemma"
    ]
    cited_types = {
        node.node_type
        for node in result_nodes
        if any(
            edge.relation is RelationType.CITES and edge.target_id == source.matek_id
            for edge in node.relations
        )
    }
    assert cited_types == {NodeType.CLAIM, NodeType.PROOF_ATTEMPT}
    derivation = next(node for node in result_nodes if node.node_type is NodeType.DERIVATION)
    assert not any(edge.relation is RelationType.CITES for edge in derivation.relations)

    second_tasks, _, _ = graph.record_assignment_tasks(
        problem_id=problem_id,
        run_id="run-one",
        decision_id=2,
        assignments=[
            {
                "id": "worker-two",
                "approach_family": "literature",
                "task": "Transfer another theorem from the same source entity.",
                "expected_output": "An exact theorem and source binding.",
                "target_node_ids": [graph.main_claim_id(problem_id)],
            }
        ],
    )
    second_report = {
        **report,
        "assignment_id": "worker-two",
        "results": [
            {
                **report["results"][0],
                "local_key": "second-literature-lemma",
                "exact_statement": "Every three-element test object has the property.",
            }
        ],
        "source_ledger": [
            {
                "source_id": "paper-final",
                "title": "The final foundational result",
                "identifiers": [
                    "doi:10.1234/matek.7",
                    "arXiv:2401.01234v4",
                ],
                "evidence_claims": [
                    {
                        "claim": "second-literature-lemma",
                        "source_ids": ["paper-final"],
                    }
                ],
                "purpose": "literature_support",
                "required_for_claim": False,
                "verified": True,
                "verification_detail": "The final revision resolved.",
            }
        ],
    }
    second = graph.integrate_worker_report(
        problem_id=problem_id,
        run_id="run-one",
        assignment={
            "id": "worker-two",
            "approach_family": "literature",
            "task": "Transfer another theorem from the same source entity.",
        },
        task_id=second_tasks["worker-two"],
        report=second_report,
        proposed_patch=None,
        source_artifact=".matek/runs/run-one/research/workers/worker-two.json",
        operation_id="typed-worker-source:run-one:worker-two",
    )
    assert second.committed
    merged_sources = [node for node in graph.load_nodes() if node.node_type is NodeType.SOURCE]
    assert len(merged_sources) == 1
    assert merged_sources[0].matek_id == source.matek_id
    assert merged_sources[0].metadata["matek_identifier_revisions"] == [
        "arxiv:2401.01234v1",
        "arxiv:2401.01234v3",
        "arxiv:2401.01234v4",
    ]
    assert merged_sources[0].metadata["matek_source_aliases"] == [
        "paper-doi",
        "paper-final",
        "paper-revision",
    ]


def test_typed_worker_arxiv_revisions_are_preserved_without_invented_citations(
    tmp_path: Path,
) -> None:
    graph, _, problem_id, _ = initialized_graph(tmp_path)
    task_id, _ = graph_task(graph, problem_id)
    report = {
        "schema_version": 2,
        "assignment_id": "worker-one",
        "results": [
            {
                "schema_version": 1,
                "local_key": "self-contained-lemma",
                "kind": "lemma",
                "exact_statement": "Every two-element test object has the property.",
                "scope": "branch",
                "assumptions": [],
                "proof_or_certificate": "Inspect the two elements directly.",
                "exact_gap": None,
                "dependency_node_ids": [],
                "target_node_ids": [],
                "disposition": "proposed_complete",
            }
        ],
        "unresolved_obligations": [],
        "source_ledger": [
            {
                "source_id": "preprint-v1",
                "title": "Related work, first version",
                "identifiers": ["arXiv:2502.00001v1"],
                "evidence_claims": [
                    {
                        "claim": "The paper studies a related but different problem.",
                        "source_ids": ["preprint-v1"],
                    }
                ],
                "purpose": "literature_support",
                "required_for_claim": False,
                "verified": True,
                "verification_detail": "Version one resolved.",
            },
            {
                "source_id": "preprint-v2",
                "title": "Related work, second version",
                "identifiers": ["https://arxiv.org/abs/2502.00001v2"],
                "evidence_claims": [
                    {
                        "claim": "The revision still studies the different problem.",
                        "source_ids": ["preprint-v2"],
                    }
                ],
                "purpose": "literature_support",
                "required_for_claim": False,
                "verified": True,
                "verification_detail": "Version two resolved.",
            },
            {
                "source_id": "unverified-paper",
                "title": "Unverified related claim",
                "identifiers": ["doi:10.1234/unverified"],
                "evidence_claims": [
                    {
                        "claim": "self-contained-lemma",
                        "source_ids": ["unverified-paper"],
                    }
                ],
                "purpose": "literature_support",
                "required_for_claim": False,
                "verified": False,
                "verification_detail": "No identifier could be verified.",
            },
            {
                "source_id": "same-title-different-id",
                "title": "Related work, first version",
                "identifiers": ["doi:10.4321/distinct-paper"],
                "evidence_claims": [
                    {
                        "claim": "This is a distinct paper despite the repeated title.",
                        "source_ids": ["same-title-different-id"],
                    }
                ],
                "purpose": "literature_support",
                "required_for_claim": False,
                "verified": True,
                "verification_detail": "The distinct DOI resolved.",
            },
        ],
        "artifact_manifest": [],
        "branch_outcome": "progress",
        "mechanism": "A self-contained finite proof.",
    }
    merged = graph.integrate_worker_report(
        problem_id=problem_id,
        run_id="run-one",
        assignment={
            "id": "worker-one",
            "approach_family": "finite",
            "task": "Prove the two-element case.",
        },
        task_id=task_id,
        report=report,
        proposed_patch=None,
        source_artifact=".matek/runs/run-one/research/workers/worker-one.json",
        operation_id="typed-worker-arxiv:run-one:worker-one",
    )

    assert merged.committed
    nodes = graph.load_nodes()
    sources = [node for node in nodes if node.node_type is NodeType.SOURCE]
    assert len(sources) == 2
    source = next(
        node for node in sources if node.metadata["matek_source_id"] == "arxiv:2502.00001"
    )
    assert source.metadata["matek_source_id"] == "arxiv:2502.00001"
    assert source.metadata["matek_identifier_revisions"] == [
        "arxiv:2502.00001v1",
        "arxiv:2502.00001v2",
    ]
    assert source.evidence == [
        "The paper studies a related but different problem.",
        "The revision still studies the different problem.",
    ]
    assert {node.metadata["matek_source_id"] for node in sources} == {
        "arxiv:2502.00001",
        "doi:10.4321/distinct-paper",
    }
    result_nodes = [
        node
        for node in nodes
        if node.metadata.get("matek_result_local_key") == "self-contained-lemma"
    ]
    assert not any(
        edge.relation is RelationType.CITES for node in result_nodes for edge in node.relations
    )


def test_later_doi_upgrades_existing_arxiv_source_without_duplicate_entity(
    tmp_path: Path,
) -> None:
    graph, _, problem_id, _ = initialized_graph(tmp_path)
    first_task_id, _ = graph_task(graph, problem_id)
    result = {
        "schema_version": 1,
        "local_key": "first-source-result",
        "kind": "source_fact",
        "exact_statement": "A cited source proves the finite auxiliary fact.",
        "scope": "branch",
        "assumptions": [],
        "proof_or_certificate": "Use the cited theorem.",
        "exact_gap": None,
        "dependency_node_ids": [],
        "target_node_ids": [],
        "disposition": "proposed_complete",
    }

    def report(
        assignment_id: str,
        *,
        local_key: str,
        source_alias: str,
        identifiers: list[str],
    ) -> dict[str, object]:
        current_result = {**result, "local_key": local_key}
        return {
            "schema_version": 2,
            "assignment_id": assignment_id,
            "results": [current_result],
            "unresolved_obligations": [],
            "source_ledger": [
                {
                    "source_id": source_alias,
                    "title": "The same auxiliary source",
                    "identifiers": identifiers,
                    "evidence_claims": [{"claim": local_key, "source_ids": [source_alias]}],
                    "purpose": "literature_support",
                    "required_for_claim": False,
                    "verified": True,
                    "verification_detail": "The stable identifiers resolved.",
                }
            ],
            "artifact_manifest": [],
            "branch_outcome": "progress",
            "mechanism": "Transfer an exact cited theorem.",
        }

    first = graph.integrate_worker_report(
        problem_id=problem_id,
        run_id="run-one",
        assignment={
            "id": "worker-one",
            "approach_family": "literature",
            "task": "Record the arXiv source.",
        },
        task_id=first_task_id,
        report=report(
            "worker-one",
            local_key="first-source-result",
            source_alias="preprint",
            identifiers=["arXiv:2601.00001v1"],
        ),
        proposed_patch=None,
        source_artifact=".matek/runs/run-one/research/workers/worker-one.json",
        operation_id="source-upgrade:run-one:worker-one",
    )
    assert first.committed
    arxiv_source = next(node for node in graph.load_nodes() if node.node_type is NodeType.SOURCE)

    task_ids, _, _ = graph.record_assignment_tasks(
        problem_id=problem_id,
        run_id="run-one",
        decision_id=2,
        assignments=[
            {
                "id": "worker-two",
                "approach_family": "literature",
                "task": "Record the published DOI for the same source.",
                "expected_output": "A verified DOI/arXiv identity binding.",
                "target_node_ids": [graph.main_claim_id(problem_id)],
            }
        ],
    )
    second = graph.integrate_worker_report(
        problem_id=problem_id,
        run_id="run-one",
        assignment={
            "id": "worker-two",
            "approach_family": "literature",
            "task": "Record the published DOI for the same source.",
        },
        task_id=task_ids["worker-two"],
        report=report(
            "worker-two",
            local_key="second-source-result",
            source_alias="published-paper",
            identifiers=["doi:10.5555/upgrade", "arXiv:2601.00001v2"],
        ),
        proposed_patch=None,
        source_artifact=".matek/runs/run-one/research/workers/worker-two.json",
        operation_id="source-upgrade:run-one:worker-two",
    )

    assert second.committed
    sources = [node for node in graph.load_nodes() if node.node_type is NodeType.SOURCE]
    assert len(sources) == 1
    assert sources[0].matek_id == arxiv_source.matek_id
    assert sources[0].metadata["matek_source_id"] == "doi:10.5555/upgrade"
    assert sources[0].metadata["matek_identifiers"] == [
        "arxiv:2601.00001",
        "doi:10.5555/upgrade",
    ]


def test_shared_url_cannot_merge_worker_sources_with_conflicting_dois(
    tmp_path: Path,
) -> None:
    graph, _, problem_id, _ = initialized_graph(tmp_path)
    task_id, revision = graph_task(graph, problem_id)
    report = {
        "schema_version": 2,
        "assignment_id": "worker-one",
        "results": [
            {
                "schema_version": 1,
                "local_key": "source-conflict-result",
                "kind": "source_fact",
                "exact_statement": "The source identity needs independent review.",
                "scope": "branch",
                "assumptions": [],
                "proof_or_certificate": "Two records claim the same landing page.",
                "exact_gap": None,
                "dependency_node_ids": [],
                "target_node_ids": [],
                "disposition": "proposed_complete",
            }
        ],
        "unresolved_obligations": [],
        "source_ledger": [
            {
                "source_id": "first-doi",
                "title": "First work",
                "identifiers": [
                    "doi:10.5555/first",
                    "https://publisher.example.edu/shared",
                ],
                "evidence_claims": [
                    {"claim": "source-conflict-result", "source_ids": ["first-doi"]}
                ],
                "purpose": "literature_support",
                "required_for_claim": False,
                "verified": True,
                "verification_detail": "The first DOI resolved.",
            },
            {
                "source_id": "second-doi",
                "title": "Second work",
                "identifiers": [
                    "doi:10.5555/second",
                    "https://publisher.example.edu/shared",
                ],
                "evidence_claims": [
                    {"claim": "source-conflict-result", "source_ids": ["second-doi"]}
                ],
                "purpose": "literature_support",
                "required_for_claim": False,
                "verified": True,
                "verification_detail": "The second DOI resolved.",
            },
        ],
        "artifact_manifest": [],
        "branch_outcome": "progress",
        "mechanism": "Audit source identity before transfer.",
    }

    merged = graph.integrate_worker_report(
        problem_id=problem_id,
        run_id="run-one",
        assignment={
            "id": "worker-one",
            "approach_family": "literature",
            "task": "Review potentially conflicting source records.",
            "matek_assignment_id": "worker-one",
        },
        task_id=task_id,
        report=report,
        proposed_patch=None,
        source_artifact=".matek/runs/run-one/research/workers/worker-one.json",
        operation_id="source-conflict:run-one:worker-one",
    )

    assert merged.committed
    assert merged.new_revision != revision
    assert any(issue.startswith("source_identity_ambiguity: ") for issue in merged.issues)
    sources = [node for node in graph.load_nodes() if node.node_type is NodeType.SOURCE]
    assert {node.metadata["matek_primary_identifier"] for node in sources} == {
        "doi:10.5555/first",
        "doi:10.5555/second",
    }
    result_node = next(
        node
        for node in graph.load_nodes()
        if node.metadata.get("matek_result_local_key") == "source-conflict-result"
    )
    assert {
        edge.target_id for edge in result_node.relations if edge.relation is RelationType.CITES
    } == {source.matek_id for source in sources}
    decision_logs = list((graph.graph_root / "repairs").glob("source-identity-decision-*.json"))
    assert len(decision_logs) == 1
    decision = json.loads(decision_logs[0].read_text(encoding="utf-8"))
    assert decision["failure_class"] == "source_identity_ambiguity"
    assert decision["decisions"][0]["decision"] == "preserve_separate_source_nodes"


@pytest.mark.asyncio
async def test_replayed_computation_creates_immutable_artifacts_and_proposed_derivation(
    tmp_path: Path,
) -> None:
    graph, _, problem_id, _ = initialized_graph(tmp_path)
    task_id, _ = graph_task(graph, problem_id)
    declaration = ScientificArtifactDeclaration(
        path="outputs/certificate.txt",
        purpose="Reproduce the exhaustive enumeration certificate.",
        supporting_result_keys=["enumeration"],
        command_line=["verify-enumeration", "inputs/domain.txt"],
        input_paths=["inputs/domain.txt"],
        stdout_path="captures/stdout.txt",
        stderr_path="captures/stderr.txt",
        expected_output="checked\n",
        replay_recipe="Run the fixed verifier against the frozen bounded domain.",
        tool_versions=["fixture-verifier 1"],
    )
    report = {
        "schema_version": 2,
        "assignment_id": "worker-one",
        "results": [
            {
                "schema_version": 1,
                "local_key": "enumeration",
                "kind": "computation",
                "exact_statement": "Every test object of size at most 8 has the property.",
                "scope": "computation",
                "assumptions": [],
                "proof_or_certificate": "Exhaustive enumeration certificate.",
                "exact_gap": None,
                "dependency_node_ids": [],
                "target_node_ids": [],
                "disposition": "proposed_complete",
            }
        ],
        "unresolved_obligations": [],
        "source_ledger": [],
        "artifact_manifest": [declaration.model_dump(mode="json")],
        "branch_outcome": "progress",
        "mechanism": "Exhaustively enumerate the bounded domain.",
    }
    run_root = graph.project_root / ".matek" / "runs" / "run-one"
    run_root.mkdir(parents=True, exist_ok=True)
    store = ComputationArtifactStore(run_root)
    workspace = store.prepare_workspace("worker-one")
    for relative, contents in {
        "outputs/certificate.txt": b"enumeration-certificate\n",
        "inputs/domain.txt": b"objects-through-size-8\n",
        "captures/stdout.txt": b"checked\n",
        "captures/stderr.txt": b"",
    }.items():
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
    collection = store.collect("worker-one", [declaration])
    assert collection.trusted

    class ReplayBackend:
        async def run(self, request: CommandRequest) -> CommandResult:
            output = request.cwd / "outputs" / "certificate.txt"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"enumeration-certificate\n")
            return CommandResult(
                argv=request.argv,
                cwd=request.cwd,
                exit_code=0,
                stdout="checked\n",
                stderr="",
                duration_seconds=0.01,
            )

    replay = await store.replay(
        "worker-one",
        ReplayBackend(),
        isolation=ComputationReplayIsolation(
            filesystem_write_confined=True,
            network_disabled=True,
            description="offline graph-integration fixture",
        ),
    )
    assert replay.trusted
    evidence = WorkerComputationEvidence(
        assignment_id="worker-one",
        collection=collection,
        replay=replay,
    )
    evidence_path = run_root / "research" / "worker-computation" / "worker-one.json"
    atomic_write_json(evidence_path, evidence)
    evidence_payload = {
        **evidence.model_dump(mode="json"),
        "source_artifact": (".matek/runs/run-one/research/worker-computation/worker-one.json"),
    }
    forged_payload = json.loads(json.dumps(evidence_payload))
    forged_payload["replay"]["record_sha256"] = "b" * 64
    with pytest.raises(GraphValidationError, match="differs from its canonical run artifact"):
        graph.integrate_worker_report(
            problem_id=problem_id,
            run_id="run-one",
            assignment={
                "id": "worker-one",
                "approach_family": "enumeration",
                "task": "Enumerate bounded test objects.",
            },
            task_id=task_id,
            report=report,
            proposed_patch=None,
            source_artifact=".matek/runs/run-one/research/workers/worker-one.json",
            operation_id="typed-computation:run-one:worker-one",
            computation_evidence=forged_payload,
        )

    merged = graph.integrate_worker_report(
        problem_id=problem_id,
        run_id="run-one",
        assignment={
            "id": "worker-one",
            "approach_family": "enumeration",
            "task": "Enumerate bounded test objects.",
        },
        task_id=task_id,
        report=report,
        proposed_patch=None,
        source_artifact=".matek/runs/run-one/research/workers/worker-one.json",
        operation_id="typed-computation:run-one:worker-one",
        computation_evidence=evidence_payload,
    )

    assert merged.committed
    nodes = graph.load_nodes()
    artifacts = [node for node in nodes if node.node_type is NodeType.ARTIFACT]
    assert len(artifacts) == 2
    assert all(node.epistemic_status is EpistemicStatus.AUDIT_PASSED for node in artifacts)
    derivation = next(node for node in nodes if node.node_type is NodeType.DERIVATION)
    artifact_ids = {node.matek_id for node in artifacts}
    assert artifact_ids.issubset({edge.target_id for edge in derivation.relations})

    computation_result = ScientificResult.model_validate(report["results"][0])
    exact_counterexample = ScientificResult(
        local_key="computed-exact-counterexample",
        kind=ScientificResultKind.COUNTEREXAMPLE,
        exact_statement="For every test object, the desired property holds.",
        scope=ScientificScope.MAIN,
        proof_or_certificate=(
            "The replayed exhaustive certificate contains a concrete test object without the "
            "desired property."
        ),
        dependency_result_keys=[computation_result.local_key],
        target_node_ids=[graph.main_claim_id(problem_id)],
        disposition=ScientificResultDisposition.REFUTED_MECHANISM,
    )
    closure_report = ResearchWorkerReport(
        assignment_id="worker-one",
        results=[computation_result, exact_counterexample],
        artifact_manifest=[declaration],
        branch_outcome=BranchOutcome.REFUTED,
        mechanism="A replayed exact finite certificate.",
    )
    graph.integrate_worker_report(
        problem_id=problem_id,
        run_id="run-one",
        assignment={
            "id": "worker-one",
            "approach_family": "enumeration",
            "task": "Use the replayed certificate as an exact counterexample.",
        },
        task_id=task_id,
        report=closure_report.model_dump(mode="json"),
        proposed_patch=None,
        source_artifact=".matek/runs/run-one/research/workers/worker-one.json",
        operation_id="typed-computation-counterexample:run-one:worker-one",
        computation_evidence=evidence_payload,
    )
    current_artifacts = [node for node in graph.load_nodes() if node.node_type is NodeType.ARTIFACT]
    assert len(current_artifacts) == 2
    assert all(
        node.epistemic_status is EpistemicStatus.AUDIT_PASSED
        and node.workflow_status is WorkflowStatus.COMPLETE
        and node.metadata.get("matek_replay_passed") is True
        and node.metadata.get("matek_computation_replay_status") == "passed"
        and "enumeration" in (node.metadata.get("matek_supporting_result_keys") or [])
        for node in current_artifacts
    ), [
        (
            node.author_role,
            node.epistemic_status,
            node.workflow_status,
            node.metadata,
        )
        for node in current_artifacts
    ]
    assert (
        next(
            node for node in current_artifacts if node.author_role == "computation-collector"
        ).metadata.get("matek_computation_manifest_sha256")
        == collection.manifest.manifest_sha256
    )
    assert (
        next(
            node for node in current_artifacts if node.author_role == "computation-replayer"
        ).metadata.get("matek_computation_replay_record_sha256")
        == replay.record_sha256
    )
    support = build_counterexample_support_bundle(
        assignment_id="worker-one",
        root_result=exact_counterexample,
        results=closure_report.results,
        artifact_manifest=closure_report.artifact_manifest,
        run_root=run_root,
        computation_evidence_path=evidence_path,
        knowledge_graph=graph,
        graph_problem_id=problem_id,
        run_id="run-one",
    )
    assert support.computation is not None

    external_computation_nodes = graph.load_nodes(include_human_notes=False)
    computation_claim = next(
        node
        for node in external_computation_nodes
        if node.node_type is NodeType.CLAIM
        and node.metadata.get("matek_result_local_key") == computation_result.local_key
    )
    computation_claim.epistemic_status = EpistemicStatus.AUDIT_PASSED
    external_counterexample = exact_counterexample.model_copy(
        update={
            "dependency_node_ids": [computation_claim.matek_id],
            "dependency_result_keys": [],
        }
    )
    external_root = next(
        node
        for node in external_computation_nodes
        if node.node_type is NodeType.COUNTEREXAMPLE
        and node.metadata.get("matek_result_local_key") == exact_counterexample.local_key
    )
    external_payload = admission_payload_sha256(external_counterexample)
    external_root.metadata["matek_admission_payload_sha256"] = external_payload
    external_root.metadata["matek_admission_bindings"] = [
        encode_admission_binding(
            admission_identity(
                "run-one",
                "worker-one",
                external_counterexample.local_key,
                external_counterexample.schema_version,
            ),
            external_payload,
        )
    ]
    with pytest.raises(StageValidationError, match="fresh same-report replay/CAS binding"):
        build_counterexample_support_bundle(
            assignment_id="worker-one",
            root_result=external_counterexample,
            results=[external_counterexample],
            knowledge_graph=CounterexampleGraphReadSnapshot(
                graph_name=graph.graph_name,
                state=graph.load_state(),
                nodes=tuple(external_computation_nodes),
                main_target_id=graph.main_claim_id(problem_id),
            ),
            graph_problem_id=problem_id,
            run_id="run-one",
        )

    unlinked_nodes = graph.load_nodes(include_human_notes=False)
    replay_node = next(
        node
        for node in unlinked_nodes
        if node.node_type is NodeType.ARTIFACT and node.author_role == "computation-replayer"
    )
    replay_node.relations = [
        edge
        for edge in replay_node.relations
        if not (edge.relation is RelationType.RELATED_TO and edge.target_id in artifact_ids)
    ]
    with pytest.raises(StageValidationError, match="not linked to its exact manifest"):
        build_counterexample_support_bundle(
            assignment_id="worker-one",
            root_result=exact_counterexample,
            results=closure_report.results,
            artifact_manifest=closure_report.artifact_manifest,
            run_root=run_root,
            computation_evidence_path=evidence_path,
            knowledge_graph=CounterexampleGraphReadSnapshot(
                graph_name=graph.graph_name,
                state=graph.load_state(),
                nodes=tuple(unlinked_nodes),
                main_target_id=graph.main_claim_id(problem_id),
            ),
            graph_problem_id=problem_id,
            run_id="run-one",
        )

    blocked_nodes = graph.load_nodes(include_human_notes=False)
    blocked_replay = next(
        node
        for node in blocked_nodes
        if node.node_type is NodeType.ARTIFACT and node.author_role == "computation-replayer"
    )
    blocked_replay.workflow_status = WorkflowStatus.BLOCKED
    with pytest.raises(StageValidationError, match="trusted graph replay pair"):
        build_counterexample_support_bundle(
            assignment_id="worker-one",
            root_result=exact_counterexample,
            results=closure_report.results,
            artifact_manifest=closure_report.artifact_manifest,
            run_root=run_root,
            computation_evidence_path=evidence_path,
            knowledge_graph=CounterexampleGraphReadSnapshot(
                graph_name=graph.graph_name,
                state=graph.load_state(),
                nodes=tuple(blocked_nodes),
                main_target_id=graph.main_claim_id(problem_id),
            ),
            graph_problem_id=problem_id,
            run_id="run-one",
        )


def test_recompiling_unchanged_source_reuses_frozen_target_across_contract_layouts(
    tmp_path: Path,
) -> None:
    graph, problem, problem_id, _ = initialized_graph(tmp_path)
    graph.initialize_problem(
        source_path=problem,
        problem_text=problem.read_text(encoding="utf-8"),
        run_id="run-two",
    )
    graph.record_compiled_problem(
        problem_id=problem_id,
        run_id="run-two",
        compiled_problem={
            "title": "Paraphrased test theorem",
            "normalized_statement": "All test objects enjoy the desired property.",
            "claim_contract": {
                "model": "all finite test objects, including degenerate cases",
                "success": "the desired property with no additive error",
            },
            "literature_status": "open_problem",
            "source_ledger": [],
        },
    )
    target = graph.show(graph.main_claim_id(problem_id))
    assert "For every test object, the desired property holds." in target.body
    assert "All test objects" not in target.body
    assert target.statement_version == 1
    problem_node = graph.show(problem_id)
    source_hash = str(problem_node.metadata["matek_normalized_source_sha256"])
    frozen = graph.frozen_target_for_source(source_hash)
    assert frozen.exact_statement == "For every test object, the desired property holds."
    assert json.loads(frozen.canonical_contract_json) == {"target": "the desired property"}


def test_edited_user_problem_requires_an_explicit_target_migration(tmp_path: Path) -> None:
    graph, problem, problem_id, _ = initialized_graph(tmp_path)
    changed_problem = "Prove the property only for nonempty test objects.\n"
    problem.write_text(changed_problem, encoding="utf-8")

    graph.initialize_problem(
        source_path=problem,
        problem_text=changed_problem,
        run_id="run-three",
    )
    changed = {
        "title": "Materially changed theorem",
        "normalized_statement": "For every nonempty test object, the property holds.",
        "claim_contract": {"target": "the property for nonempty objects"},
        "literature_status": "open_problem",
        "source_ledger": [],
    }
    with pytest.raises(GraphValidationError, match="explicit target migration"):
        graph.record_compiled_problem(
            problem_id=problem_id,
            run_id="run-three",
            compiled_problem=changed,
        )

    migrated = graph.record_compiled_problem(
        problem_id=problem_id,
        run_id="run-three",
        compiled_problem=changed,
        allow_target_migration=True,
        target_migration_reason="The user explicitly approved the nonempty-domain target.",
    )
    target = graph.show(graph.main_claim_id(problem_id))
    assert migrated.committed
    assert target.statement_version == 2
    assert target.epistemic_status is EpistemicStatus.STALE


def test_failed_compiled_graph_update_cannot_publish_target_registry_ahead_of_graph(
    tmp_path: Path,
) -> None:
    graph, _, problem_id, _ = initialized_graph(tmp_path)
    registry_before = graph.target_registry_path.read_bytes()
    revision_before = graph.load_state().revision

    with pytest.raises(GraphValidationError, match="verified source entities require"):
        graph.record_compiled_problem(
            problem_id=problem_id,
            run_id="run-two",
            compiled_problem={
                "title": "Changed target",
                "normalized_statement": "For every test object, a different property holds.",
                "claim_contract": {"target": "a different property"},
                "compiled_prompt": "Prove the explicitly migrated target.",
                "literature_status": "unknown",
                "source_ledger": [
                    {
                        "source_id": "invalid-verified-source",
                        "title": "Unresolved citation",
                        "identifiers": ["not a stable identifier"],
                        "verified": True,
                    }
                ],
            },
            allow_target_migration=True,
            target_migration_reason="Exercise atomic graph/target publication.",
        )

    assert graph.target_registry_path.read_bytes() == registry_before
    assert graph.load_state().revision == revision_before
    assert graph.show(graph.main_claim_id(problem_id)).statement_version == 1


def test_compiled_sources_use_verified_entity_identity_and_arxiv_revision_aliases(
    tmp_path: Path,
) -> None:
    graph, problem, problem_id, _ = initialized_graph(tmp_path)
    graph.initialize_problem(
        source_path=problem,
        problem_text=problem.read_text(encoding="utf-8"),
        run_id="run-two",
    )
    graph.record_compiled_problem(
        problem_id=problem_id,
        run_id="run-two",
        compiled_problem={
            "title": "Test theorem",
            "normalized_statement": "For every test object, the desired property holds.",
            "claim_contract": {"target": "the desired property"},
            "literature_status": "open_problem",
            "source_ledger": [
                {
                    "source_id": "model-source-one",
                    "title": "First preprint title",
                    "identifiers": ["arXiv:2401.01234v1"],
                    "verified": True,
                    "verification_detail": "Resolved version one.",
                },
                {
                    "source_id": "model-source-two",
                    "title": "Revised preprint title",
                    "identifiers": ["https://arxiv.org/abs/2401.01234v3"],
                    "verified": True,
                    "verification_detail": "Resolved version three.",
                },
            ],
        },
    )

    sources = [node for node in graph.load_nodes() if node.node_type is NodeType.SOURCE]
    assert len(sources) == 1
    source = sources[0]
    assert source.metadata["matek_source_id"] == "arxiv:2401.01234"
    assert source.metadata["matek_identifier_revisions"] == [
        "arxiv:2401.01234v1",
        "arxiv:2401.01234v3",
    ]
    assert source.metadata["matek_source_aliases"] == [
        "model-source-one",
        "model-source-two",
    ]

    graph.initialize_problem(
        source_path=problem,
        problem_text=problem.read_text(encoding="utf-8"),
        run_id="run-three",
    )
    graph.record_compiled_problem(
        problem_id=problem_id,
        run_id="run-three",
        compiled_problem={
            "title": "Test theorem",
            "normalized_statement": "For every test object, the desired property holds.",
            "claim_contract": {"target": "the desired property"},
            "literature_status": "open_problem",
            "source_ledger": [
                {
                    "source_id": "published-source",
                    "title": "Published preprint title",
                    "identifiers": [
                        "doi:10.5555/published-version",
                        "arXiv:2401.01234v4",
                    ],
                    "verified": True,
                    "verification_detail": "The DOI and final arXiv revision resolved.",
                }
            ],
        },
    )

    upgraded_sources = [node for node in graph.load_nodes() if node.node_type is NodeType.SOURCE]
    assert len(upgraded_sources) == 1
    assert upgraded_sources[0].matek_id == source.matek_id
    assert upgraded_sources[0].metadata["matek_source_id"] == ("doi:10.5555/published-version")
    assert upgraded_sources[0].metadata["matek_identifier_revisions"] == [
        "arxiv:2401.01234v1",
        "arxiv:2401.01234v3",
        "arxiv:2401.01234v4",
    ]


def test_compiled_multi_doi_versions_remain_distinct_and_record_decision(
    tmp_path: Path,
) -> None:
    graph, problem, problem_id, _ = initialized_graph(tmp_path)
    compiled_base = {
        "title": "Matroid secretary theorem",
        "normalized_statement": "For every test object, the desired property holds.",
        "claim_contract": {"target": "the desired property"},
        "literature_status": "resolved",
    }
    separate_versions = [
        {
            "source_id": "conference-version",
            "title": "Online contention resolution schemes",
            "authors": ["Moran Feldman", "Ola Svensson", "Rico Zenklusen"],
            "identifiers": ["doi:10.1137/1.9781611973730.79"],
            "verified": True,
            "verification_detail": "The SODA publication resolved.",
        },
        {
            "source_id": "journal-version",
            "title": "Online contention resolution schemes",
            "authors": ["Moran Feldman", "Ola Svensson", "Rico Zenklusen"],
            "identifiers": ["doi:10.1287/moor.2017.0876"],
            "verified": True,
            "verification_detail": "The journal publication resolved.",
        },
    ]
    graph.initialize_problem(
        source_path=problem,
        problem_text=problem.read_text(encoding="utf-8"),
        run_id="run-two",
    )
    graph.record_compiled_problem(
        problem_id=problem_id,
        run_id="run-two",
        compiled_problem={**compiled_base, "source_ledger": separate_versions},
    )

    graph.initialize_problem(
        source_path=problem,
        problem_text=problem.read_text(encoding="utf-8"),
        run_id="run-three",
    )
    merged = graph.record_compiled_problem(
        problem_id=problem_id,
        run_id="run-three",
        compiled_problem={
            **compiled_base,
            "source_ledger": [
                {
                    "source_id": "feldman-svensson-zenklusen",
                    "title": "Online contention resolution schemes",
                    "authors": ["Moran Feldman", "Ola Svensson", "Rico Zenklusen"],
                    "identifiers": [
                        "DOI:10.1137/1.9781611973730.79",
                        "https://doi.org/10.1287/MOOR.2017.0876",
                    ],
                    "verified": True,
                    "verification_detail": "Both publication records resolved.",
                }
            ],
        },
    )

    assert merged.committed
    assert any(issue.startswith("source_identity_ambiguity: ") for issue in merged.issues)
    sources = [node for node in graph.load_nodes() if node.node_type is NodeType.SOURCE]
    assert len(sources) == 2
    assert {node.metadata["matek_primary_identifier"] for node in sources} == {
        "doi:10.1137/1.9781611973730.79",
        "doi:10.1287/moor.2017.0876",
    }
    assert all(
        len(
            [
                identifier
                for identifier in node.metadata["matek_identifiers"]
                if identifier.startswith("doi:")
            ]
        )
        == 1
        for node in sources
    )
    target = graph.show(graph.main_claim_id(problem_id))
    cited_source_ids = {
        edge.target_id for edge in target.relations if edge.relation is RelationType.CITES
    }
    assert cited_source_ids == {source.matek_id for source in sources}
    decision_logs = list((graph.graph_root / "repairs").glob("source-identity-decision-*.json"))
    assert len(decision_logs) == 1
    decision = json.loads(decision_logs[0].read_text(encoding="utf-8"))
    assert decision["decisions"][0]["doi_identifiers"] == [
        "doi:10.1137/1.9781611973730.79",
        "doi:10.1287/moor.2017.0876",
    ]
    assert set(decision["decisions"][0]["candidate_node_ids"]) == {
        source.matek_id for source in sources
    }


def test_graph_doctor_repairs_source_identity_metadata_and_logs_the_transaction(
    tmp_path: Path,
) -> None:
    graph, problem, problem_id, _ = initialized_graph(tmp_path)
    graph.initialize_problem(
        source_path=problem,
        problem_text=problem.read_text(encoding="utf-8"),
        run_id="run-two",
    )
    compiled = {
        "title": "Test theorem",
        "normalized_statement": "For every test object, the desired property holds.",
        "claim_contract": {"target": "the desired property"},
        "literature_status": "open_problem",
        "source_ledger": [
            {
                "source_id": "published-source",
                "title": "A useful source",
                "identifiers": ["arxiv:2103.04205", "doi:10.1137/24m1630207"],
                "verified": True,
                "verification_detail": "Both stable identifiers resolved.",
            }
        ],
    }
    graph.record_compiled_problem(
        problem_id=problem_id,
        run_id="run-two",
        compiled_problem=compiled,
    )

    with graph._locked():
        state = graph._load_state_unlocked()
        nodes = graph._load_nodes_unlocked(include_human_notes=True)
        source = next(node for node in nodes if node.node_type is NodeType.SOURCE)
        source.metadata["matek_primary_identifier"] = "arxiv:9999.99999"
        graph._commit_nodes_unlocked(
            state=state,
            all_nodes=nodes,
            changed_node_ids=[source.matek_id],
            run_id="legacy-run",
            author="legacy-fixture",
            reason="Simulate historical generated metadata drift.",
            operation_id="legacy-source-metadata-drift",
        )

    planned = graph.doctor(problem_id=problem_id)
    assert len(planned.actions) == 1
    assert planned.actions[0].applied is False
    assert graph.load_state().revision == planned.previous_revision

    repaired = graph.doctor(repair=True, problem_id=problem_id, run_id="run-three")
    assert repaired.actions[0].rule == "primary_identifier_in_identifiers"
    assert repaired.actions[0].applied is True
    assert repaired.actions[0].before["primary_identifier"] == "arxiv:9999.99999"
    assert repaired.actions[0].after["primary_identifier"] == "doi:10.1137/24m1630207"
    assert repaired.repair_log is not None
    repair_log = json.loads((graph.graph_root / repaired.repair_log).read_text(encoding="utf-8"))
    assert repair_log["failure_class"] == "metadata_invariant"
    assert repair_log["actions"][0]["node_id"] == repaired.actions[0].node_id
    source = graph.show(repaired.actions[0].node_id)
    assert source.metadata["matek_primary_identifier"] == "doi:10.1137/24m1630207"
    assert graph.validate().valid

    # The repaired source no longer blocks the ordinary compiled-problem transaction.
    graph.initialize_problem(
        source_path=problem,
        problem_text=problem.read_text(encoding="utf-8"),
        run_id="run-three",
    )
    assert graph.record_compiled_problem(
        problem_id=problem_id,
        run_id="run-three",
        compiled_problem=compiled,
    ).committed

    with graph._locked():
        state = graph._load_state_unlocked()
        nodes = graph._load_nodes_unlocked(include_human_notes=True)
        source = next(node for node in nodes if node.node_type is NodeType.SOURCE)
        source.metadata["matek_identifiers"] = []
        source.metadata["matek_identifier_revisions"] = []
        graph._commit_nodes_unlocked(
            state=state,
            all_nodes=nodes,
            changed_node_ids=[source.matek_id],
            run_id="legacy-run",
            author="legacy-fixture",
            reason="Simulate a source whose stable identifiers disappeared.",
            operation_id="legacy-source-identifiers-missing",
        )
    downgraded = graph.doctor(repair=True, problem_id=problem_id, run_id="run-four")
    assert downgraded.warnings
    source = graph.show(source.matek_id)
    assert source.metadata["matek_primary_identifier"] is None
    assert source.metadata["matek_verified"] is False
    assert source.epistemic_status is EpistemicStatus.OPEN

    with graph._locked():
        state = graph._load_state_unlocked()
        nodes = graph._load_nodes_unlocked(include_human_notes=True)
        source = next(node for node in nodes if node.node_type is NodeType.SOURCE)
        source.metadata["matek_source_id"] = ""
        source.metadata["matek_primary_identifier"] = "malformed-primary"
        graph._commit_nodes_unlocked(
            state=state,
            all_nodes=nodes,
            changed_node_ids=[source.matek_id],
            run_id="legacy-run",
            author="legacy-fixture",
            reason="Simulate metadata outside the whitelisted repair boundary.",
            operation_id="legacy-source-key-missing",
        )
    revision_before_failed_repair = graph.load_state().revision
    with pytest.raises(GraphValidationError, match="lacks a canonical source identity"):
        graph.doctor(repair=True, problem_id=problem_id, run_id="run-five")
    assert graph.load_state().revision == revision_before_failed_repair


def test_graph_doctor_marks_historical_mixed_doi_identity_without_losing_evidence(
    tmp_path: Path,
) -> None:
    graph, problem, problem_id, _ = initialized_graph(tmp_path)
    graph.initialize_problem(
        source_path=problem,
        problem_text=problem.read_text(encoding="utf-8"),
        run_id="run-two",
    )
    graph.record_compiled_problem(
        problem_id=problem_id,
        run_id="run-two",
        compiled_problem={
            "title": "Test theorem",
            "normalized_statement": "For every test object, the desired property holds.",
            "claim_contract": {"target": "the desired property"},
            "literature_status": "resolved",
            "source_ledger": [
                {
                    "source_id": "historical-source",
                    "title": "A conference paper",
                    "identifiers": ["doi:10.1137/1.9781611973730.79"],
                    "verified": True,
                    "verification_detail": "The publication resolved.",
                }
            ],
        },
    )
    with graph._locked():
        state = graph._load_state_unlocked()
        nodes = graph._load_nodes_unlocked(include_human_notes=True)
        source = next(node for node in nodes if node.node_type is NodeType.SOURCE)
        source.metadata["matek_identifiers"] = [
            "doi:10.1137/1.9781611973730.79",
            "https://doi.org/10.1287/MOOR.2017.0876",
        ]
        source.evidence = ["The historical citation supports the recorded theorem."]
        graph._commit_nodes_unlocked(
            state=state,
            all_nodes=nodes,
            changed_node_ids=[source.matek_id],
            run_id="legacy-run",
            author="legacy-fixture",
            reason="Simulate a historical mixed-publication source record.",
            operation_id="legacy-mixed-doi-source",
        )

    planned = graph.doctor(problem_id=problem_id)
    assert [action.rule for action in planned.actions] == ["multiple_doi_versions"]
    assert planned.warnings[0].startswith("source_identity_ambiguity: ")

    repaired = graph.doctor(repair=True, problem_id=problem_id, run_id="run-three")
    assert repaired.actions[0].applied is True
    assert repaired.repair_log is not None
    repaired_source = graph.show(source.matek_id)
    assert repaired_source.evidence == ["The historical citation supports the recorded theorem."]
    decision = json.loads(str(repaired_source.metadata["matek_source_identity_decision"]))
    assert decision["doi_identifiers"] == [
        "doi:10.1137/1.9781611973730.79",
        "doi:10.1287/moor.2017.0876",
    ]
    assert graph.doctor(problem_id=problem_id).actions == []


def test_persistent_markdown_vault_survives_two_runs_and_rebuilds_index(
    tmp_path: Path,
) -> None:
    graph, problem, problem_id, first_revision = initialized_graph(tmp_path)
    second_problem_id, second_revision = graph.initialize_problem(
        source_path=problem,
        problem_text=problem.read_text(encoding="utf-8"),
        run_id="run-two",
    )

    assert second_problem_id == problem_id
    assert second_revision != first_revision
    scratch = graph.vault_root / "Human Notes" / "my-observation.md"
    scratch.write_text("# Observation\n\nA human-only note.\n", encoding="utf-8")
    nodes = graph.load_nodes()
    assert len([node for node in nodes if node.node_type is NodeType.PROBLEM]) == 1
    assert len([node for node in nodes if node.node_type is NodeType.RUN]) == 2
    assert any(node.node_type is NodeType.HUMAN_NOTE for node in nodes)
    assert (graph.vault_root / "Home.md").is_file()
    assert (graph.vault_root / "Dashboards" / "Open Claims.md").is_file()
    assert (graph.vault_root / "Dashboards" / "Main Proof Architecture.canvas").is_file()
    graph.index_path.unlink()
    rebuilt = graph.rebuild_index()
    with sqlite3.connect(rebuilt) as connection:
        assert connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == len(nodes)
    assert graph.validate().valid


def test_existing_problem_graph_requires_coordinator_review_before_delegation(
    tmp_path: Path,
) -> None:
    graph, problem, problem_id, _ = initialized_graph(tmp_path)
    graph.initialize_problem(
        source_path=problem,
        problem_text=problem.read_text(encoding="utf-8"),
        run_id="run-two",
    )

    memory = graph.coordinator_memory(problem_id, current_run_id="run-two")

    assert memory["review_required_before_delegation"] is True
    overview = memory["overview"]
    assert isinstance(overview, dict)
    assert overview["prior_node_count"] > 0
    assert overview["node_type_counts"]["claim"] == 1
    frontier = memory["frontier"]
    assert isinstance(frontier, dict)
    assert frontier["unresolved_claims"]
    assert "Reconstruct the current branch map" in memory["instruction"]


def test_same_run_resume_requires_current_frontier_reconstruction(tmp_path: Path) -> None:
    graph, _, problem_id, _ = initialized_graph(tmp_path)

    memory = graph.coordinator_memory(
        problem_id,
        current_run_id="run-one",
        resume_reconstruction=True,
        previous_coordinator_revision="00000000-0000000000000000",
    )

    assert memory["review_required_before_delegation"] is True
    assert memory["current_frontier_review_required"] is True
    assert memory["resume_reconstruction"] is True
    assert memory["graph_changed_since_previous_coordinator_activation"] is True
    assert memory["graph_revision"] in memory["instruction"]


def test_assignment_graph_targets_are_explicit_and_never_silently_replaced(
    tmp_path: Path,
) -> None:
    graph, _, problem_id, _ = initialized_graph(tmp_path)

    with pytest.raises(GraphValidationError, match="must name at least one graph target"):
        graph.record_assignment_tasks(
            problem_id=problem_id,
            run_id="run-one",
            decision_id=3,
            assignments=[
                {
                    "id": "untargeted",
                    "approach_family": "direct",
                    "task": "Try a direct proof.",
                    "expected_output": "A proof or exact obstruction.",
                    "target_node_ids": [],
                }
            ],
        )

    with pytest.raises(GraphValidationError, match="unknown target"):
        graph.validate_assignment_targets(
            problem_id=problem_id,
            assignments=[
                {
                    "id": "invented-target",
                    "target_node_ids": ["CLM-NOTREAL1"],
                }
            ],
        )


def test_same_family_worker_branches_keep_distinct_negative_history(tmp_path: Path) -> None:
    graph, _, problem_id, _ = initialized_graph(tmp_path)
    target_id = graph.main_claim_id(problem_id)
    tasks, _, _ = graph.record_assignment_tasks(
        problem_id=problem_id,
        run_id="run-one",
        decision_id=4,
        assignments=[
            {
                "id": "algebra-strengthening",
                "approach_family": "algebraic",
                "task": "Test a strong algebraic invariant.",
                "expected_output": "A proof or a concrete obstruction.",
                "target_node_ids": [target_id],
            },
            {
                "id": "algebra-decomposition",
                "approach_family": "algebraic",
                "task": "Develop a decomposition lemma.",
                "expected_output": "A precise lemma and proof.",
                "target_node_ids": [target_id],
            },
        ],
    )

    graph.integrate_worker_report(
        problem_id=problem_id,
        run_id="run-one",
        assignment={
            "id": "algebra-strengthening",
            "approach_family": "algebraic",
            "task": "Test a strong algebraic invariant.",
            "target_node_ids": [target_id],
        },
        task_id=tasks["algebra-strengthening"],
        report={
            "assignment_id": "algebra-strengthening",
            "status": "refuted",
            "formal_results": [],
            "proof_content": "The strengthening fails on the smallest nontrivial object.",
            "exact_gap": None,
            "counterexamples": ["A three-element object violates the proposed invariant."],
            "dependencies": [],
            "assumptions": [],
            "mechanism": "Strong algebraic invariant",
            "sources": [],
        },
        proposed_patch=None,
        source_artifact="research/workers/algebra-strengthening.json",
        operation_id="worker-report:run-one:algebra-strengthening",
    )
    graph.integrate_worker_report(
        problem_id=problem_id,
        run_id="run-one",
        assignment={
            "id": "algebra-decomposition",
            "approach_family": "algebraic",
            "task": "Develop a decomposition lemma.",
            "target_node_ids": [target_id],
        },
        task_id=tasks["algebra-decomposition"],
        report={
            "assignment_id": "algebra-decomposition",
            "status": "progress",
            "formal_results": ["Every minimal object admits the required decomposition."],
            "proof_content": "Choose a minimal separator and decompose along it.",
            "exact_gap": "Show that the decomposition recombines without loss.",
            "counterexamples": [],
            "dependencies": [],
            "assumptions": [],
            "mechanism": "Minimal-separator decomposition",
            "sources": [],
        },
        proposed_patch=None,
        source_artifact="research/workers/algebra-decomposition.json",
        operation_id="worker-report:run-one:algebra-decomposition",
    )

    nodes = graph.load_nodes()
    algebra_branches = [
        node
        for node in nodes
        if node.node_type is NodeType.APPROACH
        and node.metadata.get("matek_assignment_ids")
        in (["algebra-strengthening"], ["algebra-decomposition"])
    ]
    assert len(algebra_branches) == 2
    refuted_branch = next(
        node
        for node in algebra_branches
        if node.metadata["matek_assignment_ids"] == ["algebra-strengthening"]
    )
    productive_branch = next(
        node
        for node in algebra_branches
        if node.metadata["matek_assignment_ids"] == ["algebra-decomposition"]
    )
    assert refuted_branch.epistemic_status is EpistemicStatus.REFUTED
    assert refuted_branch.workflow_status is WorkflowStatus.ABANDONED
    assert productive_branch.epistemic_status is EpistemicStatus.OPEN
    assert productive_branch.workflow_status is WorkflowStatus.ACTIVE

    [counterexample] = [node for node in nodes if node.node_type is NodeType.COUNTEREXAMPLE]
    assert any(
        edge.relation is RelationType.RELATED_TO and edge.target_id == refuted_branch.matek_id
        for edge in counterexample.relations
    )
    assert not any(
        edge.relation is RelationType.REFUTES and edge.target_id == target_id
        for edge in counterexample.relations
    )
    assert refuted_branch.matek_id in {
        item.matek_id for item in graph.frontier(problem_id).refuted_or_unproductive_routes
    }


def test_obsidian_note_paths_use_titles_and_migrate_legacy_generated_names(
    tmp_path: Path,
) -> None:
    graph, _, problem_id, _ = initialized_graph(tmp_path)
    target_id = graph.main_claim_id(problem_id)
    target = graph.show(target_id)
    assert target.path is not None
    title_path = Path(target.path)
    assert title_path.name == f"{target.title}.md"
    assert title_path.parent.name == target_id

    legacy_path = Path("Claims") / f"{target_id}--legacy-generated-title.md"
    (graph.vault_root / title_path).rename(graph.vault_root / legacy_path)
    state = graph.load_state()
    state.node_paths[target_id] = legacy_path.as_posix()
    graph.state_path.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")

    graph.initialize()
    migrated = graph.show(target_id)
    assert migrated.path == title_path.as_posix()
    assert (graph.vault_root / title_path).is_file()
    assert not (graph.vault_root / legacy_path).exists()
    assert graph.validate().valid


def test_patch_merge_validates_relations_duplicates_and_lean_promotion(tmp_path: Path) -> None:
    graph, _, problem_id, _ = initialized_graph(tmp_path)
    task_id, revision = graph_task(graph, problem_id)
    claim_id = "CLM-TEST0001"
    proof_id = "PRF-TEST0001"
    patch = GraphPatch(
        base_graph_revision=revision,
        run_id="run-one",
        task_id=task_id,
        create_nodes=[
            GraphNodeCreate(
                matek_id=claim_id,
                node_type=NodeType.CLAIM,
                claim_type=ClaimType.LEMMA,
                title="Intermediate lemma",
                body="## Exact statement\n\nThe intermediate property holds.",
                epistemic_status=EpistemicStatus.CANDIDATE,
            ),
            GraphNodeCreate(
                matek_id=proof_id,
                node_type=NodeType.PROOF,
                title="Proof of intermediate lemma",
                body="## Proof content\n\nA complete candidate argument.",
                epistemic_status=EpistemicStatus.CANDIDATE,
            ),
        ],
        add_edges=[GraphEdge(source_id=proof_id, relation=RelationType.PROVES, target_id=claim_id)],
        evidence=["research/workers/worker-one.json"],
    )

    merged = graph.merge_patch(patch, problem_id=problem_id, operation_id="patch-one")
    assert merged.status == "merged"
    assert {claim_id, proof_id} <= set(merged.created_node_ids)
    assert graph.show(proof_id).relations[0].relation is RelationType.PROVES

    duplicate = patch.model_copy(update={"base_graph_revision": merged.new_revision})
    conflict = graph.merge_patch(duplicate, problem_id=problem_id, operation_id="patch-duplicate")
    assert conflict.status == "conflict"
    assert any("already exists" in issue or "duplicate" in issue for issue in conflict.issues)

    claim = graph.show(claim_id)
    assert claim.content_hash is not None
    prohibited = GraphPatch(
        base_graph_revision=graph.load_state().revision,
        run_id="run-one",
        task_id=task_id,
        proposed_status_changes=[
            GraphStatusChange(
                matek_id=claim_id,
                epistemic_status=EpistemicStatus.LEAN_VERIFIED,
                reason="The worker says Lean succeeded.",
            )
        ],
    )
    rejected = graph.merge_patch(
        prohibited, problem_id=problem_id, operation_id="worker-lean-promotion"
    )
    assert rejected.status == "rejected"
    assert "deterministic Lean" in " ".join(rejected.issues)

    prepromoted = GraphPatch(
        base_graph_revision=graph.load_state().revision,
        run_id="run-one",
        task_id=task_id,
        create_nodes=[
            GraphNodeCreate(
                matek_id="CLM-TEST0002",
                node_type=NodeType.CLAIM,
                claim_type=ClaimType.LEMMA,
                title="Worker-preapproved lemma",
                body="## Exact statement\n\nA worker-declared audited lemma.",
                epistemic_status=EpistemicStatus.AUDIT_PASSED,
            )
        ],
    )
    prepromoted_result = graph.merge_patch(
        prepromoted,
        problem_id=problem_id,
        operation_id="worker-created-audit-promotion",
    )
    assert prepromoted_result.status == "rejected"
    assert "independent audit" in " ".join(prepromoted_result.issues)

    worker_refutation = GraphPatch(
        base_graph_revision=graph.load_state().revision,
        run_id="run-one",
        task_id=task_id,
        proposed_status_changes=[
            GraphStatusChange(
                matek_id=claim_id,
                epistemic_status=EpistemicStatus.REFUTED,
                reason="The worker claims a counterexample.",
            )
        ],
    )
    worker_refutation_result = graph.merge_patch(
        worker_refutation,
        problem_id=problem_id,
        operation_id="worker-claim-refutation",
    )
    assert worker_refutation_result.status == "rejected"
    assert "independent review" in " ".join(worker_refutation_result.issues)
    tombstone = graph.tombstone(proof_id, reason="Superseded by a corrected proof.")
    assert tombstone.committed
    assert graph.show(proof_id).tombstone


def test_accepted_result_marks_exact_main_proof_support_subgraph(tmp_path: Path) -> None:
    graph, _, problem_id, _ = initialized_graph(tmp_path)
    task_id, revision = graph_task(graph, problem_id)
    target_id = graph.main_claim_id(problem_id)
    dependency_id = "CLM-NEEDED01"
    dependency_proof_id = "PRF-NEEDED01"
    source_id = "SRC-NEEDED01"
    approach_id = "APR-NOTNEED1"
    merged = graph.merge_patch(
        GraphPatch(
            base_graph_revision=revision,
            run_id="run-one",
            task_id=task_id,
            create_nodes=[
                GraphNodeCreate(
                    matek_id=dependency_id,
                    node_type=NodeType.CLAIM,
                    claim_type=ClaimType.LEMMA,
                    title="Needed lemma",
                    body="## Exact statement\n\nThe needed intermediate statement.",
                    epistemic_status=EpistemicStatus.CANDIDATE,
                ),
                GraphNodeCreate(
                    matek_id=dependency_proof_id,
                    node_type=NodeType.PROOF,
                    title="Proof of needed lemma",
                    body="## Proof content\n\nA proof of the needed lemma.",
                    epistemic_status=EpistemicStatus.CANDIDATE,
                ),
                GraphNodeCreate(
                    matek_id=source_id,
                    node_type=NodeType.SOURCE,
                    title="Needed imported result",
                    body="## Source record\n\nVerified source metadata.",
                    epistemic_status=EpistemicStatus.CANDIDATE,
                ),
                GraphNodeCreate(
                    matek_id=approach_id,
                    node_type=NodeType.APPROACH,
                    title="Unused alternative",
                    body="## Route\n\nAn unrelated route.",
                ),
            ],
            add_edges=[
                GraphEdge(
                    source_id=target_id,
                    relation=RelationType.DEPENDS_ON,
                    target_id=dependency_id,
                ),
                GraphEdge(
                    source_id=dependency_proof_id,
                    relation=RelationType.PROVES,
                    target_id=dependency_id,
                ),
                GraphEdge(
                    source_id=dependency_proof_id,
                    relation=RelationType.CITES,
                    target_id=source_id,
                ),
            ],
        ),
        problem_id=problem_id,
        operation_id="main-support-fixture",
    )
    assert merged.committed

    result = graph.record_research_result(
        problem_id=problem_id,
        run_id="run-one",
        research_result={
            "outcome": "accepted",
            "acceptance_gate": {"accepted": True},
            "candidate": {
                "exact_theorem": "For every test object, the desired property holds.",
                "full_proof": "Apply the needed lemma.",
                "unresolved_items": [],
                "quantitative_or_algorithmic": False,
            },
            "audits": {},
        },
    )
    assert result.committed
    accepted_proof_id = next(
        node.matek_id for node in graph.load_nodes() if node.title == "Accepted candidate proof"
    )
    needed_ids = {target_id, accepted_proof_id, dependency_id, dependency_proof_id, source_id}
    by_id = {node.matek_id: node for node in graph.load_nodes()}
    assert all("MAIN_RESULT_NEEDS" in by_id[node_id].tags for node_id in needed_ids)
    assert "MAIN_RESULT_NEEDS" not in by_id[approach_id].tags
    assert all(
        by_id[node_id].metadata["matek_main_result_needs"] == "run-one" for node_id in needed_ids
    )

    dashboard = graph.vault_root / "Dashboards" / "Main Result Needs.md"
    assert dashboard.is_file()
    dashboard_text = dashboard.read_text(encoding="utf-8")
    assert "Needed lemma" in dashboard_text
    assert "Unused alternative" not in dashboard_text
    canvas = json.loads(
        (graph.vault_root / "Dashboards" / "Main Proof Architecture.canvas").read_text(
            encoding="utf-8"
        )
    )
    assert {node["id"] for node in canvas["nodes"]} == needed_ids


def test_main_result_support_is_not_marked_without_a_passing_acceptance_gate(
    tmp_path: Path,
) -> None:
    graph, _, problem_id, _ = initialized_graph(tmp_path)
    graph.record_research_result(
        problem_id=problem_id,
        run_id="run-one",
        research_result={
            "outcome": "accepted",
            "acceptance_gate": {"accepted": False},
            "candidate": {
                "exact_theorem": "For every test object, the desired property holds.",
                "full_proof": "An unaudited argument.",
                "unresolved_items": [],
                "quantitative_or_algorithmic": False,
            },
            "audits": {},
        },
    )
    assert all("MAIN_RESULT_NEEDS" not in node.tags for node in graph.load_nodes())


def test_optimistic_conflict_detection_and_dependency_invalidation(tmp_path: Path) -> None:
    graph, _, problem_id, _ = initialized_graph(tmp_path)
    task_id, revision = graph_task(graph, problem_id)
    dependency_id = "CLM-DEPEND01"
    downstream_id = "CLM-DOWNSTR1"
    created = graph.merge_patch(
        GraphPatch(
            base_graph_revision=revision,
            run_id="run-one",
            task_id=task_id,
            create_nodes=[
                GraphNodeCreate(
                    matek_id=dependency_id,
                    node_type=NodeType.CLAIM,
                    claim_type=ClaimType.LEMMA,
                    title="Dependency lemma",
                    body="## Exact statement\n\nVersion one.",
                    epistemic_status=EpistemicStatus.CANDIDATE,
                ),
                GraphNodeCreate(
                    matek_id=downstream_id,
                    node_type=NodeType.CLAIM,
                    claim_type=ClaimType.THEOREM,
                    title="Downstream theorem",
                    body="## Exact statement\n\nUses the dependency.",
                    epistemic_status=EpistemicStatus.CANDIDATE,
                ),
            ],
            add_edges=[
                GraphEdge(
                    source_id=downstream_id,
                    relation=RelationType.DEPENDS_ON,
                    target_id=dependency_id,
                )
            ],
        ),
        problem_id=problem_id,
        operation_id="dependency-create",
    )
    assert created.committed
    dependency = graph.show(dependency_id)
    assert dependency.content_hash is not None
    base = graph.load_state().revision
    first = GraphPatch(
        base_graph_revision=base,
        run_id="run-one",
        task_id=task_id,
        update_nodes=[
            GraphNodeUpdate(
                matek_id=dependency_id,
                body="## Exact statement\n\nVersion two.",
                reason="Strengthen the exact dependency statement.",
            )
        ],
    )
    second = first.model_copy(deep=True)
    assert graph.merge_patch(first, problem_id=problem_id, operation_id="edit-one").committed
    stale = graph.show(downstream_id)
    assert stale.epistemic_status is EpistemicStatus.STALE
    assert any("dependency_changed" in item for item in stale.invalidation_reasons)
    conflict = graph.merge_patch(second, problem_id=problem_id, operation_id="edit-two")
    assert conflict.status == "conflict"


def test_main_target_edits_and_machine_frontmatter_changes_are_rejected(
    tmp_path: Path,
) -> None:
    graph, _, problem_id, _ = initialized_graph(tmp_path)
    target_id = graph.main_claim_id(problem_id)
    target = graph.show(target_id)
    assert target.path is not None
    note_path = graph.vault_root / target.path
    renamed_path = note_path.with_name(f"{target_id}--human-readable-title.md")
    note_path.rename(renamed_path)
    note_path = renamed_path
    original = note_path.read_text(encoding="utf-8")
    note_path.write_text(
        original + "\n## Human notes\n\nKeep this observation.\n",
        encoding="utf-8",
    )
    result = graph.reconcile_human_edits(run_id="human-note")
    assert result is not None
    unchanged = graph.show(target_id)
    assert unchanged.statement_version == 1
    assert "Keep this observation." in note_path.read_text(encoding="utf-8")

    before_forbidden_edit = note_path.read_text(encoding="utf-8")
    edited = before_forbidden_edit.replace(
        "For every test object, the desired property holds.",
        "For every nonempty test object, the desired property holds.",
    )
    note_path.write_text(edited, encoding="utf-8")

    with pytest.raises(GraphConflictError, match="immutable main target"):
        graph.reconcile_human_edits(run_id="human-edit")
    assert graph.show(target_id).statement_version == 1

    conflicted = before_forbidden_edit.replace(
        'workflow_status: "active"', 'workflow_status: "complete"'
    )
    note_path.write_text(conflicted, encoding="utf-8")
    report = graph.validate()
    assert not report.valid
    assert any(issue.code == "machine_field_changed" for issue in report.issues)
    with pytest.raises(GraphConflictError, match="machine-managed"):
        graph.reconcile_human_edits(run_id="conflicting-human-edit")


def test_lean_verification_is_bound_to_exact_claim_version(tmp_path: Path) -> None:
    graph, _, problem_id, _ = initialized_graph(tmp_path)
    statement_digest = "a" * 64
    source_digest = "b" * 64
    axiom_digest = "c" * 64
    merged = graph.record_lean_result(
        problem_id=problem_id,
        run_id="run-one",
        lean_result={
            "outcome": "LEAN_VERIFIED",
            "approved_statement_hash": statement_digest,
            "statement_draft": {"theorem_name": "matek_main"},
            "alignment": {"status": "aligned"},
            "verification": {
                "passed": True,
                "statement_hash_expected": statement_digest,
                "statement_hash_actual": statement_digest,
                "used_axioms": [],
            },
        },
        lean_toolchain="leanprover/lean4:v4.21.0",
        mathlib_revision="0123456789abcdef",
        source_file_hash=source_digest,
        axiom_report_hash=axiom_digest,
    )
    assert merged.committed
    claim = graph.show(graph.main_claim_id(problem_id))
    assert claim.epistemic_status is EpistemicStatus.LEAN_VERIFIED
    assert claim.metadata["matek_lean_statement_version"] == 1
    formalizations = [
        node for node in graph.load_nodes() if node.node_type is NodeType.FORMALIZATION
    ]
    assert len(formalizations) == 1
    formalization = formalizations[0]
    assert formalization.metadata == {
        "matek_claim_id": claim.matek_id,
        "matek_statement_version": 1,
        "matek_statement_hash": statement_digest,
        "matek_lean_declaration": "matek_main",
        "matek_source_file_hash": source_digest,
        "matek_lean_version": "leanprover/lean4:v4.21.0",
        "matek_mathlib_revision": "0123456789abcdef",
        "matek_build_result": "LEAN_VERIFIED",
        "matek_axiom_report_hash": axiom_digest,
        "matek_deterministic_verification_passed": True,
    }


def test_interrupted_multi_note_commit_recovers_from_write_ahead_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph, _, problem_id, _ = initialized_graph(tmp_path)
    task_id, revision = graph_task(graph, problem_id)
    from matek_theorem_agent.knowledge_graph import service as graph_service

    original_atomic_write_json = graph_service.atomic_write_json
    crashed = False

    def crash_once(path: Path, value: object, **kwargs: object) -> Path:
        nonlocal crashed
        if path == graph.state_path and graph.pending_path.is_file() and not crashed:
            crashed = True
            raise RuntimeError("simulated crash after note writes")
        return original_atomic_write_json(path, value, **kwargs)

    monkeypatch.setattr(graph_service, "atomic_write_json", crash_once)
    patch = GraphPatch(
        base_graph_revision=revision,
        run_id="run-one",
        task_id=task_id,
        create_nodes=[
            GraphNodeCreate(
                matek_id="CLM-RECOVER1",
                node_type=NodeType.CLAIM,
                claim_type=ClaimType.LEMMA,
                title="Recovered lemma",
                body="## Exact statement\n\nThis survives an interrupted commit.",
            )
        ],
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        graph.merge_patch(patch, problem_id=problem_id, operation_id="crash-recovery")
    assert graph.pending_path.is_file()

    recovered = graph.load_state()
    assert not graph.pending_path.exists()
    assert recovered.processed_operations["crash-recovery"].committed
    assert graph.show("CLM-RECOVER1").title == "Recovered lemma"
    assert graph.validate().valid


def test_graph_cli_operates_without_obsidian(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "cli-project"
    project.mkdir()
    (project / ".git").mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr("matek_theorem_agent.knowledge_graph.service.shutil.which", lambda _: None)
    cli = CliRunner()

    initialized = cli.invoke(app, ["init"])
    assert initialized.exit_code == 0, initialized.output
    assert not (project / ".matek" / "knowledge").exists()
    graph_initialized = cli.invoke(app, ["graph", "init", "problem"])
    assert graph_initialized.exit_code == 0, graph_initialized.output
    assert (project / ".matek" / "knowledge" / "problem" / "Home.md").is_file()
    validated = cli.invoke(app, ["graph", "validate"])
    assert validated.exit_code == 0, validated.output
    status = cli.invoke(app, ["graph", "status"])
    assert status.exit_code == 0
    assert '"node_count": 0' in status.output
    doctor = cli.invoke(app, ["graph", "doctor", "--repair"])
    assert doctor.exit_code == 0, doctor.output
    assert '"repair_requested": true' in doctor.output
    assert '"actions": []' in doctor.output
    exported = cli.invoke(app, ["graph", "export", "--format", "mermaid"])
    assert exported.exit_code == 0
    assert "flowchart TD" in exported.output
    opened = cli.invoke(app, ["graph", "open"])
    assert opened.exit_code == 0
    assert "Vault:" in opened.output
    assert "Obsidian unavailable" in opened.output


def test_problem_graph_names_create_isolated_vaults(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    first_problem = project / "First Result.md"
    second_problem = project / "second-problem.txt"
    first_problem.write_text("Prove the first result.\n", encoding="utf-8")
    second_problem.write_text("Prove the second result.\n", encoding="utf-8")

    first_name = problem_graph_name(first_problem)
    second_name = problem_graph_name(second_problem)
    first = KnowledgeGraph(project, first_name)
    second = KnowledgeGraph(project, second_name)
    first.initialize_problem(
        source_path=first_problem,
        problem_text=first_problem.read_text(encoding="utf-8"),
        run_id="run-first",
    )
    second.initialize_problem(
        source_path=second_problem,
        problem_text=second_problem.read_text(encoding="utf-8"),
        run_id="run-second",
    )

    assert first_name == "first-result"
    assert second_name == "second-problem"
    assert first.vault_root != second.vault_root
    assert first.index_path != second.index_path
    assert first.load_state().graph_name == first_name
    assert second.load_state().graph_name == second_name
    assert list_graph_names(project) == [first_name, second_name]
    assert len([node for node in first.load_nodes() if node.node_type is NodeType.PROBLEM]) == 1
    assert len([node for node in second.load_nodes() if node.node_type is NodeType.PROBLEM]) == 1


def test_graph_cli_requires_selection_when_multiple_graphs_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "cli-project"
    project.mkdir()
    (project / ".git").mkdir()
    monkeypatch.chdir(project)
    cli = CliRunner()

    assert cli.invoke(app, ["init"]).exit_code == 0
    assert cli.invoke(app, ["graph", "init", "alpha"]).exit_code == 0
    assert cli.invoke(app, ["graph", "init", "beta"]).exit_code == 0
    listed = cli.invoke(app, ["graph", "list"])
    assert listed.exit_code == 0
    assert '"name": "alpha"' in listed.output
    assert '"name": "beta"' in listed.output
    ambiguous = cli.invoke(app, ["graph", "status"])
    assert ambiguous.exit_code == 2
    assert "multiple knowledge graphs exist" in ambiguous.output
    selected = cli.invoke(app, ["graph", "status", "--knowledge-graph", "alpha"])
    assert selected.exit_code == 0
    assert '"graph_name": "alpha"' in selected.output

    follow_up = project / "follow-up.md"
    follow_up.write_text("Prove the follow-up theorem.\n", encoding="utf-8")
    reuse_plan = cli.invoke(
        app,
        ["run", str(follow_up), "--knowledge-graph", "alpha", "--dry-run"],
        terminal_width=240,
    )
    assert reuse_plan.exit_code == 0, reuse_plan.output
    assert "knowledge graph name" in reuse_plan.output
    assert "alpha" in reuse_plan.output
    assert "explicit existing graph" in reuse_plan.output
    missing = cli.invoke(
        app,
        ["run", str(follow_up), "--knowledge-graph", "missing", "--dry-run"],
    )
    assert missing.exit_code == 2
    assert "does not exist" in missing.output


def test_graph_doctor_renames_legacy_hash_ids_to_descriptive_one_liners(
    tmp_path: Path,
) -> None:
    graph, _problem, problem_id, _ = initialized_graph(tmp_path)
    main_target_id = graph.main_claim_id(problem_id)
    tasks, _, _ = graph.record_assignment_tasks(
        problem_id=problem_id,
        run_id="run-one",
        decision_id=7,
        assignments=[
            {
                "id": "worker-legacy",
                "approach_family": "symmetrization",
                "task": "Prove the centroid bound.",
                "expected_output": "An exact lemma and proof.",
                "target_node_ids": [main_target_id],
            }
        ],
    )
    merged = graph.merge_patch(
        GraphPatch(
            base_graph_revision=graph.load_state().revision,
            run_id="run-one",
            task_id=tasks["worker-legacy"],
            agent_role="research-auditor",
            create_nodes=[
                GraphNodeCreate(
                    matek_id="CLM-LEGACYCLAIM001",
                    node_type=NodeType.CLAIM,
                    claim_type=ClaimType.LEMMA,
                    title="Any halfspace through the centroid keeps at least 1/e of the volume",
                    body=(
                        "## Exact statement\n\nFor a convex body C of volume v with centroid c, "
                        "any halfspace H whose boundary contains c satisfies vol(H cap C) >= v/e."
                    ),
                ),
                GraphNodeCreate(
                    matek_id="APR-LEGACYAPR00001",
                    node_type=NodeType.APPROACH,
                    title="Symmetrization approach",
                    body=(
                        "## Exact route attempted\n\nUse Blaschke-Santalo symmetrization "
                        "to reduce to the ball."
                    ),
                ),
            ],
            add_edges=[
                GraphEdge(
                    source_id="CLM-LEGACYCLAIM001",
                    relation=RelationType.RELATED_TO,
                    target_id="APR-LEGACYAPR00001",
                )
            ],
        ),
        problem_id=problem_id,
        operation_id="legacy-nodes",
    )
    assert merged.committed

    planned = graph.doctor(problem_id=problem_id)
    assert [action.applied for action in planned.actions] == [False, False]
    planned_ids = {
        action.before["matek_id"]: action.after["matek_id"] for action in planned.actions
    }
    assert planned_ids == {
        "APR-LEGACYAPR00001": "APPROACH: Symmetrization approach",
        "CLM-LEGACYCLAIM001": (
            "CLAIM: Any halfspace through the centroid keeps at least 1/e of the volume"
        ),
    }
    assert graph.load_state().revision == planned.previous_revision

    repaired = graph.doctor(repair=True, problem_id=problem_id, run_id="run-doctor")
    assert all(action.applied for action in repaired.actions)
    assert repaired.repair_log is not None
    repair_log = json.loads((graph.graph_root / repaired.repair_log).read_text(encoding="utf-8"))
    assert [action["rule"] for action in repair_log["actions"]] == [
        "legacy_hash_id_rename",
        "legacy_hash_id_rename",
    ]

    nodes = {node.matek_id: node for node in graph.load_nodes(include_human_notes=False)}
    new_claim_id = planned_ids["CLM-LEGACYCLAIM001"]
    new_approach_id = planned_ids["APR-LEGACYAPR00001"]
    assert "CLM-LEGACYCLAIM001" not in nodes
    assert "APR-LEGACYAPR00001" not in nodes
    claim = nodes[new_claim_id]
    approach = nodes[new_approach_id]
    assert claim.metadata["matek_legacy_node_id"] == "CLM-LEGACYCLAIM001"
    assert approach.metadata["matek_legacy_node_id"] == "APR-LEGACYAPR00001"
    # References to renamed IDs are rewritten across the graph.
    assert [
        (edge.relation, edge.target_id) for edge in claim.relations
    ] == [(RelationType.RELATED_TO, new_approach_id)]
    # The immutable main target keeps its stable anchor ID.
    assert main_target_id in nodes
    # The rename is idempotent: a second inspection plans nothing.
    assert graph.doctor(problem_id=problem_id).actions == []
    assert graph.validate().valid
    # The canonical ledger still projects after the rename.
    state = graph.load_state()
    ledger = project_markdown_ledger(
        list(nodes.values()),
        graph_revision=state.revision,
        problem_id=problem_id,
        target_claim_id=main_target_id,
    )
    assert main_target_id in ledger.claims
