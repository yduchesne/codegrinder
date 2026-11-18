"""Packaged prompt selection, rendering, validation and precedence tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codegrinder.prompt_templates import (
    PROMPT_ROOT, PromptTemplateError, augment_prompt, supplemental_prompt,
)


class TestPromptTemplates(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "prompts"

    def write(self, path: str, content: str) -> Path:
        destination = self.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        return destination

    def values(self, step: str, state: str) -> dict[str, str]:
        data = {
            "STATE": state,
            "STEP": step,
            "PR_ID": "BM-01",
            "PATH_ROADMAP": "/tmp/Roadmaps/my roadmap.md",
            "PATH_AUTHORING_GUIDE": "/tmp/Guides/authoring guide.md",
        }
        if step == "plan":
            data["PATH_GENERATED_PLAN"] = "/tmp/state/artifacts/BM-01/plan.md"
        return data

    def render(self, step: str = "plan", state: str = "PLAN") -> str:
        return supplemental_prompt(root=self.root, step=step, state=state, variables=self.values(step, state))

    def test_packaged_general_plan_prompt_is_present(self):
        self.assertTrue((PROMPT_ROOT / "plan" / "general.prompt.txt").is_file())
        content = supplemental_prompt(
            step="plan", state="PLAN", variables=self.values("plan", "PLAN"),
        )
        self.assertIn("Provide a detailed plan for PR BM-01", content)
        self.assertIn("low-reasoning coding agent", content)
        self.assertIn("the host is idle and ready to run performance benchmarking", content)
        self.assertIn("/tmp/state/artifacts/BM-01/plan.md", content)
        self.assertIn("/tmp/Guides/authoring guide.md", content)
        self.assertIn("do not create or modify files", content)
        self.assertNotIn("{{", content)

    def test_step_fallback_and_all_defined_variables(self):
        self.write("plan/01.prompt.txt", (
            "{{STATE}} | {{STEP}} | {{PR_ID}} | {{PATH_ROADMAP}} | "
            "{{PATH_AUTHORING_GUIDE}} | {{PATH_GENERATED_PLAN}}"
        ))
        result = self.render()
        self.assertEqual(
            "PLAN | plan | BM-01 | /tmp/Roadmaps/my roadmap.md | "
            "/tmp/Guides/authoring guide.md | /tmp/state/artifacts/BM-01/plan.md",
            result,
        )
        self.assertEqual(
            "REPLAN", self.render("plan", "REPLAN").split(" | ")[0],
        )

    def test_state_specific_overrides_general_without_merging(self):
        self.write("plan/general.prompt.txt", "STEP-GENERAL")
        self.write("plan/replan/02.prompt.txt", "STATE-SECOND")
        self.write("plan/replan/01.prompt.txt", "STATE-FIRST")
        self.assertEqual("STEP-GENERAL", self.render("plan", "PLAN"))
        self.assertEqual("STATE-FIRST\n\nSTATE-SECOND", self.render("plan", "REPLAN"))

    def test_existing_empty_state_directory_suppresses_general(self):
        self.write("plan/general.prompt.txt", "STEP-GENERAL")
        (self.root / "plan" / "replan").mkdir()
        self.assertEqual("", self.render("plan", "REPLAN"))

    def test_lowercase_directories_and_suffix_filter(self):
        self.write("code/general.prompt.txt", "CODE-GENERAL")
        self.write("code/CODE_FIXUP/ignored.prompt.txt", "WRONG-CASE")
        self.write("code/code_fixup/valid.prompt.txt", "{{STATE}} {{STEP}} {{PR_ID}}")
        self.write("code/code_fixup/not-a-prompt.txt", "IGNORED")
        self.assertEqual("CODE-GENERAL", self.render("code", "CODE"))
        self.assertEqual("CODE_FIXUP code BM-01", self.render("code", "CODE_FIXUP"))
        self.assertEqual("", self.render("review", "REVIEW"))
        self.write("review/review/guidance.prompt.txt", "REVIEW-ONLY")
        self.assertEqual("REVIEW-ONLY", self.render("review", "REVIEW"))

    def test_multiple_files_sorted_and_base_prompt_preserved(self):
        self.write("plan/z.prompt.txt", "second")
        self.write("plan/a.prompt.txt", "first")
        self.write("plan/wrong.txt", "ignored")
        self.assertEqual(
            "ORIGINAL\n\nfirst\n\nsecond",
            augment_prompt("ORIGINAL", root=self.root, step="plan", state="PLAN",
                           variables=self.values("plan", "PLAN")),
        )
        self.assertEqual(
            "ORIGINAL",
            augment_prompt("ORIGINAL", root=self.root, step="code", state="CODE",
                           variables=self.values("code", "CODE")),
        )

    def test_unknown_unavailable_and_malformed_variables_stop(self):
        for template in ("{{UNLISTED}}", "{{PATH_GENERATED_PLAN}}", "{{BROKEN", "{{ }}"):
            self.write("code/general.prompt.txt", template)
            with self.subTest(template=template), self.assertRaisesRegex(
                PromptTemplateError, "PROMPT_UNKNOWN_VARIABLE|PROMPT_MALFORMED_VARIABLE"
            ):
                self.render("code", "CODE")

    def test_wrong_step_state_or_values_stop(self):
        with self.assertRaisesRegex(PromptTemplateError, "PROMPT_INVALID_STEP_STATE"):
            self.render("code", "REVIEW")
        values = self.values("plan", "PLAN")
        del values["PATH_GENERATED_PLAN"]
        with self.assertRaisesRegex(PromptTemplateError, "PROMPT_VARIABLES_INVALID"):
            supplemental_prompt(root=self.root, step="plan", state="PLAN", variables=values)

    def test_unreadable_encoding_and_symlink_stop(self):
        bad = self.write("plan/bad.prompt.txt", "fine")
        bad.write_bytes(b"\xff\xfe")
        with self.assertRaisesRegex(PromptTemplateError, "PROMPT_UNREADABLE"):
            self.render()
        bad.unlink()
        outside = self.root.parent / "outside.prompt.txt"
        outside.write_text("UNTRUSTED")
        bad.symlink_to(outside)
        with self.assertRaisesRegex(PromptTemplateError, "PROMPT_NOT_REGULAR_FILE"):
            self.render()

    def test_state_specific_path_must_be_directory(self):
        self.write("plan/general.prompt.txt", "fine")
        self.write("plan/replan", "not a directory")
        with self.assertRaisesRegex(PromptTemplateError, "PROMPT_NOT_DIRECTORY"):
            self.render("plan", "REPLAN")


if __name__ == "__main__":
    unittest.main()
