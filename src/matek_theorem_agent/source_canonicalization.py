"""Deterministic identities for reusable scholarly sources and exact claims."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .scientific import exact_statement_fingerprint, normalize_exact_statement
from .source_identifiers import source_identifiers

_ARXIV_REVISION = re.compile(r"\A(arxiv:.+?)(v\d+)\Z", re.IGNORECASE)
_STRONG_IDENTIFIER_PREFIXES = ("doi:", "arxiv:", "mr:", "isbn:")


class SourceCanonicalizationError(ValueError):
    pass


class _CanonicalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _normalized_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", normalized).strip()


def _fingerprint_text(value: str) -> str:
    normalized = _normalized_text(value).casefold()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def arxiv_base_identifier(identifier: str) -> str:
    normalized = identifier.strip().casefold()
    match = _ARXIV_REVISION.fullmatch(normalized)
    return match.group(1) if match else normalized


def canonical_source_identifiers(values: Iterable[str]) -> tuple[list[str], list[str]]:
    """Return canonical entity identifiers and separately retained arXiv revisions."""

    identifiers: set[str] = set()
    revisions: set[str] = set()
    for value in values:
        for identifier in source_identifiers(value):
            match = _ARXIV_REVISION.fullmatch(identifier)
            if match:
                identifiers.add(match.group(1).casefold())
                revisions.add(identifier.casefold())
            else:
                identifiers.add(identifier.casefold())
    return sorted(identifiers), sorted(revisions)


def provisional_source_fingerprint(title: str, authors: Sequence[str] = ()) -> str:
    title_key = _fingerprint_text(title)
    author_keys = sorted(_fingerprint_text(author) for author in authors if author.strip())
    if not title_key:
        raise SourceCanonicalizationError("provisional source identity requires a title")
    material = "\0".join([title_key, *author_keys])
    return "provisional:" + hashlib.sha256(material.encode()).hexdigest()


def canonical_source_key(
    identifiers: Iterable[str],
    *,
    title: str,
    authors: Sequence[str] = (),
    verified: bool,
) -> str:
    """Choose one source identity using the required stable-identifier precedence."""

    canonical, _ = canonical_source_identifiers(identifiers)
    if verified:
        for prefix in ("doi:", "arxiv:", "mr:", "isbn:", "url:"):
            match = next((item for item in canonical if item.startswith(prefix)), None)
            if match is not None:
                return match
    return provisional_source_fingerprint(title, authors)


def conflicting_stable_source_identifiers(
    first: Iterable[str],
    second: Iterable[str],
) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    """Return incompatible same-scheme stable identities.

    A shared URL or model alias is not sufficient evidence for merging two
    records that assert different DOI, arXiv, MR, or ISBN identities.  The
    conflict is returned to the caller so it can create a reviewable proposal
    or fail closed rather than silently collapsing distinct works.
    """

    first_ids, _ = canonical_source_identifiers(first)
    second_ids, _ = canonical_source_identifiers(second)
    conflicts: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    for prefix in _STRONG_IDENTIFIER_PREFIXES:
        left = tuple(item for item in first_ids if item.startswith(prefix))
        right = tuple(item for item in second_ids if item.startswith(prefix))
        if left and right and not set(left).intersection(right):
            conflicts[prefix.removesuffix(":")] = (left, right)
    return conflicts


class CanonicalSourceEntity(_CanonicalModel):
    source_key: str
    primary_identifier: str | None = None
    identifiers: list[str] = Field(default_factory=list)
    identifier_revisions: list[str] = Field(default_factory=list)
    titles: list[str]
    authors: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    evidence_links: list[str] = Field(default_factory=list)
    verification_provenance: list[str] = Field(default_factory=list)
    verified: bool = False

    @field_validator("source_key")
    @classmethod
    def source_key_is_nonblank(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not normalized:
            raise ValueError("canonical source keys must not be blank")
        return normalized

    @field_validator("primary_identifier")
    @classmethod
    def primary_identifier_is_normalized(cls, value: str | None) -> str | None:
        return value.strip().casefold() if value is not None else None

    @field_validator(
        "identifiers",
        "identifier_revisions",
        "titles",
        "authors",
        "aliases",
        "evidence_links",
        "verification_provenance",
    )
    @classmethod
    def text_lists_are_normalized(cls, values: list[str]) -> list[str]:
        normalized = [_normalized_text(value) for value in values if value.strip()]
        return list(dict.fromkeys(normalized))

    @model_validator(mode="after")
    def identity_is_consistent(self) -> CanonicalSourceEntity:
        if not self.titles:
            raise ValueError("canonical source entities require at least one title")
        if self.primary_identifier is not None and self.primary_identifier not in self.identifiers:
            raise ValueError("primary source identifier must occur in identifiers")
        if self.verified and self.primary_identifier is None:
            raise ValueError("verified source entities require a primary stable identifier")
        return self


def make_source_entity(
    *,
    title: str,
    identifiers: Iterable[str],
    authors: Sequence[str] = (),
    source_alias: str | None = None,
    evidence_links: Sequence[str] = (),
    verification_provenance: Sequence[str] = (),
    verified: bool,
) -> CanonicalSourceEntity:
    canonical, revisions = canonical_source_identifiers(identifiers)
    key = canonical_source_key(
        canonical,
        title=title,
        authors=authors,
        verified=verified,
    )
    primary = key if verified and not key.startswith("provisional:") else None
    return CanonicalSourceEntity(
        source_key=key,
        primary_identifier=primary,
        identifiers=canonical,
        identifier_revisions=revisions,
        titles=[title],
        authors=list(authors),
        aliases=[source_alias] if source_alias else [],
        evidence_links=list(evidence_links),
        verification_provenance=list(verification_provenance),
        verified=verified,
    )


def merge_source_entities(
    first: CanonicalSourceEntity,
    second: CanonicalSourceEntity,
) -> CanonicalSourceEntity:
    """Merge only records with an identical deterministic entity key."""

    if first.source_key != second.source_key:
        raise SourceCanonicalizationError(
            "source entities with different canonical keys require a reviewable merge proposal"
        )
    primary = first.primary_identifier or second.primary_identifier
    return CanonicalSourceEntity(
        source_key=first.source_key,
        primary_identifier=primary,
        identifiers=list(dict.fromkeys([*first.identifiers, *second.identifiers])),
        identifier_revisions=list(
            dict.fromkeys([*first.identifier_revisions, *second.identifier_revisions])
        ),
        titles=list(dict.fromkeys([*first.titles, *second.titles])),
        authors=list(dict.fromkeys([*first.authors, *second.authors])),
        aliases=list(dict.fromkeys([*first.aliases, *second.aliases])),
        evidence_links=list(dict.fromkeys([*first.evidence_links, *second.evidence_links])),
        verification_provenance=list(
            dict.fromkeys([*first.verification_provenance, *second.verification_provenance])
        ),
        verified=first.verified or second.verified,
    )


class CanonicalClaimEntity(_CanonicalModel):
    claim_key: str
    exact_statement: str
    scope: str
    aliases: list[str] = Field(default_factory=list)
    proof_attempt_ids: list[str] = Field(default_factory=list)

    @field_validator("claim_key")
    @classmethod
    def key_is_sha256(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("canonical claim keys must be SHA-256 digests")
        return normalized

    @field_validator("exact_statement", "scope")
    @classmethod
    def exact_text_is_nonblank(cls, value: str) -> str:
        normalized = normalize_exact_statement(value)
        if not normalized:
            raise ValueError("canonical claims require exact statement and scope")
        return normalized

    @field_validator("aliases", "proof_attempt_ids")
    @classmethod
    def identifiers_are_deduplicated(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values if value.strip()]
        return list(dict.fromkeys(normalized))

    @model_validator(mode="after")
    def key_matches_exact_statement_and_scope(self) -> CanonicalClaimEntity:
        if self.claim_key != canonical_claim_key(self.exact_statement, scope=self.scope):
            raise ValueError("claim key does not match exact statement and scope")
        return self


def canonical_claim_key(exact_statement: str, *, scope: str) -> str:
    statement_hash = exact_statement_fingerprint(exact_statement)
    return hashlib.sha256(f"{scope.strip().casefold()}\0{statement_hash}".encode()).hexdigest()


def make_claim_entity(
    exact_statement: str,
    *,
    scope: str,
    aliases: Sequence[str] = (),
    proof_attempt_ids: Sequence[str] = (),
) -> CanonicalClaimEntity:
    return CanonicalClaimEntity(
        claim_key=canonical_claim_key(exact_statement, scope=scope),
        exact_statement=exact_statement,
        scope=scope,
        aliases=list(aliases),
        proof_attempt_ids=list(proof_attempt_ids),
    )


def merge_exact_claim_entities(
    first: CanonicalClaimEntity,
    second: CanonicalClaimEntity,
) -> CanonicalClaimEntity:
    """Merge only exact normalized identity; semantic-near matches remain proposals."""

    if first.claim_key != second.claim_key:
        raise SourceCanonicalizationError(
            "non-identical mathematical claims require an audit or equivalence derivation"
        )
    return first.model_copy(
        update={
            "aliases": list(dict.fromkeys([*first.aliases, *second.aliases])),
            "proof_attempt_ids": list(
                dict.fromkeys([*first.proof_attempt_ids, *second.proof_attempt_ids])
            ),
        }
    )


__all__ = [
    "CanonicalClaimEntity",
    "CanonicalSourceEntity",
    "SourceCanonicalizationError",
    "arxiv_base_identifier",
    "canonical_claim_key",
    "canonical_source_identifiers",
    "canonical_source_key",
    "conflicting_stable_source_identifiers",
    "make_claim_entity",
    "make_source_entity",
    "merge_exact_claim_entities",
    "merge_source_entities",
    "provisional_source_fingerprint",
]
