"""Independent, resumable audits for exact-target counterexamples.

Worker and coordinator assertions never cross this trust boundary.  The application first
binds one complete typed main-scope counterexample to the frozen target and its immutable worker
report.  Distinct verifier and hostile-falsifier roles then inspect the same packet.  Only the
deterministic gate derived from both persisted responses can establish a terminal refutation.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..config import ModelSettings
from ..graph_ids import validate_any_node_id
from ..openai_client import ModelClient, ModelRequest, ModelResult, model_request_cache_key
from ..redaction import redact_text
from ..resources import read_resource_text
from ..scientific import (
    ScientificArtifactDeclaration,
    ScientificObligationDeclaration,
    ScientificResult,
    ScientificResultDisposition,
    ScientificResultKind,
    ScientificScope,
    is_explicit_definition_declaration,
    normalize_exact_statement,
    transitive_result_dependency_keys,
    validate_result_dependency_dag,
)
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

# These digests authenticate the immutable, versioned audit policy.  A prompt edit must create
# a new policy version instead of silently changing the meaning of an in-flight audit.  Persisted
# version-1 requests therefore remain verifiable and resumable after installed resources or the
# caller's current settings change, without allowing a forged prompt to self-authenticate.
COUNTEREXAMPLE_AUDIT_POLICY_VERSION = 1
_OFFICIAL_POLICY_PROMPT_SHA256: dict[int, dict[CounterexampleAuditRole, str]] = {}


class _AuditModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def _not_blank(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("counterexample-audit text must not be blank")
    return normalized


def _safe_id(value: str) -> str:
    normalized = value.strip()
    if not _SAFE_ID.fullmatch(normalized):
        raise ValueError("counterexample-audit IDs must use 1-128 portable characters")
    return normalized


def _sha256(value: str) -> str:
    normalized = value.strip().casefold()
    if not _SHA256.fullmatch(normalized):
        raise ValueError("counterexample-audit digests must be lowercase SHA-256 values")
    return normalized


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in values if item.strip()))


class CounterexampleAuditRole(StrEnum):
    VERIFIER = "counterexample-verifier"
    FALSIFIER = "counterexample-falsifier"


COUNTEREXAMPLE_AUDIT_ROLES = tuple(role.value for role in CounterexampleAuditRole)
_OFFICIAL_POLICY_PROMPT_SHA256[COUNTEREXAMPLE_AUDIT_POLICY_VERSION] = {
    CounterexampleAuditRole.VERIFIER: (
        "ba64fc03b32dd3e6189f4debb17299c1f236407ed778a856f978dac35bba1de0"
    ),
    CounterexampleAuditRole.FALSIFIER: (
        "fda085ceb1719684d59a7bf10966839850fb04e04d57b1e102ad225f1e7acd6c"
    ),
}


class CounterexampleAuditDecision(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"


class CounterexampleAuditGateStatus(StrEnum):
    REFUTATION_VERIFIED = "refutation_verified"
    AUDIT_FAILED = "audit_failed"
    BLOCKED = "blocked"


class CounterexampleSupportInvalidated(StageValidationError):
    """Frozen graph support changed through a later canonical graph transition."""


class CounterexampleAuditContextMode(StrEnum):
    """How one role's independent execution context was established."""

    STATELESS_ROLE_REQUEST = "stateless_role_request"
    PROVIDER_SESSION = "provider_session"


@dataclass(frozen=True)
class CounterexampleGraphReadSnapshot:
    """Already-locked graph view used to avoid recursively taking the graph lock."""

    graph_name: str
    state: Any
    nodes: tuple[Any, ...]
    main_target_id: str

    def load_nodes(self, *, include_human_notes: bool = False) -> list[Any]:
        del include_human_notes
        return list(self.nodes)

    def load_state(self) -> Any:
        return self.state

    def main_claim_id(self, problem_id: str) -> str:
        del problem_id
        return self.main_target_id


class CounterexampleComputationSupport(_AuditModel):
    """Current persisted replay evidence used by the counterexample closure."""

    evidence_path: str
    evidence_sha256: str
    manifest_sha256: str
    replay_record_sha256: str
    declaration_sha256s: list[str]

    @field_validator("evidence_path")
    @classmethod
    def evidence_path_is_canonical(cls, value: str) -> str:
        normalized = value.replace("\\", "/").strip()
        if not re.fullmatch(
            r"worker-computation/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json", normalized
        ):
            raise ValueError("counterexample computation evidence path is not canonical")
        return normalized

    @field_validator("evidence_sha256", "manifest_sha256", "replay_record_sha256")
    @classmethod
    def computation_hashes_are_valid(cls, value: str) -> str:
        return _sha256(value)

    @field_validator("declaration_sha256s")
    @classmethod
    def declaration_hashes_are_canonical(cls, values: list[str]) -> list[str]:
        normalized = sorted({_sha256(value) for value in values})
        if not normalized:
            raise ValueError("counterexample computation support needs a declaration hash")
        return normalized


class CounterexampleGraphSupport(_AuditModel):
    """Identity and content binding for the current canonical support slice."""

    graph_name: str
    problem_id: str
    run_id: str
    source_revision: str
    root_counterexample_node_id: str
    result_node_ids: dict[str, list[str]]
    dependency_node_ids: list[str]
    node_content_sha256: dict[str, str]

    @field_validator("graph_name", "run_id")
    @classmethod
    def graph_text_is_nonblank(cls, value: str) -> str:
        return _not_blank(value)

    @field_validator("problem_id", "root_counterexample_node_id")
    @classmethod
    def graph_ids_are_valid(cls, value: str) -> str:
        try:
            return validate_any_node_id(value)
        except ValueError as exc:
            raise ValueError(
                "counterexample graph support has an invalid node ID"
            ) from exc

    @field_validator("source_revision")
    @classmethod
    def graph_revision_is_valid(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not re.fullmatch(r"\d{8}-[0-9a-f]{16}", normalized):
            raise ValueError("counterexample graph support revision is invalid")
        return normalized

    @field_validator("result_node_ids")
    @classmethod
    def result_nodes_are_canonical(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        normalized: dict[str, list[str]] = {}
        for local_key, node_ids in sorted(value.items()):
            key = _safe_id(local_key)
            try:
                ids = sorted({validate_any_node_id(node_id) for node_id in node_ids})
            except ValueError as exc:
                raise ValueError(
                    "counterexample result graph bindings require stable node IDs"
                ) from exc
            if not ids:
                raise ValueError(
                    "counterexample result graph bindings require stable node IDs"
                )
            normalized[key] = ids
        return normalized

    @field_validator("dependency_node_ids")
    @classmethod
    def dependency_ids_are_canonical(cls, values: list[str]) -> list[str]:
        try:
            return sorted({validate_any_node_id(value) for value in values})
        except ValueError as exc:
            raise ValueError("counterexample graph dependency IDs are invalid") from exc

    @field_validator("node_content_sha256")
    @classmethod
    def bound_node_hashes_are_valid(cls, value: dict[str, str]) -> dict[str, str]:
        return {
            validate_any_node_id(node_id): _sha256(digest)
            for node_id, digest in sorted(value.items())
        }


class CounterexampleSupportBundle(_AuditModel):
    """Closed, application-owned support set for one exact counterexample."""

    schema_version: Literal[1] = 1
    root_result_key: str
    result_keys: list[str]
    result_sha256: dict[str, str]
    dependency_node_ids: list[str] = Field(default_factory=list)
    artifact_declaration_sha256s: list[str] = Field(default_factory=list)
    computation: CounterexampleComputationSupport | None = None
    graph: CounterexampleGraphSupport | None = None
    support_sha256: str

    @field_validator("root_result_key")
    @classmethod
    def root_result_key_is_safe(cls, value: str) -> str:
        return _safe_id(value)

    @field_validator("result_keys")
    @classmethod
    def result_keys_are_canonical(cls, values: list[str]) -> list[str]:
        normalized = sorted({_safe_id(value) for value in values})
        if not normalized:
            raise ValueError("counterexample support closure must include its root")
        return normalized

    @field_validator("result_sha256")
    @classmethod
    def result_hashes_are_valid(cls, value: dict[str, str]) -> dict[str, str]:
        return {_safe_id(key): _sha256(digest) for key, digest in sorted(value.items())}

    @field_validator("dependency_node_ids")
    @classmethod
    def support_dependency_ids_are_canonical(cls, values: list[str]) -> list[str]:
        try:
            return sorted({validate_any_node_id(value) for value in values})
        except ValueError as exc:
            raise ValueError("counterexample support dependency IDs are invalid") from exc

    @field_validator("artifact_declaration_sha256s")
    @classmethod
    def artifact_hashes_are_canonical(cls, values: list[str]) -> list[str]:
        return sorted({_sha256(value) for value in values})

    @field_validator("support_sha256")
    @classmethod
    def support_hash_is_valid(cls, value: str) -> str:
        return _sha256(value)

    @model_validator(mode="after")
    def closure_digest_is_valid(self) -> CounterexampleSupportBundle:
        if self.root_result_key not in self.result_keys:
            raise ValueError("counterexample support closure omits its root result")
        if set(self.result_sha256) != set(self.result_keys):
            raise ValueError("counterexample support hashes must exactly cover its result keys")
        payload = self.model_dump(mode="json", exclude={"support_sha256"})
        if self.support_sha256 != sha256_bytes(canonical_json_bytes(payload)):
            raise ValueError("counterexample support bundle hash does not match its payload")
        return self


class ExactCounterexampleNomination(_AuditModel):
    """Application-owned binding from a worker result to the frozen theorem."""

    schema_version: Literal[2] = 2
    audit_id: str
    assignment_id: str
    result_local_key: str
    kind: Literal[ScientificResultKind.COUNTEREXAMPLE] = ScientificResultKind.COUNTEREXAMPLE
    scope: Literal[ScientificScope.MAIN] = ScientificScope.MAIN
    disposition: Literal[ScientificResultDisposition.REFUTED_MECHANISM] = (
        ScientificResultDisposition.REFUTED_MECHANISM
    )
    exact_statement: str
    frozen_target_statement: str
    assumptions: list[str] = Field(default_factory=list)
    proof_or_certificate: str
    dependency_node_ids: list[str] = Field(default_factory=list)
    target_node_ids: list[str] = Field(default_factory=list)
    main_target_node_id: str | None = None
    worker_report_path: str
    worker_report_sha256: str
    scientific_result_sha256: str
    support_bundle: CounterexampleSupportBundle

    @field_validator("audit_id", "assignment_id", "result_local_key")
    @classmethod
    def identities_are_safe(cls, value: str) -> str:
        return _safe_id(value)

    @field_validator("exact_statement", "frozen_target_statement", "proof_or_certificate")
    @classmethod
    def required_text_is_present(cls, value: str) -> str:
        return _not_blank(value)

    @field_validator("worker_report_sha256", "scientific_result_sha256")
    @classmethod
    def digests_are_valid(cls, value: str) -> str:
        return _sha256(value)

    @field_validator("assumptions", "dependency_node_ids", "target_node_ids")
    @classmethod
    def string_lists_are_unique(cls, value: list[str]) -> list[str]:
        return _unique(value)

    @field_validator("worker_report_path")
    @classmethod
    def report_path_is_canonical(cls, value: str) -> str:
        normalized = value.replace("\\", "/").strip()
        if not re.fullmatch(r"workers/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json", normalized):
            raise ValueError("counterexample nomination requires a canonical worker report path")
        return normalized

    @field_validator("main_target_node_id")
    @classmethod
    def optional_target_id_is_normalized(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return validate_any_node_id(value)
        except ValueError as exc:
            raise ValueError("main target node ID is invalid") from exc

    @model_validator(mode="after")
    def exact_contract_is_unchanged(self) -> ExactCounterexampleNomination:
        if normalize_exact_statement(self.exact_statement) != normalize_exact_statement(
            self.frozen_target_statement
        ):
            raise ValueError("counterexample does not match the frozen exact target")
        expected_path = f"workers/{self.assignment_id}.json"
        if self.worker_report_path != expected_path:
            raise ValueError("counterexample nomination belongs to another worker report")
        if self.support_bundle.root_result_key != self.result_local_key:
            raise ValueError("counterexample nomination support belongs to another result")
        if (
            self.support_bundle.result_sha256.get(self.result_local_key)
            != self.scientific_result_sha256
        ):
            raise ValueError("counterexample nomination root support hash changed")
        return self


class BlindExactCounterexamplePacket(_AuditModel):
    schema_version: Literal[2] = 2
    audit_id: str
    exact_statement: str
    target_statement_sha256: str
    assumptions: list[str]
    proof_or_certificate: str
    certificate_sha256: str
    dependency_node_ids: list[str]
    support_result_keys: list[str]
    support_sha256: str
    computation_replay_record_sha256: str | None = None
    graph_support_node_ids: list[str] = Field(default_factory=list)


class CounterexampleAuditResponse(_AuditModel):
    schema_version: Literal[1] = 1
    audit_role: CounterexampleAuditRole
    audit_id: str
    target_statement_sha256: str
    decision: CounterexampleAuditDecision
    statement_aligned: bool
    every_hypothesis_satisfied: bool
    claimed_failure_demonstrated: bool
    certificate_valid: bool
    witness_or_instance: str
    hypothesis_check: str
    conclusion_evaluation: str
    checks_performed: list[str]
    hostile_or_boundary_tests: list[str] = Field(default_factory=list)
    rationale: str
    obligations: list[str] = Field(default_factory=list)

    @field_validator("audit_id")
    @classmethod
    def audit_identity_is_safe(cls, value: str) -> str:
        return _safe_id(value)

    @field_validator("target_statement_sha256")
    @classmethod
    def target_digest_is_valid(cls, value: str) -> str:
        return _sha256(value)

    @field_validator("checks_performed", "hostile_or_boundary_tests", "obligations")
    @classmethod
    def lists_are_normalized(cls, value: list[str]) -> list[str]:
        return _unique(value)

    @field_validator(
        "witness_or_instance",
        "hypothesis_check",
        "conclusion_evaluation",
        "rationale",
    )
    @classmethod
    def rationale_is_present(cls, value: str) -> str:
        return _not_blank(value)

    @model_validator(mode="after")
    def decision_has_complete_evidence(self) -> CounterexampleAuditResponse:
        if not self.checks_performed:
            raise ValueError("counterexample auditors must record concrete checks")
        all_required = (
            self.statement_aligned
            and self.every_hypothesis_satisfied
            and self.claimed_failure_demonstrated
            and self.certificate_valid
        )
        if self.decision is CounterexampleAuditDecision.PASS:
            if not all_required or self.obligations:
                raise ValueError("passing counterexample audit requires every exact check")
            if (
                self.audit_role is CounterexampleAuditRole.FALSIFIER
                and not self.hostile_or_boundary_tests
            ):
                raise ValueError("hostile counterexample audit must record adversarial tests")
        elif self.decision is CounterexampleAuditDecision.FAIL:
            if all_required or not self.obligations:
                raise ValueError("failed counterexample audit requires a concrete defect")
        elif not self.obligations:
            raise ValueError("blocked counterexample audit requires an exact obligation")
        return self


class CounterexampleAuditPolicyArtifact(_AuditModel):
    """First-write-wins official policy used by both independent roles."""

    schema_version: Literal[1] = 1
    audit_id: str
    policy_version: Literal[1] = 1
    settings: ModelSettings
    role_instructions: dict[str, str]
    role_instruction_sha256: dict[str, str]
    role_context_ids: dict[str, str]
    output_schema: Literal["CounterexampleAuditResponse"] = "CounterexampleAuditResponse"

    @field_validator("audit_id")
    @classmethod
    def policy_audit_id_is_safe(cls, value: str) -> str:
        return _safe_id(value)

    @field_validator("role_instruction_sha256")
    @classmethod
    def policy_instruction_hashes_are_valid(cls, value: dict[str, str]) -> dict[str, str]:
        return {
            CounterexampleAuditRole(role).value: _sha256(digest)
            for role, digest in sorted(value.items())
        }

    @field_validator("role_instructions")
    @classmethod
    def policy_instructions_are_complete(cls, value: dict[str, str]) -> dict[str, str]:
        return {
            CounterexampleAuditRole(role).value: (item if item.strip() else _not_blank(item))
            for role, item in sorted(value.items())
        }

    @field_validator("role_context_ids")
    @classmethod
    def policy_contexts_are_complete(cls, value: dict[str, str]) -> dict[str, str]:
        return {
            CounterexampleAuditRole(role).value: _not_blank(item)
            for role, item in sorted(value.items())
        }

    @model_validator(mode="after")
    def policy_roles_are_complete(self) -> CounterexampleAuditPolicyArtifact:
        expected = set(COUNTEREXAMPLE_AUDIT_ROLES)
        if (
            set(self.role_instructions) != expected
            or set(self.role_instruction_sha256) != expected
            or set(self.role_context_ids) != expected
        ):
            raise ValueError("counterexample audit policy must bind both official roles")
        if len(set(self.role_context_ids.values())) != len(CounterexampleAuditRole):
            raise ValueError("counterexample audit roles require distinct execution contexts")
        for role in CounterexampleAuditRole:
            if (
                sha256_text(self.role_instructions[role.value])
                != self.role_instruction_sha256[role.value]
            ):
                raise ValueError("counterexample audit policy prompt hash does not match its text")
        return self


class CounterexampleAuditRequestArtifact(_AuditModel):
    schema_version: Literal[2] = 2
    audit_id: str
    audit_role: CounterexampleAuditRole
    policy_version: Literal[1] = 1
    policy_artifact_sha256: str
    execution_context_id: str
    instructions: str
    input_text: str
    settings: ModelSettings
    output_schema: Literal["CounterexampleAuditResponse"] = "CounterexampleAuditResponse"
    model_request_sha256: str

    @field_validator("policy_artifact_sha256", "model_request_sha256")
    @classmethod
    def request_digest_is_valid(cls, value: str) -> str:
        return _sha256(value)

    @field_validator("execution_context_id")
    @classmethod
    def request_context_is_present(cls, value: str) -> str:
        return _safe_id(value)


class CounterexampleAuditEvidence(_AuditModel):
    schema_version: Literal[2] = 2
    audit_id: str
    audit_role: CounterexampleAuditRole
    completed_at: datetime
    nomination_sha256: str
    request_artifact_sha256: str
    model_request_sha256: str
    execution_context_id: str
    context_mode: CounterexampleAuditContextMode
    provider_session_id: str | None = None
    response_id: str
    response_sha256: str
    response: CounterexampleAuditResponse

    @field_validator(
        "nomination_sha256",
        "request_artifact_sha256",
        "model_request_sha256",
        "response_sha256",
    )
    @classmethod
    def evidence_digests_are_valid(cls, value: str) -> str:
        return _sha256(value)

    @field_validator("response_id", "execution_context_id")
    @classmethod
    def response_identity_is_present(cls, value: str) -> str:
        return _not_blank(value)

    @field_validator("provider_session_id")
    @classmethod
    def optional_session_identity_is_present(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = _safe_id(value)
        if redact_text(normalized) != normalized:
            raise ValueError("counterexample audit provider session identity contains secret data")
        return normalized

    @model_validator(mode="after")
    def context_mode_matches_session(self) -> CounterexampleAuditEvidence:
        if (self.context_mode is CounterexampleAuditContextMode.PROVIDER_SESSION) != (
            self.provider_session_id is not None
        ):
            raise ValueError("counterexample audit context mode does not match session evidence")
        return self


class VerifiedExactCounterexample(_AuditModel):
    exact_statement: str
    target_statement_sha256: str
    certificate_sha256: str
    nomination_sha256: str
    verifier_evidence_sha256: str
    falsifier_evidence_sha256: str
    terminal_main_target_refuted: Literal[True] = True
    manuscript_authorized: Literal[False] = False

    @field_validator(
        "target_statement_sha256",
        "certificate_sha256",
        "nomination_sha256",
        "verifier_evidence_sha256",
        "falsifier_evidence_sha256",
    )
    @classmethod
    def hashes_are_valid(cls, value: str) -> str:
        return _sha256(value)


class CounterexampleAuditGate(_AuditModel):
    schema_version: Literal[2] = 2
    audit_id: str
    decided_at: datetime
    status: CounterexampleAuditGateStatus
    nomination_sha256: str
    target_statement_sha256: str
    policy_artifact_sha256: str
    request_artifact_sha256: dict[str, str]
    response_evidence_sha256: dict[str, str]
    response_ids: dict[str, str]
    execution_context_ids: dict[str, str]
    provider_session_ids: dict[str, str]
    execution_obligations: dict[str, str] = Field(default_factory=dict)
    missing_roles: list[CounterexampleAuditRole] = Field(default_factory=list)
    obligations: list[str] = Field(default_factory=list)
    verified_refutation: VerifiedExactCounterexample | None = None

    @field_validator("nomination_sha256", "target_statement_sha256", "policy_artifact_sha256")
    @classmethod
    def gate_digests_are_valid(cls, value: str) -> str:
        return _sha256(value)

    @field_validator("request_artifact_sha256", "response_evidence_sha256")
    @classmethod
    def role_hashes_are_valid(cls, value: dict[str, str]) -> dict[str, str]:
        return {
            CounterexampleAuditRole(role).value: _sha256(digest)
            for role, digest in sorted(value.items())
        }

    @field_validator("response_ids")
    @classmethod
    def response_id_map_is_valid(cls, value: dict[str, str]) -> dict[str, str]:
        return {
            CounterexampleAuditRole(role).value: _not_blank(response_id)
            for role, response_id in sorted(value.items())
        }

    @field_validator(
        "execution_context_ids",
        "provider_session_ids",
        "execution_obligations",
    )
    @classmethod
    def role_text_maps_are_valid(cls, value: dict[str, str]) -> dict[str, str]:
        return {
            CounterexampleAuditRole(role).value: _not_blank(item)
            for role, item in sorted(value.items())
        }

    @model_validator(mode="after")
    def terminal_status_is_consistent(self) -> CounterexampleAuditGate:
        if self.status is CounterexampleAuditGateStatus.REFUTATION_VERIFIED:
            if self.verified_refutation is None or self.missing_roles or self.obligations:
                raise ValueError("verified refutation requires both clean independent audits")
        elif self.verified_refutation is not None:
            raise ValueError("only a passing gate may carry a verified exact counterexample")
        if self.status is CounterexampleAuditGateStatus.BLOCKED and (
            not self.obligations or not self.missing_roles
        ):
            raise ValueError("retryable counterexample audit must name a missing execution role")
        if self.status is CounterexampleAuditGateStatus.AUDIT_FAILED and not self.obligations:
            raise ValueError("failed counterexample audit requires a concrete defect")
        return self


def _make_support_bundle(
    *,
    root_result_key: str,
    result_keys: list[str],
    result_sha256: dict[str, str],
    dependency_node_ids: list[str],
    artifact_declaration_sha256s: list[str],
    computation: CounterexampleComputationSupport | None,
    graph: CounterexampleGraphSupport | None,
) -> CounterexampleSupportBundle:
    payload = {
        "schema_version": 1,
        "root_result_key": root_result_key,
        "result_keys": sorted(result_keys),
        "result_sha256": dict(sorted(result_sha256.items())),
        "dependency_node_ids": sorted(set(dependency_node_ids)),
        "artifact_declaration_sha256s": sorted(set(artifact_declaration_sha256s)),
        "computation": computation.model_dump(mode="json") if computation is not None else None,
        "graph": graph.model_dump(mode="json") if graph is not None else None,
    }
    return CounterexampleSupportBundle(
        schema_version=1,
        root_result_key=root_result_key,
        result_keys=sorted(result_keys),
        result_sha256=dict(sorted(result_sha256.items())),
        dependency_node_ids=sorted(set(dependency_node_ids)),
        artifact_declaration_sha256s=sorted(set(artifact_declaration_sha256s)),
        computation=computation,
        graph=graph,
        support_sha256=sha256_bytes(canonical_json_bytes(payload)),
    )


def _graph_node_is_live(node: object) -> bool:
    from ..knowledge_graph.models import EpistemicStatus, WorkflowStatus

    return bool(
        not getattr(node, "tombstone", True)
        and not getattr(node, "invalidation_reasons", ["missing"])
        and getattr(node, "epistemic_status", None)
        not in {
            EpistemicStatus.REFUTED,
            EpistemicStatus.INCONSISTENT,
            EpistemicStatus.STALE,
        }
        and getattr(node, "workflow_status", None)
        not in {
            WorkflowStatus.BLOCKED,
            WorkflowStatus.ABANDONED,
            WorkflowStatus.SUPERSEDED,
        }
    )


def _build_graph_support(
    *,
    assignment_id: str,
    root_result: ScientificResult,
    closure_results: Sequence[ScientificResult],
    computation: CounterexampleComputationSupport | None,
    knowledge_graph: object,
    graph_problem_id: str,
    run_id: str,
) -> CounterexampleGraphSupport:
    from ..knowledge_graph.admission import (
        canonical_admitted_definition_scope,
        node_has_scientific_admission_binding,
    )
    from ..knowledge_graph.ledger import (
        ObligationStatus,
        logical_version,
        project_markdown_ledger,
        trusted_claim_ids,
    )
    from ..knowledge_graph.models import (
        EpistemicStatus,
        NodeType,
        RelationType,
        WorkflowStatus,
    )

    load_nodes = getattr(knowledge_graph, "load_nodes", None)
    load_state = getattr(knowledge_graph, "load_state", None)
    main_claim_id = getattr(knowledge_graph, "main_claim_id", None)
    graph_name = getattr(knowledge_graph, "graph_name", None)
    if not callable(load_nodes) or not callable(load_state) or not callable(main_claim_id):
        raise StageValidationError("Named counterexample support requires a canonical graph")
    nodes: list[Any] = list(load_nodes(include_human_notes=False))
    state: Any = load_state()
    if not isinstance(graph_name, str) or not graph_name.strip():
        raise StageValidationError("Counterexample graph support has no stable graph identity")
    target_id = main_claim_id(graph_problem_id)
    problem_nodes = [node for node in nodes if node.problem_id == graph_problem_id]
    by_id = {node.matek_id: node for node in problem_nodes}
    if target_id not in by_id:
        raise StageValidationError("Counterexample support graph has no frozen main target")
    ledger = project_markdown_ledger(
        problem_nodes,
        graph_revision=state.revision,
        problem_id=graph_problem_id,
        target_claim_id=target_id,
    )
    trusted_claims = trusted_claim_ids(ledger)
    result_nodes: dict[str, list[str]] = {}
    conclusion_by_key: dict[str, str] = {}
    derivation_by_key: dict[str, Any] = {}
    attempt_by_key: dict[str, Any] = {}
    root_counterexample_id: str | None = None

    for result in closure_results:
        if target_id in result.dependency_node_ids:
            raise StageValidationError(
                "Counterexample support cannot depend on the main target it purports to refute"
            )
        matching = [
            node
            for node in problem_nodes
            if node_has_scientific_admission_binding(
                node,
                run_id=run_id,
                assignment_id=assignment_id,
                result=result,
            )
        ]
        if result.local_key == root_result.local_key:
            roots = [node for node in matching if node.node_type is NodeType.COUNTEREXAMPLE]
            if len(roots) != 1 or not _graph_node_is_live(roots[0]):
                raise StageValidationError(
                    "Exact counterexample has no single live canonical graph admission"
                )
            root = roots[0]
            if root_result.proof_or_certificate not in root.evidence:
                raise StageValidationError("Canonical counterexample certificate changed")
            root_counterexample_id = root.matek_id
            result_nodes[result.local_key] = [root.matek_id]
            continue

        if result.exact_gap is not None or (
            result.kind is not ScientificResultKind.DEFINITION
            and result.disposition is not ScientificResultDisposition.PROPOSED_COMPLETE
        ):
            raise StageValidationError(
                f"Counterexample support result {result.local_key!r} is not gap-free and complete"
            )
        if result.kind is ScientificResultKind.COUNTEREXAMPLE:
            raise StageValidationError(
                "A counterexample cannot serve as another refutation premise"
            )
        if result.kind is ScientificResultKind.DEFINITION:
            if result.scope is not ScientificScope.BRANCH or not is_explicit_definition_declaration(
                result.exact_statement
            ):
                raise StageValidationError(
                    f"Counterexample definition support {result.local_key!r} is not an explicit "
                    "branch-scoped definitional declaration"
                )
            definitions = [node for node in matching if node.node_type is NodeType.DEFINITION]
            if len(definitions) != 1 or not _graph_node_is_live(definitions[0]):
                raise StageValidationError(
                    f"Counterexample definition support {result.local_key!r} is not live"
                )
            definition = definitions[0]
            if (
                canonical_admitted_definition_scope(definition) is not ScientificScope.BRANCH
                or definition.matek_id not in trusted_claims
            ):
                raise StageValidationError(
                    f"Counterexample definition support {result.local_key!r} lacks current "
                    "canonical admission provenance"
                )
            conclusion_by_key[result.local_key] = definition.matek_id
            result_nodes[result.local_key] = [definition.matek_id]
            continue

        attempts = [node for node in matching if node.node_type is NodeType.PROOF_ATTEMPT]
        derivations = [node for node in matching if node.node_type is NodeType.DERIVATION]
        if (
            len(attempts) != 1
            or len(derivations) != 1
            or not _graph_node_is_live(attempts[0])
            or not _graph_node_is_live(derivations[0])
            or attempts[0].workflow_status is not WorkflowStatus.COMPLETE
        ):
            raise StageValidationError(
                f"Counterexample support result {result.local_key!r} lacks a live derivation"
            )
        derivation = derivations[0]
        attempt = attempts[0]
        conclusions = [
            edge.target_id for edge in derivation.relations if edge.relation is RelationType.PROVES
        ]
        if len(conclusions) != 1 or conclusions[0] not in by_id:
            raise StageValidationError(
                f"Counterexample support result {result.local_key!r} has no exact conclusion"
            )
        conclusion = by_id[conclusions[0]]
        if (
            conclusion.node_type is not NodeType.CLAIM
            or not _graph_node_is_live(conclusion)
            or normalize_exact_statement(_graph_exact_statement(conclusion.body))
            != normalize_exact_statement(result.exact_statement)
            or derivation.metadata.get("matek_proof_attempt_id") != attempt.matek_id
            or derivation.metadata.get("matek_conclusion_claim_id") != conclusion.matek_id
        ):
            raise StageValidationError(
                f"Counterexample support result {result.local_key!r} changed its graph identity"
            )
        if conclusion.matek_id == target_id:
            raise StageValidationError(
                "Counterexample support cannot derive and use the main target it purports to refute"
            )
        if result.kind is not ScientificResultKind.COMPUTATION and (
            conclusion.matek_id not in trusted_claims
        ):
            raise StageValidationError(
                f"Counterexample support result {result.local_key!r} is not canonically trusted"
            )
        conclusion_by_key[result.local_key] = conclusion.matek_id
        derivation_by_key[result.local_key] = derivation
        attempt_by_key[result.local_key] = attempt
        result_nodes[result.local_key] = sorted(
            {attempt.matek_id, derivation.matek_id, conclusion.matek_id}
        )

    if root_counterexample_id is None:
        raise StageValidationError("Counterexample support graph omitted its root admission")

    for result in closure_results:
        expected_dependencies = list(
            dict.fromkeys(
                [
                    *result.dependency_node_ids,
                    *(conclusion_by_key[key] for key in result.dependency_result_keys),
                ]
            )
        )
        owner = (
            by_id[root_counterexample_id]
            if result.local_key == root_result.local_key
            else (
                by_id[result_nodes[result.local_key][0]]
                if result.kind is ScientificResultKind.DEFINITION
                else derivation_by_key[result.local_key]
            )
        )
        actual_dependencies = [
            edge.target_id for edge in owner.relations if edge.relation is RelationType.DEPENDS_ON
        ]
        if actual_dependencies != expected_dependencies:
            raise StageValidationError(
                f"Counterexample support result {result.local_key!r} changed dependency edges"
            )
        missing_dependencies = [
            dependency_id for dependency_id in expected_dependencies if dependency_id not in by_id
        ]
        if missing_dependencies:
            raise StageValidationError(
                f"Counterexample support result {result.local_key!r} has missing dependencies: "
                + ", ".join(missing_dependencies)
            )
        expected_versions = [
            f"{dependency_id}@{logical_version(_graph_node_exact_statement(by_id[dependency_id]))}"
            for dependency_id in expected_dependencies
        ]
        if owner.dependency_versions != expected_versions:
            raise StageValidationError(
                f"Counterexample support result {result.local_key!r} changed dependency versions"
            )
        if result.local_key in attempt_by_key:
            attempt = attempt_by_key[result.local_key]
            attempt_dependencies = [
                edge.target_id
                for edge in attempt.relations
                if edge.relation is RelationType.DEPENDS_ON
            ]
            if (
                attempt_dependencies != expected_dependencies
                or attempt.dependency_versions != expected_versions
            ):
                raise StageValidationError(
                    f"Counterexample support result {result.local_key!r} changed attempt premises"
                )
            derivation = derivation_by_key[result.local_key]
            if derivation.metadata.get(
                "matek_premise_claim_ids"
            ) != expected_dependencies or derivation.metadata.get("matek_premise_versions") != [
                version.replace("@", "=", 1) for version in expected_versions
            ]:
                raise StageValidationError(
                    f"Counterexample support result {result.local_key!r} changed premise metadata"
                )
        for dependency_id in result.dependency_node_ids:
            dependency = by_id.get(dependency_id)
            if dependency is None or not _graph_node_is_live(dependency):
                raise StageValidationError(
                    f"Counterexample support dependency {dependency_id!r} is not live"
                )
            if dependency.node_type is NodeType.CLAIM:
                if dependency_id not in trusted_claims:
                    raise StageValidationError(
                        f"Counterexample support dependency {dependency_id!r} is not trusted"
                    )
                if (
                    dependency.metadata.get("matek_scientific_kind")
                    == ScientificResultKind.COMPUTATION.value
                    or dependency.metadata.get("matek_scientific_scope")
                    == ScientificScope.COMPUTATION.value
                    or "matek/computation" in dependency.tags
                ):
                    raise StageValidationError(
                        f"External computation dependency {dependency_id!r} requires a fresh "
                        "same-report replay/CAS binding"
                    )
            elif dependency.node_type is NodeType.DEFINITION:
                if (
                    dependency_id not in trusted_claims
                    or canonical_admitted_definition_scope(dependency) is not ScientificScope.BRANCH
                ):
                    raise StageValidationError(
                        f"Counterexample definition dependency {dependency_id!r} lacks current "
                        "canonical admission provenance"
                    )
            else:
                raise StageValidationError(
                    f"Counterexample support dependency {dependency_id!r} is not a canonical "
                    "mathematical claim or admitted definition"
                )

    support_ids = {node_id for node_ids in result_nodes.values() for node_id in node_ids} | {
        node_id for result in closure_results for node_id in result.dependency_node_ids
    }
    if computation is not None:
        computation_keys = {
            result.local_key
            for result in closure_results
            if result.kind is ScientificResultKind.COMPUTATION
        }
        manifest_nodes = [
            node
            for node in problem_nodes
            if node.node_type is NodeType.ARTIFACT
            and node.author_role == "computation-collector"
            and node.metadata.get("matek_assignment_id") == assignment_id
            and node.metadata.get("matek_computation_manifest_sha256")
            == computation.manifest_sha256
            and computation_keys.issubset(
                set(node.metadata.get("matek_supporting_result_keys") or [])
            )
        ]
        replay_nodes = [
            node
            for node in problem_nodes
            if node.node_type is NodeType.ARTIFACT
            and node.author_role == "computation-replayer"
            and node.metadata.get("matek_assignment_id") == assignment_id
            and node.metadata.get("matek_computation_replay_record_sha256")
            == computation.replay_record_sha256
            and node.metadata.get("matek_computation_manifest_sha256")
            == computation.manifest_sha256
            and computation_keys.issubset(
                set(node.metadata.get("matek_supporting_result_keys") or [])
            )
        ]
        if (
            len(manifest_nodes) != 1
            or len(replay_nodes) != 1
            or not _graph_node_is_live(manifest_nodes[0])
            or not _graph_node_is_live(replay_nodes[0])
            or manifest_nodes[0].epistemic_status is not EpistemicStatus.AUDIT_PASSED
            or replay_nodes[0].epistemic_status is not EpistemicStatus.AUDIT_PASSED
            or manifest_nodes[0].workflow_status is not WorkflowStatus.COMPLETE
            or replay_nodes[0].workflow_status is not WorkflowStatus.COMPLETE
            or manifest_nodes[0].metadata.get("matek_replay_passed") is not True
            or replay_nodes[0].metadata.get("matek_replay_passed") is not True
            or manifest_nodes[0].metadata.get("matek_computation_replay_status") != "passed"
            or replay_nodes[0].metadata.get("matek_computation_replay_status") != "passed"
        ):
            raise StageValidationError(
                "Counterexample computation is not bound to one trusted graph replay pair"
            )
        manifest_node = manifest_nodes[0]
        replay_node = replay_nodes[0]
        if not any(
            edge.relation is RelationType.RELATED_TO and edge.target_id == manifest_node.matek_id
            for edge in replay_node.relations
        ):
            raise StageValidationError(
                "Counterexample computation replay is not linked to its exact manifest"
            )
        expected_artifact_ids = {manifest_node.matek_id, replay_node.matek_id}
        for result_key in sorted(computation_keys):
            derivation = derivation_by_key[result_key]
            raw_artifact_ids = derivation.metadata.get("matek_artifact_ids")
            artifact_ids = (
                set(raw_artifact_ids)
                if isinstance(raw_artifact_ids, list)
                and all(isinstance(item, str) for item in raw_artifact_ids)
                else set()
            )
            related_artifact_ids = {
                edge.target_id
                for edge in derivation.relations
                if edge.relation is RelationType.RELATED_TO
                and edge.target_id in expected_artifact_ids
            }
            if (
                artifact_ids != expected_artifact_ids
                or related_artifact_ids != expected_artifact_ids
            ):
                raise StageValidationError(
                    f"Counterexample computation {result_key!r} changed its graph artifact pair"
                )
        support_ids.update(expected_artifact_ids)

    for obligation in problem_nodes:
        if obligation.node_type is not NodeType.OBLIGATION:
            continue
        ledger_obligation = ledger.obligations.get(obligation.matek_id)
        metadata_links: set[str] = set()
        for key in (
            "matek_parent_node_ids",
            "matek_parent_derivation_ids",
            "matek_parent_proof_attempt_ids",
            "matek_dependency_claim_ids",
            "matek_target_claim_ids",
        ):
            raw_links = obligation.metadata.get(key)
            if isinstance(raw_links, list):
                metadata_links.update(item for item in raw_links if isinstance(item, str))
        relation_links = {
            edge.target_id
            for edge in obligation.relations
            if edge.relation in {RelationType.BLOCKS, RelationType.DEPENDS_ON, RelationType.TARGETS}
        }
        reciprocal_links = {
            node.matek_id
            for node in problem_nodes
            if node.matek_id in support_ids
            and any(
                edge.relation is RelationType.BLOCKED_BY and edge.target_id == obligation.matek_id
                for edge in node.relations
            )
        }
        ledger_links = (
            {
                *ledger_obligation.parent_derivation_ids,
                *ledger_obligation.dependency_claim_ids,
                *ledger_obligation.target_claim_ids,
            }
            if ledger_obligation is not None
            else set()
        )
        linked_to_support = bool(
            (metadata_links | relation_links | reciprocal_links | ledger_links).intersection(
                support_ids
            )
        ) or any(
            obligation.matek_id
            in (
                node.metadata.get("matek_obligation_ids")
                if isinstance(node.metadata.get("matek_obligation_ids"), list)
                else []
            )
            for node in problem_nodes
            if node.matek_id in support_ids
        )
        unresolved = (
            ledger_obligation is None or ledger_obligation.status is not ObligationStatus.RESOLVED
        )
        if linked_to_support and unresolved:
            raise StageValidationError(
                f"Counterexample support has unresolved graph obligation {obligation.matek_id}"
            )

    node_hashes: dict[str, str] = {}
    for node_id in sorted(support_ids - {root_counterexample_id}):
        node = by_id.get(node_id)
        if node is None or node.content_hash is None:
            raise StageValidationError(
                f"Counterexample support node {node_id!r} has no canonical content hash"
            )
        node_hashes[node_id] = node.content_hash
    return CounterexampleGraphSupport(
        graph_name=graph_name,
        problem_id=graph_problem_id,
        run_id=run_id,
        source_revision=state.revision,
        root_counterexample_node_id=root_counterexample_id,
        result_node_ids=result_nodes,
        dependency_node_ids=sorted(
            {node_id for result in closure_results for node_id in result.dependency_node_ids}
        ),
        node_content_sha256=node_hashes,
    )


def _graph_exact_statement(body: str) -> str:
    from ..knowledge_graph.markdown import exact_statement

    return exact_statement(body)


def _graph_node_exact_statement(node: Any) -> str:
    statement = _graph_exact_statement(node.body)
    return statement or normalize_exact_statement(node.body)


def build_counterexample_support_bundle(
    *,
    assignment_id: str,
    root_result: ScientificResult,
    results: Sequence[ScientificResult],
    unresolved_obligations: Sequence[ScientificObligationDeclaration] = (),
    artifact_manifest: Sequence[ScientificArtifactDeclaration] = (),
    run_root: Path | None = None,
    computation_evidence_path: Path | None = None,
    knowledge_graph: object | None = None,
    graph_problem_id: str | None = None,
    run_id: str | None = None,
) -> CounterexampleSupportBundle:
    """Build and verify the complete support closure before any audit role is called."""

    try:
        validate_result_dependency_dag(results)
        by_key = {result.local_key: result for result in results}
        if by_key.get(root_result.local_key) != root_result:
            raise ValueError("counterexample root does not belong to the supplied worker report")
        closure_keys = sorted(
            {
                root_result.local_key,
                *transitive_result_dependency_keys(results, [root_result.local_key]),
            }
        )
    except ValueError as exc:
        raise StageValidationError(f"Counterexample support DAG is invalid: {exc}") from exc
    closure_results = [by_key[key] for key in closure_keys]
    assumed_results = [result.local_key for result in closure_results if result.assumptions]
    if assumed_results:
        raise StageValidationError(
            "Counterexample support contains unbound assumptions in result(s): "
            + ", ".join(sorted(assumed_results))
        )
    blocking_obligations = sorted(
        obligation.local_key
        for obligation in unresolved_obligations
        if set(obligation.parent_result_keys).intersection(closure_keys)
    )
    if blocking_obligations:
        raise StageValidationError(
            "Counterexample support has unresolved obligation(s): "
            + ", ".join(blocking_obligations)
        )
    relevant_artifacts = [
        declaration
        for declaration in artifact_manifest
        if set(declaration.supporting_result_keys).intersection(closure_keys)
    ]
    computation_results = [
        result for result in closure_results if result.kind is ScientificResultKind.COMPUTATION
    ]
    if computation_results and any(
        not any(
            result.local_key in declaration.supporting_result_keys
            for declaration in relevant_artifacts
        )
        for result in computation_results
    ):
        raise StageValidationError(
            "Counterexample computation support lacks an exact artifact declaration"
        )
    artifact_hashes = sorted(
        sha256_bytes(canonical_json_bytes(declaration)) for declaration in relevant_artifacts
    )
    computation: CounterexampleComputationSupport | None = None
    if relevant_artifacts or computation_results:
        if run_root is None or computation_evidence_path is None:
            raise StageValidationError(
                "Counterexample artifact support lacks persisted computation replay evidence"
            )
        try:
            from .computation_artifacts import verify_persisted_computation_evidence

            evidence = verify_persisted_computation_evidence(
                run_root,
                assignment_id,
                computation_evidence_path,
            )
        except (OSError, ValueError) as exc:
            raise StageValidationError(
                f"Counterexample computation evidence is not trusted: {exc}"
            ) from exc
        if (
            evidence.collection is None
            or not evidence.collection.trusted
            or evidence.collection.manifest is None
            or evidence.replay is None
            or not evidence.replay.trusted
        ):
            raise StageValidationError(
                "Counterexample computation support has no passing isolated replay"
            )
        committed_declarations = {
            item.declaration_sha256 for item in evidence.collection.manifest.declarations
        }
        if not set(artifact_hashes).issubset(committed_declarations):
            raise StageValidationError(
                "Counterexample artifact declarations differ from the replayed manifest"
            )
        research_root = Path(os.path.abspath(run_root)) / "research"
        evidence_path = Path(os.path.abspath(computation_evidence_path))
        try:
            evidence_relative = evidence_path.relative_to(research_root).as_posix()
        except ValueError as exc:
            raise StageValidationError(
                "Counterexample computation evidence escapes the research stage"
            ) from exc
        computation = CounterexampleComputationSupport(
            evidence_path=evidence_relative,
            evidence_sha256=sha256_bytes(read_regular_bytes(evidence_path)),
            manifest_sha256=evidence.collection.manifest.manifest_sha256,
            replay_record_sha256=evidence.replay.record_sha256,
            declaration_sha256s=artifact_hashes,
        )

    dependency_node_ids = sorted(
        {node_id for result in closure_results for node_id in result.dependency_node_ids}
    )
    named_support = len(closure_keys) > 1 or bool(dependency_node_ids or relevant_artifacts)
    graph: CounterexampleGraphSupport | None = None
    if named_support:
        if knowledge_graph is None or graph_problem_id is None or run_id is None:
            raise StageValidationError(
                "Named counterexample support requires current canonical graph trust"
            )
        try:
            graph = _build_graph_support(
                assignment_id=assignment_id,
                root_result=root_result,
                closure_results=closure_results,
                computation=computation,
                knowledge_graph=knowledge_graph,
                graph_problem_id=graph_problem_id,
                run_id=run_id,
            )
        except StageValidationError as exc:
            raise CounterexampleSupportInvalidated(str(exc)) from exc
    return _make_support_bundle(
        root_result_key=root_result.local_key,
        result_keys=closure_keys,
        result_sha256={
            result.local_key: sha256_bytes(canonical_json_bytes(result))
            for result in closure_results
        },
        dependency_node_ids=dependency_node_ids,
        artifact_declaration_sha256s=artifact_hashes,
        computation=computation,
        graph=graph,
    )


def build_exact_counterexample_nomination(
    *,
    assignment_id: str,
    result: ScientificResult,
    frozen_target_statement: str,
    worker_report_path: str,
    worker_report_sha256: str,
    main_target_node_id: str | None = None,
    support_bundle: CounterexampleSupportBundle | None = None,
) -> ExactCounterexampleNomination:
    """Create the application-owned nomination, rejecting every non-main approximation."""

    if result.kind is not ScientificResultKind.COUNTEREXAMPLE:
        raise StageValidationError("only a typed counterexample can enter the refutation lane")
    if result.scope is not ScientificScope.MAIN:
        raise StageValidationError("branch and intermediate counterexamples are nonterminal")
    if result.disposition is not ScientificResultDisposition.REFUTED_MECHANISM:
        raise StageValidationError("counterexample does not declare a complete refuted mechanism")
    if result.exact_gap is not None:
        raise StageValidationError("an incomplete counterexample cannot enter the audit lane")
    if result.assumptions:
        raise StageValidationError(
            "an exact counterexample cannot carry assumptions outside its exact statement"
        )
    if normalize_exact_statement(result.exact_statement) != normalize_exact_statement(
        frozen_target_statement
    ):
        raise StageValidationError("counterexample statement differs from the frozen target")
    if main_target_node_id is not None and main_target_node_id in result.dependency_node_ids:
        raise StageValidationError(
            "counterexample cannot depend on the main target it purports to refute"
        )
    result_digest = sha256_bytes(canonical_json_bytes(result))
    support = support_bundle or build_counterexample_support_bundle(
        assignment_id=assignment_id,
        root_result=result,
        results=[result],
    )
    audit_digest = sha256_text(
        "\0".join(
            [
                assignment_id,
                result.local_key,
                worker_report_sha256,
                result_digest,
                support.support_sha256,
                sha256_text(normalize_exact_statement(frozen_target_statement)),
            ]
        )
    )
    return ExactCounterexampleNomination(
        audit_id=f"cex-{audit_digest[:32]}",
        assignment_id=assignment_id,
        result_local_key=result.local_key,
        exact_statement=result.exact_statement,
        frozen_target_statement=frozen_target_statement,
        assumptions=result.assumptions,
        proof_or_certificate=result.proof_or_certificate,
        dependency_node_ids=result.dependency_node_ids,
        target_node_ids=result.target_node_ids,
        main_target_node_id=main_target_node_id,
        worker_report_path=worker_report_path,
        worker_report_sha256=worker_report_sha256,
        scientific_result_sha256=result_digest,
        support_bundle=support,
    )


def _packet(nomination: ExactCounterexampleNomination) -> BlindExactCounterexamplePacket:
    statement = normalize_exact_statement(nomination.frozen_target_statement)
    return BlindExactCounterexamplePacket(
        audit_id=nomination.audit_id,
        exact_statement=nomination.exact_statement,
        target_statement_sha256=sha256_text(statement),
        assumptions=nomination.assumptions,
        proof_or_certificate=nomination.proof_or_certificate,
        certificate_sha256=sha256_text(nomination.proof_or_certificate),
        dependency_node_ids=nomination.support_bundle.dependency_node_ids,
        support_result_keys=nomination.support_bundle.result_keys,
        support_sha256=nomination.support_bundle.support_sha256,
        computation_replay_record_sha256=(
            nomination.support_bundle.computation.replay_record_sha256
            if nomination.support_bundle.computation is not None
            else None
        ),
        graph_support_node_ids=(
            sorted(nomination.support_bundle.graph.node_content_sha256)
            if nomination.support_bundle.graph is not None
            else []
        ),
    )


def _authenticate_official_policy(policy: CounterexampleAuditPolicyArtifact) -> None:
    known = _OFFICIAL_POLICY_PROMPT_SHA256.get(policy.policy_version)
    if known is None:
        raise StageValidationError(
            f"Unknown counterexample-audit policy version {policy.policy_version}"
        )
    for role in CounterexampleAuditRole:
        expected = known.get(role)
        if expected is None or policy.role_instruction_sha256.get(role.value) != expected:
            raise StageValidationError(
                f"Persisted {role.value} request is not bound to a known official audit policy"
            )
        if sha256_text(policy.role_instructions.get(role.value, "")) != expected:
            raise StageValidationError(
                f"Persisted {role.value} prompt differs from its official policy version"
            )


def _new_official_policy(
    *,
    audit_id: str,
    settings: ModelSettings,
    supplied_instructions: Mapping[CounterexampleAuditRole, str | None],
) -> CounterexampleAuditPolicyArtifact:
    instructions: dict[str, str] = {}
    digests: dict[str, str] = {}
    contexts: dict[str, str] = {}
    resource_names = {
        CounterexampleAuditRole.VERIFIER: "prompts/counterexample_verifier.md",
        CounterexampleAuditRole.FALSIFIER: "prompts/counterexample_falsifier.md",
    }
    for role in CounterexampleAuditRole:
        text = supplied_instructions.get(role) or read_resource_text(resource_names[role])
        if not text.strip():
            raise StageValidationError("Counterexample-audit instructions must not be blank")
        instructions[role.value] = text
        digests[role.value] = sha256_text(instructions[role.value])
        contexts[role.value] = (
            "cexctx-"
            + sha256_text(
                "\0".join(
                    [
                        str(COUNTEREXAMPLE_AUDIT_POLICY_VERSION),
                        audit_id,
                        role.value,
                    ]
                )
            )[:40]
        )
    policy = CounterexampleAuditPolicyArtifact(
        audit_id=audit_id,
        settings=settings,
        role_instructions=instructions,
        role_instruction_sha256=digests,
        role_context_ids=contexts,
    )
    _authenticate_official_policy(policy)
    return policy


def _request(
    role: CounterexampleAuditRole,
    packet: BlindExactCounterexamplePacket,
    instructions: str,
    settings: ModelSettings,
    execution_context_id: str,
) -> ModelRequest:
    input_text = json.dumps(
        {
            "audit_role": role.value,
            "execution_context": {
                "context_id": execution_context_id,
                "mode": "independent role-scoped request",
            },
            "independence_requirement": (
                "Inspect the supplied certificate from scratch. Do not infer correctness from "
                "the worker's status, confidence, or requested terminal outcome."
            ),
            "exact_counterexample_packet": packet.model_dump(mode="json"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return ModelRequest(instructions=instructions, input_text=input_text, settings=settings)


def _write_immutable(path: Path, value: BaseModel) -> Path:
    data = canonical_json_bytes(value)
    if path.exists():
        if read_regular_bytes(path) != data:
            raise StageValidationError(f"Immutable counterexample-audit artifact changed: {path}")
        return path
    return atomic_write_bytes(path, data)


def _write_gate(path: Path, gate: CounterexampleAuditGate) -> Path:
    data = canonical_json_bytes(gate)
    if path.exists():
        existing = CounterexampleAuditGate.model_validate_json(read_regular_bytes(path))
        if existing.status is not CounterexampleAuditGateStatus.BLOCKED:
            if read_regular_bytes(path) != data:
                raise StageValidationError("Completed counterexample-audit gate is immutable")
            return path
    return atomic_write_bytes(path, data)


def _load(path: Path, model: type[_AuditModel]) -> _AuditModel:
    try:
        return model.model_validate_json(read_regular_bytes(path))
    except (OSError, ValueError) as exc:
        raise StageValidationError(f"Invalid counterexample-audit artifact {path}: {exc}") from exc


def _validate_worker_binding(
    nomination: ExactCounterexampleNomination,
    *,
    audit_root: Path,
    graph_snapshot: CounterexampleGraphReadSnapshot | None = None,
) -> None:
    research_root = audit_root.parent.parent
    report_path = (research_root / nomination.worker_report_path).resolve()
    try:
        report_path.relative_to(research_root.resolve())
    except ValueError as exc:
        raise StageValidationError(
            "Counterexample worker report escapes the research stage"
        ) from exc
    report_bytes = read_regular_bytes(report_path)
    if sha256_bytes(report_bytes) != nomination.worker_report_sha256:
        raise StageValidationError("Counterexample worker report is missing or changed")
    try:
        # Local import avoids a module-initialization cycle while requiring the complete
        # production envelope rather than accepting a hand-written result-shaped JSON file.
        from .research import ResearchWorkerReport

        report = ResearchWorkerReport.model_validate_json(report_bytes)
        if report.assignment_id != nomination.assignment_id:
            raise ValueError("worker report belongs to another assignment")
        if report.branch_outcome.value != "refuted":
            raise ValueError("worker report did not declare a refuted branch")
        matches = [
            result for result in report.results if result.local_key == nomination.result_local_key
        ]
    except ValueError as exc:
        raise StageValidationError(f"Counterexample worker report is malformed: {exc}") from exc
    if len(matches) != 1:
        raise StageValidationError("Counterexample nomination does not resolve to one result")
    result = matches[0]
    if sha256_bytes(canonical_json_bytes(result)) != nomination.scientific_result_sha256:
        raise StageValidationError("Counterexample scientific result changed after nomination")
    computation_path: Path | None = None
    run_root: Path | None = None
    if nomination.support_bundle.computation is not None:
        run_root = research_root.parent
        computation_path = research_root / nomination.support_bundle.computation.evidence_path
    graph: object | None = None
    graph_problem_id: str | None = None
    run_id: str | None = None
    persisted_graph = nomination.support_bundle.graph
    if persisted_graph is not None:
        run_root = research_root.parent
        if (
            research_root.name != "research"
            or run_root.name != persisted_graph.run_id
            or run_root.parent.name != "runs"
            or run_root.parent.parent.name != ".matek"
        ):
            raise StageValidationError(
                "Named counterexample support is outside a canonical .matek run"
            )
        project_root = run_root.parent.parent.parent
        if graph_snapshot is not None:
            if (
                graph_snapshot.graph_name != persisted_graph.graph_name
                or nomination.main_target_node_id not in {None, graph_snapshot.main_target_id}
            ):
                raise StageValidationError("Counterexample graph snapshot has the wrong identity")
            graph = graph_snapshot
        else:
            try:
                from ..knowledge_graph.service import KnowledgeGraph

                graph = KnowledgeGraph(project_root, persisted_graph.graph_name)
            except (OSError, ValueError) as exc:
                raise StageValidationError(
                    f"Counterexample support graph cannot be loaded: {exc}"
                ) from exc
        graph_problem_id = persisted_graph.problem_id
        run_id = persisted_graph.run_id
    expected_support = build_counterexample_support_bundle(
        assignment_id=nomination.assignment_id,
        root_result=result,
        results=report.results,
        unresolved_obligations=report.unresolved_obligations,
        artifact_manifest=report.artifact_manifest,
        run_root=run_root,
        computation_evidence_path=computation_path,
        knowledge_graph=graph,
        graph_problem_id=graph_problem_id,
        run_id=run_id,
    )
    if expected_support.graph is not None and persisted_graph is not None:
        current_graph = expected_support.graph.model_copy(
            update={"source_revision": persisted_graph.source_revision}
        )
        expected_support = _make_support_bundle(
            root_result_key=expected_support.root_result_key,
            result_keys=expected_support.result_keys,
            result_sha256=expected_support.result_sha256,
            dependency_node_ids=expected_support.dependency_node_ids,
            artifact_declaration_sha256s=(expected_support.artifact_declaration_sha256s),
            computation=expected_support.computation,
            graph=current_graph,
        )
    if expected_support != nomination.support_bundle:
        error_type = (
            CounterexampleSupportInvalidated
            if persisted_graph is not None
            else StageValidationError
        )
        raise error_type("Counterexample support closure differs from its current trusted evidence")
    expected = build_exact_counterexample_nomination(
        assignment_id=nomination.assignment_id,
        result=result,
        frozen_target_statement=nomination.frozen_target_statement,
        worker_report_path=nomination.worker_report_path,
        worker_report_sha256=nomination.worker_report_sha256,
        main_target_node_id=nomination.main_target_node_id,
        support_bundle=expected_support,
    )
    if expected != nomination:
        raise StageValidationError("Counterexample nomination differs from its worker evidence")


def _request_artifacts(
    packet: BlindExactCounterexamplePacket,
    policy: CounterexampleAuditPolicyArtifact,
    policy_artifact_sha256: str,
) -> tuple[
    dict[CounterexampleAuditRole, ModelRequest],
    dict[CounterexampleAuditRole, CounterexampleAuditRequestArtifact],
]:
    requests = {
        role: _request(
            role,
            packet,
            policy.role_instructions[role.value],
            policy.settings,
            policy.role_context_ids[role.value],
        )
        for role in CounterexampleAuditRole
    }
    artifacts = {
        role: CounterexampleAuditRequestArtifact(
            audit_id=packet.audit_id,
            audit_role=role,
            policy_version=policy.policy_version,
            policy_artifact_sha256=policy_artifact_sha256,
            execution_context_id=policy.role_context_ids[role.value],
            instructions=requests[role].instructions,
            input_text=requests[role].input_text,
            settings=requests[role].settings,
            model_request_sha256=model_request_cache_key(
                requests[role],
                CounterexampleAuditResponse,
                stage="counterexample_audit",
                cache_namespace=role.value,
            ),
        )
        for role in CounterexampleAuditRole
    }
    return requests, artifacts


def _validate_response(
    evidence: CounterexampleAuditEvidence,
    *,
    role: CounterexampleAuditRole,
    packet: BlindExactCounterexamplePacket,
    nomination_sha256: str,
    request_artifact_sha256: str,
    request: CounterexampleAuditRequestArtifact,
) -> None:
    response = evidence.response
    if evidence.audit_id != packet.audit_id or evidence.audit_role is not role:
        raise StageValidationError(f"Committed {role.value} evidence has the wrong identity")
    if evidence.nomination_sha256 != nomination_sha256:
        raise StageValidationError(f"Committed {role.value} evidence targets another nomination")
    if evidence.request_artifact_sha256 != request_artifact_sha256:
        raise StageValidationError(f"Committed {role.value} evidence targets another request")
    if evidence.model_request_sha256 != request.model_request_sha256:
        raise StageValidationError(f"Committed {role.value} request identity changed")
    if evidence.execution_context_id != request.execution_context_id:
        raise StageValidationError(f"Committed {role.value} execution context changed")
    if evidence.response_sha256 != sha256_bytes(canonical_json_bytes(response)):
        raise StageValidationError(f"Committed {role.value} response hash is invalid")


def _deterministic_gate(
    *,
    nomination: ExactCounterexampleNomination,
    nomination_sha256: str,
    packet: BlindExactCounterexamplePacket,
    policy_artifact_sha256: str,
    request_hashes: Mapping[CounterexampleAuditRole, str],
    execution_context_ids: Mapping[CounterexampleAuditRole, str],
    evidence: Mapping[CounterexampleAuditRole, CounterexampleAuditEvidence],
    evidence_hashes: Mapping[CounterexampleAuditRole, str],
    execution_obligations: Mapping[CounterexampleAuditRole, str],
    decided_at: datetime,
) -> CounterexampleAuditGate:
    missing = [role for role in CounterexampleAuditRole if role not in evidence]
    obligations = [
        execution_obligations.get(
            role,
            f"Run the missing independent {role.value} audit against the frozen certificate.",
        )
        for role in missing
    ]
    response_ids = [item.response_id for item in evidence.values()]
    independence_failed = len(response_ids) != len(set(response_ids))
    if independence_failed:
        obligations.append(
            "Repeat verifier and hostile-falsifier audits in distinct contexts; response IDs "
            "collided."
        )
    context_ids = list(execution_context_ids.values())
    if len(context_ids) != len(set(context_ids)):
        independence_failed = True
        obligations.append(
            "Repeat verifier and hostile-falsifier audits in distinct role execution contexts."
        )
    provider_sessions = [
        item.provider_session_id
        for item in evidence.values()
        if item.provider_session_id is not None
    ]
    if len(provider_sessions) != len(set(provider_sessions)):
        independence_failed = True
        obligations.append(
            "Repeat verifier and hostile-falsifier audits in distinct provider sessions."
        )
    failed = independence_failed
    for role in CounterexampleAuditRole:
        committed = evidence.get(role)
        if committed is None:
            continue
        response = committed.response
        if response.audit_role is not role:
            failed = True
            obligations.append(
                f"Committed {role.value} response declared the wrong independent audit role."
            )
        if response.audit_id != packet.audit_id:
            failed = True
            obligations.append(
                f"Committed {role.value} response belongs to another audit identity."
            )
        if response.target_statement_sha256 != packet.target_statement_sha256:
            failed = True
            obligations.append(f"Committed {role.value} response targets another exact statement.")
        obligations.extend(response.obligations)
        if response.decision is CounterexampleAuditDecision.FAIL:
            failed = True
        elif response.decision is CounterexampleAuditDecision.BLOCKED:
            # A parsed evidence judgment is complete execution, not a missing-role condition.
            # It closes this immutable audit attempt as failed so resume cannot loop forever.
            failed = True
    obligations = _unique(obligations)
    if failed:
        status = CounterexampleAuditGateStatus.AUDIT_FAILED
    elif missing:
        status = CounterexampleAuditGateStatus.BLOCKED
    elif all(
        evidence[role].response.decision is CounterexampleAuditDecision.PASS
        for role in CounterexampleAuditRole
    ):
        status = CounterexampleAuditGateStatus.REFUTATION_VERIFIED
    else:  # pragma: no cover - exhaustive enum handling
        status = CounterexampleAuditGateStatus.BLOCKED
        obligations.append("Complete both independent exact-counterexample audits.")

    verified: VerifiedExactCounterexample | None = None
    if status is CounterexampleAuditGateStatus.REFUTATION_VERIFIED:
        verified = VerifiedExactCounterexample(
            exact_statement=nomination.exact_statement,
            target_statement_sha256=packet.target_statement_sha256,
            certificate_sha256=packet.certificate_sha256,
            nomination_sha256=nomination_sha256,
            verifier_evidence_sha256=evidence_hashes[CounterexampleAuditRole.VERIFIER],
            falsifier_evidence_sha256=evidence_hashes[CounterexampleAuditRole.FALSIFIER],
        )
    return CounterexampleAuditGate(
        audit_id=nomination.audit_id,
        decided_at=decided_at,
        status=status,
        nomination_sha256=nomination_sha256,
        target_statement_sha256=packet.target_statement_sha256,
        policy_artifact_sha256=policy_artifact_sha256,
        request_artifact_sha256={
            role.value: request_hashes[role] for role in CounterexampleAuditRole
        },
        response_evidence_sha256={
            role.value: evidence_hashes[role]
            for role in CounterexampleAuditRole
            if role in evidence_hashes
        },
        response_ids={
            role.value: evidence[role].response_id
            for role in CounterexampleAuditRole
            if role in evidence
        },
        execution_context_ids={
            role.value: execution_context_ids[role] for role in CounterexampleAuditRole
        },
        provider_session_ids={
            role.value: str(evidence[role].provider_session_id)
            for role in CounterexampleAuditRole
            if role in evidence and evidence[role].provider_session_id is not None
        },
        execution_obligations={
            role.value: obligation for role, obligation in sorted(execution_obligations.items())
        },
        missing_roles=missing,
        obligations=obligations,
        verified_refutation=verified,
    )


def verify_persisted_counterexample_audit(
    nomination_path: Path,
    gate_path: Path,
    *,
    expected_target_statement: str | None = None,
    expected_instructions: Mapping[CounterexampleAuditRole, str] | None = None,
    graph_snapshot: CounterexampleGraphReadSnapshot | None = None,
    allow_invalidated_graph_support: bool = False,
) -> tuple[ExactCounterexampleNomination, CounterexampleAuditGate]:
    """Recompute a complete persisted gate without making a model call."""

    nomination_file = Path(os.path.abspath(nomination_path))
    gate_file = Path(os.path.abspath(gate_path))
    root = gate_file.parent
    if gate_file.name != "gate.json" or nomination_file != root / "nomination.json":
        raise StageValidationError("Counterexample nomination and gate are not canonical siblings")
    loaded_nomination = _load(nomination_file, ExactCounterexampleNomination)
    assert isinstance(loaded_nomination, ExactCounterexampleNomination)
    nomination = loaded_nomination
    if root.name != nomination.audit_id:
        raise StageValidationError("Counterexample audit directory identity changed")
    if expected_target_statement is not None and normalize_exact_statement(
        nomination.frozen_target_statement
    ) != normalize_exact_statement(expected_target_statement):
        raise StageValidationError("Counterexample audit targets another frozen theorem")
    try:
        _validate_worker_binding(nomination, audit_root=root, graph_snapshot=graph_snapshot)
    except CounterexampleSupportInvalidated:
        if not allow_invalidated_graph_support:
            raise
    nomination_sha = sha256_bytes(read_regular_bytes(nomination_file))
    packet = _packet(nomination)
    policy_path = root / "policy.json"
    loaded_policy = _load(policy_path, CounterexampleAuditPolicyArtifact)
    assert isinstance(loaded_policy, CounterexampleAuditPolicyArtifact)
    policy = loaded_policy
    if policy.audit_id != nomination.audit_id:
        raise StageValidationError("Counterexample audit policy belongs to another nomination")
    _authenticate_official_policy(policy)
    if expected_instructions is not None and any(
        expected_instructions[role] != policy.role_instructions[role.value]
        for role in CounterexampleAuditRole
    ):
        raise StageValidationError("Expected counterexample policy differs from frozen policy")
    policy_sha = sha256_bytes(read_regular_bytes(policy_path))

    request_artifacts: dict[CounterexampleAuditRole, CounterexampleAuditRequestArtifact] = {}
    request_hashes: dict[CounterexampleAuditRole, str] = {}
    evidence: dict[CounterexampleAuditRole, CounterexampleAuditEvidence] = {}
    evidence_hashes: dict[CounterexampleAuditRole, str] = {}
    for role in CounterexampleAuditRole:
        request_path = root / "requests" / f"{role.value}.json"
        loaded_request = _load(request_path, CounterexampleAuditRequestArtifact)
        assert isinstance(loaded_request, CounterexampleAuditRequestArtifact)
        if loaded_request.audit_id != nomination.audit_id or loaded_request.audit_role is not role:
            raise StageValidationError(f"Persisted {role.value} request has the wrong identity")
        rebuilt_request = ModelRequest(
            instructions=loaded_request.instructions,
            input_text=loaded_request.input_text,
            settings=loaded_request.settings,
        )
        if (
            loaded_request.policy_version != policy.policy_version
            or loaded_request.policy_artifact_sha256 != policy_sha
            or loaded_request.execution_context_id != policy.role_context_ids[role.value]
            or loaded_request.instructions != policy.role_instructions[role.value]
            or loaded_request.settings != policy.settings
        ):
            raise StageValidationError(
                f"Persisted {role.value} request is not bound to the official audit policy"
            )
        expected_input = _request(
            role,
            packet,
            policy.role_instructions[role.value],
            policy.settings,
            policy.role_context_ids[role.value],
        )
        if rebuilt_request != expected_input or loaded_request.model_request_sha256 != (
            model_request_cache_key(
                rebuilt_request,
                CounterexampleAuditResponse,
                stage="counterexample_audit",
                cache_namespace=role.value,
            )
        ):
            raise StageValidationError(f"Persisted {role.value} request changed")
        request_artifacts[role] = loaded_request
        request_hashes[role] = sha256_bytes(read_regular_bytes(request_path))
        response_path = root / "responses" / f"{role.value}.json"
        if not response_path.exists():
            continue
        loaded_evidence = _load(response_path, CounterexampleAuditEvidence)
        assert isinstance(loaded_evidence, CounterexampleAuditEvidence)
        _validate_response(
            loaded_evidence,
            role=role,
            packet=packet,
            nomination_sha256=nomination_sha,
            request_artifact_sha256=request_hashes[role],
            request=loaded_request,
        )
        evidence[role] = loaded_evidence
        evidence_hashes[role] = sha256_bytes(read_regular_bytes(response_path))

    loaded_gate = _load(gate_file, CounterexampleAuditGate)
    assert isinstance(loaded_gate, CounterexampleAuditGate)
    gate = loaded_gate
    gate_evidence = evidence
    gate_evidence_hashes = evidence_hashes
    if gate.status is CounterexampleAuditGateStatus.BLOCKED:
        # A role response is committed before the retryable gate is overwritten.  A process
        # crash in that narrow window leaves a valid immutable evidence superset beside the old
        # BLOCKED gate.  Authenticate the old gate against exactly the roles/hashes it named;
        # the caller may then deterministically fold the additional evidence into a new gate.
        named_roles = {CounterexampleAuditRole(role) for role in gate.response_evidence_sha256}
        if any(role not in evidence for role in named_roles) or any(
            evidence_hashes[role] != gate.response_evidence_sha256[role.value]
            for role in named_roles
        ):
            raise StageValidationError(
                "Committed retryable counterexample gate lost or changed its named evidence"
            )
        gate_evidence = {role: evidence[role] for role in named_roles}
        gate_evidence_hashes = {role: evidence_hashes[role] for role in named_roles}
    recomputed = _deterministic_gate(
        nomination=nomination,
        nomination_sha256=nomination_sha,
        packet=packet,
        policy_artifact_sha256=policy_sha,
        request_hashes=request_hashes,
        execution_context_ids={
            role: request_artifacts[role].execution_context_id for role in CounterexampleAuditRole
        },
        evidence=gate_evidence,
        evidence_hashes=gate_evidence_hashes,
        execution_obligations={
            CounterexampleAuditRole(role): obligation
            for role, obligation in gate.execution_obligations.items()
        },
        decided_at=gate.decided_at,
    )
    if gate.model_dump(mode="json") != recomputed.model_dump(mode="json"):
        raise StageValidationError("Committed counterexample gate does not match its evidence")
    return nomination, gate


def persisted_counterexample_audit_response_bindings(
    nomination_path: Path,
    gate_path: Path,
    *,
    expected_target_statement: str | None = None,
    allow_invalidated_graph_support: bool = False,
) -> dict[str, tuple[ModelRequest, str]]:
    """Return authenticated role requests and responses for scheduler recovery."""

    _, gate = verify_persisted_counterexample_audit(
        nomination_path,
        gate_path,
        expected_target_statement=expected_target_statement,
        allow_invalidated_graph_support=allow_invalidated_graph_support,
    )
    root = Path(os.path.abspath(gate_path)).parent
    bindings: dict[str, tuple[ModelRequest, str]] = {}
    for role in CounterexampleAuditRole:
        response_id = gate.response_ids.get(role.value)
        if response_id is None:
            continue
        loaded = _load(
            root / "requests" / f"{role.value}.json",
            CounterexampleAuditRequestArtifact,
        )
        assert isinstance(loaded, CounterexampleAuditRequestArtifact)
        bindings[role.value] = (
            ModelRequest(
                instructions=loaded.instructions,
                input_text=loaded.input_text,
                settings=loaded.settings,
            ),
            response_id,
        )
    return bindings


async def run_counterexample_audit(
    nomination: ExactCounterexampleNomination,
    audit_dir: Path,
    *,
    verifier_client: ModelClient,
    falsifier_client: ModelClient,
    settings: ModelSettings,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    verifier_instructions: str | None = None,
    falsifier_instructions: str | None = None,
) -> CounterexampleAuditGate:
    """Run or resume both independent roles against one immutable exact certificate."""

    root = ensure_stage_directory(Path(os.path.abspath(audit_dir)))
    requests_dir = ensure_stage_directory(root / "requests")
    responses_dir = ensure_stage_directory(root / "responses")
    nomination_path = root / "nomination.json"
    policy_path = root / "policy.json"
    gate_path = root / "gate.json"
    _write_immutable(nomination_path, nomination)
    _validate_worker_binding(nomination, audit_root=root)
    nomination_sha = sha256_bytes(read_regular_bytes(nomination_path))
    packet = _packet(nomination)
    if policy_path.exists():
        loaded_policy = _load(policy_path, CounterexampleAuditPolicyArtifact)
        assert isinstance(loaded_policy, CounterexampleAuditPolicyArtifact)
        policy = loaded_policy
        if policy.audit_id != nomination.audit_id:
            raise StageValidationError("Counterexample audit policy belongs to another nomination")
        _authenticate_official_policy(policy)
    else:
        policy = _new_official_policy(
            audit_id=nomination.audit_id,
            settings=settings,
            supplied_instructions={
                CounterexampleAuditRole.VERIFIER: verifier_instructions,
                CounterexampleAuditRole.FALSIFIER: falsifier_instructions,
            },
        )
        _write_immutable(policy_path, policy)
    policy_sha = sha256_bytes(read_regular_bytes(policy_path))
    requests, expected_request_artifacts = _request_artifacts(packet, policy, policy_sha)
    request_artifacts: dict[CounterexampleAuditRole, CounterexampleAuditRequestArtifact] = {}
    request_hashes: dict[CounterexampleAuditRole, str] = {}
    for role in CounterexampleAuditRole:
        path = _write_immutable(
            requests_dir / f"{role.value}.json", expected_request_artifacts[role]
        )
        loaded = _load(path, CounterexampleAuditRequestArtifact)
        assert isinstance(loaded, CounterexampleAuditRequestArtifact)
        if loaded != expected_request_artifacts[role]:
            raise StageValidationError(f"Persisted {role.value} request differs from policy")
        request_artifacts[role] = loaded
        request_hashes[role] = sha256_bytes(read_regular_bytes(path))

    evidence: dict[CounterexampleAuditRole, CounterexampleAuditEvidence] = {}
    evidence_hashes: dict[CounterexampleAuditRole, str] = {}
    response_paths = {
        role: responses_dir / f"{role.value}.json" for role in CounterexampleAuditRole
    }
    for role, path in response_paths.items():
        if not path.exists():
            continue
        loaded = _load(path, CounterexampleAuditEvidence)
        assert isinstance(loaded, CounterexampleAuditEvidence)
        _validate_response(
            loaded,
            role=role,
            packet=packet,
            nomination_sha256=nomination_sha,
            request_artifact_sha256=request_hashes[role],
            request=request_artifacts[role],
        )
        evidence[role] = loaded
        evidence_hashes[role] = sha256_bytes(read_regular_bytes(path))

    if gate_path.exists():
        persisted_gate = verify_persisted_counterexample_audit(
            nomination_path,
            gate_path,
            expected_target_statement=nomination.frozen_target_statement,
        )[1]
        if persisted_gate.status is not CounterexampleAuditGateStatus.BLOCKED:
            return persisted_gate

    clients = {
        CounterexampleAuditRole.VERIFIER: verifier_client,
        CounterexampleAuditRole.FALSIFIER: falsifier_client,
    }
    missing = [role for role in CounterexampleAuditRole if role not in evidence]
    tasks = {
        asyncio.create_task(
            clients[role].generate_structured(requests[role], CounterexampleAuditResponse)
        ): role
        for role in missing
    }
    execution_obligations: dict[CounterexampleAuditRole, str] = {}
    pending = set(tasks)
    while pending:
        completed, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for task in completed:
            role = tasks[task]
            try:
                result: ModelResult[CounterexampleAuditResponse] = task.result()
                response = result.parsed
                if not result.response_id.strip():
                    raise StageValidationError(f"{role.value} returned no response identity")
                if not isinstance(response, CounterexampleAuditResponse):
                    raise StageValidationError(f"{role.value} returned the wrong response type")
                serialized = canonical_json_bytes(response).decode("utf-8")
                if redact_text(serialized) != serialized:
                    raise StageValidationError(f"{role.value} response contained secret material")
                provider_session_id = provider_session_id_from_metadata(result.request_metadata)
                evidence_record = CounterexampleAuditEvidence(
                    audit_id=nomination.audit_id,
                    audit_role=role,
                    completed_at=clock(),
                    nomination_sha256=nomination_sha,
                    request_artifact_sha256=request_hashes[role],
                    model_request_sha256=request_artifacts[role].model_request_sha256,
                    execution_context_id=request_artifacts[role].execution_context_id,
                    context_mode=(
                        CounterexampleAuditContextMode.PROVIDER_SESSION
                        if provider_session_id is not None
                        else CounterexampleAuditContextMode.STATELESS_ROLE_REQUEST
                    ),
                    provider_session_id=provider_session_id,
                    response_id=result.response_id,
                    response_sha256=sha256_bytes(canonical_json_bytes(response)),
                    response=response,
                )
                _validate_response(
                    evidence_record,
                    role=role,
                    packet=packet,
                    nomination_sha256=nomination_sha,
                    request_artifact_sha256=request_hashes[role],
                    request=request_artifacts[role],
                )
                path = _write_immutable(response_paths[role], evidence_record)
                evidence[role] = evidence_record
                evidence_hashes[role] = sha256_bytes(read_regular_bytes(path))
            except asyncio.CancelledError:
                for pending_task in pending:
                    pending_task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                raise
            except BaseException as exc:
                detail = redact_text(str(exc)).replace("\n", " ").strip()[:400]
                execution_obligations[role] = (
                    f"Retry the missing independent {role.value} audit"
                    + (f" ({type(exc).__name__}: {detail})." if detail else ".")
                )

    gate = _deterministic_gate(
        nomination=nomination,
        nomination_sha256=nomination_sha,
        packet=packet,
        policy_artifact_sha256=policy_sha,
        request_hashes=request_hashes,
        execution_context_ids={
            role: request_artifacts[role].execution_context_id for role in CounterexampleAuditRole
        },
        evidence=evidence,
        evidence_hashes=evidence_hashes,
        execution_obligations=execution_obligations,
        decided_at=clock(),
    )
    _write_gate(gate_path, gate)
    return gate


__all__ = [
    "COUNTEREXAMPLE_AUDIT_ROLES",
    "BlindExactCounterexamplePacket",
    "CounterexampleAuditContextMode",
    "CounterexampleAuditDecision",
    "CounterexampleAuditEvidence",
    "CounterexampleAuditGate",
    "CounterexampleAuditGateStatus",
    "CounterexampleAuditPolicyArtifact",
    "CounterexampleAuditRequestArtifact",
    "CounterexampleAuditResponse",
    "CounterexampleAuditRole",
    "CounterexampleComputationSupport",
    "CounterexampleGraphReadSnapshot",
    "CounterexampleGraphSupport",
    "CounterexampleSupportBundle",
    "CounterexampleSupportInvalidated",
    "ExactCounterexampleNomination",
    "VerifiedExactCounterexample",
    "build_counterexample_support_bundle",
    "build_exact_counterexample_nomination",
    "persisted_counterexample_audit_response_bindings",
    "run_counterexample_audit",
    "verify_persisted_counterexample_audit",
]
