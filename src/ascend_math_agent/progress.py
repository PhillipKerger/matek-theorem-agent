"""Sparse user-facing workflow progress events."""

from __future__ import annotations

from collections.abc import Callable
from enum import IntEnum


class Ascension(IntEnum):
    """Stable high-level workflow milestones shown by the CLI."""

    FETCH_PROBLEM = 0
    FORMULATE_PROMPT = 1
    START_RESEARCH_COORDINATOR = 2
    MANAGE_RESEARCH_POOL = 3
    AUDIT_RESEARCH = 4
    WRITE_MANUSCRIPT = 5
    FORMALIZE_LEAN = 6
    PREPARE_REPORT = 7

    # Compatibility aliases for integrations written before the continuous scheduler.
    PLAN_RESEARCH = START_RESEARCH_COORDINATOR
    RUN_RESEARCH = MANAGE_RESEARCH_POOL


ProgressReporter = Callable[[Ascension, str], None]


def no_progress(ascension: Ascension, message: str) -> None:
    """Default reporter for library callers that do not request terminal output."""

    del ascension, message
