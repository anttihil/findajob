"""Count, pace, and rotate every source request through a shared per-run transport."""

import contextlib
import random
import time
from collections.abc import Callable
from typing import Any

from findajob.search.guard import SourceCircuit, SourceTripped
from findajob.search.policy import source_policy

MAX_ENDPOINT_ATTEMPTS = 3
GLOBAL_MIN_INTERVAL_RANGE = (0.3, 0.5)


class SourceTraffic:
    def __init__(
        self,
        source: str,
        config: dict[str, Any],
        proxies: list[str],
        circuit: SourceCircuit,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        policy = source_policy(config, source)
        self._rng = rng or random.Random()
        self.endpoints: list[str | None] = list(dict.fromkeys(proxies)) or [None]
        if len(self.endpoints) > 1:
            self._rng.shuffle(self.endpoints)
        circuit.set_endpoints(self.endpoints)
        self.per_proxy_limit = int(policy["requests_per_proxy"])
        self.interval = float(policy["request_interval_seconds"])
        self.circuit = circuit
        self.clock, self.sleep = clock, sleep
        self.counts = dict.fromkeys(self.endpoints, 0)
        self.last_request: dict[str | None, float] = {}
        self.last_global_request: float | None = None
        self.sessions: dict[str | None, Any] = {}

    @property
    def limit(self) -> int:
        usable = sum(1 for ep in self.endpoints if self.circuit.is_usable(ep))
        return self.per_proxy_limit * usable

    @property
    def spent(self) -> int:
        return sum(self.counts.values())

    @property
    def can_start_page(self) -> bool:
        if self.circuit.is_open or self.per_proxy_limit <= 0:
            return False
        return any(
            self.circuit.is_usable(ep) and self.counts[ep] < self.per_proxy_limit
            for ep in self.endpoints
        )

    def least_used(self, exclude: set[str | None] | None = None) -> str | None:
        blocked = exclude or set()
        usable = [ep for ep in self.endpoints if ep not in blocked and self.circuit.is_usable(ep)]
        if not usable:
            return None
        under_budget = [ep for ep in usable if self.counts[ep] < self.per_proxy_limit]
        pool = under_budget or usable
        return min(pool, key=lambda endpoint: self.counts[endpoint])

    def bound_session(self, endpoint: str | None = None) -> "PageSession":
        return PageSession(self, endpoint)

    def _send_once(
        self, endpoint: str | None, method: str, url: str, kwargs: dict[str, Any]
    ) -> Any:
        if endpoint not in self.sessions:
            from jobspy.util import create_session

            # No hidden library retries or redirects: failover is explicitly counted here.
            self.sessions[endpoint] = create_session(
                proxies=endpoint, is_tls=False, has_retry=False
            )
        now = self.clock()
        delay = 0.0
        previous = self.last_request.get(endpoint)
        if previous is not None:
            delay = max(delay, self.interval - (now - previous))
        if self.interval > 0 and len(self.endpoints) > 1 and self.last_global_request is not None:
            global_gap = min(self.interval, self._rng.uniform(*GLOBAL_MIN_INTERVAL_RANGE))
            delay = max(delay, global_gap - (now - self.last_global_request))
        if delay > 0:
            self.sleep(delay)
        stamped = self.clock()
        self.last_request[endpoint] = stamped
        self.last_global_request = stamped
        self.counts[endpoint] += 1
        kwargs["allow_redirects"] = False
        response = self.sessions[endpoint].request(method, url, **kwargs)
        response.raise_for_status()
        if response.status_code != 200:
            raise RuntimeError(f"{self.circuit.source}: unexpected HTTP {response.status_code}")
        return response

    def request(self, endpoint: str | None, method: str, url: str, **kwargs: Any) -> Any:
        self.circuit.before_request()
        tried: set[str | None] = set()
        last_exc: BaseException | None = None
        for attempt in range(MAX_ENDPOINT_ATTEMPTS):
            if attempt == 0 and endpoint is not None and self.circuit.is_usable(endpoint):
                chosen: str | None = endpoint
                has_choice = True
            else:
                usable = [
                    ep for ep in self.endpoints if ep not in tried and self.circuit.is_usable(ep)
                ]
                has_choice = bool(usable)
                chosen = self.least_used(tried) if has_choice else None
            if not has_choice:
                break
            tried.add(chosen)
            try:
                response = self._send_once(chosen, method, url, dict(kwargs))
                self.circuit.on_request_success(chosen)
                return response
            except Exception as exc:
                last_exc = exc
                with contextlib.suppress(AttributeError):
                    exc._recorded_by_traffic = True  # type: ignore[attr-defined]
                self.circuit.on_request_error(exc, chosen)
                if self.circuit.is_open:
                    raise
        if last_exc is not None:
            raise last_exc
        raise SourceTripped(
            f"{self.circuit.source} has no usable endpoints: {self.circuit.trip_reason}"
        )

    def close(self) -> None:
        for session in self.sessions.values():
            session.close()


class PageSession:
    """Small session facade for the pinned JobSpy parsers and our detail adapter."""

    def __init__(self, traffic: SourceTraffic, endpoint: str | None = None) -> None:
        self.traffic, self.endpoint = traffic, endpoint
        self.headers: dict[str, str] = {}
        self.last_response: Any = None

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        kwargs["headers"] = {**self.headers, **kwargs.get("headers", {})}
        self.last_response = self.traffic.request(self.endpoint, method, url, **kwargs)
        return self.last_response

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        # The run owns reusable underlying sessions.
        pass
