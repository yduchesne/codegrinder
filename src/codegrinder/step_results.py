"""Two-dimensional, durable step results and fail-closed error classification.

Execution status answers *did the step run reliably?*, whereas functional
outcome answers *what did the completed step conclude?*. Errors never fabricate
a negative assessment of source quality.
"""
from __future__ import annotations

import errno
import re
import subprocess
from dataclasses import dataclass

EXECUTION_STATUSES = frozenset({"SUCCEEDED", "FAILED", "INTERRUPTED"})
FUNCTIONAL_OUTCOMES = frozenset({"POSITIVE", "NEGATIVE", "BLOCKED", "PENDING"})
FAILURE_CATEGORIES = frozenset({"ENVIRONMENT", "PROTOCOL", "CONFIGURATION", "RECOVERY"})
_TOKEN = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")

# These are deliberately conservative machine-observable signatures, not an
# attempt to infer root cause from arbitrary natural-language agent responses.
_ENVIRONMENT_MARKERS = (
    "no space left on device", "enospc", "errno 28",
    "disk quota exceeded", "out of memory", "cannot allocate memory",
    "read-only file system", "input/output error",
    "command not found", "permission denied",
)
_TEST_FAILURE_MARKERS = (
    "assertionerror", "assertion failed", "tests failed", "test failed",
    "test result: fail", "== failures ==", "failed tests/", "failed in ",
    "found 1 error", "errors found", "error[e", "error[f",
    "error: expected", "error: unused",
)


class StepExecutionError(RuntimeError):
    """A step cannot deliver a trustworthy outcome for a classified reason."""

    def __init__(self, code: str, category: str):
        if category not in FAILURE_CATEGORIES or not _TOKEN.fullmatch(code):
            raise ValueError("STEP_EXECUTION_ERROR_INVALID")
        super().__init__(code)
        self.failure_category = category


@dataclass(frozen=True)
class StepResult:
    """Result tied to a PR stage and attempt; validated before persistence."""

    stage: str
    step: str
    attempt_id: str | None
    execution_status: str
    functional_outcome: str | None = None
    verdict: str | None = None
    failure_category: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not self.stage or not self.step or not all(
            isinstance(v, str) for v in (self.stage, self.step)
        ):
            raise ValueError("STEP_RESULT_IDENTITY_INVALID")
        if self.attempt_id is not None and (
            not isinstance(self.attempt_id, str) or not self.attempt_id
        ):
            raise ValueError("STEP_RESULT_ATTEMPT_INVALID")
        if self.execution_status not in EXECUTION_STATUSES:
            raise ValueError("STEP_RESULT_EXECUTION_INVALID")
        if self.execution_status == "SUCCEEDED":
            if (self.functional_outcome not in FUNCTIONAL_OUTCOMES or
                    not isinstance(self.verdict, str) or not _TOKEN.fullmatch(self.verdict) or
                    self.failure_category is not None or self.error_code is not None):
                raise ValueError("STEP_RESULT_OUTCOME_INVALID")
        elif (self.functional_outcome is not None or self.verdict is not None or
              self.failure_category not in FAILURE_CATEGORIES or
              not isinstance(self.error_code, str) or not _TOKEN.fullmatch(self.error_code)):
            raise ValueError("STEP_RESULT_FAILURE_INVALID")


def execution_error(exc: Exception) -> tuple[str, str]:
    """Return safe (category, code) for a failed invocation or transition."""
    if isinstance(exc, StepExecutionError):
        return exc.failure_category, str(exc)
    if isinstance(exc, subprocess.TimeoutExpired):
        return "ENVIRONMENT", "PROCESS_TIMEOUT"
    if isinstance(exc, FileNotFoundError):
        return "ENVIRONMENT", "COMMAND_NOT_FOUND"
    if isinstance(exc, OSError):
        if exc.errno == errno.ENOSPC:
            return "ENVIRONMENT", "DISK_FULL"
        if exc.errno == errno.ENOMEM:
            return "ENVIRONMENT", "OUT_OF_MEMORY"
        if exc.errno == errno.EACCES:
            return "ENVIRONMENT", "PERMISSION_DENIED"
        return "ENVIRONMENT", "OS_ERROR"
    msg = str(exc)
    code_match = re.match(r"^([A-Z][A-Z0-9_]{2,79})(?=:|[(]|$)", msg)
    code = code_match.group(1) if code_match else "UNCLASSIFIED_EXCEPTION"
    if code.startswith("RECOVERY_") or code in {"UNRECONCILED_ATTEMPT", "ATTEMPT_NOT_RUNNING"}:
        return "RECOVERY", code
    if code.startswith(("CONFIG_", "ROADMAP_", "GUIDE_")) or code in {
        "AGENT_NOT_CONFIGURED", "NO_REQUIRED_CI_CONFIGURED",
    }:
        return "CONFIGURATION", code
    if code.startswith((
        "PI_INVALID", "PI_INCOMPLETE", "PI_MODEL_SELECTION", "CODEX_INVALID",
        "CODEX_MISSING", "PLAN_SCHEMA", "REVIEW_SCHEMA", "REVIEW_INCONSISTENT",
        "CODING_RESULT_INVALID", "CODING_REPORT_", "PROMPT_",
    )):
        return "PROTOCOL", code
    if code.startswith((
        "PI_FAILED", "PI_LAUNCH", "PI_TIMEOUT", "CODEX_FAILED",
        "COMMAND_FAILED", "CI_STATUS_UNAVAILABLE", "ADAPTER_PREFLIGHT",
    )):
        return "ENVIRONMENT", code
    # Unknown failures must never be treated as negative code-quality findings.
    return "PROTOCOL", code


def classify_check_failure(returncode: int, stdout: str, stderr: str) -> str:
    """Classify a nonzero deterministic check as ENVIRONMENT, NEGATIVE or UNKNOWN.

    UNKNOWN stops for operator reconciliation. A process's exit status alone
    does not prove a test assertion failed. Use conservative output markers.
    """
    if type(returncode) is not int or returncode == 0:
        raise ValueError("CHECK_FAILURE_STATUS_INVALID")
    output = (stdout + "\n" + stderr).casefold()
    if returncode < 0 or returncode in {124, 126, 127, 137, 143}:
        return "ENVIRONMENT"
    if any(marker in output for marker in _ENVIRONMENT_MARKERS):
        return "ENVIRONMENT"
    if output.strip() in {"failed", "failure"}:
        return "NEGATIVE"
    if any(marker in output for marker in _TEST_FAILURE_MARKERS):
        return "NEGATIVE"
    return "UNKNOWN"
