"""Shared knowledge-graph node ID formats.

MATEK uses two node ID formats:

- Legacy deterministic hash IDs: ``XXX-XXXXXXXX`` — three uppercase letters, a dash,
  then 8-64 uppercase letters or digits.  This remains the only format for
  operational node types (problems, runs, tasks, audits, artifacts, sources,
  formalizations, human notes).
- Descriptive one-liner IDs: ``CLAIM: For a convex body of volume v ...`` — an
  uppercase full-word type prefix, the separator ``": "``, and a whitespace
  normalized one-line description chosen by the research agent that authored the
  node.  This format is used for agent-authored mathematical content (claims,
  definitions, approaches, proofs, proof attempts, derivations, obligations,
  counterexamples, experiments) so that agents can refer to graph artifacts by
  meaningful, memorable names instead of opaque hashes.

Both formats are accepted when reading; only the descriptive format is minted for
the agent-authored types.  The two formats are disjoint: a legacy ID never
contains ``": "`` and a descriptive ID never matches the legacy hash shape.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Collection, Iterable

LEGACY_NODE_ID = re.compile(r"\A[A-Z]{3}-[A-Z0-9]{8,64}\Z")

DESCRIPTIVE_ID_WORDS: frozenset[str] = frozenset(
    {
        "APPROACH",
        "CLAIM",
        "COUNTEREXAMPLE",
        "DEFINITION",
        "DERIVATION",
        "EXPERIMENT",
        "OBLIGATION",
        "PROOF",
        "PROOF ATTEMPT",
    }
)

MAX_ID_DESCRIPTION_LENGTH = 160
MAX_NODE_ID_LENGTH = 200

_WHITESPACE = re.compile(r"\s+")
_DESCRIPTIVE_SHAPE = re.compile(
    r"\A(?P<word>[A-Za-z][A-Za-z0-9 ]*?):\s*(?P<description>\S(?:.*?\S)?)\s*\Z"
)
_COLLISION_SUFFIX = re.compile(r"\s*\(\d+\)\Z")
# Characters that would corrupt Obsidian wikilinks or flat frontmatter when an ID
# is rendered as a link label.  Normalization replaces them with lookalikes.
_UNSAFE_DESCRIPTION_CHARS = str.maketrans({"[": "(", "]": ")", "|": "/"})


def normalize_id_description(text: str, *, max_length: int = MAX_ID_DESCRIPTION_LENGTH) -> str:
    """Collapse ``text`` to one capped, single-line ID description.

    Whitespace (including newlines) collapses to single spaces, and wikilink-hostile
    brackets are replaced so IDs always render cleanly.  Overlong text is truncated
    at a word boundary so agents always read a clean phrase.
    """

    normalized = _WHITESPACE.sub(" ", text.translate(_UNSAFE_DESCRIPTION_CHARS)).strip()
    if len(normalized) <= max_length:
        return normalized
    truncated = normalized[:max_length]
    boundary = truncated.rfind(" ")
    if boundary >= max_length // 2:
        truncated = truncated[:boundary]
    truncated = truncated.rstrip(" ,;:-\u2013\u2014")
    return truncated or normalized[:max_length].strip()


def descriptive_node_id(word_prefix: str, description: str) -> str:
    """Compose one canonical descriptive node ID from a type word and a one-liner."""

    word = _WHITESPACE.sub(" ", word_prefix).strip().upper()
    if word not in DESCRIPTIVE_ID_WORDS:
        raise ValueError(f"unknown descriptive node ID prefix: {word_prefix!r}")
    normalized = normalize_id_description(description)
    if not normalized:
        raise ValueError("descriptive node IDs require a nonempty one-line description")
    return f"{word}: {normalized}"


def is_legacy_node_id(value: str) -> bool:
    return LEGACY_NODE_ID.fullmatch(value.strip().upper()) is not None


def is_descriptive_node_id(value: str) -> bool:
    """Return whether ``value`` is a canonical descriptive node ID."""

    candidate = _WHITESPACE.sub(" ", value).strip()
    match = _DESCRIPTIVE_SHAPE.fullmatch(candidate)
    if match is None:
        return False
    if match.group("word").upper() not in DESCRIPTIVE_ID_WORDS:
        return False
    description = match.group("description")
    if any(char in description for char in "[]|"):
        return False
    canonical = f"{match.group('word').upper()}: {description}"
    return canonical == candidate and len(candidate) <= MAX_NODE_ID_LENGTH


def validate_any_node_id(value: str) -> str:
    """Normalize and validate one node ID in either supported format.

    Legacy hash IDs are uppercased.  Descriptive IDs are whitespace-normalized and
    their type word is uppercased; the description text keeps the author's casing.
    """

    candidate = _WHITESPACE.sub(" ", value).strip()
    legacy = candidate.upper()
    if LEGACY_NODE_ID.fullmatch(legacy):
        return legacy
    match = _DESCRIPTIVE_SHAPE.fullmatch(candidate)
    if match is not None:
        word = match.group("word").upper()
        description = match.group("description")
        normalized = f"{word}: {description}"
        if (
            word in DESCRIPTIVE_ID_WORDS
            and not any(char in description for char in "[]|")
            and len(normalized) <= MAX_NODE_ID_LENGTH
        ):
            return normalized
    raise ValueError(
        "node ID must be a legacy PREFIX- hash ID or a descriptive 'WORD: one-line "
        "description' ID with a known type word"
    )


def dedupe_descriptive_id(candidate: str, taken_casefolds: Collection[str]) -> str:
    """Return ``candidate`` or the first free ``candidate (n)`` variant.

    ``taken_casefolds`` must contain the casefolded IDs already in use; comparison
    is case-insensitive so visually identical descriptions never coexist.
    """

    if candidate.casefold() not in taken_casefolds:
        return candidate
    index = 2
    while True:
        suffixed = f"{candidate} ({index})"
        if suffixed.casefold() not in taken_casefolds:
            return suffixed
        index += 1


def strip_collision_suffix(node_id: str) -> str:
    """Return the base descriptive ID without a trailing `` (n)`` collision suffix."""

    return _COLLISION_SUFFIX.sub("", node_id)


def suggest_node_ids(
    attempted: str,
    candidates: Iterable[str],
    *,
    limit: int = 3,
) -> list[str]:
    """Return the closest known node IDs for one mistyped reference.

    This is the deterministic, offline lexical fallback for exact-match lookup
    misses: stdlib sequence similarity over the known IDs, case-insensitive, best
    matches first.  It is advisory text for error messages only — admission and
    gates still require exact IDs, and no suggestion is ever accepted implicitly.
    """

    pool = sorted(dict.fromkeys(item for item in candidates if item.strip()))
    if not pool:
        return []
    query = attempted.casefold()
    # A shared "CLAIM: " style prefix would otherwise inflate similarity between
    # unrelated statements, so the description tail is scored on its own too.
    query_tail = query.split(": ", 1)[-1]
    scored = []
    for candidate in pool:
        folded = candidate.casefold()
        overall = difflib.SequenceMatcher(None, query, folded).ratio()
        tail = difflib.SequenceMatcher(None, query_tail, folded.split(": ", 1)[-1]).ratio()
        scored.append((max(overall, tail), candidate))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [candidate for score, candidate in scored[:limit] if score > 0.45]


def unknown_id_message(prefix: str, unknown_ids: Iterable[str], known_ids: Iterable[str]) -> str:
    """Render one unknown-ID error with per-ID 'did you mean' suggestions."""

    known = list(known_ids)
    parts: list[str] = []
    for unknown in sorted(dict.fromkeys(unknown_ids)):
        suggestions = suggest_node_ids(unknown, known)
        if suggestions:
            parts.append(f"{unknown} (did you mean: {'; '.join(suggestions)})")
        else:
            parts.append(unknown)
    return prefix + ", ".join(parts)
