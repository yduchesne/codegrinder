"""Strict, deterministic reader for versioned Codegrinder Markdown roadmaps.

The format is intentionally narrow: unknown fields and missing sections stop
execution instead of being interpreted by a model.
"""
from __future__ import annotations

import re


class RoadmapFormatError(ValueError):
    """Roadmap does not match the documented Markdown contract."""


_METADATA = {
    "Version", "Roadmap-Revision", "Base-Branch",
    "Command-Timeout-Seconds", "Max-Fixups", "Merge-Mode", "Required-Checks",
}
_SECTIONS = {"Goal", "Dependencies", "Acceptance", "Allowed Paths", "Checks"}
_PR_HEADER = re.compile(r"^##\s+(?P<id>[A-Za-z0-9][A-Za-z0-9._-]{0,63}):\s+(?P<title>.+?)\s*$")
_META_LINE = re.compile(r"^(?P<key>[A-Za-z][A-Za-z-]*):\s*(?P<value>.*?)\s*$")


def _items(lines: list[str], label: str, *, allow_none: bool = False) -> list[str]:
    items: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if not line.startswith("- ") or not line[2:].strip():
            raise RoadmapFormatError(f"ROADMAP_INVALID: {label} requires '- item' lines")
        items.append(line[2:].strip())
    if allow_none and items == ["none"]:
        return []
    if "none" in items:
        raise RoadmapFormatError(f"ROADMAP_INVALID: {label}: 'none' must stand alone")
    if not items and not allow_none:
        raise RoadmapFormatError(f"ROADMAP_INVALID: {label} must not be empty")
    if len(items) != len(set(items)):
        raise RoadmapFormatError(f"ROADMAP_INVALID: {label} contains duplicates")
    return items


def _required_int(meta: dict[str, str], key: str) -> int:
    value = meta.get(key, "")
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise RoadmapFormatError(f"ROADMAP_INVALID: {key} must be a positive integer")
    return int(value)


def parse_roadmap(text: str) -> dict:
    """Return the existing controller's dict contract from a Markdown roadmap.

    Headings must use exactly one H1 document title, H2 PR headings, and the
    five required H3 PR fields. Machine-critical properties are never inferred.
    """
    if not isinstance(text, str) or not text.strip():
        raise RoadmapFormatError("ROADMAP_INVALID: empty roadmap")
    lines = text.replace("\r\n", "\n").split("\n")
    leading: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    for line in lines:
        if line.startswith("## "):
            current = []
            sections.append((line, current))
        elif current is None:
            leading.append(line)
        else:
            current.append(line)
    if not sections:
        raise RoadmapFormatError("ROADMAP_INVALID: no PR headings")

    meta: dict[str, str] = {}
    checks_lines: list[str] = []
    seen_title = False
    reading_checks = False
    for raw in leading:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# ") and not seen_title:
            seen_title = True
            continue
        if not seen_title:
            raise RoadmapFormatError("ROADMAP_INVALID: document must start with '# Title'")
        match = _META_LINE.fullmatch(line)
        if match:
            key, value = match.group("key"), match.group("value")
            if key not in _METADATA or key in meta:
                raise RoadmapFormatError(f"ROADMAP_INVALID: unknown or duplicate metadata: {key}")
            if key == "Required-Checks" and value:
                raise RoadmapFormatError("ROADMAP_INVALID: Required-Checks must use a list")
            if key != "Required-Checks" and not value:
                raise RoadmapFormatError(f"ROADMAP_INVALID: empty {key}")
            meta[key] = value
            reading_checks = key == "Required-Checks"
        elif reading_checks:
            checks_lines.append(raw)
        else:
            raise RoadmapFormatError(f"ROADMAP_INVALID: unexpected metadata line: {line}")
    for key in _METADATA:
        if key not in meta:
            raise RoadmapFormatError(f"ROADMAP_INVALID: missing metadata {key}")
    if meta["Version"] != "1":
        raise RoadmapFormatError("ROADMAP_INVALID: supported Version is 1")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", meta["Roadmap-Revision"]):
        raise RoadmapFormatError("ROADMAP_INVALID: invalid Roadmap-Revision")
    required_checks = _items(checks_lines, "Required-Checks")

    prs: list[dict] = []
    for header, body in sections:
        match = _PR_HEADER.fullmatch(header)
        if not match:
            raise RoadmapFormatError(f"ROADMAP_INVALID: invalid PR heading {header!r}")
        fields: dict[str, list[str]] = {}
        active: str | None = None
        for raw in body:
            if raw.startswith("### "):
                key = raw[4:].strip()
                if key not in _SECTIONS or key in fields:
                    raise RoadmapFormatError(f"ROADMAP_INVALID: unknown or duplicate PR field {key!r}")
                fields[key] = []
                active = key
            elif active is None:
                if raw.strip():
                    raise RoadmapFormatError(f"ROADMAP_INVALID: text outside PR field in {match['id']}")
            else:
                if raw.startswith("#"):
                    raise RoadmapFormatError(f"ROADMAP_INVALID: unexpected nested heading in {match['id']}")
                fields[active].append(raw)
        if set(fields) != _SECTIONS:
            raise RoadmapFormatError(f"ROADMAP_INVALID: {match['id']} requires fields {sorted(_SECTIONS - set(fields))}")
        goal = "\n".join(x.strip() for x in fields["Goal"] if x.strip()).strip()
        if not goal or goal.startswith("- "):
            raise RoadmapFormatError(f"ROADMAP_INVALID: {match['id']}: Goal must be prose")
        prs.append({
            "id": match["id"], "title": match["title"], "goal": goal,
            "depends_on": _items(fields["Dependencies"], f"{match['id']} Dependencies", allow_none=True),
            "acceptance": _items(fields["Acceptance"], f"{match['id']} Acceptance"),
            "allowed_paths": _items(fields["Allowed Paths"], f"{match['id']} Allowed Paths"),
            "checks": _items(fields["Checks"], f"{match['id']} Checks"),
        })
    return {
        "version": 1,
        "revision": meta["Roadmap-Revision"],
        "base_branch": meta["Base-Branch"],
        "command_timeout_seconds": _required_int(meta, "Command-Timeout-Seconds"),
        "max_fixups": _required_int(meta, "Max-Fixups"),
        "merge_mode": meta["Merge-Mode"],
        "required_checks": required_checks,
        "prs": prs,
    }
