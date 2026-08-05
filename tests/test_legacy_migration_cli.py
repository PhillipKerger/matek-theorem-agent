from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from matek_theorem_agent.cli import app
from matek_theorem_agent.knowledge_graph import KnowledgeGraph
from matek_theorem_agent.knowledge_graph.migration import (
    LegacyMigrationApplicationRecord,
    LegacyMigrationReport,
    load_legacy_migration_application,
    load_legacy_migration_report,
    migration_application_sha256,
    migration_report_sha256,
)


def _initialized_legacy_graph(tmp_path: Path) -> tuple[KnowledgeGraph, str]:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    source = project / "legacy-problem.md"
    source.write_text("Prove that every test object has property P.\n", encoding="utf-8")

    graph = KnowledgeGraph(project, "legacy-problem")
    problem_id, _ = graph.initialize_problem(
        source_path=source,
        problem_text=source.read_text(encoding="utf-8"),
        run_id="run-legacy",
    )
    graph.record_compiled_problem(
        problem_id=problem_id,
        run_id="run-legacy",
        compiled_problem={
            "title": "Legacy test theorem",
            "normalized_statement": "For every test object, property P holds.",
            "claim_contract": {"target": "property P"},
            "literature_status": "unknown",
            "source_ledger": [],
        },
    )
    return graph, problem_id


def _archive_contents(graph: KnowledgeGraph) -> dict[str, bytes]:
    return {
        path.relative_to(graph.vault_root).as_posix(): path.read_bytes()
        for path in sorted(graph.vault_root.rglob("*"))
        if path.is_file()
    }


def test_migrate_legacy_prints_integrity_protected_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph, problem_id = _initialized_legacy_graph(tmp_path)
    before = _archive_contents(graph)
    monkeypatch.chdir(graph.project_root)

    result = CliRunner().invoke(
        app,
        ["graph", "migrate-legacy", "--knowledge-graph", graph.graph_name],
        terminal_width=40,
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    integrity = payload.pop("integrity_sha256")
    assert payload["mode"] == "dry_run"
    assert payload["source_edits_applied"] is False
    assert payload["problem_id"] == problem_id
    assert payload["target_claim_id"] == graph.main_claim_id(problem_id)
    assert migration_report_sha256(LegacyMigrationReport.model_validate(payload)) == integrity
    assert _archive_contents(graph) == before


def test_migrate_legacy_writes_a_verifiable_report_outside_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph, problem_id = _initialized_legacy_graph(tmp_path)
    before = _archive_contents(graph)
    destination = graph.project_root / ".matek" / "migration-reports" / "legacy.json"
    monkeypatch.chdir(graph.project_root)

    result = CliRunner().invoke(
        app,
        [
            "graph",
            "migrate-legacy",
            "--knowledge-graph",
            graph.graph_name,
            "--problem-id",
            problem_id,
            "--target-claim-id",
            graph.main_claim_id(problem_id),
            "--output",
            str(destination),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    report = load_legacy_migration_report(destination)
    assert report.mode == "dry_run"
    assert report.source_edits_applied is False
    assert migration_report_sha256(report) in result.output
    assert _archive_contents(graph) == before


def test_migrate_legacy_refuses_to_write_a_report_inside_any_graph_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph, _ = _initialized_legacy_graph(tmp_path)
    before = _archive_contents(graph)
    destination = graph.vault_root / "migration-report.json"
    monkeypatch.chdir(graph.project_root)

    result = CliRunner().invoke(
        app,
        [
            "graph",
            "migrate-legacy",
            "--knowledge-graph",
            graph.graph_name,
            "--output",
            str(destination),
        ],
    )

    assert result.exit_code == 2
    assert "outside .matek/knowledge" in result.output
    assert not destination.exists()
    assert _archive_contents(graph) == before


def test_migrate_legacy_does_not_recover_a_pending_graph_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph, _ = _initialized_legacy_graph(tmp_path)
    graph.pending_path.write_text("leave this pending transaction untouched\n", encoding="utf-8")
    before = _archive_contents(graph)
    monkeypatch.chdir(graph.project_root)

    result = CliRunner().invoke(
        app,
        ["graph", "migrate-legacy", "--knowledge-graph", graph.graph_name],
    )

    assert result.exit_code == 6
    assert "cannot recover a pending graph" in result.output
    assert "transaction" in result.output
    assert _archive_contents(graph) == before


def test_migrate_legacy_apply_requires_confirmation_and_is_retry_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph, _ = _initialized_legacy_graph(tmp_path)
    plan_path = graph.project_root / ".matek" / "migration-reports" / "reviewed.json"
    monkeypatch.chdir(graph.project_root)
    planned = CliRunner().invoke(
        app,
        [
            "graph",
            "migrate-legacy",
            "--knowledge-graph",
            graph.graph_name,
            "--output",
            str(plan_path),
        ],
    )
    assert planned.exit_code == 0, planned.output
    source_revision = graph.load_state().revision

    cancelled = CliRunner().invoke(
        app,
        [
            "graph",
            "migrate-legacy",
            "--knowledge-graph",
            graph.graph_name,
            "--apply-plan",
            str(plan_path),
        ],
        input="n\n",
    )
    assert cancelled.exit_code == 1
    assert graph.load_state().revision == source_revision

    applied = CliRunner().invoke(
        app,
        [
            "graph",
            "migrate-legacy",
            "--knowledge-graph",
            graph.graph_name,
            "--apply-plan",
            str(plan_path),
            "--yes",
        ],
    )
    assert applied.exit_code == 0, applied.output
    payload = json.loads(applied.stdout)
    integrity = payload.pop("integrity_sha256")
    record = LegacyMigrationApplicationRecord.model_validate(payload)
    assert record.status == "applied"
    assert record.previous_revision == source_revision
    assert migration_application_sha256(record) == integrity
    persisted = load_legacy_migration_application(
        graph.ledgers_root / "migrations" / f"{record.plan_sha256}.application.json"
    )
    assert persisted == record

    retried = CliRunner().invoke(
        app,
        [
            "graph",
            "migrate-legacy",
            "--knowledge-graph",
            graph.graph_name,
            "--apply-plan",
            str(plan_path),
            "--yes",
        ],
    )
    assert retried.exit_code == 0, retried.output
    retry_payload = json.loads(retried.stdout)
    assert retry_payload["status"] == "already_applied"
    assert graph.load_state().revision == record.new_revision


def test_migrate_legacy_help_describes_safe_plan_and_apply_modes() -> None:
    result = CliRunner().invoke(
        app,
        ["graph", "migrate-legacy", "--help"],
        terminal_width=160,
    )

    assert result.exit_code == 0
    assert "explicitly apply one reviewed" in result.output
    assert "--dry-run" in result.output
    assert "read-only plan" in result.output
    assert "--apply-plan" in result.output
    assert "--yes" in result.output
    assert "--target-claim-id" in result.output
    assert "--output" in result.output
