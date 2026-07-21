"""Registry for checked-in schemas that mirror model-backed output types."""

from __future__ import annotations

from pydantic import BaseModel

from .stages.compile_prompt import CompiledProblem
from .stages.lean import ClaimAlignment
from .stages.manuscript import BibliographyAudit
from .stages.research import (
    AuditVerdict,
    ResearchCoordinatorDecision,
    ResearchRoundPlan,
    ResearchWorkerReport,
)
from .structured_schema import strict_json_schema

MODEL_SCHEMA_ARTIFACTS: dict[str, type[BaseModel]] = {
    "audit_verdict.schema.json": AuditVerdict,
    "bibliography_audit.schema.json": BibliographyAudit,
    "claim_alignment.schema.json": ClaimAlignment,
    "compiled_problem.schema.json": CompiledProblem,
    "research_coordinator_decision.schema.json": ResearchCoordinatorDecision,
    "research_round_plan.schema.json": ResearchRoundPlan,
    "research_worker_report.schema.json": ResearchWorkerReport,
}


def generated_model_schemas() -> dict[str, dict[str, object]]:
    """Return strict schemas keyed by their packaged artifact filename."""

    return {
        filename: strict_json_schema(output_type)
        for filename, output_type in MODEL_SCHEMA_ARTIFACTS.items()
    }
