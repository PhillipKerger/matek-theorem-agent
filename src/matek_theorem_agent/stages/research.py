from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from ..budget import BudgetExceeded
from ..config import ModelSettings
from ..coordinator_context import (
    COORDINATOR_PAYLOAD_SCHEMA_VERSION,
    CoordinatorArtifactReference,
    CoordinatorContextBudgetExhausted,
    CoordinatorContextBuilder,
    CoordinatorContextManifest,
    CoordinatorEvidenceItem,
    graph_node_typed_digest,
    rank_graph_evidence,
    serialize_coordinator_payload,
)
from ..execution.base import ExecutionBackend
from ..failures import classify_failure, recovery_obligations
from ..knowledge_graph import (
    EpistemicStatus,
    GraphMergeResult,
    GraphNode,
    GraphValidationError,
    KnowledgeGraph,
    NodeType,
    RelationType,
    WorkflowStatus,
)
from ..knowledge_graph.admission import (
    admission_payload_sha256,
    node_has_scientific_admission_binding,
)
from ..knowledge_graph.ledger import (
    ObligationStatus,
    logical_version,
    project_markdown_ledger,
    trusted_claim_ids,
)
from ..knowledge_graph.markdown import exact_statement as graph_exact_statement
from ..models import FailureCategory
from ..openai_client import (
    ModelClient,
    ModelInputTooLargeError,
    ModelRequest,
    ModelResult,
    model_request_cache_key,
)
from ..progress import Ascension, ProgressReporter, no_progress
from ..redaction import redact_text
from ..scientific import (
    BranchOutcome,
    ScientificArtifactDeclaration,
    ScientificObligationDeclaration,
    ScientificResult,
    ScientificResultDisposition,
    ScientificResultKind,
    ScientificScope,
    normalize_exact_statement,
    transitive_result_dependency_keys,
    validate_result_dependency_dag,
)
from ..source_identifiers import tool_metadata_source_identifiers
from ..source_provenance import IdentifierVerifier, SourceEvidenceClaim, SourceVerificationReport
from .common import (
    ArtifactManifest,
    CallManifest,
    StageValidationError,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    build_artifact_manifest,
    ensure_stage_directory,
    project_resource,
    read_regular_bytes,
    read_regular_text,
    sha256_file,
    sha256_json,
    sha256_text,
)
from .compile_prompt import (
    CompiledProblem,
    PromptCompilationResult,
    SourceLedgerEntry,
    verify_source_ledger,
)
from .computation_artifacts import (
    ComputationArtifactStore,
    ComputationReplayIsolation,
    ComputationReplayStatus,
    WorkerComputationEvidence,
    verify_persisted_computation_evidence,
)
from .counterexample_audit import (
    CounterexampleAuditGate,
    CounterexampleAuditGateStatus,
    CounterexampleAuditRequestArtifact,
    CounterexampleAuditResponse,
    CounterexampleAuditRole,
    CounterexampleSupportInvalidated,
    ExactCounterexampleNomination,
    build_counterexample_support_bundle,
    build_exact_counterexample_nomination,
    persisted_counterexample_audit_response_bindings,
    run_counterexample_audit,
    verify_persisted_counterexample_audit,
)
from .lemma_audit import (
    LemmaAuditGate,
    LemmaAuditGateStatus,
    LemmaAuditResponse,
    LemmaNomination,
    persisted_lemma_audit_response_bindings,
    run_lemma_audit,
    verify_persisted_lemma_audit,
)
from .lemma_nomination import nominate_intermediate_lemmas
from .scientific_phase import (
    AssignmentDisposition,
    DuplicateDisposition,
    ScientificPhase,
    ScientificPhasePolicy,
    ScientificPhaseState,
    ScientificProgressSnapshot,
    ScientificRole,
    ScientificTaskPlan,
    admit_assignment,
    focused_frontier_obligation,
    load_scientific_phase_state,
    next_complementary_role,
    record_scientific_progress,
    semantic_similarity,
    write_scientific_phase_state,
)


class WorkerStatus(StrEnum):
    PROGRESS = "progress"
    BLOCKED = "blocked"
    REFUTED = "refuted"
    CANDIDATE_COMPLETE = "candidate_complete"


class AuditDecision(StrEnum):
    PASS = "pass"
    REPAIRABLE = "repairable"
    FAIL = "fail"
    PARTIAL_ONLY = "partial_only"


class FinalJudgeDecision(StrEnum):
    ACCEPTED = "accepted_for_manuscript"
    REPAIRABLE = "repairable_and_return_to_research"
    REJECTED = "rejected"
    PARTIAL = "partial_result_only"


class ResearchOutcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    PARTIAL = "partial"
    BUDGET_EXHAUSTED = "budget_exhausted"
    PAUSED_RETRIABLE = "paused_retriable"


def exact_target_policy() -> dict[str, object]:
    """Return the immutable-by-convention terminal trust policy sent to every research role."""

    return {
        "terminal_reductions_allowed": False,
        "intermediate_reductions_may_be_recorded": True,
        "acceptance_requires_exact_claim_contract": True,
        "scientific_no_progress_stop_allowed": False,
        "forced_stop_conditions": [
            "configured resource or provider limit",
            "verified exact counterexample or disproof",
            "integrity or security failure",
        ],
    }


class TargetObligationVersion(BaseModel):
    """Portable typed binding from a graph target ID to its semantic contract digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    obligation_id: str
    logical_version: str

    @field_validator("obligation_id")
    @classmethod
    def obligation_id_is_stable(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}-[A-Z0-9]{8,64}", normalized):
            raise ValueError("target obligation versions require stable graph IDs")
        return normalized

    @field_validator("logical_version")
    @classmethod
    def logical_version_is_sha256(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("target obligation versions must be SHA-256 digests")
        return normalized


class ResearchAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    approach_family: str
    task: str
    expected_output: str
    inputs: list[str] = Field(default_factory=list)
    target_node_ids: list[str] = Field(default_factory=list)
    stopping_condition: str = "Return concrete formal content or an exact obstruction."
    scientific_phase: ScientificPhase = ScientificPhase.EXPLORE
    scientific_role: ScientificRole = ScientificRole.EXPLORER
    target_obligation_ids: list[str] = Field(default_factory=list)
    target_obligation_versions: list[TargetObligationVersion] = Field(default_factory=list)
    mechanism_delta: str = ""
    audited_premise_ids: list[str] = Field(default_factory=list)

    @field_validator("id", "approach_family", "task", "expected_output")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()

    @field_validator("id")
    @classmethod
    def identifier_is_safe_for_artifacts(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
            raise ValueError(
                "must use 1-128 portable characters: letters, digits, dot, underscore, or dash"
            )
        return value

    @field_validator("target_obligation_ids", "audited_premise_ids")
    @classmethod
    def scientific_ids_are_unique(cls, value: list[str]) -> list[str]:
        normalized = [item.strip().upper() for item in value]
        if any(not re.fullmatch(r"[A-Z]{3}-[A-Z0-9]{8,64}", item) for item in normalized):
            raise ValueError("scientific frontier references must be stable graph IDs")
        return list(dict.fromkeys(normalized))

    @field_validator("mechanism_delta")
    @classmethod
    def mechanism_delta_is_normalized(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def obligation_versions_cover_targets_when_present(self) -> ResearchAssignment:
        version_ids = [item.obligation_id for item in self.target_obligation_versions]
        if len(version_ids) != len(set(version_ids)):
            raise ValueError("target obligation version IDs must be unique")
        if version_ids and set(version_ids) != set(self.target_obligation_ids):
            raise ValueError("target obligation versions must exactly cover target_obligation_ids")
        return self

    @property
    def target_obligation_version_map(self) -> dict[str, str]:
        return {
            item.obligation_id: item.logical_version for item in self.target_obligation_versions
        }


class ResearchRoundPlan(BaseModel):
    """Legacy fixed-round plan retained for completed-run compatibility.

    New research runs use :class:`ResearchCoordinatorDecision`; keeping this model lets
    MATEK read old ``result.json`` and schema artifacts without silently reinterpreting
    their scheduling semantics.
    """

    model_config = ConfigDict(extra="forbid")

    round_id: int = Field(ge=1)
    assignments: list[ResearchAssignment]
    rationale: str
    candidate_packaging_recommended: bool = False
    retire_assignment_ids: list[str] = Field(default_factory=list)
    redirect_assignment_ids: list[str] = Field(default_factory=list)
    claims_requiring_counterexample_search: list[str] = Field(default_factory=list)
    lemmas_requiring_proof_completion: list[str] = Field(default_factory=list)
    stop_recommended: bool = False
    stop_reason: str | None = None


class ResearchCoordinatorDecision(BaseModel):
    """One event-indexed decision from the continuous logical coordinator."""

    model_config = ConfigDict(extra="forbid")

    decision_id: int = Field(ge=1)
    after_event_sequence: int = Field(ge=0)
    assignments: list[ResearchAssignment]
    rationale: str
    retire_assignment_ids: list[str] = Field(default_factory=list)
    redirect_assignment_ids: list[str] = Field(default_factory=list)
    claims_requiring_counterexample_search: list[str] = Field(default_factory=list)
    lemmas_requiring_proof_completion: list[str] = Field(default_factory=list)
    candidate_packaging_recommended: bool = False
    candidate_report_ids: list[str] = Field(default_factory=list)
    stop_recommended: bool = False
    stop_reason: str | None = None
    stop_category: Literal["scientific", "refuted", "budget"] = "scientific"
    requested_artifact_ids: list[str] = Field(default_factory=list, max_length=32)
    requested_graph_node_ids: list[str] = Field(default_factory=list, max_length=32)
    supporting_evidence_ids: list[str] = Field(default_factory=list, max_length=64)
    resolved_contradiction_node_ids: list[str] = Field(default_factory=list, max_length=32)

    @field_validator(
        "requested_artifact_ids",
        "requested_graph_node_ids",
        "supporting_evidence_ids",
        "resolved_contradiction_node_ids",
    )
    @classmethod
    def requested_evidence_ids_are_unique_and_nonblank(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("coordinator evidence IDs must not be blank")
        if len(normalized) != len(set(normalized)):
            raise ValueError("coordinator evidence IDs must be unique")
        return normalized


class AssignmentLifecycle(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    RETIRED = "retired"
    CANCELLED = "cancelled"


class IntermediateLemmaAuditRecord(BaseModel):
    """Durable binding from one admitted result to its independent audit gate."""

    model_config = ConfigDict(extra="forbid")

    result_local_key: str
    nomination_id: str
    graph_revision: str
    target_obligation_ids: list[str] = Field(default_factory=list)
    target_obligation_versions: dict[str, str] = Field(default_factory=dict)
    gate_status: LemmaAuditGateStatus
    nomination_path: str
    nomination_sha256: str
    gate_path: str
    gate_sha256: str
    graph_recorded: bool = False

    @field_validator(
        "result_local_key",
        "nomination_id",
        "graph_revision",
        "nomination_path",
        "gate_path",
    )
    @classmethod
    def audit_identity_is_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("intermediate lemma audit identity must not be blank")
        return normalized

    @field_validator("nomination_sha256", "gate_sha256")
    @classmethod
    def gate_digest_is_sha256(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("intermediate lemma audit digest must be SHA-256")
        return normalized

    @field_validator("target_obligation_ids")
    @classmethod
    def target_ids_are_stable_and_unique(cls, value: list[str]) -> list[str]:
        normalized = [item.strip().upper() for item in value]
        if any(not re.fullmatch(r"[A-Z]{3}-[A-Z0-9]{8,64}", item) for item in normalized):
            raise ValueError("intermediate lemma audit targets must be stable graph IDs")
        if len(normalized) != len(set(normalized)):
            raise ValueError("intermediate lemma audit targets must be unique")
        return normalized

    @field_validator("target_obligation_versions")
    @classmethod
    def target_versions_are_stable(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for raw_id, raw_version in value.items():
            target_id = raw_id.strip().upper()
            version = raw_version.strip().casefold()
            if not re.fullmatch(r"[A-Z]{3}-[A-Z0-9]{8,64}", target_id):
                raise ValueError("intermediate lemma audit version keys must be stable graph IDs")
            if not re.fullmatch(r"[0-9a-f]{64}", version):
                raise ValueError("intermediate lemma audit versions must be SHA-256 digests")
            normalized[target_id] = version
        return dict(sorted(normalized.items()))

    @model_validator(mode="after")
    def target_versions_cover_targets_when_present(self) -> IntermediateLemmaAuditRecord:
        if self.target_obligation_versions and set(self.target_obligation_versions) != set(
            self.target_obligation_ids
        ):
            raise ValueError(
                "intermediate lemma audit versions must exactly cover target obligation IDs"
            )
        return self


class ExactCounterexampleAuditRecord(BaseModel):
    """Durable binding from one typed main counterexample to its independent gate."""

    model_config = ConfigDict(extra="forbid")

    result_local_key: str
    audit_id: str
    gate_status: CounterexampleAuditGateStatus
    nomination_path: str
    nomination_sha256: str
    gate_path: str
    gate_sha256: str
    graph_recorded: bool = False
    superseded: bool = False
    superseded_reason: str | None = None

    @field_validator(
        "result_local_key",
        "audit_id",
        "nomination_path",
        "gate_path",
    )
    @classmethod
    def audit_identity_is_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("exact-counterexample audit identity must not be blank")
        return normalized

    @field_validator("nomination_sha256", "gate_sha256")
    @classmethod
    def artifact_digest_is_sha256(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("exact-counterexample audit digest must be SHA-256")
        return normalized

    @model_validator(mode="after")
    def supersession_has_a_reason(self) -> ExactCounterexampleAuditRecord:
        if self.superseded != (self.superseded_reason is not None):
            raise ValueError("superseded exact-counterexample audits require one durable reason")
        if self.superseded_reason is not None:
            self.superseded_reason = self.superseded_reason.strip()
            if not self.superseded_reason:
                raise ValueError("superseded exact-counterexample audit reason must not be blank")
        return self


class ResearchAssignmentState(BaseModel):
    """Durable lifecycle record for one logical worker assignment."""

    model_config = ConfigDict(extra="forbid")

    assignment: ResearchAssignment
    admitted_by_decision: int
    scientific_phase_epoch: int = Field(default=0, ge=0)
    exact_target_policy_version: Literal[1] | None = None
    status: AssignmentLifecycle = AssignmentLifecycle.QUEUED
    launched: bool = False
    request_settings: ModelSettings | None = None
    request_key: str | None = None
    response_id: str | None = None
    worker_report_schema_version: Literal[2] | None = None
    raw_report_path: str | None = None
    raw_report_sha256: str | None = None
    report_path: str | None = None
    report_sha256: str | None = None
    completed_event_sequence: int | None = None
    graph_task_id: str | None = None
    graph_revision: str | None = None
    graph_context: dict[str, object] | None = None
    # Assignments created before branch-scoped graph enforcement remain readable.
    # New graph-integrated assignments use version 1 and are validated strictly.
    graph_contract_version: Literal[1] | None = None
    graph_patch_path: str | None = None
    graph_patch_sha256: str | None = None
    computation_evidence_path: str | None = None
    computation_evidence_sha256: str | None = None
    intermediate_lemma_audits: list[IntermediateLemmaAuditRecord] = Field(default_factory=list)
    exact_counterexample_audits: list[ExactCounterexampleAuditRecord] = Field(default_factory=list)
    counterexample_support_rejections: dict[str, str] = Field(default_factory=dict)
    repair_generation: int = Field(default=0, ge=0, le=1)
    execution_attempts: int = Field(default=0, ge=0)

    @field_validator("counterexample_support_rejections")
    @classmethod
    def counterexample_rejections_are_nonblank(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for local_key, detail in sorted(value.items()):
            key = local_key.strip()
            message = detail.strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", key) or not message:
                raise ValueError("counterexample support rejections require stable evidence")
            normalized[key] = message
        return normalized


class ResearchCoordinatorDecisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ResearchCoordinatorDecision
    response_id: str
    request_settings: ModelSettings
    request_path: str
    request_sha256: str
    request_key: str
    context_manifest_path: str | None = None
    context_manifest_sha256: str | None = None

    @field_validator("request_sha256", "request_key", "context_manifest_sha256")
    @classmethod
    def request_hashes_are_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("coordinator request identity must be a SHA-256 digest")
        return value


class PendingCoordinatorRequest(BaseModel):
    """Frozen coordinator activation replayed until its decision is committed."""

    model_config = ConfigDict(extra="forbid")

    decision_id: int = Field(ge=1)
    after_event_sequence: int = Field(ge=0)
    initial: bool
    request_settings: ModelSettings
    request_path: str
    request_sha256: str
    request_payload: dict[str, object]
    context_generation: int = Field(default=1, ge=1)
    context_character_limit: int = Field(default=800_000, gt=0)
    context_manifest_path: str | None = None
    context_manifest_sha256: str | None = None
    headroom_assignment_id: str | None = None
    headroom_worker_request_key: str | None = None

    @field_validator("request_sha256", "context_manifest_sha256")
    @classmethod
    def persisted_hashes_are_sha256(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("pending coordinator hashes must be SHA-256 digests")
        return value

    @model_validator(mode="after")
    def validate_headroom_exchange(self) -> PendingCoordinatorRequest:
        if (self.headroom_assignment_id is None) != (self.headroom_worker_request_key is None):
            raise ValueError("coordinator headroom metadata must be complete")
        if self.headroom_worker_request_key is not None and not re.fullmatch(
            r"[0-9a-f]{64}", self.headroom_worker_request_key
        ):
            raise ValueError("coordinator headroom request identity is invalid")
        if (self.context_manifest_path is None) != (self.context_manifest_sha256 is None):
            raise ValueError("coordinator context manifest metadata must be complete")
        return self


class ExecutionIssue(BaseModel):
    """Durable recoverable failure surfaced to the research coordinator."""

    model_config = ConfigDict(extra="forbid")

    issue_id: str
    category: FailureCategory
    event_kind: str
    message: str
    retryable: bool = True
    assignment_id: str | None = None
    candidate_attempt: str | None = None
    audit_name: str | None = None
    repair_generation: int = Field(default=0, ge=0)
    trace_paths: list[str] = Field(default_factory=list)
    recovery_obligations: list[str] = Field(default_factory=list)

    @field_validator("issue_id", "event_kind", "message")
    @classmethod
    def issue_text_is_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("execution issue identity and message must not be blank")
        return normalized


class CandidateAttemptState(BaseModel):
    """Frozen candidate-gate transaction and its durable outcome."""

    model_config = ConfigDict(extra="forbid")

    attempt_name: str
    report_ids: list[str]
    source: Literal["worker", "coordinator"]
    exact_target_policy_version: Literal[1] | None = None
    computation_gate_version: Literal[1] | None = None
    computation_bindings: list[CandidateComputationBinding] = Field(default_factory=list)
    computation_obligations: list[str] = Field(default_factory=list)
    graph_support_gate_version: Literal[1] | None = None
    graph_support_bindings: list[CandidateGraphSupportBinding] = Field(default_factory=list)
    graph_support_obligations: list[str] = Field(default_factory=list)
    packager_settings: ModelSettings
    audit_settings: ModelSettings
    judge_settings: ModelSettings
    package_input_path: str
    package_input_sha256: str
    package_evidence_sha256: str | None = None
    package_sha256: str | None = None
    source_verification_sha256: str | None = None
    packager_response_id: str | None = None
    audit_sha256: dict[str, str] = Field(default_factory=dict)
    audit_response_ids: dict[str, str] = Field(default_factory=dict)
    mandatory_audits: list[str] = Field(default_factory=list)
    audit_execution_issues: list[ExecutionIssue] = Field(default_factory=list)
    verdict_sha256: str | None = None
    final_judge_response_id: str | None = None
    judge_call_reservation_key: str | None = None
    outcome_ready: bool = False
    outcome_gate: dict[str, object] | None = None
    outcome_obligations: list[str] = Field(default_factory=list)
    outcome_decision: FinalJudgeDecision | None = None
    outcome_failure_kind: Literal["scientific", "budget", "execution", "evidence"] | None = None
    raced_candidate_report_ids: list[str] = Field(default_factory=list)


class SchedulerPhase(StrEnum):
    INITIALIZING = "initializing"
    RUNNING = "running"
    AUDITING = "auditing"
    AWAITING_AUDITS = "awaiting_audits"
    COMPLETE = "complete"


class ResearchSchedulerState(BaseModel):
    """Canonical crash-safe state for the event-driven research actor loop."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    compiled_problem_sha256: str | None = None
    phase: SchedulerPhase = SchedulerPhase.INITIALIZING
    next_event_sequence: int = Field(default=1, ge=1)
    coordinator_ack_event_sequence: int = Field(default=0, ge=0)
    pending_event: dict[str, object] | None = None
    pending_coordinator_request: PendingCoordinatorRequest | None = None
    decisions: list[ResearchCoordinatorDecisionRecord] = Field(default_factory=list)
    assignments: list[ResearchAssignmentState] = Field(default_factory=list)
    repair_obligations: list[str] = Field(default_factory=list)
    candidate_attempts: int = Field(default=0, ge=0)
    failed_candidate_attempts: int = Field(default=0, ge=0)
    active_candidate_attempt: CandidateAttemptState | None = None
    latest_candidate_attempt: CandidateAttemptState | None = None
    latest_candidate_attempt_name: str | None = None
    pending_candidate_report_ids: list[str] = Field(default_factory=list)
    deferred_candidate_report_ids: list[str] = Field(default_factory=list)
    attempted_candidate_report_sets: list[list[str]] = Field(default_factory=list)
    pending_candidate_source: Literal["worker", "coordinator"] | None = None
    stop_reason: str | None = None
    stop_category: Literal["scientific", "refuted", "budget"] | None = None
    final_outcome: ResearchOutcome | None = None
    final_obligations: list[str] = Field(default_factory=list)
    final_strongest_result: str = ""
    final_acceptance_gate: dict[str, object] | None = None
    final_refutation_gate: dict[str, object] | None = None
    final_refutation_audit_id: str | None = None
    model_calls: int = Field(default=0, ge=0)
    model_call_keys: list[str] = Field(default_factory=list)
    model_response_ids_by_call_key: dict[str, str] = Field(default_factory=dict)
    response_ids: list[str] = Field(default_factory=list)
    execution_issues: list[ExecutionIssue] = Field(default_factory=list)
    requested_artifact_ids: list[str] = Field(default_factory=list)
    requested_graph_node_ids: list[str] = Field(default_factory=list)

    def assignment_record(self, assignment_id: str) -> ResearchAssignmentState | None:
        return next(
            (record for record in self.assignments if record.assignment.id == assignment_id),
            None,
        )


class ArchivedResearchWorkerReportV1(BaseModel):
    """The provider-visible worker schema used before deterministic scientific admission.

    This type exists only at the explicit persisted-artifact compatibility boundary. It is
    never passed to a model backend and is not registered as a packaged output schema.
    """

    model_config = ConfigDict(extra="forbid")

    assignment_id: str
    status: WorkerStatus
    formal_results: list[str] = Field(default_factory=list)
    proof_content: str = ""
    exact_gap: str | None = None
    sources: list[SourceLedgerEntry] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    counterexamples: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    mechanism: str | None = None
    graph_patch: object | None = None


_STABLE_SCIENTIFIC_NODE_ID = re.compile(r"\A[A-Z]{3}-[A-Z0-9]{8,64}\Z")


class ResearchWorkerReport(BaseModel):
    """Provider-visible v2 report containing mathematics rather than persistence mutations."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    assignment_id: str
    results: list[ScientificResult] = Field(default_factory=list)
    unresolved_obligations: list[ScientificObligationDeclaration] = Field(default_factory=list)
    source_ledger: list[SourceLedgerEntry] = Field(default_factory=list)
    artifact_manifest: list[ScientificArtifactDeclaration] = Field(default_factory=list)
    branch_outcome: BranchOutcome
    mechanism: str | None = None

    @field_validator("assignment_id")
    @classmethod
    def assignment_id_is_portable(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", normalized):
            raise ValueError("worker assignment_id must be a portable artifact identifier")
        return normalized

    @field_validator("mechanism")
    @classmethod
    def mechanism_is_normalized(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def scientific_payload_is_consistent(self) -> ResearchWorkerReport:
        result_keys = [result.local_key for result in self.results]
        obligation_keys = [obligation.local_key for obligation in self.unresolved_obligations]
        if len(result_keys) != len(set(result_keys)):
            raise ValueError("scientific result local_key values must be unique")
        if len(obligation_keys) != len(set(obligation_keys)):
            raise ValueError("scientific obligation local_key values must be unique")
        if set(result_keys).intersection(obligation_keys):
            raise ValueError("result and obligation local_key namespaces must not overlap")
        validate_result_dependency_dag(self.results)
        known_result_keys = set(result_keys)
        for obligation in self.unresolved_obligations:
            unknown_parents = set(obligation.parent_result_keys) - known_result_keys
            if unknown_parents:
                raise ValueError(
                    "scientific obligation references unknown result key(s): "
                    + ", ".join(sorted(unknown_parents))
                )
        for artifact in self.artifact_manifest:
            unknown_results = set(artifact.supporting_result_keys) - known_result_keys
            if unknown_results:
                raise ValueError(
                    "artifact declaration references unknown result key(s): "
                    + ", ".join(sorted(unknown_results))
                )

        gapped_results = [result for result in self.results if result.exact_gap is not None]
        complete_results = [
            result
            for result in self.results
            if result.disposition is ScientificResultDisposition.PROPOSED_COMPLETE
        ]
        if self.branch_outcome is BranchOutcome.BLOCKED:
            if not self.unresolved_obligations and not gapped_results:
                raise ValueError("a blocked branch must identify a typed unresolved obligation")
            if any(result.scope is ScientificScope.MAIN for result in complete_results):
                raise ValueError("a blocked branch cannot contain a proposed_complete main result")
        if self.branch_outcome is BranchOutcome.REFUTED and not any(
            result.kind is ScientificResultKind.COUNTEREXAMPLE for result in self.results
        ):
            raise ValueError("a refuted branch requires a concrete counterexample result")
        if self.branch_outcome is BranchOutcome.CANDIDATE_COMPLETE:
            if self.unresolved_obligations or gapped_results:
                raise ValueError("candidate_complete cannot retain any unresolved obligation")
            if not any(
                result.scope is ScientificScope.MAIN
                and result.disposition is ScientificResultDisposition.PROPOSED_COMPLETE
                for result in self.results
            ):
                raise ValueError(
                    "candidate_complete requires a gap-free proposed_complete main result"
                )
        if not self.results and self.branch_outcome is not BranchOutcome.BLOCKED:
            raise ValueError("a non-blocked worker report requires a scientific result")
        return self

    @property
    def status(self) -> WorkerStatus:
        """Legacy internal view used by the scheduler and report summaries."""

        return WorkerStatus(self.branch_outcome.value)

    @property
    def formal_results(self) -> list[str]:
        return [
            result.exact_statement
            for result in self.results
            if result.kind is not ScientificResultKind.COUNTEREXAMPLE
        ]

    @property
    def proof_content(self) -> str:
        proofs = list(
            dict.fromkeys(
                result.proof_or_certificate.strip()
                for result in self.results
                if result.proof_or_certificate.strip()
            )
        )
        return "\n\n".join(proofs)

    @property
    def exact_gap(self) -> str | None:
        gaps = list(
            dict.fromkeys(
                [
                    *(result.exact_gap for result in self.results if result.exact_gap),
                    *(
                        item.exact_statement
                        for item in self.unresolved_obligations
                        if not item.local_key.startswith("legacy-dependency-")
                    ),
                ]
            )
        )
        return "\n".join(gaps) or None

    @property
    def sources(self) -> list[SourceLedgerEntry]:
        return self.source_ledger

    @property
    def assumptions(self) -> list[str]:
        return list(dict.fromkeys(item for result in self.results for item in result.assumptions))

    @property
    def counterexamples(self) -> list[str]:
        return [
            result.exact_statement
            for result in self.results
            if result.kind is ScientificResultKind.COUNTEREXAMPLE
        ]

    @property
    def dependencies(self) -> list[str]:
        dependencies = [
            node_id for result in self.results for node_id in result.dependency_node_ids
        ]
        dependencies.extend(
            obligation.exact_statement
            for obligation in self.unresolved_obligations
            if obligation.local_key.startswith("legacy-dependency-")
        )
        return list(dict.fromkeys(dependencies))


def _assignment_matches_active_scientific_phase(
    record: ResearchAssignmentState,
    state: ScientificPhaseState,
) -> bool:
    """Require both the phase name and its monotone epoch before launching work."""

    return (
        record.assignment.scientific_phase is state.phase
        and record.scientific_phase_epoch == state.phase_epoch
    )


def _current_scientific_phase_completions(
    completed: list[ResearchAssignmentState],
    state: ScientificPhaseState,
) -> list[ResearchAssignmentState]:
    """Exclude late results from earlier phase epochs from current transition signals."""

    return [
        record
        for record in completed
        if record.status is AssignmentLifecycle.COMPLETED
        and record.assignment.scientific_phase is state.phase
        and record.scientific_phase_epoch == state.phase_epoch
    ]


def _scientific_target_versions(
    nodes: list[GraphNode],
    *,
    graph_revision: str,
    problem_id: str,
    target_claim_id: str,
) -> dict[str, str]:
    """Project semantic versions for every possible scientific cut target."""

    ledger = project_markdown_ledger(
        nodes,
        graph_revision=graph_revision,
        problem_id=problem_id,
        target_claim_id=target_claim_id,
    )
    versions = {
        obligation_id: obligation.logical_version
        for obligation_id, obligation in ledger.obligations.items()
    }
    versions.update({claim_id: claim.logical_version for claim_id, claim in ledger.claims.items()})
    for node in nodes:
        if node.problem_id != problem_id or node.tombstone:
            continue
        versions.setdefault(node.matek_id, sha256_json(node.model_dump(mode="json")))
    return dict(sorted(versions.items()))


def _adversarial_audit_has_durable_pass_evidence(
    completed: list[ResearchAssignmentState],
    reports_by_id: dict[str, ResearchWorkerReport],
    *,
    phase_epoch: int,
    active_cut_ids: list[str],
    current_obligation_versions: dict[str, str] | None = None,
) -> bool:
    """Accept an adversarial phase only from graph-recorded independent audit gates.

    A no-gap worker report is not audit evidence.  Each attacked obligation must leave the
    canonical cut and have application-owned, independently audited results from both of the
    complementary adversarial roles in the same phase epoch.
    """

    records = [
        record
        for record in completed
        if record.status is AssignmentLifecycle.COMPLETED
        and record.assignment.scientific_phase is ScientificPhase.ADVERSARIAL_AUDIT
        and record.scientific_phase_epoch == phase_epoch
        and record.assignment.id in reports_by_id
    ]
    if not records:
        return False
    target_ids = {
        target_id for record in records for target_id in record.assignment.target_obligation_ids
    }
    if not target_ids or not target_ids.isdisjoint(active_cut_ids):
        return False
    if any(reports_by_id[record.assignment.id].exact_gap is not None for record in records):
        return False

    required_roles = {
        ScientificRole.FALSIFIER,
        ScientificRole.TRANSFER_AUDITOR,
    }
    current_versions = current_obligation_versions or {}
    for target_id in target_ids:
        roles_with_durable_passes = {
            record.assignment.scientific_role
            for record in records
            if target_id in record.assignment.target_obligation_ids
            and bool(current_versions.get(target_id))
            and record.assignment.target_obligation_version_map.get(target_id)
            == current_versions.get(target_id)
            and any(
                target_id in audit.target_obligation_ids
                and audit.target_obligation_versions.get(target_id)
                == current_versions.get(target_id)
                and audit.gate_status is LemmaAuditGateStatus.AUDIT_PASSED
                and audit.graph_recorded
                for audit in record.intermediate_lemma_audits
            )
        }
        if not required_roles.issubset(roles_with_durable_passes):
            return False
    return True


def _legacy_obligation(
    *,
    local_key: str,
    statement: str,
    parent_result_keys: list[str],
) -> ScientificObligationDeclaration:
    return ScientificObligationDeclaration(
        local_key=local_key,
        exact_statement=statement,
        conclusion=statement,
        parent_result_keys=parent_result_keys,
        scope=ScientificScope.BRANCH,
    )


def adapt_research_worker_report_v1(
    value: ArchivedResearchWorkerReportV1 | dict[str, object] | str,
) -> ResearchWorkerReport:
    """Explicitly normalize one archived flat worker report into the strict v2 payload."""

    if isinstance(value, ArchivedResearchWorkerReportV1):
        legacy = value
    elif isinstance(value, str):
        legacy = ArchivedResearchWorkerReportV1.model_validate_json(value)
    else:
        legacy = ArchivedResearchWorkerReportV1.model_validate(value)

    formal_results = [item.strip() for item in legacy.formal_results if item.strip()]
    counterexamples = [item.strip() for item in legacy.counterexamples if item.strip()]
    proof_content = legacy.proof_content.strip()
    exact_gap = (legacy.exact_gap or "").strip()
    assumptions = list(dict.fromkeys(item.strip() for item in legacy.assumptions if item.strip()))
    stable_dependencies: list[str] = []
    unresolved_dependencies: list[str] = []
    for item in legacy.dependencies:
        normalized = item.strip()
        if not normalized:
            continue
        upper = normalized.upper()
        if _STABLE_SCIENTIFIC_NODE_ID.fullmatch(upper):
            stable_dependencies.append(upper)
        else:
            unresolved_dependencies.append(normalized)
    stable_dependencies = list(dict.fromkeys(stable_dependencies))
    unresolved_dependencies = list(dict.fromkeys(unresolved_dependencies))

    outcome = BranchOutcome(legacy.status.value)
    downgrade_obligation: str | None = None
    if outcome is BranchOutcome.CANDIDATE_COMPLETE and exact_gap:
        outcome = BranchOutcome.BLOCKED
    if outcome is BranchOutcome.CANDIDATE_COMPLETE and unresolved_dependencies:
        outcome = BranchOutcome.BLOCKED
        downgrade_obligation = (
            "Archived candidate retained free-text dependencies that require typed resolution."
        )
    if outcome is BranchOutcome.CANDIDATE_COMPLETE and not (formal_results or proof_content):
        outcome = BranchOutcome.BLOCKED
        downgrade_obligation = "Archived candidate contained no exact result or proof content."
    if outcome is BranchOutcome.REFUTED and not (counterexamples or exact_gap):
        outcome = BranchOutcome.BLOCKED
        downgrade_obligation = "Archived refutation contained no concrete obstruction."
    if outcome is BranchOutcome.PROGRESS and not (
        formal_results or proof_content or counterexamples
    ):
        outcome = BranchOutcome.BLOCKED
        downgrade_obligation = "Archived progress report contained no concrete scientific result."

    results: list[ScientificResult] = []
    result_scope = (
        ScientificScope.MAIN
        if outcome is BranchOutcome.CANDIDATE_COMPLETE
        else ScientificScope.BRANCH
    )
    disposition = (
        ScientificResultDisposition.PROPOSED_COMPLETE
        if outcome is BranchOutcome.CANDIDATE_COMPLETE
        else ScientificResultDisposition.PARTIAL
    )
    for index, statement in enumerate(formal_results, start=1):
        results.append(
            ScientificResult(
                local_key=f"legacy-result-{index}",
                kind=ScientificResultKind.LEMMA,
                exact_statement=statement,
                scope=result_scope,
                assumptions=assumptions,
                proof_or_certificate=proof_content or statement,
                exact_gap=exact_gap if index == 1 and exact_gap else None,
                dependency_node_ids=stable_dependencies,
                disposition=disposition,
            )
        )
    if not results and proof_content and outcome is not BranchOutcome.REFUTED:
        results.append(
            ScientificResult(
                local_key="legacy-unstructured-result",
                kind=ScientificResultKind.LEMMA,
                exact_statement=proof_content,
                scope=result_scope,
                assumptions=assumptions,
                proof_or_certificate=proof_content,
                exact_gap=exact_gap or None,
                dependency_node_ids=stable_dependencies,
                disposition=disposition,
            )
        )
    for index, statement in enumerate(counterexamples, start=1):
        results.append(
            ScientificResult(
                local_key=f"legacy-counterexample-{index}",
                kind=ScientificResultKind.COUNTEREXAMPLE,
                exact_statement=statement,
                scope=ScientificScope.BRANCH,
                assumptions=assumptions,
                proof_or_certificate=proof_content or statement,
                dependency_node_ids=stable_dependencies,
                disposition=ScientificResultDisposition.REFUTED_MECHANISM,
            )
        )
    if outcome is BranchOutcome.REFUTED and not counterexamples:
        obstruction = exact_gap or proof_content
        results.append(
            ScientificResult(
                local_key="legacy-counterexample-1",
                kind=ScientificResultKind.COUNTEREXAMPLE,
                exact_statement=obstruction,
                scope=ScientificScope.BRANCH,
                assumptions=assumptions,
                proof_or_certificate=proof_content or obstruction,
                dependency_node_ids=stable_dependencies,
                disposition=ScientificResultDisposition.REFUTED_MECHANISM,
            )
        )

    parent_keys = [result.local_key for result in results]
    obligations: list[ScientificObligationDeclaration] = []
    if exact_gap:
        obligations.append(
            _legacy_obligation(
                local_key="legacy-exact-gap",
                statement=exact_gap,
                parent_result_keys=parent_keys,
            )
        )
    for index, dependency in enumerate(unresolved_dependencies, start=1):
        obligations.append(
            _legacy_obligation(
                local_key=f"legacy-dependency-{index}",
                statement=dependency,
                parent_result_keys=parent_keys,
            )
        )
    if downgrade_obligation:
        obligations.append(
            _legacy_obligation(
                local_key="legacy-invalid-terminal-claim",
                statement=downgrade_obligation,
                parent_result_keys=parent_keys,
            )
        )
    if outcome is BranchOutcome.BLOCKED and not obligations:
        obligations.append(
            _legacy_obligation(
                local_key="legacy-missing-gap",
                statement="Archived blocked report did not identify its exact missing statement.",
                parent_result_keys=parent_keys,
            )
        )

    return ResearchWorkerReport(
        assignment_id=legacy.assignment_id,
        results=results,
        unresolved_obligations=obligations,
        source_ledger=legacy.sources,
        artifact_manifest=[],
        branch_outcome=outcome,
        mechanism=legacy.mechanism,
    )


def load_research_worker_report_json(value: str) -> ResearchWorkerReport:
    """Read a persisted v2 report or explicitly adapt a persisted v1 report."""

    raw = json.loads(value)
    if not isinstance(raw, dict):
        raise ValueError("persisted worker report must be a JSON object")
    if raw.get("schema_version") == 2:
        return ResearchWorkerReport.model_validate(raw)
    return adapt_research_worker_report_v1(cast(dict[str, object], raw))


class ResearchWorkerEvidence(BaseModel):
    """Atomic worker report/source transaction written before split artifacts."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    assignment_id: str
    response_id: str
    raw_report: dict[str, Any]
    normalized_report: ResearchWorkerReport
    source_verification: SourceVerificationReport

    @property
    def report(self) -> ResearchWorkerReport:
        return self.normalized_report


class ArchivedResearchWorkerEvidenceV1(BaseModel):
    """Explicit compatibility model for the original worker evidence transaction."""

    model_config = ConfigDict(extra="forbid")

    assignment_id: str
    response_id: str
    report: ArchivedResearchWorkerReportV1
    source_verification: SourceVerificationReport


def load_research_worker_evidence_json(value: str) -> ResearchWorkerEvidence:
    """Read v2 evidence or explicitly adapt a frozen v1 report/evidence transaction."""

    raw = json.loads(value)
    if not isinstance(raw, dict):
        raise ValueError("persisted worker evidence must be a JSON object")
    if raw.get("schema_version") == 2:
        return ResearchWorkerEvidence.model_validate(raw)
    legacy = ArchivedResearchWorkerEvidenceV1.model_validate(raw)
    raw_report = legacy.report.model_dump(mode="json")
    return ResearchWorkerEvidence(
        assignment_id=legacy.assignment_id,
        response_id=legacy.response_id,
        raw_report=raw_report,
        normalized_report=adapt_research_worker_report_v1(legacy.report),
        source_verification=legacy.source_verification,
    )


class WorkerCollectionResult(BaseModel):
    """One completion-drain result without exception fan-out."""

    model_config = ConfigDict(extra="forbid")

    accepted_reports: list[ResearchWorkerReport] = Field(default_factory=list)
    candidate_ids: list[str] = Field(default_factory=list)
    execution_issues: list[ExecutionIssue] = Field(default_factory=list)


class ApproachRecord(BaseModel):
    branch_id: str = ""
    family: str
    mechanism: str
    strongest_result: str = ""
    exact_gap: str = ""
    status: str = "active"
    target_node_ids: list[str] = Field(default_factory=list)
    reopen_condition: str = ""
    assumptions: list[str] = Field(default_factory=list)
    counterexamples: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    assignment_ids: list[str] = Field(default_factory=list)


class ApproachRegistry(BaseModel):
    approaches: list[ApproachRecord] = Field(default_factory=list)

    def update(self, assignment: ResearchAssignment, report: ResearchWorkerReport) -> None:
        # One family can contain several independent branches and sub-branches. Keep
        # them separate so a later report cannot overwrite a blocked or refuted route.
        existing = next((item for item in self.approaches if item.branch_id == assignment.id), None)
        strongest = "\n\n".join(item for item in report.formal_results if item.strip())
        if not strongest:
            strongest = report.proof_content.strip()
        reopen_condition = (
            "Reopen only with new evidence that resolves the recorded exact gap or defeats "
            "the recorded counterexample."
            if report.status in {WorkerStatus.BLOCKED, WorkerStatus.REFUTED}
            else "Continue only through a coordinator assignment targeting the remaining gap."
        )
        if existing is None:
            self.approaches.append(
                ApproachRecord(
                    branch_id=assignment.id,
                    family=assignment.approach_family,
                    mechanism=report.mechanism or assignment.task,
                    strongest_result=strongest,
                    exact_gap=report.exact_gap or "",
                    status=report.status.value,
                    target_node_ids=list(dict.fromkeys(assignment.target_node_ids)),
                    reopen_condition=reopen_condition,
                    assumptions=list(dict.fromkeys(report.assumptions)),
                    counterexamples=list(dict.fromkeys(report.counterexamples)),
                    dependencies=list(dict.fromkeys(report.dependencies)),
                    assignment_ids=[assignment.id],
                )
            )
            return
        if strongest and strongest not in existing.strongest_result:
            existing.strongest_result = "\n\n".join(
                item for item in (existing.strongest_result, strongest) if item
            )
        if report.exact_gap:
            existing.exact_gap = "\n".join(
                dict.fromkeys(item for item in (existing.exact_gap, report.exact_gap) if item)
            )
        existing.status = report.status.value
        existing.target_node_ids = list(
            dict.fromkeys([*existing.target_node_ids, *assignment.target_node_ids])
        )
        existing.reopen_condition = reopen_condition
        existing.assumptions = list(dict.fromkeys([*existing.assumptions, *report.assumptions]))
        existing.counterexamples = list(
            dict.fromkeys([*existing.counterexamples, *report.counterexamples])
        )
        existing.dependencies = list(dict.fromkeys([*existing.dependencies, *report.dependencies]))
        existing.assignment_ids = list(dict.fromkeys([*existing.assignment_ids, assignment.id]))


class ResearchContinuityRoute(BaseModel):
    """One evidence-bearing route in the durable coordinator handoff."""

    model_config = ConfigDict(extra="forbid")

    # ``round_id`` remains readable for pre-event-scheduler artifacts.
    round_id: int = 0
    decision_id: int = 0
    event_sequence: int = 0
    assignment_id: str
    approach_family: str
    target_node_ids: list[str] = Field(default_factory=list)
    status: WorkerStatus
    mechanism: str
    formal_results: list[str]
    proof_content: str
    exact_gap: str | None
    assumptions: list[str]
    counterexamples: list[str]
    dependencies: list[str]
    reopen_condition: str = ""


class ResearchContinuityState(BaseModel):
    """Durable, provider-independent mathematical handoff between decisions."""

    model_config = ConfigDict(extra="forbid")

    # ``after_round`` is the legacy checkpoint coordinate.
    after_round: int = 0
    after_event_sequence: int = 0
    promising_routes: list[ResearchContinuityRoute]
    partial_results: list[ResearchContinuityRoute]
    ruled_out_directions: list[ResearchContinuityRoute]
    blocked_routes: list[ResearchContinuityRoute]
    open_gaps: list[str]
    counterexamples: list[str]
    dependencies: list[str]
    audit_repair_obligations: list[str]
    claims_requiring_counterexample_search: list[str]
    lemmas_requiring_proof_completion: list[str]
    retired_assignment_ids: list[str]
    redirected_assignment_ids: list[str]
    completed_assignment_ids: list[str]


class LemmaDependency(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lemma: str
    dependencies: list[str]


class ImportedTheorem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    statement: str
    hypotheses: list[str]
    source_id: str
    identifiers: list[str]
    evidence_claims: list[SourceEvidenceClaim]
    verified: bool = False

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_source(cls, value: object) -> object:
        if not isinstance(value, dict) or "identifiers" in value:
            return value
        raw = dict(value)
        source = SourceLedgerEntry.model_validate(
            {
                "title": raw.get("name", "Imported theorem"),
                "stable_identifier": raw.pop("stable_identifier", None),
                "evidence": raw.get("statement", "Imported theorem statement"),
                "required_for_claim": True,
            }
        )
        raw.update(
            {
                "source_id": source.source_id,
                "identifiers": source.identifiers,
                "evidence_claims": source.evidence_claims,
                "verified": False,
            }
        )
        return raw

    def as_source_entry(self) -> SourceLedgerEntry:
        return SourceLedgerEntry(
            source_id=self.source_id,
            title=self.name,
            identifiers=self.identifiers,
            evidence_claims=self.evidence_claims,
            required_for_claim=True,
            verified=self.verified,
        )


class CandidateProofPackage(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    exact_theorem: str = Field(validation_alias=AliasChoices("exact_theorem", "theorem_statement"))
    definitions: list[str]
    lemma_dependency_graph: list[LemmaDependency]
    full_proof: str = Field(
        validation_alias=AliasChoices("full_proof", "proof_markdown", "proof_content")
    )
    imported_theorems: list[ImportedTheorem]
    exceptional_cases: list[str]
    parameter_bookkeeping: list[str]
    unresolved_items: list[str]
    quantitative_or_algorithmic: bool

    @field_validator("lemma_dependency_graph", mode="before")
    @classmethod
    def accept_legacy_dependency_map(cls, value: object) -> object:
        if isinstance(value, dict):
            return [
                {"lemma": str(lemma), "dependencies": dependencies}
                for lemma, dependencies in value.items()
            ]
        return value

    @field_validator("exact_theorem", "full_proof")
    @classmethod
    def package_text_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("candidate theorem and proof must not be empty")
        return value


class CandidatePackageEvidence(BaseModel):
    """Atomic package/source transaction written before split candidate artifacts."""

    model_config = ConfigDict(extra="forbid")

    response_id: str
    candidate: CandidateProofPackage
    source_verification: SourceVerificationReport


class CandidateComputationBinding(BaseModel):
    """Application-owned proof-candidate binding to one replayed computation result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assignment_id: str
    result_local_key: str
    result_payload_sha256: str
    evidence_path: str
    evidence_sha256: str
    manifest_path: str
    manifest_sha256: str
    manifest_file_sha256: str
    replay_path: str
    replay_sha256: str
    replay_record_sha256: str
    declaration_sha256s: list[str]
    main_result_keys: list[str]

    @field_validator(
        "result_payload_sha256",
        "evidence_sha256",
        "manifest_sha256",
        "manifest_file_sha256",
        "replay_sha256",
        "replay_record_sha256",
    )
    @classmethod
    def computation_hashes_are_sha256(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("candidate computation evidence hashes must be SHA-256")
        return value

    @field_validator("declaration_sha256s")
    @classmethod
    def declaration_hashes_are_canonical(cls, values: list[str]) -> list[str]:
        if not values or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in values):
            raise ValueError(
                "candidate computation evidence requires supporting declaration hashes"
            )
        if values != sorted(set(values)):
            raise ValueError("candidate computation declaration hashes must be sorted and unique")
        return values

    @field_validator("main_result_keys")
    @classmethod
    def main_result_keys_are_canonical(cls, values: list[str]) -> list[str]:
        if not values or values != sorted(set(values)):
            raise ValueError(
                "candidate computation evidence requires sorted exact-main dependency roots"
            )
        return values


class CandidateGraphSupportBinding(BaseModel):
    """Hash-bound canonical-ledger slice supporting one candidate-trigger report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assignment_id: str
    report_sha256: str
    admission_record_path: str
    admission_record_sha256: str
    admission_revision: str
    main_claim_id: str
    main_result_keys: list[str]
    closure_result_keys: list[str]
    computation_result_keys: list[str]
    support_nodes: list[dict[str, Any]]
    support_sha256: str

    @field_validator("report_sha256", "admission_record_sha256", "support_sha256")
    @classmethod
    def graph_support_hashes_are_sha256(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("candidate graph support hashes must be SHA-256")
        return value

    @model_validator(mode="after")
    def support_slice_digest_is_valid(self) -> CandidateGraphSupportBinding:
        if not self.main_result_keys or self.main_result_keys != sorted(set(self.main_result_keys)):
            raise ValueError("candidate graph support requires sorted exact-main result keys")
        if (
            not self.closure_result_keys
            or self.closure_result_keys != sorted(set(self.closure_result_keys))
            or not set(self.main_result_keys).issubset(self.closure_result_keys)
        ):
            raise ValueError("candidate graph support requires a canonical local-result closure")
        if self.computation_result_keys != sorted(set(self.computation_result_keys)) or not set(
            self.computation_result_keys
        ).issubset(self.closure_result_keys):
            raise ValueError("candidate graph computation keys must belong to its support closure")
        if not self.support_nodes:
            raise ValueError("candidate graph support slice must not be empty")
        if self.support_sha256 != sha256_json(self.support_nodes):
            raise ValueError("candidate graph support slice digest does not match its nodes")
        return self


class AuditIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Literal["blocking", "advisory"] = "blocking"
    description: str = ""
    repair: str | None = None


class AuditVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: AuditDecision
    issues: list[AuditIssue]
    unresolved_obligations: list[str]
    target_matches: bool
    # Defaults preserve readability of pre-v0.3 audit artifacts. New provider outputs are
    # rejected by ``run_audit`` unless they supply substantive evidence for both fields.
    audit_role: str = "legacy"
    rationale: str = "Legacy audit artifact did not record a rationale."
    checks_performed: list[str] = Field(
        default_factory=lambda: ["Legacy audit artifact predates explicit check recording."]
    )

    @field_validator("audit_role", "rationale")
    @classmethod
    def audit_evidence_text_is_nonempty(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("audit role and rationale must not be blank")
        return normalized

    @field_validator("checks_performed")
    @classmethod
    def checks_are_nonempty(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        if not normalized:
            raise ValueError("an audit must record at least one check performed")
        return normalized

    @model_validator(mode="after")
    def verdict_has_consistent_blocking_state(self) -> AuditVerdict:
        blocking_issues = [
            issue for issue in self.issues if issue.severity.casefold() == "blocking"
        ]
        if self.verdict == AuditDecision.PASS and (
            blocking_issues or self.unresolved_obligations or not self.target_matches
        ):
            raise ValueError(
                "a passing audit cannot retain blocking issues, obligations, or a target mismatch"
            )
        if self.verdict != AuditDecision.PASS and not (
            self.issues or self.unresolved_obligations or not self.target_matches
        ):
            raise ValueError("a non-passing audit must state a concrete defect")
        return self


class FinalJudgeVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: FinalJudgeDecision
    reasons: list[str] = Field(default_factory=list)
    unresolved_obligations: list[str] = Field(default_factory=list)
    strongest_result: str = ""

    @model_validator(mode="after")
    def verdict_has_consistent_obligations(self) -> FinalJudgeVerdict:
        if self.verdict == FinalJudgeDecision.ACCEPTED and self.unresolved_obligations:
            raise ValueError("an accepted final verdict cannot retain obligations")
        if self.verdict != FinalJudgeDecision.ACCEPTED and not (
            self.reasons or self.unresolved_obligations
        ):
            raise ValueError("a non-accepted final verdict must explain the exact defect")
        return self


class ResearchAcceptanceGate(BaseModel):
    accepted: bool
    candidate_sha256: str
    claim_contract_sha256: str
    mandatory_audits: list[str]
    final_judge_response_id: str
    computation_bindings_sha256: str | None = None
    graph_support_bindings_sha256: str | None = None

    @field_validator("computation_bindings_sha256", "graph_support_bindings_sha256")
    @classmethod
    def optional_computation_digest_is_sha256(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("candidate evidence binding digest must be SHA-256")
        return value


class ResearchWorkflowSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    orchestration_mode: Literal["flat", "hierarchical"] = "hierarchical"
    maximum_subagents_per_agent: int = Field(default=4, ge=0, le=32)
    minimum_initial_assignments: int = Field(default=8, ge=4)
    maximum_concurrent_agents: int = Field(default=4, ge=1)
    max_concurrent_agents: int = Field(default=24, ge=1)
    maximum_pending_assignments: int = Field(default=1_024, ge=1)
    maximum_coordinator_decisions: int = Field(default=100_000, ge=1)
    maximum_coordinator_context_characters: int = Field(default=800_000, ge=100_000)
    maximum_unrequested_full_graph_node_characters: int = Field(default=120_000, ge=1_000)
    maximum_coordinator_requested_artifacts: int = Field(default=32, ge=1, le=32)
    maximum_model_calls: int | None = Field(default=None, ge=0)
    run_complexity_audit: bool | None = None
    scientific_phase_policy: ScientificPhasePolicy = Field(default_factory=ScientificPhasePolicy)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_round_limits(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        legacy_pending = normalized.pop("maximum_assignments_per_round", None)
        legacy_decisions = normalized.pop("maximum_rounds", None)
        if legacy_pending is not None:
            normalized.setdefault("maximum_pending_assignments", legacy_pending)
        if legacy_decisions is not None:
            pending = normalized.get("maximum_pending_assignments", 32)
            if (
                isinstance(legacy_decisions, int)
                and not isinstance(legacy_decisions, bool)
                and isinstance(pending, int)
                and not isinstance(pending, bool)
            ):
                normalized.setdefault("maximum_coordinator_decisions", legacy_decisions * pending)
            else:
                normalized.setdefault("maximum_coordinator_decisions", legacy_decisions)
        return normalized

    @model_validator(mode="after")
    def pending_assignment_limit_covers_initial_portfolio(self) -> ResearchWorkflowSettings:
        if self.maximum_pending_assignments < self.minimum_initial_assignments:
            raise ValueError(
                "maximum_pending_assignments cannot be less than minimum_initial_assignments"
            )
        return self

    @property
    def maximum_assignments_per_round(self) -> int:
        """Compatibility name for callers migrating from fixed rounds."""

        return self.maximum_pending_assignments

    @property
    def maximum_rounds(self) -> int:
        """Compatibility estimate in full pending-window equivalents."""

        pending = self.maximum_pending_assignments
        return (self.maximum_coordinator_decisions + pending - 1) // pending

    @property
    def hierarchical_subagent_limit(self) -> int:
        """Return the active nested-agent limit; flat workers receive no such capability."""

        return self.maximum_subagents_per_agent if self.orchestration_mode == "hierarchical" else 0


class ResearchResult(BaseModel):
    outcome: ResearchOutcome
    rounds: list[ResearchRoundPlan] = Field(default_factory=list)
    coordinator_decisions: list[ResearchCoordinatorDecision] = Field(default_factory=list)
    research_events: int = 0
    worker_reports: list[ResearchWorkerReport]
    registry: ApproachRegistry
    candidate: CandidateProofPackage | None = None
    audits: dict[str, AuditVerdict] = Field(default_factory=dict)
    final_verdict: FinalJudgeVerdict | None = None
    unresolved_obligations: list[str] = Field(default_factory=list)
    strongest_result: str = ""
    repair_rounds: int = 0
    research_subagents_assigned: int = 0
    research_subagents_used: int = 0
    continuity: ResearchContinuityState | None = None
    acceptance_gate: ResearchAcceptanceGate | None = None
    refutation_gate: CounterexampleAuditGate | None = None
    execution_issues: list[ExecutionIssue] = Field(default_factory=list)
    pause_reason: str | None = None
    resume_action: str | None = None
    artifacts: ArtifactManifest = Field(default_factory=ArtifactManifest)
    calls: CallManifest

    @model_validator(mode="after")
    def terminal_gates_match_outcome(self) -> ResearchResult:
        if self.outcome is ResearchOutcome.REJECTED:
            if (
                self.refutation_gate is None
                or self.refutation_gate.status
                is not CounterexampleAuditGateStatus.REFUTATION_VERIFIED
                or self.acceptance_gate is not None
                or self.unresolved_obligations
            ):
                raise ValueError(
                    "rejected research requires one clean verified exact-counterexample gate"
                )
        elif self.refutation_gate is not None:
            raise ValueError("only rejected research may retain a refutation gate")
        return self

    @property
    def accepted_for_manuscript(self) -> bool:
        return (
            self.outcome == ResearchOutcome.ACCEPTED
            and self.acceptance_gate is not None
            and self.acceptance_gate.accepted
        )


TModel = TypeVar("TModel", bound=BaseModel)


class _DelegatedModelClient:
    """Adapt one role-specific client to the research scheduler's tracked call path."""

    def __init__(
        self,
        invoke: Callable[[ModelRequest, type[BaseModel]], Awaitable[ModelResult[Any]]],
    ) -> None:
        self._invoke = invoke

    async def generate_structured(
        self,
        request: ModelRequest,
        output_type: type[TModel],
    ) -> ModelResult[TModel]:
        result = await self._invoke(request, output_type)
        return cast(ModelResult[TModel], result)


class _TrackedModelClient:
    def __init__(
        self,
        client: ModelClient,
        maximum_calls: int | None,
        *,
        hard_maximum_calls: int | None = None,
        calls: int = 0,
        response_ids: list[str] | None = None,
        call_keys: list[str] | None = None,
        response_ids_by_call_key: dict[str, str] | None = None,
    ) -> None:
        self.client = client
        self._run_maximum_calls = maximum_calls
        self._hard_maximum_calls = hard_maximum_calls
        self.calls = calls
        self.response_ids = list(response_ids or [])
        self.call_keys = list(call_keys or [])
        self.response_ids_by_call_key = dict(response_ids_by_call_key or {})
        if calls != len(self.call_keys):
            raise StageValidationError(
                "Research model-call count does not match its durable request identities."
            )
        if len(set(self.call_keys)) != len(self.call_keys) or any(
            not re.fullmatch(r"[0-9a-f]{64}", key) for key in self.call_keys
        ):
            raise StageValidationError("Research model-call identities are invalid or duplicated.")
        if self.maximum_calls is not None and calls > self.maximum_calls:
            raise StageValidationError("Research model-call count exceeds its configured limit.")
        mapped_response_ids = list(self.response_ids_by_call_key.values())
        if (
            not set(self.response_ids_by_call_key).issubset(self.call_keys)
            or len(set(mapped_response_ids)) != len(mapped_response_ids)
            or any(not response_id.strip() for response_id in mapped_response_ids)
            or set(self.response_ids) != set(mapped_response_ids)
            or len(set(self.response_ids)) != len(self.response_ids)
        ):
            raise StageValidationError("Research response identities are invalid or duplicated.")
        if len(self.response_ids) > calls:
            raise StageValidationError("Research has more response identities than logical calls.")
        self._call_key_set = set(self.call_keys)
        self._accounted_credit_keys: set[str] = set()
        self._results_by_call_key: dict[str, ModelResult[Any]] = {}

    @property
    def maximum_calls(self) -> int | None:
        limits = [
            limit
            for limit in (self._run_maximum_calls, self._hard_maximum_calls)
            if limit is not None
        ]
        return min(limits) if limits else None

    def can_call(self, count: int = 1) -> bool:
        return self.maximum_calls is None or self.calls + count <= self.maximum_calls

    def _hard_limit_allows(self, count: int = 1) -> bool:
        return self._hard_maximum_calls is None or self.calls + count <= self._hard_maximum_calls

    def _request_is_accounted(
        self,
        *,
        instructions: str,
        input_text: str,
        settings: ModelSettings,
        output_type: type[BaseModel],
    ) -> bool:
        checker = getattr(self.client, "is_accounted_request", None)
        return bool(
            callable(checker)
            and checker(
                ModelRequest(
                    instructions=instructions,
                    input_text=input_text,
                    settings=settings,
                ),
                output_type,
            )
        )

    def can_admit(self, *, paid_calls: int, logical_calls: int) -> bool:
        """Check run-paid and explicit logical-call ceilings independently."""

        if paid_calls < 0 or logical_calls < 0 or paid_calls > logical_calls:
            raise ValueError("invalid model-call admission counts")
        return (
            self._run_maximum_calls is None or self.calls + paid_calls <= self._run_maximum_calls
        ) and (
            self._hard_maximum_calls is None
            or self.calls + logical_calls <= self._hard_maximum_calls
        )

    def has_call_key(self, call_key: str) -> bool:
        return call_key in self._call_key_set

    def reserve_call_key(self, call_key: str) -> bool:
        """Durably consume one future logical-call slot under a stable placeholder."""

        if call_key in self._call_key_set:
            return False
        if not self.can_call():
            raise _ResearchBudgetExhausted
        self.calls += 1
        self.call_keys.append(call_key)
        self._call_key_set.add(call_key)
        return True

    def release_call_key(self, call_key: str) -> bool:
        """Release an admitted request that provably never reached a provider."""

        if call_key not in self._call_key_set:
            return False
        if call_key in self.response_ids_by_call_key:
            raise StageValidationError("Cannot release a model request that has a response.")
        self.call_keys.remove(call_key)
        self._call_key_set.remove(call_key)
        self.calls -= 1
        if call_key in self._accounted_credit_keys:
            self._accounted_credit_keys.remove(call_key)
            assert self._run_maximum_calls is not None
            self._run_maximum_calls -= 1
        return True

    def bind_persisted_response(self, call_key: str, response_id: str) -> bool:
        """Adopt authenticated evidence committed just ahead of scheduler state."""

        if call_key not in self._call_key_set:
            raise StageValidationError(
                "Persisted audit response has no reserved research model request."
            )
        normalized_response_id = response_id.strip()
        if not normalized_response_id:
            raise StageValidationError("Persisted audit response has no usable identity.")
        existing = self.response_ids_by_call_key.get(call_key)
        if existing is not None:
            if existing != normalized_response_id:
                raise StageValidationError(
                    "Persisted audit response conflicts with its scheduler request binding."
                )
            return False
        if normalized_response_id in self.response_ids:
            raise StageValidationError(
                "Persisted audit response is already bound to a different scheduler request."
            )
        self.response_ids_by_call_key[call_key] = normalized_response_id
        self.response_ids.append(normalized_response_id)
        return True

    def request_key(
        self,
        *,
        instructions: str,
        input_text: str,
        settings: ModelSettings,
        output_type: type[BaseModel],
    ) -> str:
        request = ModelRequest(
            instructions=instructions,
            input_text=input_text,
            settings=settings,
        )
        identity_factory = getattr(self.client, "request_cache_key", None)
        if callable(identity_factory):
            identity = identity_factory(request, output_type)
            if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
                raise StageValidationError("Model client returned an invalid request identity.")
            return identity
        return model_request_cache_key(
            request,
            output_type,
            stage="research",
            cache_namespace="research-scheduler-v2",
        )

    def can_generate(
        self,
        *,
        instructions: str,
        input_text: str,
        settings: ModelSettings,
        output_type: type[BaseModel],
    ) -> bool:
        key = self.request_key(
            instructions=instructions,
            input_text=input_text,
            settings=settings,
            output_type=output_type,
        )
        return (
            key in self._call_key_set
            or self.can_call()
            or (
                self._hard_limit_allows()
                and self._request_is_accounted(
                    instructions=instructions,
                    input_text=input_text,
                    settings=settings,
                    output_type=output_type,
                )
            )
        )

    def has_request(
        self,
        *,
        instructions: str,
        input_text: str,
        settings: ModelSettings,
        output_type: type[BaseModel],
    ) -> bool:
        return (
            self.request_key(
                instructions=instructions,
                input_text=input_text,
                settings=settings,
                output_type=output_type,
            )
            in self._call_key_set
        )

    def is_accounted_request(
        self,
        *,
        instructions: str,
        input_text: str,
        settings: ModelSettings,
        output_type: type[BaseModel],
    ) -> bool:
        return self._request_is_accounted(
            instructions=instructions,
            input_text=input_text,
            settings=settings,
            output_type=output_type,
        )

    def register_request(
        self,
        *,
        instructions: str,
        input_text: str,
        settings: ModelSettings,
        output_type: type[BaseModel],
        reservation_key: str | None = None,
    ) -> bool:
        """Reserve one logical request before any provider call can yield.

        Returning ``True`` means the caller must checkpoint the new reservation.
        Replaying the same frozen request is free from the scheduler's perspective;
        the accounting adapter remains responsible for replaying its saved response.
        """

        call_key = self.request_key(
            instructions=instructions,
            input_text=input_text,
            settings=settings,
            output_type=output_type,
        )
        if call_key in self._call_key_set:
            if reservation_key is not None and reservation_key in self._call_key_set:
                self.call_keys.remove(reservation_key)
                self._call_key_set.remove(reservation_key)
                self.calls -= 1
                return True
            return False
        if reservation_key is not None:
            if reservation_key not in self._call_key_set:
                raise StageValidationError("Logical model-call reservation disappeared before use.")
            reservation_index = self.call_keys.index(reservation_key)
            self.call_keys[reservation_index] = call_key
            self._call_key_set.remove(reservation_key)
            self._call_key_set.add(call_key)
            return True
        accounted_request = self._request_is_accounted(
            instructions=instructions,
            input_text=input_text,
            settings=settings,
            output_type=output_type,
        )
        if accounted_request:
            if not self._hard_limit_allows():
                raise _ResearchBudgetExhausted
            if self._run_maximum_calls is not None:
                self._run_maximum_calls += 1
                self._accounted_credit_keys.add(call_key)
        elif not self.can_call():
            raise _ResearchBudgetExhausted
        self.calls += 1
        self.call_keys.append(call_key)
        self._call_key_set.add(call_key)
        return True

    async def generate(
        self,
        *,
        instructions: str,
        input_text: str,
        settings: ModelSettings,
        output_type: type[TModel],
        client: ModelClient | None = None,
    ) -> ModelResult[TModel]:
        call_key = self.request_key(
            instructions=instructions,
            input_text=input_text,
            settings=settings,
            output_type=output_type,
        )
        self.register_request(
            instructions=instructions,
            input_text=input_text,
            settings=settings,
            output_type=output_type,
        )
        cached_result = self._results_by_call_key.get(call_key)
        if cached_result is not None:
            return cast(ModelResult[TModel], cached_result)
        selected_client = client or self.client
        result = await selected_client.generate_structured(
            ModelRequest(
                instructions=instructions,
                input_text=input_text,
                settings=settings,
            ),
            output_type,
        )
        if not result.response_id.strip():
            raise StageValidationError("Model response has no usable durable identity.")
        previous_response_id = self.response_ids_by_call_key.get(call_key)
        if previous_response_id is not None and previous_response_id != result.response_id:
            raise StageValidationError(
                "An identical logical model request returned a different response identity."
            )
        if previous_response_id is None and result.response_id in self.response_ids:
            raise StageValidationError(
                "Different logical model requests returned the same response identity."
            )
        if previous_response_id is None:
            self.response_ids_by_call_key[call_key] = result.response_id
            self.response_ids.append(result.response_id)
        self._results_by_call_key[call_key] = result
        return result


class _ResearchBudgetExhausted(Exception):
    pass


def _client_for_role(client: ModelClient, role: str) -> ModelClient:
    role_factory = getattr(client, "for_role", None)
    return role_factory(role) if callable(role_factory) else client


def _read_prompt(path: Path | None, resource_name: str) -> str:
    selected = path or project_resource(f"prompts/{resource_name}")
    try:
        return selected.read_text(encoding="utf-8")
    except OSError as exc:
        raise StageValidationError(f"Cannot read stage prompt {selected}: {exc}") from exc


def _atomic_write_immutable_json(path: Path, value: BaseModel | dict[str, object]) -> Path:
    """Create immutable JSON evidence, accepting only a byte-equivalent data replay."""

    expected: object = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    if path.exists():
        try:
            existing = json.loads(read_regular_text(path))
        except (OSError, ValueError) as exc:
            raise StageValidationError(f"Cannot validate immutable artifact {path}: {exc}") from exc
        if existing != expected:
            raise StageValidationError(f"Immutable research artifact has different content: {path}")
        return path
    return atomic_write_json(path, value)


def _atomic_write_immutable_text(path: Path, value: str) -> Path:
    """Create immutable text evidence, accepting only an exact replay."""

    if path.exists():
        if read_regular_text(path) != value:
            raise StageValidationError(f"Immutable research artifact has different content: {path}")
        return path
    return atomic_write_text(path, value)


def _atomic_write_materialized_json(path: Path, value: BaseModel | dict[str, object]) -> Path:
    """Update a derived snapshot only when its canonical data changed."""

    expected: object = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    if path.is_file():
        try:
            if json.loads(read_regular_text(path)) == expected:
                return path
        except (OSError, ValueError):
            pass
    return atomic_write_json(path, value)


def _validate_plan(
    plan: ResearchRoundPlan,
    *,
    expected_round: int,
    minimum_assignments: int,
    maximum_assignments: int,
    initial: bool,
) -> ResearchRoundPlan:
    if plan.round_id != expected_round:
        raise StageValidationError(
            f"Coordinator returned round {plan.round_id}; expected {expected_round}."
        )
    if len(plan.assignments) > maximum_assignments:
        plan = plan.model_copy(update={"assignments": plan.assignments[:maximum_assignments]})
    if len(plan.assignments) < minimum_assignments and not plan.stop_recommended:
        raise StageValidationError(
            f"Round {expected_round} has {len(plan.assignments)} assignments; "
            f"at least {minimum_assignments} are required."
        )
    identifiers = [assignment.id for assignment in plan.assignments]
    if len(set(identifiers)) != len(identifiers):
        raise StageValidationError(f"Round {expected_round} contains duplicate assignment IDs.")
    if initial and len(plan.assignments) >= 4:
        families = {
            assignment.approach_family.casefold().strip() for assignment in plan.assignments
        }
        if len(families) < 4:
            raise StageValidationError(
                "The initial research portfolio is not materially diverse: at least four "
                "distinct approach families are required."
            )
    return plan


def _validate_coordinator_decision(
    decision: ResearchCoordinatorDecision,
    *,
    expected_decision: int,
    expected_event_sequence: int,
    minimum_assignments: int,
    maximum_new_assignments: int,
    initial: bool,
    known_assignment_ids: set[str],
    completed_assignment_ids: set[str],
) -> ResearchCoordinatorDecision:
    if decision.decision_id != expected_decision:
        raise StageValidationError(
            f"Coordinator returned decision {decision.decision_id}; expected {expected_decision}."
        )
    if decision.after_event_sequence != expected_event_sequence:
        raise StageValidationError(
            "Coordinator did not acknowledge the complete durable mailbox: "
            f"event {decision.after_event_sequence}, expected {expected_event_sequence}."
        )
    if len(decision.assignments) > maximum_new_assignments:
        raise StageValidationError(
            f"Coordinator returned {len(decision.assignments)} assignments but only "
            f"{maximum_new_assignments} open queue slots were offered."
        )
    if len(decision.assignments) < minimum_assignments and (
        initial or not decision.stop_recommended
    ):
        raise StageValidationError(
            f"Coordinator decision {expected_decision} has {len(decision.assignments)} "
            f"assignments; at least {minimum_assignments} are required."
        )
    identifiers = [assignment.id for assignment in decision.assignments]
    if len(set(identifiers)) != len(identifiers):
        raise StageValidationError(
            f"Coordinator decision {expected_decision} contains duplicate assignment IDs."
        )
    reused = sorted(set(identifiers).intersection(known_assignment_ids))
    if reused:
        raise StageValidationError(
            "Coordinator reused durable assignment ID(s): " + ", ".join(reused)
        )
    if initial and len(decision.assignments) >= 4:
        families = {
            assignment.approach_family.casefold().strip() for assignment in decision.assignments
        }
        if len(families) < 4:
            raise StageValidationError(
                "The initial research portfolio is not materially diverse: at least four "
                "distinct approach families are required."
            )
    conflicting_directives = sorted(
        set(decision.retire_assignment_ids).intersection(decision.redirect_assignment_ids)
    )
    if conflicting_directives:
        raise StageValidationError(
            "Coordinator cannot both retire and redirect the same assignment: "
            + ", ".join(conflicting_directives)
        )
    directive_ids = set(decision.retire_assignment_ids) | set(decision.redirect_assignment_ids)
    unknown_directives = sorted(directive_ids - known_assignment_ids)
    if unknown_directives:
        raise StageValidationError(
            "Coordinator attempted to retire or redirect unknown assignment ID(s): "
            + ", ".join(unknown_directives)
        )
    unknown_candidate_reports = sorted(
        set(decision.candidate_report_ids) - completed_assignment_ids
    )
    if unknown_candidate_reports:
        raise StageValidationError(
            "Coordinator requested candidate packaging from incomplete assignment ID(s): "
            + ", ".join(unknown_candidate_reports)
        )
    if decision.candidate_packaging_recommended and not decision.candidate_report_ids:
        raise StageValidationError("Candidate packaging requires at least one completed report ID.")
    if decision.candidate_packaging_recommended and decision.assignments:
        raise StageValidationError(
            "Candidate packaging pauses admission and cannot add worker assignments."
        )
    if decision.stop_recommended and not (decision.stop_reason or "").strip():
        raise StageValidationError("A coordinator stop decision must include an exact reason.")
    if initial and decision.stop_recommended:
        raise StageValidationError(
            "The initial coordinator decision must launch the funded diverse portfolio."
        )
    if decision.stop_recommended and decision.candidate_packaging_recommended:
        raise StageValidationError(
            "Coordinator cannot recommend both candidate packaging and immediate stopping."
        )
    if (
        decision.claims_requiring_counterexample_search
        or decision.lemmas_requiring_proof_completion
    ) and not decision.assignments:
        raise StageValidationError(
            "Targeted counterexample or lemma directives require executable assignments."
        )
    return decision


async def run_adaptive_research(
    *,
    client: ModelClient,
    compiled_problem: CompiledProblem | PromptCompilationResult,
    research_dir: Path,
    workflow_settings: ResearchWorkflowSettings | None = None,
    coordinator_settings: ModelSettings | None = None,
    worker_settings: ModelSettings | None = None,
    audit_settings: ModelSettings | None = None,
    final_judge_settings: ModelSettings | None = None,
    coordinator_prompt_path: Path | None = None,
    worker_prompt_path: Path | None = None,
    candidate_prompt_path: Path | None = None,
    final_judge_prompt_path: Path | None = None,
    audit_prompt_paths: dict[str, Path] | None = None,
    source_verifier: IdentifierVerifier | None = None,
    remaining_run_model_calls: int | None = None,
    knowledge_graph: KnowledgeGraph | None = None,
    graph_problem_id: str | None = None,
    run_id: str | None = None,
    coordinator_can_read_files: bool = False,
    graph_replay_dir: Path | None = None,
    computation_backend: ExecutionBackend | None = None,
    computation_replay_isolation: ComputationReplayIsolation | None = None,
    progress: ProgressReporter = no_progress,
) -> ResearchResult:
    """Run a durable, event-driven research coordinator and the proof gate.

    The coordinator is one logical actor, but its provider calls may be fresh contexts.
    Correctness never depends on hidden provider memory: every call receives the complete
    governing prompt, claim contract, raw visible reports, registry, continuity snapshot,
    audit obligations, and all mailbox events it has not yet acknowledged.  Workers remain
    asynchronous, so one straggler never creates a planning barrier.
    """

    compiled = (
        compiled_problem.compiled_problem
        if isinstance(compiled_problem, PromptCompilationResult)
        else compiled_problem
    )
    if (knowledge_graph is None) != (graph_problem_id is None):
        raise ValueError("knowledge_graph and graph_problem_id must be provided together")
    if knowledge_graph is not None and not (run_id or "").strip():
        raise ValueError("graph-integrated research requires run_id")
    settings = workflow_settings or ResearchWorkflowSettings()
    if remaining_run_model_calls is not None and remaining_run_model_calls < 0:
        raise ValueError("remaining_run_model_calls must be nonnegative")
    coordinator_model = coordinator_settings or ModelSettings(reasoning_effort="max")
    worker_model = (worker_settings or ModelSettings(reasoning_effort="xhigh")).model_copy(
        update={"maximum_subagents": settings.hierarchical_subagent_limit}
    )
    auditor_model = audit_settings or ModelSettings(reasoning_effort="xhigh")
    judge_model = final_judge_settings or coordinator_model

    destination = ensure_stage_directory(research_dir)
    coordinator_dir = ensure_stage_directory(destination / "coordinator")
    decisions_dir = ensure_stage_directory(coordinator_dir / "decisions")
    requests_dir = ensure_stage_directory(coordinator_dir / "requests")
    context_manifests_dir = ensure_stage_directory(coordinator_dir / "context-manifests")
    context_catalogs_dir = ensure_stage_directory(coordinator_dir / "artifact-catalogs")
    events_dir = ensure_stage_directory(destination / "events")
    assignments_dir = ensure_stage_directory(destination / "assignments")
    workers_dir = ensure_stage_directory(destination / "workers")
    worker_evidence_dir = ensure_stage_directory(destination / "worker-evidence")
    worker_sources_dir = ensure_stage_directory(destination / "source-verification")
    worker_computation_dir = ensure_stage_directory(destination / "worker-computation")
    counterexample_audits_dir = ensure_stage_directory(destination / "counterexample-audits")
    lemma_audits_dir = ensure_stage_directory(destination / "lemma-audits")
    lemma_selections_dir = ensure_stage_directory(lemma_audits_dir / "selections")
    graph_patches_dir = ensure_stage_directory(destination / "graph-patches")
    issues_dir = ensure_stage_directory(destination / "issues")
    candidate_dir = ensure_stage_directory(destination / "candidate")
    audits_dir = ensure_stage_directory(destination / "audits")
    scheduler_path = coordinator_dir / "state.json"
    mailbox_path = coordinator_dir / "mailbox.json"
    registry_path = destination / "registry.json"
    continuity_path = destination / "continuity.json"
    scientific_phase_path = coordinator_dir / "scientific-phase.json"
    computation_store = ComputationArtifactStore(destination.parent)
    replay_isolation = computation_replay_isolation or ComputationReplayIsolation(
        filesystem_write_confined=False,
        network_disabled=False,
        description="No independently attested replay sandbox was configured.",
    )
    replay_scheduler: ResearchSchedulerState | None = None
    replay_root: Path | None = None
    if graph_replay_dir is not None:
        replay_root = graph_replay_dir.expanduser().resolve(strict=True)
        replay_scheduler = ResearchSchedulerState.model_validate_json(
            read_regular_text(replay_root / "coordinator" / "state.json")
        )

    coordinator_prompt = _read_prompt(coordinator_prompt_path, "research_coordinator.md")
    worker_prompt = _read_prompt(worker_prompt_path, "research_worker.md")
    packager_prompt = _read_prompt(candidate_prompt_path, "candidate_packager.md")
    judge_prompt = _read_prompt(final_judge_prompt_path, "final_judge.md")
    audit_names = ["foundational", "domain", "hostile", "sources"]
    audit_resources = {
        "foundational": "audit_foundational.md",
        "domain": "audit_domain.md",
        "hostile": "audit_hostile.md",
        "sources": "audit_sources.md",
        "complexity": "audit_complexity.md",
    }
    audit_instructions = {
        name: _read_prompt((audit_prompt_paths or {}).get(name), resource)
        for name, resource in audit_resources.items()
    }

    legacy_scheduler_path = destination / "scheduler_state.json"
    if legacy_scheduler_path.is_file() and not scheduler_path.is_file():
        raise StageValidationError(
            "Legacy research scheduler state is not a resumable continuous-coordinator "
            "checkpoint. Preserve the run, then use an explicit forced research generation."
        )
    resumed = scheduler_path.is_file()
    if resumed:
        try:
            scheduler = ResearchSchedulerState.model_validate_json(
                read_regular_text(scheduler_path)
            )
        except ValidationError as exc:
            raise StageValidationError(
                "Research scheduler format is incompatible or corrupt. Preserve the run, "
                "then use an explicit forced research stage to archive it and start a new "
                "scheduler generation."
            ) from exc
    else:
        scheduler = ResearchSchedulerState(compiled_problem_sha256=sha256_json(compiled))

    scientific_phase_state = (
        load_scientific_phase_state(scientific_phase_path)
        if scientific_phase_path.is_file()
        else ScientificPhaseState()
    )
    write_scientific_phase_state(scientific_phase_path, scientific_phase_state)

    compiled_digest = sha256_json(compiled)
    if scheduler.compiled_problem_sha256 is None:
        if scheduler.decisions or scheduler.assignments:
            raise StageValidationError(
                "Legacy research scheduler is not bound to its compiled problem."
            )
        scheduler.compiled_problem_sha256 = compiled_digest
    elif scheduler.compiled_problem_sha256 != compiled_digest:
        raise StageValidationError(
            "Research scheduler belongs to a different compiled problem; rerun with "
            "an explicit forced research generation."
        )
    if scheduler.latest_candidate_attempt is not None and (
        scheduler.latest_candidate_attempt_name != scheduler.latest_candidate_attempt.attempt_name
    ):
        raise StageValidationError("Latest candidate attempt metadata has inconsistent identity.")
    attempted_candidate_keys = [
        tuple(report_ids) for report_ids in scheduler.attempted_candidate_report_sets
    ]
    if any(
        not report_ids or list(report_ids) != sorted(set(report_ids))
        for report_ids in scheduler.attempted_candidate_report_sets
    ) or len(attempted_candidate_keys) != len(set(attempted_candidate_keys)):
        raise StageValidationError("Research candidate-attempt keys are invalid or duplicated.")

    # Finish the state-first event transaction before validating the ledger. A crash
    # may leave the canonical scheduler snapshot one event ahead, but never vice versa.
    if scheduler.pending_event is not None:
        pending_sequence = scheduler.pending_event.get("sequence")
        if not isinstance(pending_sequence, int) or pending_sequence < 1:
            raise StageValidationError("Research scheduler has an invalid pending event.")
        if pending_sequence != scheduler.next_event_sequence - 1:
            raise StageValidationError(
                "Research pending event does not match the scheduler event cursor."
            )
        _atomic_write_immutable_json(
            events_dir / f"{pending_sequence:08d}.json",
            scheduler.pending_event,
        )
        scheduler.pending_event = None
        atomic_write_json(scheduler_path, scheduler)

    event_numbers: list[int] = []
    events_by_sequence: dict[int, dict[str, object]] = {}
    for event_path in events_dir.glob("*.json"):
        try:
            event_numbers.append(int(event_path.stem))
        except ValueError as exc:
            raise StageValidationError(
                f"Invalid research event artifact name: {event_path.name}"
            ) from exc
    event_numbers.sort()
    if event_numbers:
        expected_numbers = list(range(1, event_numbers[-1] + 1))
        if event_numbers != expected_numbers:
            raise StageValidationError("Research event stream is not a contiguous durable prefix.")
        if scheduler.next_event_sequence != event_numbers[-1] + 1:
            raise StageValidationError(
                "Research coordinator state and immutable event cursor disagree."
            )
        for sequence in event_numbers:
            event_path = events_dir / f"{sequence:08d}.json"
            event = json.loads(read_regular_text(event_path))
            if not isinstance(event, dict) or event.get("sequence") != sequence:
                raise StageValidationError(f"Invalid research event: {event_path}")
            events_by_sequence[sequence] = event
            raw_artifact = event.get("artifact")
            raw_digest = event.get("artifact_sha256")
            referenced_artifacts: list[tuple[str, str]] = []
            if (raw_artifact is None) != (raw_digest is None):
                raise StageValidationError(
                    f"Research event has incomplete primary artifact metadata: {event_path}"
                )
            if raw_artifact is not None and (
                not isinstance(raw_artifact, str) or not isinstance(raw_digest, str)
            ):
                raise StageValidationError(f"Invalid research event: {event_path}")
            if isinstance(raw_artifact, str) and isinstance(raw_digest, str):
                referenced_artifacts.append((raw_artifact, raw_digest))
            raw_related = event.get("related_artifacts", [])
            if not isinstance(raw_related, list):
                raise StageValidationError(f"Invalid research event: {event_path}")
            for related in raw_related:
                if not isinstance(related, dict):
                    raise StageValidationError(f"Invalid research event: {event_path}")
                related_path = related.get("path")
                related_digest = related.get("sha256")
                if not isinstance(related_path, str) or not isinstance(related_digest, str):
                    raise StageValidationError(f"Invalid research event: {event_path}")
                referenced_artifacts.append((related_path, related_digest))
            for artifact_relative, artifact_digest in referenced_artifacts:
                referenced = (destination / artifact_relative).resolve()
                try:
                    referenced.relative_to(destination)
                except ValueError as exc:
                    raise StageValidationError(
                        f"Research event artifact escapes its stage: {artifact_relative}"
                    ) from exc
                if not referenced.is_file() or sha256_file(referenced) != artifact_digest:
                    raise StageValidationError(
                        f"Research event artifact is missing or changed: {artifact_relative}"
                    )
    elif scheduler.next_event_sequence != 1:
        raise StageValidationError(
            "Research coordinator state has an event cursor but no durable event stream."
        )
    if scheduler.coordinator_ack_event_sequence >= scheduler.next_event_sequence:
        raise StageValidationError(
            "Research coordinator acknowledgement is ahead of the durable event stream."
        )

    unverified_refutation_obligation = (
        "A coordinator or candidate-proof rejection is not an independently verified "
        "disproof of the frozen exact theorem contract. Continue research unless an "
        "application-owned exact-contract counterexample audit passes, or a real "
        "resource, integrity, or security boundary is reached."
    )
    resumed_unverified_refutation_reason: str | None = None
    resumed_unverified_refutation_decision_id: int | None = None
    if scheduler.final_outcome is None and scheduler.stop_category == "refuted":
        # Older v2 checkpoints could persist a model-only refutation as a terminal
        # scheduler action. No production lane independently audited such a claim, so
        # fail closed on resume and put the exact missing audit back in the mailbox.
        resumed_unverified_refutation_reason = (
            scheduler.stop_reason or "Unspecified persisted model-only refutation stop."
        )
        resumed_unverified_refutation_decision_id = (
            scheduler.decisions[-1].decision.decision_id if scheduler.decisions else None
        )
        scheduler.stop_reason = None
        scheduler.stop_category = None
        scheduler.repair_obligations = list(
            dict.fromkeys([*scheduler.repair_obligations, unverified_refutation_obligation])
        )
    if (
        scheduler.final_outcome is ResearchOutcome.REJECTED
        and scheduler.final_refutation_gate is None
    ):
        # No pre-lane rejection is trusted on resume.  Recover it as an ordinary open
        # research checkpoint rather than laundering a historic model verdict.
        scheduler.final_outcome = None
        scheduler.final_obligations = []
        scheduler.final_strongest_result = ""
        scheduler.phase = SchedulerPhase.RUNNING
        scheduler.repair_obligations = list(
            dict.fromkeys([*scheduler.repair_obligations, unverified_refutation_obligation])
        )

    accounted_key_lookup = getattr(client, "accounted_request_keys", None)
    recovered_response_map: dict[str, str] = (
        accounted_key_lookup(scheduler.model_call_keys) if callable(accounted_key_lookup) else {}
    )
    if callable(accounted_key_lookup):
        for call_key, response_id in scheduler.model_response_ids_by_call_key.items():
            if recovered_response_map.get(call_key) != response_id:
                raise StageValidationError(
                    "Research scheduler response identity is missing or inconsistent in "
                    "the durable model-call accounting journal."
                )
    recovered_scheduler_mapping = False
    for call_key, response_id in recovered_response_map.items():
        existing = scheduler.model_response_ids_by_call_key.get(call_key)
        if existing is not None and existing != response_id:
            raise StageValidationError(
                "Recovered model-call checkpoint conflicts with scheduler response identity."
            )
        if existing is None:
            if response_id in scheduler.response_ids:
                raise StageValidationError(
                    "Recovered model response is already bound to a different request."
                )
            scheduler.model_response_ids_by_call_key[call_key] = response_id
            scheduler.response_ids.append(response_id)
            recovered_scheduler_mapping = True
    if recovered_scheduler_mapping:
        # Heal the narrow crash boundary where accounting committed usage/cache but
        # the research actor had not yet copied the response identity into its state.
        atomic_write_json(scheduler_path, scheduler)

    resolved_remaining_run_calls = remaining_run_model_calls
    remaining_call_lookup = getattr(client, "remaining_model_calls", None)
    if callable(remaining_call_lookup):
        observed_remaining = remaining_call_lookup()
        if observed_remaining is not None and (
            not isinstance(observed_remaining, int) or observed_remaining < 0
        ):
            raise StageValidationError("Model client returned an invalid remaining-call budget.")
        resolved_remaining_run_calls = observed_remaining
    run_model_call_limit = (
        None
        if resolved_remaining_run_calls is None
        else len(scheduler.response_ids) + resolved_remaining_run_calls
    )
    tracker = _TrackedModelClient(
        client,
        run_model_call_limit,
        hard_maximum_calls=settings.maximum_model_calls,
        calls=scheduler.model_calls,
        response_ids=scheduler.response_ids,
        call_keys=scheduler.model_call_keys,
        response_ids_by_call_key=scheduler.model_response_ids_by_call_key,
    )
    pending_coordinator = scheduler.pending_coordinator_request
    if pending_coordinator is not None:
        pending_input = (
            serialize_coordinator_payload(pending_coordinator.request_payload)
            if pending_coordinator.context_manifest_path is not None
            else json.dumps(
                pending_coordinator.request_payload,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        frozen_request_key = tracker.request_key(
            instructions=coordinator_prompt,
            input_text=pending_input,
            settings=pending_coordinator.request_settings,
            output_type=ResearchCoordinatorDecision,
        )
        if (
            not tracker.has_call_key(frozen_request_key)
            and pending_coordinator.request_settings != coordinator_model
        ):
            # The state-first coordinator WAL may exist before the logical model
            # request is registered. Such an activation provably never reached a
            # provider, so a resumed policy override applies to it. Once its key is
            # registered, exact replay wins and the frozen settings are preserved.
            pending_coordinator.request_settings = coordinator_model.model_copy(deep=True)
            atomic_write_json(scheduler_path, scheduler)
    coordinator_client = _client_for_role(client, "research-coordinator")
    worker_client = _client_for_role(client, "research-worker")
    packager_client = _client_for_role(client, "candidate-packager")
    auditor_client = _client_for_role(client, "research-auditor")
    judge_client = _client_for_role(client, "research-final-judge")
    model_call_semaphore = asyncio.Semaphore(settings.maximum_concurrent_agents)

    async def generate_model(
        *,
        instructions: str,
        input_text: str,
        model_settings: ModelSettings,
        output_type: type[TModel],
        selected_client: ModelClient,
        reservation_key: str | None = None,
    ) -> ModelResult[TModel]:
        newly_registered = tracker.register_request(
            instructions=instructions,
            input_text=input_text,
            settings=model_settings,
            output_type=output_type,
            reservation_key=reservation_key,
        )
        if newly_registered:
            # Persist the logical request identity before entering provider code. A
            # crash can then replay this exact request without consuming budget twice.
            persist_scheduler()
        async with model_call_semaphore:
            return await tracker.generate(
                instructions=instructions,
                input_text=input_text,
                settings=model_settings,
                output_type=output_type,
                client=selected_client,
            )

    def tracked_role_client(role: str) -> _DelegatedModelClient:
        """Give each independent audit a fresh role/session and shared accounting."""

        selected_client = _client_for_role(client, role)

        async def invoke(
            request: ModelRequest,
            output_type: type[BaseModel],
        ) -> ModelResult[Any]:
            return await generate_model(
                instructions=request.instructions,
                input_text=request.input_text,
                model_settings=request.settings,
                output_type=output_type,
                selected_client=selected_client,
            )

        return _DelegatedModelClient(invoke)

    registry = ApproachRegistry()
    latest_continuity: ResearchContinuityState | None = None
    artifact_paths: dict[str, Path] = {}
    reports_by_id: dict[str, ResearchWorkerReport] = {}
    computation_evidence_by_id: dict[str, WorkerComputationEvidence] = {}
    phase_policy = settings.scientific_phase_policy

    def literature_refresh_payload() -> dict[str, object]:
        """Expose run-local verified literature without mutating the frozen theorem."""

        return {
            "literature_status": compiled.literature_status.value,
            "literature_resolution_summary": compiled.literature_resolution_summary,
            "verified_source_ledger": [
                item.model_dump(mode="json") for item in compiled.source_ledger
            ],
            "instruction": (
                "This is run-local literature evidence. It may inform attribution and imported "
                "lemmas, but it cannot rewrite the frozen exact statement or claim contract."
            ),
        }

    def active_scientific_cut_ids() -> list[str]:
        if knowledge_graph is None or graph_problem_id is None:
            return []
        return [
            item.matek_id
            for item in knowledge_graph.frontier(graph_problem_id).smallest_known_open_cut
        ]

    def current_scientific_obligation_versions() -> dict[str, str]:
        """Return current semantic contracts for every durable obligation-like target."""

        if knowledge_graph is None or graph_problem_id is None:
            return {}
        graph_state = knowledge_graph.load_state()
        return _scientific_target_versions(
            knowledge_graph.load_nodes(),
            graph_revision=graph_state.revision,
            problem_id=graph_problem_id,
            target_claim_id=knowledge_graph.main_claim_id(graph_problem_id),
        )

    def scientific_phase_payload(decision_id: int) -> dict[str, object]:
        """Freeze frontier state when replaying an archived research generation.

        A forced prompt-validation run intentionally reuses the prior research calls
        when the compiled target is byte-identical.  The persistent graph may have
        advanced after those calls (for example, the final gate may now trust the main
        target), so rebuilding the first coordinator input from the live frontier would
        silently give the same logical activation a new request identity.  Reuse only
        this server-authored field from the authenticated archived request; material
        target changes still build it from the current graph.
        """

        cut_ids = active_scientific_cut_ids()
        obligation_versions = current_scientific_obligation_versions()
        current: dict[str, object] = {
            "phase": scientific_phase_state.phase.value,
            "phase_epoch": scientific_phase_state.phase_epoch,
            "phase_concurrency": active_scientific_concurrency(),
            "minimal_open_cut_ids": cut_ids,
            "minimal_open_cut_versions": {
                obligation_id: obligation_versions[obligation_id]
                for obligation_id in cut_ids
                if obligation_id in obligation_versions
            },
            "assignments_without_audited_progress": (
                scientific_phase_state.assignments_without_audited_progress
            ),
            "unchanged_cut_snapshots": scientific_phase_state.unchanged_cut_snapshots,
            "instruction": (
                "Every assignment will be server-bound to this phase. In bottleneck and "
                "adversarial phases, aim complementary roles at the exact open-cut IDs "
                "and state a concrete mechanism delta from archived attempts. In "
                "synthesize, use only audit-passed premises. Exact or semantic duplicate "
                "assignments are merged or redirected before launch."
            ),
        }
        if (
            replay_root is None
            or replay_scheduler is None
            or replay_scheduler.compiled_problem_sha256 != compiled_digest
            or decision_id > len(replay_scheduler.decisions)
        ):
            return current
        archived = replay_scheduler.decisions[decision_id - 1]
        archived_request = (replay_root / archived.request_path).resolve(strict=True)
        try:
            archived_request.relative_to(replay_root)
        except ValueError as exc:
            raise StageValidationError(
                "Archived coordinator request escapes its research generation."
            ) from exc
        if sha256_file(archived_request) != archived.request_sha256:
            raise StageValidationError("Archived coordinator request changed before replay.")
        raw_payload = json.loads(read_regular_text(archived_request))
        if not isinstance(raw_payload, dict):
            raise StageValidationError("Archived coordinator request is not a JSON object.")
        raw_phase = raw_payload.get("scientific_phase_state")
        if raw_phase is None:
            return current
        if not isinstance(raw_phase, dict) or not all(isinstance(key, str) for key in raw_phase):
            raise StageValidationError(
                "Archived coordinator request has invalid scientific phase state."
            )
        return cast(dict[str, object], raw_phase)

    def active_scientific_concurrency() -> int:
        return min(
            settings.maximum_concurrent_agents,
            phase_policy.concurrency_for(scientific_phase_state.phase),
        )

    def normalize_scientific_assignments(
        decision: ResearchCoordinatorDecision,
    ) -> tuple[ResearchCoordinatorDecision, ScientificPhaseState]:
        """Bind proposed work to the durable phase and screen repetition pre-launch."""

        next_phase_state = scientific_phase_state.model_copy(deep=True)
        cut_ids = active_scientific_cut_ids()
        obligation_versions = current_scientific_obligation_versions()
        audited_ids = (
            [
                item.matek_id
                for item in knowledge_graph.frontier(graph_problem_id).strongest_audited_results
            ]
            if knowledge_graph is not None and graph_problem_id is not None
            else []
        )
        frontier_focus = focused_frontier_obligation(next_phase_state, cut_ids)
        admitted: list[ResearchAssignment] = []
        for assignment in decision.assignments:
            phase = next_phase_state.phase
            if phase is ScientificPhase.EXPLORE:
                role = ScientificRole.EXPLORER
            elif phase is ScientificPhase.CONSOLIDATE:
                role = ScientificRole.CONSOLIDATOR
            elif phase in {
                ScientificPhase.BOTTLENECK,
                ScientificPhase.ADVERSARIAL_AUDIT,
            }:
                if frontier_focus is None:
                    raise StageValidationError(
                        f"{phase.value} assignments require a nonempty canonical open cut"
                    )
                role = next_complementary_role(
                    next_phase_state,
                    phase=phase,
                    target_obligation_id=frontier_focus,
                )
            else:
                role = ScientificRole.SYNTHESIZER

            target_obligations: list[str] = []
            if phase in {
                ScientificPhase.BOTTLENECK,
                ScientificPhase.ADVERSARIAL_AUDIT,
            }:
                assert frontier_focus is not None
                # One decision is a complementary portfolio around one exact cut
                # obligation.  The durable role rotation continues across later
                # coordinator activations until that obligation leaves the cut.
                target_obligations = [frontier_focus]
                rebound_targets = [frontier_focus]
            else:
                rebound_targets = list(assignment.target_node_ids)
            mechanism_delta = assignment.mechanism_delta
            if (
                phase
                in {
                    ScientificPhase.BOTTLENECK,
                    ScientificPhase.ADVERSARIAL_AUDIT,
                }
                and not mechanism_delta
            ):
                next_phase_state = next_phase_state.model_copy(
                    update={
                        "assignment_dispositions": [
                            *next_phase_state.assignment_dispositions,
                            AssignmentDisposition(
                                disposition=DuplicateDisposition.REDIRECT,
                                assignment_id=assignment.id,
                                reason=(
                                    "Frontier assignment omitted its required model-authored "
                                    "mechanism delta and was not launched."
                                ),
                            ),
                        ]
                    }
                )
                continue
            rebound = assignment.model_copy(
                update={
                    "scientific_phase": phase,
                    "scientific_role": role,
                    "target_obligation_ids": target_obligations,
                    "target_obligation_versions": [
                        TargetObligationVersion(
                            obligation_id=obligation_id,
                            logical_version=obligation_versions[obligation_id],
                        )
                        for obligation_id in target_obligations
                    ],
                    "mechanism_delta": mechanism_delta,
                    "audited_premise_ids": (
                        audited_ids if phase is ScientificPhase.SYNTHESIZE else []
                    ),
                    "target_node_ids": rebound_targets,
                }
            )
            plan = ScientificTaskPlan(
                assignment_id=rebound.id,
                phase=rebound.scientific_phase,
                phase_epoch=next_phase_state.phase_epoch,
                role=rebound.scientific_role,
                target_obligation_ids=rebound.target_obligation_ids,
                target_obligation_versions=rebound.target_obligation_version_map,
                mechanism=f"{rebound.approach_family}: {rebound.task}",
                mechanism_delta=rebound.mechanism_delta,
                audited_premise_ids=rebound.audited_premise_ids,
            )
            next_phase_state, disposition = admit_assignment(
                next_phase_state,
                plan,
                active_cut_ids=cut_ids,
                active_cut_versions={
                    obligation_id: obligation_versions[obligation_id]
                    for obligation_id in cut_ids
                    if obligation_id in obligation_versions
                },
                policy=phase_policy,
            )
            if disposition.disposition is DuplicateDisposition.LAUNCH:
                admitted.append(rebound)
        return decision.model_copy(update={"assignments": admitted}), next_phase_state

    def record_phase_progress(*, synthesis_succeeded: bool = False) -> None:
        """Persist one graph/report-derived progress snapshot after worker admission."""

        nonlocal scientific_phase_state
        if knowledge_graph is None or graph_problem_id is None:
            return
        completed = [
            record
            for record in scheduler.assignments
            if record.status is AssignmentLifecycle.COMPLETED
        ]
        current_completed = _current_scientific_phase_completions(
            completed,
            scientific_phase_state,
        )
        counted_assignment_ids = set(scientific_phase_state.progress_counted_assignment_ids)
        new_current_completed = [
            record
            for record in current_completed
            if record.assignment.id not in counted_assignment_ids
        ]
        current_assignment_ids = {record.assignment.id for record in current_completed}
        frontier = knowledge_graph.frontier(graph_problem_id)
        ledger_revision = frontier.graph_revision
        graph_nodes = knowledge_graph.load_nodes()
        current_claim_ids = {
            node.matek_id
            for node in graph_nodes
            if node.problem_id == graph_problem_id
            and node.node_type is NodeType.CLAIM
            and node.metadata.get("matek_assignment_id") in current_assignment_ids
        }
        current_claim_ids.update(
            edge.target_id
            for node in graph_nodes
            if node.problem_id == graph_problem_id
            and node.node_type is NodeType.DERIVATION
            and node.metadata.get("matek_assignment_id") in current_assignment_ids
            for edge in node.relations
            if edge.relation is RelationType.PROVES
        )
        audited_hashes = [
            hashlib.sha256(f"{item.matek_id}\0{item.statement_version}".encode()).hexdigest()
            for item in frontier.strongest_audited_results
        ]
        current_audited_hashes = {
            hashlib.sha256(f"{item.matek_id}\0{item.statement_version}".encode()).hexdigest()
            for item in frontier.strongest_audited_results
            if item.matek_id in current_claim_ids
        }
        cut_ids = [item.matek_id for item in frontier.smallest_known_open_cut]
        prior_audited = (
            set(scientific_phase_state.snapshots[-1].audited_claim_hashes)
            if scientific_phase_state.snapshots
            else set()
        )
        new_current_audited = current_audited_hashes - prior_audited
        if not new_current_completed and not new_current_audited and not synthesis_succeeded:
            return
        current_reports = [
            reports_by_id[record.assignment.id]
            for record in current_completed
            if record.assignment.id in reports_by_id
        ]
        gaps = [
            gap
            for report in current_reports
            for gap in [
                *(result.exact_gap for result in report.results if result.exact_gap),
                *(item.exact_statement for item in report.unresolved_obligations),
            ]
        ]
        result_hashes = [
            hashlib.sha256(result.exact_statement.encode()).hexdigest()
            for report in current_reports
            for result in report.results
        ]
        recent = current_completed[-phase_policy.no_audited_progress_assignments :]
        recent_reports = [
            reports_by_id[item.assignment.id]
            for item in recent
            if item.assignment.id in reports_by_id
        ]
        recent_window_ready = len(current_completed) >= phase_policy.no_audited_progress_assignments
        similarity = 0.0
        if len(current_completed) >= phase_policy.no_audited_progress_assignments:
            mechanisms = [
                f"{item.assignment.approach_family}: {item.assignment.task}"
                for item in current_completed
            ]
            similarity = max(
                (
                    semantic_similarity(first, second)
                    for index, first in enumerate(mechanisms)
                    for second in mechanisms[index + 1 :]
                ),
                default=0.0,
            )
        adversarial_reports = [
            reports_by_id[item.assignment.id]
            for item in current_completed
            if item.assignment.scientific_phase is ScientificPhase.ADVERSARIAL_AUDIT
            and item.scientific_phase_epoch == scientific_phase_state.phase_epoch
            and item.assignment.id in reports_by_id
        ]
        synthesis_reports = [
            reports_by_id[item.assignment.id]
            for item in current_completed
            if item.assignment.scientific_phase is ScientificPhase.SYNTHESIZE
            and item.scientific_phase_epoch == scientific_phase_state.phase_epoch
            and item.assignment.id in reports_by_id
        ]
        snapshot = ScientificProgressSnapshot(
            sequence=(
                scientific_phase_state.snapshots[-1].sequence + 1
                if scientific_phase_state.snapshots
                else 1
            ),
            ledger_revision=ledger_revision,
            completed_assignment_count=(
                scientific_phase_state.completed_assignment_count + len(new_current_completed)
            ),
            new_audit_passed_count=len(new_current_audited),
            audited_claim_hashes=audited_hashes,
            minimal_open_cut_ids=cut_ids,
            normalized_exact_gaps=gaps,
            admitted_result_hashes=result_hashes,
            blocked_count=sum(
                report.branch_outcome is BranchOutcome.BLOCKED for report in recent_reports
            )
            if recent_window_ready
            else 0,
            refuted_count=sum(
                report.branch_outcome is BranchOutcome.REFUTED for report in recent_reports
            )
            if recent_window_ready
            else 0,
            recent_outcome_count=len(recent_reports) if recent_window_ready else 0,
            maximum_assignment_similarity=similarity,
            adversarial_audit_passed=_adversarial_audit_has_durable_pass_evidence(
                current_completed,
                reports_by_id,
                phase_epoch=scientific_phase_state.phase_epoch,
                active_cut_ids=cut_ids,
                current_obligation_versions=current_scientific_obligation_versions(),
            ),
            adversarial_audit_failed=any(
                report.branch_outcome is BranchOutcome.REFUTED for report in adversarial_reports
            ),
            synthesis_succeeded=synthesis_succeeded,
            synthesis_exact_gap=next(
                (report.exact_gap for report in reversed(synthesis_reports) if report.exact_gap),
                None,
            ),
        )
        scientific_phase_state = record_scientific_progress(
            scientific_phase_state,
            snapshot,
            policy=phase_policy,
        )
        scientific_phase_state = scientific_phase_state.model_copy(
            update={
                "progress_counted_assignment_ids": [
                    *scientific_phase_state.progress_counted_assignment_ids,
                    *(
                        record.assignment.id
                        for record in new_current_completed
                        if record.assignment.id
                        not in scientific_phase_state.progress_counted_assignment_ids
                    ),
                ]
            }
        )
        write_scientific_phase_state(scientific_phase_path, scientific_phase_state)

    def resolved_artifact(relative: str) -> Path:
        path = (destination / relative).resolve()
        try:
            path.relative_to(destination)
        except ValueError as exc:
            raise StageValidationError(
                f"Research scheduler artifact escapes its stage: {relative}"
            ) from exc
        return path

    def immutable_gate_checkpoint(gate_path: Path) -> Path:
        """Snapshot a replaceable BLOCKED gate under its exact content digest."""

        data = read_regular_bytes(gate_path)
        digest = hashlib.sha256(data).hexdigest()
        checkpoint = gate_path.parent / "gate-checkpoints" / f"{digest}.json"
        if checkpoint.exists():
            if read_regular_bytes(checkpoint) != data:
                raise StageValidationError(
                    "Immutable audit-gate checkpoint conflicts with its content digest."
                )
            return checkpoint
        return atomic_write_bytes(checkpoint, data)

    def gate_checkpoint_path(gate_path: str, gate_sha256: str) -> Path:
        canonical_gate = resolved_artifact(gate_path)
        return canonical_gate.parent / "gate-checkpoints" / f"{gate_sha256}.json"

    def gate_checkpoint_event(
        *,
        assignment_id: str,
        gate_path: str,
        gate_sha256: str,
        nomination_path: str,
        nomination_sha256: str,
        kinds: set[str],
    ) -> dict[str, object] | None:
        checkpoint = gate_checkpoint_path(gate_path, gate_sha256)
        checkpoint_relative = checkpoint.relative_to(destination).as_posix()
        expected_nomination = {
            "path": nomination_path,
            "sha256": nomination_sha256,
        }
        matches = [
            event
            for event in events_by_sequence.values()
            if event.get("kind") in kinds
            and event.get("assignment_id") == assignment_id
            and event.get("artifact") == checkpoint_relative
            and event.get("artifact_sha256") == gate_sha256
            and isinstance(event.get("related_artifacts"), list)
            and expected_nomination in cast(list[object], event["related_artifacts"])
        ]
        return matches[-1] if matches else None

    def lemma_gate_is_monotone_evidence_extension(
        previous: LemmaAuditGate,
        current: LemmaAuditGate,
    ) -> bool:
        """Accept only the gate transition produced by adding independent role evidence."""

        previous_roles = set(previous.response_sha256)
        current_roles = set(current.response_sha256)
        static_fields_match = (
            previous.schema_version == current.schema_version
            and previous.audit_id == current.audit_id
            and previous.input_sha256 == current.input_sha256
            and previous.statement_sha256 == current.statement_sha256
            and previous.result_kind is current.result_kind
            and previous.scope is current.scope
            and previous.execution_context_ids == current.execution_context_ids
        )
        previous_evidence_is_preserved = (
            previous_roles == set(previous.response_ids)
            and previous_roles < current_roles
            and all(
                current.response_sha256.get(role) == digest
                for role, digest in previous.response_sha256.items()
            )
            and all(
                current.response_ids.get(role) == response_id
                for role, response_id in previous.response_ids.items()
            )
            and all(
                current.provider_session_ids.get(role) == session_id
                for role, session_id in previous.provider_session_ids.items()
            )
        )
        previous_missing = set(previous.missing_roles)
        current_missing = set(current.missing_roles)
        return bool(
            previous.status is LemmaAuditGateStatus.BLOCKED
            and previous_missing
            and current_missing < previous_missing
            and static_fields_match
            and previous_evidence_is_preserved
        )

    def counterexample_gate_is_monotone_evidence_extension(
        previous: CounterexampleAuditGate,
        current: CounterexampleAuditGate,
    ) -> bool:
        """Accept only a strict extension of a frozen counterexample audit."""

        previous_roles = set(previous.response_evidence_sha256)
        current_roles = set(current.response_evidence_sha256)
        static_fields_match = (
            previous.schema_version == current.schema_version
            and previous.audit_id == current.audit_id
            and previous.nomination_sha256 == current.nomination_sha256
            and previous.target_statement_sha256 == current.target_statement_sha256
            and previous.policy_artifact_sha256 == current.policy_artifact_sha256
            and previous.request_artifact_sha256 == current.request_artifact_sha256
            and previous.execution_context_ids == current.execution_context_ids
        )
        previous_evidence_is_preserved = (
            previous_roles == set(previous.response_ids)
            and previous_roles < current_roles
            and all(
                current.response_evidence_sha256.get(role) == digest
                for role, digest in previous.response_evidence_sha256.items()
            )
            and all(
                current.response_ids.get(role) == response_id
                for role, response_id in previous.response_ids.items()
            )
            and all(
                current.provider_session_ids.get(role) == session_id
                for role, session_id in previous.provider_session_ids.items()
            )
        )
        previous_missing = set(previous.missing_roles)
        current_missing = set(current.missing_roles)
        return bool(
            previous.status is CounterexampleAuditGateStatus.BLOCKED
            and previous_missing
            and current_missing < previous_missing
            and static_fields_match
            and previous_evidence_is_preserved
        )

    def adopt_persisted_audit_responses(
        bindings: dict[str, tuple[ModelRequest, str]],
        output_type: type[BaseModel],
    ) -> None:
        for request, response_id in bindings.values():
            call_key = tracker.request_key(
                instructions=request.instructions,
                input_text=request.input_text,
                settings=request.settings,
                output_type=output_type,
            )
            tracker.bind_persisted_response(call_key, response_id)

    def replace_recovered_gate_obligations(
        event: dict[str, object],
        current_obligations: list[str],
    ) -> None:
        detail = event.get("detail", [])
        detail_items = detail if isinstance(detail, list) else []
        prior = {
            str(item)
            for item in detail_items
            if isinstance(item, str) and item in scheduler.repair_obligations
        }
        scheduler.repair_obligations = list(
            dict.fromkeys(
                [
                    *(item for item in scheduler.repair_obligations if item not in prior),
                    *current_obligations,
                ]
            )
        )

    def checkpoint_recovered_audit_state() -> None:
        scheduler.model_calls = tracker.calls
        scheduler.model_call_keys = list(tracker.call_keys)
        scheduler.model_response_ids_by_call_key = dict(tracker.response_ids_by_call_key)
        scheduler.response_ids = list(dict.fromkeys(tracker.response_ids))
        atomic_write_json(scheduler_path, scheduler)

    def assignment_input(record: ResearchAssignmentState) -> str:
        nested_limit = (
            record.request_settings.maximum_subagents
            if record.request_settings is not None
            else settings.hierarchical_subagent_limit
        )
        payload: dict[str, object] = {
            "compiled_prompt": compiled.compiled_prompt,
            "claim_contract": compiled.claim_contract.as_dict(),
            "literature_refresh": literature_refresh_payload(),
            "assignment": record.assignment.model_dump(mode="json"),
            "admitted_by_coordinator_decision": record.admitted_by_decision,
            "repair_generation": record.repair_generation,
            "scientific_phase_contract": {
                "phase": record.assignment.scientific_phase.value,
                "phase_epoch": record.scientific_phase_epoch,
                "role": record.assignment.scientific_role.value,
                "target_obligation_ids": record.assignment.target_obligation_ids,
                "target_obligation_versions": [
                    item.model_dump(mode="json")
                    for item in record.assignment.target_obligation_versions
                ],
                "mechanism_delta": record.assignment.mechanism_delta,
                "audited_premise_ids": record.assignment.audited_premise_ids,
                "instruction": (
                    "Work only in the assigned phase and role. A nested contribution has no "
                    "extra epistemic weight. Report whether the exact open-cut obligation was "
                    "reduced and distinguish a new mechanism from archived attempts."
                ),
            },
            "private_artifact_workspace": {
                "enabled": callable(getattr(worker_client, "for_workspace", None)),
                "writable_relative_path": "scratch",
                "declaration_path_base": "scratch",
                "instruction": (
                    "When enabled, write computation code, inputs, expected stdout/stderr, and "
                    "certificate outputs only beneath ./scratch. In artifact_manifest, paths "
                    "are relative to ./scratch (omit the scratch/ prefix). Declare every file; "
                    "MATEK rejects symlinks, undeclared files, quota excess, worker-supplied "
                    "digests, and unreplayed computation claims."
                ),
            },
            "agent_hierarchy": (
                {
                    "role": "hierarchical_research_subagent",
                    "max_concurrent_agents": settings.max_concurrent_agents,
                    "max_concurrent_first_level_agents": settings.maximum_concurrent_agents,
                    "subagents_per_agent": nested_limit,
                    "max_nested_agent_depth": 1,
                    "instruction": (
                        f"You may spawn up to {nested_limit} sub-subagents for independent "
                        "bounded parts of this assignment. You remain responsible for "
                        "checking and synthesizing their work into this one scientific report. "
                        "Tell every spawned agent that it may not delegate further."
                    ),
                }
                if nested_limit > 0
                else {
                    "role": "regular_research_subagent",
                    "instruction": (
                        "You are a regular research subagent. Complete this assignment "
                        "yourself; no nested delegation is configured."
                    ),
                }
            ),
        }
        if record.exact_target_policy_version == 1:
            payload["exact_target_policy"] = exact_target_policy()
        if record.repair_generation:
            payload["recovery_instruction"] = (
                "This is the one bounded schema/execution repair generation. Return a valid "
                "typed scientific report for the same assignment. Do not return persistence "
                "identities or graph mutations."
            )
        if record.graph_context is not None:
            payload.update(
                {
                    "knowledge_graph_context": record.graph_context,
                    "graph_task_id": record.graph_task_id,
                    "base_graph_revision": record.graph_revision,
                    "branch_work_contract": {
                        "contract_version": record.graph_contract_version,
                        "target_node_ids": record.assignment.target_node_ids,
                        "scope_rule": (
                            "Treat these target nodes and the exact task as this assignment's "
                            "branch boundary. Work deeply on that branch. Record adjacent useful "
                            "facts without silently changing the assigned objective."
                        ),
                        "negative_result_rule": (
                            "If this branch cannot work, use blocked for a precise missing "
                            "statement or refuted only for a concrete obstruction to this "
                            "assigned branch. State the exact failure and evidence. Do not claim "
                            "that the main theorem is false merely because a strengthening, "
                            "lemma, or mechanism fails."
                        ),
                        "reopen_rule": (
                            "For blocked or refuted work, make the typed result evidence and "
                            "unresolved obligations identify what would justify reopening the "
                            "branch."
                        ),
                    },
                    "scientific_result_contract": (
                        "Return schema-version-2 typed scientific results, obligations, sources, "
                        "and artifact declarations only. MATEK owns run/task identities, stable "
                        "graph IDs, revisions, provenance, status promotion, and relation "
                        "directions. Do not return any persistence mutation payload."
                    ),
                }
            )
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def candidate_gate_policy_payload(attempt: CandidateAttemptState) -> dict[str, object]:
        """Add the policy to new gates without changing identities of legacy resumptions."""

        payload: dict[str, object] = {}
        if attempt.exact_target_policy_version == 1:
            payload["exact_target_policy"] = exact_target_policy()
        if attempt.computation_gate_version == 1:
            payload["candidate_computation_gate"] = {
                "policy": (
                    "Every computation result in a triggering report is scope=computation, "
                    "proposed_complete, backed by a canonical collection manifest and passed "
                    "isolated replay, and belongs to the declared transitive "
                    "dependency_result_keys closure of a separate exact-main proof or reduction "
                    "result. Audit domain completeness independently."
                ),
                "bindings": [
                    binding.model_dump(mode="json") for binding in attempt.computation_bindings
                ],
            }
        if attempt.graph_support_gate_version == 1:
            payload["candidate_canonical_graph_support"] = {
                "policy": (
                    "When a knowledge graph is active, package only the exact-main proposed "
                    "derivations and their application-resolved local-result closure admitted by "
                    "MATEK's canonical ledger. Bind replay-backed computation derivations and "
                    "artifacts; treat every live linked obligation as blocking."
                ),
                "bindings": [
                    binding.model_dump(mode="json") for binding in attempt.graph_support_bindings
                ],
            }
        return payload

    pending_headroom_id = (
        scheduler.pending_coordinator_request.headroom_assignment_id
        if scheduler.pending_coordinator_request is not None
        else None
    )
    pending_headroom_key = (
        scheduler.pending_coordinator_request.headroom_worker_request_key
        if scheduler.pending_coordinator_request is not None
        else None
    )
    if pending_headroom_id is not None and (
        scheduler.assignment_record(pending_headroom_id) is None
    ):
        raise StageValidationError("Pending coordinator headroom references an unknown assignment.")
    for record in scheduler.assignments:
        computation_store.prepare_workspace(record.assignment.id)
        if record.status == AssignmentLifecycle.RUNNING:
            # A process restart has no live coroutine. Reissuing the identical request
            # lets AccountingModelClient replay a completed call or safely resume work.
            record.status = AssignmentLifecycle.QUEUED
        if record.status == AssignmentLifecycle.COMPLETED and (
            record.request_key is None
            or record.response_id is None
            or record.report_path is None
            or record.report_sha256 is None
            or (
                record.worker_report_schema_version == 2
                and (record.raw_report_path is None or record.raw_report_sha256 is None)
            )
            or record.completed_event_sequence is None
            or (
                knowledge_graph is not None
                and (record.graph_patch_path is None or record.graph_patch_sha256 is None)
            )
        ):
            raise StageValidationError(
                f"Completed assignment {record.assignment.id!r} has incomplete evidence metadata."
            )
        is_borrowed_headroom = (
            record.assignment.id == pending_headroom_id
            and record.status == AssignmentLifecycle.QUEUED
            and not record.launched
            and record.request_key is None
        )
        if record.request_settings is None:
            raise StageValidationError(
                f"Assignment {record.assignment.id!r} has no frozen model settings."
            )
        if record.status in {AssignmentLifecycle.QUEUED, AssignmentLifecycle.RUNNING} and (
            record.request_key is None and not is_borrowed_headroom
        ):
            raise StageValidationError(
                f"Open assignment {record.assignment.id!r} has no reserved model request."
            )
        if is_borrowed_headroom:
            expected_headroom_key = tracker.request_key(
                instructions=worker_prompt,
                input_text=assignment_input(record),
                settings=record.request_settings,
                output_type=ResearchWorkerReport,
            )
            if pending_headroom_key != expected_headroom_key or tracker.has_call_key(
                expected_headroom_key
            ):
                raise StageValidationError(
                    "Pending coordinator headroom has inconsistent worker-request metadata."
                )
        if record.request_key is not None and not tracker.has_call_key(record.request_key):
            raise StageValidationError(
                f"Assignment {record.assignment.id!r} references an unknown model request."
            )
        if (
            record.status == AssignmentLifecycle.QUEUED
            and not record.launched
            and not is_borrowed_headroom
            and record.request_settings != worker_model
        ):
            assert record.request_key is not None
            tracker.release_call_key(record.request_key)
            record.request_settings = worker_model.model_copy(deep=True)
            worker_input = assignment_input(record)
            record.request_key = tracker.request_key(
                instructions=worker_prompt,
                input_text=worker_input,
                settings=record.request_settings,
                output_type=ResearchWorkerReport,
            )
            tracker.register_request(
                instructions=worker_prompt,
                input_text=worker_input,
                settings=record.request_settings,
                output_type=ResearchWorkerReport,
            )
        if record.response_id is not None and (
            record.request_key is None
            or tracker.response_ids_by_call_key.get(record.request_key) != record.response_id
        ):
            raise StageValidationError(
                f"Assignment {record.assignment.id!r} response is not bound to its request."
            )
        if record.status != AssignmentLifecycle.COMPLETED and record.report_path is not None:
            raise StageValidationError(
                f"Non-completed assignment {record.assignment.id!r} references a report."
            )
        if record.report_path is None:
            continue
        if record.worker_report_schema_version == 2:
            assert record.raw_report_path is not None
            raw_report_path = resolved_artifact(record.raw_report_path)
            if (
                not raw_report_path.is_file()
                or record.raw_report_sha256 is None
                or sha256_file(raw_report_path) != record.raw_report_sha256
            ):
                raise StageValidationError(
                    f"Raw research worker report changed after checkpoint: {record.assignment.id}"
                )
        report_path = resolved_artifact(record.report_path)
        if not report_path.is_file():
            raise StageValidationError(
                f"Completed assignment {record.assignment.id!r} has no durable report."
            )
        if record.report_sha256 and sha256_file(report_path) != record.report_sha256:
            raise StageValidationError(
                f"Research worker report changed after checkpoint: {record.assignment.id}"
            )
        report = load_research_worker_report_json(read_regular_text(report_path))
        if report.assignment_id != record.assignment.id:
            raise StageValidationError(
                f"Research worker report ID does not match assignment {record.assignment.id!r}."
            )
        if record.graph_patch_path is not None:
            graph_patch_path = resolved_artifact(record.graph_patch_path)
            if (
                not graph_patch_path.is_file()
                or record.graph_patch_sha256 is None
                or sha256_file(graph_patch_path) != record.graph_patch_sha256
            ):
                raise StageValidationError(
                    f"Research graph patch record changed after checkpoint: {record.assignment.id}"
                )
        if (record.computation_evidence_path is None) != (
            record.computation_evidence_sha256 is None
        ):
            raise StageValidationError(
                f"Research computation evidence is incomplete: {record.assignment.id}"
            )
        if record.computation_evidence_path is not None:
            computation_path = resolved_artifact(record.computation_evidence_path)
            if (
                not computation_path.is_file()
                or record.computation_evidence_sha256 is None
                or sha256_file(computation_path) != record.computation_evidence_sha256
            ):
                raise StageValidationError(
                    f"Research computation evidence changed after checkpoint: "
                    f"{record.assignment.id}"
                )
            computation_evidence = WorkerComputationEvidence.model_validate_json(
                read_regular_text(computation_path)
            )
            if computation_evidence.assignment_id != record.assignment.id:
                raise StageValidationError(
                    f"Research computation evidence belongs to another assignment: "
                    f"{record.assignment.id}"
                )
            computation_evidence_by_id[record.assignment.id] = computation_evidence
        audited_result_keys = [audit.result_local_key for audit in record.intermediate_lemma_audits]
        if len(audited_result_keys) != len(set(audited_result_keys)):
            raise StageValidationError(
                f"Research assignment {record.assignment.id!r} repeats a lemma audit."
            )
        for audit_record in record.intermediate_lemma_audits:
            nomination_path = resolved_artifact(audit_record.nomination_path)
            gate_path = resolved_artifact(audit_record.gate_path)
            if not nomination_path.is_file() or (
                sha256_file(nomination_path) != audit_record.nomination_sha256
            ):
                raise StageValidationError(
                    f"Intermediate lemma audit changed after checkpoint: "
                    f"{audit_record.nomination_id}"
                )
            persisted_lemma_nomination = LemmaNomination.model_validate_json(
                read_regular_text(nomination_path)
            )
            if not gate_path.is_file():
                raise StageValidationError(
                    f"Intermediate lemma audit changed after checkpoint: "
                    f"{audit_record.nomination_id}"
                )
            current_gate_sha256 = sha256_file(gate_path)
            if current_gate_sha256 != audit_record.gate_sha256:
                checkpoint_event = gate_checkpoint_event(
                    assignment_id=record.assignment.id,
                    gate_path=audit_record.gate_path,
                    gate_sha256=audit_record.gate_sha256,
                    nomination_path=audit_record.nomination_path,
                    nomination_sha256=audit_record.nomination_sha256,
                    kinds={
                        "intermediate_lemma_audit_incomplete",
                        "intermediate_lemma_audit_resumed",
                        "intermediate_lemma_audit_retry_checkpointed",
                    },
                )
                checkpoint_path = gate_checkpoint_path(
                    audit_record.gate_path,
                    audit_record.gate_sha256,
                )
                if (
                    audit_record.gate_status is not LemmaAuditGateStatus.BLOCKED
                    or audit_record.graph_recorded
                    or checkpoint_event is None
                    or not checkpoint_path.is_file()
                    or sha256_file(checkpoint_path) != audit_record.gate_sha256
                ):
                    raise StageValidationError(
                        f"Intermediate lemma audit changed after checkpoint: "
                        f"{audit_record.nomination_id}"
                    )
                previous_lemma_gate = LemmaAuditGate.model_validate_json(
                    read_regular_text(checkpoint_path)
                )
                _, gate = verify_persisted_lemma_audit(nomination_path, gate_path)
                if not lemma_gate_is_monotone_evidence_extension(previous_lemma_gate, gate):
                    raise StageValidationError(
                        "Intermediate lemma audit did not advance by a strict authenticated "
                        "role-evidence extension."
                    )
                adopt_persisted_audit_responses(
                    persisted_lemma_audit_response_bindings(nomination_path, gate_path),
                    LemmaAuditResponse,
                )
                audit_record.gate_status = gate.status
                audit_record.gate_sha256 = current_gate_sha256
                audit_record.graph_recorded = False
                replace_recovered_gate_obligations(checkpoint_event, gate.obligations)
                checkpoint_recovered_audit_state()
            else:
                input_payload = json.loads(read_regular_text(gate_path.parent / "input.json"))
                gate_payload = json.loads(read_regular_text(gate_path))
                input_schema = (
                    input_payload.get("schema_version") if isinstance(input_payload, dict) else None
                )
                gate_schema = (
                    gate_payload.get("schema_version") if isinstance(gate_payload, dict) else None
                )
                if input_schema == 1 and gate_schema == 1:
                    # Schema-v1 evidence never grants graph trust.  Preserve its bytes and
                    # identity here; ``upgrade_legacy_intermediate_gate`` archives it and reruns
                    # both roles under v2 before any promotion.  Do not demand v2 request hashes
                    # from the intentionally weaker legacy envelope.
                    if (
                        input_payload.get("audit_id") != audit_record.nomination_id
                        or gate_payload.get("audit_id") != audit_record.nomination_id
                    ):
                        raise StageValidationError(
                            "Legacy intermediate lemma audit has inconsistent identity."
                        )
                    gate = LemmaAuditGate.model_validate(gate_payload)
                elif input_schema != gate_schema:
                    raise StageValidationError(
                        "Intermediate lemma-audit input and gate schema versions differ."
                    )
                else:
                    _, gate = verify_persisted_lemma_audit(nomination_path, gate_path)
            if (
                persisted_lemma_nomination.nomination_id != audit_record.nomination_id
                or gate.audit_id != audit_record.nomination_id
                or gate.status is not audit_record.gate_status
                or (
                    audit_record.target_obligation_ids
                    and audit_record.target_obligation_ids
                    != persisted_lemma_nomination.target_obligation_ids
                )
                or (
                    audit_record.target_obligation_versions
                    and audit_record.target_obligation_versions
                    != {
                        item.obligation_id: item.logical_version
                        for item in persisted_lemma_nomination.target_obligation_contracts
                    }
                )
            ):
                raise StageValidationError(
                    f"Intermediate lemma audit metadata is inconsistent: "
                    f"{audit_record.nomination_id}"
                )
        audited_counterexample_keys = [
            audit.result_local_key
            for audit in record.exact_counterexample_audits
            if not audit.superseded
        ]
        if len(audited_counterexample_keys) != len(set(audited_counterexample_keys)):
            raise StageValidationError(
                f"Research assignment {record.assignment.id!r} repeats a counterexample audit."
            )
        for counterexample_audit_record in record.exact_counterexample_audits:
            nomination_path = resolved_artifact(counterexample_audit_record.nomination_path)
            gate_path = resolved_artifact(counterexample_audit_record.gate_path)
            if not nomination_path.is_file() or (
                sha256_file(nomination_path) != counterexample_audit_record.nomination_sha256
            ):
                raise StageValidationError(
                    "Exact-counterexample audit changed after checkpoint: "
                    f"{counterexample_audit_record.audit_id}"
                )
            if not gate_path.is_file():
                raise StageValidationError(
                    "Exact-counterexample audit changed after checkpoint: "
                    f"{counterexample_audit_record.audit_id}"
                )
            current_gate_sha256 = sha256_file(gate_path)
            if current_gate_sha256 != counterexample_audit_record.gate_sha256:
                checkpoint_event = gate_checkpoint_event(
                    assignment_id=record.assignment.id,
                    gate_path=counterexample_audit_record.gate_path,
                    gate_sha256=counterexample_audit_record.gate_sha256,
                    nomination_path=counterexample_audit_record.nomination_path,
                    nomination_sha256=counterexample_audit_record.nomination_sha256,
                    kinds={
                        "main_counterexample_audit_not_verified",
                        "main_counterexample_audit_retry_checkpointed",
                    },
                )
                checkpoint_path = gate_checkpoint_path(
                    counterexample_audit_record.gate_path,
                    counterexample_audit_record.gate_sha256,
                )
                if (
                    counterexample_audit_record.gate_status
                    is not CounterexampleAuditGateStatus.BLOCKED
                    or counterexample_audit_record.superseded
                    or checkpoint_event is None
                    or not checkpoint_path.is_file()
                    or sha256_file(checkpoint_path) != counterexample_audit_record.gate_sha256
                ):
                    raise StageValidationError(
                        "Exact-counterexample audit changed after checkpoint: "
                        f"{counterexample_audit_record.audit_id}"
                    )
                previous_counterexample_gate = CounterexampleAuditGate.model_validate_json(
                    read_regular_text(checkpoint_path)
                )
                support_invalidated = False
                try:
                    persisted_nomination, persisted_gate = verify_persisted_counterexample_audit(
                        nomination_path,
                        gate_path,
                        expected_target_statement=compiled.normalized_statement,
                    )
                except CounterexampleSupportInvalidated:
                    support_invalidated = True
                    persisted_nomination, persisted_gate = verify_persisted_counterexample_audit(
                        nomination_path,
                        gate_path,
                        expected_target_statement=compiled.normalized_statement,
                        allow_invalidated_graph_support=True,
                    )
                if not counterexample_gate_is_monotone_evidence_extension(
                    previous_counterexample_gate,
                    persisted_gate,
                ):
                    raise StageValidationError(
                        "Exact-counterexample audit did not advance by a strict authenticated "
                        "role-evidence extension."
                    )
                adopt_persisted_audit_responses(
                    persisted_counterexample_audit_response_bindings(
                        nomination_path,
                        gate_path,
                        expected_target_statement=compiled.normalized_statement,
                        allow_invalidated_graph_support=support_invalidated,
                    ),
                    CounterexampleAuditResponse,
                )
                counterexample_audit_record.gate_status = persisted_gate.status
                counterexample_audit_record.gate_sha256 = current_gate_sha256
                if support_invalidated:
                    counterexample_audit_record.superseded = True
                    counterexample_audit_record.superseded_reason = (
                        "Frozen canonical support changed after the audit gate advanced."
                    )
                    replace_recovered_gate_obligations(checkpoint_event, [])
                else:
                    replace_recovered_gate_obligations(
                        checkpoint_event,
                        persisted_gate.obligations,
                    )
                checkpoint_recovered_audit_state()
            else:
                persisted_nomination, persisted_gate = verify_persisted_counterexample_audit(
                    nomination_path,
                    gate_path,
                    expected_target_statement=compiled.normalized_statement,
                    allow_invalidated_graph_support=(
                        counterexample_audit_record.superseded
                        or counterexample_audit_record.gate_status
                        is CounterexampleAuditGateStatus.BLOCKED
                    ),
                )
            if (
                persisted_nomination.audit_id != counterexample_audit_record.audit_id
                or persisted_nomination.result_local_key
                != counterexample_audit_record.result_local_key
                or persisted_nomination.assignment_id != record.assignment.id
                or persisted_gate.status is not counterexample_audit_record.gate_status
            ):
                raise StageValidationError(
                    "Exact-counterexample audit metadata is inconsistent: "
                    f"{counterexample_audit_record.audit_id}"
                )
        reports_by_id[record.assignment.id] = report
        completion_event = events_by_sequence.get(record.completed_event_sequence or 0)
        if (
            completion_event is None
            or completion_event.get("kind") != "worker_report_accepted"
            or completion_event.get("assignment_id") != record.assignment.id
            or completion_event.get("artifact_sha256") != record.report_sha256
        ):
            raise StageValidationError(
                f"Completed assignment {record.assignment.id!r} has no matching event."
            )
        registry.update(record.assignment, report)

    for expected_id, decision_record in enumerate(scheduler.decisions, start=1):
        if decision_record.decision.decision_id != expected_id:
            raise StageValidationError("Research coordinator decision IDs are not contiguous.")
        decision_path = decisions_dir / f"{expected_id:08d}.json"
        if not decision_path.is_file():
            raise StageValidationError(
                f"Research coordinator decision artifact is missing: {decision_path}"
            )
        persisted_decision = ResearchCoordinatorDecision.model_validate_json(
            read_regular_text(decision_path)
        )
        if persisted_decision != decision_record.decision:
            raise StageValidationError(
                f"Research coordinator decision {expected_id} changed after checkpoint."
            )
        if not re.fullmatch(
            rf"coordinator/requests/{expected_id:08d}"
            r"(?:-(?:bounded|rebuild)-[0-9]{2})?\.json",
            decision_record.request_path,
        ):
            raise StageValidationError(
                f"Research coordinator request {expected_id} has an invalid artifact path."
            )
        request_path = resolved_artifact(decision_record.request_path)
        if (
            not request_path.is_file()
            or sha256_file(request_path) != decision_record.request_sha256
        ):
            raise StageValidationError(
                f"Research coordinator request {expected_id} is missing or changed."
            )
        request_payload = json.loads(read_regular_text(request_path))
        if not isinstance(request_payload, dict):
            raise StageValidationError(
                f"Research coordinator request {expected_id} is not a JSON object."
            )
        serialized_request = (
            serialize_coordinator_payload(request_payload)
            if decision_record.context_manifest_path is not None
            else json.dumps(request_payload, ensure_ascii=False, sort_keys=True)
        )
        expected_request_key = tracker.request_key(
            instructions=coordinator_prompt,
            input_text=serialized_request,
            settings=decision_record.request_settings,
            output_type=ResearchCoordinatorDecision,
        )
        if (
            expected_request_key != decision_record.request_key
            or not tracker.has_call_key(expected_request_key)
            or tracker.response_ids_by_call_key.get(expected_request_key)
            != decision_record.response_id
        ):
            raise StageValidationError(
                f"Research coordinator decision {expected_id} is not bound to its request."
            )
        if (decision_record.context_manifest_path is None) != (
            decision_record.context_manifest_sha256 is None
        ):
            raise StageValidationError(
                f"Research coordinator decision {expected_id} has partial context metadata."
            )
        if decision_record.context_manifest_path is not None:
            manifest_path = resolved_artifact(decision_record.context_manifest_path)
            if (
                not manifest_path.is_file()
                or sha256_file(manifest_path) != decision_record.context_manifest_sha256
            ):
                raise StageValidationError(
                    f"Research coordinator context {expected_id} is missing or changed."
                )
            manifest = CoordinatorContextManifest.model_validate_json(
                read_regular_text(manifest_path)
            )
            if (
                manifest.decision_id != expected_id
                or manifest.after_event_sequence != decision_record.decision.after_event_sequence
                or manifest.payload_sha256 != sha256_text(serialized_request)
            ):
                raise StageValidationError(
                    f"Research coordinator context {expected_id} is bound to other evidence."
                )
        if not any(
            event.get("kind") == "coordinator_decision"
            and event.get("decision_id") == expected_id
            and event.get("response_id") == decision_record.response_id
            and event.get("artifact_sha256") == sha256_file(decision_path)
            for event in events_by_sequence.values()
        ):
            raise StageValidationError(
                f"Research coordinator decision {expected_id} has no matching event."
            )

    launched_phase_ids = {
        item.assignment_id for item in scientific_phase_state.launched_assignments
    }
    recovered_phase_plans = [
        ScientificTaskPlan(
            assignment_id=record.assignment.id,
            phase=record.assignment.scientific_phase,
            phase_epoch=record.scientific_phase_epoch,
            role=record.assignment.scientific_role,
            target_obligation_ids=record.assignment.target_obligation_ids,
            target_obligation_versions=record.assignment.target_obligation_version_map,
            mechanism=(f"{record.assignment.approach_family}: {record.assignment.task}"),
            mechanism_delta=record.assignment.mechanism_delta,
            audited_premise_ids=record.assignment.audited_premise_ids,
        )
        for record in scheduler.assignments
        if record.assignment.id not in launched_phase_ids
    ]
    if recovered_phase_plans:
        scientific_phase_state = scientific_phase_state.model_copy(
            update={
                "launched_assignments": [
                    *scientific_phase_state.launched_assignments,
                    *recovered_phase_plans,
                ]
            }
        )
        write_scientific_phase_state(scientific_phase_path, scientific_phase_state)
    completed_on_resume = sum(
        record.status is AssignmentLifecycle.COMPLETED for record in scheduler.assignments
    )
    if (
        not scientific_phase_state.progress_counted_assignment_ids
        and scientific_phase_state.completed_assignment_count > 0
    ):
        legacy_counted_records = sorted(
            (
                record
                for record in scheduler.assignments
                if record.status is AssignmentLifecycle.COMPLETED
            ),
            key=lambda record: record.completed_event_sequence or 0,
        )
        if len(legacy_counted_records) < scientific_phase_state.completed_assignment_count:
            raise StageValidationError(
                "Scientific phase completion count exceeds the durable worker-report ledger."
            )
        scientific_phase_state = scientific_phase_state.model_copy(
            update={
                "progress_counted_assignment_ids": [
                    record.assignment.id
                    for record in legacy_counted_records[
                        : scientific_phase_state.completed_assignment_count
                    ]
                ]
            }
        )
        write_scientific_phase_state(scientific_phase_path, scientific_phase_state)
    if scientific_phase_state.completed_assignment_count > completed_on_resume:
        raise StageValidationError(
            "Scientific phase state is ahead of the durable worker-report ledger."
        )
    if scientific_phase_state.completed_assignment_count < completed_on_resume:
        record_phase_progress()

    selected_attempt_name = (
        scheduler.active_candidate_attempt.attempt_name
        if scheduler.active_candidate_attempt is not None
        else scheduler.latest_candidate_attempt_name
    )
    selected_attempt_dir = (
        candidate_dir / "attempts" / selected_attempt_name
        if selected_attempt_name is not None
        else None
    )
    selected_package_path = (
        selected_attempt_dir / "package.json"
        if selected_attempt_dir is not None
        else candidate_dir / "package.json"
    )
    current_candidate = (
        CandidateProofPackage.model_validate_json(read_regular_text(selected_package_path))
        if selected_package_path.is_file()
        else None
    )
    current_audits: dict[str, AuditVerdict] = {}
    for name in (*audit_names, "complexity"):
        audit_path = (
            audits_dir / "attempts" / selected_attempt_name / f"{name}.json"
            if selected_attempt_name is not None
            else audits_dir / f"{name}.json"
        )
        if audit_path.is_file():
            current_audits[name] = AuditVerdict.model_validate_json(read_regular_text(audit_path))
    selected_verdict_path = (
        selected_attempt_dir / "verdict.json"
        if selected_attempt_dir is not None
        else destination / "verdict.json"
    )
    current_verdict = (
        FinalJudgeVerdict.model_validate_json(read_regular_text(selected_verdict_path))
        if selected_verdict_path.is_file()
        else None
    )
    final_judge_response_id = ""
    repair_rounds = scheduler.failed_candidate_attempts

    def sync_tracker() -> None:
        scheduler.model_calls = tracker.calls
        scheduler.model_call_keys = list(tracker.call_keys)
        scheduler.model_response_ids_by_call_key = dict(tracker.response_ids_by_call_key)
        scheduler.response_ids = list(dict.fromkeys(tracker.response_ids))

    def persist_scheduler() -> None:
        sync_tracker()
        artifact_paths["scheduler_state"] = atomic_write_json(scheduler_path, scheduler)
        for record in scheduler.assignments:
            artifact_paths[f"assignment_{record.assignment.id}"] = _atomic_write_materialized_json(
                assignments_dir / f"{record.assignment.id}.json", record
            )
        unacknowledged_events: list[dict[str, object]] = []
        for event_path in sorted(events_dir.glob("*.json")):
            if int(event_path.stem) <= scheduler.coordinator_ack_event_sequence:
                continue
            raw = json.loads(read_regular_text(event_path))
            if not isinstance(raw, dict):
                raise StageValidationError(f"Invalid research event: {event_path}")
            unacknowledged_events.append(raw)
        artifact_paths["coordinator_mailbox"] = _atomic_write_materialized_json(
            mailbox_path,
            {
                "schema_version": 1,
                "through_event_sequence": scheduler.next_event_sequence - 1,
                "acknowledged_through_event_sequence": (scheduler.coordinator_ack_event_sequence),
                "unacknowledged_events": unacknowledged_events,
                "completed_reports": [
                    {
                        "assignment_id": record.assignment.id,
                        "path": record.report_path,
                        "sha256": record.report_sha256,
                        "event_sequence": record.completed_event_sequence,
                    }
                    for record in scheduler.assignments
                    if record.report_path is not None
                ],
            },
        )

    def append_event(
        kind: str,
        *,
        assignment_id: str | None = None,
        decision_id: int | None = None,
        response_id: str | None = None,
        artifact: Path | None = None,
        related_artifacts: list[Path] | None = None,
        detail: list[str] | None = None,
    ) -> int:
        sequence = scheduler.next_event_sequence
        payload: dict[str, object] = {
            "schema_version": 1,
            "sequence": sequence,
            "kind": kind,
            "assignment_id": assignment_id,
            "decision_id": decision_id,
            "response_id": response_id,
            "artifact": (
                artifact.relative_to(destination).as_posix() if artifact is not None else None
            ),
            "artifact_sha256": sha256_file(artifact) if artifact is not None else None,
            "related_artifacts": [
                {
                    "path": related.relative_to(destination).as_posix(),
                    "sha256": sha256_file(related),
                }
                for related in related_artifacts or []
            ],
            "detail": list(detail or []),
        }
        scheduler.pending_event = payload
        scheduler.next_event_sequence += 1
        persist_scheduler()
        event_path = _atomic_write_immutable_json(events_dir / f"{sequence:08d}.json", payload)
        artifact_paths[f"coordinator_event_{sequence}"] = event_path
        events_by_sequence[sequence] = payload
        scheduler.pending_event = None
        persist_scheduler()
        return sequence

    if resumed_unverified_refutation_reason is not None:
        append_event(
            "coordinator_unverified_refutation_stop_declined",
            decision_id=resumed_unverified_refutation_decision_id,
            detail=[
                resumed_unverified_refutation_reason,
                unverified_refutation_obligation,
                "Recovered a pre-hardening scheduler checkpoint and resumed research.",
            ],
        )

    def record_execution_issue(
        *,
        event_kind: str,
        exc: BaseException,
        category: FailureCategory | None = None,
        assignment_id: str | None = None,
        candidate_attempt: str | None = None,
        audit_name: str | None = None,
        repair_generation: int = 0,
        extra_obligations: list[str] | None = None,
        include_default_obligations: bool = True,
    ) -> ExecutionIssue:
        resolved_category = category or classify_failure(exc)
        issue_number = len(scheduler.execution_issues) + 1
        issue = ExecutionIssue(
            issue_id=f"issue-{issue_number:08d}",
            category=resolved_category,
            event_kind=event_kind,
            message=f"{type(exc).__name__}: {redact_text(str(exc))[:1000]}",
            assignment_id=assignment_id,
            candidate_attempt=candidate_attempt,
            audit_name=audit_name,
            repair_generation=repair_generation,
            trace_paths=[
                str(path) for path in (getattr(exc, "checkpoint_path", None),) if path is not None
            ],
            recovery_obligations=list(
                dict.fromkeys(
                    [
                        *(
                            recovery_obligations(exc, resolved_category)
                            if include_default_obligations
                            else []
                        ),
                        *(extra_obligations or []),
                    ]
                )
            ),
        )
        issue_path = _atomic_write_immutable_json(
            issues_dir / f"{issue.issue_id}.json",
            issue,
        )
        artifact_paths[f"execution_{issue.issue_id}"] = issue_path
        scheduler.execution_issues.append(issue)
        append_event(
            event_kind,
            assignment_id=assignment_id,
            artifact=issue_path,
            detail=issue.recovery_obligations,
        )
        return issue

    def assignment_records(
        *statuses: AssignmentLifecycle,
    ) -> list[ResearchAssignmentState]:
        allowed = set(statuses)
        return [record for record in scheduler.assignments if record.status in allowed]

    def canonical_candidate_report_set(report_ids: list[str]) -> list[str]:
        return sorted(set(report_ids))

    def candidate_report_set_attempted(report_ids: list[str]) -> bool:
        candidate_key = canonical_candidate_report_set(report_ids)
        return candidate_key in scheduler.attempted_candidate_report_sets

    def candidate_computation_bindings_digest(
        bindings: list[CandidateComputationBinding],
    ) -> str | None:
        if not bindings:
            return None
        return sha256_json([binding.model_dump(mode="json") for binding in bindings])

    def verify_candidate_computation_bindings(
        report_ids: list[str],
    ) -> tuple[list[CandidateComputationBinding], list[str]]:
        """Re-establish candidate computation trust from canonical persisted artifacts."""

        bindings: list[CandidateComputationBinding] = []
        obligations: list[str] = []
        for assignment_id in canonical_candidate_report_set(report_ids):
            report = reports_by_id[assignment_id]
            computation_results = sorted(
                (
                    result
                    for result in report.results
                    if result.kind is ScientificResultKind.COMPUTATION
                ),
                key=lambda result: result.local_key,
            )
            if not computation_results:
                continue

            exact_main_results = [
                result
                for result in report.results
                if result.kind in {ScientificResultKind.LEMMA, ScientificResultKind.REDUCTION}
                and result.scope is ScientificScope.MAIN
                and result.disposition is ScientificResultDisposition.PROPOSED_COMPLETE
                and normalize_exact_statement(result.exact_statement)
                == normalize_exact_statement(compiled.normalized_statement)
            ]
            main_dependency_keys = {
                result.local_key: set(
                    transitive_result_dependency_keys(report.results, [result.local_key])
                )
                for result in exact_main_results
            }
            if not exact_main_results:
                obligations.append(
                    f"Candidate-trigger report {assignment_id!r} uses computation but has no "
                    "separate proposed-complete lemma or reduction proving the frozen exact "
                    "main theorem; audit the domain-completeness reduction."
                )

            record = scheduler.assignment_record(assignment_id)
            if (
                record is None
                or record.computation_evidence_path is None
                or record.computation_evidence_sha256 is None
            ):
                obligations.append(
                    f"Candidate-trigger computation evidence is absent for assignment "
                    f"{assignment_id!r}."
                )
                continue
            evidence_path = resolved_artifact(record.computation_evidence_path)
            if (
                not evidence_path.is_file()
                or sha256_file(evidence_path) != record.computation_evidence_sha256
            ):
                obligations.append(
                    f"Candidate-trigger computation evidence changed or is missing for "
                    f"assignment {assignment_id!r}."
                )
                continue
            try:
                evidence = verify_persisted_computation_evidence(
                    destination.parent,
                    assignment_id,
                    evidence_path,
                )
            except (OSError, ValueError) as exc:
                obligations.append(
                    f"Canonical computation evidence verification failed for assignment "
                    f"{assignment_id!r}: {redact_text(str(exc))[:500]}"
                )
                continue
            collection = evidence.collection
            replay = evidence.replay
            if collection is None or not collection.trusted or collection.manifest is None:
                status = collection.status.value if collection is not None else "absent"
                obligations.append(
                    f"Candidate-trigger computation collection is not trusted for assignment "
                    f"{assignment_id!r} (status: {status})."
                )
                continue
            if replay is None or replay.status is not ComputationReplayStatus.PASSED:
                status = replay.status.value if replay is not None else "absent"
                obligations.append(
                    f"Candidate-trigger computation replay did not pass for assignment "
                    f"{assignment_id!r} (status: {status})."
                )
                continue

            manifest = collection.manifest
            manifest_path = destination.parent / Path(collection.manifest_path or "")
            replay_path = (
                destination.parent
                / "research"
                / "computations"
                / "replays"
                / assignment_id
                / manifest.manifest_sha256
                / "verdict.json"
            )
            if not manifest_path.is_file() or not replay_path.is_file():
                obligations.append(
                    f"Candidate-trigger computation has no canonical manifest/verdict files "
                    f"for assignment {assignment_id!r}."
                )
                continue
            for result in computation_results:
                supporting_main_result_keys = sorted(
                    main_result_key
                    for main_result_key, dependency_keys in main_dependency_keys.items()
                    if result.local_key in dependency_keys
                )
                if not supporting_main_result_keys:
                    obligations.append(
                        f"Computation result {assignment_id!r}/{result.local_key!r} is replayed "
                        "but unrelated to every exact-main result's declared transitive "
                        "dependency_result_keys closure."
                    )
                    continue
                if (
                    result.scope is not ScientificScope.COMPUTATION
                    or result.disposition is not ScientificResultDisposition.PROPOSED_COMPLETE
                ):
                    obligations.append(
                        f"Computation result {assignment_id!r}/{result.local_key!r} is not "
                        "mathematically admissible for candidate packaging: it must be a "
                        "proposed-complete result with computation scope."
                    )
                    continue
                supporting_declarations = sorted(
                    declaration.declaration_sha256
                    for declaration in manifest.declarations
                    if result.local_key in declaration.supporting_result_keys
                )
                if not supporting_declarations:
                    obligations.append(
                        f"Computation result {assignment_id!r}/{result.local_key!r} is not "
                        "named by any declaration in its canonical replay manifest."
                    )
                    continue
                bindings.append(
                    CandidateComputationBinding(
                        assignment_id=assignment_id,
                        result_local_key=result.local_key,
                        result_payload_sha256=sha256_json(result),
                        evidence_path=evidence_path.relative_to(destination).as_posix(),
                        evidence_sha256=sha256_file(evidence_path),
                        manifest_path=manifest_path.relative_to(destination).as_posix(),
                        manifest_sha256=manifest.manifest_sha256,
                        manifest_file_sha256=sha256_file(manifest_path),
                        replay_path=replay_path.relative_to(destination).as_posix(),
                        replay_sha256=sha256_file(replay_path),
                        replay_record_sha256=replay.record_sha256,
                        declaration_sha256s=supporting_declarations,
                        main_result_keys=supporting_main_result_keys,
                    )
                )
        bindings.sort(key=lambda item: (item.assignment_id, item.result_local_key))
        return bindings, list(dict.fromkeys(obligations))

    def candidate_graph_support_bindings_digest(
        bindings: list[CandidateGraphSupportBinding],
    ) -> str | None:
        if not bindings:
            return None
        return sha256_json([binding.model_dump(mode="json") for binding in bindings])

    def verify_candidate_graph_support_bindings(
        report_ids: list[str],
    ) -> tuple[list[CandidateGraphSupportBinding], list[str]]:
        """Bind graph-integrated candidates to their live canonical ledger derivations."""

        if knowledge_graph is None or graph_problem_id is None or run_id is None:
            missing_graph_obligations: list[str] = []
            for assignment_id in canonical_candidate_report_set(report_ids):
                report = reports_by_id[assignment_id]
                for result in report.results:
                    if (
                        result.kind
                        in {
                            ScientificResultKind.LEMMA,
                            ScientificResultKind.REDUCTION,
                        }
                        and result.scope is ScientificScope.MAIN
                        and result.disposition is ScientificResultDisposition.PROPOSED_COMPLETE
                        and normalize_exact_statement(result.exact_statement)
                        == normalize_exact_statement(compiled.normalized_statement)
                        and result.assumptions
                    ):
                        missing_graph_obligations.append(
                            f"Candidate support result {assignment_id!r}/{result.local_key!r} "
                            "contains unbound assumptions and cannot prove the bare exact-main "
                            "claim."
                        )
                        continue
                    if not (
                        result.kind
                        in {
                            ScientificResultKind.LEMMA,
                            ScientificResultKind.REDUCTION,
                        }
                        and result.scope is ScientificScope.MAIN
                        and result.disposition is ScientificResultDisposition.PROPOSED_COMPLETE
                        and normalize_exact_statement(result.exact_statement)
                        == normalize_exact_statement(compiled.normalized_statement)
                        and (result.dependency_node_ids or result.dependency_result_keys)
                    ):
                        continue
                    references = [
                        *(f"node:{item}" for item in result.dependency_node_ids),
                        *(f"result:{item}" for item in result.dependency_result_keys),
                    ]
                    missing_graph_obligations.append(
                        f"Candidate support result {assignment_id!r}/{result.local_key!r} names "
                        "proof support but no active canonical knowledge graph is available: "
                        + ", ".join(references)
                    )
            return [], missing_graph_obligations
        graph_nodes = knowledge_graph.load_nodes()
        graph_by_id = {node.matek_id: node for node in graph_nodes}
        main_claim_id = knowledge_graph.main_claim_id(graph_problem_id)
        graph_revision = knowledge_graph.load_state().revision
        markdown_ledger = project_markdown_ledger(
            graph_nodes,
            graph_revision=graph_revision,
            problem_id=graph_problem_id,
            target_claim_id=main_claim_id,
        )
        trusted_external_claim_ids = trusted_claim_ids(markdown_ledger)
        verified_computations, _ = verify_candidate_computation_bindings(report_ids)
        computation_by_result = {
            (binding.assignment_id, binding.result_local_key): binding
            for binding in verified_computations
        }
        bindings: list[CandidateGraphSupportBinding] = []
        obligations: list[str] = []
        for assignment_id in canonical_candidate_report_set(report_ids):
            report = reports_by_id[assignment_id]
            exact_main_results = sorted(
                (
                    result
                    for result in report.results
                    if result.kind in {ScientificResultKind.LEMMA, ScientificResultKind.REDUCTION}
                    and result.scope is ScientificScope.MAIN
                    and result.disposition is ScientificResultDisposition.PROPOSED_COMPLETE
                    and normalize_exact_statement(result.exact_statement)
                    == normalize_exact_statement(compiled.normalized_statement)
                ),
                key=lambda result: result.local_key,
            )
            if not exact_main_results:
                obligations.append(
                    f"Graph-integrated candidate report {assignment_id!r} has no exact-main "
                    "proposed lemma or reduction for canonical admission."
                )
                continue
            record = scheduler.assignment_record(assignment_id)
            if (
                record is None
                or record.graph_patch_path is None
                or record.graph_patch_sha256 is None
                or record.report_sha256 is None
            ):
                obligations.append(
                    f"Canonical graph admission record is absent for candidate report "
                    f"{assignment_id!r}."
                )
                continue
            admission_path = resolved_artifact(record.graph_patch_path)
            if (
                not admission_path.is_file()
                or sha256_file(admission_path) != record.graph_patch_sha256
            ):
                obligations.append(
                    f"Canonical graph admission record changed for candidate report "
                    f"{assignment_id!r}."
                )
                continue
            try:
                admission_record = json.loads(read_regular_text(admission_path))
                if not isinstance(admission_record, dict):
                    raise ValueError("admission record is not an object")
                if (
                    admission_record.get("assignment_id") != assignment_id
                    or admission_record.get("admission_mode") != "typed_scientific_report_v2"
                    or admission_record.get("model_authored_patch") is not False
                ):
                    raise ValueError("admission record identity or mode is invalid")
                merge_result = GraphMergeResult.model_validate(admission_record.get("merge_result"))
            except (ValidationError, ValueError) as exc:
                obligations.append(
                    f"Canonical graph admission record is invalid for candidate report "
                    f"{assignment_id!r}: {redact_text(str(exc))[:500]}"
                )
                continue
            if not merge_result.committed:
                obligations.append(
                    f"Canonical graph admission did not commit candidate report "
                    f"{assignment_id!r} (status: {merge_result.status})."
                )
                continue

            main_result_keys = sorted(result.local_key for result in exact_main_results)
            closure_result_keys = sorted(
                {
                    *main_result_keys,
                    *transitive_result_dependency_keys(report.results, main_result_keys),
                }
            )
            computation_result_keys = sorted(
                result.local_key
                for result in report.results
                if result.local_key in closure_result_keys
                and result.kind is ScientificResultKind.COMPUTATION
            )
            result_by_key = {result.local_key: result for result in report.results}
            closure_results = [result_by_key[local_key] for local_key in closure_result_keys]
            support_ids: set[str] = set()
            result_binding_failed = False
            merged_ids = set(merge_result.created_node_ids) | set(merge_result.updated_node_ids)
            attempts_by_key: dict[str, GraphNode] = {}
            derivations_by_key: dict[str, GraphNode] = {}
            definitions_by_key: dict[str, GraphNode] = {}
            conclusion_ids_by_key: dict[str, str] = {}

            for result in closure_results:
                if result.assumptions:
                    obligations.append(
                        f"Candidate support result {assignment_id!r}/{result.local_key!r} "
                        "contains unbound assumptions and cannot serve as a canonical proof "
                        "premise."
                    )
                    result_binding_failed = True
                    continue
                if result.kind is ScientificResultKind.COUNTEREXAMPLE or (
                    result.kind is not ScientificResultKind.DEFINITION
                    and result.disposition is not ScientificResultDisposition.PROPOSED_COMPLETE
                ):
                    obligations.append(
                        f"Candidate support result {assignment_id!r}/{result.local_key!r} is not "
                        "a gap-free proposed-complete premise."
                    )
                    result_binding_failed = True
                    continue
                if result.kind is ScientificResultKind.DEFINITION:
                    live_definitions = [
                        node
                        for node in graph_nodes
                        if node.node_type is NodeType.DEFINITION
                        and node.problem_id == graph_problem_id
                        and node.matek_id in merged_ids
                        and node_has_scientific_admission_binding(
                            node,
                            run_id=run_id,
                            assignment_id=assignment_id,
                            result=result,
                        )
                        and not node.tombstone
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
                    ]
                    if len(live_definitions) != 1:
                        obligations.append(
                            f"Candidate support result {assignment_id!r}/{result.local_key!r} "
                            "lacks one live canonical definition."
                        )
                        result_binding_failed = True
                        continue
                    definition = live_definitions[0]
                    definitions_by_key[result.local_key] = definition
                    conclusion_ids_by_key[result.local_key] = definition.matek_id
                    support_ids.add(definition.matek_id)
                    continue

                matching = [
                    node
                    for node in graph_nodes
                    if node.problem_id == graph_problem_id
                    and node.created_in_run == run_id
                    and node.metadata.get("matek_assignment_id") == assignment_id
                    and node.metadata.get("matek_result_local_key") == result.local_key
                    and node.metadata.get("matek_admission_payload_sha256")
                    == admission_payload_sha256(result)
                ]
                live_attempts = [
                    node
                    for node in matching
                    if node.node_type is NodeType.PROOF_ATTEMPT
                    and node.matek_id in merged_ids
                    and not node.tombstone
                    and not node.invalidation_reasons
                    and node.workflow_status is WorkflowStatus.COMPLETE
                    and node.epistemic_status
                    not in {
                        EpistemicStatus.REFUTED,
                        EpistemicStatus.INCONSISTENT,
                        EpistemicStatus.STALE,
                    }
                    and result.proof_or_certificate in node.evidence
                ]
                live_derivations = [
                    node
                    for node in matching
                    if node.node_type is NodeType.DERIVATION
                    and node.matek_id in merged_ids
                    and not node.tombstone
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
                ]
                if len(live_attempts) != 1 or len(live_derivations) != 1:
                    obligations.append(
                        f"Candidate support result {assignment_id!r}/{result.local_key!r} lacks "
                        "one live canonical derivation and proof attempt."
                    )
                    result_binding_failed = True
                    continue
                attempt = live_attempts[0]
                derivation = live_derivations[0]
                conclusion_ids = [
                    edge.target_id
                    for edge in derivation.relations
                    if edge.relation is RelationType.PROVES
                ]
                if len(conclusion_ids) != 1:
                    obligations.append(
                        f"Candidate support result {assignment_id!r}/{result.local_key!r} has an "
                        "ambiguous canonical conclusion."
                    )
                    result_binding_failed = True
                    continue
                conclusion_id = conclusion_ids[0]
                conclusion = graph_by_id.get(conclusion_id)
                if (
                    conclusion is None
                    or conclusion.node_type is not NodeType.CLAIM
                    or conclusion.tombstone
                    or normalize_exact_statement(graph_exact_statement(conclusion.body))
                    != normalize_exact_statement(result.exact_statement)
                    or (result.local_key in main_result_keys and conclusion_id != main_claim_id)
                    or derivation.metadata.get("matek_conclusion_claim_id") != conclusion_id
                    or derivation.metadata.get("matek_proof_attempt_id") != attempt.matek_id
                    or derivation.metadata.get("matek_exact_target_version")
                    != logical_version(result.exact_statement)
                    or not any(
                        edge.relation is RelationType.RELATED_TO
                        and edge.target_id == attempt.matek_id
                        for edge in derivation.relations
                    )
                    or not any(
                        edge.relation is RelationType.RELATED_TO and edge.target_id == conclusion_id
                        for edge in attempt.relations
                    )
                ):
                    obligations.append(
                        f"Candidate support result {assignment_id!r}/{result.local_key!r} is not "
                        "bound to its exact conclusion and proof attempt."
                    )
                    result_binding_failed = True
                    continue
                attempts_by_key[result.local_key] = attempt
                derivations_by_key[result.local_key] = derivation
                conclusion_ids_by_key[result.local_key] = conclusion_id
                support_ids.update([attempt.matek_id, derivation.matek_id])
                if conclusion_id != main_claim_id:
                    support_ids.add(conclusion_id)

            if result_binding_failed:
                continue

            for result in closure_results:
                dependency_ids = list(
                    dict.fromkeys(
                        [
                            *result.dependency_node_ids,
                            *(
                                conclusion_ids_by_key[dependency_key]
                                for dependency_key in result.dependency_result_keys
                            ),
                        ]
                    )
                )
                dependency_nodes = [graph_by_id.get(node_id) for node_id in dependency_ids]
                if any(
                    node is None
                    or node.tombstone
                    or node.invalidation_reasons
                    or node.epistemic_status
                    in {
                        EpistemicStatus.REFUTED,
                        EpistemicStatus.INCONSISTENT,
                        EpistemicStatus.STALE,
                    }
                    or node.workflow_status
                    in {
                        WorkflowStatus.BLOCKED,
                        WorkflowStatus.ABANDONED,
                        WorkflowStatus.SUPERSEDED,
                    }
                    for node in dependency_nodes
                ):
                    obligations.append(
                        f"Candidate support result {assignment_id!r}/{result.local_key!r} has a "
                        "missing, blocked, invalidated, or tombstoned dependency."
                    )
                    result_binding_failed = True
                    continue
                external_dependency_nodes = [
                    (node_id, graph_by_id[node_id]) for node_id in result.dependency_node_ids
                ]
                external_computation_ids = [
                    node_id
                    for node_id, node in external_dependency_nodes
                    if node.node_type is NodeType.CLAIM
                    and (
                        "matek/computation" in node.tags
                        or node.metadata.get("matek_scientific_kind")
                        == ScientificResultKind.COMPUTATION.value
                        or node.metadata.get("matek_scientific_scope")
                        == ScientificScope.COMPUTATION.value
                    )
                ]
                if external_computation_ids:
                    obligations.append(
                        f"Candidate support result {assignment_id!r}/{result.local_key!r} uses "
                        "external computation premise(s) without a fresh report-local persisted "
                        "replay/CAS binding: " + ", ".join(sorted(external_computation_ids))
                    )
                    result_binding_failed = True
                    continue
                untrusted_external_ids = [
                    node_id
                    for node_id, node in external_dependency_nodes
                    if not (
                        node.node_type in {NodeType.CLAIM, NodeType.DEFINITION}
                        and node_id in trusted_external_claim_ids
                    )
                ]
                if untrusted_external_ids:
                    obligations.append(
                        f"Candidate support result {assignment_id!r}/{result.local_key!r} uses "
                        "external premise(s) that are not current canonical trusted claims or "
                        "application-admitted definitions: "
                        + ", ".join(sorted(untrusted_external_ids))
                    )
                    result_binding_failed = True
                    continue
                expected_versions = [
                    f"{node_id}@{logical_version(graph_exact_statement(graph_by_id[node_id].body))}"
                    for node_id in dependency_ids
                ]
                support_ids.update(
                    node_id for node_id in dependency_ids if node_id != main_claim_id
                )
                if result.kind is ScientificResultKind.DEFINITION:
                    dependency_owner = definitions_by_key[result.local_key]
                    actual_dependency_ids = [
                        edge.target_id
                        for edge in dependency_owner.relations
                        if edge.relation is RelationType.DEPENDS_ON
                    ]
                else:
                    attempt = attempts_by_key[result.local_key]
                    dependency_owner = derivations_by_key[result.local_key]
                    actual_dependency_ids = [
                        edge.target_id
                        for edge in dependency_owner.relations
                        if edge.relation is RelationType.DEPENDS_ON
                    ]
                    attempt_dependency_ids = [
                        edge.target_id
                        for edge in attempt.relations
                        if edge.relation is RelationType.DEPENDS_ON
                    ]
                    if (
                        attempt_dependency_ids != dependency_ids
                        or dependency_owner.metadata.get("matek_premise_claim_ids")
                        != dependency_ids
                        or dependency_owner.metadata.get("matek_premise_versions")
                        != [version.replace("@", "=", 1) for version in expected_versions]
                    ):
                        obligations.append(
                            f"Candidate support result {assignment_id!r}/{result.local_key!r} "
                            "does not preserve its application-resolved premise mapping."
                        )
                        result_binding_failed = True
                        continue
                if (
                    actual_dependency_ids != dependency_ids
                    or dependency_owner.dependency_versions != expected_versions
                ):
                    obligations.append(
                        f"Candidate support result {assignment_id!r}/{result.local_key!r} has "
                        "changed dependency edges or versions."
                    )
                    result_binding_failed = True
                    continue

                if result.kind is not ScientificResultKind.COMPUTATION:
                    continue
                computation_binding = computation_by_result.get((assignment_id, result.local_key))
                expected_main_keys = sorted(
                    main_result_key
                    for main_result_key in main_result_keys
                    if result.local_key
                    in transitive_result_dependency_keys(report.results, [main_result_key])
                )
                derivation = derivations_by_key[result.local_key]
                raw_artifact_ids = derivation.metadata.get("matek_artifact_ids")
                artifact_ids = (
                    raw_artifact_ids
                    if isinstance(raw_artifact_ids, list)
                    and all(isinstance(item, str) for item in raw_artifact_ids)
                    else []
                )
                artifact_nodes = [
                    graph_by_id[item]
                    for item in artifact_ids
                    if item in graph_by_id and graph_by_id[item].node_type is NodeType.ARTIFACT
                ]
                manifest_nodes = [
                    node for node in artifact_nodes if node.author_role == "computation-collector"
                ]
                replay_nodes = [
                    node for node in artifact_nodes if node.author_role == "computation-replayer"
                ]
                if (
                    computation_binding is None
                    or computation_binding.main_result_keys != expected_main_keys
                    or len(artifact_ids) != 2
                    or len(artifact_nodes) != 2
                    or len(manifest_nodes) != 1
                    or len(replay_nodes) != 1
                ):
                    obligations.append(
                        f"Candidate computation support {assignment_id!r}/{result.local_key!r} "
                        "is not bound to one canonical manifest/replay pair."
                    )
                    result_binding_failed = True
                    continue
                manifest_node = manifest_nodes[0]
                replay_node = replay_nodes[0]
                expected_artifact_ids = {manifest_node.matek_id, replay_node.matek_id}
                supporting_manifest_keys = manifest_node.metadata.get(
                    "matek_supporting_result_keys"
                )
                supporting_replay_keys = replay_node.metadata.get("matek_supporting_result_keys")
                if (
                    set(artifact_ids) != expected_artifact_ids
                    or not expected_artifact_ids.issubset(merged_ids)
                    or manifest_node.tombstone
                    or replay_node.tombstone
                    or manifest_node.epistemic_status is not EpistemicStatus.AUDIT_PASSED
                    or replay_node.epistemic_status is not EpistemicStatus.AUDIT_PASSED
                    or manifest_node.workflow_status is not WorkflowStatus.COMPLETE
                    or replay_node.workflow_status is not WorkflowStatus.COMPLETE
                    or manifest_node.metadata.get("matek_assignment_id") != assignment_id
                    or replay_node.metadata.get("matek_assignment_id") != assignment_id
                    or manifest_node.metadata.get("matek_computation_manifest_sha256")
                    != computation_binding.manifest_sha256
                    or replay_node.metadata.get("matek_computation_manifest_sha256")
                    != computation_binding.manifest_sha256
                    or replay_node.metadata.get("matek_computation_replay_record_sha256")
                    != computation_binding.replay_record_sha256
                    or manifest_node.metadata.get("matek_replay_passed") is not True
                    or replay_node.metadata.get("matek_replay_passed") is not True
                    or not isinstance(supporting_manifest_keys, list)
                    or result.local_key not in supporting_manifest_keys
                    or not isinstance(supporting_replay_keys, list)
                    or result.local_key not in supporting_replay_keys
                    or not any(
                        edge.relation is RelationType.RELATED_TO
                        and edge.target_id == manifest_node.matek_id
                        for edge in replay_node.relations
                    )
                    or {
                        edge.target_id
                        for edge in derivation.relations
                        if edge.relation is RelationType.RELATED_TO
                        and edge.target_id in expected_artifact_ids
                    }
                    != expected_artifact_ids
                ):
                    obligations.append(
                        f"Candidate computation support {assignment_id!r}/{result.local_key!r} "
                        "has changed or non-passing canonical graph artifacts."
                    )
                    result_binding_failed = True
                    continue
                support_ids.update(expected_artifact_ids)

            if result_binding_failed:
                continue
            # The frozen exact-main claim is part of the mathematical support contract even
            # when it is the conclusion rather than a premise.  Persist and hash it alongside
            # every attempt, derivation, premise, artifact, and linked obligation.
            support_ids.add(main_claim_id)
            obligation_support_ids = set(support_ids)
            linked_obligations: list[GraphNode] = []
            unresolved_linked_obligations: list[GraphNode] = []
            for node in graph_nodes:
                if node.node_type is not NodeType.OBLIGATION:
                    continue
                ledger_obligation = markdown_ledger.obligations.get(node.matek_id)
                metadata_links: set[str] = set()
                for key in (
                    "matek_parent_node_ids",
                    "matek_parent_derivation_ids",
                    "matek_parent_proof_attempt_ids",
                    "matek_dependency_claim_ids",
                    "matek_target_claim_ids",
                ):
                    raw_links = node.metadata.get(key)
                    if isinstance(raw_links, list):
                        metadata_links.update(item for item in raw_links if isinstance(item, str))
                relation_links = {
                    edge.target_id
                    for edge in node.relations
                    if edge.relation
                    in {
                        RelationType.BLOCKS,
                        RelationType.DEPENDS_ON,
                        RelationType.TARGETS,
                    }
                }
                reciprocal_links = {
                    support_id
                    for support_id in obligation_support_ids
                    if support_id in graph_by_id
                    and any(
                        edge.relation is RelationType.BLOCKED_BY and edge.target_id == node.matek_id
                        for edge in graph_by_id[support_id].relations
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
                support_metadata_links = False
                for support_id in obligation_support_ids:
                    support_node = graph_by_id.get(support_id)
                    if support_node is None:
                        continue
                    raw_obligation_ids = support_node.metadata.get("matek_obligation_ids")
                    if isinstance(raw_obligation_ids, list) and (
                        node.matek_id in raw_obligation_ids
                    ):
                        support_metadata_links = True
                        break
                all_obligation_links = (
                    metadata_links | relation_links | reciprocal_links | ledger_links
                )
                if not (
                    all_obligation_links.intersection(obligation_support_ids)
                    or support_metadata_links
                ):
                    continue
                linked_obligations.append(node)
                if (
                    ledger_obligation is None
                    or ledger_obligation.status is not ObligationStatus.RESOLVED
                ):
                    unresolved_linked_obligations.append(node)
            if unresolved_linked_obligations:
                obligations.append(
                    f"Canonical candidate support for assignment {assignment_id!r} retains "
                    "unresolved obligation(s): "
                    + ", ".join(sorted(node.matek_id for node in unresolved_linked_obligations))
                )
            support_ids.update(node.matek_id for node in linked_obligations)
            support_nodes = [
                graph_by_id[node_id].model_dump(mode="json") for node_id in sorted(support_ids)
            ]
            bindings.append(
                CandidateGraphSupportBinding(
                    assignment_id=assignment_id,
                    report_sha256=record.report_sha256,
                    admission_record_path=record.graph_patch_path,
                    admission_record_sha256=record.graph_patch_sha256,
                    admission_revision=merge_result.new_revision,
                    main_claim_id=main_claim_id,
                    main_result_keys=main_result_keys,
                    closure_result_keys=closure_result_keys,
                    computation_result_keys=computation_result_keys,
                    support_nodes=support_nodes,
                    support_sha256=sha256_json(support_nodes),
                )
            )
        bindings.sort(key=lambda item: item.assignment_id)
        return bindings, list(dict.fromkeys(obligations))

    def worker_input_for(record: ResearchAssignmentState) -> str:
        return assignment_input(record)

    def reserve_worker_request(record: ResearchAssignmentState) -> None:
        computation_store.prepare_workspace(record.assignment.id)
        worker_input = worker_input_for(record)
        if record.request_settings is None:
            record.request_settings = worker_model.model_copy(deep=True)
        request_key = tracker.request_key(
            instructions=worker_prompt,
            input_text=worker_input,
            settings=record.request_settings,
            output_type=ResearchWorkerReport,
        )
        tracker.register_request(
            instructions=worker_prompt,
            input_text=worker_input,
            settings=record.request_settings,
            output_type=ResearchWorkerReport,
        )
        record.request_key = request_key

    def release_unlaunched_worker_request(record: ResearchAssignmentState) -> None:
        if record.launched or record.request_key is None:
            return
        tracker.release_call_key(record.request_key)
        record.request_key = None

    def build_continuity() -> ResearchContinuityState:
        routes: list[ResearchContinuityRoute] = []
        for record in scheduler.assignments:
            report = reports_by_id.get(record.assignment.id)
            if report is None:
                continue
            routes.append(
                ResearchContinuityRoute(
                    decision_id=record.admitted_by_decision,
                    event_sequence=record.completed_event_sequence or 0,
                    assignment_id=report.assignment_id,
                    approach_family=record.assignment.approach_family,
                    target_node_ids=record.assignment.target_node_ids,
                    status=report.status,
                    mechanism=report.mechanism or record.assignment.task,
                    formal_results=report.formal_results,
                    proof_content=report.proof_content,
                    exact_gap=report.exact_gap,
                    assumptions=report.assumptions,
                    counterexamples=report.counterexamples,
                    dependencies=report.dependencies,
                    reopen_condition=(
                        "Reopen only with new evidence that resolves the recorded exact gap or "
                        "defeats the recorded counterexample."
                        if report.status in {WorkerStatus.BLOCKED, WorkerStatus.REFUTED}
                        else "Continue through a coordinator task targeting the remaining gap."
                    ),
                )
            )
        decisions = [item.decision for item in scheduler.decisions]
        return ResearchContinuityState(
            after_event_sequence=scheduler.next_event_sequence - 1,
            promising_routes=[
                route
                for route in routes
                if route.status in {WorkerStatus.PROGRESS, WorkerStatus.CANDIDATE_COMPLETE}
            ],
            partial_results=[route for route in routes if route.formal_results],
            ruled_out_directions=[
                route for route in routes if route.status == WorkerStatus.REFUTED
            ],
            blocked_routes=[route for route in routes if route.status == WorkerStatus.BLOCKED],
            open_gaps=list(
                dict.fromkeys(
                    [
                        *(route.exact_gap for route in routes if route.exact_gap),
                        *scheduler.repair_obligations,
                    ]
                )
            ),
            counterexamples=list(
                dict.fromkeys(item for route in routes for item in route.counterexamples)
            ),
            dependencies=list(
                dict.fromkeys(item for route in routes for item in route.dependencies)
            ),
            audit_repair_obligations=list(dict.fromkeys(scheduler.repair_obligations)),
            claims_requiring_counterexample_search=list(
                dict.fromkeys(
                    claim
                    for decision in decisions
                    for claim in decision.claims_requiring_counterexample_search
                )
            ),
            lemmas_requiring_proof_completion=list(
                dict.fromkeys(
                    lemma
                    for decision in decisions
                    for lemma in decision.lemmas_requiring_proof_completion
                )
            ),
            retired_assignment_ids=[
                record.assignment.id
                for record in scheduler.assignments
                if record.status == AssignmentLifecycle.RETIRED
            ],
            redirected_assignment_ids=list(
                dict.fromkeys(
                    assignment_id
                    for decision in decisions
                    for assignment_id in decision.redirect_assignment_ids
                )
            ),
            completed_assignment_ids=[route.assignment_id for route in routes],
        )

    def persist_research_index() -> ResearchContinuityState:
        nonlocal latest_continuity
        artifact_paths["registry"] = atomic_write_json(registry_path, registry)
        latest_continuity = build_continuity()
        artifact_paths["continuity"] = atomic_write_json(continuity_path, latest_continuity)
        persist_scheduler()
        return latest_continuity

    def register_existing_artifacts() -> None:
        for path in destination.rglob("*"):
            if path.name == "result.json" or path.is_dir():
                continue
            if path.is_symlink():
                raise StageValidationError(f"Research artifact must not be a symlink: {path}")
            relative = path.relative_to(destination).as_posix()
            if path in artifact_paths.values():
                continue
            key = f"research::{relative}"
            if key in artifact_paths and artifact_paths[key] != path:
                raise StageValidationError(
                    f"Research artifact manifest key collision for {relative}"
                )
            artifact_paths[key] = path

    active: dict[asyncio.Task[tuple[ResearchWorkerReport, str]], ResearchAssignmentState] = {}

    def validate_acceptance_gate(
        gate: ResearchAcceptanceGate,
        *,
        attempt: CandidateAttemptState,
        require_pass_event: bool,
    ) -> None:
        """Fail closed on every scientific and artifact invariant of acceptance."""

        if not gate.accepted or current_candidate is None or current_verdict is None:
            raise StageValidationError(
                "Accepted research has no passing gate, candidate, or verdict."
            )
        expected_contract_digest = sha256_text(
            json.dumps(
                compiled.claim_contract.as_dict(),
                sort_keys=True,
                ensure_ascii=False,
            )
        )
        if gate.candidate_sha256 != sha256_json(current_candidate):
            raise StageValidationError("Accepted candidate does not match its gate digest.")
        if gate.claim_contract_sha256 != expected_contract_digest:
            raise StageValidationError("Accepted research gate targets a different claim contract.")
        if normalize_exact_statement(current_candidate.exact_theorem) != normalize_exact_statement(
            compiled.normalized_statement
        ):
            raise StageValidationError(
                "Accepted candidate theorem differs from the frozen exact target."
            )
        archived_replay = archived_candidate_attempt_for_replay(
            attempt_name=attempt.attempt_name,
            report_ids=attempt.report_ids,
            source=attempt.source,
        )
        if archived_replay is None:
            verified_computation_bindings, computation_obligations = (
                verify_candidate_computation_bindings(attempt.report_ids)
            )
            verified_graph_support, graph_support_obligations = (
                verify_candidate_graph_support_bindings(attempt.report_ids)
            )
        else:
            archived_attempt, _ = archived_replay
            verified_computation_bindings = archived_attempt.computation_bindings
            computation_obligations = archived_attempt.computation_obligations
            verified_graph_support = archived_attempt.graph_support_bindings
            graph_support_obligations = archived_attempt.graph_support_obligations
        if computation_obligations:
            raise StageValidationError(
                "Accepted candidate retains invalid computation evidence: "
                + "; ".join(computation_obligations)
            )
        if attempt.computation_gate_version != 1 and verified_computation_bindings:
            raise StageValidationError(
                "Accepted computation-dependent candidate predates the deterministic "
                "computation gate."
            )
        if (
            attempt.computation_bindings != verified_computation_bindings
            or attempt.computation_obligations
            or gate.computation_bindings_sha256
            != candidate_computation_bindings_digest(verified_computation_bindings)
        ):
            raise StageValidationError(
                "Accepted candidate is not bound to its canonical computation evidence."
            )
        if graph_support_obligations:
            raise StageValidationError(
                "Accepted candidate retains invalid canonical graph support: "
                + "; ".join(graph_support_obligations)
            )
        if attempt.graph_support_gate_version != 1 and verified_graph_support:
            raise StageValidationError(
                "Accepted graph-integrated candidate predates the canonical-support gate."
            )
        if (
            attempt.graph_support_bindings != verified_graph_support
            or attempt.graph_support_obligations
            or gate.graph_support_bindings_sha256
            != candidate_graph_support_bindings_digest(verified_graph_support)
        ):
            raise StageValidationError(
                "Accepted candidate is not bound to its canonical graph support slice."
            )
        if current_candidate.unresolved_items or any(
            not theorem.verified for theorem in current_candidate.imported_theorems
        ):
            raise StageValidationError(
                "Accepted candidate retains unresolved or unverified content."
            )
        if current_verdict.verdict != FinalJudgeDecision.ACCEPTED:
            raise StageValidationError("Accepted research has no accepting final-judge verdict.")
        expected_audits = list(audit_names)
        run_complexity = (
            settings.run_complexity_audit
            if settings.run_complexity_audit is not None
            else current_candidate.quantitative_or_algorithmic
        )
        if run_complexity:
            expected_audits.append("complexity")
        if gate.mandatory_audits != expected_audits or set(current_audits) != set(expected_audits):
            raise StageValidationError("Accepted research gate has an invalid mandatory-audit set.")
        for name in expected_audits:
            audit = current_audits[name]
            if (
                audit.verdict != AuditDecision.PASS
                or not audit.target_matches
                or audit.unresolved_obligations
                or any(issue.severity.casefold() == "blocking" for issue in audit.issues)
            ):
                raise StageValidationError(
                    f"Accepted research retained a failing mandatory {name} audit."
                )
        if (
            not gate.final_judge_response_id.strip()
            or gate.final_judge_response_id not in tracker.response_ids
        ):
            raise StageValidationError("Accepted research has no accounted final-judge response.")

        attempt_name = attempt.attempt_name
        if (
            attempt.package_evidence_sha256 is None
            or attempt.package_sha256 is None
            or attempt.source_verification_sha256 is None
            or not (attempt.packager_response_id or "").strip()
            or attempt.verdict_sha256 is None
            or attempt.final_judge_response_id != gate.final_judge_response_id
            or attempt.judge_call_reservation_key is not None
            or set(attempt.audit_sha256) != set(expected_audits)
            or set(attempt.audit_response_ids) != set(expected_audits)
            or not attempt.outcome_ready
            or attempt.outcome_gate != gate.model_dump(mode="json")
            or attempt.outcome_obligations
            or attempt.outcome_decision != FinalJudgeDecision.ACCEPTED
            or attempt.outcome_failure_kind is not None
        ):
            raise StageValidationError("Accepted candidate attempt metadata is incomplete.")
        audit_response_ids = list(attempt.audit_response_ids.values())
        if (
            any(not response_id.strip() for response_id in audit_response_ids)
            or len(set(audit_response_ids)) != len(audit_response_ids)
            or gate.final_judge_response_id in audit_response_ids
            or any(response_id not in tracker.response_ids for response_id in audit_response_ids)
        ):
            raise StageValidationError("Accepted candidate has an unaccounted audit response.")

        audit_inputs = {
            name: json.dumps(
                {
                    "audit_role": name,
                    "claim_contract": compiled.claim_contract.as_dict(),
                    **candidate_gate_policy_payload(attempt),
                    "candidate_package": current_candidate.model_dump(mode="json"),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            for name in expected_audits
        }
        for name in expected_audits:
            audit_call_key = tracker.request_key(
                instructions=audit_instructions[name],
                input_text=audit_inputs[name],
                settings=attempt.audit_settings,
                output_type=AuditVerdict,
            )
            if (
                tracker.response_ids_by_call_key.get(audit_call_key)
                != attempt.audit_response_ids[name]
            ):
                raise StageValidationError(
                    f"Accepted candidate's {name} audit is not bound to its request."
                )
        judge_input = json.dumps(
            {
                "claim_contract": compiled.claim_contract.as_dict(),
                **candidate_gate_policy_payload(attempt),
                "candidate_package": current_candidate.model_dump(mode="json"),
                "independent_audits": {
                    name: current_audits[name].model_dump(mode="json") for name in expected_audits
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        judge_call_key = tracker.request_key(
            instructions=judge_prompt,
            input_text=judge_input,
            settings=attempt.judge_settings,
            output_type=FinalJudgeVerdict,
        )
        if tracker.response_ids_by_call_key.get(judge_call_key) != gate.final_judge_response_id:
            raise StageValidationError("Accepted final verdict is not bound to its judge request.")

        attempt_dir = candidate_dir / "attempts" / attempt_name
        package_input_path = resolved_artifact(attempt.package_input_path)
        package_evidence_path = attempt_dir / "evidence.json"
        package_path = attempt_dir / "package.json"
        proof_path = attempt_dir / "proof.md"
        sources_path = attempt_dir / "source_verification.json"
        verdict_path = attempt_dir / "verdict.json"
        if (
            not package_input_path.is_file()
            or sha256_file(package_input_path) != attempt.package_input_sha256
            or not package_evidence_path.is_file()
            or sha256_file(package_evidence_path) != attempt.package_evidence_sha256
            or not package_path.is_file()
            or sha256_file(package_path) != attempt.package_sha256
            or attempt.package_sha256 != gate.candidate_sha256
            or not proof_path.is_file()
            or read_regular_text(proof_path) != current_candidate.full_proof
            or not sources_path.is_file()
            or sha256_file(sources_path) != attempt.source_verification_sha256
            or not verdict_path.is_file()
            or sha256_file(verdict_path) != attempt.verdict_sha256
            or FinalJudgeVerdict.model_validate_json(read_regular_text(verdict_path))
            != current_verdict
        ):
            raise StageValidationError("Accepted candidate evidence is incomplete or changed.")
        package_evidence = CandidatePackageEvidence.model_validate_json(
            read_regular_text(package_evidence_path)
        )
        source_verification = SourceVerificationReport.model_validate_json(
            read_regular_text(sources_path)
        )
        package_call_key = tracker.request_key(
            instructions=packager_prompt,
            input_text=read_regular_text(package_input_path),
            settings=attempt.packager_settings,
            output_type=CandidateProofPackage,
        )
        if (
            package_evidence.response_id != attempt.packager_response_id
            or package_evidence.candidate != current_candidate
            or package_evidence.source_verification != source_verification
            or tracker.response_ids_by_call_key.get(package_call_key)
            != attempt.packager_response_id
        ):
            raise StageValidationError("Accepted candidate package is not bound to its request.")
        if any(
            not set(theorem.identifiers).intersection(source_verification.verified_identifiers)
            for theorem in current_candidate.imported_theorems
        ):
            raise StageValidationError(
                "Accepted imported theorem is not supported by source-verification evidence."
            )
        expected_evidence = {
            package_evidence_path.relative_to(destination).as_posix(): (
                attempt.package_evidence_sha256
            ),
            package_path.relative_to(destination).as_posix(): attempt.package_sha256,
            sources_path.relative_to(destination).as_posix(): (attempt.source_verification_sha256),
        }
        for binding in verified_computation_bindings:
            computation_paths = {
                binding.evidence_path: binding.evidence_sha256,
                binding.manifest_path: binding.manifest_file_sha256,
                binding.replay_path: binding.replay_sha256,
            }
            for relative, digest in computation_paths.items():
                path = resolved_artifact(relative)
                if not path.is_file() or sha256_file(path) != digest:
                    raise StageValidationError(
                        "Accepted candidate computation evidence is incomplete or changed."
                    )
                existing_digest = expected_evidence.get(relative)
                if existing_digest is not None and existing_digest != digest:
                    raise StageValidationError(
                        "Accepted candidate has conflicting computation evidence bindings."
                    )
                expected_evidence[relative] = digest
        for graph_binding in verified_graph_support:
            admission_path = resolved_artifact(graph_binding.admission_record_path)
            if (
                not admission_path.is_file()
                or sha256_file(admission_path) != graph_binding.admission_record_sha256
            ):
                raise StageValidationError(
                    "Accepted candidate graph-admission evidence is incomplete or changed."
                )
            existing_digest = expected_evidence.get(graph_binding.admission_record_path)
            if (
                existing_digest is not None
                and existing_digest != graph_binding.admission_record_sha256
            ):
                raise StageValidationError(
                    "Accepted candidate has conflicting graph-admission evidence bindings."
                )
            expected_evidence[graph_binding.admission_record_path] = (
                graph_binding.admission_record_sha256
            )
        for name in expected_audits:
            audit_path = audits_dir / "attempts" / attempt_name / f"{name}.json"
            if (
                not audit_path.is_file()
                or sha256_file(audit_path) != attempt.audit_sha256[name]
                or AuditVerdict.model_validate_json(read_regular_text(audit_path))
                != current_audits[name]
            ):
                raise StageValidationError(
                    f"Accepted candidate's {name} audit artifact is incomplete or changed."
                )
            expected_evidence[audit_path.relative_to(destination).as_posix()] = (
                attempt.audit_sha256[name]
            )
        if require_pass_event:
            verdict_relative = verdict_path.relative_to(destination).as_posix()
            pass_events: list[dict[str, object]] = []
            for event_path in sorted(events_dir.glob("*.json")):
                event = json.loads(read_regular_text(event_path))
                if (
                    isinstance(event, dict)
                    and event.get("kind") == "candidate_audit_passed"
                    and event.get("artifact") == verdict_relative
                    and event.get("artifact_sha256") == attempt.verdict_sha256
                    and event.get("response_id") == gate.final_judge_response_id
                ):
                    pass_events.append(event)
            if len(pass_events) != 1:
                raise StageValidationError(
                    "Accepted research has no unique matching candidate-pass event."
                )
            related = pass_events[0].get("related_artifacts")
            if (
                not isinstance(related, list)
                or {
                    item.get("path"): item.get("sha256")
                    for item in related
                    if isinstance(item, dict)
                    and isinstance(item.get("path"), str)
                    and isinstance(item.get("sha256"), str)
                }
                != expected_evidence
            ):
                raise StageValidationError(
                    "Accepted candidate-pass event has incomplete evidence references."
                )

    def validate_refutation_gate(
        gate: CounterexampleAuditGate,
        *,
        require_pass_event: bool,
    ) -> ExactCounterexampleNomination:
        """Recompute the terminal-disproof gate and bind it to one worker result."""

        if gate.status is not CounterexampleAuditGateStatus.REFUTATION_VERIFIED:
            raise StageValidationError("Rejected research has no verified-refutation gate.")
        if scheduler.final_refutation_audit_id != gate.audit_id:
            raise StageValidationError("Rejected research names another refutation audit.")
        if scheduler.final_refutation_gate != gate.model_dump(mode="json"):
            raise StageValidationError("Rejected research gate differs from canonical state.")
        matching = [
            (record, audit_record)
            for record in scheduler.assignments
            for audit_record in record.exact_counterexample_audits
            if not audit_record.superseded and audit_record.audit_id == gate.audit_id
        ]
        if len(matching) != 1:
            raise StageValidationError(
                "Rejected research must resolve to exactly one audited worker counterexample."
            )
        record, audit_record = matching[0]
        nomination_path = resolved_artifact(audit_record.nomination_path)
        gate_path = resolved_artifact(audit_record.gate_path)
        nomination, persisted_gate = verify_persisted_counterexample_audit(
            nomination_path,
            gate_path,
            expected_target_statement=compiled.normalized_statement,
        )
        if persisted_gate != gate or (
            normalize_exact_statement(nomination.exact_statement)
            != normalize_exact_statement(compiled.normalized_statement)
        ):
            raise StageValidationError("Rejected research is bound to another exact theorem.")
        if (
            record.report_path != nomination.worker_report_path
            or record.report_sha256 != nomination.worker_report_sha256
            or record.response_id is None
            or record.request_key is None
            or tracker.response_ids_by_call_key.get(record.request_key) != record.response_id
        ):
            raise StageValidationError(
                "Rejected research is not bound to its accounted worker response."
            )
        if set(gate.response_ids) != {role.value for role in CounterexampleAuditRole} or len(
            set(gate.response_ids.values())
        ) != len(CounterexampleAuditRole):
            raise StageValidationError(
                "Rejected research lacks two distinct independent audit responses."
            )
        for role in CounterexampleAuditRole:
            request_path = gate_path.parent / "requests" / f"{role.value}.json"
            request_artifact = CounterexampleAuditRequestArtifact.model_validate_json(
                read_regular_text(request_path)
            )
            accounted_key = tracker.request_key(
                instructions=request_artifact.instructions,
                input_text=request_artifact.input_text,
                settings=request_artifact.settings,
                output_type=CounterexampleAuditResponse,
            )
            if tracker.response_ids_by_call_key.get(accounted_key) != gate.response_ids[role.value]:
                raise StageValidationError(
                    f"Rejected research's {role.value} response is not call-accounted."
                )
        if not audit_record.graph_recorded and knowledge_graph is not None:
            raise StageValidationError(
                "Rejected graph-integrated research has not committed its REFUTES edge."
            )
        if require_pass_event:
            gate_relative = gate_path.relative_to(destination).as_posix()
            pass_events: list[dict[str, object]] = []
            for event_path in sorted(events_dir.glob("*.json")):
                event = json.loads(read_regular_text(event_path))
                if (
                    isinstance(event, dict)
                    and event.get("kind") == "main_counterexample_audit_passed"
                    and event.get("artifact") == gate_relative
                    and event.get("artifact_sha256") == audit_record.gate_sha256
                    and event.get("assignment_id") == record.assignment.id
                ):
                    pass_events.append(event)
            if len(pass_events) != 1:
                raise StageValidationError(
                    "Rejected research has no unique matching refutation-pass event."
                )
        return nomination

    async def finish(
        outcome: ResearchOutcome,
        *,
        obligations: list[str] | None = None,
        strongest_result: str = "",
        acceptance_gate: ResearchAcceptanceGate | None = None,
        refutation_gate: CounterexampleAuditGate | None = None,
        audit_discovered_candidates: bool = True,
    ) -> ResearchResult:
        if active:
            unfinished = set(active)
            for task in unfinished:
                task.cancel()
            await collect_tasks(unfinished, requeue_cancelled=False)
        while (
            audit_discovered_candidates
            and outcome != ResearchOutcome.ACCEPTED
            and scheduler.final_outcome is None
        ):
            exact_counterexample_obligations = unresolved_exact_counterexample_obligations()
            if exact_counterexample_obligations:
                obligations = list(
                    dict.fromkeys([*(obligations or []), *exact_counterexample_obligations])
                )
                break
            if candidate_report_set_attempted(scheduler.pending_candidate_report_ids):
                scheduler.pending_candidate_report_ids = []
            if not scheduler.pending_candidate_report_ids:
                next_candidate = next(
                    (
                        assignment_id
                        for assignment_id in scheduler.deferred_candidate_report_ids
                        if not candidate_report_set_attempted([assignment_id])
                    ),
                    None,
                )
                if next_candidate is not None:
                    scheduler.pending_candidate_report_ids = [next_candidate]
                    scheduler.pending_candidate_source = "worker"
            if not scheduler.pending_candidate_report_ids:
                break
            # A terminal decision cannot discard self-declared complete proofs merely
            # because it has seen their reports. Give every distinct candidate report
            # the same independent gate before committing a non-accepted outcome.
            scheduler.stop_reason = None
            scheduler.stop_category = None
            scheduler.phase = SchedulerPhase.AUDITING
            persist_scheduler()
            candidate_result = await audit_pending_candidate(resume_after_failure=False)
            if candidate_result is not None:
                return candidate_result
            obligations = list(dict.fromkeys([*(obligations or []), *scheduler.repair_obligations]))
        for record in assignment_records(AssignmentLifecycle.QUEUED):
            release_unlaunched_worker_request(record)
            record.status = AssignmentLifecycle.CANCELLED
        scheduler.pending_coordinator_request = None
        if scheduler.final_outcome is not None:
            outcome = scheduler.final_outcome
            obligations = list(scheduler.final_obligations)
            strongest_result = scheduler.final_strongest_result
            acceptance_gate = (
                ResearchAcceptanceGate.model_validate(scheduler.final_acceptance_gate)
                if scheduler.final_acceptance_gate is not None
                else None
            )
            refutation_gate = (
                CounterexampleAuditGate.model_validate(scheduler.final_refutation_gate)
                if scheduler.final_refutation_gate is not None
                else None
            )
        if not strongest_result and current_verdict is not None:
            strongest_result = current_verdict.strongest_result
        if not strongest_result and current_candidate is not None:
            strongest_result = current_candidate.exact_theorem
        if outcome == ResearchOutcome.ACCEPTED:
            if obligations or not strongest_result.strip():
                raise StageValidationError(
                    "Accepted research retains obligations or has no strongest result."
                )
            if acceptance_gate is None or scheduler.latest_candidate_attempt is None:
                raise StageValidationError("Accepted research has no durable candidate attempt.")
            validate_acceptance_gate(
                acceptance_gate,
                attempt=scheduler.latest_candidate_attempt,
                require_pass_event=True,
            )
            if refutation_gate is not None:
                raise StageValidationError("Accepted research cannot retain a refutation gate.")
        elif outcome == ResearchOutcome.REJECTED:
            if acceptance_gate is not None or refutation_gate is None or obligations:
                raise StageValidationError(
                    "Rejected research requires one clean independently verified refutation."
                )
            nomination = validate_refutation_gate(refutation_gate, require_pass_event=True)
            if not strongest_result:
                strongest_result = nomination.proof_or_certificate
        elif acceptance_gate is not None:
            raise StageValidationError("Non-accepted research cannot retain an acceptance gate.")
        elif refutation_gate is not None:
            raise StageValidationError("Non-rejected research cannot retain a refutation gate.")
        if scheduler.final_outcome is None:
            scheduler.final_outcome = outcome
            scheduler.final_obligations = list(dict.fromkeys(obligations or []))
            scheduler.final_strongest_result = strongest_result
            scheduler.final_acceptance_gate = (
                acceptance_gate.model_dump(mode="json") if acceptance_gate is not None else None
            )
            scheduler.final_refutation_gate = (
                refutation_gate.model_dump(mode="json") if refutation_gate is not None else None
            )
            scheduler.final_refutation_audit_id = (
                refutation_gate.audit_id if refutation_gate is not None else None
            )
        scheduler.pending_candidate_report_ids = []
        scheduler.deferred_candidate_report_ids = []
        scheduler.pending_candidate_source = None
        scheduler.active_candidate_attempt = None
        scheduler.phase = SchedulerPhase.COMPLETE
        persist_research_index()
        finish_events = [
            json.loads(read_regular_text(event_path))
            for event_path in sorted(events_dir.glob("*.json"))
            if json.loads(read_regular_text(event_path)).get("kind") == "research_finished"
        ]
        if len(finish_events) > 1 or any(
            event.get("detail") != [outcome.value] for event in finish_events
        ):
            raise StageValidationError("Research finish event disagrees with canonical state.")
        if not finish_events:
            append_event("research_finished", detail=[outcome.value])
        persist_research_index()
        all_reports = [
            reports_by_id[record.assignment.id]
            for record in scheduler.assignments
            if record.assignment.id in reports_by_id
        ]
        register_existing_artifacts()
        result = ResearchResult(
            outcome=outcome,
            rounds=[],
            coordinator_decisions=[item.decision for item in scheduler.decisions],
            research_events=scheduler.next_event_sequence - 1,
            worker_reports=all_reports,
            registry=registry,
            candidate=current_candidate,
            audits=current_audits,
            final_verdict=current_verdict,
            unresolved_obligations=list(dict.fromkeys(obligations or [])),
            strongest_result=strongest_result,
            repair_rounds=repair_rounds,
            research_subagents_assigned=len(scheduler.assignments),
            research_subagents_used=sum(record.launched for record in scheduler.assignments),
            continuity=latest_continuity,
            acceptance_gate=acceptance_gate,
            refutation_gate=refutation_gate,
            execution_issues=list(scheduler.execution_issues),
            artifacts=ArtifactManifest(),
            calls=CallManifest(
                model_calls=tracker.calls,
                response_ids=list(dict.fromkeys(tracker.response_ids)),
            ),
        )
        result.artifacts = build_artifact_manifest(artifact_paths)
        atomic_write_json(destination / "result.json", result)
        return result

    async def pause_retriable(
        *,
        obligations: list[str],
        phase: SchedulerPhase = SchedulerPhase.AWAITING_AUDITS,
        pause_reason: str = "MANDATORY_AUDIT_UNAVAILABLE",
        resume_action: str = (
            "Run `matek resume` to retry only missing mandatory audits; completed audit "
            "artifacts remain checkpointed."
        ),
    ) -> ResearchResult:
        """Return a durable nonterminal snapshot while preserving scheduler identity."""

        if active:
            await pause_active(requeue_cancelled=True)
        if (
            scheduler.final_outcome is ResearchOutcome.REJECTED
            and scheduler.final_refutation_gate is not None
        ):
            return await finish(
                ResearchOutcome.REJECTED,
                refutation_gate=CounterexampleAuditGate.model_validate(
                    scheduler.final_refutation_gate
                ),
            )
        scheduler.phase = phase
        persist_research_index()
        strongest_result = (
            current_candidate.exact_theorem
            if current_candidate is not None
            else next(
                (
                    result
                    for approach in registry.approaches
                    for result in [approach.strongest_result]
                    if result.strip()
                ),
                "No complete result was established.",
            )
        )
        register_existing_artifacts()
        result = ResearchResult(
            outcome=ResearchOutcome.PAUSED_RETRIABLE,
            coordinator_decisions=[item.decision for item in scheduler.decisions],
            research_events=scheduler.next_event_sequence - 1,
            worker_reports=[
                reports_by_id[record.assignment.id]
                for record in scheduler.assignments
                if record.assignment.id in reports_by_id
            ],
            registry=registry,
            candidate=current_candidate,
            audits=current_audits,
            final_verdict=current_verdict,
            unresolved_obligations=list(dict.fromkeys(obligations)),
            strongest_result=strongest_result,
            repair_rounds=repair_rounds,
            research_subagents_assigned=len(scheduler.assignments),
            research_subagents_used=sum(record.launched for record in scheduler.assignments),
            continuity=latest_continuity,
            execution_issues=list(scheduler.execution_issues),
            pause_reason=pause_reason,
            resume_action=resume_action,
            calls=CallManifest(
                model_calls=tracker.calls,
                response_ids=list(dict.fromkeys(tracker.response_ids)),
            ),
        )
        result.artifacts = build_artifact_manifest(artifact_paths)
        atomic_write_json(destination / "result.json", result)
        return result

    def recent_events() -> list[dict[str, object]]:
        events: list[dict[str, object]] = []
        for event_path in sorted(events_dir.glob("*.json")):
            sequence = int(event_path.stem)
            if sequence <= scheduler.coordinator_ack_event_sequence:
                continue
            raw = json.loads(read_regular_text(event_path))
            if not isinstance(raw, dict):
                raise StageValidationError(f"Invalid research event: {event_path}")
            events.append(raw)
        return events

    def coordinator_feedback_due() -> bool:
        return bool(scheduler.requested_artifact_ids or scheduler.requested_graph_node_ids) or any(
            event.get("kind")
            in {
                "worker_report_accepted",
                "worker_execution_failed",
                "worker_repair_unavailable",
                "graph_mutation_rejected",
                "candidate_audit_failed",
                "candidate_audit_unavailable",
                "coordinator_context_compacted",
                "coordinator_input_too_large",
                "coordinator_scientific_stop_declined",
                "coordinator_unverified_refutation_stop_declined",
            }
            for event in recent_events()
        )

    def coordinator_stop_outcome() -> ResearchOutcome:
        if scheduler.stop_category == "budget":
            return ResearchOutcome.BUDGET_EXHAUSTED
        if scheduler.stop_category == "refuted":
            raise StageValidationError(
                "An unverified coordinator refutation cannot produce a terminal outcome."
            )
        return ResearchOutcome.PARTIAL

    def initial_assignment_target() -> int:
        """Scale the default bootstrap to the explicit call allowance, down to four."""

        if tracker.maximum_calls is None:
            return settings.minimum_initial_assignments
        remaining_after_coordinator = max(tracker.maximum_calls - tracker.calls - 1, 0)
        return min(settings.minimum_initial_assignments, remaining_after_coordinator)

    def compact_text(value: str | None, *, words: int = 48) -> str:
        """Extract a deterministic word-boundary summary without slicing JSON or bytes."""

        normalized = " ".join((value or "").split())
        parts = normalized.split()
        if len(parts) <= words:
            return normalized
        return " ".join(parts[:words]) + " […]"

    def coordinator_assignment_table() -> list[dict[str, object]]:
        return [
            {
                "assignment_id": record.assignment.id,
                "status": record.status.value,
                "approach_family": record.assignment.approach_family,
                "objective": compact_text(record.assignment.task),
                "target_node_ids": record.assignment.target_node_ids,
                "graph_task_id": record.graph_task_id,
                "frozen_graph_revision": record.graph_revision,
                "artifact_id": (
                    f"worker-report:{record.assignment.id}" if record.report_path else None
                ),
                "artifact_path": record.report_path,
                "artifact_sha256": record.report_sha256,
                "completed_event_sequence": record.completed_event_sequence,
            }
            for record in scheduler.assignments
        ]

    def coordinator_report_evidence() -> list[CoordinatorEvidenceItem]:
        latest_redirects = (
            set(scheduler.decisions[-1].decision.redirect_assignment_ids)
            if scheduler.decisions
            else set()
        )
        evidence: list[CoordinatorEvidenceItem] = []
        for record in scheduler.assignments:
            report = reports_by_id.get(record.assignment.id)
            if report is None or record.report_path is None or record.report_sha256 is None:
                continue
            report_path = resolved_artifact(record.report_path)
            if not report_path.is_file() or report_path.is_symlink():
                raise StageValidationError(
                    f"Coordinator report path is unavailable: {record.report_path}"
                )
            if sha256_file(report_path) != record.report_sha256:
                raise StageValidationError(
                    f"Coordinator report hash changed: {record.assignment.id}"
                )
            report_relative = report_path.relative_to(destination.parent).as_posix()
            newly_completed = (
                record.completed_event_sequence is not None
                and record.completed_event_sequence > scheduler.coordinator_ack_event_sequence
            )
            requested = f"worker-report:{record.assignment.id}" in set(
                scheduler.requested_artifact_ids
            )
            candidate = report.status is WorkerStatus.CANDIDATE_COMPLETE
            redirected = record.assignment.id in latest_redirects
            if requested:
                priority, reason = 0, "explicitly requested by the coordinator"
            elif newly_completed and candidate:
                priority, reason = 1, "newly completed candidate-producing report"
            elif candidate:
                priority, reason = 2, "candidate-producing report"
            elif newly_completed:
                priority, reason = 3, "newly completed report"
            elif redirected:
                priority, reason = 4, "explicitly redirected assignment"
            else:
                priority, reason = 10, "older completed report fitting remaining context"
            evidence.append(
                CoordinatorEvidenceItem(
                    reference=CoordinatorArtifactReference(
                        artifact_id=f"worker-report:{record.assignment.id}",
                        kind="worker_report",
                        relative_path=report_relative,
                        sha256=record.report_sha256,
                        assignment_id=record.assignment.id,
                    ),
                    summary={
                        "assignment_id": report.assignment_id,
                        "status": report.status.value,
                        "approach_family": record.assignment.approach_family,
                        "mechanism": compact_text(report.mechanism or record.assignment.task),
                        "formal_result_count": len(report.formal_results),
                        "formal_results": [
                            compact_text(item, words=64) for item in report.formal_results[:16]
                        ],
                        "counterexample_count": len(report.counterexamples),
                        "counterexamples": [
                            compact_text(item, words=64) for item in report.counterexamples[:16]
                        ],
                        "exact_gap": compact_text(report.exact_gap, words=64),
                        "dependency_count": len(report.dependencies),
                        "dependencies": [
                            compact_text(item, words=32) for item in report.dependencies[:32]
                        ],
                        "path": report_relative,
                        "sha256": record.report_sha256,
                        "completed_event_sequence": record.completed_event_sequence,
                    },
                    full_content=report.model_dump(mode="json"),
                    priority=priority,
                    inclusion_reason=reason,
                    priority_score={
                        "tier": priority,
                        "completed_event_sequence": record.completed_event_sequence,
                        "newly_completed": newly_completed,
                        "candidate_producing": candidate,
                        "redirected": redirected,
                        "approach_family": record.assignment.approach_family,
                    },
                    selection_rank=max(
                        scheduler.next_event_sequence - 1 - (record.completed_event_sequence or 0),
                        0,
                    ),
                    approach_family=record.assignment.approach_family,
                )
            )
        return evidence

    def coordinator_graph_evidence(
        graph_memory: dict[str, object] | None,
        replay_payload: dict[str, object] | None = None,
    ) -> list[CoordinatorEvidenceItem]:
        if knowledge_graph is None or graph_memory is None:
            return []
        if replay_payload is not None:
            raw_catalog = replay_payload.get("artifact_catalog", [])
            raw_summaries = replay_payload.get("graph_node_summaries", [])
            raw_full_nodes = replay_payload.get("full_graph_nodes", [])
            raw_requested_nodes = replay_payload.get("requested_graph_nodes", [])
            if (
                not isinstance(raw_catalog, list)
                or not isinstance(raw_summaries, list)
                or not isinstance(raw_full_nodes, list)
                or not isinstance(raw_requested_nodes, list)
            ):
                raise StageValidationError("Archived coordinator graph evidence is malformed.")
            summaries = {
                item["matek_id"]: item
                for item in raw_summaries
                if isinstance(item, dict) and isinstance(item.get("matek_id"), str)
            }
            full_nodes = {
                node["matek_id"]: item
                for item in [*raw_requested_nodes, *raw_full_nodes]
                if isinstance(item, dict)
                and isinstance((node := item.get("node")), dict)
                and isinstance(node.get("matek_id"), str)
            }
            references: dict[str, CoordinatorArtifactReference] = {}
            for raw_reference in raw_catalog:
                if not isinstance(raw_reference, dict) or raw_reference.get("kind") != "graph_node":
                    continue
                reference = CoordinatorArtifactReference.model_validate(raw_reference)
                if reference.graph_node_id is not None:
                    references[reference.graph_node_id] = reference
            for node_id, summary in summaries.items():
                if node_id in references:
                    continue
                path = summary.get("path")
                digest = summary.get("sha256")
                graph_revision = summary.get("graph_revision")
                if not (
                    isinstance(path, str)
                    and isinstance(digest, str)
                    and isinstance(graph_revision, str)
                ):
                    raise StageValidationError(
                        "Archived coordinator graph summary lacks authenticated provenance."
                    )
                references[node_id] = CoordinatorArtifactReference(
                    artifact_id=f"graph-node:{node_id}",
                    kind="graph_node",
                    relative_path=path,
                    sha256=digest,
                    graph_node_id=node_id,
                    graph_revision=graph_revision,
                )
            replayed: list[CoordinatorEvidenceItem] = []
            ordered_node_ids = [
                *summaries,
                *(node_id for node_id in references if node_id not in summaries),
            ]
            for node_id in ordered_node_ids:
                reference = references[node_id]
                if node_id not in summaries:
                    # A full-only archived node cannot be re-summarized without
                    # changing the immutable payload. Retain it in the catalog only.
                    continue
                summary = dict(summaries[node_id])
                raw_score = summary.get("priority_score", {})
                priority_score = dict(raw_score) if isinstance(raw_score, dict) else {}
                raw_priority = priority_score.get("tier", 8)
                priority = (
                    raw_priority
                    if isinstance(raw_priority, int) and not isinstance(raw_priority, bool)
                    else 8
                )
                raw_categories = summary.get("frontier_categories", [])
                replay_frontier_categories = (
                    [str(item) for item in raw_categories]
                    if isinstance(raw_categories, list)
                    else []
                )
                raw_rank = summary.get("selection_rank", 0)
                selection_rank = (
                    raw_rank if isinstance(raw_rank, int) and not isinstance(raw_rank, bool) else 0
                )
                replayed.append(
                    CoordinatorEvidenceItem(
                        reference=reference,
                        summary=summary,
                        full_content=dict(full_nodes.get(node_id, {})),
                        priority=priority,
                        inclusion_reason="frozen graph evidence replay",
                        frontier_categories=replay_frontier_categories,
                        priority_score=priority_score,
                        selection_rank=selection_rank,
                        approach_family=(
                            str(priority_score["approach_family"])
                            if isinstance(priority_score.get("approach_family"), str)
                            else None
                        ),
                    )
                )
            return replayed
        revision = graph_memory.get("graph_revision")
        if not isinstance(revision, str):
            raise StageValidationError("Coordinator graph memory has no frozen revision.")
        frontier = graph_memory.get("frontier", {})
        frontier_categories: dict[str, list[str]] = {}
        if isinstance(frontier, dict):
            for category, value in frontier.items():
                if not isinstance(value, list):
                    continue
                category_ids: list[str] = []
                for summary in value:
                    if isinstance(summary, dict) and isinstance(summary.get("matek_id"), str):
                        category_ids.append(str(summary["matek_id"]))
                frontier_categories[str(category)] = category_ids
        assert graph_problem_id is not None
        all_nodes = [
            node
            for node in knowledge_graph.load_nodes()
            if node.problem_id == graph_problem_id or node.matek_id == graph_problem_id
        ]
        by_id = {node.matek_id: node for node in all_nodes}
        requested_node_ids = list(dict.fromkeys(scheduler.requested_graph_node_ids))
        for node_id in requested_node_ids:
            if node_id not in by_id:
                knowledge_graph.show(node_id)
                raise StageValidationError(
                    f"Requested graph node {node_id!r} does not belong to the selected problem."
                )
        active_records = [
            record
            for record in scheduler.assignments
            if record.status in {AssignmentLifecycle.QUEUED, AssignmentLifecycle.RUNNING}
        ]
        focal_node_ids = [
            knowledge_graph.main_claim_id(graph_problem_id),
            *(
                node_id
                for record in active_records
                for node_id in record.assignment.target_node_ids
            ),
            *(
                record.graph_task_id
                for record in active_records
                if record.graph_task_id is not None
            ),
        ]
        assignment_families = {
            record.assignment.id: record.assignment.approach_family
            for record in scheduler.assignments
        }
        ranked_nodes = rank_graph_evidence(
            nodes=all_nodes,
            frontier_categories=frontier_categories,
            requested_node_ids=requested_node_ids,
            focal_node_ids=focal_node_ids,
            assignment_families=assignment_families,
            current_run_id=run_id,
        )
        result: list[CoordinatorEvidenceItem] = []
        main_target_id = knowledge_graph.main_claim_id(graph_problem_id)
        for ranked in ranked_nodes:
            node = ranked.node
            node_id = node.matek_id
            if not node.path:
                raise StageValidationError(f"Graph node {node_id!r} has no validated path.")
            unresolved_node_path = knowledge_graph.vault_root / node.path
            if unresolved_node_path.is_symlink():
                raise StageValidationError(f"Graph node path is a symlink: {node.path}")
            node_path = unresolved_node_path.resolve()
            vault_root = knowledge_graph.vault_root.resolve()
            try:
                node_path.relative_to(vault_root)
            except ValueError as exc:
                raise StageValidationError(
                    f"Graph node path escapes the frozen vault: {node.path}"
                ) from exc
            if not node_path.is_file():
                raise StageValidationError(f"Graph node path is unavailable: {node.path}")
            digest = sha256_file(node_path)
            relative_path = node_path.relative_to(knowledge_graph.project_root).as_posix()
            raw_distance = ranked.priority_score.get("graph_distance", 1_000_000)
            graph_distance = (
                raw_distance
                if isinstance(raw_distance, int) and not isinstance(raw_distance, bool)
                else 1_000_000
            )
            typed_digest = graph_node_typed_digest(
                node,
                by_id=by_id,
                graph_revision=revision,
                relative_path=relative_path,
                sha256=digest,
                graph_distance=graph_distance,
                main_target_id=main_target_id,
            )
            result.append(
                CoordinatorEvidenceItem(
                    reference=CoordinatorArtifactReference(
                        artifact_id=f"graph-node:{node_id}",
                        kind="graph_node",
                        relative_path=relative_path,
                        sha256=digest,
                        graph_node_id=node_id,
                        graph_revision=revision,
                    ),
                    summary={
                        "matek_id": node.matek_id,
                        "node_type": node.node_type.value,
                        "title": node.title,
                        "epistemic_status": node.epistemic_status.value,
                        "workflow_status": node.workflow_status.value,
                        "frontier_categories": list(ranked.frontier_categories),
                        "selection_reason": ranked.inclusion_reason,
                        "priority_score": ranked.priority_score,
                        "selection_rank": ranked.selection_rank,
                        "typed_digest": typed_digest,
                        "path": relative_path,
                        "sha256": digest,
                        "graph_revision": revision,
                    },
                    full_content={
                        "reference": {
                            "path": node_path.relative_to(knowledge_graph.project_root).as_posix(),
                            "sha256": digest,
                            "graph_revision": revision,
                        },
                        "node": node.model_dump(mode="json"),
                    },
                    priority=ranked.priority,
                    inclusion_reason=ranked.inclusion_reason,
                    frontier_categories=list(ranked.frontier_categories),
                    priority_score=ranked.priority_score,
                    selection_rank=ranked.selection_rank,
                    approach_family=ranked.approach_family,
                )
            )
        return result

    def previous_coordinator_graph_revision() -> str | None:
        """Read the graph revision actually seen by the last committed activation."""

        if not scheduler.decisions:
            return None
        previous_request = resolved_artifact(scheduler.decisions[-1].request_path)
        raw = json.loads(read_regular_text(previous_request))
        if not isinstance(raw, dict):
            raise StageValidationError("Previous coordinator request is not a JSON object.")
        memory = raw.get("knowledge_graph_memory")
        if not isinstance(memory, dict):
            return None
        revision = memory.get("graph_revision")
        return revision if isinstance(revision, str) else None

    def provider_input_character_measure(
        serialized_payload: str,
        model_settings: ModelSettings,
    ) -> int:
        request = ModelRequest(
            instructions=coordinator_prompt,
            input_text=serialized_payload,
            settings=model_settings,
        )
        measure = getattr(coordinator_client, "final_input_characters", None)
        if callable(measure):
            value = measure(request, ResearchCoordinatorDecision)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
            raise StageValidationError("Coordinator backend returned an invalid input measure.")
        return len(coordinator_prompt) + len(serialized_payload) + 32_768

    def visible_full_evidence_ids(payload: dict[str, object]) -> set[str]:
        visible: set[str] = set()
        for section in ("visible_worker_reports", "requested_artifacts"):
            raw = payload.get(section, [])
            if not isinstance(raw, list):
                continue
            for item in raw:
                if isinstance(item, dict) and isinstance(item.get("assignment_id"), str):
                    visible.add(f"worker-report:{item['assignment_id']}")
        for section in ("full_graph_nodes", "requested_graph_nodes"):
            raw = payload.get(section, [])
            if not isinstance(raw, list):
                continue
            for item in raw:
                if not isinstance(item, dict):
                    continue
                node = item.get("node")
                if isinstance(node, dict) and isinstance(node.get("matek_id"), str):
                    visible.add(f"graph-node:{node['matek_id']}")
        return visible

    def canonical_supporting_evidence_id(
        raw_id: str,
        *,
        known_worker_artifact_ids: set[str],
    ) -> str:
        normalized = raw_id.strip()
        if normalized in known_worker_artifact_ids:
            return normalized
        worker_reference = f"worker-report:{normalized}"
        if worker_reference in known_worker_artifact_ids:
            return worker_reference
        graph_node_id = normalized.removeprefix("graph-node:")
        if knowledge_graph is not None:
            try:
                knowledge_graph.show(graph_node_id)
            except GraphValidationError:
                pass
            else:
                return f"graph-node:{graph_node_id}"
        raise StageValidationError(f"Coordinator cited unknown supporting evidence ID: {raw_id}")

    def defer_consequential_action_for_omitted_evidence(
        decision: ResearchCoordinatorDecision,
        *,
        payload: dict[str, object],
    ) -> ResearchCoordinatorDecision:
        """Convert a consequential v3 decision into bounded retrieval-only work."""

        if payload.get("coordinator_payload_schema_version") != COORDINATOR_PAYLOAD_SCHEMA_VERSION:
            return decision
        known_worker_artifact_ids = {
            f"worker-report:{record.assignment.id}"
            for record in scheduler.assignments
            if record.report_path is not None
        }
        consequential = bool(
            decision.candidate_packaging_recommended or decision.resolved_contradiction_node_ids
        )
        implied_ids: list[str] = []
        if decision.candidate_packaging_recommended:
            implied_ids.extend(
                f"worker-report:{assignment_id}" for assignment_id in decision.candidate_report_ids
            )
        implied_ids.extend(
            f"graph-node:{node_id}" for node_id in decision.resolved_contradiction_node_ids
        )

        promising_categories = {
            "unresolved_contradictions",
            "missing_dependencies",
            "high_value_tasks",
            "candidate_proofs_awaiting_audit",
            "unresolved_claims",
        }
        promising_node_ids: set[str] = set()
        raw_memory = payload.get("knowledge_graph_memory")
        if isinstance(raw_memory, dict):
            raw_frontier = raw_memory.get("frontier")
            if isinstance(raw_frontier, dict):
                for category in promising_categories:
                    items = raw_frontier.get(category, [])
                    if not isinstance(items, list):
                        continue
                    promising_node_ids.update(
                        str(item["matek_id"])
                        for item in items
                        if isinstance(item, dict) and isinstance(item.get("matek_id"), str)
                    )
        raw_graph_summaries = payload.get("graph_node_summaries", [])
        if isinstance(raw_graph_summaries, list):
            for item in raw_graph_summaries:
                if not isinstance(item, dict) or not isinstance(item.get("matek_id"), str):
                    continue
                categories = item.get("frontier_categories", [])
                if isinstance(categories, list) and promising_categories.intersection(
                    str(category) for category in categories
                ):
                    promising_node_ids.add(str(item["matek_id"]))
        for assignment_id in decision.retire_assignment_ids:
            record = scheduler.assignment_record(assignment_id)
            if record is None or not record.launched or knowledge_graph is None:
                continue
            cited_targets = [
                node_id
                for node_id in record.assignment.target_node_ids
                if node_id in promising_node_ids
            ]
            if cited_targets:
                consequential = True
                implied_ids.extend(f"graph-node:{node_id}" for node_id in cited_targets)

        if not consequential:
            return decision
        cited_ids = [
            canonical_supporting_evidence_id(
                raw_id, known_worker_artifact_ids=known_worker_artifact_ids
            )
            for raw_id in [*decision.supporting_evidence_ids, *implied_ids]
        ]
        cited_ids = list(dict.fromkeys(cited_ids))
        missing = [
            evidence_id
            for evidence_id in cited_ids
            if evidence_id not in visible_full_evidence_ids(payload)
        ]
        if not missing:
            return decision

        artifact_requests = list(decision.requested_artifact_ids)
        graph_requests = list(decision.requested_graph_node_ids)
        for evidence_id in missing:
            if evidence_id.startswith("graph-node:"):
                graph_requests.append(evidence_id.removeprefix("graph-node:"))
            else:
                artifact_requests.append(evidence_id)
        request_limit = settings.maximum_coordinator_requested_artifacts
        artifact_requests = list(dict.fromkeys(artifact_requests))[:request_limit]
        graph_requests = list(dict.fromkeys(graph_requests))[:request_limit]
        deferred = decision.model_copy(
            update={
                "assignments": [],
                "retire_assignment_ids": [],
                "redirect_assignment_ids": [],
                "claims_requiring_counterexample_search": [],
                "lemmas_requiring_proof_completion": [],
                "candidate_packaging_recommended": False,
                "candidate_report_ids": [],
                "resolved_contradiction_node_ids": [],
                "stop_recommended": False,
                "stop_reason": None,
                "requested_artifact_ids": artifact_requests,
                "requested_graph_node_ids": graph_requests,
                "rationale": (
                    decision.rationale
                    + " MATEK deferred the consequential action because cited full evidence "
                    "was omitted from this activation; this is a retrieval-only decision."
                ),
            }
        )
        append_event(
            "coordinator_evidence_retrieval_deferred",
            decision_id=decision.decision_id,
            detail=[
                "A candidate, contradiction resolution, or promising-branch retirement "
                "cited evidence that was not visible in full.",
                "Deferred every consequential action and requested: " + ", ".join(missing),
            ],
        )
        return deferred

    async def request_coordinator_decision(*, initial: bool) -> ResearchCoordinatorDecision:
        nonlocal scientific_phase_state
        if len(scheduler.decisions) >= settings.maximum_coordinator_decisions:
            raise _ResearchBudgetExhausted
        pending_request = scheduler.pending_coordinator_request
        headroom_record: ResearchAssignmentState | None = None
        if pending_request is not None and pending_request.headroom_assignment_id is not None:
            headroom_record = scheduler.assignment_record(pending_request.headroom_assignment_id)
            if (
                headroom_record is None
                or headroom_record.status != AssignmentLifecycle.QUEUED
                or headroom_record.launched
                or headroom_record.request_key is not None
            ):
                raise StageValidationError(
                    "Frozen coordinator request has inconsistent headroom metadata."
                )
        elif pending_request is None and not initial and not tracker.can_call():
            borrowable = [
                record
                for record in assignment_records(AssignmentLifecycle.QUEUED)
                if not record.launched and record.request_key is not None
            ]
            if borrowable:
                # The assignment remains visible to the coordinator as queued. Its unused
                # provider slot is exchanged transactionally for this feedback activation;
                # the returned decision must retire enough queued work to restore funding.
                headroom_record = borrowable[-1]
        event_sequence = scheduler.next_event_sequence - 1
        decision_id = len(scheduler.decisions) + 1
        open_records = assignment_records(AssignmentLifecycle.QUEUED, AssignmentLifecycle.RUNNING)
        available_new_assignment_slots = max(
            settings.maximum_pending_assignments - len(open_records), 0
        )
        refundable_queued_assignments = sum(
            record.status == AssignmentLifecycle.QUEUED
            and not record.launched
            and record.request_key is not None
            for record in open_records
        )
        model_call_limited_new_assignments = (
            settings.maximum_pending_assignments
            if tracker.maximum_calls is None
            else max(
                tracker.maximum_calls - tracker.calls - 1 + refundable_queued_assignments,
                0,
            )
        )
        completed_ids = set(reports_by_id)
        payload: dict[str, object] = {
            "coordinator_mode": "continuous_event_driven",
            "research_agent_hierarchy": {
                "mode": settings.orchestration_mode,
                "max_concurrent_agents": settings.max_concurrent_agents,
                "max_concurrent_first_level_agents": settings.maximum_concurrent_agents,
                "subagents_per_agent": settings.hierarchical_subagent_limit,
                "max_nested_agent_depth": (1 if settings.hierarchical_subagent_limit > 0 else 0),
                "instruction": (
                    "Each research subagent may use its bounded sub-subagent pool and must "
                    "synthesize nested work into its own report."
                    if settings.hierarchical_subagent_limit > 0
                    else "All research workers are regular subagents without nested delegation."
                ),
            },
            "compiled_prompt": compiled.compiled_prompt,
            "claim_contract": compiled.claim_contract.as_dict(),
            "literature_refresh": literature_refresh_payload(),
            "exact_target_policy": exact_target_policy(),
            "scientific_phase_state": scientific_phase_payload(decision_id),
            "decision_id": decision_id,
            "after_event_sequence": event_sequence,
            "initial_portfolio": initial,
            "minimum_materially_diverse_initial_assignments": (
                initial_assignment_target() if initial else 0
            ),
            "maximum_open_assignments": settings.maximum_pending_assignments,
            "available_new_assignment_slots": available_new_assignment_slots,
            "available_new_assignments_without_replacement": (
                settings.maximum_pending_assignments
                if tracker.maximum_calls is None
                else max(tracker.maximum_calls - tracker.calls - 1, 0)
            ),
            "refundable_unlaunched_assignment_count": refundable_queued_assignments,
            "coordinator_headroom_borrowed_assignment_id": (
                headroom_record.assignment.id if headroom_record is not None else None
            ),
            "maximum_new_assignments_this_decision": min(
                settings.maximum_pending_assignments,
                model_call_limited_new_assignments,
            ),
            "replacement_rule": (
                "New assignments may replace open assignments named in retire_assignment_ids "
                "or redirect_assignment_ids. If coordinator_headroom_borrowed_assignment_id "
                "is non-null, one unused worker reservation funds this coordinator call; "
                "retire or redirect enough unlaunched assignments to restore that queued "
                "assignment (unless retiring it) and fund every new assignment. The resulting "
                "open count must not exceed maximum_open_assignments."
            ),
            "maximum_concurrent_workers": active_scientific_concurrency(),
            # Bootstrap assignments and later refills use the same worker model and
            # admission pool. Expose the search policy so the logical coordinator can
            # plan literature-facing work without guessing worker capabilities.
            "worker_web_search_enabled": worker_model.web_search,
            "open_assignment_count": len(open_records),
            "assignment_lifecycle": [
                {
                    "assignment": record.assignment.model_dump(mode="json"),
                    "admitted_by_decision": record.admitted_by_decision,
                    "status": record.status.value,
                    "launched": record.launched,
                    "report_path": record.report_path,
                    "report_sha256": record.report_sha256,
                    "completed_event_sequence": record.completed_event_sequence,
                }
                for record in scheduler.assignments
            ],
            "queued_assignments": [
                record.assignment.model_dump(mode="json")
                for record in assignment_records(AssignmentLifecycle.QUEUED)
            ],
            "active_assignments": [
                record.assignment.model_dump(mode="json")
                for record in assignment_records(AssignmentLifecycle.RUNNING)
            ],
            "approach_registry": registry.model_dump(mode="json"),
            "research_continuity": (latest_continuity or build_continuity()).model_dump(
                mode="json"
            ),
            "visible_worker_reports": [
                reports_by_id[record.assignment.id].model_dump(mode="json")
                for record in scheduler.assignments
                if record.assignment.id in reports_by_id
            ],
            "unacknowledged_events": recent_events(),
            "audit_repair_obligations": scheduler.repair_obligations,
            "latest_candidate_package": (
                current_candidate.model_dump(mode="json") if current_candidate is not None else None
            ),
            "latest_independent_audits": {
                name: audit.model_dump(mode="json") for name, audit in current_audits.items()
            },
            "latest_final_judge_verdict": (
                current_verdict.model_dump(mode="json") if current_verdict is not None else None
            ),
            "remaining_coordinator_decisions_after_this_call": (
                settings.maximum_coordinator_decisions - decision_id
            ),
            "remaining_model_calls_before_this_call": (
                None if tracker.maximum_calls is None else tracker.maximum_calls - tracker.calls
            ),
        }
        graph_memory: dict[str, object] | None = None
        replayed_graph_payload: dict[str, object] | None = None
        prior_graph_revision = previous_coordinator_graph_revision()
        if knowledge_graph is not None:
            assert graph_problem_id is not None
            replay_request = (
                replay_root / "coordinator" / "requests" / f"{decision_id:08d}.json"
                if replay_root is not None
                else None
            )
            if replay_request is not None and replay_request.is_file():
                replay_payload = json.loads(read_regular_text(replay_request))
                replay_memory = (
                    replay_payload.get("knowledge_graph_memory")
                    if isinstance(replay_payload, dict)
                    else None
                )
                if not isinstance(replay_memory, dict):
                    raise StageValidationError(
                        "Archived graph-integrated coordinator request is malformed."
                    )
                graph_memory = replay_memory
                replayed_graph_payload = replay_payload
            else:
                graph_memory = knowledge_graph.coordinator_memory(
                    graph_problem_id,
                    current_run_id=run_id,
                    resume_reconstruction=resumed,
                    previous_coordinator_revision=prior_graph_revision,
                )
            payload["knowledge_graph_memory"] = graph_memory
        prior_node_count = 0
        if graph_memory is not None:
            overview = graph_memory.get("overview")
            if isinstance(overview, dict):
                raw_prior_count = overview.get("prior_node_count", 0)
                if isinstance(raw_prior_count, int) and not isinstance(raw_prior_count, bool):
                    prior_node_count = raw_prior_count
        activation_kind = (
            "resume"
            if resumed
            else "existing_graph_bootstrap"
            if initial and prior_node_count > 0
            else "bootstrap"
            if initial
            else "continuation"
        )
        payload["activation_context"] = {
            "kind": activation_kind,
            "run_resumed_from_canonical_checkpoint": resumed,
            "provider_conversation_memory_assumed": False,
            "coordinator_decisions_already_committed": len(scheduler.decisions),
            "durable_events_already_committed": event_sequence,
            "knowledge_graph_contract_version": 1 if graph_memory is not None else None,
            "branch_target_binding_required": graph_memory is not None,
            "graph_review_attestation_required": graph_memory is not None,
            "current_graph_revision": (
                graph_memory.get("graph_revision") if graph_memory is not None else None
            ),
            "previous_coordinator_graph_revision": prior_graph_revision,
            "graph_changed_since_previous_coordinator_activation": (
                graph_memory.get("graph_changed_since_previous_coordinator_activation", False)
                if graph_memory is not None
                else False
            ),
            "instruction": (
                "Reconstruct the scientific state from the canonical scheduler, new events, "
                "continuity, registry, audits, and the current graph frontier before directing "
                "work. Compare productive, blocked, and ruled-out branches; look for proof "
                "synthesis across branches; then bind every new assignment to explicit stable "
                "target_node_ids. Include the current graph revision verbatim in the decision "
                "rationale."
                if graph_memory is not None
                else "Reconstruct state only from the durable scheduler payload; never assume "
                "provider conversation memory."
            ),
        }
        decision_model_settings = coordinator_model
        control_keys = {
            "coordinator_mode",
            "activation_context",
            "research_agent_hierarchy",
            "compiled_prompt",
            "claim_contract",
            "literature_refresh",
            "decision_id",
            "after_event_sequence",
            "initial_portfolio",
            "minimum_materially_diverse_initial_assignments",
            "maximum_open_assignments",
            "available_new_assignment_slots",
            "available_new_assignments_without_replacement",
            "refundable_unlaunched_assignment_count",
            "coordinator_headroom_borrowed_assignment_id",
            "maximum_new_assignments_this_decision",
            "replacement_rule",
            "maximum_concurrent_workers",
            "worker_web_search_enabled",
            "open_assignment_count",
            "audit_repair_obligations",
            "exact_target_policy",
            "scientific_phase_state",
            "latest_independent_audits",
            "latest_final_judge_verdict",
            "remaining_coordinator_decisions_after_this_call",
            "remaining_model_calls_before_this_call",
        }

        def compact_base_payload(source_payload: dict[str, object]) -> dict[str, object]:
            # An indexed first build is rebuilt after its durable compaction event.
            # Optional bulky audit fields have already been represented by the
            # bounded audit_recovery_state and need not be re-expanded.
            base = {key: source_payload[key] for key in control_keys if key in source_payload}
            base["filesystem_retrieval"] = (
                {
                    "enabled": True,
                    "instruction": (
                        "You may read referenced run artifacts and graph nodes from the "
                        "workspace. Validate the relative path and SHA-256 in the catalog "
                        "before relying on deeper evidence."
                    ),
                }
                if coordinator_can_read_files
                else {
                    "enabled": False,
                    "instruction": (
                        "Request omitted evidence by stable ID. MATEK will inline a bounded "
                        "authenticated artifact set on the next activation."
                    ),
                }
            )
            base["approach_registry"] = {
                "approaches": [
                    {
                        "branch_id": approach.branch_id,
                        "approach_family": approach.family,
                        "status": approach.status,
                        "mechanism": compact_text(approach.mechanism),
                        "exact_gap": compact_text(approach.exact_gap, words=64),
                        "target_node_ids": approach.target_node_ids,
                        "reopen_condition": compact_text(approach.reopen_condition, words=48),
                        "assignment_ids": approach.assignment_ids,
                    }
                    for approach in registry.approaches
                ]
            }
            continuity = latest_continuity or build_continuity()
            base["research_continuity"] = {
                "after_event_sequence": continuity.after_event_sequence,
                "open_gaps": [compact_text(item, words=64) for item in continuity.open_gaps],
                "counterexamples": [
                    compact_text(item, words=64) for item in continuity.counterexamples
                ],
                "dependencies": [compact_text(item, words=48) for item in continuity.dependencies],
                "retired_assignment_ids": continuity.retired_assignment_ids,
                "redirected_assignment_ids": continuity.redirected_assignment_ids,
                "completed_assignment_ids": continuity.completed_assignment_ids,
            }
            if current_candidate is not None:
                candidate_path = candidate_dir / "package.json"
                base["latest_candidate_state"] = {
                    "exact_theorem": current_candidate.exact_theorem,
                    "unresolved_items": current_candidate.unresolved_items,
                    "imported_theorems": [
                        {
                            "name": theorem.name,
                            "verified": theorem.verified,
                            "source_id": theorem.source_id,
                        }
                        for theorem in current_candidate.imported_theorems
                    ],
                    "path": (
                        candidate_path.relative_to(destination.parent).as_posix()
                        if candidate_path.is_file()
                        else None
                    ),
                    "sha256": sha256_file(candidate_path) if candidate_path.is_file() else None,
                }
            else:
                base["latest_candidate_state"] = None
            return base

        def indexed_base_payload(source_payload: dict[str, object]) -> dict[str, object]:
            """Keep exact controls inline while replacing cumulative state with an index."""

            indexed_control_keys = control_keys - {
                "audit_repair_obligations",
                "latest_independent_audits",
                "latest_final_judge_verdict",
            }
            base = {
                key: source_payload[key] for key in indexed_control_keys if key in source_payload
            }
            assignment_counts = {
                status.value: len(assignment_records(status)) for status in AssignmentLifecycle
            }
            base["scheduler_state_index"] = {
                "canonical_path": "research/coordinator/state.json",
                "assignment_count": len(scheduler.assignments),
                "assignment_counts": assignment_counts,
                "event_ledger_pattern": "research/events/<zero-padded-sequence>.json",
                "event_sequence": event_sequence,
                "instruction": (
                    "This indexed context replaces cumulative scheduler history. Running and "
                    "queued assignments, new events, report summaries, and hash-bound evidence "
                    "references are prioritized below. Use decision-scoped assignment IDs to "
                    "avoid colliding with older omitted IDs."
                ),
            }
            base["audit_recovery_state"] = {
                "obligation_count": len(scheduler.repair_obligations),
                "obligations": [
                    compact_text(item, words=48) for item in scheduler.repair_obligations[:16]
                ],
                "audits": {
                    name: {
                        "verdict": audit.verdict.value,
                        "rationale": compact_text(audit.rationale, words=48),
                        "unresolved_obligations": [
                            compact_text(item, words=32)
                            for item in audit.unresolved_obligations[:8]
                        ],
                    }
                    for name, audit in current_audits.items()
                },
                "final_judge": (
                    {
                        "verdict": current_verdict.verdict.value,
                        "reasons": [
                            compact_text(item, words=32) for item in current_verdict.reasons[:8]
                        ],
                        "unresolved_obligations": [
                            compact_text(item, words=32)
                            for item in current_verdict.unresolved_obligations[:8]
                        ],
                    }
                    if current_verdict is not None
                    else None
                ),
            }
            if current_candidate is not None:
                candidate_path = candidate_dir / "package.json"
                base["latest_candidate_state"] = {
                    "exact_theorem": compact_text(current_candidate.exact_theorem, words=96),
                    "unresolved_item_count": len(current_candidate.unresolved_items),
                    "unresolved_items": [
                        compact_text(item, words=48)
                        for item in current_candidate.unresolved_items[:16]
                    ],
                    "imported_theorems": [
                        {
                            "name": theorem.name,
                            "verified": theorem.verified,
                            "source_id": theorem.source_id,
                        }
                        for theorem in current_candidate.imported_theorems[:32]
                    ],
                    "path": (
                        candidate_path.relative_to(destination.parent).as_posix()
                        if candidate_path.is_file()
                        else None
                    ),
                    "sha256": sha256_file(candidate_path) if candidate_path.is_file() else None,
                }
            else:
                base["latest_candidate_state"] = None
            base["approach_registry_index"] = {
                "approach_count": len(registry.approaches),
                "families": [approach.family for approach in registry.approaches[:64]],
                "status_counts": {
                    status: sum(approach.status == status for approach in registry.approaches)
                    for status in sorted({approach.status for approach in registry.approaches})
                },
            }
            continuity = latest_continuity or build_continuity()
            base["research_continuity_index"] = {
                "after_event_sequence": continuity.after_event_sequence,
                "open_gap_count": len(continuity.open_gaps),
                "open_gaps": [compact_text(item, words=48) for item in continuity.open_gaps[:16]],
                "counterexample_count": len(continuity.counterexamples),
                "counterexamples": [
                    compact_text(item, words=48) for item in continuity.counterexamples[:16]
                ],
                "dependency_count": len(continuity.dependencies),
            }
            if graph_memory is not None:
                frontier = graph_memory.get("frontier", {})
                base["knowledge_graph_memory"] = {
                    "graph_revision": graph_memory.get("graph_revision"),
                    "problem_id": graph_memory.get("problem_id"),
                    "review_required_before_delegation": graph_memory.get(
                        "review_required_before_delegation", False
                    ),
                    "overview": graph_memory.get("overview", {}),
                    "frontier_counts": {
                        str(name): len(items) if isinstance(items, list) else 0
                        for name, items in frontier.items()
                    }
                    if isinstance(frontier, dict)
                    else {},
                    "instruction": graph_memory.get("instruction"),
                }
            return base

        def build_context(
            source_payload: dict[str, object],
            *,
            character_limit: int,
            force_compact: bool,
        ) -> tuple[dict[str, object], CoordinatorContextManifest]:
            source_payload["after_event_sequence"] = event_sequence
            source_payload["unacknowledged_events"] = recent_events()
            report_evidence = coordinator_report_evidence()
            graph_evidence = coordinator_graph_evidence(graph_memory, replayed_graph_payload)
            complete_catalog = [
                item.reference.model_dump(mode="json")
                for item in [*report_evidence, *graph_evidence]
            ]
            catalog_identity = sha256_json(complete_catalog)
            complete_catalog_path = _atomic_write_immutable_json(
                context_catalogs_dir / f"{decision_id:08d}-{catalog_identity[:16]}.json",
                {
                    "schema_version": 1,
                    "decision_id": decision_id,
                    "artifacts": complete_catalog,
                },
            )
            artifact_paths[f"coordinator_artifact_catalog_{decision_id}_{catalog_identity[:8]}"] = (
                complete_catalog_path
            )
            builder = CoordinatorContextBuilder(
                configured_character_limit=settings.maximum_coordinator_context_characters,
                effective_character_limit=character_limit,
                provider_input_characters=lambda serialized: provider_input_character_measure(
                    serialized, decision_model_settings
                ),
                graph_summary_character_limit=(
                    knowledge_graph.maximum_context_characters
                    if knowledge_graph is not None
                    else 60_000
                ),
                unrequested_full_graph_nodes_character_limit=(
                    settings.maximum_unrequested_full_graph_node_characters
                ),
            )
            built = builder.build(
                decision_id=decision_id,
                after_event_sequence=event_sequence,
                normal_payload=source_payload,
                compact_base=compact_base_payload(source_payload),
                indexed_base=indexed_base_payload(source_payload),
                events=recent_events(),
                assignment_table=coordinator_assignment_table(),
                report_evidence=report_evidence,
                graph_memory=graph_memory,
                graph_evidence=graph_evidence,
                requested_artifact_ids=scheduler.requested_artifact_ids,
                requested_graph_node_ids=scheduler.requested_graph_node_ids,
                artifact_catalog_descriptor={
                    "relative_path": complete_catalog_path.relative_to(
                        destination.parent
                    ).as_posix(),
                    "sha256": sha256_file(complete_catalog_path),
                },
                force_compact=(
                    force_compact
                    or bool(graph_evidence)
                    or bool(scheduler.requested_artifact_ids)
                    or bool(scheduler.requested_graph_node_ids)
                ),
            )
            return built.payload, built.manifest

        legacy_unbounded_request: PendingCoordinatorRequest | None = None
        if pending_request is not None and pending_request.context_manifest_path is None:
            # Pre-context-budget checkpoints may contain a provider-rejected oversized
            # request. Preserve its immutable artifact, but never replay it unchanged.
            legacy_unbounded_request = pending_request
            decision_model_settings = pending_request.request_settings
            scheduler.pending_coordinator_request = None
            pending_request = None
        if pending_request is not None:
            if pending_request.decision_id != decision_id:
                raise StageValidationError(
                    "Pending coordinator request has an unexpected decision ID."
                )
            if pending_request.initial != initial:
                raise StageValidationError(
                    "Pending coordinator request has inconsistent bootstrap state."
                )
            event_sequence = pending_request.after_event_sequence
            decision_model_settings = pending_request.request_settings
            request_path = resolved_artifact(pending_request.request_path)
            payload = dict(pending_request.request_payload)
            if sha256_json(payload) != pending_request.request_sha256:
                raise StageValidationError("Frozen coordinator request state is inconsistent.")
            _atomic_write_immutable_json(request_path, payload)
            if sha256_file(request_path) != pending_request.request_sha256:
                raise StageValidationError("Frozen coordinator request is missing or changed.")
            if pending_request.context_manifest_path is None:
                raise StageValidationError("Frozen coordinator request has no context manifest.")
            context_manifest_path = resolved_artifact(pending_request.context_manifest_path)
            if (
                not context_manifest_path.is_file()
                or pending_request.context_manifest_sha256 is None
                or sha256_file(context_manifest_path) != pending_request.context_manifest_sha256
            ):
                raise StageValidationError(
                    "Frozen coordinator context manifest is missing or changed."
                )
            context_manifest = CoordinatorContextManifest.model_validate_json(
                read_regular_text(context_manifest_path)
            )
            serialized_pending = serialize_coordinator_payload(payload)
            if (
                context_manifest.decision_id != decision_id
                or context_manifest.after_event_sequence != event_sequence
                or context_manifest.effective_character_limit
                != pending_request.context_character_limit
                or context_manifest.payload_sha256 != sha256_text(serialized_pending)
            ):
                raise StageValidationError(
                    "Frozen coordinator context manifest is bound to other state."
                )
        else:
            payload, context_manifest = build_context(
                payload,
                character_limit=settings.maximum_coordinator_context_characters,
                force_compact=False,
            )
            if context_manifest.mode != "normal":
                append_event(
                    "coordinator_context_compacted",
                    decision_id=decision_id,
                    detail=[
                        f"Context mode: {context_manifest.mode}.",
                        f"Omitted {len(context_manifest.omitted_artifacts)} full artifacts; "
                        "authenticated references remain available.",
                        f"Effective provider-input limit: "
                        f"{context_manifest.effective_character_limit} characters.",
                    ],
                )
                event_sequence = scheduler.next_event_sequence - 1
                payload["after_event_sequence"] = event_sequence
                payload, context_manifest = build_context(
                    payload,
                    character_limit=settings.maximum_coordinator_context_characters,
                    force_compact=True,
                )
            context_manifest_path = _atomic_write_immutable_json(
                context_manifests_dir / f"{decision_id:08d}-01.json",
                context_manifest,
            )
            artifact_paths[f"coordinator_context_manifest_{decision_id}_1"] = context_manifest_path
            request_path = requests_dir / (
                f"{decision_id:08d}-bounded-01.json"
                if legacy_unbounded_request is not None
                else f"{decision_id:08d}.json"
            )
            headroom_worker_request_key: str | None = None
            if headroom_record is not None:
                headroom_worker_request_key = (
                    legacy_unbounded_request.headroom_worker_request_key
                    if legacy_unbounded_request is not None
                    else headroom_record.request_key
                )
                if headroom_worker_request_key is None:
                    raise StageValidationError(
                        "Coordinator headroom assignment has no refundable request."
                    )
                if legacy_unbounded_request is None:
                    release_unlaunched_worker_request(headroom_record)
            scheduler.pending_coordinator_request = PendingCoordinatorRequest(
                decision_id=decision_id,
                after_event_sequence=event_sequence,
                initial=initial,
                request_settings=decision_model_settings.model_copy(deep=True),
                request_path=request_path.relative_to(destination).as_posix(),
                request_sha256=sha256_json(payload),
                request_payload=payload,
                context_generation=1,
                context_character_limit=context_manifest.effective_character_limit,
                context_manifest_path=context_manifest_path.relative_to(destination).as_posix(),
                context_manifest_sha256=sha256_file(context_manifest_path),
                headroom_assignment_id=(
                    headroom_record.assignment.id if headroom_record is not None else None
                ),
                headroom_worker_request_key=headroom_worker_request_key,
            )
            # The canonical state is the write-ahead record. Resume can materialize
            # the immutable request file from this exact payload after any interruption.
            persist_scheduler()
            _atomic_write_immutable_json(request_path, payload)
        artifact_paths[f"coordinator_request_{decision_id}"] = request_path
        maximum_new_assignments = payload.get("maximum_new_assignments_this_decision")
        if not isinstance(maximum_new_assignments, int):
            raise StageValidationError(
                "Frozen coordinator request has no valid assignment allowance."
            )
        minimum_initial_assignments = payload.get("minimum_materially_diverse_initial_assignments")
        if (
            not isinstance(minimum_initial_assignments, int)
            or isinstance(minimum_initial_assignments, bool)
            or minimum_initial_assignments < (4 if initial else 0)
            or (not initial and minimum_initial_assignments != 0)
        ):
            raise StageValidationError(
                "Frozen coordinator request has an invalid initial-portfolio target."
            )
        coordinator_input = serialize_coordinator_payload(payload)
        provider_characters = provider_input_character_measure(
            coordinator_input, decision_model_settings
        )
        frozen_context_limit = (
            scheduler.pending_coordinator_request.context_character_limit
            if scheduler.pending_coordinator_request is not None
            else settings.maximum_coordinator_context_characters
        )
        if provider_characters > frozen_context_limit:
            raise CoordinatorContextBudgetExhausted(
                limit=frozen_context_limit,
                required=provider_characters,
                diagnostic="OPTIONAL_CONTEXT_PREFLIGHT_FAILED",
            )
        context_rebuilds = 0
        while True:
            try:
                result = await generate_model(
                    instructions=coordinator_prompt,
                    input_text=coordinator_input,
                    model_settings=decision_model_settings,
                    output_type=ResearchCoordinatorDecision,
                    selected_client=coordinator_client,
                )
                break
            except ModelInputTooLargeError as exc:
                context_rebuilds += 1
                record_execution_issue(
                    event_kind="coordinator_input_too_large",
                    exc=exc,
                    category=FailureCategory.RESOURCE,
                    extra_obligations=[
                        "Rebuild the same coordinator activation under a smaller measured "
                        "context limit; never resend the identical payload."
                    ],
                )
                current_pending = scheduler.pending_coordinator_request
                if current_pending is None:
                    raise CoordinatorContextBudgetExhausted(
                        limit=frozen_context_limit,
                        required=provider_characters,
                        diagnostic="PROVIDER_CONTEXT_REJECTED_AFTER_COMPACTION",
                    ) from exc
                mandatory_retry_payload: dict[str, object] = {
                    key: payload[key]
                    for key in ("compiled_prompt", "claim_contract")
                    if key in payload
                }
                mandatory_retry_characters = provider_input_character_measure(
                    serialize_coordinator_payload(mandatory_retry_payload),
                    decision_model_settings,
                )
                reduced_limit = max(
                    mandatory_retry_characters + 10_000,
                    min(
                        frozen_context_limit - 65_536,
                        frozen_context_limit * 3 // 4,
                        provider_characters - 1,
                    ),
                )
                if reduced_limit >= frozen_context_limit:
                    raise CoordinatorContextBudgetExhausted(
                        limit=frozen_context_limit,
                        required=provider_characters,
                        diagnostic="PROVIDER_CONTEXT_REJECTED_AFTER_COMPACTION",
                    ) from exc
                frozen_context_limit = reduced_limit
                event_sequence = scheduler.next_event_sequence - 1
                payload["after_event_sequence"] = event_sequence
                # Independently of the numeric limit, remove at least one
                # lowest-priority transport field per rejected generation. This
                # guarantees the next request is smaller even when the provider
                # rejects an already tiny payload far below its advertised limit.
                rejected_generation_pruning_order = (
                    "remaining_model_calls_before_this_call",
                    "remaining_coordinator_decisions_after_this_call",
                    "open_assignment_count",
                )
                for optional_key in rejected_generation_pruning_order[:context_rebuilds]:
                    payload.pop(optional_key, None)
                payload, context_manifest = build_context(
                    payload,
                    character_limit=frozen_context_limit,
                    force_compact=True,
                )
                append_event(
                    "coordinator_context_compacted",
                    decision_id=decision_id,
                    detail=[
                        "Provider rejected the preceding serialized input as too large.",
                        f"Rebuilt generation {current_pending.context_generation + 1} "
                        f"under a {frozen_context_limit}-character limit.",
                        f"Omitted {len(context_manifest.omitted_artifacts)} full artifacts; "
                        "authenticated references remain available.",
                    ],
                )
                event_sequence = scheduler.next_event_sequence - 1
                payload, context_manifest = build_context(
                    payload,
                    character_limit=frozen_context_limit,
                    force_compact=True,
                )
                generation = current_pending.context_generation + 1
                context_manifest_path = _atomic_write_immutable_json(
                    context_manifests_dir / f"{decision_id:08d}-{generation:02d}.json",
                    context_manifest,
                )
                request_path = requests_dir / f"{decision_id:08d}-rebuild-{generation:02d}.json"
                current_pending.after_event_sequence = event_sequence
                current_pending.request_path = request_path.relative_to(destination).as_posix()
                current_pending.request_payload = payload
                current_pending.request_sha256 = sha256_json(payload)
                current_pending.context_generation = generation
                current_pending.context_character_limit = frozen_context_limit
                current_pending.context_manifest_path = context_manifest_path.relative_to(
                    destination
                ).as_posix()
                current_pending.context_manifest_sha256 = sha256_file(context_manifest_path)
                persist_scheduler()
                _atomic_write_immutable_json(request_path, payload)
                artifact_paths[f"coordinator_request_{decision_id}_rebuild_{generation}"] = (
                    request_path
                )
                artifact_paths[f"coordinator_context_manifest_{decision_id}_{generation}"] = (
                    context_manifest_path
                )
                coordinator_input = serialize_coordinator_payload(payload)
                provider_characters = provider_input_character_measure(
                    coordinator_input, decision_model_settings
                )
                if provider_characters > frozen_context_limit:
                    raise CoordinatorContextBudgetExhausted(
                        limit=frozen_context_limit,
                        required=provider_characters,
                        diagnostic="OPTIONAL_CONTEXT_PREFLIGHT_FAILED",
                    ) from exc
                if context_rebuilds >= 3:
                    # The smaller generation is durable and has not been submitted. A
                    # resume can retry it without ever replaying the rejected payload.
                    raise CoordinatorContextBudgetExhausted(
                        limit=frozen_context_limit,
                        required=provider_characters,
                        diagnostic="PROVIDER_CONTEXT_REJECTED_AFTER_COMPACTION",
                    ) from exc
        coordinator_request_key = tracker.request_key(
            instructions=coordinator_prompt,
            input_text=coordinator_input,
            settings=decision_model_settings,
            output_type=ResearchCoordinatorDecision,
        )
        decision = _validate_coordinator_decision(
            result.parsed,
            expected_decision=decision_id,
            expected_event_sequence=event_sequence,
            minimum_assignments=minimum_initial_assignments,
            maximum_new_assignments=maximum_new_assignments,
            initial=initial,
            known_assignment_ids={record.assignment.id for record in scheduler.assignments},
            completed_assignment_ids=completed_ids,
        )
        decision, proposed_scientific_phase_state = normalize_scientific_assignments(decision)
        decision = defer_consequential_action_for_omitted_evidence(
            decision,
            payload=payload,
        )
        activation_contract = payload.get("activation_context")
        graph_contract_version = (
            activation_contract.get("knowledge_graph_contract_version")
            if isinstance(activation_contract, dict)
            else None
        )
        if knowledge_graph is not None and graph_contract_version == 1:
            request_graph_memory = payload.get("knowledge_graph_memory")
            if not isinstance(request_graph_memory, dict):
                raise StageValidationError(
                    "Graph-integrated coordinator request lost its graph memory descriptor."
                )
            reviewed_revision = request_graph_memory.get("graph_revision")
            if (
                not isinstance(reviewed_revision, str)
                or reviewed_revision not in decision.rationale
            ):
                raise StageValidationError(
                    "Coordinator decision did not attest the exact graph revision reviewed in "
                    "its rationale."
                )
            assert graph_problem_id is not None
            try:
                knowledge_graph.validate_assignment_targets(
                    problem_id=graph_problem_id,
                    assignments=[
                        assignment.model_dump(mode="json") for assignment in decision.assignments
                    ],
                )
            except GraphValidationError as exc:
                raise StageValidationError(
                    f"Coordinator decision has invalid graph branch targets: {exc}"
                ) from exc
        scientific_stop_declined = bool(
            decision.stop_recommended and decision.stop_category == "scientific"
        )
        unverified_refutation_stop_declined = bool(
            decision.stop_recommended and decision.stop_category == "refuted"
        )
        coordinator_stop_declined = scientific_stop_declined or unverified_refutation_stop_declined
        directives = set(decision.retire_assignment_ids) | set(decision.redirect_assignment_ids)
        open_assignment_ids = {
            record.assignment.id
            for record in scheduler.assignments
            if record.status in {AssignmentLifecycle.QUEUED, AssignmentLifecycle.RUNNING}
        }
        non_open_directives = sorted(directives - open_assignment_ids)
        if non_open_directives:
            raise StageValidationError(
                "Coordinator directives may target only open assignments: "
                + ", ".join(non_open_directives)
            )
        open_after_directives = sum(
            record.status in {AssignmentLifecycle.QUEUED, AssignmentLifecycle.RUNNING}
            and record.assignment.id not in directives
            for record in scheduler.assignments
        )
        if open_after_directives + len(decision.assignments) > settings.maximum_pending_assignments:
            raise StageValidationError(
                "Coordinator decision would exceed the configured open-assignment ceiling."
            )

        refundable_directive_records = [
            record
            for record in scheduler.assignments
            if record.assignment.id in directives
            and record.status == AssignmentLifecycle.QUEUED
            and not record.launched
            and record.request_key is not None
        ]
        restore_headroom_assignment = bool(
            headroom_record is not None and headroom_record.assignment.id not in directives
        )
        if tracker.maximum_calls is not None:
            available_worker_calls = (
                tracker.maximum_calls - tracker.calls + len(refundable_directive_records)
            )
            required_worker_calls = len(decision.assignments) + int(restore_headroom_assignment)
            if required_worker_calls > available_worker_calls:
                raise StageValidationError(
                    "Coordinator must retire or redirect enough unlaunched assignments "
                    "to fund its requested replacements and restore borrowed headroom."
                )
        if decision.candidate_packaging_recommended:
            if candidate_report_set_attempted(decision.candidate_report_ids):
                raise StageValidationError(
                    "Coordinator requested already-audited unchanged reports: "
                    + ", ".join(canonical_candidate_report_set(decision.candidate_report_ids))
                )
        if (
            len(decision.requested_artifact_ids) > settings.maximum_coordinator_requested_artifacts
            or len(decision.requested_graph_node_ids)
            > settings.maximum_coordinator_requested_artifacts
        ):
            raise StageValidationError(
                "Coordinator requested more omitted evidence than the bounded retrieval limit."
            )
        known_artifact_ids = {
            f"worker-report:{record.assignment.id}"
            for record in scheduler.assignments
            if record.report_path is not None
        }
        unknown_artifacts = sorted(set(decision.requested_artifact_ids) - known_artifact_ids)
        if unknown_artifacts:
            raise StageValidationError(
                "Coordinator requested unknown artifact IDs: " + ", ".join(unknown_artifacts)
            )
        if knowledge_graph is None and decision.requested_graph_node_ids:
            raise StageValidationError("Coordinator requested graph nodes without an active graph.")
        if knowledge_graph is not None:
            for node_id in decision.requested_graph_node_ids:
                knowledge_graph.show(node_id)

        # Freeze the exact provider decision before mutating canonical assignment state.
        # A crash may leave this immutable file orphaned, but replay can only produce the
        # same response and complete the state transaction against it.
        decision_path = _atomic_write_immutable_json(
            decisions_dir / f"{decision.decision_id:08d}.json", decision
        )
        artifact_paths[f"coordinator_decision_{decision.decision_id}"] = decision_path

        for assignment_id in directives:
            record = scheduler.assignment_record(assignment_id)
            assert record is not None
            if record.status in {AssignmentLifecycle.QUEUED, AssignmentLifecycle.RUNNING}:
                release_unlaunched_worker_request(record)
                record.status = AssignmentLifecycle.RETIRED
        if restore_headroom_assignment:
            assert headroom_record is not None
            # This worker has never launched. A resumed run may have changed the
            # current worker policy (for example via ``--no-web-search``), so do
            # not resurrect the pre-interruption settings that were attached to
            # the reservation temporarily exchanged for coordinator headroom.
            headroom_record.request_settings = worker_model.model_copy(deep=True)
            reserve_worker_request(headroom_record)
        graph_tasks: dict[str, str] = {}
        graph_contexts: dict[str, object] = {}
        graph_revision: str | None = None
        if knowledge_graph is not None and decision.assignments:
            assert graph_problem_id is not None and run_id is not None
            replay_records = {
                item.assignment.id: item
                for item in (replay_scheduler.assignments if replay_scheduler is not None else [])
                if item.admitted_by_decision == decision.decision_id
            }
            if replay_records and all(
                assignment.id in replay_records for assignment in decision.assignments
            ):
                for assignment in decision.assignments:
                    replay_record = replay_records[assignment.id]
                    if (
                        replay_record.graph_task_id is None
                        or replay_record.graph_revision is None
                        or replay_record.graph_context is None
                    ):
                        raise StageValidationError(
                            "Archived graph assignment context is incomplete."
                        )
                    graph_tasks[assignment.id] = replay_record.graph_task_id
                    graph_contexts[assignment.id] = replay_record.graph_context
                    graph_revision = replay_record.graph_revision
            else:
                task_map, contexts, graph_revision = knowledge_graph.record_assignment_tasks(
                    problem_id=graph_problem_id,
                    run_id=run_id,
                    decision_id=decision.decision_id,
                    assignments=[item.model_dump(mode="json") for item in decision.assignments],
                    allow_legacy_default_targets=graph_contract_version != 1,
                )
                graph_tasks = task_map
                graph_contexts = {
                    assignment_id: context.model_dump(mode="json")
                    for assignment_id, context in contexts.items()
                }
        for assignment in decision.assignments:
            record = ResearchAssignmentState(
                assignment=assignment,
                admitted_by_decision=decision.decision_id,
                scientific_phase_epoch=proposed_scientific_phase_state.phase_epoch,
                exact_target_policy_version=1,
                worker_report_schema_version=2,
                graph_task_id=graph_tasks.get(assignment.id),
                graph_revision=graph_revision,
                graph_context=cast(
                    dict[str, object] | None,
                    graph_contexts.get(assignment.id),
                ),
                graph_contract_version=(1 if graph_contract_version == 1 else None),
            )
            reserve_worker_request(record)
            scheduler.assignments.append(record)
        scheduler.decisions.append(
            ResearchCoordinatorDecisionRecord(
                decision=decision,
                response_id=result.response_id,
                request_settings=decision_model_settings.model_copy(deep=True),
                request_path=request_path.relative_to(destination).as_posix(),
                request_sha256=sha256_file(request_path),
                request_key=coordinator_request_key,
                context_manifest_path=context_manifest_path.relative_to(destination).as_posix(),
                context_manifest_sha256=sha256_file(context_manifest_path),
            )
        )
        scheduler.requested_artifact_ids = list(dict.fromkeys(decision.requested_artifact_ids))
        scheduler.requested_graph_node_ids = list(dict.fromkeys(decision.requested_graph_node_ids))
        scheduler.coordinator_ack_event_sequence = event_sequence
        if decision.candidate_packaging_recommended:
            scheduler.pending_candidate_report_ids = list(
                dict.fromkeys(decision.candidate_report_ids)
            )
            scheduler.pending_candidate_source = "coordinator"
            scheduler.phase = SchedulerPhase.AUDITING
        scheduler.stop_reason = (
            decision.stop_reason
            if decision.stop_recommended and not coordinator_stop_declined
            else None
        )
        scheduler.stop_category = (
            decision.stop_category
            if decision.stop_recommended and not coordinator_stop_declined
            else None
        )
        if scheduler.deferred_candidate_report_ids and not scheduler.pending_candidate_report_ids:
            next_candidate = next(
                (
                    assignment_id
                    for assignment_id in scheduler.deferred_candidate_report_ids
                    if not candidate_report_set_attempted([assignment_id])
                ),
                None,
            )
            if next_candidate is not None:
                scheduler.pending_candidate_report_ids = [next_candidate]
                scheduler.pending_candidate_source = "worker"
                scheduler.phase = SchedulerPhase.AUDITING
        scheduler.pending_coordinator_request = None
        append_event(
            "coordinator_decision",
            decision_id=decision.decision_id,
            response_id=result.response_id,
            artifact=decision_path,
            detail=[decision.rationale],
        )
        if scientific_stop_declined:
            obligation = (
                "A scientific no-progress stop cannot replace the exact target. Continue with "
                "new or redirected assignments until exact acceptance or a real resource, "
                "verified-disproof, integrity, or security boundary is reached."
            )
            scheduler.repair_obligations = list(
                dict.fromkeys([*scheduler.repair_obligations, obligation])
            )
            append_event(
                "coordinator_scientific_stop_declined",
                decision_id=decision.decision_id,
                detail=[
                    decision.stop_reason or "Unspecified scientific no-progress stop.",
                    obligation,
                ],
            )
        if unverified_refutation_stop_declined:
            scheduler.repair_obligations = list(
                dict.fromkeys([*scheduler.repair_obligations, unverified_refutation_obligation])
            )
            append_event(
                "coordinator_unverified_refutation_stop_declined",
                decision_id=decision.decision_id,
                detail=[
                    decision.stop_reason or "Unspecified model-only refutation stop.",
                    unverified_refutation_obligation,
                ],
            )
        scientific_phase_state = proposed_scientific_phase_state
        write_scientific_phase_state(scientific_phase_path, scientific_phase_state)
        persist_research_index()
        return decision

    async def run_worker(
        record: ResearchAssignmentState,
    ) -> tuple[ResearchWorkerReport, str]:
        assignment = record.assignment
        scratch = computation_store.prepare_workspace(assignment.id)
        selected_worker_client = worker_client
        workspace_factory = getattr(worker_client, "for_workspace", None)
        if callable(workspace_factory):
            try:
                selected_worker_client = workspace_factory(
                    scratch.parent,
                    writable_paths=(scratch,),
                )
            except TypeError:
                # API and deterministic test adapters have no filesystem tool authority.
                selected_worker_client = worker_client
        worker_input = worker_input_for(record)
        if record.request_settings is None:
            raise StageValidationError(
                f"Worker assignment {assignment.id!r} has no frozen model settings."
            )
        expected_request_key = tracker.request_key(
            instructions=worker_prompt,
            input_text=worker_input,
            settings=record.request_settings,
            output_type=ResearchWorkerReport,
        )
        if record.request_key != expected_request_key:
            raise StageValidationError(
                f"Worker assignment {assignment.id!r} has inconsistent request metadata."
            )
        result = await generate_model(
            instructions=worker_prompt,
            input_text=worker_input,
            model_settings=record.request_settings,
            output_type=ResearchWorkerReport,
            selected_client=selected_worker_client,
        )
        if result.parsed.assignment_id != assignment.id:
            raise StageValidationError(
                f"Worker report {result.parsed.assignment_id!r} does not match "
                f"assignment {assignment.id!r}."
            )
        if record.graph_contract_version == 1:
            if not assignment.target_node_ids:
                raise StageValidationError(
                    f"Graph-scoped worker {assignment.id!r} has no stable branch target."
                )
            if not (result.parsed.mechanism or "").strip():
                raise StageValidationError(
                    f"Graph-scoped worker {assignment.id!r} did not identify its mechanism."
                )
            if result.parsed.status is WorkerStatus.REFUTED and not (
                result.parsed.counterexamples or (result.parsed.exact_gap or "").strip()
            ):
                raise StageValidationError(
                    f"Refuted branch {assignment.id!r} has no concrete obstruction or exact "
                    "failure statement."
                )
            if (
                result.parsed.status is WorkerStatus.CANDIDATE_COMPLETE
                and (result.parsed.exact_gap or "").strip()
            ):
                raise StageValidationError(
                    f"Candidate-complete branch {assignment.id!r} still declares an exact gap."
                )
        evidence_path = worker_evidence_dir / f"{assignment.id}.json"
        if evidence_path.is_file():
            evidence = load_research_worker_evidence_json(read_regular_text(evidence_path))
            if (
                evidence.assignment_id != assignment.id
                or evidence.response_id != result.response_id
            ):
                raise StageValidationError(
                    f"Frozen worker evidence does not match assignment {assignment.id!r}."
                )
            raw_report = evidence.raw_report
            parsed = evidence.normalized_report
            source_verification = evidence.source_verification
        else:
            raw_report = result.parsed.model_dump(mode="json")
            parsed = result.parsed.model_copy(deep=True)
            source_verification = await verify_source_ledger(
                parsed.sources,
                provider_identifiers=tool_metadata_source_identifiers(result.tool_metadata),
                verifier=source_verifier,
            )
            for source in parsed.sources:
                matched = set(source.identifiers).intersection(
                    source_verification.verified_identifiers
                )
                source.verified = bool(matched)
                source.verification_detail = (
                    "Independently verified: " + ", ".join(sorted(matched))
                    if matched
                    else "No identifier could be independently verified."
                )
                if not source.verified:
                    warning = f"Source {source.source_id} could not be independently verified."
                    for scientific_result in parsed.results:
                        scientific_result.assumptions = list(
                            dict.fromkeys([*scientific_result.assumptions, warning])
                        )
            evidence = ResearchWorkerEvidence(
                assignment_id=assignment.id,
                response_id=result.response_id,
                raw_report=raw_report,
                normalized_report=parsed,
                source_verification=source_verification,
            )
            _atomic_write_immutable_json(evidence_path, evidence)
        computation_evidence: WorkerComputationEvidence | None = None
        has_computation = any(
            item.kind is ScientificResultKind.COMPUTATION for item in parsed.results
        )
        if parsed.artifact_manifest or has_computation:
            collection = computation_store.collect(
                assignment.id,
                parsed.artifact_manifest,
            )
            replay = (
                await computation_store.replay(
                    assignment.id,
                    computation_backend,
                    isolation=replay_isolation,
                )
                if collection.trusted and computation_backend is not None
                else None
            )
            computation_evidence = WorkerComputationEvidence(
                assignment_id=assignment.id,
                collection=collection,
                replay=replay,
            )
            computation_path = _atomic_write_immutable_json(
                worker_computation_dir / f"{assignment.id}.json",
                computation_evidence,
            )
            computation_evidence_by_id[assignment.id] = computation_evidence
            artifact_paths[f"worker_{assignment.id}_computation"] = computation_path
        raw_report_path = _atomic_write_immutable_json(
            workers_dir / f"{assignment.id}.raw.json", raw_report
        )
        report_path = _atomic_write_immutable_json(workers_dir / f"{assignment.id}.json", parsed)
        source_path = _atomic_write_immutable_json(
            worker_sources_dir / f"{assignment.id}.json", source_verification
        )
        artifact_paths[f"worker_{assignment.id}"] = report_path
        artifact_paths[f"worker_{assignment.id}_raw"] = raw_report_path
        artifact_paths[f"worker_{assignment.id}_evidence"] = evidence_path
        artifact_paths[f"worker_{assignment.id}_sources"] = source_path
        return parsed, result.response_id

    def accept_worker_result(
        record: ResearchAssignmentState,
        report: ResearchWorkerReport,
        response_id: str,
    ) -> int:
        report_path = workers_dir / f"{record.assignment.id}.json"
        raw_report_path = workers_dir / f"{record.assignment.id}.raw.json"
        if (
            record.request_key is None
            or tracker.response_ids_by_call_key.get(record.request_key) != response_id
        ):
            raise StageValidationError(
                f"Worker response for {record.assignment.id!r} is not bound to its request."
            )
        # The report and its source-verification transaction were committed by
        # ``run_worker`` before this optional graph integration begins.
        graph_patch_record: Path | None = None
        graph_issue: BaseException | None = None
        graph_issue_obligations: list[str] = []
        if knowledge_graph is not None:
            assert graph_problem_id is not None and run_id is not None
            if record.graph_task_id is None:
                raise StageValidationError(
                    f"Worker assignment {record.assignment.id!r} has no graph task."
                )
            replay_patch = (
                replay_root / "graph-patches" / f"{record.assignment.id}.json"
                if replay_root is not None
                else None
            )
            if replay_patch is not None and replay_patch.is_file():
                replay_patch_payload = json.loads(read_regular_text(replay_patch))
                if not isinstance(replay_patch_payload, dict):
                    raise StageValidationError("Archived graph patch record is malformed.")
                graph_patch_record = _atomic_write_immutable_json(
                    graph_patches_dir / f"{record.assignment.id}.json",
                    replay_patch_payload,
                )
            else:
                try:
                    graph_merge = knowledge_graph.integrate_worker_report(
                        problem_id=graph_problem_id,
                        run_id=run_id,
                        assignment=record.assignment.model_dump(mode="json"),
                        task_id=record.graph_task_id,
                        report=report.model_dump(mode="json"),
                        proposed_patch=None,
                        source_artifact=(
                            f".matek/runs/{run_id}/research/workers/{record.assignment.id}.json"
                        ),
                        operation_id=f"worker-report:{run_id}:{record.assignment.id}",
                        computation_evidence=(
                            {
                                **computation_evidence_by_id[record.assignment.id].model_dump(
                                    mode="json"
                                ),
                                "source_artifact": (
                                    f".matek/runs/{run_id}/research/worker-computation/"
                                    f"{record.assignment.id}.json"
                                ),
                            }
                            if record.assignment.id in computation_evidence_by_id
                            else None
                        ),
                    )
                except BaseException as exc:
                    if classify_failure(exc) is FailureCategory.INTEGRITY:
                        raise
                    graph_issue = exc
                    graph_issue_obligations = [
                        "Retry graph integration from the frozen report without rerunning "
                        "the scientific worker."
                    ]
                    graph_merge = None
                if graph_merge is not None and graph_merge.issues and graph_issue is None:
                    graph_issue = StageValidationError("; ".join(graph_merge.issues))
                    graph_issue_obligations = [
                        "Retry deterministic graph admission from the frozen typed report; the "
                        "scientific report remains accepted."
                    ]
                graph_patch_record = _atomic_write_immutable_json(
                    graph_patches_dir / f"{record.assignment.id}.json",
                    {
                        "assignment_id": record.assignment.id,
                        "task_id": record.graph_task_id,
                        "admission_mode": "typed_scientific_report_v2",
                        "model_authored_patch": False,
                        "merge_result": (
                            graph_merge.model_dump(mode="json") if graph_merge is not None else None
                        ),
                        "warning": (
                            f"{type(graph_issue).__name__}: {redact_text(str(graph_issue))[:1000]}"
                            if graph_issue is not None
                            else None
                        ),
                    },
                )
            record.graph_patch_path = graph_patch_record.relative_to(destination).as_posix()
            record.graph_patch_sha256 = sha256_file(graph_patch_record)
            artifact_paths[f"worker_{record.assignment.id}_graph_patch"] = graph_patch_record
        record.status = AssignmentLifecycle.COMPLETED
        record.response_id = response_id
        record.worker_report_schema_version = 2
        record.raw_report_path = raw_report_path.relative_to(destination).as_posix()
        record.raw_report_sha256 = sha256_file(raw_report_path)
        record.report_path = report_path.relative_to(destination).as_posix()
        record.report_sha256 = sha256_file(report_path)
        computation_path = worker_computation_dir / f"{record.assignment.id}.json"
        if computation_path.is_file():
            record.computation_evidence_path = computation_path.relative_to(destination).as_posix()
            record.computation_evidence_sha256 = sha256_file(computation_path)
        reports_by_id[record.assignment.id] = report
        registry.update(record.assignment, report)
        if report.status == WorkerStatus.CANDIDATE_COMPLETE and scheduler.final_outcome is None:
            active_attempt_report_ids = set(
                scheduler.active_candidate_attempt.report_ids
                if scheduler.active_candidate_attempt is not None
                else []
            )
            is_unattempted = (
                not candidate_report_set_attempted([record.assignment.id])
                and record.assignment.id not in active_attempt_report_ids
            )
            if (
                is_unattempted
                and not scheduler.pending_candidate_report_ids
                and scheduler.active_candidate_attempt is None
            ):
                scheduler.pending_candidate_report_ids = [record.assignment.id]
                scheduler.pending_candidate_source = "worker"
                scheduler.phase = SchedulerPhase.AUDITING
            elif is_unattempted:
                scheduler.deferred_candidate_report_ids = list(
                    dict.fromkeys([*scheduler.deferred_candidate_report_ids, record.assignment.id])
                )
        event_sequence = scheduler.next_event_sequence
        record.completed_event_sequence = event_sequence
        published_sequence = append_event(
            "worker_report_accepted",
            assignment_id=record.assignment.id,
            response_id=response_id,
            artifact=report_path,
            related_artifacts=[
                raw_report_path,
                worker_evidence_dir / f"{record.assignment.id}.json",
                worker_sources_dir / f"{record.assignment.id}.json",
                *([computation_path] if computation_path.is_file() else []),
                *([graph_patch_record] if graph_patch_record is not None else []),
            ],
            detail=[report.status.value],
        )
        if published_sequence != event_sequence:
            raise StageValidationError("Research event cursor changed during report commit.")
        if graph_issue is not None:
            record_execution_issue(
                event_kind="graph_mutation_rejected",
                exc=graph_issue,
                category=FailureCategory.EVIDENCE,
                assignment_id=record.assignment.id,
                extra_obligations=graph_issue_obligations,
                include_default_obligations=False,
            )
        persist_research_index()
        return event_sequence

    def record_intermediate_gate_in_graph(
        record: ResearchAssignmentState,
        audit_record: IntermediateLemmaAuditRecord,
    ) -> None:
        """Finish or replay the graph side of a committed lemma-audit transaction."""

        if audit_record.graph_recorded:
            return
        if knowledge_graph is None or graph_problem_id is None or run_id is None:
            return
        nomination_path = resolved_artifact(audit_record.nomination_path)
        gate_path = resolved_artifact(audit_record.gate_path)
        nomination, gate = verify_persisted_lemma_audit(nomination_path, gate_path)
        target_versions = {
            item.obligation_id: item.logical_version
            for item in nomination.target_obligation_contracts
        }
        if (
            sha256_file(nomination_path) != audit_record.nomination_sha256
            or sha256_file(gate_path) != audit_record.gate_sha256
            or nomination.nomination_id != audit_record.nomination_id
            or nomination.origin_worker_id != record.assignment.id
            or nomination.current_graph_revision != audit_record.graph_revision
            or gate.audit_id != audit_record.nomination_id
            or gate.status is not audit_record.gate_status
            or (
                audit_record.target_obligation_ids
                and nomination.target_obligation_ids != audit_record.target_obligation_ids
            )
            or (
                audit_record.target_obligation_versions
                and target_versions != audit_record.target_obligation_versions
            )
        ):
            raise StageValidationError(
                "Intermediate lemma-audit record differs from its frozen nomination or gate."
            )
        if gate.missing_roles:
            raise StageValidationError(
                "An incomplete intermediate lemma audit cannot be recorded in the graph."
            )
        source_artifact = (
            f".matek/runs/{run_id}/research/" + gate_path.relative_to(destination).as_posix()
        )
        knowledge_graph.record_lemma_audit(
            problem_id=graph_problem_id,
            run_id=run_id,
            nomination=nomination.model_dump(mode="json"),
            gate=gate.model_dump(mode="json"),
            source_artifact=source_artifact,
        )
        audit_record.graph_recorded = True
        append_event(
            "intermediate_lemma_audit_recorded",
            assignment_id=record.assignment.id,
            artifact=gate_path,
            related_artifacts=[nomination_path],
            detail=[
                audit_record.nomination_id,
                audit_record.result_local_key,
                audit_record.gate_status.value,
            ],
        )
        persist_scheduler()

    async def upgrade_legacy_intermediate_gate(
        record: ResearchAssignmentState,
        audit_record: IntermediateLemmaAuditRecord,
    ) -> bool:
        """Archive a v1 audit and rerun both roles before any graph promotion attempt."""

        nomination_path = resolved_artifact(audit_record.nomination_path)
        gate_path = resolved_artifact(audit_record.gate_path)
        input_path = gate_path.parent / "input.json"
        try:
            input_payload = json.loads(read_regular_text(input_path))
            gate_payload = json.loads(read_regular_text(gate_path))
        except (OSError, ValueError) as exc:
            raise StageValidationError(
                f"Intermediate lemma-audit version evidence is invalid: {redact_text(str(exc))}"
            ) from exc
        if not isinstance(input_payload, dict) or not isinstance(gate_payload, dict):
            raise StageValidationError("Intermediate lemma-audit version evidence is malformed.")
        input_version = input_payload.get("schema_version")
        gate_version = gate_payload.get("schema_version")
        if input_version == 2 and gate_version == 2:
            return False
        if input_version != 1 or gate_version != 1:
            raise StageValidationError(
                "Intermediate lemma-audit input and gate schema versions are inconsistent."
            )
        nomination = LemmaNomination.model_validate_json(read_regular_text(nomination_path))
        if nomination.nomination_id != audit_record.nomination_id:
            raise StageValidationError(
                "Legacy intermediate lemma audit is bound to another nomination."
            )
        gate = await run_lemma_audit(
            nomination,
            gate_path.parent,
            verifier_client=tracked_role_client(f"lemma-verifier-{nomination.nomination_id[-24:]}"),
            falsifier_client=tracked_role_client(
                f"lemma-falsifier-{nomination.nomination_id[-24:]}"
            ),
            settings=auditor_model,
        )
        if gate.schema_version != 2 or not gate_path.is_file():
            raise StageValidationError(
                "Legacy intermediate lemma audit did not commit a fresh schema-v2 gate."
            )
        audit_record.gate_status = gate.status
        audit_record.gate_sha256 = sha256_file(gate_path)
        audit_record.graph_recorded = False
        artifact_paths[f"worker_{record.assignment.id}_lemma_audit_{nomination.nomination_id}"] = (
            gate_path
        )
        append_event(
            "intermediate_lemma_audit_upgraded_v2",
            assignment_id=record.assignment.id,
            artifact=immutable_gate_checkpoint(gate_path),
            related_artifacts=[
                nomination_path,
                gate_path.parent / "legacy-v1" / "manifest.json",
            ],
            detail=[nomination.nomination_id, gate.status.value],
        )
        persist_scheduler()
        return True

    async def reconcile_intermediate_gate(
        record: ResearchAssignmentState,
        audit_record: IntermediateLemmaAuditRecord,
    ) -> bool:
        """Resume one frozen audit and graph-record it only after both roles finish.

        ``False`` means at least one role remains unavailable. A complete model-authored
        ``BLOCKED`` verdict is terminal for this nomination and therefore returns ``True``
        after its graph audit node is recorded.
        """

        await upgrade_legacy_intermediate_gate(record, audit_record)
        nomination_path = resolved_artifact(audit_record.nomination_path)
        gate_path = resolved_artifact(audit_record.gate_path)
        nomination, persisted_gate = verify_persisted_lemma_audit(
            nomination_path,
            gate_path,
        )
        target_versions = {
            item.obligation_id: item.logical_version
            for item in nomination.target_obligation_contracts
        }
        if (
            sha256_file(nomination_path) != audit_record.nomination_sha256
            or sha256_file(gate_path) != audit_record.gate_sha256
            or nomination.nomination_id != audit_record.nomination_id
            or nomination.origin_worker_id != record.assignment.id
            or nomination.current_graph_revision != audit_record.graph_revision
            or persisted_gate.audit_id != audit_record.nomination_id
            or persisted_gate.status is not audit_record.gate_status
            or (
                audit_record.target_obligation_ids
                and nomination.target_obligation_ids != audit_record.target_obligation_ids
            )
            or (
                audit_record.target_obligation_versions
                and target_versions != audit_record.target_obligation_versions
            )
        ):
            raise StageValidationError(
                "Retryable intermediate lemma audit differs from its frozen nomination."
            )

        if persisted_gate.missing_roles:
            previous_obligations = set(persisted_gate.obligations)
            previous_gate_sha256 = sha256_file(gate_path)
            checkpoint_path = immutable_gate_checkpoint(gate_path)
            if (
                gate_checkpoint_event(
                    assignment_id=record.assignment.id,
                    gate_path=audit_record.gate_path,
                    gate_sha256=previous_gate_sha256,
                    nomination_path=audit_record.nomination_path,
                    nomination_sha256=audit_record.nomination_sha256,
                    kinds={
                        "intermediate_lemma_audit_incomplete",
                        "intermediate_lemma_audit_resumed",
                        "intermediate_lemma_audit_retry_checkpointed",
                    },
                )
                is None
            ):
                append_event(
                    "intermediate_lemma_audit_retry_checkpointed",
                    assignment_id=record.assignment.id,
                    artifact=checkpoint_path,
                    related_artifacts=[nomination_path],
                    detail=[
                        nomination.nomination_id,
                        persisted_gate.status.value,
                        *(role.value for role in persisted_gate.missing_roles),
                        *persisted_gate.obligations,
                    ],
                )
            gate = await run_lemma_audit(
                nomination,
                gate_path.parent,
                verifier_client=tracked_role_client(
                    f"lemma-verifier-{nomination.nomination_id[-24:]}"
                ),
                falsifier_client=tracked_role_client(
                    f"lemma-falsifier-{nomination.nomination_id[-24:]}"
                ),
                settings=auditor_model,
            )
            committed_nomination, committed_gate = verify_persisted_lemma_audit(
                nomination_path,
                gate_path,
            )
            if committed_nomination != nomination or committed_gate != gate:
                raise StageValidationError(
                    "Resumed intermediate lemma audit did not commit its returned gate."
                )
            audit_record.gate_status = gate.status
            audit_record.gate_sha256 = sha256_file(gate_path)
            audit_record.graph_recorded = False
            artifact_paths[
                f"worker_{record.assignment.id}_lemma_audit_{nomination.nomination_id}"
            ] = gate_path
            scheduler.repair_obligations = list(
                dict.fromkeys(
                    [
                        *(
                            item
                            for item in scheduler.repair_obligations
                            if item not in previous_obligations
                        ),
                        *gate.obligations,
                    ]
                )
            )
            append_event(
                "intermediate_lemma_audit_resumed",
                assignment_id=record.assignment.id,
                artifact=immutable_gate_checkpoint(gate_path),
                related_artifacts=[nomination_path],
                detail=[
                    nomination.nomination_id,
                    gate.status.value,
                    *(role.value for role in gate.missing_roles),
                    *gate.obligations,
                ],
            )
            persist_scheduler()
            persisted_gate = gate

        if persisted_gate.missing_roles:
            return False
        record_intermediate_gate_in_graph(record, audit_record)
        return True

    def promote_exact_counterexample(
        record: ResearchAssignmentState,
        audit_record: ExactCounterexampleAuditRecord,
        nomination: ExactCounterexampleNomination,
        gate: CounterexampleAuditGate,
    ) -> None:
        """Commit the graph edge, pass event, and terminal scheduler state in order."""

        if gate.status is not CounterexampleAuditGateStatus.REFUTATION_VERIFIED:
            return
        gate_path = resolved_artifact(audit_record.gate_path)
        nomination_path = resolved_artifact(audit_record.nomination_path)
        if knowledge_graph is not None and not audit_record.graph_recorded:
            assert graph_problem_id is not None and run_id is not None
            source_artifact = (
                f".matek/runs/{run_id}/research/" + gate_path.relative_to(destination).as_posix()
            )
            knowledge_graph.record_counterexample_audit(
                problem_id=graph_problem_id,
                run_id=run_id,
                assignment_id=record.assignment.id,
                result_local_key=audit_record.result_local_key,
                nomination=nomination.model_dump(mode="json"),
                gate=gate.model_dump(mode="json"),
                source_artifact=source_artifact,
            )
            audit_record.graph_recorded = True
        elif knowledge_graph is None:
            audit_record.graph_recorded = True

        scheduler.stop_reason = None
        scheduler.stop_category = None
        scheduler.repair_obligations = []
        scheduler.final_outcome = ResearchOutcome.REJECTED
        scheduler.final_obligations = []
        scheduler.final_strongest_result = nomination.proof_or_certificate
        scheduler.final_acceptance_gate = None
        scheduler.final_refutation_gate = gate.model_dump(mode="json")
        scheduler.final_refutation_audit_id = gate.audit_id
        scheduler.phase = SchedulerPhase.COMPLETE
        existing_pass = any(
            json.loads(read_regular_text(path)).get("kind") == "main_counterexample_audit_passed"
            and json.loads(read_regular_text(path)).get("artifact")
            == gate_path.relative_to(destination).as_posix()
            for path in sorted(events_dir.glob("*.json"))
        )
        if not existing_pass:
            audit_root = gate_path.parent
            related = [path for path in sorted(audit_root.rglob("*.json")) if path != gate_path]
            append_event(
                "main_counterexample_audit_passed",
                assignment_id=record.assignment.id,
                artifact=gate_path,
                related_artifacts=related,
                detail=[gate.audit_id, audit_record.result_local_key],
            )
        artifact_paths[f"counterexample_audit_{gate.audit_id}"] = gate_path
        artifact_paths[f"counterexample_nomination_{gate.audit_id}"] = nomination_path
        persist_scheduler()

    def eligible_exact_counterexamples(
        report: ResearchWorkerReport,
    ) -> list[ScientificResult]:
        """Return typed, exact-main refutations that require independent arbitration."""

        return [
            result
            for result in report.results
            if result.kind is ScientificResultKind.COUNTEREXAMPLE
            and result.scope is ScientificScope.MAIN
            and result.disposition is ScientificResultDisposition.REFUTED_MECHANISM
            and result.exact_gap is None
            and normalize_exact_statement(result.exact_statement)
            == normalize_exact_statement(compiled.normalized_statement)
        ]

    async def audit_admitted_exact_counterexamples(
        record: ResearchAssignmentState,
        report: ResearchWorkerReport,
    ) -> bool:
        """Audit eligible main-scope counterexamples; branch evidence never enters."""

        if record.report_path is None or record.report_sha256 is None:
            raise StageValidationError("Counterexample audit requires a frozen worker report.")
        eligible = eligible_exact_counterexamples(report)
        audited = {
            item.result_local_key: item
            for item in record.exact_counterexample_audits
            if not item.superseded
        }
        did_work = False
        for result in eligible:
            existing = audited.get(result.local_key)
            if (
                existing is not None
                and existing.gate_status is CounterexampleAuditGateStatus.AUDIT_FAILED
            ):
                continue
            persisted_retry: (
                tuple[ExactCounterexampleNomination, CounterexampleAuditGate] | None
            ) = None
            if (
                existing is not None
                and existing.gate_status is CounterexampleAuditGateStatus.BLOCKED
            ):
                nomination_path = resolved_artifact(existing.nomination_path)
                gate_path = resolved_artifact(existing.gate_path)
                try:
                    persisted_retry = verify_persisted_counterexample_audit(
                        nomination_path,
                        gate_path,
                        expected_target_statement=compiled.normalized_statement,
                    )
                except CounterexampleSupportInvalidated as exc:
                    _, archived_gate = verify_persisted_counterexample_audit(
                        nomination_path,
                        gate_path,
                        expected_target_statement=compiled.normalized_statement,
                        allow_invalidated_graph_support=True,
                    )
                    detail = redact_text(str(exc)).replace("\n", " ").strip()[:1000]
                    reason = "Frozen canonical support changed after the retryable audit" + (
                        f": {detail}" if detail else "."
                    )
                    existing.superseded = True
                    existing.superseded_reason = reason
                    scheduler.repair_obligations = [
                        item
                        for item in scheduler.repair_obligations
                        if item not in archived_gate.obligations
                    ]
                    append_event(
                        "main_counterexample_audit_support_superseded",
                        assignment_id=record.assignment.id,
                        artifact=gate_path,
                        related_artifacts=[nomination_path],
                        detail=[existing.audit_id, result.local_key, reason],
                    )
                    persist_scheduler()
                    existing = None
            if (
                existing is not None
                and existing.gate_status is CounterexampleAuditGateStatus.BLOCKED
            ):
                assert persisted_retry is not None
                nomination, persisted_gate = persisted_retry
                if not persisted_gate.missing_roles:
                    continue
                previous_obligations = set(persisted_gate.obligations)
                if (
                    persisted_gate.status is not CounterexampleAuditGateStatus.BLOCKED
                    or nomination.assignment_id != record.assignment.id
                    or nomination.result_local_key != result.local_key
                    or nomination.audit_id != existing.audit_id
                ):
                    raise StageValidationError(
                        "Retryable counterexample audit record differs from its frozen nomination."
                    )
                previous_gate_sha256 = sha256_file(gate_path)
                checkpoint_path = immutable_gate_checkpoint(gate_path)
                if (
                    gate_checkpoint_event(
                        assignment_id=record.assignment.id,
                        gate_path=existing.gate_path,
                        gate_sha256=previous_gate_sha256,
                        nomination_path=existing.nomination_path,
                        nomination_sha256=existing.nomination_sha256,
                        kinds={
                            "main_counterexample_audit_not_verified",
                            "main_counterexample_audit_retry_checkpointed",
                        },
                    )
                    is None
                ):
                    append_event(
                        "main_counterexample_audit_retry_checkpointed",
                        assignment_id=record.assignment.id,
                        artifact=checkpoint_path,
                        related_artifacts=[nomination_path],
                        detail=[
                            nomination.audit_id,
                            persisted_gate.status.value,
                            *persisted_gate.obligations,
                        ],
                    )
                gate = await run_counterexample_audit(
                    nomination,
                    gate_path.parent,
                    verifier_client=tracked_role_client(
                        f"counterexample-verifier-{nomination.audit_id[-24:]}"
                    ),
                    falsifier_client=tracked_role_client(
                        f"counterexample-falsifier-{nomination.audit_id[-24:]}"
                    ),
                    settings=auditor_model,
                )
                existing.gate_status = gate.status
                existing.gate_sha256 = sha256_file(gate_path)
                did_work = True
                persist_scheduler()
                if gate.status is CounterexampleAuditGateStatus.REFUTATION_VERIFIED:
                    promote_exact_counterexample(record, existing, nomination, gate)
                    return True
                scheduler.repair_obligations = list(
                    dict.fromkeys(
                        [
                            *(
                                item
                                for item in scheduler.repair_obligations
                                if item not in previous_obligations
                            ),
                            *gate.obligations,
                        ]
                    )
                )
                append_event(
                    "main_counterexample_audit_not_verified",
                    assignment_id=record.assignment.id,
                    artifact=immutable_gate_checkpoint(gate_path),
                    related_artifacts=[nomination_path],
                    detail=[gate.audit_id, gate.status.value, *gate.obligations],
                )
                persist_scheduler()
                continue
            graph_support_revision = (
                knowledge_graph.load_state().revision if knowledge_graph is not None else "no-graph"
            )
            support_context = sha256_text(
                "\0".join(
                    [
                        record.report_sha256 or "missing-report",
                        record.computation_evidence_sha256 or "no-computation-evidence",
                        graph_support_revision,
                    ]
                )
            )
            rejection_prefix = f"[support-context:{support_context}] "
            prior_rejection = record.counterexample_support_rejections.get(result.local_key)
            if prior_rejection is not None and prior_rejection.startswith(rejection_prefix):
                continue
            try:
                computation_evidence_path = (
                    resolved_artifact(record.computation_evidence_path)
                    if record.computation_evidence_path is not None
                    else None
                )
                support_bundle = build_counterexample_support_bundle(
                    assignment_id=record.assignment.id,
                    root_result=result,
                    results=report.results,
                    unresolved_obligations=report.unresolved_obligations,
                    artifact_manifest=report.artifact_manifest,
                    run_root=destination.parent,
                    computation_evidence_path=computation_evidence_path,
                    knowledge_graph=knowledge_graph,
                    graph_problem_id=graph_problem_id,
                    run_id=run_id,
                )
            except StageValidationError as exc:
                detail = redact_text(str(exc)).replace("\n", " ").strip()[:1000]
                obligation = (
                    f"Exact counterexample {record.assignment.id!r}/{result.local_key!r} has "
                    f"untrusted support: {detail}"
                )
                record.counterexample_support_rejections[result.local_key] = (
                    rejection_prefix + obligation
                )
                scheduler.repair_obligations = list(
                    dict.fromkeys([*scheduler.repair_obligations, obligation])
                )
                append_event(
                    "main_counterexample_support_rejected",
                    assignment_id=record.assignment.id,
                    detail=[result.local_key, obligation],
                )
                persist_scheduler()
                did_work = True
                continue
            cleared_rejection = record.counterexample_support_rejections.pop(result.local_key, None)
            if cleared_rejection is not None:
                cleared_obligation = (
                    cleared_rejection.split("] ", maxsplit=1)[1]
                    if cleared_rejection.startswith("[support-context:")
                    and "] " in cleared_rejection
                    else cleared_rejection
                )
                scheduler.repair_obligations = [
                    item for item in scheduler.repair_obligations if item != cleared_obligation
                ]
            nomination = build_exact_counterexample_nomination(
                assignment_id=record.assignment.id,
                result=result,
                frozen_target_statement=compiled.normalized_statement,
                worker_report_path=record.report_path,
                worker_report_sha256=record.report_sha256,
                main_target_node_id=(
                    knowledge_graph.main_claim_id(graph_problem_id)
                    if knowledge_graph is not None and graph_problem_id is not None
                    else None
                ),
                support_bundle=support_bundle,
            )
            audit_dir = ensure_stage_directory(counterexample_audits_dir / nomination.audit_id)
            gate = await run_counterexample_audit(
                nomination,
                audit_dir,
                verifier_client=tracked_role_client(
                    f"counterexample-verifier-{nomination.audit_id[-24:]}"
                ),
                falsifier_client=tracked_role_client(
                    f"counterexample-falsifier-{nomination.audit_id[-24:]}"
                ),
                settings=auditor_model,
            )
            nomination_path = audit_dir / "nomination.json"
            gate_path = audit_dir / "gate.json"
            if not nomination_path.is_file() or not gate_path.is_file():
                raise StageValidationError(
                    f"Counterexample audit {nomination.audit_id!r} did not commit its evidence."
                )
            if existing is None:
                existing = ExactCounterexampleAuditRecord(
                    result_local_key=result.local_key,
                    audit_id=nomination.audit_id,
                    gate_status=gate.status,
                    nomination_path=nomination_path.relative_to(destination).as_posix(),
                    nomination_sha256=sha256_file(nomination_path),
                    gate_path=gate_path.relative_to(destination).as_posix(),
                    gate_sha256=sha256_file(gate_path),
                    graph_recorded=knowledge_graph is None,
                )
                record.exact_counterexample_audits.append(existing)
            else:
                if existing.audit_id != nomination.audit_id:
                    raise StageValidationError(
                        "Counterexample result changed audit identity after checkpoint."
                    )
                existing.gate_status = gate.status
                existing.gate_sha256 = sha256_file(gate_path)
            did_work = True
            persist_scheduler()
            if gate.status is CounterexampleAuditGateStatus.REFUTATION_VERIFIED:
                promote_exact_counterexample(record, existing, nomination, gate)
                return True
            scheduler.repair_obligations = list(
                dict.fromkeys([*scheduler.repair_obligations, *gate.obligations])
            )
            append_event(
                "main_counterexample_audit_not_verified",
                assignment_id=record.assignment.id,
                artifact=immutable_gate_checkpoint(gate_path),
                related_artifacts=[nomination_path],
                detail=[gate.audit_id, gate.status.value, *gate.obligations],
            )
            persist_scheduler()
        return did_work

    def exact_counterexample_audit_pending() -> bool:
        for assignment_record in scheduler.assignments:
            for audit_record in assignment_record.exact_counterexample_audits:
                if (
                    audit_record.superseded
                    or audit_record.gate_status is not CounterexampleAuditGateStatus.BLOCKED
                ):
                    continue
                try:
                    _, gate = verify_persisted_counterexample_audit(
                        resolved_artifact(audit_record.nomination_path),
                        resolved_artifact(audit_record.gate_path),
                        expected_target_statement=compiled.normalized_statement,
                    )
                except CounterexampleSupportInvalidated:
                    continue
                if gate.missing_roles:
                    return True
        return False

    def unresolved_exact_counterexample_obligations() -> list[str]:
        """Return blockers that must be arbitrated before proof acceptance."""

        obligations: list[str] = []
        for assignment_record in scheduler.assignments:
            report = reports_by_id.get(assignment_record.assignment.id)
            current_audits = {
                item.result_local_key: item
                for item in assignment_record.exact_counterexample_audits
                if not item.superseded
            }
            if report is not None:
                for result in eligible_exact_counterexamples(report):
                    if (
                        result.local_key not in current_audits
                        and result.local_key
                        not in assignment_record.counterexample_support_rejections
                    ):
                        obligations.append(
                            "Exact-main counterexample "
                            f"{assignment_record.assignment.id!r}/{result.local_key!r} "
                            "was checkpointed before its independent audit nomination."
                        )
            for audit_record in assignment_record.exact_counterexample_audits:
                if (
                    audit_record.superseded
                    or audit_record.gate_status is not CounterexampleAuditGateStatus.BLOCKED
                ):
                    continue
                try:
                    _, gate = verify_persisted_counterexample_audit(
                        resolved_artifact(audit_record.nomination_path),
                        resolved_artifact(audit_record.gate_path),
                        expected_target_statement=compiled.normalized_statement,
                    )
                except CounterexampleSupportInvalidated:
                    continue
                else:
                    if gate.missing_roles:
                        obligations.extend(gate.obligations)
        return list(dict.fromkeys(obligations))

    async def audit_all_admitted_exact_counterexamples() -> bool:
        """Audit or resume every durable exact-main counterexample before candidates."""

        did_work = False
        for assignment_record in scheduler.assignments:
            report = reports_by_id.get(assignment_record.assignment.id)
            if report is None or not eligible_exact_counterexamples(report):
                continue
            if await audit_admitted_exact_counterexamples(assignment_record, report):
                did_work = True
            if scheduler.final_outcome is ResearchOutcome.REJECTED:
                break
        return did_work

    async def counterexample_priority_result() -> ResearchResult | None:
        """Return the only safe terminal/pause result before candidate acceptance."""

        if (
            scheduler.final_outcome is ResearchOutcome.REJECTED
            and scheduler.final_refutation_gate is not None
        ):
            return await finish(
                ResearchOutcome.REJECTED,
                refutation_gate=CounterexampleAuditGate.model_validate(
                    scheduler.final_refutation_gate
                ),
            )
        obligations = unresolved_exact_counterexample_obligations()
        if obligations:
            return await pause_retriable(
                obligations=obligations,
                pause_reason="COUNTEREXAMPLE_AUDIT_INCOMPLETE",
                resume_action=(
                    "Run `matek resume` to retry the unresolved independent exact-"
                    "counterexample audit before any proof candidate is accepted."
                ),
            )
        return None

    async def audit_admitted_intermediate(
        record: ResearchAssignmentState,
        report: ResearchWorkerReport,
    ) -> bool:
        """Nominate and independently audit the highest-leverage eligible result."""

        if knowledge_graph is None or graph_problem_id is None or run_id is None:
            return False
        for audit_record in record.intermediate_lemma_audits:
            if not audit_record.graph_recorded:
                try:
                    if not await reconcile_intermediate_gate(record, audit_record):
                        return True
                except BaseException as exc:
                    if classify_failure(exc) is FailureCategory.INTEGRITY:
                        raise
                    record_execution_issue(
                        event_kind="intermediate_lemma_graph_record_failed",
                        exc=exc,
                        category=FailureCategory.EVIDENCE,
                        assignment_id=record.assignment.id,
                        extra_obligations=[
                            "Replay graph promotion from the frozen intermediate lemma gate."
                        ],
                        include_default_obligations=False,
                    )
                    persist_scheduler()
                    return True

        if (
            report.status is WorkerStatus.CANDIDATE_COMPLETE
            or scheduler.pending_candidate_report_ids
            or scheduler.active_candidate_attempt is not None
        ):
            return False

        frontier = knowledge_graph.frontier(graph_problem_id)
        selection = nominate_intermediate_lemmas(
            report,
            graph_nodes=knowledge_graph.load_nodes(),
            frontier=frontier,
        )
        selection_digest = sha256_text(f"{record.assignment.id}\0{selection.graph_revision}")[:20]
        selection_path = _atomic_write_immutable_json(
            lemma_selections_dir / f"{record.assignment.id}-{selection_digest}.json",
            selection,
        )
        artifact_paths[f"worker_{record.assignment.id}_lemma_selection_{selection_digest}"] = (
            selection_path
        )
        audited_keys = {item.result_local_key for item in record.intermediate_lemma_audits}
        binding_by_nomination = {item.nomination_id: item for item in selection.bindings}
        nomination = next(
            (
                item
                for item in selection.nominations
                if binding_by_nomination[item.nomination_id].result_local_key not in audited_keys
            ),
            None,
        )
        if nomination is None:
            return False
        binding = binding_by_nomination[nomination.nomination_id]
        audit_dir = ensure_stage_directory(lemma_audits_dir / nomination.nomination_id)
        nomination_path = _atomic_write_immutable_json(
            audit_dir / "nomination.json",
            nomination,
        )
        gate = await run_lemma_audit(
            nomination,
            audit_dir,
            verifier_client=tracked_role_client(f"lemma-verifier-{nomination.nomination_id[-24:]}"),
            falsifier_client=tracked_role_client(
                f"lemma-falsifier-{nomination.nomination_id[-24:]}"
            ),
            settings=auditor_model,
        )
        gate_path = audit_dir / "gate.json"
        if not gate_path.is_file():
            raise StageValidationError(
                f"Lemma audit {nomination.nomination_id!r} did not commit its gate."
            )
        audit_record = IntermediateLemmaAuditRecord(
            result_local_key=binding.result_local_key,
            nomination_id=nomination.nomination_id,
            graph_revision=selection.graph_revision,
            target_obligation_ids=nomination.target_obligation_ids,
            target_obligation_versions={
                item.obligation_id: item.logical_version
                for item in nomination.target_obligation_contracts
            },
            gate_status=gate.status,
            nomination_path=nomination_path.relative_to(destination).as_posix(),
            nomination_sha256=sha256_file(nomination_path),
            gate_path=gate_path.relative_to(destination).as_posix(),
            gate_sha256=sha256_file(gate_path),
        )
        record.intermediate_lemma_audits.append(audit_record)
        artifact_paths[f"worker_{record.assignment.id}_lemma_audit_{nomination.nomination_id}"] = (
            gate_path
        )
        # Checkpoint the immutable gate before the separate graph commit. A crash here
        # resumes at the graph boundary without paying for either auditor again.
        persist_scheduler()
        if gate.missing_roles:
            scheduler.repair_obligations = list(
                dict.fromkeys([*scheduler.repair_obligations, *gate.obligations])
            )
            append_event(
                "intermediate_lemma_audit_incomplete",
                assignment_id=record.assignment.id,
                artifact=immutable_gate_checkpoint(gate_path),
                related_artifacts=[nomination_path],
                detail=[
                    nomination.nomination_id,
                    *(role.value for role in gate.missing_roles),
                    *gate.obligations,
                ],
            )
            persist_scheduler()
            return True
        try:
            record_intermediate_gate_in_graph(record, audit_record)
        except BaseException as exc:
            if classify_failure(exc) is FailureCategory.INTEGRITY:
                raise
            record_execution_issue(
                event_kind="intermediate_lemma_graph_record_failed",
                exc=exc,
                category=FailureCategory.EVIDENCE,
                assignment_id=record.assignment.id,
                extra_obligations=[
                    "Replay graph promotion from the frozen intermediate lemma gate."
                ],
                include_default_obligations=False,
            )
            persist_scheduler()
        return True

    def intermediate_lemma_audit_pending() -> bool:
        """Report only execution-incomplete audits, not terminal blocked verdicts."""

        for assignment_record in scheduler.assignments:
            for audit_record in assignment_record.intermediate_lemma_audits:
                if audit_record.graph_recorded:
                    continue
                _, gate = verify_persisted_lemma_audit(
                    resolved_artifact(audit_record.nomination_path),
                    resolved_artifact(audit_record.gate_path),
                )
                if gate.missing_roles:
                    return True
        return False

    async def reconcile_pending_intermediate_audits() -> bool:
        """Reconcile every frozen gate, returning whether roles still remain missing."""

        incomplete = False
        for assignment_record in scheduler.assignments:
            for audit_record in assignment_record.intermediate_lemma_audits:
                if not audit_record.graph_recorded and not await reconcile_intermediate_gate(
                    assignment_record,
                    audit_record,
                ):
                    incomplete = True
        return incomplete

    async def evaluate_candidate(
        report_ids: list[str],
        *,
        attempt_name: str,
    ) -> tuple[
        ResearchAcceptanceGate | None,
        list[str],
        FinalJudgeDecision | None,
        Literal["scientific", "budget", "execution", "evidence"] | None,
    ]:
        nonlocal current_candidate, current_audits, current_verdict, final_judge_response_id
        attempt = scheduler.active_candidate_attempt
        if attempt is None or attempt.attempt_name != attempt_name:
            raise StageValidationError("Candidate evaluation has no matching frozen attempt.")
        package_input_path = resolved_artifact(attempt.package_input_path)
        if (
            not package_input_path.is_file()
            or sha256_file(package_input_path) != attempt.package_input_sha256
        ):
            raise StageValidationError("Frozen candidate package input is missing or changed.")
        package_input = read_regular_text(package_input_path)
        archived_replay = archived_candidate_attempt_for_replay(
            attempt_name=attempt_name,
            report_ids=report_ids,
            source=attempt.source,
        )
        if archived_replay is None:
            verified_computation_bindings, computation_obligations = (
                verify_candidate_computation_bindings(report_ids)
            )
            verified_graph_support, graph_support_obligations = (
                verify_candidate_graph_support_bindings(report_ids)
            )
        else:
            archived_attempt, archived_package_payload = archived_replay
            if json.loads(package_input) != archived_package_payload:
                raise StageValidationError(
                    "Frozen candidate package input differs from its archived replay."
                )
            # The prior gate may already have promoted these nodes in the persistent graph.
            # Rechecking against that post-gate state would make an otherwise identical replay
            # appear to have different support.  The immutable archived input is the correct
            # pre-gate boundary; material target changes cannot enter this replay path because
            # the scheduler is separately bound to the compiled-problem digest.
            verified_computation_bindings = archived_attempt.computation_bindings
            computation_obligations = archived_attempt.computation_obligations
            verified_graph_support = archived_attempt.graph_support_bindings
            graph_support_obligations = archived_attempt.graph_support_obligations
        if attempt.computation_gate_version != 1:
            if verified_computation_bindings or computation_obligations:
                raise StageValidationError(
                    "Candidate attempt with computation predates the deterministic computation "
                    "gate and must be regenerated."
                )
        elif (
            attempt.computation_bindings != verified_computation_bindings
            or attempt.computation_obligations != computation_obligations
        ):
            raise StageValidationError(
                "Frozen candidate computation bindings changed before package audit."
            )
        if attempt.graph_support_gate_version != 1:
            if verified_graph_support or graph_support_obligations:
                raise StageValidationError(
                    "Graph-integrated candidate predates the deterministic canonical-support "
                    "gate and must be regenerated."
                )
        elif (
            attempt.graph_support_bindings != verified_graph_support
            or attempt.graph_support_obligations != graph_support_obligations
        ):
            raise StageValidationError(
                "Frozen candidate canonical graph support changed before package audit."
            )
        deterministic_gate_obligations = list(
            dict.fromkeys(
                [
                    *computation_obligations,
                    *graph_support_obligations,
                    *unresolved_exact_counterexample_obligations(),
                ]
            )
        )
        if deterministic_gate_obligations:
            return None, deterministic_gate_obligations, None, "evidence"
        if attempt.package_sha256 is None and not tracker.has_request(
            instructions=packager_prompt,
            input_text=package_input,
            settings=attempt.packager_settings,
            output_type=CandidateProofPackage,
        ):
            # Candidate-attempt state is written before its first model request.
            # Apply the resumed policy only while the packager is provably
            # unregistered; a registered request retains exact replay settings.
            if attempt.packager_settings != worker_model:
                attempt.packager_settings = worker_model.model_copy(deep=True)
                persist_scheduler()
        progress(
            Ascension.AUDIT_RESEARCH,
            "Packaging the candidate solution for independent audits.",
        )
        attempt_dir = ensure_stage_directory(candidate_dir / "attempts" / attempt_name)
        package_path = attempt_dir / "package.json"
        package_proof_path = attempt_dir / "proof.md"
        imported_sources_path = attempt_dir / "source_verification.json"
        package_evidence_path = attempt_dir / "evidence.json"
        if attempt.package_sha256 is not None:
            if (
                attempt.package_evidence_sha256 is None
                or attempt.source_verification_sha256 is None
                or not attempt.packager_response_id
            ):
                raise StageValidationError(
                    "Committed candidate package has incomplete evidence metadata."
                )
            if (
                not package_evidence_path.is_file()
                or sha256_file(package_evidence_path) != attempt.package_evidence_sha256
                or not package_path.is_file()
                or sha256_file(package_path) != attempt.package_sha256
                or not imported_sources_path.is_file()
                or sha256_file(imported_sources_path) != attempt.source_verification_sha256
            ):
                raise StageValidationError(
                    "Committed candidate package evidence is missing or changed."
                )
            package_evidence = CandidatePackageEvidence.model_validate_json(
                read_regular_text(package_evidence_path)
            )
            current_candidate = CandidateProofPackage.model_validate_json(
                read_regular_text(package_path)
            )
            imported_source_verification = SourceVerificationReport.model_validate_json(
                read_regular_text(imported_sources_path)
            )
            package_call_key = tracker.request_key(
                instructions=packager_prompt,
                input_text=package_input,
                settings=attempt.packager_settings,
                output_type=CandidateProofPackage,
            )
            if (
                package_evidence.response_id != attempt.packager_response_id
                or package_evidence.candidate != current_candidate
                or package_evidence.source_verification != imported_source_verification
                or tracker.response_ids_by_call_key.get(package_call_key)
                != attempt.packager_response_id
            ):
                raise StageValidationError(
                    "Committed candidate package transaction is inconsistent."
                )
            _atomic_write_immutable_text(package_proof_path, current_candidate.full_proof)
        else:
            if not tracker.can_generate(
                instructions=packager_prompt,
                input_text=package_input,
                settings=attempt.packager_settings,
                output_type=CandidateProofPackage,
            ):
                return (
                    None,
                    ["Budget exhausted before candidate proof packaging."],
                    None,
                    "budget",
                )
            package_result = await generate_model(
                instructions=packager_prompt,
                input_text=package_input,
                model_settings=attempt.packager_settings,
                output_type=CandidateProofPackage,
                selected_client=packager_client,
            )
            if package_evidence_path.is_file():
                package_evidence = CandidatePackageEvidence.model_validate_json(
                    read_regular_text(package_evidence_path)
                )
                if package_evidence.response_id != package_result.response_id:
                    raise StageValidationError(
                        "Frozen candidate evidence has a different packager response."
                    )
                current_candidate = package_evidence.candidate
                imported_source_verification = package_evidence.source_verification
            else:
                current_candidate = package_result.parsed
                imported_source_verification = await verify_source_ledger(
                    [theorem.as_source_entry() for theorem in current_candidate.imported_theorems],
                    provider_identifiers=tool_metadata_source_identifiers(
                        package_result.tool_metadata
                    ),
                    verifier=source_verifier,
                )
                for theorem in current_candidate.imported_theorems:
                    theorem.verified = bool(
                        set(theorem.identifiers).intersection(
                            imported_source_verification.verified_identifiers
                        )
                    )
                    if not theorem.verified:
                        current_candidate.unresolved_items.append(
                            f"Imported theorem {theorem.name!r} is not independently verified."
                        )
                current_candidate.unresolved_items = list(
                    dict.fromkeys(current_candidate.unresolved_items)
                )
                package_evidence = CandidatePackageEvidence(
                    response_id=package_result.response_id,
                    candidate=current_candidate,
                    source_verification=imported_source_verification,
                )
                _atomic_write_immutable_json(package_evidence_path, package_evidence)
            _atomic_write_immutable_json(package_path, current_candidate)
            _atomic_write_immutable_text(package_proof_path, current_candidate.full_proof)
            _atomic_write_immutable_json(imported_sources_path, imported_source_verification)
            attempt.package_evidence_sha256 = sha256_file(package_evidence_path)
            attempt.package_sha256 = sha256_file(package_path)
            attempt.source_verification_sha256 = sha256_file(imported_sources_path)
            attempt.packager_response_id = package_result.response_id
            persist_scheduler()

        artifact_paths[f"candidate_attempt_{attempt_name}"] = package_path
        artifact_paths[f"candidate_attempt_{attempt_name}_evidence"] = package_evidence_path
        artifact_paths[f"candidate_attempt_{attempt_name}_proof"] = package_proof_path
        artifact_paths[f"candidate_attempt_{attempt_name}_sources"] = imported_sources_path
        artifact_paths["candidate_package"] = atomic_write_json(
            candidate_dir / "package.json", current_candidate
        )
        artifact_paths["candidate_proof"] = atomic_write_text(
            candidate_dir / "proof.md", current_candidate.full_proof
        )
        artifact_paths["candidate_dependency_graph"] = atomic_write_json(
            candidate_dir / "dependency_graph.json",
            [
                dependency.model_dump(mode="json")
                for dependency in current_candidate.lemma_dependency_graph
            ],
        )
        current_audits = {}
        current_verdict = None
        final_judge_response_id = ""
        if normalize_exact_statement(current_candidate.exact_theorem) != normalize_exact_statement(
            compiled.normalized_statement
        ):
            return (
                None,
                [
                    "Candidate theorem does not exactly match the frozen target; package the "
                    "canonical statement byte-for-byte without weakening or strengthening it."
                ],
                None,
                "scientific",
            )
        if current_candidate.unresolved_items:
            return (
                None,
                list(current_candidate.unresolved_items),
                None,
                "scientific",
            )

        required_audits = list(audit_names)
        run_complexity = (
            settings.run_complexity_audit
            if settings.run_complexity_audit is not None
            else current_candidate.quantitative_or_algorithmic
        )
        if run_complexity:
            required_audits.append("complexity")
        if attempt.mandatory_audits and attempt.mandatory_audits != required_audits:
            raise StageValidationError("Candidate mandatory-audit set changed after it was frozen.")
        if not attempt.mandatory_audits:
            attempt.mandatory_audits = list(required_audits)
            persist_scheduler()
        audit_inputs = {
            name: json.dumps(
                {
                    "audit_role": name,
                    "claim_contract": compiled.claim_contract.as_dict(),
                    **candidate_gate_policy_payload(attempt),
                    "candidate_package": current_candidate.model_dump(mode="json"),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            for name in required_audits
        }
        old_audit_request_registered = any(
            tracker.has_request(
                instructions=audit_instructions[name],
                input_text=audit_inputs[name],
                settings=attempt.audit_settings,
                output_type=AuditVerdict,
            )
            for name in required_audits
        )
        if (
            not attempt.audit_sha256
            and not old_audit_request_registered
            and attempt.audit_settings != auditor_model
        ):
            # Mandatory audits are registered as one persisted batch before any
            # audit task starts. With no committed audit and no frozen key, none of
            # them can have launched, so the current resumed policy is safe.
            attempt.audit_settings = auditor_model.model_copy(deep=True)
            persist_scheduler()

        audit_attempt_dir = ensure_stage_directory(audits_dir / "attempts" / attempt_name)
        unexpected_committed_audits = sorted(set(attempt.audit_sha256) - set(required_audits))
        if unexpected_committed_audits:
            raise StageValidationError(
                "Candidate attempt has audits inconsistent with its frozen package: "
                + ", ".join(unexpected_committed_audits)
            )
        for name, digest in attempt.audit_sha256.items():
            audit_path = audit_attempt_dir / f"{name}.json"
            if not audit_path.is_file() or sha256_file(audit_path) != digest:
                raise StageValidationError(
                    f"Committed {name} audit evidence is missing or changed."
                )
            if name not in attempt.audit_response_ids:
                raise StageValidationError(
                    f"Committed {name} audit has no recorded response identity."
                )
            current_audits[name] = AuditVerdict.model_validate_json(read_regular_text(audit_path))

        async def run_audit(name: str) -> tuple[str, AuditVerdict, str]:
            result = await generate_model(
                instructions=audit_instructions[name],
                input_text=audit_inputs[name],
                model_settings=attempt.audit_settings,
                output_type=AuditVerdict,
                selected_client=auditor_client,
            )
            verdict = result.parsed
            if verdict.rationale.startswith(
                "Legacy audit artifact"
            ) or verdict.checks_performed == [
                "Legacy audit artifact predates explicit check recording."
            ]:
                raise StageValidationError(
                    f"The {name} audit omitted its rationale or checks_performed evidence."
                )
            verdict = verdict.model_copy(
                update={
                    "audit_role": name,
                    "rationale": f"{name.replace('_', ' ').title()} audit: {verdict.rationale}",
                }
            )
            return name, verdict, result.response_id

        missing_audits = [name for name in required_audits if name not in current_audits]
        new_logical_audit_calls = sum(
            not tracker.has_request(
                instructions=audit_instructions[name],
                input_text=audit_inputs[name],
                settings=attempt.audit_settings,
                output_type=AuditVerdict,
            )
            for name in missing_audits
        )
        new_paid_audit_calls = sum(
            not tracker.has_request(
                instructions=audit_instructions[name],
                input_text=audit_inputs[name],
                settings=attempt.audit_settings,
                output_type=AuditVerdict,
            )
            and not tracker.is_accounted_request(
                instructions=audit_instructions[name],
                input_text=audit_inputs[name],
                settings=attempt.audit_settings,
                output_type=AuditVerdict,
            )
            for name in missing_audits
        )
        provisional_judge_input: str | None = None
        judge_request_already_registered = False
        judge_request_already_accounted = False
        if not missing_audits and attempt.verdict_sha256 is None:
            provisional_judge_input = json.dumps(
                {
                    "claim_contract": compiled.claim_contract.as_dict(),
                    **candidate_gate_policy_payload(attempt),
                    "candidate_package": current_candidate.model_dump(mode="json"),
                    "independent_audits": {
                        name: current_audits[name].model_dump(mode="json")
                        for name in required_audits
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            old_judge_request_registered = tracker.has_request(
                instructions=judge_prompt,
                input_text=provisional_judge_input,
                settings=attempt.judge_settings,
                output_type=FinalJudgeVerdict,
            )
            if not old_judge_request_registered and attempt.judge_settings != judge_model:
                attempt.judge_settings = judge_model.model_copy(deep=True)
                persist_scheduler()
            judge_request_already_registered = tracker.has_request(
                instructions=judge_prompt,
                input_text=provisional_judge_input,
                settings=attempt.judge_settings,
                output_type=FinalJudgeVerdict,
            )
            judge_request_already_accounted = tracker.is_accounted_request(
                instructions=judge_prompt,
                input_text=provisional_judge_input,
                settings=attempt.judge_settings,
                output_type=FinalJudgeVerdict,
            )
        stable_judge_reservation_key = sha256_text(
            f"candidate-final-judge-reservation:{compiled_digest}:{attempt_name}"
        )
        live_judge_reservation = bool(
            attempt.judge_call_reservation_key
            and tracker.has_call_key(attempt.judge_call_reservation_key)
        )
        if (
            live_judge_reservation
            and attempt.judge_call_reservation_key != stable_judge_reservation_key
        ):
            raise StageValidationError("Candidate final-judge reservation is inconsistent.")
        new_judge_reservation = int(
            attempt.verdict_sha256 is None
            and not judge_request_already_registered
            and not live_judge_reservation
            and bool(missing_audits)
            and new_paid_audit_calls > 0
        )
        new_exact_judge_logical_call = int(
            attempt.verdict_sha256 is None
            and provisional_judge_input is not None
            and not judge_request_already_registered
            and not live_judge_reservation
        )
        new_exact_judge_paid_call = int(
            new_exact_judge_logical_call and not judge_request_already_accounted
        )
        if not tracker.can_admit(
            paid_calls=(new_paid_audit_calls + new_judge_reservation + new_exact_judge_paid_call),
            logical_calls=(
                new_logical_audit_calls + new_judge_reservation + new_exact_judge_logical_call
            ),
        ):
            return (
                None,
                ["Budget cannot fund every mandatory audit and the final judge."],
                None,
                "budget",
            )

        reservations_changed = False
        for name in missing_audits:
            reservations_changed = (
                tracker.register_request(
                    instructions=audit_instructions[name],
                    input_text=audit_inputs[name],
                    settings=attempt.audit_settings,
                    output_type=AuditVerdict,
                )
                or reservations_changed
            )
        if new_judge_reservation:
            tracker.reserve_call_key(stable_judge_reservation_key)
            attempt.judge_call_reservation_key = stable_judge_reservation_key
            reservations_changed = True
        if new_exact_judge_logical_call:
            assert provisional_judge_input is not None
            reservations_changed = (
                tracker.register_request(
                    instructions=judge_prompt,
                    input_text=provisional_judge_input,
                    settings=attempt.judge_settings,
                    output_type=FinalJudgeVerdict,
                )
                or reservations_changed
            )
        if reservations_changed:
            persist_scheduler()

        audit_tasks: dict[asyncio.Task[tuple[str, AuditVerdict, str]], str] = {}
        if missing_audits:
            for name in missing_audits:
                task = asyncio.create_task(run_audit(name))
                audit_tasks[task] = name
            pending_audits = set(audit_tasks)
            while pending_audits:
                completed_audits, pending_audits = await asyncio.wait(
                    pending_audits,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in completed_audits:
                    name = audit_tasks[task]
                    try:
                        completed_name, audit, response_id = task.result()
                    except (BudgetExceeded, _ResearchBudgetExhausted) as exc:
                        issue = record_execution_issue(
                            event_kind="candidate_audit_unavailable",
                            exc=exc,
                            category=FailureCategory.RESOURCE,
                            candidate_attempt=attempt_name,
                            audit_name=name,
                            extra_obligations=[f"Retry the missing mandatory {name} audit."],
                        )
                        attempt.audit_execution_issues.append(issue)
                        persist_scheduler()
                        continue
                    except BaseException as exc:
                        category = classify_failure(exc)
                        if category is FailureCategory.INTEGRITY:
                            for pending_task in pending_audits:
                                pending_task.cancel()
                            await asyncio.gather(*pending_audits, return_exceptions=True)
                            raise
                        issue = record_execution_issue(
                            event_kind="candidate_audit_unavailable",
                            exc=exc,
                            category=(FailureCategory.EVIDENCE if name == "sources" else category),
                            candidate_attempt=attempt_name,
                            audit_name=name,
                            extra_obligations=[f"Retry the missing mandatory {name} audit."],
                        )
                        attempt.audit_execution_issues.append(issue)
                        persist_scheduler()
                        continue
                    if completed_name != name:
                        raise StageValidationError(
                            f"Audit task {name!r} returned identity {completed_name!r}."
                        )
                    current_audits[name] = audit
                    attempt.audit_response_ids[name] = response_id
                    audit_path = _atomic_write_immutable_json(
                        audit_attempt_dir / f"{name}.json", audit
                    )
                    attempt.audit_sha256[name] = sha256_file(audit_path)
                    artifact_paths[f"audit_{attempt_name}_{name}"] = audit_path
                    artifact_paths[f"audit_{name}"] = atomic_write_json(
                        audits_dir / f"{name}.json", audit
                    )
                    # Each pass/failure is a standalone candidate checkpoint. A crash
                    # after this point retries only audits absent from audit_sha256.
                    append_event(
                        "candidate_audit_completed",
                        response_id=response_id,
                        artifact=audit_path,
                        detail=[name, audit.verdict.value],
                    )
        unavailable_audits = [name for name in required_audits if name not in current_audits]
        if unavailable_audits:
            scheduler.phase = SchedulerPhase.AWAITING_AUDITS
            persist_scheduler()
            return (
                None,
                [f"Mandatory audit remains unavailable: {name}." for name in unavailable_audits],
                None,
                ("evidence" if unavailable_audits == ["sources"] else "execution"),
            )

        for name, audit in current_audits.items():
            artifact_paths[f"audit_{attempt_name}_{name}"] = audit_attempt_dir / f"{name}.json"
            artifact_paths[f"audit_{name}"] = atomic_write_json(audits_dir / f"{name}.json", audit)

        judge_input = json.dumps(
            {
                "claim_contract": compiled.claim_contract.as_dict(),
                **candidate_gate_policy_payload(attempt),
                "candidate_package": current_candidate.model_dump(mode="json"),
                "independent_audits": {
                    name: audit.model_dump(mode="json") for name, audit in current_audits.items()
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        verdict_path = attempt_dir / "verdict.json"
        if (
            attempt.verdict_sha256 is None
            and not tracker.has_request(
                instructions=judge_prompt,
                input_text=judge_input,
                settings=attempt.judge_settings,
                output_type=FinalJudgeVerdict,
            )
            and attempt.judge_settings != judge_model
        ):
            # The settings-independent judge placeholder reserves capacity, not a
            # provider request. Until an exact judge key exists, use the resumed
            # judge policy; otherwise preserve the registered request verbatim.
            attempt.judge_settings = judge_model.model_copy(deep=True)
            persist_scheduler()
        if attempt.verdict_sha256 is not None:
            if (
                not verdict_path.is_file()
                or sha256_file(verdict_path) != attempt.verdict_sha256
                or not attempt.final_judge_response_id
            ):
                raise StageValidationError("Committed final-judge evidence is missing or changed.")
            current_verdict = FinalJudgeVerdict.model_validate_json(read_regular_text(verdict_path))
            final_judge_response_id = attempt.final_judge_response_id
        else:
            live_reservation_key = (
                attempt.judge_call_reservation_key
                if attempt.judge_call_reservation_key is not None
                and tracker.has_call_key(attempt.judge_call_reservation_key)
                else None
            )
            if (
                not tracker.has_request(
                    instructions=judge_prompt,
                    input_text=judge_input,
                    settings=attempt.judge_settings,
                    output_type=FinalJudgeVerdict,
                )
                and live_reservation_key is None
            ):
                if not tracker.can_generate(
                    instructions=judge_prompt,
                    input_text=judge_input,
                    settings=attempt.judge_settings,
                    output_type=FinalJudgeVerdict,
                ):
                    return (
                        None,
                        ["Budget cannot fund the mandatory final judge."],
                        None,
                        "budget",
                    )
                tracker.register_request(
                    instructions=judge_prompt,
                    input_text=judge_input,
                    settings=attempt.judge_settings,
                    output_type=FinalJudgeVerdict,
                )
                persist_scheduler()
            judge_result = await generate_model(
                instructions=judge_prompt,
                input_text=judge_input,
                model_settings=attempt.judge_settings,
                output_type=FinalJudgeVerdict,
                selected_client=judge_client,
                reservation_key=live_reservation_key,
            )
            current_verdict = judge_result.parsed
            final_judge_response_id = judge_result.response_id
            _atomic_write_immutable_json(verdict_path, current_verdict)
            attempt.verdict_sha256 = sha256_file(verdict_path)
            attempt.final_judge_response_id = final_judge_response_id
            attempt.judge_call_reservation_key = None
            persist_scheduler()
        artifact_paths[f"verdict_{attempt_name}"] = verdict_path
        artifact_paths["verdict"] = atomic_write_json(destination / "verdict.json", current_verdict)

        audit_obligations = [
            obligation
            for audit in current_audits.values()
            for obligation in audit.unresolved_obligations
        ]
        failed_audits = [
            name
            for name, audit in current_audits.items()
            if audit.verdict != AuditDecision.PASS
            or not audit.target_matches
            or audit.unresolved_obligations
            or any(issue.severity.casefold() == "blocking" for issue in audit.issues)
        ]
        if current_verdict.verdict == FinalJudgeDecision.ACCEPTED:
            inconsistent = [
                *current_candidate.unresolved_items,
                *audit_obligations,
                *current_verdict.unresolved_obligations,
            ]
            if failed_audits or inconsistent:
                return (
                    None,
                    [
                        *(f"Mandatory audit did not pass: {name}" for name in failed_audits),
                        *inconsistent,
                    ],
                    current_verdict.verdict,
                    "scientific",
                )
            return (
                ResearchAcceptanceGate(
                    accepted=True,
                    candidate_sha256=sha256_json(current_candidate),
                    claim_contract_sha256=sha256_text(
                        json.dumps(
                            compiled.claim_contract.as_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                        )
                    ),
                    mandatory_audits=required_audits,
                    final_judge_response_id=final_judge_response_id,
                    computation_bindings_sha256=candidate_computation_bindings_digest(
                        attempt.computation_bindings
                    ),
                    graph_support_bindings_sha256=candidate_graph_support_bindings_digest(
                        attempt.graph_support_bindings
                    ),
                ),
                [],
                current_verdict.verdict,
                None,
            )
        obligations = list(
            dict.fromkeys(
                [
                    *current_verdict.unresolved_obligations,
                    *audit_obligations,
                    *(f"Repair failed {name} audit." for name in failed_audits),
                    *current_verdict.reasons,
                ]
            )
        )
        return None, obligations, current_verdict.verdict, "scientific"

    def launch_available() -> None:
        for record in assignment_records(AssignmentLifecycle.QUEUED):
            if not _assignment_matches_active_scientific_phase(
                record,
                scientific_phase_state,
            ):
                release_unlaunched_worker_request(record)
                record.status = AssignmentLifecycle.RETIRED
                append_event(
                    "worker_retired_after_scientific_phase_transition",
                    assignment_id=record.assignment.id,
                    decision_id=record.admitted_by_decision,
                    detail=[
                        "Assignment phase: "
                        f"{record.assignment.scientific_phase.value} "
                        f"(epoch {record.scientific_phase_epoch}).",
                        "Current phase: "
                        f"{scientific_phase_state.phase.value} "
                        f"(epoch {scientific_phase_state.phase_epoch}).",
                        "The coordinator may replace it with work bound to the current frontier.",
                    ],
                )
                continue
            if len(active) >= active_scientific_concurrency():
                break
            record.status = AssignmentLifecycle.RUNNING
            record.launched = True
            append_event(
                "worker_launched",
                assignment_id=record.assignment.id,
                decision_id=record.admitted_by_decision,
            )
            task = asyncio.create_task(run_worker(record))
            active[task] = record

    async def collect_tasks(
        tasks: set[asyncio.Task[Any]],
        *,
        requeue_cancelled: bool,
    ) -> WorkerCollectionResult:
        accepted_reports: list[ResearchWorkerReport] = []
        candidate_ids: list[str] = []
        execution_issues: list[ExecutionIssue] = []
        ordered = sorted(
            tasks,
            key=lambda task: scheduler.assignments.index(active[task]),
        )
        results = await asyncio.gather(*ordered, return_exceptions=True)
        for task, result in zip(ordered, results, strict=True):
            record = active.pop(task)
            if isinstance(result, tuple):
                report, response_id = result
                accept_worker_result(record, report, response_id)
                try:
                    await audit_admitted_exact_counterexamples(record, report)
                    if scheduler.final_outcome is None:
                        await audit_admitted_intermediate(record, report)
                except asyncio.CancelledError:
                    persist_scheduler()
                    raise
                except BaseException as exc:
                    if classify_failure(exc) is FailureCategory.INTEGRITY:
                        persist_scheduler()
                        raise
                    execution_issues.append(
                        record_execution_issue(
                            event_kind="scientific_evidence_audit_failed",
                            exc=exc,
                            category=FailureCategory.EVIDENCE,
                            assignment_id=record.assignment.id,
                            extra_obligations=[
                                "Resume the independent scientific-evidence audit from its "
                                "frozen nomination and evidence before changing any trust status."
                            ],
                            include_default_obligations=False,
                        )
                    )
                record_phase_progress()
                accepted_reports.append(report)
                if report.status == WorkerStatus.CANDIDATE_COMPLETE:
                    candidate_ids.append(record.assignment.id)
                continue
            if isinstance(result, asyncio.CancelledError):
                if requeue_cancelled and record.status != AssignmentLifecycle.RETIRED:
                    record.status = AssignmentLifecycle.QUEUED
                elif record.status != AssignmentLifecycle.RETIRED:
                    record.status = AssignmentLifecycle.CANCELLED
                continue
            if isinstance(result, BaseException):
                category = classify_failure(result)
                if category is FailureCategory.INTEGRITY:
                    persist_scheduler()
                    raise result
                record.execution_attempts += 1
                issue = record_execution_issue(
                    event_kind="worker_execution_failed",
                    exc=result,
                    category=category,
                    assignment_id=record.assignment.id,
                    repair_generation=record.repair_generation,
                    extra_obligations=[
                        (
                            "Run the one bounded repair generation for this assignment."
                            if record.repair_generation == 0
                            else "Coordinator must reassign or retire this failed assignment."
                        )
                    ],
                )
                execution_issues.append(issue)
                if (
                    record.status != AssignmentLifecycle.RETIRED
                    and record.repair_generation == 0
                    and category in {FailureCategory.EXECUTION, FailureCategory.EVIDENCE}
                ):
                    record.repair_generation = 1
                    record.status = AssignmentLifecycle.QUEUED
                    record.request_key = None
                    try:
                        reserve_worker_request(record)
                    except (_ResearchBudgetExhausted, BudgetExceeded) as budget_exc:
                        record.status = AssignmentLifecycle.RETIRED
                        execution_issues.append(
                            record_execution_issue(
                                event_kind="worker_repair_unavailable",
                                exc=budget_exc,
                                category=FailureCategory.RESOURCE,
                                assignment_id=record.assignment.id,
                                repair_generation=1,
                                extra_obligations=[
                                    "Coordinator must reassign or retire the task when capacity "
                                    "is available."
                                ],
                            )
                        )
                elif record.status != AssignmentLifecycle.RETIRED:
                    record.status = AssignmentLifecycle.RETIRED
                continue
            invalid_result = StageValidationError(
                "Research worker returned an invalid task result."
            )
            record.status = AssignmentLifecycle.RETIRED
            execution_issues.append(
                record_execution_issue(
                    event_kind="worker_execution_failed",
                    exc=invalid_result,
                    category=FailureCategory.EXECUTION,
                    assignment_id=record.assignment.id,
                    repair_generation=record.repair_generation,
                    extra_obligations=["Coordinator must reassign or retire the task."],
                )
            )
        if candidate_ids:
            active_attempt_report_ids = set(
                scheduler.active_candidate_attempt.report_ids
                if scheduler.active_candidate_attempt is not None
                else []
            )
            scheduler.deferred_candidate_report_ids = list(
                dict.fromkeys(
                    [
                        *scheduler.deferred_candidate_report_ids,
                        *(
                            assignment_id
                            for assignment_id in candidate_ids
                            if assignment_id not in scheduler.pending_candidate_report_ids
                            and not candidate_report_set_attempted([assignment_id])
                            and assignment_id not in active_attempt_report_ids
                        ),
                    ]
                )
            )
        persist_scheduler()
        return WorkerCollectionResult(
            accepted_reports=accepted_reports,
            candidate_ids=candidate_ids,
            execution_issues=execution_issues,
        )

    async def pause_active(*, requeue_cancelled: bool) -> list[str]:
        tasks = set(active)
        for task in tasks:
            task.cancel()
        collected = await collect_tasks(tasks, requeue_cancelled=requeue_cancelled)
        return collected.candidate_ids

    async def apply_directed_cancellations() -> list[str]:
        tasks = {
            task for task, record in active.items() if record.status == AssignmentLifecycle.RETIRED
        }
        for task in tasks:
            task.cancel()
        if not tasks:
            return []
        collected = await collect_tasks(tasks, requeue_cancelled=False)
        return collected.candidate_ids

    def archived_candidate_attempt_for_replay(
        *,
        attempt_name: str,
        report_ids: list[str],
        source: Literal["worker", "coordinator"],
    ) -> tuple[CandidateAttemptState, dict[str, object]] | None:
        """Return the authenticated frozen candidate input for prompt-only replay.

        A successful candidate gate promotes graph nodes after its model calls finish.  Rebuilding
        candidate support from that now-promoted graph would therefore give an explicit forced
        prompt-compilation replay a different request identity and repeat paid calls.  The
        application already freezes coordinator, assignment, and admission context for this
        replay mode; candidate support must use the same archived transaction boundary.
        """

        if replay_scheduler is None or replay_root is None:
            return None
        archived = replay_scheduler.latest_candidate_attempt
        if (
            archived is None
            or archived.attempt_name != attempt_name
            or archived.report_ids != report_ids
            or archived.source != source
        ):
            return None
        archived_input_path = (replay_root / archived.package_input_path).resolve(strict=True)
        try:
            archived_input_path.relative_to(replay_root)
        except ValueError as exc:
            raise StageValidationError(
                "Archived candidate input escapes its research generation."
            ) from exc
        if sha256_file(archived_input_path) != archived.package_input_sha256:
            raise StageValidationError("Archived candidate input changed before replay.")
        raw_payload = json.loads(read_regular_text(archived_input_path))
        if not isinstance(raw_payload, dict):
            raise StageValidationError("Archived candidate input is not a JSON object.")
        return archived.model_copy(deep=True), cast(dict[str, object], raw_payload)

    async def audit_pending_candidate(
        *, resume_after_failure: bool = True
    ) -> ResearchResult | None:
        nonlocal repair_rounds
        attempt = scheduler.active_candidate_attempt
        if attempt is None:
            if tracker.maximum_calls is not None:
                for queued_record in assignment_records(AssignmentLifecycle.QUEUED):
                    if not queued_record.launched:
                        release_unlaunched_worker_request(queued_record)
                        queued_record.status = AssignmentLifecycle.RETIRED
            report_ids = list(dict.fromkeys(scheduler.pending_candidate_report_ids))
            source = scheduler.pending_candidate_source or "worker"
            if not report_ids:
                raise StageValidationError("Candidate audit has no durable triggering report.")
            unknown_reports = [
                assignment_id for assignment_id in report_ids if assignment_id not in reports_by_id
            ]
            if unknown_reports:
                raise StageValidationError(
                    "Candidate audit references incomplete reports: " + ", ".join(unknown_reports)
                )
            scheduler.phase = SchedulerPhase.AUDITING
            scheduler.candidate_attempts += 1
            attempt_number = scheduler.candidate_attempts
            attempt_name = f"event-{scheduler.next_event_sequence - 1}-attempt-{attempt_number}"
            attempt_dir = ensure_stage_directory(candidate_dir / "attempts" / attempt_name)
            archived_replay = archived_candidate_attempt_for_replay(
                attempt_name=attempt_name,
                report_ids=report_ids,
                source=source,
            )
            if archived_replay is None:
                computation_bindings, computation_obligations = (
                    verify_candidate_computation_bindings(report_ids)
                )
                graph_support_bindings, graph_support_obligations = (
                    verify_candidate_graph_support_bindings(report_ids)
                )
                archived_package_payload = None
            else:
                archived_attempt, archived_package_payload = archived_replay
                computation_bindings = archived_attempt.computation_bindings
                computation_obligations = archived_attempt.computation_obligations
                graph_support_bindings = archived_attempt.graph_support_bindings
                graph_support_obligations = archived_attempt.graph_support_obligations
            package_payload: dict[str, object] = {
                "claim_contract": compiled.claim_contract.as_dict(),
                "exact_target_policy": exact_target_policy(),
                "approach_registry": registry.model_dump(mode="json"),
                "visible_worker_reports": [
                    reports_by_id[assignment_id].model_dump(mode="json")
                    for assignment_id in report_ids
                ],
                "candidate_trigger_assignment_ids": report_ids,
                "candidate_computation_gate": {
                    "bindings": [
                        binding.model_dump(mode="json") for binding in computation_bindings
                    ],
                    "blocking_obligations": computation_obligations,
                },
                "candidate_canonical_graph_support": {
                    "bindings": [
                        binding.model_dump(mode="json") for binding in graph_support_bindings
                    ],
                    "blocking_obligations": graph_support_obligations,
                },
                "constraint": (
                    "Package only the proof supported by the named reports. Expose every "
                    "unresolved step; follow dependency_result_keys exactly and do not substitute "
                    "an unrelated route or computation."
                ),
            }
            if archived_package_payload is not None and package_payload != archived_package_payload:
                raise StageValidationError(
                    "Candidate transaction inputs changed during prompt-only graph replay."
                )
            package_input_path = _atomic_write_immutable_json(
                attempt_dir / "input.json", package_payload
            )
            artifact_paths[f"candidate_attempt_{attempt_name}_input"] = package_input_path
            attempt = CandidateAttemptState(
                attempt_name=attempt_name,
                report_ids=report_ids,
                source=source,
                exact_target_policy_version=1,
                computation_gate_version=1,
                computation_bindings=computation_bindings,
                computation_obligations=computation_obligations,
                graph_support_gate_version=1,
                graph_support_bindings=graph_support_bindings,
                graph_support_obligations=graph_support_obligations,
                packager_settings=worker_model.model_copy(deep=True),
                audit_settings=auditor_model.model_copy(deep=True),
                judge_settings=judge_model.model_copy(deep=True),
                package_input_path=package_input_path.relative_to(destination).as_posix(),
                package_input_sha256=sha256_file(package_input_path),
            )
            candidate_report_key = canonical_candidate_report_set(report_ids)
            if len(candidate_report_key) == 1:
                scheduler.deferred_candidate_report_ids = [
                    assignment_id
                    for assignment_id in scheduler.deferred_candidate_report_ids
                    if assignment_id != candidate_report_key[0]
                ]
            scheduler.attempted_candidate_report_sets = [
                *scheduler.attempted_candidate_report_sets,
                candidate_report_key,
            ]
            scheduler.active_candidate_attempt = attempt
            append_event(
                "candidate_audit_started",
                artifact=package_input_path,
                detail=report_ids,
            )
            if computation_obligations:
                append_event(
                    "candidate_computation_evidence_rejected",
                    artifact=package_input_path,
                    related_artifacts=[
                        resolved_artifact(binding.evidence_path) for binding in computation_bindings
                    ],
                    detail=computation_obligations,
                )
            if graph_support_obligations:
                append_event(
                    "candidate_canonical_graph_support_rejected",
                    artifact=package_input_path,
                    related_artifacts=[
                        resolved_artifact(binding.admission_record_path)
                        for binding in graph_support_bindings
                    ],
                    detail=graph_support_obligations,
                )
        else:
            report_ids = list(attempt.report_ids)
            attempt_name = attempt.attempt_name
            scheduler.phase = SchedulerPhase.AUDITING
            persist_scheduler()

        if attempt.outcome_ready:
            gate = (
                ResearchAcceptanceGate.model_validate(attempt.outcome_gate)
                if attempt.outcome_gate is not None
                else None
            )
            obligations = list(attempt.outcome_obligations)
            decision = attempt.outcome_decision
            failure_kind = attempt.outcome_failure_kind
        else:
            evaluation_task = asyncio.create_task(
                evaluate_candidate(report_ids, attempt_name=attempt_name)
            )
            try:
                while True:
                    wait_targets: set[asyncio.Task[Any]] = {
                        *active,
                        evaluation_task,
                    }
                    completed, _ = await asyncio.wait(
                        wait_targets,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    completed_workers = {task for task in completed if task is not evaluation_task}
                    if completed_workers:
                        raced_collection = await collect_tasks(
                            completed_workers,
                            requeue_cancelled=True,
                        )
                        raced = raced_collection.candidate_ids
                        attempt.raced_candidate_report_ids = list(
                            dict.fromkeys([*attempt.raced_candidate_report_ids, *raced])
                        )
                        persist_research_index()
                        if (
                            scheduler.final_outcome is ResearchOutcome.REJECTED
                            or unresolved_exact_counterexample_obligations()
                        ):
                            evaluation_task.cancel()
                            await asyncio.gather(evaluation_task, return_exceptions=True)
                            priority_result = await counterexample_priority_result()
                            if priority_result is None:  # pragma: no cover - state invariant
                                raise StageValidationError(
                                    "Counterexample priority disappeared during candidate race."
                                )
                            return priority_result
                    if evaluation_task in completed:
                        gate, obligations, decision, failure_kind = await evaluation_task
                        break
            except BaseException:
                evaluation_task.cancel()
                await asyncio.gather(evaluation_task, return_exceptions=True)
                raise

            if failure_kind in {"execution", "evidence"}:
                attempt.outcome_ready = False
                attempt.outcome_gate = None
                attempt.outcome_obligations = []
                attempt.outcome_decision = None
                attempt.outcome_failure_kind = None
                scheduler.phase = SchedulerPhase.AWAITING_AUDITS
                persist_scheduler()
                return await pause_retriable(obligations=obligations)

            attempt.outcome_ready = True
            attempt.outcome_gate = gate.model_dump(mode="json") if gate is not None else None
            attempt.outcome_obligations = list(dict.fromkeys(obligations))
            attempt.outcome_decision = decision
            attempt.outcome_failure_kind = failure_kind
            if (
                failure_kind == "budget"
                and attempt.judge_call_reservation_key is not None
                and tracker.has_call_key(attempt.judge_call_reservation_key)
            ):
                tracker.release_call_key(attempt.judge_call_reservation_key)
                attempt.judge_call_reservation_key = None
            persist_scheduler()

        done_after_outcome = {task for task in active if task.done()}
        if done_after_outcome:
            raced_collection = await collect_tasks(
                done_after_outcome,
                requeue_cancelled=True,
            )
            raced = raced_collection.candidate_ids
            attempt.raced_candidate_report_ids = list(
                dict.fromkeys([*attempt.raced_candidate_report_ids, *raced])
            )
            persist_research_index()
            priority_result = await counterexample_priority_result()
            if priority_result is not None:
                return priority_result
        sync_tracker()
        if gate is not None:
            priority_result = await counterexample_priority_result()
            if priority_result is not None:
                return priority_result
            if scheduler.final_outcome is not None:
                raise StageValidationError(
                    "Candidate acceptance cannot overwrite a durable terminal research outcome."
                )
            accepted_attempt_path = candidate_dir / "attempts" / attempt_name
            accepted_verdict_path = accepted_attempt_path / "verdict.json"
            accepted_computation_paths = list(
                dict.fromkeys(
                    resolved_artifact(relative)
                    for binding in attempt.computation_bindings
                    for relative in (
                        binding.evidence_path,
                        binding.manifest_path,
                        binding.replay_path,
                    )
                )
            )
            accepted_graph_admission_paths = list(
                dict.fromkeys(
                    resolved_artifact(binding.admission_record_path)
                    for binding in attempt.graph_support_bindings
                )
            )
            accepted_evidence = [
                accepted_attempt_path / "evidence.json",
                accepted_attempt_path / "package.json",
                accepted_attempt_path / "source_verification.json",
                *accepted_computation_paths,
                *accepted_graph_admission_paths,
                *(
                    audits_dir / "attempts" / attempt_name / f"{name}.json"
                    for name in gate.mandatory_audits
                ),
            ]
            strongest_result = (
                current_verdict.strongest_result
                if current_verdict is not None
                else current_candidate.exact_theorem
                if current_candidate is not None
                else ""
            )
            scheduler.latest_candidate_attempt_name = attempt_name
            scheduler.latest_candidate_attempt = attempt.model_copy(deep=True)
            validate_acceptance_gate(
                gate,
                attempt=attempt,
                require_pass_event=False,
            )
            scheduler.active_candidate_attempt = None
            scheduler.pending_candidate_report_ids = []
            scheduler.pending_candidate_source = None
            scheduler.repair_obligations = []
            scheduler.stop_reason = None
            scheduler.stop_category = None
            scheduler.final_outcome = ResearchOutcome.ACCEPTED
            scheduler.final_obligations = []
            scheduler.final_strongest_result = strongest_result
            scheduler.final_acceptance_gate = gate.model_dump(mode="json")
            scheduler.final_refutation_gate = None
            scheduler.final_refutation_audit_id = None
            scheduler.phase = SchedulerPhase.COMPLETE
            append_event(
                "candidate_audit_passed",
                response_id=gate.final_judge_response_id,
                artifact=accepted_verdict_path,
                related_artifacts=accepted_evidence,
                detail=report_ids,
            )
            if scientific_phase_state.phase is ScientificPhase.SYNTHESIZE:
                record_phase_progress(synthesis_succeeded=True)
            return await finish(
                ResearchOutcome.ACCEPTED,
                strongest_result=strongest_result,
                acceptance_gate=gate,
            )

        scheduler.failed_candidate_attempts += 1
        repair_rounds = scheduler.failed_candidate_attempts
        scheduler.repair_obligations = list(dict.fromkeys(obligations))
        scheduler.deferred_candidate_report_ids = list(
            dict.fromkeys(
                [
                    *scheduler.deferred_candidate_report_ids,
                    *attempt.raced_candidate_report_ids,
                ]
            )
        )
        scheduler.latest_candidate_attempt_name = attempt_name
        scheduler.latest_candidate_attempt = attempt.model_copy(deep=True)
        scheduler.active_candidate_attempt = None
        scheduler.pending_candidate_report_ids = []
        scheduler.pending_candidate_source = None
        scheduler.phase = SchedulerPhase.RUNNING
        candidate_attempt_path = candidate_dir / "attempts" / attempt_name
        failed_evidence = [
            path
            for path in (
                candidate_attempt_path / "evidence.json",
                candidate_attempt_path / "package.json",
                candidate_attempt_path / "source_verification.json",
                candidate_attempt_path / "verdict.json",
                *sorted((audits_dir / "attempts" / attempt_name).glob("*.json")),
            )
            if path.is_file()
        ]
        failed_verdict_path = candidate_attempt_path / "verdict.json"
        failed_package_path = candidate_attempt_path / "package.json"
        primary_failed_evidence = (
            failed_verdict_path
            if failed_verdict_path.is_file()
            else failed_package_path
            if failed_package_path.is_file()
            else None
        )
        append_event(
            "candidate_audit_failed",
            artifact=primary_failed_evidence,
            related_artifacts=[path for path in failed_evidence if path != primary_failed_evidence],
            detail=scheduler.repair_obligations,
        )
        persist_research_index()
        if failure_kind == "budget":
            return await finish(
                ResearchOutcome.BUDGET_EXHAUSTED,
                obligations=obligations,
            )
        if not resume_after_failure:
            return None
        # A candidate rejection rejects only that package. Full audit evidence returns
        # to the coordinator regardless of whether a worker or coordinator proposed it.
        if decision == FinalJudgeDecision.REPAIRABLE and not obligations:
            raise StageValidationError(
                "A repairable final verdict must include at least one exact obligation."
            )
        if scheduler.stop_reason is not None:
            # A proof candidate that raced with a terminal coordinator decision
            # still receives the independent acceptance gate. Once that candidate
            # also fails, the existing terminal decision is sufficient; purchasing
            # another coordinator activation cannot change the already-audited race.
            return None
        if len(scheduler.decisions) >= settings.maximum_coordinator_decisions:
            scheduler.repair_obligations = list(
                dict.fromkeys(
                    [
                        *scheduler.repair_obligations,
                        "Coordinator decision budget exhausted after a failed candidate audit.",
                    ]
                )
            )
            persist_scheduler()
            return None
        await request_coordinator_decision(initial=False)
        await apply_directed_cancellations()
        return None

    # Registry and continuity are derived from validated immutable reports on every
    # resume, so a torn materialized index can never compress or lose evidence.
    persist_research_index()

    def verified_refutation_is_terminal() -> bool:
        # Keep this check behind a function because audit helpers mutate scheduler state.
        return (
            scheduler.final_outcome == ResearchOutcome.REJECTED
            and scheduler.final_refutation_gate is not None
        )

    if scheduler.final_outcome is not None:
        return await finish(
            scheduler.final_outcome,
            obligations=scheduler.final_obligations,
            strongest_result=scheduler.final_strongest_result,
            acceptance_gate=(
                ResearchAcceptanceGate.model_validate(scheduler.final_acceptance_gate)
                if scheduler.final_acceptance_gate is not None
                else None
            ),
        )

    progress(
        Ascension.START_RESEARCH_COORDINATOR,
        (
            f"Resuming continuous research coordinator at event "
            f"{scheduler.next_event_sequence - 1}."
            if resumed
            else "Starting continuous research coordinator."
        ),
    )

    try:
        if not scheduler.decisions:
            if scheduler.pending_coordinator_request is None and initial_assignment_target() < 4:
                return await finish(
                    ResearchOutcome.BUDGET_EXHAUSTED,
                    obligations=[
                        "Configured model-call budget cannot fund the required diverse "
                        "initial portfolio."
                    ],
                )
            initial_decision = await request_coordinator_decision(initial=True)
            if initial_decision.stop_recommended:
                reason = initial_decision.stop_reason or "Coordinator stopped at initialization."
                return await finish(coordinator_stop_outcome(), obligations=[reason])

        # Reconcile the narrow gate-to-graph crash boundary before doing new work.
        for completed_record in scheduler.assignments:
            for counterexample_record in completed_record.exact_counterexample_audits:
                if (
                    not counterexample_record.superseded
                    and counterexample_record.gate_status
                    is CounterexampleAuditGateStatus.REFUTATION_VERIFIED
                    and scheduler.final_outcome is None
                ):
                    nomination, counterexample_gate = verify_persisted_counterexample_audit(
                        resolved_artifact(counterexample_record.nomination_path),
                        resolved_artifact(counterexample_record.gate_path),
                        expected_target_statement=compiled.normalized_statement,
                    )
                    promote_exact_counterexample(
                        completed_record,
                        counterexample_record,
                        nomination,
                        counterexample_gate,
                    )
        if verified_refutation_is_terminal():
            return await finish(
                ResearchOutcome.REJECTED,
                refutation_gate=(
                    CounterexampleAuditGate.model_validate(scheduler.final_refutation_gate)
                    if scheduler.final_refutation_gate is not None
                    else None
                ),
            )
        exact_counterexample_obligations = unresolved_exact_counterexample_obligations()
        if exact_counterexample_obligations:
            await audit_all_admitted_exact_counterexamples()
            if verified_refutation_is_terminal():
                return await finish(
                    ResearchOutcome.REJECTED,
                    refutation_gate=CounterexampleAuditGate.model_validate(
                        scheduler.final_refutation_gate
                    ),
                )
            exact_counterexample_obligations = unresolved_exact_counterexample_obligations()
            if exact_counterexample_obligations:
                return await pause_retriable(
                    obligations=exact_counterexample_obligations,
                    pause_reason="COUNTEREXAMPLE_AUDIT_INCOMPLETE",
                    resume_action=(
                        "Run `matek resume` to retry the unresolved independent exact-"
                        "counterexample audit before any proof candidate is accepted."
                    ),
                )
        higher_priority_audit_pending = bool(
            scheduler.pending_candidate_report_ids
            or scheduler.active_candidate_attempt is not None
            or exact_counterexample_audit_pending()
        )
        incomplete_intermediate_audit = False
        if not higher_priority_audit_pending:
            incomplete_intermediate_audit = await reconcile_pending_intermediate_audits()
        if incomplete_intermediate_audit:
            return await pause_retriable(
                obligations=scheduler.repair_obligations,
                pause_reason="LEMMA_AUDIT_INCOMPLETE",
                resume_action=(
                    "Run `matek resume` to retry only the missing independent intermediate "
                    "lemma-audit role against its frozen nomination."
                ),
            )
        # If a process stopped after accepting a worker but before nomination, replay
        # one highest-leverage nomination. Main-candidate audit retains budget priority.
        if (
            resumed
            and not scheduler.pending_candidate_report_ids
            and scheduler.active_candidate_attempt is None
        ):
            for completed_record in reversed(scheduler.assignments):
                completed_report = reports_by_id.get(completed_record.assignment.id)
                if completed_report is None:
                    continue
                if await audit_admitted_exact_counterexamples(
                    completed_record,
                    completed_report,
                ):
                    if verified_refutation_is_terminal():
                        return await finish(
                            ResearchOutcome.REJECTED,
                            refutation_gate=(
                                CounterexampleAuditGate.model_validate(
                                    scheduler.final_refutation_gate
                                )
                                if scheduler.final_refutation_gate is not None
                                else None
                            ),
                        )
                    if exact_counterexample_audit_pending():
                        return await pause_retriable(
                            obligations=scheduler.repair_obligations,
                            pause_reason="COUNTEREXAMPLE_AUDIT_INCOMPLETE",
                            resume_action=(
                                "Run `matek resume` to retry only the missing independent "
                                "exact-counterexample audit role."
                            ),
                        )
                    record_phase_progress()
                    break
                if await audit_admitted_intermediate(
                    completed_record,
                    completed_report,
                ):
                    if intermediate_lemma_audit_pending():
                        return await pause_retriable(
                            obligations=scheduler.repair_obligations,
                            pause_reason="LEMMA_AUDIT_INCOMPLETE",
                            resume_action=(
                                "Run `matek resume` to retry only the missing independent "
                                "intermediate lemma-audit role against its frozen nomination."
                            ),
                        )
                    record_phase_progress()
                    break

        initial_count = len(scheduler.decisions[0].decision.assignments)
        progress(
            Ascension.MANAGE_RESEARCH_POOL,
            "Managing adaptive research pool: "
            f"{initial_count} initial assignments, up to "
            f"{settings.maximum_concurrent_agents} active agents"
            f" ({scientific_phase_state.phase.value} phase limit "
            f"{active_scientific_concurrency()})"
            + (
                f", each with up to {settings.hierarchical_subagent_limit} nested subagents."
                if settings.hierarchical_subagent_limit > 0
                else "."
            ),
        )
        scheduler.phase = (
            SchedulerPhase.AUDITING
            if scheduler.pending_candidate_report_ids
            else SchedulerPhase.RUNNING
        )
        persist_scheduler()

        while True:
            if scheduler.pending_coordinator_request is not None:
                await request_coordinator_decision(
                    initial=scheduler.pending_coordinator_request.initial
                )
                await apply_directed_cancellations()
                continue

            exact_counterexample_obligations = unresolved_exact_counterexample_obligations()
            if exact_counterexample_obligations:
                await audit_all_admitted_exact_counterexamples()
                if verified_refutation_is_terminal():
                    return await finish(
                        ResearchOutcome.REJECTED,
                        refutation_gate=CounterexampleAuditGate.model_validate(
                            scheduler.final_refutation_gate
                        ),
                    )
                exact_counterexample_obligations = unresolved_exact_counterexample_obligations()
                if exact_counterexample_obligations:
                    return await pause_retriable(
                        obligations=exact_counterexample_obligations,
                        pause_reason="COUNTEREXAMPLE_AUDIT_INCOMPLETE",
                        resume_action=(
                            "Run `matek resume` to retry the unresolved independent exact-"
                            "counterexample audit before any proof candidate is accepted."
                        ),
                    )
                continue

            if scheduler.pending_candidate_report_ids:
                candidate_result = await audit_pending_candidate()
                if candidate_result is not None:
                    return candidate_result
                continue

            if (
                scheduler.active_candidate_attempt is None
                and not exact_counterexample_audit_pending()
                and intermediate_lemma_audit_pending()
            ):
                incomplete_intermediate_audit = await reconcile_pending_intermediate_audits()
                if incomplete_intermediate_audit:
                    return await pause_retriable(
                        obligations=scheduler.repair_obligations,
                        pause_reason="LEMMA_AUDIT_INCOMPLETE",
                        resume_action=(
                            "Run `matek resume` to retry only the missing independent "
                            "intermediate lemma-audit role against its frozen nomination."
                        ),
                    )
                continue

            if scheduler.stop_reason is not None:
                await pause_active(requeue_cancelled=False)
                for record in assignment_records(AssignmentLifecycle.QUEUED):
                    release_unlaunched_worker_request(record)
                    record.status = AssignmentLifecycle.RETIRED
                reason = scheduler.stop_reason
                return await finish(
                    coordinator_stop_outcome(),
                    obligations=scheduler.repair_obligations or [reason],
                )

            if coordinator_feedback_due():
                if len(scheduler.decisions) < settings.maximum_coordinator_decisions:
                    await request_coordinator_decision(initial=False)
                    await apply_directed_cancellations()
                    continue
                # No coordinator activation remains to revise queued work. Release
                # never-launched reservations so the already-running pool can drain
                # and any proof it finds still has the best available audit headroom.
                for record in assignment_records(AssignmentLifecycle.QUEUED):
                    if not record.launched:
                        release_unlaunched_worker_request(record)
                        record.status = AssignmentLifecycle.RETIRED
                persist_scheduler()

            done_now = {task for task in active if task.done()}
            if not done_now:
                launch_available()
                if not active:
                    if len(scheduler.decisions) >= settings.maximum_coordinator_decisions:
                        return await finish(
                            ResearchOutcome.BUDGET_EXHAUSTED,
                            obligations=scheduler.repair_obligations
                            or [
                                "Maximum continuous-coordinator decision budget reached "
                                "without an accepted proof."
                            ],
                        )
                    return await finish(
                        ResearchOutcome.PARTIAL,
                        obligations=scheduler.repair_obligations
                        or ["Coordinator has no remaining admissible research work."],
                    )
                done_now, _ = await asyncio.wait(set(active), return_when=asyncio.FIRST_COMPLETED)

            collection = await collect_tasks(
                done_now,
                requeue_cancelled=True,
            )
            if verified_refutation_is_terminal():
                return await finish(
                    ResearchOutcome.REJECTED,
                    refutation_gate=(
                        CounterexampleAuditGate.model_validate(scheduler.final_refutation_gate)
                        if scheduler.final_refutation_gate is not None
                        else None
                    ),
                )
            if exact_counterexample_audit_pending():
                return await pause_retriable(
                    obligations=scheduler.repair_obligations,
                    pause_reason="COUNTEREXAMPLE_AUDIT_INCOMPLETE",
                    resume_action=(
                        "Run `matek resume` to retry only the missing independent "
                        "exact-counterexample audit role."
                    ),
                )
            exact_counterexample_obligations = unresolved_exact_counterexample_obligations()
            if exact_counterexample_obligations:
                return await pause_retriable(
                    obligations=exact_counterexample_obligations,
                    pause_reason="COUNTEREXAMPLE_AUDIT_INCOMPLETE",
                    resume_action=(
                        "Run `matek resume` to resolve the exact-main counterexample "
                        "before any proof candidate is accepted."
                    ),
                )
            if intermediate_lemma_audit_pending():
                return await pause_retriable(
                    obligations=scheduler.repair_obligations,
                    pause_reason="LEMMA_AUDIT_INCOMPLETE",
                    resume_action=(
                        "Run `matek resume` to retry only the missing independent intermediate "
                        "lemma-audit role against its frozen nomination."
                    ),
                )
            candidate_ids = collection.candidate_ids
            persist_research_index()
            if collection.execution_issues and all(
                issue.category is FailureCategory.RESOURCE for issue in collection.execution_issues
            ):
                return await finish(
                    ResearchOutcome.BUDGET_EXHAUSTED,
                    obligations=list(
                        dict.fromkeys(
                            [
                                obligation
                                for issue in collection.execution_issues
                                for obligation in issue.recovery_obligations
                            ]
                            + ["Research model-call or provider resource budget exhausted."]
                        )
                    ),
                )
            if candidate_ids:
                scheduler.pending_candidate_report_ids = [candidate_ids[0]]
                scheduler.pending_candidate_source = "worker"
                scheduler.phase = SchedulerPhase.AUDITING
                persist_scheduler()
                continue

            if len(scheduler.decisions) >= settings.maximum_coordinator_decisions:
                # Existing admitted work may still finish, but no fresh coordinator
                # decisions are purchased after the explicit decision budget.
                continue
            await request_coordinator_decision(initial=False)
            raced_candidates = await apply_directed_cancellations()
            if raced_candidates and not scheduler.pending_candidate_report_ids:
                scheduler.pending_candidate_report_ids = [raced_candidates[0]]
                scheduler.pending_candidate_source = "worker"
                scheduler.phase = SchedulerPhase.AUDITING
                persist_scheduler()
    except CoordinatorContextBudgetExhausted as exc:
        mandatory_context_failure = exc.diagnostic == "MANDATORY_CONTEXT_TOO_LARGE"
        if not any(
            issue.event_kind == "coordinator_context_budget_exhausted"
            and issue.message == f"{type(exc).__name__}: {redact_text(str(exc))[:1000]}"
            for issue in scheduler.execution_issues
        ):
            record_execution_issue(
                event_kind="coordinator_context_budget_exhausted",
                exc=exc,
                category=FailureCategory.RESOURCE,
                extra_obligations=[
                    (
                        "MANDATORY_CONTEXT_TOO_LARGE: the exact prompt/claim plus the provider "
                        "instructions, output contract, and envelope do not fit. Optional "
                        "catalogs, graph views, assignments, issues, and continuity state were "
                        "already removed."
                        if mandatory_context_failure
                        else "Coordinator transport remained unavailable after headroom-adjusted "
                        "optional pruning. Resume will rebuild or submit the persisted smaller "
                        "generation without replaying the rejected request."
                    )
                ],
            )
        return await pause_retriable(
            obligations=[
                str(exc),
                "The complete research record remains durable; no acceptance gate was weakened.",
            ],
            phase=SchedulerPhase.RUNNING,
            pause_reason=exc.diagnostic,
            resume_action=(
                "Run `matek resume` to rebuild the pending coordinator activation from the "
                "same event cursor under its persisted reduced context budget."
            ),
        )
    except (_ResearchBudgetExhausted, BudgetExceeded):
        try:
            # A depleted new-call allowance must not erase work whose worker calls
            # were already reserved and launched. Drain that finite admitted set in
            # completion order, preserving every raw report and candidate marker.
            while True:
                launch_available()
                if not active:
                    break
                done_now, _ = await asyncio.wait(set(active), return_when=asyncio.FIRST_COMPLETED)
                await collect_tasks(done_now, requeue_cancelled=False)
        except BaseException:
            # Preserve the original budget outcome after best-effort cleanup.
            try:
                await pause_active(requeue_cancelled=False)
            except BaseException:
                pass
        candidate_obligations = (
            [
                "One or more complete-proof reports could not be independently audited "
                "within the remaining model-call budget."
            ]
            if scheduler.pending_candidate_report_ids
            or scheduler.deferred_candidate_report_ids
            or scheduler.active_candidate_attempt is not None
            else []
        )
        return await finish(
            ResearchOutcome.BUDGET_EXHAUSTED,
            obligations=list(
                dict.fromkeys(
                    [
                        *scheduler.repair_obligations,
                        *candidate_obligations,
                        "Research model-call or coordinator-decision budget exhausted.",
                    ]
                )
            ),
            audit_discovered_candidates=False,
        )
    except BaseException:
        try:
            await pause_active(requeue_cancelled=True)
        except BaseException:
            # Preserve the original failure after best-effort deterministic cleanup.
            pass
        persist_research_index()
        raise
