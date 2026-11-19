"""Explicit PR selection, forced supersession, and fail-closed recovery gates."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codegrinder import engine
from codegrinder.selection_override import (
    SelectionOverrideError, assess_force_safety, commit_force_selection,
    selected_pr, selection_conflicts, override_warning,
)

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "roadmap.v1.md"


class TestSelectionOverride(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.roadmap = root / "roadmap.md"
        self.roadmap.write_text(SAMPLE.read_text(encoding="utf-8"))
        self.guide = root / "guide.md"
        self.guide.write_text("Authoring guide")
        self.state = root / "state"
        self.worker = engine.Controller(self.repo, self.roadmap, self.state, self.guide)
        self.addCleanup(self.worker.logs.close)
        # Independent PR, so selection can override earlier pending PRs.
        self.worker.config["prs"].append({
            "id": "PR-003", "title": "Independent work", "goal": "Independent task",
            "depends_on": [], "allowed_paths": ["src/**"],
            "checks": ["test -d src"], "acceptance": ["tests pass"],
        })

    def active(self, pid="PR-001", stage="CODE", *, status="RUNNING", launched=False):
        ent = self.worker.entry(pid)
        ent["stage"] = stage
        self.worker.save()
        record = self.worker._begin_attempt(pid, stage)
        record["status"] = status
        if launched:
            record["launch_state"] = "LAUNCHED"
            record["process"] = {"pid": 222, "boot_id": "boot", "start_ticks": "55"}
        self.worker._save_attempt(pid)
        return record

    def test_exact_selection_does_not_skip_dependency_or_done_checks(self):
        self.assertEqual("PR-003", selected_pr(
            self.worker.config, self.worker.state, "PR-003",
        )["id"])
        for value, error in (
            ("PR 15", "PR_SELECTION_INVALID_ID"),
            ("NO-SUCH-PR", "PR_SELECTION_NOT_FOUND"),
        ):
            with self.subTest(value=value), self.assertRaisesRegex(SelectionOverrideError, error):
                selected_pr(self.worker.config, self.worker.state, value)
        with self.assertRaisesRegex(SelectionOverrideError, "PR_DEPENDENCIES_UNSATISFIED"):
            selected_pr(self.worker.config, self.worker.state, "PR-002")
        self.worker.entry("PR-001").update({
            "stage": "DONE", "merged_at": "2026-11-18T00:00:00Z",
            "merge_commit": "confirmed",
        })
        self.assertEqual("PR-002", selected_pr(
            self.worker.config, self.worker.state, "PR-002",
        )["id"])
        self.worker.entry("PR-002")["stage"] = "DONE"
        with self.assertRaisesRegex(SelectionOverrideError, "PR_ALREADY_COMPLETED"):
            selected_pr(self.worker.config, self.worker.state, "PR-002")

    def test_nonforced_selected_pr_aborts_before_preflight_if_recovery_pending(self):
        record = self.active()
        with (
            patch.object(self.worker, "preflight") as preflight,
            patch.object(self.worker, "process") as process,
            contextlib.redirect_stderr(io.StringIO()) as output,
        ):
            self.assertEqual(2, self.worker.run(pr_id="PR-003"))
        preflight.assert_not_called()
        process.assert_not_called()
        self.assertIn("RECOVERY_REQUIRED", output.getvalue())
        self.assertIn("PR-001", output.getvalue())
        self.assertIn(record["id"], output.getvalue())
        self.assertIn("grind run --pr PR-003 -f", output.getvalue())
        self.assertEqual("RUNNING", self.worker.entry("PR-001")["execution"]["status"])

    def test_success_without_stage_advancement_requires_recovery_not_duplicate_code(self):
        self.active(stage="CODE", status="SUCCEEDED")
        issues = selection_conflicts(self.worker.config, self.worker.state, "PR-001")
        self.assertEqual(1, len(issues))
        self.assertTrue(issues[0]["recovery"])
        self.assertIn("RECOVERY_REQUIRED", override_warning("PR-001", issues))
        with (
            patch.object(self.worker, "preflight") as preflight,
            patch.object(self.worker, "process") as execution,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(2, self.worker.run(pr_id="PR-001"))
        preflight.assert_not_called()
        execution.assert_not_called()

    def test_healthy_incomplete_other_pr_is_a_selection_conflict_not_recovery(self):
        self.active(stage="WAIT_MERGE", status="SUCCEEDED")
        issues = selection_conflicts(self.worker.config, self.worker.state, "PR-003")
        self.assertEqual("PR_SELECTION_CONFLICT", override_warning("PR-003", issues).split(":")[0])
        self.assertFalse(issues[0]["recovery"])

    def test_force_is_audited_and_never_reuses_or_deletes_old_worktree(self):
        record = self.active()
        old = self.worker.wt("PR-001")
        old.mkdir(parents=True)
        (old / "original.txt").write_text("untouched partial edits")
        # Pretend valid non-conflicting Git branch and remote lookups.
        with (
            patch("codegrinder.engine.git", return_value=""),
            patch.object(self.worker, "preflight"),
            patch.object(self.worker, "reconcile_completed"),
            patch.object(self.worker, "process", return_value=0) as process,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(0, self.worker.run(pr_id="PR-003", force=True))
        process.assert_called_once_with(
            follow=True, max_wait_minutes=None, selected_pr_id="PR-003",
        )
        self.assertEqual("SUPERSEDED", self.worker.entry("PR-001")["execution"]["status"])
        self.assertEqual("STOPPED", self.worker.entry("PR-001")["stage"])
        self.assertEqual(record["id"], self.worker.entry("PR-001")["execution"]["id"])
        self.assertEqual("untouched partial edits", (old / "original.txt").read_text())
        self.assertEqual("PLAN", self.worker.entry("PR-003")["stage"])
        self.assertEqual(
            "prflow/PR-003-force-00000001", self.worker._branch("PR-003"),
        )
        self.assertNotEqual(self.worker.wt("PR-003"), old)
        self.assertEqual(self.state / "worktrees" / "PR-003-force-00000001",
                         self.worker.wt("PR-003"))
        archived = json.loads(self.worker._attempt_path("PR-001", record["id"]).read_text())
        self.assertEqual("SUPERSEDED", archived["status"])
        self.assertEqual("PR-003", archived["superseded_by"]["pr_id"])
        self.assertEqual(1, len(self.worker.state["override_events"]))

    def test_force_restart_of_target_archives_old_state_and_artifacts(self):
        rec = self.active(stage="CODE")
        old_artifacts = self.worker.artifact("PR-001")
        (old_artifacts / "plan.md").write_text("historical plan")
        old_worktree = self.worker.wt("PR-001")
        old_worktree.mkdir(parents=True)
        (old_worktree / "partial.py").write_text("# keep this")
        with patch("codegrinder.engine.git", return_value=""):
            issues = selection_conflicts(self.worker.config, self.worker.state, "PR-001")
            commit_force_selection(self.worker, "PR-001", issues)
        self.assertEqual("PLAN", self.worker.entry("PR-001")["stage"])
        self.assertEqual("prflow/PR-001-force-00000001", self.worker._branch("PR-001"))
        self.assertEqual(self.state / "artifacts" / "PR-001-force-00000001",
                         self.worker.artifact("PR-001"))
        self.assertEqual("historical plan", (old_artifacts / "plan.md").read_text())
        self.assertEqual("# keep this", (old_worktree / "partial.py").read_text())
        archived = json.loads((self.state / "overrides" / "PR-001" / "00000001.json").read_text())
        self.assertEqual(rec["id"], archived["execution"]["id"])
        self.assertEqual(1, self.worker.entry("PR-001")["attempt_counter"])
        self.assertEqual(1, self.worker.state["override_counter"])
        self.assertTrue(self.worker.entry("PR-001")["selection_override"]["prior_target_sha256"])
        with patch("codegrinder.engine.git", return_value=""):
            commit_force_selection(self.worker, "PR-001", [])
        self.assertEqual("prflow/PR-001-force-00000002", self.worker._branch("PR-001"))
        self.assertTrue((self.state / "overrides" / "PR-001" / "00000002.json").exists())

    def test_persisted_forced_workspace_paths_are_strictly_validated(self):
        entry = self.worker.entry("PR-001")
        for payload in (
            {"branch": "prflow/PR-001-force-00000001", "worktree_name": "../outside",
             "artifacts_name": "../outside"},
            {"branch": "prflow/PR-001-force-00000001", "worktree_name": "PR-001-force-00000001",
             "artifacts_name": "../../escape"},
            {"branch": "prflow/another", "worktree_name": "PR-001-force-00000001",
             "artifacts_name": "PR-001-force-00000001"},
        ):
            with self.subTest(payload=payload):
                entry.update(payload)
                with self.assertRaisesRegex(engine.StopWorkflow, "WORKFLOW_OVERRIDE_PATH_INVALID"):
                    self.worker.artifact("PR-001")
                with self.assertRaisesRegex(engine.StopWorkflow, "WORKFLOW_OVERRIDE_PATH_INVALID"):
                    self.worker.wt("PR-001")
                with self.assertRaisesRegex(engine.StopWorkflow, "WORKFLOW_OVERRIDE_PATH_INVALID"):
                    self.worker._branch("PR-001")
        entry.update({
            "branch": "prflow/PR-001-force-00000002",
            "worktree_name": "PR-001-force-00000002",
            "artifacts_name": "PR-001-force-00000002",
        })
        self.assertEqual(self.state / "worktrees" / "PR-001-force-00000002",
                         self.worker.wt("PR-001"))

    def test_force_archives_completed_but_unmerged_target_stage(self):
        rec = self.active(status="SUCCEEDED")
        self.worker.entry("PR-001")["stage"] = "VERIFY"
        self.worker.save()
        with patch("codegrinder.engine.git", return_value=""):
            issues = selection_conflicts(self.worker.config, self.worker.state, "PR-001")
            self.assertEqual([], issues)
            commit_force_selection(self.worker, "PR-001", issues)
        event = self.worker.state["override_events"][-1]
        self.assertEqual(["PR-001"], event["superseded_prs"])
        self.assertEqual("PLAN", self.worker.entry("PR-001")["stage"])
        old = json.loads((self.state / "overrides" / "PR-001" / "00000001.json").read_text())
        self.assertEqual("VERIFY", old["stage"])
        self.assertEqual(rec["id"], old["execution"]["id"])

    def test_force_checks_terminal_target_process_even_without_conflicts(self):
        record = self.active(status="SUCCEEDED", launched=True)
        self.worker.entry("PR-001")["stage"] = "VERIFY"
        self.worker.save()
        issues = selection_conflicts(self.worker.config, self.worker.state, "PR-001")
        self.assertEqual([], issues)
        with patch("codegrinder.selection_override.process_status", return_value="ACTIVE"):
            with self.assertRaisesRegex(SelectionOverrideError, "FORCE_PROCESS_ACTIVE"):
                assess_force_safety(self.worker, "PR-001", issues)
        self.assertEqual("SUCCEEDED", record["status"])

    def test_active_or_unknown_prior_worker_cannot_be_forced(self):
        for state in ("ACTIVE", "UNKNOWN"):
            with self.subTest(process=state):
                self.worker.state["prs"].clear()
                rec = self.active(launched=True)
                with (
                    patch("codegrinder.selection_override.process_status", return_value=state),
                    patch("codegrinder.engine.git") as git,
                ):
                    with self.assertRaisesRegex(SelectionOverrideError, f"FORCE_PROCESS_{state}"):
                        assess_force_safety(
                            self.worker, "PR-003",
                            selection_conflicts(self.worker.config, self.worker.state, "PR-003"),
                        )
                    git.assert_not_called()
                self.assertEqual("RUNNING", self.worker.entry("PR-001")["execution"]["status"])
                self.assertEqual(rec["id"], self.worker.entry("PR-001")["execution"]["id"])

    def test_terminal_failed_record_with_live_worker_is_never_forced(self):
        self.active(status="FAILED", launched=True)
        issues = selection_conflicts(self.worker.config, self.worker.state, "PR-003")
        with patch("codegrinder.selection_override.process_status", return_value="ACTIVE"):
            with self.assertRaisesRegex(SelectionOverrideError, "FORCE_PROCESS_ACTIVE"):
                assess_force_safety(self.worker, "PR-003", issues)
        record = self.worker.entry("PR-001")["execution"]
        record["process"] = None
        self.worker._save_attempt("PR-001")
        with self.assertRaisesRegex(SelectionOverrideError, "FORCE_PROCESS_UNKNOWN"):
            assess_force_safety(self.worker, "PR-003", issues)

    def test_terminal_succeeded_record_without_launched_process_identity_is_unsafe(self):
        record = self.active(status="SUCCEEDED", launched=True)
        record["process"] = None
        self.worker._save_attempt("PR-001")
        issues = selection_conflicts(self.worker.config, self.worker.state, "PR-003")
        with patch("codegrinder.engine.git") as git:
            with self.assertRaisesRegex(SelectionOverrideError, "FORCE_PROCESS_UNKNOWN"):
                assess_force_safety(self.worker, "PR-003", issues)
            git.assert_not_called()

    def test_previous_forced_target_remote_branch_blocks_new_force(self):
        target = self.worker.entry("PR-001")
        target.update({
            "stage": "CODE",
            "branch": "prflow/PR-001-force-00000001",
            "worktree_name": "PR-001-force-00000001",
            "artifacts_name": "PR-001-force-00000001",
        })
        self.worker.save()

        def remote(_repo, *args):
            if args[:3] == ("ls-remote", "--heads", "origin") and (
                args[3] == "prflow/PR-001-force-00000001"
            ):
                return "abc123\\trefs/heads/prflow/PR-001-force-00000001"
            return ""

        with patch("codegrinder.engine.git", side_effect=remote):
            with self.assertRaisesRegex(
                SelectionOverrideError, "FORCE_PREVIOUS_REMOTE_BRANCH_PRESENT"
            ):
                assess_force_safety(self.worker, "PR-001", [])

    def test_unknown_launch_and_untracked_publish_are_not_forceable(self):
        record = self.active(launched=True)
        record["process"] = None
        self.worker._save_attempt("PR-001")
        issues = selection_conflicts(self.worker.config, self.worker.state, "PR-003")
        with self.assertRaisesRegex(SelectionOverrideError, "FORCE_PROCESS_UNKNOWN"):
            assess_force_safety(self.worker, "PR-003", issues)
        self.worker.state["prs"].clear()
        self.active(stage="PUBLISH")
        issues = selection_conflicts(self.worker.config, self.worker.state, "PR-003")
        with self.assertRaisesRegex(SelectionOverrideError, "FORCE_UNTRACKED_PROCESS"):
            assess_force_safety(self.worker, "PR-003", issues)

    def test_fresh_force_rejects_existing_target_pr_and_remote_branch(self):
        self.worker.entry("PR-001")["pr_number"] = 17
        with self.assertRaisesRegex(SelectionOverrideError, "FORCE_EXISTING_TARGET_PR"):
            assess_force_safety(self.worker, "PR-001", [])
        self.worker.entry("PR-001").pop("pr_number")
        def existing_candidate(_repo, *args):
            return "prflow/PR-001-force-00000001" if args[:2] == ("branch", "--list") else ""
        with patch("codegrinder.engine.git", side_effect=existing_candidate):
            with self.assertRaisesRegex(SelectionOverrideError, "FORCE_BRANCH_EXISTS"):
                assess_force_safety(self.worker, "PR-001", [])
        def existing_original_remote(_repo, *args):
            if args[:3] == ("ls-remote", "--heads", "origin") and args[3] == "prflow/PR-001":
                return "abc\\trefs/heads/prflow/PR-001"
            return ""
        with patch("codegrinder.engine.git", side_effect=existing_original_remote):
            with self.assertRaisesRegex(SelectionOverrideError, "FORCE_TARGET_REMOTE_BRANCH_PRESENT"):
                assess_force_safety(self.worker, "PR-001", [])

    def test_legacy_unjournaled_target_prompts_for_recovery_instead_of_resuming(self):
        self.worker.entry("PR-003")["stage"] = "CODE"
        conflicts = selection_conflicts(self.worker.config, self.worker.state, "PR-003")
        self.assertEqual(1, len(conflicts))
        self.assertTrue(conflicts[0]["recovery"])
        self.assertIn("RECOVERY_REQUIRED", override_warning("PR-003", conflicts))
        with self.assertRaisesRegex(SelectionOverrideError, "FORCE_LEGACY_STATE_UNSAFE"):
            assess_force_safety(self.worker, "PR-003", conflicts)

    def test_force_refuses_missing_or_mismatched_attempt_journal(self):
        record = self.active()
        issues = selection_conflicts(self.worker.config, self.worker.state, "PR-003")
        path = self.worker._attempt_path("PR-001", record["id"])
        path.unlink()
        with self.assertRaisesRegex(SelectionOverrideError, "FORCE_JOURNAL_INVALID"):
            assess_force_safety(self.worker, "PR-003", issues)
        path.write_text(json.dumps({"id": record["id"], "stage": "REVIEW"}))
        with self.assertRaisesRegex(SelectionOverrideError, "FORCE_JOURNAL_MISMATCH"):
            assess_force_safety(self.worker, "PR-003", issues)

    def test_force_cannot_skip_unmerged_roadmap_dependency(self):
        with (
            patch("codegrinder.engine.git", return_value=""),
            patch.object(self.worker, "preflight"),
            patch.object(self.worker, "reconcile_completed"),
            patch.object(self.worker, "process") as process,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(2, self.worker.run(pr_id="PR-002", force=True))
        process.assert_not_called()
        self.assertIn("PR_DEPENDENCIES_UNSATISFIED", stderr.getvalue())
        self.assertNotIn("override_events", self.worker.state)

    def test_force_cannot_skip_a_target_already_at_publish_boundary(self):
        self.worker.entry("PR-001")["stage"] = "PUBLISH"
        with self.assertRaisesRegex(SelectionOverrideError, "FORCE_EXISTING_TARGET_PR"):
            assess_force_safety(self.worker, "PR-001", [])

    def test_force_can_supersede_known_healthy_waiting_pr_without_touching_remote(self):
        record = self.active(stage="WAIT_MERGE", status="SUCCEEDED")
        with patch("codegrinder.engine.git", return_value=""):
            conflicts = selection_conflicts(self.worker.config, self.worker.state, "PR-003")
            commit_force_selection(self.worker, "PR-003", conflicts)
        previous = self.worker.entry("PR-001")
        self.assertEqual("STOPPED", previous["stage"])
        self.assertEqual("SUCCEEDED", previous["execution"]["status"])
        self.assertEqual(record["id"], previous["execution"]["id"])
        self.assertEqual("PR-003", previous["superseded_by"]["pr_id"])

    def test_selected_execution_stops_after_selected_pr_merges(self):
        calls = []
        def advance(pr):
            calls.append(pr["id"])
            self.worker.entry(pr["id"])["stage"] = "DONE"
            self.worker.entry(pr["id"])["merged_at"] = "now"
            self.worker.entry(pr["id"])["merge_commit"] = "sha"
            return True
        with (
            patch.object(self.worker, "recover_interrupted"),
            patch.object(self.worker, "advance", side_effect=advance),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(0, self.worker.process(selected_pr_id="PR-003"))
        self.assertEqual(["PR-003"], calls)
        self.assertIn("SELECTED_PR_COMPLETE: PR-003 confirmed merged", output.getvalue())
        self.assertNotIn("ROADMAP_COMPLETE", output.getvalue())


if __name__ == "__main__":
    unittest.main()
