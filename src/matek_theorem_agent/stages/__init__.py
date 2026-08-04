"""Dependency-injected workflow stage services and their gate result models."""

from .compile_prompt import (
    LiteratureStatus,
    PromptCompilationResult,
    PromptCompilationStatus,
    compile_prompt,
)
from .counterexample_audit import CounterexampleAuditGate, run_counterexample_audit
from .lean import LeanPipelineResult, run_lean_pipeline
from .manuscript import (
    ManuscriptResult,
    generate_manuscript,
    resume_manuscript_bibliography,
)
from .research import ResearchResult, run_adaptive_research

__all__ = [
    "CounterexampleAuditGate",
    "LeanPipelineResult",
    "LiteratureStatus",
    "ManuscriptResult",
    "PromptCompilationResult",
    "PromptCompilationStatus",
    "ResearchResult",
    "compile_prompt",
    "generate_manuscript",
    "resume_manuscript_bibliography",
    "run_adaptive_research",
    "run_counterexample_audit",
    "run_lean_pipeline",
]
