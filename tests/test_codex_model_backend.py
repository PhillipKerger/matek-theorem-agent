from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from matek_theorem_agent.codex_model_backend import (
    CodexAuthenticationClass,
    CodexAuthenticationExpiredError,
    CodexCliModelClient,
    CodexErrorKind,
    CodexInputTooLargeError,
    CodexModelUnavailableError,
    CodexNetworkOrSearchUnavailableError,
    CodexNotAuthenticatedError,
    CodexOutputMissingError,
    CodexProcessCrashError,
    CodexProcessTimeoutError,
    CodexRateLimitedError,
    CodexSchemaCompatibilityError,
    CodexSchemaValidationError,
    CodexStagePolicy,
    CodexUnauthorizedFileChangeError,
    classify_codex_failure,
    parse_codex_auth_status,
    parse_codex_capabilities,
    parse_codex_jsonl,
)
from matek_theorem_agent.config import ModelSettings
from matek_theorem_agent.execution.base import CommandRequest, CommandResult, CommandTimeoutError
from matek_theorem_agent.execution.native import NativeBackend
from matek_theorem_agent.openai_client import ModelRequest
from matek_theorem_agent.reporting import ReportNarrative
from matek_theorem_agent.stages.compile_prompt import CompiledProblem
from matek_theorem_agent.stages.lean import (
    ClaimAlignment,
    LeanFeasibilityAssessment,
    LeanStatementDraft,
)
from matek_theorem_agent.stages.manuscript import BibliographyAudit, ManuscriptDraft
from matek_theorem_agent.stages.research import (
    AuditVerdict,
    CandidateProofPackage,
    FinalJudgeVerdict,
    ResearchRoundPlan,
    ResearchWorkerReport,
)
from matek_theorem_agent.structured_schema import StrictSchemaError, strict_json_schema

ROOT_HELP = """Codex CLI

Usage: codex [OPTIONS] <COMMAND>

Commands:
  exec    Run Codex non-interactively
  login   Manage login

Options:
  -c, --config <key=value>
  -m, --model <MODEL>
  -s, --sandbox <SANDBOX_MODE>
  -C, --cd <DIR>
  -a, --ask-for-approval <APPROVAL_POLICY>
      --search
"""

EXEC_HELP = """Run Codex non-interactively

Usage: codex exec [OPTIONS] [PROMPT]

Commands:
  resume  Resume a previous session

Options:
  -c, --config <key=value>
  -m, --model <MODEL>
  -s, --sandbox <SANDBOX_MODE>
  -C, --cd <DIR>
      --skip-git-repo-check
      --ephemeral
      --ignore-user-config
      --ignore-rules
      --output-schema <FILE>
      --json
  -o, --output-last-message <FILE>
"""

RESUME_HELP = """Resume a previous session
Usage: codex exec resume [OPTIONS] [SESSION_ID] [PROMPT]
"""


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: int


class OpenAnswer(BaseModel):
    values: dict[str, int]


def _assert_strict_objects(node: object) -> None:
    if isinstance(node, list):
        for item in node:
            _assert_strict_objects(item)
        return
    if not isinstance(node, dict):
        return
    assert "default" not in node
    if node.get("type") == "object" or "properties" in node:
        properties = node.get("properties", {})
        assert isinstance(properties, dict)
        assert node.get("additionalProperties") is False
        assert set(node.get("required", [])) == set(properties)
    for value in node.values():
        _assert_strict_objects(value)


@pytest.mark.parametrize(
    "output_type",
    [
        CompiledProblem,
        ResearchRoundPlan,
        ResearchWorkerReport,
        CandidateProofPackage,
        AuditVerdict,
        FinalJudgeVerdict,
        ManuscriptDraft,
        BibliographyAudit,
        LeanFeasibilityAssessment,
        LeanStatementDraft,
        ClaimAlignment,
        ReportNarrative,
    ],
)
def test_every_production_model_output_schema_is_strict(
    output_type: type[BaseModel],
) -> None:
    _assert_strict_objects(strict_json_schema(output_type))


def test_open_arbitrary_key_map_is_rejected_with_schema_path() -> None:
    with pytest.raises(StrictSchemaError, match=r"\$\.properties\.values"):
        strict_json_schema(OpenAnswer)


class FakeCodexBackend:
    def __init__(
        self,
        outcomes: list[str] | None = None,
        *,
        auth_stdout: str = "Logged in using ChatGPT\n",
        auth_exit_code: int = 0,
    ) -> None:
        self.requests: list[CommandRequest] = []
        self.outcomes = list(outcomes or ["success"])
        self.auth_stdout = auth_stdout
        self.auth_exit_code = auth_exit_code
        self.exec_requests: list[CommandRequest] = []

    async def run(self, request: CommandRequest) -> CommandResult:
        self.requests.append(request)
        argv = request.argv
        if argv == ("codex", "--help"):
            return CommandResult(argv, request.cwd, 0, ROOT_HELP, "", 0.01)
        if argv == ("codex", "exec", "--help"):
            return CommandResult(argv, request.cwd, 0, EXEC_HELP, "", 0.01)
        if argv == ("codex", "exec", "resume", "--help"):
            return CommandResult(argv, request.cwd, 0, RESUME_HELP, "", 0.01)
        if argv == ("codex", "--version"):
            return CommandResult(argv, request.cwd, 0, "codex-cli 0.test\n", "", 0.01)
        if argv == ("codex", "login", "status"):
            return CommandResult(
                argv,
                request.cwd,
                self.auth_exit_code,
                self.auth_stdout,
                "",
                0.01,
            )

        self.exec_requests.append(request)
        outcome = self.outcomes.pop(0)
        if outcome == "timeout":
            raise CommandTimeoutError(
                CommandResult(
                    argv,
                    request.cwd,
                    -15,
                    '{"type":"thread.started","thread_id":"thread-timeout"}\n',
                    "timed out",
                    30.0,
                    timed_out=True,
                )
            )
        if outcome == "rate_limit":
            return CommandResult(argv, request.cwd, 1, "", "HTTP 429 rate limit exceeded", 0.1)
        if outcome == "model_unavailable":
            return CommandResult(argv, request.cwd, 1, "", "model is not available", 0.1)
        if outcome == "search_unavailable":
            return CommandResult(argv, request.cwd, 1, "", "web search unavailable", 0.1)
        if outcome == "crash":
            return CommandResult(argv, request.cwd, 70, "", "unexpected runtime failure", 0.1)
        if outcome == "input_too_large":
            return CommandResult(
                argv,
                request.cwd,
                1,
                "",
                "input_too_large: request exceeds the 1,048,576-character limit",
                0.1,
            )
        if outcome == "invalid_json_schema":
            return CommandResult(
                argv,
                request.cwd,
                1,
                "",
                "HTTP 400 invalid_json_schema: In context=('properties', 'claim_contract'), "
                "'additionalProperties' is required to be supplied and to be false.",
                0.1,
            )
        if outcome == "invalid_json_schema_jsonl":
            return CommandResult(
                argv,
                request.cwd,
                1,
                '{"type":"error","error":{"code":"invalid_json_schema",'
                '"message":"In context=(properties, claim_contract), schema rejected"}}\n',
                "",
                0.1,
            )

        output_path = Path(argv[argv.index("--output-last-message") + 1])
        if outcome == "invalid_schema":
            output_path.write_text('{"answer":"not-an-integer"}\n', encoding="utf-8")
        elif outcome != "missing_output":
            output_path.write_text('{"answer":42}\n', encoding="utf-8")
        if outcome == "unauthorized_write":
            (request.cwd / "unauthorized.txt").write_text("changed\n", encoding="utf-8")

        if outcome == "malformed_jsonl":
            stdout = "not-json\n"
        else:
            stdout = (
                '{"type":"thread.started","thread_id":"thread-123","model":"observed"}\n'
                '{"type":"turn.started"}\n'
                '{"type":"item.completed","item":{"id":"ws-1",'
                '"type":"web_search","status":"completed",'
                '"action":{"type":"search","sources":[{"type":"url",'
                '"url":"https://doi.org/10.1000/test","title":"Source"}]}}}\n'
                '{"type":"turn.completed","usage":{"input_tokens":10,'
                '"cached_input_tokens":4,"output_tokens":4,'
                '"reasoning_output_tokens":2}}\n'
            )
        return CommandResult(argv, request.cwd, 0, stdout, "", 0.1)


class WorkspaceWritingCodexBackend(FakeCodexBackend):
    def __init__(self, project_root: Path, target: str) -> None:
        super().__init__(["success"])
        self.project_root = project_root
        self.target = target

    async def run(self, request: CommandRequest) -> CommandResult:
        result = await super().run(request)
        if request in self.exec_requests:
            if self.target == "scratch":
                target = request.cwd / "certificate.txt"
            elif self.target == "sibling":
                target = request.cwd.parent / "sibling.txt"
            elif self.target == "project":
                target = self.project_root / "project-write.txt"
            else:  # pragma: no cover - fixture construction is closed above
                raise AssertionError(f"unknown workspace-write fixture target: {self.target}")
            target.write_text("changed\n", encoding="utf-8")
        return result


class ConcurrentWorkspaceCodexBackend(FakeCodexBackend):
    def __init__(self) -> None:
        super().__init__(["success", "success"])
        self.started = 0
        self.both_started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, request: CommandRequest) -> CommandResult:
        if "--output-last-message" in request.argv:
            self.started += 1
            if self.started == 2:
                self.both_started.set()
            await self.release.wait()
        return await super().run(request)


def _run_root(tmp_path: Path) -> Path:
    run_root = tmp_path / ".matek" / "runs" / "test-run"
    run_root.mkdir(parents=True)
    return run_root


def _request(
    *,
    web_search: bool = True,
    effort: str = "xhigh",
    model: str = "gpt-5.6-sol",
    maximum_subagents: int = 0,
) -> ModelRequest:
    return ModelRequest(
        instructions="Return the answer.",
        input_text="The answer is 42.",
        settings=ModelSettings(
            model=model,
            web_search=web_search,
            reasoning_effort=effort,  # type: ignore[arg-type]
            maximum_subagents=maximum_subagents,
        ),
    )


@pytest.mark.parametrize(
    ("text", "exit_code", "expected"),
    [
        ("Logged in using ChatGPT", 0, CodexAuthenticationClass.CHATGPT),
        ("Logged in using API key", 0, CodexAuthenticationClass.API_KEY),
        ("Authenticated using access token", 0, CodexAuthenticationClass.ACCESS_TOKEN),
        ("Logged in", 0, CodexAuthenticationClass.AUTHENTICATED_UNKNOWN),
        ("Not logged in", 1, CodexAuthenticationClass.NOT_AUTHENTICATED),
        ("unexpected failure", 1, CodexAuthenticationClass.ERROR),
    ],
)
def test_parse_auth_status_without_exposing_input(
    text: str,
    exit_code: int,
    expected: CodexAuthenticationClass,
) -> None:
    status = parse_codex_auth_status(text, exit_code=exit_code)
    assert status.authentication_class is expected
    assert status.authenticated is (
        expected
        not in {
            CodexAuthenticationClass.NOT_AUTHENTICATED,
            CodexAuthenticationClass.ERROR,
        }
    )
    assert text not in status.summary


def test_realistic_multiline_installed_help_preserves_capability_grammar() -> None:
    capabilities = parse_codex_capabilities(ROOT_HELP, EXEC_HELP, resume_help=RESUME_HELP)

    assert capabilities.supported
    assert capabilities.approval_is_global
    assert capabilities.search_is_global
    assert capabilities.resume


def test_parse_jsonl_extracts_session_usage_search_and_sources() -> None:
    text = (
        '{"type":"thread.started","thread_id":"thread-a"}\n'
        '{"type":"item.completed","item":{"id":"ws-a","type":"web_search",'
        '"sources":[{"url":"https://arxiv.org/abs/1234.5678"}]}}\n'
        '{"type":"turn.completed","usage":{"input_tokens":8,"cached_input_tokens":3,'
        '"output_tokens":5,"reasoning_output_tokens":2}}\n'
    )

    result = parse_codex_jsonl(text)

    assert result.session_id == "thread-a"
    assert result.usage.input_tokens == 8
    assert result.usage.cached_input_tokens == 3
    assert result.usage.output_tokens == 5
    assert result.usage.reasoning_tokens == 2
    assert result.usage.web_search_calls == 1
    assert result.tool_metadata[0]["type"] == "web_search_call"


@pytest.mark.asyncio
async def test_structured_client_builds_safe_current_cli_argv_and_manifest(
    tmp_path: Path,
) -> None:
    backend = FakeCodexBackend()
    run_root = _run_root(tmp_path)
    client = CodexCliModelClient(tmp_path, backend=backend).for_stage(
        "research",
        run_root=run_root,
        role="solver-primary",
    )

    result = await client.generate_structured(_request(), Answer)

    assert result.parsed == Answer(answer=42)
    assert result.usage.input_tokens == 10
    assert result.usage.cached_input_tokens == 4
    assert result.usage.output_tokens == 4
    assert result.usage.reasoning_tokens == 2
    assert result.estimated_cost_usd is None
    command = backend.exec_requests[0]
    exec_index = command.argv.index("exec")
    assert command.argv[:exec_index] == (
        "codex",
        "--ask-for-approval",
        "never",
        "--search",
    )
    assert command.argv[-1] == "-"
    assert command.stdin is not None and "The answer is 42." in command.stdin
    assert "The answer is 42." not in command.argv
    assert ("--sandbox", "read-only") in tuple(zip(command.argv, command.argv[1:], strict=False))
    config_index = command.argv.index("--config")
    assert command.argv[config_index + 1] == 'model_reasoning_effort="xhigh"'
    model_index = command.argv.index("--model")
    assert command.argv[model_index + 1] == "gpt-5.6-sol"
    schema_path = Path(command.argv[command.argv.index("--output-schema") + 1])
    output_path = Path(command.argv[command.argv.index("--output-last-message") + 1])
    assert schema_path.is_relative_to(run_root)
    assert output_path.is_relative_to(run_root)
    written_schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert written_schema == strict_json_schema(Answer)
    _assert_strict_objects(written_schema)
    manifest = client.backend_manifest()
    assert manifest["provider"] == "codex"
    assert manifest["backend_version"] == "codex-cli 0.test"
    assert manifest["authentication_class"] == "chatgpt"
    assert manifest["reasoning_effort_actual"] == "xhigh"
    assert manifest["last_session_id"] == "thread-123"
    assert manifest["estimated_cost_usd"] is None
    assert manifest["no_api_fallback"] is True


@pytest.mark.asyncio
async def test_local_open_schema_fails_before_codex_execution(tmp_path: Path) -> None:
    backend = FakeCodexBackend()
    client = CodexCliModelClient(tmp_path, backend=backend).for_stage(
        "research", run_root=_run_root(tmp_path)
    )

    with pytest.raises(CodexSchemaCompatibilityError) as caught:
        await client.generate_structured(_request(), OpenAnswer)

    assert caught.value.kind is CodexErrorKind.SCHEMA_INCOMPATIBLE
    assert not caught.value.retryable
    assert "$.properties.values" in caught.value.remedy
    assert backend.exec_requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["invalid_json_schema", "invalid_json_schema_jsonl"])
async def test_provider_invalid_json_schema_is_specific_and_not_retried(
    tmp_path: Path,
    outcome: str,
) -> None:
    backend = FakeCodexBackend([outcome, "success"])
    client = CodexCliModelClient(tmp_path, backend=backend, max_attempts=2).for_stage(
        "prompt_compilation", run_root=_run_root(tmp_path)
    )

    with pytest.raises(CodexSchemaCompatibilityError) as caught:
        await client.generate_structured(_request(), Answer)

    assert caught.value.kind is CodexErrorKind.SCHEMA_INCOMPATIBLE
    assert not caught.value.retryable
    assert caught.value.attempts == 1
    assert "schema.json" in caught.value.remedy
    assert "claim_contract" in caught.value.detail
    assert len(backend.exec_requests) == 1


@pytest.mark.asyncio
async def test_optional_model_search_and_stage_reasoning_policy(tmp_path: Path) -> None:
    backend = FakeCodexBackend()
    run_root = _run_root(tmp_path)
    client = CodexCliModelClient(
        tmp_path,
        backend=backend,
        model="gpt-5.6",
        stage_policies={"manuscript": CodexStagePolicy(reasoning_effort="high")},
        extra_args=("--color", "never"),
    ).for_stage("manuscript", run_root=run_root)

    await client.generate_structured(_request(web_search=False, model="gpt-5.6"), Answer)

    argv = backend.exec_requests[0].argv
    assert "--search" not in argv
    model_index = argv.index("--model")
    assert argv[model_index + 1] == "gpt-5.6"
    config_index = argv.index("--config")
    assert argv[config_index + 1] == 'model_reasoning_effort="high"'
    assert argv[-3:] == ("--color", "never", "-")


@pytest.mark.asyncio
async def test_hierarchical_limit_is_enabled_only_for_research_workers(tmp_path: Path) -> None:
    backend = FakeCodexBackend(["success", "success", "success"])
    client = CodexCliModelClient(
        tmp_path,
        backend=backend,
    )
    run_root = _run_root(tmp_path)

    worker = client.for_stage("research", run_root=run_root, role="research-worker")
    coordinator = client.for_stage("research", run_root=run_root, role="research-coordinator")
    await worker.generate_structured(_request(maximum_subagents=8), Answer)
    await coordinator.generate_structured(_request(maximum_subagents=8), Answer)
    await worker.generate_structured(_request(), Answer)

    worker_argv = backend.exec_requests[0].argv
    coordinator_argv = backend.exec_requests[1].argv
    regular_worker_argv = backend.exec_requests[2].argv
    assert "agents.enabled=true" in worker_argv
    assert "agents.max_concurrent_threads_per_session=8" in worker_argv
    assert "agents.enabled=true" not in coordinator_argv
    assert "agents.enabled=false" in coordinator_argv
    assert not any(
        argument.startswith("agents.max_concurrent_threads_per_session=")
        for argument in coordinator_argv
    )
    assert "agents.enabled=false" in regular_worker_argv
    assert not any(
        argument.startswith("agents.max_concurrent_threads_per_session=")
        for argument in regular_worker_argv
    )


@pytest.mark.asyncio
async def test_configured_model_must_match_durable_request_identity(tmp_path: Path) -> None:
    backend = FakeCodexBackend()
    client = CodexCliModelClient(
        tmp_path,
        backend=backend,
        model="gpt-5.6-terra",
    ).for_stage("research", run_root=_run_root(tmp_path))

    with pytest.raises(ValueError, match="durable request identity"):
        await client.generate_structured(_request(model="gpt-5.6-sol"), Answer)

    assert backend.exec_requests == []


def test_extra_args_reject_backend_or_sandbox_overrides(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="only the cosmetic --color"):
        CodexCliModelClient(tmp_path, extra_args=("--sandbox", "danger-full-access"))


@pytest.mark.asyncio
async def test_invalid_pydantic_output_gets_one_targeted_repair(tmp_path: Path) -> None:
    backend = FakeCodexBackend(["invalid_schema", "success"])
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    client = CodexCliModelClient(
        tmp_path,
        backend=backend,
        max_attempts=2,
        initial_backoff_seconds=0.5,
        sleep=sleep,
        jitter=lambda: 0.0,
    ).for_stage("audit", run_root=_run_root(tmp_path))

    result = await client.generate_structured(_request(), Answer)

    assert result.parsed.answer == 42
    assert len(backend.exec_requests) == 2
    assert delays == [0.5]
    assert backend.exec_requests[1].stdin is not None
    assert "<repair>" in str(backend.exec_requests[1].stdin)
    assert "failed independent validation" in str(backend.exec_requests[1].stdin)
    assert len(result.provider_attempts) == 2
    assert result.provider_attempts[0].schema_valid is False
    assert result.provider_attempts[1].schema_valid is True


@pytest.mark.asyncio
async def test_schema_failure_is_typed_and_bounded(tmp_path: Path) -> None:
    backend = FakeCodexBackend(["invalid_schema"])
    client = CodexCliModelClient(
        tmp_path,
        backend=backend,
        max_attempts=1,
    ).for_stage("audit", run_root=_run_root(tmp_path), role="hostile")

    with pytest.raises(CodexSchemaValidationError) as caught:
        await client.generate_structured(_request(), Answer)

    assert caught.value.kind is CodexErrorKind.SCHEMA_VALIDATION_FAILED
    assert caught.value.stage == "audit"
    assert caught.value.role == "hostile"
    assert caught.value.attempts == 1
    assert caught.value.checkpoint_path.is_dir()
    assert len(caught.value.completed_provider_attempts) == 1
    assert caught.value.completed_provider_attempts[0].schema_valid is False


@pytest.mark.asyncio
async def test_rate_limit_retries_but_never_falls_back(tmp_path: Path) -> None:
    backend = FakeCodexBackend(["rate_limit", "rate_limit"])

    async def no_sleep(_: float) -> None:
        return None

    client = CodexCliModelClient(
        tmp_path,
        backend=backend,
        max_attempts=2,
        sleep=no_sleep,
        jitter=lambda: 0.0,
    ).for_stage("research", run_root=_run_root(tmp_path))

    with pytest.raises(CodexRateLimitedError) as caught:
        await client.generate_structured(_request(), Answer)

    assert caught.value.kind is CodexErrorKind.RATE_LIMITED
    assert caught.value.retryable
    assert caught.value.attempts == 2
    assert len(backend.exec_requests) == 2
    assert "did not switch" in caught.value.remedy


@pytest.mark.asyncio
async def test_input_too_large_is_not_retried_with_the_identical_prompt(tmp_path: Path) -> None:
    backend = FakeCodexBackend(["input_too_large", "success"])
    client = CodexCliModelClient(
        tmp_path,
        backend=backend,
        max_attempts=2,
    ).for_stage("research", run_root=_run_root(tmp_path))

    with pytest.raises(CodexInputTooLargeError) as caught:
        await client.generate_structured(_request(), Answer)

    assert caught.value.kind is CodexErrorKind.INPUT_TOO_LARGE
    assert not caught.value.retryable
    assert caught.value.attempts == 1
    assert len(backend.exec_requests) == 1


@pytest.mark.asyncio
async def test_timeout_preserves_partial_trace_and_is_typed(tmp_path: Path) -> None:
    backend = FakeCodexBackend(["timeout"])
    client = CodexCliModelClient(
        tmp_path,
        backend=backend,
        max_attempts=1,
    ).for_stage("research", run_root=_run_root(tmp_path))

    with pytest.raises(CodexProcessTimeoutError) as caught:
        await client.generate_structured(_request(), Answer)

    assert caught.value.kind is CodexErrorKind.PROCESS_TIMEOUT
    assert caught.value.events_path is not None
    assert caught.value.events_path.is_file()


@pytest.mark.asyncio
async def test_missing_final_output_is_typed(tmp_path: Path) -> None:
    client = CodexCliModelClient(
        tmp_path,
        backend=FakeCodexBackend(["missing_output"]),
        max_attempts=1,
    ).for_stage("manuscript", run_root=_run_root(tmp_path))

    with pytest.raises(CodexOutputMissingError) as caught:
        await client.generate_structured(_request(), Answer)

    assert caught.value.kind is CodexErrorKind.OUTPUT_MISSING
    assert caught.value.retryable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_text", "error_type", "kind"),
    [
        (
            "Not signed in",
            CodexNotAuthenticatedError,
            CodexErrorKind.NOT_AUTHENTICATED,
        ),
        (
            "authentication token expired",
            CodexAuthenticationExpiredError,
            CodexErrorKind.AUTH_EXPIRED,
        ),
    ],
)
async def test_authentication_failures_are_typed_before_exec(
    tmp_path: Path,
    status_text: str,
    error_type: type[Exception],
    kind: CodexErrorKind,
) -> None:
    backend = FakeCodexBackend(auth_stdout=status_text, auth_exit_code=1)
    client = CodexCliModelClient(tmp_path, backend=backend, max_attempts=1).for_stage(
        "research", run_root=_run_root(tmp_path)
    )

    with pytest.raises(error_type) as caught:
        await client.generate_structured(_request(), Answer)

    assert isinstance(caught.value, (CodexNotAuthenticatedError, CodexAuthenticationExpiredError))
    assert caught.value.kind is kind
    assert backend.exec_requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "error_type", "kind"),
    [
        (
            "model_unavailable",
            CodexModelUnavailableError,
            CodexErrorKind.MODEL_UNAVAILABLE,
        ),
        (
            "search_unavailable",
            CodexNetworkOrSearchUnavailableError,
            CodexErrorKind.NETWORK_OR_SEARCH_UNAVAILABLE,
        ),
        ("crash", CodexProcessCrashError, CodexErrorKind.PROCESS_CRASH),
        ("malformed_jsonl", CodexProcessCrashError, CodexErrorKind.PROCESS_CRASH),
    ],
)
async def test_runtime_failures_are_classified_without_fallback(
    tmp_path: Path,
    outcome: str,
    error_type: type[Exception],
    kind: CodexErrorKind,
) -> None:
    client = CodexCliModelClient(
        tmp_path,
        backend=FakeCodexBackend([outcome]),
        max_attempts=1,
    ).for_stage("research", run_root=_run_root(tmp_path))

    with pytest.raises(error_type) as caught:
        await client.generate_structured(_request(), Answer)

    assert isinstance(
        caught.value,
        (CodexModelUnavailableError, CodexNetworkOrSearchUnavailableError, CodexProcessCrashError),
    )
    assert caught.value.kind is kind


@pytest.mark.asyncio
async def test_explicit_session_resume_uses_safe_resume_argv(tmp_path: Path) -> None:
    backend = FakeCodexBackend()
    client = (
        CodexCliModelClient(tmp_path, backend=backend, persist_sessions=True)
        .for_stage("research", run_root=_run_root(tmp_path), role="follow-up")
        .with_session("thread-previous")
    )

    result = await client.generate_structured(_request(), Answer)

    argv = backend.exec_requests[0].argv
    assert argv[argv.index("exec") : argv.index("exec") + 2] == ("exec", "resume")
    assert argv[-2:] == ("thread-previous", "-")
    assert "--ephemeral" not in argv
    assert result.request_metadata["session_id"] == "thread-123"


@pytest.mark.asyncio
async def test_workspace_write_detects_unauthorized_change(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    backend = FakeCodexBackend(["unauthorized_write"])
    client = CodexCliModelClient(
        tmp_path,
        backend=backend,
        max_attempts=2,
        stage_policies={
            "lean_formalization": CodexStagePolicy(
                sandbox="workspace-write",
                web_search=False,
                allowed_write_paths=(allowed,),
            )
        },
    ).for_stage("lean_formalization", run_root=_run_root(tmp_path))

    with pytest.raises(CodexUnauthorizedFileChangeError) as caught:
        await client.generate_structured(_request(web_search=False), Answer)

    assert caught.value.kind is CodexErrorKind.UNAUTHORIZED_FILE_CHANGE
    assert not caught.value.retryable
    assert len(backend.exec_requests) == 1
    assert "unauthorized.txt" in caught.value.detail


@pytest.mark.asyncio
async def test_research_worker_workspace_binding_uses_private_cwd_and_accepts_only_scratch(
    tmp_path: Path,
) -> None:
    run_root = _run_root(tmp_path)
    assignment_root = run_root / "research" / "workspaces" / "assignment-01"
    scratch = assignment_root / "scratch"
    scratch.mkdir(parents=True)
    backend = WorkspaceWritingCodexBackend(tmp_path, "scratch")
    worker = (
        CodexCliModelClient(tmp_path, backend=backend)
        .for_stage("research", run_root=run_root, role="research-worker")
        .for_workspace(scratch, writable_paths=(scratch,))
    )

    result = await worker.generate_structured(_request(web_search=False), Answer)

    assert result.parsed.answer == 42
    assert (scratch / "certificate.txt").read_text(encoding="utf-8") == "changed\n"
    command = backend.exec_requests[0]
    assert command.cwd == scratch
    assert command.argv[command.argv.index("-C") + 1] == str(scratch)
    assert command.argv[command.argv.index("--sandbox") + 1] == "workspace-write"
    output_path = Path(command.argv[command.argv.index("--output-last-message") + 1])
    assert output_path.is_relative_to(run_root)
    assert not output_path.is_relative_to(assignment_root)


@pytest.mark.asyncio
async def test_concurrent_runs_ignore_each_others_graph_and_workspace_writes(
    tmp_path: Path,
) -> None:
    backend = ConcurrentWorkspaceCodexBackend()
    workers: list[CodexCliModelClient] = []
    graph_roots: list[Path] = []
    for suffix in ("a", "b"):
        run_root = tmp_path / ".matek" / "runs" / f"test-run-{suffix}"
        scratch = run_root / "research" / "workspaces" / f"assignment-{suffix}" / "scratch"
        scratch.mkdir(parents=True)
        graph_root = tmp_path / ".matek" / "knowledge" / f"graph-{suffix}"
        graph_root.mkdir(parents=True)
        graph_roots.append(graph_root)
        workers.append(
            CodexCliModelClient(tmp_path, backend=backend, max_attempts=1)
            .for_stage("research", run_root=run_root, role="research-worker")
            .for_workspace(scratch, writable_paths=(scratch,))
        )

    calls = [
        asyncio.create_task(worker.generate_structured(_request(web_search=False), Answer))
        for worker in workers
    ]
    await asyncio.wait_for(backend.both_started.wait(), timeout=2)
    for index, graph_root in enumerate(graph_roots):
        (graph_root / "state.json").write_text(f'{{"revision":{index + 1}}}\n', encoding="utf-8")
    backend.release.set()

    results = await asyncio.gather(*calls)

    assert [result.parsed.answer for result in results] == [42, 42]
    assert len(backend.exec_requests) == 2


def test_research_worker_workspace_binding_rejects_broader_write_capabilities(
    tmp_path: Path,
) -> None:
    run_root = _run_root(tmp_path)
    assignment_root = run_root / "research" / "workspaces" / "assignment-01"
    scratch = assignment_root / "scratch"
    scratch.mkdir(parents=True)
    sibling_graph = tmp_path / ".matek" / "knowledge" / "other-graph"
    sibling_graph.mkdir(parents=True)
    client = CodexCliModelClient(tmp_path, backend=FakeCodexBackend()).for_stage(
        "research", run_root=run_root, role="research-worker"
    )

    with pytest.raises(ValueError, match="private scratch"):
        client.for_workspace(assignment_root, writable_paths=(assignment_root,))
    with pytest.raises(ValueError, match="confined"):
        client.for_workspace(sibling_graph, writable_paths=(sibling_graph,))
    with pytest.raises(ValueError, match="canonical path"):
        client.for_workspace(scratch, writable_paths=(scratch / "..",))

    escape = scratch / "escape"
    escape.symlink_to(sibling_graph, target_is_directory=True)
    with pytest.raises(ValueError, match="canonical path"):
        client.for_workspace(scratch, writable_paths=(escape,))


def test_workspace_binding_validates_role_confinement_and_explicit_scratch(
    tmp_path: Path,
) -> None:
    run_root = _run_root(tmp_path)
    assignment_root = run_root / "research" / "workspaces" / "assignment-01"
    scratch = assignment_root / "scratch"
    scratch.mkdir(parents=True)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    client = CodexCliModelClient(tmp_path, backend=FakeCodexBackend())

    with pytest.raises(ValueError, match="research-worker"):
        client.for_stage("audit", run_root=run_root, role="hostile").for_workspace(
            scratch,
            writable_paths=(scratch,),
        )
    with pytest.raises(ValueError, match="confined"):
        client.for_stage("research", run_root=run_root, role="research-worker").for_workspace(
            outside, writable_paths=(outside,)
        )
    with pytest.raises(ValueError, match="explicit writable"):
        client.for_stage("research", run_root=run_root, role="research-worker").for_workspace(
            scratch
        )
    with pytest.raises(ValueError, match="private scratch"):
        client.for_stage("research", run_root=run_root, role="research-worker").for_workspace(
            assignment_root, writable_paths=(assignment_root,)
        )
    with pytest.raises(ValueError, match="does not exist"):
        client.for_stage("research", run_root=run_root, role="research-worker").for_workspace(
            scratch,
            writable_paths=(assignment_root / "missing",),
        )


def test_workspace_binding_is_dropped_when_cloned_for_another_role(tmp_path: Path) -> None:
    run_root = _run_root(tmp_path)
    assignment_root = run_root / "research" / "workspaces" / "assignment-01"
    scratch = assignment_root / "scratch"
    scratch.mkdir(parents=True)
    bound = (
        CodexCliModelClient(tmp_path, backend=FakeCodexBackend())
        .for_stage("research", run_root=run_root, role="research-worker")
        .for_workspace(scratch, writable_paths=(scratch,))
    )

    coordinator = bound.for_stage(
        "research",
        run_root=run_root,
        role="research-coordinator",
    )

    policy = coordinator._resolved_policy(_request())
    assert policy.sandbox == "read-only"
    assert policy.allowed_write_paths == ()


def test_error_classification_is_specific_and_redacted() -> None:
    assert classify_codex_failure("allowance exhausted").kind is CodexErrorKind.ALLOWANCE_EXHAUSTED
    assert classify_codex_failure("model is not available").kind is CodexErrorKind.MODEL_UNAVAILABLE
    assert (
        classify_codex_failure("web search unavailable").kind
        is CodexErrorKind.NETWORK_OR_SEARCH_UNAVAILABLE
    )


@pytest.mark.asyncio
async def test_native_fake_executable_receives_stdin_without_ambient_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = tmp_path / "fake-codex"
    fake.write_text(
        f"""#!{sys.executable}
import json
import os
import pathlib
import sys

args = sys.argv[1:]
if args == ["--help"]:
    print({ROOT_HELP!r})
elif args == ["exec", "--help"]:
    print({EXEC_HELP!r})
elif args == ["exec", "resume", "--help"]:
    print({RESUME_HELP!r})
elif args == ["--version"]:
    print("codex-cli fake-native")
elif args == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    prompt = sys.stdin.read()
    output = pathlib.Path(args[args.index("--output-last-message") + 1])
    safe_environment = "OPENAI_API_KEY" not in os.environ and "CODEX_API_KEY" not in os.environ
    output.write_text(json.dumps({{"answer": 42 if prompt and safe_environment else 0}}) + "\\n")
    print(json.dumps({{"type":"thread.started","thread_id":"native-thread"}}))
    print(json.dumps({{"type":"turn.completed","usage":{{"input_tokens":1,"cached_input_tokens":0,"output_tokens":1,"reasoning_output_tokens":0}}}}))
""",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-do-not-inherit-this")
    monkeypatch.setenv("CODEX_API_KEY", "sk-do-not-inherit-either")
    client = CodexCliModelClient(
        tmp_path,
        executable=str(fake),
        backend=NativeBackend(),
        max_attempts=1,
    ).for_stage("research", run_root=_run_root(tmp_path))

    result = await client.generate_structured(_request(web_search=False), Answer)

    assert result.parsed.answer == 42


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="process-group cleanup assertion is POSIX-specific")
async def test_cancellation_terminates_native_codex_process(tmp_path: Path) -> None:
    fake = tmp_path / "hanging-codex"
    fake.write_text(
        f"""#!{sys.executable}
import os
import pathlib
import sys
import time

args = sys.argv[1:]
if args == ["--help"]:
    print({ROOT_HELP!r})
elif args == ["exec", "--help"]:
    print({EXEC_HELP!r})
elif args == ["exec", "resume", "--help"]:
    print({RESUME_HELP!r})
elif args == ["--version"]:
    print("codex-cli fake-hanging")
elif args == ["login", "status"]:
    print("Logged in using ChatGPT")
else:
    pathlib.Path("running.pid").write_text(str(os.getpid()))
    while True:
        time.sleep(1)
""",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    client = CodexCliModelClient(
        tmp_path,
        executable=str(fake),
        backend=NativeBackend(termination_grace_seconds=0.1),
        max_attempts=1,
    ).for_stage("research", run_root=_run_root(tmp_path))
    task = asyncio.create_task(client.generate_structured(_request(web_search=False), Answer))
    pid_path = tmp_path / "running.pid"
    for _ in range(200):
        if pid_path.is_file():
            break
        await asyncio.sleep(0.01)
    assert pid_path.is_file()
    pid = int(pid_path.read_text(encoding="utf-8"))

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
