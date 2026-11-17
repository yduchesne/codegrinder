"""Hermetic SBOM wrapper tests; no network, uv subprocess, or CycloneDX install needed."""
from __future__ import annotations

import json
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codegrinder import sbom


def document() -> dict:
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {"component": {"type": "application", "name": "codegrinder", "version": "0.1.0"}},
        "components": [],
    }


class TestSbom(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "pyproject.toml").write_text('[project]\nname = "codegrinder"\nversion = "0.1.0"\n')
        self.project = patch.object(sbom, "PROJECT", self.root)
        self.project.start()
        self.addCleanup(self.project.stop)

    def _validator(self, error=None):
        schema = types.ModuleType("cyclonedx.schema")
        schema.SchemaVersion = types.SimpleNamespace(V1_6="1.6")
        validator = types.ModuleType("cyclonedx.validation.json")
        validate = Mock(return_value=error)
        validator.JsonStrictValidator = lambda version: types.SimpleNamespace(validate_str=validate)
        return patch.dict("sys.modules", {"cyclonedx.schema": schema, "cyclonedx.validation.json": validator})

    def test_validate_rejects_malformed_and_wrong_root(self):
        for value in ("not-json", "[]", json.dumps({**document(), "specVersion": "1.5"}),
                      json.dumps({**document(), "metadata": {"component": {"name": "other"}}}),
                      json.dumps({**document(), "components": None})):
            with self.subTest(value=value), self.assertRaises(sbom.SbomError):
                sbom.validate_document(value)

    def test_schema_is_strict_and_fail_closed(self):
        with self._validator():
            self.assertEqual("CycloneDX", sbom.validate_document(json.dumps(document()))["bomFormat"])
        with self._validator("schema failure"):
            with self.assertRaisesRegex(sbom.SbomError, "schema validation"):
                sbom.validate_document(json.dumps(document()))

    def test_generation_uses_locked_runtime_dependencies(self):
        (self.root / "uv.lock").write_text("stub lock")
        out = self.root / "sbom" / "file.json"
        with patch.object(sbom, "_invoke", side_effect=["", json.dumps(document())]) as run, self._validator():
            sbom.generate(out)
        self.assertEqual("CycloneDX", json.loads(out.read_text())["bomFormat"])
        self.assertIn("--locked", run.call_args_list[0].args[0])
        self.assertIn("--no-dev", run.call_args_list[0].args[0])
        self.assertIn("--no-emit-project", run.call_args_list[0].args[0])
        self.assertEqual("", run.call_args_list[1].kwargs["stdin"])
        self.assertIn("--validate", run.call_args_list[1].args[0])

    def test_missing_lock_and_failed_commands_stop(self):
        with self.assertRaisesRegex(sbom.SbomError, "Missing uv.lock"):
            sbom.generate(self.root / "bom.json")
        with patch.object(sbom.subprocess, "run", return_value=subprocess.CompletedProcess([], 4, "", "failed")):
            with self.assertRaisesRegex(sbom.SbomError, "exit 4"):
                sbom._invoke(["uv", "export"])
        with patch.object(sbom.subprocess, "run", side_effect=OSError("not installed")):
            with self.assertRaisesRegex(sbom.SbomError, "Unable to execute"):
                sbom._invoke(["uv", "export"])

    def test_cli_reports_nonzero_on_invalid_existing_sbom(self):
        path = self.root / "invalid.json"
        path.write_text("{}")
        self.assertEqual(2, sbom.main(["validate", "--input", str(path)]))


if __name__ == "__main__":
    unittest.main()
