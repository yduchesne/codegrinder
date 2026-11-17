"""Standard-library, per-run and per-PR rotating execution logs.

Logs contain lifecycle metadata, not prompts, model output, tool arguments,
subprocess stderr or authentication data. Raw adapter traces remain separate.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


_TOKEN = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")


@dataclass(frozen=True)
class LoggingSettings:
    """Workflow logging settings; directory None means XDG-backed state logs."""

    directory: Path | None = None
    level: str = "INFO"
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5
    stdout: bool = True
    files: bool = True

    def __post_init__(self) -> None:
        if self.level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("LOGGING_INVALID_LEVEL")
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= 2**31:
            raise ValueError("LOGGING_INVALID_MAX_BYTES")
        if type(self.backup_count) is not int or not 0 <= self.backup_count <= 100:
            raise ValueError("LOGGING_INVALID_BACKUP_COUNT")
        if type(self.stdout) is not bool:
            raise ValueError("LOGGING_INVALID_STDOUT")
        if type(self.files) is not bool:
            raise ValueError("LOGGING_INVALID_FILES")
        if not (self.stdout or self.files):
            raise ValueError("LOGGING_NO_DESTINATIONS")


class _UTCFormatter(logging.Formatter):
    @staticmethod
    def converter(timestamp: float | None = None) -> time.struct_time:
        return time.gmtime(timestamp)


def _token(value: object) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        return "UNSPECIFIED"
    return value


def safe_error_code(exc: Exception) -> str:
    """Only the leading machine-readable error code; never exception details."""
    match = re.match(r"^([A-Z][A-Z0-9_]{2,79})(?::|\b)", str(exc))
    return match.group(1) if match else type(exc).__name__.upper()[:64]


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """Keep active and rotated files private even without a process-wide umask."""

    def _open(self):
        flags = os.O_CREAT | os.O_WRONLY | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.baseFilename, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            return os.fdopen(fd, "a", encoding=self.encoding, errors=self.errors)
        except BaseException:
            os.close(fd)
            raise


class WorkflowEventLogger:
    """Own handlers per workflow invocation, with one rotating log per PR."""

    def __init__(self, state_dir: Path, settings: LoggingSettings | None = None):
        self.settings = settings or LoggingSettings()
        self.root = (
            self.settings.directory if self.settings.directory is not None
            else state_dir / "logs"
        ).resolve()
        self.stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        self._suffix = f"grind-{self.stamp}-{os.getpid()}.log"
        self._loggers: dict[str, logging.Logger] = {}
        self._handlers: list[logging.Handler] = []
        self._closed = False
        self._format = _UTCFormatter(
            "%(asctime)sZ %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
        )

    def _get(self, pr_id: str | None) -> logging.Logger:
        if self._closed:
            raise RuntimeError("LOGGING_CLOSED")
        label = _token(pr_id) if pr_id is not None else "_run"
        if label in self._loggers:
            return self._loggers[label]
        logger = logging.Logger(
            f"codegrinder.{self.stamp}.{label}", level=getattr(logging, self.settings.level)
        )
        logger.propagate = False
        if self.settings.files:
            folder = self.root / label
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            folder.chmod(0o700)
            filename = folder / self._suffix
            handler = _PrivateRotatingFileHandler(
                filename, maxBytes=self.settings.max_bytes,
                backupCount=self.settings.backup_count, encoding="utf-8",
            )
            filename.chmod(0o600)
            handler.setFormatter(self._format)
            logger.addHandler(handler)
            self._handlers.append(handler)
        if self.settings.stdout:
            stream = logging.StreamHandler(sys.stdout)
            stream.setFormatter(self._format)
            logger.addHandler(stream)
            self._handlers.append(stream)
        self._loggers[label] = logger
        return logger

    def emit(
        self, event: str, *, pr_id: str | None = None,
        step: str = "workflow", state: str = "UNSPECIFIED",
        disposition: str = "UNSPECIFIED", level: str = "INFO",
        duration_ms: int | None = None, error_code: str | None = None,
    ) -> None:
        fields = (
            f"event={_token(event)} pr={_token(pr_id) if pr_id else '_run'} "
            f"step={_token(step)} state={_token(state)} "
            f"disposition={_token(disposition)}"
        )
        if duration_ms is not None:
            fields += f" duration_ms={max(0, int(duration_ms))}"
        if error_code:
            fields += f" error_code={_token(error_code)}"
        logger = self._get(pr_id)
        logger.log(getattr(logging, level), fields)

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        for logger in self._loggers.values():
            for handler in logger.handlers[:]:
                logger.removeHandler(handler)
                handler.flush()
                handler.close()
        self._handlers.clear()
        self._loggers.clear()
        self._closed = True
