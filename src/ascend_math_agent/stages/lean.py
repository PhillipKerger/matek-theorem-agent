from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import stat
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..codex_client import CodexClient, CodexRequest, CodexResult
from ..config import ModelSettings
from ..execution.base import CommandRequest, CommandResult, ExecutionBackend
from ..openai_client import ModelClient, ModelRequest
from ..redaction import SecretRedactor
from ..verification import (
    canonical_theorem_hash,
    check_axiom_allowlist,
    extract_theorem_statements,
    scan_lean_tree,
    verify_build,
)
from .common import (
    ArtifactManifest,
    CallManifest,
    StageGateError,
    StageValidationError,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    build_artifact_manifest,
    ensure_stage_directory,
    project_resource,
    read_regular_bytes,
    read_regular_text,
    sha256_json,
    sha256_text,
)
from .manuscript import ManuscriptResult
from .research import ResearchOutcome, ResearchResult


class LeanFeasibilityClass(StrEnum):
    FULL = "full_formalization_recommended"
    MAIN_THEOREM = "main_theorem_formalization_recommended"
    VERIFICATION_PLAN = "verification_plan_only"
    NOT_ATTAINABLE = "not_reasonably_attainable"


class AlignmentStatus(StrEnum):
    ALIGNED = "aligned"
    REVISION_REQUIRED = "revision_required"
    REJECTED = "rejected"


class LeanOutcome(StrEnum):
    INFEASIBLE = "LEAN_INFEASIBLE"
    STATEMENT_ONLY = "LEAN_STATEMENT_ONLY"
    PARTIAL = "LEAN_PARTIAL"
    FAILED = "LEAN_FAILED"
    VERIFIED_WITH_APPROVED_AXIOMS = "LEAN_VERIFIED_WITH_APPROVED_AXIOMS"
    VERIFIED = "LEAN_VERIFIED"


class LeanFeasibilityAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classification: LeanFeasibilityClass
    explanation: str
    expected_mathlib_dependencies: list[str]
    difficult_components: list[str]
    computational_certificates: list[str]
    paper_proof_mismatches: list[str]


class LeanClaimMapEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_contract_key: str
    lean_expression: str


class LeanStatementDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_lean: str
    statement_explanation: str
    claim_map: list[LeanClaimMapEntry]
    theorem_name: str | None = None

    @field_validator("claim_map", mode="before")
    @classmethod
    def accept_legacy_claim_map(cls, value: object) -> object:
        if isinstance(value, dict):
            return [
                {
                    "claim_contract_key": str(key),
                    "lean_expression": (
                        item
                        if isinstance(item, str)
                        else json.dumps(item, ensure_ascii=False, sort_keys=True)
                    ),
                }
                for key, item in value.items()
            ]
        return value

    @field_validator("challenge_lean", "statement_explanation")
    @classmethod
    def statement_output_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("statement-generation output must not be empty")
        return value


MANDATORY_ALIGNMENT_FIELDS = (
    "quantifiers",
    "domains",
    "finiteness",
    "exceptions",
    "equality",
    "typeclass_assumptions",
    "classical_axioms",
)


class AlignmentCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    passed: bool
    explanation: str

    @field_validator("field", "explanation")
    @classmethod
    def check_text_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("alignment check fields and explanations must not be empty")
        return value.strip()


class ClaimAlignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: AlignmentStatus
    mathematical_back_translation: str
    checks: list[AlignmentCheck]
    required_edits: list[str]

    @field_validator("mathematical_back_translation")
    @classmethod
    def back_translation_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("mathematical back-translation must not be empty")
        return value.strip()

    @model_validator(mode="after")
    def require_mandatory_field_checks(self) -> ClaimAlignment:
        fields = [check.field.casefold() for check in self.checks]
        duplicates = sorted({field for field in fields if fields.count(field) > 1})
        if duplicates:
            raise ValueError("claim alignment contains duplicate checks: " + ", ".join(duplicates))
        missing = [field for field in MANDATORY_ALIGNMENT_FIELDS if field not in fields]
        if missing:
            raise ValueError("claim alignment is missing mandatory checks: " + ", ".join(missing))
        return self

    @property
    def fully_aligned(self) -> bool:
        checks = {check.field.casefold(): check for check in self.checks}
        return (
            self.status == AlignmentStatus.ALIGNED
            and not self.required_edits
            and all(checks[field].passed for field in MANDATORY_ALIGNMENT_FIELDS)
            and all(check.passed for check in self.checks)
        )


class LeanVerificationResult(BaseModel):
    passed: bool
    build_exit_code: int | None
    axiom_exit_code: int | None
    statement_hash_expected: str
    statement_hash_actual: str | None
    prohibited_occurrences: list[str]
    suspicious_declarations: list[str]
    used_axioms: list[str]
    unapproved_axioms: list[str]
    diagnostics: list[str]
    commands: list[list[str]]


class LeanIterationRecord(BaseModel):
    iteration: int
    codex_exit_code: int
    changed_files: list[str]
    made_progress: bool
    verification: LeanVerificationResult
    iteration_dir: Path
    source_sha256: dict[str, str] = Field(default_factory=dict)
    codex_command: list[str] = Field(default_factory=list)


class LeanWorkflowSettings(BaseModel):
    maximum_statement_revisions: int = Field(default=3, ge=1)
    maximum_codex_iterations: int = Field(default=50, ge=0)
    maximum_no_progress_iterations: int = Field(default=3, ge=1)
    codex_timeout_seconds: int = Field(default=1800, ge=1)
    lean_timeout_seconds: int = Field(default=600, ge=1)
    approved_axioms: list[str] = Field(
        default_factory=lambda: ["propext", "Classical.choice", "Quot.sound"]
    )
    prohibited_tokens: list[str] = Field(default_factory=lambda: ["sorry", "admit", "by?", "TODO"])
    build_command: tuple[str, ...] | None = None
    axiom_command: tuple[str, ...] | None = None
    allow_project_edits: bool = False


class LeanPipelineResult(BaseModel):
    outcome: LeanOutcome
    feasibility: LeanFeasibilityAssessment
    statement_draft: LeanStatementDraft | None = None
    alignment: ClaimAlignment | None = None
    approved_statement_hash: str | None = None
    iterations: list[LeanIterationRecord] = Field(default_factory=list)
    verification: LeanVerificationResult | None = None
    unresolved_obligations: list[str] = Field(default_factory=list)
    artifacts: ArtifactManifest = Field(default_factory=ArtifactManifest)
    calls: CallManifest


_AXIOM_DECLARATION = re.compile(r"(?m)^\s*(?:axiom|axioms)\s+([A-Za-z_][A-Za-z0-9_'.]*)\b")


def _without_lean_comments(source: str) -> str:
    """Blank nested comments and strings while preserving source offsets."""

    output = list(source)
    index = 0
    depth = 0
    in_string = False
    escaped = False
    while index < len(source):
        pair = source[index : index + 2]
        character = source[index]
        if depth:
            if character != "\n":
                output[index] = " "
            if pair == "/-":
                output[index + 1] = " "
                depth += 1
                index += 2
                continue
            if pair == "-/":
                output[index + 1] = " "
                depth -= 1
                index += 2
                continue
            index += 1
            continue
        if in_string:
            if character != "\n":
                output[index] = " "
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if pair == "/-":
            depth = 1
            output[index] = output[index + 1] = " "
            index += 2
            continue
        if pair == "--":
            newline = source.find("\n", index)
            if newline < 0:
                for cursor in range(index, len(source)):
                    output[cursor] = " "
                break
            for cursor in range(index, newline):
                output[cursor] = " "
            index = newline + 1
            continue
        if character == '"':
            output[index] = " "
            in_string = True
            escaped = False
        index += 1
    return "".join(output)


def extract_theorem_statement(source: str, theorem_name: str | None = None) -> tuple[str, str]:
    """Extract a theorem using the deterministic verifier's canonical parser."""

    declarations = list(extract_theorem_statements(source))
    selected = next(
        (
            statement
            for statement in declarations
            if theorem_name is None or statement.name == theorem_name
        ),
        None,
    )
    if selected is None:
        label = theorem_name or "a theorem"
        raise StageValidationError(f"challenge.lean does not contain {label} declaration.")
    matching = [item for item in declarations if item.name == selected.name]
    if len(matching) != 1:
        raise StageValidationError(
            f"The theorem {selected.name!r} must be declared exactly once in challenge.lean."
        )
    return selected.name, selected.canonical


def theorem_statement_hash(source: str, theorem_name: str | None = None) -> tuple[str, str]:
    name, _ = extract_theorem_statement(source, theorem_name)
    return name, canonical_theorem_hash(source, name)


def scan_generated_lean(
    lean_dir: Path,
    prohibited_tokens: list[str],
    theorem_name: str | None = None,
) -> tuple[list[str], list[str]]:
    """Scan every run-local Lean file with the shared deterministic verifier.

    The shared scanner covers ``sorry``, ``admit``, tactic holes, unsafe/implemented-by
    escapes, symlinks, TODOs, and new axiom/constant declarations.  Additional configured
    tokens are scanned as a conservative extension.
    """

    report = scan_lean_tree(lean_dir, target_theorem=theorem_name)
    suspicious_codes = {
        "suspicious_axiom_declaration",
        "opaque_declaration",
        "unsafe_declaration",
        "implementation_override",
        "compile_time_execution",
        "custom_elaborator",
        "foreign_implementation",
        "compile_time_file_read",
    }
    prohibited = [
        f"{item.path}:{item.line}:{item.code}"
        for item in report.findings
        if item.code not in suspicious_codes
    ]
    suspicious = [
        f"{item.path}:{item.line}:{item.code}:{item.message}"
        for item in report.findings
        if item.code in suspicious_codes
    ]
    built_in_tokens = {"sorry", "admit", "by?", "TODO"}
    _validate_generated_tree(lean_dir)
    for token in prohibited_tokens:
        if token in built_in_tokens:
            continue
        for path in _generated_lean_paths(lean_dir):
            source = read_regular_text(path, encoding="utf-8", errors="replace")
            code = _without_lean_comments(source)
            for match in re.finditer(re.escape(token), code, re.IGNORECASE):
                line = source.count("\n", 0, match.start()) + 1
                prohibited.append(f"{path.relative_to(lean_dir)}:{line}:{token}")
    return list(dict.fromkeys(prohibited)), list(dict.fromkeys(suspicious))


def parse_print_axioms(output: str) -> list[str] | None:
    """Parse Lean's ``#print axioms`` output with the shared allowlist checker."""

    report = check_axiom_allowlist(output, ())
    return list(report.used_axioms) if report.output_recognized else None


def _render_command(command: tuple[str, ...], values: dict[str, str]) -> tuple[str, ...]:
    try:
        return tuple(argument.format_map(values) for argument in command)
    except KeyError as exc:
        raise StageValidationError(f"Unknown Lean command placeholder: {exc}") from exc


def _snapshot_lean_sources(lean_dir: Path) -> dict[str, str]:
    _validate_generated_tree(lean_dir)
    return {
        path.relative_to(lean_dir).as_posix(): read_regular_text(
            path, encoding="utf-8", errors="replace"
        )
        for path in _generated_lean_paths(lean_dir)
        if "iterations" not in path.relative_to(lean_dir).parts
    }


def _validate_generated_tree(root: Path) -> None:
    """Reject aliases and special files without ever traversing their targets."""

    try:
        root_entry = os.lstat(root)
    except OSError as exc:
        raise StageValidationError(f"Cannot inspect generated Lean root {root}: {exc}") from exc
    if stat.S_ISLNK(root_entry.st_mode) or not stat.S_ISDIR(root_entry.st_mode):
        raise StageValidationError(f"Generated Lean root must be a real directory: {root}")

    def traversal_error(error: OSError) -> None:
        raise StageValidationError(f"Cannot inspect generated Lean tree: {error}") from error

    for current, directories, names in os.walk(root, followlinks=False, onerror=traversal_error):
        current_path = Path(current)
        for name in sorted(directories):
            _require_generated_entry(current_path / name, directory=True, root=root)
        for name in sorted(names):
            _require_generated_entry(current_path / name, directory=False, root=root)


def _require_generated_entry(path: Path, *, directory: bool, root: Path) -> None:
    relative = path.relative_to(root).as_posix()
    try:
        entry = os.lstat(path)
    except OSError as exc:
        raise StageValidationError(f"Cannot inspect generated entry {relative}: {exc}") from exc
    expected = stat.S_ISDIR(entry.st_mode) if directory else stat.S_ISREG(entry.st_mode)
    if stat.S_ISLNK(entry.st_mode) or not expected:
        kind = "directory" if directory else "regular file"
        raise StageValidationError(f"Generated entry {relative!r} must be a non-symlink {kind}.")


def _generated_lean_paths(root: Path) -> list[Path]:
    paths: list[Path] = []
    for current, directories, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(directories)
        paths.extend(current_path / name for name in sorted(names) if name.endswith(".lean"))
    return paths


def _redact_generated_lean_sources(lean_dir: Path) -> dict[str, list[str] | int]:
    _validate_generated_tree(lean_dir)
    environment_secrets = [
        value
        for key, value in os.environ.items()
        if value
        and any(
            marker in key.upper()
            for marker in ("API_KEY", "ACCESS_TOKEN", "AUTH_TOKEN", "SECRET", "PASSWORD")
        )
    ]
    redactor = SecretRedactor(environment_secrets)
    files: list[str] = []
    categories: set[str] = set()
    replacements = 0
    for path in _generated_lean_paths(lean_dir):
        if "iterations" in path.relative_to(lean_dir).parts:
            continue
        result = redactor.redact_text_result(
            read_regular_text(path, encoding="utf-8", errors="replace")
        )
        if not result.changed:
            continue
        atomic_write_text(path, result.value)
        files.append(path.relative_to(lean_dir).as_posix())
        replacements += result.replacements
        categories.update(result.categories)
    return {
        "files": files,
        "replacements": replacements,
        "categories": sorted(categories),
    }


def _snapshot_project_entries(
    project_root: Path,
    *,
    excluded_roots: tuple[Path, ...],
) -> dict[str, dict[str, str | int | None]]:
    """Snapshot a broader writable tree without following links or reading special files."""

    root = project_root.resolve(strict=True)
    excluded = tuple(Path(os.path.abspath(path)) for path in excluded_roots)
    entries: dict[str, dict[str, str | int | None]] = {}

    def is_excluded(path: Path) -> bool:
        absolute = Path(os.path.abspath(path))
        return any(absolute == item or absolute.is_relative_to(item) for item in excluded)

    def record(path: Path) -> None:
        relative = path.relative_to(root).as_posix()
        try:
            entry = os.lstat(path)
        except OSError as exc:
            raise StageValidationError(f"Cannot audit project entry {relative}: {exc}") from exc
        digest: str | None = None
        if stat.S_ISLNK(entry.st_mode):
            kind = "symlink"
            digest = hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()
        elif stat.S_ISREG(entry.st_mode):
            kind = "regular_file"
            digest = hashlib.sha256(read_regular_bytes(path)).hexdigest()
        elif stat.S_ISDIR(entry.st_mode):
            kind = "directory"
        else:
            kind = "non_regular"
        entries[relative] = {"kind": kind, "sha256": digest, "size": entry.st_size}

    def traversal_error(error: OSError) -> None:
        raise StageValidationError(f"Cannot audit broader writable project: {error}") from error

    for current, directories, names in os.walk(root, followlinks=False, onerror=traversal_error):
        current_path = Path(current)
        kept_directories: list[str] = []
        for name in sorted(directories):
            path = current_path / name
            if name == ".git" or is_excluded(path):
                continue
            record(path)
            if not path.is_symlink():
                kept_directories.append(name)
        directories[:] = kept_directories
        for name in sorted(names):
            path = current_path / name
            if name == ".git" or is_excluded(path):
                continue
            record(path)
    return entries


def _project_changes(
    before: dict[str, dict[str, str | int | None]],
    after: dict[str, dict[str, str | int | None]],
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for path in sorted(set(before) | set(after)):
        old = before.get(path)
        new = after.get(path)
        if old == new:
            continue
        if old is None:
            status = "added"
        elif new is None:
            status = "deleted"
        elif old["kind"] != new["kind"]:
            status = "type_changed"
        else:
            status = "modified"
        changes.append({"path": path, "status": status, "before": old, "after": new})
    return changes


def _render_project_change_diff(changes: list[dict[str, Any]]) -> str:
    lines = ["# status\tpath\tbefore_sha256\tafter_sha256\tbefore_kind\tafter_kind"]
    for change in changes:
        before = change["before"] or {}
        after = change["after"] or {}
        lines.append(
            "\t".join(
                (
                    str(change["status"]),
                    json.dumps(change["path"], ensure_ascii=False),
                    str(before.get("sha256") or "-"),
                    str(after.get("sha256") or "-"),
                    str(before.get("kind") or "-"),
                    str(after.get("kind") or "-"),
                )
            )
        )
    return "\n".join(lines) + "\n"


def _source_diff(before: dict[str, str], after: dict[str, str]) -> tuple[str, list[str]]:
    changed: list[str] = []
    pieces: list[str] = []
    for name in sorted(set(before) | set(after)):
        old = before.get(name, "").splitlines(keepends=True)
        new = after.get(name, "").splitlines(keepends=True)
        if old == new:
            continue
        changed.append(name)
        pieces.extend(difflib.unified_diff(old, new, fromfile=f"a/{name}", tofile=f"b/{name}"))
    return "".join(pieces), changed


def _source_hashes(snapshot: dict[str, str]) -> dict[str, str]:
    return {name: sha256_text(source) for name, source in sorted(snapshot.items())}


def _load_iteration_record(
    path: Path,
    *,
    iteration: int,
    expected_statement_hash: str,
) -> LeanIterationRecord | None:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None
    try:
        record = LeanIterationRecord.model_validate_json(read_regular_text(path, encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StageValidationError(
            f"Cannot resume invalid Lean iteration record {path}: {exc}"
        ) from exc
    if record.iteration != iteration:
        raise StageValidationError(
            f"Lean iteration checkpoint number mismatch in {path}: {record.iteration}"
        )
    if record.verification.statement_hash_expected != expected_statement_hash:
        raise StageValidationError(
            f"Lean iteration checkpoint does not match the approved statement: {path}"
        )
    return record


async def _verify_lean(
    *,
    backend: ExecutionBackend,
    lean_dir: Path,
    project_root: Path,
    settings: LeanWorkflowSettings,
    theorem_name: str,
    expected_statement_hash: str,
) -> tuple[LeanVerificationResult, str, str]:
    _validate_generated_tree(lean_dir)
    challenge_path = lean_dir / "challenge.lean"
    main_path = lean_dir / "Main.lean"
    axiom_check_path = lean_dir / "_AscendAxiomCheck.lean"
    axiom_check_path.unlink(missing_ok=True)

    def command_path(path: Path) -> str:
        try:
            return path.relative_to(project_root).as_posix()
        except ValueError:
            return str(path)

    values = {
        "lean_dir": str(lean_dir),
        "project_root": str(project_root),
        "challenge": command_path(challenge_path),
        "main": command_path(main_path),
        "axiom_check": command_path(axiom_check_path),
        "theorem": theorem_name,
    }
    build_template = settings.build_command or (
        "lake",
        "lean",
        "{challenge}",
    )
    build_argv = _render_command(build_template, values)
    commands = [list(build_argv)]
    build_log: str
    try:
        build = await backend.run(
            CommandRequest(
                argv=build_argv,
                cwd=project_root,
                timeout_seconds=settings.lean_timeout_seconds,
            )
        )
        build_log = (
            "$ "
            + " ".join(build.argv)
            + "\n[stdout]\n"
            + build.stdout
            + "\n[stderr]\n"
            + build.stderr
        )
    except Exception as exc:
        build = CommandResult(
            argv=build_argv,
            cwd=project_root,
            exit_code=-1,
            stdout="",
            stderr=f"Lean build backend failed: {exc}",
            duration_seconds=0.0,
        )
        build_log = f"Lean build backend failed: {exc}\n"
    _validate_generated_tree(lean_dir)

    axiom_exit: int | None = None
    axiom_output = ""
    axiom_output_complete = False
    axiom_log = ""
    if build.exit_code == 0:
        atomic_write_text(
            axiom_check_path,
            read_regular_text(challenge_path, encoding="utf-8").rstrip()
            + f"\n\n#print axioms {theorem_name}\n",
        )
        axiom_template = settings.axiom_command or (
            "lake",
            "lean",
            "{axiom_check}",
        )
        axiom_argv = _render_command(axiom_template, values)
        commands.append(list(axiom_argv))
        try:
            axiom_result = await backend.run(
                CommandRequest(
                    argv=axiom_argv,
                    cwd=project_root,
                    timeout_seconds=settings.lean_timeout_seconds,
                )
            )
            axiom_exit = axiom_result.exit_code
            axiom_output = f"{axiom_result.stdout}\n{axiom_result.stderr}"
            axiom_output_complete = not (
                axiom_result.stdout_truncated
                or axiom_result.stderr_truncated
                or axiom_result.timed_out
            )
            axiom_log = (
                "$ "
                + " ".join(axiom_result.argv)
                + "\n[stdout]\n"
                + axiom_result.stdout
                + "\n[stderr]\n"
                + axiom_result.stderr
            )
        except Exception as exc:
            axiom_log = f"Axiom inspection backend failed: {exc}\n"
            axiom_output = axiom_log
        finally:
            try:
                _validate_generated_tree(lean_dir)
            finally:
                try:
                    axiom_entry = os.lstat(axiom_check_path)
                except FileNotFoundError:
                    pass
                else:
                    if not stat.S_ISDIR(axiom_entry.st_mode):
                        axiom_check_path.unlink()

    certificate = verify_build(
        lean_dir,
        expected_statement_hash,
        build,
        axiom_output,
        settings.approved_axioms,
        theorem_name=theorem_name,
        statement_file=challenge_path,
    )
    prohibited, suspicious = scan_generated_lean(lean_dir, settings.prohibited_tokens, theorem_name)
    diagnostics = [issue.message for issue in certificate.issues]
    if prohibited:
        diagnostics.append("Generated Lean sources contain prohibited proof placeholders.")
    if suspicious:
        diagnostics.append("Generated Lean sources contain suspicious declarations or escapes.")
    if build.exit_code == 0 and axiom_exit != 0:
        diagnostics.append(
            "#print axioms command did not exit successfully."
            if axiom_exit is not None
            else "Axiom inspection backend failed before returning a result."
        )
    if build.exit_code == 0 and not axiom_output_complete:
        diagnostics.append("#print axioms output was incomplete or truncated.")
    passed = (
        certificate.passed
        and axiom_exit == 0
        and axiom_output_complete
        and not prohibited
        and not suspicious
    )
    return (
        LeanVerificationResult(
            passed=passed,
            build_exit_code=build.exit_code,
            axiom_exit_code=axiom_exit,
            statement_hash_expected=expected_statement_hash,
            statement_hash_actual=certificate.actual_statement_hash,
            prohibited_occurrences=prohibited,
            suspicious_declarations=suspicious,
            used_axioms=list(certificate.used_axioms),
            unapproved_axioms=list(certificate.unapproved_axioms),
            diagnostics=list(dict.fromkeys(diagnostics)),
            commands=commands,
        ),
        build_log,
        axiom_log,
    )


def _formalization_yaml(
    *,
    project_root: Path,
    theorem_name: str,
    statement_hash: str,
    settings: LeanWorkflowSettings,
) -> str:
    toolchain_path = project_root / "lean-toolchain"
    toolchain = (
        toolchain_path.read_text(encoding="utf-8").strip()
        if toolchain_path.is_file()
        else "unknown"
    )
    lines = [
        'project_name: "ASCEND formalization"',
        f"main_theorem_name: {json.dumps(theorem_name)}",
        'challenge_file: "challenge.lean"',
        'implementation_file: "Main.lean"',
        f"lean_project_root: {json.dumps(str(project_root))}",
        f"lean_toolchain: {json.dumps(toolchain)}",
        'mathlib_revision: "unknown"',
        f"statement_hash: {json.dumps(statement_hash)}",
        "approved_axioms:",
        *(f"  - {json.dumps(item)}" for item in settings.approved_axioms),
        "prohibited_tokens:",
        *(f"  - {json.dumps(item)}" for item in settings.prohibited_tokens),
        "verification_commands:",
    ]
    build_command = settings.build_command or ("lake", "lean", "{challenge}")
    lines.append("  - " + json.dumps(list(build_command)))
    return "\n".join(lines) + "\n"


async def run_lean_pipeline(
    *,
    client: ModelClient,
    codex_client: CodexClient,
    backend: ExecutionBackend,
    research_result: ResearchResult,
    manuscript_result: ManuscriptResult,
    claim_contract: dict[str, Any],
    lean_dir: Path,
    lean_project_root: Path,
    workflow_settings: LeanWorkflowSettings | None = None,
    model_settings: ModelSettings | None = None,
    feasibility_prompt_path: Path | None = None,
    statement_generator_prompt_path: Path | None = None,
    statement_auditor_prompt_path: Path | None = None,
    codex_prompt_path: Path | None = None,
) -> LeanPipelineResult:
    """Run gated Lean feasibility, alignment, Codex iterations and kernel checks.

    ``lean_dir`` is the final run-local Lean stage directory.  Codex receives that directory
    as its sole writable path unless ``allow_project_edits`` is explicitly enabled.
    ``lean_project_root`` is read for the existing Lake/mathlib environment and is used as
    the deterministic verifier's working directory.
    """

    if not manuscript_result.passed_lean_gate:
        raise StageGateError(
            "Lean requires both a fully verified bibliography and a successful LaTeX build."
        )
    if (
        research_result.outcome != ResearchOutcome.ACCEPTED
        or research_result.candidate is None
        or research_result.acceptance_gate is None
        or manuscript_result.research_gate.candidate_sha256
        != research_result.acceptance_gate.candidate_sha256
    ):
        raise StageGateError("Lean inputs do not match the frozen accepted research package.")
    if not lean_project_root.resolve().is_dir():
        raise StageValidationError(f"Lean project root does not exist: {lean_project_root}")
    if sha256_json(research_result.candidate) != research_result.acceptance_gate.candidate_sha256:
        raise StageGateError("The frozen accepted proof package failed its integrity hash.")

    settings = workflow_settings or LeanWorkflowSettings()
    model = model_settings or ModelSettings(reasoning_effort="xhigh", web_search=False)
    destination = ensure_stage_directory(lean_dir)
    iterations_dir = ensure_stage_directory(destination / "iterations")
    _validate_generated_tree(destination)
    prompts = {
        "feasibility": feasibility_prompt_path or project_resource("prompts/lean_feasibility.md"),
        "generator": statement_generator_prompt_path
        or project_resource("prompts/lean_statement_generator.md"),
        "auditor": statement_auditor_prompt_path
        or project_resource("prompts/lean_statement_auditor.md"),
        "codex": codex_prompt_path or project_resource("prompts/codex_formalizer.md"),
    }
    try:
        prompt_text = {name: path.read_text(encoding="utf-8") for name, path in prompts.items()}
    except OSError as exc:
        raise StageValidationError(f"Cannot read a Lean-stage prompt: {exc}") from exc

    response_ids: list[str] = []
    model_calls = 0
    codex_calls = 0
    artifact_paths: dict[str, Path] = {}
    iteration_records: list[LeanIterationRecord] = []

    async def model_call(
        instructions: str, input_value: dict[str, Any], output_type: type[BaseModel]
    ) -> BaseModel:
        nonlocal model_calls
        model_calls += 1
        result = await client.generate_structured(
            ModelRequest(
                instructions=instructions,
                input_text=json.dumps(input_value, ensure_ascii=False),
                settings=model,
            ),
            output_type,
        )
        response_ids.append(result.response_id)
        return result.parsed

    common_input = {
        "frozen_claim_contract": claim_contract,
        "frozen_proof_package": research_result.candidate.model_dump(mode="json"),
        "verified_manuscript": manuscript_result.draft.paper_tex,
        "lean_project_root": str(lean_project_root.resolve()),
    }
    feasibility_model = await model_call(
        prompt_text["feasibility"], common_input, LeanFeasibilityAssessment
    )
    if not isinstance(feasibility_model, LeanFeasibilityAssessment):
        raise StageValidationError("Model client returned the wrong feasibility output type.")
    feasibility = feasibility_model
    artifact_paths["feasibility"] = atomic_write_json(destination / "feasibility.json", feasibility)

    def make_result(
        outcome: LeanOutcome,
        *,
        draft: LeanStatementDraft | None = None,
        alignment: ClaimAlignment | None = None,
        statement_hash: str | None = None,
        verification: LeanVerificationResult | None = None,
        obligations: list[str] | None = None,
    ) -> LeanPipelineResult:
        result = LeanPipelineResult(
            outcome=outcome,
            feasibility=feasibility,
            statement_draft=draft,
            alignment=alignment,
            approved_statement_hash=statement_hash,
            iterations=iteration_records,
            verification=verification,
            unresolved_obligations=obligations or [],
            artifacts=build_artifact_manifest(artifact_paths),
            calls=CallManifest(
                model_calls=model_calls,
                codex_calls=codex_calls,
                response_ids=response_ids,
            ),
        )
        atomic_write_json(destination / "result.json", result)
        return result

    if feasibility.classification == LeanFeasibilityClass.NOT_ATTAINABLE:
        return make_result(
            LeanOutcome.INFEASIBLE,
            obligations=[feasibility.explanation, *feasibility.difficult_components],
        )

    draft: LeanStatementDraft | None = None
    alignment: ClaimAlignment | None = None
    revision_requirements: list[str] = []
    for revision in range(settings.maximum_statement_revisions):
        generation_input = dict(common_input)
        if draft is not None:
            generation_input.update(
                {
                    "previous_challenge": draft.model_dump(mode="json"),
                    "mandatory_alignment_edits": revision_requirements,
                }
            )
        generated = await model_call(prompt_text["generator"], generation_input, LeanStatementDraft)
        if not isinstance(generated, LeanStatementDraft):
            raise StageValidationError("Model client returned the wrong statement output type.")
        draft = generated
        if _AXIOM_DECLARATION.search(_without_lean_comments(draft.challenge_lean)):
            raise StageValidationError("Generated challenge.lean encodes the target as an axiom.")
        theorem_name, _ = theorem_statement_hash(draft.challenge_lean, draft.theorem_name)
        if draft.theorem_name is not None and theorem_name != draft.theorem_name:
            raise StageValidationError("Generated theorem_name does not match challenge.lean.")

        revision_dir = ensure_stage_directory(destination / "statement_revisions" / str(revision))
        # Archived, unaudited drafts may contain an intentional temporary proof hole.  Keep
        # them as text diagnostics rather than executable .lean files so the final recursive
        # verifier scans only generated sources that could participate in the build.
        atomic_write_text(revision_dir / "challenge.txt", draft.challenge_lean)
        auditor_input = {
            "frozen_claim_contract": claim_contract,
            "challenge_lean": draft.challenge_lean,
            "generator_back_translation": draft.statement_explanation,
            "generator_claim_map": [entry.model_dump(mode="json") for entry in draft.claim_map],
            "mandatory_alignment_fields": list(MANDATORY_ALIGNMENT_FIELDS),
            "alignment_gate_requirement": (
                "Return exactly one explicit, explained check for every mandatory field. "
                "An aligned verdict passes only when every mandatory and additional check passes."
            ),
        }
        audited = await model_call(prompt_text["auditor"], auditor_input, ClaimAlignment)
        if not isinstance(audited, ClaimAlignment):
            raise StageValidationError("Model client returned the wrong alignment output type.")
        alignment = audited
        atomic_write_json(revision_dir / "CLAIM_ALIGNMENT.json", alignment)
        if alignment.fully_aligned:
            break
        if alignment.status == AlignmentStatus.REJECTED:
            return make_result(
                LeanOutcome.FAILED,
                draft=draft,
                alignment=alignment,
                obligations=alignment.required_edits
                or ["The generated Lean statement was rejected as misaligned."],
            )
        revision_requirements = [
            *alignment.required_edits,
            *(
                check.explanation
                for check in alignment.checks
                if not check.passed and check.explanation
            ),
        ]
    if draft is None or alignment is None or not alignment.fully_aligned:
        return make_result(
            LeanOutcome.FAILED,
            draft=draft,
            alignment=alignment,
            obligations=revision_requirements
            or ["Lean statement alignment revision limit reached."],
        )

    theorem_name, approved_hash = theorem_statement_hash(draft.challenge_lean, draft.theorem_name)
    challenge_destination = destination / "challenge.lean"
    preserve_existing_sources = False
    try:
        os.lstat(challenge_destination)
    except FileNotFoundError:
        pass
    else:
        existing_challenge = read_regular_text(challenge_destination, encoding="utf-8")
        try:
            _, existing_hash = theorem_statement_hash(existing_challenge, theorem_name)
        except (StageValidationError, ValueError):
            existing_hash = ""
        preserve_existing_sources = existing_hash == approved_hash
    artifact_paths["challenge"] = (
        challenge_destination
        if preserve_existing_sources
        else atomic_write_text(challenge_destination, draft.challenge_lean)
    )
    artifact_paths["statement_explanation"] = atomic_write_text(
        destination / "STATEMENT_EXPLANATION.md", draft.statement_explanation
    )
    artifact_paths["claim_alignment"] = atomic_write_json(
        destination / "CLAIM_ALIGNMENT.json", alignment
    )
    instructions_text = (
        "# Formalization Instructions\n\n"
        f"Approved theorem: `{theorem_name}`\n\n"
        f"Approved statement SHA-256: `{approved_hash}`\n\n"
        "Implement the proof in `challenge.lean`; keep its theorem header byte-for-byte "
        "semantically unchanged. `challenge.lean` is the deterministic build entry point. Never "
        "use sorry, admit, by?, TODO, new axioms, or declarations that encode the target.\n"
    )
    artifact_paths["formalization_instructions"] = atomic_write_text(
        destination / "FORMALIZATION_INSTRUCTIONS.md", instructions_text
    )
    artifact_paths["formalization_config"] = atomic_write_text(
        destination / "formalization.yaml",
        _formalization_yaml(
            project_root=lean_project_root.resolve(),
            theorem_name=theorem_name,
            statement_hash=approved_hash,
            settings=settings,
        ),
    )
    main_destination = destination / "Main.lean"
    if preserve_existing_sources:
        try:
            main_entry = os.lstat(main_destination)
        except FileNotFoundError:
            artifact_paths["main"] = atomic_write_text(main_destination, "import challenge\n")
        else:
            if not stat.S_ISREG(main_entry.st_mode):  # guarded by tree validation
                raise StageValidationError("Existing Main.lean is not a regular file.")
            artifact_paths["main"] = main_destination
    else:
        artifact_paths["main"] = atomic_write_text(main_destination, "import challenge\n")

    if feasibility.classification == LeanFeasibilityClass.VERIFICATION_PLAN:
        return make_result(
            LeanOutcome.STATEMENT_ONLY,
            draft=draft,
            alignment=alignment,
            statement_hash=approved_hash,
            obligations=feasibility.difficult_components,
        )
    if settings.maximum_codex_iterations == 0:
        return make_result(
            LeanOutcome.PARTIAL,
            draft=draft,
            alignment=alignment,
            statement_hash=approved_hash,
            obligations=["Codex iteration budget is zero."],
        )

    no_progress = 0
    last_verification: LeanVerificationResult | None = None
    for iteration in range(1, settings.maximum_codex_iterations + 1):
        iteration_dir = ensure_stage_directory(iterations_dir / str(iteration))
        checkpoint_path = iteration_dir / "record.json"
        checkpoint = _load_iteration_record(
            checkpoint_path,
            iteration=iteration,
            expected_statement_hash=approved_hash,
        )
        if checkpoint is not None:
            artifact_paths[f"iteration_{iteration}_record"] = checkpoint_path
            iteration_records.append(checkpoint)
            last_verification = checkpoint.verification
            no_progress = 0 if checkpoint.made_progress else no_progress + 1
            current_sources = _snapshot_lean_sources(destination)
            current_hashes = _source_hashes(current_sources)
            if checkpoint.source_sha256 and checkpoint.source_sha256 == current_hashes:
                if checkpoint.verification.passed:
                    verification, build_log, axiom_log = await _verify_lean(
                        backend=backend,
                        lean_dir=destination,
                        project_root=lean_project_root.resolve(),
                        settings=settings,
                        theorem_name=theorem_name,
                        expected_statement_hash=approved_hash,
                    )
                    verified_sources = _snapshot_lean_sources(destination)
                    checkpoint = checkpoint.model_copy(
                        update={
                            "verification": verification,
                            "source_sha256": _source_hashes(verified_sources),
                        }
                    )
                    iteration_records[-1] = checkpoint
                    last_verification = verification
                    atomic_write_json(checkpoint_path, checkpoint)
                    artifact_paths[f"iteration_{iteration}_diagnostics"] = atomic_write_text(
                        iteration_dir / "lean_diagnostics.log", build_log
                    )
                    artifact_paths[f"iteration_{iteration}_axioms"] = atomic_write_text(
                        iteration_dir / "axioms.log", axiom_log
                    )
                    artifact_paths[f"iteration_{iteration}_verdict"] = atomic_write_json(
                        iteration_dir / "verdict.json", verification
                    )
                    artifact_paths[f"iteration_{iteration}_commands"] = atomic_write_json(
                        iteration_dir / "commands.json",
                        {
                            "codex": checkpoint.codex_command,
                            "deterministic_verification": verification.commands,
                        },
                    )
                    artifact_paths["build_log"] = atomic_write_text(
                        destination / "build.log", build_log
                    )
                    artifact_paths["axioms"] = atomic_write_text(
                        destination / "axioms.txt", axiom_log
                    )
                    if _snapshot_lean_sources(destination) != verified_sources:
                        raise StageValidationError(
                            "Verified Lean sources changed while provenance artifacts were written."
                        )
                    if verification.passed:
                        outcome = (
                            LeanOutcome.VERIFIED_WITH_APPROVED_AXIOMS
                            if verification.used_axioms
                            else LeanOutcome.VERIFIED
                        )
                        return make_result(
                            outcome,
                            draft=draft,
                            alignment=alignment,
                            statement_hash=approved_hash,
                            verification=verification,
                        )
                if no_progress >= settings.maximum_no_progress_iterations:
                    return make_result(
                        LeanOutcome.PARTIAL,
                        draft=draft,
                        alignment=alignment,
                        statement_hash=approved_hash,
                        verification=last_verification,
                        obligations=[
                            "Codex reached the repeated no-progress limit.",
                            *last_verification.diagnostics,
                        ],
                    )
            continue
        latest_diagnostics = (
            last_verification.model_dump(mode="json") if last_verification is not None else {}
        )
        codex_prompt = (
            prompt_text["codex"]
            + "\n\n"
            + json.dumps(
                {
                    "bounded_iteration": iteration,
                    "maximum_iterations": settings.maximum_codex_iterations,
                    "accepted_proof": research_result.candidate.model_dump(mode="json"),
                    "verified_manuscript": manuscript_result.draft.paper_tex,
                    "formalization_instructions": instructions_text,
                    "latest_deterministic_diagnostics": latest_diagnostics,
                },
                ensure_ascii=False,
            )
        )
        artifact_paths[f"iteration_{iteration}_prompt"] = atomic_write_text(
            iteration_dir / "prompt.md", codex_prompt
        )
        before = _snapshot_lean_sources(destination)
        writable_paths = [destination]
        project_before: dict[str, dict[str, str | int | None]] | None = None
        if settings.allow_project_edits:
            writable_paths.append(lean_project_root.resolve())
            project_before = _snapshot_project_entries(
                lean_project_root,
                excluded_roots=(destination,),
            )
        artifact_paths[f"iteration_{iteration}_writable_paths"] = atomic_write_json(
            iteration_dir / "writable_paths.json",
            {
                "schema_version": 1,
                "iteration": iteration,
                "cwd": str(destination),
                "allow_project_edits": settings.allow_project_edits,
                "writable_paths": [str(path) for path in writable_paths],
            },
        )
        codex_calls += 1
        codex_result: CodexResult = await codex_client.execute(
            CodexRequest(
                prompt=codex_prompt,
                cwd=destination,
                writable_paths=tuple(writable_paths),
                timeout_seconds=settings.codex_timeout_seconds,
                jsonl_path=iteration_dir / "codex.jsonl",
                allow_broader_writes=settings.allow_project_edits,
            )
        )
        _validate_generated_tree(destination)
        if project_before is not None:
            project_after = _snapshot_project_entries(
                lean_project_root,
                excluded_roots=(destination,),
            )
            project_changes = _project_changes(project_before, project_after)
            project_manifest = {
                "schema_version": 1,
                "iteration": iteration,
                "project_root": str(lean_project_root.resolve()),
                "excluded": [".git", str(destination)],
                "changes": project_changes,
            }
            artifact_paths[f"iteration_{iteration}_project_changes"] = atomic_write_json(
                iteration_dir / "project_changes.json", project_manifest
            )
            artifact_paths[f"iteration_{iteration}_project_diff"] = atomic_write_text(
                iteration_dir / "project_changes.diff",
                _render_project_change_diff(project_changes),
            )
            unsafe_changes = [
                change
                for change in project_changes
                if change["after"] is not None
                and change["after"]["kind"] in {"symlink", "non_regular"}
            ]
            if unsafe_changes:
                paths = ", ".join(str(change["path"]) for change in unsafe_changes[:5])
                raise StageValidationError(
                    "Codex created or changed unsafe project entries outside its run directory: "
                    + paths
                )
        source_redactions = _redact_generated_lean_sources(destination)
        if source_redactions["replacements"]:
            artifact_paths[f"iteration_{iteration}_source_redactions"] = atomic_write_json(
                iteration_dir / "source_redactions.json", source_redactions
            )
        artifact_paths[f"iteration_{iteration}_stdout"] = atomic_write_text(
            iteration_dir / "codex.stdout", codex_result.stdout
        )
        artifact_paths[f"iteration_{iteration}_stderr"] = atomic_write_text(
            iteration_dir / "codex.stderr", codex_result.stderr
        )
        if codex_result.jsonl_path is not None:
            # Copy only the adapter's already validated/redacted JSONL trace.
            artifact_paths[f"iteration_{iteration}_jsonl"] = atomic_write_bytes(
                iteration_dir / "codex.jsonl", read_regular_bytes(codex_result.jsonl_path)
            )
        else:
            artifact_paths[f"iteration_{iteration}_jsonl"] = atomic_write_text(
                iteration_dir / "codex.jsonl", codex_result.stdout
            )
        after = _snapshot_lean_sources(destination)
        patch, changed_files = _source_diff(before, after)
        patch = SecretRedactor().redact_text(patch)
        artifact_paths[f"iteration_{iteration}_diff"] = atomic_write_text(
            iteration_dir / "diff.patch", patch
        )
        made_progress = bool(changed_files)
        no_progress = 0 if made_progress else no_progress + 1

        verification, build_log, axiom_log = await _verify_lean(
            backend=backend,
            lean_dir=destination,
            project_root=lean_project_root.resolve(),
            settings=settings,
            theorem_name=theorem_name,
            expected_statement_hash=approved_hash,
        )
        last_verification = verification
        verified_sources = _snapshot_lean_sources(destination)
        record = LeanIterationRecord(
            iteration=iteration,
            codex_exit_code=codex_result.exit_code,
            changed_files=changed_files,
            made_progress=made_progress,
            verification=verification,
            iteration_dir=iteration_dir,
            source_sha256=_source_hashes(verified_sources),
            codex_command=list(codex_result.command),
        )
        iteration_records.append(record)
        artifact_paths[f"iteration_{iteration}_record"] = atomic_write_json(checkpoint_path, record)
        artifact_paths[f"iteration_{iteration}_diagnostics"] = atomic_write_text(
            iteration_dir / "lean_diagnostics.log", build_log
        )
        artifact_paths[f"iteration_{iteration}_axioms"] = atomic_write_text(
            iteration_dir / "axioms.log", axiom_log
        )
        artifact_paths[f"iteration_{iteration}_commands"] = atomic_write_json(
            iteration_dir / "commands.json",
            {
                "codex": list(codex_result.command),
                "deterministic_verification": verification.commands,
            },
        )
        artifact_paths[f"iteration_{iteration}_verdict"] = atomic_write_json(
            iteration_dir / "verdict.json", verification
        )
        artifact_paths["build_log"] = atomic_write_text(destination / "build.log", build_log)
        artifact_paths["axioms"] = atomic_write_text(destination / "axioms.txt", axiom_log)
        if _snapshot_lean_sources(destination) != verified_sources:
            raise StageValidationError(
                "Verified Lean sources changed while provenance artifacts were being written."
            )
        if verification.passed:
            outcome = (
                LeanOutcome.VERIFIED_WITH_APPROVED_AXIOMS
                if verification.used_axioms
                else LeanOutcome.VERIFIED
            )
            return make_result(
                outcome,
                draft=draft,
                alignment=alignment,
                statement_hash=approved_hash,
                verification=verification,
            )
        if "ASCEND_INFEASIBLE" in f"{codex_result.stdout}\n{codex_result.stderr}":
            return make_result(
                LeanOutcome.PARTIAL,
                draft=draft,
                alignment=alignment,
                statement_hash=approved_hash,
                verification=verification,
                obligations=[
                    "Codex reported formalization infeasibility.",
                    *verification.diagnostics,
                ],
            )
        if no_progress >= settings.maximum_no_progress_iterations:
            return make_result(
                LeanOutcome.PARTIAL,
                draft=draft,
                alignment=alignment,
                statement_hash=approved_hash,
                verification=verification,
                obligations=[
                    "Codex reached the repeated no-progress limit.",
                    *verification.diagnostics,
                ],
            )

    return make_result(
        LeanOutcome.PARTIAL,
        draft=draft,
        alignment=alignment,
        statement_hash=approved_hash,
        verification=last_verification,
        obligations=[
            "Codex iteration budget exhausted.",
            *(last_verification.diagnostics if last_verification is not None else []),
        ],
    )
