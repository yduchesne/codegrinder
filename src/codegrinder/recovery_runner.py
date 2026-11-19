"""Fail-closed restart reconciliation for durable Codegrinder attempts.

This controller helper never restarts a coding worker based solely on the
workflow stage. It verifies process identity, worktree fingerprints, unique
attempt artifacts and, for published work, the GitHub PR before advancing.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, NoReturn

from .interfaces import CodingResult
from .coding_reports import CodingReportError, verified_report
from .step_results import StepResult
from .recovery import RecoveryError, process_status, validate_attempt, worktree_snapshot


class RecoveryCoordinator:
    def __init__(self, orchestrator: Any):
        self.c = orchestrator

    def block(self, pid: str, code: str) -> NoReturn:
        from .engine import StopWorkflow
        record = self.c.entry(pid)["execution"]
        record["recovery"] = {"decision": "MANUAL_REQUIRED", "reason": code}
        self.c._save_attempt(pid)
        self.c.logs.emit("RECOVERY_BLOCKED", pr_id=pid, step=record["stage"],
                         state=self.c.entry(pid)["stage"], disposition="FAILED",
                         level="ERROR", error_code=code,
                         execution_status="FAILED", failure_category="RECOVERY")
        raise StopWorkflow(f"{code}: {pid} requires manual reconciliation")

    def resolved(self, pid: str, decision: str, *, retry: bool) -> None:
        record = self.c.entry(pid)["execution"]
        prior = record.get("step_result")
        if prior is not None:
            try:
                result = StepResult(**prior)
            except (TypeError, ValueError):
                self.block(pid, "RECOVERY_STEP_RESULT_INVALID")
            if result.attempt_id != record["id"] or result.stage != record["stage"]:
                self.block(pid, "RECOVERY_STEP_RESULT_MISMATCH")
        else:
            # Recovery itself is not an agent verdict. An interrupted invocation
            # has no meaningful functional result unless terminal stage-specific
            # evidence proves that it completed.
            stage = record["stage"]
            success = decision in {
                "COMMITTED_TRANSITION_RECONCILED", "CODING_RESULT_RECONCILED",
                "PUBLISHED_PR_RECONCILED",
            } and not (
                stage in {"CODE", "CODE_FIXUP"} and self.c.entry(pid)["stage"] == "REPLAN"
            )
            if success:
                next_stage = self.c.entry(pid)["stage"]
                mapping = {
                    "PLAN": ("POSITIVE", "READY"),
                    "REPLAN": ("POSITIVE", "READY"),
                    "CODE": ("POSITIVE", "COMPLETED"),
                    "CODE_FIXUP": ("POSITIVE", "COMPLETED"),
                    "VERIFY": ("NEGATIVE", "CHECKS_FAILED") if next_stage == "REPLAN"
                              else ("POSITIVE", "CHECKS_PASSED"),
                    "REVIEW": ("NEGATIVE", "CHANGES_REQUIRED") if next_stage == "REPLAN"
                              else ("POSITIVE", "PASS"),
                    "PUBLISH": ("POSITIVE", "PUBLISHED"),
                    "WAIT_MERGE": ("POSITIVE", "MERGED"),
                }
                outcome, verdict = mapping[stage]
                self.c._record_step_result(
                    pid, stage=stage, outcome=outcome, verdict=verdict,
                )
            else:
                self.c._record_step_result(
                    pid, stage=stage, execution_status="INTERRUPTED",
                    failure_category="RECOVERY", error_code=decision,
                )
        record["recovery"] = {"decision": decision}
        self.c._finish_attempt(pid, "INTERRUPTED" if retry else "RECOVERED", decision)

    def snapshot(self, pid: str) -> dict:
        from .engine import git, StopWorkflow
        wt = self.c.wt(pid)
        if not (wt / ".git").exists():
            self.block(pid, "RECOVERY_WORKTREE_MISSING")
        try:
            return worktree_snapshot(wt, git)
        except (RecoveryError, OSError, ValueError, StopWorkflow):
            self.block(pid, "RECOVERY_WORKTREE_UNREADABLE")

    def completed_coding(self, pid: str, record: dict) -> CodingResult | None:
        from .engine import load_json
        data = record.get("result")
        if data is None:
            history = self.c.entry(pid).get("coding_results", [])
            if not isinstance(history, list):
                self.block(pid, "RECOVERY_RESULT_HISTORY_INVALID")
            matches = [
                item for item in history if isinstance(item, dict)
                and item.get("attempt_id") == record["id"]
            ]
            if len(matches) > 1:
                self.block(pid, "RECOVERY_RESULT_AMBIGUOUS")
            if len(matches) == 1:
                data = matches[0]
        if data is None:
            phase = "code" if record["stage"] == "CODE" else "fixup"
            number = self.c.entry(pid).get("fixups", 0)
            path = self.c.artifact(pid) / f"{phase}-{number}-{record['id']}.result.json"
            if not path.exists():
                return None
            try:
                if path.stat().st_size > 32768:
                    self.block(pid, "RECOVERY_RESULT_TOO_LARGE")
                data = load_json(path)
            except (OSError, ValueError, TypeError):
                self.block(pid, "RECOVERY_RESULT_INVALID")
        if not isinstance(data, dict):
            self.block(pid, "RECOVERY_RESULT_INVALID")
        try:
            result = CodingResult(**data)
        except (TypeError, ValueError):
            self.block(pid, "RECOVERY_RESULT_INVALID")
        code_agent = self.c.agents.get("code") or {}
        phase = "code" if record["stage"] == "CODE" else "fixup"
        if (result.attempt_id != record["id"] or result.pr_id != pid or
                result.phase != phase or result.status != "COMPLETED" or
                result.exit_code != 0 or result.event_count < 1 or
                result.model != code_agent.get("model")):
            self.block(pid, "RECOVERY_RESULT_MISMATCH")
        try:
            verified_report(
                result, self.c.artifact(pid), pr_id=pid, phase=phase,
                fixup_number=self.c.entry(pid).get("fixups", 0),
                attempt_id=record["id"],
            )
        except CodingReportError:
            self.block(pid, "RECOVERY_CODING_REPORT_INVALID")
        return result

    def published_evidence(self, pid: str) -> None:
        from .engine import run, StopWorkflow
        ent = self.c.entry(pid)
        try:
            output = run([
                "gh", "pr", "view", str(ent["pr_number"]),
                "--json", "number,url,headRefOid,baseRefName",
            ], self.c.wt(pid), timeout=120)
            info = json.loads(output.stdout)
        except (StopWorkflow, OSError, TypeError, ValueError, KeyError):
            self.block(pid, "RECOVERY_PUBLISH_EVIDENCE_UNAVAILABLE")
        if (not isinstance(info, dict) or info.get("number") != ent["pr_number"]
                or info.get("headRefOid") != ent["published_head"]
                or info.get("baseRefName") != self.c.config["base_branch"]):
            self.block(pid, "RECOVERY_PUBLISH_EVIDENCE_CONFLICT")

    def reconcile_publish(self, pid: str) -> None:
        """Only adopt a uniquely confirmed published PR; never blindly recreate."""
        from .engine import git, run, StopWorkflow
        wt = self.c.wt(pid)
        branch = self.c._branch(pid)
        try:
            if git(wt, "branch", "--show-current") != branch:
                self.block(pid, "RECOVERY_WRONG_BRANCH")
            sha = git(wt, "rev-parse", "HEAD")
            output = run([
                "gh", "pr", "view", branch,
                "--json", "number,url,headRefOid,baseRefName",
            ], wt, timeout=120)
            data = json.loads(output.stdout)
        except (OSError, ValueError, StopWorkflow):
            self.block(pid, "RECOVERY_PUBLISH_UNCONFIRMED")
        if (not isinstance(data, dict) or
                data.get("baseRefName") != self.c.config["base_branch"] or
                data.get("headRefOid") != sha or
                type(data.get("number")) is not int or
                not isinstance(data.get("url"), str)):
            self.block(pid, "RECOVERY_PUBLISH_MISMATCH")
        ent = self.c.entry(pid)
        if ent.get("published_head") not in (None, sha):
            self.block(pid, "RECOVERY_PUBLISHED_HEAD_CONFLICT")
        ent.update({
            "pr_number": data["number"], "pr_url": data["url"],
            "published_head": sha,
        })
        self.c.save()
        self.c.transition(pid, "REVIEW", "recovered confirmed published PR")

    def _changed_stage(self, pid: str, record: dict, stage: str) -> None:
        ent = self.c.entry(pid)
        previous = record["stage"]
        allowed = {
            "PLAN": {"CODE"}, "REPLAN": {"CODE_FIXUP"},
            "CODE": {"VERIFY", "REPLAN"}, "CODE_FIXUP": {"VERIFY", "REPLAN"},
            "VERIFY": {"PUBLISH", "REPLAN"},
            "PUBLISH": {"REVIEW"}, "REVIEW": {"WAIT_MERGE", "REPLAN"},
            "WAIT_MERGE": {"DONE"},
        }
        if stage not in allowed.get(previous, set()):
            self.block(pid, "RECOVERY_STAGE_CONFLICT")
        if previous in {"PLAN", "REPLAN", "CODE", "CODE_FIXUP", "VERIFY", "REVIEW"}:
            snapshot = self.snapshot(pid)
            baseline = record.get("baseline")
            if not isinstance(baseline, dict) or snapshot["head"] != baseline.get("head"):
                self.block(pid, "RECOVERY_TRANSITION_HEAD_CHANGED")
            if previous in {"PLAN", "REPLAN", "VERIFY", "REVIEW"} and (
                snapshot["dirty"] != baseline.get("dirty")
                or snapshot["fingerprint"] != baseline.get("fingerprint")
            ):
                self.block(pid, "RECOVERY_TRANSITION_WORKTREE_CHANGED")
            if previous in {"CODE", "CODE_FIXUP"}:
                from .engine import modified_paths, validate_paths, StopWorkflow
                pr = next(item for item in self.c.config["prs"] if item["id"] == pid)
                try:
                    validate_paths(
                        modified_paths(self.c.wt(pid), self.c.config["base_branch"]),
                        pr["allowed_paths"],
                    )
                except StopWorkflow:
                    self.block(pid, "RECOVERY_SCOPE_VIOLATION")
        if previous in {"PLAN", "REPLAN"}:
            filename = "plan.md" if previous == "PLAN" else f"plan-fixup-{ent['fixups']}.md"
            path = self.c.artifact(pid) / filename
            if ent.get("active_plan") != filename or not path.is_file():
                self.block(pid, "RECOVERY_PLAN_EVIDENCE_MISSING")
            # The existing plan must remain a validated, nonempty artifact.
            text = path.read_text(encoding="utf-8")
            if len(text) < 300 or not all(
                heading in text for heading in (
                    "## Scope", "## Steps", "## Tests",
                    "## Decision Matrix", "## Stop Conditions",
                )
            ):
                self.block(pid, "RECOVERY_PLAN_EVIDENCE_INVALID")
        if previous in {"CODE", "CODE_FIXUP"}:
            if stage == "VERIFY":
                result = self.completed_coding(pid, record)
                if result is None:
                    self.block(pid, "RECOVERY_CODING_EVIDENCE_MISSING")
                ent["latest_coding_attempt"] = record["id"]
                if not any(
                    item.get("attempt_id") == record["id"]
                    for item in ent.get("coding_results", [])
                ):
                    ent.setdefault("coding_results", []).append(asdict(result))
                self.c.save()
            elif not ent.get("active_feedback"):
                self.block(pid, "RECOVERY_CONTINUATION_FEEDBACK_MISSING")
        if previous == "PUBLISH":
            if not ent.get("pr_number") or not ent.get("published_head"):
                self.block(pid, "RECOVERY_PUBLISH_EVIDENCE_MISSING")
            self.published_evidence(pid)
        if previous == "REVIEW":
            if stage == "WAIT_MERGE" and not ent.get("accepted_head"):
                self.block(pid, "RECOVERY_REVIEW_EVIDENCE_MISSING")
            if stage == "REPLAN" and not ent.get("active_feedback"):
                self.block(pid, "RECOVERY_REVIEW_EVIDENCE_MISSING")
        if previous == "WAIT_MERGE" and not (ent.get("merged_at") and ent.get("merge_commit")):
            self.block(pid, "RECOVERY_MERGE_EVIDENCE_MISSING")
        self.resolved(pid, "COMMITTED_TRANSITION_RECONCILED", retry=False)

    def _same_stage(self, pr: dict, record: dict) -> None:
        from .engine import modified_paths, validate_paths, StopWorkflow
        pid = pr["id"]
        original = record["stage"]
        baseline = record.get("baseline")
        if baseline is None:
            if record.get("launch_state") != "NOT_STARTED":
                self.block(pid, "RECOVERY_BASELINE_UNKNOWN")
            self.resolved(pid, "PREPARATION_RETRY", retry=True)
            return
        current = self.snapshot(pid)
        if current["head"] != baseline.get("head") and original != "PUBLISH":
            self.block(pid, "RECOVERY_HEAD_CHANGED")
        if original in {"CODE", "CODE_FIXUP"}:
            result = self.completed_coding(pid, record)
            if result is not None:
                if current["head"] != baseline.get("head"):
                    self.block(pid, "RECOVERY_CODING_HEAD_CHANGED")
                try:
                    validate_paths(
                        modified_paths(self.c.wt(pid), self.c.config["base_branch"]),
                        pr["allowed_paths"],
                    )
                except StopWorkflow:
                    self.block(pid, "RECOVERY_SCOPE_VIOLATION")
                ent = self.c.entry(pid)
                ent["latest_coding_attempt"] = record["id"]
                if not any(item.get("attempt_id") == record["id"]
                           for item in ent.get("coding_results", [])):
                    ent.setdefault("coding_results", []).append(asdict(result))
                    self.c.save()
                self.c.transition(pid, "VERIFY", "recovered completed coding result")
                self.resolved(pid, "CODING_RESULT_RECONCILED", retry=False)
                return
            if record.get("launch_state") == "NOT_STARTED" and current == baseline:
                self.resolved(pid, "CODING_NOT_LAUNCHED_RETRY", retry=True)
                return
            if record.get("process") is None:
                self.block(pid, "RECOVERY_CODING_PROCESS_UNKNOWN")
            if (current["dirty"] == baseline.get("dirty") and
                    current["fingerprint"] == baseline.get("fingerprint")):
                self.block(pid, "RECOVERY_CODING_OUTCOME_UNKNOWN")
            try:
                validate_paths(
                    modified_paths(self.c.wt(pid), self.c.config["base_branch"]),
                    pr["allowed_paths"],
                )
            except StopWorkflow:
                self.block(pid, "RECOVERY_SCOPE_VIOLATION")
            self.c._start_replan(
                pid, source="interrupted_coding",
                summary=f"Interrupted {original} attempt {record['id']}; partial edits preserved",
                findings=[{
                    "id": "RECOVERY-001", "severity": "high", "file": "<worktree>",
                    "description": "Coding process exited without a valid terminal result. "
                                   "The worktree differs from its recorded baseline.",
                    "required_fix": "Inspect the partial edits and produce a bounded "
                                    "continuation plan, with tests and stop conditions.",
                }],
            )
            self.resolved(pid, "PARTIAL_CODING_REPLAN", retry=False)
            return
        if original in {"PLAN", "REPLAN", "REVIEW", "VERIFY"}:
            if (current["fingerprint"] != baseline.get("fingerprint")
                    or current["dirty"] != baseline.get("dirty")):
                self.block(pid, "RECOVERY_WORKTREE_CHANGED")
            if original in {"PLAN", "REVIEW"} and current["dirty"]:
                self.block(pid, "RECOVERY_READONLY_STEP_DIRTY")
            if record.get("launch_state") == "LAUNCHING" and record.get("process") is None:
                self.block(pid, "RECOVERY_AGENT_PROCESS_UNKNOWN")
            self.resolved(pid, "SAFE_STEP_RETRY", retry=True)
            return
        if original == "PUBLISH":
            self.reconcile_publish(pid)
            self.resolved(pid, "PUBLISHED_PR_RECONCILED", retry=False)
            return
        if original == "WAIT_MERGE":
            self.resolved(pid, "MERGE_QUERY_RETRY", retry=True)
            return
        self.block(pid, "RECOVERY_UNSUPPORTED_STAGE")

    def reconcile(self) -> None:
        """No agent is invoked until all RUNNING attempts have been resolved."""
        for pr in self.c.config["prs"]:
            pid = pr["id"]
            ent = self.c.state["prs"].get(pid, {})
            record = ent.get("execution")
            if record is None and ent.get("stage") in {
                "CODE", "CODE_FIXUP", "VERIFY", "PUBLISH", "REVIEW", "REPLAN",
            }:
                # Old versions have no per-invocation evidence. Never execute
                # a potentially repeated step merely because its stage exists.
                from .engine import StopWorkflow
                raise StopWorkflow(f"RECOVERY_LEGACY_STATE_REQUIRES_RECONCILIATION: {pid}")
            if isinstance(record, dict) and record.get("status") == "FAILED":
                if ent.get("stage") == record.get("stage"):
                    from .engine import StopWorkflow
                    raise StopWorkflow(f"RECOVERY_FAILED_ATTEMPT_REQUIRES_RECONCILIATION: {pid}")
            if not isinstance(record, dict) or record.get("status") != "RUNNING":
                continue
            try:
                record = validate_attempt(record, pid)
            except RecoveryError as exc:
                self.block(pid, str(exc))
            path = self.c._attempt_path(pid, record["id"])
            try:
                if not path.is_file():
                    self.block(pid, "RECOVERY_AUDIT_MISSING")
                if path.is_symlink() or path.stat().st_size > 1024 * 1024:
                    self.block(pid, "RECOVERY_AUDIT_UNTRUSTED")
                audited = json.loads(path.read_text(encoding="utf-8"))
                if (not isinstance(audited, dict) or audited.get("id") != record["id"]
                        or audited.get("stage") != record["stage"]):
                    self.block(pid, "RECOVERY_AUDIT_MISMATCH")
            except (OSError, ValueError, TypeError):
                self.block(pid, "RECOVERY_AUDIT_UNREADABLE")
            process = record.get("process")
            if process is not None:
                status = process_status(process)
                if status == "ACTIVE":
                    self.block(pid, "RECOVERY_PREVIOUS_PROCESS_ACTIVE")
                if status == "UNKNOWN":
                    self.block(pid, "RECOVERY_PROCESS_IDENTITY_UNKNOWN")
            stage = ent.get("stage")
            if stage != record["stage"]:
                self._changed_stage(pid, record, stage)
            else:
                self._same_stage(pr, record)
