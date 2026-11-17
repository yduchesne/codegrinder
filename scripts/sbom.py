"""Generate and strictly validate the *runtime* CycloneDX SBOM from uv.lock.

No network is used by CycloneDX itself; `uv export --locked` refuses to update
an out-of-date lock. The SBOM is a disposable build artifact, not a source file.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT / ".artifacts/sbom/codegrinder.cdx.json"


class SbomError(RuntimeError):
    """Missing dependencies, malformed BOM, or schema violations."""


def _invoke(argv: list[str], *, stdin: str | None = None) -> str:
    try:
        result = subprocess.run(
            argv, cwd=PROJECT, input=stdin, capture_output=True, text=True, check=False,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SbomError(f"Unable to execute {argv[0]}: {exc}") from exc
    if result.returncode != 0:
        raise SbomError(f"{argv[0]} failed (exit {result.returncode}): {result.stderr[-2000:]}")
    return result.stdout


def _project_identity() -> tuple[str, str]:
    data = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["name"], data["project"]["version"]


def validate_document(raw: str) -> dict[str, Any]:
    try:
        bom: Any = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise SbomError("SBOM is not valid JSON") from exc
    if not isinstance(bom, dict) or bom.get("bomFormat") != "CycloneDX" or bom.get("specVersion") != "1.6":
        raise SbomError("SBOM must use CycloneDX JSON specification 1.6")
    expected_name, expected_version = _project_identity()
    component = bom.get("metadata", {}).get("component", {})
    if not isinstance(component, dict) or (component.get("name"), component.get("version")) != (
        expected_name, expected_version
    ):
        raise SbomError("SBOM must identify the versioned Codegrinder package as its root component")
    if not isinstance(bom.get("components", []), list):
        raise SbomError("SBOM components must be an array")
    from cyclonedx.schema import SchemaVersion
    from cyclonedx.validation.json import JsonStrictValidator

    errors = JsonStrictValidator(SchemaVersion.V1_6).validate_str(raw)
    if errors:
        raise SbomError(f"CycloneDX 1.6 JSON schema validation failed: {errors}")
    return bom


def generate(output: Path) -> None:
    if not (PROJECT / "uv.lock").is_file():
        raise SbomError("Missing uv.lock: run 'uv lock' and commit the lockfile")
    requirements = _invoke([
        "uv", "export", "--locked", "--no-dev", "--no-emit-project",
        "--format", "requirements-txt", "--no-header", "--no-annotate",
    ])
    raw = _invoke([
        "cyclonedx-py", "requirements", "-", "--pyproject", str(PROJECT / "pyproject.toml"),
        "--mc-type", "application", "--spec-version", "1.6",
        "--output-format", "JSON", "--output-reproducible", "--validate",
    ], stdin=requirements)
    validate_document(raw)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(raw if raw.endswith("\n") else raw + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CycloneDX 1.6 SBOM from uv's locked runtime dependencies")
    commands = parser.add_subparsers(dest="command", required=True)
    gen = commands.add_parser("generate", help="Generate and schema-validate an SBOM")
    gen.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    validate = commands.add_parser("validate", help="Strictly validate an existing CycloneDX SBOM")
    validate.add_argument("--input", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            generate(args.output)
            print(f"SBOM_GENERATED_AND_VALIDATED: {args.output}")
        elif args.command == "validate":
            validate_document(args.input.read_text(encoding="utf-8"))
            print(f"SBOM_VALID: {args.input}")
        return 0
    except (SbomError, OSError, ImportError, ValueError, KeyError) as exc:
        print(f"SBOM_STOP: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
