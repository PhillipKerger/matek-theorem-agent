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
from ascend_math_agent.stages.research import _ResearchBudgetExhausted, _TrackedModelClient

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


async def test_research_resume_repairs_missing_response_mapping_at_full_call_cap(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    (run_root / "logs").mkdir(parents=True)
    namespace = "api-generation-0"
    request = ModelRequest("coordinate", "durable mailbox", ModelSettings())
    first_delegate = FakeClient()
    first_budget = BudgetTracker(Limits(maximum_cost_usd=1.0), maximum_calls=1)
    first = AccountingModelClient(
        first_delegate,
        stage="research",
        budget=first_budget,
        logger=RunLogger(run_root, model_cache_namespace=namespace),
    )
    request_key = first.request_cache_key(request, Answer)

    await first.generate_structured(request, Answer)

    # Simulate a crash after the accounting cache and usage journal became durable,
    # but before the research scheduler saved the request-to-response mapping.
    recovered_usage = load_usage_journal_strict(run_root / "logs" / "usage.jsonl")
    resumed_budget = BudgetTracker(
        Limits(maximum_cost_usd=1.0),
        recovered_usage,
        maximum_calls=1,
    )
    resumed_delegate = FakeClient()
    resumed_accounting = AccountingModelClient(
        resumed_delegate,
        stage="research",
        budget=resumed_budget,
        logger=RunLogger(run_root, model_cache_namespace=namespace),
    )
    scheduler = _TrackedModelClient(
        resumed_accounting,
        1,
        calls=1,
        call_keys=[request_key],
        response_ids=[],
        response_ids_by_call_key={},
    )

    replayed = await scheduler.generate(
        instructions=request.instructions,
        input_text=request.input_text,
        settings=request.settings,
        output_type=Answer,
    )

    assert replayed.response_id == "resp_fixture"
    assert resumed_delegate.calls == 0
    assert scheduler.calls == 1
    assert scheduler.response_ids == ["resp_fixture"]
    assert scheduler.response_ids_by_call_key == {request_key: "resp_fixture"}
    assert resumed_budget.snapshot().calls == 1
    assert len((run_root / "logs" / "usage.jsonl").read_text().splitlines()) == 1


async def test_cached_response_backfill_consumes_one_call_without_transferable_capacity(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    (run_root / "logs").mkdir(parents=True)
    logger = RunLogger(run_root, model_cache_namespace="api-generation-0")
    request = ModelRequest("coordinate", "cached request", ModelSettings())
    identity = logger.model_calls.identity(
        normalized_model_request(
            request,
            Answer,
            stage="research",
            cache_namespace="api-generation-0",
        )
    )
    logger.model_calls.persist(
        identity,
        stage="research",
        response_id="resp_cache_without_usage",
        status="completed",
        usage=UsageRecord(
            response_id="resp_cache_without_usage",
            stage="research",
            model="gpt-5.6-sol",
            input_tokens=12,
            output_tokens=3,
            cost_usd=0.1,
        ),
        tool_metadata=[],
        parsed=Answer(value="recovered"),
    )
    delegate = FakeClient()
    budget = BudgetTracker(Limits(maximum_cost_usd=1.0), maximum_calls=1)
    client = AccountingModelClient(
        delegate,
        stage="research",
        budget=budget,
        logger=logger,
    )

    first = await client.generate_structured(request, Answer)
    second = await client.generate_structured(request, Answer)

    assert first == second
    assert delegate.calls == 0
    assert budget.snapshot().calls == 1
    assert budget.remaining().calls == 0
    assert len((run_root / "logs" / "usage.jsonl").read_text().splitlines()) == 1
    with pytest.raises(BudgetExceeded) as raised:
        await client.generate_structured(
            ModelRequest("coordinate", "different uncached request", ModelSettings()),
            Answer,
        )
    assert raised.value.dimension == "calls"
    assert delegate.calls == 0
    assert budget.snapshot().calls == 1


async def test_fresh_research_scheduler_gets_request_specific_credit_for_accounted_cache_hit(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    (run_root / "logs").mkdir(parents=True)
    namespace = "api-generation-0"
    request = ModelRequest("coordinate", "replay me", ModelSettings())
    initial_budget = BudgetTracker(Limits(maximum_cost_usd=1.0), maximum_calls=1)
    await AccountingModelClient(
        FakeClient(),
        stage="research",
        budget=initial_budget,
        logger=RunLogger(run_root, model_cache_namespace=namespace),
    ).generate_structured(request, Answer)

    recovered_usage = load_usage_journal_strict(run_root / "logs" / "usage.jsonl")
    assert len(recovered_usage) == 1
    recovered_budget = BudgetTracker(
        Limits(maximum_cost_usd=1.0),
        recovered_usage,
        maximum_calls=1,
    )
    assert recovered_budget.remaining().calls == 0
    delegate = FakeClient()
    accounting = AccountingModelClient(
        delegate,
        stage="research",
        budget=recovered_budget,
        logger=RunLogger(run_root, model_cache_namespace=namespace),
    )
    scheduler = _TrackedModelClient(accounting, 0)

    replayed = await scheduler.generate(
        instructions=request.instructions,
        input_text=request.input_text,
        settings=request.settings,
        output_type=Answer,
    )

    replayed_key = accounting.request_cache_key(request, Answer)
    assert replayed.response_id == "resp_fixture"
    assert delegate.calls == 0
    assert scheduler.calls == 1
    assert scheduler.maximum_calls == 1
    assert scheduler.response_ids_by_call_key == {replayed_key: "resp_fixture"}
    assert len((run_root / "logs" / "usage.jsonl").read_text().splitlines()) == 1

    with pytest.raises(_ResearchBudgetExhausted):
        await scheduler.generate(
            instructions="coordinate",
            input_text="uncached and therefore not credited",
            settings=ModelSettings(),
            output_type=Answer,
        )
    assert delegate.calls == 0
    assert recovered_budget.snapshot().calls == 1


async def test_accounted_replay_preserves_paid_headroom_but_obeys_hard_logical_cap(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    (run_root / "logs").mkdir(parents=True)
    namespace = "api-generation-0"
    request = ModelRequest("coordinate", "cached", ModelSettings())
    initial_budget = BudgetTracker(Limits(maximum_cost_usd=1.0), maximum_calls=3)
    await AccountingModelClient(
        FakeClient(),
        stage="research",
        budget=initial_budget,
        logger=RunLogger(run_root, model_cache_namespace=namespace),
    ).generate_structured(request, Answer)
    recovered_budget = BudgetTracker(
        Limits(maximum_cost_usd=1.0),
        load_usage_journal_strict(run_root / "logs" / "usage.jsonl"),
        maximum_calls=3,
    )
    accounting = AccountingModelClient(
        FakeClient(),
        stage="research",
        budget=recovered_budget,
        logger=RunLogger(run_root, model_cache_namespace=namespace),
    )
    scheduler = _TrackedModelClient(accounting, 2, hard_maximum_calls=3)

    await scheduler.generate(
        instructions=request.instructions,
        input_text=request.input_text,
        settings=request.settings,
        output_type=Answer,
    )

    assert scheduler.calls == 1
    assert scheduler.can_admit(paid_calls=2, logical_calls=2)
    assert not scheduler.can_admit(paid_calls=2, logical_calls=3)
    scheduler.register_request(
        instructions="new",
        input_text="one",
        settings=ModelSettings(),
        output_type=Answer,
    )
    scheduler.reserve_call_key("f" * 64)
    with pytest.raises(_ResearchBudgetExhausted):
        scheduler.register_request(
            instructions="new",
            input_text="two",
            settings=ModelSettings(),
            output_type=Answer,
        )


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
