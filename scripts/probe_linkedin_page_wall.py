"""Standalone probe: at what page does LinkedIn's guest jobs API start blocking a single
IP, and how long does the block last?

The claim this measures ("LinkedIn rate-limits around the 10th page on a single IP") is
asserted in README.md, config.yaml, and search/scheduler.py, and drives real design
decisions (252-cell matrix, 5-day rotation cycle, the linkedin budget). Nothing in this
project has ever actually recorded a LinkedIn 429 -- source_state.total_429 is 0 and every
real sync_run shows rate_limit_hits: 0, because prod is deliberately kept at 2 pages/cell,
comfortably under the assumed wall. This script is the one place that goes looking for it
on purpose.

Deliberately NOT wired into the app:
  - no import of careerradar.search.scheduler / guard / sources -- this must not touch
    jobs.db, source_state, or sync_runs. Polluting those would corrupt the coverage
    metrics and circuit-breaker state the real scheduler depends on.
  - pins ONE proxy endpoint from $SCRAPER_PROXIES for the whole run, the same way
    search/proxies.py:pin_for() pins one endpoint per cell in production. This is the
    single-IP wall under test -- it's just that the "single IP" is a proxy exit rather
    than this machine's own address, which also means a block here lands on one pool
    entry rather than the address production's un-proxied traffic would otherwise share.
  - uses jobspy's own create_session()/headers so the request fingerprint matches what
    production already sends -- a probe with a different fingerprint could get a
    different (and irrelevant) answer.

Usage:
    export SCRAPER_PROXIES="user:pass@host:port,..."   # same var production reads
    uv run python scripts/probe_linkedin_page_wall.py \\
        --query "software engineer" --location "Austin, TX"

    # Pick a specific pool entry (0-based) instead of the first one:
    uv run python scripts/probe_linkedin_page_wall.py --proxy-index 2

    # After one or more runs, see what's accumulated:
    uv run python scripts/probe_linkedin_page_wall.py --summarize

Safety notes (read before running):
  - The proxy exit this run pins to is unavailable to production for the duration of any
    block it trips -- with N pool entries that's a 1/N capacity hit, not a full outage,
    but it's still real. Don't run this from cron or CI.
  - Run a given proxy endpoint at most once a day while investigating. Re-probing a
    flagged exit measures the block's persistence, not a fresh wall, and just extends
    that entry's downtime.
  - Pick a broad query/location (high true result count) so running out of real results
    isn't mistaken for a block -- see --query/--location defaults.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from bs4 import BeautifulSoup

EXPERIMENTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "experiments", "linkedin_page_wall"
)
SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"

# Minutes-after-first-anomaly to re-probe with a single request, to measure backoff
# duration rather than just where the wall sits. Doubling, capped at 2h so one run
# finishes in an afternoon.
RECOVERY_CHECKPOINTS_MIN = [5, 15, 30, 60, 120]


def redact(proxy: str | None) -> str | None:
    """Same redaction as search/proxies.py -- credentials never reach stdout or the log."""
    if not proxy or "@" not in proxy:
        return proxy
    return "***@" + proxy.rsplit("@", 1)[1]


def pick_proxy(proxy_arg: str | None, proxy_index: int) -> str | None:
    if proxy_arg:
        return proxy_arg
    raw = os.environ.get("SCRAPER_PROXIES", "").strip()
    if not raw:
        return None
    pool = [entry.strip() for entry in raw.split(",") if entry.strip()]
    if not pool:
        return None
    if proxy_index >= len(pool):
        raise SystemExit(
            f"--proxy-index {proxy_index} out of range: "
            f"pool has {len(pool)} entr{'y' if len(pool) == 1 else 'ies'}."
        )
    return pool[proxy_index]


def build_session(proxy: str | None) -> Any:
    # Same session JobSpy's LinkedIn scraper builds: RequestsRotating, no TLS
    # fingerprinting, retry on transient network errors, cookies cleared per request.
    # A single-element list means proxy_cycle always yields this one entry -- pinned for
    # the whole run, same as pin_for() pins one endpoint per cell in production.
    from jobspy.linkedin.constant import headers
    from jobspy.util import create_session

    # create_session's declared type is `dict | str | None`, but RequestsRotating's
    # underlying RotatingProxySession accepts a list to build its proxy_cycle -- the
    # stub is narrower than what the runtime actually does.
    session = create_session(
        proxies=[proxy] if proxy else None,  # type: ignore[arg-type]
        is_tls=False,
        has_retry=True,
        delay=5,
        clear_cookies=True,
    )
    session.headers.update(headers)
    return session


def fetch_page(
    session: Any, query: str, location: str, start: int, hours_old: int | None
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "keywords": query,
        "location": location,
        "pageNum": 0,
        "start": start,
    }
    if hours_old:
        params["f_TPR"] = f"r{hours_old * 3600}"

    t0 = time.monotonic()
    try:
        resp = session.get(SEARCH_URL, params=params, timeout=10)
    except Exception as exc:  # noqa: BLE001 - network fault is itself the observation
        return {
            "status_code": None,
            "elapsed_ms": round((time.monotonic() - t0) * 1000),
            "exception": f"{type(exc).__name__}: {exc}",
            "job_cards": 0,
            "body_snippet": None,
            "headers": {},
        }
    elapsed_ms = round((time.monotonic() - t0) * 1000)

    job_cards = 0
    if resp.status_code in range(200, 400):
        soup = BeautifulSoup(resp.text, "html.parser")
        job_cards = len(soup.find_all("div", class_="base-search-card"))

    anomaly = resp.status_code not in range(200, 400) or job_cards == 0
    return {
        "status_code": resp.status_code,
        "elapsed_ms": elapsed_ms,
        "exception": None,
        "job_cards": job_cards,
        # Only keep body text when something looks wrong -- a 429 page, a CAPTCHA
        # interstitial, or a suspicious empty 200 -- so normal runs stay small.
        "body_snippet": resp.text[:500] if anomaly else None,
        "headers": {
            k: v
            for k, v in resp.headers.items()
            if k.lower() in ("retry-after", "x-li-uuid", "x-fs-uuid", "content-length")
        },
    }


def run_probe(args: argparse.Namespace, writer: Callable[[dict[str, Any]], None]) -> None:
    session = build_session(args.proxy)
    start = 0
    consecutive_anomalies = 0
    first_anomaly_page = None
    wall_confirmed = False
    cumulative = 0

    for page in range(1, args.max_pages + 1):
        result = fetch_page(session, args.query, args.location, start, args.hours_old)
        cumulative += result["job_cards"]
        record = {
            "run_id": args.run_id,
            "proxy": redact(args.proxy),
            "phase": "ascend",
            "page": page,
            "start_offset": start,
            "ts": datetime.now(timezone.utc).isoformat(),
            "cumulative_jobs": cumulative,
            **result,
        }
        writer(record)
        print(
            f"  page {page:>3} (start={start:>4}): "
            f"status={result['status_code']} cards={result['job_cards']} "
            f"{result['elapsed_ms']}ms"
            + (
                "  <-- ANOMALY"
                if result["job_cards"] == 0 or result["status_code"] not in range(200, 400)
                else ""
            )
        )

        is_anomaly = (
            result["status_code"] is None
            or result["status_code"] not in range(200, 400)
            or result["job_cards"] == 0
        )
        if is_anomaly:
            consecutive_anomalies += 1
            if first_anomaly_page is None:
                first_anomaly_page = page
            if consecutive_anomalies >= 2:
                wall_confirmed = True
                print(
                    f"\nWall hit: 2 consecutive anomalies starting at page "
                    f"{first_anomaly_page} (start_offset="
                    f"{start - (result['job_cards'] or 0)})."
                )
                break
        else:
            consecutive_anomalies = 0
            start += result["job_cards"]

        if page < args.max_pages:
            time.sleep(args.pace_seconds)
    else:
        if first_anomaly_page is not None:
            # Reached --max-pages with a lone trailing anomaly, not 2-in-a-row -- this is
            # LinkedIn's own guest-API pagination ceiling (JobSpy hardcodes start < 1000),
            # a generic end-of-result-window every search engine has, not IP-based
            # blocking. Don't chase it into a multi-hour recovery check.
            print(
                f"\nNo wall found in {args.max_pages} pages. One trailing anomaly at "
                f"page {first_anomaly_page} -- likely the API's own result-window "
                f"ceiling (start reached ~1000), not a block. Raise --max-pages only "
                f"if start_offset was well under 1000 when this printed."
            )
        else:
            print(
                f"\nNo wall found in {args.max_pages} pages -- raise --max-pages to "
                f"look further, or the wall is above what was tested."
            )

    if not wall_confirmed or args.no_recovery_check:
        return

    print("\nRecovery check: single request at each checkpoint until one succeeds.")
    for minutes in RECOVERY_CHECKPOINTS_MIN:
        print(f"  waiting {minutes}min...")
        time.sleep(minutes * 60)
        result = fetch_page(session, args.query, args.location, 0, args.hours_old)
        record = {
            "run_id": args.run_id,
            "proxy": redact(args.proxy),
            "phase": "recovery",
            "checkpoint_minutes": minutes,
            "ts": datetime.now(timezone.utc).isoformat(),
            **result,
        }
        writer(record)
        recovered = result["status_code"] in range(200, 400) and result["job_cards"] > 0
        print(
            f"  +{minutes}min: status={result['status_code']} "
            f"cards={result['job_cards']} "
            f"{'RECOVERED' if recovered else 'still blocked'}"
        )
        if recovered:
            print(f"\nBackoff duration: recovered within {minutes} minutes of first anomaly.")
            return
    print(
        "\nStill blocked after all checkpoints -- backoff exceeds "
        f"{RECOVERY_CHECKPOINTS_MIN[-1]} minutes."
    )


def summarize() -> None:
    if not os.path.isdir(EXPERIMENTS_DIR):
        print(f"No runs yet -- {EXPERIMENTS_DIR} does not exist.")
        return
    runs = {}
    for name in sorted(os.listdir(EXPERIMENTS_DIR)):
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(EXPERIMENTS_DIR, name)) as fh:
            records = [json.loads(line) for line in fh if line.strip()]
        if not records:
            continue
        run_id = records[0]["run_id"]
        runs[run_id] = records

    if not runs:
        print("No runs recorded yet.")
        return

    print(f"{'run_id':<22} {'proxy':<24} {'first anomaly page':<20} {'recovered at':<15}")
    for run_id, records in runs.items():
        proxy = records[0].get("proxy") or "(none - local IP)"
        ascend = [r for r in records if r["phase"] == "ascend"]
        anomaly_pages = [
            r["page"]
            for r in ascend
            if r["job_cards"] == 0 or r["status_code"] not in range(200, 400)
        ]
        first_anomaly = anomaly_pages[0] if anomaly_pages else "none in range"
        recovery = [
            r
            for r in records
            if r["phase"] == "recovery"
            and r["status_code"] in range(200, 400)
            and r["job_cards"] > 0
        ]
        recovered_at = f"{recovery[0]['checkpoint_minutes']}min" if recovery else "-"
        print(f"{run_id:<22} {proxy:<24} {first_anomaly!s:<20} {recovered_at:<15}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--query",
        default="software engineer",
        help="Broad, high-volume query so exhausting real results before the wall is unlikely.",
    )
    parser.add_argument(
        "--location", default="United States", help="Broad location, same reasoning as --query."
    )
    parser.add_argument(
        "--hours-old",
        type=int,
        default=336,
        help="Matches the backfill_hours_old prod actually uses.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=40,
        help="~25 results/page; LinkedIn's guest API caps start<1000, "
        "i.e. ~40 pages is the hard ceiling anyway.",
    )
    parser.add_argument(
        "--pace-seconds",
        type=float,
        default=5.0,
        help="Delay between pages. JobSpy's own internal pacing during "
        "a real scrape is random 3-7s -- default matches that so "
        "the probe measures the wall under real operating "
        "conditions, not an artificially slow or fast one.",
    )
    parser.add_argument(
        "--no-recovery-check",
        action="store_true",
        help="Skip measuring how long the block lasts after it's hit.",
    )
    parser.add_argument(
        "--proxy",
        default=None,
        help="Explicit 'user:pass@host:port' to pin for the whole run. "
        "Defaults to entry --proxy-index of $SCRAPER_PROXIES, the "
        "same env var production reads.",
    )
    parser.add_argument(
        "--proxy-index",
        type=int,
        default=0,
        help="Which entry of $SCRAPER_PROXIES to pin when --proxy is "
        "not given (0-based). Ignored if --proxy is set.",
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="Hit LinkedIn directly from this machine's own IP instead "
        "of a proxy. Off by default: production's un-proxied "
        "traffic doesn't exist (SCRAPER_PROXIES is always set), so "
        "this measures an IP nothing actually scrapes from.",
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--summarize", action="store_true", help="Print results from all prior runs and exit."
    )
    args = parser.parse_args()

    if args.summarize:
        summarize()
        return

    if args.no_proxy:
        args.proxy = None
    else:
        args.proxy = pick_proxy(args.proxy, args.proxy_index)
        if not args.proxy:
            raise SystemExit(
                "No proxy available: $SCRAPER_PROXIES is unset/empty. Pass --proxy "
                "explicitly, or --no-proxy to deliberately probe this machine's own IP."
            )

    args.run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    os.makedirs(EXPERIMENTS_DIR, exist_ok=True)
    out_path = os.path.join(EXPERIMENTS_DIR, f"{args.run_id}.jsonl")

    print("Probing LinkedIn page wall.")
    print(f"  proxy={redact(args.proxy) if args.proxy else '(none - local IP)'}")
    print(f"  query={args.query!r} location={args.location!r} pace={args.pace_seconds}s")
    print(f"  writing to {out_path}\n")

    with open(out_path, "a", encoding="utf-8") as fh:

        def writer(record: dict[str, Any]) -> None:
            fh.write(json.dumps(record) + "\n")
            fh.flush()

        try:
            run_probe(args, writer)
        except KeyboardInterrupt:
            print("\nInterrupted -- partial results kept.", file=sys.stderr)


if __name__ == "__main__":
    main()
