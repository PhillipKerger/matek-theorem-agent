from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from matek_theorem_agent.knowledge_graph.admission import (
    ScientificAdmissionError,
    build_scientific_admission,
    node_has_scientific_admission_binding,
)
from matek_theorem_agent.knowledge_graph.ledger import (
    ClaimStatus,
    DerivationStatus,
    logical_version,
    project_markdown_ledger,
    smallest_known_open_cut,
    trusted_claim_ids,
)
from matek_theorem_agent.knowledge_graph.markdown import exact_statement, new_generated_body
from matek_theorem_agent.knowledge_graph.models import (
    ClaimType,
    EpistemicStatus,
    GraphEdge,
    GraphNode,
    NodeType,
    RelationType,
    WorkflowStatus,
)
from matek_theorem_agent.scientific import (
    ScientificObligationDeclaration,
    ScientificResult,
    ScientificResultDisposition,
    ScientificResultKind,
    ScientificScope,
    normalize_exact_statement,
)

NOW = datetime(2026, 8, 3, tzinfo=UTC)
PROBLEM_ID = "PRB-PROBLEM1"
TARGET_ID = "CLM-TARGET001"
TASK_ID = "TSK-ASSIGN01"
APPROACH_ID = "APR-BRANCH01"
RUN_NODE_ID = "RUN-RUNNODE1"


def graph_node(node_id: str, node_type: NodeType, title: str) -> GraphNode:
    claim_type = ClaimType.THEOREM if node_type is NodeType.CLAIM else None
    body = (
        new_generated_body(title, "## Exact statement\n\nFor every n, P(n).")
        if node_type is NodeType.CLAIM
        else new_generated_body(title, "## Summary\n\nFixture graph node.")
    )
    return GraphNode(
        matek_id=node_id,
        node_type=node_type,
        problem_id=PROBLEM_ID,
        title=title,
        epistemic_status=EpistemicStatus.CONJECTURED,
        workflow_status=WorkflowStatus.ACTIVE,
        claim_type=claim_type,
        created_in_run="run-one",
        last_modified_run="run-one",
        created_at=NOW,
        updated_at=NOW,
        body=body,
    )


def existing_nodes() -> list[GraphNode]:
    return [
        graph_node(TARGET_ID, NodeType.CLAIM, "Main target"),
        graph_node(TASK_ID, NodeType.TASK, "Assignment"),
        graph_node(APPROACH_ID, NodeType.APPROACH, "Branch"),
        graph_node(RUN_NODE_ID, NodeType.RUN, "Run"),
    ]


def result(
    *,
    local_key: str = "lemma-one",
    kind: ScientificResultKind = ScientificResultKind.LEMMA,
    statement: str = "For every n, P(n).",
    scope: ScientificScope = ScientificScope.MAIN,
    gap: str | None = None,
    dependencies: list[str] | None = None,
    result_dependencies: list[str] | None = None,
    targets: list[str] | None = None,
    assumptions: list[str] | None = None,
    disposition: ScientificResultDisposition | None = None,
) -> ScientificResult:
    return ScientificResult(
        local_key=local_key,
        kind=kind,
        exact_statement=statement,
        scope=scope,
        proof_or_certificate="A detailed proof or checkable certificate.",
        exact_gap=gap,
        assumptions=assumptions or [],
        dependency_node_ids=dependencies or [],
        dependency_result_keys=result_dependencies or [],
        target_node_ids=targets or [],
        disposition=disposition
        or (
            ScientificResultDisposition.REFUTED_MECHANISM
            if kind is ScientificResultKind.COUNTEREXAMPLE
            else ScientificResultDisposition.PARTIAL
            if gap
            else ScientificResultDisposition.PROPOSED_COMPLETE
        ),
    )


def admit(
    reported: list[ScientificResult],
    *,
    nodes: list[GraphNode] | None = None,
    obligations: list[ScientificObligationDeclaration] | None = None,
):
    return build_scientific_admission(
        existing_nodes=nodes or existing_nodes(),
        problem_id=PROBLEM_ID,
        main_target_id=TARGET_ID,
        run_id="run-one",
        assignment_id="worker-one",
        task_id=TASK_ID,
        approach_id=APPROACH_ID,
        results=reported,
        unresolved_obligations=obligations or [],
        source_artifact=".matek/runs/run-one/research/workers/worker-one.json",
        now=NOW,
    )


def deterministic_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest().upper()[:20]
    return f"{prefix}-{digest}"


def verified_replay_nodes(*, supporting_keys: list[str]) -> list[GraphNode]:
    manifest_sha256 = "a" * 64
    replay_sha256 = "b" * 64
    manifest_id = deterministic_id(
        "ART",
        PROBLEM_ID,
        "run-one",
        "worker-one",
        manifest_sha256,
        "manifest",
    )
    replay_id = deterministic_id(
        "ART",
        PROBLEM_ID,
        "run-one",
        "worker-one",
        replay_sha256,
        "replay",
    )
    manifest = graph_node(manifest_id, NodeType.ARTIFACT, "Replay manifest")
    manifest.author_role = "computation-collector"
    manifest.epistemic_status = EpistemicStatus.AUDIT_PASSED
    manifest.workflow_status = WorkflowStatus.COMPLETE
    manifest.source_artifacts = ["verified-computation-evidence.json"]
    manifest.metadata = {
        "matek_assignment_id": "worker-one",
        "matek_computation_manifest_sha256": manifest_sha256,
        "matek_computation_replay_status": "passed",
        "matek_replay_passed": True,
        "matek_supporting_result_keys": supporting_keys,
    }
    replay = graph_node(replay_id, NodeType.ARTIFACT, "Independent replay")
    replay.author_role = "computation-replayer"
    replay.epistemic_status = EpistemicStatus.AUDIT_PASSED
    replay.workflow_status = WorkflowStatus.COMPLETE
    replay.source_artifacts = ["verified-computation-evidence.json"]
    replay.metadata = {
        "matek_assignment_id": "worker-one",
        "matek_computation_manifest_sha256": manifest_sha256,
        "matek_computation_replay_record_sha256": replay_sha256,
        "matek_computation_replay_status": "passed",
        "matek_replay_passed": True,
        "matek_supporting_result_keys": supporting_keys,
    }
    replay.relations = [
        GraphEdge(
            source_id=replay_id,
            relation=RelationType.RELATED_TO,
            target_id=manifest_id,
        )
    ]
    return [manifest, replay]


def test_gapped_result_is_attempt_and_obligation_never_derivation() -> None:
    plan = admit([result(gap="Prove the induction step for arbitrary n.")])

    assert {node.node_type for node in plan.nodes} == {
        NodeType.PROOF_ATTEMPT,
        NodeType.OBLIGATION,
    }
    assert not any(
        edge.relation is RelationType.PROVES for node in plan.nodes for edge in node.relations
    )
    assert not plan.records[0].canonical_ledger_admitted
    assert plan.records[0].blocking_obligation_ids


def test_assumed_result_is_archive_only_and_assumption_contracts_do_not_alias() -> None:
    statement = "Every integer is even."
    first = result(
        local_key="self-assumed",
        statement=statement,
        scope=ScientificScope.BRANCH,
        assumptions=[statement],
    )
    second = result(
        local_key="other-assumption",
        statement=statement,
        scope=ScientificScope.BRANCH,
        assumptions=["Every integer is divisible by two."],
    )
    plan = admit([first, second])

    claims = [node for node in plan.nodes if node.node_type is NodeType.CLAIM]
    attempts = [node for node in plan.nodes if node.node_type is NodeType.PROOF_ATTEMPT]
    obligations = [node for node in plan.nodes if node.node_type is NodeType.OBLIGATION]
    assert len(claims) == len(attempts) == len(obligations) == 2
    assert len({node.matek_id for node in claims}) == 2
    assert not any(node.node_type is NodeType.DERIVATION for node in plan.nodes)
    assert all(node.workflow_status is WorkflowStatus.BLOCKED for node in claims + attempts)
    assert all("matek/unbound-assumptions" in node.tags for node in obligations)
    assert all(not record.canonical_ledger_admitted for record in plan.records)

    # Even a bypassed/legacy status mutation cannot turn the conditional archive claim into
    # a trusted bare theorem during ledger projection.
    claims[0].epistemic_status = EpistemicStatus.AUDIT_PASSED

    ledger = project_markdown_ledger(
        [*existing_nodes(), *plan.nodes],
        graph_revision="00000001-0123456789abcdef",
        problem_id=PROBLEM_ID,
        target_claim_id=TARGET_ID,
    )
    assert {node.matek_id for node in claims}.isdisjoint(trusted_claim_ids(ledger))
    assert TARGET_ID not in trusted_claim_ids(ledger)
    assert ledger.claims[claims[0].matek_id].status is ClaimStatus.STALE
    assert any(
        ambiguity.source_node_id == claims[0].matek_id
        and ambiguity.code == "unbound_claim_assumptions"
        for ambiguity in ledger.ambiguities
    )


def test_partial_gap_free_result_is_archive_only_with_completion_obligation() -> None:
    plan = admit(
        [
            result(
                local_key="partial-without-exact-gap",
                statement="A promising but unfinished branch claim.",
                scope=ScientificScope.BRANCH,
                disposition=ScientificResultDisposition.PARTIAL,
            )
        ]
    )

    assert not any(node.node_type is NodeType.DERIVATION for node in plan.nodes)
    attempt = next(node for node in plan.nodes if node.node_type is NodeType.PROOF_ATTEMPT)
    obligation = next(node for node in plan.nodes if node.node_type is NodeType.OBLIGATION)
    assert attempt.workflow_status is WorkflowStatus.BLOCKED
    assert "matek/incomplete-result" in attempt.tags
    assert "matek/incomplete-result" in obligation.tags
    assert "partial disposition" in obligation.body
    assert not plan.records[0].canonical_ledger_admitted


def test_legacy_partial_derivation_is_excluded_from_canonical_projection() -> None:
    plan = admit(
        [
            result(
                local_key="initially-complete",
                statement="Every archive fixture has a witness.",
                scope=ScientificScope.BRANCH,
            )
        ]
    )
    derivation = next(node for node in plan.nodes if node.node_type is NodeType.DERIVATION)
    derivation.epistemic_status = EpistemicStatus.AUDIT_PASSED
    derivation.metadata["matek_scientific_disposition"] = ScientificResultDisposition.PARTIAL.value

    ledger = project_markdown_ledger(
        [*existing_nodes(), *plan.nodes],
        graph_revision="00000001-0123456789abcdef",
        problem_id=PROBLEM_ID,
        target_claim_id=TARGET_ID,
    )

    assert derivation.matek_id not in ledger.derivations
    assert any(
        ambiguity.source_node_id == derivation.matek_id
        and ambiguity.code == "archive_only_scientific_derivation"
        for ambiguity in ledger.ambiguities
    )


def test_branch_counterexample_cannot_refute_main_target_without_audit() -> None:
    plan = admit(
        [
            result(
                kind=ScientificResultKind.COUNTEREXAMPLE,
                scope=ScientificScope.BRANCH,
                targets=[TARGET_ID],
            )
        ]
    )
    counterexample = plan.nodes[0]

    assert counterexample.node_type is NodeType.COUNTEREXAMPLE
    assert not any(
        edge.relation is RelationType.REFUTES and edge.target_id == TARGET_ID
        for edge in counterexample.relations
    )
    assert any(edge.target_id == APPROACH_ID for edge in counterexample.relations)
    assert "confined to its branch" in plan.issues[0]


def test_unknown_dependency_fails_loudly() -> None:
    with pytest.raises(ScientificAdmissionError, match="unknown dependency"):
        admit([result(dependencies=["CLM-UNKNOWN1"])])


def test_gap_free_result_creates_versioned_derivation_with_real_premise_edge() -> None:
    premise = graph_node("CLM-PREMISE1", NodeType.CLAIM, "Audited premise")
    premise.epistemic_status = EpistemicStatus.AUDIT_PASSED
    plan = admit(
        [
            result(
                local_key="derived-lemma",
                statement="For every n, Q(n).",
                scope=ScientificScope.BRANCH,
                dependencies=[premise.matek_id],
            )
        ],
        nodes=[*existing_nodes(), premise],
    )
    derivation = next(node for node in plan.nodes if node.node_type is NodeType.DERIVATION)
    claim = next(node for node in plan.nodes if node.node_type is NodeType.CLAIM)

    assert claim.matek_id.startswith("CLAIM: ")
    assert any(
        edge.relation is RelationType.PROVES and edge.target_id == claim.matek_id
        for edge in derivation.relations
    )
    assert any(
        edge.relation is RelationType.DEPENDS_ON and edge.target_id == premise.matek_id
        for edge in derivation.relations
    )
    assert derivation.dependency_versions[0].startswith(f"{premise.matek_id}@")
    assert derivation.metadata["matek_premise_versions"] == [
        f"{premise.matek_id}={derivation.dependency_versions[0].partition('@')[2]}"
    ]
    assert plan.records[0].canonical_ledger_admitted


def test_changed_premise_stales_even_an_audited_admitted_derivation() -> None:
    premise = graph_node("CLM-PREMISE1", NodeType.CLAIM, "Audited premise")
    premise.epistemic_status = EpistemicStatus.AUDIT_PASSED
    plan = admit(
        [
            result(
                local_key="conditional-route",
                statement="For every n, Q(n).",
                scope=ScientificScope.BRANCH,
                dependencies=[premise.matek_id],
            )
        ],
        nodes=[*existing_nodes(), premise],
    )
    claim = next(node for node in plan.nodes if node.node_type is NodeType.CLAIM)
    derivation = next(node for node in plan.nodes if node.node_type is NodeType.DERIVATION)
    derivation.epistemic_status = EpistemicStatus.AUDIT_PASSED
    changed_premise = premise.model_copy(deep=True)
    changed_premise.body = new_generated_body(
        "Changed audited premise",
        "## Exact statement\n\nA materially changed premise.",
    )
    ledger = project_markdown_ledger(
        [*existing_nodes(), changed_premise, *plan.nodes],
        graph_revision="00000002-0123456789abcdef",
        problem_id=PROBLEM_ID,
        target_claim_id=TARGET_ID,
    )

    assert ledger.derivations[derivation.matek_id].status is DerivationStatus.STALE
    assert claim.matek_id not in trusted_claim_ids(ledger)


def test_admission_is_idempotent_and_payload_collision_is_rejected() -> None:
    original = result(gap="Prove the induction step.")
    first = admit([original])
    replay_nodes = [*existing_nodes(), *first.nodes]

    repeated = admit([original], nodes=replay_nodes)

    assert repeated.records[0].already_applied
    assert repeated.nodes == []
    altered = original.model_copy(update={"proof_or_certificate": "Different evidence."})
    with pytest.raises(ScientificAdmissionError, match="identity collision"):
        admit([altered], nodes=replay_nodes)


def test_unreplayed_computation_stays_outside_canonical_ledger() -> None:
    plan = admit(
        [
            result(
                local_key="enumeration",
                kind=ScientificResultKind.COMPUTATION,
                statement="Exactly 17 normalized states occur.",
                scope=ScientificScope.COMPUTATION,
            )
        ]
    )

    assert {node.node_type for node in plan.nodes} == {
        NodeType.EXPERIMENT,
        NodeType.OBLIGATION,
    }
    assert not plan.records[0].canonical_ledger_admitted
    assert "until replay passes" in plan.issues[0]


def test_verified_replayed_computation_may_enter_as_proposed_derivation() -> None:
    artifacts = verified_replay_nodes(supporting_keys=["enumeration"])
    plan = admit(
        [
            result(
                local_key="enumeration",
                kind=ScientificResultKind.COMPUTATION,
                statement="Exactly 17 normalized states occur.",
                scope=ScientificScope.COMPUTATION,
            )
        ],
        nodes=[*existing_nodes(), *artifacts],
    )

    assert any(node.node_type is NodeType.DERIVATION for node in plan.nodes)
    assert plan.records[0].canonical_ledger_admitted


def test_same_report_computation_dependency_resolves_to_canonical_premise() -> None:
    artifacts = verified_replay_nodes(supporting_keys=["enumeration"])
    plan = admit(
        [
            result(
                local_key="main-proof",
                statement="For every n, P(n).",
                scope=ScientificScope.MAIN,
                result_dependencies=["enumeration"],
            ),
            result(
                local_key="enumeration",
                kind=ScientificResultKind.COMPUTATION,
                statement="Exactly 17 normalized states occur.",
                scope=ScientificScope.COMPUTATION,
            ),
        ],
        nodes=[*existing_nodes(), *artifacts],
    )
    derivations = {
        str(node.metadata["matek_result_local_key"]): node
        for node in plan.nodes
        if node.node_type is NodeType.DERIVATION
    }
    computation_claim_id = next(
        edge.target_id
        for edge in derivations["enumeration"].relations
        if edge.relation is RelationType.PROVES
    )

    assert any(
        edge.relation is RelationType.DEPENDS_ON and edge.target_id == computation_claim_id
        for edge in derivations["main-proof"].relations
    )
    assert derivations["main-proof"].metadata["matek_premise_claim_ids"] == [computation_claim_id]


def test_exact_main_result_cannot_depend_on_its_own_canonical_claim() -> None:
    with pytest.raises(ScientificAdmissionError, match="own canonical conclusion"):
        admit(
            [
                result(
                    local_key="circular-main-proof",
                    statement="For every n, P(n).",
                    scope=ScientificScope.MAIN,
                    dependencies=[TARGET_ID],
                )
            ]
        )


def test_admitted_definition_is_a_versioned_trusted_ledger_premise() -> None:
    definition_statement = "Define R(n) to mean that n is reachable from zero."
    lemma_statement = "For every n, R(n) implies R(n)."
    plan = admit(
        [
            result(
                local_key="reachable-definition",
                kind=ScientificResultKind.DEFINITION,
                statement=definition_statement,
                scope=ScientificScope.BRANCH,
            ),
            result(
                local_key="definition-identity",
                kind=ScientificResultKind.LEMMA,
                statement=lemma_statement,
                scope=ScientificScope.BRANCH,
                result_dependencies=["reachable-definition"],
            ),
        ]
    )
    definition = next(node for node in plan.nodes if node.node_type is NodeType.DEFINITION)
    derivation = next(
        node
        for node in plan.nodes
        if node.node_type is NodeType.DERIVATION
        and node.metadata["matek_result_local_key"] == "definition-identity"
    )

    projected = project_markdown_ledger(
        [*existing_nodes(), *plan.nodes],
        graph_revision="00000001-0123456789abcdef",
        problem_id=PROBLEM_ID,
        target_claim_id=TARGET_ID,
    )

    assert definition.matek_id in projected.claims
    definition_premise = projected.claims[definition.matek_id]
    assert definition_premise.status is ClaimStatus.AUDIT_PASSED
    assert definition_premise.logical_version == logical_version(definition_statement)
    assert projected.derivations[derivation.matek_id].premise_claim_ids == [definition.matek_id]
    assert not any(
        ambiguity.source_node_id == derivation.matek_id
        and ambiguity.code == "unknown_derivation_claim"
        for ambiguity in projected.ambiguities
    )

    dependency_laundered_definition = definition.model_copy(deep=True)
    dependency_laundered_definition.relations.append(
        GraphEdge(
            source_id=definition.matek_id,
            relation=RelationType.DEPENDS_ON,
            target_id=TARGET_ID,
        )
    )
    dependency_laundered_definition.dependency_versions = [
        f"{TARGET_ID}@{logical_version('For every n, P(n).')}"
    ]
    tampered_projection = project_markdown_ledger(
        [
            *existing_nodes(),
            *(
                dependency_laundered_definition if node.matek_id == definition.matek_id else node
                for node in plan.nodes
            ),
        ],
        graph_revision="00000002-0123456789abcdef",
        problem_id=PROBLEM_ID,
        target_claim_id=TARGET_ID,
    )
    assert definition.matek_id not in trusted_claim_ids(tampered_projection)
    assert any(
        ambiguity.source_node_id == definition.matek_id
        and ambiguity.code == "unadmitted_definition"
        for ambiguity in tampered_projection.ambiguities
    )

    assumption_laundered_definition = definition.model_copy(deep=True)
    assumption_laundered_definition.metadata["matek_normalized_assumptions"] = [
        "assume the notation already has the required meaning"
    ]
    assumption_projection = project_markdown_ledger(
        [
            *existing_nodes(),
            *(
                assumption_laundered_definition if node.matek_id == definition.matek_id else node
                for node in plan.nodes
            ),
        ],
        graph_revision="00000003-0123456789abcdef",
        problem_id=PROBLEM_ID,
        target_claim_id=TARGET_ID,
    )
    assert definition.matek_id not in trusted_claim_ids(assumption_projection)
    assert any(
        ambiguity.source_node_id == definition.matek_id
        and ambiguity.code == "unadmitted_definition"
        for ambiguity in assumption_projection.ambiguities
    )


def test_definition_dependency_cycle_is_rejected_even_after_model_copy_bypass() -> None:
    statement = "Define R(n) to mean that n is reachable from zero."
    first = admit(
        [
            result(
                local_key="reachable-definition-one",
                kind=ScientificResultKind.DEFINITION,
                statement=statement,
                scope=ScientificScope.BRANCH,
            )
        ]
    )
    second = result(
        local_key="reachable-definition-two",
        kind=ScientificResultKind.DEFINITION,
        statement=statement,
        scope=ScientificScope.BRANCH,
    ).model_copy(update={"dependency_node_ids": [TARGET_ID]})
    with pytest.raises(ScientificAdmissionError, match="dependency-free"):
        build_scientific_admission(
            existing_nodes=[*existing_nodes(), *first.nodes],
            problem_id=PROBLEM_ID,
            main_target_id=TARGET_ID,
            run_id="run-one",
            assignment_id="worker-two",
            task_id=TASK_ID,
            approach_id=APPROACH_ID,
            results=[second],
            unresolved_obligations=[],
            source_artifact=".matek/runs/run-one/research/workers/worker-two.json",
            now=NOW,
        )


def test_same_report_exact_aliases_preserve_every_definition_and_claim_binding() -> None:
    definition_statement = "Define R(n) to mean that n is reachable from zero."
    claim_statement = "For every n, R(n) implies R(n)."
    results = [
        result(
            local_key="definition-one",
            kind=ScientificResultKind.DEFINITION,
            statement=definition_statement,
            scope=ScientificScope.BRANCH,
            targets=[TARGET_ID],
        ),
        result(
            local_key="definition-two",
            kind=ScientificResultKind.DEFINITION,
            statement=definition_statement,
            scope=ScientificScope.BRANCH,
            targets=[TASK_ID],
        ),
        result(
            local_key="claim-one",
            statement=claim_statement,
            scope=ScientificScope.BRANCH,
            targets=[TARGET_ID],
        ),
        result(
            local_key="claim-two",
            statement=claim_statement,
            scope=ScientificScope.BRANCH,
            targets=[TASK_ID],
        ),
    ]
    first = admit(results)
    definitions = [node for node in first.nodes if node.node_type is NodeType.DEFINITION]
    claims = [
        node
        for node in first.nodes
        if node.node_type is NodeType.CLAIM
        and normalize_exact_statement(exact_statement(node.body))
        == normalize_exact_statement(claim_statement)
    ]
    assert len(definitions) == len(claims) == 1
    for shared in [*definitions, *claims]:
        bindings = shared.metadata.get("matek_admission_bindings")
        assert isinstance(bindings, list)
        assert len(bindings) == 2
        assert set(shared.metadata["matek_target_node_ids"]) == {TARGET_ID, TASK_ID}
        assert {
            edge.target_id for edge in shared.relations if edge.relation is RelationType.RELATED_TO
        } == {TARGET_ID, TASK_ID}
    assert set(claims[0].metadata["matek_result_aliases"]) == {
        "run-one:worker-one:claim-one",
        "run-one:worker-one:claim-two",
    }
    for item in results:
        shared = definitions[0] if item.kind is ScientificResultKind.DEFINITION else claims[0]
        assert node_has_scientific_admission_binding(
            shared,
            run_id="run-one",
            assignment_id="worker-one",
            result=item,
        )
    assert len([node for node in first.nodes if node.node_type is NodeType.PROOF_ATTEMPT]) == 2
    assert len([node for node in first.nodes if node.node_type is NodeType.DERIVATION]) == 2

    resumed = admit(results, nodes=[*existing_nodes(), *first.nodes])
    assert resumed.nodes == []
    assert all(record.already_applied for record in resumed.records)


def test_theorem_text_cannot_launder_itself_as_a_definition_or_main_premise() -> None:
    with pytest.raises(ValueError, match="explicit definitional declaration"):
        ScientificResult(
            local_key="laundered-theorem",
            kind=ScientificResultKind.DEFINITION,
            exact_statement="For every n, P(n).",
            scope=ScientificScope.BRANCH,
            proof_or_certificate="The worker calls this notation.",
            disposition=ScientificResultDisposition.PROPOSED_COMPLETE,
        )
    with pytest.raises(ValueError, match="branch-scoped"):
        ScientificResult(
            local_key="laundered-main-definition",
            kind=ScientificResultKind.DEFINITION,
            exact_statement="Define P(n) to mean that n has property P.",
            scope=ScientificScope.MAIN,
            proof_or_certificate="A purported main-target definition.",
            disposition=ScientificResultDisposition.PROPOSED_COMPLETE,
        )
    with pytest.raises(ValueError, match="cannot carry unbound"):
        ScientificResult(
            local_key="assumed-definition",
            kind=ScientificResultKind.DEFINITION,
            exact_statement="Define P(n) to mean that n has property P.",
            scope=ScientificScope.BRANCH,
            assumptions=["P(n) holds."],
            proof_or_certificate="A purported conditional definition.",
            disposition=ScientificResultDisposition.PROPOSED_COMPLETE,
        )

    # Admission rechecks the declaration contract even for a validation-bypassing in-memory
    # model copy or an older persisted object.
    bypassed = result(
        local_key="laundered-theorem",
        statement="For every n, P(n).",
        scope=ScientificScope.BRANCH,
    ).model_copy(update={"kind": ScientificResultKind.DEFINITION})
    with pytest.raises(ScientificAdmissionError, match="explicit branch-scoped"):
        admit([bypassed])


def test_incomplete_definition_cannot_enter_scientific_admission() -> None:
    with pytest.raises(ScientificAdmissionError, match="gap-free proposed-complete"):
        admit(
            [
                result(
                    local_key="unfinished-definition",
                    kind=ScientificResultKind.DEFINITION,
                    statement="Define R(n) to mean that n satisfies the recursive predicate.",
                    scope=ScientificScope.BRANCH,
                    gap="Specify R on the zero boundary.",
                )
            ]
        )

    bypassed_assumption = result(
        local_key="assumed-definition-bypass",
        kind=ScientificResultKind.DEFINITION,
        statement="Define S(n) to mean that n satisfies the symmetric predicate.",
        scope=ScientificScope.BRANCH,
    ).model_copy(update={"assumptions": ["S(n) already has the desired meaning."]})
    with pytest.raises(ScientificAdmissionError, match="proposed-complete declarations"):
        admit([bypassed_assumption])


def test_same_report_unreplayed_computation_cannot_be_a_proof_premise() -> None:
    with pytest.raises(ScientificAdmissionError, match="replay did not pass"):
        admit(
            [
                result(
                    local_key="main-proof",
                    result_dependencies=["enumeration"],
                ),
                result(
                    local_key="enumeration",
                    kind=ScientificResultKind.COMPUTATION,
                    statement="Exactly 17 normalized states occur.",
                    scope=ScientificScope.COMPUTATION,
                ),
            ]
        )


def test_replay_pair_cannot_be_reused_for_an_undeclared_result_key() -> None:
    artifacts = verified_replay_nodes(supporting_keys=["different-result"])
    plan = admit(
        [
            result(
                local_key="enumeration",
                kind=ScientificResultKind.COMPUTATION,
                statement="Exactly 17 normalized states occur.",
                scope=ScientificScope.COMPUTATION,
            )
        ],
        nodes=[*existing_nodes(), *artifacts],
    )

    assert {node.node_type for node in plan.nodes} == {
        NodeType.EXPERIMENT,
        NodeType.OBLIGATION,
    }
    assert not plan.records[0].canonical_ledger_admitted


@pytest.mark.parametrize(
    ("kind", "gap"),
    [
        (ScientificResultKind.DEFINITION, None),
        (ScientificResultKind.LEMMA, "Discharge the exact remaining case."),
        (ScientificResultKind.REDUCTION, None),
        (ScientificResultKind.COUNTEREXAMPLE, None),
        (ScientificResultKind.COMPUTATION, None),
        (ScientificResultKind.SOURCE_FACT, None),
    ],
)
def test_every_scientific_result_node_preserves_declared_target_ids(
    kind: ScientificResultKind,
    gap: str | None,
) -> None:
    plan = admit(
        [
            result(
                kind=kind,
                statement=(
                    "Define FixturePredicate(n) to mean that n is a fixture object."
                    if kind is ScientificResultKind.DEFINITION
                    else f"Exact {kind.value} fixture statement."
                ),
                scope=(
                    ScientificScope.COMPUTATION
                    if kind is ScientificResultKind.COMPUTATION
                    else ScientificScope.BRANCH
                ),
                gap=gap,
                targets=[TARGET_ID],
            )
        ]
    )

    assert plan.nodes
    assert all(node.metadata.get("matek_target_node_ids") == [TARGET_ID] for node in plan.nodes)


def test_reused_canonical_claim_accumulates_each_results_declared_targets() -> None:
    statement = "Every branch fixture has a canonical witness."
    first = admit(
        [
            result(
                local_key="first-route",
                statement=statement,
                scope=ScientificScope.BRANCH,
                targets=[TARGET_ID],
            )
        ]
    )
    first_claim = next(node for node in first.nodes if node.node_type is NodeType.CLAIM)

    second = build_scientific_admission(
        existing_nodes=[*existing_nodes(), *first.nodes],
        problem_id=PROBLEM_ID,
        main_target_id=TARGET_ID,
        run_id="run-one",
        assignment_id="worker-two",
        task_id=TASK_ID,
        approach_id=APPROACH_ID,
        results=[
            result(
                local_key="second-route",
                statement=statement,
                scope=ScientificScope.BRANCH,
                targets=[APPROACH_ID],
            )
        ],
        unresolved_obligations=[],
        source_artifact=".matek/runs/run-one/research/workers/worker-two.json",
        now=NOW,
    )
    reused_claim = next(
        node
        for node in second.nodes
        if node.node_type is NodeType.CLAIM and node.matek_id == first_claim.matek_id
    )

    assert reused_claim.metadata["matek_target_node_ids"] == [TARGET_ID, APPROACH_ID]
    assert {
        edge.target_id
        for edge in reused_claim.relations
        if edge.relation is RelationType.RELATED_TO
    }.issuperset({TARGET_ID, APPROACH_ID})


def test_explicit_obligation_preserves_attempt_and_derivation_parents() -> None:
    declaration = ScientificObligationDeclaration(
        local_key="boundary-case",
        exact_statement="Prove the boundary case used by the proposed route.",
        conclusion="The boundary case holds.",
        parent_result_keys=["lemma-one"],
        scope=ScientificScope.MAIN,
        estimated_leverage=95,
    )
    plan = admit([result()], obligations=[declaration])
    attempt = next(node for node in plan.nodes if node.node_type is NodeType.PROOF_ATTEMPT)
    derivation = next(node for node in plan.nodes if node.node_type is NodeType.DERIVATION)
    obligation = next(node for node in plan.nodes if node.node_type is NodeType.OBLIGATION)

    assert obligation.metadata["matek_parent_node_ids"] == [
        attempt.matek_id,
        derivation.matek_id,
    ]
    assert obligation.metadata["matek_parent_proof_attempt_ids"] == [attempt.matek_id]
    assert obligation.metadata["matek_parent_derivation_ids"] == [derivation.matek_id]
    assert {
        edge.target_id for edge in obligation.relations if edge.relation is RelationType.BLOCKS
    } == {attempt.matek_id, derivation.matek_id}
    assert any(
        edge.relation is RelationType.BLOCKED_BY and edge.target_id == obligation.matek_id
        for edge in derivation.relations
    )
    assert plan.records[0].blocking_obligation_ids == [obligation.matek_id]
    assert not plan.records[0].canonical_ledger_admitted

    ledger = project_markdown_ledger(
        [*existing_nodes(), *plan.nodes],
        graph_revision="00000001-0123456789abcdef",
        problem_id=PROBLEM_ID,
        target_claim_id=TARGET_ID,
    )
    assert smallest_known_open_cut(ledger).obligation_ids == sorted(
        [derivation.matek_id, obligation.matek_id]
    )


def test_result_with_explicit_obligation_is_idempotently_resumable() -> None:
    declaration = ScientificObligationDeclaration(
        local_key="boundary-case",
        exact_statement="Prove the boundary case used by the proposed route.",
        conclusion="The boundary case holds.",
        parent_result_keys=["lemma-one"],
        scope=ScientificScope.MAIN,
    )
    first = admit([result()], obligations=[declaration])
    obligation = next(node for node in first.nodes if node.node_type is NodeType.OBLIGATION)

    repeated = admit(
        [result()],
        obligations=[declaration],
        nodes=[*existing_nodes(), *first.nodes],
    )

    assert repeated.nodes == []
    assert repeated.records[0].already_applied
    assert repeated.records[0].blocking_obligation_ids == [obligation.matek_id]

    altered = declaration.model_copy(
        update={"exact_statement": "A materially different boundary obligation."}
    )
    with pytest.raises(ScientificAdmissionError, match="obligation admission identity collision"):
        admit(
            [result()],
            obligations=[altered],
            nodes=[*existing_nodes(), *first.nodes],
        )


def test_standalone_main_obligation_targets_frozen_claim_and_is_the_open_cut() -> None:
    declaration = ScientificObligationDeclaration(
        local_key="global-boundary",
        exact_statement="Prove the remaining global boundary case.",
        conclusion="The frozen main theorem follows at the boundary.",
        scope=ScientificScope.MAIN,
        estimated_leverage=100,
    )
    plan = admit([], obligations=[declaration])
    obligation = next(node for node in plan.nodes if node.node_type is NodeType.OBLIGATION)

    assert obligation.metadata["matek_target_claim_ids"] == [TARGET_ID]
    assert any(
        edge.relation is RelationType.TARGETS and edge.target_id == TARGET_ID
        for edge in obligation.relations
    )
    ledger = project_markdown_ledger(
        [*existing_nodes(), *plan.nodes],
        graph_revision="00000001-0123456789abcdef",
        problem_id=PROBLEM_ID,
        target_claim_id=TARGET_ID,
    )
    assert smallest_known_open_cut(ledger).obligation_ids == [obligation.matek_id]


def test_automatic_exact_gap_is_the_canonical_main_open_cut() -> None:
    plan = admit([result(gap="Prove the induction step for arbitrary n.")])
    obligation = next(node for node in plan.nodes if node.node_type is NodeType.OBLIGATION)
    ledger = project_markdown_ledger(
        [*existing_nodes(), *plan.nodes],
        graph_revision="00000001-0123456789abcdef",
        problem_id=PROBLEM_ID,
        target_claim_id=TARGET_ID,
    )

    assert smallest_known_open_cut(ledger).obligation_ids == [obligation.matek_id]


CENTROID_STATEMENT = (
    "For every convex body C, vol(H cap C) >= v/e when bd H contains the centroid."
)


def _result_with_one_liner(
    *,
    local_key: str,
    statement: str,
    one_liner: str,
    scope: ScientificScope = ScientificScope.BRANCH,
) -> ScientificResult:
    return ScientificResult(
        local_key=local_key,
        kind=ScientificResultKind.LEMMA,
        exact_statement=statement,
        scope=scope,
        one_liner=one_liner,
        proof_or_certificate="A complete, checkable proof.",
        disposition=ScientificResultDisposition.PROPOSED_COMPLETE,
    )


def test_agent_one_liner_becomes_the_descriptive_node_ids() -> None:
    plan = admit(
        [
            _result_with_one_liner(
                local_key="centroid-lemma",
                statement=CENTROID_STATEMENT,
                one_liner="Halfspaces through the centroid keep at least a 1/e volume fraction",
            )
        ]
    )
    claim = next(node for node in plan.nodes if node.node_type is NodeType.CLAIM)
    derivation = next(node for node in plan.nodes if node.node_type is NodeType.DERIVATION)
    attempt = next(node for node in plan.nodes if node.node_type is NodeType.PROOF_ATTEMPT)

    assert claim.matek_id == (
        "CLAIM: Halfspaces through the centroid keep at least a 1/e volume fraction"
    )
    assert claim.title == "Halfspaces through the centroid keep at least a 1/e volume fraction"
    assert derivation.matek_id.startswith("DERIVATION: Halfspaces through the centroid")
    assert attempt.matek_id.startswith("PROOF ATTEMPT: Halfspaces through the centroid")
    assert claim.matek_id in {
        edge.target_id for edge in derivation.relations if edge.relation is RelationType.PROVES
    }


def test_identical_statement_with_new_one_liner_coalesces_onto_existing_claim() -> None:
    statement = CENTROID_STATEMENT
    first = admit(
        [
            _result_with_one_liner(
                local_key="lemma-a",
                statement=statement,
                one_liner="Centroid halfspaces keep a 1/e fraction",
            )
        ]
    )
    claim_id = next(
        node.matek_id for node in first.nodes if node.node_type is NodeType.CLAIM
    )
    second = admit(
        [
            _result_with_one_liner(
                local_key="lemma-b",
                statement=statement,
                one_liner="A totally different description of the same statement",
            )
        ],
        nodes=[*existing_nodes(), *first.nodes],
    )
    claims = [node for node in second.nodes if node.node_type is NodeType.CLAIM]
    assert [claim.matek_id for claim in claims] == [claim_id]
    assert claims[0].matek_id.startswith("CLAIM: Centroid halfspaces")


def test_distinct_statements_with_the_same_one_liner_get_numeric_suffixes() -> None:
    plan = admit(
        [
            _result_with_one_liner(
                local_key="lemma-a",
                statement="For every n, P(n).",
                one_liner="The same one-liner",
            ),
            _result_with_one_liner(
                local_key="lemma-b",
                statement="For every n, Q(n).",
                one_liner="The same one-liner",
            ),
        ]
    )
    claim_ids = sorted(
        node.matek_id for node in plan.nodes if node.node_type is NodeType.CLAIM
    )
    assert claim_ids == ["CLAIM: The same one-liner", "CLAIM: The same one-liner (2)"]


def test_missing_one_liner_falls_back_to_the_exact_statement() -> None:
    plan = admit(
        [
            ScientificResult(
                local_key="plain-lemma",
                kind=ScientificResultKind.LEMMA,
                exact_statement="For every n, the placeholder property holds.",
                scope=ScientificScope.BRANCH,
                proof_or_certificate="A complete, checkable proof.",
                disposition=ScientificResultDisposition.PROPOSED_COMPLETE,
            )
        ]
    )
    claim = next(node for node in plan.nodes if node.node_type is NodeType.CLAIM)
    assert claim.matek_id == "CLAIM: For every n, the placeholder property holds."


def test_obligation_one_liner_names_the_open_obligation() -> None:
    plan = admit(
        [result(local_key="parent-lemma", scope=ScientificScope.BRANCH)],
        obligations=[
            ScientificObligationDeclaration(
                local_key="close-gap",
                exact_statement="Prove the induction step for arbitrary n.",
                one_liner="Close the induction step for arbitrary n",
                conclusion="The induction step holds for arbitrary n.",
                parent_result_keys=["parent-lemma"],
            )
        ],
    )
    obligation = next(node for node in plan.nodes if node.node_type is NodeType.OBLIGATION)
    assert obligation.matek_id == "OBLIGATION: Close the induction step for arbitrary n"
    assert obligation.title == "Close the induction step for arbitrary n"


def test_obligation_retry_coalesces_by_admission_binding_not_id() -> None:
    obligations = [
        ScientificObligationDeclaration(
            local_key="close-gap",
            exact_statement="Prove the induction step for arbitrary n.",
            one_liner="Close the induction step",
            conclusion="The induction step holds.",
            parent_result_keys=["parent-lemma"],
        )
    ]
    first = admit(
        [result(local_key="parent-lemma", scope=ScientificScope.BRANCH)],
        obligations=obligations,
    )
    retry = admit(
        [result(local_key="parent-lemma", scope=ScientificScope.BRANCH)],
        nodes=[*existing_nodes(), *first.nodes],
        obligations=obligations,
    )
    assert retry.nodes == []
