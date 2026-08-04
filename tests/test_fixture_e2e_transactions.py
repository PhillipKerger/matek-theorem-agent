from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from matek_theorem_agent.config import ModelSettings
from matek_theorem_agent.execution.base import CommandRequest, CommandResult
from matek_theorem_agent.execution.docker import DockerBackend
from matek_theorem_agent.execution.native import NativeBackend
from matek_theorem_agent.knowledge_graph import (
    EpistemicStatus,
    KnowledgeGraph,
    NodeType,
)
from matek_theorem_agent.knowledge_graph.ledger import obligation_logical_version
from matek_theorem_agent.openai_client import ModelRequest, ModelResult
from matek_theorem_agent.scientific import (
    BranchOutcome,
    ScientificArtifactDeclaration,
    ScientificResult,
    ScientificResultDisposition,
    ScientificResultKind,
    ScientificScope,
)
from matek_theorem_agent.stages.common import StageValidationError, atomic_write_json
from matek_theorem_agent.stages.computation_artifacts import (
    ComputationArtifactStore,
    ComputationReplayIsolation,
    ComputationReplayStatus,
    WorkerComputationEvidence,
)
from matek_theorem_agent.stages.lemma_audit import (
    IntermediateResultKind,
    LemmaAuditDecision,
    LemmaAuditGateStatus,
    LemmaAuditResponse,
    LemmaAuditRole,
    LemmaLeverage,
    LemmaNomination,
    LemmaProofStep,
    LemmaScope,
    LemmaTargetObligationReference,
    run_lemma_audit,
    verify_persisted_lemma_audit,
)
from matek_theorem_agent.stages.research import ResearchWorkerReport
from matek_theorem_agent.stages.scientific_phase import (
    DuplicateDisposition,
    ScientificPhase,
    ScientificPhasePolicy,
    ScientificPhaseState,
    ScientificProgressSnapshot,
    ScientificRole,
    ScientificTaskPlan,
    admit_assignment,
    load_scientific_phase_state,
    record_scientific_progress,
    write_scientific_phase_state,
)


def _initialized_graph(tmp_path: Path) -> tuple[KnowledgeGraph, Path, str]:
    project = tmp_path / "project"
    project.mkdir()
    problem = project / "problem.md"
    problem.write_text(
        "Prove that every fixture object has the desired property.\n",
        encoding="utf-8",
    )
    graph = KnowledgeGraph(project, "problem")
    problem_id, _ = graph.initialize_problem(
        source_path=problem,
        problem_text=problem.read_text(encoding="utf-8"),
        run_id="run-one",
    )
    graph.record_compiled_problem(
        problem_id=problem_id,
        run_id="run-one",
        compiled_problem={
            "title": "Fixture theorem",
            "normalized_statement": "Every fixture object has the desired property.",
            "claim_contract": {"domain": "all fixture objects"},
            "compiled_prompt": "Prove the frozen fixture theorem.",
            "literature_status": "unknown",
            "source_ledger": [],
        },
    )
    return graph, problem, problem_id


def test_target_migration_wal_recovers_registry_and_graph_as_one_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph, problem, problem_id = _initialized_graph(tmp_path)
    graph.initialize_problem(
        source_path=problem,
        problem_text=problem.read_text(encoding="utf-8"),
        run_id="run-two",
    )
    source_hash = str(graph.show(problem_id).metadata["matek_normalized_source_sha256"])
    from matek_theorem_agent.knowledge_graph import service as graph_service

    original_atomic_write_json = graph_service.atomic_write_json
    crashed = False

    def crash_after_target_files(path: Path, value: object, **kwargs: object) -> Path:
        nonlocal crashed
        if path == graph.state_path and graph.pending_path.is_file() and not crashed:
            crashed = True
            raise RuntimeError("fixture interruption after target-registry publication")
        return original_atomic_write_json(path, value, **kwargs)

    monkeypatch.setattr(graph_service, "atomic_write_json", crash_after_target_files)
    changed = {
        "title": "Migrated fixture theorem",
        "normalized_statement": ("Every nonempty fixture object has the desired property."),
        "claim_contract": {"domain": "all nonempty fixture objects"},
        "compiled_prompt": "Prove the explicitly migrated fixture theorem.",
        "literature_status": "unknown",
        "source_ledger": [],
    }

    with pytest.raises(RuntimeError, match="fixture interruption"):
        graph.record_compiled_problem(
            problem_id=problem_id,
            run_id="run-two",
            compiled_problem=changed,
            allow_target_migration=True,
            target_migration_reason="Correct the domain after reviewing the frozen target.",
        )

    assert graph.pending_path.is_file()
    interrupted_registry = json.loads(graph.target_registry_path.read_text(encoding="utf-8"))
    assert interrupted_registry["targets"][source_hash]["statement_version"] == 2

    recovered = graph.load_state()
    assert not graph.pending_path.exists()
    assert recovered.processed_operations["prompt-compiled:run-two"].committed
    frozen = graph.frozen_target_for_source(source_hash)
    target = graph.show(graph.main_claim_id(problem_id))
    assert frozen.statement_version == 2
    assert frozen.last_migration_run_id == "run-two"
    assert frozen.migrations[-1].reason.startswith("Correct the domain")
    assert target.statement_version == 2
    assert target.epistemic_status is EpistemicStatus.STALE
    assert "statement_changed_requires_reaudit" in target.invalidation_reasons

    recovered_revision = recovered.revision
    replayed = graph.record_compiled_problem(
        problem_id=problem_id,
        run_id="run-two",
        compiled_problem=changed,
        allow_target_migration=True,
        target_migration_reason="Correct the domain after reviewing the frozen target.",
    )
    assert replayed.status == "already_applied"
    assert graph.load_state().revision == recovered_revision
    assert graph.validate().valid


def test_scientific_phase_checkpoint_resumes_with_epoch_bound_task_contracts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "research" / "coordinator" / "scientific-phase.json"
    policy = ScientificPhasePolicy(no_audited_progress_assignments=2)
    exploratory = ScientificTaskPlan(
        assignment_id="explore-fixture",
        phase=ScientificPhase.EXPLORE,
        phase_epoch=0,
        role=ScientificRole.EXPLORER,
        mechanism="Search for an invariant across the whole domain.",
    )
    state, launched = admit_assignment(
        ScientificPhaseState(),
        exploratory,
        active_cut_ids=["OBL-FIXTURE1"],
        policy=policy,
    )
    assert launched.disposition is DuplicateDisposition.LAUNCH
    state = record_scientific_progress(
        state,
        ScientificProgressSnapshot(
            sequence=1,
            ledger_revision="ledger-after-exploration",
            completed_assignment_count=2,
            minimal_open_cut_ids=["OBL-FIXTURE1"],
        ),
        policy=policy,
    )
    assert state.phase is ScientificPhase.CONSOLIDATE
    assert state.phase_epoch == 1
    write_scientific_phase_state(path, state)
    checkpoint_bytes = path.read_bytes()

    resumed = load_scientific_phase_state(path)
    assert path.read_bytes() == checkpoint_bytes
    assert resumed == state
    with pytest.raises(StageValidationError, match="not active phase"):
        admit_assignment(
            resumed,
            exploratory.model_copy(update={"assignment_id": "stale-explore-fixture"}),
            active_cut_ids=["OBL-FIXTURE1"],
            policy=policy,
        )

    consolidator = ScientificTaskPlan(
        assignment_id="consolidate-fixture",
        phase=ScientificPhase.CONSOLIDATE,
        phase_epoch=resumed.phase_epoch,
        role=ScientificRole.CONSOLIDATOR,
        mechanism="Intersect the surviving invariant statements.",
    )
    resumed, disposition = admit_assignment(
        resumed,
        consolidator,
        active_cut_ids=["OBL-FIXTURE1"],
        policy=policy,
    )
    assert disposition.disposition is DuplicateDisposition.LAUNCH
    resumed = record_scientific_progress(
        resumed,
        ScientificProgressSnapshot(
            sequence=2,
            ledger_revision="ledger-after-consolidation",
            completed_assignment_count=3,
            minimal_open_cut_ids=["OBL-FIXTURE1"],
        ),
        policy=policy,
    )
    assert resumed.phase is ScientificPhase.BOTTLENECK
    assert resumed.phase_epoch == 2
    write_scientific_phase_state(path, resumed)

    second_resume = load_scientific_phase_state(path)
    bottleneck = ScientificTaskPlan(
        assignment_id="bottleneck-fixture",
        phase=ScientificPhase.BOTTLENECK,
        phase_epoch=second_resume.phase_epoch,
        role=ScientificRole.PROVER,
        target_obligation_ids=["OBL-FIXTURE1"],
        mechanism="Prove the exact surviving cut obligation.",
        mechanism_delta="Use a minimal counterexample rather than invariant search.",
    )
    second_resume, disposition = admit_assignment(
        second_resume,
        bottleneck,
        active_cut_ids=["OBL-FIXTURE1"],
        policy=policy,
    )
    assert disposition.disposition is DuplicateDisposition.LAUNCH
    assert [transition.sequence for transition in second_resume.transitions] == [1, 2]
    assert [plan.phase_epoch for plan in second_resume.launched_assignments] == [0, 1, 2]


class _LemmaAuditClient:
    def __init__(self, role: LemmaAuditRole, *, unavailable: bool = False) -> None:
        self.role = role
        self.unavailable = unavailable
        self.requests: list[ModelRequest] = []

    async def generate_structured(
        self,
        request: ModelRequest,
        output_type: type[LemmaAuditResponse],
    ) -> ModelResult[LemmaAuditResponse]:
        assert output_type is LemmaAuditResponse
        self.requests.append(request)
        if self.unavailable:
            raise RuntimeError("offline fixture role unavailable")
        packet = json.loads(request.input_text)["blind_lemma_audit_packet"]
        return ModelResult(
            parsed=LemmaAuditResponse(
                audit_role=self.role,
                audit_id=packet["audit_id"],
                statement_sha256=packet["statement_sha256"],
                decision=LemmaAuditDecision.PASS,
                statement_aligned=True,
                proof_valid=True if self.role is LemmaAuditRole.VERIFIER else None,
                proof_step_ids_checked=[item["step_id"] for item in packet["proof_steps"]],
                source_artifact_ids_checked=[],
                checks_performed=["Checked the frozen statement and complete proof step."],
                boundary_or_adversarial_cases=(
                    ["Checked the empty and one-element boundary cases."]
                    if self.role is LemmaAuditRole.FALSIFIER
                    else []
                ),
                rationale=f"Independent {self.role.value} fixture pass.",
            ),
            response_id=f"fixture-{self.role.value}",
        )


class _MustNotRunLemmaClient(_LemmaAuditClient):
    async def generate_structured(
        self,
        request: ModelRequest,
        output_type: type[LemmaAuditResponse],
    ) -> ModelResult[LemmaAuditResponse]:
        del request, output_type
        raise AssertionError(f"{self.role.value} durable evidence was not reused")


def _lemma_nomination() -> LemmaNomination:
    statement = "Every one-element fixture object has property P."
    obligation_statement = "Every finite fixture object has property P."
    return LemmaNomination(
        nomination_id="lemma-fixture-resume",
        statement_id="CLM-FIXTURE1",
        canonical_derivation_id="DRV-FIXTURE1",
        result_kind=IntermediateResultKind.RESTRICTED_THEOREM,
        scope=LemmaScope.BRANCH,
        exact_statement=statement,
        hypotheses=["The object has exactly one element."],
        main_target_statement="Every finite fixture object has property P.",
        target_obligation_ids=["OBL-FIXTURE1"],
        target_obligation_contracts=[
            LemmaTargetObligationReference(
                obligation_id="OBL-FIXTURE1",
                exact_statement=obligation_statement,
                conclusion=obligation_statement,
                scope=ScientificScope.BRANCH,
                notation_definition_version="1",
                logical_version=obligation_logical_version(
                    obligation_statement,
                    conclusion=obligation_statement,
                    scope=ScientificScope.BRANCH,
                ),
                statement_version=1,
                content_sha256=hashlib.sha256(obligation_statement.encode()).hexdigest(),
            )
        ],
        relevance_statement="Closes the base case in the active induction branch.",
        supports_main_target=True,
        proof_steps=[
            LemmaProofStep(
                step_id="base-case",
                statement=statement,
                justification="Expand the frozen definition on the unique element.",
            )
        ],
        conclusion_step_id="base-case",
        gap_free=True,
        base_graph_revision="00000003-feedfacefeedface",
        current_graph_revision="00000003-feedfacefeedface",
        leverage=LemmaLeverage(
            downstream_obligation_ids=["OBL-FIXTURE1"],
            estimated_open_cut_reduction=1,
            unlocked_branch_count=1,
            rationale="The induction cannot start until this exact base case is closed.",
        ),
        origin_worker_id="hidden-origin-worker",
        origin_confidence="origin-claimed certainty",
        desired_verdict="pass",
    )


@pytest.mark.asyncio
async def test_blind_lemma_resume_calls_only_missing_role_and_reverifies_gate(
    tmp_path: Path,
) -> None:
    nomination = _lemma_nomination()
    destination = tmp_path / "research" / "lemma-audits" / nomination.nomination_id
    atomic_write_json(destination / "nomination.json", nomination)
    verifier = _LemmaAuditClient(LemmaAuditRole.VERIFIER)
    unavailable_falsifier = _LemmaAuditClient(
        LemmaAuditRole.FALSIFIER,
        unavailable=True,
    )

    interrupted = await run_lemma_audit(
        nomination,
        destination,
        verifier_client=verifier,
        falsifier_client=unavailable_falsifier,
        settings=ModelSettings(web_search=False),
    )
    frozen_input = (destination / "input.json").read_bytes()
    frozen_verifier = (destination / "responses" / "lemma-verifier.json").read_bytes()
    assert interrupted.status is LemmaAuditGateStatus.BLOCKED
    assert interrupted.missing_roles == [LemmaAuditRole.FALSIFIER]
    assert len(verifier.requests) == len(unavailable_falsifier.requests) == 1

    resumed_falsifier = _LemmaAuditClient(LemmaAuditRole.FALSIFIER)
    resumed = await run_lemma_audit(
        nomination,
        destination,
        verifier_client=_MustNotRunLemmaClient(LemmaAuditRole.VERIFIER),
        falsifier_client=resumed_falsifier,
        settings=ModelSettings(web_search=False),
    )
    assert resumed.status is LemmaAuditGateStatus.AUDIT_PASSED
    assert resumed.missing_roles == []
    assert not resumed.main_target_acceptance_authorized
    assert not resumed.manuscript_authorized
    assert len(resumed_falsifier.requests) == 1
    assert (destination / "input.json").read_bytes() == frozen_input
    assert (destination / "responses" / "lemma-verifier.json").read_bytes() == frozen_verifier
    assert "hidden-origin-worker" not in frozen_input.decode("utf-8")

    verified_nomination, verified_gate = verify_persisted_lemma_audit(
        destination / "nomination.json",
        destination / "gate.json",
    )
    assert verified_nomination == nomination
    assert verified_gate == resumed


def _computation_declaration() -> ScientificArtifactDeclaration:
    return ScientificArtifactDeclaration(
        path="outputs/certificate.txt",
        purpose="Reproduce the finite fixture enumeration.",
        supporting_result_keys=["finite-case"],
        command_line=["python3", "code/verify.py", "inputs/domain.txt"],
        input_paths=["code/verify.py", "inputs/domain.txt"],
        stdout_path="captures/stdout.txt",
        stderr_path="captures/stderr.txt",
        expected_output="checked\n",
        replay_recipe="Run the frozen verifier over the frozen finite domain.",
        tool_versions=["python 3.11 fixture"],
    )


def _populate_computation_workspace(workspace: Path) -> None:
    for relative, contents in {
        "code/verify.py": b"# deterministic offline fixture verifier\n",
        "inputs/domain.txt": b"fixture objects through size 8\n",
        "outputs/certificate.txt": b"fixture-certificate-v1\n",
        "captures/stdout.txt": b"checked\n",
        "captures/stderr.txt": b"",
    }.items():
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)


class _DockerFixtureHost:
    def __init__(self) -> None:
        self.requests: list[CommandRequest] = []

    async def run(self, request: CommandRequest) -> CommandResult:
        self.requests.append(request)
        assert request.argv[:3] == ("docker", "run", "--rm")
        network_index = request.argv.index("--network")
        assert request.argv[network_index + 1] == "none"
        assert "--pull=never" in request.argv
        (request.cwd / "outputs" / "certificate.txt").write_bytes(b"fixture-certificate-v1\n")
        return CommandResult(
            argv=request.argv,
            cwd=request.cwd,
            exit_code=0,
            stdout="checked\n",
            stderr="",
            duration_seconds=0.01,
        )


def _computation_report(
    assignment_id: str,
    declaration: ScientificArtifactDeclaration,
    target_id: str,
) -> ResearchWorkerReport:
    return ResearchWorkerReport(
        assignment_id=assignment_id,
        results=[
            ScientificResult(
                local_key="finite-case",
                kind=ScientificResultKind.COMPUTATION,
                exact_statement="Every fixture object of size at most 8 has property P.",
                scope=ScientificScope.COMPUTATION,
                proof_or_certificate="The retained exhaustive-enumeration certificate.",
                target_node_ids=[target_id],
                disposition=ScientificResultDisposition.PROPOSED_COMPLETE,
            )
        ],
        artifact_manifest=[declaration],
        branch_outcome=BranchOutcome.PROGRESS,
        mechanism="Exhaustively enumerate the bounded fixture domain.",
    )


@pytest.mark.asyncio
async def test_docker_replay_admits_proposed_support_while_native_remains_experiment(
    tmp_path: Path,
) -> None:
    graph, _, problem_id = _initialized_graph(tmp_path)
    target_id = graph.main_claim_id(problem_id)
    assignments = {
        assignment_id: {
            "id": assignment_id,
            "approach_family": "finite-enumeration",
            "task": f"Replay the {assignment_id} finite certificate.",
            "expected_output": "A confined deterministic replay verdict.",
            "target_node_ids": [target_id],
        }
        for assignment_id in ("native-worker", "docker-worker")
    }
    task_ids, _, _ = graph.record_assignment_tasks(
        problem_id=problem_id,
        run_id="run-one",
        decision_id=7,
        assignments=list(assignments.values()),
    )
    run_root = graph.project_root / ".matek" / "runs" / "run-one"
    run_root.mkdir(parents=True, exist_ok=True)
    store = ComputationArtifactStore(run_root)
    declaration = _computation_declaration()

    native_workspace = store.prepare_workspace("native-worker")
    _populate_computation_workspace(native_workspace)
    native_collection = store.collect("native-worker", [declaration])
    native_replay = await store.replay(
        "native-worker",
        NativeBackend(),
        isolation=ComputationReplayIsolation(
            filesystem_write_confined=False,
            network_disabled=False,
            description="Native execution has no independent confinement attestation.",
        ),
    )
    assert native_replay.status is ComputationReplayStatus.UNSAFE_BACKEND
    native_evidence = WorkerComputationEvidence(
        assignment_id="native-worker",
        collection=native_collection,
        replay=native_replay,
    )
    native_evidence_path = run_root / "research" / "worker-computation" / "native-worker.json"
    atomic_write_json(native_evidence_path, native_evidence)
    native_report = _computation_report("native-worker", declaration, target_id)
    graph.integrate_worker_report(
        problem_id=problem_id,
        run_id="run-one",
        assignment=assignments["native-worker"],
        task_id=task_ids["native-worker"],
        report=native_report.model_dump(mode="json"),
        proposed_patch=None,
        source_artifact=".matek/runs/run-one/research/workers/native-worker.json",
        operation_id="fixture-computation:native-worker",
        computation_evidence={
            **native_evidence.model_dump(mode="json"),
            "source_artifact": (
                ".matek/runs/run-one/research/worker-computation/native-worker.json"
            ),
        },
    )

    docker_workspace = store.prepare_workspace("docker-worker")
    _populate_computation_workspace(docker_workspace)
    docker_collection = store.collect("docker-worker", [declaration])
    docker_host = _DockerFixtureHost()
    docker_replay = await store.replay(
        "docker-worker",
        DockerBackend("matek-fixture:test", native_backend=docker_host),  # type: ignore[arg-type]
        isolation=ComputationReplayIsolation(
            filesystem_write_confined=True,
            network_disabled=True,
            description="Restricted Docker fixture replay with networking disabled.",
        ),
    )
    assert docker_replay.status is ComputationReplayStatus.PASSED
    assert docker_replay.trusted
    assert len(docker_host.requests) == 1
    docker_evidence = WorkerComputationEvidence(
        assignment_id="docker-worker",
        collection=docker_collection,
        replay=docker_replay,
    )
    docker_evidence_path = run_root / "research" / "worker-computation" / "docker-worker.json"
    atomic_write_json(docker_evidence_path, docker_evidence)
    docker_report = _computation_report("docker-worker", declaration, target_id)
    graph.integrate_worker_report(
        problem_id=problem_id,
        run_id="run-one",
        assignment=assignments["docker-worker"],
        task_id=task_ids["docker-worker"],
        report=docker_report.model_dump(mode="json"),
        proposed_patch=None,
        source_artifact=".matek/runs/run-one/research/workers/docker-worker.json",
        operation_id="fixture-computation:docker-worker",
        computation_evidence={
            **docker_evidence.model_dump(mode="json"),
            "source_artifact": (
                ".matek/runs/run-one/research/worker-computation/docker-worker.json"
            ),
        },
    )

    nodes = graph.load_nodes()
    native_nodes = [
        node for node in nodes if node.metadata.get("matek_assignment_id") == "native-worker"
    ]
    docker_nodes = [
        node for node in nodes if node.metadata.get("matek_assignment_id") == "docker-worker"
    ]
    assert any(node.node_type is NodeType.EXPERIMENT for node in native_nodes)
    assert not any(node.node_type is NodeType.DERIVATION for node in native_nodes)
    assert any(
        node.node_type is NodeType.ARTIFACT
        and node.metadata.get("matek_computation_replay_status") == "unsafe_backend"
        for node in native_nodes
    )
    docker_derivation = next(node for node in docker_nodes if node.node_type is NodeType.DERIVATION)
    assert docker_derivation.epistemic_status is EpistemicStatus.CANDIDATE
    docker_artifacts = [node for node in docker_nodes if node.node_type is NodeType.ARTIFACT]
    assert len(docker_artifacts) == 2
    assert all(node.epistemic_status is EpistemicStatus.AUDIT_PASSED for node in docker_artifacts)
    assert graph.validate().valid
