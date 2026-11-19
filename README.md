# Codegrinder

Codegrinder is an installable Python package for executing a **versioned pull-request roadmap** through a deterministic, resumable workflow. It uses pluggable step interfaces: Codex plans, Pi codes, Codex independently reviews; the controller owns PR ordering, Git operations, CI gates, review/fixup cycles, and durable state. The framework is a **pilot**, not production-validated for unsupervised use.


## Installation from GitHub

Codegrinder is distributed directly from GitHub through `uv`. **No PyPI publication or registry credentials are needed.** After the current feature PR has been merged to `main`:

```bash
uv python install 3.14
uv tool install --python 3.14 "git+https://github.com/yduchesne/codegrinder.git@main"
grind --help
```

Until the feature PR is merged, install from its branch explicitly:

```bash
uv tool install --python 3.14 \
  "git+https://github.com/yduchesne/codegrinder.git@feat/prflow-orchestrator"
```

For reproducible versions, install a **release tag after that tag has been created** (for example, `@v0.1.0`). A Git-tag install is preferred for ordinary users; `@main` is for following development. `uv` installs the `grind` CLI in an isolated tool environment. Ensure the tool executable directory reported by `uv tool dir --bin` is on `PATH`.

Codegrinder requires Python 3.14+ and, for actual workflow execution, independently installed and authenticated `git`, `gh`, `codex`, and `pi` CLIs. An installation does **not** set up external agents or their credentials. See [CLI installation and authentication](docs/INSTALLATION.md#external-cli-installation-and-authentication), including official upstream links, and the [first-run checklist](docs/GETTING_STARTED.md). The existing `./install.sh` is for contributors building from a source checkout (development dependencies, pre-commit, and an editable tool install), **not** for ordinary users. Workflows are run from the target project's Git checkout.

## Documentation

**New to Codegrinder?** Start with [Getting started](docs/GETTING_STARTED.md):
install the CLIs, authenticate GitHub/Codex/Pi, prepare a small roadmap and
authoring guide, configure `grind.toml`, run `grind explain`, then review
your first real PR. For failures, use
[Troubleshooting](docs/TROUBLESHOOTING.md).

- [Getting started](docs/GETTING_STARTED.md) — practical, complete first-PR walkthrough and official CLI references
- [Installation](docs/INSTALLATION.md) — `uv` installation plus each external CLI's installation, authentication, upgrades and uninstall
- [Configuration](docs/CONFIGURATION.md) — `grind.toml`, models, providers, defaults, paths, overrides and logging
- [Troubleshooting](docs/TROUBLESHOOTING.md) — authentication, models, verification failures, recovery and concurrency
- [Architecture](docs/ARCHITECTURE.md) — step outcomes, interfaces, lifecycle, durable state and recovery
- [Releasing](docs/RELEASING.md) — maintainer versioning, CI gates, Git-tag releases and rollback
- [Security and SBOM](docs/SECURITY_AND_SBOM.md) — development checks and supply-chain controls

## CLI

```bash
grind --help                 # Root help and command hierarchy
grind run --help             # Run-specific flags and help

grind run                         # Use [plan.guide] / [plan.roadmap] in ./grind.toml
grind run --pr BM-15              # Execute only BM-15 if no conflicting unfinished work
grind run --pr BM-15 -f           # Request safe supersession and fresh BM-15 planning

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

### PR selection and forced planning

`grind run` preserves automatic crash recovery and runs the normal
dependency-ordered roadmap. With `--pr`, you can select **one exact**
roadmap PR ID, e.g. `grind run --pr BM-15`. PR IDs cannot contain spaces;
`"PR 15"` is invalid. The command stops after that PR merges.

If unfinished or ambiguous work is found, explicit selection stops with
a recovery or selection-conflict warning rather than silently resuming
an unrelated PR. `grind run --pr BM-15 -f` requests controlled
supersession and **new planning** for BM-15, but does not override active
worker safety, unresolved publishing, dependency checks, or an existing
published target PR. Previous files, journals and Git history remain intact;
the new run receives a unique branch and worktree. `-f` alone is invalid.

See [Architecture: explicit PR selection](docs/ARCHITECTURE.md#explicit-pr-selection-and-controlled-forced-planning)
and [Configuration](docs/CONFIGURATION.md#explicit-pr-selection-and-recovery-override).

## Inspect configuration and workflow progress

`grind explain` is read-only. It displays the resolved TOML configuration,
per-step adapter/provider/model and reasoning level, roadmap and authoring guide
paths (or N/A), Git checkout, CLI executable presence, logging destinations,
and saved execution status. It reports the exact `state.json` path even
when the file is not yet created, displaying `N/A` when a state location
cannot be determined. The file's status is reported separately. It shows
the last PR, last step/checkpoint, disposition, and a conservative offline
next-PR candidate.

```bash
grind explain
grind explain --help
grind --conf /path/to/settings.toml explain
grind explain --conf /path/to/settings.toml -r docs/ROADMAP.v1.md -g docs/GUIDE.md
```

Just like `run`, `explain` resolves relative TOML/CLI paths against the
current working directory. Missing optional roadmap or guide configuration
is displayed as N/A, not an error. Unreadable or invalid roadmap/state files
are reported diagnostically.

`explain` **does not run agent preflight**, call GitHub, construct the
orchestrator, update checkpoints, create worktrees, or create log files.
It reads the saved `state.json` without modifying it. Current stages are
checkpoints, **not proof that an interrupted step completed**. A saved
`DONE` with merge evidence is marked as *saved evidence, not reverified*.
When versioned inputs differ, saved PRs are unknown, or completion claims
lack evidence, the next PR is reported as unavailable pending reconciliation
rather than guessed.

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

## Step results and failure classification

Codegrinder records **execution status separately from functional outcome**.
A valid Codex `CHANGES_REQUIRED` review is `SUCCEEDED / NEGATIVE` and
leads to `REPLAN`; a valid `BLOCKED` plan or `STOP` review is
`SUCCEEDED / BLOCKED` and causes a controlled stop. Missing executables,
disk exhaustion, malformed responses and uncertain verification output are
execution failures, not code-quality verdicts. Each stage's structured result
is retained in the workflow state/attempt journal and logged with separate
`execution_status`, `outcome`, `verdict`, and sanitized failure metadata.
See [Architecture](docs/ARCHITECTURE.md#step-execution-status-versus-functional-outcome)
for the result model, error categories, decision matrix and recovery behavior.

## Orchestrator and completed-roadmap reconciliation

The public `Orchestrator` protocol exposes `run() -> int`. The default
`DefaultOrchestrator` receives an immutable `WorkflowContext`, injected
`PlanningStep`, `CodingStep`, and `ReviewStep` implementations, and an
optional `PRSelector`. The CLI constructs those dependencies; it does not
own the lifecycle. The legacy `Controller` remains as a compatibility shim.

`SequentialPRSelector` chooses the first incomplete PR whose declared
predecessors are complete. The roadmap can optionally include one
`### Status` field per PR with `pending`, `planned`, `in_progress`,
`not started`, `DONE`, `complete`, `completed`, or `merged` (case-insensitive,
with whitespace ignored). Completed aliases normalize to `DONE`; other
accepted values normalize to `PENDING`.

**Roadmap status is not merge evidence.** Before `grind run` selects work,
the orchestrator reconciles every roadmap completion label and persisted
completion with a uniquely matching GitHub PR whose branch is
`prflow/<PR-ID>`, whose base matches the roadmap, and whose state is
`MERGED` with a merge timestamp and commit SHA. Missing, ambiguous,
wrong-base, unmerged, or contradictory evidence triggers a hard stop;
completion never silently skips unmerged work. Existing in-progress state is
preserved when a PR is not completed. Roadmap revisions still require
explicit state reconciliation because input hashes are pinned.

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

## Crash recovery and execution checkpoints

Each workflow stage now creates a durable, uniquely identified attempt entry
**before preparing the worktree or invoking an agent**. The execution journal is
stored under `<state-dir>/attempts/<PR-ID>/<attempt-id>.json`, with a current
attempt mirrored in `state.json`. Writes use atomic replacement and fsync of
both the JSON file and its directory. The journal includes the PR, stage,
attempt ID, start/finish time, baseline Git HEAD, a worktree fingerprint of
tracked/staged/untracked changes, process identity when known, and normalized
coding result evidence. It does not store secrets or model-generated text.

On restart, Codegrinder reconciles every saved `RUNNING` attempt before
agent preflight, GitHub merge reconciliation, or selecting another PR:

- Previous process still alive or PID identity uncertain → **STOP**; never
  launch a concurrent worker. Linux process identity uses boot ID and
  process-start ticks to guard against PID reuse, including surviving process
  groups.
- Successful, correctly identified Pi result recorded in the attempt journal
  or written to an attempt-unique result artifact → validate the model,
  attempt ID, repository HEAD and allowed paths, then continue at `VERIFY`.
- Pi exited without a valid result but left in-scope partial edits → retain
  the edits and invoke mandatory `REPLAN` using review-style recovery
  observations. The planner must produce a new validated correction plan
  before coding can resume. Unknown process ownership or edits outside
  permitted paths → **STOP**.
- No Pi edits after the process launched, but no trusted terminal result →
  **STOP**, because external side effects cannot be ruled out.
- Interrupted read-only planning/review, or unchanged deterministic
  verification → safely retry only after validating process exit and the
  unchanged worktree. Previously committed state transitions are reconciled
  using the frozen plan or other stage-specific evidence.
- Interrupted publish → only adopt a GitHub-confirmed PR at the expected
  branch, base, and HEAD; never blindly create a duplicate PR.
- A legacy unfinished checkpoint with no attempt journal → **STOP** for
  explicit migration rather than guessing whether the step ran.

`grind explain` reports the attempt identifier, saved execution status,
recovery disposition, previous process status when known and whether an
unresolved attempt blocks resumption. It remains read-only and does not
repair state itself.

**Safety boundary:** recovery prevents speculative invocation of uncertain
non-idempotent work; it does not claim exactly-once execution of arbitrary
external agent side effects. A process that escapes its tracked process group
or a corrupted local evidence store may require manual investigation.
Tests use synthetic crash windows and mocked agents/GitHub calls, not
live authenticated agent sessions.

## Streaming coding execution and structured results

`CodingStep.execute(CodingTask)` returns an immutable `CodingResult`
instead of `None`. The result records PR and phase identities, adapter/model,
terminal status, exit code, elapsed time, stream-event count, tool-call count,
failed tools, Pi session ID when emitted, and paths to the raw stdout/stderr and
sanitized progress JSONL. `DefaultOrchestrator` validates the result and
durably records it in the PR state **before** advancing to verification.
The Git worktree remains the authoritative code change evidence.

The Pi adapter uses `asyncio.create_subprocess_exec()` to read JSONL stdout
and stderr concurrently, while writing both streams incrementally to
restricted-permission diagnostic files. It emits normalized `CodingProgress`
events (agent/turn lifecycle, tool start/finish, and throttled stream activity)
through the optional `CodingTask.on_progress` callback. Progress deliberately
excludes untrusted model text, tool arguments and tool output. The orchestrator
can display these events immediately without waiting for the coding session
to terminate. Stream frames, aggregate output, and event counts are bounded;
timeout and invalid JSONL fail closed and terminate the subprocess.

The public orchestration method remains synchronous for compatibility.
Asynchronous clients can await `PiAdapter.execute_async(task)` directly,
while `execute(task)` acts as a sync compatibility wrapper. A future
orchestrator-wide asyncio migration can use the awaitable interface without
changing progress or result types.

## Operational logging

Codegrinder uses Python's standard `logging` module, with timestamped
`RotatingFileHandler` outputs. The default location is the persistent XDG
state directory for the selected repo+roadmap:
`${XDG_STATE_HOME:-~/.local/state}/codegrinder/<workflow-id>/logs/`.
Logs are split into `_run/` (preflight, startup, roadmap-level events) and
`<PR-ID>/` (individual PR lifecycle, phase invocation and disposition).
File names include a UTC invocation timestamp and PID, e.g.
`PR-001/grind-20261117T210000.123456Z-1234.log`. Rotated files retain
that timestamp in the basename with `.1`, `.2` suffixes.

Before and after every planning, coding and reviewing adapter invocation,
the orchestrator records an INFO-level `STEP_START`/`STEP_FINISH` with PR,
step name, current state, disposition, and duration. A rejected review
records a successful invocation with a NEGATIVE functional outcome and
transitions to REPLAN; true execution failures are logged separately. Preflight calls are logged in
`_run/`. Sanitized coding progress is logged without prompts, tool
arguments, model responses or raw credentials. Original stdout/stderr traces
remain separately in restricted adapter artifacts and **are not**
automatically included in operational logs.

Defaults are INFO, 10 MiB per file, 5 rotated backups, and both stdout and
file logging enabled. The `stdout` and `files` settings are independent.
Set either to `false` to disable that destination; both cannot be disabled
simultaneously. Stdout output is intended for terminals and pipeline capture.
Diagnostic agent stdout/stderr files are separate and never automatically copied
into operational logs.
To change them, add the following to `grind.toml` (relative directory paths
resolve against the current working directory, just like roadmap paths):

```toml
[logging]
directory = "/tmp/codegrinder/my-project"
level = "INFO"
max_bytes = 10485760
backup_count = 5
stdout = true
files = true
```

A configured directory becomes the workflow's log root; when `files = true`,
Codegrinder creates `_run` and PR subfolders under it. When `files = false`,
no operational log directories or rotating files are created. Avoid sharing one custom root among
different roadmap runs without intentionally handling their combined logs.
Logs are written with file permissions 0600 and per-PR directories 0700.
Rotating log backups do not delete historical logs from prior invocations:
the invocation timestamp distinguishes them. Use your operating system's
retention policy for older runs.

## Agent preflight (before roadmap execution)

`grind run` invokes `preflight(PreflightTask)` on **every configured phase
adapter** (plan, code, review) before PR selection, GitHub merge reconciliation
or any coding/review execution. `PreflightCheck` is a protocol shared by
`PlanningStep`, `CodingStep` and `ReviewStep`; `PreflightResult` reports
a status code without exposing authentication output. Missing preflight support,
malformed results and any failed check stop the entire run.

The Codex adapter checks `codex login status` and sends a minimal, **read-only,
ephemeral model request** using the exact model and reasoning effort configured
for that phase. The Pi adapter checks `pi auth check` with the exact configured
provider and model, then sends a minimal **no-tools, no-session JSONL** request
using the configured model and thinking level. Both adapters verify actual model
responses; because Pi's CLI can resolve fuzzy model names, its preflight also
compares the response's actual provider/model fields against the configured IDs
and rejects substitutions. Listing models or inspecting local credentials alone
does not prove that a model can answer. No fallback model, provider-specific
Codegrinder logic, credential collection or raw CLI authentication-output
logging is allowed.

Preflights use the current harness's configured credentials and consume a
small number of model tokens. A successful preflight is not a guarantee against
later rate limits, revoked credentials or outages: runtime errors still stop
the workflow. For an untrusted project, remember that harness-specific local
extensions/configuration and login-status commands are outside Codegrinder's
sandbox guarantees; use an isolated runner.

## Review rejection and mandatory replanning

An actionable review rejection **never transitions directly to coding**.
For `CHANGES_REQUIRED`, the orchestrator saves the review observations
(`feedback-N.json`), allocates one bounded corrective cycle, then enters
`REPLAN`. The same injected `PlanningStep` receives the previous frozen
plan, the complete feedback history (including current findings), the
current code, the PR requirements and the authoring guide. It must produce a
new, fully specified `plan-fixup-N.md` with a decision matrix and stop
conditions. Only a valid `READY` plan permits `CODE_FIXUP`, which calls the
injected `CodingStep`. Every correction then goes through validation,
publication and independent review again. The next PR stays blocked until
the current PR has a GitHub-confirmed merge.

Deterministic verification failures also route through `REPLAN`, so their
diagnostics are incorporated into an explicit corrective plan rather than
being given to the coding agent as design instructions. A reviewer `STOP`,
invalid review output, adapter failure, impossible corrective plan, or
exhausted `Max-Fixups` budget fails closed. A legacy persisted `FIXUP`
stage from previous versions requires explicit state reconciliation; it
cannot silently bypass the new planning requirement. Every corrective
attempt preserves its own versioned feedback and plan artifacts.

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
