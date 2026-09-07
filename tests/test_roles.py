"""Search-target tests: cell planning, search labels, and taxonomy integrity."""

import unittest
from typing import Any

from careerradar.taxonomy.roles import ACCESS_LEVELS, RoleTaxonomy

TEST_TAXONOMY_SPEC: dict[str, Any] = {
    "families": {
        "forward_deployed_engineer": {
            "label": "Forward Deployed / Solutions Engineer",
            "query_terms": ["Forward Deployed Engineer", "Solutions Engineer"],
            "enabled": True,
        },
        "devops_engineer": {
            "label": "DevOps Engineer",
            "query_terms": ["DevOps Engineer"],
            "enabled": True,
        },
        "ai_engineer": {
            "label": "AI / GenAI Engineer",
            "query_terms": ["AI Engineer", "GenAI Engineer"],
            "enabled": True,
        },
        "mobile_engineer": {
            "label": "Mobile Engineer",
            "query_terms": [],
            "enabled": True,
        },
        "product_manager": {
            "label": "Product Manager",
            "query_terms": ["Product Manager"],
            "enabled": False,
        },
        "software_engineer": {
            "label": "Software Engineer (generalist)",
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

    def test_location_ids_are_not_ambiguous_abbreviations(self) -> None:
        """ "la" reads as Louisiana, and two-letter ids collide with US state codes."""
        for location_id in self.roles.locations:
            self.assertNotIn(location_id, {"la", "fi", "se", "dk", "ca", "wa", "ny"})

    def test_remote_location_is_flagged_remote(self) -> None:
        self.assertTrue(self.roles.locations["us_remote"].is_remote)
        self.assertFalse(self.roles.locations["los_angeles"].is_remote)

    def test_every_location_declares_an_access_level(self) -> None:
        for location in self.roles.locations.values():
            self.assertIn(location.access, ACCESS_LEVELS, location.id)


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
        self.assertTrue(silent, "spec must declare a family with no query terms")
        planned = {s["role_family"] for s in self.roles.cell_specs()}
        for key in silent:
            self.assertNotIn(key, planned, key)

    def test_a_disabled_family_is_not_searched(self) -> None:
        planned = {s["role_family"] for s in self.roles.cell_specs()}
        self.assertNotIn("product_manager", planned)

    def test_cell_cost_of_a_term_covers_enabled_locations(self) -> None:
        specs = self.roles.cell_specs(sources=("indeed", "linkedin"))
        enabled_locs = [loc for loc in self.roles.locations.values() if loc.enabled]
        for key, family in self.roles.families.items():
            if not family.query_terms or not family.enabled:
                continue
            expected = len(family.query_terms) * len(enabled_locs) * 2
            self.assertEqual(len([s for s in specs if s["role_family"] == key]), expected, key)


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
