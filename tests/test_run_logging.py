"""Tests for timestamped, rotating, independently configured log destinations."""
from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from codegrinder.config import ConfigurationError, load_config
from codegrinder.engine import Controller
from codegrinder.interfaces import CodingResult, PreflightResult
from codegrinder.run_logging import LoggingSettings, WorkflowEventLogger, safe_error_code

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "roadmap.v1.md"


class TestLogging(unittest.TestCase):
    def test_default_uses_files_and_stdout_with_identical_event(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                logs = WorkflowEventLogger(state)
                logs.emit("STEP_START", pr_id="PR-001", step="plan", state="PLAN",
                          disposition="STARTED")
                logs.emit("STEP_FINISH", pr_id="PR-001", step="plan", state="PLAN",
                          disposition="SUCCEEDED", duration_ms=14)
                logs.close()
            logfile = next((state / "logs" / "PR-001").glob("*.log"))
            self.assertIn(logs.stamp, logfile.name)
            self.assertEqual(0o600, logfile.stat().st_mode & 0o777)
            self.assertEqual(0o700, logfile.parent.stat().st_mode & 0o777)
            self.assertEqual(2, len(logfile.read_text().splitlines()))
            self.assertEqual(logfile.read_text(), captured.getvalue())
            self.assertIn("pr=PR-001 step=plan state=PLAN disposition=SUCCEEDED", captured.getvalue())
            self.assertIn("duration_ms=14", captured.getvalue())

    def test_stdout_only_does_not_create_file_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                logs = WorkflowEventLogger(
                    state, LoggingSettings(stdout=True, files=False)
                )
                logs.emit("STEP_START", pr_id="PR-55", state="CODE")
                logs.close()
            self.assertIn("pr=PR-55", output.getvalue())
            self.assertFalse((state / "logs").exists())

    def test_files_only_does_not_write_to_stdout(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                logs = WorkflowEventLogger(
                    state, LoggingSettings(stdout=False, files=True)
                )
                logs.emit("STEP_START", pr_id="PR-42", step="review", state="REVIEW")
                logs.close()
            self.assertEqual("", output.getvalue())
            log = next((state / "logs" / "PR-42").glob("*.log")).read_text()
            self.assertIn("pr=PR-42 step=review state=REVIEW", log)

    def test_custom_root_and_rotation_keeps_timestamp_in_backup_names(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "tmp-log" / "codegrinder"
            logs = WorkflowEventLogger(
                Path(temp) / "state",
                LoggingSettings(directory=root, stdout=False, files=True,
                                max_bytes=180, backup_count=2),
            )
            for _ in range(14):
                logs.emit("STEP_FINISH", pr_id="PR-101", step="code",
                          state="CODE", disposition="SUCCEEDED", duration_ms=200)
            logs.close()
            paths = sorted((root / "PR-101").iterdir())
            self.assertEqual(3, len(paths))
            self.assertTrue(any(p.name.endswith(".log.1") for p in paths))
            self.assertTrue(any(p.name.endswith(".log.2") for p in paths))
            self.assertTrue(all(logs.stamp in p.name for p in paths))
            self.assertTrue(all((p.stat().st_mode & 0o777) == 0o600 for p in paths))

    def test_run_scoped_preflight_log_is_separate_from_pr_logs(self):
        with tempfile.TemporaryDirectory() as temp:
            logs = WorkflowEventLogger(
                Path(temp), LoggingSettings(stdout=False, files=True)
            )
            logs.emit("STEP_START", step="preflight_plan",
                      state="PREFLIGHT", disposition="STARTED")
            logs.emit("STEP_FINISH", step="preflight_plan",
                      state="PREFLIGHT", disposition="SUCCEEDED")
            logs.emit("STATE_TRANSITION", pr_id="PR-1", step="PLAN",
                      state="CODE", disposition="SUCCEEDED")
            logs.close()
            self.assertEqual(1, len(list((Path(temp) / "logs" / "_run").glob("*.log"))))
            self.assertEqual(1, len(list((Path(temp) / "logs" / "PR-1").glob("*.log"))))

    def test_no_raw_exception_or_untrusted_directory_name(self):
        with tempfile.TemporaryDirectory() as temp:
            logs = WorkflowEventLogger(
                Path(temp), LoggingSettings(stdout=False, files=True)
            )
            failure = RuntimeError("AUTH_FAILED: private credential content")
            self.assertEqual("AUTH_FAILED", safe_error_code(failure))
            logs.emit("STEP_FINISH", pr_id="../untrusted", step="code",
                      state="CODE", disposition="FAILED",
                      error_code=safe_error_code(failure))
            logs.close()
            log = next((Path(temp) / "logs" / "UNSPECIFIED").glob("*.log"))
            self.assertNotIn("private credential", log.read_text())
            self.assertIn("error_code=AUTH_FAILED", log.read_text())

    def test_config_accepts_independent_outputs_and_cwd_relative_root(self):
        with tempfile.TemporaryDirectory() as temp:
            cwd = Path(temp) / "cwd"
            cwd.mkdir()
            config_parent = Path(temp) / "elsewhere"
            config_parent.mkdir()
            path = config_parent / "agents.toml"
            path.write_text(
                '[logging]\n'
                'directory = "logs"\n'
                'stdout = false\n'
                'files = true\n'
                'level = "DEBUG"\n'
                'max_bytes = 128\n'
                'backup_count = 3\n'
            )
            config = load_config(path, cwd=cwd)
            self.assertEqual((cwd / "logs").resolve(), config.logging.directory)
            self.assertFalse(config.logging.stdout)
            self.assertTrue(config.logging.files)
            self.assertEqual("DEBUG", config.logging.level)
            self.assertEqual(128, config.logging.max_bytes)
            self.assertEqual(3, config.logging.backup_count)
            defaults = load_config(cwd=cwd).logging
            self.assertTrue(defaults.stdout)
            self.assertTrue(defaults.files)

    def test_invalid_destination_configuration_rejected(self):
        cases = [
            '[logging]\nstdout = false\nfiles = false\n',
            '[logging]\nstdout = "yes"\n',
            '[logging]\nfiles = 1\n',
            '[logging]\nunknown = true\n',
            '[logging]\nlevel = "trace"\n',
            '[logging]\nmax_bytes = true\n',
            '[logging]\nbackup_count = -1\n',
        ]
        for toml in cases:
            with self.subTest(toml=toml), tempfile.TemporaryDirectory() as temp:
                Path(temp, "grind.toml").write_text(toml)
                with self.assertRaises(ConfigurationError):
                    load_config(cwd=Path(temp))

    def test_orchestrator_step_start_success_and_failure_dispositions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            guide = root / "guide.md"
            guide.write_text("Guide")
            worker = Controller(repo, SAMPLE, root / "state", guide)
            mock = Mock()
            mock.execute.return_value = {"status": "READY"}
            with contextlib.redirect_stdout(io.StringIO()):
                worker._structured(mock, repo, "PR-001", "plan", "prompt", guide)
                mock.execute.side_effect = RuntimeError("TOKEN_should_not_leak")
                with self.assertRaises(RuntimeError):
                    worker._structured(mock, repo, "PR-001", "review", "prompt", guide)
            worker.logs.close()
            log = next((root / "state" / "logs" / "PR-001").glob("*.log")).read_text()
            self.assertIn("step=plan state=PLAN disposition=STARTED", log)
            self.assertIn("step=plan state=PLAN disposition=SUCCEEDED", log)
            self.assertIn("step=review state=PLAN disposition=FAILED", log)
            self.assertNotIn("TOKEN_should_not_leak", log)

    def test_orchestrator_coding_result_and_state_logged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            guide = root / "guide.md"
            guide.write_text("Guide")
            coder = Mock()
            coder.identity.return_value = {"adapter": "fake"}
            coder.execute.return_value = CodingResult(
                "PR-001", "code", "fake", "mock-model", "COMPLETED",
                0, 0.1, 3, 0, 0, None, "", "", "",
            )
            worker = Controller(repo, SAMPLE, root / "state", guide, coder=coder)
            with contextlib.redirect_stdout(io.StringIO()):
                worker.transition("PR-001", "CODE")
                worker._coding(repo, "PR-001", "code", "prompt")
            worker.logs.close()
            log = next((root / "state" / "logs" / "PR-001").glob("*.log")).read_text()
            self.assertIn("event=STATE_TRANSITION", log)
            self.assertIn("step=code state=CODE disposition=STARTED", log)
            self.assertIn("step=code state=CODE disposition=SUCCEEDED", log)

    def test_preflight_logs_lifecycle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            guide = root / "guide.md"
            guide.write_text("Guide")
            adapters = []
            for label in ("plan", "code", "review"):
                adapter = Mock()
                adapter.identity.return_value = {"adapter": label, "model": "fake"}
                adapter.preflight.return_value = PreflightResult.passed()
                adapters.append(adapter)
            worker = Controller(repo, SAMPLE, root / "state", guide,
                                planner=adapters[0], coder=adapters[1], reviewer=adapters[2])
            with contextlib.redirect_stdout(io.StringIO()):
                worker.preflight()
            worker.logs.close()
            log = next((root / "state" / "logs" / "_run").glob("*.log")).read_text()
            for phase in ("plan", "code", "review"):
                self.assertIn(f"step=preflight_{phase} state=PREFLIGHT disposition=STARTED", log)
                self.assertIn(f"step=preflight_{phase} state=PREFLIGHT disposition=SUCCEEDED", log)


if __name__ == "__main__":
    unittest.main()
