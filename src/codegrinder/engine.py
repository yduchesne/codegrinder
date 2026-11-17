#!/usr/bin/env python3
"""Roadmap-driven Codex/Pi/GitHub PR orchestrator (single-repository, sequential).

Requires Python 3.14+, git, gh, codex, pi. Intentionally fail-closed.
Run from a trusted Linux / WSL2 environment; see README for security limits.
"""
from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .roadmap import parse_roadmap
from .interfaces import (
    AgentExecutionError,
    PlanningStep,
    CodingStep,
    ReviewStep,
    StructuredTask,
    CodingTask,
    WorkflowContext,
    PreflightTask,
    PreflightResult,
)
from .selection import PRSelector, PRSelectionError, SequentialPRSelector, is_completed

ROOT = Path(__file__).resolve().parent
STAGES = {
    "PLAN", "CODE", "VERIFY", "PUBLISH", "REVIEW",
    "REPLAN", "CODE_FIXUP", "FIXUP", "WAIT_MERGE", "DONE", "STOPPED",
}
PR_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

class StopWorkflow(Exception):
    pass


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def validate_roadmap(config: dict):
    if config.get("version") != 1 or not isinstance(config.get("prs"), list) or not config["prs"]:
        raise StopWorkflow("ROADMAP_INVALID: require version=1 and nonempty prs list")
    base_branch = config.get("base_branch", "")
    if (not isinstance(base_branch, str) or not re.fullmatch(r"[A-Za-z0-9._/-]+", base_branch)
            or base_branch.startswith("/") or ".." in Path(base_branch).parts):
        raise StopWorkflow("ROADMAP_INVALID: base_branch missing or invalid")
    if config.get("merge_mode", "manual") not in ("manual", "auto"):
        raise StopWorkflow("ROADMAP_INVALID: merge_mode must be manual or auto")
    for key in ("max_fixups", "command_timeout_seconds"):
        if not isinstance(config.get(key), int) or config[key] < 1:
            raise StopWorkflow(f"ROADMAP_INVALID: {key} must be a positive integer")
    if not isinstance(config.get("required_checks"), list) or any(not isinstance(x, str) or not x for x in config["required_checks"]):
        raise StopWorkflow("ROADMAP_INVALID: required_checks must be a list of CI check names")
    ids = set()
    for pr in config["prs"]:
        pid = pr.get("id", "")
        if not isinstance(pid, str) or not PR_ID_RE.fullmatch(pid) or pid in ids:
            raise StopWorkflow(f"ROADMAP_INVALID: duplicate or invalid PR id {pid!r}")
        if not isinstance(pr.get("title"), str) or not pr["title"].strip():
            raise StopWorkflow(f"ROADMAP_INVALID: title missing for {pid}")
        for key in ("goal",):
            if not isinstance(pr.get(key), str) or not pr[key].strip():
                raise StopWorkflow(f"ROADMAP_INVALID: {key} missing for {pid}")
        for key in ("depends_on", "acceptance", "allowed_paths", "checks"):
            xs = pr.get(key)
            if not isinstance(xs, list) or any(not isinstance(x, str) or not x.strip() for x in xs):
                raise StopWorkflow(f"ROADMAP_INVALID: {key} must be a string list for {pid}")
        if not pr["acceptance"] or not pr["allowed_paths"] or not pr["checks"]:
            raise StopWorkflow(f"ROADMAP_INVALID: acceptance, allowed_paths, checks required for {pid}")
        if any(dep not in ids for dep in pr["depends_on"]):
            raise StopWorkflow(f"ROADMAP_INVALID: dependencies must refer to earlier roadmap PRs: {pid}")
        if any(p.startswith("/") or ".." in Path(p).parts for p in pr["allowed_paths"]):
            raise StopWorkflow(f"ROADMAP_INVALID: allowed_paths must be relative: {pid}")
        status = pr.get("status")
        if status is not None and status not in {"DONE", "PENDING"}:
            raise StopWorkflow(f"ROADMAP_INVALID: invalid canonical status for {pid}")
        ids.add(pid)


def run(command: list[str], cwd: Path, *, timeout: int = 1800, stdin: str | None = None,
        output_base: Path | None = None, check: bool = True):
    result = subprocess.run(command, cwd=str(cwd), input=stdin, text=True,
                            capture_output=True, timeout=timeout, check=False)
    if output_base:
        output_base.parent.mkdir(parents=True, exist_ok=True)
        output_base.with_suffix('.stdout').write_text(result.stdout, encoding='utf-8')
        output_base.with_suffix('.stderr').write_text(result.stderr, encoding='utf-8')
    if check and result.returncode:
        raise StopWorkflow(f"COMMAND_FAILED({result.returncode}): {command[0]} {command[1] if len(command)>1 else ''}; "
                           f"stderr={result.stderr[-650:]}")
    return result


def git(cwd: Path, *args: str, check=True):
    return run(["git", *args], cwd, timeout=120, check=check).stdout.strip()


def modified_paths(wt: Path, base: str) -> set[str]:
    # Base-to-head diff plus uncommitted/unstaged and untracked paths.
    paths = set(filter(None, git(wt, "diff", "--no-renames", "--name-only", "--diff-filter=ACDMRTUXB", f"origin/{base}...HEAD").splitlines()))
    paths |= set(filter(None, git(wt, "diff", "--name-only").splitlines()))
    paths |= set(filter(None, git(wt, "diff", "--cached", "--name-only").splitlines()))
    paths |= set(filter(None, git(wt, "ls-files", "--others", "--exclude-standard").splitlines()))
    return paths


def validate_paths(paths: set[str], patterns: list[str]):
    forbidden = sorted(p for p in paths if not any(fnmatch.fnmatchcase(p, glob) for glob in patterns))
    if forbidden:
        raise StopWorkflow("SCOPE_VIOLATION: " + ", ".join(forbidden))


def require_clean(wt: Path):
    status = git(wt, "status", "--porcelain")
    if status:
        raise StopWorkflow("WORKTREE_NOT_CLEAN: unexpected modifications before a read-only step")


def validate_plan(data):
    if not isinstance(data, dict) or data.get("status") not in ("READY", "BLOCKED"):
        raise StopWorkflow("PLAN_SCHEMA_INVALID: bad status")
    if data["status"] == "BLOCKED":
        raise StopWorkflow("PLANNING_BLOCKED: " + str(data.get("blocker", "unspecified")))
    markdown = data.get("plan_markdown", "")
    if not isinstance(markdown, str) or len(markdown) < 300:
        raise StopWorkflow("PLAN_SCHEMA_INVALID: insufficient detailed plan")
    for token in ("## Scope", "## Steps", "## Tests", "## Decision Matrix", "## Stop Conditions"):
        if token not in markdown:
            raise StopWorkflow("PLAN_SCHEMA_INVALID: missing required heading " + token)
    return markdown


def validate_review(data):
    if not isinstance(data, dict) or data.get("verdict") not in ("PASS", "CHANGES_REQUIRED", "STOP"):
        raise StopWorkflow("REVIEW_SCHEMA_INVALID: bad verdict")
    findings = data.get("findings")
    if not isinstance(findings, list):
        raise StopWorkflow("REVIEW_SCHEMA_INVALID: findings not an array")
    if data["verdict"] == "PASS" and findings:
        raise StopWorkflow("REVIEW_INCONSISTENT: PASS with findings")
    if data["verdict"] == "CHANGES_REQUIRED" and not findings:
        raise StopWorkflow("REVIEW_INCONSISTENT: findings required")
    for f in findings:
        if not isinstance(f, dict) or any(not f.get(x) for x in ("id", "severity", "file", "description", "required_fix")):
            raise StopWorkflow("REVIEW_SCHEMA_INVALID: malformed finding")
        if f["severity"] not in ("critical", "high", "medium", "low"):
            raise StopWorkflow("REVIEW_SCHEMA_INVALID: unknown severity")
    if data["verdict"] == "STOP":
        raise StopWorkflow("REVIEW_STOP: " + data.get("summary", "unspecified"))
    return data["verdict"], findings


def check_pi_events(path: Path):
    settled = None
    last_assistant = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as e:
            raise StopWorkflow("PI_INVALID_JSONL: " + str(e))
        if event.get("type") == "agent_settled":
            settled = event
        if event.get("type") == "message_end" and event.get("message", {}).get("role") == "assistant":
            last_assistant = event["message"]
    if settled is None or settled.get("aborted") is not False:
        raise StopWorkflow("PI_INCOMPLETE: no successful agent_settled event")
    if last_assistant is None or last_assistant.get("stopReason") in ("error", "aborted", "length", "pending", "toolUse"):
        raise StopWorkflow("PI_INCOMPLETE: last assistant message did not finish normally")


@contextmanager
def single_runner(folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    fd = os.open(folder / "runner.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StopWorkflow("ANOTHER_RUNNER_ACTIVE")
        yield
    finally:
        os.close(fd)


class DefaultOrchestrator:
    """Dependency-injected, sequential PR workflow with durable state."""

    def __init__(self, context: WorkflowContext,
                 *, planner: PlanningStep | None = None,
                 coder: CodingStep | None = None,
                 reviewer: ReviewStep | None = None,
                 selector: PRSelector | None = None):
        repo, roadmap, state_dir, guide = (
            context.repo, context.roadmap, context.state_dir, context.guide
        )
        self.context = context
        self.planner, self.coder, self.reviewer = planner, coder, reviewer
        self.selector = selector if selector is not None else SequentialPRSelector()
        self.configuration_sha256 = context.configuration_sha256
        self.agents = {
            "plan": planner.identity() if planner else None,
            "code": coder.identity() if coder else None,
            "review": reviewer.identity() if reviewer else None,
        }
        self.repo, self.roadmap, self.state_dir = repo.resolve(), roadmap.resolve(), state_dir.resolve()
        self.guide = guide.resolve()
        if not (self.repo / ".git").exists():
            # git worktree could be a .git file; this also permits one.
            raise StopWorkflow("REPO_INVALID: path is not a Git checkout")
        if self.repo == self.state_dir or self.repo in self.state_dir.parents:
            raise StopWorkflow("STATE_DIR_INSIDE_REPO: use a separate sibling directory")
        self.guide_text = self.guide.read_text(encoding="utf-8")
        if not self.guide_text.strip():
            raise StopWorkflow("GUIDE_EMPTY: detailed plan authoring guide is empty")
        self.guide_hash = hashlib.sha256(self.guide.read_bytes()).hexdigest()
        self.config = parse_roadmap(self.roadmap.read_text(encoding="utf-8"))
        validate_roadmap(self.config)
        self.hash = hashlib.sha256(self.roadmap.read_bytes()).hexdigest()
        self.state_file = self.state_dir / "state.json"
        if self.state_file.exists():
            self.state = load_json(self.state_file)
            if self.state.get("roadmap_sha256") != self.hash:
                raise StopWorkflow("ROADMAP_CHANGED: stop and reconcile the roadmap before continuing")
            if self.state.get("guide_sha256") != self.guide_hash:
                raise StopWorkflow("GUIDE_CHANGED: stop and reconcile the guide before continuing")
            if self.state.get("configuration_sha256") != self.configuration_sha256:
                raise StopWorkflow("CONFIG_CHANGED: stop and reconcile the TOML before continuing")
            if self.state.get("agents") != self.agents:
                raise StopWorkflow("AGENT_CONFIG_CHANGED: reconcile agent settings before resuming")
        else:
            self.state = {"roadmap_sha256": self.hash, "guide_sha256": self.guide_hash,
                          "roadmap_revision": self.config["revision"], "agents": self.agents,
                          "configuration_sha256": self.configuration_sha256,
                          "prs": {}, "created_at": utcnow()}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.save()  # pin both input hashes before the first potentially interrupted stage

    def save(self):
        atomic_json(self.state_file, self.state)

    def entry(self, pid: str):
        if pid not in self.state["prs"]:
            self.state["prs"][pid] = {"stage": "PLAN", "fixups": 0, "history": []}
            self.save()
        return self.state["prs"][pid]

    def transition(self, pid: str, stage: str, reason: str = ""):
        if stage not in STAGES:
            raise StopWorkflow("INVALID_INTERNAL_STAGE")
        ent = self.entry(pid)
        ent["history"].append({"at": utcnow(), "from": ent["stage"], "to": stage, "reason": reason})
        ent["stage"] = stage
        self.save()
        print(f"[{pid}] -> {stage}" + (f" ({reason})" if reason else ""), flush=True)

    def stop(self, pid: str, reason: str):
        self.entry(pid)["stopped_reason"] = reason
        self.transition(pid, "STOPPED", reason)
        print("STOP: " + reason, file=sys.stderr, flush=True)

    def wt(self, pid: str):
        return self.state_dir / "worktrees" / pid

    def artifact(self, pid: str):
        path = self.state_dir / "artifacts" / pid
        path.mkdir(parents=True, exist_ok=True)
        return path

    def prepare(self, pid: str):
        branch = f"prflow/{pid}"
        wt = self.wt(pid)
        base = self.config["base_branch"]
        if not wt.exists():
            git(self.repo, "fetch", "origin", base)
            known = git(self.repo, "branch", "--list", branch)
            if known:
                git(self.repo, "worktree", "add", str(wt), branch)
            else:
                git(self.repo, "worktree", "add", "-b", branch, str(wt), f"origin/{base}")
        if git(wt, "branch", "--show-current") != branch:
            raise StopWorkflow(f"WRONG_BRANCH: worktree does not use {branch}")
        return wt, branch

    def _structured(self, adapter, wt: Path, pid: str, kind: str,
                    prompt: str, schema: Path) -> dict:
        if adapter is None:
            raise StopWorkflow(f"AGENT_NOT_CONFIGURED: {kind}")
        return adapter.execute(StructuredTask(
            worktree=wt, pr_id=pid, phase=kind, prompt=prompt, schema=schema,
            artifacts=self.artifact(pid), timeout_seconds=self.config["command_timeout_seconds"],
        ))

    def _coding(self, wt: Path, pid: str, kind: str, prompt: str) -> None:
        if self.coder is None:
            raise StopWorkflow("AGENT_NOT_CONFIGURED: code")
        self.coder.execute(CodingTask(
            worktree=wt, pr_id=pid, phase=kind, prompt=prompt,
            artifacts=self.artifact(pid), state_dir=self.state_dir,
            fixup_number=self.entry(pid)["fixups"],
            timeout_seconds=self.config["command_timeout_seconds"],
        ))

    def plan(self, pr: dict, wt: Path):
        pid = pr["id"]
        require_clean(wt)
        prompt = f"""You are the PLANNER, not the implementer. Read and obey the frozen guide
identified as {self.guide} (sha256 {self.guide_hash}). Its authoritative text is included below.
Read relevant repository code, architecture docs and CI contracts. Do not change files or git state.
Produce a complete autonomous low-reasoning-agent implementation plan with sections EXACTLY:
## Scope, ## Steps, ## Tests, ## Decision Matrix, ## Stop Conditions.
Decision matrix must have predetermined conditions, actions and stop cases. No discretionary design left to coder.
No PR may expand scope beyond listed allowed_paths. No guessed APIs, tests or migration ordering.
If information is insufficient or conflicts with repository evidence, return BLOCKED and an explicit blocker.
The JSON final answer must match the supplied schema. Only status READY warrants continuation.
PR definition:\n{json.dumps(pr, indent=2)}
Base branch: {self.config['base_branch']}."""
        prompt += f"\n\nFROZEN DETAILED PLAN AUTHORING GUIDE:\n{self.guide_text}"
        data = self._structured(self.planner, wt, pid, "plan", prompt, ROOT / "schemas" / "plan.schema.json")
        markdown = validate_plan(data)
        (self.artifact(pid) / "plan.md").write_text(markdown + "\n", encoding="utf-8")
        self.entry(pid)["active_plan"] = "plan.md"
        self.save()
        self.transition(pid, "CODE")

    def _active_plan(self, pid: str) -> tuple[str, str]:
        """Return the frozen plan selected by the orchestrator, never by a worker."""
        name = self.entry(pid).get("active_plan", "plan.md")
        if not isinstance(name, str) or not re.fullmatch(r"plan(?:-fixup-[1-9][0-9]*)?\.md", name):
            raise StopWorkflow(f"PLAN_REFERENCE_INVALID: {pid}")
        file = self.artifact(pid) / name
        if not file.is_file():
            raise StopWorkflow(f"ACTIVE_PLAN_MISSING: {pid}: {name}")
        return name, file.read_text(encoding="utf-8")

    def _start_replan(self, pid: str, *, source: str, findings: list[dict],
                      summary: str = "", reviewed_head: str | None = None) -> None:
        """Atomically allocate one bounded correction cycle and route to planning.

        Feedback is durable and versioned. The stage and cycle counter advance in
        one state write, so restart never bypasses planning or spends a second slot.
        """
        ent = self.entry(pid)
        if ent["fixups"] >= self.config["max_fixups"]:
            raise StopWorkflow("MAX_FIXUPS_REACHED")
        if not findings:
            raise StopWorkflow("REPLAN_FEEDBACK_EMPTY")
        revision = ent["fixups"] + 1
        feedback_name = f"feedback-{revision}.json"
        feedback = {
            "source": source,
            "summary": summary,
            "findings": findings,
            "revision": revision,
            "previous_plan": ent.get("active_plan", "plan.md"),
            "reviewed_head": reviewed_head,
        }
        atomic_json(self.artifact(pid) / feedback_name, feedback)
        # Legacy name remains as a convenience for diagnostics; the history is
        # retained separately and the plan revision is the coding authority.
        atomic_json(self.artifact(pid) / "fixup.json", feedback)
        ent["fixups"] = revision
        ent["active_feedback"] = feedback_name
        ent["history"].append({
            "at": utcnow(), "from": ent["stage"], "to": "REPLAN",
            "reason": f"{source}: correction cycle {revision}",
        })
        ent["stage"] = "REPLAN"
        self.save()
        print(f"[{pid}] -> REPLAN ({source}: correction cycle {revision})", flush=True)

    def replan(self, pr: dict, wt: Path) -> None:
        """Invoke PlanningStep on the SAME PR using review/check observations."""
        pid = pr["id"]
        ent = self.entry(pid)
        revision = ent["fixups"]
        feedback_name = ent.get("active_feedback")
        if not isinstance(feedback_name, str) or feedback_name != f"feedback-{revision}.json":
            raise StopWorkflow("REPLAN_FEEDBACK_REFERENCE_INVALID")
        feedback_path = self.artifact(pid) / feedback_name
        if not feedback_path.is_file():
            raise StopWorkflow("REPLAN_FEEDBACK_MISSING")
        feedback = load_json(feedback_path)
        previous_name, previous_plan = self._active_plan(pid)
        if feedback.get("previous_plan") != previous_name:
            raise StopWorkflow("REPLAN_PREVIOUS_PLAN_MISMATCH")
        if feedback.get("source") == "codex_review":
            require_clean(wt)
            current_head = git(wt, "rev-parse", "HEAD")
            if current_head != feedback.get("reviewed_head"):
                raise StopWorkflow("REPLAN_REVIEWED_HEAD_CHANGED")
        if not isinstance(feedback.get("findings"), list) or not feedback["findings"]:
            raise StopWorkflow("REPLAN_FEEDBACK_INVALID")
        history = []
        for index in range(1, revision + 1):
            path = self.artifact(pid) / f"feedback-{index}.json"
            if not path.is_file():
                raise StopWorkflow(f"REPLAN_FEEDBACK_HISTORY_MISSING: {index}")
            history.append(load_json(path))
        prompt = f"""You are the PLANNER for CORRECTION CYCLE {revision} of PR {pid}.
You are NOT the coding agent. Examine the actual repository/worktree, the previous
frozen plan, all prior feedback, and CURRENT REVIEW/TEST OBSERVATIONS.
The current observations are mandatory input, not instructions to ignore or
blindly trust: validate them against the code. Define exact corrective steps.
Do not edit files, change Git state, expand PR scope, invent APIs, relax checks,
or delegate design decisions to the coding agent.
Write a complete REVISED FIXUP PLAN with exactly these headings:
## Scope, ## Steps, ## Tests, ## Decision Matrix, ## Stop Conditions.
The plan must specify precise repairs, objective validation, predetermined
choices, and explicit STOP cases. If findings are contradictory or the
implementation cannot safely meet them, return BLOCKED with a reason.
Preserve already accepted design requirements and account for earlier attempts.
Only READY and a valid detailed plan permits another coding invocation.
PR REQUIREMENTS:
{json.dumps(pr, indent=2)}
PREVIOUS PLAN ({previous_name}):
{previous_plan}
FEEDBACK HISTORY (including current cycle):
{json.dumps(history, indent=2)}
CURRENT REVIEW/TEST OBSERVATIONS:
{json.dumps(feedback, indent=2)}
AUTHORITATIVE DETAILED PLAN AUTHORING GUIDE:
{self.guide_text}
"""
        result = self._structured(
            self.planner, wt, pid, f"replan-{revision}", prompt,
            ROOT / "schemas" / "plan.schema.json",
        )
        markdown = validate_plan(result)
        next_name = f"plan-fixup-{revision}.md"
        (self.artifact(pid) / next_name).write_text(markdown + "\n", encoding="utf-8")
        ent["active_plan"] = next_name
        self.save()
        self.transition(pid, "CODE_FIXUP", f"revised plan {next_name} validated")

    def coding_prompt(self, pr: dict, fixup: bool):
        pid = pr["id"]
        plan_name, plan = self._active_plan(pid)
        directive = """Execute the FROZEN plan below exactly. Work only on specified paths; run relevant tests.
Do NOT invent alternate architecture, relax tests, edit the plan, create commits, push, merge, or operate on other PRs.
Stop with explicit BLOCKED and explanation if any condition does not fit the decision matrix.
Leave changed files in this worktree for the orchestrator to validate and commit."""
        if fixup:
            if plan_name != f"plan-fixup-{self.entry(pid)['fixups']}.md":
                raise StopWorkflow("FIXUP_PLAN_NOT_APPROVED")
            observations = self.artifact(pid) / f"feedback-{self.entry(pid)['fixups']}.json"
            if not observations.is_file():
                raise StopWorkflow("FIXUP_FEEDBACK_MISSING")
            directive += ("\nFix only the findings below, following the revised plan. "
                          "Do not redesign the PR or silently waive findings:\n"
                          + observations.read_text(encoding="utf-8"))
        return (f"You are the CODING WORKER for {pid}.\n{directive}\n"
                f"ALLOWED PATHS:\n{json.dumps(pr['allowed_paths'])}\n"
                f"FROZEN PLAN ({plan_name}):\n{plan}")

    def verify(self, pr: dict, wt: Path):
        pid = pr["id"]
        changed = modified_paths(wt, self.config["base_branch"])
        validate_paths(changed, pr["allowed_paths"])
        if not changed:
            raise StopWorkflow("NO_CHANGE: PR branch has no changes")
        failed = []
        for n, check in enumerate(pr["checks"]):
            # Commands are trusted configuration, never model-generated.
            res = run(["bash", "-lc", check], wt, timeout=self.config["command_timeout_seconds"],
                      check=False, output_base=self.artifact(pid) / f"check-{n}")
            if res.returncode:
                failed.append({"id": f"CHECK-{n}", "severity": "high", "file": "<check>",
                               "description": f"Check failed: {check} (exit {res.returncode})",
                               "required_fix": "Fix underlying issue without weakening the check; inspect check log.",
                               "stderr_tail": res.stderr[-1500:]})
        if failed:
            self._start_replan(pid, source="deterministic_checks",
                               findings=failed, summary="Deterministic checks failed")
        else:
            self.transition(pid, "PUBLISH", "all configured checks passed")

    def publish(self, pr: dict, wt: Path, branch: str):
        pid = pr["id"]
        base = self.config["base_branch"]
        validate_paths(modified_paths(wt, base), pr["allowed_paths"])
        git(wt, "add", "--all")
        staged = git(wt, "diff", "--cached", "--name-only")
        if staged:
            git(wt, "commit", "-m", f"{pid}: {pr['title']}")
        if not git(wt, "diff", "--name-only", f"origin/{base}...HEAD"):
            raise StopWorkflow("NO_COMMITS: no changes to publish")
        require_clean(wt)
        git(wt, "push", "--set-upstream", "origin", branch)
        gh = run(["gh", "pr", "view", branch, "--json", "number,url,headRefOid,baseRefName"], wt, check=False)
        if gh.returncode:
            body = self.artifact(pid) / "pr-body.md"
            body.write_text(f"## Goal\n{pr['goal']}\n\n## Acceptance\n" +
                            "\n".join("- " + a for a in pr["acceptance"]) +
                            f"\n\nOrchestrated PR {pid}. Frozen plan and review evidence retained outside checkout.\n",
                            encoding="utf-8")
            run(["gh", "pr", "create", "--base", base, "--head", branch, "--title",
                 f"{pid}: {pr['title']}", "--body-file", str(body)], wt, timeout=120)
            gh = run(["gh", "pr", "view", branch, "--json", "number,url,headRefOid,baseRefName"], wt)
        info = json.loads(gh.stdout)
        sha = git(wt, "rev-parse", "HEAD")
        if info.get("headRefOid") != sha or info.get("baseRefName") != base:
            raise StopWorkflow("PUBLISHED_SHA_OR_BASE_MISMATCH")
        ent = self.entry(pid)
        ent.update({"pr_number": info["number"], "pr_url": info["url"], "published_head": sha})
        self.save()
        self.transition(pid, "REVIEW", "published head " + sha[:12])

    def review(self, pr: dict, wt: Path):
        pid = pr["id"]
        require_clean(wt)
        before = git(wt, "rev-parse", "HEAD")
        if before != self.entry(pid).get("published_head"):
            raise StopWorkflow("HEAD_CHANGED_SINCE_PUBLISH")
        plan_name, plan = self._active_plan(pid)
        check_evidence = []
        for n, cmd in enumerate(pr["checks"]):
            log = self.artifact(pid) / f"check-{n}.stderr"
            out = self.artifact(pid) / f"check-{n}.stdout"
            check_evidence.append({"command": cmd, "stderr_tail": log.read_text(encoding="utf-8")[-1000:] if log.exists() else "MISSING",
                                   "stdout_tail": out.read_text(encoding="utf-8")[-1500:] if out.exists() else "MISSING"})
        prompt = f"""You are an INDEPENDENT READ-ONLY REVIEWER of PR {pid}, SHA {before}.
Do not edit, commit, push, merge or modify any state. Review ALL changes in
`git diff origin/{self.config['base_branch']}...HEAD` against the original requirements,
actual architecture, plan, tests, and safety/security implications. Never accept a claimed test
without inspecting observable evidence and relevant implementation. Treat unknowns as STOP.
Return structured JSON matching schema; verdict PASS only when there are zero findings.
For CHANGES_REQUIRED, give precise, independently actionable fixes with file paths.
PR:\n{json.dumps(pr, indent=2)}\nLOCAL CHECK EVIDENCE (not proof by itself):\n{json.dumps(check_evidence, indent=2)}\nFROZEN PLAN ({plan_name}):\n{plan}"""
        data = self._structured(self.reviewer, wt, pid, f"review-{self.entry(pid)['fixups']}", prompt,
                          ROOT / "schemas" / "review.schema.json")
        require_clean(wt)
        if git(wt, "rev-parse", "HEAD") != before:
            raise StopWorkflow("HEAD_CHANGED_DURING_REVIEW")
        verdict, findings = validate_review(data)
        if verdict == "CHANGES_REQUIRED":
            self._start_replan(
                pid, source="codex_review", findings=findings,
                summary=str(data.get("summary", "")), reviewed_head=before,
            )
        else:
            self.entry(pid)["accepted_head"] = before
            self.save()
            self.transition(pid, "WAIT_MERGE", "independent Codex review PASS")

    def wait_merge(self, pr: dict, wt: Path):
        pid = pr["id"]
        ent = self.entry(pid)
        info = json.loads(run(["gh", "pr", "view", str(ent["pr_number"]),
                               "--json", "state,mergedAt,headRefOid,baseRefName,mergeCommit"], wt).stdout)
        if info.get("headRefOid") != ent.get("accepted_head"):
            raise StopWorkflow("PR_HEAD_CHANGED_AFTER_REVIEW: do not merge unreviewed code")
        if info.get("baseRefName") != self.config["base_branch"]:
            raise StopWorkflow("PR_BASE_CHANGED_AFTER_REVIEW")
        if info.get("state") == "MERGED" and info.get("mergedAt"):
            ent["merged_at"] = info["mergedAt"]
            ent["merge_commit"] = (info.get("mergeCommit") or {}).get("oid")
            self.save()
            self.transition(pid, "DONE", "GitHub confirms PR merged")
            return True
        if info.get("state") != "OPEN":
            raise StopWorkflow("PR_NOT_OPEN_OR_MERGED")
        checks = run(["gh", "pr", "checks", str(ent["pr_number"]),
                      "--json", "name,bucket,state"], wt, check=False)
        if checks.returncode and not checks.stdout.strip():
            raise StopWorkflow("CI_STATUS_UNAVAILABLE: " + checks.stderr[-500:])
        try:
            reported = json.loads(checks.stdout)
        except ValueError:
            raise StopWorkflow("CI_STATUS_INVALID")
        required = self.config["required_checks"]
        names = {c["name"]: c.get("bucket") for c in reported}
        if not required:
            raise StopWorkflow("NO_REQUIRED_CI_CONFIGURED: specify required_checks before merging")
        if any(names.get(name) in ("fail", "cancel") for name in required):
            raise StopWorkflow("REQUIRED_CI_FAILED: " + repr(names))
        checks_passed = all(names.get(name) == "pass" for name in required)
        if self.config.get("merge_mode", "manual") == "auto" and checks_passed and not ent.get("merge_requested"):
            # GitHub enforces branch protection/required approvals. Do not bypass them.
            run(["gh", "pr", "merge", str(ent["pr_number"]), "--auto", "--squash",
                 "--match-head-commit", ent["accepted_head"]], wt, timeout=120)
            ent["merge_requested"] = True
            self.save()
        if checks_passed:
            print(f"[{pid}] CI passed. Waiting for GitHub merge (mode={self.config.get('merge_mode')}).")
        else:
            print(f"[{pid}] Waiting for required CI: " + repr({k: names.get(k) for k in required}))
        return False

    def advance(self, pr: dict):
        pid = pr["id"]
        wt, branch = self.prepare(pid)
        stage = self.entry(pid)["stage"]
        if stage == "PLAN":
            self.plan(pr, wt)
        elif stage == "CODE":
            self._coding(wt, pid, "code", self.coding_prompt(pr, False))
            self.transition(pid, "VERIFY")
        elif stage == "VERIFY":
            self.verify(pr, wt)
        elif stage == "PUBLISH":
            self.publish(pr, wt, branch)
        elif stage == "REVIEW":
            self.review(pr, wt)
        elif stage == "REPLAN":
            self.replan(pr, wt)
        elif stage == "CODE_FIXUP":
            self._coding(wt, pid, "fixup", self.coding_prompt(pr, True))
            self.transition(pid, "VERIFY")
        elif stage == "FIXUP":
            # Older persisted FIXUP states invoked the coding agent directly.
            # Never silently resume that unsafe transition; reconcile manually.
            raise StopWorkflow("LEGACY_FIXUP_REQUIRES_REPLAN_MIGRATION")
        elif stage == "WAIT_MERGE":
            return self.wait_merge(pr, wt)
        elif stage in ("STOPPED", "DONE"):
            return False
        else:
            raise StopWorkflow("UNKNOWN_STAGE: " + stage)
        return True

    def next_pr(self) -> dict | None:
        """Delegate deterministic selection to the injected PRSelector."""
        try:
            return self.selector.next_pr(self.config["prs"], self.state["prs"])
        except PRSelectionError as exc:
            raise StopWorkflow(str(exc)) from exc

    def _verified_merge(self, pr: dict) -> dict:
        """Look up one exact, merged GitHub PR for the deterministic branch."""
        pid = pr["id"]
        branch = f"prflow/{pid}"
        completed = run([
            "gh", "pr", "list", "--state", "all", "--head", branch,
            "--json", "number,url,state,mergedAt,baseRefName,headRefName,headRefOid,mergeCommit",
            "--limit", "100",
        ], self.repo, timeout=120)
        try:
            results = json.loads(completed.stdout)
        except (TypeError, ValueError) as exc:
            raise StopWorkflow(f"MERGE_EVIDENCE_INVALID: {pid}") from exc
        if not isinstance(results, list):
            raise StopWorkflow(f"MERGE_EVIDENCE_INVALID: {pid}")
        matches = [item for item in results if isinstance(item, dict)
                   and item.get("headRefName") == branch]
        if len(matches) != 1:
            raise StopWorkflow(f"MERGE_EVIDENCE_AMBIGUOUS_OR_MISSING: {pid}: {len(matches)}")
        evidence = matches[0]
        commit = evidence.get("mergeCommit")
        if (evidence.get("state") != "MERGED" or not evidence.get("mergedAt")
                or not isinstance(commit, dict) or not commit.get("oid")
                or not isinstance(evidence.get("number"), int)
                or not isinstance(evidence.get("url"), str)):
            raise StopWorkflow(f"PR_NOT_VERIFIED_MERGED: {pid}")
        if evidence.get("baseRefName") != self.config["base_branch"]:
            raise StopWorkflow(f"PR_MERGED_TO_WRONG_BASE: {pid}")
        return evidence

    def reconcile_completed(self) -> None:
        """Verify completed labels and persisted completions against GitHub.

        A completed Markdown label is never independently trusted. Repeated
        runs verify the same merge SHA; discrepancies stop before selecting a PR.
        """
        for pr in self.config["prs"]:
            pid = pr["id"]
            ent = self.state["prs"].get(pid)
            if not is_completed(pr.get("status")) and not (
                ent is not None and is_completed(ent.get("stage"))
            ):
                continue
            evidence = self._verified_merge(pr)
            if ent is None:
                ent = self.entry(pid)
            expected = {
                "pr_number": evidence["number"],
                "pr_url": evidence["url"],
                "merged_at": evidence["mergedAt"],
                "merge_commit": evidence["mergeCommit"]["oid"],
            }
            for key, value in expected.items():
                if key in ent and ent[key] is not None and ent[key] != value:
                    raise StopWorkflow(f"MERGE_EVIDENCE_CONFLICT: {pid}: {key}")
            accepted = ent.get("accepted_head")
            if accepted is not None and evidence.get("headRefOid") != accepted:
                raise StopWorkflow(f"MERGE_EVIDENCE_CONFLICT: {pid}: accepted_head")
            ent.update(expected)
            if ent.get("stage") != "DONE":
                self.transition(pid, "DONE", "reconciled verified GitHub merge")
            else:
                self.save()

        # A merged dependent PR without confirmed merged predecessors is a
        # contradictory roadmap baseline, not a reason to schedule more work.
        for pr in self.config["prs"]:
            pid = pr["id"]
            ent = self.state["prs"].get(pid, {})
            if is_completed(ent.get("stage")):
                for dependency in pr["depends_on"]:
                    parent = self.state["prs"].get(dependency, {})
                    if not is_completed(parent.get("stage")):
                        raise StopWorkflow(
                            f"DEPENDENCY_MERGE_CONFLICT: {pid} requires {dependency}"
                        )

    def preflight(self) -> None:
        """Validate all configured agent/model pairs before starting any PR."""
        for phase, adapter in (
            ("plan", self.planner), ("code", self.coder), ("review", self.reviewer)
        ):
            if adapter is None or not callable(getattr(adapter, "preflight", None)):
                raise StopWorkflow(f"ADAPTER_PREFLIGHT_UNSUPPORTED: phase={phase}")
            identity = adapter.identity()
            if not isinstance(identity, dict):
                raise StopWorkflow(f"ADAPTER_IDENTITY_INVALID: phase={phase}")
            result = adapter.preflight(PreflightTask(
                repo=self.repo, phase=phase,
                timeout_seconds=self.config["command_timeout_seconds"],
            ))
            if not isinstance(result, PreflightResult):
                raise StopWorkflow(f"ADAPTER_PREFLIGHT_INVALID: phase={phase}")
            if not result.ok:
                # Never include raw harness output, credentials or tokens.
                safe_code = result.code if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", result.code) else "UNKNOWN_ERROR"
                raise StopWorkflow(
                    f"ADAPTER_PREFLIGHT_FAILED: phase={phase} "
                    f"adapter={identity.get('adapter', '<unknown>')} "
                    f"model={identity.get('model', '<unknown>')} reason={safe_code}"
                )
            print(f"PREFLIGHT_PASS: phase={phase} adapter={identity.get('adapter')} "
                  f"model={identity.get('model')}", flush=True)

    def run(self) -> int:
        """Preflight every configured agent, then reconcile and run the roadmap."""
        try:
            self.preflight()
            self.reconcile_completed()
        except (StopWorkflow, AgentExecutionError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            print(f"STOP: {exc}", file=sys.stderr)
            return 2
        return self.process(follow=True, max_wait_minutes=None)

    def process(self, *, once=False, follow=False, wait_seconds=30, max_wait_minutes=None):
        current = None
        wait_started = None
        while True:
            pr = self.next_pr()
            if pr is None:
                print("ROADMAP_COMPLETE: every PR confirmed merged")
                return 0
            pid = pr["id"]
            if current is None:
                current = pid
            elif once and current != pid:
                print(f"ONE_PR_COMPLETE: {current}")
                return 0
            if self.entry(pid)["stage"] == "STOPPED":
                print(f"[{pid}] STOPPED: {self.entry(pid).get('stopped_reason')}")
                return 2
            stage_before = self.entry(pid)["stage"]
            try:
                moved = self.advance(pr)
            except (StopWorkflow, AgentExecutionError, subprocess.TimeoutExpired, OSError, ValueError) as e:
                self.stop(pid, f"{type(e).__name__}: {e}")
                return 2
            if stage_before == "WAIT_MERGE" and not moved:
                if not follow:
                    return 3
                if wait_started is None:
                    wait_started = time.monotonic()
                if max_wait_minutes is not None and time.monotonic() - wait_started >= max_wait_minutes * 60:
                    print(f"[{pid}] WAIT_BUDGET_EXHAUSTED: resume later; stage is durable")
                    return 3
                time.sleep(wait_seconds)
            else:
                wait_started = None



class Controller(DefaultOrchestrator):
    """Compatibility shim for the original four-path constructor.

    New integrations should instantiate DefaultOrchestrator(WorkflowContext(...))
    with injected phase adapters.
    """

    def __init__(self, repo: Path, roadmap: Path, state_dir: Path, guide: Path,
                 *, planner: PlanningStep | None = None,
                 coder: CodingStep | None = None,
                 reviewer: ReviewStep | None = None,
                 configuration_sha256: str | None = None,
                 selector: PRSelector | None = None):
        super().__init__(
            WorkflowContext(repo, roadmap, state_dir, guide, configuration_sha256),
            planner=planner, coder=coder, reviewer=reviewer, selector=selector,
        )
