"""Registry for checked-in schemas that mirror authoritative Pydantic types."""

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel

from .knowledge_graph.semantic import SemanticCoordinatorDecision, SemanticWorkerReport
from .reporting import FinalReport
from .stages.compile_prompt import CompiledProblem
from .stages.counterexample_audit import CounterexampleAuditResponse
from .stages.lean import ClaimAlignment
from .stages.manuscript import BibliographyAudit
from .stages.research import (
    AuditVerdict,
    ResearchRoundPlan,
)
from .structured_schema import strict_json_schema

MODEL_SCHEMA_ARTIFACTS: dict[str, type[BaseModel]] = {
    "audit_verdict.schema.json": AuditVerdict,
    "bibliography_audit.schema.json": BibliographyAudit,
    "claim_alignment.schema.json": ClaimAlignment,
    "compiled_problem.schema.json": CompiledProblem,
    "counterexample_audit_response.schema.json": CounterexampleAuditResponse,
    "research_coordinator_decision.schema.json": SemanticCoordinatorDecision,
    "research_round_plan.schema.json": ResearchRoundPlan,
    "research_worker_report.schema.json": SemanticWorkerReport,
}


def generated_model_schemas() -> dict[str, dict[str, object]]:
    """Return strict schemas keyed by their packaged artifact filename."""

    return {
        filename: strict_json_schema(output_type)
        for filename, output_type in MODEL_SCHEMA_ARTIFACTS.items()
    }


def generated_resource_schemas() -> dict[str, dict[str, object]]:
    """Return every schema packaged as a public resource.

    Model-output schemas use the provider-compatible closed representation. The final
    report is a deterministic artifact rather than model output, so its schema preserves
    the report model's intentionally extensible metadata mappings.
    """

    schemas = generated_model_schemas()
    report_schema: dict[str, Any] = FinalReport.model_json_schema(mode="serialization")
    report_schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schemas["final_report.schema.json"] = cast(dict[str, object], report_schema)
    return schemas
