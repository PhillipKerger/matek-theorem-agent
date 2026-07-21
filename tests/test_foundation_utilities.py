from __future__ import annotations

import json
from pathlib import Path

import pytest

from ascend_math_agent.budget import BudgetExceeded, BudgetTracker, UsageRecord
from ascend_math_agent.config import Limits
from ascend_math_agent.logging import RunLogger
from ascend_math_agent.redaction import REDACTED, SecretRedactor, contains_secret
from ascend_math_agent.workspace import create_run_root


def test_text_and_structured_secret_redaction() -> None:
    secret = "sk-proj-super-secret-token"
    redactor = SecretRedactor([secret])
    text = redactor.redact_text(f"Authorization: Bearer {secret}")
    data = redactor.redact_data({"OPENAI_API_KEY": secret, "safe": "value"})
    assert secret not in text
    assert data == {"OPENAI_API_KEY": REDACTED, "safe": "value"}
    assert not contains_secret(text, secrets=[secret])


def test_run_logger_writes_redacted_event_usage_and_notice(tmp_path: Path) -> None:
    run_root = create_run_root(tmp_path, run_id="20260719T123456Z-logs-abcdef")
    secret = "sk-proj-super-secret-token"
    logger = RunLogger(run_root, secrets=(secret,))
    logger.event("adapter.failed", data={"error": f"key={secret}"})
    logger.usage(
        UsageRecord(
            response_id="resp_1",
            model="model",
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.25,
        )
    )

    event_text = (run_root / "logs" / "events.jsonl").read_text(encoding="utf-8")
    usage = json.loads((run_root / "logs" / "usage.jsonl").read_text(encoding="utf-8"))
    notice = json.loads((run_root / "logs" / "redaction.log").read_text(encoding="utf-8"))
    assert secret not in event_text
    assert REDACTED in event_text
    assert usage["usage"]["input_tokens"] == 10
    assert usage["usage"]["total_tokens"] == 15
    assert notice["replacements"] >= 1


def test_run_logger_creates_all_contracted_logs_immediately(tmp_path: Path) -> None:
    run_root = create_run_root(tmp_path, run_id="20260719T123456Z-empty-logs-abcdef")
    RunLogger(run_root)
    assert {path.name for path in (run_root / "logs").iterdir() if path.is_file()} == {
        "events.jsonl",
        "usage.jsonl",
        "redaction.log",
    }


def test_budget_accounts_usage_and_blocks_projected_cost() -> None:
    tracker = BudgetTracker(Limits(maximum_cost_usd=1.0, maximum_wall_clock_hours=1.0))
    snapshot = tracker.record(UsageRecord(input_tokens=10, output_tokens=5, cost_usd=0.75))
    assert snapshot.total_tokens == 15
    with pytest.raises(BudgetExceeded) as error:
        tracker.ensure_available(estimated_cost_usd=0.5)
    assert error.value.dimension == "cost_usd"


def test_budget_enforces_token_and_wall_clock_limits() -> None:
    ticks = iter([0.0, 0.0, 3_601.0])
    tracker = BudgetTracker(
        Limits(
            maximum_cost_usd=10.0,
            maximum_wall_clock_hours=1.0,
            maximum_total_tokens=10,
        ),
        monotonic=lambda: next(ticks),
    )
    with pytest.raises(BudgetExceeded) as token_error:
        tracker.ensure_available(estimated_tokens=11)
    assert token_error.value.dimension == "tokens"
    with pytest.raises(BudgetExceeded) as time_error:
        tracker.ensure_available()
    assert time_error.value.dimension == "wall_clock"


def test_budget_reservations_are_atomic_and_reconcile_actual_usage() -> None:
    tracker = BudgetTracker(
        Limits(
            maximum_cost_usd=1.0,
            maximum_wall_clock_hours=1.0,
            maximum_total_tokens=100,
        )
    )
    first = tracker.reserve(estimated_cost_usd=0.6, estimated_tokens=60)
    with pytest.raises(BudgetExceeded):
        tracker.reserve(estimated_cost_usd=0.5, estimated_tokens=50)
    assert tracker.remaining().cost_usd == pytest.approx(0.4)

    snapshot = tracker.reconcile(
        first,
        UsageRecord(input_tokens=20, output_tokens=10, cost_usd=0.3),
    )

    assert snapshot.cost_usd == pytest.approx(0.3)
    assert snapshot.total_tokens == 30
    assert tracker.remaining().tokens == 70


def test_budget_carries_elapsed_time_across_resume() -> None:
    ticks = iter([10.0, 15.0])
    tracker = BudgetTracker(
        Limits(maximum_cost_usd=1.0, maximum_wall_clock_hours=1.0),
        monotonic=lambda: next(ticks),
        prior_elapsed_seconds=30.0,
    )

    assert tracker.snapshot().elapsed_seconds == 35.0


def test_budget_has_no_wall_clock_limit_by_default() -> None:
    tracker = BudgetTracker(
        Limits(maximum_cost_usd=1.0),
        prior_elapsed_seconds=10_000_000.0,
    )

    assert tracker.remaining().wall_clock_seconds is None
    tracker.ensure_available()


def test_unknown_cost_fails_closed_before_another_call() -> None:
    tracker = BudgetTracker(Limits(maximum_cost_usd=10.0, maximum_wall_clock_hours=1.0))
    tracker.record(UsageRecord(input_tokens=1, output_tokens=1), enforce=False)

    with pytest.raises(BudgetExceeded) as error:
        tracker.ensure_available()

    assert error.value.dimension == "cost_usd"
    assert error.value.actual == float("inf")


def test_subscription_budget_tracks_unknown_cost_without_applying_api_dollar_gate() -> None:
    tracker = BudgetTracker(
        Limits(maximum_cost_usd=10.0, maximum_wall_clock_hours=1.0),
        enforce_cost_budget=False,
        maximum_calls=2,
    )
    tracker.record(UsageRecord(response_id="codex-1", cost_usd=None))
    tracker.ensure_available(estimated_calls=1)
    tracker.record(UsageRecord(response_id="codex-2", cost_usd=None))

    with pytest.raises(BudgetExceeded) as error:
        tracker.ensure_available(estimated_calls=1)

    assert error.value.dimension == "calls"
    assert tracker.snapshot().unknown_cost_calls == 2
