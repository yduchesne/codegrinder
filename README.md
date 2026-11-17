# Codegrinder

Codegrinder is an installable Python package for executing a **versioned pull-request roadmap** through a deterministic, resumable workflow. It uses pluggable step interfaces: Codex plans, Pi codes, Codex independently reviews; the controller owns PR ordering, Git operations, CI gates, review/fixup cycles, and durable state. The framework is a **pilot**, not production-validated for unsupervised use.

## Installation

Requires Python 3.14+ and the external `git`, `gh`, `codex`, and `pi` CLIs installed and authenticated on the machine that will execute workflows.

```bash
./install.sh               # Installs uv (if needed), Python 3.14, and dev dependencies
uv run grind --help
uv run grind run --help
```

`install.sh` also installs the `grind` executable as a global uv tool, so you can
run it from other Git repositories (ensure `~/.local/bin` is on `PATH`).
`uv` owns the `.venv` and dependencies; `.python-version` pins the project interpreter
to Python 3.14. When `uv` is already installed, use `uv python install 3.14`
followed by `uv sync --group dev`. In CI, the same environment is prepared with
`astral-sh/setup-uv` and `uv sync --group dev`. For a production deployment,
review and commit a generated `uv.lock` to freeze dependency resolution.


Codegrinder runs from within the target project's Git checkout. The package can be installed once and then invoked from other repositories.

## CLI

```bash
grind --help                 # Root help and command hierarchy
grind run --help             # Run-specific flags and help

grind run                         # Use [plan.guide] / [plan.roadmap] in ./grind.toml

grind run \
  --guide docs/DETAILED_PR_PLAN_AUTHORING_GUIDE.md \
  --roadmap docs/ROADMAP.v1.md     # Both explicit paths override TOML

grind --conf /path/to/project-agents.toml run \
  -g docs/DETAILED_PR_PLAN_AUTHORING_GUIDE.md \
  -r docs/ROADMAP.v1.md

# Equivalent configuration placement:
grind run --conf /path/to/project-agents.toml -g guide.md -r roadmap.md
```

`run` processes the entire ordered roadmap, automatically resuming the next incomplete PR. Paths are resolved independently: `-g` / `--guide` overrides `[plan.guide].path`, while `-r` / `--roadmap` overrides `[plan.roadmap].path`. If a flag is absent, its corresponding TOML path is used; if neither is defined, `grind run` stops with an actionable error. Both CLI paths and TOML paths resolve relative to the current working directory when relative; absolute paths are used as supplied (both accept `~` expansion). The `--conf` file can be located anywhere: its directory does **not** change the base for `[plan.roadmap].path` or `[plan.guide].path`. The roadmap must use the *strict versioned Markdown format* shown in [`examples/roadmap.v1.md`](examples/roadmap.v1.md), not an unstructured prose roadmap.

## Agent configuration (`grind.toml`)

Configuration discovery is explicit and predictable:

1. Without `--conf`, Codegrinder loads `./grind.toml` from the **current working directory** if it exists; otherwise the built-in defaults apply.
2. With `grind --conf PATH` (or `grind run --conf PATH`), Codegrinder loads exactly that file. Any file name is accepted; it does not have to be `grind.toml`.
3. An explicitly specified but missing file, malformed TOML, unknown setting, unsupported adapter, or invalid value is a **hard STOP**. There is no silent adapter/model fallback.

See the checked-in [`grind.toml.example`](grind.toml.example). Copy it to your working directory to customize:

```toml
[plan.roadmap]
path = "docs/ROADMAP.v1.md"

[plan.guide]
path = "docs/DETAILED_PR_PLAN_AUTHORING_GUIDE.md"

[steps.plan]
adapter = "codex"
model = "gpt-6-sol"
reasoning_effort = "high"

[steps.code]
adapter = "pi"
provider = "deepseek"
model = "deepseek-flash"
thinking = "high"

[steps.review]
adapter = "codex"
model = "gpt-6-sol"
reasoning_effort = "high"
```

Step adapter settings inherit the defaults when omitted. `plan.roadmap` and `plan.guide` have no built-in path defaults: if absent, their paths must be supplied on the CLI. A configured table must have exactly one string key, `path`; nonexistent files are rejected when selected for execution. A CLI override does not require the overridden TOML path to exist. The Codex CLI model ID for **GPT-6 Sol** is `gpt-6-sol`, *not* `gpt-6`; DeepSeek's current Flash API model ID is `deepseek-flash` (not the compatibility alias `deepseek-v4-flash`). Pi selects the model via its own `--provider` and `--model` options. You must ensure that **your installed, authenticated Codex and Pi harnesses expose these exact model IDs**; configurations must use IDs valid for the selected harness, not display names. Codegrinder checks identifier syntax and passes exact IDs to the relevant CLI, stopping on an invocation error. It cannot infer account-specific model entitlements and will never replace an unavailable model with another one automatically. Use `pi --list-models` and Codex's model picker to check local availability before your first run.

Only `codex` is supported for planning/review and `pi` for coding in this first version. Protocols allow additional adapters without redesigning the controller. `grind.toml` may be committed to each target repository to version the agent selections; the tracked `.example` is the public configuration template. Store API credentials in the harnesses' own authenticated mechanisms, **not** in the TOML file.

## Steps and contracts

`src/codegrinder/interfaces.py` defines `PlanningStep`, `CodingStep`, and `ReviewStep` protocols, with immutable task data classes. `src/codegrinder/adapters/` houses the Codex and Pi implementations. The engine depends on those protocols, not specific agent CLI APIs; `config.py` is the adapter factory/validation boundary.

1. Select the first PR whose dependencies are complete; prepare an isolated Git worktree.
2. **Plan** using Codex in a read-only sandbox, with the frozen detailed plan authoring guide; validate schema and required plan headings.
3. **Code** using Pi, restricted by a plan, allowed paths, and a decision matrix. The coding agent cannot create the PR or merge it.
4. **Verify** changed-file scope and configured deterministic test commands.
5. **Publish** the branch/PR via GitHub CLI and independently **review** at the frozen commit SHA using Codex.
6. **Fix up** via Pi when necessary, within a limited budget; repeat verification and review.
7. **Wait for merge** and required checks. Only GitHub-confirmed merges advance the roadmap.

The entire run is pinned to SHA-256 hashes of the guide, roadmap, and (when present) TOML configuration. Changes to any of those inputs or to the adapter identities between resumptions stop the run for reconciliation. Runtime state, plans, session traces, and logs reside outside the checkout under `${XDG_STATE_HOME:-~/.local/state}/codegrinder/<stable-repo-roadmap-id>/`. A lock blocks concurrent execution of the same workflow.

The roadmap controls manual vs protected auto-merge and specifies required CI check names. See the example's `Merge-Mode` and `Required-Checks`. A missing required CI check never counts as a pass.

## Safety and limitations

- **Unattended operation is not production-validated.** Run a disposable-repository integration pilot before enabling `Merge-Mode: auto`.
- Codex planning/review use CLI read-only sandboxing, but Pi coding currently runs with the runner's filesystem permissions. Isolate the runner/container and keep GitHub credentials out of the coding agent's environment.
- The coding agent is instructed not to commit/push, and the controller checks changed paths; this is **not** an OS-level sandbox or credential boundary.
- Model IDs are checked for format, but actual model availability is provider/account-specific; a model rejected by the underlying harness produces a stop, not substitution.
- Planning and review outputs are validated; not all semantic requirements can be mechanically verified.

## Tests

```bash
uv sync --group dev
uv run ruff check src tests
uv run mypy src/codegrinder
uv run pytest     # Enforces >=85% statement/branch-aware coverage
```

GitHub Actions runs under Python 3.14, verifying the installed `grind`
entry point, Ruff, mypy, and coverage enforcement. The `pytest` configuration
fails the build whenever total Codegrinder source coverage falls below 85%.
 Integration tests with actual Codex/Pi/GitHub and credentials are intentionally not part of public CI.

## Pre-commit security policy and CycloneDX SBOM

Run `uv run --no-sync pre-commit run --all-files` to execute Ruff, mypy, Bandit,
Semgrep (vendored rules), Gitleaks (official pinned hook), and CycloneDX SBOM
generation/schema validation. The first Gitleaks hook installation needs
Go 1.24.11 or newer. CI also scans the entire Git history for secrets, enforces
85% unit-test coverage, and uploads the validated runtime SBOM as an artifact.

The SBOM is generated from `uv.lock` using a locked, production-only export:

```bash
uv lock # commit uv.lock alongside dependency changes
uv run --no-sync python scripts/sbom.py generate --output .artifacts/sbom/codegrinder.cdx.json
uv run --no-sync python scripts/sbom.py validate --input .artifacts/sbom/codegrinder.cdx.json
```

See [docs/SECURITY_AND_SBOM.md](docs/SECURITY_AND_SBOM.md) for the full policy.
