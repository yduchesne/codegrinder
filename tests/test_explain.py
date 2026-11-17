"""Read-only explain diagnostics, including persisted state path and PR checkpoints."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codegrinder import cli

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "roadmap.v1.md"


class TestExplain(unittest.TestCase):
    def setUp(self):
        scope = tempfile.TemporaryDirectory()
        self.addCleanup(scope.cleanup)
        self.root = Path(scope.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.roadmap = self.repo / "roadmap.md"
        self.roadmap.write_text(SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        self.guide = self.repo / "guide.md"
        self.guide.write_text("Detailed authoring guide version 1", encoding="utf-8")
        self.conf = self.repo / "grind.toml"
        self.conf.write_text(
            '[plan.roadmap]\npath = "roadmap.md"\n'
            '[plan.guide]\npath = "guide.md"\n'
            '[steps.plan]\nmodel = "custom-plan-model"\nreasoning_effort = "medium"\n'
            '[steps.code]\nprovider = "another-provider"\nmodel = "model-123"\nthinking = "high"\n'
            '[logging]\nstdout = false\nfiles = true\n',
            encoding="utf-8",
        )
        self.state = self.root / "state"
        self.previous_cwd = Path.cwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, self.previous_cwd)

    def saved(self, entries: dict, *, changes: dict | None = None):
        digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
        payload = {
            "roadmap_sha256": digest(self.roadmap),
            "guide_sha256": digest(self.guide),
            "configuration_sha256": digest(self.conf),
            "prs": entries,
        }
        if changes:
            payload.update(changes)
        self.state.mkdir(exist_ok=True)
        (self.state / "state.json").write_text(json.dumps(payload), encoding="utf-8")

    def run_explain(self, *args, has_repo=True):
        output = io.StringIO()
        with patch.object(cli, "_repo_root", return_value=self.repo if has_repo else None), \
             patch.object(cli, "state_directory", return_value=self.state), \
             patch.object(cli, "DefaultOrchestrator") as orchestrator, \
             patch.object(cli, "single_runner") as lock, \
             contextlib.redirect_stdout(output):
            self.assertEqual(0, cli.main(["explain", *args]))
        orchestrator.assert_not_called()
        lock.assert_not_called()
        return output.getvalue()

    def test_root_and_explain_help(self):
        for tokens in (["--help"], ["explain", "--help"]):
            with self.subTest(tokens=tokens), \
                 self.assertRaises(SystemExit) as raised, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                cli.parser().parse_args(tokens)
            self.assertEqual(0, raised.exception.code)
            self.assertIn("explain", output.getvalue())

    def test_explain_config_and_missing_state_file_location(self):
        data = self.run_explain()
        self.assertIn("model=custom-plan-model thinking=medium", data)
        self.assertIn("provider=another-provider model=model-123 thinking=high", data)
        self.assertIn("review: adapter=codex", data)
        self.assertIn(f"Roadmap: {self.roadmap}", data)
        self.assertIn(f"Authoring guide: {self.guide}", data)
        self.assertIn(f"State directory: {self.state}", data)
        self.assertIn(f"State file: {self.state / 'state.json'}", data)
        self.assertIn("State file status: NOT_STARTED", data)
        self.assertIn("Next PR: PR-001 (offline candidate", data)
        self.assertFalse(self.state.exists(), "explain must not create a state directory")

    def test_saved_checkpoint_and_disposition_on_interrupted_review(self):
        self.saved({
            "PR-001": {"stage": "DONE", "merged_at": "2026-11-17T12:00:00Z",
                       "merge_commit": "confirmed-historical-sha"},
            "PR-002": {"stage": "REVIEW", "fixups": 1, "history": [
                {"at": "2026-11-17T12:01:00+00:00", "from": "PUBLISH",
                 "to": "REVIEW", "reason": "published"},
            ]},
        })
        before = (self.state / "state.json").read_bytes()
        data = self.run_explain()
        self.assertIn("Last PR: PR-002", data)
        self.assertIn("Last step: REVIEW", data)
        self.assertIn("Last stage: REVIEW", data)
        self.assertIn("Last disposition: INCOMPLETE", data)
        self.assertIn("Next PR: PR-002 (offline candidate", data)
        self.assertIn(f"State file: {self.state / 'state.json'}", data)
        self.assertIn("State file status: PRESENT", data)
        self.assertEqual(before, (self.state / "state.json").read_bytes())

    def test_stop_and_failed_step(self):
        self.saved({"PR-001": {"stage": "STOPPED", "stopped_reason": "ERROR_CODE",
                  "history": [{"at": "2026-11-17T00:01:00+00:00",
                               "from": "REPLAN", "to": "STOPPED"}]}})
        data = self.run_explain()
        self.assertIn("Last PR: PR-001", data)
        self.assertIn("Last step: REPLAN", data)
        self.assertIn("Last stage: STOPPED", data)
        self.assertIn("Last disposition: FAILED / STOPPED", data)
        self.assertIn("Next PR: PR-001 (STOPPED;", data)

    def test_unconfigured_roadmap_and_guide_render_na(self):
        conf = self.repo / "other.toml"
        conf.write_text('[steps.plan]\nmodel="custom-plan-model"\n')
        output = io.StringIO()
        with patch.object(cli, "_repo_root", return_value=self.repo), \
             patch.object(cli, "DefaultOrchestrator") as worker, \
             contextlib.redirect_stdout(output):
            self.assertEqual(0, cli.main(["--conf", str(conf), "explain"]))
        worker.assert_not_called()
        data = output.getvalue()
        self.assertIn("Roadmap: N/A", data)
        self.assertIn("Authoring guide: N/A", data)
        self.assertIn("State file: N/A", data)
        self.assertIn("Next PR: N/A (roadmap not configured)", data)

    def test_relocated_config_and_cli_override_paths(self):
        config = self.root / "custom.toml"
        config.write_text('[plan.roadmap]\npath="missing-roadmap.md"\n[plan.guide]\npath="missing-guide.md"\n')
        out = io.StringIO()
        with patch.object(cli, "_repo_root", return_value=self.repo), \
             patch.object(cli, "state_directory", return_value=self.state), \
             contextlib.redirect_stdout(out):
            self.assertEqual(0, cli.main([
                "explain", "--conf", str(config), "-r", str(self.roadmap),
                "-g", str(self.guide),
            ]))
        data = out.getvalue()
        self.assertIn(f"Roadmap: {self.roadmap} (present)", data)
        self.assertIn(f"Authoring guide: {self.guide} (present)", data)

    def test_conflicts_and_unverified_done_are_not_claimed_completed(self):
        self.saved({"PR-001": {"stage": "DONE"}})
        data = self.run_explain()
        self.assertIn("Last disposition: UNVERIFIED", data)
        self.assertIn("Next PR: N/A (saved completion for PR-001 lacks merge evidence)", data)
        self.saved({"PR-001": {"stage": "DONE", "merged_at": "now",
                                "merge_commit": "sha"}},
                   changes={"roadmap_sha256": "stale"})
        self.assertIn("Input consistency: CHANGED (roadmap)", self.run_explain())
        self.assertIn("Next PR: N/A (state reconciliation required)", self.run_explain())

    def test_invalid_state_is_reported_not_overwritten(self):
        self.state.mkdir(exist_ok=True)
        path = self.state / "state.json"
        path.write_text("{bad-json")
        before = path.read_bytes()
        output = self.run_explain()
        self.assertIn("State file status: INVALID", output)
        self.assertIn("Next PR: N/A (invalid saved state)", output)
        self.assertEqual(before, path.read_bytes())

    def test_unknown_pr_and_invalid_roadmap_are_safe(self):
        self.saved({"OTHER": {"stage": "REVIEW"}})
        self.assertIn("unknown saved PR identifiers", self.run_explain())
        self.roadmap.write_text("This is not a roadmap")
        output = self.run_explain()
        self.assertIn("Roadmap parsing: INVALID", output)
        self.assertIn("Next PR: N/A (invalid roadmap)", output)

    def test_git_checkout_unavailable_keeps_diagnostics_operational(self):
        with patch.object(cli, "_repo_root", side_effect=cli.StopWorkflow("REPO_INVALID")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, cli.main(["explain"]))
        data = output.getvalue()
        self.assertIn("Repository: N/A", data)
        self.assertIn("State file: N/A", data)
        self.assertIn("Next PR: N/A (not a Git checkout)", data)


if __name__ == "__main__":
    unittest.main()
