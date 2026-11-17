"""Deterministic simulated-crash tests for Codegrinder's execution recovery."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch

from codegrinder import engine
from codegrinder.interfaces import CodingResult
from codegrinder.recovery import process_identity, process_status, worktree_snapshot

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "roadmap.v1.md"
BASELINE = {"head": "abc123", "dirty": False, "fingerprint": "before", "path_count": 0}
EDITED = {"head": "abc123", "dirty": True, "fingerprint": "after", "path_count": 1}


class TestRecovery(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.roadmap = root / "roadmap.md"
        self.roadmap.write_text(SAMPLE.read_text())
        self.guide = root / "guide.md"
        self.guide.write_text("Authoring guide")
        self.state = root / "state"
        self.coder = Mock()
        self.coder.identity.return_value = {"adapter": "fake", "model": "test-model"}
        self.worker = engine.Controller(
            self.repo, self.roadmap, self.state, self.guide, coder=self.coder
        )
        self.pid = "PR-001"
        self.wt = self.worker.wt(self.pid)
        self.wt.mkdir(parents=True, exist_ok=True)
        (self.wt / ".git").write_text("gitdir: fake")
        self.addCleanup(self.worker.logs.close)

    def begin(self, stage="CODE", *, launched=False):
        entry = self.worker.entry(self.pid)
        entry["stage"] = stage
        self.worker.save()
        record = self.worker._begin_attempt(self.pid, stage)
        with patch.object(engine, "worktree_snapshot", return_value=dict(BASELINE)):
            self.worker._capture_baseline(self.pid, self.wt)
        if launched:
            record["launch_state"] = "LAUNCHED"
            record["process"] = {"pid": 987654, "boot_id": "test", "start_ticks": "100"}
            self.worker._save_attempt(self.pid)
        return record

    def resumed(self):
        again = engine.Controller(
            self.repo, self.roadmap, self.state, self.guide, coder=self.coder
        )
        self.addCleanup(again.logs.close)
        return again

    def reconcile(self, worker, snapshot=EDITED, process="EXITED"):
        with patch("codegrinder.recovery_runner.process_status", return_value=process), \
             patch("codegrinder.recovery_runner.worktree_snapshot", return_value=snapshot), \
             patch.object(engine, "modified_paths", return_value={"src/file.py"}):
            worker.recover_interrupted()

    def coding_result(self, attempt_id):
        return CodingResult(
            "PR-001", "code", "pi", "test-model", "COMPLETED", 0, 12.4, 8, 3, 0,
            "s-123", "out", "err", "progress", attempt_id,
        )

    def test_journal_is_written_before_step_invocation(self):
        record = self.begin("CODE")
        state = json.loads((self.state / "state.json").read_text())
        self.assertEqual("RUNNING", state["prs"][self.pid]["execution"]["status"])
        self.assertEqual(record["id"], state["prs"][self.pid]["execution"]["id"])
        attempt_path = self.worker._attempt_path(self.pid, record["id"])
        self.assertEqual("RUNNING", json.loads(attempt_path.read_text())["status"])
        self.assertEqual(0o600, attempt_path.stat().st_mode & 0o777)

    def test_finished_pi_result_from_crash_window_skips_duplicate_coding(self):
        rec = self.begin(launched=True)
        path = self.worker.artifact(self.pid) / f"code-0-{rec['id']}.result.json"
        path.write_text(json.dumps(asdict(self.coding_result(rec["id"]))))
        resumed = self.resumed()
        self.reconcile(resumed)
        self.assertEqual("VERIFY", resumed.entry(self.pid)["stage"])
        self.assertEqual("RECOVERED", resumed.entry(self.pid)["execution"]["status"])
        self.assertEqual(1, len(resumed.entry(self.pid)["coding_results"]))
        self.coder.execute.assert_not_called()

    def test_completed_result_wrong_attempt_is_rejected(self):
        rec = self.begin(launched=True)
        path = self.worker.artifact(self.pid) / f"code-0-{rec['id']}.result.json"
        path.write_text(json.dumps(asdict(self.coding_result("another-attempt"))))
        resumed = self.resumed()
        with self.assertRaisesRegex(engine.StopWorkflow, "RECOVERY_RESULT_MISMATCH"):
            self.reconcile(resumed)
        self.assertEqual("MANUAL_REQUIRED", resumed.entry(self.pid)["execution"]["recovery"]["decision"])
        self.assertEqual("CODE", resumed.entry(self.pid)["stage"])

    def test_partial_coding_returns_to_planning_and_is_not_repeated(self):
        self.begin(launched=True)
        resumed = self.resumed()
        self.reconcile(resumed)
        entry = resumed.entry(self.pid)
        self.assertEqual("REPLAN", entry["stage"])
        self.assertEqual(1, entry["fixups"])
        self.assertEqual("RECOVERED", entry["execution"]["status"])
        self.assertEqual("PARTIAL_CODING_REPLAN", entry["execution"]["recovery"]["decision"])
        feedback = json.loads((resumed.artifact(self.pid) / "feedback-1.json").read_text())
        self.assertEqual("interrupted_coding", feedback["source"])
        self.assertIn("RECOVERY-001", str(feedback["findings"]))
        self.coder.execute.assert_not_called()
        # Interruption is only reconciled once; a repeated recovery cannot
        # consume another fixup slot or overwrite the previous feedback.
        self.reconcile(resumed)
        self.assertEqual(1, resumed.entry(self.pid)["fixups"])

    def test_previous_process_still_active_blocks_restart(self):
        self.begin(launched=True)
        resumed = self.resumed()
        with self.assertRaisesRegex(engine.StopWorkflow, "RECOVERY_PREVIOUS_PROCESS_ACTIVE"):
            self.reconcile(resumed, process="ACTIVE")
        self.assertEqual("CODE", resumed.entry(self.pid)["stage"])
        self.coder.execute.assert_not_called()

    def test_unknown_process_identity_never_retries(self):
        self.begin(launched=True)
        resumed = self.resumed()
        with self.assertRaisesRegex(engine.StopWorkflow, "RECOVERY_PROCESS_IDENTITY_UNKNOWN"):
            self.reconcile(resumed, process="UNKNOWN")
        self.coder.execute.assert_not_called()

    def test_no_edits_after_launched_coding_remains_ambiguous(self):
        self.begin(launched=True)
        resumed = self.resumed()
        with self.assertRaisesRegex(engine.StopWorkflow, "RECOVERY_CODING_OUTCOME_UNKNOWN"):
            self.reconcile(resumed, snapshot=dict(BASELINE))
        self.assertEqual("CODE", resumed.entry(self.pid)["stage"])
        self.coder.execute.assert_not_called()

    def test_not_launched_can_retry_without_double_execution(self):
        self.begin()
        resumed = self.resumed()
        self.reconcile(resumed, snapshot=dict(BASELINE))
        rec = resumed.entry(self.pid)["execution"]
        self.assertEqual("INTERRUPTED", rec["status"])
        self.assertEqual("CODING_NOT_LAUNCHED_RETRY", rec["recovery"]["decision"])

    def test_read_only_plan_retries_only_after_stable_worktree(self):
        self.begin("PLAN", launched=True)
        resumed = self.resumed()
        self.reconcile(resumed, snapshot=dict(BASELINE))
        self.assertEqual("PLAN", resumed.entry(self.pid)["stage"])
        self.assertEqual("SAFE_STEP_RETRY", resumed.entry(self.pid)["execution"]["recovery"]["decision"])

    def test_changed_head_before_coding_recovery_blocks(self):
        self.begin(launched=True)
        resumed = self.resumed()
        snapshot = dict(EDITED, head="changed-sha")
        with self.assertRaisesRegex(engine.StopWorkflow, "RECOVERY_HEAD_CHANGED"):
            self.reconcile(resumed, snapshot=snapshot)

    def test_stage_already_advanced_before_terminal_attempt_saved(self):
        rec = self.begin("CODE", launched=True)
        entry = self.worker.entry(self.pid)
        entry["stage"] = "VERIFY"
        entry["coding_results"] = [asdict(self.coding_result(rec["id"]))]
        self.worker.save()
        resumed = self.resumed()
        self.reconcile(resumed)
        self.assertEqual("VERIFY", resumed.entry(self.pid)["stage"])
        self.assertEqual("RECOVERED", resumed.entry(self.pid)["execution"]["status"])
        self.coder.execute.assert_not_called()

    def test_publish_recovery_confirms_existing_github_pr(self):
        self.begin("PUBLISH")
        resumed = self.resumed()
        data = {"number": 45, "url": "https://example.invalid/pull/45",
                "headRefOid": "published-sha", "baseRefName": "main"}
        def mock_git(_wd, *args, **_kw):
            if args[0] == "branch":
                return "prflow/PR-001"
            if args[0] == "rev-parse":
                return "published-sha"
            return ""
        with patch.object(engine, "git", side_effect=mock_git), \
             patch("codegrinder.recovery_runner.worktree_snapshot", return_value=EDITED), \
             patch.object(engine, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(data), "")):
            resumed.recover_interrupted()
        self.assertEqual("REVIEW", resumed.entry(self.pid)["stage"])
        self.assertEqual("published-sha", resumed.entry(self.pid)["published_head"])
        self.assertEqual("RECOVERED", resumed.entry(self.pid)["execution"]["status"])

    def test_publish_unknown_remote_state_fails_closed(self):
        self.begin("PUBLISH")
        resumed = self.resumed()
        with patch("codegrinder.recovery_runner.worktree_snapshot", return_value=EDITED), \
             patch.object(engine, "git", return_value="prflow/PR-001"), \
             patch.object(engine, "run", side_effect=engine.StopWorkflow("CI_UNAVAILABLE")):
            with self.assertRaisesRegex(engine.StopWorkflow, "RECOVERY_PUBLISH_UNCONFIRMED"):
                resumed.recover_interrupted()

    def test_legacy_incomplete_stage_cannot_relaunch_code(self):
        ent = self.worker.entry(self.pid)
        ent["stage"] = "CODE"
        self.worker.save()
        with self.assertRaisesRegex(engine.StopWorkflow, "RECOVERY_LEGACY_STATE"):
            self.worker.recover_interrupted()

    def test_run_checks_recovery_before_preflight(self):
        self.begin(launched=True)
        resumed = self.resumed()
        with patch("codegrinder.recovery_runner.process_status", return_value="ACTIVE"), \
             patch.object(resumed, "preflight") as preflight, \
             patch.object(resumed, "process") as process, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, resumed.run())
        preflight.assert_not_called()
        process.assert_not_called()

    def test_snapshot_fingerprints_real_changed_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp)
            (wt / "file.txt").write_text("version 1")
            def fake_git(_wd, *args):
                if args[0] == "rev-parse":
                    return "sha"
                if args[0] == "status":
                    return " M file.txt"
                if args[0] == "ls-files":
                    return "file.txt\0"
                return ""
            first = worktree_snapshot(wt, fake_git)
            (wt / "file.txt").write_text("version 2")
            second = worktree_snapshot(wt, fake_git)
            self.assertNotEqual(first["fingerprint"], second["fingerprint"])
            self.assertTrue(second["dirty"])

    def test_process_identity_ties_pid_to_this_boot(self):
        actual = process_identity(os.getpid())
        if not actual["boot_id"] or not actual["start_ticks"]:
            self.skipTest("Linux /proc not available on this runner")
        self.assertEqual("ACTIVE", process_status(actual))
        self.assertEqual("UNKNOWN", process_status({"pid": 1}))


if __name__ == "__main__":
    unittest.main()
