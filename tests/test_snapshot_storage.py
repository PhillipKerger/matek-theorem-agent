from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from matek_theorem_agent.cli import app
from matek_theorem_agent.knowledge_graph import GraphValidationError, KnowledgeGraph


class AdvancingClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 3, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.value += timedelta(seconds=1)
        return self.value


def _graph_with_revisions(
    tmp_path: Path,
    *,
    checkpoint_interval: int = 2,
    run_count: int = 2,
) -> tuple[KnowledgeGraph, list[str]]:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    problem = project / "problem.md"
    problem.write_text("Prove the snapshot test theorem.\n", encoding="utf-8")
    graph = KnowledgeGraph(
        project,
        "problem",
        clock=AdvancingClock(),
        snapshot_checkpoint_interval=checkpoint_interval,
    )
    revisions = [graph.initialize().revision]
    for number in range(1, run_count + 1):
        _, revision = graph.initialize_problem(
            source_path=problem,
            problem_text=problem.read_text(encoding="utf-8"),
            run_id=f"run-{number}",
        )
        revisions.append(revision)
    return graph, revisions


def _legacy_snapshot_bytes(payload: dict[str, object]) -> bytes:
    legacy = dict(payload)
    legacy["schema_version"] = 1
    legacy.pop("integrity_root", None)
    legacy.pop("content_root", None)
    return (json.dumps(legacy, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def test_schema_v2_uses_deltas_content_blobs_and_periodic_checkpoints(
    tmp_path: Path,
) -> None:
    graph, revisions = _graph_with_revisions(tmp_path)

    assert not list(graph.snapshots_root.glob("*.json"))
    manifests = [graph.snapshots_root / "manifests" / f"{revision}.json" for revision in revisions]
    assert all(path.is_file() for path in manifests)
    second_manifest = json.loads(manifests[1].read_text(encoding="utf-8"))
    assert second_manifest["schema_version"] == 2
    assert second_manifest["artifact_type"] == "matek.graph.snapshot.delta"
    assert second_manifest["previous_revision"] == revisions[0]
    assert second_manifest["checkpoint_sha256"] is None
    assert second_manifest["added_nodes"]
    assert "nodes" not in second_manifest
    assert "edges" not in second_manifest
    assert len(second_manifest["content_root"]) == 64
    assert len(second_manifest["integrity_root"]) == 64

    checkpoints = {path.stem for path in (graph.snapshots_root / "checkpoints").glob("*.json")}
    assert checkpoints == {revisions[0], revisions[2]}

    verification = graph.verify_snapshots()
    assert [item.revision for item in verification] == revisions
    assert [item.schema_version for item in verification] == [2, 2, 2]
    assert verification[1].checkpoint_revision == revisions[0]
    assert verification[2].checkpoint_revision == revisions[2]

    reconstructed = graph.reconstruct_snapshot(revisions[2])
    assert reconstructed == graph.reconstruct_snapshot(revisions[2])
    final_manifest = json.loads(manifests[2].read_text(encoding="utf-8"))
    assert hashlib.sha256(reconstructed).hexdigest() == final_manifest["reconstruction_sha256"]
    payload = json.loads(reconstructed)
    assert payload["revision"] == revisions[2]
    assert payload["schema_version"] == 2
    assert payload["node_hashes"]

    node_blob_count = len(list((graph.snapshots_root / "blobs" / "nodes").glob("*.json")))
    assert node_blob_count < sum(item.node_count for item in verification)
    difference = graph.diff(revisions[0], revisions[2])
    assert difference.added_nodes
    assert difference.removed_nodes == []


def test_legacy_snapshots_remain_byte_exact_and_seed_a_v2_checkpoint(tmp_path: Path) -> None:
    graph, revisions = _graph_with_revisions(tmp_path, run_count=1)
    payloads = {revision: graph.snapshot_store.load_snapshot(revision) for revision in revisions}
    legacy_bytes = {
        revision: _legacy_snapshot_bytes(payload) for revision, payload in payloads.items()
    }
    for revision, contents in legacy_bytes.items():
        (graph.snapshots_root / f"{revision}.json").write_bytes(contents)
    for path in (graph.snapshots_root / "manifests").glob("*.json"):
        path.unlink()
    for path in (graph.snapshots_root / "checkpoints").glob("*.json"):
        path.unlink()

    legacy_verification = graph.verify_snapshots()
    assert [item.schema_version for item in legacy_verification] == [1, 1]
    assert graph.reconstruct_snapshot(revisions[1]) == legacy_bytes[revisions[1]]
    before = {
        revision: hashlib.sha256(contents).hexdigest()
        for revision, contents in legacy_bytes.items()
    }

    problem = graph.project_root / "problem.md"
    _, next_revision = graph.initialize_problem(
        source_path=problem,
        problem_text=problem.read_text(encoding="utf-8"),
        run_id="run-2",
    )

    for revision, digest in before.items():
        path = graph.snapshots_root / f"{revision}.json"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert not (graph.snapshots_root / f"{next_revision}.json").exists()
    assert (graph.snapshots_root / "manifests" / f"{next_revision}.json").is_file()
    assert (graph.snapshots_root / "checkpoints" / f"{next_revision}.json").is_file()
    current = graph.verify_snapshots(next_revision)[0]
    assert current.schema_version == 2
    assert current.checkpoint_revision == next_revision
    assert graph.diff(revisions[1], next_revision).added_nodes


def test_blob_corruption_blocks_reconstruction_and_graph_validation(tmp_path: Path) -> None:
    graph, revisions = _graph_with_revisions(
        tmp_path,
        checkpoint_interval=1,
        run_count=1,
    )
    current = revisions[-1]
    checkpoint_path = graph.snapshots_root / "checkpoints" / f"{current}.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    digest = next(iter(checkpoint["node_blobs"].values()))
    blob_path = graph.snapshots_root / "blobs" / "nodes" / f"{digest}.json"
    blob_path.write_bytes(b"{}\n")

    with pytest.raises(GraphValidationError, match="node blob digest is invalid"):
        graph.verify_snapshots(current)
    with pytest.raises(GraphValidationError, match="node blob digest is invalid"):
        graph.reconstruct_snapshot(current)
    report = graph.validate()
    assert not report.valid
    assert any(issue.code == "snapshot_integrity" for issue in report.issues)


def test_snapshot_reconstruct_and_verify_cli_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph, revisions = _graph_with_revisions(tmp_path, run_count=1)
    monkeypatch.chdir(graph.project_root)
    runner = CliRunner()

    verified = runner.invoke(
        app,
        ["graph", "verify-snapshots", revisions[-1], "--knowledge-graph", "problem"],
    )
    assert verified.exit_code == 0, verified.output
    assert json.loads(verified.stdout)[0]["revision"] == revisions[-1]

    output = graph.project_root / "snapshot.json"
    reconstructed = runner.invoke(
        app,
        [
            "graph",
            "reconstruct",
            revisions[-1],
            "--knowledge-graph",
            "problem",
            "--output",
            str(output),
        ],
    )
    assert reconstructed.exit_code == 0, reconstructed.output
    assert output.read_bytes() == graph.reconstruct_snapshot(revisions[-1])
