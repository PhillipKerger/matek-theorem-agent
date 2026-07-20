from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, BinaryIO

from pydantic import BaseModel, ConfigDict, Field


class StageError(RuntimeError):
    """Base class for a truthful, user-actionable stage failure."""


class StageValidationError(StageError, ValueError):
    """Raised when an input or model response violates a workflow contract."""


class StageGateError(StageError):
    """Raised when a downstream stage is invoked before its gate has passed."""


class ArtifactManifest(BaseModel):
    """Absolute artifact paths and the digest of every artifact that exists."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    paths: dict[str, Path] = Field(default_factory=dict)
    sha256: dict[str, str] = Field(default_factory=dict)


class CallManifest(BaseModel):
    """Traceable paid-call metadata without private model reasoning."""

    model_calls: int = 0
    codex_calls: int = 0
    response_ids: list[str] = Field(default_factory=list)


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_text(content: str) -> str:
    return sha256_bytes(content.encode("utf-8"))


def sha256_file(path: Path) -> str:
    """Hash a regular file without following a final-component symlink."""

    digest = hashlib.sha256()
    with _open_regular_file(path) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_regular_bytes(path: Path) -> bytes:
    """Read a regular file without following a final-component symlink."""

    with _open_regular_file(path) as stream:
        return stream.read()


def read_regular_text(
    path: Path,
    *,
    encoding: str = "utf-8",
    errors: str = "strict",
) -> str:
    return read_regular_bytes(path).decode(encoding, errors=errors)


def _open_regular_file(path: Path) -> BinaryIO:
    try:
        entry = os.lstat(path)
    except OSError as exc:
        raise StageValidationError(f"Cannot inspect artifact file {path}: {exc}") from exc
    if not stat.S_ISREG(entry.st_mode):
        raise StageValidationError(f"Artifact file is not a regular file: {path}")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise StageValidationError(f"Cannot open artifact file safely {path}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise StageValidationError(f"Artifact file is not a regular file: {path}")
        if (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino):
            raise StageValidationError(f"Artifact file changed while it was opened: {path}")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def ensure_stage_directory(path: Path) -> Path:
    """Create and return a resolved stage directory.

    Callers pass the final stage directory, not a run root.  For example, prompt
    artifacts go directly below ``prompts_dir``.
    """

    absolute = Path(os.path.abspath(path))
    _reject_symlink_ancestors(absolute)
    absolute.mkdir(parents=True, exist_ok=True)
    _reject_symlink_ancestors(absolute)
    try:
        entry = os.lstat(absolute)
    except OSError as exc:  # pragma: no cover - guarded by mkdir in normal operation
        raise StageValidationError(f"Cannot inspect stage artifact directory: {absolute}") from exc
    if not stat.S_ISDIR(entry.st_mode):
        raise StageValidationError(f"Stage artifact path is not a directory: {absolute}")
    return absolute


def _reject_symlink_ancestors(path: Path) -> None:
    """Reject aliases in a stage path before filesystem operations traverse them."""

    for candidate in reversed((path, *path.parents)):
        try:
            entry = os.lstat(candidate)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise StageValidationError(
                f"Cannot inspect stage path component {candidate}: {exc}"
            ) from exc
        if stat.S_ISLNK(entry.st_mode):
            raise StageValidationError(f"Stage path must not contain symlinks: {candidate}")
        if candidate != path and not stat.S_ISDIR(entry.st_mode):
            raise StageValidationError(f"Stage path ancestor is not a directory: {candidate}")


def atomic_write_bytes(path: Path, content: bytes) -> Path:
    """Atomically replace *path* with *content* using a sibling temporary file."""

    parent = ensure_stage_directory(path.parent)
    target = parent / path.name
    if not path.name or path.name in {".", ".."}:
        raise StageValidationError(f"Artifact path escapes its stage directory: {path}")
    _reject_unsafe_write_target(target)

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _reject_unsafe_write_target(target)
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def _reject_unsafe_write_target(target: Path) -> None:
    try:
        entry = os.lstat(target)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise StageValidationError(f"Cannot inspect artifact destination {target}: {exc}") from exc
    if stat.S_ISLNK(entry.st_mode):
        raise StageValidationError(f"Artifact destination must not be a symlink: {target}")
    if not stat.S_ISREG(entry.st_mode):
        raise StageValidationError(
            f"Artifact destination must be a regular file when it exists: {target}"
        )


def atomic_write_text(path: Path, content: str) -> Path:
    return atomic_write_bytes(path, content.encode("utf-8"))


def atomic_write_json(path: Path, value: BaseModel | Any) -> Path:
    return atomic_write_bytes(path, canonical_json_bytes(value))


def canonical_json_bytes(value: BaseModel | Any) -> bytes:
    """Render JSON exactly as stage artifacts do, for stable gate hashes."""

    if isinstance(value, BaseModel):
        payload: Any = value.model_dump(mode="json")
    else:
        payload = value
    rendered = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    return rendered.encode("utf-8")


def sha256_json(value: BaseModel | Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def build_artifact_manifest(paths: dict[str, Path]) -> ArtifactManifest:
    absolute = {name: Path(os.path.abspath(path)) for name, path in paths.items()}
    hashes: dict[str, str] = {}
    for name, path in absolute.items():
        try:
            os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise StageValidationError(f"Cannot inspect artifact {path}: {exc}") from exc
        hashes[name] = sha256_file(path)
    return ArtifactManifest(paths=absolute, sha256=hashes)


def project_resource(relative: str) -> Path:
    """Resolve a resource in a source checkout.

    Packaged applications should inject prompt/template paths explicitly.  The source-tree
    fallback keeps stage functions independently testable without global configuration.
    """

    candidate = Path(__file__).resolve().parents[3] / "resources" / relative
    if not candidate.is_file():
        raise StageValidationError(
            f"Required resource is missing: {candidate}. Pass an explicit resource path."
        )
    return candidate
