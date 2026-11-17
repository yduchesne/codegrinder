"""Tool-agnostic contracts for the three agent-driven workflow phases.

The controller must depend on these protocols, never on a particular agent CLI.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from .run_logging import LoggingSettings


@dataclass(frozen=True)
class WorkflowContext:
    """Immutable orchestration inputs. Paths must be resolved by the CLI."""

    repo: Path
    roadmap: Path
    state_dir: Path
    guide: Path
    configuration_sha256: str | None = None
    logging: LoggingSettings | None = None


@runtime_checkable
class Orchestrator(Protocol):
    """Public contract for an executable, resumable roadmap workflow."""

    def run(self) -> int: ...


class AgentExecutionError(RuntimeError):
    """Shared failure type for pluggable step implementations."""


@dataclass(frozen=True)
class PreflightTask:
    """Preflight context; never includes credentials or a mutable worktree."""

    repo: Path
    phase: str
    timeout_seconds: int


@dataclass(frozen=True)
class PreflightResult:
    """Standard adapter-level status without sensitive process output."""

    ok: bool
    code: str

    @classmethod
    def passed(cls) -> PreflightResult:
        return cls(ok=True, code="READY")

    @classmethod
    def failed(cls, code: str) -> PreflightResult:
        return cls(ok=False, code=code)


@runtime_checkable
class PreflightCheck(Protocol):
    """Check the configured harness/model, without changing repository state."""

    def preflight(self, task: PreflightTask) -> PreflightResult: ...


@dataclass(frozen=True)
class StructuredTask:
    worktree: Path
    pr_id: str
    phase: str
    prompt: str
    schema: Path
    artifacts: Path
    timeout_seconds: int


@dataclass(frozen=True)
class CodingProgress:
    """Sanitized, model-agnostic activity event emitted during coding."""

    pr_id: str
    phase: str
    sequence: int
    elapsed_seconds: float
    event_type: str
    tool_name: str | None = None


@dataclass(frozen=True)
class CodingResult:
    """Terminal evidence for one coding invocation; the Git diff remains authoritative."""

    pr_id: str
    phase: str
    adapter: str
    model: str
    status: str
    exit_code: int
    duration_seconds: float
    event_count: int
    tool_calls: int
    failed_tools: int
    session_id: str | None
    stdout_path: str
    stderr_path: str
    progress_path: str


ProgressCallback = Callable[[CodingProgress], None]


@dataclass(frozen=True)
class CodingTask:
    worktree: Path
    pr_id: str
    phase: str
    prompt: str
    artifacts: Path
    state_dir: Path
    fixup_number: int
    timeout_seconds: int
    on_progress: ProgressCallback | None = None


@runtime_checkable
class PlanningStep(PreflightCheck, Protocol):
    """Produce a schema-constrained, read-only plan response."""
    def execute(self, task: StructuredTask) -> dict: ...
    def identity(self) -> dict[str, str]: ...


@runtime_checkable
class CodingStep(PreflightCheck, Protocol):
    """Execute a frozen plan within the supplied worktree."""
    def execute(self, task: CodingTask) -> CodingResult: ...
    def identity(self) -> dict[str, str]: ...


@runtime_checkable
class ReviewStep(PreflightCheck, Protocol):
    """Produce a schema-constrained, read-only review response."""
    def execute(self, task: StructuredTask) -> dict: ...
    def identity(self) -> dict[str, str]: ...
