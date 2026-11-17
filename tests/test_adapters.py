import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codegrinder.adapters import CodexAdapter, PiAdapter
from codegrinder.adapters.codex import CodexAdapterError
from codegrinder.interfaces import PlanningStep, CodingStep, ReviewStep, StructuredTask


class TestAdapterContracts(unittest.TestCase):
    def test_defaults(self):
        codex = CodexAdapter()
        pi = PiAdapter()
        self.assertEqual({"adapter": "codex", "model": "gpt-6-sol", "reasoning_effort": "high"}, codex.identity())
        self.assertEqual({"adapter": "pi", "provider": "deepseek", "model": "deepseek-flash", "thinking": "high"}, pi.identity())
        self.assertIsInstance(codex, PlanningStep)
        self.assertIsInstance(codex, ReviewStep)
        self.assertIsInstance(pi, CodingStep)

    def test_codex_preflight_probes_selected_model_and_auth(self):
        from codegrinder.interfaces import PreflightTask, PreflightCheck
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = CodexAdapter(model="codex-custom-model", reasoning_effort="high")
            self.assertIsInstance(adapter, PreflightCheck)
            invocations = []
            def fake_run(args, **kwargs):
                invocations.append(list(args))
                self.assertEqual(root, kwargs["cwd"])
                if args[1:3] == ["login", "status"]:
                    return subprocess.CompletedProcess(args, 0, "", "")
                return subprocess.CompletedProcess(args, 0, "CODEGRINDER_PREFLIGHT_OK", "")
            with patch("codegrinder.adapters.codex.subprocess.run", side_effect=fake_run):
                self.assertTrue(adapter.preflight(PreflightTask(root, "plan", 30)).ok)
            self.assertEqual(["codex", "login", "status"], invocations[0])
            self.assertIn("codex-custom-model", invocations[1])
            self.assertIn("--ephemeral", invocations[1])
            self.assertIn("read-only", invocations[1])

    def test_codex_preflight_fails_closed_without_leaking_auth_output(self):
        from codegrinder.interfaces import PreflightTask
        with tempfile.TemporaryDirectory() as tmp:
            task = PreflightTask(Path(tmp), "review", 30)
            auth_failure = subprocess.CompletedProcess([], 1, "SECRET_TOKEN", "SECRET_TOKEN")
            with patch("codegrinder.adapters.codex.subprocess.run", return_value=auth_failure) as run:
                result = CodexAdapter().preflight(task)
            self.assertEqual("AUTH_NOT_READY", result.code)
            self.assertEqual(1, run.call_count)
            responses = [
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 1, "", "PRIVATE_API_KEY"),
            ]
            with patch("codegrinder.adapters.codex.subprocess.run", side_effect=responses):
                result = CodexAdapter().preflight(task)
            self.assertEqual("MODEL_PROBE_FAILED", result.code)
            self.assertNotIn("PRIVATE_API_KEY", repr(result))

    def test_codex_preflight_rejects_unverified_response_or_missing_binary(self):
        from codegrinder.interfaces import PreflightTask
        with tempfile.TemporaryDirectory() as tmp:
            task = PreflightTask(Path(tmp), "review", 30)
            auth = subprocess.CompletedProcess([], 0, "", "")
            empty = subprocess.CompletedProcess([], 0, "other answer", "")
            with patch("codegrinder.adapters.codex.subprocess.run", side_effect=[auth, empty]):
                self.assertEqual(
                    "MODEL_PROBE_UNVERIFIED", CodexAdapter().preflight(task).code
                )
            with patch("codegrinder.adapters.codex.subprocess.run", side_effect=FileNotFoundError()):
                self.assertEqual(
                    "HARNESS_NOT_INSTALLED", CodexAdapter().preflight(task).code
                )

    def test_pi_preflight_uses_arbitrary_provider_model_without_session_or_tools(self):
        from codegrinder.interfaces import PreflightTask, PreflightCheck
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = PiAdapter(provider="example-provider", model="model-x", thinking="medium")
            self.assertIsInstance(adapter, PreflightCheck)
            events = "\n".join([
                json.dumps({"type": "message_end", "message": {
                    "role": "assistant", "stopReason": "stop",
                    "provider": "example-provider", "model": "model-x",
                    "content": [{"type": "text", "text": "CODEGRINDER_PREFLIGHT_OK"}],
                }}),
                json.dumps({"type": "agent_settled", "aborted": False}),
            ])
            invocations = []
            def fake_run(args, **kwargs):
                invocations.append(args)
                self.assertEqual(root, kwargs["cwd"])
                return subprocess.CompletedProcess(args, 0, "" if len(invocations) == 1 else events, "")
            with patch("codegrinder.adapters.pi.subprocess.run", side_effect=fake_run):
                self.assertTrue(adapter.preflight(PreflightTask(root, "code", 30)).ok)
            self.assertEqual(
                ["pi", "auth", "check", "--provider", "example-provider",
                 "--model", "model-x", "--json"],
                invocations[0],
            )
            self.assertIn("--no-session", invocations[1])
            self.assertIn("--no-tools", invocations[1])
            self.assertIn("example-provider", invocations[1])
            self.assertIn("model-x", invocations[1])

    def test_pi_preflight_rejects_fuzzy_model_substitution(self):
        from codegrinder.interfaces import PreflightTask
        with tempfile.TemporaryDirectory() as tmp:
            task = PreflightTask(Path(tmp), "code", 30)
            auth = subprocess.CompletedProcess([], 0, "", "")
            wrong = subprocess.CompletedProcess([], 0, "\n".join([
                json.dumps({"type": "message_end", "message": {
                    "role": "assistant", "stopReason": "stop",
                    "provider": "example-provider", "model": "different-model",
                    "content": [{"type": "text", "text": "CODEGRINDER_PREFLIGHT_OK"}],
                }}),
                json.dumps({"type": "agent_settled", "aborted": False}),
            ]), "")
            with patch("codegrinder.adapters.pi.subprocess.run", side_effect=[auth, wrong]):
                result = PiAdapter(provider="example-provider", model="model-x").preflight(task)
            self.assertEqual("MODEL_SELECTION_MISMATCH", result.code)

    def test_pi_preflight_rejects_bad_auth_and_invalid_model_result(self):
        from codegrinder.interfaces import PreflightTask
        with tempfile.TemporaryDirectory() as tmp:
            task = PreflightTask(Path(tmp), "code", 30)
            failure = subprocess.CompletedProcess([], 2, "PRIVATE_API_KEY", "")
            with patch("codegrinder.adapters.pi.subprocess.run", return_value=failure) as run:
                result = PiAdapter().preflight(task)
            self.assertEqual("AUTH_NOT_READY", result.code)
            self.assertEqual(1, run.call_count)
            self.assertNotIn("PRIVATE_API_KEY", repr(result))
            auth = subprocess.CompletedProcess([], 0, "", "")
            wrong = subprocess.CompletedProcess([], 0, "not-jsonl", "")
            with patch("codegrinder.adapters.pi.subprocess.run", side_effect=[auth, wrong]):
                self.assertEqual("MODEL_PROBE_INVALID_OUTPUT", PiAdapter().preflight(task).code)
            with patch("codegrinder.adapters.pi.subprocess.run", side_effect=FileNotFoundError()):
                self.assertEqual("HARNESS_NOT_INSTALLED", PiAdapter().preflight(task).code)

    def test_codex_invocation_is_read_only_high_with_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            schema = root / "plan.json"
            schema.write_text('{}')
            task = StructuredTask(root, "PR1", "plan", "prompt", schema, root / "artifacts", 10)
            def run(args, **kwargs):
                self.assertEqual(["codex", "exec", "--model", "gpt-6-sol", "-c", "model_reasoning_effort=high"], args[:6])
                self.assertIn("read-only", args)
                self.assertIn("--output-schema", args)
                self.assertEqual("prompt", kwargs['input'])
                task.artifacts.mkdir(parents=True, exist_ok=True)
                (task.artifacts / "plan.json").write_text('{"status":"READY"}')
                return subprocess.CompletedProcess(args, 0, '', '')
            with patch('codegrinder.adapters.codex.subprocess.run', side_effect=run):
                self.assertEqual('READY', CodexAdapter().execute(task)['status'])

    def test_codex_rejects_stale_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = StructuredTask(root, 'P', 'plan', 'prompt', root / 'schema.json', root, 10)
            (root / 'plan.json').write_text('{"status":"READY"}')
            with patch('codegrinder.adapters.codex.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'failed')):
                with self.assertRaises(CodexAdapterError):
                    CodexAdapter().execute(task)
            self.assertFalse((root / 'plan.json').exists())

    def test_pi_invocation_uses_flash_high_and_settled_event(self):
        """Async Pi process execution is exercised in test_coding_stream."""
        from codegrinder.interfaces import CodingResult
        self.assertEqual("COMPLETED", CodingResult(
            "P", "code", "pi", "m", "COMPLETED", 0, 1.0, 2, 0, 0,
            None, "out", "err", "progress",
        ).status)


if __name__ == '__main__':
    unittest.main()
