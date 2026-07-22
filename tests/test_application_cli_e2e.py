from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Collection
from pathlib import Path
from typing import Any, Literal

import pytest
import typer
from pydantic import BaseModel
from typer.testing import CliRunner

import matek_theorem_agent.cli as cli_module
from matek_theorem_agent.application import (
    LEAN_CONSENT_TIMEOUT_SECONDS,
    LeanConsentOutcome,
    LeanConsentRequest,
    WorkflowDependencies,
    WorkflowOptions,
    WorkflowRunner,
)
from matek_theorem_agent.budget import BudgetExceeded
from matek_theorem_agent.cli import app
from matek_theorem_agent.codex_client import CodexRequest, CodexResult
from matek_theorem_agent.config import (
    AppConfig,
    BackendSettings,
    CodexSettings,
    LeanSettings,
    Limits,
    ManuscriptSettings,
    ResearchSettings,
    merge_config,
)
from matek_theorem_agent.execution.base import CommandRequest, CommandResult
from matek_theorem_agent.intake import ingest_problem
from matek_theorem_agent.knowledge_graph import GraphNotInitializedError, KnowledgeGraph, NodeType
from matek_theorem_agent.models import ScientificStatus, StageName, StageStatus
from matek_theorem_agent.openai_client import ModelRequest, ModelResult
from matek_theorem_agent.progress import Ascension
from matek_theorem_agent.source_provenance import (
    SourceVerificationRecord,
    SourceVerificationReport,
    SourceVerificationStatus,
    WebDisabledSourceVerifier,
)
from matek_theorem_agent.stages.common import sha256_json, sha256_text
from matek_theorem_agent.stages.compile_prompt import (
    CompiledProblem,
    PromptCompilationStatus,
    PromptPlaceholderRepair,
)
from matek_theorem_agent.stages.lean import (
    MANDATORY_ALIGNMENT_FIELDS,
    AlignmentCheck,
    AlignmentStatus,
    ClaimAlignment,
    LeanFeasibilityAssessment,
    LeanFeasibilityClass,
    LeanStatementDraft,
)
from matek_theorem_agent.stages.manuscript import (
    BibliographyAudit,
    BibliographyEntryAudit,
    BibliographyEntryStatus,
    BibliographyStatus,
    FrozenClaimFidelity,
    IntroductionCoverage,
    ManuscriptDraft,
    RelatedWorkClaimAudit,
)
from matek_theorem_agent.stages.research import (
    AuditDecision,
    AuditVerdict,
    CandidateProofPackage,
    FinalJudgeDecision,
    FinalJudgeVerdict,
    ResearchAssignment,
    ResearchCoordinatorDecision,
    ResearchWorkerReport,
    WorkerStatus,
)
from matek_theorem_agent.state import StateStore

E2E_CLAIM_CONTRACT = {
    "quantifiers": "for every natural number n",
    "conclusion": "P(n)",
}
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


def web_source_metadata() -> tuple[dict[str, Any], ...]:
    return (
        {
            "type": "web_search_call",
            "id": "ws_fixture",
            "status": "completed",
            "action": {
                "type": "search",
                "sources": [
                    {
                        "type": "url",
                        "url": VERIFIED_SOURCE_URL,
                        "title": "Fixture source",
                    },
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


def covered_compiled_prompt() -> str:
    return "\n\n".join(
        f"{section}\nThis fixture preserves the rigorous framework section and gives concrete "
        "problem-specific proof obligations for every research agent."
        for section in FRAMEWORK_SECTIONS
    )


def fixture_candidate_package() -> CandidateProofPackage:
    return CandidateProofPackage(
        exact_theorem="For every natural number n, P(n).",
        definitions=["P is the fixture predicate."],
        lemma_dependency_graph={"main": ["fixture_lemma"]},
        full_proof="First prove the fixture lemma, then apply it to arbitrary n.",
        imported_theorems=[],
        exceptional_cases=[],
        parameter_bookkeeping=["n is arbitrary"],
        unresolved_items=[],
        quantitative_or_algorithmic=False,
    )


class ResearchWorkflowModel:
    """Protocol fake that drives the real compiler and research stage services."""

    def __init__(self, *, accepted: bool) -> None:
        self.accepted = accepted
        self.requests: list[tuple[ModelRequest, type[BaseModel]]] = []

    async def generate_structured(
        self,
        request: ModelRequest,
        output_type: type[Any],
    ) -> ModelResult[Any]:
        self.requests.append((request, output_type))
        if output_type is CompiledProblem:
            output: BaseModel = CompiledProblem(
                title="Offline fixture theorem",
                normalized_statement="Prove P(n) for every natural number n.",
                claim_contract=E2E_CLAIM_CONTRACT,
                compiled_prompt=covered_compiled_prompt(),
                source_ledger=[],
                unresolved_ambiguities=[],
            )
        elif output_type is ResearchCoordinatorDecision:
            payload = json.loads(request.input_text)
            decision_id = int(payload["decision_id"])
            completed_ids = [
                report["assignment_id"] for report in payload["visible_worker_reports"]
            ]
            latest_verdict = payload.get("latest_final_judge_verdict")
            definitively_refuted = (
                not self.accepted
                and isinstance(latest_verdict, dict)
                and latest_verdict.get("verdict") == FinalJudgeDecision.REJECTED.value
            )
            output = ResearchCoordinatorDecision(
                decision_id=decision_id,
                after_event_sequence=int(payload["after_event_sequence"]),
                assignments=(
                    [
                        ResearchAssignment(
                            id=f"route-{index}",
                            approach_family=family,
                            task=f"Develop the {family} route.",
                            expected_output="A complete proof or an exact obstruction.",
                        )
                        for index, family in enumerate(
                            ("direct", "structural", "counterexample", "literature"),
                            start=1,
                        )
                    ]
                    if decision_id == 1
                    else []
                ),
                rationale=(
                    "Four materially different proof mechanisms."
                    if decision_id == 1
                    else "The independent judge definitively refuted the fixture candidate."
                    if definitively_refuted
                    else "Submit the completed route for an aggregate independent judgment."
                ),
                candidate_packaging_recommended=decision_id > 1 and not definitively_refuted,
                candidate_report_ids=(
                    completed_ids[:1] if decision_id > 1 and not definitively_refuted else []
                ),
                stop_recommended=definitively_refuted,
                stop_reason=(
                    "Independent final judgment refuted the only claimed fixture proof."
                    if definitively_refuted
                    else None
                ),
                stop_category="refuted" if definitively_refuted else "scientific",
            )
        elif output_type is ResearchWorkerReport:
            assignment = json.loads(request.input_text)["assignment"]
            output = ResearchWorkerReport(
                assignment_id=assignment["id"],
                status=WorkerStatus.CANDIDATE_COMPLETE,
                formal_results=[f"Lemma produced by {assignment['approach_family']} route."],
                proof_content="A visible, checkable proof argument.",
                exact_gap=None,
                sources=[],
                mechanism=assignment["task"],
            )
        elif output_type is CandidateProofPackage:
            output = fixture_candidate_package()
        elif output_type is AuditVerdict:
            output = AuditVerdict(
                verdict=AuditDecision.PASS,
                issues=[],
                unresolved_obligations=[],
                target_matches=True,
                audit_role="fixture",
                rationale="The fixture audit found no defect within its assigned scope.",
                checks_performed=["Checked the frozen candidate against the role-specific prompt."],
            )
        elif output_type is FinalJudgeVerdict:
            if self.accepted:
                output = FinalJudgeVerdict(
                    verdict=FinalJudgeDecision.ACCEPTED,
                    reasons=["Every mandatory audit passed."],
                    unresolved_obligations=[],
                    strongest_result="For every natural number n, P(n).",
                )
            else:
                output = FinalJudgeVerdict(
                    verdict=FinalJudgeDecision.REJECTED,
                    reasons=["The purported induction step is invalid."],
                    unresolved_obligations=["Supply a valid induction step."],
                    strongest_result="P(0) only.",
                )
        else:  # pragma: no cover - catches accidental expansion into later paid stages
            raise AssertionError(f"unexpected model request: {output_type.__name__}")

        assert isinstance(output, output_type)
        call_number = len(self.requests)
        return ModelResult(
            parsed=output,
            response_id=f"offline-response-{call_number}",
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            estimated_cost_usd=0.01,
        )


class NeverReturningModel:
    def __init__(self) -> None:
        self.cancelled = False

    async def generate_structured(
        self,
        request: ModelRequest,
        output_type: type[Any],
    ) -> ModelResult[Any]:
        del request, output_type
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class ForbiddenBackend:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, request: CommandRequest) -> CommandResult:
        self.calls += 1
        raise AssertionError(f"unexpected command execution: {request.argv}")


class PdfBackend:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, request: CommandRequest) -> CommandResult:
        self.calls += 1
        (request.cwd / "paper.pdf").write_bytes(b"%PDF-offline-fixture")
        return CommandResult(
            argv=request.argv,
            cwd=request.cwd,
            exit_code=0,
            stdout="Latexmk: success",
            stderr="",
            duration_seconds=0.01,
        )


def manuscript_draft() -> ManuscriptDraft:
    exact_theorem = fixture_candidate_package().exact_theorem
    related_excerpt = (
        "Smith's earlier article proves a comparison lemma for a restricted family of fixture "
        "objects and supplies the relevant historical context"
    )
    difference_excerpt = (
        "The accepted theorem differs by treating every natural number without the earlier "
        "restriction on the fixture predicate"
    )
    advance_excerpt = (
        "Its precise advance is a complete uniform proof that includes the boundary instance and "
        "preserves the original quantifier order"
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
            "\\section{Related and Existing Work}\n"
            "Smith proves a nearby lemma for restricted fixture objects and documents the "
            "methodological context \\cite{smith2020}. That source does not establish the uniform "
            "claim made here, and the manuscript records both the restriction and the exact "
            "difference before using the comparison lemma.\n"
            "\\section{Proof}\nThe complete fixture proof follows the accepted package.\n"
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
        proof_dependency_graph={"main": ["fixture"]},
        introduction_coverage=IntroductionCoverage(
            related_work_excerpt=related_excerpt,
            difference_from_prior_work_excerpt=difference_excerpt,
            advance_over_prior_work_excerpt=advance_excerpt,
            citation_keys=["smith2020"],
        ),
        frozen_claim_fidelity=FrozenClaimFidelity(
            candidate_sha256=sha256_json(fixture_candidate_package()),
            claim_contract_sha256=sha256_text(
                json.dumps(E2E_CLAIM_CONTRACT, sort_keys=True, ensure_ascii=False)
            ),
            exact_theorem=exact_theorem,
            manuscript_main_claim=exact_theorem,
            exact_match=True,
        ),
    )


def bibliography_audit(*, verified: bool) -> BibliographyAudit:
    return BibliographyAudit(
        status=(
            BibliographyStatus.VERIFIED if verified else BibliographyStatus.CORRECTIONS_REQUIRED
        ),
        entries=[
            BibliographyEntryAudit(
                citation_key="smith2020",
                status=(
                    BibliographyEntryStatus.VERIFIED
                    if verified
                    else BibliographyEntryStatus.AMBIGUOUS
                ),
                exists=verified,
                exact_title_verified=verified,
                authors_verified=verified,
                year_verified=verified,
                venue_or_status_verified=verified,
                stable_identifier_checked=verified,
                characterization_supported=verified,
                theorem_hypotheses_supported=verified,
                authoritative_evidence=(["https://doi.org/10.5555/12345678"] if verified else []),
            ),
            BibliographyEntryAudit(
                citation_key="matekSoftwareFixture",
                status=(
                    BibliographyEntryStatus.VERIFIED
                    if verified
                    else BibliographyEntryStatus.AMBIGUOUS
                ),
                exists=verified,
                exact_title_verified=verified,
                authors_verified=verified,
                year_verified=verified,
                venue_or_status_verified=verified,
                stable_identifier_checked=verified,
                characterization_supported=verified,
                theorem_hypotheses_supported=verified,
                authoritative_evidence=([MATEK_FIXTURE_REPOSITORY_URL] if verified else []),
            ),
            BibliographyEntryAudit(
                citation_key="matekWhitepaperFixture",
                status=(
                    BibliographyEntryStatus.VERIFIED
                    if verified
                    else BibliographyEntryStatus.AMBIGUOUS
                ),
                exists=verified,
                exact_title_verified=verified,
                authors_verified=verified,
                year_verified=verified,
                venue_or_status_verified=verified,
                stable_identifier_checked=verified,
                characterization_supported=verified,
                theorem_hypotheses_supported=verified,
                authoritative_evidence=([MATEK_FIXTURE_WHITEPAPER_URL] if verified else []),
            ),
        ],
        claim_checks=[
            RelatedWorkClaimAudit(
                claim="Prior work proves a nearby lemma.",
                citation_keys=["smith2020"],
                supported=verified,
                evidence=["https://doi.org/10.5555/12345678"] if verified else [],
            )
        ],
        blocking_issues=[] if verified else ["Disambiguate the source."],
        correction_plan=[] if verified else ["Use the DOI record."],
    )


class BibliographyResumeModel(ResearchWorkflowModel):
    def __init__(self) -> None:
        super().__init__(accepted=True)
        self.bibliography_calls = 0

    async def generate_structured(
        self,
        request: ModelRequest,
        output_type: type[Any],
    ) -> ModelResult[Any]:
        if output_type is ManuscriptDraft:
            self.requests.append((request, output_type))
            parsed: BaseModel = manuscript_draft()
        elif output_type is BibliographyAudit:
            self.requests.append((request, output_type))
            self.bibliography_calls += 1
            parsed = bibliography_audit(verified=self.bibliography_calls > 1)
        else:
            return await super().generate_structured(request, output_type)
        return ModelResult(
            parsed=parsed,
            response_id=f"offline-response-{len(self.requests)}",
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            estimated_cost_usd=0.01,
            tool_metadata=(
                web_source_metadata()
                if isinstance(parsed, BibliographyAudit)
                and parsed.status == BibliographyStatus.VERIFIED
                else ()
            ),
        )


class ForbiddenCodex:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, request: CodexRequest) -> CodexResult:
        self.calls += 1
        raise AssertionError(f"unexpected Codex execution in {request.cwd}")


class AlwaysVerifiedIdentifierVerifier:
    """Deterministic source verifier for offline workflow fixtures."""

    async def verify(
        self,
        identifiers: Collection[str],
        *,
        expected_title: str | None = None,
    ) -> SourceVerificationReport:
        del expected_title
        return SourceVerificationReport(
            records=[
                SourceVerificationRecord(
                    identifier=identifier,
                    status=SourceVerificationStatus.VERIFIED,
                    detail="verified by offline fixture",
                )
                for identifier in identifiers
            ]
        )


class FullWorkflowModel(ResearchWorkflowModel):
    """Drive a completion-triggered follow-up through every real stage."""

    def __init__(self) -> None:
        super().__init__(accepted=True)

    async def generate_structured(
        self,
        request: ModelRequest,
        output_type: type[Any],
    ) -> ModelResult[Any]:
        if output_type is ResearchCoordinatorDecision:
            self.requests.append((request, output_type))
            payload = json.loads(request.input_text)
            decision_id = int(payload["decision_id"])
            families = (
                ("direct", "structural", "counterexample", "literature")
                if decision_id == 1
                else ("synthesis",)
            )
            parsed: BaseModel = ResearchCoordinatorDecision(
                decision_id=decision_id,
                after_event_sequence=int(payload["after_event_sequence"]),
                assignments=[
                    ResearchAssignment(
                        id=f"decision-{decision_id}-route-{index}",
                        approach_family=family,
                        task=f"Develop the {family} route after decision {decision_id}.",
                        expected_output="A complete proof or an exact obstruction.",
                    )
                    for index, family in enumerate(families, start=1)
                ],
                rationale="The follow-up synthesizes newly completed independent evidence.",
            )
        elif output_type is ResearchWorkerReport:
            assignment = json.loads(request.input_text)["assignment"]
            self.requests.append((request, output_type))
            parsed = ResearchWorkerReport(
                assignment_id=assignment["id"],
                status=(
                    WorkerStatus.CANDIDATE_COMPLETE
                    if assignment["approach_family"] == "synthesis"
                    else WorkerStatus.PROGRESS
                ),
                formal_results=[f"Lemma produced by {assignment['approach_family']} route."],
                proof_content="A visible, checkable proof argument.",
                exact_gap=(
                    None
                    if assignment["approach_family"] == "synthesis"
                    else "Synthesize this lemma with the other independent routes."
                ),
                sources=[],
                mechanism=assignment["task"],
            )
        elif output_type is ManuscriptDraft:
            self.requests.append((request, output_type))
            parsed = manuscript_draft()
        elif output_type is BibliographyAudit:
            self.requests.append((request, output_type))
            parsed = bibliography_audit(verified=True)
        elif output_type is LeanFeasibilityAssessment:
            self.requests.append((request, output_type))
            parsed = LeanFeasibilityAssessment(
                classification=LeanFeasibilityClass.MAIN_THEOREM,
                explanation="The accepted theorem is directly expressible in Lean.",
                expected_mathlib_dependencies=[],
                difficult_components=[],
                computational_certificates=[],
                paper_proof_mismatches=[],
            )
        elif output_type is LeanStatementDraft:
            self.requests.append((request, output_type))
            parsed = LeanStatementDraft(
                challenge_lean="theorem matek_main : True := by\n  sorry\n",
                statement_explanation="The frozen fixture claim is represented by True.",
                claim_map={"conclusion": "True"},
                theorem_name="matek_main",
            )
        elif output_type is ClaimAlignment:
            self.requests.append((request, output_type))
            parsed = ClaimAlignment(
                status=AlignmentStatus.ALIGNED,
                mathematical_back_translation="True.",
                checks=[
                    AlignmentCheck(
                        field=field,
                        passed=True,
                        explanation=(
                            f"The Lean statement preserves the frozen {field.replace('_', ' ')}."
                        ),
                    )
                    for field in MANDATORY_ALIGNMENT_FIELDS
                ],
                required_edits=[],
            )
        else:
            return await super().generate_structured(request, output_type)

        assert isinstance(parsed, output_type)
        call_number = len(self.requests)
        return ModelResult(
            parsed=parsed,
            response_id=f"offline-response-{call_number}",
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            estimated_cost_usd=0.01,
            tool_metadata=(web_source_metadata() if output_type is BibliographyAudit else ()),
        )


class RoutedFullWorkflowModel(FullWorkflowModel):
    """Full protocol fixture with an explicit, inspectable billing boundary."""

    def __init__(self, provider: Literal["codex", "api"]) -> None:
        super().__init__()
        self.provider = provider

    def backend_manifest(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "backend_version": f"{self.provider}-offline-e2e-fixture",
            "authentication_class": ("chatgpt" if self.provider == "codex" else "platform_api_key"),
            "no_api_fallback": True,
        }


class FullWorkflowBackend:
    def __init__(self) -> None:
        self.requests: list[CommandRequest] = []

    async def run(self, request: CommandRequest) -> CommandResult:
        self.requests.append(request)
        stdout = ""
        if request.cwd.name == "manuscript":
            (request.cwd / "paper.pdf").write_bytes(b"%PDF-offline-e2e-fixture")
            stdout = "Latexmk: success"
        elif request.argv[-1].endswith("_MatekAxiomCheck.lean"):
            stdout = "'matek_main' depends on no axioms"
        return CommandResult(
            argv=request.argv,
            cwd=request.cwd,
            exit_code=0,
            stdout=stdout,
            stderr="",
            duration_seconds=0.01,
        )


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
        return CodexResult(
            exit_code=0,
            stdout='{"type":"turn.completed"}\n',
            stderr="",
            command=("codex", "exec"),
        )


class InterruptOnceModel(ResearchWorkflowModel):
    def __init__(self) -> None:
        super().__init__(accepted=True)
        self.interrupted = False

    async def generate_structured(
        self,
        request: ModelRequest,
        output_type: type[Any],
    ) -> ModelResult[Any]:
        if not self.interrupted:
            self.interrupted = True
            raise asyncio.CancelledError
        return await super().generate_structured(request, output_type)


class InterruptAtCandidateModel(ResearchWorkflowModel):
    """Cancel after the initial plan and concurrent worker portfolio complete."""

    def __init__(self) -> None:
        super().__init__(accepted=True)
        self.candidate_attempts = 0

    async def generate_structured(
        self,
        request: ModelRequest,
        output_type: type[Any],
    ) -> ModelResult[Any]:
        if output_type is CandidateProofPackage:
            self.candidate_attempts += 1
            if self.candidate_attempts == 1:
                raise asyncio.CancelledError
        return await super().generate_structured(request, output_type)


def workflow_runner(
    project_root: Path,
    *,
    accepted: bool,
) -> tuple[WorkflowRunner, ResearchWorkflowModel, ForbiddenBackend, ForbiddenCodex]:
    model = ResearchWorkflowModel(accepted=accepted)
    backend = ForbiddenBackend()
    codex = ForbiddenCodex()
    config = AppConfig(
        project_root=project_root,
        research=ResearchSettings(
            minimum_initial_agents=4,
            maximum_concurrent_agents=2,
            maximum_rounds=1,
        ),
    )
    runner = WorkflowRunner(
        config,
        WorkflowDependencies(
            model_client=model,
            execution_backend=backend,
            codex_client=codex,
            source_verifier=AlwaysVerifiedIdentifierVerifier(),
        ),
    )
    return runner, model, backend, codex


def make_problem(project_root: Path) -> Path:
    problem = project_root / "problem.md"
    problem.write_text("# Problem\n\nProve P(n) for every natural number n.\n", encoding="utf-8")
    return problem


class ClarificationOnlyModel:
    def __init__(self) -> None:
        self.requests: list[tuple[ModelRequest, type[BaseModel]]] = []

    async def generate_structured(
        self,
        request: ModelRequest,
        output_type: type[Any],
    ) -> ModelResult[Any]:
        self.requests.append((request, output_type))
        assert output_type is CompiledProblem
        parsed = CompiledProblem(
            status=PromptCompilationStatus.NEEDS_CLARIFICATION,
            clarification_reason=(
                "The description names an extension problem but not its domain or target."
            ),
            clarification_questions=[
                "What mathematical objects are being extended?",
                "What exact conclusion should be proved?",
            ],
            candidate_interpretations=[
                "An operator-extension theorem.",
                "A combinatorial extension problem.",
            ],
            unresolved_ambiguities=["The domain and success criterion are both ambiguous."],
        )
        return ModelResult(
            parsed=parsed,
            response_id="clarification-response",
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            estimated_cost_usd=None,
        )


class PlaceholderRecoveryWorkflowModel(ResearchWorkflowModel):
    """Drive prompt recovery, then reuse the ordinary rejected-research fixture."""

    def __init__(self, *, section: str, repair_on_attempt: int | None) -> None:
        super().__init__(accepted=False)
        self.section = section
        self.repair_on_attempt = repair_on_attempt
        self.compiler_calls = 0
        self.repair_calls = 0

    async def generate_structured(
        self,
        request: ModelRequest,
        output_type: type[Any],
    ) -> ModelResult[Any]:
        if output_type is CompiledProblem:
            self.requests.append((request, output_type))
            self.compiler_calls += 1
            prompt = covered_compiled_prompt().replace(
                f"{self.section}\n",
                f"{self.section}\nResolve [INSERT TARGET HERE] in this sentence.\n",
            )
            return ModelResult(
                parsed=CompiledProblem(
                    title="Offline placeholder fixture",
                    normalized_statement="Prove P(n) for every natural number n.",
                    claim_contract=E2E_CLAIM_CONTRACT,
                    compiled_prompt=prompt,
                    source_ledger=[],
                    unresolved_ambiguities=[],
                ),
                response_id=f"placeholder-compiler-{self.compiler_calls}",
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                estimated_cost_usd=0.01,
            )
        if output_type is PromptPlaceholderRepair:
            self.requests.append((request, output_type))
            self.repair_calls += 1
            if self.repair_on_attempt != self.repair_calls:
                raise RuntimeError("fixture repair unavailable")
            return ModelResult(
                parsed=PromptPlaceholderRepair(
                    replacement_sentence=(
                        "Resolve P(n) for every natural number n in this sentence."
                    )
                ),
                response_id=f"placeholder-repair-{self.repair_calls}",
                input_tokens=3,
                output_tokens=2,
                total_tokens=5,
                estimated_cost_usd=0.001,
            )
        return await super().generate_structured(request, output_type)


def test_ambiguous_problem_stops_before_research_and_asks_for_clarification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    problem = project / "problem.md"
    problem.write_text("Solve the extension problem.\n", encoding="utf-8")
    model = ClarificationOnlyModel()
    backend = ForbiddenBackend()
    codex = ForbiddenCodex()
    runner = WorkflowRunner(
        AppConfig(project_root=project),
        WorkflowDependencies(
            model_client=model,  # type: ignore[arg-type]
            execution_backend=backend,
            codex_client=codex,
            source_verifier=AlwaysVerifiedIdentifierVerifier(),
        ),
    )
    monkeypatch.chdir(project)
    monkeypatch.setattr(cli_module, "_live_runner", lambda config: runner)

    invocation = CliRunner().invoke(app, ["run", str(problem)])

    assert invocation.exit_code == 0, invocation.output
    assert "Resolved MATEK run configuration" in invocation.output
    assert "research coordinator" in invocation.output
    assert "max effort" in invocation.output
    assert "research agents" in invocation.output
    assert "xhigh effort" in invocation.output
    assert "web access" in invocation.output
    assert "up to 32 effective" in invocation.output
    assert "no automatic API fallback" in invocation.output
    assert "MATEK run summary" in invocation.output
    assert "Problem solved?" in invocation.output
    assert "UNDETERMINED" in invocation.output
    assert "Where it stopped" in invocation.output
    assert "Prompt compilation" in invocation.output
    assert "Full report" in invocation.output
    assert "stopped before research" in invocation.output
    assert "What mathematical objects are being extended?" in invocation.output
    [run_root] = (project / ".matek" / "runs").iterdir()
    state = StateStore(run_root).load()
    assert state.scientific_status is ScientificStatus.NEEDS_PROBLEM_CLARIFICATION
    assert state.stages[StageName.PROMPT_COMPILATION].status is StageStatus.SUCCEEDED
    assert state.stages[StageName.RESEARCH].status is StageStatus.SKIPPED
    assert state.stages[StageName.MANUSCRIPT].status is StageStatus.SKIPPED
    assert state.stages[StageName.LEAN_VERIFICATION].status is StageStatus.SKIPPED
    assert state.stages[StageName.REPORT].status is StageStatus.SUCCEEDED
    assert len(model.requests) == 1
    assert backend.calls == 0
    assert codex.calls == 0
    clarification = (run_root / "prompts" / "clarification_request.md").read_text(encoding="utf-8")
    report = (run_root / "report" / "REPORT.md").read_text(encoding="utf-8")
    assert "start a new MATEK run" in clarification
    assert "Problem clarification required" in report
    assert "revise the problem file" in report.lower()


@pytest.mark.asyncio
async def test_run_wide_deadline_interrupts_the_active_stage_and_writes_report(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    model = NeverReturningModel()
    config = AppConfig(project_root=project)
    config.api.limits = Limits(
        maximum_cost_usd=1.0,
        maximum_wall_clock_hours=0.00003,
    )
    runner = WorkflowRunner(
        config,
        WorkflowDependencies(
            model_client=model,  # type: ignore[arg-type]
            execution_backend=ForbiddenBackend(),
            codex_client=ForbiddenCodex(),
            source_verifier=AlwaysVerifiedIdentifierVerifier(),
        ),
    )

    with pytest.raises(BudgetExceeded, match="wall_clock"):
        await runner.run_new(
            make_problem(project),
            project,
            options=WorkflowOptions(research_only=True),
            environment_snapshot={"fixture": "offline"},
        )

    [run_root] = (project / ".matek" / "runs").iterdir()
    state = StateStore(run_root).load()
    assert model.cancelled
    assert state.stages[StageName.PROMPT_COMPILATION].status is StageStatus.INTERRUPTED
    assert state.stages[StageName.REPORT].status is StageStatus.SUCCEEDED
    assert "wall-clock limit" in (state.stages[StageName.PROMPT_COMPILATION].error or "")


def placeholder_recovery_runner(
    project: Path,
    model: PlaceholderRecoveryWorkflowModel,
) -> WorkflowRunner:
    return WorkflowRunner(
        AppConfig(
            project_root=project,
            research=ResearchSettings(
                minimum_initial_agents=4,
                maximum_concurrent_agents=2,
                maximum_rounds=1,
            ),
        ),
        WorkflowDependencies(
            model_client=model,  # type: ignore[arg-type]
            execution_backend=ForbiddenBackend(),
            codex_client=ForbiddenCodex(),
            source_verifier=AlwaysVerifiedIdentifierVerifier(),
        ),
    )


@pytest.mark.asyncio
async def test_prompt_placeholder_is_automatically_repaired_before_research(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    model = PlaceholderRecoveryWorkflowModel(
        section="Current task statement",
        repair_on_attempt=1,
    )
    runner = placeholder_recovery_runner(project, model)

    result = await runner.run_new(make_problem(project), project)

    assert result.state.stages[StageName.PROMPT_COMPILATION].status is StageStatus.SUCCEEDED
    assert result.state.scientific_status is ScientificStatus.RESEARCH_REJECTED
    assert model.compiler_calls == 1
    assert model.repair_calls == 1
    prompt = (result.state.run_root / "prompts" / "compiled_research_prompt.md").read_text(
        encoding="utf-8"
    )
    assert "[INSERT TARGET HERE]" not in prompt
    validation = json.loads(
        (result.state.run_root / "prompts" / "prompt_validation.json").read_text(encoding="utf-8")
    )
    assert validation["diagnostics"][0]["disposition"] == "repaired"


@pytest.mark.asyncio
async def test_optional_placeholder_downgrade_reaches_truthful_final_report(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    model = PlaceholderRecoveryWorkflowModel(
        section="Known starting point and exact bottleneck",
        repair_on_attempt=None,
    )
    runner = placeholder_recovery_runner(project, model)

    result = await runner.run_new(make_problem(project), project)

    assert result.state.scientific_status is ScientificStatus.RESEARCH_REJECTED
    assert result.report.report.prompt_validation_warnings
    report = result.report.report_markdown.read_text(encoding="utf-8")
    assert "Prompt validation warnings" in report
    assert "[INSERT TARGET HERE]" in report
    prompt = (result.state.run_root / "prompts" / "compiled_research_prompt.md").read_text(
        encoding="utf-8"
    )
    assert "[INSERT TARGET HERE]" not in prompt


@pytest.mark.asyncio
async def test_force_prompt_stage_reuses_compiler_and_retries_only_bounded_repair(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    model = PlaceholderRecoveryWorkflowModel(
        section="Exact success criterion",
        repair_on_attempt=2,
    )
    runner = placeholder_recovery_runner(project, model)

    paused = await runner.run_new(make_problem(project), project)
    assert paused.report.report.workflow_status == "PAUSED_RETRIABLE"
    assert any(
        "[INSERT TARGET HERE]" in issue.get("message", "")
        for issue in paused.report.report.execution_issues
    )

    [run_root] = (project / ".matek" / "runs").iterdir()
    failed = StateStore(run_root).load()
    assert failed.stages[StageName.PROMPT_COMPILATION].status is StageStatus.FAILED
    assert (run_root / "prompts" / "compiled_problem.json").is_file()
    assert (run_root / "prompts" / "prompt_validation.json").is_file()
    assert failed.metadata["prompt_compilation_recovery"]["artifacts_preserved"]

    resumed = await runner.resume(
        project,
        run_id=failed.run_id,
        force_stage=StageName.PROMPT_COMPILATION,
    )

    assert resumed.state.stages[StageName.PROMPT_COMPILATION].status is StageStatus.SUCCEEDED
    assert resumed.state.metadata["prompt_validation_generation"] == 1
    assert model.compiler_calls == 1
    assert model.repair_calls == 2


@pytest.mark.parametrize("provider", ["codex", "api"])
@pytest.mark.asyncio
async def test_complete_continuous_pipeline_is_lean_verified_and_resume_is_noop(
    tmp_path: Path,
    provider: Literal["codex", "api"],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "lean-toolchain").write_text("leanprover/lean4:stable\n", encoding="utf-8")
    (project / "lakefile.toml").write_text("name = 'fixture'\n", encoding="utf-8")
    model = RoutedFullWorkflowModel(provider)
    backend = FullWorkflowBackend()
    codex = EditingCodex()
    updates: list[tuple[Ascension, str]] = []
    runner = WorkflowRunner(
        AppConfig(
            project_root=project,
            backend=BackendSettings(provider=provider),
            research=ResearchSettings(
                minimum_initial_agents=4,
                maximum_concurrent_agents=2,
                maximum_rounds=2,
            ),
            lean=LeanSettings(maximum_codex_iterations=1),
        ),
        WorkflowDependencies(
            model_client=model,
            execution_backend=backend,
            codex_client=codex,
            source_verifier=AlwaysVerifiedIdentifierVerifier(),
            progress=lambda ascension, message: updates.append((ascension, message)),
        ),
    )

    result = await runner.run_new(
        make_problem(project),
        project,
        environment_snapshot={"fixture": "offline"},
    )

    research = json.loads(
        (result.state.run_root / "research" / "result.json").read_text(encoding="utf-8")
    )
    bibliography = BibliographyAudit.model_validate_json(
        (result.state.run_root / "manuscript" / "bibliography_audit.json").read_text(
            encoding="utf-8"
        )
    )
    assert research["rounds"] == []
    assert len(research["coordinator_decisions"]) >= 2
    assert bibliography.status is BibliographyStatus.VERIFIED
    assert all(entry.status is BibliographyEntryStatus.VERIFIED for entry in bibliography.entries)
    assert result.state.scientific_status is ScientificStatus.RESEARCH_ACCEPTED_FOR_MANUSCRIPT
    assert result.state.metadata["lean_status"] == ScientificStatus.LEAN_VERIFIED.value
    assert result.state.metadata["backend"]["provider"] == provider
    assert result.state.metadata["backend"]["automatic_fallback"] is False
    assert result.report.report.lean_status == ScientificStatus.LEAN_VERIFIED.value
    assert result.state.stages[StageName.LEAN_VERIFICATION].status is StageStatus.SUCCEEDED
    assert result.report.report.artifacts
    with cli_module.console.capture() as capture:
        cli_module._print_result(result)
    terminal_summary = capture.get()
    assert "MATEK run summary" in terminal_summary
    assert "Problem solved?" in terminal_summary
    assert "YES — the exact research result passed" in terminal_summary
    assert "Research performed" in terminal_summary
    assert "Strongest result" in terminal_summary
    assert "Full report" in terminal_summary
    assert str(result.report.report_markdown) in terminal_summary
    report_markdown = result.report.report_markdown.read_text(encoding="utf-8")
    assert all(f"[`{relative}`]" in report_markdown for relative in result.report.report.artifacts)
    assert (result.state.run_root / "manuscript" / "paper.pdf").is_file()
    assert (result.state.run_root / "lean" / "challenge.lean").is_file()
    assert len(codex.requests) == 1
    assert len(backend.requests) == 3
    assert [ascension for ascension, _ in updates] == [
        Ascension.FETCH_PROBLEM,
        Ascension.FORMULATE_PROMPT,
        Ascension.START_RESEARCH_COORDINATOR,
        Ascension.MANAGE_RESEARCH_POOL,
        Ascension.AUDIT_RESEARCH,
        Ascension.WRITE_MANUSCRIPT,
        Ascension.FORMALIZE_LEAN,
        Ascension.PREPARE_REPORT,
    ]
    usage_records = [
        json.loads(line)
        for line in (result.state.run_root / "logs" / "usage.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert usage_records
    assert {record["usage"]["provider"] for record in usage_records} == {provider}
    model_call_records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (result.state.run_root / "logs" / "model_calls").glob("*.json")
    ]
    assert model_call_records
    assert {record["cache_namespace"] for record in model_call_records} == {
        f"{provider}-generation-0"
    }
    events = [
        json.loads(line)
        for line in (result.state.run_root / "logs" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    boundary_targets = {
        event["data"]["next_step"]
        for event in events
        if event["event"] == "stage.boundary.validated"
    }
    assert {
        "prompt_compilation",
        "research",
        "manuscript",
        "lean_consent",
        "lean_feasibility",
        "report",
    } <= boundary_targets

    call_count = len(model.requests)
    paid_call_ids = tuple(result.state.paid_call_ids)
    backend_count = len(backend.requests)
    codex_count = len(codex.requests)
    resumed = await runner.resume(project, run_id=result.state.run_id)

    assert len(model.requests) == call_count
    assert tuple(resumed.state.paid_call_ids) == paid_call_ids
    assert len(backend.requests) == backend_count
    assert len(codex.requests) == codex_count
    assert resumed.report.hashes == result.report.hashes


def full_runner_with_consent(
    project: Path,
    consent: Any,
) -> tuple[WorkflowRunner, RoutedFullWorkflowModel, FullWorkflowBackend, EditingCodex]:
    (project / "lean-toolchain").write_text("leanprover/lean4:stable\n", encoding="utf-8")
    (project / "lakefile.toml").write_text("name = 'fixture'\n", encoding="utf-8")
    model = RoutedFullWorkflowModel("codex")
    backend = FullWorkflowBackend()
    codex = EditingCodex()
    runner = WorkflowRunner(
        AppConfig(
            project_root=project,
            research=ResearchSettings(
                minimum_initial_agents=4,
                maximum_concurrent_agents=2,
                maximum_rounds=2,
            ),
            lean=LeanSettings(maximum_codex_iterations=1),
        ),
        WorkflowDependencies(
            model_client=model,
            execution_backend=backend,
            codex_client=codex,
            source_verifier=AlwaysVerifiedIdentifierVerifier(),
            lean_consent=consent,
        ),
    )
    return runner, model, backend, codex


@pytest.mark.asyncio
async def test_user_can_decline_lean_after_safe_manuscript_draft(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    requests: list[LeanConsentRequest] = []

    async def decline(request: LeanConsentRequest) -> LeanConsentOutcome:
        requests.append(request)
        return (
            LeanConsentOutcome.USER_DECLINED
            if len(requests) == 1
            else LeanConsentOutcome.USER_APPROVED
        )

    runner, _, _, codex = full_runner_with_consent(project, decline)

    result = await runner.run_new(make_problem(project), project)

    assert len(requests) == 1
    assert requests[0].timeout_seconds == LEAN_CONSENT_TIMEOUT_SECONDS == 300
    assert requests[0].manuscript_path.name == "paper.tex"
    assert result.state.scientific_status is ScientificStatus.RESEARCH_ACCEPTED_FOR_MANUSCRIPT
    assert result.state.metadata["lean_status"] == ScientificStatus.LEAN_NOT_REQUESTED.value
    assert result.state.metadata["lean_consent"]["outcome"] == "user_declined"
    assert result.state.metadata["lean_consent"]["manuscript"] == "manuscript/paper.tex"
    assert result.report.report.lean_consent["proceed"] is False
    assert result.state.stages[StageName.MANUSCRIPT].status is StageStatus.SUCCEEDED
    assert result.state.stages[StageName.BIBLIOGRAPHY].status is StageStatus.SUCCEEDED
    assert result.state.stages[StageName.LEAN_FEASIBILITY].status is StageStatus.SKIPPED
    assert len(codex.requests) == 0
    assert (result.state.run_root / "lean" / "consent.json").is_file()
    assert not (result.state.run_root / "lean" / "challenge.lean").exists()

    resumed = await runner.resume(
        project,
        run_id=result.state.run_id,
        force_stage=StageName.LEAN_FEASIBILITY,
    )

    assert len(requests) == 2
    assert resumed.state.scientific_status is ScientificStatus.RESEARCH_ACCEPTED_FOR_MANUSCRIPT
    assert resumed.state.metadata["lean_status"] == ScientificStatus.LEAN_VERIFIED.value
    assert resumed.state.metadata["lean_consent"]["outcome"] == "user_approved"
    assert list((result.state.run_root / "lean" / "consent-history").glob("*.json"))


@pytest.mark.asyncio
async def test_lean_consent_timeout_defaults_to_full_verification(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    async def no_answer(_: LeanConsentRequest) -> LeanConsentOutcome:
        raise TimeoutError

    runner, _, _, codex = full_runner_with_consent(project, no_answer)

    result = await runner.run_new(make_problem(project), project)

    assert result.state.metadata["lean_consent"]["outcome"] == "timed_out_auto_proceed"
    assert result.state.metadata["lean_consent"]["timeout_seconds"] == 300
    assert result.state.scientific_status is ScientificStatus.RESEARCH_ACCEPTED_FOR_MANUSCRIPT
    assert result.state.metadata["lean_status"] == ScientificStatus.LEAN_VERIFIED.value
    assert result.state.stages[StageName.LEAN_VERIFICATION].status is StageStatus.SUCCEEDED
    assert len(codex.requests) == 1


@pytest.mark.asyncio
async def test_saved_lean_consent_is_reused_after_boundary_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    consent_calls = 0

    async def approve(_: LeanConsentRequest) -> LeanConsentOutcome:
        nonlocal consent_calls
        consent_calls += 1
        return LeanConsentOutcome.USER_APPROVED

    runner, _, _, _ = full_runner_with_consent(project, approve)
    original_lean_stage = runner._lean_stage

    async def fail_at_boundary(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError("fixture failure after durable consent")

    monkeypatch.setattr(runner, "_lean_stage", fail_at_boundary)
    paused = await runner.run_new(make_problem(project), project)
    assert paused.report.report.workflow_status == "PAUSED_RETRIABLE"
    assert any(
        "after durable consent" in issue.get("message", "")
        for issue in paused.report.report.execution_issues
    )

    [run_root] = (project / ".matek" / "runs").iterdir()
    interrupted = StateStore(run_root).load()
    assert interrupted.metadata["lean_consent"]["outcome"] == "user_approved"
    assert consent_calls == 1

    monkeypatch.setattr(runner, "_lean_stage", original_lean_stage)
    resumed = await runner.resume(project, run_id=interrupted.run_id)

    assert consent_calls == 1
    assert resumed.state.scientific_status is ScientificStatus.RESEARCH_ACCEPTED_FOR_MANUSCRIPT
    assert resumed.state.metadata["lean_status"] == ScientificStatus.LEAN_VERIFIED.value


@pytest.mark.asyncio
async def test_integrity_failure_still_hard_stops_and_reports_that_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    runner, _, _, _ = workflow_runner(project, accepted=False)

    async def unsafe_prompt_stage(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError("unsafe path traversal in immutable prompt artifact")

    monkeypatch.setattr(runner, "_prompt_stage", unsafe_prompt_stage)
    with pytest.raises(RuntimeError, match="unsafe path traversal"):
        await runner.run_new(make_problem(project), project)

    [run_root] = (project / ".matek" / "runs").iterdir()
    report = json.loads((run_root / "report" / "report.json").read_text(encoding="utf-8"))
    assert report["workflow_status"] == "HARD_STOPPED"


@pytest.mark.asyncio
async def test_cancellation_checkpoints_interrupted_stage_and_resume_completes(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    model = InterruptOnceModel()
    backend = ForbiddenBackend()
    codex = ForbiddenCodex()
    runner = WorkflowRunner(
        AppConfig(
            project_root=project,
            research=ResearchSettings(
                minimum_initial_agents=4,
                maximum_concurrent_agents=2,
                maximum_rounds=1,
            ),
        ),
        WorkflowDependencies(
            model_client=model,
            execution_backend=backend,
            codex_client=codex,
            source_verifier=AlwaysVerifiedIdentifierVerifier(),
        ),
    )

    with pytest.raises(asyncio.CancelledError):
        await runner.run_new(
            make_problem(project),
            project,
            options=WorkflowOptions(research_only=True),
            environment_snapshot={"fixture": "offline"},
        )

    [run_root] = (project / ".matek" / "runs").iterdir()
    interrupted = StateStore(run_root).load()
    assert interrupted.stages[StageName.PROMPT_COMPILATION].status is StageStatus.INTERRUPTED
    assert interrupted.stages[StageName.REPORT].status is StageStatus.SUCCEEDED

    resumed = await runner.resume(project, run_id=interrupted.run_id)

    assert resumed.state.stages[StageName.PROMPT_COMPILATION].attempts == 2
    assert resumed.state.stages[StageName.PROMPT_COMPILATION].status is StageStatus.SUCCEEDED
    assert resumed.state.stages[StageName.REPORT].status is StageStatus.SUCCEEDED
    assert resumed.report.report.scientific_status == (
        ScientificStatus.RESEARCH_ACCEPTED_FOR_MANUSCRIPT.value
    )
    assert backend.calls == 0
    assert codex.calls == 0


@pytest.mark.asyncio
async def test_resume_after_worker_events_reuses_paid_calls_and_persisted_artifacts(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    model = InterruptAtCandidateModel()
    backend = ForbiddenBackend()
    codex = ForbiddenCodex()
    runner = WorkflowRunner(
        AppConfig(
            project_root=project,
            research=ResearchSettings(
                minimum_initial_agents=4,
                maximum_concurrent_agents=2,
                maximum_rounds=1,
            ),
        ),
        WorkflowDependencies(
            model_client=model,
            execution_backend=backend,
            codex_client=codex,
            source_verifier=AlwaysVerifiedIdentifierVerifier(),
        ),
    )

    with pytest.raises(asyncio.CancelledError):
        await runner.run_new(
            make_problem(project),
            project,
            options=WorkflowOptions(research_only=True),
            environment_snapshot={"fixture": "offline"},
        )

    [run_root] = (project / ".matek" / "runs").iterdir()
    interrupted = StateStore(run_root).load()
    decision_path = run_root / "research" / "coordinator" / "decisions" / "00000001.json"
    worker_paths = tuple(sorted((run_root / "research" / "workers").glob("*.json")))
    preserved_artifacts = {
        path.relative_to(run_root).as_posix(): path.read_bytes()
        for path in (decision_path, *worker_paths)
    }
    record_root = run_root / "logs" / "model_calls"
    initial_records = {path.name: path.read_bytes() for path in sorted(record_root.glob("*.json"))}
    initial_paid_ids = tuple(interrupted.paid_call_ids)

    assert interrupted.stages[StageName.RESEARCH].status is StageStatus.INTERRUPTED
    assert interrupted.stages[StageName.REPORT].status is StageStatus.SUCCEEDED
    assert len(worker_paths) == 2
    assert len(initial_paid_ids) == 4  # compiler, coordinator, and two visible workers
    assert len(initial_records) == 4
    assert sum(output_type is ResearchCoordinatorDecision for _, output_type in model.requests) == 1
    assert sum(output_type is ResearchWorkerReport for _, output_type in model.requests) == 2
    assert model.candidate_attempts == 1

    resumed = await runner.resume(project, run_id=interrupted.run_id)

    assert resumed.state.stages[StageName.RESEARCH].status is StageStatus.SUCCEEDED
    assert resumed.state.stages[StageName.RESEARCH].attempts == 2
    assert resumed.report.report.scientific_status == (
        ScientificStatus.RESEARCH_ACCEPTED_FOR_MANUSCRIPT.value
    )
    assert sum(output_type is ResearchCoordinatorDecision for _, output_type in model.requests) == 1
    assert sum(output_type is ResearchWorkerReport for _, output_type in model.requests) == 2
    assert sum(output_type is CandidateProofPackage for _, output_type in model.requests) == 1
    assert model.candidate_attempts == 2
    assert set(initial_paid_ids).issubset(resumed.state.paid_call_ids)
    assert len(resumed.state.paid_call_ids) == 10
    assert {
        relative: (run_root / relative).read_bytes() for relative in preserved_artifacts
    } == preserved_artifacts
    assert {name: (record_root / name).read_bytes() for name in initial_records} == initial_records
    assert set(initial_records) < {path.name for path in record_root.glob("*.json")}
    assert backend.calls == 0
    assert codex.calls == 0


@pytest.mark.asyncio
async def test_rejected_full_run_stops_before_manuscript_and_lean(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    problem = make_problem(project)
    runner, model, backend, codex = workflow_runner(project, accepted=False)

    result = await runner.run_new(
        problem,
        project,
        environment_snapshot={"fixture": "offline"},
    )

    assert result.state.scientific_status is ScientificStatus.RESEARCH_REJECTED
    assert result.report.report.scientific_status == ScientificStatus.RESEARCH_REJECTED.value
    assert result.state.stages[StageName.RESEARCH].status is StageStatus.SUCCEEDED
    assert result.state.stages[StageName.RESEARCH_AUDIT].status is StageStatus.SUCCEEDED
    for stage in (
        StageName.MANUSCRIPT,
        StageName.BIBLIOGRAPHY,
        StageName.LEAN_FEASIBILITY,
        StageName.LEAN_ALIGNMENT,
        StageName.LEAN_FORMALIZATION,
        StageName.LEAN_VERIFICATION,
    ):
        assert result.state.stages[stage].status is StageStatus.SKIPPED
    # A second complete worker report races with the first candidate audit. It is
    # independently packaged and checked before the coordinator's terminal stop is
    # honored, without purchasing a redundant third coordinator activation.
    assert len(model.requests) == 12
    assert sum(output_type is ResearchCoordinatorDecision for _, output_type in model.requests) == 2
    assert sum(output_type is CandidateProofPackage for _, output_type in model.requests) == 2
    research_result = json.loads(
        (result.state.run_root / "research" / "result.json").read_text(encoding="utf-8")
    )
    terminal_decision = research_result["coordinator_decisions"][-1]
    assert terminal_decision["stop_recommended"] is True
    assert terminal_decision["stop_category"] == "refuted"
    assert terminal_decision["candidate_packaging_recommended"] is False
    assert backend.calls == 0
    assert codex.calls == 0
    assert result.report.report_json.is_file()
    assert result.report.report_markdown.is_file()


@pytest.mark.asyncio
async def test_research_only_success_writes_complete_report(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    problem = make_problem(project)
    runner, model, backend, codex = workflow_runner(project, accepted=True)

    result = await runner.run_new(
        problem,
        project,
        options=WorkflowOptions(research_only=True),
        environment_snapshot={"fixture": "offline"},
    )

    assert result.report.report.scientific_status == (
        ScientificStatus.RESEARCH_ACCEPTED_FOR_MANUSCRIPT.value
    )
    assert result.report.report.manuscript_status == "NOT_REQUESTED"
    assert result.report.report.lean_status == ScientificStatus.LEAN_NOT_REQUESTED.value
    assert result.state.stages[StageName.REPORT].status is StageStatus.SUCCEEDED
    assert result.state.stages[StageName.MANUSCRIPT].status is StageStatus.SKIPPED
    assert result.state.stages[StageName.LEAN_VERIFICATION].status is StageStatus.SKIPPED
    assert len(result.state.paid_call_ids) == 10
    assert len(model.requests) == 10
    assert backend.calls == 0
    assert codex.calls == 0
    assert "research/result.json" in result.report.report.artifacts
    assert "prompts/compiled_problem.json" in result.report.report.artifacts


@pytest.mark.asyncio
async def test_completed_resume_repeats_no_calls_and_does_not_mutate_logs(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    runner, model, backend, codex = workflow_runner(project, accepted=True)
    initial = await runner.run_new(
        make_problem(project),
        project,
        options=WorkflowOptions(research_only=True),
        environment_snapshot={"fixture": "offline"},
    )
    logs_root = initial.state.run_root / "logs"
    log_paths = tuple(sorted(path for path in logs_root.rglob("*") if path.is_file()))
    before_logs = {str(path.relative_to(logs_root)): path.read_bytes() for path in log_paths}
    before_state = (initial.state.run_root / "state.json").read_bytes()
    before_calls = len(model.requests)
    before_paid_ids = tuple(initial.state.paid_call_ids)

    resumed = await runner.resume(project, run_id=initial.state.run_id)

    assert len(model.requests) == before_calls
    assert tuple(resumed.state.paid_call_ids) == before_paid_ids
    assert {
        str(path.relative_to(logs_root)): path.read_bytes() for path in log_paths
    } == before_logs
    assert (initial.state.run_root / "state.json").read_bytes() == before_state
    assert backend.calls == 0
    assert codex.calls == 0
    assert resumed.report.hashes == initial.report.hashes


@pytest.mark.asyncio
async def test_force_prompt_stage_reuses_successful_source_work_and_records(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    runner, model, backend, codex = workflow_runner(project, accepted=True)
    initial = await runner.run_new(
        make_problem(project),
        project,
        options=WorkflowOptions(research_only=True),
        environment_snapshot={"fixture": "offline"},
    )
    calls_before = len(model.requests)
    records_before = set((initial.state.run_root / "logs" / "model_calls").glob("*.json"))
    previous_research_result = (initial.state.run_root / "research" / "result.json").read_bytes()

    forced = await runner.resume(
        project,
        run_id=initial.state.run_id,
        force_stage=StageName.PROMPT_COMPILATION,
    )

    records_after = set((initial.state.run_root / "logs" / "model_calls").glob("*.json"))
    assert len(model.requests) == calls_before
    assert forced.state.metadata.get("model_cache_generation", 0) == 0
    assert forced.state.metadata["prompt_validation_generation"] == 1
    assert records_after == records_before
    assert len(forced.state.paid_call_ids) == calls_before
    research_history = forced.state.metadata["research_generation_history"]
    assert len(research_history) == 1
    assert research_history[0]["reason"] == "explicit --force-stage request"
    archived_research = forced.state.run_root / research_history[0]["artifact"]
    assert (archived_research / "result.json").read_bytes() == previous_research_result
    assert (archived_research / "coordinator" / "state.json").is_file()
    assert (forced.state.run_root / "research" / "result.json").is_file()
    assert backend.calls == 0
    assert codex.calls == 0


@pytest.mark.asyncio
async def test_two_runs_extend_one_persistent_problem_graph(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    runner, _, _, _ = workflow_runner(project, accepted=True)
    problem = make_problem(project)

    first = await runner.run_new(
        problem,
        project,
        options=WorkflowOptions(research_only=True, run_name="first-graph-run"),
        environment_snapshot={"fixture": "offline"},
    )
    first_graph = dict(first.state.metadata["knowledge_graph"])
    second = await runner.run_new(
        problem,
        project,
        options=WorkflowOptions(research_only=True, run_name="second-graph-run"),
        environment_snapshot={"fixture": "offline"},
    )
    second_graph = dict(second.state.metadata["knowledge_graph"])

    assert first_graph["problem_id"] == second_graph["problem_id"]
    assert first_graph["revision"] != second_graph["revision"]
    graph = KnowledgeGraph(project, "problem")
    nodes = graph.load_nodes()
    assert len([node for node in nodes if node.node_type is NodeType.PROBLEM]) == 1
    assert any(node.node_type is NodeType.TASK for node in nodes)
    assert any(node.node_type is NodeType.APPROACH for node in nodes)
    assert any(node.node_type is NodeType.AUDIT for node in nodes)
    assert any(node.node_type is NodeType.PROOF for node in nodes)
    run_ids = {node.created_in_run for node in nodes if node.node_type is NodeType.RUN}
    assert {first.state.run_id, second.state.run_id} <= run_ids
    assert graph.validate().valid


@pytest.mark.asyncio
async def test_problem_files_get_separate_graphs_and_can_explicitly_reuse_one(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    runner, _, _, _ = workflow_runner(project, accepted=True)
    primary_problem = make_problem(project)
    independent_problem = project / "independent.md"
    independent_problem.write_text("Prove an independent theorem.\n", encoding="utf-8")
    follow_up_problem = project / "follow-up.md"
    follow_up_problem.write_text(
        "Extend the original theorem to a new setting.\n", encoding="utf-8"
    )

    primary = await runner.run_new(
        primary_problem,
        project,
        options=WorkflowOptions(research_only=True, run_name="primary"),
        environment_snapshot={"fixture": "offline"},
    )
    independent = await runner.run_new(
        independent_problem,
        project,
        options=WorkflowOptions(research_only=True, run_name="independent"),
        environment_snapshot={"fixture": "offline"},
    )
    follow_up = await runner.run_new(
        follow_up_problem,
        project,
        options=WorkflowOptions(
            research_only=True,
            run_name="follow-up",
            knowledge_graph="problem",
        ),
        environment_snapshot={"fixture": "offline"},
    )

    assert primary.state.metadata["knowledge_graph"]["name"] == "problem"
    assert independent.state.metadata["knowledge_graph"]["name"] == "independent"
    assert independent.state.metadata["knowledge_graph"]["selection"] == "problem_stem"
    assert follow_up.state.metadata["knowledge_graph"]["name"] == "problem"
    assert follow_up.state.metadata["knowledge_graph"]["selection"] == "explicit_existing"
    primary_graph = KnowledgeGraph(project, "problem")
    independent_graph = KnowledgeGraph(project, "independent")
    assert (
        len([node for node in primary_graph.load_nodes() if node.node_type is NodeType.PROBLEM])
        == 2
    )
    assert (
        len([node for node in independent_graph.load_nodes() if node.node_type is NodeType.PROBLEM])
        == 1
    )
    assert primary_graph.validate().valid
    assert independent_graph.validate().valid


@pytest.mark.asyncio
async def test_explicit_graph_reuse_rejects_unknown_graph_before_creating_run(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    runner, model, _, _ = workflow_runner(project, accepted=True)
    problem = make_problem(project)

    with pytest.raises(GraphNotInitializedError, match="does not exist"):
        await runner.run_new(
            problem,
            project,
            options=WorkflowOptions(research_only=True, knowledge_graph="typo"),
            environment_snapshot={"fixture": "offline"},
        )

    assert not (project / ".matek" / "runs").exists()
    assert model.requests == []


@pytest.mark.asyncio
async def test_force_research_archives_generation_and_uses_fresh_call_cache(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    runner, model, backend, codex = workflow_runner(project, accepted=True)
    initial = await runner.run_new(
        make_problem(project),
        project,
        options=WorkflowOptions(research_only=True),
        environment_snapshot={"fixture": "offline"},
    )
    run_root = initial.state.run_root
    previous_research_result = (run_root / "research" / "result.json").read_bytes()
    records_root = run_root / "logs" / "model_calls"
    previous_records = {
        path.name: path.read_bytes() for path in sorted(records_root.glob("*.json"))
    }
    compiler_calls_before = sum(output_type is CompiledProblem for _, output_type in model.requests)
    coordinator_calls_before = sum(
        output_type is ResearchCoordinatorDecision for _, output_type in model.requests
    )
    research_calls_before = len(model.requests) - compiler_calls_before

    forced = await runner.resume(
        project,
        run_id=initial.state.run_id,
        force_stage=StageName.RESEARCH,
    )

    assert forced.state.metadata["model_cache_generation"] == 1
    assert forced.state.stages[StageName.RESEARCH].attempts == 2
    assert sum(output_type is CompiledProblem for _, output_type in model.requests) == (
        compiler_calls_before
    )
    assert (
        sum(output_type is ResearchCoordinatorDecision for _, output_type in model.requests)
        == coordinator_calls_before * 2
    )
    assert len(model.requests) - compiler_calls_before == research_calls_before * 2
    research_history = forced.state.metadata["research_generation_history"]
    assert len(research_history) == 1
    archived_research = run_root / research_history[0]["artifact"]
    assert (archived_research / "result.json").read_bytes() == previous_research_result
    assert (archived_research / "coordinator" / "state.json").is_file()
    assert (run_root / "research" / "result.json").is_file()
    assert {
        name: (records_root / name).read_bytes() for name in previous_records
    } == previous_records
    assert set(previous_records) < {path.name for path in records_root.glob("*.json")}
    assert backend.calls == 0
    assert codex.calls == 0


@pytest.mark.asyncio
async def test_report_regeneration_is_offline_and_recreates_missing_report(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    live_runner, _, _, _ = workflow_runner(project, accepted=True)
    initial = await live_runner.run_new(
        make_problem(project),
        project,
        options=WorkflowOptions(research_only=True),
        environment_snapshot={"fixture": "offline"},
    )
    initial.report.report_markdown.unlink()
    offline_runner, offline_model, backend, codex = workflow_runner(project, accepted=False)

    regenerated = offline_runner.regenerate_report(project, run_id=initial.state.run_id)

    assert offline_model.requests == []
    assert backend.calls == 0
    assert codex.calls == 0
    assert regenerated.report.report_markdown.is_file()
    assert regenerated.state.stages[StageName.REPORT].status is StageStatus.SUCCEEDED
    persisted = StateStore(initial.state.run_root).load()
    assert persisted.stages[StageName.REPORT].status is StageStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_workflow_emits_sparse_progress_from_intake_to_prompt(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    updates: list[tuple[Ascension, str]] = []
    runner = WorkflowRunner(
        AppConfig(
            project_root=project,
            research=ResearchSettings(
                minimum_initial_agents=4,
                maximum_concurrent_agents=2,
                maximum_rounds=1,
            ),
        ),
        WorkflowDependencies(
            model_client=InterruptAtCandidateModel(),
            execution_backend=ForbiddenBackend(),
            codex_client=ForbiddenCodex(),
            source_verifier=AlwaysVerifiedIdentifierVerifier(),
            progress=lambda ascension, message: updates.append((ascension, message)),
        ),
    )

    with pytest.raises(asyncio.CancelledError):
        await runner.run_new(
            make_problem(project),
            project,
            options=WorkflowOptions(research_only=True),
            environment_snapshot={"fixture": "offline"},
        )

    assert updates[:2] == [
        (Ascension.FETCH_PROBLEM, "Fetching problem."),
        (Ascension.FORMULATE_PROMPT, "Formulating technical research prompt."),
    ]
    assert (
        Ascension.START_RESEARCH_COORDINATOR,
        "Starting continuous research coordinator.",
    ) in updates
    assert any(
        ascension is Ascension.MANAGE_RESEARCH_POOL
        and message.startswith("Managing adaptive research pool: 4 initial assignments")
        for ascension, message in updates
    )
    assert (
        Ascension.AUDIT_RESEARCH,
        "Packaging the candidate solution for independent audits.",
    ) in updates
    assert updates[-1] == (Ascension.PREPARE_REPORT, "Preparing final report.")


@pytest.mark.asyncio
async def test_explicit_bibliography_retry_corrects_preserved_draft_without_restarting_writer(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    model = BibliographyResumeModel()
    backend = PdfBackend()
    codex = ForbiddenCodex()
    runner = WorkflowRunner(
        AppConfig(
            project_root=project,
            research=ResearchSettings(
                minimum_initial_agents=4,
                maximum_concurrent_agents=2,
                maximum_rounds=1,
            ),
            manuscript=ManuscriptSettings(maximum_revision_rounds=0),
        ),
        WorkflowDependencies(
            model_client=model,
            execution_backend=backend,
            codex_client=codex,
            source_verifier=AlwaysVerifiedIdentifierVerifier(),
        ),
    )
    initial = await runner.run_new(
        make_problem(project),
        project,
        options=WorkflowOptions(no_lean=True),
        environment_snapshot={"fixture": "offline"},
    )

    assert initial.state.stages[StageName.MANUSCRIPT].status is StageStatus.SUCCEEDED
    assert initial.state.stages[StageName.BIBLIOGRAPHY].status is StageStatus.SUCCEEDED
    assert initial.state.metadata["publication_status"] == "BLOCKED_BIBLIOGRAPHY"
    assert initial.state.scientific_status is ScientificStatus.RESEARCH_ACCEPTED_FOR_MANUSCRIPT
    assert initial.report.report.scientific_status == (
        ScientificStatus.RESEARCH_ACCEPTED_FOR_MANUSCRIPT.value
    )
    assert initial.report.report.manuscript_status == "DRAFT_WITH_WARNINGS"
    assert initial.report.report.workflow_status == "COMPLETE_WITH_WARNINGS"
    initial_paid_ids = tuple(initial.state.paid_call_ids)
    initial_writer_requests = [
        request for request, output_type in model.requests if output_type is ManuscriptDraft
    ]
    assert len(initial_writer_requests) == 1

    resumed = await runner.resume(
        project,
        run_id=initial.state.run_id,
        force_stage=StageName.BIBLIOGRAPHY,
    )

    writer_requests = [
        request for request, output_type in model.requests if output_type is ManuscriptDraft
    ]
    assert len(writer_requests) == 2
    correction_payload = json.loads(writer_requests[-1].input_text)
    assert correction_payload["previous_manuscript"] == manuscript_draft().model_dump(mode="json")
    assert resumed.state.stages[StageName.MANUSCRIPT].status is StageStatus.SUCCEEDED
    assert resumed.state.stages[StageName.BIBLIOGRAPHY].status is StageStatus.SUCCEEDED
    assert resumed.state.stages[StageName.LEAN_VERIFICATION].status is StageStatus.SKIPPED
    assert tuple(resumed.state.paid_call_ids[: len(initial_paid_ids)]) == initial_paid_ids
    assert len(resumed.state.paid_call_ids) == len(initial_paid_ids) + 2
    assert backend.calls == 2
    assert codex.calls == 0


def test_cli_dry_run_creates_no_workspace_and_never_constructs_live_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    problem = make_problem(tmp_path)

    def forbidden_live_runner(config: AppConfig) -> WorkflowRunner:
        del config
        raise AssertionError("dry-run constructed a live workflow runner")

    monkeypatch.setattr(cli_module, "_live_runner", forbidden_live_runner)
    result = CliRunner().invoke(app, ["run", str(problem), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Dry run complete" in result.output
    assert not (tmp_path / ".matek").exists()


def test_cli_no_web_search_is_global_and_default_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    problem = make_problem(tmp_path)

    default = CliRunner().invoke(app, ["run", str(problem), "--dry-run"])
    disabled = CliRunner().invoke(
        app,
        ["run", str(problem), "--no-web-search", "--dry-run"],
    )

    assert default.exit_code == 0, default.output
    assert "unlimited" in default.output
    assert disabled.exit_code == 0, disabled.output
    assert "enabled per stage" in default.output
    assert "disabled globally" in disabled.output
    assert not (tmp_path / ".matek").exists()


def test_cli_time_limit_is_resolved_without_starting_a_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    problem = make_problem(tmp_path)

    result = CliRunner().invoke(
        app,
        ["run", str(problem), "--time-limit-minutes", "25", "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert "total active time limit" in result.output
    assert "25 minutes" in result.output
    assert not (tmp_path / ".matek").exists()


def test_cli_heavy_research_defaults_are_resolved_in_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    problem = make_problem(tmp_path)

    result = CliRunner().invoke(
        app,
        ["run", str(problem), "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert "initial research agents" in result.output
    assert "16" in result.output
    assert "maximum pending assignments" in result.output
    assert "coordinator decision limit" in result.output
    assert "coordinator context budget" in result.output
    assert "800,000 serialized provider" in result.output
    assert "8 on-demand evidence requests" in result.output
    assert "concurrent research agents" in result.output
    assert "up to 32 effective" in result.output
    assert "research coordinator" in result.output
    assert "max effort" in result.output
    assert "research agents" in result.output
    assert "xhigh effort" in result.output
    assert result.output.count("32") >= 2
    assert "total research-subagent limit" not in result.output
    assert not (tmp_path / ".matek").exists()


def test_cli_resolves_continuous_decision_limit_and_rejects_legacy_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    problem = make_problem(tmp_path)

    current = CliRunner().invoke(
        app,
        [
            "run",
            str(problem),
            "--max-coordinator-decisions",
            "192",
            "--dry-run",
        ],
    )
    legacy = CliRunner().invoke(
        app,
        ["run", str(problem), "--max-rounds", "2", "--dry-run"],
    )
    conflict = CliRunner().invoke(
        app,
        [
            "run",
            str(problem),
            "--max-coordinator-decisions",
            "192",
            "--max-rounds",
            "2",
            "--dry-run",
        ],
    )

    assert current.exit_code == 0, current.output
    assert "192" in current.output
    assert legacy.exit_code == 0, legacy.output
    assert "64" in legacy.output
    assert conflict.exit_code == 2
    assert "cannot be combined" in conflict.output
    assert not (tmp_path / ".matek").exists()


def test_global_no_web_policy_reaches_every_model_stage_and_source_resolver(
    tmp_path: Path,
) -> None:
    config = merge_config(AppConfig(project_root=tmp_path), {"no_web_search": True})
    runner = WorkflowRunner(
        config,
        WorkflowDependencies(
            model_client=ResearchWorkflowModel(accepted=False),  # type: ignore[arg-type]
            execution_backend=ForbiddenBackend(),
            codex_client=ForbiddenCodex(),
            source_verifier=AlwaysVerifiedIdentifierVerifier(),
        ),
    )

    assert all(
        not runner._model_settings(category).web_search
        for category in (
            "prompt",
            "research_coordinator",
            "research_worker",
            "audit",
            "manuscript",
        )
    )
    assert isinstance(
        runner._source_verifier(tmp_path),
        WebDisabledSourceVerifier,
    )


def test_research_coordinator_and_worker_use_distinct_default_efforts(
    tmp_path: Path,
) -> None:
    runner = WorkflowRunner(
        AppConfig(project_root=tmp_path),
        WorkflowDependencies(
            model_client=ResearchWorkflowModel(accepted=False),  # type: ignore[arg-type]
            execution_backend=ForbiddenBackend(),
            codex_client=ForbiddenCodex(),
        ),
    )

    coordinator = runner._model_settings("research_coordinator")
    worker = runner._model_settings("research_worker")

    assert coordinator.model == "gpt-5.6-sol"
    assert coordinator.reasoning_mode == "pro"
    assert coordinator.reasoning_effort == "max"
    assert worker.model == "gpt-5.6-sol"
    assert worker.reasoning_mode == "pro"
    assert worker.reasoning_effort == "xhigh"


def test_codex_model_is_pinned_into_every_durable_model_request(tmp_path: Path) -> None:
    runner = WorkflowRunner(
        AppConfig(
            project_root=tmp_path,
            codex=CodexSettings(model="gpt-5.6-pinned"),
        ),
        WorkflowDependencies(
            model_client=ResearchWorkflowModel(accepted=False),  # type: ignore[arg-type]
            execution_backend=ForbiddenBackend(),
            codex_client=ForbiddenCodex(),
        ),
    )

    settings = {
        category: runner._model_settings(category)
        for category in (
            "prompt",
            "research_coordinator",
            "research_worker",
            "audit",
            "manuscript",
        )
    }

    assert {value.model for value in settings.values()} == {"gpt-5.6-pinned"}
    assert settings["research_coordinator"].reasoning_effort == "max"
    assert settings["research_worker"].reasoning_effort == "xhigh"


def test_cli_progress_uses_ascension_terminal_format() -> None:
    with cli_module.console.capture() as capture:
        cli_module._print_progress(Ascension.RUN_RESEARCH, "Launching 4 research agents.")

    assert capture.get().strip() == "ASCENSION 3: Launching 4 research agents."


def test_cli_error_renders_bracketed_tokens_literally() -> None:
    with cli_module.console.capture() as capture:
        with pytest.raises(typer.Exit):
            cli_module._abort(ValueError("unresolved tokens: [x,y], [1,c], [TODO]"))

    rendered = capture.get()
    assert "[x,y]" in rendered
    assert "[1,c]" in rendered
    assert "[TODO]" in rendered


@pytest.mark.asyncio
async def test_noninteractive_lean_prompt_proceeds_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module.sys, "stdin", io.StringIO(""))
    request = LeanConsentRequest(
        run_id="fixture-run",
        manuscript_path=Path("paper.tex"),
    )

    with cli_module.console.capture() as capture:
        outcome = await cli_module._terminal_lean_consent(request)

    assert outcome is LeanConsentOutcome.NON_INTERACTIVE
    assert "Proceed with formal Lean verification?" in capture.get()
    assert "proceeding with Lean verification" in capture.get()


def test_cli_init_status_and_usage_exit_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    cli = CliRunner()

    initialized = cli.invoke(app, ["init"])
    assert initialized.exit_code == 0, initialized.output
    assert (tmp_path / "matek.toml").is_file()
    assert (tmp_path / ".matek" / ".gitignore").is_file()

    problem = make_problem(tmp_path)
    intake = ingest_problem(
        problem_file=problem,
        project_root=tmp_path,
        config=AppConfig(project_root=tmp_path),
        invocation={"fixture": True},
        run_id="20260719T120000Z-cli-abcdef",
        snapshot={"fixture": "offline"},
    )
    status_result = cli.invoke(app, ["status"])
    assert status_result.exit_code == 0, status_result.output
    assert intake.state.run_id in status_result.output
    assert "intake" in status_result.output

    repeated_init = cli.invoke(app, ["init"])
    assert repeated_init.exit_code == 2
    missing_run = cli.invoke(app, ["status", "20260719T120000Z-missing-abcdef"])
    assert missing_run.exit_code == 2
