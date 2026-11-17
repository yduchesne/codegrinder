"""Mock asynchronous Pi subprocess streaming, timeouts, and result validation."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codegrinder.adapters.pi import PiAdapter, PiAdapterError
from codegrinder.interfaces import CodingResult, CodingTask


def line(value: dict) -> bytes:
    return (json.dumps(value) + "\n").encode("utf-8")


def good_events(model="test-model", provider="test-provider") -> bytes:
    return b"".join((
        line({"type": "session", "id": "session-123"}),
        line({"type": "agent_start"}),
        line({"type": "tool_execution_start", "toolName": "edit", "args": {"secret": "DO_NOT_LOG"}}),
        line({"type": "tool_execution_end", "toolName": "edit", "isError": False}),
        line({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "stop", "model": model, "provider": provider,
            "content": [{"type": "text", "text": "Sensitive final content"}],
        }}),
        line({"type": "agent_settled", "aborted": False}),
    ))


class Stream:
    def __init__(self, chunks: list[bytes], delay: float = 0.0):
        self.chunks, self.delay = list(chunks), delay

    async def read(self, _n: int) -> bytes:
        await asyncio.sleep(self.delay)
        return self.chunks.pop(0) if self.chunks else b""


class FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b"", status: int = 0):
        self.stdout = Stream([stdout[:27], stdout[27:]])
        self.stderr = Stream([stderr])
        self.exit_status = status
        self.returncode: int | None = None
        self.terminated = False

    async def wait(self):
        await asyncio.sleep(0)
        self.returncode = self.exit_status
        return self.exit_status

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.terminated = True
        self.returncode = -9


class TestPiStream(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.events = []
        self.task = CodingTask(
            self.root, "PR-1", "code", "implement", self.root / "artifacts",
            self.root / "state", 0, 5, self.events.append,
        )
        self.adapter = PiAdapter(provider="test-provider", model="test-model")

    def invoke(self, fake: FakeProcess):
        async def spawn(*args, **kwargs):
            self.assertEqual("pi", args[0])
            self.assertIn("test-model", args)
            return fake
        with patch("codegrinder.adapters.pi.asyncio.create_subprocess_exec", side_effect=spawn):
            return self.adapter.execute(self.task)

    def test_streamed_progress_result_and_private_logs(self):
        output = good_events()
        result = self.invoke(FakeProcess(output, b"stderr diagnostic"))
        self.assertIsInstance(result, CodingResult)
        self.assertEqual("COMPLETED", result.status)
        self.assertEqual("session-123", result.session_id)
        self.assertEqual(6, result.event_count)
        self.assertEqual(1, result.tool_calls)
        self.assertEqual("edit", self.events[2].tool_name)
        self.assertIn("agent_settled", [e.event_type for e in self.events])
        self.assertNotIn("DO_NOT_LOG", Path(result.progress_path).read_text())
        self.assertNotIn("Sensitive final content", Path(result.progress_path).read_text())
        self.assertEqual(output, Path(result.stdout_path).read_bytes())
        self.assertEqual(b"stderr diagnostic", Path(result.stderr_path).read_bytes())
        self.assertEqual(0o600, Path(result.stdout_path).stat().st_mode & 0o777)
        self.assertEqual("COMPLETED", json.loads(
            (self.task.artifacts / "code-0.result.json").read_text()
        )["status"])

    def test_progress_callback_runs_before_process_exits(self):
        """An agent waiting on progress must not deadlock the adapter."""
        gate = asyncio.Event()

        class GateProcess(FakeProcess):
            async def wait(self):
                await asyncio.wait_for(gate.wait(), timeout=1.0)
                self.returncode = 0
                return 0

        fake = GateProcess(good_events())
        progress = []

        def on_progress(evt):
            progress.append(evt)
            if evt.event_type == "tool_started":
                gate.set()

        self.task = CodingTask(
            self.root, "PR-1", "code", "implement",
            self.root / "artifacts", self.root / "state", 0, 3,
            on_progress,
        )
        result = self.invoke(fake)
        self.assertEqual("COMPLETED", result.status)
        self.assertTrue(gate.is_set())
        self.assertIn("tool_started", [e.event_type for e in progress])

    def test_invalid_json_nonzero_and_incomplete_fail_closed(self):
        for stdout, rc, code in (
            (b'{"type":"agent_start"}\nnot-json\n', 0, "PI_INVALID_JSONL"),
            (good_events(), 1, "PI_FAILED"),
            (line({"type": "agent_start"}), 0, "PI_INCOMPLETE"),
            (good_events(model="other-model"), 0, "PI_MODEL_SELECTION_MISMATCH"),
        ):
            with self.subTest(code=code):
                with self.assertRaisesRegex(PiAdapterError, code):
                    self.invoke(FakeProcess(stdout, status=rc))

    def test_timeout_terminates_and_never_returns_success(self):
        class HungStream:
            async def read(self, _n):
                await asyncio.Event().wait()
        class HungProcess(FakeProcess):
            async def wait(self):
                if not self.terminated:
                    await asyncio.Event().wait()
                return self.returncode
        fake = HungProcess(b"")
        fake.stdout = HungStream()
        fake.stderr = HungStream()
        self.task = CodingTask(
            self.root, "PR-1", "code", "implement", self.root / "artifacts",
            self.root / "state", 0, 0.01,
        )
        with self.assertRaisesRegex(PiAdapterError, "PI_TIMEOUT"):
            self.invoke(fake)
        self.assertTrue(fake.terminated)
        self.assertFalse((self.task.artifacts / "code-0.result.json").exists())


if __name__ == "__main__":
    unittest.main()
