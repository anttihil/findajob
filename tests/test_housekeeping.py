"""Retention and rotation.

Both of these only matter once the thing runs unattended: under a systemd timer nothing
ever truncates app.log or empties raw_payloads/ by hand, and the failure is not an error
but a disk that fills up months later.
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
from logging.handlers import RotatingFileHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.logger import BACKUP_COUNT, MAX_BYTES, get_logger  # noqa: E402
from backend.sources.jobspy_source import prune_archives  # noqa: E402


class ArchivePruningTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _payload(self, name, age_days, size=1024):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as handle:
            handle.write(b"x" * size)
        stamp = time.time() - (age_days * 86400)
        os.utime(path, (stamp, stamp))
        return path

    def test_old_payloads_are_removed(self):
        self._payload("old.json", age_days=20)
        removed, freed = prune_archives(self.dir, max_age_days=14)
        self.assertEqual(removed, 1)
        self.assertEqual(freed, 1024)
        self.assertEqual(os.listdir(self.dir), [])

    def test_recent_payloads_survive(self):
        self._payload("fresh.json", age_days=3)
        removed, _ = prune_archives(self.dir, max_age_days=14)
        self.assertEqual(removed, 0)
        self.assertEqual(os.listdir(self.dir), ["fresh.json"])

    def test_boundary_is_not_off_by_a_day(self):
        """13d survives, 15d does not -- the window means what it says."""
        self._payload("day13.json", age_days=13)
        self._payload("day15.json", age_days=15)
        prune_archives(self.dir, max_age_days=14)
        self.assertEqual(sorted(os.listdir(self.dir)), ["day13.json"])

    def test_zero_retention_keeps_everything(self):
        """0 must mean 'keep', not 'delete all' -- the destructive misreading."""
        self._payload("ancient.json", age_days=900)
        removed, _ = prune_archives(self.dir, max_age_days=0)
        self.assertEqual(removed, 0)
        self.assertEqual(os.listdir(self.dir), ["ancient.json"])

    def test_missing_directory_is_not_an_error(self):
        """Housekeeping must never be what fails a scrape run."""
        self.assertEqual(prune_archives("/nonexistent/path", 14), (0, 0))

    def test_none_directory_is_not_an_error(self):
        self.assertEqual(prune_archives(None, 14), (0, 0))

    def test_subdirectories_are_left_alone(self):
        os.mkdir(os.path.join(self.dir, "keep_me"))
        old = time.time() - (100 * 86400)
        os.utime(os.path.join(self.dir, "keep_me"), (old, old))
        removed, _ = prune_archives(self.dir, max_age_days=14)
        self.assertEqual(removed, 0)
        self.assertTrue(os.path.isdir(os.path.join(self.dir, "keep_me")))


class LogRotationTests(unittest.TestCase):
    def test_file_handler_rotates(self):
        handlers = [h for h in get_logger().handlers
                    if isinstance(h, RotatingFileHandler)]
        self.assertEqual(len(handlers), 1, "expected exactly one rotating file handler")
        self.assertEqual(handlers[0].maxBytes, MAX_BYTES)
        self.assertEqual(handlers[0].backupCount, BACKUP_COUNT)

    def test_total_log_footprint_is_bounded(self):
        """The point of rotating at all: a fixed ceiling, not merely smaller files."""
        ceiling_mb = MAX_BYTES * (BACKUP_COUNT + 1) / 1048576
        self.assertLessEqual(ceiling_mb, 32)


if __name__ == "__main__":
    unittest.main()
