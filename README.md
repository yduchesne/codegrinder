# PRFlow — Codex planning/review + Pi/DeepSeek coding

This is a **portable starter implementation**, not a preconfigured integration with any user's actual repo. It has not been integration-tested against a live Codex/Pi/DeepSeek/GitHub account; perform a disposable-repository pilot before unattended use.

## Architecture

`roadmap.json` (all PR definitions, in order) -> `prflow.py` deterministic state machine -> Codex plans -> Pi implements -> project checks -> Git push/PR creation -> Codex reviews -> Pi fixes -> checks and review again -> merge is externally confirmed -> next PR. Execution is **sequential**.

- Roadmap is hashed; modification during a run causes an explicit stop.
- Durable per-PR stage/history is stored in `STATE_DIR/state.json`, outside the working repository.
- Each PR gets a separate Git worktree, Codex plan/review artifacts and Pi session, all in `STATE_DIR`.
- Script takes a single-runner file lock (Linux/WSL2 `fcntl`).
- Codex planning and review are run with `--sandbox read-only` and schema-constrained final JSON.
- Pi runs in `--mode json`; successful completion requires `agent_settled` and no terminal error.
- Only checks specified by the roadmap execute as deterministic quality gates. The agent cannot alter configured gates without changing the frozen roadmap.
- Agent edits are checked against `allowed_paths` (this is **post-execution detection**, not a security sandbox).
- Pi fixups are limited by `max_fixups`, including attempts interrupted mid-run.
- The controller creates branches, commits, pushes, opens PRs, checks status and merges. Pi is instructed not to do those things.
- Default mode requires a separate human merge; `--auto-merge` opts into GitHub's standard auto-merge rules without bypassing protection.
- `DONE` is marked only after GitHub reports the PR as merged. No PR is selected merely because an agent claimed success.

## Environment (Linux / WSL2)

Requires Python 3.11+, `git`, GitHub CLI `gh`, OpenAI `codex` CLI, `pi`, and authenticated provider credentials. DeepSeek's current Pi integration describes a `models.json` provider with `deepseek-v4-pro` and `deepseek-v4-flash`. Check your installed Pi version and `pi --list-models deepseek` before using the configured model.

See:
- Codex noninteractive: https://learn.chatgpt.com/docs/non-interactive-mode
- Pi CLI: https://docs.pi.dev/latest/cli
- Pi JSON integration: https://docs.pi.dev/latest/cli-integration
- DeepSeek + Pi: https://github.com/deepseek-ai/awesome-deepseek-agent/blob/main/docs/pi_mono.md
- GitHub merge CLI: https://cli.github.com/manual/gh_pr_merge

## Configure

1. Copy `config/roadmap.json` to an appropriate path **outside** the target repo, and fill in the *actual* complete PR list in execution order. The two entries shipped are **placeholders**: do not run them against a real project.
2. For each PR supply: stable ID, title, goal, dependencies on earlier PRs, objectively testable acceptance requirements, allowed file globs, and pre-approved shell check commands. Use exact check command and full test set required for that PR; no tests are invented by agents.
3. Set `planning_guide` to the repo-relative authoring guide (or `""` if unavailable). A configured but missing guide stops planning.
4. Set `required_checks` to exact GitHub check names from the repo's actual CI. The controller **will not automatically merge** without a configured CI list. Required GitHub branch protection and approvals should be set on `main` separately.
5. Set `max_fixups` and `command_timeout_seconds` to budgets. Keep `merge_mode: manual` initially.
6. Authenticate `gh`, `codex`, and Pi/DeepSeek with your normal tools. Do **not** commit API keys, auth files, state artifacts, or model logs.
7. Run the workflow in a security-isolated environment before fully unattended operation. **Pi tools are not inherently sandboxed**. Run Pi without GitHub write credentials or host secrets, ideally using a restricted container/runner with only its isolated worktree available; leave network/git authorization to the orchestrator outside that container. This starter does **not** implement that isolation. For trusted personal repositories on a personal machine, assess that risk explicitly.

## Commands

```bash
export REPO="$HOME/dev/quervant"                  # change for your repo
export ROADMAP="$HOME/automation/roadmap.json"   # completed roadmap JSON
export STATE_DIR="$HOME/prflow-state/quervant"   # outside REPO, not versioned

python3 prflow.py preflight --repo "$REPO" --roadmap "$ROADMAP" --state-dir "$STATE_DIR"
python3 -m unittest discover -s tests -v
python3 prflow.py status --repo "$REPO" --roadmap "$ROADMAP" --state-dir "$STATE_DIR"

# Pilot just one PR. Returns exit 3 if it is awaiting CI/merge.
python3 prflow.py run --once --repo "$REPO" --roadmap "$ROADMAP" --state-dir "$STATE_DIR"

# After completing the pilot, allow GitHub auto-merge and continue all PRs.
python3 prflow.py run --auto-merge --follow --repo "$REPO" --roadmap "$ROADMAP" --state-dir "$STATE_DIR"
```

`--follow` polls the current PR every 30 seconds for up to 60 minutes (change via `--wait-seconds` / `--max-wait-minutes`). If still pending, exit 3 with state retained; run again later. Exit 0 means entire roadmap complete (or one merged PR with `--once`), exit 2 means a stop, exit 3 means pending merge/CI.

Inspect `STATE_DIR/state.json`, `STATE_DIR/artifacts/<PR>/plan.md`, `review-*.json` and check logs. A `STOPPED` PR requires explicit inspection, then `unblock` with the safest appropriate stage:

```bash
python3 prflow.py unblock --pr-id PR-001 --stage VERIFY \
  --repo "$REPO" --roadmap "$ROADMAP" --state-dir "$STATE_DIR"
```

**Do not** use `unblock` to waive security, scope, review or CI failures. Only restart after resolving the underlying issue and confirming evidence. If a roadmap change is legitimately required mid-run, stop and reconcile the running PR and state before replacing the original roadmap; direct edits trigger the hash guard.

## Important safeguards still needed for production unattended operation

- Containerized Pi worker, with credentials/network restrictions; GitHub tokens only in the controller, not Pi's process/home; Codex review/planning execution equally isolated from untrusted repo configuration/extensions.
- Stronger scope enforcement (filesystem sandbox; Git diff checks alone don't prevent access outside repo).
- Verified GitHub branch protection, merge queue handling, required external approvals where applicable.
- A per-PR frozen base SHA, signed evidence and independent review of the exact **published** SHA (controller checks SHA equality, but should also verify remote branch updates after asynchronous merge requests).
- Budget controls for API spend, rate limits and maximum program runtime; alerting when the workflow exits 2/3.
- Better recovery for ambiguous in-flight subprocess crashes (this starter fails closed and requires operator intervention).
- End-to-end tests with fake codex/pi/gh executables and a disposable GitHub repo.

## Stop behavior / decision matrix

| Condition | Decision |
|---|---|
| Roadmap changed; missing plan guide; unknown dependency | STOP |
| Codex plan BLOCKED or plan schema/required headings missing | STOP |
| Pi output missing/incomplete, timeout, or process failure | STOP |
| Changed file lies outside PR `allowed_paths` | STOP |
| A configured local check fails | Pi fixup within fixed budget; recheck |
| Codex review finds actionable deficiencies | Pi fixup within fixed budget; recheck and rereview |
| Reviewer returns STOP or inconsistent structured response | STOP |
| PR head changed after accepted review | STOP |
| CI required check fails | STOP (operator investigates) |
| CI pending, PR open, reviewer passed | Wait/poll or exit 3; retain stage |
| Merge requested but not GitHub-confirmed | Do not advance |
| GitHub reports exact reviewed PR merged | Mark DONE, select next uncompleted PR |
| Every roadmap PR DONE | ROADMAP_COMPLETE |

The controller is intentionally conservative. The first version should be exercised on a noncritical roadmap with small documentation/code PRs.
