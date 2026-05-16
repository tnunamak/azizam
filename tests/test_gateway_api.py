"""Tests for the read-only ``/gateway/v1`` discovery router."""

from __future__ import annotations

import importlib
import sys
import textwrap
import unittest
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import receipt_store  # noqa: E402
from gateway.api import create_gateway_router  # noqa: E402
from gateway.capabilities import (  # noqa: E402
    Capability,
    ResourceEstimate,
    unavailable_capability,
)
from gateway.discovery import WorkflowRegistry  # noqa: E402
from gateway.receipts import RouteReceipt  # noqa: E402


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

    def test_audience_resolver_error_falls_back_to_client(self) -> None:
        def resolver(_request) -> str:
            raise RuntimeError("resolver should not leak")

        registry = _build_registry()
        app = FastAPI()
        app.include_router(
            create_gateway_router(registry, audience_resolver=resolver)
        )
        client = TestClient(app)
        response = client.get("/gateway/v1/workflows")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["audience"], "client")
        self.assertNotIn("resolver should not leak", response.text)


def _sample_capabilities() -> list[Capability]:
    return [
        Capability(
            intent="image.edit",
            backend="comfyui.local",
            model="image-controlnet",
            workflow="image.edit.controlnet.example",
            input_modalities=("text", "image"),
            output_modalities=("image",),
            execution="job",
            lifecycle="warm",
            health="healthy",
            resources=ResourceEstimate(gpu_required=True, vram_gb_estimate=12),
            caveats=("aliases: controlnet-alias",),
            policy_sources=("profile:image-agent",),
            policy_reasons=("workflow permitted",),
        ),
        unavailable_capability(
            intent="video.generate",
            backend="comfyui.video",
            reason="backend not configured",
        ),
    ]


def _make_capabilities_client(
    *,
    audience: str | None = None,
    provider=None,
) -> TestClient:
    registry = _build_registry()
    resolver = None
    if audience is not None:
        resolver = lambda _request, _a=audience: _a  # noqa: E731
    app = FastAPI()
    app.include_router(
        create_gateway_router(
            registry,
            audience_resolver=resolver,
            capabilities_provider=provider,
        )
    )
    return TestClient(app)


class GatewayCapabilitiesEndpointTests(unittest.TestCase):
    def test_missing_provider_returns_empty_catalog(self) -> None:
        client = _make_capabilities_client()
        response = client.get("/gateway/v1/capabilities")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["schema_version"], 1)
        self.assertEqual(body["audience"], "client")
        self.assertEqual(body["capabilities"], [])

    def test_provider_receives_request_and_returns_caps(self) -> None:
        seen: list[str] = []

        def provider(request):
            seen.append(request.headers.get("x-trace", ""))
            return _sample_capabilities()

        client = _make_capabilities_client(provider=provider)
        response = client.get(
            "/gateway/v1/capabilities", headers={"x-trace": "abc"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen, ["abc"])
        body = response.json()
        intents = sorted({c["intent"] for c in body["capabilities"]})
        self.assertEqual(intents, ["image.edit", "video.generate"])

    def test_client_audience_hides_backend_and_policy_internals(self) -> None:
        client = _make_capabilities_client(provider=lambda _r: _sample_capabilities())
        response = client.get("/gateway/v1/capabilities")
        self.assertEqual(response.status_code, 200)
        raw = response.text
        for forbidden in (
            "comfyui.local",
            "comfyui.video",
            "profile:image-agent",
            "workflow permitted",
            "vram_gb_estimate",
            "policy_sources",
            "policy_reasons",
            "policy_available",
            "lifecycle",
            "health",
            "resources",
        ):
            self.assertNotIn(forbidden, raw, f"client leaked {forbidden!r}")

    def test_admin_audience_includes_backend_and_resources(self) -> None:
        client = _make_capabilities_client(
            audience="admin",
            provider=lambda _r: _sample_capabilities(),
        )
        response = client.get("/gateway/v1/capabilities")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        first = body["capabilities"][0]
        self.assertEqual(first["backend"], "comfyui.local")
        self.assertEqual(first["resources"]["gpu_required"], True)
        self.assertEqual(first["policy_sources"], ["profile:image-agent"])
        self.assertNotIn("policy_reasons", first)

    def test_internal_audience_exposes_policy_reasons(self) -> None:
        client = _make_capabilities_client(
            audience="internal",
            provider=lambda _r: _sample_capabilities(),
        )
        response = client.get("/gateway/v1/capabilities")
        body = response.json()
        first = body["capabilities"][0]
        self.assertEqual(first["policy_reasons"], ["workflow permitted"])

    def test_provider_error_is_protocol_shaped(self) -> None:
        def provider(_request):
            raise RuntimeError("provider boom")

        client = _make_capabilities_client(provider=provider)
        response = client.get("/gateway/v1/capabilities")
        self.assertEqual(response.status_code, 500)
        body = response.json()
        self.assertEqual(body["error"]["code"], "gateway_internal_error")
        # Client audience must not leak the raw provider error message.
        self.assertNotIn("provider boom", response.text)

    def test_provider_error_message_visible_to_admin(self) -> None:
        def provider(_request):
            raise RuntimeError("provider boom")

        client = _make_capabilities_client(provider=provider, audience="admin")
        response = client.get("/gateway/v1/capabilities")
        self.assertEqual(response.status_code, 500)
        self.assertIn("provider boom", response.text)

    def test_model_capabilities_filters_by_model(self) -> None:
        client = _make_capabilities_client(provider=lambda _r: _sample_capabilities())
        response = client.get("/gateway/v1/models/image-controlnet/capabilities")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["audience"], "client")
        self.assertEqual(len(body["capabilities"]), 1)
        self.assertEqual(body["capabilities"][0]["model"], "image-controlnet")

    def test_model_capabilities_filters_by_workflow_alias(self) -> None:
        client = _make_capabilities_client(provider=lambda _r: _sample_capabilities())
        response = client.get("/gateway/v1/models/controlnet-alias/capabilities")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["capabilities"]), 1)
        self.assertEqual(
            body["capabilities"][0]["workflow"],
            "image.edit.controlnet.example",
        )

    def test_model_capabilities_keeps_admin_audience_filtering(self) -> None:
        client = _make_capabilities_client(
            audience="admin",
            provider=lambda _r: _sample_capabilities(),
        )
        response = client.get("/gateway/v1/models/image-controlnet/capabilities")

        self.assertEqual(response.status_code, 200)
        first = response.json()["capabilities"][0]
        self.assertEqual(first["backend"], "comfyui.local")
        self.assertNotIn("policy_reasons", first)

    def test_extensions_lists_namespaces_and_route_targets(self) -> None:
        client = _make_capabilities_client()
        response = client.get("/gateway/v1/extensions")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["audience"], "client")
        by_namespace = {item["namespace"]: item for item in body["extensions"]}
        self.assertEqual(
            by_namespace["com.vividfish.workflow"]["namespace"],
            "com.vividfish.workflow",
        )
        routes = by_namespace["com.vividfish.workflow"]["routes"]
        route_targets = {route["route"]: route["targets"] for route in routes}
        self.assertIn("/v1/images/edits", route_targets)
        edit_target = route_targets["/v1/images/edits"][0]
        self.assertEqual(edit_target["intent"], "image.edit")
        self.assertEqual(edit_target["backends"], ["comfyui"])
        self.assertEqual(
            edit_target["workflows"][0]["controls"],
            ["denoise"],
        )
        generate_intents = {
            target["intent"] for target in route_targets["/v1/images/generations"]
        }
        self.assertNotIn("image.edit", generate_intents)
        self.assertEqual(
            by_namespace["com.vividfish.chat"]["routes"][0]["controls"],
            ["receipt_verbosity"],
        )
        self.assertEqual(
            by_namespace["com.vividfish.routing"]["routes"][0]["controls"],
            ["local_preference"],
        )
        self.assertNotIn("/srv/openai-proxy", response.text)


def _make_receipts_client(
    *,
    audience: str | None = None,
    receipt_store_path: Path | None = None,
    receipt_getter=None,
    receipt_lister=None,
) -> TestClient:
    registry = _build_registry()
    resolver = None
    if audience is not None:
        resolver = lambda _request, _a=audience: _a  # noqa: E731
    app = FastAPI()
    app.include_router(
        create_gateway_router(
            registry,
            audience_resolver=resolver,
            receipt_store_path=receipt_store_path,
            receipt_getter=receipt_getter,
            receipt_lister=receipt_lister,
        )
    )
    return TestClient(app)


def _seed_receipt(db: Path, **overrides) -> RouteReceipt:
    r = RouteReceipt(
        protocol="openai",
        intent="chat.completions",
        path="/v1/chat/completions",
        app_profile="default",
        api_key_name="alice",
        requested_model="gpt-4o",
        resolved_model="gpt-4o-mini",
        backend="openai.local",
    )
    r.accept_input("messages")
    r.set_policy_decision("allowed", reason="profile-default", source="config")
    r.finish("ok", latency_ms=42.0)
    r.metadata["internal_note"] = "ops eyes only"
    for k, v in overrides.items():
        setattr(r, k, v)
    receipt_store.record(r, path=db)
    return r


class GatewayReceiptsEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "receipts.db"
        receipt_store.init_db(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_providers_returns_empty_list(self) -> None:
        client = _make_receipts_client()
        response = client.get("/gateway/v1/receipts")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["schema_version"], 1)
        self.assertEqual(body["audience"], "client")
        self.assertEqual(body["receipts"], [])

    def test_missing_provider_get_returns_protocol_404(self) -> None:
        client = _make_receipts_client()
        response = client.get("/gateway/v1/receipts/req_anything")
        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertEqual(body["error"]["code"], "receipt_not_found")
        self.assertIn("req_anything", body["error"]["message"])

    def test_store_path_wires_durable_store(self) -> None:
        seeded = _seed_receipt(self.db)
        client = _make_receipts_client(receipt_store_path=self.db)

        listing = client.get("/gateway/v1/receipts")
        self.assertEqual(listing.status_code, 200)
        body = listing.json()
        self.assertEqual(len(body["receipts"]), 1)
        self.assertEqual(body["receipts"][0]["request_id"], seeded.request_id)

        single = client.get(f"/gateway/v1/receipts/{seeded.request_id}")
        self.assertEqual(single.status_code, 200)
        self.assertEqual(single.json()["request_id"], seeded.request_id)

    def test_client_audience_redacts_internal_fields(self) -> None:
        seeded = _seed_receipt(self.db)
        client = _make_receipts_client(receipt_store_path=self.db)

        for url in (
            "/gateway/v1/receipts",
            f"/gateway/v1/receipts/{seeded.request_id}",
        ):
            response = client.get(url)
            self.assertEqual(response.status_code, 200)
            raw = response.text
            for forbidden in (
                "ops eyes only",
                "openai.local",
                "metadata",
                "backend",
                "api_key_name",
                "alice",
                "policy_sources",
                "policy_reasons",
                "manifest_hash",
                "app_profile",
            ):
                self.assertNotIn(
                    forbidden, raw, f"client receipts leaked {forbidden!r} at {url}"
                )

    def test_admin_audience_sees_backend_but_not_metadata(self) -> None:
        seeded = _seed_receipt(self.db)
        client = _make_receipts_client(
            audience="admin", receipt_store_path=self.db
        )
        response = client.get(f"/gateway/v1/receipts/{seeded.request_id}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["backend"], "openai.local")
        self.assertNotIn("metadata", body)
        self.assertNotIn("ops eyes only", response.text)

    def test_internal_audience_sees_metadata(self) -> None:
        seeded = _seed_receipt(self.db)
        client = _make_receipts_client(
            audience="internal", receipt_store_path=self.db
        )
        response = client.get(f"/gateway/v1/receipts/{seeded.request_id}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["metadata"], {"internal_note": "ops eyes only"})

    def test_unknown_receipt_returns_protocol_404(self) -> None:
        client = _make_receipts_client(receipt_store_path=self.db)
        response = client.get("/gateway/v1/receipts/req_missing")
        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertEqual(body["error"]["code"], "receipt_not_found")

    def test_list_respects_limit_and_filters(self) -> None:
        import time

        now = int(time.time())
        ids = []
        for i in range(3):
            r = RouteReceipt(
                protocol="openai",
                intent="chat.completions",
                app_profile="alpha" if i < 2 else "beta",
            )
            r.finish("ok")
            receipt_store.record(r, created_at=now + i, path=self.db)
            ids.append(r.request_id)

        # Use admin so app_profile filter remains meaningful (client audience
        # strips app_profile from output, but the filter still applies).
        client = _make_receipts_client(
            audience="admin", receipt_store_path=self.db
        )
        response = client.get(
            "/gateway/v1/receipts",
            params={"limit": 5, "app_profile": "alpha"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        returned = {row["request_id"] for row in body["receipts"]}
        self.assertEqual(returned, {ids[0], ids[1]})

    def test_explicit_providers_override_store_path(self) -> None:
        captured: dict[str, Any] = {}

        def lister(*, audience, limit, app_profile=None, since_s=None):
            captured["audience"] = audience
            captured["limit"] = limit
            return [{"request_id": "stub", "status": "ok"}]

        def getter(request_id, *, audience):
            captured["get_audience"] = audience
            return {"request_id": request_id, "status": "ok"}

        client = _make_receipts_client(
            receipt_store_path=self.db,
            receipt_getter=getter,
            receipt_lister=lister,
        )
        listing = client.get("/gateway/v1/receipts", params={"limit": 7})
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(captured["limit"], 7)
        self.assertEqual(captured["audience"], "client")
        self.assertEqual(listing.json()["receipts"][0]["request_id"], "stub")

        single = client.get("/gateway/v1/receipts/whatever")
        self.assertEqual(single.status_code, 200)
        self.assertEqual(single.json()["request_id"], "whatever")

    def test_list_provider_error_is_protocol_shaped(self) -> None:
        def lister(**_kwargs):
            raise RuntimeError("lister boom")

        client = _make_receipts_client(receipt_lister=lister)
        response = client.get("/gateway/v1/receipts")
        self.assertEqual(response.status_code, 500)
        body = response.json()
        self.assertEqual(body["error"]["code"], "gateway_internal_error")
        self.assertNotIn("lister boom", response.text)

    def test_limit_validation_rejects_zero(self) -> None:
        client = _make_receipts_client(receipt_store_path=self.db)
        response = client.get("/gateway/v1/receipts", params={"limit": 0})
        # FastAPI validation kicks in before the handler.
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main(verbosity=2)
