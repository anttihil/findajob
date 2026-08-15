"""Proxy pool loading, budget lifting, and per-cell pinning.

The pinning tests exist because the cost they address is invisible in a functional test:
handing JobSpy the whole pool works perfectly well, it is just slow, since it reassigns
session.proxies per request and so pays a fresh TLS handshake every time.
"""

import os
import sys
import unittest
from typing import Any, ClassVar

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.search.proxies import (
    ENV_VAR,
    apply_proxy_budgets,
    is_rotating,
    load_proxies,
    pin_for,
    redact,
)

POOL = [f"user:pass@10.0.0.{n}:8000" for n in range(1, 6)]


class LoadProxiesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.pop(ENV_VAR, None)

    def tearDown(self) -> None:
        os.environ.pop(ENV_VAR, None)
        if self._saved is not None:
            os.environ[ENV_VAR] = self._saved

    def test_unset_env_yields_no_proxies(self) -> None:
        self.assertEqual(load_proxies({}), [])

    def test_comma_separated_list_is_split_and_stripped(self) -> None:
        os.environ[ENV_VAR] = " a:b@h1:1 , a:b@h2:2 "
        self.assertEqual(load_proxies({}), ["a:b@h1:1", "a:b@h2:2"])

    def test_disabled_in_config_overrides_a_populated_env(self) -> None:
        os.environ[ENV_VAR] = "a:b@h1:1"
        config = {"scraper": {"proxies": {"enabled": False}}}
        self.assertEqual(load_proxies(config), [])

    def test_rotating_defaults_true(self) -> None:
        self.assertTrue(is_rotating({}))


class BudgetLiftTests(unittest.TestCase):
    CONFIG: ClassVar[dict[str, Any]] = {
        "budgets": {
            "linkedin": {
                "searches_per_run": 6,
                "fetch_descriptions": False,
                "desc_selection": "none",
            }
        },
        "proxies": {
            "with_proxies": {
                "linkedin": {
                    "searches_per_run": 12,
                    "fetch_descriptions": True,
                    "desc_selection": "census",
                }
            }
        },
    }

    def test_no_proxies_keeps_the_conservative_budget(self) -> None:
        result = apply_proxy_budgets(self.CONFIG, [])
        self.assertEqual(result["budgets"]["linkedin"]["searches_per_run"], 6)
        self.assertFalse(result["budgets"]["linkedin"]["fetch_descriptions"])

    def test_proxies_lift_linkedin_to_a_description_census(self) -> None:
        result = apply_proxy_budgets(self.CONFIG, POOL)
        self.assertEqual(result["budgets"]["linkedin"]["searches_per_run"], 12)
        self.assertEqual(result["budgets"]["linkedin"]["desc_selection"], "census")

    def test_original_config_is_not_mutated(self) -> None:
        apply_proxy_budgets(self.CONFIG, POOL)
        self.assertEqual(self.CONFIG["budgets"]["linkedin"]["searches_per_run"], 6)


class PinningTests(unittest.TestCase):
    def test_pin_returns_exactly_one_endpoint(self) -> None:
        """A single-element list is the point: cycle() over it never changes the proxy."""
        pinned = pin_for(POOL, key=3)
        self.assertEqual(len(pinned), 1)
        self.assertIn(pinned[0], POOL)

    def test_same_cell_and_attempt_is_stable(self) -> None:
        self.assertEqual(pin_for(POOL, key=3), pin_for(POOL, key=3))

    def test_different_cells_spread_across_the_pool(self) -> None:
        picked = {pin_for(POOL, key=k)[0] for k in range(len(POOL))}
        self.assertEqual(len(picked), len(POOL))

    def test_retry_moves_to_a_different_exit_ip(self) -> None:
        """The invariant the circuit breaker depends on: rotating past a flagged IP."""
        first = pin_for(POOL, key=2, attempt=0)[0]
        second = pin_for(POOL, key=2, attempt=1)[0]
        self.assertNotEqual(first, second)

    def test_empty_pool_stays_empty(self) -> None:
        self.assertEqual(pin_for([], key=1), [])

    def test_single_endpoint_pool_survives_retries(self) -> None:
        """No rotation available, but it must not crash or return an empty list."""
        self.assertEqual(pin_for(["only:one@h:1"], key=7, attempt=3), ["only:one@h:1"])


class RedactTests(unittest.TestCase):
    def test_credentials_are_stripped(self) -> None:
        self.assertEqual(redact("user:pass@host:7000"), "***@host:7000")

    def test_passthrough_without_credentials(self) -> None:
        self.assertEqual(redact("host:7000"), "host:7000")


if __name__ == "__main__":
    unittest.main()
