"""Tests for the read-only ``/gateway/v1`` discovery router."""

from __future__ import annotations

import importlib
import sys
import textwrap
import unittest
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.api import create_gateway_router  # noqa: E402
from gateway.discovery import WorkflowRegistry  # noqa: E402


CONTROLNET_MANIFEST_YAML = textwrap.dedent(
    """
    schema_version: 1
    min_gateway_version: "0.1"
    version: "0.1.0"
    id: image.edit.controlnet.example
    intent: image.edit
    display_name: Example Controlnet Edit
    description: Test workflow used to exercise the router.
    source: local
    trusted: true
    inputs:
      prompt:
        type: string
        required: true
      denoise:
        type: number
        minimum: 0
        maximum: 1
        default: 0.5
    outputs:
      image:
        media_type: image/png
        description: Edited image.
    bindings:
      comfyui:
        workflow: workflows/images/example-controlnet.json
        map:
          prompt:
            - node: "6"
              input: text
          denoise:
            - node: "31"
              input: denoise
    """
).strip()


SIMPLE_MANIFEST_YAML = textwrap.dedent(
    """
    schema_version: 1
    version: "0.2.0"
    id: image.generate.simple.example
    intent: image.generate
    source: local
    trusted: false
    inputs:
      prompt:
        type: string
        required: true
    outputs:
      image:
        media_type: image/png
    bindings:
      comfyui:
        workflow: workflows/images/simple.json
        map:
          prompt:
            - node: "6"
              input: text
    """
).strip()


def _build_registry() -> WorkflowRegistry:
    registry = WorkflowRegistry()
    registry.add_manifest(
        yaml.safe_load(CONTROLNET_MANIFEST_YAML),
        source_path="/srv/openai-proxy/manifests/workflows/controlnet.yml",
    )
    registry.add_manifest(
        yaml.safe_load(SIMPLE_MANIFEST_YAML),
        source_path="/srv/openai-proxy/manifests/workflows/simple.yml",
    )
    return registry


def _make_client(audience: str | None = None) -> TestClient:
    registry = _build_registry()
    resolver = None
    if audience is not None:
        resolver = lambda _request, _a=audience: _a  # noqa: E731
    app = FastAPI()
    app.include_router(create_gateway_router(registry, audience_resolver=resolver))
    return TestClient(app)


class GatewayDiscoveryRouterTests(unittest.TestCase):
    def test_list_workflows_default_client_audience(self) -> None:
        client = _make_client()
        response = client.get("/gateway/v1/workflows")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["schema_version"], 1)
        self.assertEqual(body["audience"], "client")
        ids = [w["id"] for w in body["workflows"]]
        self.assertEqual(
            ids,
            [
                "image.edit.controlnet.example",
                "image.generate.simple.example",
            ],
        )

    def test_get_workflow_returns_client_summary(self) -> None:
        client = _make_client()
        response = client.get(
            "/gateway/v1/workflows/image.edit.controlnet.example"
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["id"], "image.edit.controlnet.example")
        self.assertEqual(body["intent"], "image.edit")
        self.assertEqual(body["supported_backends"], ["comfyui"])

    def test_unknown_workflow_returns_protocol_404(self) -> None:
        client = _make_client()
        response = client.get("/gateway/v1/workflows/does.not.exist")
        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertIn("error", body)
        self.assertEqual(body["error"]["code"], "workflow_not_found")
        self.assertIn("does.not.exist", body["error"]["message"])

    def test_client_audience_does_not_leak_filesystem_or_bindings(self) -> None:
        client = _make_client()
        responses = [
            client.get("/gateway/v1/workflows"),
            client.get("/gateway/v1/workflows/image.edit.controlnet.example"),
        ]
        for response in responses:
            self.assertEqual(response.status_code, 200)
            raw = response.text
            self.assertNotIn("/srv/openai-proxy/manifests", raw)
            self.assertNotIn("source_path", raw)
            self.assertNotIn("manifest_hash", raw)
            self.assertNotIn("bindings", raw)
            self.assertNotIn(
                "workflows/images/example-controlnet.json", raw
            )

    def test_admin_audience_exposes_provenance_when_resolver_grants_it(
        self,
    ) -> None:
        client = _make_client(audience="admin")
        response = client.get(
            "/gateway/v1/workflows/image.edit.controlnet.example"
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["manifest_hash"]), 64)
        self.assertEqual(body["source"], "local")
        self.assertTrue(body["trusted"])
        self.assertTrue(body["source_path"].endswith("controlnet.yml"))
        binding = body["bindings"][0]
        self.assertEqual(binding["backend"], "comfyui")
        self.assertEqual(
            binding["workflow_artifact"],
            "workflows/images/example-controlnet.json",
        )
        self.assertNotIn("map", binding)

    def test_internal_audience_includes_raw_binding_map(self) -> None:
        client = _make_client(audience="internal")
        response = client.get(
            "/gateway/v1/workflows/image.edit.controlnet.example"
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        binding = body["bindings"][0]
        self.assertEqual(binding["map"]["prompt"][0]["node"], "6")

    def test_invalid_audience_falls_back_to_client(self) -> None:
        client = _make_client(audience="superuser")
        response = client.get("/gateway/v1/workflows")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["audience"], "client")
        raw = response.text
        self.assertNotIn("manifest_hash", raw)
        self.assertNotIn("source_path", raw)

    def test_router_does_not_import_proxy(self) -> None:
        # Importing the api module must not pull in proxy.py. We snapshot the
        # sys.modules state, drop any cached copy, re-import, and assert proxy
        # is still not present afterwards.
        sys.modules.pop("proxy", None)
        sys.modules.pop("gateway.api", None)
        importlib.import_module("gateway.api")
        self.assertNotIn("proxy", sys.modules)

    def test_custom_prefix_is_honored(self) -> None:
        registry = _build_registry()
        app = FastAPI()
        app.include_router(
            create_gateway_router(registry, prefix="/custom/v1")
        )
        client = TestClient(app)
        response = client.get("/custom/v1/workflows")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["audience"], "client")

    def test_audience_resolver_receives_request(self) -> None:
        seen: list[str] = []

        def resolver(request) -> str:
            seen.append(request.headers.get("x-audience", "client"))
            return seen[-1]

        registry = _build_registry()
        app = FastAPI()
        app.include_router(
            create_gateway_router(registry, audience_resolver=resolver)
        )
        client = TestClient(app)
        response = client.get(
            "/gateway/v1/workflows", headers={"x-audience": "admin"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen, ["admin"])
        self.assertEqual(response.json()["audience"], "admin")


if __name__ == "__main__":
    unittest.main(verbosity=2)
