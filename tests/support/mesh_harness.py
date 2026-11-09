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

import proxy
from gateway import route_registry, service_registry
from gateway.exclusive_pool import ExclusivePoolLeaseManager
from gateway.mesh_api import MeshApiDeps

ROOT = Path(__file__).resolve().parents[2]
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128
GLB = b"glTF\x02\x00\x00\x00" + b"model-bytes" * 8
MIB = 1 << 20
TOTAL = 24 * 1024 * MIB
OBSERVER_URL = "http://vram-observer.test"
TRELLIS_URL = "http://127.0.0.1:8890"
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


class Harness:
    """Holds the scenario's mutable state: records, services and upstream behaviour."""

    def __init__(self) -> None:
        cfg = proxy_config()
        self.frontends = list(route_registry.protocol_frontend_records(registry=None, proxy_config=cfg))
        trellis_raw = copy.deepcopy(cfg["backends"]["trellis"])
        trellis_raw["resource_capacity"] = dict(GOOD_CAPACITY)
        self.services: dict[str, dict[str, Any]] = {
            "trellis": service_registry.service_from_backend_config("trellis", trellis_raw, source="provisioned"),
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
        self.current_manifest: Any = lambda: proxy.current_mesh_workflow()
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
        if url == f"{TRELLIS_URL}/generate":
            self.downstream.append(request)
            mode = self.downstream_mode
            if isinstance(mode, Exception):
                raise mode
            if mode == "ok":
                return httpx.Response(200, content=GLB, headers={"content-type": "model/gltf-binary"})
            return httpx.Response(mode, content=b'{"error":"internal secret"}')
        return httpx.Response(404)
