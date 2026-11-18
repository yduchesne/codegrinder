"""Packaged, state-aware supplemental prompt templates.

The framework's authoritative prompt remains unchanged. Only curated package
templates can be appended: <package>/prompts/<step>/*.prompt.txt, or a state
directory (<package>/prompts/<step>/<state>/*.prompt.txt) when it exists.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

PROMPT_ROOT = Path(__file__).resolve().parent / "prompts"
STATE_STEPS = {
    "PLAN": "plan",
    "REPLAN": "plan",
    "CODE": "code",
    "CODE_FIXUP": "code",
    "REVIEW": "review",
}
VARIABLES = frozenset({
    "STATE", "STEP", "PR_ID", "PATH_ROADMAP", "PATH_AUTHORING_GUIDE",
    "PATH_GENERATED_PLAN",
})
_VARIABLE = re.compile(r"\{\{([^{}]*)\}\}")
_MAX_FILE_BYTES = 1024 * 1024
_MAX_TOTAL_BYTES = 4 * 1024 * 1024


class PromptTemplateError(ValueError):
    """Reject unsafe or unrenderable supplemental prompts before agent execution."""


def supplemental_prompt(
    *, step: str, state: str, variables: Mapping[str, str],
    root: Path | None = None,
) -> str:
    """Render selected package templates in lexical filename order.

    An existing state directory takes precedence even if empty. The step
    directory is never combined with it. Directory names are canonical lower
    case and state names retain internal underscores (CODE_FIXUP -> code_fixup).
    """
    if STATE_STEPS.get(state) != step:
        raise PromptTemplateError(f"PROMPT_INVALID_STEP_STATE: {step}/{state}")
    expected = {"STATE", "STEP", "PR_ID", "PATH_ROADMAP", "PATH_AUTHORING_GUIDE"}
    if step == "plan":
        expected.add("PATH_GENERATED_PLAN")
    if set(variables) != expected or any(
        not isinstance(variables[name], str) or not variables[name] for name in expected
    ):
        raise PromptTemplateError("PROMPT_VARIABLES_INVALID")

    base = (root if root is not None else PROMPT_ROOT) / step
    specific = base / state.lower()
    try:
        if specific.is_symlink() or base.is_symlink():
            raise PromptTemplateError("PROMPT_DIRECTORY_SYMLINK")
        if specific.exists():
            directory = specific
        else:
            directory = base
        if not directory.exists():
            return ""
        if not directory.is_dir():
            raise PromptTemplateError(f"PROMPT_NOT_DIRECTORY: {directory}")
        paths = sorted(
            (file for file in directory.iterdir() if file.name.endswith(".prompt.txt")),
            key=lambda file: file.name,
        )
        texts: list[str] = []
        total_bytes = 0
        for file in paths:
            if file.is_symlink() or not file.is_file():
                raise PromptTemplateError(f"PROMPT_NOT_REGULAR_FILE: {file}")
            size = file.stat().st_size
            total_bytes += size
            if size > _MAX_FILE_BYTES or total_bytes > _MAX_TOTAL_BYTES:
                raise PromptTemplateError(f"PROMPT_TOO_LARGE: {file}")
            source = file.read_text(encoding="utf-8")
            def expand(match: re.Match[str]) -> str:
                name = match.group(1)
                if name not in VARIABLES or name not in variables:
                    raise PromptTemplateError(f"PROMPT_UNKNOWN_VARIABLE: {name} in {file}")
                return variables[name]
            expanded = _VARIABLE.sub(expand, source)
            if "{{" in expanded or "}}" in expanded:
                raise PromptTemplateError(f"PROMPT_MALFORMED_VARIABLE: {file}")
            if expanded.strip():
                texts.append(expanded.strip())
        return "\n\n".join(texts)
    except (OSError, UnicodeError) as exc:
        raise PromptTemplateError(f"PROMPT_UNREADABLE: {directory}") from exc


def augment_prompt(
    original: str, *, step: str, state: str, variables: Mapping[str, str],
    root: Path | None = None,
) -> str:
    """Append selected supplemental instructions without replacing baseline text."""
    extra = supplemental_prompt(step=step, state=state, variables=variables, root=root)
    return original + "\n\n" + extra if extra else original
