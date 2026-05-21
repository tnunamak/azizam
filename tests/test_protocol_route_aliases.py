"""Canonical protocol surface alias wiring tests."""

from __future__ import annotations

import sys
import unittest
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

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
            "/anthropic/v1/unknown",
            "/ollama/api/unknown",
        ):
            response = client.get(path)
            self.assertEqual(response.status_code, 404, msg=path)
            self.assertEqual(response.json()["error"]["code"], "not_found")

    def test_anthropic_models_has_anthropic_shape_without_v1_models_collision(self) -> None:
        client = TestClient(proxy.app)
        with patch.object(
            proxy,
            "ROLES",
            {
                "claude-local": {
                    "backend": "kobold",
                    "model": "/tmp/claude-local.gguf",
                    "description": "Claude Local",
                }
            },
        ), patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy, "MODELS_CATALOG", {}
        ), patch.object(
            proxy, "currently_loaded_kobold_model", lambda: None
        ):
            response = client.get("/anthropic/v1/models")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["object"], "list")
        self.assertEqual(body["data"][0]["id"], "claude-local")
        self.assertEqual(body["data"][0]["type"], "model")
        self.assertIn("created_at", body["data"][0])
        self.assertNotIn(
            ("GET", "/v1/models"),
            {
                (method, entry["canonical"])
                for entry in proxy.CANONICAL_PROTOCOL_ROUTE_ALIASES
                for method in entry["methods"]
                if entry.get("alias") == "/anthropic/v1/models"
            },
        )

    def test_anthropic_count_tokens_has_anthropic_shape(self) -> None:
        client = TestClient(proxy.app)
        with patch.object(proxy, "AUTH_ENABLED", False):
            response = client.post(
                "/anthropic/v1/messages/count_tokens",
                json={
                    "model": "claude-local",
                    "messages": [{"role": "user", "content": "hello world"}],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"input_tokens"})
        self.assertGreater(response.json()["input_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
