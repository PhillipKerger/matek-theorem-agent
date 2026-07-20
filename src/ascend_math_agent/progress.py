"""Sparse user-facing workflow progress events."""

from __future__ import annotations

from collections.abc import Callable
from enum import IntEnum


class Ascension(IntEnum):
    """Stable high-level workflow milestones shown by the CLI."""

    FETCH_PROBLEM = 0
    FORMULATE_PROMPT = 1
    PLAN_RESEARCH = 2
    RUN_RESEARCH = 3
    AUDIT_RESEARCH = 4
    WRITE_MANUSCRIPT = 5
    FORMALIZE_LEAN = 6
    PREPARE_REPORT = 7


ProgressReporter = Callable[[Ascension, str], None]


def no_progress(ascension: Ascension, message: str) -> None:
    """Default reporter for library callers that do not request terminal output."""

    del ascension, message
