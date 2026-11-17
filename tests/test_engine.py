import json
import sys
import tempfile
import unittest
from pathlib import Path

from codegrinder.engine import (StopWorkflow, validate_roadmap, validate_plan, validate_review,
                    validate_paths, check_pi_events, atomic_json)


def sample():
    return {
        "version": 1, "base_branch": "main", "merge_mode": "manual", "required_checks": ["tests"],
        "max_fixups": 2, "command_timeout_seconds": 900,
        "prs": [{"id": "p1", "title": "First", "goal": "Build first piece", "depends_on": [],
                 "acceptance": ["Pass fixture"], "allowed_paths": ["src/**"], "checks": ["true"]},
                {"id": "p2", "title": "Second", "goal": "Build second piece", "depends_on": ["p1"],
                 "acceptance": ["Pass fixture"], "allowed_paths": ["src/**"], "checks": ["true"]}]
    }

class TestRoadmap(unittest.TestCase):
    def test_valid(self):
        validate_roadmap(sample())

    def test_dependency_must_be_earlier(self):
        cfg = sample()
        cfg["prs"][0]["depends_on"] = ["p2"]
        with self.assertRaises(StopWorkflow): validate_roadmap(cfg)

    def test_duplicate_id(self):
        cfg = sample()
        cfg["prs"][1]["id"] = "p1"
        with self.assertRaises(StopWorkflow): validate_roadmap(cfg)

    def test_scope_fail_closed(self):
        validate_paths({"src/app.py"}, ["src/**"])
        with self.assertRaises(StopWorkflow): validate_paths({"src/app.py", ".github/workflows/ci.yml"}, ["src/**"])

    def test_plan_headings(self):
        plan = "## Scope\n" + "x" * 300 + "\n## Steps\n## Tests\n## Decision Matrix\n## Stop Conditions"
        self.assertEqual(plan, validate_plan({"status": "READY", "plan_markdown": plan}))
        with self.assertRaises(StopWorkflow): validate_plan({"status": "BLOCKED", "blocker": "missing interface"})

    def test_review_mismatch(self):
        self.assertEqual(("PASS", []), validate_review({"verdict": "PASS", "findings": [], "summary": "fine"}))
        with self.assertRaises(StopWorkflow):
            validate_review({"verdict": "CHANGES_REQUIRED", "findings": []})

    def test_pi_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "events.jsonl"
            p.write_text('\n'.join([json.dumps({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}}),
                                    json.dumps({"type": "agent_settled", "aborted": False})]))
            check_pi_events(p)
            p.write_text(json.dumps({"type": "agent_settled", "aborted": True}))
            with self.assertRaises(StopWorkflow): check_pi_events(p)

    def test_atomic_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "s.json"
            atomic_json(p, {"x": 1})
            self.assertEqual({"x": 1}, json.loads(p.read_text()))

if __name__ == "__main__": unittest.main()
