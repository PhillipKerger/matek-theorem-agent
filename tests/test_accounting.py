from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, TypeVar

import pytest
from pydantic import BaseModel

from ascend_math_agent.accounting import AccountingModelClient
from ascend_math_agent.budget import BudgetExceeded, BudgetTracker, UsageRecord
from ascend_math_agent.config import Limits, ModelSettings
from ascend_math_agent.logging import (
    JournalCorruptionError,
    ModelCallJournalError,
    RunLogger,
    load_usage_journal_strict,
)
from ascend_math_agent.openai_client import (
    ModelRequest,
    ModelResult,
    UsageMetadata,
    normalized_model_request,
)

T = TypeVar("T", bound=BaseModel)


class Answer(BaseModel):
    value: str


class FakeClient:
    def __init__(self) -> None:
        self.calls = 0

    async def generate_structured(
        self, request: ModelRequest, output_type: type[T]
    ) -> ModelResult[T]:
        self.calls += 1
        parsed = output_type.model_validate({"value": "ok"})
        return ModelResult(
            parsed=parsed,
            response_id="resp_fixture",
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            estimated_cost_usd=0.25,
            usage=UsageMetadata(
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                cached_input_tokens=2,
                reasoning_tokens=3,
                estimated_cost_usd=0.25,
            ),
        )


class SlowClient:
    def __init__(self) -> None:
        self.cancelled = False

    async def generate_structured(
        self, request: ModelRequest, output_type: type[T]
    ) -> ModelResult[T]:
        del request, output_type
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("slow fixture should have been cancelled")


class RoleAwareClient(FakeClient):
    def __init__(self, observed_roles: list[str | None] | None = None) -> None:
        super().__init__()
        self.observed_roles = observed_roles if observed_roles is not None else []

    def for_stage(
        self,
        stage: str,
        *,
        run_root: Path,
        role: str | None = None,
    ) -> RoleAwareClient:
        del stage, run_root
        self.observed_roles.append(role)
        return self


async def test_accounting_decorator_logs_and_aggregates(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    (run_root / "logs").mkdir(parents=True)
    budget = BudgetTracker(Limits(maximum_cost_usd=1.0))
    client = AccountingModelClient(
        FakeClient(),
        stage="prompt_compilation",
        budget=budget,
        logger=RunLogger(run_root),
    )

    result = await client.generate_structured(
        ModelRequest("compile", "problem", ModelSettings()), Answer
    )

    assert result.parsed.value == "ok"
    assert budget.snapshot().cost_usd == 0.25
    usage = json.loads((run_root / "logs" / "usage.jsonl").read_text().splitlines()[0])
    assert usage["usage"]["response_id"] == "resp_fixture"
    assert usage["usage"]["reasoning_tokens"] == 3


async def test_accounting_decorator_creates_explicit_model_role_contexts(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    (run_root / "logs").mkdir(parents=True)
    delegate = RoleAwareClient()
    client = AccountingModelClient(
        delegate,
        stage="research",
        budget=BudgetTracker(Limits(maximum_cost_usd=1.0)),
        logger=RunLogger(run_root),
    )

    orchestrator = client.for_role("research-orchestrator")
    await orchestrator.generate_structured(ModelRequest("plan", "problem", ModelSettings()), Answer)

    assert delegate.observed_roles == [None, "research-orchestrator"]


async def test_run_wall_clock_cancels_an_in_flight_model_call(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    (run_root / "logs").mkdir(parents=True)
    delegate = SlowClient()
    budget = BudgetTracker(Limits(maximum_cost_usd=1.0, maximum_wall_clock_hours=0.00003))
    client = AccountingModelClient(
        delegate,
        stage="research",
        budget=budget,
        logger=RunLogger(run_root),
    )

    with pytest.raises(BudgetExceeded, match="wall_clock"):
        await client.generate_structured(
            ModelRequest("research", "problem", ModelSettings()), Answer
        )

    assert delegate.cancelled
    assert budget.snapshot().calls == 0


async def test_model_call_is_atomically_checkpointed_and_replayed_after_restart(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    (run_root / "logs").mkdir(parents=True)
    request = ModelRequest(
        "compile\r\nAuthorization: Bearer sk-proj-super-secret-token",
        "problem",
        ModelSettings(),
    )
    first_delegate = FakeClient()
    first_budget = BudgetTracker(Limits(maximum_cost_usd=1.0))
    first = AccountingModelClient(
        first_delegate,
        stage="prompt_compilation",
        budget=first_budget,
        logger=RunLogger(run_root),
    )

    initial = await first.generate_structured(request, Answer)

    records = list((run_root / "logs" / "model_calls").glob("*.json"))
    assert len(records) == 1
    assert not list((run_root / "logs" / "model_calls").glob("*.tmp"))
    checkpoint_text = records[0].read_text(encoding="utf-8")
    checkpoint = json.loads(checkpoint_text)
    assert "sk-proj-super-secret-token" not in checkpoint_text
    assert "Authorization: Bearer" not in checkpoint_text
    assert checkpoint["request"] == {
        "input_text_sha256": checkpoint["request"]["input_text_sha256"],
        "instructions_sha256": checkpoint["request"]["instructions_sha256"],
        "maximum_web_search_calls": 8,
        "max_output_tokens": 100_000,
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "reasoning_mode": "pro",
        "web_search": True,
    }
    assert checkpoint["response_id"] == "resp_fixture"
    assert checkpoint["cache_namespace"] == "default"
    assert checkpoint["parsed"] == {"value": "ok"}

    second_delegate = FakeClient()
    recovered_usage = load_usage_journal_strict(run_root / "logs" / "usage.jsonl")
    second_budget = BudgetTracker(Limits(maximum_cost_usd=1.0), recovered_usage)
    second = AccountingModelClient(
        second_delegate,
        stage="prompt_compilation",
        budget=second_budget,
        logger=RunLogger(run_root),
    )

    replayed = await second.generate_structured(request, Answer)

    assert initial == replayed
    assert first_delegate.calls == 1
    assert second_delegate.calls == 0
    assert second_budget.snapshot().calls == 1
    assert len((run_root / "logs" / "usage.jsonl").read_text().splitlines()) == 1


async def test_replay_backfills_usage_if_crash_happened_after_response_checkpoint(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    (run_root / "logs").mkdir(parents=True)
    logger = RunLogger(run_root)
    request = ModelRequest("compile", "problem", ModelSettings())
    identity = logger.model_calls.identity(
        normalized_model_request(request, Answer, stage="prompt_compilation")
    )
    logger.model_calls.persist(
        identity,
        stage="prompt_compilation",
        response_id="resp_crash_boundary",
        status="completed",
        usage=UsageRecord(
            response_id="resp_crash_boundary",
            stage="prompt_compilation",
            model="gpt-5.6-sol",
            input_tokens=12,
            output_tokens=3,
            cost_usd=0.1,
        ),
        tool_metadata=[{"type": "web_search_call", "id": "ws_1", "status": "completed"}],
        parsed=Answer(value="recovered"),
    )
    delegate = FakeClient()
    budget = BudgetTracker(Limits(maximum_cost_usd=1.0))
    client = AccountingModelClient(
        delegate,
        stage="prompt_compilation",
        budget=budget,
        logger=logger,
    )

    result = await client.generate_structured(request, Answer)

    assert result.parsed.value == "recovered"
    assert delegate.calls == 0
    assert budget.snapshot().cost_usd == 0.1
    usage = load_usage_journal_strict(run_root / "logs" / "usage.jsonl")
    assert [item.response_id for item in usage] == ["resp_crash_boundary"]


async def test_disabling_replay_in_used_namespace_fails_before_another_paid_call(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    (run_root / "logs").mkdir(parents=True)
    logger = RunLogger(run_root)
    budget = BudgetTracker(Limits(maximum_cost_usd=1.0))
    request = ModelRequest("compile", "problem", ModelSettings())
    await AccountingModelClient(
        FakeClient(),
        stage="prompt_compilation",
        budget=budget,
        logger=logger,
    ).generate_structured(request, Answer)
    second_delegate = FakeClient()
    no_replay = AccountingModelClient(
        second_delegate,
        stage="prompt_compilation",
        budget=budget,
        logger=logger,
        replay_completed=False,
    )

    with pytest.raises(ModelCallJournalError, match="fresh cache_namespace"):
        await no_replay.generate_structured(request, Answer)

    assert second_delegate.calls == 0


def test_usage_journal_loader_rejects_any_malformed_or_duplicate_row(tmp_path: Path) -> None:
    path = tmp_path / "usage.jsonl"
    valid: dict[str, Any] = {
        "timestamp": "2026-07-19T12:00:00Z",
        "run_id": "run",
        "usage": UsageRecord(
            response_id="resp_duplicate",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.01,
        ).model_dump(mode="json"),
    }
    path.write_text(f"{json.dumps(valid)}\n{{not-json}}\n", encoding="utf-8")
    with pytest.raises(JournalCorruptionError):
        load_usage_journal_strict(path)

    path.write_text(f"{json.dumps(valid)}\n{json.dumps(valid)}\n", encoding="utf-8")
    with pytest.raises(JournalCorruptionError):
        load_usage_journal_strict(path)
