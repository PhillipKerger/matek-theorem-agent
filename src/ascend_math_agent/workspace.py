"""Project discovery, run workspaces, safe paths, and atomic artifact writes."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import unicodedata
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

PROJECT_MARKERS = ("lean-toolchain", "lakefile.toml", "lakefile.lean", ".git")

# These are the concrete (non-wildcard) directories in ARTIFACT_CONTRACT.md.
ARTIFACT_DIRECTORIES = (
    "input",
    "config",
    "prompts",
    "research",
    "research/rounds",
    "research/candidate",
    "research/audits",
    "manuscript",
    "lean",
    "lean/iterations",
    "report",
    "logs",
    "traces",
    "traces/codex",
)

_LEGACY_RUN_ID_PATTERN = re.compile(
    r"\A(?P<timestamp>\d{8}T\d{6}Z)-[a-z0-9](?:[a-z0-9_-]{0,47})-[a-f0-9]{6}\Z"
)
_PROBLEM_RUN_ID_PATTERN = re.compile(
    r"\Arun-[a-z0-9](?:[a-z0-9_-]{0,72})-"
    r"(?P<timestamp>\d{8}T\d{6}Z)-[a-f0-9]{6}\Z"
)
_SLUG_UNSAFE = re.compile(r"[^a-z0-9_-]+")
_SLUG_SEPARATORS = re.compile(r"[-_]{2,}")


class WorkspaceError(ValueError):
    """Base error for an invalid or unsafe workspace operation."""


class PathConfinementError(WorkspaceError):
    """Raised when a path escapes its permitted root."""


class InvalidRunIdError(WorkspaceError):
    """Raised when a caller-supplied run ID is unsafe or malformed."""


class RunLockError(WorkspaceError):
    """Raised when a run's advisory lock cannot be used safely."""


class RunLockHeldError(RunLockError):
    """Raised when another process already owns a run's advisory lock."""

    def __init__(self, run_id: str, lock_path: Path, owner: dict[str, Any]) -> None:
        self.run_id = run_id
        self.lock_path = lock_path
        self.owner = owner
        details: list[str] = []
        pid = owner.get("pid")
        acquired_at = owner.get("acquired_at")
        if isinstance(pid, int):
            details.append(f"pid {pid}")
        if isinstance(acquired_at, str) and acquired_at:
            details.append(f"since {acquired_at}")
        holder = f" ({', '.join(details)})" if details else ""
        super().__init__(
            f"run {run_id!r} is already active{holder}; wait for it to finish before "
            "resuming or rewriting it"
        )


class RunLock:
    """Fail-fast POSIX advisory lock for one run.

    Locks live beneath ``.ascend/locks`` rather than inside a run so acquiring a
    lock never changes that run's certified artifact inventory. The lock file is
    intentionally retained after release: unlinking it could let contenders lock
    different inodes and enter the same run concurrently.
    """

    def __init__(self, run_root: Path) -> None:
        unresolved = run_root.expanduser()
        if unresolved.is_symlink():
            raise RunLockError(f"refusing to lock a symlinked run workspace: {run_root}")
        try:
            resolved = unresolved.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise RunLockError(f"cannot resolve run workspace {run_root}: {exc}") from exc
        if not resolved.is_dir():
            raise RunLockError(f"run workspace is not a directory: {resolved}")
        validate_run_id(resolved.name)
        if resolved.parent.name != "runs" or resolved.parent.parent.name != ".ascend":
            raise RunLockError(
                f"run workspace must be located at .ascend/runs/<run-id>: {resolved}"
            )

        ascend_root = resolved.parent.parent
        locks_root = ascend_root / "locks"
        if locks_root.is_symlink():
            raise RunLockError(f"refusing to use a symlinked lock directory: {locks_root}")
        try:
            locks_root.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise RunLockError(f"cannot create run-lock directory {locks_root}: {exc}") from exc
        try:
            lock_directory = ensure_path_confined(ascend_root, locks_root)
        except WorkspaceError as exc:
            raise RunLockError(f"unsafe run-lock directory {locks_root}: {exc}") from exc
        try:
            directory_status = lock_directory.stat(follow_symlinks=False)
        except OSError as exc:
            raise RunLockError(
                f"cannot inspect run-lock directory {lock_directory}: {exc}"
            ) from exc
        if not stat.S_ISDIR(directory_status.st_mode):
            raise RunLockError(f"run-lock path is not a directory: {lock_directory}")

        self.run_id = resolved.name
        self.lock_path = lock_directory / f"{self.run_id}.lock"
        self._descriptor: int | None = None

    def _open_lock_file(self) -> int:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        file_flags = (
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        directory_descriptor: int | None = None
        try:
            directory_descriptor = os.open(self.lock_path.parent, directory_flags)
            descriptor = os.open(
                self.lock_path.name,
                file_flags,
                0o600,
                dir_fd=directory_descriptor,
            )
        except OSError as exc:
            raise RunLockError(f"cannot safely open run lock {self.lock_path}: {exc}") from exc
        finally:
            if directory_descriptor is not None:
                os.close(directory_descriptor)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RunLockError(f"run lock is not a regular file: {self.lock_path}")
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    @staticmethod
    def _read_owner(descriptor: int) -> dict[str, Any]:
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            raw = os.read(descriptor, 4096)
            value = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(value, dict):
            return {}
        return {
            key: item
            for key, item in value.items()
            if key in {"schema_version", "run_id", "pid", "acquired_at"}
            and isinstance(item, (str, int))
            and not isinstance(item, bool)
        }

    @staticmethod
    def _write_all(descriptor: int, payload: bytes) -> None:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:  # pragma: no cover - defensive POSIX invariant
                raise OSError("short write while recording run-lock owner")
            remaining = remaining[written:]

    def acquire(self) -> RunLock:
        if self._descriptor is not None:
            raise RunLockError(f"run lock is already acquired: {self.lock_path}")
        descriptor = self._open_lock_file()
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                owner = self._read_owner(descriptor)
                os.close(descriptor)
                raise RunLockHeldError(self.run_id, self.lock_path, owner) from exc
            os.close(descriptor)
            raise RunLockError(f"cannot acquire run lock {self.lock_path}: {exc}") from exc

        try:
            owner = {
                "schema_version": 1,
                "run_id": self.run_id,
                "pid": os.getpid(),
                "acquired_at": datetime.now(UTC).isoformat(),
            }
            payload = (json.dumps(owner, sort_keys=True) + "\n").encode("utf-8")
            os.fchmod(descriptor, 0o600)
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            self._write_all(descriptor, payload)
            os.fsync(descriptor)
        except BaseException:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        return self

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> RunLock:
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.release()


def discover_project_root(start: Path) -> Path:
    """Return the nearest ancestor containing a Lean/Lake or Git marker.

    If no marker exists, the resolved starting directory is returned.  This keeps
    research-only projects usable while ensuring all callers agree on one absolute
    workspace root.
    """

    expanded = start.expanduser()
    if not expanded.exists():
        raise WorkspaceError(f"project discovery path does not exist: {start}")
    current = expanded.resolve(strict=True)
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if any((candidate / marker).exists() for marker in PROJECT_MARKERS):
            return candidate
    return current


def _slugify_component(value: str | None, *, default: str, max_length: int) -> str:
    if value is None or not value.strip():
        return default
    ascii_name = (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", errors="ignore")
        .decode("ascii")
        .lower()
    )
    slug = _SLUG_UNSAFE.sub("-", ascii_name)
    slug = _SLUG_SEPARATORS.sub("-", slug).strip("-_.")
    slug = slug[:max_length].rstrip("-_")
    return slug or default


def _slugify_run_name(run_name: str | None) -> str:
    return _slugify_component(run_name, default="run", max_length=48)


def generate_run_id(
    run_name: str | None = None,
    *,
    problem_name: str | None = None,
    now: datetime | None = None,
    random_suffix: str | None = None,
) -> str:
    """Generate a portable, traversal-safe, collision-resistant run identifier.

    New workflow runs supply ``problem_name`` and use
    ``run-<problem>[-<name>]-<timestamp>-<suffix>``. Calls without a problem name
    retain the original timestamp-first format for API and on-disk compatibility.
    """

    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    timestamp = moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = random_suffix or secrets.token_hex(3)
    if not re.fullmatch(r"[a-f0-9]{6}", suffix):
        raise InvalidRunIdError("run ID random suffix must be six lowercase hexadecimal digits")
    if problem_name is None:
        run_id = f"{timestamp}-{_slugify_run_name(run_name)}-{suffix}"
    else:
        problem_slug = _slugify_component(problem_name, default="problem", max_length=48)
        label = problem_slug
        if run_name is not None and run_name.strip():
            name_slug = _slugify_component(run_name, default="run", max_length=24)
            label = f"{problem_slug}-{name_slug}"
        run_id = f"run-{label}-{timestamp}-{suffix}"
    validate_run_id(run_id)
    return run_id


def validate_run_id(run_id: str) -> str:
    """Validate and return a run ID suitable for use as one path component."""

    if not (_LEGACY_RUN_ID_PATTERN.fullmatch(run_id) or _PROBLEM_RUN_ID_PATTERN.fullmatch(run_id)):
        raise InvalidRunIdError(
            "run ID must match run-problem[-name]-YYYYMMDDTHHMMSSZ-xxxxxx "
            "(or the legacy YYYYMMDDTHHMMSSZ-name-xxxxxx format) using lowercase safe "
            "characters"
        )
    if run_id in {".", ".."} or Path(run_id).name != run_id:
        raise InvalidRunIdError("run ID must be exactly one relative path component")
    return run_id


def ensure_path_confined(root: Path, candidate: Path, *, allow_root: bool = False) -> Path:
    """Resolve ``candidate`` and reject traversal or symlink escapes from ``root``.

    ``Path.resolve(strict=False)`` resolves every existing symlink in the path while
    still allowing a not-yet-created final component.  Returning the resolved path
    also prevents callers from subsequently following an already-known symlink path.
    """

    try:
        resolved_root = root.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise PathConfinementError(f"cannot resolve confinement root {root}: {exc}") from exc
    if not resolved_root.is_absolute():  # pragma: no cover - resolve always makes it absolute
        raise PathConfinementError(f"confinement root is not absolute: {root}")
    unresolved = candidate.expanduser()
    if not unresolved.is_absolute():
        unresolved = resolved_root / unresolved
    try:
        resolved_candidate = unresolved.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise PathConfinementError(f"cannot safely resolve path {candidate}: {exc}") from exc
    if resolved_candidate == resolved_root:
        if allow_root:
            return resolved_candidate
        raise PathConfinementError("operation may not target the confinement root itself")
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise PathConfinementError(
            f"path escapes confined workspace {resolved_root}: {candidate}"
        ) from exc
    return resolved_candidate


def confined_path(root: Path, *relative_parts: str | Path) -> Path:
    """Build a confined path from explicitly relative path components."""

    if not relative_parts:
        raise PathConfinementError("at least one relative path component is required")
    candidate = root
    for part in relative_parts:
        component = Path(part)
        if component.is_absolute():
            raise PathConfinementError(f"absolute paths are not allowed: {part}")
        candidate /= component
    return ensure_path_confined(root, candidate)


# A discoverable alias for callers/tests phrasing the operation as an assertion.
ensure_confined_path = ensure_path_confined


def create_artifact_directories(run_root: Path) -> None:
    """Create every concrete directory required by the artifact contract."""

    resolved_root = run_root.resolve(strict=True)
    for relative in ARTIFACT_DIRECTORIES:
        path = confined_path(resolved_root, relative)
        path.mkdir(parents=True, exist_ok=False)


def create_run_root(
    project_root: Path,
    run_name: str | None = None,
    *,
    problem_name: str | None = None,
    run_id: str | None = None,
    now: datetime | None = None,
) -> Path:
    """Create and return a new artifact-contract workspace.

    An existing or symlinked run target is always rejected.  The ``.ascend`` path
    itself must resolve under the project root, preventing a pre-created symlink from
    redirecting artifacts elsewhere.
    """

    root = project_root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise WorkspaceError(f"project root is not a directory: {project_root}")
    selected_id = (
        validate_run_id(run_id)
        if run_id is not None
        else generate_run_id(run_name, problem_name=problem_name, now=now)
    )

    ascend_root = ensure_path_confined(root, root / ".ascend")
    runs_root = ensure_path_confined(root, ascend_root / "runs")
    ascend_root.mkdir(exist_ok=True)
    runs_root.mkdir(exist_ok=True)

    run_root = ensure_path_confined(runs_root, runs_root / selected_id)
    try:
        run_root.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise WorkspaceError(f"run workspace already exists: {run_root}") from exc
    create_artifact_directories(run_root)
    return run_root


def find_run_root(project_root: Path, run_id: str) -> Path:
    """Resolve an existing run without allowing a run-ID or symlink escape."""

    validate_run_id(run_id)
    root = project_root.expanduser().resolve(strict=True)
    runs_root = ensure_path_confined(root, root / ".ascend" / "runs")
    run_root = ensure_path_confined(runs_root, runs_root / run_id)
    if not run_root.is_dir():
        raise WorkspaceError(f"run does not exist: {run_id}")
    return run_root


def latest_run_root(project_root: Path) -> Path | None:
    """Return the latest valid run workspace by its embedded UTC timestamp, if any."""

    root = project_root.expanduser().resolve(strict=True)
    runs_root = ensure_path_confined(root, root / ".ascend" / "runs")
    if not runs_root.is_dir():
        return None
    valid: list[Path] = []
    for candidate in runs_root.iterdir():
        try:
            validate_run_id(candidate.name)
            resolved = ensure_path_confined(runs_root, candidate)
        except WorkspaceError:
            continue
        if resolved.is_dir():
            valid.append(resolved)
    return max(valid, key=lambda item: (_run_id_timestamp(item.name), item.name), default=None)


def _run_id_timestamp(run_id: str) -> str:
    """Extract the sortable UTC timestamp from either supported run-ID format."""

    for pattern in (_LEGACY_RUN_ID_PATTERN, _PROBLEM_RUN_ID_PATTERN):
        match = pattern.fullmatch(run_id)
        if match is not None:
            return match.group("timestamp")
    raise InvalidRunIdError(f"invalid run ID: {run_id}")


def _json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (Path, date, datetime)):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"object of type {type(value).__name__} is not JSON serializable")


def _fsync_directory(directory: Path) -> None:
    """Best-effort directory sync for durable rename semantics."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
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


def atomic_write_bytes(
    path: Path,
    contents: bytes,
    *,
    confinement_root: Path | None = None,
    mode: int = 0o600,
) -> Path:
    """Durably replace ``path`` with ``contents`` using a sibling temporary file."""

    target = (
        ensure_path_confined(confinement_root, path)
        if confinement_root is not None
        else path.expanduser().resolve(strict=False)
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    if confinement_root is not None:
        ensure_path_confined(confinement_root, target.parent, allow_root=True)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return target


def atomic_write_text(
    path: Path,
    contents: str,
    *,
    confinement_root: Path | None = None,
    encoding: str = "utf-8",
    mode: int = 0o600,
) -> Path:
    """Atomically write text with deterministic UTF-8 defaults."""

    return atomic_write_bytes(
        path, contents.encode(encoding), confinement_root=confinement_root, mode=mode
    )


def atomic_write_json(
    path: Path,
    value: Any,
    *,
    confinement_root: Path | None = None,
    mode: int = 0o600,
) -> Path:
    """Atomically write stable, human-readable JSON terminated by a newline."""

    contents = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        default=_json_default,
    )
    return atomic_write_text(path, f"{contents}\n", confinement_root=confinement_root, mode=mode)


def sha256_bytes(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def sha256_text(contents: str, *, encoding: str = "utf-8") -> str:
    return sha256_bytes(contents.encode(encoding))


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Hash a regular file without loading it all into memory."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def relative_artifact_path(run_root: Path, artifact: Path) -> str:
    """Return a portable POSIX artifact path after confinement validation."""

    resolved_root = run_root.resolve(strict=True)
    resolved_artifact = ensure_path_confined(resolved_root, artifact)
    return resolved_artifact.relative_to(resolved_root).as_posix()


__all__ = [
    "ARTIFACT_DIRECTORIES",
    "PROJECT_MARKERS",
    "InvalidRunIdError",
    "PathConfinementError",
    "RunLock",
    "RunLockError",
    "RunLockHeldError",
    "WorkspaceError",
    "atomic_write_bytes",
    "atomic_write_json",
    "atomic_write_text",
    "confined_path",
    "create_artifact_directories",
    "create_run_root",
    "discover_project_root",
    "ensure_confined_path",
    "ensure_path_confined",
    "find_run_root",
    "generate_run_id",
    "latest_run_root",
    "relative_artifact_path",
    "sha256_bytes",
    "sha256_file",
    "sha256_text",
    "validate_run_id",
]
