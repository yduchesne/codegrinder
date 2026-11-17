"""Codex CLI adapter for read-only planning and independent review."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from ..interfaces import AgentExecutionError, PreflightResult, PreflightTask, StructuredTask


class CodexAdapterError(AgentExecutionError):
    pass


@dataclass(frozen=True)
class CodexAdapter:
    model: str = "gpt-6-sol"
    reasoning_effort: str = "high"
    executable: str = "codex"

    def __post_init__(self) -> None:
        if not self.model.strip() or self.reasoning_effort not in {"low", "medium", "high", "xhigh"}:
            raise ValueError("Invalid Codex adapter model/reasoning configuration")

    def identity(self) -> dict[str, str]:
        return {"adapter": "codex", "model": self.model, "reasoning_effort": self.reasoning_effort}

    def preflight(self, task: PreflightTask) -> PreflightResult:
        """Verify Codex authentication and the configured model using a read-only probe.

        A successful login alone is insufficient: the model may be unavailable
        or authorization may expire between authentication and execution.
        No CLI stderr or credential-bearing output is retained or surfaced.
        """
        timeout = max(1, min(task.timeout_seconds, 90))
        try:
            auth = subprocess.run(
                [self.executable, "login", "status"],
                cwd=task.repo, capture_output=True, text=True,
                timeout=min(20, timeout), check=False,
            )
            if auth.returncode:
                return PreflightResult.failed("AUTH_NOT_READY")
            probe = subprocess.run(
                [
                    self.executable, "exec", "--ephemeral",
                    "--sandbox", "read-only", "--model", self.model,
                    "-c", f"model_reasoning_effort={self.reasoning_effort}",
                    "-",
                ],
                cwd=task.repo,
                input="Reply with exactly CODEGRINDER_PREFLIGHT_OK. Do not use tools.",
                capture_output=True, text=True,
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
        if "CODEGRINDER_PREFLIGHT_OK" not in probe.stdout:
            return PreflightResult.failed("MODEL_PROBE_UNVERIFIED")
        return PreflightResult.passed()

    def execute(self, task: StructuredTask) -> dict:
        task.artifacts.mkdir(parents=True, exist_ok=True)
        output = task.artifacts / (task.phase + ".json")
        if output.exists():
            output.unlink()  # Never accept stale JSON after a failed invocation.
        args = [
            self.executable, "exec", "--model", self.model,
            "-c", f"model_reasoning_effort={self.reasoning_effort}",
            "--sandbox", "read-only", "--output-schema", str(task.schema),
            "-o", str(output), "-",
        ]
        completed = subprocess.run(args, cwd=task.worktree, input=task.prompt,
                                   capture_output=True, text=True,
                                   timeout=task.timeout_seconds, check=False)
        (task.artifacts / (task.phase + ".stdout")).write_text(completed.stdout, encoding="utf-8")
        (task.artifacts / (task.phase + ".stderr")).write_text(completed.stderr, encoding="utf-8")
        if completed.returncode:
            raise CodexAdapterError(f"CODEX_FAILED({completed.returncode}): {completed.stderr[-650:]}")
        if not output.is_file():
            raise CodexAdapterError(f"CODEX_MISSING_OUTPUT: {task.phase}")
        try:
            result = json.loads(output.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CodexAdapterError(f"CODEX_INVALID_JSON: {task.phase}") from exc
        if not isinstance(result, dict):
            raise CodexAdapterError("CODEX_INVALID_JSON: expected object")
        return result
