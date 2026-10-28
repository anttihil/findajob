"""Real page parsers with synthetic HTTP: budgets stop pages, never lose discoveries."""

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import Mock

import pytest

from findajob.core.database import Database
from findajob.search.guard import SourceCircuit
from findajob.search.runner import run_cell
from findajob.search.scheduler import ScrapeTask
from findajob.search.sources.jobspy_source import JobSpySource
from findajob.search.traffic import SourceTraffic

DESCRIPTION = "Build software using Python and TypeScript. " * 20


class Board:
    def __init__(self, available: int = 600, fail_page: int = 0, detail_status: int = 200):
        self.available, self.fail_page, self.detail_status = available, fail_page, detail_status
        self.pages = 0
        self.details = 0
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def request(self, _method: str, url: str, **kwargs: Any) -> Any:
        self.requests.append((url, kwargs))
        response = Mock(status_code=200)
        if "jobPosting/" in url:
            self.details += 1
            response.status_code = self.detail_status
            response.text = f'<div class="show-more-less-html__markup">{DESCRIPTION}</div>'
            if self.detail_status != 200:
                response.raise_for_status.side_effect = RuntimeError(f"HTTP {self.detail_status}")
            return response
        self.pages += 1
        if self.pages == self.fail_page:
            raise RuntimeError("Read timeout")
        if "graphql" in url:
            start = (self.pages - 1) * 100
            jobs = [
                {
                    "job": {
                        "key": str(i),
                        "title": f"Engineer {i}",
                        "description": {"html": DESCRIPTION},
                        "attributes": [],
                        "datePublished": 1793188800000,
                        "employer": None,
                        "location": {
                            "city": "Helsinki",
                            "countryCode": "FI",
                            "formatted": {"long": "Helsinki"},
                        },
                        "compensation": {"baseSalary": None, "estimated": None},
                        "recruit": {},
                    }
                }
                for i in range(start + 1, min(start + 100, self.available) + 1)
            ]
            response.json.return_value = {
                "data": {
                    "jobSearch": {
                        "results": jobs,
                        "pageInfo": {"nextCursor": str(start + 100) if jobs else None},
                    }
                }
            }
        else:
            start = kwargs["params"]["start"]
            response.text = "".join(
                f'<div class="base-search-card"><a class="base-card__full-link" '
                f'href="https://www.linkedin.com/jobs/view/engineer-{i}"></a>'
                f'<span class="sr-only">Engineer {i}</span><h4 class="base-search-card__subtitle">'
                '<a>Acme</a></h4><div class="base-search-card__metadata">Helsinki</div></div>'
                for i in range(start + 1, min(start + 25, self.available) + 1)
            )
        return response


@pytest.fixture
def db():
    database = Database(":memory:")
    database.conn.execute(
        "INSERT INTO scrape_cells (id,source,location_id,query,search_label,country,created_at) "
        "VALUES (1,'linkedin','fi','Engineer','Helsinki','FI',?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    database.conn.commit()
    yield database
    database.close()


def execute(
    db: Database,
    board: Board,
    limit: int = 100,
    source: str = "linkedin",
    proxies: list[str] | None = None,
) -> tuple[dict[str, Any], SourceTraffic]:
    config = {"policies": {source: {"requests_per_proxy": limit, "request_interval_seconds": 0}}}
    guard = SourceCircuit(source, config)
    traffic = SourceTraffic(source, config, proxies or [], guard)
    for endpoint in traffic.endpoints:
        traffic.sessions[endpoint] = board
    run_id = db.start_sync_run("incremental")
    task = ScrapeTask(
        1,
        source,
        "Engineer",
        "fi",
        "Helsinki",
        "FI",
        "finland",
        False,
        50,
        500,
        72,
        True,
        "census",
    )
    result = run_cell(db, JobSpySource(), guard, task.to_dict(), traffic, run_id, config)
    return result, traffic


def test_cold_page_finishes_details_then_stops_at_budget(db: Database):
    board = Board()
    result, traffic = execute(db, board)
    assert result["status"] == "budget_limited"
    assert result["stop_reason"] == "budget_limit"
    assert result["postings_fetched"] == 100
    assert board.pages == 4 and board.details == 100
    assert traffic.spent == result["requests_made"] == 104
    assert (
        db.conn.execute("SELECT count(*) FROM jobs WHERE description_quality='full'").fetchone()[0]
        == 100
    )
    row = db.conn.execute("SELECT * FROM cell_observations").fetchone()
    assert row["status"] == "budget_limited" and row["saturated"] == 1
    assert row["stop_reason"] == "budget_limit"
    assert json.loads(row["detail_stats"])["fetched"] == 100
    cell = db.conn.execute("SELECT * FROM scrape_cells").fetchone()
    assert cell["last_scraped_at"] is not None
    assert cell["last_success_at"] is None
    assert cell["last_hours_old"] is None
    assert cell["consecutive_error"] == 0
    assert db.conn.execute("SELECT count(*) FROM v_skill_eligible").fetchone()[0] == 0


def test_restart_from_front_uses_cache_to_reach_deeper_without_cursor(db: Database):
    execute(db, Board())
    second = Board()
    result, traffic = execute(db, second)
    assert result["postings_fetched"] == 200
    assert second.pages == 8 and second.details == 100
    assert traffic.spent == 108
    assert result["detail_stats"]["reused"] == 100
    assert db.conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 200
    assert db.conn.execute("SELECT times_seen FROM jobs ORDER BY id LIMIT 1").fetchone()[0] == 2


def test_one_request_allowance_can_only_overrun_by_current_page(db: Database):
    result, traffic = execute(db, Board(), limit=1)
    assert result["postings_fetched"] == 25
    assert result["status"] == "budget_limited"
    assert traffic.spent == 26
    assert "details_pending" not in {r[1] for r in db.conn.execute("PRAGMA table_info(jobs)")}


def test_full_result_limit_stops_at_five_hundred(db: Database):
    board = Board()
    result, traffic = execute(db, board, limit=1000)
    assert result["stop_reason"] == "result_limit"
    assert result["postings_fetched"] == 500
    assert board.pages == 20 and board.details == 500
    assert traffic.spent == 520
    assert db.conn.execute("SELECT count(*) FROM v_skill_eligible").fetchone()[0] == 500


def test_warm_cache_fits_all_five_hundred_under_one_hundred_requests(db: Database):
    execute(db, Board(), limit=1000)
    board = Board()
    result, traffic = execute(db, board)
    assert result["postings_fetched"] == result["detail_stats"]["reused"] == 500
    assert traffic.spent == board.pages == 20
    assert board.details == 0


@pytest.mark.parametrize("source", ["linkedin", "indeed"])
def test_empty_results_stop_without_claiming_budget_exhaustion(db: Database, source: str):
    board = Board(available=0)
    result, traffic = execute(db, board, source=source)
    assert result["status"] == "empty"
    assert result["stop_reason"] == "exhausted"
    assert result["saturated"] is False
    assert traffic.spent == board.pages == 1


def test_indeed_cursor_pages_reach_five_hundred_with_included_details(db: Database):
    board = Board()
    result, traffic = execute(db, board, source="indeed")
    assert result["postings_fetched"] == 500
    assert result["stop_reason"] == "result_limit"
    assert traffic.spent == board.pages == 5
    assert board.details == 0
    assert all("72h" in kwargs["json"]["query"] for _, kwargs in board.requests)
    assert 'cursor: "100"' in board.requests[1][1]["json"]["query"]


def test_error_after_a_saved_page_preserves_jobs_and_records_failure(db: Database):
    board = Board(fail_page=2)
    result, traffic = execute(db, board)
    assert result["status"] == "error" and result["stop_reason"] == "error"
    assert traffic.spent == 27
    assert db.conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 25


def test_rate_limit_interrupts_page_without_more_detail_requests(db: Database):
    board = Board(detail_status=429)
    result, traffic = execute(db, board)
    assert result["status"] == "error"
    assert board.pages == board.details == 1
    assert traffic.spent == 2
    assert db.conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 25


def test_proxy_allowance_scales_and_pages_choose_least_used_endpoint(db: Database):
    result, traffic = execute(db, Board(), proxies=["a:1", "a:1", "b:1", "c:1"])
    assert traffic.limit == 300
    assert traffic.counts == {"a:1": 104, "b:1": 104, "c:1": 104}
    assert result["postings_fetched"] == 300


def test_zero_allowance_makes_no_requests(db: Database):
    board = Board()
    result, traffic = execute(db, board, limit=0)
    assert result["status"] == "budget_limited"
    assert traffic.spent == board.pages == board.details == 0


def test_pacing_and_failed_attempts_use_same_transport():
    now = [0.0]
    sleeps: list[float] = []

    def sleep(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    config = {"policies": {"linkedin": {"request_interval_seconds": 3}}}
    traffic = SourceTraffic(
        "linkedin", config, [], SourceCircuit("linkedin", config), clock=lambda: now[0], sleep=sleep
    )
    session = Mock()
    session.request.side_effect = [RuntimeError("Read timeout"), Mock(status_code=200)]
    traffic.sessions[None] = session
    with pytest.raises(RuntimeError, match="timeout"):
        traffic.request(None, "GET", "https://example.test/page")
    traffic.request(None, "GET", "https://example.test/detail")
    assert traffic.spent == 2
    assert sleeps == [3]
    assert session.request.call_args.kwargs["allow_redirects"] is False
