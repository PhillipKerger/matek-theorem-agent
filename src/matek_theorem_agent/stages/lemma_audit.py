"""Independent, resumable audits for high-leverage intermediate lemmas.

This lane deliberately does not participate in the main candidate-acceptance gate.  It turns a
structurally complete intermediate derivation into a blind mathematical packet, obtains fresh
verifier and falsifier reports, and applies a deterministic two-pass gate.  Input and individual
responses are immutable; a derived gate may be replaced only when a previously missing role is
completed during resume.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..config import ModelSettings
from ..graph_ids import validate_any_node_id
from ..knowledge_graph.ledger import logical_version as claim_logical_version
from ..knowledge_graph.ledger import obligation_logical_version
from ..openai_client import (
    ModelClient,
    ModelRequest,
    ModelResult,
    model_request_cache_key,
)
from ..redaction import redact_text
from ..resources import read_resource_text
from ..scientific import ScientificScope, normalize_exact_statement
from .common import (
    StageValidationError,
    atomic_write_bytes,
    canonical_json_bytes,
    ensure_stage_directory,
    provider_session_id_from_metadata,
    read_regular_bytes,
    sha256_bytes,
    sha256_text,
)

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_SAFE_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_PLACEHOLDER = re.compile(
    r"(?i)(?:\bTBD\b|\bTODO\b|\bFIXME\b|\?\?\?|"
    r"\[(?:GAP|MISSING|UNKNOWN|UNCLEAR)\]|"
    r"(?:^|\n)\s*(?:GAP|MISSING STEP)\s*:|"
    r"<[^>]*(?:fill|placeholder|missing)[^>]*>)"
)

LEMMA_AUDIT_ROLES = ("lemma-verifier", "lemma-falsifier")


class _AuditModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def _not_blank(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value.strip())
    if not normalized:
        raise ValueError("lemma-audit text must not be blank")
    return normalized


def _safe_id(value: str) -> str:
    normalized = value.strip()
    if not _SAFE_ID.fullmatch(normalized):
        raise ValueError("lemma-audit IDs must use 1-128 portable characters")
    return normalized


def _graph_id(value: str) -> str:
    try:
        return validate_any_node_id(value)
    except ValueError as exc:
        raise ValueError("lemma-audit graph references must be stable node IDs") from exc


def _artifact_id(value: str) -> str:
    """Artifact references name graph nodes or run-local artifact labels."""

    try:
        return validate_any_node_id(value)
    except ValueError:
        return _safe_id(value)


def _sha256(value: str) -> str:
    if not _SHA256.fullmatch(value):
        raise ValueError("lemma-audit digests must be lowercase SHA-256 values")
    return value


def _unique_strings(values: Sequence[str], *, allow_empty: bool = False) -> list[str]:
    normalized: list[str] = []
    for value in values:
        item = unicodedata.normalize("NFC", value.strip())
        if item or allow_empty:
            normalized.append(item)
    return list(dict.fromkeys(normalized))


def _normalized_math(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


class LemmaScope(StrEnum):
    MAIN = "main"
    REDUCTION = "reduction"
    BRANCH = "branch"
    COMPUTATION = "computation"


class IntermediateResultKind(StrEnum):
    LEMMA = "lemma"
    RESTRICTED_THEOREM = "restricted_theorem"


class LemmaPreflightCode(StrEnum):
    GAPPED = "gapped"
    STALE = "stale"
    AMBIGUOUS = "ambiguous"
    IRRELEVANT = "irrelevant"
    LOW_LEVERAGE = "low_leverage"
    SENSITIVE_INPUT = "sensitive_input"


class LemmaAuditRole(StrEnum):
    VERIFIER = "lemma-verifier"
    FALSIFIER = "lemma-falsifier"


class LemmaAuditDecision(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"


class LemmaAuditGateStatus(StrEnum):
    AUDIT_PASSED = "audit_passed"
    AUDIT_FAILED = "audit_failed"
    BLOCKED = "blocked"


class LemmaProofStep(_AuditModel):
    step_id: str
    statement: str
    justification: str
    depends_on: list[str] = Field(default_factory=list)
    source_artifact_ids: list[str] = Field(default_factory=list)

    @field_validator("step_id")
    @classmethod
    def step_id_is_safe(cls, value: str) -> str:
        return _safe_id(value)

    @field_validator("statement", "justification")
    @classmethod
    def proof_text_is_not_blank(cls, value: str) -> str:
        return _not_blank(value)

    @field_validator("depends_on")
    @classmethod
    def step_references_are_unique(cls, value: list[str]) -> list[str]:
        return [_safe_id(item) for item in _unique_strings(value)]

    @field_validator("source_artifact_ids")
    @classmethod
    def artifact_references_are_unique(cls, value: list[str]) -> list[str]:
        return [_artifact_id(item) for item in _unique_strings(value)]


class LemmaSourceArtifact(_AuditModel):
    artifact_id: str
    media_type: str = "text/plain"
    content: str
    content_sha256: str
    origin_annotations: list[str] = Field(default_factory=list)

    @field_validator("artifact_id")
    @classmethod
    def artifact_id_is_safe(cls, value: str) -> str:
        return _artifact_id(value)

    @field_validator("media_type")
    @classmethod
    def media_type_is_not_blank(cls, value: str) -> str:
        return _not_blank(value)

    @field_validator("content")
    @classmethod
    def artifact_content_is_preserved(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("lemma source artifact content must not be blank")
        return value

    @field_validator("content_sha256")
    @classmethod
    def content_digest_is_valid(cls, value: str) -> str:
        return _sha256(value)

    @field_validator("origin_annotations")
    @classmethod
    def annotations_are_unique(cls, value: list[str]) -> list[str]:
        return _unique_strings(value)


class LemmaDependencyReference(_AuditModel):
    dependency_id: str
    exact_statement: str
    statement_version: int = Field(ge=1)
    content_sha256: str
    current_statement_version: int = Field(ge=1)
    current_content_sha256: str
    origin_status: str | None = None

    @field_validator("dependency_id")
    @classmethod
    def dependency_id_is_safe(cls, value: str) -> str:
        return _graph_id(value)

    @field_validator("exact_statement")
    @classmethod
    def dependency_statement_is_not_blank(cls, value: str) -> str:
        return _not_blank(value)

    @field_validator("content_sha256", "current_content_sha256")
    @classmethod
    def dependency_digests_are_valid(cls, value: str) -> str:
        return _sha256(value)

    @field_validator("origin_status")
    @classmethod
    def optional_status_is_normalized(cls, value: str | None) -> str | None:
        return None if value is None else _not_blank(value)


class LemmaTargetObligationReference(_AuditModel):
    """Frozen semantic and persisted-content contract for one targeted obligation."""

    target_kind: Literal["obligation", "claim"] = "obligation"
    obligation_id: str
    exact_statement: str
    quantifiers: list[str] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    conclusion: str
    dependency_claim_ids: list[str] = Field(default_factory=list)
    target_claim_ids: list[str] = Field(default_factory=list)
    scope: ScientificScope
    notation_definition_version: str
    falsification_evidence: list[str] = Field(default_factory=list)
    logical_version: str
    statement_version: int = Field(ge=1)
    content_sha256: str

    @field_validator("obligation_id")
    @classmethod
    def obligation_identity_is_safe(cls, value: str) -> str:
        return _graph_id(value)

    @field_validator("exact_statement", "conclusion", "notation_definition_version")
    @classmethod
    def obligation_text_is_present(cls, value: str) -> str:
        normalized = normalize_exact_statement(value)
        if not normalized:
            raise ValueError("lemma target obligation text must not be blank")
        return normalized

    @field_validator(
        "quantifiers",
        "hypotheses",
        "falsification_evidence",
    )
    @classmethod
    def obligation_text_lists_are_normalized(cls, value: list[str]) -> list[str]:
        normalized = [normalize_exact_statement(item) for item in value]
        return list(dict.fromkeys(item for item in normalized if item))

    @field_validator("dependency_claim_ids", "target_claim_ids")
    @classmethod
    def obligation_link_ids_are_safe(cls, value: list[str]) -> list[str]:
        return [_graph_id(item) for item in _unique_strings(value)]

    @field_validator("logical_version", "content_sha256")
    @classmethod
    def obligation_hashes_are_valid(cls, value: str) -> str:
        return _sha256(value)

    @model_validator(mode="after")
    def logical_version_covers_complete_contract(self) -> LemmaTargetObligationReference:
        if self.target_kind == "claim":
            if (
                self.conclusion != self.exact_statement
                or self.quantifiers
                or self.hypotheses
                or self.dependency_claim_ids
                or self.target_claim_ids
                or self.falsification_evidence
                or self.logical_version
                != claim_logical_version(
                    self.exact_statement,
                    notation_definition_version=self.notation_definition_version,
                )
            ):
                raise ValueError(
                    "lemma claim-cut references require an exact self-validating claim contract"
                )
            return self
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
            raise ValueError(
                "lemma target obligation logical_version does not match its full contract"
            )
        return self


class LemmaLeverage(_AuditModel):
    downstream_obligation_ids: list[str]
    estimated_open_cut_reduction: int = Field(ge=0)
    unlocked_branch_count: int = Field(ge=0)
    rationale: str

    @field_validator("downstream_obligation_ids")
    @classmethod
    def obligation_ids_are_unique(cls, value: list[str]) -> list[str]:
        return [_graph_id(item) for item in _unique_strings(value)]

    @field_validator("rationale")
    @classmethod
    def rationale_is_not_blank(cls, value: str) -> str:
        return _not_blank(value)

    @property
    def score(self) -> int:
        return self.estimated_open_cut_reduction + self.unlocked_branch_count


class LemmaNomination(_AuditModel):
    """Proof-architect nomination before origin metadata is stripped."""

    schema_version: Literal[1] = 1
    nomination_id: str
    statement_id: str
    canonical_derivation_id: str
    result_kind: IntermediateResultKind
    scope: LemmaScope
    exact_statement: str
    hypotheses: list[str] = Field(default_factory=list)
    main_target_statement: str
    target_obligation_ids: list[str]
    target_obligation_contracts: list[LemmaTargetObligationReference]
    relevance_statement: str
    supports_main_target: bool
    proof_steps: list[LemmaProofStep]
    conclusion_step_id: str
    gap_free: bool
    unresolved_obligations: list[str] = Field(default_factory=list)
    ambiguity_flags: list[str] = Field(default_factory=list)
    base_graph_revision: str
    current_graph_revision: str
    dependencies: list[LemmaDependencyReference] = Field(default_factory=list)
    source_artifacts: list[LemmaSourceArtifact] = Field(default_factory=list)
    leverage: LemmaLeverage
    origin_worker_id: str | None = None
    origin_confidence: str | None = None
    origin_status: str | None = None
    desired_verdict: str | None = None

    @field_validator(
        "nomination_id",
        "conclusion_step_id",
    )
    @classmethod
    def identity_is_safe(cls, value: str) -> str:
        return _safe_id(value)

    @field_validator("statement_id", "canonical_derivation_id")
    @classmethod
    def graph_identity_is_valid(cls, value: str) -> str:
        return _graph_id(value)

    @field_validator(
        "exact_statement",
        "main_target_statement",
        "relevance_statement",
        "base_graph_revision",
        "current_graph_revision",
    )
    @classmethod
    def required_text_is_not_blank(cls, value: str) -> str:
        return _not_blank(value)

    @field_validator("hypotheses", "unresolved_obligations", "ambiguity_flags")
    @classmethod
    def prose_lists_are_unique(cls, value: list[str]) -> list[str]:
        return _unique_strings(value)

    @field_validator("target_obligation_ids")
    @classmethod
    def target_ids_are_unique(cls, value: list[str]) -> list[str]:
        return [_graph_id(item) for item in _unique_strings(value)]

    @field_validator(
        "origin_worker_id",
        "origin_confidence",
        "origin_status",
        "desired_verdict",
    )
    @classmethod
    def optional_origin_text_is_normalized(cls, value: str | None) -> str | None:
        return None if value is None else _not_blank(value)

    @model_validator(mode="after")
    def identities_are_unique(self) -> LemmaNomination:
        step_ids = [step.step_id for step in self.proof_steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("lemma proof step IDs must be unique")
        artifact_ids = [artifact.artifact_id for artifact in self.source_artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("lemma source artifact IDs must be unique")
        dependency_ids = [dependency.dependency_id for dependency in self.dependencies]
        if len(dependency_ids) != len(set(dependency_ids)):
            raise ValueError("lemma dependency IDs must be unique")
        contract_ids = [item.obligation_id for item in self.target_obligation_contracts]
        if len(contract_ids) != len(set(contract_ids)) or set(contract_ids) != set(
            self.target_obligation_ids
        ):
            raise ValueError(
                "lemma target obligation contracts must exactly cover target_obligation_ids"
            )
        return self


class BlindLemmaSourceArtifact(_AuditModel):
    artifact_id: str
    media_type: str
    content: str
    content_sha256: str


class BlindLemmaDependency(_AuditModel):
    dependency_id: str
    exact_statement: str
    statement_version: int
    content_sha256: str


class BlindLemmaAuditPacket(_AuditModel):
    """Exact mathematical evidence with all origin/verdict metadata absent by construction."""

    schema_version: Literal[1] = 1
    audit_id: str
    statement_id: str
    result_kind: IntermediateResultKind
    scope: LemmaScope
    exact_statement: str
    statement_sha256: str
    hypotheses: list[str]
    main_target_statement: str
    target_obligation_ids: list[str]
    target_obligation_contracts: list[LemmaTargetObligationReference]
    proof_steps: list[LemmaProofStep]
    conclusion_step_id: str
    dependencies: list[BlindLemmaDependency]
    source_artifacts: list[BlindLemmaSourceArtifact]


class LemmaAuditPolicy(_AuditModel):
    minimum_downstream_obligations: int = Field(default=1, ge=1)
    minimum_leverage_score: int = Field(default=1, ge=1)


class LemmaPreflightIssue(_AuditModel):
    code: LemmaPreflightCode
    message: str
    references: list[str] = Field(default_factory=list)

    @field_validator("message")
    @classmethod
    def message_is_not_blank(cls, value: str) -> str:
        return _not_blank(value)


class LemmaPreflightReport(_AuditModel):
    nomination_id: str
    accepted: bool
    issues: list[LemmaPreflightIssue] = Field(default_factory=list)


class LemmaFalsificationFinding(_AuditModel):
    case_description: str
    concrete_instance: str
    observed_failure: str

    @field_validator("case_description", "concrete_instance", "observed_failure")
    @classmethod
    def finding_is_exact(cls, value: str) -> str:
        return _not_blank(value)


class LemmaAuditResponse(_AuditModel):
    schema_version: Literal[1] = 1
    audit_role: LemmaAuditRole
    audit_id: str
    statement_sha256: str
    decision: LemmaAuditDecision
    statement_aligned: bool
    proof_valid: bool | None = None
    counterexample_found: bool = False
    proof_step_ids_checked: list[str]
    source_artifact_ids_checked: list[str]
    checks_performed: list[str]
    boundary_or_adversarial_cases: list[str] = Field(default_factory=list)
    rationale: str
    obligations: list[str] = Field(default_factory=list)
    falsification_evidence: list[LemmaFalsificationFinding] = Field(default_factory=list)

    @field_validator("audit_id")
    @classmethod
    def audit_id_is_safe(cls, value: str) -> str:
        return _safe_id(value)

    @field_validator("statement_sha256")
    @classmethod
    def statement_digest_is_valid(cls, value: str) -> str:
        return _sha256(value)

    @field_validator("proof_step_ids_checked")
    @classmethod
    def checked_step_ids_are_unique(cls, value: list[str]) -> list[str]:
        return [_safe_id(item) for item in _unique_strings(value)]

    @field_validator("source_artifact_ids_checked")
    @classmethod
    def checked_artifact_ids_are_unique(cls, value: list[str]) -> list[str]:
        return [_artifact_id(item) for item in _unique_strings(value)]

    @field_validator(
        "checks_performed",
        "boundary_or_adversarial_cases",
        "obligations",
    )
    @classmethod
    def evidence_lists_are_unique(cls, value: list[str]) -> list[str]:
        return _unique_strings(value)

    @field_validator("rationale")
    @classmethod
    def rationale_is_not_blank(cls, value: str) -> str:
        return _not_blank(value)

    @model_validator(mode="after")
    def decision_has_evidence(self) -> LemmaAuditResponse:
        if not self.checks_performed:
            raise ValueError("lemma auditors must record concrete checks performed")
        if self.decision is LemmaAuditDecision.PASS:
            if self.obligations or self.falsification_evidence or self.counterexample_found:
                raise ValueError("a passing lemma audit cannot retain blocking findings")
            if not self.statement_aligned:
                raise ValueError("a passing lemma audit must align to the exact scoped statement")
            if self.audit_role is LemmaAuditRole.VERIFIER and self.proof_valid is not True:
                raise ValueError("the verifier may pass only a valid complete proof")
            if (
                self.audit_role is LemmaAuditRole.FALSIFIER
                and not self.boundary_or_adversarial_cases
            ):
                raise ValueError("the falsifier must record boundary or adversarial tests")
        elif self.decision is LemmaAuditDecision.FAIL:
            if not self.obligations and not self.falsification_evidence:
                raise ValueError("a failed lemma audit requires an exact obligation or finding")
        elif not self.obligations:
            raise ValueError("a blocked lemma audit requires an exact audit obligation")
        if self.counterexample_found and not self.falsification_evidence:
            raise ValueError("a claimed counterexample requires concrete falsification evidence")
        return self


class LemmaAuditInputArtifact(_AuditModel):
    schema_version: Literal[1, 2] = 2
    audit_id: str
    created_at: datetime
    packet_sha256: str
    packet: BlindLemmaAuditPacket
    settings: ModelSettings
    instruction_sha256: dict[str, str]
    request_sha256: dict[str, str]
    execution_context_ids: dict[str, str] = Field(default_factory=dict)

    @field_validator("packet_sha256")
    @classmethod
    def packet_digest_is_valid(cls, value: str) -> str:
        return _sha256(value)

    @field_validator("instruction_sha256", "request_sha256")
    @classmethod
    def role_hashes_are_valid(cls, value: dict[str, str]) -> dict[str, str]:
        if set(value) != set(LEMMA_AUDIT_ROLES):
            raise ValueError("lemma-audit role hashes must cover both independent roles")
        return {role: _sha256(value[role]) for role in LEMMA_AUDIT_ROLES}

    @field_validator("execution_context_ids")
    @classmethod
    def role_contexts_are_valid(cls, value: dict[str, str]) -> dict[str, str]:
        return {
            LemmaAuditRole(role).value: _safe_id(context_id)
            for role, context_id in sorted(value.items())
        }

    @model_validator(mode="after")
    def v2_has_distinct_role_contexts(self) -> LemmaAuditInputArtifact:
        if self.schema_version == 2:
            if set(self.execution_context_ids) != set(LEMMA_AUDIT_ROLES):
                raise ValueError("lemma-audit v2 input must bind both role execution contexts")
            if len(set(self.execution_context_ids.values())) != len(LemmaAuditRole):
                raise ValueError("lemma-audit v2 roles require distinct execution contexts")
        elif self.execution_context_ids:
            raise ValueError("legacy lemma-audit input cannot claim v2 execution contexts")
        return self


class LemmaAuditEvidence(_AuditModel):
    schema_version: Literal[1, 2] = 2
    audit_id: str
    audit_role: LemmaAuditRole
    completed_at: datetime
    input_sha256: str
    request_sha256: str
    execution_context_id: str | None = None
    provider_session_id: str | None = None
    response_id: str
    response_sha256: str
    response: LemmaAuditResponse

    @field_validator("input_sha256", "request_sha256", "response_sha256")
    @classmethod
    def evidence_digests_are_valid(cls, value: str) -> str:
        return _sha256(value)

    @field_validator("response_id")
    @classmethod
    def response_id_is_not_blank(cls, value: str) -> str:
        return _not_blank(value)

    @field_validator("execution_context_id", "provider_session_id")
    @classmethod
    def optional_context_identity_is_valid(cls, value: str | None) -> str | None:
        return None if value is None else _not_blank(value)

    @model_validator(mode="after")
    def schema_matches_context_evidence(self) -> LemmaAuditEvidence:
        if self.schema_version == 2 and self.execution_context_id is None:
            raise ValueError("lemma-audit v2 evidence requires an execution context")
        if self.schema_version == 1 and (
            self.execution_context_id is not None or self.provider_session_id is not None
        ):
            raise ValueError("legacy lemma-audit evidence cannot claim v2 context metadata")
        return self


class AcceptedIntermediateTheorem(_AuditModel):
    statement_id: str
    result_kind: IntermediateResultKind
    scope: LemmaScope
    exact_statement: str
    hypotheses: list[str]
    proof_sha256: str
    source_artifact_sha256: dict[str, str]
    verifier_evidence_sha256: str
    falsifier_evidence_sha256: str
    status: Literal["audit_passed"] = "audit_passed"
    terminal_main_target_satisfied: Literal[False] = False
    manuscript_authorized: Literal[False] = False

    @field_validator(
        "proof_sha256",
        "verifier_evidence_sha256",
        "falsifier_evidence_sha256",
    )
    @classmethod
    def theorem_digests_are_valid(cls, value: str) -> str:
        return _sha256(value)

    @field_validator("source_artifact_sha256")
    @classmethod
    def source_digests_are_valid(cls, value: dict[str, str]) -> dict[str, str]:
        return {_artifact_id(key): _sha256(digest) for key, digest in sorted(value.items())}


class LemmaAuditGate(_AuditModel):
    schema_version: Literal[1, 2] = 2
    audit_id: str
    decided_at: datetime
    status: LemmaAuditGateStatus
    input_sha256: str
    statement_sha256: str
    result_kind: IntermediateResultKind
    scope: LemmaScope
    response_sha256: dict[str, str]
    response_ids: dict[str, str]
    execution_context_ids: dict[str, str] = Field(default_factory=dict)
    provider_session_ids: dict[str, str] = Field(default_factory=dict)
    missing_roles: list[LemmaAuditRole] = Field(default_factory=list)
    obligations: list[str] = Field(default_factory=list)
    falsification_evidence: list[LemmaFalsificationFinding] = Field(default_factory=list)
    accepted_intermediate: AcceptedIntermediateTheorem | None = None
    main_target_acceptance_authorized: Literal[False] = False
    manuscript_authorized: Literal[False] = False

    @field_validator("input_sha256", "statement_sha256")
    @classmethod
    def gate_digests_are_valid(cls, value: str) -> str:
        return _sha256(value)

    @field_validator("response_sha256")
    @classmethod
    def response_digests_are_valid(cls, value: dict[str, str]) -> dict[str, str]:
        return {
            LemmaAuditRole(role).value: _sha256(digest) for role, digest in sorted(value.items())
        }

    @field_validator("response_ids")
    @classmethod
    def response_id_map_is_valid(cls, value: dict[str, str]) -> dict[str, str]:
        return {
            LemmaAuditRole(role).value: _not_blank(response_id)
            for role, response_id in sorted(value.items())
        }

    @field_validator("execution_context_ids", "provider_session_ids")
    @classmethod
    def role_context_maps_are_valid(cls, value: dict[str, str]) -> dict[str, str]:
        return {
            LemmaAuditRole(role).value: _not_blank(context_id)
            for role, context_id in sorted(value.items())
        }

    @model_validator(mode="after")
    def gate_cannot_promote_the_main_result(self) -> LemmaAuditGate:
        if self.schema_version == 2:
            if set(self.execution_context_ids) != set(LEMMA_AUDIT_ROLES):
                raise ValueError("lemma-audit v2 gate must bind both role execution contexts")
            if not set(self.provider_session_ids).issubset(self.response_ids):
                raise ValueError("provider sessions must belong to committed role evidence")
        elif self.execution_context_ids or self.provider_session_ids:
            raise ValueError("legacy lemma-audit gate cannot claim v2 context metadata")
        if self.status is LemmaAuditGateStatus.AUDIT_PASSED:
            if self.accepted_intermediate is None or self.missing_roles or self.obligations:
                raise ValueError("a passing lemma gate requires a complete intermediate theorem")
        elif self.accepted_intermediate is not None:
            raise ValueError("only a passing lemma gate may represent an accepted intermediate")
        if self.status is LemmaAuditGateStatus.BLOCKED and not self.obligations:
            raise ValueError("a blocked lemma gate requires exact obligations")
        if (
            self.status is LemmaAuditGateStatus.AUDIT_FAILED
            and not self.obligations
            and not self.falsification_evidence
        ):
            raise ValueError("a failed lemma gate requires an obligation or falsification")
        return self


class _LemmaAuditV1ArchiveManifest(_AuditModel):
    schema_version: Literal[1] = 1
    audit_id: str
    archived_schema_version: Literal[1] = 1
    migration_target_schema_version: Literal[2] = 2
    file_sha256: dict[str, str]

    @field_validator("audit_id")
    @classmethod
    def audit_identity_is_safe(cls, value: str) -> str:
        return _safe_id(value)

    @field_validator("file_sha256")
    @classmethod
    def archive_hashes_are_valid(cls, value: dict[str, str]) -> dict[str, str]:
        allowed = {
            "input.json",
            "gate.json",
            *(f"responses/{role}.json" for role in LEMMA_AUDIT_ROLES),
        }
        if "input.json" not in value or not set(value).issubset(allowed):
            raise ValueError("lemma-audit v1 archive has an invalid artifact inventory")
        return {name: _sha256(digest) for name, digest in sorted(value.items())}


class LemmaNominationRejected(StageValidationError):
    """Raised before persistence/model work when deterministic preflight fails."""

    def __init__(self, report: LemmaPreflightReport) -> None:
        self.report = report
        details = "; ".join(issue.message for issue in report.issues)
        super().__init__(f"Lemma nomination {report.nomination_id!r} was rejected: {details}")


class LemmaAuditFileSystem(Protocol):
    def ensure_directory(self, path: Path) -> Path: ...

    def artifact_exists(self, path: Path) -> bool: ...

    def read_bytes(self, path: Path) -> bytes: ...

    def write_atomic_bytes(self, path: Path, contents: bytes) -> Path: ...

    def write_immutable_bytes(self, path: Path, contents: bytes) -> Path: ...

    def remove_regular_file(self, path: Path) -> None: ...


class LocalLemmaAuditFileSystem:
    """Symlink-safe local implementation used by the production stage."""

    def ensure_directory(self, path: Path) -> Path:
        return ensure_stage_directory(path)

    def artifact_exists(self, path: Path) -> bool:
        try:
            entry = os.lstat(path)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise StageValidationError(
                f"Cannot inspect lemma-audit artifact {path}: {exc}"
            ) from exc
        if not stat.S_ISREG(entry.st_mode):
            raise StageValidationError(f"Lemma-audit artifact is not a regular file: {path}")
        return True

    def read_bytes(self, path: Path) -> bytes:
        return read_regular_bytes(path)

    def write_atomic_bytes(self, path: Path, contents: bytes) -> Path:
        return atomic_write_bytes(path, contents)

    def write_immutable_bytes(self, path: Path, contents: bytes) -> Path:
        if self.artifact_exists(path):
            if self.read_bytes(path) != contents:
                raise StageValidationError(
                    f"Immutable lemma-audit artifact changed across resume: {path}"
                )
            return path
        return self.write_atomic_bytes(path, contents)

    def remove_regular_file(self, path: Path) -> None:
        try:
            entry = os.lstat(path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise StageValidationError(
                f"Cannot inspect legacy lemma-audit artifact {path}: {exc}"
            ) from exc
        if not stat.S_ISREG(entry.st_mode):
            raise StageValidationError(f"Legacy lemma-audit artifact is not a regular file: {path}")
        try:
            path.unlink()
        except OSError as exc:
            raise StageValidationError(
                f"Cannot retire archived legacy lemma-audit artifact {path}: {exc}"
            ) from exc


def _issue(
    code: LemmaPreflightCode,
    message: str,
    *references: str,
) -> LemmaPreflightIssue:
    return LemmaPreflightIssue(code=code, message=message, references=list(references))


def _reachable_proof_steps(
    conclusion_step_id: str,
    by_id: Mapping[str, LemmaProofStep],
) -> set[str]:
    reachable: set[str] = set()
    pending = [conclusion_step_id]
    while pending:
        step_id = pending.pop()
        if step_id in reachable or step_id not in by_id:
            continue
        reachable.add(step_id)
        pending.extend(by_id[step_id].depends_on)
    return reachable


def preflight_lemma_nomination(
    nomination: LemmaNomination,
    *,
    policy: LemmaAuditPolicy | None = None,
) -> LemmaPreflightReport:
    """Apply deterministic relevance, freshness, ambiguity, leverage, and closure checks."""

    selected_policy = policy or LemmaAuditPolicy()
    issues: list[LemmaPreflightIssue] = []
    by_id = {step.step_id: step for step in nomination.proof_steps}
    artifact_ids = {artifact.artifact_id for artifact in nomination.source_artifacts}

    if not nomination.gap_free:
        issues.append(
            _issue(
                LemmaPreflightCode.GAPPED,
                "The proof architect did not nominate the derivation as gap-free.",
            )
        )
    if nomination.unresolved_obligations:
        issues.append(
            _issue(
                LemmaPreflightCode.GAPPED,
                "The nomination retains unresolved proof obligations.",
                *nomination.unresolved_obligations,
            )
        )
    if not nomination.proof_steps:
        issues.append(_issue(LemmaPreflightCode.GAPPED, "The nomination has no proof derivation."))
    elif nomination.conclusion_step_id not in by_id:
        issues.append(
            _issue(
                LemmaPreflightCode.GAPPED,
                "The nominated conclusion step is missing from the proof derivation.",
                nomination.conclusion_step_id,
            )
        )
    else:
        conclusion = by_id[nomination.conclusion_step_id]
        if _normalized_math(conclusion.statement) != _normalized_math(nomination.exact_statement):
            issues.append(
                _issue(
                    LemmaPreflightCode.AMBIGUOUS,
                    "The final proof step does not exactly state the nominated scoped theorem.",
                    nomination.conclusion_step_id,
                )
            )
        reachable = _reachable_proof_steps(nomination.conclusion_step_id, by_id)
        disconnected = sorted(set(by_id) - reachable)
        if disconnected:
            issues.append(
                _issue(
                    LemmaPreflightCode.AMBIGUOUS,
                    "The derivation contains steps disconnected from its nominated conclusion.",
                    *disconnected,
                )
            )

    step_positions = {step.step_id: index for index, step in enumerate(nomination.proof_steps)}
    for step in nomination.proof_steps:
        for dependency_id in step.depends_on:
            if dependency_id not in by_id:
                issues.append(
                    _issue(
                        LemmaPreflightCode.GAPPED,
                        f"Proof step {step.step_id} depends on missing step {dependency_id}.",
                        step.step_id,
                        dependency_id,
                    )
                )
            elif step_positions[dependency_id] >= step_positions[step.step_id]:
                issues.append(
                    _issue(
                        LemmaPreflightCode.GAPPED,
                        f"Proof step {step.step_id} has a cyclic or forward dependency.",
                        step.step_id,
                        dependency_id,
                    )
                )
        unknown_artifacts = sorted(set(step.source_artifact_ids) - artifact_ids)
        if unknown_artifacts:
            issues.append(
                _issue(
                    LemmaPreflightCode.GAPPED,
                    f"Proof step {step.step_id} cites unavailable source artifacts.",
                    *unknown_artifacts,
                )
            )

    ambiguous_references = [*nomination.ambiguity_flags]
    mathematical_text = [
        nomination.exact_statement,
        *nomination.hypotheses,
        *(step.statement for step in nomination.proof_steps),
        *(step.justification for step in nomination.proof_steps),
    ]
    if any(_PLACEHOLDER.search(value) for value in mathematical_text):
        ambiguous_references.append("placeholder_or_gap_marker")
    if ambiguous_references:
        issues.append(
            _issue(
                LemmaPreflightCode.AMBIGUOUS,
                "The nominated statement or derivation retains ambiguity markers.",
                *ambiguous_references,
            )
        )

    if nomination.base_graph_revision != nomination.current_graph_revision:
        issues.append(
            _issue(
                LemmaPreflightCode.STALE,
                "The nomination was built from a stale graph revision.",
                nomination.base_graph_revision,
                nomination.current_graph_revision,
            )
        )
    for dependency in nomination.dependencies:
        if (
            dependency.statement_version != dependency.current_statement_version
            or dependency.content_sha256 != dependency.current_content_sha256
        ):
            issues.append(
                _issue(
                    LemmaPreflightCode.STALE,
                    f"Dependency {dependency.dependency_id} changed after nomination.",
                    dependency.dependency_id,
                )
            )
    for artifact in nomination.source_artifacts:
        if sha256_text(artifact.content) != artifact.content_sha256:
            issues.append(
                _issue(
                    LemmaPreflightCode.STALE,
                    f"Source artifact {artifact.artifact_id} does not match its frozen hash.",
                    artifact.artifact_id,
                )
            )

    target_ids = set(nomination.target_obligation_ids)
    downstream_ids = set(nomination.leverage.downstream_obligation_ids)
    if (
        not nomination.supports_main_target
        or not target_ids
        or not target_ids.intersection(downstream_ids)
    ):
        issues.append(
            _issue(
                LemmaPreflightCode.IRRELEVANT,
                "The proposed lemma is not linked to a live main-target obligation.",
                *sorted(target_ids),
            )
        )
    if (
        len(downstream_ids) < selected_policy.minimum_downstream_obligations
        or nomination.leverage.score < selected_policy.minimum_leverage_score
    ):
        issues.append(
            _issue(
                LemmaPreflightCode.LOW_LEVERAGE,
                "The proposed lemma does not meet the deterministic leverage threshold.",
            )
        )

    blind_candidate = _blind_packet(nomination)
    serialized_blind = json.dumps(
        blind_candidate.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
    )
    if redact_text(serialized_blind) != serialized_blind:
        issues.append(
            _issue(
                LemmaPreflightCode.SENSITIVE_INPUT,
                "The mathematical audit packet contains secret-like material and cannot persist.",
            )
        )

    return LemmaPreflightReport(
        nomination_id=nomination.nomination_id,
        accepted=not issues,
        issues=issues,
    )


def _blind_packet(nomination: LemmaNomination) -> BlindLemmaAuditPacket:
    statement_digest = sha256_text(_normalized_math(nomination.exact_statement))
    return BlindLemmaAuditPacket(
        audit_id=nomination.nomination_id,
        statement_id=nomination.statement_id,
        result_kind=nomination.result_kind,
        scope=nomination.scope,
        exact_statement=nomination.exact_statement,
        statement_sha256=statement_digest,
        hypotheses=nomination.hypotheses,
        main_target_statement=nomination.main_target_statement,
        target_obligation_ids=nomination.target_obligation_ids,
        target_obligation_contracts=nomination.target_obligation_contracts,
        proof_steps=nomination.proof_steps,
        conclusion_step_id=nomination.conclusion_step_id,
        dependencies=[
            BlindLemmaDependency(
                dependency_id=item.dependency_id,
                exact_statement=item.exact_statement,
                statement_version=item.statement_version,
                content_sha256=item.content_sha256,
            )
            for item in nomination.dependencies
        ],
        source_artifacts=[
            BlindLemmaSourceArtifact(
                artifact_id=item.artifact_id,
                media_type=item.media_type,
                content=item.content,
                content_sha256=item.content_sha256,
            )
            for item in nomination.source_artifacts
        ],
    )


def _now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime):
        raise StageValidationError("Lemma-audit clock must return a datetime.")
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _load_model(
    filesystem: LemmaAuditFileSystem,
    path: Path,
    model: type[_AuditModel],
) -> _AuditModel:
    raw = filesystem.read_bytes(path)
    try:
        return model.model_validate_json(raw)
    except (ValueError, UnicodeError) as exc:
        raise StageValidationError(f"Lemma-audit artifact is invalid: {path}: {exc}") from exc


def _archive_legacy_v1_for_upgrade(
    filesystem: LemmaAuditFileSystem,
    root: Path,
) -> None:
    """Preserve v1 evidence, then retire only the archived canonical copies.

    The immutable manifest is committed before any canonical v1 file is removed.  Re-entering
    this function after a crash validates the archive and finishes the same bounded retirement,
    allowing the ordinary runner to create a fresh v2 input and rerun both roles from scratch.
    """

    archive_root = filesystem.ensure_directory(root / "legacy-v1")
    manifest_path = archive_root / "manifest.json"
    source_paths = {
        "input.json": root / "input.json",
        "gate.json": root / "gate.json",
        **{
            f"responses/{role.value}.json": root / "responses" / f"{role.value}.json"
            for role in LemmaAuditRole
        },
    }
    artifact_models: dict[str, type[_AuditModel]] = {
        "input.json": LemmaAuditInputArtifact,
        "gate.json": LemmaAuditGate,
        **{f"responses/{role.value}.json": LemmaAuditEvidence for role in LemmaAuditRole},
    }

    if filesystem.artifact_exists(manifest_path):
        loaded_manifest = _load_model(
            filesystem,
            manifest_path,
            _LemmaAuditV1ArchiveManifest,
        )
        assert isinstance(loaded_manifest, _LemmaAuditV1ArchiveManifest)
        manifest = loaded_manifest
    else:
        input_path = source_paths["input.json"]
        loaded_input = _load_model(filesystem, input_path, LemmaAuditInputArtifact)
        assert isinstance(loaded_input, LemmaAuditInputArtifact)
        if loaded_input.schema_version != 1:
            raise StageValidationError("Only lemma-audit v1 evidence may enter v1 migration.")
        hashes: dict[str, str] = {}
        for relative, source in source_paths.items():
            if not filesystem.artifact_exists(source):
                continue
            contents = filesystem.read_bytes(source)
            destination = archive_root / relative
            filesystem.ensure_directory(destination.parent)
            filesystem.write_immutable_bytes(destination, contents)
            hashes[relative] = sha256_bytes(contents)
        manifest = _LemmaAuditV1ArchiveManifest(
            audit_id=loaded_input.audit_id,
            file_sha256=hashes,
        )
        filesystem.write_immutable_bytes(manifest_path, canonical_json_bytes(manifest))

    for relative, expected_sha256 in manifest.file_sha256.items():
        archived_path = archive_root / relative
        if not filesystem.artifact_exists(archived_path):
            raise StageValidationError(
                f"Lemma-audit v1 archive is missing committed artifact {relative}."
            )
        if sha256_bytes(filesystem.read_bytes(archived_path)) != expected_sha256:
            raise StageValidationError(
                f"Lemma-audit v1 archive artifact changed after migration: {relative}."
            )

    for relative, source in source_paths.items():
        if not filesystem.artifact_exists(source):
            continue
        loaded = _load_model(filesystem, source, artifact_models[relative])
        schema_version = getattr(loaded, "schema_version", None)
        if schema_version == 2:
            continue
        archived_expected_sha256 = manifest.file_sha256.get(relative)
        if (
            archived_expected_sha256 is None
            or sha256_bytes(filesystem.read_bytes(source)) != archived_expected_sha256
        ):
            raise StageValidationError(
                f"Canonical lemma-audit v1 artifact is not preserved by its archive: {relative}."
            )
        filesystem.remove_regular_file(source)


def _write_immutable_json(
    filesystem: LemmaAuditFileSystem,
    path: Path,
    value: BaseModel | Any,
) -> Path:
    return filesystem.write_immutable_bytes(path, canonical_json_bytes(value))


def _write_gate(
    filesystem: LemmaAuditFileSystem,
    path: Path,
    gate: LemmaAuditGate,
) -> Path:
    return filesystem.write_atomic_bytes(path, canonical_json_bytes(gate))


def _execution_context_ids(audit_id: str) -> dict[str, str]:
    return {
        role.value: ("lemmactx-" + sha256_text("\0".join(["2", audit_id, role.value]))[:40])
        for role in LemmaAuditRole
    }


def _role_request(
    role: LemmaAuditRole,
    packet: BlindLemmaAuditPacket,
    instructions: str,
    settings: ModelSettings,
    *,
    schema_version: Literal[1, 2],
    execution_context_id: str | None,
) -> ModelRequest:
    if schema_version == 2 and execution_context_id is None:
        raise StageValidationError("Lemma-audit v2 requests require an execution context.")
    payload: dict[str, Any] = {
        "schema_version": schema_version,
        "audit_role": role.value,
        "blind_lemma_audit_packet": packet.model_dump(mode="json"),
    }
    if schema_version == 2:
        payload["execution_context"] = {
            "context_id": execution_context_id,
            "mode": "independent role-scoped request",
        }
    return ModelRequest(
        instructions=instructions,
        input_text=json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
        ),
        settings=settings,
    )


def _validate_input_artifact(
    artifact: LemmaAuditInputArtifact,
    *,
    packet: BlindLemmaAuditPacket,
    settings: ModelSettings,
    instruction_sha256: Mapping[str, str],
    request_sha256: Mapping[str, str],
    execution_context_ids: Mapping[str, str],
) -> None:
    if artifact.audit_id != packet.audit_id:
        raise StageValidationError("Lemma-audit input identity changed across resume.")
    if artifact.packet != packet or artifact.packet_sha256 != sha256_bytes(
        canonical_json_bytes(packet)
    ):
        raise StageValidationError("Lemma-audit blind packet changed across resume.")
    if artifact.settings != settings:
        raise StageValidationError("Lemma-audit model settings changed across resume.")
    if artifact.instruction_sha256 != dict(instruction_sha256):
        raise StageValidationError("Lemma-audit role instructions changed across resume.")
    if artifact.request_sha256 != dict(request_sha256):
        raise StageValidationError("Lemma-audit request identities changed across resume.")
    if artifact.execution_context_ids != dict(execution_context_ids):
        raise StageValidationError("Lemma-audit role execution contexts changed across resume.")


def _validate_evidence(
    evidence: LemmaAuditEvidence,
    *,
    role: LemmaAuditRole,
    packet: BlindLemmaAuditPacket,
    input_sha256: str,
    request_sha256: str,
    schema_version: Literal[1, 2],
    execution_context_id: str | None,
) -> None:
    if evidence.audit_id != packet.audit_id or evidence.audit_role is not role:
        raise StageValidationError(f"Committed {role.value} evidence has inconsistent identity.")
    if evidence.input_sha256 != input_sha256 or evidence.request_sha256 != request_sha256:
        raise StageValidationError(f"Committed {role.value} evidence is bound to another input.")
    if evidence.schema_version != schema_version:
        raise StageValidationError(f"Committed {role.value} evidence uses another schema version.")
    if evidence.execution_context_id != execution_context_id:
        raise StageValidationError(f"Committed {role.value} execution context changed.")
    if evidence.response_sha256 != sha256_bytes(canonical_json_bytes(evidence.response)):
        raise StageValidationError(f"Committed {role.value} response hash is invalid.")


def _response_coverage_issues(
    response: LemmaAuditResponse,
    *,
    expected_role: LemmaAuditRole,
    packet: BlindLemmaAuditPacket,
) -> list[str]:
    issues: list[str] = []
    if response.audit_role is not expected_role:
        issues.append(
            f"Obtain a fresh {expected_role.value} report with the correct independent role."
        )
    if response.audit_id != packet.audit_id:
        issues.append(f"Re-audit the frozen nomination {packet.audit_id}; audit identity drifted.")
    if response.statement_sha256 != packet.statement_sha256:
        issues.append("Re-audit the exact frozen scoped statement; the statement hash drifted.")
    expected_steps = {step.step_id for step in packet.proof_steps}
    checked_steps = set(response.proof_step_ids_checked)
    if checked_steps != expected_steps:
        omitted = sorted(expected_steps - checked_steps)
        extra = sorted(checked_steps - expected_steps)
        detail = [
            *(f"unchecked:{item}" for item in omitted),
            *(f"unknown:{item}" for item in extra),
        ]
        issues.append("Audit every exact proof step (" + ", ".join(detail) + ").")
    expected_sources = {item.artifact_id for item in packet.source_artifacts}
    checked_sources = set(response.source_artifact_ids_checked)
    if checked_sources != expected_sources:
        omitted = sorted(expected_sources - checked_sources)
        extra = sorted(checked_sources - expected_sources)
        detail = [
            *(f"unchecked:{item}" for item in omitted),
            *(f"unknown:{item}" for item in extra),
        ]
        issues.append("Audit every exact source artifact (" + ", ".join(detail) + ").")
    if expected_role is LemmaAuditRole.VERIFIER and response.proof_valid is not True:
        issues.append("Supply a verifier report establishing every proof step as valid.")
    if expected_role is LemmaAuditRole.FALSIFIER and not response.boundary_or_adversarial_cases:
        issues.append("Run and record small, boundary, or adversarial falsification tests.")
    return issues


def _deterministic_gate(
    *,
    schema_version: Literal[1, 2],
    packet: BlindLemmaAuditPacket,
    input_sha256: str,
    execution_context_ids: Mapping[LemmaAuditRole, str],
    evidence: Mapping[LemmaAuditRole, LemmaAuditEvidence],
    evidence_file_sha256: Mapping[LemmaAuditRole, str],
    execution_obligations: Mapping[LemmaAuditRole, str],
    decided_at: datetime,
) -> LemmaAuditGate:
    missing_roles = [role for role in LemmaAuditRole if role not in evidence]
    obligations: list[str] = [
        execution_obligations.get(
            role,
            f"Run the missing independent {role.value} audit against the frozen packet.",
        )
        for role in missing_roles
    ]
    findings: list[LemmaFalsificationFinding] = []
    coverage_issues: list[str] = []
    scientific_failure = False
    model_blocked = False
    response_ids = [item.response_id for item in evidence.values()]
    if len(response_ids) != len(set(response_ids)):
        coverage_issues.append(
            "Repeat both lemma audits in independent fresh contexts; response IDs collided."
        )
    if schema_version == 2:
        context_ids = list(execution_context_ids.values())
        if len(context_ids) != len(LemmaAuditRole) or len(context_ids) != len(set(context_ids)):
            coverage_issues.append("Repeat both lemma audits in distinct role execution contexts.")
        provider_session_ids = [
            item.provider_session_id
            for item in evidence.values()
            if item.provider_session_id is not None
        ]
        if len(provider_session_ids) != len(set(provider_session_ids)):
            coverage_issues.append("Repeat both lemma audits in distinct provider sessions.")

    for role in LemmaAuditRole:
        committed = evidence.get(role)
        if committed is None:
            continue
        response = committed.response
        role_coverage = _response_coverage_issues(
            response,
            expected_role=role,
            packet=packet,
        )
        coverage_issues.extend(role_coverage)
        obligations.extend(response.obligations)
        findings.extend(response.falsification_evidence)
        if response.decision is LemmaAuditDecision.FAIL or response.counterexample_found:
            scientific_failure = True
        elif response.decision is LemmaAuditDecision.BLOCKED:
            model_blocked = True

    if missing_roles or model_blocked:
        status = LemmaAuditGateStatus.BLOCKED
    elif scientific_failure or coverage_issues:
        status = LemmaAuditGateStatus.AUDIT_FAILED
    elif all(
        evidence[role].response.decision is LemmaAuditDecision.PASS for role in LemmaAuditRole
    ):
        status = LemmaAuditGateStatus.AUDIT_PASSED
    else:  # pragma: no cover - exhaustive enum/role handling above
        status = LemmaAuditGateStatus.BLOCKED
        obligations.append("Complete both independent lemma audits.")

    obligations = list(dict.fromkeys([*obligations, *coverage_issues]))
    accepted: AcceptedIntermediateTheorem | None = None
    if status is LemmaAuditGateStatus.AUDIT_PASSED:
        proof_digest = sha256_bytes(
            canonical_json_bytes([step.model_dump(mode="json") for step in packet.proof_steps])
        )
        accepted = AcceptedIntermediateTheorem(
            statement_id=packet.statement_id,
            result_kind=packet.result_kind,
            scope=packet.scope,
            exact_statement=packet.exact_statement,
            hypotheses=packet.hypotheses,
            proof_sha256=proof_digest,
            source_artifact_sha256={
                item.artifact_id: item.content_sha256 for item in packet.source_artifacts
            },
            verifier_evidence_sha256=evidence_file_sha256[LemmaAuditRole.VERIFIER],
            falsifier_evidence_sha256=evidence_file_sha256[LemmaAuditRole.FALSIFIER],
        )

    return LemmaAuditGate(
        schema_version=schema_version,
        audit_id=packet.audit_id,
        decided_at=decided_at,
        status=status,
        input_sha256=input_sha256,
        statement_sha256=packet.statement_sha256,
        result_kind=packet.result_kind,
        scope=packet.scope,
        response_sha256={
            role.value: evidence_file_sha256[role]
            for role in LemmaAuditRole
            if role in evidence_file_sha256
        },
        response_ids={
            role.value: evidence[role].response_id for role in LemmaAuditRole if role in evidence
        },
        execution_context_ids=(
            {role.value: execution_context_ids[role] for role in LemmaAuditRole}
            if schema_version == 2
            else {}
        ),
        provider_session_ids=(
            {
                role.value: str(evidence[role].provider_session_id)
                for role in LemmaAuditRole
                if role in evidence and evidence[role].provider_session_id is not None
            }
            if schema_version == 2
            else {}
        ),
        missing_roles=missing_roles,
        obligations=obligations,
        falsification_evidence=findings,
        accepted_intermediate=accepted,
    )


def _validate_complete_gate(
    gate: LemmaAuditGate,
    recomputed: LemmaAuditGate,
) -> None:
    if gate.model_dump(mode="json") != recomputed.model_dump(mode="json"):
        raise StageValidationError("Committed lemma-audit gate does not match its evidence.")


def verify_persisted_lemma_audit(
    nomination_path: Path,
    gate_path: Path,
) -> tuple[LemmaNomination, LemmaAuditGate]:
    """Re-establish a committed nomination-to-gate trust chain without model calls.

    Graph admission uses this read-only boundary instead of accepting a caller's
    nomination/gate dictionaries.  Every available response is rebound to the frozen
    input and, once both independent roles are present, the deterministic gate is
    recomputed byte-for-byte using the gate's already committed ``decided_at``.
    """

    nomination_file = Path(os.path.abspath(nomination_path))
    gate_file = Path(os.path.abspath(gate_path))
    audit_root = gate_file.parent
    if gate_file.name != "gate.json" or nomination_file != audit_root / "nomination.json":
        raise StageValidationError(
            "Lemma-audit nomination and gate paths are not canonical siblings."
        )

    filesystem = LocalLemmaAuditFileSystem()
    loaded_nomination = _load_model(filesystem, nomination_file, LemmaNomination)
    assert isinstance(loaded_nomination, LemmaNomination)
    nomination = loaded_nomination
    if audit_root.name != nomination.nomination_id:
        raise StageValidationError("Lemma-audit directory identity does not match its nomination.")
    preflight = preflight_lemma_nomination(nomination)
    if not preflight.accepted:
        raise LemmaNominationRejected(preflight)

    packet = _blind_packet(nomination)
    input_path = audit_root / "input.json"
    loaded_input = _load_model(filesystem, input_path, LemmaAuditInputArtifact)
    assert isinstance(loaded_input, LemmaAuditInputArtifact)
    instructions = {
        LemmaAuditRole.VERIFIER: read_resource_text("prompts/lemma_verifier.md"),
        LemmaAuditRole.FALSIFIER: read_resource_text("prompts/lemma_falsifier.md"),
    }
    execution_context_ids = (
        _execution_context_ids(packet.audit_id) if loaded_input.schema_version == 2 else {}
    )
    requests = {
        role: _role_request(
            role,
            packet,
            instructions[role],
            loaded_input.settings,
            schema_version=loaded_input.schema_version,
            execution_context_id=execution_context_ids.get(role.value),
        )
        for role in LemmaAuditRole
    }
    instruction_hashes = {role.value: sha256_text(instructions[role]) for role in LemmaAuditRole}
    request_hashes = {
        role.value: model_request_cache_key(
            requests[role],
            LemmaAuditResponse,
            stage="lemma_audit",
            cache_namespace=role.value,
        )
        for role in LemmaAuditRole
    }
    _validate_input_artifact(
        loaded_input,
        packet=packet,
        settings=loaded_input.settings,
        instruction_sha256=instruction_hashes,
        request_sha256=request_hashes,
        execution_context_ids=execution_context_ids,
    )
    input_sha256 = sha256_bytes(filesystem.read_bytes(input_path))

    evidence: dict[LemmaAuditRole, LemmaAuditEvidence] = {}
    evidence_file_sha256: dict[LemmaAuditRole, str] = {}
    for role in LemmaAuditRole:
        response_path = audit_root / "responses" / f"{role.value}.json"
        if not filesystem.artifact_exists(response_path):
            continue
        loaded_evidence = _load_model(filesystem, response_path, LemmaAuditEvidence)
        assert isinstance(loaded_evidence, LemmaAuditEvidence)
        _validate_evidence(
            loaded_evidence,
            role=role,
            packet=packet,
            input_sha256=input_sha256,
            request_sha256=request_hashes[role.value],
            schema_version=loaded_input.schema_version,
            execution_context_id=execution_context_ids.get(role.value),
        )
        evidence[role] = loaded_evidence
        evidence_file_sha256[role] = sha256_bytes(filesystem.read_bytes(response_path))

    loaded_gate = _load_model(filesystem, gate_file, LemmaAuditGate)
    assert isinstance(loaded_gate, LemmaAuditGate)
    gate = loaded_gate
    if gate.schema_version != loaded_input.schema_version:
        raise StageValidationError("Committed lemma-audit gate uses another schema version.")
    if gate.audit_id != packet.audit_id or gate.input_sha256 != input_sha256:
        raise StageValidationError("Committed lemma-audit gate is bound to another input.")
    if (
        gate.statement_sha256 != packet.statement_sha256
        or gate.result_kind is not packet.result_kind
        or gate.scope is not packet.scope
    ):
        raise StageValidationError(
            "Committed lemma-audit gate is bound to another scoped statement."
        )
    expected_response_hashes = {
        role.value: evidence_file_sha256[role] for role in LemmaAuditRole if role in evidence
    }
    expected_response_ids = {
        role.value: evidence[role].response_id for role in LemmaAuditRole if role in evidence
    }
    if gate.response_sha256 != expected_response_hashes:
        raise StageValidationError("Committed lemma-audit gate response digests are incomplete.")
    if gate.response_ids != expected_response_ids:
        raise StageValidationError("Committed lemma-audit gate response identities changed.")
    expected_gate_context_ids = execution_context_ids if loaded_input.schema_version == 2 else {}
    if gate.execution_context_ids != expected_gate_context_ids:
        raise StageValidationError("Committed lemma-audit gate execution contexts changed.")
    expected_provider_sessions = {
        role.value: str(evidence[role].provider_session_id)
        for role in LemmaAuditRole
        if role in evidence and evidence[role].provider_session_id is not None
    }
    if gate.provider_session_ids != expected_provider_sessions:
        raise StageValidationError("Committed lemma-audit gate provider sessions changed.")
    missing_roles = [role for role in LemmaAuditRole if role not in evidence]
    if gate.missing_roles != missing_roles:
        raise StageValidationError("Committed lemma-audit gate has inconsistent missing roles.")
    if not missing_roles:
        recomputed = _deterministic_gate(
            schema_version=loaded_input.schema_version,
            packet=packet,
            input_sha256=input_sha256,
            execution_context_ids={
                role: execution_context_ids[role.value]
                for role in LemmaAuditRole
                if role.value in execution_context_ids
            },
            evidence=evidence,
            evidence_file_sha256=evidence_file_sha256,
            execution_obligations={},
            decided_at=gate.decided_at,
        )
        _validate_complete_gate(gate, recomputed)
        if gate.schema_version == 1 and gate.status is LemmaAuditGateStatus.AUDIT_PASSED:
            raise StageValidationError(
                "Legacy lemma-audit v1 passing evidence cannot establish independent sessions; "
                "run a fresh schema-v2 lemma audit before graph admission."
            )
    elif gate.status is not LemmaAuditGateStatus.BLOCKED:
        raise StageValidationError("An incomplete independent lemma audit must remain blocked.")
    return nomination, gate


def persisted_lemma_audit_response_bindings(
    nomination_path: Path,
    gate_path: Path,
) -> dict[str, tuple[ModelRequest, str]]:
    """Return authenticated role requests and responses for scheduler recovery."""

    nomination, gate = verify_persisted_lemma_audit(nomination_path, gate_path)
    audit_root = Path(os.path.abspath(gate_path)).parent
    filesystem = LocalLemmaAuditFileSystem()
    loaded = _load_model(filesystem, audit_root / "input.json", LemmaAuditInputArtifact)
    assert isinstance(loaded, LemmaAuditInputArtifact)
    packet = _blind_packet(nomination)
    instructions = {
        LemmaAuditRole.VERIFIER: read_resource_text("prompts/lemma_verifier.md"),
        LemmaAuditRole.FALSIFIER: read_resource_text("prompts/lemma_falsifier.md"),
    }
    execution_context_ids = (
        _execution_context_ids(packet.audit_id) if loaded.schema_version == 2 else {}
    )
    requests = {
        role: _role_request(
            role,
            packet,
            instructions[role],
            loaded.settings,
            schema_version=loaded.schema_version,
            execution_context_id=execution_context_ids.get(role.value),
        )
        for role in LemmaAuditRole
    }
    return {
        role.value: (requests[role], gate.response_ids[role.value])
        for role in LemmaAuditRole
        if role.value in gate.response_ids
    }


async def _call_role(
    role: LemmaAuditRole,
    client: ModelClient,
    request: ModelRequest,
) -> tuple[LemmaAuditRole, ModelResult[LemmaAuditResponse]]:
    result = await client.generate_structured(request, LemmaAuditResponse)
    if not result.response_id.strip():
        raise StageValidationError(f"The {role.value} call returned no response identity.")
    return role, result


def _safe_execution_obligation(role: LemmaAuditRole, exc: BaseException) -> str:
    detail = redact_text(str(exc)).replace("\n", " ").strip()
    if len(detail) > 400:
        detail = detail[:397] + "..."
    suffix = f" ({type(exc).__name__}: {detail})" if detail else f" ({type(exc).__name__})"
    return f"Retry the missing independent {role.value} audit against the frozen packet{suffix}."


async def run_lemma_audit(
    nomination: LemmaNomination,
    audit_dir: Path,
    *,
    verifier_client: ModelClient,
    falsifier_client: ModelClient,
    settings: ModelSettings,
    policy: LemmaAuditPolicy | None = None,
    clock: Callable[[], datetime] = _utc_now,
    filesystem: LemmaAuditFileSystem | None = None,
    verifier_instructions: str | None = None,
    falsifier_instructions: str | None = None,
) -> LemmaAuditGate:
    """Audit one intermediate theorem, resuming only roles without immutable evidence.

    Passing this lane records a reusable intermediate theorem only.  Both main-target acceptance
    and manuscript authorization remain hard-coded false in the returned and persisted gate.
    """

    preflight = preflight_lemma_nomination(nomination, policy=policy)
    if not preflight.accepted:
        raise LemmaNominationRejected(preflight)

    selected_filesystem = filesystem or LocalLemmaAuditFileSystem()
    root = selected_filesystem.ensure_directory(Path(os.path.abspath(audit_dir)))
    responses_dir = selected_filesystem.ensure_directory(root / "responses")
    input_path = root / "input.json"
    gate_path = root / "gate.json"
    packet = _blind_packet(nomination)

    legacy_manifest_path = root / "legacy-v1" / "manifest.json"
    legacy_upgrade_in_progress = selected_filesystem.artifact_exists(legacy_manifest_path)
    if not legacy_upgrade_in_progress and selected_filesystem.artifact_exists(input_path):
        candidate_input = _load_model(
            selected_filesystem,
            input_path,
            LemmaAuditInputArtifact,
        )
        assert isinstance(candidate_input, LemmaAuditInputArtifact)
        legacy_upgrade_in_progress = candidate_input.schema_version == 1
    if legacy_upgrade_in_progress:
        _archive_legacy_v1_for_upgrade(selected_filesystem, root)

    instructions = {
        LemmaAuditRole.VERIFIER: verifier_instructions
        or read_resource_text("prompts/lemma_verifier.md"),
        LemmaAuditRole.FALSIFIER: falsifier_instructions
        or read_resource_text("prompts/lemma_falsifier.md"),
    }
    if any(not value.strip() for value in instructions.values()):
        raise StageValidationError("Lemma-audit role instructions must not be blank.")
    instruction_hashes = {role.value: sha256_text(instructions[role]) for role in LemmaAuditRole}
    existing_input: LemmaAuditInputArtifact | None = None
    if selected_filesystem.artifact_exists(input_path):
        loaded = _load_model(selected_filesystem, input_path, LemmaAuditInputArtifact)
        assert isinstance(loaded, LemmaAuditInputArtifact)
        existing_input = loaded
    schema_version: Literal[1, 2] = (
        existing_input.schema_version if existing_input is not None else 2
    )
    execution_context_ids = _execution_context_ids(packet.audit_id) if schema_version == 2 else {}
    requests = {
        role: _role_request(
            role,
            packet,
            instructions[role],
            settings,
            schema_version=schema_version,
            execution_context_id=execution_context_ids.get(role.value),
        )
        for role in LemmaAuditRole
    }
    request_hashes = {
        role.value: model_request_cache_key(
            requests[role],
            LemmaAuditResponse,
            stage="lemma_audit",
            cache_namespace=role.value,
        )
        for role in LemmaAuditRole
    }
    packet_digest = sha256_bytes(canonical_json_bytes(packet))

    if existing_input is not None:
        loaded_input = existing_input
        _validate_input_artifact(
            loaded_input,
            packet=packet,
            settings=settings,
            instruction_sha256=instruction_hashes,
            request_sha256=request_hashes,
            execution_context_ids=execution_context_ids,
        )
    else:
        loaded_input = LemmaAuditInputArtifact(
            schema_version=2,
            audit_id=packet.audit_id,
            created_at=_now(clock),
            packet_sha256=packet_digest,
            packet=packet,
            settings=settings,
            instruction_sha256=instruction_hashes,
            request_sha256=request_hashes,
            execution_context_ids=execution_context_ids,
        )
        _write_immutable_json(selected_filesystem, input_path, loaded_input)
    input_sha256 = sha256_bytes(selected_filesystem.read_bytes(input_path))

    evidence: dict[LemmaAuditRole, LemmaAuditEvidence] = {}
    evidence_file_sha256: dict[LemmaAuditRole, str] = {}
    response_paths = {role: responses_dir / f"{role.value}.json" for role in LemmaAuditRole}
    for role, path in response_paths.items():
        if not selected_filesystem.artifact_exists(path):
            continue
        loaded = _load_model(selected_filesystem, path, LemmaAuditEvidence)
        assert isinstance(loaded, LemmaAuditEvidence)
        _validate_evidence(
            loaded,
            role=role,
            packet=packet,
            input_sha256=input_sha256,
            request_sha256=request_hashes[role.value],
            schema_version=schema_version,
            execution_context_id=execution_context_ids.get(role.value),
        )
        evidence[role] = loaded
        evidence_file_sha256[role] = sha256_bytes(selected_filesystem.read_bytes(path))

    committed_gate: LemmaAuditGate | None = None
    if selected_filesystem.artifact_exists(gate_path):
        loaded_gate = _load_model(selected_filesystem, gate_path, LemmaAuditGate)
        assert isinstance(loaded_gate, LemmaAuditGate)
        if loaded_gate.schema_version != schema_version:
            raise StageValidationError("Committed lemma-audit gate uses another schema version.")
        if loaded_gate.input_sha256 != input_sha256:
            raise StageValidationError("Committed lemma-audit gate is bound to another input.")
        for role_name, digest in loaded_gate.response_sha256.items():
            role = LemmaAuditRole(role_name)
            if evidence_file_sha256.get(role) != digest:
                raise StageValidationError(
                    f"Committed lemma-audit gate has changed {role.value} evidence."
                )
        committed_gate = loaded_gate

    missing = [role for role in LemmaAuditRole if role not in evidence]
    if not missing and committed_gate is not None and not committed_gate.missing_roles:
        recomputed = _deterministic_gate(
            schema_version=schema_version,
            packet=packet,
            input_sha256=input_sha256,
            execution_context_ids={
                role: execution_context_ids[role.value]
                for role in LemmaAuditRole
                if role.value in execution_context_ids
            },
            evidence=evidence,
            evidence_file_sha256=evidence_file_sha256,
            execution_obligations={},
            decided_at=committed_gate.decided_at,
        )
        _validate_complete_gate(committed_gate, recomputed)
        if (
            committed_gate.schema_version == 1
            and committed_gate.status is LemmaAuditGateStatus.AUDIT_PASSED
        ):
            raise StageValidationError(
                "Legacy lemma-audit v1 passing evidence cannot establish independent sessions; "
                "run a fresh schema-v2 lemma audit before graph admission."
            )
        return committed_gate

    if schema_version == 1:
        raise StageValidationError(
            "Legacy lemma-audit v1 archival did not complete; retry the same audit so its "
            "schema-v2 independent roles can run from the preserved nomination."
        )

    clients = {
        LemmaAuditRole.VERIFIER: verifier_client,
        LemmaAuditRole.FALSIFIER: falsifier_client,
    }
    tasks: dict[
        asyncio.Task[tuple[LemmaAuditRole, ModelResult[LemmaAuditResponse]]],
        LemmaAuditRole,
    ] = {
        asyncio.create_task(_call_role(role, clients[role], requests[role])): role
        for role in missing
    }
    execution_obligations: dict[LemmaAuditRole, str] = {}
    pending = set(tasks)
    while pending:
        completed, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for task in completed:
            expected_role = tasks[task]
            try:
                completed_role, result = task.result()
            except BaseException as exc:
                execution_obligations[expected_role] = _safe_execution_obligation(
                    expected_role, exc
                )
                continue
            if completed_role is not expected_role:  # pragma: no cover - internal invariant
                raise StageValidationError("Lemma-audit task returned the wrong role identity.")
            raw_response: Any = result.parsed
            if not isinstance(
                raw_response, LemmaAuditResponse
            ):  # pragma: no cover - protocol guard
                execution_obligations[expected_role] = (
                    f"Retry the missing independent {expected_role.value} audit; "
                    "the provider returned the wrong structured response type."
                )
                continue
            response = raw_response
            serialized_response = canonical_json_bytes(response).decode("utf-8")
            if redact_text(serialized_response) != serialized_response:
                execution_obligations[expected_role] = (
                    f"Retry the missing independent {expected_role.value} audit; "
                    "the response contained secret-like material and was not persisted."
                )
                continue
            try:
                provider_session_id = provider_session_id_from_metadata(result.request_metadata)
            except StageValidationError as exc:
                execution_obligations[expected_role] = _safe_execution_obligation(
                    expected_role,
                    exc,
                )
                continue
            evidence_record = LemmaAuditEvidence(
                schema_version=2,
                audit_id=packet.audit_id,
                audit_role=expected_role,
                completed_at=_now(clock),
                input_sha256=input_sha256,
                request_sha256=request_hashes[expected_role.value],
                execution_context_id=execution_context_ids[expected_role.value],
                provider_session_id=provider_session_id,
                response_id=result.response_id,
                response_sha256=sha256_bytes(canonical_json_bytes(response)),
                response=response,
            )
            path = _write_immutable_json(
                selected_filesystem,
                response_paths[expected_role],
                evidence_record,
            )
            evidence[expected_role] = evidence_record
            evidence_file_sha256[expected_role] = sha256_bytes(selected_filesystem.read_bytes(path))

    gate = _deterministic_gate(
        schema_version=2,
        packet=packet,
        input_sha256=input_sha256,
        execution_context_ids={role: execution_context_ids[role.value] for role in LemmaAuditRole},
        evidence=evidence,
        evidence_file_sha256=evidence_file_sha256,
        execution_obligations=execution_obligations,
        decided_at=_now(clock),
    )
    _write_gate(selected_filesystem, gate_path, gate)
    return gate


__all__ = [
    "LEMMA_AUDIT_ROLES",
    "AcceptedIntermediateTheorem",
    "BlindLemmaAuditPacket",
    "IntermediateResultKind",
    "LemmaAuditDecision",
    "LemmaAuditEvidence",
    "LemmaAuditFileSystem",
    "LemmaAuditGate",
    "LemmaAuditGateStatus",
    "LemmaAuditInputArtifact",
    "LemmaAuditPolicy",
    "LemmaAuditResponse",
    "LemmaAuditRole",
    "LemmaDependencyReference",
    "LemmaFalsificationFinding",
    "LemmaLeverage",
    "LemmaNomination",
    "LemmaNominationRejected",
    "LemmaPreflightCode",
    "LemmaPreflightIssue",
    "LemmaPreflightReport",
    "LemmaProofStep",
    "LemmaScope",
    "LemmaSourceArtifact",
    "LemmaTargetObligationReference",
    "LocalLemmaAuditFileSystem",
    "persisted_lemma_audit_response_bindings",
    "preflight_lemma_nomination",
    "run_lemma_audit",
    "verify_persisted_lemma_audit",
]
