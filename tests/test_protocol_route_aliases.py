"""Canonical protocol surface alias wiring tests."""

from __future__ import annotations

import json
import sys
import unittest
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.routing import Match

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from tests.support.access import authenticated  # noqa: E402


def _iter_routes(routes):
    for route in routes:
        yield route
        yield from _iter_routes(getattr(route, "routes", ()))


def _first_full_route_name(method: str, path: str) -> str | None:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "root_path": "",
    }
    for route in _iter_routes(proxy.app.routes):
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return route.name
    return None


class TestCanonicalProtocolRouteAliases(unittest.TestCase):
    def test_declared_aliases_are_registered(self) -> None:
        registered = {
            (method, route.path)
            for route in _iter_routes(proxy.app.routes)
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
        # Routing-layer classification (below auth): disable auth to reach it,
        # matching the convention used by the other routing tests in this file.
        # The unauthenticated-client posture (uniform 401, no enumeration oracle)
        # is asserted in test_discovery_metadata.py.
        client = TestClient(proxy.app)
        with authenticated():
            for path in (
                "/anthropic/v1/unknown",
                "/ollama/api/unknown",
            ):
                response = client.get(path)
                self.assertEqual(response.status_code, 404, msg=path)
                self.assertEqual(response.json()["error"]["code"], "not_found")

    def test_protocol_namespace_route_ownership_matrix(self) -> None:
        cases = (
            ("GET", "/v1/models", "list_models"),
            ("GET", "/openai/v1/models", "list_models"),
            ("GET", "/anthropic/v1/models", "anthropic_models"),
            (
                "POST",
                "/anthropic/v1/messages/count_tokens",
                "anthropic_count_tokens",
            ),
            ("POST", "/ollama/api/chat", "ollama_chat"),
            ("POST", "/api/chat", "ollama_chat"),
            ("POST", "/ollama/api/generate", "ollama_generate"),
            ("POST", "/api/generate", "ollama_generate"),
            ("POST", "/ollama/api/embed", "ollama_embed"),
            ("POST", "/api/embed", "ollama_embed"),
            ("POST", "/ollama/api/embeddings", "ollama_embeddings"),
            ("POST", "/api/embeddings", "ollama_embeddings"),
            ("GET", "/ollama/api/tags", "ollama_tags"),
            ("GET", "/api/tags", "ollama_tags"),
            ("POST", "/ollama/api/show", "ollama_show"),
            ("POST", "/api/show", "ollama_show"),
            ("GET", "/ollama/api/ps", "ollama_ps"),
            ("GET", "/api/ps", "ollama_ps"),
            ("GET", "/ollama/api/version", "ollama_version"),
            ("GET", "/api/version", "ollama_version"),
            ("GET", "/openai/v1/fine_tuning/jobs", "openai_fine_tuning_unsupported"),
            ("GET", "/v1/fine_tuning/jobs", "openai_fine_tuning_unsupported"),
            ("POST", "/ollama/api/create", "ollama_create"),
            ("POST", "/api/create", "ollama_create"),
            ("POST", "/ollama/api/copy", "ollama_copy"),
            ("POST", "/api/copy", "ollama_copy"),
            ("POST", "/ollama/api/pull", "ollama_pull"),
            ("POST", "/api/pull", "ollama_pull"),
            ("POST", "/ollama/api/push", "ollama_push"),
            ("POST", "/api/push", "ollama_push"),
            ("DELETE", "/ollama/api/delete", "ollama_delete"),
            ("DELETE", "/api/delete", "ollama_delete"),
            ("GET", "/gateway/v1/providers/tabby/v1/models", "provider_native_request"),
            ("POST", "/gateway/v1/providers/tabby/v1/chat/completions", "provider_native_request"),
            ("PATCH", "/gateway/v1/providers/tabby/custom", "provider_native_request"),
        )

        for method, path, expected_name in cases:
            with self.subTest(method=method, path=path):
                self.assertEqual(_first_full_route_name(method, path), expected_name)

    def test_provider_native_passthrough_is_confined_to_gateway_provider_namespace(self) -> None:
        provider_native_routes = {
            (method, route.path)
            for route in _iter_routes(proxy.app.routes)
            if getattr(route, "name", None) == "provider_native_request"
            for method in getattr(route, "methods", set())
        }

        self.assertEqual(
            provider_native_routes,
            {
                (method, "/gateway/v1/providers/{provider}/{native_path:path}")
                for method in {"GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"}
            },
        )
        self.assertNotEqual(
            _first_full_route_name("GET", "/v1/models"),
            "provider_native_request",
        )
        self.assertNotEqual(
            _first_full_route_name("GET", "/anthropic/v1/models"),
            "provider_native_request",
        )
        self.assertNotEqual(
            _first_full_route_name("GET", "/ollama/api/tags"),
            "provider_native_request",
        )

    def test_unknown_protocol_namespace_routes_return_not_found_without_backend_passthrough(self) -> None:
        client = TestClient(proxy.app)
        cases = (
            ("GET", "/v1/unknown"),
            ("POST", "/v1/unknown"),
            ("GET", "/openai/v1/unknown"),
            ("POST", "/openai/v1/unknown"),
            ("POST", "/anthropic/v1/unknown"),
            ("GET", "/ollama/api"),
            ("POST", "/ollama/api/unknown"),
            ("GET", "/api"),
            ("POST", "/api/unknown"),
            ("GET", "/gateway/v1/unknown"),
            ("POST", "/gateway/v1/providers"),
        )

        with authenticated():
            for method, path in cases:
                with self.subTest(method=method, path=path):
                    response = client.request(method, path)
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(response.json()["error"]["code"], "not_found")

    def test_anthropic_models_has_anthropic_shape_without_v1_models_collision(self) -> None:
        client = TestClient(proxy.app)

        class _Client:
            async def aclose(self):
                return None

        class _Response:
            status_code = 200
            headers = {}

            async def aread(self):
                return json.dumps({
                    "object": "list",
                    "data": [
                        {
                            "id": "claude-local",
                            "object": "model",
                            "owned_by": "litellm",
                            "created": 1,
                        }
                    ],
                }).encode()

            async def aclose(self):
                return None

        def fake_registry():
            return {
                "services": {
                    "litellm": {
                        "id": "litellm",
                        "kind": "llm-substrate",
                        "enabled": True,
                        "capabilities": ["llm.execution"],
                        "config": {"base_url": "http://127.0.0.1:4011"},
                    }
                }
            }

        async def fake_send(_execution_request, *, disconnect_watcher=None, cancellation_token=None, on_signal=None):
            return _Client(), _Response()

        with authenticated(), patch.object(
            proxy.ADMIN_STORE, "get_service_registry", fake_registry
        ), patch.object(
            proxy, "send_llm_execution_request", fake_send
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
        with authenticated():
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
