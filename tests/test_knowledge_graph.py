from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from matek_theorem_agent.cli import app
from matek_theorem_agent.knowledge_graph import (
    ClaimType,
    EpistemicStatus,
    GraphConflictError,
    GraphEdge,
    GraphNodeCreate,
    GraphNodeUpdate,
    GraphPatch,
    GraphStatusChange,
    KnowledgeGraph,
    NodeType,
    RelationType,
    list_graph_names,
    problem_graph_name,
)


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
                expected_content_hash=claim.content_hash,
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
    tombstone = graph.tombstone(proof_id, reason="Superseded by a corrected proof.")
    assert tombstone.committed
    assert graph.show(proof_id).tombstone


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
                expected_content_hash=dependency.content_hash,
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


def test_human_statement_edits_are_preserved_and_machine_conflicts_are_rejected(
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
    edited = original.replace(
        "For every test object, the desired property holds.",
        "For every nonempty test object, the desired property holds.",
    )
    edited += "\n## Human notes\n\nKeep this observation.\n"
    note_path.write_text(edited, encoding="utf-8")

    result = graph.reconcile_human_edits(run_id="human-edit")
    assert result is not None and target_id in result.stale_node_ids
    changed = graph.show(target_id)
    assert changed.path == renamed_path.relative_to(graph.vault_root).as_posix()
    assert changed.statement_version == 2
    assert changed.epistemic_status is EpistemicStatus.STALE
    assert "Keep this observation." in note_path.read_text(encoding="utf-8")

    conflicted = note_path.read_text(encoding="utf-8").replace(
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
