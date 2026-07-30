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

    def _family(self, title, tech=True):
        """Default tech=True: most callers are testing title patterns, and the normalizer
        passes real skill evidence in production."""
        return self.roles.classify(title, has_tech_skills=tech)[0]

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

    def test_weak_patterns_require_technical_corroboration(self):
        """Bare "X Engineer" is only a software role if the posting names a technology.

        A live dry run classified an "R&D Engineer, Materials" as software_engineer,
        inflating that family's supply and handing it a perfect title-family score.
        """
        for title in ["R&D Engineer, Materials", "Python + TypeScript Engineers"]:
            self.assertIsNone(
                self._family(title, tech=False),
                f"{title!r} should not classify without technical evidence",
            )
            self.assertEqual(
                self._family(title, tech=True), "software_engineer", title
            )

    def test_excluded_titles_are_vetoed_even_with_technical_skills(self):
        """Hardware, RF, manufacturing, and facilities postings do mention Python and
        Linux, so the technical-skill gate alone cannot exclude them. Left unchecked they
        are absorbed by the generic family and inflate its supply figure. Every title here
        was observed in a real scrape."""
        for title in [
            "Stationary Engineer", "Electrical Engineer", "Manufacturing Engineer",
            "Hardware Engineer, PCB", "ASIC Verification Engineer", "Network Engineer",
            "RF/Microwave Engineer - Senior Member of Technical Staff",
            "STAFF ENGINEER, DRY ETCH DRAM", "Senior SAR-Based MTI Engineer",
            "Distinguished Engineer (Chiplet Lead - Photonic Fabric)",
            "Principal Failure Analysis Engineer", "Senior Quantum Error Correction Engineer",
        ]:
            self.assertIsNone(self._family(title, tech=True), title)

    def test_exclusions_do_not_veto_real_software_titles(self):
        """The vetoes must be narrow. "Flight Software Engineer" is software despite the
        aerospace context; "Manufacturing Test" software is still software."""
        for title in [
            "Flight Software Engineer II", "Senior Software Engineer",
            "Robotics Software Engineer - UAS", "Senior Platform Engineer",
            "Embedded Autonomy Engineer", "Senior Software Engineer, Bazel Tools",
        ]:
            self.assertIsNotNone(self._family(title, tech=True), title)

    def test_a_confident_match_survives_an_exclusion_elsewhere_in_the_title(self):
        """Multi-role titles mix software with non-software roles. Vetoing unconditionally
        discarded real postings, so an exclusion only overrides a WEAK match."""
        self.assertEqual(
            self._family(
                "Sr. Java Backend / Sr. React Frontend / Sr. Network Engineer / "
                "Sr. Cloud Infrastructure Engineer"
            ),
            "backend_engineer",
        )
        self.assertEqual(
            self._family(
                "Principal Software Engineer, Senior Java Engineer - Cloud, "
                "Account Executive"
            ),
            "software_engineer",
        )

    def test_confident_specific_match_beats_earlier_weak_generic_match(self):
        """Position alone is not enough: the bare "Engineer" in "Observability Engineer"
        sits at a lower offset than "site reliability", so an earliest-position-wins rule
        classified this real posting as generic software."""
        self.assertEqual(
            self._family("Observability Engineer / Site Reliability Engineer"), "sre"
        )

    def test_pattern_variants_seen_in_real_postings(self):
        """Each of these lost a real posting to the generic bucket before being handled."""
        cases = [
            ("Senior ML Ops Engineer (Machine Learning Infrastructure)", "mlops_engineer"),
            ("AI Developer", "ai_engineer"),
            ("AI Architect/Developer", "ai_engineer"),
            ("Senior AI Agentic Engineer", "ai_engineer"),
            ("Lead IT DevSecOps Engineer", "devops_engineer"),
            ("Senior Frontend Software Engineer - Invoicing & Payments",
             "frontend_engineer"),
            ("Senior Mobile Application Developer", "mobile_engineer"),
        ]
        for title, expected in cases:
            self.assertEqual(self._family(title), expected, title)

    def test_confident_patterns_do_not_need_corroboration(self):
        """An explicit software title stands on its own."""
        for title, expected in [
            ("Senior Platform Engineer", "platform_engineer"),
            ("DevOps Engineer", "devops_engineer"),
            ("Full Stack Developer", "fullstack_engineer"),
            ("Software Engineer", "software_engineer"),
        ]:
            self.assertEqual(self._family(title, tech=False), expected, title)

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
            # Legacy rows came from curated API feeds, so nearly every title is a real
            # software role. Scraped rows are raw board output, where a large minority
            # genuinely is not -- the two need different expectations.
            cls.curated = [r[0] for r in conn.execute(
                "SELECT title FROM jobs WHERE sync_run_id IS NULL")]
            cls.scraped = [r[0] for r in conn.execute(
                "SELECT title FROM jobs WHERE sync_run_id IS NOT NULL")]
        finally:
            conn.close()
        cls.roles = load_roles()
        cls.titles = cls.curated + cls.scraped

    def test_curated_feed_titles_classify_almost_completely(self):
        if not self.curated:
            self.skipTest("no curated rows")
        classified = [t for t in self.curated
                      if self.roles.classify(t, has_tech_skills=True)[0] is not None]
        rate = len(classified) / len(self.curated)
        self.assertGreater(rate, 0.90, f"only {rate:.0%} of curated titles classified")

    def test_only_non_titles_remain_unclassified_in_curated_feed(self):
        if not self.curated:
            self.skipTest("no curated rows")
        unclassified = {t for t in self.curated
                        if self.roles.classify(t, has_tech_skills=True)[0] is None}
        self.assertTrue(
            unclassified <= {"Full-time", "Remote (US, Canada)", "Toronto, ON", "YC 19"},
            f"unexpected unclassified curated titles: {unclassified}",
        )

    def test_scraped_titles_are_mostly_but_not_entirely_classified(self):
        """Boards return substantial off-target results, so a 100% rate would mean the
        vetoes are not working -- and a very low rate would mean they are too broad."""
        if not self.scraped:
            self.skipTest("no scraped rows")
        classified = [t for t in self.scraped
                      if self.roles.classify(t, has_tech_skills=True)[0] is not None]
        rate = len(classified) / len(self.scraped)
        self.assertGreater(rate, 0.55, f"only {rate:.0%} classified — vetoes too broad?")
        self.assertLess(rate, 0.98, f"{rate:.0%} classified — vetoes not firing?")

    def test_every_classification_is_a_known_family(self):
        for title in self.titles:
            family, _ = self.roles.classify(title, has_tech_skills=True)
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
