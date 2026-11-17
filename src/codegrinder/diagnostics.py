"""Read-only configuration, environment and durable roadmap diagnostics.

Never instantiates agent adapters, invokes GitHub, starts workflow workers,
creates directories, or alters the persisted orchestration state.
"""
from __future__ import annotations

import hashlib
import json
import platform
import shutil
from pathlib import Path

from .config import GrindConfig
from .engine import StopWorkflow, validate_roadmap
from .roadmap import RoadmapFormatError, parse_roadmap
from .selection import PRSelectionError, SequentialPRSelector, is_completed

MAX_STATE_BYTES = 16 * 1024 * 1024


def _path_label(path: Path | None) -> str:
    if path is None:
        return "N/A"
    return f"{path} ({'present' if path.is_file() else 'MISSING'})"


def _file_sha256(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_state(directory: Path | None) -> tuple[dict | None, str]:
    if directory is None:
        return None, "N/A (no repository or roadmap)"
    filename = directory / "state.json"
    if not filename.exists():
        return None, "NOT_STARTED"
    try:
        size = filename.stat().st_size
        if size > MAX_STATE_BYTES:
            return None, "INVALID (state exceeds size limit)"
        if size == 0:
            return None, "INVALID (empty state)"
        state = json.loads(filename.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None, "INVALID (state unreadable)"
    if not isinstance(state, dict) or not isinstance(state.get("prs"), dict):
        return None, "INVALID (state structure)"
    if any(not isinstance(k, str) or not isinstance(v, dict)
           for k, v in state["prs"].items()):
        return None, "INVALID (PR entries)"
    return state, "PRESENT"


def _last_checkpoint(prs: list[dict], entries: dict) -> tuple[str, str, str, str]:
    order = {pr["id"]: n for n, pr in enumerate(prs)}
    known = [(pid, entry) for pid, entry in entries.items()
             if pid in order and isinstance(entry, dict)]
    if not known:
        return "N/A", "N/A", "N/A", "N/A"

    def last_activity(item: tuple[str, dict]) -> tuple[str, int]:
        pid, entry = item
        history = entry.get("history")
        if isinstance(history, list):
            for event in reversed(history):
                if isinstance(event, dict) and isinstance(event.get("at"), str):
                    # All controller history dates are UTC ISO-8601.
                    return event["at"], order[pid]
        return "", order[pid]

    pid, entry = max(known, key=last_activity)
    stage = entry.get("stage")
    if not isinstance(stage, str):
        return pid, "UNKNOWN", "UNKNOWN", "INVALID (saved stage)"
    previous_step = stage
    if is_completed(stage) or stage == "STOPPED":
        history = entry.get("history")
        if isinstance(history, list) and history:
            event = history[-1]
            if isinstance(event, dict) and isinstance(event.get("from"), str):
                previous_step = event["from"]

    if is_completed(stage):
        outcome = (
            "SUCCEEDED (saved merge evidence, not rechecked)"
            if entry.get("merged_at") and entry.get("merge_commit")
            else "UNVERIFIED (no saved merge evidence)"
        )
    elif stage == "STOPPED":
        outcome = "FAILED / STOPPED"
    elif stage == "WAIT_MERGE":
        outcome = "INCOMPLETE (awaiting confirmed merge)"
    else:
        outcome = "INCOMPLETE (checkpoint only; step may have been interrupted)"
    return pid, previous_step, stage, outcome


def _offline_next(prs: list[dict], entries: dict) -> str:
    for pr in prs:
        pid = pr["id"]
        entry = entries.get(pid, {})
        if pr.get("status") == "DONE" and not is_completed(entry.get("stage")):
            return f"N/A (roadmap completion for {pid} needs GitHub reconciliation)"
        if is_completed(entry.get("stage")) and not (
            entry.get("merged_at") and entry.get("merge_commit")
        ):
            return f"N/A (saved completion for {pid} lacks merge evidence)"
    try:
        selected = SequentialPRSelector().next_pr(prs, entries)
    except (PRSelectionError, KeyError, TypeError) as exc:
        code = str(exc).split(":", 1)[0]
        if not code.startswith("DEPENDENCY_"):
            code = "SELECTION_BLOCKED"
        return f"N/A ({code}; reconciliation required)"
    if selected is None:
        return "N/A (roadmap completed in saved state; merges not rechecked)"
    pid = selected["id"]
    if entries.get(pid, {}).get("stage") == "STOPPED":
        return f"{pid} (STOPPED; manual reconciliation required)"
    return f"{pid} (offline candidate; GitHub not queried)"


def explain(
    config: GrindConfig, *, repo: Path | None, state_dir: Path | None,
    roadmap: Path | None = None, guide: Path | None = None,
) -> str:
    """Explain configuration and saved progress without changing the environment."""
    roadmap = config.roadmap_path if roadmap is None else roadmap
    guide = config.guide_path if guide is None else guide
    lines = [
        "Codegrinder environment",
        f"  Python: {platform.python_version()}",
        f"  Repository: {repo if repo else 'N/A (not a Git checkout)'}",
        f"  Configuration: {config.source or 'N/A (built-in defaults)'}",
        "",
        "Configured steps",
    ]
    for phase, settings in (("plan", config.plan), ("code", config.code), ("review", config.review)):
        provider = f" provider={settings['provider']}" if "provider" in settings else ""
        effort = settings.get("thinking", settings.get("reasoning_effort", "N/A"))
        lines.append(
            f"  {phase}: adapter={settings['adapter']}{provider} "
            f"model={settings['model']} thinking={effort}"
        )
    lines.extend([
        "",
        "Plan inputs",
        f"  Roadmap: {_path_label(roadmap)}",
        f"  Authoring guide: {_path_label(guide)}",
        "",
        "Installed CLIs (presence only; authentication and models not tested)",
    ])
    for executable in ("git", "gh", "codex", "pi"):
        lines.append(f"  {executable}: {shutil.which(executable) or 'N/A'}")
    log_dir = (
        config.logging.directory if config.logging.directory else
        state_dir / "logs" if state_dir else None
    )
    lines.extend([
        "",
        "Logging",
        f"  stdout={str(config.logging.stdout).lower()} files={str(config.logging.files).lower()} "
        f"level={config.logging.level}",
        f"  Directory: {log_dir or 'N/A'}",
        "",
        "Workflow status (saved checkpoints; read-only)",
        f"  State directory: {state_dir or 'N/A'}",
    ])
    state, state_status = _read_state(state_dir)
    lines.append(f"  Saved state: {state_status}")
    if roadmap is None:
        lines.extend([
            "  Last PR: N/A", "  Last step: N/A", "  Last stage: N/A",
            "  Last disposition: N/A", "  Next PR: N/A (roadmap not configured)",
        ])
        return "\n".join(lines) + "\n"
    if not roadmap.is_file():
        lines.extend([
            "  Last PR: N/A", "  Last step: N/A", "  Last stage: N/A",
            "  Last disposition: N/A", "  Next PR: N/A (roadmap missing)",
        ])
        return "\n".join(lines) + "\n"
    try:
        roadmap_config = parse_roadmap(roadmap.read_text(encoding="utf-8"))
        validate_roadmap(roadmap_config)
    except (OSError, UnicodeError, RoadmapFormatError, StopWorkflow):
        lines.extend([
            "  Roadmap parsing: INVALID", "  Last PR: N/A", "  Last step: N/A",
            "  Last stage: N/A", "  Last disposition: N/A",
            "  Next PR: N/A (invalid roadmap)",
        ])
        return "\n".join(lines) + "\n"
    prs = roadmap_config["prs"]
    lines.append(f"  Roadmap PRs: {len(prs)}")
    if state is None and state_status.startswith("INVALID"):
        lines.extend([
            "  Last PR: N/A", "  Last step: N/A", "  Last stage: N/A",
            "  Last disposition: N/A", "  Next PR: N/A (invalid saved state)",
        ])
        return "\n".join(lines) + "\n"
    entries = state["prs"] if state else {}
    last_id, last_step, last_stage, outcome = _last_checkpoint(prs, entries)
    lines.extend([
        f"  Last PR: {last_id}",
        f"  Last step: {last_step}",
        f"  Last stage: {last_stage}",
        f"  Last disposition: {outcome}",
    ])
    if state is not None:
        extra = set(entries) - {pr["id"] for pr in prs}
        if extra:
            lines.extend([
                "  Input consistency: INVALID (unknown saved PR identifiers)",
                "  Next PR: N/A (state reconciliation required)",
            ])
            return "\n".join(lines) + "\n"
        try:
            inputs = (
                ("roadmap_sha256", _file_sha256(roadmap)),
                ("guide_sha256", _file_sha256(guide)),
                ("configuration_sha256", config.sha256),
            )
        except OSError:
            lines.append("  Input consistency: INVALID (file unreadable)")
            lines.append("  Next PR: N/A (state reconciliation required)")
            return "\n".join(lines) + "\n"
        changed = [name.removesuffix("_sha256") for name, digest in inputs
                   if state.get(name) != digest]
        if changed:
            lines.append("  Input consistency: CHANGED (" + ", ".join(changed) + ")")
            lines.append("  Next PR: N/A (state reconciliation required)")
            return "\n".join(lines) + "\n"
        lines.append("  Input consistency: MATCH")
    if repo is None:
        lines.append("  Next PR: N/A (not a Git checkout)")
    else:
        lines.append(f"  Next PR: {_offline_next(prs, entries)}")
    lines.append("  Note: no agent execution or GitHub merge verification was performed.")
    return "\n".join(lines) + "\n"
