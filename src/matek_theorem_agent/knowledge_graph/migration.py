"""Deterministic planning and integrity records for legacy graph backfill.

Legacy Markdown graphs are valuable research archives, but their prose must not be
silently promoted into the canonical proof ledger.  This module inspects immutable
``GraphNode`` values and emits a review plan.  Planning never mutates source nodes or
writes inside the graph vault.  A separately confirmed service operation may later apply
that exact integrity-bound plan while retaining the legacy notes as archive evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..scientific import ScientificScope, normalize_exact_statement
from ..workspace import atomic_write_json
from .ledger import Derivation, DerivationStatus, deterministic_ledger_id, logical_version
from .markdown import exact_statement, generated_section
from .models import (
    EpistemicStatus,
    GraphNode,
    NodeType,
    RelationType,
    WorkflowStatus,
    validate_node_id,
)

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_SECTION = re.compile(r"(?m)^##\s+(.+?)\s*$")
_REFERENCE_CANDIDATE = re.compile(r"\b(?:CLM|PRF|APR)-[A-Za-z0-9]{1,80}\b", re.IGNORECASE)
_DISJUNCTION = re.compile(r"\b(?:either|or)\b", re.IGNORECASE)
_MECHANISM_ONLY = (
    re.compile(
        r"\b(?:only|merely|solely)\b.{0,100}"
        r"\b(?:mechanism|approach|branch|route|strategy|strengthening|invariant)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:mechanism|approach|branch|route|strategy|strengthening|invariant)\b"
        r".{0,100}\b(?:only|merely|solely)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:does not|doesn't|cannot|can't)\s+refute\b.{0,100}"
        r"\b(?:theorem|main (?:target|claim)|claim)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:rules?|ruled)\s+out\b.{0,100}"
        r"\b(?:approach|branch|route|strategy|mechanism)\b",
        re.IGNORECASE | re.DOTALL,
    ),
)
_DEPENDENCY_FIELDS = (
    "matek_dependencies",
    "dependencies",
    "dependency_node_ids",
    "matek_dependency_claim_ids",
)
_GRAPH_NAME = re.compile(r"\A[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\Z")


class LegacyMigrationError(RuntimeError):
    """A migration plan could not be built or failed its integrity check."""


class _MigrationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _stable_id(value: str) -> str:
    try:
        return validate_node_id(value)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


def _stable_ids(values: list[str]) -> list[str]:
    return list(dict.fromkeys(_stable_id(value) for value in values))


class ProofAttemptReclassification(_MigrationModel):
    proof_node_id: str
    exact_gap: str
    proposed_node_type: Literal["proof_attempt"] = "proof_attempt"
    archive_preserved: Literal[True] = True

    @field_validator("proof_node_id")
    @classmethod
    def proof_id_is_valid(cls, value: str) -> str:
        normalized = _stable_id(value)
        if not normalized.startswith("PRF-"):
            raise ValueError("legacy proof reclassification requires a PRF node ID")
        return normalized

    @field_validator("exact_gap")
    @classmethod
    def exact_gap_is_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("proof-attempt reclassification requires an exact gap")
        return normalized


class DependencyExtraction(_MigrationModel):
    source_node_id: str
    source_fields: list[str]
    raw_entries: list[str]
    claim_ids: list[str] = Field(default_factory=list)
    proof_ids: list[str] = Field(default_factory=list)
    approach_ids: list[str] = Field(default_factory=list)
    unknown_ids: list[str] = Field(default_factory=list)
    review_blockers: list[str] = Field(default_factory=list)

    @field_validator("source_node_id")
    @classmethod
    def source_id_is_valid(cls, value: str) -> str:
        return _stable_id(value)

    @field_validator("claim_ids", "proof_ids", "approach_ids", "unknown_ids")
    @classmethod
    def reference_ids_are_valid(cls, values: list[str]) -> list[str]:
        return _stable_ids(values)

    @field_validator("source_fields", "raw_entries", "review_blockers")
    @classmethod
    def text_lists_are_normalized(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class ReviewDerivationProposal(_MigrationModel):
    proposal_id: str
    derivation: Derivation
    supporting_archive_node_ids: list[str] = Field(default_factory=list)
    source_dependency_fields: list[str] = Field(default_factory=list)
    disposition: Literal["review_required"] = "review_required"

    @field_validator("proposal_id")
    @classmethod
    def proposal_id_is_valid(cls, value: str) -> str:
        normalized = _stable_id(value)
        if not normalized.startswith("DRV-"):
            raise ValueError("derivation proposal IDs must use the DRV prefix")
        return normalized

    @field_validator("supporting_archive_node_ids")
    @classmethod
    def supporting_ids_are_valid(cls, values: list[str]) -> list[str]:
        return _stable_ids(values)

    @field_validator("source_dependency_fields")
    @classmethod
    def fields_are_normalized(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @model_validator(mode="after")
    def proposal_matches_derivation(self) -> ReviewDerivationProposal:
        if self.proposal_id != self.derivation.derivation_id:
            raise ValueError("proposal_id must equal its derivation_id")
        if self.derivation.status is not DerivationStatus.PROPOSED:
            raise ValueError("legacy derivations may only be proposed for independent review")
        return self


class RefutationQuarantine(_MigrationModel):
    refutation_node_id: str
    main_target_id: str
    original_relation: Literal["refutes"] = "refutes"
    proposed_scope: Literal[ScientificScope.BRANCH] = ScientificScope.BRANCH
    candidate_branch_target_ids: list[str] = Field(default_factory=list)
    reason: str
    archive_preserved: Literal[True] = True

    @field_validator("refutation_node_id", "main_target_id")
    @classmethod
    def linked_ids_are_valid(cls, value: str) -> str:
        return _stable_id(value)

    @field_validator("candidate_branch_target_ids")
    @classmethod
    def branch_targets_are_valid(cls, values: list[str]) -> list[str]:
        normalized = _stable_ids(values)
        if any(not value.startswith("APR-") for value in normalized):
            raise ValueError("quarantine branch targets must be approach node IDs")
        return normalized

    @field_validator("reason")
    @classmethod
    def reason_is_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("refutation quarantine requires a reason")
        return normalized


class ClaimAliasGroup(_MigrationModel):
    canonical_candidate_id: str
    alias_ids: list[str]
    exact_statement: str
    logical_version: str
    scopes: list[ScientificScope]
    disposition: Literal["ready_for_review", "scope_conflict"]

    @field_validator("canonical_candidate_id")
    @classmethod
    def canonical_id_is_valid(cls, value: str) -> str:
        normalized = _stable_id(value)
        if not normalized.startswith("CLM-"):
            raise ValueError("canonical claim candidates must use the CLM prefix")
        return normalized

    @field_validator("alias_ids")
    @classmethod
    def aliases_are_valid(cls, values: list[str]) -> list[str]:
        normalized = _stable_ids(values)
        if any(not value.startswith("CLM-") for value in normalized):
            raise ValueError("claim aliases must use the CLM prefix")
        return normalized

    @field_validator("exact_statement")
    @classmethod
    def statement_is_exact(cls, value: str) -> str:
        normalized = normalize_exact_statement(value)
        if not normalized:
            raise ValueError("claim alias groups require an exact statement")
        return normalized

    @field_validator("logical_version")
    @classmethod
    def logical_version_is_valid(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not _SHA256.fullmatch(normalized):
            raise ValueError("claim logical versions must be lowercase SHA-256 values")
        return normalized

    @model_validator(mode="after")
    def group_is_consistent(self) -> ClaimAliasGroup:
        if not self.alias_ids:
            raise ValueError("claim alias groups require at least one alias")
        if self.canonical_candidate_id in self.alias_ids:
            raise ValueError("canonical claim candidate cannot also be an alias")
        if self.logical_version != logical_version(self.exact_statement):
            raise ValueError("claim alias group logical version does not match its statement")
        if (len(self.scopes) > 1) != (self.disposition == "scope_conflict"):
            raise ValueError("claim alias disposition must reflect its distinct scopes")
        return self


def _independent_audit_lanes() -> list[Literal["verifier", "falsifier"]]:
    return ["verifier", "falsifier"]


class AuditNomination(_MigrationModel):
    claim_id: str
    proof_node_id: str
    exact_statement: str
    logical_version: str
    scope: ScientificScope
    strength_score: int = Field(ge=0, le=100)
    reasons: list[str]
    independent_lanes: list[Literal["verifier", "falsifier"]] = Field(
        default_factory=_independent_audit_lanes
    )

    @field_validator("claim_id", "proof_node_id")
    @classmethod
    def linked_ids_are_valid(cls, value: str) -> str:
        return _stable_id(value)

    @field_validator("exact_statement")
    @classmethod
    def statement_is_exact(cls, value: str) -> str:
        normalized = normalize_exact_statement(value)
        if not normalized:
            raise ValueError("audit nominations require an exact statement")
        return normalized

    @field_validator("logical_version")
    @classmethod
    def logical_version_is_valid(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not _SHA256.fullmatch(normalized):
            raise ValueError("audit nomination logical versions must be SHA-256 values")
        return normalized

    @field_validator("reasons")
    @classmethod
    def reasons_are_nonblank(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not normalized:
            raise ValueError("audit nominations require at least one ranking reason")
        return normalized

    @model_validator(mode="after")
    def version_matches_statement(self) -> AuditNomination:
        if self.logical_version != logical_version(self.exact_statement):
            raise ValueError("audit nomination logical version does not match its statement")
        if self.independent_lanes != ["verifier", "falsifier"]:
            raise ValueError("fresh lemma audits require independent verifier and falsifier lanes")
        return self


class MigrationIssue(_MigrationModel):
    code: str
    source_node_ids: list[str]
    detail: str
    referenced_ids: list[str] = Field(default_factory=list)

    @field_validator("source_node_ids", "referenced_ids")
    @classmethod
    def node_ids_are_valid(cls, values: list[str]) -> list[str]:
        return _stable_ids(values)

    @field_validator("code", "detail")
    @classmethod
    def text_is_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("migration issue fields must not be blank")
        return normalized

    @model_validator(mode="after")
    def issue_has_a_source(self) -> MigrationIssue:
        if not self.source_node_ids:
            raise ValueError("migration issues must identify at least one archive node")
        return self


class LegacyMigrationReport(_MigrationModel):
    schema_version: Literal[1] = 1
    artifact_type: Literal["matek.legacy_graph_migration.plan"] = (
        "matek.legacy_graph_migration.plan"
    )
    mode: Literal["dry_run"] = "dry_run"
    source_graph_name: str | None = None
    source_graph_revision: str
    source_archive_sha256: str
    source_node_count: int = Field(ge=1)
    audit_nomination_limit: int = Field(default=25, ge=1)
    problem_id: str
    target_claim_id: str
    proof_attempt_reclassifications: list[ProofAttemptReclassification] = Field(
        default_factory=list
    )
    dependency_extractions: list[DependencyExtraction] = Field(default_factory=list)
    derivation_proposals: list[ReviewDerivationProposal] = Field(default_factory=list)
    refutation_quarantines: list[RefutationQuarantine] = Field(default_factory=list)
    claim_alias_groups: list[ClaimAliasGroup] = Field(default_factory=list)
    audit_nominations: list[AuditNomination] = Field(default_factory=list)
    ambiguous_dependencies: list[MigrationIssue] = Field(default_factory=list)
    scope_conflicts: list[MigrationIssue] = Field(default_factory=list)
    review_notes: list[MigrationIssue] = Field(default_factory=list)
    source_edits_applied: Literal[False] = False

    @field_validator("source_graph_name")
    @classmethod
    def graph_name_is_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().casefold()
        if not _GRAPH_NAME.fullmatch(normalized):
            raise ValueError("source graph name is not a portable MATEK graph name")
        return normalized

    @field_validator("source_graph_revision")
    @classmethod
    def revision_is_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("migration reports require a source graph revision")
        return normalized

    @field_validator("source_archive_sha256")
    @classmethod
    def archive_digest_is_valid(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not _SHA256.fullmatch(normalized):
            raise ValueError("source archive digest must be a lowercase SHA-256 value")
        return normalized

    @field_validator("problem_id", "target_claim_id")
    @classmethod
    def identities_are_valid(cls, value: str) -> str:
        return _stable_id(value)


class LegacyMigrationApplicationRecord(_MigrationModel):
    """Immutable evidence describing one committed reviewed migration plan."""

    schema_version: Literal[1] = 1
    artifact_type: Literal["matek.legacy_graph_migration.application"] = (
        "matek.legacy_graph_migration.application"
    )
    status: Literal["applied", "already_applied"] = "applied"
    plan_sha256: str
    graph_name: str
    problem_id: str
    target_claim_id: str
    source_graph_revision: str
    source_archive_sha256: str
    operation_id: str
    previous_revision: str
    new_revision: str
    applied_at: datetime
    proof_attempt_node_ids: list[str] = Field(default_factory=list)
    derivation_node_ids: list[str] = Field(default_factory=list)
    updated_archive_node_ids: list[str] = Field(default_factory=list)
    recorded_alias_ids: list[str] = Field(default_factory=list)
    quarantined_refutation_node_ids: list[str] = Field(default_factory=list)
    retargeted_refutation_node_ids: list[str] = Field(default_factory=list)
    audit_task_node_ids: list[str] = Field(default_factory=list)
    unapplied_issues: list[MigrationIssue] = Field(default_factory=list)

    @field_validator("plan_sha256", "source_archive_sha256")
    @classmethod
    def digests_are_valid(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not _SHA256.fullmatch(normalized):
            raise ValueError("migration application digests must be lowercase SHA-256 values")
        return normalized

    @field_validator("graph_name")
    @classmethod
    def applied_graph_name_is_valid(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not _GRAPH_NAME.fullmatch(normalized):
            raise ValueError("application graph name is not a portable MATEK graph name")
        return normalized

    @field_validator("problem_id", "target_claim_id")
    @classmethod
    def application_identities_are_valid(cls, value: str) -> str:
        return _stable_id(value)

    @field_validator(
        "proof_attempt_node_ids",
        "derivation_node_ids",
        "updated_archive_node_ids",
        "recorded_alias_ids",
        "quarantined_refutation_node_ids",
        "retargeted_refutation_node_ids",
        "audit_task_node_ids",
    )
    @classmethod
    def application_node_ids_are_valid(cls, values: list[str]) -> list[str]:
        return _stable_ids(values)

    @field_validator("source_graph_revision", "operation_id", "previous_revision", "new_revision")
    @classmethod
    def application_text_is_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("migration application identity fields must not be blank")
        return normalized


def _issue(
    issues: list[MigrationIssue],
    *,
    code: str,
    source_node_ids: Iterable[str],
    detail: str,
    referenced_ids: Iterable[str] = (),
) -> None:
    issues.append(
        MigrationIssue(
            code=code,
            source_node_ids=sorted(set(source_node_ids)),
            detail=detail,
            referenced_ids=sorted(set(referenced_ids)),
        )
    )


def _deduplicate_issues(issues: Iterable[MigrationIssue]) -> list[MigrationIssue]:
    keyed = {
        (
            issue.code,
            tuple(issue.source_node_ids),
            issue.detail,
            tuple(issue.referenced_ids),
        ): issue
        for issue in issues
    }
    return [keyed[key] for key in sorted(keyed)]


def _section_text(body: str, heading: str) -> str | None:
    managed = generated_section(body)
    matches = list(_SECTION.finditer(managed))
    for index, match in enumerate(matches):
        if match.group(1).strip().casefold() != heading.casefold():
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(managed)
        return managed[start:end].strip()
    return None


def _gap_declaration(node: GraphNode) -> tuple[Literal["gapped", "gap_free", "unknown"], str]:
    raw_metadata = node.metadata.get("matek_exact_gap")
    raw = raw_metadata.strip() if isinstance(raw_metadata, str) else ""
    if not raw:
        raw = (_section_text(node.body, "Exact gap") or "").strip()
    if not raw:
        return "unknown", ""
    plain = re.sub(r"[`_*]", "", raw).strip().casefold().rstrip(".!")
    if (
        plain in {"none", "none declared", "n/a", "not applicable", "null"}
        or plain.startswith("no gap")
        or plain.startswith("the proof is complete")
        or plain.startswith("proof complete")
    ):
        return "gap_free", raw
    return "gapped", raw


def _claim_scope(
    node: GraphNode,
    *,
    target_claim_id: str,
    issues: list[MigrationIssue],
) -> ScientificScope:
    if node.matek_id == target_claim_id or "matek/main-target" in node.tags:
        return ScientificScope.MAIN
    raw = node.metadata.get("matek_scientific_scope")
    if raw is None:
        return ScientificScope.BRANCH
    try:
        return ScientificScope(str(raw))
    except ValueError:
        _issue(
            issues,
            code="unknown_claim_scope",
            source_node_ids=[node.matek_id],
            detail=f"Unknown scientific scope {raw!r}; no scope conversion was proposed.",
        )
        return ScientificScope.BRANCH


def legacy_archive_sha256(nodes: Sequence[GraphNode], *, problem_id: str) -> str:
    """Hash the complete, ordered archive view for one problem."""

    normalized_problem_id = _stable_id(problem_id)
    payload = [
        node.model_dump(mode="json")
        for node in sorted(
            (item for item in nodes if item.problem_id == normalized_problem_id),
            key=lambda item: item.matek_id,
        )
    ]
    if not payload:
        raise LegacyMigrationError(f"archive contains no nodes for {normalized_problem_id}")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _dependency_entries(
    node: GraphNode,
    *,
    issues: list[MigrationIssue],
) -> tuple[list[str], list[str]]:
    fields: list[str] = []
    entries: list[str] = []
    for field in _DEPENDENCY_FIELDS:
        if field not in node.metadata:
            continue
        raw = node.metadata[field]
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            _issue(
                issues,
                code="malformed_dependency_array",
                source_node_ids=[node.matek_id],
                detail=f"Legacy dependency field {field!r} is not an array of strings.",
            )
            continue
        fields.append(field)
        entries.extend(item.strip() for item in raw if item.strip())
    return sorted(set(fields)), list(dict.fromkeys(entries))


def _extract_dependencies(
    node: GraphNode,
    *,
    by_id: Mapping[str, GraphNode],
    claim_aliases: Mapping[str, str],
    scope_conflict_ids: set[str],
    issues: list[MigrationIssue],
) -> DependencyExtraction | None:
    fields, entries = _dependency_entries(node, issues=issues)
    if not fields:
        return None
    claims: list[str] = []
    proofs: list[str] = []
    approaches: list[str] = []
    unknown: list[str] = []
    blockers: list[str] = []
    expected_types = {
        "CLM": NodeType.CLAIM,
        "PRF": NodeType.PROOF,
        "APR": NodeType.APPROACH,
    }
    for entry in entries:
        candidates = list(
            dict.fromkeys(match.group(0).upper() for match in _REFERENCE_CANDIDATE.finditer(entry))
        )
        valid: list[str] = []
        for candidate in candidates:
            try:
                valid.append(validate_node_id(candidate))
            except ValueError:
                blockers.append("invalid_dependency_reference")
                _issue(
                    issues,
                    code="invalid_dependency_reference",
                    source_node_ids=[node.matek_id],
                    detail=f"Dependency entry contains a malformed stable reference: {entry!r}.",
                )
        if not valid:
            blockers.append("unresolved_free_text_dependency")
            _issue(
                issues,
                code="unresolved_free_text_dependency",
                source_node_ids=[node.matek_id],
                detail=f"Dependency text has no valid CLM/PRF/APR reference: {entry!r}.",
            )
            continue
        if len(valid) > 1 and _DISJUNCTION.search(entry):
            blockers.append("disjunctive_dependency")
            _issue(
                issues,
                code="disjunctive_dependency",
                source_node_ids=[node.matek_id],
                detail=(
                    "A free-text dependency uses OR semantics; it was not converted "
                    "to AND premises."
                ),
                referenced_ids=valid,
            )
            continue
        for reference in valid:
            referenced = by_id.get(reference)
            if referenced is None:
                unknown.append(reference)
                blockers.append("unknown_dependency_reference")
                _issue(
                    issues,
                    code="unknown_dependency_reference",
                    source_node_ids=[node.matek_id],
                    detail="A syntactically valid dependency reference is absent from the archive.",
                    referenced_ids=[reference],
                )
                continue
            expected = expected_types[reference[:3]]
            if referenced.node_type is not expected:
                blockers.append("dependency_reference_type_conflict")
                _issue(
                    issues,
                    code="dependency_reference_type_conflict",
                    source_node_ids=[node.matek_id, referenced.matek_id],
                    detail=(
                        f"Reference prefix {reference[:3]} conflicts with archive node type "
                        f"{referenced.node_type.value!r}."
                    ),
                    referenced_ids=[reference],
                )
                continue
            if reference.startswith("CLM-"):
                if reference in scope_conflict_ids:
                    blockers.append("dependency_scope_conflict")
                    _issue(
                        issues,
                        code="dependency_scope_conflict",
                        source_node_ids=[node.matek_id, reference],
                        detail=(
                            "A dependency belongs to an exact-claim group with conflicting scopes."
                        ),
                        referenced_ids=[reference],
                    )
                    continue
                claims.append(claim_aliases.get(reference, reference))
            elif reference.startswith("PRF-"):
                proofs.append(reference)
            else:
                approaches.append(reference)
    return DependencyExtraction(
        source_node_id=node.matek_id,
        source_fields=fields,
        raw_entries=entries,
        claim_ids=sorted(set(claims)),
        proof_ids=sorted(set(proofs)),
        approach_ids=sorted(set(approaches)),
        unknown_ids=sorted(set(unknown)),
        review_blockers=sorted(set(blockers)),
    )


def _mechanism_only_refutation(node: GraphNode) -> bool:
    if "matek/branch-local" in node.tags:
        return True
    if node.metadata.get("matek_scientific_scope") == ScientificScope.BRANCH.value:
        return True
    if node.metadata.get("matek_disposition") == "refuted_mechanism":
        return True
    return any(pattern.search(node.body) is not None for pattern in _MECHANISM_ONLY)


def _nomination_score(
    claim: GraphNode,
    proof: GraphNode,
    *,
    premise_count: int,
) -> tuple[int, list[str]]:
    status_score = {
        EpistemicStatus.PROVED_INFORMALLY: 55,
        EpistemicStatus.CANDIDATE: 45,
        EpistemicStatus.CONJECTURED: 30,
        EpistemicStatus.OPEN: 20,
    }.get(claim.epistemic_status, 0)
    score = status_score
    reasons = [f"claim status is {claim.epistemic_status.value}"]
    if proof.workflow_status is WorkflowStatus.COMPLETE:
        score += 20
        reasons.append("archived proof attempt is marked complete")
    if premise_count:
        score += min(10, premise_count * 2)
        reasons.append(f"proposal records {premise_count} explicit premise(s)")
    if proof.source_artifacts:
        score += 8
        reasons.append("proof attempt retains source artifacts")
    if claim.evidence or proof.evidence:
        score += 7
        reasons.append("archive retains supporting evidence")
    return min(score, 100), reasons


def plan_legacy_graph_backfill(
    nodes: Sequence[GraphNode],
    *,
    graph_revision: str,
    problem_id: str,
    target_claim_id: str,
    audit_nomination_limit: int = 25,
    graph_name: str | None = None,
) -> LegacyMigrationReport:
    """Build a deterministic dry-run plan without editing any archive node.

    Ambiguous prose, missing references, disjunctions, and scope conflicts are reported
    for human or independent-agent review.  They are never guessed into derivations.
    """

    if audit_nomination_limit < 1:
        raise ValueError("audit_nomination_limit must be positive")
    normalized_problem_id = _stable_id(problem_id)
    normalized_target_id = _stable_id(target_claim_id)
    selected = sorted(
        (node for node in nodes if node.problem_id == normalized_problem_id),
        key=lambda item: item.matek_id,
    )
    if not selected:
        raise LegacyMigrationError(f"archive contains no nodes for {normalized_problem_id}")
    by_id = {node.matek_id: node for node in selected}
    if len(by_id) != len(selected):
        raise LegacyMigrationError("archive contains duplicate node IDs")
    target = by_id.get(normalized_target_id)
    if target is None or target.node_type is not NodeType.CLAIM:
        raise LegacyMigrationError("main target must identify an archived claim node")

    all_issues: list[MigrationIssue] = []
    claim_nodes = {
        node.matek_id: node
        for node in selected
        if node.node_type is NodeType.CLAIM and not node.tombstone
    }
    scopes: dict[str, ScientificScope] = {}
    statements: dict[str, str] = {}
    grouped_claims: dict[str, list[str]] = {}
    for claim in claim_nodes.values():
        statement = normalize_exact_statement(exact_statement(claim.body))
        if not statement:
            _issue(
                all_issues,
                code="missing_exact_statement",
                source_node_ids=[claim.matek_id],
                detail="Claim has no exact statement and was excluded from alias planning.",
            )
            continue
        statements[claim.matek_id] = statement
        scopes[claim.matek_id] = _claim_scope(
            claim,
            target_claim_id=normalized_target_id,
            issues=all_issues,
        )
        grouped_claims.setdefault(statement, []).append(claim.matek_id)

    alias_groups: list[ClaimAliasGroup] = []
    claim_aliases: dict[str, str] = {}
    scope_conflict_ids: set[str] = set()
    for statement, member_ids in sorted(grouped_claims.items(), key=lambda item: item[0]):
        member_ids = sorted(member_ids)
        if len(member_ids) < 2:
            claim_aliases[member_ids[0]] = member_ids[0]
            continue
        canonical = normalized_target_id if normalized_target_id in member_ids else member_ids[0]
        group_scopes = sorted({scopes[item] for item in member_ids}, key=lambda item: item.value)
        conflict = len(group_scopes) > 1
        if conflict:
            scope_conflict_ids.update(member_ids)
            _issue(
                all_issues,
                code="exact_claim_scope_conflict",
                source_node_ids=member_ids,
                detail=(
                    "Exact-normalized duplicate claims carry different scientific scopes; "
                    "the alias group requires explicit scope review."
                ),
            )
        else:
            for member_id in member_ids:
                claim_aliases[member_id] = canonical
        alias_groups.append(
            ClaimAliasGroup(
                canonical_candidate_id=canonical,
                alias_ids=[item for item in member_ids if item != canonical],
                exact_statement=statement,
                logical_version=logical_version(statement),
                scopes=group_scopes,
                disposition="scope_conflict" if conflict else "ready_for_review",
            )
        )
    for claim_id in statements:
        claim_aliases.setdefault(claim_id, claim_id)

    extractions: list[DependencyExtraction] = []
    extraction_by_id: dict[str, DependencyExtraction] = {}
    for node in selected:
        extraction = _extract_dependencies(
            node,
            by_id=by_id,
            claim_aliases=claim_aliases,
            scope_conflict_ids=scope_conflict_ids,
            issues=all_issues,
        )
        if extraction is not None:
            extractions.append(extraction)
            extraction_by_id[node.matek_id] = extraction

    reclassifications: list[ProofAttemptReclassification] = []
    proof_gap_status: dict[str, Literal["gapped", "gap_free", "unknown"]] = {}
    for node in selected:
        if node.node_type is not NodeType.PROOF or node.tombstone:
            continue
        gap_status, gap = _gap_declaration(node)
        proof_gap_status[node.matek_id] = gap_status
        if gap_status == "gapped":
            reclassifications.append(
                ProofAttemptReclassification(proof_node_id=node.matek_id, exact_gap=gap)
            )
        elif gap_status == "unknown":
            _issue(
                all_issues,
                code="missing_gap_declaration",
                source_node_ids=[node.matek_id],
                detail=(
                    "Proof has no explicit exact-gap declaration; completeness was not inferred."
                ),
            )

    proposals: list[ReviewDerivationProposal] = []
    for proof in selected:
        if (
            proof.node_type is not NodeType.PROOF
            or proof.tombstone
            or proof_gap_status.get(proof.matek_id) != "gap_free"
        ):
            continue
        extraction = extraction_by_id.get(proof.matek_id)
        if extraction is not None and extraction.review_blockers:
            continue
        conclusions = sorted(
            {edge.target_id for edge in proof.relations if edge.relation is RelationType.PROVES}
        )
        if len(conclusions) != 1:
            _issue(
                all_issues,
                code="ambiguous_derivation_conclusion",
                source_node_ids=[proof.matek_id],
                detail="A legacy proof requires exactly one claim-valued PROVES edge.",
                referenced_ids=conclusions,
            )
            continue
        raw_conclusion = conclusions[0]
        if raw_conclusion in scope_conflict_ids:
            _issue(
                all_issues,
                code="derivation_conclusion_scope_conflict",
                source_node_ids=[proof.matek_id, raw_conclusion],
                detail="The proof conclusion belongs to an unresolved exact-claim scope group.",
                referenced_ids=[raw_conclusion],
            )
            continue
        conclusion_id = claim_aliases.get(raw_conclusion, raw_conclusion)
        if conclusion_id not in statements:
            _issue(
                all_issues,
                code="unknown_derivation_conclusion",
                source_node_ids=[proof.matek_id],
                detail="The legacy PROVES edge does not resolve to a canonical claim.",
                referenced_ids=[raw_conclusion],
            )
            continue

        premises = set(extraction.claim_ids if extraction is not None else [])
        supporting = set(
            [
                *(extraction.proof_ids if extraction is not None else []),
                *(extraction.approach_ids if extraction is not None else []),
            ]
        )
        structured_invalid = False
        for edge in proof.relations:
            if edge.relation is not RelationType.DEPENDS_ON:
                continue
            dependency = by_id.get(edge.target_id)
            if dependency is None:
                structured_invalid = True
                _issue(
                    all_issues,
                    code="unknown_structured_dependency",
                    source_node_ids=[proof.matek_id],
                    detail="A structured dependency edge targets an absent archive node.",
                    referenced_ids=[edge.target_id],
                )
            elif dependency.node_type is NodeType.CLAIM:
                if dependency.matek_id in scope_conflict_ids:
                    structured_invalid = True
                    _issue(
                        all_issues,
                        code="structured_dependency_scope_conflict",
                        source_node_ids=[proof.matek_id, dependency.matek_id],
                        detail="A structured premise belongs to an unresolved scope group.",
                        referenced_ids=[dependency.matek_id],
                    )
                else:
                    premises.add(claim_aliases.get(dependency.matek_id, dependency.matek_id))
            elif dependency.node_type in {NodeType.PROOF, NodeType.APPROACH}:
                supporting.add(dependency.matek_id)
            else:
                structured_invalid = True
                _issue(
                    all_issues,
                    code="unsupported_structured_dependency",
                    source_node_ids=[proof.matek_id, dependency.matek_id],
                    detail=(
                        "Only claims are mathematical premises; proofs and approaches are evidence."
                    ),
                    referenced_ids=[dependency.matek_id],
                )
        if structured_invalid:
            continue
        if conclusion_id in premises:
            _issue(
                all_issues,
                code="self_dependent_derivation",
                source_node_ids=[proof.matek_id, conclusion_id],
                detail="A proposed derivation would depend on its own conclusion.",
                referenced_ids=[conclusion_id],
            )
            continue
        unknown_premises = sorted(item for item in premises if item not in statements)
        if unknown_premises:
            _issue(
                all_issues,
                code="unknown_derivation_premise",
                source_node_ids=[proof.matek_id],
                detail="One or more extracted premises lack an exact canonical claim.",
                referenced_ids=unknown_premises,
            )
            continue
        ordered_premises = sorted(premises)
        proposal_id = deterministic_ledger_id(
            "DRV", "legacy-backfill", normalized_problem_id, proof.matek_id
        )
        derivation = Derivation(
            derivation_id=proposal_id,
            conclusion_claim_id=conclusion_id,
            premise_claim_ids=ordered_premises,
            proof_attempt_id=proof.matek_id,
            exact_target_version=logical_version(statements[conclusion_id]),
            premise_versions={item: logical_version(statements[item]) for item in ordered_premises},
            status=DerivationStatus.PROPOSED,
        )
        proposals.append(
            ReviewDerivationProposal(
                proposal_id=proposal_id,
                derivation=derivation,
                supporting_archive_node_ids=sorted(supporting),
                source_dependency_fields=(
                    extraction.source_fields if extraction is not None else []
                ),
            )
        )

    quarantines: list[RefutationQuarantine] = []
    for node in selected:
        if node.tombstone or not _mechanism_only_refutation(node):
            continue
        if not any(
            edge.relation is RelationType.REFUTES and edge.target_id == normalized_target_id
            for edge in node.relations
        ):
            continue
        extraction = extraction_by_id.get(node.matek_id)
        branch_targets = {
            edge.target_id
            for edge in node.relations
            if edge.target_id in by_id
            and by_id[edge.target_id].node_type is NodeType.APPROACH
            and edge.target_id != normalized_target_id
        }
        if extraction is not None:
            branch_targets.update(extraction.approach_ids)
        ordered_targets = sorted(branch_targets)
        quarantines.append(
            RefutationQuarantine(
                refutation_node_id=node.matek_id,
                main_target_id=normalized_target_id,
                candidate_branch_target_ids=ordered_targets,
                reason=(
                    "The archive body limits the failure to a mechanism or branch, so the "
                    "direct main-target refutation requires independent counterexample audit."
                ),
            )
        )
        if len(ordered_targets) != 1:
            _issue(
                all_issues,
                code=(
                    "quarantine_branch_target_unresolved"
                    if not ordered_targets
                    else "quarantine_branch_target_ambiguous"
                ),
                source_node_ids=[node.matek_id],
                detail=(
                    "No unique approach target can replace the quarantined main-target edge; "
                    "the archive relation was left untouched."
                ),
                referenced_ids=ordered_targets,
            )

    proposal_by_proof = {item.derivation.proof_attempt_id: item for item in proposals}
    nominations: list[AuditNomination] = []
    for proof_id, proposal in proposal_by_proof.items():
        proof = by_id[proof_id]
        claim_id = proposal.derivation.conclusion_claim_id
        claim = claim_nodes[claim_id]
        if claim_id == normalized_target_id or scopes[claim_id] is ScientificScope.MAIN:
            continue
        if claim.epistemic_status in {
            EpistemicStatus.AUDIT_PASSED,
            EpistemicStatus.LEAN_VERIFIED,
            EpistemicStatus.REFUTED,
            EpistemicStatus.INCONSISTENT,
            EpistemicStatus.STALE,
        }:
            continue
        score, reasons = _nomination_score(
            claim,
            proof,
            premise_count=len(proposal.derivation.premise_claim_ids),
        )
        nominations.append(
            AuditNomination(
                claim_id=claim_id,
                proof_node_id=proof_id,
                exact_statement=statements[claim_id],
                logical_version=logical_version(statements[claim_id]),
                scope=scopes[claim_id],
                strength_score=score,
                reasons=reasons,
            )
        )
    nominations.sort(key=lambda item: (-item.strength_score, item.claim_id, item.proof_node_id))
    # Multiple proof attempts for one exact claim remain archived; nominate only the
    # strongest deterministic representative for a fresh blinded audit.
    unique_nominations: list[AuditNomination] = []
    nominated_claims: set[str] = set()
    for nomination in nominations:
        if nomination.claim_id in nominated_claims:
            continue
        nominated_claims.add(nomination.claim_id)
        unique_nominations.append(nomination)
        if len(unique_nominations) == audit_nomination_limit:
            break

    all_issues = _deduplicate_issues(all_issues)
    dependency_codes = {
        "malformed_dependency_array",
        "invalid_dependency_reference",
        "unresolved_free_text_dependency",
        "disjunctive_dependency",
        "unknown_dependency_reference",
        "dependency_reference_type_conflict",
        "dependency_scope_conflict",
        "unknown_structured_dependency",
        "unsupported_structured_dependency",
        "unknown_derivation_premise",
        "self_dependent_derivation",
    }
    scope_codes = {
        "unknown_claim_scope",
        "exact_claim_scope_conflict",
        "derivation_conclusion_scope_conflict",
        "structured_dependency_scope_conflict",
    }
    return LegacyMigrationReport(
        source_graph_name=graph_name,
        source_graph_revision=graph_revision,
        source_archive_sha256=legacy_archive_sha256(selected, problem_id=normalized_problem_id),
        source_node_count=len(selected),
        audit_nomination_limit=audit_nomination_limit,
        problem_id=normalized_problem_id,
        target_claim_id=normalized_target_id,
        proof_attempt_reclassifications=sorted(
            reclassifications, key=lambda item: item.proof_node_id
        ),
        dependency_extractions=sorted(extractions, key=lambda item: item.source_node_id),
        derivation_proposals=sorted(proposals, key=lambda item: item.proposal_id),
        refutation_quarantines=sorted(quarantines, key=lambda item: item.refutation_node_id),
        claim_alias_groups=sorted(
            alias_groups, key=lambda item: (item.logical_version, item.canonical_candidate_id)
        ),
        audit_nominations=unique_nominations,
        ambiguous_dependencies=[item for item in all_issues if item.code in dependency_codes],
        scope_conflicts=[item for item in all_issues if item.code in scope_codes],
        review_notes=[
            item
            for item in all_issues
            if item.code not in dependency_codes and item.code not in scope_codes
        ],
    )


def migration_report_sha256(report: LegacyMigrationReport) -> str:
    """Return the canonical digest stored beside a migration report."""

    payload = json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_legacy_migration_report(path: Path, report: LegacyMigrationReport) -> Path:
    """Persist a dry-run report with a digest over its complete typed payload."""

    payload = report.model_dump(mode="json")
    payload["integrity_sha256"] = migration_report_sha256(report)
    return atomic_write_json(path, payload, confinement_root=path.parent)


def load_legacy_migration_report(path: Path) -> LegacyMigrationReport:
    """Load a migration report and reject schema or integrity corruption."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LegacyMigrationError(f"cannot load legacy migration report {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise LegacyMigrationError("legacy migration report must contain one JSON object")
    expected = raw.pop("integrity_sha256", None)
    try:
        report = LegacyMigrationReport.model_validate(raw)
    except ValueError as exc:
        raise LegacyMigrationError(f"legacy migration report schema is invalid: {exc}") from exc
    if expected != migration_report_sha256(report):
        raise LegacyMigrationError("legacy migration report integrity digest does not match")
    return report


def migration_application_sha256(record: LegacyMigrationApplicationRecord) -> str:
    """Return the canonical digest for a committed migration application record."""

    payload = json.dumps(
        record.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_legacy_migration_application(
    path: Path,
    record: LegacyMigrationApplicationRecord,
    *,
    confinement_root: Path | None = None,
) -> Path:
    """Persist an integrity-protected record of a committed migration."""

    payload = record.model_dump(mode="json")
    payload["integrity_sha256"] = migration_application_sha256(record)
    return atomic_write_json(
        path,
        payload,
        confinement_root=confinement_root or path.parent,
    )


def load_legacy_migration_application(path: Path) -> LegacyMigrationApplicationRecord:
    """Load an application record and reject schema or integrity corruption."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LegacyMigrationError(
            f"cannot load legacy migration application record {path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise LegacyMigrationError(
            "legacy migration application record must contain one JSON object"
        )
    expected = raw.pop("integrity_sha256", None)
    try:
        record = LegacyMigrationApplicationRecord.model_validate(raw)
    except ValueError as exc:
        raise LegacyMigrationError(
            f"legacy migration application record schema is invalid: {exc}"
        ) from exc
    if expected != migration_application_sha256(record):
        raise LegacyMigrationError(
            "legacy migration application record integrity digest does not match"
        )
    return record


__all__ = [
    "AuditNomination",
    "ClaimAliasGroup",
    "DependencyExtraction",
    "LegacyMigrationApplicationRecord",
    "LegacyMigrationError",
    "LegacyMigrationReport",
    "MigrationIssue",
    "ProofAttemptReclassification",
    "RefutationQuarantine",
    "ReviewDerivationProposal",
    "legacy_archive_sha256",
    "load_legacy_migration_application",
    "load_legacy_migration_report",
    "migration_application_sha256",
    "migration_report_sha256",
    "plan_legacy_graph_backfill",
    "write_legacy_migration_application",
    "write_legacy_migration_report",
]
