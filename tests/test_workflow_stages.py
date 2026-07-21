from __future__ import annotations

import asyncio
import json
from collections.abc import Collection
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

import ascend_math_agent.stages.research as research_stage
from ascend_math_agent.budget import BudgetExceeded, BudgetSnapshot
from ascend_math_agent.codex_client import CodexRequest, CodexResult
from ascend_math_agent.config import ModelSettings
from ascend_math_agent.execution.base import CommandRequest, CommandResult
from ascend_math_agent.openai_client import ModelRequest, ModelResult
from ascend_math_agent.source_provenance import (
    SourceVerificationRecord,
    SourceVerificationReport,
    SourceVerificationStatus,
)
from ascend_math_agent.stages.common import (
    StageGateError,
    StageValidationError,
    atomic_write_text,
    sha256_json,
    sha256_text,
)
from ascend_math_agent.stages.compile_prompt import (
    EXPECTED_FRAMEWORK_SHA256,
    CompiledProblem,
    LiteratureStatus,
    PlaceholderDisposition,
    PromptCompilationStatus,
    PromptPlaceholderRepair,
    SourceLedgerEntry,
    SourceLedgerRepair,
    compile_prompt,
    find_unresolved_placeholders,
)
from ascend_math_agent.stages.lean import (
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
from ascend_math_agent.stages.manuscript import (
    BibliographyAudit,
    BibliographyEntryAudit,
    BibliographyEntryStatus,
    BibliographyStatus,
    FrozenClaimFidelity,
    IntroductionCoverage,
    LatexBuildResult,
    ManuscriptDraft,
    ManuscriptOutcome,
    ManuscriptResult,
    RelatedWorkClaimAudit,
    RelatedWorkValidation,
    generate_manuscript,
    resume_manuscript_bibliography,
)
from ascend_math_agent.stages.research import (
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
    run_adaptive_research,
)

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
ASCEND_FIXTURE_REPOSITORY_URL = "https://github.com/ascend-test-fixtures/ascend-math-agent"
ASCEND_FIXTURE_WHITEPAPER_ID = "2099.99999"
ASCEND_FIXTURE_WHITEPAPER_URL = f"https://arxiv.org/abs/{ASCEND_FIXTURE_WHITEPAPER_ID}"


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
        normalized_statement="Prove the fixture theorem.",
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
    }
    assert client.requests[0].settings.reasoning_effort == "xhigh"
    assert client.requests[0].settings.web_search is True

    bad_client = StaticClient(
        [compiled_problem(covered_compiled_prompt("Prove [INSERT TARGET HERE]."))]
    )
    with pytest.raises(StageValidationError, match="unresolved editorial"):
        await compile_prompt(
            client=bad_client,
            problem_text="Prove P.",
            framework_path=FRAMEWORK,
            prompts_dir=tmp_path / "bad",
        )


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
async def test_prompt_compiler_preserves_artifacts_on_target_critical_repair_failure(
    tmp_path: Path,
) -> None:
    payload = compiled_problem(covered_compiled_prompt("Prove [INSERT TARGET HERE]."))

    with pytest.raises(StageValidationError, match=r"\[INSERT TARGET HERE\]"):
        await compile_prompt(
            client=StaticClient([payload]),
            problem_text="Prove P.",
            framework_path=FRAMEWORK,
            prompts_dir=tmp_path,
        )

    assert (tmp_path / "compiled_problem.json").is_file()
    assert (tmp_path / "compiled_research_prompt.md").is_file()
    validation = json.loads((tmp_path / "prompt_validation.json").read_text(encoding="utf-8"))
    assert validation["passed"] is False
    assert validation["diagnostics"][0]["disposition"] == "target_critical_failure"


@pytest.mark.asyncio
async def test_prompt_compiler_returns_a_terminal_clarification_request(
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

    assert result.needs_clarification
    assert "compiled_prompt" not in result.artifacts.paths
    assert (tmp_path / "compiled_problem.json").is_file()
    request = (tmp_path / "clarification_request.md").read_text(encoding="utf-8")
    assert "stopped before mathematical research" in request
    assert "Which objects are being extended?" in request
    assert "start a new ASCEND run" in request


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
    with pytest.raises(StageValidationError, match="preserve the reusable framework"):
        await compile_prompt(
            client=incomplete,
            problem_text="Prove P.",
            framework_path=FRAMEWORK,
            prompts_dir=tmp_path / "incomplete",
        )

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
    with pytest.raises(StageValidationError, match="absent from its verified source ledger"):
        await compile_prompt(
            client=StaticClient([unledgered], tool_metadata=web_source_metadata()),
            problem_text="Prove the cited fixture statement.",
            framework_path=FRAMEWORK,
            prompts_dir=tmp_path / "unledgered",
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
                )
        elif output_type is ResearchWorkerReport:
            assignment = json.loads(request.input_text)["assignment"]
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            try:
                await asyncio.sleep(0.01)
            finally:
                self.active -= 1
            parsed = ResearchWorkerReport(
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
                parsed=ResearchWorkerReport(
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
                report = ResearchWorkerReport(
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
                report = ResearchWorkerReport(
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
                report = ResearchWorkerReport(
                    assignment_id=assignment_id,
                    status=WorkerStatus.BLOCKED,
                    formal_results=[],
                    proof_content="Reduction attempted.",
                    exact_gap="Missing compactness lemma.",
                    sources=[],
                    mechanism=assignment["task"],
                )
            elif assignment_id == "route-4":
                report = ResearchWorkerReport(
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
                report = ResearchWorkerReport(
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
                assert payload["visible_worker_reports"][0]["proof_content"] == (
                    "Full proof of the reduction lemma."
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
                parsed: BaseModel = ResearchWorkerReport(
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
                parsed = ResearchWorkerReport(
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
                parsed = ResearchWorkerReport(
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
                parsed=ResearchWorkerReport(
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
                    stop_category="scientific",
                )
            return ModelResult(parsed=parsed, response_id=response_id)
        if output_type is ResearchWorkerReport:
            assignment = json.loads(request.input_text)["assignment"]
            assignment_id = assignment["id"]
            if assignment_id == "fast-terminal-feedback":
                await self.candidate_started.wait()
                parsed = ResearchWorkerReport(
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
                parsed = ResearchWorkerReport(
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
                    stop_category="scientific",
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
                parsed=ResearchWorkerReport(
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
        exact_theorem="For every n, P n.",
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
            raise RuntimeError("simulated worker interruption")

    web_enabled = ModelSettings(web_search=True)
    with pytest.raises(RuntimeError, match="simulated worker interruption"):
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
                parsed=ResearchWorkerReport(
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
                    parsed=ResearchWorkerReport(
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

    assert settings.minimum_initial_assignments == 16
    assert settings.maximum_concurrent_agents == 32
    assert settings.maximum_pending_assignments == 32
    assert settings.maximum_coordinator_decisions == 256
    assert "maximum_research_subagents" not in type(settings).model_fields


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
    assert route_one["proof_content"] == "Proof of Lemma A."
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
    # The first two workers finish together under the two-agent semaphore. The
    # remaining routes are stopped once that visible proof passes the full gate.
    assert len(result.registry.approaches) == 2
    assert client.maximum_active == 2
    assert result.calls.model_calls == 9
    assert list((tmp_path / "candidate" / "attempts").glob("event-*-attempt-1/package.json"))
    assert (tmp_path / "candidate" / "package.json").is_file()
    assert (tmp_path / "verdict.json").is_file()


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
        workflow_settings=ResearchWorkflowSettings(maximum_coordinator_decisions=32),
        source_verifier=OfflineIdentifierVerifier(),
    )

    assert result.outcome is ResearchOutcome.PARTIAL
    assert not result.accepted_for_manuscript
    assert "not independently verified" in result.unresolved_obligations[0]
    assert result.candidate is not None
    assert result.candidate.imported_theorems[0].verified is False
    assert not result.audits
    assert list(
        (tmp_path / "candidate" / "attempts").glob("event-*-attempt-1/source_verification.json")
    )


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
            maximum_concurrent_agents=9,
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
        (FinalJudgeDecision.REJECTED, ResearchOutcome.REJECTED),
        (FinalJudgeDecision.PARTIAL, ResearchOutcome.PARTIAL),
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
    )
    assert result.outcome == outcome
    assert not result.accepted_for_manuscript
    assert (tmp_path / "candidate" / "package.json").is_file()
    assert result.unresolved_obligations


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
                parsed=ResearchWorkerReport(
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
                citation_key="ascendSoftwareFixture",
                status=BibliographyEntryStatus.VERIFIED,
                exists=True,
                exact_title_verified=True,
                authors_verified=True,
                year_verified=True,
                venue_or_status_verified=True,
                stable_identifier_checked=True,
                characterization_supported=True,
                theorem_hypotheses_supported=True,
                authoritative_evidence=[ASCEND_FIXTURE_REPOSITORY_URL],
            ),
            BibliographyEntryAudit(
                citation_key="ascendWhitepaperFixture",
                status=BibliographyEntryStatus.VERIFIED,
                exists=True,
                exact_title_verified=True,
                authors_verified=True,
                year_verified=True,
                venue_or_status_verified=True,
                stable_identifier_checked=True,
                characterization_supported=True,
                theorem_hypotheses_supported=True,
                authoritative_evidence=[ASCEND_FIXTURE_WHITEPAPER_URL],
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
    assert result.related_work.ascend_repository_citation_key == "ascendSoftwareFixture"
    assert result.related_work.ascend_whitepaper_citation_key == "ascendWhitepaperFixture"
    assert result.latex_build is not None and result.latex_build.pdf_path is not None
    assert len(backend.requests) == 1
    assert "-no-shell-escape" in backend.requests[0].argv
    assert "-norc" in backend.requests[0].argv
    writer_payload = json.loads(client.requests[0].input_text)
    assert "statement_of_ai_usage" in writer_payload["mandatory_structured_content"]


@pytest.mark.asyncio
async def test_manuscript_rejects_missing_statement_of_ai_usage_before_source_audit(
    tmp_path: Path,
) -> None:
    draft = manuscript_draft().model_copy(deep=True)
    statement_start = draft.paper_tex.index("\\section*{Statement of AI Usage}")
    bibliography_start = draft.paper_tex.index("\\bibliography{references}")
    draft.paper_tex = draft.paper_tex[:statement_start] + draft.paper_tex[bibliography_start:]
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

    assert result.outcome == ManuscriptOutcome.CONTENT_REJECTED
    assert not result.related_work.ai_usage_disclosure_verified
    assert any("Statement of AI Usage" in issue for issue in result.related_work.issues)
    assert len(client.requests) == 1
    assert not backend.requests


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
    assert result.outcome == ManuscriptOutcome.BIBLIOGRAPHY_REJECTED
    assert not result.passed_lean_gate
    assert not backend.requests


@pytest.mark.asyncio
async def test_manuscript_rejects_missing_introduction_coverage_and_frozen_claim_drift(
    tmp_path: Path,
) -> None:
    bad_coverage = manuscript_draft().model_copy(deep=True)
    bad_coverage.introduction_coverage.advance_over_prior_work_excerpt = (
        "This purported advance does not occur anywhere in the generated introduction text"
    )
    coverage_client = StaticClient([bad_coverage])
    coverage_result = await generate_manuscript(
        client=coverage_client,
        backend=PdfBackend(),
        research_result=accepted_research(),
        claim_contract=MANUSCRIPT_CLAIM_CONTRACT,
        source_ledger=[],
        manuscript_dir=tmp_path / "coverage",
    )
    assert coverage_result.outcome == ManuscriptOutcome.CONTENT_REJECTED
    assert not coverage_result.related_work.introduction_coverage_verified
    assert coverage_result.calls.model_calls == 1

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
    assert drift_result.outcome == ManuscriptOutcome.CONTENT_REJECTED
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
    assert result.outcome == ManuscriptOutcome.CONTENT_REJECTED
    assert any("prohibited TeX escape" in issue for issue in result.related_work.issues)
    assert len(client.requests) == 1
    assert not backend.requests


@pytest.mark.asyncio
async def test_bibliography_verifier_requires_web_search_before_any_write(tmp_path: Path) -> None:
    client = StaticClient([])
    destination = tmp_path / "manuscript"
    with pytest.raises(StageValidationError, match="requires web_search"):
        await generate_manuscript(
            client=client,
            backend=PdfBackend(),
            research_result=accepted_research(),
            claim_contract=MANUSCRIPT_CLAIM_CONTRACT,
            source_ledger=[],
            manuscript_dir=destination,
            verifier_settings=ModelSettings(web_search=False),
        )
    assert not client.requests
    assert not destination.exists()


@pytest.mark.asyncio
async def test_arbitrary_bibliography_evidence_cannot_pass_gate(tmp_path: Path) -> None:
    audit = verified_bibliography().model_copy(deep=True)
    audit.entries[0].authoritative_evidence = ["the publisher says this is real"]
    audit.claim_checks[0].evidence = ["another model confirmed the theorem"]
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
    assert result.outcome == ManuscriptOutcome.BIBLIOGRAPHY_REJECTED
    assert not result.bibliography_verified


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
    assert result.outcome == ManuscriptOutcome.BIBLIOGRAPHY_REJECTED
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
    assert first.outcome == ManuscriptOutcome.BIBLIOGRAPHY_REJECTED

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
    assert "mandatory_bibliography_corrections" in first_new_payload
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
    )
    assert result.outcome == ManuscriptOutcome.LATEX_FAILED
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
            if "_AscendAxiomCheck.lean" in request.argv[-1]
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
    lean_dir = project / ".ascend" / "runs" / "fixture" / "lean"

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
    lean_dir = project / ".ascend" / "runs" / "fixture" / "lean"

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
    lean_dir = project / ".ascend" / "runs" / "fixture" / "lean"

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
    lean_dir = project / ".ascend" / "runs" / "fixture" / "lean"
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
async def test_lean_gate_rejects_uncompiled_manuscript(tmp_path: Path) -> None:
    research = accepted_research()
    manuscript = compiled_manuscript(research, tmp_path)
    manuscript.outcome = ManuscriptOutcome.LATEX_FAILED
    with pytest.raises(StageGateError, match="verified bibliography"):
        await run_lean_pipeline(
            client=LeanModelClient(),
            codex_client=EditingCodex(),
            backend=LeanBackend(),
            research_result=research,
            manuscript_result=manuscript,
            claim_contract={"conclusion": "True"},
            lean_dir=tmp_path / "lean",
            lean_project_root=tmp_path,
        )


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
