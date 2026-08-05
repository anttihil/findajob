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
        for location_id in ["us_remote", "los_angeles", "helsinki", "stockholm",
                            "oslo", "copenhagen"]:
            self.assertIn(location_id, self.roles.locations)
        self.assertEqual(self.roles.locations["helsinki"].indeed_country, "finland")
        self.assertEqual(self.roles.locations["stockholm"].indeed_country, "sweden")

    def test_location_ids_are_not_ambiguous_abbreviations(self):
        """"la" reads as Louisiana, and two-letter ids collide with US state codes."""
        for location_id in self.roles.locations:
            self.assertNotIn(location_id, {"la", "fi", "se", "dk", "ca", "wa", "ny"})

    def test_remote_location_is_flagged_remote(self):
        self.assertTrue(self.roles.locations["us_remote"].is_remote)
        self.assertFalse(self.roles.locations["los_angeles"].is_remote)

    def test_every_location_declares_an_access_level(self):
        """access governs whether a role is takeable without moving house."""
        from backend.roles import ACCESS_LEVELS

        for location in self.roles.locations.values():
            self.assertIn(location.access, ACCESS_LEVELS, location.id)
        self.assertEqual(self.roles.locations["los_angeles"].access, "commutable")
        self.assertEqual(self.roles.locations["us_remote"].access, "remote")
        self.assertEqual(self.roles.locations["helsinki"].access, "relocation")


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
                  if s["location_id"] in {"helsinki", "stockholm", "oslo", "copenhagen"}}
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


class CommutableAreaTests(unittest.TestCase):
    """candidates have local or remote preferences, so this is the distinction that decides whether a
    posting is worth reading: commutable, remote, or requires relocating."""

    def setUp(self):
        self.roles = load_roles()

    def test_la_basin_cities_are_commutable(self):
        for city in ["Los Angeles", "Santa Monica", "Pasadena", "El Segundo",
                     "Long Beach", "Burbank", "Culver City", "Irvine", "Commerce"]:
            self.assertTrue(
                self.roles.is_commutable(city=city, region="CA"), city
            )

    def test_same_named_cities_in_other_states_are_not(self):
        """Glendale AZ, Pasadena TX, and Ontario CA-vs-Canada all exist."""
        self.assertFalse(self.roles.is_commutable(city="Glendale", region="AZ"))
        self.assertFalse(self.roles.is_commutable(city="Pasadena", region="TX"))

    def test_distant_california_cities_are_not_commutable(self):
        for city in ["San Francisco", "San Jose", "Sacramento", "San Diego"]:
            self.assertFalse(
                self.roles.is_commutable(city=city, region="CA"), city
            )

    def test_area_phrasings_are_recognised(self):
        for text in ["Greater Los Angeles Area", "Orange County, CA",
                     "South Bay", "San Fernando Valley"]:
            self.assertTrue(self.roles.is_commutable(location_text=text), text)

    def test_commutable_beats_remote(self):
        """A remote-friendly role down the road is strictly better than one merely remote."""
        self.assertEqual(
            self.roles.classify_access(city="Santa Monica", region="CA", is_remote=True),
            "commutable",
        )

    def test_remote_elsewhere_is_remote(self):
        self.assertEqual(
            self.roles.classify_access(city="Austin", region="TX", is_remote=True),
            "remote",
        )

    def test_onsite_elsewhere_requires_relocation(self):
        for city, region in [("Austin", "TX"), ("Stockholm", ""), ("Helsinki", "")]:
            self.assertEqual(
                self.roles.classify_access(city=city, region=region, is_remote=False),
                "relocation",
                city,
            )

    def test_unknown_location_with_no_remote_flag_is_relocation(self):
        """Conservative: never imply a role is reachable without evidence."""
        self.assertEqual(self.roles.classify_access(), "relocation")


class SearchLabelTests(unittest.TestCase):
    """`label` is for humans, `search_label` is sent to the board.

    Conflating them cost a silent outage: us_nat was searched as "United States (onsite,
    nationwide)", which Indeed matches literally and answers with 0 rows, so 20 core cells
    recorded status='empty' -- indistinguishable downstream from a genuine "none observed".
    """

    def setUp(self):
        self.roles = load_roles()

    def test_search_label_defaults_to_label(self):
        location = self.roles.locations["los_angeles"]
        self.assertEqual(location.search_label, location.label)

    def test_qualified_labels_are_overridden(self):
        for location_id in ("us_nat", "us_remote"):
            location = self.roles.locations[location_id]
            self.assertEqual(location.search_label, "United States")
            self.assertIn("(", location.label)

    def test_no_shipped_location_sends_a_parenthetical_to_a_board(self):
        for location in self.roles.locations.values():
            self.assertNotIn(
                "(", location.search_label,
                f"{location.id} would be searched as {location.search_label!r}",
            )

    def test_validate_rejects_a_parenthetical_search_label(self):
        self.roles.locations["us_nat"].search_label = "United States (nationwide)"
        problems = self.roles.validate()
        self.assertTrue(any("parenthetical" in p for p in problems), problems)

    def test_planned_cells_carry_the_search_label(self):
        """The bridge that actually broke: cell_specs -> task -> kwargs['location']."""
        from backend.scheduler import CellState, _make_task
        from datetime import datetime, timezone

        cell = CellState(1, "indeed", "ai_engineer", "us_nat", "AI Engineer", tier="core")
        task = _make_task(cell, {}, self.roles, "indeed", datetime.now(timezone.utc))
        self.assertEqual(task.location_label, "United States")
