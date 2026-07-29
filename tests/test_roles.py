"""Role normalization tests.

Fixtures are drawn from the 121 real titles already in jobs.db, including the messy cases
that motivated the design: Swedish titles, multi-role titles, and non-title garbage that
earlier ingestion wrote into the title column.
"""

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.roles import SENIORITY_UNSPECIFIED, load_roles  # noqa: E402

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "jobs.db"
)


class RoleTaxonomyIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.roles = load_roles()

    def test_validates_clean(self):
        problems = self.roles.validate()
        self.assertEqual(problems, [], f"role taxonomy problems: {problems}")

    def test_has_wide_catalog(self):
        self.assertGreaterEqual(len(self.roles), 25)

    def test_generic_catch_all_is_declared_last(self):
        """software_engineer must lose positional ties to every specific family."""
        self.assertEqual(list(self.roles.families)[-1], "software_engineer")

    def test_all_six_resume_variants_are_reachable(self):
        mapped = {f.resume for f in self.roles.families.values() if f.resume}
        for expected in [
            "fullstack_ai_engineer.md", "genai_application_engineer.md",
            "platform_devops_engineer.md", "academic_technology_manager.md",
            "edtech_product_program_manager.md",
        ]:
            self.assertIn(expected, mapped)

    def test_nordic_locations_are_configured(self):
        for location_id in ["us_remote", "la", "fi", "se", "no", "dk"]:
            self.assertIn(location_id, self.roles.locations)
        self.assertEqual(self.roles.locations["fi"].indeed_country, "finland")
        self.assertEqual(self.roles.locations["se"].indeed_country, "sweden")

    def test_remote_location_is_flagged_remote(self):
        self.assertTrue(self.roles.locations["us_remote"].is_remote)
        self.assertFalse(self.roles.locations["la"].is_remote)


class SeniorityTests(unittest.TestCase):
    def setUp(self):
        self.roles = load_roles()

    def test_common_seniority_markers(self):
        cases = [
            ("Senior Platform Engineer", "senior"),
            ("Sr. Software Engineer (Elixir/React)", "senior"),
            ("Staff Software Engineer", "staff"),
            ("Principal Software Engineer", "staff"),
            ("Junior Software Engineer", "junior"),
            ("Software Engineer", SENIORITY_UNSPECIFIED),
            ("Engineering Manager", "lead"),
            ("Lead Software Engineer", "lead"),
        ]
        for title, expected in cases:
            self.assertEqual(self.roles.seniority(title), expected, title)

    def test_open_range_reads_as_the_lower_bound(self):
        """"Junior to Senior" is honestly junior-eligible, so it should not read senior."""
        self.assertEqual(
            self.roles.seniority("Junior to Senior Fullstack Engineer multiple positions"),
            "junior",
        )

    def test_founding_engineer_reads_as_senior(self):
        self.assertEqual(self.roles.seniority("Founding Engineer (Full-Stack)"), "senior")


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.roles = load_roles()

    def _family(self, title):
        return self.roles.classify(title)[0]

    def test_specific_families_beat_the_generic_catch_all(self):
        cases = [
            ("Senior Platform Engineer", "platform_engineer"),
            ("DevOps Engineer", "devops_engineer"),
            ("Senior AI Engineer (Python)", "ai_engineer"),
            ("Full Stack Developer (.NET/React)", "fullstack_engineer"),
            ("Backend Software Engineer", "backend_engineer"),
            ("Site Reliability & DevOps Engineer", "sre"),
            ("Forward Deployed Engineer - backend - python",
             "forward_deployed_engineer"),
            ("Senior Data & AI Platform Engineer till Axfood IT", "mlops_engineer"),
            ("Trust & Safety Engineer", "security_engineer"),
            ("Edge AI Engineer", "ai_engineer"),
            ("Integration engineer", "integration_engineer"),
            ("Mobile Engineer", "mobile_engineer"),
            ("Senior Embedded Software Engineer", "embedded_engineer"),
            ("Cloud Engineer Jönköping", "cloud_engineer"),
        ]
        for title, expected in cases:
            self.assertEqual(self._family(title), expected, title)

    def test_generic_titles_fall_through_to_software_engineer(self):
        for title in ["Software Engineer", "Senior Software Engineer",
                      "Software Engineers", "Python + TypeScript Engineers",
                      "Various Software Engineering Roles"]:
            self.assertEqual(self._family(title), "software_engineer", title)

    def test_plural_engineers_still_classifies(self):
        """\\bengineer\\b cannot match "Engineers"; the pattern must allow the plural."""
        self.assertIsNotNone(self._family("Python + TypeScript Engineers"))

    def test_earliest_position_wins_for_multi_role_titles(self):
        """Titles lead with the primary role, so position beats declaration order."""
        self.assertEqual(
            self._family("Senior Backend Developer / DevOps Engineer"),
            "backend_engineer",
        )
        self.assertEqual(
            self._family("DevOps Engineer / Platform Engineer"), "devops_engineer"
        )
        self.assertEqual(
            self._family("Staff Software Engineer, Staff ML Engineer"),
            "software_engineer",
        )

    def test_swedish_titles_classify(self):
        cases = [
            ("DevOps Ingenjör", "devops_engineer"),
            ("DevOps Engineer/Systemutvecklare", "devops_engineer"),
            ("Konsultuppdrag - AI Engineer", "ai_engineer"),
            ("DevOps Engineer – Infrastruktur & Automatisering", "devops_engineer"),
            ("Platform Engineer sökes till spännande uppdrag inom försvarsindustrin",
             "platform_engineer"),
        ]
        for title, expected in cases:
            self.assertEqual(self._family(title), expected, title)

    def test_non_title_garbage_is_unclassified(self):
        """These are in the real corpus, written into the title column by earlier
        ingestion. They must be excluded from every statistic, not bucketed somewhere."""
        for title in ["Full-time", "Remote (US, Canada)", "Toronto, ON", "YC 19", ""]:
            self.assertIsNone(self._family(title), title)

    def test_off_topic_posting_is_unclassified(self):
        """A live 'Platform Engineer' query on Indeed returned this."""
        self.assertIsNone(self._family("Maintenance Technician"))
        self.assertIsNone(self._family("Registered Nurse"))
        self.assertIsNone(self._family("Warehouse Associate"))

    def test_none_and_whitespace_are_safe(self):
        self.assertEqual(self.roles.classify(None), (None, SENIORITY_UNSPECIFIED))
        self.assertEqual(self.roles.classify("   ")[0], None)

    def test_classify_all_exposes_secondary_families(self):
        families = self.roles.classify_all(
            "Senior Backend Developer / DevOps Engineer"
        )
        self.assertEqual(families[0], "backend_engineer")
        self.assertIn("devops_engineer", families)


class RealCorpusTests(unittest.TestCase):
    """Coverage against every title currently in the database."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(DB_PATH):
            raise unittest.SkipTest("jobs.db not present")
        conn = sqlite3.connect(DB_PATH)
        try:
            cls.titles = [row[0] for row in conn.execute("SELECT title FROM jobs")]
        finally:
            conn.close()
        cls.roles = load_roles()

    def test_high_classification_rate(self):
        classified = [t for t in self.titles if self.roles.classify(t)[0] is not None]
        rate = len(classified) / len(self.titles)
        self.assertGreater(rate, 0.90, f"only {rate:.0%} of real titles classified")

    def test_only_non_titles_remain_unclassified(self):
        unclassified = {t for t in self.titles
                        if self.roles.classify(t)[0] is None}
        # Every remaining failure should be something that is not a job title at all.
        self.assertTrue(
            unclassified <= {"Full-time", "Remote (US, Canada)", "Toronto, ON", "YC 19"},
            f"unexpected unclassified titles: {unclassified}",
        )

    def test_every_classification_is_a_known_family(self):
        for title in self.titles:
            family, _ = self.roles.classify(title)
            if family is not None:
                self.assertIn(family, self.roles.families, title)


class CellPlanningTests(unittest.TestCase):
    def setUp(self):
        self.roles = load_roles()

    def test_cell_count_supports_a_short_matrix_cycle(self):
        """Cell count is the binding constraint on the analytics window.

        At roughly 52 cells/day sustainable, ~250 cells is a ~5-day full cycle, which the
        30-day minimum analysis window can accommodate several times over. Materially more
        than this and supply comparisons stop being honest.
        """
        specs = self.roles.cell_specs()
        self.assertLess(len(specs), 300, f"{len(specs)} cells is too many to cycle")
        self.assertGreater(len(specs), 150)

    def test_cells_are_unique_on_the_schema_key(self):
        specs = self.roles.cell_specs()
        keys = {(s["source"], s["role_family"], s["location_id"], s["query"])
                for s in specs}
        self.assertEqual(len(keys), len(specs), "duplicate cell keys would break UNIQUE")

    def test_tier_locations_are_respected(self):
        specs = self.roles.cell_specs()
        breadth_locations = set(self.roles.tier_locations["breadth"])
        for spec in specs:
            if spec["tier"] == "breadth":
                self.assertIn(spec["location_id"], breadth_locations, spec)

    def test_core_families_reach_the_nordics(self):
        specs = self.roles.cell_specs()
        nordic = {s["role_family"] for s in specs
                  if s["location_id"] in {"fi", "se", "no", "dk"}}
        self.assertIn("ai_engineer", nordic)
        self.assertIn("platform_engineer", nordic)

    def test_both_sources_are_planned(self):
        sources = {s["source"] for s in self.roles.cell_specs()}
        self.assertEqual(sources, {"indeed", "linkedin"})

    def test_alternate_queries_are_preserved_for_rotation(self):
        alternates = self.roles.alternate_queries("ai_engineer")
        self.assertTrue(alternates)
        self.assertNotIn("AI Engineer", alternates)

    def test_more_queries_per_family_expands_the_matrix(self):
        one = self.roles.cell_specs(queries_per_family=1)
        two = self.roles.cell_specs(queries_per_family=2)
        self.assertGreater(len(two), len(one))


if __name__ == "__main__":
    unittest.main()
