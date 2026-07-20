"""Canonical public-source identifier extraction independent of workflow stages."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_DOI_PATTERN = re.compile(
    r"(?i)(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/[^\s<>\"{}]+)"
)
_ARXIV_LABELED_PATTERN = re.compile(
    r"(?i)(?:\barxiv\s*:\s*|https?://arxiv\.org/(?:abs|pdf)/)"
    r"((?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z-]+)?/\d{7})(?:v\d+)?)"
)
_BARE_ARXIV_PATTERN = re.compile(r"(?i)^(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z-]+)?/\d{7})(?:v\d+)?$")
_MR_PATTERN = re.compile(r"(?i)\bMR\s*(\d{6,8})\b")
_HTTPS_URL_PATTERN = re.compile(r"https://[^\s<>\"{}\[\]]+")
_RESERVED_SOURCE_HOSTS = frozenset({"localhost", "example.com", "example.org", "example.net"})


def _canonical_https_url(value: str) -> str | None:
    candidate = value.strip().rstrip(".,;:)")
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme.casefold() != "https" or not hostname or parsed.username is not None:
        return None
    if (
        hostname in _RESERVED_SOURCE_HOSTS
        or hostname.endswith((".example", ".invalid", ".localhost", ".test"))
        or "." not in hostname
    ):
        return None
    port = f":{parsed.port}" if parsed.port not in (None, 443) else ""
    normalized = urlunsplit(("https", f"{hostname}{port}", parsed.path or "/", "", ""))
    return normalized.rstrip("/")


def _valid_isbn(value: str) -> str | None:
    raw = value.strip()
    if not re.fullmatch(r"(?i)(?:ISBN(?:-1[03])?\s*:?\s*)?[0-9Xx -]{10,24}", raw):
        return None
    digits = re.sub(r"[^0-9Xx]", "", raw).upper()
    if len(digits) == 10:
        if not re.fullmatch(r"\d{9}[\dX]", digits):
            return None
        total = sum(
            (10 - index) * (10 if digit == "X" else int(digit))
            for index, digit in enumerate(digits)
        )
        return digits if total % 11 == 0 else None
    if len(digits) == 13 and digits.isdigit():
        total = sum(int(digit) * (1 if index % 2 == 0 else 3) for index, digit in enumerate(digits))
        return digits if total % 10 == 0 else None
    return None


def source_identifiers(value: str) -> frozenset[str]:
    """Extract canonical DOI, arXiv, ISBN, MR, and authoritative HTTPS identifiers."""

    identifiers: set[str] = set()
    text = value.strip()
    for match in _DOI_PATTERN.finditer(text):
        identifiers.add(f"doi:{match.group(1).rstrip('.,;:)').casefold()}")
    for match in _ARXIV_LABELED_PATTERN.finditer(text):
        identifiers.add(f"arxiv:{match.group(1).rstrip('.,;:)').casefold()}")
    if _BARE_ARXIV_PATTERN.fullmatch(text):
        identifiers.add(f"arxiv:{text.casefold()}")
    for match in _MR_PATTERN.finditer(text):
        identifiers.add(f"mr:{match.group(1)}")
    isbn = _valid_isbn(text)
    if isbn is not None:
        identifiers.add(f"isbn:{isbn}")
    for match in _HTTPS_URL_PATTERN.finditer(text):
        normalized_url = _canonical_https_url(match.group(0))
        if normalized_url is None:
            continue
        resolver_host = (urlsplit(normalized_url).hostname or "").casefold()
        if resolver_host not in {"arxiv.org", "dx.doi.org", "doi.org", "www.arxiv.org"}:
            identifiers.add(f"url:{normalized_url}")
    return frozenset(identifiers)


def valid_source_identifier(value: str | None) -> bool:
    return bool(value and source_identifiers(value))


def tool_metadata_source_identifiers(
    tool_metadata: Iterable[Mapping[str, Any]],
) -> frozenset[str]:
    """Extract identifiers actually returned by provider citation metadata."""

    identifiers: set[str] = set()
    for item in tool_metadata:
        if item.get("type") == "url_citation":
            url = item.get("url")
            if isinstance(url, str):
                identifiers.update(source_identifiers(url))
            continue
        if item.get("type") != "web_search_call":
            continue
        action = item.get("action")
        if not isinstance(action, Mapping):
            continue
        sources = action.get("sources")
        if not isinstance(sources, Iterable) or isinstance(sources, (str, bytes, Mapping)):
            continue
        for source in sources:
            if not isinstance(source, Mapping):
                continue
            url = source.get("url")
            if isinstance(url, str):
                identifiers.update(source_identifiers(url))
    return frozenset(identifiers)
