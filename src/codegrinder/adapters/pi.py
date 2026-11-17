"""Pi CLI coding adapter using a configurable DeepSeek model."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from ..interfaces import AgentExecutionError, CodingTask


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

    def execute(self, task: CodingTask) -> None:
        task.artifacts.mkdir(parents=True, exist_ok=True)
        session_dir = task.state_dir / "pi-sessions" / task.pr_id
        session_dir.mkdir(parents=True, exist_ok=True)
        name = f"{task.phase}-{task.fixup_number}"
        args = [
            self.executable, "--mode", "json", "--provider", self.provider,
            "--model", self.model, "--thinking", self.thinking,
            "--session-dir", str(session_dir), task.prompt,
        ]
        completed = subprocess.run(args, cwd=task.worktree, capture_output=True,
                                   text=True, timeout=task.timeout_seconds, check=False)
        (task.artifacts / (name + ".stdout")).write_text(completed.stdout, encoding="utf-8")
        (task.artifacts / (name + ".stderr")).write_text(completed.stderr, encoding="utf-8")
        if completed.returncode:
            raise PiAdapterError(f"PI_FAILED({completed.returncode}): {completed.stderr[-650:]}")
        validate_pi_events(completed.stdout)
