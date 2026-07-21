from __future__ import annotations

import json
from pathlib import Path

import pytest

from ascend_math_agent.source_provenance import (
    BoundedHttpSourceVerifier,
    HttpResponse,
    SourceVerificationStatus,
    WebDisabledSourceVerifier,
    canonical_identifiers,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identifier", "expected_url", "content_type", "body"),
    [
        (
            "doi:10.5555/12345678",
            "https://api.crossref.org/works/10.5555%2F12345678",
            "application/json",
            b'{"message":{"title":["A Verified Mathematical Source"]}}',
        ),
        (
            "arxiv:2401.01234",
            "https://export.arxiv.org/api/query?id_list=2401.01234",
            "application/atom+xml",
            b"<feed><entry><title>A Verified Mathematical Source</title></entry></feed>",
        ),
    ],
)
async def test_bounded_verifier_resolves_doi_and_arxiv(
    identifier: str,
    expected_url: str,
    content_type: str,
    body: bytes,
) -> None:
    requests: list[str] = []

    async def fetcher(url: str, timeout: float, maximum_bytes: int) -> HttpResponse:
        requests.append(url)
        assert timeout == 2.0
        assert maximum_bytes == 1_024
        return HttpResponse(200, url, {"Content-Type": content_type}, body)

    verifier = BoundedHttpSourceVerifier(
        fetcher=fetcher,
        timeout_seconds=2.0,
        maximum_bytes=1_024,
        maximum_attempts=1,
    )

    report = await verifier.verify([identifier], expected_title="A Verified Mathematical Source")

    assert report.records[0].status is SourceVerificationStatus.VERIFIED
    assert requests == [expected_url]


@pytest.mark.asyncio
async def test_verifier_accepts_canonical_redirect_and_reuses_success_cache(tmp_path: Path) -> None:
    cache_path = tmp_path / "source-cache.json"
    calls = 0

    async def fetcher(url: str, timeout: float, maximum_bytes: int) -> HttpResponse:
        nonlocal calls
        calls += 1
        return HttpResponse(
            200,
            "https://publisher.example.edu/canonical-paper",
            {"Content-Type": "text/html"},
            b"<title>Canonical Paper</title>",
        )

    verifier = BoundedHttpSourceVerifier(cache_path=cache_path, fetcher=fetcher)
    first = await verifier.verify(
        ["url:https://publisher.example.edu/old-paper"],
        expected_title="Canonical Paper",
    )
    second = await verifier.verify(
        ["url:https://publisher.example.edu/old-paper"],
        expected_title="Canonical Paper",
    )

    assert first.records[0].canonical_url == "https://publisher.example.edu/canonical-paper"
    assert second.records[0].status is SourceVerificationStatus.VERIFIED
    assert calls == 1
    assert json.loads(cache_path.read_text(encoding="utf-8"))["records"]


@pytest.mark.asyncio
async def test_verifier_retries_one_transient_failure() -> None:
    calls = 0

    async def fetcher(url: str, timeout: float, maximum_bytes: int) -> HttpResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("temporary")
        return HttpResponse(200, url, {"Content-Type": "text/html"}, b"<title>Paper</title>")

    verifier = BoundedHttpSourceVerifier(fetcher=fetcher, maximum_attempts=2)
    report = await verifier.verify(["url:https://publisher.example.edu/paper"])

    assert report.records[0].status is SourceVerificationStatus.VERIFIED
    assert calls == 2


@pytest.mark.asyncio
async def test_verifier_reports_partially_unavailable_identifier_set() -> None:
    async def fetcher(url: str, timeout: float, maximum_bytes: int) -> HttpResponse:
        if "crossref.org" in url:
            return HttpResponse(
                200,
                url,
                {"Content-Type": "application/json"},
                b'{"message":{"title":["Verified Part"]}}',
            )
        raise TimeoutError("offline fixture")

    verifier = BoundedHttpSourceVerifier(fetcher=fetcher, maximum_attempts=1)
    report = await verifier.verify(
        ["doi:10.5555/12345678", "arxiv:2401.01234"],
        expected_title="Verified Part",
    )

    statuses = {record.identifier: record.status for record in report.records}
    assert statuses == {
        "arxiv:2401.01234": SourceVerificationStatus.UNAVAILABLE,
        "doi:10.5555/12345678": SourceVerificationStatus.VERIFIED,
    }
    assert report.verified_identifiers == {"doi:10.5555/12345678"}
    assert report.warnings


@pytest.mark.asyncio
async def test_web_disabled_verifier_reports_unavailable_without_network() -> None:
    report = await WebDisabledSourceVerifier().verify(["doi:10.5555/12345678", "arxiv:2401.01234"])

    assert {record.status for record in report.records} == {SourceVerificationStatus.UNAVAILABLE}
    assert all("disabled by configuration" in record.detail for record in report.records)
    assert len(report.warnings) == 2


def test_canonical_identifiers_split_combined_model_formatting() -> None:
    identifiers = canonical_identifiers(
        ["DOI: 10.5555/12345678; arXiv:2401.01234", "https://publisher.example.edu/paper"]
    )

    assert identifiers == {
        "doi:10.5555/12345678",
        "arxiv:2401.01234",
        "url:https://publisher.example.edu/paper",
    }
