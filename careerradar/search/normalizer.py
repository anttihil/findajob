"""Normalize raw board rows into the shape the database and analytics expect.

Pure functions over dicts, so every rule here is unit-testable without a network call.
Contextual assessment (role fit, technical depth, remote/hybrid nuances, clearance/domain)
is left to downstream scoring and LLM stages.

Four normalizations carry real analytical weight:

  location  JobSpy returns "Commerce, CA, US" as one string. Country comes from the
            *query*, not the row, because the row's trailing token is unreliable and the
            query is what defines the market being sampled.
  salary    min/max/interval/currency -> a single annual USD figure. Without this, a SEK
            monthly figure and a USD hourly figure land in the same median.
  dedup     a content hash over normalized (company, title, location), because the same
            requisition appears on both boards with different URLs and ids.
  agency    staffing firms repost one requisition many times under varied titles, which
            near-duplicate detection cannot catch since the descriptions genuinely differ.
"""

import hashlib
import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

# Descriptions shorter than this are snippets, not job descriptions. Skill extraction over a
# snippet produces a handful of hits and silently deflates every demand denominator, so
# these are excluded from skill statistics rather than treated as full text.
FULL_DESCRIPTION_MIN_CHARS = 400

# Pay-period multipliers to annualize.
INTERVAL_FACTOR = {
    "yearly": 1,
    "annual": 1,
    "annually": 1,
    "year": 1,
    "monthly": 12,
    "month": 12,
    "weekly": 52,
    "week": 52,
    "biweekly": 26,
    "daily": 260,
    "day": 260,
    "hourly": 2080,
    "hour": 2080,
}

# Static FX rates, annual-USD conversion only. Exact rates do not matter much because
# salary_lift is a ratio within a stratum, and strata include country precisely so that
# cross-currency comparisons never happen implicitly.
DEFAULT_FX_TO_USD = {
    "USD": 1.0,
    "EUR": 1.08,
    "SEK": 0.095,
    "NOK": 0.093,
    "DKK": 0.145,
    "GBP": 1.27,
    "CAD": 0.73,
}

# A parsed salary outside this band is a parse error (a stray "401k" or an equity figure),
# not a real offer.
SALARY_SANITY_MIN_USD = 12_000
SALARY_SANITY_MAX_USD = 1_500_000

# Legal-form suffixes stripped before hashing, so "Spotify AB" and "Spotify" collide.
# The Nordic forms matter here because SE/NO/DK/FI are in scope.
# Legal forms only. Words like "Technologies", "Group", or "Systems" are part of the actual
# name -- stripping them would make "Foo Technologies" and "Foo Systems" collide, turning two
# employers into one and understating n_companies.
COMPANY_SUFFIXES = [
    "incorporated",
    "inc",
    "llc",
    "l.l.c",
    "ltd",
    "limited",
    "corp",
    "corporation",
    "plc",
    "gmbh",
    "mbh",
    "ag",
    "sarl",
    "sas",
    "bv",
    "nv",
    "ab",
    "asa",
    "as",
    "a/s",
    "aps",
    "oy",
    "oyj",
    "kk",
    "pty",
    "pte",
    "srl",
    "spa",
]
_SUFFIX_RE = re.compile(
    r"[\s,]+(?:{})\.?$".format("|".join(re.escape(s) for s in COMPANY_SUFFIXES)),
    re.IGNORECASE,
)


def _text(value: Any) -> str:
    """Coerce a value that may be NaN, None, or a pandas scalar into a clean string."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    if text.lower() in ("nan", "none", "null", "<na>"):
        return ""
    return text


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def normalize_company(name: Any) -> str:
    """Casefold, strip legal-form suffixes and punctuation, collapse whitespace."""
    text = _text(name)
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    # Strip repeatedly: "Foo Technologies Inc" needs two passes.
    for _ in range(3):
        stripped = _SUFFIX_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    text = re.sub(r"[^\w\s&+-]", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def is_agency(company: Any, agency_names: list[str] | None) -> bool:
    """Whether a company is a staffing agency or consultancy.

    Left out of analytics by default: agencies repost the same requisition under many
    slightly different titles, so without this the top of every supply chart is staffing
    firms rather than employers.
    """
    normalized = normalize_company(company)
    if not normalized:
        return False
    for name in agency_names or []:
        candidate = normalize_company(name)
        if candidate and (candidate == normalized or candidate in normalized):
            return True
    return False


def parse_location(
    raw: Any,
    country_hint: str | None = None,
    is_remote_query: bool = False,
    fallback_label: str = "",
) -> dict[str, Any]:
    """Split a board location string into city / region / country.

    JobSpy emits "Commerce, CA, US" or "Stockholm, Sweden" or "Remote". The trailing token
    is inconsistent, so `country_hint` (from the query) wins -- the query defines which
    market is being sampled, which is what the statistics are about.
    """
    text = _text(raw)
    parts = [p.strip() for p in text.split(",") if p.strip()]

    city = region = ""
    if parts:
        # Drop a trailing country token; the hint is authoritative.
        if len(parts) >= 3 or len(parts) == 2:
            city, region = parts[0], parts[1]
        else:
            city = parts[0]

    if city.lower() in ("remote", "anywhere", "n/a"):
        city = ""

    if city or region:
        label = ", ".join(p for p in (city, region) if p)
    elif is_remote_query:
        label = "Remote"
    else:
        label = fallback_label or text

    return {
        "location": label,
        "city": city,
        "region": region,
        "country": (country_hint or "").upper() or None,
    }


def infer_remote(row: dict[str, Any], is_remote_query: bool = False) -> int | None:
    """1 / 0 / None. None means undetermined from board metadata / query.

    Avoids regex-guessing on unstructured description text; contextual work-arrangement
    nuances are evaluated by the LLM.
    """
    explicit = row.get("is_remote")
    if explicit is not None and not (isinstance(explicit, float) and math.isnan(explicit)):
        try:
            return 1 if bool(explicit) else 0
        except (TypeError, ValueError):
            pass

    if is_remote_query:
        return 1

    return None


def normalize_salary(
    row: dict[str, Any], country_hint: str | None = None, fx: dict[str, float] | None = None
) -> dict[str, Any]:
    """Normalize a salary range to annual USD.

    Returns the original figures for display plus `salary_annual_usd` for comparison.
    `salary_currency_inferred` records whether the currency was stated by the board or
    guessed from the query country -- salary_lift must disclose that, and its strata
    include country so inferred and stated figures never share a median.
    """
    fx = fx or DEFAULT_FX_TO_USD
    low = _number(row.get("min_amount"))
    high = _number(row.get("max_amount"))
    interval = _text(row.get("interval")).lower() or None
    currency = _text(row.get("currency")).upper() or None

    inferred = 0
    if not currency:
        currency = {
            "US": "USD",
            "SE": "SEK",
            "NO": "NOK",
            "DK": "DKK",
            "FI": "EUR",
        }.get((country_hint or "").upper())
        inferred = 1 if currency else 0

    result = {
        "salary_min": low,
        "salary_max": high,
        "salary_interval": interval,
        "salary_currency": currency,
        "salary_currency_inferred": inferred,
        "salary_annual_usd": None,
        "salary_source": _text(row.get("salary_source")) or None,
    }

    if low is None and high is None:
        return result

    values = [v for v in (low, high) if v is not None]
    midpoint = sum(values) / len(values)

    factor = INTERVAL_FACTOR.get(interval) if interval else None
    rate = fx.get(currency) if currency else None
    if factor is None or rate is None:
        return result

    annual = midpoint * factor * rate
    if SALARY_SANITY_MIN_USD <= annual <= SALARY_SANITY_MAX_USD:
        result["salary_annual_usd"] = round(annual, 2)
    return result


def normalize_date_posted(
    row: dict[str, Any], observed_at: datetime, hours_old: float | None = None
) -> dict[str, Any]:
    """Resolve the posting date or timestamp, recording how precisely it is known.

    JobSpy does supply `date_posted` for Indeed, so this is usually exact. When it is
    missing, the observation is interval-censored: the posting appeared somewhere inside
    [observed_at - hours_old, observed_at]. Recording the window rather than inventing a
    date is what lets the analytics label the axis "week observed" honestly.
    """
    raw = row.get("date_posted")
    posted = None

    if isinstance(raw, (datetime, date)):
        posted = raw
    else:
        text = _text(raw)
        if text:
            try:
                posted = datetime.fromisoformat(text.strip())
            except ValueError:
                try:
                    posted = date.fromisoformat(text.strip())
                except ValueError:
                    for fmt in (
                        "%Y-%m-%d %H:%M:%S",
                        "%Y/%m/%d",
                        "%d-%m-%Y",
                    ):
                        try:
                            dt = datetime.strptime(text.strip()[:19], fmt)
                            posted = dt if "%H" in fmt else dt.date()
                            break
                        except ValueError:
                            continue

    window_end = observed_at
    window_start = observed_at - timedelta(hours=hours_old) if hours_old else None

    if isinstance(posted, datetime):
        posted_iso = (
            posted.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if posted.tzinfo
            else f"{posted.strftime('%Y-%m-%dT%H:%M:%S')}Z"
        )
    elif isinstance(posted, date):
        posted_iso = f"{posted.isoformat()}T12:00:00Z"
    else:
        posted_iso = None

    return {
        "date_posted": posted_iso,
        "date_precision": "exact" if posted else ("interval" if hours_old else "unknown"),
        "posted_window_start": window_start.isoformat() if window_start else None,
        "posted_window_end": window_end.isoformat(),
    }


def content_hash(company: Any, title: Any, location: Any) -> str:
    """Stable hash for cross-board deduplication.

    The same requisition on LinkedIn and Indeed has different URLs and different board ids,
    so url/job_key alone cannot detect it. Normalizing company and stripping seniority
    decoration from the title makes the collision happen.
    """
    company_key = normalize_company(company)
    title_key = _text(title).lower()
    title_key = re.sub(r"\(.*?\)", " ", title_key)
    title_key = re.sub(r"[^\w\s]", " ", title_key)
    title_key = re.sub(r"\s+", " ", title_key).strip()
    location_key = re.sub(r"[^\w]", "", _text(location).lower())
    payload = f"{company_key}|{title_key}|{location_key}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def description_quality(description: Any) -> str:
    text = _text(description)
    if not text:
        return "missing"
    if len(text) < FULL_DESCRIPTION_MIN_CHARS:
        return "snippet"
    return "full"


def normalize_row(
    row: dict[str, Any],
    task: dict[str, Any],
    observed_at: datetime | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Turn one board row into a database-ready posting dict with normalized fields.

    `task` supplies the query provenance: source, location, country, remote flag, and the
    `hours_old` bound.
    """
    config = config or {}
    observed_at = observed_at or datetime.now(timezone.utc)

    title = _text(row.get("title"))
    company = _text(row.get("company"))
    url = _text(row.get("job_url"))
    description = _text(row.get("description"))
    site = _text(row.get("site")) or task.get("source", "")
    site_job_id = _text(row.get("id"))

    location = parse_location(
        row.get("location"),
        country_hint=task.get("country"),
        is_remote_query=task.get("is_remote", False),
        fallback_label=task.get("location_label", ""),
    )

    posting = {
        "title": title,
        "company": company,
        "company_normalized": normalize_company(company),
        "url": url,
        "url_direct": _text(row.get("job_url_direct")) or None,
        "description": description,
        "description_quality": description_quality(description),
        "desc_selection": task.get("desc_selection", "none"),
        "source": site,
        "site_job_id": site_job_id or None,
        # job_key must be stable across runs and unique per board posting.
        "job_key": f"{site}-{site_job_id}" if site_job_id else (url or None),
        "is_remote": infer_remote(row, task.get("is_remote", False)),
        "is_agency": 1 if is_agency(company, config.get("agencies")) else 0,
        "company_num_employees": _text(row.get("company_num_employees")) or None,
        "company_industry": _text(row.get("company_industry")) or None,
        "scrape_cell_id": task.get("cell_id"),
        "role_family_hint": task.get("role_family"),
    }
    posting.update(location)
    posting.update(normalize_salary(row, task.get("country"), config.get("fx")))
    posting.update(normalize_date_posted(row, observed_at, task.get("hours_old")))
    posting["content_hash"] = content_hash(company, title, posting["location"])

    return posting


def normalize_rows(
    rows: list[dict[str, Any]],
    task: dict[str, Any],
    observed_at: datetime | None = None,
    config: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Normalize a batch, dropping rows with no title or no URL.

    Returns (postings, stats) where stats carries the counts `cell_observations` needs.
    """
    observed_at = observed_at or datetime.now(timezone.utc)
    postings = []
    skipped = 0

    for row in rows:
        if not _text(row.get("title")) or not _text(row.get("job_url")):
            skipped += 1
            continue
        postings.append(normalize_row(row, task, observed_at, config))

    stats = {
        "returned": len(rows),
        "usable": len(postings),
        "skipped": skipped,
        "with_full_description": sum(1 for p in postings if p["description_quality"] == "full"),
        "with_salary": sum(1 for p in postings if p["salary_annual_usd"] is not None),
    }
    return postings, stats
