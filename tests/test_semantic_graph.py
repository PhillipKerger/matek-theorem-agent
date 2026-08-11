from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from matek_theorem_agent.cli import app
from matek_theorem_agent.knowledge_graph import semantic as semantic_module
from matek_theorem_agent.knowledge_graph.semantic import (
    SemanticCoordinatorDecision,
    SemanticFinding,
    SemanticFindingStatus,
    SemanticFindingType,
    SemanticGraphError,
    SemanticGraphWriter,
    SemanticWorkerReport,
    recover_semantic_finding,
)
from matek_theorem_agent.stages.research import (
    ResearchCoordinatorDecision,
    ResearchWorkerReport,
)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 11, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.value += timedelta(seconds=1)
        return self.value


def writer(tmp_path: Path, name: str = "jantzen") -> SemanticGraphWriter:
    tmp_path.mkdir(exist_ok=True)
    graph = SemanticGraphWriter(tmp_path, name, clock=Clock())
    graph.initialize()
    return graph


def finding(
    title: str,
    *,
    relates_to: list[str] | None = None,
    finding_type: SemanticFindingType = SemanticFindingType.LEMMA,
) -> SemanticFinding:
    return SemanticFinding(
        finding_type=finding_type,
        title=title,
        relates_to=relates_to or [],
        status=SemanticFindingStatus.INCOMPLETE,
        statement=f"Statement of {title}.",
        what_was_established=f"Established the recoverable part of {title}.",
        next_mathematical_bottleneck="Close the remaining boundary case.",
    )


def test_title_resolves_after_restart_and_index_deletion(tmp_path: Path) -> None:
    graph = writer(tmp_path)
    graph.admit_finding(finding("Within-layer Yoneda vanishing"))
    graph.index_path.unlink()

    restarted = SemanticGraphWriter(tmp_path, "jantzen", clock=Clock())
    node = restarted.resolve_title("Within-layer Yoneda vanishing")

    assert node.title == "Within-layer Yoneda vanishing"
    assert restarted.index_path.is_file()
    assert not (restarted.graph_root / "ledgers").exists()


def test_title_collision_is_deterministically_disambiguated(tmp_path: Path) -> None:
    graph = writer(tmp_path)
    first = graph.admit_finding(finding("Within-layer Yoneda vanishing"))
    second = graph.admit_finding(
        finding("Within-layer Yoneda vanishing"), disambiguation="singular blocks"
    )

    assert first.title == "Within-layer Yoneda vanishing"
    assert second.title == "Within-layer Yoneda vanishing (singular blocks)"
    assert second.path.name == "Within-layer Yoneda vanishing (singular blocks).md"


def test_rename_updates_inbound_links_and_preserves_hidden_identity(tmp_path: Path) -> None:
    graph = writer(tmp_path)
    original = graph.admit_finding(finding("Arc-first Morita criterion"))
    uid = graph.resolve_title(original.title).uid
    dependent = graph.admit_finding(
        finding(
            "Rank-three extension obstruction",
            relates_to=["Arc-first Morita criterion"],
        )
    )

    renamed = graph.rename_node("Arc-first Morita criterion", "Arc-first derived Morita criterion")

    assert renamed == "Arc-first derived Morita criterion"
    assert graph.resolve_title(renamed).uid == uid
    assert "[[Arc-first derived Morita criterion]]" in dependent.path.read_text(encoding="utf-8")
    assert "[[Arc-first Morita criterion]]" not in dependent.path.read_text(encoding="utf-8")
    assert graph.validate().valid


def test_dangling_relation_creates_local_incident_and_keeps_finding(tmp_path: Path) -> None:
    graph = writer(tmp_path)
    result = graph.admit_finding(
        finding(
            "Rank-three singular block audit",
            relates_to=["Missing within-layer vanishing statement"],
        ),
        provenance=["worker report: rank-three audit"],
    )

    assert result.status == "committed_with_incident"
    assert result.path.is_file()
    assert len(result.incident_paths) == 1
    incident = result.incident_paths[0].read_text(encoding="utf-8")
    assert "Missing within-layer vanishing statement" in incident
    assert "Rank-three singular block audit" in incident
    assert "research should continue" in result.semantic_correction
    assert graph.resolve_title("Rank-three singular block audit")
    assert graph.validate().valid


def test_partial_worker_progress_is_first_class(tmp_path: Path) -> None:
    graph = writer(tmp_path)
    report = SemanticWorkerReport(
        assignment_title="Audit the rank-three singular block",
        overall_progress="The computation establishes the regular-block case only.",
        next_assignment="Investigate singular boundary weights.",
    )

    admitted = graph.admit_worker_report(report)

    assert len(admitted) == 1
    partial = graph.resolve_title("Partial progress on Audit the rank-three singular block")
    assert partial.kind.value == "partial_progress"
    assert "No theorem" not in partial.body
    assert "regular-block case" in partial.body
    assert "No theorem admitted" in admitted[0].semantic_correction


def test_two_simultaneous_graphs_mutate_only_their_own_state(tmp_path: Path) -> None:
    first = writer(tmp_path, "matroid-secretary")
    second = writer(tmp_path, "jantzen")

    with ThreadPoolExecutor(max_workers=2) as pool:
        left = pool.submit(first.admit_finding, finding("Exact matroid secretary target"))
        right = pool.submit(second.admit_finding, finding("Within-layer Yoneda vanishing"))
        left.result()
        right.result()

    assert first.resolve_title("Exact matroid secretary target")
    assert second.resolve_title("Within-layer Yoneda vanishing")
    assert not list(first.graph_root.rglob("Within-layer Yoneda vanishing.md"))
    assert not list(second.graph_root.rglob("Exact matroid secretary target.md"))


def test_long_sequence_ignores_stale_or_corrupt_derived_index(tmp_path: Path) -> None:
    graph = writer(tmp_path, "long-run")
    graph.admit_finding(finding("Initial mathematical target"))

    for index in range(180):
        if index % 17 == 0:
            graph.index_path.write_text("not a sqlite database", encoding="utf-8")
        result = graph.admit_finding(
            finding(
                f"Admission sequence result {index:03d}",
                relates_to=["Initial mathematical target"],
                finding_type=SemanticFindingType.PARTIAL_PROGRESS,
            )
        )
        assert result.status == "committed"

    assert graph.validate().valid
    assert len(graph.load_nodes()[0]) == 181


def test_semantic_context_contains_no_storage_identifiers(tmp_path: Path) -> None:
    graph = writer(tmp_path)
    graph.admit_finding(finding("Within-layer Yoneda vanishing"))

    serialized = json.dumps(graph.semantic_context(), sort_keys=True)

    for forbidden in ("OBL-", "TSK-", "graph-node:", "graph_patch", "ledger", "uid"):
        assert forbidden not in serialized


def test_index_rebuild_failure_is_nonfatal_and_markdown_still_resolves(tmp_path: Path) -> None:
    graph = writer(tmp_path)
    graph.admit_finding(finding("Within-layer Yoneda vanishing"))

    def fail_refresh(_writer: SemanticGraphWriter, _nodes: object) -> None:
        raise SemanticGraphError("simulated derived cache failure")

    restarted = SemanticGraphWriter(
        tmp_path,
        "jantzen",
        clock=Clock(),
        index_refresher=fail_refresh,
    )
    restarted.index_path.unlink()
    nodes, warnings = restarted.load_nodes()

    assert [node.title for node in nodes] == ["Within-layer Yoneda vanishing"]
    assert any("continuing from Markdown authority" in warning for warning in warnings)
    assert restarted.resolve_title("Within-layer Yoneda vanishing")


def test_restart_completes_an_interrupted_multi_note_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = writer(tmp_path)
    real_replace = semantic_module.os.replace
    staged_replacements = 0

    def interrupt_second_staged_write(source: object, destination: object) -> None:
        nonlocal staged_replacements
        if (
            "/files/" in str(source)
            and "/files/" not in str(destination)
            and str(destination).endswith(".md")
        ):
            staged_replacements += 1
            if staged_replacements == 2:
                raise OSError("simulated process interruption")
        real_replace(source, destination)

    monkeypatch.setattr(semantic_module.os, "replace", interrupt_second_staged_write)
    with pytest.raises(SemanticGraphError, match="transaction retained"):
        graph.admit_finding(
            finding("Recoverable interrupted result", relates_to=["Missing relation"])
        )
    monkeypatch.setattr(semantic_module.os, "replace", real_replace)

    restarted = SemanticGraphWriter(tmp_path, "jantzen", clock=Clock())
    nodes, _ = restarted.load_nodes()

    assert {node.title for node in nodes} >= {"Recoverable interrupted result"}
    assert any(node.kind.value == "incident" for node in nodes)
    assert restarted.validate().valid


def test_graph_cli_initializes_only_graph_markdown_and_derived_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "cli-project"
    project.mkdir()
    (project / ".git").mkdir()
    monkeypatch.chdir(project)

    result = CliRunner().invoke(app, ["graph", "init", "matroid-secretary"])

    assert result.exit_code == 0
    vault = project / ".matek" / "knowledge" / "matroid-secretary"
    assert (vault / "Claims").is_dir()
    assert (vault / "Incidents").is_dir()
    assert (vault / "graph-index.sqlite").is_file()
    assert not (vault / "graph-state.json").exists()
    assert not (vault / "ledgers").exists()
    help_result = CliRunner().invoke(app, ["graph", "--help"])
    assert "migrate-legacy" not in help_result.output
    assert "verify-snapshots" not in help_result.output


def test_problem_initialization_creates_a_descriptive_root_note(tmp_path: Path) -> None:
    graph = writer(tmp_path, "new-project")

    title = graph.initialize_problem(
        title="Matroid Secretary Conjecture",
        statement="Every matroid admits a constant-competitive secretary algorithm.",
        provenance=["run: fresh"],
    )

    node = graph.resolve_title(title)
    assert node.kind.value == "problem"
    assert node.path.name == "Matroid Secretary Conjecture.md"
    assert "constant-competitive" in node.body
    assert not (graph.graph_root / "graph-state.json").exists()


def test_task_status_update_preserves_identity_and_mathematics(tmp_path: Path) -> None:
    graph = writer(tmp_path)
    admitted = graph.admit_finding(
        finding("Audit the singular block", finding_type=SemanticFindingType.TASK)
    )
    original = graph.resolve_title(admitted.title)

    graph.update_status(
        admitted.title,
        "complete",
        provenance=["worker completed assignment"],
    )

    updated = graph.resolve_title(admitted.title)
    assert updated.uid == original.uid
    assert updated.status == "complete"
    assert "Statement of Audit the singular block" in updated.body
    assert "worker completed assignment" in updated.provenance


def test_malformed_optional_worker_fields_recover_as_partial_progress(tmp_path: Path) -> None:
    graph = writer(tmp_path)
    recovered = recover_semantic_finding(
        {
            "finding_type": "partial_progress",
            "title": "Rank-three singular block audit",
            "status": "incomplete",
            "statement": 17,
            "what_was_established": "The regular block reduces to one coefficient.",
            "relates_to": "malformed optional field",
            "depends_on": [42, "Also malformed / title"],
            "supporting_evidence": [42, "hand calculation"],
        }
    )

    assert recovered is not None
    assert recovered.statement is None
    assert recovered.relates_to == []
    assert recovered.depends_on == []
    admitted = graph.admit_finding(recovered)
    assert admitted.status == "committed"
    assert "regular block" in admitted.path.read_text(encoding="utf-8")


def test_runtime_output_contracts_expose_semantic_schemas_only() -> None:
    coordinator_schema = ResearchCoordinatorDecision.model_json_schema()
    worker_schema = ResearchWorkerReport.model_json_schema()

    assert coordinator_schema == SemanticCoordinatorDecision.model_json_schema()
    assert worker_schema == SemanticWorkerReport.model_json_schema()
    serialized = json.dumps([coordinator_schema, worker_schema], sort_keys=True)
    for forbidden in (
        "decision_id",
        "assignment_id",
        "target_node_ids",
        "requested_graph_node_ids",
        "graph_patch",
        "ledger",
    ):
        assert forbidden not in serialized

    coordinator = ResearchCoordinatorDecision.model_validate(
        {
            "assignments": [
                {
                    "title": "Audit the rank-three singular block",
                    "approach_family": "representation theory",
                    "task": "Compute the remaining extension coefficient.",
                    "expected_output": "An exact vanishing lemma or obstruction.",
                    "relates_to": ["Matroid Secretary Conjecture"],
                    "stopping_condition": "Stop after isolating the exact coefficient.",
                }
            ],
            "rationale": "This is the smallest open mathematical cut.",
        }
    )
    worker = ResearchWorkerReport.model_validate(
        {
            "schema_version": 3,
            "assignment_title": "Audit the rank-three singular block",
            "findings": [],
            "overall_progress": "The regular block is complete; the singular case remains.",
            "next_assignment": "Compute the singular coefficient.",
        }
    )

    assert coordinator.semantic_decision is not None
    assert worker.semantic_report is not None
    assert worker.branch_outcome.value == "blocked"
