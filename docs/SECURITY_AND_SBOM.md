# Codegrinder quality, security, and SBOM policy

Codegrinder requires Python 3.14 and `uv`. Development tools are installed in
`uv`'s managed virtual environment. The same `.pre-commit-config.yaml` is invoked
locally and in CI; CI separately scans *all reachable Git history* with Gitleaks.

## Bootstrapping

1. Install Go **1.24.11 or newer** (needed to build the pinned Gitleaks hook).
2. Run `./install.sh` (installs `uv` if necessary, Python 3.14, project, and hooks).
3. Run `uv run --no-sync pre-commit run --all-files` before opening a PR.
4. Run `uv run --no-sync pytest` to enforce the **85% branch-aware coverage** floor.

The pre-commit checks are intentionally read-only with respect to tracked source.
They run even when a commit stages only a non-Python file. A failed check blocks
the commit; developers must resolve findings rather than bypass them.

| Gate | Scope | Failure policy |
| --- | --- | --- |
| Ruff | Package, tests, scripts | Any lint error fails |
| mypy | Package | Any type error fails |
| Bandit | Package and scripts | Medium+ severity **and** confidence fail |
| Semgrep | Package and scripts, committed local Python rules | Findings fail (no remote ruleset downloads) |
| Gitleaks | Staged changes locally; entire Git history in CI | Any detected secret fails |
| SBOM | Production dependencies from `uv.lock` | Locked export, generation, structural and strict schema validation required |
| pytest | Unit and mock-adapter integration tests | At least 85% statement+branch-aware coverage |

## SBOM generation and validation

The `cyclonedx-bom` CLI does not directly ingest `uv.lock`. Instead, the SBOM
script exports the locked **runtime** dependency graph with:

```sh
uv export --locked --no-dev --no-emit-project --format requirements-txt --no-header --no-annotate
```

It passes that export directly to `cyclonedx-py requirements -` and supplies
`--pyproject pyproject.toml` to record Codegrinder as the root application.

```sh
uv run --no-sync python scripts/sbom.py generate --output .artifacts/sbom/codegrinder.cdx.json
uv run --no-sync python scripts/sbom.py validate --input .artifacts/sbom/codegrinder.cdx.json
```

`--output-reproducible` excludes generation-only randomness and timestamps.
Generation and validation fail closed if the lock is absent or out of date, the
external programs fail, the project root is omitted, or the CycloneDX 1.6 strict
JSON schema fails validation. Dev tools are excluded from the *runtime* BOM.
The output lives under ignored `.artifacts/` and is uploaded by CI as a retained
artifact. Developers should commit `uv.lock` (not the generated SBOM).

## Security boundaries

Pre-commit hooks can be bypassed locally (`--no-verify`), so GitHub CI must
always be required before merge. Repository administrators should configure
branch protection accordingly. Pre-commit scans are **not** a sandbox: the Pi
worker still needs process/filesystem isolation and limited secrets exposure.
A passing scanner does not prove software is free of vulnerabilities. Update
pinned security tooling and rules via a reviewed dependency-change PR.
