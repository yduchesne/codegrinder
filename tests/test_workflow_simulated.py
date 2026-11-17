"""End-to-end controller contract tests with fake agents and Git/GitHub boundaries.

These use the real roadmap/state machine; only process and external Git/GitHub
interactions are mocked. No real Codex, Pi, git push, or network is used.
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codegrinder import engine
from codegrinder.interfaces import CodingTask, StructuredTask, WorkflowContext

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "roadmap.v1.md"
VALID_PLAN = (
    "## Scope\n" + "Prepared implementation plan. " * 18
    + "\n## Steps\n## Tests\n## Decision Matrix\n## Stop Conditions"
)
FINDING = {
    "id": "F-1", "severity": "high", "file": "src/file.py",
    "description": "Simulated defect", "required_fix": "Fix the simulated defect",
}


class FakePlanner:
    def __init__(self):
        self.calls: list[StructuredTask] = []

    def identity(self) -> dict[str, str]:
        return {"adapter": "fake-planner"}

    def execute(self, task: StructuredTask) -> dict:
        self.calls.append(task)
        return {"status": "READY", "plan_markdown": VALID_PLAN}


class FakeCoder:
    def __init__(self):
        self.calls: list[CodingTask] = []

    def identity(self) -> dict[str, str]:
        return {"adapter": "fake-coder"}

    def execute(self, task: CodingTask) -> None:
        self.calls.append(task)
        path = task.worktree / "src" / "file.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {task.pr_id}, {task.phase}, fixup {task.fixup_number}\n")


class FakeReviewer:
    def __init__(self, first_review_requires_changes: bool = True, failures_for_first_pr: int = 1):
        self.calls: list[StructuredTask] = []
        self.first_review_requires_changes = first_review_requires_changes
        self.failures_for_first_pr = failures_for_first_pr

    def identity(self) -> dict[str, str]:
        return {"adapter": "fake-reviewer"}

    def execute(self, task: StructuredTask) -> dict:
        self.calls.append(task)
        if (self.first_review_requires_changes and task.pr_id == "PR-001"
                and task.phase.startswith("review-")
                and int(task.phase.removeprefix("review-")) < self.failures_for_first_pr):
            return {"verdict": "CHANGES_REQUIRED", "findings": [FINDING],
                    "summary": "Review found a simulated defect"}
        return {"verdict": "PASS", "findings": [], "summary": "Accepted by fake reviewer"}


def completed(args, output="", returncode=0):
    return subprocess.CompletedProcess(args, returncode, output, "")


class TestSimulatedRoadmap(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.roadmap = root / "roadmap.md"
        self.roadmap.write_text(SAMPLE.read_text())
        self.guide = root / "authoring-guide.md"
        self.guide.write_text("Guide for the fake planner")
        self.state = root / "state"
        self.planner = FakePlanner()
        self.coder = FakeCoder()
        self.reviewer = FakeReviewer()
        self.controller = engine.DefaultOrchestrator(
            WorkflowContext(self.repo, self.roadmap, self.state, self.guide),
            planner=self.planner, coder=self.coder, reviewer=self.reviewer,
        )
        self.sha_versions = {"PR-001": 0, "PR-002": 0}
        self.repos_by_number = {1: "PR-001", 2: "PR-002"}
        self.merged = {"PR-001": True, "PR-002": True}
        self.published: list[str] = []
        self.operations: list[tuple[str, str]] = []

    def prepare(self, pid: str):
        wt = self.state / "worktrees" / pid
        wt.mkdir(parents=True, exist_ok=True)
        return wt, f"prflow/{pid}"

    def mock_git(self, cwd, *args, **kwargs):
        pid = Path(cwd).name
        if args[:3] == ("diff", "--cached", "--name-only"):
            return "src/file.py"
        if args[:2] == ("diff", "--name-only"):
            return "src/file.py"
        if args[:2] == ("rev-parse", "HEAD"):
            return f"{pid}-sha-{self.sha_versions[pid]}"
        if args[:2] == ("commit", "-m"):
            self.sha_versions[pid] += 1
            self.operations.append(("commit", pid))
        if args[:2] == ("push", "--set-upstream"):
            self.published.append(pid)
        return ""

    def mock_run(self, command, cwd, **kwargs):
        if command[:2] == ["bash", "-lc"]:
            self.operations.append(("verify", Path(cwd).name))
            return completed(command)
        if command[:3] == ["gh", "pr", "view"]:
            target = command[3]
            if target.startswith("prflow/"):
                pid = target.removeprefix("prflow/")
                number = 1 if pid == "PR-001" else 2
                return completed(command, json.dumps({
                    "number": number, "url": f"https://example.invalid/pull/{number}",
                    "headRefOid": f"{pid}-sha-{self.sha_versions[pid]}",
                    "baseRefName": "main",
                }))
            pid = self.repos_by_number[int(target)]
            return completed(command, json.dumps({
                "state": "MERGED" if self.merged[pid] else "OPEN",
                "mergedAt": "2026-11-16T20:00:00Z" if self.merged[pid] else None,
                "headRefOid": f"{pid}-sha-{self.sha_versions[pid]}",
                "baseRefName": "main", "mergeCommit": {"oid": f"merged-{pid}"},
            }))
        if command[:3] == ["gh", "pr", "checks"]:
            return completed(command, json.dumps([
                {"name": "REPLACE_WITH_REAL_GITHUB_CHECK_NAME", "bucket": "pass"}
            ]))
        raise AssertionError(f"Unexpected subprocess call: {command!r}")

    def test_complete_two_pr_roadmap_with_review_fixup(self):
        with (
            patch.object(self.controller, "prepare", side_effect=self.prepare),
            patch.object(engine, "git", side_effect=self.mock_git),
            patch.object(engine, "modified_paths", return_value={"src/file.py"}),
            patch.object(engine, "require_clean"),
            patch.object(engine, "run", side_effect=self.mock_run),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = self.controller.process(follow=False)
        self.assertEqual(0, result)
        self.assertEqual(
            [("PR-001", "plan"), ("PR-001", "replan-1"), ("PR-002", "plan")],
            [(t.pr_id, t.phase) for t in self.planner.calls],
        )
        self.assertIn("Simulated defect", self.planner.calls[1].prompt)
        self.assertIn("PREVIOUS PLAN", self.planner.calls[1].prompt)
        self.assertTrue((self.controller.artifact("PR-001") / "plan-fixup-1.md").exists())
        self.assertEqual(
            [("PR-001", "code", 0), ("PR-001", "fixup", 1), ("PR-002", "code", 0)],
            [(t.pr_id, t.phase, t.fixup_number) for t in self.coder.calls],
        )
        self.assertEqual(
            [("PR-001", "review-0"), ("PR-001", "review-1"), ("PR-002", "review-0")],
            [(t.pr_id, t.phase) for t in self.reviewer.calls],
        )
        self.assertEqual(["PR-001", "PR-001", "PR-002"], self.published)
        self.assertEqual("DONE", self.controller.entry("PR-001")["stage"])
        self.assertEqual("DONE", self.controller.entry("PR-002")["stage"])
        self.assertEqual(1, self.controller.entry("PR-001")["fixups"])
        self.assertIsNone(self.controller.next_pr())
        transitions = [item["to"] for item in self.controller.entry("PR-001")["history"]]
        self.assertIn("REPLAN", transitions)
        self.assertIn("CODE_FIXUP", transitions)
        self.assertLess(transitions.index("REPLAN"), transitions.index("CODE_FIXUP"))
        self.assertEqual("DONE", transitions[-1])
        persisted = json.loads((self.state / "state.json").read_text())
        self.assertEqual("DONE", persisted["prs"]["PR-002"]["stage"])

    def test_two_review_rejections_force_two_replans_before_next_pr(self):
        self.reviewer.failures_for_first_pr = 2
        with (
            patch.object(self.controller, "prepare", side_effect=self.prepare),
            patch.object(engine, "git", side_effect=self.mock_git),
            patch.object(engine, "modified_paths", return_value={"src/file.py"}),
            patch.object(engine, "require_clean"),
            patch.object(engine, "run", side_effect=self.mock_run),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(0, self.controller.process(follow=False))
        self.assertEqual(
            [("PR-001", "plan"), ("PR-001", "replan-1"),
             ("PR-001", "replan-2"), ("PR-002", "plan")],
            [(task.pr_id, task.phase) for task in self.planner.calls],
        )
        self.assertEqual(
            [("PR-001", "code"), ("PR-001", "fixup"),
             ("PR-001", "fixup"), ("PR-002", "code")],
            [(task.pr_id, task.phase) for task in self.coder.calls],
        )
        self.assertEqual(
            [("PR-001", "review-0"), ("PR-001", "review-1"),
             ("PR-001", "review-2"), ("PR-002", "review-0")],
            [(task.pr_id, task.phase) for task in self.reviewer.calls],
        )
        entry = self.controller.entry("PR-001")
        self.assertEqual(2, entry["fixups"])
        for number in (1, 2):
            artifacts = self.controller.artifact("PR-001")
            self.assertTrue((artifacts / f"plan-fixup-{number}.md").is_file())
            feedback = json.loads((artifacts / f"feedback-{number}.json").read_text())
            self.assertEqual("codex_review", feedback["source"])
            self.assertIn("Simulated defect", self.planner.calls[number].prompt)
        self.assertEqual("DONE", self.controller.entry("PR-002")["stage"])
        stages = [item["to"] for item in entry["history"]]
        self.assertEqual(2, stages.count("REPLAN"))
        self.assertEqual(2, stages.count("CODE_FIXUP"))

    def test_repeated_review_rejection_stops_before_next_pr(self):
        self.reviewer.failures_for_first_pr = 3
        with (
            patch.object(self.controller, "prepare", side_effect=self.prepare),
            patch.object(engine, "git", side_effect=self.mock_git),
            patch.object(engine, "modified_paths", return_value={"src/file.py"}),
            patch.object(engine, "require_clean"),
            patch.object(engine, "run", side_effect=self.mock_run),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(2, self.controller.process(follow=False))
        self.assertEqual("STOPPED", self.controller.entry("PR-001")["stage"])
        self.assertIn("MAX_FIXUPS_REACHED", self.controller.entry("PR-001")["stopped_reason"])
        self.assertEqual(
            ["plan", "replan-1", "replan-2"],
            [task.phase for task in self.planner.calls],
        )
        self.assertEqual([], [task for task in self.coder.calls if task.pr_id == "PR-002"])

    def test_replanning_blocked_stops_without_another_coding_invocation(self):
        def planner_with_blocked_revision(task: StructuredTask) -> dict:
            self.planner.calls.append(task)
            if task.phase == "replan-1":
                return {"status": "BLOCKED", "blocker": "review observations conflict"}
            return {"status": "READY", "plan_markdown": VALID_PLAN}

        with (
            patch.object(self.planner, "execute", side_effect=planner_with_blocked_revision),
            patch.object(self.controller, "prepare", side_effect=self.prepare),
            patch.object(engine, "git", side_effect=self.mock_git),
            patch.object(engine, "modified_paths", return_value={"src/file.py"}),
            patch.object(engine, "require_clean"),
            patch.object(engine, "run", side_effect=self.mock_run),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(2, self.controller.process(follow=False))
        self.assertEqual("STOPPED", self.controller.entry("PR-001")["stage"])
        self.assertEqual(
            [("PR-001", "code")],
            [(task.pr_id, task.phase) for task in self.coder.calls],
        )
        self.assertNotIn("PR-002", [task.pr_id for task in self.planner.calls])

    def test_resume_wait_for_merge_without_rerunning_agents(self):
        self.reviewer.first_review_requires_changes = False
        self.merged = {"PR-001": False, "PR-002": False}
        with (
            patch.object(self.controller, "prepare", side_effect=self.prepare),
            patch.object(engine, "git", side_effect=self.mock_git),
            patch.object(engine, "modified_paths", return_value={"src/file.py"}),
            patch.object(engine, "require_clean"),
            patch.object(engine, "run", side_effect=self.mock_run),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(3, self.controller.process(follow=False))
            self.assertEqual("WAIT_MERGE", self.controller.entry("PR-001")["stage"])
            self.assertEqual(1, len(self.planner.calls))
            self.assertEqual(1, len(self.coder.calls))
            self.assertEqual(1, len(self.reviewer.calls))
            self.merged["PR-001"] = True
            resumed = engine.DefaultOrchestrator(
                WorkflowContext(self.repo, self.roadmap, self.state, self.guide),
                planner=self.planner, coder=self.coder, reviewer=self.reviewer,
            )
            with patch.object(resumed, "prepare", side_effect=self.prepare):
                self.assertEqual(3, resumed.process(follow=False))
            self.assertEqual("DONE", resumed.entry("PR-001")["stage"])
            self.assertEqual("WAIT_MERGE", resumed.entry("PR-002")["stage"])
            self.assertEqual(2, len(self.planner.calls))
            self.assertEqual(2, len(self.coder.calls))
            self.assertEqual(2, len(self.reviewer.calls))

    def test_blocked_planner_stops_before_coding(self):
        def blocked(task):
            self.planner.calls.append(task)
            return {"status": "BLOCKED", "blocker": "missing acceptance contract"}

        with (
            patch.object(self.planner, "execute", side_effect=blocked),
            patch.object(self.controller, "prepare", side_effect=self.prepare),
            patch.object(engine, "require_clean"),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(2, self.controller.process(follow=False))
        self.assertEqual("STOPPED", self.controller.entry("PR-001")["stage"])
        self.assertEqual([], self.coder.calls)
        self.assertEqual([], self.reviewer.calls)


if __name__ == "__main__":
    unittest.main()
