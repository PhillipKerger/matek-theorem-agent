"""Deterministic, evidence-preserving context construction for research coordination."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def serialize_coordinator_payload(payload: Mapping[str, object]) -> str:
    """Return the exact canonical JSON sent as the coordinator's stage input."""

    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


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


class CoordinatorContextManifest(BaseModel):
    """Reproducible account of one exact provider working set."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1, 2] = 2
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


class CoordinatorContextBuilder:
    """Build a complete small context or a prioritized compact working set."""

    def __init__(
        self,
        *,
        configured_character_limit: int,
        effective_character_limit: int | None = None,
        provider_input_characters: Callable[[str], int] | None = None,
        graph_summary_character_limit: int = 60_000,
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
        self.graph_summary_character_limit = graph_summary_character_limit
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
        return {key: source[key] for key in ("compiled_prompt", "claim_contract") if key in source}

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
        normal_serialized, normal_characters = self._measure(normal_payload)
        if (
            not force_compact
            and not graph_evidence
            and normal_characters <= self.packing_character_limit
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
                payload=normal_payload,
                serialized=normal_serialized,
                provider_characters=normal_characters,
                included=normal_included,
                omitted=[],
                aggregated=[],
                requested_artifacts=requested_artifacts,
                requested_graph_nodes=requested_graph_nodes,
            )
            return CoordinatorContextBuild(normal_payload, normal_serialized, manifest)

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
            **mandatory,
            **self._operational_controls(source_base),
            "context_mode": mode,
            "context_contract": {
                "raw_evidence_is_authoritative": True,
                "references_are_bound_to_frozen_sha256": True,
                "historical_state_is_an_index_not_canonical_evidence": mode == "indexed",
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
            "full_evidence": max(8_000, min(400_000, packing_limit // 2)),
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
        for key in (
            "filesystem_retrieval",
            "latest_candidate_state",
            "audit_recovery_state",
            "audit_repair_obligations",
            "latest_independent_audits",
            "latest_final_judge_verdict",
            "approach_registry_index",
            "approach_registry",
            "research_continuity_index",
            "research_continuity",
            "scheduler_state_index",
            "exact_target_policy",
        ):
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

        def add_full_evidence(evidence_item: CoordinatorEvidenceItem) -> None:
            nonlocal serialized, provider_characters
            reference = evidence_item.reference
            requested = evidence_requested(evidence_item)
            if reference.kind == "graph_node":
                key = "requested_graph_nodes" if requested else "full_graph_nodes"
            else:
                key = "requested_artifacts" if requested else "visible_worker_reports"
            current = payload[key]
            assert isinstance(current, list)
            current.append(evidence_item.full_content)
            candidate_serialized, candidate_characters = self._measure(payload)
            if (
                field_characters(key) > section_caps["full_evidence"]
                or candidate_characters > packing_limit
            ):
                current.pop()
                return
            serialized, provider_characters = candidate_serialized, candidate_characters
            included_ids.add(reference.artifact_id)
            included.append(
                {
                    "artifact_id": reference.artifact_id,
                    "reason": evidence_item.inclusion_reason,
                }
            )

        # Explicit retrieval requests are serviced before lower-priority history.
        for evidence_item in ordered:
            if evidence_requested(evidence_item):
                add_full_evidence(evidence_item)

        # Scientific summaries outrank closed assignment bookkeeping.
        for evidence_item in ordered:
            key = (
                "graph_node_summaries"
                if evidence_item.reference.kind == "graph_node"
                else "report_summaries"
            )
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

        # Then include high-priority complete evidence that still fits.
        for evidence_item in ordered:
            if not evidence_requested(evidence_item) and evidence_item.priority < 10:
                add_full_evidence(evidence_item)

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
            or item.priority < 10
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
    ) -> CoordinatorContextManifest:
        return CoordinatorContextManifest(
            decision_id=decision_id,
            after_event_sequence=after_event_sequence,
            mode=mode,
            configured_character_limit=self.configured_character_limit,
            effective_character_limit=self.effective_character_limit,
            packing_character_limit=self.packing_character_limit,
            reserved_headroom_characters=self.reserved_headroom_characters,
            serialized_payload_characters=len(serialized),
            serialized_provider_input_characters=provider_characters,
            serialized_section_characters=self._section_characters(payload),
            estimated_input_tokens=(provider_characters + 3) // 4,
            payload_sha256=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            included_full_artifacts=included,
            omitted_artifacts=omitted,
            aggregated_event_groups=aggregated,
            requested_artifact_ids=requested_artifacts,
            requested_graph_node_ids=requested_graph_nodes,
            omitted_state_sections=list(omitted_state or []),
        )


__all__ = [
    "CoordinatorArtifactReference",
    "CoordinatorContextBudgetExhausted",
    "CoordinatorContextBuild",
    "CoordinatorContextBuilder",
    "CoordinatorContextManifest",
    "CoordinatorEvidenceItem",
    "serialize_coordinator_payload",
]
