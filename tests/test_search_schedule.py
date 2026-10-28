from typing import Any

import pytest

from findajob.core.search_schedule import search_schedules


def test_defaults_and_source_overrides():
    defaults = search_schedules({})
    assert defaults["indeed"].interval_minutes == 180
    assert defaults["linkedin"].interval_minutes == 360
    cfg = {
        "scheduler": {
            "search": {
                "schedule": ["19:00", "01:00", "01:00"],
                "sources": {"indeed": {"interval_minutes": 120}},
            }
        }
    }
    schedules = search_schedules(cfg)
    assert schedules["indeed"].runs_per_day == 12
    assert schedules["linkedin"].times == ("01:00", "19:00")
    assert schedules["linkedin"].max_gap_hours == 18


@pytest.mark.parametrize(
    "settings",
    [
        {"interval_minutes": 0},
        {"interval_minutes": -1},
        {"interval_minutes": float("nan")},
        {"schedule": []},
        {"schedule": ["25:00"]},
        {"schedule": ["01:00"], "interval_minutes": 60},
        {"jitter_minutes": -1},
    ],
)
def test_invalid_schedules_fail_explicitly(settings: dict[str, Any]):
    with pytest.raises(ValueError):
        search_schedules({"scheduler": {"search": {"sources": {"indeed": settings}}}})


def test_disabled_sources_are_excluded():
    assert list(
        search_schedules({"scraper": {"sources": {"indeed": False, "linkedin": True}}})
    ) == ["linkedin"]
