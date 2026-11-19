"""Pure, dependency-aware PR selection and status normalization.

Completion labels are only scheduling hints until reconciled against GitHub by
the orchestrator. A selector never authorizes a merge or marks work complete.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


COMPLETED_ALIASES = frozenset({"done", "complete", "completed", "merged"})
PENDING_ALIASES = frozenset({
    "pending", "planned", "todo", "to do", "not started", "not_started",
    "in progress", "in_progress", "open",
})


class PRSelectionError(ValueError):
    """A roadmap PR cannot be safely selected."""


def is_completed(value: object) -> bool:
    """Recognize supported completion aliases, case-insensitively."""
    return isinstance(value, str) and value.strip().casefold() in COMPLETED_ALIASES


def normalize_roadmap_status(value: str) -> str:
    """Validate a user-supplied Markdown status and normalize its meaning."""
    normalized = value.strip().casefold()
    if normalized in COMPLETED_ALIASES:
        return "DONE"
    if normalized in PENDING_ALIASES:
        return "PENDING"
    raise PRSelectionError(f"ROADMAP_INVALID_STATUS: {value!r}")


@runtime_checkable
class PRSelector(Protocol):
    """Choose a PR without modifying roadmap or persistent state."""

    def next_pr(self, prs: list[dict], entries: dict[str, dict]) -> dict | None: ...


class SequentialPRSelector:
    """Take the first unfinished PR and reject unsatisfied dependencies."""

    def next_pr(self, prs: list[dict], entries: dict[str, dict]) -> dict | None:
        for pr in prs:
            pid = pr["id"]
            if is_completed(entries.get(pid, {}).get("stage")):
                continue
            for dependency in pr["depends_on"]:
                if not is_completed(entries.get(dependency, {}).get("stage")):
                    raise PRSelectionError(
                        f"DEPENDENCY_NOT_DONE: {pid} depends on {dependency}"
                    )
            return pr
        return None
