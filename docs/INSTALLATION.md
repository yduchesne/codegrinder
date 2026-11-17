# Installing Codegrinder from GitHub

Codegrinder is a Python command-line application. Its command is `grind`. For now, we distribute the package **directly from this GitHub repository**, using [uv](https://docs.astral.sh/uv/); no PyPI account, publication, or package registry is involved.

> **Project status:** Codegrinder is an integration pilot. Its real-agent unattended workflows have not been validated for production use. The current implementation is on the `feat/prflow-orchestrator` branch until that pull request is merged into `main`.

## Prerequisites

| Requirement | Why it is needed |
| --- | --- |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | Installs Python and manages Codegrinder in an isolated tool environment |
| Git and access to GitHub | Lets `uv` fetch the package source from this repository; Git is also used by workflows |
| Python 3.14 | Minimum version declared in Codegrinder's package metadata; `uv` can install it |
| GitHub CLI (`gh`) | Required by `grind run` to publish/reconcile PRs and verify merges |
| Codex CLI (`codex`) | Default planner and reviewer |
| Pi CLI (`pi`) | Default coding agent |

For `grind run`, the `gh`, `codex`, and `pi` CLIs must be separately installed, authenticated, and permitted to access the target repository and selected models. Installing the Python package **does not** install these external programs or authenticate them. `grind --help` and `grind explain --help` can be checked without running agents.

The workflow's process-recovery logic is Linux-oriented; do not assume that unattended execution has been validated on Windows or macOS merely because `uv` can install the package there.

## Install the CLI

Once the current implementation has been merged into `main`, install it using:

```bash
uv python install 3.14
uv tool install --python 3.14 "git+https://github.com/yduchesne/codegrinder.git@main"
grind --help
```

`uv tool install` creates a dedicated environment and places the `grind` executable in its tool-bin directory. You do **not** need to clone Codegrinder into every project where you will run it.

**Before the feature PR is merged**, use the development branch explicitly:

```bash
uv tool install --python 3.14 \
  "git+https://github.com/yduchesne/codegrinder.git@feat/prflow-orchestrator"
```

For reproducible installs, prefer a **tagged release** after the maintainer has created one. For example, *once `v0.1.0` exists*:

```bash
uv tool install --python 3.14 \
  "git+https://github.com/yduchesne/codegrinder.git@v0.1.0"
```

These examples do not imply that `v0.1.0` is already published. A Git tag selects a specific release; `@main` follows the evolving development branch at installation time. For an exact revision, substitute a full commit SHA after `@`.

If your shell cannot find `grind` after installation, inspect the directory with `uv tool dir --bin`, then run `uv tool update-shell` and restart your shell or add the reported directory to your `PATH`. On common Linux setups this is `~/.local/bin`.

## First checks in a target repository

Codegrinder operates on **the target project's Git checkout**, not necessarily on the Codegrinder repository itself. From that checkout:

```bash
grind --help
grind explain --help
grind explain
```

To run a workflow you must provide a versioned roadmap and an authoring guide, either in `./grind.toml` or using `--roadmap` and `--guide`. For example:

```bash
grind run \
  --roadmap docs/ROADMAP.v1.md \
  --guide docs/DETAILED_PR_PLAN_AUTHORING_GUIDE.md
```

The file names above are examples: **they must actually exist in the target checkout** and the roadmap must match the [versioned roadmap format](../examples/roadmap.v1.md). Execution also requires configured, authenticated Codex, Pi, and `gh`. Start with `grind explain` to inspect the resolved configuration and state path without contacting GitHub or invoking agents. See [Configuration](CONFIGURATION.md) and [Architecture](ARCHITECTURE.md) for details.

## Upgrade, switch revisions, and uninstall

To refresh an installation that follows the moving `main` branch:

```bash
uv tool install --reinstall --refresh --python 3.14 \
  "git+https://github.com/yduchesne/codegrinder.git@main"
```

To switch from one versioned Git release to another, explicitly reinstall with the **new tag** (for example, `@v0.2.0` once it exists). A tag-pinned install should not silently change to a newer release. Do not rewrite published release tags.

Inspect or remove your installed tool:

```bash
uv tool list
uv tool uninstall codegrinder
```

A tool upgrade/reinstallation changes the Python installation, **not** the saved workflow state in `${XDG_STATE_HOME:-~/.local/state}/codegrinder/`. Existing checkpoints may refuse incompatible configuration or workflow changes and require deliberate reconciliation.

## Developing Codegrinder itself

Contributors should use a source checkout and the existing `./install.sh` script, which prepares Python 3.14, `uv` dependencies, development checks, and an **editable** tool installation. That script is **not** required for ordinary GitHub-based tool installation. See [Security and SBOM](SECURITY_AND_SBOM.md) for the development quality gates.

For the tag-based maintainer procedure, see [Releasing](RELEASING.md).
