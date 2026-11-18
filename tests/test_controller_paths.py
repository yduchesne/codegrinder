"""Unit coverage for the deterministic controller, without real agent or GitHub calls."""
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codegrinder import engine
from codegrinder.coding_reports import persist_report, report_path
from codegrinder.roadmap import parse_roadmap
from codegrinder.interfaces import CodingResult

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "roadmap.v1.md"
VALID_PLAN = "## Scope\n" + "x" * 310 + "\n## Steps\n## Tests\n## Decision Matrix\n## Stop Conditions"


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(["test"], returncode, stdout, stderr)


class ControllerFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.roadmap = self.root / "roadmap.md"
        self.roadmap.write_text(SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        self.guide = self.root / "guide.md"
        self.guide.write_text("Authoring guide for plans.", encoding="utf-8")
        self.state = self.root / "state"
        self.controller = engine.Controller(self.repo, self.roadmap, self.state, self.guide)
        self.pr = self.controller.config["prs"][0]
        self.pid = self.pr["id"]
        self.wt = self.root / "worktree"
        self.wt.mkdir()

    def test_state_pinned_to_adapter_identity(self):
        adapter = Mock()
        adapter.identity.return_value = {"name": "different"}
        with self.assertRaisesRegex(engine.StopWorkflow, "AGENT_CONFIG_CHANGED"):
            engine.Controller(self.repo, self.roadmap, self.state, self.guide, planner=adapter)

    def test_init_rejects_no_repo_nested_state_and_empty_guide(self):
        with self.assertRaisesRegex(engine.StopWorkflow, "REPO_INVALID"):
            engine.Controller(self.root, self.roadmap, self.root / "no-state", self.guide)
        with self.assertRaisesRegex(engine.StopWorkflow, "STATE_DIR_INSIDE_REPO"):
            engine.Controller(self.repo, self.roadmap, self.repo / "state", self.guide)
        self.guide.write_text("   ")
        with self.assertRaisesRegex(engine.StopWorkflow, "GUIDE_EMPTY"):
            engine.Controller(self.repo, self.roadmap, self.root / "other", self.guide)

    def test_state_entry_transition_stop_and_helpers(self):
        entry = self.controller.entry(self.pid)
        self.assertEqual("PLAN", entry["stage"])
        self.assertEqual(entry, self.controller.entry(self.pid))
        self.assertEqual(self.state / "worktrees" / self.pid, self.controller.wt(self.pid))
        self.assertTrue(self.controller.artifact(self.pid).exists())
        self.controller.transition(self.pid, "CODE", "ready")
        self.assertEqual("CODE", entry["stage"])
        self.assertEqual("PLAN", entry["history"][0]["from"])
        with self.assertRaisesRegex(engine.StopWorkflow, "INVALID_INTERNAL_STAGE"):
            self.controller.transition(self.pid, "NOT_A_STAGE")
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.controller.stop(self.pid, "test stop")
        self.assertIn("test stop", stderr.getvalue())
        self.assertEqual("STOPPED", entry["stage"])
        self.assertEqual("test stop", entry["stopped_reason"])

    def test_prepare_creates_new_worktree_and_reuses_known_branch(self):
        git = Mock(side_effect=["", "", "", "prflow/PR-001"])
        with patch.object(engine, "git", git):
            wt, branch = self.controller.prepare(self.pid)
        self.assertEqual("prflow/PR-001", branch)
        self.assertEqual(self.controller.wt(self.pid), wt)
        self.assertEqual("fetch", git.call_args_list[0].args[1])
        self.assertEqual("-b", git.call_args_list[2].args[3])
        git = Mock(side_effect=["", "known", "", "prflow/PR-001"])
        with patch.object(engine, "git", git):
            self.controller.prepare(self.pid)
        self.assertNotIn("-b", git.call_args_list[2].args)
        with patch.object(engine, "git", return_value="wrong-branch"):
            with self.assertRaisesRegex(engine.StopWorkflow, "WRONG_BRANCH"):
                self.controller.prepare(self.pid)

    def test_agent_phase_contracts_and_errors(self):
        with self.assertRaisesRegex(engine.StopWorkflow, "AGENT_NOT_CONFIGURED"):
            self.controller._structured(None, self.wt, self.pid, "plan", "prompt", self.guide)
        with self.assertRaisesRegex(engine.StopWorkflow, "AGENT_NOT_CONFIGURED"):
            self.controller._coding(self.wt, self.pid, "code", "prompt")
        planner = Mock()
        planner.execute.return_value = {"status": "READY", "plan_markdown": VALID_PLAN}
        self.controller.planner = planner
        with patch.object(engine, "require_clean"):
            self.controller.plan(self.pr, self.wt)
        self.assertEqual("CODE", self.controller.entry(self.pid)["stage"])
        self.assertIn("## Decision Matrix", (self.controller.artifact(self.pid) / "plan.md").read_text())
        self.assertIn("Authoring guide", planner.execute.call_args.args[0].prompt)
        self.assertEqual("plan", planner.execute.call_args.args[0].phase)
        coder = Mock()
        report = report_path(self.controller.artifact(self.pid), phase="code", fixup_number=0, attempt_id="")
        digest = persist_report(report, "Coding completed; changes verified by reviewer.")
        coder.execute.return_value = CodingResult(
            self.pid, "code", "mock", "mock-model", "COMPLETED",
            0, 0.0, 3, 0, 0, None, "", "", "", None, str(report), digest,
        )
        self.controller.coder = coder
        self.controller._coding(self.wt, self.pid, "code", "execute")
        self.assertEqual("code", coder.execute.call_args.args[0].phase)
        self.assertEqual(0, coder.execute.call_args.args[0].fixup_number)
        self.assertEqual(1, len(self.controller.entry(self.pid)["coding_results"]))
        self.assertEqual("COMPLETED", self.controller.entry(self.pid)["coding_results"][0]["status"])
        self.assertIn("FROZEN PLAN", self.controller.coding_prompt(self.pr, False))
        finding = {
            "id": "F-1", "severity": "high", "file": "src/file.py",
            "description": "Observed bug", "required_fix": "Repair under revised plan",
        }
        self.controller._start_replan(
            self.pid, source="codex_review", findings=[finding],
            reviewed_head="mock-head",
        )
        with patch.object(engine, "require_clean"), \
             patch.object(engine, "git", return_value="mock-head"):
            self.controller.replan(self.pr, self.wt)
        self.assertEqual("CODE_FIXUP", self.controller.entry(self.pid)["stage"])
        self.assertIn("fix only", self.controller.coding_prompt(self.pr, True).lower())
        self.assertIn("Observed bug", planner.execute.call_args.args[0].prompt)
        self.assertEqual("replan-1", planner.execute.call_args.args[0].phase)

    def test_verify_passes_and_logs_failing_checks(self):
        changed = {"src/file.py"}
        with patch.object(engine, "modified_paths", return_value=changed), patch.object(engine, "run", return_value=completed()):
            self.controller.verify(self.pr, self.wt)
        self.assertEqual("PUBLISH", self.controller.entry(self.pid)["stage"])
        with patch.object(engine, "modified_paths", return_value=changed), patch.object(engine, "run", return_value=completed(returncode=4, stderr="failed")):
            self.controller.verify(self.pr, self.wt)
        self.assertEqual("REPLAN", self.controller.entry(self.pid)["stage"])
        self.assertEqual(1, self.controller.entry(self.pid)["fixups"])
        failure = json.loads((self.controller.artifact(self.pid) / "feedback-1.json").read_text())
        self.assertEqual("deterministic_checks", failure["source"])
        self.assertEqual("CHECK-0", failure["findings"][0]["id"])
        with patch.object(engine, "modified_paths", return_value=set()):
            with self.assertRaisesRegex(engine.StopWorkflow, "NO_CHANGE"):
                self.controller.verify(self.pr, self.wt)
        with patch.object(engine, "modified_paths", return_value={".github/workflows/ci.yml"}):
            with self.assertRaisesRegex(engine.StopWorkflow, "SCOPE_VIOLATION"):
                self.controller.verify(self.pr, self.wt)

    def test_publish_existing_or_new_pr(self):
        info = {"number": 3, "url": "https://example.invalid/pr/3", "headRefOid": "fakesha", "baseRefName": "main"}
        git_results = ["", "src/file.py", "", "src/file.py", "", "", "fakesha"]
        with (patch.object(engine, "modified_paths", return_value={"src/file.py"}),
              patch.object(engine, "git", side_effect=git_results) as git,
              patch.object(engine, "run", return_value=completed(json.dumps(info)))):
            self.controller.publish(self.pr, self.wt, "prflow/PR-001")
        self.assertEqual("REVIEW", self.controller.entry(self.pid)["stage"])
        self.assertEqual(3, self.controller.entry(self.pid)["pr_number"])
        self.assertTrue(any("push" in call.args for call in git.call_args_list))
        self.controller.transition(self.pid, "PUBLISH")
        git_results = ["", "src/file.py", "", "src/file.py", "", "", "fakesha"]
        calls = iter([completed(returncode=1), completed(), completed(json.dumps(info))])
        with (patch.object(engine, "modified_paths", return_value={"src/file.py"}),
              patch.object(engine, "git", side_effect=git_results),
              patch.object(engine, "run", side_effect=lambda *a, **kw: next(calls))):
            self.controller.publish(self.pr, self.wt, "prflow/PR-001")
        self.assertTrue((self.controller.artifact(self.pid) / "pr-body.md").is_file())
        bad = dict(info, headRefOid="another-sha")
        git_results = ["", "src/file.py", "", "src/file.py", "", "", "fakesha"]
        with (patch.object(engine, "modified_paths", return_value={"src/file.py"}),
              patch.object(engine, "git", side_effect=git_results),
              patch.object(engine, "run", return_value=completed(json.dumps(bad)))):
            with self.assertRaisesRegex(engine.StopWorkflow, "PUBLISHED_SHA_OR_BASE_MISMATCH"):
                self.controller.publish(self.pr, self.wt, "prflow/PR-001")

    def test_review_accepts_or_requests_fixup(self):
        self.controller.entry(self.pid)["published_head"] = "abc"
        (self.controller.artifact(self.pid) / "plan.md").write_text(VALID_PLAN)
        report = report_path(
            self.controller.artifact(self.pid), phase="code",
            fixup_number=0, attempt_id="PR-001-00000001",
        )
        digest = persist_report(report, "Coding summary to verify independently.")
        self.controller.entry(self.pid)["latest_coding_attempt"] = "PR-001-00000001"
        self.controller.entry(self.pid)["coding_results"] = [
            CodingResult(
                self.pid, "code", "mock", "mock-model", "COMPLETED",
                0, 1.0, 3, 0, 0, None, "", "", "",
                "PR-001-00000001", str(report), digest,
            ).__dict__,
        ]
        with (patch.object(engine, "git", return_value="abc"), patch.object(engine, "require_clean"),
              patch.object(self.controller, "_structured", return_value={"verdict": "PASS", "findings": [], "summary": "done"})):
            self.controller.review(self.pr, self.wt)
        self.assertEqual("WAIT_MERGE", self.controller.entry(self.pid)["stage"])
        self.assertEqual("abc", self.controller.entry(self.pid)["accepted_head"])
        self.assertIn("Coding summary to verify independently.", self.controller._review_coding_report(self.pid))
        finding = {"id": "F-1", "severity": "medium", "file": "src/file.py", "description": "bug", "required_fix": "patch"}
        with (patch.object(engine, "git", return_value="abc"), patch.object(engine, "require_clean"),
              patch.object(self.controller, "_structured", return_value={"verdict": "CHANGES_REQUIRED", "findings": [finding]})):
            self.controller.review(self.pr, self.wt)
        self.assertEqual("REPLAN", self.controller.entry(self.pid)["stage"])
        self.assertEqual("codex_review", json.loads(
            (self.controller.artifact(self.pid) / "feedback-1.json").read_text()
        )["source"])
        report.write_text("tampered after successful coding", encoding="utf-8")
        with (
            patch.object(engine, "git", return_value="abc"),
            patch.object(engine, "require_clean"),
            patch.object(self.controller, "_structured") as codex,
        ):
            with self.assertRaisesRegex(ValueError, "CODING_REPORT_DIGEST_MISMATCH"):
                self.controller.review(self.pr, self.wt)
            codex.assert_not_called()
        self.assertEqual("REPLAN", self.controller.entry(self.pid)["stage"])
        self.controller.entry(self.pid)["published_head"] = "old"
        with patch.object(engine, "git", return_value="abc"), patch.object(engine, "require_clean"):
            with self.assertRaisesRegex(engine.StopWorkflow, "HEAD_CHANGED_SINCE_PUBLISH"):
                self.controller.review(self.pr, self.wt)

    def test_wait_merge_success_ci_pending_failure_and_auto(self):
        ent = self.controller.entry(self.pid)
        ent.update({"accepted_head": "sha", "pr_number": 5})
        base = {"state": "OPEN", "headRefOid": "sha", "baseRefName": "main"}
        def wait(info, checks=None, code=0):
            responses = [completed(json.dumps(info)), completed(json.dumps(checks or []), code)]
            with patch.object(engine, "run", side_effect=responses) as runner:
                return self.controller.wait_merge(self.pr, self.wt), runner
        self.assertFalse(wait(base, [{"name": "REPLACE_WITH_REAL_GITHUB_CHECK_NAME", "bucket": "pending"}])[0])
        self.assertFalse(wait(base, [{"name": "REPLACE_WITH_REAL_GITHUB_CHECK_NAME", "bucket": "pass"}])[0])
        with self.assertRaisesRegex(engine.StopWorkflow, "REQUIRED_CI_FAILED"):
            wait(base, [{"name": "REPLACE_WITH_REAL_GITHUB_CHECK_NAME", "bucket": "fail"}])
        with self.assertRaisesRegex(engine.StopWorkflow, "PR_HEAD_CHANGED_AFTER_REVIEW"):
            wait(dict(base, headRefOid="wrong"))
        with self.assertRaisesRegex(engine.StopWorkflow, "PR_BASE_CHANGED_AFTER_REVIEW"):
            wait(dict(base, baseRefName="other"))
        with self.assertRaisesRegex(engine.StopWorkflow, "PR_NOT_OPEN_OR_MERGED"):
            wait(dict(base, state="CLOSED"))
        with self.assertRaisesRegex(engine.StopWorkflow, "CI_STATUS_INVALID"):
            with patch.object(engine, "run", side_effect=[completed(json.dumps(base)), completed("invalid")]):
                self.controller.wait_merge(self.pr, self.wt)
        self.controller.config["merge_mode"] = "auto"
        checks = [{"name": "REPLACE_WITH_REAL_GITHUB_CHECK_NAME", "bucket": "pass"}]
        responses = iter([completed(json.dumps(base)), completed(json.dumps(checks)), completed()])
        with patch.object(engine, "run", side_effect=lambda *a, **kw: next(responses)) as runner:
            self.assertFalse(self.controller.wait_merge(self.pr, self.wt))
        self.assertTrue(ent["merge_requested"])
        self.assertEqual(3, runner.call_count)
        self.assertTrue(wait(dict(base, state="MERGED", mergedAt="today", mergeCommit={"oid": "merged-sha"}))[0])
        self.assertEqual("DONE", ent["stage"])

    def test_next_pr_dependency_and_completion(self):
        self.assertEqual("PR-001", self.controller.next_pr()["id"])
        self.controller.entry("PR-001")["stage"] = "DONE"
        self.assertEqual("PR-002", self.controller.next_pr()["id"])
        self.controller.entry("PR-002")["stage"] = "DONE"
        self.assertIsNone(self.controller.next_pr())
        self.controller.entry("PR-001")["stage"] = "CODE"
        self.controller.config["prs"] = [self.controller.config["prs"][1]]
        self.controller.entry("PR-002")["stage"] = "PLAN"
        with self.assertRaisesRegex(engine.StopWorkflow, "DEPENDENCY_NOT_DONE"):
            self.controller.next_pr()

    def test_dispatch_all_stages(self):
        with patch.object(self.controller, "prepare", return_value=(self.wt, "branch")):
            for stage, method in [("PLAN", "plan"), ("CODE", "_coding"), ("VERIFY", "verify"),
                                  ("PUBLISH", "publish"), ("REVIEW", "review"), ("WAIT_MERGE", "wait_merge")]:
                with self.subTest(stage=stage), patch.object(self.controller, method) as mocked:
                    self.controller.entry(self.pid)["stage"] = stage
                    if stage == "CODE":
                        (self.controller.artifact(self.pid) / "plan.md").write_text(VALID_PLAN)
                    self.controller.advance(self.pr)
                    mocked.assert_called_once()
            self.controller.entry(self.pid)["stage"] = "FIXUP"
            with self.assertRaisesRegex(engine.StopWorkflow, "LEGACY_FIXUP_REQUIRES_REPLAN"):
                self.controller.advance(self.pr)
            self.controller.entry(self.pid)["stage"] = "REPLAN"
            with patch.object(self.controller, "replan") as replanner:
                self.controller.advance(self.pr)
            replanner.assert_called_once()
            self.controller.entry(self.pid)["stage"] = "CODE_FIXUP"
            self.controller.entry(self.pid)["fixups"] = 1
            self.controller.entry(self.pid)["active_plan"] = "plan-fixup-1.md"
            (self.controller.artifact(self.pid) / "plan-fixup-1.md").write_text(VALID_PLAN)
            (self.controller.artifact(self.pid) / "feedback-1.json").write_text('{"findings":[{"id":"F1"}]}')
            with patch.object(self.controller, "_coding") as coder:
                self.controller.advance(self.pr)
            coder.assert_called_once()
            self.assertEqual("VERIFY", self.controller.entry(self.pid)["stage"])
            for stage in ("DONE", "STOPPED"):
                self.controller.entry(self.pid)["stage"] = stage
                self.assertFalse(self.controller.advance(self.pr))
            self.controller.entry(self.pid)["stage"] = "UNKNOWN"
            with self.assertRaisesRegex(engine.StopWorkflow, "UNKNOWN_STAGE"):
                self.controller.advance(self.pr)

    def test_process_success_stop_and_wait(self):
        with patch.object(self.controller, "next_pr", return_value=None):
            self.assertEqual(0, self.controller.process())
        self.controller.entry(self.pid)["stage"] = "STOPPED"
        with patch.object(self.controller, "next_pr", return_value=self.pr):
            self.assertEqual(2, self.controller.process())
        self.controller.entry(self.pid)["stage"] = "PLAN"
        with (patch.object(self.controller, "next_pr", return_value=self.pr),
              patch.object(self.controller, "advance", side_effect=engine.StopWorkflow("blocked"))):
            self.assertEqual(2, self.controller.process())
        self.assertEqual("STOPPED", self.controller.entry(self.pid)["stage"])
        self.controller.entry(self.pid)["stage"] = "WAIT_MERGE"
        with (patch.object(self.controller, "next_pr", return_value=self.pr),
              patch.object(self.controller, "advance", return_value=False)):
            self.assertEqual(3, self.controller.process(follow=False))
            with patch.object(engine.time, "sleep"), patch.object(engine.time, "monotonic", side_effect=[0.0, 600.0]):
                self.assertEqual(3, self.controller.process(follow=True, max_wait_minutes=1))


class ValidationEdges(unittest.TestCase):
    def test_roadmap_invalid_fields(self):
        src = parse_roadmap(SAMPLE.read_text())
        cases = [
            ("version", 2), ("prs", []), ("base_branch", "../bad"), ("merge_mode", "skip"),
            ("max_fixups", 0), ("command_timeout_seconds", 0), ("required_checks", [5]),
        ]
        for key, value in cases:
            with self.subTest(key=key):
                bad = dict(src, **{key: value})
                with self.assertRaises(engine.StopWorkflow):
                    engine.validate_roadmap(bad)
        cases = [
            ("id", "bad id"), ("title", ""), ("goal", ""), ("acceptance", []),
            ("allowed_paths", ["../escapes"]), ("checks", [""]), ("depends_on", ["missing"]),
        ]
        for key, value in cases:
            with self.subTest(key=key):
                bad = json.loads(json.dumps(src))
                bad["prs"][0][key] = value
                with self.assertRaises(engine.StopWorkflow):
                    engine.validate_roadmap(bad)

    def test_plan_review_and_pi_error_edges(self):
        for plan in ({}, {"status": "READY"}, {"status": "READY", "plan_markdown": "x" * 310}):
            with self.assertRaises(engine.StopWorkflow):
                engine.validate_plan(plan)
        for review in ({}, {"verdict": "PASS", "findings": [{}]},
                       {"verdict": "STOP", "findings": [], "summary": "blocked"},
                       {"verdict": "CHANGES_REQUIRED", "findings": [{}]}):
            with self.assertRaises(engine.StopWorkflow):
                engine.validate_review(review)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "events"
            p.write_text("invalid-json\n")
            with self.assertRaisesRegex(engine.StopWorkflow, "PI_INVALID_JSONL"):
                engine.check_pi_events(p)
            p.write_text('\n'.join([json.dumps({"type": "agent_settled", "aborted": False}),
                                    json.dumps({"type": "message_end", "message": {"role": "assistant", "stopReason": "error"}})]))
            with self.assertRaisesRegex(engine.StopWorkflow, "PI_INCOMPLETE"):
                engine.check_pi_events(p)

    def test_run_paths_and_require_clean(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            with patch.object(engine.subprocess, "run", return_value=completed("ok")):
                result = engine.run(["cmd"], folder, output_base=folder / "artifacts" / "check")
                self.assertEqual("ok", result.stdout)
                self.assertEqual("ok", (folder / "artifacts" / "check.stdout").read_text())
            with patch.object(engine.subprocess, "run", return_value=completed(returncode=3, stderr="oops")):
                with self.assertRaisesRegex(engine.StopWorkflow, "COMMAND_FAILED"):
                    engine.run(["cmd"], folder)
            with patch.object(engine, "git", return_value=" M src/x.py"):
                with self.assertRaisesRegex(engine.StopWorkflow, "WORKTREE_NOT_CLEAN"):
                    engine.require_clean(folder)
            with patch.object(engine, "git", return_value=""):
                engine.require_clean(folder)
            with patch.object(engine, "git", side_effect=["src/head.py", "src/worktree.py", "src/staged.py", "src/untracked.py"]):
                changed = engine.modified_paths(folder, "main")
            self.assertEqual(4, len(changed))
            with engine.single_runner(folder / "lock-folder"):
                with self.assertRaisesRegex(engine.StopWorkflow, "ANOTHER_RUNNER_ACTIVE"):
                    with engine.single_runner(folder / "lock-folder"):
                        pass


if __name__ == "__main__":
    unittest.main()
