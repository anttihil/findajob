"""Count and pace every source request through a shared per-run transport."""

import time
from collections.abc import Callable
from typing import Any

from findajob.search.guard import SourceCircuit
from findajob.search.policy import source_policy


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
    ) -> None:
        policy = source_policy(config, source)
        self.endpoints: list[str | None] = list(dict.fromkeys(proxies)) or [None]
        self.limit = int(policy["requests_per_proxy"]) * len(self.endpoints)
        self.interval = float(policy["request_interval_seconds"])
        self.circuit = circuit
        self.clock, self.sleep = clock, sleep
        self.counts = dict.fromkeys(self.endpoints, 0)
        self.last_request: dict[str | None, float] = {}
        self.sessions: dict[str | None, Any] = {}

    @property
    def spent(self) -> int:
        return sum(self.counts.values())

    @property
    def can_start_page(self) -> bool:
        return self.spent < self.limit and not self.circuit.is_open

    def least_used(self) -> str | None:
        return min(self.endpoints, key=lambda endpoint: self.counts[endpoint])

    def bound_session(self, endpoint: str | None) -> "PageSession":
        return PageSession(self, endpoint)

    def request(self, endpoint: str | None, method: str, url: str, **kwargs: Any) -> Any:
        self.circuit.before_request()
        if endpoint not in self.sessions:
            from jobspy.util import create_session

            # No hidden retries or redirects: each attempt is explicitly counted and paced.
            self.sessions[endpoint] = create_session(
                proxies=endpoint, is_tls=False, has_retry=False
            )
        previous = self.last_request.get(endpoint)
        if previous is not None:
            delay = self.interval - (self.clock() - previous)
            if delay > 0:
                self.sleep(delay)
        self.last_request[endpoint] = self.clock()
        self.counts[endpoint] += 1
        kwargs["allow_redirects"] = False
        response = self.sessions[endpoint].request(method, url, **kwargs)
        response.raise_for_status()
        if response.status_code != 200:
            raise RuntimeError(f"{self.circuit.source}: unexpected HTTP {response.status_code}")
        return response

    def close(self) -> None:
        for session in self.sessions.values():
            session.close()


class PageSession:
    """Small session facade for the pinned JobSpy parsers and our detail adapter."""

    def __init__(self, traffic: SourceTraffic, endpoint: str | None) -> None:
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
