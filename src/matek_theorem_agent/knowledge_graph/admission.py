"""Deterministic admission from typed scientific reports into graph records."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ..graph_ids import (
    dedupe_descriptive_id,
    descriptive_node_id,
    normalize_id_description,
    unknown_id_message,
)
from ..scientific import (
    ScientificObligationDeclaration,
    ScientificResult,
    ScientificResultDisposition,
    ScientificResultKind,
    ScientificScope,
    exact_statement_fingerprint,
    is_explicit_definition_declaration,
    normalize_exact_statement,
    validate_result_dependency_dag,
)
from .ledger import logical_version
from .markdown import exact_statement, new_generated_body
from .models import (
    NODE_ID_WORDS,
    ClaimType,
    EpistemicStatus,
    GraphEdge,
    GraphNode,
    NodeType,
    RelationType,
    WorkflowStatus,
    node_id_matches_type,
    validate_node_id,
)


class ScientificAdmissionError(ValueError):
    """A typed report cannot be admitted without inventing or weakening evidence."""


class _AdmissionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdmittedResultRecord(_AdmissionModel):
    local_key: str
    admission_identity: str
    payload_sha256: str
    node_ids: list[str] = Field(default_factory=list)
    canonical_ledger_admitted: bool
    already_applied: bool = False
    blocking_obligation_ids: list[str] = Field(default_factory=list)


class ScientificAdmissionPlan(_AdmissionModel):
    nodes: list[GraphNode]
    records: list[AdmittedResultRecord]
    issues: list[str] = Field(default_factory=list)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _deterministic_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest().upper()[:20]
    return validate_node_id(f"{prefix}-{digest}")


class _NodeIdAllocator:
    """Deterministically mint descriptive node IDs for one admission batch.

    Canonical claim and definition identity still coalesces identical statements:
    existing nodes are indexed by their exact-statement fingerprint, scope, and
    assumption contract, and re-admission resolves to the existing node whatever
    its ID format.  Only genuinely new content receives a fresh descriptive ID,
    with a numeric `` (n)`` suffix when two different statements share an
    agent-written one-liner.
    """

    def __init__(
        self,
        existing_nodes: Sequence[GraphNode],
        *,
        problem_id: str,
        main_target_id: str,
    ) -> None:
        self.problem_id = problem_id
        self.main_target_id = main_target_id
        self.taken: set[str] = {node.matek_id.casefold() for node in existing_nodes}
        self.claim_index: dict[tuple[str, str, tuple[str, ...]], str] = {}
        self.definition_index: dict[tuple[str, str], str] = {}
        for node in existing_nodes:
            if node.problem_id != problem_id or node.tombstone:
                continue
            statement = exact_statement(node.body)
            if not statement:
                continue
            fingerprint = exact_statement_fingerprint(statement)
            if node.node_type is NodeType.CLAIM:
                scope = (
                    ScientificScope.MAIN.value
                    if node.matek_id == main_target_id
                    else str(node.metadata.get("matek_scientific_scope") or "branch")
                )
                assumptions = tuple(_metadata_string_list(node, "matek_normalized_assumptions"))
                self.claim_index.setdefault((scope, fingerprint, assumptions), node.matek_id)
            elif node.node_type is NodeType.DEFINITION:
                scope = str(node.metadata.get("matek_scientific_scope") or "branch")
                self.definition_index.setdefault((scope, fingerprint), node.matek_id)

    def allocate(self, node_type: NodeType, description: str) -> str:
        word = NODE_ID_WORDS[node_type]
        candidate = descriptive_node_id(word, description)
        allocated = dedupe_descriptive_id(candidate, self.taken)
        self.taken.add(allocated.casefold())
        return allocated

    def claim_id(
        self,
        *,
        main_target_id: str,
        main_target_statement: str,
        result: ScientificResult,
    ) -> str:
        """Return the canonical claim node ID for one result, coalescing by content."""

        assumption_contract = _normalized_assumption_contract(result)
        if (
            not assumption_contract
            and result.scope is ScientificScope.MAIN
            and normalize_exact_statement(result.exact_statement)
            == normalize_exact_statement(main_target_statement)
        ):
            return main_target_id
        fingerprint = exact_statement_fingerprint(result.exact_statement)
        key = (result.scope.value, fingerprint, tuple(assumption_contract))
        existing = self.claim_index.get(key)
        if existing is not None:
            return existing
        description = result.one_liner or result.exact_statement
        allocated = self.allocate(NodeType.CLAIM, description)
        self.claim_index[key] = allocated
        return allocated

    def definition_id(self, *, result: ScientificResult) -> str:
        """Return the canonical definition node ID for one result, coalescing by content."""

        fingerprint = exact_statement_fingerprint(result.exact_statement)
        key = (result.scope.value, fingerprint)
        existing = self.definition_index.get(key)
        if existing is not None:
            return existing
        description = result.one_liner or result.exact_statement
        allocated = self.allocate(NodeType.DEFINITION, description)
        self.definition_index[key] = allocated
        return allocated


def _result_one_liner(result: ScientificResult) -> str:
    """Return the agent-written one-liner or fall back to the exact statement."""

    return result.one_liner or normalize_id_description(result.exact_statement)


def admission_identity(
    run_id: str,
    assignment_id: str,
    local_key: str,
    schema_version: int,
) -> str:
    return "\0".join([run_id, assignment_id, local_key, str(schema_version)])


def admission_payload_sha256(result: ScientificResult) -> str:
    payload = result.model_dump(mode="json")
    # Preserve the established identity hash for schema-v1 reports written before local result
    # dependencies were added.  A nonempty dependency DAG remains part of the immutable payload.
    if not result.dependency_result_keys:
        payload.pop("dependency_result_keys", None)
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def encode_admission_binding(identity: str, payload_sha256: str) -> str:
    """Encode one immutable report-to-node admission binding for graph metadata.

    Canonical claim and definition IDs intentionally coalesce identical statements.  Scalar
    provenance therefore cannot identify every report that admitted the shared node.  Graph
    metadata only permits scalar values and ``list[str]``, so bindings use canonical JSON strings
    and retain the legacy scalar fields for backwards compatibility.
    """

    if not identity or not re.fullmatch(r"[0-9a-f]{64}", payload_sha256):
        raise ValueError("admission bindings require an identity and lowercase SHA-256 payload")
    return _canonical_json(
        {
            "binding_version": 1,
            "identity": identity,
            "payload_sha256": payload_sha256,
        }
    )


def admission_binding_payloads(node: GraphNode, identity: str) -> set[str]:
    """Return every valid payload bound to ``identity`` on ``node``.

    Malformed list entries are ignored rather than guessed.  A conflicting valid payload is
    surfaced to the caller as a collision.  Legacy single-binding nodes remain readable.
    """

    payloads: set[str] = set()
    legacy_identity = node.metadata.get("matek_admission_identity")
    legacy_payload = node.metadata.get("matek_admission_payload_sha256")
    if (
        legacy_identity == identity
        and isinstance(legacy_payload, str)
        and re.fullmatch(r"[0-9a-f]{64}", legacy_payload)
    ):
        payloads.add(legacy_payload)
    raw_bindings = node.metadata.get("matek_admission_bindings")
    if not isinstance(raw_bindings, list):
        return payloads
    for raw_binding in raw_bindings:
        try:
            binding = json.loads(raw_binding)
        except ValueError:
            continue
        if not isinstance(binding, dict) or binding.get("binding_version") != 1:
            continue
        payload = binding.get("payload_sha256")
        if (
            binding.get("identity") == identity
            and isinstance(payload, str)
            and re.fullmatch(r"[0-9a-f]{64}", payload)
        ):
            payloads.add(payload)
    return payloads


def matches_admission_binding(node: GraphNode, identity: str, payload_sha256: str) -> bool:
    """Return whether a canonical graph node retains this exact immutable report binding."""

    return admission_binding_payloads(node, identity) == {payload_sha256}


def scientific_admission_binding_sha256(
    run_id: str,
    assignment_id: str,
    result: ScientificResult,
) -> str:
    """Return a stable digest naming one exact scientific-report admission binding."""

    encoded = encode_admission_binding(
        admission_identity(run_id, assignment_id, result.local_key, result.schema_version),
        admission_payload_sha256(result),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def node_has_scientific_admission_binding(
    node: GraphNode,
    *,
    run_id: str,
    assignment_id: str,
    result: ScientificResult,
) -> bool:
    """Authenticate the exact report binding retained by a canonical shared node."""

    return matches_admission_binding(
        node,
        admission_identity(run_id, assignment_id, result.local_key, result.schema_version),
        admission_payload_sha256(result),
    )


def canonical_definition_dependency_contract(node: GraphNode) -> tuple[list[str], list[str]]:
    """Return the ordered mathematical dependency edge/version contract for a definition."""

    return (
        [edge.target_id for edge in node.relations if edge.relation is RelationType.DEPENDS_ON],
        list(node.dependency_versions),
    )


def canonical_admitted_definition_scope(node: GraphNode) -> ScientificScope | None:
    """Authenticate an application-admitted notation declaration and return its scope."""

    if node.node_type is not NodeType.DEFINITION:
        return None
    statement = exact_statement(node.body)
    raw_scope = node.metadata.get("matek_scientific_scope")
    raw_schema = node.metadata.get("matek_scientific_schema_version")
    try:
        scope = ScientificScope(str(raw_scope))
    except ValueError:
        return None
    if (
        not statement
        or scope is not ScientificScope.BRANCH
        or not is_explicit_definition_declaration(statement)
        or node.author_role != "matek-scientific-admission"
        or node.metadata.get("matek_scientific_kind") != ScientificResultKind.DEFINITION.value
        or node.metadata.get("matek_scientific_disposition")
        != ScientificResultDisposition.PROPOSED_COMPLETE.value
        or node.metadata.get("matek_exact_gap") is not None
        or bool(node.metadata.get("matek_normalized_assumptions"))
        or raw_schema != 1
        or canonical_definition_dependency_contract(node) != ([], [])
        or bool(node.metadata.get("matek_dependency_result_keys"))
    ):
        return None
    if not node_id_matches_type(node.matek_id, NodeType.DEFINITION):
        return None
    identity = node.metadata.get("matek_admission_identity")
    payload = node.metadata.get("matek_admission_payload_sha256")
    if not isinstance(identity, str) or not isinstance(payload, str):
        return None
    if identity != admission_identity(
        node.created_in_run,
        str(node.metadata.get("matek_assignment_id") or ""),
        str(node.metadata.get("matek_result_local_key") or ""),
        raw_schema,
    ):
        return None
    return scope if matches_admission_binding(node, identity, payload) else None


def _obligation_admission_identity(
    run_id: str,
    assignment_id: str,
    declaration: ScientificObligationDeclaration,
) -> str:
    return "\0".join(
        [
            run_id,
            assignment_id,
            "obligation",
            declaration.local_key,
            str(declaration.schema_version),
        ]
    )


def _obligation_payload_sha256(declaration: ScientificObligationDeclaration) -> str:
    payload = _canonical_json(declaration.model_dump(mode="json"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _metadata_admission_identity(node: GraphNode) -> str | None:
    value = node.metadata.get("matek_admission_identity")
    return value if isinstance(value, str) else None


def _metadata_payload_hash(node: GraphNode) -> str | None:
    value = node.metadata.get("matek_admission_payload_sha256")
    return value if isinstance(value, str) else None


def _logical_node_version(node: GraphNode) -> str:
    statement = exact_statement(node.body)
    if not statement:
        statement = normalize_exact_statement(node.body)
    return logical_version(statement)


def _metadata_string_list(node: GraphNode, key: str) -> list[str]:
    value = node.metadata.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return []
    return list(dict.fromkeys(item.strip() for item in value if item.strip()))


def _merge_admission_binding_metadata(
    node: GraphNode,
    incoming_metadata: Mapping[str, str | int | bool | list[str] | None],
) -> None:
    """Merge immutable aliases without replacing the shared node's primary provenance."""

    current = node.metadata.get("matek_admission_bindings")
    incoming = incoming_metadata.get("matek_admission_bindings")
    current_bindings = current if isinstance(current, list) else []
    incoming_bindings = incoming if isinstance(incoming, list) else []
    node.metadata["matek_admission_bindings"] = sorted(set([*current_bindings, *incoming_bindings]))


def _verified_replay_artifacts_by_result(
    *,
    existing_nodes: Sequence[GraphNode],
    problem_id: str,
    run_id: str,
    assignment_id: str,
    results: Sequence[ScientificResult],
) -> dict[str, list[str]]:
    """Discover replay-passed computation evidence from application-owned graph nodes.

    Scientific reports cannot grant themselves replay trust.  A computation enters the
    proposed ledger only when the existing graph contains the exact manifest/replay pair
    created by MATEK after persisted evidence verification.  In particular, both artifacts
    bind the assignment and result key, the replay binds the manifest by edge and digest,
    and both nodes have already passed the independent replay gate.
    """

    artifacts = {
        node.matek_id: node
        for node in existing_nodes
        if node.node_type is NodeType.ARTIFACT
        and node.problem_id == problem_id
        and node.created_in_run == run_id
    }
    verified: dict[str, list[str]] = {}
    for result in results:
        if result.kind is not ScientificResultKind.COMPUTATION:
            continue
        pairs: set[tuple[str, str]] = set()
        for replay in artifacts.values():
            replay_record_sha256 = replay.metadata.get("matek_computation_replay_record_sha256")
            manifest_sha256 = replay.metadata.get("matek_computation_manifest_sha256")
            if (
                replay.author_role != "computation-replayer"
                or replay.epistemic_status is not EpistemicStatus.AUDIT_PASSED
                or replay.workflow_status is not WorkflowStatus.COMPLETE
                or replay.metadata.get("matek_assignment_id") != assignment_id
                or replay.metadata.get("matek_computation_replay_status") != "passed"
                or replay.metadata.get("matek_replay_passed") is not True
                or not isinstance(manifest_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256)
                or not isinstance(replay_record_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", replay_record_sha256)
                or result.local_key
                not in _metadata_string_list(replay, "matek_supporting_result_keys")
                or replay.matek_id
                != _deterministic_id(
                    "ART",
                    problem_id,
                    run_id,
                    assignment_id,
                    replay_record_sha256,
                    "replay",
                )
            ):
                continue
            manifest_ids = {
                edge.target_id
                for edge in replay.relations
                if edge.relation is RelationType.RELATED_TO and edge.target_id in artifacts
            }
            for manifest_id in manifest_ids:
                manifest = artifacts[manifest_id]
                if (
                    manifest.author_role != "computation-collector"
                    or manifest.epistemic_status is not EpistemicStatus.AUDIT_PASSED
                    or manifest.workflow_status is not WorkflowStatus.COMPLETE
                    or manifest.metadata.get("matek_assignment_id") != assignment_id
                    or manifest.metadata.get("matek_computation_manifest_sha256") != manifest_sha256
                    or manifest.metadata.get("matek_computation_replay_status") != "passed"
                    or manifest.metadata.get("matek_replay_passed") is not True
                    or result.local_key
                    not in _metadata_string_list(manifest, "matek_supporting_result_keys")
                    or manifest.matek_id
                    != _deterministic_id(
                        "ART",
                        problem_id,
                        run_id,
                        assignment_id,
                        manifest_sha256,
                        "manifest",
                    )
                    or not set(replay.source_artifacts).intersection(manifest.source_artifacts)
                ):
                    continue
                pairs.add((manifest.matek_id, replay.matek_id))
        if len(pairs) > 1:
            raise ScientificAdmissionError(
                f"ambiguous verified replay evidence for result {result.local_key!r}"
            )
        if pairs:
            verified[result.local_key] = list(next(iter(pairs)))
    return verified


def _scope_claim_id(
    *,
    allocator: _NodeIdAllocator,
    main_target_id: str,
    main_target_statement: str,
    result: ScientificResult,
) -> str:
    return allocator.claim_id(
        main_target_id=main_target_id,
        main_target_statement=main_target_statement,
        result=result,
    )


def _normalized_assumption_contract(result: ScientificResult) -> list[str]:
    """Return the canonical archive-only assumption contract for one reported result."""

    return sorted(normalize_exact_statement(item) for item in result.assumptions)


def _same_report_dependency_target_id(
    *,
    allocator: _NodeIdAllocator,
    main_target_id: str,
    main_target_statement: str,
    result: ScientificResult,
    replayed: Mapping[str, list[str]],
) -> str | None:
    """Return the canonical graph node that can serve as this result's local premise."""

    if (
        result.assumptions
        or result.exact_gap is not None
        or result.disposition is not ScientificResultDisposition.PROPOSED_COMPLETE
    ):
        return None
    if result.kind is ScientificResultKind.DEFINITION:
        if (
            result.scope is not ScientificScope.BRANCH
            or not is_explicit_definition_declaration(result.exact_statement)
            or result.exact_gap is not None
            or result.disposition is not ScientificResultDisposition.PROPOSED_COMPLETE
            or result.dependency_node_ids
            or result.dependency_result_keys
        ):
            return None
        return allocator.definition_id(result=result)
    if result.kind is ScientificResultKind.COUNTEREXAMPLE:
        return None
    if result.kind is ScientificResultKind.COMPUTATION and not replayed.get(result.local_key):
        return None
    return _scope_claim_id(
        allocator=allocator,
        main_target_id=main_target_id,
        main_target_statement=main_target_statement,
        result=result,
    )


def _claim_type(kind: ScientificResultKind) -> ClaimType:
    return {
        ScientificResultKind.REDUCTION: ClaimType.EQUIVALENCE,
        ScientificResultKind.SOURCE_FACT: ClaimType.LEMMA,
        ScientificResultKind.COMPUTATION: ClaimType.LEMMA,
        ScientificResultKind.LEMMA: ClaimType.LEMMA,
    }.get(kind, ClaimType.LEMMA)


def _admission_metadata(
    *,
    identity: str,
    payload_sha256: str,
    assignment_id: str,
    result: ScientificResult,
) -> dict[str, str | int | bool | list[str] | None]:
    return {
        "matek_admission_identity": identity,
        "matek_admission_payload_sha256": payload_sha256,
        "matek_admission_bindings": [encode_admission_binding(identity, payload_sha256)],
        "matek_assignment_id": assignment_id,
        "matek_result_local_key": result.local_key,
        "matek_scientific_schema_version": result.schema_version,
        "matek_scientific_scope": result.scope.value,
        "matek_scientific_kind": result.kind.value,
        "matek_scientific_disposition": result.disposition.value,
        "matek_exact_gap": result.exact_gap,
        "matek_dependency_result_keys": result.dependency_result_keys,
        "matek_target_node_ids": result.target_node_ids,
        "matek_normalized_assumptions": _normalized_assumption_contract(result),
    }


def _common_node(
    *,
    node_id: str,
    node_type: NodeType,
    problem_id: str,
    title: str,
    body: str,
    run_id: str,
    now: datetime,
    source_artifact: str,
    metadata: dict[str, str | int | bool | list[str] | None],
    relations: list[GraphEdge] | None = None,
    epistemic_status: EpistemicStatus = EpistemicStatus.OPEN,
    workflow_status: WorkflowStatus = WorkflowStatus.ACTIVE,
    tags: list[str] | None = None,
    evidence: list[str] | None = None,
    dependency_versions: list[str] | None = None,
    claim_type: ClaimType | None = None,
) -> GraphNode:
    return GraphNode(
        matek_id=node_id,
        node_type=node_type,
        problem_id=problem_id,
        title=title,
        epistemic_status=epistemic_status,
        workflow_status=workflow_status,
        claim_type=claim_type,
        created_in_run=run_id,
        last_modified_run=run_id,
        author_role="matek-scientific-admission",
        created_at=now,
        updated_at=now,
        body=body,
        tags=tags or [],
        relations=relations or [],
        dependency_versions=dependency_versions or [],
        source_artifacts=[source_artifact],
        evidence=evidence or [],
        metadata=metadata,
    )


def _explicit_obligation_node(
    declaration: ScientificObligationDeclaration,
    *,
    allocator: _NodeIdAllocator,
    problem_id: str,
    run_id: str,
    assignment_id: str,
    now: datetime,
    source_artifact: str,
    parent_ids: Sequence[str],
    target_claim_ids: Sequence[str],
    dependency_nodes: Mapping[str, GraphNode],
) -> GraphNode:
    obligation_id = allocator.allocate(
        NodeType.OBLIGATION,
        declaration.one_liner or declaration.conclusion,
    )
    dependency_versions = [
        f"{node_id}@{_logical_node_version(dependency_nodes[node_id])}"
        for node_id in declaration.dependency_node_ids
    ]
    relations = [
        GraphEdge(
            source_id=obligation_id,
            relation=RelationType.DEPENDS_ON,
            target_id=node_id,
        )
        for node_id in declaration.dependency_node_ids
    ]
    relations.extend(
        GraphEdge(
            source_id=obligation_id,
            relation=RelationType.BLOCKS,
            target_id=parent_id,
        )
        for parent_id in parent_ids
    )
    relations.extend(
        GraphEdge(
            source_id=obligation_id,
            relation=RelationType.TARGETS,
            target_id=target_id,
        )
        for target_id in target_claim_ids
    )
    parent_types = {
        parent_id: dependency_nodes[parent_id].node_type
        for parent_id in parent_ids
        if parent_id in dependency_nodes
    }
    obligation_title = declaration.one_liner or f"Open obligation: {declaration.local_key}"
    return _common_node(
        node_id=obligation_id,
        node_type=NodeType.OBLIGATION,
        problem_id=problem_id,
        title=obligation_title,
        body=new_generated_body(
            obligation_title,
            "## Exact statement\n\n"
            + declaration.exact_statement
            + "\n\n## Quantifiers\n\n"
            + ("\n".join(f"- {item}" for item in declaration.quantifiers) or "_None._")
            + "\n\n## Hypotheses\n\n"
            + ("\n".join(f"- {item}" for item in declaration.hypotheses) or "_None._")
            + "\n\n## Conclusion\n\n"
            + declaration.conclusion
            + "\n\n## Falsification evidence\n\n"
            + ("\n".join(f"- {item}" for item in declaration.falsification_evidence) or "_None._"),
        ),
        run_id=run_id,
        now=now,
        source_artifact=source_artifact,
        metadata={
            "matek_obligation_admission_identity": _obligation_admission_identity(
                run_id,
                assignment_id,
                declaration,
            ),
            "matek_obligation_admission_payload_sha256": _obligation_payload_sha256(declaration),
            "matek_assignment_id": assignment_id,
            "matek_obligation_local_key": declaration.local_key,
            "matek_parent_derivation_ids": [
                parent_id
                for parent_id in parent_ids
                if parent_types.get(parent_id) is NodeType.DERIVATION
            ],
            "matek_parent_proof_attempt_ids": [
                parent_id
                for parent_id in parent_ids
                if parent_types.get(parent_id) is NodeType.PROOF_ATTEMPT
            ],
            "matek_parent_node_ids": list(parent_ids),
            "matek_dependency_claim_ids": declaration.dependency_node_ids,
            "matek_target_claim_ids": list(target_claim_ids),
            "matek_quantifiers": declaration.quantifiers,
            "matek_hypotheses": declaration.hypotheses,
            "matek_conclusion": declaration.conclusion,
            "matek_scope": declaration.scope.value,
            "matek_notation_definition_version": declaration.notation_definition_version,
            "matek_estimated_leverage": declaration.estimated_leverage,
        },
        relations=relations,
        workflow_status=WorkflowStatus.BLOCKED,
        tags=["matek/obligation", f"matek/scope-{declaration.scope.value}"],
        evidence=declaration.falsification_evidence,
        dependency_versions=dependency_versions,
    )


def build_scientific_admission(
    *,
    existing_nodes: Sequence[GraphNode],
    problem_id: str,
    main_target_id: str,
    run_id: str,
    assignment_id: str,
    task_id: str,
    approach_id: str,
    results: Sequence[ScientificResult],
    unresolved_obligations: Sequence[ScientificObligationDeclaration],
    source_artifact: str,
    now: datetime,
) -> ScientificAdmissionPlan:
    """Construct all graph records from application-owned identity and validated math."""

    by_id = {node.matek_id: node for node in existing_nodes}
    target = by_id.get(main_target_id)
    if target is None or target.node_type is not NodeType.CLAIM:
        raise ScientificAdmissionError("scientific admission requires the frozen main target")
    if task_id not in by_id or by_id[task_id].node_type is not NodeType.TASK:
        raise ScientificAdmissionError("scientific admission requires its server-owned task")
    if approach_id not in by_id and not any(
        node.matek_id == approach_id for node in existing_nodes
    ):
        # The service may include the newly constructed approach in existing_nodes.  It is
        # never legal for the model to select or synthesize the branch identity itself.
        raise ScientificAdmissionError("scientific admission requires its server-owned approach")

    replayed = _verified_replay_artifacts_by_result(
        existing_nodes=existing_nodes,
        problem_id=problem_id,
        run_id=run_id,
        assignment_id=assignment_id,
        results=results,
    )
    allocator = _NodeIdAllocator(
        existing_nodes, problem_id=problem_id, main_target_id=main_target_id
    )
    known_ids = set(by_id)
    declared_result_keys = [result.local_key for result in results]
    if len(declared_result_keys) != len(set(declared_result_keys)):
        raise ScientificAdmissionError("scientific result local_key values must be unique")
    incomplete_definitions = [
        result.local_key
        for result in results
        if result.kind is ScientificResultKind.DEFINITION
        and (
            result.scope is not ScientificScope.BRANCH
            or not is_explicit_definition_declaration(result.exact_statement)
            or result.exact_gap is not None
            or result.disposition is not ScientificResultDisposition.PROPOSED_COMPLETE
            or result.dependency_node_ids
            or result.dependency_result_keys
            or result.assumptions
        )
    ]
    if incomplete_definitions:
        raise ScientificAdmissionError(
            "scientific definitions must be explicit branch-scoped, dependency-free, gap-free "
            "proposed-complete declarations: " + ", ".join(sorted(incomplete_definitions))
        )
    unknown_result_dependencies = sorted(
        {
            dependency_id
            for result in results
            for dependency_id in result.dependency_node_ids
            if dependency_id not in known_ids
        }
    )
    if unknown_result_dependencies:
        raise ScientificAdmissionError(
            unknown_id_message(
                "scientific report references unknown dependency node ID(s): ",
                unknown_result_dependencies,
                known_ids,
            )
        )
    try:
        dependency_order = validate_result_dependency_dag(results)
    except ValueError as exc:
        raise ScientificAdmissionError(str(exc)) from exc
    result_by_key = {result.local_key: result for result in results}
    main_target_statement = exact_statement(target.body)
    local_dependency_target_ids = {
        result.local_key: target_id
        for result in results
        for target_id in [
            _same_report_dependency_target_id(
                allocator=allocator,
                main_target_id=main_target_id,
                main_target_statement=main_target_statement,
                result=result,
                replayed=replayed,
            )
        ]
        if target_id is not None
    }
    resolved_dependency_ids: dict[str, list[str]] = {}
    resolved_dependency_versions: dict[str, list[str]] = {}
    for local_key in dependency_order:
        result = result_by_key[local_key]
        local_ids: list[str] = []
        for dependency_key in result.dependency_result_keys:
            dependency_id = local_dependency_target_ids.get(dependency_key)
            if dependency_id is None:
                dependency = result_by_key[dependency_key]
                reason = (
                    "its independent computation replay did not pass"
                    if dependency.kind is ScientificResultKind.COMPUTATION
                    else f"result kind {dependency.kind.value!r} cannot serve as a proof premise"
                )
                raise ScientificAdmissionError(
                    f"result {local_key!r} depends on local result {dependency_key!r}, but "
                    + reason
                )
            own_target_id = local_dependency_target_ids.get(local_key)
            if own_target_id is not None and dependency_id == own_target_id:
                raise ScientificAdmissionError(
                    f"result {local_key!r} has a local dependency with the same canonical "
                    "conclusion"
                )
            local_ids.append(dependency_id)
        effective_ids = list(dict.fromkeys([*result.dependency_node_ids, *local_ids]))
        own_target_id = local_dependency_target_ids.get(local_key)
        if own_target_id is not None and own_target_id in effective_ids:
            raise ScientificAdmissionError(
                f"result {local_key!r} cannot use its own canonical conclusion "
                f"{own_target_id} as a proof premise"
            )
        resolved_dependency_ids[local_key] = effective_ids
        versions: list[str] = []
        for dependency_id in effective_ids:
            existing_dependency = by_id.get(dependency_id)
            if existing_dependency is not None:
                version = _logical_node_version(existing_dependency)
            else:
                matching_local = next(
                    (
                        dependency
                        for dependency in results
                        if local_dependency_target_ids.get(dependency.local_key) == dependency_id
                    ),
                    None,
                )
                if matching_local is None:
                    raise ScientificAdmissionError(
                        f"result {local_key!r} has no versionable dependency {dependency_id}"
                    )
                version = logical_version(matching_local.exact_statement)
            versions.append(f"{dependency_id}@{version}")
        resolved_dependency_versions[local_key] = versions
    declared_obligation_keys = [item.local_key for item in unresolved_obligations]
    if len(declared_obligation_keys) != len(set(declared_obligation_keys)):
        raise ScientificAdmissionError("scientific obligation local_key values must be unique")

    unknown_dependencies = sorted(
        {
            dependency_id
            for result in results
            for dependency_id in result.dependency_node_ids
            if dependency_id not in known_ids
        }
        | {
            dependency_id
            for obligation in unresolved_obligations
            for dependency_id in obligation.dependency_node_ids
            if dependency_id not in known_ids
        }
    )
    if unknown_dependencies:
        raise ScientificAdmissionError(
            unknown_id_message(
                "scientific report references unknown dependency node ID(s): ",
                unknown_dependencies,
                known_ids,
            )
        )
    unknown_targets = sorted(
        {
            target_id
            for result in results
            for target_id in result.target_node_ids
            if target_id not in known_ids
        }
    )
    if unknown_targets:
        raise ScientificAdmissionError(
            unknown_id_message(
                "scientific report references unknown target node ID(s): ",
                unknown_targets,
                known_ids,
            )
        )
    planned: dict[str, GraphNode] = {}
    records: list[AdmittedResultRecord] = []
    issues: list[str] = []
    result_parent_ids: dict[str, list[str]] = {}
    run_node_id = _deterministic_id("RUN", problem_id, run_id)

    for result in results:
        identity = admission_identity(
            run_id,
            assignment_id,
            result.local_key,
            result.schema_version,
        )
        payload_hash = admission_payload_sha256(result)
        prior = [node for node in existing_nodes if admission_binding_payloads(node, identity)]
        if prior:
            prior_hashes = {
                payload for node in prior for payload in admission_binding_payloads(node, identity)
            }
            if prior_hashes != {payload_hash}:
                raise ScientificAdmissionError(
                    f"admission identity collision for result {result.local_key!r}"
                )
            records.append(
                AdmittedResultRecord(
                    local_key=result.local_key,
                    admission_identity=identity,
                    payload_sha256=payload_hash,
                    node_ids=sorted(node.matek_id for node in prior),
                    canonical_ledger_admitted=any(
                        node.node_type is NodeType.DERIVATION for node in prior
                    ),
                    already_applied=True,
                    blocking_obligation_ids=sorted(
                        node.matek_id for node in prior if node.node_type is NodeType.OBLIGATION
                    ),
                )
            )
            result_parent_ids[result.local_key] = sorted(
                node.matek_id
                for node in prior
                if node.node_type
                in {
                    NodeType.DEFINITION,
                    NodeType.PROOF_ATTEMPT,
                    NodeType.DERIVATION,
                    NodeType.COUNTEREXAMPLE,
                    NodeType.EXPERIMENT,
                }
            )
            continue

        metadata = _admission_metadata(
            identity=identity,
            payload_sha256=payload_hash,
            assignment_id=assignment_id,
            result=result,
        )
        dependency_ids = resolved_dependency_ids[result.local_key]
        dependency_versions = resolved_dependency_versions[result.local_key]
        created_ids: list[str] = []
        blocking_ids: list[str] = []
        canonical_admitted = False

        if result.kind is ScientificResultKind.COUNTEREXAMPLE:
            counterexample_id = allocator.allocate(
                NodeType.COUNTEREXAMPLE, _result_one_liner(result)
            )
            requested_targets = result.target_node_ids or [approach_id]
            safe_targets: list[str] = []
            for target_id in requested_targets:
                if target_id == main_target_id:
                    issues.append(
                        f"Counterexample {result.local_key} was confined to its branch pending "
                        "an independent exact-contract counterexample audit."
                    )
                    continue
                safe_targets.append(target_id)
            if not safe_targets:
                safe_targets = [approach_id]
            relations = [
                GraphEdge(
                    source_id=counterexample_id,
                    relation=(
                        RelationType.REFUTES
                        if by_id.get(target_id, by_id[approach_id]).node_type
                        in {NodeType.CLAIM, NodeType.APPROACH}
                        else RelationType.RELATED_TO
                    ),
                    target_id=target_id,
                )
                for target_id in safe_targets
            ]
            relations.append(
                GraphEdge(
                    source_id=counterexample_id,
                    relation=RelationType.CREATED_DURING,
                    target_id=run_node_id,
                )
            )
            relations.extend(
                GraphEdge(
                    source_id=counterexample_id,
                    relation=RelationType.DEPENDS_ON,
                    target_id=dependency_id,
                )
                for dependency_id in dependency_ids
            )
            node = _common_node(
                node_id=counterexample_id,
                node_type=NodeType.COUNTEREXAMPLE,
                problem_id=problem_id,
                title=f"Counterexample: {result.local_key}",
                body=new_generated_body(
                    f"Counterexample: {result.local_key}",
                    "## Exact statement refuted\n\n"
                    + result.exact_statement
                    + "\n\n## Complete instance or certificate\n\n"
                    + result.proof_or_certificate
                    + "\n\n## Assumptions\n\n"
                    + ("\n".join(f"- {item}" for item in result.assumptions) or "_None._"),
                ),
                run_id=run_id,
                now=now,
                source_artifact=source_artifact,
                metadata=metadata,
                relations=relations,
                epistemic_status=EpistemicStatus.CANDIDATE,
                workflow_status=WorkflowStatus.COMPLETE,
                tags=["matek/counterexample", "matek/branch-local"],
                evidence=[result.proof_or_certificate],
                dependency_versions=dependency_versions,
            )
            planned[node.matek_id] = node
            created_ids.append(node.matek_id)
            result_parent_ids[result.local_key] = [node.matek_id]
        elif result.kind is ScientificResultKind.COMPUTATION and not replayed.get(result.local_key):
            experiment_id = allocator.allocate(NodeType.EXPERIMENT, _result_one_liner(result))
            experiment = _common_node(
                node_id=experiment_id,
                node_type=NodeType.EXPERIMENT,
                problem_id=problem_id,
                title=f"Unreplayed computation: {result.local_key}",
                body=new_generated_body(
                    f"Unreplayed computation: {result.local_key}",
                    "## Reported result\n\n"
                    + result.exact_statement
                    + "\n\n## Reported certificate\n\n"
                    + result.proof_or_certificate
                    + "\n\n## Trust status\n\n"
                    + "Not admitted to the canonical ledger: independent replay is missing.",
                ),
                run_id=run_id,
                now=now,
                source_artifact=source_artifact,
                metadata={**metadata, "matek_replay_status": "missing"},
                relations=[
                    GraphEdge(
                        source_id=experiment_id,
                        relation=RelationType.CREATED_DURING,
                        target_id=run_node_id,
                    ),
                    *(
                        GraphEdge(
                            source_id=experiment_id,
                            relation=RelationType.RELATED_TO,
                            target_id=target_id,
                        )
                        for target_id in result.target_node_ids
                    ),
                ],
                workflow_status=WorkflowStatus.BLOCKED,
                tags=["matek/computation", "matek/replay-required"],
            )
            obligation_id = allocator.allocate(
                NodeType.OBLIGATION,
                f"Independently replay and verify computation: {_result_one_liner(result)}",
            )
            obligation = _common_node(
                node_id=obligation_id,
                node_type=NodeType.OBLIGATION,
                problem_id=problem_id,
                title=f"Replay computation {result.local_key}",
                body=new_generated_body(
                    f"Replay computation {result.local_key}",
                    "## Exact statement\n\n"
                    + f"Independently replay and verify computation {result.local_key}."
                    + "\n\n## Conclusion\n\n"
                    + result.exact_statement,
                ),
                run_id=run_id,
                now=now,
                source_artifact=source_artifact,
                metadata={
                    **metadata,
                    "matek_parent_derivation_ids": [],
                    "matek_parent_proof_attempt_ids": [],
                    "matek_parent_node_ids": [experiment_id],
                    "matek_dependency_claim_ids": [],
                    "matek_target_claim_ids": [
                        target_id
                        for target_id in result.target_node_ids
                        if by_id[target_id].node_type is NodeType.CLAIM
                    ],
                    "matek_conclusion": result.exact_statement,
                    "matek_scope": ScientificScope.COMPUTATION.value,
                    "matek_notation_definition_version": "1",
                    "matek_estimated_leverage": 100,
                },
                relations=[
                    GraphEdge(
                        source_id=obligation_id,
                        relation=RelationType.BLOCKS,
                        target_id=experiment_id,
                    ),
                    *(
                        GraphEdge(
                            source_id=obligation_id,
                            relation=RelationType.TARGETS,
                            target_id=target_id,
                        )
                        for target_id in result.target_node_ids
                    ),
                ],
                workflow_status=WorkflowStatus.BLOCKED,
                tags=["matek/obligation", "matek/computation-replay"],
            )
            planned[experiment_id] = experiment
            planned[obligation_id] = obligation
            created_ids.extend([experiment_id, obligation_id])
            blocking_ids.append(obligation_id)
            result_parent_ids[result.local_key] = [experiment_id]
            issues.append(
                f"Computation {result.local_key} remains outside the ledger until replay passes."
            )
        elif result.kind is ScientificResultKind.DEFINITION:
            definition_id = allocator.definition_id(result=result)
            existing_definition = planned.get(definition_id) or by_id.get(definition_id)
            if existing_definition is not None:
                existing_contract = canonical_definition_dependency_contract(existing_definition)
                incoming_contract = (dependency_ids, dependency_versions)
                if (
                    existing_definition.node_type is not NodeType.DEFINITION
                    or normalize_exact_statement(exact_statement(existing_definition.body))
                    != normalize_exact_statement(result.exact_statement)
                ):
                    raise ScientificAdmissionError(
                        f"canonical definition identity collision for {definition_id}"
                    )
                if existing_contract != incoming_contract:
                    raise ScientificAdmissionError(
                        f"canonical definition {definition_id} has an incompatible dependency "
                        "contract"
                    )
            incoming_definition = _common_node(
                node_id=definition_id,
                node_type=NodeType.DEFINITION,
                problem_id=problem_id,
                title=result.one_liner or f"Definition: {result.local_key}",
                body=new_generated_body(
                    result.one_liner or f"Definition: {result.local_key}",
                    "## Exact statement\n\n"
                    + result.exact_statement
                    + "\n\n## Definition evidence\n\n"
                    + result.proof_or_certificate,
                ),
                run_id=run_id,
                now=now,
                source_artifact=source_artifact,
                metadata=metadata,
                relations=[
                    *(
                        GraphEdge(
                            source_id=definition_id,
                            relation=RelationType.DEPENDS_ON,
                            target_id=dependency_id,
                        )
                        for dependency_id in dependency_ids
                    ),
                    GraphEdge(
                        source_id=definition_id,
                        relation=RelationType.CREATED_DURING,
                        target_id=run_node_id,
                    ),
                    *(
                        GraphEdge(
                            source_id=definition_id,
                            relation=RelationType.RELATED_TO,
                            target_id=target_id,
                        )
                        for target_id in result.target_node_ids
                    ),
                ],
                epistemic_status=EpistemicStatus.CANDIDATE,
                tags=["matek/definition", f"matek/scope-{result.scope.value}"],
                evidence=[result.proof_or_certificate],
                dependency_versions=dependency_versions,
            )
            if existing_definition is None:
                definition = incoming_definition
            else:
                definition = existing_definition.model_copy(deep=True)
                _merge_admission_binding_metadata(definition, metadata)
                definition.metadata["matek_target_node_ids"] = sorted(
                    set(
                        [
                            *_metadata_string_list(definition, "matek_target_node_ids"),
                            *result.target_node_ids,
                        ]
                    )
                )
                definition.relations = list(
                    {
                        (edge.relation, edge.target_id): edge
                        for edge in [*definition.relations, *incoming_definition.relations]
                    }.values()
                )
                definition.source_artifacts = sorted(
                    set([*definition.source_artifacts, source_artifact])
                )
                definition.evidence = sorted(
                    set([*definition.evidence, result.proof_or_certificate])
                )
            planned[definition_id] = definition
            created_ids.append(definition_id)
            result_parent_ids[result.local_key] = [definition_id]
        else:
            claim_id = _scope_claim_id(
                allocator=allocator,
                main_target_id=main_target_id,
                main_target_statement=exact_statement(target.body),
                result=result,
            )
            existing_claim = planned.get(claim_id) or by_id.get(claim_id)
            if existing_claim is not None:
                if (
                    existing_claim.node_type is not NodeType.CLAIM
                    or (
                        normalize_exact_statement(exact_statement(existing_claim.body))
                        != normalize_exact_statement(result.exact_statement)
                    )
                    or _metadata_string_list(
                        existing_claim,
                        "matek_normalized_assumptions",
                    )
                    != _normalized_assumption_contract(result)
                ):
                    raise ScientificAdmissionError(
                        f"canonical claim identity collision for {claim_id}"
                    )
                claim = existing_claim.model_copy(deep=True)
                aliases = claim.metadata.get("matek_result_aliases", [])
                prior_aliases = aliases if isinstance(aliases, list) else []
                claim.metadata["matek_result_aliases"] = list(
                    dict.fromkeys([*prior_aliases, f"{run_id}:{assignment_id}:{result.local_key}"])
                )
                _merge_admission_binding_metadata(claim, metadata)
                claim.source_artifacts = list(
                    dict.fromkeys([*claim.source_artifacts, source_artifact])
                )
                prior_targets = _metadata_string_list(claim, "matek_target_node_ids")
                claim.metadata["matek_target_node_ids"] = list(
                    dict.fromkeys([*prior_targets, *result.target_node_ids])
                )
                known_relations = {(edge.relation, edge.target_id) for edge in claim.relations}
                claim.relations.extend(
                    GraphEdge(
                        source_id=claim_id,
                        relation=RelationType.RELATED_TO,
                        target_id=target_id,
                    )
                    for target_id in result.target_node_ids
                    if target_id != claim_id
                    and (RelationType.RELATED_TO, target_id) not in known_relations
                )
            else:
                claim_title = result.one_liner or f"Scientific result: {result.local_key}"
                claim = _common_node(
                    node_id=claim_id,
                    node_type=NodeType.CLAIM,
                    problem_id=problem_id,
                    title=claim_title,
                    body=new_generated_body(
                        claim_title,
                        "## Exact statement\n\n"
                        + result.exact_statement
                        + "\n\n## Assumptions\n\n"
                        + ("\n".join(f"- {item}" for item in result.assumptions) or "_None._")
                        + "\n\n## Scope\n\n"
                        + result.scope.value,
                    ),
                    run_id=run_id,
                    now=now,
                    source_artifact=source_artifact,
                    metadata={
                        **metadata,
                        "matek_exact_statement_sha256": exact_statement_fingerprint(
                            result.exact_statement
                        ),
                        "matek_result_aliases": [f"{run_id}:{assignment_id}:{result.local_key}"],
                    },
                    relations=[
                        GraphEdge(
                            source_id=claim_id,
                            relation=RelationType.CREATED_DURING,
                            target_id=run_node_id,
                        ),
                        *(
                            GraphEdge(
                                source_id=claim_id,
                                relation=RelationType.RELATED_TO,
                                target_id=target_id,
                            )
                            for target_id in result.target_node_ids
                            if target_id != claim_id
                        ),
                    ],
                    epistemic_status=(
                        EpistemicStatus.CONJECTURED
                        if result.exact_gap is not None
                        or result.assumptions
                        or result.disposition is not ScientificResultDisposition.PROPOSED_COMPLETE
                        else EpistemicStatus.CANDIDATE
                    ),
                    workflow_status=(
                        WorkflowStatus.BLOCKED
                        if result.exact_gap is not None
                        or result.assumptions
                        or result.disposition is not ScientificResultDisposition.PROPOSED_COMPLETE
                        else WorkflowStatus.ACTIVE
                    ),
                    claim_type=_claim_type(result.kind),
                    tags=[
                        "matek/claim",
                        f"matek/scope-{result.scope.value}",
                        *(["matek/unbound-assumptions"] if result.assumptions else []),
                        *(
                            ["matek/incomplete-result"]
                            if result.disposition
                            is not ScientificResultDisposition.PROPOSED_COMPLETE
                            else []
                        ),
                    ],
                )
            if claim_id != main_target_id:
                planned[claim_id] = claim
                created_ids.append(claim_id)

            attempt_id = allocator.allocate(
                NodeType.PROOF_ATTEMPT, _result_one_liner(result)
            )
            attempt_relations = [
                GraphEdge(
                    source_id=attempt_id,
                    relation=RelationType.DEPENDS_ON,
                    target_id=dependency_id,
                )
                for dependency_id in dependency_ids
            ]
            attempt_relations.extend(
                [
                    GraphEdge(
                        source_id=attempt_id,
                        relation=RelationType.RELATED_TO,
                        target_id=claim_id,
                    ),
                    GraphEdge(
                        source_id=attempt_id,
                        relation=RelationType.RELATED_TO,
                        target_id=approach_id,
                    ),
                    GraphEdge(
                        source_id=attempt_id,
                        relation=RelationType.CREATED_DURING,
                        target_id=run_node_id,
                    ),
                    *(
                        GraphEdge(
                            source_id=attempt_id,
                            relation=RelationType.RELATED_TO,
                            target_id=target_id,
                        )
                        for target_id in result.target_node_ids
                        if target_id not in {claim_id, approach_id}
                    ),
                ]
            )
            attempt = _common_node(
                node_id=attempt_id,
                node_type=NodeType.PROOF_ATTEMPT,
                problem_id=problem_id,
                title=f"Proof attempt: {result.local_key}",
                body=new_generated_body(
                    f"Proof attempt: {result.local_key}",
                    "## Exact target\n\n"
                    + result.exact_statement
                    + "\n\n## Proof or certificate\n\n"
                    + result.proof_or_certificate
                    + "\n\n## Declared assumptions\n\n"
                    + ("\n".join(f"- {item}" for item in result.assumptions) or "_None._")
                    + "\n\n## Exact gap\n\n"
                    + (result.exact_gap or "_None declared._"),
                ),
                run_id=run_id,
                now=now,
                source_artifact=source_artifact,
                metadata={**metadata, "matek_exact_gap": result.exact_gap},
                relations=attempt_relations,
                epistemic_status=EpistemicStatus.OPEN,
                workflow_status=(
                    WorkflowStatus.BLOCKED
                    if result.exact_gap is not None
                    or result.assumptions
                    or result.disposition is not ScientificResultDisposition.PROPOSED_COMPLETE
                    else WorkflowStatus.COMPLETE
                ),
                tags=[
                    "matek/proof-attempt",
                    (
                        "matek/unbound-assumptions"
                        if result.assumptions
                        else "matek/gapped"
                        if result.exact_gap is not None
                        else "matek/incomplete-result"
                        if result.disposition is not ScientificResultDisposition.PROPOSED_COMPLETE
                        else "matek/gap-free"
                    ),
                ],
                evidence=[result.proof_or_certificate],
                dependency_versions=dependency_versions,
            )
            planned[attempt_id] = attempt
            created_ids.append(attempt_id)
            result_parent_ids[result.local_key] = [attempt_id]

            computation_artifacts = replayed.get(result.local_key, [])
            if (
                result.exact_gap is None
                and not result.assumptions
                and result.disposition is ScientificResultDisposition.PROPOSED_COMPLETE
            ):
                derivation_id = allocator.allocate(
                    NodeType.DERIVATION, _result_one_liner(result)
                )
                derivation_relations = [
                    GraphEdge(
                        source_id=derivation_id,
                        relation=RelationType.PROVES,
                        target_id=claim_id,
                    ),
                    GraphEdge(
                        source_id=derivation_id,
                        relation=RelationType.RELATED_TO,
                        target_id=attempt_id,
                    ),
                    *(
                        GraphEdge(
                            source_id=derivation_id,
                            relation=RelationType.DEPENDS_ON,
                            target_id=dependency_id,
                        )
                        for dependency_id in dependency_ids
                    ),
                    *(
                        GraphEdge(
                            source_id=derivation_id,
                            relation=RelationType.RELATED_TO,
                            target_id=artifact_id,
                        )
                        for artifact_id in computation_artifacts
                    ),
                    GraphEdge(
                        source_id=derivation_id,
                        relation=RelationType.CREATED_DURING,
                        target_id=run_node_id,
                    ),
                    *(
                        GraphEdge(
                            source_id=derivation_id,
                            relation=RelationType.RELATED_TO,
                            target_id=target_id,
                        )
                        for target_id in result.target_node_ids
                        if target_id != claim_id
                    ),
                ]
                derivation = _common_node(
                    node_id=derivation_id,
                    node_type=NodeType.DERIVATION,
                    problem_id=problem_id,
                    title=f"Proposed derivation: {result.local_key}",
                    body=new_generated_body(
                        f"Proposed derivation: {result.local_key}",
                        "## Exact conclusion\n\n"
                        + result.exact_statement
                        + "\n\n## Joint premises\n\n"
                        + (
                            "\n".join(f"- {dependency_id}" for dependency_id in dependency_ids)
                            or "_No prior premises declared._"
                        )
                        + "\n\n## Proof attempt\n\n"
                        + attempt_id,
                    ),
                    run_id=run_id,
                    now=now,
                    source_artifact=source_artifact,
                    metadata={
                        **metadata,
                        "matek_conclusion_claim_id": claim_id,
                        "matek_premise_claim_ids": dependency_ids,
                        "matek_proof_attempt_id": attempt_id,
                        "matek_exact_target_version": (logical_version(result.exact_statement)),
                        "matek_premise_versions": [
                            dependency_version.replace("@", "=", 1)
                            for dependency_version in dependency_versions
                        ],
                        "matek_artifact_ids": computation_artifacts,
                    },
                    relations=derivation_relations,
                    epistemic_status=EpistemicStatus.CANDIDATE,
                    workflow_status=WorkflowStatus.ACTIVE,
                    tags=["matek/derivation", "matek/proposed"],
                    dependency_versions=dependency_versions,
                )
                planned[derivation_id] = derivation
                created_ids.append(derivation_id)
                result_parent_ids[result.local_key] = [attempt_id, derivation_id]
                canonical_admitted = True
            else:
                gap_description = (
                    f"Bind and discharge the assumptions of {_result_one_liner(result)}"
                    if result.assumptions
                    else f"Close the exact gap in {_result_one_liner(result)}"
                    if result.exact_gap is not None
                    else f"Complete the partial result for {_result_one_liner(result)}"
                )
                obligation_id = allocator.allocate(NodeType.OBLIGATION, gap_description)
                target_claim_ids = list(
                    dict.fromkeys(
                        [
                            claim_id,
                            *(
                                target_id
                                for target_id in result.target_node_ids
                                if by_id[target_id].node_type is NodeType.CLAIM
                            ),
                        ]
                    )
                )
                obligation = _common_node(
                    node_id=obligation_id,
                    node_type=NodeType.OBLIGATION,
                    problem_id=problem_id,
                    title=f"Gap in {result.local_key}",
                    body=new_generated_body(
                        f"Gap in {result.local_key}",
                        "## Exact statement\n\n"
                        + (
                            "Bind and discharge every declared assumption: "
                            + "; ".join(result.assumptions)
                            if result.assumptions
                            else str(result.exact_gap)
                            if result.exact_gap is not None
                            else (
                                "Complete the partial result and provide a gap-free, checkable "
                                f"derivation of {result.exact_statement}."
                            )
                        )
                        + "\n\n## Conclusion\n\n"
                        + (
                            "No bare canonical theorem follows until the assumption contract is "
                            f"discharged for {result.exact_statement}."
                            if result.assumptions
                            else (
                                "Discharge the exact gap before "
                                f"{result.exact_statement} is proved."
                                if result.exact_gap is not None
                                else (
                                    "No canonical theorem follows from a partial disposition "
                                    "without a completed derivation."
                                )
                            )
                        ),
                    ),
                    run_id=run_id,
                    now=now,
                    source_artifact=source_artifact,
                    metadata={
                        **metadata,
                        "matek_parent_derivation_ids": [],
                        "matek_parent_proof_attempt_ids": [attempt_id],
                        "matek_parent_node_ids": [attempt_id],
                        "matek_dependency_claim_ids": dependency_ids,
                        "matek_target_claim_ids": target_claim_ids,
                        "matek_conclusion": result.exact_statement,
                        "matek_hypotheses": _normalized_assumption_contract(result),
                        "matek_scope": result.scope.value,
                        "matek_notation_definition_version": "1",
                        "matek_estimated_leverage": 75,
                    },
                    relations=[
                        GraphEdge(
                            source_id=obligation_id,
                            relation=RelationType.BLOCKS,
                            target_id=attempt_id,
                        ),
                        *(
                            GraphEdge(
                                source_id=obligation_id,
                                relation=RelationType.TARGETS,
                                target_id=target_id,
                            )
                            for target_id in target_claim_ids
                        ),
                    ],
                    workflow_status=WorkflowStatus.BLOCKED,
                    tags=[
                        "matek/obligation",
                        (
                            "matek/unbound-assumptions"
                            if result.assumptions
                            else "matek/exact-gap"
                            if result.exact_gap is not None
                            else "matek/incomplete-result"
                        ),
                    ],
                    dependency_versions=dependency_versions,
                )
                planned[obligation_id] = obligation
                blocking_ids.append(obligation_id)
                created_ids.append(obligation_id)

        records.append(
            AdmittedResultRecord(
                local_key=result.local_key,
                admission_identity=identity,
                payload_sha256=payload_hash,
                node_ids=created_ids,
                canonical_ledger_admitted=canonical_admitted,
                blocking_obligation_ids=blocking_ids,
            )
        )

    combined_nodes = {**by_id, **planned}
    for declaration in unresolved_obligations:
        unknown_parent_keys = sorted(set(declaration.parent_result_keys) - set(result_parent_ids))
        if unknown_parent_keys:
            raise ScientificAdmissionError(
                f"obligation {declaration.local_key!r} references unknown result key(s): "
                + ", ".join(unknown_parent_keys)
            )
        parent_ids = [
            parent_id
            for key in declaration.parent_result_keys
            for parent_id in result_parent_ids[key]
        ]
        explicit_target_claim_ids = {
            edge.target_id
            for parent_id in parent_ids
            for parent in [combined_nodes.get(parent_id)]
            if parent is not None
            for edge in parent.relations
            if (
                edge.relation is RelationType.PROVES
                or (
                    parent.node_type is NodeType.PROOF_ATTEMPT
                    and edge.relation is RelationType.RELATED_TO
                    and edge.target_id in combined_nodes
                    and combined_nodes[edge.target_id].node_type is NodeType.CLAIM
                )
            )
        }
        if not explicit_target_claim_ids and declaration.scope is ScientificScope.MAIN:
            explicit_target_claim_ids.add(main_target_id)
        # Obligations coalesce across retries by their immutable admission binding,
        # not by a recomputed ID: descriptive node IDs are agent-chosen labels.
        declaration_identity = _obligation_admission_identity(run_id, assignment_id, declaration)
        bound_existing = [
            node
            for node in combined_nodes.values()
            if node.node_type is NodeType.OBLIGATION
            and node.metadata.get("matek_obligation_admission_identity")
            == declaration_identity
        ]
        if len(bound_existing) > 1:
            raise ScientificAdmissionError(
                f"obligation admission identity collision for {declaration.local_key!r}"
            )
        if bound_existing:
            existing_obligation = bound_existing[0]
            if (
                existing_obligation.metadata.get("matek_obligation_admission_payload_sha256")
                != _obligation_payload_sha256(declaration)
            ):
                raise ScientificAdmissionError(
                    f"obligation admission identity collision for {declaration.local_key!r}"
                )
            obligation = existing_obligation
        else:
            obligation = _explicit_obligation_node(
                declaration,
                allocator=allocator,
                problem_id=problem_id,
                run_id=run_id,
                assignment_id=assignment_id,
                now=now,
                source_artifact=source_artifact,
                parent_ids=parent_ids,
                target_claim_ids=sorted(explicit_target_claim_ids),
                dependency_nodes=combined_nodes,
            )
            planned[obligation.matek_id] = obligation
            combined_nodes[obligation.matek_id] = obligation
        for key in declaration.parent_result_keys:
            for record in records:
                if record.local_key == key:
                    record.blocking_obligation_ids = list(
                        dict.fromkeys([*record.blocking_obligation_ids, obligation.matek_id])
                    )
                    record.canonical_ledger_admitted = False
                    derivation_ids = [
                        item
                        for item in record.node_ids
                        if combined_nodes.get(item) is not None
                        and combined_nodes[item].node_type is NodeType.DERIVATION
                    ]
                    for derivation_id in derivation_ids:
                        parent_derivation = planned.get(derivation_id)
                        if parent_derivation is not None:
                            raw_ids = parent_derivation.metadata.get("matek_obligation_ids", [])
                            existing_ids = raw_ids if isinstance(raw_ids, list) else []
                            parent_derivation.metadata["matek_obligation_ids"] = list(
                                dict.fromkeys([*existing_ids, obligation.matek_id])
                            )
                            parent_derivation.relations.append(
                                GraphEdge(
                                    source_id=derivation_id,
                                    relation=RelationType.BLOCKED_BY,
                                    target_id=obligation.matek_id,
                                )
                            )

    return ScientificAdmissionPlan(
        nodes=list(planned.values()),
        records=records,
        issues=issues,
    )


__all__ = [
    "AdmittedResultRecord",
    "ScientificAdmissionError",
    "ScientificAdmissionPlan",
    "admission_binding_payloads",
    "admission_identity",
    "admission_payload_sha256",
    "build_scientific_admission",
    "canonical_admitted_definition_scope",
    "canonical_definition_dependency_contract",
    "encode_admission_binding",
    "matches_admission_binding",
    "node_has_scientific_admission_binding",
    "scientific_admission_binding_sha256",
]
