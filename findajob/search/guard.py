"""Per-source failure classification and persisted backoff.

SourceTraffic owns request pacing. Blocking and rate-limit responses interrupt
work regardless of proxy availability; switching endpoints is not proof of recovery.
"""

import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from findajob.core.logger import get_logger

if TYPE_CHECKING:
    from findajob.core.database import Database

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
    r"blocked|bot detection)\b",
    re.IGNORECASE,
)
_TRANSIENT_MARKERS = re.compile(
    r"\b(?:50[0-9]|timeout|timed out|connection (?:reset|aborted|error)|"
    r"temporarily unavailable|read timeout|ssl|"
    # A dead exit IP is the retryable case once proxies are pinned per cell: the retry
    # shifts to a different endpoint (proxies.pin_for), so the bad one is simply not used
    # again. Without these, JobSpy's "Bad proxy" wording matches nothing and classifies
    # fatal, which is not retried and counts toward tripping the source.
    r"bad proxy|proxy responded|proxy error)\b",
    re.IGNORECASE,
)


def classify_error(exc: BaseException) -> str:
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

    def __init__(
        self,
        source: str,
        config: dict[str, Any] | None,
        db: "Database | None" = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.source = source
        self.config = config or {}
        self.db = db
        self._now = now or (lambda: datetime.now(timezone.utc))

        breaker = self.config.get("circuit_breaker", {})
        self.errors_to_trip = breaker.get("consecutive_errors_to_trip", 3)
        self.rate_limit_trips_immediately = breaker.get("http_429_trips_immediately", True)
        self.source_backoff_minutes = breaker.get("source_backoff_minutes", [15, 60, 240, 1440])
        self.cell_backoff_minutes = breaker.get("cell_backoff_minutes", [60, 360, 1440])

        self.tripped = False
        self.trip_reason: str | None = None
        self.consecutive_errors = 0
        self.searches_ok = 0
        self.rate_limit_hits = 0
        self.errors: list[dict[str, Any]] = []

    # -- state ------------------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self.tripped

    def persisted_backoff_active(self) -> bool:
        """Whether a previous run left this source in backoff."""
        if self.db is None:
            return False
        until = self.db.get_source_backoff(self.source)
        if not until:
            return False
        return _parse(until) > self._now()

    def backoff_until(self) -> str | None:
        if self.db is None:
            return None
        return self.db.get_source_backoff(self.source)

    # -- pacing -----------------------------------------------------------------------
    def before_request(self) -> None:
        if self.tripped:
            raise SourceTripped(f"{self.source} circuit is open: {self.trip_reason}")

    # -- outcomes ---------------------------------------------------------------------
    def on_detail_success(self) -> None:
        """A successful enrichment request is not another completed search."""
        self.consecutive_errors = 0

    def on_success(self, returned: int = 0) -> int:
        self.consecutive_errors = 0
        self.searches_ok += 1
        return returned

    def on_empty(self) -> None:
        """An empty result is not an error -- the query simply matched nothing."""
        self.consecutive_errors = 0
        self.searches_ok += 1

    def on_error(self, exc: BaseException, cell: int | None = None) -> str:  # noqa: ARG002 - part of the source-guard callback signature
        """Record an error and decide whether the source should trip.

        Returns the error class so the caller can decide about retrying: only
        ERROR_TRANSIENT is worth another attempt.
        """
        error_class = classify_error(exc)
        message = str(exc)[:400]
        self.errors.append({"class": error_class, "error": message})

        if error_class in (ERROR_RATE_LIMIT, ERROR_BLOCKED):
            self.rate_limit_hits += 1
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

    def _trip(self, reason: str, escalate: bool) -> None:
        self.tripped = True
        self.trip_reason = reason

        if self.db is None:
            logger.warning(f"[{self.source}] circuit tripped: {reason}")
            return

        trips = self.db.get_source_trips(self.source) if escalate else 0
        index = min(trips, len(self.source_backoff_minutes) - 1)
        minutes = self.source_backoff_minutes[index] if escalate else self.source_backoff_minutes[0]
        until = self._now() + timedelta(minutes=minutes)
        self.db.set_source_backoff(self.source, until.isoformat(), reason=reason, escalate=escalate)
        logger.warning(
            f"[{self.source}] circuit tripped ({reason}); "
            f"backing off {minutes}m until {until.isoformat(timespec='seconds')}"
        )

    def cell_backoff(self, consecutive: int) -> str:
        """How long to sideline a single cell after `consecutive` bad outcomes."""
        index = min(max(consecutive - 1, 0), len(self.cell_backoff_minutes) - 1)
        minutes = self.cell_backoff_minutes[index]
        return (self._now() + timedelta(minutes=minutes)).isoformat()

    def note_clean_run(self) -> None:
        """Decay the escalation counter after a healthy run.

        Without this, one bad afternoon leaves the source pinned at a 24-hour backoff
        indefinitely.
        """
        if self.db is None or self.tripped:
            return
        if self.searches_ok >= 5 and not self.rate_limit_hits:
            self.db.reset_source_trips(self.source)

    def summary(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "tripped": self.tripped,
            "reason": self.trip_reason,
            "searches_ok": self.searches_ok,
            "rate_limit_hits": self.rate_limit_hits,
            "errors": self.errors[:10],
        }


def _parse(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
