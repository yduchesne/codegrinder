"""Direct tests for durable, attempt-scoped final coding report evidence."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from codegrinder.coding_reports import (
    CodingReportError, MAX_REPORT_BYTES, extract_final_report,
    persist_report, report_path, verified_report,
)
from codegrinder.interfaces import CodingResult


class TestCodingReports(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.artifacts = Path(temp.name) / "artifacts" / "BM-01"
        self.attempt = "BM-01-00000003"
        self.path = report_path(
            self.artifacts, phase="fixup", fixup_number=1, attempt_id=self.attempt,
        )
        digest = persist_report(self.path, "Implemented the fix; tested the change.")
        self.result = CodingResult(
            "BM-01", "fixup", "pi", "model", "COMPLETED",
            0, 1.0, 8, 2, 0, "session", "", "", "", self.attempt,
            str(self.path), digest,
        )

    def verify(self, result: CodingResult | None = None) -> str:
        return verified_report(
            result or self.result, self.artifacts, pr_id="BM-01",
            phase="fixup", fixup_number=1, attempt_id=self.attempt,
        )

    def test_round_trip_private_report(self):
        self.assertEqual("Implemented the fix; tested the change.", self.verify())
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)
        self.assertEqual(
            hashlib.sha256(self.path.read_bytes()).hexdigest(),
            self.result.final_report_sha256,
        )

    def test_final_report_text_only_and_utf8(self):
        report = extract_final_report({
            "role": "assistant", "content": [
                {"type": "thinking", "thinking": "private"},
                {"type": "text", "text": " Done "},
                {"type": "tool_result", "text": "not report"},
                {"type": "text", "text": "Résumé ✓"},
            ],
        })
        self.assertEqual("Done\n\nRésumé ✓", report)
        digest = persist_report(self.path, report)
        self.assertEqual(report, self.verify(replace(self.result, final_report_sha256=digest)))

    def test_report_identity_rejections(self):
        variants = (
            replace(self.result, pr_id="BM-02"),
            replace(self.result, phase="code"),
            replace(self.result, attempt_id="old-attempt"),
            replace(self.result, status="FAILED"),
            replace(self.result, exit_code=1),
            replace(self.result, final_report_path="/tmp/external.md"),
            replace(self.result, final_report_sha256="not-a-sha"),
        )
        for value in variants:
            with self.subTest(value=value), self.assertRaises(CodingReportError):
                self.verify(value)

    def test_file_missing_tampered_symlink_and_oversize_are_rejected(self):
        self.path.write_text("tampered content", encoding="utf-8")
        with self.assertRaisesRegex(CodingReportError, "CODING_REPORT_DIGEST_MISMATCH"):
            self.verify()
        self.path.unlink()
        with self.assertRaisesRegex(CodingReportError, "CODING_REPORT_UNREADABLE"):
            self.verify()
        outside = self.artifacts.parent / "outside.md"
        outside.write_text("not the report")
        self.path.symlink_to(outside)
        with self.assertRaises(CodingReportError):
            self.verify()
        self.path.unlink()
        self.path.write_bytes(b"x" * (MAX_REPORT_BYTES + 1))
        with self.assertRaisesRegex(CodingReportError, "CODING_REPORT_FILE_INVALID"):
            self.verify()

    def test_missing_text_and_malformed_blocks_are_rejected(self):
        for blocks, message in (
            ([], "CODING_REPORT_EMPTY"),
            ([{"type": "thinking", "text": "hidden"}], "CODING_REPORT_EMPTY"),
            ([{"type": "text", "text": False}], "CODING_REPORT_CONTENT_INVALID"),
            ("not an array", "CODING_REPORT_CONTENT_INVALID"),
            ([{"type": "text", "text": "a" * (MAX_REPORT_BYTES + 1)}], "CODING_REPORT_TOO_LARGE"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(CodingReportError, message):
                extract_final_report({"content": blocks})


if __name__ == "__main__":
    unittest.main()
