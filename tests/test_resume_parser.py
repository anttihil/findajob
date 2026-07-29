"""Resume and achievements parsing tests.

The regression these guard against is specific: the previous parser matched only
"## Skills" (3 of 7 files) and deleted parentheticals, which silently dropped
EC2/S3/IAM/VPC/SSM and FastAPI/Flask from every profile.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.resume_parser import (  # noqa: E402
    AchievementsParser,
    ResumeParser,
    build_skill_evidence,
    extract_skill_candidates,
    parse_projects,
    parse_skills_section,
    parse_technology_summary,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RESUMES_DIR = os.path.join(REPO_ROOT, "resumes")
ACHIEVEMENTS = os.path.join(REPO_ROOT, "achievements.md")
CURRENT_RESUME = os.path.join(REPO_ROOT, "current_resume.md")


class CandidateExtractionTests(unittest.TestCase):
    def test_parenthetical_children_are_expanded_not_deleted(self):
        """The headline bug: the old parser ran re.sub(r"\\([^)]+\\)", "", ...)."""
        got = extract_skill_candidates("AWS (EC2, S3, IAM, VPC, SSM), Terraform")
        for expected in ["AWS", "EC2", "S3", "IAM", "VPC", "SSM", "Terraform"]:
            self.assertIn(expected, got)

    def test_nested_slash_inside_parenthetical(self):
        got = extract_skill_candidates("Python (FastAPI, Flask), Node.js")
        for expected in ["Python", "FastAPI", "Flask", "Node.js"]:
            self.assertIn(expected, got)

    def test_top_level_comma_split_respects_parentheses(self):
        """"AWS (EC2, S3), Terraform" is two items, not four."""
        got = extract_skill_candidates("AWS (EC2, S3), Terraform")
        self.assertIn("Terraform", got)
        self.assertNotIn("S3), Terraform", got)

    def test_slash_compounds_emit_both_joined_and_split(self):
        """CI/CD is meaningful whole; Ansible/Trellis needs splitting. Emit all forms and
        let the taxonomy keep what it recognizes."""
        got = extract_skill_candidates("Ansible/Trellis, CI/CD")
        self.assertIn("Ansible/Trellis", got)
        self.assertIn("Ansible", got)
        self.assertIn("Trellis", got)
        self.assertIn("CI/CD", got)

    def test_backticks_and_bold_are_stripped(self):
        got = extract_skill_candidates("GPU-accelerated OCR (`ocrmypdf`), **VeraPDF**")
        self.assertIn("ocrmypdf", got)
        self.assertIn("VeraPDF", got)
        self.assertNotIn("`ocrmypdf`", got)

    def test_trailing_qualifier_is_discarded_but_head_kept(self):
        got = extract_skill_candidates("Create React App (legacy), uv (Python packaging)")
        self.assertIn("Create React App", got)
        self.assertIn("uv", got)
        self.assertNotIn("legacy", got)

    def test_stop_fragments_are_dropped(self):
        got = [c.lower() for c in extract_skill_candidates("CI/CD, and, etc")]
        self.assertNotIn("and", got)
        self.assertNotIn("etc", got)

    def test_candidates_are_deduplicated(self):
        got = extract_skill_candidates("Python, Python, python")
        self.assertEqual(got.count("Python"), 1)


class SkillsSectionTests(unittest.TestCase):
    def test_all_four_heading_variants_match(self):
        for heading in [
            "Skills",
            "Technical Competencies",
            "Core Competencies",
            "Technical & Professional Competencies",
        ]:
            content = f"# Name\n\n## {heading}\n* **Backend:** Python, Go\n\n## Next\n"
            categories = parse_skills_section(content)
            self.assertTrue(categories, f"heading '{heading}' did not match")
            self.assertIn("Backend", categories)

    def test_both_bullet_indent_styles(self):
        one_space = "## Skills\n* **A:** Python\n"
        three_space = "## Skills\n*   **A:** Python\n"
        for content in (one_space, three_space):
            self.assertIn("Python", parse_skills_section(content)["A"])

    def test_bullet_without_category_prefix(self):
        categories = parse_skills_section("## Skills\n* Python, Go\n")
        self.assertIn("General", categories)
        self.assertIn("Python", categories["General"])

    def test_section_stops_at_next_heading(self):
        content = "## Skills\n* **A:** Python\n\n## Experience\n* **B:** NotASkill\n"
        categories = parse_skills_section(content)
        self.assertNotIn("B", categories)


class RealResumeFileTests(unittest.TestCase):
    """Runs against the actual resume set, not fixtures -- these files are the contract."""

    def setUp(self):
        if not os.path.isdir(RESUMES_DIR):
            self.skipTest("resumes/ directory not present")

    def test_every_resume_file_yields_a_skills_section(self):
        results = ResumeParser(RESUMES_DIR).parse_all()
        self.assertGreaterEqual(len(results), 6)
        missing = [n for n, i in results.items() if not i["has_skills_section"]]
        self.assertEqual(missing, [], f"files with no matched skills section: {missing}")

    def test_every_resume_yields_a_useful_number_of_skills(self):
        for name, info in ResumeParser(RESUMES_DIR).parse_all().items():
            self.assertGreater(
                len(info["skills"]), 15, f"{name} produced only {len(info['skills'])}"
            )

    def test_current_resume_aws_children_and_go(self):
        if not os.path.exists(CURRENT_RESUME):
            self.skipTest("current_resume.md not present")
        info = ResumeParser(REPO_ROOT).parse_file(CURRENT_RESUME)
        for expected in ["AWS", "EC2", "S3", "IAM", "VPC", "SSM"]:
            self.assertIn(expected, info["skills"])
        # Go appears only in current_resume.md; the runtime parser scans resumes/ only,
        # so taxonomy bootstrapping must read this file too or Go is invisible.
        self.assertTrue({"Go", "Golang"} & set(info["skills"]))


class AchievementsTests(unittest.TestCase):
    def setUp(self):
        if not os.path.exists(ACHIEVEMENTS):
            self.skipTest("achievements.md not present")

    def test_technology_summary_table_parses(self):
        summary = parse_technology_summary(open(ACHIEVEMENTS).read())
        self.assertGreaterEqual(len(summary), 10)
        self.assertIn("Languages", summary)
        self.assertIn("TypeScript", summary["Languages"])
        # The |---|---| separator row must not become a category.
        self.assertNotIn("Category", summary)

    def test_all_four_project_date_formats_parse(self):
        """"Jan 2025 - present", "Feb - Mar 2026", "Mar 2026", and "Ongoing" all occur."""
        projects = parse_projects(open(ACHIEVEMENTS).read())
        self.assertGreaterEqual(len(projects), 8)
        dates = " ".join(p["dates"] for p in projects)
        self.assertIn("present", dates)
        self.assertIn("Ongoing", dates)

    def test_commit_and_pr_volume_extracted(self):
        projects = parse_projects(open(ACHIEVEMENTS).read())
        top = max(projects, key=lambda p: p["commits"])
        self.assertGreater(top["commits"], 100)
        self.assertGreater(top["merged_prs"], 0)

    def test_ongoing_counts_as_current(self):
        projects = parse_projects(open(ACHIEVEMENTS).read())
        ongoing = [p for p in projects if p["dates"].lower().startswith("ongoing")]
        self.assertTrue(ongoing)
        self.assertTrue(all(p["is_current"] for p in ongoing))

    def test_skill_evidence_accumulates_across_projects(self):
        result = AchievementsParser(ACHIEVEMENTS).parse()
        evidence = result["skill_evidence"]
        self.assertGreater(len(evidence), 20)
        # Terraform appears in more than one project, so its evidence should be merged.
        terraform = evidence.get("Terraform")
        self.assertIsNotNone(terraform)
        self.assertGreaterEqual(len(terraform["projects"]), 2)

    def test_prose_italic_without_stack_is_not_a_project(self):
        """"*Ongoing side work - useful for ... framing*" has no pipe-delimited stack."""
        projects = parse_projects(open(ACHIEVEMENTS).read())
        names = [p["name"] for p in projects]
        self.assertNotIn("Personal / Independent Projects", names)


class SkillEvidenceTests(unittest.TestCase):
    def test_current_project_flag_propagates(self):
        evidence = build_skill_evidence([
            {"name": "Old", "tech": ["Python"], "commits": 10,
             "merged_prs": 1, "is_current": False},
            {"name": "New", "tech": ["Python"], "commits": 5,
             "merged_prs": 2, "is_current": True},
        ])
        self.assertEqual(evidence["Python"]["commits"], 15)
        self.assertEqual(evidence["Python"]["merged_prs"], 3)
        self.assertTrue(evidence["Python"]["is_current"])
        self.assertEqual(len(evidence["Python"]["projects"]), 2)


if __name__ == "__main__":
    unittest.main()
