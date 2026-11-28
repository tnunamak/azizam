"""A trellis service, an observer and a fake engine for tests of the 3D workflow's discovery and its multipart runner.

The service records are the real seeded data (``tests/fixtures/config_homes/deployment_config.yml``); only the transport is
fake. ``Harness.deps()`` is what the availability probe reads (``mesh_api.MeshApiDeps``); ``Harness.host()`` is what the
multipart runner reads (``multipart_adapter.MultipartHost``). Both share the harness's services, leases and engine.
"""

from __future__ import annotations

import copy
import inspect
from pathlib import Path
from typing import Any

import httpx
import yaml

from gateway import route_registry, service_registry
from gateway.exclusive_pool import ExclusivePoolLeaseManager
from gateway.mesh_api import MeshApiDeps, mesh_workflow
from tests.media_test_bytes import glb_bytes, png_bytes

ROOT = Path(__file__).resolve().parents[2]
PNG = png_bytes()
GLB = glb_bytes(b"model-bytes" * 8)
MIB = 1 << 20
TOTAL = 24 * 1024 * MIB
OBSERVER_URL = "http://vram-observer.test"
TRELLIS_URL = "http://127.0.0.1:8890"
PIXAL3D_URL = "http://127.0.0.1:8892"
PIXAL3D_GLB = glb_bytes(b"pixal-model" * 8)
GOOD_CAPACITY = {
    "enabled": True,
    "mode": "exclusive-route-pool",
    "pool_id": "gpu0",
    "max_claim_bytes": 8 * 1024 * MIB,
    "reserve_bytes": 512 * MIB,
    "retry_after_seconds": 30,
    "timeout_seconds": 5,
    "observer": {"service": "vram-observer", "path": "/facts", "free_pointer": "/free", "total_pointer": "/total"},
}


def proxy_config() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "tests/fixtures/config_homes/deployment_config.yml").read_text(encoding="utf-8"))


class Upstream(httpx.AsyncBaseTransport):
    """Routes requests to a sync or async handler (httpx's MockTransport awaits only sync results)."""

    def __init__(self, handler, hook=None) -> None:
        self.handler = handler
        self.hook = hook  # optional async hook that may stall before the body is read

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.hook is not None:
            await self.hook(request)
        await request.aread()
        result = self.handler(request)
        if inspect.isawaitable(result):
            result = await result
        return result


OWNER_MESH_MANIFEST = {
    "schema_version": 2,
    "min_gateway_version": "0.1",
    "version": "0.1.0",
    "id": "mesh.generate.owner-demo",
    "intent": "mesh.generate",
    "capability": "mesh",
    "display_name": "Owner image to 3D",
    "purpose": "One image to a textured 3D model",
    "description": "Turns one image into a textured 3D model in GLB format.",
    "summary": "Send one picture; receive one textured 3D model (model/gltf-binary).",
    "inputs": {
        "image": {"label": "picture", "type": "asset", "accepts": ["image/png", "image/jpeg", "image/webp"], "required": True},
        "seed": {"label": "variation number", "type": "integer", "minimum": 0, "maximum": 4294967295},
        "resolution": {"label": "detail", "type": "integer", "enum": [512, 1024, 1536], "default": 1024},
        "bg_removal": {"label": "background removal", "type": "string", "enum": ["threshold", "birefnet"]},
        "extensions": {"label": "extensions", "type": "object", "opaque": True},
    },
    "outputs": {"mesh": {"media_type": "model/gltf-binary"}},
    "bindings": {"http-multipart": {
        "retry": {"max_attempts": 11, "max_s": 30},
        "map": {"image": "image", "seed": "seed", "resolution": "resolution", "bg_removal": "bg_removal"},
        "service": "trellis", "path": "/generate"}},
}


OWNER_MESH_FACTS = dict(exposure="default", trust_tier="stable", resource_tier="batch", resource_class="media",
                        tags=(), selection_rank=2)


def add_mesh_manifest(registry, manifest, **facts) -> None:
    """Put a mesh workflow into a workflow registry as a bundled-reviewed record (a test stands in for the deployment that
    holds one; the release ships no mesh workflow)."""
    from gateway.discovery import BUNDLED_PROVENANCE
    from gateway.workflow_policy import PolicyFacts
    registry.add_manifest(copy.deepcopy(manifest), source_path=f"{manifest['id']}.yml", provenance=BUNDLED_PROVENANCE,
                          facts=PolicyFacts(**{**OWNER_MESH_FACTS, **facts}), replace=True)


def add_owner_mesh(registry) -> None:
    add_mesh_manifest(registry, OWNER_MESH_MANIFEST)


def rival_mesh_manifest() -> dict:
    """A second mesh workflow on the same service, for tests of which one an unnamed request runs."""
    return {**copy.deepcopy(OWNER_MESH_MANIFEST), "id": "mesh.generate.rival-demo", "display_name": "Rival image to 3D"}


def install_owner_mesh(monkeypatch) -> None:
    """The live registry and its snapshot serve the owner mesh workflow for this test (the conftest puts both back)."""
    import proxy
    from gateway import registry_snapshot
    add_owner_mesh(proxy.GATEWAY_WORKFLOW_REGISTRY)
    real = registry_snapshot.bundled_release

    def with_owner_mesh(**kwargs):
        release = real(**kwargs)
        scratch = type(proxy.GATEWAY_WORKFLOW_REGISTRY)()
        add_owner_mesh(scratch)
        return registry_snapshot.BundledRelease({**release.workflows, **{r.id: r for r in scratch.records()}},
                                                release.frontends)

    monkeypatch.setattr(registry_snapshot, "bundled_release", with_owner_mesh)
    proxy.GATEWAY_REGISTRY_PROVIDER.swap(proxy._build_registry_snapshot(proxy.config.get("model_aliases")))


class Harness:
    """Holds the scenario's mutable state: records, services and upstream behaviour."""

    def __init__(self) -> None:
        cfg = proxy_config()
        self.frontends = list(route_registry.protocol_frontend_records(registry=None, proxy_config=cfg))
        trellis_raw = copy.deepcopy(cfg["backends"]["trellis"])
        trellis_raw["resource_capacity"] = dict(GOOD_CAPACITY)
        pixal_raw = copy.deepcopy(cfg["backends"]["pixal3d"])
        pixal_raw["resource_capacity"] = dict(GOOD_CAPACITY)  # the same pool as trellis: both run on one card
        self.pixal_health_status = 200
        self.pixal_downstream: list[httpx.Request] = []
        self.services: dict[str, dict[str, Any]] = {
            "trellis": service_registry.service_from_backend_config("trellis", trellis_raw, source="provisioned"),
            "pixal3d": service_registry.service_from_backend_config("pixal3d", pixal_raw, source="provisioned"),
            "vram-observer": service_registry.service_from_backend_config(
                "vram-observer", {"base_url": OBSERVER_URL, "capabilities": []}, source="provisioned"),
        }
        self.downstream: list[httpx.Request] = []
        self.health_status = 200
        self.observer_body: Any = {"free": TOTAL, "total": TOTAL}
        self.observer_status = 200
        self.downstream_mode: Any = "ok"
        self.leases = ExclusivePoolLeaseManager()
        self.hook: Any = None  # stalls the upstream transport; see Upstream
        owner_workflow = mesh_workflow(OWNER_MESH_MANIFEST, "owner-mesh-fixture")
        self.current_manifest: Any = lambda: owner_workflow
        # Whether the live registry holds the workflow record (an owner override may turn it off).
        self.workflow_present = True

    def deps(self) -> MeshApiDeps:
        return MeshApiDeps(
            current_manifest=lambda: self.current_manifest(),
            frontend_records=lambda: self.frontends,
            services=lambda: self.services,
            client_factory=lambda: httpx.AsyncClient(transport=Upstream(self.handler, self.hook)),
            workflow_available=lambda: self.workflow_present,
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(OBSERVER_URL):
            if self.observer_status != 200:
                return httpx.Response(self.observer_status, content=b"nope")
            return httpx.Response(200, json=self.observer_body)
        if url == f"{TRELLIS_URL}/health":
            return httpx.Response(self.health_status, content=b"ok")
        if url == f"{PIXAL3D_URL}/health":
            return httpx.Response(self.pixal_health_status, content=b"ok")
        if url == f"{PIXAL3D_URL}/generate":
            self.pixal_downstream.append(request)
            return httpx.Response(200, content=PIXAL3D_GLB, headers={"content-type": "model/gltf-binary"})
        if url == f"{TRELLIS_URL}/generate":
            self.downstream.append(request)
            mode = self.downstream_mode
            if isinstance(mode, Exception):
                raise mode
            if mode == "ok":
                return httpx.Response(200, content=GLB, headers={"content-type": "model/gltf-binary"})
            return httpx.Response(mode, content=b'{"error":"internal secret"}')
        return httpx.Response(404)
