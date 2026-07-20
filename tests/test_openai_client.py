from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from ascend_math_agent.config import ModelSettings
from ascend_math_agent.openai_client import (
    IncompleteResponseError,
    ModelAdapterError,
    ModelRefusalError,
    ModelRequest,
    ModelTransportError,
    OpenAIResponsesClient,
    TokenPricing,
    model_request_cache_key,
)


class Answer(BaseModel):
    value: int


class TextAnswer(BaseModel):
    value: str


class FakeResponses:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeSDK:
    def __init__(self, outcomes: list[Any]) -> None:
        self.responses = FakeResponses(outcomes)


def completed_response() -> SimpleNamespace:
    return SimpleNamespace(
        id="resp_123",
        status="completed",
        output_parsed=Answer(value=7),
        output=[SimpleNamespace(type="web_search_call", id="ws_1", status="completed")],
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            total_tokens=120,
            input_tokens_details=SimpleNamespace(cached_tokens=40),
            output_tokens_details=SimpleNamespace(reasoning_tokens=8),
        ),
    )


@pytest.mark.asyncio
async def test_responses_parse_contract_usage_and_cost() -> None:
    sdk = FakeSDK([completed_response()])
    client = OpenAIResponsesClient(
        sdk,
        pricing={"test-model": TokenPricing(2.0, 10.0, cached_input_per_million=1.0)},
    )
    settings = ModelSettings(model="test-model", max_output_tokens=456)

    result = await client.generate_structured(
        ModelRequest("developer text", "user text", settings), Answer
    )

    assert result.parsed == Answer(value=7)
    assert result.response_id == "resp_123"
    assert result.usage.cached_input_tokens == 40
    assert result.usage.reasoning_tokens == 8
    assert result.estimated_cost_usd == pytest.approx((60 * 2 + 40 * 1 + 20 * 10) / 1e6)
    assert result.tool_metadata == (
        {"type": "web_search_call", "id": "ws_1", "status": "completed"},
    )
    assert sdk.responses.calls == [
        {
            "model": "test-model",
            "input": [
                {"role": "developer", "content": "developer text"},
                {"role": "user", "content": "user text"},
            ],
            "text_format": Answer,
            "reasoning": {"mode": "pro", "effort": "xhigh"},
            "max_output_tokens": 456,
            "tools": [{"type": "web_search"}],
            "max_tool_calls": 8,
            "include": ["web_search_call.action.sources"],
        }
    ]


@pytest.mark.asyncio
async def test_cost_includes_cache_writes_search_and_long_context_multiplier() -> None:
    response = completed_response()
    response.usage = SimpleNamespace(
        input_tokens=300_000,
        output_tokens=100,
        total_tokens=300_100,
        input_tokens_details=SimpleNamespace(
            cached_tokens=50_000,
            cache_write_tokens=20_000,
        ),
        output_tokens_details=SimpleNamespace(reasoning_tokens=40),
    )
    pricing = TokenPricing(
        input_per_million=5.0,
        output_per_million=30.0,
        cached_input_per_million=0.5,
        cache_write_per_million=6.25,
        web_search_per_call=0.01,
        long_context_threshold=272_000,
        long_input_multiplier=2.0,
        long_output_multiplier=1.5,
    )
    client = OpenAIResponsesClient(FakeSDK([response]), pricing={"test-model": pricing})

    result = await client.generate_structured(
        ModelRequest("d", "u", ModelSettings(model="test-model")), Answer
    )

    token_cost = (
        (230_000 * 5.0 + 50_000 * 0.5 + 20_000 * 6.25) * 2.0 + 100 * 30.0 * 1.5
    ) / 1_000_000
    assert result.estimated_cost_usd == pytest.approx(token_cost + 0.01)
    assert result.usage.cache_write_tokens == 20_000
    assert result.usage.web_search_calls == 1


def test_request_cache_key_is_normalized_redacted_and_schema_sensitive() -> None:
    settings = ModelSettings(web_search=False)
    first = ModelRequest(
        "line one\r\nkey=sk-proj-first-secret-token",
        "problem",
        settings,
    )
    second = ModelRequest(
        "line one\nkey=sk-proj-second-secret-token",
        "problem",
        settings,
    )

    first_key = model_request_cache_key(first, Answer, stage="research")
    assert first_key == model_request_cache_key(second, Answer, stage="research")
    assert first_key != model_request_cache_key(first, TextAnswer, stage="research")
    assert first_key != model_request_cache_key(first, Answer, stage="research_audit")
    assert first_key != model_request_cache_key(
        first,
        Answer,
        stage="research",
        cache_namespace="force-generation-1",
    )


def test_request_cache_key_includes_schema_body_for_same_qualified_name() -> None:
    class SchemaVersionOne(BaseModel):
        value: int

    class SchemaVersionTwo(BaseModel):
        value: int
        explanation: str

    for output_type in (SchemaVersionOne, SchemaVersionTwo):
        output_type.__module__ = "fixture.outputs"
        output_type.__qualname__ = "StableOutput"

    request = ModelRequest("instructions", "input", ModelSettings(web_search=False))

    assert model_request_cache_key(
        request, SchemaVersionOne, stage="research"
    ) != model_request_cache_key(request, SchemaVersionTwo, stage="research")


def test_request_estimate_reserves_max_output_and_fails_without_pricing() -> None:
    request = ModelRequest(
        "d",
        "u",
        ModelSettings(model="test-model", max_output_tokens=100, web_search=True),
    )
    client = OpenAIResponsesClient(
        FakeSDK([]),
        pricing={
            "test-model": TokenPricing(
                2.0,
                10.0,
                cached_input_per_million=1.0,
                web_search_per_call=0.01,
            )
        },
    )

    estimate = client.estimate_request(request)

    assert estimate.input_tokens == 514
    assert estimate.output_tokens == 100
    assert estimate.total_tokens == 614
    assert estimate.web_search_calls == 8
    assert estimate.estimated_cost_usd == pytest.approx((514 * 2 + 100 * 10) / 1e6 + 0.08)
    with pytest.raises(ModelAdapterError, match="no standard API pricing"):
        OpenAIResponsesClient(FakeSDK([])).estimate_request(request)


@pytest.mark.asyncio
async def test_only_public_web_sources_and_citations_are_extracted() -> None:
    response = completed_response()
    response.output = [
        SimpleNamespace(
            type="web_search_call",
            id="ws_1",
            status="completed",
            action=SimpleNamespace(
                type="search",
                query="public theorem source",
                sources=[
                    SimpleNamespace(type="url", url="https://example.test/source", title="Source")
                ],
            ),
        ),
        SimpleNamespace(
            type="message",
            content=[
                SimpleNamespace(
                    text="not persisted as metadata",
                    annotations=[
                        SimpleNamespace(
                            type="url_citation",
                            url="https://example.test/citation",
                            title="Citation",
                            start_index=0,
                            end_index=8,
                        )
                    ],
                )
            ],
        ),
        SimpleNamespace(type="reasoning", summary="private chain-of-thought must be ignored"),
    ]
    client = OpenAIResponsesClient(FakeSDK([response]))

    result = await client.generate_structured(ModelRequest("d", "u", ModelSettings()), Answer)

    assert result.tool_metadata == (
        {
            "type": "web_search_call",
            "id": "ws_1",
            "status": "completed",
            "action": {
                "type": "search",
                "query": "public theorem source",
                "sources": [
                    {"type": "url", "url": "https://example.test/source", "title": "Source"}
                ],
            },
        },
        {
            "type": "url_citation",
            "url": "https://example.test/citation",
            "title": "Citation",
            "start_index": 0,
            "end_index": 8,
        },
    )
    assert "chain-of-thought" not in repr(result.tool_metadata)


@pytest.mark.asyncio
async def test_bounded_exponential_retry_is_injectable() -> None:
    class RateLimit(Exception):
        status_code = 429

    sdk = FakeSDK([RateLimit("retry"), RateLimit("retry"), completed_response()])
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    client = OpenAIResponsesClient(
        sdk,
        max_attempts=3,
        initial_backoff_seconds=0.25,
        maximum_backoff_seconds=0.5,
        sleep=fake_sleep,
    )
    await client.generate_structured(
        ModelRequest("d", "u", ModelSettings(web_search=False)), Answer
    )

    assert delays == [0.25, 0.5]
    assert "tools" not in sdk.responses.calls[-1]
    assert "include" not in sdk.responses.calls[-1]


@pytest.mark.asyncio
async def test_nontransient_error_is_not_retried_and_is_redacted() -> None:
    class BadRequest(Exception):
        status_code = 400

    sdk = FakeSDK([BadRequest("Authorization: Bearer sk-secretsecret")])
    client = OpenAIResponsesClient(sdk)
    with pytest.raises(ModelTransportError) as caught:
        await client.generate_structured(ModelRequest("d", "u", ModelSettings()), Answer)
    assert len(sdk.responses.calls) == 1
    assert "sk-secretsecret" not in str(caught.value)


@pytest.mark.asyncio
async def test_parsed_model_output_is_redacted_before_reaching_artifact_stages() -> None:
    response = completed_response()
    response.output_parsed = TextAnswer(value="Authorization: Bearer sk-secretsecret")
    client = OpenAIResponsesClient(FakeSDK([response]))

    result = await client.generate_structured(ModelRequest("d", "u", ModelSettings()), TextAnswer)

    assert "sk-secretsecret" not in result.parsed.value
    assert "[REDACTED]" in result.parsed.value


@pytest.mark.asyncio
async def test_incomplete_and_refusal_are_truthful_errors() -> None:
    incomplete = SimpleNamespace(
        id="resp_incomplete",
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
    )
    client = OpenAIResponsesClient(FakeSDK([incomplete]))
    with pytest.raises(IncompleteResponseError) as caught:
        await client.generate_structured(ModelRequest("d", "u", ModelSettings()), Answer)
    assert caught.value.details == {"reason": "max_output_tokens"}

    refusal = SimpleNamespace(
        id="resp_refusal",
        status="completed",
        output_parsed=None,
        output=[
            SimpleNamespace(content=[SimpleNamespace(type="refusal", refusal="cannot comply")])
        ],
    )
    client = OpenAIResponsesClient(FakeSDK([refusal]))
    with pytest.raises(ModelRefusalError):
        await client.generate_structured(ModelRequest("d", "u", ModelSettings()), Answer)
