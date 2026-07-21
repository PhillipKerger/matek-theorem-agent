"""Budget and usage-logging decorator for injected model clients."""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Mapping
from typing import Any, Literal, TypeVar, cast

from pydantic import BaseModel, ValidationError

from .budget import BudgetExceeded, BudgetReservation, BudgetSnapshot, BudgetTracker, UsageRecord
from .logging import ModelCallJournalError, ModelCallRecord, RunLogger
from .openai_client import (
    ModelClient,
    ModelRequest,
    ModelResult,
    RequestEstimate,
    UsageMetadata,
    normalized_model_request,
)

T = TypeVar("T", bound=BaseModel)


class AccountingModelClient:
    """Decorate any model client without coupling workflow stages to accounting."""

    def __init__(
        self,
        delegate: ModelClient,
        *,
        stage: str,
        budget: BudgetTracker,
        logger: RunLogger,
        replay_completed: bool = True,
        cache_namespace: str | None = None,
        provider: Literal["codex", "api"] | None = None,
        role: str | None = None,
    ) -> None:
        self._source_delegate = delegate
        stage_factory = getattr(delegate, "for_stage", None)
        self._delegate = (
            cast(
                ModelClient,
                stage_factory(stage, run_root=logger.run_root, role=role),
            )
            if callable(stage_factory)
            else delegate
        )
        self._stage = stage
        self._provider = provider
        self._budget = budget
        self._logger = logger
        self._replay_completed = replay_completed
        self._role = role
        resolved_namespace = cache_namespace or logger.model_cache_namespace
        if not resolved_namespace.strip():
            raise ValueError("model cache namespace must not be blank")
        self._cache_namespace = resolved_namespace.strip()
        self._request_locks: dict[str, asyncio.Lock] = {}

    def for_role(self, role: str) -> AccountingModelClient:
        """Create a role-isolated model context sharing accounting and call journals."""

        if not role.strip():
            raise ValueError("model role must not be blank")
        return type(self)(
            self._source_delegate,
            stage=self._stage,
            budget=self._budget,
            logger=self._logger,
            replay_completed=self._replay_completed,
            cache_namespace=self._cache_namespace,
            provider=self._provider,
            role=role.strip(),
        )

    def request_cache_key(
        self,
        request: ModelRequest,
        output_type: type[BaseModel],
    ) -> str:
        """Return the exact durable replay identity used by this decorator."""

        return self._logger.model_calls.identity(
            normalized_model_request(
                request,
                output_type,
                stage=self._stage,
                cache_namespace=self._cache_namespace,
            )
        ).request_key

    def accounted_request_keys(self, request_keys: Collection[str]) -> dict[str, str]:
        """Return current-scope checkpoints already present in run-wide accounting."""

        recovered: dict[str, str] = {}
        for request_key in request_keys:
            record = self._logger.model_calls.load_by_request_key(
                request_key,
                expected_stage=self._stage,
                expected_cache_namespace=self._cache_namespace,
            )
            if record is not None and self._budget.has_response(record.response_id):
                recovered[request_key] = record.response_id
        return recovered

    def is_accounted_request(
        self,
        request: ModelRequest,
        output_type: type[BaseModel],
    ) -> bool:
        """Whether this exact replay is paid already and consumes no remaining call slot."""

        identity = self._logger.model_calls.identity(
            normalized_model_request(
                request,
                output_type,
                stage=self._stage,
                cache_namespace=self._cache_namespace,
            )
        )
        record = self._logger.model_calls.load(identity)
        return record is not None and self._budget.has_response(record.response_id)

    def remaining_model_calls(self) -> int | None:
        """Return the current run-wide call remainder, including live reservations."""

        return self._budget.remaining().calls

    async def generate_structured(
        self,
        request: ModelRequest,
        output_type: type[T],
    ) -> ModelResult[T]:
        identity = self._logger.model_calls.identity(
            normalized_model_request(
                request,
                output_type,
                stage=self._stage,
                cache_namespace=self._cache_namespace,
            )
        )
        # Single-flight identical calls in one workflow process. Different research
        # requests remain concurrent and are protected by atomic budget reservations.
        request_lock = self._request_locks.setdefault(identity.request_key, asyncio.Lock())
        async with request_lock:
            cached = self._logger.model_calls.load(identity)
            if cached is not None:
                if self._replay_completed:
                    self._account_if_missing(cached.usage)
                    return self._result_from_record(cached, output_type)
                raise ModelCallJournalError(
                    "replay is disabled but this cache namespace already has a completed "
                    "response; use a fresh cache_namespace before purchasing another call"
                )

            reservation = self._reserve(request)
            try:
                result = await self._generate_with_time_limit(request, output_type)
            except BaseException:
                if reservation is not None:
                    self._budget.release(reservation)
                raise

            usage = self._usage_record(result, request)
            try:
                record = self._logger.model_calls.persist(
                    identity,
                    stage=self._stage,
                    response_id=result.response_id,
                    status=result.status,
                    usage=usage,
                    tool_metadata=[dict(item) for item in result.tool_metadata],
                    parsed=result.parsed,
                )
            except BaseException:
                # The response is already billable. Account it even if durable result
                # checkpointing fails, then surface the persistence failure.
                self._account_if_missing(usage, reservation=reservation)
                raise

            self._account_if_missing(record.usage, reservation=reservation)
            return self._result_from_record(record, output_type)

    async def _generate_with_time_limit(
        self,
        request: ModelRequest,
        output_type: type[T],
    ) -> ModelResult[T]:
        """Bound an in-flight model call by the run-wide remaining wall clock."""

        remaining = self._budget.remaining().wall_clock_seconds
        if remaining is None:
            return await self._delegate.generate_structured(request, output_type)
        if remaining <= 0:
            snapshot = self._budget.snapshot()
            configured_hours = self._budget.limits.maximum_wall_clock_hours
            assert configured_hours is not None
            limit = configured_hours * 3600
            raise BudgetExceeded("wall_clock", limit, snapshot.elapsed_seconds, snapshot)
        task = asyncio.create_task(self._delegate.generate_structured(request, output_type))
        try:
            done, _ = await asyncio.wait({task}, timeout=remaining)
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        if task not in done:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            snapshot = self._budget.snapshot()
            configured_hours = self._budget.limits.maximum_wall_clock_hours
            assert configured_hours is not None
            limit = configured_hours * 3600
            raise BudgetExceeded("wall_clock", limit, snapshot.elapsed_seconds, snapshot)
        return task.result()

    def _reserve(self, request: ModelRequest) -> BudgetReservation | None:
        estimator = getattr(self._delegate, "estimate_request", None)
        if not callable(estimator):
            # Subscription-backed and deterministic clients may not have dollar-price
            # knowledge. Reserve a zero-cost call slot anyway: a read-only availability
            # check would let concurrent Codex calls race past ``max_agent_calls`` before
            # either one had recorded its result.
            return self._budget.reserve(estimated_cost_usd=0.0, estimated_tokens=0)
        estimate = estimator(request)
        if not isinstance(estimate, RequestEstimate):
            raise TypeError("model client's estimate_request returned an invalid estimate")
        return self._budget.reserve(
            estimated_cost_usd=estimate.estimated_cost_usd,
            estimated_tokens=estimate.total_tokens,
        )

    def _usage_record(self, result: ModelResult[Any], request: ModelRequest) -> UsageRecord:
        return UsageRecord(
            response_id=result.response_id or None,
            stage=self._stage,
            provider=self._provider,
            model=request.settings.model,
            input_tokens=(
                result.input_tokens
                if result.input_tokens is not None
                else result.usage.input_tokens or 0
            ),
            output_tokens=(
                result.output_tokens
                if result.output_tokens is not None
                else result.usage.output_tokens or 0
            ),
            cached_input_tokens=result.usage.cached_input_tokens or 0,
            cache_write_tokens=result.usage.cache_write_tokens or 0,
            reasoning_tokens=result.usage.reasoning_tokens or 0,
            web_search_calls=result.usage.web_search_calls,
            cost_usd=(
                result.estimated_cost_usd
                if result.estimated_cost_usd is not None
                else result.usage.estimated_cost_usd
            ),
        )

    def _account_if_missing(
        self,
        usage: UsageRecord,
        *,
        reservation: BudgetReservation | None = None,
    ) -> None:
        response_id = usage.response_id
        if response_id is None:  # ModelCallStore rejects this for persisted results.
            raise ModelCallJournalError("paid response usage has no response ID")
        try:
            self._logger.usage_once(usage, stage=usage.stage or self._stage)
        except BaseException:
            # Preserve truthful in-memory accounting for this paid call even when a
            # corrupt journal correctly blocks continued execution.
            if not self._budget.has_response(response_id):
                if reservation is None:
                    self._budget.record(usage, enforce=False)
                else:
                    self._budget.reconcile(reservation, usage, enforce=False)
            elif reservation is not None:
                self._budget.release(reservation)
            raise
        if self._budget.has_response(response_id):
            if reservation is not None:
                self._budget.release(reservation)
            return
        if reservation is None:
            self._budget.record(usage, enforce=False)
        else:
            self._budget.reconcile(reservation, usage, enforce=False)

    @staticmethod
    def _result_from_record(
        record: ModelCallRecord,
        output_type: type[T],
    ) -> ModelResult[T]:
        try:
            parsed = output_type.model_validate(record.parsed)
        except ValidationError as exc:
            raise ModelCallJournalError(
                f"cached parsed result does not validate as {record.output_schema}"
            ) from exc
        usage = UsageMetadata(
            input_tokens=record.usage.input_tokens,
            output_tokens=record.usage.output_tokens,
            total_tokens=record.usage.total_tokens,
            cached_input_tokens=record.usage.cached_input_tokens,
            cache_write_tokens=record.usage.cache_write_tokens,
            reasoning_tokens=record.usage.reasoning_tokens,
            web_search_calls=record.usage.web_search_calls,
            estimated_cost_usd=record.usage.cost_usd,
        )
        request_metadata: Mapping[str, Any] = {
            "backend": record.usage.provider,
            "model": record.request.model,
            "reasoning": {
                "mode": record.request.reasoning_mode,
                "effort": record.request.reasoning_effort,
            },
            "web_search": record.request.web_search,
            "maximum_web_search_calls": record.request.maximum_web_search_calls,
            "max_output_tokens": record.request.max_output_tokens,
        }
        return ModelResult(
            parsed=parsed,
            response_id=record.response_id,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            estimated_cost_usd=usage.estimated_cost_usd,
            status=record.status,
            usage=usage,
            request_metadata=request_metadata,
            tool_metadata=tuple(cast(Mapping[str, Any], item) for item in record.tool_metadata),
        )

    def snapshot(self) -> BudgetSnapshot:
        return self._budget.snapshot()
