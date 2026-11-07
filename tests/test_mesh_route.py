"""POST /v1/meshes/generate end to end inside the process: fail-closed policy, typed outcomes, cleanup.

The frontend record and the trellis service record are the real seeded data (proxy_config.yml and
registry/protocol_frontends.yml); only auth, receipts, the observer and the downstream are fakes.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
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
from gateway.mesh_api import MESH_PATH, MeshApiDeps, MeshJobDeps, register_mesh_routes
from gateway.mesh_manifest import load_mesh_manifest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = load_mesh_manifest(ROOT / "tests" / "fixtures" / "workflows_v1" / "mesh.generate.trellis-2.yml")
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
    return yaml.safe_load((ROOT / "tests/fixtures/config_homes/deployment_config.yml").read_text(encoding="utf-8"))


class Owner:
    """The receipt owner's contract: one close, by the handler's exit or, after ``handoff``, by the task that took over."""

    def __init__(self, sink: list[dict[str, Any]], rid: str) -> None:
        self.sink = sink
        self.rid = rid
        self.outcome: tuple[str, str] | None = None
        self.handed_off = False
        self.closed = False

    async def __aenter__(self) -> "Owner":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if not self.handed_off:
            await self.settle(exc)
        return False

    def handoff(self) -> "Owner":
        assert not self.closed, "handed off after the close"
        self.handed_off = True
        return self

    async def settle(self, exc: BaseException | None = None) -> None:
        assert not self.closed, "the receipt was closed twice"
        self.closed = True
        self.sink.append({"request_id": self.rid, "outcome": self.outcome, "exc": type(exc).__name__ if exc else None})

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
        self.hook: Any = None  # stalls the upstream transport; see _Upstream
        self.events: list[str] = []
        # Route policy for the caller: None allows; a callable returns a decision-like object to deny.
        self.route_policy: Any = None
        # store_output(auth, output) for the library; None stores nothing.
        self.store_output: Any = None
        # The media-job store for ``Prefer: respond-async``; None leaves the async form off.
        self.jobs_path: Path | None = None
        # register_job_counter(name, count) of the gateway's drain state; None registers nothing.
        self.register_job_counter: Any = None
        # discard_output(asset_id): removes a stored model from the library; None keeps it.
        self.discard_output: Any = None
        self.requests = 0
        # The gateway's send check: a Response refuses the send just before the engine is called.
        self.egress_refusal: Any = None
        self.egress_asked: list[str] = []
        # Whether the live registry holds the route's workflow record (an owner override may turn it off).
        self.workflow_present = True

    def deps(self) -> MeshApiDeps:
        def authenticate(request: Request, path: str) -> Any:
            if not self.authenticated:
                return JSONResponse({"error": {"message": "Missing or invalid API key.", "code": "unauthorized"}}, status_code=401)
            return _Auth()

        def open_receipt(auth: Any, rid: str, path: str) -> Owner:
            return Owner(self.receipts, rid)

        def new_request_id() -> str:
            self.requests += 1
            return "req-test" if self.requests == 1 else f"req-test-{self.requests}"

        jobs = None
        if self.jobs_path is not None:
            jobs_path = self.jobs_path
            jobs = MeshJobDeps(
                path=lambda: jobs_path,
                owner=lambda auth: {"app_id": auth.app_id, "api_key_name": auth.name, "app_profile": "plan-test"},
                response=lambda job, request: JSONResponse(
                    _public_job(job), status_code=202, headers={"Location": f"/gateway/v1/media/jobs/{job['job_id']}"}),
            )

        return MeshApiDeps(
            current_manifest=lambda: MANIFEST,
            authenticate=authenticate,
            frontend_records=lambda: self.frontends,
            services=lambda: self.services,
            open_receipt=open_receipt,
            leases=self.leases,
            client_factory=lambda: httpx.AsyncClient(transport=_Upstream(self.handler, self.hook)),
            new_request_id=new_request_id,
            route_policy=lambda auth, row: self.route_policy(auth) if self.route_policy else None,
            store_output=self.store_output,
            jobs=jobs,
            register_job_counter=self.register_job_counter,
            discard_output=self.discard_output,
            egress_refusal=self._egress,
            workflow_available=lambda: self.workflow_present,
        )

    def _egress(self, service_id: str) -> Any:
        self.egress_asked.append(service_id)
        return self.egress_refusal(service_id) if self.egress_refusal else None

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
        app = FastAPI()
        register_mesh_routes(app, self.deps())
        return app


class _Upstream(httpx.AsyncBaseTransport):
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


def _public_job(job: Any) -> dict[str, Any]:
    """The part of the job a submit answers with in these tests (the gateway's own client payload is proxy's)."""
    return {"object": "media.job", "id": job["job_id"], "status": job["status"], "request_id": job["request_id"]}


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


def test_a_stored_output_names_its_library_asset_in_a_header() -> None:
    harness = Harness()
    stored: list[Any] = []
    harness.store_output = lambda auth, output: stored.append(output) or "ast_" + "b" * 64
    response = _post(_client(harness), **_multipart())
    assert response.status_code == 200 and response.content == GLB
    assert response.headers["x-asset-id"] == "ast_" + "b" * 64
    [output] = stored
    assert output.data == GLB and output.suffix == ".glb" and output.request_id == "req-test"


def test_a_stored_output_carries_the_workflow_name_and_the_picture_it_was_made_from() -> None:
    harness = Harness()
    stored: list[Any] = []
    harness.store_output = lambda auth, output: stored.append(output) or "ast_" + "b" * 64
    response = _post(_client(harness), **_multipart(name="garden-gnome.png"))
    assert response.status_code == 200
    [output] = stored
    assert output.workflow_name == "TRELLIS.2 image to 3D"
    assert output.takes_text is False
    [source] = output.inputs
    assert (source.data, source.name, source.media_type) == (PNG, "garden-gnome.png", "image/png")


def test_a_storage_fault_still_returns_the_model_without_an_asset_id() -> None:
    harness = Harness()

    def broken(auth: Any, output: Any) -> str:
        raise OSError("disk full")

    harness.store_output = broken
    response = _post(_client(harness), **_multipart())
    assert response.status_code == 200 and response.content == GLB
    assert "x-asset-id" not in response.headers


def test_a_refused_egress_sends_nothing_and_still_closes_the_receipt() -> None:
    harness = Harness()
    harness.egress_refusal = lambda service_id: JSONResponse({"error": {"code": "transition_unsupported"}}, status_code=503)
    response = _post(_client(harness), **_multipart())
    assert response.status_code == 503 and response.json()["error"]["code"] == "transition_unsupported"
    assert harness.downstream == [], "nothing reaches the engine"
    assert harness.egress_asked == [MANIFEST.service_id]
    assert harness.receipts == [{"request_id": "req-test", "outcome": ("failed", "not_admitting"), "exc": None}]
    assert harness.leases.active_claims() == 0


def test_an_egress_check_that_allows_changes_nothing() -> None:
    harness = Harness()
    response = _post(_client(harness), **_multipart({"seed": "42"}))
    assert response.status_code == 200 and harness.egress_asked == [MANIFEST.service_id]


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


def test_a_route_whose_workflow_record_is_absent_or_disabled_is_unavailable() -> None:
    harness = Harness()
    harness.workflow_present = False
    response = _post(_client(harness), **_multipart())
    assert response.status_code == 503 and response.json()["error"]["code"] == "service_unavailable"
    assert harness.downstream == [] and harness.receipts == []
    harness.workflow_present = True  # the record is back: the same route serves again
    assert _post(_client(harness), **_multipart()).status_code == 200


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


# --- disconnect: the receipt closes at once; the lease stays until the engine answers ------------

class _Engine:
    """A stand-in for the engine's slow phase: it stalls until the test lets it return, and notes both ends."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.events: list[str] = []

    async def stall(self, name: str) -> None:
        self.events.append(f"{name}-start")
        await self.release.wait()
        self.events.append(f"{name}-returned")


class _StalledGlb(httpx.AsyncByteStream):
    """A 200 whose body starts and then stops: the route is stuck in the read phase."""

    def __init__(self, engine: _Engine) -> None:
        self.engine = engine

    async def __aiter__(self):
        yield GLB[:8]
        await self.engine.stall("read")


def _scope(body: bytes, boundary: str) -> dict:
    return {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": MESH_PATH, "raw_path": MESH_PATH.encode(), "query_string": b"",
        "headers": [(b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
                    (b"content-length", str(len(body)).encode())],
        "client": ("127.0.0.1", 1), "server": ("testserver", 80), "root_path": "",
    }


async def _call(app: FastAPI, body: bytes, boundary: str, *, extra_headers: list | None = None,
                disconnect_when=None) -> list[dict]:
    """One ASGI call. With ``disconnect_when``, the client goes away once it is true; else it stays."""
    scope = _scope(body, boundary)
    scope["headers"] = [*scope["headers"], *(extra_headers or [])]
    gone = asyncio.Event()
    sent_body = False
    sends: list[dict] = []

    async def receive() -> dict:
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": body, "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sends.append(message)

    async def watch() -> None:
        while not disconnect_when():
            await asyncio.sleep(0.01)
        gone.set()

    async def run() -> None:
        try:
            await app(scope, receive, send)
        except asyncio.CancelledError:
            sends.append({"type": "cancelled"})

    if disconnect_when is None:
        await asyncio.wait_for(run(), timeout=10)
    else:
        await asyncio.wait_for(asyncio.gather(run(), watch()), timeout=10)
    return sends


async def _until(condition, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


def _others() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]


def _pool_held(harness: Harness) -> bool:
    return any(lock.locked() for lock in harness.leases._locks.values())


@pytest.mark.parametrize("phase", ["observer", "connect", "write", "read"])
def test_disconnect_in_each_phase_closes_the_receipt_and_frees_the_lease_only_when_the_engine_returns(phase: str) -> None:
    """The client leaves while the observer, the connect, the upload or the response read is in flight.

    The engine has no cancel call, so the run is not cancelled: the receipt closes as cancelled at once, the lease stays
    held (a second run would meet a busy engine), and it is freed when the stalled call returns. Then no task is left and
    the next request runs. The phases: the observer GET; the transport stalls before any byte is sent (connect); the
    request body is partly consumed and then stops (write); the 200 stalls mid-body (read).
    """
    harness = Harness()
    engine = _Engine()
    answer = harness.handler

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if phase == "observer" and url.startswith(OBSERVER_URL):
            await engine.stall("observer")
        if phase == "read" and url == f"{TRELLIS_URL}/generate":
            return httpx.Response(200, stream=_StalledGlb(engine), headers={"content-type": "model/gltf-binary"})
        return answer(request)

    async def hook(request: httpx.Request) -> None:
        if str(request.url) != f"{TRELLIS_URL}/generate":
            return
        if phase == "connect":
            await engine.stall("connect")
        if phase == "write":
            stalled = False
            async for _chunk in request.stream:  # one chunk goes out, then the upload stops until the engine returns
                if not stalled:
                    stalled = True
                    await engine.stall("write")

    harness.handler = handler  # type: ignore[method-assign]
    harness.hook = hook if phase in ("connect", "write") else None

    async def drive() -> tuple[list[dict], list[str], bool, int, list[dict], int]:
        app = harness.app()
        body, boundary = _multipart_body()
        await _call(app, body, boundary, disconnect_when=lambda: f"{phase}-start" in engine.events)
        at_disconnect = list(engine.events)
        held = _pool_held(harness)
        receipts_at_disconnect = list(harness.receipts)
        assert held and receipts_at_disconnect  # closed now; the lease is not freed yet
        # A second run now meets the busy engine and waits for it: it must not have started the engine twice.
        engine.release.set()
        await _until(lambda: not _pool_held(harness) and not _others())
        harness.hook = None
        harness.handler = answer  # type: ignore[method-assign]
        follow = await _call(app, body, boundary)
        return receipts_at_disconnect, at_disconnect, held, len(_others()), follow, len(harness.downstream)

    receipts, at_disconnect, held, leftover, follow, _ = asyncio.run(drive())
    assert at_disconnect == [f"{phase}-start"]  # nothing was cancelled
    assert engine.events == [f"{phase}-start", f"{phase}-returned"]
    assert held and leftover == 0
    assert receipts == [{"request_id": "req-test", "outcome": None, "exc": "CancelledError"}]
    assert harness.leases.active_claims() == 0
    assert [m for m in follow if m["type"] == "http.response.start"][0]["status"] == 200
    assert len(harness.receipts) == 2 and harness.receipts[-1]["outcome"] == ("succeeded", "succeeded")


def test_a_second_sync_run_is_refused_at_once_while_a_left_client_still_holds_the_pool() -> None:
    """The sync form does not queue behind the abandoned run: it answers 503 resource_unavailable with Retry-After at
    once (as the async form does), the engine is never called twice, and the next run is admitted when the pool is free."""
    harness = Harness()
    engine = _Engine()
    answer = harness.handler
    running = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal running, peak
        if str(request.url) == f"{TRELLIS_URL}/generate":
            running += 1
            peak = max(peak, running)
            try:
                if len(harness.downstream) == 0:
                    harness.downstream.append(request)
                    await engine.stall("first")
                    return httpx.Response(200, content=GLB, headers={"content-type": "model/gltf-binary"})
            finally:
                running -= 1
        return answer(request)

    harness.handler = handler  # type: ignore[method-assign]

    async def drive() -> tuple[list[dict], float, list[dict]]:
        app = harness.app()
        body, boundary = _multipart_body()
        await _call(app, body, boundary, disconnect_when=lambda: "first-start" in engine.events)
        started = asyncio.get_running_loop().time()
        refused = await asyncio.wait_for(_call(app, body, boundary), timeout=5)  # returns while the engine still runs
        waited = asyncio.get_running_loop().time() - started
        engine.release.set()
        await _until(lambda: not _pool_held(harness) and not _others())
        return refused, waited, await _call(app, body, boundary)

    refused, waited, after = asyncio.run(drive())
    start = [m for m in refused if m["type"] == "http.response.start"][0]
    assert start["status"] == 503 and waited < 2
    assert dict(start["headers"])[b"retry-after"]
    assert json.loads(b"".join(m.get("body", b"") for m in refused if m["type"] == "http.response.body"))["error"]["code"] == "resource_unavailable"
    assert [m for m in after if m["type"] == "http.response.start"][0]["status"] == 200
    assert peak == 1
    assert [r["outcome"] for r in harness.receipts[1:]] == [("failed", "capacity"), ("succeeded", "succeeded")]


def _multipart_body() -> tuple[bytes, str]:
    boundary = "xBOUNDARYx"
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="p.png"\r\nContent-Type: image/png\r\n\r\n'.encode()
        + PNG + b"\r\n",
        f'--{boundary}--\r\n'.encode(),
    ]
    return b"".join(parts), boundary


# --- route policy: the same access rules as the other Try workflows -----------

class _Denied:
    denied = True
    allowed = False
    code = "MODEL_NOT_IN_PLAN"
    reasons = ("This isn't in your access plan.",)
    side = "person"
    sources = ("access_plan:general",)


def test_route_policy_denial_is_403_before_the_body_receipt_or_downstream() -> None:
    harness = Harness()
    harness.route_policy = lambda auth: _Denied()
    client = _client(harness)

    response = _post(client, **_multipart({"seed": "42"}))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "MODEL_NOT_IN_PLAN"
    assert harness.receipts == []
    assert harness.downstream == []


def test_route_policy_runs_after_authentication_and_before_the_body_is_read() -> None:
    harness = Harness()
    seen: list[Any] = []
    harness.route_policy = lambda auth: seen.append(auth) or _Denied()
    client = _client(harness)

    # A malformed multipart body would be a 400 if the body were read first; the denial must win.
    response = client.post(MESH_PATH, content=b"not multipart", headers={"content-type": "text/plain"})
    assert response.status_code == 403
    assert len(seen) == 1

    harness.authenticated = False
    seen.clear()
    assert _post(_client(harness), **_multipart()).status_code == 401
    assert seen == []


def test_route_policy_allow_keeps_the_success_path() -> None:
    harness = Harness()
    harness.route_policy = lambda auth: None

    response = _post(_client(harness), **_multipart())

    assert response.status_code == 200
    assert response.content == GLB
