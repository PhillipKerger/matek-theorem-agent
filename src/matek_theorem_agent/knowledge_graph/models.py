"""Typed models for MATEK's persistent Markdown knowledge graph."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..graph_ids import LEGACY_NODE_ID, validate_any_node_id


class _GraphModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NodeType(StrEnum):
    PROBLEM = "problem"
    DEFINITION = "definition"
    CLAIM = "claim"
    PROOF = "proof"
    PROOF_ATTEMPT = "proof_attempt"
    DERIVATION = "derivation"
    OBLIGATION = "obligation"
    APPROACH = "approach"
    TASK = "task"
    COUNTEREXAMPLE = "counterexample"
    EXPERIMENT = "experiment"
    SOURCE = "source"
    AUDIT = "audit"
    FORMALIZATION = "formalization"
    RUN = "run"
    ARTIFACT = "artifact"
    HUMAN_NOTE = "human_note"


class ClaimType(StrEnum):
    LEMMA = "lemma"
    THEOREM = "theorem"
    CONJECTURE = "conjecture"
    COROLLARY = "corollary"
    EQUIVALENCE = "equivalence"
    ALGORITHM = "algorithm"
    LOWER_BOUND = "lower_bound"
    UPPER_BOUND = "upper_bound"
    CLASSIFICATION = "classification"


class EpistemicStatus(StrEnum):
    OPEN = "open"
    CONJECTURED = "conjectured"
    CANDIDATE = "candidate"
    PROVED_INFORMALLY = "proved_informally"
    AUDIT_PASSED = "audit_passed"
    LEAN_VERIFIED = "lean_verified"
    REFUTED = "refuted"
    INCONSISTENT = "inconsistent"
    STALE = "stale"


class WorkflowStatus(StrEnum):
    QUEUED = "queued"
    ACTIVE = "active"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DORMANT = "dormant"
    ABANDONED = "abandoned"
    SUPERSEDED = "superseded"
    COMPLETE = "complete"


class RelationType(StrEnum):
    DEPENDS_ON = "depends_on"
    PROVES = "proves"
    DISPROVES = "disproves"
    REFUTES = "refutes"
    STRENGTHENS = "strengthens"
    WEAKENS = "weakens"
    SPECIALIZES = "specializes"
    GENERALIZES = "generalizes"
    EQUIVALENT_TO = "equivalent_to"
    CONTRADICTS = "contradicts"
    MOTIVATES = "motivates"
    BLOCKS = "blocks"
    BLOCKED_BY = "blocked_by"
    RESOLVES = "resolves"
    TESTS = "tests"
    CITES = "cites"
    AUDITS = "audits"
    FORMALIZES = "formalizes"
    SUPERSEDES = "supersedes"
    CREATED_DURING = "created_during"
    RELATED_TO = "related_to"
    TARGETS = "targets"


NODE_ID_PREFIXES: dict[NodeType, str] = {
    NodeType.PROBLEM: "PRB",
    NodeType.DEFINITION: "DEF",
    NodeType.CLAIM: "CLM",
    NodeType.PROOF: "PRF",
    NodeType.PROOF_ATTEMPT: "PAT",
    NodeType.DERIVATION: "DRV",
    NodeType.OBLIGATION: "OBL",
    NodeType.APPROACH: "APR",
    NodeType.TASK: "TSK",
    NodeType.COUNTEREXAMPLE: "CEX",
    NodeType.EXPERIMENT: "EXP",
    NodeType.SOURCE: "SRC",
    NodeType.AUDIT: "AUD",
    NodeType.FORMALIZATION: "FRM",
    NodeType.RUN: "RUN",
    NodeType.ARTIFACT: "ART",
    NodeType.HUMAN_NOTE: "HUM",
}

#: Full-word ID prefixes for agent-authored mathematical content.  These node
#: types are minted with descriptive one-liner IDs (``CLAIM: ...``) instead of
#: opaque hashes so agents can reference graph artifacts by meaningful names.
NODE_ID_WORDS: dict[NodeType, str] = {
    NodeType.CLAIM: "CLAIM",
    NodeType.DEFINITION: "DEFINITION",
    NodeType.APPROACH: "APPROACH",
    NodeType.PROOF: "PROOF",
    NodeType.PROOF_ATTEMPT: "PROOF ATTEMPT",
    NodeType.DERIVATION: "DERIVATION",
    NodeType.OBLIGATION: "OBLIGATION",
    NodeType.COUNTEREXAMPLE: "COUNTEREXAMPLE",
    NodeType.EXPERIMENT: "EXPERIMENT",
}

NODE_TYPE_DIRECTORIES: dict[NodeType, str] = {
    NodeType.PROBLEM: "Problems",
    NodeType.DEFINITION: "Definitions",
    NodeType.CLAIM: "Claims",
    NodeType.PROOF: "Proofs",
    NodeType.PROOF_ATTEMPT: "Proof Attempts",
    NodeType.DERIVATION: "Derivations",
    NodeType.OBLIGATION: "Obligations",
    NodeType.APPROACH: "Approaches",
    NodeType.TASK: "Tasks",
    NodeType.COUNTEREXAMPLE: "Counterexamples",
    NodeType.EXPERIMENT: "Experiments",
    NodeType.SOURCE: "Sources",
    NodeType.AUDIT: "Audits",
    NodeType.FORMALIZATION: "Formalizations",
    NodeType.RUN: "Runs",
    NodeType.ARTIFACT: "Artifacts",
    NodeType.HUMAN_NOTE: "Human Notes",
}

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


def validate_node_id(value: str) -> str:
    return validate_any_node_id(value)


def node_id_matches_type(value: str, node_type: NodeType) -> bool:
    """Return whether one valid node ID belongs to ``node_type``.

    Both legacy hash IDs (``CLM-...``) and descriptive one-liner IDs
    (``CLAIM: ...``) are recognized.
    """

    if value.startswith(NODE_ID_PREFIXES[node_type] + "-"):
        return bool(LEGACY_NODE_ID.fullmatch(value))
    word = NODE_ID_WORDS.get(node_type)
    return word is not None and value.startswith(word + ": ")


class GraphEdge(_GraphModel):
    source_id: str
    relation: RelationType
    target_id: str

    @field_validator("source_id", "target_id")
    @classmethod
    def node_ids_are_valid(cls, value: str) -> str:
        return validate_node_id(value)


class GraphNode(_GraphModel):
    matek_id: str
    node_type: NodeType
    problem_id: str
    title: str
    epistemic_status: EpistemicStatus = EpistemicStatus.OPEN
    workflow_status: WorkflowStatus = WorkflowStatus.ACTIVE
    claim_type: ClaimType | None = None
    statement_version: int = Field(default=1, ge=1)
    created_in_run: str
    last_modified_run: str
    author_role: str = "matek"
    created_at: datetime
    updated_at: datetime
    body: str
    tags: list[str] = Field(default_factory=list)
    relations: list[GraphEdge] = Field(default_factory=list)
    invalidation_reasons: list[str] = Field(default_factory=list)
    dependency_versions: list[str] = Field(default_factory=list)
    source_artifacts: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    manuscript_mappings: list[str] = Field(default_factory=list)
    metadata: dict[str, str | int | bool | list[str] | None] = Field(default_factory=dict)
    tombstone: bool = False
    path: str | None = None
    content_hash: str | None = None

    @field_validator("matek_id", "problem_id")
    @classmethod
    def node_ids_are_valid(cls, value: str) -> str:
        return validate_node_id(value)

    @field_validator("title", "created_in_run", "last_modified_run", "author_role")
    @classmethod
    def text_is_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("graph node text fields must not be blank")
        return normalized

    @field_validator(
        "tags",
        "invalidation_reasons",
        "dependency_versions",
        "source_artifacts",
        "evidence",
        "manuscript_mappings",
    )
    @classmethod
    def string_lists_are_normalized(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        return list(dict.fromkeys(normalized))

    @model_validator(mode="after")
    def type_specific_fields_are_consistent(self) -> GraphNode:
        if not node_id_matches_type(self.matek_id, self.node_type):
            raise ValueError(
                f"{self.node_type.value} node ID does not match its node type: "
                f"{self.matek_id!r}"
            )
        if self.node_type is NodeType.CLAIM and self.claim_type is None:
            raise ValueError("claim nodes require claim_type")
        if self.node_type is not NodeType.CLAIM and self.claim_type is not None:
            raise ValueError("claim_type is permitted only on claim nodes")
        if any(edge.source_id != self.matek_id for edge in self.relations):
            raise ValueError("every embedded relation must originate at its containing node")
        return self


class GraphNodeCreate(_GraphModel):
    matek_id: str | None = None
    node_type: NodeType
    title: str
    body: str
    claim_type: ClaimType | None = None
    epistemic_status: EpistemicStatus = EpistemicStatus.OPEN
    workflow_status: WorkflowStatus = WorkflowStatus.ACTIVE
    tags: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    source_artifacts: list[str] = Field(default_factory=list)

    @field_validator("matek_id")
    @classmethod
    def optional_id_is_valid(cls, value: str | None) -> str | None:
        return None if value is None else validate_node_id(value)

    @field_validator("title", "body")
    @classmethod
    def required_text_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("created graph nodes require a title and distilled body")
        return value.strip()

    @model_validator(mode="after")
    def claim_type_matches_node_type(self) -> GraphNodeCreate:
        if self.node_type is NodeType.CLAIM and self.claim_type is None:
            raise ValueError("created claim nodes require claim_type")
        if self.node_type is not NodeType.CLAIM and self.claim_type is not None:
            raise ValueError("claim_type is permitted only on claim nodes")
        if self.matek_id is not None and not node_id_matches_type(self.matek_id, self.node_type):
            raise ValueError("proposed node ID prefix does not match node_type")
        return self


class GraphNodeUpdate(_GraphModel):
    matek_id: str
    title: str | None = None
    body: str | None = None
    tags: list[str] | None = None
    evidence: list[str] = Field(default_factory=list)
    source_artifacts: list[str] = Field(default_factory=list)
    reason: str

    @field_validator("matek_id")
    @classmethod
    def node_id_is_valid(cls, value: str) -> str:
        return validate_node_id(value)

    @model_validator(mode="after")
    def update_changes_something(self) -> GraphNodeUpdate:
        if (
            self.title is None
            and self.body is None
            and self.tags is None
            and not (self.evidence or self.source_artifacts)
        ):
            raise ValueError("graph update must change content or attach evidence")
        if not self.reason.strip():
            raise ValueError("graph update requires a reason")
        return self


class GraphStatusChange(_GraphModel):
    matek_id: str
    epistemic_status: EpistemicStatus | None = None
    workflow_status: WorkflowStatus | None = None
    reason: str

    @field_validator("matek_id")
    @classmethod
    def node_id_is_valid(cls, value: str) -> str:
        return validate_node_id(value)

    @model_validator(mode="after")
    def status_change_is_complete(self) -> GraphStatusChange:
        if self.epistemic_status is None and self.workflow_status is None:
            raise ValueError("status change must select at least one status axis")
        if not self.reason.strip():
            raise ValueError("status change requires a reason")
        return self


class GraphPatch(_GraphModel):
    base_graph_revision: str
    run_id: str
    task_id: str
    agent_role: str = "research-worker"
    create_nodes: list[GraphNodeCreate] = Field(default_factory=list)
    update_nodes: list[GraphNodeUpdate] = Field(default_factory=list)
    add_edges: list[GraphEdge] = Field(default_factory=list)
    remove_edges: list[GraphEdge] = Field(default_factory=list)
    proposed_status_changes: list[GraphStatusChange] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    unresolved_obligations: list[str] = Field(default_factory=list)

    @field_validator("base_graph_revision", "run_id", "agent_role")
    @classmethod
    def patch_identity_is_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("graph patch identity fields must not be blank")
        return normalized

    @field_validator("task_id")
    @classmethod
    def task_id_is_valid(cls, value: str) -> str:
        normalized = validate_node_id(value)
        if not normalized.startswith("TSK-"):
            raise ValueError("graph patch task_id must identify a task node")
        return normalized


class GraphMergeResult(_GraphModel):
    operation_id: str
    status: Literal["merged", "partially_merged", "conflict", "rejected", "already_applied"]
    base_revision: str
    previous_revision: str
    new_revision: str
    created_node_ids: list[str] = Field(default_factory=list)
    updated_node_ids: list[str] = Field(default_factory=list)
    stale_node_ids: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)

    @property
    def committed(self) -> bool:
        return self.status in {"merged", "partially_merged", "already_applied"}


class GraphValidationIssue(_GraphModel):
    severity: Literal["error", "warning"]
    code: str
    message: str
    path: str | None = None
    node_id: str | None = None


class GraphValidationReport(_GraphModel):
    valid: bool
    revision: str | None
    node_count: int
    edge_count: int
    issues: list[GraphValidationIssue] = Field(default_factory=list)


class GraphHygieneAction(_GraphModel):
    rule: Literal["primary_identifier_in_identifiers", "multiple_doi_versions"]
    failure_class: Literal["metadata_invariant"] = "metadata_invariant"
    node_id: str
    before: dict[str, object]
    after: dict[str, object]
    applied: bool = False
    warning: str | None = None
    timestamp: datetime

    @field_validator("node_id")
    @classmethod
    def node_id_is_valid(cls, value: str) -> str:
        return validate_node_id(value)


class GraphHygieneReport(_GraphModel):
    graph_name: str
    problem_id: str | None = None
    inspected_source_count: int = Field(ge=0)
    repair_requested: bool
    previous_revision: str
    new_revision: str
    actions: list[GraphHygieneAction] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    repair_log: str | None = None


class GraphNodeSummary(_GraphModel):
    matek_id: str
    node_type: NodeType
    title: str
    epistemic_status: EpistemicStatus
    workflow_status: WorkflowStatus
    path: str
    statement_version: int = 1
    invalidation_reasons: list[str] = Field(default_factory=list)


class GraphFrontier(_GraphModel):
    problem_id: str
    graph_revision: str
    main_target: GraphNodeSummary | None = None
    live_derivations: list[GraphNodeSummary] = Field(default_factory=list)
    strongest_audited_results: list[GraphNodeSummary] = Field(default_factory=list)
    open_obligations: list[GraphNodeSummary] = Field(default_factory=list)
    smallest_known_open_cut: list[GraphNodeSummary] = Field(default_factory=list)
    open_cut_search_capped: bool = False
    unresolved_claims: list[GraphNodeSummary] = Field(default_factory=list)
    candidate_proofs_awaiting_audit: list[GraphNodeSummary] = Field(default_factory=list)
    blocked_approaches: list[GraphNodeSummary] = Field(default_factory=list)
    unresolved_contradictions: list[GraphNodeSummary] = Field(default_factory=list)
    missing_dependencies: list[GraphNodeSummary] = Field(default_factory=list)
    high_value_tasks: list[GraphNodeSummary] = Field(default_factory=list)
    prior_runs: list[GraphNodeSummary] = Field(default_factory=list)
    refuted_or_unproductive_routes: list[GraphNodeSummary] = Field(default_factory=list)
    unverified_sources: list[GraphNodeSummary] = Field(default_factory=list)


class GraphContextNode(_GraphModel):
    summary: GraphNodeSummary
    body_excerpt: str
    outgoing: list[GraphEdge] = Field(default_factory=list)
    content_hash: str


class GraphContextSlice(_GraphModel):
    graph_revision: str
    problem_id: str
    task_id: str
    target_node_ids: list[str]
    exact_task: str
    nodes: list[GraphContextNode]
    omitted_node_count: int = 0


class GraphStatus(_GraphModel):
    graph_name: str
    initialized: bool
    vault_path: str
    revision: str | None
    node_count: int
    edge_count: int
    problem_count: int
    stale_count: int
    active_task_count: int
    last_change_at: datetime | None = None


class GraphDiff(_GraphModel):
    revision_a: str
    revision_b: str
    added_nodes: list[str]
    removed_nodes: list[str]
    changed_nodes: list[str]
    added_edges: list[GraphEdge]
    removed_edges: list[GraphEdge]


class GraphSnapshotVerification(_GraphModel):
    """Integrity result for one immutable graph revision snapshot."""

    revision: str
    schema_version: Literal[1, 2]
    integrity_root: str
    node_count: int = Field(ge=0)
    edge_count: int = Field(ge=0)
    checkpoint_revision: str | None = None
    legacy: bool
    artifact_path: str
    created_at: str

    @field_validator("revision", "checkpoint_revision")
    @classmethod
    def snapshot_revisions_are_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"\d{8}-[0-9a-f]{16}", value):
            raise ValueError("snapshot revision has an invalid format")
        return value

    @field_validator("integrity_root")
    @classmethod
    def integrity_root_is_valid(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("snapshot integrity root must be a lowercase SHA-256 value")
        return value


class GraphChangeRecord(_GraphModel):
    revision: str
    previous_revision: str
    run_id: str
    author: str
    timestamp: datetime
    reason: str
    operation_id: str
    changed_nodes: list[str]
    previous_hashes: dict[str, str | None]
    new_hashes: dict[str, str | None]
    source_artifacts: list[str] = Field(default_factory=list)


class GraphState(_GraphModel):
    schema_version: Literal[1] = 1
    graph_name: str
    revision_number: int = Field(default=0, ge=0)
    revision: str
    created_at: datetime
    updated_at: datetime
    problem_files: dict[str, str] = Field(default_factory=dict)
    node_paths: dict[str, str] = Field(default_factory=dict)
    node_hashes: dict[str, str] = Field(default_factory=dict)
    machine_hashes: dict[str, str] = Field(default_factory=dict)
    statement_hashes: dict[str, str] = Field(default_factory=dict)
    processed_operations: dict[str, GraphMergeResult] = Field(default_factory=dict)
    changes: list[GraphChangeRecord] = Field(default_factory=list)


__all__ = [
    "NODE_ID_PREFIXES",
    "NODE_ID_WORDS",
    "NODE_TYPE_DIRECTORIES",
    "ClaimType",
    "EpistemicStatus",
    "GraphChangeRecord",
    "GraphContextNode",
    "GraphContextSlice",
    "GraphDiff",
    "GraphEdge",
    "GraphFrontier",
    "GraphHygieneAction",
    "GraphHygieneReport",
    "GraphMergeResult",
    "GraphNode",
    "GraphNodeCreate",
    "GraphNodeSummary",
    "GraphNodeUpdate",
    "GraphPatch",
    "GraphSnapshotVerification",
    "GraphState",
    "GraphStatus",
    "GraphStatusChange",
    "GraphValidationIssue",
    "GraphValidationReport",
    "NodeType",
    "RelationType",
    "WorkflowStatus",
    "node_id_matches_type",
    "validate_node_id",
]
