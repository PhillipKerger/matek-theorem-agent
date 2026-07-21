"""Resumable application service that owns ASCEND's stage gates."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .accounting import AccountingModelClient
from .budget import BudgetExceeded, BudgetTracker, UsageRecord
from .codex_client import CodexClient
from .config import AppConfig, ModelSettings, config_as_toml, load_config, merge_config
from .execution.base import ExecutionBackend
from .intake import ingest_problem
from .logging import RunLogger, load_usage_journal_strict
from .models import RunState, ScientificStatus, StageName, StageStatus
from .openai_client import ModelClient, ModelRequest
from .progress import Ascension, ProgressReporter, no_progress
from .redaction import redact_data, redact_text
from .reporting import (
    ReportArtifacts,
    ReportNarrative,
    assert_report_certificate_inventory,
    build_final_report,
    load_final_report,
    write_final_report,
)
from .resources import resource_path, resource_paths
from .source_provenance import (
    BoundedHttpSourceVerifier,
    IdentifierVerifier,
    WebDisabledSourceVerifier,
)
from .stages.compile_prompt import (
    EXPECTED_FRAMEWORK_SHA256,
    CompiledProblem,
    PromptCompilationResult,
    compile_prompt,
)
from .stages.lean import (
    AlignmentStatus,
    LeanOutcome,
    LeanPipelineResult,
    LeanWorkflowSettings,
    run_lean_pipeline,
)
from .stages.manuscript import (
    ManuscriptOutcome,
    ManuscriptResult,
    generate_manuscript,
    resume_manuscript_bibliography,
)
from .stages.research import (
    ResearchOutcome,
    ResearchResult,
    ResearchWorkflowSettings,
    run_adaptive_research,
)
from .state import (
    StateCorruptionError,
    StateStore,
    assert_recorded_artifacts,
    fail_stage,
    first_incomplete_stage,
    interrupt_stage,
    invalidate_from,
    prepare_for_resume,
    record_artifact_file,
    record_paid_call,
    skip_stage,
    start_stage,
    succeed_stage,
)
from .workspace import (
    RunLock,
    atomic_write_json,
    atomic_write_text,
    ensure_path_confined,
    find_run_root,
    latest_run_root,
    sha256_file,
)


class WorkflowError(RuntimeError):
    """Base class for an application-level execution failure."""


class RunNotFoundError(WorkflowError):
    pass


class WorkflowOptions(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_name: str | None = None
    framework_path: Path | None = None
    no_lean: bool = False
    research_only: bool = False
    allow_project_edits: bool = False
    invocation: dict[str, Any] = Field(default_factory=dict)


LEAN_CONSENT_TIMEOUT_SECONDS = 5 * 60


class LeanConsentOutcome(StrEnum):
    """Durable result of the manuscript-to-Lean user checkpoint."""

    USER_APPROVED = "user_approved"
    USER_DECLINED = "user_declined"
    TIMED_OUT = "timed_out_auto_proceed"
    NON_INTERACTIVE = "non_interactive_auto_proceed"
    AUTOMATION_DEFAULT = "automation_default_auto_proceed"
    PROMPT_ERROR = "prompt_error_auto_proceed"

    @property
    def proceed(self) -> bool:
        return self is not LeanConsentOutcome.USER_DECLINED


@dataclass(frozen=True)
class LeanConsentRequest:
    run_id: str
    manuscript_path: Path
    timeout_seconds: int = LEAN_CONSENT_TIMEOUT_SECONDS


LeanConsentHandler = Callable[[LeanConsentRequest], Awaitable[LeanConsentOutcome]]


async def automatic_lean_consent(_: LeanConsentRequest) -> LeanConsentOutcome:
    """Noninteractive library default; the CLI injects the actual terminal prompt."""

    return LeanConsentOutcome.AUTOMATION_DEFAULT


@dataclass(frozen=True)
class WorkflowDependencies:
    model_client: ModelClient
    execution_backend: ExecutionBackend
    codex_client: CodexClient
    source_verifier: IdentifierVerifier | None = None
    progress: ProgressReporter = no_progress
    lean_consent: LeanConsentHandler = automatic_lean_consent


class WorkflowResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    state: RunState
    report: ReportArtifacts


def resolve_run_root(project_root: Path, run_id: str | None = None) -> Path:
    if run_id is not None:
        return find_run_root(project_root, run_id)
    latest = latest_run_root(project_root)
    if latest is None:
        raise RunNotFoundError(f"no ASCEND runs found beneath {project_root}")
    return latest


def _usage_records(run_root: Path) -> list[UsageRecord]:
    return load_usage_journal_strict(run_root / "logs" / "usage.jsonl")


def _model_cache_generation(state: RunState) -> int:
    value = state.metadata.get("model_cache_generation", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StateCorruptionError("model_cache_generation must be a nonnegative integer")
    return value


def _model_cache_namespace(state: RunState) -> str:
    generation = _model_cache_generation(state)
    if state.metadata.get("model_cache_schema_version") != 2:
        # Preserve cache compatibility for v0.1 runs created before provider
        # provenance became part of the replay identity.
        return f"generation-{generation}"
    backend = state.metadata.get("backend", {})
    provider = backend.get("provider") if isinstance(backend, dict) else None
    if provider not in {"codex", "api"}:
        raise StateCorruptionError("provider-scoped model cache has invalid backend metadata")
    return f"{provider}-generation-{generation}"


def _prompt_validation_generation(state: RunState) -> int:
    value = state.metadata.get("prompt_validation_generation", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StateCorruptionError("prompt_validation_generation must be a nonnegative integer")
    return value


def _sync_paid_calls_from_usage(state: RunState, records: list[UsageRecord]) -> bool:
    """Recover durable paid response IDs into state after an interrupted stage."""

    changed = False
    for record in records:
        if record.response_id is None:
            continue
        if record.stage is None:
            raise StateCorruptionError(
                f"paid usage {record.response_id!r} has no owning workflow stage"
            )
        try:
            stage = StageName(record.stage)
        except ValueError as exc:
            raise StateCorruptionError(
                f"paid usage {record.response_id!r} has unknown stage {record.stage!r}"
            ) from exc
        changed = record_paid_call(state, stage, record.response_id) or changed
    return changed


class WorkflowRunner:
    """Coordinate stage services while preserving every successful checkpoint."""

    def __init__(self, config: AppConfig, dependencies: WorkflowDependencies) -> None:
        self.config = config
        self.dependencies = dependencies

    def _source_verifier(self, run_root: Path) -> IdentifierVerifier:
        if not self.config.web_search_enabled:
            return WebDisabledSourceVerifier()
        if self.dependencies.source_verifier is not None:
            return self.dependencies.source_verifier
        cache_path = run_root / "prompts" / "source_verification_cache.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        return BoundedHttpSourceVerifier(cache_path=cache_path)

    def _budget_tracker(
        self,
        usage_records: list[UsageRecord],
        *,
        prior_elapsed_seconds: float,
        include_unscoped_usage: bool = True,
    ) -> BudgetTracker:
        provider = self.config.backend.provider
        scoped_usage = [
            record
            for record in usage_records
            if record.provider == provider or (include_unscoped_usage and record.provider is None)
        ]
        if self.config.backend.provider == "api":
            return BudgetTracker(
                self.config.limits,
                scoped_usage,
                prior_elapsed_seconds=prior_elapsed_seconds,
            )

        # Subscription/credit-backed Codex usage has no authoritative per-call USD
        # conversion. Preserve observed unknown-cost usage without applying the API
        # dollar gate, while enforcing conservative call/thread and wall-clock limits.
        codex_limits = self.config.codex.limits
        configured_wall_limits = [
            limit
            for limit in (
                self.config.limits.maximum_wall_clock_hours,
                (
                    codex_limits.max_wall_clock_minutes / 60
                    if codex_limits.max_wall_clock_minutes is not None
                    else None
                ),
            )
            if limit is not None
        ]
        effective_limits = self.config.limits.model_copy(
            update={
                "maximum_wall_clock_hours": (
                    min(configured_wall_limits) if configured_wall_limits else None
                )
            }
        )
        return BudgetTracker(
            effective_limits,
            scoped_usage,
            prior_elapsed_seconds=prior_elapsed_seconds,
            maximum_calls=min(
                codex_limits.max_agent_calls,
                codex_limits.max_codex_threads,
            ),
            enforce_cost_budget=False,
        )

    def _sync_backend_metadata(self, state: RunState) -> None:
        """Persist current nonsecret provider provenance without trusting the adapter."""

        current = state.metadata.get("backend", {})
        if not isinstance(current, dict):
            raise StateCorruptionError("backend metadata must be an object")
        manifest_provider = getattr(self.dependencies.model_client, "backend_manifest", None)
        observed: Any = manifest_provider() if callable(manifest_provider) else {}
        if observed is None:
            observed = {}
        if not isinstance(observed, Mapping):
            raise StateCorruptionError("model backend manifest must be an object")
        safe_observed = redact_data(dict(observed))
        if not isinstance(safe_observed, dict):  # pragma: no cover - mapping stays mapping
            raise StateCorruptionError("backend manifest redaction changed its shape")
        for pending_key in (
            "authentication_class",
            "backend_version",
            "model_requested",
            "model_observed",
            "reasoning_effort_requested",
            "reasoning_effort_actual",
        ):
            if safe_observed.get(pending_key) is None:
                safe_observed.pop(pending_key, None)
        configured_provider = self.config.backend.provider
        observed_provider = safe_observed.get("provider")
        if observed_provider is not None and observed_provider != configured_provider:
            raise WorkflowError(
                "injected model client provider does not match the frozen run backend: "
                f"configured={configured_provider}, injected={observed_provider}"
            )
        # A newly constructed adapter has not observed authentication, version, model,
        # or usage yet. Do not let those ``None`` placeholders erase the frozen
        # configuration recorded by intake. Concrete observations replace it after a
        # successful call.
        current.update({key: value for key, value in safe_observed.items() if value is not None})
        provider = configured_provider
        current.update(
            {
                "schema_version": 1,
                "provider": provider,
                "display_name": ("Codex CLI" if provider == "codex" else "OpenAI Responses API"),
                "automatic_fallback": False,
            }
        )
        state.metadata["backend"] = current
        summary = state.metadata.get("configuration_summary", {})
        if not isinstance(summary, dict):
            raise StateCorruptionError("configuration_summary must be an object")
        summary.update(
            {
                "model_execution_backend": provider,
                "backend_display_name": current["display_name"],
                "authentication_class": current.get("authentication_class", "unverified"),
                "backend_version": current.get("backend_version"),
                "configured_model": current.get("model_requested"),
                "configured_reasoning_effort": current.get("reasoning_effort_requested"),
                "live_web_search": (
                    current.get("web_search_enabled")
                    if current.get("completed_calls", 0)
                    else current.get("web_search_policy", current.get("web_search_enabled"))
                ),
                "automatic_fallback": False,
            }
        )
        state.metadata["configuration_summary"] = summary
        manifest_path = state.run_root / "config" / "backend_manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(manifest_path, current, confinement_root=state.run_root)

    def _model_settings(self, category: str) -> ModelSettings:
        """Resolve backend-aware effort while preserving every other stage setting."""

        base = {
            "prompt": self.config.models.prompt_compiler,
            "research": self.config.models.research,
            "audit": self.config.models.audit,
            "manuscript": self.config.models.manuscript,
        }.get(category)
        if base is None:
            raise ValueError(f"unknown model settings category: {category}")
        if self.config.backend.provider == "api":
            return base
        effort = {
            "prompt": self.config.codex.research_effort,
            "research": self.config.codex.research_effort,
            "audit": self.config.codex.audit_effort,
            "manuscript": self.config.codex.manuscript_effort,
        }[category]
        return base.model_copy(update={"reasoning_effort": effort})

    async def run_new(
        self,
        problem_file: Path,
        project_root: Path,
        *,
        options: WorkflowOptions | None = None,
        environment_snapshot: Mapping[str, Any] | None = None,
    ) -> WorkflowResult:
        selected = options or WorkflowOptions()
        self.dependencies.progress(Ascension.FETCH_PROBLEM, "Fetching problem.")
        intake = ingest_problem(
            problem_file=problem_file,
            project_root=project_root,
            config=self.config,
            invocation={**selected.invocation, **selected.model_dump(mode="python")},
            run_name=selected.run_name,
            snapshot=environment_snapshot,
        )
        with RunLock(intake.run_root):
            intake.state.metadata.update(
                {
                    "research_only": selected.research_only,
                    "no_lean": selected.no_lean,
                    "allow_project_edits": selected.allow_project_edits,
                    "custom_framework_path": (
                        str(selected.framework_path.resolve()) if selected.framework_path else None
                    ),
                }
            )
            self._sync_backend_metadata(intake.state)
            StateStore(intake.run_root).save(intake.state)
            return await self._execute(intake.state, selected)

    async def resume(
        self,
        project_root: Path,
        *,
        run_id: str | None = None,
        force_stage: StageName | None = None,
        config_overrides: Mapping[str, Any] | None = None,
    ) -> WorkflowResult:
        run_root = resolve_run_root(project_root, run_id)
        with RunLock(run_root):
            return await self._resume_locked(
                run_root,
                force_stage=force_stage,
                config_overrides=config_overrides,
            )

    async def _resume_locked(
        self,
        run_root: Path,
        *,
        force_stage: StageName | None,
        config_overrides: Mapping[str, Any] | None,
    ) -> WorkflowResult:
        store = StateStore(run_root)
        state = store.load()
        if state.stages[StageName.REPORT].status is StageStatus.SUCCEEDED:
            assert_report_certificate_inventory(run_root)
        if force_stage is not None:
            if force_stage is StageName.INTAKE:
                raise WorkflowError("intake is immutable; start a new run to replace the input")
            if force_stage is StageName.BIBLIOGRAPHY:
                previous_path = run_root / "manuscript" / "result.json"
                if previous_path.is_file():
                    previous = ManuscriptResult.model_validate_json(
                        previous_path.read_text(encoding="utf-8")
                    )
                    if previous.outcome is ManuscriptOutcome.BIBLIOGRAPHY_REJECTED:
                        state.metadata["resume_bibliography_correction"] = True
            composite_boundary = {
                StageName.RESEARCH_AUDIT: StageName.RESEARCH,
                StageName.BIBLIOGRAPHY: StageName.MANUSCRIPT,
                StageName.LEAN_ALIGNMENT: StageName.LEAN_FEASIBILITY,
                StageName.LEAN_FORMALIZATION: StageName.LEAN_FEASIBILITY,
                StageName.LEAN_VERIFICATION: StageName.LEAN_FEASIBILITY,
            }.get(force_stage, force_stage)
            invalidate_from(state, composite_boundary, "explicit --force-stage request")
            if force_stage in {
                StageName.MANUSCRIPT,
                StageName.BIBLIOGRAPHY,
                StageName.LEAN_FEASIBILITY,
                StageName.LEAN_ALIGNMENT,
                StageName.LEAN_FORMALIZATION,
                StageName.LEAN_VERIFICATION,
            }:
                self._archive_lean_consent(state, reason="explicit --force-stage request")
            if force_stage is StageName.PROMPT_COMPILATION:
                # Preserve the expensive successful compiler/source calls. Only the bounded
                # post-compilation repair gets a fresh identity for this explicit recovery.
                state.metadata["prompt_validation_generation"] = (
                    _prompt_validation_generation(state) + 1
                )
            else:
                state.metadata["model_cache_generation"] = _model_cache_generation(state) + 1
            assert_recorded_artifacts(state)
        else:
            assert_recorded_artifacts(state)
            prepare_for_resume(state)
            incomplete = first_incomplete_stage(state)
            if incomplete is None:
                # A completed resume is a byte-for-byte no-op: do not rewrite state,
                # logs, reports, or configuration snapshots.
                return WorkflowResult(state=state, report=load_final_report(run_root))
            if incomplete is StageName.BIBLIOGRAPHY:
                # The bibliography correction cycle and manuscript build are one
                # bounded service in v0.1. Preserve its prior call IDs/diagnostics,
                # but reset the composite checkpoint before retrying the failed gate.
                state.metadata["resume_bibliography_correction"] = True
                invalidate_from(
                    state,
                    StageName.MANUSCRIPT,
                    "resume bibliography correction cycle from preserved draft",
                )
                incomplete = StageName.MANUSCRIPT
            if incomplete in {
                StageName.LEAN_ALIGNMENT,
                StageName.LEAN_FORMALIZATION,
                StageName.LEAN_VERIFICATION,
            }:
                # The v0.1 Lean service is a single bounded pipeline. Preserve all prior
                # diagnostics/call IDs but invalidate its mutable generated sources before
                # another attempt so immutable hashes cannot be silently overwritten.
                invalidate_from(
                    state,
                    StageName.LEAN_FEASIBILITY,
                    "resume bounded Lean pipeline from preserved diagnostics",
                )
                incomplete = StageName.LEAN_FEASIBILITY
            if (
                incomplete is not None
                and incomplete is not StageName.REPORT
                and state.stages[StageName.REPORT].status is StageStatus.SUCCEEDED
            ):
                invalidate_from(state, StageName.REPORT, "upstream stage will be resumed")
        effective_config_path = run_root / "config" / "effective_config.toml"
        snapshot_path = (
            effective_config_path
            if effective_config_path.is_file()
            else run_root / "input" / "config.resolved.toml"
        )
        snapshot_config = load_config(
            snapshot_path,
            project_root=state.project_root,
            env={},
        )
        previous_provider = snapshot_config.backend.provider
        self.config = merge_config(snapshot_config, config_overrides)
        selected_provider = self.config.backend.provider
        if selected_provider != previous_provider:
            history = state.metadata.setdefault("backend_history", [])
            if not isinstance(history, list):
                raise StateCorruptionError("backend_history must be a list")
            history.append(
                {
                    "from": previous_provider,
                    "to": selected_provider,
                    "changed_at": datetime.now(UTC).isoformat(),
                    "reason": "explicit resume backend migration",
                    "provenance_warning": (
                        "Model behavior and provider provenance differ after this checkpoint."
                    ),
                    "usage_at_switch": (
                        dict(state.metadata["usage"])
                        if isinstance(state.metadata.get("usage"), dict)
                        else {}
                    ),
                }
            )
            state.metadata["model_cache_generation"] = _model_cache_generation(state) + 1
        backend_metadata = state.metadata.get("backend", {})
        if not isinstance(backend_metadata, dict):
            raise StateCorruptionError("backend metadata must be an object")
        if selected_provider != previous_provider:
            # Provider-specific observations from the old adapter (authentication,
            # model, version, and billing class) must never be presented as facts about
            # the explicitly selected replacement provider.
            backend_metadata = {
                "authentication_class": "unverified",
                "backend_version": None,
                "model_requested": (
                    self.config.codex.model or None
                    if selected_provider == "codex"
                    else {
                        "prompt_compiler": self.config.models.prompt_compiler.model,
                        "research": self.config.models.research.model,
                        "audit": self.config.models.audit.model,
                        "manuscript": self.config.models.manuscript.model,
                    }
                ),
                "reasoning_effort_requested": (
                    {
                        "research": self.config.codex.research_effort,
                        "audit": self.config.codex.audit_effort,
                        "manuscript": self.config.codex.manuscript_effort,
                        "formalization": self.config.codex.formalization_effort,
                    }
                    if selected_provider == "codex"
                    else "per-stage API settings"
                ),
                "web_search_policy": (
                    "enabled only for stages whose model settings require it"
                    if self.config.web_search_enabled
                    else "disabled for all stages by configuration"
                ),
            }
        backend_metadata.update(
            {
                "schema_version": 1,
                "provider": selected_provider,
                "display_name": (
                    "Codex CLI" if selected_provider == "codex" else "OpenAI Responses API"
                ),
                "automatic_fallback": False,
                "web_search_policy": (
                    "enabled only for stages whose model settings require it"
                    if self.config.web_search_enabled
                    else "disabled for all stages by configuration"
                ),
            }
        )
        state.metadata["backend"] = backend_metadata
        summary = state.metadata.get("configuration_summary", {})
        if not isinstance(summary, dict):
            raise StateCorruptionError("configuration_summary must be an object")
        summary.update(
            {
                "model_execution_backend": selected_provider,
                "backend_display_name": backend_metadata["display_name"],
                "authentication_class": backend_metadata.get("authentication_class", "unverified"),
                "automatic_fallback": False,
            }
        )
        state.metadata["configuration_summary"] = summary
        # Validate the injected adapter before any resumed model call. This catches a
        # programmatic resume that loaded the frozen provider (or explicitly migrated
        # it) but accidentally retained a client for the other billing boundary.
        self._sync_backend_metadata(state)
        effective_config_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            effective_config_path,
            config_as_toml(self.config),
            confinement_root=run_root,
        )
        atomic_write_json(
            run_root / "config" / "backend_manifest.json",
            backend_metadata,
            confinement_root=run_root,
        )
        store.save(state)
        options = WorkflowOptions(
            framework_path=(
                Path(str(state.metadata["custom_framework_path"]))
                if state.metadata.get("custom_framework_path")
                else None
            ),
            no_lean=bool(state.metadata.get("no_lean", False)),
            research_only=bool(state.metadata.get("research_only", False)),
            allow_project_edits=bool(state.metadata.get("allow_project_edits", False)),
        )
        return await self._execute(state, options)

    async def _execute(self, state: RunState, options: WorkflowOptions) -> WorkflowResult:
        run_root = state.run_root
        store = StateStore(run_root)
        if (
            state.stages[StageName.REPORT].status is StageStatus.SUCCEEDED
            and first_incomplete_stage(state) is None
        ):
            # A completed resume is a true no-op. In particular, do not append to the
            # logs after their hashes have been captured in the final report.
            return WorkflowResult(state=state, report=load_final_report(run_root))
        logger = RunLogger(
            run_root,
            run_id=state.run_id,
            model_cache_namespace=_model_cache_namespace(state),
        )
        previous_usage = state.metadata.get("usage", {})
        prior_elapsed = (
            float(previous_usage.get("elapsed_seconds", 0.0))
            if isinstance(previous_usage, dict)
            else 0.0
        )
        usage_records = _usage_records(run_root)
        if _sync_paid_calls_from_usage(state, usage_records):
            store.save(state)
        budget = self._budget_tracker(
            usage_records,
            prior_elapsed_seconds=max(0.0, prior_elapsed),
            include_unscoped_usage=not bool(state.metadata.get("backend_history")),
        )
        logger.event("workflow.resumed", data={"run_id": state.run_id})
        workflow_task = asyncio.current_task()
        if workflow_task is None:  # pragma: no cover - _execute always runs in an event loop
            raise WorkflowError("workflow execution has no owning asyncio task")
        deadline_expired = False

        def expire_workflow() -> None:
            nonlocal deadline_expired
            deadline_expired = True
            workflow_task.cancel()

        remaining_wall_clock = budget.remaining().wall_clock_seconds
        deadline_handle = (
            asyncio.get_running_loop().call_later(remaining_wall_clock, expire_workflow)
            if remaining_wall_clock is not None
            else None
        )
        try:
            self._validate_stage_boundary(
                state,
                logger,
                completed=(StageName.INTAKE,),
                next_step=StageName.PROMPT_COMPILATION.value,
            )
            compiled = await self._prompt_stage(state, store, logger, budget, options)
            if compiled.needs_clarification:
                self._skip_after_prompt(
                    state,
                    "prompt compiler could not identify a unique mathematical target",
                )
                store.save(state)
                return WorkflowResult(
                    state=state,
                    report=self._report_stage(state, store, logger),
                )
            self._validate_stage_boundary(
                state,
                logger,
                completed=(StageName.PROMPT_COMPILATION,),
                next_step=StageName.RESEARCH.value,
            )
            research = await self._research_stage(state, store, logger, budget, compiled)
            if not research.accepted_for_manuscript:
                self._skip_manuscript_and_lean(state, "research acceptance gate did not pass")
                store.save(state)
                return WorkflowResult(state=state, report=self._report_stage(state, store, logger))
            if options.research_only or not self.config.manuscript.enabled:
                self._skip_manuscript_and_lean(state, "research-only mode")
                state.metadata["manuscript_status"] = "NOT_REQUESTED"
                state.metadata["lean_status"] = ScientificStatus.LEAN_NOT_REQUESTED.value
                store.save(state)
                return WorkflowResult(state=state, report=self._report_stage(state, store, logger))

            self._validate_stage_boundary(
                state,
                logger,
                completed=(StageName.RESEARCH, StageName.RESEARCH_AUDIT),
                next_step=StageName.MANUSCRIPT.value,
            )

            manuscript = await self._manuscript_stage(
                state, store, logger, budget, compiled, research
            )
            if not manuscript.passed_lean_gate:
                self._skip_lean(state, "manuscript or bibliography gate did not pass")
                store.save(state)
                return WorkflowResult(state=state, report=self._report_stage(state, store, logger))
            if options.no_lean or not self.config.lean.enabled:
                self._skip_lean(state, "Lean was not requested")
                state.scientific_status = ScientificStatus.LEAN_NOT_REQUESTED
                state.metadata["lean_status"] = ScientificStatus.LEAN_NOT_REQUESTED.value
                store.save(state)
                return WorkflowResult(state=state, report=self._report_stage(state, store, logger))

            self._validate_stage_boundary(
                state,
                logger,
                completed=(StageName.MANUSCRIPT, StageName.BIBLIOGRAPHY),
                next_step="lean_consent",
            )
            if not await self._confirm_lean_after_manuscript(state, store, logger):
                self._skip_lean(state, "user declined Lean verification after manuscript")
                state.scientific_status = ScientificStatus.LEAN_NOT_REQUESTED
                state.metadata["lean_status"] = ScientificStatus.LEAN_NOT_REQUESTED.value
                store.save(state)
                return WorkflowResult(state=state, report=self._report_stage(state, store, logger))

            self._validate_stage_boundary(
                state,
                logger,
                completed=(StageName.MANUSCRIPT, StageName.BIBLIOGRAPHY),
                next_step=StageName.LEAN_FEASIBILITY.value,
            )
            await self._lean_stage(state, store, logger, budget, compiled, manuscript, options)
            return WorkflowResult(state=state, report=self._report_stage(state, store, logger))
        except asyncio.CancelledError:
            interruption_reason = (
                "run-wide wall-clock limit reached"
                if deadline_expired
                else "workflow task was cancelled"
            )
            self._interrupt_current_stage(state, interruption_reason)
            _sync_paid_calls_from_usage(state, _usage_records(run_root))
            state.metadata["usage"] = budget.snapshot().model_dump(mode="json")
            self._sync_backend_metadata(state)
            store.save(state)
            self._report_stage(state, store, logger)
            if deadline_expired:
                snapshot = budget.snapshot()
                configured_hours = budget.limits.maximum_wall_clock_hours
                assert configured_hours is not None
                limit = configured_hours * 3600
                raise BudgetExceeded(
                    "wall_clock",
                    limit,
                    max(snapshot.elapsed_seconds, limit + 1e-9),
                    snapshot,
                ) from None
            raise
        except Exception:
            # Stage methods checkpoint their own failures. Always leave a factual report,
            # then preserve the exception for CLI exit-code classification.
            _sync_paid_calls_from_usage(state, _usage_records(run_root))
            state.metadata["usage"] = budget.snapshot().model_dump(mode="json")
            self._sync_backend_metadata(state)
            store.save(state)
            if state.stages[StageName.REPORT].status is not StageStatus.SUCCEEDED:
                self._report_stage(state, store, logger)
            raise
        finally:
            if deadline_handle is not None:
                deadline_handle.cancel()

    def regenerate_report(self, project_root: Path, *, run_id: str | None = None) -> WorkflowResult:
        """Rebuild only deterministic report artifacts from an existing run.

        No model or Codex adapter is touched.  Upstream artifact hashes are checked
        before the old report checkpoint is explicitly invalidated.
        """

        run_root = resolve_run_root(project_root, run_id)
        with RunLock(run_root):
            return self._regenerate_report_locked(run_root)

    def _regenerate_report_locked(self, run_root: Path) -> WorkflowResult:
        store = StateStore(run_root)
        state = store.load()
        if (
            state.stages[StageName.REPORT].status is StageStatus.SUCCEEDED
            and (run_root / "report" / "verification_certificate.json").is_file()
        ):
            assert_report_certificate_inventory(run_root)
        invalidate_from(state, StageName.REPORT, "explicit report regeneration")
        # Report files are replaceable outputs; verify every still-recorded upstream
        # artifact only after removing the prior report checkpoint from the contract.
        assert_recorded_artifacts(state)
        store.save(state)
        logger = RunLogger(
            run_root,
            run_id=state.run_id,
            model_cache_namespace=_model_cache_namespace(state),
        )
        report = self._report_stage(state, store, logger)
        return WorkflowResult(state=state, report=report)

    async def rewrite_report(
        self,
        project_root: Path,
        *,
        run_id: str | None = None,
    ) -> WorkflowResult:
        """Explicitly purchase an optional narrative rewrite around deterministic facts."""

        run_root = resolve_run_root(project_root, run_id)
        with RunLock(run_root):
            return await self._rewrite_report_locked(run_root)

    async def _rewrite_report_locked(self, run_root: Path) -> WorkflowResult:
        store = StateStore(run_root)
        state = store.load()
        if (
            state.stages[StageName.REPORT].status is StageStatus.SUCCEEDED
            and (run_root / "report" / "verification_certificate.json").is_file()
        ):
            assert_report_certificate_inventory(run_root)
        invalidate_from(state, StageName.REPORT, "explicit --rewrite request")
        state.metadata["model_cache_generation"] = _model_cache_generation(state) + 1
        assert_recorded_artifacts(state)
        logger = RunLogger(
            run_root,
            run_id=state.run_id,
            model_cache_namespace=_model_cache_namespace(state),
        )
        previous_usage = state.metadata.get("usage", {})
        prior_elapsed = (
            float(previous_usage.get("elapsed_seconds", 0.0))
            if isinstance(previous_usage, dict)
            else 0.0
        )
        usage_records = _usage_records(run_root)
        _sync_paid_calls_from_usage(state, usage_records)
        budget = self._budget_tracker(
            usage_records,
            prior_elapsed_seconds=max(0.0, prior_elapsed),
            include_unscoped_usage=not bool(state.metadata.get("backend_history")),
        )
        self._begin(state, store, logger, StageName.REPORT)
        factual_report = build_final_report(state)
        try:
            with resource_path("prompts/report_writer.md") as instructions_path:
                result = await self._stage_client(
                    StageName.REPORT,
                    budget,
                    logger,
                ).generate_structured(
                    ModelRequest(
                        instructions=instructions_path.read_text(encoding="utf-8"),
                        input_text=json.dumps(
                            {
                                "authoritative_report": factual_report.model_dump(mode="json"),
                                "constraint": (
                                    "Rewrite prose only. Do not alter statuses, hashes, artifact "
                                    "paths, costs, or unresolved obligations."
                                ),
                            },
                            ensure_ascii=False,
                        ),
                        settings=self._model_settings("manuscript").model_copy(
                            update={
                                "reasoning_mode": "standard",
                                "reasoning_effort": "medium",
                                "web_search": False,
                                "max_output_tokens": 4_000,
                            }
                        ),
                    ),
                    ReportNarrative,
                )
        except Exception as exc:
            state.metadata["usage"] = budget.snapshot().model_dump(mode="json")
            self._failure(state, store, logger, StageName.REPORT, exc)
            raise

        record_paid_call(state, StageName.REPORT, result.response_id)
        state.metadata["usage"] = budget.snapshot().model_dump(mode="json")
        logger.event("report.rewrite.accepted", stage=StageName.REPORT)
        self._sync_backend_metadata(state)
        report = write_final_report(state, narrative=result.parsed)
        for path in (
            report.report_json,
            report.report_markdown,
            report.verification_certificate,
        ):
            record_artifact_file(state, StageName.REPORT, path)
        succeed_stage(state, StageName.REPORT)
        state.metadata["workflow_status"] = ScientificStatus.REPORT_COMPLETE.value
        store.save(state)
        budget.ensure_available()
        return WorkflowResult(state=state, report=report)

    def _stage_client(
        self,
        stage: StageName,
        budget: BudgetTracker,
        logger: RunLogger,
    ) -> AccountingModelClient:
        return AccountingModelClient(
            self.dependencies.model_client,
            stage=stage.value,
            budget=budget,
            logger=logger,
            provider=self.config.backend.provider,
        )

    @staticmethod
    def _begin(
        state: RunState,
        store: StateStore,
        logger: RunLogger,
        stage: StageName,
    ) -> bool:
        status = state.stages[stage].status
        if status in {StageStatus.SUCCEEDED, StageStatus.SKIPPED}:
            return False
        start_stage(state, stage)
        logger.event("stage.started", stage=stage)
        store.save(state)
        return True

    def _checkpoint(
        self,
        state: RunState,
        store: StateStore,
        logger: RunLogger,
        stage: StageName,
        *,
        paths: Mapping[str, Path],
        response_ids: list[str],
        budget: BudgetTracker,
    ) -> None:
        for path in paths.values():
            record_artifact_file(state, stage, path)
        for response_id in response_ids:
            record_paid_call(state, stage, response_id)
        succeed_stage(state, stage)
        state.metadata["usage"] = budget.snapshot().model_dump(mode="json")
        logger.event("stage.succeeded", stage=stage, data={"calls": len(response_ids)})
        self._sync_backend_metadata(state)
        store.save(state)

    def _failure(
        self,
        state: RunState,
        store: StateStore,
        logger: RunLogger,
        stage: StageName,
        exc: Exception,
    ) -> None:
        safe_message = redact_text(str(exc))
        fail_stage(
            state,
            stage,
            safe_message,
            kind=type(exc).__name__,
            retriable=True,
        )
        logger.event(
            "stage.failed",
            level="ERROR",
            stage=stage,
            data={"error_type": type(exc).__name__, "message": safe_message},
        )
        self._sync_backend_metadata(state)
        store.save(state)

    @staticmethod
    def _validate_stage_boundary(
        state: RunState,
        logger: RunLogger,
        *,
        completed: Sequence[StageName],
        next_step: str,
    ) -> None:
        """Fail closed before crossing a stage boundary with incomplete or changed inputs."""

        incomplete = [
            stage.value
            for stage in completed
            if state.stages[stage].status is not StageStatus.SUCCEEDED
        ]
        if incomplete:
            raise StateCorruptionError(
                f"cannot enter {next_step}; required stage checkpoints are not successful: "
                + ", ".join(incomplete)
            )
        assert_recorded_artifacts(state)
        logger.event(
            "stage.boundary.validated",
            data={
                "completed": [stage.value for stage in completed],
                "next_step": next_step,
            },
        )

    @staticmethod
    def _existing_lean_consent(state: RunState) -> LeanConsentOutcome | None:
        raw = state.metadata.get("lean_consent")
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise StateCorruptionError("lean_consent metadata must be an object")
        try:
            outcome = LeanConsentOutcome(str(raw["outcome"]))
            expected_hash = str(raw["artifact_sha256"])
            artifact_path = ensure_path_confined(
                state.run_root, state.run_root / str(raw["artifact"])
            )
        except (KeyError, ValueError) as exc:
            raise StateCorruptionError("lean_consent metadata is incomplete or invalid") from exc
        if not artifact_path.is_file() or artifact_path.is_symlink():
            raise StateCorruptionError("the durable Lean consent artifact is missing or unsafe")
        if sha256_file(artifact_path) != expected_hash:
            raise StateCorruptionError("the durable Lean consent artifact has changed")
        try:
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StateCorruptionError("the durable Lean consent artifact is invalid") from exc
        if (
            not isinstance(artifact, dict)
            or artifact.get("run_id") != state.run_id
            or artifact.get("outcome") != outcome.value
            or artifact.get("proceed") is not outcome.proceed
            or raw.get("proceed") is not outcome.proceed
        ):
            raise StateCorruptionError("Lean consent metadata does not match its durable artifact")
        return outcome

    @classmethod
    def _archive_lean_consent(cls, state: RunState, *, reason: str) -> None:
        """Preserve an invalidated decision as history before a forced downstream rerun."""

        outcome = cls._existing_lean_consent(state)
        if outcome is None:
            return
        consent_path = ensure_path_confined(
            state.run_root, state.run_root / "lean" / "consent.json"
        )
        artifact = json.loads(consent_path.read_text(encoding="utf-8"))
        history_path = (
            state.run_root
            / "lean"
            / "consent-history"
            / f"checkpoint-{state.checkpoint_generation}.json"
        )
        atomic_write_json(
            history_path,
            {
                **artifact,
                "invalidated_at": datetime.now(UTC).isoformat(),
                "invalidation_reason": reason,
                "previous_outcome": outcome.value,
            },
            confinement_root=state.run_root,
        )
        consent_path.unlink()
        raw_history = state.metadata.get("lean_consent_history", [])
        history = list(raw_history) if isinstance(raw_history, list) else []
        history.append(
            {
                "outcome": outcome.value,
                "artifact": history_path.relative_to(state.run_root).as_posix(),
                "reason": reason,
            }
        )
        state.metadata["lean_consent_history"] = history
        state.metadata.pop("lean_consent", None)

    async def _confirm_lean_after_manuscript(
        self,
        state: RunState,
        store: StateStore,
        logger: RunLogger,
    ) -> bool:
        """Ask once after the manuscript gate and durably preserve the answer."""

        existing = self._existing_lean_consent(state)
        if existing is not None:
            logger.event(
                "lean.consent.reused",
                data={"outcome": existing.value, "proceed": existing.proceed},
            )
            return existing.proceed

        request = LeanConsentRequest(
            run_id=state.run_id,
            manuscript_path=state.run_root / "manuscript" / "paper.pdf",
        )
        error_kind: str | None = None
        try:
            raw_outcome = await asyncio.wait_for(
                self.dependencies.lean_consent(request),
                timeout=request.timeout_seconds,
            )
            outcome = LeanConsentOutcome(raw_outcome)
        except TimeoutError:
            outcome = LeanConsentOutcome.TIMED_OUT
        except Exception as exc:
            # A broken terminal prompt must not discard a verified manuscript or strand the
            # workflow. Treat it as no response, record the error class, and proceed.
            outcome = LeanConsentOutcome.PROMPT_ERROR
            error_kind = type(exc).__name__

        consent_path = state.run_root / "lean" / "consent.json"
        payload = {
            "schema_version": 1,
            "run_id": state.run_id,
            "outcome": outcome.value,
            "proceed": outcome.proceed,
            "timeout_seconds": request.timeout_seconds,
            "manuscript": request.manuscript_path.relative_to(state.run_root).as_posix(),
            "recorded_at": datetime.now(UTC).isoformat(),
            "prompt_error_kind": error_kind,
        }
        atomic_write_json(consent_path, payload, confinement_root=state.run_root)
        state.metadata["lean_consent"] = {
            **payload,
            "artifact": consent_path.relative_to(state.run_root).as_posix(),
            "artifact_sha256": sha256_file(consent_path),
        }
        logger.event(
            "lean.consent.recorded",
            data={"outcome": outcome.value, "proceed": outcome.proceed},
        )
        store.save(state)
        return outcome.proceed

    async def _prompt_stage(
        self,
        state: RunState,
        store: StateStore,
        logger: RunLogger,
        budget: BudgetTracker,
        options: WorkflowOptions,
    ) -> CompiledProblem:
        result_path = state.run_root / "prompts" / "compiled_problem.json"
        if not self._begin(state, store, logger, StageName.PROMPT_COMPILATION):
            return CompiledProblem.model_validate_json(result_path.read_text(encoding="utf-8"))
        self.dependencies.progress(
            Ascension.FORMULATE_PROMPT,
            "Formulating technical research prompt.",
        )

        custom = options.framework_path
        framework_context = (
            nullcontext(custom.resolve(strict=True))
            if custom is not None
            else resource_path("prompts/research_prompt_framework.txt")
        )
        try:
            with (
                framework_context as framework,
                resource_path("prompts/prompt_compiler.md") as instructions,
            ):
                result: PromptCompilationResult = await compile_prompt(
                    client=self._stage_client(StageName.PROMPT_COMPILATION, budget, logger),
                    problem_text=(state.run_root / "input" / "problem.md").read_text(
                        encoding="utf-8"
                    ),
                    framework_path=framework,
                    prompts_dir=state.run_root / "prompts",
                    instructions_path=instructions,
                    settings=self._model_settings("prompt"),
                    expected_framework_sha256=(
                        None if custom is not None else EXPECTED_FRAMEWORK_SHA256
                    ),
                    source_verifier=self._source_verifier(state.run_root),
                    placeholder_repair_generation=_prompt_validation_generation(state),
                )
        except Exception as exc:
            prompt_dir = state.run_root / "prompts"
            preserved = (
                sorted(
                    path.relative_to(state.run_root).as_posix()
                    for path in prompt_dir.iterdir()
                    if path.is_file() and not path.is_symlink()
                )
                if prompt_dir.is_dir()
                else []
            )
            state.metadata["prompt_compilation_recovery"] = {
                "artifacts_preserved": preserved,
                "placeholder_repair_generation": state.metadata.get(
                    "prompt_validation_generation", 0
                ),
                "next_action": (
                    "Resume to reuse cached compiler/source work, or use --force-stage "
                    "prompt_compilation for a fresh bounded placeholder-repair generation."
                ),
            }
            self._failure(state, store, logger, StageName.PROMPT_COMPILATION, exc)
            raise
        state.metadata.pop("prompt_compilation_recovery", None)
        state.metadata.update(
            {
                "literature_status": result.compiled_problem.literature_status.value,
                "literature_resolution_summary": (
                    result.compiled_problem.literature_resolution_summary
                ),
                "source_provenance_warnings": result.source_verification.warnings,
                "prompt_validation_warnings": result.prompt_validation.warnings,
                "prompt_validation_generation": result.prompt_validation.repair_generation,
            }
        )
        if result.needs_clarification:
            state.scientific_status = ScientificStatus.NEEDS_PROBLEM_CLARIFICATION
            state.metadata.update(
                {
                    "research_status": ScientificStatus.NEEDS_PROBLEM_CLARIFICATION.value,
                    "strongest_result": (
                        "No research claim was attempted because ASCEND could not identify "
                        "a unique mathematical problem from the supplied description."
                    ),
                    "unresolved_obligations": result.compiled_problem.clarification_questions,
                    "problem_clarification": {
                        "required": True,
                        "reason": result.compiled_problem.clarification_reason,
                        "questions": result.compiled_problem.clarification_questions,
                        "candidate_interpretations": (
                            result.compiled_problem.candidate_interpretations
                        ),
                        "next_action": (
                            "Revise the problem file to identify one exact target, then start "
                            "a new ASCEND run."
                        ),
                    },
                    "manuscript_status": "SKIPPED_PROBLEM_CLARIFICATION",
                    "lean_status": "SKIPPED_PROBLEM_CLARIFICATION",
                }
            )
        else:
            state.scientific_status = ScientificStatus.PROMPT_COMPILED
            state.metadata["research_status"] = ScientificStatus.PROMPT_COMPILED.value
        self._checkpoint(
            state,
            store,
            logger,
            StageName.PROMPT_COMPILATION,
            paths=result.artifacts.paths,
            response_ids=result.calls.response_ids,
            budget=budget,
        )
        budget.ensure_available()
        return result.compiled_problem

    async def _research_stage(
        self,
        state: RunState,
        store: StateStore,
        logger: RunLogger,
        budget: BudgetTracker,
        compiled: CompiledProblem,
    ) -> ResearchResult:
        result_path = state.run_root / "research" / "result.json"
        if not self._begin(state, store, logger, StageName.RESEARCH):
            result = ResearchResult.model_validate_json(result_path.read_text(encoding="utf-8"))
            self._finalize_research_audit(state, store, logger, result)
            return result
        names = (
            "prompts/research_coordinator.md",
            "prompts/research_worker.md",
            "prompts/candidate_packager.md",
            "prompts/final_judge.md",
            "prompts/audit_foundational.md",
            "prompts/audit_domain.md",
            "prompts/audit_hostile.md",
            "prompts/audit_sources.md",
            "prompts/audit_complexity.md",
        )
        try:
            with resource_paths(*names) as paths:
                result = await run_adaptive_research(
                    client=self._stage_client(StageName.RESEARCH, budget, logger),
                    compiled_problem=compiled,
                    research_dir=state.run_root / "research",
                    workflow_settings=ResearchWorkflowSettings(
                        minimum_initial_assignments=max(
                            4, self.config.research.minimum_initial_agents
                        ),
                        maximum_concurrent_agents=(
                            min(
                                self.config.research.maximum_concurrent_agents,
                                self.config.codex.max_parallel_agents,
                                self.config.codex.max_parallel_web_agents,
                            )
                            if self.config.backend.provider == "codex"
                            else min(
                                self.config.research.maximum_concurrent_agents,
                                self.config.api.max_parallel_agents,
                            )
                        ),
                        maximum_research_subagents=(
                            self.config.research.maximum_research_subagents
                        ),
                        maximum_assignments_per_round=(
                            self.config.research.maximum_assignments_per_round
                        ),
                        maximum_rounds=(
                            min(
                                self.config.research.maximum_rounds,
                                self.config.codex.limits.max_research_rounds,
                            )
                            if self.config.backend.provider == "codex"
                            else self.config.research.maximum_rounds
                        ),
                    ),
                    coordinator_settings=self._model_settings("research"),
                    worker_settings=self._model_settings("research"),
                    audit_settings=self._model_settings("audit"),
                    coordinator_prompt_path=paths[names[0]],
                    worker_prompt_path=paths[names[1]],
                    candidate_prompt_path=paths[names[2]],
                    final_judge_prompt_path=paths[names[3]],
                    audit_prompt_paths={
                        "foundational": paths[names[4]],
                        "domain": paths[names[5]],
                        "hostile": paths[names[6]],
                        "sources": paths[names[7]],
                        "complexity": paths[names[8]],
                    },
                    source_verifier=self._source_verifier(state.run_root),
                    progress=self.dependencies.progress,
                )
        except Exception as exc:
            self._failure(state, store, logger, StageName.RESEARCH, exc)
            raise

        status = {
            ResearchOutcome.ACCEPTED: ScientificStatus.RESEARCH_ACCEPTED_FOR_MANUSCRIPT,
            ResearchOutcome.REJECTED: ScientificStatus.RESEARCH_REJECTED,
            ResearchOutcome.PARTIAL: ScientificStatus.RESEARCH_PARTIAL,
            ResearchOutcome.BUDGET_EXHAUSTED: ScientificStatus.RESEARCH_PARTIAL,
        }[result.outcome]
        state.scientific_status = status
        configuration_summary = state.metadata.get("configuration_summary", {})
        if not isinstance(configuration_summary, dict):
            raise StateCorruptionError("configuration_summary must be an object")
        configuration_summary.update(
            {
                "research_subagents_assigned": result.research_subagents_assigned,
                "research_subagents_used": result.research_subagents_used,
            }
        )
        state.metadata.update(
            {
                "research_status": status.value,
                "research_subagents_assigned": result.research_subagents_assigned,
                "research_subagents_used": result.research_subagents_used,
                "configuration_summary": configuration_summary,
                "strongest_result": result.strongest_result,
                "unresolved_obligations": result.unresolved_obligations,
            }
        )
        research_paths = dict(result.artifacts.paths)
        if result_path.is_file():
            research_paths["result"] = result_path
        self._checkpoint(
            state,
            store,
            logger,
            StageName.RESEARCH,
            paths=research_paths,
            response_ids=result.calls.response_ids,
            budget=budget,
        )
        self._finalize_research_audit(state, store, logger, result)
        budget.ensure_available()
        return result

    def _finalize_research_audit(
        self,
        state: RunState,
        store: StateStore,
        logger: RunLogger,
        result: ResearchResult,
    ) -> None:
        if result.final_verdict is not None:
            if self._begin(state, store, logger, StageName.RESEARCH_AUDIT):
                for path in result.artifacts.paths.values():
                    if "audit" in path.parts or path.name == "verdict.json":
                        record_artifact_file(state, StageName.RESEARCH_AUDIT, path)
                succeed_stage(state, StageName.RESEARCH_AUDIT)
        else:
            self._skip_pending(state, StageName.RESEARCH_AUDIT, "no candidate reached audit")
        store.save(state)

    async def _manuscript_stage(
        self,
        state: RunState,
        store: StateStore,
        logger: RunLogger,
        budget: BudgetTracker,
        compiled: CompiledProblem,
        research: ResearchResult,
    ) -> ManuscriptResult:
        result_path = state.run_root / "manuscript" / "result.json"
        if not self._begin(state, store, logger, StageName.MANUSCRIPT):
            return ManuscriptResult.model_validate_json(result_path.read_text(encoding="utf-8"))
        self.dependencies.progress(
            Ascension.WRITE_MANUSCRIPT,
            "Writing manuscript and verifying bibliography.",
        )
        resume_bibliography = bool(state.metadata.get("resume_bibliography_correction", False))
        try:
            with resource_paths(
                "prompts/manuscript_writer.md", "prompts/bibliography_verifier.md"
            ) as paths:
                if resume_bibliography:
                    previous_result = ManuscriptResult.model_validate_json(
                        result_path.read_text(encoding="utf-8")
                    )
                    result = await resume_manuscript_bibliography(
                        client=self._stage_client(StageName.BIBLIOGRAPHY, budget, logger),
                        previous_result=previous_result,
                        backend=self.dependencies.execution_backend,
                        research_result=research,
                        claim_contract=compiled.claim_contract.as_dict(),
                        source_ledger=[
                            entry.model_dump(mode="json") for entry in compiled.source_ledger
                        ],
                        manuscript_dir=state.run_root / "manuscript",
                        maximum_additional_correction_cycles=max(
                            1, self.config.manuscript.maximum_revision_rounds
                        ),
                        writer_settings=self._model_settings("manuscript"),
                        verifier_settings=self._model_settings("audit"),
                        latex_command=tuple(self.config.manuscript.latex_command),
                        manuscript_prompt_path=paths["prompts/manuscript_writer.md"],
                        bibliography_prompt_path=paths["prompts/bibliography_verifier.md"],
                        source_verifier=self._source_verifier(state.run_root),
                    )
                else:
                    result = await generate_manuscript(
                        client=self._stage_client(StageName.MANUSCRIPT, budget, logger),
                        backend=self.dependencies.execution_backend,
                        research_result=research,
                        claim_contract=compiled.claim_contract.as_dict(),
                        source_ledger=[
                            entry.model_dump(mode="json") for entry in compiled.source_ledger
                        ],
                        manuscript_dir=state.run_root / "manuscript",
                        maximum_correction_cycles=self.config.manuscript.maximum_revision_rounds,
                        writer_settings=self._model_settings("manuscript"),
                        verifier_settings=self._model_settings("audit"),
                        latex_command=tuple(self.config.manuscript.latex_command),
                        manuscript_prompt_path=paths["prompts/manuscript_writer.md"],
                        bibliography_prompt_path=paths["prompts/bibliography_verifier.md"],
                        source_verifier=self._source_verifier(state.run_root),
                    )
        except Exception as exc:
            self._failure(state, store, logger, StageName.MANUSCRIPT, exc)
            raise

        paid_stage = StageName.BIBLIOGRAPHY if resume_bibliography else StageName.MANUSCRIPT
        for response_id in result.calls.response_ids:
            record_paid_call(state, paid_stage, response_id)
        for path in result.artifacts.paths.values():
            record_artifact_file(state, StageName.MANUSCRIPT, path)
        if result_path.is_file():
            record_artifact_file(state, StageName.MANUSCRIPT, result_path)
        if result.outcome is ManuscriptOutcome.COMPILED:
            succeed_stage(state, StageName.MANUSCRIPT)
            if self._begin(state, store, logger, StageName.BIBLIOGRAPHY):
                for path in result.artifacts.paths.values():
                    if "bibliography" in path.name or path.name == "references.bib":
                        record_artifact_file(state, StageName.BIBLIOGRAPHY, path)
                succeed_stage(state, StageName.BIBLIOGRAPHY)
            state.scientific_status = ScientificStatus.BIBLIOGRAPHY_VERIFIED
            state.metadata["manuscript_status"] = ScientificStatus.BIBLIOGRAPHY_VERIFIED.value
        elif result.outcome is ManuscriptOutcome.BIBLIOGRAPHY_REJECTED:
            succeed_stage(state, StageName.MANUSCRIPT)
            if self._begin(state, store, logger, StageName.BIBLIOGRAPHY):
                fail_stage(state, StageName.BIBLIOGRAPHY, "bibliography verification rejected")
            state.scientific_status = ScientificStatus.BIBLIOGRAPHY_REJECTED
            state.metadata["manuscript_status"] = ScientificStatus.BIBLIOGRAPHY_REJECTED.value
        else:
            fail_stage(state, StageName.MANUSCRIPT, result.outcome.value)
            self._skip_pending(state, StageName.BIBLIOGRAPHY, "manuscript gate failed")
            state.scientific_status = ScientificStatus.MANUSCRIPT_FAILED
            state.metadata["manuscript_status"] = ScientificStatus.MANUSCRIPT_FAILED.value
        state.metadata["usage"] = budget.snapshot().model_dump(mode="json")
        state.metadata.pop("resume_bibliography_correction", None)
        store.save(state)
        budget.ensure_available()
        return result

    async def _lean_stage(
        self,
        state: RunState,
        store: StateStore,
        logger: RunLogger,
        budget: BudgetTracker,
        compiled: CompiledProblem,
        manuscript: ManuscriptResult,
        options: WorkflowOptions,
    ) -> None:
        result_path = state.run_root / "lean" / "result.json"
        if not self._begin(state, store, logger, StageName.LEAN_FEASIBILITY):
            result = LeanPipelineResult.model_validate_json(result_path.read_text(encoding="utf-8"))
            self._apply_lean_result(state, store, logger, result, budget)
            return
        self.dependencies.progress(
            Ascension.FORMALIZE_LEAN,
            "Assessing and verifying the Lean formalization.",
        )
        names = (
            "prompts/lean_feasibility.md",
            "prompts/lean_statement_generator.md",
            "prompts/lean_statement_auditor.md",
            "prompts/codex_formalizer.md",
        )
        prohibited = ["by?", "TODO"]
        if self.config.lean.prohibit_sorry:
            prohibited.append("sorry")
        if self.config.lean.prohibit_admit:
            prohibited.append("admit")
        try:
            with resource_paths(*names) as paths:
                codex_limits = self.config.codex.limits
                remaining_threads = (
                    max(0, codex_limits.max_codex_threads - budget.snapshot().calls)
                    if self.config.backend.provider == "codex"
                    else codex_limits.max_codex_threads
                )
                result = await run_lean_pipeline(
                    client=self._stage_client(StageName.LEAN_FEASIBILITY, budget, logger),
                    codex_client=self.dependencies.codex_client,
                    backend=self.dependencies.execution_backend,
                    research_result=ResearchResult.model_validate_json(
                        (state.run_root / "research" / "result.json").read_text(encoding="utf-8")
                    ),
                    manuscript_result=manuscript,
                    claim_contract=compiled.claim_contract.as_dict(),
                    lean_dir=state.run_root / "lean",
                    lean_project_root=state.project_root,
                    workflow_settings=LeanWorkflowSettings(
                        maximum_codex_iterations=min(
                            self.config.lean.maximum_codex_iterations,
                            codex_limits.max_formalization_iterations,
                            codex_limits.max_agent_calls,
                            remaining_threads,
                        ),
                        approved_axioms=self.config.lean.approved_axioms,
                        prohibited_tokens=prohibited,
                        allow_project_edits=options.allow_project_edits,
                    ),
                    model_settings=self._model_settings("audit").model_copy(
                        update={"web_search": False}
                    ),
                    feasibility_prompt_path=paths[names[0]],
                    statement_generator_prompt_path=paths[names[1]],
                    statement_auditor_prompt_path=paths[names[2]],
                    codex_prompt_path=paths[names[3]],
                )
        except Exception as exc:
            self._failure(state, store, logger, StageName.LEAN_FEASIBILITY, exc)
            raise
        for path in result.artifacts.paths.values():
            record_artifact_file(state, StageName.LEAN_FEASIBILITY, path)
        if result_path.is_file():
            record_artifact_file(state, StageName.LEAN_FEASIBILITY, result_path)
        for response_id in result.calls.response_ids:
            record_paid_call(state, StageName.LEAN_FEASIBILITY, response_id)
        succeed_stage(state, StageName.LEAN_FEASIBILITY)
        self._apply_lean_result(state, store, logger, result, budget)
        budget.ensure_available()

    def _apply_lean_result(
        self,
        state: RunState,
        store: StateStore,
        logger: RunLogger,
        result: LeanPipelineResult,
        budget: BudgetTracker,
    ) -> None:
        aligned = (
            result.alignment is not None
            and result.alignment.status is AlignmentStatus.ALIGNED
            and result.alignment.fully_aligned
        )
        if result.outcome is LeanOutcome.INFEASIBLE:
            self._skip_pending(state, StageName.LEAN_ALIGNMENT, "formalization infeasible")
            self._skip_pending(state, StageName.LEAN_FORMALIZATION, "formalization infeasible")
            self._skip_pending(state, StageName.LEAN_VERIFICATION, "formalization infeasible")
        elif not aligned:
            if self._begin(state, store, logger, StageName.LEAN_ALIGNMENT):
                fail_stage(state, StageName.LEAN_ALIGNMENT, "statement alignment failed")
            self._skip_pending(state, StageName.LEAN_FORMALIZATION, "alignment gate failed")
            self._skip_pending(state, StageName.LEAN_VERIFICATION, "alignment gate failed")
        else:
            if self._begin(state, store, logger, StageName.LEAN_ALIGNMENT):
                succeed_stage(state, StageName.LEAN_ALIGNMENT)
            if result.outcome is LeanOutcome.STATEMENT_ONLY:
                self._skip_pending(
                    state, StageName.LEAN_FORMALIZATION, "statement-only feasibility outcome"
                )
                self._skip_pending(
                    state, StageName.LEAN_VERIFICATION, "statement-only feasibility outcome"
                )
            elif result.outcome in {
                LeanOutcome.VERIFIED,
                LeanOutcome.VERIFIED_WITH_APPROVED_AXIOMS,
            }:
                if self._begin(state, store, logger, StageName.LEAN_FORMALIZATION):
                    succeed_stage(state, StageName.LEAN_FORMALIZATION)
                if self._begin(state, store, logger, StageName.LEAN_VERIFICATION):
                    succeed_stage(state, StageName.LEAN_VERIFICATION)
            else:
                if self._begin(state, store, logger, StageName.LEAN_FORMALIZATION):
                    fail_stage(
                        state,
                        StageName.LEAN_FORMALIZATION,
                        "Lean formalization did not reach verification",
                        retriable=True,
                    )
                if result.verification is not None:
                    if self._begin(state, store, logger, StageName.LEAN_VERIFICATION):
                        fail_stage(
                            state,
                            StageName.LEAN_VERIFICATION,
                            "; ".join(result.verification.diagnostics)
                            or "deterministic Lean verification failed",
                            retriable=True,
                        )
                else:
                    self._skip_pending(
                        state, StageName.LEAN_VERIFICATION, "no completed Lean proof"
                    )
        state.metadata.update(
            {
                "lean_status": result.outcome.value,
                "unresolved_obligations": result.unresolved_obligations,
                "deterministic_verification_passed": bool(
                    result.verification is not None and result.verification.passed
                ),
                "approved_axioms": (
                    result.verification.used_axioms if result.verification is not None else []
                ),
                "usage": budget.snapshot().model_dump(mode="json"),
            }
        )
        state.scientific_status = ScientificStatus(result.outcome.value)
        logger.event("lean.pipeline.completed", data={"outcome": result.outcome.value})
        store.save(state)

    def _report_stage(
        self,
        state: RunState,
        store: StateStore,
        logger: RunLogger,
    ) -> ReportArtifacts:
        assert_recorded_artifacts(state)
        self._existing_lean_consent(state)
        logger.event(
            "stage.boundary.validated",
            data={
                "completed": [
                    stage.value
                    for stage, record in state.stages.items()
                    if stage is not StageName.REPORT
                    and record.status in {StageStatus.SUCCEEDED, StageStatus.SKIPPED}
                ],
                "next_step": StageName.REPORT.value,
            },
        )
        existing = state.stages[StageName.REPORT]
        if existing.status is StageStatus.SUCCEEDED:
            return load_final_report(state.run_root)
        self.dependencies.progress(Ascension.PREPARE_REPORT, "Preparing final report.")
        self._begin(state, store, logger, StageName.REPORT)
        logger.event("report.generating", stage=StageName.REPORT)
        self._sync_backend_metadata(state)
        report = write_final_report(state)
        for path in (
            report.report_json,
            report.report_markdown,
            report.verification_certificate,
        ):
            record_artifact_file(state, StageName.REPORT, path)
        succeed_stage(state, StageName.REPORT)
        state.metadata["workflow_status"] = ScientificStatus.REPORT_COMPLETE.value
        store.save(state)
        return report

    @staticmethod
    def _skip_pending(state: RunState, stage: StageName, reason: str) -> None:
        if state.stages[stage].status is StageStatus.PENDING:
            skip_stage(state, stage, reason)

    def _skip_lean(self, state: RunState, reason: str) -> None:
        for stage in (
            StageName.LEAN_FEASIBILITY,
            StageName.LEAN_ALIGNMENT,
            StageName.LEAN_FORMALIZATION,
            StageName.LEAN_VERIFICATION,
        ):
            self._skip_pending(state, stage, reason)

    def _skip_manuscript_and_lean(self, state: RunState, reason: str) -> None:
        self._skip_pending(state, StageName.MANUSCRIPT, reason)
        self._skip_pending(state, StageName.BIBLIOGRAPHY, reason)
        self._skip_lean(state, reason)

    def _skip_after_prompt(self, state: RunState, reason: str) -> None:
        for stage in (
            StageName.RESEARCH,
            StageName.RESEARCH_AUDIT,
            StageName.MANUSCRIPT,
            StageName.BIBLIOGRAPHY,
            StageName.LEAN_FEASIBILITY,
            StageName.LEAN_ALIGNMENT,
            StageName.LEAN_FORMALIZATION,
            StageName.LEAN_VERIFICATION,
        ):
            self._skip_pending(state, stage, reason)

    @staticmethod
    def _interrupt_current_stage(state: RunState, message: str) -> None:
        for stage, record in state.stages.items():
            if record.status is StageStatus.RUNNING:
                interrupt_stage(state, stage, message)
