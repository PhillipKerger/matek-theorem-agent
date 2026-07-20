"""Typed command-line interface for ASCEND."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Coroutine, Mapping
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
console = Console()

T = TypeVar("T")


class SandboxChoice(StrEnum):
    NATIVE = "native"
    DOCKER = "docker"


class BackendChoice(StrEnum):
    CODEX = "codex"
    API = "api"


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
            model=config.codex.model or None,
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
                model=config.codex.model or None,
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
    max_rounds: int | None = None,
    max_agents: int | None = None,
    no_lean: bool | None = None,
    sandbox: SandboxChoice | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    return {
        "backend": backend.value if backend is not None else None,
        "budget_usd": budget_usd,
        "max_rounds": max_rounds,
        "max_agents": max_agents,
        "no_lean": True if no_lean else None,
        "sandbox": sandbox.value if sandbox is not None else None,
        "logging": {"level": "DEBUG"} if verbose else None,
    }


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


@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="Replace existing starter files."),
) -> None:
    """Initialize ASCEND configuration in the current project."""

    try:
        root = _project_root()
        result = initialize_project(root, force=force)
        for path in result.created:
            console.print(f"[green]✓[/green] Created {path.relative_to(root)}")
        for path in result.overwritten:
            console.print(f"[yellow]![/yellow] Replaced {path.relative_to(root)}")
        for path in result.preserved:
            console.print(f"[dim]- Preserved {path.relative_to(root)}[/dim]")
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
    max_rounds: int | None = typer.Option(None, "--max-rounds", min=1),
    max_agents: int | None = typer.Option(None, "--max-agents", min=1),
    no_lean: bool = typer.Option(False, "--no-lean"),
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
            max_rounds=max_rounds,
            max_agents=max_agents,
            no_lean=no_lean,
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
                "web search": config.models.prompt_compiler.web_search,
                "research rounds": config.research.maximum_rounds,
                "concurrent agents": config.research.maximum_concurrent_agents,
                "usage limit": (
                    f"{config.codex.limits.max_agent_calls} Codex agent calls"
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
                        "max_rounds": max_rounds,
                        "max_agents": max_agents,
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
    max_rounds: int | None = typer.Option(None, "--max-rounds", min=1),
    max_agents: int | None = typer.Option(None, "--max-agents", min=1),
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
        _show_migration_notice(frozen)
        if budget_usd is not None and budget_usd < frozen.limits.maximum_cost_usd:
            raise ConfigError(
                "--budget-usd on resume may only increase the frozen run budget "
                f"({frozen.limits.maximum_cost_usd:g})"
            )
        overrides = _config_overrides(
            backend=backend,
            budget_usd=budget_usd,
            max_rounds=max_rounds,
            max_agents=max_agents,
            verbose=verbose,
        )
        config = merge_config(frozen, overrides)
        state = StateStore(run_root).load()
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
