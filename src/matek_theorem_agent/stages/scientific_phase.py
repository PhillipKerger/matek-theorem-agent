"""Durable scientific-phase orchestration over the canonical proof frontier."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .common import StageValidationError, atomic_write_json, read_regular_text

_STABLE_ID = re.compile(r"\A[A-Z]{3}-[A-Z0-9]{8,64}\Z")
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


class _PhaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScientificPhase(StrEnum):
    EXPLORE = "explore"
    CONSOLIDATE = "consolidate"
    BOTTLENECK = "bottleneck"
    ADVERSARIAL_AUDIT = "adversarial_audit"
    SYNTHESIZE = "synthesize"


class ScientificRole(StrEnum):
    EXPLORER = "explorer"
    CONSOLIDATOR = "consolidator"
    PROVER = "independent_prover"
    FALSIFIER = "hostile_falsifier"
    COMPUTATION = "small_case_computation"
    TRANSFER_AUDITOR = "transfer_auditor"
    SYNTHESIZER = "synthesizer"


BOTTLENECK_COMPLEMENTARY_ROLES: tuple[ScientificRole, ...] = (
    ScientificRole.PROVER,
    ScientificRole.FALSIFIER,
    ScientificRole.COMPUTATION,
    ScientificRole.TRANSFER_AUDITOR,
    ScientificRole.SYNTHESIZER,
)

ADVERSARIAL_COMPLEMENTARY_ROLES: tuple[ScientificRole, ...] = (
    ScientificRole.FALSIFIER,
    ScientificRole.TRANSFER_AUDITOR,
)


class DuplicateDisposition(StrEnum):
    LAUNCH = "launch"
    REDIRECT = "redirect"
    MERGE = "merge"


class PhaseTransitionReason(StrEnum):
    PLATEAU = "plateau"
    FRONTIER_CONSOLIDATED = "frontier_consolidated"
    BOTTLENECK_PROGRESS = "bottleneck_progress"
    ADVERSARIAL_PASS = "adversarial_pass"
    ADVERSARIAL_FAILURE = "adversarial_failure"
    SYNTHESIS_GAP = "synthesis_gap"
    SYNTHESIS_ACCEPTED = "synthesis_accepted"


def _stable_ids(values: list[str]) -> list[str]:
    normalized = [value.strip().upper() for value in values]
    if any(not _STABLE_ID.fullmatch(value) for value in normalized):
        raise ValueError("scientific frontier references must be stable graph IDs")
    return list(dict.fromkeys(normalized))


def _sha256_values(values: list[str]) -> list[str]:
    normalized = [value.strip().casefold() for value in values]
    if any(not _SHA256.fullmatch(value) for value in normalized):
        raise ValueError("scientific progress identities must be SHA-256 digests")
    return sorted(set(normalized))


def normalize_mechanism(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def semantic_similarity(first: str, second: str) -> float:
    """Return deterministic token-set similarity for pre-launch duplicate screening."""

    first_tokens = set(normalize_mechanism(first).split())
    second_tokens = set(normalize_mechanism(second).split())
    if not first_tokens and not second_tokens:
        return 1.0
    if not first_tokens or not second_tokens:
        return 0.0
    return len(first_tokens & second_tokens) / len(first_tokens | second_tokens)


class ScientificPhasePolicy(_PhaseModel):
    no_audited_progress_assignments: int = Field(default=8, ge=1)
    unchanged_cut_snapshots: int = Field(default=4, ge=1)
    repeated_gap_threshold: int = Field(default=3, ge=2)
    similarity_threshold: float = Field(default=0.86, ge=0.0, le=1.0)
    blocked_or_refuted_ratio: float = Field(default=0.60, ge=0.0, le=1.0)
    bottleneck_maximum_size: int = Field(default=3, ge=1)
    bottleneck_attempts_before_audit: int = Field(default=5, ge=1)
    explore_concurrency: int = Field(default=8, ge=1)
    consolidate_concurrency: int = Field(default=4, ge=1)
    bottleneck_concurrency: int = Field(default=3, ge=1)
    adversarial_concurrency: int = Field(default=2, ge=1)
    synthesize_concurrency: Literal[1] = 1

    def concurrency_for(self, phase: ScientificPhase) -> int:
        return {
            ScientificPhase.EXPLORE: self.explore_concurrency,
            ScientificPhase.CONSOLIDATE: self.consolidate_concurrency,
            ScientificPhase.BOTTLENECK: self.bottleneck_concurrency,
            ScientificPhase.ADVERSARIAL_AUDIT: self.adversarial_concurrency,
            ScientificPhase.SYNTHESIZE: self.synthesize_concurrency,
        }[phase]


class ScientificProgressSnapshot(_PhaseModel):
    sequence: int = Field(ge=1)
    ledger_revision: str
    completed_assignment_count: int = Field(ge=0)
    new_audit_passed_count: int = Field(default=0, ge=0)
    audited_claim_hashes: list[str] = Field(default_factory=list)
    minimal_open_cut_ids: list[str] = Field(default_factory=list)
    normalized_exact_gaps: list[str] = Field(default_factory=list)
    admitted_result_hashes: list[str] = Field(default_factory=list)
    blocked_count: int = Field(default=0, ge=0)
    refuted_count: int = Field(default=0, ge=0)
    recent_outcome_count: int = Field(default=0, ge=0)
    maximum_assignment_similarity: float = Field(default=0.0, ge=0.0, le=1.0)
    adversarial_audit_passed: bool = False
    adversarial_audit_failed: bool = False
    synthesis_succeeded: bool = False
    synthesis_exact_gap: str | None = None

    @field_validator("ledger_revision")
    @classmethod
    def revision_is_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("scientific progress requires a ledger revision")
        return normalized

    @field_validator("audited_claim_hashes", "admitted_result_hashes")
    @classmethod
    def hashes_are_sha256(cls, values: list[str]) -> list[str]:
        return _sha256_values(values)

    @field_validator("minimal_open_cut_ids")
    @classmethod
    def cut_ids_are_stable(cls, values: list[str]) -> list[str]:
        return sorted(_stable_ids(values))

    @field_validator("normalized_exact_gaps")
    @classmethod
    def gaps_are_normalized(cls, values: list[str]) -> list[str]:
        normalized = [normalize_mechanism(value) for value in values]
        return sorted(value for value in normalized if value)

    @field_validator("synthesis_exact_gap")
    @classmethod
    def synthesis_gap_is_normalized(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_mechanism(value)
        return normalized or None

    @model_validator(mode="after")
    def outcome_counts_are_consistent(self) -> ScientificProgressSnapshot:
        if self.blocked_count + self.refuted_count > self.recent_outcome_count:
            raise ValueError("blocked and refuted counts exceed the recent outcome window")
        if self.adversarial_audit_passed and self.adversarial_audit_failed:
            raise ValueError("an adversarial audit cannot both pass and fail")
        if self.synthesis_succeeded and self.synthesis_exact_gap is not None:
            raise ValueError("successful synthesis cannot retain an exact gap")
        return self

    @property
    def cut_hash(self) -> str:
        return hashlib.sha256("\0".join(self.minimal_open_cut_ids).encode()).hexdigest()

    @property
    def blocked_or_refuted_fraction(self) -> float:
        if self.recent_outcome_count == 0:
            return 0.0
        return (self.blocked_count + self.refuted_count) / self.recent_outcome_count


class ScientificTaskPlan(_PhaseModel):
    assignment_id: str
    phase: ScientificPhase
    phase_epoch: int = Field(default=0, ge=0)
    role: ScientificRole
    target_obligation_ids: list[str] = Field(default_factory=list)
    target_obligation_versions: dict[str, str] = Field(default_factory=dict)
    mechanism: str
    mechanism_delta: str = ""
    audited_premise_ids: list[str] = Field(default_factory=list)

    @field_validator("assignment_id", "mechanism")
    @classmethod
    def required_text_is_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("scientific assignments require identity and mechanism")
        return normalized

    @field_validator("mechanism_delta")
    @classmethod
    def delta_is_normalized(cls, value: str) -> str:
        return value.strip()

    @field_validator("target_obligation_ids", "audited_premise_ids")
    @classmethod
    def referenced_ids_are_stable(cls, values: list[str]) -> list[str]:
        return sorted(_stable_ids(values))

    @field_validator("target_obligation_versions")
    @classmethod
    def obligation_versions_are_stable(cls, values: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for raw_id, raw_version in values.items():
            obligation_id = _stable_ids([raw_id])[0]
            version = raw_version.strip().casefold()
            if not _SHA256.fullmatch(version):
                raise ValueError("target obligation versions must be SHA-256 digests")
            normalized[obligation_id] = version
        return dict(sorted(normalized.items()))

    @model_validator(mode="after")
    def obligation_versions_cover_targets_when_present(self) -> ScientificTaskPlan:
        if self.target_obligation_versions and set(self.target_obligation_versions) != set(
            self.target_obligation_ids
        ):
            raise ValueError("target obligation versions must exactly cover target_obligation_ids")
        return self

    @property
    def signature(self) -> str:
        material = {
            "phase": self.phase.value,
            "phase_epoch": self.phase_epoch,
            "role": self.role.value,
            "targets": self.target_obligation_ids,
            "target_versions": self.target_obligation_versions,
            "mechanism": normalize_mechanism(self.mechanism),
        }
        payload = json.dumps(material, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


class AssignmentDisposition(_PhaseModel):
    disposition: DuplicateDisposition
    assignment_id: str
    matched_assignment_id: str | None = None
    similarity: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str


class PhaseTransition(_PhaseModel):
    sequence: int = Field(ge=1)
    previous_phase: ScientificPhase
    next_phase: ScientificPhase
    reason: PhaseTransitionReason
    ledger_revision: str


class ScientificPhaseState(_PhaseModel):
    schema_version: Literal[1] = 1
    phase: ScientificPhase = ScientificPhase.EXPLORE
    phase_epoch: int = Field(default=0, ge=0)
    snapshots: list[ScientificProgressSnapshot] = Field(default_factory=list)
    transitions: list[PhaseTransition] = Field(default_factory=list)
    assignments_without_audited_progress: int = Field(default=0, ge=0)
    unchanged_cut_snapshots: int = Field(default=0, ge=0)
    bottleneck_attempts: int = Field(default=0, ge=0)
    completed_assignment_count: int = Field(default=0, ge=0)
    progress_counted_assignment_ids: list[str] = Field(default_factory=list)
    launched_assignments: list[ScientificTaskPlan] = Field(default_factory=list)
    assignment_dispositions: list[AssignmentDisposition] = Field(default_factory=list)
    accepted_synthesis: bool = False

    @field_validator("progress_counted_assignment_ids")
    @classmethod
    def counted_assignment_ids_are_unique(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("counted scientific assignment IDs must not be blank")
        if len(normalized) != len(set(normalized)):
            raise ValueError("counted scientific assignment IDs must be unique")
        return normalized

    @model_validator(mode="after")
    def snapshots_are_monotone(self) -> ScientificPhaseState:
        sequences = [snapshot.sequence for snapshot in self.snapshots]
        if sequences != sorted(set(sequences)):
            raise ValueError("scientific progress snapshot sequences must be unique and monotone")
        counts = [snapshot.completed_assignment_count for snapshot in self.snapshots]
        if counts != sorted(counts):
            raise ValueError("completed scientific assignment counts cannot decrease")
        if self.snapshots and self.completed_assignment_count != counts[-1]:
            raise ValueError("phase state completion count must match its latest snapshot")
        if any(plan.phase_epoch > self.phase_epoch for plan in self.launched_assignments):
            raise ValueError("scientific assignments cannot originate in a future phase epoch")
        if len(self.progress_counted_assignment_ids) > self.completed_assignment_count:
            raise ValueError("counted scientific assignment IDs exceed the completion count")
        return self


def focused_frontier_obligation(
    state: ScientificPhaseState,
    active_cut_ids: list[str],
) -> str | None:
    """Choose one durable cut obligation for a complementary attack portfolio."""

    active = _stable_ids(active_cut_ids)
    if not active:
        return None
    active_set = set(active)
    for prior in reversed(state.launched_assignments):
        if prior.phase not in {
            ScientificPhase.BOTTLENECK,
            ScientificPhase.ADVERSARIAL_AUDIT,
        }:
            continue
        if len(prior.target_obligation_ids) != 1:
            continue
        target = prior.target_obligation_ids[0]
        if target in active_set:
            return target
    return sorted(active)[0]


def next_complementary_role(
    state: ScientificPhaseState,
    *,
    phase: ScientificPhase,
    target_obligation_id: str,
) -> ScientificRole:
    """Rotate durable complementary roles around the same exact obligation."""

    target = _stable_ids([target_obligation_id])[0]
    if phase is ScientificPhase.BOTTLENECK:
        portfolio = BOTTLENECK_COMPLEMENTARY_ROLES
    elif phase is ScientificPhase.ADVERSARIAL_AUDIT:
        portfolio = ADVERSARIAL_COMPLEMENTARY_ROLES
    else:
        raise StageValidationError(
            "complementary frontier roles apply only in bottleneck or adversarial phases"
        )
    prior_count = sum(
        plan.phase is phase and plan.target_obligation_ids == [target]
        for plan in state.launched_assignments
    )
    return portfolio[prior_count % len(portfolio)]


def validate_task_contract(
    plan: ScientificTaskPlan,
    *,
    active_phase: ScientificPhase,
    active_cut_ids: list[str],
    active_cut_versions: dict[str, str] | None = None,
    active_phase_epoch: int | None = None,
) -> None:
    if plan.phase is not active_phase:
        raise StageValidationError(
            f"assignment {plan.assignment_id} targets {plan.phase.value}, not active phase "
            f"{active_phase.value}"
        )
    if active_phase_epoch is not None and plan.phase_epoch != active_phase_epoch:
        raise StageValidationError(
            f"assignment {plan.assignment_id} targets phase epoch {plan.phase_epoch}, not active "
            f"epoch {active_phase_epoch}"
        )
    active_cut = set(_stable_ids(active_cut_ids))
    targets = set(plan.target_obligation_ids)
    allowed_roles = {
        ScientificPhase.EXPLORE: {ScientificRole.EXPLORER},
        ScientificPhase.CONSOLIDATE: {ScientificRole.CONSOLIDATOR},
        ScientificPhase.BOTTLENECK: {
            ScientificRole.PROVER,
            ScientificRole.FALSIFIER,
            ScientificRole.COMPUTATION,
            ScientificRole.TRANSFER_AUDITOR,
            ScientificRole.SYNTHESIZER,
        },
        ScientificPhase.ADVERSARIAL_AUDIT: {
            ScientificRole.FALSIFIER,
            ScientificRole.TRANSFER_AUDITOR,
        },
        ScientificPhase.SYNTHESIZE: {ScientificRole.SYNTHESIZER},
    }[active_phase]
    if plan.role not in allowed_roles:
        raise StageValidationError(f"role {plan.role.value} is invalid during {active_phase.value}")
    if active_phase in {ScientificPhase.BOTTLENECK, ScientificPhase.ADVERSARIAL_AUDIT}:
        if not targets:
            raise StageValidationError("frontier attacks require exact target obligation IDs")
        if not targets.issubset(active_cut):
            raise StageValidationError("assignment targets an obligation outside the active cut")
        if not plan.mechanism_delta:
            raise StageValidationError("frontier attacks require a mechanism delta")
        if active_cut_versions is not None:
            expected_versions: dict[str, str] = {}
            for target_id in sorted(targets):
                version = active_cut_versions.get(target_id)
                if version is None:
                    raise StageValidationError(
                        "frontier attack target has no current canonical obligation version"
                    )
                expected_versions[target_id] = version
            if plan.target_obligation_versions != expected_versions:
                raise StageValidationError(
                    "frontier attack target obligation version is missing or stale"
                )
    if active_phase is ScientificPhase.SYNTHESIZE and not plan.audited_premise_ids:
        raise StageValidationError("synthesis assignments require audited premise IDs")


def screen_duplicate_assignment(
    state: ScientificPhaseState,
    plan: ScientificTaskPlan,
    *,
    policy: ScientificPhasePolicy,
) -> AssignmentDisposition:
    for prior in state.launched_assignments:
        if prior.phase_epoch != state.phase_epoch:
            continue
        if prior.signature == plan.signature:
            return AssignmentDisposition(
                disposition=DuplicateDisposition.MERGE,
                assignment_id=plan.assignment_id,
                matched_assignment_id=prior.assignment_id,
                similarity=1.0,
                reason="Exact scientific assignment signature already launched.",
            )
    closest: tuple[ScientificTaskPlan, float] | None = None
    for prior in state.launched_assignments:
        if (
            prior.phase_epoch != state.phase_epoch
            or prior.phase is not plan.phase
            or prior.role is not plan.role
            or prior.target_obligation_ids != plan.target_obligation_ids
            or prior.target_obligation_versions != plan.target_obligation_versions
        ):
            continue
        similarity = semantic_similarity(prior.mechanism, plan.mechanism)
        if closest is None or similarity > closest[1]:
            closest = (prior, similarity)
    if closest is not None and closest[1] >= policy.similarity_threshold:
        return AssignmentDisposition(
            disposition=DuplicateDisposition.REDIRECT,
            assignment_id=plan.assignment_id,
            matched_assignment_id=closest[0].assignment_id,
            similarity=closest[1],
            reason="Near-duplicate mechanism should extend the existing branch.",
        )
    return AssignmentDisposition(
        disposition=DuplicateDisposition.LAUNCH,
        assignment_id=plan.assignment_id,
        similarity=closest[1] if closest is not None else 0.0,
        reason="Assignment contributes a distinct frontier attack.",
    )


def admit_assignment(
    state: ScientificPhaseState,
    plan: ScientificTaskPlan,
    *,
    active_cut_ids: list[str],
    active_cut_versions: dict[str, str] | None = None,
    policy: ScientificPhasePolicy,
) -> tuple[ScientificPhaseState, AssignmentDisposition]:
    validate_task_contract(
        plan,
        active_phase=state.phase,
        active_cut_ids=active_cut_ids,
        active_cut_versions=active_cut_versions,
        active_phase_epoch=state.phase_epoch,
    )
    disposition = screen_duplicate_assignment(state, plan, policy=policy)
    update: dict[str, object] = {
        "assignment_dispositions": [*state.assignment_dispositions, disposition],
    }
    if disposition.disposition is DuplicateDisposition.LAUNCH:
        update["launched_assignments"] = [*state.launched_assignments, plan]
    return state.model_copy(update=update), disposition


def _repeated_gap(snapshot: ScientificProgressSnapshot, threshold: int) -> bool:
    return any(count >= threshold for count in Counter(snapshot.normalized_exact_gaps).values())


def record_scientific_progress(
    state: ScientificPhaseState,
    snapshot: ScientificProgressSnapshot,
    *,
    policy: ScientificPhasePolicy,
) -> ScientificPhaseState:
    """Advance phase state solely from persisted, deterministic progress signals."""

    if state.snapshots and snapshot.sequence <= state.snapshots[-1].sequence:
        raise StageValidationError("scientific progress sequence must increase")
    if snapshot.completed_assignment_count < state.completed_assignment_count:
        raise StageValidationError("scientific completed assignment count cannot decrease")
    completed_delta = snapshot.completed_assignment_count - state.completed_assignment_count
    no_progress = (
        0
        if snapshot.new_audit_passed_count > 0
        else state.assignments_without_audited_progress + completed_delta
    )
    prior_cut = state.snapshots[-1].cut_hash if state.snapshots else None
    unchanged_cut = (
        state.unchanged_cut_snapshots + 1
        if prior_cut is not None and prior_cut == snapshot.cut_hash
        else 0
    )
    phase = state.phase
    reason: PhaseTransitionReason | None = None
    bottleneck_attempts = state.bottleneck_attempts
    accepted_synthesis = state.accepted_synthesis

    plateau = (
        no_progress >= policy.no_audited_progress_assignments
        or unchanged_cut >= policy.unchanged_cut_snapshots
        or _repeated_gap(snapshot, policy.repeated_gap_threshold)
        or snapshot.maximum_assignment_similarity >= policy.similarity_threshold
        or snapshot.blocked_or_refuted_fraction >= policy.blocked_or_refuted_ratio
    )
    cut_is_focused = 0 < len(snapshot.minimal_open_cut_ids) <= policy.bottleneck_maximum_size

    if phase is ScientificPhase.EXPLORE and plateau:
        phase = ScientificPhase.CONSOLIDATE
        reason = PhaseTransitionReason.PLATEAU
    elif phase is ScientificPhase.CONSOLIDATE and (cut_is_focused or plateau):
        phase = ScientificPhase.BOTTLENECK
        reason = PhaseTransitionReason.FRONTIER_CONSOLIDATED
        bottleneck_attempts = 0
    elif phase is ScientificPhase.BOTTLENECK:
        bottleneck_attempts += completed_delta
        if snapshot.new_audit_passed_count > 0 or (
            bottleneck_attempts >= policy.bottleneck_attempts_before_audit
        ):
            phase = ScientificPhase.ADVERSARIAL_AUDIT
            reason = PhaseTransitionReason.BOTTLENECK_PROGRESS
    elif phase is ScientificPhase.ADVERSARIAL_AUDIT:
        if snapshot.adversarial_audit_failed:
            phase = ScientificPhase.BOTTLENECK
            reason = PhaseTransitionReason.ADVERSARIAL_FAILURE
            bottleneck_attempts = 0
        elif snapshot.adversarial_audit_passed:
            phase = ScientificPhase.SYNTHESIZE
            reason = PhaseTransitionReason.ADVERSARIAL_PASS
    elif phase is ScientificPhase.SYNTHESIZE:
        if snapshot.synthesis_succeeded:
            reason = PhaseTransitionReason.SYNTHESIS_ACCEPTED
            accepted_synthesis = True
        elif snapshot.synthesis_exact_gap is not None:
            phase = ScientificPhase.BOTTLENECK
            reason = PhaseTransitionReason.SYNTHESIS_GAP
            bottleneck_attempts = 0

    transitions = list(state.transitions)
    if reason is not None:
        transitions.append(
            PhaseTransition(
                sequence=snapshot.sequence,
                previous_phase=state.phase,
                next_phase=phase,
                reason=reason,
                ledger_revision=snapshot.ledger_revision,
            )
        )
    return state.model_copy(
        update={
            "phase": phase,
            "phase_epoch": state.phase_epoch + int(phase is not state.phase),
            "snapshots": [*state.snapshots, snapshot],
            "transitions": transitions,
            "assignments_without_audited_progress": no_progress,
            "unchanged_cut_snapshots": unchanged_cut,
            "bottleneck_attempts": bottleneck_attempts,
            "completed_assignment_count": snapshot.completed_assignment_count,
            "accepted_synthesis": accepted_synthesis,
        }
    )


def phase_state_sha256(state: ScientificPhaseState) -> str:
    payload = json.dumps(
        state.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def write_scientific_phase_state(path: Path, state: ScientificPhaseState) -> Path:
    return atomic_write_json(
        path,
        {
            **state.model_dump(mode="json"),
            "integrity_sha256": phase_state_sha256(state),
        },
    )


def load_scientific_phase_state(path: Path) -> ScientificPhaseState:
    try:
        raw = json.loads(read_regular_text(path))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StageValidationError(f"Cannot load scientific phase state {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise StageValidationError("Scientific phase state must be one JSON object")
    expected = raw.pop("integrity_sha256", None)
    try:
        state = ScientificPhaseState.model_validate(raw)
    except ValueError as exc:
        raise StageValidationError(f"Scientific phase state is invalid: {exc}") from exc
    if expected != phase_state_sha256(state):
        raise StageValidationError("Scientific phase state integrity digest does not match")
    return state


__all__ = [
    "ADVERSARIAL_COMPLEMENTARY_ROLES",
    "BOTTLENECK_COMPLEMENTARY_ROLES",
    "AssignmentDisposition",
    "DuplicateDisposition",
    "PhaseTransition",
    "PhaseTransitionReason",
    "ScientificPhase",
    "ScientificPhasePolicy",
    "ScientificPhaseState",
    "ScientificProgressSnapshot",
    "ScientificRole",
    "ScientificTaskPlan",
    "admit_assignment",
    "focused_frontier_obligation",
    "load_scientific_phase_state",
    "next_complementary_role",
    "normalize_mechanism",
    "phase_state_sha256",
    "record_scientific_progress",
    "screen_duplicate_assignment",
    "semantic_similarity",
    "validate_task_contract",
    "write_scientific_phase_state",
]
