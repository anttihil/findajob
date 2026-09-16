"""Log rotation.

This checks that the *deployment* rotates the file, because the application
deliberately does not -- see findajob/core/logger.py.
"""

import logging
import os
import sys
import unittest
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from findajob.core.logger import get_logger


class LogRotationTests(unittest.TestCase):
    def test_the_application_does_not_rotate(self) -> None:
        """Four processes share this file; a rename-based rotation orphans three of them."""
        handlers = [h for h in get_logger().handlers if isinstance(h, logging.FileHandler)]
        self.assertEqual(len(handlers), 1, "expected exactly one file handler")
        self.assertNotIsInstance(handlers[0], RotatingFileHandler)
        self.assertNotIsInstance(handlers[0], TimedRotatingFileHandler)


if __name__ == "__main__":
    unittest.main()
