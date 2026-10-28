"""Discovery and detail reuse must remain independent of storage and request limits."""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock

import pytest

from findajob.core.config import DEFAULT_CONFIG, load_config
from findajob.core.database import Database
from findajob.search.enrichment import enrich_linkedin
from findajob.search.guard import SourceCircuit
from findajob.search.normalizer import normalize_rows
from findajob.search.scheduler import CellState, select_cells
from findajob.search.sources.linkedin_details import (
    LinkedInDetails,
    LinkedInDetailsAdapter,
    parse_details,
)

NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
DESCRIPTION = "Build software with Python and TypeScript. " * 20


class FakeDetails(LinkedInDetailsAdapter):
    def __init__(self, error: Exception | None = None) -> None:
        super().__init__([])
        self.calls: list[str] = []
        self.error = error

    def fetch(self, site_job_id: str) -> LinkedInDetails:
        self.calls.append(site_job_id)
        self.last_fetch = {"requests_made": 1, "duration_ms": 2}
        if self.error:
            raise self.error
        return LinkedInDetails(DESCRIPTION, "Software", "https://example.com/apply")


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


def circuit() -> SourceCircuit:
    return SourceCircuit("linkedin", {}, now=lambda: NOW)


def listings(count: int = 1) -> list[dict[str, Any]]:
    return [
        {
            "id": f"li-{i}",
            "site": "linkedin",
            "title": "Engineer",
            "company": "Acme",
            "location": "Helsinki, Finland",
            "job_url": f"https://www.linkedin.com/jobs/view/{i}",
        }
        for i in range(1, count + 1)
    ]


def postings(count: int = 1) -> list[dict[str, Any]]:
    return normalize_rows(listings(count), {"source": "linkedin"}, NOW)[0]


def cache(db: Database, posting: dict[str, Any], age: float | None = 1) -> None:
    posting.update(
        description=DESCRIPTION,
        description_quality="full",
        company_industry="Software",
        url_direct="https://example.com/apply",
        details_fetched_at=(NOW - timedelta(hours=age)).isoformat() if age is not None else None,
    )
    db.upsert_posting(posting)


def test_five_hundred_listings_only_fetch_fifty_uncached_details(db: Database):
    for posting in postings(450):
        cache(db, posting)
    found = postings(500)
    adapter = FakeDetails()
    counts, cost = enrich_linkedin(db, found, adapter, circuit(), {}, fetch=True, now=NOW)
    assert len(found) == 500
    assert counts == {
        "fetched": 50,
        "reused": 450,
        "failed": 0,
        "missing": 0,
        "skipped": 0,
        "stale": 0,
    }
    assert cost["requests_made"] == 50
    assert all(posting["description"] == DESCRIPTION for posting in found)
    assert all(posting["desc_selection"] == "complete" for posting in found)


@pytest.mark.parametrize("age", [169, None])
def test_stale_and_legacy_details_are_refreshed(db: Database, age: float | None):
    cache(db, postings()[0], age)
    found = postings()
    adapter = FakeDetails()
    counts, _ = enrich_linkedin(db, found, adapter, circuit(), {}, fetch=True, now=NOW)
    assert counts["fetched"] == 1
    assert found[0]["details_fetched_at"] == NOW.isoformat()


def test_listing_change_refreshes_details_but_fresh_unchanged_job_is_reused(db: Database):
    cache(db, postings()[0])
    found = postings()
    found[0]["title"] = "Senior Engineer"
    adapter = FakeDetails()
    counts, _ = enrich_linkedin(db, found, adapter, circuit(), {}, fetch=True, now=NOW)
    assert counts["fetched"] == 1
    assert found[0]["title"] == "Senior Engineer"


def test_failed_refresh_has_a_cooldown_across_queries(db: Database):
    found = postings()
    first = FakeDetails(RuntimeError("Read timeout"))
    enrich_linkedin(db, found, first, circuit(), {}, fetch=True, now=NOW)
    db.upsert_posting(found[0])
    retry = FakeDetails()
    counts, _ = enrich_linkedin(
        db,
        postings(),
        retry,
        circuit(),
        {},
        fetch=True,
        now=NOW + timedelta(minutes=30),
    )
    assert counts["skipped"] == 1
    assert retry.calls == []
    enrich_linkedin(
        db,
        postings(),
        retry,
        circuit(),
        {},
        fetch=True,
        now=NOW + timedelta(hours=2),
    )
    assert retry.calls == ["li-1"]


def test_rate_limit_stops_detail_batch_and_retains_remaining_discoveries(db: Database):
    adapter = FakeDetails(RuntimeError("HTTP 429"))
    found = postings(3)
    guard = circuit()
    counts, _ = enrich_linkedin(db, found, adapter, guard, {}, fetch=True, now=NOW)
    assert adapter.calls == ["li-1"]
    assert guard.is_open
    assert counts["failed"] == 1
    assert counts["skipped"] == 2
    assert counts["missing"] == 3


def test_incomplete_cohorts_are_not_a_selected_subset_for_skill_statistics(db: Database):
    cache(db, postings()[0])
    found = postings(2)
    enrich_linkedin(
        db, found, FakeDetails(RuntimeError("Read timeout")), circuit(), {}, fetch=True, now=NOW
    )
    run = db.start_sync_run("incremental")
    for posting in found:
        db.upsert_posting(posting, run_id=run)
    assert db.conn.execute("SELECT count(*) FROM v_skill_eligible").fetchone()[0] == 0
    complete = postings(2)
    enrich_linkedin(
        db, complete, FakeDetails(), circuit(), {}, fetch=True, now=NOW + timedelta(hours=2)
    )
    for posting in complete:
        db.upsert_posting(posting, run_id=run)
    assert db.conn.execute("SELECT count(*) FROM v_skill_eligible").fetchone()[0] == 2


def test_refresh_failure_preserves_cached_content_and_timestamp(db: Database):
    cache(db, postings()[0], 200)
    found = postings()
    counts, cost = enrich_linkedin(
        db,
        found,
        FakeDetails(RuntimeError("HTTP 429")),
        circuit(),
        {},
        fetch=True,
        now=NOW,
    )
    assert counts["failed"] == counts["reused"] == counts["stale"] == 1
    assert cost["requests_made"] == 1
    assert found[0]["description"] == DESCRIPTION
    assert found[0]["url_direct"] == "https://example.com/apply"
    assert found[0]["details_fetched_at"] is not None
    assert "429" in found[0]["details_error"]


def test_changed_listing_stays_invalidated_after_failed_refresh(db: Database):
    cache(db, postings()[0], 1)
    changed = postings()
    changed[0]["title"] = "Senior Engineer"
    enrich_linkedin(
        db, changed, FakeDetails(RuntimeError("Read timeout")), circuit(), {}, fetch=True, now=NOW
    )
    db.upsert_posting(changed[0])

    rediscovered = postings()
    adapter = FakeDetails()
    counts, _ = enrich_linkedin(
        db, rediscovered, adapter, circuit(), {}, fetch=True, now=NOW + timedelta(hours=2)
    )
    assert counts["fetched"] == 1
    assert adapter.calls == ["li-1"]
    assert rediscovered[0]["details_fetched_at"] == (NOW + timedelta(hours=2)).isoformat()


def test_disabled_fetch_reuses_content_without_overwriting_it(db: Database):
    cache(db, postings()[0], 200)
    found = postings()
    adapter = FakeDetails()
    counts, _ = enrich_linkedin(db, found, adapter, circuit(), {}, fetch=False, now=NOW)
    assert adapter.calls == []
    assert counts["reused"] == 1
    assert found[0]["description"] == DESCRIPTION
    assert found[0]["company_industry"] == "Software"


def test_detail_parser_preserves_used_fields_and_rejects_empty_content():
    html = f"""<div class="show-more-less-html__markup"><p>{DESCRIPTION}</p></div>
        <h3 class="description__job-criteria-subheader">Industries</h3><span>Software</span>
        <code id="applyUrl">"https://www.linkedin.com/redir?url=https%3A%2F%2Fexample.com%2Fapply"</code>"""
    result = parse_details(html)
    assert "TypeScript" in result.description
    assert result.company_industry == "Software"
    assert result.url_direct == "https://example.com/apply"
    with pytest.raises(ValueError, match="no description"):
        parse_details("<html>Sign in</html>")
    with pytest.raises(ValueError, match="incomplete"):
        parse_details('<div class="show-more-less-html__markup">Short</div>')


def test_adapter_uses_one_guest_request_and_propagates_errors():
    adapter = LinkedInDetailsAdapter([])
    response = Mock(status_code=429)
    response.raise_for_status.side_effect = RuntimeError("HTTP 429")
    adapter._session = Mock()
    adapter._session.get.return_value = response
    with pytest.raises(RuntimeError, match="429"):
        adapter.fetch("li-123")
    adapter._session.get.assert_called_once_with(
        "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/123",
        timeout=10,
        allow_redirects=False,
    )
    assert adapter.last_fetch["requests_made"] == 1


@pytest.mark.parametrize("source", ["indeed", "linkedin"])
@pytest.mark.parametrize("proxies", [[], ["example:1234"]])
def test_default_and_repository_config_reach_fixed_five_hundred(source: str, proxies: list[str]):
    for settings in (DEFAULT_CONFIG["scraper"], load_config()["scraper"]):
        settings = dict(settings, proxies_list=proxies)
        for saturated in (0, 1):
            cells = [CellState(1, source, "fi", "Engineer", last_saturated=saturated)]
            tasks = select_cells(cells, settings, source, NOW)
            assert len(tasks) == 1
            assert tasks[0].results_wanted == 500
        assert settings["hours_old_floor"] == 72
        assert settings["max_hours_old"] == 168


def test_fixed_depth_is_configurable_without_legacy_clamps():
    config = {
        "policies": {
            "linkedin": {
                "results_wanted": 800,
                "results_wanted_default": 50,
                "max_results_wanted": 75,
                "request_units": 1000,
                "max_pages_per_run": 1000,
            }
        }
    }
    tasks = select_cells([CellState(1, "linkedin", "fi", "Engineer")], config, "linkedin", NOW)
    assert tasks[0].results_wanted == 800
