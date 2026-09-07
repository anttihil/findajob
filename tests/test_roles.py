"""Search-target tests: cell planning, search labels, and target integrity."""

import unittest
from typing import Any

from careerradar.taxonomy.roles import RoleTaxonomy

TEST_TAXONOMY_SPEC: dict[str, Any] = {
    "queries": [
        "Forward Deployed Engineer",
        "Solutions Engineer",
        "DevOps Engineer",
        "AI Engineer",
        "GenAI Engineer",
        "Software Engineer",
    ],
    "locations": [
        {
            "id": "us_remote",
            "label": "United States (remote)",
            "search_label": "United States",
            "country": "US",
            "indeed_country": "usa",
            "is_remote": True,
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
            "weight": 0.55,
            "distance": 50,
            "enabled": True,
        },
        {
            "id": "sydney",
            "label": "Sydney, Australia",
            "search_label": "Sydney, Australia",
            "country": "AU",
            "indeed_country": "australia",
            "is_remote": False,
            "weight": 0.4,
            "distance": 50,
            "enabled": False,
        },
    ],
}


class RoleTaxonomyIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roles = RoleTaxonomy(spec_dict=TEST_TAXONOMY_SPEC)

    def test_validates_clean(self) -> None:
        problems = self.roles.validate()
        self.assertEqual(problems, [], f"search target problems: {problems}")

    def test_location_ids_are_not_ambiguous_abbreviations(self) -> None:
        """ "la" reads as Louisiana, and two-letter ids collide with US state codes."""
        for location_id in self.roles.locations:
            self.assertNotIn(location_id, {"la", "fi", "se", "dk", "ca", "wa", "ny"})

    def test_remote_location_is_flagged_remote(self) -> None:
        self.assertTrue(self.roles.locations["us_remote"].is_remote)
        self.assertFalse(self.roles.locations["los_angeles"].is_remote)


class CellPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roles = RoleTaxonomy(spec_dict=TEST_TAXONOMY_SPEC)
        self.enabled_locations = [loc for loc in self.roles.locations.values() if loc.enabled]

    def test_cells_are_unique_on_the_schema_key(self) -> None:
        specs = self.roles.cell_specs()
        keys = {(s["source"], s["location_id"], s["query"]) for s in specs}
        self.assertEqual(len(keys), len(specs), "duplicate cell keys would break UNIQUE")

    def test_cell_planning_covers_every_enabled_location(self) -> None:
        planned = {s["location_id"] for s in self.roles.cell_specs()}
        self.assertEqual(planned, {loc.id for loc in self.enabled_locations})

    def test_a_disabled_location_is_not_searched(self) -> None:
        self.assertNotIn("sydney", {s["location_id"] for s in self.roles.cell_specs()})

    def test_both_sources_are_planned(self) -> None:
        sources = {s["source"] for s in self.roles.cell_specs()}
        self.assertEqual(sources, {"indeed", "linkedin"})

    def test_every_declared_query_is_seeded(self) -> None:
        specs = self.roles.cell_specs(sources=("indeed",))
        self.assertEqual({s["query"] for s in specs}, set(self.roles.queries))

    def test_the_matrix_is_queries_by_locations_by_sources(self) -> None:
        specs = self.roles.cell_specs(sources=("indeed", "linkedin"))
        self.assertEqual(len(specs), len(self.roles.queries) * len(self.enabled_locations) * 2)

    def test_specs_carry_the_whole_jobspy_call(self) -> None:
        """The cell is built from one spec and joins nothing at task-build time."""
        for spec in self.roles.cell_specs(sources=("indeed",)):
            location = self.roles.locations[spec["location_id"]]
            self.assertEqual(spec["search_label"], location.search_label)
            self.assertEqual(spec["country"], location.country)
            self.assertEqual(spec["indeed_country"], location.indeed_country)
            self.assertEqual(bool(spec["is_remote"]), location.is_remote)
            self.assertEqual(spec["distance"], location.distance)
            self.assertEqual(spec["weight"], location.weight)


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

        cell = CellState(
            1,
            "indeed",
            "us_nat",
            "AI Engineer",
            search_label="United States",
            country="US",
            active=True,
        )
        task = _make_task(cell, {}, "indeed", datetime.now(timezone.utc))
        self.assertEqual(task.location_label, "United States")
        self.assertEqual(task.country, "US")


if __name__ == "__main__":
    unittest.main()
