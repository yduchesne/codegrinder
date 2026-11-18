"""Durable, attempt-scoped coding reports passed as untrusted review context.

An agent's final text is not verification evidence: callers must independently
review the Git diff, tests, and requirements. Only validated reports from the
corresponding terminal coding attempt can be handed to a reviewer or recovered.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from pathlib import Path

from .interfaces import CodingResult

MAX_REPORT_BYTES = 256 * 1024
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CodingReportError(ValueError):
    """Invalid, missing, or non-recoverable coding report evidence."""


def report_path(artifacts: Path, *, phase: str, fixup_number: int, attempt_id: str) -> Path:
    if phase not in {"code", "fixup"} or type(fixup_number) is not int or fixup_number < 0:
        raise CodingReportError("CODING_REPORT_IDENTITY_INVALID")
    if not isinstance(attempt_id, str) or (attempt_id and not _ID.fullmatch(attempt_id)):
        raise CodingReportError("CODING_REPORT_IDENTITY_INVALID")
    suffix = f"-{attempt_id}" if attempt_id else ""
    return artifacts / f"{phase}-{fixup_number}{suffix}.report.md"


def extract_final_report(assistant: dict) -> str:
    """Extract only final assistant text blocks; never include tool/thinking output."""
    if not isinstance(assistant, dict):
        raise CodingReportError("CODING_REPORT_MESSAGE_INVALID")
    blocks = assistant.get("content")
    if not isinstance(blocks, list):
        raise CodingReportError("CODING_REPORT_CONTENT_INVALID")
    paragraphs: list[str] = []
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise CodingReportError("CODING_REPORT_CONTENT_INVALID")
        if block["type"] != "text":
            continue
        value = block.get("text")
        if not isinstance(value, str):
            raise CodingReportError("CODING_REPORT_CONTENT_INVALID")
        if value.strip():
            paragraphs.append(value.strip())
    report = "\n\n".join(paragraphs)
    if not report:
        raise CodingReportError("CODING_REPORT_EMPTY")
    if len(report.encode("utf-8")) > MAX_REPORT_BYTES:
        raise CodingReportError("CODING_REPORT_TOO_LARGE")
    return report


def persist_report(path: Path, report: str) -> str:
    """Atomically publish private UTF-8 report bytes and return their digest."""
    data = report.encode("utf-8")
    if not data.strip() or len(data) > MAX_REPORT_BYTES:
        raise CodingReportError("CODING_REPORT_INVALID_SIZE")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as writer:
            writer.write(data)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(data).hexdigest()


def verified_report(
    result: CodingResult, artifacts: Path, *, pr_id: str, phase: str,
    fixup_number: int, attempt_id: str,
) -> str:
    """Validate report provenance, digest, length and file type, then return text.

    Expected path is derived independently, never taken from the agent's result.
    Both normal review and recovery use this single validation boundary.
    """
    expected = report_path(artifacts, phase=phase, fixup_number=fixup_number, attempt_id=attempt_id)
    if (result.pr_id != pr_id or result.phase != phase or
            result.attempt_id != (attempt_id or None) or
            result.status != "COMPLETED" or result.exit_code != 0):
        raise CodingReportError("CODING_REPORT_IDENTITY_MISMATCH")
    if result.final_report_path != str(expected):
        raise CodingReportError("CODING_REPORT_PATH_MISMATCH")
    if not isinstance(result.final_report_sha256, str) or not _SHA256.fullmatch(result.final_report_sha256):
        raise CodingReportError("CODING_REPORT_DIGEST_INVALID")
    try:
        # O_NOFOLLOW and fstat prevent a symlink substituted at read time from
        # escaping the expected artifact path.
        fd = os.open(expected, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as reader:
            info = os.fstat(reader.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size < 1 or info.st_size > MAX_REPORT_BYTES:
                raise CodingReportError("CODING_REPORT_FILE_INVALID")
            data = reader.read(MAX_REPORT_BYTES + 1)
    except OSError as exc:
        raise CodingReportError("CODING_REPORT_UNREADABLE") from exc
    if not data or len(data) > MAX_REPORT_BYTES:
        raise CodingReportError("CODING_REPORT_FILE_INVALID")
    if hashlib.sha256(data).hexdigest() != result.final_report_sha256:
        raise CodingReportError("CODING_REPORT_DIGEST_MISMATCH")
    try:
        report = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CodingReportError("CODING_REPORT_ENCODING_INVALID") from exc
    if not report.strip():
        raise CodingReportError("CODING_REPORT_EMPTY")
    return report
