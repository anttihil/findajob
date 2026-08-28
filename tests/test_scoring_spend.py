"""What one scoring run says it cost must be what it actually cost.

Two ways it was not. The graph kept only the last attempt's token counts, so a posting
that answered on the second try was billed as one call; and the worker skipped its cost
accounting entirely for a posting that produced no verdict, so the most expensive
postings in a run -- the ones that failed MAX_ATTEMPTS times -- were recorded as free.
Both errors point the same way: `job_verdicts.cost_usd` reads low, and it is the only
durable record of what scoring spends.

The first is pinned in test_scoring_module.py at the graph. This pins the second, and
pins that what the graph accumulated is what reaches the row.
"""

import io
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from typing import Any
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.core.llm import usage_cost
from careerradar.core.migrations import apply_pragmas, migrate
from careerradar.scoring import worker

MODEL = "deepseek-v4-flash"
CONFIG: dict[str, Any] = {"scoring": {"model": MODEL, "concurrency": 2, "max_usd_per_run": None}}

# One attempt's tokens, at roughly the shape a real posting has.
ATTEMPT = {"prompt": 6_000, "cache_hit": 4_800, "cache_miss": 1_200, "completion": 1_400}


def usage(attempts: int) -> dict[str, int]:
    return {key: value * attempts for key, value in ATTEMPT.items()}


VERDICT = {
    "fit": True,
    "reason_type": "match",
    "reason_description": "Solid overlap.",
}


class _Db:
    """Just enough of `Database` for `run_scoring`, over a real migrated file."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def update_cell_quality(self, cell_id: int, score: int) -> None:
        raise AssertionError("no posting in this fixture carries a scrape cell")

    def close(self) -> None:
        pass


class _Graph:
    """Returns a canned end state per posting, keyed by job id."""

    def __init__(self, states: dict[int, dict[str, Any]]) -> None:
        self.states = states

    def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
        return self.states[state["posting"]["id"]]


class RunSpendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = sqlite3.connect(self.tmp.name)
        self.conn.row_factory = sqlite3.Row
        apply_pragmas(self.conn)
        migrate(self.conn)
        for job_id in (1, 2):
            self.conn.execute(
                "INSERT INTO jobs (id, job_key, title, url, description, pipeline_state, "
                "sync_run_id) VALUES (?, ?, 'Platform Engineer', ?, ?, 'new', 1)",
                (job_id, f"k{job_id}", f"https://example.test/{job_id}", "x" * 400),
            )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        os.unlink(self.tmp.name)

    def run_scoring(self, states: dict[int, dict[str, Any]]) -> str:
        adapter = mock.MagicMock()
        out = io.StringIO()
        with (
            mock.patch.object(worker, "Database", return_value=_Db(self.conn)),
            mock.patch.object(worker, "load_config", return_value=CONFIG),
            mock.patch.object(worker, "load_active", return_value=(1, {}, "summary")),
            mock.patch.object(worker, "load_taxonomy", return_value=mock.MagicMock()),
            mock.patch.object(worker, "load_profile_adapter", return_value=adapter),
            mock.patch.object(worker, "_build_scorer", return_value=None),
            mock.patch.object(worker, "build_system", return_value="SYSTEM RULES"),
            mock.patch.object(worker, "build_graph", return_value=_Graph(states)),
            redirect_stdout(out),
        ):
            self.assertEqual(worker.run_scoring(), 0)
        return out.getvalue()

    def printed_cost(self, output: str) -> float:
        match = re.search(r"^cost:\s+\$([0-9.]+)$", output, re.MULTILINE)
        assert match is not None, output
        return float(match.group(1))

    def stored_cost(self, job_id: int) -> float:
        row = self.conn.execute(
            "SELECT cost_usd, tokens_in, tokens_out FROM job_verdicts WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        assert row is not None
        return row["cost_usd"]

    def test_the_retry_a_verdict_needed_is_stored_on_that_verdict(self) -> None:
        self.run_scoring(
            {
                1: {"verdict": dict(VERDICT), "usage": usage(1), "attempts": 1},
                2: {"verdict": dict(VERDICT), "usage": usage(3), "attempts": 3},
            }
        )
        self.assertAlmostEqual(self.stored_cost(1), usage_cost(MODEL, usage(1)))
        self.assertAlmostEqual(self.stored_cost(2), usage_cost(MODEL, usage(3)))
        # The whole point: the posting that took three calls is not billed as one.
        self.assertAlmostEqual(self.stored_cost(2), 3 * self.stored_cost(1))

    def test_a_posting_that_produced_no_verdict_is_still_charged_to_the_run(self) -> None:
        output = self.run_scoring(
            {
                1: {"verdict": dict(VERDICT), "usage": usage(1), "attempts": 1},
                2: {"verdict": None, "usage": usage(3), "attempts": 3, "error": "no tool call"},
            }
        )
        self.assertAlmostEqual(self.printed_cost(output), usage_cost(MODEL, usage(4)), places=4)
        # And it says so, because no verdict row can: summing job_verdicts.cost_usd over
        # this run recovers a quarter of the bill.
        self.assertIn("bought no verdict at all", output)

    def test_the_run_total_is_the_sum_of_the_verdict_rows_when_nothing_failed(self) -> None:
        output = self.run_scoring(
            {
                1: {"verdict": dict(VERDICT), "usage": usage(1), "attempts": 1},
                2: {"verdict": dict(VERDICT), "usage": usage(2), "attempts": 2},
            }
        )
        self.assertAlmostEqual(
            self.printed_cost(output),
            self.stored_cost(1) + self.stored_cost(2),
            places=4,
        )


if __name__ == "__main__":
    unittest.main()
