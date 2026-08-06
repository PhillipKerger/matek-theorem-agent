"""Stable failure taxonomy shared by workflow and research orchestration."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

from .budget import BudgetExceeded
from .models import FailureCategory
from .state import ArtifactIntegrityError, StateCorruptionError

_INTEGRITY_MARKERS = (
    "artifact hash",
    "artifact changed",
    "artifact integrity",
    "integrity check failed",
    "checkpoint is missing",
    "checkpoint is invalid",
    "incompatible or corrupt",
    "event stream is not",
    "state and immutable event cursor disagree",
    "event artifact is missing or changed",
    "checkpoint belongs to a different",
    "canonical scheduler",
    "escapes its stage",
    "escapes its run",
    "path traversal",
    "must not be a symlink",
    "unsafe path",
    "unauthorized file",
    "unauthorized write",
    "verification certificate inventory mismatch",
)


class FailureExplanation(BaseModel):
    """Brief user-facing explanation produced after an operational failure."""

    model_config = ConfigDict(extra="forbid")

    explanation: str
    suggested_resolution: str

    @field_validator("explanation", "suggested_resolution")
    @classmethod
    def text_is_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("failure explanation text must not be blank")
        return normalized


def classify_failure(exc: BaseException) -> FailureCategory:
    """Classify without turning ordinary provider/schema failures into integrity stops."""

    if isinstance(exc, (StateCorruptionError, ArtifactIntegrityError)):
        return FailureCategory.INTEGRITY
    name = type(exc).__name__.casefold()
    message = str(exc).casefold()
    if any(
        marker in name
        for marker in (
            "unauthorizedfilechange",
            "pathconfinement",
            "corruption",
            "integrity",
            "security",
            "journalerror",
        )
    ):
        return FailureCategory.INTEGRITY
    if any(marker in message for marker in _INTEGRITY_MARKERS):
        return FailureCategory.INTEGRITY
    if isinstance(exc, BudgetExceeded) or any(
        marker in name for marker in ("budget", "allowance", "ratelimit", "ratelimited")
    ):
        return FailureCategory.RESOURCE
    if any(marker in name for marker in ("source", "citation", "bibliograph")):
        return FailureCategory.EVIDENCE
    if any(marker in name for marker in ("gate", "scientific")):
        return FailureCategory.SCIENTIFIC
    return FailureCategory.EXECUTION


def recovery_obligations(exc: BaseException, category: FailureCategory) -> list[str]:
    """Return deterministic, user-visible obligations for a recoverable failure."""

    remedy = getattr(exc, "remedy", None)
    obligations = [str(remedy).strip()] if isinstance(remedy, str) and remedy.strip() else []
    if category is FailureCategory.RESOURCE:
        obligations.append(
            "Increase the applicable limit or resume when provider capacity returns."
        )
    elif category is FailureCategory.EVIDENCE:
        obligations.append("Acquire and independently verify the missing source evidence.")
    elif category is FailureCategory.EXECUTION:
        obligations.append("Retry the saved request or reassign the task from its durable event.")
    return list(dict.fromkeys(obligations))


__all__ = ["FailureExplanation", "classify_failure", "recovery_obligations"]
