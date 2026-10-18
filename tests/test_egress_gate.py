"""Decision D31: one egress gate for every management response.

A management handler may answer only with an approved response (``gateway/egress.py``). A router built with
``ManagementRoute`` refuses anything else, the ``EgressGate`` holds a management response until its bytes are admitted
and gives every framework error a fixed envelope that never echoes a request body or an exception, and startup fails for
an admin route that is neither a management route nor an inventoried exception. These tests build small apps with the
same classes, and drive the exported gateway app for the framework errors.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel

from gateway import egress, projection
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401

SEED = "SEED-egress-canary-9931"


class Body(BaseModel):
    count: int


def _app(*, gate: bool = True) -> FastAPI:
    app = FastAPI()
    router = APIRouter(prefix="/admin/api", route_class=egress.ManagementRoute)

    @router.get("/fine")
    def fine():
        return egress.plain_json_response({"ok": True, "count": 3})

    @router.get("/record")
    def record():
        return egress.projected_response({"service_registry": {"schema_version": 2, "services": {}}})

    @router.get("/raw")
    def raw():
        return JSONResponse({"leak": SEED})

    @router.get("/dict")
    def as_dict():
        return {"leak": SEED}

    @router.get("/stream")
    def stream():
        return StreamingResponse(iter([SEED.encode()]))

    @router.get("/plain-with-record-key")
    def plain_with_record_key():
        return egress.plain_json_response({"service_registry": {"x": SEED}})

    @router.get("/unregistered-key")
    def unregistered_key():
        return egress.projected_response({"surprise": SEED})

    @router.get("/boom")
    def boom():
        raise RuntimeError(f"the secret is {SEED}")

    @router.get("/http-error")
    def http_error():
        raise HTTPException(404, detail=SEED)

    @router.post("/typed")
    def typed(body: Body):
        return egress.plain_json_response({"count": body.count})

    @router.get("/forged")
    def forged():
        response = JSONResponse({"leak": SEED})
        response.headers["X-Sanitized"] = "true"  # a header a handler sets proves nothing
        return response

    @router.get("/error")
    def error():
        return egress.safe_error_response(400, "invalid_thing", f"Bad value.\x00 see http://u:{SEED}@h/x")

    app.include_router(router)

    @app.get("/v1/open")
    def open_route():
        return JSONResponse({"data": SEED})  # outside the management plane: not held

    if gate:
        app.add_middleware(egress.EgressGate)
    return app


@pytest.fixture
def client():
    return TestClient(_app(), raise_server_exceptions=False)


def test_an_approved_response_passes_unchanged(client):
    assert client.get("/admin/api/fine").json() == {"ok": True, "count": 3}
    assert client.get("/admin/api/record").json()["service_registry"]["schema_version"] == 2


@pytest.mark.parametrize("path", ["raw", "dict", "stream", "forged"])
def test_a_handler_that_answers_with_anything_but_an_approved_response_is_refused(client, path):
    response = client.get(f"/admin/api/{path}")
    assert response.status_code == 500 and SEED not in response.text
    assert response.json() == {"error": {"code": "egress_blocked", "message": "The gateway could not answer this request."}}


def test_record_content_through_the_plain_constructor_or_an_unregistered_key_is_refused(client):
    for path in ("plain-with-record-key", "unregistered-key"):
        response = client.get(f"/admin/api/{path}")
        assert response.status_code == 500 and SEED not in response.text, path
        assert response.json()["error"]["code"] == "internal_error"


def test_an_exception_gives_the_fixed_500_and_the_log_names_only_its_class(client, capfd):
    response = client.get("/admin/api/boom")
    assert response.status_code == 500 and SEED not in response.text
    assert response.json() == {"error": {"code": "internal_error", "message": "The gateway could not answer this request."}}
    logged = capfd.readouterr()
    assert SEED not in logged.out + logged.err and "RuntimeError" in logged.out


def test_framework_errors_get_fixed_envelopes_that_never_echo_the_request(client):
    cases = {
        "http error": client.get("/admin/api/http-error"),
        "unknown route": client.get(f"/admin/api/{SEED}"),
        "wrong method": client.delete("/admin/api/fine"),
        "invalid body": client.post("/admin/api/typed", json={"count": SEED}),
        "missing body": client.post("/admin/api/typed", content=SEED.encode(), headers={"content-type": "application/json"}),
    }
    expected = {"http error": 404, "unknown route": 404, "wrong method": 405, "invalid body": 422, "missing body": 422}
    for label, response in cases.items():
        assert response.status_code == expected[label], (label, response.status_code)
        assert SEED not in response.text and set(response.json()) == {"error"}, label


def test_a_safe_error_is_one_fixed_shape_cleaned_of_control_characters_and_url_user_info(client):
    response = client.get("/admin/api/error")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_thing"
    assert SEED not in response.text and "\x00" not in response.text and "http://h/x" in response.text


def test_a_blocked_response_never_starts_before_the_gate_has_decided():
    """The gate holds the start of a response: a refused one sends only the fixed envelope."""
    app = _app()
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": "/admin/api/raw", "raw_path": b"/admin/api/raw", "query_string": b"",
             "headers": [], "server": ("test", 80), "client": ("c", 1), "scheme": "http", "http_version": "1.1", "root_path": ""}
    asyncio.run(app(scope, receive, send))

    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert len(starts) == 1 and starts[0]["status"] == 500
    assert SEED.encode() not in b"".join(m.get("body", b"") for m in sent)


def test_a_response_larger_than_the_bound_is_refused(monkeypatch):
    app = FastAPI()
    router = APIRouter(prefix="/admin/api", route_class=egress.ManagementRoute)

    @router.get("/big")
    def big():
        return egress.plain_json_response({"text": "x" * 5000})

    app.include_router(router)
    app.add_middleware(egress.EgressGate, max_body=1000)
    response = TestClient(app, raise_server_exceptions=False).get("/admin/api/big")
    assert response.status_code == 500 and response.json()["error"]["code"] == "internal_error"


def test_a_route_outside_the_management_plane_is_not_held(client):
    assert client.get("/v1/open").json() == {"data": SEED}


def test_startup_fails_for_an_admin_route_that_is_neither_managed_nor_inventoried():
    app = FastAPI()
    plain = APIRouter(prefix="/admin/api")

    @plain.get("/sneaky")
    def sneaky():
        return {"ok": True}

    app.include_router(plain)
    with pytest.raises(RuntimeError, match="sneaky"):
        egress.verify_and_freeze(app)


def test_a_management_route_added_after_startup_froze_the_table_answers_404():
    app = FastAPI()
    router = APIRouter(prefix="/admin/api", route_class=egress.ManagementRoute)

    @router.get("/early")
    def early():
        return egress.plain_json_response({"ok": True})

    app.include_router(router)
    egress.verify_and_freeze(app)
    late = APIRouter(prefix="/admin/api", route_class=egress.ManagementRoute)

    @late.get("/late")
    def late_route():
        return egress.plain_json_response({"ok": True})

    app.include_router(late)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/admin/api/early").status_code == 200
    assert client.get("/admin/api/late").status_code == 404


def test_the_exported_gateway_app_gives_its_own_framework_errors_the_fixed_envelopes(gateway):
    owner, *_ = owner_and_admin(gateway)
    unknown = owner.get("/admin/api/engines/nothing-here")  # answered by a handler: an error of its own, in the one fixed shape
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "service_not_found" and set(unknown.json()) == {"error"}
    unreadable = owner.put("/admin/api/operator/service-registry", content=SEED.encode(),
                           headers={**csrf_for(owner), "content-type": "application/json"})
    assert unreadable.status_code in (400, 412, 422) and SEED not in unreadable.text


def test_a_management_route_without_the_gate_still_refuses_a_response_that_is_not_approved():
    """The route class is a layer of its own: with no middleware at all, a raw response is still not sent."""
    response = TestClient(_app(gate=False), raise_server_exceptions=False).get("/admin/api/raw")
    assert response.status_code == 500 and SEED not in response.text


def test_the_gate_holds_a_management_response_that_no_approved_response_made():
    """The gate is a layer of its own too: an inner app that marks a request as management and answers with raw JSON is held."""
    async def inner(scope, receive, send):
        scope[egress.MANAGEMENT_KEY] = True
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": json.dumps({"leak": SEED}).encode()})

    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    gate = egress.EgressGate(inner)
    asyncio.run(gate({"type": "http", "method": "GET", "path": "/admin/api/x", "headers": []}, receive, send))

    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [500]
    assert SEED.encode() not in b"".join(m.get("body", b"") for m in sent)




def test_an_exception_inside_the_exported_app_answers_with_the_fixed_envelope(gateway, monkeypatch):
    """The gate is installed on the app the server runs: an exception that escapes a handler is not the framework's 500."""
    import proxy

    owner, *_ = owner_and_admin(gateway)

    def broken(*args, **kwargs):
        raise RuntimeError(f"the registry holds {SEED}")

    monkeypatch.setattr(proxy.ADMIN_STORE, "get_service_registry", broken)
    response = owner.get("/admin/api/operator/service-registry")
    assert response.status_code == 500 and SEED not in response.text
    assert response.json() == {"error": {"code": "internal_error", "message": "The gateway could not answer this request."}}


def _mixed_app() -> FastAPI:
    app = FastAPI()
    outside = APIRouter(prefix="/admin/api/outside")  # a router the phase 1 inventory leaves unmanaged

    @outside.get("/mine")
    def mine():
        return JSONResponse({"error": {"code": "test_not_found", "message": "That test is over."}}, status_code=404)

    @outside.get("/framework")
    def framework():
        raise HTTPException(404, detail=SEED)

    @outside.post("/only-post")
    def only_post():
        return JSONResponse({"ok": True})

    app.include_router(outside)
    app.add_middleware(egress.EgressGate)
    return app


def test_a_handler_outside_the_managed_list_keeps_its_own_error_code_and_the_framework_text_is_still_fixed():
    client = TestClient(_mixed_app(), raise_server_exceptions=False)

    own = client.get("/admin/api/outside/mine")
    framework = client.get("/admin/api/outside/framework")

    assert own.status_code == 404 and own.json()["error"]["code"] == "test_not_found"  # a page reads this code
    assert framework.status_code == 404 and SEED not in framework.text and framework.json()["error"]["code"] == "not_found"


def test_the_fixed_405_keeps_the_allow_header_of_the_route():
    response = TestClient(_mixed_app(), raise_server_exceptions=False).get("/admin/api/outside/only-post")

    assert response.status_code == 405 and response.headers["allow"] == "POST"
    assert response.json()["error"]["code"] == "method_not_allowed"
