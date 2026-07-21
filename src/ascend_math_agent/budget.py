"""Deterministic usage accounting and pre-call budget checks."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import Limits


class UsageRecord(BaseModel):
    """One paid model/tool usage observation.

    Cached input tokens are reported separately for cost analysis but are already a
    subset of ``input_tokens`` and therefore are not added again to ``total_tokens``.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    response_id: str | None = None
    stage: str | None = None
    provider: Literal["codex", "api"] | None = None
    model: str | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    web_search_calls: int = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)

    total_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _populate_total(cls, value: Any) -> Any:
        return _populate_total_tokens(value)

    @model_validator(mode="after")
    def _total_is_consistent(self) -> UsageRecord:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal input_tokens + output_tokens")
        return self


class BudgetSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    web_search_calls: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    unknown_cost_calls: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0.0, ge=0)
    total_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _populate_total(cls, value: Any) -> Any:
        return _populate_total_tokens(value)

    @model_validator(mode="after")
    def _total_is_consistent(self) -> BudgetSnapshot:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal input_tokens + output_tokens")
        return self


def _populate_total_tokens(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    prepared = dict(value)
    if "total_tokens" not in prepared:
        input_tokens = prepared.get("input_tokens", 0)
        output_tokens = prepared.get("output_tokens", 0)
        if (
            isinstance(input_tokens, int)
            and not isinstance(input_tokens, bool)
            and isinstance(output_tokens, int)
            and not isinstance(output_tokens, bool)
        ):
            prepared["total_tokens"] = input_tokens + output_tokens
    return prepared


BudgetDimension = Literal["calls", "cost_usd", "tokens", "wall_clock"]


class BudgetExceeded(RuntimeError):
    def __init__(
        self,
        dimension: BudgetDimension,
        limit: float | int,
        actual: float | int,
        snapshot: BudgetSnapshot,
    ) -> None:
        self.dimension = dimension
        self.limit = limit
        self.actual = actual
        self.snapshot = snapshot
        super().__init__(f"{dimension} budget exceeded: {actual} > {limit}")


@dataclass(frozen=True, slots=True)
class BudgetRemaining:
    cost_usd: float
    tokens: int | None
    wall_clock_seconds: float | None
    calls: int | None


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    identifier: int
    cost_usd: float
    tokens: int


class BudgetTracker:
    """Thread-safe aggregate suitable for concurrent worker completion callbacks."""

    def __init__(
        self,
        limits: Limits,
        usage: Iterable[UsageRecord] = (),
        *,
        monotonic: Callable[[], float] = time.monotonic,
        prior_elapsed_seconds: float = 0.0,
        maximum_calls: int | None = None,
        enforce_cost_budget: bool = True,
    ) -> None:
        if prior_elapsed_seconds < 0:
            raise ValueError("prior_elapsed_seconds must be nonnegative")
        if maximum_calls is not None and maximum_calls < 1:
            raise ValueError("maximum_calls must be positive when configured")
        self.limits = limits
        self.maximum_calls = maximum_calls
        self.enforce_cost_budget = enforce_cost_budget
        self._monotonic = monotonic
        self._started = float(monotonic())
        self._prior_elapsed_seconds = float(prior_elapsed_seconds)
        self._lock = threading.Lock()
        self._calls = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._cached_input_tokens = 0
        self._cache_write_tokens = 0
        self._reasoning_tokens = 0
        self._web_search_calls = 0
        self._cost_usd = 0.0
        self._unknown_cost_calls = 0
        self._response_ids: set[str] = set()
        self._next_reservation_id = 1
        self._reservations: dict[int, BudgetReservation] = {}
        for record in usage:
            self.record(record, enforce=False)

    def _elapsed(self) -> float:
        return self._prior_elapsed_seconds + max(0.0, float(self._monotonic()) - self._started)

    def _snapshot_locked(self) -> BudgetSnapshot:
        return BudgetSnapshot(
            calls=self._calls,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            cached_input_tokens=self._cached_input_tokens,
            cache_write_tokens=self._cache_write_tokens,
            reasoning_tokens=self._reasoning_tokens,
            web_search_calls=self._web_search_calls,
            cost_usd=self._cost_usd,
            unknown_cost_calls=self._unknown_cost_calls,
            elapsed_seconds=self._elapsed(),
        )

    def snapshot(self) -> BudgetSnapshot:
        with self._lock:
            return self._snapshot_locked()

    def remaining(self) -> BudgetRemaining:
        with self._lock:
            snapshot = self._snapshot_locked()
            reserved_cost = sum(item.cost_usd for item in self._reservations.values())
            reserved_tokens = sum(item.tokens for item in self._reservations.values())
            token_limit = self.limits.maximum_total_tokens
            return BudgetRemaining(
                cost_usd=max(
                    0.0,
                    self.limits.maximum_cost_usd - snapshot.cost_usd - reserved_cost,
                ),
                tokens=(
                    None
                    if token_limit is None
                    else max(0, token_limit - snapshot.total_tokens - reserved_tokens)
                ),
                wall_clock_seconds=(
                    None
                    if self.limits.maximum_wall_clock_hours is None
                    else max(
                        0.0,
                        self.limits.maximum_wall_clock_hours * 3600 - snapshot.elapsed_seconds,
                    )
                ),
                calls=(
                    None
                    if self.maximum_calls is None
                    else max(
                        0,
                        self.maximum_calls - snapshot.calls - len(self._reservations),
                    )
                ),
            )

    def _raise_if_exceeded(
        self,
        snapshot: BudgetSnapshot,
        *,
        additional_cost_usd: float = 0.0,
        additional_tokens: int = 0,
        additional_calls: int = 0,
    ) -> None:
        projected_calls = snapshot.calls + additional_calls
        if self.maximum_calls is not None and projected_calls > self.maximum_calls:
            raise BudgetExceeded("calls", self.maximum_calls, projected_calls, snapshot)
        if self.enforce_cost_budget and snapshot.unknown_cost_calls:
            raise BudgetExceeded(
                "cost_usd",
                self.limits.maximum_cost_usd,
                float("inf"),
                snapshot,
            )
        projected_cost = snapshot.cost_usd + additional_cost_usd
        if self.enforce_cost_budget and projected_cost > self.limits.maximum_cost_usd:
            raise BudgetExceeded("cost_usd", self.limits.maximum_cost_usd, projected_cost, snapshot)
        token_limit = self.limits.maximum_total_tokens
        projected_tokens = snapshot.total_tokens + additional_tokens
        if token_limit is not None and projected_tokens > token_limit:
            raise BudgetExceeded("tokens", token_limit, projected_tokens, snapshot)
        if self.limits.maximum_wall_clock_hours is not None:
            wall_limit = self.limits.maximum_wall_clock_hours * 3600
            if snapshot.elapsed_seconds > wall_limit:
                raise BudgetExceeded("wall_clock", wall_limit, snapshot.elapsed_seconds, snapshot)

    def ensure_available(
        self,
        *,
        estimated_cost_usd: float = 0.0,
        estimated_tokens: int = 0,
        estimated_calls: int = 0,
    ) -> None:
        """Fail before a call when its estimate cannot fit the remaining budget."""

        if estimated_cost_usd < 0 or estimated_tokens < 0 or estimated_calls < 0:
            raise ValueError("budget estimates must be nonnegative")
        with self._lock:
            reserved_cost = sum(item.cost_usd for item in self._reservations.values())
            reserved_tokens = sum(item.tokens for item in self._reservations.values())
            self._raise_if_exceeded(
                self._snapshot_locked(),
                additional_cost_usd=reserved_cost + estimated_cost_usd,
                additional_tokens=reserved_tokens + estimated_tokens,
                additional_calls=len(self._reservations) + estimated_calls,
            )

    def reserve(self, *, estimated_cost_usd: float, estimated_tokens: int) -> BudgetReservation:
        """Atomically reserve capacity before starting a potentially concurrent paid call."""

        if estimated_cost_usd < 0 or estimated_tokens < 0:
            raise ValueError("budget estimates must be nonnegative")
        with self._lock:
            reserved_cost = sum(item.cost_usd for item in self._reservations.values())
            reserved_tokens = sum(item.tokens for item in self._reservations.values())
            snapshot = self._snapshot_locked()
            self._raise_if_exceeded(
                snapshot,
                additional_cost_usd=reserved_cost + estimated_cost_usd,
                additional_tokens=reserved_tokens + estimated_tokens,
                additional_calls=len(self._reservations) + 1,
            )
            reservation = BudgetReservation(
                identifier=self._next_reservation_id,
                cost_usd=estimated_cost_usd,
                tokens=estimated_tokens,
            )
            self._next_reservation_id += 1
            self._reservations[reservation.identifier] = reservation
            return reservation

    def release(self, reservation: BudgetReservation) -> None:
        """Release a reservation after a call fails before producing billable usage."""

        with self._lock:
            existing = self._reservations.pop(reservation.identifier, None)
            if existing != reservation:
                raise ValueError("unknown or already released budget reservation")

    def reconcile(
        self,
        reservation: BudgetReservation,
        usage: UsageRecord,
        *,
        enforce: bool = True,
    ) -> BudgetSnapshot:
        """Replace one reservation with actual usage as one locked operation."""

        with self._lock:
            existing = self._reservations.pop(reservation.identifier, None)
            if existing != reservation:
                raise ValueError("unknown or already reconciled budget reservation")
            snapshot = self._record_locked(usage)
        if enforce:
            self._raise_if_exceeded(snapshot)
        return snapshot

    def _record_locked(self, usage: UsageRecord) -> BudgetSnapshot:
        if usage.response_id is not None:
            if usage.response_id in self._response_ids:
                raise ValueError(f"response usage already recorded: {usage.response_id}")
            self._response_ids.add(usage.response_id)
        self._calls += 1
        self._input_tokens += usage.input_tokens
        self._output_tokens += usage.output_tokens
        self._cached_input_tokens += usage.cached_input_tokens
        self._cache_write_tokens += usage.cache_write_tokens
        self._reasoning_tokens += usage.reasoning_tokens
        self._web_search_calls += usage.web_search_calls
        if usage.cost_usd is None:
            self._unknown_cost_calls += 1
        else:
            self._cost_usd += usage.cost_usd
        return self._snapshot_locked()

    def has_response(self, response_id: str) -> bool:
        """Return whether this tracker already includes a provider response."""

        with self._lock:
            return response_id in self._response_ids

    def record(self, usage: UsageRecord, *, enforce: bool = True) -> BudgetSnapshot:
        """Account actual usage, then raise truthfully if it crossed a hard limit."""

        with self._lock:
            snapshot = self._record_locked(usage)
        if enforce:
            self._raise_if_exceeded(snapshot)
        return snapshot


__all__ = [
    "BudgetDimension",
    "BudgetExceeded",
    "BudgetRemaining",
    "BudgetReservation",
    "BudgetSnapshot",
    "BudgetTracker",
    "UsageRecord",
]
