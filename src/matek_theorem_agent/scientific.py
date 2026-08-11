"""Typed scientific results and deterministic target-contract checks.

The objects in this module deliberately contain no persistence or provider details.  A
research model reports mathematics in :class:`ScientificResult`; application code owns
run identities, stable graph IDs, revisions, provenance, and status promotion.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Sequence
from enum import StrEnum
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .graph_ids import normalize_id_description, validate_any_node_id


class _ScientificModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScientificResultKind(StrEnum):
    DEFINITION = "definition"
    LEMMA = "lemma"
    REDUCTION = "reduction"
    COUNTEREXAMPLE = "counterexample"
    COMPUTATION = "computation"
    SOURCE_FACT = "source_fact"


class ScientificResultDisposition(StrEnum):
    PARTIAL = "partial"
    PROPOSED_COMPLETE = "proposed_complete"
    REFUTED_MECHANISM = "refuted_mechanism"


class ScientificScope(StrEnum):
    MAIN = "main"
    REDUCTION = "reduction"
    BRANCH = "branch"
    COMPUTATION = "computation"


class BranchOutcome(StrEnum):
    PROGRESS = "progress"
    BLOCKED = "blocked"
    REFUTED = "refuted"
    CANDIDATE_COMPLETE = "candidate_complete"


_LOCAL_KEY = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SAFE_RELATIVE_COMPONENT = re.compile(r"\A(?!/)(?!.*(?:^|/)\.\.(?:/|$)).+\Z")
_PROPOSITION_PREFIX = re.compile(
    r"\A\s*(?:theorem|lemma|proposition|corollary|claim|conjecture|"
    r"for\s+(?:every|all|each|any)|there\s+exists?|if\b|∀|∃)",
    re.IGNORECASE,
)
_EXPLICIT_DEFINITION_FORMS = (
    re.compile(
        r"\A\s*(?:definition\s*[:.-]?\s*)?(?:we\s+)?define\s+.+?\s+"
        r"(?:to\s+mean|to\s+be|as)\s+.+\Z",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\A\s*(?:definition\s*[:.-]?\s*)?let\s+.+?\s+"
        r"(?:denote|mean|be)\s+.+\Z",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\A\s*(?:definition\s*[:.-]?\s*)?.+?\s+(?:is|are)\s+defined\s+"
        r"(?:as|to\s+be)\s+.+\Z",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"\A\s*[^\n]+?\s*(?::=|≔)\s*\S.+\Z", re.DOTALL),
)


def _normalize_local_key(value: str) -> str:
    normalized = value.strip()
    if not _LOCAL_KEY.fullmatch(normalized):
        raise ValueError("scientific local_key must use 1-128 portable identifier characters")
    return normalized


def _normalize_relative_artifact_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    if not normalized or not _SAFE_RELATIVE_COMPONENT.fullmatch(normalized):
        raise ValueError("artifact paths must be nonempty, relative, and contain no '..'")
    return normalized


def _normalize_node_ids(values: list[str]) -> list[str]:
    try:
        normalized = [validate_any_node_id(item) for item in values]
    except ValueError as exc:
        raise ValueError("scientific dependencies and targets must be stable node IDs") from exc
    return list(dict.fromkeys(normalized))


def normalize_exact_statement(value: str) -> str:
    """Return a conservative identity form for an exact mathematical statement.

    This normalization removes presentation-only Unicode and whitespace differences.  It
    intentionally does not attempt semantic equivalence: nontrivial merges still require
    an audit or an explicit equivalence derivation.
    """

    normalized = unicodedata.normalize("NFKC", value)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[ \t\f\v]+", " ", normalized)
    normalized = re.sub(r" *\n *", "\n", normalized)
    return normalized.strip()


def is_explicit_definition_declaration(value: str) -> bool:
    """Return whether text has a conservative, explicit definitional declaration form.

    Definitions are trusted notation-level premises in the canonical ledger.  This predicate
    deliberately rejects proposition/theorem syntax and requires an actual declaration marker;
    uncertain prose must be reported as a lemma or source fact and audited instead.
    """

    statement = normalize_exact_statement(value)
    if not statement or _PROPOSITION_PREFIX.match(statement):
        return False
    return any(pattern.fullmatch(statement) is not None for pattern in _EXPLICIT_DEFINITION_FORMS)


def exact_statement_fingerprint(value: str) -> str:
    return sha256(normalize_exact_statement(value).encode("utf-8")).hexdigest()


class ScientificArtifactDeclaration(_ScientificModel):
    """A worker-declared file and its replay contract.

    Paths are relative to the assignment-private workspace.  Hashes are deliberately not
    accepted from the worker; MATEK computes them while collecting the workspace.
    """

    path: str
    purpose: str
    supporting_result_keys: list[str] = Field(default_factory=list)
    command_line: list[str] = Field(default_factory=list)
    input_paths: list[str] = Field(default_factory=list)
    stdout_path: str | None = None
    stderr_path: str | None = None
    expected_output: str | None = None
    replay_recipe: str
    tool_versions: list[str] = Field(default_factory=list)

    @field_validator("path", "stdout_path", "stderr_path")
    @classmethod
    def paths_are_confined_syntax(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_relative_artifact_path(value)

    @field_validator("input_paths")
    @classmethod
    def input_paths_are_confined_syntax(cls, values: list[str]) -> list[str]:
        return [_normalize_relative_artifact_path(value) for value in values if value.strip()]

    @field_validator("purpose", "replay_recipe")
    @classmethod
    def required_text_is_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("artifact purpose and replay recipe must not be blank")
        return normalized

    @field_validator("command_line", "supporting_result_keys", "tool_versions")
    @classmethod
    def string_lists_are_normalized(cls, values: list[str]) -> list[str]:
        normalized = [item.strip() for item in values if item.strip()]
        return list(dict.fromkeys(normalized))


class ScientificResult(_ScientificModel):
    schema_version: Literal[1] = 1
    local_key: str
    kind: ScientificResultKind
    exact_statement: str
    scope: ScientificScope
    one_liner: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    proof_or_certificate: str
    exact_gap: str | None = None
    dependency_node_ids: list[str] = Field(default_factory=list)
    dependency_result_keys: list[str] = Field(default_factory=list)
    target_node_ids: list[str] = Field(default_factory=list)
    disposition: ScientificResultDisposition

    @field_validator("local_key")
    @classmethod
    def local_key_is_portable(cls, value: str) -> str:
        return _normalize_local_key(value)

    @field_validator("one_liner")
    @classmethod
    def one_liner_is_normalized(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_id_description(value)
        return normalized or None

    @field_validator("exact_statement", "proof_or_certificate")
    @classmethod
    def scientific_text_is_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("scientific results require an exact statement and evidence")
        return normalized

    @field_validator("exact_gap")
    @classmethod
    def exact_gap_is_normalized(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("dependency_node_ids", "target_node_ids")
    @classmethod
    def node_ids_are_valid(cls, values: list[str]) -> list[str]:
        return _normalize_node_ids(values)

    @field_validator("dependency_result_keys")
    @classmethod
    def result_keys_are_valid(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(_normalize_local_key(value) for value in values))

    @field_validator("assumptions")
    @classmethod
    def assumptions_are_normalized(cls, values: list[str]) -> list[str]:
        normalized = [item.strip() for item in values if item.strip()]
        return list(dict.fromkeys(normalized))

    @model_validator(mode="after")
    def gap_and_disposition_are_consistent(self) -> ScientificResult:
        if self.kind is ScientificResultKind.DEFINITION:
            if self.scope is not ScientificScope.BRANCH:
                raise ValueError("scientific definitions must be branch-scoped declarations")
            if not is_explicit_definition_declaration(self.exact_statement):
                raise ValueError(
                    "scientific definitions require explicit definitional declaration syntax; "
                    "theorem and proposition assertions must use a claim-bearing result kind"
                )
            if self.dependency_node_ids or self.dependency_result_keys:
                raise ValueError(
                    "scientific definitions are notation declarations and cannot declare "
                    "mathematical dependencies"
                )
            if self.assumptions:
                raise ValueError(
                    "scientific definitions cannot carry unbound mathematical assumptions"
                )
        if (
            self.exact_gap is not None
            and self.disposition is ScientificResultDisposition.PROPOSED_COMPLETE
        ):
            raise ValueError("a result with an exact gap is a proof attempt, not proposed_complete")
        if (
            self.kind is ScientificResultKind.COUNTEREXAMPLE
            and self.disposition is not ScientificResultDisposition.REFUTED_MECHANISM
        ):
            raise ValueError("counterexamples default to refuted_mechanism disposition")
        if self.kind is ScientificResultKind.COUNTEREXAMPLE and self.exact_gap is not None:
            raise ValueError(
                "an incomplete counterexample cannot create a refutation; report its gap as an "
                "explicit obligation instead"
            )
        if (
            self.disposition is ScientificResultDisposition.REFUTED_MECHANISM
            and self.kind is not ScientificResultKind.COUNTEREXAMPLE
        ):
            raise ValueError("refuted_mechanism is reserved for explicit counterexamples")
        return self


def validate_result_dependency_dag(results: Sequence[ScientificResult]) -> list[str]:
    """Validate branch-local result references and return a dependency-first order.

    ``dependency_node_ids`` name graph records that predate the report.  This companion DAG
    resolves references between results in the same immutable report without asking a model to
    predict application-owned graph IDs.
    """

    by_key = {result.local_key: result for result in results}
    if len(by_key) != len(results):
        raise ValueError("scientific result local_key values must be unique")
    for result in results:
        unknown = sorted(set(result.dependency_result_keys) - set(by_key))
        if unknown:
            raise ValueError(
                f"scientific result {result.local_key!r} references unknown local result "
                "key(s): " + ", ".join(unknown)
            )
        if result.local_key in result.dependency_result_keys:
            raise ValueError(f"scientific result {result.local_key!r} cannot depend on itself")

    state: dict[str, int] = {}
    stack: list[str] = []
    ordered: list[str] = []

    def visit(local_key: str) -> None:
        marker = state.get(local_key, 0)
        if marker == 2:
            return
        if marker == 1:
            cycle_start = stack.index(local_key)
            cycle = [*stack[cycle_start:], local_key]
            raise ValueError("scientific result dependency cycle: " + " -> ".join(cycle))
        state[local_key] = 1
        stack.append(local_key)
        for dependency_key in by_key[local_key].dependency_result_keys:
            visit(dependency_key)
        stack.pop()
        state[local_key] = 2
        ordered.append(local_key)

    for result in results:
        visit(result.local_key)
    return ordered


def transitive_result_dependency_keys(
    results: Sequence[ScientificResult],
    root_keys: Iterable[str],
) -> list[str]:
    """Return the sorted strict transitive closure of branch-local result dependencies."""

    validate_result_dependency_dag(results)
    by_key = {result.local_key: result for result in results}
    roots = list(dict.fromkeys(root_keys))
    unknown_roots = sorted(set(roots) - set(by_key))
    if unknown_roots:
        raise ValueError(
            "scientific result dependency closure has unknown root key(s): "
            + ", ".join(unknown_roots)
        )
    closure: set[str] = set()
    pending = list(roots)
    while pending:
        local_key = pending.pop()
        for dependency_key in by_key[local_key].dependency_result_keys:
            if dependency_key in closure:
                continue
            closure.add(dependency_key)
            pending.append(dependency_key)
    return sorted(closure)


class ScientificObligationDeclaration(_ScientificModel):
    schema_version: Literal[1] = 1
    local_key: str
    exact_statement: str
    one_liner: str | None = None
    quantifiers: list[str] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    conclusion: str
    parent_result_keys: list[str] = Field(default_factory=list)
    dependency_node_ids: list[str] = Field(default_factory=list)
    scope: ScientificScope = ScientificScope.BRANCH
    notation_definition_version: str = "1"
    falsification_evidence: list[str] = Field(default_factory=list)
    estimated_leverage: int = Field(default=0, ge=0, le=100)

    @field_validator("local_key")
    @classmethod
    def local_key_is_portable(cls, value: str) -> str:
        return _normalize_local_key(value)

    @field_validator("one_liner")
    @classmethod
    def one_liner_is_normalized(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_id_description(value)
        return normalized or None

    @field_validator("exact_statement", "conclusion", "notation_definition_version")
    @classmethod
    def required_text_is_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("obligation statements and versions must not be blank")
        return normalized

    @field_validator("dependency_node_ids")
    @classmethod
    def dependency_ids_are_valid(cls, values: list[str]) -> list[str]:
        return _normalize_node_ids(values)

    @field_validator(
        "quantifiers",
        "hypotheses",
        "parent_result_keys",
        "falsification_evidence",
    )
    @classmethod
    def string_lists_are_normalized(cls, values: list[str]) -> list[str]:
        normalized = [item.strip() for item in values if item.strip()]
        return list(dict.fromkeys(normalized))


class TargetClauseCategory(StrEnum):
    QUANTIFIERS = "quantifiers"
    CONSTANTS = "constants"
    ADDITIVE_TERMS = "additive_terms"
    DOMAIN = "domain"
    INFORMATION_MODEL = "information_model"
    ONLINE_DECISIONS = "online_decisions"
    FEASIBILITY = "feasibility"
    RANDOMNESS = "randomness"
    EDGE_CASES = "edge_cases"
    POLARITY = "polarity"
    CONCLUSION = "conclusion"
    OTHER = "other"


class TargetPolarity(StrEnum):
    """Structured requested-outcome value used for local polarity comparison."""

    AFFIRMATIVE_PROOF = "affirmative_proof"
    DISPROOF = "disproof"
    CLASSIFICATION = "classification"
    CONSTRUCTION = "construction"
    INVESTIGATION = "investigation"
    AMBIGUOUS = "ambiguous"


class PolarityDecision(_ScientificModel):
    """Auditable record of the local, structured polarity-alignment decision."""

    gate: Literal["target_polarity_alignment"] = "target_polarity_alignment"
    contract_clause_key: str
    contract_polarity: TargetPolarity
    statement_polarity: TargetPolarity
    decision_rule: str
    material_contradiction: bool
    detail: str


class AlgorithmRandomization(StrEnum):
    ALLOWED_OR_REQUIRED = "allowed_or_required"
    DETERMINISTIC_ONLY = "deterministic_only"
    UNSPECIFIED = "unspecified"


class ArrivalRandomness(StrEnum):
    UNIFORM_RANDOM_PERMUTATION = "uniform_random_permutation"
    ADVERSARIAL_OR_DETERMINISTIC_ORDER = "adversarial_or_deterministic_order"
    UNSPECIFIED = "unspecified"


class WeightAdversary(StrEnum):
    OBLIVIOUS_BEFORE_RANDOMNESS = "oblivious_before_randomness"
    ADAPTIVE_AFTER_RANDOMNESS = "adaptive_after_randomness"
    UNSPECIFIED = "unspecified"


class FeasibilityRequirement(StrEnum):
    PATHWISE = "pathwise"
    IN_EXPECTATION = "in_expectation"
    HIGH_PROBABILITY = "high_probability"
    UNSPECIFIED = "unspecified"


class ValueGuarantee(StrEnum):
    IN_EXPECTATION = "in_expectation"
    PATHWISE = "pathwise"
    HIGH_PROBABILITY = "high_probability"
    UNSPECIFIED = "unspecified"


class RandomnessFacts(_ScientificModel):
    """Orthogonal randomness and execution-wise requirements for one target encoding."""

    algorithm_randomization: AlgorithmRandomization = AlgorithmRandomization.UNSPECIFIED
    arrival_randomness: ArrivalRandomness = ArrivalRandomness.UNSPECIFIED
    weight_adversary: WeightAdversary = WeightAdversary.UNSPECIFIED
    expectation_over: list[Literal["arrival_order", "algorithm_coins"]] = Field(
        default_factory=list
    )
    feasibility_requirement: FeasibilityRequirement = FeasibilityRequirement.UNSPECIFIED
    value_guarantee: ValueGuarantee = ValueGuarantee.UNSPECIFIED


class RandomnessDecision(_ScientificModel):
    """Auditable comparison that keeps random sources separate from pathwise invariants."""

    gate: Literal["target_randomness_alignment"] = "target_randomness_alignment"
    contract_clause_keys: list[str]
    contract: RandomnessFacts
    statement: RandomnessFacts
    decision_rule: str
    material_contradiction: bool
    material_conflicts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    detail: str


class TargetClauseCheck(_ScientificModel):
    key: str
    category: TargetClauseCategory
    passed: bool
    detail: str
    contract_values: dict[str, str | list[str]] = Field(default_factory=dict)
    statement_values: dict[str, str | list[str]] = Field(default_factory=dict)
    material_conflicts: list[str] = Field(default_factory=list)


class AlignmentWarningOrigin(StrEnum):
    """Origin of a non-authoritative target-alignment observation."""

    DETERMINISTIC_STRUCTURED = "deterministic_structured"
    HEURISTIC_EXTRACTOR = "heuristic_extractor"
    REGEX = "regex"
    LLM = "LLM"
    LEGACY_RECOVERY = "legacy_recovery"


class AlignmentWarning(_ScientificModel):
    """A visible concern that cannot by itself prevent research."""

    warning_id: str
    origin: AlignmentWarningOrigin
    clause_keys: list[str] = Field(default_factory=list)
    observation: str
    statement_sha256: str
    contract_sha256: str


class MaterialityVerdict(StrEnum):
    NO_MATERIAL_CONFLICT = "NO_MATERIAL_CONFLICT"
    CONFIRMED_CONFLICT = "CONFIRMED_CONFLICT"


class TargetMaterialityAssessment(_ScientificModel):
    """Small structured output requested from the independent semantic reviewer."""

    verdict: MaterialityVerdict
    rationale: str
    clause_keys: list[str] = Field(default_factory=list)

    @field_validator("rationale")
    @classmethod
    def rationale_is_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("materiality rationale must not be blank")
        return normalized

    @model_validator(mode="after")
    def confirmed_conflict_names_clauses(self) -> TargetMaterialityAssessment:
        if self.verdict is MaterialityVerdict.CONFIRMED_CONFLICT and not self.clause_keys:
            raise ValueError(
                "a confirmed conflict must identify at least one claim-contract clause"
            )
        return self


class MaterialityReviewRecord(_ScientificModel):
    """Durable result and provenance for one bounded materiality review."""

    status: Literal["completed", "unavailable"]
    verdict: MaterialityVerdict | None = None
    rationale: str
    clause_keys: list[str] = Field(default_factory=list)
    response_id: str | None = None
    model: str
    reasoning_effort: str

    @model_validator(mode="after")
    def completed_review_has_verdict(self) -> MaterialityReviewRecord:
        if self.status == "completed" and self.verdict is None:
            raise ValueError("a completed materiality review must include a verdict")
        return self


class TargetContractAlignment(_ScientificModel):
    schema_version: Literal[2] = 2
    statement_sha256: str
    contract_sha256: str
    passed: bool
    checks: list[TargetClauseCheck]
    blocking_issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    alignment_warnings: list[AlignmentWarning] = Field(default_factory=list)
    materiality_review: MaterialityReviewRecord | None = None
    polarity: PolarityDecision | None = None
    randomness: RandomnessDecision | None = None


_QUANTIFIER_KEYS = ("quantifier", "uniformity")
_CONSTANT_KEYS = ("constant", "parameter", "coefficient")
_ADDITIVE_KEYS = ("additive", "offset", "slack")
_DOMAIN_KEYS = ("domain", "space", "instance", "object", "metric", "finite", "arbitrary")
_INFORMATION_KEYS = ("information", "known", "oracle", "revealed", "unseen")
_ONLINE_DECISION_KEYS = ("online decision", "decision", "irrevocable", "immediate", "operation")
_FEASIBILITY_KEYS = ("feasibility", "feasible", "independence", "invariant")
_RANDOMNESS_KEYS = (
    "randomness",
    "randomization",
    "randomisation",
    "randomized",
    "randomised",
    "stochastic",
    "arrival",
    "coin",
)
_EDGE_KEYS = ("edge", "boundary", "exception", "degenerate", "zero", "empty")
_POLARITY_KEYS = ("polarity", "posture", "prove", "refute", "disprove")
_CONCLUSION_KEYS = ("conclusion", "target", "guarantee", "benchmark", "bound", "inequality")
_STRUCTURAL_CLAUSE_KEYS = {
    "clause",
    "clauses",
    "description",
    "family",
    "included",
    "includes",
    "item",
    "items",
    "kind",
    "name",
    "requirement",
    "requirements",
    "type",
    "value",
    "values",
}


def _plain_math(value: str) -> str:
    # Some structured-output transports have emitted a malformed Unicode escape as
    # U+001C followed by four hexadecimal digits (for example ``\x1c03c0`` for π,
    # or ``\x1c22650`` for ≥ followed by 0). Decode that unambiguous artifact only
    # for lexical comparison; the persisted model output remains byte-for-byte
    # available for diagnosis.
    value = re.sub(
        r"\x1c([0-9a-fA-F]{4})",
        lambda match: chr(int(match.group(1), 16)),
        value,
    )
    normalized = normalize_exact_statement(value).casefold()
    replacements = {
        "\\forall": " forall ",
        "∀": " forall ",
        "\\exists": " exists ",
        "∃": " exists ",
        "\\leq": " <= ",
        "\\le": " <= ",
        "≤": " <= ",
        "\\geq": " >= ",
        "\\ge": " >= ",
        "≥": " >= ",
        "\N{MINUS SIGN}": "-",
    }
    for source, target in replacements.items():
        normalized = normalized.replace(source, target)
    normalized = re.sub(r"[{}$`]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _clause_fragments(value: str, *, clause_key: str | None = None) -> list[str]:
    """Return semantic scalar fragments from a plain or JSON-encoded clause.

    ``ClaimContract`` keeps its provider-facing schema closed by serializing legacy list and
    object values as JSON strings. Validation must inspect those scalar values instead of
    treating JSON punctuation as theorem content. Meaningful mapping keys are retained, while
    generic structural wrappers such as ``values`` and ``description`` are ignored.
    """

    try:
        decoded: object = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return [value]

    if isinstance(decoded, bool):
        key = (clause_key or "").replace("_", " ").replace("-", " ").strip()
        if key and key.casefold() not in _STRUCTURAL_CLAUSE_KEYS:
            return [key if decoded else f"not {key}"]
        return []

    fragments: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for raw_key, child in item.items():
                key = str(raw_key).replace("_", " ").replace("-", " ").strip()
                if isinstance(child, bool):
                    if key and key.casefold() not in _STRUCTURAL_CLAUSE_KEYS:
                        fragments.append(key if child else f"not {key}")
                    continue
                if key and key.casefold() not in _STRUCTURAL_CLAUSE_KEYS:
                    fragments.append(key)
                visit(child)
            return
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if isinstance(item, str):
            if item.strip():
                fragments.append(item)
            return
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            fragments.append(str(item))

    visit(decoded)
    return list(dict.fromkeys(fragments))


def _clause_category(key: str, value: str) -> TargetClauseCategory:
    def classify(material: str) -> TargetClauseCategory | None:
        if any(marker in material for marker in _ADDITIVE_KEYS) or re.search(
            r"\+\s*[a-z\u03b1-\u03c9]", material
        ):
            return TargetClauseCategory.ADDITIVE_TERMS
        if any(marker in material for marker in _QUANTIFIER_KEYS) or re.search(
            r"\b(?:for all|for every|forall|exists|there exists)\b", material
        ):
            return TargetClauseCategory.QUANTIFIERS
        if any(marker in material for marker in _POLARITY_KEYS):
            return TargetClauseCategory.POLARITY
        if any(marker in material for marker in _RANDOMNESS_KEYS):
            return TargetClauseCategory.RANDOMNESS
        if any(marker in material for marker in _FEASIBILITY_KEYS):
            return TargetClauseCategory.FEASIBILITY
        if any(marker in material for marker in _INFORMATION_KEYS):
            return TargetClauseCategory.INFORMATION_MODEL
        if any(marker in material for marker in _ONLINE_DECISION_KEYS):
            return TargetClauseCategory.ONLINE_DECISIONS
        if any(marker in material for marker in _EDGE_KEYS):
            return TargetClauseCategory.EDGE_CASES
        if any(marker in material for marker in _DOMAIN_KEYS):
            return TargetClauseCategory.DOMAIN
        if any(marker in material for marker in _CONSTANT_KEYS):
            return TargetClauseCategory.CONSTANTS
        if any(marker in material for marker in _CONCLUSION_KEYS):
            return TargetClauseCategory.CONCLUSION
        return None

    # The provider is instructed to use semantic clause keys. Prefer that explicit
    # structure over incidental prose: a ``constants`` clause mentioning an
    # "instance parameter" is not a domain clause, and a ``conclusion`` beginning
    # "for every" is still a conclusion.
    key_category = classify(key.replace("_", " ").replace("-", " ").casefold())
    if key_category is not None:
        return key_category
    material = value.casefold()
    return classify(material) or TargetClauseCategory.OTHER


_SYMBOLIC_QUANTIFIER_NAMES = {
    "alpha",
    "beta",
    "delta",
    "epsilon",
    "eta",
    "gamma",
    "iota",
    "kappa",
    "lambda",
    "mu",
    "nu",
    "omega",
    "phi",
    "pi",
    "psi",
    "rho",
    "sigma",
    "tau",
    "theta",
    "xi",
    "zeta",
}


def _likely_quantified_symbol(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) == 1 or value in _SYMBOLIC_QUANTIFIER_NAMES or re.search(r"[_0-9]", value):
        return value
    return None


def _quantifier_requirements(value: str) -> list[tuple[str, str | None]]:
    plain = _plain_math(value)
    requirements: list[tuple[str, str | None]] = []
    pattern = re.compile(
        r"\b(forall|for all|for every|each|exists|there exists?|there must exists?|"
        r"must exists?|there is|there are)\b"
        r"(?:\s+(?:(?:an?|one)\s+)?([a-z][a-z0-9_]*))?"
    )
    for kind, variable in pattern.findall(plain):
        normalized_kind = (
            "exists" if "exist" in kind or kind in {"there is", "there are"} else "forall"
        )
        requirements.append((normalized_kind, _likely_quantified_symbol(variable or None)))
    return list(dict.fromkeys(requirements))


def _has_quantifier(statement: str, kind: str, variable: str | None) -> bool:
    plain = _plain_math(statement)
    marker = (
        r"(?:exists?|there\s+(?:must\s+)?exists?|must\s+exists?|there\s+(?:is|are))"
        if kind == "exists"
        else r"(?:forall|for all|for every|each|any)"
    )
    if variable:
        return bool(re.search(rf"\b{marker}\s+(?:an?\s+)?{re.escape(variable)}\b", plain))
    return bool(re.search(rf"\b{marker}\b", plain))


def _comparison_lexemes(value: str) -> list[str]:
    """Return the material syntax of one comparison side.

    Operators are retained so that, for example, ``a * b + c`` is not treated as
    interchangeable with ``a + b * c`` merely because the identifiers occur in the same
    order.  Parentheses are deliberately ignored: the target-contract gate is a conservative
    lexical alignment check, not an algebraic equivalence prover.
    """

    return re.findall(
        r"[a-z\u03b1-\u03c9][a-z0-9_]*|\d+(?:\.\d+)?|[+*/^-]",
        value,
    )


def _comparison_aligned(statement: str, clause: str) -> bool:
    expected = re.search(r"(.+?)\s*(<=|>=|=|<|>)\s*(.+)", clause)
    if expected is None:
        return True
    expected_left_text = re.split(r"(?:[.;:]|\b(?:and|then)\b)", expected.group(1))[-1]
    expected_right_text = re.split(r"(?:[,;.]|\bwhere\b)", expected.group(3))[0]
    expected_left = _comparison_lexemes(expected_left_text)
    expected_right = _comparison_lexemes(expected_right_text)
    if not expected_left or not expected_right:
        return True
    operator = expected.group(2)
    for candidate in re.finditer(r"<=|>=|(?<![<>])=(?!=)|<|>", statement):
        if candidate.group(0) != operator:
            continue
        candidate_left = _comparison_lexemes(statement[: candidate.start()])
        candidate_right = _comparison_lexemes(statement[candidate.end() :])
        remaining_right = candidate_right[len(expected_right) :]
        # Introductory theorem prose and earlier comparisons may precede the left-hand
        # expression. Match the expected expression at the end of that prefix and at the
        # beginning of the right side. Ordinary prose may follow; an immediate arithmetic
        # operator may not, since it materially extends the requested expression.
        if (
            len(candidate_left) >= len(expected_left)
            and candidate_left[-len(expected_left) :] == expected_left
            and candidate_right[: len(expected_right)] == expected_right
            and (not remaining_right or remaining_right[0] not in {"+", "-", "*", "/", "^"})
        ):
            return True
    return False


def _comparison_direction_conflict(statement: str, clause: str) -> bool:
    """Return true only for a visible reversal of the same comparison left side."""

    opposites = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}
    statement_matches = list(re.finditer(r"<=|>=|(?<![<>])=(?!=)|<|>", statement))
    for expected in re.finditer(r"<=|>=|(?<![<>])=(?!=)|<|>", clause):
        opposite = opposites.get(expected.group(0))
        if opposite is None:
            continue
        expected_left_text = re.split(
            r"(?:[.;:]|\b(?:and|then|satisfies)\b)", clause[: expected.start()]
        )[-1]
        expected_left = _comparison_lexemes(expected_left_text)
        if not expected_left:
            continue
        if any(
            candidate.group(0) == opposite
            and _comparison_lexemes(statement[: candidate.start()])[-len(expected_left) :]
            == expected_left
            for candidate in statement_matches
        ):
            return True
    return False


def _quantifier_conflicts(statement: str, fragment: str) -> list[str]:
    conflicts: list[str] = []
    for kind, variable in _quantifier_requirements(fragment):
        opposite = "forall" if kind == "exists" else "exists"
        if not _has_quantifier(statement, kind, variable) and _has_quantifier(
            statement, opposite, variable
        ):
            conflicts.append(f"{kind} {variable or ''}".strip())
    return conflicts


def _algorithm_randomization(value: str) -> AlgorithmRandomization:
    plain = _plain_math(value)
    if "allowed_or_required" in plain:
        return AlgorithmRandomization.ALLOWED_OR_REQUIRED
    randomized_algorithm = re.search(
        r"\b(?:randomized|randomised|stochastic)\s+"
        r"(?:(?:causal|online)\s+){0,2}(?:algorithm|policy|alg)\b",
        plain,
    )
    algorithm_coins = re.search(
        r"\b(?:algorithm|policy|alg)(?:'s|s)?(?:\s+(?:private|internal))?\s+"
        r"(?:randomness|random coins?|coins?|random seed)\b",
        plain,
    )
    coins_permitted = re.search(
        r"\b(?:may|must|shall|can)\s+(?:also\s+)?(?:use|draw)\s+"
        r"(?:private\s+|internal\s+)?(?:randomness|random coins?|coins?)\b",
        plain,
    )
    if randomized_algorithm or algorithm_coins or coins_permitted:
        return AlgorithmRandomization.ALLOWED_OR_REQUIRED

    deterministic_algorithm = re.search(
        r"\bdeterministic(?:-only|\s+only)?\s+"
        r"(?:(?:causal|online)\s+){0,2}(?:algorithm|policy|alg)\b",
        plain,
    ) or re.search(
        r"\b(?:algorithm|policy|alg)\s+"
        r"(?:must|shall|is|may|can|is required to)\s+(?:be\s+)?deterministic\b",
        plain,
    )
    coins_forbidden = re.search(
        r"\b(?:may not|must not|shall not|cannot|can not|does not|no)\b[^.;]{0,48}"
        r"\b(?:use|have|draw)?\s*(?:any\s+)?(?:private\s+|internal\s+)?"
        r"(?:randomness|random coins?|coins?)\b",
        plain,
    )
    if (
        "deterministic_only" in plain
        or deterministic_algorithm
        or coins_forbidden
        or re.search(r"\bnot randomized\b", plain)
    ):
        return AlgorithmRandomization.DETERMINISTIC_ONLY
    return AlgorithmRandomization.UNSPECIFIED


def _arrival_randomness(value: str) -> ArrivalRandomness:
    plain = _plain_math(value)
    if "uniform_random_permutation" in plain:
        return ArrivalRandomness.UNIFORM_RANDOM_PERMUTATION
    if "adversarial_or_deterministic_order" in plain:
        return ArrivalRandomness.ADVERSARIAL_OR_DETERMINISTIC_ORDER
    uniform = re.search(
        r"\b(?:uniform(?:ly)?\s+random(?:ly)?|random[- ]order)\b[^.;]{0,40}"
        r"\b(?:arrival|arrivals|order|permutation)\b",
        plain,
    ) or re.search(
        r"\b(?:arrival|arrivals|order|permutation)\b[^.;]{0,40}"
        r"\b(?:uniform(?:ly)?\s+random(?:ly)?|random[- ]order)\b",
        plain,
    )
    adversarial = re.search(
        r"\b(?:adversarial|adversarially chosen|deterministic)\b[^.;]{0,24}"
        r"\b(?:arrival|arrivals|arrival order|order|permutation)\b",
        plain,
    ) or re.search(
        r"\b(?:arrival|arrivals|arrival order|order|permutation)\b[^.;]{0,24}"
        r"\b(?:adversarial|adversarially chosen|deterministic)\b",
        plain,
    )
    if uniform and not adversarial:
        return ArrivalRandomness.UNIFORM_RANDOM_PERMUTATION
    if adversarial and not uniform:
        return ArrivalRandomness.ADVERSARIAL_OR_DETERMINISTIC_ORDER
    return ArrivalRandomness.UNSPECIFIED


def _weight_adversary(value: str) -> WeightAdversary:
    plain = _plain_math(value)
    if "oblivious_before_randomness" in plain:
        return WeightAdversary.OBLIVIOUS_BEFORE_RANDOMNESS
    if "adaptive_after_randomness" in plain:
        return WeightAdversary.ADAPTIVE_AFTER_RANDOMNESS
    oblivious = re.search(r"\boblivious adversar", plain) or re.search(
        r"\b(?:weights?|w)\b[^.;]{0,80}\b(?:fixed|chosen|committed)\b[^.;]{0,40}"
        r"\bbefore\b[^.;]{0,40}\b(?:randomness|coins?|arrival|permutation|order)\b",
        plain,
    )
    adaptive = re.search(
        r"\b(?:weights?|weight adversary|adversary)\b[^.;]{0,80}"
        r"\b(?:adaptive|after seeing|after observing|depends? on)\b[^.;]{0,48}"
        r"\b(?:randomness|coins?|arrival|permutation|order|seed)\b",
        plain,
    )
    if oblivious and not adaptive:
        return WeightAdversary.OBLIVIOUS_BEFORE_RANDOMNESS
    if adaptive and not oblivious:
        return WeightAdversary.ADAPTIVE_AFTER_RANDOMNESS
    return WeightAdversary.UNSPECIFIED


def _feasibility_requirement(value: str) -> FeasibilityRequirement:
    plain = _plain_math(value)
    if "pathwise" in plain and not re.search(r"\b(?:value|reward|weight)\b", plain):
        return FeasibilityRequirement.PATHWISE
    if "in_expectation" in plain and "feasibility" in plain:
        return FeasibilityRequirement.IN_EXPECTATION
    if "high_probability" in plain and "feasibility" in plain:
        return FeasibilityRequirement.HIGH_PROBABILITY
    feasibility_subject = re.search(
        r"\b(?:feasib(?:le|ility)|independen(?:t|ce)|accepted (?:prefix|set))\b",
        plain,
    )
    if not feasibility_subject:
        return FeasibilityRequirement.UNSPECIFIED
    high_probability = re.search(
        r"\b(?:feasib(?:le|ility)|independen(?:t|ce))\b[^.;]{0,60}"
        r"\b(?:with high probability|probably)\b",
        plain,
    )
    expected = re.search(
        r"\b(?:expected feasibility|feasib(?:le|ility)\s+(?:only\s+)?in expectation)\b",
        plain,
    )
    pathwise = re.search(
        r"\b(?:pathwise|pointwise|for every realization|in every realization|"
        r"for each realization|deterministically conditional on every realization)\b",
        plain,
    ) or re.search(
        r"\b(?:feasibility|independence)\b[^.;]{0,32}\bdeterministically\b",
        plain,
    )
    if pathwise:
        return FeasibilityRequirement.PATHWISE
    if expected:
        return FeasibilityRequirement.IN_EXPECTATION
    if high_probability:
        return FeasibilityRequirement.HIGH_PROBABILITY
    return FeasibilityRequirement.UNSPECIFIED


def _value_guarantee(value: str) -> ValueGuarantee:
    plain = _plain_math(value)
    if "in_expectation" in plain:
        return ValueGuarantee.IN_EXPECTATION
    if "pathwise_value" in plain:
        return ValueGuarantee.PATHWISE
    if "high_probability_value" in plain:
        return ValueGuarantee.HIGH_PROBABILITY
    expected_feasibility_only = bool(
        re.search(r"\b(?:expected feasibility|feasib(?:le|ility)\s+in expectation)\b", plain)
    ) and not re.search(r"\b(?:value|weight|reward|cost|objective|guarantee|bound)\b", plain)
    if not expected_feasibility_only and re.search(r"\b(?:expectation|expected)\b|\be_", plain):
        return ValueGuarantee.IN_EXPECTATION
    if re.search(
        r"\b(?:value|weight|reward|cost|objective|guarantee|bound)\b[^.;]{0,80}"
        r"\b(?:pathwise|pointwise|in every realization|for every realization)\b",
        plain,
    ) or re.search(
        r"\b(?:pathwise|pointwise|in every realization|for every realization)\b[^.;]{0,80}"
        r"\b(?:value|weight|reward|cost|objective|guarantee|bound)\b",
        plain,
    ):
        return ValueGuarantee.PATHWISE
    if re.search(
        r"\b(?:value|weight|reward|cost|objective|guarantee|bound)\b[^.;]{0,80}"
        r"\bwith high probability\b",
        plain,
    ):
        return ValueGuarantee.HIGH_PROBABILITY
    return ValueGuarantee.UNSPECIFIED


def _expectation_sources(value: str) -> list[Literal["arrival_order", "algorithm_coins"]]:
    plain = _plain_math(value)
    if not re.search(r"\b(?:expectation|expected)\b|\be_", plain):
        return []
    sources: list[Literal["arrival_order", "algorithm_coins"]] = []
    expectation_spans = " ".join(
        match.group(0)
        for match in re.finditer(
            r"(?:expectation|expected|e_)[^.;]{0,180}",
            plain,
        )
    )
    if re.search(
        r"\b(?:arrival|arrival_order|permutation|uniform order|random order|pi)\b|\bπ\b",
        expectation_spans,
    ):
        sources.append("arrival_order")
    if re.search(
        r"\b(?:algorithm coins?|algorithm_coins|random coins?|private randomness|"
        r"internal randomness|seed)\b",
        expectation_spans,
    ) or re.search(r"\be_\s*(?:π|pi)?\s*,?\s*r\b", expectation_spans):
        sources.append("algorithm_coins")
    return sources


def _randomness_facts(value: str) -> RandomnessFacts:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict) and any(key in RandomnessFacts.model_fields for key in decoded):
        try:
            return RandomnessFacts.model_validate(decoded)
        except ValueError:
            # Malformed model output is uncertain rather than a keyword-derived hard stop.
            pass
    return RandomnessFacts(
        algorithm_randomization=_algorithm_randomization(value),
        arrival_randomness=_arrival_randomness(value),
        weight_adversary=_weight_adversary(value),
        expectation_over=_expectation_sources(value),
        feasibility_requirement=_feasibility_requirement(value),
        value_guarantee=_value_guarantee(value),
    )


def _randomness_clause_facts(key: str, raw_value: str) -> RandomnessFacts:
    direct = _randomness_facts(raw_value)
    if direct != RandomnessFacts():
        return direct
    return _randomness_facts("\n".join(_clause_fragments(raw_value, clause_key=key)))


def _merge_randomness_facts(facts: Iterable[RandomnessFacts]) -> RandomnessFacts:
    merged = RandomnessFacts()
    for item in facts:
        updates: dict[str, object] = {}
        for field in (
            "algorithm_randomization",
            "arrival_randomness",
            "weight_adversary",
            "feasibility_requirement",
            "value_guarantee",
        ):
            incoming = getattr(item, field)
            current = getattr(merged, field)
            if current.value == "unspecified" and incoming.value != "unspecified":
                updates[field] = incoming
        if item.expectation_over:
            updates["expectation_over"] = list(
                dict.fromkeys([*merged.expectation_over, *item.expectation_over])
            )
        if updates:
            merged = merged.model_copy(update=updates)
    return merged


def _randomness_conflicts(
    contract: RandomnessFacts,
    statement: RandomnessFacts,
) -> list[str]:
    conflicts: list[str] = []
    if (
        contract.algorithm_randomization is AlgorithmRandomization.ALLOWED_OR_REQUIRED
        and statement.algorithm_randomization is AlgorithmRandomization.DETERMINISTIC_ONLY
    ):
        conflicts.append(
            "algorithm_randomization: contract=allowed_or_required, "
            "statement=deterministic_only (not randomized)"
        )
    elif (
        contract.algorithm_randomization is AlgorithmRandomization.DETERMINISTIC_ONLY
        and statement.algorithm_randomization is AlgorithmRandomization.ALLOWED_OR_REQUIRED
    ):
        conflicts.append(
            "algorithm_randomization: contract=deterministic_only (not randomized), "
            "statement=allowed_or_required"
        )
    if (
        contract.arrival_randomness is ArrivalRandomness.UNIFORM_RANDOM_PERMUTATION
        and statement.arrival_randomness is ArrivalRandomness.ADVERSARIAL_OR_DETERMINISTIC_ORDER
    ):
        conflicts.append(
            "arrival_randomness: contract=uniform_random_permutation, "
            "statement=adversarial_or_deterministic_order"
        )
    elif (
        contract.arrival_randomness is ArrivalRandomness.ADVERSARIAL_OR_DETERMINISTIC_ORDER
        and statement.arrival_randomness is ArrivalRandomness.UNIFORM_RANDOM_PERMUTATION
    ):
        conflicts.append(
            "arrival_randomness: contract=adversarial_or_deterministic_order, "
            "statement=uniform_random_permutation"
        )
    if (
        contract.weight_adversary is WeightAdversary.OBLIVIOUS_BEFORE_RANDOMNESS
        and statement.weight_adversary is WeightAdversary.ADAPTIVE_AFTER_RANDOMNESS
    ):
        conflicts.append(
            "weight_adversary: contract=oblivious_before_randomness, "
            "statement=adaptive_after_randomness"
        )
    if (
        contract.feasibility_requirement is FeasibilityRequirement.PATHWISE
        and statement.feasibility_requirement
        in {FeasibilityRequirement.IN_EXPECTATION, FeasibilityRequirement.HIGH_PROBABILITY}
    ):
        conflicts.append(
            "feasibility_requirement: contract=pathwise, "
            f"statement={statement.feasibility_requirement.value}"
        )
    if contract.value_guarantee is ValueGuarantee.IN_EXPECTATION and statement.value_guarantee in {
        ValueGuarantee.PATHWISE,
        ValueGuarantee.HIGH_PROBABILITY,
    }:
        conflicts.append(
            f"value_guarantee: contract=in_expectation, statement={statement.value_guarantee.value}"
        )
    if (
        contract.value_guarantee is ValueGuarantee.IN_EXPECTATION
        and statement.value_guarantee is ValueGuarantee.IN_EXPECTATION
        and contract.expectation_over
        and statement.expectation_over
    ):
        missing_sources = sorted(set(contract.expectation_over) - set(statement.expectation_over))
        if missing_sources:
            conflicts.append(
                "expectation_over: contract="
                f"{contract.expectation_over}, statement={statement.expectation_over}; "
                f"removed required source(s) {missing_sources}"
            )
    return conflicts


def _randomness_warnings(
    contract: RandomnessFacts,
    statement: RandomnessFacts,
) -> list[str]:
    warnings: list[str] = []
    scalar_fields = (
        "algorithm_randomization",
        "arrival_randomness",
        "weight_adversary",
        "feasibility_requirement",
        "value_guarantee",
    )
    for field in scalar_fields:
        contract_value = getattr(contract, field)
        statement_value = getattr(statement, field)
        if contract_value.value != "unspecified" and statement_value.value == "unspecified":
            warnings.append(
                f"{field}: contract={contract_value.value}, statement=unspecified; "
                "no explicit opposing value was found"
            )
    if contract.expectation_over and not statement.expectation_over:
        warnings.append(
            "expectation_over: contract="
            f"{contract.expectation_over}, statement=[]; no explicit removal was found"
        )
    return warnings


def _randomness_decision(
    normalized_statement: str,
    claim_contract: dict[str, str],
) -> RandomnessDecision:
    relevant_keys: list[str] = []
    contract_facts: list[RandomnessFacts] = []
    for key, raw_value in claim_contract.items():
        fragments = _clause_fragments(raw_value, clause_key=key)
        contract_facts.append(_randomness_clause_facts(key, raw_value))
        material = f"{key} {' '.join(fragments)}".casefold()
        if re.search(
            r"\b(?:random|randomized|randomised|stochastic|coin|arrival|permutation|"
            r"expectation|expected|feasib|pathwise|pointwise|realization|oblivious)\w*\b",
            material,
        ):
            relevant_keys.append(key)
    contract = _merge_randomness_facts(contract_facts)
    statement = _randomness_facts(normalized_statement)
    conflicts = _randomness_conflicts(contract, statement)
    warnings = _randomness_warnings(contract, statement) if not conflicts else []
    if conflicts:
        detail = "Material structured randomness conflict(s): " + "; ".join(conflicts)
        rule = "explicit opposing structured randomness values block research"
    elif warnings:
        detail = (
            "Structured randomness comparison is non-materially incomplete; "
            "the frozen claim contract remains canonical and research may continue."
        )
        rule = "unknown or absent structured values warn and continue"
    else:
        detail = "Structured randomness facts are aligned; pathwise feasibility is orthogonal."
        rule = "compare orthogonal random sources, guarantee mode, and feasibility mode"
    return RandomnessDecision(
        contract_clause_keys=list(dict.fromkeys(relevant_keys)),
        contract=contract,
        statement=statement,
        decision_rule=rule,
        material_contradiction=bool(conflicts),
        material_conflicts=conflicts,
        warnings=warnings,
        detail=detail,
    )


def _mode(value: str, patterns: tuple[tuple[str, str], ...]) -> str:
    plain = _plain_math(value)
    for mode, pattern in patterns:
        if re.search(pattern, plain):
            return mode
    return "unspecified"


def _signal_assertions(value: str, pattern: str) -> tuple[bool, bool]:
    """Return whether a lexical signal is affirmed or explicitly negated.

    This is deliberately local to one sentence/clause. It handles modal
    prohibitions and coordinated lists such as ``may not revoke, buffer, or
    defer`` without pretending to be a general natural-language parser.
    """

    plain = _plain_math(value)
    affirmed = False
    negated = False
    for match in re.finditer(pattern, plain):
        prefix = re.split(
            r"[.;:]|\b(?:but|however|although)\b",
            plain[: match.start()],
        )[-1][-120:]
        if re.search(r"\bnot\s+only\b[^.;]{0,96}$", prefix):
            affirmed = True
            continue
        modal_matches = list(
            re.finditer(
                r"\b(?P<negative>(?:may|must|shall|can|could|does|do|need|is|are)\s+not|"
                r"cannot|never)\b|"
                r"\b(?P<positive>may|must|shall|can|could|does|do|need|is|are)\b",
                prefix,
            )
        )
        if modal_matches:
            is_negated = modal_matches[-1].group("negative") is not None
        else:
            is_negated = bool(re.search(r"\b(?:not|no|without)(?:\s+[a-z-]+){0,3}\s*$", prefix))
        negated = negated or is_negated
        affirmed = affirmed or not is_negated
    return affirmed, negated


def _opposed_mode(
    value: str,
    *,
    first: tuple[str, str],
    second: tuple[str, str],
) -> str:
    first_affirmed, first_negated = _signal_assertions(value, first[1])
    second_affirmed, second_negated = _signal_assertions(value, second[1])
    first_supported = first_affirmed or second_negated
    second_supported = second_affirmed or first_negated
    if first_supported == second_supported:
        return "unspecified"
    return first[0] if first_supported else second[0]


def _online_decision_values(value: str) -> dict[str, str | list[str]]:
    return {
        "timing": _opposed_mode(
            value,
            first=("immediate", r"\bimmediate(?:ly)?\b"),
            second=(
                "deferred",
                r"\b(?:defer\w*|delay\w*|buffer\w*|shortlist\w*|"
                r"wait(?:ing)?\s+before\s+decid\w*)\b",
            ),
        ),
        "revision": _opposed_mode(
            value,
            first=("irrevocable", r"\birrevocable\w*\b"),
            second=(
                "revocable",
                r"\b(?:revocable|undo\w*|revoke\w*|exchange\w*|"
                r"change\w*\s+(?:(?:an?|the)\s+)?(?:prior|earlier|accepted|rejected|"
                r"decision|choice))\b",
            ),
        ),
    }


def _clause_values(
    category: TargetClauseCategory,
    value: str,
) -> dict[str, str | list[str]]:
    plain = _plain_math(value)
    if category is TargetClauseCategory.QUANTIFIERS:
        return {
            "quantifiers": [
                f"{kind} {variable or '*'}" for kind, variable in _quantifier_requirements(value)
            ]
        }
    if category is TargetClauseCategory.CONSTANTS:
        return {
            "scope": _mode(
                value,
                (
                    ("instance_dependent", r"\b(?:instance[- ]dependent|may depend on)\b"),
                    ("universal", r"\b(?:universal|independent of (?:the )?instance)\b"),
                ),
            ),
            "numeric_values": re.findall(r"(?<![a-z_])\d+(?:\.\d+)?(?![a-z0-9_])", plain),
        }
    if category is TargetClauseCategory.DOMAIN:
        return {
            "finiteness": _mode(
                value,
                (("infinite", r"\binfinite\b"), ("finite", r"\bfinite\b")),
            )
        }
    if category is TargetClauseCategory.INFORMATION_MODEL:
        return {
            "unseen_information": _mode(
                value,
                (
                    (
                        "allowed",
                        r"\b(?:may|can)\b[^.;]{0,32}\b(?:inspect|use|access)\b[^.;]{0,32}"
                        r"\b(?:unseen|unrevealed|future)\b",
                    ),
                    (
                        "forbidden",
                        r"\b(?:only revealed|no access to (?:unseen|unrevealed|future)|"
                        r"not known until arrival|without (?:unseen|unrevealed|future))\b",
                    ),
                ),
            )
        }
    if category is TargetClauseCategory.ONLINE_DECISIONS:
        return _online_decision_values(value)
    if category is TargetClauseCategory.FEASIBILITY:
        return {"feasibility_requirement": _feasibility_requirement(value).value}
    if category is TargetClauseCategory.RANDOMNESS:
        facts = _randomness_facts(value)
        return {
            key: item if isinstance(item, list) else str(item)
            for key, item in facts.model_dump(mode="json").items()
        }
    if category is TargetClauseCategory.POLARITY:
        return {"requested_outcome": classify_requested_polarity(value).value}
    if category in {TargetClauseCategory.ADDITIVE_TERMS, TargetClauseCategory.CONCLUSION}:
        comparisons = []
        for match in re.finditer(r"(.+?)\s*(<=|>=|=|<|>)\s*(.+)", plain):
            comparisons.append(
                " ".join(
                    [
                        *_comparison_lexemes(match.group(1)),
                        match.group(2),
                        *_comparison_lexemes(match.group(3)),
                    ]
                )
            )
        return {"comparisons": comparisons}
    return {}


def _opposing_field_conflicts(
    category: TargetClauseCategory,
    contract_values: dict[str, str | list[str]],
    statement_values: dict[str, str | list[str]],
) -> list[str]:
    conflicts: list[str] = []
    opposing_fields: dict[TargetClauseCategory, tuple[str, ...]] = {
        TargetClauseCategory.CONSTANTS: ("scope",),
        TargetClauseCategory.DOMAIN: ("finiteness",),
        TargetClauseCategory.INFORMATION_MODEL: ("unseen_information",),
        TargetClauseCategory.ONLINE_DECISIONS: ("timing", "revision"),
        TargetClauseCategory.FEASIBILITY: ("feasibility_requirement",),
    }
    for field in opposing_fields.get(category, ()):
        expected = contract_values.get(field, "unspecified")
        observed = statement_values.get(field, "unspecified")
        if expected != "unspecified" and observed != "unspecified" and expected != observed:
            conflicts.append(f"{field}: contract={expected}, statement={observed}")
    return conflicts


def _is_compact_formal_clause(fragment: str) -> bool:
    plain = _plain_math(fragment)
    return (
        bool(re.search(r"<=|>=|(?<![<>])=(?!=)|<|>", plain))
        and len(_comparison_lexemes(plain)) <= 12
    )


# Ordered structured requested-outcome signals. Detection inspects only the leading
# requested-outcome sentence of a compact ``polarity`` clause or normalized statement, never
# broad framework prose, literature summaries, excluded-outcome enumerations, or audit
# vocabulary. Overloaded words such as ``counterexample``, ``refuted``, and ``barrier`` are
# intentionally absent: they routinely and legitimately describe intermediate lemmas and
# excluded outcomes inside an affirmative proof request and must never, on their own, imply a
# disproof polarity.
_POLARITY_SIGNALS: tuple[tuple[TargetPolarity, tuple[str, ...]], ...] = (
    (TargetPolarity.DISPROOF, ("disprove", "refute", "disproof")),
    (
        TargetPolarity.AFFIRMATIVE_PROOF,
        (
            "affirmatively prove",
            "affirmative proof",
            "prove",
            "proof of",
            "establish",
            "demonstrate",
            "show that",
            "verify that",
            "confirm that",
        ),
    ),
    (
        TargetPolarity.CLASSIFICATION,
        ("classify", "characterize", "determine whether", "decide whether"),
    ),
    (TargetPolarity.CONSTRUCTION, ("construct", "exhibit", "design")),
    (TargetPolarity.INVESTIGATION, ("investigate", "study", "explore")),
)
_POLARITY_EXCLUSION_MARKERS = (
    "not ",
    "non ",
    "rather than",
    "instead of",
    "without",
    "excluding",
    "does not",
    "do not",
    "cannot",
    "can not",
    "neither",
    "nor ",
    "fails to",
)
_OPEN_QUESTION_PATTERNS = (
    re.compile(r"\b(?:prove|establish)\b[^.]{0,24}\bor\b[^.]{0,24}\b(?:disprove|refute)\b"),
    re.compile(r"\b(?:disprove|refute)\b[^.]{0,24}\bor\b[^.]{0,24}\b(?:prove|establish)\b"),
)


def classify_requested_polarity(text: str) -> TargetPolarity:
    """Map an explicit requested outcome to a compact structured polarity value.

    Only the explicit requested-outcome sentence is examined, and only the first
    non-excluded directive within it decides the polarity. This deliberately ignores
    excluded/insufficient outcomes, negated clauses, and overloaded audit vocabulary so that
    stochastic framework prose cannot flip an affirmative request into a disproof request.
    Ambiguous prose returns :attr:`TargetPolarity.AMBIGUOUS` rather than guessing.
    """

    plain = _plain_math(text)
    if not plain:
        return TargetPolarity.AMBIGUOUS
    token = plain.replace(" ", "_")
    for polarity in TargetPolarity:
        if polarity is not TargetPolarity.AMBIGUOUS and token == polarity.value:
            return polarity
    first_sentence = re.split(r"(?<=[.!?])\s", plain)[0]
    if any(pattern.search(first_sentence) for pattern in _OPEN_QUESTION_PATTERNS):
        return TargetPolarity.INVESTIGATION
    best_position: int | None = None
    best_polarity = TargetPolarity.AMBIGUOUS
    for polarity, signals in _POLARITY_SIGNALS:
        for signal in signals:
            for match in re.finditer(rf"\b{re.escape(signal)}\b", first_sentence):
                window = first_sentence[max(0, match.start() - 40) : match.start()]
                if any(marker in window for marker in _POLARITY_EXCLUSION_MARKERS):
                    continue
                if best_position is None or match.start() < best_position:
                    best_position = match.start()
                    best_polarity = polarity
    return best_polarity


def _evaluate_polarity(
    normalized_statement: str,
    claim_contract: dict[str, str],
    polarity_clause_key: str,
) -> PolarityDecision:
    """Compare structured contract and statement polarity for one polarity clause."""

    contract_polarity = classify_requested_polarity(claim_contract[polarity_clause_key])
    statement_polarity = classify_requested_polarity(normalized_statement)
    material = {contract_polarity, statement_polarity} == {
        TargetPolarity.AFFIRMATIVE_PROOF,
        TargetPolarity.DISPROOF,
    }
    if material:
        detail = (
            f"Structured polarity mismatch: contract requests {contract_polarity.value}, "
            f"statement requests {statement_polarity.value} (refute/disprove polarity)."
        )
        rule = "material contradiction: affirmative_proof versus disproof blocks research"
    elif (
        contract_polarity is TargetPolarity.AMBIGUOUS
        or statement_polarity is TargetPolarity.AMBIGUOUS
    ):
        detail = (
            "Requested-outcome polarity is ambiguous from prose "
            f"(contract={contract_polarity.value}, statement={statement_polarity.value}); "
            "reusing the canonical target and continuing."
        )
        rule = "ambiguous prose polarity: warn and reuse canonical target"
    elif contract_polarity != statement_polarity:
        detail = (
            "Non-material polarity wording difference "
            f"(contract={contract_polarity.value}, statement={statement_polarity.value}); "
            "reusing the canonical target and continuing."
        )
        rule = "non-material polarity difference: warn and continue"
    else:
        detail = f"Structured polarity aligned: {contract_polarity.value}."
        rule = "structured polarity values match"
    return PolarityDecision(
        contract_clause_key=polarity_clause_key,
        contract_polarity=contract_polarity,
        statement_polarity=statement_polarity,
        decision_rule=rule,
        material_contradiction=material,
        detail=detail,
    )


def validate_target_contract(
    normalized_statement: str,
    claim_contract: dict[str, str],
) -> TargetContractAlignment:
    """Reject explicit contradictions between a compiled statement and its contract.

    The prompt compiler authors both fields, so lexical absence is not evidence of drift:
    equivalent mathematical prose routinely uses different words and abbreviations. This
    deterministic guard therefore blocks only high-confidence conflicts and records every
    clause for diagnosis. It does not attempt to prove semantic equivalence.
    """

    statement = _plain_math(normalized_statement)
    canonical_contract = "\n".join(
        f"{key}\0{value}" for key, value in sorted(claim_contract.items())
    )
    checks: list[TargetClauseCheck] = []
    issues: list[str] = []
    warnings: list[str] = []

    randomness_decision = _randomness_decision(normalized_statement, claim_contract)
    local_randomness = {
        key: _randomness_clause_facts(key, raw_value) for key, raw_value in claim_contract.items()
    }
    randomness_conflicts_by_key: dict[str, list[str]] = {}
    for conflict in randomness_decision.material_conflicts:
        field = conflict.partition(":")[0]
        specified_keys: list[str] = []
        for candidate_key, candidate in local_randomness.items():
            candidate_value = getattr(candidate, field)
            if isinstance(candidate_value, list):
                if candidate_value:
                    specified_keys.append(candidate_key)
            elif candidate_value.value != "unspecified":
                specified_keys.append(candidate_key)

        owner = next(
            iter(specified_keys),
            randomness_decision.contract_clause_keys[0]
            if randomness_decision.contract_clause_keys
            else next(iter(claim_contract)),
        )
        randomness_conflicts_by_key.setdefault(owner, []).append(conflict)

    if randomness_decision.warnings and randomness_decision.contract_clause_keys:
        warnings.extend(
            f"Structured randomness uncertainty: {warning}"
            for warning in randomness_decision.warnings
        )

    polarity_clause_key: str | None = None
    if "polarity" in claim_contract:
        polarity_clause_key = "polarity"
    else:
        for key, raw_value in claim_contract.items():
            if _clause_category(key, raw_value) is TargetClauseCategory.POLARITY:
                polarity_clause_key = key
                break
    polarity_decision = (
        _evaluate_polarity(normalized_statement, claim_contract, polarity_clause_key)
        if polarity_clause_key is not None
        else None
    )

    for key, raw_value in claim_contract.items():
        fragments = _clause_fragments(raw_value, clause_key=key)
        category = _clause_category(key, raw_value)
        clause_text = "\n".join(fragments)
        contract_values = _clause_values(category, clause_text)
        statement_values = _clause_values(category, normalized_statement)
        conflicts: list[str] = []
        clause_warnings: list[str] = []

        for fragment in fragments:
            conflicts.extend(_quantifier_conflicts(normalized_statement, fragment))
        conflicts.extend(_opposing_field_conflicts(category, contract_values, statement_values))
        randomness_conflicts = randomness_conflicts_by_key.get(key, [])
        conflicts.extend(randomness_conflicts)
        if randomness_conflicts:
            for conflict in randomness_conflicts:
                field = conflict.partition(":")[0]
                contract_value = getattr(randomness_decision.contract, field)
                statement_value = getattr(randomness_decision.statement, field)
                contract_values[field] = (
                    list(contract_value)
                    if isinstance(contract_value, list)
                    else contract_value.value
                )
                statement_values[field] = (
                    list(statement_value)
                    if isinstance(statement_value, list)
                    else statement_value.value
                )

        is_polarity_clause = polarity_decision is not None and key == polarity_clause_key
        if is_polarity_clause:
            assert polarity_decision is not None
            if polarity_decision.material_contradiction:
                conflicts.append("refute/disprove polarity")
            elif (
                polarity_decision.contract_polarity != polarity_decision.statement_polarity
                or polarity_decision.contract_polarity is TargetPolarity.AMBIGUOUS
            ):
                clause_warnings.append(polarity_decision.detail)

        for fragment in fragments:
            fragment_value = _plain_math(fragment)
            if _is_compact_formal_clause(fragment) and not _comparison_aligned(
                statement, fragment_value
            ):
                conflicts.append(
                    "compact formal comparison: "
                    f"contract={_comparison_lexemes(fragment_value)}, "
                    f"statement={statement_values.get('comparisons', [])}"
                )
            elif category is TargetClauseCategory.CONCLUSION and _comparison_direction_conflict(
                statement, fragment_value
            ):
                conflicts.append(
                    "reversed comparison direction: "
                    f"contract={contract_values.get('comparisons', [])}, "
                    f"statement={statement_values.get('comparisons', [])}"
                )
            if re.fullmatch(r"\d+(?:\.\d+)?", fragment_value) and not re.search(
                rf"(?<![a-z_]){re.escape(fragment_value)}(?![a-z0-9_])", statement
            ):
                statement_numeric_values = re.findall(
                    r"(?<![a-z_])\d+(?:\.\d+)?(?![a-z0-9_])", statement
                )
                conflicts.append(
                    f"numeric value {fragment_value}: contract="
                    f"{contract_values.get('numeric_values', [fragment_value])}, "
                    f"statement={statement_numeric_values}"
                )

        conflicts = list(dict.fromkeys(conflicts))
        passed = not conflicts
        if is_polarity_clause and passed:
            detail = polarity_decision.detail  # type: ignore[union-attr]
        elif key in randomness_decision.contract_clause_keys and passed:
            detail = randomness_decision.detail
        elif passed:
            detail = "No high-confidence contradiction detected."
        else:
            detail = (
                "Explicit structured contradiction(s): "
                + "; ".join(conflicts)
                + ". Compared contract values="
                + json.dumps(contract_values, sort_keys=True, ensure_ascii=False)
                + "; statement values="
                + json.dumps(statement_values, sort_keys=True, ensure_ascii=False)
                + "."
            )
        checks.append(
            TargetClauseCheck(
                key=key,
                category=category,
                passed=passed,
                detail=detail,
                contract_values=contract_values,
                statement_values=statement_values,
                material_conflicts=conflicts,
            )
        )
        if not passed:
            issues.append(f"Claim-contract clause {key!r} is not aligned: {detail}")
        for warning in clause_warnings:
            warnings.append(f"Claim-contract clause {key!r} polarity: {warning}")

    statement_sha256 = sha256(normalized_statement.encode("utf-8")).hexdigest()
    contract_sha256 = sha256(canonical_contract.encode("utf-8")).hexdigest()
    observations = [
        AlignmentWarning(
            warning_id=f"alignment-{index:04d}",
            origin=AlignmentWarningOrigin.HEURISTIC_EXTRACTOR,
            clause_keys=[check.key],
            observation=f"Claim-contract clause {check.key!r}: {check.detail}",
            statement_sha256=statement_sha256,
            contract_sha256=contract_sha256,
        )
        for index, check in enumerate(
            (check for check in checks if check.material_conflicts),
            start=1,
        )
    ]
    diagnostic_warnings = [
        *warnings,
        *(
            [
                "Heuristic target-alignment observations require semantic review and do not "
                "block research on their own: " + " ".join(issues)
            ]
            if issues
            else []
        ),
    ]
    return TargetContractAlignment(
        statement_sha256=statement_sha256,
        contract_sha256=contract_sha256,
        # Generated-prose extraction is advisory. The prompt stage may change this only
        # after an independent materiality reviewer confirms a genuine theorem change.
        passed=True,
        checks=checks,
        blocking_issues=[],
        warnings=diagnostic_warnings,
        alignment_warnings=observations,
        polarity=polarity_decision,
        randomness=randomness_decision,
    )


def bind_target_contract_identity(
    normalized_statement: str,
    claim_contract: dict[str, str],
) -> TargetContractAlignment:
    """Hash-bind a canonical target without repeating semantic prose extraction."""

    canonical_contract = "\n".join(
        f"{key}\0{value}" for key, value in sorted(claim_contract.items())
    )
    return TargetContractAlignment(
        statement_sha256=sha256(normalized_statement.encode("utf-8")).hexdigest(),
        contract_sha256=sha256(canonical_contract.encode("utf-8")).hexdigest(),
        passed=True,
        checks=[],
    )


__all__ = [
    "AlgorithmRandomization",
    "AlignmentWarning",
    "AlignmentWarningOrigin",
    "ArrivalRandomness",
    "BranchOutcome",
    "FeasibilityRequirement",
    "MaterialityReviewRecord",
    "MaterialityVerdict",
    "PolarityDecision",
    "RandomnessDecision",
    "RandomnessFacts",
    "ScientificArtifactDeclaration",
    "ScientificObligationDeclaration",
    "ScientificResult",
    "ScientificResultDisposition",
    "ScientificResultKind",
    "ScientificScope",
    "TargetClauseCategory",
    "TargetClauseCheck",
    "TargetContractAlignment",
    "TargetMaterialityAssessment",
    "TargetPolarity",
    "ValueGuarantee",
    "WeightAdversary",
    "bind_target_contract_identity",
    "classify_requested_polarity",
    "exact_statement_fingerprint",
    "is_explicit_definition_declaration",
    "normalize_exact_statement",
    "transitive_result_dependency_keys",
    "validate_result_dependency_dag",
    "validate_target_contract",
]
