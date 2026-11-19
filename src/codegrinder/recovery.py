"""Deterministic, fail-closed recovery evidence for interrupted PR attempts.

Only metadata and cryptographic digests are persisted; no source content,
model prompts, environment variables or credentials are written here.
"""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Callable


class RecoveryError(RuntimeError):
    """Recovery cannot safely infer the previous invocation's outcome."""


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            h.update(block)
    return h.hexdigest()


def worktree_snapshot(worktree: Path, git_fn: Callable) -> dict:
    """Fingerprint tracked edits, staged files and untracked files, without following symlinks."""
    head = git_fn(worktree, "rev-parse", "HEAD")
    porcelain = git_fn(worktree, "status", "--porcelain", "--untracked-files=all")
    changed = git_fn(worktree, "ls-files", "-z", "--modified", "--others", "--exclude-standard")
    staged = git_fn(worktree, "diff", "--cached", "--name-only", "-z")
    paths = sorted(set(filter(None, changed.split("\0") + staged.split("\0"))))
    if len(paths) > 50000:
        raise RecoveryError("RECOVERY_TOO_MANY_CHANGED_FILES")
    digest = hashlib.sha256()
    for name in paths:
        if name.startswith("/") or ".." in Path(name).parts or "\0" in name:
            raise RecoveryError("RECOVERY_UNSAFE_PATH")
        path = worktree / name
        # Parent symlinks must never let a recovery fingerprint read outside
        # its worktree, even when the final path component is regular.
        try:
            path.resolve(strict=False).relative_to(worktree.resolve(strict=True))
        except (OSError, ValueError):
            raise RecoveryError("RECOVERY_PATH_ESCAPES_WORKTREE") from None
        digest.update(name.encode("utf-8", errors="surrogateescape") + b"\0")
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            digest.update(b"DELETED")
            continue
        if stat.S_ISLNK(metadata.st_mode):
            digest.update(b"SYMLINK" + os.readlink(path).encode("utf-8", errors="surrogateescape"))
        elif stat.S_ISREG(metadata.st_mode):
            digest.update(b"FILE" + bytes.fromhex(file_digest(path)))
        else:
            raise RecoveryError("RECOVERY_UNSUPPORTED_FILE_TYPE")
    # Include porcelain for renames / mode changes not visible in content digest.
    digest.update(porcelain.encode("utf-8", errors="surrogateescape"))
    return {
        "head": head,
        "dirty": bool(porcelain.strip()),
        "fingerprint": digest.hexdigest(),
        "path_count": len(paths),
    }


def process_identity(pid: int) -> dict:
    """Bind a PID to one Linux boot/process start to avoid PID reuse confusion."""
    value: dict = {"pid": pid, "boot_id": None, "start_ticks": None}
    if type(pid) is not int or pid <= 0:
        return value
    try:
        value["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        statline = Path(f"/proc/{pid}/stat").read_text()
        # comm may contain spaces and ')' characters.
        value["start_ticks"] = statline.rsplit(") ", 1)[1].split()[19]
    except (OSError, IndexError, ValueError):
        pass
    return value


def process_status(identity: object) -> str:
    """ACTIVE, EXITED, or UNKNOWN: UNKNOWN always requires a hard stop."""
    if not isinstance(identity, dict):
        return "UNKNOWN"
    pid = identity.get("pid")
    if type(pid) is not int or pid <= 0:
        return "UNKNOWN"
    if not identity.get("boot_id") or not identity.get("start_ticks"):
        return "UNKNOWN"
    try:
        stat_path = Path(f"/proc/{pid}/stat")
        if not stat_path.exists():
            # A child may outlive its session leader. Pi starts a new
            # process session/group; never resume while that group survives.
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return "EXITED"
            except (PermissionError, OSError):
                return "UNKNOWN"
            return "ACTIVE"
        current = process_identity(pid)
    except (OSError, ValueError):
        return "UNKNOWN"
    if current.get("boot_id") is None or current.get("start_ticks") is None:
        return "UNKNOWN"
    if (current["boot_id"], current["start_ticks"]) != (
        identity["boot_id"], identity["start_ticks"]
    ):
        return "EXITED"
    return "ACTIVE"


def validate_attempt(record: object, pid: str) -> dict:
    """Reject corrupt/foreign/malformed saved attempt evidence before recovery."""
    if not isinstance(record, dict) or record.get("pr_id") != pid:
        raise RecoveryError("RECOVERY_ATTEMPT_INVALID")
    if record.get("status") not in {"RUNNING", "SUCCEEDED", "FAILED", "INTERRUPTED", "RECOVERED", "SUPERSEDED"}:
        raise RecoveryError("RECOVERY_STATUS_INVALID")
    if not isinstance(record.get("stage"), str):
        raise RecoveryError("RECOVERY_STAGE_INVALID")
    attempt_id = record.get("id")
    if not isinstance(attempt_id, str) or not attempt_id.startswith(pid + "-"):
        raise RecoveryError("RECOVERY_ATTEMPT_ID_INVALID")
    return record
