"""Role normalization and taxonomy engine tests."""

import unittest
from typing import Any

from careerradar.taxonomy.roles import (
    ACCESS_LEVELS,
    SENIORITY_UNSPECIFIED,
    RoleTaxonomy,
)

TEST_TAXONOMY_SPEC: dict[str, Any] = {
    "families": {
        "forward_deployed_engineer": {
            "label": "Forward Deployed / Solutions Engineer",
            "aliases": ["forward[- ]deployed", "solutions? engineer", "solutions? architect"],
            "query_terms": ["Forward Deployed Engineer", "Solutions Engineer"],
            "enabled": True,
        },
        "devops_engineer": {
            "label": "DevOps Engineer",
            "aliases": [r"\bdev\w*ops\b", "ci/cd engineer", "devsecops engineer"],
            "query_terms": ["DevOps Engineer"],
            "enabled": True,
        },
        "sre": {
            "label": "Site Reliability Engineer",
            "aliases": ["site reliability", r"\bsre\b", "observability engineer"],
            "query_terms": ["Site Reliability Engineer"],
            "enabled": True,
        },
        "mlops_engineer": {
            "label": "MLOps Engineer",
            "aliases": [
                r"\bml ?ops\b",
                "ml (?:platform|infrastructure|infra)",
                "machine learning infrastructure",
            ],
            "query_terms": ["MLOps Engineer"],
            "enabled": True,
        },
        "ai_engineer": {
            "label": "AI / GenAI Engineer",
            "aliases": [
                r"\bai engineer",
                r"\bgenai\b",
                "generative ai",
                r"\bllm",
                "ai developer",
                "ai architect",
                r"\bagentic\b",
            ],
            "query_terms": ["AI Engineer", "GenAI Engineer"],
            "enabled": True,
        },
        "platform_engineer": {
            "label": "Platform Engineer",
            "aliases": ["platform engineer", "infrastructure engineer"],
            "query_terms": ["Platform Engineer"],
            "enabled": True,
        },
        "backend_engineer": {
            "label": "Backend Engineer",
            "aliases": [r"back[- ]?end (?:engineer|developer)", r"back[- ]?end\b"],
            "query_terms": ["Backend Engineer"],
            "enabled": True,
        },
        "frontend_engineer": {
            "label": "Frontend Engineer",
            "aliases": [
                r"front[- ]?end\b",
                "ui engineer",
                "web developer",
                r"frontend (?:software )?engineer",
            ],
            "query_terms": ["Frontend Engineer"],
            "enabled": True,
        },
        "fullstack_engineer": {
            "label": "Full-Stack Engineer",
            "aliases": ["full[- ]?stack", "fullstack"],
            "query_terms": ["Full Stack Engineer"],
            "enabled": True,
        },
        "mobile_engineer": {
            "label": "Mobile Engineer",
            "aliases": [
                r"mobile (?:software |application )?(?:engineer|developer)",
                r"\bios engineer",
                "android engineer",
            ],
            "query_terms": [],
            "enabled": True,
        },
        "security_engineer": {
            "label": "Security Engineer",
            "aliases": [
                "security engineer",
                "appsec",
                "trust & safety engineer",
                "trust and safety engineer",
            ],
            "query_terms": ["Security Engineer"],
            "enabled": True,
        },
        "product_manager": {
            "label": "Product Manager",
            "aliases": ["product manager", "product owner"],
            "query_terms": ["Product Manager"],
            "enabled": True,
        },
        "embedded_engineer": {
            "label": "Embedded Engineer",
            "aliases": [r"embedded (?:software )?engineer", "firmware engineer"],
            "query_terms": [],
            "enabled": True,
        },
        "integration_engineer": {
            "label": "Integration Engineer",
            "aliases": ["integration engineer"],
            "query_terms": [],
            "enabled": True,
        },
        "cloud_engineer": {
            "label": "Cloud Engineer",
            "aliases": ["cloud engineer", "cloud architect"],
            "query_terms": ["Cloud Engineer"],
            "enabled": True,
        },
        "software_engineer": {
            "label": "Software Engineer (generalist)",
            "aliases": ["software engineer", "software developer", "software engineering"],
            "weak_patterns": [r"\bengineers?\b", r"\bdevelopers?\b"],
            "query_terms": ["Software Engineer"],
            "enabled": True,
        },
    },
    "locations": [
        {
            "id": "us_remote",
            "label": "United States (remote)",
            "search_label": "United States",
            "country": "US",
            "indeed_country": "usa",
            "is_remote": True,
            "access": "remote",
            "weight": 1.0,
            "distance": 50,
            "enabled": True,
        },
        {
            "id": "los_angeles",
            "label": "Los Angeles, CA",
            "search_label": "Los Angeles, CA",
            "country": "US",
            "indeed_country": "usa",
            "is_remote": False,
            "access": "commutable",
            "weight": 1.0,
            "distance": 50,
            "enabled": True,
        },
        {
            "id": "us_nat",
            "label": "United States (onsite, nationwide)",
            "search_label": "United States",
            "country": "US",
            "indeed_country": "usa",
            "is_remote": False,
            "access": "relocation",
            "weight": 0.55,
            "distance": 50,
            "enabled": True,
        },
    ],
}


class RoleTaxonomyIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roles = RoleTaxonomy(spec_dict=TEST_TAXONOMY_SPEC)

    def test_validates_clean(self) -> None:
        problems = self.roles.validate()
        self.assertEqual(problems, [], f"role taxonomy problems: {problems}")

    def test_generic_catch_all_is_declared_last(self) -> None:
        """software_engineer must lose positional ties to every specific family."""
        self.assertEqual(list(self.roles.families)[-1], "software_engineer")

    def test_location_ids_are_not_ambiguous_abbreviations(self) -> None:
        """ "la" reads as Louisiana, and two-letter ids collide with US state codes."""
        for location_id in self.roles.locations:
            self.assertNotIn(location_id, {"la", "fi", "se", "dk", "ca", "wa", "ny"})

    def test_remote_location_is_flagged_remote(self) -> None:
        self.assertTrue(self.roles.locations["us_remote"].is_remote)
        self.assertFalse(self.roles.locations["los_angeles"].is_remote)

    def test_every_location_declares_an_access_level(self) -> None:
        """access governs whether a role is takeable without moving house."""
        for location in self.roles.locations.values():
            self.assertIn(location.access, ACCESS_LEVELS, location.id)
        self.assertEqual(self.roles.locations["los_angeles"].access, "commutable")
        self.assertEqual(self.roles.locations["us_remote"].access, "remote")
        self.assertEqual(self.roles.locations["us_nat"].access, "relocation")


class SeniorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roles = RoleTaxonomy(spec_dict=TEST_TAXONOMY_SPEC)

    def test_common_seniority_markers(self) -> None:
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

    def test_open_range_reads_as_the_lower_bound(self) -> None:
        """ "Junior to Senior" is honestly junior-eligible, so it should not read senior."""
        self.assertEqual(
            self.roles.seniority("Junior to Senior Fullstack Engineer multiple positions"),
            "junior",
        )

    def test_founding_engineer_reads_as_senior(self) -> None:
        self.assertEqual(self.roles.seniority("Founding Engineer (Full-Stack)"), "senior")

    def test_senior_associate_reads_as_senior_not_junior(self) -> None:
        cases = [
            "Senior Software Engineer (Python) - Senior Associate",
            "Sr Associate, Product Management – Applied AI Platforms",
            "Senior Associate- Software Engineer",
        ]
        for title in cases:
            self.assertEqual(self.roles.seniority(title), "senior", title)

    def test_plain_associate_still_reads_as_junior(self) -> None:
        self.assertEqual(self.roles.seniority("Associate Engineer"), "junior")

    def test_member_of_technical_staff_is_not_staff_level(self) -> None:
        cases = [
            "Member of Technical Staff",
            "Member Technical Staff",
            "Design Engineer - Member of Technical Staff",
            "Member of the Technical Staff, Inference",
        ]
        for title in cases:
            self.assertEqual(self.roles.seniority(title), "unspecified", title)

    def test_genuine_staff_titles_still_read_as_staff(self) -> None:
        for title in ("Staff Software Engineer", "Principal Engineer", "Solutions Architect"):
            self.assertEqual(self.roles.seniority(title), "staff", title)

    def test_senior_reorder_does_not_disturb_staff_or_lead(self) -> None:
        cases = [
            ("Senior Staff Engineer", "staff"),
            ("Senior Director of Engineering", "lead"),
        ]
        for title, expected in cases:
            self.assertEqual(self.roles.seniority(title), expected, title)


class ClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roles = RoleTaxonomy(
            spec_dict=TEST_TAXONOMY_SPEC,
            exclusions=[
                r"\brf\b|microwave|antenna|\bradar\b|satellite communications",
                r"photonic|chiplet|\basic\b|dram|dry etch|semiconductor",
                r"analog (?:validation|design|mixed)|failure analysis",
                r"electrical engineer|mechanical engineer|chemical engineer|civil engineer",
                r"hardware engineer|\bpcb\b|\bfpga\b(?!.*software)",
                r"manufacturing engineer|process (?:development )?engineer",
                r"stationary engineer|operating engineer|facilities|\bhvac\b",
                r"network engineer|\bnoc\b technician",
                r"quantum error correction|synthetic scene",
                r"maintenance (?:technician|engineer)|building engineer|\bcustodian\b",
                r"sales representative|account executive|recruiter|nurse|driver|warehouse",
            ],
        )

    def _family(self, title: str | None, tech: bool = True) -> str | None:
        return self.roles.classify(title, has_tech_skills=tech)[0]

    def test_specific_families_beat_the_generic_catch_all(self) -> None:
        cases = [
            ("Senior Platform Engineer", "platform_engineer"),
            ("DevOps Engineer", "devops_engineer"),
            ("Senior AI Engineer (Python)", "ai_engineer"),
            ("Full Stack Developer (.NET/React)", "fullstack_engineer"),
            ("Backend Software Engineer", "backend_engineer"),
            ("Site Reliability & DevOps Engineer", "sre"),
            ("Forward Deployed Engineer - backend - python", "forward_deployed_engineer"),
            ("Trust & Safety Engineer", "security_engineer"),
            ("Edge AI Engineer", "ai_engineer"),
            ("Integration engineer", "integration_engineer"),
            ("Mobile Engineer", "mobile_engineer"),
            ("Senior Embedded Software Engineer", "embedded_engineer"),
            ("Cloud Engineer", "cloud_engineer"),
        ]
        for title, expected in cases:
            self.assertEqual(self._family(title), expected, title)

    def test_generic_titles_fall_through_to_software_engineer(self) -> None:
        for title in [
            "Software Engineer",
            "Senior Software Engineer",
            "Software Engineers",
            "Python + TypeScript Engineers",
            "Various Software Engineering Roles",
        ]:
            self.assertEqual(self._family(title), "software_engineer", title)

    def test_plural_engineers_still_classifies(self) -> None:
        self.assertIsNotNone(self._family("Python + TypeScript Engineers"))

    def test_weak_patterns_require_technical_corroboration(self) -> None:
        for title in ["R&D Engineer, Materials", "Python + TypeScript Engineers"]:
            self.assertIsNone(
                self._family(title, tech=False),
                f"{title!r} should not classify without technical evidence",
            )
            self.assertEqual(self._family(title, tech=True), "software_engineer", title)

    def test_excluded_titles_are_vetoed_even_with_technical_skills(self) -> None:
        for title in [
            "Stationary Engineer",
            "Electrical Engineer",
            "Manufacturing Engineer",
            "Hardware Engineer, PCB",
            "ASIC Verification Engineer",
            "Network Engineer",
            "STAFF ENGINEER, DRY ETCH DRAM",
            "Principal Failure Analysis Engineer",
            "Senior Quantum Error Correction Engineer",
        ]:
            self.assertIsNone(self._family(title, tech=True), title)

    def test_exclusions_do_not_veto_real_software_titles(self) -> None:
        for title in [
            "Flight Software Engineer II",
            "Senior Software Engineer",
            "Senior Platform Engineer",
        ]:
            self.assertIsNotNone(self._family(title, tech=True), title)

    def test_a_confident_match_survives_an_exclusion_elsewhere_in_the_title(self) -> None:
        self.assertEqual(
            self._family("Sr. Java Backend / Sr. React Frontend / Sr. Network Engineer"),
            "backend_engineer",
        )

    def test_confident_specific_match_beats_earlier_weak_generic_match(self) -> None:
        self.assertEqual(self._family("Observability Engineer / Site Reliability Engineer"), "sre")

    def test_pattern_variants_seen_in_real_postings(self) -> None:
        cases = [
            ("Senior ML Ops Engineer (Machine Learning Infrastructure)", "mlops_engineer"),
            ("AI Developer", "ai_engineer"),
            ("AI Architect/Developer", "ai_engineer"),
            ("Senior AI Agentic Engineer", "ai_engineer"),
            ("Lead IT DevSecOps Engineer", "devops_engineer"),
            ("Senior Frontend Software Engineer", "frontend_engineer"),
            ("Senior Mobile Application Developer", "mobile_engineer"),
        ]
        for title, expected in cases:
            self.assertEqual(self._family(title), expected, title)

    def test_confident_patterns_do_not_need_corroboration(self) -> None:
        for title, expected in [
            ("Senior Platform Engineer", "platform_engineer"),
            ("Fullstack Engineer", "fullstack_engineer"),
        ]:
            self.assertEqual(self._family(title, tech=False), expected, title)

    def test_earliest_position_wins_for_multi_role_titles(self) -> None:
        self.assertEqual(
            self._family("Senior Backend Developer / DevOps Engineer"),
            "backend_engineer",
        )
        self.assertEqual(
            self._family("DevOps Engineer / Senior Backend Developer"),
            "devops_engineer",
        )

    def test_classify_all_exposes_secondary_families(self) -> None:
        families = self.roles.classify_all("Senior Backend Developer / DevOps Engineer")
        self.assertEqual(families[0], "backend_engineer")
        self.assertIn("devops_engineer", families)


class CellPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roles = RoleTaxonomy(spec_dict=TEST_TAXONOMY_SPEC)

    def test_cells_are_unique_on_the_schema_key(self) -> None:
        specs = self.roles.cell_specs()
        keys = {(s["source"], s["role_family"], s["location_id"], s["query"]) for s in specs}
        self.assertEqual(len(keys), len(specs), "duplicate cell keys would break UNIQUE")

    def test_cell_planning_covers_all_locations(self) -> None:
        specs = self.roles.cell_specs()
        locations = {s["location_id"] for s in specs}
        for loc_id in self.roles.locations:
            self.assertIn(loc_id, locations)

    def test_both_sources_are_planned(self) -> None:
        sources = {s["source"] for s in self.roles.cell_specs()}
        self.assertEqual(sources, {"indeed", "linkedin"})

    def test_every_declared_query_term_is_seeded(self) -> None:
        specs = self.roles.cell_specs(sources=("indeed",))
        for key, family in self.roles.families.items():
            if not family.enabled:
                continue
            seeded = {s["query"] for s in specs if s["role_family"] == key}
            self.assertEqual(seeded, set(family.query_terms), key)

    def test_a_family_with_no_query_terms_is_not_searched(self) -> None:
        silent = [k for k, f in self.roles.families.items() if not f.query_terms]
        self.assertTrue(silent, "expected at least one classify-only family in roles")
        planned = {s["role_family"] for s in self.roles.cell_specs()}
        for key in silent:
            self.assertNotIn(key, planned, key)

    def test_cell_cost_of_a_term_covers_enabled_locations(self) -> None:
        specs = self.roles.cell_specs(sources=("indeed", "linkedin"))
        enabled_locs = [loc for loc in self.roles.locations.values() if loc.enabled]
        for key, family in self.roles.families.items():
            if not family.query_terms or not family.enabled:
                continue
            expected = len(family.query_terms) * len(enabled_locs) * 2
            self.assertEqual(len([s for s in specs if s["role_family"] == key]), expected, key)


class CommutableAreaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roles = RoleTaxonomy(
            spec_dict=TEST_TAXONOMY_SPEC,
            commutable_area={
                "region": "CA",
                "cities": [
                    "Los Angeles",
                    "Santa Monica",
                    "Pasadena",
                    "El Segundo",
                    "Long Beach",
                    "Burbank",
                    "Culver City",
                    "Irvine",
                    "Commerce",
                ],
                "patterns": [
                    r"greater los angeles",
                    r"orange county",
                    r"south bay",
                    r"san fernando valley",
                ],
            },
        )

    def test_la_basin_cities_are_commutable(self) -> None:
        for city in ["Los Angeles", "Santa Monica", "Pasadena", "Long Beach"]:
            self.assertTrue(self.roles.is_commutable(city=city, region="CA"), city)

    def test_same_named_cities_in_other_states_are_not(self) -> None:
        self.assertFalse(self.roles.is_commutable(city="Glendale", region="AZ"))
        self.assertFalse(self.roles.is_commutable(city="Pasadena", region="TX"))

    def test_distant_california_cities_are_not_commutable(self) -> None:
        for city in ["San Francisco", "San Jose", "Sacramento", "San Diego"]:
            self.assertFalse(self.roles.is_commutable(city=city, region="CA"), city)

    def test_area_phrasings_are_recognised(self) -> None:
        for text in [
            "Greater Los Angeles Area",
            "Orange County, CA",
            "South Bay",
        ]:
            self.assertTrue(self.roles.is_commutable(location_text=text), text)

    def test_commutable_beats_remote(self) -> None:
        self.assertEqual(
            self.roles.classify_access(city="Santa Monica", region="CA", is_remote=True),
            "commutable",
        )

    def test_remote_elsewhere_is_remote(self) -> None:
        self.assertEqual(
            self.roles.classify_access(city="Austin", region="TX", is_remote=True),
            "remote",
        )

    def test_onsite_elsewhere_requires_relocation(self) -> None:
        for city, region in [("Austin", "TX"), ("Seattle", "WA")]:
            self.assertEqual(
                self.roles.classify_access(city=city, region=region, is_remote=False),
                "relocation",
                city,
            )

    def test_unknown_location_with_no_remote_flag_is_relocation(self) -> None:
        self.assertEqual(self.roles.classify_access(), "relocation")


class SearchLabelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roles = RoleTaxonomy(spec_dict=TEST_TAXONOMY_SPEC)

    def test_search_label_defaults_to_label(self) -> None:
        location = self.roles.locations["los_angeles"]
        self.assertEqual(location.search_label, location.label)

    def test_qualified_labels_are_overridden(self) -> None:
        for location_id in ("us_nat", "us_remote"):
            location = self.roles.locations[location_id]
            self.assertEqual(location.search_label, "United States")
            self.assertIn("(", location.label)

    def test_no_shipped_location_sends_a_parenthetical_to_a_board(self) -> None:
        for location in self.roles.locations.values():
            self.assertNotIn(
                "(",
                location.search_label,
                f"{location.id} would be searched as {location.search_label!r}",
            )

    def test_validate_rejects_a_parenthetical_search_label(self) -> None:
        self.roles.locations["us_nat"].search_label = "United States (nationwide)"
        problems = self.roles.validate()
        self.assertTrue(any("parenthetical" in p for p in problems), problems)

    def test_planned_cells_carry_the_search_label(self) -> None:
        from datetime import datetime, timezone

        from careerradar.search.scheduler import CellState, _make_task

        cell = CellState(1, "indeed", "ai_engineer", "us_nat", "AI Engineer", active=True)
        task = _make_task(cell, {}, self.roles, "indeed", datetime.now(timezone.utc))
        self.assertEqual(task.location_label, "United States")


if __name__ == "__main__":
    unittest.main()
