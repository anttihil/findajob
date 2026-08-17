"""Retention and rotation.

Both of these only matter once the thing runs unattended: under a systemd timer nothing
ever truncates app.log or empties raw_payloads/ by hand, and the failure is not an error
but a disk that fills up months later.

The log half now checks that the *deployment* rotates the file, because the application
deliberately does not -- see careerradar/core/logger.py.
"""

import logging
import os
import shutil
import sys
import tempfile
import time
import unittest
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.core.logger import get_logger
from careerradar.core.paths import REPO_ROOT
from careerradar.search.sources.jobspy_source import prune_archives


class ArchivePruningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _payload(self, name: str, age_days: float, size: int = 1024) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "wb") as handle:
            handle.write(b"x" * size)
        stamp = time.time() - (age_days * 86400)
        os.utime(path, (stamp, stamp))
        return path

    def test_old_payloads_are_removed(self) -> None:
        self._payload("old.json", age_days=20)
        removed, freed = prune_archives(self.dir, max_age_days=14)
        self.assertEqual(removed, 1)
        self.assertEqual(freed, 1024)
        self.assertEqual(os.listdir(self.dir), [])

    def test_recent_payloads_survive(self) -> None:
        self._payload("fresh.json", age_days=3)
        removed, _ = prune_archives(self.dir, max_age_days=14)
        self.assertEqual(removed, 0)
        self.assertEqual(os.listdir(self.dir), ["fresh.json"])

    def test_boundary_is_not_off_by_a_day(self) -> None:
        """13d survives, 15d does not -- the window means what it says."""
        self._payload("day13.json", age_days=13)
        self._payload("day15.json", age_days=15)
        prune_archives(self.dir, max_age_days=14)
        self.assertEqual(sorted(os.listdir(self.dir)), ["day13.json"])

    def test_zero_retention_keeps_everything(self) -> None:
        """0 must mean 'keep', not 'delete all' -- the destructive misreading."""
        self._payload("ancient.json", age_days=900)
        removed, _ = prune_archives(self.dir, max_age_days=0)
        self.assertEqual(removed, 0)
        self.assertEqual(os.listdir(self.dir), ["ancient.json"])

    def test_missing_directory_is_not_an_error(self) -> None:
        """Housekeeping must never be what fails a scrape run."""
        self.assertEqual(prune_archives("/nonexistent/path", 14), (0, 0))

    def test_none_directory_is_not_an_error(self) -> None:
        self.assertEqual(prune_archives(None, 14), (0, 0))

    def test_subdirectories_are_left_alone(self) -> None:
        os.mkdir(os.path.join(self.dir, "keep_me"))
        old = time.time() - (100 * 86400)
        os.utime(os.path.join(self.dir, "keep_me"), (old, old))
        removed, _ = prune_archives(self.dir, max_age_days=14)
        self.assertEqual(removed, 0)
        self.assertTrue(os.path.isdir(os.path.join(self.dir, "keep_me")))


class LogRotationTests(unittest.TestCase):
    def test_the_application_does_not_rotate(self) -> None:
        """Four processes share this file; a rename-based rotation orphans three of them."""
        handlers = [h for h in get_logger().handlers if isinstance(h, logging.FileHandler)]
        self.assertEqual(len(handlers), 1, "expected exactly one file handler")
        self.assertNotIsInstance(handlers[0], RotatingFileHandler)
        self.assertNotIsInstance(handlers[0], TimedRotatingFileHandler)

    def test_the_deployment_does_rotate(self) -> None:
        """Nothing else bounds the file now, and copytruncate is why it is safe to share."""
        snippet = os.path.join(REPO_ROOT, "deploy", "careerradar.logrotate")
        with open(snippet) as handle:
            body = handle.read()
        self.assertIn("copytruncate", body)
        self.assertIn("app.log", body)


if __name__ == "__main__":
    unittest.main()
