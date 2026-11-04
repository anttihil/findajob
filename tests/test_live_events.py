"""Unit tests and starter test cases for SSE LiveEventHub.

TODO for implementation:
Unskip these tests and verify your implementation of `format_sse_message` and `LiveEventHub`.
"""

import asyncio
import json
import unittest
from unittest.mock import patch

from findajob.core.scheduler import scheduler
from findajob.web.live import LiveEventHub, format_sse_message


class TestLiveEventHub(unittest.IsolatedAsyncioTestCase):
    def test_format_sse_message(self):
        """Verify that SSE messages strictly follow the text/event-stream specification."""
        payload = {"status_counts": {"unread": 10, "saved": 2}, "total_jobs": 12}
        formatted = format_sse_message("stats_update", payload)

        self.assertTrue(formatted.startswith("event: stats_update\n"))
        self.assertTrue(formatted.endswith("\n\n"))
        self.assertIn("data: ", formatted)

        data_line = next(line for line in formatted.splitlines() if line.startswith("data: "))
        parsed = json.loads(data_line.removeprefix("data: "))
        self.assertEqual(parsed["total_jobs"], 12)
        self.assertEqual(parsed["status_counts"]["unread"], 10)

    async def test_subscribe_and_broadcast(self):
        """Verify that broadcasting puts formatted messages into active subscriber queues."""
        hub = LiveEventHub()
        gen = hub.subscribe()
        first_msg = await gen.__anext__()
        second_msg = await gen.__anext__()
        third_msg = await gen.__anext__()
        self.assertIn("event: pipeline_progress\n", first_msg)
        self.assertIn("event: stats_update\n", second_msg)
        self.assertIn("event: scheduler_preferences\n", third_msg)
        hub.broadcast("stats_update", {"total_jobs": 42})
        msg = await gen.__anext__()
        self.assertIn("stats_update", msg)
        self.assertIn("42", msg)
        await gen.aclose()

    async def test_subscribe_and_stop(self):
        """Verify that stopping the hub cleanly terminates subscriber generators."""
        hub = LiveEventHub()
        gen = hub.subscribe()
        await gen.__anext__()  # pipeline_progress
        await gen.__anext__()  # stats_update
        await gen.__anext__()  # scheduler_preferences
        hub.stop()
        with self.assertRaises(StopAsyncIteration):
            await gen.__anext__()

    async def test_check_updates_broadcasts_scheduler_preferences_on_change(self):
        """Verify scheduler preference/next_runs changes push over SSE once per change."""
        hub = LiveEventHub()
        hub._ping_counter = 1  # skip initial ping on first tick
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        hub.subscribers.add(queue)

        status_v1 = {
            "enabled": True,
            "is_running": True,
            "active_stage": None,
            "next_runs": {"search:indeed": "2026-11-04T18:00:00+00:00"},
        }
        status_v2 = {
            **status_v1,
            "next_runs": {"search:indeed": "2026-11-04T20:00:00+00:00"},
        }

        with patch.object(scheduler, "get_status", side_effect=[status_v1, status_v1, status_v2]):
            await hub.check_updates()
            self.assertTrue(queue.empty())

            await hub.check_updates()
            self.assertTrue(queue.empty())

            await hub.check_updates()
            msg = queue.get_nowait()

        assert msg is not None
        self.assertTrue(msg.startswith("event: scheduler_preferences\n"))
        data_line = next(line for line in msg.splitlines() if line.startswith("data: "))
        parsed = json.loads(data_line.removeprefix("data: "))
        self.assertEqual(
            parsed["status"]["next_runs"]["search:indeed"],
            "2026-11-04T20:00:00+00:00",
        )


if __name__ == "__main__":
    unittest.main()
