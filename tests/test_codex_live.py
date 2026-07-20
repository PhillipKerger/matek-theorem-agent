from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from ascend_math_agent.codex_model_backend import (
    CodexAuthenticationClass,
    CodexCliModelClient,
)
from ascend_math_agent.config import ModelSettings
from ascend_math_agent.openai_client import ModelRequest
from ascend_math_agent.stages.compile_prompt import (
    CompiledProblem,
    PromptCompilationStatus,
)


class _LiveProbeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool


pytestmark = [
    pytest.mark.codex_live,
    pytest.mark.skipif(
        os.environ.get("ASCEND_CODEX_LIVE_TESTS") != "1",
        reason="set ASCEND_CODEX_LIVE_TESTS=1 to consume Codex allowance",
    ),
]


@pytest.mark.asyncio
async def test_live_codex_saved_auth_structured_read_only_call(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    use_search = os.environ.get("ASCEND_CODEX_LIVE_SEARCH") == "1"
    client = CodexCliModelClient(
        tmp_path,
        max_attempts=1,
        timeout_seconds=120,
        skip_git_repo_check=True,
    ).for_stage("live_probe", run_root=run_root, role="doctor")

    probe = await client.probe()
    assert probe.capabilities.supported
    assert probe.authentication.authentication_class in {
        CodexAuthenticationClass.CHATGPT,
        CodexAuthenticationClass.API_KEY,
        CodexAuthenticationClass.ACCESS_TOKEN,
        CodexAuthenticationClass.AUTHENTICATED_UNKNOWN,
    }
    result = await client.generate_structured(
        ModelRequest(
            instructions="Return the requested probe object. Do not run commands or edit files.",
            input_text="Set ok to true.",
            settings=ModelSettings(
                model="gpt-5.6-sol",
                reasoning_mode="standard",
                reasoning_effort="low",
                web_search=use_search,
                maximum_web_search_calls=1,
                max_output_tokens=128,
            ),
        ),
        _LiveProbeOutput,
    )

    assert result.parsed == _LiveProbeOutput(ok=True)
    assert list((run_root / "traces" / "codex" / "live_probe").rglob("events.jsonl"))


@pytest.mark.asyncio
async def test_live_codex_prompt_compilation_nested_schema(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    client = CodexCliModelClient(
        tmp_path,
        max_attempts=1,
        timeout_seconds=120,
        skip_git_repo_check=True,
    ).for_stage("prompt_compilation", run_root=run_root, role="compiler-smoke")

    result = await client.generate_structured(
        ModelRequest(
            instructions=(
                "Return a needs_clarification CompiledProblem. Populate every schema field. "
                "Use an empty claim contract and source ledger, unknown literature status, "
                "and null literature summary."
            ),
            input_text="The problem is intentionally unspecified; ask what theorem to prove.",
            settings=ModelSettings(
                model="gpt-5.6-sol",
                reasoning_mode="standard",
                reasoning_effort="low",
                web_search=False,
                max_output_tokens=1_024,
            ),
        ),
        CompiledProblem,
    )

    assert result.parsed.status is PromptCompilationStatus.NEEDS_CLARIFICATION
    assert not result.parsed.claim_contract.entries
    schema_paths = list((run_root / "traces" / "codex" / "prompt_compilation").rglob("schema.json"))
    assert len(schema_paths) == 1
