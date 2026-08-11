"""Persistent, transactional Markdown knowledge graph service.

Markdown notes are authoritative.  ``graph-index.sqlite`` is a disposable query
index rebuilt from those notes, while ``graph-state.json`` supplies optimistic
concurrency, human-edit detection, and crash recovery for multi-note patches.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import unicodedata
import warnings
from collections import defaultdict, deque
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import quote

from pydantic import ValidationError

from ..graph_ids import (
    dedupe_descriptive_id,
    descriptive_node_id,
    is_legacy_node_id,
    unknown_id_message,
    validate_any_node_id,
)
from ..scientific import (
    ScientificObligationDeclaration,
    ScientificResult,
    ScientificResultDisposition,
    ScientificResultKind,
    ScientificScope,
    normalize_exact_statement,
    transitive_result_dependency_keys,
)
from ..source_canonicalization import (
    SOURCE_IDENTITY_AMBIGUITY_PREFIX,
    CanonicalSourceEntity,
    SourceCanonicalizationError,
    canonical_source_identifiers,
    conflicting_stable_source_identifiers,
    make_source_entity,
    merge_source_entities,
    split_source_entity_by_doi,
)
from ..workspace import (
    atomic_write_json,
    atomic_write_text,
    ensure_path_confined,
    sha256_file,
    sha256_text,
)
from .admission import (
    ScientificAdmissionError,
    build_scientific_admission,
    canonical_admitted_definition_scope,
    canonical_definition_dependency_contract,
    matches_admission_binding,
    node_has_scientific_admission_binding,
)
from .ledger import logical_version
from .markdown import (
    GENERATED_END,
    GENERATED_START,
    GraphMarkdownError,
    exact_statement,
    generated_section,
    machine_hash,
    new_generated_body,
    parse_node_note,
    render_node_note,
    replace_generated_section,
    statement_hash,
    wikilink_for,
)
from .migration import (
    LegacyMigrationApplicationRecord,
    LegacyMigrationError,
    LegacyMigrationReport,
    legacy_archive_sha256,
    load_legacy_migration_application,
    migration_report_sha256,
    plan_legacy_graph_backfill,
    write_legacy_migration_application,
)
from .models import (
    NODE_ID_PREFIXES,
    NODE_ID_WORDS,
    NODE_TYPE_DIRECTORIES,
    ClaimType,
    EpistemicStatus,
    GraphChangeRecord,
    GraphContextNode,
    GraphContextSlice,
    GraphDiff,
    GraphEdge,
    GraphFrontier,
    GraphHygieneAction,
    GraphHygieneReport,
    GraphMergeResult,
    GraphNode,
    GraphNodeSummary,
    GraphPatch,
    GraphSnapshotVerification,
    GraphState,
    GraphStatus,
    GraphValidationIssue,
    GraphValidationReport,
    NodeType,
    RelationType,
    WorkflowStatus,
)
from .snapshots import (
    DEFAULT_SNAPSHOT_CHECKPOINT_INTERVAL,
    SnapshotIntegrityError,
    SnapshotStore,
)
from .targets import (
    FrozenTarget,
    TargetBindingDisposition,
    TargetRegistryError,
    bind_frozen_target,
    canonical_contract_json,
    load_target_registry,
    render_target_registry,
    target_semantic_fingerprint,
)

GRAPH_SCHEMA_VERSION = 1
GRAPH_COLLECTION_RELATIVE = Path(".matek") / "knowledge"
GRAPH_DIRECTORIES = (
    "Problems",
    "Definitions",
    "Claims",
    "Proofs",
    "Proof Attempts",
    "Derivations",
    "Obligations",
    "Approaches",
    "Counterexamples",
    "Experiments",
    "Sources",
    "Tasks",
    "Audits",
    "Formalizations",
    "Runs",
    "Artifacts",
    "Human Notes",
    "Dashboards",
)

_REVISION = re.compile(r"\A\d{8}-[0-9a-f]{16}\Z")
_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")
_GRAPH_NAME = re.compile(r"\A[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\Z")
_NOTE_FILENAME_UNSAFE = re.compile(r'[\x00-\x1f<>:"/\\|?*\[\]#^]')
_WINDOWS_RESERVED_FILENAMES = {
    "aux",
    "clock$",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}

MAIN_RESULT_NEEDS_TAG = "MAIN_RESULT_NEEDS"
_MAIN_RESULT_NEEDS_METADATA = "matek_main_result_needs"
MANUSCRIPT_CONTEXT_MAXIMUM_NODES = 80
FORMALIZATION_CONTEXT_MAXIMUM_NODES = 60
_TRUSTED_CONTEXT_POLICY = "markdown-graph-trusted-v1"


class KnowledgeGraphError(RuntimeError):
    """Base error for graph integrity or persistence failures."""


class GraphNotInitializedError(KnowledgeGraphError):
    pass


class GraphConflictError(KnowledgeGraphError):
    pass


class GraphValidationError(KnowledgeGraphError):
    pass


class GraphMetadataInvariantError(GraphValidationError):
    failure_class = "metadata_invariant"


class GraphTargetValidationError(GraphValidationError):
    failure_class = "scientific_target"


@dataclass(frozen=True)
class _VerifiedWorkerSourceRecord:
    alias: str
    entity: CanonicalSourceEntity
    evidence_claims: tuple[tuple[str, tuple[str, ...]], ...]


def _doi_identifiers(entity: CanonicalSourceEntity) -> tuple[str, ...]:
    return tuple(identifier for identifier in entity.identifiers if identifier.startswith("doi:"))


def _source_candidates_conflict(
    candidates: Sequence[tuple[GraphNode, CanonicalSourceEntity]],
) -> bool:
    return any(
        conflicting_stable_source_identifiers(left.identifiers, right.identifiers)
        for index, (_, left) in enumerate(candidates)
        for _, right in candidates[index + 1 :]
    )


def _source_identity_decision(
    *,
    context: str,
    identifiers: Iterable[str],
    aliases: Iterable[str] = (),
    candidate_node_ids: Iterable[str] = (),
) -> dict[str, object]:
    normalized, _ = canonical_source_identifiers(identifiers)
    return {
        "type": "multiple_doi_versions",
        "context": context,
        "decision": "preserve_separate_source_nodes",
        "doi_identifiers": [item for item in normalized if item.startswith("doi:")],
        "aliases": sorted(set(aliases), key=str.casefold),
        "candidate_node_ids": sorted(set(candidate_node_ids)),
    }


def _source_identity_issue(decision: Mapping[str, object]) -> str:
    return SOURCE_IDENTITY_AMBIGUITY_PREFIX + _canonical_json(decision)


def _source_identity_decision_artifact(
    *,
    graph_name: str,
    problem_id: str,
    run_id: str,
    timestamp: datetime,
    decisions: Sequence[Mapping[str, object]],
) -> tuple[str, str]:
    payload = {
        "schema_version": 1,
        "failure_class": "source_identity_ambiguity",
        "graph_name": graph_name,
        "problem_id": problem_id,
        "run_id": run_id,
        "timestamp": timestamp.isoformat(),
        "decisions": list(decisions),
    }
    digest = hashlib.sha256(_canonical_json(payload).encode()).hexdigest()[:20]
    relative = f"repairs/source-identity-decision-{digest}.json"
    return relative, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _metadata_text_list(node: GraphNode, key: str) -> list[str]:
    raw = node.metadata.get(key)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str) and item.strip()]


def _source_entity_from_node(node: GraphNode) -> CanonicalSourceEntity:
    if node.node_type is not NodeType.SOURCE:
        raise GraphValidationError(
            f"canonical source identity collides with non-source node {node.matek_id}"
        )
    source_key = str(node.metadata.get("matek_source_id") or "").strip().casefold()
    if not source_key:
        raise GraphValidationError(
            f"existing source node {node.matek_id} lacks a canonical source identity"
        )
    identifiers = _metadata_text_list(node, "matek_identifiers")
    verified = bool(node.metadata.get("matek_verified", False))
    primary_value = node.metadata.get("matek_primary_identifier")
    primary = str(primary_value).strip().casefold() if isinstance(primary_value, str) else None
    if verified and primary is None and not source_key.startswith("provisional:"):
        primary = source_key
    provenance = _metadata_text_list(node, "matek_verification_provenance")
    rendered_verification = _generated_heading_value(node.body, "Verification")
    if rendered_verification and rendered_verification not in provenance:
        provenance.append(rendered_verification)
    try:
        return CanonicalSourceEntity(
            source_key=source_key,
            primary_identifier=primary,
            identifiers=identifiers,
            identifier_revisions=_metadata_text_list(node, "matek_identifier_revisions"),
            titles=_metadata_text_list(node, "matek_source_titles") or [node.title],
            authors=_metadata_text_list(node, "matek_source_authors"),
            aliases=_metadata_text_list(node, "matek_source_aliases"),
            evidence_links=_metadata_text_list(node, "matek_evidence_links"),
            verification_provenance=provenance,
            verified=verified,
        )
    except ValueError as exc:
        raise GraphMetadataInvariantError(
            f"existing canonical source node {node.matek_id} is malformed: {exc}"
        ) from exc


def _source_candidate_node_ids(
    existing_nodes: Iterable[GraphNode],
    *,
    problem_id: str,
    identifiers: Iterable[str],
) -> list[str]:
    normalized, _ = canonical_source_identifiers(identifiers)
    identifier_set = set(normalized)
    return sorted(
        node.matek_id
        for node in existing_nodes
        if node.node_type is NodeType.SOURCE
        and node.problem_id == problem_id
        and identifier_set.intersection(_source_entity_from_node(node).identifiers)
    )


def _compatible_existing_sources(
    *,
    existing_nodes: Iterable[GraphNode],
    problem_id: str,
    incoming: CanonicalSourceEntity,
    deterministic_source_id: str,
    context: str,
    require_verified_overlap: bool,
) -> tuple[
    list[tuple[GraphNode, CanonicalSourceEntity]],
    bool,
    list[dict[str, object]],
]:
    """Resolve safe upgrades and isolate ambiguous DOI candidates."""

    compatible: list[tuple[GraphNode, CanonicalSourceEntity]] = []
    decisions: list[dict[str, object]] = []
    blocked_direct = False
    for candidate in existing_nodes:
        if candidate.node_type is not NodeType.SOURCE or candidate.problem_id != problem_id:
            continue
        candidate_entity = _source_entity_from_node(candidate)
        overlaps = bool(set(candidate_entity.identifiers).intersection(incoming.identifiers))
        if len(_doi_identifiers(candidate_entity)) > 1:
            if overlaps or candidate.matek_id == deterministic_source_id:
                decisions.append(
                    _source_identity_decision(
                        context=context,
                        identifiers=[*candidate_entity.identifiers, *incoming.identifiers],
                        aliases=[*candidate_entity.aliases, *incoming.aliases],
                        candidate_node_ids=[candidate.matek_id],
                    )
                )
                blocked_direct = blocked_direct or candidate.matek_id == deterministic_source_id
            continue
        if (
            candidate.matek_id == deterministic_source_id
            and candidate_entity.source_key == incoming.source_key
        ):
            compatible.append((candidate, candidate_entity))
            continue
        if require_verified_overlap and (not incoming.verified or not candidate_entity.verified):
            continue
        if candidate.matek_id == deterministic_source_id and not overlaps:
            raise GraphValidationError(
                f"canonical source ID collision at {deterministic_source_id}"
            )
        if not overlaps:
            continue
        if conflicting_stable_source_identifiers(
            candidate_entity.identifiers,
            incoming.identifiers,
        ):
            decisions.append(
                _source_identity_decision(
                    context=context,
                    identifiers=[*candidate_entity.identifiers, *incoming.identifiers],
                    aliases=[*candidate_entity.aliases, *incoming.aliases],
                    candidate_node_ids=[candidate.matek_id],
                )
            )
            blocked_direct = blocked_direct or candidate.matek_id == deterministic_source_id
            continue
        compatible.append((candidate, candidate_entity))

    if _source_candidates_conflict(compatible):
        decisions.append(
            _source_identity_decision(
                context=f"{context}_ambiguous_matches",
                identifiers=[
                    identifier
                    for _, candidate_entity in compatible
                    for identifier in candidate_entity.identifiers
                ],
                aliases=[
                    *incoming.aliases,
                    *(
                        alias
                        for _, candidate_entity in compatible
                        for alias in candidate_entity.aliases
                    ),
                ],
                candidate_node_ids=[candidate.matek_id for candidate, _ in compatible],
            )
        )
        blocked_direct = blocked_direct or any(
            candidate.matek_id == deterministic_source_id for candidate, _ in compatible
        )
        compatible = []
    return compatible, blocked_direct, decisions


def _verified_worker_source_records(
    raw_ledger: object,
    *,
    source_artifact: str,
) -> tuple[list[_VerifiedWorkerSourceRecord], list[dict[str, object]]]:
    if not isinstance(raw_ledger, list):
        raise GraphValidationError("typed source_ledger must be a list")
    records: list[_VerifiedWorkerSourceRecord] = []
    decisions: list[dict[str, object]] = []
    for index, raw_entry in enumerate(raw_ledger):
        if not isinstance(raw_entry, Mapping):
            raise GraphValidationError(
                f"typed source ledger entry {index} must be a structured object"
            )
        if raw_entry.get("verified") is not True:
            continue
        alias = str(raw_entry.get("source_id") or "").strip()
        title = str(raw_entry.get("title") or "").strip()
        raw_identifiers = raw_entry.get("identifiers")
        if (
            not alias
            or not title
            or not isinstance(raw_identifiers, list)
            or any(not isinstance(item, str) for item in raw_identifiers)
        ):
            raise GraphValidationError(
                f"verified source ledger entry {index} lacks valid identity fields"
            )
        raw_authors = raw_entry.get("authors", [])
        if not isinstance(raw_authors, list) or any(
            not isinstance(item, str) for item in raw_authors
        ):
            raise GraphValidationError(
                f"verified source ledger entry {alias!r} has malformed authors"
            )
        raw_claims = raw_entry.get("evidence_claims")
        if not isinstance(raw_claims, list):
            raise GraphValidationError(
                f"verified source ledger entry {alias!r} has malformed evidence claims"
            )
        evidence_claims: list[tuple[str, tuple[str, ...]]] = []
        for raw_claim in raw_claims:
            if not isinstance(raw_claim, Mapping):
                raise GraphValidationError(
                    f"verified source ledger entry {alias!r} has malformed evidence claims"
                )
            claim = str(raw_claim.get("claim") or "").strip()
            raw_source_ids = raw_claim.get("source_ids")
            if (
                not claim
                or not isinstance(raw_source_ids, list)
                or any(not isinstance(item, str) for item in raw_source_ids)
            ):
                raise GraphValidationError(
                    f"verified source ledger entry {alias!r} has malformed evidence links"
                )
            source_ids = tuple(
                dict.fromkeys(item.strip() for item in raw_source_ids if item.strip())
            )
            evidence_claims.append((claim, source_ids))
        verification_detail = str(raw_entry.get("verification_detail") or "").strip()
        identifiers = [item.strip() for item in raw_identifiers if item.strip()]
        evidence_links = [item for item in identifiers if item.casefold().startswith("https://")]
        try:
            entity = make_source_entity(
                title=title,
                identifiers=identifiers,
                authors=[item.strip() for item in raw_authors if item.strip()],
                source_alias=alias,
                evidence_links=evidence_links,
                verification_provenance=[
                    verification_detail,
                    f"Verified worker ledger {source_artifact} ({alias}).",
                ],
                verified=True,
            )
        except (SourceCanonicalizationError, ValueError) as exc:
            raise GraphValidationError(
                f"verified source ledger entry {alias!r} has no valid stable identity: {exc}"
            ) from exc
        versions = split_source_entity_by_doi(entity)
        if len(versions) > 1:
            decisions.append(
                _source_identity_decision(
                    context="worker_source_ledger",
                    identifiers=entity.identifiers,
                    aliases=[alias],
                )
            )
        records.extend(
            _VerifiedWorkerSourceRecord(
                alias=alias,
                entity=version,
                evidence_claims=tuple(evidence_claims),
            )
            for version in versions
        )
    return sorted(records, key=lambda item: (item.entity.source_key, item.alias)), decisions


def _group_verified_worker_sources(
    records: Sequence[_VerifiedWorkerSourceRecord],
) -> tuple[
    list[CanonicalSourceEntity],
    dict[str, set[str]],
    list[dict[str, object]],
]:
    """Merge records only through exact aliases or canonical stable identifiers."""

    parents = list(range(len(records)))
    group_members = [{index} for index in range(len(records))]
    decisions: list[dict[str, object]] = []

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> bool:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return True
        left_identifiers = {
            identifier
            for index in group_members[left_root]
            for identifier in records[index].entity.identifiers
        }
        right_identifiers = {
            identifier
            for index in group_members[right_root]
            for identifier in records[index].entity.identifiers
        }
        if conflicting_stable_source_identifiers(left_identifiers, right_identifiers):
            decisions.append(
                _source_identity_decision(
                    context="worker_source_grouping",
                    identifiers=[*left_identifiers, *right_identifiers],
                    aliases=[
                        records[index].alias
                        for index in group_members[left_root] | group_members[right_root]
                    ],
                )
            )
            return False
        root = min(left_root, right_root)
        child = max(left_root, right_root)
        parents[child] = root
        group_members[root].update(group_members[child])
        group_members[child].clear()
        return True

    for left, left_record in enumerate(records):
        left_identifiers = set(left_record.entity.identifiers)
        for right in range(left + 1, len(records)):
            right_record = records[right]
            overlaps = left_record.alias == right_record.alias or bool(
                left_identifiers.intersection(right_record.entity.identifiers)
            )
            if not overlaps:
                continue
            union(left, right)

    grouped: dict[int, list[_VerifiedWorkerSourceRecord]] = defaultdict(list)
    for index, record in enumerate(records):
        grouped[find(index)].append(record)

    entities: list[CanonicalSourceEntity] = []
    reference_to_keys: dict[str, set[str]] = defaultdict(set)
    for members in grouped.values():
        identifiers = sorted(
            {identifier for member in members for identifier in member.entity.identifiers}
        )
        revisions = sorted(
            {revision for member in members for revision in member.entity.identifier_revisions}
        )
        titles = sorted(
            {title for member in members for title in member.entity.titles},
            key=str.casefold,
        )
        authors = sorted(
            {author for member in members for author in member.entity.authors},
            key=str.casefold,
        )
        aliases = sorted({member.alias for member in members}, key=str.casefold)
        evidence_links = sorted(
            {link for member in members for link in member.entity.evidence_links}
        )
        provenance = sorted(
            {item for member in members for item in member.entity.verification_provenance}
        )
        seed = make_source_entity(
            title=titles[0],
            identifiers=identifiers,
            authors=authors,
            source_alias=aliases[0],
            evidence_links=evidence_links,
            verification_provenance=provenance,
            verified=True,
        )
        entity = CanonicalSourceEntity(
            source_key=seed.source_key,
            primary_identifier=seed.primary_identifier,
            identifiers=identifiers,
            identifier_revisions=revisions,
            titles=titles,
            authors=authors,
            aliases=aliases,
            evidence_links=evidence_links,
            verification_provenance=provenance,
            verified=True,
        )
        entities.append(entity)
        for reference in [entity.source_key, *entity.identifiers, *aliases]:
            reference_to_keys[reference].add(entity.source_key)
    unique_decisions = {_canonical_json(decision): decision for decision in decisions}
    return (
        sorted(entities, key=lambda item: item.source_key),
        dict(reference_to_keys),
        list(unique_decisions.values()),
    )


def _explicit_result_source_keys(
    records: Sequence[_VerifiedWorkerSourceRecord],
    *,
    typed_results: Sequence[ScientificResult],
    reference_to_keys: Mapping[str, set[str]],
) -> tuple[dict[str, set[str]], dict[str, list[str]], list[str]]:
    by_local_key = {result.local_key: result.local_key for result in typed_results}
    by_statement: dict[str, list[str]] = defaultdict(list)
    for result in typed_results:
        by_statement[normalize_exact_statement(result.exact_statement)].append(result.local_key)
    result_sources: dict[str, set[str]] = defaultdict(set)
    evidence_by_source: dict[str, list[str]] = defaultdict(list)
    issues: list[str] = []
    for record in records:
        for claim, source_references in record.evidence_claims:
            resolved_keys: set[str] = set()
            for reference in source_references:
                source_keys = reference_to_keys.get(reference)
                if not source_keys:
                    issues.append(
                        f"verified evidence claim references unknown or unverified source "
                        f"{reference!r}"
                    )
                    continue
                resolved_keys.update(source_keys)
                for source_key in source_keys:
                    evidence_by_source[source_key].append(claim)
            if not resolved_keys:
                continue
            result_keys: list[str] = []
            if claim in by_local_key:
                result_keys = [claim]
            elif claim.startswith("result:") and claim.removeprefix("result:") in by_local_key:
                result_keys = [claim.removeprefix("result:")]
            else:
                result_keys = by_statement.get(normalize_exact_statement(claim), [])
            for result_key in result_keys:
                result_sources[result_key].update(resolved_keys)
    return result_sources, evidence_by_source, list(dict.fromkeys(issues))


def _merge_compatible_source_identities(
    entities: Sequence[CanonicalSourceEntity],
) -> CanonicalSourceEntity:
    """Merge exact-identifier evidence while recomputing precedence.

    This differs from ``merge_source_entities`` because a later verified record
    may upgrade an arXiv-keyed entity to a DOI-keyed entity.  Conflicting
    same-scheme identities are never reconciled automatically.
    """

    if not entities:
        raise GraphValidationError("cannot merge an empty source identity set")
    for index, left in enumerate(entities):
        for right in entities[index + 1 :]:
            conflicts = conflicting_stable_source_identifiers(
                left.identifiers,
                right.identifiers,
            )
            if conflicts:
                schemes = ", ".join(sorted(conflicts))
                raise GraphValidationError(
                    f"source identity upgrade has conflicting {schemes} identifiers"
                )
    identifiers = sorted({item for entity in entities for item in entity.identifiers})
    revisions = sorted({item for entity in entities for item in entity.identifier_revisions})
    titles = sorted({item for entity in entities for item in entity.titles}, key=str.casefold)
    authors = sorted({item for entity in entities for item in entity.authors}, key=str.casefold)
    aliases = sorted({item for entity in entities for item in entity.aliases}, key=str.casefold)
    evidence_links = sorted({item for entity in entities for item in entity.evidence_links})
    provenance = sorted({item for entity in entities for item in entity.verification_provenance})
    seed = make_source_entity(
        title=titles[0],
        identifiers=identifiers,
        authors=authors,
        source_alias=aliases[0] if aliases else None,
        evidence_links=evidence_links,
        verification_provenance=provenance,
        verified=True,
    )
    return CanonicalSourceEntity(
        source_key=seed.source_key,
        primary_identifier=seed.primary_identifier,
        identifiers=identifiers,
        identifier_revisions=revisions,
        titles=titles,
        authors=authors,
        aliases=aliases,
        evidence_links=evidence_links,
        verification_provenance=provenance,
        verified=True,
    )


def _superseded_source_alias(
    node: GraphNode,
    *,
    canonical_source_id: str,
    canonical_source_key: str,
    run_id: str,
    now: datetime,
) -> GraphNode:
    alias = node.model_copy(deep=True)
    alias.epistemic_status = EpistemicStatus.STALE
    alias.workflow_status = WorkflowStatus.SUPERSEDED
    alias.last_modified_run = run_id
    alias.updated_at = now
    alias.author_role = "canonical-source-migrator"
    alias.invalidation_reasons = list(
        dict.fromkeys([*alias.invalidation_reasons, "canonical_source_identity_merged"])
    )
    alias.tags = list(dict.fromkeys([*alias.tags, "matek/source-alias"]))
    alias.metadata["matek_canonical_source_node_id"] = canonical_source_id
    alias.metadata["matek_canonical_source_key"] = canonical_source_key
    return alias


def _verified_worker_source_nodes(
    raw_ledger: object,
    *,
    typed_results: Sequence[ScientificResult],
    existing_nodes: Mapping[str, GraphNode],
    problem_id: str,
    run_id: str,
    source_artifact: str,
    now: datetime,
) -> tuple[
    list[GraphNode],
    dict[str, set[str]],
    list[str],
    list[dict[str, object]],
]:
    records, decisions = _verified_worker_source_records(
        raw_ledger,
        source_artifact=source_artifact,
    )
    decisions = [
        {
            **decision,
            "candidate_node_ids": _source_candidate_node_ids(
                existing_nodes.values(),
                problem_id=problem_id,
                identifiers=cast(list[str], decision["doi_identifiers"]),
            ),
        }
        if not decision["candidate_node_ids"]
        else decision
        for decision in decisions
    ]
    entities, reference_to_keys, grouping_decisions = _group_verified_worker_sources(records)
    decisions.extend(grouping_decisions)
    result_source_keys, evidence_by_source, issues = _explicit_result_source_keys(
        records,
        typed_results=typed_results,
        reference_to_keys=reference_to_keys,
    )
    issues.extend(_source_identity_issue(decision) for decision in decisions)
    source_nodes: list[GraphNode] = []
    source_ids_by_key: dict[str, str] = {}
    run_node_id = _deterministic_id(NodeType.RUN, problem_id, run_id)
    for incoming in entities:
        incoming_key = incoming.source_key
        deterministic_source_id = _deterministic_id(
            NodeType.SOURCE,
            problem_id,
            incoming_key,
        )
        compatible_existing, blocked_direct, candidate_decisions = _compatible_existing_sources(
            existing_nodes=existing_nodes.values(),
            problem_id=problem_id,
            incoming=incoming,
            deterministic_source_id=deterministic_source_id,
            context="worker_existing_source_upgrade",
            require_verified_overlap=False,
        )
        decisions.extend(candidate_decisions)
        issues.extend(_source_identity_issue(decision) for decision in candidate_decisions)
        compatible_existing.sort(
            key=lambda item: (
                item[0].matek_id != deterministic_source_id,
                item[0].created_at,
                item[0].matek_id,
            )
        )
        existing = compatible_existing[0][0] if compatible_existing else None
        source_id = (
            existing.matek_id
            if existing is not None
            else (
                _deterministic_id(NodeType.SOURCE, problem_id, incoming_key, "doi-version")
                if blocked_direct
                else deterministic_source_id
            )
        )
        source_ids_by_key[incoming_key] = source_id
        if compatible_existing:
            incoming = _merge_compatible_source_identities(
                [*(entity for _, entity in compatible_existing), incoming]
            )
        superseded_ids = [
            candidate.matek_id
            for candidate, _ in compatible_existing
            if candidate.matek_id != source_id
        ]
        source_evidence = list(
            dict.fromkeys(
                [
                    *(
                        evidence
                        for candidate, _ in compatible_existing
                        for evidence in candidate.evidence
                    ),
                    *evidence_by_source.get(incoming_key, []),
                ]
            )
        )
        source_nodes.append(
            GraphNode(
                matek_id=source_id,
                node_type=NodeType.SOURCE,
                problem_id=problem_id,
                title=incoming.titles[0],
                epistemic_status=EpistemicStatus.AUDIT_PASSED,
                workflow_status=WorkflowStatus.COMPLETE,
                created_in_run=existing.created_in_run if existing is not None else run_id,
                last_modified_run=run_id,
                author_role="research-source-verifier",
                created_at=existing.created_at if existing is not None else now,
                updated_at=now,
                body=new_generated_body(
                    incoming.titles[0],
                    "## Stable identifiers\n\n"
                    + ("\n".join(f"- `{item}`" for item in incoming.identifiers) or "_None._")
                    + "\n\n## Identifier revisions\n\n"
                    + (
                        "\n".join(f"- `{item}`" for item in incoming.identifier_revisions)
                        or "_None._"
                    )
                    + "\n\n## Titles and aliases\n\n"
                    + "\n".join(
                        [
                            *(f"- Title: {item}" for item in incoming.titles),
                            *(f"- Alias: `{item}`" for item in incoming.aliases),
                        ]
                    )
                    + "\n\n## Explicit evidence claims\n\n"
                    + (
                        "\n".join(f"- {item}" for item in source_evidence)
                        or "_None linked to a scientific result._"
                    )
                    + "\n\n## Verification\n\n"
                    + "\n".join(incoming.verification_provenance),
                ),
                tags=["matek/source", "matek/source-verified"],
                relations=_unique_edges(
                    [
                        *(existing.relations if existing is not None else []),
                        GraphEdge(
                            source_id=source_id,
                            relation=RelationType.CREATED_DURING,
                            target_id=run_node_id,
                        ),
                        *(
                            GraphEdge(
                                source_id=source_id,
                                relation=RelationType.SUPERSEDES,
                                target_id=superseded_id,
                            )
                            for superseded_id in superseded_ids
                        ),
                    ]
                ),
                source_artifacts=list(
                    dict.fromkeys(
                        [
                            *(
                                artifact
                                for candidate, _ in compatible_existing
                                for artifact in candidate.source_artifacts
                            ),
                            source_artifact,
                        ]
                    )
                ),
                evidence=source_evidence,
                metadata={
                    "matek_source_id": incoming.source_key,
                    "matek_primary_identifier": incoming.primary_identifier,
                    "matek_identifiers": incoming.identifiers,
                    "matek_identifier_revisions": incoming.identifier_revisions,
                    "matek_source_aliases": incoming.aliases,
                    "matek_source_titles": incoming.titles,
                    "matek_source_authors": incoming.authors,
                    "matek_evidence_links": incoming.evidence_links,
                    "matek_verification_provenance": incoming.verification_provenance,
                    "matek_source_evidence_claims": source_evidence,
                    "matek_verified": True,
                },
            )
        )
        source_nodes.extend(
            _superseded_source_alias(
                candidate,
                canonical_source_id=source_id,
                canonical_source_key=incoming.source_key,
                run_id=run_id,
                now=now,
            )
            for candidate, _ in compatible_existing
            if candidate.matek_id != source_id
        )
    result_source_ids = {
        result_key: {
            source_ids_by_key[source_key]
            for source_key in source_keys
            if source_key in source_ids_by_key
        }
        for result_key, source_keys in result_source_keys.items()
    }
    unique_decisions = {_canonical_json(decision): decision for decision in decisions}
    return (
        source_nodes,
        result_source_ids,
        list(dict.fromkeys(issues)),
        list(unique_decisions.values()),
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _generated_heading_value(body: str, heading: str) -> str | None:
    generated = generated_section(body)
    pattern = re.compile(rf"(?ms)^##\s+{re.escape(heading)}\s*$\n+(.*?)(?=^##\s+|\Z)")
    match = pattern.search(generated)
    return match.group(1).strip() if match is not None else None


def _replace_generated_heading_value(body: str, title: str, heading: str, value: str) -> str:
    generated = generated_section(body)
    pattern = re.compile(rf"(?ms)(^##\s+{re.escape(heading)}\s*$\n+).*?(?=^##\s+|\Z)")
    replacement = rf"\g<1>{value.strip()}\n\n"
    updated, count = pattern.subn(replacement, generated, count=1)
    if count == 0:
        updated = generated.rstrip() + f"\n\n## {heading}\n\n{value.strip()}"
    return replace_generated_section(body, title, updated)


def _mixed_doi_source_repair(
    node: GraphNode,
    run_id: str,
    timestamp: datetime,
) -> tuple[GraphNode, GraphHygieneAction] | None:
    """Normalize and mark a historical source that contains distinct DOI versions."""

    raw_identifiers = _metadata_text_list(node, "matek_identifiers")
    raw_revisions = _metadata_text_list(node, "matek_identifier_revisions")
    identifiers, revisions = canonical_source_identifiers([*raw_identifiers, *raw_revisions])
    dois = [identifier for identifier in identifiers if identifier.startswith("doi:")]
    if len(dois) <= 1:
        return None
    primary_value = node.metadata.get("matek_primary_identifier")
    primary_candidates, _ = canonical_source_identifiers(
        [primary_value] if isinstance(primary_value, str) else []
    )
    primary = next(
        (identifier for identifier in primary_candidates if identifier in identifiers),
        dois[0],
    )
    decision = _source_identity_decision(
        context="graph_doctor_historical_source",
        identifiers=identifiers,
        aliases=_metadata_text_list(node, "matek_source_aliases"),
        candidate_node_ids=[node.matek_id],
    )
    raw_identity_decision = node.metadata.get("matek_source_identity_decision")
    normalized_identity_decision: object = raw_identity_decision
    if isinstance(raw_identity_decision, str):
        try:
            normalized_identity_decision = json.loads(raw_identity_decision)
        except json.JSONDecodeError:
            pass
    before: dict[str, object] = {
        "primary_identifier": primary_value,
        "identifiers": raw_identifiers,
        "identifier_revisions": raw_revisions,
        "identity_decision": normalized_identity_decision,
    }
    after: dict[str, object] = {
        "primary_identifier": primary,
        "identifiers": identifiers,
        "identifier_revisions": revisions,
        "identity_decision": decision,
    }
    if before == after:
        return None

    repaired = node.model_copy(deep=True)
    repaired.metadata["matek_primary_identifier"] = primary
    repaired.metadata["matek_identifiers"] = identifiers
    repaired.metadata["matek_identifier_revisions"] = revisions
    repaired.metadata["matek_source_identity_decision"] = _canonical_json(decision)
    repaired.body = _replace_generated_heading_value(
        repaired.body,
        repaired.title,
        "Stable identifiers",
        "\n".join(f"- `{identifier}`" for identifier in identifiers),
    )
    repaired.body = _replace_generated_heading_value(
        repaired.body,
        repaired.title,
        "Identifier revisions",
        "\n".join(f"- `{revision}`" for revision in revisions) or "_None._",
    )
    repaired.body = _replace_generated_heading_value(
        repaired.body,
        repaired.title,
        "Identity review",
        (
            "This historical record contains multiple DOI publication versions. "
            "It is retained without merging or deleting evidence; new ingestion preserves "
            "one canonical source node per DOI."
        ),
    )
    repaired.last_modified_run = run_id
    repaired.author_role = "matek-graph-hygiene"
    repaired.updated_at = timestamp
    _source_entity_from_node(repaired)
    warning = _source_identity_issue(decision)
    return repaired, GraphHygieneAction(
        rule="multiple_doi_versions",
        node_id=node.matek_id,
        before=before,
        after=after,
        warning=warning,
        timestamp=timestamp,
    )


def _source_primary_identifier_repair(
    node: GraphNode,
    run_id: str,
    timestamp: datetime,
) -> tuple[GraphNode, GraphHygieneAction] | None:
    """Return the one whitelisted source-metadata repair, if needed."""

    raw_identifiers = _metadata_text_list(node, "matek_identifiers")
    raw_revisions = _metadata_text_list(node, "matek_identifier_revisions")
    identifiers, revisions = canonical_source_identifiers([*raw_identifiers, *raw_revisions])
    primary_value = node.metadata.get("matek_primary_identifier")
    raw_primary = str(primary_value).strip() if isinstance(primary_value, str) else None
    primary_candidates, _ = canonical_source_identifiers([raw_primary] if raw_primary else [])
    normalized_primary = primary_candidates[0] if len(primary_candidates) == 1 else None
    if normalized_primary not in identifiers:
        normalized_primary = next(
            (identifier for identifier in identifiers if identifier.startswith("doi:")),
            identifiers[0] if identifiers else None,
        )
    verified = bool(node.metadata.get("matek_verified", False))
    repaired_verified = verified and bool(identifiers)
    before: dict[str, object] = {
        "primary_identifier": primary_value,
        "identifiers": raw_identifiers,
        "identifier_revisions": raw_revisions,
        "verified": verified,
    }
    after: dict[str, object] = {
        "primary_identifier": normalized_primary,
        "identifiers": identifiers,
        "identifier_revisions": revisions,
        "verified": repaired_verified,
    }
    if before == after:
        return None

    repaired = node.model_copy(deep=True)
    repaired.metadata["matek_primary_identifier"] = normalized_primary
    repaired.metadata["matek_identifiers"] = identifiers
    repaired.metadata["matek_identifier_revisions"] = revisions
    repaired.metadata["matek_verified"] = repaired_verified
    repaired.body = _replace_generated_heading_value(
        repaired.body,
        repaired.title,
        "Stable identifiers",
        "\n".join(f"- `{identifier}`" for identifier in identifiers) or "_None._",
    )
    repaired.body = _replace_generated_heading_value(
        repaired.body,
        repaired.title,
        "Identifier revisions",
        "\n".join(f"- `{revision}`" for revision in revisions) or "_None._",
    )
    warning: str | None = None
    if not identifiers:
        warning = (
            f"Source {node.matek_id} has no stable identifier; retained as unverified metadata."
        )
        repaired.epistemic_status = EpistemicStatus.OPEN
        repaired.workflow_status = WorkflowStatus.ACTIVE
        repaired.tags = [tag for tag in repaired.tags if tag != "matek/source-verified"]
        repaired.tags = list(dict.fromkeys([*repaired.tags, "matek/source-open"]))
        verification = _generated_heading_value(repaired.body, "Verification") or ""
        downgrade = "Not independently verified: no stable identifier is currently recorded."
        if downgrade not in verification:
            repaired.body = _replace_generated_heading_value(
                repaired.body,
                repaired.title,
                "Verification",
                "\n\n".join(item for item in (verification, downgrade) if item),
            )
    repaired.last_modified_run = run_id
    repaired.author_role = "matek-graph-hygiene"
    repaired.updated_at = timestamp
    # Validate only the affected source. Mathematical claims and targets are not touched.
    _source_entity_from_node(repaired)
    return repaired, GraphHygieneAction(
        rule="primary_identifier_in_identifiers",
        node_id=node.matek_id,
        before=before,
        after=after,
        warning=warning,
        timestamp=timestamp,
    )


_SOURCE_HYGIENE_RULES: dict[
    str,
    Callable[[GraphNode, str, datetime], tuple[GraphNode, GraphHygieneAction] | None],
] = {
    "multiple_doi_versions": _mixed_doi_source_repair,
    "primary_identifier_in_identifiers": _source_primary_identifier_repair,
}


def _deterministic_id(node_type: NodeType, *parts: str) -> str:
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest().upper()[:20]
    return f"{NODE_ID_PREFIXES[node_type]}-{digest}"


def _new_id(node_type: NodeType) -> str:
    return f"{NODE_ID_PREFIXES[node_type]}-{secrets.token_hex(10).upper()}"


def _descriptive_id(
    node_type: NodeType,
    description: str,
    taken: Collection[str],
) -> str:
    """Mint one descriptive one-liner node ID, deduped against ``taken`` IDs."""

    word = NODE_ID_WORDS[node_type]
    candidate = descriptive_node_id(word, description)
    taken_casefolds = {node_id.casefold() for node_id in taken}
    return dedupe_descriptive_id(candidate, taken_casefolds)


def _slug(value: str) -> str:
    ascii_value = (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", errors="ignore")
        .decode("ascii")
        .casefold()
    )
    return _SLUG_UNSAFE.sub("-", ascii_value).strip("-")[:56] or "note"


def _note_filename(title: str) -> str:
    """Keep a readable Obsidian node name while remaining portable."""

    normalized = unicodedata.normalize("NFC", title)
    cleaned = _NOTE_FILENAME_UNSAFE.sub("-", normalized)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .") or "Untitled"
    if cleaned.casefold() in _WINDOWS_RESERVED_FILENAMES:
        cleaned = f"Note {cleaned}"
    while len(cleaned.encode("utf-8")) > 220:
        cleaned = cleaned[:-1].rstrip(" .")
    return cleaned or "Untitled"


def normalize_graph_name(value: str) -> str:
    """Return one portable graph-directory name from a problem stem or user label."""

    normalized = _slug(value)[:64].rstrip("-")
    if not _GRAPH_NAME.fullmatch(normalized):  # pragma: no cover - guarded by _slug
        raise KnowledgeGraphError(f"invalid knowledge-graph name: {value!r}")
    return normalized


def problem_graph_name(source_path: Path) -> str:
    """Derive the default graph identity from the problem filename without its extension."""

    return normalize_graph_name(source_path.stem)


def list_graph_names(project_root: Path) -> list[str]:
    """List initialized named graphs without opening or mutating any of them."""

    root = project_root.expanduser().resolve(strict=True)
    collection = ensure_path_confined(root, root / GRAPH_COLLECTION_RELATIVE)
    if not collection.is_dir():
        return []
    names: list[str] = []
    for candidate in sorted(collection.iterdir(), key=lambda item: item.name):
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        if not _GRAPH_NAME.fullmatch(candidate.name):
            continue
        if (candidate / "graph-state.json").is_file():
            names.append(candidate.name)
    return names


def _revision(number: int, node_hashes: Mapping[str, str]) -> str:
    digest = hashlib.sha256(_canonical_json(dict(sorted(node_hashes.items()))).encode()).hexdigest()
    return f"{number:08d}-{digest[:16]}"


def _node_summary(node: GraphNode) -> GraphNodeSummary:
    return GraphNodeSummary(
        matek_id=node.matek_id,
        node_type=node.node_type,
        title=node.title,
        epistemic_status=node.epistemic_status,
        workflow_status=node.workflow_status,
        path=node.path or "",
        statement_version=node.statement_version,
        invalidation_reasons=node.invalidation_reasons,
    )


def _context_node_is_live(node: GraphNode) -> bool:
    return (
        not node.tombstone
        and not node.invalidation_reasons
        and node.epistemic_status
        not in {
            EpistemicStatus.REFUTED,
            EpistemicStatus.INCONSISTENT,
            EpistemicStatus.STALE,
        }
        and node.workflow_status
        not in {
            WorkflowStatus.BLOCKED,
            WorkflowStatus.ABANDONED,
            WorkflowStatus.SUPERSEDED,
        }
    )


def _markdown_trusted_claim_ids(nodes: Sequence[GraphNode]) -> set[str]:
    """Compute audited mathematical trust directly from parsed Markdown nodes."""

    by_id = {node.matek_id: node for node in nodes}
    trusted = {
        node.matek_id
        for node in nodes
        if node.node_type in {NodeType.CLAIM, NodeType.DEFINITION}
        and _context_node_is_live(node)
        and (
            node.epistemic_status in {EpistemicStatus.AUDIT_PASSED, EpistemicStatus.LEAN_VERIFIED}
            or (
                node.node_type is NodeType.DEFINITION
                and canonical_admitted_definition_scope(node) is not None
            )
        )
    }
    changed = True
    while changed:
        changed = False
        for derivation in nodes:
            if (
                derivation.node_type not in {NodeType.PROOF, NodeType.DERIVATION}
                or not _context_node_is_live(derivation)
                or derivation.epistemic_status
                not in {EpistemicStatus.AUDIT_PASSED, EpistemicStatus.LEAN_VERIFIED}
            ):
                continue
            conclusion = derivation.metadata.get("matek_conclusion_claim_id")
            if not isinstance(conclusion, str):
                proved = [
                    edge.target_id
                    for edge in derivation.relations
                    if edge.relation is RelationType.PROVES
                ]
                conclusion = proved[0] if len(proved) == 1 else None
            if not isinstance(conclusion, str) or conclusion not in by_id:
                continue
            dependencies = [
                by_id.get(edge.target_id)
                for edge in derivation.relations
                if edge.relation is RelationType.DEPENDS_ON
            ]
            if any(dependency is None for dependency in dependencies):
                continue
            if any(
                dependency.node_type in {NodeType.CLAIM, NodeType.DEFINITION}
                and dependency.matek_id not in trusted
                for dependency in dependencies
                if dependency is not None
            ):
                continue
            if any(
                dependency.node_type is NodeType.OBLIGATION
                and (
                    not _context_node_is_live(dependency)
                    or (
                        dependency.workflow_status is not WorkflowStatus.COMPLETE
                        and dependency.epistemic_status
                        not in {EpistemicStatus.AUDIT_PASSED, EpistemicStatus.LEAN_VERIFIED}
                    )
                )
                for dependency in dependencies
                if dependency is not None
            ):
                continue
            if conclusion not in trusted:
                trusted.add(conclusion)
                changed = True
    return trusted


def _metadata_string_list(node: GraphNode, key: str) -> list[str]:
    value = node.metadata.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return []
    return list(value)


def _verified_source_for_context(node: GraphNode) -> bool:
    if (
        node.node_type is not NodeType.SOURCE
        or not _context_node_is_live(node)
        or node.epistemic_status is not EpistemicStatus.AUDIT_PASSED
        or node.workflow_status is not WorkflowStatus.COMPLETE
        or node.author_role not in {"prompt-source-verifier", "research-source-verifier"}
        or node.metadata.get("matek_verified") is not True
    ):
        return False
    try:
        return _source_entity_from_node(node).verified
    except GraphValidationError:
        return False


def _verified_formalization_for_context(
    node: GraphNode,
    *,
    nodes: Mapping[str, GraphNode],
    trusted_claim_node_ids: set[str],
) -> bool:
    formalized_ids = [
        edge.target_id for edge in node.relations if edge.relation is RelationType.FORMALIZES
    ]
    if len(formalized_ids) != 1:
        return False
    claim_id = formalized_ids[0]
    claim = nodes.get(claim_id)
    return (
        node.node_type is NodeType.FORMALIZATION
        and _context_node_is_live(node)
        and node.epistemic_status is EpistemicStatus.LEAN_VERIFIED
        and node.workflow_status is WorkflowStatus.COMPLETE
        and node.author_role == "deterministic-lean-verifier"
        and node.metadata.get("matek_deterministic_verification_passed") is True
        and node.metadata.get("matek_claim_id") == claim_id
        and claim_id in trusted_claim_node_ids
        and claim is not None
        and node.metadata.get("matek_statement_version") == claim.statement_version
    )


def _unique_edges(edges: Iterable[GraphEdge]) -> list[GraphEdge]:
    result: list[GraphEdge] = []
    seen: set[tuple[str, str, str]] = set()
    for edge in edges:
        key = (edge.source_id, edge.relation.value, edge.target_id)
        if key not in seen:
            seen.add(key)
            result.append(edge)
    return sorted(result, key=lambda item: (item.source_id, item.relation.value, item.target_id))


class KnowledgeGraph:
    """One named project-scoped graph supporting related stable problem nodes.

    MATEK's existing security contract permits automatic writes only beneath
    ``.matek/``.  Each Obsidian vault therefore lives at
    ``.matek/knowledge/<graph-name>/``.  A normal run derives ``graph-name`` from
    the problem filename; an explicit selection may attach related problems to an
    existing graph without merging unrelated default vaults.
    """

    def __init__(
        self,
        project_root: Path,
        graph_name: str,
        *,
        clock: Callable[[], datetime] | None = None,
        maximum_context_nodes: int = 48,
        maximum_context_characters: int = 60_000,
        snapshot_checkpoint_interval: int = DEFAULT_SNAPSHOT_CHECKPOINT_INTERVAL,
    ) -> None:
        root = project_root.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise KnowledgeGraphError(f"project root is not a directory: {project_root}")
        self.project_root = root
        self.graph_name = normalize_graph_name(graph_name)
        self.collection_root = ensure_path_confined(root, root / GRAPH_COLLECTION_RELATIVE)
        requested_root = root / GRAPH_COLLECTION_RELATIVE / self.graph_name
        if requested_root.is_symlink():
            raise KnowledgeGraphError(f"refusing symlinked knowledge graph: {requested_root}")
        self.graph_root = ensure_path_confined(root, requested_root)
        self.vault_root = self.graph_root
        self.state_path = ensure_path_confined(root, self.graph_root / "graph-state.json")
        self.schema_path = ensure_path_confined(root, self.graph_root / "graph-schema.json")
        self.index_path = ensure_path_confined(root, self.graph_root / "graph-index.sqlite")
        self.pending_path = ensure_path_confined(root, self.graph_root / "graph-pending.json")
        self.target_registry_path = ensure_path_confined(
            root, self.graph_root / "target-registry.json"
        )
        # Retained only so an invocation of the removed legacy migration method fails
        # deterministically before writing. New graph initialization never creates it.
        self.ledgers_root = ensure_path_confined(root, self.graph_root / "ledgers")
        requested_snapshots_root = self.graph_root / "snapshots"
        if requested_snapshots_root.is_symlink():
            raise KnowledgeGraphError(
                f"refusing symlinked snapshot store: {requested_snapshots_root}"
            )
        self.snapshots_root = ensure_path_confined(root, requested_snapshots_root)
        self.snapshot_store = SnapshotStore(
            self.snapshots_root,
            checkpoint_interval=snapshot_checkpoint_interval,
        )
        self.locks_root = ensure_path_confined(root, self.graph_root / "locks")
        self.lock_path = ensure_path_confined(root, self.locks_root / "graph.lock")
        self._clock = clock or _utc_now
        self.maximum_context_nodes = maximum_context_nodes
        self.maximum_context_characters = maximum_context_characters
        if maximum_context_nodes < 4 or maximum_context_characters < 1_000:
            raise ValueError("graph context limits are too small for a useful bounded slice")

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise KnowledgeGraphError("graph clock must return a datetime")
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.collection_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.graph_root.mkdir(mode=0o700, exist_ok=True)
        self.locks_root.mkdir(mode=0o700, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.lock_path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise KnowledgeGraphError(f"graph lock is not a regular file: {self.lock_path}")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    @property
    def initialized(self) -> bool:
        return self.state_path.is_file() and self.vault_root.is_dir()

    def _ensure_layout(self) -> None:
        self.collection_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.graph_root.mkdir(mode=0o700, exist_ok=True)
        self.snapshots_root.mkdir(mode=0o700, exist_ok=True)
        self.locks_root.mkdir(mode=0o700, exist_ok=True)
        for relative in GRAPH_DIRECTORIES:
            ensure_path_confined(self.vault_root, self.vault_root / relative).mkdir(
                parents=True, exist_ok=True
            )
        obsidian = ensure_path_confined(self.vault_root, self.vault_root / ".obsidian")
        obsidian.mkdir(mode=0o700, exist_ok=True)
        app_config = ensure_path_confined(self.vault_root, obsidian / "app.json")
        if not app_config.exists():
            atomic_write_json(app_config, {}, confinement_root=self.vault_root)

    def _write_schema(self) -> None:
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "schema_version": GRAPH_SCHEMA_VERSION,
            "description": "MATEK Markdown knowledge graph node and patch schemas",
            "node": GraphNode.model_json_schema(),
            "patch": GraphPatch.model_json_schema(),
            "relation_types": [item.value for item in RelationType],
            "node_types": [item.value for item in NodeType],
        }
        atomic_write_json(self.schema_path, schema, confinement_root=self.graph_root)

    def initialize(self) -> GraphState:
        """Create an empty portable vault and derived index idempotently."""

        with self._locked():
            self._ensure_layout()
            self._write_schema()
            if self.state_path.is_file():
                self._recover_pending_unlocked()
                state = self._load_state_unlocked()
            else:
                now = self._now()
                empty_revision = _revision(0, {})
                state = GraphState(
                    graph_name=self.graph_name,
                    revision=empty_revision,
                    created_at=now,
                    updated_at=now,
                )
                atomic_write_json(self.state_path, state, confinement_root=self.graph_root)
                self._write_snapshot_unlocked(state, [])
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            state, nodes = self._migrate_legacy_paths_unlocked(state, nodes)
            self._refresh_derived_views_unlocked(state, nodes)
            return state

    def _load_state_unlocked(self) -> GraphState:
        if not self.state_path.is_file():
            raise GraphNotInitializedError(
                "knowledge graph is not initialized; run "
                f"'matek graph init {self.graph_name}' in {self.project_root}"
            )
        try:
            state = GraphState.model_validate_json(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValidationError) as exc:
            raise GraphValidationError(f"graph state is invalid: {exc}") from exc
        if not _REVISION.fullmatch(state.revision):
            raise GraphValidationError("graph state contains an invalid revision identifier")
        if state.graph_name != self.graph_name:
            raise GraphValidationError(
                f"graph state is named {state.graph_name!r}, not {self.graph_name!r}"
            )
        return state

    def _unlink_graph_file_unlocked(self, relative: str) -> None:
        requested = self.vault_root / relative
        target = ensure_path_confined(self.vault_root, requested)
        if target != requested.absolute():
            raise GraphValidationError(
                f"refusing graph transaction removal through a symlink: {relative}"
            )
        try:
            status = requested.stat(follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(status.st_mode):
            raise GraphValidationError(
                f"graph transaction removal is not a regular file: {relative}"
            )
        target.unlink()

    def load_state(self) -> GraphState:
        with self._locked():
            self._recover_pending_unlocked()
            return self._load_state_unlocked()

    def _recover_pending_unlocked(self) -> None:
        if not self.pending_path.is_file():
            return
        try:
            pending = json.loads(self.pending_path.read_text(encoding="utf-8"))
            writes = pending["writes"]
            removals = pending.get("removals", [])
            raw_state = pending["state_after"]
            if (
                not isinstance(writes, list)
                or not isinstance(removals, list)
                or any(not isinstance(item, str) for item in removals)
                or not isinstance(raw_state, dict)
            ):
                raise TypeError("transaction fields have invalid types")
            state_after = GraphState.model_validate(raw_state)
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValidationError,
        ) as exc:
            raise GraphValidationError(f"pending graph transaction is invalid: {exc}") from exc
        for write in writes:
            if not isinstance(write, dict):
                raise GraphValidationError("pending graph transaction contains an invalid write")
            relative = write.get("path")
            contents = write.get("contents")
            digest = write.get("sha256")
            if not all(isinstance(item, str) for item in (relative, contents, digest)):
                raise GraphValidationError("pending graph transaction write is incomplete")
            target = ensure_path_confined(self.vault_root, self.vault_root / cast(str, relative))
            if sha256_text(cast(str, contents)) != digest:
                raise GraphValidationError("pending graph transaction content hash is invalid")
            if not target.is_file() or sha256_file(target) != digest:
                atomic_write_text(
                    target, cast(str, contents), confinement_root=self.vault_root, mode=0o600
                )
        written_paths = {cast(str, write["path"]) for write in writes}
        for relative in removals:
            if relative in written_paths:
                raise GraphValidationError(
                    "pending graph transaction removes a path that it also writes"
                )
            self._unlink_graph_file_unlocked(relative)
        atomic_write_json(self.state_path, state_after, confinement_root=self.graph_root)
        nodes = self._load_nodes_unlocked(include_human_notes=True)
        self._write_snapshot_unlocked(state_after, nodes)
        self._refresh_derived_views_unlocked(state_after, nodes)
        self.pending_path.unlink(missing_ok=True)

    def _load_nodes_unlocked(self, *, include_human_notes: bool) -> list[GraphNode]:
        if not self.vault_root.is_dir():
            raise GraphNotInitializedError("knowledge graph vault is missing")
        nodes: list[GraphNode] = []
        seen: set[str] = set()
        problem_ids: list[str] = []
        candidates = sorted(self.vault_root.rglob("*.md"))
        for path in candidates:
            relative = path.relative_to(self.vault_root).as_posix()
            if relative == "Home.md" or relative.startswith("Dashboards/"):
                continue
            try:
                prefix = path.read_text(encoding="utf-8")[:2048]
            except (OSError, UnicodeError) as exc:
                raise GraphMarkdownError(f"cannot read graph note {path}: {exc}") from exc
            if "matek_id:" not in prefix:
                if include_human_notes:
                    node_id = _deterministic_id(NodeType.HUMAN_NOTE, relative)
                    stat_result = path.stat()
                    timestamp = datetime.fromtimestamp(stat_result.st_mtime, tz=UTC)
                    problem_id = problem_ids[0] if problem_ids else node_id
                    text = path.read_text(encoding="utf-8")
                    nodes.append(
                        GraphNode(
                            matek_id=node_id,
                            node_type=NodeType.HUMAN_NOTE,
                            problem_id=problem_id,
                            title=path.stem,
                            created_in_run="HUMAN",
                            last_modified_run="HUMAN",
                            author_role="human",
                            created_at=timestamp,
                            updated_at=timestamp,
                            body=text,
                            tags=["matek/human-note"],
                            path=relative,
                            content_hash=sha256_file(path),
                        )
                    )
                continue
            node = parse_node_note(path, relative_path=relative)
            if node.matek_id in seen:
                raise GraphValidationError(f"duplicate graph node ID: {node.matek_id}")
            seen.add(node.matek_id)
            if node.node_type is NodeType.PROBLEM:
                problem_ids.append(node.matek_id)
            nodes.append(node)
        return nodes

    def load_nodes(self, *, include_human_notes: bool = True) -> list[GraphNode]:
        with self._locked():
            self._recover_pending_unlocked()
            self._load_state_unlocked()
            return self._load_nodes_unlocked(include_human_notes=include_human_notes)

    def apply_legacy_migration(
        self,
        report: LegacyMigrationReport,
    ) -> LegacyMigrationApplicationRecord:
        """Apply one externally reviewed, integrity-bound legacy migration plan.

        The source revision and complete problem-local archive digest are checked while
        holding the graph lock.  The graph commit is one normal recoverable transaction;
        old notes and all prior snapshots remain in place.  A digest-protected application
        record makes a retry observable and idempotent even if the first process stopped
        after committing the graph but before writing that record.
        """

        plan_sha256 = migration_report_sha256(report)
        operation_id = f"legacy-migration:{plan_sha256}"
        if report.source_graph_name is None:
            raise LegacyMigrationError(
                "legacy migration plan lacks source_graph_name and cannot be safely applied"
            )
        if report.source_graph_name != self.graph_name:
            raise LegacyMigrationError(
                f"legacy migration plan belongs to graph {report.source_graph_name!r}, "
                f"not {self.graph_name!r}"
            )
        record_path = ensure_path_confined(
            self.graph_root,
            self.ledgers_root / "migrations" / f"{plan_sha256}.application.json",
        )

        proof_ids = sorted(
            {
                *(item.proof_node_id for item in report.proof_attempt_reclassifications),
                *(item.derivation.proof_attempt_id for item in report.derivation_proposals),
            }
        )
        proof_attempt_ids = [
            _deterministic_id(
                NodeType.PROOF_ATTEMPT,
                "legacy-backfill",
                plan_sha256,
                proof_id,
            )
            for proof_id in proof_ids
        ]
        derivation_ids = sorted(item.proposal_id for item in report.derivation_proposals)
        recorded_alias_ids = sorted(
            alias_id
            for group in report.claim_alias_groups
            if group.disposition == "ready_for_review"
            for alias_id in group.alias_ids
        )
        quarantine_ids = sorted(item.refutation_node_id for item in report.refutation_quarantines)
        retargeted_ids = sorted(
            item.refutation_node_id
            for item in report.refutation_quarantines
            if len(item.candidate_branch_target_ids) == 1
        )
        audit_task_ids = [
            _deterministic_id(
                NodeType.TASK,
                "legacy-audit-nomination",
                plan_sha256,
                item.claim_id,
                item.proof_node_id,
            )
            for item in report.audit_nominations
        ]
        updated_archive_ids = sorted(
            {
                *proof_ids,
                *quarantine_ids,
                *recorded_alias_ids,
                *(
                    group.canonical_candidate_id
                    for group in report.claim_alias_groups
                    if group.disposition == "ready_for_review"
                ),
            }
        )
        unapplied_issues = [
            *report.ambiguous_dependencies,
            *report.scope_conflicts,
            *report.review_notes,
        ]

        def application_record(
            result: GraphMergeResult,
            *,
            applied_at: datetime,
            status: Literal["applied", "already_applied"],
        ) -> LegacyMigrationApplicationRecord:
            return LegacyMigrationApplicationRecord(
                status=status,
                plan_sha256=plan_sha256,
                graph_name=self.graph_name,
                problem_id=report.problem_id,
                target_claim_id=report.target_claim_id,
                source_graph_revision=report.source_graph_revision,
                source_archive_sha256=report.source_archive_sha256,
                operation_id=operation_id,
                previous_revision=result.previous_revision,
                new_revision=result.new_revision,
                applied_at=applied_at,
                proof_attempt_node_ids=proof_attempt_ids,
                derivation_node_ids=derivation_ids,
                updated_archive_node_ids=updated_archive_ids,
                recorded_alias_ids=recorded_alias_ids,
                quarantined_refutation_node_ids=quarantine_ids,
                retargeted_refutation_node_ids=retargeted_ids,
                audit_task_node_ids=audit_task_ids,
                unapplied_issues=unapplied_issues,
            )

        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            existing_operation = state.processed_operations.get(operation_id)
            if record_path.exists():
                existing_record = load_legacy_migration_application(record_path)
                if (
                    existing_record.plan_sha256 != plan_sha256
                    or existing_record.graph_name != self.graph_name
                    or existing_record.operation_id != operation_id
                    or existing_operation is None
                    or existing_operation.new_revision != existing_record.new_revision
                ):
                    raise LegacyMigrationError(
                        "legacy migration application record does not match graph history"
                    )
                return existing_record.model_copy(update={"status": "already_applied"})
            if existing_operation is not None:
                change = next(
                    (item for item in state.changes if item.operation_id == operation_id),
                    None,
                )
                if change is None:
                    raise LegacyMigrationError(
                        "applied legacy migration is missing its graph change record"
                    )
                recovered = application_record(
                    existing_operation,
                    applied_at=change.timestamp,
                    status="applied",
                )
                write_legacy_migration_application(
                    record_path,
                    recovered,
                    confinement_root=self.ledgers_root,
                )
                return recovered.model_copy(update={"status": "already_applied"})

            if state.revision != report.source_graph_revision:
                raise LegacyMigrationError(
                    "legacy migration plan is stale: current graph revision "
                    f"{state.revision} differs from reviewed revision "
                    f"{report.source_graph_revision}"
                )
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            selected = [node for node in nodes if node.problem_id == report.problem_id]
            if len(selected) != report.source_node_count:
                raise LegacyMigrationError(
                    "legacy migration plan is stale: source node count changed"
                )
            current_archive_sha256 = legacy_archive_sha256(
                selected,
                problem_id=report.problem_id,
            )
            if current_archive_sha256 != report.source_archive_sha256:
                raise LegacyMigrationError(
                    "legacy migration plan is stale: source archive digest changed"
                )
            expected_report = plan_legacy_graph_backfill(
                nodes,
                graph_revision=state.revision,
                problem_id=report.problem_id,
                target_claim_id=report.target_claim_id,
                audit_nomination_limit=report.audit_nomination_limit,
                graph_name=self.graph_name,
            )
            if expected_report != report:
                raise LegacyMigrationError(
                    "legacy migration plan does not match the deterministic plan for its "
                    "integrity-bound source archive"
                )
            by_id = {node.matek_id: node.model_copy(deep=True) for node in nodes}
            target = by_id.get(report.target_claim_id)
            if (
                target is None
                or target.node_type is not NodeType.CLAIM
                or target.problem_id != report.problem_id
                or (
                    target.matek_id != self.main_claim_id(report.problem_id)
                    and "matek/main-target" not in target.tags
                )
            ):
                raise LegacyMigrationError(
                    "legacy migration target is not the graph's current main claim"
                )

            now = self._now()
            run_id = f"legacy-migration-{plan_sha256[:16]}"
            changed: set[str] = set()
            gap_by_proof = {
                item.proof_node_id: item.exact_gap
                for item in report.proof_attempt_reclassifications
            }
            proof_attempt_by_archive: dict[str, str] = {}
            for proof_id, attempt_id in zip(proof_ids, proof_attempt_ids, strict=True):
                proof = by_id.get(proof_id)
                if proof is None or proof.node_type is not NodeType.PROOF:
                    raise LegacyMigrationError(
                        f"reviewed legacy proof node is unavailable or mistyped: {proof_id}"
                    )
                if attempt_id in by_id:
                    raise LegacyMigrationError(
                        f"deterministic proof-attempt ID collides with archive node: {attempt_id}"
                    )
                proof_attempt_by_archive[proof_id] = attempt_id
                gap = gap_by_proof.get(proof_id)
                preserved = generated_section(proof.body).strip()
                attempt = GraphNode(
                    matek_id=attempt_id,
                    node_type=NodeType.PROOF_ATTEMPT,
                    problem_id=report.problem_id,
                    title=f"Legacy proof attempt {proof_id}",
                    epistemic_status=EpistemicStatus.CANDIDATE,
                    workflow_status=(
                        WorkflowStatus.BLOCKED if gap is not None else WorkflowStatus.COMPLETE
                    ),
                    created_in_run=run_id,
                    last_modified_run=run_id,
                    author_role="matek-legacy-migrator",
                    created_at=now,
                    updated_at=now,
                    body=new_generated_body(
                        f"Legacy proof attempt {proof_id}",
                        "## Archive source\n\n"
                        + proof_id
                        + "\n\n## Preserved proof content\n\n"
                        + (preserved or "_No generated proof body was recoverable._")
                        + "\n\n## Exact gap\n\n"
                        + (gap or "_None declared in the reviewed legacy plan._"),
                    ),
                    tags=["matek/proof-attempt", "matek/legacy-backfill"],
                    relations=[
                        GraphEdge(
                            source_id=attempt_id,
                            relation=RelationType.RELATED_TO,
                            target_id=proof_id,
                        ),
                        *(
                            GraphEdge(
                                source_id=attempt_id,
                                relation=RelationType.RELATED_TO,
                                target_id=edge.target_id,
                            )
                            for edge in proof.relations
                            if edge.relation is RelationType.PROVES
                        ),
                    ],
                    source_artifacts=list(proof.source_artifacts),
                    evidence=list(proof.evidence),
                    metadata={
                        "matek_legacy_archive_node_id": proof_id,
                        "matek_legacy_migration_plan_sha256": plan_sha256,
                        **({"matek_exact_gap": gap} if gap is not None else {}),
                    },
                )
                by_id[attempt_id] = attempt
                proof.metadata["matek_archive_only"] = True
                proof.metadata["matek_superseded_by"] = attempt_id
                proof.metadata["matek_legacy_migration_plan_sha256"] = plan_sha256
                proof.tags = list(
                    dict.fromkeys([*proof.tags, "matek/archive-only", "matek/legacy-proof"])
                )
                proof.workflow_status = WorkflowStatus.SUPERSEDED
                proof.last_modified_run = run_id
                proof.author_role = "matek-legacy-migrator"
                proof.updated_at = now
                changed.update({proof_id, attempt_id})

            proposal_by_proof: dict[str, str] = {}
            for proposal in report.derivation_proposals:
                derivation = proposal.derivation
                proof_attempt_id = proof_attempt_by_archive.get(derivation.proof_attempt_id)
                if proof_attempt_id is None:
                    raise LegacyMigrationError(
                        f"derivation {proposal.proposal_id} lacks a canonical proof attempt"
                    )
                conclusion = by_id.get(derivation.conclusion_claim_id)
                premises = [by_id.get(item) for item in derivation.premise_claim_ids]
                if (
                    conclusion is None
                    or conclusion.node_type is not NodeType.CLAIM
                    or any(
                        item is None or item.node_type is not NodeType.CLAIM for item in premises
                    )
                ):
                    raise LegacyMigrationError(
                        f"derivation {proposal.proposal_id} references a missing claim"
                    )
                current_target_version = logical_version(exact_statement(conclusion.body))
                current_premise_versions = {
                    item.matek_id: logical_version(exact_statement(item.body))
                    for item in premises
                    if item is not None
                }
                if (
                    current_target_version != derivation.exact_target_version
                    or current_premise_versions != derivation.premise_versions
                ):
                    raise LegacyMigrationError(
                        f"derivation {proposal.proposal_id} exact claim versions are stale"
                    )
                if proposal.proposal_id in by_id:
                    raise LegacyMigrationError(
                        f"proposed derivation ID collides with archive node: {proposal.proposal_id}"
                    )
                proposal_by_proof[derivation.proof_attempt_id] = proposal.proposal_id
                derivation_node = GraphNode(
                    matek_id=proposal.proposal_id,
                    node_type=NodeType.DERIVATION,
                    problem_id=report.problem_id,
                    title=f"Reviewed legacy derivation for {conclusion.title}",
                    epistemic_status=EpistemicStatus.CANDIDATE,
                    workflow_status=WorkflowStatus.QUEUED,
                    created_in_run=run_id,
                    last_modified_run=run_id,
                    author_role="matek-legacy-migrator",
                    created_at=now,
                    updated_at=now,
                    body=new_generated_body(
                        f"Reviewed legacy derivation for {conclusion.title}",
                        "## Exact conclusion\n\n"
                        + exact_statement(conclusion.body)
                        + "\n\n## Joint premises\n\n"
                        + (
                            "\n".join(f"- {item}" for item in derivation.premise_claim_ids)
                            or "_No prior premises declared._"
                        )
                        + "\n\n## Canonical proof attempt\n\n"
                        + proof_attempt_id
                        + "\n\n## Review status\n\n"
                        "Proposed only; fresh independent audit is required.",
                    ),
                    tags=["matek/derivation", "matek/proposed", "matek/legacy-backfill"],
                    relations=[
                        GraphEdge(
                            source_id=proposal.proposal_id,
                            relation=RelationType.PROVES,
                            target_id=derivation.conclusion_claim_id,
                        ),
                        *(
                            GraphEdge(
                                source_id=proposal.proposal_id,
                                relation=RelationType.DEPENDS_ON,
                                target_id=item,
                            )
                            for item in derivation.premise_claim_ids
                        ),
                        GraphEdge(
                            source_id=proposal.proposal_id,
                            relation=RelationType.RELATED_TO,
                            target_id=proof_attempt_id,
                        ),
                        *(
                            GraphEdge(
                                source_id=proposal.proposal_id,
                                relation=RelationType.RELATED_TO,
                                target_id=item,
                            )
                            for item in proposal.supporting_archive_node_ids
                        ),
                    ],
                    source_artifacts=[f"legacy-migration-plan:{plan_sha256}"],
                    metadata={
                        "matek_conclusion_claim_id": derivation.conclusion_claim_id,
                        "matek_premise_claim_ids": derivation.premise_claim_ids,
                        "matek_proof_attempt_id": proof_attempt_id,
                        "matek_exact_target_version": derivation.exact_target_version,
                        "matek_premise_versions": [
                            f"{item}={derivation.premise_versions[item]}"
                            for item in derivation.premise_claim_ids
                        ],
                        "matek_obligation_ids": [],
                        "matek_legacy_archive_proof_id": derivation.proof_attempt_id,
                        "matek_legacy_migration_plan_sha256": plan_sha256,
                    },
                )
                by_id[proposal.proposal_id] = derivation_node
                changed.add(proposal.proposal_id)

            for group in report.claim_alias_groups:
                if group.disposition != "ready_for_review":
                    continue
                canonical = by_id.get(group.canonical_candidate_id)
                aliases = [by_id.get(item) for item in group.alias_ids]
                if (
                    canonical is None
                    or canonical.node_type is not NodeType.CLAIM
                    or any(item is None or item.node_type is not NodeType.CLAIM for item in aliases)
                ):
                    raise LegacyMigrationError("reviewed claim alias group is no longer resolvable")
                if logical_version(exact_statement(canonical.body)) != group.logical_version or any(
                    logical_version(exact_statement(item.body)) != group.logical_version
                    for item in aliases
                    if item is not None
                ):
                    raise LegacyMigrationError("reviewed claim alias group changed exact statement")
                canonical.metadata["matek_claim_alias_ids"] = sorted(group.alias_ids)
                canonical.metadata["matek_legacy_migration_plan_sha256"] = plan_sha256
                canonical.updated_at = now
                canonical.last_modified_run = run_id
                canonical.author_role = "matek-legacy-migrator"
                changed.add(canonical.matek_id)
                for alias in aliases:
                    if alias is None:  # pragma: no cover - guarded above for type narrowing
                        continue
                    alias.metadata["matek_alias_of"] = canonical.matek_id
                    alias.metadata["matek_legacy_migration_plan_sha256"] = plan_sha256
                    alias.relations = _unique_edges(
                        [
                            *alias.relations,
                            GraphEdge(
                                source_id=alias.matek_id,
                                relation=RelationType.EQUIVALENT_TO,
                                target_id=canonical.matek_id,
                            ),
                        ]
                    )
                    alias.updated_at = now
                    alias.last_modified_run = run_id
                    alias.author_role = "matek-legacy-migrator"
                    changed.add(alias.matek_id)

            for quarantine in report.refutation_quarantines:
                refutation = by_id.get(quarantine.refutation_node_id)
                if refutation is None or refutation.node_type is not NodeType.COUNTEREXAMPLE:
                    raise LegacyMigrationError(
                        f"reviewed refutation node is unavailable: {quarantine.refutation_node_id}"
                    )
                refutation.relations = [
                    edge
                    for edge in refutation.relations
                    if not (
                        edge.relation is RelationType.REFUTES
                        and edge.target_id == quarantine.main_target_id
                    )
                ]
                if len(quarantine.candidate_branch_target_ids) == 1:
                    branch_target = quarantine.candidate_branch_target_ids[0]
                    approach = by_id.get(branch_target)
                    if approach is None or approach.node_type is not NodeType.APPROACH:
                        raise LegacyMigrationError(
                            f"reviewed refutation retarget is unavailable: {branch_target}"
                        )
                    refutation.relations = _unique_edges(
                        [
                            *refutation.relations,
                            GraphEdge(
                                source_id=refutation.matek_id,
                                relation=RelationType.REFUTES,
                                target_id=branch_target,
                            ),
                        ]
                    )
                refutation.metadata["matek_quarantined_main_refutation"] = quarantine.main_target_id
                refutation.metadata["matek_quarantine_reason"] = quarantine.reason
                refutation.metadata["matek_legacy_migration_plan_sha256"] = plan_sha256
                refutation.tags = list(
                    dict.fromkeys([*refutation.tags, "matek/quarantined-refutation"])
                )
                refutation.updated_at = now
                refutation.last_modified_run = run_id
                refutation.author_role = "matek-legacy-migrator"
                changed.add(refutation.matek_id)

            for nomination, task_id in zip(report.audit_nominations, audit_task_ids, strict=True):
                derivation_id = proposal_by_proof.get(nomination.proof_node_id)
                if derivation_id is None:
                    raise LegacyMigrationError(
                        f"audit nomination for {nomination.claim_id} lacks a proposed derivation"
                    )
                if task_id in by_id:
                    raise LegacyMigrationError(
                        f"deterministic audit task ID collides with archive node: {task_id}"
                    )
                task = GraphNode(
                    matek_id=task_id,
                    node_type=NodeType.TASK,
                    problem_id=report.problem_id,
                    title=f"Fresh audit of legacy result {nomination.claim_id}",
                    epistemic_status=EpistemicStatus.OPEN,
                    workflow_status=WorkflowStatus.QUEUED,
                    created_in_run=run_id,
                    last_modified_run=run_id,
                    author_role="matek-legacy-migrator",
                    created_at=now,
                    updated_at=now,
                    body=new_generated_body(
                        f"Fresh audit of legacy result {nomination.claim_id}",
                        "## Exact statement\n\n"
                        + nomination.exact_statement
                        + "\n\n## Required independent lanes\n\n"
                        "- verifier\n- falsifier\n\n"
                        "## Admission rule\n\nBoth fresh blinded lanes must pass before promotion.",
                    ),
                    tags=["matek/task", "matek/audit-nomination", "matek/legacy-backfill"],
                    relations=[
                        GraphEdge(
                            source_id=task_id,
                            relation=RelationType.TARGETS,
                            target_id=nomination.claim_id,
                        ),
                        GraphEdge(
                            source_id=task_id,
                            relation=RelationType.TARGETS,
                            target_id=derivation_id,
                        ),
                    ],
                    source_artifacts=[f"legacy-migration-plan:{plan_sha256}"],
                    metadata={
                        "matek_audit_lanes": list(nomination.independent_lanes),
                        "matek_audit_status": "queued",
                        "matek_legacy_proof_node_id": nomination.proof_node_id,
                        "matek_logical_version": nomination.logical_version,
                        "matek_strength_score": nomination.strength_score,
                        "matek_legacy_migration_plan_sha256": plan_sha256,
                    },
                )
                by_id[task_id] = task
                changed.add(task_id)

            relation_issues = [
                issue
                for node_id in sorted(changed)
                for edge in by_id[node_id].relations
                for issue in [
                    (
                        f"edge target does not exist: {edge.target_id}"
                        if edge.target_id not in by_id
                        else self._relation_issue(edge, by_id)
                    )
                ]
                if issue is not None
            ]
            cycle = self._dependency_cycle(list(by_id.values()))
            if cycle is not None:
                relation_issues.append(
                    "legacy migration creates dependency cycle: " + " -> ".join(cycle)
                )
            if relation_issues:
                raise LegacyMigrationError("; ".join(relation_issues))

            result = self._commit_nodes_unlocked(
                state=state,
                all_nodes=list(by_id.values()),
                changed_node_ids=sorted(changed),
                run_id=run_id,
                author="matek-legacy-migrator",
                reason="Apply an explicitly reviewed integrity-bound legacy graph migration.",
                operation_id=operation_id,
                source_artifacts=[f"legacy-migration-plan:{plan_sha256}"],
            )
            record = application_record(result, applied_at=now, status="applied")
            write_legacy_migration_application(
                record_path,
                record,
                confinement_root=self.ledgers_root,
            )
            return record

    def _node_path(self, node: GraphNode, state: GraphState) -> str:
        # A parsed node path is authoritative for an allowed human rename. Stable
        # identity comes from frontmatter, never from the filename.
        existing = node.path or state.node_paths.get(node.matek_id)
        if existing:
            return existing
        return self._canonical_node_path(node)

    @staticmethod
    def _node_directory_name(node: GraphNode) -> str:
        """Return the portable vault directory component for one node.

        Legacy hash IDs are already path-safe.  Descriptive one-liner IDs contain
        spaces and a colon, which Obsidian and some filesystems reject, so their
        directory is an ASCII slug with a short digest of the full ID appended:
        slugs truncate, and distinct one-liners must never share a directory.  The
        full ID stays in frontmatter and wikilink labels.
        """

        if is_legacy_node_id(node.matek_id):
            return node.matek_id
        slug = _slug(node.matek_id)[:48].rstrip("-") or "node"
        digest = hashlib.sha256(node.matek_id.casefold().encode("utf-8")).hexdigest()[:8]
        return f"{slug}-{digest}"

    @classmethod
    def _canonical_node_path(cls, node: GraphNode) -> str:
        directory = NODE_TYPE_DIRECTORIES[node.node_type]
        return f"{directory}/{cls._node_directory_name(node)}/{_note_filename(node.title)}.md"

    @classmethod
    def _uses_title_path(cls, node: GraphNode, relative: str) -> bool:
        expected_parent = Path(NODE_TYPE_DIRECTORIES[node.node_type]) / cls._node_directory_name(
            node
        )
        return Path(relative).parent == expected_parent

    def _migrate_legacy_paths_unlocked(
        self,
        state: GraphState,
        nodes: Sequence[GraphNode],
    ) -> tuple[GraphState, list[GraphNode]]:
        """Move old ID-prefixed notes to title-named files without losing identity."""

        overrides: dict[str, str] = {}
        managed = {node.matek_id: node for node in nodes if node.matek_id in state.node_paths}
        for node_id, node in managed.items():
            current = node.path
            recorded = state.node_paths.get(node_id)
            if current is None or current != recorded:
                # A path that differs from state is a deliberate human rename.
                continue
            path = Path(current)
            expected_parent = Path(NODE_TYPE_DIRECTORIES[node.node_type])
            if path.parent != expected_parent or not path.name.startswith(f"{node_id}--"):
                continue
            desired = self._canonical_node_path(node)
            if desired != current:
                overrides[node_id] = desired
        if not overrides:
            return state, list(nodes)
        migration_key = hashlib.sha256(
            _canonical_json(dict(sorted(overrides.items()))).encode("utf-8")
        ).hexdigest()[:16]
        self._commit_nodes_unlocked(
            state=state,
            all_nodes=list(nodes),
            changed_node_ids=list(managed),
            run_id="SYSTEM",
            author="matek-graph-migrator",
            reason="Use note titles as Obsidian graph labels while preserving stable IDs.",
            operation_id=f"obsidian-title-paths:{migration_key}",
            path_overrides=overrides,
        )
        migrated_state = self._load_state_unlocked()
        migrated_nodes = self._load_nodes_unlocked(include_human_notes=True)
        return migrated_state, migrated_nodes

    def _commit_nodes_unlocked(
        self,
        *,
        state: GraphState,
        all_nodes: Sequence[GraphNode],
        changed_node_ids: Sequence[str],
        run_id: str,
        author: str,
        reason: str,
        operation_id: str,
        source_artifacts: Sequence[str] = (),
        result_status: Literal["merged", "partially_merged"] = "merged",
        stale_node_ids: Sequence[str] = (),
        path_overrides: Mapping[str, str] | None = None,
        removed_paths: Sequence[str] = (),
        additional_writes: Mapping[str, str] | None = None,
        issues: Sequence[str] = (),
    ) -> GraphMergeResult:
        if operation_id in state.processed_operations:
            previous = state.processed_operations[operation_id]
            return previous.model_copy(update={"status": "already_applied"})
        nodes = {node.matek_id: node.model_copy(deep=True) for node in all_nodes}
        changed = list(dict.fromkeys(changed_node_ids))
        missing = sorted(set(changed) - nodes.keys())
        if missing:
            raise GraphValidationError("cannot commit missing graph nodes: " + ", ".join(missing))
        overrides = dict(path_overrides or {})
        unknown_overrides = sorted(set(overrides) - nodes.keys())
        if unknown_overrides:
            raise GraphValidationError(
                "cannot override paths for missing graph nodes: " + ", ".join(unknown_overrides)
            )
        planned_paths: dict[str, str] = {}
        automatic_removals = list(removed_paths)
        for node_id in changed:
            node = nodes[node_id]
            current = node.path or state.node_paths.get(node_id)
            if node_id in overrides:
                relative = overrides[node_id]
            elif (
                current is not None
                and current == state.node_paths.get(node_id)
                and self._uses_title_path(node, current)
            ):
                relative = self._canonical_node_path(node)
            else:
                relative = self._node_path(node, state)
            planned_paths[node_id] = relative
            if current is not None and current != relative:
                automatic_removals.append(current)
        changed_path_ids = {
            node_id
            for node_id, relative in planned_paths.items()
            if relative != (nodes[node_id].path or state.node_paths.get(node_id))
        }
        if changed_path_ids:
            for source in nodes.values():
                if source.matek_id in changed:
                    continue
                if any(edge.target_id in changed_path_ids for edge in source.relations):
                    changed.append(source.matek_id)
                    planned_paths[source.matek_id] = self._node_path(source, state)
        extra_writes = dict(additional_writes or {})
        for relative in extra_writes:
            requested = Path(relative)
            if requested.is_absolute() or requested.as_posix() != relative:
                raise GraphValidationError(
                    f"graph transaction has an invalid additional write path: {relative!r}"
                )
            ensure_path_confined(self.vault_root, self.vault_root / relative)
        duplicate_writes = sorted(set(planned_paths.values()).intersection(extra_writes))
        if duplicate_writes:
            raise GraphValidationError(
                "graph transaction writes a path more than once: " + ", ".join(duplicate_writes)
            )
        removals = list(dict.fromkeys(automatic_removals))
        written_paths = {*planned_paths.values(), *extra_writes}
        overlapping = sorted(written_paths.intersection(removals))
        if overlapping:
            raise GraphValidationError(
                "graph transaction cannot write and remove the same path: " + ", ".join(overlapping)
            )
        for node_id, relative in planned_paths.items():
            current = nodes[node_id].path or state.node_paths.get(node_id)
            target = ensure_path_confined(self.vault_root, self.vault_root / relative)
            if target.exists() and relative != current:
                raise GraphConflictError(f"graph note path already exists: {relative}")
            nodes[node_id].path = relative
        previous_hashes = {node_id: state.node_hashes.get(node_id) for node_id in changed}
        writes: list[dict[str, str]] = []
        next_hashes = dict(state.node_hashes)
        next_paths = dict(state.node_paths)
        next_machine = dict(state.machine_hashes)
        next_statements = dict(state.statement_hashes)
        for node_id in changed:
            node = nodes[node_id]
            relative = planned_paths[node_id]
            contents = render_node_note(node, relation_targets=nodes)
            digest = sha256_text(contents)
            node.path = relative
            node.content_hash = digest
            next_paths[node_id] = relative
            next_hashes[node_id] = digest
            next_machine[node_id] = machine_hash(node)
            next_statements[node_id] = statement_hash(node)
            writes.append({"path": relative, "contents": contents, "sha256": digest})
        for relative, contents in sorted(extra_writes.items()):
            writes.append(
                {
                    "path": relative,
                    "contents": contents,
                    "sha256": sha256_text(contents),
                }
            )
        # A human may rename an unchanged note. Preserve the discovered location in state.
        for node in nodes.values():
            if node.path and node.node_type is not NodeType.HUMAN_NOTE:
                next_paths[node.matek_id] = node.path
                if node.content_hash:
                    next_hashes.setdefault(node.matek_id, node.content_hash)
                next_machine.setdefault(node.matek_id, machine_hash(node))
                next_statements.setdefault(node.matek_id, statement_hash(node))
        now = self._now()
        next_number = state.revision_number + 1
        next_revision = _revision(next_number, next_hashes)
        result = GraphMergeResult(
            operation_id=operation_id,
            status=result_status,
            base_revision=state.revision,
            previous_revision=state.revision,
            new_revision=next_revision,
            created_node_ids=[node_id for node_id in changed if previous_hashes[node_id] is None],
            updated_node_ids=[
                node_id for node_id in changed if previous_hashes[node_id] is not None
            ],
            stale_node_ids=list(dict.fromkeys(stale_node_ids)),
            issues=list(dict.fromkeys(issues)),
        )
        next_state = state.model_copy(deep=True)
        next_state.revision_number = next_number
        next_state.revision = next_revision
        next_state.updated_at = now
        next_state.node_paths = next_paths
        next_state.node_hashes = next_hashes
        next_state.machine_hashes = next_machine
        next_state.statement_hashes = next_statements
        next_state.processed_operations[operation_id] = result
        next_state.changes.append(
            GraphChangeRecord(
                revision=next_revision,
                previous_revision=state.revision,
                run_id=run_id,
                author=author,
                timestamp=now,
                reason=reason,
                operation_id=operation_id,
                changed_nodes=changed,
                previous_hashes=previous_hashes,
                new_hashes={node_id: next_hashes.get(node_id) for node_id in changed},
                source_artifacts=list(source_artifacts),
            )
        )
        transaction = {
            "schema_version": 1,
            "operation_id": operation_id,
            "previous_revision": state.revision,
            "new_revision": next_revision,
            "writes": writes,
            "removals": removals,
            "state_after": next_state.model_dump(mode="json"),
        }
        atomic_write_json(self.pending_path, transaction, confinement_root=self.graph_root)
        for write in writes:
            target = ensure_path_confined(self.vault_root, self.vault_root / write["path"])
            atomic_write_text(
                target, write["contents"], confinement_root=self.vault_root, mode=0o600
            )
        for relative in removals:
            self._unlink_graph_file_unlocked(relative)
        atomic_write_json(self.state_path, next_state, confinement_root=self.graph_root)
        committed_nodes = list(nodes.values())
        self._write_snapshot_unlocked(next_state, committed_nodes)
        self._refresh_derived_views_unlocked(next_state, committed_nodes)
        self.pending_path.unlink(missing_ok=True)
        return result

    def _write_snapshot_unlocked(self, state: GraphState, nodes: Sequence[GraphNode]) -> None:
        if state.revision_number == 0:
            previous_revision = None
        else:
            change = next(
                (item for item in reversed(state.changes) if item.revision == state.revision),
                None,
            )
            if change is None:
                raise GraphValidationError(
                    f"graph revision {state.revision} has no parent change record"
                )
            previous_revision = change.previous_revision
        selected = [
            node
            for node in nodes
            if node.node_type is not NodeType.HUMAN_NOTE or node.matek_id in state.node_paths
        ]
        try:
            self.snapshot_store.write_revision(
                revision=state.revision,
                revision_number=state.revision_number,
                created_at=state.updated_at.isoformat(),
                nodes=selected,
                previous_revision=previous_revision,
            )
        except (GraphMarkdownError, SnapshotIntegrityError) as exc:
            raise GraphValidationError(
                f"cannot persist graph revision snapshot {state.revision}: {exc}"
            ) from exc

    def _snapshot_unlocked(self, revision: str) -> dict[str, Any]:
        if not _REVISION.fullmatch(revision):
            raise GraphValidationError(f"invalid graph revision: {revision!r}")
        try:
            return self.snapshot_store.load_snapshot(revision)
        except SnapshotIntegrityError as exc:
            raise GraphValidationError(
                f"graph revision snapshot is invalid: {revision}: {exc}"
            ) from exc

    def reconstruct_snapshot(self, revision: str) -> bytes:
        """Reconstruct a deterministic full snapshot, preserving legacy bytes exactly."""

        with self._locked():
            self._recover_pending_unlocked()
            self._load_state_unlocked()
            try:
                return self.snapshot_store.reconstruct_bytes(revision)
            except ValueError as exc:
                raise GraphValidationError(
                    f"cannot reconstruct graph revision snapshot {revision}: {exc}"
                ) from exc

    def verify_snapshots(self, revision: str | None = None) -> list[GraphSnapshotVerification]:
        """Verify one revision or the graph's complete immutable snapshot history."""

        with self._locked():
            self._recover_pending_unlocked()
            self._load_state_unlocked()
            try:
                if revision is not None:
                    return [self.snapshot_store.verify_revision(revision)]
                return self.snapshot_store.verify_all()
            except ValueError as exc:
                target = revision or "history"
                raise GraphValidationError(
                    f"graph snapshot integrity verification failed for {target}: {exc}"
                ) from exc

    def _rebuild_index_unlocked(
        self, state: GraphState, nodes: Sequence[GraphNode] | None = None
    ) -> Path:
        selected = (
            list(nodes)
            if nodes is not None
            else self._load_nodes_unlocked(include_human_notes=True)
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".graph-index.", suffix=".sqlite", dir=self.graph_root
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            connection = sqlite3.connect(temporary)
            try:
                connection.executescript(
                    """
                    PRAGMA journal_mode=DELETE;
                    PRAGMA foreign_keys=OFF;
                    CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE nodes (
                        matek_id TEXT PRIMARY KEY,
                        node_type TEXT NOT NULL,
                        problem_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        epistemic_status TEXT NOT NULL,
                        workflow_status TEXT NOT NULL,
                        claim_type TEXT,
                        statement_version INTEGER NOT NULL,
                        path TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        body TEXT NOT NULL,
                        invalidation_reasons_json TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        tombstone INTEGER NOT NULL
                    );
                    CREATE TABLE edges (
                        source_id TEXT NOT NULL,
                        relation TEXT NOT NULL,
                        target_id TEXT NOT NULL,
                        PRIMARY KEY (source_id, relation, target_id)
                    );
                    CREATE TABLE tags (
                        matek_id TEXT NOT NULL,
                        tag TEXT NOT NULL,
                        PRIMARY KEY (matek_id, tag)
                    );
                    CREATE TABLE changes (
                        revision TEXT NOT NULL,
                        operation_id TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        author TEXT NOT NULL,
                        reason TEXT NOT NULL
                    );
                    CREATE INDEX nodes_problem_status ON nodes(problem_id, epistemic_status);
                    CREATE INDEX edges_target ON edges(target_id, relation);
                    """
                )
                connection.execute(
                    "INSERT INTO metadata VALUES (?, ?)", ("revision", state.revision)
                )
                connection.execute(
                    "INSERT INTO metadata VALUES (?, ?)",
                    ("schema_version", str(GRAPH_SCHEMA_VERSION)),
                )
                for node in selected:
                    content_hash = node.content_hash or sha256_text(render_node_note(node))
                    connection.execute(
                        "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            node.matek_id,
                            node.node_type.value,
                            node.problem_id,
                            node.title,
                            node.epistemic_status.value,
                            node.workflow_status.value,
                            node.claim_type.value if node.claim_type is not None else None,
                            node.statement_version,
                            node.path or "",
                            content_hash,
                            node.body,
                            _canonical_json(node.invalidation_reasons),
                            _canonical_json(node.metadata),
                            int(node.tombstone),
                        ),
                    )
                    connection.executemany(
                        "INSERT OR IGNORE INTO tags VALUES (?, ?)",
                        [(node.matek_id, tag) for tag in node.tags],
                    )
                    connection.executemany(
                        "INSERT OR IGNORE INTO edges VALUES (?, ?, ?)",
                        [
                            (edge.source_id, edge.relation.value, edge.target_id)
                            for edge in node.relations
                        ],
                    )
                connection.executemany(
                    "INSERT INTO changes VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            change.revision,
                            change.operation_id,
                            change.timestamp.isoformat(),
                            change.run_id,
                            change.author,
                            change.reason,
                        )
                        for change in state.changes
                    ],
                )
                connection.commit()
            finally:
                connection.close()
            os.replace(temporary, self.index_path)
        finally:
            temporary.unlink(missing_ok=True)
        return self.index_path

    def _refresh_derived_views_unlocked(
        self, state: GraphState, nodes: Sequence[GraphNode]
    ) -> None:
        """Best-effort refresh caches without making them research authorities."""

        try:
            self._write_navigation_unlocked(state, nodes)
        except (OSError, sqlite3.Error, GraphValidationError) as exc:
            warnings.warn(
                "derived graph navigation refresh failed; continuing from Markdown: " + str(exc),
                RuntimeWarning,
                stacklevel=2,
            )
        try:
            self._rebuild_index_unlocked(state, nodes)
        except (OSError, sqlite3.Error, GraphValidationError) as exc:
            warnings.warn(
                "derived graph index refresh failed; continuing from Markdown: " + str(exc),
                RuntimeWarning,
                stacklevel=2,
            )

    def rebuild_index(self) -> Path:
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            state, nodes = self._migrate_legacy_paths_unlocked(state, nodes)
            self._write_navigation_unlocked(state, nodes)
            return self._rebuild_index_unlocked(state, nodes)

    def doctor(
        self,
        *,
        repair: bool = False,
        problem_id: str | None = None,
        run_id: str = "SYSTEM",
    ) -> GraphHygieneReport:
        """Inspect or transactionally repair whitelisted generated source metadata."""

        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            if problem_id is not None and not any(
                node.matek_id == problem_id and node.node_type is NodeType.PROBLEM for node in nodes
            ):
                raise GraphValidationError(f"problem node does not exist: {problem_id}")
            timestamp = self._now()
            selected = [
                node
                for node in nodes
                if node.node_type is NodeType.SOURCE
                and not node.tombstone
                and (problem_id is None or node.problem_id == problem_id)
            ]
            repaired_nodes: dict[str, GraphNode] = {}
            actions: list[GraphHygieneAction] = []
            for node in sorted(selected, key=lambda item: item.matek_id):
                for rule in _SOURCE_HYGIENE_RULES.values():
                    planned = rule(node, run_id, timestamp)
                    if planned is None:
                        continue
                    repaired_node, repaired_action = planned
                    repaired_nodes[node.matek_id] = repaired_node
                    actions.append(repaired_action)
                    break
            warnings = [action.warning for action in actions if action.warning is not None]
            if not repair or not actions:
                return GraphHygieneReport(
                    graph_name=self.graph_name,
                    problem_id=problem_id,
                    inspected_source_count=len(selected),
                    repair_requested=repair,
                    previous_revision=state.revision,
                    new_revision=state.revision,
                    actions=actions,
                    warnings=warnings,
                )

            by_id = {node.matek_id: node for node in nodes}
            by_id.update(repaired_nodes)
            applied_actions = [action.model_copy(update={"applied": True}) for action in actions]
            action_payloads = [action.model_dump(mode="json") for action in applied_actions]
            repair_sha256 = hashlib.sha256(
                _canonical_json(action_payloads).encode("utf-8")
            ).hexdigest()
            operation_id = f"graph-hygiene:{repair_sha256[:20]}"
            repair_log = f"repairs/{operation_id.replace(':', '-')}.json"
            log_payload = {
                "schema_version": 1,
                "failure_class": "metadata_invariant",
                "graph_name": self.graph_name,
                "problem_id": problem_id,
                "run_id": run_id,
                "timestamp": timestamp.isoformat(),
                "actions": action_payloads,
            }
            result = self._commit_nodes_unlocked(
                state=state,
                all_nodes=list(by_id.values()),
                changed_node_ids=sorted(repaired_nodes),
                run_id=run_id,
                author="matek-graph-hygiene",
                reason="Repair whitelisted generated source identity metadata.",
                operation_id=operation_id,
                source_artifacts=[repair_log],
                additional_writes={
                    repair_log: json.dumps(
                        log_payload,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                },
            )
            return GraphHygieneReport(
                graph_name=self.graph_name,
                problem_id=problem_id,
                inspected_source_count=len(selected),
                repair_requested=True,
                previous_revision=result.previous_revision,
                new_revision=result.new_revision,
                actions=applied_actions,
                warnings=warnings,
                repair_log=repair_log,
            )

    @staticmethod
    def _relation_issue(edge: GraphEdge, by_id: Mapping[str, GraphNode]) -> str | None:
        source = by_id[edge.source_id]
        target = by_id[edge.target_id]
        allowed: dict[RelationType, tuple[set[NodeType] | None, set[NodeType] | None]] = {
            RelationType.PROVES: ({NodeType.PROOF, NodeType.DERIVATION}, {NodeType.CLAIM}),
            RelationType.DISPROVES: (
                {NodeType.PROOF, NodeType.DERIVATION},
                {NodeType.CLAIM},
            ),
            RelationType.AUDITS: (
                {
                    NodeType.AUDIT,
                },
                {
                    NodeType.PROOF,
                    NodeType.PROOF_ATTEMPT,
                    NodeType.DERIVATION,
                    NodeType.CLAIM,
                },
            ),
            RelationType.DEPENDS_ON: (
                {
                    NodeType.CLAIM,
                    NodeType.DEFINITION,
                    NodeType.PROOF,
                    NodeType.PROOF_ATTEMPT,
                    NodeType.DERIVATION,
                    NodeType.OBLIGATION,
                    NodeType.COUNTEREXAMPLE,
                },
                {NodeType.CLAIM, NodeType.DEFINITION, NodeType.OBLIGATION},
            ),
            RelationType.FORMALIZES: ({NodeType.FORMALIZATION}, {NodeType.CLAIM}),
            RelationType.REFUTES: (
                {NodeType.COUNTEREXAMPLE},
                {NodeType.CLAIM, NodeType.APPROACH},
            ),
            RelationType.TARGETS: ({NodeType.TASK, NodeType.OBLIGATION}, None),
            RelationType.CITES: (None, {NodeType.SOURCE}),
            RelationType.CREATED_DURING: (None, {NodeType.RUN}),
        }
        constraint = allowed.get(edge.relation)
        if constraint is None:
            return None
        source_types, target_types = constraint
        if source_types is not None and source.node_type not in source_types:
            return (
                f"{edge.relation.value} cannot originate at {source.node_type.value} "
                f"node {source.matek_id}"
            )
        if target_types is not None and target.node_type not in target_types:
            return (
                f"{edge.relation.value} cannot target {target.node_type.value} "
                f"node {target.matek_id}"
            )
        return None

    @staticmethod
    def _dependency_cycle(nodes: Sequence[GraphNode]) -> list[str] | None:
        graph: dict[str, list[str]] = defaultdict(list)
        for node in nodes:
            for edge in node.relations:
                if edge.relation is RelationType.DEPENDS_ON:
                    graph[edge.source_id].append(edge.target_id)
        visiting: set[str] = set()
        visited: set[str] = set()
        path: list[str] = []

        def visit(node_id: str) -> list[str] | None:
            if node_id in visiting:
                start = path.index(node_id)
                return [*path[start:], node_id]
            if node_id in visited:
                return None
            visiting.add(node_id)
            path.append(node_id)
            for target in graph.get(node_id, []):
                cycle = visit(target)
                if cycle is not None:
                    return cycle
            path.pop()
            visiting.remove(node_id)
            visited.add(node_id)
            return None

        for node_id in sorted(graph):
            cycle = visit(node_id)
            if cycle is not None:
                return cycle
        return None

    def _validate_unlocked(
        self, state: GraphState, nodes: Sequence[GraphNode]
    ) -> GraphValidationReport:
        issues: list[GraphValidationIssue] = []
        by_id = {node.matek_id: node for node in nodes}
        managed = {node_id: node for node_id, node in by_id.items() if node_id in state.node_paths}
        for node_id, relative in state.node_paths.items():
            node = by_id.get(node_id)
            if node is None:
                issues.append(
                    GraphValidationIssue(
                        severity="error",
                        code="missing_node",
                        message=f"managed node {node_id} is missing",
                        path=relative,
                        node_id=node_id,
                    )
                )
                continue
            if node.path != relative:
                issues.append(
                    GraphValidationIssue(
                        severity="warning",
                        code="human_rename",
                        message=(
                            f"node {node_id} was renamed from {relative!r} to {node.path!r}; "
                            "its stable ID remains unchanged"
                        ),
                        path=node.path,
                        node_id=node_id,
                    )
                )
            expected_machine = state.machine_hashes.get(node_id)
            if expected_machine is not None and machine_hash(node) != expected_machine:
                issues.append(
                    GraphValidationIssue(
                        severity="error",
                        code="machine_field_changed",
                        message=(
                            f"machine-managed frontmatter changed for {node_id}; restore the "
                            "managed fields or apply a validated graph patch"
                        ),
                        path=node.path,
                        node_id=node_id,
                    )
                )
                continue
            expected_content = state.node_hashes.get(node_id)
            if expected_content is not None and node.content_hash != expected_content:
                code = (
                    "claim_statement_changed"
                    if node.node_type is NodeType.CLAIM
                    and statement_hash(node) != state.statement_hashes.get(node_id, "")
                    else "human_prose_changed"
                )
                issues.append(
                    GraphValidationIssue(
                        severity="warning",
                        code=code,
                        message=(
                            f"human-editable content changed for {node_id}; the next run will "
                            "preserve it and record any required invalidation"
                        ),
                        path=node.path,
                        node_id=node_id,
                    )
                )
        for node in nodes:
            if node.node_type is not NodeType.HUMAN_NOTE and node.problem_id not in by_id:
                issues.append(
                    GraphValidationIssue(
                        severity="error",
                        code="missing_problem",
                        message=(
                            f"node {node.matek_id} references missing problem {node.problem_id}"
                        ),
                        path=node.path,
                        node_id=node.matek_id,
                    )
                )
            for edge in node.relations:
                if edge.target_id not in by_id:
                    issues.append(
                        GraphValidationIssue(
                            severity="error",
                            code="missing_relation_target",
                            message=(
                                f"{edge.source_id} --{edge.relation.value}--> "
                                f"{edge.target_id} has no target node"
                            ),
                            path=node.path,
                            node_id=node.matek_id,
                        )
                    )
                    continue
                relation_issue = self._relation_issue(edge, by_id)
                if relation_issue:
                    issues.append(
                        GraphValidationIssue(
                            severity="error",
                            code="invalid_relation_types",
                            message=relation_issue,
                            path=node.path,
                            node_id=node.matek_id,
                        )
                    )
        cycle = self._dependency_cycle(nodes)
        if cycle is not None:
            issues.append(
                GraphValidationIssue(
                    severity="error",
                    code="dependency_cycle",
                    message="mathematical dependency cycle: " + " -> ".join(cycle),
                )
            )
        try:
            snapshot = self._snapshot_unlocked(state.revision)
            snapshot_hashes = snapshot.get("node_hashes")
            if snapshot_hashes != dict(sorted(state.node_hashes.items())):
                raise GraphValidationError("current snapshot node hashes do not match graph state")
        except GraphValidationError as exc:
            issues.append(
                GraphValidationIssue(
                    severity="error",
                    code="snapshot_integrity",
                    message=str(exc),
                )
            )
        if self.index_path.is_file():
            try:
                connection = sqlite3.connect(f"file:{self.index_path}?mode=ro", uri=True)
                try:
                    row = connection.execute(
                        "SELECT value FROM metadata WHERE key = 'revision'"
                    ).fetchone()
                finally:
                    connection.close()
                if row is None or row[0] != state.revision:
                    issues.append(
                        GraphValidationIssue(
                            severity="warning",
                            code="index_stale",
                            message="derived SQLite index is stale; run matek graph rebuild-index",
                        )
                    )
            except sqlite3.Error as exc:
                issues.append(
                    GraphValidationIssue(
                        severity="warning",
                        code="index_invalid",
                        message=f"derived SQLite index is unreadable and rebuildable: {exc}",
                    )
                )
        elif managed:
            issues.append(
                GraphValidationIssue(
                    severity="warning",
                    code="index_missing",
                    message="derived SQLite index is missing and rebuildable",
                )
            )
        return GraphValidationReport(
            valid=not any(issue.severity == "error" for issue in issues),
            revision=state.revision,
            node_count=len(nodes),
            edge_count=sum(len(node.relations) for node in nodes),
            issues=issues,
        )

    def validate(self) -> GraphValidationReport:
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            try:
                nodes = self._load_nodes_unlocked(include_human_notes=True)
            except (GraphMarkdownError, GraphValidationError) as exc:
                return GraphValidationReport(
                    valid=False,
                    revision=state.revision,
                    node_count=0,
                    edge_count=0,
                    issues=[
                        GraphValidationIssue(
                            severity="error",
                            code="malformed_note",
                            message=str(exc),
                        )
                    ],
                )
            return self._validate_unlocked(state, nodes)

    def _render_dashboard(self, title: str, nodes: Sequence[GraphNode], *, description: str) -> str:
        lines = [f"# {title}", "", GENERATED_START, description, ""]
        if nodes:
            lines.extend(f"- {wikilink_for(node)}" for node in nodes)
        else:
            lines.append("_No matching nodes._")
        lines.extend([GENERATED_END, ""])
        return "\n".join(lines)

    def _write_navigation_unlocked(self, state: GraphState, nodes: Sequence[GraphNode]) -> None:
        by_problem: dict[str, list[GraphNode]] = defaultdict(list)
        for node in nodes:
            by_problem[node.problem_id].append(node)
        problems = sorted(
            (node for node in nodes if node.node_type is NodeType.PROBLEM),
            key=lambda item: item.title.casefold(),
        )
        established = [
            node
            for node in nodes
            if node.node_type is NodeType.CLAIM
            and node.epistemic_status
            in {EpistemicStatus.AUDIT_PASSED, EpistemicStatus.LEAN_VERIFIED}
        ]
        obligations = [
            node
            for node in nodes
            if (
                node.node_type is NodeType.OBLIGATION
                and node.workflow_status is not WorkflowStatus.COMPLETE
            )
        ]
        active_tasks = [
            node
            for node in nodes
            if node.node_type is NodeType.TASK
            and node.workflow_status
            in {WorkflowStatus.QUEUED, WorkflowStatus.ACTIVE, WorkflowStatus.IN_PROGRESS}
        ]
        blocked = [
            node
            for node in nodes
            if node.node_type is NodeType.APPROACH
            and node.workflow_status
            in {WorkflowStatus.BLOCKED, WorkflowStatus.ABANDONED, WorkflowStatus.SUPERSEDED}
        ]
        contradictions = [
            node
            for node in nodes
            if node.epistemic_status is EpistemicStatus.INCONSISTENT
            or any(edge.relation is RelationType.CONTRADICTS for edge in node.relations)
        ]
        recent_runs = sorted(
            (node for node in nodes if node.node_type is NodeType.RUN),
            key=lambda item: item.updated_at,
            reverse=True,
        )[:12]
        formalizations = [node for node in nodes if node.node_type is NodeType.FORMALIZATION]
        accepted_main_result_needs = [node for node in nodes if MAIN_RESULT_NEEDS_TAG in node.tags]
        main_result_needs = accepted_main_result_needs or [
            node
            for node in nodes
            if (
                node.node_type is NodeType.OBLIGATION
                and node.workflow_status is not WorkflowStatus.COMPLETE
            )
            or (
                node.node_type is NodeType.CLAIM
                and node.matek_id == self.main_claim_id(node.problem_id)
            )
        ]
        home_generated: list[str] = [
            "## Exact main problem",
            "",
            *(f"- {wikilink_for(problem)}" for problem in problems),
            "",
            "## Overall status",
            "",
            f"Graph revision: `{state.revision}`",
            f"Tracked problems: {len(problems)}; nodes: {len(nodes)}.",
            "",
            "## Strongest established results",
            "",
            *(f"- {wikilink_for(node)} — `{node.epistemic_status.value}`" for node in established),
            "",
            "## Current proof architecture",
            "",
            "See [[Dashboards/Main Proof Architecture.canvas|Main Proof Architecture]].",
            "",
            "## Main result needs",
            "",
            *(f"- {wikilink_for(node)}" for node in main_result_needs),
            "",
            "## Unresolved main obligations",
            "",
            *(f"- {wikilink_for(node)}" for node in obligations),
            "",
            "## Active tasks",
            "",
            *(f"- {wikilink_for(node)}" for node in active_tasks),
            "",
            "## Blocked or refuted routes",
            "",
            *(f"- {wikilink_for(node)}" for node in blocked),
            "",
            "## Unresolved contradictions",
            "",
            *(f"- {wikilink_for(node)}" for node in contradictions),
            "",
            "## Recent run summaries",
            "",
            *(f"- {wikilink_for(node)}" for node in recent_runs),
            "",
            "## Lean verification status",
            "",
            *(
                f"- {wikilink_for(node)} — `{node.epistemic_status.value}`"
                for node in formalizations
            ),
            "",
            "## Dashboards",
            "",
            "- [[Dashboards/Open Claims]]",
            "- [[Dashboards/Candidate Proofs Awaiting Audit]]",
            "- [[Dashboards/Audit-Passed Results]]",
            "- [[Dashboards/Lean-Verified Results]]",
            "- [[Dashboards/Active Tasks]]",
            "- [[Dashboards/Blocked Approaches]]",
            "- [[Dashboards/Unresolved Contradictions]]",
            "- [[Dashboards/Unverified Sources]]",
            "- [[Dashboards/Main Result Needs]]",
            "- [[Dashboards/Recent Changes]]",
        ]
        home = self.vault_root / "Home.md"
        human = ""
        if home.is_file():
            existing = home.read_text(encoding="utf-8")
            end = existing.find(GENERATED_END)
            human = existing[end + len(GENERATED_END) :].strip() if end >= 0 else existing
        home_text = new_generated_body("MATEK Knowledge Graph", "\n".join(home_generated), human)
        atomic_write_text(home, home_text, confinement_root=self.vault_root)

        candidate_proofs = [
            node
            for node in nodes
            if node.node_type in {NodeType.PROOF, NodeType.DERIVATION}
            and node.epistemic_status
            in {EpistemicStatus.CANDIDATE, EpistemicStatus.PROVED_INFORMALLY}
            and node.workflow_status is not WorkflowStatus.BLOCKED
            and not bool(node.metadata.get("matek_exact_gap"))
        ]
        audit_passed = [
            node for node in nodes if node.epistemic_status is EpistemicStatus.AUDIT_PASSED
        ]
        lean_verified = [
            node for node in nodes if node.epistemic_status is EpistemicStatus.LEAN_VERIFIED
        ]
        unverified_sources = [
            node
            for node in nodes
            if node.node_type is NodeType.SOURCE
            and not bool(node.metadata.get("matek_verified", False))
        ]
        recent_changed_nodes: list[GraphNode] = []
        for node_id in reversed(
            list(
                dict.fromkeys(
                    node_id for change in state.changes[-20:] for node_id in change.changed_nodes
                )
            )
        ):
            matched = next((item for item in nodes if item.matek_id == node_id), None)
            if matched is not None:
                recent_changed_nodes.append(matched)
        dashboards: dict[str, tuple[str, list[GraphNode]]] = {
            "Open Claims": (
                "Claims that remain open, conjectured, candidate, or stale.",
                [
                    node
                    for node in nodes
                    if node.node_type is NodeType.CLAIM
                    and node.epistemic_status
                    in {
                        EpistemicStatus.OPEN,
                        EpistemicStatus.CONJECTURED,
                        EpistemicStatus.CANDIDATE,
                        EpistemicStatus.STALE,
                    }
                ],
            ),
            "Candidate Proofs Awaiting Audit": (
                "Candidate proof nodes that have not passed independent audit.",
                candidate_proofs,
            ),
            "Audit-Passed Results": ("Claims and proofs with passing audits.", audit_passed),
            "Lean-Verified Results": (
                "Claims and formalizations certified by deterministic Lean checks.",
                lean_verified,
            ),
            "Active Tasks": ("Queued or active graph-scoped research tasks.", active_tasks),
            "Blocked Approaches": ("Blocked, abandoned, or superseded approaches.", blocked),
            "Unresolved Contradictions": (
                "Inconsistent nodes or nodes participating in contradiction edges.",
                contradictions,
            ),
            "Unverified Sources": (
                "Source nodes without independently verified identifiers.",
                unverified_sources,
            ),
            "Main Result Needs": (
                "The immutable target and smallest known open cut; after acceptance, the "
                "audited support closure.",
                main_result_needs,
            ),
            "Recent Changes": (
                "Nodes touched by recent graph revisions.",
                recent_changed_nodes,
            ),
        }
        dashboard_root = self.vault_root / "Dashboards"
        for title, (description, selected) in dashboards.items():
            atomic_write_text(
                dashboard_root / f"{title}.md",
                self._render_dashboard(title, selected, description=description),
                confinement_root=self.vault_root,
            )
        self._write_canvases_unlocked(nodes)

    def _write_canvases_unlocked(self, nodes: Sequence[GraphNode]) -> None:
        specifications: dict[str, tuple[set[NodeType], set[RelationType]]] = {
            "Main Proof Architecture": (
                {
                    NodeType.CLAIM,
                    NodeType.PROOF,
                    NodeType.DERIVATION,
                    NodeType.OBLIGATION,
                    NodeType.DEFINITION,
                    NodeType.SOURCE,
                },
                {
                    RelationType.DEPENDS_ON,
                    RelationType.PROVES,
                    RelationType.BLOCKS,
                    RelationType.BLOCKED_BY,
                    RelationType.CITES,
                },
            ),
            "Active Research Routes": (
                {NodeType.APPROACH, NodeType.TASK, NodeType.CLAIM},
                {RelationType.TARGETS, RelationType.MOTIVATES, RelationType.RELATED_TO},
            ),
            "Dependency Bottlenecks": (
                {
                    NodeType.CLAIM,
                    NodeType.OBLIGATION,
                    NodeType.DERIVATION,
                    NodeType.DEFINITION,
                    NodeType.COUNTEREXAMPLE,
                },
                {RelationType.DEPENDS_ON, RelationType.BLOCKED_BY, RelationType.REFUTES},
            ),
            "Formalization Map": (
                {NodeType.CLAIM, NodeType.FORMALIZATION, NodeType.ARTIFACT},
                {RelationType.FORMALIZES, RelationType.RELATED_TO},
            ),
        }
        by_id = {node.matek_id: node for node in nodes}
        for title, (node_types, relations) in specifications.items():
            tagged_architecture = title == "Main Proof Architecture" and any(
                MAIN_RESULT_NEEDS_TAG in node.tags for node in nodes
            )
            selected = [
                node
                for node in nodes
                if node.node_type in node_types
                and node.node_type is not NodeType.HUMAN_NOTE
                and (not tagged_architecture or MAIN_RESULT_NEEDS_TAG in node.tags)
            ][:40]
            selected_ids = {node.matek_id for node in selected}
            canvas_nodes = [
                {
                    "id": node.matek_id,
                    "type": "file",
                    "file": node.path,
                    "x": (index % 5) * 360,
                    "y": (index // 5) * 240,
                    "width": 320,
                    "height": 180,
                }
                for index, node in enumerate(selected)
                if node.path
            ]
            canvas_edges = [
                {
                    "id": hashlib.sha256(
                        f"{edge.source_id}:{edge.relation.value}:{edge.target_id}".encode()
                    ).hexdigest()[:16],
                    "fromNode": edge.source_id,
                    "toNode": edge.target_id,
                    "label": edge.relation.value,
                }
                for node in selected
                for edge in node.relations
                if edge.relation in relations
                and edge.target_id in selected_ids
                and edge.target_id in by_id
            ]
            atomic_write_json(
                self.vault_root / "Dashboards" / f"{title}.canvas",
                {"nodes": canvas_nodes, "edges": canvas_edges},
                confinement_root=self.vault_root,
            )

    def status(self) -> GraphStatus:
        if not self.initialized:
            return GraphStatus(
                graph_name=self.graph_name,
                initialized=False,
                vault_path=str(self.vault_root),
                revision=None,
                node_count=0,
                edge_count=0,
                problem_count=0,
                stale_count=0,
                active_task_count=0,
            )
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            return GraphStatus(
                graph_name=self.graph_name,
                initialized=True,
                vault_path=str(self.vault_root),
                revision=state.revision,
                node_count=len(nodes),
                edge_count=sum(len(node.relations) for node in nodes),
                problem_count=sum(node.node_type is NodeType.PROBLEM for node in nodes),
                stale_count=sum(node.epistemic_status is EpistemicStatus.STALE for node in nodes),
                active_task_count=sum(
                    node.node_type is NodeType.TASK
                    and node.workflow_status
                    in {WorkflowStatus.QUEUED, WorkflowStatus.ACTIVE, WorkflowStatus.IN_PROGRESS}
                    for node in nodes
                ),
                last_change_at=state.updated_at,
            )

    @staticmethod
    def _select_problem_id(nodes: Sequence[GraphNode], problem_id: str | None) -> str:
        problems = [node.matek_id for node in nodes if node.node_type is NodeType.PROBLEM]
        if problem_id is not None:
            if problem_id not in problems:
                raise GraphValidationError(f"unknown graph problem ID: {problem_id}")
            return problem_id
        if len(problems) == 1:
            return problems[0]
        if not problems:
            raise GraphValidationError("knowledge graph has no problem node")
        raise GraphValidationError(
            "knowledge graph tracks multiple problems; pass an explicit problem ID"
        )

    def _frontier_unlocked(
        self, state: GraphState, nodes: Sequence[GraphNode], problem_id: str
    ) -> GraphFrontier:
        selected = [node for node in nodes if node.problem_id == problem_id]
        by_id = {node.matek_id: node for node in selected}
        trusted_ids = _markdown_trusted_claim_ids(selected)
        open_obligation_nodes = [
            node
            for node in selected
            if node.node_type is NodeType.OBLIGATION
            and node.workflow_status is not WorkflowStatus.COMPLETE
        ]
        main_target = by_id.get(self.main_claim_id(problem_id))

        def obligation_priority(node: GraphNode) -> tuple[int, str]:
            raw_leverage = node.metadata.get("matek_estimated_leverage", 0)
            leverage = raw_leverage if isinstance(raw_leverage, int) else 0
            return (-leverage, node.title.casefold())

        proof_targets = {
            edge.target_id
            for node in selected
            if node.node_type in {NodeType.PROOF, NodeType.DERIVATION}
            and node.epistemic_status
            in {EpistemicStatus.CANDIDATE, EpistemicStatus.PROVED_INFORMALLY}
            for edge in node.relations
            if edge.relation is RelationType.PROVES
        }
        audited_targets = {
            edge.target_id
            for node in selected
            if node.node_type is NodeType.AUDIT
            and node.epistemic_status
            in {EpistemicStatus.AUDIT_PASSED, EpistemicStatus.LEAN_VERIFIED}
            for edge in node.relations
            if edge.relation is RelationType.AUDITS
        }
        missing_dependency_sources = {
            node.matek_id
            for node in selected
            if any(
                edge.relation is RelationType.DEPENDS_ON
                and edge.target_id not in {item.matek_id for item in nodes}
                for edge in node.relations
            )
            or "missing_dependency" in node.invalidation_reasons
        }
        unresolved_claims = [
            node
            for node in selected
            if node.node_type is NodeType.CLAIM
            and node.epistemic_status
            in {
                EpistemicStatus.OPEN,
                EpistemicStatus.CONJECTURED,
                EpistemicStatus.CANDIDATE,
                EpistemicStatus.STALE,
            }
        ]
        candidate_proofs = [
            node
            for node in selected
            if node.node_type in {NodeType.PROOF, NodeType.DERIVATION}
            and node.epistemic_status
            in {EpistemicStatus.CANDIDATE, EpistemicStatus.PROVED_INFORMALLY}
            and node.workflow_status is not WorkflowStatus.BLOCKED
            and not bool(node.metadata.get("matek_exact_gap"))
            and node.matek_id not in audited_targets
            and any(
                edge.relation is RelationType.PROVES and edge.target_id in proof_targets
                for edge in node.relations
            )
        ]
        return GraphFrontier(
            problem_id=problem_id,
            graph_revision=state.revision,
            main_target=_node_summary(main_target) if main_target is not None else None,
            live_derivations=[
                _node_summary(node)
                for node in selected
                if node.node_type is NodeType.DERIVATION
                and node.epistemic_status
                not in {
                    EpistemicStatus.REFUTED,
                    EpistemicStatus.INCONSISTENT,
                    EpistemicStatus.STALE,
                }
            ],
            strongest_audited_results=[
                _node_summary(node)
                for node in selected
                if node.node_type is NodeType.CLAIM and node.matek_id in trusted_ids
            ],
            open_obligations=[_node_summary(node) for node in open_obligation_nodes],
            smallest_known_open_cut=[
                _node_summary(node)
                for node in (
                    sorted(open_obligation_nodes, key=obligation_priority)[:3]
                    or ([main_target] if main_target is not None else [])
                )
            ],
            open_cut_search_capped=False,
            unresolved_claims=[_node_summary(node) for node in unresolved_claims],
            candidate_proofs_awaiting_audit=[_node_summary(node) for node in candidate_proofs],
            blocked_approaches=[
                _node_summary(node)
                for node in selected
                if node.node_type is NodeType.APPROACH
                and node.workflow_status
                in {WorkflowStatus.BLOCKED, WorkflowStatus.DORMANT, WorkflowStatus.ABANDONED}
            ],
            unresolved_contradictions=[
                _node_summary(node)
                for node in selected
                if node.epistemic_status is EpistemicStatus.INCONSISTENT
                or any(edge.relation is RelationType.CONTRADICTS for edge in node.relations)
            ],
            missing_dependencies=[
                _node_summary(node)
                for node in selected
                if node.matek_id in missing_dependency_sources
            ],
            high_value_tasks=[
                _node_summary(node)
                for node in selected
                if node.node_type is NodeType.TASK
                and node.workflow_status
                in {WorkflowStatus.QUEUED, WorkflowStatus.ACTIVE, WorkflowStatus.IN_PROGRESS}
            ],
            prior_runs=[
                _node_summary(node)
                for node in sorted(
                    (item for item in selected if item.node_type is NodeType.RUN),
                    key=lambda item: item.updated_at,
                    reverse=True,
                )[:20]
            ],
            refuted_or_unproductive_routes=[
                _node_summary(node)
                for node in selected
                if (
                    node.epistemic_status is EpistemicStatus.REFUTED
                    or node.workflow_status
                    in {
                        WorkflowStatus.ABANDONED,
                        WorkflowStatus.SUPERSEDED,
                        WorkflowStatus.DORMANT,
                    }
                )
                and node.node_type
                in {
                    NodeType.APPROACH,
                    NodeType.CLAIM,
                    NodeType.PROOF,
                    NodeType.PROOF_ATTEMPT,
                    NodeType.DERIVATION,
                }
            ],
            unverified_sources=[
                _node_summary(node)
                for node in selected
                if node.node_type is NodeType.SOURCE
                and not bool(node.metadata.get("matek_verified", False))
            ],
        )

    def frontier(self, problem_id: str | None = None) -> GraphFrontier:
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            selected = self._select_problem_id(nodes, problem_id)
            return self._frontier_unlocked(state, nodes, selected)

    def _context_slice_unlocked(
        self,
        state: GraphState,
        nodes: Sequence[GraphNode],
        *,
        problem_id: str,
        task_id: str,
    ) -> GraphContextSlice:
        by_id = {node.matek_id: node for node in nodes}
        task = by_id.get(task_id)
        if task is None or task.node_type is not NodeType.TASK:
            raise GraphValidationError(f"graph task does not exist: {task_id}")
        target_ids = [
            edge.target_id for edge in task.relations if edge.relation is RelationType.TARGETS
        ] or [problem_id]
        reverse: dict[str, list[GraphEdge]] = defaultdict(list)
        for node in nodes:
            for edge in node.relations:
                reverse[edge.target_id].append(edge)
        queue: deque[tuple[str, int]] = deque(
            [(problem_id, 0), (task_id, 0), *((target, 0) for target in target_ids)]
        )
        selected_ids: list[str] = []
        seen: set[str] = set()
        while queue and len(selected_ids) < self.maximum_context_nodes:
            node_id, depth = queue.popleft()
            if node_id in seen or node_id not in by_id:
                continue
            node = by_id[node_id]
            if node.problem_id != problem_id and node.matek_id != problem_id:
                continue
            seen.add(node_id)
            selected_ids.append(node_id)
            if depth >= 3:
                continue
            for edge in node.relations:
                if edge.relation in {
                    RelationType.DEPENDS_ON,
                    RelationType.PROVES,
                    RelationType.REFUTES,
                    RelationType.CITES,
                    RelationType.AUDITS,
                    RelationType.FORMALIZES,
                    RelationType.BLOCKED_BY,
                    RelationType.RELATED_TO,
                }:
                    queue.append((edge.target_id, depth + 1))
            for edge in reverse.get(node_id, []):
                if edge.relation in {
                    RelationType.DEPENDS_ON,
                    RelationType.PROVES,
                    RelationType.REFUTES,
                    RelationType.AUDITS,
                    RelationType.FORMALIZES,
                    RelationType.TARGETS,
                }:
                    queue.append((edge.source_id, depth + 1))
        context_nodes: list[GraphContextNode] = []
        remaining_characters = self.maximum_context_characters
        for node_id in selected_ids:
            node = by_id[node_id]
            excerpt = generated_section(node.body)
            excerpt = excerpt[: min(6_000, remaining_characters)]
            remaining_characters -= len(excerpt)
            context_nodes.append(
                GraphContextNode(
                    summary=_node_summary(node),
                    body_excerpt=excerpt,
                    outgoing=node.relations,
                    content_hash=node.content_hash or sha256_text(render_node_note(node)),
                )
            )
            if remaining_characters <= 0:
                break
        problem_node_count = sum(
            node.problem_id == problem_id or node.matek_id == problem_id for node in nodes
        )
        return GraphContextSlice(
            graph_revision=state.revision,
            problem_id=problem_id,
            task_id=task_id,
            target_node_ids=target_ids,
            exact_task=generated_section(task.body),
            nodes=context_nodes,
            omitted_node_count=max(0, problem_node_count - len(context_nodes)),
        )

    def context_slice(self, problem_id: str, task_id: str) -> GraphContextSlice:
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            return self._context_slice_unlocked(
                state, nodes, problem_id=problem_id, task_id=task_id
            )

    def show(self, node_id: str) -> GraphNode:
        with self._locked():
            self._recover_pending_unlocked()
            self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            node = next((item for item in nodes if item.matek_id == node_id), None)
            if node is None:
                raise GraphValidationError(f"graph node does not exist: {node_id}")
            return node

    def traverse(
        self, node_id: str, *, downstream: bool, relation: RelationType = RelationType.DEPENDS_ON
    ) -> list[GraphNodeSummary]:
        with self._locked():
            self._recover_pending_unlocked()
            self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            by_id = {node.matek_id: node for node in nodes}
            if node_id not in by_id:
                raise GraphValidationError(f"graph node does not exist: {node_id}")
            adjacency: dict[str, list[str]] = defaultdict(list)
            for node in nodes:
                for edge in node.relations:
                    if edge.relation is relation:
                        if downstream:
                            adjacency[edge.target_id].append(edge.source_id)
                        else:
                            adjacency[edge.source_id].append(edge.target_id)
            result: list[GraphNodeSummary] = []
            queue = deque(adjacency.get(node_id, []))
            seen = {node_id}
            while queue:
                current = queue.popleft()
                if current in seen or current not in by_id:
                    continue
                seen.add(current)
                result.append(_node_summary(by_id[current]))
                queue.extend(adjacency.get(current, []))
            return result

    def list_stale(self, problem_id: str | None = None) -> list[GraphNodeSummary]:
        nodes = self.load_nodes()
        selected = self._select_problem_id(nodes, problem_id)
        return [
            _node_summary(node)
            for node in nodes
            if node.problem_id == selected
            and (node.epistemic_status is EpistemicStatus.STALE or node.invalidation_reasons)
        ]

    def list_tasks(self, problem_id: str | None = None) -> list[GraphNodeSummary]:
        nodes = self.load_nodes()
        selected = self._select_problem_id(nodes, problem_id)
        return [
            _node_summary(node)
            for node in nodes
            if node.problem_id == selected and node.node_type is NodeType.TASK
        ]

    def tombstone(self, node_id: str, *, reason: str, run_id: str = "HUMAN") -> GraphMergeResult:
        """Retain a deleted/superseded identity without breaking incoming links."""

        if not reason.strip():
            raise GraphValidationError("tombstoning a node requires a reason")
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            by_id = {node.matek_id: node for node in nodes}
            node = by_id.get(node_id)
            if node is None:
                raise GraphValidationError(f"graph node does not exist: {node_id}")
            if node.node_type in {NodeType.PROBLEM, NodeType.RUN}:
                raise GraphValidationError("problem and run nodes cannot be tombstoned")
            if node.tombstone:
                operation_id = f"tombstone:{node_id}:{sha256_text(reason.strip())[:16]}"
                previous = state.processed_operations.get(operation_id)
                if previous is not None:
                    return previous.model_copy(update={"status": "already_applied"})
            node.tombstone = True
            node.workflow_status = WorkflowStatus.SUPERSEDED
            node.epistemic_status = EpistemicStatus.STALE
            node.invalidation_reasons = list(
                dict.fromkeys([*node.invalidation_reasons, "tombstoned"])
            )
            node.metadata["matek_tombstone_reason"] = reason.strip()
            node.last_modified_run = run_id
            node.author_role = "human"
            node.updated_at = self._now()
            stale = self._propagate_staleness(
                by_id, [node_id], "dependency_tombstoned_requires_reaudit"
            )
            operation_id = f"tombstone:{node_id}:{sha256_text(reason.strip())[:16]}"
            return self._commit_nodes_unlocked(
                state=state,
                all_nodes=list(by_id.values()),
                changed_node_ids=[node_id, *stale],
                run_id=run_id,
                author="human",
                reason=f"Tombstone {node_id}: {reason.strip()}",
                operation_id=operation_id,
                stale_node_ids=[node_id, *stale],
            )

    def diff(self, revision_a: str, revision_b: str) -> GraphDiff:
        with self._locked():
            self._recover_pending_unlocked()
            first = self._snapshot_unlocked(revision_a)
            second = self._snapshot_unlocked(revision_b)
            first_hashes = cast(dict[str, str], first.get("node_hashes", {}))
            second_hashes = cast(dict[str, str], second.get("node_hashes", {}))
            first_edges = {
                (item["source_id"], item["relation"], item["target_id"])
                for item in cast(list[dict[str, str]], first.get("edges", []))
            }
            second_edges = {
                (item["source_id"], item["relation"], item["target_id"])
                for item in cast(list[dict[str, str]], second.get("edges", []))
            }

            def edge(value: tuple[str, str, str]) -> GraphEdge:
                return GraphEdge(
                    source_id=value[0], relation=RelationType(value[1]), target_id=value[2]
                )

            return GraphDiff(
                revision_a=revision_a,
                revision_b=revision_b,
                added_nodes=sorted(second_hashes.keys() - first_hashes.keys()),
                removed_nodes=sorted(first_hashes.keys() - second_hashes.keys()),
                changed_nodes=sorted(
                    node_id
                    for node_id in first_hashes.keys() & second_hashes.keys()
                    if first_hashes[node_id] != second_hashes[node_id]
                ),
                added_edges=[edge(value) for value in sorted(second_edges - first_edges)],
                removed_edges=[edge(value) for value in sorted(first_edges - second_edges)],
            )

    def export(self, *, output_format: Literal["json", "graphviz", "mermaid"] = "json") -> str:
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            edges = _unique_edges(edge for node in nodes for edge in node.relations)
            if output_format == "json":
                return (
                    json.dumps(
                        {
                            "schema_version": 1,
                            "revision": state.revision,
                            "nodes": [node.model_dump(mode="json") for node in nodes],
                            "edges": [edge.model_dump(mode="json") for edge in edges],
                        },
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                )
            if output_format == "graphviz":
                lines = ["digraph MATEK {"]
                for node in nodes:
                    label = json.dumps(f"{node.matek_id}\\n{node.title}")
                    lines.append(f'  "{node.matek_id}" [label={label}];')
                for edge in edges:
                    lines.append(
                        f'  "{edge.source_id}" -> "{edge.target_id}" '
                        f'[label="{edge.relation.value}"];'
                    )
                lines.append("}")
                return "\n".join(lines) + "\n"
            lines = ["flowchart TD"]
            for node in nodes:
                safe_title = node.title.replace('"', "'").replace("[", "(").replace("]", ")")
                lines.append(f'  {node.matek_id.replace("-", "_")}["{safe_title}"]')
            for edge in edges:
                lines.append(
                    f"  {edge.source_id.replace('-', '_')} -->|{edge.relation.value}| "
                    f"{edge.target_id.replace('-', '_')}"
                )
            return "\n".join(lines) + "\n"

    def open_in_obsidian(self) -> tuple[bool, Path, str]:
        """Launch Obsidian when discoverable, otherwise return a graceful remedy."""

        if not self.initialized:
            self.initialize()
        executable = shutil.which("obsidian")
        url = f"obsidian://open?path={quote(str(self.vault_root))}"
        if executable is not None:
            try:
                subprocess.Popen(
                    [executable, url],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as exc:
                return False, self.vault_root, f"Obsidian could not be launched: {exc}"
            return True, self.vault_root, "Opened the MATEK vault in Obsidian."
        return (
            False,
            self.vault_root,
            "Obsidian is not installed or not on PATH; open this directory as a vault manually.",
        )

    @staticmethod
    def _propagate_staleness(
        nodes: dict[str, GraphNode], seeds: Sequence[str], reason: str
    ) -> list[str]:
        def has_live_audited_derivation(claim_id: str, *, excluding: str) -> bool:
            claim = nodes.get(claim_id)
            if claim is None or claim.node_type is not NodeType.CLAIM:
                return False
            current_nodes = [
                node
                for node in nodes.values()
                if node.matek_id != excluding
                and not node.tombstone
                and not node.invalidation_reasons
            ]
            by_id = {node.matek_id: node for node in current_nodes}
            trusted = _markdown_trusted_claim_ids(current_nodes)
            for derivation in current_nodes:
                if derivation.node_type not in {NodeType.PROOF, NodeType.DERIVATION}:
                    continue
                conclusion = derivation.metadata.get("matek_conclusion_claim_id")
                if not isinstance(conclusion, str):
                    proved = [
                        edge.target_id
                        for edge in derivation.relations
                        if edge.relation is RelationType.PROVES
                    ]
                    conclusion = proved[0] if len(proved) == 1 else None
                if conclusion != claim_id:
                    continue
                if not _context_node_is_live(derivation) or derivation.epistemic_status not in {
                    EpistemicStatus.AUDIT_PASSED,
                    EpistemicStatus.LEAN_VERIFIED,
                }:
                    continue
                dependencies = [
                    by_id.get(edge.target_id)
                    for edge in derivation.relations
                    if edge.relation is RelationType.DEPENDS_ON
                ]
                if any(dependency is None for dependency in dependencies):
                    continue
                if any(
                    dependency.node_type in {NodeType.CLAIM, NodeType.DEFINITION}
                    and dependency.matek_id not in trusted
                    for dependency in dependencies
                    if dependency is not None
                ):
                    continue
                if any(
                    dependency.node_type is NodeType.OBLIGATION
                    and (
                        dependency.workflow_status is not WorkflowStatus.COMPLETE
                        and dependency.epistemic_status
                        not in {EpistemicStatus.AUDIT_PASSED, EpistemicStatus.LEAN_VERIFIED}
                    )
                    for dependency in dependencies
                    if dependency is not None
                ):
                    continue
                return True
            return False

        changed: list[str] = []
        queue: deque[str] = deque(seeds)
        visited = set(seeds)
        while queue:
            changed_id = queue.popleft()
            changed_node = nodes.get(changed_id)
            if changed_node is None:
                continue
            affected: set[str] = set()
            for node in nodes.values():
                if (
                    changed_node.node_type is NodeType.PROOF_ATTEMPT
                    and node.node_type is NodeType.DERIVATION
                    and node.metadata.get("matek_proof_attempt_id") == changed_id
                ):
                    # The canonical derivation stores its proof evidence in a separate PAT
                    # node. RELATED_TO remains intentionally non-causal in general, so follow
                    # only this application-owned, typed identity link for invalidation.
                    affected.add(node.matek_id)
                for edge in node.relations:
                    if edge.relation is RelationType.DEPENDS_ON and edge.target_id == changed_id:
                        affected.add(edge.source_id)
                    if edge.relation in {RelationType.AUDITS, RelationType.FORMALIZES} and (
                        edge.target_id == changed_id
                    ):
                        affected.add(edge.source_id)
                    if edge.relation is RelationType.PROVES:
                        if edge.source_id == changed_id:
                            affected.add(edge.target_id)
                        elif edge.target_id == changed_id:
                            affected.add(edge.source_id)
            for node_id in sorted(affected):
                node = nodes[node_id]
                if (
                    node.node_type is NodeType.CLAIM
                    and changed_node.node_type in {NodeType.DERIVATION, NodeType.PROOF}
                    and has_live_audited_derivation(node_id, excluding=changed_node.matek_id)
                ):
                    continue
                if node.node_type in {
                    NodeType.CLAIM,
                    NodeType.PROOF,
                    NodeType.PROOF_ATTEMPT,
                    NodeType.DERIVATION,
                    NodeType.OBLIGATION,
                    NodeType.AUDIT,
                    NodeType.FORMALIZATION,
                }:
                    node.epistemic_status = EpistemicStatus.STALE
                    node.invalidation_reasons = list(
                        dict.fromkeys([*node.invalidation_reasons, reason])
                    )
                    if node.node_type is NodeType.FORMALIZATION:
                        node.workflow_status = WorkflowStatus.BLOCKED
                    if node_id not in changed:
                        changed.append(node_id)
                if node_id not in visited:
                    visited.add(node_id)
                    queue.append(node_id)
        return changed

    def reconcile_human_edits(self, *, run_id: str) -> GraphMergeResult | None:
        """Preserve allowed human prose/renames and invalidate changed mathematics."""

        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            by_id = {node.matek_id: node for node in nodes}
            conflicts: list[str] = []
            changed: list[str] = []
            stale: list[str] = []
            now = self._now()
            for node_id, expected_machine in state.machine_hashes.items():
                node = by_id.get(node_id)
                if node is None:
                    conflicts.append(f"managed node {node_id} was deleted; use a tombstone")
                    continue
                if machine_hash(node) != expected_machine:
                    conflicts.append(f"machine-managed frontmatter changed for {node_id}")
                    continue
                renamed = node.path != state.node_paths.get(node_id)
                content_changed = node.content_hash != state.node_hashes.get(node_id)
                if not (renamed or content_changed):
                    continue
                if (
                    content_changed
                    and node.node_type is NodeType.CLAIM
                    and (statement_hash(node) != state.statement_hashes.get(node_id, ""))
                ):
                    if node_id == self.main_claim_id(node.problem_id):
                        conflicts.append(
                            f"immutable main target {node_id} was edited; use an explicit "
                            "target migration"
                        )
                        continue
                    node.statement_version += 1
                    node.epistemic_status = EpistemicStatus.STALE
                    node.invalidation_reasons = list(
                        dict.fromkeys(
                            [*node.invalidation_reasons, "statement_changed_requires_reaudit"]
                        )
                    )
                    stale.append(node_id)
                    stale.extend(
                        self._propagate_staleness(
                            by_id, [node_id], "dependency_changed_requires_reaudit"
                        )
                    )
                elif content_changed and node.node_type in {
                    NodeType.PROOF,
                    NodeType.PROOF_ATTEMPT,
                }:
                    node.epistemic_status = EpistemicStatus.STALE
                    node.invalidation_reasons = list(
                        dict.fromkeys(
                            [*node.invalidation_reasons, "proof_changed_requires_reaudit"]
                        )
                    )
                    stale.append(node_id)
                    stale.extend(
                        self._propagate_staleness(
                            by_id, [node_id], "proof_changed_requires_reaudit"
                        )
                    )
                node.author_role = "human"
                node.last_modified_run = run_id
                node.updated_at = now
                changed.append(node_id)
            if conflicts:
                raise GraphConflictError(
                    "knowledge vault contains conflicting manual changes: " + "; ".join(conflicts)
                )
            changed = list(dict.fromkeys([*changed, *stale]))
            if not changed:
                return None
            return self._commit_nodes_unlocked(
                state=state,
                all_nodes=list(by_id.values()),
                changed_node_ids=changed,
                run_id=run_id,
                author="human",
                reason="Preserve human vault edits and invalidate affected evidence.",
                operation_id=f"human-reconcile:{run_id}:{state.revision}",
                stale_node_ids=stale,
            )

    def _problem_file_key(self, source_path: Path) -> str:
        resolved = source_path.expanduser().resolve(strict=True)
        try:
            return resolved.relative_to(self.project_root).as_posix()
        except ValueError:
            digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:16]
            return f"external-{digest}-{resolved.name}"

    def initialize_problem(
        self,
        *,
        source_path: Path,
        problem_text: str,
        run_id: str,
    ) -> tuple[str, str]:
        """Create or load the stable problem node and one run node."""

        if not self.initialized:
            self.initialize()
        self.reconcile_human_edits(run_id=run_id)
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            by_id = {node.matek_id: node for node in nodes}
            key = self._problem_file_key(source_path)
            problem_id = state.problem_files.get(key)
            now = self._now()
            changed: list[str] = []
            if problem_id is None:
                problem_id = _new_id(NodeType.PROBLEM)
                state.problem_files[key] = problem_id
                problem = GraphNode(
                    matek_id=problem_id,
                    node_type=NodeType.PROBLEM,
                    problem_id=problem_id,
                    title=source_path.stem.replace("_", " ").replace("-", " ").title(),
                    epistemic_status=EpistemicStatus.OPEN,
                    workflow_status=WorkflowStatus.ACTIVE,
                    created_in_run=run_id,
                    last_modified_run=run_id,
                    author_role="matek-intake",
                    created_at=now,
                    updated_at=now,
                    body=new_generated_body(
                        source_path.stem,
                        "## Exact main problem\n\n"
                        + problem_text.strip()
                        + "\n\n## Overall status\n\nResearch not yet compiled.",
                    ),
                    tags=["matek/problem"],
                    source_artifacts=[f".matek/runs/{run_id}/input/problem.md"],
                    metadata={
                        "matek_normalized_source_sha256": sha256_text(problem_text),
                    },
                )
                by_id[problem_id] = problem
                changed.append(problem_id)
            else:
                existing_problem = by_id.get(problem_id)
                if existing_problem is None:
                    raise GraphValidationError(
                        f"problem mapping {key!r} references missing node {problem_id}"
                    )
                problem = existing_problem
                normalized_source_sha256 = sha256_text(problem_text)
                if (
                    problem.metadata.get("matek_normalized_source_sha256")
                    != normalized_source_sha256
                ):
                    problem.metadata["matek_normalized_source_sha256"] = normalized_source_sha256
                    changed.append(problem_id)
                current_problem = exact_statement(problem.body)
                if problem_text.strip() not in current_problem:
                    problem.body = replace_generated_section(
                        problem.body,
                        problem.title,
                        "## Exact main problem\n\n"
                        + problem_text.strip()
                        + "\n\n## Overall status\n\nA later MATEK run updated the problem input.",
                    )
                    problem.updated_at = now
                    problem.last_modified_run = run_id
                    problem.author_role = "matek-intake"
                    problem.invalidation_reasons = list(
                        dict.fromkeys([*problem.invalidation_reasons, "problem_statement_changed"])
                    )
                    changed.append(problem_id)
                    changed.extend(
                        self._propagate_staleness(by_id, [problem_id], "problem_statement_changed")
                    )
            run_node_id = _deterministic_id(NodeType.RUN, problem_id, run_id)
            if run_node_id not in by_id:
                run_node = GraphNode(
                    matek_id=run_node_id,
                    node_type=NodeType.RUN,
                    problem_id=problem_id,
                    title=f"MATEK run {run_id}",
                    epistemic_status=EpistemicStatus.OPEN,
                    workflow_status=WorkflowStatus.IN_PROGRESS,
                    created_in_run=run_id,
                    last_modified_run=run_id,
                    author_role="matek-workflow",
                    created_at=now,
                    updated_at=now,
                    body=new_generated_body(
                        f"MATEK run {run_id}",
                        "## Run summary\n\nWorkflow started.\n\n"
                        "## Run artifacts\n\n"
                        f"- `.matek/runs/{run_id}/`",
                    ),
                    tags=["matek/run"],
                    relations=[
                        GraphEdge(
                            source_id=run_node_id,
                            relation=RelationType.RELATED_TO,
                            target_id=problem_id,
                        )
                    ],
                    source_artifacts=[f".matek/runs/{run_id}/state.json"],
                )
                by_id[run_node_id] = run_node
                changed.append(run_node_id)
            if changed:
                result = self._commit_nodes_unlocked(
                    state=state,
                    all_nodes=list(by_id.values()),
                    changed_node_ids=changed,
                    run_id=run_id,
                    author="matek-workflow",
                    reason="Initialize or resume the persistent problem graph.",
                    operation_id=f"run-start:{run_id}",
                )
                revision = result.new_revision
            else:
                revision = state.revision
            return problem_id, revision

    def _upsert_generated_nodes_unlocked(
        self,
        *,
        state: GraphState,
        nodes: dict[str, GraphNode],
        proposed: Sequence[GraphNode],
        run_id: str,
        author: str,
        reason: str,
        operation_id: str,
        source_artifacts: Sequence[str] = (),
        stale_node_ids: Sequence[str] = (),
        additional_writes: Mapping[str, str] | None = None,
        issues: Sequence[str] = (),
    ) -> GraphMergeResult:
        if operation_id in state.processed_operations:
            prior = state.processed_operations[operation_id]
            return prior.model_copy(update={"status": "already_applied"})
        changed: list[str] = []
        epistemic_rank = {
            EpistemicStatus.OPEN: 0,
            EpistemicStatus.CONJECTURED: 1,
            EpistemicStatus.CANDIDATE: 2,
            EpistemicStatus.PROVED_INFORMALLY: 3,
            EpistemicStatus.AUDIT_PASSED: 4,
            EpistemicStatus.LEAN_VERIFIED: 5,
        }
        for incoming in proposed:
            existing = nodes.get(incoming.matek_id)
            if existing is None:
                nodes[incoming.matek_id] = incoming
                changed.append(incoming.matek_id)
                continue
            definition_merge = (
                existing.node_type is NodeType.DEFINITION
                or incoming.node_type is NodeType.DEFINITION
            )
            if definition_merge:
                if (
                    existing.node_type is not NodeType.DEFINITION
                    or incoming.node_type is not NodeType.DEFINITION
                    or normalize_exact_statement(exact_statement(existing.body))
                    != normalize_exact_statement(exact_statement(incoming.body))
                ):
                    raise GraphValidationError(
                        f"canonical definition identity collision for {incoming.matek_id}"
                    )
                if canonical_definition_dependency_contract(
                    existing
                ) != canonical_definition_dependency_contract(incoming):
                    raise GraphValidationError(
                        f"canonical definition {incoming.matek_id} has an incompatible "
                        "dependency contract"
                    )
            # Preserve human prose outside the generated block and the stable creation record.
            if not definition_merge:
                existing.title = incoming.title
                existing.body = replace_generated_section(
                    existing.body, incoming.title, generated_section(incoming.body)
                )
            # A later workflow generation may refresh a deterministic note, but it
            # must never silently erase stronger evidence established in an earlier
            # run. Negative/invalidation states remain explicit and authoritative.
            if incoming.epistemic_status in {
                EpistemicStatus.REFUTED,
                EpistemicStatus.INCONSISTENT,
                EpistemicStatus.STALE,
            } or existing.epistemic_status in {
                EpistemicStatus.REFUTED,
                EpistemicStatus.INCONSISTENT,
                EpistemicStatus.STALE,
            }:
                existing.epistemic_status = incoming.epistemic_status
            elif (
                epistemic_rank[incoming.epistemic_status]
                >= epistemic_rank[existing.epistemic_status]
            ):
                existing.epistemic_status = incoming.epistemic_status
            existing.workflow_status = incoming.workflow_status
            existing.claim_type = incoming.claim_type
            existing.statement_version = max(existing.statement_version, incoming.statement_version)
            existing.last_modified_run = run_id
            # Preserve the typed producer identity carried by the proposed node.  In
            # particular, replay/manifest ART nodes must not become generic
            # ``research-worker`` records merely because an idempotent worker-report
            # integration refreshed their deterministic notes.
            existing.author_role = incoming.author_role
            existing.updated_at = incoming.updated_at
            existing.tags = list(dict.fromkeys([*existing.tags, *incoming.tags]))
            existing.relations = _unique_edges([*existing.relations, *incoming.relations])
            existing.invalidation_reasons = list(
                dict.fromkeys([*existing.invalidation_reasons, *incoming.invalidation_reasons])
            )
            existing.dependency_versions = list(
                dict.fromkeys([*existing.dependency_versions, *incoming.dependency_versions])
            )
            existing.source_artifacts = list(
                dict.fromkeys([*existing.source_artifacts, *incoming.source_artifacts])
            )
            existing.evidence = list(dict.fromkeys([*existing.evidence, *incoming.evidence]))
            existing.manuscript_mappings = list(
                dict.fromkeys([*existing.manuscript_mappings, *incoming.manuscript_mappings])
            )
            if definition_merge:
                raw_existing_bindings = existing.metadata.get("matek_admission_bindings")
                existing_bindings = (
                    [str(item) for item in raw_existing_bindings]
                    if isinstance(raw_existing_bindings, list)
                    else []
                )
                raw_incoming_bindings = incoming.metadata.get("matek_admission_bindings")
                incoming_bindings = (
                    [str(item) for item in raw_incoming_bindings]
                    if isinstance(raw_incoming_bindings, list)
                    else []
                )
                existing.metadata["matek_admission_bindings"] = sorted(
                    set([*existing_bindings, *incoming_bindings])
                )
                raw_existing_targets = existing.metadata.get("matek_target_node_ids")
                raw_incoming_targets = incoming.metadata.get("matek_target_node_ids")
                existing_targets = (
                    [str(item) for item in raw_existing_targets]
                    if isinstance(raw_existing_targets, list)
                    else []
                )
                incoming_targets = (
                    [str(item) for item in raw_incoming_targets]
                    if isinstance(raw_incoming_targets, list)
                    else []
                )
                existing.metadata["matek_target_node_ids"] = sorted(
                    set([*existing_targets, *incoming_targets])
                )
            else:
                existing.metadata.update(incoming.metadata)
            changed.append(existing.matek_id)
        return self._commit_nodes_unlocked(
            state=state,
            all_nodes=list(nodes.values()),
            changed_node_ids=changed,
            run_id=run_id,
            author=author,
            reason=reason,
            operation_id=operation_id,
            source_artifacts=source_artifacts,
            stale_node_ids=stale_node_ids,
            additional_writes=additional_writes,
            issues=issues,
        )

    @staticmethod
    def _epistemic_transition_allowed(current: EpistemicStatus, target: EpistemicStatus) -> bool:
        allowed: dict[EpistemicStatus, set[EpistemicStatus]] = {
            EpistemicStatus.OPEN: {
                EpistemicStatus.CONJECTURED,
                EpistemicStatus.CANDIDATE,
                EpistemicStatus.REFUTED,
                EpistemicStatus.INCONSISTENT,
                EpistemicStatus.STALE,
            },
            EpistemicStatus.CONJECTURED: {
                EpistemicStatus.CANDIDATE,
                EpistemicStatus.REFUTED,
                EpistemicStatus.INCONSISTENT,
                EpistemicStatus.STALE,
            },
            EpistemicStatus.CANDIDATE: {
                EpistemicStatus.PROVED_INFORMALLY,
                EpistemicStatus.REFUTED,
                EpistemicStatus.INCONSISTENT,
                EpistemicStatus.STALE,
            },
            EpistemicStatus.PROVED_INFORMALLY: {
                EpistemicStatus.CANDIDATE,
                EpistemicStatus.AUDIT_PASSED,
                EpistemicStatus.REFUTED,
                EpistemicStatus.INCONSISTENT,
                EpistemicStatus.STALE,
            },
            EpistemicStatus.AUDIT_PASSED: {
                EpistemicStatus.CANDIDATE,
                EpistemicStatus.LEAN_VERIFIED,
                EpistemicStatus.REFUTED,
                EpistemicStatus.INCONSISTENT,
                EpistemicStatus.STALE,
            },
            EpistemicStatus.LEAN_VERIFIED: {
                EpistemicStatus.STALE,
                EpistemicStatus.REFUTED,
                EpistemicStatus.INCONSISTENT,
            },
            EpistemicStatus.REFUTED: {EpistemicStatus.STALE, EpistemicStatus.OPEN},
            EpistemicStatus.INCONSISTENT: {EpistemicStatus.STALE, EpistemicStatus.OPEN},
            EpistemicStatus.STALE: {
                EpistemicStatus.OPEN,
                EpistemicStatus.CONJECTURED,
                EpistemicStatus.CANDIDATE,
                EpistemicStatus.PROVED_INFORMALLY,
                EpistemicStatus.AUDIT_PASSED,
                EpistemicStatus.REFUTED,
                EpistemicStatus.INCONSISTENT,
            },
        }
        return target is current or target in allowed[current]

    @staticmethod
    def _workflow_transition_allowed(current: WorkflowStatus, target: WorkflowStatus) -> bool:
        if current is target:
            return True
        if current is WorkflowStatus.COMPLETE:
            return target in {
                WorkflowStatus.ACTIVE,
                WorkflowStatus.BLOCKED,
                WorkflowStatus.SUPERSEDED,
            }
        if current is WorkflowStatus.SUPERSEDED:
            return target is WorkflowStatus.ACTIVE
        return True

    def merge_patch(
        self,
        patch: GraphPatch,
        *,
        problem_id: str,
        operation_id: str,
    ) -> GraphMergeResult:
        """Validate and atomically merge a proposed agent patch.

        A stale base revision may rebase only when every touched source node is
        unchanged from that frozen snapshot. MATEK binds content hashes from that
        server-owned revision; workers never supply trusted hashes. This
        permits independent concurrent additions while detecting true edit conflicts.
        """

        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            if operation_id in state.processed_operations:
                prior = state.processed_operations[operation_id]
                return prior.model_copy(update={"status": "already_applied"})
            nodes_list = self._load_nodes_unlocked(include_human_notes=True)
            validation = self._validate_unlocked(state, nodes_list)
            errors = [issue.message for issue in validation.issues if issue.severity == "error"]
            if errors:
                return GraphMergeResult(
                    operation_id=operation_id,
                    status="rejected",
                    base_revision=patch.base_graph_revision,
                    previous_revision=state.revision,
                    new_revision=state.revision,
                    issues=errors,
                )
            by_id = {node.matek_id: node.model_copy(deep=True) for node in nodes_list}
            task = by_id.get(patch.task_id)
            if task is None or task.node_type is not NodeType.TASK:
                return GraphMergeResult(
                    operation_id=operation_id,
                    status="rejected",
                    base_revision=patch.base_graph_revision,
                    previous_revision=state.revision,
                    new_revision=state.revision,
                    issues=[f"patch task does not exist: {patch.task_id}"],
                )
            if task.problem_id != problem_id:
                return GraphMergeResult(
                    operation_id=operation_id,
                    status="rejected",
                    base_revision=patch.base_graph_revision,
                    previous_revision=state.revision,
                    new_revision=state.revision,
                    issues=["patch task belongs to a different problem"],
                )
            if patch.agent_role != "research-worker" and not patch.agent_role.startswith(
                "research-auditor"
            ):
                return GraphMergeResult(
                    operation_id=operation_id,
                    status="rejected",
                    base_revision=patch.base_graph_revision,
                    previous_revision=state.revision,
                    new_revision=state.revision,
                    issues=[f"unsupported graph patch agent role: {patch.agent_role}"],
                )
            try:
                base_snapshot = self._snapshot_unlocked(patch.base_graph_revision)
            except GraphValidationError as exc:
                return GraphMergeResult(
                    operation_id=operation_id,
                    status="conflict",
                    base_revision=patch.base_graph_revision,
                    previous_revision=state.revision,
                    new_revision=state.revision,
                    issues=[str(exc)],
                )
            base_hashes = cast(dict[str, str], base_snapshot.get("node_hashes", {}))
            touched = {
                *(update.matek_id for update in patch.update_nodes),
                *(change.matek_id for change in patch.proposed_status_changes),
                *(edge.source_id for edge in [*patch.add_edges, *patch.remove_edges]),
            }
            proposed_ids = [item.matek_id for item in patch.create_nodes if item.matek_id]
            conflicts: list[str] = []
            immutable_target_id = self.main_claim_id(problem_id)
            if any(update.matek_id == immutable_target_id for update in patch.update_nodes):
                conflicts.append(
                    "the main target is immutable outside an explicit target migration"
                )
            if any(
                change.matek_id == immutable_target_id for change in patch.proposed_status_changes
            ):
                conflicts.append("worker graph patches cannot change the main target status")
            for node_id in touched:
                current = by_id.get(node_id)
                if current is None and node_id not in proposed_ids:
                    conflicts.append(f"patch touches missing node {node_id}")
                    continue
                if current is None:
                    continue
                if patch.base_graph_revision != state.revision and (
                    base_hashes.get(node_id) != current.content_hash
                ):
                    conflicts.append(
                        f"node {node_id} changed after base revision {patch.base_graph_revision}"
                    )
            if len(proposed_ids) != len(set(proposed_ids)):
                conflicts.append("patch proposes duplicate stable node IDs")
            conflicts.extend(
                f"proposed stable node ID already exists: {node_id}"
                for node_id in proposed_ids
                if node_id in by_id
            )
            existing_signatures = {
                (node.node_type, node.title.casefold().strip()): node.matek_id
                for node in by_id.values()
                if not node.tombstone
            }
            for item in patch.create_nodes:
                duplicate = existing_signatures.get((item.node_type, item.title.casefold().strip()))
                if duplicate is not None:
                    conflicts.append(
                        f"likely duplicate {item.node_type.value} node {item.title!r}: {duplicate}"
                    )
            if conflicts:
                return GraphMergeResult(
                    operation_id=operation_id,
                    status="conflict",
                    base_revision=patch.base_graph_revision,
                    previous_revision=state.revision,
                    new_revision=state.revision,
                    issues=list(dict.fromkeys(conflicts)),
                )
            now = self._now()
            changed: list[str] = []
            created_ids: list[str] = []
            for item in patch.create_nodes:
                if item.epistemic_status is EpistemicStatus.LEAN_VERIFIED:
                    conflicts.append(
                        "only deterministic Lean verification may create lean_verified evidence"
                    )
                    continue
                if (
                    item.epistemic_status is EpistemicStatus.AUDIT_PASSED
                    and not patch.agent_role.startswith("research-auditor")
                ):
                    conflicts.append(
                        "only a recorded independent audit may create audit_passed evidence"
                    )
                    continue
                if (
                    patch.agent_role == "research-worker"
                    and item.node_type is NodeType.CLAIM
                    and item.epistemic_status is EpistemicStatus.REFUTED
                ):
                    conflicts.append(
                        "a research worker cannot create an already-refuted claim; preserve "
                        "the counterexample as candidate evidence for independent review"
                    )
                    continue
                created_node_id = item.matek_id
                if created_node_id is None:
                    if item.node_type in NODE_ID_WORDS:
                        created_node_id = _descriptive_id(item.node_type, item.title, set(by_id))
                    else:
                        created_node_id = _new_id(item.node_type)
                node = GraphNode(
                    matek_id=created_node_id,
                    node_type=item.node_type,
                    problem_id=problem_id,
                    title=item.title,
                    epistemic_status=item.epistemic_status,
                    workflow_status=item.workflow_status,
                    claim_type=item.claim_type,
                    created_in_run=patch.run_id,
                    last_modified_run=patch.run_id,
                    author_role=patch.agent_role,
                    created_at=now,
                    updated_at=now,
                    body=new_generated_body(item.title, item.body),
                    tags=list(dict.fromkeys([f"matek/{item.node_type.value}", *item.tags])),
                    evidence=list(dict.fromkeys([*item.evidence, *patch.evidence])),
                    source_artifacts=item.source_artifacts,
                )
                by_id[created_node_id] = node
                changed.append(created_node_id)
                created_ids.append(created_node_id)
            if conflicts:
                return GraphMergeResult(
                    operation_id=operation_id,
                    status="rejected",
                    base_revision=patch.base_graph_revision,
                    previous_revision=state.revision,
                    new_revision=state.revision,
                    issues=list(dict.fromkeys(conflicts)),
                )
            for update in patch.update_nodes:
                node = by_id[update.matek_id]
                if update.title is not None:
                    node.title = update.title.strip()
                if update.body is not None:
                    old_statement = exact_statement(node.body)
                    node.body = replace_generated_section(node.body, node.title, update.body)
                    if (
                        node.node_type is NodeType.CLAIM
                        and exact_statement(node.body) != old_statement
                    ):
                        node.statement_version += 1
                        node.epistemic_status = EpistemicStatus.STALE
                        node.invalidation_reasons = list(
                            dict.fromkeys(
                                [*node.invalidation_reasons, "statement_changed_requires_reaudit"]
                            )
                        )
                if update.tags is not None:
                    node.tags = list(dict.fromkeys(update.tags))
                node.evidence = list(
                    dict.fromkeys([*node.evidence, *patch.evidence, *update.evidence])
                )
                node.source_artifacts = list(
                    dict.fromkeys([*node.source_artifacts, *update.source_artifacts])
                )
                node.updated_at = now
                node.last_modified_run = patch.run_id
                node.author_role = patch.agent_role
                changed.append(node.matek_id)
            remove_keys = {
                (edge.source_id, edge.relation, edge.target_id) for edge in patch.remove_edges
            }
            for source_id, relation, target_id in remove_keys:
                source = by_id[source_id]
                source.relations = [
                    edge
                    for edge in source.relations
                    if (edge.source_id, edge.relation, edge.target_id)
                    != (source_id, relation, target_id)
                ]
                source.updated_at = now
                source.last_modified_run = patch.run_id
                changed.append(source_id)
            for edge in patch.add_edges:
                if edge.target_id not in by_id:
                    conflicts.append(f"edge target does not exist: {edge.target_id}")
                    continue
                issue = self._relation_issue(edge, by_id)
                if issue:
                    conflicts.append(issue)
                    continue
                source = by_id[edge.source_id]
                source.relations = _unique_edges([*source.relations, edge])
                if edge.relation is RelationType.DEPENDS_ON:
                    target = by_id[edge.target_id]
                    version = (
                        f"{target.matek_id}@{target.statement_version}:"
                        f"{target.content_hash or sha256_text(render_node_note(target))}"
                    )
                    source.dependency_versions = list(
                        dict.fromkeys([*source.dependency_versions, version])
                    )
                source.updated_at = now
                source.last_modified_run = patch.run_id
                changed.append(source.matek_id)
            if conflicts:
                return GraphMergeResult(
                    operation_id=operation_id,
                    status="rejected",
                    base_revision=patch.base_graph_revision,
                    previous_revision=state.revision,
                    new_revision=state.revision,
                    issues=list(dict.fromkeys(conflicts)),
                )
            for change in patch.proposed_status_changes:
                node = by_id[change.matek_id]
                if change.epistemic_status is EpistemicStatus.LEAN_VERIFIED:
                    conflicts.append("only deterministic Lean verification may set lean_verified")
                    continue
                if (
                    change.epistemic_status is EpistemicStatus.AUDIT_PASSED
                    and not patch.agent_role.startswith("research-auditor")
                ):
                    conflicts.append("only a recorded independent audit may set audit_passed")
                    continue
                if (
                    change.epistemic_status is EpistemicStatus.REFUTED
                    and node.node_type is NodeType.CLAIM
                    and patch.agent_role == "research-worker"
                ):
                    conflicts.append(
                        "a research worker cannot mark a claim refuted without independent review"
                    )
                    continue
                if change.epistemic_status is not None and not self._epistemic_transition_allowed(
                    node.epistemic_status, change.epistemic_status
                ):
                    conflicts.append(
                        f"invalid epistemic transition for {node.matek_id}: "
                        f"{node.epistemic_status.value} -> {change.epistemic_status.value}"
                    )
                    continue
                if change.workflow_status is not None and not self._workflow_transition_allowed(
                    node.workflow_status, change.workflow_status
                ):
                    conflicts.append(
                        f"invalid workflow transition for {node.matek_id}: "
                        f"{node.workflow_status.value} -> {change.workflow_status.value}"
                    )
                    continue
                if change.epistemic_status is not None:
                    node.epistemic_status = change.epistemic_status
                if change.workflow_status is not None:
                    node.workflow_status = change.workflow_status
                node.updated_at = now
                node.last_modified_run = patch.run_id
                node.author_role = patch.agent_role
                node.evidence = list(dict.fromkeys([*node.evidence, *patch.evidence]))
                changed.append(node.matek_id)
            if conflicts:
                return GraphMergeResult(
                    operation_id=operation_id,
                    status="rejected",
                    base_revision=patch.base_graph_revision,
                    previous_revision=state.revision,
                    new_revision=state.revision,
                    issues=list(dict.fromkeys(conflicts)),
                )
            cycle = self._dependency_cycle(list(by_id.values()))
            if cycle is not None:
                return GraphMergeResult(
                    operation_id=operation_id,
                    status="rejected",
                    base_revision=patch.base_graph_revision,
                    previous_revision=state.revision,
                    new_revision=state.revision,
                    issues=["patch creates dependency cycle: " + " -> ".join(cycle)],
                )
            stale_seeds = [
                node_id
                for node_id in changed
                if by_id[node_id].epistemic_status
                in {EpistemicStatus.STALE, EpistemicStatus.REFUTED}
            ]
            stale = self._propagate_staleness(
                by_id,
                stale_seeds,
                (
                    "dependency_refuted_requires_reaudit"
                    if any(
                        by_id[node_id].epistemic_status is EpistemicStatus.REFUTED
                        for node_id in stale_seeds
                    )
                    else "dependency_changed_requires_reaudit"
                ),
            )
            changed = list(dict.fromkeys([*changed, *stale]))
            result = self._commit_nodes_unlocked(
                state=state,
                all_nodes=list(by_id.values()),
                changed_node_ids=changed,
                run_id=patch.run_id,
                author=patch.agent_role,
                reason=f"Merge validated graph patch for task {patch.task_id}.",
                operation_id=operation_id,
                source_artifacts=[
                    *patch.evidence,
                    *(
                        artifact
                        for item in patch.create_nodes
                        for artifact in item.source_artifacts
                    ),
                    *(
                        artifact
                        for item in patch.update_nodes
                        for artifact in item.source_artifacts
                    ),
                ],
                stale_node_ids=stale,
            )
            # Preserve the worker-supplied base in the returned audit record. The
            # durable transaction also records the actual previous revision.
            return result.model_copy(
                update={
                    "base_revision": patch.base_graph_revision,
                    "created_node_ids": list(
                        dict.fromkeys([*result.created_node_ids, *created_ids])
                    ),
                }
            )

    @staticmethod
    def main_claim_id(problem_id: str) -> str:
        return _deterministic_id(NodeType.CLAIM, problem_id, "main-target")

    def frozen_target_for_source(self, normalized_source_sha256: str) -> FrozenTarget:
        """Return the immutable target bound to one normalized source problem.

        Callers use this after :meth:`record_compiled_problem` so every downstream
        stage receives the canonical theorem bytes rather than a fresh compiler
        paraphrase.  Returning a deep copy keeps the registry itself immutable.
        """

        source_hash = normalized_source_sha256.strip().casefold()
        with self._locked():
            self._recover_pending_unlocked()
            try:
                registry = load_target_registry(self.target_registry_path)
            except TargetRegistryError as exc:
                raise GraphValidationError(f"cannot load the immutable main target: {exc}") from exc
            target = registry.targets.get(source_hash)
            if target is None:
                raise GraphValidationError(
                    f"no immutable main target is bound to normalized source hash {source_hash!r}"
                )
            return target.model_copy(deep=True)

    def record_compiled_problem(
        self,
        *,
        problem_id: str,
        run_id: str,
        compiled_problem: Mapping[str, Any],
        normalized_source_sha256: str | None = None,
        allow_target_migration: bool = False,
        target_migration_reason: str | None = None,
    ) -> GraphMergeResult:
        """Freeze the exact target and materialize its canonical verified sources."""

        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            by_id = {node.matek_id: node for node in nodes}
            problem = by_id.get(problem_id)
            if problem is None or problem.node_type is not NodeType.PROBLEM:
                raise GraphValidationError(f"problem node does not exist: {problem_id}")
            now = self._now()
            title = str(compiled_problem.get("title") or problem.title).strip()
            incoming_statement = str(
                compiled_problem.get("normalized_statement") or generated_section(problem.body)
            ).strip()
            incoming_contract = compiled_problem.get("claim_contract", {})
            incoming_prompt = str(compiled_problem.get("compiled_prompt") or "")
            source_hash_value = normalized_source_sha256 or problem.metadata.get(
                "matek_normalized_source_sha256"
            )
            source_hash = (
                str(source_hash_value).strip().casefold()
                if source_hash_value
                else sha256_text(generated_section(problem.body))
            )
            target_id = self.main_claim_id(problem_id)
            existing_target = by_id.get(target_id)
            try:
                target_registry = load_target_registry(self.target_registry_path)
                if existing_target is not None and source_hash not in target_registry.targets:
                    prior_targets = [
                        item
                        for item in target_registry.targets.values()
                        if item.target_node_id == target_id
                    ]
                    if prior_targets:
                        if not allow_target_migration:
                            raise TargetRegistryError(
                                "the user-authored problem changed; an explicit target migration "
                                "and reason are required"
                            )
                        prior = max(
                            prior_targets,
                            key=lambda item: (item.statement_version, item.established_run_id),
                        )
                        prior_title = prior.title
                        prior_statement = prior.exact_statement
                        prior_contract = json.loads(prior.canonical_contract_json)
                        prior_prompt = prior.compiled_prompt
                        prior_run_id = prior.established_run_id
                    else:
                        # One-time compatibility path for graphs created before the target
                        # registry existed.  The graph note, not fresh compiler prose, is the
                        # best available canonical target.
                        prior_contract_text = _generated_heading_value(
                            existing_target.body, "Scope and conventions"
                        )
                        prior_contract = incoming_contract
                        if prior_contract_text:
                            try:
                                prior_contract = json.loads(prior_contract_text)
                            except json.JSONDecodeError:
                                incoming_contract_hash = sha256_text(
                                    canonical_contract_json(incoming_contract)
                                )
                                recorded_contract_hash = existing_target.metadata.get(
                                    "matek_claim_contract_sha256"
                                )
                                if recorded_contract_hash != incoming_contract_hash:
                                    raise TargetRegistryError(
                                        "legacy target contract cannot be reconstructed safely"
                                    ) from None
                        prior_title = existing_target.title.removeprefix("Main target — ")
                        prior_statement = exact_statement(existing_target.body)
                        prior_prompt = incoming_prompt
                        prior_run_id = existing_target.created_in_run
                    target_registry, _ = bind_frozen_target(
                        target_registry,
                        normalized_source_sha256=source_hash,
                        target_node_id=target_id,
                        title=prior_title,
                        exact_statement=prior_statement,
                        claim_contract=prior_contract,
                        compiled_prompt=prior_prompt,
                        run_id=prior_run_id,
                    )

                # An unchanged normalized source hash is the deliberately cheap sanity
                # check for a normal repeat run.  Its frozen target is authoritative:
                # stochastic compiler wording and JSON layout must not enter the strict
                # migration comparison at all.
                existing_frozen = target_registry.targets.get(source_hash)
                if existing_frozen is not None and not allow_target_migration:
                    bind_title = existing_frozen.title
                    bind_statement = existing_frozen.exact_statement
                    bind_contract = json.loads(existing_frozen.canonical_contract_json)
                    bind_prompt = existing_frozen.compiled_prompt
                else:
                    bind_title = title
                    bind_statement = incoming_statement
                    bind_contract = incoming_contract
                    bind_prompt = incoming_prompt
                target_registry, binding = bind_frozen_target(
                    target_registry,
                    normalized_source_sha256=source_hash,
                    target_node_id=target_id,
                    title=bind_title,
                    exact_statement=bind_statement,
                    claim_contract=bind_contract,
                    compiled_prompt=bind_prompt,
                    run_id=run_id,
                    allow_material_migration=allow_target_migration,
                    migration_reason=target_migration_reason,
                )
            except (TargetRegistryError, ValueError) as exc:
                raise GraphTargetValidationError(
                    f"cannot bind the immutable main target: {exc}"
                ) from exc

            frozen = binding.target
            normalized_statement = frozen.exact_statement
            raw_contract = json.loads(frozen.canonical_contract_json)
            contract = frozen.canonical_contract_json
            literature_status = str(compiled_problem.get("literature_status", "unknown"))
            literature_summary = compiled_problem.get("literature_resolution_summary")
            problem.title = frozen.title
            problem.body = replace_generated_section(
                problem.body,
                frozen.title,
                "## Exact main problem\n\n"
                + normalized_statement
                + "\n\n## Claim contract\n\n```json\n"
                + json.dumps(raw_contract, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n```\n\n## Prior-literature status\n\n"
                + f"`{literature_status}`"
                + (f"\n\n{literature_summary}" if literature_summary else ""),
            )
            problem.last_modified_run = run_id
            problem.updated_at = now
            problem.metadata["matek_literature_status"] = literature_status
            problem.metadata["matek_normalized_source_sha256"] = source_hash
            problem.metadata["matek_target_registry"] = "target-registry.json"
            target = GraphNode(
                matek_id=target_id,
                node_type=NodeType.CLAIM,
                problem_id=problem_id,
                title=f"Main target — {frozen.title}",
                epistemic_status=EpistemicStatus.CONJECTURED,
                workflow_status=WorkflowStatus.ACTIVE,
                claim_type=ClaimType.THEOREM,
                created_in_run=run_id,
                last_modified_run=run_id,
                author_role="prompt-compiler",
                created_at=now,
                updated_at=now,
                body=new_generated_body(
                    f"Main target — {frozen.title}",
                    "## Exact statement\n\n"
                    + normalized_statement
                    + "\n\n## Scope and conventions\n\n"
                    + contract
                    + "\n\n## Current significance\n\n"
                    + "This is the exact claim governed by the compiled MATEK claim contract.",
                ),
                tags=["matek/claim", "matek/theorem", "matek/main-target"],
                relations=[
                    GraphEdge(
                        source_id=target_id,
                        relation=RelationType.CREATED_DURING,
                        target_id=_deterministic_id(NodeType.RUN, problem_id, run_id),
                    )
                ],
                source_artifacts=[
                    f".matek/runs/{run_id}/prompts/compiled_problem.json",
                    f".matek/runs/{run_id}/prompts/compiled_research_prompt.md",
                    f".matek/knowledge/{self.graph_name}/target-registry.json",
                ],
                metadata={
                    "matek_claim_contract_sha256": frozen.contract_sha256,
                    "matek_normalized_source_sha256": source_hash,
                    "matek_target_semantic_sha256": target_semantic_fingerprint(frozen),
                    "matek_target_registry_version": frozen.statement_version,
                },
            )
            stale_nodes: list[str] = []
            if existing_target is not None:
                target.statement_version = frozen.statement_version
                if binding.disposition is TargetBindingDisposition.MIGRATED:
                    target.epistemic_status = EpistemicStatus.STALE
                    target.invalidation_reasons = ["statement_changed_requires_reaudit"]
                    stale_nodes = self._propagate_staleness(
                        by_id,
                        [target_id],
                        "dependency_changed_requires_reaudit",
                    )
                else:
                    target.epistemic_status = existing_target.epistemic_status
                    target.workflow_status = existing_target.workflow_status
                    target.invalidation_reasons = list(existing_target.invalidation_reasons)
            proposed: list[GraphNode] = [problem, target]
            source_nodes: list[GraphNode] = []
            source_entities: dict[str, Any] = {}
            source_identity_decisions: list[dict[str, object]] = []
            raw_sources = compiled_problem.get("source_ledger", [])
            if isinstance(raw_sources, list):
                for raw_source in raw_sources:
                    if not isinstance(raw_source, Mapping):
                        continue
                    identifiers = [
                        str(item) for item in raw_source.get("identifiers", []) if str(item).strip()
                    ]
                    raw_authors = raw_source.get("authors", [])
                    authors = (
                        [str(item) for item in raw_authors if str(item).strip()]
                        if isinstance(raw_authors, list)
                        else []
                    )
                    verified = bool(raw_source.get("verified", False))
                    source_title = str(
                        raw_source.get("title") or raw_source.get("source_id") or "Untitled source"
                    ).strip()
                    try:
                        entity = make_source_entity(
                            title=source_title,
                            identifiers=identifiers,
                            authors=authors,
                            source_alias=(str(raw_source.get("source_id") or "").strip() or None),
                            verification_provenance=[
                                str(raw_source.get("verification_detail") or "").strip()
                            ],
                            verified=verified,
                        )
                    except (SourceCanonicalizationError, ValueError) as exc:
                        raise GraphValidationError(
                            f"compiled source ledger entry has invalid identity: {exc}"
                        ) from exc
                    versions = split_source_entity_by_doi(entity)
                    if len(versions) > 1:
                        source_identity_decisions.append(
                            _source_identity_decision(
                                context="compiled_source_ledger",
                                identifiers=entity.identifiers,
                                aliases=entity.aliases,
                                candidate_node_ids=_source_candidate_node_ids(
                                    by_id.values(),
                                    problem_id=problem_id,
                                    identifiers=entity.identifiers,
                                ),
                            )
                        )
                    for version in versions:
                        previous_entity = source_entities.get(version.source_key)
                        source_entities[version.source_key] = (
                            merge_source_entities(previous_entity, version)
                            if previous_entity is not None
                            else version
                        )

                for source_key, entity in sorted(source_entities.items()):
                    deterministic_source_id = _deterministic_id(
                        NodeType.SOURCE,
                        problem_id,
                        source_key,
                    )
                    (
                        compatible_existing,
                        blocked_direct,
                        candidate_decisions,
                    ) = _compatible_existing_sources(
                        existing_nodes=by_id.values(),
                        problem_id=problem_id,
                        incoming=entity,
                        deterministic_source_id=deterministic_source_id,
                        context="compiled_existing_source_upgrade",
                        require_verified_overlap=True,
                    )
                    source_identity_decisions.extend(candidate_decisions)
                    compatible_existing.sort(
                        key=lambda item: (
                            item[0].matek_id != deterministic_source_id,
                            item[0].created_at,
                            item[0].matek_id,
                        )
                    )
                    existing_source = compatible_existing[0][0] if compatible_existing else None
                    if compatible_existing:
                        prior_entities = [
                            *(item for _, item in compatible_existing),
                            entity,
                        ]
                        if all(item.source_key == source_key for item in prior_entities):
                            merged_entity = prior_entities[0]
                            for next_entity in prior_entities[1:]:
                                merged_entity = merge_source_entities(
                                    merged_entity,
                                    next_entity,
                                )
                            entity = merged_entity
                        else:
                            entity = _merge_compatible_source_identities(prior_entities)
                    source_id = (
                        existing_source.matek_id
                        if existing_source is not None
                        else (
                            _deterministic_id(
                                NodeType.SOURCE,
                                problem_id,
                                source_key,
                                "doi-version",
                            )
                            if blocked_direct
                            else deterministic_source_id
                        )
                    )
                    superseded_source_ids = [
                        candidate.matek_id
                        for candidate, _ in compatible_existing
                        if candidate.matek_id != source_id
                    ]
                    source = GraphNode(
                        matek_id=source_id,
                        node_type=NodeType.SOURCE,
                        problem_id=problem_id,
                        title=entity.titles[0],
                        epistemic_status=(
                            EpistemicStatus.AUDIT_PASSED
                            if entity.verified
                            else EpistemicStatus.OPEN
                        ),
                        workflow_status=(
                            WorkflowStatus.COMPLETE if entity.verified else WorkflowStatus.ACTIVE
                        ),
                        created_in_run=(
                            existing_source.created_in_run
                            if existing_source is not None
                            else run_id
                        ),
                        last_modified_run=run_id,
                        author_role="prompt-source-verifier",
                        created_at=(
                            existing_source.created_at if existing_source is not None else now
                        ),
                        updated_at=now,
                        body=new_generated_body(
                            entity.titles[0],
                            "## Stable identifiers\n\n"
                            + ("\n".join(f"- `{item}`" for item in entity.identifiers) or "_None._")
                            + "\n\n## Identifier revisions\n\n"
                            + (
                                "\n".join(f"- `{item}`" for item in entity.identifier_revisions)
                                or "_None._"
                            )
                            + "\n\n## Verification\n\n"
                            + (
                                "\n".join(entity.verification_provenance)
                                or (
                                    "Verified."
                                    if entity.verified
                                    else "Not independently verified."
                                )
                            ),
                        ),
                        tags=[
                            "matek/source",
                            "matek/source-verified" if entity.verified else "matek/source-open",
                        ],
                        relations=_unique_edges(
                            [
                                *(existing_source.relations if existing_source is not None else []),
                                *(
                                    GraphEdge(
                                        source_id=source_id,
                                        relation=RelationType.SUPERSEDES,
                                        target_id=superseded_id,
                                    )
                                    for superseded_id in superseded_source_ids
                                ),
                            ]
                        ),
                        source_artifacts=list(
                            dict.fromkeys(
                                [
                                    *(
                                        existing_source.source_artifacts
                                        if existing_source is not None
                                        else []
                                    ),
                                    f".matek/runs/{run_id}/prompts/source_ledger.json",
                                ]
                            )
                        ),
                        evidence=(
                            list(existing_source.evidence) if existing_source is not None else []
                        ),
                        metadata={
                            "matek_source_id": entity.source_key,
                            "matek_primary_identifier": entity.primary_identifier,
                            "matek_identifiers": entity.identifiers,
                            "matek_identifier_revisions": entity.identifier_revisions,
                            "matek_source_aliases": entity.aliases,
                            "matek_source_titles": entity.titles,
                            "matek_source_authors": entity.authors,
                            "matek_verified": entity.verified,
                        },
                    )
                    source_nodes.append(source)
                    source_nodes.extend(
                        _superseded_source_alias(
                            candidate,
                            canonical_source_id=source_id,
                            canonical_source_key=entity.source_key,
                            run_id=run_id,
                            now=now,
                        )
                        for candidate, _ in compatible_existing
                        if candidate.matek_id != source_id
                    )
                    target.relations.append(
                        GraphEdge(
                            source_id=target_id,
                            relation=RelationType.CITES,
                            target_id=source_id,
                        )
                    )
            proposed.extend(source_nodes)
            unique_identity_decisions = {
                _canonical_json(decision): decision for decision in source_identity_decisions
            }
            source_identity_decisions = list(unique_identity_decisions.values())
            source_identity_writes: dict[str, str] = {}
            source_identity_artifacts: list[str] = []
            if source_identity_decisions:
                decision_path, decision_contents = _source_identity_decision_artifact(
                    graph_name=self.graph_name,
                    problem_id=problem_id,
                    run_id=run_id,
                    timestamp=now,
                    decisions=source_identity_decisions,
                )
                source_identity_writes[decision_path] = decision_contents
                source_identity_artifacts.append(decision_path)
            return self._upsert_generated_nodes_unlocked(
                state=state,
                nodes=by_id,
                proposed=proposed,
                run_id=run_id,
                author="prompt-compiler",
                reason="Compile the exact target and source ledger into the persistent graph.",
                operation_id=f"prompt-compiled:{run_id}",
                source_artifacts=[
                    f".matek/runs/{run_id}/prompts/compiled_problem.json",
                    *source_identity_artifacts,
                ],
                stale_node_ids=[target_id, *stale_nodes] if stale_nodes else (),
                additional_writes={
                    self.target_registry_path.relative_to(self.vault_root).as_posix(): (
                        render_target_registry(target_registry)
                    ),
                    **source_identity_writes,
                },
                issues=[_source_identity_issue(decision) for decision in source_identity_decisions],
            )

    def record_assignment_tasks(
        self,
        *,
        problem_id: str,
        run_id: str,
        decision_id: int,
        assignments: Sequence[Mapping[str, Any]],
        allow_legacy_default_targets: bool = False,
    ) -> tuple[dict[str, str], dict[str, GraphContextSlice], str]:
        """Create graph-scoped task nodes for one coordinator decision."""

        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes_list = self._load_nodes_unlocked(include_human_notes=True)
            by_id = {node.matek_id: node for node in nodes_list}
            if problem_id not in by_id:
                raise GraphValidationError(f"problem node does not exist: {problem_id}")
            run_node_id = _deterministic_id(NodeType.RUN, problem_id, run_id)
            now = self._now()
            proposed: list[GraphNode] = []
            assignment_to_task: dict[str, str] = {}
            for assignment in assignments:
                assignment_id = str(assignment.get("id") or "").strip()
                if not assignment_id:
                    raise GraphValidationError("research assignment has no stable ID")
                task_id = _deterministic_id(NodeType.TASK, problem_id, run_id, assignment_id)
                raw_targets = assignment.get("target_node_ids", [])
                if not isinstance(raw_targets, list):
                    raise GraphValidationError(
                        f"research assignment {assignment_id!r} has invalid target_node_ids"
                    )
                if allow_legacy_default_targets and not raw_targets:
                    target_default = (
                        self.main_claim_id(problem_id)
                        if self.main_claim_id(problem_id) in by_id
                        else problem_id
                    )
                    raw_targets = [target_default]
                target_ids = self._validate_assignment_target_ids_unlocked(
                    by_id,
                    problem_id=problem_id,
                    assignment_id=assignment_id,
                    target_node_ids=[str(item) for item in raw_targets],
                )
                task_text = str(assignment.get("task") or "Research assignment").strip()
                expected = str(assignment.get("expected_output") or "Concrete mathematical result")
                stop = str(
                    assignment.get("stopping_condition")
                    or "Return concrete content or an exact obstruction."
                )
                relations = [
                    *(
                        GraphEdge(
                            source_id=task_id,
                            relation=RelationType.TARGETS,
                            target_id=target_id,
                        )
                        for target_id in target_ids
                    ),
                    GraphEdge(
                        source_id=task_id,
                        relation=RelationType.CREATED_DURING,
                        target_id=run_node_id,
                    ),
                ]
                proposed.append(
                    GraphNode(
                        matek_id=task_id,
                        node_type=NodeType.TASK,
                        problem_id=problem_id,
                        title=f"Task {assignment_id}: {task_text[:72]}",
                        epistemic_status=EpistemicStatus.OPEN,
                        workflow_status=WorkflowStatus.QUEUED,
                        created_in_run=run_id,
                        last_modified_run=run_id,
                        author_role="research-coordinator",
                        created_at=now,
                        updated_at=now,
                        body=new_generated_body(
                            f"Task {assignment_id}",
                            "## Exact requested task\n\n"
                            + task_text
                            + "\n\n## Expected output\n\n"
                            + expected
                            + "\n\n## Stopping condition\n\n"
                            + stop
                            + "\n\n## Approach family\n\n"
                            + str(assignment.get("approach_family") or "unspecified"),
                        ),
                        tags=["matek/task", "matek/task-active"],
                        relations=relations,
                        source_artifacts=[
                            f".matek/runs/{run_id}/research/coordinator/decisions/"
                            f"{decision_id:08d}.json"
                        ],
                        metadata={
                            "matek_assignment_id": assignment_id,
                            "matek_decision_id": decision_id,
                            "matek_priority": "high"
                            if "audit" in task_text.casefold()
                            else "normal",
                        },
                    )
                )
                assignment_to_task[assignment_id] = task_id
            if proposed:
                self._upsert_generated_nodes_unlocked(
                    state=state,
                    nodes=by_id,
                    proposed=proposed,
                    run_id=run_id,
                    author="research-coordinator",
                    reason=f"Create graph-scoped tasks from coordinator decision {decision_id}.",
                    operation_id=f"coordinator-tasks:{run_id}:{decision_id}",
                )
                state = self._load_state_unlocked()
                nodes_list = self._load_nodes_unlocked(include_human_notes=True)
            contexts = {
                assignment_id: self._context_slice_unlocked(
                    state,
                    nodes_list,
                    problem_id=problem_id,
                    task_id=task_id,
                )
                for assignment_id, task_id in assignment_to_task.items()
            }
            return assignment_to_task, contexts, state.revision

    @staticmethod
    def _validate_assignment_target_ids_unlocked(
        nodes: Mapping[str, GraphNode],
        *,
        problem_id: str,
        assignment_id: str,
        target_node_ids: Sequence[str],
    ) -> list[str]:
        """Bind one assignment to explicit live nodes without a silent fallback."""

        normalized: list[str] = []
        for item in target_node_ids:
            if not item.strip():
                continue
            try:
                normalized.append(validate_any_node_id(item))
            except ValueError:
                normalized.append(item.strip())
        if not normalized:
            raise GraphValidationError(
                f"research assignment {assignment_id!r} must name at least one graph target"
            )
        if len(normalized) != len(set(normalized)):
            raise GraphValidationError(
                f"research assignment {assignment_id!r} repeats a graph target"
            )
        issues: list[str] = []
        for node_id in normalized:
            node = nodes.get(node_id)
            if node is None:
                issues.append(unknown_id_message("unknown target ", [node_id], nodes.keys()))
            elif node.problem_id != problem_id and node.matek_id != problem_id:
                issues.append(f"target {node_id} belongs to another problem")
            elif node.tombstone:
                issues.append(f"target {node_id} is tombstoned")
            elif node.node_type in {NodeType.RUN, NodeType.ARTIFACT, NodeType.HUMAN_NOTE}:
                issues.append(
                    f"target {node_id} is a {node.node_type.value} node, not a research branch"
                )
        if issues:
            raise GraphValidationError(
                f"research assignment {assignment_id!r} has invalid graph targets: "
                + "; ".join(issues)
            )
        return normalized

    def validate_assignment_targets(
        self,
        *,
        problem_id: str,
        assignments: Sequence[Mapping[str, Any]],
    ) -> None:
        """Validate coordinator branch bindings before its decision becomes durable."""

        with self._locked():
            self._recover_pending_unlocked()
            self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            by_id = {node.matek_id: node for node in nodes}
            if problem_id not in by_id:
                raise GraphValidationError(f"problem node does not exist: {problem_id}")
            for assignment in assignments:
                assignment_id = str(assignment.get("id") or "").strip()
                raw_targets = assignment.get("target_node_ids", [])
                if not isinstance(raw_targets, list):
                    raise GraphValidationError(
                        f"research assignment {assignment_id!r} has invalid target_node_ids"
                    )
                self._validate_assignment_target_ids_unlocked(
                    by_id,
                    problem_id=problem_id,
                    assignment_id=assignment_id,
                    target_node_ids=[str(item) for item in raw_targets],
                )

    def coordinator_memory(
        self,
        problem_id: str,
        *,
        current_run_id: str | None = None,
        resume_reconstruction: bool = False,
        previous_coordinator_revision: str | None = None,
    ) -> dict[str, object]:
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            frontier = self._frontier_unlocked(state, nodes, problem_id)
            problem_nodes = [node for node in nodes if node.problem_id == problem_id]
            prior_nodes = [
                node
                for node in problem_nodes
                if current_run_id is None or node.created_in_run != current_run_id
            ]
            node_type_counts = {
                node_type.value: sum(node.node_type is node_type for node in problem_nodes)
                for node_type in NodeType
                if any(node.node_type is node_type for node in problem_nodes)
            }
            branch_status_counts = {
                status.value: sum(
                    node.node_type is NodeType.APPROACH and node.workflow_status is status
                    for node in problem_nodes
                )
                for status in WorkflowStatus
                if any(
                    node.node_type is NodeType.APPROACH and node.workflow_status is status
                    for node in problem_nodes
                )
            }
            review_required = bool(prior_nodes) or resume_reconstruction
            return {
                "graph_revision": state.revision,
                "problem_id": problem_id,
                "review_required_before_delegation": review_required,
                "current_frontier_review_required": True,
                "resume_reconstruction": resume_reconstruction,
                "previous_coordinator_graph_revision": previous_coordinator_revision,
                "graph_changed_since_previous_coordinator_activation": (
                    previous_coordinator_revision is not None
                    and previous_coordinator_revision != state.revision
                ),
                "overview": {
                    "node_count": len(problem_nodes),
                    "edge_count": sum(len(node.relations) for node in problem_nodes),
                    "prior_node_count": len(prior_nodes),
                    "node_type_counts": node_type_counts,
                    "approach_branch_status_counts": branch_status_counts,
                },
                "graph_root": self.graph_root.relative_to(self.project_root).as_posix(),
                "index_path": self.index_path.relative_to(self.project_root).as_posix(),
                "frontier": frontier.model_dump(mode="json"),
                "instruction": (
                    "Reconstruct the current branch map from this overview and frontier before "
                    "making the decision. On initial delegation, resume, and every later "
                    "activation, use prior results, failures, gaps, audits, active tasks, and "
                    "cross-branch dependencies to shape delegation and synthesis. Use stable "
                    "node IDs in every assignment's target_node_ids. Do not reopen a blocked or "
                    "refuted branch unless new evidence or a mechanism addressing its recorded "
                    "failure is stated explicitly. Include this exact graph revision in the "
                    "decision rationale as the review attestation: "
                    f"{state.revision}."
                ),
            }

    def integrate_worker_report(
        self,
        *,
        problem_id: str,
        run_id: str,
        assignment: Mapping[str, Any],
        task_id: str,
        report: Mapping[str, Any],
        proposed_patch: GraphPatch | None,
        source_artifact: str,
        operation_id: str,
        computation_evidence: Mapping[str, Any] | None = None,
    ) -> GraphMergeResult:
        """Merge an agent patch, then always preserve a distilled worker summary."""

        proposal_issues: list[str] = []
        proposal_result: GraphMergeResult | None = None
        typed_report_v2 = report.get("schema_version") == 2 and isinstance(
            report.get("results"), list
        )
        if proposed_patch is not None and typed_report_v2:
            proposal_issues.append(
                "typed scientific reports cannot contain model-authored graph mutations"
            )
        elif proposed_patch is not None:
            if proposed_patch.run_id != run_id:
                proposal_issues.append("worker graph patch run_id does not match its run")
            elif proposed_patch.task_id != task_id:
                proposal_issues.append("worker graph patch task_id does not match its assignment")
            elif proposed_patch.agent_role != "research-worker":
                proposal_issues.append(
                    "worker graph patch agent_role must be exactly research-worker"
                )
            else:
                proposal_result = self.merge_patch(
                    proposed_patch,
                    problem_id=problem_id,
                    operation_id=f"{operation_id}:proposal",
                )
                if not proposal_result.committed:
                    proposal_issues.extend(proposal_result.issues)
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            if operation_id in state.processed_operations:
                previous = state.processed_operations[operation_id]
                return previous.model_copy(update={"status": "already_applied"})
            nodes_list = self._load_nodes_unlocked(include_human_notes=True)
            by_id = {node.matek_id: node for node in nodes_list}
            task = by_id.get(task_id)
            if task is None or task.node_type is not NodeType.TASK:
                raise GraphValidationError(f"worker graph task is missing: {task_id}")
            target_ids = [
                edge.target_id
                for edge in task.relations
                if edge.relation is RelationType.TARGETS and edge.target_id in by_id
            ]
            if not target_ids:
                raise GraphValidationError(
                    f"worker graph task has no valid branch targets: {task_id}"
                )
            now = self._now()
            assignment_id = str(assignment.get("id") or report.get("assignment_id") or "unknown")
            task_assignment_id = task.metadata.get("matek_assignment_id")
            if isinstance(task_assignment_id, str) and task_assignment_id != assignment_id:
                raise GraphValidationError(
                    "worker report assignment does not match its server-owned graph task"
                )
            family = str(assignment.get("approach_family") or "unspecified")
            mechanism = str(report.get("mechanism") or assignment.get("task") or family)
            typed_results: list[ScientificResult] = []
            typed_obligations: list[ScientificObligationDeclaration] = []
            verified_computation_evidence: Any | None = None
            if typed_report_v2:
                try:
                    # Local import avoids a module cycle while enforcing the complete
                    # provider-visible envelope, including assignment identity and all
                    # cross-result branch invariants.
                    from ..stages.research import ResearchWorkerReport

                    validated_report = ResearchWorkerReport.model_validate(report)
                except ValidationError as exc:
                    raise GraphValidationError(
                        f"typed scientific report is invalid: {exc}"
                    ) from exc
                if validated_report.assignment_id != assignment_id:
                    raise GraphValidationError(
                        "typed scientific report assignment_id does not match its "
                        "server-owned assignment"
                    )
                typed_results = list(validated_report.results)
                typed_obligations = list(validated_report.unresolved_obligations)
                status = validated_report.branch_outcome.value
                formal_results = [
                    item.exact_statement
                    for item in typed_results
                    if item.kind.value != "counterexample"
                ]
                counterexamples = [
                    item.exact_statement
                    for item in typed_results
                    if item.kind.value == "counterexample"
                ]
                gaps = [
                    item
                    for item in [
                        *(result.exact_gap for result in typed_results),
                        *(item.exact_statement for item in typed_obligations),
                    ]
                    if item
                ]
                exact_gap = "\n".join(dict.fromkeys(gaps))
                dependencies = list(
                    dict.fromkeys(
                        dependency
                        for result in typed_results
                        for dependency in result.dependency_node_ids
                    )
                )
                assumptions = list(
                    dict.fromkeys(
                        assumption for result in typed_results for assumption in result.assumptions
                    )
                )
                if computation_evidence is not None:
                    expected_computation_artifact = (
                        f".matek/runs/{run_id}/research/worker-computation/{assignment_id}.json"
                    )
                    if (
                        str(computation_evidence.get("source_artifact") or "")
                        != expected_computation_artifact
                    ):
                        raise GraphValidationError(
                            "computation evidence is not bound to the canonical run artifact"
                        )
                    supplied_evidence = {
                        str(key): value
                        for key, value in computation_evidence.items()
                        if key != "source_artifact"
                    }
                    try:
                        from ..stages.common import canonical_json_bytes
                        from ..stages.computation_artifacts import (
                            verify_persisted_computation_evidence,
                        )

                        evidence_path = ensure_path_confined(
                            self.project_root,
                            self.project_root / expected_computation_artifact,
                        )
                        verified_computation_evidence = verify_persisted_computation_evidence(
                            self.project_root / ".matek" / "runs" / run_id,
                            assignment_id,
                            evidence_path,
                        )
                    except (OSError, ValueError) as exc:
                        raise GraphValidationError(
                            f"persisted computation evidence failed verification: {exc}"
                        ) from exc
                    if supplied_evidence != verified_computation_evidence.model_dump(mode="json"):
                        raise GraphValidationError(
                            "caller computation evidence differs from its canonical run artifact"
                        )
                    collection = verified_computation_evidence.collection
                    if collection is not None and collection.trusted:
                        assert collection.manifest is not None
                        reported_declaration_hashes = [
                            hashlib.sha256(canonical_json_bytes(item)).hexdigest()
                            for item in validated_report.artifact_manifest
                        ]
                        committed_declaration_hashes = [
                            item.declaration_sha256 for item in collection.manifest.declarations
                        ]
                        if reported_declaration_hashes != committed_declaration_hashes:
                            raise GraphValidationError(
                                "computation manifest declarations differ from the frozen "
                                "scientific report"
                            )
            else:
                if computation_evidence is not None:
                    raise GraphValidationError(
                        "computation evidence requires a typed scientific report"
                    )
                status = str(report.get("status") or "progress")
                formal_results = [
                    str(item) for item in report.get("formal_results", []) if str(item).strip()
                ]
                exact_gap = str(report.get("exact_gap") or "").strip()
                counterexamples = [
                    str(item) for item in report.get("counterexamples", []) if str(item).strip()
                ]
                dependencies = [
                    str(item) for item in report.get("dependencies", []) if str(item).strip()
                ]
                assumptions = [
                    str(item) for item in report.get("assumptions", []) if str(item).strip()
                ]
            classification = {
                "blocked": "blocked_local_gap",
                "refuted": "ruled_out_branch",
                "candidate_complete": "candidate",
            }.get(status, "partial_progress")
            # A family is a search taxonomy, not a branch identity. Distinct
            # assignments in the same family must retain distinct outcomes.
            approach_id = _descriptive_id(NodeType.APPROACH, family, set(by_id))
            approach_workflow = {
                "blocked": WorkflowStatus.BLOCKED,
                "refuted": WorkflowStatus.ABANDONED,
                "candidate_complete": WorkflowStatus.COMPLETE,
            }.get(status, WorkflowStatus.ACTIVE)
            approach_epistemic = {
                "refuted": EpistemicStatus.REFUTED,
                "candidate_complete": EpistemicStatus.CANDIDATE,
            }.get(status, EpistemicStatus.OPEN)
            partial = "\n".join(f"- {item}" for item in formal_results) or "_None established._"
            failure = exact_gap or (
                "\n".join(f"- {item}" for item in counterexamples)
                if status == "refuted" and counterexamples
                else "The assigned branch was ruled out, but no sharper obstruction was recorded."
                if status == "refuted"
                else "No exact failure recorded."
            )
            reopen = (
                "Reopen only if a new mechanism resolves the exact gap or defeats the recorded "
                "counterexample."
                if status in {"blocked", "refuted"}
                else "Continue when a coordinator task targets the remaining obligation."
            )
            approach = GraphNode(
                matek_id=approach_id,
                node_type=NodeType.APPROACH,
                problem_id=problem_id,
                title=f"Approach branch {assignment_id}: {family}",
                epistemic_status=approach_epistemic,
                workflow_status=approach_workflow,
                created_in_run=run_id,
                last_modified_run=run_id,
                author_role="research-worker",
                created_at=now,
                updated_at=now,
                body=new_generated_body(
                    f"Approach: {family}",
                    "## Exact route attempted\n\n"
                    + mechanism
                    + "\n\n## Stable branch targets\n\n"
                    + "\n".join(f"- {node_id}" for node_id in target_ids)
                    + "\n\n## Proposed invariant or mechanism\n\n"
                    + mechanism
                    + "\n\n## Strongest valid partial result\n\n"
                    + partial
                    + "\n\n## Exact failure point\n\n"
                    + failure
                    + "\n\n## Classification\n\n"
                    + f"`{classification}`"
                    + "\n\n## Reopen condition\n\n"
                    + reopen,
                ),
                tags=["matek/approach", f"matek/{classification}"],
                relations=[
                    GraphEdge(
                        source_id=approach_id,
                        relation=RelationType.CREATED_DURING,
                        target_id=_deterministic_id(NodeType.RUN, problem_id, run_id),
                    ),
                    GraphEdge(
                        source_id=approach_id,
                        relation=RelationType.RELATED_TO,
                        target_id=task_id,
                    ),
                    *(
                        GraphEdge(
                            source_id=approach_id,
                            relation=(
                                RelationType.SPECIALIZES
                                if by_id[target_id].node_type is NodeType.APPROACH
                                else RelationType.RELATED_TO
                            ),
                            target_id=target_id,
                        )
                        for target_id in target_ids
                    ),
                ],
                source_artifacts=[source_artifact],
                evidence=[*formal_results, *counterexamples],
                metadata={
                    "matek_assignment_ids": [assignment_id],
                    "matek_branch_target_ids": target_ids,
                    "matek_assumptions": assumptions,
                    "matek_dependencies": dependencies,
                    "matek_worker_status": status,
                    "matek_reopen_condition": reopen,
                },
            )
            if typed_report_v2:
                computation_nodes: list[GraphNode] = []
                if verified_computation_evidence is not None:
                    raw_collection = (
                        verified_computation_evidence.collection.model_dump(mode="json")
                        if verified_computation_evidence.collection is not None
                        else None
                    )
                    raw_replay = (
                        verified_computation_evidence.replay.model_dump(mode="json")
                        if verified_computation_evidence.replay is not None
                        else None
                    )
                    evidence_artifact = (
                        f".matek/runs/{run_id}/research/worker-computation/{assignment_id}.json"
                    )
                    if raw_collection is not None and not isinstance(raw_collection, Mapping):
                        raise GraphValidationError(
                            "computation collection evidence must be a structured object"
                        )
                    if raw_replay is not None and not isinstance(raw_replay, Mapping):
                        raise GraphValidationError(
                            "computation replay evidence must be a structured object"
                        )
                    manifest = (
                        raw_collection.get("manifest")
                        if isinstance(raw_collection, Mapping)
                        else None
                    )
                    if manifest is not None and not isinstance(manifest, Mapping):
                        raise GraphValidationError(
                            "computation manifest evidence must be a structured object"
                        )
                    if isinstance(manifest, Mapping):
                        collection_status = str(
                            raw_collection.get("status")
                            if isinstance(raw_collection, Mapping)
                            else ""
                        )
                        if collection_status not in {"collected", "reused"}:
                            raise GraphValidationError(
                                "a computation manifest requires a successful collection status"
                            )
                        manifest_assignment = str(manifest.get("assignment_id") or "")
                        manifest_sha256 = str(manifest.get("manifest_sha256") or "")
                        if manifest_assignment != assignment_id or not re.fullmatch(
                            r"[0-9a-f]{64}", manifest_sha256
                        ):
                            raise GraphValidationError(
                                "computation manifest identity does not match its assignment"
                            )
                        replay_status = (
                            str(raw_replay.get("status") or "not_collected")
                            if isinstance(raw_replay, Mapping)
                            else "not_collected"
                        )
                        replay_record_sha256 = (
                            str(raw_replay.get("record_sha256") or "")
                            if isinstance(raw_replay, Mapping)
                            else ""
                        )
                        if isinstance(raw_replay, Mapping) and (
                            str(raw_replay.get("assignment_id") or "") != assignment_id
                            or str(raw_replay.get("manifest_sha256") or "") != manifest_sha256
                        ):
                            raise GraphValidationError(
                                "computation replay identity does not match its manifest"
                            )
                        replay_passed = replay_status == "passed" and bool(
                            re.fullmatch(r"[0-9a-f]{64}", replay_record_sha256)
                        )
                        raw_declarations = manifest.get("declarations", [])
                        if not isinstance(raw_declarations, list):
                            raise GraphValidationError(
                                "computation manifest declarations must be a list"
                            )
                        declared_keys = {
                            str(key)
                            for declaration in raw_declarations
                            if isinstance(declaration, Mapping)
                            for raw_keys in [declaration.get("supporting_result_keys", [])]
                            if isinstance(raw_keys, list)
                            for key in raw_keys
                        }
                        manifest_id = _deterministic_id(
                            NodeType.ARTIFACT,
                            problem_id,
                            run_id,
                            assignment_id,
                            manifest_sha256,
                            "manifest",
                        )
                        manifest_path = str(
                            raw_collection.get("manifest_path") or ""
                            if isinstance(raw_collection, Mapping)
                            else ""
                        )
                        manifest_node = GraphNode(
                            matek_id=manifest_id,
                            node_type=NodeType.ARTIFACT,
                            problem_id=problem_id,
                            title=f"Computation manifest {assignment_id}",
                            epistemic_status=(
                                EpistemicStatus.AUDIT_PASSED
                                if replay_passed
                                else EpistemicStatus.OPEN
                            ),
                            workflow_status=(
                                WorkflowStatus.COMPLETE if replay_passed else WorkflowStatus.BLOCKED
                            ),
                            created_in_run=run_id,
                            last_modified_run=run_id,
                            author_role="computation-collector",
                            created_at=now,
                            updated_at=now,
                            body=new_generated_body(
                                f"Computation manifest {assignment_id}",
                                "## Immutable manifest\n\n"
                                + (f"`{manifest_path}`" if manifest_path else evidence_artifact)
                                + "\n\n## Application-computed SHA-256\n\n"
                                + f"`{manifest_sha256}`"
                                + "\n\n## Retained files\n\n"
                                + str(manifest.get("retained_file_count", 0))
                                + " files, "
                                + str(manifest.get("retained_total_bytes", 0))
                                + " bytes.\n\n## Replay status\n\n"
                                + f"`{replay_status}`",
                            ),
                            tags=["matek/artifact", "matek/computation", "matek/immutable"],
                            relations=[
                                GraphEdge(
                                    source_id=manifest_id,
                                    relation=RelationType.RELATED_TO,
                                    target_id=task_id,
                                ),
                                GraphEdge(
                                    source_id=manifest_id,
                                    relation=RelationType.CREATED_DURING,
                                    target_id=_deterministic_id(NodeType.RUN, problem_id, run_id),
                                ),
                            ],
                            source_artifacts=list(
                                dict.fromkeys(
                                    [
                                        evidence_artifact,
                                        *([manifest_path] if manifest_path else []),
                                    ]
                                )
                            ),
                            metadata={
                                "matek_assignment_id": assignment_id,
                                "matek_computation_manifest_sha256": manifest_sha256,
                                "matek_computation_replay_status": replay_status,
                                "matek_replay_passed": replay_passed,
                                "matek_supporting_result_keys": sorted(declared_keys),
                            },
                        )
                        computation_nodes.append(manifest_node)
                        replay_id: str | None = None
                        if isinstance(raw_replay, Mapping) and replay_record_sha256:
                            if not re.fullmatch(r"[0-9a-f]{64}", replay_record_sha256):
                                raise GraphValidationError(
                                    "computation replay evidence has an invalid record digest"
                                )
                            replay_id = _deterministic_id(
                                NodeType.ARTIFACT,
                                problem_id,
                                run_id,
                                assignment_id,
                                replay_record_sha256,
                                "replay",
                            )
                            replay_node = GraphNode(
                                matek_id=replay_id,
                                node_type=NodeType.ARTIFACT,
                                problem_id=problem_id,
                                title=f"Independent computation replay {assignment_id}",
                                epistemic_status=(
                                    EpistemicStatus.AUDIT_PASSED
                                    if replay_passed
                                    else EpistemicStatus.REFUTED
                                    if replay_status == "mismatch"
                                    else EpistemicStatus.OPEN
                                ),
                                workflow_status=(
                                    WorkflowStatus.COMPLETE
                                    if replay_passed
                                    else WorkflowStatus.BLOCKED
                                ),
                                created_in_run=run_id,
                                last_modified_run=run_id,
                                author_role="computation-replayer",
                                created_at=now,
                                updated_at=now,
                                body=new_generated_body(
                                    f"Independent computation replay {assignment_id}",
                                    "## Replay verdict\n\n"
                                    + f"`{replay_status}`"
                                    + "\n\n## Replay record SHA-256\n\n"
                                    + f"`{replay_record_sha256}`"
                                    + "\n\n## Isolation attestation\n\n```json\n"
                                    + json.dumps(
                                        raw_replay.get("isolation", {}),
                                        ensure_ascii=False,
                                        indent=2,
                                        sort_keys=True,
                                    )
                                    + "\n```",
                                ),
                                tags=[
                                    "matek/artifact",
                                    "matek/computation-replay",
                                    (
                                        "matek/replay-passed"
                                        if replay_passed
                                        else "matek/replay-not-passed"
                                    ),
                                ],
                                relations=[
                                    GraphEdge(
                                        source_id=replay_id,
                                        relation=RelationType.RELATED_TO,
                                        target_id=manifest_id,
                                    ),
                                    GraphEdge(
                                        source_id=replay_id,
                                        relation=RelationType.CREATED_DURING,
                                        target_id=_deterministic_id(
                                            NodeType.RUN, problem_id, run_id
                                        ),
                                    ),
                                ],
                                source_artifacts=[evidence_artifact],
                                metadata={
                                    "matek_assignment_id": assignment_id,
                                    "matek_computation_manifest_sha256": manifest_sha256,
                                    "matek_computation_replay_record_sha256": (
                                        replay_record_sha256
                                    ),
                                    "matek_computation_replay_status": replay_status,
                                    "matek_replay_passed": replay_passed,
                                    "matek_supporting_result_keys": sorted(declared_keys),
                                },
                            )
                            computation_nodes.append(replay_node)
                try:
                    admission = build_scientific_admission(
                        existing_nodes=[*nodes_list, approach, *computation_nodes],
                        problem_id=problem_id,
                        main_target_id=self.main_claim_id(problem_id),
                        run_id=run_id,
                        assignment_id=assignment_id,
                        task_id=task_id,
                        approach_id=approach_id,
                        results=typed_results,
                        unresolved_obligations=typed_obligations,
                        source_artifact=source_artifact,
                        now=now,
                    )
                except ScientificAdmissionError as exc:
                    raise GraphValidationError(
                        f"deterministic scientific admission failed: {exc}"
                    ) from exc
                exact_main_counterexamples = [
                    result
                    for result in typed_results
                    if result.kind is ScientificResultKind.COUNTEREXAMPLE
                    and result.scope is ScientificScope.MAIN
                    and result.disposition is ScientificResultDisposition.REFUTED_MECHANISM
                    and result.exact_gap is None
                    and normalize_exact_statement(result.exact_statement)
                    == normalize_exact_statement(
                        exact_statement(by_id[self.main_claim_id(problem_id)].body)
                    )
                ]
                refutation_support_keys = {
                    key
                    for counterexample in exact_main_counterexamples
                    for key in {
                        counterexample.local_key,
                        *transitive_result_dependency_keys(
                            typed_results,
                            [counterexample.local_key],
                        ),
                    }
                }
                if status == "refuted":
                    for admitted_node in admission.nodes:
                        if admitted_node.node_type not in {
                            NodeType.CLAIM,
                            NodeType.PROOF_ATTEMPT,
                            NodeType.DERIVATION,
                        }:
                            continue
                        if (
                            admitted_node.metadata.get("matek_result_local_key")
                            in refutation_support_keys
                        ):
                            # A branch-level ``refuted`` status normally invalidates its
                            # proposed route.  Exact-main counterexample premises instead remain
                            # live but untrusted until the independent refutation gate verifies
                            # this closed support bundle.
                            continue
                        admitted_node.epistemic_status = EpistemicStatus.STALE
                        admitted_node.workflow_status = WorkflowStatus.ABANDONED
                        admitted_node.invalidation_reasons = list(
                            dict.fromkeys(
                                [
                                    *admitted_node.invalidation_reasons,
                                    "originating_approach_refuted",
                                ]
                            )
                        )
                        admitted_node.metadata["matek_originating_approach_refuted"] = approach_id
                (
                    source_nodes,
                    result_source_ids,
                    source_issues,
                    source_identity_decisions,
                ) = _verified_worker_source_nodes(
                    report.get("source_ledger", []),
                    typed_results=typed_results,
                    existing_nodes=by_id,
                    problem_id=problem_id,
                    run_id=run_id,
                    source_artifact=source_artifact,
                    now=now,
                )
                admitted_by_id = {node.matek_id: node for node in admission.nodes}
                existing_citation_updates: dict[str, GraphNode] = {}
                for result_key, explicit_source_ids in sorted(result_source_ids.items()):
                    if not explicit_source_ids:
                        continue
                    result_nodes = [
                        node
                        for node in admission.nodes
                        if node.metadata.get("matek_result_local_key") == result_key
                    ]
                    attempts = [
                        node for node in result_nodes if node.node_type is NodeType.PROOF_ATTEMPT
                    ]
                    citation_targets = [
                        node for node in result_nodes if node.node_type is NodeType.CLAIM
                    ]
                    for attempt in attempts:
                        citation_targets.append(attempt)
                        for edge in attempt.relations:
                            if edge.relation is not RelationType.RELATED_TO:
                                continue
                            claim = admitted_by_id.get(edge.target_id) or by_id.get(edge.target_id)
                            if claim is None or claim.node_type is not NodeType.CLAIM:
                                continue
                            if claim.matek_id in admitted_by_id:
                                citation_targets.append(admitted_by_id[claim.matek_id])
                            else:
                                update = existing_citation_updates.setdefault(
                                    claim.matek_id, claim.model_copy(deep=True)
                                )
                                citation_targets.append(update)
                    for node in citation_targets:
                        node.relations = _unique_edges(
                            [
                                *node.relations,
                                *(
                                    GraphEdge(
                                        source_id=node.matek_id,
                                        relation=RelationType.CITES,
                                        target_id=source_id,
                                    )
                                    for source_id in sorted(explicit_source_ids)
                                ),
                            ]
                        )
                typed_proposed_nodes = [
                    approach,
                    *computation_nodes,
                    *source_nodes,
                    *admission.nodes,
                    *existing_citation_updates.values(),
                ]
                combined = {
                    **by_id,
                    **{node.matek_id: node for node in typed_proposed_nodes},
                }
                relation_issues = [
                    issue
                    for node in typed_proposed_nodes
                    for edge in node.relations
                    for issue in [
                        (
                            f"edge target does not exist: {edge.target_id}"
                            if edge.target_id not in combined
                            else self._relation_issue(edge, combined)
                        )
                    ]
                    if issue is not None
                ]
                cycle = self._dependency_cycle(list(combined.values()))
                if cycle is not None:
                    relation_issues.append(
                        "scientific admission creates dependency cycle: " + " -> ".join(cycle)
                    )
                if relation_issues:
                    raise GraphValidationError("; ".join(relation_issues))
                source_identity_writes: dict[str, str] = {}
                source_identity_artifacts: list[str] = []
                if source_identity_decisions:
                    decision_path, decision_contents = _source_identity_decision_artifact(
                        graph_name=self.graph_name,
                        problem_id=problem_id,
                        run_id=run_id,
                        timestamp=now,
                        decisions=source_identity_decisions,
                    )
                    source_identity_writes[decision_path] = decision_contents
                    source_identity_artifacts.append(decision_path)
                merged = self._upsert_generated_nodes_unlocked(
                    state=state,
                    nodes=by_id,
                    proposed=typed_proposed_nodes,
                    run_id=run_id,
                    author="matek-scientific-admission",
                    reason=(f"Admit typed scientific report {assignment_id} deterministically."),
                    operation_id=operation_id,
                    source_artifacts=[source_artifact, *source_identity_artifacts],
                    additional_writes=source_identity_writes,
                    issues=source_issues,
                )
                return merged.model_copy(
                    update={
                        "issues": list(
                            dict.fromkeys(
                                [
                                    *merged.issues,
                                    *proposal_issues,
                                    *admission.issues,
                                    *source_issues,
                                ]
                            )
                        )
                    }
                )
            proposed_nodes: list[GraphNode] = [approach]
            result_claim_ids: list[str] = []
            for index, result_text in enumerate(formal_results, start=1):
                claim_id = _descriptive_id(
                    NodeType.CLAIM,
                    result_text,
                    {*by_id, *(node.matek_id for node in proposed_nodes)},
                )
                result_claim_ids.append(claim_id)
                proposed_nodes.append(
                    GraphNode(
                        matek_id=claim_id,
                        node_type=NodeType.CLAIM,
                        problem_id=problem_id,
                        title=f"Result from {assignment_id} #{index}",
                        epistemic_status=EpistemicStatus.CANDIDATE,
                        workflow_status=WorkflowStatus.ACTIVE,
                        claim_type=ClaimType.LEMMA,
                        created_in_run=run_id,
                        last_modified_run=run_id,
                        author_role="research-worker",
                        created_at=now,
                        updated_at=now,
                        body=new_generated_body(
                            f"Result from {assignment_id} #{index}",
                            "## Exact statement\n\n"
                            + result_text
                            + "\n\n## Scope and conventions\n\n"
                            + ("\n".join(f"- {item}" for item in assumptions) or "_None recorded._")
                            + "\n\n## Current significance\n\n"
                            + f"Candidate result distilled from task {task_id}.",
                        ),
                        tags=["matek/claim", "matek/lemma", "matek/candidate"],
                        relations=[
                            GraphEdge(
                                source_id=claim_id,
                                relation=RelationType.CREATED_DURING,
                                target_id=_deterministic_id(NodeType.RUN, problem_id, run_id),
                            ),
                            *(
                                GraphEdge(
                                    source_id=claim_id,
                                    relation=RelationType.RELATED_TO,
                                    target_id=target_id,
                                )
                                for target_id in target_ids
                            ),
                        ],
                        source_artifacts=[source_artifact],
                        evidence=[result_text],
                    )
                )
            proof_content = str(report.get("proof_content") or "").strip()
            if proof_content and (formal_results or status == "candidate_complete"):
                proof_targets = result_claim_ids or [self.main_claim_id(problem_id)]
                proof_description = (
                    formal_results[0] if formal_results else f"Candidate proof from {assignment_id}"
                )
                proof_id = _descriptive_id(
                    NodeType.PROOF,
                    proof_description,
                    {*by_id, *(node.matek_id for node in proposed_nodes)},
                )
                proposed_nodes.append(
                    GraphNode(
                        matek_id=proof_id,
                        node_type=NodeType.PROOF,
                        problem_id=problem_id,
                        title=f"Candidate proof from {assignment_id}",
                        epistemic_status=EpistemicStatus.CANDIDATE,
                        workflow_status=(
                            WorkflowStatus.COMPLETE
                            if status == "candidate_complete"
                            else WorkflowStatus.ACTIVE
                        ),
                        created_in_run=run_id,
                        last_modified_run=run_id,
                        author_role="research-worker",
                        created_at=now,
                        updated_at=now,
                        body=new_generated_body(
                            f"Candidate proof from {assignment_id}",
                            "## Proof content\n\n"
                            + proof_content
                            + "\n\n## Exact gap\n\n"
                            + (
                                exact_gap
                                or "_No gap declared by the worker; independent audit required._"
                            ),
                        ),
                        tags=["matek/proof", "matek/candidate"],
                        relations=[
                            *(
                                GraphEdge(
                                    source_id=proof_id,
                                    relation=RelationType.PROVES,
                                    target_id=claim_id,
                                )
                                for claim_id in proof_targets
                            ),
                            GraphEdge(
                                source_id=proof_id,
                                relation=RelationType.CREATED_DURING,
                                target_id=_deterministic_id(NodeType.RUN, problem_id, run_id),
                            ),
                        ],
                        source_artifacts=[source_artifact],
                    )
                )
            for index, counterexample in enumerate(counterexamples, start=1):
                counterexample_id = _descriptive_id(
                    NodeType.COUNTEREXAMPLE,
                    counterexample,
                    {*by_id, *(node.matek_id for node in proposed_nodes)},
                )
                proposed_nodes.append(
                    GraphNode(
                        matek_id=counterexample_id,
                        node_type=NodeType.COUNTEREXAMPLE,
                        problem_id=problem_id,
                        title=f"Counterexample from {assignment_id} #{index}",
                        # Worker-declared counterexamples remain candidates until an
                        # independent audit verifies them.
                        epistemic_status=EpistemicStatus.CANDIDATE,
                        workflow_status=WorkflowStatus.COMPLETE,
                        created_in_run=run_id,
                        last_modified_run=run_id,
                        author_role="research-worker",
                        created_at=now,
                        updated_at=now,
                        body=new_generated_body(
                            f"Counterexample from {assignment_id} #{index}",
                            "## Explicit counterexample or obstruction\n\n"
                            + counterexample
                            + "\n\n## Scope\n\n"
                            "This automatically distilled node rules out the assigned approach "
                            "branch only. An explicit validated graph patch and independent "
                            "scientific audit are required before it can refute a claim node.",
                        ),
                        tags=["matek/counterexample", "matek/branch-local"],
                        relations=[
                            GraphEdge(
                                source_id=counterexample_id,
                                relation=RelationType.RELATED_TO,
                                target_id=approach_id,
                            )
                        ],
                        source_artifacts=[source_artifact],
                        metadata={"matek_branch_target_ids": target_ids},
                    )
                )
            raw_sources = report.get("sources", [])
            if isinstance(raw_sources, list):
                for raw_source in raw_sources:
                    if not isinstance(raw_source, Mapping):
                        continue
                    key = str(raw_source.get("source_id") or raw_source.get("title") or "source")
                    source_id = _deterministic_id(NodeType.SOURCE, problem_id, key)
                    verified = bool(raw_source.get("verified", False))
                    proposed_nodes.append(
                        GraphNode(
                            matek_id=source_id,
                            node_type=NodeType.SOURCE,
                            problem_id=problem_id,
                            title=str(raw_source.get("title") or key),
                            epistemic_status=(
                                EpistemicStatus.AUDIT_PASSED if verified else EpistemicStatus.OPEN
                            ),
                            workflow_status=(
                                WorkflowStatus.COMPLETE if verified else WorkflowStatus.ACTIVE
                            ),
                            created_in_run=run_id,
                            last_modified_run=run_id,
                            author_role="research-source-verifier",
                            created_at=now,
                            updated_at=now,
                            body=new_generated_body(
                                str(raw_source.get("title") or key),
                                "## Source record\n\n```json\n"
                                + json.dumps(
                                    raw_source, ensure_ascii=False, indent=2, sort_keys=True
                                )
                                + "\n```",
                            ),
                            tags=["matek/source"],
                            source_artifacts=[source_artifact],
                            metadata={
                                "matek_source_id": key,
                                "matek_verified": verified,
                            },
                        )
                    )
                    approach.relations.append(
                        GraphEdge(
                            source_id=approach_id,
                            relation=RelationType.CITES,
                            target_id=source_id,
                        )
                    )
            task.workflow_status = (
                WorkflowStatus.BLOCKED if status == "blocked" else WorkflowStatus.COMPLETE
            )
            task.epistemic_status = {
                "refuted": EpistemicStatus.REFUTED,
                "candidate_complete": EpistemicStatus.CANDIDATE,
            }.get(status, EpistemicStatus.OPEN)
            task.updated_at = now
            task.last_modified_run = run_id
            task.author_role = "research-worker"
            task.body = replace_generated_section(
                task.body,
                task.title,
                generated_section(task.body)
                + "\n\n## Worker outcome\n\n"
                + f"`{status}`\n\n"
                + (partial if formal_results else failure),
            )
            task.relations = _unique_edges(
                [
                    *task.relations,
                    GraphEdge(
                        source_id=task_id,
                        relation=RelationType.RELATED_TO,
                        target_id=approach_id,
                    ),
                ]
            )
            proposed_nodes.append(task)
            auto_result = self._upsert_generated_nodes_unlocked(
                state=state,
                nodes=by_id,
                proposed=proposed_nodes,
                run_id=run_id,
                author="research-worker",
                reason=f"Distill worker report {assignment_id} into reusable mathematical memory.",
                operation_id=operation_id,
                source_artifacts=[source_artifact],
            )
            combined_issues = list(
                dict.fromkeys(
                    [
                        *proposal_issues,
                        *(proposal_result.issues if proposal_result is not None else []),
                        *auto_result.issues,
                    ]
                )
            )
            return auto_result.model_copy(
                update={
                    "status": "partially_merged" if combined_issues else auto_result.status,
                    "issues": combined_issues,
                    "created_node_ids": list(
                        dict.fromkeys(
                            [
                                *(proposal_result.created_node_ids if proposal_result else []),
                                *auto_result.created_node_ids,
                            ]
                        )
                    ),
                    "updated_node_ids": list(
                        dict.fromkeys(
                            [
                                *(proposal_result.updated_node_ids if proposal_result else []),
                                *auto_result.updated_node_ids,
                            ]
                        )
                    ),
                }
            )

    @staticmethod
    def _main_result_support_ids(
        nodes: Mapping[str, GraphNode],
        *,
        target_id: str,
        accepted_proof_id: str,
    ) -> set[str]:
        """Return the explicit proof-support closure for an accepted main result."""

        reverse_proofs: dict[str, list[str]] = defaultdict(list)
        for node in nodes.values():
            for edge in node.relations:
                if edge.relation is RelationType.PROVES:
                    reverse_proofs[edge.target_id].append(edge.source_id)
        selected: set[str] = set()
        queue = deque([target_id, accepted_proof_id])
        unusable = {
            EpistemicStatus.REFUTED,
            EpistemicStatus.INCONSISTENT,
            EpistemicStatus.STALE,
        }
        while queue:
            node_id = queue.popleft()
            current = nodes.get(node_id)
            if current is None or node_id in selected or current.tombstone:
                continue
            selected.add(node_id)
            for edge in current.relations:
                if edge.relation in {RelationType.DEPENDS_ON, RelationType.CITES}:
                    queue.append(edge.target_id)
            if current.node_type is not NodeType.CLAIM:
                continue
            for proof_node_id in reverse_proofs.get(node_id, []):
                if node_id == target_id and proof_node_id != accepted_proof_id:
                    continue
                proof = nodes.get(proof_node_id)
                if (
                    proof is not None
                    and not proof.tombstone
                    and proof.epistemic_status not in unusable
                    and proof.workflow_status
                    not in {WorkflowStatus.ABANDONED, WorkflowStatus.SUPERSEDED}
                ):
                    queue.append(proof_node_id)
        return selected

    @classmethod
    def _mark_main_result_support(
        cls,
        *,
        nodes: dict[str, GraphNode],
        proposed: Sequence[GraphNode],
        problem_id: str,
        run_id: str,
        target_id: str,
        accepted_proof_id: str,
        now: datetime,
    ) -> list[GraphNode]:
        combined = dict(nodes)
        combined.update({node.matek_id: node for node in proposed})
        support_ids = cls._main_result_support_ids(
            combined,
            target_id=target_id,
            accepted_proof_id=accepted_proof_id,
        )
        proposed_by_id = {node.matek_id: node for node in proposed}
        for node in combined.values():
            if node.problem_id != problem_id:
                continue
            should_mark = node.matek_id in support_ids
            owned_mark = _MAIN_RESULT_NEEDS_METADATA in node.metadata
            changed = False
            if should_mark:
                if MAIN_RESULT_NEEDS_TAG not in node.tags:
                    node.tags.append(MAIN_RESULT_NEEDS_TAG)
                    changed = True
                if node.metadata.get(_MAIN_RESULT_NEEDS_METADATA) != run_id:
                    node.metadata[_MAIN_RESULT_NEEDS_METADATA] = run_id
                    changed = True
            elif owned_mark:
                node.tags = [tag for tag in node.tags if tag != MAIN_RESULT_NEEDS_TAG]
                node.metadata.pop(_MAIN_RESULT_NEEDS_METADATA, None)
                changed = True
            if changed:
                node.last_modified_run = run_id
                node.updated_at = now
                proposed_by_id[node.matek_id] = node
        return list(proposed_by_id.values())

    def record_research_result(
        self,
        *,
        problem_id: str,
        run_id: str,
        research_result: Mapping[str, Any],
    ) -> GraphMergeResult:
        """Bind candidate proofs and independent audits to separate graph nodes."""

        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes_list = self._load_nodes_unlocked(include_human_notes=True)
            by_id = {node.matek_id: node for node in nodes_list}
            target_id = self.main_claim_id(problem_id)
            target = by_id.get(target_id)
            if target is None:
                raise GraphValidationError("compiled main claim is missing from the graph")
            now = self._now()
            outcome = str(research_result.get("outcome") or "partial")
            candidate = research_result.get("candidate")
            candidate_map = candidate if isinstance(candidate, Mapping) else None
            raw_acceptance_gate = research_result.get("acceptance_gate")
            accepted = (
                outcome == "accepted"
                and isinstance(raw_acceptance_gate, Mapping)
                and raw_acceptance_gate.get("accepted") is True
            )
            proposed: list[GraphNode] = []
            proof_id: str | None = None
            if candidate_map is not None:
                exact_theorem = str(candidate_map.get("exact_theorem") or "").strip()
                full_proof = str(candidate_map.get("full_proof") or "").strip()
                frozen_statement = exact_statement(target.body)
                if normalize_exact_statement(exact_theorem) != normalize_exact_statement(
                    frozen_statement
                ):
                    raise GraphValidationError(
                        "candidate exact theorem does not match the immutable main target"
                    )
                if accepted:
                    target.epistemic_status = EpistemicStatus.AUDIT_PASSED
                    target.workflow_status = WorkflowStatus.COMPLETE
                    target.last_modified_run = run_id
                    target.updated_at = now
                    target.author_role = "research-acceptance-gate"
                    target.source_artifacts = list(
                        dict.fromkeys(
                            [
                                *target.source_artifacts,
                                f".matek/runs/{run_id}/research/candidate/package.json",
                                f".matek/runs/{run_id}/research/verdict.json",
                            ]
                        )
                    )
                proof_id = _descriptive_id(
                    NodeType.PROOF,
                    str(candidate_map.get("exact_theorem") or "Accepted candidate proof"),
                    set(by_id),
                )
                accepted_proof = GraphNode(
                    matek_id=proof_id,
                    node_type=NodeType.PROOF,
                    problem_id=problem_id,
                    title="Accepted candidate proof" if accepted else "Audited candidate proof",
                    epistemic_status=(
                        EpistemicStatus.AUDIT_PASSED if accepted else EpistemicStatus.CANDIDATE
                    ),
                    workflow_status=(
                        WorkflowStatus.COMPLETE if accepted else WorkflowStatus.BLOCKED
                    ),
                    created_in_run=run_id,
                    last_modified_run=run_id,
                    author_role="candidate-packager",
                    created_at=now,
                    updated_at=now,
                    body=new_generated_body(
                        "Accepted candidate proof" if accepted else "Audited candidate proof",
                        "## Theorem\n\n"
                        + str(candidate_map.get("exact_theorem") or "")
                        + "\n\n## Full proof\n\n"
                        + full_proof
                        + "\n\n## Unresolved items\n\n"
                        + (
                            "\n".join(
                                f"- {item}" for item in candidate_map.get("unresolved_items", [])
                            )
                            or "_None._"
                        ),
                    ),
                    tags=[
                        "matek/proof",
                        "matek/audit-passed" if accepted else "matek/candidate",
                    ],
                    relations=[
                        GraphEdge(
                            source_id=proof_id,
                            relation=RelationType.PROVES,
                            target_id=target_id,
                        ),
                        GraphEdge(
                            source_id=proof_id,
                            relation=RelationType.CREATED_DURING,
                            target_id=_deterministic_id(NodeType.RUN, problem_id, run_id),
                        ),
                    ],
                    source_artifacts=[
                        f".matek/runs/{run_id}/research/candidate/package.json",
                        f".matek/runs/{run_id}/research/candidate/proof.md",
                    ],
                    metadata={
                        "matek_quantitative_or_algorithmic": bool(
                            candidate_map.get("quantitative_or_algorithmic", False)
                        ),
                        "matek_acceptance_gate_passed": accepted,
                    },
                )
                proposed.append(accepted_proof)
            if accepted:
                proposed.append(target)
            audits = research_result.get("audits", {})
            if isinstance(audits, Mapping):
                for name, raw_audit in audits.items():
                    if not isinstance(raw_audit, Mapping):
                        continue
                    verdict = str(raw_audit.get("verdict") or "fail")
                    audit_id = _deterministic_id(NodeType.AUDIT, problem_id, run_id, str(name))
                    audit_passed = verdict == "pass"
                    target_of_audit = proof_id or target_id
                    proposed.append(
                        GraphNode(
                            matek_id=audit_id,
                            node_type=NodeType.AUDIT,
                            problem_id=problem_id,
                            title=f"{str(name).title()} research audit",
                            epistemic_status=(
                                EpistemicStatus.AUDIT_PASSED
                                if audit_passed
                                else EpistemicStatus.INCONSISTENT
                            ),
                            workflow_status=WorkflowStatus.COMPLETE,
                            created_in_run=run_id,
                            last_modified_run=run_id,
                            author_role="research-auditor",
                            created_at=now,
                            updated_at=now,
                            body=new_generated_body(
                                f"{str(name).title()} research audit",
                                "## Verdict\n\n"
                                + f"`{verdict}`"
                                + "\n\n## Issues\n\n"
                                + (
                                    "\n".join(
                                        "- " + str(item.get("description") or item)
                                        for item in raw_audit.get("issues", [])
                                    )
                                    or "_None._"
                                )
                                + "\n\n## Unresolved obligations\n\n"
                                + (
                                    "\n".join(
                                        f"- {item}"
                                        for item in raw_audit.get("unresolved_obligations", [])
                                    )
                                    or "_None._"
                                ),
                            ),
                            tags=["matek/audit", f"matek/audit-{verdict}"],
                            relations=[
                                GraphEdge(
                                    source_id=audit_id,
                                    relation=RelationType.AUDITS,
                                    target_id=target_of_audit,
                                ),
                                GraphEdge(
                                    source_id=audit_id,
                                    relation=RelationType.CREATED_DURING,
                                    target_id=_deterministic_id(NodeType.RUN, problem_id, run_id),
                                ),
                            ],
                            source_artifacts=[f".matek/runs/{run_id}/research/audits/{name}.json"],
                            metadata={"matek_audit_verdict": verdict},
                        )
                    )
            if accepted and proof_id is not None:
                proposed = self._mark_main_result_support(
                    nodes=by_id,
                    proposed=proposed,
                    problem_id=problem_id,
                    run_id=run_id,
                    target_id=target_id,
                    accepted_proof_id=proof_id,
                    now=now,
                )
            return self._upsert_generated_nodes_unlocked(
                state=state,
                nodes=by_id,
                proposed=proposed,
                run_id=run_id,
                author="research-acceptance-gate",
                reason=f"Record research outcome {outcome} with separate proof and audit nodes.",
                operation_id=f"research-result-v2:{run_id}",
                source_artifacts=[f".matek/runs/{run_id}/research/result.json"],
            )

    def record_lemma_audit(
        self,
        *,
        problem_id: str,
        run_id: str,
        nomination: Mapping[str, Any],
        gate: Mapping[str, Any],
        source_artifact: str,
    ) -> GraphMergeResult:
        """Apply an independent intermediate-lemma gate to its exact derivation.

        This lane can promote a reusable restricted theorem and its audited proof
        route, but it is structurally forbidden from authorizing the main target or
        manuscript generation.
        """

        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            by_id = {node.matek_id: node for node in nodes}
            provisional_audit_id = str(nomination.get("nomination_id") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", provisional_audit_id):
                raise GraphValidationError("lemma audit has an invalid nomination identity")
            expected_source_artifact = (
                f".matek/runs/{run_id}/research/lemma-audits/{provisional_audit_id}/gate.json"
            )
            if source_artifact != expected_source_artifact:
                raise GraphValidationError("lemma audit is not bound to its canonical run artifact")
            try:
                from ..stages.lemma_audit import verify_persisted_lemma_audit

                gate_path = ensure_path_confined(
                    self.project_root,
                    self.project_root / expected_source_artifact,
                )
                persisted_nomination, persisted_gate = verify_persisted_lemma_audit(
                    gate_path.parent / "nomination.json",
                    gate_path,
                )
            except (OSError, ValueError) as exc:
                raise GraphValidationError(
                    f"persisted lemma-audit evidence failed verification: {exc}"
                ) from exc
            if nomination != persisted_nomination.model_dump(mode="json"):
                raise GraphValidationError(
                    "caller nomination differs from its canonical run artifact"
                )
            if gate != persisted_gate.model_dump(mode="json"):
                raise GraphValidationError(
                    "caller lemma gate differs from its canonical run artifact"
                )
            nomination = persisted_nomination.model_dump(mode="json")
            gate = persisted_gate.model_dump(mode="json")
            statement_id = str(nomination.get("statement_id") or "").strip()
            claim = by_id.get(statement_id)
            if claim is None or claim.node_type is not NodeType.CLAIM:
                raise GraphValidationError(
                    "lemma audit does not identify an admitted canonical claim"
                )
            if statement_id == self.main_claim_id(problem_id):
                raise GraphValidationError(
                    "the intermediate lemma-audit lane cannot authorize the main target"
                )
            if claim.problem_id != problem_id:
                raise GraphValidationError("lemma audit claim belongs to another problem")
            if (
                claim.tombstone
                or claim.invalidation_reasons
                or claim.epistemic_status
                in {
                    EpistemicStatus.STALE,
                    EpistemicStatus.INCONSISTENT,
                    EpistemicStatus.REFUTED,
                }
            ):
                raise GraphValidationError("nominated canonical claim is no longer live")
            scope = str(nomination.get("scope") or "")
            if scope not in {ScientificScope.REDUCTION.value, ScientificScope.BRANCH.value}:
                raise GraphValidationError(
                    "lemma audits are restricted to branch or reduction theorems"
                )
            exact_claim = normalize_exact_statement(exact_statement(claim.body))
            nominated_statement = normalize_exact_statement(
                str(nomination.get("exact_statement") or "")
            )
            if not exact_claim or exact_claim != nominated_statement:
                raise GraphValidationError(
                    "lemma audit statement does not match the canonical exact claim"
                )
            expected_statement_sha256 = sha256_text(" ".join(exact_claim.split()))
            if str(gate.get("statement_sha256") or "") != expected_statement_sha256:
                raise GraphValidationError(
                    "lemma audit gate is bound to a different exact statement"
                )
            audit_id_text = str(gate.get("audit_id") or "").strip()
            if audit_id_text != str(nomination.get("nomination_id") or "").strip():
                raise GraphValidationError(
                    "lemma audit gate identity does not match its nomination"
                )
            if bool(gate.get("main_target_acceptance_authorized")) or bool(
                gate.get("manuscript_authorized")
            ):
                raise GraphValidationError(
                    "an intermediate lemma audit cannot authorize a terminal stage"
                )
            status = str(gate.get("status") or "")
            if status not in {"audit_passed", "audit_failed", "blocked"}:
                raise GraphValidationError("lemma audit gate has an unknown status")
            accepted_intermediate = gate.get("accepted_intermediate")
            if status == "audit_passed":
                if not isinstance(accepted_intermediate, Mapping) or (
                    str(accepted_intermediate.get("statement_id") or "").strip() != statement_id
                    or bool(accepted_intermediate.get("terminal_main_target_satisfied"))
                    or bool(accepted_intermediate.get("manuscript_authorized"))
                ):
                    raise GraphValidationError(
                        "passing lemma audit lacks a nonterminal accepted theorem record"
                    )
            elif accepted_intermediate is not None:
                raise GraphValidationError(
                    "only a passing lemma audit may carry an accepted theorem"
                )
            origin_assignment = str(nomination.get("origin_worker_id") or "").strip()
            canonical_derivation_id = str(nomination.get("canonical_derivation_id") or "").strip()
            operation_id = f"lemma-audit:{run_id}:{audit_id_text}"
            prior_operation = state.processed_operations.get(operation_id)
            audit_node_id = _deterministic_id(
                NodeType.AUDIT,
                problem_id,
                run_id,
                audit_id_text,
                expected_statement_sha256,
            )
            existing_audit = by_id.get(audit_node_id)
            expected_response_bindings = sorted(
                f"{key}:{value}"
                for key, value in cast(
                    Mapping[str, object], gate.get("response_sha256", {})
                ).items()
            )
            same_committed_audit = bool(
                prior_operation is not None
                and existing_audit is not None
                and existing_audit.node_type is NodeType.AUDIT
                and existing_audit.problem_id == problem_id
                and existing_audit.created_in_run == run_id
                and existing_audit.source_artifacts == [source_artifact]
                and existing_audit.metadata.get("matek_audit_id") == audit_id_text
                and existing_audit.metadata.get("matek_audit_status") == status
                and existing_audit.metadata.get("matek_statement_sha256")
                == expected_statement_sha256
                and existing_audit.metadata.get("matek_origin_assignment_id")
                == (origin_assignment or None)
                and existing_audit.metadata.get("matek_gate_input_sha256")
                == gate.get("input_sha256")
                and existing_audit.metadata.get("matek_response_sha256")
                == expected_response_bindings
                and any(
                    edge.relation is RelationType.AUDITS and edge.target_id == statement_id
                    for edge in existing_audit.relations
                )
                and any(
                    edge.relation is RelationType.AUDITS
                    and edge.target_id == canonical_derivation_id
                    for edge in existing_audit.relations
                )
            )
            if prior_operation is not None and not same_committed_audit:
                raise GraphValidationError(
                    "processed lemma-audit operation differs from its committed audit binding"
                )
            derivation = by_id.get(canonical_derivation_id)
            if derivation is None or derivation.node_type is not NodeType.DERIVATION:
                raise GraphValidationError(
                    "lemma audit does not identify its frozen canonical derivation"
                )
            admission_identity_value = derivation.metadata.get("matek_admission_identity")
            admission_payload_value = derivation.metadata.get("matek_admission_payload_sha256")
            admission_identity_parts = (
                admission_identity_value.split("\0")
                if isinstance(admission_identity_value, str)
                else []
            )
            if (
                derivation.problem_id != problem_id
                or not any(
                    edge.relation is RelationType.PROVES and edge.target_id == statement_id
                    for edge in derivation.relations
                )
                or not origin_assignment
                or derivation.metadata.get("matek_assignment_id") != origin_assignment
                or not isinstance(admission_identity_value, str)
                or not isinstance(admission_payload_value, str)
                or not matches_admission_binding(
                    derivation,
                    admission_identity_value,
                    admission_payload_value,
                )
                or admission_identity_parts
                != [
                    run_id,
                    origin_assignment,
                    str(derivation.metadata.get("matek_result_local_key") or ""),
                    str(derivation.metadata.get("matek_scientific_schema_version") or ""),
                ]
                or derivation.created_in_run != run_id
            ):
                raise GraphValidationError(
                    "frozen lemma derivation lacks its exact application admission binding"
                )
            exact_failed_gate_replay = bool(
                same_committed_audit
                and status == "audit_failed"
                and derivation.epistemic_status is EpistemicStatus.REFUTED
                and derivation.workflow_status is WorkflowStatus.BLOCKED
                and derivation.invalidation_reasons == ["independent_lemma_audit_failed"]
                and derivation.last_modified_run == run_id
            )
            if (
                derivation.tombstone
                or derivation.epistemic_status
                in {
                    EpistemicStatus.STALE,
                    EpistemicStatus.INCONSISTENT,
                    EpistemicStatus.REFUTED,
                }
                or derivation.invalidation_reasons
            ) and not exact_failed_gate_replay:
                raise GraphValidationError("nominated derivation is no longer live and promotable")
            if derivation.metadata.get("matek_conclusion_claim_id") != statement_id or (
                derivation.metadata.get("matek_exact_target_version")
                != logical_version(exact_claim)
            ):
                raise GraphValidationError(
                    "nominated derivation is no longer bound to the exact claim version"
                )
            attempt_id = derivation.metadata.get("matek_proof_attempt_id")
            proof_attempt = by_id.get(attempt_id) if isinstance(attempt_id, str) else None
            if (
                proof_attempt is None
                or proof_attempt.node_type is not NodeType.PROOF_ATTEMPT
                or proof_attempt.tombstone
                or proof_attempt.invalidation_reasons
                or proof_attempt.epistemic_status
                in {
                    EpistemicStatus.STALE,
                    EpistemicStatus.INCONSISTENT,
                    EpistemicStatus.REFUTED,
                }
            ):
                raise GraphValidationError(
                    "nominated derivation has lost its canonical proof attempt"
                )
            source_artifacts_by_id = {
                item.artifact_id: item for item in persisted_nomination.source_artifacts
            }
            frozen_attempt = source_artifacts_by_id.get(proof_attempt.matek_id)
            frozen_derivation = source_artifacts_by_id.get(derivation.matek_id)
            if (
                frozen_attempt is None
                or frozen_attempt.content not in proof_attempt.evidence
                or sha256_text(frozen_attempt.content) != frozen_attempt.content_sha256
                or frozen_derivation is None
                or frozen_derivation.content != derivation.body
                or sha256_text(derivation.body) != frozen_derivation.content_sha256
            ):
                raise GraphValidationError(
                    "nominated proof route changed after the audit packet was frozen"
                )
            nominated_dependencies = {
                item.dependency_id: item for item in persisted_nomination.dependencies
            }
            current_dependency_ids = sorted(
                edge.target_id
                for edge in derivation.relations
                if edge.relation is RelationType.DEPENDS_ON
            )
            if current_dependency_ids != sorted(nominated_dependencies):
                raise GraphValidationError("nominated derivation premises changed after audit")
            problem_nodes = [node for node in nodes if node.problem_id == problem_id]
            frozen_obligations = {
                item.obligation_id: item
                for item in persisted_nomination.target_obligation_contracts
            }
            if set(frozen_obligations) != set(persisted_nomination.target_obligation_ids):
                raise GraphValidationError(
                    "lemma nomination lacks a complete frozen target-obligation contract"
                )
            for obligation_id, frozen_obligation in frozen_obligations.items():
                current_obligation_node = by_id.get(obligation_id)
                if frozen_obligation.target_kind == "claim":
                    if (
                        current_obligation_node is None
                        or current_obligation_node.node_type is not NodeType.CLAIM
                        or current_obligation_node.problem_id != problem_id
                        or current_obligation_node.tombstone
                        or current_obligation_node.invalidation_reasons
                        or current_obligation_node.epistemic_status
                        in {
                            EpistemicStatus.STALE,
                            EpistemicStatus.INCONSISTENT,
                            EpistemicStatus.REFUTED,
                        }
                        or normalize_exact_statement(exact_statement(current_obligation_node.body))
                        != frozen_obligation.exact_statement
                        or logical_version(exact_statement(current_obligation_node.body))
                        != frozen_obligation.logical_version
                        or current_obligation_node.statement_version
                        != frozen_obligation.statement_version
                        or current_obligation_node.content_hash != frozen_obligation.content_sha256
                    ):
                        raise GraphValidationError(
                            f"target claim {obligation_id} changed after lemma audit"
                        )
                    continue
                resolved_by_this_gate = bool(
                    current_obligation_node is not None
                    and current_obligation_node.metadata.get("matek_resolved_by_derivation_id")
                    == canonical_derivation_id
                    and current_obligation_node.metadata.get("matek_resolution_audit_id")
                    == audit_id_text
                )
                if (
                    current_obligation_node is None
                    or current_obligation_node.node_type is not NodeType.OBLIGATION
                    or normalize_exact_statement(exact_statement(current_obligation_node.body))
                    != frozen_obligation.exact_statement
                    or _metadata_string_list(current_obligation_node, "matek_quantifiers")
                    != frozen_obligation.quantifiers
                    or _metadata_string_list(current_obligation_node, "matek_hypotheses")
                    != frozen_obligation.hypotheses
                    or normalize_exact_statement(
                        str(current_obligation_node.metadata.get("matek_conclusion") or "")
                    )
                    != frozen_obligation.conclusion
                    or _metadata_string_list(current_obligation_node, "matek_dependency_claim_ids")
                    != frozen_obligation.dependency_claim_ids
                    or _metadata_string_list(current_obligation_node, "matek_target_claim_ids")
                    != frozen_obligation.target_claim_ids
                    or str(current_obligation_node.metadata.get("matek_scope") or "branch")
                    != frozen_obligation.scope.value
                    or str(
                        current_obligation_node.metadata.get("matek_notation_definition_version")
                        or "1"
                    )
                    != frozen_obligation.notation_definition_version
                    or current_obligation_node.evidence != frozen_obligation.falsification_evidence
                    or current_obligation_node.statement_version
                    != frozen_obligation.statement_version
                    or (
                        current_obligation_node.content_hash != frozen_obligation.content_sha256
                        and not resolved_by_this_gate
                    )
                ):
                    raise GraphValidationError(
                        f"target obligation {obligation_id} changed after lemma audit"
                    )
            current_trusted_claim_ids = _markdown_trusted_claim_ids(problem_nodes)
            expected_dependency_versions: list[str] = []
            for dependency_id in current_dependency_ids:
                dependency = by_id.get(dependency_id)
                frozen_dependency = nominated_dependencies[dependency_id]
                if (
                    dependency is None
                    or dependency.node_type not in {NodeType.CLAIM, NodeType.DEFINITION}
                    or dependency.tombstone
                    or dependency.invalidation_reasons
                    or dependency.epistemic_status
                    in {
                        EpistemicStatus.STALE,
                        EpistemicStatus.INCONSISTENT,
                        EpistemicStatus.REFUTED,
                    }
                    or dependency.workflow_status
                    in {
                        WorkflowStatus.BLOCKED,
                        WorkflowStatus.ABANDONED,
                        WorkflowStatus.SUPERSEDED,
                    }
                    or dependency.statement_version != frozen_dependency.current_statement_version
                    or dependency.content_hash != frozen_dependency.current_content_sha256
                    or normalize_exact_statement(exact_statement(dependency.body))
                    != normalize_exact_statement(frozen_dependency.exact_statement)
                ):
                    raise GraphValidationError(
                        f"nominated dependency {dependency_id} changed after audit"
                    )
                if dependency.node_type is NodeType.DEFINITION and (
                    canonical_admitted_definition_scope(dependency) is not ScientificScope.BRANCH
                    or dependency_id not in current_trusted_claim_ids
                ):
                    raise GraphValidationError(
                        f"nominated definition dependency {dependency_id} lacks current "
                        "audited Markdown provenance"
                    )
                if dependency_id not in current_trusted_claim_ids:
                    raise GraphValidationError(
                        f"nominated dependency {dependency_id} is not trusted in the current "
                        "Markdown graph"
                    )
                expected_dependency_versions.append(
                    f"{dependency_id}@{logical_version(exact_statement(dependency.body))}"
                )
            if derivation.dependency_versions != expected_dependency_versions:
                raise GraphValidationError(
                    "nominated derivation dependency-version binding changed after audit"
                )
            now = self._now()
            raw_obligations = gate.get("obligations", [])
            obligations = (
                [str(item).strip() for item in raw_obligations if str(item).strip()]
                if isinstance(raw_obligations, list)
                else []
            )
            raw_falsification = gate.get("falsification_evidence", [])
            falsification = (
                [item for item in raw_falsification if isinstance(item, Mapping)]
                if isinstance(raw_falsification, list)
                else []
            )
            if status == "blocked" and not obligations:
                raise GraphValidationError(
                    "blocked lemma audit must record an exact audit obligation"
                )
            if status == "audit_failed" and not (obligations or falsification):
                raise GraphValidationError(
                    "failed lemma audit must record a defect or falsification"
                )
            audit_status = {
                "audit_passed": EpistemicStatus.AUDIT_PASSED,
                "audit_failed": EpistemicStatus.REFUTED,
                "blocked": EpistemicStatus.OPEN,
            }[status]
            audit_node = GraphNode(
                matek_id=audit_node_id,
                node_type=NodeType.AUDIT,
                problem_id=problem_id,
                title=f"Independent lemma audit: {claim.title}",
                epistemic_status=audit_status,
                workflow_status=(
                    WorkflowStatus.COMPLETE if status != "blocked" else WorkflowStatus.BLOCKED
                ),
                created_in_run=run_id,
                last_modified_run=run_id,
                author_role="lemma-audit-gate",
                created_at=now,
                updated_at=now,
                body=new_generated_body(
                    f"Independent lemma audit: {claim.title}",
                    "## Exact statement audited\n\n"
                    + exact_claim
                    + "\n\n## Deterministic gate\n\n"
                    + f"`{status}`"
                    + "\n\n## Audit obligations\n\n"
                    + ("\n".join(f"- {item}" for item in obligations) or "_None._")
                    + "\n\n## Falsification evidence\n\n"
                    + (
                        "\n".join(
                            "- "
                            + str(
                                item.get("observed_failure") or item.get("case_description") or item
                            )
                            for item in falsification
                        )
                        or "_None._"
                    ),
                ),
                tags=["matek/audit", "matek/lemma-audit", f"matek/{status}"],
                relations=[
                    GraphEdge(
                        source_id=audit_node_id,
                        relation=RelationType.AUDITS,
                        target_id=statement_id,
                    ),
                    GraphEdge(
                        source_id=audit_node_id,
                        relation=RelationType.AUDITS,
                        target_id=derivation.matek_id,
                    ),
                    GraphEdge(
                        source_id=audit_node_id,
                        relation=RelationType.CREATED_DURING,
                        target_id=_deterministic_id(NodeType.RUN, problem_id, run_id),
                    ),
                ],
                source_artifacts=[source_artifact],
                evidence=[
                    str(value)
                    for value in cast(
                        Mapping[str, object], gate.get("response_sha256", {})
                    ).values()
                ]
                if isinstance(gate.get("response_sha256", {}), Mapping)
                else [],
                metadata={
                    "matek_audit_id": audit_id_text,
                    "matek_audit_status": status,
                    "matek_statement_sha256": expected_statement_sha256,
                    "matek_origin_assignment_id": origin_assignment or None,
                    "matek_gate_input_sha256": gate.get("input_sha256"),
                    "matek_response_sha256": sorted(
                        f"{key}:{value}"
                        for key, value in cast(
                            Mapping[str, object], gate.get("response_sha256", {})
                        ).items()
                    )
                    if isinstance(gate.get("response_sha256", {}), Mapping)
                    else [],
                },
            )
            proposed: list[GraphNode] = [audit_node]
            if status == "audit_passed":
                # The audit accepts this exact derivation, not every route to the
                # conclusion. Trust is recomputed from current Markdown after binding
                # all premises and obligations.
                derivation.epistemic_status = EpistemicStatus.AUDIT_PASSED
                derivation.workflow_status = WorkflowStatus.COMPLETE
                derivation.last_modified_run = run_id
                derivation.updated_at = now
                trusted_after_audit = _markdown_trusted_claim_ids(problem_nodes)
                resolvable_obligation_ids = (
                    persisted_nomination.target_obligation_ids
                    if statement_id in trusted_after_audit
                    else []
                )
                for obligation_id in resolvable_obligation_ids:
                    target_obligation = by_id.get(obligation_id)
                    frozen_obligation = frozen_obligations[obligation_id]
                    if (
                        frozen_obligation.target_kind != "obligation"
                        or target_obligation is None
                        or target_obligation.node_type is not NodeType.OBLIGATION
                        or target_obligation.problem_id != problem_id
                        or target_obligation.tombstone
                        or target_obligation.invalidation_reasons
                        or target_obligation.workflow_status
                        in {WorkflowStatus.COMPLETE, WorkflowStatus.ABANDONED}
                        or target_obligation.epistemic_status
                        in {
                            EpistemicStatus.AUDIT_PASSED,
                            EpistemicStatus.LEAN_VERIFIED,
                            EpistemicStatus.REFUTED,
                            EpistemicStatus.STALE,
                            EpistemicStatus.INCONSISTENT,
                        }
                    ):
                        continue
                    obligation_statement = normalize_exact_statement(
                        exact_statement(target_obligation.body)
                    )
                    obligation_conclusion = normalize_exact_statement(
                        str(target_obligation.metadata.get("matek_conclusion") or "")
                    )
                    if (
                        frozen_obligation.quantifiers
                        or frozen_obligation.hypotheses
                        or frozen_obligation.falsification_evidence
                        or not set(frozen_obligation.dependency_claim_ids).issubset(
                            current_dependency_ids
                        )
                        or frozen_obligation.scope.value != scope
                    ):
                        # A bare claim does not encode a quantified/hypothesized obligation
                        # contract.  Likewise, unresolved falsification evidence or missing
                        # premise bindings require a dedicated semantic discharge rather than
                        # this exact-text convenience transition.
                        continue
                    if exact_claim not in {obligation_statement, obligation_conclusion}:
                        continue
                    target_obligation.epistemic_status = EpistemicStatus.AUDIT_PASSED
                    target_obligation.workflow_status = WorkflowStatus.COMPLETE
                    target_obligation.last_modified_run = run_id
                    target_obligation.updated_at = now
                    target_obligation.source_artifacts = list(
                        dict.fromkeys([*target_obligation.source_artifacts, source_artifact])
                    )
                    target_obligation.metadata["matek_resolved_by_derivation_id"] = (
                        derivation.matek_id
                    )
                    target_obligation.metadata["matek_resolution_audit_id"] = audit_id_text
                    if not any(
                        edge.relation is RelationType.RESOLVES
                        and edge.target_id == target_obligation.matek_id
                        for edge in derivation.relations
                    ):
                        derivation.relations.append(
                            GraphEdge(
                                source_id=derivation.matek_id,
                                relation=RelationType.RESOLVES,
                                target_id=target_obligation.matek_id,
                            )
                        )
                    proposed.append(target_obligation)
                proposed.append(derivation)
            elif status == "audit_failed":
                derivation.epistemic_status = EpistemicStatus.REFUTED
                derivation.workflow_status = WorkflowStatus.BLOCKED
                derivation.invalidation_reasons = list(
                    dict.fromkeys(
                        [*derivation.invalidation_reasons, "independent_lemma_audit_failed"]
                    )
                )
                derivation.last_modified_run = run_id
                derivation.updated_at = now
                proposed.append(derivation)
            else:
                created_obligation_ids: list[str] = []
                for index, obligation_text in enumerate(obligations, start=1):
                    obligation_id = _descriptive_id(
                        NodeType.OBLIGATION,
                        obligation_text,
                        {*by_id, *(node.matek_id for node in proposed)},
                    )
                    obligation_node = GraphNode(
                        matek_id=obligation_id,
                        node_type=NodeType.OBLIGATION,
                        problem_id=problem_id,
                        title=f"Lemma audit obligation {index}: {claim.title}",
                        epistemic_status=EpistemicStatus.OPEN,
                        workflow_status=WorkflowStatus.BLOCKED,
                        created_in_run=run_id,
                        last_modified_run=run_id,
                        author_role="lemma-audit-gate",
                        created_at=now,
                        updated_at=now,
                        body=new_generated_body(
                            f"Lemma audit obligation {index}: {claim.title}",
                            "## Exact statement\n\n"
                            + obligation_text
                            + "\n\n## Conclusion\n\n"
                            + exact_claim,
                        ),
                        tags=["matek/obligation", "matek/lemma-audit"],
                        relations=[
                            GraphEdge(
                                source_id=obligation_id,
                                relation=RelationType.BLOCKS,
                                target_id=derivation.matek_id,
                            )
                        ],
                        source_artifacts=[source_artifact],
                        metadata={
                            "matek_parent_derivation_ids": [derivation.matek_id],
                            "matek_dependency_claim_ids": [],
                            "matek_target_claim_ids": [statement_id],
                            "matek_conclusion": exact_claim,
                            "matek_scope": scope,
                            "matek_notation_definition_version": "1",
                            "matek_estimated_leverage": 100,
                        },
                    )
                    proposed.append(obligation_node)
                    created_obligation_ids.append(obligation_id)
                if created_obligation_ids:
                    raw_obligation_ids = derivation.metadata.get("matek_obligation_ids", [])
                    existing_obligation_ids = (
                        [str(item) for item in raw_obligation_ids]
                        if isinstance(raw_obligation_ids, list)
                        else []
                    )
                    derivation.metadata["matek_obligation_ids"] = list(
                        dict.fromkeys([*existing_obligation_ids, *created_obligation_ids])
                    )
                    for obligation_id in created_obligation_ids:
                        if not any(
                            edge.relation is RelationType.BLOCKED_BY
                            and edge.target_id == obligation_id
                            for edge in derivation.relations
                        ):
                            derivation.relations.append(
                                GraphEdge(
                                    source_id=derivation.matek_id,
                                    relation=RelationType.BLOCKED_BY,
                                    target_id=obligation_id,
                                )
                            )
                    derivation.last_modified_run = run_id
                    derivation.updated_at = now
                    proposed.append(derivation)
            return self._upsert_generated_nodes_unlocked(
                state=state,
                nodes=by_id,
                proposed=proposed,
                run_id=run_id,
                author="lemma-audit-gate",
                reason=f"Record independent intermediate-lemma audit {audit_id_text}.",
                operation_id=operation_id,
                source_artifacts=[source_artifact],
            )

    def record_counterexample_audit(
        self,
        *,
        problem_id: str,
        run_id: str,
        assignment_id: str,
        result_local_key: str,
        nomination: Mapping[str, Any],
        gate: Mapping[str, Any],
        source_artifact: str,
    ) -> GraphMergeResult:
        """Promote one independently verified exact counterexample to the main target.

        Worker admission deliberately cannot create this edge.  This method re-reads and
        recomputes the canonical run artifact before adding ``REFUTES`` or changing the frozen
        target's epistemic status.
        """

        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=True)
            by_id = {node.matek_id: node for node in nodes}
            audit_id = str(nomination.get("audit_id") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", audit_id):
                raise GraphValidationError("counterexample audit has an invalid identity")
            expected_source = (
                f".matek/runs/{run_id}/research/counterexample-audits/{audit_id}/gate.json"
            )
            if source_artifact != expected_source:
                raise GraphValidationError(
                    "counterexample audit is not bound to its canonical run artifact"
                )
            try:
                from ..stages.counterexample_audit import (
                    CounterexampleAuditGateStatus,
                    CounterexampleGraphReadSnapshot,
                    verify_persisted_counterexample_audit,
                )

                gate_path = ensure_path_confined(
                    self.project_root,
                    self.project_root / expected_source,
                )
                target_id = self.main_claim_id(problem_id)
                target = by_id.get(target_id)
                if target is None or target.node_type is not NodeType.CLAIM:
                    raise GraphValidationError("counterexample audit has no frozen main target")
                target_statement = normalize_exact_statement(exact_statement(target.body))
                persisted_nomination, persisted_gate = verify_persisted_counterexample_audit(
                    gate_path.parent / "nomination.json",
                    gate_path,
                    expected_target_statement=target_statement,
                    graph_snapshot=CounterexampleGraphReadSnapshot(
                        graph_name=self.graph_name,
                        state=state,
                        nodes=tuple(nodes),
                        main_target_id=target_id,
                    ),
                )
            except GraphValidationError:
                raise
            except (OSError, ValueError) as exc:
                raise GraphValidationError(
                    f"persisted counterexample-audit evidence failed verification: {exc}"
                ) from exc
            if nomination != persisted_nomination.model_dump(mode="json"):
                raise GraphValidationError(
                    "caller counterexample nomination differs from its canonical artifact"
                )
            if gate != persisted_gate.model_dump(mode="json"):
                raise GraphValidationError(
                    "caller counterexample gate differs from its canonical artifact"
                )
            if persisted_gate.status is not CounterexampleAuditGateStatus.REFUTATION_VERIFIED:
                raise GraphValidationError(
                    "only a verified exact-counterexample gate may refute the main target"
                )
            if (
                persisted_nomination.assignment_id != assignment_id
                or persisted_nomination.result_local_key != result_local_key
            ):
                raise GraphValidationError("counterexample audit belongs to another worker result")
            if persisted_nomination.main_target_node_id not in {None, target_id}:
                raise GraphValidationError("counterexample nomination names another main target")
            try:
                from ..stages.common import canonical_json_bytes, read_regular_bytes, sha256_bytes
                from ..stages.research import ResearchWorkerReport

                report_path = ensure_path_confined(
                    self.project_root,
                    self.project_root
                    / ".matek"
                    / "runs"
                    / run_id
                    / "research"
                    / persisted_nomination.worker_report_path,
                )
                worker_report = ResearchWorkerReport.model_validate_json(
                    read_regular_bytes(report_path)
                )
                bound_results = [
                    result
                    for result in worker_report.results
                    if result.local_key == result_local_key
                ]
                if len(bound_results) != 1:
                    raise ValueError("worker report does not contain one bound result")
                bound_result = bound_results[0]
                if (
                    worker_report.assignment_id != assignment_id
                    or sha256_bytes(canonical_json_bytes(bound_result))
                    != persisted_nomination.scientific_result_sha256
                ):
                    raise ValueError("worker report result differs from the nomination")
            except (OSError, ValueError) as exc:
                raise GraphValidationError(
                    f"counterexample admission binding cannot be reconstructed: {exc}"
                ) from exc
            # The admitted counterexample is found by its immutable report binding,
            # not by a recomputed ID: descriptive node IDs are agent-chosen labels.
            binding_candidates = [
                node
                for node in by_id.values()
                if node.node_type is NodeType.COUNTEREXAMPLE
                and node.problem_id == problem_id
                and node.created_in_run == run_id
                and node.metadata.get("matek_assignment_id") == assignment_id
                and node.metadata.get("matek_result_local_key") == result_local_key
            ]
            counterexample = binding_candidates[0] if len(binding_candidates) == 1 else None
            if counterexample is None:
                raise GraphValidationError(
                    "counterexample audit does not resolve to its deterministic admitted candidate"
                )
            if (
                counterexample.problem_id != problem_id
                or counterexample.created_in_run != run_id
                or counterexample.author_role != "matek-scientific-admission"
                or not node_has_scientific_admission_binding(
                    counterexample,
                    run_id=run_id,
                    assignment_id=assignment_id,
                    result=bound_result,
                )
                or counterexample.tombstone
                or counterexample.invalidation_reasons
                or counterexample.epistemic_status
                in {
                    EpistemicStatus.STALE,
                    EpistemicStatus.INCONSISTENT,
                    EpistemicStatus.REFUTED,
                }
                or counterexample.workflow_status
                in {
                    WorkflowStatus.BLOCKED,
                    WorkflowStatus.ABANDONED,
                    WorkflowStatus.SUPERSEDED,
                }
            ):
                raise GraphValidationError("admitted counterexample is no longer live")
            admitted_statement = _generated_heading_value(
                counterexample.body, "Exact statement refuted"
            )
            if (
                admitted_statement is None
                or normalize_exact_statement(admitted_statement)
                != normalize_exact_statement(persisted_nomination.exact_statement)
                or normalize_exact_statement(persisted_nomination.exact_statement)
                != target_statement
            ):
                raise GraphValidationError(
                    "admitted counterexample exact statement changed after nomination"
                )
            if persisted_nomination.proof_or_certificate not in counterexample.evidence:
                raise GraphValidationError(
                    "admitted counterexample certificate changed after audit nomination"
                )
            verified = persisted_gate.verified_refutation
            if verified is None or (
                verified.target_statement_sha256 != sha256_text(target_statement)
                or verified.certificate_sha256
                != sha256_text(persisted_nomination.proof_or_certificate)
            ):
                raise GraphValidationError(
                    "passing counterexample gate is not bound to the exact live evidence"
                )
            if not any(
                edge.relation is RelationType.REFUTES and edge.target_id == target_id
                for edge in counterexample.relations
            ):
                counterexample.relations.append(
                    GraphEdge(
                        source_id=counterexample.matek_id,
                        relation=RelationType.REFUTES,
                        target_id=target_id,
                    )
                )
            now = self._now()
            counterexample.epistemic_status = EpistemicStatus.AUDIT_PASSED
            counterexample.workflow_status = WorkflowStatus.COMPLETE
            counterexample.updated_at = now
            counterexample.last_modified_run = run_id
            counterexample.tags = list(
                dict.fromkeys(
                    [
                        *(item for item in counterexample.tags if item != "matek/branch-local"),
                        "matek/exact-main-counterexample",
                        "matek/refutation-verified",
                    ]
                )
            )
            counterexample.source_artifacts = list(
                dict.fromkeys([*counterexample.source_artifacts, source_artifact])
            )
            counterexample.metadata["matek_counterexample_audit_id"] = audit_id
            counterexample.metadata["matek_counterexample_gate_sha256"] = sha256_file(gate_path)

            target.epistemic_status = EpistemicStatus.REFUTED
            target.workflow_status = WorkflowStatus.COMPLETE
            target.updated_at = now
            target.last_modified_run = run_id
            target.source_artifacts = list(
                dict.fromkeys([*target.source_artifacts, source_artifact])
            )
            target.metadata["matek_refuted_by_counterexample_id"] = counterexample.matek_id
            target.metadata["matek_counterexample_audit_id"] = audit_id

            audit_node_id = _deterministic_id(
                NodeType.AUDIT,
                problem_id,
                run_id,
                audit_id,
                persisted_gate.target_statement_sha256,
            )
            audit_node = GraphNode(
                matek_id=audit_node_id,
                node_type=NodeType.AUDIT,
                problem_id=problem_id,
                title=f"Independent exact-counterexample audit: {counterexample.title}",
                epistemic_status=EpistemicStatus.AUDIT_PASSED,
                workflow_status=WorkflowStatus.COMPLETE,
                created_in_run=run_id,
                last_modified_run=run_id,
                author_role="counterexample-audit-gate",
                created_at=now,
                updated_at=now,
                body=new_generated_body(
                    f"Independent exact-counterexample audit: {counterexample.title}",
                    "## Exact statement audited\n\n"
                    + persisted_nomination.exact_statement
                    + "\n\n## Deterministic gate\n\n`refutation_verified`\n\n"
                    + "## Certificate SHA-256\n\n`"
                    + verified.certificate_sha256
                    + "`",
                ),
                tags=[
                    "matek/audit",
                    "matek/counterexample-audit",
                    "matek/refutation-verified",
                ],
                relations=[
                    GraphEdge(
                        source_id=audit_node_id,
                        relation=RelationType.AUDITS,
                        target_id=counterexample.matek_id,
                    ),
                    GraphEdge(
                        source_id=audit_node_id,
                        relation=RelationType.AUDITS,
                        target_id=target_id,
                    ),
                    GraphEdge(
                        source_id=audit_node_id,
                        relation=RelationType.CREATED_DURING,
                        target_id=_deterministic_id(NodeType.RUN, problem_id, run_id),
                    ),
                ],
                source_artifacts=[source_artifact],
                evidence=list(persisted_gate.response_evidence_sha256.values()),
                metadata={
                    "matek_audit_id": audit_id,
                    "matek_audit_status": persisted_gate.status.value,
                    "matek_statement_sha256": persisted_gate.target_statement_sha256,
                    "matek_origin_assignment_id": assignment_id,
                    "matek_result_local_key": result_local_key,
                },
            )
            return self._upsert_generated_nodes_unlocked(
                state=state,
                nodes=by_id,
                proposed=[counterexample, target, audit_node],
                run_id=run_id,
                author="counterexample-audit-gate",
                reason=f"Record independently verified exact counterexample {audit_id}.",
                operation_id=f"counterexample-audit:{run_id}:{audit_id}",
                source_artifacts=[source_artifact],
            )

    def _trusted_context_selection_unlocked(
        self,
        *,
        state: GraphState,
        nodes: Sequence[GraphNode],
        problem_id: str,
        maximum_nodes: int,
        include_sources: bool,
        include_audits: bool,
        include_formalizations: bool,
    ) -> tuple[list[GraphNode], dict[str, object]]:
        """Select a bounded downstream context from current Markdown trust state."""

        if maximum_nodes < 1:
            raise ValueError("trusted context maximum_nodes must be positive")
        problem_nodes = [node for node in nodes if node.problem_id == problem_id]
        by_id = {node.matek_id: node for node in problem_nodes}
        target_id = self.main_claim_id(problem_id)
        if target_id not in by_id:
            raise GraphValidationError(
                f"cannot build trusted context without canonical main claim {target_id}"
            )
        del state
        trusted_claim_node_ids = _markdown_trusted_claim_ids(problem_nodes)
        selected_by_id: dict[str, GraphNode] = {}

        for claim_id in sorted(trusted_claim_node_ids):
            node = by_id.get(claim_id)
            if node is None or not _context_node_is_live(node):
                continue
            if node.node_type is NodeType.CLAIM:
                selected_by_id[node.matek_id] = node
            elif node.node_type is NodeType.DEFINITION and (
                canonical_admitted_definition_scope(node) is not None
            ):
                selected_by_id[node.matek_id] = node

        trusted_route_node_ids: set[str] = set()
        trusted_proof_attempt_ids: set[str] = set()

        def route_conclusion(node: GraphNode) -> str | None:
            stored = node.metadata.get("matek_conclusion_claim_id")
            if isinstance(stored, str):
                return stored
            proved = [
                edge.target_id for edge in node.relations if edge.relation is RelationType.PROVES
            ]
            return proved[0] if len(proved) == 1 else None

        trusted_derivations = [
            node
            for node in problem_nodes
            if node.node_type in {NodeType.PROOF, NodeType.DERIVATION}
            and _context_node_is_live(node)
            and node.epistemic_status
            in {EpistemicStatus.AUDIT_PASSED, EpistemicStatus.LEAN_VERIFIED}
            and route_conclusion(node) in trusted_claim_node_ids
            and all(
                dependency.target_id in trusted_claim_node_ids
                or (
                    (target := by_id.get(dependency.target_id)) is not None
                    and target.node_type is NodeType.OBLIGATION
                    and (
                        target.workflow_status is WorkflowStatus.COMPLETE
                        or target.epistemic_status
                        in {EpistemicStatus.AUDIT_PASSED, EpistemicStatus.LEAN_VERIFIED}
                    )
                )
                for dependency in node.relations
                if dependency.relation is RelationType.DEPENDS_ON
            )
        ]
        for derivation_node in trusted_derivations:
            selected_by_id[derivation_node.matek_id] = derivation_node
            trusted_route_node_ids.add(derivation_node.matek_id)
            if derivation_node.node_type is NodeType.PROOF:
                continue

            proof_attempt_id = derivation_node.metadata.get("matek_proof_attempt_id")
            proof_node = by_id.get(proof_attempt_id) if isinstance(proof_attempt_id, str) else None
            if proof_node is None or not _context_node_is_live(proof_node):
                continue
            if proof_node.node_type is NodeType.PROOF:
                selected_by_id[proof_node.matek_id] = proof_node
                trusted_route_node_ids.add(proof_node.matek_id)
                continue
            if (
                proof_node.node_type is not NodeType.PROOF_ATTEMPT
                or derivation_node.node_type is not NodeType.DERIVATION
            ):
                continue
            identity = derivation_node.metadata.get("matek_admission_identity")
            payload = derivation_node.metadata.get("matek_admission_payload_sha256")
            if (
                isinstance(identity, str)
                and isinstance(payload, str)
                and proof_node.metadata.get("matek_admission_identity") == identity
                and proof_node.metadata.get("matek_admission_payload_sha256") == payload
                and matches_admission_binding(derivation_node, identity, payload)
                and matches_admission_binding(proof_node, identity, payload)
                and any(
                    edge.relation is RelationType.RELATED_TO
                    and edge.target_id == proof_node.matek_id
                    for edge in derivation_node.relations
                )
            ):
                selected_by_id[proof_node.matek_id] = proof_node
                trusted_proof_attempt_ids.add(proof_node.matek_id)

        if include_sources:
            for node in problem_nodes:
                if _verified_source_for_context(node):
                    selected_by_id[node.matek_id] = node

        if include_formalizations:
            for node in problem_nodes:
                if _verified_formalization_for_context(
                    node,
                    nodes=by_id,
                    trusted_claim_node_ids=trusted_claim_node_ids,
                ):
                    selected_by_id[node.matek_id] = node

        if include_audits:
            audit_anchor_ids = set(selected_by_id)
            for node in problem_nodes:
                if (
                    node.node_type is NodeType.AUDIT
                    and _context_node_is_live(node)
                    and node.epistemic_status
                    in {EpistemicStatus.AUDIT_PASSED, EpistemicStatus.LEAN_VERIFIED}
                    and node.workflow_status is WorkflowStatus.COMPLETE
                    and any(
                        edge.relation is RelationType.AUDITS and edge.target_id in audit_anchor_ids
                        for edge in node.relations
                    )
                ):
                    selected_by_id[node.matek_id] = node

        accepted_main_proof_ids = {
            node_id
            for node_id in trusted_route_node_ids
            for node in [by_id[node_id]]
            if node.node_type is NodeType.PROOF
            and node.author_role == "candidate-packager"
            and node.metadata.get("matek_acceptance_gate_passed") is True
            and any(
                edge.relation is RelationType.PROVES and edge.target_id == target_id
                for edge in node.relations
            )
        }
        main_support_ids: set[str] = {target_id, *accepted_main_proof_ids}
        for accepted_proof_id in accepted_main_proof_ids:
            main_support_ids.update(
                self._main_result_support_ids(
                    by_id,
                    target_id=target_id,
                    accepted_proof_id=accepted_proof_id,
                )
            )
        for derivation_node in trusted_derivations:
            proof_attempt_id = derivation_node.metadata.get("matek_proof_attempt_id")
            if derivation_node.matek_id in main_support_ids and isinstance(proof_attempt_id, str):
                main_support_ids.add(proof_attempt_id)
        audit_support_ids = {
            node.matek_id
            for node in selected_by_id.values()
            if node.node_type is NodeType.AUDIT
            and any(
                edge.relation is RelationType.AUDITS and edge.target_id in main_support_ids
                for edge in node.relations
            )
        }
        main_support_ids.update(audit_support_ids)

        type_priority = {
            NodeType.CLAIM: 0,
            NodeType.DEFINITION: 1,
            NodeType.PROOF: 2,
            NodeType.PROOF_ATTEMPT: 3,
            NodeType.DERIVATION: 4,
            NodeType.SOURCE: 5,
            NodeType.AUDIT: 6,
            NodeType.FORMALIZATION: 7,
        }

        def priority(node: GraphNode) -> tuple[int, int, str]:
            if node.matek_id == target_id:
                group = 0
            elif node.matek_id in accepted_main_proof_ids:
                group = 1
            elif node.matek_id in main_support_ids:
                group = 2
            elif node.matek_id in trusted_claim_node_ids:
                group = 3
            elif node.matek_id in trusted_route_node_ids | trusted_proof_attempt_ids:
                group = 4
            else:
                group = 5
            return (group, type_priority.get(node.node_type, 99), node.matek_id)

        eligible = sorted(selected_by_id.values(), key=priority)
        included = eligible[:maximum_nodes]
        omitted_count = len(eligible) - len(included)
        selection = {
            "policy": _TRUSTED_CONTEXT_POLICY,
            "maximum_nodes": maximum_nodes,
            "eligible_node_count": len(eligible),
            "included_node_count": len(included),
            "omitted_node_count": omitted_count,
            "truncated": omitted_count > 0,
            "priority_order": [
                "main_target",
                "accepted_main_proof",
                "accepted_main_proof_support",
                "other_markdown_trusted_mathematics",
                "verified_evidence",
            ],
        }
        return included, selection

    def manuscript_context(self, problem_id: str) -> dict[str, object]:
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=False)
            accepted, selection = self._trusted_context_selection_unlocked(
                state=state,
                nodes=nodes,
                problem_id=problem_id,
                maximum_nodes=MANUSCRIPT_CONTEXT_MAXIMUM_NODES,
                include_sources=True,
                include_audits=True,
                include_formalizations=False,
            )
            return {
                "graph_revision": state.revision,
                "problem_id": problem_id,
                "selection": selection,
                "accepted_nodes": [
                    {
                        "node": _node_summary(node).model_dump(mode="json"),
                        "content": generated_section(node.body),
                        "relations": [edge.model_dump(mode="json") for edge in node.relations],
                    }
                    for node in accepted
                ],
                "instruction": (
                    "Use only accepted claim/proof nodes for theorem content. Preserve dependency "
                    "order and return manuscript mappings for durable graph recording."
                ),
            }

    def record_manuscript_result(
        self,
        *,
        problem_id: str,
        run_id: str,
        manuscript_result: Mapping[str, Any],
    ) -> GraphMergeResult:
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes_list = self._load_nodes_unlocked(include_human_notes=True)
            by_id = {node.matek_id: node for node in nodes_list}
            now = self._now()
            outcome = str(manuscript_result.get("outcome") or "unknown")
            draft = manuscript_result.get("draft", {})
            draft_map = draft if isinstance(draft, Mapping) else {}
            claims = draft_map.get("claims", [])
            proposed: list[GraphNode] = []
            target = by_id.get(self.main_claim_id(problem_id))
            if target is not None:
                target.manuscript_mappings = list(
                    dict.fromkeys(
                        [
                            *target.manuscript_mappings,
                            *(
                                f"{target.matek_id} -> manuscript claim {index}"
                                for index, _ in enumerate(
                                    claims if isinstance(claims, list) else [], start=1
                                )
                            ),
                        ]
                    )
                )
                target.updated_at = now
                target.last_modified_run = run_id
                proposed.append(target)
            artifact_specs = (
                ("paper.tex", "LaTeX manuscript source"),
                ("references.bib", "Verified bibliography"),
                ("paper.pdf", "Compiled manuscript PDF"),
                ("bibliography_audit.json", "Bibliography verification audit"),
            )
            for filename, title in artifact_specs:
                artifact_id = _deterministic_id(NodeType.ARTIFACT, problem_id, run_id, filename)
                proposed.append(
                    GraphNode(
                        matek_id=artifact_id,
                        node_type=NodeType.ARTIFACT,
                        problem_id=problem_id,
                        title=title,
                        epistemic_status=(
                            EpistemicStatus.AUDIT_PASSED
                            if outcome == "compiled"
                            else EpistemicStatus.OPEN
                        ),
                        workflow_status=(
                            WorkflowStatus.COMPLETE
                            if outcome == "compiled"
                            else WorkflowStatus.BLOCKED
                        ),
                        created_in_run=run_id,
                        last_modified_run=run_id,
                        author_role="manuscript-stage",
                        created_at=now,
                        updated_at=now,
                        body=new_generated_body(
                            title,
                            "## Artifact\n\n"
                            + f"`.matek/runs/{run_id}/manuscript/{filename}`"
                            + "\n\n## Manuscript outcome\n\n"
                            + f"`{outcome}`",
                        ),
                        tags=["matek/artifact", "matek/manuscript"],
                        relations=[
                            GraphEdge(
                                source_id=artifact_id,
                                relation=RelationType.RELATED_TO,
                                target_id=self.main_claim_id(problem_id),
                            ),
                            GraphEdge(
                                source_id=artifact_id,
                                relation=RelationType.CREATED_DURING,
                                target_id=_deterministic_id(NodeType.RUN, problem_id, run_id),
                            ),
                        ],
                        source_artifacts=[f".matek/runs/{run_id}/manuscript/{filename}"],
                        metadata={"matek_manuscript_outcome": outcome},
                    )
                )
            bibliography = manuscript_result.get("bibliography_audit")
            if isinstance(bibliography, Mapping):
                entries = bibliography.get("entries", [])
                if isinstance(entries, list):
                    source_nodes = [
                        node for node in by_id.values() if node.node_type is NodeType.SOURCE
                    ]
                    for entry in entries:
                        if not isinstance(entry, Mapping):
                            continue
                        key = str(entry.get("citation_key") or "")
                        for source in source_nodes:
                            if key and (
                                key == source.metadata.get("matek_source_id")
                                or key.casefold() in source.title.casefold()
                            ):
                                source.manuscript_mappings = list(
                                    dict.fromkeys(
                                        [
                                            *source.manuscript_mappings,
                                            f"{source.matek_id} -> {key}",
                                        ]
                                    )
                                )
                                source.metadata["matek_bibtex_key"] = key
                                source.updated_at = now
                                source.last_modified_run = run_id
                                proposed.append(source)
            return self._upsert_generated_nodes_unlocked(
                state=state,
                nodes=by_id,
                proposed=proposed,
                run_id=run_id,
                author="manuscript-stage",
                reason=f"Record manuscript mappings and artifact nodes for outcome {outcome}.",
                operation_id=f"manuscript-result:{run_id}",
                source_artifacts=[f".matek/runs/{run_id}/manuscript/result.json"],
            )

    def formalization_context(self, problem_id: str) -> dict[str, object]:
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes = self._load_nodes_unlocked(include_human_notes=False)
            selected, selection = self._trusted_context_selection_unlocked(
                state=state,
                nodes=nodes,
                problem_id=problem_id,
                maximum_nodes=FORMALIZATION_CONTEXT_MAXIMUM_NODES,
                include_sources=False,
                include_audits=False,
                include_formalizations=True,
            )
            return {
                "graph_revision": state.revision,
                "problem_id": problem_id,
                "selection": selection,
                "statement_nodes": [
                    {
                        "node": _node_summary(node).model_dump(mode="json"),
                        "content": generated_section(node.body),
                        "content_hash": node.content_hash,
                    }
                    for node in selected
                ],
            }

    def record_lean_result(
        self,
        *,
        problem_id: str,
        run_id: str,
        lean_result: Mapping[str, Any],
        lean_toolchain: str,
        mathlib_revision: str,
        source_file_hash: str | None,
        axiom_report_hash: str | None,
    ) -> GraphMergeResult:
        """Attach formalization to one exact statement version and build record."""

        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes_list = self._load_nodes_unlocked(include_human_notes=True)
            by_id = {node.matek_id: node for node in nodes_list}
            claim_id = self.main_claim_id(problem_id)
            claim = by_id.get(claim_id)
            if claim is None:
                raise GraphValidationError("main claim is missing before Lean graph integration")
            now = self._now()
            outcome = str(lean_result.get("outcome") or "LEAN_FAILED")
            verification = lean_result.get("verification")
            verification_map = verification if isinstance(verification, Mapping) else {}
            alignment = lean_result.get("alignment")
            alignment_map = alignment if isinstance(alignment, Mapping) else {}
            statement = lean_result.get("statement_draft")
            statement_map = statement if isinstance(statement, Mapping) else {}
            verified = (
                outcome in {"LEAN_VERIFIED", "LEAN_VERIFIED_WITH_APPROVED_AXIOMS"}
                and bool(verification_map.get("passed", False))
                and str(verification_map.get("statement_hash_expected") or "")
                == str(verification_map.get("statement_hash_actual") or "")
                and str(alignment_map.get("status") or "") == "aligned"
            )
            formalization_id = _deterministic_id(
                NodeType.FORMALIZATION,
                problem_id,
                claim_id,
                str(claim.statement_version),
                run_id,
            )
            theorem_name = str(statement_map.get("theorem_name") or "unknown")
            statement_digest = str(
                lean_result.get("approved_statement_hash")
                or verification_map.get("statement_hash_expected")
                or ""
            )
            formalization = GraphNode(
                matek_id=formalization_id,
                node_type=NodeType.FORMALIZATION,
                problem_id=problem_id,
                title=f"Lean formalization of {claim.title}",
                epistemic_status=(
                    EpistemicStatus.LEAN_VERIFIED
                    if verified
                    else EpistemicStatus.CANDIDATE
                    if statement_map
                    else EpistemicStatus.OPEN
                ),
                workflow_status=(WorkflowStatus.COMPLETE if verified else WorkflowStatus.BLOCKED),
                created_in_run=run_id,
                last_modified_run=run_id,
                author_role="deterministic-lean-verifier" if verified else "lean-stage",
                created_at=now,
                updated_at=now,
                body=new_generated_body(
                    f"Lean formalization of {claim.title}",
                    "## Claim linkage\n\n"
                    + f"- Claim: {wikilink_for(claim)}\n"
                    + f"- Statement version: `{claim.statement_version}`\n"
                    + f"- Statement hash: `{statement_digest or 'unknown'}`\n"
                    + "\n## Lean declaration\n\n"
                    + f"`{theorem_name}`\n\n"
                    + "## Build result\n\n"
                    + f"`{outcome}`\n\n"
                    + "## Axiom report\n\n"
                    + (
                        "\n".join(f"- `{item}`" for item in verification_map.get("used_axioms", []))
                        or "_No axioms reported._"
                    ),
                ),
                tags=[
                    "matek/formalization",
                    "matek/lean-verified" if verified else "matek/lean-open",
                ],
                relations=[
                    GraphEdge(
                        source_id=formalization_id,
                        relation=RelationType.FORMALIZES,
                        target_id=claim_id,
                    ),
                    GraphEdge(
                        source_id=formalization_id,
                        relation=RelationType.CREATED_DURING,
                        target_id=_deterministic_id(NodeType.RUN, problem_id, run_id),
                    ),
                ],
                source_artifacts=[
                    f".matek/runs/{run_id}/lean/challenge.lean",
                    f".matek/runs/{run_id}/lean/Main.lean",
                    f".matek/runs/{run_id}/lean/build.log",
                    f".matek/runs/{run_id}/lean/axioms.txt",
                ],
                metadata={
                    "matek_claim_id": claim_id,
                    "matek_statement_version": claim.statement_version,
                    "matek_statement_hash": statement_digest,
                    "matek_lean_declaration": theorem_name,
                    "matek_source_file_hash": source_file_hash,
                    "matek_lean_version": lean_toolchain,
                    "matek_mathlib_revision": mathlib_revision,
                    "matek_build_result": outcome,
                    "matek_axiom_report_hash": axiom_report_hash,
                    "matek_deterministic_verification_passed": verified,
                },
            )
            if verified:
                claim.epistemic_status = EpistemicStatus.LEAN_VERIFIED
                claim.workflow_status = WorkflowStatus.COMPLETE
                claim.invalidation_reasons = []
                claim.updated_at = now
                claim.last_modified_run = run_id
                claim.author_role = "deterministic-lean-verifier"
                claim.metadata.update(
                    {
                        "matek_lean_statement_version": claim.statement_version,
                        "matek_lean_statement_hash": statement_digest,
                        "matek_lean_formalization_id": formalization_id,
                    }
                )
            return self._upsert_generated_nodes_unlocked(
                state=state,
                nodes=by_id,
                proposed=[claim, formalization],
                run_id=run_id,
                author="deterministic-lean-verifier" if verified else "lean-stage",
                reason=f"Attach Lean outcome {outcome} to exact claim statement version.",
                operation_id=f"lean-result:{run_id}",
                source_artifacts=[f".matek/runs/{run_id}/lean/result.json"],
            )

    def record_run_status(
        self,
        *,
        problem_id: str,
        run_id: str,
        scientific_status: str,
        strongest_result: str,
        unresolved_obligations: Sequence[str],
        complete: bool,
    ) -> GraphMergeResult:
        with self._locked():
            self._recover_pending_unlocked()
            state = self._load_state_unlocked()
            nodes_list = self._load_nodes_unlocked(include_human_notes=True)
            by_id = {node.matek_id: node for node in nodes_list}
            run_node_id = _deterministic_id(NodeType.RUN, problem_id, run_id)
            run_node = by_id.get(run_node_id)
            if run_node is None:
                raise GraphValidationError(f"run node is missing: {run_node_id}")
            now = self._now()
            run_node.workflow_status = (
                WorkflowStatus.COMPLETE if complete else WorkflowStatus.IN_PROGRESS
            )
            run_node.epistemic_status = (
                EpistemicStatus.AUDIT_PASSED
                if scientific_status
                in {
                    "RESEARCH_ACCEPTED_FOR_MANUSCRIPT",
                    "LEAN_VERIFIED",
                    "LEAN_VERIFIED_WITH_APPROVED_AXIOMS",
                }
                else EpistemicStatus.OPEN
            )
            run_node.updated_at = now
            run_node.last_modified_run = run_id
            run_node.body = replace_generated_section(
                run_node.body,
                run_node.title,
                "## Run summary\n\n"
                + f"Scientific status: `{scientific_status}`\n\n"
                + "## Strongest result\n\n"
                + (strongest_result or "_No complete result established._")
                + "\n\n## Unresolved obligations\n\n"
                + ("\n".join(f"- {item}" for item in unresolved_obligations) or "_None._")
                + "\n\n## Run artifacts\n\n"
                + f"- `.matek/runs/{run_id}/`",
            )
            return self._upsert_generated_nodes_unlocked(
                state=state,
                nodes=by_id,
                proposed=[run_node],
                run_id=run_id,
                author="matek-workflow",
                reason=f"Record run status {scientific_status} in persistent graph memory.",
                operation_id=f"run-status:{run_id}:{scientific_status}:{int(complete)}",
                source_artifacts=[f".matek/runs/{run_id}/state.json"],
            )


__all__ = [
    "GRAPH_COLLECTION_RELATIVE",
    "GRAPH_DIRECTORIES",
    "GRAPH_SCHEMA_VERSION",
    "MAIN_RESULT_NEEDS_TAG",
    "GraphConflictError",
    "GraphNotInitializedError",
    "GraphValidationError",
    "KnowledgeGraph",
    "KnowledgeGraphError",
    "list_graph_names",
    "normalize_graph_name",
    "problem_graph_name",
]
