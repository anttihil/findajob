"""Error capture around JobSpy.

The library's LinkedIn scraper catches network faults inside its pagination loop, logs
them, and returns the rows it already had. Nothing raises, so the circuit breaker sees a
healthy run and a truncated cell is stored as a complete count. These tests pin the seam
that turns those log lines back into exceptions.
"""

import logging
import os
import sys
import unittest
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.search.guard import (
    ERROR_BLOCKED,
    ERROR_FATAL,
    ERROR_RATE_LIMIT,
    ERROR_TRANSIENT,
    classify_error,
)
from careerradar.search.sources.jobspy_source import (
    JOBSPY_LOGGERS,
    ScraperReportedError,
    capture_scraper_errors,
)

# Verbatim from jobspy/linkedin/__init__.py, so a library reword shows up as a test failure
# rather than as silently-uncaught errors in production.
TIMEOUT_MESSAGE = (
    "LinkedIn: HTTPSConnectionPool(host='www.linkedin.com', port=443): Max retries "
    "exceeded with url: /jobs-guest/jobs/api/seeMoreJobPostings/search "
    '(Caused by ReadTimeoutError("Read timed out. (read timeout=10)"))'
)
RATE_LIMIT_MESSAGE = "429 Response - Blocked by LinkedIn for too many requests"
BAD_PROXY_MESSAGE = "LinkedIn: Bad proxy"


class CaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.board = logging.getLogger(JOBSPY_LOGGERS["linkedin"])

    def test_error_records_are_collected(self) -> None:
        with capture_scraper_errors("linkedin") as reported:
            self.board.error(TIMEOUT_MESSAGE)
        self.assertEqual(len(reported.messages), 1)
        self.assertIn("Read timed out", reported.messages[0])

    def test_info_and_warning_are_ignored(self) -> None:
        """Only failures. JobSpy logs 'finished scraping' at INFO on every single run."""
        with capture_scraper_errors("linkedin") as reported:
            self.board.info("finished scraping")
            self.board.warning("something mildly interesting")
        self.assertEqual(reported.messages, [])

    def test_handler_is_removed_afterwards(self) -> None:
        before = len(self.board.handlers)
        with capture_scraper_errors("linkedin"):
            self.assertEqual(len(self.board.handlers), before + 1)
        self.assertEqual(len(self.board.handlers), before)

    def test_handler_is_removed_even_when_the_scrape_raises(self) -> None:
        before = len(self.board.handlers)
        with self.assertRaises(ValueError), capture_scraper_errors("linkedin"):
            raise ValueError("scrape blew up")
        self.assertEqual(len(self.board.handlers), before)

    def test_records_do_not_leak_between_captures(self) -> None:
        with capture_scraper_errors("linkedin") as first:
            self.board.error(TIMEOUT_MESSAGE)
        with capture_scraper_errors("linkedin") as second:
            pass
        self.assertEqual(len(first.messages), 1)
        self.assertEqual(second.messages, [])

    def test_unknown_source_is_inert_rather_than_fatal(self) -> None:
        with capture_scraper_errors("glassdoor") as reported:
            self.assertEqual(reported.messages, [])

    def test_none_source_is_inert(self) -> None:
        with capture_scraper_errors(None) as reported:
            self.assertEqual(reported.messages, [])

    def test_indeed_logger_is_watched_too(self) -> None:
        indeed = logging.getLogger(JOBSPY_LOGGERS["indeed"])
        with capture_scraper_errors("indeed") as reported:
            indeed.error("Indeed: something went wrong")
        self.assertEqual(len(reported.messages), 1)

    def test_capture_is_scoped_to_the_requested_board(self) -> None:
        indeed = logging.getLogger(JOBSPY_LOGGERS["indeed"])
        with capture_scraper_errors("linkedin") as reported:
            indeed.error("Indeed: unrelated failure")
        self.assertEqual(reported.messages, [])


class ClassificationTests(unittest.TestCase):
    """The captured wording must survive into the right circuit-breaker decision."""

    def _classify(self, board_message: str, rows: int = 0) -> str:
        exc = ScraperReportedError(
            f"linkedin reported 1 error(s) after {rows} row(s): {board_message}",
            board_messages=[board_message],
        )
        return classify_error(exc)

    def test_timeout_is_retryable(self) -> None:
        self.assertEqual(self._classify(TIMEOUT_MESSAGE, rows=25), ERROR_TRANSIENT)

    def test_rate_limit_beats_the_word_blocked(self) -> None:
        """The 429 text also contains 'Blocked'; it must not classify as a block."""
        self.assertEqual(self._classify(RATE_LIMIT_MESSAGE), ERROR_RATE_LIMIT)

    def test_forbidden_is_blocked(self) -> None:
        self.assertEqual(
            self._classify("LinkedIn response status code 403 - Forbidden"),
            ERROR_BLOCKED,
        )

    def test_bad_proxy_is_retryable(self) -> None:
        """Pinning means the retry uses a different exit IP, so this is worth retrying."""
        self.assertEqual(self._classify(BAD_PROXY_MESSAGE), ERROR_TRANSIENT)

    def test_server_error_is_retryable(self) -> None:
        self.assertEqual(self._classify("LinkedIn response status code 503"), ERROR_TRANSIENT)

    def test_unrecognised_wording_stays_fatal(self) -> None:
        """Unknown failures must not be quietly retried against a board."""
        self.assertEqual(self._classify("something entirely unexpected"), ERROR_FATAL)

    def test_wrapper_text_does_not_itself_trigger_a_marker(self) -> None:
        """Our own row/error counts must not be able to trip the source.

        Caught a real defect: classification read the whole formatted message, so
        "after 429 row(s)" classified as a rate limit and would have tripped LinkedIn
        with an escalating backoff over a number this code wrote itself.
        """
        exc = ScraperReportedError(
            "linkedin reported 500 error(s) after 429 row(s): plain failure",
            board_messages=["plain failure"],
        )
        self.assertEqual(classify_error(exc), ERROR_FATAL)

    def test_classification_ignores_the_wrapper_entirely(self) -> None:
        """A board timeout stays transient however alarming the surrounding prose is."""
        exc = ScraperReportedError(
            "linkedin reported 403 error(s) after 429 row(s): read timed out",
            board_messages=["LinkedIn: Read timed out. (read timeout=10)"],
        )
        self.assertEqual(classify_error(exc), ERROR_TRANSIENT)

    def test_plain_exceptions_still_classify_on_their_text(self) -> None:
        """No classify_text attribute: fall back to the exception's own message."""
        self.assertEqual(classify_error(TimeoutError("read timed out")), ERROR_TRANSIENT)
        self.assertEqual(classify_error(RuntimeError("429 too many requests")), ERROR_RATE_LIMIT)


if __name__ == "__main__":
    unittest.main()


class FetchWiringTests(unittest.TestCase):
    """The seam itself: a board that logs instead of raising must still raise here.

    Stubs the scrape callable and frame_to_rows rather than importing pandas, keeping the
    suite runnable without pandas/numpy/tls-client as the module docstring intends.
    """

    def setUp(self) -> None:
        import careerradar.search.sources.jobspy_source as module

        self.module = module
        self.source = module.JobSpySource(archive_dir=None)
        self.board = logging.getLogger(JOBSPY_LOGGERS["linkedin"])

        self._real_frame_to_rows = module.frame_to_rows
        module.frame_to_rows = lambda frame: list(frame or [])
        self.addCleanup(setattr, module, "frame_to_rows", self._real_frame_to_rows)

    def _task(self) -> dict[str, Any]:
        return {
            "source": "linkedin",
            "query": "AI Engineer",
            "country": "US",
            "indeed_country": "usa",
            "location_label": "Los Angeles, CA",
            "results_wanted": 50,
            "is_remote": False,
            "distance": 50,
            "hours_old": 336,
            "fetch_description": True,
        }

    def test_logged_failure_becomes_an_exception(self) -> None:
        def fake_scrape(**kwargs: Any) -> list[dict[str, Any]]:
            self.board.error(TIMEOUT_MESSAGE)
            return [{"title": "partial row"}]  # what JobSpy actually returns

        self.source._scrape = fake_scrape
        with self.assertRaises(ScraperReportedError) as caught:
            self.source.fetch_for_task(self._task())

        # Classification must reach the circuit breaker as retryable, not as a clean run.
        self.assertEqual(classify_error(caught.exception), ERROR_TRANSIENT)
        self.assertIn("Read timed out", caught.exception.classify_text)

    def test_partial_row_count_is_reported(self) -> None:
        """The operator needs to see it was truncated, not merely that it failed."""

        def fake_scrape(**kwargs: Any) -> list[dict[str, Any]]:
            self.board.error(TIMEOUT_MESSAGE)
            return [{"title": "a"}, {"title": "b"}]

        self.source._scrape = fake_scrape
        with self.assertRaises(ScraperReportedError) as caught:
            self.source.fetch_for_task(self._task())
        self.assertIn("after 2 row(s)", str(caught.exception))

    def test_clean_scrape_still_returns_rows(self) -> None:
        def fake_scrape(**kwargs: Any) -> list[dict[str, Any]]:
            self.board.info("finished scraping")
            return [{"title": "a"}]

        self.source._scrape = fake_scrape
        self.assertEqual(self.source.fetch_for_task(self._task()), [{"title": "a"}])

    def test_payload_is_archived_before_raising(self) -> None:
        """A truncated payload is exactly the one worth replaying."""
        import tempfile

        archive = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, archive, True)
        source = self.module.JobSpySource(archive_dir=archive)

        def fake_scrape(**kwargs: Any) -> list[dict[str, Any]]:
            self.board.error(TIMEOUT_MESSAGE)
            return [{"title": "partial"}]

        source._scrape = fake_scrape
        with self.assertRaises(ScraperReportedError):
            source.fetch_for_task(self._task())
        self.assertEqual(len(os.listdir(archive)), 1)
