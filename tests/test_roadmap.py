import unittest
from pathlib import Path

from codegrinder.roadmap import RoadmapFormatError, parse_roadmap
from codegrinder.engine import StopWorkflow, validate_roadmap


SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "roadmap.v1.md"


class TestMarkdownRoadmap(unittest.TestCase):
    def test_versioned_markdown_converts_to_controller_contract(self):
        config = parse_roadmap(SAMPLE.read_text(encoding="utf-8"))
        self.assertEqual("v1", config["revision"])
        self.assertEqual(["PR-001", "PR-002"], [p["id"] for p in config["prs"]])
        self.assertEqual([], config["prs"][0]["depends_on"])
        self.assertEqual(["PR-001"], config["prs"][1]["depends_on"])
        validate_roadmap(config)

    def test_unknown_metadata_is_rejected(self):
        with self.assertRaisesRegex(RoadmapFormatError, "unknown or duplicate metadata"):
            parse_roadmap(SAMPLE.read_text().replace("Version: 1", "Version: 1\nFoo: bar"))

    def test_missing_critical_field_is_rejected(self):
        with self.assertRaisesRegex(RoadmapFormatError, "unknown or duplicate PR field"):
            parse_roadmap(SAMPLE.read_text().replace("### Allowed Paths", "### Not Allowed", 1))

    def test_unsupported_version_is_rejected(self):
        with self.assertRaisesRegex(RoadmapFormatError, "supported Version"):
            parse_roadmap(SAMPLE.read_text().replace("Version: 1", "Version: 2"))

    def test_invalid_ordered_dependency_stops(self):
        text = SAMPLE.read_text().replace("- none", "- PR-002", 1)
        with self.assertRaises(StopWorkflow):
            validate_roadmap(parse_roadmap(text))

    def test_missing_pr_heading_is_rejected(self):
        with self.assertRaisesRegex(RoadmapFormatError, "no PR headings"):
            parse_roadmap("# Roadmap\nVersion: 1\n")

    def test_duplicate_required_checks_rejected(self):
        text = SAMPLE.read_text().replace("- REPLACE_WITH_REAL_GITHUB_CHECK_NAME", "- tests\n- tests", 1)
        with self.assertRaisesRegex(RoadmapFormatError, "duplicates"):
            parse_roadmap(text)

    def test_checks_must_be_one_bullet_per_command(self):
        text = SAMPLE.read_text().replace("- REPLACE_WITH_REAL_TEST_COMMAND", "```bash\npytest\n```", 1)
        with self.assertRaisesRegex(RoadmapFormatError, "requires '- item'"):
            parse_roadmap(text)


if __name__ == "__main__":
    unittest.main()
