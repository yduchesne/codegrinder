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

    def test_guide_and_roadmap_flags_optional(self):
        args = cli.parser().parse_args(["run"])
        self.assertIsNone(args.guide)
        self.assertIsNone(args.roadmap)
        args = cli.parser().parse_args(["run", "-r", "roadmap.md"])
        self.assertEqual(Path("roadmap.md"), args.roadmap)
        self.assertIsNone(args.guide)

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


    def test_toml_paths_use_cwd_even_when_config_elsewhere(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "configuration").mkdir()
            guide = root / "guide.md"
            roadmap = root / "roadmap.md"
            guide.write_text("Authoring guide")
            roadmap.write_text(SAMPLE.read_text())
            config = root / "configuration" / "settings.toml"
            config.write_text('[plan.guide]\npath="guide.md"\n[plan.roadmap]\npath="roadmap.md"\n')
            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                with (patch.object(cli, "_repo_root", return_value=root),
                      patch.object(cli, "state_directory", return_value=root / "state"),
                      patch.object(cli, "Controller") as controller):
                    controller.return_value.process.return_value = 0
                    self.assertEqual(0, cli.main(["--conf", str(config), "run"]))
                    self.assertEqual(roadmap.resolve(), controller.call_args.args[1])
                    self.assertEqual(guide.resolve(), controller.call_args.args[3])
            finally:
                os.chdir(old_cwd)

    def test_each_flag_overrides_only_matching_toml_entry(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            config_dir = root / "configuration"
            config_dir.mkdir()
            guide = root / "guide.md"
            roadmap = root / "roadmap.md"
            other_guide = root / "other-guide.md"
            other_roadmap = root / "other-roadmap.md"
            guide.write_text("Authoring guide")
            roadmap.write_text(SAMPLE.read_text())
            other_guide.write_text("Alternate guide")
            other_roadmap.write_text(SAMPLE.read_text())
            config = config_dir / "settings.toml"
            config.write_text('[plan.guide]\npath="guide.md"\n[plan.roadmap]\npath="roadmap.md"\n')
            cases = [
                (["-g", "other-guide.md"], roadmap, other_guide),
                (["-r", "other-roadmap.md"], other_roadmap, guide),
                (["-g", str(other_guide), "-r", str(other_roadmap)], other_roadmap, other_guide),
            ]
            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                for opts, expected_roadmap, expected_guide in cases:
                    with self.subTest(opts=opts), (
                        patch.object(cli, "_repo_root", return_value=root),
                        patch.object(cli, "state_directory", return_value=root / "state"),
                        patch.object(cli, "Controller") as controller,
                    ):
                        controller.return_value.process.return_value = 0
                        self.assertEqual(0, cli.main(["run", "--conf", str(config), *opts]))
                        self.assertEqual(expected_roadmap.resolve(), controller.call_args.args[1])
                        self.assertEqual(expected_guide.resolve(), controller.call_args.args[3])
            finally:
                os.chdir(old_cwd)

    def test_missing_unconfigured_paths_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            old_cwd = Path.cwd()
            try:
                os.chdir(d)
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(2, cli.main(["run"]))
                self.assertIn("GUIDE_UNSPECIFIED", err.getvalue())
            finally:
                os.chdir(old_cwd)

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
