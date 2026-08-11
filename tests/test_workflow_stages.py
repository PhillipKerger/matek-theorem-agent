from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Collection
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

import matek_theorem_agent.stages.research as research_stage
from matek_theorem_agent.budget import BudgetExceeded, BudgetSnapshot
from matek_theorem_agent.codex_client import CodexRequest, CodexResult
from matek_theorem_agent.config import ModelSettings
from matek_theorem_agent.coordinator_context import serialize_coordinator_payload
from matek_theorem_agent.execution.base import CommandRequest, CommandResult
from matek_theorem_agent.knowledge_graph import (
    ClaimType,
    EpistemicStatus,
    GraphEdge,
    GraphNodeCreate,
    GraphNodeUpdate,
    GraphPatch,
    GraphValidationError,
    KnowledgeGraph,
    NodeType,
    RelationType,
    WorkflowStatus,
)
from matek_theorem_agent.openai_client import (
    ModelInputTooLargeError,
    ModelRequest,
    ModelResult,
    StructuredOutputError,
)
from matek_theorem_agent.scientific import (
    BranchOutcome,
    MaterialityVerdict,
    ScientificArtifactDeclaration,
    ScientificObligationDeclaration,
    ScientificResult,
    ScientificResultDisposition,
    ScientificResultKind,
    ScientificScope,
    TargetMaterialityAssessment,
)
from matek_theorem_agent.source_provenance import (
    SourceVerificationRecord,
    SourceVerificationReport,
    SourceVerificationStatus,
)
from matek_theorem_agent.stages.common import (
    StageValidationError,
    atomic_write_json,
    atomic_write_text,
    sha256_json,
    sha256_text,
)
from matek_theorem_agent.stages.compile_prompt import (
    EXPECTED_FRAMEWORK_SHA256,
    CompiledProblem,
    LiteratureStatus,
    PlaceholderDisposition,
    PromptCompilationStatus,
    PromptPlaceholderRepair,
    SourceLedgerEntry,
    SourceLedgerRepair,
    SourcePurpose,
    compile_prompt,
    find_unresolved_placeholders,
)
from matek_theorem_agent.stages.computation_artifacts import ComputationReplayIsolation
from matek_theorem_agent.stages.counterexample_audit import (
    CounterexampleAuditDecision,
    CounterexampleAuditResponse,
    CounterexampleAuditRole,
)
from matek_theorem_agent.stages.lean import (
    MANDATORY_ALIGNMENT_FIELDS,
    AlignmentCheck,
    AlignmentStatus,
    ClaimAlignment,
    LeanFeasibilityAssessment,
    LeanFeasibilityClass,
    LeanOutcome,
    LeanStatementDraft,
    LeanWorkflowSettings,
    run_lean_pipeline,
    scan_generated_lean,
)
from matek_theorem_agent.stages.lemma_audit import (
    LemmaAuditDecision,
    LemmaAuditResponse,
    LemmaAuditRole,
    LemmaNomination,
    run_lemma_audit,
)
from matek_theorem_agent.stages.manuscript import (
    BibliographyAudit,
    BibliographyEntryAudit,
    BibliographyEntryStatus,
    BibliographyStatus,
    FrozenClaimFidelity,
    IntroductionCoverage,
    LatexBuildResult,
    ManuscriptDraft,
    ManuscriptFinding,
    ManuscriptFindingSeverity,
    ManuscriptOutcome,
    ManuscriptResult,
    ManuscriptStatus,
    PublicationStatus,
    RelatedWorkClaimAudit,
    RelatedWorkValidation,
    generate_manuscript,
    resume_manuscript_bibliography,
    validate_related_work,
)
from matek_theorem_agent.stages.research import (
    ApproachRegistry,
    AuditDecision,
    AuditVerdict,
    CandidateProofPackage,
    FinalJudgeDecision,
    FinalJudgeVerdict,
    ImportedTheorem,
    ResearchAcceptanceGate,
    ResearchAssignment,
    ResearchCoordinatorDecision,
    ResearchOutcome,
    ResearchResult,
    ResearchWorkerReport,
    ResearchWorkflowSettings,
    WorkerStatus,
    _validate_coordinator_decision,
    adapt_research_worker_report_v1,
    run_adaptive_research,
)
from matek_theorem_agent.stages.scientific_phase import (
    ScientificPhase,
    ScientificPhasePolicy,
    ScientificPhaseState,
    ScientificRole,
    load_scientific_phase_state,
)

pytestmark = pytest.mark.comprehensive

PROJECT = Path(__file__).resolve().parents[1]
FRAMEWORK = PROJECT / "resources" / "prompts" / "research_prompt_framework.txt"
PROMPT_COMPILER_INSTRUCTIONS = PROJECT / "resources" / "prompts" / "prompt_compiler.md"


def test_stage_atomic_write_rejects_symlink_destination_without_touching_target(
    tmp_path: Path,
) -> None:
    challenge = tmp_path / "challenge.lean"
    challenge.write_text("theorem target : True := by trivial\n", encoding="utf-8")
    build_log = tmp_path / "build.log"
    build_log.symlink_to(challenge.name)

    with pytest.raises(StageValidationError, match="must not be a symlink"):
        atomic_write_text(build_log, "malicious overwrite\n")

    assert challenge.read_text(encoding="utf-8") == "theorem target : True := by trivial\n"


MANUSCRIPT_CLAIM_CONTRACT = {"conclusion": "P n"}
FRAMEWORK_SECTIONS = (
    "Current task statement",
    "Exact success criterion",
    "Insufficient outcomes",
    "Known starting point and exact bottleneck",
    "Potential master lemmas",
    "Multiagent research protocol",
    "Adversarial auditing requirements",
    "Candidate-solution protocol",
    "Intermediate outcomes",
    "Stopping and reporting policy",
    "Source and public-search policy",
    "Final-response format",
)
VERIFIED_SOURCE_URL = "https://doi.org/10.5555/12345678"
MATEK_FIXTURE_REPOSITORY_URL = "https://github.com/matek-test-fixtures/matek-theorem-agent"
MATEK_FIXTURE_WHITEPAPER_ID = "2099.99999"
MATEK_FIXTURE_WHITEPAPER_URL = f"https://arxiv.org/abs/{MATEK_FIXTURE_WHITEPAPER_ID}"


def research_worker_report_v1(**values: Any) -> ResearchWorkerReport:
    """Build an archived flat fixture only through the explicit compatibility adapter."""

    return adapt_research_worker_report_v1(values)


def typed_worker_report_payload() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "assignment_id": "typed-worker-fixture",
        "results": [
            {
                "schema_version": 1,
                "local_key": "lemma-1",
                "kind": "lemma",
                "exact_statement": "For every n, P(n).",
                "scope": "branch",
                "assumptions": [],
                "proof_or_certificate": "A complete proof of the scoped lemma.",
                "exact_gap": None,
                "dependency_node_ids": [],
                "dependency_result_keys": [],
                "target_node_ids": [],
                "disposition": "partial",
            }
        ],
        "unresolved_obligations": [],
        "source_ledger": [],
        "artifact_manifest": [],
        "branch_outcome": "progress",
        "mechanism": "Direct induction on n.",
    }


def test_research_worker_v2_provider_schema_omits_low_level_graph_mutations() -> None:
    schema = ResearchWorkerReport.model_json_schema()
    properties = schema["properties"]
    assert set(properties) == {
        "schema_version",
        "assignment_id",
        "results",
        "unresolved_obligations",
        "source_ledger",
        "artifact_manifest",
        "branch_outcome",
        "mechanism",
    }
    rendered = json.dumps(schema, sort_keys=True)
    for forbidden in (
        "graph_patch",
        "base_graph_revision",
        "run_id",
        "task_id",
        "create_nodes",
        "update_nodes",
        "add_edges",
    ):
        assert forbidden not in rendered


def test_resumed_assignment_must_match_current_scientific_phase_epoch() -> None:
    archived = research_stage.ResearchAssignmentState(
        assignment=ResearchAssignment(
            id="stale-bottleneck-route",
            approach_family="cut attack",
            task="Attack the old cut.",
            expected_output="A proof or exact gap.",
            scientific_phase=ScientificPhase.BOTTLENECK,
            scientific_role=ScientificRole.PROVER,
            target_obligation_ids=["OBL-OLD10000"],
            mechanism_delta="Try the old decomposition in reverse.",
        ),
        admitted_by_decision=1,
        scientific_phase_epoch=2,
    )
    resumed = research_stage.ResearchAssignmentState.model_validate_json(archived.model_dump_json())
    cycled_state = ScientificPhaseState(
        phase=ScientificPhase.BOTTLENECK,
        phase_epoch=4,
    )

    assert not research_stage._assignment_matches_active_scientific_phase(
        resumed,
        cycled_state,
    )
    assert research_stage._assignment_matches_active_scientific_phase(
        resumed.model_copy(update={"scientific_phase_epoch": 4}),
        cycled_state,
    )


def test_bare_main_claim_open_cut_has_a_semantic_phase_target_version(tmp_path: Path) -> None:
    problem = tmp_path / "problem.md"
    problem.write_text("Prove the fixture theorem.", encoding="utf-8")
    graph = KnowledgeGraph(tmp_path, "bare-main-cut")
    problem_id, _ = graph.initialize_problem(
        source_path=problem,
        problem_text=problem.read_text(encoding="utf-8"),
        run_id="run-bare-main-cut",
    )
    graph.record_compiled_problem(
        problem_id=problem_id,
        run_id="run-bare-main-cut",
        compiled_problem=compiled_problem().model_dump(mode="json"),
    )
    target_id = graph.main_claim_id(problem_id)

    versions = research_stage._scientific_target_versions(
        graph.load_nodes(),
        graph_revision=graph.load_state().revision,
        problem_id=problem_id,
        target_claim_id=target_id,
    )

    assert target_id in versions
    assert len(versions[target_id]) == 64


@pytest.mark.asyncio
async def test_late_old_epoch_workers_do_not_advance_the_current_scientific_phase(
    tmp_path: Path,
) -> None:
    problem = tmp_path / "problem.md"
    problem.write_text("Prove the fixture theorem.", encoding="utf-8")
    run_id = "run-stale-phase-workers"
    graph = KnowledgeGraph(tmp_path, "stale-phase-workers")
    problem_id, _ = graph.initialize_problem(
        source_path=problem,
        problem_text=problem.read_text(encoding="utf-8"),
        run_id=run_id,
    )
    compiled = compiled_problem()
    graph.record_compiled_problem(
        problem_id=problem_id,
        run_id=run_id,
        compiled_problem=compiled.model_dump(mode="json"),
    )
    target_id = graph.main_claim_id(problem_id)

    class StaggeredPhaseClient:
        def __init__(self) -> None:
            self.release_old_epoch = asyncio.Event()

        async def generate_structured(
            self,
            request: ModelRequest,
            output_type: type[Any],
        ) -> ModelResult[Any]:
            payload = json.loads(request.input_text)
            if output_type is ResearchCoordinatorDecision:
                assignments = [
                    ResearchAssignment(
                        id=f"stale-route-{index}",
                        approach_family=family,
                        task=f"Investigate {family}.",
                        expected_output="A proof or exact gap.",
                        target_node_ids=[target_id],
                    )
                    for index, family in enumerate(
                        ("direct", "structural", "probabilistic", "computational"),
                        start=1,
                    )
                ]
                memory = payload["knowledge_graph_memory"]
                return ModelResult(
                    parsed=ResearchCoordinatorDecision(
                        decision_id=payload["decision_id"],
                        after_event_sequence=payload["after_event_sequence"],
                        assignments=assignments,
                        rationale=(
                            f"Graph review {memory['graph_revision']}: launch a staggered "
                            "phase-transition fixture."
                        ),
                    ),
                    response_id="stale-phase-coordinator",
                )
            assert output_type is ResearchWorkerReport
            assignment_id = payload["assignment"]["id"]
            if assignment_id == "stale-route-1":
                asyncio.get_running_loop().call_later(0.01, self.release_old_epoch.set)
            else:
                await self.release_old_epoch.wait()
            return ModelResult(
                parsed=ResearchWorkerReport(
                    assignment_id=assignment_id,
                    unresolved_obligations=[
                        ScientificObligationDeclaration(
                            local_key="exact-gap",
                            exact_statement=f"Complete the {assignment_id} route.",
                            conclusion=f"Complete the {assignment_id} route.",
                        )
                    ],
                    branch_outcome=BranchOutcome.BLOCKED,
                    mechanism=payload["assignment"]["task"],
                ),
                response_id=f"stale-phase-{assignment_id}",
            )

    research_dir = tmp_path / ".matek" / "runs" / run_id / "research"
    result = await run_adaptive_research(
        client=StaggeredPhaseClient(),  # type: ignore[arg-type]
        compiled_problem=compiled,
        research_dir=research_dir,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=4,
            maximum_pending_assignments=4,
            maximum_coordinator_decisions=1,
            scientific_phase_policy=ScientificPhasePolicy(
                no_audited_progress_assignments=1,
            ),
        ),
        knowledge_graph=graph,
        graph_problem_id=problem_id,
        run_id=run_id,
    )

    assert result.outcome is ResearchOutcome.BUDGET_EXHAUSTED
    phase_state = load_scientific_phase_state(
        research_dir / "coordinator" / "scientific-phase.json"
    )
    assert phase_state.phase is ScientificPhase.CONSOLIDATE
    assert phase_state.phase_epoch == 1
    assert phase_state.completed_assignment_count == 1
    assert phase_state.progress_counted_assignment_ids == ["stale-route-1"]
    scheduler = json.loads(
        (research_dir / "coordinator" / "state.json").read_text(encoding="utf-8")
    )
    assert all(record["status"] == "completed" for record in scheduler["assignments"])


def test_no_gap_progress_report_cannot_pass_adversarial_phase_without_durable_audits() -> None:
    target_id = "OBL-AUDIT001"
    target_version = "c" * 64

    def evidence_record(
        assignment_id: str,
        role: ScientificRole,
        *,
        audited: bool,
        audit_target_id: str = target_id,
    ) -> tuple[research_stage.ResearchAssignmentState, ResearchWorkerReport]:
        audit_records = (
            [
                research_stage.IntermediateLemmaAuditRecord(
                    result_local_key="checked-claim",
                    nomination_id=f"nomination-{assignment_id}",
                    graph_revision="00000001-fixture",
                    target_obligation_ids=[audit_target_id],
                    target_obligation_versions={audit_target_id: target_version},
                    gate_status=research_stage.LemmaAuditGateStatus.AUDIT_PASSED,
                    nomination_path=f"lemma-audits/{assignment_id}/nomination.json",
                    nomination_sha256="a" * 64,
                    gate_path=f"lemma-audits/{assignment_id}/gate.json",
                    gate_sha256="b" * 64,
                    graph_recorded=True,
                )
            ]
            if audited
            else []
        )
        record = research_stage.ResearchAssignmentState(
            assignment=ResearchAssignment(
                id=assignment_id,
                approach_family=role.value,
                task="Audit the exact cut obligation.",
                expected_output="An independently checked result.",
                scientific_phase=ScientificPhase.ADVERSARIAL_AUDIT,
                scientific_role=role,
                target_obligation_ids=[target_id],
                target_obligation_versions=[
                    research_stage.TargetObligationVersion(
                        obligation_id=target_id,
                        logical_version=target_version,
                    )
                ],
                mechanism_delta=f"Use the {role.value} lane.",
            ),
            admitted_by_decision=2,
            scientific_phase_epoch=7,
            status=research_stage.AssignmentLifecycle.COMPLETED,
            intermediate_lemma_audits=audit_records,
        )
        report = ResearchWorkerReport(
            assignment_id=assignment_id,
            results=[
                ScientificResult(
                    local_key="checked-claim",
                    kind=ScientificResultKind.LEMMA,
                    exact_statement=f"The {role.value} check passes.",
                    scope=ScientificScope.BRANCH,
                    proof_or_certificate="Complete independently checkable argument.",
                    disposition=ScientificResultDisposition.PROPOSED_COMPLETE,
                )
            ],
            branch_outcome=BranchOutcome.PROGRESS,
            mechanism=role.value,
        )
        return record, report

    no_audit_record, no_audit_report = evidence_record(
        "no-gap-progress",
        ScientificRole.FALSIFIER,
        audited=False,
    )
    assert not research_stage._adversarial_audit_has_durable_pass_evidence(
        [no_audit_record],
        {no_audit_report.assignment_id: no_audit_report},
        phase_epoch=7,
        active_cut_ids=[],
        current_obligation_versions={target_id: target_version},
    )

    falsifier_record, falsifier_report = evidence_record(
        "audited-falsifier",
        ScientificRole.FALSIFIER,
        audited=True,
    )
    transfer_record, transfer_report = evidence_record(
        "audited-transfer",
        ScientificRole.TRANSFER_AUDITOR,
        audited=True,
    )
    assert research_stage._adversarial_audit_has_durable_pass_evidence(
        [falsifier_record, transfer_record],
        {
            falsifier_report.assignment_id: falsifier_report,
            transfer_report.assignment_id: transfer_report,
        },
        phase_epoch=7,
        active_cut_ids=[],
        current_obligation_versions={target_id: target_version},
    )

    assert not research_stage._adversarial_audit_has_durable_pass_evidence(
        [falsifier_record, transfer_record],
        {
            falsifier_report.assignment_id: falsifier_report,
            transfer_report.assignment_id: transfer_report,
        },
        phase_epoch=7,
        active_cut_ids=[],
        current_obligation_versions={target_id: "d" * 64},
    )

    legacy_payload = falsifier_record.model_dump(mode="json")
    legacy_payload["intermediate_lemma_audits"][0].pop("target_obligation_ids")
    legacy_payload["intermediate_lemma_audits"][0].pop("target_obligation_versions")
    legacy_falsifier = research_stage.ResearchAssignmentState.model_validate(legacy_payload)
    assert legacy_falsifier.intermediate_lemma_audits[0].target_obligation_ids == []
    assert not research_stage._adversarial_audit_has_durable_pass_evidence(
        [legacy_falsifier, transfer_record],
        {
            falsifier_report.assignment_id: falsifier_report,
            transfer_report.assignment_id: transfer_report,
        },
        phase_epoch=7,
        active_cut_ids=[],
        current_obligation_versions={target_id: target_version},
    )

    unrelated_target_id = "OBL-OTHER001"
    unrelated_falsifier, unrelated_falsifier_report = evidence_record(
        "unrelated-falsifier",
        ScientificRole.FALSIFIER,
        audited=True,
        audit_target_id=unrelated_target_id,
    )
    unrelated_transfer, unrelated_transfer_report = evidence_record(
        "unrelated-transfer",
        ScientificRole.TRANSFER_AUDITOR,
        audited=True,
        audit_target_id=unrelated_target_id,
    )
    assert not research_stage._adversarial_audit_has_durable_pass_evidence(
        [unrelated_falsifier, unrelated_transfer],
        {
            unrelated_falsifier_report.assignment_id: unrelated_falsifier_report,
            unrelated_transfer_report.assignment_id: unrelated_transfer_report,
        },
        phase_epoch=7,
        active_cut_ids=[],
        current_obligation_versions={
            target_id: target_version,
            unrelated_target_id: target_version,
        },
    )


@pytest.mark.parametrize(
    "mutation_field",
    ["graph_patch", "run_id", "task_id", "base_graph_revision", "update_nodes"],
)
def test_research_worker_v2_rejects_model_authored_identity_and_mutation_fields(
    mutation_field: str,
) -> None:
    payload = typed_worker_report_payload()
    payload[mutation_field] = {"model_authored": True}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ResearchWorkerReport.model_validate(payload)


def test_archived_worker_v1_reports_adapt_without_schema_rejection() -> None:
    archived = [
        {
            "assignment_id": "atsp-route",
            "status": "progress",
            "formal_results": ["A cycle-cover reduction holds under hypothesis H."],
            "proof_content": "Derive the reduction directly from the decomposition.",
            "exact_gap": "Remove hypothesis H.",
            "sources": [],
            "assumptions": ["H"],
            "counterexamples": [],
            "dependencies": ["CLM-ABCDEF12", "Prove the transfer lemma."],
            "mechanism": "cycle-cover reduction",
            "graph_patch": {"run_id": "legacy-run", "update_nodes": []},
        },
        {
            "assignment_id": "matroid-route",
            "status": "blocked",
            "formal_results": [],
            "proof_content": "The exchange argument stops at the correlation step.",
            "exact_gap": "Prove negative correlation for the selected basis.",
            "sources": [],
            "assumptions": [],
            "counterexamples": [],
            "dependencies": [],
            "mechanism": "exchange argument",
            "graph_patch": None,
        },
        {
            "assignment_id": "k-server-route",
            "status": "refuted",
            "formal_results": [],
            "proof_content": "The displayed three-point instance violates the branch lemma.",
            "exact_gap": None,
            "sources": [],
            "assumptions": [],
            "counterexamples": ["A three-point metric refutes the proposed transfer lemma."],
            "dependencies": [],
            "mechanism": "hostile small-instance search",
            "graph_patch": '{"nodes": []}',
        },
    ]

    normalized = [adapt_research_worker_report_v1(item) for item in archived]

    assert len(normalized) == len(archived)
    assert all(item.schema_version == 2 for item in normalized)
    assert all("graph_patch" not in item.model_dump(mode="json") for item in normalized)
    assert normalized[0].unresolved_obligations
    assert normalized[1].branch_outcome.value == "blocked"
    assert normalized[2].counterexamples


def test_gapped_or_blocked_worker_result_cannot_be_proposed_complete() -> None:
    gapped = typed_worker_report_payload()
    gapped["results"][0]["exact_gap"] = "Prove the induction step."
    gapped["results"][0]["disposition"] = "proposed_complete"
    with pytest.raises(ValidationError, match="exact gap is a proof attempt"):
        ResearchWorkerReport.model_validate(gapped)

    blocked = typed_worker_report_payload()
    blocked["results"][0]["scope"] = "main"
    blocked["results"][0]["disposition"] = "proposed_complete"
    blocked["branch_outcome"] = "blocked"
    blocked["unresolved_obligations"] = [
        {
            "schema_version": 1,
            "local_key": "open-gap",
            "exact_statement": "Prove the induction step.",
            "quantifiers": [],
            "hypotheses": [],
            "conclusion": "The induction step holds.",
            "parent_result_keys": ["lemma-1"],
            "dependency_node_ids": [],
            "scope": "main",
            "notation_definition_version": "1",
            "falsification_evidence": [],
            "estimated_leverage": 100,
        }
    ]
    with pytest.raises(
        ValidationError,
        match="blocked branch cannot contain a proposed_complete main result",
    ):
        ResearchWorkerReport.model_validate(blocked)


def test_blocked_branch_can_preserve_a_complete_restricted_lemma() -> None:
    payload = typed_worker_report_payload()
    payload["branch_outcome"] = "blocked"
    payload["unresolved_obligations"] = [
        {
            "local_key": "main-gap",
            "exact_statement": "Extend the restricted lemma to the main domain.",
            "conclusion": "The main-domain theorem holds.",
            "scope": "main",
        }
    ]
    payload["results"][0]["disposition"] = "proposed_complete"

    report = ResearchWorkerReport.model_validate(payload)

    assert report.results[0].scope is ScientificScope.BRANCH
    assert report.results[0].disposition is ScientificResultDisposition.PROPOSED_COMPLETE


def test_incomplete_counterexample_cannot_create_a_refutation() -> None:
    payload = typed_worker_report_payload()["results"][0]
    payload.update(
        {
            "kind": "counterexample",
            "disposition": "refuted_mechanism",
            "exact_gap": "Check that the proposed instance satisfies the domain assumptions.",
        }
    )

    with pytest.raises(ValidationError, match="incomplete counterexample"):
        ScientificResult.model_validate(payload)


@pytest.mark.parametrize(
    ("dependency_keys", "match"),
    [
        (["missing-result"], "unknown local result"),
        (["lemma-1"], "cannot depend on itself"),
    ],
)
def test_worker_report_rejects_invalid_local_result_dependencies(
    dependency_keys: list[str],
    match: str,
) -> None:
    payload = typed_worker_report_payload()
    payload["results"][0]["dependency_result_keys"] = dependency_keys

    with pytest.raises(ValidationError, match=match):
        ResearchWorkerReport.model_validate(payload)


def test_worker_report_rejects_local_result_dependency_cycle() -> None:
    payload = typed_worker_report_payload()
    second = dict(payload["results"][0])
    second["local_key"] = "lemma-2"
    second["dependency_result_keys"] = ["lemma-1"]
    payload["results"][0]["dependency_result_keys"] = ["lemma-2"]
    payload["results"].append(second)

    with pytest.raises(ValidationError, match="dependency cycle"):
        ResearchWorkerReport.model_validate(payload)


def test_prompt_compiler_requires_compact_cdc_aligned_research_mandate() -> None:
    instructions = PROMPT_COMPILER_INSTRUCTIONS.read_text(encoding="utf-8")
    normalized = " ".join(instructions.split())

    assert "Research mandate snapshot" in normalized
    for requirement in (
        "exact target",
        "boundary conventions",
        "managed adaptively rather than by fixed quotas",
        "problem-specific adversarial checks",
        "permitted public-search boundary",
        "audited complete solution",
        "additive terms",
        "deterministic versus randomized",
        "finite versus arbitrary",
        "prove-versus-refute",
    ):
        assert requirement in normalized


def web_source_metadata(url: str = VERIFIED_SOURCE_URL) -> tuple[dict[str, Any], ...]:
    return (
        {
            "type": "web_search_call",
            "id": "ws_fixture",
            "status": "completed",
            "action": {
                "type": "search",
                "sources": [
                    {"type": "url", "url": url, "title": "Fixture source"},
                    {
                        "type": "url",
                        "url": MATEK_FIXTURE_REPOSITORY_URL,
                        "title": "MATEK software test fixture",
                    },
                    {
                        "type": "url",
                        "url": MATEK_FIXTURE_WHITEPAPER_URL,
                        "title": "MATEK whitepaper test fixture",
                    },
                ],
            },
        },
    )


def covered_compiled_prompt(extra: str = "") -> str:
    blocks = [
        (
            f"{section}\n"
            "This problem-specific section preserves the complete rigorous method and states "
            "concrete obligations for the fixture theorem."
        )
        for section in FRAMEWORK_SECTIONS
    ]
    if extra:
        blocks[0] += f" {extra}"
    return "\n\n".join(blocks)


class StaticClient:
    def __init__(
        self,
        outputs: list[BaseModel],
        *,
        tool_metadata: tuple[dict[str, Any], ...] = (),
    ) -> None:
        self.outputs = outputs
        self.requests: list[ModelRequest] = []
        self.tool_metadata = tool_metadata

    async def generate_structured(
        self, request: ModelRequest, output_type: type[Any]
    ) -> ModelResult[Any]:
        self.requests.append(request)
        output = self.outputs.pop(0)
        assert isinstance(output, output_type)
        return ModelResult(
            parsed=output,
            response_id=f"response-{len(self.requests)}",
            tool_metadata=self.tool_metadata,
        )


def compiled_problem(
    prompt: str | None = None,
) -> CompiledProblem:
    return CompiledProblem(
        title="Fixture theorem",
        normalized_statement="Prove P(n) for every n.",
        claim_contract={"quantifiers": "for every n", "conclusion": "P n"},
        compiled_prompt=prompt or covered_compiled_prompt(),
        source_ledger=[],
        unresolved_ambiguities=[],
    )


@pytest.mark.parametrize(
    "protected_text",
    [
        "The interval [1,c] is finite.",
        "For every [x,y], take its order complex.",
        r"The lower interval [1,x^{-1}y] has the required rank.",
        "Use the indexed interval [a_i,b_j].",
        "The matrix entry M[i,j] and index set A_{[i,j]} are fixed.",
        "This follows from [Smith 2020] and [@smith2020].",
        "See [the primary source](https://example.test/source).",
        r"\[ [x,y] = \{z : x \le z \le y\}. \]",
        "Keep `[TODO]` as a literal code example.",
        "```text\n[INSERT TARGET HERE]\n```",
    ],
)
def test_placeholder_detector_accepts_math_citations_links_and_code(
    protected_text: str,
) -> None:
    assert find_unresolved_placeholders(protected_text) == []


@pytest.mark.parametrize(
    "marker",
    [
        "[TODO]",
        "[TBD]",
        "[FIXME: state the lemma]",
        "[INSERT TARGET HERE]",
        "[FILL IN THE CONSTANT]",
        "[REPLACE THIS TEXT]",
        "[PLACEHOLDER]",
        "[citation needed]",
        "[FULL NAME OF THE PROBLEM, CONJECTURE, OR TARGET THEOREM]",
    ],
)
def test_placeholder_detector_rejects_strong_editorial_markers(marker: str) -> None:
    assert find_unresolved_placeholders(f"Prose {marker} remains.") == [marker]


@pytest.mark.asyncio
async def test_prompt_compiler_checks_hash_placeholders_and_writes_contract(
    tmp_path: Path,
) -> None:
    payload = compiled_problem(
        covered_compiled_prompt("Use four independent routes and prove the exact theorem.")
    )
    client = StaticClient([payload])

    result = await compile_prompt(
        client=client,
        problem_text="Prove P.",
        framework_path=FRAMEWORK,
        prompts_dir=tmp_path,
    )

    assert result.framework_sha256 == EXPECTED_FRAMEWORK_SHA256
    assert (tmp_path / "framework.txt").read_bytes() == FRAMEWORK.read_bytes()
    assert set(result.artifacts.paths) == {
        "framework",
        "compiled_prompt",
        "compiled_problem",
        "prompt_validation",
        "source_ledger",
        "source_verification",
        "target_alignment",
    }
    assert result.target_alignment is not None
    assert result.target_alignment.passed is True
    assert client.requests[0].settings.reasoning_effort == "xhigh"
    assert client.requests[0].settings.web_search is True
    assert "Allowed terminal reductions: none" in result.compiled_prompt
    assert "external configured resource limit" in result.compiled_prompt

    bad_client = StaticClient(
        [compiled_problem(covered_compiled_prompt("Prove [INSERT TARGET HERE]."))]
    )
    recovered = await compile_prompt(
        client=bad_client,
        problem_text="Prove P.",
        framework_path=FRAMEWORK,
        prompts_dir=tmp_path / "bad",
    )
    assert recovered.prompt_validation.passed is True
    assert recovered.prompt_validation.warnings
    assert "[INSERT TARGET HERE]" not in recovered.compiled_prompt


@pytest.mark.asyncio
async def test_prompt_compiler_persists_hash_bound_k_server_target_alignment(
    tmp_path: Path,
) -> None:
    statement = (
        "Prove that on arbitrary metric spaces, for every k there exists beta such that "
        "cost_ALG <= k * OPT + beta."
    )
    contract = {
        "quantifiers": "for every k there exists beta",
        "domain": "arbitrary metric spaces",
        "additive_terms": "cost_ALG <= k * OPT + beta",
        "polarity": "prove",
        "conclusion": "cost_ALG <= k * OPT + beta",
    }
    payload = CompiledProblem(
        title="K-server target",
        normalized_statement=statement,
        claim_contract=contract,
        compiled_prompt=covered_compiled_prompt(),
    )

    result = await compile_prompt(
        client=StaticClient([payload]),
        problem_text="Prove the k-server bound including its additive constant.",
        framework_path=FRAMEWORK,
        prompts_dir=tmp_path,
    )

    assert result.target_alignment is not None
    assert result.target_alignment.passed is True
    persisted = json.loads((tmp_path / "target_alignment.json").read_text(encoding="utf-8"))
    assert persisted["passed"] is True
    assert persisted["statement_sha256"] == sha256_text(statement)
    canonical_contract = "\n".join(f"{key}\0{value}" for key, value in sorted(contract.items()))
    assert persisted["contract_sha256"] == sha256_text(canonical_contract)
    assert "target_alignment" in result.artifacts.paths


@pytest.mark.asyncio
async def test_prompt_compiler_incident_randomized_policy_reaches_research_boundary(
    tmp_path: Path,
) -> None:
    payload = CompiledProblem(
        title="Random-order matroid secretary target",
        normalized_statement=(
            "There exist a universal constant C and one randomized causal online policy ALG. "
            "Let pi be a uniformly random arrival permutation and R be ALG's internal "
            "randomness. Every accepted set is feasible for every realization. "
            "E_{pi,R}[w(I_ALG)] >= OPT/C."
        ),
        claim_contract={
            "randomness": json.dumps(
                {
                    "algorithm_randomization": "allowed_or_required",
                    "arrival_randomness": "uniform_random_permutation",
                    "weight_adversary": "oblivious_before_randomness",
                    "expectation_over": ["arrival_order", "algorithm_coins"],
                    "feasibility_requirement": "pathwise",
                    "value_guarantee": "in_expectation",
                }
            )
        },
        compiled_prompt=covered_compiled_prompt(),
    )

    result = await compile_prompt(
        client=StaticClient([payload]),
        problem_text="Prove the random-order matroid secretary guarantee.",
        framework_path=FRAMEWORK,
        prompts_dir=tmp_path,
    )

    assert result.target_alignment is not None
    assert result.target_alignment.passed is True
    assert result.target_alignment.randomness is not None
    assert result.target_alignment.randomness.material_contradiction is False
    persisted = json.loads((tmp_path / "target_alignment.json").read_text(encoding="utf-8"))
    assert persisted["randomness"]["statement"]["algorithm_randomization"] == (
        "allowed_or_required"
    )
    assert persisted["randomness"]["statement"]["feasibility_requirement"] == "pathwise"


@pytest.mark.asyncio
async def test_prompt_compiler_warns_when_k_server_target_drops_additive_beta(
    tmp_path: Path,
) -> None:
    payload = CompiledProblem(
        title="Weakened k-server target",
        normalized_statement=(
            "Prove that on arbitrary metric spaces, for every k there exists beta such that "
            "cost_ALG <= k * OPT."
        ),
        claim_contract={
            "quantifiers": "for every k there exists beta",
            "domain": "arbitrary metric spaces",
            "additive_terms": "cost_ALG <= k * OPT + beta",
            "polarity": "prove",
            "conclusion": "cost_ALG <= k * OPT + beta",
        },
        compiled_prompt=covered_compiled_prompt(),
    )
    client = StaticClient([payload])

    result = await compile_prompt(
        client=client,
        problem_text="Prove the k-server bound including its additive constant.",
        framework_path=FRAMEWORK,
        prompts_dir=tmp_path,
    )

    assert len(client.requests) == 1
    assert result.target_alignment is not None
    assert result.target_alignment.passed is True
    assert result.target_alignment.materiality_review is not None
    assert result.target_alignment.materiality_review.status == "unavailable"
    persisted = json.loads((tmp_path / "target_alignment.json").read_text(encoding="utf-8"))
    assert persisted["passed"] is True
    assert persisted["blocking_issues"] == []
    additive_check = next(
        check for check in persisted["checks"] if check["category"] == "additive_terms"
    )
    assert additive_check["passed"] is False
    assert "compact formal comparison" in additive_check["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("statement", "contract", "expected_category", "missing_marker"),
    [
        (
            "Prove the guarantee for every deterministic online algorithm.",
            {"algorithm_domain": "randomized online algorithm"},
            "domain",
            "randomized",
        ),
        (
            "Prove P(n) for every n.",
            {"quantifiers": "there exists n such that P(n)"},
            "quantifiers",
            "exists n",
        ),
        (
            "Prove that the conjecture holds.",
            {"polarity": "refute the conjecture by a counterexample"},
            "polarity",
            "refute/disprove polarity",
        ),
        (
            "For every n, foo(n) <= bar(n) + beta.",
            {"conclusion": "cost_ALG(n) <= k * OPT(n) + beta"},
            "conclusion",
            "compact formal comparison",
        ),
        (
            "For every n, cost(n) <= 2 * OPT(n).",
            {"conclusion": "cost(n) <= 3 * OPT(n)"},
            "conclusion",
            "compact formal comparison",
        ),
    ],
)
async def test_prompt_compiler_warns_on_heuristic_target_clause_drift(
    tmp_path: Path,
    statement: str,
    contract: dict[str, str],
    expected_category: str,
    missing_marker: str,
) -> None:
    payload = CompiledProblem(
        title="Clause drift fixture",
        normalized_statement=statement,
        claim_contract=contract,
        compiled_prompt=covered_compiled_prompt(),
    )

    result = await compile_prompt(
        client=StaticClient([payload]),
        problem_text="Exercise deterministic target-clause validation.",
        framework_path=FRAMEWORK,
        prompts_dir=tmp_path,
    )

    persisted = json.loads((tmp_path / "target_alignment.json").read_text(encoding="utf-8"))
    assert result.target_alignment is not None
    assert result.target_alignment.passed is True
    assert persisted["blocking_issues"] == []
    check = next(item for item in persisted["checks"] if item["category"] == expected_category)
    assert check["passed"] is False
    assert missing_marker in check["detail"]


@pytest.mark.asyncio
async def test_prompt_compiler_only_blocks_a_reviewer_confirmed_target_conflict(
    tmp_path: Path,
) -> None:
    payload = CompiledProblem(
        title="Changed constant",
        normalized_statement="For every n, cost(n) <= 2 * OPT(n).",
        claim_contract={"conclusion": "cost(n) <= 3 * OPT(n)"},
        compiled_prompt=covered_compiled_prompt(),
    )
    assessment = TargetMaterialityAssessment(
        verdict=MaterialityVerdict.CONFIRMED_CONFLICT,
        rationale="The normalized statement changes the approximation factor from 3 to 2.",
        clause_keys=["conclusion"],
    )
    client = StaticClient([payload, assessment])

    with pytest.raises(StageValidationError, match="does not match the claim contract"):
        await compile_prompt(
            client=client,
            problem_text="Prove the factor-three bound.",
            framework_path=FRAMEWORK,
            prompts_dir=tmp_path,
            alignment_review_settings=ModelSettings(
                model="gpt-5.6-terra",
                reasoning_effort="medium",
                web_search=False,
            ),
        )

    persisted = json.loads((tmp_path / "target_alignment.json").read_text(encoding="utf-8"))
    assert persisted["passed"] is False
    assert persisted["materiality_review"]["verdict"] == "CONFIRMED_CONFLICT"
    assert len(client.requests) == 2


@pytest.mark.asyncio
async def test_prompt_compiler_continues_when_reviewer_clears_a_possible_conflict(
    tmp_path: Path,
) -> None:
    payload = CompiledProblem(
        title="Scoped negation",
        normalized_statement=(
            "Future weights are not known, and ALG makes immediate and irrevocable decisions."
        ),
        claim_contract={
            "online_decisions": (
                "decisions may not be deferred and accepted elements may not be revoked"
            )
        },
        compiled_prompt=covered_compiled_prompt(),
    )
    assessment = TargetMaterialityAssessment(
        verdict=MaterialityVerdict.NO_MATERIAL_CONFLICT,
        rationale="Both texts require immediate, irrevocable decisions; the negation has scope.",
        clause_keys=["online_decisions"],
    )
    client = StaticClient([payload, assessment])

    result = await compile_prompt(
        client=client,
        problem_text="Prove the online theorem.",
        framework_path=FRAMEWORK,
        prompts_dir=tmp_path,
        alignment_review_settings=ModelSettings(
            model="gpt-5.6-terra", reasoning_effort="medium", web_search=False
        ),
    )

    assert result.target_alignment is not None
    assert result.target_alignment.passed is True
    assert result.target_alignment.materiality_review is not None
    assert result.target_alignment.materiality_review.verdict is (
        MaterialityVerdict.NO_MATERIAL_CONFLICT
    )
    assert result.calls.model_calls == 2


@pytest.mark.asyncio
async def test_prompt_compiler_continues_when_materiality_reviewer_is_unavailable(
    tmp_path: Path,
) -> None:
    payload = CompiledProblem(
        title="Scoped negation",
        normalized_statement=(
            "Future weights are not known, and ALG makes immediate and irrevocable decisions."
        ),
        claim_contract={
            "online_decisions": (
                "decisions may not be deferred and accepted elements may not be revoked"
            )
        },
        compiled_prompt=covered_compiled_prompt(),
    )
    client = StaticClient([payload])

    result = await compile_prompt(
        client=client,
        problem_text="Prove the online theorem.",
        framework_path=FRAMEWORK,
        prompts_dir=tmp_path,
        alignment_review_settings=ModelSettings(
            model="gpt-5.6-terra", reasoning_effort="medium", web_search=False
        ),
    )

    assert len(client.requests) == 2
    assert result.target_alignment is not None
    assert result.target_alignment.passed is True
    assert result.target_alignment.blocking_issues == []
    assert result.target_alignment.materiality_review is not None
    assert result.target_alignment.materiality_review.status == "unavailable"
    assert any("review was unavailable" in warning for warning in result.target_alignment.warnings)


@pytest.mark.asyncio
async def test_prompt_compiler_uses_one_small_context_only_placeholder_repair(
    tmp_path: Path,
) -> None:
    payload = compiled_problem(
        covered_compiled_prompt("Prove [INSERT TARGET HERE] under the stated hypotheses.")
    )
    repair = PromptPlaceholderRepair(
        replacement_sentence="Prove P for every n under the stated hypotheses."
    )
    client = StaticClient([payload, repair])

    result = await compile_prompt(
        client=client,
        problem_text="Prove P.",
        framework_path=FRAMEWORK,
        prompts_dir=tmp_path,
    )

    assert result.calls.model_calls == 2
    assert result.prompt_validation.passed is True
    assert result.prompt_validation.diagnostics[0].disposition is PlaceholderDisposition.REPAIRED
    assert "[INSERT TARGET HERE]" not in result.compiled_prompt
    repair_input = json.loads(client.requests[1].input_text)
    assert set(repair_input) == {
        "claim_contract",
        "normalized_statement",
        "section_name",
        "suspect_sentence",
    }
    assert "compiled_prompt" not in repair_input
    assert client.requests[1].settings.web_search is False
    assert client.requests[1].settings.max_output_tokens == 1_200


@pytest.mark.asyncio
async def test_prompt_compiler_downgrades_unrepairable_optional_sentence(
    tmp_path: Path,
) -> None:
    prompt = covered_compiled_prompt().replace(
        "Known starting point and exact bottleneck\n",
        "Known starting point and exact bottleneck\nRemove [TODO] from this optional note.\n",
    )
    client = StaticClient([compiled_problem(prompt)])

    result = await compile_prompt(
        client=client,
        problem_text="Prove P.",
        framework_path=FRAMEWORK,
        prompts_dir=tmp_path,
    )

    assert result.prompt_validation.passed is True
    assert result.prompt_validation.warnings
    diagnostic = result.prompt_validation.diagnostics[0]
    assert diagnostic.disposition is PlaceholderDisposition.REMOVED_OPTIONAL
    assert diagnostic.target_critical is False
    assert "[TODO]" not in result.compiled_prompt
    persisted = json.loads((tmp_path / "prompt_validation.json").read_text(encoding="utf-8"))
    assert persisted["warnings"] == result.prompt_validation.warnings


@pytest.mark.asyncio
async def test_prompt_compiler_removes_unrepairable_target_placeholder_and_warns(
    tmp_path: Path,
) -> None:
    payload = compiled_problem(covered_compiled_prompt("Prove [INSERT TARGET HERE]."))

    result = await compile_prompt(
        client=StaticClient([payload]),
        problem_text="Prove P.",
        framework_path=FRAMEWORK,
        prompts_dir=tmp_path,
    )

    assert (tmp_path / "compiled_problem.json").is_file()
    assert (tmp_path / "compiled_research_prompt.md").is_file()
    assert "[INSERT TARGET HERE]" not in result.compiled_prompt
    validation = json.loads((tmp_path / "prompt_validation.json").read_text(encoding="utf-8"))
    assert validation["passed"] is True
    assert validation["warnings"]
    assert validation["diagnostics"][0]["disposition"] == "removed_target_critical"


@pytest.mark.asyncio
async def test_prompt_compiler_selects_and_warns_on_an_ambiguous_interpretation(
    tmp_path: Path,
) -> None:
    clarification = CompiledProblem(
        status=PromptCompilationStatus.NEEDS_CLARIFICATION,
        clarification_reason=(
            "The phrase 'extension problem' could refer to two inequivalent targets."
        ),
        clarification_questions=[
            "Which objects are being extended?",
            "Is the requested conclusion existence, uniqueness, or classification?",
        ],
        candidate_interpretations=[
            "Extend a bounded operator from a subspace.",
            "Extend a partial combinatorial structure.",
        ],
        unresolved_ambiguities=["The mathematical domain and conclusion are unspecified."],
    )

    result = await compile_prompt(
        client=StaticClient([clarification]),
        problem_text="Solve the extension problem.",
        framework_path=FRAMEWORK,
        prompts_dir=tmp_path,
    )

    assert not result.needs_clarification
    assert result.compiled_problem.assumed_interpretation == (
        "Extend a bounded operator from a subspace."
    )
    assert result.compiled_problem.assumption_warning
    assert "compiled_prompt" in result.artifacts.paths
    assert (tmp_path / "compiled_problem.json").is_file()
    assert not (tmp_path / "clarification_request.md").exists()
    assert result.prompt_validation.warnings
    assert "assumed" in result.compiled_prompt.casefold()


@pytest.mark.asyncio
async def test_prompt_compiler_marks_verified_existing_literature_without_novelty(
    tmp_path: Path,
) -> None:
    payload = compiled_problem().model_dump(mode="python")
    payload.update(
        {
            "literature_status": LiteratureStatus.FULLY_RESOLVED,
            "literature_resolution_summary": (
                "The cited theorem has the same domain, quantifiers, hypotheses, and conclusion."
            ),
            "source_ledger": [
                {
                    "title": "Verified fixture theorem",
                    "stable_identifier": "10.5555/12345678",
                    "url": VERIFIED_SOURCE_URL,
                    "verified": True,
                    "evidence": VERIFIED_SOURCE_URL,
                }
            ],
        }
    )
    known = CompiledProblem.model_validate(payload)

    result = await compile_prompt(
        client=StaticClient([known], tool_metadata=web_source_metadata()),
        problem_text="Reconstruct the verified fixture theorem.",
        framework_path=FRAMEWORK,
        prompts_dir=tmp_path,
    )

    assert result.compiled_problem.literature_status is LiteratureStatus.FULLY_RESOLVED
    assert result.compiled_problem.literature_resolution_summary


@pytest.mark.asyncio
async def test_prompt_compiler_rejects_modified_default_framework(tmp_path: Path) -> None:
    modified = tmp_path / "framework.txt"
    modified.write_bytes(FRAMEWORK.read_bytes() + b"\nmodified\n")
    client = StaticClient([compiled_problem()])
    with pytest.raises(StageValidationError, match="integrity check failed"):
        await compile_prompt(
            client=client,
            problem_text="Prove P.",
            framework_path=modified,
            prompts_dir=tmp_path / "prompts",
        )
    assert not client.requests


@pytest.mark.asyncio
async def test_prompt_compiler_rejects_missing_framework_sections_and_downgrades_bad_sources(
    tmp_path: Path,
) -> None:
    incomplete = StaticClient([compiled_problem("Current task statement\nProve the theorem.")])
    incomplete_result = await compile_prompt(
        client=incomplete,
        problem_text="Prove P.",
        framework_path=FRAMEWORK,
        prompts_dir=tmp_path / "incomplete",
    )
    assert incomplete_result.prompt_validation.passed is True
    assert incomplete_result.prompt_validation.warnings

    bad_source = compiled_problem()
    bad_source.source_ledger = [
        {
            "title": "Asserted paper",
            "stable_identifier": "paper-123",
            "verified": True,
            "evidence": "a model said the publisher confirms it",
        }
    ]
    client = StaticClient([bad_source])
    result = await compile_prompt(
        client=client,
        problem_text="Prove P.",
        framework_path=FRAMEWORK,
        prompts_dir=tmp_path / "bad-source",
    )
    assert result.compiled_problem.literature_status is LiteratureStatus.UNKNOWN
    assert result.source_ledger == []
    assert "removed after one bounded repair" in result.source_verification.warnings[0]
    assert len(client.requests) == 2


@pytest.mark.asyncio
async def test_prompt_compiler_uses_one_small_source_ledger_repair(tmp_path: Path) -> None:
    malformed = compiled_problem()
    malformed_entry = SourceLedgerEntry.model_validate(
        {
            "title": "Repairable fixture source",
            "stable_identifier": "not canonical",
            "evidence": "The fixture theorem is stated in this source.",
        }
    )
    malformed.source_ledger = [malformed_entry]
    source_id = malformed_entry.source_id
    repair = SourceLedgerRepair(
        source_ledger=[
            SourceLedgerEntry(
                source_id=source_id,
                title="Repairable fixture source",
                identifiers=["doi:10.5555/12345678"],
                evidence_claims=[
                    {
                        "claim": "The fixture theorem is stated in this source.",
                        "source_ids": [source_id],
                    }
                ],
            )
        ]
    )
    client = StaticClient([malformed, repair], tool_metadata=web_source_metadata())

    result = await compile_prompt(
        client=client,
        problem_text="Prove P.",
        framework_path=FRAMEWORK,
        prompts_dir=tmp_path,
    )

    assert result.source_ledger[0]["verified"] is True
    assert result.calls.model_calls == 2
    assert len(client.requests) == 2
    assert client.requests[1].settings.reasoning_effort == "medium"
    assert client.requests[1].settings.maximum_web_search_calls == 4
    assert client.requests[1].settings.max_output_tokens == 8_000


@pytest.mark.asyncio
async def test_prompt_compiler_allows_a_verified_empty_source_ledger(tmp_path: Path) -> None:
    result = await compile_prompt(
        client=StaticClient([compiled_problem()]),
        problem_text="Prove an elementary self-contained identity.",
        framework_path=FRAMEWORK,
        prompts_dir=tmp_path,
    )
    assert result.source_ledger == []

    sourced = compiled_problem()
    sourced.source_ledger = [
        {
            "title": "Verified fixture source",
            "stable_identifier": "10.5555/12345678",
            "url": "https://doi.org/10.5555/12345678",
            "verified": True,
            "evidence": "https://doi.org/10.5555/12345678",
        }
    ]
    downgraded = await compile_prompt(
        client=StaticClient([sourced]),
        problem_text="Prove a source-dependent fixture statement.",
        framework_path=FRAMEWORK,
        prompts_dir=tmp_path / "missing-provider-source",
    )
    assert downgraded.compiled_problem.literature_status is LiteratureStatus.UNKNOWN
    assert downgraded.source_ledger[0]["verified"] is False
    assert downgraded.source_verification.warnings
    sourced_result = await compile_prompt(
        client=StaticClient([sourced], tool_metadata=web_source_metadata()),
        problem_text="Prove a source-dependent fixture statement.",
        framework_path=FRAMEWORK,
        prompts_dir=tmp_path / "sourced",
    )
    assert sourced_result.source_ledger[0]["verified"] is True

    unledgered = compiled_problem(covered_compiled_prompt(f"See {VERIFIED_SOURCE_URL}."))
    unledgered_result = await compile_prompt(
        client=StaticClient([unledgered], tool_metadata=web_source_metadata()),
        problem_text="Prove the cited fixture statement.",
        framework_path=FRAMEWORK,
        prompts_dir=tmp_path / "unledgered",
    )
    assert unledgered_result.prompt_validation.passed is True
    assert unledgered_result.prompt_validation.warnings


@pytest.mark.asyncio
async def test_unavailable_required_literature_source_is_quarantined_not_aborted(
    tmp_path: Path,
) -> None:
    compiled = compiled_problem()
    compiled.literature_status = LiteratureStatus.PARTIALLY_RESOLVED
    compiled.literature_resolution_summary = "The cited preprint claims one special case."
    claim = "The cited preprint proves the special case."
    compiled.compiled_prompt = covered_compiled_prompt(claim)
    compiled.source_ledger = [
        SourceLedgerEntry(
            source_id="literature-lead",
            title="Unavailable preprint",
            identifiers=["arxiv:2401.01234"],
            evidence_claims=[{"claim": claim, "source_ids": ["literature-lead"]}],
            purpose=SourcePurpose.LITERATURE_SUPPORT,
            required_for_claim=True,
        )
    ]

    result = await compile_prompt(
        client=StaticClient([compiled]),
        problem_text="Prove the independently specified fixture theorem.",
        framework_path=FRAMEWORK,
        prompts_dir=tmp_path,
        source_verifier=OfflineIdentifierVerifier(),
    )

    assert result.compiled_problem.literature_status is LiteratureStatus.UNKNOWN
    assert result.compiled_problem.source_ledger[0].verified is False
    assert "Unverified literature lead" in result.compiled_prompt
    assert result.source_verification.warnings


@pytest.mark.asyncio
async def test_malformed_required_literature_source_is_quarantined_not_aborted(
    tmp_path: Path,
) -> None:
    compiled = compiled_problem()
    compiled.literature_status = LiteratureStatus.PARTIALLY_RESOLVED
    compiled.literature_resolution_summary = "An incomplete source record claims a special case."
    compiled.source_ledger = [
        SourceLedgerEntry(
            source_id="malformed-literature-lead",
            title="Incomplete literature lead",
            identifiers=[],
            evidence_claims=[],
            purpose=SourcePurpose.LITERATURE_SUPPORT,
            required_for_claim=True,
        )
    ]

    result = await compile_prompt(
        client=StaticClient([compiled]),
        problem_text="Prove the independently specified fixture theorem.",
        framework_path=FRAMEWORK,
        prompts_dir=tmp_path,
    )

    assert result.compiled_problem.literature_status is LiteratureStatus.UNKNOWN
    assert result.compiled_problem.source_ledger == []
    assert any(
        "removed after one bounded repair" in warning
        for warning in result.source_verification.warnings
    )


@pytest.mark.asyncio
async def test_typed_worker_report_drives_graph_integration_without_model_patch(
    tmp_path: Path,
) -> None:
    problem = tmp_path / "problem.md"
    problem.write_text("Prove the fixture theorem.", encoding="utf-8")
    graph = KnowledgeGraph(tmp_path, "resilience")
    problem_id, _ = graph.initialize_problem(
        source_path=problem,
        problem_text=problem.read_text(encoding="utf-8"),
        run_id="run-graph-prior",
    )
    graph.initialize_problem(
        source_path=problem,
        problem_text=problem.read_text(encoding="utf-8"),
        run_id="run-graph-warning",
    )
    compiled = compiled_problem()
    graph.record_compiled_problem(
        problem_id=problem_id,
        run_id="run-graph-warning",
        compiled_problem=compiled.model_dump(mode="json"),
    )

    class TypedScientificClient(SuccessfulResearchClient):
        def __init__(self) -> None:
            super().__init__()
            self.initial_graph_review_seen = False

        async def generate_structured(
            self, request: ModelRequest, output_type: type[Any]
        ) -> ModelResult[Any]:
            payload = json.loads(request.input_text)
            if output_type is ResearchCoordinatorDecision and payload["initial_portfolio"]:
                memory = payload["knowledge_graph_memory"]
                assert memory["review_required_before_delegation"] is True
                assert memory["node_count"] > 0
                assert memory["graph_root"].endswith("/resilience")
                assert payload["graph_node_summaries"]
                assert payload["activation_context"]["kind"] == "existing_graph_bootstrap"
                assert (
                    payload["activation_context"]["provider_conversation_memory_assumed"] is False
                )
                self.initial_graph_review_seen = True
            result = await super().generate_structured(request, output_type)
            if output_type is ResearchCoordinatorDecision:
                memory = payload["knowledge_graph_memory"]
                target_id = next(
                    (
                        item["matek_id"]
                        for item in payload["graph_node_summaries"]
                        if item["node_type"] == "claim"
                    ),
                    memory["problem_id"],
                )
                result.parsed.assignments = [
                    assignment.model_copy(update={"target_node_ids": [target_id]})
                    for assignment in result.parsed.assignments
                ]
                result.parsed.rationale = (
                    f"Graph review {memory['graph_revision']}: " + result.parsed.rationale
                )
            if output_type is ResearchWorkerReport:
                assert self.initial_graph_review_seen
                assert "graph_patch" not in request.instructions
                assert "graph_patch" not in request.input_text
                assert "graph_patch_contract" not in payload
                assert "graph_patch" not in payload
                assert "scientific_result_contract" in payload
                result.parsed.results = [
                    scientific_result.model_copy(
                        update={"exact_statement": compiled.normalized_statement}
                    )
                    for scientific_result in result.parsed.results
                ]
            return result

    client = TypedScientificClient()
    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled,
        research_dir=tmp_path / "research",
        knowledge_graph=graph,
        graph_problem_id=problem_id,
        run_id="run-graph-warning",
    )

    assert result.worker_reports
    assert client.initial_graph_review_seen
    assert result.accepted_for_manuscript
    assert result.acceptance_gate is not None
    assert result.acceptance_gate.graph_support_bindings_sha256 is not None
    candidate_input = json.loads(
        next((tmp_path / "research" / "candidate" / "attempts").glob("*/input.json")).read_text(
            encoding="utf-8"
        )
    )
    canonical_support = candidate_input["candidate_canonical_graph_support"]
    assert canonical_support["blocking_obligations"] == []
    assert {node["node_type"] for node in canonical_support["bindings"][0]["support_nodes"]} == {
        "claim",
        "proof_attempt",
        "derivation",
    }
    assert not [
        issue for issue in result.execution_issues if issue.event_kind == "graph_mutation_rejected"
    ]
    patch_records = list((tmp_path / "research" / "graph-patches").glob("*.json"))
    assert patch_records
    for path in patch_records:
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["model_authored_patch"] is False
        assert "proposed_patch_json" not in record
    raw_reports = list((tmp_path / "research" / "workers").glob("*.raw.json"))
    assert raw_reports
    raw_payload = json.loads(raw_reports[0].read_text(encoding="utf-8"))
    normalized_path = raw_reports[0].with_name(
        raw_reports[0].name.removesuffix(".raw.json") + ".json"
    )
    normalized_payload = json.loads(normalized_path.read_text(encoding="utf-8"))
    assert raw_payload["schema_version"] == 2
    assert normalized_payload["schema_version"] == 2
    evidence_path = (
        tmp_path / "research" / "worker-evidence" / f"{normalized_payload['assignment_id']}.json"
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["raw_report"] == raw_payload
    assert evidence["normalized_report"] == normalized_payload


@pytest.mark.asyncio
async def test_gap_free_intermediate_results_run_live_blind_independent_audits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    problem = tmp_path / "problem.md"
    problem.write_text("Prove the fixture theorem.", encoding="utf-8")
    graph = KnowledgeGraph(tmp_path, "lemma-live")
    problem_id, _ = graph.initialize_problem(
        source_path=problem,
        problem_text=problem.read_text(encoding="utf-8"),
        run_id="run-lemma-live",
    )
    compiled = compiled_problem()
    graph.record_compiled_problem(
        problem_id=problem_id,
        run_id="run-lemma-live",
        compiled_problem=compiled.model_dump(mode="json"),
    )
    target_id = graph.main_claim_id(problem_id)
    dependency_id = "CLM-0000000000000000D001"
    dependency_tasks, _, _ = graph.record_assignment_tasks(
        problem_id=problem_id,
        run_id="run-lemma-live",
        decision_id=900,
        assignments=[
            {
                "id": "dependency-seed",
                "approach_family": "prior-audit",
                "task": "Record the already audited fixture premise.",
                "expected_output": "One frozen dependency lemma.",
                "target_node_ids": [target_id],
            }
        ],
    )
    dependency_task_id = dependency_tasks["dependency-seed"]
    dependency_created = graph.merge_patch(
        GraphPatch(
            base_graph_revision=graph.load_state().revision,
            run_id="run-lemma-live",
            task_id=dependency_task_id,
            agent_role="research-auditor-fixture",
            create_nodes=[
                GraphNodeCreate(
                    matek_id=dependency_id,
                    node_type=NodeType.CLAIM,
                    claim_type=ClaimType.LEMMA,
                    title="Audited fixture premise",
                    body="## Exact statement\n\nThe fixture premise holds.",
                    epistemic_status=EpistemicStatus.AUDIT_PASSED,
                )
            ],
        ),
        problem_id=problem_id,
        operation_id="create-audited-fixture-premise",
    )
    assert dependency_created.committed

    class IntermediateAuditClient:
        def __init__(self) -> None:
            self.calls = 0
            self.audit_inputs: list[str] = []
            self.response_ids_by_request: dict[tuple[str, str, str], str] = {}

        async def generate_structured(
            self,
            request: ModelRequest,
            output_type: type[Any],
        ) -> ModelResult[Any]:
            request_identity = (
                output_type.__name__,
                request.instructions,
                request.input_text,
            )
            response_id = self.response_ids_by_request.get(request_identity)
            if response_id is None:
                self.calls += 1
                response_id = f"lemma-live-{self.calls}"
                self.response_ids_by_request[request_identity] = response_id
            payload = json.loads(request.input_text)
            if output_type is ResearchCoordinatorDecision:
                assert payload["literature_refresh"]["literature_status"] == "unknown"
                assert payload["literature_refresh"]["verified_source_ledger"] == []
                assert "cannot rewrite" in payload["literature_refresh"]["instruction"]
                assert payload["scientific_phase_state"]["phase"] == "explore"
                assert payload["compiled_prompt"] == compiled.compiled_prompt
                assert payload["claim_contract"] == compiled.claim_contract.as_dict()
                assert payload["exact_target_policy"]["acceptance_requires_exact_claim_contract"]
                assignments = (
                    [
                        ResearchAssignment(
                            id=f"lemma-worker-{index}",
                            approach_family=f"family-{index}",
                            task=f"Prove restricted lemma {index}",
                            expected_output="A complete scoped lemma.",
                            target_node_ids=[target_id],
                        )
                        for index in range(1, 5)
                    ]
                    if payload["initial_portfolio"]
                    else []
                )
                parsed: BaseModel = ResearchCoordinatorDecision(
                    decision_id=payload["decision_id"],
                    after_event_sequence=payload["after_event_sequence"],
                    assignments=assignments,
                    rationale=(
                        f"Graph review {payload['knowledge_graph_memory']['graph_revision']}: "
                        "audit exact restricted lemmas."
                    ),
                    stop_recommended=not assignments,
                    stop_reason=(
                        None if assignments else "The configured fixture budget is exhausted."
                    ),
                    stop_category="budget",
                )
            elif output_type is ResearchWorkerReport:
                assignment = payload["assignment"]
                statement = f"Every object in restricted class {assignment['id']} has property P."
                parsed = ResearchWorkerReport(
                    assignment_id=assignment["id"],
                    results=[
                        ScientificResult(
                            local_key="restricted-lemma",
                            kind=ScientificResultKind.LEMMA,
                            exact_statement=statement,
                            scope=ScientificScope.BRANCH,
                            proof_or_certificate="A complete induction over the restricted class.",
                            dependency_node_ids=[dependency_id],
                            target_node_ids=[target_id],
                            disposition=ScientificResultDisposition.PROPOSED_COMPLETE,
                        )
                    ],
                    branch_outcome=BranchOutcome.PROGRESS,
                    mechanism=assignment["task"],
                )
            elif output_type is LemmaAuditResponse:
                self.audit_inputs.append(request.input_text)
                packet = payload["blind_lemma_audit_packet"]
                role = LemmaAuditRole(payload["audit_role"])
                parsed = LemmaAuditResponse(
                    audit_role=role,
                    audit_id=packet["audit_id"],
                    statement_sha256=packet["statement_sha256"],
                    decision=LemmaAuditDecision.PASS,
                    statement_aligned=True,
                    proof_valid=True if role is LemmaAuditRole.VERIFIER else None,
                    proof_step_ids_checked=[item["step_id"] for item in packet["proof_steps"]],
                    source_artifact_ids_checked=[
                        item["artifact_id"] for item in packet["source_artifacts"]
                    ],
                    checks_performed=["Checked the exact statement and every supplied step."],
                    boundary_or_adversarial_cases=(
                        ["Checked the smallest and empty-boundary cases."]
                        if role is LemmaAuditRole.FALSIFIER
                        else []
                    ),
                    rationale="The independent role found no unresolved defect.",
                )
            else:  # pragma: no cover - this fixture permits no terminal candidate lane
                raise AssertionError(output_type)
            return ModelResult(parsed=parsed, response_id=response_id)

    client = IntermediateAuditClient()
    research_dir = tmp_path / ".matek" / "runs" / "run-lemma-live" / "research"
    original_record_lemma_audit = graph.record_lemma_audit
    fail_first_graph_commit = True

    def flaky_record_lemma_audit(**kwargs: Any) -> Any:
        nonlocal fail_first_graph_commit
        if fail_first_graph_commit:
            fail_first_graph_commit = False
            raise RuntimeError("simulated crash-boundary graph interruption")
        return original_record_lemma_audit(**kwargs)

    monkeypatch.setattr(graph, "record_lemma_audit", flaky_record_lemma_audit)
    result = await run_adaptive_research(
        client=client,  # type: ignore[arg-type]
        compiled_problem=compiled,
        research_dir=research_dir,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=4,
            maximum_pending_assignments=4,
            maximum_coordinator_decisions=2,
        ),
        knowledge_graph=graph,
        graph_problem_id=problem_id,
        run_id="run-lemma-live",
    )

    assert result.outcome is ResearchOutcome.BUDGET_EXHAUSTED
    gates = sorted((research_dir / "lemma-audits").glob("lemma-*/gate.json"))
    assert len(gates) == 4
    assert len(client.audit_inputs) == 8
    assert all("origin_confidence" not in item for item in client.audit_inputs)
    assert all("desired_verdict" not in item for item in client.audit_inputs)
    scheduler_path = research_dir / "coordinator" / "state.json"
    scheduler = json.loads(scheduler_path.read_text(encoding="utf-8"))
    pending_graph_records = [
        (assignment, assignment["intermediate_lemma_audits"][0])
        for assignment in scheduler["assignments"]
        if not assignment["intermediate_lemma_audits"][0]["graph_recorded"]
    ]
    assert len(pending_graph_records) == 1
    pending_assignment, pending_audit = pending_graph_records[0]
    legacy_gate = research_dir / pending_audit["gate_path"]
    legacy_input = legacy_gate.parent / "input.json"
    input_payload = json.loads(legacy_input.read_text(encoding="utf-8"))
    input_payload["schema_version"] = 1
    input_payload.pop("execution_context_ids")
    atomic_write_json(legacy_input, input_payload)
    for response_path in sorted((legacy_gate.parent / "responses").glob("*.json")):
        response_payload = json.loads(response_path.read_text(encoding="utf-8"))
        response_payload["schema_version"] = 1
        response_payload.pop("execution_context_id")
        response_payload.pop("provider_session_id")
        atomic_write_json(response_path, response_payload)
    gate_payload = json.loads(legacy_gate.read_text(encoding="utf-8"))
    gate_payload["schema_version"] = 1
    gate_payload.pop("execution_context_ids")
    gate_payload.pop("provider_session_ids")
    atomic_write_json(legacy_gate, gate_payload)
    pending_audit["gate_sha256"] = hashlib.sha256(legacy_gate.read_bytes()).hexdigest()
    scheduler["phase"] = "running"
    scheduler["stop_reason"] = None
    scheduler["stop_category"] = None
    scheduler["final_outcome"] = None
    scheduler["final_obligations"] = []
    scheduler["final_strongest_result"] = ""
    atomic_write_json(scheduler_path, scheduler)

    calls_before_resume = len(client.audit_inputs)
    resumed = await run_adaptive_research(
        client=client,  # type: ignore[arg-type]
        compiled_problem=compiled,
        research_dir=research_dir,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=4,
            maximum_pending_assignments=4,
            maximum_coordinator_decisions=2,
        ),
        knowledge_graph=graph,
        graph_problem_id=problem_id,
        run_id="run-lemma-live",
    )

    assert resumed.outcome is ResearchOutcome.BUDGET_EXHAUSTED
    assert len(client.audit_inputs) == calls_before_resume + 2
    assert json.loads(legacy_gate.read_text(encoding="utf-8"))["schema_version"] == 2
    assert (legacy_gate.parent / "legacy-v1" / "manifest.json").is_file()
    scheduler = json.loads(scheduler_path.read_text(encoding="utf-8"))
    assert all(
        assignment["intermediate_lemma_audits"][0]["graph_recorded"]
        for assignment in scheduler["assignments"]
    )
    assert pending_assignment["assignment"]["id"] in {
        assignment["assignment"]["id"] for assignment in scheduler["assignments"]
    }
    # Four newly audited intermediates plus the pre-audited dependency fixture.
    assert len(graph.frontier(problem_id).strongest_audited_results) == 5

    dependency_changed = graph.merge_patch(
        GraphPatch(
            base_graph_revision=graph.load_state().revision,
            run_id="run-lemma-live",
            task_id=dependency_task_id,
            agent_role="research-auditor-fixture",
            update_nodes=[
                GraphNodeUpdate(
                    matek_id=dependency_id,
                    body="## Exact statement\n\nThe strengthened fixture premise holds.",
                    reason="Exercise the post-audit dependency-version boundary.",
                )
            ],
        ),
        problem_id=problem_id,
        operation_id="mutate-audited-fixture-premise",
    )
    assert dependency_changed.committed
    first_gate = gates[0]
    first_nomination = first_gate.parent / "nomination.json"
    with pytest.raises(GraphValidationError, match=r"no longer live|changed after audit"):
        graph.record_lemma_audit(
            problem_id=problem_id,
            run_id="run-lemma-live",
            nomination=json.loads(first_nomination.read_text(encoding="utf-8")),
            gate=json.loads(first_gate.read_text(encoding="utf-8")),
            source_artifact=(
                ".matek/runs/run-lemma-live/research/lemma-audits/"
                f"{first_gate.parent.name}/gate.json"
            ),
        )


@pytest.mark.asyncio
async def test_intermediate_lemma_resume_reuses_frozen_nomination_and_only_missing_role(
    tmp_path: Path,
) -> None:
    problem = tmp_path / "problem.md"
    problem.write_text("Prove the fixture theorem.", encoding="utf-8")
    graph = KnowledgeGraph(tmp_path, "lemma-missing-role")
    run_id = "run-lemma-missing-role"
    problem_id, _ = graph.initialize_problem(
        source_path=problem,
        problem_text=problem.read_text(encoding="utf-8"),
        run_id=run_id,
    )
    compiled = compiled_problem()
    graph.record_compiled_problem(
        problem_id=problem_id,
        run_id=run_id,
        compiled_problem=compiled.model_dump(mode="json"),
    )
    target_id = graph.main_claim_id(problem_id)
    dependency_id = "CLM-0000000000000000D101"
    dependency_tasks, _, _ = graph.record_assignment_tasks(
        problem_id=problem_id,
        run_id=run_id,
        decision_id=901,
        assignments=[
            {
                "id": "missing-role-dependency",
                "approach_family": "prior-audit",
                "task": "Record a trusted fixture premise.",
                "expected_output": "One frozen dependency lemma.",
                "target_node_ids": [target_id],
            }
        ],
    )
    dependency_task_id = dependency_tasks["missing-role-dependency"]
    dependency_created = graph.merge_patch(
        GraphPatch(
            base_graph_revision=graph.load_state().revision,
            run_id=run_id,
            task_id=dependency_task_id,
            agent_role="research-auditor-fixture",
            create_nodes=[
                GraphNodeCreate(
                    matek_id=dependency_id,
                    node_type=NodeType.CLAIM,
                    claim_type=ClaimType.LEMMA,
                    title="Trusted missing-role fixture premise",
                    body="## Exact statement\n\nThe frozen fixture premise holds.",
                    epistemic_status=EpistemicStatus.AUDIT_PASSED,
                )
            ],
        ),
        problem_id=problem_id,
        operation_id="create-missing-role-fixture-premise",
    )
    assert dependency_created.committed

    class MissingRoleClient:
        def __init__(self) -> None:
            self.calls = 0
            self.failed_falsifier = False
            self.failed_audit_id: str | None = None
            self.audit_attempts: list[tuple[str, LemmaAuditRole]] = []

        async def generate_structured(
            self,
            request: ModelRequest,
            output_type: type[Any],
        ) -> ModelResult[Any]:
            self.calls += 1
            payload = json.loads(request.input_text)
            if output_type is ResearchCoordinatorDecision:
                assignments = (
                    [
                        ResearchAssignment(
                            id=f"missing-role-worker-{index}",
                            approach_family=f"family-{index}",
                            task=f"Investigate fixture route {index}.",
                            expected_output="A complete restricted lemma or exact obstruction.",
                            target_node_ids=[target_id],
                        )
                        for index in range(1, 5)
                    ]
                    if payload["initial_portfolio"]
                    else []
                )
                parsed: BaseModel = ResearchCoordinatorDecision(
                    decision_id=payload["decision_id"],
                    after_event_sequence=payload["after_event_sequence"],
                    assignments=assignments,
                    rationale=(
                        f"Graph review {payload['knowledge_graph_memory']['graph_revision']}: "
                        "exercise frozen missing-role lemma-audit resume."
                    ),
                    stop_recommended=not assignments,
                    stop_reason=None if assignments else "The fixture work is complete.",
                    stop_category="budget",
                )
            elif output_type is ResearchWorkerReport:
                assignment_id = payload["assignment"]["id"]
                if assignment_id == "missing-role-worker-1":
                    parsed = ResearchWorkerReport(
                        assignment_id=assignment_id,
                        results=[
                            ScientificResult(
                                local_key="frozen-intermediate",
                                kind=ScientificResultKind.LEMMA,
                                exact_statement=(
                                    "Every object in the frozen restricted class has property P."
                                ),
                                scope=ScientificScope.BRANCH,
                                proof_or_certificate=(
                                    "A complete induction proves the restricted statement."
                                ),
                                dependency_node_ids=[dependency_id],
                                target_node_ids=[target_id],
                                disposition=ScientificResultDisposition.PROPOSED_COMPLETE,
                            )
                        ],
                        branch_outcome=BranchOutcome.PROGRESS,
                        mechanism="Complete the frozen restricted induction.",
                    )
                else:
                    parsed = ResearchWorkerReport(
                        assignment_id=assignment_id,
                        results=[],
                        unresolved_obligations=[
                            ScientificObligationDeclaration(
                                local_key="unused-route",
                                exact_statement="Complete this unused fixture route.",
                                conclusion="Complete this unused fixture route.",
                            )
                        ],
                        branch_outcome=BranchOutcome.BLOCKED,
                        mechanism="No result on this unused fixture route.",
                    )
            elif output_type is LemmaAuditResponse:
                packet = payload["blind_lemma_audit_packet"]
                role = LemmaAuditRole(payload["audit_role"])
                audit_id = packet["audit_id"]
                self.audit_attempts.append((audit_id, role))
                if role is LemmaAuditRole.FALSIFIER and not self.failed_falsifier:
                    self.failed_falsifier = True
                    self.failed_audit_id = audit_id
                    raise RuntimeError("fixture falsifier transport interruption")
                decision = (
                    LemmaAuditDecision.BLOCKED
                    if role is LemmaAuditRole.FALSIFIER
                    else LemmaAuditDecision.PASS
                )
                parsed = LemmaAuditResponse(
                    audit_role=role,
                    audit_id=audit_id,
                    statement_sha256=packet["statement_sha256"],
                    decision=decision,
                    statement_aligned=True,
                    proof_valid=True if role is LemmaAuditRole.VERIFIER else None,
                    proof_step_ids_checked=[item["step_id"] for item in packet["proof_steps"]],
                    source_artifact_ids_checked=[
                        item["artifact_id"] for item in packet["source_artifacts"]
                    ],
                    checks_performed=["Checked the frozen statement and supplied evidence."],
                    boundary_or_adversarial_cases=(
                        ["The final boundary case remains unresolved."]
                        if role is LemmaAuditRole.FALSIFIER
                        else []
                    ),
                    rationale=(
                        "The boundary case requires additional mathematical evidence."
                        if decision is LemmaAuditDecision.BLOCKED
                        else "The supplied proof is complete."
                    ),
                    obligations=(
                        ["Resolve the final boundary case in the frozen intermediate lemma."]
                        if decision is LemmaAuditDecision.BLOCKED
                        else []
                    ),
                )
            else:  # pragma: no cover - this fixture has no main candidate
                raise AssertionError(output_type)
            return ModelResult(parsed=parsed, response_id=f"missing-role-{self.calls}")

    client = MissingRoleClient()
    research_dir = tmp_path / ".matek" / "runs" / run_id / "research"
    workflow_settings = ResearchWorkflowSettings(
        minimum_initial_assignments=4,
        maximum_concurrent_agents=4,
        maximum_pending_assignments=4,
        maximum_coordinator_decisions=2,
    )
    interrupted = await run_adaptive_research(
        client=client,  # type: ignore[arg-type]
        compiled_problem=compiled,
        research_dir=research_dir,
        workflow_settings=workflow_settings,
        knowledge_graph=graph,
        graph_problem_id=problem_id,
        run_id=run_id,
    )

    assert interrupted.outcome is ResearchOutcome.PAUSED_RETRIABLE
    assert interrupted.pause_reason == "LEMMA_AUDIT_INCOMPLETE"
    assert client.failed_audit_id is not None
    audit_id = client.failed_audit_id
    audit_dir = research_dir / "lemma-audits" / audit_id
    nomination_path = audit_dir / "nomination.json"
    gate_path = audit_dir / "gate.json"
    nomination_before = nomination_path.read_bytes()
    frozen_revision = json.loads(nomination_before)["current_graph_revision"]
    gate_payload = json.loads(gate_path.read_text(encoding="utf-8"))
    assert gate_payload["missing_roles"] == [LemmaAuditRole.FALSIFIER.value]
    assert (audit_dir / "responses" / f"{LemmaAuditRole.VERIFIER.value}.json").is_file()
    assert not (audit_dir / "responses" / f"{LemmaAuditRole.FALSIFIER.value}.json").exists()
    scheduler_path = research_dir / "coordinator" / "state.json"
    scheduler = json.loads(scheduler_path.read_text(encoding="utf-8"))
    frozen_record = next(
        audit
        for assignment in scheduler["assignments"]
        for audit in assignment["intermediate_lemma_audits"]
        if audit["nomination_id"] == audit_id
    )
    assert not frozen_record["graph_recorded"]
    assert not any(node.metadata.get("matek_audit_id") == audit_id for node in graph.load_nodes())

    unrelated_change = graph.merge_patch(
        GraphPatch(
            base_graph_revision=graph.load_state().revision,
            run_id=run_id,
            task_id=dependency_task_id,
            agent_role="research-auditor-fixture",
            create_nodes=[
                GraphNodeCreate(
                    matek_id="CLM-0000000000000000D102",
                    node_type=NodeType.CLAIM,
                    claim_type=ClaimType.LEMMA,
                    title="Unrelated post-interruption claim",
                    body="## Exact statement\n\nThis unrelated fixture claim remains open.",
                    epistemic_status=EpistemicStatus.OPEN,
                )
            ],
        ),
        problem_id=problem_id,
        operation_id="create-unrelated-post-interruption-claim",
    )
    assert unrelated_change.committed
    assert graph.load_state().revision != frozen_revision

    verifier_attempts_before = client.audit_attempts.count((audit_id, LemmaAuditRole.VERIFIER))
    falsifier_attempts_before = client.audit_attempts.count((audit_id, LemmaAuditRole.FALSIFIER))
    resumed = await run_adaptive_research(
        client=client,  # type: ignore[arg-type]
        compiled_problem=compiled,
        research_dir=research_dir,
        workflow_settings=workflow_settings,
        knowledge_graph=graph,
        graph_problem_id=problem_id,
        run_id=run_id,
    )

    assert resumed.outcome is not ResearchOutcome.PAUSED_RETRIABLE
    assert nomination_path.read_bytes() == nomination_before
    assert client.audit_attempts.count((audit_id, LemmaAuditRole.VERIFIER)) == (
        verifier_attempts_before
    )
    assert client.audit_attempts.count((audit_id, LemmaAuditRole.FALSIFIER)) == (
        falsifier_attempts_before + 1
    )
    gate_payload = json.loads(gate_path.read_text(encoding="utf-8"))
    assert gate_payload["status"] == "blocked"
    assert gate_payload["missing_roles"] == []
    scheduler = json.loads(scheduler_path.read_text(encoding="utf-8"))
    frozen_record = next(
        audit
        for assignment in scheduler["assignments"]
        for audit in assignment["intermediate_lemma_audits"]
        if audit["nomination_id"] == audit_id
    )
    assert frozen_record["graph_recorded"]
    assert sum(node.metadata.get("matek_audit_id") == audit_id for node in graph.load_nodes()) == 1
    replayed_blocked_commit = graph.record_lemma_audit(
        problem_id=problem_id,
        run_id=run_id,
        nomination=json.loads(nomination_path.read_text(encoding="utf-8")),
        gate=json.loads(gate_path.read_text(encoding="utf-8")),
        source_artifact=(f".matek/runs/{run_id}/research/lemma-audits/{audit_id}/gate.json"),
    )
    assert replayed_blocked_commit.status == "already_applied"

    # Emulate a process dying after the graph service committed an AUDIT_FAILED
    # mutation but before the scheduler could checkpoint graph_recorded=True.
    failed_nomination_id = "lemma-failed-graph-commit-replay"
    failed_nomination = LemmaNomination.model_validate_json(
        nomination_path.read_text(encoding="utf-8")
    ).model_copy(update={"nomination_id": failed_nomination_id})
    failed_audit_dir = research_dir / "lemma-audits" / failed_nomination_id
    atomic_write_json(failed_audit_dir / "nomination.json", failed_nomination)

    class FailedGateClient:
        def __init__(self, role: LemmaAuditRole) -> None:
            self.role = role

        async def generate_structured(
            self,
            request: ModelRequest,
            output_type: type[LemmaAuditResponse],
        ) -> ModelResult[LemmaAuditResponse]:
            packet = json.loads(request.input_text)["blind_lemma_audit_packet"]
            decision = (
                LemmaAuditDecision.FAIL
                if self.role is LemmaAuditRole.VERIFIER
                else LemmaAuditDecision.PASS
            )
            return ModelResult(
                parsed=LemmaAuditResponse(
                    audit_role=self.role,
                    audit_id=packet["audit_id"],
                    statement_sha256=packet["statement_sha256"],
                    decision=decision,
                    statement_aligned=True,
                    proof_valid=False if self.role is LemmaAuditRole.VERIFIER else None,
                    proof_step_ids_checked=[item["step_id"] for item in packet["proof_steps"]],
                    source_artifact_ids_checked=[
                        item["artifact_id"] for item in packet["source_artifacts"]
                    ],
                    checks_performed=["Rechecked every frozen proof step."],
                    boundary_or_adversarial_cases=(
                        ["Checked the smallest boundary instance."]
                        if self.role is LemmaAuditRole.FALSIFIER
                        else []
                    ),
                    rationale=(
                        "The verifier found a decisive proof defect."
                        if decision is LemmaAuditDecision.FAIL
                        else "No additional falsification was found."
                    ),
                    obligations=(
                        ["Repair the decisive proof defect."]
                        if decision is LemmaAuditDecision.FAIL
                        else []
                    ),
                ),
                response_id=f"failed-gate-{self.role.value}",
            )

    failed_gate = await run_lemma_audit(
        failed_nomination,
        failed_audit_dir,
        verifier_client=FailedGateClient(LemmaAuditRole.VERIFIER),
        falsifier_client=FailedGateClient(LemmaAuditRole.FALSIFIER),
        settings=ModelSettings(web_search=False),
    )
    failed_nomination_payload = json.loads(
        (failed_audit_dir / "nomination.json").read_text(encoding="utf-8")
    )
    failed_gate_payload = failed_gate.model_dump(mode="json")
    failed_source = f".matek/runs/{run_id}/research/lemma-audits/{failed_nomination_id}/gate.json"
    first_failed_commit = graph.record_lemma_audit(
        problem_id=problem_id,
        run_id=run_id,
        nomination=failed_nomination_payload,
        gate=failed_gate_payload,
        source_artifact=failed_source,
    )
    assert first_failed_commit.committed
    replayed_failed_commit = graph.record_lemma_audit(
        problem_id=problem_id,
        run_id=run_id,
        nomination=failed_nomination_payload,
        gate=failed_gate_payload,
        source_artifact=failed_source,
    )
    assert replayed_failed_commit.status == "already_applied"

    migration_run_id = f"{run_id}-migration"
    graph.initialize_problem(
        source_path=problem,
        problem_text=problem.read_text(encoding="utf-8"),
        run_id=migration_run_id,
    )
    changed_compiled = compiled.model_dump(mode="json")
    changed_compiled["title"] = "Changed fixture theorem"
    changed_compiled["normalized_statement"] = (
        "The changed fixture theorem has a different conclusion."
    )
    changed_compiled["compiled_prompt"] = "Prove the explicitly changed fixture theorem."
    changed_target = graph.record_compiled_problem(
        problem_id=problem_id,
        run_id=migration_run_id,
        compiled_problem=changed_compiled,
        allow_target_migration=True,
        target_migration_reason="Exercise the frozen claim-cut version boundary.",
    )
    assert changed_target.committed
    with pytest.raises(
        GraphValidationError,
        match=rf"target claim {target_id} changed|no longer live|changed after audit",
    ):
        graph.record_lemma_audit(
            problem_id=problem_id,
            run_id=run_id,
            nomination=failed_nomination_payload,
            gate=failed_gate_payload,
            source_artifact=failed_source,
        )


class SuccessfulResearchClient:
    def __init__(
        self,
        *,
        worker_sources: list[SourceLedgerEntry] | None = None,
        imported_theorems: list[ImportedTheorem] | None = None,
    ) -> None:
        self.calls = 0
        self.active = 0
        self.maximum_active = 0
        self.worker_sources = worker_sources or []
        self.imported_theorems = imported_theorems or []

    async def generate_structured(
        self, request: ModelRequest, output_type: type[Any]
    ) -> ModelResult[Any]:
        self.calls += 1
        response_id = f"research-{self.calls}"
        if output_type is ResearchCoordinatorDecision:
            payload = json.loads(request.input_text)
            if payload["initial_portfolio"]:
                target = payload["minimum_materially_diverse_initial_assignments"]
                parsed: BaseModel = ResearchCoordinatorDecision(
                    decision_id=payload["decision_id"],
                    after_event_sequence=payload["after_event_sequence"],
                    assignments=[
                        ResearchAssignment(
                            id=f"worker-{index}",
                            approach_family=family,
                            task=f"Investigate {family}",
                            expected_output="A formal proof or exact obstruction",
                        )
                        for index, family in enumerate(
                            (
                                "direct",
                                "structural",
                                "counterexample",
                                "literature",
                                "probabilistic",
                                "computational",
                                "inductive",
                                "algebraic",
                                "geometric",
                                "topological",
                                "analytic",
                                "combinatorial",
                                "variational",
                                "spectral",
                                "logical",
                                "formalization-aware",
                            )[:target],
                            start=1,
                        )
                    ],
                    rationale="Independent mechanisms",
                )
            else:
                parsed = ResearchCoordinatorDecision(
                    decision_id=payload["decision_id"],
                    after_event_sequence=payload["after_event_sequence"],
                    assignments=[],
                    rationale="The remaining obligation cannot be resolved offline.",
                    stop_recommended=True,
                    stop_reason="No further admissible research route remains.",
                    stop_category="budget",
                )
        elif output_type is ResearchWorkerReport:
            assignment = json.loads(request.input_text)["assignment"]
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            try:
                await asyncio.sleep(0.01)
            finally:
                self.active -= 1
            parsed = research_worker_report_v1(
                assignment_id=assignment["id"],
                status=WorkerStatus.CANDIDATE_COMPLETE,
                formal_results=[f"Lemma from {assignment['approach_family']}"],
                proof_content="Detailed proof.",
                exact_gap=None,
                sources=self.worker_sources,
                mechanism=assignment["task"],
            )
        elif output_type is CandidateProofPackage:
            parsed = candidate_package().model_copy(
                update={"imported_theorems": self.imported_theorems}, deep=True
            )
        elif output_type is AuditVerdict:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            parsed = passing_audit()
        elif output_type is FinalJudgeVerdict:
            parsed = FinalJudgeVerdict(
                verdict=FinalJudgeDecision.ACCEPTED,
                reasons=["All exact obligations discharged."],
                strongest_result="Fixture theorem",
            )
        else:  # pragma: no cover - a stage adding an unexpected call should fail loudly
            raise AssertionError(output_type)
        return ModelResult(parsed=parsed, response_id=response_id)


SAFE_COMPUTATION_REPLAY = ComputationReplayIsolation(
    filesystem_write_confined=True,
    network_disabled=True,
    description="offline candidate-gate fixture",
)


class CandidateComputationBackend:
    def __init__(self, *, mismatch: bool = False) -> None:
        self.mismatch = mismatch
        self.requests: list[CommandRequest] = []

    async def run(self, request: CommandRequest) -> CommandResult:
        self.requests.append(request)
        output = request.cwd / "outputs" / "certificate.txt"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"wrong-certificate\n" if self.mismatch else b"certificate-v1\n")
        return CommandResult(
            argv=request.argv,
            cwd=request.cwd,
            exit_code=0,
            stdout="checked\n",
            stderr="",
            duration_seconds=0.01,
        )


class CandidateComputationResearchClient(SuccessfulResearchClient):
    def __init__(
        self,
        *,
        supporting_key: str = "finite-certificate",
        computation_scope: ScientificScope = ScientificScope.COMPUTATION,
        link_computation: bool = True,
        transitive_link: bool = False,
    ) -> None:
        super().__init__()
        self.supporting_key = supporting_key
        self.computation_scope = computation_scope
        self.link_computation = link_computation
        self.transitive_link = transitive_link
        self.workspaces: dict[str, Path] = {}
        self.package_calls = 0

    def for_workspace(
        self,
        workspace_root: Path,
        *,
        writable_paths: tuple[Path, ...],
    ) -> CandidateComputationResearchClient:
        assert len(writable_paths) == 1
        # The bound workspace is the assignment root; declared computation evidence lives in
        # its scratch/ child.
        self.workspaces[workspace_root.name] = writable_paths[0] / "scratch"
        return self

    @staticmethod
    def _write_computation_workspace(workspace: Path) -> None:
        files = {
            "code/verify.py": b"# deterministic verifier fixture\n",
            "inputs/data.txt": b"1 2 3\n",
            "outputs/certificate.txt": b"certificate-v1\n",
            "captures/stdout.txt": b"checked\n",
            "captures/stderr.txt": b"",
        }
        for relative, contents in files.items():
            path = workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)

    async def generate_structured(
        self, request: ModelRequest, output_type: type[Any]
    ) -> ModelResult[Any]:
        if output_type is ResearchCoordinatorDecision:
            self.calls += 1
            payload = json.loads(request.input_text)
            if payload["initial_portfolio"]:
                assignments = [
                    ResearchAssignment(
                        id="computed-candidate",
                        approach_family="finite verification",
                        task="Prove the exact theorem using a replayed finite certificate.",
                        expected_output="Exact proof plus replayable certificate.",
                    ),
                    *(
                        ResearchAssignment(
                            id=f"unused-{index}",
                            approach_family=f"unused-{index}",
                            task="Explore an independent route.",
                            expected_output="A proof or exact gap.",
                        )
                        for index in range(1, 4)
                    ),
                ]
                return ModelResult(
                    parsed=ResearchCoordinatorDecision(
                        decision_id=payload["decision_id"],
                        after_event_sequence=payload["after_event_sequence"],
                        assignments=assignments,
                        rationale="Exercise the deterministic computation candidate gate.",
                    ),
                    response_id=f"candidate-computation-{self.calls}",
                )
            return ModelResult(
                parsed=ResearchCoordinatorDecision(
                    decision_id=payload["decision_id"],
                    after_event_sequence=payload["after_event_sequence"],
                    assignments=[],
                    rationale="The fixture stops after the rejected computation candidate.",
                    stop_recommended=True,
                    stop_reason="No further fixture work is configured.",
                    stop_category="budget",
                ),
                response_id=f"candidate-computation-{self.calls}",
            )
        if output_type is ResearchWorkerReport:
            self.calls += 1
            assignment_id = json.loads(request.input_text)["assignment"]["id"]
            if assignment_id != "computed-candidate":
                return ModelResult(
                    parsed=ResearchWorkerReport(
                        assignment_id=assignment_id,
                        results=[],
                        unresolved_obligations=[
                            ScientificObligationDeclaration(
                                local_key="unused-gap",
                                exact_statement="Complete the unused fixture route.",
                                conclusion="Complete the unused fixture route.",
                            )
                        ],
                        branch_outcome=BranchOutcome.BLOCKED,
                        mechanism="Unused fixture route.",
                    ),
                    response_id=f"candidate-computation-{self.calls}",
                )
            workspace = self.workspaces[assignment_id]
            self._write_computation_workspace(workspace)
            report = ResearchWorkerReport(
                assignment_id=assignment_id,
                results=[
                    ScientificResult(
                        local_key="main-proof",
                        kind=ScientificResultKind.LEMMA,
                        exact_statement="Prove P(n) for every n.",
                        scope=ScientificScope.MAIN,
                        proof_or_certificate=(
                            "Reduce the frozen theorem to the finite certificate and verify "
                            "domain completeness."
                        ),
                        dependency_result_keys=(
                            ["domain-reduction"]
                            if self.link_computation and self.transitive_link
                            else ["finite-certificate"]
                            if self.link_computation
                            else []
                        ),
                        disposition=ScientificResultDisposition.PROPOSED_COMPLETE,
                    ),
                    *(
                        [
                            ScientificResult(
                                local_key="domain-reduction",
                                kind=ScientificResultKind.REDUCTION,
                                exact_statement=(
                                    "The frozen theorem follows from the finite certificate."
                                ),
                                scope=ScientificScope.REDUCTION,
                                proof_or_certificate=(
                                    "Verify the exhaustive finite-domain reduction."
                                ),
                                dependency_result_keys=["finite-certificate"],
                                disposition=ScientificResultDisposition.PROPOSED_COMPLETE,
                            )
                        ]
                        if self.transitive_link
                        else []
                    ),
                    ScientificResult(
                        local_key="finite-certificate",
                        kind=ScientificResultKind.COMPUTATION,
                        exact_statement="The deterministic finite verifier accepts every case.",
                        scope=self.computation_scope,
                        proof_or_certificate="The retained certificate and verifier output.",
                        disposition=ScientificResultDisposition.PROPOSED_COMPLETE,
                    ),
                ],
                artifact_manifest=[
                    ScientificArtifactDeclaration(
                        path="outputs/certificate.txt",
                        purpose="Check the finite certificate supporting the exact proof.",
                        supporting_result_keys=[self.supporting_key],
                        command_line=["python3", "code/verify.py", "inputs/data.txt"],
                        input_paths=["code/verify.py", "inputs/data.txt"],
                        stdout_path="captures/stdout.txt",
                        stderr_path="captures/stderr.txt",
                        expected_output="checked\n",
                        replay_recipe="Run the fixed verifier over the frozen finite input.",
                        tool_versions=["python 3.11"],
                    )
                ],
                branch_outcome=BranchOutcome.CANDIDATE_COMPLETE,
                mechanism="Finite reduction with a deterministic certificate.",
            )
            return ModelResult(
                parsed=report,
                response_id=f"candidate-computation-{self.calls}",
            )
        if output_type is CandidateProofPackage:
            self.package_calls += 1
        return await super().generate_structured(request, output_type)


class GraphCandidateComputationResearchClient(CandidateComputationResearchClient):
    async def generate_structured(
        self, request: ModelRequest, output_type: type[Any]
    ) -> ModelResult[Any]:
        result = await super().generate_structured(request, output_type)
        if output_type is ResearchCoordinatorDecision:
            payload = json.loads(request.input_text)
            memory = payload.get("knowledge_graph_memory")
            if isinstance(memory, dict):
                result.parsed.rationale = (
                    f"Graph review {memory['graph_revision']}: " + result.parsed.rationale
                )
            summaries = payload.get("graph_node_summaries", [])
            target_id = next(
                (item["matek_id"] for item in summaries if item.get("node_type") == "claim"),
                None,
            )
            if target_id is not None:
                result.parsed.assignments = [
                    assignment.model_copy(update={"target_node_ids": [target_id]})
                    for assignment in result.parsed.assignments
                ]
        return result


def candidate_computation_events(research_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((research_dir / "events").glob("*.json"))
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "backend", "isolation", "supporting_key", "scope", "status_fragment"),
    [
        (
            "missing",
            None,
            SAFE_COMPUTATION_REPLAY,
            "finite-certificate",
            ScientificScope.COMPUTATION,
            "absent",
        ),
        (
            "unsafe",
            CandidateComputationBackend(),
            ComputationReplayIsolation(
                filesystem_write_confined=False,
                network_disabled=False,
                description="untrusted native fixture",
            ),
            "finite-certificate",
            ScientificScope.COMPUTATION,
            "unsafe_backend",
        ),
        (
            "failed",
            CandidateComputationBackend(mismatch=True),
            SAFE_COMPUTATION_REPLAY,
            "finite-certificate",
            ScientificScope.COMPUTATION,
            "mismatch",
        ),
        (
            "wrong-result-key",
            CandidateComputationBackend(),
            SAFE_COMPUTATION_REPLAY,
            "main-proof",
            ScientificScope.COMPUTATION,
            "not named",
        ),
        (
            "wrong-scope",
            CandidateComputationBackend(),
            SAFE_COMPUTATION_REPLAY,
            "finite-certificate",
            ScientificScope.BRANCH,
            "mathematically admissible",
        ),
    ],
)
async def test_candidate_computation_gate_rejects_untrusted_or_inadmissible_evidence(
    tmp_path: Path,
    case: str,
    backend: CandidateComputationBackend | None,
    isolation: ComputationReplayIsolation,
    supporting_key: str,
    scope: ScientificScope,
    status_fragment: str,
) -> None:
    research_dir = tmp_path / "research"
    client = CandidateComputationResearchClient(
        supporting_key=supporting_key,
        computation_scope=scope,
    )

    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=research_dir,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=1,
            maximum_pending_assignments=4,
            maximum_coordinator_decisions=2,
        ),
        computation_backend=backend,
        computation_replay_isolation=isolation,
    )

    assert case
    assert result.outcome is ResearchOutcome.PAUSED_RETRIABLE
    assert not result.accepted_for_manuscript
    assert result.acceptance_gate is None
    assert client.package_calls == 0
    assert any(status_fragment in obligation for obligation in result.unresolved_obligations)
    rejection_events = [
        event
        for event in candidate_computation_events(research_dir)
        if event["kind"] == "candidate_computation_evidence_rejected"
    ]
    assert len(rejection_events) == 1
    assert any(status_fragment in detail for detail in rejection_events[0]["detail"])


@pytest.mark.asyncio
async def test_valid_computation_replay_still_requires_graph_bound_named_support(
    tmp_path: Path,
) -> None:
    research_dir = tmp_path / "research"
    backend = CandidateComputationBackend()
    client = CandidateComputationResearchClient()

    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=research_dir,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=1,
            maximum_pending_assignments=4,
        ),
        computation_backend=backend,
        computation_replay_isolation=SAFE_COMPUTATION_REPLAY,
    )

    assert result.outcome is ResearchOutcome.PAUSED_RETRIABLE
    assert not result.accepted_for_manuscript
    assert client.package_calls == 0
    assert len(backend.requests) == 1
    candidate_input = next((research_dir / "candidate" / "attempts").glob("*/input.json"))
    candidate_payload = json.loads(candidate_input.read_text(encoding="utf-8"))
    gate_payload = candidate_payload["candidate_computation_gate"]
    assert gate_payload["blocking_obligations"] == []
    assert gate_payload["bindings"][0]["result_local_key"] == "finite-certificate"
    assert any(
        "no active canonical knowledge graph" in obligation
        for obligation in candidate_payload["candidate_canonical_graph_support"][
            "blocking_obligations"
        ]
    )


@pytest.mark.asyncio
async def test_candidate_computation_gate_rejects_unrelated_replayed_result(
    tmp_path: Path,
) -> None:
    research_dir = tmp_path / "research"
    client = CandidateComputationResearchClient(link_computation=False)

    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=research_dir,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=1,
            maximum_pending_assignments=4,
            maximum_coordinator_decisions=2,
        ),
        computation_backend=CandidateComputationBackend(),
        computation_replay_isolation=SAFE_COMPUTATION_REPLAY,
    )

    assert result.outcome is ResearchOutcome.PAUSED_RETRIABLE
    assert client.package_calls == 0
    assert any("unrelated" in obligation for obligation in result.unresolved_obligations)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "support_kind",
    ["external-node", "local-result"],
)
async def test_named_candidate_support_requires_an_active_knowledge_graph(
    tmp_path: Path,
    support_kind: str,
) -> None:
    compiled = compiled_problem()

    class NoGraphNamedSupportClient(SuccessfulResearchClient):
        def __init__(self) -> None:
            super().__init__()
            self.package_calls = 0

        async def generate_structured(
            self,
            request: ModelRequest,
            output_type: type[Any],
        ) -> ModelResult[Any]:
            if output_type is ResearchWorkerReport:
                self.calls += 1
                assignment_id = json.loads(request.input_text)["assignment"]["id"]
                main_result = ScientificResult(
                    local_key="main-proof",
                    kind=ScientificResultKind.LEMMA,
                    exact_statement=compiled.normalized_statement,
                    scope=ScientificScope.MAIN,
                    proof_or_certificate="A purported derivation using named support.",
                    dependency_node_ids=(
                        ["CLM-FAKE0001"] if support_kind == "external-node" else []
                    ),
                    dependency_result_keys=(
                        ["support-lemma"] if support_kind == "local-result" else []
                    ),
                    disposition=ScientificResultDisposition.PROPOSED_COMPLETE,
                )
                local_support = (
                    [
                        ScientificResult(
                            local_key="support-lemma",
                            kind=ScientificResultKind.LEMMA,
                            exact_statement="The named local support lemma holds.",
                            scope=ScientificScope.BRANCH,
                            proof_or_certificate="A separate purported proof.",
                            disposition=ScientificResultDisposition.PROPOSED_COMPLETE,
                        )
                    ]
                    if support_kind == "local-result"
                    else []
                )
                return ModelResult(
                    parsed=ResearchWorkerReport(
                        assignment_id=assignment_id,
                        results=[main_result, *local_support],
                        branch_outcome=BranchOutcome.CANDIDATE_COMPLETE,
                        mechanism="Use named support without a graph.",
                    ),
                    response_id=f"no-graph-support-{self.calls}",
                )
            if output_type is CandidateProofPackage:
                self.package_calls += 1
            return await super().generate_structured(request, output_type)

    client = NoGraphNamedSupportClient()
    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled,
        research_dir=tmp_path / "research",
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=1,
            maximum_pending_assignments=4,
            maximum_coordinator_decisions=1,
        ),
    )

    assert result.outcome is ResearchOutcome.PAUSED_RETRIABLE
    assert not result.accepted_for_manuscript
    assert client.package_calls == 0
    assert any(
        "no active canonical knowledge graph" in obligation
        for obligation in result.unresolved_obligations
    )


@pytest.mark.asyncio
async def test_self_assumed_exact_main_candidate_never_reaches_packaging_without_graph(
    tmp_path: Path,
) -> None:
    compiled = compiled_problem()

    class AssumedMainCandidateClient(SuccessfulResearchClient):
        def __init__(self) -> None:
            super().__init__()
            self.package_calls = 0

        async def generate_structured(
            self,
            request: ModelRequest,
            output_type: type[Any],
        ) -> ModelResult[Any]:
            if output_type is ResearchWorkerReport:
                self.calls += 1
                assignment_id = json.loads(request.input_text)["assignment"]["id"]
                return ModelResult(
                    parsed=ResearchWorkerReport(
                        assignment_id=assignment_id,
                        results=[
                            ScientificResult(
                                local_key="self-assumed-main",
                                kind=ScientificResultKind.LEMMA,
                                exact_statement=compiled.normalized_statement,
                                scope=ScientificScope.MAIN,
                                assumptions=[compiled.normalized_statement],
                                proof_or_certificate="The conclusion is repeated as an assumption.",
                                disposition=ScientificResultDisposition.PROPOSED_COMPLETE,
                            )
                        ],
                        branch_outcome=BranchOutcome.CANDIDATE_COMPLETE,
                        mechanism="Assume the exact theorem and repeat it.",
                    ),
                    response_id=f"assumed-main-{self.calls}",
                )
            if output_type is CandidateProofPackage:
                self.package_calls += 1
            return await super().generate_structured(request, output_type)

    client = AssumedMainCandidateClient()
    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled,
        research_dir=tmp_path / "research",
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=1,
            maximum_pending_assignments=4,
            maximum_coordinator_decisions=1,
        ),
    )

    assert result.outcome is ResearchOutcome.PAUSED_RETRIABLE
    assert not result.accepted_for_manuscript
    assert client.package_calls == 0
    assert any("unbound assumptions" in item for item in result.unresolved_obligations)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "dependency_node_type",
        "dependency_status",
        "dependency_tags",
        "blocking_main_obligation",
        "accepted",
        "obligation_fragment",
    ),
    [
        (
            NodeType.CLAIM,
            EpistemicStatus.OPEN,
            [],
            False,
            False,
            "not current audited Markdown claims",
        ),
        (
            NodeType.CLAIM,
            EpistemicStatus.CANDIDATE,
            [],
            False,
            False,
            "not current audited Markdown claims",
        ),
        (NodeType.CLAIM, EpistemicStatus.AUDIT_PASSED, [], False, True, None),
        (
            NodeType.CLAIM,
            EpistemicStatus.AUDIT_PASSED,
            [],
            True,
            False,
            "unresolved obligation",
        ),
        (
            NodeType.CLAIM,
            EpistemicStatus.AUDIT_PASSED,
            ["matek/computation", "matek/replay-required"],
            False,
            False,
            "external computation premise",
        ),
        (
            NodeType.DEFINITION,
            EpistemicStatus.OPEN,
            [],
            False,
            False,
            "application-admitted definitions",
        ),
    ],
)
async def test_candidate_graph_support_requires_trusted_external_premises(
    tmp_path: Path,
    dependency_node_type: NodeType,
    dependency_status: EpistemicStatus,
    dependency_tags: list[str],
    blocking_main_obligation: bool,
    accepted: bool,
    obligation_fragment: str | None,
) -> None:
    problem = tmp_path / "problem.md"
    problem.write_text("Prove the fixture theorem.", encoding="utf-8")
    run_id = "run-external-premise"
    graph = KnowledgeGraph(tmp_path, "external-premise")
    problem_id, _ = graph.initialize_problem(
        source_path=problem,
        problem_text=problem.read_text(encoding="utf-8"),
        run_id=run_id,
    )
    compiled = compiled_problem()
    graph.record_compiled_problem(
        problem_id=problem_id,
        run_id=run_id,
        compiled_problem=compiled.model_dump(mode="json"),
    )
    target_id = graph.main_claim_id(problem_id)
    dependency_id = (
        "DEF-HUMAN001" if dependency_node_type is NodeType.DEFINITION else "CLM-EXTERNAL001"
    )
    task_ids, _, _ = graph.record_assignment_tasks(
        problem_id=problem_id,
        run_id=run_id,
        decision_id=800,
        assignments=[
            {
                "id": "external-premise-seed",
                "approach_family": "fixture",
                "task": "Record the external premise fixture.",
                "expected_output": "One exact premise.",
                "target_node_ids": [target_id],
            }
        ],
    )
    created = graph.merge_patch(
        GraphPatch(
            base_graph_revision=graph.load_state().revision,
            run_id=run_id,
            task_id=task_ids["external-premise-seed"],
            agent_role="research-auditor-fixture",
            create_nodes=[
                GraphNodeCreate(
                    matek_id=dependency_id,
                    node_type=dependency_node_type,
                    claim_type=(
                        ClaimType.LEMMA if dependency_node_type is NodeType.CLAIM else None
                    ),
                    title="External fixture premise",
                    body="## Exact statement\n\nThe external fixture premise holds.",
                    epistemic_status=dependency_status,
                    tags=dependency_tags,
                )
            ],
        ),
        problem_id=problem_id,
        operation_id="create-external-premise-fixture",
    )
    assert created.committed
    if blocking_main_obligation:
        obligation_id = "OBL-MAINBLOCK001"
        blocked = graph.merge_patch(
            GraphPatch(
                base_graph_revision=graph.load_state().revision,
                run_id=run_id,
                task_id=task_ids["external-premise-seed"],
                agent_role="research-auditor-fixture",
                create_nodes=[
                    GraphNodeCreate(
                        matek_id=obligation_id,
                        node_type=NodeType.OBLIGATION,
                        title="Unresolved main-target fixture",
                        body=(
                            "## Exact statement\n\nDischarge the remaining main-target case."
                            "\n\n## Conclusion\n\nThe fixture theorem holds."
                        ),
                        workflow_status=WorkflowStatus.BLOCKED,
                    )
                ],
                add_edges=[
                    GraphEdge(
                        source_id=obligation_id,
                        relation=RelationType.TARGETS,
                        target_id=target_id,
                    )
                ],
            ),
            problem_id=problem_id,
            operation_id="create-main-obligation-fixture",
        )
        assert blocked.committed

    class ExternalPremiseCandidateClient(SuccessfulResearchClient):
        def __init__(self) -> None:
            super().__init__()
            self.package_calls = 0

        async def generate_structured(
            self,
            request: ModelRequest,
            output_type: type[Any],
        ) -> ModelResult[Any]:
            if output_type is ResearchCoordinatorDecision:
                result = await super().generate_structured(request, output_type)
                payload = json.loads(request.input_text)
                memory = payload["knowledge_graph_memory"]
                result.parsed.assignments = [
                    assignment.model_copy(update={"target_node_ids": [target_id]})
                    for assignment in result.parsed.assignments
                ]
                result.parsed.rationale = (
                    f"Graph review {memory['graph_revision']}: " + result.parsed.rationale
                )
                return result
            if output_type is ResearchWorkerReport:
                self.calls += 1
                assignment_id = json.loads(request.input_text)["assignment"]["id"]
                return ModelResult(
                    parsed=ResearchWorkerReport(
                        assignment_id=assignment_id,
                        results=[
                            ScientificResult(
                                local_key="main-proof",
                                kind=ScientificResultKind.LEMMA,
                                exact_statement=compiled.normalized_statement,
                                scope=ScientificScope.MAIN,
                                proof_or_certificate=(
                                    "A complete derivation from the exact external premise."
                                ),
                                dependency_node_ids=[dependency_id],
                                target_node_ids=[target_id],
                                disposition=ScientificResultDisposition.PROPOSED_COMPLETE,
                            )
                        ],
                        branch_outcome=BranchOutcome.CANDIDATE_COMPLETE,
                        mechanism="Apply the exact external premise.",
                    ),
                    response_id=f"external-premise-{self.calls}",
                )
            if output_type is CandidateProofPackage:
                self.package_calls += 1
            return await super().generate_structured(request, output_type)

    client = ExternalPremiseCandidateClient()
    research_dir = tmp_path / ".matek" / "runs" / run_id / "research"
    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled,
        research_dir=research_dir,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=1,
            maximum_pending_assignments=4,
            maximum_coordinator_decisions=1,
        ),
        knowledge_graph=graph,
        graph_problem_id=problem_id,
        run_id=run_id,
    )

    assert result.accepted_for_manuscript is accepted
    assert client.package_calls == int(accepted)
    if obligation_fragment is not None:
        assert result.outcome is ResearchOutcome.PAUSED_RETRIABLE
        assert any(
            obligation_fragment in obligation for obligation in result.unresolved_obligations
        )


@pytest.mark.asyncio
async def test_candidate_graph_support_binds_computation_dependency_and_artifacts(
    tmp_path: Path,
) -> None:
    problem = tmp_path / "problem.md"
    problem.write_text("Prove the fixture theorem.", encoding="utf-8")
    run_id = "run-computation-graph"
    graph = KnowledgeGraph(tmp_path, "computation-candidate")
    problem_id, _ = graph.initialize_problem(
        source_path=problem,
        problem_text=problem.read_text(encoding="utf-8"),
        run_id=run_id,
    )
    compiled = compiled_problem()
    graph.record_compiled_problem(
        problem_id=problem_id,
        run_id=run_id,
        compiled_problem=compiled.model_dump(mode="json"),
    )
    research_dir = tmp_path / ".matek" / "runs" / run_id / "research"

    result = await run_adaptive_research(
        client=GraphCandidateComputationResearchClient(transitive_link=True),
        compiled_problem=compiled,
        research_dir=research_dir,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=1,
            maximum_pending_assignments=4,
        ),
        knowledge_graph=graph,
        graph_problem_id=problem_id,
        run_id=run_id,
        computation_backend=CandidateComputationBackend(),
        computation_replay_isolation=SAFE_COMPUTATION_REPLAY,
    )

    assert result.accepted_for_manuscript
    candidate_input = json.loads(
        next((research_dir / "candidate" / "attempts").glob("*/input.json")).read_text(
            encoding="utf-8"
        )
    )
    binding = candidate_input["candidate_canonical_graph_support"]["bindings"][0]
    assert binding["main_result_keys"] == ["main-proof"]
    assert binding["closure_result_keys"] == [
        "domain-reduction",
        "finite-certificate",
        "main-proof",
    ]
    assert binding["computation_result_keys"] == ["finite-certificate"]
    support_nodes = binding["support_nodes"]
    assert any(node["matek_id"] == binding["main_claim_id"] for node in support_nodes)
    assert binding["support_sha256"] == sha256_json(support_nodes)
    assert {node["node_type"] for node in support_nodes} == {
        "artifact",
        "claim",
        "derivation",
        "proof_attempt",
    }
    claim_ids = {
        node["metadata"].get("matek_result_local_key"): node["matek_id"]
        for node in support_nodes
        if node["node_type"] == "claim"
    }
    derivations = {
        node["metadata"].get("matek_result_local_key"): node
        for node in support_nodes
        if node["node_type"] == "derivation"
    }
    assert derivations["main-proof"]["metadata"]["matek_premise_claim_ids"] == [
        claim_ids["domain-reduction"]
    ]
    assert derivations["domain-reduction"]["metadata"]["matek_premise_claim_ids"] == [
        claim_ids["finite-certificate"]
    ]
    assert len([node for node in support_nodes if node["node_type"] == "artifact"]) == 2


@pytest.mark.asyncio
async def test_accepted_candidate_revalidates_computation_cas_on_resume(tmp_path: Path) -> None:
    problem = tmp_path / "problem.md"
    problem.write_text("Prove the fixture theorem.", encoding="utf-8")
    run_id = "run-computation-resume"
    graph = KnowledgeGraph(tmp_path, "computation-resume")
    problem_id, _ = graph.initialize_problem(
        source_path=problem,
        problem_text=problem.read_text(encoding="utf-8"),
        run_id=run_id,
    )
    compiled = compiled_problem()
    graph.record_compiled_problem(
        problem_id=problem_id,
        run_id=run_id,
        compiled_problem=compiled.model_dump(mode="json"),
    )
    research_dir = tmp_path / ".matek" / "runs" / run_id / "research"
    client = GraphCandidateComputationResearchClient()
    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled,
        research_dir=research_dir,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=1,
            maximum_pending_assignments=4,
        ),
        computation_backend=CandidateComputationBackend(),
        computation_replay_isolation=SAFE_COMPUTATION_REPLAY,
        knowledge_graph=graph,
        graph_problem_id=problem_id,
        run_id=run_id,
    )
    assert result.accepted_for_manuscript
    blob = next((research_dir / "computations" / "blobs" / "sha256").iterdir())
    blob.chmod(0o600)
    blob.write_bytes(b"tampered\n")

    with pytest.raises(StageValidationError, match="invalid computation evidence"):
        await run_adaptive_research(
            client=client,
            compiled_problem=compiled,
            research_dir=research_dir,
            workflow_settings=ResearchWorkflowSettings(
                minimum_initial_assignments=4,
                maximum_concurrent_agents=1,
                maximum_pending_assignments=4,
            ),
            computation_backend=CandidateComputationBackend(),
            computation_replay_isolation=SAFE_COMPUTATION_REPLAY,
            knowledge_graph=graph,
            graph_problem_id=problem_id,
            run_id=run_id,
        )


class PolicyAssertingResearchClient(SuccessfulResearchClient):
    def __init__(self, *, expected_web_search: bool, response_prefix: str) -> None:
        super().__init__()
        self.expected_web_search = expected_web_search
        self.response_prefix = response_prefix
        self.output_types: list[type[Any]] = []

    async def generate_structured(
        self, request: ModelRequest, output_type: type[Any]
    ) -> ModelResult[Any]:
        assert request.settings.web_search is self.expected_web_search
        self.output_types.append(output_type)
        result = await super().generate_structured(request, output_type)
        return ModelResult(
            parsed=result.parsed,
            response_id=f"{self.response_prefix}-{self.calls}",
        )


class CompletionDrainResearchClient(SuccessfulResearchClient):
    """Expose a slower candidate after an ordinary report without another decision."""

    def __init__(self) -> None:
        super().__init__()
        self.progress_completed = asyncio.Event()
        self.candidate_cancelled = False

    async def generate_structured(
        self, request: ModelRequest, output_type: type[Any]
    ) -> ModelResult[Any]:
        if output_type is ResearchCoordinatorDecision:
            self.calls += 1
            call_number = self.calls
            payload = json.loads(request.input_text)
            assert payload["initial_portfolio"]
            assignments = [
                ResearchAssignment(
                    id=assignment_id,
                    approach_family=family,
                    task=f"Investigate {family}",
                    expected_output="A formal proof or exact obstruction",
                )
                for assignment_id, family in (
                    ("fast-progress", "direct"),
                    ("slower-candidate", "structural"),
                    ("other-counterexample", "counterexample"),
                    ("other-literature", "literature"),
                )
            ]
            return ModelResult(
                parsed=ResearchCoordinatorDecision(
                    decision_id=payload["decision_id"],
                    after_event_sequence=payload["after_event_sequence"],
                    assignments=assignments,
                    rationale="Launch four independent families.",
                ),
                response_id=f"drain-{call_number}",
            )
        if output_type is ResearchWorkerReport:
            self.calls += 1
            call_number = self.calls
            assignment = json.loads(request.input_text)["assignment"]
            assignment_id = assignment["id"]
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            try:
                if assignment_id == "fast-progress":
                    self.progress_completed.set()
                    status = WorkerStatus.PROGRESS
                    proof_content = "A useful intermediate lemma."
                    exact_gap = "Complete the structural argument."
                else:
                    await self.progress_completed.wait()
                    try:
                        await asyncio.sleep(0.02 if assignment_id == "slower-candidate" else 0.04)
                    except asyncio.CancelledError:
                        if assignment_id == "slower-candidate":
                            self.candidate_cancelled = True
                        raise
                    status = (
                        WorkerStatus.CANDIDATE_COMPLETE
                        if assignment_id == "slower-candidate"
                        else WorkerStatus.PROGRESS
                    )
                    proof_content = (
                        "A complete proof of the exact target."
                        if status is WorkerStatus.CANDIDATE_COMPLETE
                        else "Another useful partial result."
                    )
                    exact_gap = (
                        None
                        if status is WorkerStatus.CANDIDATE_COMPLETE
                        else "Combine with the structural route."
                    )
            finally:
                self.active -= 1
            return ModelResult(
                parsed=research_worker_report_v1(
                    assignment_id=assignment_id,
                    status=status,
                    formal_results=[f"Result from {assignment_id}"],
                    proof_content=proof_content,
                    exact_gap=exact_gap,
                    sources=[],
                    mechanism=assignment["task"],
                ),
                response_id=f"drain-{call_number}",
            )
        return await super().generate_structured(request, output_type)


class OfflineIdentifierVerifier:
    def __init__(self, verified: Collection[str] = ()) -> None:
        self.verified = set(verified)

    async def verify(
        self,
        identifiers: Collection[str],
        *,
        expected_title: str | None = None,
    ) -> SourceVerificationReport:
        records = [
            SourceVerificationRecord(
                identifier=identifier,
                status=(
                    SourceVerificationStatus.VERIFIED
                    if identifier in self.verified
                    else SourceVerificationStatus.UNAVAILABLE
                ),
                detail="offline fixture",
            )
            for identifier in identifiers
        ]
        return SourceVerificationReport(records=records)


class ContinuityResearchClient(SuccessfulResearchClient):
    def __init__(self) -> None:
        super().__init__()
        self.coordinator_payloads: list[dict[str, Any]] = []

    async def generate_structured(
        self, request: ModelRequest, output_type: type[Any]
    ) -> ModelResult[Any]:
        if output_type is ResearchCoordinatorDecision:
            self.calls += 1
            payload = json.loads(request.input_text)
            self.coordinator_payloads.append(payload)
            decision_id = payload["decision_id"]
            completed_ids = {
                report["assignment_id"] for report in payload["visible_worker_reports"]
            }
            if payload["initial_portfolio"]:
                families = ("direct", "structural", "counterexample", "literature")
                assignments = [
                    ResearchAssignment(
                        id=f"route-{index}",
                        approach_family=family,
                        task=f"Investigate {family}",
                        expected_output="formal content or an exact obstruction",
                    )
                    for index, family in enumerate(families, start=1)
                ]
            elif {
                "route-1",
                "route-2",
                "route-3",
                "route-4",
            }.issubset(completed_ids) and "continuity-synthesis" not in completed_ids:
                assignments = [
                    ResearchAssignment(
                        id="continuity-synthesis",
                        approach_family="continuity synthesis",
                        task="Combine the surviving lemma and discharge the exact open gap",
                        expected_output="a complete proof",
                    )
                ]
            else:
                assignments = []
            return ModelResult(
                parsed=ResearchCoordinatorDecision(
                    decision_id=decision_id,
                    after_event_sequence=payload["after_event_sequence"],
                    assignments=assignments,
                    rationale="Use the durable event-indexed mathematical handoff.",
                ),
                response_id=f"continuity-decision-{decision_id}",
            )
        if output_type is ResearchWorkerReport:
            self.calls += 1
            assignment = json.loads(request.input_text)["assignment"]
            assignment_id = assignment["id"]
            if assignment_id == "route-1":
                report = research_worker_report_v1(
                    assignment_id=assignment_id,
                    status=WorkerStatus.PROGRESS,
                    formal_results=["Lemma A establishes the finite reduction."],
                    proof_content="Proof of Lemma A.",
                    exact_gap="Prove the reduced boundary case.",
                    sources=[],
                    dependencies=["Boundary lemma B"],
                    mechanism=assignment["task"],
                )
            elif assignment_id == "route-2":
                report = research_worker_report_v1(
                    assignment_id=assignment_id,
                    status=WorkerStatus.REFUTED,
                    formal_results=[],
                    proof_content="The proposed strengthening fails.",
                    exact_gap=None,
                    sources=[],
                    counterexamples=["A size-three object refutes the strengthening."],
                    mechanism=assignment["task"],
                )
            elif assignment_id == "route-3":
                report = research_worker_report_v1(
                    assignment_id=assignment_id,
                    status=WorkerStatus.BLOCKED,
                    formal_results=[],
                    proof_content="Reduction attempted.",
                    exact_gap="Missing compactness lemma.",
                    sources=[],
                    mechanism=assignment["task"],
                )
            elif assignment_id == "route-4":
                report = research_worker_report_v1(
                    assignment_id=assignment_id,
                    status=WorkerStatus.PROGRESS,
                    formal_results=["Lemma B proves the required boundary case."],
                    proof_content="Proof of Lemma B.",
                    exact_gap="Combine Lemmas A and B.",
                    sources=[],
                    dependencies=["Lemma A"],
                    mechanism=assignment["task"],
                )
            else:
                report = research_worker_report_v1(
                    assignment_id=assignment_id,
                    status=WorkerStatus.CANDIDATE_COMPLETE,
                    formal_results=["The target follows from Lemmas A and B."],
                    proof_content="Complete proof combining Lemmas A and B.",
                    exact_gap=None,
                    sources=[],
                    mechanism=assignment["task"],
                )
            return ModelResult(parsed=report, response_id=f"continuity-worker-{self.calls}")
        return await super().generate_structured(request, output_type)


class RollingPoolResearchClient:
    def __init__(self) -> None:
        self.calls = 0
        self.slow_started = asyncio.Event()
        self.release_slow = asyncio.Event()
        self.followup_started = asyncio.Event()
        self.slow_completed = False
        self.slow_cancelled = False
        self.coordinator_payloads: list[dict[str, Any]] = []

    async def generate_structured(
        self, request: ModelRequest, output_type: type[Any]
    ) -> ModelResult[Any]:
        self.calls += 1
        response_id = f"rolling-{self.calls}"
        if output_type is ResearchCoordinatorDecision:
            payload = json.loads(request.input_text)
            self.coordinator_payloads.append(payload)
            if payload["initial_portfolio"]:
                assignments = [
                    ResearchAssignment(
                        id="fast-route",
                        approach_family="direct",
                        task="Prove a useful reduction quickly",
                        expected_output="A reduction lemma",
                    ),
                    ResearchAssignment(
                        id="slow-route",
                        approach_family="structural",
                        task="Explore a deliberately slow structural route",
                        expected_output="A structural lemma",
                    ),
                    ResearchAssignment(
                        id="queued-counterexample",
                        approach_family="counterexample",
                        task="Search for obstructions",
                        expected_output="A counterexample or exclusion",
                    ),
                    ResearchAssignment(
                        id="queued-literature",
                        approach_family="literature",
                        task="Check nearby results",
                        expected_output="A verified theorem map",
                    ),
                ]
                retire_ids: list[str] = []
            else:
                assert self.slow_started.is_set()
                assert not self.slow_completed
                assert {item["id"] for item in payload["active_assignments"]} == {"slow-route"}
                assert [
                    report["assignment_id"] for report in payload["visible_worker_reports"]
                ] == ["fast-route"]
                assert (
                    payload["visible_worker_reports"][0]["results"][0]["proof_or_certificate"]
                    == "Full proof of the reduction lemma."
                )
                assert any(
                    event["kind"] == "worker_report_accepted"
                    and event["assignment_id"] == "fast-route"
                    for event in payload["unacknowledged_events"]
                )
                assignments = [
                    ResearchAssignment(
                        id="targeted-followup",
                        approach_family="targeted synthesis",
                        task="Use the reduction lemma to finish the proof",
                        expected_output="A complete proof",
                    )
                ]
                retire_ids = ["queued-counterexample", "queued-literature"]
            return ModelResult(
                parsed=ResearchCoordinatorDecision(
                    decision_id=payload["decision_id"],
                    after_event_sequence=payload["after_event_sequence"],
                    assignments=assignments,
                    rationale="Continuously react to the newest durable report.",
                    retire_assignment_ids=retire_ids,
                ),
                response_id=response_id,
            )
        if output_type is ResearchWorkerReport:
            assignment = json.loads(request.input_text)["assignment"]
            assignment_id = assignment["id"]
            if assignment_id == "fast-route":
                await self.slow_started.wait()
                parsed: BaseModel = research_worker_report_v1(
                    assignment_id=assignment_id,
                    status=WorkerStatus.PROGRESS,
                    formal_results=["Reduction lemma"],
                    proof_content="Full proof of the reduction lemma.",
                    exact_gap="Apply the reduction lemma to the boundary case.",
                    sources=[],
                    mechanism=assignment["task"],
                )
            elif assignment_id == "slow-route":
                self.slow_started.set()
                try:
                    await self.release_slow.wait()
                except asyncio.CancelledError:
                    self.slow_cancelled = True
                    raise
                self.slow_completed = True
                parsed = research_worker_report_v1(
                    assignment_id=assignment_id,
                    status=WorkerStatus.PROGRESS,
                    formal_results=["Slow structural lemma"],
                    proof_content="Slow proof.",
                    exact_gap="Finish the theorem.",
                    sources=[],
                    mechanism=assignment["task"],
                )
            elif assignment_id == "targeted-followup":
                self.followup_started.set()
                assert not self.slow_completed
                parsed = research_worker_report_v1(
                    assignment_id=assignment_id,
                    status=WorkerStatus.CANDIDATE_COMPLETE,
                    formal_results=["The target theorem"],
                    proof_content="Complete proof using the reduction lemma.",
                    exact_gap=None,
                    sources=[],
                    mechanism=assignment["task"],
                )
            else:  # pragma: no cover - retired assignments must never launch
                raise AssertionError(f"unexpected worker launch: {assignment_id}")
        elif output_type is CandidateProofPackage:
            parsed = candidate_package()
        elif output_type is AuditVerdict:
            parsed = passing_audit()
        elif output_type is FinalJudgeVerdict:
            parsed = FinalJudgeVerdict(
                verdict=FinalJudgeDecision.ACCEPTED,
                strongest_result="Fixture theorem",
            )
        else:  # pragma: no cover - a stage adding an unexpected call should fail loudly
            raise AssertionError(output_type)
        return ModelResult(parsed=parsed, response_id=response_id)


class ReservationReplacementResearchClient:
    """Exercise coordinator feedback while every configured call slot is reserved."""

    def __init__(self) -> None:
        self.calls = 0
        self.coordinator_payloads: list[dict[str, Any]] = []
        self.worker_ids: list[str] = []

    async def generate_structured(
        self, request: ModelRequest, output_type: type[Any]
    ) -> ModelResult[Any]:
        self.calls += 1
        response_id = f"reservation-replacement-{self.calls}"
        if output_type is ResearchCoordinatorDecision:
            payload = json.loads(request.input_text)
            self.coordinator_payloads.append(payload)
            if payload["initial_portfolio"]:
                parsed: BaseModel = ResearchCoordinatorDecision(
                    decision_id=payload["decision_id"],
                    after_event_sequence=payload["after_event_sequence"],
                    assignments=[
                        ResearchAssignment(
                            id="fast-feedback",
                            approach_family="direct",
                            task="Produce immediate feedback",
                            expected_output="A concrete reduction",
                        ),
                        ResearchAssignment(
                            id="replaceable-structural",
                            approach_family="structural",
                            task="Explore a replaceable structural route",
                            expected_output="A structural lemma",
                        ),
                        ResearchAssignment(
                            id="replaceable-counterexample",
                            approach_family="counterexample",
                            task="Explore a replaceable obstruction route",
                            expected_output="An obstruction",
                        ),
                        ResearchAssignment(
                            id="replaceable-literature",
                            approach_family="literature",
                            task="Explore a replaceable literature route",
                            expected_output="A theorem map",
                        ),
                    ],
                    rationale="Start with four materially distinct routes.",
                )
            else:
                assert [
                    report["assignment_id"] for report in payload["visible_worker_reports"]
                ] == ["fast-feedback"]
                assert payload["refundable_unlaunched_assignment_count"] == 3
                assert {assignment["id"] for assignment in payload["queued_assignments"]} == {
                    "replaceable-structural",
                    "replaceable-counterexample",
                    "replaceable-literature",
                }
                assert payload["maximum_new_assignments_this_decision"] >= 1
                parsed = ResearchCoordinatorDecision(
                    decision_id=payload["decision_id"],
                    after_event_sequence=payload["after_event_sequence"],
                    assignments=[
                        ResearchAssignment(
                            id="targeted-replacement",
                            approach_family="targeted synthesis",
                            task="Use the new reduction instead of the stale queued routes",
                            expected_output="A sharpened reduction",
                        )
                    ],
                    rationale="Replace unlaunched work in response to durable feedback.",
                    retire_assignment_ids=[
                        "replaceable-structural",
                        "replaceable-counterexample",
                        "replaceable-literature",
                    ],
                )
            return ModelResult(parsed=parsed, response_id=response_id)
        if output_type is ResearchWorkerReport:
            assignment = json.loads(request.input_text)["assignment"]
            assignment_id = assignment["id"]
            self.worker_ids.append(assignment_id)
            assert assignment_id in {"fast-feedback", "targeted-replacement"}
            return ModelResult(
                parsed=research_worker_report_v1(
                    assignment_id=assignment_id,
                    status=WorkerStatus.PROGRESS,
                    formal_results=[f"Progress from {assignment_id}."],
                    proof_content=f"Full durable reasoning from {assignment_id}.",
                    exact_gap="A final lemma remains.",
                    sources=[],
                    mechanism=assignment["task"],
                ),
                response_id=response_id,
            )
        raise AssertionError(output_type)


class CleanupCandidateRaceResearchClient:
    """Return a complete candidate only while terminal cleanup cancels its task."""

    def __init__(self) -> None:
        self.calls = 0
        self.candidate_started = asyncio.Event()
        self.cleanup_cancelled_candidate = False
        self.coordinator_payloads: list[dict[str, Any]] = []
        self.gate_output_types: list[type[Any]] = []

    async def generate_structured(
        self, request: ModelRequest, output_type: type[Any]
    ) -> ModelResult[Any]:
        self.calls += 1
        response_id = f"cleanup-candidate-race-{self.calls}"
        if output_type is ResearchCoordinatorDecision:
            payload = json.loads(request.input_text)
            self.coordinator_payloads.append(payload)
            if payload["initial_portfolio"]:
                parsed: BaseModel = ResearchCoordinatorDecision(
                    decision_id=payload["decision_id"],
                    after_event_sequence=payload["after_event_sequence"],
                    assignments=[
                        ResearchAssignment(
                            id="fast-terminal-feedback",
                            approach_family="direct",
                            task="Produce feedback that triggers terminal handling",
                            expected_output="An exact remaining gap",
                        ),
                        ResearchAssignment(
                            id="cleanup-candidate",
                            approach_family="structural",
                            task="Finish the proof concurrently",
                            expected_output="A complete proof",
                        ),
                        ResearchAssignment(
                            id="unused-counterexample",
                            approach_family="counterexample",
                            task="Search for an obstruction",
                            expected_output="A counterexample or exclusion",
                        ),
                        ResearchAssignment(
                            id="unused-literature",
                            approach_family="literature",
                            task="Search nearby literature",
                            expected_output="A verified theorem map",
                        ),
                    ],
                    rationale="Run diverse work concurrently.",
                )
            else:
                assert self.candidate_started.is_set()
                assert {item["id"] for item in payload["active_assignments"]} == {
                    "cleanup-candidate"
                }
                parsed = ResearchCoordinatorDecision(
                    decision_id=payload["decision_id"],
                    after_event_sequence=payload["after_event_sequence"],
                    assignments=[],
                    rationale="The visible partial report does not justify more work.",
                    retire_assignment_ids=["unused-counterexample", "unused-literature"],
                    stop_recommended=True,
                    stop_reason="No route visible to the coordinator remains fundable.",
                    stop_category="budget",
                )
            return ModelResult(parsed=parsed, response_id=response_id)
        if output_type is ResearchWorkerReport:
            assignment = json.loads(request.input_text)["assignment"]
            assignment_id = assignment["id"]
            if assignment_id == "fast-terminal-feedback":
                await self.candidate_started.wait()
                parsed = research_worker_report_v1(
                    assignment_id=assignment_id,
                    status=WorkerStatus.PROGRESS,
                    formal_results=["A reduction with one apparent gap."],
                    proof_content="Proof of the reduction.",
                    exact_gap="The coordinator sees no way to close the gap.",
                    sources=[],
                    mechanism=assignment["task"],
                )
            elif assignment_id == "cleanup-candidate":
                self.candidate_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cleanup_cancelled_candidate = True
                parsed = research_worker_report_v1(
                    assignment_id=assignment_id,
                    status=WorkerStatus.CANDIDATE_COMPLETE,
                    formal_results=["The exact target theorem."],
                    proof_content="Complete proof discovered before cleanup finished.",
                    exact_gap=None,
                    sources=[],
                    mechanism=assignment["task"],
                )
            else:  # pragma: no cover - queued work must be retired, never launched
                raise AssertionError(f"unexpected worker launch: {assignment_id}")
            return ModelResult(parsed=parsed, response_id=response_id)

        self.gate_output_types.append(output_type)
        if output_type is CandidateProofPackage:
            parsed = candidate_package()
        elif output_type is AuditVerdict:
            parsed = passing_audit()
        elif output_type is FinalJudgeVerdict:
            parsed = FinalJudgeVerdict(
                verdict=FinalJudgeDecision.ACCEPTED,
                reasons=["The cleanup-time candidate passes every independent check."],
                strongest_result="Fixture theorem",
            )
        else:  # pragma: no cover - a new gate call should fail this regression loudly
            raise AssertionError(output_type)
        return ModelResult(parsed=parsed, response_id=response_id)


class DeferredCandidateGateClient:
    """Reject one candidate, then accept a distinct proof completed during its audit."""

    def __init__(
        self,
        *,
        package_repair_combination: bool = False,
        reject_repair_combination: bool = False,
    ) -> None:
        self.calls = 0
        self.package_repair_combination = package_repair_combination
        self.reject_repair_combination = reject_repair_combination
        self.second_started = asyncio.Event()
        self.release_second = asyncio.Event()
        self.packaged_ids: list[str] = []

    async def generate_structured(
        self, request: ModelRequest, output_type: type[Any]
    ) -> ModelResult[Any]:
        self.calls += 1
        response_id = f"deferred-candidate-{self.calls}"
        payload = json.loads(request.input_text)
        if output_type is ResearchCoordinatorDecision:
            if payload["initial_portfolio"]:
                assignments = [
                    ResearchAssignment(
                        id=assignment_id,
                        approach_family=family,
                        task=f"Investigate {family}",
                        expected_output="A complete proof or exact obstruction",
                    )
                    for assignment_id, family in (
                        ("first-candidate", "direct"),
                        ("second-candidate", "structural"),
                        ("unused-counterexample", "counterexample"),
                        ("unused-literature", "literature"),
                    )
                ]
                return ModelResult(
                    parsed=ResearchCoordinatorDecision(
                        decision_id=payload["decision_id"],
                        after_event_sequence=payload["after_event_sequence"],
                        assignments=assignments,
                        rationale="Start independent candidate routes.",
                    ),
                    response_id=response_id,
                )
            if self.package_repair_combination:
                return ModelResult(
                    parsed=ResearchCoordinatorDecision(
                        decision_id=payload["decision_id"],
                        after_event_sequence=payload["after_event_sequence"],
                        assignments=[],
                        rationale="Combine the original proof with the new repair lemma.",
                        candidate_packaging_recommended=True,
                        candidate_report_ids=["first-candidate", "second-candidate"],
                    ),
                    response_id=response_id,
                )
            return ModelResult(
                parsed=ResearchCoordinatorDecision(
                    decision_id=payload["decision_id"],
                    after_event_sequence=payload["after_event_sequence"],
                    assignments=[],
                    rationale="The first audited candidate failed, so recommend stopping.",
                    stop_recommended=True,
                    stop_reason="No further work is needed if no other candidate passes.",
                    stop_category="budget",
                ),
                response_id=response_id,
            )
        if output_type is ResearchWorkerReport:
            assignment_id = payload["assignment"]["id"]
            if assignment_id == "first-candidate":
                await self.second_started.wait()
            elif assignment_id == "second-candidate":
                self.second_started.set()
                await self.release_second.wait()
            else:  # pragma: no cover - finite gate budget retires queued work
                raise AssertionError(f"unexpected worker launch: {assignment_id}")
            return ModelResult(
                parsed=research_worker_report_v1(
                    assignment_id=assignment_id,
                    status=WorkerStatus.CANDIDATE_COMPLETE,
                    formal_results=[f"Candidate theorem from {assignment_id}."],
                    proof_content=f"Complete proof text from {assignment_id}.",
                    exact_gap=None,
                    sources=[],
                    mechanism=payload["assignment"]["task"],
                ),
                response_id=response_id,
            )
        if output_type is CandidateProofPackage:
            assignment_id = "+".join(payload["candidate_trigger_assignment_ids"])
            self.packaged_ids.append(assignment_id)
            if assignment_id == "first-candidate":
                self.release_second.set()
            package = candidate_package().model_copy(
                update={"full_proof": f"Packaged proof from {assignment_id}."}
            )
            return ModelResult(parsed=package, response_id=response_id)
        if output_type is AuditVerdict:
            return ModelResult(parsed=passing_audit(), response_id=response_id)
        if output_type is FinalJudgeVerdict:
            proof = payload["candidate_package"]["full_proof"]
            accepted = "second-candidate" in proof and not (
                self.reject_repair_combination and "+" in proof
            )
            return ModelResult(
                parsed=FinalJudgeVerdict(
                    verdict=(
                        FinalJudgeDecision.ACCEPTED if accepted else FinalJudgeDecision.REJECTED
                    ),
                    reasons=[
                        "The second independent proof passes." if accepted else "First fails."
                    ],
                    unresolved_obligations=([] if accepted else ["Use the second proof route."]),
                    strongest_result=("Fixture theorem" if accepted else "First partial lemma"),
                ),
                response_id=response_id,
            )
        raise AssertionError(output_type)


def candidate_package() -> CandidateProofPackage:
    return CandidateProofPackage(
        exact_theorem="Prove P(n) for every n.",
        definitions=["P is the fixture predicate."],
        lemma_dependency_graph={"main": ["lemma"]},
        full_proof="Proof of the lemma and then the theorem.",
        imported_theorems=[],
        exceptional_cases=[],
        parameter_bookkeeping=["n is arbitrary"],
        unresolved_items=[],
        quantitative_or_algorithmic=False,
    )


def passing_audit() -> AuditVerdict:
    return AuditVerdict(
        verdict=AuditDecision.PASS,
        issues=[],
        unresolved_obligations=[],
        target_matches=True,
        audit_role="fixture",
        rationale="The fixture audit checked its assigned trust boundary and found no defect.",
        checks_performed=["Compared the frozen claim and proof against the assigned audit scope."],
    )


@pytest.mark.asyncio
async def test_fast_worker_triggers_followup_while_slow_worker_is_still_running(
    tmp_path: Path,
) -> None:
    client = RollingPoolResearchClient()
    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=2,
            maximum_pending_assignments=4,
            maximum_coordinator_decisions=4,
            maximum_model_calls=11,
        ),
    )

    assert result.outcome is ResearchOutcome.ACCEPTED
    assert client.followup_started.is_set()
    assert not client.slow_completed
    assert client.slow_cancelled
    assert len(client.coordinator_payloads) == 2
    assert [decision.after_event_sequence for decision in result.coordinator_decisions] == [
        0,
        client.coordinator_payloads[1]["after_event_sequence"],
    ]
    assert result.coordinator_decisions[1].assignments[0].id == "targeted-followup"
    assert result.calls.model_calls == 11
    scheduler = json.loads((tmp_path / "coordinator" / "state.json").read_text(encoding="utf-8"))
    assert scheduler["model_calls"] == 11
    assert len(scheduler["model_call_keys"]) == 11
    assert scheduler["latest_candidate_attempt"]["judge_call_reservation_key"] is None
    assert (tmp_path / "workers" / "targeted-followup.json").is_file()
    assert not (tmp_path / "workers" / "slow-route.json").exists()


@pytest.mark.asyncio
async def test_initial_coordinator_cannot_stop_instead_of_launching_funded_portfolio(
    tmp_path: Path,
) -> None:
    class InitialStopClient:
        async def generate_structured(
            self, request: ModelRequest, output_type: type[Any]
        ) -> ModelResult[Any]:
            assert output_type is ResearchCoordinatorDecision
            payload = json.loads(request.input_text)
            assignments = [
                ResearchAssignment(
                    id=f"route-{index}",
                    approach_family=family,
                    task=f"Investigate {family}",
                    expected_output="A proof or exact obstruction",
                )
                for index, family in enumerate(
                    ("direct", "structural", "counterexample", "literature"), start=1
                )
            ]
            return ModelResult(
                parsed=ResearchCoordinatorDecision(
                    decision_id=payload["decision_id"],
                    after_event_sequence=payload["after_event_sequence"],
                    assignments=assignments,
                    rationale="Stop without executing the required portfolio.",
                    stop_recommended=True,
                    stop_reason="No research attempted.",
                ),
                response_id="initial-stop",
            )

    with pytest.raises(StageValidationError, match="must launch the funded diverse portfolio"):
        await run_adaptive_research(
            client=InitialStopClient(),  # type: ignore[arg-type]
            compiled_problem=compiled_problem(),
            research_dir=tmp_path,
            workflow_settings=ResearchWorkflowSettings(minimum_initial_assignments=4),
        )


def _decision_for_validation(**overrides: Any) -> ResearchCoordinatorDecision:
    base: dict[str, Any] = {
        "decision_id": 2,
        "after_event_sequence": 5,
        "assignments": [],
        "rationale": "Coordinator rationale.",
    }
    base.update(overrides)
    return ResearchCoordinatorDecision(**base)


_VALIDATION_KWARGS: dict[str, Any] = {
    "expected_decision": 2,
    "expected_event_sequence": 5,
    "minimum_assignments": 0,
    "maximum_new_assignments": 4,
    "initial": False,
    "known_assignment_ids": {"worker-1", "worker-2"},
    "completed_assignment_ids": {"worker-2"},
}


def test_incomplete_candidate_report_reference_is_sanitized_not_fatal() -> None:
    """Incident A: packaging a not-yet-terminal report is sanitized, not fatal."""

    warnings: list[str] = []
    decision = _decision_for_validation(
        candidate_packaging_recommended=True,
        candidate_report_ids=["worker-2", "worker-report:RA-JTZ-00000020-E1-D1-KOS"],
    )

    result = _validate_coordinator_decision(
        decision, reference_warnings=warnings, **_VALIDATION_KWARGS
    )

    # The terminal subset is preserved; the premature reference is dropped, not fatal.
    assert result.candidate_report_ids == ["worker-2"]
    assert result.candidate_packaging_recommended
    assert any("RA-JTZ-00000020-E1-D1-KOS" in warning for warning in warnings)


def test_all_incomplete_candidate_reports_drop_packaging_not_fatal() -> None:
    """Incident A variant: when every referenced report is nonterminal, packaging drops."""

    warnings: list[str] = []
    decision = _decision_for_validation(
        candidate_packaging_recommended=True,
        candidate_report_ids=["worker-report:RA-JTZ-00000021-E1-D2-AUD-ADM"],
    )

    result = _validate_coordinator_decision(
        decision, reference_warnings=warnings, **_VALIDATION_KWARGS
    )

    assert result.candidate_report_ids == []
    assert not result.candidate_packaging_recommended
    assert warnings


def test_unknown_artifact_and_directive_references_are_sanitized_not_fatal() -> None:
    """Incident B: unknown retire/redirect and retrieval IDs are dropped, not fatal."""

    warnings: list[str] = []
    decision = _decision_for_validation(
        retire_assignment_ids=["worker-1", "ghost-x"],
        redirect_assignment_ids=["ghost-y"],
    )

    result = _validate_coordinator_decision(
        decision, reference_warnings=warnings, **_VALIDATION_KWARGS
    )

    # The valid directive survives; unknown IDs are removed without raising.
    assert result.retire_assignment_ids == ["worker-1"]
    assert result.redirect_assignment_ids == []
    assert any("ghost-x" in warning for warning in warnings)


def test_structural_violation_still_raises() -> None:
    """A genuine structural violation (duplicate IDs) remains a hard failure."""

    decision = _decision_for_validation(
        assignments=[
            ResearchAssignment(id="dup", approach_family="a", task="t", expected_output="e"),
            ResearchAssignment(id="dup", approach_family="b", task="t", expected_output="e"),
        ]
    )

    with pytest.raises(StageValidationError, match="duplicate assignment IDs"):
        _validate_coordinator_decision(decision, **_VALIDATION_KWARGS)


@pytest.mark.asyncio
async def test_resume_rejects_changed_completed_coordinator_request(tmp_path: Path) -> None:
    await run_adaptive_research(
        client=SuccessfulResearchClient(),
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
    )
    request_path = tmp_path / "coordinator" / "requests" / "00000001.json"
    request_path.write_text(request_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(StageValidationError, match="request 1 is missing or changed"):
        await run_adaptive_research(
            client=SuccessfulResearchClient(),
            compiled_problem=compiled_problem(),
            research_dir=tmp_path,
        )


@pytest.mark.asyncio
async def test_resume_completes_state_first_pending_event_publication(tmp_path: Path) -> None:
    original = await run_adaptive_research(
        client=SuccessfulResearchClient(),
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
    )
    scheduler_path = tmp_path / "coordinator" / "state.json"
    scheduler = json.loads(scheduler_path.read_text(encoding="utf-8"))
    event_paths = sorted((tmp_path / "events").glob("*.json"))
    final_event_path = event_paths[-1]
    final_event = json.loads(final_event_path.read_text(encoding="utf-8"))
    assert final_event["kind"] == "research_finished"
    final_event_path.unlink()
    scheduler["pending_event"] = final_event
    scheduler_path.write_text(
        json.dumps(scheduler, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    resume_client = SuccessfulResearchClient()

    resumed = await run_adaptive_research(
        client=resume_client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
    )

    assert resumed.outcome is original.outcome is ResearchOutcome.ACCEPTED
    assert resume_client.calls == 0
    repaired = json.loads(scheduler_path.read_text(encoding="utf-8"))
    assert repaired["pending_event"] is None
    assert json.loads(final_event_path.read_text(encoding="utf-8")) == final_event


@pytest.mark.asyncio
async def test_resume_rejects_scheduler_response_missing_from_accounting_journal(
    tmp_path: Path,
) -> None:
    await run_adaptive_research(
        client=SuccessfulResearchClient(),
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
    )
    scheduler_path = tmp_path / "coordinator" / "state.json"
    scheduler = json.loads(scheduler_path.read_text(encoding="utf-8"))
    judge_response_id = scheduler["final_acceptance_gate"]["final_judge_response_id"]
    accounted = dict(scheduler["model_response_ids_by_call_key"])
    missing_judge_keys = [
        key for key, response_id in accounted.items() if response_id == judge_response_id
    ]
    assert len(missing_judge_keys) == 1
    accounted.pop(missing_judge_keys[0])

    class MissingJudgeAccountingClient(SuccessfulResearchClient):
        def accounted_request_keys(self, request_keys: Collection[str]) -> dict[str, str]:
            return {key: accounted[key] for key in request_keys if key in accounted}

    with pytest.raises(StageValidationError, match="durable model-call accounting journal"):
        await run_adaptive_research(
            client=MissingJudgeAccountingClient(),
            compiled_problem=compiled_problem(),
            research_dir=tmp_path,
        )


@pytest.mark.asyncio
async def test_interrupted_research_resume_freezes_old_requests_and_rekeys_unlaunched_work(
    tmp_path: Path,
) -> None:
    class InterruptingClient:
        async def generate_structured(
            self, request: ModelRequest, output_type: type[Any]
        ) -> ModelResult[Any]:
            payload = json.loads(request.input_text)
            if output_type is ResearchCoordinatorDecision:
                assignments = [
                    ResearchAssignment(
                        id=f"route-{index}",
                        approach_family=family,
                        task=f"Investigate {family}",
                        expected_output="A proof or exact obstruction",
                    )
                    for index, family in enumerate(
                        ("direct", "structural", "counterexample", "literature"), start=1
                    )
                ]
                return ModelResult(
                    parsed=ResearchCoordinatorDecision(
                        decision_id=payload["decision_id"],
                        after_event_sequence=payload["after_event_sequence"],
                        assignments=assignments,
                        rationale="Launch the required portfolio.",
                    ),
                    response_id="interrupting-coordinator",
                )
            raise RuntimeError("unsafe path integrity interruption")

    web_enabled = ModelSettings(web_search=True)
    with pytest.raises(RuntimeError, match="unsafe path integrity interruption"):
        await run_adaptive_research(
            client=InterruptingClient(),  # type: ignore[arg-type]
            compiled_problem=compiled_problem(),
            research_dir=tmp_path,
            workflow_settings=ResearchWorkflowSettings(
                minimum_initial_assignments=4,
                maximum_concurrent_agents=1,
                maximum_coordinator_decisions=1,
            ),
            coordinator_settings=web_enabled,
            worker_settings=web_enabled,
        )

    class ResumeClient:
        async def generate_structured(
            self, request: ModelRequest, output_type: type[Any]
        ) -> ModelResult[Any]:
            assert output_type is ResearchWorkerReport
            assignment = json.loads(request.input_text)["assignment"]
            return ModelResult(
                parsed=research_worker_report_v1(
                    assignment_id=assignment["id"],
                    status=WorkerStatus.PROGRESS,
                    formal_results=["A preserved partial result."],
                    proof_content="Proof of the partial result.",
                    exact_gap="No coordinator decisions remain.",
                    sources=[],
                    mechanism=assignment["task"],
                ),
                response_id="resumed-worker",
            )

    web_disabled = ModelSettings(web_search=False)
    result = await run_adaptive_research(
        client=ResumeClient(),  # type: ignore[arg-type]
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=1,
            maximum_coordinator_decisions=1,
        ),
        coordinator_settings=web_disabled,
        worker_settings=web_disabled,
    )

    assert result.outcome is ResearchOutcome.BUDGET_EXHAUSTED
    scheduler = json.loads((tmp_path / "coordinator" / "state.json").read_text(encoding="utf-8"))
    assert scheduler["decisions"][0]["request_settings"]["web_search"] is True
    by_id = {item["assignment"]["id"]: item for item in scheduler["assignments"]}
    assert by_id["route-1"]["request_settings"]["web_search"] is True
    assert all(
        by_id[f"route-{index}"]["request_settings"]["web_search"] is False for index in (2, 3, 4)
    )


@pytest.mark.asyncio
async def test_wal_only_coordinator_request_uses_resumed_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write = research_stage._atomic_write_immutable_json
    interrupted = False

    def interrupt_after_coordinator_wal(path: Path, value: object) -> Path:
        nonlocal interrupted
        written = original_write(path, value)
        expected = tmp_path / "coordinator" / "requests" / "00000001.json"
        if path == expected and not interrupted:
            interrupted = True
            raise RuntimeError("simulated crash after coordinator WAL")
        return written

    first_client = PolicyAssertingResearchClient(
        expected_web_search=True,
        response_prefix="before-coordinator-wal",
    )
    web_enabled = ModelSettings(web_search=True)
    with monkeypatch.context() as crash:
        crash.setattr(
            research_stage,
            "_atomic_write_immutable_json",
            interrupt_after_coordinator_wal,
        )
        with pytest.raises(RuntimeError, match="simulated crash after coordinator WAL"):
            await run_adaptive_research(
                client=first_client,
                compiled_problem=compiled_problem(),
                research_dir=tmp_path,
                workflow_settings=ResearchWorkflowSettings(
                    minimum_initial_assignments=4,
                    maximum_concurrent_agents=1,
                    maximum_coordinator_decisions=1,
                ),
                coordinator_settings=web_enabled,
                worker_settings=web_enabled,
                audit_settings=web_enabled,
                final_judge_settings=web_enabled,
            )

    assert interrupted
    assert first_client.calls == 0
    pending_state = json.loads(
        (tmp_path / "coordinator" / "state.json").read_text(encoding="utf-8")
    )
    assert pending_state["pending_coordinator_request"]["request_settings"]["web_search"] is True
    assert pending_state["model_call_keys"] == []

    resume_client = PolicyAssertingResearchClient(
        expected_web_search=False,
        response_prefix="after-coordinator-wal",
    )
    web_disabled = ModelSettings(web_search=False)
    result = await run_adaptive_research(
        client=resume_client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=1,
            maximum_coordinator_decisions=1,
        ),
        coordinator_settings=web_disabled,
        worker_settings=web_disabled,
        audit_settings=web_disabled,
        final_judge_settings=web_disabled,
    )

    assert result.outcome is ResearchOutcome.ACCEPTED
    scheduler = json.loads((tmp_path / "coordinator" / "state.json").read_text(encoding="utf-8"))
    assert scheduler["decisions"][0]["request_settings"]["web_search"] is False
    latest_attempt = scheduler["latest_candidate_attempt"]
    assert latest_attempt["packager_settings"]["web_search"] is False
    assert latest_attempt["audit_settings"]["web_search"] is False
    assert latest_attempt["judge_settings"]["web_search"] is False


@pytest.mark.asyncio
async def test_wal_only_candidate_attempt_uses_resumed_gate_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write = research_stage._atomic_write_immutable_json
    interrupted = False

    def interrupt_after_candidate_wal(path: Path, value: object) -> Path:
        nonlocal interrupted
        written = original_write(path, value)
        if (
            isinstance(value, dict)
            and value.get("kind") == "candidate_audit_started"
            and not interrupted
        ):
            interrupted = True
            raise RuntimeError("simulated crash after candidate WAL")
        return written

    first_client = PolicyAssertingResearchClient(
        expected_web_search=True,
        response_prefix="before-candidate-wal",
    )
    web_enabled = ModelSettings(web_search=True)
    with monkeypatch.context() as crash:
        crash.setattr(
            research_stage,
            "_atomic_write_immutable_json",
            interrupt_after_candidate_wal,
        )
        with pytest.raises(RuntimeError, match="simulated crash after candidate WAL"):
            await run_adaptive_research(
                client=first_client,
                compiled_problem=compiled_problem(),
                research_dir=tmp_path,
                workflow_settings=ResearchWorkflowSettings(
                    minimum_initial_assignments=4,
                    maximum_concurrent_agents=1,
                    maximum_coordinator_decisions=1,
                ),
                coordinator_settings=web_enabled,
                worker_settings=web_enabled,
                audit_settings=web_enabled,
                final_judge_settings=web_enabled,
            )

    assert interrupted
    assert first_client.output_types == [ResearchCoordinatorDecision, ResearchWorkerReport]
    pending_state = json.loads(
        (tmp_path / "coordinator" / "state.json").read_text(encoding="utf-8")
    )
    pending_attempt = pending_state["active_candidate_attempt"]
    assert pending_attempt["packager_settings"]["web_search"] is True
    assert pending_attempt["packager_response_id"] is None

    resume_client = PolicyAssertingResearchClient(
        expected_web_search=False,
        response_prefix="after-candidate-wal",
    )
    web_disabled = ModelSettings(web_search=False)
    result = await run_adaptive_research(
        client=resume_client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=1,
            maximum_coordinator_decisions=1,
        ),
        coordinator_settings=web_disabled,
        worker_settings=web_disabled,
        audit_settings=web_disabled,
        final_judge_settings=web_disabled,
    )

    assert result.outcome is ResearchOutcome.ACCEPTED
    assert resume_client.output_types == [
        CandidateProofPackage,
        AuditVerdict,
        AuditVerdict,
        AuditVerdict,
        AuditVerdict,
        FinalJudgeVerdict,
    ]
    scheduler = json.loads((tmp_path / "coordinator" / "state.json").read_text(encoding="utf-8"))
    latest_attempt = scheduler["latest_candidate_attempt"]
    assert latest_attempt["packager_settings"]["web_search"] is False
    assert latest_attempt["audit_settings"]["web_search"] is False
    assert latest_attempt["judge_settings"]["web_search"] is False


@pytest.mark.asyncio
async def test_finite_call_budget_can_exchange_unlaunched_reservations_for_feedback(
    tmp_path: Path,
) -> None:
    client = ReservationReplacementResearchClient()
    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=1,
            maximum_pending_assignments=4,
            maximum_coordinator_decisions=2,
            # This exactly funds the initial coordinator and its four required
            # assignments. Adaptive feedback must exchange, not exceed, a reserved slot.
            maximum_model_calls=5,
        ),
    )

    assert result.outcome is ResearchOutcome.BUDGET_EXHAUSTED
    assert len(client.coordinator_payloads) == 2
    assert client.worker_ids == ["fast-feedback", "targeted-replacement"]
    assert result.calls.model_calls <= 5
    scheduler = json.loads((tmp_path / "coordinator" / "state.json").read_text(encoding="utf-8"))
    assert scheduler["model_calls"] == len(scheduler["model_call_keys"])
    assert scheduler["model_calls"] <= 5
    lifecycle = {
        record["assignment"]["id"]: record["status"] for record in scheduler["assignments"]
    }
    assert lifecycle["targeted-replacement"] == "completed"
    assert {
        lifecycle[assignment_id]
        for assignment_id in (
            "replaceable-structural",
            "replaceable-counterexample",
            "replaceable-literature",
        )
    } == {"retired"}


@pytest.mark.asyncio
async def test_resumed_borrowed_headroom_uses_current_worker_policy(tmp_path: Path) -> None:
    class InterruptingHeadroomClient(ReservationReplacementResearchClient):
        async def generate_structured(
            self, request: ModelRequest, output_type: type[Any]
        ) -> ModelResult[Any]:
            if output_type is ResearchCoordinatorDecision:
                payload = json.loads(request.input_text)
                if not payload["initial_portfolio"]:
                    raise RuntimeError("simulated coordinator interruption")
            return await super().generate_structured(request, output_type)

    web_enabled = ModelSettings(web_search=True)
    with pytest.raises(RuntimeError, match="simulated coordinator interruption"):
        await run_adaptive_research(
            client=InterruptingHeadroomClient(),
            compiled_problem=compiled_problem(),
            research_dir=tmp_path,
            workflow_settings=ResearchWorkflowSettings(
                minimum_initial_assignments=4,
                maximum_concurrent_agents=1,
                maximum_pending_assignments=4,
                maximum_coordinator_decisions=2,
                maximum_model_calls=5,
            ),
            coordinator_settings=web_enabled,
            worker_settings=web_enabled,
        )

    interrupted = json.loads((tmp_path / "coordinator" / "state.json").read_text(encoding="utf-8"))
    pending = interrupted["pending_coordinator_request"]
    assert pending["headroom_assignment_id"] == "replaceable-literature"
    interrupted_by_id = {item["assignment"]["id"]: item for item in interrupted["assignments"]}
    assert interrupted_by_id["replaceable-literature"]["request_key"] is None
    assert interrupted_by_id["replaceable-literature"]["request_settings"]["web_search"] is True

    class ResumeHeadroomClient:
        def __init__(self) -> None:
            self.calls = 0

        async def generate_structured(
            self, request: ModelRequest, output_type: type[Any]
        ) -> ModelResult[Any]:
            self.calls += 1
            payload = json.loads(request.input_text)
            if output_type is ResearchCoordinatorDecision:
                assert request.settings.web_search is True
                assert payload["coordinator_headroom_borrowed_assignment_id"] == (
                    "replaceable-literature"
                )
                return ModelResult(
                    parsed=ResearchCoordinatorDecision(
                        decision_id=payload["decision_id"],
                        after_event_sequence=payload["after_event_sequence"],
                        assignments=[],
                        rationale="Retire one stale route and restore the borrowed worker.",
                        retire_assignment_ids=["replaceable-structural"],
                    ),
                    response_id=f"resume-headroom-{self.calls}",
                )
            if output_type is ResearchWorkerReport:
                assert request.settings.web_search is False
                assignment = payload["assignment"]
                return ModelResult(
                    parsed=research_worker_report_v1(
                        assignment_id=assignment["id"],
                        status=WorkerStatus.PROGRESS,
                        formal_results=["A preserved partial result."],
                        proof_content="Proof of the partial result.",
                        exact_gap="No coordinator decisions remain.",
                        sources=[],
                        mechanism=assignment["task"],
                    ),
                    response_id=f"resume-headroom-{self.calls}",
                )
            raise AssertionError(output_type)

    web_disabled = ModelSettings(web_search=False)
    result = await run_adaptive_research(
        client=ResumeHeadroomClient(),  # type: ignore[arg-type]
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=1,
            maximum_pending_assignments=4,
            maximum_coordinator_decisions=2,
            maximum_model_calls=5,
        ),
        coordinator_settings=web_disabled,
        worker_settings=web_disabled,
    )

    assert result.outcome is ResearchOutcome.BUDGET_EXHAUSTED
    scheduler = json.loads((tmp_path / "coordinator" / "state.json").read_text(encoding="utf-8"))
    assert scheduler["pending_coordinator_request"] is None
    assert scheduler["decisions"][1]["request_settings"]["web_search"] is True
    by_id = {item["assignment"]["id"]: item for item in scheduler["assignments"]}
    assert by_id["replaceable-literature"]["request_settings"]["web_search"] is False


@pytest.mark.asyncio
async def test_cleanup_time_candidate_is_audited_before_coordinator_stop(
    tmp_path: Path,
) -> None:
    client = CleanupCandidateRaceResearchClient()
    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=2,
            maximum_pending_assignments=4,
            maximum_coordinator_decisions=2,
            maximum_model_calls=10,
        ),
    )

    assert result.outcome is ResearchOutcome.ACCEPTED
    assert result.accepted_for_manuscript
    assert client.cleanup_cancelled_candidate
    assert len(client.coordinator_payloads) == 2
    assert client.gate_output_types.count(CandidateProofPackage) == 1
    assert client.gate_output_types.count(AuditVerdict) == 4
    assert client.gate_output_types.count(FinalJudgeVerdict) == 1
    assert result.calls.model_calls == 10
    candidate_report = next(
        report for report in result.worker_reports if report.assignment_id == "cleanup-candidate"
    )
    assert candidate_report.status is WorkerStatus.CANDIDATE_COMPLETE
    assert (tmp_path / "candidate" / "package.json").is_file()
    assert (tmp_path / "verdict.json").is_file()


@pytest.mark.asyncio
async def test_deferred_distinct_candidate_is_gated_even_when_coordinator_stops(
    tmp_path: Path,
) -> None:
    client = DeferredCandidateGateClient()
    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=2,
            maximum_pending_assignments=4,
            maximum_coordinator_decisions=2,
            maximum_model_calls=16,
        ),
    )

    assert result.outcome is ResearchOutcome.ACCEPTED
    assert client.packaged_ids == ["first-candidate", "second-candidate"]
    scheduler = json.loads((tmp_path / "coordinator" / "state.json").read_text())
    assert scheduler["attempted_candidate_report_sets"] == [
        ["first-candidate"],
        ["second-candidate"],
    ]
    assert scheduler["deferred_candidate_report_ids"] == []
    assert scheduler["stop_reason"] is None


@pytest.mark.asyncio
async def test_repair_can_repackage_prior_proof_with_new_candidate_report(
    tmp_path: Path,
) -> None:
    client = DeferredCandidateGateClient(package_repair_combination=True)
    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=2,
            maximum_pending_assignments=4,
            maximum_coordinator_decisions=2,
            maximum_model_calls=16,
        ),
    )

    assert result.outcome is ResearchOutcome.ACCEPTED
    assert client.packaged_ids == [
        "first-candidate",
        "first-candidate+second-candidate",
    ]
    scheduler = json.loads((tmp_path / "coordinator" / "state.json").read_text())
    assert scheduler["attempted_candidate_report_sets"] == [
        ["first-candidate"],
        ["first-candidate", "second-candidate"],
    ]


@pytest.mark.asyncio
async def test_failed_grouped_repair_preserves_singleton_candidate_for_gate(
    tmp_path: Path,
) -> None:
    client = DeferredCandidateGateClient(
        package_repair_combination=True,
        reject_repair_combination=True,
    )
    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=2,
            maximum_pending_assignments=4,
            maximum_coordinator_decisions=2,
            maximum_model_calls=22,
        ),
    )

    assert result.outcome is ResearchOutcome.ACCEPTED
    assert client.packaged_ids == [
        "first-candidate",
        "first-candidate+second-candidate",
        "second-candidate",
    ]
    scheduler = json.loads((tmp_path / "coordinator" / "state.json").read_text())
    assert scheduler["attempted_candidate_report_sets"] == [
        ["first-candidate"],
        ["first-candidate", "second-candidate"],
        ["second-candidate"],
    ]
    events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((tmp_path / "events").glob("*.json"))
    ]
    event_kinds = [event["kind"] for event in events]
    assert event_kinds.index("candidate_audit_passed") < event_kinds.index("research_finished")
    assert events[-1]["detail"] == [ResearchOutcome.ACCEPTED.value]


def test_research_workflow_defaults_use_a_large_continuous_pending_window() -> None:
    settings = ResearchWorkflowSettings()

    assert settings.orchestration_mode == "hierarchical"
    assert settings.maximum_subagents_per_agent == 4
    assert settings.hierarchical_subagent_limit == 4
    assert settings.minimum_initial_assignments == 8
    assert settings.maximum_concurrent_agents == 4
    assert settings.max_concurrent_agents == 24
    assert settings.maximum_pending_assignments == 1_024
    assert settings.maximum_coordinator_decisions == 100_000
    assert settings.maximum_coordinator_context_characters == 800_000
    assert settings.maximum_coordinator_requested_artifacts == 32
    assert "maximum_research_subagents" not in type(settings).model_fields
    assert "exact_target_persistence" not in type(settings).model_fields


@pytest.mark.asyncio
async def test_hierarchical_limits_are_given_to_coordinator_and_workers(
    tmp_path: Path,
) -> None:
    class HierarchyAwareClient(SuccessfulResearchClient):
        def __init__(self) -> None:
            super().__init__()
            self.coordinator_hierarchies: list[dict[str, Any]] = []
            self.worker_hierarchies: list[dict[str, Any]] = []

        async def generate_structured(
            self, request: ModelRequest, output_type: type[Any]
        ) -> ModelResult[Any]:
            payload = json.loads(request.input_text)
            if output_type is ResearchCoordinatorDecision:
                self.coordinator_hierarchies.append(payload["research_agent_hierarchy"])
            elif output_type is ResearchWorkerReport:
                self.worker_hierarchies.append(payload["agent_hierarchy"])
            return await super().generate_structured(request, output_type)

    client = HierarchyAwareClient()
    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            orchestration_mode="hierarchical",
            maximum_subagents_per_agent=8,
            minimum_initial_assignments=4,
            maximum_concurrent_agents=4,
            max_concurrent_agents=36,
            maximum_pending_assignments=4,
        ),
    )

    assert result.accepted_for_manuscript
    assert client.coordinator_hierarchies
    assert client.coordinator_hierarchies[0] == {
        "instruction": (
            "Each research subagent may use its bounded sub-subagent pool and must "
            "synthesize nested work into its own report."
        ),
        "max_concurrent_agents": 36,
        "max_concurrent_first_level_agents": 4,
        "max_nested_agent_depth": 1,
        "subagents_per_agent": 8,
        "mode": "hierarchical",
    }
    assert client.worker_hierarchies
    assert all(
        hierarchy["role"] == "hierarchical_research_subagent"
        and hierarchy["max_concurrent_agents"] == 36
        and hierarchy["max_concurrent_first_level_agents"] == 4
        and hierarchy["subagents_per_agent"] == 8
        and hierarchy["max_nested_agent_depth"] == 1
        for hierarchy in client.worker_hierarchies
    )


@pytest.mark.asyncio
async def test_zero_nested_limit_tells_workers_they_are_regular_subagents(
    tmp_path: Path,
) -> None:
    class RegularWorkerClient(SuccessfulResearchClient):
        def __init__(self) -> None:
            super().__init__()
            self.worker_hierarchies: list[dict[str, Any]] = []

        async def generate_structured(
            self, request: ModelRequest, output_type: type[Any]
        ) -> ModelResult[Any]:
            if output_type is ResearchWorkerReport:
                self.worker_hierarchies.append(json.loads(request.input_text)["agent_hierarchy"])
            return await super().generate_structured(request, output_type)

    client = RegularWorkerClient()
    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            orchestration_mode="hierarchical",
            maximum_subagents_per_agent=0,
            minimum_initial_assignments=4,
            maximum_concurrent_agents=4,
            maximum_pending_assignments=4,
        ),
    )

    assert result.accepted_for_manuscript
    assert client.worker_hierarchies
    assert all(
        hierarchy
        == {
            "instruction": (
                "You are a regular research subagent. Complete this assignment yourself; "
                "no nested delegation is configured."
            ),
            "role": "regular_research_subagent",
        }
        for hierarchy in client.worker_hierarchies
    )


@pytest.mark.asyncio
async def test_scientific_reduction_stop_is_declined_and_exact_research_continues(
    tmp_path: Path,
) -> None:
    class ExactTargetPersistenceClient:
        def __init__(self) -> None:
            self.calls = 0
            self.stop_declined = False

        async def generate_structured(
            self, request: ModelRequest, output_type: type[Any]
        ) -> ModelResult[Any]:
            self.calls += 1
            payload = json.loads(request.input_text)
            if output_type is ResearchCoordinatorDecision:
                policy = payload["exact_target_policy"]
                assert policy["terminal_reductions_allowed"] is False
                if payload["initial_portfolio"]:
                    parsed: BaseModel = ResearchCoordinatorDecision(
                        decision_id=payload["decision_id"],
                        after_event_sequence=payload["after_event_sequence"],
                        assignments=[
                            ResearchAssignment(
                                id=f"persistence-route-{index}",
                                approach_family=f"family-{index}",
                                task=f"Investigate exact route {index}",
                                expected_output="Exact proof or rigorous intermediate result",
                            )
                            for index in range(4)
                        ],
                        rationale="Launch diverse exact-target work.",
                    )
                elif not any(
                    event["kind"] == "coordinator_scientific_stop_declined"
                    for event in payload["unacknowledged_events"]
                ):
                    parsed = ResearchCoordinatorDecision(
                        decision_id=payload["decision_id"],
                        after_event_sequence=payload["after_event_sequence"],
                        assignments=[],
                        rationale="Keep only the reduction and stop.",
                        stop_recommended=True,
                        stop_reason="A useful reduction was proved but its target remains open.",
                        stop_category="scientific",
                    )
                else:
                    self.stop_declined = True
                    parsed = ResearchCoordinatorDecision(
                        decision_id=payload["decision_id"],
                        after_event_sequence=payload["after_event_sequence"],
                        assignments=[
                            ResearchAssignment(
                                id="exact-target-finisher",
                                approach_family="reduction-completion",
                                task=(
                                    "Prove the reduced theorem and transfer it to the exact target."
                                ),
                                expected_output="A complete proof of the unchanged claim contract",
                            )
                        ],
                        rationale="The reduction is intermediate, so discharge its exact gap.",
                    )
            elif output_type is ResearchWorkerReport:
                assert payload["exact_target_policy"]["terminal_reductions_allowed"] is False
                assignment_id = payload["assignment"]["id"]
                exact = assignment_id == "exact-target-finisher"
                parsed = research_worker_report_v1(
                    assignment_id=assignment_id,
                    status=(WorkerStatus.CANDIDATE_COMPLETE if exact else WorkerStatus.PROGRESS),
                    formal_results=[
                        "The exact target theorem." if exact else "A rigorous reduction lemma."
                    ],
                    proof_content=(
                        "Complete proof of the reduced theorem, transfer, and exact target."
                        if exact
                        else "Complete proof of an intermediate reduction only."
                    ),
                    exact_gap=(None if exact else "Prove the reduced target and transfer back."),
                    sources=[],
                    mechanism=payload["assignment"]["task"],
                )
            elif output_type is CandidateProofPackage:
                assert payload["exact_target_policy"]["terminal_reductions_allowed"] is False
                parsed = candidate_package()
            elif output_type is AuditVerdict:
                assert payload["exact_target_policy"]["terminal_reductions_allowed"] is False
                parsed = passing_audit()
            elif output_type is FinalJudgeVerdict:
                assert payload["exact_target_policy"]["terminal_reductions_allowed"] is False
                parsed = FinalJudgeVerdict(
                    verdict=FinalJudgeDecision.ACCEPTED,
                    reasons=["The complete chain proves the exact target."],
                    strongest_result="Fixture theorem",
                )
            else:  # pragma: no cover - exhaustiveness guard
                raise AssertionError(output_type)
            return ModelResult(parsed=parsed, response_id=f"persistence-{self.calls}")

    client = ExactTargetPersistenceClient()
    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=4,
            maximum_pending_assignments=4,
            maximum_coordinator_decisions=8,
        ),
    )

    assert result.outcome is ResearchOutcome.ACCEPTED
    assert client.stop_declined
    events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((tmp_path / "events").glob("*.json"))
    ]
    assert any(event["kind"] == "coordinator_scientific_stop_declined" for event in events)
    assert result.worker_reports[-1].assignment_id == "exact-target-finisher"


@pytest.mark.asyncio
async def test_model_only_refutation_stop_is_declined_and_research_continues(
    tmp_path: Path,
) -> None:
    class RefutationPersistenceClient:
        def __init__(self) -> None:
            self.calls = 0
            self.noninitial_coordinator_calls = 0
            self.saw_declined_refutation = False

        async def generate_structured(
            self, request: ModelRequest, output_type: type[Any]
        ) -> ModelResult[Any]:
            self.calls += 1
            payload = json.loads(request.input_text)
            if output_type is ResearchCoordinatorDecision:
                if payload["initial_portfolio"]:
                    parsed: BaseModel = ResearchCoordinatorDecision(
                        decision_id=payload["decision_id"],
                        after_event_sequence=payload["after_event_sequence"],
                        assignments=[
                            ResearchAssignment(
                                id=f"refutation-route-{index}",
                                approach_family=f"family-{index}",
                                task=f"Investigate exact route {index}",
                                expected_output="An exact proof or a checkable obstruction",
                            )
                            for index in range(4)
                        ],
                        rationale="Launch diverse exact-target work.",
                    )
                else:
                    self.noninitial_coordinator_calls += 1
                    if self.noninitial_coordinator_calls == 1:
                        parsed = ResearchCoordinatorDecision(
                            decision_id=payload["decision_id"],
                            after_event_sequence=payload["after_event_sequence"],
                            assignments=[],
                            rationale="Treat unsuccessful proof attempts as a refutation.",
                            stop_recommended=True,
                            stop_reason=("The attempted routes failed, so the theorem is false."),
                            stop_category="refuted",
                        )
                    elif self.noninitial_coordinator_calls == 2:
                        self.saw_declined_refutation = any(
                            event["kind"] == "coordinator_unverified_refutation_stop_declined"
                            for event in payload["unacknowledged_events"]
                        )
                        assert self.saw_declined_refutation
                        parsed = ResearchCoordinatorDecision(
                            decision_id=payload["decision_id"],
                            after_event_sequence=payload["after_event_sequence"],
                            assignments=[
                                ResearchAssignment(
                                    id="post-refutation-check",
                                    approach_family="counterexample-audit",
                                    task=(
                                        "Continue the exact theorem search and independently "
                                        "check the alleged obstruction."
                                    ),
                                    expected_output="Checkable exact-contract evidence",
                                )
                            ],
                            rationale=("The prior model-only claim was not a theorem refutation."),
                        )
                    else:
                        parsed = ResearchCoordinatorDecision(
                            decision_id=payload["decision_id"],
                            after_event_sequence=payload["after_event_sequence"],
                            assignments=[],
                            rationale="The configured research budget is now exhausted.",
                            stop_recommended=True,
                            stop_reason="No more funded research activations remain.",
                            stop_category="budget",
                        )
            elif output_type is ResearchWorkerReport:
                assignment_id = payload["assignment"]["id"]
                parsed = research_worker_report_v1(
                    assignment_id=assignment_id,
                    status=WorkerStatus.PROGRESS,
                    formal_results=[f"Partial result from {assignment_id}."],
                    proof_content="A rigorous partial argument, but not a disproof.",
                    exact_gap="The frozen exact theorem remains unresolved.",
                    sources=[],
                    mechanism=payload["assignment"]["task"],
                )
            else:  # pragma: no cover - this fixture never produces a candidate
                raise AssertionError(output_type)
            return ModelResult(parsed=parsed, response_id=f"refutation-{self.calls}")

    client = RefutationPersistenceClient()
    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=4,
            maximum_pending_assignments=8,
            maximum_coordinator_decisions=6,
        ),
    )

    assert result.outcome is ResearchOutcome.BUDGET_EXHAUSTED
    assert client.saw_declined_refutation
    assert any(report.assignment_id == "post-refutation-check" for report in result.worker_reports)
    assert any(
        "independently verified disproof" in obligation
        for obligation in result.unresolved_obligations
    )
    events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((tmp_path / "events").glob("*.json"))
    ]
    decline_events = [
        event
        for event in events
        if event["kind"] == "coordinator_unverified_refutation_stop_declined"
    ]
    assert len(decline_events) == 1
    assert "theorem is false" in decline_events[0]["detail"][0]


@pytest.mark.asyncio
async def test_default_pool_queues_8_and_runs_4_hierarchical_workers_at_once(
    tmp_path: Path,
) -> None:
    class ThirtyTwoWorkerClient:
        def __init__(self) -> None:
            self.calls = 0
            self.active_workers = 0
            self.maximum_active_workers = 0
            self.all_workers_started = asyncio.Event()
            self.worker_ids: set[str] = set()

        async def generate_structured(
            self, request: ModelRequest, output_type: type[Any]
        ) -> ModelResult[Any]:
            self.calls += 1
            assert request.settings.web_search is True
            payload = json.loads(request.input_text)
            if output_type is ResearchCoordinatorDecision:
                assert payload["maximum_concurrent_workers"] == 4
                assert payload["research_agent_hierarchy"]["max_concurrent_agents"] == 24
                assert payload["research_agent_hierarchy"]["subagents_per_agent"] == 4
                assert payload["worker_web_search_enabled"] is True
                if payload["initial_portfolio"]:
                    parsed: BaseModel = ResearchCoordinatorDecision(
                        decision_id=payload["decision_id"],
                        after_event_sequence=payload["after_event_sequence"],
                        assignments=[
                            ResearchAssignment(
                                id=f"initial-worker-{index:02d}",
                                approach_family=f"family-{index % 8}",
                                task=f"Investigate independent route {index}",
                                expected_output="A rigorous partial result or exact obstruction",
                            )
                            for index in range(8)
                        ],
                        rationale="Fill the available initial research pool.",
                    )
                else:
                    parsed = ResearchCoordinatorDecision(
                        decision_id=payload["decision_id"],
                        after_event_sequence=payload["after_event_sequence"],
                        assignments=[],
                        rationale="All bounded initial routes have reported.",
                        stop_recommended=True,
                        stop_reason="No complete proof was found in the initial portfolio.",
                        stop_category="budget",
                    )
            elif output_type is ResearchWorkerReport:
                assignment = payload["assignment"]
                assignment_id = assignment["id"]
                self.worker_ids.add(assignment_id)
                self.active_workers += 1
                self.maximum_active_workers = max(self.maximum_active_workers, self.active_workers)
                if self.active_workers == 4:
                    self.all_workers_started.set()
                try:
                    await asyncio.wait_for(self.all_workers_started.wait(), timeout=2)
                finally:
                    self.active_workers -= 1
                parsed = research_worker_report_v1(
                    assignment_id=assignment_id,
                    status=WorkerStatus.PROGRESS,
                    formal_results=[f"Partial result from {assignment_id}"],
                    proof_content="A rigorous partial argument was obtained.",
                    exact_gap="A complete proof of the target remains open.",
                    sources=[],
                    mechanism=assignment["task"],
                )
            else:  # pragma: no cover - this scenario deliberately creates no candidate
                raise AssertionError(output_type)
            return ModelResult(parsed=parsed, response_id=f"pool-{self.calls}")

    client = ThirtyTwoWorkerClient()
    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
    )

    assert result.outcome is ResearchOutcome.BUDGET_EXHAUSTED
    assert client.maximum_active_workers == 4
    assert len(client.worker_ids) == 4
    scheduler = json.loads((tmp_path / "coordinator" / "state.json").read_text())
    assert len(scheduler["assignments"]) == 8


@pytest.mark.asyncio
async def test_coordinator_input_too_large_rebuilds_a_smaller_distinct_context(
    tmp_path: Path,
) -> None:
    class ContextRetryClient:
        def __init__(self) -> None:
            self.calls = 0
            self.later_coordinator_inputs: list[str] = []

        async def generate_structured(
            self, request: ModelRequest, output_type: type[Any]
        ) -> ModelResult[Any]:
            self.calls += 1
            payload = json.loads(request.input_text)
            if output_type is ResearchCoordinatorDecision:
                if payload["initial_portfolio"]:
                    parsed: BaseModel = ResearchCoordinatorDecision(
                        decision_id=payload["decision_id"],
                        after_event_sequence=payload["after_event_sequence"],
                        assignments=[
                            ResearchAssignment(
                                id=f"large-route-{index}",
                                approach_family=f"family-{index}",
                                task=f"Investigate large route {index}",
                                expected_output="A rigorous partial result",
                            )
                            for index in range(4)
                        ],
                        rationale="Launch four large independent reports.",
                    )
                else:
                    self.later_coordinator_inputs.append(request.input_text)
                    if len(self.later_coordinator_inputs) == 1:
                        raise ModelInputTooLargeError("input_too_large at provider boundary")
                    assert payload["context_mode"] == "compact"
                    parsed = ResearchCoordinatorDecision(
                        decision_id=payload["decision_id"],
                        after_event_sequence=payload["after_event_sequence"],
                        assignments=[],
                        rationale="The bounded evidence contains no complete proof.",
                        stop_recommended=True,
                        stop_reason="No complete route remains.",
                        stop_category="budget",
                    )
            elif output_type is ResearchWorkerReport:
                assignment = payload["assignment"]
                parsed = research_worker_report_v1(
                    assignment_id=assignment["id"],
                    status=WorkerStatus.PROGRESS,
                    formal_results=[f"Partial lemma from {assignment['id']}"],
                    proof_content="Detailed mathematical argument. " * 8_000,
                    exact_gap="Complete the remaining boundary case.",
                    sources=[],
                    mechanism=assignment["task"],
                )
            else:  # pragma: no cover - workers deliberately produce no candidate
                raise AssertionError(output_type)
            return ModelResult(parsed=parsed, response_id=f"context-retry-{self.calls}")

    client = ContextRetryClient()
    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=4,
            maximum_pending_assignments=4,
        ),
    )

    assert result.outcome is ResearchOutcome.BUDGET_EXHAUSTED
    assert len(client.later_coordinator_inputs) == 2
    assert client.later_coordinator_inputs[0] != client.later_coordinator_inputs[1]
    assert len(client.later_coordinator_inputs[1]) < len(client.later_coordinator_inputs[0])
    manifests = sorted((tmp_path / "coordinator" / "context-manifests").glob("00000002-*.json"))
    assert len(manifests) == 2
    scheduler = json.loads((tmp_path / "coordinator" / "state.json").read_text())
    second_decision = scheduler["decisions"][1]["decision"]
    assert scheduler["coordinator_ack_event_sequence"] == second_decision["after_event_sequence"]
    events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((tmp_path / "events").glob("*.json"))
    ]
    assert sum(event["kind"] == "coordinator_context_compacted" for event in events) == 2
    assert sum(event["kind"] == "coordinator_input_too_large" for event in events) == 1


@pytest.mark.asyncio
async def test_oversized_mandatory_scheduler_history_uses_indexed_context_and_continues(
    tmp_path: Path,
) -> None:
    class IndexedContextClient:
        def __init__(self) -> None:
            self.calls = 0
            self.indexed_payload: dict[str, Any] | None = None

        async def generate_structured(
            self, request: ModelRequest, output_type: type[Any]
        ) -> ModelResult[Any]:
            self.calls += 1
            payload = json.loads(request.input_text)
            if output_type is ResearchCoordinatorDecision:
                if payload["initial_portfolio"]:
                    parsed: BaseModel = ResearchCoordinatorDecision(
                        decision_id=payload["decision_id"],
                        after_event_sequence=payload["after_event_sequence"],
                        assignments=[
                            ResearchAssignment(
                                id=f"indexed-route-{index}",
                                approach_family=f"family-{index}",
                                task=f"Investigate indexed route {index}",
                                expected_output="A rigorous partial result",
                            )
                            for index in range(4)
                        ],
                        rationale="Launch four independent routes.",
                    )
                else:
                    assert payload["context_mode"] == "indexed"
                    assert payload["research_agent_hierarchy"] == {
                        "instruction": (
                            "Each research subagent may use its bounded sub-subagent pool and "
                            "must synthesize nested work into its own report."
                        ),
                        "max_concurrent_agents": 28,
                        "max_concurrent_first_level_agents": 4,
                        "max_nested_agent_depth": 1,
                        "subagents_per_agent": 6,
                        "mode": "hierarchical",
                    }
                    assert payload["scheduler_state_index"]["assignment_count"] == 4
                    assert payload["unacknowledged_events"]
                    assert payload["report_summaries"]
                    self.indexed_payload = payload
                    parsed = ResearchCoordinatorDecision(
                        decision_id=payload["decision_id"],
                        after_event_sequence=payload["after_event_sequence"],
                        assignments=[],
                        rationale="The configured coordinator decision budget is exhausted.",
                        stop_recommended=True,
                        stop_reason="The bounded fixture has reached its configured limit.",
                        stop_category="budget",
                    )
            elif output_type is ResearchWorkerReport:
                assignment = payload["assignment"]
                parsed = research_worker_report_v1(
                    assignment_id=assignment["id"],
                    status=WorkerStatus.PROGRESS,
                    formal_results=[f"Partial lemma from {assignment['id']}"],
                    proof_content="A retained rigorous partial argument.",
                    exact_gap="One exact boundary lemma remains.",
                    sources=[],
                    mechanism=assignment["task"],
                    dependencies=[
                        f"dependency-{index:05d}-with-authenticated-evidence"
                        for index in range(5_000)
                    ],
                )
            else:  # pragma: no cover - this fixture creates no candidate
                raise AssertionError(output_type)
            return ModelResult(parsed=parsed, response_id=f"indexed-context-{self.calls}")

    client = IndexedContextClient()
    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            orchestration_mode="hierarchical",
            maximum_subagents_per_agent=6,
            minimum_initial_assignments=4,
            maximum_concurrent_agents=4,
            max_concurrent_agents=28,
            maximum_pending_assignments=4,
            maximum_coordinator_decisions=2,
            maximum_coordinator_context_characters=100_000,
        ),
    )

    assert result.outcome is ResearchOutcome.BUDGET_EXHAUSTED
    assert client.indexed_payload is not None
    manifests = sorted((tmp_path / "coordinator" / "context-manifests").glob("*.json"))
    assert any(json.loads(path.read_text())["mode"] == "indexed" for path in manifests)
    assert any(
        "Context mode: indexed." in event.get("detail", [])
        for event in (
            json.loads(path.read_text()) for path in sorted((tmp_path / "events").glob("*.json"))
        )
        if event.get("kind") == "coordinator_context_compacted"
    )


@pytest.mark.asyncio
async def test_repeated_context_rejection_pauses_with_partial_progress_and_resumable_request(
    tmp_path: Path,
) -> None:
    class ExhaustedContextClient:
        def __init__(self) -> None:
            self.calls = 0
            self.rejected_inputs: list[str] = []

        async def generate_structured(
            self, request: ModelRequest, output_type: type[Any]
        ) -> ModelResult[Any]:
            self.calls += 1
            payload = json.loads(request.input_text)
            if output_type is ResearchCoordinatorDecision:
                if payload["initial_portfolio"]:
                    parsed: BaseModel = ResearchCoordinatorDecision(
                        decision_id=payload["decision_id"],
                        after_event_sequence=payload["after_event_sequence"],
                        assignments=[
                            ResearchAssignment(
                                id=f"exhaustion-route-{index}",
                                approach_family=f"family-{index}",
                                task=f"Investigate exhaustion route {index}",
                                expected_output="A rigorous partial result",
                            )
                            for index in range(4)
                        ],
                        rationale="Launch four independent routes.",
                    )
                else:
                    self.rejected_inputs.append(request.input_text)
                    raise ModelInputTooLargeError("input_too_large at provider boundary")
            elif output_type is ResearchWorkerReport:
                assignment = payload["assignment"]
                parsed = research_worker_report_v1(
                    assignment_id=assignment["id"],
                    status=WorkerStatus.PROGRESS,
                    formal_results=[f"Durable partial lemma from {assignment['id']}"],
                    proof_content="Preserved mathematical progress. " * 6_000,
                    exact_gap="One boundary lemma remains.",
                    sources=[],
                    mechanism=assignment["task"],
                )
            else:  # pragma: no cover - workers deliberately produce no candidate
                raise AssertionError(output_type)
            return ModelResult(parsed=parsed, response_id=f"context-exhausted-{self.calls}")

    client = ExhaustedContextClient()
    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=4,
            maximum_pending_assignments=4,
        ),
    )

    assert result.outcome is ResearchOutcome.PAUSED_RETRIABLE
    assert result.pause_reason == "PROVIDER_CONTEXT_REJECTED_AFTER_COMPACTION"
    assert len(result.worker_reports) == 4
    assert result.strongest_result != "No complete result was established."
    assert "matek resume" in (result.resume_action or "")
    assert len(client.rejected_inputs) == 3
    assert len(set(client.rejected_inputs)) == 3
    assert all(
        len(later) < len(earlier)
        for earlier, later in zip(client.rejected_inputs, client.rejected_inputs[1:], strict=False)
    )
    scheduler = json.loads((tmp_path / "coordinator" / "state.json").read_text())
    pending = scheduler["pending_coordinator_request"]
    assert scheduler["phase"] == "running"
    assert pending["request_payload"] != json.loads(client.rejected_inputs[-1])
    assert len(serialize_coordinator_payload(pending["request_payload"])) < len(
        client.rejected_inputs[-1]
    )
    prior_events = {
        path.name: path.read_bytes() for path in sorted((tmp_path / "events").glob("*.json"))
    }

    class ResumeContextClient:
        def __init__(self) -> None:
            self.payload: dict[str, Any] | None = None

        async def generate_structured(
            self, request: ModelRequest, output_type: type[Any]
        ) -> ModelResult[Any]:
            assert output_type is ResearchCoordinatorDecision
            self.payload = json.loads(request.input_text)
            return ModelResult(
                parsed=ResearchCoordinatorDecision(
                    decision_id=self.payload["decision_id"],
                    after_event_sequence=self.payload["after_event_sequence"],
                    assignments=[],
                    rationale="The resumed bounded state remains partial.",
                    stop_recommended=True,
                    stop_reason="No complete proof remains.",
                    stop_category="budget",
                ),
                response_id="context-resume",
            )

    resume_client = ResumeContextClient()
    resumed = await run_adaptive_research(
        client=resume_client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=4,
            maximum_pending_assignments=4,
        ),
    )

    assert resumed.outcome is ResearchOutcome.BUDGET_EXHAUSTED
    assert resume_client.payload == pending["request_payload"]
    assert all(
        (tmp_path / "events" / name).read_bytes() == content
        for name, content in prior_events.items()
    )
    resumed_scheduler = json.loads((tmp_path / "coordinator" / "state.json").read_text())
    second_decision = resumed_scheduler["decisions"][1]["decision"]
    assert (
        resumed_scheduler["coordinator_ack_event_sequence"]
        == second_decision["after_event_sequence"]
    )
    sequences = [
        json.loads(path.read_text())["sequence"]
        for path in sorted((tmp_path / "events").glob("*.json"))
    ]
    assert sequences == list(range(1, len(sequences) + 1))


@pytest.mark.asyncio
async def test_api_coordinator_can_request_and_receive_omitted_report(
    tmp_path: Path,
) -> None:
    class RetrievalClient:
        def __init__(self) -> None:
            self.calls = 0
            self.requested_id: str | None = None
            self.request_payload: dict[str, Any] | None = None

        async def generate_structured(
            self, request: ModelRequest, output_type: type[Any]
        ) -> ModelResult[Any]:
            self.calls += 1
            payload = json.loads(request.input_text)
            if output_type is ResearchCoordinatorDecision:
                if payload["initial_portfolio"]:
                    parsed: BaseModel = ResearchCoordinatorDecision(
                        decision_id=payload["decision_id"],
                        after_event_sequence=payload["after_event_sequence"],
                        assignments=[
                            ResearchAssignment(
                                id=f"retrieval-route-{index}",
                                approach_family=f"family-{index}",
                                task=f"Investigate retrieval route {index}",
                                expected_output="A rigorous partial result",
                            )
                            for index in range(4)
                        ],
                        rationale="Launch retrieval fixtures.",
                    )
                elif self.requested_id is None:
                    assert payload["context_mode"] == "compact"
                    assert payload["filesystem_retrieval"]["enabled"] is False
                    included = {
                        report["assignment_id"] for report in payload["visible_worker_reports"]
                    }
                    omitted = next(
                        item
                        for item in payload["artifact_catalog"]
                        if item.get("assignment_id") not in included
                    )
                    self.requested_id = omitted["artifact_id"]
                    parsed = ResearchCoordinatorDecision(
                        decision_id=payload["decision_id"],
                        after_event_sequence=payload["after_event_sequence"],
                        assignments=[],
                        rationale="Retrieve one omitted proof before deciding.",
                        requested_artifact_ids=[self.requested_id],
                    )
                else:
                    self.request_payload = payload
                    requested = payload["requested_artifacts"]
                    assert requested
                    requested_assignment = self.requested_id.removeprefix("worker-report:")
                    assert requested[0]["assignment_id"] == requested_assignment
                    parsed = ResearchCoordinatorDecision(
                        decision_id=payload["decision_id"],
                        after_event_sequence=payload["after_event_sequence"],
                        assignments=[],
                        rationale="Requested evidence was supplied and remains partial.",
                        stop_recommended=True,
                        stop_reason="No complete proof remains.",
                        stop_category="budget",
                    )
            elif output_type is ResearchWorkerReport:
                assignment = payload["assignment"]
                parsed = research_worker_report_v1(
                    assignment_id=assignment["id"],
                    status=WorkerStatus.PROGRESS,
                    formal_results=[f"Partial lemma from {assignment['id']}"],
                    proof_content="Complete retained report prose. " * 8_000,
                    exact_gap="One exact lemma remains open.",
                    sources=[],
                    mechanism=assignment["task"],
                )
            else:  # pragma: no cover - workers deliberately produce no candidate
                raise AssertionError(output_type)
            return ModelResult(parsed=parsed, response_id=f"retrieval-{self.calls}")

    client = RetrievalClient()
    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        coordinator_can_read_files=False,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=4,
            maximum_pending_assignments=4,
        ),
    )

    assert result.outcome is ResearchOutcome.BUDGET_EXHAUSTED
    assert client.requested_id is not None
    assert client.request_payload is not None
    assert len(result.coordinator_decisions) == 3


@pytest.mark.asyncio
async def test_consequential_decision_citing_omitted_evidence_becomes_retrieval_only(
    tmp_path: Path,
) -> None:
    class ConsequentialRetrievalClient:
        def __init__(self) -> None:
            self.calls = 0
            self.omitted_id: str | None = None

        async def generate_structured(
            self, request: ModelRequest, output_type: type[Any]
        ) -> ModelResult[Any]:
            self.calls += 1
            payload = json.loads(request.input_text)
            if output_type is ResearchCoordinatorDecision:
                if payload["initial_portfolio"]:
                    parsed: BaseModel = ResearchCoordinatorDecision(
                        decision_id=payload["decision_id"],
                        after_event_sequence=payload["after_event_sequence"],
                        assignments=[
                            ResearchAssignment(
                                id=f"consequential-route-{index}",
                                approach_family=f"family-{index}",
                                task=f"Investigate consequential route {index}",
                                expected_output="A rigorous report",
                            )
                            for index in range(4)
                        ],
                        rationale="Launch four independent evidence-producing routes.",
                    )
                elif self.omitted_id is None:
                    visible = {
                        f"worker-report:{item['assignment_id']}"
                        for item in payload["visible_worker_reports"]
                    }
                    omitted = next(
                        item
                        for item in payload["artifact_catalog"]
                        if item.get("kind") == "worker_report"
                        and item["artifact_id"] not in visible
                    )
                    self.omitted_id = omitted["artifact_id"]
                    parsed = ResearchCoordinatorDecision(
                        decision_id=payload["decision_id"],
                        after_event_sequence=payload["after_event_sequence"],
                        assignments=[],
                        rationale="Package the cited omitted route.",
                        candidate_packaging_recommended=True,
                        candidate_report_ids=[self.omitted_id.removeprefix("worker-report:")],
                        supporting_evidence_ids=[self.omitted_id],
                    )
                else:
                    assert any(
                        item["assignment_id"] == self.omitted_id.removeprefix("worker-report:")
                        for item in payload["requested_artifacts"]
                    )
                    parsed = ResearchCoordinatorDecision(
                        decision_id=payload["decision_id"],
                        after_event_sequence=payload["after_event_sequence"],
                        assignments=[],
                        rationale="The retrieved evidence remains only partial.",
                        stop_recommended=True,
                        stop_reason="The bounded fixture has no accepted proof.",
                        stop_category="budget",
                    )
            elif output_type is ResearchWorkerReport:
                assignment = payload["assignment"]
                parsed = research_worker_report_v1(
                    assignment_id=assignment["id"],
                    status=WorkerStatus.PROGRESS,
                    formal_results=[f"Partial result from {assignment['id']}"],
                    proof_content="Substantive distinct proof content. " * 6_000 + assignment["id"],
                    exact_gap="An exact terminal lemma remains open.",
                    sources=[],
                    mechanism=assignment["task"],
                )
            else:  # pragma: no cover - the deferred action never reaches candidate packaging
                raise AssertionError(output_type)
            return ModelResult(parsed=parsed, response_id=f"consequential-{self.calls}")

    client = ConsequentialRetrievalClient()
    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        coordinator_can_read_files=False,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=4,
            maximum_pending_assignments=4,
            maximum_coordinator_context_characters=500_000,
        ),
    )

    assert result.outcome is ResearchOutcome.BUDGET_EXHAUSTED
    assert client.omitted_id is not None
    scheduler = json.loads((tmp_path / "coordinator" / "state.json").read_text())
    retrieval_decision = scheduler["decisions"][1]["decision"]
    assert retrieval_decision["candidate_packaging_recommended"] is False
    assert retrieval_decision["candidate_report_ids"] == []
    assert retrieval_decision["assignments"] == []
    assert retrieval_decision["requested_artifact_ids"] == [client.omitted_id]
    assert retrieval_decision["supporting_evidence_ids"] == [client.omitted_id]
    assert any(
        json.loads(path.read_text())["kind"] == "coordinator_evidence_retrieval_deferred"
        for path in (tmp_path / "events").glob("*.json")
    )
    assert not (tmp_path / "candidate" / "package.json").exists()


@pytest.mark.asyncio
async def test_research_coordinator_receives_durable_full_fidelity_continuity(
    tmp_path: Path,
) -> None:
    client = ContinuityResearchClient()
    compiled = compiled_problem()
    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled,
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=2,
            maximum_pending_assignments=4,
            maximum_coordinator_decisions=16,
        ),
    )

    assert result.outcome is ResearchOutcome.ACCEPTED
    assert result.research_subagents_assigned == 5
    assert result.research_subagents_used == 5
    assert result.research_subagents_assigned > 4  # the pending ceiling, not a run-wide cap
    assert result.rounds == []
    assert len(result.coordinator_decisions) >= 2
    later = next(
        payload
        for payload in client.coordinator_payloads
        if {report["assignment_id"] for report in payload["visible_worker_reports"]}
        == {"route-1", "route-2", "route-3", "route-4"}
    )
    assert later["compiled_prompt"] == compiled.compiled_prompt
    assert later["claim_contract"] == compiled.claim_contract.as_dict()
    assert "remaining_research_subagents" not in later
    assert later["maximum_open_assignments"] == 4
    assert later["coordinator_mode"] == "continuous_event_driven"
    assert later["after_event_sequence"] > 0
    assert later["unacknowledged_events"]
    assert any(
        event["kind"] == "worker_report_accepted" for event in later["unacknowledged_events"]
    )
    route_one = next(
        report for report in later["visible_worker_reports"] if report["assignment_id"] == "route-1"
    )
    assert route_one["results"][0]["proof_or_certificate"] == "Proof of Lemma A."
    continuity = later["research_continuity"]
    assert {route["assignment_id"] for route in continuity["promising_routes"]} == {
        "route-1",
        "route-4",
    }
    assert continuity["partial_results"]
    assert continuity["ruled_out_directions"][0]["assignment_id"] == "route-2"
    assert continuity["blocked_routes"][0]["assignment_id"] == "route-3"
    assert "A size-three object refutes the strengthening." in continuity["counterexamples"]
    assert "Boundary lemma B" in continuity["dependencies"]
    assert "Prove the reduced boundary case." in continuity["open_gaps"]
    assert (tmp_path / "continuity.json").is_file()
    assert (tmp_path / "coordinator" / "decisions" / "00000001.json").is_file()
    assert list((tmp_path / "events").glob("*.json"))
    assert (tmp_path / "coordinator" / "mailbox.json").is_file()
    assert (tmp_path / "workers" / "route-1.json").is_file()


@pytest.mark.asyncio
async def test_first_complete_proof_is_audited_without_draining_the_worker_pool(
    tmp_path: Path,
) -> None:
    client = SuccessfulResearchClient()
    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(maximum_concurrent_agents=2),
    )

    assert result.outcome == ResearchOutcome.ACCEPTED
    assert result.accepted_for_manuscript
    assert set(result.audits) == {"foundational", "domain", "hostile", "sources"}
    assert {audit.audit_role for audit in result.audits.values()} == set(result.audits)
    assert len({audit.rationale for audit in result.audits.values()}) == len(result.audits)
    assert all(audit.checks_performed for audit in result.audits.values())
    # The first two workers finish together under the two-agent semaphore. The
    # remaining routes are stopped once that visible proof passes the full gate.
    assert len(result.registry.approaches) == 2
    assert client.maximum_active == 2
    assert result.calls.model_calls == 9
    assert list((tmp_path / "candidate" / "attempts").glob("event-*-attempt-1/package.json"))
    assert (tmp_path / "candidate" / "package.json").is_file()
    assert (tmp_path / "verdict.json").is_file()


@pytest.mark.asyncio
async def test_simultaneous_research_runs_each_receive_their_configured_capacity(
    tmp_path: Path,
) -> None:
    class SharedCapacity:
        def __init__(self) -> None:
            self.active_workers = 0
            self.all_started = asyncio.Event()

    class RunLocalCapacityClient(SuccessfulResearchClient):
        def __init__(self, shared: SharedCapacity) -> None:
            super().__init__()
            self.shared = shared

        async def generate_structured(
            self, request: ModelRequest, output_type: type[Any]
        ) -> ModelResult[Any]:
            if output_type is not ResearchWorkerReport:
                return await super().generate_structured(request, output_type)
            self.shared.active_workers += 1
            if self.shared.active_workers == 4:
                self.shared.all_started.set()
            try:
                await asyncio.wait_for(self.shared.all_started.wait(), timeout=2)
                return await super().generate_structured(request, output_type)
            finally:
                self.shared.active_workers -= 1

    shared = SharedCapacity()
    clients = [RunLocalCapacityClient(shared), RunLocalCapacityClient(shared)]
    settings = ResearchWorkflowSettings(maximum_concurrent_agents=2)

    results = await asyncio.gather(
        *(
            run_adaptive_research(
                client=client,
                compiled_problem=compiled_problem(),
                research_dir=tmp_path / f"run-{index}",
                workflow_settings=settings,
            )
            for index, client in enumerate(clients)
        )
    )

    assert all(result.outcome is ResearchOutcome.ACCEPTED for result in results)
    assert shared.all_started.is_set()
    assert [client.maximum_active for client in clients] == [2, 2]


@pytest.mark.asyncio
async def test_research_records_unavailable_optional_sources_as_assumptions(
    tmp_path: Path,
) -> None:
    source = SourceLedgerEntry(
        source_id="worker-source",
        title="Optional background source",
        identifiers=["doi:10.5555/12345678"],
        evidence_claims=[{"claim": "Background context only", "source_ids": ["worker-source"]}],
    )

    result = await run_adaptive_research(
        client=SuccessfulResearchClient(worker_sources=[source]),
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        source_verifier=OfflineIdentifierVerifier(),
    )

    assert result.accepted_for_manuscript
    assert all(report.sources[0].verified is False for report in result.worker_reports)
    assert all(
        "could not be independently verified" in report.assumptions[0]
        for report in result.worker_reports
    )


@pytest.mark.asyncio
async def test_unverified_imported_theorem_blocks_research_acceptance(tmp_path: Path) -> None:
    theorem = ImportedTheorem(
        name="External theorem",
        statement="Every fixture object has property P.",
        hypotheses=["The fixture object is admissible."],
        source_id="external-theorem",
        identifiers=["arxiv:2401.01234"],
        evidence_claims=[{"claim": "The theorem statement", "source_ids": ["external-theorem"]}],
    )

    result = await run_adaptive_research(
        client=SuccessfulResearchClient(imported_theorems=[theorem]),
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            maximum_coordinator_decisions=32,
        ),
        source_verifier=OfflineIdentifierVerifier(),
    )

    assert result.outcome is ResearchOutcome.BUDGET_EXHAUSTED
    assert not result.accepted_for_manuscript
    assert "not independently verified" in result.unresolved_obligations[0]
    assert result.candidate is not None
    assert result.candidate.imported_theorems[0].verified is False
    assert not result.audits
    assert list(
        (tmp_path / "candidate" / "attempts").glob("event-*-attempt-1/source_verification.json")
    )


@pytest.mark.asyncio
async def test_completed_audits_survive_one_crash_and_resume_retries_only_missing(
    tmp_path: Path,
) -> None:
    class OneAuditCrashClient(SuccessfulResearchClient):
        def __init__(self) -> None:
            super().__init__()
            self.audit_calls: dict[str, int] = {}
            self.hostile_crashed = False

        async def generate_structured(
            self, request: ModelRequest, output_type: type[Any]
        ) -> ModelResult[Any]:
            if output_type is AuditVerdict:
                audit_name = str(json.loads(request.input_text)["audit_role"])
                self.audit_calls[audit_name] = self.audit_calls.get(audit_name, 0) + 1
                if audit_name == "hostile" and not self.hostile_crashed:
                    self.hostile_crashed = True
                    self.calls += 1
                    raise RuntimeError("hostile audit provider crashed")
            return await super().generate_structured(request, output_type)

    client = OneAuditCrashClient()
    paused = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
    )

    assert paused.outcome is ResearchOutcome.PAUSED_RETRIABLE
    assert paused.candidate is not None
    assert "hostile" not in paused.audits
    assert set(paused.audits) == {"foundational", "domain", "sources"}
    assert any(
        issue.event_kind == "candidate_audit_unavailable" for issue in paused.execution_issues
    )
    attempt_dirs = list((tmp_path / "audits" / "attempts").iterdir())
    assert len(attempt_dirs) == 1
    attempt_dir = attempt_dirs[0]
    completed_hashes = {name: (attempt_dir / f"{name}.json").read_bytes() for name in paused.audits}
    calls_before_resume = dict(client.audit_calls)

    resumed = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
    )

    assert resumed.accepted_for_manuscript
    assert client.audit_calls["hostile"] == calls_before_resume["hostile"] + 1
    for name in ("foundational", "domain", "sources"):
        assert client.audit_calls[name] == calls_before_resume[name]
    for name, contents in completed_hashes.items():
        path = next((tmp_path / "audits" / "attempts").glob(f"*/{name}.json"))
        assert path.read_bytes() == contents


@pytest.mark.asyncio
async def test_worker_schema_failure_during_candidate_audit_does_not_cancel_audits(
    tmp_path: Path,
) -> None:
    class WorkerFailureDuringAuditClient:
        def __init__(self) -> None:
            self.calls = 0
            self.audit_calls = 0

        def result(self, parsed: BaseModel) -> ModelResult[Any]:
            self.calls += 1
            return ModelResult(parsed=parsed, response_id=f"audit-race-{self.calls}")

        async def generate_structured(
            self, request: ModelRequest, output_type: type[Any]
        ) -> ModelResult[Any]:
            payload = json.loads(request.input_text)
            if output_type is ResearchCoordinatorDecision:
                return self.result(
                    ResearchCoordinatorDecision(
                        decision_id=payload["decision_id"],
                        after_event_sequence=payload["after_event_sequence"],
                        assignments=[
                            ResearchAssignment(
                                id=assignment_id,
                                approach_family=family,
                                task=f"Investigate {family}",
                                expected_output="Proof or exact obstruction",
                            )
                            for assignment_id, family in (
                                ("candidate-fast", "direct"),
                                ("schema-failure", "structural"),
                                ("slow-counterexample", "counterexample"),
                                ("slow-literature", "literature"),
                            )
                        ],
                        rationale="Exercise worker/audit isolation.",
                    )
                )
            if output_type is ResearchWorkerReport:
                assignment_id = payload["assignment"]["id"]
                if assignment_id == "candidate-fast":
                    await asyncio.sleep(0)
                    return self.result(
                        research_worker_report_v1(
                            assignment_id=assignment_id,
                            status=WorkerStatus.CANDIDATE_COMPLETE,
                            formal_results=["Fixture theorem"],
                            proof_content="Complete fixture proof.",
                            exact_gap=None,
                            sources=[],
                        )
                    )
                if assignment_id == "schema-failure":
                    await asyncio.sleep(0.02)
                    raise StructuredOutputError("worker report failed schema validation")
                await asyncio.sleep(0.08)
                return self.result(
                    research_worker_report_v1(
                        assignment_id=assignment_id,
                        status=WorkerStatus.PROGRESS,
                        formal_results=["Slow partial result"],
                        proof_content="Partial proof.",
                        exact_gap="Complete the route.",
                        sources=[],
                    )
                )
            if output_type is CandidateProofPackage:
                return self.result(candidate_package())
            if output_type is AuditVerdict:
                self.audit_calls += 1
                await asyncio.sleep(0.05)
                return self.result(passing_audit())
            if output_type is FinalJudgeVerdict:
                return self.result(
                    FinalJudgeVerdict(
                        verdict=FinalJudgeDecision.ACCEPTED,
                        reasons=["All audits passed despite the unrelated worker failure."],
                        strongest_result="Fixture theorem",
                    )
                )
            raise AssertionError(output_type)

    client = WorkerFailureDuringAuditClient()
    result = await run_adaptive_research(
        client=client,  # type: ignore[arg-type]
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=4,
        ),
    )

    assert result.accepted_for_manuscript
    assert client.audit_calls == 4
    events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((tmp_path / "events").glob("*.json"))
    ]
    assert any(event["kind"] == "worker_execution_failed" for event in events)
    assert any(issue.assignment_id == "schema-failure" for issue in result.execution_issues)


@pytest.mark.asyncio
async def test_research_reports_budget_limited_initial_portfolio(tmp_path: Path) -> None:
    client = SuccessfulResearchClient()
    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            maximum_model_calls=3,
            maximum_concurrent_agents=2,
        ),
    )
    assert result.outcome == ResearchOutcome.BUDGET_EXHAUSTED
    assert result.worker_reports == []
    assert result.calls.model_calls == 0
    assert "cannot fund" in result.unresolved_obligations[0]
    assert result.acceptance_gate is None


@pytest.mark.asyncio
async def test_legacy_scheduler_file_cannot_replace_missing_canonical_checkpoint(
    tmp_path: Path,
) -> None:
    (tmp_path / "scheduler_state.json").write_text("{}\n", encoding="utf-8")
    client = SuccessfulResearchClient()

    with pytest.raises(StageValidationError, match="not a resumable continuous-coordinator"):
        await run_adaptive_research(
            client=client,
            compiled_problem=compiled_problem(),
            research_dir=tmp_path,
        )

    assert client.calls == 0


@pytest.mark.asyncio
async def test_research_scales_initial_portfolio_to_available_budget_above_safety_floor(
    tmp_path: Path,
) -> None:
    client = SuccessfulResearchClient()

    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            maximum_model_calls=10,
            minimum_initial_assignments=9,
            maximum_concurrent_agents=9,
            scientific_phase_policy=ScientificPhasePolicy(explore_concurrency=9),
        ),
    )

    initial = result.coordinator_decisions[0]
    assert len(initial.assignments) == 9
    assert len({assignment.approach_family for assignment in initial.assignments}) >= 4
    assert result.research_subagents_assigned == 9
    assert result.research_subagents_used == 9
    assert len(result.worker_reports) == 9
    assert result.calls.model_calls == 10
    assert result.outcome is ResearchOutcome.BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_decision_cap_drains_admitted_workers_and_audits_later_candidate(
    tmp_path: Path,
) -> None:
    client = CompletionDrainResearchClient()

    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_coordinator_decisions=1,
            maximum_concurrent_agents=4,
        ),
    )

    assert result.outcome is ResearchOutcome.ACCEPTED
    assert result.accepted_for_manuscript
    assert not client.candidate_cancelled
    assert any(
        report.assignment_id == "slower-candidate"
        and report.status is WorkerStatus.CANDIDATE_COMPLETE
        for report in result.worker_reports
    )


@pytest.mark.asyncio
async def test_model_call_cap_drains_every_already_funded_worker_report(tmp_path: Path) -> None:
    client = CompletionDrainResearchClient()

    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_model_calls=5,
            maximum_concurrent_agents=4,
        ),
    )

    assert result.outcome is ResearchOutcome.BUDGET_EXHAUSTED
    assert result.calls.model_calls == 5
    assert len(result.worker_reports) == 4
    assert not client.candidate_cancelled
    assert any(
        report.assignment_id == "slower-candidate"
        and report.status is WorkerStatus.CANDIDATE_COMPLETE
        for report in result.worker_reports
    )
    assert any(
        "could not be independently audited" in item for item in result.unresolved_obligations
    )


@pytest.mark.asyncio
async def test_concurrent_worker_budget_failures_preserve_truthful_budget_outcome(
    tmp_path: Path,
) -> None:
    class ConcurrentBudgetFailureClient:
        def __init__(self) -> None:
            self.calls = 0

        async def generate_structured(
            self, request: ModelRequest, output_type: type[Any]
        ) -> ModelResult[Any]:
            self.calls += 1
            payload = json.loads(request.input_text)
            if output_type is ResearchCoordinatorDecision:
                assignments = [
                    ResearchAssignment(
                        id=f"route-{index}",
                        approach_family=family,
                        task=f"Investigate {family}",
                        expected_output="A proof or exact obstruction",
                    )
                    for index, family in enumerate(
                        ("direct", "structural", "counterexample", "literature"), start=1
                    )
                ]
                return ModelResult(
                    parsed=ResearchCoordinatorDecision(
                        decision_id=payload["decision_id"],
                        after_event_sequence=payload["after_event_sequence"],
                        assignments=assignments,
                        rationale="Launch the diverse portfolio.",
                    ),
                    response_id="budget-failure-coordinator",
                )
            await asyncio.sleep(0)
            raise BudgetExceeded("calls", 4, 5, BudgetSnapshot())

    client = ConcurrentBudgetFailureClient()
    result = await run_adaptive_research(
        client=client,  # type: ignore[arg-type]
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_concurrent_agents=4,
        ),
    )

    assert result.outcome is ResearchOutcome.BUDGET_EXHAUSTED
    assert result.acceptance_gate is None
    assert "budget exhausted" in result.unresolved_obligations[-1].casefold()


@pytest.mark.asyncio
async def test_research_honors_remaining_run_wide_model_calls(tmp_path: Path) -> None:
    client = SuccessfulResearchClient()
    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        remaining_run_model_calls=3,
    )

    assert result.outcome is ResearchOutcome.BUDGET_EXHAUSTED
    assert result.calls.model_calls == 0
    assert client.calls == 0
    assert "cannot fund" in result.unresolved_obligations[0]


class VerdictResearchClient(SuccessfulResearchClient):
    def __init__(self, decision: FinalJudgeDecision) -> None:
        super().__init__()
        self.decision = decision

    async def generate_structured(
        self, request: ModelRequest, output_type: type[Any]
    ) -> ModelResult[Any]:
        if output_type is ResearchCoordinatorDecision:
            payload = json.loads(request.input_text)
            if payload["initial_portfolio"]:
                return await super().generate_structured(request, output_type)
            self.calls += 1
            latest_verdict = payload["latest_final_judge_verdict"]
            assert latest_verdict is not None
            return ModelResult(
                parsed=ResearchCoordinatorDecision(
                    decision_id=payload["decision_id"],
                    after_event_sequence=payload["after_event_sequence"],
                    assignments=[],
                    rationale="Stop only after processing the independent gate evidence.",
                    stop_recommended=True,
                    stop_reason="The independent candidate gate is conclusive for this fixture.",
                    stop_category=(
                        "refuted" if self.decision is FinalJudgeDecision.REJECTED else "scientific"
                    ),
                ),
                response_id=f"research-{self.calls}",
            )
        if output_type is not FinalJudgeVerdict:
            return await super().generate_structured(request, output_type)
        self.calls += 1
        return ModelResult(
            parsed=FinalJudgeVerdict(
                verdict=self.decision,
                reasons=["fixture decision"],
                unresolved_obligations=["unresolved fixture obligation"],
                strongest_result="A proper partial result",
            ),
            response_id=f"research-{self.calls}",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "outcome"),
    [
        (FinalJudgeDecision.REJECTED, ResearchOutcome.BUDGET_EXHAUSTED),
        (FinalJudgeDecision.PARTIAL, ResearchOutcome.BUDGET_EXHAUSTED),
    ],
)
async def test_research_preserves_rejected_and_partial_candidates(
    tmp_path: Path,
    decision: FinalJudgeDecision,
    outcome: ResearchOutcome,
) -> None:
    result = await run_adaptive_research(
        client=VerdictResearchClient(decision),
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(maximum_coordinator_decisions=2),
    )
    assert result.outcome == outcome
    assert not result.accepted_for_manuscript
    assert (tmp_path / "candidate" / "package.json").is_file()
    assert result.unresolved_obligations


EXACT_FALSE_TARGET = "For every integer n, n + 1 = n."


def false_exact_compiled_problem() -> CompiledProblem:
    return CompiledProblem(
        title="False fixture theorem",
        normalized_statement=EXACT_FALSE_TARGET,
        claim_contract={
            "quantifiers": "for every integer n",
            "domain": "integers",
            "conclusion": "n + 1 = n",
        },
        compiled_prompt=covered_compiled_prompt(),
        source_ledger=[],
        unresolved_ambiguities=[],
    )


class ExactCounterexampleResearchClient:
    def __init__(
        self,
        *,
        interrupt_falsifier: bool = False,
        parented_support_obligation: bool = False,
        graph_target_id: str | None = None,
        dependency_node_id: str | None = None,
    ) -> None:
        self.calls = 0
        self.interrupt_falsifier = interrupt_falsifier
        self.parented_support_obligation = parented_support_obligation
        self.graph_target_id = graph_target_id
        self.dependency_node_id = dependency_node_id
        self.role_calls: list[CounterexampleAuditRole] = []

    async def generate_structured(
        self, request: ModelRequest, output_type: type[Any]
    ) -> ModelResult[Any]:
        self.calls += 1
        payload = json.loads(request.input_text)
        if output_type is ResearchCoordinatorDecision:
            return ModelResult(
                parsed=ResearchCoordinatorDecision(
                    decision_id=payload["decision_id"],
                    after_event_sequence=payload["after_event_sequence"],
                    assignments=[
                        ResearchAssignment(
                            id=f"exact-refutation-route-{index}",
                            approach_family=family,
                            task=f"Investigate {family}",
                            expected_output="A typed result or exact obstruction",
                            target_node_ids=(
                                [self.graph_target_id] if self.graph_target_id is not None else []
                            ),
                        )
                        for index, family in enumerate(
                            ("counterexample", "direct", "structural", "literature"),
                            start=1,
                        )
                    ],
                    rationale=(
                        "Launch a diverse exact-target portfolio."
                        + (
                            " Reviewed graph "
                            + str(payload["knowledge_graph_memory"]["graph_revision"])
                            + "."
                            if self.graph_target_id is not None
                            else ""
                        )
                    ),
                ),
                response_id=f"exact-refutation-{self.calls}",
            )
        if output_type is ResearchWorkerReport:
            assignment_id = payload["assignment"]["id"]
            if assignment_id == "exact-refutation-route-1":
                report = ResearchWorkerReport(
                    assignment_id=assignment_id,
                    results=[
                        ScientificResult(
                            local_key="main-exact-counterexample",
                            kind=ScientificResultKind.COUNTEREXAMPLE,
                            exact_statement=EXACT_FALSE_TARGET,
                            scope=ScientificScope.MAIN,
                            proof_or_certificate=(
                                "Take n = 0. It is an integer, but 0 + 1 = 1 and 1 is not equal "
                                "to 0, so the exact universal conclusion fails."
                            ),
                            dependency_node_ids=(
                                [self.dependency_node_id]
                                if self.dependency_node_id is not None
                                else []
                            ),
                            disposition=ScientificResultDisposition.REFUTED_MECHANISM,
                        )
                    ],
                    unresolved_obligations=(
                        [
                            {
                                "local_key": "unresolved-certificate-domain",
                                "exact_statement": "The witness belongs to the exact domain.",
                                "conclusion": "The witness belongs to the exact domain.",
                                "parent_result_keys": ["main-exact-counterexample"],
                            }
                        ]
                        if self.parented_support_obligation
                        else []
                    ),
                    branch_outcome=BranchOutcome.REFUTED,
                    mechanism="A complete explicit exact-target instance.",
                )
            else:
                report = ResearchWorkerReport(
                    assignment_id=assignment_id,
                    results=[
                        ScientificResult(
                            local_key=f"partial-{assignment_id}",
                            kind=ScientificResultKind.LEMMA,
                            exact_statement=f"Partial statement from {assignment_id}.",
                            scope=ScientificScope.BRANCH,
                            proof_or_certificate="A rigorous but nonterminal branch calculation.",
                            exact_gap="Connect this calculation to the frozen main theorem.",
                            disposition=ScientificResultDisposition.PARTIAL,
                        )
                    ],
                    unresolved_obligations=[
                        {
                            "local_key": f"gap-{assignment_id}",
                            "exact_statement": "Connect the branch to the exact main theorem.",
                            "conclusion": "The exact main theorem follows.",
                        }
                    ],
                    branch_outcome=BranchOutcome.BLOCKED,
                    mechanism="A nonterminal comparison route.",
                )
            return ModelResult(parsed=report, response_id=f"exact-refutation-{self.calls}")
        if output_type is CounterexampleAuditResponse:
            role = CounterexampleAuditRole(payload["audit_role"])
            self.role_calls.append(role)
            if role is CounterexampleAuditRole.FALSIFIER and self.interrupt_falsifier:
                raise RuntimeError("fixture interruption after verifier persistence")
            packet = payload["exact_counterexample_packet"]
            return ModelResult(
                parsed=CounterexampleAuditResponse(
                    audit_role=role,
                    audit_id=packet["audit_id"],
                    target_statement_sha256=packet["target_statement_sha256"],
                    decision=CounterexampleAuditDecision.PASS,
                    statement_aligned=True,
                    every_hypothesis_satisfied=True,
                    claimed_failure_demonstrated=True,
                    certificate_valid=True,
                    witness_or_instance="n = 0",
                    hypothesis_check="0 is an integer, so the quantified domain includes it.",
                    conclusion_evaluation="0 + 1 = 1, and 1 is not equal to 0.",
                    checks_performed=["Checked the complete exact certificate independently."],
                    hostile_or_boundary_tests=(
                        ["Attacked the domain, boundary instance, and quantifier order."]
                        if role is CounterexampleAuditRole.FALSIFIER
                        else []
                    ),
                    rationale="The exact-target counterexample survives independent review.",
                ),
                response_id=f"exact-refutation-{role.value}-{self.calls}",
            )
        raise AssertionError(output_type)


@pytest.mark.asyncio
async def test_exact_main_counterexample_requires_two_audits_and_resumes_missing_role(
    tmp_path: Path,
) -> None:
    interrupted_client = ExactCounterexampleResearchClient(interrupt_falsifier=True)
    paused = await run_adaptive_research(
        client=interrupted_client,
        compiled_problem=false_exact_compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_pending_assignments=4,
            maximum_concurrent_agents=4,
            maximum_coordinator_decisions=1,
        ),
    )
    assert paused.outcome is ResearchOutcome.PAUSED_RETRIABLE
    assert paused.pause_reason == "COUNTEREXAMPLE_AUDIT_INCOMPLETE"
    assert interrupted_client.role_calls.count(CounterexampleAuditRole.VERIFIER) == 1
    assert interrupted_client.role_calls.count(CounterexampleAuditRole.FALSIFIER) == 1

    resume_client = ExactCounterexampleResearchClient()
    resumed = await run_adaptive_research(
        client=resume_client,
        compiled_problem=false_exact_compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_pending_assignments=4,
            maximum_concurrent_agents=4,
            maximum_coordinator_decisions=1,
        ),
    )
    assert resumed.outcome is ResearchOutcome.REJECTED
    assert resumed.refutation_gate is not None
    assert resumed.refutation_gate.verified_refutation is not None
    assert resume_client.role_calls == [CounterexampleAuditRole.FALSIFIER]
    events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((tmp_path / "events").glob("*.json"))
    ]
    assert sum(event["kind"] == "main_counterexample_audit_passed" for event in events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "support_changed",
    [False, True],
    ids=["unrelated-revision", "genuine-support-change"],
)
async def test_counterexample_resume_reuses_frozen_nomination_after_unrelated_graph_revision(
    tmp_path: Path,
    support_changed: bool,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    problem = project / "problem.md"
    problem.write_text(EXACT_FALSE_TARGET + "\n", encoding="utf-8")
    run_id = "run-frozen-counterexample"
    graph = KnowledgeGraph(project, "frozen-counterexample")
    problem_id, _ = graph.initialize_problem(
        source_path=problem,
        problem_text=problem.read_text(encoding="utf-8"),
        run_id=run_id,
    )
    compiled = false_exact_compiled_problem()
    graph.record_compiled_problem(
        problem_id=problem_id,
        run_id=run_id,
        compiled_problem=compiled.model_dump(mode="json"),
    )
    target_id = graph.main_claim_id(problem_id)
    setup_tasks, _, _ = graph.record_assignment_tasks(
        problem_id=problem_id,
        run_id=run_id,
        decision_id=0,
        assignments=[
            {
                "id": "trusted-premise-setup",
                "task": "Record an independently checked domain fact.",
                "expected_output": "One audited premise.",
                "target_node_ids": [target_id],
            }
        ],
    )
    dependency_id = "CLM-DEPEND01"
    trusted_dependency = GraphPatch(
        base_graph_revision=graph.load_state().revision,
        run_id=run_id,
        task_id=setup_tasks["trusted-premise-setup"],
        agent_role="research-auditor-fixture",
        create_nodes=[
            GraphNodeCreate(
                matek_id=dependency_id,
                node_type=NodeType.CLAIM,
                claim_type=ClaimType.LEMMA,
                title="Audited integer-domain fact",
                body="## Exact statement\n\nZero is an integer.",
                epistemic_status=EpistemicStatus.AUDIT_PASSED,
                workflow_status=WorkflowStatus.COMPLETE,
            )
        ],
    )
    assert graph.merge_patch(
        trusted_dependency,
        problem_id=problem_id,
        operation_id="trusted-premise-setup",
    ).committed

    research_dir = project / ".matek" / "runs" / run_id / "research"
    settings = ResearchWorkflowSettings(
        minimum_initial_assignments=4,
        maximum_pending_assignments=4,
        maximum_concurrent_agents=4,
        maximum_coordinator_decisions=1,
    )
    interrupted_client = ExactCounterexampleResearchClient(
        interrupt_falsifier=True,
        graph_target_id=target_id,
        dependency_node_id=dependency_id,
    )
    paused = await run_adaptive_research(
        client=interrupted_client,
        compiled_problem=compiled,
        research_dir=research_dir,
        workflow_settings=settings,
        knowledge_graph=graph,
        graph_problem_id=problem_id,
        run_id=run_id,
    )
    assert paused.outcome is ResearchOutcome.PAUSED_RETRIABLE
    scheduler_path = research_dir / "coordinator" / "state.json"
    paused_scheduler = json.loads(scheduler_path.read_text(encoding="utf-8"))
    paused_assignment = next(
        item
        for item in paused_scheduler["assignments"]
        if item["assignment"]["id"] == "exact-refutation-route-1"
    )
    paused_audit = paused_assignment["exact_counterexample_audits"][0]
    frozen_audit_id = paused_audit["audit_id"]
    frozen_nomination_sha256 = paused_audit["nomination_sha256"]
    graph_revision_at_pause = graph.load_state().revision

    unrelated_patch = GraphPatch(
        base_graph_revision=graph_revision_at_pause,
        run_id=run_id,
        task_id=setup_tasks["trusted-premise-setup"],
        agent_role=("research-auditor-fixture" if support_changed else "research-worker"),
        create_nodes=(
            []
            if support_changed
            else [
                GraphNodeCreate(
                    matek_id="SRC-UNRELATED1",
                    node_type=NodeType.SOURCE,
                    title="Unrelated archival source",
                    body="An unrelated source note that is not counterexample support.",
                )
            ]
        ),
        update_nodes=(
            [
                GraphNodeUpdate(
                    matek_id=dependency_id,
                    evidence=["A second independent domain check was recorded."],
                    reason="Attach fresh support evidence without changing the exact claim.",
                )
            ]
            if support_changed
            else []
        ),
    )
    assert graph.merge_patch(
        unrelated_patch,
        problem_id=problem_id,
        operation_id=(
            "support-post-audit-revision" if support_changed else "unrelated-post-audit-revision"
        ),
    ).committed
    assert graph.load_state().revision != graph_revision_at_pause

    resume_client = ExactCounterexampleResearchClient(
        graph_target_id=target_id,
        dependency_node_id=dependency_id,
    )
    resumed = await run_adaptive_research(
        client=resume_client,
        compiled_problem=compiled,
        research_dir=research_dir,
        workflow_settings=settings,
        knowledge_graph=graph,
        graph_problem_id=problem_id,
        run_id=run_id,
    )

    assert resumed.outcome is ResearchOutcome.REJECTED
    assert resume_client.role_calls == (
        [CounterexampleAuditRole.VERIFIER, CounterexampleAuditRole.FALSIFIER]
        if support_changed
        else [CounterexampleAuditRole.FALSIFIER]
    )
    resumed_scheduler = json.loads(scheduler_path.read_text(encoding="utf-8"))
    resumed_assignment = next(
        item
        for item in resumed_scheduler["assignments"]
        if item["assignment"]["id"] == "exact-refutation-route-1"
    )
    resumed_audits = resumed_assignment["exact_counterexample_audits"]
    if support_changed:
        assert len(resumed_audits) == 2
        assert resumed_audits[0]["audit_id"] == frozen_audit_id
        assert resumed_audits[0]["nomination_sha256"] == frozen_nomination_sha256
        assert resumed_audits[0]["superseded"] is True
        assert "support changed" in resumed_audits[0]["superseded_reason"].casefold()
        assert resumed_audits[1]["audit_id"] != frozen_audit_id
        assert resumed_audits[1]["superseded"] is False
        assert sorted(
            path.name for path in (research_dir / "counterexample-audits").iterdir()
        ) == sorted([frozen_audit_id, resumed_audits[1]["audit_id"]])
    else:
        assert len(resumed_audits) == 1
        assert resumed_audits[0]["audit_id"] == frozen_audit_id
        assert resumed_audits[0]["nomination_sha256"] == frozen_nomination_sha256
        assert [path.name for path in (research_dir / "counterexample-audits").iterdir()] == [
            frozen_audit_id
        ]


@pytest.mark.asyncio
async def test_untrusted_counterexample_support_is_durably_nonterminal(tmp_path: Path) -> None:
    client = ExactCounterexampleResearchClient(parented_support_obligation=True)
    result = await run_adaptive_research(
        client=client,
        compiled_problem=false_exact_compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(
            minimum_initial_assignments=4,
            maximum_pending_assignments=4,
            maximum_concurrent_agents=4,
            maximum_coordinator_decisions=1,
        ),
    )
    assert result.outcome is not ResearchOutcome.REJECTED
    assert result.refutation_gate is None
    assert client.role_calls == []
    scheduler = json.loads((tmp_path / "coordinator" / "state.json").read_text(encoding="utf-8"))
    assignment = next(
        item
        for item in scheduler["assignments"]
        if item["assignment"]["id"] == "exact-refutation-route-1"
    )
    assert "main-exact-counterexample" in assignment["counterexample_support_rejections"]
    events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((tmp_path / "events").glob("*.json"))
    ]
    assert sum(event["kind"] == "main_counterexample_support_rejected" for event in events) == 1


class RepairResearchClient(SuccessfulResearchClient):
    def __init__(self) -> None:
        super().__init__()
        self.coordinator_payloads: list[dict[str, Any]] = []
        self.judgments = 0
        self.release_unrelated_workers = asyncio.Event()

    async def generate_structured(
        self, request: ModelRequest, output_type: type[Any]
    ) -> ModelResult[Any]:
        if output_type is ResearchCoordinatorDecision:
            self.calls += 1
            payload = json.loads(request.input_text)
            self.coordinator_payloads.append(payload)
            if payload["initial_portfolio"]:
                assignments = [
                    ResearchAssignment(
                        id=f"initial-{index}",
                        approach_family=family,
                        task=f"Investigate {family}",
                        expected_output="formal content",
                    )
                    for index, family in enumerate(
                        (
                            "direct",
                            "structural",
                            "counterexample",
                            "literature",
                            "probabilistic",
                            "computational",
                            "inductive",
                            "algebraic",
                            "geometric",
                            "topological",
                            "analytic",
                            "combinatorial",
                            "variational",
                            "spectral",
                            "logical",
                            "formalization-aware",
                        ),
                        start=1,
                    )
                ]
                retire_assignment_ids: list[str] = []
            else:
                assert payload["audit_repair_obligations"] == ["prove the missing boundary case"]
                assert payload["approach_registry"]
                assignments = [
                    ResearchAssignment(
                        id="boundary-repair",
                        approach_family="boundary repair",
                        task="Prove the missing boundary case",
                        expected_output="a complete boundary proof",
                    )
                ]
                retire_assignment_ids = [
                    assignment["id"]
                    for assignment in [
                        *payload["queued_assignments"],
                        *payload["active_assignments"],
                    ]
                ]
            return ModelResult(
                parsed=ResearchCoordinatorDecision(
                    decision_id=payload["decision_id"],
                    after_event_sequence=payload["after_event_sequence"],
                    assignments=assignments,
                    rationale="Adaptive fixture plan",
                    retire_assignment_ids=retire_assignment_ids,
                ),
                response_id=f"research-{self.calls}",
            )
        if output_type is ResearchWorkerReport:
            self.calls += 1
            assignment = json.loads(request.input_text)["assignment"]
            assignment_id = assignment["id"]
            if assignment_id.startswith("initial-") and assignment_id != "initial-1":
                await self.release_unrelated_workers.wait()
            return ModelResult(
                parsed=research_worker_report_v1(
                    assignment_id=assignment_id,
                    status=WorkerStatus.CANDIDATE_COMPLETE,
                    formal_results=[f"Proof route from {assignment_id}"],
                    proof_content="Detailed proof.",
                    exact_gap=None,
                    sources=[],
                    mechanism=assignment["task"],
                ),
                response_id=f"research-{self.calls}",
            )
        if output_type is CandidateProofPackage:
            self.calls += 1
            payload = json.loads(request.input_text)
            package = candidate_package()
            if "boundary-repair" in payload["candidate_trigger_assignment_ids"]:
                package = package.model_copy(
                    update={
                        "full_proof": (
                            "Proof of the lemma, the repaired boundary case, and the theorem."
                        ),
                        "parameter_bookkeeping": [
                            "n is arbitrary",
                            "the boundary case is discharged",
                        ],
                    }
                )
            return ModelResult(parsed=package, response_id=f"research-{self.calls}")
        if output_type is FinalJudgeVerdict:
            self.calls += 1
            self.judgments += 1
            verdict = (
                FinalJudgeDecision.REPAIRABLE
                if self.judgments == 1
                else FinalJudgeDecision.ACCEPTED
            )
            return ModelResult(
                parsed=FinalJudgeVerdict(
                    verdict=verdict,
                    unresolved_obligations=(
                        ["prove the missing boundary case"] if self.judgments == 1 else []
                    ),
                    strongest_result="Fixture theorem",
                ),
                response_id=f"research-{self.calls}",
            )
        return await super().generate_structured(request, output_type)


@pytest.mark.asyncio
async def test_failed_early_audit_returns_exact_obligations_to_the_coordinator(
    tmp_path: Path,
) -> None:
    client = RepairResearchClient()
    result = await run_adaptive_research(
        client=client,
        compiled_problem=compiled_problem(),
        research_dir=tmp_path,
        workflow_settings=ResearchWorkflowSettings(maximum_coordinator_decisions=8),
    )
    assert result.outcome == ResearchOutcome.ACCEPTED
    assert result.repair_rounds == 1
    assert client.judgments == 2
    assert [decision.decision_id for decision in result.coordinator_decisions] == [1, 2]
    assert client.coordinator_payloads[1]["audit_repair_obligations"] == [
        "prove the missing boundary case"
    ]
    assert "boundary-repair" in {report.assignment_id for report in result.worker_reports}
    assert (
        len(list((tmp_path / "candidate" / "attempts").glob("event-*-attempt-*/package.json"))) == 2
    )


def accepted_research() -> ResearchResult:
    package = candidate_package()
    gate = ResearchAcceptanceGate(
        accepted=True,
        candidate_sha256=sha256_json(package),
        claim_contract_sha256=sha256_text(
            json.dumps(MANUSCRIPT_CLAIM_CONTRACT, sort_keys=True, ensure_ascii=False)
        ),
        mandatory_audits=["foundational", "domain", "hostile", "sources"],
        final_judge_response_id="judge-1",
    )
    verdict = FinalJudgeVerdict(
        verdict=FinalJudgeDecision.ACCEPTED,
        strongest_result=package.exact_theorem,
    )
    return ResearchResult(
        outcome=ResearchOutcome.ACCEPTED,
        rounds=[],
        worker_reports=[],
        registry=ApproachRegistry(),
        candidate=package,
        audits={name: passing_audit() for name in gate.mandatory_audits},
        final_verdict=verdict,
        strongest_result=package.exact_theorem,
        acceptance_gate=gate,
        calls={"model_calls": 0},
    )


def manuscript_draft() -> ManuscriptDraft:
    exact_theorem = candidate_package().exact_theorem
    candidate_sha256 = sha256_json(candidate_package())
    claim_contract_sha256 = sha256_text(
        json.dumps(MANUSCRIPT_CLAIM_CONTRACT, sort_keys=True, ensure_ascii=False)
    )
    related_excerpt = (
        "Smith's prior study establishes a nearby lemma for restricted fixture objects and "
        "supplies the historical comparison used here"
    )
    difference_excerpt = (
        "Unlike that work, our theorem removes the restriction and treats every natural-number "
        "instance without changing the predicate or its domain"
    )
    advance_excerpt = (
        "The present advance is a complete uniform argument connecting the fixture lemma to all "
        "parameters, including the boundary case"
    )
    return ManuscriptDraft(
        paper_tex=(
            "\\documentclass{article}\n"
            "\\begin{document}\n"
            "\\section{Introduction}\n"
            f"{related_excerpt} \\cite{{smith2020}}.\n"
            f"{difference_excerpt}.\n"
            f"{advance_excerpt}.\n"
            f"{exact_theorem}\n"
            "\\section{Related Work}\n"
            "Smith analyzes restricted fixture objects and proves the comparison lemma used to "
            "locate this result in the literature \\cite{smith2020}. The published argument does "
            "not claim the uniform theorem proved here, and we distinguish its hypotheses from "
            "ours explicitly.\n"
            "\\section{Proof}\nThe complete proof is given here.\n"
            "\\section*{Statement of AI Usage}\n"
            "The MATEK system with GPT 5.6 was used in this work "
            "\\cite{matekSoftwareFixture,matekWhitepaperFixture}.\n"
            "\\bibliography{references}\n"
            "\\end{document}\n"
        ),
        references_bib=(
            "@article{smith2020, title={A Real Paper}, author={Smith, Ada}, "
            "year={2020}, journal={Journal of Fixtures}, doi={10.5555/12345678}}\n"
            "@misc{matekSoftwareFixture, author={MATEK test-fixture contributors}, "
            "title={MATEK: Multi-Agent Theorem Exploration through Knowledge-Graph "
            "Memory}, year={2099}, howpublished={Software repository}, "
            f"url={{{MATEK_FIXTURE_REPOSITORY_URL}}}}}\n"
            "@misc{matekWhitepaperFixture, author={MATEK test-fixture contributors}, "
            "title={MATEK: Multi-Agent Theorem Exploration through Knowledge-Graph "
            "Memory}, year={2099}, howpublished={arXiv preprint}, "
            f"eprint={{{MATEK_FIXTURE_WHITEPAPER_ID}}}, archiveprefix={{arXiv}}}}\n"
        ),
        claims=[{"claim": "fixture theorem", "proof": "main"}],
        proof_dependency_graph={"main": ["lemma"]},
        introduction_coverage=IntroductionCoverage(
            related_work_excerpt=related_excerpt,
            difference_from_prior_work_excerpt=difference_excerpt,
            advance_over_prior_work_excerpt=advance_excerpt,
            citation_keys=["smith2020"],
        ),
        frozen_claim_fidelity=FrozenClaimFidelity(
            candidate_sha256=candidate_sha256,
            claim_contract_sha256=claim_contract_sha256,
            exact_theorem=exact_theorem,
            manuscript_main_claim=exact_theorem,
            exact_match=True,
        ),
    )


def verified_bibliography() -> BibliographyAudit:
    return BibliographyAudit(
        status=BibliographyStatus.VERIFIED,
        entries=[
            BibliographyEntryAudit(
                citation_key="smith2020",
                status=BibliographyEntryStatus.VERIFIED,
                exists=True,
                exact_title_verified=True,
                authors_verified=True,
                year_verified=True,
                venue_or_status_verified=True,
                stable_identifier_checked=True,
                characterization_supported=True,
                theorem_hypotheses_supported=True,
                authoritative_evidence=["https://doi.org/10.5555/12345678"],
            ),
            BibliographyEntryAudit(
                citation_key="matekSoftwareFixture",
                status=BibliographyEntryStatus.VERIFIED,
                exists=True,
                exact_title_verified=True,
                authors_verified=True,
                year_verified=True,
                venue_or_status_verified=True,
                stable_identifier_checked=True,
                characterization_supported=True,
                theorem_hypotheses_supported=True,
                authoritative_evidence=[MATEK_FIXTURE_REPOSITORY_URL],
            ),
            BibliographyEntryAudit(
                citation_key="matekWhitepaperFixture",
                status=BibliographyEntryStatus.VERIFIED,
                exists=True,
                exact_title_verified=True,
                authors_verified=True,
                year_verified=True,
                venue_or_status_verified=True,
                stable_identifier_checked=True,
                characterization_supported=True,
                theorem_hypotheses_supported=True,
                authoritative_evidence=[MATEK_FIXTURE_WHITEPAPER_URL],
            ),
        ],
        claim_checks=[
            RelatedWorkClaimAudit(
                claim="Prior work established a nearby lemma.",
                citation_keys=["smith2020"],
                supported=True,
                evidence=["https://doi.org/10.5555/12345678"],
            )
        ],
        blocking_issues=[],
    )


def test_bibliography_entry_requires_explicit_theorem_hypothesis_verification() -> None:
    entry = verified_bibliography().entries[0]
    payload = entry.model_dump(exclude={"theorem_hypotheses_supported"})
    with pytest.raises(ValueError, match="theorem_hypotheses_supported"):
        BibliographyEntryAudit.model_validate(payload)


class PdfBackend:
    def __init__(self) -> None:
        self.requests: list[CommandRequest] = []

    async def run(self, request: CommandRequest) -> CommandResult:
        self.requests.append(request)
        (request.cwd / "paper.pdf").write_bytes(b"%PDF-fixture")
        return CommandResult(
            argv=request.argv,
            cwd=request.cwd,
            exit_code=0,
            stdout="Latexmk: All targets are up-to-date",
            stderr="",
            duration_seconds=0.1,
        )


class NoPdfBackend(PdfBackend):
    async def run(self, request: CommandRequest) -> CommandResult:
        self.requests.append(request)
        return CommandResult(
            argv=request.argv,
            cwd=request.cwd,
            exit_code=0,
            stdout="Latexmk claimed success without an output",
            stderr="",
            duration_seconds=0.1,
        )


@pytest.mark.asyncio
async def test_manuscript_requires_verified_bibliography_and_real_pdf(tmp_path: Path) -> None:
    client = StaticClient(
        [manuscript_draft(), verified_bibliography()],
        tool_metadata=web_source_metadata(),
    )
    backend = PdfBackend()
    result = await generate_manuscript(
        client=client,
        backend=backend,
        research_result=accepted_research(),
        claim_contract=MANUSCRIPT_CLAIM_CONTRACT,
        source_ledger=[],
        manuscript_dir=tmp_path,
    )
    assert result.outcome == ManuscriptOutcome.COMPILED
    assert result.passed_lean_gate
    assert result.bibliography_verified
    assert result.related_work.ai_usage_disclosure_verified
    assert result.related_work.matek_repository_citation_key == "matekSoftwareFixture"
    assert result.related_work.matek_whitepaper_citation_key == "matekWhitepaperFixture"
    assert result.latex_build is not None and result.latex_build.pdf_path is not None
    assert len(backend.requests) == 1
    assert "-no-shell-escape" in backend.requests[0].argv
    assert "-norc" in backend.requests[0].argv
    writer_payload = json.loads(client.requests[0].input_text)
    assert "statement_of_ai_usage" in writer_payload["mandatory_structured_content"]


@pytest.mark.asyncio
async def test_missing_matek_whitepaper_metadata_warns_and_compiles_draft(
    tmp_path: Path,
) -> None:
    draft = manuscript_draft().model_copy(deep=True)
    draft.paper_tex = draft.paper_tex.replace(
        "matekSoftwareFixture,matekWhitepaperFixture", "matekSoftwareFixture"
    )
    draft.references_bib = draft.references_bib.split("@misc{matekWhitepaperFixture", maxsplit=1)[0]
    audit = verified_bibliography().model_copy(deep=True)
    audit.entries = [
        entry for entry in audit.entries if entry.citation_key != "matekWhitepaperFixture"
    ]
    result = await generate_manuscript(
        client=StaticClient([draft, audit], tool_metadata=web_source_metadata()),
        backend=PdfBackend(),
        research_result=accepted_research(),
        claim_contract=MANUSCRIPT_CLAIM_CONTRACT,
        source_ledger=[],
        manuscript_dir=tmp_path,
    )

    assert result.outcome is ManuscriptOutcome.DRAFT_WITH_WARNINGS
    assert result.publication_status is PublicationStatus.BLOCKED_METADATA
    assert result.bibliography_verified
    assert result.latex_build is not None and result.latex_build.passed
    assert result.permits_formalization
    assert result.related_work.matek_whitepaper_citation_pending
    assert any(finding.code == "matek_whitepaper_citation_pending" for finding in result.findings)
    assert "\\PackageError" not in result.draft.paper_tex


def test_tex_macro_theorem_matches_structured_frozen_claim() -> None:
    draft = manuscript_draft().model_copy(deep=True)
    exact_theorem = candidate_package().exact_theorem
    draft.paper_tex = draft.paper_tex.replace(exact_theorem, "\\FixtureTheorem", 1)
    draft.paper_tex = draft.paper_tex.replace(
        "\\begin{document}",
        f"\\newcommand{{\\FixtureTheorem}}{{{exact_theorem}}}\n\\begin{{document}}",
    )
    draft.frozen_claim_fidelity.manuscript_main_claim = "\\FixtureTheorem"

    validation = validate_related_work(
        draft.paper_tex,
        draft.references_bib,
        introduction_coverage=draft.introduction_coverage,
        frozen_claim_fidelity=draft.frozen_claim_fidelity,
        expected_candidate_sha256=sha256_json(candidate_package()),
        expected_claim_contract_sha256=sha256_text(
            json.dumps(MANUSCRIPT_CLAIM_CONTRACT, sort_keys=True, ensure_ascii=False)
        ),
        expected_exact_theorem=exact_theorem,
    )

    assert validation.frozen_claim_fidelity_verified
    assert not any(finding.code == "manuscript_claim_drift" for finding in validation.findings)


def test_semantically_equivalent_introduction_coverage_need_not_be_verbatim() -> None:
    draft = manuscript_draft().model_copy(deep=True)
    draft.introduction_coverage.related_work_excerpt = (
        "The historical comparison comes from Smith's study of restricted fixture objects, "
        "where a nearby lemma is established."
    )
    draft.introduction_coverage.difference_from_prior_work_excerpt = (
        "Our theorem instead handles every natural-number instance and removes the earlier "
        "restriction while preserving the predicate and domain."
    )
    draft.introduction_coverage.advance_over_prior_work_excerpt = (
        "The advance gives a uniform complete argument from the fixture lemma through all "
        "parameters and the boundary case."
    )

    validation = validate_related_work(
        draft.paper_tex,
        draft.references_bib,
        introduction_coverage=draft.introduction_coverage,
        frozen_claim_fidelity=draft.frozen_claim_fidelity,
        expected_candidate_sha256=sha256_json(candidate_package()),
        expected_claim_contract_sha256=sha256_text(
            json.dumps(MANUSCRIPT_CLAIM_CONTRACT, sort_keys=True, ensure_ascii=False)
        ),
        expected_exact_theorem=candidate_package().exact_theorem,
    )

    assert validation.introduction_coverage_verified


@pytest.mark.asyncio
async def test_repairable_findings_consume_rounds_and_preserve_each_draft(
    tmp_path: Path,
) -> None:
    first_draft = manuscript_draft().model_copy(deep=True)
    first_draft.paper_tex = first_draft.paper_tex.replace("\\section{Related Work}", "")
    repaired_draft = manuscript_draft()
    client = StaticClient(
        [first_draft, verified_bibliography(), repaired_draft, verified_bibliography()],
        tool_metadata=web_source_metadata(),
    )

    result = await generate_manuscript(
        client=client,
        backend=PdfBackend(),
        research_result=accepted_research(),
        claim_contract=MANUSCRIPT_CLAIM_CONTRACT,
        source_ledger=[],
        manuscript_dir=tmp_path,
        maximum_correction_cycles=1,
    )

    assert result.outcome is ManuscriptOutcome.COMPILED
    assert result.correction_cycles == 1
    assert result.selected_draft_cycle == 1
    for cycle in (0, 1):
        assert (tmp_path / "drafts" / str(cycle) / "paper.tex").is_file()
        assert (tmp_path / "drafts" / str(cycle) / "validation.json").is_file()


@pytest.mark.asyncio
async def test_manuscript_repairs_missing_statement_and_still_audits_and_builds(
    tmp_path: Path,
) -> None:
    draft = manuscript_draft().model_copy(deep=True)
    statement_start = draft.paper_tex.index("\\section*{Statement of AI Usage}")
    bibliography_start = draft.paper_tex.index("\\bibliography{references}")
    draft.paper_tex = draft.paper_tex[:statement_start] + draft.paper_tex[bibliography_start:]
    client = StaticClient([draft, verified_bibliography()], tool_metadata=web_source_metadata())
    backend = PdfBackend()

    result = await generate_manuscript(
        client=client,
        backend=backend,
        research_result=accepted_research(),
        claim_contract=MANUSCRIPT_CLAIM_CONTRACT,
        source_ledger=[],
        manuscript_dir=tmp_path,
        maximum_correction_cycles=0,
    )

    assert result.outcome == ManuscriptOutcome.DRAFT_WITH_WARNINGS
    assert result.manuscript_status is ManuscriptStatus.DRAFT_WITH_WARNINGS
    assert not result.related_work.ai_usage_disclosure_verified
    assert any("Statement of AI Usage" in issue for issue in result.related_work.issues)
    assert len(client.requests) == 2
    assert result.bibliography_audit is not None
    assert backend.requests
    assert result.permits_formalization


@pytest.mark.asyncio
async def test_false_citation_blocks_latex_and_lean(tmp_path: Path) -> None:
    audit = verified_bibliography().model_copy(deep=True)
    audit.status = BibliographyStatus.REJECTED
    audit.entries[0].status = BibliographyEntryStatus.NONEXISTENT
    audit.entries[0].exists = False
    audit.blocking_issues = ["No authoritative record exists."]
    client = StaticClient([manuscript_draft(), audit])
    backend = PdfBackend()
    result = await generate_manuscript(
        client=client,
        backend=backend,
        research_result=accepted_research(),
        claim_contract=MANUSCRIPT_CLAIM_CONTRACT,
        source_ledger=[],
        manuscript_dir=tmp_path,
        maximum_correction_cycles=0,
    )
    assert result.outcome == ManuscriptOutcome.PUBLICATION_BLOCKED
    assert result.publication_status is PublicationStatus.BLOCKED_INTEGRITY
    assert not result.passed_lean_gate
    assert backend.requests


@pytest.mark.asyncio
async def test_manuscript_rejects_missing_introduction_coverage_and_frozen_claim_drift(
    tmp_path: Path,
) -> None:
    bad_coverage = manuscript_draft().model_copy(deep=True)
    bad_coverage.introduction_coverage.advance_over_prior_work_excerpt = (
        "This purported advance does not occur anywhere in the generated introduction text"
    )
    coverage_client = StaticClient(
        [bad_coverage, verified_bibliography()], tool_metadata=web_source_metadata()
    )
    coverage_result = await generate_manuscript(
        client=coverage_client,
        backend=PdfBackend(),
        research_result=accepted_research(),
        claim_contract=MANUSCRIPT_CLAIM_CONTRACT,
        source_ledger=[],
        manuscript_dir=tmp_path / "coverage",
        maximum_correction_cycles=0,
    )
    assert coverage_result.outcome == ManuscriptOutcome.DRAFT_WITH_WARNINGS
    assert not coverage_result.related_work.introduction_coverage_verified
    assert coverage_result.calls.model_calls == 2
    assert coverage_result.permits_formalization

    drifted = manuscript_draft().model_copy(deep=True)
    drifted.frozen_claim_fidelity.candidate_sha256 = "f" * 64
    drift_client = StaticClient([drifted])
    drift_result = await generate_manuscript(
        client=drift_client,
        backend=PdfBackend(),
        research_result=accepted_research(),
        claim_contract=MANUSCRIPT_CLAIM_CONTRACT,
        source_ledger=[],
        manuscript_dir=tmp_path / "drift",
    )
    assert drift_result.outcome == ManuscriptOutcome.PUBLICATION_BLOCKED
    assert not drift_result.related_work.frozen_claim_fidelity_verified
    assert any("candidate hash" in issue for issue in drift_result.related_work.issues)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "escape",
    [r"\immediate\write18{touch escaped}", r"\input{/etc/passwd}"],
)
async def test_manuscript_rejects_tex_shell_and_file_io_escapes(
    tmp_path: Path,
    escape: str,
) -> None:
    draft = manuscript_draft().model_copy(deep=True)
    draft.paper_tex = draft.paper_tex.replace("\\section{Proof}", f"{escape}\n\\section{{Proof}}")
    client = StaticClient([draft])
    backend = PdfBackend()
    result = await generate_manuscript(
        client=client,
        backend=backend,
        research_result=accepted_research(),
        claim_contract=MANUSCRIPT_CLAIM_CONTRACT,
        source_ledger=[],
        manuscript_dir=tmp_path,
    )
    assert result.outcome == ManuscriptOutcome.PUBLICATION_BLOCKED
    assert any("prohibited TeX escape" in issue for issue in result.related_work.issues)
    assert len(client.requests) == 2
    assert not backend.requests


@pytest.mark.asyncio
async def test_manuscript_rejects_deliberate_tex_build_failure_command(tmp_path: Path) -> None:
    draft = manuscript_draft().model_copy(deep=True)
    draft.paper_tex = draft.paper_tex.replace(
        "\\section{Proof}",
        "\\PackageError{matek}{missing metadata}{do not fabricate}\n\\section{Proof}",
    )
    backend = PdfBackend()
    result = await generate_manuscript(
        client=StaticClient([draft]),
        backend=backend,
        research_result=accepted_research(),
        claim_contract=MANUSCRIPT_CLAIM_CONTRACT,
        source_ledger=[],
        manuscript_dir=tmp_path,
        maximum_correction_cycles=0,
    )

    assert result.outcome is ManuscriptOutcome.PUBLICATION_BLOCKED
    assert result.publication_status is PublicationStatus.BLOCKED_INTEGRITY
    assert any(finding.code == "deliberate_latex_failure" for finding in result.findings)
    assert not backend.requests


@pytest.mark.asyncio
async def test_disabled_bibliography_search_preserves_draft_and_build(tmp_path: Path) -> None:
    client = StaticClient([manuscript_draft()])
    destination = tmp_path / "manuscript"
    backend = PdfBackend()
    result = await generate_manuscript(
        client=client,
        backend=backend,
        research_result=accepted_research(),
        claim_contract=MANUSCRIPT_CLAIM_CONTRACT,
        source_ledger=[],
        manuscript_dir=destination,
        verifier_settings=ModelSettings(web_search=False),
        maximum_correction_cycles=0,
    )
    assert result.outcome is ManuscriptOutcome.DRAFT_WITH_WARNINGS
    assert result.bibliography_audit is None
    assert (destination / "drafts" / "0" / "validation.json").is_file()
    assert backend.requests


@pytest.mark.asyncio
async def test_arbitrary_bibliography_evidence_cannot_pass_gate(tmp_path: Path) -> None:
    audit = verified_bibliography().model_copy(deep=True)
    audit.entries[0].authoritative_evidence = ["the publisher says this is real"]
    audit.claim_checks[0].evidence = ["another model confirmed the theorem"]
    # The deliberately type-invalid fixture should trigger Pydantic's serializer warning;
    # capture it so the release suite remains warning-clean while still exercising rejection.
    with pytest.warns(UserWarning, match="Pydantic serializer warnings"):
        result = await generate_manuscript(
            client=StaticClient(
                [manuscript_draft(), audit],
                tool_metadata=web_source_metadata(),
            ),
            backend=PdfBackend(),
            research_result=accepted_research(),
            claim_contract=MANUSCRIPT_CLAIM_CONTRACT,
            source_ledger=[],
            manuscript_dir=tmp_path,
            maximum_correction_cycles=0,
        )
    assert result.outcome == ManuscriptOutcome.DRAFT_WITH_WARNINGS
    assert not result.bibliography_verified
    assert result.permits_formalization


@pytest.mark.asyncio
async def test_bibliography_evidence_must_match_provider_tool_sources(tmp_path: Path) -> None:
    result = await generate_manuscript(
        client=StaticClient([manuscript_draft(), verified_bibliography()]),
        backend=PdfBackend(),
        research_result=accepted_research(),
        claim_contract=MANUSCRIPT_CLAIM_CONTRACT,
        source_ledger=[],
        manuscript_dir=tmp_path,
        maximum_correction_cycles=0,
    )
    assert result.outcome == ManuscriptOutcome.DRAFT_WITH_WARNINGS
    audit_text = (tmp_path / "bibliography_audit.md").read_text(encoding="utf-8")
    assert "independently resolved" in audit_text


@pytest.mark.asyncio
async def test_bibliography_resume_reuses_draft_without_repeating_initial_writer(
    tmp_path: Path,
) -> None:
    rejected_audit = verified_bibliography().model_copy(deep=True)
    rejected_audit.status = BibliographyStatus.CORRECTIONS_REQUIRED
    rejected_audit.entries[0].status = BibliographyEntryStatus.AMBIGUOUS
    rejected_audit.entries[0].exists = False
    rejected_audit.blocking_issues = ["Disambiguate the source."]
    rejected_audit.correction_plan = ["Replace the ambiguous record with the DOI record."]
    research = accepted_research()
    first = await generate_manuscript(
        client=StaticClient([manuscript_draft(), rejected_audit]),
        backend=PdfBackend(),
        research_result=research,
        claim_contract=MANUSCRIPT_CLAIM_CONTRACT,
        source_ledger=[],
        manuscript_dir=tmp_path,
        maximum_correction_cycles=0,
    )
    assert first.outcome == ManuscriptOutcome.DRAFT_WITH_WARNINGS

    resume_client = StaticClient(
        [manuscript_draft(), verified_bibliography()],
        tool_metadata=web_source_metadata(),
    )
    resumed = await resume_manuscript_bibliography(
        client=resume_client,
        backend=PdfBackend(),
        previous_result=first,
        research_result=research,
        claim_contract=MANUSCRIPT_CLAIM_CONTRACT,
        source_ledger=[],
        manuscript_dir=tmp_path,
    )
    assert resumed.outcome == ManuscriptOutcome.COMPILED
    assert resumed.correction_cycles == 1
    assert resumed.calls.model_calls == 2
    first_new_payload = json.loads(resume_client.requests[0].input_text)
    assert first_new_payload["previous_manuscript"] == first.draft.model_dump(mode="json")
    assert "mandatory_validation_corrections" in first_new_payload
    assert (tmp_path / "result.json").is_file()


@pytest.mark.asyncio
async def test_latex_exit_zero_without_pdf_fails_gate(tmp_path: Path) -> None:
    result = await generate_manuscript(
        client=StaticClient(
            [manuscript_draft(), verified_bibliography()],
            tool_metadata=web_source_metadata(),
        ),
        backend=NoPdfBackend(),
        research_result=accepted_research(),
        claim_contract=MANUSCRIPT_CLAIM_CONTRACT,
        source_ledger=[],
        manuscript_dir=tmp_path,
        maximum_correction_cycles=0,
    )
    assert result.outcome == ManuscriptOutcome.PUBLICATION_BLOCKED
    assert not result.passed_lean_gate
    assert result.latex_build is not None
    assert "nonempty paper.pdf" in result.latex_build.diagnostics[0]


def compiled_manuscript(research: ResearchResult, root: Path) -> ManuscriptResult:
    pdf = root / "paper.pdf"
    pdf.write_bytes(b"%PDF-fixture")
    return ManuscriptResult(
        outcome=ManuscriptOutcome.COMPILED,
        draft=manuscript_draft(),
        bibliography_audit=verified_bibliography(),
        bibliography_verified=True,
        related_work=RelatedWorkValidation(
            passed=True,
            has_related_work_section=True,
            cited_keys=["smith2020"],
            bibliography_keys=["smith2020"],
            missing_bibliography_keys=[],
            issues=[],
        ),
        latex_build=LatexBuildResult(
            passed=True,
            argv=["latexmk"],
            exit_code=0,
            diagnostics=[],
            pdf_path=pdf,
        ),
        correction_cycles=0,
        research_gate=research.acceptance_gate,
        calls={"model_calls": 2},
    )


def mandatory_alignment_checks(*, failed_field: str | None = None) -> list[AlignmentCheck]:
    return [
        AlignmentCheck(
            field=field,
            passed=field != failed_field,
            explanation=(
                f"The Lean statement preserves the frozen {field.replace('_', ' ')} field."
                if field != failed_field
                else f"The Lean statement changes the frozen {field.replace('_', ' ')} field."
            ),
        )
        for field in MANDATORY_ALIGNMENT_FIELDS
    ]


def test_claim_alignment_requires_every_mandated_scientific_check() -> None:
    with pytest.raises(ValueError, match="missing mandatory checks"):
        ClaimAlignment(
            status=AlignmentStatus.ALIGNED,
            mathematical_back_translation="For every n, P n.",
            checks=[
                AlignmentCheck(
                    field="quantifiers",
                    passed=True,
                    explanation="The universal quantifier is unchanged.",
                )
            ],
            required_edits=[],
        )

    alignment = ClaimAlignment(
        status=AlignmentStatus.ALIGNED,
        mathematical_back_translation="For every n, P n.",
        checks=mandatory_alignment_checks(failed_field="finiteness"),
        required_edits=[],
    )
    assert not alignment.fully_aligned


class LeanModelClient:
    def __init__(self) -> None:
        self.calls = 0

    async def generate_structured(
        self, request: ModelRequest, output_type: type[Any]
    ) -> ModelResult[Any]:
        del request
        self.calls += 1
        if output_type is LeanFeasibilityAssessment:
            parsed: BaseModel = LeanFeasibilityAssessment(
                classification=LeanFeasibilityClass.MAIN_THEOREM,
                explanation="The proposition is directly expressible.",
                expected_mathlib_dependencies=[],
                difficult_components=[],
                computational_certificates=[],
                paper_proof_mismatches=[],
            )
        elif output_type is LeanStatementDraft:
            parsed = LeanStatementDraft(
                challenge_lean="theorem main_result : True := by\n  sorry\n",
                statement_explanation="The theorem says True.",
                claim_map={"conclusion": "True"},
                theorem_name="main_result",
            )
        elif output_type is ClaimAlignment:
            parsed = ClaimAlignment(
                status=AlignmentStatus.ALIGNED,
                mathematical_back_translation="True.",
                checks=mandatory_alignment_checks(),
                required_edits=[],
            )
        else:  # pragma: no cover
            raise AssertionError(output_type)
        return ModelResult(parsed=parsed, response_id=f"lean-model-{self.calls}")


class EditingCodex:
    def __init__(self) -> None:
        self.requests: list[CodexRequest] = []

    async def execute(self, request: CodexRequest) -> CodexResult:
        self.requests.append(request)
        challenge = request.cwd / "challenge.lean"
        challenge.write_text(
            challenge.read_text(encoding="utf-8").replace("sorry", "trivial"),
            encoding="utf-8",
        )
        return CodexResult(exit_code=0, stdout='{"type":"turn.completed"}\n', stderr="")


class SymlinkAttackCodex:
    def __init__(self, attack: str, external_target: Path | None = None) -> None:
        self.attack = attack
        self.external_target = external_target

    async def execute(self, request: CodexRequest) -> CodexResult:
        if self.attack == "leak":
            assert self.external_target is not None
            (request.cwd / "leak.lean").symlink_to(self.external_target)
        elif self.attack == "build_log":
            (request.cwd / "build.log").symlink_to("challenge.lean")
        else:  # pragma: no cover - test fixture misuse
            raise AssertionError(self.attack)
        return CodexResult(exit_code=0, stdout='{"type":"turn.completed"}\n', stderr="")


class BroaderEditingCodex(EditingCodex):
    def __init__(self, project: Path) -> None:
        super().__init__()
        self.project = project

    async def execute(self, request: CodexRequest) -> CodexResult:
        result = await super().execute(request)
        (self.project / "notes.txt").write_text("modified\n", encoding="utf-8")
        (self.project / "old.bin").unlink()
        (self.project / "new.json").write_text('{"added": true}\n', encoding="utf-8")
        return result


class LeanBackend:
    def __init__(self) -> None:
        self.requests: list[CommandRequest] = []

    async def run(self, request: CommandRequest) -> CommandResult:
        self.requests.append(request)
        output = (
            "'main_result' depends on no axioms"
            if "_MatekAxiomCheck.lean" in request.argv[-1]
            else ""
        )
        return CommandResult(
            argv=request.argv,
            cwd=request.cwd,
            exit_code=0,
            stdout=output,
            stderr="",
            duration_seconds=0.1,
        )


@pytest.mark.asyncio
async def test_lean_pipeline_uses_alignment_codex_and_deterministic_verifier(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "lean-toolchain").write_text("leanprover/lean4:stable", encoding="utf-8")
    research = accepted_research()
    manuscript = compiled_manuscript(research, tmp_path)
    codex = EditingCodex()
    backend = LeanBackend()
    lean_dir = project / ".matek" / "runs" / "fixture" / "lean"

    result = await run_lean_pipeline(
        client=LeanModelClient(),
        codex_client=codex,
        backend=backend,
        research_result=research,
        manuscript_result=manuscript,
        claim_contract={"conclusion": "True"},
        lean_dir=lean_dir,
        lean_project_root=project,
        workflow_settings=LeanWorkflowSettings(maximum_codex_iterations=1),
    )

    assert result.outcome == LeanOutcome.VERIFIED
    assert result.verification is not None and result.verification.passed
    assert result.calls.model_calls == 3
    assert result.calls.codex_calls == 1
    assert codex.requests[0].cwd == lean_dir.resolve()
    assert codex.requests[0].writable_paths == (lean_dir.resolve(),)
    assert len(backend.requests) == 2
    assert not scan_generated_lean(lean_dir, ["sorry", "admit", "by?", "TODO"])[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("attack", ["leak", "build_log"])
async def test_lean_pipeline_rejects_generated_symlinks_before_read_or_build(
    tmp_path: Path,
    attack: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "lean-toolchain").write_text("leanprover/lean4:stable", encoding="utf-8")
    external = tmp_path / "external-secret.txt"
    external.write_text("sk-super-secret-must-not-be-read", encoding="utf-8")
    research = accepted_research()
    manuscript = compiled_manuscript(research, tmp_path)
    backend = LeanBackend()
    lean_dir = project / ".matek" / "runs" / "fixture" / "lean"

    with pytest.raises(StageValidationError, match="must be a non-symlink"):
        await run_lean_pipeline(
            client=LeanModelClient(),
            codex_client=SymlinkAttackCodex(attack, external),
            backend=backend,
            research_result=research,
            manuscript_result=manuscript,
            claim_contract={"conclusion": "True"},
            lean_dir=lean_dir,
            lean_project_root=project,
            workflow_settings=LeanWorkflowSettings(maximum_codex_iterations=1),
        )

    assert not backend.requests
    assert external.read_text(encoding="utf-8") == "sk-super-secret-must-not-be-read"
    assert "sorry" in (lean_dir / "challenge.lean").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_lean_pipeline_audits_all_broader_project_edits(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "lean-toolchain").write_text("leanprover/lean4:stable", encoding="utf-8")
    (project / "notes.txt").write_text("original\n", encoding="utf-8")
    (project / "old.bin").write_bytes(b"old")
    research = accepted_research()
    manuscript = compiled_manuscript(research, tmp_path)
    lean_dir = project / ".matek" / "runs" / "fixture" / "lean"

    result = await run_lean_pipeline(
        client=LeanModelClient(),
        codex_client=BroaderEditingCodex(project),
        backend=LeanBackend(),
        research_result=research,
        manuscript_result=manuscript,
        claim_contract={"conclusion": "True"},
        lean_dir=lean_dir,
        lean_project_root=project,
        workflow_settings=LeanWorkflowSettings(
            maximum_codex_iterations=1,
            allow_project_edits=True,
        ),
    )

    assert result.outcome == LeanOutcome.VERIFIED
    iteration_dir = lean_dir / "iterations" / "1"
    writable = json.loads((iteration_dir / "writable_paths.json").read_text(encoding="utf-8"))
    assert writable["allow_project_edits"] is True
    assert str(project.resolve()) in writable["writable_paths"]
    manifest = json.loads((iteration_dir / "project_changes.json").read_text(encoding="utf-8"))
    changes = {item["path"]: item for item in manifest["changes"]}
    assert changes["notes.txt"]["status"] == "modified"
    assert changes["notes.txt"]["before"]["sha256"]
    assert changes["notes.txt"]["after"]["sha256"]
    assert changes["old.bin"]["status"] == "deleted"
    assert changes["new.json"]["status"] == "added"
    rendered_diff = (iteration_dir / "project_changes.diff").read_text(encoding="utf-8")
    assert all(name in rendered_diff for name in ("notes.txt", "old.bin", "new.json"))


@pytest.mark.asyncio
async def test_lean_pipeline_reuses_completed_iteration_without_repeating_codex(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "lean-toolchain").write_text("leanprover/lean4:stable", encoding="utf-8")
    research = accepted_research()
    manuscript = compiled_manuscript(research, tmp_path)
    lean_dir = project / ".matek" / "runs" / "fixture" / "lean"
    codex = EditingCodex()
    settings = LeanWorkflowSettings(maximum_codex_iterations=1)

    first = await run_lean_pipeline(
        client=LeanModelClient(),
        codex_client=codex,
        backend=LeanBackend(),
        research_result=research,
        manuscript_result=manuscript,
        claim_contract={"conclusion": "True"},
        lean_dir=lean_dir,
        lean_project_root=project,
        workflow_settings=settings,
    )
    second = await run_lean_pipeline(
        client=LeanModelClient(),
        codex_client=codex,
        backend=LeanBackend(),
        research_result=research,
        manuscript_result=manuscript,
        claim_contract={"conclusion": "True"},
        lean_dir=lean_dir,
        lean_project_root=project,
        workflow_settings=settings,
    )

    assert first.outcome == second.outcome == LeanOutcome.VERIFIED
    assert len(codex.requests) == 1
    assert second.calls.codex_calls == 0
    assert "trivial" in (lean_dir / "challenge.lean").read_text(encoding="utf-8")
    assert (lean_dir / "iterations" / "1" / "record.json").is_file()


@pytest.mark.asyncio
async def test_lean_allows_repairable_publication_findings_after_research_acceptance(
    tmp_path: Path,
) -> None:
    research = accepted_research()
    manuscript = compiled_manuscript(research, tmp_path)
    manuscript.outcome = ManuscriptOutcome.DRAFT_WITH_WARNINGS
    manuscript.manuscript_status = ManuscriptStatus.DRAFT_WITH_WARNINGS
    manuscript.publication_status = PublicationStatus.BLOCKED_BIBLIOGRAPHY
    manuscript.bibliography_verified = False
    manuscript.findings = [
        ManuscriptFinding(
            code="incomplete_bibliography_metadata",
            severity=ManuscriptFindingSeverity.REPAIRABLE,
            message="A venue field remains incomplete.",
        )
    ]
    result = await run_lean_pipeline(
        client=LeanModelClient(),
        codex_client=EditingCodex(),
        backend=LeanBackend(),
        research_result=research,
        manuscript_result=manuscript,
        claim_contract={"conclusion": "True"},
        lean_dir=tmp_path / "lean",
        lean_project_root=tmp_path,
    )

    assert result.alignment is not None and result.alignment.fully_aligned


def test_lean_scanner_rejects_opaque_target_shortcuts(tmp_path: Path) -> None:
    (tmp_path / "Main.lean").write_text(
        "opaque hiddenProof : False := by contradiction\n",
        encoding="utf-8",
    )
    prohibited, suspicious = scan_generated_lean(
        tmp_path,
        ["sorry", "admit", "by?", "TODO"],
        "main_result",
    )
    assert not prohibited
    assert any("opaque hiddenProof" in item for item in suspicious)
