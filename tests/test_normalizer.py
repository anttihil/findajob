"""Unit tests for findajob.search.normalizer.

Tests all pure data normalizations: company names, locations, salaries, dates,
deduplication hashing, description quality, and batch normalization.
"""

import unittest
from datetime import datetime, timezone

from findajob.search.normalizer import (
    _number,
    _text,
    content_hash,
    description_quality,
    infer_remote,
    is_agency,
    normalize_company,
    normalize_date_posted,
    normalize_row,
    normalize_rows,
    normalize_salary,
    parse_location,
)


class TestNormalizerDataHygiene(unittest.TestCase):
    def test_text_coercion(self) -> None:
        self.assertEqual(_text(None), "")
        self.assertEqual(_text(float("nan")), "")
        self.assertEqual(_text("nan"), "")
        self.assertEqual(_text("None"), "")
        self.assertEqual(_text("null"), "")
        self.assertEqual(_text("<NA>"), "")
        self.assertEqual(_text("  hello world  "), "hello world")
        self.assertEqual(_text(123), "123")

    def test_number_coercion(self) -> None:
        self.assertIsNone(_number(None))
        self.assertIsNone(_number("invalid"))
        self.assertIsNone(_number(float("nan")))
        self.assertIsNone(_number(float("inf")))
        self.assertEqual(_number("123.45"), 123.45)
        self.assertEqual(_number(50), 50.0)


class TestNormalizeCompany(unittest.TestCase):
    def test_strips_legal_suffixes(self) -> None:
        cases = [
            ("Spotify AB", "spotify"),
            ("Google LLC", "google"),
            ("Apple Inc.", "apple"),
            ("Amazon Corp", "amazon"),
            ("Wolt Oy", "wolt"),
            ("Klarna Bank AB", "klarna bank"),
            ("Nordic Retail A/S", "nordic retail"),
            ("Tech Solutions Ltd.", "tech solutions"),
            ("Foo Technologies Inc", "foo technologies"),
        ]
        for raw, expected in cases:
            self.assertEqual(normalize_company(raw), expected, raw)

    def test_preserves_company_name_words(self) -> None:
        # Does not strip "Technologies" or "Systems" or "Group"
        self.assertEqual(normalize_company("Foo Technologies"), "foo technologies")
        self.assertEqual(normalize_company("Bar Systems"), "bar systems")
        self.assertEqual(normalize_company("Acme Group"), "acme group")

    def test_is_agency(self) -> None:
        agencies = ["Robert Half", "Toptal", "Randstad", "Adecco"]
        self.assertTrue(is_agency("Robert Half International Inc", agencies))
        self.assertTrue(is_agency("Toptal LLC", agencies))
        self.assertTrue(is_agency("Randstad AB", agencies))
        self.assertFalse(is_agency("Stripe", agencies))
        self.assertFalse(is_agency("Google Inc", agencies))


class TestParseLocation(unittest.TestCase):
    def test_us_location(self) -> None:
        res = parse_location("Commerce, CA, US", country_hint="US")
        self.assertEqual(res["city"], "Commerce")
        self.assertEqual(res["region"], "CA")
        self.assertEqual(res["country"], "US")
        self.assertEqual(res["location"], "Commerce, CA")

    def test_nordic_location(self) -> None:
        res = parse_location("Stockholm, Sweden", country_hint="SE")
        self.assertEqual(res["city"], "Stockholm")
        self.assertEqual(res["country"], "SE")

    def test_remote_fallback(self) -> None:
        res = parse_location("Remote", country_hint="US", is_remote_query=True)
        self.assertEqual(res["city"], "")
        self.assertEqual(res["location"], "Remote")
        self.assertEqual(res["country"], "US")


class TestInferRemote(unittest.TestCase):
    def test_explicit_board_metadata(self) -> None:
        self.assertEqual(infer_remote({"is_remote": True}), 1)
        self.assertEqual(infer_remote({"is_remote": 1}), 1)
        self.assertEqual(infer_remote({"is_remote": False}), 0)
        self.assertEqual(infer_remote({"is_remote": 0}), 0)

    def test_remote_query_provenance(self) -> None:
        self.assertEqual(infer_remote({}, is_remote_query=True), 1)

    def test_unspecified_leaves_to_llm(self) -> None:
        # Unstructured text mentioning "remote" or "on-site" is NOT regex-guessed
        row = {"description": "This is a 100% remote job with distributed team"}
        self.assertIsNone(infer_remote(row, is_remote_query=False))
        row_onsite = {"description": "This position is strictly on-site in office"}
        self.assertIsNone(infer_remote(row_onsite, is_remote_query=False))


class TestNormalizeSalary(unittest.TestCase):
    def test_usd_annual(self) -> None:
        row = {
            "min_amount": "120000",
            "max_amount": "160000",
            "interval": "yearly",
            "currency": "USD",
        }
        res = normalize_salary(row, country_hint="US")
        self.assertEqual(res["salary_min"], 120000.0)
        self.assertEqual(res["salary_max"], 160000.0)
        self.assertEqual(res["salary_annual_usd"], 140000.0)
        self.assertEqual(res["salary_currency_inferred"], 0)

    def test_hourly_usd(self) -> None:
        row = {"min_amount": 50, "max_amount": 70, "interval": "hourly", "currency": "USD"}
        res = normalize_salary(row, country_hint="US")
        # midpoint 60 * 2080 = 124,800
        self.assertEqual(res["salary_annual_usd"], 124800.0)

    def test_fx_conversion_sek(self) -> None:
        row = {"min_amount": 60000, "max_amount": 80000, "interval": "monthly", "currency": "SEK"}
        res = normalize_salary(row, country_hint="SE")
        # midpoint 70,000 * 12 * 0.095 = 79,800 USD
        self.assertEqual(res["salary_annual_usd"], 79800.0)

    def test_currency_inferred_from_country(self) -> None:
        row = {"min_amount": 100000, "max_amount": 120000, "interval": "yearly", "currency": None}
        res = normalize_salary(row, country_hint="US")
        self.assertEqual(res["salary_currency"], "USD")
        self.assertEqual(res["salary_currency_inferred"], 1)
        self.assertEqual(res["salary_annual_usd"], 110000.0)

    def test_sanity_filtering(self) -> None:
        # Too low (< 12k)
        low_row = {"min_amount": 100, "max_amount": 200, "interval": "yearly", "currency": "USD"}
        self.assertIsNone(normalize_salary(low_row)["salary_annual_usd"])
        # Too high (> 1.5M)
        high_row = {
            "min_amount": 5000000,
            "max_amount": 6000000,
            "interval": "yearly",
            "currency": "USD",
        }
        self.assertIsNone(normalize_salary(high_row)["salary_annual_usd"])


class TestNormalizeDatePosted(unittest.TestCase):
    def test_exact_iso_date(self) -> None:
        observed = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)
        res = normalize_date_posted({"date_posted": "2026-08-20T10:00:00Z"}, observed)
        self.assertEqual(res["date_posted"], "2026-08-20T10:00:00Z")
        self.assertEqual(res["date_precision"], "exact")

    def test_interval_censored(self) -> None:
        observed = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)
        res = normalize_date_posted({"date_posted": None}, observed, hours_old=24)
        self.assertIsNone(res["date_posted"])
        self.assertEqual(res["date_precision"], "interval")
        self.assertEqual(res["posted_window_start"], "2026-08-27T12:00:00+00:00")
        self.assertEqual(res["posted_window_end"], "2026-08-28T12:00:00+00:00")


class TestContentHashAndQuality(unittest.TestCase):
    def test_content_hash_collision_across_variants(self) -> None:
        # Title variation in parentheses or casing should hash to the same content_hash
        h1 = content_hash("Google Inc.", "Senior Software Engineer (Backend)", "Mountain View, CA")
        h2 = content_hash("Google LLC", "Senior Software Engineer", "Mountain View, CA")
        self.assertEqual(h1, h2)

    def test_description_quality(self) -> None:
        self.assertEqual(description_quality(""), "missing")
        self.assertEqual(description_quality(None), "missing")
        self.assertEqual(description_quality("short snippet"), "snippet")
        self.assertEqual(description_quality("a" * 450), "full")


class TestNormalizeRowAndRows(unittest.TestCase):
    def test_normalize_row_structure(self) -> None:
        task = {
            "source": "indeed",
            "country": "US",
            "location_label": "Los Angeles, CA",
            "is_remote": False,
            "hours_old": 24,
            "cell_id": 101,
        }
        row = {
            "id": "ind-12345",
            "title": "Backend Software Engineer",
            "company": "Acme Corp.",
            "location": "Los Angeles, CA, US",
            "job_url": "https://indeed.com/viewjob?jk=12345",
            "description": "We are looking for a backend engineer with Python experience..." * 10,
            "min_amount": 150000,
            "max_amount": 170000,
            "interval": "yearly",
            "currency": "USD",
        }
        posting = normalize_row(row, task)
        self.assertEqual(posting["title"], "Backend Software Engineer")
        self.assertEqual(posting["company_normalized"], "acme")
        self.assertEqual(posting["city"], "Los Angeles")
        self.assertEqual(posting["country"], "US")
        self.assertEqual(posting["salary_annual_usd"], 160000.0)
        self.assertEqual(posting["job_key"], "indeed-ind-12345")
        self.assertEqual(posting["description_quality"], "full")
        self.assertEqual(posting["scrape_cell_id"], 101)
        # Ensure no taxonomy/role classification coupling is present in normalizer
        self.assertNotIn("skills", posting)

    def test_normalize_rows_batch_and_stats(self) -> None:
        task = {"source": "linkedin", "country": "US"}
        rows = [
            {"title": "Role 1", "job_url": "https://example.com/1", "description": "a" * 500},
            {
                "title": "Role 2",
                "job_url": "https://example.com/2",
                "description": "short",
                "min_amount": 100000,
                "interval": "yearly",
                "currency": "USD",
            },
            {"title": "", "job_url": "https://example.com/3"},  # Skipped: missing title
            {"title": "Role 4", "job_url": ""},  # Skipped: missing URL
        ]
        postings, stats = normalize_rows(rows, task)
        self.assertEqual(len(postings), 2)
        self.assertEqual(stats["returned"], 4)
        self.assertEqual(stats["usable"], 2)
        self.assertEqual(stats["skipped"], 2)
        self.assertEqual(stats["with_full_description"], 1)
        self.assertEqual(stats["with_salary"], 1)


if __name__ == "__main__":
    unittest.main()
