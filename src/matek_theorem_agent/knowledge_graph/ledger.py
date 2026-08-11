"""Canonical derivation and obligation ledger for admitted mathematics.

The Markdown vault remains the durable research archive.  This module supplies the
small, rebuildable proof layer used to answer which exact claims are trusted, which
joint premises support them, and which obligations form the current proof frontier.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..graph_ids import validate_any_node_id
from ..scientific import ScientificScope, normalize_exact_statement
from ..workspace import atomic_write_json
from .markdown import exact_statement
from .models import EpistemicStatus, GraphNode, RelationType

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


class LedgerError(RuntimeError):
    """The canonical proof ledger is inconsistent or cannot be reconstructed."""


class _LedgerModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClaimStatus(StrEnum):
    OPEN = "open"
    PROPOSED = "proposed"
    AUDIT_PASSED = "audit_passed"
    LEAN_VERIFIED = "lean_verified"
    REFUTED = "refuted"
    STALE = "stale"


class DerivationStatus(StrEnum):
    PROPOSED = "proposed"
    AUDIT_PASSED = "audit_passed"
    AUDIT_FAILED = "audit_failed"
    STALE = "stale"


class ObligationStatus(StrEnum):
    OPEN = "open"
    PROPOSED = "proposed"
    RESOLVED = "resolved"
    REFUTED = "refuted"
    STALE = "stale"


def _stable_id(value: str) -> str:
    try:
        return validate_any_node_id(value)
    except ValueError as exc:
        raise ValueError(
            "ledger IDs must be legacy PREFIX- hash IDs or descriptive 'WORD: ...' IDs"
        ) from exc


def _sha256(value: str) -> str:
    normalized = value.strip().casefold()
    if not _SHA256.fullmatch(normalized):
        raise ValueError("logical versions must be lowercase SHA-256 digests")
    return normalized


def logical_version(exact_claim: str, *, notation_definition_version: str = "1") -> str:
    """Hash only exact mathematical content and its explicit notation version."""

    normalized = normalize_exact_statement(exact_claim)
    notation = notation_definition_version.strip()
    if not normalized or not notation:
        raise ValueError("logical versions require an exact statement and notation version")
    return hashlib.sha256(f"{notation}\0{normalized}".encode()).hexdigest()


def obligation_logical_version(
    exact_statement: str,
    *,
    conclusion: str,
    quantifiers: Sequence[str] = (),
    hypotheses: Sequence[str] = (),
    dependency_claim_ids: Sequence[str] = (),
    target_claim_ids: Sequence[str] = (),
    scope: ScientificScope = ScientificScope.BRANCH,
    notation_definition_version: str = "1",
    falsification_evidence: Sequence[str] = (),
) -> str:
    """Hash the complete semantic contract of a canonical obligation."""

    notation = normalize_exact_statement(notation_definition_version)
    payload = {
        "conclusion": normalize_exact_statement(conclusion),
        "dependency_claim_ids": list(dict.fromkeys(dependency_claim_ids)),
        "exact_statement": normalize_exact_statement(exact_statement),
        "falsification_evidence": [
            normalize_exact_statement(item) for item in falsification_evidence
        ],
        "hypotheses": [normalize_exact_statement(item) for item in hypotheses],
        "notation_definition_version": notation,
        "quantifiers": [normalize_exact_statement(item) for item in quantifiers],
        "scope": scope.value,
        "target_claim_ids": list(dict.fromkeys(target_claim_ids)),
    }
    if not payload["exact_statement"] or not payload["conclusion"] or not notation:
        raise ValueError("obligation logical versions require an exact statement and conclusion")
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def deterministic_ledger_id(prefix: str, *parts: str) -> str:
    normalized_prefix = prefix.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", normalized_prefix):
        raise ValueError("ledger ID prefixes must contain exactly three uppercase letters")
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest().upper()[:20]
    return f"{normalized_prefix}-{digest}"


class LedgerClaim(_LedgerModel):
    claim_id: str
    exact_statement: str
    logical_version: str
    scope: ScientificScope = ScientificScope.BRANCH
    status: ClaimStatus = ClaimStatus.OPEN
    source_node_id: str | None = None
    aliases: list[str] = Field(default_factory=list)

    @field_validator("claim_id", "source_node_id")
    @classmethod
    def ids_are_valid(cls, value: str | None) -> str | None:
        return None if value is None else _stable_id(value)

    @field_validator("exact_statement")
    @classmethod
    def statement_is_exact(cls, value: str) -> str:
        normalized = normalize_exact_statement(value)
        if not normalized:
            raise ValueError("ledger claims require a nonblank exact statement")
        return normalized

    @field_validator("logical_version")
    @classmethod
    def version_is_sha256(cls, value: str) -> str:
        return _sha256(value)

    @field_validator("aliases")
    @classmethod
    def aliases_are_stable_ids(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(_stable_id(value) for value in values))

    @model_validator(mode="after")
    def version_matches_statement(self) -> LedgerClaim:
        if self.logical_version != logical_version(self.exact_statement):
            raise ValueError("claim logical_version does not match its exact statement")
        return self


class Derivation(_LedgerModel):
    derivation_id: str
    conclusion_claim_id: str
    premise_claim_ids: list[str] = Field(default_factory=list)
    proof_attempt_id: str
    exact_target_version: str
    premise_versions: dict[str, str] = Field(default_factory=dict)
    obligation_ids: list[str] = Field(default_factory=list)
    status: DerivationStatus = DerivationStatus.PROPOSED
    audit_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)

    @field_validator(
        "derivation_id",
        "conclusion_claim_id",
        "proof_attempt_id",
    )
    @classmethod
    def required_ids_are_valid(cls, value: str) -> str:
        return _stable_id(value)

    @field_validator("premise_claim_ids", "obligation_ids", "audit_ids", "artifact_ids")
    @classmethod
    def id_lists_are_valid(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(_stable_id(value) for value in values))

    @field_validator("exact_target_version")
    @classmethod
    def target_version_is_sha256(cls, value: str) -> str:
        return _sha256(value)

    @field_validator("premise_versions")
    @classmethod
    def premise_versions_are_valid(cls, values: dict[str, str]) -> dict[str, str]:
        return {_stable_id(key): _sha256(value) for key, value in sorted(values.items())}

    @model_validator(mode="after")
    def premise_versions_are_complete(self) -> Derivation:
        if self.conclusion_claim_id in self.premise_claim_ids:
            raise ValueError("a derivation cannot directly depend on its own conclusion")
        if set(self.premise_versions) != set(self.premise_claim_ids):
            raise ValueError("premise_versions must exactly cover premise_claim_ids")
        return self


class Obligation(_LedgerModel):
    obligation_id: str
    exact_statement: str
    quantifiers: list[str] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    conclusion: str
    parent_derivation_ids: list[str] = Field(default_factory=list)
    dependency_claim_ids: list[str] = Field(default_factory=list)
    target_claim_ids: list[str] = Field(default_factory=list)
    scope: ScientificScope = ScientificScope.BRANCH
    notation_definition_version: str = "1"
    logical_version: str
    status: ObligationStatus = ObligationStatus.OPEN
    falsification_evidence: list[str] = Field(default_factory=list)
    estimated_leverage: int = Field(default=0, ge=0, le=100)

    @field_validator("obligation_id")
    @classmethod
    def obligation_id_is_valid(cls, value: str) -> str:
        return _stable_id(value)

    @field_validator("parent_derivation_ids", "dependency_claim_ids", "target_claim_ids")
    @classmethod
    def linked_ids_are_valid(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(_stable_id(value) for value in values))

    @field_validator("exact_statement", "conclusion", "notation_definition_version")
    @classmethod
    def required_text_is_nonblank(cls, value: str) -> str:
        normalized = normalize_exact_statement(value)
        if not normalized:
            raise ValueError("obligations require exact, nonblank mathematical text")
        return normalized

    @field_validator("quantifiers", "hypotheses", "falsification_evidence")
    @classmethod
    def text_lists_are_normalized(cls, values: list[str]) -> list[str]:
        normalized = [normalize_exact_statement(value) for value in values]
        return list(dict.fromkeys(value for value in normalized if value))

    @field_validator("logical_version")
    @classmethod
    def version_is_sha256(cls, value: str) -> str:
        return _sha256(value)

    @model_validator(mode="after")
    def version_matches_statement(self) -> Obligation:
        expected = obligation_logical_version(
            self.exact_statement,
            conclusion=self.conclusion,
            quantifiers=self.quantifiers,
            hypotheses=self.hypotheses,
            dependency_claim_ids=self.dependency_claim_ids,
            target_claim_ids=self.target_claim_ids,
            scope=self.scope,
            notation_definition_version=self.notation_definition_version,
            falsification_evidence=self.falsification_evidence,
        )
        if self.logical_version != expected:
            raise ValueError("obligation logical_version does not match its semantic contract")
        return self


class LedgerAmbiguity(_LedgerModel):
    source_node_id: str
    code: str
    detail: str

    @field_validator("source_node_id")
    @classmethod
    def source_id_is_valid(cls, value: str) -> str:
        return _stable_id(value)

    @field_validator("code", "detail")
    @classmethod
    def text_is_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("ledger ambiguity fields must not be blank")
        return normalized


class CanonicalLedger(_LedgerModel):
    schema_version: Literal[1] = 1
    graph_revision: str
    problem_id: str
    target_claim_id: str
    claims: dict[str, LedgerClaim] = Field(default_factory=dict)
    derivations: dict[str, Derivation] = Field(default_factory=dict)
    obligations: dict[str, Obligation] = Field(default_factory=dict)
    ambiguities: list[LedgerAmbiguity] = Field(default_factory=list)

    @field_validator("graph_revision")
    @classmethod
    def revision_is_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("ledger graph_revision must not be blank")
        return normalized

    @field_validator("problem_id", "target_claim_id")
    @classmethod
    def stable_ids_are_valid(cls, value: str) -> str:
        return _stable_id(value)

    @model_validator(mode="after")
    def keyed_records_are_consistent(self) -> CanonicalLedger:
        for key, claim in self.claims.items():
            if _stable_id(key) != claim.claim_id:
                raise ValueError("claim dictionary keys must equal claim_id")
        for key, derivation in self.derivations.items():
            if _stable_id(key) != derivation.derivation_id:
                raise ValueError("derivation dictionary keys must equal derivation_id")
        for key, obligation in self.obligations.items():
            if _stable_id(key) != obligation.obligation_id:
                raise ValueError("obligation dictionary keys must equal obligation_id")
        if self.target_claim_id not in self.claims:
            raise ValueError("target_claim_id must identify a canonical ledger claim")
        return self


class OpenCut(_LedgerModel):
    target_claim_id: str
    obligation_ids: list[str]
    alternative_cuts: list[list[str]]
    search_capped: bool = False

    @field_validator("target_claim_id")
    @classmethod
    def target_is_valid(cls, value: str) -> str:
        return _stable_id(value)


def _current_derivation_status(
    derivation: Derivation,
    claims: Mapping[str, LedgerClaim],
) -> DerivationStatus:
    conclusion = claims.get(derivation.conclusion_claim_id)
    if conclusion is None or conclusion.logical_version != derivation.exact_target_version:
        return DerivationStatus.STALE
    for premise_id, expected in derivation.premise_versions.items():
        premise = claims.get(premise_id)
        if premise is None or premise.logical_version != expected:
            return DerivationStatus.STALE
    return derivation.status


def validate_ledger(ledger: CanonicalLedger) -> None:
    """Validate references and reject circular mathematical support."""

    claims = ledger.claims
    for derivation in ledger.derivations.values():
        missing_claims = [
            claim_id
            for claim_id in [derivation.conclusion_claim_id, *derivation.premise_claim_ids]
            if claim_id not in claims
        ]
        if missing_claims:
            raise LedgerError(
                f"derivation {derivation.derivation_id} references unknown claim(s): "
                + ", ".join(missing_claims)
            )
        asymmetric_obligations = [
            item
            for item in derivation.obligation_ids
            if item not in ledger.obligations
            or derivation.derivation_id not in ledger.obligations[item].parent_derivation_ids
        ]
        if asymmetric_obligations:
            raise LedgerError(
                f"derivation {derivation.derivation_id} has non-reciprocal obligation link(s): "
                + ", ".join(asymmetric_obligations)
            )
    for obligation in ledger.obligations.values():
        asymmetric_parents = [
            item
            for item in obligation.parent_derivation_ids
            if item not in ledger.derivations
            or obligation.obligation_id not in ledger.derivations[item].obligation_ids
        ]
        if asymmetric_parents:
            raise LedgerError(
                f"obligation {obligation.obligation_id} has non-reciprocal parent link(s): "
                + ", ".join(asymmetric_parents)
            )

    adjacency: dict[str, set[str]] = {claim_id: set() for claim_id in claims}
    for derivation in ledger.derivations.values():
        adjacency[derivation.conclusion_claim_id].update(derivation.premise_claim_ids)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(claim_id: str) -> None:
        if claim_id in visiting:
            raise LedgerError(f"cyclic derivation support detected at {claim_id}")
        if claim_id in visited:
            return
        visiting.add(claim_id)
        for premise_id in sorted(adjacency[claim_id]):
            visit(premise_id)
        visiting.remove(claim_id)
        visited.add(claim_id)

    for claim_id in sorted(claims):
        visit(claim_id)


def trusted_claim_ids(ledger: CanonicalLedger) -> set[str]:
    """Compute trusted claims using audited base claims and AND/OR derivation closure.

    A claim explicitly marked ``audit_passed`` or ``lean_verified`` is an intentional base
    fact even when no derivation is stored. Candidate/open claims acquire trust only through a
    current audited derivation whose premises and obligations are all trusted or resolved.
    """

    validate_ledger(ledger)
    # Explicit claim-level audits (including imported facts and deterministic Lean results) are
    # base cases.  Keeping this separate from derivation closure is intentional: a derived
    # candidate remains untrusted until one of its exact routes passes audit.
    trusted = {
        claim.claim_id
        for claim in ledger.claims.values()
        if claim.status in {ClaimStatus.AUDIT_PASSED, ClaimStatus.LEAN_VERIFIED}
    }
    changed = True
    while changed:
        changed = False
        for derivation in ledger.derivations.values():
            if _current_derivation_status(derivation, ledger.claims) not in {
                DerivationStatus.AUDIT_PASSED
            }:
                continue
            if derivation.conclusion_claim_id in trusted:
                continue
            if not set(derivation.premise_claim_ids).issubset(trusted):
                continue
            if any(
                ledger.obligations[item].status is not ObligationStatus.RESOLVED
                for item in derivation.obligation_ids
            ):
                continue
            trusted.add(derivation.conclusion_claim_id)
            changed = True
    return trusted


def refresh_derivation_staleness(ledger: CanonicalLedger) -> CanonicalLedger:
    """Return a copy with logical-version mismatches marked stale.

    Staleness belongs to one derivation.  An independent alternative derivation is not
    changed, so a conclusion remains trusted whenever another current audited route exists.
    """

    refreshed = {
        derivation_id: derivation.model_copy(
            update={"status": _current_derivation_status(derivation, ledger.claims)}
        )
        for derivation_id, derivation in ledger.derivations.items()
    }
    return ledger.model_copy(update={"derivations": refreshed})


def _antichain(
    candidates: Iterable[frozenset[str]],
    *,
    maximum_candidates: int,
) -> tuple[list[frozenset[str]], bool]:
    unique = sorted(set(candidates), key=lambda item: (len(item), tuple(sorted(item))))
    minimal: list[frozenset[str]] = []
    capped = False
    for candidate in unique:
        if any(existing.issubset(candidate) for existing in minimal):
            continue
        minimal = [existing for existing in minimal if not candidate.issubset(existing)]
        minimal.append(candidate)
        if len(minimal) > maximum_candidates:
            minimal = minimal[:maximum_candidates]
            capped = True
    return minimal, capped


def smallest_known_open_cut(
    ledger: CanonicalLedger,
    *,
    target_claim_id: str | None = None,
    maximum_candidates: int = 512,
) -> OpenCut:
    """Compute a bounded minimal antichain for the selected target.

    Each derivation is one OR alternative.  Its premises and declared obligations are an
    AND-set.  If no known derivation reaches a claim, that exact claim is itself the open
    cut.  The result is labelled ``search_capped`` instead of claiming false optimality
    when the configured antichain bound is reached.
    """

    if maximum_candidates < 1:
        raise ValueError("maximum_candidates must be positive")
    validate_ledger(ledger)
    target = _stable_id(target_claim_id or ledger.target_claim_id)
    if target not in ledger.claims:
        raise LedgerError(f"open-cut target is not a canonical claim: {target}")
    trusted = trusted_claim_ids(ledger)
    by_conclusion: dict[str, list[Derivation]] = {}
    for derivation in ledger.derivations.values():
        status = _current_derivation_status(derivation, ledger.claims)
        if status in {DerivationStatus.AUDIT_FAILED, DerivationStatus.STALE}:
            continue
        by_conclusion.setdefault(derivation.conclusion_claim_id, []).append(derivation)
    memo: dict[str, tuple[list[frozenset[str]], bool]] = {}

    def cuts_for(claim_id: str) -> tuple[list[frozenset[str]], bool]:
        if claim_id in trusted:
            return [frozenset()], False
        if claim_id in memo:
            return memo[claim_id]
        alternatives = sorted(by_conclusion.get(claim_id, []), key=lambda item: item.derivation_id)
        if not alternatives:
            direct_obligations = sorted(
                obligation.obligation_id
                for obligation in ledger.obligations.values()
                if claim_id in obligation.target_claim_ids
                and obligation.status is not ObligationStatus.RESOLVED
            )
            result = (
                [frozenset(direct_obligations)] if direct_obligations else [frozenset({claim_id})],
                False,
            )
            memo[claim_id] = result
            return result
        candidate_cuts: list[frozenset[str]] = []
        any_capped = False
        for derivation in alternatives:
            derivation_status = _current_derivation_status(derivation, ledger.claims)
            combinations: list[frozenset[str]] = [frozenset()]
            for premise_id in derivation.premise_claim_ids:
                premise_cuts, premise_capped = cuts_for(premise_id)
                any_capped = any_capped or premise_capped
                combinations = [
                    existing | addition for existing in combinations for addition in premise_cuts
                ]
                combinations, capped = _antichain(
                    combinations,
                    maximum_candidates=maximum_candidates,
                )
                any_capped = any_capped or capped
            open_obligations = frozenset(
                obligation_id
                for obligation_id in derivation.obligation_ids
                if ledger.obligations[obligation_id].status is not ObligationStatus.RESOLVED
            )
            audit_requirement = (
                frozenset({derivation.derivation_id})
                if derivation_status is DerivationStatus.PROPOSED
                else frozenset()
            )
            candidate_cuts.extend(
                item | open_obligations | audit_requirement for item in combinations
            )
        reduced, capped = _antichain(
            candidate_cuts,
            maximum_candidates=maximum_candidates,
        )
        result = (reduced or [frozenset({claim_id})], any_capped or capped)
        memo[claim_id] = result
        return result

    alternatives, capped = cuts_for(target)
    ordered = [sorted(item) for item in alternatives]
    ordered.sort(key=lambda item: (len(item), item))
    smallest = ordered[0] if ordered else []
    return OpenCut(
        target_claim_id=target,
        obligation_ids=smallest,
        alternative_cuts=ordered,
        search_capped=capped,
    )


def _claim_status(status: EpistemicStatus) -> ClaimStatus:
    return {
        EpistemicStatus.AUDIT_PASSED: ClaimStatus.AUDIT_PASSED,
        EpistemicStatus.LEAN_VERIFIED: ClaimStatus.LEAN_VERIFIED,
        EpistemicStatus.REFUTED: ClaimStatus.REFUTED,
        EpistemicStatus.STALE: ClaimStatus.STALE,
        EpistemicStatus.CANDIDATE: ClaimStatus.PROPOSED,
        EpistemicStatus.PROVED_INFORMALLY: ClaimStatus.PROPOSED,
    }.get(status, ClaimStatus.OPEN)


def _admitted_definition_scope(node: GraphNode, statement: str) -> ScientificScope | None:
    """Return the scope of a current, application-admitted definition or ``None``.

    Definitions are declarations rather than self-proving theorems.  The ledger may use one
    as a versioned base premise only when its deterministic identity and immutable admission
    provenance agree with the exact statement currently stored in the graph.
    """

    del statement
    # Imported lazily to avoid the admission module's top-level logical-version dependency.
    from .admission import canonical_admitted_definition_scope

    return canonical_admitted_definition_scope(node)


def _claim_assumption_contract_issue(node: GraphNode) -> tuple[str, str] | None:
    """Return a fail-closed archive reason for a claim with unbound assumptions."""

    metadata_key = "matek_normalized_assumptions"
    raw_assumptions = node.metadata.get(metadata_key)
    if metadata_key in node.metadata:
        if not isinstance(raw_assumptions, list) or not all(
            isinstance(item, str) for item in raw_assumptions
        ):
            return (
                "malformed_claim_assumption_contract",
                "Claim assumption metadata is malformed; the claim remains stale archive context.",
            )
        if any(normalize_exact_statement(item) for item in raw_assumptions):
            return (
                "unbound_claim_assumptions",
                "A claim with nonempty unbound assumptions cannot enter the trusted ledger.",
            )
    elif node.author_role == "matek-scientific-admission":
        return (
            "missing_claim_assumption_contract",
            "An application-admitted claim lacks its explicit assumption contract.",
        )

    section = re.search(
        r"(?ms)^## Assumptions\s*\n(?P<body>.*?)(?=^##\s|\Z)",
        node.body,
    )
    if section is not None:
        content = section.group("body").strip()
        if content and normalize_exact_statement(content).casefold() not in {
            "none",
            "none.",
            "_none_",
            "_none._",
        }:
            return (
                "unbound_claim_assumptions",
                "The managed Assumptions section is nonempty; the claim remains stale archive "
                "context.",
            )
    return None


def _scientific_derivation_archive_issue(node: GraphNode) -> str | None:
    """Reject legacy/bypassed admission derivations lacking a complete bare contract."""

    if node.author_role != "matek-scientific-admission":
        return None
    raw_assumptions = node.metadata.get("matek_normalized_assumptions")
    if not isinstance(raw_assumptions, list) or any(
        not isinstance(item, str) or normalize_exact_statement(item) for item in raw_assumptions
    ):
        return "An application-admitted derivation has missing, malformed, or unbound assumptions."
    if node.metadata.get("matek_scientific_disposition") != "proposed_complete":
        return "A partial scientific result is archive evidence, not a canonical derivation."
    return None


def project_markdown_ledger(
    nodes: Sequence[GraphNode],
    *,
    graph_revision: str,
    problem_id: str,
    target_claim_id: str,
) -> CanonicalLedger:
    """Build the canonical proof projection from structured graph notes.

    Legacy prose is never guessed into proof support.  Ambiguous or gapped proof notes are
    recorded for review, while new structured proof notes use real ``proves`` and
    ``depends_on`` edges plus exact logical versions.
    """

    claims: dict[str, LedgerClaim] = {}
    derivations: dict[str, Derivation] = {}
    obligations: dict[str, Obligation] = {}
    ambiguities: list[LedgerAmbiguity] = []
    screened_obligation_ids: set[str] = set()
    selected = [node for node in nodes if node.problem_id == problem_id and not node.tombstone]
    selected_by_id = {node.matek_id: node for node in selected}
    audit_ids_by_target: dict[str, list[str]] = {}
    for audit in selected:
        if str(audit.node_type.value) != "audit":
            continue
        for edge in audit.relations:
            if edge.relation is RelationType.AUDITS:
                audit_ids_by_target.setdefault(edge.target_id, []).append(audit.matek_id)

    claim_groups: dict[tuple[ScientificScope, str], list[tuple[GraphNode, str]]] = {}
    archive_claims: list[tuple[GraphNode, str, ScientificScope, str]] = []
    for node in selected:
        node_type_value = str(node.node_type.value)
        if node_type_value != "claim":
            continue
        statement = exact_statement(node.body)
        if not statement:
            ambiguities.append(
                LedgerAmbiguity(
                    source_node_id=node.matek_id,
                    code="missing_exact_statement",
                    detail="Claim note has no exact-statement section.",
                )
            )
            continue
        if node.matek_id == target_claim_id or "matek/main-target" in node.tags:
            scope = ScientificScope.MAIN
        else:
            raw_scope = node.metadata.get("matek_scientific_scope")
            try:
                scope = ScientificScope(str(raw_scope)) if raw_scope else ScientificScope.BRANCH
            except ValueError:
                scope = ScientificScope.BRANCH
                ambiguities.append(
                    LedgerAmbiguity(
                        source_node_id=node.matek_id,
                        code="unknown_claim_scope",
                        detail=f"Unknown scientific scope {raw_scope!r}; retained as branch.",
                    )
                )
        version = logical_version(statement)
        assumption_issue = _claim_assumption_contract_issue(node)
        if assumption_issue is not None:
            code, detail = assumption_issue
            ambiguities.append(
                LedgerAmbiguity(
                    source_node_id=node.matek_id,
                    code=code,
                    detail=detail,
                )
            )
            archive_claims.append((node, statement, scope, version))
            continue
        claim_groups.setdefault((scope, version), []).append((node, statement))

    claim_aliases: dict[str, str] = {}
    status_rank = {
        ClaimStatus.REFUTED: 0,
        ClaimStatus.STALE: 1,
        ClaimStatus.OPEN: 2,
        ClaimStatus.PROPOSED: 3,
        ClaimStatus.AUDIT_PASSED: 4,
        ClaimStatus.LEAN_VERIFIED: 5,
    }
    for (scope, version), members in sorted(
        claim_groups.items(), key=lambda item: (item[0][0].value, item[0][1])
    ):
        member_ids = {node.matek_id for node, _ in members}
        canonical_id = target_claim_id if target_claim_id in member_ids else min(member_ids)
        canonical_node, statement = next(
            (node, text) for node, text in members if node.matek_id == canonical_id
        )
        statuses = [_claim_status(node.epistemic_status) for node, _ in members]
        if any(status is ClaimStatus.REFUTED for status in statuses) and any(
            status in {ClaimStatus.AUDIT_PASSED, ClaimStatus.LEAN_VERIFIED} for status in statuses
        ):
            ambiguities.append(
                LedgerAmbiguity(
                    source_node_id=canonical_id,
                    code="conflicting_exact_claim_status",
                    detail=(
                        "Exact-statement aliases contain both trusted and refuted statuses; "
                        "fresh audit is required."
                    ),
                )
            )
            status = ClaimStatus.STALE
        else:
            status = max(statuses, key=status_rank.__getitem__)
        aliases = sorted(member_ids - {canonical_id})
        claims[canonical_id] = LedgerClaim(
            claim_id=canonical_id,
            exact_statement=statement,
            logical_version=version,
            scope=scope,
            status=status,
            source_node_id=canonical_node.matek_id,
            aliases=aliases,
        )
        for member_id in member_ids:
            claim_aliases[member_id] = canonical_id

    for node, statement, scope, version in archive_claims:
        claims[node.matek_id] = LedgerClaim(
            claim_id=node.matek_id,
            exact_statement=statement,
            logical_version=version,
            scope=scope,
            status=ClaimStatus.STALE,
            source_node_id=node.matek_id,
        )
        claim_aliases[node.matek_id] = node.matek_id

    for node in selected:
        if str(node.node_type.value) != "definition":
            continue
        statement = exact_statement(node.body)
        if not statement:
            ambiguities.append(
                LedgerAmbiguity(
                    source_node_id=node.matek_id,
                    code="missing_definition_exact_statement",
                    detail="Definition note has no exact-statement section.",
                )
            )
            continue
        definition_scope = _admitted_definition_scope(node, statement)
        if definition_scope is None:
            ambiguities.append(
                LedgerAmbiguity(
                    source_node_id=node.matek_id,
                    code="unadmitted_definition",
                    detail=(
                        "A definition without exact application-owned admission identity "
                        "remains archive context and cannot serve as a canonical premise."
                    ),
                )
            )
            continue
        invalid = node.epistemic_status in {
            EpistemicStatus.STALE,
            EpistemicStatus.INCONSISTENT,
            EpistemicStatus.REFUTED,
        } or bool(node.invalidation_reasons)
        version = logical_version(statement)
        claims[node.matek_id] = LedgerClaim(
            claim_id=node.matek_id,
            exact_statement=statement,
            logical_version=version,
            scope=definition_scope,
            status=ClaimStatus.STALE if invalid else ClaimStatus.AUDIT_PASSED,
            source_node_id=node.matek_id,
        )
        claim_aliases[node.matek_id] = node.matek_id

    def derivation_id_for(node: GraphNode) -> str:
        """Return the canonical ledger ID for one derivation candidate node."""

        return (
            node.matek_id
            if str(node.node_type.value) == "derivation"
            else deterministic_ledger_id("DRV", problem_id, node.matek_id)
        )

    def derivation_projection_issue(node: GraphNode | None) -> tuple[str, str] | None:
        """Return the ambiguity code/detail for one derivation candidate, if any.

        This predicate decides derivation-ledger membership in exactly one place.
        Obligation parent screening above uses the same rules so an obligation
        referencing a demoted derivation is itself demoted instead of tripping a
        late validation error hours into a run.
        """

        if node is None:
            return (
                "unknown_parent_derivation",
                "The parent derivation node does not exist in this problem archive.",
            )
        node_type_value = str(node.node_type.value)
        if node_type_value not in {"proof", "derivation"}:
            return (
                "unknown_parent_derivation",
                "The parent derivation reference does not identify a proof or derivation.",
            )
        if node_type_value == "proof" and node.metadata.get("matek_archive_only") is True:
            return (
                "archive_only_legacy_proof",
                "A reviewed migration retains the incompatible legacy proof note as "
                "evidence; its canonical route lives in the proof attempt/derivation pair.",
            )
        if node_type_value == "proof" and node.epistemic_status not in {
            EpistemicStatus.AUDIT_PASSED,
            EpistemicStatus.LEAN_VERIFIED,
        }:
            return (
                "unadmitted_archive_proof",
                "A proof note without independent acceptance remains in the research "
                "archive and was not inferred into the canonical ledger.",
            )
        archive_issue = _scientific_derivation_archive_issue(node)
        if archive_issue is not None:
            return ("archive_only_scientific_derivation", archive_issue)
        raw_gap = node.metadata.get("matek_exact_gap")
        if isinstance(raw_gap, str) and raw_gap.strip():
            return (
                "gapped_proof_attempt",
                "Gapped proof remains in the archive and is not a derivation.",
            )
        conclusions = [
            edge.target_id for edge in node.relations if edge.relation is RelationType.PROVES
        ]
        premises = [
            edge.target_id for edge in node.relations if edge.relation is RelationType.DEPENDS_ON
        ]
        if len(conclusions) != 1:
            return (
                "ambiguous_derivation_conclusion",
                "A derivation requires exactly one structured proves edge.",
            )
        conclusion_id = claim_aliases.get(conclusions[0], conclusions[0])
        canonical_premises = list(dict.fromkeys(claim_aliases.get(item, item) for item in premises))
        if conclusion_id not in claims or any(item not in claims for item in canonical_premises):
            return (
                "unknown_derivation_claim",
                "A structured derivation references an unknown canonical claim.",
            )
        raw_proof_attempt_id = node.metadata.get("matek_proof_attempt_id")
        if node_type_value == "derivation" and (
            not isinstance(raw_proof_attempt_id, str) or not raw_proof_attempt_id.strip()
        ):
            return (
                "missing_proof_attempt_id",
                "A structured derivation has no canonical proof-attempt identity.",
            )
        if node_type_value == "derivation" and isinstance(raw_proof_attempt_id, str):
            proof_attempt = selected_by_id.get(raw_proof_attempt_id.strip())
            if proof_attempt is None or str(proof_attempt.node_type.value) != "proof_attempt":
                return (
                    "invalid_proof_attempt_link",
                    "A structured derivation's matek_proof_attempt_id does not identify a "
                    "canonical proof-attempt node.",
                )
        raw_premise_versions = node.metadata.get("matek_premise_versions")
        if node_type_value == "derivation" and isinstance(raw_premise_versions, list):
            parsed_ids: set[str] = set()
            malformed_versions = False
            for raw_version in raw_premise_versions:
                raw_id, separator, raw_digest = raw_version.partition("=")
                premise_id = claim_aliases.get(raw_id.strip(), raw_id.strip())
                digest = raw_digest.strip().casefold()
                if not separator or not premise_id or not _SHA256.fullmatch(digest):
                    malformed_versions = True
                    break
                parsed_ids.add(premise_id)
            if malformed_versions or parsed_ids != set(canonical_premises):
                return (
                    "malformed_premise_versions",
                    "A structured derivation's exact premise versions do not cover its "
                    "canonical premises.",
                )
        return None

    # Pass 1: decide derivation membership before obligations are admitted, so
    # obligation parent screening below can use the definitive eligibility set.
    derivation_eligibility: dict[str, tuple[str, str] | None] = {}
    for node in selected:
        if str(node.node_type.value) in {"proof", "derivation"}:
            derivation_eligibility[node.matek_id] = derivation_projection_issue(node)

    for node in selected:
        node_type_value = str(node.node_type.value)
        if node_type_value == "obligation":
            statement = exact_statement(node.body)
            conclusion = str(node.metadata.get("matek_conclusion") or statement).strip()
            parents = node.metadata.get("matek_parent_derivation_ids", [])
            dependencies = node.metadata.get("matek_dependency_claim_ids", [])
            targets = node.metadata.get("matek_target_claim_ids", [])
            quantifiers = node.metadata.get("matek_quantifiers", [])
            hypotheses = node.metadata.get("matek_hypotheses", [])
            if (
                not isinstance(parents, list)
                or not isinstance(dependencies, list)
                or not isinstance(targets, list)
                or not isinstance(quantifiers, list)
                or not isinstance(hypotheses, list)
            ):
                ambiguities.append(
                    LedgerAmbiguity(
                        source_node_id=node.matek_id,
                        code="malformed_obligation_links",
                        detail="Obligation link metadata is not a list of stable IDs.",
                    )
                )
                continue
            try:
                obligation_scope = ScientificScope(
                    str(node.metadata.get("matek_scope") or ScientificScope.BRANCH.value)
                )
            except ValueError:
                ambiguities.append(
                    LedgerAmbiguity(
                        source_node_id=node.matek_id,
                        code="malformed_obligation_scope",
                        detail="Obligation scope is not a recognized scientific scope.",
                    )
                )
                continue
            # Reference integrity is screened here, not in validate_ledger: an
            # obligation that references a demoted or unknown node is itself
            # ambiguous archive evidence, never a reason to halt the projection.
            parent_ids = [str(item) for item in parents]
            canonical_dependencies = [
                claim_aliases.get(str(item), str(item)) for item in dependencies
            ]
            canonical_targets = [claim_aliases.get(str(item), str(item)) for item in targets]
            unresolved_links = sorted(
                {
                    *(
                        parent_id
                        for parent_id in parent_ids
                        if derivation_eligibility.get(
                            parent_id,
                            (
                                "unknown_parent_derivation",
                                "The parent derivation node does not exist in this problem "
                                "archive.",
                            ),
                        )
                        is not None
                    ),
                    *(
                        claim_id
                        for claim_id in [*canonical_dependencies, *canonical_targets]
                        if claim_id not in claims
                    ),
                }
            )
            if unresolved_links:
                ambiguities.append(
                    LedgerAmbiguity(
                        source_node_id=node.matek_id,
                        code="unresolved_obligation_links",
                        detail=(
                            "Obligation links reference demoted or unknown canonical "
                            "records: " + ", ".join(unresolved_links)
                        ),
                    )
                )
                screened_obligation_ids.add(node.matek_id)
                continue
            notation = str(node.metadata.get("matek_notation_definition_version") or "1")
            raw_leverage = node.metadata.get("matek_estimated_leverage")
            leverage = raw_leverage if isinstance(raw_leverage, int) else 0
            obligations[node.matek_id] = Obligation(
                obligation_id=node.matek_id,
                exact_statement=statement,
                conclusion=conclusion,
                quantifiers=[str(item) for item in quantifiers],
                hypotheses=[str(item) for item in hypotheses],
                parent_derivation_ids=parent_ids,
                dependency_claim_ids=canonical_dependencies,
                target_claim_ids=canonical_targets,
                scope=obligation_scope,
                notation_definition_version=notation,
                logical_version=obligation_logical_version(
                    statement,
                    conclusion=conclusion,
                    quantifiers=[str(item) for item in quantifiers],
                    hypotheses=[str(item) for item in hypotheses],
                    dependency_claim_ids=canonical_dependencies,
                    target_claim_ids=canonical_targets,
                    scope=obligation_scope,
                    notation_definition_version=notation,
                    falsification_evidence=node.evidence,
                ),
                status=(
                    ObligationStatus.RESOLVED
                    if node.epistemic_status
                    in {EpistemicStatus.AUDIT_PASSED, EpistemicStatus.LEAN_VERIFIED}
                    else ObligationStatus.STALE
                    if node.epistemic_status is EpistemicStatus.STALE
                    else ObligationStatus.REFUTED
                    if node.epistemic_status is EpistemicStatus.REFUTED
                    else ObligationStatus.OPEN
                ),
                falsification_evidence=node.evidence,
                estimated_leverage=leverage,
            )

    for node in selected:
        node_type_value = str(node.node_type.value)
        if node_type_value not in {"proof", "derivation"}:
            continue
        issue = derivation_projection_issue(node)
        if issue is not None:
            code, detail = issue
            # An archive-only legacy proof note is retained verbatim; it is not
            # itself an ambiguity, only its mathematical route matters.
            if code != "archive_only_legacy_proof":
                ambiguities.append(
                    LedgerAmbiguity(
                        source_node_id=node.matek_id,
                        code=code,
                        detail=detail,
                    )
                )
            continue
        conclusions = [
            edge.target_id for edge in node.relations if edge.relation is RelationType.PROVES
        ]
        premises = [
            edge.target_id for edge in node.relations if edge.relation is RelationType.DEPENDS_ON
        ]
        conclusion_id = claim_aliases.get(conclusions[0], conclusions[0])
        canonical_premises = list(dict.fromkeys(claim_aliases.get(item, item) for item in premises))
        raw_obligations = node.metadata.get("matek_obligation_ids", [])
        obligation_ids = (
            [str(item) for item in raw_obligations] if isinstance(raw_obligations, list) else []
        )
        asymmetric_links = sorted(
            obligation_id
            for obligation_id in obligation_ids
            if obligation_id not in obligations
            or derivation_id_for(node) not in obligations[obligation_id].parent_derivation_ids
        )
        if asymmetric_links:
            ambiguities.append(
                LedgerAmbiguity(
                    source_node_id=node.matek_id,
                    code="non_reciprocal_obligation_link",
                    detail=(
                        "A derivation names obligations that were screened out or that do "
                        "not name it as their parent: " + ", ".join(asymmetric_links)
                    ),
                )
            )
            continue
        references_screened = sorted(
            obligation_id
            for obligation_id in obligation_ids
            if obligation_id in screened_obligation_ids
        )
        if references_screened:
            ambiguities.append(
                LedgerAmbiguity(
                    source_node_id=node.matek_id,
                    code="screened_obligation_link",
                    detail=(
                        "A derivation references obligations whose own links are ambiguous; "
                        "the derivation stays in the archive with them: "
                        + ", ".join(references_screened)
                    ),
                )
            )
            continue
        derivation_id = derivation_id_for(node)
        raw_proof_attempt_id = node.metadata.get("matek_proof_attempt_id")
        proof_attempt_id = (
            raw_proof_attempt_id.strip()
            if node_type_value == "derivation" and isinstance(raw_proof_attempt_id, str)
            else node.matek_id
        )
        target_version = claims[conclusion_id].logical_version
        raw_target_version = node.metadata.get("matek_exact_target_version")
        if node_type_value == "derivation" and isinstance(raw_target_version, str):
            target_version = raw_target_version.strip().casefold()
        premise_versions = {item: claims[item].logical_version for item in canonical_premises}
        raw_premise_versions = node.metadata.get("matek_premise_versions")
        if node_type_value == "derivation" and isinstance(raw_premise_versions, list):
            parsed_versions: dict[str, str] = {}
            for raw_version in raw_premise_versions:
                raw_id, _, raw_digest = raw_version.partition("=")
                premise_id = claim_aliases.get(raw_id.strip(), raw_id.strip())
                parsed_versions[premise_id] = raw_digest.strip().casefold()
            premise_versions = parsed_versions
        derivations[derivation_id] = Derivation(
            derivation_id=derivation_id,
            conclusion_claim_id=conclusion_id,
            premise_claim_ids=canonical_premises,
            proof_attempt_id=proof_attempt_id,
            exact_target_version=target_version,
            premise_versions=premise_versions,
            obligation_ids=obligation_ids,
            status=(
                DerivationStatus.AUDIT_PASSED
                if node.epistemic_status
                in {EpistemicStatus.AUDIT_PASSED, EpistemicStatus.LEAN_VERIFIED}
                else DerivationStatus.AUDIT_FAILED
                if node.epistemic_status is EpistemicStatus.REFUTED
                else DerivationStatus.STALE
                if node.epistemic_status is EpistemicStatus.STALE
                else DerivationStatus.PROPOSED
            ),
            audit_ids=sorted(set(audit_ids_by_target.get(node.matek_id, []))),
            artifact_ids=[
                edge.target_id
                for edge in node.relations
                if edge.relation is RelationType.RELATED_TO and edge.target_id.startswith("ART-")
            ],
        )

    ledger = CanonicalLedger(
        graph_revision=graph_revision,
        problem_id=problem_id,
        target_claim_id=target_claim_id,
        claims=claims,
        derivations=derivations,
        obligations=obligations,
        ambiguities=ambiguities,
    )
    validate_ledger(ledger)
    return refresh_derivation_staleness(ledger)


def ledger_integrity_sha256(ledger: CanonicalLedger) -> str:
    payload = ledger.model_dump(mode="json")
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def write_canonical_ledger(path: Path, ledger: CanonicalLedger) -> Path:
    validate_ledger(ledger)
    payload = ledger.model_dump(mode="json")
    payload["integrity_sha256"] = ledger_integrity_sha256(ledger)
    return atomic_write_json(path, payload, confinement_root=path.parent)


def load_canonical_ledger(path: Path) -> CanonicalLedger:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LedgerError(f"cannot load canonical ledger {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise LedgerError("canonical ledger must contain one JSON object")
    expected = raw.pop("integrity_sha256", None)
    try:
        ledger = CanonicalLedger.model_validate(raw)
    except ValueError as exc:
        raise LedgerError(f"canonical ledger schema is invalid: {exc}") from exc
    if expected != ledger_integrity_sha256(ledger):
        raise LedgerError("canonical ledger integrity digest does not match its contents")
    validate_ledger(ledger)
    return ledger


__all__ = [
    "CanonicalLedger",
    "ClaimStatus",
    "Derivation",
    "DerivationStatus",
    "LedgerAmbiguity",
    "LedgerClaim",
    "LedgerError",
    "Obligation",
    "ObligationStatus",
    "OpenCut",
    "deterministic_ledger_id",
    "ledger_integrity_sha256",
    "load_canonical_ledger",
    "logical_version",
    "obligation_logical_version",
    "project_markdown_ledger",
    "refresh_derivation_staleness",
    "smallest_known_open_cut",
    "trusted_claim_ids",
    "validate_ledger",
    "write_canonical_ledger",
]
