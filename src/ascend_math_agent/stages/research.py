from __future__ import annotations

import asyncio
import json
import re
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
from ..knowledge_graph import GraphPatch, KnowledgeGraph
from ..openai_client import ModelClient, ModelRequest, ModelResult, model_request_cache_key
from ..progress import Ascension, ProgressReporter, no_progress
from ..source_identifiers import tool_metadata_source_identifiers
from ..source_provenance import IdentifierVerifier, SourceEvidenceClaim, SourceVerificationReport
from .common import (
    ArtifactManifest,
    CallManifest,
    StageValidationError,
    atomic_write_json,
    atomic_write_text,
    build_artifact_manifest,
    ensure_stage_directory,
    project_resource,
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


class ResearchAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    approach_family: str
    task: str
    expected_output: str
    inputs: list[str] = Field(default_factory=list)
    target_node_ids: list[str] = Field(default_factory=list)
    stopping_condition: str = "Return concrete formal content or an exact obstruction."

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


class ResearchRoundPlan(BaseModel):
    """Legacy fixed-round plan retained for completed-run compatibility.

    New research runs use :class:`ResearchCoordinatorDecision`; keeping this model lets
    ASCEND read old ``result.json`` and schema artifacts without silently reinterpreting
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


class AssignmentLifecycle(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    RETIRED = "retired"
    CANCELLED = "cancelled"


class ResearchAssignmentState(BaseModel):
    """Durable lifecycle record for one logical worker assignment."""

    model_config = ConfigDict(extra="forbid")

    assignment: ResearchAssignment
    admitted_by_decision: int
    status: AssignmentLifecycle = AssignmentLifecycle.QUEUED
    launched: bool = False
    request_settings: ModelSettings | None = None
    request_key: str | None = None
    response_id: str | None = None
    report_path: str | None = None
    report_sha256: str | None = None
    completed_event_sequence: int | None = None
    graph_task_id: str | None = None
    graph_revision: str | None = None
    graph_context: dict[str, object] | None = None
    graph_patch_path: str | None = None
    graph_patch_sha256: str | None = None


class ResearchCoordinatorDecisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ResearchCoordinatorDecision
    response_id: str
    request_settings: ModelSettings
    request_path: str
    request_sha256: str
    request_key: str

    @field_validator("request_sha256", "request_key")
    @classmethod
    def request_hashes_are_sha256(cls, value: str) -> str:
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
    headroom_assignment_id: str | None = None
    headroom_worker_request_key: str | None = None

    @model_validator(mode="after")
    def validate_headroom_exchange(self) -> PendingCoordinatorRequest:
        if (self.headroom_assignment_id is None) != (self.headroom_worker_request_key is None):
            raise ValueError("coordinator headroom metadata must be complete")
        if self.headroom_worker_request_key is not None and not re.fullmatch(
            r"[0-9a-f]{64}", self.headroom_worker_request_key
        ):
            raise ValueError("coordinator headroom request identity is invalid")
        return self


class CandidateAttemptState(BaseModel):
    """Frozen candidate-gate transaction and its durable outcome."""

    model_config = ConfigDict(extra="forbid")

    attempt_name: str
    report_ids: list[str]
    source: Literal["worker", "coordinator"]
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
    verdict_sha256: str | None = None
    final_judge_response_id: str | None = None
    judge_call_reservation_key: str | None = None
    outcome_ready: bool = False
    outcome_gate: dict[str, object] | None = None
    outcome_obligations: list[str] = Field(default_factory=list)
    outcome_decision: FinalJudgeDecision | None = None
    outcome_failure_kind: Literal["scientific", "budget"] | None = None
    raced_candidate_report_ids: list[str] = Field(default_factory=list)


class SchedulerPhase(StrEnum):
    INITIALIZING = "initializing"
    RUNNING = "running"
    AUDITING = "auditing"
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
    model_calls: int = Field(default=0, ge=0)
    model_call_keys: list[str] = Field(default_factory=list)
    model_response_ids_by_call_key: dict[str, str] = Field(default_factory=dict)
    response_ids: list[str] = Field(default_factory=list)

    def assignment_record(self, assignment_id: str) -> ResearchAssignmentState | None:
        return next(
            (record for record in self.assignments if record.assignment.id == assignment_id),
            None,
        )


class ResearchWorkerReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignment_id: str
    status: WorkerStatus
    formal_results: list[str]
    proof_content: str
    exact_gap: str | None
    sources: list[SourceLedgerEntry]
    assumptions: list[str] = Field(default_factory=list)
    counterexamples: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    mechanism: str | None = None
    graph_patch: GraphPatch | None = None

    @model_validator(mode="after")
    def blocked_work_has_an_exact_gap(self) -> ResearchWorkerReport:
        if self.status == WorkerStatus.BLOCKED and not (self.exact_gap or "").strip():
            raise ValueError("a blocked worker must identify its exact missing statement")
        return self


class ResearchWorkerEvidence(BaseModel):
    """Atomic worker report/source transaction written before split artifacts."""

    model_config = ConfigDict(extra="forbid")

    assignment_id: str
    response_id: str
    report: ResearchWorkerReport
    source_verification: SourceVerificationReport


class ApproachRecord(BaseModel):
    family: str
    mechanism: str
    strongest_result: str = ""
    exact_gap: str = ""
    status: str = "active"
    assumptions: list[str] = Field(default_factory=list)
    counterexamples: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    assignment_ids: list[str] = Field(default_factory=list)


class ApproachRegistry(BaseModel):
    approaches: list[ApproachRecord] = Field(default_factory=list)

    def update(self, assignment: ResearchAssignment, report: ResearchWorkerReport) -> None:
        key = assignment.approach_family.casefold().strip()
        existing = next(
            (item for item in self.approaches if item.family.casefold().strip() == key), None
        )
        strongest = "\n\n".join(item for item in report.formal_results if item.strip())
        if not strongest:
            strongest = report.proof_content.strip()
        if existing is None:
            self.approaches.append(
                ApproachRecord(
                    family=assignment.approach_family,
                    mechanism=report.mechanism or assignment.task,
                    strongest_result=strongest,
                    exact_gap=report.exact_gap or "",
                    status=report.status.value,
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
    status: WorkerStatus
    mechanism: str
    formal_results: list[str]
    proof_content: str
    exact_gap: str | None
    assumptions: list[str]
    counterexamples: list[str]
    dependencies: list[str]


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


class ResearchWorkflowSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum_initial_assignments: int = Field(default=16, ge=4)
    maximum_concurrent_agents: int = Field(default=32, ge=1)
    maximum_pending_assignments: int = Field(default=32, ge=1)
    maximum_coordinator_decisions: int = Field(default=256, ge=1)
    maximum_model_calls: int | None = Field(default=None, ge=0)
    run_complexity_audit: bool | None = None

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
    artifacts: ArtifactManifest = Field(default_factory=ArtifactManifest)
    calls: CallManifest

    @property
    def accepted_for_manuscript(self) -> bool:
        return (
            self.outcome == ResearchOutcome.ACCEPTED
            and self.acceptance_gate is not None
            and self.acceptance_gate.accepted
        )


TModel = TypeVar("TModel", bound=BaseModel)


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
    graph_replay_dir: Path | None = None,
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
    worker_model = worker_settings or ModelSettings(reasoning_effort="xhigh")
    auditor_model = audit_settings or ModelSettings(reasoning_effort="xhigh")
    judge_model = final_judge_settings or coordinator_model

    destination = ensure_stage_directory(research_dir)
    coordinator_dir = ensure_stage_directory(destination / "coordinator")
    decisions_dir = ensure_stage_directory(coordinator_dir / "decisions")
    requests_dir = ensure_stage_directory(coordinator_dir / "requests")
    events_dir = ensure_stage_directory(destination / "events")
    assignments_dir = ensure_stage_directory(destination / "assignments")
    workers_dir = ensure_stage_directory(destination / "workers")
    worker_evidence_dir = ensure_stage_directory(destination / "worker-evidence")
    worker_sources_dir = ensure_stage_directory(destination / "source-verification")
    graph_patches_dir = ensure_stage_directory(destination / "graph-patches")
    candidate_dir = ensure_stage_directory(destination / "candidate")
    audits_dir = ensure_stage_directory(destination / "audits")
    scheduler_path = coordinator_dir / "state.json"
    mailbox_path = coordinator_dir / "mailbox.json"
    registry_path = destination / "registry.json"
    continuity_path = destination / "continuity.json"
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
        pending_input = json.dumps(
            pending_coordinator.request_payload,
            ensure_ascii=False,
            sort_keys=True,
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

    registry = ApproachRegistry()
    latest_continuity: ResearchContinuityState | None = None
    artifact_paths: dict[str, Path] = {}
    reports_by_id: dict[str, ResearchWorkerReport] = {}

    def resolved_artifact(relative: str) -> Path:
        path = (destination / relative).resolve()
        try:
            path.relative_to(destination)
        except ValueError as exc:
            raise StageValidationError(
                f"Research scheduler artifact escapes its stage: {relative}"
            ) from exc
        return path

    def assignment_input(record: ResearchAssignmentState) -> str:
        payload: dict[str, object] = {
            "compiled_prompt": compiled.compiled_prompt,
            "claim_contract": compiled.claim_contract.as_dict(),
            "assignment": record.assignment.model_dump(mode="json"),
            "admitted_by_coordinator_decision": record.admitted_by_decision,
        }
        if record.graph_context is not None:
            payload.update(
                {
                    "knowledge_graph_context": record.graph_context,
                    "graph_task_id": record.graph_task_id,
                    "base_graph_revision": record.graph_revision,
                    "graph_patch_contract": (
                        "Return graph_patch as a structured proposal. Do not edit the shared "
                        "vault. Distill mathematical results; do not copy raw transcripts."
                    ),
                }
            )
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

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
        if record.status == AssignmentLifecycle.RUNNING:
            # A process restart has no live coroutine. Reissuing the identical request
            # lets AccountingModelClient replay a completed call or safely resume work.
            record.status = AssignmentLifecycle.QUEUED
        if record.status == AssignmentLifecycle.COMPLETED and (
            record.request_key is None
            or record.response_id is None
            or record.report_path is None
            or record.report_sha256 is None
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
        report_path = resolved_artifact(record.report_path)
        if not report_path.is_file():
            raise StageValidationError(
                f"Completed assignment {record.assignment.id!r} has no durable report."
            )
        if record.report_sha256 and sha256_file(report_path) != record.report_sha256:
            raise StageValidationError(
                f"Research worker report changed after checkpoint: {record.assignment.id}"
            )
        report = ResearchWorkerReport.model_validate_json(read_regular_text(report_path))
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
        expected_request_relative = f"coordinator/requests/{expected_id:08d}.json"
        if decision_record.request_path != expected_request_relative:
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
        expected_request_key = tracker.request_key(
            instructions=coordinator_prompt,
            input_text=json.dumps(request_payload, ensure_ascii=False, sort_keys=True),
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
        scheduler.pending_event = None
        persist_scheduler()
        return sequence

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

    def worker_input_for(record: ResearchAssignmentState) -> str:
        return assignment_input(record)

    def reserve_worker_request(record: ResearchAssignmentState) -> None:
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
                    status=report.status,
                    mechanism=report.mechanism or record.assignment.task,
                    formal_results=report.formal_results,
                    proof_content=report.proof_content,
                    exact_gap=report.exact_gap,
                    assumptions=report.assumptions,
                    counterexamples=report.counterexamples,
                    dependencies=report.dependencies,
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

    async def finish(
        outcome: ResearchOutcome,
        *,
        obligations: list[str] | None = None,
        strongest_result: str = "",
        acceptance_gate: ResearchAcceptanceGate | None = None,
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
        elif acceptance_gate is not None:
            raise StageValidationError("Non-accepted research cannot retain an acceptance gate.")
        if scheduler.final_outcome is None:
            scheduler.final_outcome = outcome
            scheduler.final_obligations = list(dict.fromkeys(obligations or []))
            scheduler.final_strongest_result = strongest_result
            scheduler.final_acceptance_gate = (
                acceptance_gate.model_dump(mode="json") if acceptance_gate is not None else None
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
            artifacts=ArtifactManifest(),
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
        return any(
            event.get("kind") in {"worker_report_accepted", "candidate_audit_failed"}
            for event in recent_events()
        )

    def coordinator_stop_outcome() -> ResearchOutcome:
        if scheduler.stop_category == "budget":
            return ResearchOutcome.BUDGET_EXHAUSTED
        if scheduler.stop_category == "refuted":
            return ResearchOutcome.REJECTED
        return ResearchOutcome.PARTIAL

    def initial_assignment_target() -> int:
        """Scale the default bootstrap to the explicit call allowance, down to four."""

        if tracker.maximum_calls is None:
            return settings.minimum_initial_assignments
        remaining_after_coordinator = max(tracker.maximum_calls - tracker.calls - 1, 0)
        return min(settings.minimum_initial_assignments, remaining_after_coordinator)

    async def request_coordinator_decision(*, initial: bool) -> ResearchCoordinatorDecision:
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
            "compiled_prompt": compiled.compiled_prompt,
            "claim_contract": compiled.claim_contract.as_dict(),
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
            "maximum_concurrent_workers": settings.maximum_concurrent_agents,
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
                payload["knowledge_graph_memory"] = replay_memory
            else:
                payload["knowledge_graph_memory"] = knowledge_graph.coordinator_memory(
                    graph_problem_id
                )
        decision_model_settings = coordinator_model
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
        else:
            request_path = requests_dir / f"{decision_id:08d}.json"
            headroom_worker_request_key: str | None = None
            if headroom_record is not None:
                headroom_worker_request_key = headroom_record.request_key
                if headroom_worker_request_key is None:
                    raise StageValidationError(
                        "Coordinator headroom assignment has no refundable request."
                    )
                release_unlaunched_worker_request(headroom_record)
            scheduler.pending_coordinator_request = PendingCoordinatorRequest(
                decision_id=decision_id,
                after_event_sequence=event_sequence,
                initial=initial,
                request_settings=decision_model_settings.model_copy(deep=True),
                request_path=request_path.relative_to(destination).as_posix(),
                request_sha256=sha256_json(payload),
                request_payload=payload,
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
        result = await generate_model(
            instructions=coordinator_prompt,
            input_text=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            model_settings=decision_model_settings,
            output_type=ResearchCoordinatorDecision,
            selected_client=coordinator_client,
        )
        coordinator_request_key = tracker.request_key(
            instructions=coordinator_prompt,
            input_text=json.dumps(payload, ensure_ascii=False, sort_keys=True),
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
                graph_task_id=graph_tasks.get(assignment.id),
                graph_revision=graph_revision,
                graph_context=cast(
                    dict[str, object] | None,
                    graph_contexts.get(assignment.id),
                ),
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
            )
        )
        scheduler.coordinator_ack_event_sequence = event_sequence
        if decision.candidate_packaging_recommended:
            scheduler.pending_candidate_report_ids = list(
                dict.fromkeys(decision.candidate_report_ids)
            )
            scheduler.pending_candidate_source = "coordinator"
            scheduler.phase = SchedulerPhase.AUDITING
        scheduler.stop_reason = decision.stop_reason if decision.stop_recommended else None
        scheduler.stop_category = decision.stop_category if decision.stop_recommended else None
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
        persist_research_index()
        return decision

    async def run_worker(
        record: ResearchAssignmentState,
    ) -> tuple[ResearchWorkerReport, str]:
        assignment = record.assignment
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
            selected_client=worker_client,
        )
        if result.parsed.assignment_id != assignment.id:
            raise StageValidationError(
                f"Worker report {result.parsed.assignment_id!r} does not match "
                f"assignment {assignment.id!r}."
            )
        evidence_path = worker_evidence_dir / f"{assignment.id}.json"
        if evidence_path.is_file():
            evidence = ResearchWorkerEvidence.model_validate_json(read_regular_text(evidence_path))
            if (
                evidence.assignment_id != assignment.id
                or evidence.response_id != result.response_id
            ):
                raise StageValidationError(
                    f"Frozen worker evidence does not match assignment {assignment.id!r}."
                )
            parsed = evidence.report
            source_verification = evidence.source_verification
        else:
            parsed = result.parsed
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
                    parsed.assumptions.append(
                        f"Source {source.source_id} could not be independently verified."
                    )
            parsed.assumptions = list(dict.fromkeys(parsed.assumptions))
            evidence = ResearchWorkerEvidence(
                assignment_id=assignment.id,
                response_id=result.response_id,
                report=parsed,
                source_verification=source_verification,
            )
            _atomic_write_immutable_json(evidence_path, evidence)
        report_path = _atomic_write_immutable_json(workers_dir / f"{assignment.id}.json", parsed)
        source_path = _atomic_write_immutable_json(
            worker_sources_dir / f"{assignment.id}.json", source_verification
        )
        artifact_paths[f"worker_{assignment.id}"] = report_path
        artifact_paths[f"worker_{assignment.id}_evidence"] = evidence_path
        artifact_paths[f"worker_{assignment.id}_sources"] = source_path
        return parsed, result.response_id

    def accept_worker_result(
        record: ResearchAssignmentState,
        report: ResearchWorkerReport,
        response_id: str,
    ) -> int:
        report_path = workers_dir / f"{record.assignment.id}.json"
        if (
            record.request_key is None
            or tracker.response_ids_by_call_key.get(record.request_key) != response_id
        ):
            raise StageValidationError(
                f"Worker response for {record.assignment.id!r} is not bound to its request."
            )
        graph_patch_record: Path | None = None
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
                graph_merge = knowledge_graph.integrate_worker_report(
                    problem_id=graph_problem_id,
                    run_id=run_id,
                    assignment=record.assignment.model_dump(mode="json"),
                    task_id=record.graph_task_id,
                    report=report.model_dump(mode="json", exclude={"graph_patch"}),
                    proposed_patch=report.graph_patch,
                    source_artifact=(
                        f".ascend/runs/{run_id}/research/workers/{record.assignment.id}.json"
                    ),
                    operation_id=f"worker-report:{run_id}:{record.assignment.id}",
                )
                graph_patch_record = _atomic_write_immutable_json(
                    graph_patches_dir / f"{record.assignment.id}.json",
                    {
                        "assignment_id": record.assignment.id,
                        "task_id": record.graph_task_id,
                        "proposed_patch": (
                            report.graph_patch.model_dump(mode="json")
                            if report.graph_patch is not None
                            else None
                        ),
                        "merge_result": graph_merge.model_dump(mode="json"),
                    },
                )
            record.graph_patch_path = graph_patch_record.relative_to(destination).as_posix()
            record.graph_patch_sha256 = sha256_file(graph_patch_record)
            artifact_paths[f"worker_{record.assignment.id}_graph_patch"] = graph_patch_record
        record.status = AssignmentLifecycle.COMPLETED
        record.response_id = response_id
        record.report_path = report_path.relative_to(destination).as_posix()
        record.report_sha256 = sha256_file(report_path)
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
                worker_evidence_dir / f"{record.assignment.id}.json",
                worker_sources_dir / f"{record.assignment.id}.json",
                *([graph_patch_record] if graph_patch_record is not None else []),
            ],
            detail=[report.status.value],
        )
        if published_sequence != event_sequence:
            raise StageValidationError("Research event cursor changed during report commit.")
        persist_research_index()
        return event_sequence

    async def evaluate_candidate(
        report_ids: list[str],
        *,
        attempt_name: str,
    ) -> tuple[
        ResearchAcceptanceGate | None,
        list[str],
        FinalJudgeDecision | None,
        Literal["scientific", "budget"] | None,
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
        audit_inputs = {
            name: json.dumps(
                {
                    "audit_role": name,
                    "claim_contract": compiled.claim_contract.as_dict(),
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
            return name, result.parsed, result.response_id

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

        audit_tasks: list[asyncio.Task[tuple[str, AuditVerdict, str]]] = []
        audit_budget_failure = False
        if missing_audits:
            try:
                async with asyncio.TaskGroup() as audit_group:
                    for name in missing_audits:
                        audit_tasks.append(audit_group.create_task(run_audit(name)))
            except* (BudgetExceeded, _ResearchBudgetExhausted):
                audit_budget_failure = True
        for task in audit_tasks:
            if task.cancelled() or task.exception() is not None:
                continue
            name, audit, response_id = task.result()
            current_audits[name] = audit
            attempt.audit_response_ids[name] = response_id
            audit_path = _atomic_write_immutable_json(audit_attempt_dir / f"{name}.json", audit)
            attempt.audit_sha256[name] = sha256_file(audit_path)
        if audit_tasks:
            persist_scheduler()
        if audit_budget_failure:
            return (
                None,
                ["A configured run-wide budget was exhausted during mandatory audits."],
                None,
                "budget",
            )

        for name, audit in current_audits.items():
            artifact_paths[f"audit_{attempt_name}_{name}"] = audit_attempt_dir / f"{name}.json"
            artifact_paths[f"audit_{name}"] = atomic_write_json(audits_dir / f"{name}.json", audit)

        judge_input = json.dumps(
            {
                "claim_contract": compiled.claim_contract.as_dict(),
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
            if len(active) >= settings.maximum_concurrent_agents:
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
    ) -> list[str]:
        candidate_ids: list[str] = []
        first_error: BaseException | None = None
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
                if record.status != AssignmentLifecycle.RETIRED:
                    record.status = AssignmentLifecycle.QUEUED
                if first_error is None:
                    first_error = result
                continue
            if first_error is None:
                first_error = StageValidationError(
                    "Research worker returned an invalid task result."
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
        if first_error is not None:
            raise first_error
        return candidate_ids

    async def pause_active(*, requeue_cancelled: bool) -> list[str]:
        tasks = set(active)
        for task in tasks:
            task.cancel()
        return await collect_tasks(tasks, requeue_cancelled=requeue_cancelled)

    async def apply_directed_cancellations() -> list[str]:
        tasks = {
            task for task, record in active.items() if record.status == AssignmentLifecycle.RETIRED
        }
        for task in tasks:
            task.cancel()
        return await collect_tasks(tasks, requeue_cancelled=False) if tasks else []

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
            package_payload: dict[str, object] = {
                "claim_contract": compiled.claim_contract.as_dict(),
                "approach_registry": registry.model_dump(mode="json"),
                "visible_worker_reports": [
                    reports_by_id[assignment_id].model_dump(mode="json")
                    for assignment_id in report_ids
                ],
                "candidate_trigger_assignment_ids": report_ids,
                "constraint": (
                    "Package only the proof supported by the named reports. Expose every "
                    "unresolved step; do not substitute an unrelated route."
                ),
            }
            package_input_path = _atomic_write_immutable_json(
                attempt_dir / "input.json", package_payload
            )
            artifact_paths[f"candidate_attempt_{attempt_name}_input"] = package_input_path
            attempt = CandidateAttemptState(
                attempt_name=attempt_name,
                report_ids=report_ids,
                source=source,
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
                        raced = await collect_tasks(
                            completed_workers,
                            requeue_cancelled=True,
                        )
                        attempt.raced_candidate_report_ids = list(
                            dict.fromkeys([*attempt.raced_candidate_report_ids, *raced])
                        )
                        persist_research_index()
                    if evaluation_task in completed:
                        gate, obligations, decision, failure_kind = await evaluation_task
                        break
            except BaseException:
                evaluation_task.cancel()
                await asyncio.gather(evaluation_task, return_exceptions=True)
                raise

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
            raced = await collect_tasks(
                done_after_outcome,
                requeue_cancelled=True,
            )
            attempt.raced_candidate_report_ids = list(
                dict.fromkeys([*attempt.raced_candidate_report_ids, *raced])
            )
            persist_research_index()
        sync_tracker()
        if gate is not None:
            accepted_attempt_path = candidate_dir / "attempts" / attempt_name
            accepted_verdict_path = accepted_attempt_path / "verdict.json"
            accepted_evidence = [
                accepted_attempt_path / "evidence.json",
                accepted_attempt_path / "package.json",
                accepted_attempt_path / "source_verification.json",
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
            scheduler.phase = SchedulerPhase.COMPLETE
            append_event(
                "candidate_audit_passed",
                response_id=gate.final_judge_response_id,
                artifact=accepted_verdict_path,
                related_artifacts=accepted_evidence,
                detail=report_ids,
            )
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

        initial_count = len(scheduler.decisions[0].decision.assignments)
        progress(
            Ascension.MANAGE_RESEARCH_POOL,
            "Managing adaptive research pool: "
            f"{initial_count} initial assignments, up to "
            f"{settings.maximum_concurrent_agents} active agents.",
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

            if scheduler.pending_candidate_report_ids:
                candidate_result = await audit_pending_candidate()
                if candidate_result is not None:
                    return candidate_result
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

            candidate_ids = await collect_tasks(
                done_now,
                requeue_cancelled=True,
            )
            persist_research_index()
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
