"""Asyncio-based pipeline scheduler with subprocess worker isolation.

Replaces systemd timers with a cross-platform, config-driven scheduling engine.
Executes pipeline stages (search, score) as isolated CLI subprocesses to
guarantee complete OS memory reclamation and fault isolation.
"""

import asyncio
import random
import sqlite3
import sys
from collections import deque
from datetime import datetime, time, timedelta, timezone
from typing import Any

from findajob.core.config import load_config
from findajob.core.logger import get_logger
from findajob.core.paths import DB_PATH
from findajob.core.scheduler_preferences import (
    get_scheduler_preferences,
    update_scheduler_preferences,
)

logger = get_logger()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


class PipelineScheduler:
    """Manages scheduled and chained execution of Find a Job pipeline stages."""

    def __init__(self) -> None:
        self._running: bool = False
        self._enabled: bool = False
        self._tasks: list[asyncio.Task[None]] = []
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._active_stage: str | None = None
        self._active_proc: asyncio.subprocess.Process | None = None
        self._active_started_at: datetime | None = None
        self._stage_locks: dict[str, asyncio.Lock] = {
            "search": asyncio.Lock(),
            "score": asyncio.Lock(),
        }
        self._next_runs: dict[str, datetime] = {}
        self._last_runs: dict[str, dict[str, Any]] = {}
        self._history: deque[dict[str, Any]] = deque(maxlen=50)

    @property
    def is_running(self) -> bool:
        return self._running

    def get_status(self) -> dict[str, Any]:
        """Returns the current operational status of the scheduler."""
        return {
            "enabled": self._enabled,
            "is_running": self._running,
            "active_stage": self._active_stage,
            "active_started_at": _iso(self._active_started_at),
            "next_runs": {k: _iso(v) for k, v in self._next_runs.items()},
            "last_runs": self._last_runs,
            "history": list(self._history),
        }

    async def start(self) -> None:
        """Starts the scheduler engine and schedules all configured stages."""
        if self._running:
            return

        config = load_config().get("scheduler", {})
        with sqlite3.connect(DB_PATH) as conn:
            preferences = get_scheduler_preferences(conn)
            # Preserve the opt-in choice of installations that used the pre-v30 YAML
            # setting. Once imported, the database is authoritative.
            if preferences["updated_at"] is None and config.get("enabled", False):
                preferences = update_scheduler_preferences(conn, enabled=True)
        self._enabled = preferences["enabled"]
        if not self._enabled:
            logger.info("Pipeline scheduler is disabled by user preference.")
            return

        self._running = True
        logger.info("Starting Find a Job background scheduler...")

        if config.get("search", {}).get("persistent_catchup", True):
            self._tasks.append(asyncio.create_task(self._boot_catchup_check()))

        self._tasks.append(asyncio.create_task(self._schedule_loop_daily("search")))
        self._tasks.append(asyncio.create_task(self._schedule_loop_interval("score")))

    async def stop(self) -> None:
        """Stops the scheduler and terminates any active worker subprocess."""
        self._enabled = False
        if not self._running:
            return

        self._running = False
        logger.info("Stopping Find a Job scheduler...")

        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

        for bg in self._bg_tasks:
            bg.cancel()
        self._bg_tasks.clear()

        # Terminate active subprocess if running
        if self._active_proc and self._active_proc.returncode is None:
            logger.warning(
                "Terminating active %s worker on scheduler shutdown (PID %s)...",
                self._active_stage,
                self._active_proc.pid,
            )
            try:
                self._active_proc.terminate()
                await asyncio.sleep(2)
                if self._active_proc.returncode is None:
                    self._active_proc.kill()
            except (OSError, ProcessLookupError) as e:
                logger.error("Error terminating worker process: %s", e)

        self._active_stage = None
        self._active_proc = None
        self._active_started_at = None

    async def trigger(self, stage: str, manual: bool = True) -> bool:
        """Triggers a stage run asynchronously."""
        if stage not in self._stage_locks:
            raise ValueError(f"Unknown pipeline stage: {stage}")

        return await self._run_stage(stage, manual=manual)

    async def _boot_catchup_check(self) -> None:
        """Checks if a scrape was missed while the host was asleep or offline."""
        await asyncio.sleep(5)  # Wait 5 seconds after startup for initial stabilization
        if not self._running:
            return

        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT finished_at FROM sync_runs WHERE status = 'ok' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            conn.close()

            last_finished_str = row["finished_at"] if row else None
            should_catchup = False

            if not last_finished_str:
                should_catchup = True
            else:
                last_finished = datetime.fromisoformat(last_finished_str)
                if last_finished.tzinfo is None:
                    last_finished = last_finished.replace(tzinfo=timezone.utc)
                hours_since = (_now() - last_finished).total_seconds() / 3600.0
                if hours_since >= 7.0:
                    should_catchup = True
                    logger.info(
                        "Catch-up scrape triggered: last scrape was %.1fh ago (Persistent=true).",
                        hours_since,
                    )

            if should_catchup:
                await self._run_stage("search", manual=False)
        except (sqlite3.Error, OSError) as e:
            logger.error("Error during boot catch-up check: %s", e)

    def _next_daily_time(self, time_strs: list[str], jitter_minutes: int) -> datetime:
        """Calculates next upcoming time from a list of 'HH:MM' strings with jitter."""
        now = _now()
        candidates: list[datetime] = []

        for t_str in time_strs:
            try:
                parts = [int(p) for p in t_str.split(":")]
                t = time(hour=parts[0], minute=parts[1])
            except (ValueError, IndexError):
                continue

            today_target = datetime.combine(now.date(), t, tzinfo=timezone.utc)
            if jitter_minutes > 0:
                jitter_sec = random.uniform(-jitter_minutes * 60, jitter_minutes * 60)
                today_target += timedelta(seconds=jitter_sec)

            if today_target > now:
                candidates.append(today_target)
            else:
                tomorrow_target = today_target + timedelta(days=1)
                candidates.append(tomorrow_target)

        if not candidates:
            return now + timedelta(hours=6)
        return min(candidates)

    async def _schedule_loop_daily(self, stage: str) -> None:
        """Loop for stages scheduled on fixed daily times (search)."""
        while self._running:
            try:
                config = load_config().get("scheduler", {}).get(stage, {})
                default_times = (
                    ["01:00", "07:00", "13:00", "19:00"] if stage == "search" else ["20:30"]
                )
                schedule = config.get("schedule", default_times)
                jitter = config.get("jitter_minutes", 30 if stage == "search" else 20)

                next_run = self._next_daily_time(schedule, jitter)
                self._next_runs[stage] = next_run

                delay = max(0.0, (next_run - _now()).total_seconds())
                logger.info(
                    "%s scheduled next at %s (in %.1f minutes)",
                    stage.capitalize(),
                    next_run.isoformat(),
                    delay / 60.0,
                )

                await asyncio.sleep(delay)
                if not self._running:
                    break

                await self._run_stage(stage, manual=False)
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.error("Error in %s daily schedule loop: %s", stage, e)
                await asyncio.sleep(60)

    async def _schedule_loop_interval(self, stage: str) -> None:
        """Loop for stages scheduled on recurring intervals (score)."""
        while self._running:
            try:
                config = load_config().get("scheduler", {}).get(stage, {})
                interval_min = config.get("interval_minutes", 30)
                jitter_min = config.get("jitter_minutes", 3)

                jitter_sec = (
                    random.uniform(-jitter_min * 60, jitter_min * 60) if jitter_min > 0 else 0
                )
                delay = max(30.0, (interval_min * 60) + jitter_sec)

                next_run = _now() + timedelta(seconds=delay)
                self._next_runs[stage] = next_run
                logger.debug(
                    "%s scheduled next at %s (in %.1f minutes)",
                    stage.capitalize(),
                    next_run.isoformat(),
                    delay / 60.0,
                )

                await asyncio.sleep(delay)
                if not self._running:
                    break

                await self._run_stage(stage, manual=False)
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.error("Error in %s interval schedule loop: %s", stage, e)
                await asyncio.sleep(60)

    async def _run_stage(self, stage: str, manual: bool = False) -> bool:
        """Executes a pipeline stage in an isolated CLI subprocess with timeout handling."""
        stage_lock = self._stage_locks[stage]
        if stage_lock.locked():
            logger.warning("Stage '%s' is already executing; skipping new run request.", stage)
            return False

        async with stage_lock:
            config = load_config().get("scheduler", {})
            stage_config = config.get(stage, {})
            timeout_min = stage_config.get("timeout_minutes", 90)
            timeout_sec = timeout_min * 60

            started_at = _now()
            self._active_stage = stage
            self._active_started_at = started_at

            cmd = [sys.executable, "-m", "findajob.cli", stage, "run"]
            logger.info(
                "Executing pipeline stage '%s' (manual=%s, timeout=%dm)...",
                stage,
                manual,
                timeout_min,
            )

            run_record: dict[str, Any] = {
                "stage": stage,
                "manual": manual,
                "started_at": _iso(started_at),
                "finished_at": None,
                "status": "running",
                "returncode": None,
                "error": None,
            }

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                self._active_proc = proc

                async def stream_output(stream: asyncio.StreamReader, is_err: bool = False) -> None:
                    while True:
                        line = await stream.readline()
                        if not line:
                            break
                        decoded = line.decode("utf-8", errors="replace").rstrip()
                        if decoded:
                            if is_err:
                                logger.warning("[%s] %s", stage, decoded)
                            else:
                                logger.info("[%s] %s", stage, decoded)

                stdout_task = (
                    asyncio.create_task(stream_output(proc.stdout, is_err=False))
                    if proc.stdout
                    else None
                )
                stderr_task = (
                    asyncio.create_task(stream_output(proc.stderr, is_err=True))
                    if proc.stderr
                    else None
                )

                try:
                    await asyncio.wait_for(proc.wait(), timeout=timeout_sec)
                except asyncio.TimeoutError:
                    logger.error(
                        "Stage '%s' timed out after %d seconds; killing process (PID %s).",
                        stage,
                        timeout_sec,
                        proc.pid,
                    )
                    proc.terminate()
                    await asyncio.sleep(3)
                    if proc.returncode is None:
                        proc.kill()
                    run_record["status"] = "timeout"
                    run_record["error"] = f"Timed out after {timeout_min} minutes"
                    return False
                finally:
                    if stdout_task:
                        await stdout_task
                    if stderr_task:
                        await stderr_task

                returncode = proc.returncode or 0
                run_record["returncode"] = returncode
                run_record["status"] = "ok" if returncode == 0 else "error"
                if returncode != 0:
                    run_record["error"] = f"Process exited with code {returncode}"
                    logger.error("Stage '%s' exited with code %d", stage, returncode)
                else:
                    logger.info("Stage '%s' completed successfully.", stage)

                # Event-driven chaining
                chaining = config.get("chaining", {})
                if (
                    returncode == 0
                    and stage == "search"
                    and chaining.get("search_triggers_score", True)
                ):
                    logger.info(
                        "Search succeeded; triggering immediate Score (pipeline chaining)..."
                    )
                    task = asyncio.create_task(self._run_stage("score", manual=False))
                    self._bg_tasks.add(task)
                    task.add_done_callback(self._bg_tasks.discard)

                return returncode == 0
            except Exception as e:  # noqa: BLE001
                run_record["status"] = "error"
                run_record["error"] = str(e)
                logger.error("Failed to execute stage '%s': %s", stage, e)
                return False
            finally:
                finished_at = _now()
                run_record["finished_at"] = _iso(finished_at)
                self._last_runs[stage] = run_record
                self._history.append(dict(run_record))
                self._active_stage = None
                self._active_proc = None
                self._active_started_at = None


scheduler = PipelineScheduler()
