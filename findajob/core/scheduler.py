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
from findajob.core.search_schedule import search_schedules

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
        self._active_source: str | None = None
        self._active_stage: str | None = None
        self._active_proc: asyncio.subprocess.Process | None = None
        self._active_started_at: datetime | None = None
        self._stage_locks: dict[str, asyncio.Lock] = {
            "search": asyncio.Lock(),
            "score": asyncio.Lock(),
        }
        self._execution_lock = asyncio.Lock()
        self._executions: set[asyncio.Task[Any]] = set()
        self._pending: set[str] = set()
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
            "active_source": self._active_source,
            "pending": sorted(self._pending),
            "active_started_at": _iso(self._active_started_at),
            "next_runs": {k: _iso(v) for k, v in self._next_runs.items()},
            "last_runs": self._last_runs,
            "history": list(self._history),
        }

    async def start(self) -> None:
        """Starts the scheduler engine and schedules all configured stages."""
        if self._running:
            return

        full_config = load_config()
        config = full_config.get("scheduler", {})
        schedules = search_schedules(full_config)
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

        self._tasks.append(asyncio.create_task(self._boot_catchup_check()))
        for source in schedules:
            self._tasks.append(asyncio.create_task(self._schedule_loop_search(source)))

        self._tasks.append(asyncio.create_task(self._schedule_loop_interval("score")))

    async def stop(self) -> None:
        """Stops the scheduler and terminates any active worker subprocess."""
        self._enabled = False
        if not self._running and not self._executions:
            return

        self._running = False
        logger.info("Stopping Find a Job scheduler...")

        tasks = set(self._tasks) | self._bg_tasks | self._executions
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._bg_tasks.clear()
        self._next_runs.clear()
        self._pending.clear()
        self._active_stage = None
        self._active_proc = None
        self._active_started_at = None

    async def trigger(self, stage: str, manual: bool = True) -> bool:
        """Triggers a stage run asynchronously."""
        if stage not in {"search", "score"}:
            raise ValueError(f"Unknown pipeline stage: {stage}")

        return await self._run_stage(stage, manual=manual)

    async def _boot_catchup_check(self) -> None:
        """A recent visit to one board must not hide a missed visit to another."""
        await asyncio.sleep(5)
        if not self._running:
            return
        try:
            schedules = search_schedules(load_config())
            with sqlite3.connect(DB_PATH) as conn:
                latest = dict(
                    conn.execute(
                        "SELECT o.source, MAX(r.finished_at) FROM sync_runs r "
                        "JOIN cell_observations o ON o.sync_run_id = r.id "
                        "WHERE r.status IN ('ok', 'partial') "
                        "AND o.status IN ('ok', 'empty', 'budget_limited') "
                        "GROUP BY o.source"
                    )
                )
            due = []
            for source, schedule in schedules.items():
                if not schedule.persistent_catchup:
                    continue
                stamp = latest.get(source)
                last = datetime.fromisoformat(stamp) if stamp else None
                if last and last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                gap = schedule.max_gap_hours + 2 * schedule.jitter_minutes / 60
                if last is None or (_now() - last).total_seconds() >= gap * 3600:
                    due.append(self._run_stage("search", manual=False, source=source))
            await asyncio.gather(*due)
        except (sqlite3.Error, OSError, ValueError) as e:
            logger.error("Error during boot catch-up check: %s", e)

    def _next_daily_time(
        self, time_strs: list[str], jitter_minutes: float, after: datetime | None = None
    ) -> datetime:
        """Calculates next upcoming time from a list of 'HH:MM' strings with jitter."""
        now = max(_now(), after) if after else _now()
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

    async def _schedule_loop_search(self, source: str) -> None:
        """Keep independent due times while all workers share the execution queue."""
        key = f"search:{source}"
        next_run: datetime | None = None
        nominal: datetime | None = None
        while self._running:
            try:
                schedule = search_schedules(load_config()).get(source)
                if schedule is None:
                    self._next_runs.pop(key, None)
                    return
                if schedule.interval_minutes:
                    jitter = random.uniform(-schedule.jitter_minutes, schedule.jitter_minutes)
                    # Anchor intervals to due time, not the end of a slow worker.
                    next_run = max(
                        (next_run or _now())
                        + timedelta(seconds=max(30, (schedule.interval_minutes + jitter) * 60)),
                        _now() + timedelta(seconds=30),
                    )
                else:
                    nominal = self._next_daily_time(list(schedule.times), 0, after=nominal)
                    next_run = max(
                        _now() + timedelta(seconds=1),
                        nominal
                        + timedelta(
                            minutes=random.uniform(
                                -schedule.jitter_minutes, schedule.jitter_minutes
                            )
                        ),
                    )
                self._next_runs[key] = next_run
                await asyncio.sleep(max(0, (next_run - _now()).total_seconds()))
                if not self._running:
                    break
                self._next_runs.pop(key, None)
                await self._run_stage("search", manual=False, source=source)
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.error("Error in %s schedule loop: %s", key, e)
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

    async def _run_stage(self, stage: str, manual: bool = False, source: str | None = None) -> bool:
        """Deduplicate requests and queue every distinct source behind one worker."""
        if stage not in {"search", "score"}:
            raise ValueError(f"Unknown pipeline stage: {stage}")
        if source is not None and stage != "search":
            raise ValueError("Only search supports a source")
        schedules = search_schedules(load_config())
        if source is not None and source not in schedules:
            return False
        key = f"search:{source}" if source else stage
        covered = (
            {f"search:{name}" for name in schedules} if stage == "search" and not source else {key}
        )
        covered.add(key)
        stage_lock = self._stage_locks.setdefault(key, asyncio.Lock())
        if stage_lock.locked() or self._pending.intersection(covered):
            return False
        task = asyncio.current_task()
        assert task is not None
        self._pending.update(covered)
        self._executions.add(task)
        try:
            async with stage_lock, self._execution_lock:
                return await self._execute_stage(stage, manual, source)
        finally:
            self._pending.difference_update(covered)
            self._executions.discard(task)

    async def _execute_stage(self, stage: str, manual: bool, source: str | None) -> bool:
        """Execute an isolated worker; its timeout begins after queueing."""
        config = load_config().get("scheduler", {})
        stage_config = config.get(stage, {})
        timeout_min = (
            search_schedules(load_config())[source].timeout_minutes
            if source
            else stage_config.get("timeout_minutes", 90)
        )
        timeout_sec = timeout_min * 60

        started_at = _now()
        self._active_stage = stage
        self._active_source = source
        self._active_started_at = started_at

        cmd = [sys.executable, "-m", "findajob.cli", stage, "run"]
        if source:
            cmd.extend(["--source", source])
        logger.info(
            "Executing pipeline stage '%s' (manual=%s, timeout=%dm)...",
            stage,
            manual,
            timeout_min,
        )

        run_record: dict[str, Any] = {
            "stage": stage,
            "source": source,
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
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
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
                logger.info("Search succeeded; triggering immediate Score (pipeline chaining)...")
                task = asyncio.create_task(self._run_stage("score", manual=False))
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)

            return returncode == 0
        except asyncio.CancelledError:
            run_record["status"] = "cancelled"
            raise
        except Exception as e:  # noqa: BLE001
            run_record["status"] = "error"
            run_record["error"] = str(e)
            logger.error("Failed to execute stage '%s': %s", stage, e)
            return False
        finally:
            if self._active_proc and self._active_proc.returncode is None:
                self._active_proc.terminate()
                try:
                    await asyncio.wait_for(self._active_proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    self._active_proc.kill()
                    await self._active_proc.wait()
            finished_at = _now()
            run_record["finished_at"] = _iso(finished_at)
            key = f"search:{source}" if source else stage
            self._last_runs[key] = run_record
            self._history.append(dict(run_record))
            self._active_source = None
            self._active_stage = None
            self._active_proc = None
            self._active_started_at = None


scheduler = PipelineScheduler()
