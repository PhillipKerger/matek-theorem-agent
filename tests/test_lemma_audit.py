from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from matek_theorem_agent.accounting import AccountingModelClient
from matek_theorem_agent.budget import BudgetTracker
from matek_theorem_agent.config import Limits, ModelSettings
from matek_theorem_agent.knowledge_graph.ledger import obligation_logical_version
from matek_theorem_agent.logging import RunLogger
from matek_theorem_agent.openai_client import (
    ModelRequest,
    ModelResult,
    model_request_cache_key,
)
from matek_theorem_agent.resources import read_resource_text
from matek_theorem_agent.scientific import ScientificScope
from matek_theorem_agent.stages.common import StageValidationError, atomic_write_json
from matek_theorem_agent.stages.lemma_audit import (
    IntermediateResultKind,
    LemmaAuditDecision,
    LemmaAuditGateStatus,
    LemmaAuditResponse,
    LemmaAuditRole,
    LemmaDependencyReference,
    LemmaFalsificationFinding,
    LemmaLeverage,
    LemmaNomination,
    LemmaNominationRejected,
    LemmaPreflightCode,
    LemmaProofStep,
    LemmaScope,
    LemmaSourceArtifact,
    LemmaTargetObligationReference,
    LocalLemmaAuditFileSystem,
    preflight_lemma_nomination,
    run_lemma_audit,
    verify_persisted_lemma_audit,
)


class AdvancingClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 4, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.value += timedelta(seconds=1)
        return self.value


class RecordingFileSystem(LocalLemmaAuditFileSystem):
    def __init__(self) -> None:
        self.immutable_writes: list[Path] = []
        self.atomic_writes: list[Path] = []

    def write_atomic_bytes(self, path: Path, contents: bytes) -> Path:
        self.atomic_writes.append(path)
        return super().write_atomic_bytes(path, contents)

    def write_immutable_bytes(self, path: Path, contents: bytes) -> Path:
        existed = self.artifact_exists(path)
        result = super().write_immutable_bytes(path, contents)
        if not existed:
            self.immutable_writes.append(path)
        return result


class FakeAuditClient:
    def __init__(
        self,
        role: LemmaAuditRole,
        *,
        decision: LemmaAuditDecision = LemmaAuditDecision.PASS,
        response_id: str | None = None,
        error: BaseException | None = None,
        omit_last_source: bool = False,
        provider_session_id: str | None = None,
    ) -> None:
        self.role = role
        self.decision = decision
        self.response_id = response_id or f"resp-{role.value}"
        self.error = error
        self.omit_last_source = omit_last_source
        self.provider_session_id = provider_session_id
        self.requests: list[ModelRequest] = []

    async def generate_structured(
        self,
        request: ModelRequest,
        output_type: type[LemmaAuditResponse],
    ) -> ModelResult[LemmaAuditResponse]:
        assert output_type is LemmaAuditResponse
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        payload = json.loads(request.input_text)
        packet = payload["blind_lemma_audit_packet"]
        assert payload["audit_role"] == self.role.value
        findings: list[LemmaFalsificationFinding] = []
        obligations: list[str] = []
        counterexample_found = False
        proof_valid: bool | None = True if self.role is LemmaAuditRole.VERIFIER else None
        boundary_cases: list[str] = []
        if self.role is LemmaAuditRole.FALSIFIER:
            boundary_cases = ["n = 0", "n = 1", "largest finite boundary allowed by scope"]
        if self.decision is LemmaAuditDecision.FAIL:
            obligations = ["Restrict the statement to n > 0 or repair the zero case."]
            if self.role is LemmaAuditRole.FALSIFIER:
                counterexample_found = True
                findings = [
                    LemmaFalsificationFinding(
                        case_description="Zero boundary case",
                        concrete_instance="n = 0",
                        observed_failure="The claimed strict positivity conclusion becomes 0 > 0.",
                    )
                ]
            else:
                proof_valid = False
        elif self.decision is LemmaAuditDecision.BLOCKED:
            obligations = ["Provide the exact cited source theorem statement."]
            proof_valid = None
        response = LemmaAuditResponse(
            audit_role=self.role,
            audit_id=packet["audit_id"],
            statement_sha256=packet["statement_sha256"],
            decision=self.decision,
            statement_aligned=True,
            proof_valid=proof_valid,
            counterexample_found=counterexample_found,
            proof_step_ids_checked=[item["step_id"] for item in packet["proof_steps"]],
            source_artifact_ids_checked=(
                [item["artifact_id"] for item in packet["source_artifacts"]][:-1]
                if self.omit_last_source
                else [item["artifact_id"] for item in packet["source_artifacts"]]
            ),
            checks_performed=[
                "Checked the exact scoped statement and hypotheses.",
                "Checked every derivation edge and cited source artifact.",
            ],
            boundary_or_adversarial_cases=boundary_cases,
            rationale=f"Independent {self.role.value} assessment.",
            obligations=obligations,
            falsification_evidence=findings,
        )
        return ModelResult(
            parsed=response,
            response_id=self.response_id,
            request_metadata=(
                {"session_id": self.provider_session_id}
                if self.provider_session_id is not None
                else {}
            ),
        )


class MustNotRunClient(FakeAuditClient):
    async def generate_structured(
        self,
        request: ModelRequest,
        output_type: type[LemmaAuditResponse],
    ) -> ModelResult[LemmaAuditResponse]:
        del request, output_type
        raise AssertionError(f"{self.role.value} should have resumed from durable evidence")


def valid_nomination() -> LemmaNomination:
    source_content = "Peano recursion gives n + 0 = n for every natural number n.\n"
    dependency_statement = "Natural-number addition satisfies the zero recursion equation."
    dependency_digest = hashlib.sha256(dependency_statement.encode()).hexdigest()
    obligation_statement = "Prove the target recurrence for every natural number."
    obligation_falsification = ["The n = 0 boundary was previously unresolved."]
    obligation_version = obligation_logical_version(
        obligation_statement,
        conclusion=obligation_statement,
        scope=ScientificScope.BRANCH,
        falsification_evidence=obligation_falsification,
    )
    return LemmaNomination(
        nomination_id="lemma-zero-identity",
        statement_id="CLM-ZERO-IDENTITY",
        canonical_derivation_id="DRV-ZERO-IDENTITY",
        result_kind=IntermediateResultKind.RESTRICTED_THEOREM,
        scope=LemmaScope.BRANCH,
        exact_statement="For every natural number n, n + 0 = n.",
        hypotheses=["n is a natural number."],
        main_target_statement="Prove the target recurrence for every natural number.",
        target_obligation_ids=["OBL-MAIN-RECURRENCE"],
        target_obligation_contracts=[
            LemmaTargetObligationReference(
                obligation_id="OBL-MAIN-RECURRENCE",
                exact_statement=obligation_statement,
                conclusion=obligation_statement,
                scope=ScientificScope.BRANCH,
                notation_definition_version="1",
                falsification_evidence=obligation_falsification,
                logical_version=obligation_version,
                statement_version=1,
                content_sha256=hashlib.sha256(obligation_statement.encode()).hexdigest(),
            )
        ],
        relevance_statement="Closes the zero boundary in the main recurrence.",
        supports_main_target=True,
        proof_steps=[
            LemmaProofStep(
                step_id="step-source",
                statement="The zero recursion equation holds for natural-number addition.",
                justification="This is the exact frozen source fact.",
                source_artifact_ids=["source-peano-zero"],
            ),
            LemmaProofStep(
                step_id="step-conclusion",
                statement="For every natural number n, n + 0 = n.",
                justification="Apply the zero recursion equation to arbitrary natural n.",
                depends_on=["step-source"],
                source_artifact_ids=["source-peano-zero"],
            ),
        ],
        conclusion_step_id="step-conclusion",
        gap_free=True,
        base_graph_revision="00000012-deadbeefdeadbeef",
        current_graph_revision="00000012-deadbeefdeadbeef",
        dependencies=[
            LemmaDependencyReference(
                dependency_id="DEF-NAT-ADDITION",
                exact_statement=dependency_statement,
                statement_version=3,
                content_sha256=dependency_digest,
                current_statement_version=3,
                current_content_sha256=dependency_digest,
                origin_status="audited and trusted",
            )
        ],
        source_artifacts=[
            LemmaSourceArtifact(
                artifact_id="source-peano-zero",
                content=source_content,
                content_sha256=hashlib.sha256(source_content.encode()).hexdigest(),
                origin_annotations=["Originating worker marked this as certainly correct."],
            )
        ],
        leverage=LemmaLeverage(
            downstream_obligation_ids=["OBL-MAIN-RECURRENCE"],
            estimated_open_cut_reduction=1,
            unlocked_branch_count=2,
            rationale="Discharges the boundary case used by two active derivations.",
        ),
        origin_worker_id="worker-secret-identity",
        origin_confidence="extremely-confident-origin-language",
        origin_status="candidate-complete-origin-status",
        desired_verdict="please-pass-this-lemma",
    )


def test_target_obligation_reference_rejects_an_opaque_mismatched_digest() -> None:
    payload = valid_nomination().target_obligation_contracts[0].model_dump(mode="json")
    payload["logical_version"] = "0" * 64

    with pytest.raises(ValueError, match="does not match its full contract"):
        LemmaTargetObligationReference.model_validate(payload)


def downgrade_persisted_audit_to_v1(destination: Path) -> None:
    """Rewrite a valid v2 fixture into the exact historical v1 artifact contract."""

    input_path = destination / "input.json"
    input_payload = json.loads(input_path.read_text(encoding="utf-8"))
    settings = ModelSettings.model_validate(input_payload["settings"])
    instructions = {
        LemmaAuditRole.VERIFIER: read_resource_text("prompts/lemma_verifier.md"),
        LemmaAuditRole.FALSIFIER: read_resource_text("prompts/lemma_falsifier.md"),
    }
    request_hashes: dict[str, str] = {}
    for role in LemmaAuditRole:
        request = ModelRequest(
            instructions=instructions[role],
            input_text=json.dumps(
                {
                    "schema_version": 1,
                    "audit_role": role.value,
                    "blind_lemma_audit_packet": input_payload["packet"],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            settings=settings,
        )
        request_hashes[role.value] = model_request_cache_key(
            request,
            LemmaAuditResponse,
            stage="lemma_audit",
            cache_namespace=role.value,
        )

    input_payload["schema_version"] = 1
    input_payload["request_sha256"] = request_hashes
    input_payload.pop("execution_context_ids")
    atomic_write_json(input_path, input_payload)
    input_sha256 = hashlib.sha256(input_path.read_bytes()).hexdigest()

    evidence_sha256: dict[str, str] = {}
    for role in LemmaAuditRole:
        response_path = destination / "responses" / f"{role.value}.json"
        evidence_payload = json.loads(response_path.read_text(encoding="utf-8"))
        evidence_payload["schema_version"] = 1
        evidence_payload["input_sha256"] = input_sha256
        evidence_payload["request_sha256"] = request_hashes[role.value]
        evidence_payload.pop("execution_context_id")
        evidence_payload.pop("provider_session_id")
        atomic_write_json(response_path, evidence_payload)
        evidence_sha256[role.value] = hashlib.sha256(response_path.read_bytes()).hexdigest()

    gate_path = destination / "gate.json"
    gate_payload = json.loads(gate_path.read_text(encoding="utf-8"))
    gate_payload["schema_version"] = 1
    gate_payload["input_sha256"] = input_sha256
    gate_payload["response_sha256"] = evidence_sha256
    gate_payload.pop("execution_context_ids")
    gate_payload.pop("provider_session_ids")
    accepted = gate_payload["accepted_intermediate"]
    accepted["verifier_evidence_sha256"] = evidence_sha256[LemmaAuditRole.VERIFIER.value]
    accepted["falsifier_evidence_sha256"] = evidence_sha256[LemmaAuditRole.FALSIFIER.value]
    atomic_write_json(gate_path, gate_payload)


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        (
            lambda item: item.model_copy(update={"gap_free": False}),
            LemmaPreflightCode.GAPPED,
        ),
        (
            lambda item: item.model_copy(update={"base_graph_revision": "stale-revision"}),
            LemmaPreflightCode.STALE,
        ),
        (
            lambda item: item.model_copy(update={"ambiguity_flags": ["quantifier unclear"]}),
            LemmaPreflightCode.AMBIGUOUS,
        ),
        (
            lambda item: item.model_copy(update={"supports_main_target": False}),
            LemmaPreflightCode.IRRELEVANT,
        ),
        (
            lambda item: item.model_copy(
                update={
                    "leverage": item.leverage.model_copy(
                        update={"estimated_open_cut_reduction": 0, "unlocked_branch_count": 0}
                    )
                }
            ),
            LemmaPreflightCode.LOW_LEVERAGE,
        ),
    ],
)
@pytest.mark.asyncio
async def test_preflight_rejects_unfit_nominations_before_io_or_model_calls(
    tmp_path: Path,
    mutator: Any,
    expected_code: LemmaPreflightCode,
) -> None:
    nomination = mutator(valid_nomination())
    report = preflight_lemma_nomination(nomination)
    verifier = FakeAuditClient(LemmaAuditRole.VERIFIER)
    falsifier = FakeAuditClient(LemmaAuditRole.FALSIFIER)
    destination = tmp_path / "audit"

    assert not report.accepted
    assert expected_code in {issue.code for issue in report.issues}
    with pytest.raises(LemmaNominationRejected) as rejected:
        await run_lemma_audit(
            nomination,
            destination,
            verifier_client=verifier,
            falsifier_client=falsifier,
            settings=ModelSettings(web_search=False),
        )

    assert expected_code in {issue.code for issue in rejected.value.report.issues}
    assert not destination.exists()
    assert verifier.requests == []
    assert falsifier.requests == []


@pytest.mark.asyncio
async def test_two_blind_independent_passes_accept_only_an_intermediate_theorem(
    tmp_path: Path,
) -> None:
    nomination = valid_nomination()
    verifier = FakeAuditClient(
        LemmaAuditRole.VERIFIER,
        provider_session_id="provider-session-verifier",
    )
    falsifier = FakeAuditClient(
        LemmaAuditRole.FALSIFIER,
        provider_session_id="provider-session-falsifier",
    )
    filesystem = RecordingFileSystem()
    destination = tmp_path / "audit"

    gate = await run_lemma_audit(
        nomination,
        destination,
        verifier_client=verifier,
        falsifier_client=falsifier,
        settings=ModelSettings(web_search=False),
        clock=AdvancingClock(),
        filesystem=filesystem,
    )

    assert gate.status is LemmaAuditGateStatus.AUDIT_PASSED
    assert gate.accepted_intermediate is not None
    assert gate.accepted_intermediate.result_kind is IntermediateResultKind.RESTRICTED_THEOREM
    assert gate.accepted_intermediate.terminal_main_target_satisfied is False
    assert gate.accepted_intermediate.manuscript_authorized is False
    assert gate.main_target_acceptance_authorized is False
    assert gate.manuscript_authorized is False
    assert len(verifier.requests) == len(falsifier.requests) == 1
    assert verifier.requests[0].input_text != falsifier.requests[0].input_text
    request_payloads = [
        json.loads(verifier.requests[0].input_text),
        json.loads(falsifier.requests[0].input_text),
    ]
    request_contexts = {
        payload["audit_role"]: payload["execution_context"]["context_id"]
        for payload in request_payloads
    }
    assert len(set(request_contexts.values())) == 2
    assert gate.schema_version == 2
    assert gate.execution_context_ids == request_contexts
    assert gate.provider_session_ids == {
        LemmaAuditRole.VERIFIER.value: "provider-session-verifier",
        LemmaAuditRole.FALSIFIER.value: "provider-session-falsifier",
    }

    input_bytes = (destination / "input.json").read_bytes()
    serialized = input_bytes.decode()
    for forbidden in (
        "origin_worker_id",
        "origin_confidence",
        "origin_status",
        "desired_verdict",
        "worker-secret-identity",
        "extremely-confident-origin-language",
        "candidate-complete-origin-status",
        "please-pass-this-lemma",
        "Originating worker marked this as certainly correct.",
    ):
        assert forbidden not in serialized
        assert forbidden not in verifier.requests[0].input_text
        assert forbidden not in falsifier.requests[0].input_text
    assert nomination.exact_statement in serialized
    assert nomination.source_artifacts[0].content.strip() in serialized
    for payload in request_payloads:
        assert payload["blind_lemma_audit_packet"]["target_obligation_contracts"][0][
            "falsification_evidence"
        ] == ["The n = 0 boundary was previously unresolved."]
    assert destination / "input.json" in filesystem.immutable_writes
    assert destination / "responses" / "lemma-verifier.json" in filesystem.immutable_writes
    assert destination / "responses" / "lemma-falsifier.json" in filesystem.immutable_writes
    for role, digest in gate.response_sha256.items():
        assert (
            hashlib.sha256((destination / "responses" / f"{role}.json").read_bytes()).hexdigest()
            == digest
        )


@pytest.mark.asyncio
async def test_persisted_lemma_gate_is_recomputed_from_immutable_role_evidence(
    tmp_path: Path,
) -> None:
    nomination = valid_nomination()
    destination = tmp_path / nomination.nomination_id
    atomic_write_json(destination / "nomination.json", nomination)
    gate = await run_lemma_audit(
        nomination,
        destination,
        verifier_client=FakeAuditClient(LemmaAuditRole.VERIFIER),
        falsifier_client=FakeAuditClient(LemmaAuditRole.FALSIFIER),
        settings=ModelSettings(web_search=False),
        clock=AdvancingClock(),
    )

    verified_nomination, verified_gate = verify_persisted_lemma_audit(
        destination / "nomination.json",
        destination / "gate.json",
    )
    assert verified_nomination == nomination
    assert verified_gate == gate

    forged_gate = gate.model_dump(mode="json")
    forged_gate["response_ids"][LemmaAuditRole.VERIFIER.value] = "forged-response-id"
    atomic_write_json(destination / "gate.json", forged_gate)
    with pytest.raises(StageValidationError, match="response identities changed"):
        verify_persisted_lemma_audit(
            destination / "nomination.json",
            destination / "gate.json",
        )


@pytest.mark.asyncio
async def test_shared_provider_session_fails_independence_gate(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    (run_root / "logs").mkdir(parents=True)
    budget = BudgetTracker(Limits(maximum_cost_usd=1.0))
    logger = RunLogger(run_root)
    verifier = AccountingModelClient(
        FakeAuditClient(
            LemmaAuditRole.VERIFIER,
            provider_session_id="shared-provider-session",
        ),
        stage="lemma_audit",
        budget=budget,
        logger=logger,
    )
    falsifier = AccountingModelClient(
        FakeAuditClient(
            LemmaAuditRole.FALSIFIER,
            provider_session_id="shared-provider-session",
        ),
        stage="lemma_audit",
        budget=budget,
        logger=logger,
    )
    gate = await run_lemma_audit(
        valid_nomination(),
        tmp_path / "audit",
        verifier_client=verifier,
        falsifier_client=falsifier,
        settings=ModelSettings(web_search=False),
        clock=AdvancingClock(),
    )

    assert gate.status is LemmaAuditGateStatus.AUDIT_FAILED
    assert gate.accepted_intermediate is None
    assert any("distinct provider sessions" in item for item in gate.obligations)
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (run_root / "logs" / "model_calls").glob("*.json")
    ]
    assert len(records) == 2
    assert {record["provider_session_id"] for record in records} == {"shared-provider-session"}


@pytest.mark.asyncio
async def test_secret_like_provider_session_is_not_persisted(tmp_path: Path) -> None:
    destination = tmp_path / "audit"
    secret_session = "sk-proj-super-secret-provider-session-token"
    gate = await run_lemma_audit(
        valid_nomination(),
        destination,
        verifier_client=FakeAuditClient(
            LemmaAuditRole.VERIFIER,
            provider_session_id=secret_session,
        ),
        falsifier_client=FakeAuditClient(LemmaAuditRole.FALSIFIER),
        settings=ModelSettings(web_search=False),
        clock=AdvancingClock(),
    )

    assert gate.status is LemmaAuditGateStatus.BLOCKED
    assert LemmaAuditRole.VERIFIER in gate.missing_roles
    assert not (destination / "responses" / "lemma-verifier.json").exists()
    assert secret_session not in "".join(
        path.read_text(encoding="utf-8") for path in destination.rglob("*.json")
    )


@pytest.mark.asyncio
async def test_v1_passing_gate_is_refused_then_archived_and_reaudited_as_v2(
    tmp_path: Path,
) -> None:
    nomination = valid_nomination()
    destination = tmp_path / nomination.nomination_id
    atomic_write_json(destination / "nomination.json", nomination)
    await run_lemma_audit(
        nomination,
        destination,
        verifier_client=FakeAuditClient(LemmaAuditRole.VERIFIER),
        falsifier_client=FakeAuditClient(LemmaAuditRole.FALSIFIER),
        settings=ModelSettings(web_search=False),
        clock=AdvancingClock(),
    )
    downgrade_persisted_audit_to_v1(destination)

    with pytest.raises(StageValidationError, match="cannot establish independent sessions"):
        verify_persisted_lemma_audit(
            destination / "nomination.json",
            destination / "gate.json",
        )

    verifier = FakeAuditClient(
        LemmaAuditRole.VERIFIER,
        provider_session_id="fresh-verifier-session",
    )
    falsifier = FakeAuditClient(
        LemmaAuditRole.FALSIFIER,
        provider_session_id="fresh-falsifier-session",
    )
    upgraded = await run_lemma_audit(
        nomination,
        destination,
        verifier_client=verifier,
        falsifier_client=falsifier,
        settings=ModelSettings(web_search=False),
        clock=AdvancingClock(),
    )

    assert upgraded.schema_version == 2
    assert upgraded.status is LemmaAuditGateStatus.AUDIT_PASSED
    assert len(verifier.requests) == len(falsifier.requests) == 1
    archive = destination / "legacy-v1"
    assert json.loads((archive / "input.json").read_text(encoding="utf-8"))["schema_version"] == 1
    assert json.loads((archive / "gate.json").read_text(encoding="utf-8"))["schema_version"] == 1
    manifest = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["file_sha256"]) == {
        "input.json",
        "gate.json",
        "responses/lemma-verifier.json",
        "responses/lemma-falsifier.json",
    }
    assert (
        verify_persisted_lemma_audit(
            destination / "nomination.json",
            destination / "gate.json",
        )[1]
        == upgraded
    )


@pytest.mark.asyncio
async def test_falsification_evidence_fails_the_intermediate_gate(tmp_path: Path) -> None:
    gate = await run_lemma_audit(
        valid_nomination(),
        tmp_path / "audit",
        verifier_client=FakeAuditClient(LemmaAuditRole.VERIFIER),
        falsifier_client=FakeAuditClient(
            LemmaAuditRole.FALSIFIER,
            decision=LemmaAuditDecision.FAIL,
        ),
        settings=ModelSettings(web_search=False),
        clock=AdvancingClock(),
    )

    assert gate.status is LemmaAuditGateStatus.AUDIT_FAILED
    assert gate.accepted_intermediate is None
    assert gate.falsification_evidence[0].concrete_instance == "n = 0"
    assert "Restrict the statement" in gate.obligations[0]
    assert not gate.main_target_acceptance_authorized
    assert not gate.manuscript_authorized


@pytest.mark.asyncio
async def test_pass_claim_without_complete_source_coverage_fails_closed(tmp_path: Path) -> None:
    gate = await run_lemma_audit(
        valid_nomination(),
        tmp_path / "audit",
        verifier_client=FakeAuditClient(
            LemmaAuditRole.VERIFIER,
            omit_last_source=True,
        ),
        falsifier_client=FakeAuditClient(LemmaAuditRole.FALSIFIER),
        settings=ModelSettings(web_search=False),
        clock=AdvancingClock(),
    )

    assert gate.status is LemmaAuditGateStatus.AUDIT_FAILED
    assert gate.accepted_intermediate is None
    assert any("Audit every exact source artifact" in item for item in gate.obligations)


@pytest.mark.asyncio
async def test_resume_runs_only_the_missing_audit_role(tmp_path: Path) -> None:
    destination = tmp_path / "audit"
    nomination = valid_nomination()
    verifier = FakeAuditClient(LemmaAuditRole.VERIFIER)
    unavailable_falsifier = FakeAuditClient(
        LemmaAuditRole.FALSIFIER,
        error=RuntimeError("temporary offline fixture outage"),
    )
    first = await run_lemma_audit(
        nomination,
        destination,
        verifier_client=verifier,
        falsifier_client=unavailable_falsifier,
        settings=ModelSettings(web_search=False),
        clock=AdvancingClock(),
    )
    input_before = (destination / "input.json").read_bytes()
    verifier_before = (destination / "responses" / "lemma-verifier.json").read_bytes()

    assert first.status is LemmaAuditGateStatus.BLOCKED
    assert first.missing_roles == [LemmaAuditRole.FALSIFIER]
    assert "Retry the missing independent lemma-falsifier" in first.obligations[0]
    assert len(verifier.requests) == 1
    assert len(unavailable_falsifier.requests) == 1

    skipped_verifier = MustNotRunClient(LemmaAuditRole.VERIFIER)
    resumed_falsifier = FakeAuditClient(LemmaAuditRole.FALSIFIER)
    resumed = await run_lemma_audit(
        nomination,
        destination,
        verifier_client=skipped_verifier,
        falsifier_client=resumed_falsifier,
        settings=ModelSettings(web_search=False),
        clock=AdvancingClock(),
    )

    assert resumed.status is LemmaAuditGateStatus.AUDIT_PASSED
    assert skipped_verifier.requests == []
    assert len(resumed_falsifier.requests) == 1
    assert (destination / "input.json").read_bytes() == input_before
    assert (destination / "responses" / "lemma-verifier.json").read_bytes() == verifier_before

    replayed = await run_lemma_audit(
        nomination,
        destination,
        verifier_client=skipped_verifier,
        falsifier_client=MustNotRunClient(LemmaAuditRole.FALSIFIER),
        settings=ModelSettings(web_search=False),
        clock=AdvancingClock(),
    )
    assert replayed == resumed


@pytest.mark.asyncio
async def test_changed_committed_response_blocks_resume_before_model_calls(tmp_path: Path) -> None:
    destination = tmp_path / "audit"
    nomination = valid_nomination()
    await run_lemma_audit(
        nomination,
        destination,
        verifier_client=FakeAuditClient(LemmaAuditRole.VERIFIER),
        falsifier_client=FakeAuditClient(LemmaAuditRole.FALSIFIER),
        settings=ModelSettings(web_search=False),
        clock=AdvancingClock(),
    )
    (destination / "responses" / "lemma-verifier.json").write_text("{}\n", encoding="utf-8")
    verifier = FakeAuditClient(LemmaAuditRole.VERIFIER)
    falsifier = FakeAuditClient(LemmaAuditRole.FALSIFIER)

    with pytest.raises(StageValidationError, match="artifact is invalid"):
        await run_lemma_audit(
            nomination,
            destination,
            verifier_client=verifier,
            falsifier_client=falsifier,
            settings=ModelSettings(web_search=False),
            clock=AdvancingClock(),
        )
    assert verifier.requests == []
    assert falsifier.requests == []
