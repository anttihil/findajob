"""Unit tests and starter test cases for SSE LiveEventHub.

TODO for implementation:
Unskip these tests and verify your implementation of `format_sse_message` and `LiveEventHub`.
"""

import json
import unittest

from careerradar.web.live import LiveEventHub, format_sse_message


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
        self.assertIn("pipeline_progress", first_msg)
        hub.broadcast("stats_update", {"total_jobs": 42})
        msg = await gen.__anext__()
        self.assertIn("stats_update", msg)

    async def test_subscribe_and_stop(self):
        """Verify that stopping the hub cleanly terminates subscriber generators."""
        hub = LiveEventHub()
        gen = hub.subscribe()
        await gen.__anext__()  # pipeline_progress
        await gen.__anext__()  # stats_update
        hub.stop()
        with self.assertRaises(StopAsyncIteration):
            await gen.__anext__()


if __name__ == "__main__":
    unittest.main()
