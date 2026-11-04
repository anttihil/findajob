import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from findajob.core.migrations import migrate
from findajob.core.scheduler import scheduler
from findajob.core.scheduler_preferences import (
    get_scheduler_preferences,
    update_scheduler_preferences,
)
from findajob.web.app import app
from findajob.web.live import live_hub


def test_scheduler_preference_defaults_to_disabled_and_persists() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        migrate(conn)
        assert get_scheduler_preferences(conn) == {"enabled": False, "updated_at": None}

        saved = update_scheduler_preferences(conn, enabled=True)
        assert saved["enabled"] is True
        assert saved["updated_at"] is not None
        assert get_scheduler_preferences(conn)["enabled"] is True
    finally:
        conn.close()


def test_scheduler_preference_api_persists_and_applies_change() -> None:
    client = TestClient(app)
    current = client.get("/api/scheduler/preferences")
    assert current.status_code == 200
    assert isinstance(current.json()["enabled"], bool)

    queue: asyncio.Queue[str | None] = asyncio.Queue()
    live_hub.subscribers.add(queue)
    try:
        with (
            patch.object(scheduler, "start", new_callable=AsyncMock) as start,
            patch.object(scheduler, "stop", new_callable=AsyncMock) as stop,
        ):
            enabled = client.put("/api/scheduler/preferences", json={"enabled": True})
            assert enabled.status_code == 200
            assert enabled.json()["enabled"] is True
            start.assert_awaited_once()
            enabled_msg = queue.get_nowait()
            assert enabled_msg is not None
            assert enabled_msg.startswith("event: scheduler_preferences\n")
            assert json.loads(enabled_msg.splitlines()[1].removeprefix("data: "))["enabled"] is True

            disabled = client.put("/api/scheduler/preferences", json={"enabled": False})
            assert disabled.status_code == 200
            assert disabled.json()["enabled"] is False
            stop.assert_awaited_once()
            disabled_msg = queue.get_nowait()
            assert disabled_msg is not None
            assert disabled_msg.startswith("event: scheduler_preferences\n")
            assert (
                json.loads(disabled_msg.splitlines()[1].removeprefix("data: "))["enabled"] is False
            )
    finally:
        live_hub.subscribers.discard(queue)
