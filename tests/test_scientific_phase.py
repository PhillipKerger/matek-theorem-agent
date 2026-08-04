from __future__ import annotations

from pathlib import Path

import pytest

from matek_theorem_agent.stages.common import StageValidationError
from matek_theorem_agent.stages.scientific_phase import (
    BOTTLENECK_COMPLEMENTARY_ROLES,
    DuplicateDisposition,
    PhaseTransitionReason,
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
    validate_task_contract,
    write_scientific_phase_state,
)

HASH_A = "a" * 64
HASH_B = "b" * 64


def snapshot(
    sequence: int,
    completed: int,
    *,
    cut: list[str] | None = None,
    passed: int = 0,
    blocked: int = 0,
    refuted: int = 0,
    outcomes: int = 0,
    gaps: list[str] | None = None,
    adversarial_passed: bool = False,
    adversarial_failed: bool = False,
    synthesis_succeeded: bool = False,
    synthesis_gap: str | None = None,
) -> ScientificProgressSnapshot:
    return ScientificProgressSnapshot(
        sequence=sequence,
        ledger_revision=f"revision-{sequence}",
        completed_assignment_count=completed,
        new_audit_passed_count=passed,
        audited_claim_hashes=[HASH_A] if passed else [],
        minimal_open_cut_ids=cut or ["OBL-BOUNDARY1"],
        normalized_exact_gaps=gaps or [],
        blocked_count=blocked,
        refuted_count=refuted,
        recent_outcome_count=outcomes,
        adversarial_audit_passed=adversarial_passed,
        adversarial_audit_failed=adversarial_failed,
        synthesis_succeeded=synthesis_succeeded,
        synthesis_exact_gap=synthesis_gap,
    )


def test_plateau_moves_exploration_to_consolidation_and_focused_cut_to_bottleneck() -> None:
    policy = ScientificPhasePolicy(no_audited_progress_assignments=3)
    state = record_scientific_progress(
        ScientificPhaseState(),
        snapshot(1, 3, blocked=2, outcomes=3),
        policy=policy,
    )

    assert state.phase is ScientificPhase.CONSOLIDATE
    assert state.phase_epoch == 1
    assert state.transitions[-1].reason is PhaseTransitionReason.PLATEAU

    state = record_scientific_progress(state, snapshot(2, 4), policy=policy)
    assert state.phase is ScientificPhase.BOTTLENECK
    assert state.phase_epoch == 2
    assert state.transitions[-1].reason is PhaseTransitionReason.FRONTIER_CONSOLIDATED


def test_repeated_normalized_gap_is_a_durable_plateau_signal(tmp_path: Path) -> None:
    policy = ScientificPhasePolicy(
        no_audited_progress_assignments=20,
        unchanged_cut_snapshots=20,
        repeated_gap_threshold=3,
        blocked_or_refuted_ratio=1.0,
        similarity_threshold=1.0,
    )
    first = record_scientific_progress(
        ScientificPhaseState(),
        snapshot(
            1,
            1,
            gaps=[
                "Prove the ZERO boundary case.",
                "prove-the-zero boundary case",
                "Prove the zero boundary case",
            ],
        ),
        policy=policy,
    )
    path = tmp_path / "scientific-phase.json"
    write_scientific_phase_state(path, first)

    resumed = load_scientific_phase_state(path)

    assert resumed == first
    assert resumed.phase is ScientificPhase.CONSOLIDATE
    assert resumed.transitions[-1].reason is PhaseTransitionReason.PLATEAU


def test_bottleneck_contract_requires_cut_target_and_mechanism_delta() -> None:
    plan = ScientificTaskPlan(
        assignment_id="worker-1",
        phase=ScientificPhase.BOTTLENECK,
        role=ScientificRole.PROVER,
        target_obligation_ids=["OBL-BOUNDARY1"],
        mechanism="Prove the boundary lemma by induction.",
    )
    with pytest.raises(StageValidationError, match="mechanism delta"):
        validate_task_contract(
            plan,
            active_phase=ScientificPhase.BOTTLENECK,
            active_cut_ids=["OBL-BOUNDARY1"],
        )

    valid = plan.model_copy(update={"mechanism_delta": "Use the minimal counterexample."})
    validate_task_contract(
        valid,
        active_phase=ScientificPhase.BOTTLENECK,
        active_cut_ids=["OBL-BOUNDARY1"],
    )

    version_bound = valid.model_copy(
        update={"target_obligation_versions": {"OBL-BOUNDARY1": HASH_A}}
    )
    validate_task_contract(
        version_bound,
        active_phase=ScientificPhase.BOTTLENECK,
        active_cut_ids=["OBL-BOUNDARY1"],
        active_cut_versions={"OBL-BOUNDARY1": HASH_A},
    )
    with pytest.raises(StageValidationError, match="version is missing or stale"):
        validate_task_contract(
            version_bound,
            active_phase=ScientificPhase.BOTTLENECK,
            active_cut_ids=["OBL-BOUNDARY1"],
            active_cut_versions={"OBL-BOUNDARY1": HASH_B},
        )


def test_bottleneck_portfolio_persists_one_focus_and_rotates_all_complementary_roles() -> None:
    cut = ["OBL-SECONDARY2", "OBL-BOUNDARY1"]
    state = ScientificPhaseState(phase=ScientificPhase.BOTTLENECK)
    focus = focused_frontier_obligation(state, cut)

    assert focus == "OBL-BOUNDARY1"
    observed: list[ScientificRole] = []
    for index in range(len(BOTTLENECK_COMPLEMENTARY_ROLES)):
        role = next_complementary_role(
            state,
            phase=ScientificPhase.BOTTLENECK,
            target_obligation_id=focus,
        )
        observed.append(role)
        plan = ScientificTaskPlan(
            assignment_id=f"focused-{index}",
            phase=ScientificPhase.BOTTLENECK,
            role=role,
            target_obligation_ids=[focus],
            mechanism=f"Distinct mechanism {index}",
            mechanism_delta=f"New attack {index}",
        )
        state, disposition = admit_assignment(
            state,
            plan,
            active_cut_ids=cut,
            policy=ScientificPhasePolicy(),
        )
        assert disposition.disposition is DuplicateDisposition.LAUNCH

    assert observed == list(BOTTLENECK_COMPLEMENTARY_ROLES)
    assert {tuple(item.target_obligation_ids) for item in state.launched_assignments} == {(focus,)}
    assert focused_frontier_obligation(state, list(reversed(cut))) == focus
    assert (
        next_complementary_role(
            state,
            phase=ScientificPhase.BOTTLENECK,
            target_obligation_id=focus,
        )
        is ScientificRole.PROVER
    )

    # Once the focus is discharged, the server selects the remaining exact cut.
    assert focused_frontier_obligation(state, ["OBL-SECONDARY2"]) == "OBL-SECONDARY2"


def test_duplicate_assignment_is_redirected_before_launch() -> None:
    policy = ScientificPhasePolicy(similarity_threshold=0.55)
    base = ScientificPhaseState(phase=ScientificPhase.BOTTLENECK)
    first = ScientificTaskPlan(
        assignment_id="worker-1",
        phase=ScientificPhase.BOTTLENECK,
        role=ScientificRole.PROVER,
        target_obligation_ids=["OBL-BOUNDARY1"],
        mechanism="Use induction on the number of finite metric states.",
        mechanism_delta="Try induction rather than enumeration.",
    )
    state, launched = admit_assignment(
        base,
        first,
        active_cut_ids=["OBL-BOUNDARY1"],
        policy=policy,
    )
    assert launched.disposition is DuplicateDisposition.LAUNCH

    duplicate = first.model_copy(
        update={
            "assignment_id": "worker-2",
            "mechanism": "Induct on the number of metric states in the finite space.",
            "mechanism_delta": "Cosmetic rewrite only.",
        }
    )
    state, disposition = admit_assignment(
        state,
        duplicate,
        active_cut_ids=["OBL-BOUNDARY1"],
        policy=policy,
    )

    assert semantic_similarity(first.mechanism, duplicate.mechanism) >= 0.55
    assert disposition.disposition is DuplicateDisposition.REDIRECT
    assert disposition.matched_assignment_id == first.assignment_id
    assert [item.assignment_id for item in state.launched_assignments] == [first.assignment_id]


def test_same_phase_plan_from_an_older_epoch_cannot_launch_or_mask_fresh_work() -> None:
    stale = ScientificTaskPlan(
        assignment_id="old-bottleneck",
        phase=ScientificPhase.BOTTLENECK,
        phase_epoch=2,
        role=ScientificRole.PROVER,
        target_obligation_ids=["OBL-BOUNDARY1"],
        mechanism="Use induction on the exact cut.",
        mechanism_delta="Original bottleneck attack.",
    )
    state = ScientificPhaseState(
        phase=ScientificPhase.BOTTLENECK,
        phase_epoch=4,
        launched_assignments=[stale],
    )

    with pytest.raises(StageValidationError, match="phase epoch 2, not active epoch 4"):
        admit_assignment(
            state,
            stale.model_copy(update={"assignment_id": "stale-queued"}),
            active_cut_ids=["OBL-BOUNDARY1"],
            policy=ScientificPhasePolicy(),
        )

    fresh = stale.model_copy(
        update={"assignment_id": "fresh-bottleneck", "phase_epoch": state.phase_epoch}
    )
    state, disposition = admit_assignment(
        state,
        fresh,
        active_cut_ids=["OBL-BOUNDARY1"],
        policy=ScientificPhasePolicy(),
    )

    assert disposition.disposition is DuplicateDisposition.LAUNCH
    assert state.launched_assignments[-1] == fresh


def test_adversarial_failure_returns_to_bottleneck_and_synthesis_gap_updates_phase() -> None:
    policy = ScientificPhasePolicy()
    audit = ScientificPhaseState(phase=ScientificPhase.ADVERSARIAL_AUDIT)
    failed = record_scientific_progress(
        audit,
        snapshot(1, 1, adversarial_failed=True),
        policy=policy,
    )
    assert failed.phase is ScientificPhase.BOTTLENECK
    assert failed.transitions[-1].reason is PhaseTransitionReason.ADVERSARIAL_FAILURE

    synthesis = ScientificPhaseState(phase=ScientificPhase.SYNTHESIZE)
    gapped = record_scientific_progress(
        synthesis,
        snapshot(1, 1, synthesis_gap="Prove the transfer inequality."),
        policy=policy,
    )
    assert gapped.phase is ScientificPhase.BOTTLENECK
    assert gapped.transitions[-1].reason is PhaseTransitionReason.SYNTHESIS_GAP


def test_synthesis_contract_requires_audited_premises() -> None:
    plan = ScientificTaskPlan(
        assignment_id="synthesis-1",
        phase=ScientificPhase.SYNTHESIZE,
        role=ScientificRole.SYNTHESIZER,
        mechanism="Compose the current audited ledger.",
    )
    with pytest.raises(StageValidationError, match="audited premise"):
        validate_task_contract(
            plan,
            active_phase=ScientificPhase.SYNTHESIZE,
            active_cut_ids=[],
        )
