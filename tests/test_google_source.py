"""Exercise pinned Google parsers against synthetic HTTP through the real runner."""

import json
from typing import Any
from unittest.mock import Mock

import pytest

from findajob.core.database import Database
from findajob.search.guard import SourceCircuit
from findajob.search.runner import run_cell
from findajob.search.scheduler import ScrapeTask
from findajob.search.sources.jobspy_source import JobSpySource, build_scrape_kwargs
from findajob.search.traffic import SourceTraffic

DESCRIPTION = "Build software using Python and TypeScript. " * 20


def record(identifier: int, optional: bool = True) -> list[Any]:
    info: list[Any] = [None] * 29
    info[0:4] = [
        f"Engineer {identifier}",
        "Acme" if optional else None,
        "Helsinki" if optional else None,
        [[f"https://example.test/apply/{identifier}"]],
    ]
    info[12] = "2 days ago" if optional else "Recently"
    info[19] = DESCRIPTION if optional else None
    info[28] = str(identifier)
    return [*info, []]


def response_text(records: list[list[Any]], cursor: str | None, initial: bool) -> str:
    suffix = f'<div jsname="Yust4d" data-async-fc="{cursor}"></div>' if cursor else ""
    if initial:
        return (
            "".join('520084652":' + json.dumps(info) + "}]]]]]" for info in records) + suffix
            if records
            else "No jobs found"
        )
    payload = [["job", json.dumps([[[{"520084652": info}]]])] for info in records]
    return json.dumps([payload or [["metadata", ""]]]) + suffix


class Board:
    def __init__(self, pages: list[tuple[list[list[Any]], str | None] | str | Exception]):
        self.pages = iter(pages)
        self.requests: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        assert method == "GET" and "google.com/" in url
        assert kwargs["timeout"] == 10
        self.requests.append(kwargs)
        page = next(self.pages)
        if isinstance(page, Exception):
            raise page
        text = (
            page if isinstance(page, str) else response_text(*page, initial=len(self.requests) == 1)
        )
        return Mock(status_code=200, text=text)


def task(**kwargs: Any) -> Any:
    payload: dict[str, Any] = dict(
        ScrapeTask(
            0,
            "google",
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
        ).to_dict()
    )
    payload["cell_id"] = None
    payload.update(kwargs)
    return payload


def execute(
    db: Database, board: Board, limit: int = 100, **kwargs: Any
) -> tuple[Any, SourceTraffic]:
    config = {"policies": {"google": {"requests_per_proxy": limit, "request_interval_seconds": 0}}}
    guard = SourceCircuit("google", config)
    traffic = SourceTraffic("google", config, [], guard)
    traffic.sessions[None] = board
    result = run_cell(
        db,
        JobSpySource(),
        guard,
        task(**kwargs),
        traffic,
        db.start_sync_run("explicit_query"),
        config,
    )
    assert traffic.spent == len(board.requests) == result["requests_made"]
    return result, traffic


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


@pytest.mark.parametrize(
    "hours,phrase",
    [
        (24, "since yesterday"),
        (72, "in the last 3 days"),
        (168, "in the last week"),
        (720, "in the last month"),
        (0, ""),
    ],
)
def test_query_construction_and_included_descriptions(db: Database, hours: int, phrase: str):
    board = Board([([record(1)], "next"), ([record(2)], None)])
    result, _ = execute(db, board, hours_old=hours)
    assert result["status"] == "ok" and result["postings_fetched"] == 2
    query = board.requests[0]["params"]["q"]
    assert query == f"Engineer jobs near Helsinki{' ' + phrase if phrase else ''}"
    assert board.requests[0]["params"]["udm"] == "8"
    assert board.requests[1]["params"]["fc"] == ["next"]
    row = db.conn.execute("SELECT * FROM jobs ORDER BY id").fetchone()
    assert row["site_job_id"] == "go-1" and row["source"] == "google"
    assert row["url"] == "https://example.test/apply/1"
    assert row["description"] == DESCRIPTION.strip() and row["description_quality"] == "full"
    assert row["date_posted"] is not None


@pytest.mark.parametrize(
    "location_id,scope", [("us_remote", " near United States"), ("global_remote", "")]
)
def test_remote_scope(db: Database, location_id: str, scope: str):
    board = Board([([record(1)], None)])
    payload = task(location_id=location_id, location_label="Remote", is_remote=True)
    assert "distance" not in build_scrape_kwargs(payload)
    execute(db, board, **payload)
    assert board.requests[0]["params"]["q"] == f"Engineer jobs{scope} in the last 3 days remote"


def test_duplicate_only_page_continues_and_raw_counts_survive(db: Database):
    board = Board([([record(1)], "a"), ([record(1)], "b"), ([record(2)], None)])
    result, _ = execute(db, board)
    assert result["postings_fetched"] == 3
    assert result["postings_new"] == 2
    assert result["stop_reason"] == "exhausted"


def test_repeated_cursor_stops(db: Database):
    result, _ = execute(db, Board([([record(1)], "a"), ([record(2)], "a")]))
    assert result["stop_reason"] == "pagination_limit"
    assert result["postings_fetched"] == 2


@pytest.mark.parametrize("initial", [True, False])
def test_exhaustion(db: Database, initial: bool):
    pages: list[Any] = [] if initial else [([record(1)], "a")]
    result, _ = execute(db, Board([*pages, ([], None)]))
    assert result["stop_reason"] == "exhausted"
    assert result["status"] == ("empty" if initial else "ok")


def test_optional_fields_do_not_invent_a_date(db: Database):
    result, _ = execute(db, Board([([record(1, optional=False)], None)]))
    assert result["status"] == "ok"
    row = db.conn.execute("SELECT * FROM jobs").fetchone()
    assert row["date_posted"] is None
    assert not row["description"]
    assert not row["company"]


@pytest.mark.parametrize(
    "bad",
    [
        "<html>Unknown layout</html>",
        "<html>unusual traffic recaptcha</html>",
        '520084652":broken',
        RuntimeError("HTTP 429"),
        RuntimeError("Read timeout"),
    ],
)
@pytest.mark.parametrize("after_saved", [True, False])
def test_failures_propagate_and_preserve_saved_pages(db: Database, bad: Any, after_saved: bool):
    pages = [([record(1)], "a"), bad] if after_saved else [bad]
    result, _ = execute(db, Board(pages))
    assert result["status"] == "error"
    assert db.conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == int(after_saved)


def test_budget_and_result_ceilings(db: Database):
    result, _ = execute(db, Board([([record(1)], "a")]), limit=1)
    assert result["status"] == "budget_limited"
    # Raw duplicates count toward the ceiling, without fetching another page.
    result, _ = execute(db, Board([([record(1), record(1), record(2)], "a")]), results_wanted=3)
    assert result["stop_reason"] == "result_limit"
    assert result["postings_fetched"] == 3
    result, _ = execute(db, Board([]), limit=0)
    assert result["status"] == "budget_limited"


def test_cross_provider_duplicate_keeps_google_provenance(db: Database):
    from findajob.search.normalizer import normalize_row
    from findajob.search.runner import store_postings

    posting = normalize_row(
        {
            "title": "Engineer 1",
            "company": "Acme",
            "location": "Helsinki",
            "job_url": "https://indeed.test/1",
            "id": "in-1",
            "site": "indeed",
        },
        task(),
    )
    ids, _, _ = store_postings(db, [posting], None)
    result, _ = execute(db, Board([([record(1)], None)]))
    assert result["duplicates_merged"] == 1
    row = db.conn.execute("SELECT * FROM jobs WHERE source='google'").fetchone()
    assert row["duplicate_of"] == ids[0]
    assert row["site_job_id"] == "go-1"


def test_five_hundred_result_ceiling_uses_fifty_pages(db: Database):
    board = Board(
        [
            ([record(i) for i in range(start, start + 10)], str(start + 10))
            for start in range(0, 510, 10)
        ]
    )
    result, traffic = execute(db, board)
    assert result["stop_reason"] == "result_limit"
    assert result["postings_fetched"] == 500
    assert traffic.spent == 50
    assert db.conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 500


def test_missing_url_counts_raw_record_and_missing_id_uses_url(db: Database):
    no_url, no_id = record(1), record(2)
    no_url[3] = None
    no_id[28] = None
    result, _ = execute(db, Board([([no_url, no_id], None)]))
    assert result["postings_fetched"] == 2 and result["postings_new"] == 1
    row = db.conn.execute("SELECT * FROM jobs").fetchone()
    assert row["site_job_id"] is None
    assert row["job_key"] == "https://example.test/apply/2"


def test_failed_http_attempt_is_counted_before_proxy_failover(db: Database):
    config = {"policies": {"google": {"request_interval_seconds": 0}}}
    guard = SourceCircuit("google", config)
    traffic = SourceTraffic("google", config, ["a:1", "b:1"], guard)
    failed = Mock()
    failure = Mock(status_code=429)
    failure.raise_for_status.side_effect = RuntimeError("HTTP 429")
    failed.request.return_value = failure
    board = Board([([record(1)], None)])
    traffic.sessions[traffic.endpoints[0]] = failed
    traffic.sessions[traffic.endpoints[1]] = board
    result = run_cell(
        db, JobSpySource(), guard, task(), traffic, db.start_sync_run("incremental"), config
    )
    assert result["status"] == "ok"
    assert result["requests_made"] == traffic.spent == 2
    assert failed.request.call_count == len(board.requests) == 1


def test_google_scheduler_resolves_explicit_scope(db: Database):
    from findajob.core.config import DEFAULT_CONFIG
    from findajob.search.scheduler import scrape_tasks
    from findajob.search.targets import add_query, load_targets, save_location

    save_location(db.conn, "fi", "Helsinki", country="FI", indeed_country="finland")
    add_query(db.conn, "Legacy engineer")
    add_query(db.conn, "Google engineer", sources=["google"])
    db.seed_cells(load_targets(conn=db.conn).cell_specs())
    tasks = scrape_tasks(db, DEFAULT_CONFIG, "google")
    assert len(tasks) == 1
    assert tasks[0].query == "Google engineer" and tasks[0].results_wanted == 500
