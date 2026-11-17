"""Strict, optional TOML configuration for phase-specific agent adapters.

Configuration is loaded from ./grind.toml by default, or from an explicit
--conf path. An absent default file uses the documented default adapters.
"""
from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .adapters import CodexAdapter, PiAdapter
from .interfaces import PlanningStep, CodingStep, ReviewStep

MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
CODEX_EFFORTS = {"low", "medium", "high", "xhigh"}
PI_EFFORTS = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
DEFAULTS = {
    "plan": {"adapter": "codex", "model": "gpt-6-sol", "reasoning_effort": "high"},
    "code": {"adapter": "pi", "provider": "deepseek", "model": "deepseek-flash", "thinking": "high"},
    "review": {"adapter": "codex", "model": "gpt-6-sol", "reasoning_effort": "high"},
}


class ConfigurationError(ValueError):
    """Invalid configuration, refused without silent fallback."""


@dataclass(frozen=True)
class GrindConfig:
    source: Path | None
    sha256: str | None
    plan: dict[str, str]
    code: dict[str, str]
    review: dict[str, str]
    roadmap_path: Path | None
    guide_path: Path | None

    def adapters(self) -> tuple[PlanningStep, CodingStep, ReviewStep]:
        """Construct phase adapters; the orchestration engine remains vendor-neutral."""
        return (
            CodexAdapter(model=self.plan["model"], reasoning_effort=self.plan["reasoning_effort"]),
            PiAdapter(provider=self.code["provider"], model=self.code["model"], thinking=self.code["thinking"]),
            CodexAdapter(model=self.review["model"], reasoning_effort=self.review["reasoning_effort"]),
        )


def _nonempty_str(value: object, path: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ConfigurationError(f"CONFIG_INVALID: {path} must be a non-empty string without surrounding whitespace")
    return value


def load_config(path: Path | None = None, *, cwd: Path | None = None) -> GrindConfig:
    """Load optional CWD grind.toml, or require an explicit --conf file."""
    working_dir = (cwd if cwd is not None else Path.cwd()).resolve()
    explicit = path is not None
    source = (path.expanduser() if path is not None else working_dir / "grind.toml").resolve()
    if not source.is_file():
        if explicit:
            raise ConfigurationError(f"CONFIG_MISSING: {source}")
        data: dict = {}
        raw: bytes | None = None
        source_path: Path | None = None
    else:
        try:
            raw = source.read_bytes()
            data = tomllib.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ConfigurationError(f"CONFIG_INVALID: cannot parse TOML {source}: {exc}") from exc
        source_path = source
    allowed_top_level = {"steps", "plan"}
    if set(data) - allowed_top_level:
        raise ConfigurationError(f"CONFIG_INVALID: unknown top-level keys: {sorted(set(data) - allowed_top_level)}")
    plan_files = data.get("plan", {})
    if not isinstance(plan_files, dict):
        raise ConfigurationError("CONFIG_INVALID: plan must be a TOML table")
    if set(plan_files) - {"roadmap", "guide"}:
        raise ConfigurationError(f"CONFIG_INVALID: unknown plan settings: {sorted(set(plan_files) - {'roadmap', 'guide'})}")

    def plan_path(name: str) -> Path | None:
        entry = plan_files.get(name)
        if entry is None:
            return None
        if not isinstance(entry, dict) or set(entry) != {"path"}:
            raise ConfigurationError(f"CONFIG_INVALID: plan.{name} must contain only a path string")
        value = _nonempty_str(entry["path"], f"plan.{name}.path")
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = working_dir / candidate
        return candidate.resolve()

    roadmap_path = plan_path("roadmap")
    guide_path = plan_path("guide")
    steps = data.get("steps", {})
    if not isinstance(steps, dict):
        raise ConfigurationError("CONFIG_INVALID: steps must be a TOML table")
    if set(steps) - set(DEFAULTS):
        raise ConfigurationError(f"CONFIG_INVALID: unknown phases: {sorted(set(steps) - set(DEFAULTS))}")
    resolved: dict[str, dict[str, str]] = {}
    for phase, base in DEFAULTS.items():
        supplied = steps.get(phase, {})
        if not isinstance(supplied, dict):
            raise ConfigurationError(f"CONFIG_INVALID: steps.{phase} must be a TOML table")
        extra = set(supplied) - set(base)
        if extra:
            raise ConfigurationError(f"CONFIG_INVALID: unknown steps.{phase} settings: {sorted(extra)}")
        values = dict(base)
        for key, value in supplied.items():
            values[key] = _nonempty_str(value, f"steps.{phase}.{key}")
        if values["adapter"] != base["adapter"]:
            raise ConfigurationError(f"CONFIG_UNSUPPORTED_ADAPTER: {phase}: {values['adapter']!r}")
        if not MODEL_ID.fullmatch(values["model"]):
            raise ConfigurationError(f"CONFIG_INVALID_MODEL_ID: steps.{phase}.model {values['model']!r}")
        if phase == "code":
            if not MODEL_ID.fullmatch(values["provider"]):
                raise ConfigurationError("CONFIG_INVALID_PROVIDER: steps.code.provider")
            if values["thinking"] not in PI_EFFORTS:
                raise ConfigurationError("CONFIG_INVALID_THINKING: steps.code.thinking")
        elif values["reasoning_effort"] not in CODEX_EFFORTS:
            raise ConfigurationError(f"CONFIG_INVALID_REASONING: steps.{phase}.reasoning_effort")
        resolved[phase] = values
    return GrindConfig(
        source=source_path,
        sha256=hashlib.sha256(raw).hexdigest() if raw is not None else None,
        plan=resolved["plan"], code=resolved["code"], review=resolved["review"],
        roadmap_path=roadmap_path, guide_path=guide_path,
    )
