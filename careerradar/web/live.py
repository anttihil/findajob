"""Server-Sent Events (SSE) event hub for real-time dashboard feeds.

ARCHITECTURE HINTS & EXPLANATIONS:
----------------------------------
1. Multi-Process Change Detection:
   Because the scraper (`careerradar search run`) and scoring worker (`careerradar score run`)
   run as separate CLI commands or systemd services, they write to SQLite without touching
   the web server process memory.
   SQLite provides `PRAGMA data_version`:
   - It returns an integer that increments whenever *another* connection commits a transaction.
   - Querying `PRAGMA data_version` is fast (~0.05ms) and does not lock the database.
   - An async monitor loop checking `PRAGMA data_version` every 1-2 seconds can detect external
     writes with minimal CPU and zero contention.

2. SSE Event Format (text/event-stream):
   Each message sent to the client is formatted as:
       event: <event_name>\n
       data: <json_string>\n
       \n
   For keep-alive / heartbeats:
       event: ping\n
       data: {}\n
       \n

3. Connection Management:
   - When a browser connects to GET /api/live/events, `subscribe()` creates an `asyncio.Queue`.
   - The queue is added to `self.subscribers`.
   - When the client disconnects, `subscribe()` removes the queue from `self.subscribers`.
   - `broadcast(event_type, data)` puts the formatted payload into all active queues.
"""

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import Any

from careerradar.core.database import Database
from careerradar.core.status_manager import is_sync_running


def format_sse_message(event_type: str, data: dict[str, Any]) -> str:
    """Format a Python dictionary as a standard Server-Sent Event string.

    HINT:
    The output string must follow the SSE specification:
        "event: {event_type}\\ndata: {json_str}\\n\\n"

    TODO for implementation:
    1. Serialize `data` to a JSON string with `json.dumps()`.
    2. Return formatted string matching the SSE specification.
    """
    json_str = json.dumps(data)
    return f"event: {event_type}\ndata: {json_str}\n\n"


class LiveEventHub:
    """Manages active SSE connections and broadcasts SQLite/pipeline updates.

    IMPLEMENTATION GUIDE:
    1. `subscribe()`:
       - Instantiates `asyncio.Queue[str](maxsize=50)`.
       - Adds it to `self.subscribers`.
       - Sends an initial snapshot of stats and pipeline status so new clients don't wait.
       - Yields items from `await queue.get()` in a `try/finally` block that cleans up the queue.

    2. `broadcast()`:
       - Iterates through `self.subscribers`.
       - Formats message using `format_sse_message(event_type, data)`.
       - Uses `put_nowait()` and catches `asyncio.QueueFull` to drop messages for slow clients.

    3. `start_monitor()`:
       - Runs an async `while True:` loop sleeping ~1.5s per tick.
       - Connects to SQLite and checks `PRAGMA data_version` and `is_sync_running()`.
       - When changes occur, calls `broadcast("stats_update", ...)` and
         `broadcast("jobs_changed", ...)`.
       - Periodically (every ~15s) broadcasts a `"ping"` event to prevent timeouts.
    """

    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue[str]] = set()
        self._last_data_version: int | None = None
        self._last_sync_running: bool = False
        self._ping_counter: int = 0

    async def subscribe(self) -> AsyncGenerator[str, None]:
        """Async generator yielding SSE events to a connected client.

        TODO for implementation:
        1. Create an `asyncio.Queue[str](maxsize=50)` and add it to `self.subscribers`.
        2. Optionally yield an initial snapshot (e.g. current stats or pipeline status).
        3. Loop `msg = await queue.get()` and `yield msg`.
        4. In the `finally` block, remove the queue from `self.subscribers`.
        """
        queue = asyncio.Queue[str](maxsize=50)
        self.subscribers.add(queue)

        db = Database()
        sync_running = is_sync_running()

        try:
            yield format_sse_message(
                "pipeline_progress", self._get_pipeline_status(db, sync_running)
            )
            yield format_sse_message("stats_update", db.get_stats())
        finally:
            db.close()

        try:
            while True:
                msg = await queue.get()
                yield msg
        finally:
            self.subscribers.discard(queue)

    def broadcast(self, event_type: str, data: dict[str, Any]) -> None:
        """Broadcast an event to all connected subscriber queues.

        TODO for implementation:
        1. If `self.subscribers` is empty, return early.
        2. Format message with `format_sse_message(event_type, data)`.
        3. Iterate through `list(self.subscribers)` and call `queue.put_nowait(msg)`.
        4. Handle `asyncio.QueueFull` gracefully by ignoring or dropping.
        """
        if not self.subscribers:
            return

        msg = format_sse_message(event_type, data)

        for sub in list(self.subscribers):
            with suppress(asyncio.QueueFull):
                sub.put_nowait(msg)

    async def check_updates(self, db: Database | None = None) -> None:
        """Single check tick to detect database and pipeline state changes.

        TODO for implementation:
        1. Open SQLite database if not provided.
        2. Query `current_version = db.conn.execute("PRAGMA data_version").fetchone()[0]`.
        3. Query `sync_running = is_sync_running()`.
        4. If `_last_data_version is not None` and `current_version != _last_data_version`:
           - Fetch updated stats via `db.get_stats()` and broadcast `"stats_update"`.
           - Broadcast `"jobs_changed"` with `{ "version": current_version }`.
        5. If `sync_running` or `_last_sync_running != sync_running`:
           - Broadcast `"pipeline_progress"`.
        6. Handle keep-alive heartbeat: every ~10 ticks, broadcast `"ping"`.
        7. Update `_last_data_version` and `_last_sync_running`.
        """
        if not db:
            db = Database()

        try:
            current_version = db.conn.execute("PRAGMA data_version").fetchone()[0]
            sync_running = is_sync_running()

            if self._last_data_version is not None and current_version != self._last_data_version:
                stats = db.get_stats()
                self.broadcast(event_type="stats_update", data=stats)
                self.broadcast(event_type="jobs_changed", data={"version": current_version})
            if sync_running or self._last_sync_running != sync_running:
                self.broadcast(
                    event_type="pipeline_progress", data=self._get_pipeline_status(db, sync_running)
                )
            self._last_data_version = current_version
            self._last_sync_running = sync_running

            if self._ping_counter % 10 == 0:
                self.broadcast(event_type="ping", data={})

            self._ping_counter += 1
        finally:
            db.close()

    async def start_monitor(self, poll_interval_seconds: float = 1.5) -> None:
        """Long-running async background task to monitor for database/pipeline updates.

        TODO for implementation:
        1. Run `while True:` loop calling `await self.check_updates()`.
        2. Catch `asyncio.CancelledError` to cleanly exit.
        3. `await asyncio.sleep(poll_interval_seconds)`.
        """
        while True:
            with suppress(asyncio.CancelledError):
                await self.check_updates()
            await asyncio.sleep(poll_interval_seconds)

    def _get_pipeline_status(self, db: Database, sync_running: bool):
        from careerradar.core import status_repository as status_repo

        return status_repo.get_pipeline_status(
            db.conn,
            sync_running=sync_running,
            db_instance=db,
        )


# Global singleton instance for the FastAPI web server
live_hub = LiveEventHub()
