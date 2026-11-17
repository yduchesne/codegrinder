import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codegrinder import cli
from codegrinder.engine import Controller, StopWorkflow


SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "roadmap.v1.md"


class TestCli(unittest.TestCase):
    def test_root_help_lists_run(self):
        with self.assertRaises(SystemExit) as result, contextlib.redirect_stdout(io.StringIO()) as out:
            cli.parser().parse_args(["--help"])
        self.assertEqual(0, result.exception.code)
        self.assertIn("run", out.getvalue())
        self.assertIn("grind", out.getvalue())

    def test_run_help_documents_both_path_flags(self):
        with self.assertRaises(SystemExit) as result, contextlib.redirect_stdout(io.StringIO()) as out:
            cli.parser().parse_args(["run", "--help"])
        self.assertEqual(0, result.exception.code)
        for option in ("--guide", "-g", "--roadmap", "-r", "--conf"):
            self.assertIn(option, out.getvalue())

    def test_root_help_includes_config(self):
        with self.assertRaises(SystemExit) as result, contextlib.redirect_stdout(io.StringIO()) as out:
            cli.parser().parse_args(["--help"])
        self.assertEqual(0, result.exception.code)
        self.assertIn("--conf", out.getvalue())

    def test_conf_before_or_after_run(self):
        for args in (["--conf", "example.toml", "run", "-g", "a", "-r", "b"],
                     ["run", "--conf", "example.toml", "-g", "a", "-r", "b"]):
            parsed = cli.parser().parse_args(args)
            self.assertEqual(Path("example.toml"), parsed.conf or parsed.run_conf)

    def test_required_args(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                cli.parser().parse_args(["run", "-r", "roadmap.md"])
        self.assertEqual(2, caught.exception.code)

    def test_unknown_subcommand(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.parser().parse_args(["status"])

    def test_run_invokes_full_roadmap(self):
        with tempfile.TemporaryDirectory() as tmp:
            guide = Path(tmp, "guide.md")
            guide.write_text("# Design guide\nPredefine each step.\n")
            with (patch.object(cli, "_repo_root", return_value=Path(tmp)),
                  patch.object(cli, "state_directory", return_value=Path(tmp) / "state"),
                  patch.object(cli, "Controller") as controller):
                controller.return_value.process.return_value = 0
                result = cli.main(["run", "-g", str(guide), "-r", str(SAMPLE)])
                self.assertEqual(0, result)
                controller.return_value.process.assert_called_once_with(follow=True, max_wait_minutes=None)

    def test_config_change_prohibits_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp, "repo")
            repo.mkdir()
            (repo / ".git").mkdir()
            guide = Path(tmp, "guide.md")
            guide.write_text("guide")
            state = Path(tmp, "state")
            Controller(repo, SAMPLE, state, guide, configuration_sha256="hash-v1")
            with self.assertRaisesRegex(StopWorkflow, "CONFIG_CHANGED"):
                Controller(repo, SAMPLE, state, guide, configuration_sha256="hash-v2")

    def test_guide_change_prohibits_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp, "repo")
            repo.mkdir()
            (repo / ".git").mkdir()
            guide = Path(tmp, "guide.md")
            guide.write_text("Authoring guide v1")
            state = Path(tmp, "state")
            Controller(repo, SAMPLE, state, guide)
            guide.write_text("Authoring guide v2")
            with self.assertRaisesRegex(StopWorkflow, "GUIDE_CHANGED"):
                Controller(repo, SAMPLE, state, guide)

    def test_roadmap_change_prohibits_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp, "repo")
            repo.mkdir()
            (repo / ".git").mkdir()
            guide = Path(tmp, "guide.md")
            guide.write_text("guide")
            roadmap = Path(tmp, "roadmap.md")
            roadmap.write_text(SAMPLE.read_text())
            state = Path(tmp, "state")
            Controller(repo, roadmap, state, guide)
            roadmap.write_text(SAMPLE.read_text() + "\n")
            with self.assertRaisesRegex(StopWorkflow, "ROADMAP_CHANGED"):
                Controller(repo, roadmap, state, guide)


if __name__ == "__main__":
    unittest.main()
