# Getting started: from a fresh machine to your first PR

This is the operator walkthrough for Codegrinder, a **pilot** that plans with
Codex, implements with Pi, validates, publishes a GitHub pull request, and
independently reviews it with Codex. It is **not yet validated for unattended
production use**. The instructions target a Linux shell and an existing,
writable GitHub repository. Set up and test on a disposable branch/repository
before running against valuable code.

**Path through the documentation:** [Installation](INSTALLATION.md) covers
`uv` and all external CLIs; [Configuration](CONFIGURATION.md) documents the
full TOML contract; [Troubleshooting](TROUBLESHOOTING.md) maps failures to
remedies; [Architecture](ARCHITECTURE.md) covers recovery and concurrency.

## 1. Install the required command-line programs

Codegrinder itself is a Python tool installed with `uv`, but it **does not
install** Git, GitHub CLI, Codex CLI, or Pi for you.

| CLI | Purpose | Official documentation |
| --- | --- | --- |
| `uv` | Install Python 3.14 and the isolated `grind` tool | [uv installation](https://docs.astral.sh/uv/getting-started/installation/), [tool management](https://docs.astral.sh/uv/guides/tools/) |
| `git` | Find the checkout, manage branches/worktrees, commit and push | [Git installation](https://git-scm.com/book/en/v2/Getting-Started-Installing-Git), [Git documentation](https://git-scm.com/docs) |
| `gh` | Create/query GitHub PRs and check merges/CI | [GitHub CLI](https://cli.github.com/manual/), [authentication](https://cli.github.com/manual/gh_auth_login) |
| `codex` | Read-only plan and independent review | [Codex CLI](https://developers.openai.com/codex/cli/), [authentication](https://developers.openai.com/codex/auth/) |
| `pi` | Coding agent with structured JSON output | [Pi quickstart](https://docs.pi.dev/latest/quickstart), [CLI reference](https://docs.pi.dev/latest/cli), [providers](https://docs.pi.dev/latest/providers) |
| `grind` | Codegrinder's installed command | [Installation](INSTALLATION.md), [CLI/configuration](CONFIGURATION.md) |

Follow each upstream installation guide. For example, on a Linux host
that already has Git and GitHub CLI:

```bash
# Install uv using its official instructions first.
uv python install 3.14

# Until PR #1 is merged, use this explicit feature-branch source.
uv tool install --python 3.14 \
  "git+https://github.com/yduchesne/codegrinder.git@feat/prflow-orchestrator"

grind --help
```

After PR #1 merges, install from `@main` or a **published** release tag;
do not assume an example tag exists. Use `uv tool update-shell` if the `grind`
executable is not on `PATH`, then open a new terminal.

## 2. Authenticate each remote service and verify the selected models

```bash
gh auth login
gh auth status

codex login
codex login status

pi --version
pi
```

Inside Pi, use `/login` to select a supported provider (or follow Pi's
[provider documentation](https://docs.pi.dev/latest/providers) for an API
key). Use `/model` to inspect the models available **to your account**;
exit the interactive session afterward. If using the default
`provider = "deepseek"`, configure its API key/credentials according to
your installed Pi version's provider documentation.

**Authentication is separate for `gh`, Codex, and Pi**; a ChatGPT login
does not automatically authenticate GitHub or a Pi API-key provider. Never
paste API keys into the roadmap, Git repository, `grind.toml`, a screenshot,
or diagnostic logs. Do not run `gh auth token` to troubleshoot basic setup.

Confirm that both Codex and Pi support the **exact model IDs** you intend
to configure. The shipped example values are defaults, not a guarantee that
those models exist or are available to your account. Codegrinder never
silently substitutes a different model.

Codegrinder preflight additionally invokes `codex login status`, a
read-only Codex model probe, and Pi's provider/model authentication and JSON
probe. Verify that your installed Pi distribution supports the specific
`pi auth check --provider ... --model ... --json` interface expected by
Codegrinder. A generic successful `pi` interactive login does **not**
guarantee this compatibility; see [Troubleshooting](TROUBLESHOOTING.md#pi-cli-compatibility-and-authentication).

## 3. Choose a writable target Git repository

Open a separate, already cloned repository (not necessarily the Codegrinder
source repository). It must have a configured `origin`, a valid baseline
branch (normally `main`), and GitHub access that permits creating PRs.

```bash
cd /path/to/your-target-repository
git rev-parse --show-toplevel
git remote -v
gh repo view --json nameWithOwner
git status --short
```

Do not proceed if you cannot identify the intended repository, if its
baseline is unavailable, or if `gh` cannot access it. Codegrinder creates
separate `prflow/<PR-ID>` branches and Git worktrees; it should not be
started against a repository with unexpected existing branches of those
names.

## 4. Create a narrow roadmap and a planning guide

Prepare two Markdown files **in your target repository**. The exact,
versioned roadmap grammar is demonstrated by
[examples/roadmap.v1.md](../examples/roadmap.v1.md). The following
single-PR exercise is intentionally small:

```bash
mkdir -p docs
cat > docs/ROADMAP.v1.md <<'EOF'
# First Codegrinder Roadmap

Version: 1
Roadmap-Revision: v1
Base-Branch: main
Command-Timeout-Seconds: 3600
Max-Fixups: 2
Merge-Mode: manual
Required-Checks:
- REPLACE_WITH_REAL_GITHUB_CHECK_NAME

## PR-001: Add a repository introduction note

### Status
pending

### Goal
Add docs/CODEGRINDER_PILOT.md explaining the purpose of this repository and how a developer can navigate its existing documentation. Do not change executable code.

### Dependencies
- none

### Acceptance
- docs/CODEGRINDER_PILOT.md exists and has a meaningful introduction.
- The contents are supported by existing repository evidence.

### Allowed Paths
- docs/CODEGRINDER_PILOT.md

### Checks
- test -s docs/CODEGRINDER_PILOT.md
EOF
```

**Before execution**, replace `REPLACE_WITH_REAL_GITHUB_CHECK_NAME`
with the exact name of a **real, required CI check** that reports on PRs
in your target repository. The `Checks` shell command validates the local
changes; `Required-Checks` is a **GitHub check name**, not a shell command.
The example does not create a GitHub Actions workflow. If the repository
doesn't have the required CI, configure one first or choose another repository
for the pilot. Do **not** put a fabricated name or weaken an existing gate
merely to pass the workflow.

Create a real authoring guide for your coding agent. A minimal starting point
is below; strengthen it for the actual repository:

```bash
cat > docs/DETAILED_PR_PLAN_AUTHORING_GUIDE.md <<'EOF'
# Detailed PR Plan Authoring Guide

Read relevant source, tests and architecture documentation before planning.
Do not invent an API, dependency, file, tool or check without verifying it.

Return an implementation plan with exactly these headings:
## Scope
## Steps
## Tests
## Decision Matrix
## Stop Conditions

Specify concrete files, intended behavior, ordered edits and test commands.
For every uncertain condition, state a deterministic decision with observable
evidence and a safe action. Unknown or conflicting evidence means STOP, not
permission to improvise. Keep all edits inside the roadmap's allowed paths.
EOF
```

The guide should contain all project-specific architecture and acceptance
rules the planning agent needs. A short guide alone is not proof that an
unattended agent will make sound decisions.

**Baseline visibility:** Codegrinder creates its PR worktree from
`origin/main` (or the roadmap's configured base branch), not from
uncommitted files in your current checkout. Review and commit the relevant
roadmap, authoring guide, and repository documentation to the intended
baseline through your normal Git/PR process **before starting** if the
agent needs to read them as files. The orchestrator reads the roadmap and
guide from their configured paths, but those files do not automatically
appear in a separate worktree unless present in the Git baseline. You
may keep the local `grind.toml` untracked if you prefer; never put secrets
in it.

## 5. Configure `grind.toml` and inspect before running

```bash
cat > grind.toml <<'EOF'
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

[logging]
level = "INFO"
stdout = true
files = true
EOF

grind explain
```

**Adjust the model IDs/provider to versions verified in step 2**, and
confirm the roadmap paths and state-file location in `grind explain`.
The `grind.toml` path values resolve relative to the **current working
directory**, not the TOML file location; `-r` and `-g` override only their
matching TOML entries. Codegrinder can read configuration from
`grind --conf /path/to/config.toml explain` as well.

`grind explain` is offline/read-only; it does **not** test credentials,
verify model access, create a PR, or prove GitHub CI readiness.

## 6. Run the first PR, review it, and merge deliberately

```bash
grind run
```

This is a **real workflow execution**, not a dry run. Codegrinder
preflights the configured agent CLIs, plans the first PR, lets Pi edit in an
isolated worktree, verifies the configured local check, commits/pushes the
branch, creates a GitHub PR, and invokes independent review. After
successful review and CI it waits for GitHub to confirm the merge.

In another terminal, inspect the newly created PR via GitHub or `gh pr list`.
Review the diff and checks yourself before merging, for example with
`gh pr view <PR_NUMBER>` and `gh pr checks <PR_NUMBER>`.
The sample `Merge-Mode: manual` means the operator controls merging;
Codegrinder does not bypass that decision.

If the first terminal remains at `WAIT_MERGE`, that is expected: its
workflow lock is still held, and a duplicate `grind run` for the same
roadmap will stop with `ANOTHER_RUNNER_ACTIVE`. Once the PR is merged,
Codegrinder confirms the merge and can proceed to the next roadmap entry.
If the terminal was interrupted, use `grind explain` and the recovery
instructions before restarting; do not delete `state.json` or
`runner.lock` to force progress.

## 7. Find artifacts and decide what to do next

`grind explain` prints the workflow state path. By default, checkpoint
data live under `~/.local/state/codegrinder/<workflow-id>/` (or
`$XDG_STATE_HOME/codegrinder/<workflow-id>/`). Plans, reports, review
JSON, logs, and attempt journals are stored outside the target checkout.
The current stage and step result distinguish negative review verdicts
(`SUCCEEDED / NEGATIVE`) from genuine execution errors (`FAILED` with
a category and error code).

If the first run stops, consult [Troubleshooting](TROUBLESHOOTING.md)
before modifying workflow files or replaying a stage. Codegrinder may
require manual reconciliation to prevent duplicate Git and agent actions.
