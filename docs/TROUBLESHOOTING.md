# Troubleshooting Codegrinder

Use this guide when `grind` cannot start, an agent cannot authenticate,
the workflow produces a negative verdict, or crash recovery stops. Start
with `grind explain` **from the target Git checkout**; it reads saved
configuration/checkpoints without contacting GitHub or executing agents.
For a complete initial setup, see [Getting started](GETTING_STARTED.md).

> **Safety first:** Codegrinder is an integration pilot, not a
> production-validated unattended runner. Do not delete the state directory,
> attempt journals, agent artifacts, or a Git lock to force a retry. Do not
> paste credentials, raw agent output, private prompts, or full logs into
> public bug reports.

## Quick diagnostic sequence

```bash
# Run these inside the repository the workflow is meant to modify.
git rev-parse --show-toplevel
git remote -v
git status --short

grind --help
grind explain

gh auth status
codex login status

command -v uv
command -v git
command -v gh
command -v codex
command -v pi
```

If a command is absent from `PATH`, install the matching CLI and open a
new terminal. Do **not** run a second `grind run` while another instance
is working on the same repository/roadmap. See
[Installation](INSTALLATION.md#external-cli-installation-and-authentication)
for upstream references and compatible setup procedures.

## Installation and configuration errors

| Error or symptom | Meaning | Safe response |
| --- | --- | --- |
| `grind: command not found` | Tool executable not on `PATH` or `uv tool install` not completed | Run `uv tool list`, `uv tool dir --bin`, and `uv tool update-shell`; restart the shell and use [uv's tool documentation](https://docs.astral.sh/uv/guides/tools/) |
| `GUIDE_UNSPECIFIED`, `ROADMAP_UNSPECIFIED` | Neither matching TOML path nor CLI override was supplied | Provide both `[plan.*].path` values or use `grind run -g ... -r ...` |
| `GUIDE_MISSING`, `ROADMAP_MISSING` | Resolved input path does not exist | Check `pwd`; relative paths resolve against the **working directory**, not the TOML file |
| `CONFIG_...` / invalid TOML or unknown keys | Configuration does not satisfy strict schema | Compare with [configuration reference](CONFIGURATION.md) and `grind.toml.example`; correct typos, then rerun `grind explain` |
| `ROADMAP_INVALID` | Roadmap syntax or required metadata/fields invalid | Compare with [roadmap example](../examples/roadmap.v1.md); keep exact headings, list syntax and one `#` title |
| `REPO_INVALID` | Not in intended Git checkout / missing Git repository | Change into correct checkout and verify `git rev-parse --show-toplevel` |
| `ANOTHER_RUNNER_ACTIVE` | Nonblocking workflow lock is held | Do not remove lock; let the first runner finish, or inspect whether it has terminated and recover deliberately |
| `CONFIG_CHANGED`, `ROADMAP_CHANGED`, `GUIDE_CHANGED`, `AGENT_CONFIG_CHANGED` | An input/agent identity differs from the frozen workflow checkpoint | Restore the original inputs or reconcile the workflow; do not overwrite state to bypass integrity checks |

`grind explain` can show `N/A` for missing optional paths; this does
not mean `grind run` will accept those paths. Similarly, a clean
`grind explain` is **not** a credential or connectivity test.

## Authentication and agent preflight errors

Codegrinder uses independent external agents; installing the Python
package never authenticates them.

| Error | Likely boundary | Diagnostic and corrective action |
| --- | --- | --- |
| `HARNESS_NOT_INSTALLED` | `codex` or `pi` binary missing | `command -v codex` / `command -v pi`, then follow official install guides |
| `AUTH_NOT_READY` | Agent's own auth probe reports unavailable credentials | For Codex run `codex login` and `codex login status`. For Pi, use interactive `pi` then `/login`, or configure the chosen provider's API key, as documented by Pi |
| `MODEL_PROBE_FAILED` / `MODEL_PROBE_UNVERIFIED` | Authenticated CLI did not produce the exact expected model response | Verify provider/model exists and is authorized, check rate limits/network access, review configured model ID and thinking level; preflight is fail-closed |
| `MODEL_SELECTION_MISMATCH` | Pi responded with a different provider/model than requested | Choose an **exact** available ID; Pi can resolve fuzzy names, but Codegrinder refuses substitutions |
| `MODEL_PROBE_TIMEOUT` | Preflight agent probe timed out | Inspect connectivity/provider availability and system load; do not assume coding failed |
| `HARNESS_EXECUTION_FAILED` | Agent subprocess could not execute | Check executable permissions, working environment, and OS diagnostics |
| `PI_INVALID_JSONL`, `PI_INCOMPLETE`, `CODING_REPORT_...` | Agent returned malformed/incomplete terminal evidence | Inspect private attempt artifacts securely, check Pi distribution/version compatibility, and reconcile before retrying |
| `CODEX_INVALID_JSON`, `CODEX_MISSING_OUTPUT`, `PLAN_SCHEMA_INVALID`, `REVIEW_SCHEMA_INVALID` | Structured agent response invalid or absent | Inspect the schema-contract mismatch; do not fabricate a plan or verdict |

### Pi CLI compatibility and authentication

**Important:** Codegrinder currently invokes `pi auth check --provider
<provider> --model <model> --json` during preflight. Verify that the Pi
**build you installed** actually implements these options using its own
`pi --help` or relevant command help. Pi's upstream interactive
`/login` procedure does not, by itself, establish support for
Codegrinder's noninteractive preflight command. If this command is
unsupported, expect a preflight stop: record the incompatibility and
resolve it in the adapter or choose a verified compatible Pi build,
**rather than disabling the preflight gate**.

After interactive authentication, check the model/provider IDs with
Pi's `/model` selector. For a DeepSeek API-key configuration, follow
the current [Pi provider guide](https://docs.pi.dev/latest/providers).
For other Pi invocation flags and JSONL event output, see the
[Pi CLI reference](https://docs.pi.dev/latest/cli) and
[JSON event documentation](https://docs.pi.dev/latest/json).

Codex's official [authentication guide](https://developers.openai.com/codex/auth/)
covers browser sign-in, API-key sign-in, and `codex login status`.
Codex planning/review requires read-only structured-output behavior
and the exact model selected in `grind.toml`.

## Negative verdict is not an execution error

These must not be conflated:

| Saved result | What it means | Next action |
| --- | --- | --- |
| `SUCCEEDED / NEGATIVE / CHANGES_REQUIRED` | Independent review successfully identified actionable defects | Codegrinder persists review findings and enters `REPLAN` |
| `SUCCEEDED / NEGATIVE / CHECKS_FAILED` | A check ran and recognizable test/linter evidence rejected the implementation | Corrective plan, then coding fixup, subject to fixup budget |
| `SUCCEEDED / BLOCKED / BLOCKED` | Planner successfully returned a blocker | Resolve missing/conflicting evidence before another run |
| `SUCCEEDED / BLOCKED / STOP` | Reviewer produced a valid stop assessment | Operator investigation; no automatic coding |
| `SUCCEEDED / BLOCKED / REQUIRED_CI_FAILED` | GitHub reported a failed or canceled required check | Inspect that check on GitHub; its verdict alone does not prove source or environment failure |
| `FAILED` with category `ENVIRONMENT` | Command unavailable, disk full, timeout, permissions, etc. | Repair environment, then follow recovery procedure |
| `FAILED` with category `PROTOCOL` | Untrusted/invalid response or ambiguous check | Investigate contract and evidence; do not let coding guess |
| `INTERRUPTED` | No reliable functional outcome established | Recovery checks determine whether safe to continue |

The workflow itself may stop with CLI exit code **2** for either a
controlled blocking verdict or an execution error. Inspect
`grind explain` and the persisted structured step result to distinguish
the two; exit code alone is not the step-outcome taxonomy.

## Verification and GitHub PR issues

| Error | Meaning and response |
| --- | --- |
| `VERIFY_ENVIRONMENT_FAILURE` | Check command failed with an environmental signature, e.g. `command not found`, disk exhaustion or signal termination. Repair host/dependencies; **do not** ask the coding agent to fix an unrelated environment |
| `VERIFY_CHECK_UNCLASSIFIED` | Check returned nonzero without recognizable diagnostic evidence. Review the check logs and improve its reporting before continuing |
| `MAX_FIXUPS_REACHED` | Bounded correction budget exhausted. Review code, findings and policy manually; do not quietly increase the budget mid-run |
| `NO_REQUIRED_CI_CONFIGURED` | Roadmap lacks a usable CI gate. Set the names of real required GitHub checks; don't use a fabricated placeholder |
| `REQUIRED_CI_FAILED` | At least one required GitHub check reports fail/cancel. Inspect its GitHub job logs; distinguish actual test rejection from CI infrastructure failure |
| `CI_STATUS_UNAVAILABLE`, `CI_STATUS_INVALID` | GitHub check query unavailable/invalid. Check `gh auth status`, repository access, network, and required checks before retry |
| `PUBLISHED_SHA_OR_BASE_MISMATCH`, `PR_HEAD_CHANGED_AFTER_REVIEW` | Git branch/base no longer matches recorded review evidence. Stop; inspect remote PR and reconcile manually |
| PR remains at `WAIT_MERGE` | Normal if the PR is still open: check required CI and merge manually if configured. Do not launch a duplicate runner |

`Required-Checks` refers to the **reported GitHub check name**, while
each `### Checks` item is a locally executed shell command. These are
two separate validation gates. Refer to the
[GitHub CLI PR checks reference](https://cli.github.com/manual/gh_pr_checks)
and the target repository's CI workflow.

## Crash recovery and concurrent execution

The state root is normally
`${XDG_STATE_HOME:-~/.local/state}/codegrinder/<workflow-id>/`,
where the ID is derived from the **resolved repository and roadmap
paths**. `grind explain` prints the resolved `state.json` location.

If a run was killed mid-step, Codegrinder records a journaled
`RUNNING` attempt and may require recovery reconciliation before any
new agent call. `RECOVERY_...`, `UNRECONCILED_ATTEMPT`, or
`FAILED_ATTEMPT_REQUIRES_RECONCILIATION` mean that the operation
cannot be blindly repeated. Preserve state and artifacts. Inspect the
saved PR branch, worktree, GitHub PR head, and attempt-specific journal
before any manual repair.

Different roadmaps in the **same repository** currently have different
workflow locks and can conflict; running two independent `grind run`
instances is not safely supported just because their lock files differ.
See [Architecture: Concurrency](ARCHITECTURE.md#concurrency-running-several-grind-instances)
and [Restart and crash recovery](ARCHITECTURE.md#restart-and-crash-recovery).

## Official external CLI reference

| Tool | Authoritative upstream reference |
| --- | --- |
| `uv` | [Installation](https://docs.astral.sh/uv/getting-started/installation/), [managing tools](https://docs.astral.sh/uv/guides/tools/) |
| `git` | [Installing Git](https://git-scm.com/book/en/v2/Getting-Started-Installing-Git), [command reference](https://git-scm.com/docs) |
| `gh` | [Manual](https://cli.github.com/manual/), [authentication status](https://cli.github.com/manual/gh_auth_status) |
| `codex` | [Codex CLI](https://developers.openai.com/codex/cli/), [authentication](https://developers.openai.com/codex/auth/) |
| `pi` | [Quickstart](https://docs.pi.dev/latest/quickstart), [providers](https://docs.pi.dev/latest/providers), [CLI](https://docs.pi.dev/latest/cli) |
| `grind` | [Configuration](CONFIGURATION.md), [Architecture](ARCHITECTURE.md) |
