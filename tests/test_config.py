"""Configuration discovery, strict validation, and adapter mapping."""
import tempfile
import unittest
from pathlib import Path

from codegrinder.config import load_config, ConfigurationError


class TestConfig(unittest.TestCase):
    def test_missing_default_uses_canonical_models(self):
        with tempfile.TemporaryDirectory() as d:
            c = load_config(cwd=Path(d))
            self.assertIsNone(c.source)
            self.assertIsNone(c.sha256)
            plan, code, review = c.adapters()
            self.assertEqual("gpt-6-sol", plan.identity()["model"])
            self.assertEqual("deepseek-flash", code.identity()["model"])
            self.assertEqual("high", code.identity()["thinking"])
            self.assertEqual("gpt-6-sol", review.identity()["model"])

    def test_implicit_grind_toml_in_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "grind.toml")
            path.write_text('[steps.code]\nmodel = "deepseek-flash"\n')
            c = load_config(cwd=Path(d))
            self.assertEqual(path.resolve(), c.source)
            self.assertIsNotNone(c.sha256)

    def test_explicit_custom_filename_takes_precedence(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "grind.toml").write_text('[steps.plan]\nmodel="gpt-6-luna"\n')
            custom = Path(d, "my-config.toml")
            custom.write_text('[steps.plan]\nmodel="gpt-6-sol"\nreasoning_effort="high"\n')
            c = load_config(custom, cwd=Path(d))
            self.assertEqual(custom.resolve(), c.source)
            self.assertEqual("gpt-6-sol", c.plan["model"])

    def test_explicit_missing_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ConfigurationError, "CONFIG_MISSING"):
                load_config(Path(d, "absent.toml"))

    def test_unknown_keys_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "grind.toml")
            p.write_text('[steps.review]\nadaptor="codex"\n')
            with self.assertRaisesRegex(ConfigurationError, "unknown steps.review"):
                load_config(cwd=Path(d))

    def test_invalid_model_and_adapter_fail_closed(self):
        for src in ('[steps.plan]\nmodel="gpt 6 sol"\n',
                    '[steps.code]\nadapter="codex"\n',
                    '[steps.code]\nthinking="super"\n',
                    '[steps.review]\nmodel=10\n'):
            with self.subTest(src=src), tempfile.TemporaryDirectory() as d:
                Path(d, "grind.toml").write_text(src)
                with self.assertRaises(ConfigurationError):
                    load_config(cwd=Path(d))

    def test_bad_toml_fails(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "grind.toml").write_text('[steps.plan\n')
            with self.assertRaisesRegex(ConfigurationError, "cannot parse TOML"):
                load_config(cwd=Path(d))


if __name__ == "__main__":
    unittest.main()
