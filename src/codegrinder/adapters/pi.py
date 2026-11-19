"""Pi CLI adapter using the configured provider and model."""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from ..interfaces import (
    AgentExecutionError, CodingProgress, CodingResult, CodingTask, PreflightTask, PreflightResult,
)
from ..coding_reports import CodingReportError, extract_final_report, persist_report, report_path


class PiAdapterError(AgentExecutionError):
    pass


def validate_pi_events(text: str) -> None:
    settled = None
    last_assistant = None
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError) as exc:
            raise PiAdapterError("PI_INVALID_JSONL") from exc
        if not isinstance(event, dict):
            raise PiAdapterError("PI_INVALID_JSONL: expected object event")
        if event.get("type") == "agent_settled":
            settled = event
        if event.get("type") == "message_end" and isinstance(event.get("message"), dict):
            if event["message"].get("role") == "assistant":
                last_assistant = event["message"]
    if settled is None or settled.get("aborted") is not False:
        raise PiAdapterError("PI_INCOMPLETE: no successful agent_settled event")
    if last_assistant is None or last_assistant.get("stopReason") in ("error", "aborted", "length", "pending", "toolUse"):
        raise PiAdapterError("PI_INCOMPLETE: last assistant message did not finish normally")


@dataclass(frozen=True)
class PiAdapter:
    provider: str = "deepseek"
    model: str = "deepseek-flash"
    thinking: str = "high"
    executable: str = "pi"

    def __post_init__(self) -> None:
        if (not self.provider.strip() or not self.model.strip() or
            self.thinking not in {"off", "minimal", "low", "medium", "high", "xhigh", "max"}):
            raise ValueError("Invalid Pi adapter provider/model/thinking configuration")

    def identity(self) -> dict[str, str]:
        return {"adapter": "pi", "provider": self.provider, "model": self.model, "thinking": self.thinking}

    def preflight(self, task: PreflightTask) -> PreflightResult:
        """Check auth and run a no-tools, no-session probe on the selected model."""
        timeout = max(1, min(task.timeout_seconds, 90))
        try:
            auth = subprocess.run(
                [
                    self.executable, "auth", "check", "--provider", self.provider,
                    "--model", self.model, "--json",
                ],
                cwd=task.repo, capture_output=True, text=True,
                timeout=min(20, timeout), check=False,
            )
            # Status codes: 0 ready; 1 not ready; 2 invalid.
            # Output is deliberately not logged (it may contain sensitive data).
            if auth.returncode:
                return PreflightResult.failed("AUTH_NOT_READY")
            probe = subprocess.run(
                [
                    self.executable, "--mode", "json", "--no-session", "--no-tools",
                    "--no-extensions", "--no-skills", "--no-context-files",
                    "--provider", self.provider, "--model", self.model,
                    "--thinking", self.thinking,
                    "Reply with exactly CODEGRINDER_PREFLIGHT_OK. Do not use tools.",
                ],
                cwd=task.repo, capture_output=True, text=True,
                timeout=timeout, check=False,
            )
        except FileNotFoundError:
            return PreflightResult.failed("HARNESS_NOT_INSTALLED")
        except subprocess.TimeoutExpired:
            return PreflightResult.failed("MODEL_PROBE_TIMEOUT")
        except OSError:
            return PreflightResult.failed("HARNESS_EXECUTION_FAILED")
        if probe.returncode:
            return PreflightResult.failed("MODEL_PROBE_FAILED")
        try:
            validate_pi_events(probe.stdout)
            assistant = None
            for line in probe.stdout.splitlines():
                event = json.loads(line)
                message = event.get("message") if isinstance(event, dict) else None
                if event.get("type") == "message_end" and isinstance(message, dict):
                    if message.get("role") == "assistant":
                        assistant = message
            if assistant is None or not any(
                isinstance(item, dict)
                and item.get("type") == "text"
                and "CODEGRINDER_PREFLIGHT_OK" in str(item.get("text", ""))
                for item in assistant.get("content", [])
            ):
                return PreflightResult.failed("MODEL_PROBE_UNVERIFIED")
            # Pi permits fuzzy --model matching. Verify which provider/model
            # actually answered, rather than accepting a different selection.
            if (assistant.get("provider") != self.provider
                    or assistant.get("model") != self.model):
                return PreflightResult.failed("MODEL_SELECTION_MISMATCH")
        except (PiAdapterError, ValueError, TypeError, AttributeError):
            return PreflightResult.failed("MODEL_PROBE_INVALID_OUTPUT")
        return PreflightResult.passed()

    def execute(self, task: CodingTask) -> CodingResult:
        """Sync compatibility wrapper; async hosts should await execute_async()."""
        return asyncio.run(self.execute_async(task))

    async def execute_async(self, task: CodingTask) -> CodingResult:
        """Drain Pi's stdout/stderr concurrently and convert JSONL to safe progress."""
        task.artifacts.mkdir(parents=True, exist_ok=True)
        session_dir = task.state_dir / "pi-sessions" / task.pr_id
        session_dir.mkdir(parents=True, exist_ok=True)
        name = f"{task.phase}-{task.fixup_number}"
        if task.attempt_id:
            name += f"-{task.attempt_id}"
        stdout_path = task.artifacts / f"{name}.stdout"
        stderr_path = task.artifacts / f"{name}.stderr"
        progress_path = task.artifacts / f"{name}.progress.jsonl"
        result_path = task.artifacts / f"{name}.result.json"
        final_report_path = report_path(
            task.artifacts, phase=task.phase,
            fixup_number=task.fixup_number, attempt_id=task.attempt_id,
        )
        args = [
            self.executable, "--mode", "json", "--provider", self.provider,
            "--model", self.model, "--thinking", self.thinking,
            "--session-dir", str(session_dir), task.prompt,
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *args, cwd=str(task.worktree),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise PiAdapterError("PI_LAUNCH_FAILED") from exc
        try:
            if task.on_process_started is not None and type(process.pid) is int:
                task.on_process_started(process.pid)
        except BaseException:
            await _terminate_process(process)
            raise
        stdout_stream, stderr_stream = process.stdout, process.stderr
        if stdout_stream is None or stderr_stream is None:
            await _terminate_process(process)
            raise PiAdapterError("PI_MISSING_OUTPUT_STREAM")

        started = time.monotonic()
        event_count = 0
        tool_calls = 0
        failed_tools = 0
        session_id: str | None = None
        settled = False
        assistant: dict | None = None
        progress_sequence = 0
        updates = 0
        max_line_bytes = 4 * 1024 * 1024
        max_stdout_bytes = 128 * 1024 * 1024
        max_stderr_bytes = 8 * 1024 * 1024

        def record(event_type: str, progress_file, tool_name: str | None = None) -> None:
            nonlocal progress_sequence
            progress_sequence += 1
            tool = (
                tool_name if isinstance(tool_name, str)
                and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", tool_name) else None
            )
            progress = CodingProgress(
                pr_id=task.pr_id, phase=task.phase,
                sequence=progress_sequence,
                elapsed_seconds=round(time.monotonic() - started, 3),
                event_type=event_type, tool_name=tool,
            )
            progress_file.write(
                (json.dumps(asdict(progress), sort_keys=True) + "\n").encode("utf-8")
            )
            progress_file.flush()
            if task.on_progress is not None:
                task.on_progress(progress)

        def consume(raw: bytes, progress_file) -> None:
            nonlocal event_count, tool_calls, failed_tools, session_id, settled, assistant, updates
            if len(raw) > max_line_bytes:
                raise PiAdapterError("PI_EVENT_TOO_LARGE")
            try:
                event = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise PiAdapterError("PI_INVALID_JSONL") from exc
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise PiAdapterError("PI_INVALID_JSONL: object/event type required")
            event_count += 1
            if event_count > 1_000_000:
                raise PiAdapterError("PI_EVENT_COUNT_LIMIT")
            kind = event["type"]
            if kind == "session" and isinstance(event.get("id"), str):
                session_id = event["id"][:128]
            if kind == "agent_settled":
                settled = event.get("aborted") is False
            if kind == "message_end" and isinstance(event.get("message"), dict):
                if event["message"].get("role") == "assistant":
                    if settled:
                        raise PiAdapterError("PI_MESSAGE_AFTER_SETTLED")
                    assistant = event["message"]
                    record("assistant_message", progress_file)
            elif kind == "tool_execution_start":
                tool_calls += 1
                record("tool_started", progress_file, event.get("toolName"))
            elif kind == "tool_execution_end":
                if event.get("isError") is True:
                    failed_tools += 1
                record("tool_finished", progress_file, event.get("toolName"))
            elif kind in {
                "agent_start", "agent_settled", "turn_start", "turn_end",
                "auto_retry_start", "auto_retry_end", "compaction_start", "compaction_end",
            }:
                record(kind, progress_file)
            elif kind == "message_update":
                updates += 1
                if updates % 100 == 0:
                    record("stream_activity", progress_file)

        async def drain_stdout(output, progress_file) -> None:
            buffer = b""
            total = 0
            while True:
                chunk = await stdout_stream.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_stdout_bytes:
                    raise PiAdapterError("PI_STDOUT_LIMIT")
                output.write(chunk)
                output.flush()
                buffer += chunk
                if len(buffer) > max_line_bytes and b"\n" not in buffer:
                    raise PiAdapterError("PI_EVENT_TOO_LARGE")
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    if raw.strip():
                        consume(raw, progress_file)
                if len(buffer) > max_line_bytes:
                    raise PiAdapterError("PI_EVENT_TOO_LARGE")
            if buffer.strip():
                consume(buffer, progress_file)

        async def drain_stderr(output) -> None:
            total = 0
            while True:
                chunk = await stderr_stream.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_stderr_bytes:
                    raise PiAdapterError("PI_STDERR_LIMIT")
                output.write(chunk)
                output.flush()

        try:
            with (
                _private_binary(stdout_path) as out,
                _private_binary(stderr_path) as err,
                _private_binary(progress_path) as progress,
            ):
                record("started", progress)
                await asyncio.wait_for(
                    asyncio.gather(
                        drain_stdout(out, progress), drain_stderr(err), process.wait()
                    ),
                    timeout=task.timeout_seconds,
                )
        except TimeoutError as exc:
            await _terminate_process(process)
            raise PiAdapterError("PI_TIMEOUT") from exc
        except BaseException:
            await _terminate_process(process)
            raise

        if process.returncode != 0:
            raise PiAdapterError(f"PI_FAILED({process.returncode})")
        if not settled or assistant is None or assistant.get("stopReason") != "stop":
            raise PiAdapterError("PI_INCOMPLETE: missing successful settled assistant")
        if (
            assistant.get("provider") is not None
            and assistant.get("provider") != self.provider
        ) or (
            assistant.get("model") is not None and assistant.get("model") != self.model
        ):
            raise PiAdapterError("PI_MODEL_SELECTION_MISMATCH")

        # Only the final completed assistant message contributes report text.
        # Intermediate model output and tool traces remain in the private JSONL.
        try:
            report = extract_final_report(assistant)
            report_sha256 = persist_report(final_report_path, report)
        except CodingReportError as exc:
            raise PiAdapterError(str(exc)) from exc

        result = CodingResult(
            pr_id=task.pr_id, phase=task.phase, adapter="pi",
            model=self.model, status="COMPLETED",
            exit_code=process.returncode,
            duration_seconds=round(time.monotonic() - started, 3),
            event_count=event_count, tool_calls=tool_calls,
            failed_tools=failed_tools, session_id=session_id,
            stdout_path=str(stdout_path), stderr_path=str(stderr_path),
            progress_path=str(progress_path), attempt_id=task.attempt_id or None,
            final_report_path=str(final_report_path),
            final_report_sha256=report_sha256,
        )
        with _private_binary(result_path) as handle:
            handle.write((json.dumps(asdict(result), sort_keys=True, indent=2) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        return result


@contextmanager
def _private_binary(path: Path):
    """Keep raw agent traces restricted to the current user."""
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    os.chmod(path, 0o600)
    with os.fdopen(fd, "wb") as handle:
        yield handle


async def _terminate_process(process) -> None:
    if process.returncode is not None:
        return
    try:
        # Pi runs in a dedicated process group, so child tools are terminated
        # together on timeout or a failed progress callback.
        pid = getattr(process, "pid", None)
        if type(pid) is int and pid > 0:
            os.killpg(pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        try:
            pid = getattr(process, "pid", None)
            if type(pid) is int and pid > 0:
                os.killpg(pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
