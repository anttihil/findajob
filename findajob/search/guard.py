"""Per-endpoint failure classification, backoff, and broken-proxy tracking."""

import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from findajob.core.logger import get_logger
from findajob.search.proxies import proxy_id, redact

if TYPE_CHECKING:
    from findajob.core.database import Database

logger = get_logger()

ALL_SOURCES = ("indeed", "linkedin")

# Error classes, in precedence order.
ERROR_RATE_LIMIT = "rate_limit"
ERROR_PROXY = "proxy_transport"
ERROR_BLOCKED = "blocked"
ERROR_TRANSIENT = "transient"
ERROR_FATAL = "fatal"

_RATE_LIMIT_MARKERS = re.compile(
    r"\b(?:429|too many requests|rate ?limit(?:ed|ing)?)\b", re.IGNORECASE
)
_PROXY_MARKERS = re.compile(
    r"\b(?:407|proxy authentication|proxyerror|unable to connect to proxy|"
    r"tunnel connection failed|bad proxy|proxy responded|proxy error)\b",
    re.IGNORECASE,
)
_BLOCKED_MARKERS = re.compile(
    r"\b(?:403|forbidden|captcha|challenge|access denied|unusual traffic|"
    r"blocked|bot detection)\b",
    re.IGNORECASE,
)
_TRANSIENT_MARKERS = re.compile(
    r"\b(?:50[0-9]|timeout|timed out|connection (?:reset|aborted|error)|"
    r"temporarily unavailable|read timeout|ssl)\b",
    re.IGNORECASE,
)

PROXY_BACKOFF_MINUTES = (60, 300)
PROXY_BROKEN_FAILURES = 3
BOARD_BLOCK_BACKOFF_MINUTES = (60, 240, 1440)
TRANSIENT_BACKOFF_MINUTES = (15, 60, 240)


def classify_error(exc: BaseException) -> str:
    """Classify a scrape exception into proxy, rate-limit, blocked, transient, or fatal."""
    message = getattr(exc, "classify_text", None) or f"{type(exc).__name__}: {exc}"
    if _RATE_LIMIT_MARKERS.search(message):
        return ERROR_RATE_LIMIT
    if _PROXY_MARKERS.search(message):
        return ERROR_PROXY
    if _BLOCKED_MARKERS.search(message):
        return ERROR_BLOCKED
    if _TRANSIENT_MARKERS.search(message):
        return ERROR_TRANSIENT
    return ERROR_FATAL


class SourceTripped(Exception):
    """Raised when a source has no usable endpoints or hits its systemic tripwire."""


class SourceCircuit:
    """Tracks per-endpoint health for one source and persists failures in proxy_state."""

    def __init__(
        self,
        source: str,
        config: dict[str, Any] | None,
        db: "Database | None" = None,
        now: Callable[[], datetime] | None = None,
        proxies: list[str] | None = None,
    ) -> None:
        self.source = source
        self.config = config or {}
        self.db = db
        self._now = now or (lambda: datetime.now(timezone.utc))

        breaker = self.config.get("circuit_breaker", {})
        self.errors_to_trip = int(breaker.get("consecutive_errors_to_trip", 3))
        self.systemic_trip_limit = int(breaker.get("systemic_trip_endpoints", 8))
        self.cell_backoff_minutes = breaker.get("cell_backoff_minutes", [60, 360, 1440])

        self.endpoints: list[str | None] = list(dict.fromkeys(proxies or [])) or [None]
        self.proxy_states: dict[str, dict[str, Any]] = {}
        if self.db is not None:
            for row in self.db.get_proxy_states(source):
                self.proxy_states[row["proxy_id"]] = dict(row)

        self.tripped = False
        self.trip_reason: str | None = None
        self.consecutive_errors = 0
        self.searches_ok = 0
        self.rate_limit_hits = 0
        self.failed_endpoints_streak: set[str] = set()
        self.errors: list[dict[str, Any]] = []

    def set_endpoints(self, endpoints: list[str | None]) -> None:
        self.endpoints = list(dict.fromkeys(endpoints)) or [None]

    # -- state ------------------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self.tripped

    def is_usable(self, endpoint: str | None) -> bool:
        state = self.proxy_states.get(proxy_id(endpoint))
        if not state:
            return True
        if state["status"] == "broken":
            return False
        until = state.get("backoff_until")
        return not (until and _parse(until) > self._now())

    def pool_counts(self) -> dict[str, int]:
        usable = backoff = broken = 0
        now = self._now()
        for ep in self.endpoints:
            state = self.proxy_states.get(proxy_id(ep))
            if not state:
                usable += 1
            elif state["status"] == "broken":
                broken += 1
            elif state.get("backoff_until") and _parse(state["backoff_until"]) > now:
                backoff += 1
            else:
                usable += 1
        return {
            "configured": len(self.endpoints),
            "usable": usable,
            "backoff": backoff,
            "broken": broken,
        }

    def persisted_backoff_active(self) -> bool:
        """Whether every configured endpoint for this source is broken or in backoff."""
        return not any(self.is_usable(ep) for ep in self.endpoints)

    def backoff_until(self) -> str | None:
        now = self._now()
        active = [
            state["backoff_until"]
            for ep in self.endpoints
            if (state := self.proxy_states.get(proxy_id(ep)))
            and state["status"] != "broken"
            and state.get("backoff_until")
            and _parse(state["backoff_until"]) > now
        ]
        if active:
            return min(active, key=_parse)
        if self.persisted_backoff_active():
            return "all proxies broken"
        return None

    # -- pacing -----------------------------------------------------------------------
    def before_request(self) -> None:
        if self.tripped:
            raise SourceTripped(f"{self.source} circuit is open: {self.trip_reason}")

    # -- outcomes ---------------------------------------------------------------------
    def on_request_success(self, endpoint: str | None) -> None:
        pid = proxy_id(endpoint)
        self.consecutive_errors = 0
        self.failed_endpoints_streak.clear()
        if pid in self.proxy_states:
            del self.proxy_states[pid]
            if self.db is not None:
                self.db.clear_proxy_state(proxy_id=pid, source=self.source)

    def on_detail_success(self) -> None:
        """A successful enrichment request is not another completed search."""
        if len(self.endpoints) == 1:
            self.on_request_success(self.endpoints[0])
        else:
            self.consecutive_errors = 0
            self.failed_endpoints_streak.clear()

    def on_success(self, returned: int = 0) -> int:
        if len(self.endpoints) == 1:
            self.on_request_success(self.endpoints[0])
        else:
            self.consecutive_errors = 0
            self.failed_endpoints_streak.clear()
        self.searches_ok += 1
        return returned

    def on_empty(self) -> None:
        """An empty result is not an error -- the query simply matched nothing."""
        if len(self.endpoints) == 1:
            self.on_request_success(self.endpoints[0])
        else:
            self.consecutive_errors = 0
            self.failed_endpoints_streak.clear()
        self.searches_ok += 1

    def on_request_error(self, exc: BaseException, endpoint: str | None) -> str:
        """Record a failed HTTP attempt against its endpoint and check run tripwires."""
        pid = proxy_id(endpoint)
        label = redact(endpoint) or "direct"
        error_class = classify_error(exc)
        message = str(exc)[:400]
        self.errors.append({"class": error_class, "proxy": label, "error": message})

        prev = self.proxy_states.get(pid)
        failures = (int(prev["consecutive_failures"]) if prev else 0) + 1
        minutes: int | None = None
        status = "backoff"
        sources: tuple[str, ...] = (self.source,)

        if error_class == ERROR_PROXY:
            sources = ALL_SOURCES
            if pid != "direct" and failures >= PROXY_BROKEN_FAILURES:
                status = "broken"
            else:
                minutes = PROXY_BACKOFF_MINUTES[min(failures - 1, len(PROXY_BACKOFF_MINUTES) - 1)]
        elif error_class in (ERROR_RATE_LIMIT, ERROR_BLOCKED):
            self.rate_limit_hits += 1
            minutes = BOARD_BLOCK_BACKOFF_MINUTES[
                min(failures - 1, len(BOARD_BLOCK_BACKOFF_MINUTES) - 1)
            ]
        else:
            self.consecutive_errors += 1
            threshold = self.errors_to_trip if len(self.endpoints) == 1 else 2
            if failures >= threshold:
                idx = min(failures - threshold, len(TRANSIENT_BACKOFF_MINUTES) - 1)
                minutes = TRANSIENT_BACKOFF_MINUTES[idx]

        until_dt = self._now() + timedelta(minutes=minutes) if minutes is not None else None
        until = until_dt.isoformat() if until_dt is not None else None
        state = {
            "proxy_id": pid,
            "source": self.source,
            "status": status,
            "backoff_until": until,
            "consecutive_failures": failures,
            "last_error": message,
        }
        self.proxy_states[pid] = state
        if self.db is not None and (status == "broken" or until is not None or prev is not None):
            self.db.set_proxy_failure(
                pid,
                sources,
                status=status,
                backoff_until=until,
                consecutive_failures=failures,
                last_error=message,
            )

        if status == "broken":
            logger.warning(
                "[%s] endpoint %s marked broken after %d failures (%s)",
                self.source,
                label,
                failures,
                message[:120],
            )
        elif until_dt is not None:
            logger.warning(
                "[%s] endpoint %s (%s); backing off %dm until %s",
                self.source,
                label,
                error_class,
                minutes,
                until_dt.isoformat(timespec="seconds"),
            )

        self.failed_endpoints_streak.add(pid)
        if not any(self.is_usable(ep) for ep in self.endpoints):
            self._trip_run(f"{error_class} ({message[:120]})")
        elif len(self.failed_endpoints_streak) >= self.systemic_trip_limit:
            self._trip_run(
                f"{len(self.failed_endpoints_streak)} distinct endpoints failed consecutively "
                f"({error_class}: {message[:120]})"
            )
        return error_class

    def on_error(self, exc: BaseException, cell: int | None = None) -> str:  # noqa: ARG002 - part of the source-guard callback signature
        """Fallback error hook for callers/tests outside SourceTraffic."""
        if getattr(exc, "_recorded_by_traffic", False):
            return classify_error(exc)
        if isinstance(exc, ValueError):
            error_class = classify_error(exc)
            self.errors.append({"class": error_class, "error": str(exc)[:400]})
            return error_class
        return self.on_request_error(exc, None)

    def _trip_run(self, reason: str) -> None:
        if self.tripped:
            return
        self.tripped = True
        self.trip_reason = reason
        logger.warning("[%s] stopping source for this run (%s)", self.source, reason)

    def cell_backoff(self, consecutive: int) -> str:
        """How long to sideline a single cell after `consecutive` bad outcomes."""
        index = min(max(consecutive - 1, 0), len(self.cell_backoff_minutes) - 1)
        minutes = self.cell_backoff_minutes[index]
        return (self._now() + timedelta(minutes=minutes)).isoformat()

    def summary(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "tripped": self.tripped,
            "reason": self.trip_reason,
            "searches_ok": self.searches_ok,
            "rate_limit_hits": self.rate_limit_hits,
            "pool": self.pool_counts(),
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
