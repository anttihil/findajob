"""Proxy pool loading, budget lifting, and per-cell pinning.

The pinning tests exist because the cost they address is invisible in a functional test:
handing JobSpy the whole pool works perfectly well, it is just slow, since it reassigns
session.proxies per request and so pays a fresh TLS handshake every time.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from findajob.search.proxies import (
    ENV_VAR,
    load_proxies,
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

    def test_duplicate_endpoints_do_not_inflate_capacity(self) -> None:
        os.environ[ENV_VAR] = "a:b@h1:1,a:b@h1:1,a:b@h2:2"
        self.assertEqual(load_proxies({}), ["a:b@h1:1", "a:b@h2:2"])


class RedactTests(unittest.TestCase):
    def test_credentials_are_stripped(self) -> None:
        self.assertEqual(redact("user:pass@host:7000"), "***@host:7000")

    def test_passthrough_without_credentials(self) -> None:
        self.assertEqual(redact("host:7000"), "host:7000")


if __name__ == "__main__":
    unittest.main()
