"""Backend-independent verification of public source identifiers."""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from .source_identifiers import source_identifiers
from .workspace import atomic_write_json


class SourceVerificationStatus(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    UNAVAILABLE = "unavailable"


class SourceEvidenceClaim(BaseModel):
    """A prose claim linked explicitly to stable source IDs."""

    model_config = ConfigDict(extra="forbid")

    claim: str
    source_ids: list[str]


class SourceVerificationRecord(BaseModel):
    """Deterministic verification result for one canonical identifier."""

    model_config = ConfigDict(extra="forbid")

    identifier: str
    status: SourceVerificationStatus
    canonical_url: str | None = None
    resolved_title: str | None = None
    detail: str


class SourceVerificationReport(BaseModel):
    """Combined provider and deterministic provenance for one ledger."""

    model_config = ConfigDict(extra="forbid")

    records: list[SourceVerificationRecord] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def verified_identifiers(self) -> frozenset[str]:
        return frozenset(
            record.identifier
            for record in self.records
            if record.status is SourceVerificationStatus.VERIFIED
        )


class IdentifierVerifier(Protocol):
    async def verify(
        self,
        identifiers: Collection[str],
        *,
        expected_title: str | None = None,
    ) -> SourceVerificationReport: ...


class WebDisabledSourceVerifier:
    """Return explicit unavailable evidence without performing network I/O."""

    async def verify(
        self,
        identifiers: Collection[str],
        *,
        expected_title: str | None = None,
    ) -> SourceVerificationReport:
        del expected_title
        records = [
            SourceVerificationRecord(
                identifier=identifier,
                status=SourceVerificationStatus.UNAVAILABLE,
                detail="web search is disabled by configuration",
            )
            for identifier in sorted(set(identifiers))
        ]
        return SourceVerificationReport(
            records=records,
            warnings=[
                f"Verification unavailable for {record.identifier}: {record.detail}"
                for record in records
            ],
        )


@dataclass(frozen=True)
class HttpResponse:
    status: int
    url: str
    headers: Mapping[str, str]
    body: bytes


HttpFetcher = Callable[[str, float, int], Awaitable[HttpResponse]]


async def _default_fetcher(url: str, timeout_seconds: float, maximum_bytes: int) -> HttpResponse:
    def fetch() -> HttpResponse:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json, application/atom+xml, text/html;q=0.8",
                "User-Agent": "ASCEND/0.2 source verifier",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(maximum_bytes + 1)
            if len(body) > maximum_bytes:
                raise ValueError("source response exceeded the configured size bound")
            return HttpResponse(
                status=int(response.status),
                url=response.geturl(),
                headers=dict(response.headers.items()),
                body=body,
            )

    return await asyncio.to_thread(fetch)


class BoundedHttpSourceVerifier:
    """Resolve public identifiers with bounded requests and a persistent success cache."""

    def __init__(
        self,
        *,
        cache_path: Path | None = None,
        fetcher: HttpFetcher = _default_fetcher,
        timeout_seconds: float = 8.0,
        maximum_bytes: int = 512 * 1024,
        maximum_attempts: int = 2,
    ) -> None:
        if timeout_seconds <= 0 or maximum_bytes <= 0 or maximum_attempts not in {1, 2, 3}:
            raise ValueError("invalid bounded source-verifier limits")
        self._cache_path = cache_path
        self._fetcher = fetcher
        self._timeout_seconds = timeout_seconds
        self._maximum_bytes = maximum_bytes
        self._maximum_attempts = maximum_attempts
        self._cache = self._load_cache(cache_path)

    async def verify(
        self,
        identifiers: Collection[str],
        *,
        expected_title: str | None = None,
    ) -> SourceVerificationReport:
        records: list[SourceVerificationRecord] = []
        warnings: list[str] = []
        for identifier in sorted(set(identifiers)):
            cached = self._cache.get(identifier)
            if cached is not None:
                records.append(cached)
                continue
            record = await self._verify_one(identifier, expected_title=expected_title)
            records.append(record)
            if record.status is SourceVerificationStatus.VERIFIED:
                self._cache[identifier] = record
                self._write_cache()
            elif record.status is SourceVerificationStatus.UNAVAILABLE:
                warnings.append(f"Verification unavailable for {identifier}: {record.detail}")
        return SourceVerificationReport(records=records, warnings=warnings)

    async def _verify_one(
        self,
        identifier: str,
        *,
        expected_title: str | None,
    ) -> SourceVerificationRecord:
        urls = _resolver_urls(identifier)
        if not urls:
            return SourceVerificationRecord(
                identifier=identifier,
                status=SourceVerificationStatus.UNVERIFIED,
                detail="identifier type has no deterministic resolver",
            )
        unavailable_details: list[str] = []
        for url in urls:
            for attempt in range(self._maximum_attempts):
                try:
                    response = await self._fetcher(url, self._timeout_seconds, self._maximum_bytes)
                except (OSError, TimeoutError, ValueError, urllib.error.URLError) as exc:
                    unavailable_details.append(type(exc).__name__)
                    if attempt + 1 < self._maximum_attempts:
                        continue
                    break
                if response.status < 200 or response.status >= 400:
                    unavailable_details.append(f"HTTP {response.status}")
                    break
                resolved_title = _response_title(response)
                if (
                    expected_title
                    and resolved_title
                    and not _titles_compatible(expected_title, resolved_title)
                ):
                    return SourceVerificationRecord(
                        identifier=identifier,
                        status=SourceVerificationStatus.UNVERIFIED,
                        canonical_url=response.url,
                        resolved_title=resolved_title,
                        detail="resolved metadata title does not match the ledger title",
                    )
                return SourceVerificationRecord(
                    identifier=identifier,
                    status=SourceVerificationStatus.VERIFIED,
                    canonical_url=response.url,
                    resolved_title=resolved_title,
                    detail="identifier resolved successfully",
                )
        return SourceVerificationRecord(
            identifier=identifier,
            status=SourceVerificationStatus.UNAVAILABLE,
            detail=", ".join(unavailable_details[:4]) or "all resolvers unavailable",
        )

    @staticmethod
    def _load_cache(cache_path: Path | None) -> dict[str, SourceVerificationRecord]:
        if cache_path is None or not cache_path.is_file():
            return {}
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
            records = [
                SourceVerificationRecord.model_validate(item) for item in raw.get("records", [])
            ]
        except (OSError, ValueError, TypeError):
            return {}
        return {
            record.identifier: record
            for record in records
            if record.status is SourceVerificationStatus.VERIFIED
        }

    def _write_cache(self) -> None:
        if self._cache_path is None:
            return
        atomic_write_json(
            self._cache_path,
            {
                "schema_version": 1,
                "records": [record.model_dump(mode="json") for record in self._cache.values()],
            },
        )


def canonical_identifiers(values: Sequence[str | None]) -> frozenset[str]:
    identifiers: set[str] = set()
    for value in values:
        if value:
            identifiers.update(source_identifiers(value))
    return frozenset(identifiers)


def provider_verification_records(
    identifiers: Collection[str],
    provider_identifiers: Collection[str],
) -> list[SourceVerificationRecord]:
    provider_set = set(provider_identifiers)
    return [
        SourceVerificationRecord(
            identifier=identifier,
            status=SourceVerificationStatus.VERIFIED,
            detail="identifier was returned by provider citation metadata",
        )
        for identifier in sorted(set(identifiers).intersection(provider_set))
    ]


def _resolver_urls(identifier: str) -> tuple[str, ...]:
    kind, separator, value = identifier.partition(":")
    if not separator or not value:
        return ()
    if kind == "doi":
        return (
            f"https://api.crossref.org/works/{quote(value, safe='')}",
            f"https://doi.org/{value}",
        )
    if kind == "arxiv":
        return (f"https://export.arxiv.org/api/query?id_list={quote(value, safe='/.')}",)
    if kind == "isbn":
        return (f"https://openlibrary.org/isbn/{quote(value, safe='')}.json",)
    if kind == "mr":
        return (f"https://mathscinet.ams.org/mathscinet-getitem?mr={quote(value, safe='')}",)
    if kind == "url":
        parsed = urlsplit(value)
        return (value,) if parsed.scheme == "https" and parsed.hostname else ()
    return ()


def _response_title(response: HttpResponse) -> str | None:
    content_type = response.headers.get("Content-Type", "").casefold()
    text = response.body.decode("utf-8", errors="replace")
    if "json" in content_type or text.lstrip().startswith(("{", "[")):
        try:
            value: Any = json.loads(text)
        except json.JSONDecodeError:
            return None
        return _json_title(value)
    for pattern in (
        r"(?is)<title[^>]*>(.*?)</title>",
        r"(?is)<entry[^>]*>.*?<title[^>]*>(.*?)</title>",
    ):
        match = re.search(pattern, text)
        if match:
            return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", match.group(1))).strip()
    return None


def _json_title(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key in ("title", "name"):
            title = value.get(key)
            if isinstance(title, str):
                return title.strip()
            if isinstance(title, list) and title and isinstance(title[0], str):
                return title[0].strip()
        message = value.get("message")
        if message is not value:
            return _json_title(message)
    return None


def _titles_compatible(expected: str, actual: str) -> bool:
    def normalize(value: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", value.casefold()))

    expected_words = normalize(expected)
    actual_words = normalize(actual)
    if not expected_words or not actual_words:
        return False
    overlap = len(expected_words.intersection(actual_words))
    return overlap / max(1, min(len(expected_words), len(actual_words))) >= 0.75
