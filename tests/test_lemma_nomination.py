from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from matek_theorem_agent.knowledge_graph.admission import build_scientific_admission
from matek_theorem_agent.knowledge_graph.markdown import new_generated_body
from matek_theorem_agent.knowledge_graph.models import (
    ClaimType,
    EpistemicStatus,
    GraphEdge,
    GraphFrontier,
    GraphNode,
    GraphNodeSummary,
    NodeType,
    RelationType,
    WorkflowStatus,
)
from matek_theorem_agent.scientific import (
    BranchOutcome,
    ScientificObligationDeclaration,
    ScientificResult,
    ScientificResultDisposition,
    ScientificResultKind,
    ScientificScope,
)
from matek_theorem_agent.stages.lemma_audit import preflight_lemma_nomination
from matek_theorem_agent.stages.lemma_nomination import (
    LemmaNominationPolicy,
    LemmaNominationSkipCode,
    nominate_intermediate_lemmas,
)
from matek_theorem_agent.stages.research import ResearchWorkerReport

NOW = datetime(2026, 8, 3, tzinfo=UTC)
PROBLEM_ID = "PRB-PROBLEM1"
MAIN_ID = "CLM-TARGET001"
CUT_ID = "OBL-CUT00001"
CLAIM_ID = "CLM-LEMMA001"
DERIVATION_ID = "DRV-DERIVE01"
ATTEMPT_ID = "PAT-ATTEMPT1"
PREMISE_ID = "CLM-PREMISE1"
ASSIGNMENT_ID = "worker-one"
LOCAL_KEY = "cut-lemma"
REVISION = "00000042-aabbccddaabbccdd"
STATEMENT = "For every natural number n, Q(n)."
PROOF = "Fix arbitrary n. The premise gives Q(n), which is the required conclusion."


def _node(
    node_id: str,
    node_type: NodeType,
    *,
    statement: str,
    metadata: dict[str, str | int | bool | list[str] | None] | None = None,
    relations: list[GraphEdge] | None = None,
    evidence: list[str] | None = None,
) -> GraphNode:
    body = new_generated_body(
        node_id,
        (
            f"## Exact statement\n\n{statement}"
            if node_type in {NodeType.CLAIM, NodeType.OBLIGATION}
            else f"## Mathematical evidence\n\n{statement}"
        ),
    )
    content_hash = hashlib.sha256((node_id + body).encode()).hexdigest()
    return GraphNode(
        matek_id=node_id,
        node_type=node_type,
        problem_id=PROBLEM_ID,
        title=node_id,
        epistemic_status=(
            EpistemicStatus.AUDIT_PASSED if node_id == PREMISE_ID else EpistemicStatus.CANDIDATE
        ),
        workflow_status=WorkflowStatus.ACTIVE,
        claim_type=(ClaimType.THEOREM if node_type is NodeType.CLAIM else None),
        created_in_run="run-one",
        last_modified_run="run-one",
        created_at=NOW,
        updated_at=NOW,
        body=body,
        relations=relations or [],
        evidence=evidence or [],
        metadata=metadata or {},
        path=f"{node_id}.md",
        content_hash=content_hash,
    )


def _summary(node: GraphNode) -> GraphNodeSummary:
    assert node.path is not None
    return GraphNodeSummary(
        matek_id=node.matek_id,
        node_type=node.node_type,
        title=node.title,
        epistemic_status=node.epistemic_status,
        workflow_status=node.workflow_status,
        path=node.path,
        statement_version=node.statement_version,
    )


def _result(
    *,
    local_key: str = LOCAL_KEY,
    kind: ScientificResultKind = ScientificResultKind.LEMMA,
    scope: ScientificScope = ScientificScope.BRANCH,
    gap: str | None = None,
    targets: list[str] | None = None,
    disposition: ScientificResultDisposition | None = None,
) -> ScientificResult:
    return ScientificResult(
        local_key=local_key,
        kind=kind,
        exact_statement=(
            "Define Reachable(n) to mean that n is reachable from zero."
            if kind is ScientificResultKind.DEFINITION
            else STATEMENT
        ),
        scope=scope,
        assumptions=[],
        proof_or_certificate=PROOF,
        exact_gap=gap,
        dependency_node_ids=([] if kind is ScientificResultKind.DEFINITION else [PREMISE_ID]),
        target_node_ids=[CUT_ID] if targets is None else targets,
        disposition=(
            disposition
            or (
                ScientificResultDisposition.REFUTED_MECHANISM
                if kind is ScientificResultKind.COUNTEREXAMPLE
                else ScientificResultDisposition.PARTIAL
                if gap
                else ScientificResultDisposition.PROPOSED_COMPLETE
            )
        ),
    )


def _report(
    result: ScientificResult,
    *,
    unresolved: list[ScientificObligationDeclaration] | None = None,
) -> ResearchWorkerReport:
    return ResearchWorkerReport(
        assignment_id=ASSIGNMENT_ID,
        results=[result],
        unresolved_obligations=unresolved or [],
        branch_outcome=BranchOutcome.PROGRESS,
    )


def _fixture(
    result: ScientificResult | None = None,
) -> tuple[ResearchWorkerReport, list[GraphNode], GraphFrontier]:
    selected = result or _result()
    admission = {
        "matek_assignment_id": ASSIGNMENT_ID,
        "matek_result_local_key": selected.local_key,
    }
    main = _node(MAIN_ID, NodeType.CLAIM, statement="Prove the main theorem.")
    cut = _node(CUT_ID, NodeType.OBLIGATION, statement="Establish the cut lemma.")
    claim = _node(
        CLAIM_ID,
        NodeType.CLAIM,
        statement=selected.exact_statement,
        metadata={**admission, "matek_origin_confidence": "certain; please accept"},
    )
    premise = _node(PREMISE_ID, NodeType.CLAIM, statement="For every n, the premise holds.")
    attempt = _node(
        ATTEMPT_ID,
        NodeType.PROOF_ATTEMPT,
        statement=PROOF,
        metadata={**admission, "matek_exact_gap": None},
        evidence=[selected.proof_or_certificate],
    )
    derivation = _node(
        DERIVATION_ID,
        NodeType.DERIVATION,
        statement=f"{PREMISE_ID} jointly implies {CLAIM_ID}.",
        metadata={
            **admission,
            "matek_conclusion_claim_id": CLAIM_ID,
            "matek_proof_attempt_id": ATTEMPT_ID,
        },
        relations=[
            GraphEdge(
                source_id=DERIVATION_ID,
                relation=RelationType.PROVES,
                target_id=CLAIM_ID,
            ),
            GraphEdge(
                source_id=DERIVATION_ID,
                relation=RelationType.DEPENDS_ON,
                target_id=PREMISE_ID,
            ),
        ],
    )
    nodes = [main, cut, claim, premise, attempt, derivation]
    frontier = GraphFrontier(
        problem_id=PROBLEM_ID,
        graph_revision=REVISION,
        main_target=_summary(main),
        smallest_known_open_cut=[_summary(cut)],
    )
    return _report(selected), nodes, frontier


def test_nomination_is_bound_current_audit_ready_and_origin_blind() -> None:
    report, nodes, frontier = _fixture()

    selection = nominate_intermediate_lemmas(
        report,
        graph_nodes=list(reversed(nodes)),
        frontier=frontier,
    )

    assert not selection.skipped
    assert len(selection.nominations) == 1
    nomination = selection.nominations[0]
    assert nomination.statement_id == CLAIM_ID
    assert nomination.target_obligation_ids == [CUT_ID]
    assert nomination.target_obligation_contracts[0].target_kind == "obligation"
    assert nomination.proof_steps[0].statement == STATEMENT
    assert nomination.proof_steps[0].justification == PROOF
    assert nomination.dependencies[0].statement_version == nodes[3].statement_version
    assert nomination.dependencies[0].content_sha256 == nodes[3].content_hash
    assert nomination.dependencies[0].current_content_sha256 == nodes[3].content_hash
    assert all(not artifact.origin_annotations for artifact in nomination.source_artifacts)
    assert "certain; please accept" not in nomination.model_dump_json()
    assert preflight_lemma_nomination(nomination).accepted
    binding = selection.bindings[0]
    assert binding.assignment_id == ASSIGNMENT_ID
    assert binding.result_local_key == LOCAL_KEY
    assert binding.canonical_claim_id == CLAIM_ID
    assert binding.canonical_derivation_id == DERIVATION_ID
    assert binding.canonical_proof_attempt_id == ATTEMPT_ID


def test_non_main_claim_open_cut_gets_typed_relevance_contract() -> None:
    claim_cut_id = "CLM-CUTCLAIM1"
    report, nodes, frontier = _fixture(_result(targets=[claim_cut_id]))
    nodes = [node for node in nodes if node.matek_id != CUT_ID]
    claim_cut = _node(
        claim_cut_id,
        NodeType.CLAIM,
        statement="Establish the unresolved premise claim.",
    )
    nodes.append(claim_cut)
    frontier = frontier.model_copy(update={"smallest_known_open_cut": [_summary(claim_cut)]})

    selection = nominate_intermediate_lemmas(report, graph_nodes=nodes, frontier=frontier)

    assert not selection.skipped
    [nomination] = selection.nominations
    [contract] = nomination.target_obligation_contracts
    assert contract.target_kind == "claim"
    assert contract.obligation_id == claim_cut_id
    assert contract.exact_statement == contract.conclusion
    assert not contract.quantifiers
    assert not contract.hypotheses
    assert not contract.dependency_claim_ids
    assert not contract.target_claim_ids
    assert not contract.falsification_evidence
    assert contract.statement_version == claim_cut.statement_version
    assert contract.content_sha256 == claim_cut.content_hash
    assert preflight_lemma_nomination(nomination).accepted


def test_nonempty_unbound_assumptions_are_not_lemma_auditable() -> None:
    assumed = _result().model_copy(update={"assumptions": ["Q(n) is already known."]})
    report, nodes, frontier = _fixture(assumed)

    selection = nominate_intermediate_lemmas(report, graph_nodes=nodes, frontier=frontier)

    assert not selection.nominations
    assert selection.skipped[0].code is LemmaNominationSkipCode.ASSUMPTIONS_UNBOUND


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_result(scope=ScientificScope.MAIN), LemmaNominationSkipCode.MAIN_RESULT),
        (
            _result(kind=ScientificResultKind.COMPUTATION, scope=ScientificScope.COMPUTATION),
            LemmaNominationSkipCode.COMPUTATION,
        ),
        (_result(kind=ScientificResultKind.DEFINITION), LemmaNominationSkipCode.DEFINITION),
        (
            _result(kind=ScientificResultKind.COUNTEREXAMPLE),
            LemmaNominationSkipCode.COUNTEREXAMPLE,
        ),
        (_result(gap="Prove the induction step."), LemmaNominationSkipCode.GAPPED),
    ],
)
def test_ineligible_scientific_results_have_typed_skip(
    result: ScientificResult,
    expected: LemmaNominationSkipCode,
) -> None:
    report, nodes, frontier = _fixture(result)

    selection = nominate_intermediate_lemmas(report, graph_nodes=nodes, frontier=frontier)

    assert not selection.nominations
    assert selection.skipped[0].code is expected


def test_parent_obligation_prevents_nomination() -> None:
    result = _result()
    report, nodes, frontier = _fixture(result)
    report = _report(
        result,
        unresolved=[
            ScientificObligationDeclaration(
                local_key="missing-case",
                exact_statement="Prove the missing case.",
                conclusion="The missing case holds.",
                parent_result_keys=[LOCAL_KEY],
            )
        ],
    )

    selection = nominate_intermediate_lemmas(report, graph_nodes=nodes, frontier=frontier)

    assert not selection.nominations
    assert selection.skipped[0].code is LemmaNominationSkipCode.UNRESOLVED
    assert selection.skipped[0].references == ["missing-case"]


def test_ambiguous_admission_mapping_is_reported_without_guessing() -> None:
    report, nodes, frontier = _fixture()
    original = next(node for node in nodes if node.matek_id == DERIVATION_ID)
    duplicate = original.model_copy(
        update={
            "matek_id": "DRV-DERIVE02",
            "relations": [
                edge.model_copy(update={"source_id": "DRV-DERIVE02"}) for edge in original.relations
            ],
        }
    )

    selection = nominate_intermediate_lemmas(
        report,
        graph_nodes=[*nodes, duplicate],
        frontier=frontier,
    )

    assert not selection.nominations
    assert selection.skipped[0].code is LemmaNominationSkipCode.ADMISSION_AMBIGUOUS
    assert selection.skipped[0].references == [DERIVATION_ID, "DRV-DERIVE02"]


def test_only_explicit_members_of_exact_smallest_open_cut_are_relevant() -> None:
    result = _result(targets=[])
    report, nodes, frontier = _fixture(result)

    irrelevant = nominate_intermediate_lemmas(report, graph_nodes=nodes, frontier=frontier)
    capped = nominate_intermediate_lemmas(
        report,
        graph_nodes=nodes,
        frontier=frontier.model_copy(update={"open_cut_search_capped": True}),
    )

    assert irrelevant.skipped[0].code is LemmaNominationSkipCode.NOT_ON_OPEN_CUT
    assert capped.skipped[0].code is LemmaNominationSkipCode.OPEN_CUT_NOT_EXACT


def test_missing_current_dependency_hash_is_a_typed_skip() -> None:
    report, nodes, frontier = _fixture()
    premise_index = next(index for index, node in enumerate(nodes) if node.matek_id == PREMISE_ID)
    nodes[premise_index] = nodes[premise_index].model_copy(update={"content_hash": None})

    selection = nominate_intermediate_lemmas(report, graph_nodes=nodes, frontier=frontier)

    assert not selection.nominations
    assert selection.skipped[0].code is LemmaNominationSkipCode.DEPENDENCY_MISSING
    assert selection.skipped[0].references == [PREMISE_ID]


def test_order_is_deterministic_and_leverage_threshold_is_typed() -> None:
    report, nodes, frontier = _fixture()

    first = nominate_intermediate_lemmas(report, graph_nodes=nodes, frontier=frontier)
    second = nominate_intermediate_lemmas(
        report,
        graph_nodes=list(reversed(nodes)),
        frontier=frontier,
    )
    too_high = nominate_intermediate_lemmas(
        report,
        graph_nodes=nodes,
        frontier=frontier,
        policy=LemmaNominationPolicy(minimum_leverage_score=2),
    )

    assert first == second
    assert not too_high.nominations
    assert too_high.skipped[0].code is LemmaNominationSkipCode.LOW_LEVERAGE


def _replay_artifacts_for(local_key: str) -> list[GraphNode]:
    manifest_sha256 = "a" * 64
    replay_sha256 = "b" * 64

    def artifact_id(record_sha256: str, role: str) -> str:
        material = "\0".join([PROBLEM_ID, "run-one", ASSIGNMENT_ID, record_sha256, role])
        return f"ART-{hashlib.sha256(material.encode()).hexdigest().upper()[:20]}"

    manifest_id = artifact_id(manifest_sha256, "manifest")
    replay_id = artifact_id(replay_sha256, "replay")
    manifest = _node(manifest_id, NodeType.ARTIFACT, statement="Collected computation evidence.")
    manifest.author_role = "computation-collector"
    manifest.epistemic_status = EpistemicStatus.AUDIT_PASSED
    manifest.workflow_status = WorkflowStatus.COMPLETE
    manifest.source_artifacts = ["verified-computation-evidence.json"]
    manifest.metadata = {
        "matek_assignment_id": ASSIGNMENT_ID,
        "matek_computation_manifest_sha256": manifest_sha256,
        "matek_computation_replay_status": "passed",
        "matek_replay_passed": True,
        "matek_supporting_result_keys": [local_key],
    }
    replay = _node(replay_id, NodeType.ARTIFACT, statement="Independent replay evidence.")
    replay.author_role = "computation-replayer"
    replay.epistemic_status = EpistemicStatus.AUDIT_PASSED
    replay.workflow_status = WorkflowStatus.COMPLETE
    replay.source_artifacts = ["verified-computation-evidence.json"]
    replay.metadata = {
        "matek_assignment_id": ASSIGNMENT_ID,
        "matek_computation_manifest_sha256": manifest_sha256,
        "matek_computation_replay_record_sha256": replay_sha256,
        "matek_computation_replay_status": "passed",
        "matek_replay_passed": True,
        "matek_supporting_result_keys": [local_key],
    }
    replay.relations = [
        GraphEdge(
            source_id=replay_id,
            relation=RelationType.RELATED_TO,
            target_id=manifest_id,
        )
    ]
    return [manifest, replay]


def _persisted(node: GraphNode) -> GraphNode:
    node.path = f"{node.matek_id}.md"
    node.content_hash = hashlib.sha256((node.matek_id + node.body).encode()).hexdigest()
    return node


def test_same_report_replayed_computation_dependency_is_auditable_and_exact() -> None:
    computation = ScientificResult(
        local_key="enumeration",
        kind=ScientificResultKind.COMPUTATION,
        exact_statement="Exactly 17 normalized states occur.",
        scope=ScientificScope.COMPUTATION,
        proof_or_certificate="The persisted exhaustive enumeration reports 17 states.",
        disposition=ScientificResultDisposition.PROPOSED_COMPLETE,
    )
    reduction = ScientificResult(
        local_key="finite-reduction",
        kind=ScientificResultKind.REDUCTION,
        exact_statement="The branch theorem reduces to the 17 normalized states.",
        scope=ScientificScope.REDUCTION,
        proof_or_certificate="Normalize an arbitrary instance and apply the finite check.",
        dependency_result_keys=[computation.local_key],
        target_node_ids=[CUT_ID],
        disposition=ScientificResultDisposition.PROPOSED_COMPLETE,
    )
    main = _node(MAIN_ID, NodeType.CLAIM, statement="Prove the main theorem.")
    cut = _node(CUT_ID, NodeType.OBLIGATION, statement="Establish the finite reduction.")
    task = _node("TSK-ASSIGN01", NodeType.TASK, statement="Audit the branch.")
    approach = _node("APR-BRANCH01", NodeType.APPROACH, statement="Finite reduction branch.")
    run = _node("RUN-RUNNODE1", NodeType.RUN, statement="Scientific run.")
    replay_artifacts = _replay_artifacts_for(computation.local_key)
    base_nodes = [main, cut, task, approach, run, *replay_artifacts]
    plan = build_scientific_admission(
        existing_nodes=base_nodes,
        problem_id=PROBLEM_ID,
        main_target_id=MAIN_ID,
        run_id="run-one",
        assignment_id=ASSIGNMENT_ID,
        task_id=task.matek_id,
        approach_id=approach.matek_id,
        results=[computation, reduction],
        unresolved_obligations=[],
        source_artifact="workers/worker-one.json",
        now=NOW,
    )
    nodes = [_persisted(node) for node in [*base_nodes, *plan.nodes]]
    report = ResearchWorkerReport(
        assignment_id=ASSIGNMENT_ID,
        results=[computation, reduction],
        branch_outcome=BranchOutcome.PROGRESS,
    )
    frontier = GraphFrontier(
        problem_id=PROBLEM_ID,
        graph_revision=REVISION,
        main_target=_summary(main),
        smallest_known_open_cut=[_summary(cut)],
    )

    computation_derivation = next(
        node
        for node in plan.nodes
        if node.node_type is NodeType.DERIVATION
        and node.metadata["matek_result_local_key"] == computation.local_key
    )
    computation_claim_id = next(
        edge.target_id
        for edge in computation_derivation.relations
        if edge.relation is RelationType.PROVES
    )
    initially_skipped = nominate_intermediate_lemmas(report, graph_nodes=nodes, frontier=frontier)
    assert not initially_skipped.nominations
    assert any(
        item.result_local_key == reduction.local_key
        and item.code is LemmaNominationSkipCode.DEPENDENCY_UNTRUSTED
        for item in initially_skipped.skipped
    )

    computation_claim = next(node for node in nodes if node.matek_id == computation_claim_id)
    computation_claim.epistemic_status = EpistemicStatus.AUDIT_PASSED
    selection = nominate_intermediate_lemmas(report, graph_nodes=nodes, frontier=frontier)
    assert len(selection.nominations) == 1
    nomination = selection.nominations[0]
    assert [item.dependency_id for item in nomination.dependencies] == [computation_claim_id]
    assert preflight_lemma_nomination(nomination).accepted

    reduction_derivation_index = next(
        index
        for index, node in enumerate(nodes)
        if node.node_type is NodeType.DERIVATION
        and node.metadata.get("matek_result_local_key") == reduction.local_key
    )
    reduction_derivation = nodes[reduction_derivation_index]
    reduction_derivation.metadata["matek_premise_claim_ids"] = [PREMISE_ID]
    tampered = nominate_intermediate_lemmas(report, graph_nodes=nodes, frontier=frontier)
    assert not tampered.nominations
    assert any(
        item.result_local_key == reduction.local_key
        and item.code is LemmaNominationSkipCode.ADMISSION_MISMATCH
        for item in tampered.skipped
    )
