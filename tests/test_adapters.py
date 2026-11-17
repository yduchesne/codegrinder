import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codegrinder.adapters import CodexAdapter, PiAdapter
from codegrinder.adapters.codex import CodexAdapterError
from codegrinder.adapters.pi import PiAdapterError
from codegrinder.interfaces import CodingTask, PlanningStep, CodingStep, ReviewStep, StructuredTask


class TestAdapterContracts(unittest.TestCase):
    def test_defaults(self):
        codex = CodexAdapter()
        pi = PiAdapter()
        self.assertEqual({"adapter": "codex", "model": "gpt-6-sol", "reasoning_effort": "high"}, codex.identity())
        self.assertEqual({"adapter": "pi", "provider": "deepseek", "model": "deepseek-flash", "thinking": "high"}, pi.identity())
        self.assertIsInstance(codex, PlanningStep)
        self.assertIsInstance(codex, ReviewStep)
        self.assertIsInstance(pi, CodingStep)

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
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = CodingTask(root, 'PR1', 'code', 'implement', root / 'artifacts', root / 'state', 0, 10)
            events = '\n'.join([json.dumps({'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'stop'}}), json.dumps({'type': 'agent_settled', 'aborted': False})])
            def run(args, **kwargs):
                self.assertEqual(['pi', '--mode', 'json', '--provider', 'deepseek', '--model', 'deepseek-flash', '--thinking', 'high'], args[:9])
                self.assertEqual('implement', args[-1])
                return subprocess.CompletedProcess(args, 0, events, '')
            with patch('codegrinder.adapters.pi.subprocess.run', side_effect=run):
                PiAdapter().execute(task)
            self.assertTrue((task.artifacts / 'code-0.stdout').exists())

    def test_pi_aborted_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = CodingTask(root, 'PR1', 'code', 'implement', root, root / 'state', 0, 10)
            events = json.dumps({'type': 'agent_settled', 'aborted': True})
            with patch('codegrinder.adapters.pi.subprocess.run', return_value=subprocess.CompletedProcess([], 0, events, '')):
                with self.assertRaises(PiAdapterError):
                    PiAdapter().execute(task)


if __name__ == '__main__':
    unittest.main()
