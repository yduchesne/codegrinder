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

    def test_guide_and_roadmap_are_optional_parser_arguments(self):
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

    def test_config_only_execution_uses_both_toml_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_dir = root / "configuration"
            cfg_dir.mkdir()
            guide = root / "guide.md"
            guide.write_text("Authoring guide")
            roadmap = root / "roadmap.md"
            roadmap.write_text(SAMPLE.read_text())
            config = cfg_dir / "custom.toml"
            config.write_text('[plan.guide]\npath = "guide.md"\n[plan.roadmap]\npath = "roadmap.md"\n')
            with (patch.object(cli, "_repo_root", return_value=root),
                  patch.object(cli, "state_directory", return_value=root / "state"),
                  patch.object(cli, "Controller") as controller):
                controller.return_value.process.return_value = 0
                original_cwd = Path.cwd()
                try:
                    os.chdir(root)
                    result = cli.main(["--conf", str(config), "run"])
                finally:
                    os.chdir(original_cwd)
                self.assertEqual(0, result)
                self.assertEqual(roadmap.resolve(), controller.call_args.args[1])
                self.assertEqual(guide.resolve(), controller.call_args.args[3])

    def test_each_cli_path_overrides_only_corresponding_toml_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_dir = root / "config"
            cfg_dir.mkdir()
            guide = root / "default-guide.md"
            guide.write_text("Default guide")
            roadmap = root / "default-roadmap.md"
            roadmap.write_text(SAMPLE.read_text())
            override_guide = root / "override-guide.md"
            override_guide.write_text("Override guide")
            override_roadmap = root / "override-roadmap.md"
            override_roadmap.write_text(SAMPLE.read_text())
            config = cfg_dir / "custom.toml"
            config.write_text('[plan.guide]\npath="default-guide.md"\n[plan.roadmap]\npath="default-roadmap.md"\n')
            cases = [
                (["-g", str(override_guide)], roadmap.resolve(), override_guide.resolve()),
                (["-r", str(override_roadmap)], override_roadmap.resolve(), guide.resolve()),
                (["-g", str(override_guide), "-r", str(override_roadmap)],
                 override_roadmap.resolve(), override_guide.resolve()),
            ]
            for flags, expected_roadmap, expected_guide in cases:
                with self.subTest(flags=flags), patch.object(cli, "_repo_root", return_value=root), \
                     patch.object(cli, "state_directory", return_value=root / "state"), \
                     patch.object(cli, "Controller") as controller:
                    controller.return_value.process.return_value = 0
                    original_cwd = Path.cwd()
                    try:
                        os.chdir(root)
                        self.assertEqual(0, cli.main(["run", "--conf", str(config), *flags]))
                    finally:
                        os.chdir(original_cwd)
                    self.assertEqual(expected_roadmap, controller.call_args.args[1])
                    self.assertEqual(expected_guide, controller.call_args.args[3])

    def test_relative_cli_flags_use_cwd_with_config_elsewhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conf_dir = root / "elsewhere"
            conf_dir.mkdir()
            config = conf_dir / "settings.toml"
            config.write_text('[plan.guide]\npath="missing-guide.md"\n[plan.roadmap]\npath="missing-roadmap.md"\n')
            guide = root / "guide.md"
            roadmap = root / "roadmap.md"
            guide.write_text("Guide")
            roadmap.write_text(SAMPLE.read_text())
            previous = Path.cwd()
            try:
                os.chdir(root)
                with (patch.object(cli, "_repo_root", return_value=root),
                      patch.object(cli, "state_directory", return_value=root / "state"),
                      patch.object(cli, "Controller") as controller):
                    controller.return_value.process.return_value = 0
                    self.assertEqual(0, cli.main(["--conf", str(config), "run", "-g", "guide.md", "-r", "roadmap.md"]))
                    self.assertEqual(guide.resolve(), controller.call_args.args[3])
                    self.assertEqual(roadmap.resolve(), controller.call_args.args[1])
            finally:
                os.chdir(previous)

    def test_missing_unconfigured_guide_or_roadmap_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(Path(tmp) / "state")}), \
                 patch.object(cli, "_repo_root") as repo_root, \
                 patch.object(cli, "load_config", wraps=cli.load_config) as load:
                cwd = os.getcwd()
                try:
                    os.chdir(tmp)
                    with contextlib.redirect_stderr(io.StringIO()) as err:
                        result = cli.main(["run"])
                    self.assertEqual(2, result)
                    self.assertIn("GUIDE_UNSPECIFIED", err.getvalue())
                    repo_root.assert_not_called()
                    load.assert_called_once()
                finally:
                    os.chdir(cwd)

    def test_cli_override_ignores_nonexistent_toml_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guide = root / "real-guide.md"
            guide.write_text("Guide")
            roadmap = root / "real-roadmap.md"
            roadmap.write_text(SAMPLE.read_text())
            config = root / "grind.toml"
            config.write_text('[plan.guide]\npath="missing.md"\n[plan.roadmap]\npath="missing-roadmap.md"\n')
            with (patch.object(cli, "_repo_root", return_value=root),
                  patch.object(cli, "state_directory", return_value=root / "state"),
                  patch.object(cli, "Controller") as controller):
                controller.return_value.process.return_value = 0
                self.assertEqual(0, cli.main(["run", "--conf", str(config), "-g", str(guide), "-r", str(roadmap)]))

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
