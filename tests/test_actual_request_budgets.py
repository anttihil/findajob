"""Request estimates must never truncate or exclude discovery at execution time."""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from findajob.search import runner
from findajob.search.scheduler import CellState
from findajob.search.traffic import SourceTraffic


def run_with_costs(
    monkeypatch: pytest.MonkeyPatch, costs: dict[str, list[int]], limits: dict[str, int]
) -> tuple[dict[str, Any], list[tuple[str, int]], dict[str, SourceTraffic]]:
    now = datetime.now(timezone.utc).isoformat()
    cells = {
        source: [
            CellState(i, source, "fi", "Engineer", last_success_at=now)
            for i in range(1, len(source_costs) + 1)
        ]
        for source, source_costs in costs.items()
    }
    config = {
        "scraper": {
            "sources": dict.fromkeys(costs, True),
            "proxies": {"enabled": False},
            "policies": {source: {"requests_per_proxy": limit} for source, limit in limits.items()},
        }
    }
    db = MagicMock()
    db.get_cells.side_effect = lambda source: cells[source]
    visited: list[tuple[str, int]] = []
    budgets: dict[str, SourceTraffic] = {}

    def scrape(*args: Any, traffic: SourceTraffic) -> str:
        task = args[3]
        visited.append((task.source, task.cell_id))
        budgets[task.source] = traffic
        traffic.counts[None] += costs[task.source][task.cell_id - 1]
        return "ok"

    monkeypatch.setattr(runner, "load_config", lambda: config)
    monkeypatch.setattr(runner, "load_targets", lambda: None)
    monkeypatch.setattr(runner, "Database", lambda: db)
    monkeypatch.setattr(runner, "_scrape_one", scrape)
    monkeypatch.setattr(runner, "_report_coverage", lambda *_: None)
    totals = runner.run_sync(dry_run=True)
    assert totals is not None
    return totals, visited, budgets


def test_quiet_queries_use_actual_budget_despite_large_estimates(monkeypatch: pytest.MonkeyPatch):
    totals, visited, budgets = run_with_costs(monkeypatch, {"indeed": [1, 1, 1]}, {"indeed": 3})
    assert visited == [("indeed", 1), ("indeed", 2), ("indeed", 3)]
    assert budgets["indeed"].spent == 3
    assert totals["cells_succeeded"] == 3
    assert totals["cells_skipped"] == 0


def test_discovery_finishes_current_cell_after_overrun_then_defers_next(
    monkeypatch: pytest.MonkeyPatch,
):
    totals, visited, budgets = run_with_costs(monkeypatch, {"indeed": [2, 5, 1]}, {"indeed": 3})
    assert visited == [("indeed", 1), ("indeed", 2)]
    assert budgets["indeed"].spent == 7
    assert totals["cells_succeeded"] == 2
    assert totals["cells_skipped"] == 1


def test_sources_have_independent_actual_budgets(monkeypatch: pytest.MonkeyPatch):
    totals, visited, budgets = run_with_costs(
        monkeypatch, {"indeed": [2, 1], "linkedin": [1, 1]}, {"indeed": 1, "linkedin": 2}
    )
    assert visited == [("indeed", 1), ("linkedin", 1), ("linkedin", 2)]
    assert budgets["indeed"].spent == budgets["linkedin"].spent == 2
    assert totals["cells_skipped"] == 1


def test_zero_budget_starts_no_cell(monkeypatch: pytest.MonkeyPatch):
    totals, visited, _ = run_with_costs(monkeypatch, {"indeed": [1]}, {"indeed": 0})
    assert visited == []
    assert totals["cells_skipped"] == 1
