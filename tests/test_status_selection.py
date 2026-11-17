"""Selection, completion reconciliation, and Orchestrator contract tests."""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codegrinder import engine
from codegrinder.interfaces import Orchestrator, WorkflowContext
from codegrinder.roadmap import RoadmapFormatError, parse_roadmap
from codegrinder.selection import (
    PRSelectionError,
    SequentialPRSelector,
    is_completed,
    normalize_roadmap_status,
)

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "roadmap.v1.md"


def merged_evidence(pr_id: str = "PR-001", number: int = 12) -> dict:
    return {
        "number": number,
        "url": f"https://github.com/example/app/pull/{number}",
        "state": "MERGED",
        "mergedAt": "2026-11-16T00:00:00Z",
        "headRefName": f"prflow/{pr_id}",
        "headRefOid": f"reviewed-{pr_id}",
        "baseRefName": "main",
        "mergeCommit": {"oid": f"merge-{pr_id}"},
    }


class TestSelection(unittest.TestCase):
    def test_completed_aliases_are_case_insensitive_and_trimmed(self):
        for status in ("DONE", "done", "DoNe", " completed ", "CoMpLeTe",
                       "MERGED", " merged "):
            with self.subTest(status=status):
                self.assertTrue(is_completed(status))
                self.assertEqual("DONE", normalize_roadmap_status(status))
        for status in (None, "", "pending", "in_progress", "doing", 1, "completed-ish"):
            with self.subTest(status=status):
                self.assertFalse(is_completed(status))

    def test_roadmap_pending_aliases_and_unknown_status(self):
        for status in ("pending", "PLANNED", "todo", "not started", "IN_PROGRESS", "open"):
            self.assertEqual("PENDING", normalize_roadmap_status(status))
        for status in ("done-ish", "review", ""):
            with self.assertRaises(PRSelectionError):
                normalize_roadmap_status(status)

    def test_sequential_selector_handles_aliases_for_dependencies(self):
        prs = [{"id": "A", "depends_on": []},
               {"id": "B", "depends_on": ["A"]}]
        selector = SequentialPRSelector()
        self.assertEqual("A", selector.next_pr(prs, {})["id"])
        for alias in ("DONE", " completed ", "merged", "CoMpLeTe"):
            with self.subTest(alias=alias):
                self.assertEqual("B", selector.next_pr(prs, {"A": {"stage": alias}})["id"])
        self.assertIsNone(selector.next_pr(prs, {
            "A": {"stage": "DONE"}, "B": {"stage": "completed"}
        }))
        with self.assertRaisesRegex(PRSelectionError, "DEPENDENCY_NOT_DONE"):
            selector.next_pr([prs[1]], {})

    def test_markdown_status_is_optional_and_normalized(self):
        raw = SAMPLE.read_text(encoding="utf-8")
        self.assertEqual("PENDING", parse_roadmap(raw)["prs"][0]["status"])
        self.assertNotIn("status", parse_roadmap(raw)["prs"][1])
        for label in ("DONE", "  completed ", "- mErGeD"):
            with self.subTest(label=label):
                text = raw.replace("### Status\npending", "### Status\n" + label, 1)
                self.assertEqual("DONE", parse_roadmap(text)["prs"][0]["status"])
        without = raw.replace("### Status\npending\n\n", "", 1)
        self.assertNotIn("status", parse_roadmap(without)["prs"][0])

    def test_markdown_malformed_status_is_rejected(self):
        raw = SAMPLE.read_text(encoding="utf-8")
        for field in ("", "done\npending", "probably complete", "- "):
            with self.subTest(field=field):
                value = raw.replace("### Status\npending", "### Status\n" + field, 1)
                with self.assertRaises(RoadmapFormatError):
                    parse_roadmap(value)


class TestOrchestrator(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.repo = base / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.roadmap = base / "roadmap.md"
        self.roadmap.write_text(SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        self.guide = base / "guide.md"
        self.guide.write_text("Guide text", encoding="utf-8")
        self.state_dir = base / "state"
        self.context = WorkflowContext(self.repo, self.roadmap, self.state_dir, self.guide)
        self.planner = Mock()
        self.coder = Mock()
        self.reviewer = Mock()
        self.planner.identity.return_value = {"adapter": "mock-plan"}
        self.coder.identity.return_value = {"adapter": "mock-code"}
        self.reviewer.identity.return_value = {"adapter": "mock-review"}

    def make_orchestrator(self, completed: bool = False):
        if completed:
            text = self.roadmap.read_text(encoding="utf-8")
            self.roadmap.write_text(
                text.replace("### Status\npending", "### Status\nCoMpLeTeD", 1),
                encoding="utf-8",
            )
        return engine.DefaultOrchestrator(
            self.context, planner=self.planner, coder=self.coder, reviewer=self.reviewer,
        )

    def test_default_orchestrator_implements_protocol_and_injects_steps(self):
        worker = self.make_orchestrator()
        self.assertIsInstance(worker, Orchestrator)
        self.assertIs(worker.planner, self.planner)
        self.assertIs(worker.coder, self.coder)
        self.assertIs(worker.reviewer, self.reviewer)
        self.assertIsInstance(worker.selector, SequentialPRSelector)
        self.assertEqual("PR-001", worker.next_pr()["id"])
        with patch.object(worker, "process", return_value=0) as process:
            self.assertEqual(0, worker.run())
        process.assert_called_once_with(follow=True, max_wait_minutes=None)

    def test_reconcile_completed_roadmap_status_and_resume_next_pr(self):
        worker = self.make_orchestrator(completed=True)
        with patch.object(worker, "_verified_merge", return_value=merged_evidence()) as verify:
            with patch.object(worker, "process", return_value=0):
                self.assertEqual(0, worker.run())
        verify.assert_called_once()
        entry = worker.entry("PR-001")
        self.assertEqual("DONE", entry["stage"])
        self.assertEqual("merge-PR-001", entry["merge_commit"])
        self.assertEqual(12, entry["pr_number"])
        self.assertEqual("PR-002", worker.next_pr()["id"])
        self.assertEqual("DONE", json.loads((self.state_dir / "state.json").read_text())
                         ["prs"]["PR-001"]["stage"])

    def test_missing_merge_fails_closed_before_workers(self):
        worker = self.make_orchestrator(completed=True)
        with patch.object(worker, "_verified_merge", side_effect=engine.StopWorkflow("PR_NOT_VERIFIED_MERGED")), \
             patch.object(worker, "process") as process, \
             contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(2, worker.run())
        self.assertIn("PR_NOT_VERIFIED_MERGED", err.getvalue())
        process.assert_not_called()
        self.planner.execute.assert_not_called()
        self.coder.execute.assert_not_called()

    def test_persisted_completion_alias_is_reconciled(self):
        worker = self.make_orchestrator()
        worker.entry("PR-001")["stage"] = " completed "
        worker.save()
        resumed = self.make_orchestrator()
        with patch.object(resumed, "_verified_merge", return_value=merged_evidence()):
            resumed.reconcile_completed()
        self.assertEqual("DONE", resumed.entry("PR-001")["stage"])
        self.assertEqual("PR-002", resumed.next_pr()["id"])

    def test_merge_evidence_conflict_fails_closed(self):
        worker = self.make_orchestrator(completed=True)
        worker.entry("PR-001")["pr_number"] = 100
        worker.save()
        with patch.object(worker, "_verified_merge", return_value=merged_evidence()):
            with self.assertRaisesRegex(engine.StopWorkflow, "MERGE_EVIDENCE_CONFLICT"):
                worker.reconcile_completed()

    def test_reviewed_head_conflict_fails_closed(self):
        worker = self.make_orchestrator(completed=True)
        worker.entry("PR-001")["accepted_head"] = "another-head"
        worker.save()
        with patch.object(worker, "_verified_merge", return_value=merged_evidence()):
            with self.assertRaisesRegex(engine.StopWorkflow, "accepted_head"):
                worker.reconcile_completed()

    def test_completed_persisted_state_without_roadmap_status_rechecks_merge(self):
        worker = self.make_orchestrator()
        worker.entry("PR-002")["stage"] = "DONE"
        worker.save()
        with patch.object(worker, "_verified_merge", return_value=merged_evidence("PR-002")) as verify:
            worker.reconcile_completed()
        verify.assert_called_once()
        self.assertEqual("merge-PR-002", worker.entry("PR-002")["merge_commit"])

    def test_fresh_pending_roadmap_does_not_query_gh(self):
        worker = self.make_orchestrator()
        with patch.object(worker, "_verified_merge") as verify:
            worker.reconcile_completed()
        verify.assert_not_called()

    def test_merge_lookup_rejects_missing_ambiguous_unmerged_and_wrong_base(self):
        worker = self.make_orchestrator()
        pr = worker.config["prs"][0]
        cases = [
            ([], "MERGE_EVIDENCE_AMBIGUOUS_OR_MISSING"),
            ([merged_evidence(), merged_evidence(number=13)], "MERGE_EVIDENCE_AMBIGUOUS_OR_MISSING"),
            ([dict(merged_evidence(), state="OPEN")], "PR_NOT_VERIFIED_MERGED"),
            ([dict(merged_evidence(), mergedAt=None)], "PR_NOT_VERIFIED_MERGED"),
            ([dict(merged_evidence(), baseRefName="develop")], "PR_MERGED_TO_WRONG_BASE"),
            ([dict(merged_evidence(), mergeCommit=None)], "PR_NOT_VERIFIED_MERGED"),
        ]
        for evidence, message in cases:
            with self.subTest(message=message, evidence=evidence), \
                 patch.object(engine, "run", return_value=subprocess.CompletedProcess(
                     [], 0, json.dumps(evidence), ""
                 )):
                with self.assertRaisesRegex(engine.StopWorkflow, message):
                    worker._verified_merge(pr)

    def test_merge_lookup_checks_exact_branch_and_json(self):
        worker = self.make_orchestrator()
        pr = worker.config["prs"][0]
        matching = merged_evidence()
        other = merged_evidence("UNRELATED")
        with patch.object(engine, "run", return_value=subprocess.CompletedProcess(
            [], 0, json.dumps([other, matching]), ""
        )) as command:
            result = worker._verified_merge(pr)
            self.assertEqual(12, result["number"])
            self.assertIn("prflow/PR-001", command.call_args.args[0])
        with patch.object(engine, "run", return_value=subprocess.CompletedProcess([], 0, "{}", "")):
            with self.assertRaisesRegex(engine.StopWorkflow, "MERGE_EVIDENCE_INVALID"):
                worker._verified_merge(pr)


if __name__ == "__main__":
    unittest.main()
