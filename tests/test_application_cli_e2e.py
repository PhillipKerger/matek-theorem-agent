from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any, Literal

import pytest
import typer
from pydantic import BaseModel
from typer.testing import CliRunner

import ascend_math_agent.cli as cli_module
from ascend_math_agent.application import (
    LEAN_CONSENT_TIMEOUT_SECONDS,
    LeanConsentOutcome,
    LeanConsentRequest,
    WorkflowDependencies,
    WorkflowOptions,
    WorkflowRunner,
)
from ascend_math_agent.cli import app
from ascend_math_agent.codex_client import CodexRequest, CodexResult
from ascend_math_agent.config import (
    AppConfig,
    BackendSettings,
    LeanSettings,
    ManuscriptSettings,
    ResearchSettings,
)
from ascend_math_agent.execution.base import CommandRequest, CommandResult
from ascend_math_agent.intake import ingest_problem
from ascend_math_agent.models import ScientificStatus, StageName, StageStatus
from ascend_math_agent.openai_client import ModelRequest, ModelResult
from ascend_math_agent.progress import Ascension
from ascend_math_agent.stages.common import sha256_json, sha256_text
from ascend_math_agent.stages.compile_prompt import (
    CompiledProblem,
    PromptCompilationStatus,
    PromptPlaceholderRepair,
)
from ascend_math_agent.stages.lean import (
    MANDATORY_ALIGNMENT_FIELDS,
    AlignmentCheck,
    AlignmentStatus,
    ClaimAlignment,
    LeanFeasibilityAssessment,
    LeanFeasibilityClass,
    LeanStatementDraft,
)
from ascend_math_agent.stages.manuscript import (
    BibliographyAudit,
    BibliographyEntryAudit,
    BibliographyEntryStatus,
    BibliographyStatus,
    FrozenClaimFidelity,
    IntroductionCoverage,
    ManuscriptDraft,
    RelatedWorkClaimAudit,
)
from ascend_math_agent.stages.research import (
    AuditDecision,
    AuditVerdict,
    CandidateProofPackage,
    FinalJudgeDecision,
    FinalJudgeVerdict,
    ResearchAssignment,
    ResearchRoundPlan,
    ResearchWorkerReport,
    WorkerStatus,
)
from ascend_math_agent.state import StateStore

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
ASCEND_FIXTURE_REPOSITORY_URL = "https://github.com/ascend-test-fixtures/ascend-math-agent"
ASCEND_FIXTURE_WHITEPAPER_ID = "2099.99999"
ASCEND_FIXTURE_WHITEPAPER_URL = f"https://arxiv.org/abs/{ASCEND_FIXTURE_WHITEPAPER_ID}"


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
                        "url": ASCEND_FIXTURE_REPOSITORY_URL,
                        "title": "ASCEND software test fixture",
                    },
                    {
                        "type": "url",
                        "url": ASCEND_FIXTURE_WHITEPAPER_URL,
                        "title": "ASCEND whitepaper test fixture",
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
        elif output_type is ResearchRoundPlan:
            output = ResearchRoundPlan(
                round_id=1,
                assignments=[
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
                ],
                rationale="Four materially different proof mechanisms.",
                candidate_packaging_recommended=True,
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
            "The ASCEND system with GPT 5.6 was used in this work "
            "\\cite{ascendSoftwareFixture,ascendWhitepaperFixture}.\n"
            "\\bibliography{references}\n"
            "\\end{document}\n"
        ),
        references_bib=(
            "@article{smith2020, title={A Real Paper}, author={Smith, Ada}, "
            "year={2020}, journal={Journal of Fixtures}, doi={10.5555/12345678}}\n"
            "@misc{ascendSoftwareFixture, author={ASCEND test-fixture contributors}, "
            "title={ASCEND: Autonomous System for Conjecture Exploration and Verified "
            "Deduction}, year={2099}, howpublished={Software repository}, "
            f"url={{{ASCEND_FIXTURE_REPOSITORY_URL}}}}}\n"
            "@misc{ascendWhitepaperFixture, author={ASCEND test-fixture contributors}, "
            "title={ASCEND: Autonomous System for Conjecture Exploration and Verified "
            "Deduction}, year={2099}, howpublished={arXiv preprint}, "
            f"eprint={{{ASCEND_FIXTURE_WHITEPAPER_ID}}}, archiveprefix={{arXiv}}}}\n"
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
                citation_key="ascendSoftwareFixture",
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
                authoritative_evidence=([ASCEND_FIXTURE_REPOSITORY_URL] if verified else []),
            ),
            BibliographyEntryAudit(
                citation_key="ascendWhitepaperFixture",
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
                authoritative_evidence=([ASCEND_FIXTURE_WHITEPAPER_URL] if verified else []),
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


class FullWorkflowModel(ResearchWorkflowModel):
    """Drive the complete two-round acceptance scenario through every real stage."""

    def __init__(self) -> None:
        super().__init__(accepted=True)

    async def generate_structured(
        self,
        request: ModelRequest,
        output_type: type[Any],
    ) -> ModelResult[Any]:
        if output_type is ResearchRoundPlan:
            self.requests.append((request, output_type))
            round_id = int(json.loads(request.input_text)["round_id"])
            families = (
                ("direct", "structural", "counterexample", "literature")
                if round_id == 1
                else ("synthesis",)
            )
            parsed: BaseModel = ResearchRoundPlan(
                round_id=round_id,
                assignments=[
                    ResearchAssignment(
                        id=f"round-{round_id}-route-{index}",
                        approach_family=family,
                        task=f"Develop the {family} route in round {round_id}.",
                        expected_output="A complete proof or an exact obstruction.",
                    )
                    for index, family in enumerate(families, start=1)
                ],
                rationale="The second round synthesizes the independent first-round routes.",
                candidate_packaging_recommended=round_id == 2,
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
                challenge_lean="theorem ascend_main : True := by\n  sorry\n",
                statement_explanation="The frozen fixture claim is represented by True.",
                claim_map={"conclusion": "True"},
                theorem_name="ascend_main",
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
        elif request.argv[-1].endswith("_AscendAxiomCheck.lean"):
            stdout = "'ascend_main' depends on no axioms"
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
        ),
    )
    monkeypatch.chdir(project)
    monkeypatch.setattr(cli_module, "_live_runner", lambda config: runner)

    invocation = CliRunner().invoke(app, ["run", str(problem)])

    assert invocation.exit_code == 0, invocation.output
    assert "stopped before research" in invocation.output
    assert "What mathematical objects are being extended?" in invocation.output
    [run_root] = (project / ".ascend" / "runs").iterdir()
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
    assert "start a new ASCEND run" in clarification
    assert "Problem clarification required" in report
    assert "revise the problem file" in report.lower()


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

    with pytest.raises(ValueError, match=r"\[INSERT TARGET HERE\]"):
        await runner.run_new(make_problem(project), project)

    [run_root] = (project / ".ascend" / "runs").iterdir()
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
async def test_complete_two_round_pipeline_is_lean_verified_and_resume_is_noop(
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
    assert len(research["rounds"]) == 2
    assert bibliography.status is BibliographyStatus.VERIFIED
    assert all(entry.status is BibliographyEntryStatus.VERIFIED for entry in bibliography.entries)
    assert result.state.scientific_status is ScientificStatus.LEAN_VERIFIED
    assert result.state.metadata["backend"]["provider"] == provider
    assert result.state.metadata["backend"]["automatic_fallback"] is False
    assert result.report.report.lean_status == ScientificStatus.LEAN_VERIFIED.value
    assert result.state.stages[StageName.LEAN_VERIFICATION].status is StageStatus.SUCCEEDED
    assert result.report.report.artifacts
    report_markdown = result.report.report_markdown.read_text(encoding="utf-8")
    assert all(f"[`{relative}`]" in report_markdown for relative in result.report.report.artifacts)
    assert (result.state.run_root / "manuscript" / "paper.pdf").is_file()
    assert (result.state.run_root / "lean" / "challenge.lean").is_file()
    assert len(codex.requests) == 1
    assert len(backend.requests) == 3
    assert [ascension for ascension, _ in updates] == [
        Ascension.FETCH_PROBLEM,
        Ascension.FORMULATE_PROMPT,
        Ascension.PLAN_RESEARCH,
        Ascension.RUN_RESEARCH,
        Ascension.PLAN_RESEARCH,
        Ascension.RUN_RESEARCH,
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
            lean_consent=consent,
        ),
    )
    return runner, model, backend, codex


@pytest.mark.asyncio
async def test_user_can_decline_lean_after_verified_manuscript(tmp_path: Path) -> None:
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
    assert result.state.scientific_status is ScientificStatus.LEAN_NOT_REQUESTED
    assert result.state.metadata["lean_consent"]["outcome"] == "user_declined"
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
    assert resumed.state.scientific_status is ScientificStatus.LEAN_VERIFIED
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
    assert result.state.scientific_status is ScientificStatus.LEAN_VERIFIED
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
    with pytest.raises(RuntimeError, match="after durable consent"):
        await runner.run_new(make_problem(project), project)

    [run_root] = (project / ".ascend" / "runs").iterdir()
    interrupted = StateStore(run_root).load()
    assert interrupted.metadata["lean_consent"]["outcome"] == "user_approved"
    assert consent_calls == 1

    monkeypatch.setattr(runner, "_lean_stage", original_lean_stage)
    resumed = await runner.resume(project, run_id=interrupted.run_id)

    assert consent_calls == 1
    assert resumed.state.scientific_status is ScientificStatus.LEAN_VERIFIED


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
        ),
    )

    with pytest.raises(asyncio.CancelledError):
        await runner.run_new(
            make_problem(project),
            project,
            options=WorkflowOptions(research_only=True),
            environment_snapshot={"fixture": "offline"},
        )

    [run_root] = (project / ".ascend" / "runs").iterdir()
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
async def test_resume_after_worker_batch_reuses_paid_calls_and_persisted_artifacts(
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
        ),
    )

    with pytest.raises(asyncio.CancelledError):
        await runner.run_new(
            make_problem(project),
            project,
            options=WorkflowOptions(research_only=True),
            environment_snapshot={"fixture": "offline"},
        )

    [run_root] = (project / ".ascend" / "runs").iterdir()
    interrupted = StateStore(run_root).load()
    plan_path = run_root / "research" / "rounds" / "1" / "plan.json"
    worker_paths = tuple(sorted((plan_path.parent / "workers").glob("*.json")))
    preserved_artifacts = {
        path.relative_to(run_root).as_posix(): path.read_bytes()
        for path in (plan_path, *worker_paths)
    }
    record_root = run_root / "logs" / "model_calls"
    initial_records = {path.name: path.read_bytes() for path in sorted(record_root.glob("*.json"))}
    initial_paid_ids = tuple(interrupted.paid_call_ids)

    assert interrupted.stages[StageName.RESEARCH].status is StageStatus.INTERRUPTED
    assert interrupted.stages[StageName.REPORT].status is StageStatus.SUCCEEDED
    assert len(worker_paths) == 4
    assert len(initial_paid_ids) == 6  # compiler, coordinator, and four workers
    assert len(initial_records) == 6
    assert sum(output_type is ResearchRoundPlan for _, output_type in model.requests) == 1
    assert sum(output_type is ResearchWorkerReport for _, output_type in model.requests) == 4
    assert model.candidate_attempts == 1

    resumed = await runner.resume(project, run_id=interrupted.run_id)

    assert resumed.state.stages[StageName.RESEARCH].status is StageStatus.SUCCEEDED
    assert resumed.state.stages[StageName.RESEARCH].attempts == 2
    assert resumed.report.report.scientific_status == (
        ScientificStatus.RESEARCH_ACCEPTED_FOR_MANUSCRIPT.value
    )
    assert sum(output_type is ResearchRoundPlan for _, output_type in model.requests) == 1
    assert sum(output_type is ResearchWorkerReport for _, output_type in model.requests) == 4
    assert sum(output_type is CandidateProofPackage for _, output_type in model.requests) == 1
    assert model.candidate_attempts == 2
    assert set(initial_paid_ids).issubset(resumed.state.paid_call_ids)
    assert len(resumed.state.paid_call_ids) == 12
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
    assert len(model.requests) == 12
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
    assert len(result.state.paid_call_ids) == 12
    assert len(model.requests) == 12
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
    assert (Ascension.PLAN_RESEARCH, "Planning research round 1.") in updates
    assert (
        Ascension.RUN_RESEARCH,
        "Launching 4 research agents for round 1.",
    ) in updates
    assert (
        Ascension.AUDIT_RESEARCH,
        "Packaging the candidate solution for independent audits.",
    ) in updates
    assert updates[-1] == (Ascension.PREPARE_REPORT, "Preparing final report.")


@pytest.mark.asyncio
async def test_resume_failed_bibliography_corrects_persisted_draft_without_restarting_writer(
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
        ),
    )
    initial = await runner.run_new(
        make_problem(project),
        project,
        options=WorkflowOptions(no_lean=True),
        environment_snapshot={"fixture": "offline"},
    )

    assert initial.state.stages[StageName.MANUSCRIPT].status is StageStatus.SUCCEEDED
    assert initial.state.stages[StageName.BIBLIOGRAPHY].status is StageStatus.FAILED
    initial_paid_ids = tuple(initial.state.paid_call_ids)
    initial_writer_requests = [
        request for request, output_type in model.requests if output_type is ManuscriptDraft
    ]
    assert len(initial_writer_requests) == 1

    resumed = await runner.resume(project, run_id=initial.state.run_id)

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
    assert backend.calls == 1
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
    assert not (tmp_path / ".ascend").exists()


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
        manuscript_path=Path("paper.pdf"),
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
    assert (tmp_path / "ascend.toml").is_file()
    assert (tmp_path / ".ascend" / ".gitignore").is_file()

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
