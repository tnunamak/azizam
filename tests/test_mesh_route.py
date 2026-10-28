"""POST /v1/meshes/generate end to end inside the process: fail-closed policy, typed outcomes, cleanup.

The frontend record and the trellis service record are the real seeded data (proxy_config.yml and
registry/protocol_frontends.yml); only auth, receipts, the observer and the downstream are fakes.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from gateway import route_registry, service_registry
from gateway.exclusive_pool import ExclusivePoolLeaseManager
from gateway.mesh_api import MESH_PATH, MeshApiDeps, register_mesh_routes
from gateway.mesh_manifest import load_mesh_manifest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = load_mesh_manifest(ROOT / "manifests" / "workflows" / "mesh.generate.trellis-2.yml")
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


def _proxy_config() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "proxy_config.yml").read_text(encoding="utf-8"))


class Owner:
    def __init__(self, sink: list[dict[str, Any]], rid: str) -> None:
        self.sink = sink
        self.rid = rid
        self.outcome: tuple[str, str] | None = None

    async def __aenter__(self) -> "Owner":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self.sink.append({"request_id": self.rid, "outcome": self.outcome, "exc": exc_type.__name__ if exc_type else None})
        return False

    def report(self, *, status: str, reason: str) -> None:
        self.outcome = (status, reason)


class Harness:
    """Holds the scenario's mutable state: records, services, upstream behaviour and the receipts written."""

    def __init__(self) -> None:
        cfg = _proxy_config()
        self.frontends = list(route_registry.protocol_frontend_records(registry=None, proxy_config=cfg))
        trellis_raw = copy.deepcopy(cfg["backends"]["trellis"])
        trellis_raw["resource_capacity"] = dict(GOOD_CAPACITY)
        self.services: dict[str, dict[str, Any]] = {
            "trellis": service_registry.service_from_backend_config("trellis", trellis_raw, source="provisioned"),
            "vram-observer": service_registry.service_from_backend_config(
                "vram-observer", {"base_url": OBSERVER_URL, "capabilities": []}, source="provisioned"),
        }
        self.authenticated = True
        self.receipts: list[dict[str, Any]] = []
        self.downstream: list[httpx.Request] = []
        self.health_status = 200
        self.observer_body: Any = {"free": TOTAL, "total": TOTAL}
        self.observer_status = 200
        self.downstream_mode: Any = "ok"
        self.leases = ExclusivePoolLeaseManager()

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

    def app(self) -> FastAPI:
        harness = self

        def authenticate(request: Request, path: str) -> Any:
            if not harness.authenticated:
                return JSONResponse({"error": {"message": "Missing or invalid API key.", "code": "unauthorized"}}, status_code=401)
            return _Auth()

        def open_receipt(auth: Any, rid: str, path: str) -> Owner:
            return Owner(harness.receipts, rid)

        app = FastAPI()
        register_mesh_routes(app, MeshApiDeps(
            manifest=MANIFEST,
            authenticate=authenticate,
            frontend_records=lambda: harness.frontends,
            services=lambda: harness.services,
            open_receipt=open_receipt,
            leases=harness.leases,
            client_factory=lambda: httpx.AsyncClient(transport=_Upstream(harness.handler)),
            new_request_id=lambda: "req-test",
        ))
        return app


class _Upstream(httpx.AsyncBaseTransport):
    """Routes requests to a sync or async handler (httpx's MockTransport awaits only sync results)."""

    def __init__(self, handler) -> None:
        self.handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        result = self.handler(request)
        if inspect.isawaitable(result):
            result = await result
        return result


class _Auth:
    identity = None
    app_id = "app-test"
    name = "test"


def _multipart(fields: dict[str, str] | None = None, *, image: bytes | None = PNG, name: str = "picture.png") -> dict:
    files = {"image": (name, image, "image/png")} if image is not None else {}
    return {"data": fields or {}, "files": files}


def _post(client: TestClient, **kwargs: Any) -> httpx.Response:
    return client.post(MESH_PATH, **kwargs)


def _client(harness: Harness) -> TestClient:
    return TestClient(harness.app())


def _set_service(harness: Harness, service_id: str, mutate) -> None:
    record = copy.deepcopy(harness.services[service_id])
    mutate(record)
    harness.services[service_id] = record


def _frontend(harness: Harness, mutate) -> None:
    records = copy.deepcopy(harness.frontends)
    for record in records:
        if record.get("id") == "mesh":
            mutate(record)
    harness.frontends = records


# --- the success path --------------------------------------------------------

def test_success_returns_raw_glb_and_one_receipt() -> None:
    harness = Harness()
    client = _client(harness)

    response = _post(client, **_multipart({"seed": "42"}))

    assert response.status_code == 200
    assert response.headers["content-type"] == "model/gltf-binary"
    assert response.content == GLB
    assert response.headers["x-request-id"] == "req-test"
    assert len(harness.downstream) == 1
    sent = harness.downstream[0].content
    assert b'name="seed"' in sent and b"42" in sent
    assert b'name="resolution"' in sent and b"1024" in sent  # the manifest default
    assert harness.receipts == [{"request_id": "req-test", "outcome": ("succeeded", "succeeded"), "exc": None}]
    assert harness.leases.active_claims() == 0


# --- auth, then route policy, then body --------------------------------------

def test_auth_refusal_precedes_a_broken_route_record() -> None:
    harness = Harness()
    harness.authenticated = False
    _frontend(harness, lambda record: record.update(enabled=False))
    response = _post(_client(harness), **_multipart({"seed": "1"}))
    assert response.status_code == 401
    assert harness.downstream == [] and harness.receipts == []


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(enabled=False),
    lambda r: r["registered_routes"].clear(),
    lambda r: r["registered_routes"][0].update(workflow="image.generate.other"),
    lambda r: r["registered_routes"][0].update(intent="images.generate"),
    lambda r: r["registered_routes"][0].update(capability="images"),
    lambda r: r["registered_routes"][0].update(methods=["GET"]),
    lambda r: r["registered_routes"][0].update(canonical="/v1/meshes/other"),
    lambda r: r["registered_routes"].append(dict(r["registered_routes"][0])),
])
def test_bad_frontend_record_fails_closed_before_the_body_is_read(mutate) -> None:
    harness = Harness()
    _frontend(harness, mutate)
    client = _client(harness)

    # A body that would be a 400 if it were read: the answer must be the policy refusal, not the body error.
    response = _post(client, data={"seed": "nope"}, files={"image": ("x.gif", b"GIF89a", "image/gif")})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert "retry-after" not in {k.lower() for k in response.headers}
    assert harness.downstream == []


def test_absent_frontend_record_fails_closed() -> None:
    harness = Harness()
    harness.frontends = [r for r in harness.frontends if r.get("id") != "mesh"]
    response = _post(_client(harness), **_multipart())
    assert response.status_code == 503


@pytest.mark.parametrize("mutate", [
    lambda s: s.update(enabled=False),
    lambda s: s.update(capabilities=["images"]),
    lambda s: s["config"].update(resource_capacity={"enabled": False}),
    lambda s: s["config"].update(resource_capacity={**GOOD_CAPACITY, "mode": "shared-pool"}),
    lambda s: s["config"].update(resource_capacity={**GOOD_CAPACITY, "max_claim_bytes": True}),
    lambda s: s["config"].pop("resource_capacity"),
])
def test_service_policy_refuses_before_the_body_without_retry_after(mutate) -> None:
    harness = Harness()
    _set_service(harness, "trellis", mutate)
    response = _post(_client(harness), **_multipart({"seed": "nope"}))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert "retry-after" not in {k.lower() for k in response.headers}
    assert harness.downstream == []


def test_absent_service_is_unavailable() -> None:
    harness = Harness()
    del harness.services["trellis"]
    assert _post(_client(harness), **_multipart()).status_code == 503


# --- body validation ----------------------------------------------------------

@pytest.mark.parametrize("request_kwargs, code", [
    (dict(content=b"not a multipart body", headers={"content-type": "multipart/form-data; boundary=zz"}), "invalid_multipart"),
    (dict(data={"seed": "1"}), "missing_image"),                         # urlencoded form, no image part
    (_multipart({"seed": "-1"}), "invalid_input"),
    (_multipart({"seed": "4294967296"}), "invalid_input"),
    (_multipart({"seed": "1", "resolution": "768"}), "invalid_input"),
    (_multipart({"extensions": "{}"}), "unsupported_input"),
    (_multipart({"surprise": "x"}), "unknown_field"),
    (_multipart({}, image=None), "missing_image"),
    (_multipart({}, image=b"GIF89a" + b"\x00" * 16, name="x.gif"), "unsupported_image"),
    (_multipart({}, image=b""), "invalid_image"),
])
def test_bad_input_is_400_with_a_code(request_kwargs, code) -> None:
    harness = Harness()
    response = _post(_client(harness), **request_kwargs)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == code
    assert harness.downstream == []
    assert [r["outcome"][0] for r in harness.receipts] == ["failed"]


# --- health, observer, capacity, downstream -----------------------------------

def test_unhealthy_service_is_503_without_observer_or_downstream() -> None:
    harness = Harness()
    harness.health_status = 503
    response = _post(_client(harness), **_multipart())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert harness.downstream == []


@pytest.mark.parametrize("body, status", [
    ({"free": True, "total": TOTAL}, 200),
    ({"free": TOTAL + 1, "total": TOTAL}, 200),
    ({"other": 1}, 200),
    ({"free": TOTAL, "total": TOTAL}, 500),
])
def test_malformed_or_unavailable_observer_is_503_resource_unavailable(body, status) -> None:
    harness = Harness()
    harness.observer_body = body
    harness.observer_status = status
    response = _post(_client(harness), **_multipart())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "resource_unavailable"
    assert response.headers["retry-after"] == "30"
    assert harness.downstream == []
    assert harness.leases.active_claims() == 0


def test_insufficient_claim_plus_reserve_is_503_resource_unavailable() -> None:
    harness = Harness()
    harness.observer_body = {"free": 4 * 1024 * MIB, "total": TOTAL}
    response = _post(_client(harness), **_multipart())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "resource_unavailable"
    assert harness.downstream == []


@pytest.mark.parametrize("mode, status, code", [
    (500, 502, "upstream_error"),
    (404, 502, "upstream_error"),
    (httpx.ConnectError("refused"), 502, "upstream_unavailable"),
    (httpx.ReadTimeout("slow"), 504, "upstream_timeout"),
])
def test_downstream_failures_map_to_bounded_public_errors(mode, status, code) -> None:
    harness = Harness()
    harness.downstream_mode = mode
    client = _client(harness)
    response = _post(client, **_multipart())
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert "internal secret" not in response.text
    assert harness.leases.active_claims() == 0
    assert harness.receipts[-1]["outcome"][0] == "failed"


def test_lease_is_released_after_a_failure_and_the_next_request_runs() -> None:
    harness = Harness()
    client = _client(harness)
    harness.downstream_mode = 500
    assert _post(client, **_multipart()).status_code == 502
    harness.downstream_mode = "ok"
    assert _post(client, **_multipart()).status_code == 200


def test_unexpected_fault_is_one_bounded_500() -> None:
    harness = Harness()
    harness.downstream_mode = RuntimeError("boom internal detail")
    response = _post(_client(harness), **_multipart())
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "boom" not in response.text


# --- disconnect: cancel the HTTP task, release the lease, then the receipt ------

def test_client_disconnect_cancels_downstream_and_releases_the_lease() -> None:
    harness = Harness()
    events: list[str] = []

    async def hang(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{TRELLIS_URL}/generate":
            events.append("downstream-start")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                events.append("downstream-cancelled")
                raise
        return harness.handler(request)

    harness.handler = hang  # type: ignore[method-assign]

    async def drive() -> tuple[list[dict], list[str]]:
        app = harness.app()
        body, boundary = _multipart_body()
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
            "scheme": "http", "path": MESH_PATH, "raw_path": MESH_PATH.encode(), "query_string": b"",
            "headers": [(b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
                        (b"content-length", str(len(body)).encode())],
            "client": ("127.0.0.1", 1), "server": ("testserver", 80), "root_path": "",
        }
        sent_body = False
        disconnect = asyncio.Event()
        sends: list[dict] = []

        async def receive() -> dict:
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            sends.append(message)

        async def run_app() -> None:
            try:
                await app(scope, receive, send)
            except asyncio.CancelledError:
                sends.append({"type": "cancelled"})

        async def watch() -> None:
            while "downstream-start" not in events:
                await asyncio.sleep(0.01)
            disconnect.set()

        await asyncio.wait_for(asyncio.gather(run_app(), watch()), timeout=10)
        return harness.receipts, events

    receipts, seen = asyncio.run(drive())
    assert seen == ["downstream-start", "downstream-cancelled"]
    assert harness.leases.active_claims() == 0
    assert receipts and receipts[-1]["exc"] == "CancelledError"


def _multipart_body() -> tuple[bytes, str]:
    boundary = "xBOUNDARYx"
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="p.png"\r\nContent-Type: image/png\r\n\r\n'.encode()
        + PNG + b"\r\n",
        f'--{boundary}--\r\n'.encode(),
    ]
    return b"".join(parts), boundary
