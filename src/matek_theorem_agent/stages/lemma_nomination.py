"""Deterministic nominations for the independent intermediate-lemma audit lane.

This module is an adapter between an already admitted scientific worker report and
``run_lemma_audit``.  It never infers a graph identity from prose.  A result is eligible
only when its application-owned admission metadata identifies exactly one derivation,
that derivation identifies exactly one canonical claim and proof attempt, and an
explicit target is a member of the current, uncapped smallest open cut.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..knowledge_graph.admission import (
    admission_payload_sha256,
    canonical_admitted_definition_scope,
    node_has_scientific_admission_binding,
)
from ..knowledge_graph.ledger import (
    project_markdown_ledger,
    trusted_claim_ids,
)
from ..knowledge_graph.markdown import exact_statement
from ..knowledge_graph.models import (
    EpistemicStatus,
    GraphFrontier,
    GraphNode,
    NodeType,
    RelationType,
    WorkflowStatus,
)
from ..scientific import (
    ScientificResult,
    ScientificResultDisposition,
    ScientificResultKind,
    ScientificScope,
    normalize_exact_statement,
)
from .lemma_audit import (
    IntermediateResultKind,
    LemmaAuditPolicy,
    LemmaDependencyReference,
    LemmaLeverage,
    LemmaNomination,
    LemmaProofStep,
    LemmaScope,
    LemmaSourceArtifact,
    LemmaTargetObligationReference,
    preflight_lemma_nomination,
)

if TYPE_CHECKING:
    from .research import ResearchWorkerReport

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


class _NominationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LemmaNominationSkipCode(StrEnum):
    """Machine-actionable reason that a scientific result was not nominated."""

    MAIN_RESULT = "main_result"
    COMPUTATION = "computation"
    DEFINITION = "definition"
    COUNTEREXAMPLE = "counterexample"
    NOT_PROPOSED_COMPLETE = "not_proposed_complete"
    GAPPED = "gapped"
    ASSUMPTIONS_UNBOUND = "assumptions_unbound"
    UNRESOLVED = "unresolved"
    FRONTIER_INVALID = "frontier_invalid"
    OPEN_CUT_NOT_EXACT = "open_cut_not_exact"
    NOT_ON_OPEN_CUT = "not_on_open_cut"
    ADMISSION_MISSING = "admission_missing"
    ADMISSION_AMBIGUOUS = "admission_ambiguous"
    ADMISSION_MISMATCH = "admission_mismatch"
    DEPENDENCY_MISSING = "dependency_missing"
    DEPENDENCY_STALE = "dependency_stale"
    DEPENDENCY_UNTRUSTED = "dependency_untrusted"
    SOURCE_ARTIFACT_MISSING = "source_artifact_missing"
    LOW_LEVERAGE = "low_leverage"
    AUDIT_PREFLIGHT_REJECTED = "audit_preflight_rejected"


class LemmaNominationPolicy(_NominationModel):
    """Deterministic threshold applied after exact open-cut relevance is established."""

    minimum_leverage_score: int = Field(default=1, ge=1)


class LemmaNominationSkip(_NominationModel):
    assignment_id: str
    result_local_key: str
    code: LemmaNominationSkipCode
    message: str
    references: list[str] = Field(default_factory=list)

    @field_validator("assignment_id", "result_local_key", "message")
    @classmethod
    def required_text_is_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("lemma-nomination skip fields must not be blank")
        return normalized


class LemmaNominationBinding(_NominationModel):
    """Exact application-owned admission identity behind one audit nomination."""

    nomination_id: str
    assignment_id: str
    result_local_key: str
    canonical_claim_id: str
    canonical_derivation_id: str
    canonical_proof_attempt_id: str


class LemmaNominationSelection(_NominationModel):
    graph_revision: str
    nominations: list[LemmaNomination] = Field(default_factory=list)
    bindings: list[LemmaNominationBinding] = Field(default_factory=list)
    skipped: list[LemmaNominationSkip] = Field(default_factory=list)


def _skip(
    report: ResearchWorkerReport,
    result: ScientificResult,
    code: LemmaNominationSkipCode,
    message: str,
    *references: str,
) -> LemmaNominationSkip:
    return LemmaNominationSkip(
        assignment_id=report.assignment_id,
        result_local_key=result.local_key,
        code=code,
        message=message,
        references=list(dict.fromkeys(references)),
    )


def _admission_value(node: GraphNode, key: str) -> str | None:
    value = node.metadata.get(key)
    return value if isinstance(value, str) else None


def _matches_admission(node: GraphNode, *, assignment_id: str, local_key: str) -> bool:
    return (
        _admission_value(node, "matek_assignment_id") == assignment_id
        and _admission_value(node, "matek_result_local_key") == local_key
    )


def _scope(scope: ScientificScope) -> LemmaScope:
    return {
        ScientificScope.REDUCTION: LemmaScope.REDUCTION,
        ScientificScope.BRANCH: LemmaScope.BRANCH,
    }[scope]


def _result_kind(kind: ScientificResultKind) -> IntermediateResultKind:
    return (
        IntermediateResultKind.LEMMA
        if kind is ScientificResultKind.LEMMA
        else IntermediateResultKind.RESTRICTED_THEOREM
    )


def _artifact(artifact_id: str, content: str) -> LemmaSourceArtifact:
    return LemmaSourceArtifact(
        artifact_id=artifact_id,
        content=content,
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        # Origin annotations (including confidence and desired verdicts) are deliberately
        # not transferred from the worker or graph metadata.
        origin_annotations=[],
    )


def _nomination_id(
    *, graph_revision: str, assignment_id: str, local_key: str, derivation_id: str
) -> str:
    material = "\0".join([graph_revision, assignment_id, local_key, derivation_id])
    return "lemma-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _unresolved_for_result(report: ResearchWorkerReport, result: ScientificResult) -> list[str]:
    return sorted(
        obligation.local_key
        for obligation in report.unresolved_obligations
        if result.local_key in obligation.parent_result_keys
    )


def _explicit_open_cut_targets(
    result: ScientificResult,
    *,
    claim: GraphNode,
    derivation: GraphNode,
    proof_attempt: GraphNode,
    open_cut_ids: set[str],
    by_id: dict[str, GraphNode],
) -> list[str]:
    targets = set(result.target_node_ids).intersection(open_cut_ids)
    if claim.matek_id in open_cut_ids:
        targets.add(claim.matek_id)
    for node in (claim, derivation, proof_attempt):
        targets.update(
            edge.target_id
            for edge in node.relations
            if edge.target_id in open_cut_ids
            and edge.relation in {RelationType.RESOLVES, RelationType.TARGETS}
        )
    for cut_id in open_cut_ids:
        cut_node = by_id[cut_id]
        if any(
            edge.relation is RelationType.DEPENDS_ON
            and edge.target_id in {claim.matek_id, derivation.matek_id}
            for edge in cut_node.relations
        ):
            targets.add(cut_id)
    return sorted(targets)


def _unlocked_derivations(
    *,
    nodes: Sequence[GraphNode],
    claim_id: str,
    target_ids: set[str],
    canonical_derivation_id: str,
) -> list[str]:
    unlocked: set[str] = set()
    for node in nodes:
        if node.node_type is not NodeType.DERIVATION or node.matek_id == canonical_derivation_id:
            continue
        if any(
            (edge.relation is RelationType.DEPENDS_ON and edge.target_id == claim_id)
            or (edge.relation is RelationType.BLOCKED_BY and edge.target_id in target_ids)
            for edge in node.relations
        ):
            unlocked.add(node.matek_id)
    for node in nodes:
        if node.matek_id not in target_ids:
            continue
        unlocked.update(
            edge.target_id
            for edge in node.relations
            if edge.relation is RelationType.BLOCKS and edge.target_id != canonical_derivation_id
        )
    return sorted(unlocked)


def _dependency_references(
    report: ResearchWorkerReport,
    result: ScientificResult,
    *,
    derivation: GraphNode,
    nodes: Sequence[GraphNode],
    by_id: dict[str, GraphNode],
    trusted_dependency_ids: set[str],
) -> tuple[list[LemmaDependencyReference] | None, LemmaNominationSkipCode | None, str, list[str]]:
    result_by_key = {item.local_key: item for item in report.results}
    resolved_local_ids: list[str] = []
    for local_key in result.dependency_result_keys:
        local_result = result_by_key.get(local_key)
        if local_result is None:
            return (
                None,
                LemmaNominationSkipCode.ADMISSION_MISMATCH,
                "A local dependency key has no exact typed result in this report.",
                [local_key],
            )
        if (
            local_result.exact_gap is not None
            or local_result.disposition is not ScientificResultDisposition.PROPOSED_COMPLETE
        ):
            return (
                None,
                LemmaNominationSkipCode.ADMISSION_MISMATCH,
                "A local dependency is not a gap-free proposed-complete result.",
                [local_key],
            )
        matching = [
            node
            for node in nodes
            if (
                node_has_scientific_admission_binding(
                    node,
                    run_id=derivation.created_in_run,
                    assignment_id=report.assignment_id,
                    result=local_result,
                )
                if local_result.kind is ScientificResultKind.DEFINITION
                else (
                    _matches_admission(
                        node,
                        assignment_id=report.assignment_id,
                        local_key=local_key,
                    )
                    and _has_exact_result_admission(
                        node,
                        report=report,
                        result=local_result,
                    )
                )
            )
        ]
        if local_result.kind is ScientificResultKind.DEFINITION:
            definitions = [node for node in matching if node.node_type is NodeType.DEFINITION]
            if len(definitions) != 1:
                return (
                    None,
                    LemmaNominationSkipCode.ADMISSION_AMBIGUOUS,
                    "A local definition dependency does not resolve to exactly one admitted "
                    "definition.",
                    [local_key, *(node.matek_id for node in definitions)],
                )
            definition = definitions[0]
            if (
                canonical_admitted_definition_scope(definition) is not ScientificScope.BRANCH
                or definition.tombstone
                or definition.invalidation_reasons
                or definition.epistemic_status
                in {
                    EpistemicStatus.STALE,
                    EpistemicStatus.INCONSISTENT,
                    EpistemicStatus.REFUTED,
                }
                or definition.workflow_status
                in {
                    WorkflowStatus.BLOCKED,
                    WorkflowStatus.ABANDONED,
                    WorkflowStatus.SUPERSEDED,
                }
                or normalize_exact_statement(exact_statement(definition.body))
                != normalize_exact_statement(local_result.exact_statement)
            ):
                return (
                    None,
                    LemmaNominationSkipCode.ADMISSION_MISMATCH,
                    "A local definition dependency differs from its typed exact statement.",
                    [local_key, definition.matek_id],
                )
            resolved_local_ids.append(definition.matek_id)
            if definition.matek_id not in trusted_dependency_ids:
                return (
                    None,
                    LemmaNominationSkipCode.DEPENDENCY_UNTRUSTED,
                    "A local definition dependency is not trusted in the current canonical ledger.",
                    [definition.matek_id],
                )
            continue

        dependency_derivations = [
            node for node in matching if node.node_type is NodeType.DERIVATION
        ]
        if len(dependency_derivations) != 1:
            return (
                None,
                LemmaNominationSkipCode.ADMISSION_AMBIGUOUS,
                "A local result dependency does not resolve to exactly one admitted derivation.",
                [local_key, *(node.matek_id for node in dependency_derivations)],
            )
        dependency_derivation = dependency_derivations[0]
        if (
            dependency_derivation.tombstone
            or dependency_derivation.epistemic_status
            in {
                EpistemicStatus.STALE,
                EpistemicStatus.INCONSISTENT,
                EpistemicStatus.REFUTED,
            }
            or dependency_derivation.workflow_status
            in {
                WorkflowStatus.BLOCKED,
                WorkflowStatus.ABANDONED,
                WorkflowStatus.SUPERSEDED,
            }
            or dependency_derivation.invalidation_reasons
        ):
            return (
                None,
                LemmaNominationSkipCode.DEPENDENCY_STALE,
                "A local result dependency's admitted derivation is no longer current.",
                [dependency_derivation.matek_id],
            )
        proved_ids = [
            edge.target_id
            for edge in dependency_derivation.relations
            if edge.relation is RelationType.PROVES
        ]
        if len(proved_ids) != 1:
            return (
                None,
                LemmaNominationSkipCode.ADMISSION_AMBIGUOUS,
                "A local dependency derivation does not prove exactly one canonical claim.",
                [dependency_derivation.matek_id, *proved_ids],
            )
        dependency_claim = by_id.get(proved_ids[0])
        if (
            dependency_claim is None
            or dependency_claim.node_type is not NodeType.CLAIM
            or dependency_claim.tombstone
            or dependency_claim.invalidation_reasons
            or dependency_claim.epistemic_status
            in {
                EpistemicStatus.STALE,
                EpistemicStatus.INCONSISTENT,
                EpistemicStatus.REFUTED,
            }
            or dependency_claim.workflow_status
            in {
                WorkflowStatus.BLOCKED,
                WorkflowStatus.ABANDONED,
                WorkflowStatus.SUPERSEDED,
            }
            or _admission_value(dependency_derivation, "matek_conclusion_claim_id")
            != dependency_claim.matek_id
            or normalize_exact_statement(exact_statement(dependency_claim.body))
            != normalize_exact_statement(local_result.exact_statement)
        ):
            return (
                None,
                LemmaNominationSkipCode.ADMISSION_MISMATCH,
                "A local dependency's typed result, derivation, and conclusion disagree.",
                [dependency_derivation.matek_id, proved_ids[0]],
            )
        if dependency_claim.matek_id not in trusted_dependency_ids:
            return (
                None,
                LemmaNominationSkipCode.DEPENDENCY_UNTRUSTED,
                "A local claim dependency has not passed independent canonical audit.",
                [dependency_claim.matek_id],
            )
        resolved_local_ids.append(dependency_claim.matek_id)

    expected_ids = list(dict.fromkeys([*result.dependency_node_ids, *resolved_local_ids]))
    admitted_ids = sorted(
        edge.target_id for edge in derivation.relations if edge.relation is RelationType.DEPENDS_ON
    )
    declared_ids = sorted(expected_ids)
    if admitted_ids != declared_ids:
        return (
            None,
            LemmaNominationSkipCode.ADMISSION_MISMATCH,
            "The canonical derivation premises do not exactly match the typed result.",
            sorted(set(admitted_ids).symmetric_difference(declared_ids)),
        )
    if result.dependency_result_keys:
        raw_result_keys = derivation.metadata.get("matek_dependency_result_keys")
        raw_premise_ids = derivation.metadata.get("matek_premise_claim_ids")
        if raw_result_keys != result.dependency_result_keys or raw_premise_ids != expected_ids:
            return (
                None,
                LemmaNominationSkipCode.ADMISSION_MISMATCH,
                "The application-owned local dependency metadata does not match the typed "
                "report and admitted premise edges.",
                list(dict.fromkeys([*result.dependency_result_keys, *expected_ids])),
            )
    references: list[LemmaDependencyReference] = []
    for dependency_id in declared_ids:
        dependency = by_id.get(dependency_id)
        if dependency is None:
            return (
                None,
                LemmaNominationSkipCode.DEPENDENCY_MISSING,
                "A declared dependency is absent from the current graph.",
                [dependency_id],
            )
        if (
            dependency.tombstone
            or dependency.epistemic_status
            in {
                EpistemicStatus.STALE,
                EpistemicStatus.INCONSISTENT,
                EpistemicStatus.REFUTED,
            }
            or dependency.workflow_status
            in {
                WorkflowStatus.BLOCKED,
                WorkflowStatus.ABANDONED,
                WorkflowStatus.SUPERSEDED,
            }
            or dependency.invalidation_reasons
        ):
            return (
                None,
                LemmaNominationSkipCode.DEPENDENCY_STALE,
                "A declared dependency is stale, inconsistent, tombstoned, or invalidated.",
                [dependency_id],
            )
        if dependency.node_type not in {NodeType.CLAIM, NodeType.DEFINITION} or (
            dependency.node_type is NodeType.DEFINITION
            and canonical_admitted_definition_scope(dependency) is not ScientificScope.BRANCH
        ):
            return (
                None,
                LemmaNominationSkipCode.ADMISSION_MISMATCH,
                "A declared dependency is not a canonical claim or admitted definition.",
                [dependency_id],
            )
        if dependency_id not in trusted_dependency_ids:
            return (
                None,
                LemmaNominationSkipCode.DEPENDENCY_UNTRUSTED,
                "A declared dependency has not passed independent canonical audit.",
                [dependency_id],
            )
        dependency_statement = exact_statement(dependency.body).strip()
        if (
            not dependency_statement
            or dependency.content_hash is None
            or not _SHA256.fullmatch(dependency.content_hash)
        ):
            return (
                None,
                LemmaNominationSkipCode.DEPENDENCY_MISSING,
                "A dependency lacks its current exact statement or persisted content hash.",
                [dependency_id],
            )
        references.append(
            LemmaDependencyReference(
                dependency_id=dependency_id,
                exact_statement=dependency_statement,
                statement_version=dependency.statement_version,
                content_sha256=dependency.content_hash,
                current_statement_version=dependency.statement_version,
                current_content_sha256=dependency.content_hash,
                origin_status=dependency.epistemic_status.value,
            )
        )
    return references, None, "", []


def _has_exact_result_admission(
    node: GraphNode,
    *,
    report: ResearchWorkerReport,
    result: ScientificResult,
) -> bool:
    """Recognize application-owned admission metadata for one immutable typed result."""

    identity = _admission_value(node, "matek_admission_identity")
    payload_sha256 = _admission_value(node, "matek_admission_payload_sha256")
    if identity is None or payload_sha256 != admission_payload_sha256(result):
        return False
    return identity.split("\0") == [
        node.created_in_run,
        report.assignment_id,
        result.local_key,
        str(result.schema_version),
    ]


def _canonical_binding(
    report: ResearchWorkerReport,
    result: ScientificResult,
    *,
    nodes: Sequence[GraphNode],
    by_id: dict[str, GraphNode],
) -> tuple[GraphNode, GraphNode, GraphNode] | LemmaNominationSkip:
    matched = [
        node
        for node in nodes
        if _matches_admission(
            node,
            assignment_id=report.assignment_id,
            local_key=result.local_key,
        )
    ]
    derivations = [node for node in matched if node.node_type is NodeType.DERIVATION]
    if not derivations:
        return _skip(
            report,
            result,
            LemmaNominationSkipCode.ADMISSION_MISSING,
            "No admitted canonical derivation has this assignment/local-key identity.",
        )
    if len(derivations) != 1:
        return _skip(
            report,
            result,
            LemmaNominationSkipCode.ADMISSION_AMBIGUOUS,
            "More than one derivation has this assignment/local-key identity.",
            *(node.matek_id for node in derivations),
        )
    derivation = derivations[0]
    proved_ids = sorted(
        edge.target_id for edge in derivation.relations if edge.relation is RelationType.PROVES
    )
    if len(proved_ids) != 1:
        return _skip(
            report,
            result,
            LemmaNominationSkipCode.ADMISSION_AMBIGUOUS,
            "The admitted derivation does not prove exactly one canonical claim.",
            *proved_ids,
        )
    claim = by_id.get(proved_ids[0])
    if claim is None or claim.node_type is not NodeType.CLAIM:
        return _skip(
            report,
            result,
            LemmaNominationSkipCode.ADMISSION_MISSING,
            "The canonical claim proved by the admitted derivation is unavailable.",
            proved_ids[0],
        )
    if _admission_value(
        derivation, "matek_conclusion_claim_id"
    ) != claim.matek_id or normalize_exact_statement(
        exact_statement(claim.body)
    ) != normalize_exact_statement(result.exact_statement):
        return _skip(
            report,
            result,
            LemmaNominationSkipCode.ADMISSION_MISMATCH,
            "The typed result, derivation conclusion, and canonical exact claim disagree.",
            claim.matek_id,
            derivation.matek_id,
        )
    attempt_id = _admission_value(derivation, "matek_proof_attempt_id")
    attempt = by_id.get(attempt_id or "")
    if (
        attempt is None
        or attempt.node_type is not NodeType.PROOF_ATTEMPT
        or not _matches_admission(
            attempt,
            assignment_id=report.assignment_id,
            local_key=result.local_key,
        )
    ):
        return _skip(
            report,
            result,
            LemmaNominationSkipCode.ADMISSION_MISSING,
            "The derivation's uniquely identified canonical proof attempt is unavailable.",
            *(item for item in [attempt_id or ""] if item),
        )
    if (
        derivation.metadata.get("matek_obligation_ids")
        or any(edge.relation is RelationType.BLOCKED_BY for edge in derivation.relations)
        or attempt.metadata.get("matek_exact_gap") is not None
    ):
        return _skip(
            report,
            result,
            LemmaNominationSkipCode.UNRESOLVED,
            "The admitted derivation or proof attempt retains a blocking obligation.",
            derivation.matek_id,
            attempt.matek_id,
        )
    if result.proof_or_certificate not in attempt.evidence:
        return _skip(
            report,
            result,
            LemmaNominationSkipCode.ADMISSION_MISMATCH,
            "The canonical proof attempt does not preserve the typed proof certificate.",
            attempt.matek_id,
        )
    return claim, derivation, attempt


def _early_skip(
    report: ResearchWorkerReport,
    result: ScientificResult,
) -> LemmaNominationSkip | None:
    if result.assumptions:
        return _skip(
            report,
            result,
            LemmaNominationSkipCode.ASSUMPTIONS_UNBOUND,
            "The result carries assumptions that are not part of its canonical exact statement.",
            *result.assumptions,
        )
    if result.scope is ScientificScope.MAIN:
        return _skip(
            report,
            result,
            LemmaNominationSkipCode.MAIN_RESULT,
            "The independent lemma lane cannot audit the main result.",
        )
    if (
        result.kind is ScientificResultKind.COMPUTATION
        or result.scope is ScientificScope.COMPUTATION
    ):
        return _skip(
            report,
            result,
            LemmaNominationSkipCode.COMPUTATION,
            "Computational results require replay, not the intermediate-lemma lane.",
        )
    if result.kind is ScientificResultKind.DEFINITION:
        return _skip(
            report,
            result,
            LemmaNominationSkipCode.DEFINITION,
            "Definitions are not intermediate theorem nominations.",
        )
    if result.kind is ScientificResultKind.COUNTEREXAMPLE:
        return _skip(
            report,
            result,
            LemmaNominationSkipCode.COUNTEREXAMPLE,
            "Counterexamples use their independent exact-contract audit lane.",
        )
    if result.exact_gap is not None:
        return _skip(
            report,
            result,
            LemmaNominationSkipCode.GAPPED,
            "The scientific result retains an exact proof gap.",
        )
    if result.disposition is not ScientificResultDisposition.PROPOSED_COMPLETE:
        return _skip(
            report,
            result,
            LemmaNominationSkipCode.NOT_PROPOSED_COMPLETE,
            "Only a proposed-complete intermediate can enter independent lemma audit.",
        )
    unresolved = _unresolved_for_result(report, result)
    if unresolved:
        return _skip(
            report,
            result,
            LemmaNominationSkipCode.UNRESOLVED,
            "The worker report retains obligations whose parent is this result.",
            *unresolved,
        )
    return None


def nominate_intermediate_lemmas(
    report: ResearchWorkerReport,
    *,
    graph_nodes: Sequence[GraphNode],
    frontier: GraphFrontier,
    policy: LemmaNominationPolicy | None = None,
) -> LemmaNominationSelection:
    """Return audit-ready intermediate nominations and typed deterministic skips.

    ``graph_nodes`` and ``frontier`` must be from the same current graph revision.  The
    helper performs no I/O and no semantic or fuzzy matching.
    """

    selected_policy = policy or LemmaNominationPolicy()
    nodes = sorted(graph_nodes, key=lambda node: node.matek_id)
    by_id = {node.matek_id: node for node in nodes}
    cut_ids = {item.matek_id for item in frontier.smallest_known_open_cut}
    main_target_id = frontier.main_target.matek_id if frontier.main_target is not None else None
    main_target = by_id.get(main_target_id or "")
    frontier_valid = bool(
        main_target is not None
        and main_target.node_type is NodeType.CLAIM
        and cut_ids
        and cut_ids.issubset(by_id)
    )
    nominations: list[LemmaNomination] = []
    bindings: list[LemmaNominationBinding] = []
    skipped: list[LemmaNominationSkip] = []
    canonical_ledger = (
        project_markdown_ledger(
            nodes,
            graph_revision=frontier.graph_revision,
            problem_id=frontier.problem_id,
            target_claim_id=main_target_id,
        )
        if main_target_id is not None
        else None
    )
    trusted_dependency_ids = (
        trusted_claim_ids(canonical_ledger) if canonical_ledger is not None else set()
    )

    for result in sorted(report.results, key=lambda item: item.local_key):
        early = _early_skip(report, result)
        if early is not None:
            skipped.append(early)
            continue
        if frontier.open_cut_search_capped:
            skipped.append(
                _skip(
                    report,
                    result,
                    LemmaNominationSkipCode.OPEN_CUT_NOT_EXACT,
                    "The current open-cut search was capped; exact minimal-cut relevance "
                    "is unknown.",
                )
            )
            continue
        if not frontier_valid or main_target is None:
            skipped.append(
                _skip(
                    report,
                    result,
                    LemmaNominationSkipCode.FRONTIER_INVALID,
                    "The current frontier lacks a resolvable main target or nonempty exact "
                    "open cut.",
                    *(sorted(cut_ids - set(by_id))),
                )
            )
            continue
        binding = _canonical_binding(report, result, nodes=nodes, by_id=by_id)
        if isinstance(binding, LemmaNominationSkip):
            skipped.append(binding)
            continue
        claim, derivation, proof_attempt = binding
        dependencies, dependency_code, dependency_message, dependency_refs = _dependency_references(
            report,
            result,
            derivation=derivation,
            nodes=nodes,
            by_id=by_id,
            trusted_dependency_ids=trusted_dependency_ids,
        )
        if dependencies is None:
            assert dependency_code is not None
            skipped.append(
                _skip(
                    report,
                    result,
                    dependency_code,
                    dependency_message,
                    *dependency_refs,
                )
            )
            continue
        target_ids = _explicit_open_cut_targets(
            result,
            claim=claim,
            derivation=derivation,
            proof_attempt=proof_attempt,
            open_cut_ids=cut_ids,
            by_id=by_id,
        )
        if not target_ids:
            skipped.append(
                _skip(
                    report,
                    result,
                    LemmaNominationSkipCode.NOT_ON_OPEN_CUT,
                    "No explicit result target or graph edge reaches the current smallest "
                    "open cut.",
                    *sorted(cut_ids),
                )
            )
            continue
        target_contracts: list[LemmaTargetObligationReference] = []
        invalid_target_ids: list[str] = []
        for target_id in target_ids:
            obligation = (
                canonical_ledger.obligations.get(target_id)
                if canonical_ledger is not None
                else None
            )
            obligation_node = by_id.get(target_id)
            if (
                obligation is not None
                and obligation_node is not None
                and (
                    obligation_node.node_type is NodeType.OBLIGATION
                    and obligation_node.content_hash is not None
                )
            ):
                target_contracts.append(
                    LemmaTargetObligationReference(
                        obligation_id=obligation.obligation_id,
                        exact_statement=obligation.exact_statement,
                        quantifiers=obligation.quantifiers,
                        hypotheses=obligation.hypotheses,
                        conclusion=obligation.conclusion,
                        dependency_claim_ids=obligation.dependency_claim_ids,
                        target_claim_ids=obligation.target_claim_ids,
                        scope=obligation.scope,
                        notation_definition_version=obligation.notation_definition_version,
                        falsification_evidence=obligation.falsification_evidence,
                        logical_version=obligation.logical_version,
                        statement_version=obligation_node.statement_version,
                        content_sha256=obligation_node.content_hash,
                    )
                )
                continue
            claim_contract = (
                canonical_ledger.claims.get(target_id) if canonical_ledger is not None else None
            )
            if (
                claim_contract is None
                or obligation_node is None
                or obligation_node.node_type is not NodeType.CLAIM
                or obligation_node.content_hash is None
            ):
                invalid_target_ids.append(target_id)
                continue
            # An obligation-free graph uses the canonical main CLAIM as its exact
            # smallest-cut fallback. Freeze a self-describing contract for that one
            # case; explicit OBL nodes retain their full richer contracts above.
            target_contracts.append(
                LemmaTargetObligationReference(
                    target_kind="claim",
                    obligation_id=claim_contract.claim_id,
                    exact_statement=claim_contract.exact_statement,
                    conclusion=claim_contract.exact_statement,
                    scope=claim_contract.scope,
                    notation_definition_version="1",
                    logical_version=claim_contract.logical_version,
                    statement_version=obligation_node.statement_version,
                    content_sha256=obligation_node.content_hash,
                )
            )
        if invalid_target_ids:
            skipped.append(
                _skip(
                    report,
                    result,
                    LemmaNominationSkipCode.FRONTIER_INVALID,
                    "A targeted open-cut node lacks a complete canonical obligation contract.",
                    *invalid_target_ids,
                )
            )
            continue
        unlocked_ids = _unlocked_derivations(
            nodes=nodes,
            claim_id=claim.matek_id,
            target_ids=set(target_ids),
            canonical_derivation_id=derivation.matek_id,
        )
        leverage = LemmaLeverage(
            downstream_obligation_ids=target_ids,
            estimated_open_cut_reduction=len(target_ids),
            unlocked_branch_count=len(unlocked_ids),
            rationale=(
                f"Explicitly reaches {len(target_ids)} member(s) of the current smallest "
                f"open cut and unlocks {len(unlocked_ids)} graph derivation(s)."
            ),
        )
        if leverage.score < selected_policy.minimum_leverage_score:
            skipped.append(
                _skip(
                    report,
                    result,
                    LemmaNominationSkipCode.LOW_LEVERAGE,
                    "The exact graph leverage score is below the nomination threshold.",
                    str(leverage.score),
                )
            )
            continue
        source_artifacts = [
            _artifact(proof_attempt.matek_id, result.proof_or_certificate),
            _artifact(derivation.matek_id, derivation.body),
        ]
        related_artifact_ids = sorted(
            {
                edge.target_id
                for edge in derivation.relations
                if edge.target_id in by_id and by_id[edge.target_id].node_type is NodeType.ARTIFACT
            }
        )
        for artifact_id in related_artifact_ids:
            artifact_node = by_id[artifact_id]
            if not artifact_node.body.strip():
                skipped.append(
                    _skip(
                        report,
                        result,
                        LemmaNominationSkipCode.SOURCE_ARTIFACT_MISSING,
                        "A canonical related artifact has no auditable content.",
                        artifact_id,
                    )
                )
                source_artifacts = []
                break
            source_artifacts.append(_artifact(artifact_id, artifact_node.body))
        if not source_artifacts:
            continue
        # Preserve typed manifest declarations as evidence, never worker confidence/status.
        for declaration in sorted(report.artifact_manifest, key=lambda item: item.path):
            if result.local_key not in declaration.supporting_result_keys:
                continue
            content = json.dumps(
                declaration.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            source_artifacts.append(_artifact(f"manifest-{digest[:24]}", content))

        nomination_id = _nomination_id(
            graph_revision=frontier.graph_revision,
            assignment_id=report.assignment_id,
            local_key=result.local_key,
            derivation_id=derivation.matek_id,
        )
        proof_source_ids = [artifact.artifact_id for artifact in source_artifacts]
        nomination = LemmaNomination(
            nomination_id=nomination_id,
            statement_id=claim.matek_id,
            canonical_derivation_id=derivation.matek_id,
            result_kind=_result_kind(result.kind),
            scope=_scope(result.scope),
            exact_statement=result.exact_statement,
            hypotheses=result.assumptions,
            main_target_statement=exact_statement(main_target.body),
            target_obligation_ids=target_ids,
            target_obligation_contracts=target_contracts,
            relevance_statement=(
                "This admitted intermediate has an explicit edge or typed target into the "
                "current smallest open cut."
            ),
            supports_main_target=True,
            proof_steps=[
                LemmaProofStep(
                    step_id="complete-proof",
                    statement=result.exact_statement,
                    justification=result.proof_or_certificate,
                    source_artifact_ids=proof_source_ids,
                )
            ],
            conclusion_step_id="complete-proof",
            gap_free=True,
            base_graph_revision=frontier.graph_revision,
            current_graph_revision=frontier.graph_revision,
            dependencies=dependencies,
            source_artifacts=source_artifacts,
            leverage=leverage,
            origin_worker_id=report.assignment_id,
            origin_status=result.disposition.value,
        )
        preflight = preflight_lemma_nomination(
            nomination,
            policy=LemmaAuditPolicy(minimum_leverage_score=selected_policy.minimum_leverage_score),
        )
        if not preflight.accepted:
            skipped.append(
                _skip(
                    report,
                    result,
                    LemmaNominationSkipCode.AUDIT_PREFLIGHT_REJECTED,
                    "The constructed nomination failed deterministic lemma-audit preflight.",
                    *(issue.code.value for issue in preflight.issues),
                )
            )
            continue
        nominations.append(nomination)
        bindings.append(
            LemmaNominationBinding(
                nomination_id=nomination_id,
                assignment_id=report.assignment_id,
                result_local_key=result.local_key,
                canonical_claim_id=claim.matek_id,
                canonical_derivation_id=derivation.matek_id,
                canonical_proof_attempt_id=proof_attempt.matek_id,
            )
        )

    ordered = sorted(
        zip(nominations, bindings, strict=True),
        key=lambda item: (-item[0].leverage.score, item[0].statement_id, item[0].nomination_id),
    )
    return LemmaNominationSelection(
        graph_revision=frontier.graph_revision,
        nominations=[item[0] for item in ordered],
        bindings=[item[1] for item in ordered],
        skipped=skipped,
    )


__all__ = [
    "LemmaNominationBinding",
    "LemmaNominationPolicy",
    "LemmaNominationSelection",
    "LemmaNominationSkip",
    "LemmaNominationSkipCode",
    "nominate_intermediate_lemmas",
]
