from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from matek_theorem_agent.workspace import (
    ARTIFACT_DIRECTORIES,
    PathConfinementError,
    RunLock,
    RunLockHeldError,
    WorkspaceError,
    atomic_write_json,
    atomic_write_text,
    confined_path,
    create_run_root,
    discover_project_root,
    generate_run_id,
    latest_run_root,
    latest_run_root_for_problem,
)


def test_discovers_lean_project(tmp_path: Path) -> None:
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:stable", encoding="utf-8")
    child = tmp_path / "a" / "b"
    child.mkdir(parents=True)
    assert discover_project_root(child) == tmp_path


def test_run_id_is_safe_and_deterministic_with_injected_values() -> None:
    run_id = generate_run_id(
        "../../ My Résult / name",
        now=datetime(2026, 7, 19, 12, 34, 56, tzinfo=UTC),
        random_suffix="abcdef",
    )
    assert run_id == "20260719T123456Z-my-result-name-abcdef"
    assert Path(run_id).name == run_id


def test_run_id_includes_safe_problem_stem_before_timestamp() -> None:
    run_id = generate_run_id(
        "Second attempt",
        problem_name="My Résult",
        now=datetime(2026, 7, 19, 12, 34, 56, tzinfo=UTC),
        random_suffix="abcdef",
    )

    assert run_id == "run-my-result-second-attempt-20260719T123456Z-abcdef"
    assert Path(run_id).name == run_id


def test_latest_run_uses_embedded_timestamp_with_problem_first_ids(tmp_path: Path) -> None:
    older = create_run_root(
        tmp_path,
        run_id="run-zeta-problem-20260718T120000Z-abcdef",
    )
    newer = create_run_root(
        tmp_path,
        run_id="run-alpha-problem-20260719T120000Z-abcdef",
    )

    assert older.name > newer.name
    assert latest_run_root(tmp_path) == newer


def test_latest_run_for_problem_uses_frozen_source_path_not_run_name(tmp_path: Path) -> None:
    first_problem = tmp_path / "first.md"
    second_problem = tmp_path / "second.md"
    first_problem.write_text("First problem.\n", encoding="utf-8")
    second_problem.write_text("Second problem.\n", encoding="utf-8")
    older = create_run_root(
        tmp_path,
        run_id="run-unrelated-name-20260718T120000Z-abcdef",
    )
    newer = create_run_root(
        tmp_path,
        run_id="run-another-name-20260720T120000Z-abcdef",
    )
    other = create_run_root(
        tmp_path,
        run_id="run-first-20260721T120000Z-abcdef",
    )
    for run_root, problem in (
        (older, first_problem),
        (newer, first_problem),
        (other, second_problem),
    ):
        atomic_write_json(
            run_root / "input" / "invocation.json",
            {"problem_file": str(problem.resolve())},
            confinement_root=run_root,
        )

    assert latest_run_root_for_problem(tmp_path, first_problem) == newer
    assert latest_run_root_for_problem(tmp_path, second_problem) == other

    missing = tmp_path / "missing.md"
    with pytest.raises(WorkspaceError, match="does not exist"):
        latest_run_root_for_problem(tmp_path, missing)


def test_create_run_root_builds_exact_concrete_contract_dirs(tmp_path: Path) -> None:
    run_root = create_run_root(tmp_path, run_id="20260719T123456Z-contract-abcdef")
    directories = {
        path.relative_to(run_root).as_posix() for path in run_root.rglob("*") if path.is_dir()
    }
    assert directories == set(ARTIFACT_DIRECTORIES)


def test_path_traversal_is_rejected(tmp_path: Path) -> None:
    run_root = create_run_root(tmp_path, run_id="20260719T123456Z-paths-abcdef")
    with pytest.raises(PathConfinementError):
        confined_path(run_root, "..", "outside.txt")
    with pytest.raises(PathConfinementError):
        confined_path(run_root, tmp_path / "absolute.txt")


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    run_root = create_run_root(tmp_path, run_id="20260719T123456Z-links-abcdef")
    outside = tmp_path / "outside"
    outside.mkdir()
    (run_root / "logs" / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathConfinementError):
        confined_path(run_root, "logs", "escape", "secret.txt")


def test_symlink_loop_is_rejected_as_confinement_error(tmp_path: Path) -> None:
    run_root = create_run_root(tmp_path, run_id="20260719T123456Z-loop-abcdef")
    (run_root / "logs" / "loop").symlink_to(run_root / "logs" / "loop")
    with pytest.raises(PathConfinementError):
        confined_path(run_root, "logs", "loop", "event.json")


def test_symlinked_matek_root_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / ".matek").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathConfinementError):
        create_run_root(project, run_id="20260719T123456Z-links-abcdef")


def test_atomic_text_and_json_writes(tmp_path: Path) -> None:
    run_root = create_run_root(tmp_path, run_id="20260719T123456Z-atomic-abcdef")
    text_path = run_root / "input" / "problem.md"
    json_path = run_root / "input" / "invocation.json"
    atomic_write_text(text_path, "problem\n", confinement_root=run_root)
    atomic_write_json(json_path, {"answer": 42}, confinement_root=run_root)
    assert text_path.read_text(encoding="utf-8") == "problem\n"
    assert json.loads(json_path.read_text(encoding="utf-8")) == {"answer": 42}
    assert not list(run_root.rglob("*.tmp"))


def test_run_lock_is_external_fail_fast_and_reusable_after_release(tmp_path: Path) -> None:
    run_root = create_run_root(tmp_path, run_id="20260719T123456Z-lock-abcdef")
    first = RunLock(run_root)

    assert first.lock_path == (tmp_path / ".matek" / "locks" / "20260719T123456Z-lock-abcdef.lock")
    assert not first.lock_path.is_relative_to(run_root)

    with first:
        owner = json.loads(first.lock_path.read_text(encoding="utf-8"))
        assert owner["run_id"] == run_root.name
        assert isinstance(owner["pid"], int)
        with pytest.raises(RunLockHeldError, match="already active") as raised:
            with RunLock(run_root):
                pytest.fail("a contending process entered the locked run")
        assert raised.value.run_id == run_root.name
        assert raised.value.owner["pid"] == owner["pid"]

    # The lock file remains on disk to preserve one stable inode, but release makes
    # it immediately acquirable by a subsequent process.
    assert first.lock_path.is_file()
    with RunLock(run_root):
        pass


def test_stale_run_lock_metadata_is_reclaimed_by_the_next_owner(tmp_path: Path) -> None:
    run_root = create_run_root(tmp_path, run_id="20260719T123456Z-stale-abcdef")
    lock = RunLock(run_root)
    lock.lock_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": run_root.name,
                "pid": 999_999_999,
                "acquired_at": "2000-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    with lock:
        owner = json.loads(lock.lock_path.read_text(encoding="utf-8"))
        assert owner["pid"] == os.getpid()
        assert owner["run_id"] == run_root.name
