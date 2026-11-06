"""Timeouts expire and cannot contribute to a permanent proxy failure streak."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from findajob.core.database import Database
from findajob.search.guard import ERROR_PROXY, ERROR_TIMEOUT, SourceCircuit, classify_error
from findajob.search.proxies import proxy_id

NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)
PROXY = "user:pass@10.0.0.1:8000"
OTHER = "user:pass@10.0.0.2:8000"
WRAPPED_TIMEOUT = (
    "ProxyError('Unable to connect to proxy', "
    "ConnectTimeoutError('Connection to proxy timed out. (connect timeout=10)'))"
)


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        RuntimeError("Read timed out"),
        RuntimeError("ReadTimeout"),
        RuntimeError(WRAPPED_TIMEOUT),
    ],
)
def test_timeout_classification(error: BaseException) -> None:
    assert classify_error(error) == ERROR_TIMEOUT


def test_hidden_nested_timeout_is_classified_without_overriding_authentication() -> None:
    wrapper = RuntimeError("Unable to connect to proxy")
    wrapper.__cause__ = TimeoutError()
    assert classify_error(wrapper) == ERROR_TIMEOUT
    wrapper = RuntimeError("407 Proxy Authentication Required (timeout while authenticating)")
    wrapper.__cause__ = TimeoutError()
    assert classify_error(wrapper) == ERROR_PROXY


@pytest.mark.parametrize(
    "message,shared",
    [
        (WRAPPED_TIMEOUT, True),
        ("ConnectTimeout", True),
        ("Read timed out", False),
        ("ProxyError: ReadTimeoutError: read timed out", False),
    ],
)
def test_timeout_backoff_persists_expires_and_caps_across_restarts(
    tmp_path: Path, message: str, shared: bool
) -> None:
    path = str(tmp_path / "jobs.db")
    now = NOW

    def clock() -> datetime:
        return now

    for count, minutes in enumerate([15, 60, 240, 240], 1):
        db = Database(path)
        try:
            circuit = SourceCircuit("linkedin", {}, db=db, now=clock, proxies=[PROXY, OTHER])
            assert circuit.is_usable(PROXY)
            circuit.on_request_error(RuntimeError(message), PROXY)
            assert not circuit.is_open  # The other endpoint still works.
            assert not circuit.is_usable(PROXY)
            rows = db.get_proxy_states("linkedin")
            assert rows[0]["failure_class"] == ERROR_TIMEOUT
            assert rows[0]["status"] == "backoff"
            assert rows[0]["consecutive_failures"] == count
            until = now + timedelta(minutes=minutes)
            assert rows[0]["backoff_until"] == until.isoformat()
            restarted = SourceCircuit("linkedin", {}, db=db, now=clock, proxies=[PROXY])
            assert restarted.persisted_backoff_active()
            indeed = SourceCircuit("indeed", {}, db=db, now=clock, proxies=[PROXY])
            assert indeed.is_usable(PROXY) is not shared
            now = until
            assert restarted.is_usable(PROXY)
        finally:
            db.close()

    db = Database(path)
    try:
        circuit = SourceCircuit("linkedin", {}, db=db, now=clock, proxies=[PROXY])
        circuit.on_request_success(PROXY)
        assert db.get_proxy_states("linkedin") == []
        circuit.on_request_error(RuntimeError(message), PROXY)
        assert db.get_proxy_states("linkedin")[0]["consecutive_failures"] == 1
        assert (
            db.get_proxy_states("linkedin")[0]["backoff_until"]
            == (now + timedelta(minutes=15)).isoformat()
        )
    finally:
        db.close()


def test_failure_categories_do_not_inherit_each_others_counts() -> None:
    db = Database(":memory:")
    try:
        now = NOW

        def clock() -> datetime:
            return now

        for message, expected_count in [
            ("429 Too Many Requests", 1),
            ("429 Too Many Requests", 2),
            (WRAPPED_TIMEOUT, 1),
            (WRAPPED_TIMEOUT, 2),
            ("407 Proxy Authentication Required", 1),
            ("407 Proxy Authentication Required", 2),
            (WRAPPED_TIMEOUT, 1),
            ("407 Proxy Authentication Required", 1),
        ]:
            circuit = SourceCircuit("linkedin", {}, db=db, now=clock, proxies=[PROXY, OTHER])
            circuit.on_request_error(RuntimeError(message), PROXY)
            row = next(
                r for r in db.get_proxy_states("linkedin") if r["proxy_id"] == proxy_id(PROXY)
            )
            assert row["consecutive_failures"] == expected_count
            assert row["status"] == "backoff"
            now += timedelta(days=2)
    finally:
        db.close()


def test_single_endpoint_timeout_stops_run_temporarily() -> None:
    circuit = SourceCircuit("linkedin", {}, now=lambda: NOW)
    circuit.on_request_error(TimeoutError(), None)
    assert circuit.is_open
    assert not circuit.is_usable(None)
    assert circuit.backoff_until() == (NOW + timedelta(minutes=15)).isoformat()
