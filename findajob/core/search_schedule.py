"""Resolve search schedules once for execution, diagnostics, and capacity estimates."""

from dataclasses import dataclass
from typing import Any

DEFAULT_INTERVALS = {"indeed": 180, "linkedin": 360}


@dataclass(frozen=True)
class SearchSchedule:
    interval_minutes: float | None
    times: tuple[str, ...]
    jitter_minutes: float
    timeout_minutes: float
    persistent_catchup: bool

    @property
    def runs_per_day(self) -> float:
        return 1440 / self.interval_minutes if self.interval_minutes else float(len(self.times))

    @property
    def max_gap_hours(self) -> float:
        if self.interval_minutes:
            return self.interval_minutes / 60
        minutes = sorted(int(t[:2]) * 60 + int(t[3:]) for t in self.times)
        return (
            max(b - a for a, b in zip(minutes, [*minutes[1:], minutes[0] + 1440], strict=True)) / 60
        )


def search_schedules(config: dict[str, Any]) -> dict[str, SearchSchedule]:
    """Per-source overrides beat the legacy shared daily schedule, then defaults.

    Defaults live here rather than in the merged YAML tree so an existing shared
    schedule is not accidentally shadowed by newly introduced source defaults.
    """
    search = config.get("scheduler", {}).get("search", {})
    enabled = config.get("scraper", {}).get("sources", dict.fromkeys(DEFAULT_INTERVALS, True))
    schedules = {}
    for source, on in enabled.items():
        if not on or source not in DEFAULT_INTERVALS:
            continue
        override = (search.get("sources") or {}).get(source, {})
        interval = override.get("interval_minutes")
        times = override.get("schedule")
        if interval is None and times is None:
            times = search.get("schedule")
            if times is None:
                interval = DEFAULT_INTERVALS[source]
        if interval is not None and times is not None:
            raise ValueError(f"{source}: choose interval_minutes or schedule, not both")
        normalized_times = ()
        if interval is not None:
            interval = float(interval)
            if not 1 <= interval <= 525600:
                raise ValueError(f"{source}: interval_minutes must be between 1 and 525600")
        else:
            normalized = set()
            for value in times or []:
                parts = str(value).split(":")
                if len(parts) != 2 or not (0 <= int(parts[0]) < 24 and 0 <= int(parts[1]) < 60):
                    raise ValueError(f"{source}: invalid daily search time {value!r}")
                normalized.add(f"{int(parts[0]):02d}:{int(parts[1]):02d}")
            normalized_times = tuple(sorted(normalized))
            if not normalized_times:
                raise ValueError(f"{source}: daily search schedule cannot be empty")
        jitter = float(override.get("jitter_minutes", search.get("jitter_minutes", 15)))
        timeout = float(override.get("timeout_minutes", search.get("timeout_minutes", 90)))
        if not 0 <= jitter < 1440 or not 0 < timeout <= 10080:
            raise ValueError(f"{source}: invalid jitter or timeout")
        schedules[source] = SearchSchedule(
            interval,
            normalized_times,
            jitter,
            timeout,
            bool(override.get("persistent_catchup", search.get("persistent_catchup", True))),
        )
    return schedules
