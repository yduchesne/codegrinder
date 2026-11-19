"""Explicit PR selection and controlled, audited supersession.

Never clean/reset/delete a worktree, reuse a branch for a forced restart, or
ignore a possibly live worker. All operator choices are scoped by the
workflow's existing single-runner lock.
"""
from __future__ import annotations

import copy
import hashlib
import re

from .recovery import process_status
from .selection import is_completed


class SelectionOverrideError(ValueError):
    """Selection override is unsafe or violates the roadmap contract."""


def selected_pr(config: dict, state: dict, pr_id: str, *, check_dependencies: bool = True) -> dict:
    """Validate exact PR ID, completion, and direct dependency merge evidence."""
    if not isinstance(pr_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", pr_id):
        raise SelectionOverrideError("PR_SELECTION_INVALID_ID: use the exact roadmap PR ID (no spaces)")
    matches = [pr for pr in config["prs"] if pr["id"] == pr_id]
    if len(matches) != 1:
        raise SelectionOverrideError(f"PR_SELECTION_NOT_FOUND: {pr_id}")
    pr = matches[0]
    entry = state["prs"].get(pr_id, {})
    if is_completed(pr.get("status")) or is_completed(entry.get("stage")):
        raise SelectionOverrideError(f"PR_ALREADY_COMPLETED: {pr_id}")
    if check_dependencies:
        for parent in pr["depends_on"]:
            evidence = state["prs"].get(parent, {})
            if not is_completed(evidence.get("stage")):
                raise SelectionOverrideError(f"PR_DEPENDENCIES_UNSATISFIED: {pr_id} depends on {parent}")
            if not (evidence.get("merged_at") and evidence.get("merge_commit")):
                raise SelectionOverrideError(f"PR_DEPENDENCIES_UNVERIFIED: {parent}")
    return pr


def selection_conflicts(config: dict, state: dict, selected_id: str) -> list[dict]:
    """Find evidence requiring recovery or explicit supersession before selection."""
    conflicts: list[dict] = []
    known = {pr["id"] for pr in config["prs"]}
    for pid, ent in state["prs"].items():
        if pid not in known:
            raise SelectionOverrideError(f"PR_SELECTION_FOREIGN_STATE: {pid}")
        if not isinstance(ent, dict):
            raise SelectionOverrideError(f"PR_SELECTION_INVALID_STATE: {pid}")
        if is_completed(ent.get("stage")) or ent.get("superseded_by"):
            continue
        record = ent.get("execution")
        status = record.get("status") if isinstance(record, dict) else None
        stage = ent.get("stage", "PLAN")
        # Reached a durable terminal stage, with no attempt to recover, or an
        # attempt that failed before its state was advanced.
        legacy_unjournaled = status is None and stage not in {"PLAN", "DONE", "STOPPED"}
        unresolved = status in {"RUNNING", "FAILED"} or stage == "STOPPED" or legacy_unjournaled
        # Only the selected PR may resume from a legitimate nonterminal stage.
        busy_other = pid != selected_id and (stage != "PLAN" or status is not None)
        if unresolved or busy_other:
            conflicts.append({
                "pr_id": pid, "stage": stage, "status": status or "UNRECORDED",
                "attempt_id": record.get("id") if isinstance(record, dict) else None,
                "recovery": unresolved,
            })
    return conflicts


def override_warning(selected_id: str, conflicts: list[dict]) -> str:
    problem = "RECOVERY_REQUIRED" if any(x["recovery"] for x in conflicts) else "PR_SELECTION_CONFLICT"
    details = ", ".join(
        f"{x['pr_id']} stage={x['stage']} status={x['status']} attempt={x['attempt_id'] or 'N/A'}"
        for x in conflicts
    )
    return (
        f"{problem}: requested {selected_id}; unfinished execution: {details}. "
        f"Run 'grind run' to reconcile the existing workflow, or "
        f"'grind run --pr {selected_id} -f' to request a controlled fresh PLAN. "
        f"Force never bypasses active processes, dependencies or Git safety checks."
    )


def assess_force_safety(orch, pr_id: str, conflicts: list[dict]) -> None:
    """Read-only preflight before any journal/state/branch mutation."""
    from .engine import git
    target = orch.state["prs"].get(pr_id, {})
    # A previous PR on the same roadmap ID cannot be silently abandoned; it
    # might still be open or may have been merged after our last checkpoint.
    if (target.get("pr_number") or target.get("published_head") or
            target.get("stage") in {"PUBLISH", "REVIEW", "WAIT_MERGE"}):
        raise SelectionOverrideError(f"FORCE_EXISTING_TARGET_PR: {pr_id} requires GitHub reconciliation")
    for conflict in conflicts:
        pid = conflict["pr_id"]
        entry = orch.state["prs"][pid]
        record = entry.get("execution")
        if not isinstance(record, dict):
            if entry.get("stage") not in {"PLAN", "STOPPED", "WAIT_MERGE", "REVIEW", "REPLAN", "VERIFY", "CODE", "CODE_FIXUP"}:
                raise SelectionOverrideError(f"FORCE_LEGACY_STATE_UNSAFE: {pid}")
            # Legacy nonterminal state has no process identity or journal.
            if entry.get("stage") not in {"PLAN", "STOPPED"}:
                raise SelectionOverrideError(f"FORCE_LEGACY_STATE_UNSAFE: {pid}")
            continue
        status = record.get("status")
        if status == "RUNNING":
            # Non-agent stages may have started untracked Git/GitHub or check
            # subprocesses. A RUNNING record cannot prove their completion.
            if record.get("stage") not in {"PLAN", "REPLAN", "CODE", "CODE_FIXUP", "REVIEW"}:
                raise SelectionOverrideError(f"FORCE_UNTRACKED_PROCESS: {pid}")
            launch = record.get("launch_state")
            identity = record.get("process")
            if identity is not None:
                process = process_status(identity)
                if process != "EXITED":
                    raise SelectionOverrideError(f"FORCE_PROCESS_{process}: {pid}")
            elif launch != "NOT_STARTED":
                raise SelectionOverrideError(f"FORCE_PROCESS_UNKNOWN: {pid}")
            # Do not bypass an absent or corrupted recovery audit journal.
            path = orch._attempt_path(pid, record.get("id", ""))
            if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024 * 1024:
                raise SelectionOverrideError(f"FORCE_JOURNAL_INVALID: {pid}")
            from .engine import load_json
            audit = load_json(path)
            if audit.get("id") != record.get("id") or audit.get("stage") != record.get("stage"):
                raise SelectionOverrideError(f"FORCE_JOURNAL_MISMATCH: {pid}")
        elif status not in {"SUCCEEDED", "FAILED", "INTERRUPTED", "RECOVERED", "SUPERSEDED"}:
            raise SelectionOverrideError(f"FORCE_STATUS_INVALID: {pid}")
    # A target's old worktree may contain valuable partial edits. Force always
    # creates a new worktree and branch, without touching any old worktree.
    serial = orch.state.get("override_counter", 0)
    if type(serial) is not int or serial < 0:
        raise SelectionOverrideError("FORCE_COUNTER_INVALID")
    branch = f"prflow/{pr_id}-force-{serial + 1:08d}"
    if git(orch.repo, "branch", "--list", branch).strip():
        raise SelectionOverrideError(f"FORCE_BRANCH_EXISTS: {branch}")
    if git(orch.repo, "ls-remote", "--heads", "origin", branch).strip():
        raise SelectionOverrideError(f"FORCE_REMOTE_BRANCH_EXISTS: {branch}")
    if (orch.state_dir / "worktrees" / f"{pr_id}-force-{serial + 1:08d}").exists():
        raise SelectionOverrideError("FORCE_WORKTREE_EXISTS")


def commit_force_selection(orch, pr_id: str, conflicts: list[dict]) -> None:
    """Create audited supersession before launching a new selected PLAN.

    The prior target state is saved as an immutable, digest-referenced snapshot;
    journaled RUNNING attempts on other PRs become SUPERSEDED only after their
    processes have been verified exited.
    """
    from .engine import atomic_json, utcnow
    assess_force_safety(orch, pr_id, conflicts)
    serial = orch.state.get("override_counter", 0) + 1
    label = f"{pr_id}-force-{serial:08d}"
    branch = f"prflow/{label}"
    at = utcnow()
    archive = orch.state_dir / "overrides" / pr_id / f"{serial:08d}.json"
    prior_target = orch.state["prs"].get(pr_id)
    if prior_target is not None:
        atomic_json(archive, copy.deepcopy(prior_target))
        archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    else:
        archive_hash = None
    metadata = {
        "pr_id": pr_id, "generation": serial, "branch": branch,
        "worktree_name": label, "at": at,
        "prior_target_sha256": archive_hash,
        "superseded_prs": [item["pr_id"] for item in conflicts],
    }
    for conflict in conflicts:
        pid = conflict["pr_id"]
        if pid == pr_id:
            continue
        entry = orch.state["prs"][pid]
        entry["superseded_by"] = metadata
        entry["stopped_reason"] = "OPERATOR_FORCE_PR_SELECTION"
        entry["stage"] = "STOPPED"
        record = entry.get("execution")
        if isinstance(record, dict) and record.get("status") == "RUNNING":
            record["status"] = "SUPERSEDED"
            record["superseded_at"] = at
            record["superseded_by"] = metadata
    previous_counter = prior_target.get("attempt_counter", 0) if isinstance(prior_target, dict) else 0
    if type(previous_counter) is not int or previous_counter < 0:
        raise SelectionOverrideError("FORCE_ATTEMPT_COUNTER_INVALID")
    orch.state["prs"][pr_id] = {
        "stage": "PLAN", "fixups": 0, "history": [],
        "attempt_counter": previous_counter,
        "branch": branch, "worktree_name": label, "artifacts_name": label,
        "selection_override": metadata,
    }
    orch.state["override_counter"] = serial
    orch.state.setdefault("override_events", []).append(metadata)
    orch.save()
    # The checkpoint is the authority after a crash. Per-attempt journals are
    # reconciled after the atomic state commit; old data is never deleted.
    for conflict in conflicts:
        if conflict["pr_id"] == pr_id:
            # The target has a new entry; retain the old attempt in its
            # immutable archive AND mark the old attempt journal superseded.
            previous = prior_target.get("execution") if isinstance(prior_target, dict) else None
            if isinstance(previous, dict) and previous.get("status") == "RUNNING":
                previous = copy.deepcopy(previous)
                previous["status"] = "SUPERSEDED"
                previous["superseded_at"] = at
                previous["superseded_by"] = metadata
                atomic_json(orch._attempt_path(pr_id, previous["id"]), previous)
            continue
        entry = orch.state["prs"][conflict["pr_id"]]
        record = entry.get("execution")
        if isinstance(record, dict) and record.get("status") == "SUPERSEDED":
            atomic_json(orch._attempt_path(conflict["pr_id"], record["id"]), record)
    orch.logs.emit(
        "PR_SELECTION_OVERRIDE", pr_id=pr_id, state="PLAN",
        disposition="SUCCEEDED",
    )
    orch.logs.emit(
        "RECOVERY_OVERRIDE_APPROVED", pr_id=pr_id, state="PLAN",
        disposition="SUCCEEDED",
    )
    if conflicts:
        orch.logs.emit(
            "ATTEMPT_SUPERSEDED", pr_id=pr_id, state="PLAN",
            disposition="SUCCEEDED",
        )
