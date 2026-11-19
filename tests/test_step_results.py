"""Step execution and functional verdicts are independent, validated dimensions."""
from __future__ import annotations

import errno
import subprocess
import unittest

from codegrinder.step_results import (
    StepExecutionError, StepResult, classify_check_failure, execution_error,
)


class TestStepResults(unittest.TestCase):
    def test_completed_step_can_be_positive_negative_blocked_or_pending(self):
        for outcome, verdict in (
            ("POSITIVE", "PASS"),
            ("NEGATIVE", "CHANGES_REQUIRED"),
            ("BLOCKED", "BLOCKED"),
            ("PENDING", "WAITING"),
        ):
            result = StepResult("REVIEW", "review", "PR-1-00000001", "SUCCEEDED", outcome, verdict)
            self.assertEqual("SUCCEEDED", result.execution_status)
            self.assertEqual(outcome, result.functional_outcome)
            self.assertIsNone(result.failure_category)

    def test_failed_or_interrupted_step_has_no_code_quality_verdict(self):
        for status in ("FAILED", "INTERRUPTED"):
            result = StepResult(
                "CODE", "code", "PR-1-00000001", status,
                failure_category="ENVIRONMENT" if status == "FAILED" else "RECOVERY",
                error_code="DISK_FULL" if status == "FAILED" else "SAFE_STEP_RETRY",
            )
            self.assertIsNone(result.functional_outcome)
            self.assertIsNone(result.verdict)
        for invalid in (
            dict(execution_status="SUCCEEDED", functional_outcome=None, verdict="PASS"),
            dict(execution_status="FAILED", functional_outcome="NEGATIVE",
                 failure_category="ENVIRONMENT", error_code="DISK_FULL"),
            dict(execution_status="FAILED", failure_category="ENVIRONMENT"),
            dict(execution_status="INTERRUPTED", failure_category="UNRECOGNIZED", error_code="RETRY"),
            dict(execution_status="SUCCEEDED", functional_outcome="NEGATIVE", verdict="INVALID VERDICT"),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                StepResult(stage="VERIFY", step="verify", attempt_id=None, **invalid)

    def test_classifies_operational_faults(self):
        cases = (
            (FileNotFoundError("missing"), ("ENVIRONMENT", "COMMAND_NOT_FOUND")),
            (OSError(errno.ENOSPC, "disk full"), ("ENVIRONMENT", "DISK_FULL")),
            (OSError(errno.ENOMEM, "no memory"), ("ENVIRONMENT", "OUT_OF_MEMORY")),
            (subprocess.TimeoutExpired("pytest", 5), ("ENVIRONMENT", "PROCESS_TIMEOUT")),
            (StepExecutionError("VERIFY_ENVIRONMENT_FAILURE", "ENVIRONMENT"),
             ("ENVIRONMENT", "VERIFY_ENVIRONMENT_FAILURE")),
            (ValueError("REVIEW_SCHEMA_INVALID: bad json"), ("PROTOCOL", "REVIEW_SCHEMA_INVALID")),
            (ValueError("CONFIG_INVALID: bad settings"), ("CONFIGURATION", "CONFIG_INVALID")),
            (ValueError("PI_FAILED(127): worker unavailable"), ("ENVIRONMENT", "PI_FAILED")),
            (ValueError("CODEX_FAILED(1): no response"), ("ENVIRONMENT", "CODEX_FAILED")),
            (ValueError("RECOVERY_HEAD_CHANGED: invalid"), ("RECOVERY", "RECOVERY_HEAD_CHANGED")),
        )
        for exc, expected in cases:
            with self.subTest(exc=exc):
                self.assertEqual(expected, execution_error(exc))

    def test_check_classification_never_confuses_known_system_fault_with_failed_test(self):
        self.assertEqual("ENVIRONMENT", classify_check_failure(127, "", "bash: missing: command not found"))
        self.assertEqual("ENVIRONMENT", classify_check_failure(1, "", "No space left on device"))
        self.assertEqual("ENVIRONMENT", classify_check_failure(1, "", "[Errno 28] Failed to write file"))
        self.assertEqual("ENVIRONMENT", classify_check_failure(-9, "", ""))
        self.assertEqual("ENVIRONMENT", classify_check_failure(126, "", ""))
        self.assertEqual("NEGATIVE", classify_check_failure(1, "", "AssertionError: unexpected result"))
        self.assertEqual("NEGATIVE", classify_check_failure(4, "", "failed"))
        self.assertEqual("NEGATIVE", classify_check_failure(1, "1 failed in 0.5s", ""))
        self.assertEqual("NEGATIVE", classify_check_failure(1, "Found 1 error", ""))
        self.assertEqual("UNKNOWN", classify_check_failure(2, "", "unknown exit"))
        self.assertEqual("UNKNOWN", classify_check_failure(2, "", "failed to launch service"))
        with self.assertRaisesRegex(ValueError, "CHECK_FAILURE_STATUS_INVALID"):
            classify_check_failure(0, "", "")


if __name__ == "__main__":
    unittest.main()
