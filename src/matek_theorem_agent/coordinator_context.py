"""Deterministic, evidence-preserving context construction for research coordination."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from .knowledge_graph.models import (
    EpistemicStatus,
    GraphNode,
    NodeType,
    WorkflowStatus,
)

COORDINATOR_PAYLOAD_SCHEMA_VERSION = 3
COORDINATOR_SECTION_ORDER_VERSION = 1
DEFAULT_UNREQUESTED_FULL_GRAPH_NODES_CHARACTER_LIMIT = 120_000
GRAPH_NODE_DIGEST_CHARACTER_LIMIT = 16_000

# This order is part of the schema-v3 provider-input contract. Values inside each
# section remain canonical JSON, but top-level prompt layout is intentionally
# scientific rather than alphabetical. Unknown future sections are placed before
# current deltas instead of perturbing the exact-target prefix or decision brief.
COORDINATOR_SECTION_ORDER: tuple[str, ...] = (
    "coordinator_payload_schema_version",
    "compiled_prompt",
    "claim_contract",
    "exact_target_policy",
    "context_mode",
    "context_contract",
    "coordinator_mode",
    "activation_context",
    "research_agent_hierarchy",
    "decision_id",
    "after_event_sequence",
    "initial_portfolio",
    "minimum_materially_diverse_initial_assignments",
    "maximum_open_assignments",
    "available_new_assignment_slots",
    "available_new_assignments_without_replacement",
    "refundable_unlaunched_assignment_count",
    "coordinator_headroom_borrowed_assignment_id",
    "maximum_new_assignments_this_decision",
    "replacement_rule",
    "maximum_concurrent_workers",
    "worker_web_search_enabled",
    "open_assignment_count",
    "remaining_coordinator_decisions_after_this_call",
    "remaining_model_calls_before_this_call",
    "filesystem_retrieval",
    "scheduler_state_index",
    "knowledge_graph_memory",
    "latest_candidate_package",
    "latest_candidate_state",
    "audit_recovery_state",
    "audit_repair_obligations",
    "latest_independent_audits",
    "latest_final_judge_verdict",
    "queued_assignments",
    "active_assignments",
    "assignment_lifecycle",
    "approach_registry",
    "approach_registry_index",
    "research_continuity",
    "research_continuity_index",
    "artifact_catalog",
    "indexed_omissions",
    # Current deltas and the selected evidence are deliberately near the end.
    "unacknowledged_events",
    "requested_artifacts",
    "requested_graph_nodes",
    "visible_worker_reports",
    "full_graph_nodes",
    "report_summaries",
    "graph_node_summaries",
    "decision_brief",
)


def _ordered_payload_keys(payload: Mapping[str, object]) -> list[str]:
    known = [key for key in COORDINATOR_SECTION_ORDER if key in payload]
    known_set = set(known)
    unknown = sorted(key for key in payload if key not in known_set)
    delta_start = next(
        (
            index
            for index, key in enumerate(known)
            if key
            in {
                "unacknowledged_events",
                "requested_artifacts",
                "requested_graph_nodes",
                "visible_worker_reports",
                "full_graph_nodes",
                "report_summaries",
                "graph_node_summaries",
                "decision_brief",
            }
        ),
        len(known),
    )
    return [*known[:delta_start], *unknown, *known[delta_start:]]


def serialize_coordinator_payload(payload: Mapping[str, object]) -> str:
    """Return the exact canonical JSON sent as the coordinator's stage input."""

    if payload.get("coordinator_payload_schema_version") == COORDINATOR_PAYLOAD_SCHEMA_VERSION:
        fields = [
            json.dumps(key, ensure_ascii=False)
            + ":"
            + json.dumps(
                payload[key],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for key in _ordered_payload_keys(payload)
        ]
        return "{" + ",".join(fields) + "}"
    # Frozen schema-v1/v2 requests used fully alphabetic canonical JSON. Dispatch
    # on the payload marker so legacy manifests keep their exact provider identity.
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def coordinator_section_positions(payload: Mapping[str, object]) -> dict[str, int]:
    """Return the top-level section ordinal used by the exact serialization."""

    if payload.get("coordinator_payload_schema_version") == COORDINATOR_PAYLOAD_SCHEMA_VERSION:
        return {key: index for index, key in enumerate(_ordered_payload_keys(payload))}
    return {key: index for index, key in enumerate(sorted(payload))}


ScoreValue: TypeAlias = str | int | bool | list[str] | None


class CoordinatorArtifactReference(BaseModel):
    """Authenticated address for evidence retained outside the working context."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    kind: Literal["worker_report", "graph_node", "candidate", "audit", "event"]
    relative_path: str
    sha256: str
    assignment_id: str | None = None
    graph_node_id: str | None = None
    graph_revision: str | None = None


class CoordinatorEvidenceItem(BaseModel):
    """One complete artifact plus its deterministic structured summary."""

    model_config = ConfigDict(extra="forbid")

    reference: CoordinatorArtifactReference
    summary: dict[str, object]
    full_content: dict[str, object]
    priority: int = Field(ge=0)
    inclusion_reason: str
    frontier_categories: list[str] = Field(default_factory=list)
    priority_score: dict[str, ScoreValue] = Field(default_factory=dict)
    selection_rank: int = Field(default=0, ge=0)
    approach_family: str | None = None


class CoordinatorContextManifest(BaseModel):
    """Reproducible account of one exact provider working set."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1, 2, 3] = 3
    payload_schema_version: int = Field(default=2, ge=1)
    section_order_version: int = Field(default=0, ge=0)
    decision_id: int = Field(ge=1)
    after_event_sequence: int = Field(ge=0)
    mode: Literal["normal", "compact", "indexed"]
    configured_character_limit: int = Field(gt=0)
    effective_character_limit: int = Field(gt=0)
    # Defaults keep pre-headroom schema-v2 manifests readable on resume.
    packing_character_limit: int = Field(default=1, gt=0)
    reserved_headroom_characters: int = Field(default=0, ge=0)
    serialized_payload_characters: int = Field(ge=0)
    serialized_provider_input_characters: int = Field(ge=0)
    serialized_section_characters: dict[str, int] = Field(default_factory=dict)
    estimated_input_tokens: int = Field(ge=0)
    payload_sha256: str
    included_full_artifacts: list[dict[str, str]] = Field(default_factory=list)
    omitted_artifacts: list[CoordinatorArtifactReference] = Field(default_factory=list)
    aggregated_event_groups: list[dict[str, object]] = Field(default_factory=list)
    requested_artifact_ids: list[str] = Field(default_factory=list)
    requested_graph_node_ids: list[str] = Field(default_factory=list)
    omitted_state_sections: list[dict[str, object]] = Field(default_factory=list)
    evidence_selection: list[dict[str, object]] = Field(default_factory=list)
    section_positions: dict[str, int] = Field(default_factory=dict)
    unused_headroom_characters: int = Field(default=0, ge=0)
    redundant_characters_removed: int = Field(default=0, ge=0)
    unrequested_full_graph_nodes_characters: int = Field(default=0, ge=0)


@dataclass(frozen=True)
class CoordinatorContextBuild:
    payload: dict[str, object]
    serialized_input: str
    manifest: CoordinatorContextManifest


class CoordinatorContextBudgetExhausted(RuntimeError):
    """The irreducible prompt/claim envelope cannot fit the transport budget."""

    def __init__(
        self,
        *,
        limit: int,
        required: int,
        largest_fields: list[tuple[str, int]] | None = None,
        diagnostic: str = "MANDATORY_CONTEXT_TOO_LARGE",
    ) -> None:
        self.limit = limit
        self.required = required
        self.largest_fields = list(largest_fields or [])
        self.diagnostic = diagnostic
        field_summary = (
            "; largest mandatory fields: "
            + ", ".join(f"{name}={characters}" for name, characters in self.largest_fields[:5])
            if self.largest_fields
            else ""
        )
        if diagnostic == "MANDATORY_CONTEXT_TOO_LARGE":
            detail = (
                "the exact coordinator prompt, claim contract, output contract/instructions, "
                "and provider envelope"
            )
        else:
            detail = "the smallest valid compact coordinator transport"
        super().__init__(
            f"CONTEXT_BUDGET_EXHAUSTED: {diagnostic}: {detail} requires {required} serialized "
            f"provider characters but the effective limit is {limit}{field_summary}."
        )


def _aggregate_repetitive_events(
    events: Iterable[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    repetitive_kinds = {
        "coordinator_input_too_large",
        "graph_mutation_rejected",
        "worker_execution_failed",
        "worker_repair_unavailable",
    }
    ordinary: list[dict[str, object]] = []
    groups: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for event in events:
        kind = event.get("kind")
        if not isinstance(kind, str) or kind not in repetitive_kinds:
            ordinary.append(event)
            continue
        detail = event.get("detail", [])
        normalized_detail = json.dumps(detail, ensure_ascii=False, sort_keys=True)
        groups[(kind, normalized_detail)].append(event)

    aggregated: list[dict[str, object]] = []
    group_evidence: list[dict[str, object]] = []
    for (kind, _), members in sorted(groups.items()):
        if len(members) == 1:
            ordinary.append(members[0])
            continue
        assignment_ids = sorted(
            {
                assignment_id
                for member in members
                if isinstance((assignment_id := member.get("assignment_id")), str)
            }
        )
        issue_paths = sorted(
            {path for member in members if isinstance((path := member.get("artifact")), str)}
        )
        sequences = sorted(
            sequence
            for member in members
            if isinstance((sequence := member.get("sequence")), int)
            and not isinstance(sequence, bool)
        )
        group = {
            "schema_version": 1,
            "kind": f"{kind}_aggregate",
            "count": len(members),
            "first_sequence": sequences[0] if sequences else None,
            "last_sequence": sequences[-1] if sequences else None,
            "affected_assignment_ids": assignment_ids,
            "issue_paths": issue_paths,
            "detail": members[0].get("detail", []),
        }
        aggregated.append(group)
        group_evidence.append(group)
    combined = [*ordinary, *aggregated]

    def event_sort_key(event: dict[str, object]) -> tuple[int, str]:
        raw_sequence = event.get("sequence", event.get("first_sequence", 0))
        sequence = (
            raw_sequence
            if isinstance(raw_sequence, int) and not isinstance(raw_sequence, bool)
            else 0
        )
        return sequence, str(event.get("kind", ""))

    combined.sort(key=event_sort_key)
    return combined, group_evidence


def _compact_event(event: Mapping[str, object]) -> dict[str, object]:
    """Preserve event identity and obligations without carrying unbounded prose."""

    result = {
        key: event[key]
        for key in (
            "schema_version",
            "sequence",
            "first_sequence",
            "last_sequence",
            "kind",
            "count",
            "assignment_id",
            "decision_id",
            "response_id",
            "artifact",
            "artifact_sha256",
            "related_artifacts",
            "affected_assignment_ids",
            "issue_paths",
        )
        if key in event
    }
    raw_detail = event.get("detail", [])
    detail = raw_detail if isinstance(raw_detail, list) else [raw_detail]
    result["detail_summary"] = [
        normalized if len(normalized) <= 320 else normalized[:319].rstrip() + "…"
        for item in detail[:8]
        if (normalized := " ".join(str(item).split()))
    ]
    if len(detail) > 8:
        result["omitted_detail_items"] = len(detail) - 8
    return result


@dataclass(frozen=True)
class RankedGraphEvidence:
    """Scientific ordering metadata for one frontier graph node."""

    node: GraphNode
    priority: int
    inclusion_reason: str
    frontier_categories: tuple[str, ...]
    priority_score: dict[str, ScoreValue]
    selection_rank: int
    approach_family: str


_FRONTIER_CATEGORY_ORDER: tuple[str, ...] = (
    "unresolved_contradictions",
    "missing_dependencies",
    "high_value_tasks",
    "candidate_proofs_awaiting_audit",
    "unresolved_claims",
    "unverified_sources",
    "blocked_approaches",
    "refuted_or_unproductive_routes",
    "prior_runs",
)

_SECTION_HEADING = re.compile(r"(?m)^##\s+(.+?)\s*$")


def _markdown_sections(body: str) -> dict[str, str]:
    matches = list(_SECTION_HEADING.finditer(body))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        value = body[match.end() : end].strip()
        value = value.replace("<!-- MATEK:GENERATED:END -->", "").strip()
        sections[match.group(1).strip().casefold()] = value
    return sections


def _bounded_digest_text(value: str, *, character_limit: int = 2_400) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= character_limit:
        return normalized
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    prefix = normalized[:character_limit].rsplit(" ", 1)[0].rstrip()
    return f"{prefix} […] [full-text-sha256:{digest}]"


def _metadata_strings(node: GraphNode, key: str) -> list[str]:
    value = node.metadata.get(key)
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item.strip()]
    return []


def graph_node_typed_digest(
    node: GraphNode,
    *,
    by_id: Mapping[str, GraphNode],
    graph_revision: str,
    relative_path: str,
    sha256: str,
    graph_distance: int,
    main_target_id: str,
) -> dict[str, object]:
    """Build a bounded, node-type-aware digest without replacing canonical evidence."""

    sections = _markdown_sections(node.body)
    digest: dict[str, object] = {
        "digest_schema_version": 1,
        "node_type": node.node_type.value,
        "statement_version": node.statement_version,
        "relation_to_main_target": {
            "main_target_id": _bounded_digest_text(main_target_id, character_limit=200),
            "graph_distance": None if graph_distance >= 1_000_000 else graph_distance,
            "is_exact_target": node.matek_id == main_target_id,
        },
    }

    def first_section(*names: str) -> str:
        return next((sections[name] for name in names if sections.get(name)), "")

    exact_result = first_section(
        "exact statement",
        "exact main problem",
        "strongest valid partial result",
        "exact requested task",
    )
    if not exact_result and node.evidence:
        exact_result = "\n".join(node.evidence)
    if exact_result:
        digest["exact_statement_or_result"] = _bounded_digest_text(exact_result)

    proof_mechanism = first_section(
        "proposed invariant or mechanism",
        "exact route attempted",
        "proof content",
    )
    if proof_mechanism:
        digest["proof_mechanism"] = _bounded_digest_text(proof_mechanism)

    assumptions = _metadata_strings(node, "matek_assumptions")
    if not assumptions:
        raw_scope = first_section("scope and conventions")
        if raw_scope:
            assumptions = [_bounded_digest_text(raw_scope, character_limit=1_200)]
    if assumptions:
        digest["assumptions"] = [
            _bounded_digest_text(item, character_limit=600) for item in assumptions[:12]
        ]

    dependencies = [
        *_metadata_strings(node, "matek_dependencies"),
        *node.dependency_versions,
        *(
            edge.target_id
            for edge in node.relations
            if edge.relation.value in {"depends_on", "blocked_by"}
        ),
    ]
    if dependencies:
        digest["dependencies"] = [
            _bounded_digest_text(item, character_limit=400)
            for item in list(dict.fromkeys(dependencies))[:24]
        ]

    unresolved_gap = first_section("exact gap", "exact failure point")
    if not unresolved_gap and node.invalidation_reasons:
        unresolved_gap = "\n".join(node.invalidation_reasons)
    if unresolved_gap and "no gap declared" not in unresolved_gap.casefold():
        digest["exact_unresolved_gap"] = _bounded_digest_text(unresolved_gap)

    if node.node_type is NodeType.COUNTEREXAMPLE:
        target_ids = [
            edge.target_id
            for edge in node.relations
            if edge.relation.value in {"refutes", "disproves", "related_to", "targets"}
        ]
        target_ids.extend(_metadata_strings(node, "matek_branch_target_ids"))
        digest["counterexample_target_and_scope"] = {
            "counterexample_or_obstruction": _bounded_digest_text(
                first_section("explicit counterexample or obstruction") or node.body
            ),
            "scope": _bounded_digest_text(first_section("scope"), character_limit=1_200),
            "target_node_ids": [
                _bounded_digest_text(item, character_limit=200)
                for item in list(dict.fromkeys(target_ids))[:32]
            ],
        }

    typed_relations: list[dict[str, object]] = []
    for edge in sorted(node.relations, key=lambda item: (item.relation.value, item.target_id))[:32]:
        target = by_id.get(edge.target_id)
        typed_relations.append(
            {
                "relation": edge.relation.value,
                "target_id": _bounded_digest_text(edge.target_id, character_limit=200),
                "target_type": target.node_type.value if target is not None else None,
                "target_title": (
                    _bounded_digest_text(target.title, character_limit=240)
                    if target is not None
                    else None
                ),
            }
        )
    if typed_relations:
        digest["key_typed_relations"] = typed_relations

    digest["provenance"] = {
        "relative_path": _bounded_digest_text(relative_path, character_limit=600),
        "graph_revision": _bounded_digest_text(graph_revision, character_limit=200),
        "sha256": sha256,
        "last_modified_run": _bounded_digest_text(node.last_modified_run, character_limit=200),
        "updated_at": node.updated_at.isoformat(),
    }
    prunable_list_keys = ("key_typed_relations", "dependencies", "assumptions")
    omitted_counts: dict[str, int] = defaultdict(int)

    def serialized_digest_characters() -> int:
        if omitted_counts:
            digest["omitted_digest_items"] = dict(sorted(omitted_counts.items()))
        return len(
            json.dumps(
                digest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    while serialized_digest_characters() > GRAPH_NODE_DIGEST_CHARACTER_LIMIT:
        pruned = False
        for key in prunable_list_keys:
            value = digest.get(key)
            if isinstance(value, list) and value:
                value.pop()
                omitted_counts[key] += 1
                pruned = True
                break
        if not pruned:
            counterexample = digest.get("counterexample_target_and_scope")
            counterexample_target_ids = (
                counterexample.get("target_node_ids") if isinstance(counterexample, dict) else None
            )
            if isinstance(counterexample_target_ids, list) and counterexample_target_ids:
                counterexample_target_ids.pop()
                omitted_counts["counterexample_target_node_ids"] += 1
                pruned = True
        if not pruned:
            break
    if serialized_digest_characters() > GRAPH_NODE_DIGEST_CHARACTER_LIMIT:
        # All variable-cardinality fields have been exhausted. This can occur
        # only with unusually long free-text fields, so retain their hashes and
        # shorter previews while preserving every required digest category.
        for key in (
            "proof_mechanism",
            "exact_unresolved_gap",
            "exact_statement_or_result",
        ):
            value = digest.get(key)
            if isinstance(value, str):
                digest[key] = _bounded_digest_text(value, character_limit=800)
        counterexample = digest.get("counterexample_target_and_scope")
        if isinstance(counterexample, dict):
            for key in ("counterexample_or_obstruction", "scope"):
                value = counterexample.get(key)
                if isinstance(value, str):
                    counterexample[key] = _bounded_digest_text(value, character_limit=800)
        omitted_counts["free_text_compacted"] += 1
        serialized_digest_characters()
    if serialized_digest_characters() > GRAPH_NODE_DIGEST_CHARACTER_LIMIT:
        raise ValueError("typed graph-node digest exceeds its hard character limit")
    return digest


def _node_approach_family(
    node: GraphNode,
    *,
    assignment_families: Mapping[str, str],
    by_id: Mapping[str, GraphNode],
) -> str:
    explicit = node.metadata.get("matek_approach_family")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip().casefold()
    assignment_ids = [
        *_metadata_strings(node, "matek_assignment_ids"),
        *_metadata_strings(node, "matek_assignment_id"),
    ]
    for assignment_id in assignment_ids:
        family = assignment_families.get(assignment_id)
        if family:
            return family.casefold()
    for edge in node.relations:
        target = by_id.get(edge.target_id)
        if target is None or target.node_type is not NodeType.TASK:
            continue
        target_assignment = target.metadata.get("matek_assignment_id")
        if isinstance(target_assignment, str) and target_assignment in assignment_families:
            return assignment_families[target_assignment].casefold()
    return node.node_type.value


def _status_rank(node: GraphNode) -> int:
    epistemic = {
        EpistemicStatus.INCONSISTENT: 0,
        EpistemicStatus.CANDIDATE: 1,
        EpistemicStatus.PROVED_INFORMALLY: 2,
        EpistemicStatus.OPEN: 3,
        EpistemicStatus.CONJECTURED: 3,
        EpistemicStatus.STALE: 4,
        EpistemicStatus.AUDIT_PASSED: 5,
        EpistemicStatus.LEAN_VERIFIED: 6,
        EpistemicStatus.REFUTED: 7,
    }[node.epistemic_status]
    workflow = {
        WorkflowStatus.ACTIVE: 0,
        WorkflowStatus.IN_PROGRESS: 0,
        WorkflowStatus.QUEUED: 1,
        WorkflowStatus.COMPLETE: 2,
        WorkflowStatus.BLOCKED: 5,
        WorkflowStatus.DORMANT: 6,
        WorkflowStatus.ABANDONED: 7,
        WorkflowStatus.SUPERSEDED: 8,
    }[node.workflow_status]
    return epistemic * 10 + workflow


def _graph_distances(nodes: Sequence[GraphNode], focal_node_ids: Iterable[str]) -> dict[str, int]:
    by_id = {node.matek_id: node for node in nodes}
    adjacency: dict[str, set[str]] = defaultdict(set)
    for node in nodes:
        for edge in node.relations:
            if edge.target_id not in by_id:
                continue
            adjacency[node.matek_id].add(edge.target_id)
            adjacency[edge.target_id].add(node.matek_id)
    distances: dict[str, int] = {}
    queue: deque[tuple[str, int]] = deque(
        (node_id, 0) for node_id in dict.fromkeys(focal_node_ids) if node_id in by_id
    )
    while queue:
        node_id, distance = queue.popleft()
        if node_id in distances:
            continue
        distances[node_id] = distance
        queue.extend((neighbor, distance + 1) for neighbor in sorted(adjacency[node_id]))
    return distances


def _semantic_node_tiebreaker(node: GraphNode, by_id: Mapping[str, GraphNode]) -> str:
    relations = [
        {
            "relation": edge.relation.value,
            "target_type": by_id[edge.target_id].node_type.value
            if edge.target_id in by_id
            else None,
            "target_title": by_id[edge.target_id].title if edge.target_id in by_id else None,
        }
        for edge in node.relations
    ]
    value = {
        "node_type": node.node_type.value,
        "title": node.title,
        "epistemic_status": node.epistemic_status.value,
        "workflow_status": node.workflow_status.value,
        "statement_version": node.statement_version,
        "body": node.body,
        "tags": sorted(node.tags),
        "relations": sorted(
            relations,
            key=lambda item: (
                str(item["relation"]),
                str(item["target_type"]),
                str(item["target_title"]),
            ),
        ),
    }
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def rank_graph_evidence(
    *,
    nodes: Sequence[GraphNode],
    frontier_categories: Mapping[str, Sequence[str]],
    requested_node_ids: Sequence[str],
    focal_node_ids: Sequence[str],
    assignment_families: Mapping[str, str],
    current_run_id: str | None,
) -> list[RankedGraphEvidence]:
    """Rank graph evidence by scientific state, using IDs only as the last tie-breaker."""

    by_id = {node.matek_id: node for node in nodes}
    category_memberships: dict[str, list[str]] = defaultdict(list)
    for category in _FRONTIER_CATEGORY_ORDER:
        for node_id in frontier_categories.get(category, ()):
            if node_id in by_id and category not in category_memberships[node_id]:
                category_memberships[node_id].append(category)
    candidate_ids = list(
        dict.fromkeys(
            [
                *requested_node_ids,
                *(
                    node_id
                    for category in _FRONTIER_CATEGORY_ORDER
                    for node_id in frontier_categories.get(category, ())
                ),
            ]
        )
    )
    candidate_nodes = [by_id[node_id] for node_id in candidate_ids if node_id in by_id]
    distances = _graph_distances(nodes, focal_node_ids)

    base: dict[str, tuple[int, str, int, int, int, str, str]] = {}
    families: dict[str, str] = {}
    for node in candidate_nodes:
        categories = category_memberships[node.matek_id]
        distance = distances.get(node.matek_id, 1_000_000)
        if node.matek_id in requested_node_ids:
            tier, reason = 0, "explicitly requested graph node"
        elif any(
            category in {"unresolved_contradictions", "missing_dependencies"}
            for category in categories
        ):
            tier, reason = 1, "unresolved contradiction or missing dependency"
        elif "high_value_tasks" in categories:
            tier, reason = 2, "active or high-value task"
        elif "candidate_proofs_awaiting_audit" in categories:
            tier, reason = 3, "candidate proof awaiting audit"
        elif "unresolved_claims" in categories and distance <= 3:
            tier, reason = 4, "unresolved claim near the exact target or active assignment"
        elif not (
            node.epistemic_status is EpistemicStatus.REFUTED
            or node.workflow_status
            in {
                WorkflowStatus.BLOCKED,
                WorkflowStatus.DORMANT,
                WorkflowStatus.ABANDONED,
                WorkflowStatus.SUPERSEDED,
            }
            or "prior_runs" in categories
            or "refuted_or_unproductive_routes" in categories
            or "blocked_approaches" in categories
        ):
            tier, reason = 5, "recently changed, diverse relevant evidence"
        else:
            tier, reason = 6, "blocked, refuted, dormant, or historical route"
        recency = int(node.updated_at.timestamp() * 1_000_000)
        status_rank = _status_rank(node)
        family = _node_approach_family(node, assignment_families=assignment_families, by_id=by_id)
        families[node.matek_id] = family
        base[node.matek_id] = (
            tier,
            reason,
            distance,
            status_rank,
            recency,
            family,
            _semantic_node_tiebreaker(node, by_id),
        )

    family_counts: dict[str, int] = defaultdict(int)
    remaining = list(candidate_nodes)
    ranked: list[RankedGraphEvidence] = []
    while remaining:
        node = min(
            remaining,
            key=lambda item: (
                base[item.matek_id][0],
                base[item.matek_id][2],
                base[item.matek_id][3],
                family_counts[families[item.matek_id]],
                -base[item.matek_id][4],
                base[item.matek_id][6],
                item.matek_id,
            ),
        )
        remaining.remove(node)
        tier, reason, distance, status_rank, recency, family, semantic_tiebreaker = base[
            node.matek_id
        ]
        diversity_rank = family_counts[family]
        family_counts[family] += 1
        ranked.append(
            RankedGraphEvidence(
                node=node,
                priority=tier,
                inclusion_reason=reason,
                frontier_categories=tuple(category_memberships[node.matek_id]),
                priority_score={
                    "tier": tier,
                    "graph_distance": distance,
                    "status_rank": status_rank,
                    "updated_at_epoch_microseconds": recency,
                    "recently_changed_in_current_run": (
                        current_run_id is not None and node.last_modified_run == current_run_id
                    ),
                    "approach_family": family,
                    "approach_family_prior_selections": diversity_rank,
                    "semantic_tiebreaker": semantic_tiebreaker,
                },
                selection_rank=len(ranked),
                approach_family=family,
            )
        )
    return ranked


class CoordinatorContextBuilder:
    """Build a complete small context or a prioritized compact working set."""

    def __init__(
        self,
        *,
        configured_character_limit: int,
        effective_character_limit: int | None = None,
        provider_input_characters: Callable[[str], int] | None = None,
        graph_summary_character_limit: int = 60_000,
        unrequested_full_graph_nodes_character_limit: int = (
            DEFAULT_UNREQUESTED_FULL_GRAPH_NODES_CHARACTER_LIMIT
        ),
        maximum_graph_summary_items: int = 128,
    ) -> None:
        if configured_character_limit <= 0:
            raise ValueError("coordinator context character limit must be positive")
        self.configured_character_limit = configured_character_limit
        self.effective_character_limit = effective_character_limit or configured_character_limit
        if self.effective_character_limit <= 0:
            raise ValueError("effective coordinator context limit must be positive")
        if self.effective_character_limit > configured_character_limit:
            raise ValueError("effective coordinator limit cannot exceed its configured limit")
        if graph_summary_character_limit <= 0:
            raise ValueError("graph summary character limit must be positive")
        if unrequested_full_graph_nodes_character_limit <= 0:
            raise ValueError("unrequested full graph-node limit must be positive")
        if maximum_graph_summary_items <= 0:
            raise ValueError("maximum graph summary items must be positive")
        self.graph_summary_character_limit = graph_summary_character_limit
        self.unrequested_full_graph_nodes_character_limit = (
            unrequested_full_graph_nodes_character_limit
        )
        self.maximum_graph_summary_items = maximum_graph_summary_items
        self._provider_input_characters = provider_input_characters or len

    def _measure(self, payload: Mapping[str, object]) -> tuple[str, int]:
        serialized = serialize_coordinator_payload(payload)
        return serialized, self._provider_input_characters(serialized)

    @property
    def minimum_headroom_characters(self) -> int:
        """Return the normal safety margin below the configured provider ceiling."""

        return max(40_000, (self.configured_character_limit + 19) // 20)

    @property
    def packing_character_limit(self) -> int:
        """Target below the provider ceiling, further reduced after a rejection."""

        headroom_target = max(
            1,
            self.configured_character_limit - self.minimum_headroom_characters,
        )
        return min(self.effective_character_limit, headroom_target)

    @property
    def reserved_headroom_characters(self) -> int:
        """Report total slack between this generation and the configured ceiling."""

        return self.configured_character_limit - self.packing_character_limit

    @staticmethod
    def _section_characters(payload: Mapping[str, object]) -> dict[str, int]:
        """Measure each top-level field after canonical JSON serialization."""

        return {
            key: len(
                json.dumps(
                    {key: value},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            for key, value in payload.items()
        }

    @staticmethod
    def _catalog_descriptor(
        evidence: list[CoordinatorEvidenceItem],
        supplied: Mapping[str, object] | None,
    ) -> dict[str, object]:
        counts: dict[str, int] = defaultdict(int)
        for item in evidence:
            counts[item.reference.kind] += 1
        descriptor: dict[str, object] = {
            "descriptor_type": "full_artifact_catalog",
            "relative_path": None,
            "sha256": None,
            "total_count": len(evidence),
            "counts_by_kind": dict(sorted(counts.items())),
            "instruction": (
                "The complete authenticated catalog is durable at relative_path. Read it when "
                "the bounded entries below do not identify the needed evidence."
            ),
        }
        if supplied is not None:
            descriptor.update(dict(supplied))
            descriptor["descriptor_type"] = "full_artifact_catalog"
            descriptor["total_count"] = len(evidence)
            descriptor["counts_by_kind"] = dict(sorted(counts.items()))
        return descriptor

    @staticmethod
    def _graph_descriptor(graph_memory: Mapping[str, object]) -> dict[str, object]:
        overview = graph_memory.get("overview", {})
        overview_mapping = overview if isinstance(overview, Mapping) else {}
        return {
            "graph_root": graph_memory.get("graph_root"),
            "graph_revision": graph_memory.get("graph_revision"),
            "problem_id": graph_memory.get("problem_id"),
            "index_path": graph_memory.get("index_path"),
            "node_count": overview_mapping.get("node_count", graph_memory.get("node_count")),
            "edge_count": overview_mapping.get("edge_count", graph_memory.get("edge_count")),
            "review_required_before_delegation": graph_memory.get(
                "review_required_before_delegation", False
            ),
            "current_frontier_review_required": graph_memory.get(
                "current_frontier_review_required", True
            ),
            "resume_reconstruction": graph_memory.get("resume_reconstruction", False),
            "previous_coordinator_graph_revision": graph_memory.get(
                "previous_coordinator_graph_revision"
            ),
            "graph_changed_since_previous_coordinator_activation": graph_memory.get(
                "graph_changed_since_previous_coordinator_activation", False
            ),
            "overview": dict(overview_mapping),
            "instruction": graph_memory.get("instruction"),
            "retrieval_instruction": (
                "Use graph_node_summaries as the bounded working set. Read the validated graph "
                "index or a hash-bound node path only when deeper graph evidence is needed."
            ),
        }

    @staticmethod
    def _mandatory_payload(source: Mapping[str, object]) -> dict[str, object]:
        return {
            key: source[key]
            for key in ("compiled_prompt", "claim_contract", "exact_target_policy")
            if key in source
        }

    @staticmethod
    def _operational_controls(source: Mapping[str, object]) -> dict[str, object]:
        keys = {
            "coordinator_mode",
            "activation_context",
            "research_agent_hierarchy",
            "decision_id",
            "after_event_sequence",
            "initial_portfolio",
            "minimum_materially_diverse_initial_assignments",
            "maximum_open_assignments",
            "available_new_assignment_slots",
            "available_new_assignments_without_replacement",
            "refundable_unlaunched_assignment_count",
            "coordinator_headroom_borrowed_assignment_id",
            "maximum_new_assignments_this_decision",
            "replacement_rule",
            "maximum_concurrent_workers",
            "worker_web_search_enabled",
            "open_assignment_count",
            "remaining_coordinator_decisions_after_this_call",
            "remaining_model_calls_before_this_call",
        }
        return {key: source[key] for key in keys if key in source}

    @staticmethod
    def _decision_brief(
        payload: Mapping[str, object],
        *,
        included_full_count: int,
        omitted_evidence_count: int,
    ) -> dict[str, object]:
        assignments = payload.get("assignment_lifecycle", [])
        events = payload.get("unacknowledged_events", [])
        report_summaries = payload.get("report_summaries", [])
        graph_summaries = payload.get("graph_node_summaries", [])
        return {
            "brief_schema_version": 1,
            "after_event_sequence": payload.get("after_event_sequence"),
            "open_assignment_count": sum(
                isinstance(item, Mapping)
                and item.get("status") in {"queued", "running", "active", "in_progress"}
                for item in assignments
            )
            if isinstance(assignments, list)
            else 0,
            "current_delta_count": len(events) if isinstance(events, list) else 0,
            "included_full_evidence_count": included_full_count,
            "omitted_evidence_count": omitted_evidence_count,
            "ranked_report_summary_count": (
                len(report_summaries) if isinstance(report_summaries, list) else 0
            ),
            "ranked_graph_summary_count": (
                len(graph_summaries) if isinstance(graph_summaries, list) else 0
            ),
            "instruction": (
                "Base consequential actions only on the hash-bound full evidence visible in "
                "this activation. Request cited omitted evidence and defer the action otherwise."
            ),
        }

    @staticmethod
    def _schema_v3_payload(payload: Mapping[str, object]) -> dict[str, object]:
        result = dict(payload)
        result["coordinator_payload_schema_version"] = COORDINATOR_PAYLOAD_SCHEMA_VERSION
        result.setdefault(
            "context_contract",
            {
                "raw_evidence_is_authoritative": True,
                "references_are_bound_to_frozen_sha256": True,
                "consequential_actions_require_supporting_evidence_ids": True,
                "consequential_actions": [
                    "candidate_packaging",
                    "contradiction_resolution",
                    "promising_branch_retirement",
                ],
                "omitted_cited_evidence_requires_retrieval_only_decision": True,
                "request_omitted_evidence_with": [
                    "requested_artifact_ids",
                    "requested_graph_node_ids",
                ],
            },
        )
        return result

    @staticmethod
    def _evidence_reference_for_value(
        value: Mapping[str, object],
        evidence_by_id: Mapping[str, CoordinatorEvidenceItem],
    ) -> CoordinatorArtifactReference | None:
        assignment_id = value.get("assignment_id")
        if isinstance(assignment_id, str):
            item = evidence_by_id.get(f"worker-report:{assignment_id}")
            if item is not None:
                return item.reference
        node = value.get("node")
        if isinstance(node, Mapping) and isinstance(node.get("matek_id"), str):
            item = evidence_by_id.get(f"graph-node:{node['matek_id']}")
            if item is not None:
                return item.reference
        matek_id = value.get("matek_id")
        if isinstance(matek_id, str):
            item = evidence_by_id.get(f"graph-node:{matek_id}")
            if item is not None:
                return item.reference
        return None

    @classmethod
    def _deduplicate_payload(
        cls,
        payload: dict[str, object],
        evidence: Sequence[CoordinatorEvidenceItem],
    ) -> tuple[dict[str, object], int]:
        """Replace substantive exact repeats with authenticated canonical references."""

        result = deepcopy(payload)
        evidence_by_id = {item.reference.artifact_id: item for item in evidence}
        canonical: dict[str, dict[str, object]] = {}
        minimum_content_characters = 128

        def locator(
            reference: CoordinatorArtifactReference, path: tuple[str | int, ...]
        ) -> dict[str, object]:
            return {
                "artifact_id": reference.artifact_id,
                "relative_path": reference.relative_path,
                "sha256": reference.sha256,
                "graph_revision": reference.graph_revision,
                "json_path": [str(item) for item in path],
            }

        def transform(
            value: object,
            *,
            reference: CoordinatorArtifactReference | None,
            path: tuple[str | int, ...],
            register_new: bool,
        ) -> object:
            if isinstance(value, str):
                if len(value) < minimum_content_characters:
                    return value
                prior = canonical.get(value)
                if prior is not None:
                    replacement = {"authenticated_content_reference": prior}
                    if len(
                        json.dumps(
                            replacement,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    ) < len(json.dumps(value, ensure_ascii=False)):
                        return replacement
                original_value = value
                for repeated, repeated_locator in sorted(
                    canonical.items(),
                    key=lambda item: len(item[0]),
                    reverse=True,
                ):
                    if repeated == value or repeated not in value:
                        continue
                    marker = (
                        "[MATEK-AUTHENTICATED-CONTENT-REFERENCE:"
                        + json.dumps(
                            repeated_locator,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "]"
                    )
                    if len(marker) < len(repeated):
                        value = value.replace(repeated, marker)
                if register_new and reference is not None:
                    canonical[value] = locator(reference, path)
                    for section_name, section_value in _markdown_sections(original_value).items():
                        if (
                            len(section_value) >= minimum_content_characters
                            and section_value in value
                        ):
                            section_locator = locator(reference, path)
                            section_locator["markdown_section"] = section_name
                            canonical.setdefault(section_value, section_locator)
                return value
            if isinstance(value, list):
                return [
                    transform(
                        item,
                        reference=reference,
                        path=(*path, index),
                        register_new=register_new,
                    )
                    for index, item in enumerate(value)
                ]
            if isinstance(value, Mapping):
                return {
                    str(key): transform(
                        item,
                        reference=reference,
                        path=(*path, str(key)),
                        register_new=register_new,
                    )
                    for key, item in value.items()
                }
            return value

        # Full evidence is canonical. Explicit retrievals come first, followed by
        # current reports and graph nodes. Registry/continuity/summary views only
        # point back to these authenticated representations.
        for section in (
            "requested_artifacts",
            "requested_graph_nodes",
            "visible_worker_reports",
            "full_graph_nodes",
        ):
            raw = result.get(section)
            if not isinstance(raw, list):
                continue
            transformed: list[object] = []
            for index, item in enumerate(raw):
                reference = (
                    cls._evidence_reference_for_value(item, evidence_by_id)
                    if isinstance(item, Mapping)
                    else None
                )
                transformed.append(
                    transform(
                        item,
                        reference=reference,
                        path=(section, index),
                        register_new=True,
                    )
                )
            result[section] = transformed

        for section in (
            "approach_registry",
            "approach_registry_index",
            "research_continuity",
            "research_continuity_index",
            "report_summaries",
            "graph_node_summaries",
        ):
            if section in result:
                result[section] = transform(
                    result[section],
                    reference=None,
                    path=(section,),
                    register_new=False,
                )

        before = len(serialize_coordinator_payload(payload))
        after = len(serialize_coordinator_payload(result))
        return result, max(before - after, 0)

    def build(
        self,
        *,
        decision_id: int,
        after_event_sequence: int,
        normal_payload: dict[str, object],
        compact_base: dict[str, object],
        indexed_base: dict[str, object] | None = None,
        events: list[dict[str, object]],
        assignment_table: list[dict[str, object]],
        report_evidence: list[CoordinatorEvidenceItem],
        graph_memory: dict[str, object] | None,
        graph_evidence: list[CoordinatorEvidenceItem] | None = None,
        requested_artifact_ids: list[str] | None = None,
        requested_graph_node_ids: list[str] | None = None,
        artifact_catalog_descriptor: dict[str, object] | None = None,
        force_compact: bool = False,
    ) -> CoordinatorContextBuild:
        requested_artifacts = list(dict.fromkeys(requested_artifact_ids or []))
        requested_graph_nodes = list(dict.fromkeys(requested_graph_node_ids or []))
        all_evidence = [*report_evidence, *(graph_evidence or [])]
        normal_candidate = self._schema_v3_payload(normal_payload)
        normal_candidate["decision_brief"] = self._decision_brief(
            normal_candidate,
            included_full_count=len(all_evidence),
            omitted_evidence_count=0,
        )
        _, normal_pre_dedup_characters = self._measure(normal_candidate)
        normal_candidate, redundant_characters = self._deduplicate_payload(
            normal_candidate, all_evidence
        )
        normal_serialized, normal_characters = self._measure(normal_candidate)
        if (
            not force_compact
            and not graph_evidence
            and normal_pre_dedup_characters <= self.packing_character_limit
        ):
            normal_included = [
                {
                    "artifact_id": item.reference.artifact_id,
                    "reason": "normal context includes complete current evidence",
                }
                for item in all_evidence
            ]
            manifest = self._manifest(
                decision_id=decision_id,
                after_event_sequence=after_event_sequence,
                mode="normal",
                payload=normal_candidate,
                serialized=normal_serialized,
                provider_characters=normal_characters,
                included=normal_included,
                omitted=[],
                aggregated=[],
                requested_artifacts=requested_artifacts,
                requested_graph_nodes=requested_graph_nodes,
                evidence=all_evidence,
                redundant_characters_removed=redundant_characters,
            )
            return CoordinatorContextBuild(normal_candidate, normal_serialized, manifest)

        compact_events, aggregated = _aggregate_repetitive_events(events)
        compact_probe = {
            **compact_base,
            "assignment_lifecycle": assignment_table,
            "unacknowledged_events": compact_events,
        }
        _, compact_probe_characters = self._measure(compact_probe)
        mode: Literal["compact", "indexed"] = (
            "indexed"
            if indexed_base is not None and compact_probe_characters > self.packing_character_limit
            else "compact"
        )
        source_base = (
            indexed_base if mode == "indexed" and indexed_base is not None else compact_base
        )
        return self._build_bounded(
            decision_id=decision_id,
            after_event_sequence=after_event_sequence,
            mode=mode,
            source_base=source_base,
            events=compact_events,
            assignment_table=assignment_table,
            evidence=all_evidence,
            graph_memory=graph_memory,
            aggregated=aggregated,
            requested_artifacts=requested_artifacts,
            requested_graph_nodes=requested_graph_nodes,
            artifact_catalog_descriptor=artifact_catalog_descriptor,
        )

    def _build_bounded(
        self,
        *,
        decision_id: int,
        after_event_sequence: int,
        mode: Literal["compact", "indexed"],
        source_base: dict[str, object],
        events: list[dict[str, object]],
        assignment_table: list[dict[str, object]],
        evidence: list[CoordinatorEvidenceItem],
        graph_memory: dict[str, object] | None,
        aggregated: list[dict[str, object]],
        requested_artifacts: list[str],
        requested_graph_nodes: list[str],
        artifact_catalog_descriptor: dict[str, object] | None,
    ) -> CoordinatorContextBuild:
        """Pack a compact working set while every cumulative section remains optional."""

        mandatory = self._mandatory_payload(source_base)
        _, mandatory_characters = self._measure(mandatory)
        if mandatory_characters > self.packing_character_limit:
            mandatory_fields = sorted(
                self._section_characters(mandatory).items(),
                key=lambda item: (-item[1], item[0]),
            )
            raise CoordinatorContextBudgetExhausted(
                limit=self.packing_character_limit,
                required=mandatory_characters,
                largest_fields=mandatory_fields,
            )

        payload: dict[str, object] = {
            "coordinator_payload_schema_version": COORDINATOR_PAYLOAD_SCHEMA_VERSION,
            **mandatory,
            **self._operational_controls(source_base),
            "context_mode": mode,
            "context_contract": {
                "raw_evidence_is_authoritative": True,
                "references_are_bound_to_frozen_sha256": True,
                "historical_state_is_an_index_not_canonical_evidence": mode == "indexed",
                "consequential_actions_require_supporting_evidence_ids": True,
                "consequential_actions": [
                    "candidate_packaging",
                    "contradiction_resolution",
                    "promising_branch_retirement",
                ],
                "omitted_cited_evidence_requires_retrieval_only_decision": True,
                "request_omitted_evidence_with": [
                    "requested_artifact_ids",
                    "requested_graph_node_ids",
                ],
            },
            "assignment_lifecycle": [],
            "unacknowledged_events": [],
            "report_summaries": [],
            "graph_node_summaries": [],
            "visible_worker_reports": [],
            "full_graph_nodes": [],
            "requested_artifacts": [],
            "requested_graph_nodes": [],
            "artifact_catalog": [],
            "indexed_omissions": [],
        }
        payload["decision_brief"] = self._decision_brief(
            payload,
            included_full_count=0,
            omitted_evidence_count=len(evidence),
        )
        serialized, provider_characters = self._measure(payload)
        if provider_characters > self.packing_character_limit:
            # Operational scalar controls are transport metadata, not grounds for
            # misreporting the exact prompt/claim as irreducible.
            minimum_controls: dict[str, object] = {
                key: source_base[key]
                for key in (
                    "activation_context",
                    "research_agent_hierarchy",
                    "decision_id",
                    "after_event_sequence",
                    "initial_portfolio",
                    "minimum_materially_diverse_initial_assignments",
                    "maximum_new_assignments_this_decision",
                )
                if key in source_base
            }
            payload = {
                "coordinator_payload_schema_version": COORDINATOR_PAYLOAD_SCHEMA_VERSION,
                **mandatory,
                **minimum_controls,
                "context_mode": mode,
                "context_contract": payload["context_contract"],
                "assignment_lifecycle": [],
                "unacknowledged_events": [],
                "report_summaries": [],
                "graph_node_summaries": [],
                "visible_worker_reports": [],
                "full_graph_nodes": [],
                "requested_artifacts": [],
                "requested_graph_nodes": [],
                "artifact_catalog": [],
                "indexed_omissions": [],
            }
            payload["decision_brief"] = self._decision_brief(
                payload,
                included_full_count=0,
                omitted_evidence_count=len(evidence),
            )
            serialized, provider_characters = self._measure(payload)
            if provider_characters > self.packing_character_limit:
                raise CoordinatorContextBudgetExhausted(
                    limit=self.packing_character_limit,
                    required=provider_characters,
                    diagnostic="OPERATIONAL_CONTEXT_TOO_LARGE",
                )
        packing_limit = self.packing_character_limit

        omitted_state: list[dict[str, object]] = []

        def integer_value(value: object) -> int:
            return value if isinstance(value, int) and not isinstance(value, bool) else 0

        section_caps = {
            "base": max(4_000, min(60_000, packing_limit // 10)),
            "assignment_lifecycle": max(8_000, min(120_000, packing_limit // 5)),
            "unacknowledged_events": max(8_000, min(160_000, packing_limit // 4)),
            "report_summaries": max(8_000, min(200_000, packing_limit // 4)),
            "graph_node_summaries": max(
                1_000,
                min(
                    self.graph_summary_character_limit,
                    80_000,
                    max(1_000, packing_limit // 8),
                ),
            ),
            "requested_evidence": packing_limit,
            "current_full_reports": max(8_000, min(400_000, packing_limit // 2)),
            "full_graph_nodes": min(
                self.unrequested_full_graph_nodes_character_limit,
                max(1_000, packing_limit),
            ),
            "artifact_catalog": max(8_000, min(80_000, packing_limit // 8)),
        }

        def field_characters(key: str) -> int:
            return self._section_characters({key: payload.get(key)})[key]

        def try_set(key: str, value: object, *, cap: int) -> bool:
            nonlocal serialized, provider_characters
            previous = payload.get(key)
            existed = key in payload
            payload[key] = value
            candidate_serialized, candidate_characters = self._measure(payload)
            if field_characters(key) > cap or candidate_characters > packing_limit:
                if existed:
                    payload[key] = previous
                else:
                    payload.pop(key, None)
                return False
            serialized, provider_characters = candidate_serialized, candidate_characters
            return True

        excluded_base_keys = {
            *mandatory,
            *self._operational_controls(source_base),
            "knowledge_graph_memory",
        }
        essential_state_keys = (
            "filesystem_retrieval",
            "latest_candidate_state",
            "audit_recovery_state",
            "audit_repair_obligations",
            "latest_independent_audits",
            "latest_final_judge_verdict",
            "scheduler_state_index",
        )
        summary_state_keys = (
            "approach_registry_index",
            "approach_registry",
            "research_continuity_index",
            "research_continuity",
            "exact_target_policy",
        )
        for key in essential_state_keys:
            if key not in source_base or key in excluded_base_keys:
                continue
            if not try_set(key, source_base[key], cap=section_caps["base"]):
                omitted_state.append(
                    {
                        "section": key,
                        "included": 0,
                        "omitted": 1,
                        "recovery": "Read the canonical scheduler ledger or graph index.",
                    }
                )

        if graph_memory is not None:
            try_set(
                "knowledge_graph_memory",
                self._graph_descriptor(graph_memory),
                cap=section_caps["base"],
            )

        status_priority = {"running": 0, "queued": 1, "completed": 2}
        ordered_assignments = sorted(
            assignment_table,
            key=lambda assignment_item: (
                status_priority.get(str(assignment_item.get("status")), 3),
                -integer_value(assignment_item.get("completed_event_sequence")),
                str(assignment_item.get("assignment_id", "")),
            ),
        )
        lifecycle = payload["assignment_lifecycle"]
        assert isinstance(lifecycle, list)
        open_assignments = [
            item for item in ordered_assignments if item.get("status") in {"running", "queued"}
        ]
        for assignment_item in open_assignments:
            lifecycle.append(assignment_item)
            candidate_serialized, candidate_characters = self._measure(payload)
            if (
                field_characters("assignment_lifecycle") > section_caps["assignment_lifecycle"]
                or candidate_characters > packing_limit
            ):
                lifecycle.pop()
                continue
            serialized, provider_characters = candidate_serialized, candidate_characters

        indexed_events = [_compact_event(event) for event in events]
        selected_events: list[dict[str, object]] = []
        for event in reversed(indexed_events):
            selected_events.append(event)
            selected_events.sort(
                key=lambda event_item: integer_value(
                    event_item.get("sequence", event_item.get("first_sequence", 0))
                )
            )
            payload["unacknowledged_events"] = selected_events
            candidate_serialized, candidate_characters = self._measure(payload)
            if (
                field_characters("unacknowledged_events") > section_caps["unacknowledged_events"]
                or candidate_characters > packing_limit
            ):
                selected_events.remove(event)
                payload["unacknowledged_events"] = selected_events
                continue
            serialized, provider_characters = candidate_serialized, candidate_characters
        if len(selected_events) < len(indexed_events):
            omitted_state.append(
                {
                    "section": "unacknowledged_events",
                    "included": len(selected_events),
                    "omitted": len(indexed_events) - len(selected_events),
                    "recovery": "Read immutable research/events/*.json evidence by sequence.",
                }
            )

        ordered = sorted(
            evidence,
            key=lambda evidence_item: (
                evidence_item.priority,
                evidence_item.selection_rank,
                evidence_item.reference.artifact_id,
            ),
        )

        def evidence_requested(evidence_item: CoordinatorEvidenceItem) -> bool:
            reference = evidence_item.reference
            return reference.artifact_id in requested_artifacts or (
                reference.graph_node_id is not None
                and reference.graph_node_id in requested_graph_nodes
            )

        included: list[dict[str, str]] = []
        included_ids: set[str] = set()
        summary_ids: set[str] = set()
        unrequested_graph_node_count = 0

        def add_full_evidence(evidence_item: CoordinatorEvidenceItem) -> bool:
            nonlocal serialized, provider_characters, unrequested_graph_node_count
            reference = evidence_item.reference
            requested = evidence_requested(evidence_item)
            if reference.kind == "graph_node":
                key = "requested_graph_nodes" if requested else "full_graph_nodes"
            else:
                key = "requested_artifacts" if requested else "visible_worker_reports"
            if key == "full_graph_nodes" and unrequested_graph_node_count >= 24:
                return False
            current = payload[key]
            assert isinstance(current, list)
            current.append(evidence_item.full_content)
            candidate_serialized, candidate_characters = self._measure(payload)
            section_cap = (
                section_caps["requested_evidence"]
                if requested
                else section_caps["full_graph_nodes"]
                if key == "full_graph_nodes"
                else section_caps["current_full_reports"]
            )
            if field_characters(key) > section_cap or candidate_characters > packing_limit:
                current.pop()
                return False
            serialized, provider_characters = candidate_serialized, candidate_characters
            if key == "full_graph_nodes":
                unrequested_graph_node_count += 1
            included_ids.add(reference.artifact_id)
            included.append(
                {
                    "artifact_id": reference.artifact_id,
                    "reason": evidence_item.inclusion_reason,
                }
            )
            return True

        # Explicit retrieval requests are serviced before lower-priority history.
        for evidence_item in ordered:
            if evidence_requested(evidence_item):
                add_full_evidence(evidence_item)

        # Newly completed and candidate-producing reports are canonical full
        # evidence before any summary or historical view is admitted.
        for evidence_item in ordered:
            if (
                not evidence_requested(evidence_item)
                and evidence_item.reference.kind != "graph_node"
                and evidence_item.priority <= 4
            ):
                add_full_evidence(evidence_item)

        # A small amount of scientifically ranked graph evidence may be useful in
        # full. Tier-six dormant/history nodes are summary-only unless requested.
        for evidence_item in ordered:
            if (
                not evidence_requested(evidence_item)
                and evidence_item.reference.kind == "graph_node"
                and evidence_item.priority <= 5
            ):
                add_full_evidence(evidence_item)

        # Registry and continuity views are ranked summaries, not prerequisites for
        # explicit retrieval or newly completed canonical evidence.
        for key in summary_state_keys:
            if key not in source_base or key in excluded_base_keys:
                continue
            if not try_set(key, source_base[key], cap=section_caps["base"]):
                omitted_state.append(
                    {
                        "section": key,
                        "included": 0,
                        "omitted": 1,
                        "recovery": "Read the canonical scheduler ledger or graph index.",
                    }
                )

        # Scientific summaries precede any optional historical full evidence.
        graph_summary_count = 0
        for evidence_item in ordered:
            key = (
                "graph_node_summaries"
                if evidence_item.reference.kind == "graph_node"
                else "report_summaries"
            )
            if (
                key == "graph_node_summaries"
                and graph_summary_count >= self.maximum_graph_summary_items
            ):
                continue
            current = payload[key]
            assert isinstance(current, list)
            current.append(evidence_item.summary)
            candidate_serialized, candidate_characters = self._measure(payload)
            section_cap = (
                section_caps["graph_node_summaries"]
                if key == "graph_node_summaries"
                else section_caps["report_summaries"]
            )
            if field_characters(key) > section_cap or candidate_characters > packing_limit:
                current.pop()
                continue
            serialized, provider_characters = candidate_serialized, candidate_characters
            summary_ids.add(evidence_item.reference.artifact_id)
            if key == "graph_node_summaries":
                graph_summary_count += 1

        # The exhaustive catalog remains durable on disk. Inline only references tied
        # to current work and a single authenticated descriptor for everything else.
        catalog = payload["artifact_catalog"]
        assert isinstance(catalog, list)
        descriptor = self._catalog_descriptor(evidence, artifact_catalog_descriptor)
        catalog.append(descriptor)
        candidate_serialized, candidate_characters = self._measure(payload)
        descriptor_included = not (
            field_characters("artifact_catalog") > section_caps["artifact_catalog"]
            or candidate_characters > packing_limit
        )
        if descriptor_included:
            serialized, provider_characters = candidate_serialized, candidate_characters
        else:
            catalog.pop()
        active_assignment_ids = {
            str(item.get("assignment_id"))
            for item in assignment_table
            if item.get("status") in {"running", "queued"}
        }
        event_assignment_ids = {
            str(item.get("assignment_id"))
            for item in events
            if isinstance(item.get("assignment_id"), str)
        }
        high_priority_catalog = [
            item
            for item in ordered
            if evidence_requested(item)
            or (item.reference.kind == "graph_node" and item.priority <= 5)
            or (item.reference.kind != "graph_node" and item.priority < 10)
            or item.reference.assignment_id in active_assignment_ids
            or item.reference.assignment_id in event_assignment_ids
            or item.reference.kind in {"candidate", "audit"}
        ]
        for evidence_item in high_priority_catalog:
            insert_at = len(catalog) - 1 if descriptor_included else len(catalog)
            catalog.insert(insert_at, evidence_item.reference.model_dump(mode="json"))
            candidate_serialized, candidate_characters = self._measure(payload)
            if (
                field_characters("artifact_catalog") > section_caps["artifact_catalog"]
                or candidate_characters > packing_limit
            ):
                catalog.pop(insert_at)
                continue
            serialized, provider_characters = candidate_serialized, candidate_characters

        historical_assignments = [
            item for item in ordered_assignments if item.get("status") not in {"running", "queued"}
        ]
        for assignment_item in historical_assignments:
            lifecycle.append(assignment_item)
            candidate_serialized, candidate_characters = self._measure(payload)
            if (
                field_characters("assignment_lifecycle") > section_caps["assignment_lifecycle"]
                or candidate_characters > packing_limit
            ):
                lifecycle.pop()
                continue
            serialized, provider_characters = candidate_serialized, candidate_characters
        if len(lifecycle) < len(ordered_assignments):
            omitted_state.append(
                {
                    "section": "assignment_lifecycle",
                    "included": len(lifecycle),
                    "omitted": len(ordered_assignments) - len(lifecycle),
                    "recovery": "Use report summaries and the authenticated scheduler index.",
                }
            )

        omitted = [
            evidence_item.reference
            for evidence_item in evidence
            if evidence_item.reference.artifact_id not in included_ids
        ]
        payload["indexed_omissions"] = omitted_state
        candidate_serialized, candidate_characters = self._measure(payload)
        if candidate_characters <= packing_limit:
            serialized, provider_characters = candidate_serialized, candidate_characters
        elif omitted_state:
            payload["indexed_omissions"] = [
                {
                    "section": "multiple_indexed_sections",
                    "omitted": sum(integer_value(item.get("omitted")) for item in omitted_state),
                    "recovery": "Inspect the context manifest and canonical scheduler ledger.",
                }
            ]
            candidate_serialized, candidate_characters = self._measure(payload)
            if candidate_characters <= packing_limit:
                serialized, provider_characters = candidate_serialized, candidate_characters
            else:
                payload["indexed_omissions"] = []
                serialized, provider_characters = self._measure(payload)
        payload["decision_brief"] = self._decision_brief(
            payload,
            included_full_count=len(included_ids),
            omitted_evidence_count=len(omitted),
        )
        payload, redundant_characters = self._deduplicate_payload(payload, evidence)
        serialized, provider_characters = self._measure(payload)
        if provider_characters > packing_limit:
            # The brief has a reserved bounded shape; this is defensive for exotic
            # provider framing functions whose measurements are not payload-linear.
            payload["decision_brief"] = {
                "brief_schema_version": 1,
                "after_event_sequence": after_event_sequence,
                "instruction": "Request omitted full evidence before consequential action.",
            }
            payload, redundant_characters = self._deduplicate_payload(payload, evidence)
            serialized, provider_characters = self._measure(payload)
        manifest = self._manifest(
            decision_id=decision_id,
            after_event_sequence=after_event_sequence,
            mode=mode,
            payload=payload,
            serialized=serialized,
            provider_characters=provider_characters,
            included=included,
            omitted=omitted,
            aggregated=aggregated,
            requested_artifacts=requested_artifacts,
            requested_graph_nodes=requested_graph_nodes,
            omitted_state=omitted_state,
            evidence=evidence,
            redundant_characters_removed=redundant_characters,
        )
        return CoordinatorContextBuild(payload, serialized, manifest)

    def _manifest(
        self,
        *,
        decision_id: int,
        after_event_sequence: int,
        mode: Literal["normal", "compact", "indexed"],
        payload: Mapping[str, object],
        serialized: str,
        provider_characters: int,
        included: list[dict[str, str]],
        omitted: list[CoordinatorArtifactReference],
        aggregated: list[dict[str, object]],
        requested_artifacts: list[str],
        requested_graph_nodes: list[str],
        omitted_state: list[dict[str, object]] | None = None,
        evidence: Sequence[CoordinatorEvidenceItem] = (),
        redundant_characters_removed: int = 0,
    ) -> CoordinatorContextManifest:
        included_ids = {item["artifact_id"] for item in included}
        raw_report_summaries = payload.get("report_summaries", [])
        report_summaries = raw_report_summaries if isinstance(raw_report_summaries, list) else []
        raw_graph_summaries = payload.get("graph_node_summaries", [])
        graph_summaries = raw_graph_summaries if isinstance(raw_graph_summaries, list) else []
        report_summary_ids = {
            f"worker-report:{assignment_id}"
            for item in report_summaries
            if isinstance(item, Mapping)
            and isinstance((assignment_id := item.get("assignment_id")), str)
        }
        graph_summary_ids = {
            f"graph-node:{node_id}"
            for item in graph_summaries
            if isinstance(item, Mapping) and isinstance((node_id := item.get("matek_id")), str)
        }
        selection: list[dict[str, object]] = []
        for item in sorted(
            evidence,
            key=lambda value: (
                value.priority,
                value.selection_rank,
                value.reference.artifact_id,
            ),
        ):
            reference = item.reference
            requested = reference.artifact_id in requested_artifacts or (
                reference.graph_node_id is not None
                and reference.graph_node_id in requested_graph_nodes
            )
            full_section = (
                "requested_graph_nodes"
                if requested and reference.kind == "graph_node"
                else "requested_artifacts"
                if requested
                else "full_graph_nodes"
                if reference.kind == "graph_node"
                else "visible_worker_reports"
            )
            summary_section = (
                "graph_node_summaries" if reference.kind == "graph_node" else "report_summaries"
            )
            selected_summary = reference.artifact_id in (
                graph_summary_ids if reference.kind == "graph_node" else report_summary_ids
            )
            selection.append(
                {
                    "artifact_id": reference.artifact_id,
                    "kind": reference.kind,
                    "priority": item.priority,
                    "selection_rank": item.selection_rank,
                    "selection_reason": item.inclusion_reason,
                    "frontier_categories": item.frontier_categories,
                    "priority_score": item.priority_score,
                    "approach_family": item.approach_family,
                    "requested": requested,
                    "selected_full": reference.artifact_id in included_ids,
                    "selected_summary": selected_summary,
                    "full_section": (
                        full_section if reference.artifact_id in included_ids else None
                    ),
                    "summary_section": summary_section if selected_summary else None,
                    "serialized_full_characters": len(
                        json.dumps(
                            item.full_content,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    ),
                    "serialized_summary_characters": len(
                        json.dumps(
                            item.summary,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    ),
                }
            )
        payload_schema = payload.get("coordinator_payload_schema_version")
        payload_schema_version = (
            payload_schema
            if isinstance(payload_schema, int) and not isinstance(payload_schema, bool)
            else 2
        )
        section_characters = self._section_characters(payload)
        return CoordinatorContextManifest(
            schema_version=3,
            payload_schema_version=payload_schema_version,
            section_order_version=(
                COORDINATOR_SECTION_ORDER_VERSION
                if payload_schema_version == COORDINATOR_PAYLOAD_SCHEMA_VERSION
                else 0
            ),
            decision_id=decision_id,
            after_event_sequence=after_event_sequence,
            mode=mode,
            configured_character_limit=self.configured_character_limit,
            effective_character_limit=self.effective_character_limit,
            packing_character_limit=self.packing_character_limit,
            reserved_headroom_characters=self.reserved_headroom_characters,
            serialized_payload_characters=len(serialized),
            serialized_provider_input_characters=provider_characters,
            serialized_section_characters=section_characters,
            estimated_input_tokens=(provider_characters + 3) // 4,
            payload_sha256=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            included_full_artifacts=included,
            omitted_artifacts=omitted,
            aggregated_event_groups=aggregated,
            requested_artifact_ids=requested_artifacts,
            requested_graph_node_ids=requested_graph_nodes,
            omitted_state_sections=list(omitted_state or []),
            evidence_selection=selection,
            section_positions=coordinator_section_positions(payload),
            unused_headroom_characters=max(
                self.configured_character_limit - provider_characters, 0
            ),
            redundant_characters_removed=redundant_characters_removed,
            unrequested_full_graph_nodes_characters=section_characters.get("full_graph_nodes", 0),
        )


__all__ = [
    "COORDINATOR_PAYLOAD_SCHEMA_VERSION",
    "COORDINATOR_SECTION_ORDER",
    "COORDINATOR_SECTION_ORDER_VERSION",
    "DEFAULT_UNREQUESTED_FULL_GRAPH_NODES_CHARACTER_LIMIT",
    "GRAPH_NODE_DIGEST_CHARACTER_LIMIT",
    "CoordinatorArtifactReference",
    "CoordinatorContextBudgetExhausted",
    "CoordinatorContextBuild",
    "CoordinatorContextBuilder",
    "CoordinatorContextManifest",
    "CoordinatorEvidenceItem",
    "RankedGraphEvidence",
    "coordinator_section_positions",
    "graph_node_typed_digest",
    "rank_graph_evidence",
    "serialize_coordinator_payload",
]
