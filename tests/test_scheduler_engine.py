"""Tests for the Python-managed asyncio pipeline scheduler."""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.core.scheduler import PipelineScheduler, _now


class SchedulerEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.scheduler = PipelineScheduler()

    async def asyncTearDown(self) -> None:
        await self.scheduler.stop()

    def test_initial_status(self) -> None:
        status = self.scheduler.get_status()
        self.assertFalse(status["is_running"])
        self.assertIsNone(status["active_stage"])
        self.assertIsNone(status["active_started_at"])
        self.assertEqual(status["next_runs"], {})
        self.assertEqual(status["last_runs"], {})
        self.assertEqual(status["history"], [])

    def test_next_daily_time_calculation(self) -> None:
        schedule = ["01:00", "07:00", "13:00", "19:00"]
        next_time = self.scheduler._next_daily_time(schedule, jitter_minutes=0)
        self.assertIsInstance(next_time, datetime)
        self.assertGreater(next_time, _now())
        self.assertIn(next_time.hour, [1, 7, 13, 19])
        self.assertEqual(next_time.minute, 0)

    def test_next_daily_time_with_jitter(self) -> None:
        schedule = ["12:00"]
        next_time = self.scheduler._next_daily_time(schedule, jitter_minutes=15)
        self.assertIsInstance(next_time, datetime)
        self.assertGreater(next_time, _now())

    async def test_run_stage_executes_subprocess_successfully(self) -> None:
        mock_proc = mock.AsyncMock()
        mock_proc.returncode = 0
        mock_proc.pid = 12345
        mock_proc.stdout = None
        mock_proc.stderr = None
        mock_proc.wait = mock.AsyncMock(return_value=0)

        mock_cfg = {
            "scheduler": {
                "chaining": {"search_triggers_score": False, "score_triggers_research": False}
            }
        }

        with (
            mock.patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            mock.patch("careerradar.core.scheduler.load_config", return_value=mock_cfg),
        ):
            success = await self.scheduler._run_stage("search", manual=True)
            self.assertTrue(success)

            status = self.scheduler.get_status()
            self.assertIn("search", status["last_runs"])
            self.assertEqual(status["last_runs"]["search"]["status"], "ok")
            self.assertEqual(status["last_runs"]["search"]["returncode"], 0)
            self.assertEqual(len(status["history"]), 1)

    async def test_run_stage_handles_process_failure(self) -> None:
        mock_proc = mock.AsyncMock()
        mock_proc.returncode = 1
        mock_proc.pid = 12346
        mock_proc.stdout = None
        mock_proc.stderr = None
        mock_proc.wait = mock.AsyncMock(return_value=1)

        with mock.patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            success = await self.scheduler._run_stage("score", manual=True)
            self.assertFalse(success)

            status = self.scheduler.get_status()
            self.assertIn("score", status["last_runs"])
            self.assertEqual(status["last_runs"]["score"]["status"], "error")
            self.assertEqual(status["last_runs"]["score"]["returncode"], 1)

    async def test_run_stage_skips_when_stage_locked(self) -> None:
        await self.scheduler._stage_locks["search"].acquire()
        try:
            success = await self.scheduler._run_stage("search", manual=True)
            self.assertFalse(success)
        finally:
            self.scheduler._stage_locks["search"].release()

    async def test_trigger_invalid_stage_raises(self) -> None:
        with self.assertRaises(ValueError):
            await self.scheduler.trigger("invalid_stage")

    async def test_boot_catchup_detects_gap(self) -> None:
        tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp_db.close()
        self.addCleanup(os.unlink, tmp_db.name)

        conn = sqlite3.connect(tmp_db.name)
        conn.execute(
            "CREATE TABLE sync_runs (id INTEGER PRIMARY KEY, status TEXT, finished_at TEXT)"
        )
        conn.execute("INSERT INTO sync_runs VALUES (1, 'ok', '2026-01-01T00:00:00+00:00')")
        conn.commit()
        conn.close()

        self.scheduler._running = True

        with (
            mock.patch("careerradar.core.scheduler.DB_PATH", tmp_db.name),
            mock.patch.object(self.scheduler, "_run_stage", return_value=True) as mock_run,
            mock.patch("asyncio.sleep", return_value=None),
        ):
            await self.scheduler._boot_catchup_check()
            mock_run.assert_awaited_once_with("search", manual=False)

    async def test_chaining_search_triggers_score(self) -> None:
        mock_proc = mock.AsyncMock()
        mock_proc.returncode = 0
        mock_proc.pid = 12347
        mock_proc.stdout = None
        mock_proc.stderr = None
        mock_proc.wait = mock.AsyncMock(return_value=0)

        mock_cfg = {
            "scheduler": {
                "chaining": {"search_triggers_score": True, "score_triggers_research": False}
            }
        }

        with (
            mock.patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            mock.patch("careerradar.core.scheduler.load_config", return_value=mock_cfg),
        ):
            success = await self.scheduler._run_stage("search", manual=True)
            self.assertTrue(success)
            import asyncio

            await asyncio.sleep(0.05)
            status = self.scheduler.get_status()
            self.assertIn("search", status["last_runs"])
            self.assertIn("score", status["last_runs"])


if __name__ == "__main__":
    unittest.main()
