from __future__ import annotations

import asyncio
import json
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from ..config import ModelSettings
from ..openai_client import ModelClient, ModelRequest, ModelResult
from ..progress import Ascension, ProgressReporter, no_progress
from ..source_identifiers import tool_metadata_source_identifiers
from ..source_provenance import IdentifierVerifier, SourceEvidenceClaim
from .common import (
    ArtifactManifest,
    CallManifest,
    StageValidationError,
    atomic_write_json,
    atomic_write_text,
    build_artifact_manifest,
    ensure_stage_directory,
    project_resource,
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
    stopping_condition: str = "Return concrete formal content or an exact obstruction."

    @field_validator("id", "approach_family", "task", "expected_output")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()


class ResearchRoundPlan(BaseModel):
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

    @model_validator(mode="after")
    def blocked_work_has_an_exact_gap(self) -> ResearchWorkerReport:
        if self.status == WorkerStatus.BLOCKED and not (self.exact_gap or "").strip():
            raise ValueError("a blocked worker must identify its exact missing statement")
        return self


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
        if strongest:
            existing.strongest_result = strongest
        existing.exact_gap = report.exact_gap or ""
        existing.status = report.status.value
        existing.assumptions = list(dict.fromkeys([*existing.assumptions, *report.assumptions]))
        existing.counterexamples = list(
            dict.fromkeys([*existing.counterexamples, *report.counterexamples])
        )
        existing.dependencies = list(dict.fromkeys([*existing.dependencies, *report.dependencies]))
        existing.assignment_ids = list(dict.fromkeys([*existing.assignment_ids, assignment.id]))


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
    quantitative_or_algorithmic: bool = False

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


class AuditIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: str = "blocking"
    description: str = ""
    repair: str | None = None


class AuditVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: AuditDecision
    issues: list[AuditIssue]
    unresolved_obligations: list[str]
    target_matches: bool


class FinalJudgeVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: FinalJudgeDecision
    reasons: list[str] = Field(default_factory=list)
    unresolved_obligations: list[str] = Field(default_factory=list)
    strongest_result: str = ""


class ResearchAcceptanceGate(BaseModel):
    accepted: bool
    candidate_sha256: str
    claim_contract_sha256: str
    mandatory_audits: list[str]
    final_judge_response_id: str


class ResearchWorkflowSettings(BaseModel):
    minimum_initial_assignments: int = Field(default=4, ge=4)
    maximum_concurrent_agents: int = Field(default=8, ge=1)
    maximum_rounds: int = Field(default=8, ge=1)
    maximum_assignments_per_round: int = Field(default=12, ge=1)
    maximum_model_calls: int | None = Field(default=None, ge=1)
    run_complexity_audit: bool | None = None


class ResearchResult(BaseModel):
    outcome: ResearchOutcome
    rounds: list[ResearchRoundPlan]
    worker_reports: list[ResearchWorkerReport]
    registry: ApproachRegistry
    candidate: CandidateProofPackage | None = None
    audits: dict[str, AuditVerdict] = Field(default_factory=dict)
    final_verdict: FinalJudgeVerdict | None = None
    unresolved_obligations: list[str] = Field(default_factory=list)
    strongest_result: str = ""
    repair_rounds: int = 0
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
    def __init__(self, client: ModelClient, maximum_calls: int | None) -> None:
        self.client = client
        self.maximum_calls = maximum_calls
        self.calls = 0
        self.response_ids: list[str] = []

    def can_call(self, count: int = 1) -> bool:
        return self.maximum_calls is None or self.calls + count <= self.maximum_calls

    async def generate(
        self,
        *,
        instructions: str,
        input_text: str,
        settings: ModelSettings,
        output_type: type[TModel],
    ) -> ModelResult[TModel]:
        if not self.can_call():
            raise _ResearchBudgetExhausted
        # Increment before yielding so concurrent audit/worker calls cannot oversubscribe.
        self.calls += 1
        result = await self.client.generate_structured(
            ModelRequest(
                instructions=instructions,
                input_text=input_text,
                settings=settings,
            ),
            output_type,
        )
        self.response_ids.append(result.response_id)
        return result


class _ResearchBudgetExhausted(Exception):
    pass


def _read_prompt(path: Path | None, resource_name: str) -> str:
    selected = path or project_resource(f"prompts/{resource_name}")
    try:
        return selected.read_text(encoding="utf-8")
    except OSError as exc:
        raise StageValidationError(f"Cannot read stage prompt {selected}: {exc}") from exc


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


async def run_adaptive_research(
    *,
    client: ModelClient,
    compiled_problem: CompiledProblem | PromptCompilationResult,
    research_dir: Path,
    workflow_settings: ResearchWorkflowSettings | None = None,
    coordinator_settings: ModelSettings | None = None,
    worker_settings: ModelSettings | None = None,
    audit_settings: ModelSettings | None = None,
    coordinator_prompt_path: Path | None = None,
    worker_prompt_path: Path | None = None,
    candidate_prompt_path: Path | None = None,
    final_judge_prompt_path: Path | None = None,
    audit_prompt_paths: dict[str, Path] | None = None,
    source_verifier: IdentifierVerifier | None = None,
    progress: ProgressReporter = no_progress,
) -> ResearchResult:
    """Run coordinator-managed, bounded adaptive research and its acceptance gate.

    ``research_dir`` is the final research stage directory; ``registry.json``, ``rounds/``,
    ``candidate/``, ``audits/``, and ``verdict.json`` are written directly beneath it.
    Every model dependency is injected and calls are bounded by ``workflow_settings``.
    """

    compiled = (
        compiled_problem.compiled_problem
        if isinstance(compiled_problem, PromptCompilationResult)
        else compiled_problem
    )
    settings = workflow_settings or ResearchWorkflowSettings()
    coordinator_model = coordinator_settings or ModelSettings(reasoning_effort="max")
    worker_model = worker_settings or ModelSettings(reasoning_effort="max")
    auditor_model = audit_settings or ModelSettings(reasoning_effort="max")
    destination = ensure_stage_directory(research_dir)
    rounds_dir = ensure_stage_directory(destination / "rounds")
    candidate_dir = ensure_stage_directory(destination / "candidate")
    audits_dir = ensure_stage_directory(destination / "audits")

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

    tracker = _TrackedModelClient(client, settings.maximum_model_calls)
    registry = ApproachRegistry()
    all_rounds: list[ResearchRoundPlan] = []
    all_reports: list[ResearchWorkerReport] = []
    current_candidate: CandidateProofPackage | None = None
    current_audits: dict[str, AuditVerdict] = {}
    current_verdict: FinalJudgeVerdict | None = None
    final_judge_response_id = ""
    repair_obligations: list[str] = []
    repair_rounds = 0
    artifact_paths: dict[str, Path] = {}

    async def finish(
        outcome: ResearchOutcome,
        *,
        obligations: list[str] | None = None,
        strongest_result: str = "",
        acceptance_gate: ResearchAcceptanceGate | None = None,
    ) -> ResearchResult:
        artifact_paths["registry"] = atomic_write_json(destination / "registry.json", registry)
        result = ResearchResult(
            outcome=outcome,
            rounds=all_rounds,
            worker_reports=all_reports,
            registry=registry,
            candidate=current_candidate,
            audits=current_audits,
            final_verdict=current_verdict,
            unresolved_obligations=list(dict.fromkeys(obligations or [])),
            strongest_result=strongest_result,
            repair_rounds=repair_rounds,
            acceptance_gate=acceptance_gate,
            artifacts=ArtifactManifest(),
            calls=CallManifest(
                model_calls=tracker.calls,
                response_ids=tracker.response_ids,
            ),
        )
        result.artifacts = build_artifact_manifest(artifact_paths)
        # result.json cannot contain its own stable digest, so it is intentionally excluded
        # from ArtifactManifest and written exactly once after all other hashes are known.
        atomic_write_json(destination / "result.json", result)
        return result

    for round_number in range(1, settings.maximum_rounds + 1):
        if not tracker.can_call():
            return await finish(
                ResearchOutcome.BUDGET_EXHAUSTED,
                obligations=repair_obligations or ["Model-call budget exhausted before planning."],
            )

        progress(Ascension.PLAN_RESEARCH, f"Planning research round {round_number}.")

        if round_number == 1:
            coordinator_input = json.dumps(
                {
                    "compiled_prompt": compiled.compiled_prompt,
                    "claim_contract": compiled.claim_contract.as_dict(),
                    "round_id": round_number,
                    "minimum_materially_diverse_assignments": settings.minimum_initial_assignments,
                    "maximum_assignments": settings.maximum_assignments_per_round,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        else:
            # Later planning is deliberately based on visible reports, registry and audit
            # obligations; no hidden worker scratchpads or confidence scores are supplied.
            coordinator_input = json.dumps(
                {
                    "round_id": round_number,
                    "approach_registry": registry.model_dump(mode="json"),
                    "visible_worker_reports": [
                        report.model_dump(mode="json") for report in all_reports
                    ],
                    "repair_obligations": repair_obligations,
                    "maximum_assignments": settings.maximum_assignments_per_round,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        plan_result = await tracker.generate(
            instructions=coordinator_prompt,
            input_text=coordinator_input,
            settings=coordinator_model,
            output_type=ResearchRoundPlan,
        )

        initial_capacity = settings.minimum_initial_assignments
        budget_limited_initial = False
        if round_number == 1 and tracker.maximum_calls is not None:
            remaining_worker_calls = tracker.maximum_calls - tracker.calls
            if remaining_worker_calls < initial_capacity:
                budget_limited_initial = True
                initial_capacity = max(0, remaining_worker_calls)
        minimum = (
            initial_capacity
            if round_number == 1
            else (0 if plan_result.parsed.stop_recommended else 1)
        )
        plan = _validate_plan(
            plan_result.parsed,
            expected_round=round_number,
            minimum_assignments=minimum,
            maximum_assignments=(
                min(settings.maximum_assignments_per_round, initial_capacity)
                if round_number == 1 and budget_limited_initial
                else settings.maximum_assignments_per_round
            ),
            initial=round_number == 1,
        )
        if (
            round_number == 1
            and budget_limited_initial
            and len(plan.assignments) > initial_capacity
        ):
            plan = plan.model_copy(update={"assignments": plan.assignments[:initial_capacity]})

        all_rounds.append(plan)
        round_dir = ensure_stage_directory(rounds_dir / str(round_number))
        workers_dir = ensure_stage_directory(round_dir / "workers")
        artifact_paths[f"round_{round_number}_plan"] = atomic_write_json(
            round_dir / "plan.json", plan
        )

        if not plan.assignments:
            reason = plan.stop_reason or "Coordinator stopped without a complete candidate."
            outcome = (
                ResearchOutcome.BUDGET_EXHAUSTED
                if "budget" in reason.casefold()
                else ResearchOutcome.PARTIAL
            )
            return await finish(outcome, obligations=repair_obligations or [reason])

        semaphore = asyncio.Semaphore(settings.maximum_concurrent_agents)
        progress(
            Ascension.RUN_RESEARCH,
            f"Launching {len(plan.assignments)} research agents for round {round_number}.",
        )

        async def run_worker(
            assignment: ResearchAssignment,
            *,
            worker_semaphore: asyncio.Semaphore = semaphore,
            worker_round: int = round_number,
            worker_output_dir: Path = workers_dir,
        ) -> ResearchWorkerReport:
            async with worker_semaphore:
                worker_input = json.dumps(
                    {
                        "compiled_prompt": compiled.compiled_prompt,
                        "claim_contract": compiled.claim_contract.as_dict(),
                        "assignment": assignment.model_dump(mode="json"),
                        "round_id": worker_round,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                result = await tracker.generate(
                    instructions=worker_prompt,
                    input_text=worker_input,
                    settings=worker_model,
                    output_type=ResearchWorkerReport,
                )
                if result.parsed.assignment_id != assignment.id:
                    raise StageValidationError(
                        f"Worker report {result.parsed.assignment_id!r} does not match "
                        f"assignment {assignment.id!r}."
                    )
                parsed = result.parsed
                if not isinstance(parsed, ResearchWorkerReport):
                    raise StageValidationError("Model client returned the wrong worker type.")
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
                path = atomic_write_json(worker_output_dir / f"{assignment.id}.json", parsed)
                artifact_paths[f"round_{worker_round}_worker_{assignment.id}"] = path
                artifact_paths[f"round_{worker_round}_worker_{assignment.id}_sources"] = (
                    atomic_write_json(
                        worker_output_dir / "source_verification" / f"{assignment.id}.json",
                        source_verification,
                    )
                )
                return parsed

        async def evaluate_candidate(
            *,
            trigger_report: ResearchWorkerReport | None,
            attempt_name: str,
            candidate_semaphore: asyncio.Semaphore,
        ) -> tuple[ResearchAcceptanceGate | None, list[str], FinalJudgeDecision | None]:
            """Package and fully audit one visible candidate before more research."""

            nonlocal current_candidate, current_audits, current_verdict, final_judge_response_id
            if not tracker.can_call():
                return None, ["Budget exhausted before candidate proof packaging."], None
            progress(
                Ascension.AUDIT_RESEARCH,
                "Packaging the candidate solution for independent audits.",
            )
            visible_reports = [trigger_report] if trigger_report is not None else list(all_reports)
            package_input = json.dumps(
                {
                    "claim_contract": compiled.claim_contract.as_dict(),
                    "approach_registry": registry.model_dump(mode="json"),
                    "visible_worker_reports": [
                        report.model_dump(mode="json") for report in visible_reports
                    ],
                    "candidate_trigger_assignment_id": (
                        trigger_report.assignment_id if trigger_report is not None else None
                    ),
                    "constraint": (
                        "Package the triggering worker's claimed complete proof without "
                        "substituting an unrelated route. Expose every unresolved step."
                        if trigger_report is not None
                        else "Package the strongest complete proof supported by all reports."
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            package_result = await tracker.generate(
                instructions=packager_prompt,
                input_text=package_input,
                settings=worker_model,
                output_type=CandidateProofPackage,
            )
            current_candidate = package_result.parsed
            imported_source_verification = await verify_source_ledger(
                [theorem.as_source_entry() for theorem in current_candidate.imported_theorems],
                provider_identifiers=tool_metadata_source_identifiers(package_result.tool_metadata),
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
            attempt_dir = ensure_stage_directory(candidate_dir / "attempts" / attempt_name)
            artifact_paths[f"candidate_attempt_{attempt_name}"] = atomic_write_json(
                attempt_dir / "package.json", current_candidate
            )
            artifact_paths[f"candidate_attempt_{attempt_name}_proof"] = atomic_write_text(
                attempt_dir / "proof.md", current_candidate.full_proof
            )
            artifact_paths[f"candidate_attempt_{attempt_name}_source_verification"] = (
                atomic_write_json(
                    attempt_dir / "source_verification.json", imported_source_verification
                )
            )
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
                return None, list(current_candidate.unresolved_items), None

            required_audits = list(audit_names)
            run_complexity = (
                settings.run_complexity_audit
                if settings.run_complexity_audit is not None
                else current_candidate.quantitative_or_algorithmic
            )
            if run_complexity:
                required_audits.append("complexity")
            calls_needed = len(required_audits) + 1
            if not tracker.can_call(calls_needed):
                return (
                    None,
                    ["Budget cannot fund all mandatory independent audits and final judge."],
                    None,
                )

            audit_input = json.dumps(
                {
                    "claim_contract": compiled.claim_contract.as_dict(),
                    "candidate_package": current_candidate.model_dump(mode="json"),
                },
                ensure_ascii=False,
                sort_keys=True,
            )

            async def run_audit(name: str) -> tuple[str, AuditVerdict]:
                async with candidate_semaphore:
                    result = await tracker.generate(
                        instructions=audit_instructions[name],
                        input_text=audit_input,
                        settings=auditor_model,
                        output_type=AuditVerdict,
                    )
                    return name, result.parsed

            audit_pairs = await asyncio.gather(*(run_audit(name) for name in required_audits))
            current_audits = dict(audit_pairs)
            audit_attempt_dir = ensure_stage_directory(audits_dir / "attempts" / attempt_name)
            for name, audit in current_audits.items():
                artifact_paths[f"audit_{attempt_name}_{name}"] = atomic_write_json(
                    audit_attempt_dir / f"{name}.json", audit
                )
                artifact_paths[f"audit_{name}"] = atomic_write_json(
                    audits_dir / f"{name}.json", audit
                )

            judge_input = json.dumps(
                {
                    "claim_contract": compiled.claim_contract.as_dict(),
                    "candidate_package": current_candidate.model_dump(mode="json"),
                    "independent_audits": {
                        name: audit.model_dump(mode="json")
                        for name, audit in current_audits.items()
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            judge_result = await tracker.generate(
                instructions=judge_prompt,
                input_text=judge_input,
                settings=auditor_model,
                output_type=FinalJudgeVerdict,
            )
            current_verdict = judge_result.parsed
            final_judge_response_id = judge_result.response_id
            artifact_paths[f"verdict_{attempt_name}"] = atomic_write_json(
                attempt_dir / "verdict.json", current_verdict
            )
            artifact_paths["verdict"] = atomic_write_json(
                destination / "verdict.json", current_verdict
            )

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
            return None, obligations, current_verdict.verdict

        try:
            queued_assignments = list(plan.assignments)
            task_assignments: dict[asyncio.Task[ResearchWorkerReport], ResearchAssignment] = {}
            pending: set[asyncio.Task[ResearchWorkerReport]] = set()

            def launch_available(
                *,
                queue: list[ResearchAssignment] = queued_assignments,
                active: set[asyncio.Task[ResearchWorkerReport]] = pending,
                task_map: dict[
                    asyncio.Task[ResearchWorkerReport], ResearchAssignment
                ] = task_assignments,
                maximum_concurrency: int = settings.maximum_concurrent_agents,
            ) -> None:
                while queue and len(active) < maximum_concurrency:
                    assignment = queue.pop(0)
                    task = asyncio.create_task(run_worker(assignment))
                    task_map[task] = assignment
                    active.add(task)

            launch_available()
            completed_ids: set[str] = set()
            round_reports: list[ResearchWorkerReport] = []
            early_attempted = False

            def record_finished(
                tasks: set[asyncio.Task[ResearchWorkerReport]],
                *,
                round_plan: ResearchRoundPlan = plan,
                task_map: dict[
                    asyncio.Task[ResearchWorkerReport], ResearchAssignment
                ] = task_assignments,
                seen_ids: set[str] = completed_ids,
                collected_reports: list[ResearchWorkerReport] = round_reports,
            ) -> list[ResearchWorkerReport]:
                reports: list[ResearchWorkerReport] = []
                ordered = sorted(
                    tasks, key=lambda task: round_plan.assignments.index(task_map[task])
                )
                for task in ordered:
                    report = task.result()
                    if report.assignment_id in seen_ids:
                        continue
                    assignment = task_map[task]
                    seen_ids.add(report.assignment_id)
                    reports.append(report)
                    collected_reports.append(report)
                    all_reports.append(report)
                    registry.update(assignment, report)
                return reports

            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                newly_finished = record_finished(done)
                trigger = next(
                    (
                        report
                        for report in newly_finished
                        if report.status == WorkerStatus.CANDIDATE_COMPLETE
                    ),
                    None,
                )
                if (
                    trigger is None
                    or early_attempted
                    or budget_limited_initial
                    or not tracker.can_call(6)
                ):
                    launch_available()
                    continue

                early_attempted = True
                interrupted = list(pending)
                for task in interrupted:
                    task.cancel()
                interrupted_results = await asyncio.gather(*interrupted, return_exceptions=True)
                retry_assignments: list[ResearchAssignment] = list(queued_assignments)
                queued_assignments.clear()
                finished_during_stop: set[asyncio.Task[ResearchWorkerReport]] = set()
                for task, result in zip(interrupted, interrupted_results, strict=True):
                    if isinstance(result, ResearchWorkerReport):
                        finished_during_stop.add(task)
                    elif isinstance(result, asyncio.CancelledError):
                        retry_assignments.append(task_assignments[task])
                    elif isinstance(result, BaseException):
                        raise result
                record_finished(finished_during_stop)
                pending.clear()

                gate, early_obligations, _ = await evaluate_candidate(
                    trigger_report=trigger,
                    attempt_name=f"{round_number}-early",
                    candidate_semaphore=semaphore,
                )
                if gate is not None:
                    assert current_candidate is not None
                    artifact_paths["registry"] = atomic_write_json(
                        destination / "registry.json", registry
                    )
                    return await finish(
                        ResearchOutcome.ACCEPTED,
                        strongest_result=(
                            current_verdict.strongest_result
                            if current_verdict is not None
                            else current_candidate.exact_theorem
                        ),
                        acceptance_gate=gate,
                    )

                repair_obligations = early_obligations
                if retry_assignments:
                    retry_tasks = {
                        asyncio.create_task(run_worker(assignment)): assignment
                        for assignment in retry_assignments
                    }
                    task_assignments.update(retry_tasks)
                    retry_done = await asyncio.gather(*retry_tasks, return_exceptions=True)
                    finished_retries: set[asyncio.Task[ResearchWorkerReport]] = set()
                    for task, result in zip(retry_tasks, retry_done, strict=True):
                        if isinstance(result, ResearchWorkerReport):
                            finished_retries.add(task)
                        elif isinstance(result, BaseException):
                            raise result
                    record_finished(finished_retries)
        except _ResearchBudgetExhausted:
            live_tasks = [task for task in task_assignments if not task.done()]
            for task in live_tasks:
                task.cancel()
            await asyncio.gather(*live_tasks, return_exceptions=True)
            return await finish(
                ResearchOutcome.BUDGET_EXHAUSTED,
                obligations=["Model-call budget exhausted during a worker batch."],
            )
        except BaseException:
            live_tasks = [task for task in task_assignments if not task.done()]
            for task in live_tasks:
                task.cancel()
            await asyncio.gather(*live_tasks, return_exceptions=True)
            raise
        artifact_paths["registry"] = atomic_write_json(destination / "registry.json", registry)

        if budget_limited_initial:
            return await finish(
                ResearchOutcome.BUDGET_EXHAUSTED,
                obligations=[
                    "Configured model-call budget could not support the required four-agent "
                    "initial portfolio and downstream acceptance audits."
                ],
            )
        round_has_candidate = any(
            report.status == WorkerStatus.CANDIDATE_COMPLETE for report in round_reports
        )
        if (
            plan.stop_recommended
            and not plan.candidate_packaging_recommended
            and not round_has_candidate
        ):
            reason = plan.stop_reason or "Coordinator recommended a budget-aware stop."
            outcome = (
                ResearchOutcome.BUDGET_EXHAUSTED
                if "budget" in reason.casefold()
                else ResearchOutcome.PARTIAL
            )
            return await finish(outcome, obligations=repair_obligations or [reason])
        if not plan.candidate_packaging_recommended and not round_has_candidate:
            continue
        gate, candidate_obligations, candidate_decision = await evaluate_candidate(
            trigger_report=None,
            attempt_name=str(round_number),
            candidate_semaphore=semaphore,
        )
        if gate is not None:
            assert current_candidate is not None
            assert current_verdict is not None
            return await finish(
                ResearchOutcome.ACCEPTED,
                strongest_result=(
                    current_verdict.strongest_result or current_candidate.exact_theorem
                ),
                acceptance_gate=gate,
            )

        repair_obligations = candidate_obligations
        if candidate_decision is None:
            budget_failure = any("budget" in item.casefold() for item in candidate_obligations)
            if budget_failure:
                return await finish(
                    ResearchOutcome.BUDGET_EXHAUSTED,
                    obligations=candidate_obligations,
                    strongest_result=(
                        current_candidate.exact_theorem if current_candidate is not None else ""
                    ),
                )
            if round_number < settings.maximum_rounds:
                repair_rounds += 1
                continue
            return await finish(
                ResearchOutcome.PARTIAL,
                obligations=candidate_obligations,
                strongest_result=(
                    current_candidate.exact_theorem if current_candidate is not None else ""
                ),
            )

        if candidate_decision == FinalJudgeDecision.REPAIRABLE:
            if not repair_obligations:
                raise StageValidationError(
                    "A repairable final verdict must include at least one exact obligation."
                )
            if round_number < settings.maximum_rounds:
                repair_rounds += 1
                continue
            return await finish(
                ResearchOutcome.PARTIAL,
                obligations=repair_obligations,
                strongest_result=current_verdict.strongest_result if current_verdict else "",
            )

        if candidate_decision in {
            FinalJudgeDecision.REJECTED,
            FinalJudgeDecision.ACCEPTED,
        }:
            return await finish(
                ResearchOutcome.REJECTED,
                obligations=candidate_obligations,
                strongest_result=current_verdict.strongest_result if current_verdict else "",
            )

        return await finish(
            ResearchOutcome.PARTIAL,
            obligations=candidate_obligations,
            strongest_result=current_verdict.strongest_result if current_verdict else "",
        )

    outcome = (
        ResearchOutcome.BUDGET_EXHAUSTED if not tracker.can_call() else ResearchOutcome.PARTIAL
    )
    return await finish(
        outcome,
        obligations=repair_obligations or ["Maximum research rounds reached without acceptance."],
    )
