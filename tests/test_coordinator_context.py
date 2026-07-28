from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from matek_theorem_agent.coordinator_context import (
    COORDINATOR_SECTION_ORDER,
    GRAPH_NODE_DIGEST_CHARACTER_LIMIT,
    CoordinatorArtifactReference,
    CoordinatorContextBudgetExhausted,
    CoordinatorContextBuilder,
    CoordinatorEvidenceItem,
    graph_node_typed_digest,
    rank_graph_evidence,
    serialize_coordinator_payload,
)
from matek_theorem_agent.knowledge_graph import (
    ClaimType,
    EpistemicStatus,
    GraphEdge,
    GraphNode,
    NodeType,
    RelationType,
    WorkflowStatus,
)


def _report(index: int, *, priority: int, status: str = "progress") -> CoordinatorEvidenceItem:
    assignment_id = f"worker-{index:02d}"
    digest = f"{index:064x}"
    return CoordinatorEvidenceItem(
        reference=CoordinatorArtifactReference(
            artifact_id=f"worker-report:{assignment_id}",
            kind="worker_report",
            relative_path=f"research/workers/{assignment_id}.json",
            sha256=digest,
            assignment_id=assignment_id,
        ),
        summary={
            "assignment_id": assignment_id,
            "status": status,
            "mechanism": f"mechanism {index}",
            "formal_results": [f"Lemma {index}"],
            "counterexamples": [],
            "exact_gap": f"gap {index}",
            "dependencies": [],
            "path": f"research/workers/{assignment_id}.json",
            "sha256": digest,
        },
        full_content={
            "assignment_id": assignment_id,
            "status": status,
            "proof_content": (f"complete mathematical report {index} " * 800),
        },
        priority=priority,
        inclusion_reason=f"priority {priority}",
    )


def _graph_item(index: int, *, priority: int, content_size: int = 8_000) -> CoordinatorEvidenceItem:
    node_id = f"CLM-{index:08d}"
    digest = f"{index + 1:064x}"
    return CoordinatorEvidenceItem(
        reference=CoordinatorArtifactReference(
            artifact_id=f"graph-node:{node_id}",
            kind="graph_node",
            relative_path=f".matek/knowledge/problem/Claims/{node_id}.md",
            sha256=digest,
            graph_node_id=node_id,
            graph_revision="00000001-abcdef0123456789",
        ),
        summary={
            "matek_id": node_id,
            "title": f"Claim {index}",
            "frontier_categories": ["unresolved_claims"],
            "priority_score": {"tier": priority, "graph_distance": index},
            "typed_digest": {
                "exact_statement_or_result": f"Exact statement {index}",
                "provenance": {"sha256": digest},
            },
        },
        full_content={
            "reference": {"sha256": digest},
            "node": {
                "matek_id": node_id,
                "body": f"graph proof evidence {index} " * content_size,
            },
        },
        priority=priority,
        inclusion_reason=f"scientific tier {priority}",
        frontier_categories=["unresolved_claims"],
        priority_score={"tier": priority, "graph_distance": index},
        selection_rank=index,
        approach_family=f"family-{index % 4}",
    )


def test_large_coordinator_context_is_bounded_prioritized_and_addressable() -> None:
    reports = [
        _report(
            index,
            priority=(0 if index == 31 else 1 if index == 7 else 3 if index == 8 else 10),
            status="candidate_complete" if index == 7 else "progress",
        )
        for index in range(32)
    ]
    repeated_events = [
        {
            "schema_version": 1,
            "sequence": index + 1,
            "kind": "graph_mutation_rejected",
            "assignment_id": f"worker-{index:02d}",
            "artifact": f"issues/issue-{index:02d}.json",
            "detail": ["Repair the optional graph proposal."],
        }
        for index in range(12)
    ]
    base = {
        "compiled_prompt": "Exact unchanged research prompt.",
        "claim_contract": {"conclusion": "P"},
        "decision_id": 8,
        "after_event_sequence": 12,
    }
    normal = {
        **base,
        "unacknowledged_events": repeated_events,
        "visible_worker_reports": [item.full_content for item in reports],
    }
    builder = CoordinatorContextBuilder(
        configured_character_limit=120_000,
        provider_input_characters=lambda serialized: len(serialized) + 2_048,
    )

    built = builder.build(
        decision_id=8,
        after_event_sequence=12,
        normal_payload=normal,
        compact_base=base,
        events=repeated_events,
        assignment_table=[
            {
                "assignment_id": f"worker-{index:02d}",
                "status": "completed",
                "approach_family": f"family-{index % 8}",
                "objective": f"route {index}",
                "artifact_id": f"worker-report:worker-{index:02d}",
            }
            for index in range(32)
        ],
        report_evidence=reports,
        graph_memory=None,
        requested_artifact_ids=["worker-report:worker-31"],
    )

    assert built.manifest.mode == "compact"
    assert built.manifest.serialized_provider_input_characters <= 80_000
    assert built.manifest.reserved_headroom_characters == 40_000
    assert len(built.payload["report_summaries"]) == 32
    included_ids = [item["assignment_id"] for item in built.payload["visible_worker_reports"]]
    assert "worker-07" in included_ids
    requested = built.payload["requested_artifacts"]
    assert isinstance(requested, list)
    assert requested[0]["assignment_id"] == "worker-31"
    assert built.manifest.omitted_artifacts
    assert all(item.reference.relative_path and item.reference.sha256 for item in reports[0:1])
    aggregate = built.manifest.aggregated_event_groups[0]
    assert aggregate["count"] == 12
    assert len(aggregate["affected_assignment_ids"]) == 12
    assert len(aggregate["issue_paths"]) == 12


def test_immutable_prompt_contract_fails_truthfully_when_it_cannot_fit() -> None:
    builder = CoordinatorContextBuilder(configured_character_limit=100_000)
    mandatory = {
        "compiled_prompt": "mandatory theorem statement " * 8_000,
        "claim_contract": {"conclusion": "P"},
        "decision_id": 1,
        "after_event_sequence": 0,
    }

    try:
        builder.build(
            decision_id=1,
            after_event_sequence=0,
            normal_payload=mandatory,
            compact_base=mandatory,
            events=[],
            assignment_table=[],
            report_evidence=[],
            graph_memory=None,
            force_compact=True,
        )
    except CoordinatorContextBudgetExhausted as exc:
        assert exc.limit == 60_000
        assert exc.required > exc.limit
        assert "CONTEXT_BUDGET_EXHAUSTED" in str(exc)
        assert "MANDATORY_CONTEXT_TOO_LARGE" in str(exc)
        assert exc.largest_fields[0][0] == "compiled_prompt"
    else:  # pragma: no cover - the fixture must exceed its explicit hard budget
        raise AssertionError("mandatory oversized context unexpectedly fit")


def test_mandatory_context_cannot_consume_reserved_transport_headroom() -> None:
    builder = CoordinatorContextBuilder(configured_character_limit=800_000)
    mandatory = {
        "compiled_prompt": "x" * 765_000,
        "claim_contract": {"conclusion": "P"},
    }

    try:
        builder.build(
            decision_id=1,
            after_event_sequence=0,
            normal_payload=mandatory,
            compact_base=mandatory,
            events=[],
            assignment_table=[],
            report_evidence=[],
            graph_memory=None,
            force_compact=True,
        )
    except CoordinatorContextBudgetExhausted as exc:
        assert exc.diagnostic == "MANDATORY_CONTEXT_TOO_LARGE"
        assert exc.limit == 760_000
        assert exc.required > exc.limit
    else:  # pragma: no cover - the mandatory payload deliberately exceeds the packing target
        raise AssertionError("mandatory context consumed reserved transport headroom")


def test_oversized_scheduler_state_falls_back_to_bounded_indexed_context() -> None:
    builder = CoordinatorContextBuilder(configured_character_limit=100_000)
    base = {
        "compiled_prompt": "Exact unchanged research prompt.",
        "claim_contract": {"conclusion": "P"},
        "decision_id": 41,
        "after_event_sequence": 2_000,
    }
    assignments = [
        {
            "assignment_id": f"worker-{index:04d}",
            "status": "running" if index >= 1_995 else "completed",
            "approach_family": f"family-{index % 8}",
            "objective": "Large historical objective " * 20,
            "completed_event_sequence": index,
        }
        for index in range(2_000)
    ]
    events = [
        {
            "schema_version": 1,
            "sequence": index,
            "kind": "worker_report_accepted",
            "assignment_id": f"worker-{index:04d}",
            "artifact": f"workers/worker-{index:04d}.json",
            "artifact_sha256": f"{index:064x}",
            "detail": ["Detailed historical event prose " * 30],
        }
        for index in range(1, 2_001)
    ]

    built = builder.build(
        decision_id=41,
        after_event_sequence=2_000,
        normal_payload={**base, "assignment_lifecycle": assignments, "events": events},
        compact_base={**base, "large_registry": "registry " * 80_000},
        indexed_base={
            **base,
            "scheduler_state_index": {
                "assignment_count": len(assignments),
                "canonical_path": "research/coordinator/state.json",
            },
        },
        events=events,
        assignment_table=assignments,
        report_evidence=[],
        graph_memory=None,
        force_compact=True,
    )

    assert built.manifest.mode == "indexed"
    assert built.manifest.serialized_provider_input_characters <= 100_000
    assert built.payload["context_mode"] == "indexed"
    assert built.payload["scheduler_state_index"]["assignment_count"] == 2_000
    selected_events = built.payload["unacknowledged_events"]
    assert selected_events
    assert selected_events[-1]["sequence"] == 2_000
    lifecycle = built.payload["assignment_lifecycle"]
    assert lifecycle
    assert lifecycle[0]["status"] == "running"
    assert built.manifest.omitted_state_sections
    assert built.payload["indexed_omissions"]


def test_compact_catalog_prunes_833_old_entries_to_authenticated_descriptor() -> None:
    reports = [_report(index, priority=10) for index in range(833)]
    reports[17] = _report(17, priority=0)
    base = {
        "compiled_prompt": "Exact research prompt.",
        "claim_contract": {"conclusion": "P"},
        "decision_id": 9,
        "after_event_sequence": 833,
    }
    builder = CoordinatorContextBuilder(configured_character_limit=800_000)

    built = builder.build(
        decision_id=9,
        after_event_sequence=833,
        normal_payload={
            **base,
            "visible_worker_reports": [item.full_content for item in reports],
        },
        compact_base=base,
        indexed_base=base,
        events=[
            {
                "sequence": 833,
                "kind": "worker_report_accepted",
                "assignment_id": "worker-17",
            }
        ],
        assignment_table=[
            {
                "assignment_id": f"worker-{index:02d}",
                "status": "completed",
                "artifact_id": f"worker-report:worker-{index:02d}",
            }
            for index in range(833)
        ],
        report_evidence=reports,
        graph_memory=None,
        artifact_catalog_descriptor={
            "relative_path": "research/coordinator/artifact-catalogs/00000009.json",
            "sha256": "f" * 64,
        },
        force_compact=True,
    )

    assert built.manifest.mode in {"compact", "indexed"}
    assert built.manifest.serialized_provider_input_characters <= 760_000
    catalog = built.payload["artifact_catalog"]
    descriptor = next(
        item for item in catalog if item.get("descriptor_type") == "full_artifact_catalog"
    )
    assert descriptor["total_count"] == 833
    assert descriptor["counts_by_kind"] == {"worker_report": 833}
    assert descriptor["relative_path"].endswith("00000009.json")
    assert descriptor["sha256"] == "f" * 64
    assert len(catalog) < 100


def test_compact_graph_is_one_descriptor_plus_bounded_summaries() -> None:
    graph_evidence = []
    for index in range(100):
        digest = f"{index + 1:064x}"
        graph_evidence.append(
            CoordinatorEvidenceItem(
                reference=CoordinatorArtifactReference(
                    artifact_id=f"graph-node:CLM-{index:04d}",
                    kind="graph_node",
                    relative_path=f".matek/knowledge/problem/CLM-{index:04d}.md",
                    sha256=digest,
                    graph_node_id=f"CLM-{index:04d}",
                    graph_revision="00000001-abcdef0123456789",
                ),
                summary={
                    "matek_id": f"CLM-{index:04d}",
                    "title": f"Claim {index} " + ("summary " * 80),
                    "path": f".matek/knowledge/problem/CLM-{index:04d}.md",
                    "sha256": digest,
                },
                full_content={"node": {"matek_id": f"CLM-{index:04d}"}},
                priority=8,
                inclusion_reason="bounded graph frontier",
            )
        )
    base = {
        "compiled_prompt": "Exact research prompt.",
        "claim_contract": {"conclusion": "P"},
        "decision_id": 3,
        "after_event_sequence": 0,
    }
    graph_memory = {
        "graph_root": ".matek/knowledge/problem",
        "index_path": ".matek/knowledge/problem/graph-index.sqlite",
        "graph_revision": "00000001-abcdef0123456789",
        "problem_id": "PRB-1",
        "overview": {"node_count": 10_000, "edge_count": 25_000},
        "frontier": {"open_claims": [{"large": "duplicate " * 30_000}]},
    }
    builder = CoordinatorContextBuilder(
        configured_character_limit=800_000,
        graph_summary_character_limit=12_000,
    )

    built = builder.build(
        decision_id=3,
        after_event_sequence=0,
        normal_payload={**base, "knowledge_graph_memory": graph_memory},
        compact_base=base,
        indexed_base=base,
        events=[],
        assignment_table=[],
        report_evidence=[],
        graph_memory=graph_memory,
        graph_evidence=graph_evidence,
        force_compact=True,
    )

    memory = built.payload["knowledge_graph_memory"]
    assert memory["graph_root"] == ".matek/knowledge/problem"
    assert memory["node_count"] == 10_000
    assert memory["edge_count"] == 25_000
    assert "frontier" not in memory
    assert built.payload["graph_node_summaries"]
    graph_summary_characters = len(
        json.dumps(
            {"graph_node_summaries": built.payload["graph_node_summaries"]},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    assert graph_summary_characters <= 12_000
    assert built.manifest.serialized_provider_input_characters <= 760_000


def test_small_new_event_prunes_optional_history_instead_of_exhausting_context() -> None:
    reports = [_report(index, priority=10) for index in range(200)]
    base = {
        "compiled_prompt": "Exact research prompt.",
        "claim_contract": {"conclusion": "P"},
        "decision_id": 4,
        "after_event_sequence": 200,
    }
    builder = CoordinatorContextBuilder(configured_character_limit=800_000)

    first = builder.build(
        decision_id=4,
        after_event_sequence=200,
        normal_payload={**base, "reports": [item.full_content for item in reports]},
        compact_base=base,
        indexed_base=base,
        events=[],
        assignment_table=[],
        report_evidence=reports,
        graph_memory=None,
        force_compact=True,
    )
    second = builder.build(
        decision_id=4,
        after_event_sequence=200,
        normal_payload={**base, "reports": [item.full_content for item in reports]},
        compact_base=base,
        indexed_base=base,
        events=[
            {
                "schema_version": 1,
                "sequence": 201,
                "kind": "worker_report_accepted",
                "assignment_id": "worker-199",
                "detail": ["A small newly completed event."],
            }
        ],
        assignment_table=[],
        report_evidence=reports,
        graph_memory=None,
        force_compact=True,
    )

    assert first.manifest.serialized_provider_input_characters <= 760_000
    assert second.manifest.serialized_provider_input_characters <= 760_000
    assert second.payload["unacknowledged_events"][-1]["sequence"] == 201


def test_schema_v3_uses_intentional_top_level_order_and_replays_legacy_sorting() -> None:
    base = {
        "compiled_prompt": "Exact research prompt.",
        "claim_contract": {"conclusion": "P"},
        "exact_target_policy": {"acceptance_requires_exact_claim_contract": True},
        "decision_id": 1,
        "after_event_sequence": 0,
        "z_future_section": {"b": 2, "a": 1},
    }
    built = CoordinatorContextBuilder(configured_character_limit=800_000).build(
        decision_id=1,
        after_event_sequence=0,
        normal_payload=base,
        compact_base=base,
        events=[],
        assignment_table=[],
        report_evidence=[],
        graph_memory=None,
    )

    keys = list(json.loads(built.serialized_input))
    assert keys[:4] == [
        "coordinator_payload_schema_version",
        "compiled_prompt",
        "claim_contract",
        "exact_target_policy",
    ]
    assert keys[-1] == "decision_brief"
    assert keys.index("z_future_section") < keys.index("decision_brief")
    assert built.manifest.schema_version == 3
    assert built.manifest.payload_schema_version == 3
    assert built.manifest.section_order_version == 1
    assert built.manifest.section_positions["compiled_prompt"] == 1
    assert built.manifest.unused_headroom_characters > 0
    assert tuple(key for key in COORDINATOR_SECTION_ORDER if key in keys) != tuple(sorted(keys))

    legacy = {"z": 1, "a": {"z": 2, "a": 3}}
    assert serialize_coordinator_payload(legacy) == '{"a":{"a":3,"z":2},"z":1}'


def test_unrequested_full_graph_nodes_are_capped_and_low_ranked_nodes_do_not_fill() -> None:
    base = {
        "compiled_prompt": "Exact research prompt.",
        "claim_contract": {"conclusion": "P"},
        "decision_id": 2,
        "after_event_sequence": 0,
    }
    relevant = [_graph_item(index, priority=1, content_size=240) for index in range(20)]
    irrelevant = [_graph_item(100 + index, priority=6, content_size=240) for index in range(300)]
    builder = CoordinatorContextBuilder(configured_character_limit=800_000)

    built = builder.build(
        decision_id=2,
        after_event_sequence=0,
        normal_payload=base,
        compact_base=base,
        indexed_base=base,
        events=[],
        assignment_table=[],
        report_evidence=[],
        graph_memory={"graph_revision": "00000001-abcdef0123456789"},
        graph_evidence=[*relevant, *irrelevant],
        force_compact=True,
    )

    assert built.manifest.unrequested_full_graph_nodes_characters <= 120_000
    selected_full = {
        item["artifact_id"] for item in built.manifest.evidence_selection if item["selected_full"]
    }
    assert "graph-node:CLM-00000000" in selected_full
    assert not any(artifact_id.startswith("graph-node:CLM-000001") for artifact_id in selected_full)
    assert built.manifest.serialized_provider_input_characters < 400_000


def test_substantive_exact_duplicates_are_replaced_by_authenticated_references() -> None:
    duplicate = "The same complete proof mechanism and exact derivation. " * 200
    first = _report(1, priority=1)
    second = _report(2, priority=1)
    first.full_content["proof_content"] = duplicate
    second.full_content["proof_content"] = duplicate
    base = {
        "compiled_prompt": "Exact research prompt.",
        "claim_contract": {"conclusion": "P"},
        "decision_id": 3,
        "after_event_sequence": 2,
    }
    built = CoordinatorContextBuilder(configured_character_limit=800_000).build(
        decision_id=3,
        after_event_sequence=2,
        normal_payload={
            **base,
            "visible_worker_reports": [first.full_content, second.full_content],
        },
        compact_base=base,
        events=[],
        assignment_table=[],
        report_evidence=[first, second],
        graph_memory=None,
    )

    assert built.serialized_input.count(duplicate) == 1
    assert "authenticated_content_reference" in built.serialized_input
    assert built.manifest.redundant_characters_removed > 0


def test_worker_content_embedded_in_graph_markdown_is_deduplicated() -> None:
    duplicate = ("The exact proof mechanism closes every remaining case. " * 200).strip()
    report = _report(1, priority=1)
    report.full_content["proof_content"] = duplicate
    graph = _graph_item(1, priority=1)
    graph.full_content["node"]["body"] = (
        f"## Proof content\n\n{duplicate}\n\n"
        "## Exact gap\n\nNo gap declared; this is the completed route."
    )
    base = {
        "compiled_prompt": "Exact research prompt.",
        "claim_contract": {"conclusion": "P"},
        "decision_id": 3,
        "after_event_sequence": 2,
    }
    built = CoordinatorContextBuilder(configured_character_limit=800_000).build(
        decision_id=3,
        after_event_sequence=2,
        normal_payload={
            **base,
            "visible_worker_reports": [report.full_content],
            "full_graph_nodes": [graph.full_content],
        },
        compact_base=base,
        events=[],
        assignment_table=[],
        report_evidence=[report, graph],
        graph_memory=None,
    )

    assert built.serialized_input.count(duplicate) == 1
    assert "MATEK-AUTHENTICATED-CONTENT-REFERENCE" in built.serialized_input
    assert built.manifest.redundant_characters_removed > 0


def _ranking_nodes(id_suffixes: tuple[str, str, str, str]) -> tuple[list[GraphNode], str]:
    now = datetime(2026, 7, 27, tzinfo=UTC)
    problem_id = f"PRB-{id_suffixes[0]}"
    target_id = f"CLM-{id_suffixes[1]}"
    task_id = f"TSK-{id_suffixes[2]}"
    route_id = f"APR-{id_suffixes[3]}"
    common = {
        "problem_id": problem_id,
        "created_in_run": "run-1",
        "last_modified_run": "run-1",
        "created_at": now,
        "body": "## Exact statement\n\nA deterministic mathematical statement.",
    }
    target = GraphNode(
        **common,
        matek_id=target_id,
        node_type=NodeType.CLAIM,
        title="Exact target",
        claim_type=ClaimType.THEOREM,
        epistemic_status=EpistemicStatus.CONJECTURED,
        workflow_status=WorkflowStatus.ACTIVE,
        updated_at=now,
    )
    task = GraphNode(
        **common,
        matek_id=task_id,
        node_type=NodeType.TASK,
        title="High-value active task",
        updated_at=now - timedelta(minutes=1),
        relations=[
            GraphEdge(
                source_id=task_id,
                relation=RelationType.TARGETS,
                target_id=target_id,
            )
        ],
        metadata={"matek_assignment_id": "active-route"},
    )
    contradiction = GraphNode(
        **common,
        matek_id=route_id,
        node_type=NodeType.APPROACH,
        title="Contradictory route",
        epistemic_status=EpistemicStatus.INCONSISTENT,
        updated_at=now - timedelta(minutes=2),
        relations=[
            GraphEdge(
                source_id=route_id,
                relation=RelationType.CONTRADICTS,
                target_id=target_id,
            )
        ],
    )
    problem = GraphNode(
        **common,
        matek_id=problem_id,
        node_type=NodeType.PROBLEM,
        title="Problem",
        updated_at=now - timedelta(days=1),
    )
    return [problem, target, task, contradiction], target_id


def test_graph_ranking_is_semantic_under_complete_node_id_renaming() -> None:
    first_nodes, first_target = _ranking_nodes(("AAAAAAAA", "BBBBBBBB", "CCCCCCCC", "DDDDDDDD"))
    second_nodes, second_target = _ranking_nodes(("ZZZZZZZZ", "YYYYYYYY", "XXXXXXXX", "WWWWWWWW"))

    def ranked_titles(nodes: list[GraphNode], target_id: str) -> list[tuple[str, int, str]]:
        by_title = {node.title: node.matek_id for node in nodes}
        ranked = rank_graph_evidence(
            nodes=nodes,
            frontier_categories={
                "unresolved_claims": [by_title["Exact target"]],
                "high_value_tasks": [by_title["High-value active task"]],
                "unresolved_contradictions": [by_title["Contradictory route"]],
                "prior_runs": [by_title["Problem"]],
            },
            requested_node_ids=[],
            focal_node_ids=[target_id],
            assignment_families={"active-route": "structural"},
            current_run_id="run-1",
        )
        return [(item.node.title, item.priority, item.inclusion_reason) for item in ranked]

    assert ranked_titles(first_nodes, first_target) == ranked_titles(second_nodes, second_target)


def test_typed_graph_digest_exposes_claim_gap_dependencies_and_provenance() -> None:
    nodes, target_id = _ranking_nodes(("AAAAAAAA", "BBBBBBBB", "CCCCCCCC", "DDDDDDDD"))
    target = next(node for node in nodes if node.matek_id == target_id)
    target.body = (
        "## Exact statement\n\nFor every admissible x, P(x).\n\n"
        "## Scope and conventions\n\nAssume x lies in the frozen domain.\n\n"
        "## Exact gap\n\nProve the boundary case."
    )
    target.metadata["matek_dependencies"] = ["Boundary lemma"]
    digest = graph_node_typed_digest(
        target,
        by_id={node.matek_id: node for node in nodes},
        graph_revision="00000007-abcdef0123456789",
        relative_path=".matek/knowledge/problem/Claims/target.md",
        sha256="f" * 64,
        graph_distance=0,
        main_target_id=target_id,
    )

    assert digest["exact_statement_or_result"] == "For every admissible x, P(x)."
    assert digest["exact_unresolved_gap"] == "Prove the boundary case."
    assert digest["dependencies"] == ["Boundary lemma"]
    assert digest["assumptions"] == ["Assume x lies in the frozen domain."]
    assert digest["provenance"] == {
        "relative_path": ".matek/knowledge/problem/Claims/target.md",
        "graph_revision": "00000007-abcdef0123456789",
        "sha256": "f" * 64,
        "last_modified_run": "run-1",
        "updated_at": "2026-07-27T00:00:00+00:00",
    }


def test_typed_graph_digest_has_a_hard_serialized_character_bound() -> None:
    nodes, target_id = _ranking_nodes(("AAAAAAAA", "BBBBBBBB", "CCCCCCCC", "DDDDDDDD"))
    target = next(node for node in nodes if node.matek_id == target_id)
    long_text = "mathematical detail " * 2_000
    target.body = (
        f"## Exact statement\n\n{long_text}\n\n"
        f"## Proposed invariant or mechanism\n\n{long_text}\n\n"
        f"## Exact gap\n\n{long_text}"
    )
    target.metadata["matek_assumptions"] = [long_text] * 50
    target.metadata["matek_dependencies"] = [f"{index}: {long_text}" for index in range(50)]
    digest = graph_node_typed_digest(
        target,
        by_id={node.matek_id: node for node in nodes},
        graph_revision="00000007-abcdef0123456789",
        relative_path=".matek/knowledge/problem/Claims/target.md",
        sha256="f" * 64,
        graph_distance=0,
        main_target_id=target_id,
    )

    serialized = json.dumps(
        digest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert len(serialized) <= GRAPH_NODE_DIGEST_CHARACTER_LIMIT
    assert digest["omitted_digest_items"]


def test_counterexample_digest_names_its_target_and_scope() -> None:
    nodes, target_id = _ranking_nodes(("AAAAAAAA", "BBBBBBBB", "CCCCCCCC", "DDDDDDDD"))
    now = datetime(2026, 7, 27, tzinfo=UTC)
    counterexample = GraphNode(
        matek_id="CEX-EEEEEEEE",
        node_type=NodeType.COUNTEREXAMPLE,
        problem_id=nodes[0].problem_id,
        title="Boundary obstruction",
        created_in_run="run-1",
        last_modified_run="run-1",
        created_at=now,
        updated_at=now,
        body=(
            "## Explicit counterexample or obstruction\n\n"
            "At x = 0 the proposed inequality reverses.\n\n"
            "## Scope\n\nThis refutes only the unrestricted boundary branch."
        ),
        relations=[
            GraphEdge(
                source_id="CEX-EEEEEEEE",
                relation=RelationType.REFUTES,
                target_id=target_id,
            )
        ],
    )
    by_id = {node.matek_id: node for node in [*nodes, counterexample]}
    digest = graph_node_typed_digest(
        counterexample,
        by_id=by_id,
        graph_revision="00000007-abcdef0123456789",
        relative_path=".matek/knowledge/problem/Counterexamples/boundary.md",
        sha256="e" * 64,
        graph_distance=1,
        main_target_id=target_id,
    )

    assert digest["counterexample_target_and_scope"] == {
        "counterexample_or_obstruction": "At x = 0 the proposed inequality reverses.",
        "scope": "This refutes only the unrestricted boundary branch.",
        "target_node_ids": [target_id],
    }
    assert digest["key_typed_relations"] == [
        {
            "relation": "refutes",
            "target_id": target_id,
            "target_type": "claim",
            "target_title": "Exact target",
        }
    ]
