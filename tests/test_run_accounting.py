"""Cell accounting for one sync run.

Empty searches finish successfully and count toward progress. Their separate yield
counter still lets a run warn when a material share of searches returns no postings.
"""

import os
import sys
import unittest
from datetime import datetime, timezone
from typing import Any
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from findajob.search import runner
from findajob.search.scheduler import CellState, ScrapeTask

CONFIG: dict[str, Any] = {
    "scraper": {"sources": {"indeed": True}, "proxies": {"enabled": False}},
    "matching": {},
}


def _task(cell_id: int) -> ScrapeTask:
    return ScrapeTask(
        cell_id=cell_id,
        source="indeed",
        query="Platform Engineer",
        location_id="helsinki",
        location_label="Helsinki, Finland",
        country="finland",
        indeed_country="finland",
        is_remote=False,
        distance=50,
        results_wanted=75,
        hours_old=24,
        fetch_description=True,
        desc_selection="census",
    )


def _run(outcomes: list[str]) -> tuple[dict[str, Any], list[str], list[str]]:
    """Drive one dry run whose cells return `outcomes`, and collect what it logged.

    The cell is given a fresh last_success_at so the staleness check stays quiet: it writes
    to the real sync_status.json, which a test must not touch.
    """
    cell = CellState(
        id=1,
        source="indeed",
        location_id="helsinki",
        query="Platform Engineer",
        active=True,
        last_success_at=datetime.now(timezone.utc).isoformat(),
    )
    db = mock.MagicMock()
    db.get_cells.return_value = [cell]
    targets = mock.MagicMock()
    targets.validate.return_value = []
    targets.locations = {}

    with (
        mock.patch.object(runner, "Database", return_value=db),
        mock.patch.object(runner, "load_config", return_value=CONFIG),
        mock.patch.object(runner, "load_targets", return_value=targets),
        mock.patch.object(
            runner, "select_cells", return_value=[_task(i) for i in range(len(outcomes))]
        ),
        mock.patch.object(runner, "_scrape_one", side_effect=outcomes) as scrape,
        mock.patch.object(runner.logger, "info") as info,
        mock.patch.object(runner.logger, "warning") as warning,
    ):
        totals = runner.run_sync(dry_run=True, sources=["indeed"])
    assert scrape.call_count == len(outcomes)
    assert totals is not None, "a completed run always returns its totals"
    return (
        totals,
        [str(call.args[0]) for call in info.call_args_list],
        [str(call.args[0]) for call in warning.call_args_list],
    )


class CellAccountingTests(unittest.TestCase):
    def test_empty_searches_are_successes_and_errors_remain_separate(self) -> None:
        totals, _, _ = _run(["ok", "empty", "error", "ok"])
        self.assertEqual(totals["cells_planned"], 4)
        self.assertEqual(totals["cells_succeeded"], 3)
        self.assertEqual(totals["cells_empty"], 1)
        self.assertEqual(totals["cells_error"], 1)
        self.assertEqual(
            totals["cells_planned"],
            totals["cells_succeeded"] + totals["cells_error"] + totals["cells_skipped"],
        )

    def test_empty_searches_complete_the_production_sized_run(self) -> None:
        totals, info, _ = _run(["ok"] * 34 + ["empty"] * 10)
        self.assertEqual(totals["cells_succeeded"], totals["cells_planned"])
        self.assertEqual(totals["cells_succeeded"], 44)
        self.assertEqual(totals["cells_empty"], 10)
        self.assertEqual(totals["cells_error"], 0)
        self.assertEqual(totals["cells_skipped"], 0)
        self.assertIn("Sync finished (partial)", info)

    def test_all_empty_searches_finish_without_being_called_failed(self) -> None:
        totals, info, _ = _run(["empty"] * 4)
        self.assertEqual(totals["cells_succeeded"], 4)
        self.assertEqual(totals["cells_empty"], 4)
        self.assertIn("Sync finished (partial)", info)

    def test_all_errors_still_fail_the_run(self) -> None:
        totals, info, _ = _run(["error"] * 4)
        self.assertEqual(totals["cells_succeeded"], 0)
        self.assertEqual(totals["cells_error"], 4)
        self.assertIn("Sync finished (failed)", info)

    def test_a_run_that_loses_a_material_share_is_not_ok(self) -> None:
        _, info, warnings = _run(["ok"] * 6 + ["empty"] * 4)
        self.assertIn("Sync finished (partial)", info)
        self.assertTrue(
            any("[indeed]" in line and "4 empty" in line for line in warnings), warnings
        )

    def test_a_few_empty_cells_stay_ok(self) -> None:
        _, info, warnings = _run(["ok"] * 19 + ["empty"])
        self.assertIn("Sync finished (ok)", info)
        self.assertTrue(any("1 empty" in line for line in warnings), warnings)
