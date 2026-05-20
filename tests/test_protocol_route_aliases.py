"""Canonical protocol surface alias wiring tests."""

from __future__ import annotations

import sys
import unittest
from collections import defaultdict
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402


class TestCanonicalProtocolRouteAliases(unittest.TestCase):
    def test_declared_aliases_are_registered(self) -> None:
        registered = {
            (method, route.path)
            for route in proxy.app.routes
            for method in getattr(route, "methods", set())
        }

        for entry in proxy.CANONICAL_PROTOCOL_ROUTE_ALIASES:
            for method in entry["methods"]:
                self.assertIn((method, entry["canonical"]), registered)
                self.assertIn((method, entry["alias"]), registered)

    def test_no_duplicate_method_path_registrations(self) -> None:
        routes_by_method_path = defaultdict(list)
        for route in proxy.app.routes:
            for method in getattr(route, "methods", set()):
                routes_by_method_path[(method, route.path)].append(route.name)

        duplicates = {
            key: names
            for key, names in routes_by_method_path.items()
            if len(names) > 1
        }
        self.assertEqual(duplicates, {})

    def test_unregistered_provider_native_paths_stay_not_found(self) -> None:
        client = TestClient(proxy.app)
        for path in (
            "/openai/v1/fine_tuning/jobs",
            "/anthropic/v1/models",
            "/ollama/api/unknown",
        ):
            response = client.get(path)
            self.assertEqual(response.status_code, 404, msg=path)
            self.assertEqual(response.json()["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
