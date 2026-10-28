"""Source-specific search parameters are preserved by the page adapter."""

import unittest

from findajob.search.scheduler import ScrapeTask
from findajob.search.sources.jobspy_source import build_scrape_kwargs


class ScrapeKwargsTests(unittest.TestCase):
    def test_linkedin_search_includes_configured_radius(self) -> None:
        task = ScrapeTask(
            cell_id=1,
            source="linkedin",
            query="AI Engineer",
            location_id="los_angeles",
            location_label="Los Angeles, CA",
            country="US",
            indeed_country="usa",
            is_remote=False,
            distance=25,
            results_wanted=50,
            hours_old=336,
            fetch_description=False,
            desc_selection="none",
        ).to_dict()

        kwargs = build_scrape_kwargs(task)

        self.assertEqual(kwargs.get("distance"), 25)
        self.assertFalse(kwargs.get("linkedin_fetch_description"))

    def test_us_remote_linkedin_search_is_scoped_to_united_states(self) -> None:
        """JobSpy sends is_remote as f_WT=2; location must provide the US scope."""
        task = ScrapeTask(
            cell_id=1,
            source="linkedin",
            query="AI Engineer",
            location_id="us_remote",
            location_label="Remote",  # legacy cell value retained in existing databases
            country="US",
            indeed_country="usa",
            is_remote=True,
            distance=50,
            results_wanted=50,
            hours_old=336,
            fetch_description=False,
            desc_selection="none",
        ).to_dict()

        kwargs = build_scrape_kwargs(task)

        self.assertEqual(kwargs.get("location"), "United States")
        self.assertTrue(kwargs.get("is_remote"))

    def test_global_remote_linkedin_search_remains_unscoped(self) -> None:
        task = ScrapeTask(
            cell_id=1,
            source="linkedin",
            query="AI Engineer",
            location_id="global_remote",
            location_label="Remote",
            country="US",
            indeed_country="usa",
            is_remote=True,
            distance=50,
            results_wanted=50,
            hours_old=336,
            fetch_description=False,
            desc_selection="none",
        ).to_dict()

        self.assertEqual(build_scrape_kwargs(task).get("location"), "Remote")


if __name__ == "__main__":
    unittest.main()
