from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from matek_theorem_agent.execution.base import (
    CommandRequest,
    CommandResult,
    CommandTimeoutError,
)
from matek_theorem_agent.scientific import ScientificArtifactDeclaration
from matek_theorem_agent.stages.common import atomic_write_json
from matek_theorem_agent.stages.computation_artifacts import (
    ArtifactCollectionStatus,
    ComputationArtifactIssueCode,
    ComputationArtifactQuotas,
    ComputationArtifactStore,
    ComputationReplayIsolation,
    ComputationReplayLimits,
    ComputationReplayStatus,
    WorkerComputationEvidence,
    assignment_workspace_path,
    collect_computation_artifacts,
    prepare_assignment_workspace,
    replay_computation_artifacts,
    verify_persisted_computation_evidence,
)

SAFE_ISOLATION = ComputationReplayIsolation(
    filesystem_write_confined=True,
    network_disabled=True,
    description="offline unit-test fake",
)


def _run_root(tmp_path: Path) -> Path:
    run_root = tmp_path / "run"
    run_root.mkdir(parents=True)
    return run_root


def _declaration(
    *,
    path: str = "outputs/certificate.txt",
    command_line: list[str] | None = None,
    input_paths: list[str] | None = None,
    stdout_path: str | None = "captures/stdout.txt",
    stderr_path: str | None = "captures/stderr.txt",
    expected_output: str | None = "checked\n",
) -> ScientificArtifactDeclaration:
    return ScientificArtifactDeclaration(
        path=path,
        purpose="Reproduce a finite certificate",
        supporting_result_keys=["finite-case"],
        command_line=(
            command_line
            if command_line is not None
            else ["python3", "code/verify.py", "inputs/data.txt"]
        ),
        input_paths=(
            input_paths if input_paths is not None else ["code/verify.py", "inputs/data.txt"]
        ),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        expected_output=expected_output,
        replay_recipe="Run the fixed verifier argument array in the isolated workspace.",
        tool_versions=["python 3.11"],
    )


def _populate_workspace(
    workspace: Path,
    *,
    output: bytes = b"certificate-v1\n",
    stdout: bytes = b"checked\n",
    stderr: bytes = b"",
) -> None:
    files = {
        "code/verify.py": b"# deterministic verifier\n",
        "inputs/data.txt": b"1 2 3\n",
        "outputs/certificate.txt": output,
        "captures/stdout.txt": stdout,
        "captures/stderr.txt": stderr,
    }
    for relative, contents in files.items():
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)


class WritingBackend:
    def __init__(
        self,
        *,
        output: bytes = b"certificate-v1\n",
        stdout: str = "checked\n",
        stderr: str = "",
        exit_code: int = 0,
        extra_path: str | None = None,
        symlink_output: Path | None = None,
    ) -> None:
        self.output = output
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.extra_path = extra_path
        self.symlink_output = symlink_output
        self.requests: list[CommandRequest] = []

    async def run(self, request: CommandRequest) -> CommandResult:
        self.requests.append(request)
        assert (request.cwd / "code" / "verify.py").is_file()
        assert (request.cwd / "inputs" / "data.txt").read_bytes() == b"1 2 3\n"
        target = request.cwd / "outputs" / "certificate.txt"
        if self.symlink_output is not None:
            target.symlink_to(self.symlink_output)
        else:
            target.write_bytes(self.output)
        if self.extra_path is not None:
            extra = request.cwd / self.extra_path
            extra.parent.mkdir(parents=True, exist_ok=True)
            extra.write_text("unexpected", encoding="utf-8")
        return CommandResult(
            argv=request.argv,
            cwd=request.cwd,
            exit_code=self.exit_code,
            stdout=self.stdout,
            stderr=self.stderr,
            duration_seconds=0.01,
        )


class FailingBackend:
    def __init__(self) -> None:
        self.requests: list[CommandRequest] = []

    async def run(self, request: CommandRequest) -> CommandResult:
        self.requests.append(request)
        raise RuntimeError("sandbox unavailable")


class InputMutatingBackend(WritingBackend):
    async def run(self, request: CommandRequest) -> CommandResult:
        result = await super().run(request)
        input_path = request.cwd / "inputs" / "data.txt"
        input_path.chmod(0o600)
        input_path.write_text("changed\n", encoding="utf-8")
        return result


class TimeoutBackend:
    async def run(self, request: CommandRequest) -> CommandResult:
        raise CommandTimeoutError(
            CommandResult(
                argv=request.argv,
                cwd=request.cwd,
                exit_code=-1,
                stdout="partial",
                stderr="",
                duration_seconds=3.0,
                timed_out=True,
            )
        )


def test_assignment_workspace_is_deterministic_private_and_confined(tmp_path: Path) -> None:
    run_root = _run_root(tmp_path)

    expected = run_root / "research" / "workspaces" / "worker-01" / "scratch"
    assert assignment_workspace_path(run_root, "worker-01") == expected
    assert prepare_assignment_workspace(run_root, "worker-01") == expected
    assert prepare_assignment_workspace(run_root, "worker-01") == expected
    assert stat_mode(expected) == 0o700
    # The assignment root is the private Codex -C root and writable domain.
    assert stat_mode(expected.parent) == 0o700

    with pytest.raises(ValueError, match="assignment ID"):
        assignment_workspace_path(run_root, "../escape")


def test_collection_hashes_files_into_immutable_run_local_cas_and_reuses_manifest(
    tmp_path: Path,
) -> None:
    run_root = _run_root(tmp_path)
    store = ComputationArtifactStore(run_root)
    workspace = store.prepare_workspace("worker-01")
    _populate_workspace(workspace)
    declaration = _declaration()

    result = store.collect("worker-01", [declaration])

    assert result.status is ArtifactCollectionStatus.COLLECTED
    assert result.trusted
    assert result.manifest is not None
    assert result.manifest.workspace_file_count == 5
    assert result.manifest.retained_file_count == 5
    assert (
        result.manifest.declarations[0].primary_sha256
        == hashlib.sha256(b"certificate-v1\n").hexdigest()
    )
    assert (
        result.manifest.declarations[0].expected_stdout_sha256
        == hashlib.sha256(b"checked\n").hexdigest()
    )
    for retained in result.manifest.files:
        blob = run_root / retained.blob_path
        assert blob.is_file()
        assert blob.read_bytes() == (workspace / retained.relative_path).read_bytes()
        assert stat_mode(blob) == 0o400

    manifest_path = run_root / str(result.manifest_path)
    manifest_bytes = manifest_path.read_bytes()
    for path in sorted(workspace.rglob("*"), reverse=True):
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()

    reused = store.collect("worker-01", [])
    assert reused.status is ArtifactCollectionStatus.REUSED
    assert reused.manifest == result.manifest
    assert manifest_path.read_bytes() == manifest_bytes


def test_content_addressed_storage_deduplicates_equal_files(tmp_path: Path) -> None:
    run_root = _run_root(tmp_path)
    store = ComputationArtifactStore(run_root)
    workspace = store.prepare_workspace("worker-01")
    _populate_workspace(workspace, output=b"1 2 3\n")

    result = store.collect("worker-01", [_declaration(expected_output="checked\n")])

    assert result.manifest is not None
    output = next(
        item for item in result.manifest.files if item.relative_path.endswith("certificate.txt")
    )
    data = next(item for item in result.manifest.files if item.relative_path == "inputs/data.txt")
    assert output.sha256 == data.sha256
    assert output.blob_path == data.blob_path
    blob_root = run_root / "research" / "computations" / "blobs" / "sha256"
    assert len(list(blob_root.iterdir())) == 4


@pytest.mark.parametrize(
    ("extra_name", "quotas", "expected_code"),
    [
        (
            "undeclared.tmp",
            ComputationArtifactQuotas(),
            ComputationArtifactIssueCode.UNDECLARED_FILE,
        ),
        (
            None,
            ComputationArtifactQuotas(maximum_files=4),
            ComputationArtifactIssueCode.QUOTA_EXCEEDED,
        ),
        (
            None,
            ComputationArtifactQuotas(
                maximum_files=10,
                maximum_total_bytes=16,
                maximum_file_bytes=16,
            ),
            ComputationArtifactIssueCode.QUOTA_EXCEEDED,
        ),
    ],
)
def test_collection_rejects_undeclared_files_and_configured_quota_violations(
    tmp_path: Path,
    extra_name: str | None,
    quotas: ComputationArtifactQuotas,
    expected_code: ComputationArtifactIssueCode,
) -> None:
    run_root = _run_root(tmp_path)
    store = ComputationArtifactStore(run_root, quotas=quotas)
    workspace = store.prepare_workspace("worker-01")
    _populate_workspace(workspace)
    if extra_name is not None:
        (workspace / extra_name).write_text("temporary", encoding="utf-8")

    result = store.collect("worker-01", [_declaration()])

    assert result.status is ArtifactCollectionStatus.REJECTED
    assert result.issues[0].code is expected_code
    assert result.manifest is None


def test_collection_can_record_but_not_retain_explicitly_allowed_undeclared_files(
    tmp_path: Path,
) -> None:
    run_root = _run_root(tmp_path)
    quotas = ComputationArtifactQuotas(reject_undeclared_files=False)
    store = ComputationArtifactStore(run_root, quotas=quotas)
    workspace = store.prepare_workspace("worker-01")
    _populate_workspace(workspace)
    (workspace / "temporary.log").write_text("ignored", encoding="utf-8")

    result = store.collect("worker-01", [_declaration()])

    assert result.status is ArtifactCollectionStatus.COLLECTED
    assert result.manifest is not None
    assert result.manifest.ignored_paths == ["temporary.log"]
    assert result.manifest.workspace_file_count == 6
    assert result.manifest.retained_file_count == 5


def test_collection_rejects_symlink_hardlink_and_special_files(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    symlink_root = _run_root(tmp_path / "symlink-case")
    symlink_store = ComputationArtifactStore(symlink_root)
    symlink_workspace = symlink_store.prepare_workspace("worker")
    _populate_workspace(symlink_workspace)
    (symlink_workspace / "outputs" / "certificate.txt").unlink()
    (symlink_workspace / "outputs" / "certificate.txt").symlink_to(outside)
    symlink_result = symlink_store.collect("worker", [_declaration()])
    assert symlink_result.issues[0].code is ComputationArtifactIssueCode.SYMLINK

    hardlink_root = _run_root(tmp_path / "hardlink-case")
    hardlink_store = ComputationArtifactStore(hardlink_root)
    hardlink_workspace = hardlink_store.prepare_workspace("worker")
    _populate_workspace(hardlink_workspace)
    (hardlink_workspace / "outputs" / "certificate.txt").unlink()
    os.link(outside, hardlink_workspace / "outputs" / "certificate.txt")
    hardlink_result = hardlink_store.collect("worker", [_declaration()])
    assert hardlink_result.issues[0].code is ComputationArtifactIssueCode.HARDLINK

    if hasattr(os, "mkfifo"):
        fifo_root = _run_root(tmp_path / "fifo-case")
        fifo_store = ComputationArtifactStore(fifo_root)
        fifo_workspace = fifo_store.prepare_workspace("worker")
        _populate_workspace(fifo_workspace)
        os.mkfifo(fifo_workspace / "pipe")
        fifo_result = fifo_store.collect("worker", [_declaration()])
        assert fifo_result.issues[0].code is ComputationArtifactIssueCode.SPECIAL_FILE


@pytest.mark.parametrize(
    "declaration",
    [
        _declaration(path="C:\\outside.txt"),
        _declaration(command_line=["python3", "../outside.py"]),
        _declaration(command_line=["python3", "https://example.invalid/tool"]),
        _declaration(command_line=[]),
    ],
)
def test_collection_rejects_absolute_escape_network_and_missing_argv(
    tmp_path: Path,
    declaration: ScientificArtifactDeclaration,
) -> None:
    run_root = _run_root(tmp_path)
    store = ComputationArtifactStore(run_root)
    workspace = store.prepare_workspace("worker")
    _populate_workspace(workspace)

    result = store.collect("worker", [declaration])

    assert result.status is ArtifactCollectionStatus.REJECTED
    assert result.issues[0].code in {
        ComputationArtifactIssueCode.INVALID_PATH,
        ComputationArtifactIssueCode.UNSAFE_COMMAND,
    }


def test_collection_rejects_missing_files_and_conflicting_stdout_contract(tmp_path: Path) -> None:
    missing_root = _run_root(tmp_path / "missing")
    missing_store = ComputationArtifactStore(missing_root)
    missing_workspace = missing_store.prepare_workspace("worker")
    _populate_workspace(missing_workspace)
    (missing_workspace / "inputs" / "data.txt").unlink()

    missing = missing_store.collect("worker", [_declaration()])
    assert missing.issues[0].code is ComputationArtifactIssueCode.MISSING_FILE

    conflict_root = _run_root(tmp_path / "conflict")
    conflict_store = ComputationArtifactStore(conflict_root)
    conflict_workspace = conflict_store.prepare_workspace("worker")
    _populate_workspace(conflict_workspace, stdout=b"different\n")
    conflict = conflict_store.collect("worker", [_declaration(expected_output="checked\n")])
    assert conflict.issues[0].code is ComputationArtifactIssueCode.INVALID_DECLARATION


def test_committed_manifest_and_cas_corruption_fail_closed(tmp_path: Path) -> None:
    manifest_root = _run_root(tmp_path / "manifest-case")
    manifest_store = ComputationArtifactStore(manifest_root)
    manifest_workspace = manifest_store.prepare_workspace("worker")
    _populate_workspace(manifest_workspace)
    collected = manifest_store.collect("worker", [_declaration()])
    assert collected.manifest_path is not None
    manifest_path = manifest_root / collected.manifest_path
    manifest_path.chmod(0o600)
    manifest_path.write_text("{}\n", encoding="utf-8")

    corrupted_manifest = manifest_store.collect("worker", [_declaration()])
    assert corrupted_manifest.status is ArtifactCollectionStatus.INTEGRITY_FAILED
    assert (
        corrupted_manifest.issues[0].code is ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT
    )

    blob_root = _run_root(tmp_path / "blob-case")
    blob_store = ComputationArtifactStore(blob_root)
    blob_workspace = blob_store.prepare_workspace("worker")
    _populate_workspace(blob_workspace)
    blob_collected = blob_store.collect("worker", [_declaration()])
    assert blob_collected.manifest is not None
    blob = blob_root / blob_collected.manifest.files[0].blob_path
    blob.chmod(0o600)
    blob.write_text("tampered", encoding="utf-8")

    corrupted_blob = blob_store.collect("worker", [_declaration()])
    assert corrupted_blob.status is ArtifactCollectionStatus.INTEGRITY_FAILED
    assert corrupted_blob.issues[0].code is ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT


def test_committed_assignment_rejects_a_different_declaration_identity(tmp_path: Path) -> None:
    run_root = _run_root(tmp_path)
    store = ComputationArtifactStore(run_root)
    workspace = store.prepare_workspace("worker")
    _populate_workspace(workspace)
    assert store.collect("worker", [_declaration()]).trusted

    changed = _declaration(command_line=["python3", "code/verify.py", "--different"])
    conflict = store.collect("worker", [changed])

    assert conflict.status is ArtifactCollectionStatus.INTEGRITY_FAILED
    assert conflict.issues[0].code is ComputationArtifactIssueCode.MANIFEST_CONFLICT


@pytest.mark.asyncio
async def test_replay_uses_fresh_workspace_compares_all_evidence_and_is_cached(
    tmp_path: Path,
) -> None:
    run_root = _run_root(tmp_path)
    workspace = prepare_assignment_workspace(run_root, "worker")
    _populate_workspace(workspace)
    collected = collect_computation_artifacts(run_root, "worker", [_declaration()])
    assert collected.status is ArtifactCollectionStatus.COLLECTED
    backend = WritingBackend()

    replay = await replay_computation_artifacts(
        run_root,
        "worker",
        backend,
        isolation=SAFE_ISOLATION,
    )

    assert replay.status is ComputationReplayStatus.PASSED
    assert replay.trusted
    assert not replay.reused
    assert len(backend.requests) == 1
    request = backend.requests[0]
    assert request.argv == ("python3", "code/verify.py", "inputs/data.txt")
    assert request.cwd != workspace
    assert request.cwd.is_relative_to(run_root)
    assert replay.commands[0].passed
    assert replay.commands[0].actual_output_sha256 == replay.commands[0].expected_output_sha256

    unused_backend = FailingBackend()
    cached = await replay_computation_artifacts(
        run_root,
        "worker",
        unused_backend,
        isolation=SAFE_ISOLATION,
    )
    assert cached.status is ComputationReplayStatus.PASSED
    assert cached.reused
    assert unused_backend.requests == []


@pytest.mark.asyncio
async def test_persisted_computation_evidence_revalidates_cas_and_terminal_verdict(
    tmp_path: Path,
) -> None:
    run_root = _run_root(tmp_path)
    workspace = prepare_assignment_workspace(run_root, "worker")
    _populate_workspace(workspace)
    collection = collect_computation_artifacts(run_root, "worker", [_declaration()])
    replay = await replay_computation_artifacts(
        run_root,
        "worker",
        WritingBackend(),
        isolation=SAFE_ISOLATION,
    )
    evidence = WorkerComputationEvidence(
        assignment_id="worker",
        collection=collection,
        replay=replay,
    )
    evidence_path = run_root / "research" / "worker-computation" / "worker.json"
    atomic_write_json(evidence_path, evidence)

    assert verify_persisted_computation_evidence(run_root, "worker", evidence_path) == evidence

    assert collection.manifest is not None
    blob_path = run_root / collection.manifest.files[0].blob_path
    blob_path.chmod(0o600)
    blob_path.write_bytes(b"tampered\n")
    with pytest.raises(ValueError, match="blob does not match"):
        verify_persisted_computation_evidence(run_root, "worker", evidence_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "expected_code"),
    [
        (WritingBackend(exit_code=7), ComputationArtifactIssueCode.EXIT_STATUS_MISMATCH),
        (WritingBackend(stdout="wrong\n"), ComputationArtifactIssueCode.STDOUT_MISMATCH),
        (WritingBackend(stderr="warning\n"), ComputationArtifactIssueCode.STDERR_MISMATCH),
        (WritingBackend(output=b"different\n"), ComputationArtifactIssueCode.OUTPUT_MISMATCH),
        (
            WritingBackend(extra_path="unexpected.txt"),
            ComputationArtifactIssueCode.REPLAY_WORKSPACE_INVALID,
        ),
    ],
)
async def test_replay_fails_closed_for_each_deterministic_mismatch(
    tmp_path: Path,
    backend: WritingBackend,
    expected_code: ComputationArtifactIssueCode,
) -> None:
    run_root = _run_root(tmp_path)
    workspace = prepare_assignment_workspace(run_root, "worker")
    _populate_workspace(workspace)
    collect_computation_artifacts(run_root, "worker", [_declaration()])

    replay = await replay_computation_artifacts(
        run_root,
        "worker",
        backend,
        isolation=SAFE_ISOLATION,
    )

    assert replay.status is ComputationReplayStatus.MISMATCH
    assert not replay.trusted
    assert expected_code in {issue.code for issue in replay.issues}


@pytest.mark.asyncio
async def test_replay_rejects_symlink_output(tmp_path: Path) -> None:
    run_root = _run_root(tmp_path)
    workspace = prepare_assignment_workspace(run_root, "worker")
    _populate_workspace(workspace)
    collect_computation_artifacts(run_root, "worker", [_declaration()])
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    replay = await replay_computation_artifacts(
        run_root,
        "worker",
        WritingBackend(symlink_output=outside),
        isolation=SAFE_ISOLATION,
    )

    assert replay.status is ComputationReplayStatus.MISMATCH
    assert ComputationArtifactIssueCode.OUTPUT_MISSING in {issue.code for issue in replay.issues}
    assert outside.read_text(encoding="utf-8") == "outside"


@pytest.mark.asyncio
async def test_replay_rejects_input_mutation_and_unbounded_backend_output(tmp_path: Path) -> None:
    mutation_root = _run_root(tmp_path / "mutation")
    mutation_workspace = prepare_assignment_workspace(mutation_root, "worker")
    _populate_workspace(mutation_workspace)
    collect_computation_artifacts(mutation_root, "worker", [_declaration()])

    mutation = await replay_computation_artifacts(
        mutation_root,
        "worker",
        InputMutatingBackend(),
        isolation=SAFE_ISOLATION,
    )
    assert mutation.status is ComputationReplayStatus.MISMATCH
    assert ComputationArtifactIssueCode.OUTPUT_MISMATCH in {issue.code for issue in mutation.issues}

    bounded_root = _run_root(tmp_path / "bounded")
    bounded_workspace = prepare_assignment_workspace(bounded_root, "worker")
    _populate_workspace(bounded_workspace)
    collect_computation_artifacts(bounded_root, "worker", [_declaration()])
    bounded = await replay_computation_artifacts(
        bounded_root,
        "worker",
        WritingBackend(stdout="checked\n"),
        isolation=SAFE_ISOLATION,
        limits=ComputationReplayLimits(maximum_output_bytes=4),
    )
    assert bounded.status is ComputationReplayStatus.EXECUTION_FAILED
    assert bounded.commands[0].stdout_truncated
    assert len(bounded.commands[0].stdout.encode("utf-8")) <= 4


@pytest.mark.asyncio
async def test_replay_refuses_unattested_backend_without_executing(tmp_path: Path) -> None:
    run_root = _run_root(tmp_path)
    workspace = prepare_assignment_workspace(run_root, "worker")
    _populate_workspace(workspace)
    collect_computation_artifacts(run_root, "worker", [_declaration()])
    backend = WritingBackend()

    replay = await replay_computation_artifacts(
        run_root,
        "worker",
        backend,
        isolation=ComputationReplayIsolation(
            filesystem_write_confined=False,
            network_disabled=False,
            description="ordinary native cwd only",
        ),
    )

    assert replay.status is ComputationReplayStatus.UNSAFE_BACKEND
    assert replay.issues[0].code is ComputationArtifactIssueCode.UNSAFE_REPLAY_BACKEND
    assert backend.requests == []


@pytest.mark.asyncio
async def test_replay_reports_retryable_backend_failure_and_timeout(tmp_path: Path) -> None:
    failure_root = _run_root(tmp_path / "failure")
    failure_workspace = prepare_assignment_workspace(failure_root, "worker")
    _populate_workspace(failure_workspace)
    collect_computation_artifacts(failure_root, "worker", [_declaration()])
    failing = FailingBackend()

    failure = await replay_computation_artifacts(
        failure_root,
        "worker",
        failing,
        isolation=SAFE_ISOLATION,
    )
    assert failure.status is ComputationReplayStatus.EXECUTION_FAILED
    assert failure.issues[0].code is ComputationArtifactIssueCode.REPLAY_EXECUTION_FAILED
    attempts = list(
        (
            failure_root
            / "research"
            / "computations"
            / "replays"
            / "worker"
            / str(failure.manifest_sha256)
            / "attempts"
        ).glob("*.json")
    )
    assert [path.name for path in attempts] == ["00000001.json"]

    retry = await replay_computation_artifacts(
        failure_root,
        "worker",
        WritingBackend(),
        isolation=SAFE_ISOLATION,
    )
    assert retry.status is ComputationReplayStatus.PASSED
    assert not retry.reused

    timeout_root = _run_root(tmp_path / "timeout")
    timeout_workspace = prepare_assignment_workspace(timeout_root, "worker")
    _populate_workspace(timeout_workspace)
    collect_computation_artifacts(timeout_root, "worker", [_declaration()])
    timeout = await replay_computation_artifacts(
        timeout_root,
        "worker",
        TimeoutBackend(),
        isolation=SAFE_ISOLATION,
    )
    assert timeout.status is ComputationReplayStatus.EXECUTION_FAILED
    assert timeout.issues[0].code is ComputationArtifactIssueCode.REPLAY_TIMEOUT


@pytest.mark.asyncio
async def test_replay_without_committed_manifest_is_not_trusted(tmp_path: Path) -> None:
    run_root = _run_root(tmp_path)

    result = await replay_computation_artifacts(
        run_root,
        "worker",
        WritingBackend(),
        isolation=SAFE_ISOLATION,
    )

    assert result.status is ComputationReplayStatus.NOT_COLLECTED
    assert not result.trusted
    assert result.issues[0].code is ComputationArtifactIssueCode.MANIFEST_MISSING


def stat_mode(path: Path) -> int:
    return os.stat(path, follow_symlinks=False).st_mode & 0o777
