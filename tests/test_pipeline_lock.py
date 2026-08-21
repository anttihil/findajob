"""The pipeline stages must not write to SQLite at the same time.

The failure this prevents is silent in a spot check: two stages both commit per posting,
so each one looks correct on its own, and the collision only shows up as `database is
locked` after 30 seconds of contention -- on whichever stage happened to lose.
"""

import os
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.cli import build_parser
from careerradar.core import pipeline_lock

# A child that holds the lock for `seconds`, and says so on stdout once it has it.
HOLDER = """
import sys, time
sys.path.insert(0, {root!r})
from careerradar.core import pipeline_lock
with pipeline_lock.hold("holder"):
    print("held", flush=True)
    time.sleep({seconds})
"""


def _holder(seconds: float) -> subprocess.Popen[str]:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER.format(root=root, seconds=seconds)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "held"
    return proc


class TestPipelineLock(unittest.TestCase):
    def test_second_stage_waits_for_the_first(self) -> None:
        holder = _holder(1.5)
        started = time.monotonic()
        with pipeline_lock.hold("waiter"):
            waited = time.monotonic() - started
        holder.wait()
        self.assertGreaterEqual(waited, 1.0)

    def test_lock_is_released_on_exit(self) -> None:
        with pipeline_lock.hold("first"):
            pass
        started = time.monotonic()
        with pipeline_lock.hold("second"):
            pass
        self.assertLess(time.monotonic() - started, 0.5)

    def test_killed_holder_does_not_wedge_the_pipeline(self) -> None:
        """The kernel drops an flock when the holder dies.

        This is the whole reason the lock is an flock and not a row or a status file: the
        sync_status.json lock needed STALE_LOCK_MINUTES to recover from a crashed run.
        """
        holder = _holder(60)
        holder.kill()
        holder.wait()
        started = time.monotonic()
        with pipeline_lock.hold("after-crash"):
            pass
        self.assertLess(time.monotonic() - started, 0.5)


class TestStagesTakeTheLock(unittest.TestCase):
    def _stage(self, argv: list[str]) -> str | None:
        return build_parser().parse_args(argv).stage

    def test_writing_commands_are_serialized(self) -> None:
        self.assertEqual(self._stage(["search", "run"]), "search")
        self.assertEqual(self._stage(["score", "run"]), "score")
        self.assertEqual(self._stage(["research", "run"]), "research")
        self.assertEqual(self._stage(["migrate"]), "migrate")

    def test_read_only_commands_are_not(self) -> None:
        """A report must not block for the 20 minutes a scrape takes."""
        self.assertIsNone(self._stage(["status"]))
        self.assertIsNone(self._stage(["score", "stats"]))
        self.assertIsNone(self._stage(["search", "cost"]))
        self.assertIsNone(self._stage(["web"]))


if __name__ == "__main__":
    unittest.main()
