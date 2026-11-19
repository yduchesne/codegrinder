"""Mock asynchronous Pi subprocess streaming, timeouts, and result validation."""
from __future__ import annotations

import asyncio
import hashlib
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
        self.assertEqual("Sensitive final content", Path(result.final_report_path).read_text())
        self.assertEqual(
            hashlib.sha256(b"Sensitive final content").hexdigest(),
            result.final_report_sha256,
        )
        self.assertEqual(0o600, Path(result.final_report_path).stat().st_mode & 0o777)
        self.assertEqual("COMPLETED", json.loads(
            (self.task.artifacts / "code-0.result.json").read_text()
        )["status"])

    def test_uses_only_last_assistant_final_text_not_intermediate_or_tool_output(self):
        prefix = b"".join((
            line({"type": "session", "id": "session-123"}),
            line({"type": "message_end", "message": {
                "role": "assistant", "stopReason": "toolUse",
                "content": [{"type": "text", "text": "INTERMEDIATE_MESSAGE_NOT_IN_REPORT"}],
            }}),
            line({"type": "tool_execution_end", "toolName": "bash",
                  "result": "TOOL_OUTPUT_NOT_IN_REPORT"}),
        ))
        suffix = b"".join((
            line({"type": "message_end", "message": {
                "role": "assistant", "stopReason": "stop", "provider": "test-provider",
                "model": "test-model",
                "content": [
                    {"type": "thinking", "thinking": "THINKING_NOT_IN_REPORT"},
                    {"type": "text", "text": "Completed implementation."},
                    {"type": "tool_result", "text": "IGNORED_TOOL_TEXT"},
                    {"type": "text", "text": "Tests passed."},
                ],
            }}),
            line({"type": "agent_settled", "aborted": False}),
        ))
        result = self.invoke(FakeProcess(prefix + suffix))
        self.assertEqual(
            "Completed implementation.\\n\\nTests passed.".replace("\\n", "\n"),
            Path(result.final_report_path).read_text(),
        )
        self.assertNotIn("INTERMEDIATE_MESSAGE_NOT_IN_REPORT", Path(result.final_report_path).read_text())
        self.assertNotIn("TOOL_OUTPUT_NOT_IN_REPORT", Path(result.final_report_path).read_text())
        self.assertNotIn("THINKING_NOT_IN_REPORT", Path(result.final_report_path).read_text())

    def test_missing_empty_or_malformed_final_report_fails_closed(self):
        def events(content):
            return b"".join((
                line({"type": "agent_start"}),
                line({"type": "message_end", "message": {
                    "role": "assistant", "stopReason": "stop", "provider": "test-provider",
                    "model": "test-model", "content": content,
                }}),
                line({"type": "agent_settled", "aborted": False}),
            ))
        for content, code in (
            ([], "CODING_REPORT_EMPTY"),
            ([{"type": "thinking", "thinking": "private"}], "CODING_REPORT_EMPTY"),
            ([{"type": "text", "text": "   "}], "CODING_REPORT_EMPTY"),
            ([{"type": "text", "text": 123}], "CODING_REPORT_CONTENT_INVALID"),
            ([{"type": "text", "text": "X" * (256 * 1024 + 1)}], "CODING_REPORT_TOO_LARGE"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(PiAdapterError, code):
                self.invoke(FakeProcess(events(content)))
            self.assertFalse((self.task.artifacts / "code-0.result.json").exists())

    def test_assistant_after_settled_or_without_terminal_stop_is_rejected(self):
        complete = b"".join((
            line({"type": "message_end", "message": {
                "role": "assistant", "stopReason": "stop", "model": "test-model",
                "provider": "test-provider", "content": [{"type": "text", "text": "first report"}],
            }}),
            line({"type": "agent_settled", "aborted": False}),
        ))
        late = complete + line({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "stop", "model": "test-model",
            "provider": "test-provider", "content": [{"type": "text", "text": "late report"}],
        }})
        no_stop = b"".join((
            line({"type": "message_end", "message": {
                "role": "assistant", "model": "test-model",
                "provider": "test-provider", "content": [{"type": "text", "text": "unknown status"}],
            }}),
            line({"type": "agent_settled", "aborted": False}),
        ))
        for raw, code in (
            (late, "PI_MESSAGE_AFTER_SETTLED"),
            (no_stop, "PI_INCOMPLETE"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(PiAdapterError, code):
                self.invoke(FakeProcess(raw))

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

    def test_unique_attempt_result_and_process_callback(self):
        from codegrinder.interfaces import CodingTask
        seen = []
        fake = FakeProcess(good_events())
        fake.pid = 987654
        self.task = CodingTask(
            self.root, "PR-1", "code", "implement",
            self.root / "artifacts", self.root / "state", 0, 5,
            self.events.append, "PR-1-00000008", seen.append,
        )
        result = self.invoke(fake)
        self.assertEqual("PR-1-00000008", result.attempt_id)
        self.assertEqual([987654], seen)
        path = self.task.artifacts / "code-0-PR-1-00000008.result.json"
        self.assertTrue(path.is_file())
        self.assertEqual(result.attempt_id, json.loads(path.read_text())["attempt_id"])

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
