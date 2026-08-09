"""Collection and deterministic replay of assignment-private computation artifacts.

Research workers declare files through :class:`ScientificArtifactDeclaration`, but those
declarations are untrusted.  This module owns path validation, filesystem inspection, hashes,
content-addressed storage, and replay verdicts.  It deliberately has no dependency on the
research scheduler so it can be used at the report-admission boundary without weakening that
boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..execution.base import (
    CommandRequest,
    CommandResult,
    CommandTimeoutError,
    ExecutionBackend,
)
from ..redaction import redact_text
from ..scientific import ScientificArtifactDeclaration
from .common import canonical_json_bytes

_ASSIGNMENT_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_URI_SCHEME = re.compile(r"\A[A-Za-z][A-Za-z0-9+.-]*://")
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_READ_CHUNK_SIZE = 1024 * 1024


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactCollectionStatus(StrEnum):
    COLLECTED = "collected"
    REUSED = "reused"
    REJECTED = "rejected"
    INTEGRITY_FAILED = "integrity_failed"


class ComputationReplayStatus(StrEnum):
    PASSED = "passed"
    MISMATCH = "mismatch"
    EXECUTION_FAILED = "execution_failed"
    INTEGRITY_FAILED = "integrity_failed"
    NOT_COLLECTED = "not_collected"
    UNSAFE_BACKEND = "unsafe_backend"


class ComputationArtifactIssueCode(StrEnum):
    INVALID_ASSIGNMENT_ID = "invalid_assignment_id"
    INVALID_DECLARATION = "invalid_declaration"
    INVALID_PATH = "invalid_path"
    UNSAFE_COMMAND = "unsafe_command"
    MISSING_FILE = "missing_file"
    UNDECLARED_FILE = "undeclared_file"
    SYMLINK = "symlink"
    HARDLINK = "hardlink"
    SPECIAL_FILE = "special_file"
    QUOTA_EXCEEDED = "quota_exceeded"
    FILE_CHANGED = "file_changed"
    MANIFEST_CONFLICT = "manifest_conflict"
    IMMUTABLE_ARTIFACT_CORRUPT = "immutable_artifact_corrupt"
    MANIFEST_MISSING = "manifest_missing"
    UNSAFE_REPLAY_BACKEND = "unsafe_replay_backend"
    REPLAY_TIMEOUT = "replay_timeout"
    REPLAY_EXECUTION_FAILED = "replay_execution_failed"
    EXIT_STATUS_MISMATCH = "exit_status_mismatch"
    STDOUT_MISMATCH = "stdout_mismatch"
    STDERR_MISMATCH = "stderr_mismatch"
    OUTPUT_MISSING = "output_missing"
    OUTPUT_MISMATCH = "output_mismatch"
    REPLAY_WORKSPACE_INVALID = "replay_workspace_invalid"


class ComputationArtifactIssue(_StrictModel):
    code: ComputationArtifactIssueCode
    detail: str
    path: str | None = None

    @field_validator("detail")
    @classmethod
    def detail_is_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("artifact issue detail must not be blank")
        return normalized


class ComputationArtifactQuotas(_StrictModel):
    """Limits applied to the entire private workspace, not just retained files."""

    maximum_files: int = Field(default=256, ge=1)
    maximum_total_bytes: int = Field(default=64 * 1024 * 1024, ge=1)
    maximum_file_bytes: int = Field(default=16 * 1024 * 1024, ge=1)
    reject_undeclared_files: bool = True

    @model_validator(mode="after")
    def individual_file_fits_total(self) -> Self:
        if self.maximum_file_bytes > self.maximum_total_bytes:
            raise ValueError("maximum_file_bytes must not exceed maximum_total_bytes")
        return self


class ComputationReplayLimits(_StrictModel):
    timeout_seconds: int = Field(default=600, ge=1)
    maximum_output_bytes: int = Field(default=4 * 1024 * 1024, ge=1)


class ComputationReplayIsolation(_StrictModel):
    """Application attestation for an injected command backend.

    The generic :class:`ExecutionBackend` protocol cannot itself prove sandbox properties.
    Callers must therefore attest both properties from their resolved backend configuration.
    Replay refuses to start unless both are true.  In particular, an ordinary native backend
    must not be attested as filesystem-confined merely because its cwd is private.
    """

    filesystem_write_confined: bool
    network_disabled: bool
    description: str = ""


class CollectedComputationFile(_StrictModel):
    relative_path: str
    sha256: str
    size_bytes: int = Field(ge=0)
    blob_path: str
    executable: bool = False
    roles: list[str] = Field(default_factory=list)

    @field_validator("sha256")
    @classmethod
    def digest_is_sha256(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("collected file digest must be lowercase SHA-256")
        return value


class CollectedComputationDeclaration(_StrictModel):
    declaration_sha256: str
    primary_path: str
    purpose: str
    supporting_result_keys: list[str] = Field(default_factory=list)
    argv: list[str]
    input_paths: list[str] = Field(default_factory=list)
    stdout_reference_path: str | None = None
    stderr_reference_path: str | None = None
    expected_output: str | None = None
    replay_recipe: str
    tool_versions: list[str] = Field(default_factory=list)
    expected_exit_code: int = 0
    primary_sha256: str
    input_sha256: dict[str, str] = Field(default_factory=dict)
    expected_stdout_sha256: str
    expected_stderr_sha256: str

    @field_validator(
        "declaration_sha256",
        "primary_sha256",
        "expected_stdout_sha256",
        "expected_stderr_sha256",
    )
    @classmethod
    def digests_are_sha256(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("declaration evidence digest must be lowercase SHA-256")
        return value

    @field_validator("input_sha256")
    @classmethod
    def input_digests_are_sha256(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not _SHA256.fullmatch(digest) for digest in value.values()):
            raise ValueError("input evidence digests must be lowercase SHA-256")
        return value


class ComputationArtifactManifest(_StrictModel):
    schema_version: Literal[1] = 1
    assignment_id: str
    workspace_path: str
    declarations: list[CollectedComputationDeclaration]
    files: list[CollectedComputationFile]
    workspace_file_count: int = Field(ge=0)
    workspace_total_bytes: int = Field(ge=0)
    retained_file_count: int = Field(ge=0)
    retained_total_bytes: int = Field(ge=0)
    ignored_paths: list[str] = Field(default_factory=list)
    manifest_sha256: str

    @model_validator(mode="after")
    def content_hash_is_valid(self) -> Self:
        expected = _model_content_sha256(self, excluded={"manifest_sha256"})
        if self.manifest_sha256 != expected:
            raise ValueError("computation manifest content hash does not match its payload")
        try:
            _validate_manifest_structure(self)
        except _ArtifactFailure as exc:
            raise ValueError(exc.issue.detail) from exc
        return self


class ComputationArtifactCollectionResult(_StrictModel):
    status: ArtifactCollectionStatus
    assignment_id: str
    workspace_path: str
    manifest_path: str | None = None
    manifest: ComputationArtifactManifest | None = None
    issues: list[ComputationArtifactIssue] = Field(default_factory=list)

    @property
    def trusted(self) -> bool:
        return self.status in {
            ArtifactCollectionStatus.COLLECTED,
            ArtifactCollectionStatus.REUSED,
        }


class ComputationReplayCommandEvidence(_StrictModel):
    primary_path: str
    argv: list[str]
    expected_exit_code: int
    actual_exit_code: int | None
    expected_stdout_sha256: str
    actual_stdout_sha256: str | None
    expected_stderr_sha256: str
    actual_stderr_sha256: str | None
    expected_output_sha256: str
    actual_output_sha256: str | None
    stdout: str
    stderr: str
    duration_seconds: float = Field(ge=0)
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False
    passed: bool


class ComputationReplayResult(_StrictModel):
    schema_version: Literal[1] = 1
    status: ComputationReplayStatus
    assignment_id: str
    manifest_sha256: str | None
    replay_workspace_path: str | None
    isolation: ComputationReplayIsolation
    commands: list[ComputationReplayCommandEvidence] = Field(default_factory=list)
    issues: list[ComputationArtifactIssue] = Field(default_factory=list)
    record_sha256: str
    reused: bool = False

    @model_validator(mode="after")
    def content_hash_is_valid(self) -> Self:
        expected = _model_content_sha256(
            self,
            excluded={"record_sha256", "reused"},
        )
        if self.record_sha256 != expected:
            raise ValueError("computation replay record hash does not match its payload")
        return self

    @property
    def trusted(self) -> bool:
        return self.status is ComputationReplayStatus.PASSED


class WorkerComputationEvidence(_StrictModel):
    """Application-owned collection/replay transaction for one assignment."""

    schema_version: Literal[1] = 1
    assignment_id: str
    collection: ComputationArtifactCollectionResult | None = None
    replay: ComputationReplayResult | None = None

    @model_validator(mode="after")
    def replay_requires_collection(self) -> Self:
        if self.replay is not None and self.collection is None:
            raise ValueError("computation replay evidence requires collection evidence")
        return self


@dataclass(frozen=True)
class _ScannedFile:
    relative_path: str
    absolute_path: Path
    status: os.stat_result


@dataclass(frozen=True)
class _NormalizedDeclaration:
    original: ScientificArtifactDeclaration
    primary_path: str
    argv: tuple[str, ...]
    input_paths: tuple[str, ...]
    stdout_path: str | None
    stderr_path: str | None


class _ArtifactFailure(RuntimeError):
    def __init__(
        self,
        code: ComputationArtifactIssueCode,
        detail: str,
        *,
        path: str | None = None,
        integrity: bool = False,
    ) -> None:
        self.issue = ComputationArtifactIssue(code=code, detail=detail, path=path)
        self.integrity = integrity
        super().__init__(detail)


class ComputationArtifactStore:
    """Run-local artifact collector and replay service."""

    def __init__(
        self,
        run_root: Path,
        *,
        quotas: ComputationArtifactQuotas | None = None,
    ) -> None:
        self.run_root = _validated_run_root(run_root)
        self.quotas = quotas or ComputationArtifactQuotas()

    def workspace_path(self, assignment_id: str) -> Path:
        """Return the assignment's declared evidence area (``scratch/``).

        The assignment's parent directory is the private Codex ``-C`` root and writable
        domain so that Codex's own control directories (``.agents``, ``.codex``, ``.git``)
        are worker-owned state; this path remains the directory whose declared files are
        collected as computation evidence.
        """

        identifier = _validate_assignment_id(assignment_id)
        return self.run_root / "research" / "workspaces" / identifier / "scratch"

    def prepare_workspace(self, assignment_id: str) -> Path:
        workspace = self.workspace_path(assignment_id)
        _ensure_internal_directory(self.run_root, workspace, mode=0o700)
        try:
            os.chmod(workspace, 0o700)
            os.chmod(workspace.parent, 0o700)
        except OSError as exc:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.REPLAY_WORKSPACE_INVALID,
                f"cannot make assignment workspace private: {exc}",
                path=_relative_to_run(self.run_root, workspace),
            ) from exc
        return workspace

    def collect(
        self,
        assignment_id: str,
        declarations: Sequence[ScientificArtifactDeclaration],
    ) -> ComputationArtifactCollectionResult:
        """Validate, hash, and immutably commit one assignment's declarations.

        A committed manifest wins on resume: it is verified with every referenced blob and
        returned without consulting mutable scratch state.  An assignment can therefore never
        silently replace already admitted computational evidence.
        """

        try:
            identifier = _validate_assignment_id(assignment_id)
        except ValueError as exc:
            return _collection_failure(
                assignment_id,
                "",
                _ArtifactFailure(
                    ComputationArtifactIssueCode.INVALID_ASSIGNMENT_ID,
                    str(exc),
                ),
            )

        workspace = self.workspace_path(identifier)
        workspace_relative = _relative_to_run(self.run_root, workspace)
        manifest_path = self._manifest_path(identifier)
        try:
            manifest_exists = _lexists(manifest_path)
        except _ArtifactFailure as failure:
            return _collection_failure(identifier, workspace_relative, failure)
        if manifest_exists:
            try:
                manifest = self._load_manifest(identifier)
                if declarations:
                    normalized_existing = _normalize_declarations(declarations)
                    incoming_hashes = [
                        hashlib.sha256(canonical_json_bytes(item.original)).hexdigest()
                        for item in normalized_existing
                    ]
                    committed_hashes = [item.declaration_sha256 for item in manifest.declarations]
                    if incoming_hashes != committed_hashes:
                        raise _ArtifactFailure(
                            ComputationArtifactIssueCode.MANIFEST_CONFLICT,
                            "assignment declarations differ from its immutable committed manifest",
                            path=_relative_to_run(self.run_root, manifest_path),
                            integrity=True,
                        )
            except _ArtifactFailure as failure:
                return _collection_failure(identifier, workspace_relative, failure)
            return ComputationArtifactCollectionResult(
                status=ArtifactCollectionStatus.REUSED,
                assignment_id=identifier,
                workspace_path=workspace_relative,
                manifest_path=_relative_to_run(self.run_root, manifest_path),
                manifest=manifest,
            )

        try:
            normalized = _normalize_declarations(declarations)
            if not normalized:
                raise _ArtifactFailure(
                    ComputationArtifactIssueCode.INVALID_DECLARATION,
                    "at least one computation artifact declaration is required",
                )
            _require_directory(workspace, self.run_root, label="assignment workspace")
            scanned = _scan_workspace(workspace, self.quotas)
            scanned_by_path = {item.relative_path: item for item in scanned}
            roles = _declared_file_roles(normalized)
            referenced_paths = set(roles)
            for relative_path in sorted(referenced_paths):
                if relative_path not in scanned_by_path:
                    raise _ArtifactFailure(
                        ComputationArtifactIssueCode.MISSING_FILE,
                        "declared computation file does not exist as a regular file",
                        path=relative_path,
                    )
            ignored_paths = sorted(set(scanned_by_path) - referenced_paths)
            if ignored_paths and self.quotas.reject_undeclared_files:
                raise _ArtifactFailure(
                    ComputationArtifactIssueCode.UNDECLARED_FILE,
                    "private workspace contains undeclared regular files",
                    path=ignored_paths[0],
                )

            files: list[CollectedComputationFile] = []
            for relative_path in sorted(referenced_paths):
                files.append(
                    self._store_scanned_file(
                        scanned_by_path[relative_path],
                        roles=sorted(roles[relative_path]),
                    )
                )
            file_by_path = {item.relative_path: item for item in files}
            collected_declarations = [
                _build_collected_declaration(item, file_by_path) for item in normalized
            ]
            manifest_payload: dict[str, Any] = {
                "schema_version": 1,
                "assignment_id": identifier,
                "workspace_path": workspace_relative,
                "declarations": [item.model_dump(mode="json") for item in collected_declarations],
                "files": [item.model_dump(mode="json") for item in files],
                "workspace_file_count": len(scanned),
                "workspace_total_bytes": sum(item.status.st_size for item in scanned),
                "retained_file_count": len(files),
                "retained_total_bytes": sum(item.size_bytes for item in files),
                "ignored_paths": ignored_paths,
            }
            manifest_payload["manifest_sha256"] = _sha256_canonical(manifest_payload)
            manifest = ComputationArtifactManifest.model_validate(manifest_payload)
            _publish_immutable_bytes(
                manifest_path,
                canonical_json_bytes(manifest),
                confinement_root=self.run_root,
            )
        except _ArtifactFailure as failure:
            return _collection_failure(identifier, workspace_relative, failure)
        except (OSError, ValueError) as exc:
            collection_failure = _ArtifactFailure(
                ComputationArtifactIssueCode.INVALID_DECLARATION,
                f"could not collect computation artifacts: {exc}",
            )
            return _collection_failure(identifier, workspace_relative, collection_failure)

        return ComputationArtifactCollectionResult(
            status=ArtifactCollectionStatus.COLLECTED,
            assignment_id=identifier,
            workspace_path=workspace_relative,
            manifest_path=_relative_to_run(self.run_root, manifest_path),
            manifest=manifest,
        )

    def load_manifest(self, assignment_id: str) -> ComputationArtifactManifest:
        """Load a committed manifest and verify every immutable CAS blob."""

        identifier = _validate_assignment_id(assignment_id)
        return self._load_manifest(identifier)

    def load_replay_result(
        self,
        assignment_id: str,
        manifest_sha256: str,
    ) -> ComputationReplayResult:
        """Load and integrity-check the immutable terminal replay verdict."""

        identifier = _validate_assignment_id(assignment_id)
        manifest = self._load_manifest(identifier)
        if manifest.manifest_sha256 != manifest_sha256:
            raise ValueError("replay verdict request does not match the committed manifest")
        path = self._replay_verdict_path(identifier, manifest_sha256)
        if not _lexists(path):
            raise ValueError("no immutable terminal replay verdict exists for this manifest")
        try:
            result = _load_replay_result(path, self.run_root)
        except _ArtifactFailure as exc:
            raise ValueError(exc.issue.detail) from exc
        if result.assignment_id != identifier or result.manifest_sha256 != manifest_sha256:
            raise ValueError("immutable replay verdict is bound to another assignment or manifest")
        return result

    async def replay(
        self,
        assignment_id: str,
        backend: ExecutionBackend,
        *,
        isolation: ComputationReplayIsolation,
        limits: ComputationReplayLimits | None = None,
    ) -> ComputationReplayResult:
        """Replay a committed manifest in a fresh, backend-isolated workspace."""

        limits = limits or ComputationReplayLimits()
        try:
            identifier = _validate_assignment_id(assignment_id)
        except ValueError as exc:
            return _new_replay_result(
                status=ComputationReplayStatus.NOT_COLLECTED,
                assignment_id=assignment_id,
                manifest_sha256=None,
                replay_workspace_path=None,
                isolation=isolation,
                issues=[
                    ComputationArtifactIssue(
                        code=ComputationArtifactIssueCode.INVALID_ASSIGNMENT_ID,
                        detail=str(exc),
                    )
                ],
            )

        try:
            manifest = self._load_manifest(identifier)
        except _ArtifactFailure as failure:
            status = (
                ComputationReplayStatus.INTEGRITY_FAILED
                if failure.integrity
                else ComputationReplayStatus.NOT_COLLECTED
            )
            return _new_replay_result(
                status=status,
                assignment_id=identifier,
                manifest_sha256=None,
                replay_workspace_path=None,
                isolation=isolation,
                issues=[failure.issue],
            )

        replay_relative = (
            f"research/computations/replay-workspaces/{identifier}/{manifest.manifest_sha256}"
        )
        replay_workspace = self.run_root / Path(replay_relative)
        verdict_path = self._replay_verdict_path(identifier, manifest.manifest_sha256)
        try:
            verdict_exists = _lexists(verdict_path)
        except _ArtifactFailure as failure:
            return _new_replay_result(
                status=ComputationReplayStatus.INTEGRITY_FAILED,
                assignment_id=identifier,
                manifest_sha256=manifest.manifest_sha256,
                replay_workspace_path=replay_relative,
                isolation=isolation,
                issues=[failure.issue],
            )
        if verdict_exists:
            try:
                cached = _load_replay_result(verdict_path, self.run_root)
                if (
                    cached.assignment_id != identifier
                    or cached.manifest_sha256 != manifest.manifest_sha256
                ):
                    raise _ArtifactFailure(
                        ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
                        "cached replay verdict does not match its manifest identity",
                        path=_relative_to_run(self.run_root, verdict_path),
                        integrity=True,
                    )
                return cached.model_copy(update={"reused": True})
            except _ArtifactFailure as failure:
                return _new_replay_result(
                    status=ComputationReplayStatus.INTEGRITY_FAILED,
                    assignment_id=identifier,
                    manifest_sha256=manifest.manifest_sha256,
                    replay_workspace_path=replay_relative,
                    isolation=isolation,
                    issues=[failure.issue],
                )

        if not isolation.filesystem_write_confined or not isolation.network_disabled:
            missing: list[str] = []
            if not isolation.filesystem_write_confined:
                missing.append("filesystem write confinement")
            if not isolation.network_disabled:
                missing.append("network isolation")
            return _new_replay_result(
                status=ComputationReplayStatus.UNSAFE_BACKEND,
                assignment_id=identifier,
                manifest_sha256=manifest.manifest_sha256,
                replay_workspace_path=replay_relative,
                isolation=isolation,
                issues=[
                    ComputationArtifactIssue(
                        code=ComputationArtifactIssueCode.UNSAFE_REPLAY_BACKEND,
                        detail=(
                            "replay refused because the injected backend does not attest "
                            + " and ".join(missing)
                        ),
                    )
                ],
            )

        try:
            _reset_replay_workspace(self.run_root, replay_workspace)
            self._materialize_replay_inputs(manifest, replay_workspace)
        except _ArtifactFailure as failure:
            return _new_replay_result(
                status=ComputationReplayStatus.INTEGRITY_FAILED,
                assignment_id=identifier,
                manifest_sha256=manifest.manifest_sha256,
                replay_workspace_path=replay_relative,
                isolation=isolation,
                issues=[failure.issue],
            )

        command_evidence: list[ComputationReplayCommandEvidence] = []
        issues: list[ComputationArtifactIssue] = []
        terminal_status = ComputationReplayStatus.PASSED
        for declaration in manifest.declarations:
            request = CommandRequest(
                argv=tuple(declaration.argv),
                cwd=replay_workspace,
                timeout_seconds=limits.timeout_seconds,
                max_output_bytes=limits.maximum_output_bytes,
            )
            try:
                result = await backend.run(request)
            except CommandTimeoutError as exc:
                result = exc.result
                issue = ComputationArtifactIssue(
                    code=ComputationArtifactIssueCode.REPLAY_TIMEOUT,
                    detail="computation replay command timed out",
                    path=declaration.primary_path,
                )
                evidence = _command_evidence(
                    declaration,
                    result,
                    replay_workspace,
                    maximum_output_bytes=limits.maximum_output_bytes,
                )
                command_evidence.append(evidence)
                issues.append(issue)
                terminal_status = ComputationReplayStatus.EXECUTION_FAILED
                break
            except Exception as exc:  # execution backends intentionally expose varied failures
                issues.append(
                    ComputationArtifactIssue(
                        code=ComputationArtifactIssueCode.REPLAY_EXECUTION_FAILED,
                        detail=(
                            "computation replay backend failed: "
                            f"{type(exc).__name__}: {redact_text(str(exc))}"
                        ),
                        path=declaration.primary_path,
                    )
                )
                terminal_status = ComputationReplayStatus.EXECUTION_FAILED
                break

            evidence = _command_evidence(
                declaration,
                result,
                replay_workspace,
                maximum_output_bytes=limits.maximum_output_bytes,
            )
            command_evidence.append(evidence)
            result_cwd = Path(os.path.abspath(result.cwd))
            if result.argv != request.argv or result_cwd != replay_workspace:
                issues.append(
                    ComputationArtifactIssue(
                        code=ComputationArtifactIssueCode.REPLAY_EXECUTION_FAILED,
                        detail=(
                            "execution backend returned evidence for a different argv or "
                            "working directory"
                        ),
                        path=declaration.primary_path,
                    )
                )
                terminal_status = ComputationReplayStatus.EXECUTION_FAILED
                break
            mismatch_issues = _command_mismatch_issues(declaration, evidence)
            if mismatch_issues:
                issues.extend(mismatch_issues)
                terminal_status = (
                    ComputationReplayStatus.EXECUTION_FAILED
                    if evidence.timed_out or evidence.stdout_truncated or evidence.stderr_truncated
                    else ComputationReplayStatus.MISMATCH
                )
                break

        if terminal_status is ComputationReplayStatus.PASSED:
            try:
                _validate_replay_workspace(manifest, replay_workspace, self.quotas)
            except _ArtifactFailure as failure:
                issues.append(failure.issue)
                terminal_status = ComputationReplayStatus.MISMATCH

        replay_result = _new_replay_result(
            status=terminal_status,
            assignment_id=identifier,
            manifest_sha256=manifest.manifest_sha256,
            replay_workspace_path=replay_relative,
            isolation=isolation,
            commands=command_evidence,
            issues=issues,
        )
        if terminal_status in {
            ComputationReplayStatus.PASSED,
            ComputationReplayStatus.MISMATCH,
        }:
            try:
                _publish_immutable_bytes(
                    verdict_path,
                    canonical_json_bytes(replay_result),
                    confinement_root=self.run_root,
                )
            except _ArtifactFailure as failure:
                return _new_replay_result(
                    status=ComputationReplayStatus.INTEGRITY_FAILED,
                    assignment_id=identifier,
                    manifest_sha256=manifest.manifest_sha256,
                    replay_workspace_path=replay_relative,
                    isolation=isolation,
                    commands=command_evidence,
                    issues=[*issues, failure.issue],
                )
        elif terminal_status is ComputationReplayStatus.EXECUTION_FAILED:
            try:
                attempt_path = _next_replay_attempt_path(
                    self.run_root,
                    identifier,
                    manifest.manifest_sha256,
                )
                _publish_immutable_bytes(
                    attempt_path,
                    canonical_json_bytes(replay_result),
                    confinement_root=self.run_root,
                )
            except _ArtifactFailure as failure:
                return _new_replay_result(
                    status=ComputationReplayStatus.INTEGRITY_FAILED,
                    assignment_id=identifier,
                    manifest_sha256=manifest.manifest_sha256,
                    replay_workspace_path=replay_relative,
                    isolation=isolation,
                    commands=command_evidence,
                    issues=[*issues, failure.issue],
                )
        return replay_result

    def _manifest_path(self, assignment_id: str) -> Path:
        return self.run_root / "research" / "computations" / "manifests" / f"{assignment_id}.json"

    def _replay_verdict_path(self, assignment_id: str, manifest_sha256: str) -> Path:
        return (
            self.run_root
            / "research"
            / "computations"
            / "replays"
            / assignment_id
            / manifest_sha256
            / "verdict.json"
        )

    def _load_manifest(self, assignment_id: str) -> ComputationArtifactManifest:
        path = self._manifest_path(assignment_id)
        if not _lexists(path):
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.MANIFEST_MISSING,
                "no committed computation manifest exists for this assignment",
                path=_relative_to_run(self.run_root, path),
            )
        try:
            raw = _read_regular_file(path, maximum_bytes=16 * 1024 * 1024)
            manifest = ComputationArtifactManifest.model_validate_json(raw)
        except _ArtifactFailure:
            raise
        except (ValueError, json.JSONDecodeError) as exc:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
                f"committed computation manifest is invalid: {exc}",
                path=_relative_to_run(self.run_root, path),
                integrity=True,
            ) from exc
        if manifest.assignment_id != assignment_id:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
                "committed computation manifest has the wrong assignment identity",
                path=_relative_to_run(self.run_root, path),
                integrity=True,
            )
        expected_workspace = _relative_to_run(self.run_root, self.workspace_path(assignment_id))
        if manifest.workspace_path != expected_workspace:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
                "committed computation manifest has an unexpected workspace path",
                path=_relative_to_run(self.run_root, path),
                integrity=True,
            )
        self._verify_manifest_blobs(manifest)
        return manifest

    def _verify_manifest_blobs(self, manifest: ComputationArtifactManifest) -> None:
        for item in manifest.files:
            expected_blob = f"research/computations/blobs/sha256/{item.sha256}"
            if item.blob_path != expected_blob:
                raise _ArtifactFailure(
                    ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
                    "manifest blob path is not canonical for its digest",
                    path=item.blob_path,
                    integrity=True,
                )
            blob = self.run_root / Path(_strict_relative_path(item.blob_path, "blob path"))
            digest, size, _ = _hash_regular_file(blob)
            if digest != item.sha256 or size != item.size_bytes:
                raise _ArtifactFailure(
                    ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
                    "content-addressed blob does not match the committed manifest",
                    path=item.blob_path,
                    integrity=True,
                )

    def _store_scanned_file(
        self,
        item: _ScannedFile,
        *,
        roles: list[str],
    ) -> CollectedComputationFile:
        blob_root = self.run_root / "research" / "computations" / "blobs" / "sha256"
        _ensure_internal_directory(self.run_root, blob_root, mode=0o700)
        digest, size, incoming = _copy_regular_to_temporary(item, blob_root)
        target = blob_root / digest
        try:
            _publish_temporary_file(
                incoming,
                target,
                confinement_root=self.run_root,
            )
        finally:
            incoming.unlink(missing_ok=True)
        verified_digest, verified_size, _ = _hash_regular_file(target)
        if verified_digest != digest or verified_size != size:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
                "content-addressed blob failed post-commit verification",
                path=_relative_to_run(self.run_root, target),
                integrity=True,
            )
        return CollectedComputationFile(
            relative_path=item.relative_path,
            sha256=digest,
            size_bytes=size,
            blob_path=_relative_to_run(self.run_root, target),
            executable=bool(item.status.st_mode & 0o111),
            roles=roles,
        )

    def _materialize_replay_inputs(
        self,
        manifest: ComputationArtifactManifest,
        replay_workspace: Path,
    ) -> None:
        file_by_path = {item.relative_path: item for item in manifest.files}
        produced_paths = {item.primary_path for item in manifest.declarations}
        materialized: set[str] = set()
        for declaration in manifest.declarations:
            output = replay_workspace / Path(declaration.primary_path)
            _ensure_internal_directory(
                replay_workspace,
                output.parent,
                mode=0o700,
            )
            for input_path in declaration.input_paths:
                if input_path in produced_paths:
                    continue
                if input_path in materialized:
                    continue
                collected = file_by_path[input_path]
                blob = self.run_root / Path(collected.blob_path)
                destination = replay_workspace / Path(input_path)
                _materialize_file(
                    blob,
                    destination,
                    confinement_root=replay_workspace,
                    executable=collected.executable,
                )
                materialized.add(input_path)


def assignment_workspace_path(run_root: Path, assignment_id: str) -> Path:
    """Return the deterministic private scratch path without creating it."""

    return ComputationArtifactStore(run_root).workspace_path(assignment_id)


def prepare_assignment_workspace(run_root: Path, assignment_id: str) -> Path:
    """Create an assignment's deterministic private scratch directory with mode ``0700``."""

    return ComputationArtifactStore(run_root).prepare_workspace(assignment_id)


def collect_computation_artifacts(
    run_root: Path,
    assignment_id: str,
    declarations: Sequence[ScientificArtifactDeclaration],
    *,
    quotas: ComputationArtifactQuotas | None = None,
) -> ComputationArtifactCollectionResult:
    return ComputationArtifactStore(run_root, quotas=quotas).collect(
        assignment_id,
        declarations,
    )


async def replay_computation_artifacts(
    run_root: Path,
    assignment_id: str,
    backend: ExecutionBackend,
    *,
    isolation: ComputationReplayIsolation,
    limits: ComputationReplayLimits | None = None,
    quotas: ComputationArtifactQuotas | None = None,
) -> ComputationReplayResult:
    return await ComputationArtifactStore(run_root, quotas=quotas).replay(
        assignment_id,
        backend,
        isolation=isolation,
        limits=limits,
    )


def verify_persisted_computation_evidence(
    run_root: Path,
    assignment_id: str,
    evidence_path: Path,
) -> WorkerComputationEvidence:
    """Verify the graph-admission evidence against manifests, CAS blobs, and replay verdict.

    This is intentionally a read-only operation. It lets persistence consumers re-establish
    the application-owned trust chain instead of accepting a caller-supplied status/digest map.
    """

    store = ComputationArtifactStore(run_root)
    identifier = _validate_assignment_id(assignment_id)
    expected_path = store.run_root / "research" / "worker-computation" / f"{identifier}.json"
    supplied_path = Path(os.path.abspath(evidence_path))
    if supplied_path != expected_path:
        raise ValueError("computation evidence path is not canonical for its run and assignment")
    try:
        raw = _read_regular_file(supplied_path, maximum_bytes=32 * 1024 * 1024)
        evidence = WorkerComputationEvidence.model_validate_json(raw)
    except _ArtifactFailure as exc:
        raise ValueError(exc.issue.detail) from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"persisted computation evidence is invalid: {exc}") from exc
    if evidence.assignment_id != identifier:
        raise ValueError("persisted computation evidence belongs to another assignment")

    collection = evidence.collection
    replay = evidence.replay
    if collection is None:
        if replay is not None:
            raise ValueError("replay evidence has no collection transaction")
        return evidence
    if collection.assignment_id != identifier:
        raise ValueError("computation collection belongs to another assignment")
    expected_workspace_path = f"research/workspaces/{identifier}/scratch"
    if collection.workspace_path != expected_workspace_path:
        raise ValueError("computation collection workspace path is not canonical")
    if collection.trusted:
        if collection.manifest is None:
            raise ValueError("trusted computation collection has no immutable manifest")
        if collection.issues:
            raise ValueError("trusted computation collection cannot retain collection issues")
        expected_manifest_path = f"research/computations/manifests/{identifier}.json"
        if collection.manifest_path != expected_manifest_path:
            raise ValueError("computation collection manifest path is not canonical")
        try:
            committed_manifest = store.load_manifest(identifier)
        except _ArtifactFailure as exc:
            raise ValueError(exc.issue.detail) from exc
        if committed_manifest != collection.manifest:
            raise ValueError("computation evidence differs from the committed manifest")
    elif collection.manifest is not None or collection.manifest_path is not None:
        raise ValueError("unsuccessful computation collection cannot carry a trusted manifest")

    if replay is None:
        return evidence
    if not collection.trusted or collection.manifest is None:
        raise ValueError("computation replay is not backed by a trusted collection")
    if (
        replay.assignment_id != identifier
        or replay.manifest_sha256 != collection.manifest.manifest_sha256
    ):
        raise ValueError("computation replay identity does not match its collection")
    if replay.status in {ComputationReplayStatus.PASSED, ComputationReplayStatus.MISMATCH}:
        committed_replay = store.load_replay_result(identifier, collection.manifest.manifest_sha256)
        if committed_replay.model_copy(update={"reused": False}) != replay.model_copy(
            update={"reused": False}
        ):
            raise ValueError("computation evidence differs from the immutable replay verdict")
    if replay.status is ComputationReplayStatus.PASSED:
        if (
            not replay.isolation.filesystem_write_confined
            or not replay.isolation.network_disabled
            or replay.issues
            or len(replay.commands) != len(collection.manifest.declarations)
            or not replay.commands
            or any(not command.passed for command in replay.commands)
        ):
            raise ValueError("passing computation replay lacks complete isolated command evidence")
    return evidence


def _validate_assignment_id(value: str) -> str:
    normalized = value.strip()
    if not _ASSIGNMENT_ID.fullmatch(normalized):
        raise ValueError(
            "assignment ID must use 1-128 portable letters, digits, dot, underscore, or dash"
        )
    return normalized


def _validated_run_root(value: Path) -> Path:
    root = Path(os.path.abspath(value.expanduser()))
    if root == Path(root.anchor):
        raise ValueError("run root must not be a filesystem root")
    _reject_symlink_ancestors(root)
    try:
        status = os.lstat(root)
    except OSError as exc:
        raise ValueError(f"run root does not exist: {root}: {exc}") from exc
    if not stat.S_ISDIR(status.st_mode):
        raise ValueError(f"run root is not a directory: {root}")
    return root


def _strict_relative_path(value: str, label: str) -> str:
    if not value or "\x00" in value:
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.INVALID_PATH,
            f"{label} must be a nonempty relative path without NUL bytes",
            path=value or None,
        )
    normalized = value.replace("\\", "/")
    windows = PureWindowsPath(normalized)
    if normalized.startswith("/") or windows.is_absolute() or windows.drive:
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.INVALID_PATH,
            f"{label} must not be absolute or drive-qualified",
            path=normalized,
        )
    components = normalized.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.INVALID_PATH,
            f"{label} must not contain empty, current-directory, or traversal components",
            path=normalized,
        )
    if any(":" in component for component in components):
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.INVALID_PATH,
            f"{label} must use portable path components without colons",
            path=normalized,
        )
    return "/".join(components)


def _normalize_declarations(
    declarations: Sequence[ScientificArtifactDeclaration],
) -> list[_NormalizedDeclaration]:
    normalized: list[_NormalizedDeclaration] = []
    primary_indexes: dict[str, int] = {}
    for index, declaration in enumerate(declarations):
        if not isinstance(declaration, ScientificArtifactDeclaration):
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.INVALID_DECLARATION,
                "artifact declarations must be validated ScientificArtifactDeclaration objects",
            )
        primary = _strict_relative_path(declaration.path, "artifact path")
        if primary in primary_indexes:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.INVALID_DECLARATION,
                "two declarations cannot claim the same primary output path",
                path=primary,
            )
        primary_indexes[primary] = index
        inputs = tuple(
            dict.fromkeys(
                _strict_relative_path(path, "artifact input path")
                for path in declaration.input_paths
            )
        )
        stdout_path = (
            _strict_relative_path(declaration.stdout_path, "stdout reference path")
            if declaration.stdout_path is not None
            else None
        )
        stderr_path = (
            _strict_relative_path(declaration.stderr_path, "stderr reference path")
            if declaration.stderr_path is not None
            else None
        )
        if primary in inputs:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.INVALID_DECLARATION,
                "a primary replay output cannot also be its own input",
                path=primary,
            )
        capture_paths = [path for path in (stdout_path, stderr_path) if path is not None]
        if len(set(capture_paths)) != len(capture_paths):
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.INVALID_DECLARATION,
                "stdout and stderr reference paths must be distinct",
                path=capture_paths[0],
            )
        if any(path == primary or path in inputs for path in capture_paths):
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.INVALID_DECLARATION,
                "capture references must be distinct from replay inputs and primary output",
                path=primary,
            )
        argv = _validate_fixed_argv(declaration.command_line)
        normalized.append(
            _NormalizedDeclaration(
                original=declaration,
                primary_path=primary,
                argv=argv,
                input_paths=inputs,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        )

    for index, normalized_declaration in enumerate(normalized):
        for input_path in normalized_declaration.input_paths:
            producer = primary_indexes.get(input_path)
            if producer is not None and producer >= index:
                raise _ArtifactFailure(
                    ComputationArtifactIssueCode.INVALID_DECLARATION,
                    "a replay input produced by another declaration must be produced earlier",
                    path=input_path,
                )
    return normalized


def _validate_fixed_argv(values: Sequence[str]) -> tuple[str, ...]:
    if not values or not values[0].strip():
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.UNSAFE_COMMAND,
            "computation replay requires a nonempty fixed argument array",
        )
    argv = tuple(values)
    for index, argument in enumerate(argv):
        if "\x00" in argument:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.UNSAFE_COMMAND,
                "replay arguments must not contain NUL bytes",
            )
        candidates = [argument]
        if "=" in argument:
            candidates.append(argument.split("=", 1)[1])
        for candidate in candidates:
            if _URI_SCHEME.match(candidate):
                raise _ArtifactFailure(
                    ComputationArtifactIssueCode.UNSAFE_COMMAND,
                    "replay arguments must not contain network resource URLs",
                )
            if "/" not in candidate and "\\" not in candidate:
                continue
            normalized = candidate.replace("\\", "/")
            windows = PureWindowsPath(normalized)
            if normalized.startswith("/") or windows.is_absolute() or windows.drive:
                raise _ArtifactFailure(
                    ComputationArtifactIssueCode.UNSAFE_COMMAND,
                    "replay arguments must not name absolute filesystem paths",
                )
            if ".." in normalized.split("/"):
                raise _ArtifactFailure(
                    ComputationArtifactIssueCode.UNSAFE_COMMAND,
                    "replay arguments must not traverse outside the replay workspace",
                )
        if index == 0 and _URI_SCHEME.match(argument):
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.UNSAFE_COMMAND,
                "replay executable must be a local tool",
            )
    return argv


def _declared_file_roles(
    declarations: Sequence[_NormalizedDeclaration],
) -> dict[str, set[str]]:
    roles: dict[str, set[str]] = {}

    def add(path: str, role: str) -> None:
        roles.setdefault(path, set()).add(role)

    for index, declaration in enumerate(declarations):
        add(declaration.primary_path, f"output:{index}")
        for path in declaration.input_paths:
            add(path, f"input:{index}")
        if declaration.stdout_path is not None:
            add(declaration.stdout_path, f"stdout:{index}")
        if declaration.stderr_path is not None:
            add(declaration.stderr_path, f"stderr:{index}")
    return roles


def _build_collected_declaration(
    declaration: _NormalizedDeclaration,
    files: dict[str, CollectedComputationFile],
) -> CollectedComputationDeclaration:
    stdout_sha256 = hashlib.sha256(b"").hexdigest()
    if declaration.stdout_path is not None:
        stdout_sha256 = files[declaration.stdout_path].sha256
    if declaration.original.expected_output is not None:
        literal_sha256 = hashlib.sha256(
            declaration.original.expected_output.encode("utf-8")
        ).hexdigest()
        if declaration.stdout_path is not None and literal_sha256 != stdout_sha256:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.INVALID_DECLARATION,
                "expected_output text disagrees with the declared stdout reference",
                path=declaration.stdout_path,
            )
        stdout_sha256 = literal_sha256
    stderr_sha256 = hashlib.sha256(b"").hexdigest()
    if declaration.stderr_path is not None:
        stderr_sha256 = files[declaration.stderr_path].sha256
    return CollectedComputationDeclaration(
        declaration_sha256=hashlib.sha256(canonical_json_bytes(declaration.original)).hexdigest(),
        primary_path=declaration.primary_path,
        purpose=declaration.original.purpose,
        supporting_result_keys=declaration.original.supporting_result_keys,
        argv=list(declaration.argv),
        input_paths=list(declaration.input_paths),
        stdout_reference_path=declaration.stdout_path,
        stderr_reference_path=declaration.stderr_path,
        expected_output=declaration.original.expected_output,
        replay_recipe=declaration.original.replay_recipe,
        tool_versions=declaration.original.tool_versions,
        primary_sha256=files[declaration.primary_path].sha256,
        input_sha256={path: files[path].sha256 for path in declaration.input_paths},
        expected_stdout_sha256=stdout_sha256,
        expected_stderr_sha256=stderr_sha256,
    )


def _validate_manifest_structure(manifest: ComputationArtifactManifest) -> None:
    try:
        assignment_id = _validate_assignment_id(manifest.assignment_id)
    except ValueError as exc:
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
            str(exc),
            integrity=True,
        ) from exc
    expected_workspace = f"research/workspaces/{assignment_id}/scratch"
    if manifest.workspace_path != expected_workspace:
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
            "manifest workspace path does not match its assignment identity",
            path=manifest.workspace_path,
            integrity=True,
        )
    if not manifest.declarations or not manifest.files:
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
            "computation manifests require declarations and retained files",
            integrity=True,
        )

    file_by_path: dict[str, CollectedComputationFile] = {}
    for item in manifest.files:
        relative = _strict_relative_path(item.relative_path, "manifest file path")
        blob = _strict_relative_path(item.blob_path, "manifest blob path")
        if relative != item.relative_path:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
                "manifest file path is not canonical",
                path=item.relative_path,
                integrity=True,
            )
        if relative in file_by_path:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
                "manifest contains duplicate retained file paths",
                path=relative,
                integrity=True,
            )
        if blob != f"research/computations/blobs/sha256/{item.sha256}":
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
                "manifest blob path is not canonical for its digest",
                path=blob,
                integrity=True,
            )
        file_by_path[relative] = item

    ignored_paths = [
        _strict_relative_path(path, "ignored workspace path") for path in manifest.ignored_paths
    ]
    if len(set(ignored_paths)) != len(ignored_paths):
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
            "manifest contains duplicate ignored paths",
            integrity=True,
        )
    overlap = set(ignored_paths) & set(file_by_path)
    if overlap:
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
            "manifest path cannot be both retained and ignored",
            path=sorted(overlap)[0],
            integrity=True,
        )
    if manifest.retained_file_count != len(manifest.files):
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
            "manifest retained file count is inconsistent",
            integrity=True,
        )
    if manifest.retained_total_bytes != sum(item.size_bytes for item in manifest.files):
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
            "manifest retained byte count is inconsistent",
            integrity=True,
        )
    if manifest.workspace_file_count != len(manifest.files) + len(ignored_paths):
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
            "manifest workspace file count is inconsistent",
            integrity=True,
        )
    if manifest.workspace_total_bytes < manifest.retained_total_bytes:
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
            "manifest workspace byte count is smaller than retained content",
            integrity=True,
        )

    primary_paths: set[str] = set()
    for declaration in manifest.declarations:
        primary = _strict_relative_path(declaration.primary_path, "primary output path")
        if primary in primary_paths:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
                "manifest contains duplicate primary output paths",
                path=primary,
                integrity=True,
            )
        primary_paths.add(primary)
        _validate_fixed_argv(declaration.argv)
        inputs = [
            _strict_relative_path(path, "replay input path") for path in declaration.input_paths
        ]
        if len(set(inputs)) != len(inputs) or set(inputs) != set(declaration.input_sha256):
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
                "manifest replay inputs and their hashes are inconsistent",
                path=primary,
                integrity=True,
            )
        primary_file = file_by_path.get(primary)
        if primary_file is None or primary_file.sha256 != declaration.primary_sha256:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
                "manifest primary output is missing or has an inconsistent hash",
                path=primary,
                integrity=True,
            )
        for input_path, digest in declaration.input_sha256.items():
            input_file = file_by_path.get(input_path)
            if input_file is None or input_file.sha256 != digest:
                raise _ArtifactFailure(
                    ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
                    "manifest replay input is missing or has an inconsistent hash",
                    path=input_path,
                    integrity=True,
                )
        stdout_path = declaration.stdout_reference_path
        stderr_path = declaration.stderr_reference_path
        if stdout_path is not None:
            stdout_path = _strict_relative_path(stdout_path, "stdout reference path")
        if stderr_path is not None:
            stderr_path = _strict_relative_path(stderr_path, "stderr reference path")
        expected_stdout = hashlib.sha256(b"").hexdigest()
        if stdout_path is not None:
            stdout_file = file_by_path.get(stdout_path)
            if stdout_file is None:
                raise _ArtifactFailure(
                    ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
                    "manifest stdout reference file is missing",
                    path=stdout_path,
                    integrity=True,
                )
            expected_stdout = stdout_file.sha256
        if declaration.expected_output is not None:
            literal_stdout = hashlib.sha256(declaration.expected_output.encode("utf-8")).hexdigest()
            if stdout_path is not None and literal_stdout != expected_stdout:
                raise _ArtifactFailure(
                    ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
                    "manifest literal stdout disagrees with its reference file",
                    path=stdout_path,
                    integrity=True,
                )
            expected_stdout = literal_stdout
        expected_stderr = hashlib.sha256(b"").hexdigest()
        if stderr_path is not None:
            stderr_file = file_by_path.get(stderr_path)
            if stderr_file is None:
                raise _ArtifactFailure(
                    ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
                    "manifest stderr reference file is missing",
                    path=stderr_path,
                    integrity=True,
                )
            expected_stderr = stderr_file.sha256
        if declaration.expected_stdout_sha256 != expected_stdout:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
                "manifest expected stdout hash is inconsistent",
                path=primary,
                integrity=True,
            )
        if declaration.expected_stderr_sha256 != expected_stderr:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
                "manifest expected stderr hash is inconsistent",
                path=primary,
                integrity=True,
            )
        if declaration.expected_exit_code != 0:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
                "computation evidence must require a successful exit status",
                path=primary,
                integrity=True,
            )
        reconstructed = ScientificArtifactDeclaration(
            path=primary,
            purpose=declaration.purpose,
            supporting_result_keys=declaration.supporting_result_keys,
            command_line=declaration.argv,
            input_paths=inputs,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            expected_output=declaration.expected_output,
            replay_recipe=declaration.replay_recipe,
            tool_versions=declaration.tool_versions,
        )
        reconstructed_sha256 = hashlib.sha256(canonical_json_bytes(reconstructed)).hexdigest()
        if reconstructed_sha256 != declaration.declaration_sha256:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
                "manifest declaration identity does not match its stored fields",
                path=primary,
                integrity=True,
            )


def _scan_workspace(
    workspace: Path,
    quotas: ComputationArtifactQuotas,
) -> list[_ScannedFile]:
    files: list[_ScannedFile] = []
    total_bytes = 0

    def visit(directory: Path) -> None:
        nonlocal total_bytes
        _require_directory(directory, workspace, label="workspace directory")
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.INVALID_PATH,
                f"cannot scan private workspace: {exc}",
                path=_relative_lexical(workspace, directory),
            ) from exc
        for entry in entries:
            child = directory / entry.name
            relative = _relative_lexical(workspace, child)
            try:
                child_status = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise _ArtifactFailure(
                    ComputationArtifactIssueCode.FILE_CHANGED,
                    f"cannot inspect workspace entry: {exc}",
                    path=relative,
                ) from exc
            if stat.S_ISLNK(child_status.st_mode):
                raise _ArtifactFailure(
                    ComputationArtifactIssueCode.SYMLINK,
                    "symlinks are forbidden in computation workspaces",
                    path=relative,
                )
            if stat.S_ISDIR(child_status.st_mode):
                visit(child)
                continue
            if not stat.S_ISREG(child_status.st_mode):
                raise _ArtifactFailure(
                    ComputationArtifactIssueCode.SPECIAL_FILE,
                    "only regular files and directories are allowed in computation workspaces",
                    path=relative,
                )
            if child_status.st_nlink != 1:
                raise _ArtifactFailure(
                    ComputationArtifactIssueCode.HARDLINK,
                    "multiply linked files are forbidden in computation workspaces",
                    path=relative,
                )
            if child_status.st_size > quotas.maximum_file_bytes:
                raise _ArtifactFailure(
                    ComputationArtifactIssueCode.QUOTA_EXCEEDED,
                    "computation file exceeds the configured per-file byte quota",
                    path=relative,
                )
            files.append(
                _ScannedFile(
                    relative_path=relative,
                    absolute_path=child,
                    status=child_status,
                )
            )
            total_bytes += child_status.st_size
            if len(files) > quotas.maximum_files:
                raise _ArtifactFailure(
                    ComputationArtifactIssueCode.QUOTA_EXCEEDED,
                    "private workspace exceeds the configured file-count quota",
                )
            if total_bytes > quotas.maximum_total_bytes:
                raise _ArtifactFailure(
                    ComputationArtifactIssueCode.QUOTA_EXCEEDED,
                    "private workspace exceeds the configured total-byte quota",
                )

    visit(workspace)
    return files


def _copy_regular_to_temporary(
    item: _ScannedFile,
    destination_directory: Path,
) -> tuple[str, int, Path]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        source_descriptor = os.open(item.absolute_path, flags)
    except OSError as exc:
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.FILE_CHANGED,
            f"cannot safely open declared file: {exc}",
            path=item.relative_path,
        ) from exc
    temporary_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".incoming-",
        dir=destination_directory,
    )
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    size = 0
    try:
        opened = os.fstat(source_descriptor)
        if not _same_file_status(item.status, opened) or opened.st_nlink != 1:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.FILE_CHANGED,
                "declared file changed between workspace scan and collection",
                path=item.relative_path,
            )
        with os.fdopen(temporary_descriptor, "wb") as destination:
            while True:
                chunk = os.read(source_descriptor, _READ_CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        after = os.fstat(source_descriptor)
        if not _same_file_status(opened, after) or size != opened.st_size:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.FILE_CHANGED,
                "declared file changed while MATEK hashed it",
                path=item.relative_path,
            )
        os.chmod(temporary, 0o400)
    except BaseException:
        try:
            os.close(temporary_descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
    finally:
        os.close(source_descriptor)
    return digest.hexdigest(), size, temporary


def _publish_temporary_file(
    temporary: Path,
    target: Path,
    *,
    confinement_root: Path,
) -> None:
    _assert_lexically_confined(confinement_root, target)
    _ensure_internal_directory(confinement_root, target.parent, mode=0o700)
    try:
        os.link(temporary, target, follow_symlinks=False)
    except FileExistsError:
        incoming_digest, incoming_size, _ = _hash_regular_file(temporary)
        existing_digest, existing_size, _ = _hash_regular_file(target)
        if incoming_digest != existing_digest or incoming_size != existing_size:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
                "immutable content-addressed target already exists with different bytes",
                path=_relative_to_run(confinement_root, target),
                integrity=True,
            ) from None
    except OSError as exc:
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
            f"cannot publish immutable content-addressed file: {exc}",
            path=_relative_to_run(confinement_root, target),
            integrity=True,
        ) from exc
    temporary.unlink(missing_ok=True)
    _fsync_directory(target.parent)


def _publish_immutable_bytes(
    target: Path,
    contents: bytes,
    *,
    confinement_root: Path,
) -> None:
    _assert_lexically_confined(confinement_root, target)
    _ensure_internal_directory(confinement_root, target.parent, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".incoming-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o400)
        _publish_temporary_file(temporary, target, confinement_root=confinement_root)
        existing = _read_regular_file(target, maximum_bytes=max(len(contents), 1) + 1)
        if existing != contents:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.MANIFEST_CONFLICT,
                "immutable artifact already exists with different content",
                path=_relative_to_run(confinement_root, target),
                integrity=True,
            )
    finally:
        temporary.unlink(missing_ok=True)


def _hash_regular_file(path: Path) -> tuple[str, int, os.stat_result]:
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
            f"cannot inspect immutable regular file: {exc}",
            path=str(path),
            integrity=True,
        ) from exc
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        code = (
            ComputationArtifactIssueCode.HARDLINK
            if stat.S_ISREG(before.st_mode)
            else ComputationArtifactIssueCode.SPECIAL_FILE
        )
        raise _ArtifactFailure(
            code,
            "immutable artifact is not a singly linked regular file",
            path=str(path),
            integrity=True,
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
            f"cannot safely open immutable regular file: {exc}",
            path=str(path),
            integrity=True,
        ) from exc
    digest = hashlib.sha256()
    size = 0
    try:
        opened = os.fstat(descriptor)
        if not _same_file_status(before, opened):
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.FILE_CHANGED,
                "file changed while it was opened for hashing",
                path=str(path),
                integrity=True,
            )
        while True:
            chunk = os.read(descriptor, _READ_CHUNK_SIZE)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        if not _same_file_status(opened, after) or size != opened.st_size:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.FILE_CHANGED,
                "file changed while it was hashed",
                path=str(path),
                integrity=True,
            )
        return digest.hexdigest(), size, after
    finally:
        os.close(descriptor)


def _read_regular_file(path: Path, *, maximum_bytes: int) -> bytes:
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
            f"cannot inspect immutable regular file: {exc}",
            path=str(path),
            integrity=True,
        ) from exc
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
            "immutable artifact is not a singly linked regular file",
            path=str(path),
            integrity=True,
        )
    if before.st_size > maximum_bytes:
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
            "immutable metadata file exceeds its maximum allowed size",
            path=str(path),
            integrity=True,
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
            f"cannot safely open immutable regular file: {exc}",
            path=str(path),
            integrity=True,
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if not _same_file_status(before, opened):
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.FILE_CHANGED,
                "immutable file changed while it was opened",
                path=str(path),
                integrity=True,
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            remaining = maximum_bytes + 1 - total
            chunk = os.read(descriptor, min(_READ_CHUNK_SIZE, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum_bytes:
                raise _ArtifactFailure(
                    ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
                    "immutable metadata file exceeds its maximum allowed size",
                    path=str(path),
                    integrity=True,
                )
        after = os.fstat(descriptor)
        if not _same_file_status(opened, after) or total != opened.st_size:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.FILE_CHANGED,
                "immutable file changed while it was read",
                path=str(path),
                integrity=True,
            )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _materialize_file(
    blob: Path,
    destination: Path,
    *,
    confinement_root: Path,
    executable: bool,
) -> None:
    _assert_lexically_confined(confinement_root, destination)
    _ensure_internal_directory(confinement_root, destination.parent, mode=0o700)
    contents = _read_regular_file(blob, maximum_bytes=2**63 - 1)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(destination, flags, 0o500 if executable else 0o400)
    except OSError as exc:
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.REPLAY_WORKSPACE_INVALID,
            f"cannot materialize replay input: {exc}",
            path=_relative_lexical(confinement_root, destination),
            integrity=True,
        ) from exc
    try:
        remaining = memoryview(contents)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:  # pragma: no cover - defensive filesystem invariant
                raise OSError("short write while materializing replay input")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _command_evidence(
    declaration: CollectedComputationDeclaration,
    result: CommandResult,
    replay_workspace: Path,
    *,
    maximum_output_bytes: int,
) -> ComputationReplayCommandEvidence:
    stdout_bytes = result.stdout.encode("utf-8")
    stderr_bytes = result.stderr.encode("utf-8")
    stdout_sha256 = hashlib.sha256(stdout_bytes).hexdigest()
    stderr_sha256 = hashlib.sha256(stderr_bytes).hexdigest()
    captured_stdout, stdout_over_limit = _bounded_text(result.stdout, maximum_output_bytes)
    captured_stderr, stderr_over_limit = _bounded_text(result.stderr, maximum_output_bytes)
    output = replay_workspace / Path(declaration.primary_path)
    actual_output_sha256: str | None = None
    if _lexists(output):
        try:
            actual_output_sha256, _, _ = _hash_regular_file(output)
        except _ArtifactFailure:
            actual_output_sha256 = None
    passed = (
        result.exit_code == declaration.expected_exit_code
        and not result.timed_out
        and not result.stdout_truncated
        and not stdout_over_limit
        and not result.stderr_truncated
        and not stderr_over_limit
        and stdout_sha256 == declaration.expected_stdout_sha256
        and stderr_sha256 == declaration.expected_stderr_sha256
        and actual_output_sha256 == declaration.primary_sha256
    )
    return ComputationReplayCommandEvidence(
        primary_path=declaration.primary_path,
        argv=list(declaration.argv),
        expected_exit_code=declaration.expected_exit_code,
        actual_exit_code=result.exit_code,
        expected_stdout_sha256=declaration.expected_stdout_sha256,
        actual_stdout_sha256=stdout_sha256,
        expected_stderr_sha256=declaration.expected_stderr_sha256,
        actual_stderr_sha256=stderr_sha256,
        expected_output_sha256=declaration.primary_sha256,
        actual_output_sha256=actual_output_sha256,
        stdout=redact_text(captured_stdout),
        stderr=redact_text(captured_stderr),
        duration_seconds=max(result.duration_seconds, 0.0),
        stdout_truncated=result.stdout_truncated or stdout_over_limit,
        stderr_truncated=result.stderr_truncated or stderr_over_limit,
        timed_out=result.timed_out,
        passed=passed,
    )


def _command_mismatch_issues(
    declaration: CollectedComputationDeclaration,
    evidence: ComputationReplayCommandEvidence,
) -> list[ComputationArtifactIssue]:
    issues: list[ComputationArtifactIssue] = []
    if evidence.timed_out:
        issues.append(
            ComputationArtifactIssue(
                code=ComputationArtifactIssueCode.REPLAY_TIMEOUT,
                detail="replay command timed out",
                path=declaration.primary_path,
            )
        )
    if evidence.stdout_truncated or evidence.stderr_truncated:
        issues.append(
            ComputationArtifactIssue(
                code=ComputationArtifactIssueCode.REPLAY_EXECUTION_FAILED,
                detail="replay output exceeded the configured capture bound",
                path=declaration.primary_path,
            )
        )
    if evidence.actual_exit_code != evidence.expected_exit_code:
        issues.append(
            ComputationArtifactIssue(
                code=ComputationArtifactIssueCode.EXIT_STATUS_MISMATCH,
                detail=(
                    f"expected exit status {evidence.expected_exit_code}, got "
                    f"{evidence.actual_exit_code}"
                ),
                path=declaration.primary_path,
            )
        )
    if evidence.actual_stdout_sha256 != evidence.expected_stdout_sha256:
        issues.append(
            ComputationArtifactIssue(
                code=ComputationArtifactIssueCode.STDOUT_MISMATCH,
                detail="replayed stdout does not match the application-owned reference hash",
                path=declaration.primary_path,
            )
        )
    if evidence.actual_stderr_sha256 != evidence.expected_stderr_sha256:
        issues.append(
            ComputationArtifactIssue(
                code=ComputationArtifactIssueCode.STDERR_MISMATCH,
                detail="replayed stderr does not match the application-owned reference hash",
                path=declaration.primary_path,
            )
        )
    if evidence.actual_output_sha256 is None:
        issues.append(
            ComputationArtifactIssue(
                code=ComputationArtifactIssueCode.OUTPUT_MISSING,
                detail="replay did not create a singly linked regular primary output",
                path=declaration.primary_path,
            )
        )
    elif evidence.actual_output_sha256 != evidence.expected_output_sha256:
        issues.append(
            ComputationArtifactIssue(
                code=ComputationArtifactIssueCode.OUTPUT_MISMATCH,
                detail="replayed primary output does not match its collected SHA-256",
                path=declaration.primary_path,
            )
        )
    return issues


def _bounded_text(value: str, maximum_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value, False
    return encoded[:maximum_bytes].decode("utf-8", errors="replace"), True


def _validate_replay_workspace(
    manifest: ComputationArtifactManifest,
    replay_workspace: Path,
    quotas: ComputationArtifactQuotas,
) -> None:
    scanned = _scan_workspace(replay_workspace, quotas)
    expected_hashes: dict[str, str] = {}
    for declaration in manifest.declarations:
        for path, digest in declaration.input_sha256.items():
            prior = expected_hashes.get(path)
            if prior is not None and prior != digest:
                raise _ArtifactFailure(
                    ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
                    "manifest gives one replay input two different hashes",
                    path=path,
                    integrity=True,
                )
            expected_hashes[path] = digest
        prior_output = expected_hashes.get(declaration.primary_path)
        if prior_output is not None and prior_output != declaration.primary_sha256:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
                "produced replay input hash disagrees with its producer output hash",
                path=declaration.primary_path,
                integrity=True,
            )
        expected_hashes[declaration.primary_path] = declaration.primary_sha256
    expected = set(expected_hashes)
    actual = {item.relative_path for item in scanned}
    extras = sorted(actual - expected)
    missing = sorted(expected - actual)
    if extras:
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.REPLAY_WORKSPACE_INVALID,
            "replay created undeclared files",
            path=extras[0],
        )
    if missing:
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.OUTPUT_MISSING,
            "replay workspace is missing a declared input or output",
            path=missing[0],
        )
    for item in scanned:
        expected_digest = expected_hashes[item.relative_path]
        actual_digest, _, _ = _hash_regular_file(item.absolute_path)
        if actual_digest != expected_digest:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.OUTPUT_MISMATCH,
                "replay changed an input or produced a file with the wrong hash",
                path=item.relative_path,
            )


def _new_replay_result(
    *,
    status: ComputationReplayStatus,
    assignment_id: str,
    manifest_sha256: str | None,
    replay_workspace_path: str | None,
    isolation: ComputationReplayIsolation,
    commands: list[ComputationReplayCommandEvidence] | None = None,
    issues: list[ComputationArtifactIssue] | None = None,
) -> ComputationReplayResult:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "status": status.value,
        "assignment_id": assignment_id,
        "manifest_sha256": manifest_sha256,
        "replay_workspace_path": replay_workspace_path,
        "isolation": isolation.model_dump(mode="json"),
        "commands": [item.model_dump(mode="json") for item in commands or []],
        "issues": [item.model_dump(mode="json") for item in issues or []],
    }
    payload["record_sha256"] = _sha256_canonical(payload)
    payload["reused"] = False
    return ComputationReplayResult.model_validate(payload)


def _load_replay_result(path: Path, run_root: Path) -> ComputationReplayResult:
    try:
        raw = _read_regular_file(path, maximum_bytes=32 * 1024 * 1024)
        return ComputationReplayResult.model_validate_json(raw)
    except _ArtifactFailure:
        raise
    except (ValueError, json.JSONDecodeError) as exc:
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.IMMUTABLE_ARTIFACT_CORRUPT,
            f"cached replay verdict is invalid: {exc}",
            path=_relative_to_run(run_root, path),
            integrity=True,
        ) from exc


def _collection_failure(
    assignment_id: str,
    workspace_path: str,
    failure: _ArtifactFailure,
) -> ComputationArtifactCollectionResult:
    status = (
        ArtifactCollectionStatus.INTEGRITY_FAILED
        if failure.integrity
        else ArtifactCollectionStatus.REJECTED
    )
    return ComputationArtifactCollectionResult(
        status=status,
        assignment_id=assignment_id,
        workspace_path=workspace_path,
        issues=[failure.issue],
    )


def _next_replay_attempt_path(
    run_root: Path,
    assignment_id: str,
    manifest_sha256: str,
) -> Path:
    attempts = (
        run_root
        / "research"
        / "computations"
        / "replays"
        / assignment_id
        / manifest_sha256
        / "attempts"
    )
    _ensure_internal_directory(run_root, attempts, mode=0o700)
    index = 1
    while True:
        candidate = attempts / f"{index:08d}.json"
        if not _lexists(candidate):
            return candidate
        index += 1


def _reset_replay_workspace(run_root: Path, replay_workspace: Path) -> None:
    _assert_lexically_confined(run_root, replay_workspace)
    if _lexists(replay_workspace):
        _remove_tree(replay_workspace)
    _ensure_internal_directory(run_root, replay_workspace, mode=0o700)
    os.chmod(replay_workspace, 0o700)


def _remove_tree(path: Path) -> None:
    status = os.lstat(path)
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.REPLAY_WORKSPACE_INVALID,
            "refusing to clear a replay workspace that is not a real directory",
            path=str(path),
            integrity=True,
        )
    with os.scandir(path) as entries:
        children = sorted(entries, key=lambda item: item.name)
    for entry in children:
        child = path / entry.name
        child_status = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(child_status.st_mode):
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.SYMLINK,
                "refusing to clear a replay workspace containing a symlink",
                path=str(child),
                integrity=True,
            )
        if stat.S_ISDIR(child_status.st_mode):
            _remove_tree(child)
        elif stat.S_ISREG(child_status.st_mode):
            child.unlink()
        else:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.SPECIAL_FILE,
                "refusing to clear a replay workspace containing a special file",
                path=str(child),
                integrity=True,
            )
    path.rmdir()


def _ensure_internal_directory(root: Path, directory: Path, *, mode: int) -> Path:
    _assert_lexically_confined(root, directory, allow_root=True)
    relative = directory.relative_to(root)
    current = root
    for component in relative.parts:
        current /= component
        try:
            os.mkdir(current, mode)
        except FileExistsError:
            pass
        except OSError as exc:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.INVALID_PATH,
                f"cannot create private artifact directory: {exc}",
                path=_relative_lexical(root, current),
            ) from exc
        try:
            status = os.lstat(current)
        except OSError as exc:
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.INVALID_PATH,
                f"cannot inspect private artifact directory: {exc}",
                path=_relative_lexical(root, current),
            ) from exc
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise _ArtifactFailure(
                ComputationArtifactIssueCode.SYMLINK
                if stat.S_ISLNK(status.st_mode)
                else ComputationArtifactIssueCode.SPECIAL_FILE,
                "artifact directory path contains a symlink or non-directory",
                path=_relative_lexical(root, current),
                integrity=True,
            )
    return directory


def _require_directory(directory: Path, root: Path, *, label: str) -> None:
    _assert_lexically_confined(root, directory, allow_root=True)
    try:
        status = os.lstat(directory)
    except OSError as exc:
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.INVALID_PATH,
            f"{label} is unavailable: {exc}",
            path=_relative_lexical(root, directory),
        ) from exc
    if stat.S_ISLNK(status.st_mode):
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.SYMLINK,
            f"{label} must not be a symlink",
            path=_relative_lexical(root, directory),
        )
    if not stat.S_ISDIR(status.st_mode):
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.SPECIAL_FILE,
            f"{label} must be a real directory",
            path=_relative_lexical(root, directory),
        )


def _reject_symlink_ancestors(path: Path) -> None:
    for candidate in reversed((path, *path.parents)):
        try:
            status = os.lstat(candidate)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(f"cannot inspect path component {candidate}: {exc}") from exc
        if stat.S_ISLNK(status.st_mode):
            raise ValueError(f"path must not contain symlinks: {candidate}")
        if candidate != path and not stat.S_ISDIR(status.st_mode):
            raise ValueError(f"path ancestor is not a directory: {candidate}")


def _assert_lexically_confined(
    root: Path,
    candidate: Path,
    *,
    allow_root: bool = False,
) -> None:
    root_absolute = Path(os.path.abspath(root))
    candidate_absolute = Path(os.path.abspath(candidate))
    if candidate_absolute == root_absolute:
        if allow_root:
            return
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.INVALID_PATH,
            "operation may not target the confinement root itself",
        )
    try:
        candidate_absolute.relative_to(root_absolute)
    except ValueError as exc:
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.INVALID_PATH,
            "path escapes the computation artifact root",
            path=str(candidate),
        ) from exc


def _relative_to_run(run_root: Path, path: Path) -> str:
    _assert_lexically_confined(run_root, path, allow_root=True)
    return path.relative_to(run_root).as_posix()


def _relative_lexical(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _same_file_status(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_size,
        left.st_mtime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_size,
        right.st_mtime_ns,
    )


def _lexists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise _ArtifactFailure(
            ComputationArtifactIssueCode.INVALID_PATH,
            f"cannot inspect artifact path: {exc}",
            path=str(path),
            integrity=True,
        ) from exc
    return True


def _sha256_canonical(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _model_content_sha256(model: BaseModel, *, excluded: set[str]) -> str:
    return _sha256_canonical(model.model_dump(mode="json", exclude=excluded))


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


__all__ = [
    "ArtifactCollectionStatus",
    "CollectedComputationDeclaration",
    "CollectedComputationFile",
    "ComputationArtifactCollectionResult",
    "ComputationArtifactIssue",
    "ComputationArtifactIssueCode",
    "ComputationArtifactManifest",
    "ComputationArtifactQuotas",
    "ComputationArtifactStore",
    "ComputationReplayCommandEvidence",
    "ComputationReplayIsolation",
    "ComputationReplayLimits",
    "ComputationReplayResult",
    "ComputationReplayStatus",
    "WorkerComputationEvidence",
    "assignment_workspace_path",
    "collect_computation_artifacts",
    "prepare_assignment_workspace",
    "replay_computation_artifacts",
    "verify_persisted_computation_evidence",
]
