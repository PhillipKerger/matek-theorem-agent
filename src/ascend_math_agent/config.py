"""Typed configuration loading for ASCEND.

Configuration has one deliberately small and predictable precedence chain::

    built-in defaults < ascend.toml < ASCEND_* environment < CLI overrides

Environment keys use ``__`` as the nesting separator, for example
``ASCEND_MODELS__PROMPT_COMPILER__MODEL``.  A handful of CLI-shaped convenience
names (``ASCEND_MAX_ROUNDS``, ``ASCEND_BUDGET_USD``, and so on) are also accepted.
The model-execution backend follows that same chain and defaults to the locally
installed Codex CLI.  API settings remain available under ``[api]`` but selecting
Codex never falls through to API billing.  Secrets are intentionally not part of
this model; credentials are read by the selected adapter at call time and must
never be included in a resolved config snapshot.
"""

from __future__ import annotations

import copy
import json
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar, Literal, TypeAlias, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

ConfigMapping: TypeAlias = Mapping[str, Any]
BackendProvider: TypeAlias = Literal["codex", "api"]

CURRENT_CONFIG_VERSION: Literal[2] = 2
BACKEND_MIGRATION_ID: Literal["backend-provider-v2"] = "backend-provider-v2"
BACKEND_MIGRATION_MESSAGE = (
    "ASCEND migrated this pre-v2 configuration in memory and preserved the OpenAI API "
    "backend because legacy API model, pricing, or limit settings were detected. Add "
    'config_version = 2 and [backend] provider = "api" to ascend.toml to make the '
    "selection explicit. ASCEND did not discard any API settings and will never switch "
    "providers automatically."
)


class ConfigError(ValueError):
    """Raised when configuration cannot be read, merged, or validated."""


class _StrictSettings(BaseModel):
    """Base class shared by persisted configuration sections."""

    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)


class ModelSettings(_StrictSettings):
    model: str = "gpt-5.6-sol"
    reasoning_mode: Literal["standard", "pro"] = "pro"
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] = "xhigh"
    web_search: bool = True
    maximum_web_search_calls: int = Field(default=8, gt=0)
    max_output_tokens: int = Field(default=100_000, gt=0)

    @field_validator("model")
    @classmethod
    def _model_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model must not be blank")
        return value.strip()


class ModelsSettings(_StrictSettings):
    prompt_compiler: ModelSettings = Field(default_factory=ModelSettings)
    research_coordinator: ModelSettings = Field(
        default_factory=lambda: ModelSettings(reasoning_effort="max", max_output_tokens=120_000)
    )
    research_worker: ModelSettings = Field(
        default_factory=lambda: ModelSettings(reasoning_effort="xhigh", max_output_tokens=120_000)
    )
    audit: ModelSettings = Field(
        default_factory=lambda: ModelSettings(reasoning_effort="xhigh", max_output_tokens=120_000)
    )
    manuscript: ModelSettings = Field(
        default_factory=lambda: ModelSettings(max_output_tokens=120_000)
    )

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_research_role(cls, value: Any) -> Any:
        """Apply the old shared ``research`` model settings to both new roles.

        Role-specific values win field by field when a transitional configuration
        includes both the legacy table and one of the new tables.
        """

        if not isinstance(value, Mapping) or "research" not in value:
            return value
        normalized = copy.deepcopy(dict(value))
        legacy = normalized.pop("research")
        for role in ("research_coordinator", "research_worker"):
            explicit = normalized.get(role)
            if isinstance(legacy, Mapping) and isinstance(explicit, Mapping):
                normalized[role] = _deep_merge(dict(legacy), explicit)
            elif role not in normalized:
                normalized[role] = copy.deepcopy(legacy)
        return normalized

    @property
    def research(self) -> ModelSettings:
        """Compatibility view of the former shared research-worker settings."""

        return self.research_worker


class BackendSettings(_StrictSettings):
    """Model-execution provider selection and its no-fallback invariant."""

    provider: BackendProvider = "codex"
    allow_automatic_fallback: Literal[False] = False

    @field_validator("allow_automatic_fallback", mode="before")
    @classmethod
    def _fallback_is_never_automatic(cls, value: Any) -> Any:
        if value is not False:
            raise ValueError(
                "backend.allow_automatic_fallback must remain false; select the API "
                "backend explicitly to permit Platform API billing"
            )
        return value


class CodexLimits(_StrictSettings):
    """Subscription/credit limits, intentionally separate from API dollars."""

    max_agent_calls: int | None = Field(default=None, gt=0)
    max_research_coordinator_decisions: int = Field(default=256, gt=0)
    max_codex_threads: int | None = Field(default=None, gt=0)
    max_wall_clock_minutes: int | None = Field(default=None, gt=0)
    max_formalization_iterations: int = Field(default=60, gt=0)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_research_round_limit(cls, value: Any) -> Any:
        """Budget an old round as up to 32 completion-driven decisions."""

        if not isinstance(value, Mapping) or "max_research_rounds" not in value:
            return value
        normalized = copy.deepcopy(dict(value))
        legacy = normalized.pop("max_research_rounds")
        migrated = (
            legacy * 32 if isinstance(legacy, int) and not isinstance(legacy, bool) else legacy
        )
        normalized.setdefault("max_research_coordinator_decisions", migrated)
        return normalized

    @property
    def max_research_rounds(self) -> int:
        """Compatibility estimate using the historical 32-worker round capacity."""

        return (self.max_research_coordinator_decisions + 31) // 32


class CodexSettings(_StrictSettings):
    """Safe, backend-specific settings for official ``codex exec`` runs."""

    executable: str = "codex"
    model: str = "gpt-5.6-sol"
    research_coordinator_effort: Literal[
        "none", "minimal", "low", "medium", "high", "xhigh", "max"
    ] = "max"
    research_worker_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] = (
        "xhigh"
    )
    audit_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] = "xhigh"
    manuscript_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] = "high"
    formalization_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] = (
        "xhigh"
    )
    max_parallel_agents: int = Field(default=32, gt=0)
    max_parallel_web_agents: int = Field(default=32, gt=0)
    persist_sessions: bool = True
    skip_git_repo_check: bool = False
    extra_args: list[str] = Field(default_factory=list)
    limits: CodexLimits = Field(default_factory=CodexLimits)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_research_effort(cls, value: Any) -> Any:
        """Use a legacy shared effort for both research roles unless specialized."""

        if not isinstance(value, Mapping) or "research_effort" not in value:
            return value
        normalized = copy.deepcopy(dict(value))
        legacy = normalized.pop("research_effort")
        normalized.setdefault("research_coordinator_effort", legacy)
        normalized.setdefault("research_worker_effort", legacy)
        return normalized

    @field_validator("executable")
    @classmethod
    def _executable_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("codex.executable must not be blank")
        return normalized

    @field_validator("model")
    @classmethod
    def _codex_model_must_be_pinned(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(
                "codex.model must not be blank; durable request identity requires an explicit model"
            )
        return normalized

    @field_validator("extra_args")
    @classmethod
    def _extra_args_are_safe(cls, value: list[str]) -> list[str]:
        """Allow only presentation flags that cannot bypass ASCEND controls.

        Output locations, workspace, model, effort, authentication, search, sandbox,
        approval, and feature toggles are all owned by the adapter.  Keeping this a
        deliberately tiny positive allowlist prevents an innocent-looking config
        extension from weakening those invariants.
        """

        normalized = [part.strip() for part in value]
        if any(not part for part in normalized):
            raise ValueError("codex.extra_args cannot contain blank arguments")
        index = 0
        while index < len(normalized):
            argument = normalized[index]
            if argument.startswith("--color="):
                color = argument.partition("=")[2]
                if color not in {"always", "never", "auto"}:
                    raise ValueError("codex.extra_args --color must be always, never, or auto")
                index += 1
                continue
            if argument == "--color":
                if index + 1 >= len(normalized) or normalized[index + 1] not in {
                    "always",
                    "never",
                    "auto",
                }:
                    raise ValueError(
                        "codex.extra_args --color must be followed by always, never, or auto"
                    )
                index += 2
                continue
            raise ValueError(
                f"codex.extra_args contains unsupported or ASCEND-controlled argument: "
                f"{argument}; only --color always|never|auto is allowed"
            )
        return normalized

    @model_validator(mode="after")
    def web_parallelism_does_not_exceed_total(self) -> CodexSettings:
        if self.max_parallel_web_agents > self.max_parallel_agents:
            raise ValueError(
                "codex.max_parallel_web_agents cannot exceed codex.max_parallel_agents"
            )
        return self

    @property
    def research_effort(
        self,
    ) -> Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]:
        """Compatibility view of the former shared research-worker effort."""

        return self.research_worker_effort


class ResearchSettings(_StrictSettings):
    minimum_initial_agents: int = Field(default=16, ge=4)
    maximum_concurrent_agents: int = Field(default=32, gt=0)
    maximum_pending_assignments: int = Field(default=32, gt=0)
    maximum_coordinator_decisions: int = Field(default=256, gt=0)
    require_foundational_audit: Literal[True] = True
    require_domain_audit: Literal[True] = True
    require_hostile_audit: Literal[True] = True
    require_source_theorem_audit: Literal[True] = True

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_round_limits(cls, value: Any) -> Any:
        """Translate fixed-round limit names into continuous-scheduler controls."""

        if not isinstance(value, Mapping):
            return value
        normalized = copy.deepcopy(dict(value))
        missing = object()
        legacy_pending = normalized.pop("maximum_assignments_per_round", missing)
        legacy_rounds = normalized.pop("maximum_rounds", missing)
        if legacy_pending is not missing:
            normalized.setdefault("maximum_pending_assignments", legacy_pending)
        if legacy_rounds is not missing:
            pending = normalized.get("maximum_pending_assignments", 32)
            migrated = (
                legacy_rounds * pending
                if isinstance(legacy_rounds, int)
                and not isinstance(legacy_rounds, bool)
                and isinstance(pending, int)
                and not isinstance(pending, bool)
                else legacy_rounds
            )
            normalized.setdefault("maximum_coordinator_decisions", migrated)
        return normalized

    @model_validator(mode="after")
    def pending_assignment_cap_funds_initial_portfolio(self) -> ResearchSettings:
        if self.maximum_pending_assignments < self.minimum_initial_agents:
            raise ValueError(
                "research.maximum_pending_assignments (legacy "
                "maximum_assignments_per_round) cannot be less than "
                "research.minimum_initial_agents"
            )
        return self

    @property
    def maximum_assignments_per_round(self) -> int:
        """Compatibility name for the pending assignment-window limit."""

        return self.maximum_pending_assignments

    @property
    def maximum_rounds(self) -> int:
        """Compatibility estimate in full pending-window equivalents."""

        pending = self.maximum_pending_assignments
        return (self.maximum_coordinator_decisions + pending - 1) // pending


class ManuscriptSettings(_StrictSettings):
    enabled: bool = True
    latex_command: list[str] = Field(
        default_factory=lambda: ["latexmk", "-pdf", "-interaction=nonstopmode", "paper.tex"],
        min_length=1,
    )
    maximum_revision_rounds: int = Field(default=3, ge=0)
    require_verified_bibliography: bool = True
    require_related_work: bool = True

    @field_validator("latex_command")
    @classmethod
    def _command_parts_must_not_be_blank(cls, value: list[str]) -> list[str]:
        if any(not part.strip() for part in value):
            raise ValueError("manuscript.latex_command cannot contain blank arguments")
        return value


class LeanSettings(_StrictSettings):
    enabled: bool = True
    execution_backend: Literal["native", "docker"] = "native"
    docker_image: str = "ascend-math-agent:latest"
    allow_project_edits: bool = False
    codex_command: str = "codex"
    maximum_codex_iterations: int = Field(default=50, ge=0)
    prohibit_sorry: bool = True
    prohibit_admit: bool = True
    check_axioms: bool = True
    approved_axioms: list[str] = Field(
        default_factory=lambda: ["propext", "Classical.choice", "Quot.sound"]
    )

    @field_validator("codex_command", "docker_image")
    @classmethod
    def _commands_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Lean command and image settings must not be blank")
        return value.strip()

    @field_validator("approved_axioms")
    @classmethod
    def _axioms_must_be_unique_and_nonblank(cls, value: list[str]) -> list[str]:
        normalized = [axiom.strip() for axiom in value]
        if any(not axiom for axiom in normalized):
            raise ValueError("lean.approved_axioms cannot contain blank names")
        if len(normalized) != len(set(normalized)):
            raise ValueError("lean.approved_axioms cannot contain duplicates")
        return normalized

    @field_validator("allow_project_edits")
    @classmethod
    def _project_edits_require_explicit_cli_consent(cls, value: bool) -> bool:
        if value:
            raise ValueError(
                "lean.allow_project_edits cannot be enabled in configuration; "
                "use the explicit --allow-project-edits CLI flag"
            )
        return value


class Limits(_StrictSettings):
    maximum_cost_usd: float = Field(default=150.0, ge=0, allow_inf_nan=False)
    maximum_wall_clock_hours: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    maximum_api_retries: int = Field(default=4, ge=0)
    maximum_total_tokens: int | None = Field(default=None, gt=0)


class LoggingSettings(_StrictSettings):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    store_reasoning_summaries: bool = False


class ModelPricingSettings(_StrictSettings):
    """Standard API prices in USD per million tokens, plus per-tool-call fees."""

    input_per_million: float = Field(gt=0, allow_inf_nan=False)
    output_per_million: float = Field(gt=0, allow_inf_nan=False)
    cached_input_per_million: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    cache_write_per_million: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    web_search_per_call: float = Field(default=0.01, ge=0, allow_inf_nan=False)
    long_context_threshold: int | None = Field(default=None, gt=0)
    long_input_multiplier: float = Field(default=1.0, ge=1, allow_inf_nan=False)
    long_output_multiplier: float = Field(default=1.0, ge=1, allow_inf_nan=False)


def _default_model_pricing() -> dict[str, ModelPricingSettings]:
    return {
        "gpt-5.6-sol": ModelPricingSettings(
            input_per_million=5.0,
            cached_input_per_million=0.5,
            cache_write_per_million=6.25,
            output_per_million=30.0,
            web_search_per_call=0.01,
            long_context_threshold=272_000,
            long_input_multiplier=2.0,
            long_output_multiplier=1.5,
        ),
        "gpt-5.6-terra": ModelPricingSettings(
            input_per_million=2.5,
            cached_input_per_million=0.25,
            cache_write_per_million=3.125,
            output_per_million=15.0,
            web_search_per_call=0.01,
            long_context_threshold=272_000,
            long_input_multiplier=2.0,
            long_output_multiplier=1.5,
        ),
        "gpt-5.6-luna": ModelPricingSettings(
            input_per_million=1.0,
            cached_input_per_million=0.1,
            cache_write_per_million=1.25,
            output_per_million=6.0,
            web_search_per_call=0.01,
            long_context_threshold=272_000,
            long_input_multiplier=2.0,
            long_output_multiplier=1.5,
        ),
    }


class PricingSettings(_StrictSettings):
    as_of: str = "2026-07-19"
    models: dict[str, ModelPricingSettings] = Field(default_factory=_default_model_pricing)


class ApiSettings(_StrictSettings):
    """Existing Responses API configuration, namespaced without changing semantics."""

    max_parallel_agents: int = Field(default=32, gt=0)
    models: ModelsSettings = Field(default_factory=ModelsSettings)
    limits: Limits = Field(default_factory=Limits)
    pricing: PricingSettings = Field(default_factory=PricingSettings)


class GraphSettings(_StrictSettings):
    """Persistent Markdown knowledge-graph limits.

    The vault location is deliberately fixed beneath ``.ascend`` so routine runs
    preserve ASCEND's no-project-source-write guarantee. It remains a normal
    Obsidian vault and is independent of individual run directories.
    """

    maximum_context_nodes: int = Field(default=40, ge=4, le=200)
    maximum_context_characters: int = Field(default=60_000, ge=1_000, le=500_000)


class ConfigMigrationNotice(_StrictSettings):
    """A nonsecret runtime notice for one durable, user-facing migration message."""

    migration_id: Literal["backend-provider-v2"] = BACKEND_MIGRATION_ID
    message: str = BACKEND_MIGRATION_MESSAGE


class AppConfig(_StrictSettings):
    config_version: Literal[2] = CURRENT_CONFIG_VERSION
    backend: BackendSettings = Field(default_factory=BackendSettings)
    codex: CodexSettings = Field(default_factory=CodexSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    research: ResearchSettings = Field(default_factory=ResearchSettings)
    manuscript: ManuscriptSettings = Field(default_factory=ManuscriptSettings)
    lean: LeanSettings = Field(default_factory=LeanSettings)
    graph: GraphSettings = Field(default_factory=GraphSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    # Runtime context, not emitted into resolved TOML snapshots.
    project_root: Path | None = Field(default=None, exclude=True)
    migration_notice: ConfigMigrationNotice | None = Field(default=None, exclude=True)

    ENV_PREFIX: ClassVar[str] = "ASCEND_"

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_api_shape(cls, value: Any) -> Any:
        """Keep direct ``AppConfig.model_validate`` calls backward compatible."""

        if not isinstance(value, Mapping):
            return value
        migrated, inferred_api = _migrate_config_mapping(value)
        if inferred_api and "migration_notice" not in migrated:
            migrated["migration_notice"] = ConfigMigrationNotice().model_dump(mode="python")
        return migrated

    @model_validator(mode="after")
    def selected_models_have_budget_pricing(self) -> AppConfig:
        if self.backend.provider != "api":
            return self
        selected = {
            self.models.prompt_compiler.model,
            self.models.research_coordinator.model,
            self.models.research_worker.model,
            self.models.audit.model,
            self.models.manuscript.model,
        }
        missing = sorted(selected - self.pricing.models.keys())
        if missing:
            raise ValueError(
                "pricing.models must define standard API rates for selected model(s): "
                + ", ".join(missing)
            )
        return self

    # Compatibility conveniences preserve the v0.1 public API while persisted v2
    # configurations group direct-API-only settings under ``[api]``.
    @property
    def models(self) -> ModelsSettings:
        return self.api.models

    @property
    def limits(self) -> Limits:
        return self.api.limits

    @property
    def pricing(self) -> PricingSettings:
        return self.api.pricing

    @property
    def prompt_compiler(self) -> ModelSettings:
        return self.models.prompt_compiler

    @property
    def audit(self) -> ModelSettings:
        return self.models.audit

    @property
    def model_manuscript(self) -> ModelSettings:
        return self.models.manuscript

    @property
    def research_settings(self) -> ResearchSettings:
        return self.research

    @property
    def web_search_enabled(self) -> bool:
        """Whether any model stage is allowed to use live web search.

        The CLI-wide ``--no-web-search`` override disables every stage setting.  This
        aggregate is also the switch for ASCEND's deterministic identifier resolver,
        so a globally offline run cannot make an unexpected HTTP lookup outside the
        selected model adapter.
        """

        return any(
            settings.web_search
            for settings in (
                self.models.prompt_compiler,
                self.models.research_coordinator,
                self.models.research_worker,
                self.models.audit,
                self.models.manuscript,
            )
        )


_CONVENIENCE_PATHS: dict[str, tuple[str, ...]] = {
    "BACKEND": ("backend", "provider"),
    "BUDGET_USD": ("api", "limits", "maximum_cost_usd"),
    "MAXIMUM_COST_USD": ("api", "limits", "maximum_cost_usd"),
    "MAX_COORDINATOR_DECISIONS": ("research", "maximum_coordinator_decisions"),
    "MAXIMUM_COORDINATOR_DECISIONS": ("research", "maximum_coordinator_decisions"),
    "MAX_ROUNDS": ("research", "maximum_rounds"),
    "MAXIMUM_ROUNDS": ("research", "maximum_rounds"),
    "MAX_AGENTS": ("research", "maximum_concurrent_agents"),
    "MAXIMUM_CONCURRENT_AGENTS": ("research", "maximum_concurrent_agents"),
    "LEAN_ENABLED": ("lean", "enabled"),
    "SANDBOX": ("lean", "execution_backend"),
}

_CLI_PATHS: dict[str, tuple[str, ...]] = {
    "backend": ("backend", "provider"),
    "budget_usd": ("api", "limits", "maximum_cost_usd"),
    "max_coordinator_decisions": ("research", "maximum_coordinator_decisions"),
    "max_rounds": ("research", "maximum_rounds"),
    "max_agents": ("research", "maximum_concurrent_agents"),
    "sandbox": ("lean", "execution_backend"),
    "no_lean": ("lean", "enabled"),
}

_MODEL_STAGE_NAMES = (
    "prompt_compiler",
    "research_coordinator",
    "research_worker",
    "audit",
    "manuscript",
)


def _set_all_web_search(target: dict[str, Any], enabled: bool) -> None:
    for stage_name in _MODEL_STAGE_NAMES:
        _set_nested(target, ("api", "models", stage_name, "web_search"), enabled)


def _set_total_time_limit(target: dict[str, Any], minutes: int) -> None:
    _set_nested(target, ("codex", "limits", "max_wall_clock_minutes"), minutes)
    _set_nested(target, ("api", "limits", "maximum_wall_clock_hours"), minutes / 60.0)


_LEGACY_API_SECTIONS = ("models", "limits", "pricing")


def _deep_merge(base: dict[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge mappings without mutating either input."""

    merged = copy.deepcopy(base)
    for key, value in update.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(dict(merged[key]), value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _normalize_continuous_research_aliases(
    values: Mapping[str, Any],
    *,
    legacy_pending_capacity: int = 32,
) -> dict[str, Any]:
    """Normalize legacy fixed-round/shared-role keys in one precedence layer.

    Each source layer is migrated before it is merged with built-in defaults.  This is
    important: migrating only during final Pydantic validation would make a built-in
    role-specific default look explicit and could hide an older user setting.
    """

    normalized = copy.deepcopy(dict(values))

    api = normalized.get("api")
    if isinstance(api, Mapping):
        api_data = copy.deepcopy(dict(api))
        models = api_data.get("models")
        if isinstance(models, Mapping) and "research" in models:
            model_data = copy.deepcopy(dict(models))
            legacy = model_data.pop("research")
            for role in ("research_coordinator", "research_worker"):
                explicit = model_data.get(role)
                if isinstance(legacy, Mapping) and isinstance(explicit, Mapping):
                    model_data[role] = _deep_merge(dict(legacy), explicit)
                elif role not in model_data:
                    model_data[role] = copy.deepcopy(legacy)
            api_data["models"] = model_data
        normalized["api"] = api_data

    codex = normalized.get("codex")
    if isinstance(codex, Mapping):
        codex_data = copy.deepcopy(dict(codex))
        if "research_effort" in codex_data:
            legacy_effort = codex_data.pop("research_effort")
            codex_data.setdefault("research_coordinator_effort", legacy_effort)
            codex_data.setdefault("research_worker_effort", legacy_effort)
        limits = codex_data.get("limits")
        if isinstance(limits, Mapping) and "max_research_rounds" in limits:
            limit_data = copy.deepcopy(dict(limits))
            legacy_rounds = limit_data.pop("max_research_rounds")
            migrated_decisions = (
                legacy_rounds * 32
                if isinstance(legacy_rounds, int) and not isinstance(legacy_rounds, bool)
                else legacy_rounds
            )
            limit_data.setdefault("max_research_coordinator_decisions", migrated_decisions)
            codex_data["limits"] = limit_data
        normalized["codex"] = codex_data

    research = normalized.get("research")
    if isinstance(research, Mapping):
        research_data = copy.deepcopy(dict(research))
        missing = object()
        legacy_pending = research_data.pop("maximum_assignments_per_round", missing)
        legacy_rounds = research_data.pop("maximum_rounds", missing)
        if legacy_pending is not missing:
            research_data.setdefault("maximum_pending_assignments", legacy_pending)
        if legacy_rounds is not missing:
            pending = research_data.get("maximum_pending_assignments", legacy_pending_capacity)
            migrated_decisions = (
                legacy_rounds * pending
                if isinstance(legacy_rounds, int)
                and not isinstance(legacy_rounds, bool)
                and isinstance(pending, int)
                and not isinstance(pending, bool)
                else legacy_rounds
            )
            research_data.setdefault("maximum_coordinator_decisions", migrated_decisions)
        normalized["research"] = research_data

    return normalized


def _migrate_config_mapping(values: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """Upgrade a raw config mapping to schema v2 without dropping legacy API values.

    The boolean reports whether an API provider had to be inferred.  An explicit
    ``[backend]`` always wins, including in an otherwise legacy-shaped file.
    """

    migrated = copy.deepcopy(dict(values))
    raw_version = migrated.get("config_version")
    if isinstance(raw_version, bool):
        # Let strict Pydantic validation produce the normal field diagnostic.
        return migrated, False
    if isinstance(raw_version, int) and raw_version > CURRENT_CONFIG_VERSION:
        return migrated, False

    legacy_api: dict[str, Any] = {}
    for section in _LEGACY_API_SECTIONS:
        if section in migrated:
            legacy_api[section] = migrated.pop(section)

    existing_api = migrated.get("api", {})
    if legacy_api:
        if not isinstance(existing_api, Mapping):
            # Preserve the invalid value so validation reports the offending section.
            return migrated, False
        migrated["api"] = _deep_merge(legacy_api, existing_api)

    backend = migrated.get("backend")
    explicit_provider = isinstance(backend, Mapping) and "provider" in backend
    is_pre_v2 = raw_version is None or raw_version == 1
    inferred_api = bool(is_pre_v2 and (legacy_api or raw_version == 1) and not explicit_provider)
    if inferred_api:
        backend_data = dict(backend) if isinstance(backend, Mapping) else {}
        backend_data["provider"] = "api"
        migrated["backend"] = backend_data

    if is_pre_v2:
        migrated["config_version"] = CURRENT_CONFIG_VERSION
    return _normalize_continuous_research_aliases(migrated), inferred_api


def _v2_path(path: tuple[str, ...]) -> tuple[str, ...]:
    """Translate the v0.1 public API paths into their v2 ``api`` namespace."""

    if path and path[0] in _LEGACY_API_SECTIONS:
        return ("api", *path)
    return path


def _lookup_template(config: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = config
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            raise ConfigError(f"unknown configuration key: {'.'.join(path)}")
        current = current[part]
    return current


def _set_nested(target: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current = target
    for part in path[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            raise ConfigError(f"configuration key conflicts at {'.'.join(path)}")
        current = child
    current[path[-1]] = value


def _parse_environment_value(raw: str, template: Any, key: str) -> Any:
    """Parse an environment string according to the default value's strict type."""

    try:
        if isinstance(template, bool):
            normalized = raw.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
            raise ValueError("expected true/false")
        if isinstance(template, int) and not isinstance(template, bool):
            return int(raw.strip())
        if isinstance(template, float):
            return float(raw.strip())
        if isinstance(template, list):
            parsed = json.loads(raw)
            if not isinstance(parsed, list):
                raise ValueError("expected a JSON array")
            return parsed
        if template is None:
            normalized = raw.strip().lower()
            if normalized in {"none", "null", ""}:
                return None
            return int(raw.strip())
        return raw.strip()
    except (ValueError, json.JSONDecodeError) as exc:
        raise ConfigError(f"invalid value for environment variable {key}: {exc}") from exc


def _flat_environment_paths(
    values: Mapping[str, Any], prefix: tuple[str, ...] = ()
) -> dict[str, tuple[str, ...]]:
    paths: dict[str, tuple[str, ...]] = {}
    for key, value in values.items():
        path = (*prefix, key)
        if isinstance(value, Mapping):
            paths.update(_flat_environment_paths(value, path))
        else:
            paths["_".join(part.upper() for part in path)] = path
    return paths


def environment_overrides(
    environment: Mapping[str, str] | None = None,
    *,
    defaults: AppConfig | None = None,
    legacy_pending_capacity: int = 32,
) -> dict[str, Any]:
    """Convert supported ``ASCEND_*`` variables to a nested config mapping."""

    source = os.environ if environment is None else environment
    template = (defaults or AppConfig()).model_dump(mode="python", exclude={"project_root"})
    flat_paths = _flat_environment_paths(template)
    api_template = template.get("api")
    if isinstance(api_template, Mapping):
        for legacy_name in _LEGACY_API_SECTIONS:
            legacy_template = api_template.get(legacy_name)
            if isinstance(legacy_template, Mapping):
                flat_paths.update(_flat_environment_paths(legacy_template, prefix=(legacy_name,)))
    overrides: dict[str, Any] = {}
    for key, raw in source.items():
        if not key.startswith(AppConfig.ENV_PREFIX):
            continue
        suffix = key[len(AppConfig.ENV_PREFIX) :]
        if suffix in {"LIVE_TESTS", "CONFIG"}:
            # Operational switches are not application configuration.
            continue
        if suffix == "NO_LEAN":
            enabled = not _parse_environment_value(raw, False, key)
            _set_nested(overrides, ("lean", "enabled"), enabled)
            continue
        if suffix == "NO_WEB_SEARCH":
            disabled = _parse_environment_value(raw, False, key)
            _set_all_web_search(overrides, not disabled)
            continue
        if suffix == "TIME_LIMIT_MINUTES":
            try:
                minutes = int(raw.strip())
            except ValueError as exc:
                raise ConfigError(
                    f"invalid value for environment variable {key}: expected an integer"
                ) from exc
            if minutes < 1:
                raise ConfigError(f"invalid value for environment variable {key}: must be >= 1")
            _set_total_time_limit(overrides, minutes)
            continue
        canonical_suffix = suffix.replace("__", "_")
        legacy_model_tail: str | None = None
        for prefix in ("MODELS_RESEARCH_", "API_MODELS_RESEARCH_"):
            if canonical_suffix.startswith(prefix):
                candidate = canonical_suffix[len(prefix) :]
                model_fields = {
                    name.upper(): name for name in ModelSettings().model_dump(mode="python")
                }
                if candidate in model_fields:
                    legacy_model_tail = model_fields[candidate]
                break
        if legacy_model_tail is not None:
            role_template_path = (
                "api",
                "models",
                "research_worker",
                legacy_model_tail,
            )
            legacy_model_value = _parse_environment_value(
                raw,
                _lookup_template(template, role_template_path),
                key,
            )
            _set_nested(
                overrides,
                ("api", "models", "research", legacy_model_tail),
                legacy_model_value,
            )
            continue
        legacy_environment_paths: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
            "CODEX_RESEARCH_EFFORT": (
                ("codex", "research_effort"),
                ("codex", "research_worker_effort"),
            ),
            "CODEX_LIMITS_MAX_RESEARCH_ROUNDS": (
                ("codex", "limits", "max_research_rounds"),
                ("codex", "limits", "max_research_coordinator_decisions"),
            ),
            "RESEARCH_MAXIMUM_ASSIGNMENTS_PER_ROUND": (
                ("research", "maximum_assignments_per_round"),
                ("research", "maximum_pending_assignments"),
            ),
            "MAXIMUM_ASSIGNMENTS_PER_ROUND": (
                ("research", "maximum_assignments_per_round"),
                ("research", "maximum_pending_assignments"),
            ),
            "RESEARCH_MAXIMUM_ROUNDS": (
                ("research", "maximum_rounds"),
                ("research", "maximum_coordinator_decisions"),
            ),
            "MAX_ROUNDS": (
                ("research", "maximum_rounds"),
                ("research", "maximum_coordinator_decisions"),
            ),
            "MAXIMUM_ROUNDS": (
                ("research", "maximum_rounds"),
                ("research", "maximum_coordinator_decisions"),
            ),
        }
        legacy_paths = legacy_environment_paths.get(canonical_suffix)
        if legacy_paths is not None:
            legacy_path, legacy_template_path = legacy_paths
            legacy_value = _parse_environment_value(
                raw,
                _lookup_template(template, legacy_template_path),
                key,
            )
            _set_nested(overrides, legacy_path, legacy_value)
            continue
        path = _CONVENIENCE_PATHS.get(suffix) or flat_paths.get(suffix)
        if path is None:
            if "__" not in suffix:
                # Ignore unrelated ASCEND process flags.  Nested config-like keys,
                # however, are rejected below so misspellings cannot silently pass.
                continue
            path = tuple(part.lower() for part in suffix.split("__") if part)
        path = _v2_path(path)
        expected = _lookup_template(template, path)
        if path[-1:] == ("maximum_wall_clock_hours",):
            try:
                parsed_value: Any = float(raw.strip())
            except ValueError as exc:
                raise ConfigError(
                    f"invalid value for environment variable {key}: expected a number"
                ) from exc
        else:
            parsed_value = _parse_environment_value(raw, expected, key)
        _set_nested(overrides, path, parsed_value)
    return _normalize_continuous_research_aliases(
        overrides,
        legacy_pending_capacity=legacy_pending_capacity,
    )


def normalize_cli_overrides(
    overrides: ConfigMapping | None,
    *,
    legacy_pending_capacity: int = 32,
) -> dict[str, Any]:
    """Normalize nested, dotted, or common CLI option names.

    ``None`` values are omitted, matching the usual semantics of optional Typer
    parameters.  Values otherwise remain typed so Pydantic's strict validation can
    catch accidental string-to-number coercion.
    """

    normalized: dict[str, Any] = {}
    if not overrides:
        return normalized

    def clean_nested(mapping: Mapping[str, Any], prefix: tuple[str, ...] = ()) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        for nested_key, nested_value in mapping.items():
            if nested_value is None:
                continue
            key_path = (
                (nested_key,)
                if prefix in {("pricing", "models"), ("api", "pricing", "models")}
                else tuple(nested_key.split("."))
            )
            value = (
                clean_nested(nested_value, (*prefix, *key_path))
                if isinstance(nested_value, Mapping)
                else nested_value
            )
            _set_nested(cleaned, key_path, value)
        return cleaned

    for key, value in overrides.items():
        if value is None:
            continue
        if key == "no_web_search":
            if not isinstance(value, bool):
                raise ConfigError("CLI override no_web_search must be a boolean")
            _set_all_web_search(normalized, not value)
            continue
        if key == "time_limit_minutes":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError("CLI override time_limit_minutes must be an integer")
            if value < 1:
                raise ConfigError("CLI override time_limit_minutes must be at least 1")
            _set_total_time_limit(normalized, value)
            continue
        if isinstance(value, Mapping):
            path = _v2_path(tuple(key.split(".")))
            _set_nested(normalized, path, clean_nested(value, path))
            continue
        path = _v2_path(_CLI_PATHS.get(key, tuple(key.split("."))))
        if key == "no_lean":
            if not isinstance(value, bool):
                raise ConfigError("CLI override no_lean must be a boolean")
            value = not value
        _set_nested(normalized, path, value)
    return _normalize_continuous_research_aliases(
        normalized,
        legacy_pending_capacity=legacy_pending_capacity,
    )


def merge_config(config: AppConfig, overrides: ConfigMapping | None) -> AppConfig:
    """Return a validated copy of ``config`` with CLI-style overrides applied."""

    data = config.model_dump(mode="python")
    merged = _deep_merge(
        data,
        normalize_cli_overrides(
            overrides,
            legacy_pending_capacity=config.research.maximum_pending_assignments,
        ),
    )
    try:
        result = AppConfig.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration overrides: {exc}") from exc
    result.project_root = config.project_root
    result.migration_notice = config.migration_notice
    return result


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            contents = tomllib.load(stream)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file does not exist: {path}") from exc
    except PermissionError as exc:
        raise ConfigError(f"configuration file is not readable: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    if not isinstance(contents, dict):  # pragma: no cover - tomllib guarantees this
        raise ConfigError(f"configuration root must be a table: {path}")
    return contents


def find_config_file(start: Path) -> Path | None:
    """Find the nearest ``ascend.toml`` at or above ``start``."""

    resolved = start.resolve()
    if resolved.is_file():
        resolved = resolved.parent
    for candidate in (resolved, *resolved.parents):
        path = candidate / "ascend.toml"
        if path.is_file():
            return path
    return None


def load_config(
    path: Path | None = None,
    *,
    project_root: Path | None = None,
    env: Mapping[str, str] | None = None,
    cli_overrides: ConfigMapping | None = None,
) -> AppConfig:
    """Load and validate resolved application configuration.

    An explicit ``path`` is required to exist.  Without one, ``ascend.toml`` is
    searched for from ``project_root`` (or the current directory); absence simply
    selects built-in defaults.
    """

    defaults = AppConfig()
    data = defaults.model_dump(mode="python", exclude={"project_root"})
    migration_notice: ConfigMigrationNotice | None = None

    explicit_path = path.expanduser().resolve() if path is not None else None
    discovered_path = explicit_path or find_config_file(project_root or Path.cwd())
    if discovered_path is not None:
        file_data, inferred_api = _migrate_config_mapping(_read_toml(discovered_path))
        data = _deep_merge(data, file_data)
        if inferred_api:
            migration_notice = ConfigMigrationNotice()

    file_research = data.get("research")
    file_pending = (
        file_research.get("maximum_pending_assignments", 32)
        if isinstance(file_research, Mapping)
        else 32
    )
    if not isinstance(file_pending, int) or isinstance(file_pending, bool):
        file_pending = 32
    data = _deep_merge(
        data,
        environment_overrides(
            env,
            defaults=defaults,
            legacy_pending_capacity=file_pending,
        ),
    )
    resolved_research = data.get("research")
    resolved_pending = (
        resolved_research.get("maximum_pending_assignments", 32)
        if isinstance(resolved_research, Mapping)
        else 32
    )
    if not isinstance(resolved_pending, int) or isinstance(resolved_pending, bool):
        resolved_pending = 32
    data = _deep_merge(
        data,
        normalize_cli_overrides(
            cli_overrides,
            legacy_pending_capacity=resolved_pending,
        ),
    )

    root = project_root
    if root is None and discovered_path is not None:
        root = discovered_path.parent
    if root is not None:
        data["project_root"] = root.expanduser().resolve()
    if migration_notice is not None:
        data["migration_notice"] = migration_notice.model_dump(mode="python")

    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        source = discovered_path or "built-in/environment/CLI configuration"
        raise ConfigError(f"invalid configuration from {source}: {exc}") from exc


def resolve_backend_provider(
    config: AppConfig,
    *,
    cli_provider: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> BackendProvider:
    """Resolve CLI > ``ASCEND_BACKEND`` > config > Codex without fallback."""

    source = os.environ if environment is None else environment
    candidate = cli_provider if cli_provider is not None else source.get("ASCEND_BACKEND")
    if candidate is None:
        return config.backend.provider
    normalized = candidate.strip()
    if normalized not in {"codex", "api"}:
        raise ConfigError(f"invalid backend provider {candidate!r}; expected 'codex' or 'api'")
    return cast(BackendProvider, normalized)


def consume_config_migration_notice(config: AppConfig) -> str | None:
    """Return a legacy migration notice once per project, using a durable marker.

    Loading configuration remains read-only.  A CLI surface calls this helper when it
    is ready to display notices; the helper creates no marker when no migration was
    needed.  If a marker cannot be persisted safely, the notice is returned rather
    than silently suppressed.
    """

    notice = config.migration_notice
    if notice is None:
        return None
    if config.project_root is None:
        return notice.message

    root = config.project_root.expanduser().resolve()
    ascend_dir = root / ".ascend"
    marker_dir = ascend_dir / "config-migrations"
    for directory in (ascend_dir, marker_dir):
        try:
            if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
                return notice.message
            directory.mkdir(mode=0o700, exist_ok=True)
        except OSError:
            return notice.message

    marker = marker_dir / notice.migration_id
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(marker, flags, 0o600)
    except FileExistsError:
        return None
    except OSError:
        return notice.message
    try:
        os.write(descriptor, f"{notice.message}\n".encode())
    finally:
        os.close(descriptor)
    return notice.message


def config_as_toml(config: AppConfig) -> str:
    """Serialize a resolved, secret-free config snapshot as TOML."""

    try:
        import tomli_w
    except ImportError as exc:  # pragma: no cover - declared runtime dependency
        raise ConfigError("tomli-w is required to serialize configuration") from exc
    data = config.model_dump(mode="python", exclude={"project_root"}, exclude_none=True)
    return tomli_w.dumps(data)


__all__ = [
    "BACKEND_MIGRATION_ID",
    "BACKEND_MIGRATION_MESSAGE",
    "CURRENT_CONFIG_VERSION",
    "ApiSettings",
    "AppConfig",
    "BackendProvider",
    "BackendSettings",
    "CodexLimits",
    "CodexSettings",
    "ConfigError",
    "ConfigMigrationNotice",
    "GraphSettings",
    "LeanSettings",
    "Limits",
    "LoggingSettings",
    "ManuscriptSettings",
    "ModelPricingSettings",
    "ModelSettings",
    "ModelsSettings",
    "PricingSettings",
    "ResearchSettings",
    "config_as_toml",
    "consume_config_migration_notice",
    "environment_overrides",
    "find_config_file",
    "load_config",
    "merge_config",
    "normalize_cli_overrides",
    "resolve_backend_provider",
]
