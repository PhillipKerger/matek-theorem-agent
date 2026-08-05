from __future__ import annotations

import pytest

from matek_theorem_agent.source_canonicalization import (
    SourceCanonicalizationError,
    canonical_claim_key,
    canonical_source_identifiers,
    canonical_source_key,
    conflicting_stable_source_identifiers,
    make_claim_entity,
    make_source_entity,
    merge_exact_claim_entities,
    merge_source_entities,
    split_source_entity_by_doi,
)


def test_verified_source_key_uses_required_identifier_precedence() -> None:
    identifiers = [
        "https://publisher.example.edu/paper",
        "arXiv:2401.01234v3",
        "DOI:10.5555/12345678",
    ]

    assert (
        canonical_source_key(
            identifiers,
            title="A Result",
            verified=True,
        )
        == "doi:10.5555/12345678"
    )


def test_arxiv_versions_share_entity_and_remain_revisions() -> None:
    first = make_source_entity(
        title="A Preprint",
        identifiers=["arXiv:2401.01234v1"],
        source_alias="compiler-source-1",
        verified=True,
    )
    second = make_source_entity(
        title="A Preprint (revised)",
        identifiers=["https://arxiv.org/abs/2401.01234v3"],
        source_alias="worker-source-8",
        verified=True,
    )

    merged = merge_source_entities(first, second)

    assert merged.source_key == "arxiv:2401.01234"
    assert merged.primary_identifier == "arxiv:2401.01234"
    assert merged.identifiers == ["arxiv:2401.01234"]
    assert merged.identifier_revisions == ["arxiv:2401.01234v1", "arxiv:2401.01234v3"]
    assert merged.aliases == ["compiler-source-1", "worker-source-8"]


def test_unverified_identifier_does_not_claim_stable_entity_identity() -> None:
    key = canonical_source_key(
        ["doi:10.5555/12345678"],
        title="Unverified Model Citation",
        authors=["Ada Example"],
        verified=False,
    )

    assert key.startswith("provisional:")


def test_fuzzy_title_similarity_never_merges_source_entities() -> None:
    first = make_source_entity(
        title="Exact Bounds for Online Servers",
        identifiers=[],
        authors=["Ada Example"],
        verified=False,
    )
    second = make_source_entity(
        title="Exact Bound for the Online Server Problem",
        identifiers=[],
        authors=["Ada Example"],
        verified=False,
    )

    with pytest.raises(SourceCanonicalizationError, match="reviewable merge proposal"):
        merge_source_entities(first, second)


def test_shared_url_does_not_erase_conflicting_doi_identities() -> None:
    conflicts = conflicting_stable_source_identifiers(
        ["doi:10.1000/first", "https://publisher.example/shared"],
        ["doi:10.1000/second", "https://publisher.example/shared"],
    )

    assert conflicts == {
        "doi": (("doi:10.1000/first",), ("doi:10.1000/second",)),
    }


def test_same_doi_renderings_deduplicate_without_splitting() -> None:
    entity = make_source_entity(
        title="One publication",
        identifiers=[
            "DOI:10.5555/ABC",
            "https://doi.org/10.5555/abc",
        ],
        verified=True,
    )

    assert split_source_entity_by_doi(entity) == [entity]
    assert entity.identifiers == ["doi:10.5555/abc"]


def test_distinct_dois_split_while_preserving_shared_provenance() -> None:
    entity = make_source_entity(
        title="Conference and journal versions",
        identifiers=[
            "doi:10.1137/1.9781611973730.79",
            "doi:10.1287/moor.2017.0876",
            "arxiv:1407.0001v2",
        ],
        source_alias="feldman-svensson-zenklusen",
        verification_provenance=["Both publications were independently resolved."],
        verified=True,
    )

    versions = split_source_entity_by_doi(entity)

    assert [version.source_key for version in versions] == [
        "doi:10.1137/1.9781611973730.79",
        "doi:10.1287/moor.2017.0876",
    ]
    assert all(version.primary_identifier == version.source_key for version in versions)
    assert all("arxiv:1407.0001" in version.identifiers for version in versions)
    assert all(version.aliases == ["feldman-svensson-zenklusen"] for version in versions)


def test_canonical_identifier_extraction_returns_base_and_revision() -> None:
    identifiers, revisions = canonical_source_identifiers(
        ["arXiv:math/0301234v2", "ISBN 978-0-306-40615-7"]
    )

    assert identifiers == ["arxiv:math/0301234", "isbn:9780306406157"]
    assert revisions == ["arxiv:math/0301234v2"]


def test_exact_claims_merge_but_near_duplicates_require_equivalence_audit() -> None:
    first = make_claim_entity(
        "For every n, P(n).",
        scope="branch",
        aliases=["CLM-ALIAS001"],
        proof_attempt_ids=["PRF-ATTEMPT1"],
    )
    cosmetic = make_claim_entity(
        "  For every n,   P(n).  ",
        scope="branch",
        aliases=["CLM-ALIAS002"],
        proof_attempt_ids=["PRF-ATTEMPT2"],
    )
    merged = merge_exact_claim_entities(first, cosmetic)

    assert merged.claim_key == canonical_claim_key("For every n, P(n).", scope="branch")
    assert merged.aliases == ["CLM-ALIAS001", "CLM-ALIAS002"]
    assert merged.proof_attempt_ids == ["PRF-ATTEMPT1", "PRF-ATTEMPT2"]

    near_duplicate = make_claim_entity("For every integer n, P(n).", scope="branch")
    with pytest.raises(SourceCanonicalizationError, match="equivalence derivation"):
        merge_exact_claim_entities(first, near_duplicate)
