"""Per-source circuit breaker and request pacing.

The central rule here departs deliberately from the usual HTTP client policy of retrying a
429 with exponential backoff. That is correct for a polite API, which is telling you to slow
down, and wrong for a job board, which is telling you the IP is flagged -- retrying inside
the same run deepens the flag rather than recovering from it. So a 429 trips the source for
the remainder of the run and persists an escalating backoff; only 5xx and timeouts are
retried.

The one case where a 429 IS retryable is a rotating proxy pool, because then it flagged one
exit IP rather than the account and the retry arrives from a different one. That is the
`rotating_proxies` branch in `on_error`, and nothing else here relaxes the rule.
"""

import random
import re
import time
from datetime import datetime, timedelta, timezone

from careerradar.core.logger import get_logger

logger = get_logger()

# Error classes, in the order the caller should care about.
ERROR_RATE_LIMIT = "rate_limit"
ERROR_BLOCKED = "blocked"
ERROR_TRANSIENT = "transient"
ERROR_FATAL = "fatal"

_RATE_LIMIT_MARKERS = re.compile(
    r"\b(?:429|too many requests|rate ?limit(?:ed|ing)?)\b", re.IGNORECASE
)
_BLOCKED_MARKERS = re.compile(
    r"\b(?:403|forbidden|captcha|challenge|access denied|unusual traffic|"
    r"blocked|bot detection)\b", re.IGNORECASE
)
_TRANSIENT_MARKERS = re.compile(
    r"\b(?:50[0-9]|timeout|timed out|connection (?:reset|aborted|error)|"
    r"temporarily unavailable|read timeout|ssl|"
    # A dead exit IP is the retryable case once proxies are pinned per cell: the retry
    # shifts to a different endpoint (proxies.pin_for), so the bad one is simply not used
    # again. Without these, JobSpy's "Bad proxy" wording matches nothing and classifies
    # fatal, which is not retried and counts toward tripping the source.
    r"bad proxy|proxy responded|proxy error)\b", re.IGNORECASE
)


def classify_error(exc):
    """Classify a scrape exception. Rate-limit and blocked are NOT retryable.

    An exception may carry `classify_text` to narrow what is matched to the wording that
    actually came from the board. Anything a caller adds around it -- row counts, retry
    counts -- is prose, and prose containing a bare "429" or "503" must not be able to
    decide that a source gets tripped for the rest of the run.
    """
    message = getattr(exc, "classify_text", None) or f"{type(exc).__name__}: {exc}"
    if _RATE_LIMIT_MARKERS.search(message):
        return ERROR_RATE_LIMIT
    if _BLOCKED_MARKERS.search(message):
        return ERROR_BLOCKED
    if _TRANSIENT_MARKERS.search(message):
        return ERROR_TRANSIENT
    return ERROR_FATAL


class SourceTripped(Exception):
    """Raised when a source's circuit is open for the remainder of the run."""


class SourceCircuit:
    """Tracks one source's health across a run and persists backoff across runs."""

    def __init__(self, source, config, db=None, now=None, sleep=time.sleep,
                 rotating_proxies=False):
        self.source = source
        # A rotating pool changes what a 429 means: one exit IP got flagged, not the
        # account, and the next request arrives from a different IP. Retrying is then the
        # correct response rather than the thing that deepens the flag.
        self.rotating_proxies = bool(rotating_proxies)
        self.config = config or {}
        self.db = db
        self.sleep = sleep
        self._now = now or (lambda: datetime.now(timezone.utc))

        breaker = self.config.get("circuit_breaker", {})
        self.errors_to_trip = breaker.get("consecutive_errors_to_trip", 3)
        self.rate_limit_trips_immediately = breaker.get(
            "http_429_trips_immediately", True
        )
        self.source_backoff_minutes = breaker.get(
            "source_backoff_minutes", [15, 60, 240, 1440]
        )
        self.cell_backoff_minutes = breaker.get(
            "cell_backoff_minutes", [60, 360, 1440]
        )
        self.max_retries = breaker.get("transient_retries", 2)
        self.proxy_rotation_retries = breaker.get("proxy_rotation_retries", 3)
        self.rotation_attempts = 0

        budget = (self.config.get("budgets") or {}).get(source, {})
        self.min_interval = budget.get("min_seconds_between_searches", 2.0)
        self.jitter = budget.get("jitter_seconds", [0.5, 2.0])

        self.tripped = False
        self.trip_reason = None
        self.consecutive_errors = 0
        self.searches_ok = 0
        self.rate_limit_hits = 0
        self.errors = []
        self._last_request_at = None

    # -- state ------------------------------------------------------------------------
    @property
    def is_open(self):
        return self.tripped

    def persisted_backoff_active(self):
        """Whether a previous run left this source in backoff."""
        if self.db is None:
            return False
        until = self.db.get_source_backoff(self.source)
        if not until:
            return False
        return _parse(until) > self._now()

    def backoff_until(self):
        if self.db is None:
            return None
        return self.db.get_source_backoff(self.source)

    # -- pacing -----------------------------------------------------------------------
    def before_request(self):
        if self.tripped:
            raise SourceTripped(f"{self.source} circuit is open: {self.trip_reason}")

        if self._last_request_at is not None:
            low, high = [*self.jitter, 0.0, 0.0][:2]
            target = self.min_interval + random.uniform(float(low), float(high))
            elapsed = (self._now() - self._last_request_at).total_seconds()
            if elapsed < target:
                self.sleep(target - elapsed)
        self._last_request_at = self._now()

    # -- outcomes ---------------------------------------------------------------------
    def on_success(self, returned=0):
        self.consecutive_errors = 0
        self.searches_ok += 1
        return returned

    def on_empty(self):
        """An empty result is not an error -- the query simply matched nothing."""
        self.consecutive_errors = 0
        self.searches_ok += 1

    def on_error(self, exc, cell=None):  # noqa: ARG002 - part of the source-guard callback signature
        """Record an error and decide whether the source should trip.

        Returns the error class so the caller can decide about retrying: only
        ERROR_TRANSIENT is worth another attempt.
        """
        error_class = classify_error(exc)
        message = str(exc)[:400]
        self.errors.append({"class": error_class, "error": message})

        if error_class in (ERROR_RATE_LIMIT, ERROR_BLOCKED):
            self.rate_limit_hits += 1
            if self.rotating_proxies and self.rotation_attempts < self.proxy_rotation_retries:
                # Burn a rotation attempt instead of tripping: the retry comes from a
                # different exit IP, so the flagged one is simply not used again.
                self.rotation_attempts += 1
                logger.warning(
                    f"[{self.source}] {error_class} on one exit IP; rotating "
                    f"({self.rotation_attempts}/{self.proxy_rotation_retries})"
                )
                return ERROR_TRANSIENT
            if self.rate_limit_trips_immediately:
                self._trip(f"{error_class} ({message[:120]})", escalate=True)
            return error_class

        self.consecutive_errors += 1
        if self.consecutive_errors >= self.errors_to_trip:
            self._trip(
                f"{self.consecutive_errors} consecutive failures ({message[:120]})",
                escalate=False,
            )
        return error_class

    def _trip(self, reason, escalate):
        self.tripped = True
        self.trip_reason = reason

        if self.db is None:
            logger.warning(f"[{self.source}] circuit tripped: {reason}")
            return

        trips = self.db.get_source_trips(self.source) if escalate else 0
        index = min(trips, len(self.source_backoff_minutes) - 1)
        minutes = self.source_backoff_minutes[index] if escalate else \
            self.source_backoff_minutes[0]
        until = self._now() + timedelta(minutes=minutes)
        self.db.set_source_backoff(
            self.source, until.isoformat(), reason=reason, escalate=escalate
        )
        logger.warning(
            f"[{self.source}] circuit tripped ({reason}); "
            f"backing off {minutes}m until {until.isoformat(timespec='seconds')}"
        )

    def cell_backoff(self, consecutive):
        """How long to sideline a single cell after `consecutive` bad outcomes."""
        index = min(max(consecutive - 1, 0), len(self.cell_backoff_minutes) - 1)
        minutes = self.cell_backoff_minutes[index]
        return (self._now() + timedelta(minutes=minutes)).isoformat()

    def note_clean_run(self):
        """Decay the escalation counter after a healthy run.

        Without this, one bad afternoon leaves the source pinned at a 24-hour backoff
        indefinitely.
        """
        if self.db is None or self.tripped:
            return
        if self.searches_ok >= 5 and not self.rate_limit_hits:
            self.db.reset_source_trips(self.source)

    def summary(self):
        return {
            "source": self.source,
            "tripped": self.tripped,
            "reason": self.trip_reason,
            "searches_ok": self.searches_ok,
            "rate_limit_hits": self.rate_limit_hits,
            "proxy_rotations": self.rotation_attempts,
            "errors": self.errors[:10],
        }


def _parse(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
