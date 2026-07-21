"""Typed command-line interface for ASCEND."""

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
    GraphValidationError,
    KnowledgeGraph,
    KnowledgeGraphError,
    RelationType,
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
from .resources import resource_path
from .stages.compile_prompt import EXPECTED_FRAMEWORK_SHA256
from .state import (
    ArtifactIntegrityError,
    StateCorruptionError,
    StateError,
    StateStore,
    first_incomplete_stage,
)
from .workspace import RunLock, WorkspaceError, discover_project_root, sha256_file

app = typer.Typer(
    no_args_is_help=True,
    help="ASCEND: auditable mathematical research and optional Lean verification.",
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
        "[bold]The verified manuscript is ready.[/bold] Proceed with formal Lean "
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
            model=config.codex.model,
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
    if isinstance(exc, GraphValidationError):
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
    if verbose:
        console.print(f"[dim]Exception type: {type(exc).__name__}; exit code: {code}[/dim]")
    raise typer.Exit(code=code)


def _config_overrides(
    *,
    backend: BackendChoice | None = None,
    budget_usd: float | None = None,
    max_coordinator_decisions: int | None = None,
    max_rounds: int | None = None,
    max_agents: int | None = None,
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
    return {
        "backend": backend.value if backend is not None else None,
        "budget_usd": budget_usd,
        "max_coordinator_decisions": max_coordinator_decisions,
        "max_rounds": max_rounds,
        "max_agents": max_agents,
        "time_limit_minutes": time_limit_minutes,
        "no_lean": True if no_lean else None,
        "no_web_search": True if no_web_search else None,
        "sandbox": sandbox.value if sandbox is not None else None,
        "logging": {"level": "DEBUG"} if verbose else None,
    }


def _time_limit_display(config: AppConfig) -> str:
    if config.backend.provider == "codex":
        minutes = config.codex.limits.max_wall_clock_minutes
        return "unlimited" if minutes is None else f"{minutes} minutes"
    hours = config.limits.maximum_wall_clock_hours
    return "unlimited" if hours is None else f"{hours * 60:g} minutes"


def _show_migration_notice(config: AppConfig) -> None:
    notice = consume_config_migration_notice(config)
    if notice is not None:
        console.print(f"[yellow]Configuration migration:[/yellow] {notice}")


def _effective_run_config_path(run_root: Path) -> Path:
    effective = run_root / "config" / "effective_config.toml"
    return effective if effective.is_file() else run_root / "input" / "config.resolved.toml"


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


def _print_result(result: WorkflowResult) -> None:
    console.print(f"Run: [bold]{result.state.run_id}[/bold]")
    backend = result.state.metadata.get("backend", {})
    if isinstance(backend, dict):
        console.print(f"Backend: {backend.get('display_name', backend.get('provider', 'unknown'))}")
    console.print(f"Research: {result.report.report.scientific_status}")
    console.print(f"Manuscript: {result.report.report.manuscript_status}")
    console.print(f"Lean: {result.report.report.lean_status}")
    clarification = result.report.report.problem_clarification
    if clarification.get("required") is True:
        console.print(
            "[yellow]ASCEND stopped before research because it could not uniquely identify "
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
            "[bold]ascend run PROBLEM_FILE[/bold]."
        )
    console.print(f"Report: {result.report.report_markdown}")


def _project_graph() -> KnowledgeGraph:
    root = _project_root()
    config = load_config(project_root=root)
    return KnowledgeGraph(
        root,
        maximum_context_nodes=config.graph.maximum_context_nodes,
        maximum_context_characters=config.graph.maximum_context_characters,
    )


@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="Replace existing starter files."),
) -> None:
    """Initialize ASCEND configuration in the current project."""

    try:
        root = _project_root()
        result = initialize_project(root, force=force)
        graph = _project_graph()
        graph.initialize()
        for path in result.created:
            console.print(f"[green]✓[/green] Created {path.relative_to(root)}")
        for path in result.overwritten:
            console.print(f"[yellow]![/yellow] Replaced {path.relative_to(root)}")
        for path in result.preserved:
            console.print(f"[dim]- Preserved {path.relative_to(root)}[/dim]")
        console.print(f"[green]✓[/green] Knowledge vault {graph.vault_root.relative_to(root)}")
    except BaseException as exc:
        _abort(exc)


@graph_app.command("init")
def graph_init() -> None:
    """Create the portable Markdown vault and rebuildable graph index."""

    try:
        graph = _project_graph()
        state = graph.initialize()
        console.print(f"Vault: {graph.vault_root}")
        console.print(f"Revision: {state.revision}")
    except BaseException as exc:
        _abort(exc)


@graph_app.command("validate")
def graph_validate() -> None:
    """Validate Markdown, machine ownership, relations, DAGs, and index revision."""

    try:
        report = _project_graph().validate()
        console.print(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
        if not report.valid:
            raise typer.Exit(code=6)
    except typer.Exit:
        raise
    except BaseException as exc:
        _abort(exc)


@graph_app.command("status")
def graph_status_command() -> None:
    """Show the current graph revision and typed node/status counts."""

    try:
        status_value = _project_graph().status()
        console.print(json.dumps(status_value.model_dump(mode="json"), indent=2, sort_keys=True))
    except BaseException as exc:
        _abort(exc)


@graph_app.command("frontier")
def graph_frontier(problem_id: str | None = typer.Option(None, "--problem-id")) -> None:
    """Show unresolved claims, audits, contradictions, blockers, and active tasks."""

    try:
        frontier_value = _project_graph().frontier(problem_id)
        console.print(json.dumps(frontier_value.model_dump(mode="json"), indent=2, sort_keys=True))
    except BaseException as exc:
        _abort(exc)


@graph_app.command("rebuild-index")
def graph_rebuild_index() -> None:
    """Rebuild the disposable SQLite index from authoritative Markdown notes."""

    try:
        path = _project_graph().rebuild_index()
        console.print(f"Rebuilt {path}")
    except BaseException as exc:
        _abort(exc)


@graph_app.command("open")
def graph_open() -> None:
    """Open the vault in Obsidian when available, otherwise print its path."""

    try:
        opened, path, detail = _project_graph().open_in_obsidian()
        console.print(f"Vault: {path}")
        console.print(("Opened in Obsidian. " if opened else "Obsidian unavailable. ") + detail)
    except BaseException as exc:
        _abort(exc)


@graph_app.command("export")
def graph_export(
    output_format: GraphExportChoice = typer.Option(GraphExportChoice.JSON, "--format"),
    output: Path | None = typer.Option(None, "--output", dir_okay=False),
) -> None:
    """Export JSON, Graphviz DOT, or Mermaid without requiring Obsidian."""

    try:
        rendered = _project_graph().export(output_format=output_format.value)
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
def graph_diff(revision_a: str, revision_b: str) -> None:
    """Compare two durable graph snapshots."""

    try:
        difference = _project_graph().diff(revision_a, revision_b)
        console.print(json.dumps(difference.model_dump(mode="json"), indent=2, sort_keys=True))
    except BaseException as exc:
        _abort(exc)


@graph_app.command("show")
def graph_show(node_id: str) -> None:
    """Show one node by immutable ID."""

    try:
        node = _project_graph().show(node_id)
        console.print(json.dumps(node.model_dump(mode="json"), indent=2, sort_keys=True))
    except BaseException as exc:
        _abort(exc)


def _print_graph_nodes(nodes: Sequence[BaseModel]) -> None:
    console.print(
        json.dumps([node.model_dump(mode="json") for node in nodes], indent=2, sort_keys=True)
    )


@graph_app.command("dependencies")
def graph_dependencies(node_id: str) -> None:
    """Traverse mathematical dependencies of a node."""

    try:
        _print_graph_nodes(
            _project_graph().traverse(node_id, downstream=False, relation=RelationType.DEPENDS_ON)
        )
    except BaseException as exc:
        _abort(exc)


@graph_app.command("downstream")
def graph_downstream(node_id: str) -> None:
    """Traverse nodes invalidated when this dependency changes."""

    try:
        _print_graph_nodes(
            _project_graph().traverse(node_id, downstream=True, relation=RelationType.DEPENDS_ON)
        )
    except BaseException as exc:
        _abort(exc)


@graph_app.command("stale")
def graph_stale(problem_id: str | None = typer.Option(None, "--problem-id")) -> None:
    """List stale nodes and invalidation reasons."""

    try:
        _print_graph_nodes(_project_graph().list_stale(problem_id))
    except BaseException as exc:
        _abort(exc)


@graph_app.command("tasks")
def graph_tasks(problem_id: str | None = typer.Option(None, "--problem-id")) -> None:
    """List persistent graph-scoped research tasks."""

    try:
        _print_graph_nodes(_project_graph().list_tasks(problem_id))
    except BaseException as exc:
        _abort(exc)


@graph_app.command("tombstone")
def graph_tombstone(node_id: str, reason: str = typer.Option(..., "--reason")) -> None:
    """Retain a superseded node identity and invalidate its dependents."""

    try:
        result = _project_graph().tombstone(node_id, reason=reason)
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
        console.print("[bold]ASCEND environment[/bold]")
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
        help="Limit event-driven coordinator decisions (default 256).",
    ),
    max_rounds: int | None = typer.Option(
        None,
        "--max-rounds",
        min=1,
        help="Deprecated: migrate each historical round to one pending-window of decisions.",
    ),
    max_agents: int | None = typer.Option(None, "--max-agents", min=1),
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
        help="Disable live model search and ASCEND source-identifier HTTP lookups.",
    ),
    research_only: bool = typer.Option(False, "--research-only"),
    sandbox: SandboxChoice | None = typer.Option(None, "--sandbox"),
    allow_project_edits: bool = typer.Option(False, "--allow-project-edits"),
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
            max_agents=max_agents,
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
        if framework is not None:
            framework_path = framework.expanduser().resolve(strict=True)
            framework_hash = sha256_file(framework_path)
        else:
            with resource_path("prompts/research_prompt_framework.txt") as bundled:
                framework_path = bundled
                framework_hash = sha256_file(bundled)
            if framework_hash != EXPECTED_FRAMEWORK_SHA256:
                raise IntakeError(
                    "bundled prompt framework integrity check failed; reinstall ASCEND "
                    "or explicitly select an intentional custom framework with --framework"
                )
        try:
            framework_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise IntakeError("prompt framework must be valid UTF-8") from exc

        if dry_run:
            table = Table(title="Resolved ASCEND plan")
            table.add_column("Setting")
            table.add_column("Value")
            plan: Mapping[str, object] = {
                "model backend": config.backend.provider,
                "automatic fallback": config.backend.allow_automatic_fallback,
                "project root": root,
                "problem": problem_file.resolve(),
                "problem characters": len(problem),
                "framework": framework_path,
                "framework SHA-256": framework_hash,
                "model": config.models.prompt_compiler.model,
                "reasoning": (
                    f"{config.models.prompt_compiler.reasoning_mode}/"
                    f"{config.models.prompt_compiler.reasoning_effort}"
                ),
                "web search": (
                    "enabled per stage" if config.web_search_enabled else "disabled globally"
                ),
                "initial research agents": config.research.minimum_initial_agents,
                "maximum pending assignments": (config.research.maximum_pending_assignments),
                "coordinator decisions": (config.research.maximum_coordinator_decisions),
                "concurrent agents": config.research.maximum_concurrent_agents,
                "persistent knowledge graph": root / ".ascend" / "knowledge",
                "graph context limit": (
                    f"{config.graph.maximum_context_nodes} nodes / "
                    f"{config.graph.maximum_context_characters} characters"
                ),
                "total active time limit": _time_limit_display(config),
                "usage limit": (
                    (
                        f"{config.codex.limits.max_agent_calls} Codex agent calls"
                        if config.codex.limits.max_agent_calls is not None
                        else "no configured Codex call-count limit"
                    )
                    if config.backend.provider == "codex"
                    else f"${config.limits.maximum_cost_usd:g} API spend"
                ),
                "manuscript": config.manuscript.enabled and not research_only,
                "Lean": config.lean.enabled and not no_lean and not research_only,
                "execution backend": config.lean.execution_backend,
                "project edits": allow_project_edits,
            }
            for key, value in plan.items():
                table.add_row(key, str(value))
            console.print(table)
            console.print(
                "[green]Dry run complete; no run workspace or model call was made.[/green]"
            )
            return

        _show_migration_notice(config)
        if allow_project_edits and not yes:
            typer.confirm(
                "Allow Codex to edit files outside .ascend/ in this project?",
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
                    invocation={
                        "config": str(config_path) if config_path else None,
                        "backend": config.backend.provider,
                        "budget_usd": budget_usd,
                        "max_coordinator_decisions": max_coordinator_decisions,
                        "max_rounds": max_rounds,
                        "max_agents": max_agents,
                        "time_limit_minutes": time_limit_minutes,
                        "no_web_search": no_web_search,
                        "sandbox": sandbox.value if sandbox else None,
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


@app.command()
def status(run_id: str | None = typer.Argument(None)) -> None:
    """Show checkpoints, usage, elapsed time, and artifact paths."""

    try:
        state = _load_state(_project_root(), run_id)
        console.print(f"Run [bold]{state.run_id}[/bold] — {state.scientific_status.value}")
        clarification = state.metadata.get("problem_clarification", {})
        if isinstance(clarification, dict) and clarification.get("required") is True:
            console.print(
                "[yellow]Problem clarification required:[/yellow] "
                f"{clarification.get('reason', 'the intended target is ambiguous')}"
            )
            console.print("Revise the problem file and start a new run.")
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
        scheduler_path = state.run_root / "research" / "coordinator" / "state.json"
        if scheduler_path.is_file():
            scheduler = json.loads(scheduler_path.read_text(encoding="utf-8"))
            assignments = scheduler.get("assignments", [])
            if not isinstance(assignments, list):
                raise ConfigError("research coordinator assignment state is invalid")
            counts = {
                status: sum(
                    isinstance(item, dict) and item.get("status") == status for item in assignments
                )
                for status in ("queued", "running", "completed")
            }
            console.print(
                "Research coordinator: "
                f"phase {scheduler.get('phase', 'unknown')}; "
                f"decisions {len(scheduler.get('decisions', []))}; "
                "mailbox acknowledged through event "
                f"{scheduler.get('coordinator_ack_event_sequence', 0)}; "
                f"queued {counts['queued']}; active {counts['running']}; "
                f"completed {counts['completed']}"
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
    run_id: str | None = typer.Argument(None),
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
    max_agents: int | None = typer.Option(None, "--max-agents", min=1),
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
        run_root = resolve_run_root(root, run_id)
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
            max_agents=max_agents,
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
                "and ASCEND will record the switch. No provider fallback is automatic."
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
