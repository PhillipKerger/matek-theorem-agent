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
_NODE_ID = re.compile(r"\A[A-Z]{3}-[A-Z0-9]{8,64}\Z")
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
    normalized = [item.strip().upper() for item in values]
    if any(not _NODE_ID.fullmatch(item) for item in normalized):
        raise ValueError("scientific dependencies and targets must be stable node IDs")
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
    EDGE_CASES = "edge_cases"
    POLARITY = "polarity"
    CONCLUSION = "conclusion"
    OTHER = "other"


class TargetClauseCheck(_ScientificModel):
    key: str
    category: TargetClauseCategory
    passed: bool
    detail: str


class TargetContractAlignment(_ScientificModel):
    statement_sha256: str
    contract_sha256: str
    passed: bool
    checks: list[TargetClauseCheck]
    blocking_issues: list[str] = Field(default_factory=list)


_QUANTIFIER_KEYS = ("quantifier", "uniformity")
_CONSTANT_KEYS = ("constant", "parameter", "coefficient")
_ADDITIVE_KEYS = ("additive", "offset", "slack")
_DOMAIN_KEYS = ("domain", "space", "instance", "object", "metric", "finite", "arbitrary")
_EDGE_KEYS = ("edge", "boundary", "exception", "degenerate", "zero", "empty")
_POLARITY_KEYS = ("polarity", "posture", "prove", "refute", "disprove")
_CONCLUSION_KEYS = ("conclusion", "target", "guarantee", "bound", "inequality")
_MATERIAL_QUALIFIERS = (
    "randomized",
    "deterministic",
    "finite",
    "infinite",
    "arbitrary",
    "uniform",
    "nonuniform",
    "constructive",
    "existential",
    "metric",
    "probability",
)
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
_MATERIAL_STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "be",
    "by",
    "case",
    "cases",
    "clause",
    "clauses",
    "consist",
    "consists",
    "domain",
    "for",
    "from",
    "in",
    "include",
    "included",
    "includes",
    "including",
    "is",
    "of",
    "on",
    "or",
    "over",
    "such",
    "that",
    "the",
    "to",
    "under",
    "with",
}
_JSON_LITERAL_TOKENS = {"false", "null", "true"}


def _plain_math(value: str) -> str:
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


def _canonical_material_token(token: str) -> str:
    irregular = {
        "axes": "axis",
        "indices": "index",
        "matrices": "matrix",
        "vertices": "vertex",
    }
    if token in irregular:
        return irregular[token]
    if token in {"all", "any", "each", "every"}:
        return "forall"
    if len(token) > 4 and token.endswith("ies"):
        return f"{token[:-3]}y"
    if len(token) > 4 and token.endswith(("ches", "shes", "sses", "xes", "zes")):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith(("is", "ss", "us")):
        return token[:-1]
    return token


def _material_tokens(values: list[str], *, discard_scaffolding: bool) -> list[str]:
    tokens: list[str] = []
    for value in values:
        plain = _plain_math(value).replace("_", " ")
        for token in re.findall(r"[a-z\u03b1-\u03c9][a-z0-9_]*|\d+(?:\.\d+)?", plain):
            if token in _JSON_LITERAL_TOKENS:
                continue
            if discard_scaffolding and token in _MATERIAL_STOPWORDS:
                continue
            tokens.append(_canonical_material_token(token))
    return list(dict.fromkeys(tokens))


def _clause_category(key: str, value: str) -> TargetClauseCategory:
    material = f"{key} {value}".casefold()
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
    if any(marker in material for marker in _EDGE_KEYS):
        return TargetClauseCategory.EDGE_CASES
    if any(marker in material for marker in _DOMAIN_KEYS):
        return TargetClauseCategory.DOMAIN
    if any(marker in material for marker in _CONSTANT_KEYS):
        return TargetClauseCategory.CONSTANTS
    if any(marker in material for marker in _CONCLUSION_KEYS):
        return TargetClauseCategory.CONCLUSION
    return TargetClauseCategory.OTHER


def _quantifier_requirements(value: str) -> list[tuple[str, str | None]]:
    plain = _plain_math(value)
    requirements: list[tuple[str, str | None]] = []
    pattern = re.compile(
        r"\b(forall|for all|for every|each|exists|there exists)\b"
        r"(?:\s+(?:an?\s+)?([a-z][a-z0-9_]*))?"
    )
    for kind, variable in pattern.findall(plain):
        normalized_kind = "exists" if "exist" in kind else "forall"
        requirements.append((normalized_kind, variable or None))
    return list(dict.fromkeys(requirements))


def _has_quantifier(statement: str, kind: str, variable: str | None) -> bool:
    plain = _plain_math(statement)
    marker = (
        r"(?:exists|there exists)" if kind == "exists" else r"(?:forall|for all|for every|each|any)"
    )
    if variable:
        return bool(re.search(rf"\b{marker}\s+(?:an?\s+)?{re.escape(variable)}\b", plain))
    return bool(re.search(rf"\b{marker}\b", plain))


def _additive_requirements(value: str) -> list[str]:
    plain = _plain_math(value)
    return list(dict.fromkeys(re.findall(r"\+\s*([a-z\u03b1-\u03c9][a-z0-9_]*)", plain)))


def _symbol_requirements(value: str) -> list[str]:
    plain = _plain_math(value)
    ignored = {
        "a",
        "an",
        "and",
        "all",
        "any",
        "as",
        "at",
        "be",
        "by",
        "constant",
        "exists",
        "for",
        "from",
        "in",
        "is",
        "of",
        "on",
        "or",
        "the",
        "there",
        "to",
        "with",
    }
    symbols = re.findall(r"\b[a-z\u03b1-\u03c9][a-z0-9_]*\b", plain)
    return [item for item in dict.fromkeys(symbols) if item not in ignored]


def _number_requirements(value: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"(?<![a-z_])\d+(?:\.\d+)?", _plain_math(value))))


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
    expected_left = _comparison_lexemes(expected.group(1))
    expected_right = _comparison_lexemes(expected.group(3))
    operator = expected.group(2)
    for candidate in re.finditer(r"(.+?)\s*(<=|>=|=|<|>)\s*(.+)", statement):
        if candidate.group(2) != operator:
            continue
        candidate_left = _comparison_lexemes(candidate.group(1))
        candidate_right = _comparison_lexemes(candidate.group(3))
        # Introductory theorem prose and quantifiers may precede the left-hand expression, so
        # align it at the end of the candidate side.  The right-hand side runs to the end of the
        # asserted comparison and must match exactly; accepting a mere subsequence here would
        # silently admit material drift such as ``+ gamma + beta`` for a requested ``+ beta``.
        if (
            len(candidate_left) >= len(expected_left)
            and candidate_left[-len(expected_left) :] == expected_left
            and candidate_right == expected_right
        ):
            return True
    return False


def _qualifier_postures(fragments: list[str], qualifier: str) -> set[bool]:
    """Return positive/negative requirements for a material structured qualifier."""

    postures: set[bool] = set()
    negative = re.compile(rf"\b(?:not|non)\s*-?\s*{re.escape(qualifier)}\b")
    positive = re.compile(rf"\b{re.escape(qualifier)}\b")
    for fragment in fragments:
        plain = _plain_math(fragment)
        without_negative = negative.sub(" ", plain)
        if negative.search(plain):
            postures.add(False)
        if positive.search(without_negative):
            postures.add(True)
    return postures


def validate_target_contract(
    normalized_statement: str,
    claim_contract: dict[str, str],
) -> TargetContractAlignment:
    """Check material contract clauses against the exact compiled statement.

    The checker is intentionally conservative and transparent.  It catches logical-strength
    regressions such as a lost ``+ beta`` term or a deterministic/randomized swap; it does
    not claim that string matching proves semantic equivalence.
    """

    statement = _plain_math(normalized_statement)
    statement_material_tokens = set(
        _material_tokens([normalized_statement], discard_scaffolding=False)
    )
    canonical_contract = "\n".join(
        f"{key}\0{value}" for key, value in sorted(claim_contract.items())
    )
    checks: list[TargetClauseCheck] = []
    issues: list[str] = []

    for key, raw_value in claim_contract.items():
        value = _plain_math(raw_value)
        fragments = _clause_fragments(raw_value, clause_key=key)
        category = _clause_category(key, raw_value)
        missing: list[str] = []

        # Material features are checked orthogonally. A clause such as a conclusion
        # containing ``+ beta`` must not be reduced to an additive-term-only check.
        for fragment in fragments:
            for kind, variable in _quantifier_requirements(fragment):
                if not _has_quantifier(normalized_statement, kind, variable):
                    missing.append(f"{kind} {variable or ''}".strip())

            for term in _additive_requirements(fragment):
                if not re.search(rf"\+\s*{re.escape(term)}\b", statement):
                    missing.append(f"+ {term}")

        for qualifier in _MATERIAL_QUALIFIERS:
            postures = _qualifier_postures(fragments, qualifier)
            statement_has_qualifier = bool(re.search(rf"\b{qualifier}\b", statement))
            if postures == {True} and not statement_has_qualifier:
                missing.append(qualifier)
            elif postures == {False} and statement_has_qualifier:
                missing.append(f"not {qualifier}")
            elif postures == {False, True}:
                missing.append(f"consistent {qualifier} qualifier")

        if category is TargetClauseCategory.POLARITY:
            requested_refutation = bool(re.search(r"\b(?:refute|disprove|counterexample)\b", value))
            requested_proof = bool(re.search(r"\b(?:prove|establish|show)\b", value))
            statement_refutation = bool(
                re.search(r"\b(?:refute|disprove|counterexample)\b", statement)
            )
            if requested_refutation and not statement_refutation:
                missing.append("refute/disprove polarity")
            if requested_proof and statement_refutation:
                missing.append("prove polarity")

        key_text = key.casefold()
        domain_like = category is TargetClauseCategory.DOMAIN or any(
            marker in key_text for marker in _DOMAIN_KEYS
        )
        edge_like = category is TargetClauseCategory.EDGE_CASES or any(
            marker in key_text for marker in _EDGE_KEYS
        )
        constant_like = category is TargetClauseCategory.CONSTANTS or any(
            marker in key_text for marker in _CONSTANT_KEYS
        )
        if domain_like or edge_like or constant_like:
            material_requirements = _material_tokens(fragments, discard_scaffolding=True)
            if not material_requirements:
                missing.append("nonempty material clause")
            if domain_like or edge_like:
                for requirement in material_requirements:
                    if requirement not in statement_material_tokens:
                        missing.append(requirement)

        conclusion_like = any(marker in key.casefold() for marker in _CONCLUSION_KEYS) or bool(
            re.search(r"<=|>=|(?<![<>])=(?!=)|<|>", value)
        )
        # Short formal conclusion/constant clauses should visibly survive even when
        # another feature (for example an additive term) determined the display category.
        if conclusion_like or constant_like:
            for fragment in fragments:
                symbols = _symbol_requirements(fragment)
                if len(symbols) <= 12:
                    for symbol in symbols:
                        canonical_symbol = _canonical_material_token(symbol)
                        if not re.search(rf"\b{re.escape(symbol)}\b", statement) and (
                            canonical_symbol not in statement_material_tokens
                        ):
                            missing.append(symbol)
                for number in _number_requirements(fragment):
                    if not re.search(rf"(?<![a-z_]){re.escape(number)}(?![a-z0-9_])", statement):
                        missing.append(number)
                fragment_value = _plain_math(fragment)
                if not _comparison_aligned(statement, fragment_value):
                    missing.append("ordered comparison sides")

        missing = list(dict.fromkeys(missing))
        passed = not missing
        detail = (
            "Material clause markers are present in the exact statement."
            if passed
            else "Missing or incompatible material marker(s): " + ", ".join(missing)
        )
        checks.append(TargetClauseCheck(key=key, category=category, passed=passed, detail=detail))
        if not passed:
            issues.append(f"Claim-contract clause {key!r} is not aligned: {detail}")

    return TargetContractAlignment(
        statement_sha256=sha256(normalized_statement.encode("utf-8")).hexdigest(),
        contract_sha256=sha256(canonical_contract.encode("utf-8")).hexdigest(),
        passed=not issues,
        checks=checks,
        blocking_issues=issues,
    )


__all__ = [
    "BranchOutcome",
    "ScientificArtifactDeclaration",
    "ScientificObligationDeclaration",
    "ScientificResult",
    "ScientificResultDisposition",
    "ScientificResultKind",
    "ScientificScope",
    "TargetClauseCategory",
    "TargetClauseCheck",
    "TargetContractAlignment",
    "exact_statement_fingerprint",
    "is_explicit_definition_declaration",
    "normalize_exact_statement",
    "transitive_result_dependency_keys",
    "validate_result_dependency_dag",
    "validate_target_contract",
]
