"""Typed command-line interface for MATEK."""

from __future__ import annotations

import asyncio
import json
import sys
import tomllib
from collections.abc import Coroutine, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, NoReturn, TypeVar, cast

import typer
from pydantic import BaseModel
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .application import (
    LeanConsentOutcome,
    LeanConsentRequest,
    RunNotFoundError,
    WorkflowDependencies,
    WorkflowError,
    WorkflowOptions,
    WorkflowResult,
    WorkflowRunner,
    resolve_run_root,
)
from .budget import BudgetExceeded
from .codex_client import CodexAdapterError, CodexClient, CodexExecClient
from .codex_model_backend import CodexBackendError, CodexCliModelClient
from .config import (
    AppConfig,
    ConfigError,
    consume_config_migration_notice,
    load_config,
    merge_config,
)
from .doctor import CheckLevel, DoctorGroup, run_doctor_checks
from .execution.base import ExecutionBackend
from .execution.docker import DockerBackend
from .execution.native import NativeBackend
from .initialization import InitializationError, initialize_project
from .intake import IntakeError, normalize_problem_text
from .knowledge_graph import (
    GraphNode,
    GraphNotInitializedError,
    GraphValidationError,
    KnowledgeGraph,
    KnowledgeGraphError,
    NodeType,
    RelationType,
    list_graph_names,
    normalize_graph_name,
    problem_graph_name,
)
from .knowledge_graph.migration import (
    LegacyMigrationApplicationRecord,
    LegacyMigrationError,
    LegacyMigrationReport,
    load_legacy_migration_report,
    migration_application_sha256,
    migration_report_sha256,
    plan_legacy_graph_backfill,
    write_legacy_migration_report,
)
from .logging import JournalCorruptionError
from .models import RunState, StageName, StageStatus
from .openai_client import (
    ModelAdapterError,
    ModelClient,
    OpenAIResponsesClient,
    TokenPricing,
)
from .progress import Ascension
from .redaction import redact_text
from .reporting import FinalReport, blocking_failure_summary
from .resources import resource_path
from .stages.compile_prompt import EXPECTED_FRAMEWORK_SHA256
from .state import (
    ArtifactIntegrityError,
    StateCorruptionError,
    StateError,
    StateStore,
    first_incomplete_stage,
)
from .workspace import (
    RunLock,
    WorkspaceError,
    atomic_write_bytes,
    discover_project_root,
    latest_run_root_for_problem,
    list_run_roots,
    sha256_file,
)

app = typer.Typer(
    no_args_is_help=True,
    help="MATEK: auditable mathematical research and optional Lean verification.",
)
graph_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect and maintain the persistent Obsidian-compatible knowledge graph.",
)
app.add_typer(graph_app, name="graph")
console = Console()

T = TypeVar("T")


class SandboxChoice(StrEnum):
    NATIVE = "native"
    DOCKER = "docker"


class BackendChoice(StrEnum):
    CODEX = "codex"
    API = "api"


class GraphExportChoice(StrEnum):
    JSON = "json"
    GRAPHVIZ = "graphviz"
    MERMAID = "mermaid"


class _OfflineModelClient:
    async def generate_structured(
        self, request: Any, output_type: type[BaseModel]
    ) -> Any:  # pragma: no cover - defensive tripwire
        del request, output_type
        raise RuntimeError("offline report service attempted a model call")


class _OfflineCodexClient:
    async def execute(self, request: Any) -> Any:  # pragma: no cover - defensive tripwire
        del request
        raise RuntimeError("offline report service attempted a Codex call")


def _run_async(awaitable: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(awaitable)


def _project_root() -> Path:
    return discover_project_root(Path.cwd())


def _print_progress(ascension: Ascension, message: str) -> None:
    console.print(f"[bold cyan]ASCENSION {int(ascension)}:[/bold cyan] {message}")


async def _terminal_lean_consent(request: LeanConsentRequest) -> LeanConsentOutcome:
    """Ask on an interactive terminal without blocking the workflow event loop."""

    console.print(
        "[bold]The accepted result is ready.[/bold] Proceed with formal Lean "
        "verification? [Y/n] "
        f"(automatically proceeds after {request.timeout_seconds // 60} minutes): ",
        end="",
    )
    if not sys.stdin.isatty():
        console.print("input is noninteractive; proceeding with Lean verification.")
        return LeanConsentOutcome.NON_INTERACTIVE

    loop = asyncio.get_running_loop()
    response: asyncio.Future[str] = loop.create_future()

    def input_ready() -> None:
        if response.done():
            return
        try:
            response.set_result(sys.stdin.readline())
        except Exception as exc:  # pragma: no cover - terminal I/O failure
            response.set_exception(exc)

    try:
        descriptor = sys.stdin.fileno()
        loop.add_reader(descriptor, input_ready)
    except (AttributeError, NotImplementedError, OSError, ValueError):
        console.print("timed input is unavailable; proceeding with Lean verification.")
        return LeanConsentOutcome.NON_INTERACTIVE

    try:
        answer = (await response).strip().casefold()
    finally:
        loop.remove_reader(descriptor)

    if answer in {"n", "no"}:
        console.print("Lean verification was declined; preparing the final report.")
        return LeanConsentOutcome.USER_DECLINED
    if not answer:
        console.print("proceeding with Lean verification.")
    elif answer not in {"y", "yes"}:
        console.print("unrecognized response; using the default and proceeding with Lean.")
    return LeanConsentOutcome.USER_APPROVED


def _execution_backend(config: AppConfig) -> ExecutionBackend:
    if config.lean.execution_backend == SandboxChoice.DOCKER.value:
        return DockerBackend(image=config.lean.docker_image)
    return NativeBackend()


def _live_runner(config: AppConfig) -> WorkflowRunner:
    backend = _execution_backend(config)
    if config.backend.provider == "codex":
        workspace_root = (config.project_root or _project_root()).expanduser().resolve(strict=True)
        model_client: ModelClient = CodexCliModelClient(
            workspace_root,
            executable=config.codex.executable,
            # Every durable request carries its explicit model. Leaving the adapter
            # unpinned permits the configured Terra diagnostic role without changing
            # the selected Codex backend or any billing boundary.
            model=None,
            persist_sessions=config.codex.persist_sessions,
            skip_git_repo_check=config.codex.skip_git_repo_check,
            extra_args=config.codex.extra_args,
        )
    else:
        pricing = {
            model: TokenPricing(**settings.model_dump(mode="python"))
            for model, settings in config.pricing.models.items()
        }
        model_client = OpenAIResponsesClient(
            max_attempts=config.limits.maximum_api_retries + 1,
            pricing=pricing,
        )
    return WorkflowRunner(
        config,
        WorkflowDependencies(
            model_client=model_client,
            execution_backend=backend,
            # Codex itself owns a host-side workspace sandbox; the configured
            # execution backend remains responsible for Lean/LaTeX commands.
            codex_client=CodexExecClient(
                NativeBackend(),
                executable=(
                    config.codex.executable
                    if config.backend.provider == "codex"
                    else config.lean.codex_command
                ),
                model=config.codex.model,
                reasoning_effort=config.codex.formalization_effort,
            ),
            progress=_print_progress,
            lean_consent=_terminal_lean_consent,
        ),
    )


def _offline_runner(config: AppConfig) -> WorkflowRunner:
    return WorkflowRunner(
        config,
        WorkflowDependencies(
            model_client=cast(ModelClient, _OfflineModelClient()),
            execution_backend=NativeBackend(),
            codex_client=cast(CodexClient, _OfflineCodexClient()),
            progress=_print_progress,
        ),
    )


def _error_code(exc: BaseException) -> int:
    if isinstance(exc, KeyboardInterrupt | asyncio.CancelledError):
        return 130
    if isinstance(exc, BudgetExceeded):
        return 5
    if isinstance(exc, (ArtifactIntegrityError, StateCorruptionError, JournalCorruptionError)):
        return 6
    if isinstance(exc, (GraphValidationError, LegacyMigrationError)):
        return 6
    if isinstance(exc, CodexBackendError):
        return 3
    if isinstance(exc, ModelAdapterError):
        return 4
    if isinstance(exc, (CodexAdapterError, FileNotFoundError, PermissionError, OSError)):
        return 3
    if isinstance(
        exc,
        (
            ConfigError,
            IntakeError,
            InitializationError,
            RunNotFoundError,
            WorkflowError,
            WorkspaceError,
            KnowledgeGraphError,
            ValueError,
        ),
    ):
        return 2
    if isinstance(exc, StateError):
        return 6
    return 1


def _abort(exc: BaseException, *, verbose: bool = False) -> NoReturn:
    code = _error_code(exc)
    message = redact_text(str(exc)).strip() or type(exc).__name__
    console.print("[red]Error:[/red] ", end="")
    console.print(message, markup=False)
    explanation = getattr(exc, "matek_user_explanation", None)
    if isinstance(explanation, dict) and explanation.get("available") is True:
        console.print("[bold]What happened:[/bold] ", end="")
        console.print(str(explanation.get("explanation", "")), markup=False)
        console.print("[bold]Suggested resolution:[/bold] ", end="")
        console.print(str(explanation.get("suggested_resolution", "")), markup=False)
    if verbose:
        console.print(f"[dim]Exception type: {type(exc).__name__}; exit code: {code}[/dim]")
    raise typer.Exit(code=code)


def _config_overrides(
    *,
    backend: BackendChoice | None = None,
    budget_usd: float | None = None,
    max_coordinator_decisions: int | None = None,
    max_rounds: int | None = None,
    num_first_level_agents: int | None = None,
    max_concurrent_agents: int | None = None,
    max_agents: int | None = None,
    hierarchical: bool | None = None,
    flat: bool | None = None,
    subagents_per_agent: int | None = None,
    time_limit_minutes: int | None = None,
    no_lean: bool | None = None,
    no_web_search: bool | None = None,
    sandbox: SandboxChoice | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    if max_coordinator_decisions is not None and max_rounds is not None:
        raise ConfigError(
            "--max-coordinator-decisions and deprecated --max-rounds cannot be combined"
        )
    if max_concurrent_agents is not None and max_agents is not None:
        raise ConfigError("--max-concurrent-agents and deprecated --max-agents cannot be combined")
    if hierarchical and flat:
        raise ConfigError("--hierarchical and --flat cannot be combined")
    return {
        "backend": backend.value if backend is not None else None,
        "budget_usd": budget_usd,
        "max_coordinator_decisions": max_coordinator_decisions,
        "max_rounds": max_rounds,
        "num_first_level_agents": num_first_level_agents,
        "max_concurrent_agents": max_concurrent_agents,
        "max_agents": max_agents,
        "research_mode": (
            "flat"
            if flat
            else (
                "hierarchical" if hierarchical is True or subagents_per_agent is not None else None
            )
        ),
        "subagents_per_agent": subagents_per_agent,
        "time_limit_minutes": time_limit_minutes,
        "no_lean": True if no_lean else None,
        "no_web_search": True if no_web_search else None,
        "sandbox": sandbox.value if sandbox is not None else None,
        "logging": {"level": "DEBUG"} if verbose else None,
    }


def _time_limit_display(config: AppConfig) -> str:
    minutes: float | None
    if config.backend.provider == "codex":
        codex_minutes = config.codex.limits.max_wall_clock_minutes
        minutes = None if codex_minutes is None else float(codex_minutes)
    else:
        hours = config.limits.maximum_wall_clock_hours
        minutes = None if hours is None else hours * 60
    if minutes is None:
        return "unlimited"
    if minutes % 60 == 0:
        display_hours = minutes / 60
        return f"{display_hours:g} hour{'s' if display_hours != 1 else ''}"
    return f"{minutes:g} minutes"


def _model_role_display(config: AppConfig, role: str) -> str:
    """Describe the model settings actually used by the selected backend."""

    settings = getattr(config.models, role)
    if config.backend.provider == "api":
        return (
            f"{settings.model} · {settings.reasoning_mode} mode · "
            f"{settings.reasoning_effort} effort"
        )
    efforts = {
        "prompt_compiler": config.codex.research_worker_effort,
        "research_coordinator": config.codex.research_coordinator_effort,
        "research_worker": config.codex.research_worker_effort,
        "audit": config.codex.audit_effort,
        "manuscript": config.codex.manuscript_effort,
    }
    return f"{config.codex.model} · {efforts[role]} effort"


def _effective_research_concurrency(config: AppConfig) -> tuple[int, str]:
    configured = config.effective_max_concurrent_first_level_agents
    effective = config.effective_research_model_call_concurrency
    if config.backend.provider == "codex":
        ceilings = (
            f"research-agent capacity {config.research.max_concurrent_agents} "
            f"=> {configured} first-level, Codex "
            f"{config.codex.max_concurrent_model_calls}, "
            f"web {config.codex.max_concurrent_web_model_calls}"
        )
    else:
        ceilings = (
            f"research-agent capacity {config.research.max_concurrent_agents} "
            f"=> {configured} first-level, API {config.api.max_concurrent_model_calls}"
        )
    return effective, ceilings


def _resolved_run_summary(
    config: AppConfig,
    *,
    graph_name: str,
    research_only: bool,
    no_lean: bool,
    allow_project_edits: bool,
) -> Mapping[str, object]:
    """Return the important effective settings shown before a run starts."""

    concurrency, concurrency_ceilings = _effective_research_concurrency(config)
    coordinator_decisions = config.research.max_coordinator_decisions
    if config.backend.provider == "codex":
        coordinator_decisions = min(
            coordinator_decisions,
            config.codex.limits.max_research_coordinator_decisions,
        )
    web_state = "enabled per stage" if config.web_search_enabled else "disabled globally"
    coordinator_web = "on" if config.models.research_coordinator.web_search else "off"
    worker_web = "on" if config.models.research_worker.web_search else "off"
    source_web = "on" if config.web_search_enabled else "off"
    usage_limit = (
        (
            f"{config.codex.limits.max_agent_calls} Codex agent calls"
            if config.codex.limits.max_agent_calls is not None
            else "no configured Codex call-count limit"
        )
        if config.backend.provider == "codex"
        else f"${config.limits.maximum_cost_usd:g} API spend"
    )
    nested_limit = config.effective_hierarchical_subagent_limit
    research_organization = (
        f"hierarchical · {config.research.max_concurrent_agents} total reserved agent slots · "
        f"up to {concurrency} concurrent first-level agents · "
        f"up to {nested_limit} Codex subagents per first-level agent · "
        "one nested tier"
        if nested_limit > 0
        else (
            "flat · regular research agents without nested delegation · "
            "Responses API adapter has no nested-agent tool"
            if config.backend.provider == "api"
            else "flat · regular research agents without nested delegation"
        )
    )
    return {
        "model backend": (
            "codex — Codex CLI (no automatic API fallback)"
            if config.backend.provider == "codex"
            else "api — OpenAI Responses API (explicit selection)"
        ),
        "prompt compiler": _model_role_display(config, "prompt_compiler"),
        "research coordinator": _model_role_display(config, "research_coordinator"),
        "research agents": _model_role_display(config, "research_worker"),
        "independent audits": _model_role_display(config, "audit"),
        "web access": (
            f"{web_state} · coordinator {coordinator_web} · research agents {worker_web} · "
            f"source lookup {source_web}"
        ),
        "initial first-level assignments": (
            f"{config.research.num_first_level_agents} first-level assignments"
        ),
        "research organization": research_organization,
        "concurrent first-level agents": (
            f"up to {concurrency} effective ({concurrency_ceilings})"
        ),
        "maximum pending assignments": config.research.max_pending_assignments,
        "coordinator decision limit": coordinator_decisions,
        "coordinator context budget": (
            f"{config.research.max_coordinator_context_characters:,} serialized provider "
            f"characters; up to {config.research.max_coordinator_requested_artifacts} "
            "on-demand evidence requests; "
            f"{config.research.max_unrequested_full_graph_node_characters:,} optional "
            "full-graph characters"
        ),
        "total active time limit": _time_limit_display(config),
        "usage limit": usage_limit,
        "knowledge graph": graph_name,
        "manuscript stage": (
            "enabled" if config.manuscript.enabled and not research_only else "off"
        ),
        "Lean stage": (
            "enabled" if config.lean.enabled and not no_lean and not research_only else "off"
        ),
        "execution backend": config.lean.execution_backend,
        "project source edits": "allowed" if allow_project_edits else "blocked",
    }


def _print_settings_table(title: str, settings: Mapping[str, object]) -> None:
    table = Table(title=title)
    table.add_column("Setting")
    table.add_column("Resolved value")
    for key, value in settings.items():
        table.add_row(key, str(value))
    console.print(table)


def _show_migration_notice(config: AppConfig) -> None:
    notice = consume_config_migration_notice(config)
    if notice is not None:
        console.print(f"[yellow]Configuration migration:[/yellow] {notice}")


def _effective_run_config_path(run_root: Path) -> Path:
    effective = run_root / "config" / "effective_config.toml"
    return effective if effective.is_file() else run_root / "input" / "config.resolved.toml"


def _resolve_resume_selector(project_root: Path, selector: str | None) -> Path:
    """Resolve a run ID, or the latest run created from a named problem file."""

    if selector is not None and Path(selector).suffix.lower() in {".md", ".txt"}:
        return latest_run_root_for_problem(project_root, Path(selector))
    return resolve_run_root(project_root, selector)


def _validate_problem_for_dry_run(problem_file: Path) -> str:
    source = problem_file.expanduser().resolve(strict=True)
    if not source.is_file():
        raise IntakeError(f"problem path is not a regular file: {problem_file}")
    if source.suffix.lower() not in {".md", ".txt"}:
        raise IntakeError("problem file must use a .md or .txt extension")
    try:
        content = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise IntakeError(f"problem file is not valid UTF-8: {source}") from exc
    return normalize_problem_text(content)


def _compact_terminal_text(value: str, *, limit: int = 600) -> str:
    """Keep the terminal handoff readable without changing the persisted report."""

    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _problem_resolution_summary(report: FinalReport) -> str:
    if report.scientific_status == "RESEARCH_ACCEPTED_FOR_MANUSCRIPT":
        return "YES — the exact research result passed the mandatory acceptance gate."
    if report.scientific_status == "RESEARCH_REJECTED":
        return "NO — MATEK did not establish an accepted proof; the research result was rejected."
    if report.scientific_status == "NEEDS_PROBLEM_CLARIFICATION":
        return "UNDETERMINED — the target must be clarified before research can start."
    if report.scientific_status == "CANDIDATE_AWAITING_AUDIT":
        return "NOT YET — a candidate exists but mandatory audits are still incomplete."
    return "NOT YET — no proof of the exact target has passed the acceptance gate."


def _workflow_stop_summary(report: FinalReport) -> str:
    if report.problem_clarification.get("required") is True:
        return "Prompt compilation: the mathematical target was ambiguous."
    checkpoint = report.research_checkpoint
    if report.workflow_status == "PAUSED_RETRIABLE":
        phase = checkpoint.get("phase") if isinstance(checkpoint, dict) else None
        suffix = f" (scheduler phase: {phase})" if phase else ""
        return f"Paused retriably during research{suffix}."
    failed = [
        stage.replace("_", " ")
        for stage, status in report.stage_statuses.items()
        if status in {"failed", "interrupted", "running"}
    ]
    if failed:
        return f"Stopped at {', '.join(failed)}."
    if report.scientific_status in {"RESEARCH_PARTIAL", "RESEARCH_REJECTED"}:
        return "Research ended without an accepted proof of the exact target."
    if report.workflow_status == "COMPLETE_WITH_WARNINGS":
        return "All reachable stages finished; warnings or publication blockers remain."
    return "All configured and reachable workflow stages finished."


def _research_activity_summary(report: FinalReport) -> str:
    checkpoint = report.research_checkpoint
    if not isinstance(checkpoint, dict) or not checkpoint:
        return "Research did not produce a scheduler checkpoint."
    assignments = checkpoint.get("assignments", {})
    completed = assignments.get("completed", 0) if isinstance(assignments, dict) else 0
    decisions = checkpoint.get("coordinator_decisions", 0)
    rejected = checkpoint.get("rejected_candidates", 0)
    completed_audits = checkpoint.get("completed_audits", [])
    audits = ", ".join(str(item) for item in completed_audits) or "none"
    return (
        f"{completed} worker report(s); {decisions} coordinator decision(s); "
        f"{rejected} rejected candidate(s); completed audits: {audits}."
    )


def _terminal_summary_rows(result: WorkflowResult) -> list[tuple[str, str]]:
    report = result.report.report
    completed_stages = [
        stage.replace("_", " ")
        for stage, status in report.stage_statuses.items()
        if status == "succeeded"
    ]
    skipped_stages = [
        str(item.get("stage", "unknown")).replace("_", " ") for item in report.skipped_stages
    ]
    next_action = report.resume_action
    if not next_action and report.problem_clarification.get("required") is True:
        next_action = "Clarify the problem statement and start a new run."
    if not next_action and report.retriable_actions:
        next_action = report.retriable_actions[0]
    if not next_action:
        next_action = "Review the full report and retained artifacts."

    rows = [
        ("Run", result.state.run_id),
        ("Problem solved?", _problem_resolution_summary(report)),
        ("Where it stopped", _workflow_stop_summary(report)),
        ("Scientific", report.scientific_status),
        ("Workflow", report.workflow_status),
        ("Manuscript", report.manuscript_status),
        ("Publication", report.publication_status),
        ("Lean", report.lean_status),
        ("Research performed", _research_activity_summary(report)),
        ("Completed stages", ", ".join(completed_stages) or "none"),
    ]
    if skipped_stages:
        rows.append(("Skipped stages", ", ".join(skipped_stages)))
    rows.extend(
        [
            ("Strongest result", _compact_terminal_text(report.strongest_result)),
            ("Next action", _compact_terminal_text(next_action)),
        ]
    )
    return rows


def _print_result(result: WorkflowResult) -> None:
    table = Table(title="MATEK run summary", show_header=False)
    table.add_column("Item", style="bold cyan", no_wrap=True)
    table.add_column("Result")
    for label, value in _terminal_summary_rows(result):
        table.add_row(Text(label), Text(value))
    console.print(table)
    report_path = Text("Full report: ", style="bold cyan")
    report_path.append(str(result.report.report_markdown))
    console.print(report_path, soft_wrap=True)
    artifact_path = Text("Run artifacts: ", style="bold cyan")
    artifact_path.append(str(result.state.run_root))
    console.print(artifact_path, soft_wrap=True)

    backend = result.state.metadata.get("backend", {})
    if isinstance(backend, dict):
        console.print(f"Backend: {backend.get('display_name', backend.get('provider', 'unknown'))}")
    clarification = result.report.report.problem_clarification
    if clarification.get("required") is True:
        console.print(
            "[yellow]MATEK stopped before research because it could not uniquely identify "
            "the mathematical problem to solve.[/yellow]"
        )
        reason = clarification.get("reason")
        if reason:
            console.print(f"Reason: {reason}")
        questions = clarification.get("questions", [])
        if isinstance(questions, list):
            for question in questions:
                console.print(f"  - {question}")
        console.print(
            "Revise the problem file with the requested details, then start a new run with "
            "[bold]matek run PROBLEM_FILE[/bold]."
        )
    obligations = result.report.report.unresolved_obligations
    if obligations:
        console.print("[bold yellow]Remaining obligations[/bold yellow]")
        for obligation in obligations[:5]:
            console.print("  • ", Text(_compact_terminal_text(obligation)), sep="")
        if len(obligations) > 5:
            console.print(f"  • … and {len(obligations) - 5} more in the full report")
    explanation = result.report.report.error_explanation
    if explanation.get("available") is True:
        console.print("[bold]What happened:[/bold] ", end="")
        console.print(str(explanation.get("explanation", "")), markup=False)
        console.print("[bold]Suggested resolution:[/bold] ", end="")
        console.print(str(explanation.get("suggested_resolution", "")), markup=False)


def _project_graph(graph_name: str | None = None) -> KnowledgeGraph:
    root = _project_root()
    config = load_config(project_root=root)
    available = list_graph_names(root)
    if graph_name is not None:
        selected = normalize_graph_name(graph_name)
        if selected not in available:
            suffix = f" Available graphs: {', '.join(available)}." if available else ""
            raise GraphNotInitializedError(f"knowledge graph {selected!r} does not exist.{suffix}")
    elif len(available) == 1:
        selected = available[0]
    elif not available:
        raise GraphNotInitializedError(
            "no knowledge graphs exist; start a run or use 'matek graph init GRAPH_NAME'"
        )
    else:
        raise KnowledgeGraphError(
            "multiple knowledge graphs exist; select one with --knowledge-graph NAME "
            f"(available: {', '.join(available)})"
        )
    return KnowledgeGraph(
        root,
        selected,
        maximum_context_nodes=config.graph.maximum_context_nodes,
        maximum_context_characters=config.graph.maximum_context_characters,
    )


def _new_project_graph(graph_name: str) -> KnowledgeGraph:
    root = _project_root()
    config = load_config(project_root=root)
    return KnowledgeGraph(
        root,
        normalize_graph_name(graph_name),
        maximum_context_nodes=config.graph.maximum_context_nodes,
        maximum_context_characters=config.graph.maximum_context_characters,
    )


@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="Replace existing starter files."),
) -> None:
    """Initialize MATEK configuration in the current project."""

    try:
        root = _project_root()
        result = initialize_project(root, force=force)
        for path in result.created:
            console.print(f"[green]✓[/green] Created {path.relative_to(root)}")
        for path in result.overwritten:
            console.print(f"[yellow]![/yellow] Replaced {path.relative_to(root)}")
        for path in result.preserved:
            console.print(f"[dim]- Preserved {path.relative_to(root)}[/dim]")
        console.print(
            "[dim]Knowledge graphs are created per problem when 'matek run' starts.[/dim]"
        )
    except BaseException as exc:
        _abort(exc)


@graph_app.command("init")
def graph_init(graph_name: str = typer.Argument(..., help="Name for the knowledge graph.")) -> None:
    """Create one named Markdown vault and rebuildable graph index."""

    try:
        graph = _new_project_graph(graph_name)
        state = graph.initialize()
        console.print(f"Graph: {graph.graph_name}")
        console.print(f"Vault: {graph.vault_root}")
        console.print(f"Revision: {state.revision}")
    except BaseException as exc:
        _abort(exc)


@graph_app.command("list")
def graph_list() -> None:
    """List the initialized knowledge graphs in this project."""

    try:
        root = _project_root()
        values = [
            {
                "name": name,
                "vault": str((root / ".matek" / "knowledge" / name).relative_to(root)),
            }
            for name in list_graph_names(root)
        ]
        console.print(json.dumps(values, indent=2, sort_keys=True))
    except BaseException as exc:
        _abort(exc)


@graph_app.command("validate")
def graph_validate(
    knowledge_graph: str | None = typer.Option(None, "--knowledge-graph", "-g"),
) -> None:
    """Validate Markdown, machine ownership, relations, DAGs, and index revision."""

    try:
        report = _project_graph(knowledge_graph).validate()
        console.print(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
        if not report.valid:
            raise typer.Exit(code=6)
    except typer.Exit:
        raise
    except BaseException as exc:
        _abort(exc)


@graph_app.command("status")
def graph_status_command(
    knowledge_graph: str | None = typer.Option(None, "--knowledge-graph", "-g"),
) -> None:
    """Show the current graph revision and typed node/status counts."""

    try:
        status_value = _project_graph(knowledge_graph).status()
        console.print(json.dumps(status_value.model_dump(mode="json"), indent=2, sort_keys=True))
    except BaseException as exc:
        _abort(exc)


@graph_app.command("doctor")
def graph_doctor(
    repair: bool = typer.Option(
        False,
        "--repair",
        help=(
            "Transactionally repair whitelisted generated metadata defects and rename "
            "legacy hash node IDs to descriptive one-liner IDs."
        ),
    ),
    problem_id: str | None = typer.Option(None, "--problem-id"),
    knowledge_graph: str | None = typer.Option(None, "--knowledge-graph", "-g"),
) -> None:
    """Inspect repairable generated graph metadata and legacy node IDs without model calls."""

    try:
        report = _project_graph(knowledge_graph).doctor(
            repair=repair,
            problem_id=problem_id,
        )
        console.print(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
    except BaseException as exc:
        _abort(exc)


@graph_app.command("frontier")
def graph_frontier(
    problem_id: str | None = typer.Option(None, "--problem-id"),
    knowledge_graph: str | None = typer.Option(None, "--knowledge-graph", "-g"),
) -> None:
    """Show unresolved claims, audits, contradictions, blockers, and active tasks."""

    try:
        frontier_value = _project_graph(knowledge_graph).frontier(problem_id)
        console.print(json.dumps(frontier_value.model_dump(mode="json"), indent=2, sort_keys=True))
    except BaseException as exc:
        _abort(exc)


def _read_legacy_migration_source(graph: KnowledgeGraph) -> tuple[str, list[GraphNode]]:
    """Read one consistent archive revision without triggering transaction recovery."""

    with graph._locked():
        if graph.pending_path.exists():
            raise GraphValidationError(
                "legacy migration planning is read-only and cannot recover a pending graph "
                "transaction; run another graph read command to recover it first"
            )
        state = graph._load_state_unlocked()
        nodes = graph._load_nodes_unlocked(include_human_notes=True)
    return state.revision, nodes


def _migration_problem_id(nodes: Sequence[GraphNode], requested: str | None) -> str:
    problem_ids = sorted(node.matek_id for node in nodes if node.node_type is NodeType.PROBLEM)
    if requested is not None:
        normalized = requested.strip()
        if normalized not in problem_ids:
            raise GraphValidationError(f"unknown graph problem ID: {normalized}")
        return normalized
    if len(problem_ids) == 1:
        return problem_ids[0]
    if not problem_ids:
        raise GraphValidationError("knowledge graph has no problem node")
    raise GraphValidationError(
        "knowledge graph tracks multiple problems; pass --problem-id explicitly"
    )


def _migration_target_claim_id(
    graph: KnowledgeGraph,
    nodes: Sequence[GraphNode],
    *,
    problem_id: str,
    requested: str | None,
) -> str:
    claims = {
        node.matek_id: node
        for node in nodes
        if node.problem_id == problem_id and node.node_type is NodeType.CLAIM
    }
    if requested is not None:
        normalized = requested.strip()
        if normalized not in claims:
            raise GraphValidationError(
                f"target claim {normalized!r} is not a claim for problem {problem_id}"
            )
        return normalized

    tagged = sorted(node.matek_id for node in claims.values() if "matek/main-target" in node.tags)
    if len(tagged) == 1:
        return tagged[0]
    if len(tagged) > 1:
        raise GraphValidationError(
            "multiple claims are tagged as the main target; pass --target-claim-id explicitly"
        )
    canonical = graph.main_claim_id(problem_id)
    if canonical in claims:
        return canonical
    raise GraphValidationError(
        "no main target claim is identifiable; pass --target-claim-id explicitly"
    )


def _legacy_migration_payload(report: LegacyMigrationReport) -> dict[str, Any]:
    payload = report.model_dump(mode="json")
    payload["integrity_sha256"] = migration_report_sha256(report)
    return payload


def _legacy_migration_application_payload(
    record: LegacyMigrationApplicationRecord,
) -> dict[str, Any]:
    payload = record.model_dump(mode="json")
    payload["integrity_sha256"] = migration_application_sha256(record)
    return payload


def _migration_output_path(graph: KnowledgeGraph, requested: Path) -> Path:
    expanded = requested.expanduser()
    if expanded.is_symlink():
        raise WorkspaceError(f"refusing symlinked migration report output: {expanded}")
    try:
        destination = expanded.resolve(strict=False)
        knowledge_root = graph.collection_root.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceError(f"cannot resolve migration report output {expanded}: {exc}") from exc
    if destination == knowledge_root or knowledge_root in destination.parents:
        raise WorkspaceError(
            "migration reports must be written outside .matek/knowledge so the archival "
            "vault and snapshots remain unchanged"
        )
    return destination


def _migration_input_path(graph: KnowledgeGraph, requested: Path) -> Path:
    expanded = requested.expanduser()
    if expanded.is_symlink():
        raise WorkspaceError(f"refusing symlinked migration plan input: {expanded}")
    try:
        source = expanded.resolve(strict=True)
        knowledge_root = graph.collection_root.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceError(f"cannot resolve migration plan input {expanded}: {exc}") from exc
    if source == knowledge_root or knowledge_root in source.parents:
        raise WorkspaceError("reviewed migration plans must remain outside .matek/knowledge")
    if not source.is_file():
        raise WorkspaceError(f"migration plan input is not a regular file: {source}")
    return source


@graph_app.command("migrate-legacy")
def graph_migrate_legacy(
    problem_id: str | None = typer.Option(
        None,
        "--problem-id",
        help="Problem node to plan; required only when the graph contains several problems.",
    ),
    target_claim_id: str | None = typer.Option(
        None,
        "--target-claim-id",
        help="Main claim to plan; otherwise infer the uniquely tagged/canonical main target.",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        dir_okay=False,
        help="Write integrity-protected JSON outside the graph vault; otherwise print it.",
    ),
    apply_plan: Path | None = typer.Option(
        None,
        "--apply-plan",
        dir_okay=False,
        help="Apply one externally reviewed integrity-protected plan.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Confirm application without an interactive prompt.",
    ),
    audit_nomination_limit: int = typer.Option(
        25,
        "--audit-nomination-limit",
        min=1,
        help="Maximum number of strong intermediate results nominated for fresh audit.",
    ),
    dry_run: bool = typer.Option(
        True,
        "--dry-run",
        help="Build a read-only plan when --apply-plan is omitted (the default).",
    ),
    knowledge_graph: str | None = typer.Option(None, "--knowledge-graph", "-g"),
) -> None:
    """Plan a legacy backfill, or explicitly apply one reviewed external plan.

    Dry-run planning is the default. Applying a plan requires --apply-plan plus an
    interactive confirmation or --yes. Existing snapshots and legacy note identities
    remain archived.
    """

    try:
        graph = _project_graph(knowledge_graph)
        if apply_plan is not None:
            if output is not None:
                raise KnowledgeGraphError("--output cannot be combined with --apply-plan")
            source = _migration_input_path(graph, apply_plan)
            report = load_legacy_migration_report(source)
            if not yes:
                typer.confirm(
                    "Apply this reviewed legacy migration plan to the selected graph?",
                    abort=True,
                )
            record = graph.apply_legacy_migration(report)
            sys.stdout.write(
                json.dumps(
                    _legacy_migration_application_payload(record),
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            return
        if not dry_run:  # pragma: no cover - Typer exposes only the affirmative flag
            raise KnowledgeGraphError("pass --apply-plan to apply a reviewed migration")
        revision, nodes = _read_legacy_migration_source(graph)
        selected_problem = _migration_problem_id(nodes, problem_id)
        selected_target = _migration_target_claim_id(
            graph,
            nodes,
            problem_id=selected_problem,
            requested=target_claim_id,
        )
        report = plan_legacy_graph_backfill(
            nodes,
            graph_revision=revision,
            problem_id=selected_problem,
            target_claim_id=selected_target,
            audit_nomination_limit=audit_nomination_limit,
            graph_name=graph.graph_name,
        )
        if output is None:
            sys.stdout.write(
                json.dumps(_legacy_migration_payload(report), indent=2, sort_keys=True) + "\n"
            )
        else:
            destination = _migration_output_path(graph, output)
            written = write_legacy_migration_report(destination, report)
            console.print(f"Wrote read-only migration plan: {written}")
            console.print(f"Integrity SHA-256: {migration_report_sha256(report)}")
    except BaseException as exc:
        _abort(exc)


@graph_app.command("rebuild-index")
def graph_rebuild_index(
    knowledge_graph: str | None = typer.Option(None, "--knowledge-graph", "-g"),
) -> None:
    """Rebuild the disposable SQLite index from authoritative Markdown notes."""

    try:
        path = _project_graph(knowledge_graph).rebuild_index()
        console.print(f"Rebuilt {path}")
    except BaseException as exc:
        _abort(exc)


@graph_app.command("open")
def graph_open(knowledge_graph: str | None = typer.Option(None, "--knowledge-graph", "-g")) -> None:
    """Open the vault in Obsidian when available, otherwise print its path."""

    try:
        opened, path, detail = _project_graph(knowledge_graph).open_in_obsidian()
        console.print(f"Vault: {path}")
        console.print(("Opened in Obsidian. " if opened else "Obsidian unavailable. ") + detail)
    except BaseException as exc:
        _abort(exc)


@graph_app.command("export")
def graph_export(
    output_format: GraphExportChoice = typer.Option(GraphExportChoice.JSON, "--format"),
    output: Path | None = typer.Option(None, "--output", dir_okay=False),
    knowledge_graph: str | None = typer.Option(None, "--knowledge-graph", "-g"),
) -> None:
    """Export JSON, Graphviz DOT, or Mermaid without requiring Obsidian."""

    try:
        rendered = _project_graph(knowledge_graph).export(output_format=output_format.value)
        if output is None:
            console.print(rendered, markup=False, end="")
        else:
            destination = output.expanduser().resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(rendered, encoding="utf-8")
            console.print(f"Wrote {destination}")
    except BaseException as exc:
        _abort(exc)


@graph_app.command("diff")
def graph_diff(
    revision_a: str,
    revision_b: str,
    knowledge_graph: str | None = typer.Option(None, "--knowledge-graph", "-g"),
) -> None:
    """Compare two durable graph snapshots."""

    try:
        difference = _project_graph(knowledge_graph).diff(revision_a, revision_b)
        console.print(json.dumps(difference.model_dump(mode="json"), indent=2, sort_keys=True))
    except BaseException as exc:
        _abort(exc)


@graph_app.command("reconstruct")
def graph_reconstruct(
    revision: str,
    output: Path | None = typer.Option(None, "--output", dir_okay=False),
    knowledge_graph: str | None = typer.Option(None, "--knowledge-graph", "-g"),
) -> None:
    """Reconstruct and integrity-check one immutable graph revision snapshot."""

    try:
        contents = _project_graph(knowledge_graph).reconstruct_snapshot(revision)
        if output is None:
            sys.stdout.write(contents.decode("utf-8"))
        else:
            requested = output.expanduser()
            if requested.is_symlink():
                raise WorkspaceError(f"refusing symlinked reconstruction output: {requested}")
            destination = atomic_write_bytes(requested, contents)
            console.print(f"Wrote {destination}")
    except BaseException as exc:
        _abort(exc)


@graph_app.command("verify-snapshots")
def graph_verify_snapshots(
    revision: str | None = typer.Argument(
        None,
        help="Optional revision; omit it to verify the complete snapshot history.",
    ),
    knowledge_graph: str | None = typer.Option(None, "--knowledge-graph", "-g"),
) -> None:
    """Verify manifests, parent roots, checkpoints, and live content blobs."""

    try:
        results = _project_graph(knowledge_graph).verify_snapshots(revision)
        console.print(
            json.dumps(
                [item.model_dump(mode="json") for item in results],
                indent=2,
                sort_keys=True,
            )
        )
    except BaseException as exc:
        _abort(exc)


@graph_app.command("show")
def graph_show(
    node_id: str,
    knowledge_graph: str | None = typer.Option(None, "--knowledge-graph", "-g"),
) -> None:
    """Show one node by immutable ID."""

    try:
        node = _project_graph(knowledge_graph).show(node_id)
        console.print(json.dumps(node.model_dump(mode="json"), indent=2, sort_keys=True))
    except BaseException as exc:
        _abort(exc)


def _print_graph_nodes(nodes: Sequence[BaseModel]) -> None:
    console.print(
        json.dumps([node.model_dump(mode="json") for node in nodes], indent=2, sort_keys=True)
    )


@graph_app.command("dependencies")
def graph_dependencies(
    node_id: str,
    knowledge_graph: str | None = typer.Option(None, "--knowledge-graph", "-g"),
) -> None:
    """Traverse mathematical dependencies of a node."""

    try:
        _print_graph_nodes(
            _project_graph(knowledge_graph).traverse(
                node_id, downstream=False, relation=RelationType.DEPENDS_ON
            )
        )
    except BaseException as exc:
        _abort(exc)


@graph_app.command("downstream")
def graph_downstream(
    node_id: str,
    knowledge_graph: str | None = typer.Option(None, "--knowledge-graph", "-g"),
) -> None:
    """Traverse nodes invalidated when this dependency changes."""

    try:
        _print_graph_nodes(
            _project_graph(knowledge_graph).traverse(
                node_id, downstream=True, relation=RelationType.DEPENDS_ON
            )
        )
    except BaseException as exc:
        _abort(exc)


@graph_app.command("stale")
def graph_stale(
    problem_id: str | None = typer.Option(None, "--problem-id"),
    knowledge_graph: str | None = typer.Option(None, "--knowledge-graph", "-g"),
) -> None:
    """List stale nodes and invalidation reasons."""

    try:
        _print_graph_nodes(_project_graph(knowledge_graph).list_stale(problem_id))
    except BaseException as exc:
        _abort(exc)


@graph_app.command("tasks")
def graph_tasks(
    problem_id: str | None = typer.Option(None, "--problem-id"),
    knowledge_graph: str | None = typer.Option(None, "--knowledge-graph", "-g"),
) -> None:
    """List persistent graph-scoped research tasks."""

    try:
        _print_graph_nodes(_project_graph(knowledge_graph).list_tasks(problem_id))
    except BaseException as exc:
        _abort(exc)


@graph_app.command("tombstone")
def graph_tombstone(
    node_id: str,
    reason: str = typer.Option(..., "--reason"),
    knowledge_graph: str | None = typer.Option(None, "--knowledge-graph", "-g"),
) -> None:
    """Retain a superseded node identity and invalidate its dependents."""

    try:
        result = _project_graph(knowledge_graph).tombstone(node_id, reason=reason)
        console.print(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))
    except BaseException as exc:
        _abort(exc)


@app.command()
def doctor(
    online: bool = typer.Option(
        False,
        "--online",
        help="Probe the advanced OpenAI API backend (optional; requires OPENAI_API_KEY).",
    ),
    deep: bool = typer.Option(
        False,
        "--deep",
        help="Make one minimal live Codex structured-output call (consumes allowance).",
    ),
    config_path: Path | None = typer.Option(
        None, "--config", exists=True, readable=True, dir_okay=False
    ),
) -> None:
    """Check local dependencies, configuration, and prompt integrity."""

    try:
        root = _project_root()
        config = load_config(config_path, project_root=root)
        _show_migration_notice(config)
        report = run_doctor_checks(config, root, online=online, deep=deep)
        console.print("[bold]MATEK environment[/bold]")
        console.print(
            "Default model backend: "
            + ("Codex CLI" if config.backend.provider == "codex" else "OpenAI Responses API")
        )
        symbols = {
            CheckLevel.PASS: "[green]✓[/green]",
            CheckLevel.WARNING: "[yellow]![/yellow]",
            CheckLevel.FAILURE: "[red]✗[/red]",
        }
        for group in DoctorGroup:
            checks = report.checks_for(group)
            if not checks:
                continue
            table = Table(title=group.value)
            table.add_column("State", no_wrap=True)
            table.add_column("Check")
            table.add_column("Detail")
            for check in checks:
                detail = check.detail
                if check.remediation:
                    detail += f"\n[cyan]Remediation:[/cyan] {check.remediation}"
                table.add_row(symbols[check.level], check.name, detail)
            console.print(table)
        if report.failures:
            raise typer.Exit(code=3)
    except typer.Exit:
        raise
    except BaseException as exc:
        _abort(exc)


@app.command()
def run(
    problem_file: Path = typer.Argument(..., exists=True, readable=True, dir_okay=False),
    config_path: Path | None = typer.Option(
        None, "--config", exists=True, readable=True, dir_okay=False
    ),
    framework: Path | None = typer.Option(
        None, "--framework", exists=True, readable=True, dir_okay=False
    ),
    run_name: str | None = typer.Option(None, "--run-name"),
    backend: BackendChoice | None = typer.Option(
        None,
        "--backend",
        help="Model backend: codex (recommended/default) or api (advanced, separately billed).",
    ),
    budget_usd: float | None = typer.Option(None, "--budget-usd", min=0.0),
    max_coordinator_decisions: int | None = typer.Option(
        None,
        "--max-coordinator-decisions",
        min=1,
        help="Limit event-driven coordinator decisions (default 100000).",
    ),
    max_rounds: int | None = typer.Option(
        None,
        "--max-rounds",
        min=1,
        help="Deprecated: migrate each historical round to one pending-window of decisions.",
    ),
    num_first_level_agents: int | None = typer.Option(
        None,
        "--num-first-level-agents",
        min=4,
        help="Initial independent first-level research assignments (default 8).",
    ),
    max_concurrent_agents: int | None = typer.Option(
        None,
        "--max-concurrent-agents",
        min=1,
        help=(
            "Across-tier research-agent capacity (default 24); a hierarchical worker "
            "reserves one parent slot plus its configured nested allowance."
        ),
    ),
    max_agents: int | None = typer.Option(
        None,
        "--max-agents",
        min=1,
        help="Deprecated first-level-only concurrency option; use --max-concurrent-agents.",
    ),
    hierarchical: bool = typer.Option(
        False,
        "--hierarchical",
        help="Let each first-level Codex research agent spawn a bounded nested team.",
    ),
    flat: bool = typer.Option(
        False,
        "--flat",
        help="Disable nested delegation and use regular first-level research agents.",
    ),
    subagents_per_agent: int | None = typer.Option(
        None,
        "--subagents-per-agent",
        min=0,
        max=32,
        help="Nested agents available to each first-level agent (default 4 in hierarchical mode).",
    ),
    time_limit_minutes: int | None = typer.Option(
        None,
        "--time-limit-minutes",
        min=1,
        help="Limit total active run time across stages and resume attempts.",
    ),
    no_lean: bool = typer.Option(False, "--no-lean"),
    no_web_search: bool = typer.Option(
        False,
        "--no-web-search",
        help="Disable live model search and MATEK source-identifier HTTP lookups.",
    ),
    research_only: bool = typer.Option(False, "--research-only"),
    sandbox: SandboxChoice | None = typer.Option(None, "--sandbox"),
    allow_project_edits: bool = typer.Option(False, "--allow-project-edits"),
    knowledge_graph: str | None = typer.Option(
        None,
        "--knowledge-graph",
        "-g",
        help="Reuse an existing named graph instead of the problem filename's graph.",
    ),
    migrate_target: str | None = typer.Option(
        None,
        "--migrate-target",
        metavar="REASON",
        help=(
            "Explicitly authorize a material canonical-target migration and record REASON; "
            "otherwise target changes fail closed."
        ),
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Accept safety confirmations."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Start a new auditable research run."""

    try:
        root = _project_root()
        overrides = _config_overrides(
            backend=backend,
            budget_usd=budget_usd,
            max_coordinator_decisions=max_coordinator_decisions,
            max_rounds=max_rounds,
            num_first_level_agents=num_first_level_agents,
            max_concurrent_agents=max_concurrent_agents,
            max_agents=max_agents,
            hierarchical=hierarchical,
            flat=flat,
            subagents_per_agent=subagents_per_agent,
            time_limit_minutes=time_limit_minutes,
            no_lean=no_lean,
            no_web_search=no_web_search,
            sandbox=sandbox,
            verbose=verbose,
        )
        config = load_config(
            config_path,
            project_root=root,
            cli_overrides=overrides,
        )
        problem = _validate_problem_for_dry_run(problem_file)
        if knowledge_graph is None:
            selected_graph_name = problem_graph_name(problem_file)
            graph_selection = "problem filename"
        else:
            selected_graph_name = normalize_graph_name(knowledge_graph)
            graph_selection = "explicit existing graph"
            if selected_graph_name not in list_graph_names(root):
                available = list_graph_names(root)
                suffix = f" Available graphs: {', '.join(available)}." if available else ""
                raise GraphNotInitializedError(
                    f"knowledge graph {selected_graph_name!r} does not exist.{suffix}"
                )
        if framework is not None:
            framework_path = framework.expanduser().resolve(strict=True)
            framework_hash = sha256_file(framework_path)
        else:
            with resource_path("prompts/research_prompt_framework.txt") as bundled:
                framework_path = bundled
                framework_hash = sha256_file(bundled)
            if framework_hash != EXPECTED_FRAMEWORK_SHA256:
                raise IntakeError(
                    "bundled prompt framework integrity check failed; reinstall MATEK "
                    "or explicitly select an intentional custom framework with --framework"
                )
        try:
            framework_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise IntakeError("prompt framework must be valid UTF-8") from exc

        summary = _resolved_run_summary(
            config,
            graph_name=selected_graph_name,
            research_only=research_only,
            no_lean=no_lean,
            allow_project_edits=allow_project_edits,
        )
        if dry_run:
            plan: dict[str, object] = {
                **summary,
                "project root": root,
                "problem": problem_file.resolve(),
                "problem characters": len(problem),
                "framework": framework_path,
                "framework SHA-256": framework_hash,
                "knowledge graph name": selected_graph_name,
                "knowledge graph selection": graph_selection,
                "persistent knowledge graph": (root / ".matek" / "knowledge" / selected_graph_name),
                "graph context limit": (
                    f"{config.graph.maximum_context_nodes} nodes / "
                    f"{config.graph.maximum_context_characters} characters"
                ),
                "target migration": migrate_target or "not authorized",
            }
            _print_settings_table("Resolved MATEK plan", plan)
            console.print(
                "[green]Dry run complete; no run workspace or model call was made.[/green]"
            )
            return

        _show_migration_notice(config)
        _print_settings_table("Resolved MATEK run configuration", summary)
        if allow_project_edits and not yes:
            typer.confirm(
                "Allow Codex to edit files outside .matek/ in this project?",
                abort=True,
            )
        if migrate_target is not None and not yes:
            typer.confirm(
                "Authorize a versioned canonical theorem migration and invalidate affected "
                "proof evidence?",
                abort=True,
            )
        result = _run_async(
            _live_runner(config).run_new(
                problem_file,
                root,
                options=WorkflowOptions(
                    run_name=run_name,
                    framework_path=framework,
                    no_lean=no_lean,
                    research_only=research_only,
                    allow_project_edits=allow_project_edits,
                    knowledge_graph=knowledge_graph,
                    target_migration_reason=migrate_target,
                    invocation={
                        "config": str(config_path) if config_path else None,
                        "backend": config.backend.provider,
                        "budget_usd": budget_usd,
                        "max_coordinator_decisions": max_coordinator_decisions,
                        "max_rounds": max_rounds,
                        "num_first_level_agents": num_first_level_agents,
                        "max_concurrent_agents": max_concurrent_agents,
                        "max_agents": max_agents,
                        "hierarchical": hierarchical,
                        "flat": flat,
                        "subagents_per_agent": subagents_per_agent,
                        "time_limit_minutes": time_limit_minutes,
                        "no_web_search": no_web_search,
                        "sandbox": sandbox.value if sandbox else None,
                        "knowledge_graph": knowledge_graph,
                        "target_migration_reason": migrate_target,
                    },
                ),
            )
        )
        _print_result(result)
    except typer.Abort as exc:
        _abort(exc)
    except BaseException as exc:
        _abort(exc, verbose=verbose)


def _load_state(root: Path, run_id: str | None) -> RunState:
    return StateStore(resolve_run_root(root, run_id)).load()


def _elapsed_seconds(state: RunState) -> float:
    terminal_times = [
        record.completed_at for record in state.stages.values() if record.completed_at is not None
    ]
    end = max(terminal_times) if terminal_times else datetime.now(UTC)
    return max(0.0, (end - state.created_at).total_seconds())


def _research_scheduler_snapshot(state: RunState) -> tuple[dict[str, int], str, dict[str, Any]]:
    path = state.run_root / "research" / "coordinator" / "state.json"
    if not path.is_file():
        return {"queued": 0, "running": 0, "completed": 0}, "not_started", {}
    scheduler = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(scheduler, dict):
        raise ConfigError("research coordinator state is invalid")
    assignments = scheduler.get("assignments", [])
    if not isinstance(assignments, list):
        raise ConfigError("research coordinator assignment state is invalid")
    counts = {
        status: sum(
            isinstance(item, dict) and item.get("status") == status for item in assignments
        )
        for status in ("queued", "running", "completed")
    }
    return counts, str(scheduler.get("phase", "unknown")), scheduler


def _capacity_values(state: RunState) -> tuple[object, object]:
    summary = state.metadata.get("configuration_summary", {})
    if not isinstance(summary, dict):
        return "unknown", "unknown"
    requested = summary.get("max_concurrent_agents", "unknown")
    effective = summary.get(
        "effective_research_model_call_concurrency",
        summary.get("max_concurrent_first_level_agents", "unknown"),
    )
    return requested, effective


def _print_concurrent_runs(project_root: Path) -> None:
    rows: list[tuple[RunState, dict[str, int]]] = []
    for run_root in list_run_roots(project_root):
        try:
            state = StateStore(run_root).load()
            if state.metadata.get("workflow_status") != "RUNNING" and not any(
                record.status is StageStatus.RUNNING for record in state.stages.values()
            ):
                continue
            counts, _, _ = _research_scheduler_snapshot(state)
        except (OSError, ValueError, StateError, json.JSONDecodeError):
            continue
        rows.append((state, counts))
    if not rows:
        return

    console.print("[bold]Active run-owned capacity[/bold]")
    for state, counts in rows:
        graph = state.metadata.get("knowledge_graph", {})
        graph_name = graph.get("name", "unassigned") if isinstance(graph, dict) else "unassigned"
        requested, effective = _capacity_values(state)
        console.print(
            f"- Run {state.run_id}: graph {graph_name}; workspace {state.run_root}; "
            f"requested {requested}; effective {effective}; "
            f"active {counts['running']}; queued {counts['queued']}"
        )
    console.print("Global MATEK capacity constraint: none; each row owns its run-local pool.")


@app.command()
def status(run_id: str | None = typer.Argument(None)) -> None:
    """Show checkpoints, usage, elapsed time, and artifact paths."""

    try:
        project_root = _project_root()
        if run_id is None:
            _print_concurrent_runs(project_root)
        state = _load_state(project_root, run_id)
        scientific_status = str(
            state.metadata.get("research_status", state.scientific_status.value)
        )
        workflow_status = str(state.metadata.get("workflow_status", "RUNNING"))
        root_failure = blocking_failure_summary(state)
        if root_failure and workflow_status in {"RUNNING", "COMPLETE"}:
            workflow_status = (
                "HARD_STOPPED"
                if root_failure.get("category") == "integrity"
                else "PAUSED_RETRIABLE"
            )
        if (
            root_failure.get("blocking_stage") == "prompt_compilation"
            and scientific_status == "PROMPT_COMPILED"
        ):
            scientific_status = "RECEIVED"
        console.print(f"Run [bold]{state.run_id}[/bold]")
        graph = state.metadata.get("knowledge_graph", {})
        graph_name = graph.get("name", "unassigned") if isinstance(graph, dict) else "unassigned"
        console.print(f"Ownership: graph {graph_name}; workspace {state.run_root}")
        console.print(f"Scientific: {scientific_status}")
        console.print(f"Workflow: {workflow_status}")
        if root_failure:
            console.print(f"Blocking stage: {root_failure['blocking_stage']}")
            console.print(f"Failure class: {root_failure['failure_class']}")
            console.print(f"Root cause: {root_failure['root_cause']}")
            console.print(f"Automatic recovery: {root_failure['automatic_recovery']}")
            console.print(f"Next action: {root_failure['next_action']}")
        console.print(f"Manuscript: {state.metadata.get('manuscript_status', 'NOT_STARTED')}")
        console.print(f"Publication: {state.metadata.get('publication_status', 'NOT_ASSESSED')}")
        console.print(f"Lean: {state.metadata.get('lean_status', 'NOT_STARTED')}")
        clarification = state.metadata.get("problem_clarification", {})
        if isinstance(clarification, dict) and clarification.get("required") is True:
            console.print(
                "[yellow]Problem clarification required:[/yellow] "
                f"{clarification.get('reason', 'the intended target is ambiguous')}"
            )
            console.print("Revise the problem file and start a new run.")
        target_assumption = state.metadata.get("target_assumption", {})
        if isinstance(target_assumption, dict) and target_assumption.get("assumed_interpretation"):
            console.print(
                "[yellow]Assumed target:[/yellow] "
                + str(target_assumption["assumed_interpretation"])
            )
        prompt_warnings = state.metadata.get("prompt_validation_warnings", [])
        if isinstance(prompt_warnings, list) and prompt_warnings:
            console.print(f"Prompt/alignment warnings: {len(prompt_warnings)}")
            for warning in prompt_warnings[:5]:
                console.print(f"  - {_compact_terminal_text(str(warning))}")
        error_explanation = state.metadata.get("error_explanation", {})
        if isinstance(error_explanation, dict) and error_explanation.get("available") is True:
            console.print(
                "Error explanation: "
                + _compact_terminal_text(str(error_explanation.get("explanation", "")))
            )
            console.print(
                "Suggested resolution: "
                + _compact_terminal_text(str(error_explanation.get("suggested_resolution", "")))
            )
        backend = state.metadata.get("backend", {})
        if not isinstance(backend, dict):
            backend = {}
        provider = str(backend.get("provider", "unknown"))
        authentication = backend.get("authentication_class", "unverified")
        authentication_description = {
            "chatgpt": "ChatGPT subscription",
            "api_key": "Codex API-key login",
            "access_token": "Codex access token",
            "authenticated_unknown": "authenticated (method unknown)",
            "platform_api_key": "OpenAI Platform API key",
            "not_configured": "not configured",
            "not_authenticated": "not authenticated",
            "unverified": "unverified",
            None: "unverified",
        }.get(authentication, str(authentication))
        requested_model = backend.get("model_requested")
        if requested_model is None:
            requested_model = "Codex default" if provider == "codex" else "unobserved"
        requested_effort = backend.get("reasoning_effort_requested", "unobserved")
        search_setting = (
            backend.get("web_search_enabled", "unobserved")
            if backend.get("completed_calls", 0)
            else backend.get(
                "web_search_policy",
                backend.get("web_search_enabled", "unobserved"),
            )
        )
        console.print(
            "Backend: "
            f"{backend.get('display_name', provider)}; "
            f"authentication {authentication_description}; "
            f"version {backend.get('backend_version') or 'unobserved'}; "
            f"model {requested_model}; "
            f"reasoning effort {requested_effort}; "
            f"live web search {search_setting}; "
            f"automatic fallback {backend.get('automatic_fallback', False)}"
        )
        configuration = state.metadata.get("configuration_summary", {})
        if isinstance(configuration, dict):
            console.print(
                "Research roles: "
                f"coordinator {configuration.get('research_coordinator_model', 'unobserved')} "
                f"at {configuration.get('research_coordinator_effort', 'unobserved')}; "
                f"workers {configuration.get('research_worker_model', 'unobserved')} "
                f"at {configuration.get('research_worker_effort', 'unobserved')}"
            )
            hierarchy_mode = configuration.get("research_orchestration_mode", "flat")
            nested_limit = configuration.get(
                "subagents_per_agent",
                configuration.get("maximum_subagents_per_agent", 0),
            )
            total_capacity = configuration.get("max_concurrent_agents")
            first_level_capacity = configuration.get(
                "max_concurrent_first_level_agents",
                configuration.get("maximum_concurrent_agents"),
            )
            capacity_prefix = (
                f"{total_capacity} total reserved agent slots; "
                if total_capacity is not None
                else ""
            )
            first_level_text = (
                f"up to {first_level_capacity} concurrent first-level agents; "
                if first_level_capacity is not None
                else ""
            )
            console.print(
                "Research organization: "
                + (
                    f"hierarchical; {capacity_prefix}{first_level_text}"
                    f"up to {nested_limit} nested subagents per first-level agent"
                    if hierarchy_mode == "hierarchical" and nested_limit
                    else (
                        f"flat; up to {first_level_capacity} concurrent research agents"
                        if first_level_capacity is not None
                        else "flat; regular research agents"
                    )
                )
            )
        counts, scheduler_phase, scheduler = _research_scheduler_snapshot(state)
        requested_capacity, effective_capacity = _capacity_values(state)
        console.print(
            "Capacity: "
            f"requested {requested_capacity} run-owned agent slots; "
            f"effective {effective_capacity} concurrent model calls; "
            f"active {counts['running']}; queued {counts['queued']}; "
            "global wait none (no MATEK-global pool)"
        )
        if scheduler:
            console.print(
                "Research coordinator: "
                f"phase {scheduler_phase}; "
                f"decisions {len(scheduler.get('decisions', []))}; "
                "mailbox acknowledged through event "
                f"{scheduler.get('coordinator_ack_event_sequence', 0)}; "
                f"queued {counts['queued']}; active {counts['running']}; "
                f"completed {counts['completed']}"
            )
            candidate_attempt = scheduler.get("active_candidate_attempt")
            if isinstance(candidate_attempt, dict):
                mandatory = candidate_attempt.get("mandatory_audits", [])
                completed = candidate_attempt.get("audit_sha256", {})
                if isinstance(mandatory, list) and isinstance(completed, dict):
                    missing = [str(name) for name in mandatory if name not in completed]
                    console.print(
                        "Candidate audits: "
                        f"completed {', '.join(sorted(completed)) or 'none'}; "
                        f"missing {', '.join(missing) or 'none'}"
                    )
        lean_consent = state.metadata.get("lean_consent")
        if isinstance(lean_consent, dict):
            console.print(
                "Lean decision: "
                f"{lean_consent.get('outcome', 'unknown')}; "
                f"proceed {lean_consent.get('proceed', False)}"
            )
        raw_history = state.metadata.get("backend_history", [])
        if isinstance(raw_history, list) and raw_history:
            console.print(
                f"Provider migrations: {len(raw_history)} explicit provenance-changing switch(es)"
            )
        table = Table(title="Stages")
        table.add_column("Stage")
        table.add_column("Status")
        table.add_column("Attempts", justify="right")
        table.add_column("Artifacts", justify="right")
        for stage, record in state.stages.items():
            color = {
                StageStatus.SUCCEEDED: "green",
                StageStatus.FAILED: "red",
                StageStatus.INTERRUPTED: "yellow",
                StageStatus.RUNNING: "cyan",
                StageStatus.SKIPPED: "dim",
            }.get(record.status, "white")
            table.add_row(
                stage.value,
                f"[{color}]{record.status.value}[/{color}]",
                str(record.attempts),
                str(len(record.artifacts)),
            )
        console.print(table)
        usage = state.metadata.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}
        usage_prefix = (
            "Codex allowance/credits (no dollar estimate); "
            if provider == "codex"
            else f"${float(usage.get('cost_usd', 0.0)):.4f}; "
        )
        console.print(
            "Usage: "
            f"{usage_prefix}"
            f"{int(usage.get('total_tokens', 0)):,} tokens; "
            f"{int(usage.get('calls', len(state.paid_call_ids))):,} calls; "
            f"{int(usage.get('unknown_cost_calls', 0)):,} unknown-cost calls; "
            f"elapsed {_elapsed_seconds(state):.1f}s"
        )
        artifact_table = Table(title="Recorded artifacts")
        artifact_table.add_column("Path")
        artifact_table.add_column("SHA-256")
        for relative, digest in sorted(state.artifact_hashes.items()):
            artifact_table.add_row(relative, digest)
        console.print(artifact_table)
    except BaseException as exc:
        _abort(exc)


@app.command()
def resume(
    run_id: str | None = typer.Argument(
        None,
        metavar="RUN_ID_OR_PROBLEM",
        help="Run ID, or a .md/.txt problem file whose most recent run should resume.",
    ),
    force_stage: StageName | None = typer.Option(None, "--force-stage"),
    backend: BackendChoice | None = typer.Option(
        None,
        "--backend",
        help="Explicitly migrate the remaining run to codex or api; provenance will differ.",
    ),
    budget_usd: float | None = typer.Option(None, "--budget-usd", min=0.0),
    max_coordinator_decisions: int | None = typer.Option(
        None, "--max-coordinator-decisions", min=1
    ),
    max_rounds: int | None = typer.Option(
        None, "--max-rounds", min=1, help="Deprecated compatibility option."
    ),
    num_first_level_agents: int | None = typer.Option(
        None,
        "--num-first-level-agents",
        min=4,
        help="Set the initial first-level portfolio if research has not started.",
    ),
    max_concurrent_agents: int | None = typer.Option(
        None,
        "--max-concurrent-agents",
        min=1,
        help="Set the across-tier research-agent capacity for remaining work.",
    ),
    max_agents: int | None = typer.Option(
        None,
        "--max-agents",
        min=1,
        help="Deprecated first-level-only concurrency option; use --max-concurrent-agents.",
    ),
    hierarchical: bool = typer.Option(
        False,
        "--hierarchical",
        help="Enable hierarchical research for remaining unlaunched workers.",
    ),
    flat: bool = typer.Option(
        False,
        "--flat",
        help="Disable nested delegation for remaining unlaunched workers.",
    ),
    subagents_per_agent: int | None = typer.Option(
        None,
        "--subagents-per-agent",
        min=0,
        max=32,
        help="Nested agents available to each first-level agent.",
    ),
    time_limit_minutes: int | None = typer.Option(
        None,
        "--time-limit-minutes",
        min=1,
        help="Set the total active-time limit for this run, including prior attempts.",
    ),
    no_web_search: bool = typer.Option(
        False,
        "--no-web-search",
        help="Disable web search for all remaining stages of this run.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm an explicit backend migration."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Resume the first incomplete checkpoint without repeating completed calls."""

    try:
        root = _project_root()
        run_root = _resolve_resume_selector(root, run_id)
        if run_id is not None and Path(run_id).suffix.lower() in {".md", ".txt"}:
            selection = Text("Resuming most recent run for ")
            selection.append(run_id, style="bold")
            selection.append(": ")
            selection.append(run_root.name, style="bold")
            console.print(selection)
        frozen = load_config(
            _effective_run_config_path(run_root),
            project_root=root,
            env={},
        )
        state = StateStore(run_root).load()
        pending_migration = state.metadata.get("pending_backend_migration")
        if pending_migration is not None:
            if not isinstance(pending_migration, dict) or not isinstance(
                pending_migration.get("target_config_toml"), str
            ):
                raise ConfigError("pending backend migration checkpoint is invalid")
            try:
                pending_mapping = tomllib.loads(pending_migration["target_config_toml"])
                pending_mapping["project_root"] = root
                frozen = AppConfig.model_validate(pending_mapping)
            except Exception as exc:
                raise ConfigError(
                    "pending backend migration target configuration is invalid"
                ) from exc
        _show_migration_notice(frozen)
        if budget_usd is not None and budget_usd < frozen.limits.maximum_cost_usd:
            raise ConfigError(
                "--budget-usd on resume may only increase the frozen run budget "
                f"({frozen.limits.maximum_cost_usd:g})"
            )
        overrides = _config_overrides(
            backend=backend,
            budget_usd=budget_usd,
            max_coordinator_decisions=max_coordinator_decisions,
            max_rounds=max_rounds,
            num_first_level_agents=num_first_level_agents,
            max_concurrent_agents=max_concurrent_agents,
            max_agents=max_agents,
            hierarchical=hierarchical,
            flat=flat,
            subagents_per_agent=subagents_per_agent,
            time_limit_minutes=time_limit_minutes,
            no_web_search=no_web_search,
            verbose=verbose,
        )
        config = merge_config(frozen, overrides)
        if config.backend.provider != frozen.backend.provider:
            if force_stage is None and first_incomplete_stage(state) is None:
                raise ConfigError(
                    "a completed run has no remaining model work to migrate; use "
                    "--force-stage together with --backend to rerun an explicit checkpoint"
                )
            warning = (
                f"Switch this run from {frozen.backend.provider} to "
                f"{config.backend.provider}? Model behavior and provenance will differ, "
                "and MATEK will record the switch. No provider fallback is automatic."
            )
            console.print(f"[yellow]Warning:[/yellow] {warning}")
            if not yes:
                typer.confirm("Continue with this backend migration?", abort=True)
        runner = (
            _offline_runner(config)
            if (
                force_stage is StageName.REPORT
                or (force_stage is None and first_incomplete_stage(state) is None)
            )
            else _live_runner(config)
        )
        result = _run_async(
            runner.resume(
                root,
                run_id=run_root.name,
                force_stage=force_stage,
                config_overrides=overrides,
            )
        )
        _print_result(result)
    except BaseException as exc:
        _abort(exc, verbose=verbose)


@app.command()
def report(
    run_id: str | None = typer.Argument(None),
    rewrite: bool = typer.Option(
        False,
        "--rewrite",
        help="Make one explicit paid model call for optional narrative prose.",
    ),
) -> None:
    """Regenerate reports offline, or explicitly request a paid narrative rewrite."""

    try:
        root = _project_root()
        run_root = resolve_run_root(root, run_id)
        frozen = load_config(
            _effective_run_config_path(run_root),
            project_root=root,
            env={},
        )
        result = (
            _run_async(_live_runner(frozen).rewrite_report(root, run_id=run_root.name))
            if rewrite
            else _offline_runner(frozen).regenerate_report(root, run_id=run_root.name)
        )
        _print_result(result)
        if rewrite:
            console.print(
                "[green]Report regenerated with explicit model-assisted narrative.[/green]"
            )
        else:
            console.print("[green]Report regenerated without model calls.[/green]")
    except BaseException as exc:
        _abort(exc)


@app.command()
def verify(run_id: str | None = typer.Argument(None)) -> None:
    """Re-run deterministic file, bibliography, LaTeX, and Lean checks."""

    try:
        # Imported lazily so status/report remain usable even if an optional local
        # verifier dependency is unavailable.
        from .reproduce import verify_run

        run_root = resolve_run_root(_project_root(), run_id)
        # Verification reads a cross-artifact snapshot and creates isolated temporary
        # compiler inputs. Do not let it race an active writer for the same run.
        with RunLock(run_root):
            result = _run_async(verify_run(run_root, NativeBackend()))
        table = Table(title=f"Verification — {run_root.name}")
        table.add_column("Check")
        table.add_column("State")
        table.add_column("Diagnostics")
        for check in result.checks:
            symbol = {
                "passed": "[green]✓ pass[/green]",
                "failed": "[red]✗ fail[/red]",
                "skipped": "[yellow]- skipped[/yellow]",
            }[check.status.value]
            table.add_row(check.name, symbol, "\n".join(check.diagnostics) or "—")
        console.print(table)
        console.print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        if not result.passed:
            raise typer.Exit(code=7)
    except typer.Exit:
        raise
    except BaseException as exc:
        _abort(exc)


if __name__ == "__main__":
    app()
