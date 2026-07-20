from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar, cast

from pydantic import BaseModel

from .config import ModelSettings
from .redaction import redact_data as _shared_redact_data
from .redaction import redact_text as _shared_redact_text
from .structured_schema import strict_schema_sha256

T = TypeVar("T", bound=BaseModel)

_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)(\b(?:authorization|(?:openai[_-]?)?api[_-]?key|"
        r"access[_-]?token|secret)\b"
        r"\s*[:=]\s*)([^\s,;]+)"
    ),
)


@dataclass(frozen=True)
class ModelRequest:
    instructions: str
    input_text: str
    settings: ModelSettings


@dataclass(frozen=True)
class TokenPricing:
    """Optional USD prices per one million tokens, supplied by configuration."""

    input_per_million: float
    output_per_million: float
    cached_input_per_million: float | None = None
    cache_write_per_million: float | None = None
    web_search_per_call: float = 0.0
    long_context_threshold: int | None = None
    long_input_multiplier: float = 1.0
    long_output_multiplier: float = 1.0

    def __post_init__(self) -> None:
        values = (
            self.input_per_million,
            self.output_per_million,
            self.cached_input_per_million,
            self.cache_write_per_million,
            self.web_search_per_call,
        )
        if any(value is not None and value < 0 for value in values):
            raise ValueError("token prices must not be negative")
        if self.long_context_threshold is not None and self.long_context_threshold <= 0:
            raise ValueError("long-context threshold must be positive")
        if self.long_input_multiplier < 1 or self.long_output_multiplier < 1:
            raise ValueError("long-context price multipliers must be at least one")


@dataclass(frozen=True)
class UsageMetadata:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    web_search_calls: int = 0
    estimated_cost_usd: float | None = None

    def to_dict(self) -> dict[str, int | float | None]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "web_search_calls": self.web_search_calls,
            "estimated_cost_usd": self.estimated_cost_usd,
        }


@dataclass(frozen=True)
class RequestEstimate:
    """Conservative pre-call reservation for hard-budget enforcement."""

    input_tokens: int
    output_tokens: int
    web_search_calls: int
    estimated_cost_usd: float

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class ModelResult(Generic[T]):
    parsed: T
    response_id: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    estimated_cost_usd: float | None = None
    status: str = "completed"
    usage: UsageMetadata = field(default_factory=UsageMetadata)
    request_metadata: Mapping[str, Any] = field(default_factory=dict)
    tool_metadata: tuple[Mapping[str, Any], ...] = ()


def _normalized_text(value: str) -> str:
    """Normalize text for stable cache identities without changing its semantics."""

    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def output_schema_name(output_type: type[BaseModel]) -> str:
    """Return a process-independent identity for a structured output model."""

    return f"{output_type.__module__}.{output_type.__qualname__}"


def normalized_model_request(
    request: ModelRequest,
    output_type: type[BaseModel],
    *,
    stage: str,
    cache_namespace: str = "default",
) -> dict[str, Any]:
    """Build the canonical request payload used for crash-replay cache keys.

    This payload is deliberately kept in memory. Persistence code must redact it and
    stores only hashes of the prompt text, never the prompt text itself.
    """

    if not stage.strip():
        raise ValueError("model request stage must not be blank")
    if not cache_namespace.strip():
        raise ValueError("model request cache namespace must not be blank")
    settings = request.settings
    return {
        "schema_version": 2,
        "stage": stage.strip(),
        "cache_namespace": cache_namespace.strip(),
        "output_schema": output_schema_name(output_type),
        "output_schema_sha256": strict_schema_sha256(output_type),
        "instructions": _normalized_text(request.instructions),
        "input_text": _normalized_text(request.input_text),
        "settings": {
            "model": settings.model,
            "reasoning_mode": settings.reasoning_mode,
            "reasoning_effort": settings.reasoning_effort,
            "web_search": settings.web_search,
            "maximum_web_search_calls": settings.maximum_web_search_calls,
            "max_output_tokens": settings.max_output_tokens,
        },
    }


def model_request_cache_key(
    request: ModelRequest,
    output_type: type[BaseModel],
    *,
    stage: str,
    cache_namespace: str = "default",
    redact: Callable[[Any], Any] | None = None,
) -> str:
    """Hash the redacted, normalized request and output-schema identity."""

    redaction_function = redact or redact_value
    safe_payload = redaction_function(
        normalized_model_request(
            request,
            output_type,
            stage=stage,
            cache_namespace=cache_namespace,
        )
    )
    encoded = json.dumps(
        safe_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ModelClient(Protocol):
    async def generate_structured(
        self, request: ModelRequest, output_type: type[T]
    ) -> ModelResult[T]: ...


class ResponsesParser(Protocol):
    async def parse(self, **kwargs: Any) -> Any: ...


class AsyncResponsesClient(Protocol):
    responses: ResponsesParser


class ModelAdapterError(RuntimeError):
    """Base class for safe, redacted adapter failures."""


class ModelTransportError(ModelAdapterError):
    pass


class IncompleteResponseError(ModelAdapterError):
    def __init__(self, response_id: str, details: Mapping[str, Any]) -> None:
        self.response_id = response_id
        self.details = details
        reason = details.get("reason", "unspecified")
        super().__init__(f"response {response_id or '<unknown>'} was incomplete: {reason}")


class ModelRefusalError(ModelAdapterError):
    def __init__(self, response_id: str, refusal: str) -> None:
        self.response_id = response_id
        self.refusal = redact_sensitive(refusal)[:500]
        super().__init__(f"response {response_id or '<unknown>'} was refused: {self.refusal}")


class StructuredOutputError(ModelAdapterError):
    pass


class OpenAIResponsesClient:
    """Narrow, fake-friendly adapter around ``AsyncOpenAI.responses.parse``.

    It intentionally exposes parsed output and non-sensitive audit metadata only.  Raw
    response objects (which may contain provider-internal reasoning items) never leave
    this adapter.
    """

    def __init__(
        self,
        client: AsyncResponsesClient | None = None,
        *,
        max_attempts: int = 3,
        initial_backoff_seconds: float = 1.0,
        maximum_backoff_seconds: float = 8.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        pricing: Mapping[str, TokenPricing] | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if initial_backoff_seconds < 0 or maximum_backoff_seconds < 0:
            raise ValueError("backoff values must not be negative")
        if initial_backoff_seconds > maximum_backoff_seconds:
            raise ValueError("initial backoff must not exceed maximum backoff")
        self._client = client if client is not None else _default_async_client()
        self._max_attempts = max_attempts
        self._initial_backoff_seconds = initial_backoff_seconds
        self._maximum_backoff_seconds = maximum_backoff_seconds
        self._sleep = sleep
        self._pricing = dict(pricing or {})

    def estimate_request(self, request: ModelRequest) -> RequestEstimate:
        """Reserve a conservative standard-API upper bound before dispatch.

        UTF-8 byte length is a conservative approximation for input tokens and the
        configured maximum is used for output. A small fixed allowance covers message
        framing and tool declarations.
        """

        model = str(request.settings.model)
        pricing = self._pricing.get(model)
        if pricing is None:
            raise ModelAdapterError(
                f"cannot reserve hard budget: no standard API pricing for model {model!r}"
            )
        input_tokens = (
            len(request.instructions.encode("utf-8"))
            + len(request.input_text.encode("utf-8"))
            + 512
        )
        output_tokens = request.settings.max_output_tokens
        web_search_calls = (
            request.settings.maximum_web_search_calls if request.settings.web_search else 0
        )
        input_rate = max(
            pricing.input_per_million,
            pricing.cached_input_per_million or 0.0,
            pricing.cache_write_per_million or 0.0,
        )
        is_long = (
            pricing.long_context_threshold is not None
            and input_tokens > pricing.long_context_threshold
        )
        input_multiplier = pricing.long_input_multiplier if is_long else 1.0
        output_multiplier = pricing.long_output_multiplier if is_long else 1.0
        cost = (
            input_tokens * input_rate * input_multiplier
            + output_tokens * pricing.output_per_million * output_multiplier
        ) / 1_000_000 + web_search_calls * pricing.web_search_per_call
        return RequestEstimate(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            web_search_calls=web_search_calls,
            estimated_cost_usd=cost,
        )

    def backend_manifest(self) -> Mapping[str, Any]:
        """Return nonsecret provider provenance for run reports."""

        try:
            version = importlib_metadata.version("openai")
        except importlib_metadata.PackageNotFoundError:  # pragma: no cover - install diagnostic
            version = "unknown"
        return {
            "provider": "api",
            "backend_version": f"openai-python {version}",
            "authentication_class": (
                "platform_api_key" if os.environ.get("OPENAI_API_KEY") else "not_configured"
            ),
            "billing_class": "OpenAI Platform API",
        }

    async def generate_structured(
        self, request: ModelRequest, output_type: type[T]
    ) -> ModelResult[T]:
        kwargs = _request_kwargs(request, output_type)
        response: Any | None = None
        for attempt in range(self._max_attempts):
            try:
                response = await self._client.responses.parse(**kwargs)
                break
            except Exception as exc:
                if not is_transient_error(exc) or attempt + 1 >= self._max_attempts:
                    message = redact_sensitive(str(exc))[:500]
                    raise ModelTransportError(
                        f"Responses API request failed after {attempt + 1} attempt(s): "
                        f"{type(exc).__name__}: {message}"
                    ) from exc
                delay = min(
                    self._maximum_backoff_seconds,
                    self._initial_backoff_seconds * (2**attempt),
                )
                await self._sleep(delay)

        if response is None:  # defensive; the loop either sets response or raises
            raise ModelTransportError("Responses API returned no response")
        return self._parse_response(response, request, output_type)

    def _parse_response(
        self,
        response: Any,
        request: ModelRequest,
        output_type: type[T],
    ) -> ModelResult[T]:
        response_id = str(_field(response, "id", ""))
        status = str(_field(response, "status", "completed") or "completed")
        if status == "incomplete":
            details = _public_details(_field(response, "incomplete_details", None))
            raise IncompleteResponseError(response_id, details)
        if status not in {"completed", "success", "succeeded"}:
            error = _public_details(_field(response, "error", None))
            reason = str(error.get("message") or error.get("code") or status)
            raise ModelAdapterError(
                f"response {response_id or '<unknown>'} ended with status "
                f"{redact_sensitive(reason)[:500]}"
            )

        parsed = _field(response, "output_parsed", None)
        if parsed is None:
            refusal = _extract_refusal(response)
            if refusal is not None:
                raise ModelRefusalError(response_id, refusal)
            raise StructuredOutputError(
                f"response {response_id or '<unknown>'} completed without parsed output"
            )
        payload = parsed.model_dump(mode="python") if isinstance(parsed, BaseModel) else parsed
        try:
            safe_parsed = output_type.model_validate(redact_value(payload))
        except Exception as exc:
            raise StructuredOutputError(
                f"response {response_id or '<unknown>'} parsed output failed safe validation: "
                f"{type(exc).__name__}"
            ) from exc

        tool_metadata = _extract_tool_metadata(response)
        usage = _usage_metadata(
            _field(response, "usage", None),
            self._pricing.get(str(getattr(request.settings, "model", ""))),
            web_search_calls=sum(item.get("type") == "web_search_call" for item in tool_metadata),
        )
        request_metadata: dict[str, Any] = {
            "model": str(getattr(request.settings, "model", "")),
            "reasoning": {
                "mode": str(getattr(request.settings, "reasoning_mode", "")),
                "effort": str(getattr(request.settings, "reasoning_effort", "")),
            },
            "web_search": bool(getattr(request.settings, "web_search", False)),
            "maximum_web_search_calls": int(
                getattr(request.settings, "maximum_web_search_calls", 0) or 0
            ),
            "max_output_tokens": int(getattr(request.settings, "max_output_tokens", 0) or 0),
        }
        return ModelResult(
            parsed=safe_parsed,
            response_id=response_id,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            estimated_cost_usd=usage.estimated_cost_usd,
            status=status,
            usage=usage,
            request_metadata=cast(Mapping[str, Any], redact_value(request_metadata)),
            tool_metadata=tool_metadata,
        )


# Public backend-oriented name; the existing client name remains source-compatible.
OpenAIResponsesBackend = OpenAIResponsesClient


def _default_async_client() -> AsyncResponsesClient:
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:  # pragma: no cover - packaging/install diagnostic
        raise RuntimeError(
            "The 'openai' package is required unless an AsyncResponsesClient fake is injected"
        ) from exc
    return cast(AsyncResponsesClient, AsyncOpenAI())


def _request_kwargs(request: ModelRequest, output_type: type[T]) -> dict[str, Any]:
    settings = request.settings
    kwargs: dict[str, Any] = {
        "model": settings.model,
        "input": [
            {"role": "developer", "content": request.instructions},
            {"role": "user", "content": request.input_text},
        ],
        "text_format": output_type,
        "reasoning": {
            "mode": settings.reasoning_mode,
            "effort": settings.reasoning_effort,
        },
        "max_output_tokens": settings.max_output_tokens,
    }
    if settings.web_search:
        kwargs["tools"] = [{"type": "web_search"}]
        kwargs["max_tool_calls"] = settings.maximum_web_search_calls
        # The Responses API returns cited annotations by default, but the complete
        # source list for each web-search action is opt-in response metadata.
        kwargs["include"] = ["web_search_call.action.sources"]
    return kwargs


def is_transient_error(exc: BaseException) -> bool:
    """Classify retryable SDK/transport failures without importing SDK internals."""

    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        return status_code in {408, 409, 425, 429} or 500 <= status_code <= 599

    return type(exc).__name__ in {
        "APIConnectionError",
        "APITimeoutError",
        "RateLimitError",
        "InternalServerError",
        "ServiceUnavailableError",
    } or isinstance(exc, (ConnectionError, TimeoutError))


is_transient_exception = is_transient_error


def redact_sensitive(text: str) -> str:
    redacted = _shared_redact_text(text)
    home = str(Path.home())
    if home and home != "/":
        redacted = redacted.replace(home, "$HOME")
    redacted = _SECRET_PATTERNS[0].sub("[REDACTED]", redacted)
    redacted = _SECRET_PATTERNS[1].sub("Bearer [REDACTED]", redacted)
    redacted = _SECRET_PATTERNS[2].sub(r"\1[REDACTED]", redacted)
    return redacted


redact_secrets = redact_sensitive


def redact_value(value: Any) -> Any:
    """Recursively redact JSON-like data while avoiding arbitrary object traversal."""

    value = _shared_redact_data(value)
    if isinstance(value, str):
        return redact_sensitive(value)
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]"
            if re.search(
                r"(?i)(authorization|api.?key|access.?token|secret|password|cookie)",
                str(key),
            )
            else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_sensitive(str(value))


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _integer_field(value: Any, name: str) -> int | None:
    candidate = _field(value, name, None)
    if isinstance(candidate, bool) or not isinstance(candidate, int):
        return None
    return candidate if candidate >= 0 else None


def _usage_metadata(
    value: Any,
    pricing: TokenPricing | None,
    *,
    web_search_calls: int = 0,
) -> UsageMetadata:
    input_tokens = _integer_field(value, "input_tokens")
    output_tokens = _integer_field(value, "output_tokens")
    total_tokens = _integer_field(value, "total_tokens")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens

    input_details = _field(value, "input_tokens_details", None)
    output_details = _field(value, "output_tokens_details", None)
    cached_tokens = _integer_field(input_details, "cached_tokens")
    cache_write_tokens = _integer_field(input_details, "cache_write_tokens")
    reasoning_tokens = _integer_field(output_details, "reasoning_tokens")
    cost = _estimate_cost(
        input_tokens,
        output_tokens,
        cached_tokens,
        cache_write_tokens,
        web_search_calls,
        pricing,
    )
    return UsageMetadata(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_input_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
        web_search_calls=web_search_calls,
        estimated_cost_usd=cost,
    )


def _estimate_cost(
    input_tokens: int | None,
    output_tokens: int | None,
    cached_tokens: int | None,
    cache_write_tokens: int | None,
    web_search_calls: int,
    pricing: TokenPricing | None,
) -> float | None:
    if pricing is None or input_tokens is None or output_tokens is None:
        return None
    cached = min(cached_tokens or 0, input_tokens)
    cache_write = min(cache_write_tokens or 0, input_tokens - cached)
    uncached = input_tokens - cached - cache_write
    cached_rate = (
        pricing.cached_input_per_million
        if pricing.cached_input_per_million is not None
        else pricing.input_per_million
    )
    cache_write_rate = (
        pricing.cache_write_per_million
        if pricing.cache_write_per_million is not None
        else pricing.input_per_million
    )
    is_long = (
        pricing.long_context_threshold is not None and input_tokens > pricing.long_context_threshold
    )
    input_multiplier = pricing.long_input_multiplier if is_long else 1.0
    output_multiplier = pricing.long_output_multiplier if is_long else 1.0
    return (
        (
            uncached * pricing.input_per_million
            + cached * cached_rate
            + cache_write * cache_write_rate
        )
        * input_multiplier
        + output_tokens * pricing.output_per_million * output_multiplier
    ) / 1_000_000 + web_search_calls * pricing.web_search_per_call


def _public_details(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        source = value
    else:
        source = {
            name: getattr(value, name)
            for name in ("reason", "code", "message", "type")
            if hasattr(value, name)
        }
    return cast(dict[str, Any], redact_value(dict(source)))


def _extract_refusal(response: Any) -> str | None:
    outputs = _field(response, "output", ())
    if not isinstance(outputs, Sequence) or isinstance(outputs, (str, bytes)):
        return None
    for output in outputs:
        contents = _field(output, "content", ())
        if not isinstance(contents, Sequence) or isinstance(contents, (str, bytes)):
            continue
        for content in contents:
            refusal = _field(content, "refusal", None)
            content_type = _field(content, "type", None)
            if refusal is not None or content_type == "refusal":
                return str(refusal or _field(content, "text", "refused"))
    return None


def _extract_tool_metadata(response: Any) -> tuple[Mapping[str, Any], ...]:
    outputs = _field(response, "output", ())
    if not isinstance(outputs, Sequence) or isinstance(outputs, (str, bytes)):
        return ()
    metadata: list[Mapping[str, Any]] = []
    for item in outputs:
        item_type = str(_field(item, "type", ""))
        if item_type in {"web_search_call", "file_search_call", "computer_call"}:
            public: dict[str, Any] = {
                "type": item_type,
                "id": str(_field(item, "id", "")),
                "status": str(_field(item, "status", "")),
            }
            if item_type == "web_search_call":
                action = _field(item, "action", None)
                public_action = _public_web_action(action)
                if public_action:
                    public["action"] = public_action
            metadata.append(cast(Mapping[str, Any], redact_value(public)))
            continue

        # URL citations live on message output-text annotations. Deliberately inspect
        # no other message or reasoning content: provider reasoning must never enter
        # logs or replay artifacts.
        if item_type != "message":
            continue
        contents = _field(item, "content", ())
        if not isinstance(contents, Sequence) or isinstance(contents, (str, bytes)):
            continue
        for content in contents:
            annotations = _field(content, "annotations", ())
            if not isinstance(annotations, Sequence) or isinstance(annotations, (str, bytes)):
                continue
            for annotation in annotations:
                citation = _public_url_citation(annotation)
                if citation is not None:
                    metadata.append(cast(Mapping[str, Any], redact_value(citation)))
    return tuple(metadata)


def _public_web_source(value: Any) -> dict[str, Any] | None:
    url = _field(value, "url", None)
    if not isinstance(url, str) or not url:
        return None
    source: dict[str, Any] = {
        "type": str(_field(value, "type", "url")),
        "url": url,
    }
    title = _field(value, "title", None)
    if isinstance(title, str) and title:
        source["title"] = title
    return source


def _public_web_action(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    action: dict[str, Any] = {}
    action_type = _field(value, "type", None)
    if isinstance(action_type, str) and action_type:
        action["type"] = action_type
    for name in ("query", "url", "pattern"):
        candidate = _field(value, name, None)
        if isinstance(candidate, str) and candidate:
            action[name] = candidate
    sources = _field(value, "sources", ())
    if isinstance(sources, Sequence) and not isinstance(sources, (str, bytes)):
        public_sources = [
            source
            for source in (_public_web_source(candidate) for candidate in sources)
            if source is not None
        ]
        if public_sources:
            action["sources"] = public_sources
    return action


def _public_url_citation(value: Any) -> dict[str, Any] | None:
    if str(_field(value, "type", "")) != "url_citation":
        return None
    url = _field(value, "url", None)
    if not isinstance(url, str) or not url:
        return None
    citation: dict[str, Any] = {"type": "url_citation", "url": url}
    title = _field(value, "title", None)
    if isinstance(title, str) and title:
        citation["title"] = title
    for name in ("start_index", "end_index"):
        index = _integer_field(value, name)
        if index is not None:
            citation[name] = index
    return citation


__all__ = [
    "AsyncResponsesClient",
    "IncompleteResponseError",
    "ModelAdapterError",
    "ModelClient",
    "ModelRefusalError",
    "ModelRequest",
    "ModelResult",
    "ModelTransportError",
    "OpenAIResponsesBackend",
    "OpenAIResponsesClient",
    "RequestEstimate",
    "ResponsesParser",
    "StructuredOutputError",
    "TokenPricing",
    "UsageMetadata",
    "is_transient_error",
    "is_transient_exception",
    "model_request_cache_key",
    "normalized_model_request",
    "output_schema_name",
    "redact_secrets",
    "redact_sensitive",
    "redact_value",
]
