"""Exercise the pinned JobSpy pagination loops with local synthetic responses."""

from typing import Any
from unittest.mock import Mock

import pytest
from jobspy.indeed import Indeed
from jobspy.linkedin import LinkedIn
from jobspy.model import JobPost, ScraperInput, Site


def job(identifier: int) -> JobPost:
    return JobPost(
        id=str(identifier),
        title="Engineer",
        company_name="Acme",
        location=None,
        job_url=f"https://example.com/jobs/{identifier}",
    )


@pytest.mark.parametrize("available,expected_calls", [(0, 1), (130, 3), (600, 5)])
def test_indeed_reaches_five_hundred_or_stops_on_empty_page(
    available: int, expected_calls: int, monkeypatch: pytest.MonkeyPatch
):
    scraper = Indeed()
    requests: list[str] = []

    def post(_url: str, **kwargs: Any):
        query = kwargs["json"]["query"]
        requests.append(query)
        start = (len(requests) - 1) * 100
        response = Mock(ok=True)
        response.json.return_value = {
            "data": {
                "jobSearch": {
                    "results": [
                        {"job": {"key": i}} for i in range(start, min(start + 100, available))
                    ],
                    "pageInfo": {"nextCursor": str(start + 100)},
                }
            }
        }
        return response

    def process(raw: dict[str, Any]):
        scraper.seen_urls.add(str(raw["key"]))
        return job(raw["key"])

    monkeypatch.setattr(scraper.session, "post", post)
    monkeypatch.setattr(scraper, "_process_job", process)
    result = scraper.scrape(ScraperInput(site_type=[Site.INDEED], results_wanted=500, hours_old=72))
    assert len(result.jobs) == min(available, 500)
    assert len(requests) == expected_calls
    assert all("72h" in query for query in requests)
    if len(requests) > 1:
        assert 'cursor: "100"' in requests[1]


@pytest.mark.parametrize("available,expected_calls", [(0, 1), (130, 7), (600, 20)])
def test_linkedin_reaches_five_hundred_or_stops_on_empty_page(
    available: int, expected_calls: int, monkeypatch: pytest.MonkeyPatch
):
    scraper = LinkedIn()
    starts: list[int] = []

    def get(_url: str, **kwargs: Any):
        params = kwargs["params"]
        start = params["start"]
        starts.append(start)
        assert params["f_TPR"] == "r259200"
        html = "".join(
            f'<div class="base-search-card"><a class="base-card__full-link" '
            f'href="https://www.linkedin.com/jobs/view/engineer-{i}"></a></div>'
            for i in range(start, min(start + 25, available))
        )
        return Mock(status_code=200, text=html)

    def process(_card: Any, identifier: str, full: bool):
        assert full is False
        return job(int(identifier))

    monkeypatch.setattr(scraper.session, "get", get)
    monkeypatch.setattr(scraper, "_process_job", process)
    monkeypatch.setattr("jobspy.linkedin.time.sleep", lambda _: None)
    result = scraper.scrape(
        ScraperInput(
            site_type=[Site.LINKEDIN],
            results_wanted=500,
            hours_old=72,
            linkedin_fetch_description=False,
        )
    )
    assert len(result.jobs) == min(available, 500)
    assert len(starts) == expected_calls
    assert starts == [min(i * 25, available) for i in range(expected_calls)]
