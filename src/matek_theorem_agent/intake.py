"""Problem ingestion and creation of a complete resumable run checkpoint."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from .config import AppConfig, config_as_toml
from .models import RunState, ScientificStatus, StageName, StageStatus, new_run_state
from .redaction import SecretRedactor, redact_text, sanitized_environment
from .state import save_state_atomic
from .workspace import (
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    create_run_root,
    relative_artifact_path,
    sha256_file,
)


class IntakeError(ValueError):
    """Raised when a problem cannot safely become a run."""


class IntakeResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    state: RunState
    run_root: Path
    problem_text: str
    artifacts: dict[str, Path]


def normalize_problem_text(text: str) -> str:
    """Normalize only line endings and terminal whitespace, preserving mathematical Unicode."""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise IntakeError("problem file is empty or contains only whitespace")
    return f"{normalized}\n"


def _version(argv: Sequence[str]) -> str | None:
    if shutil.which(argv[0]) is None:
        return None
    try:
        result = subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            env=sanitized_environment(),
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = redact_text((result.stdout or result.stderr).strip())
    return output.splitlines()[0] if output else f"exit {result.returncode}"


def environment_snapshot(project_root: Path) -> dict[str, Any]:
    """Return an allowlisted environment description with no variable values."""

    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
        "project_root": str(project_root),
        "tools": {
            "git": _version(("git", "--version")),
            "codex": _version(("codex", "--version")),
            "lean": _version(("lean", "--version")),
            "lake": _version(("lake", "--version")),
            "latexmk": _version(("latexmk", "--version")),
        },
    }


def _safe_invocation(arguments: Mapping[str, Any]) -> dict[str, Any]:
    prepared: dict[str, Any] = {}
    for key, value in arguments.items():
        if isinstance(value, Path):
            prepared[key] = str(value)
        elif isinstance(value, (str, int, float, bool, list, dict, type(None))):
            prepared[key] = value
        else:
            prepared[key] = str(value)
    redacted = SecretRedactor().redact_data(prepared)
    if not isinstance(redacted, dict):  # pragma: no cover - mapping input always returns a dict
        raise IntakeError("invocation redaction did not preserve its mapping shape")
    return {str(key): value for key, value in redacted.items()}


def ingest_problem(
    *,
    problem_file: Path,
    project_root: Path,
    config: AppConfig,
    invocation: Mapping[str, Any],
    run_name: str | None = None,
    run_id: str | None = None,
    now: datetime | None = None,
    snapshot: Mapping[str, Any] | None = None,
) -> IntakeResult:
    """Create the run and atomically persist all Stage 0 artifacts and state."""

    source = problem_file.expanduser().resolve(strict=True)
    if not source.is_file():
        raise IntakeError(f"problem path is not a regular file: {problem_file}")
    if source.suffix.lower() not in {".md", ".txt"}:
        raise IntakeError("problem file must use a .md or .txt extension")
    original = source.read_bytes()
    try:
        decoded = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IntakeError(f"problem file is not valid UTF-8: {source}") from exc
    environment_secrets = [
        value
        for key, value in os.environ.items()
        if value
        and any(
            marker in key.upper()
            for marker in ("API_KEY", "ACCESS_TOKEN", "AUTH_TOKEN", "SECRET", "PASSWORD")
        )
    ]
    redaction = SecretRedactor(environment_secrets).redact_text_result(decoded)
    safe_original = redaction.value.encode("utf-8") if redaction.changed else original
    normalized = normalize_problem_text(redaction.value)

    root = project_root.expanduser().resolve(strict=True)
    timestamp = now or datetime.now(UTC)
    config_snapshot = config_as_toml(config)
    run_root = create_run_root(
        root,
        run_name,
        problem_name=source.stem,
        run_id=run_id,
        now=timestamp,
    )
    backend_provider = config.backend.provider
    backend_manifest: dict[str, Any] = {
        "schema_version": 1,
        "provider": backend_provider,
        "display_name": ("Codex CLI" if backend_provider == "codex" else "OpenAI Responses API"),
        "automatic_fallback": False,
        "authentication_class": "unverified",
        "backend_version": None,
        "model_requested": (
            {
                "research_coordinator": config.codex.model,
                "research_worker": config.codex.model,
            }
            if backend_provider == "codex"
            else {
                "prompt_compiler": config.models.prompt_compiler.model,
                "research_coordinator": config.models.research_coordinator.model,
                "research_worker": config.models.research_worker.model,
                "audit": config.models.audit.model,
                "manuscript": config.models.manuscript.model,
            }
        ),
        "reasoning_effort_requested": (
            {
                "research_coordinator": config.codex.research_coordinator_effort,
                "research_worker": config.codex.research_worker_effort,
                "audit": config.codex.audit_effort,
                "manuscript": config.codex.manuscript_effort,
                "formalization": config.codex.formalization_effort,
            }
            if backend_provider == "codex"
            else "per-stage API settings"
        ),
        "web_search_policy": (
            "enabled only for stages whose model settings require it"
            if config.web_search_enabled
            else "disabled for all stages by configuration"
        ),
    }
    # The immutable intake snapshot remains at input/config.resolved.toml for legacy
    # readers. The effective copy is the resume source and may change only during an
    # explicitly confirmed backend migration, which is recorded in run provenance.
    atomic_write_text(
        run_root / "config" / "effective_config.toml",
        config_snapshot,
        confinement_root=run_root,
    )
    atomic_write_json(
        run_root / "config" / "backend_manifest.json",
        backend_manifest,
        confinement_root=run_root,
    )
    artifacts = {
        "problem_original": atomic_write_bytes(
            run_root / "input" / "problem.original", safe_original, confinement_root=run_root
        ),
        "problem_normalized": atomic_write_text(
            run_root / "input" / "problem.md", normalized, confinement_root=run_root
        ),
        "invocation": atomic_write_json(
            run_root / "input" / "invocation.json",
            {
                "problem_file": str(source),
                "timestamp": timestamp.astimezone(UTC).isoformat(),
                "argv": list(sys.argv),
                "arguments": _safe_invocation(invocation),
            },
            confinement_root=run_root,
        ),
        "config": atomic_write_text(
            run_root / "input" / "config.resolved.toml",
            config_snapshot,
            confinement_root=run_root,
        ),
        "environment": atomic_write_json(
            run_root / "input" / "environment.json",
            dict(snapshot) if snapshot is not None else environment_snapshot(root),
            confinement_root=run_root,
        ),
    }

    state = new_run_state(
        run_root.name,
        root,
        run_root,
        now=timestamp,
        metadata={
            "problem_original_sha256": sha256_file(artifacts["problem_original"]),
            "problem_normalized_sha256": sha256_file(artifacts["problem_normalized"]),
            "research_status": ScientificStatus.RECEIVED.value,
            "manuscript_status": "NOT_STARTED",
            "lean_status": "NOT_STARTED",
            "unresolved_obligations": [],
            "configuration_summary": {
                "model_execution_backend": backend_provider,
                "backend_display_name": backend_manifest["display_name"],
                "authentication_class": backend_manifest["authentication_class"],
                "automatic_fallback": False,
                "prompt_compiler_model": config.models.prompt_compiler.model,
                "research_coordinator_model": (
                    config.codex.model
                    if backend_provider == "codex"
                    else config.models.research_coordinator.model
                ),
                "research_coordinator_effort": (
                    config.codex.research_coordinator_effort
                    if backend_provider == "codex"
                    else config.models.research_coordinator.reasoning_effort
                ),
                "research_worker_model": (
                    config.codex.model
                    if backend_provider == "codex"
                    else config.models.research_worker.model
                ),
                "research_worker_effort": (
                    config.codex.research_worker_effort
                    if backend_provider == "codex"
                    else config.models.research_worker.reasoning_effort
                ),
                "minimum_initial_agents": config.research.minimum_initial_agents,
                "maximum_pending_assignments": (config.research.maximum_pending_assignments),
                "maximum_coordinator_decisions": (config.research.maximum_coordinator_decisions),
                "maximum_concurrent_agents": config.research.maximum_concurrent_agents,
                "knowledge_graph_collection": ".matek/knowledge",
                "graph_maximum_context_nodes": config.graph.maximum_context_nodes,
                "graph_maximum_context_characters": config.graph.maximum_context_characters,
                "maximum_cost_usd": config.limits.maximum_cost_usd,
                "lean_enabled": config.lean.enabled,
            },
            "backend": backend_manifest,
            "backend_history": [],
            "model_cache_schema_version": 2,
            "input_redactions": {
                "replacements": redaction.replacements,
                "categories": list(redaction.categories),
            },
        },
    )
    record = state.stages[StageName.INTAKE]
    record.status = StageStatus.SUCCEEDED
    record.attempts = 1
    record.started_at = timestamp
    record.completed_at = timestamp
    record.updated_at = timestamp
    record.artifacts = {
        relative_artifact_path(run_root, path): sha256_file(path) for path in artifacts.values()
    }
    state.artifact_hashes.update(record.artifacts)
    state.updated_at = timestamp
    save_state_atomic(state, run_root / "state.json")
    return IntakeResult(
        state=state,
        run_root=run_root,
        problem_text=normalized,
        artifacts=artifacts,
    )
