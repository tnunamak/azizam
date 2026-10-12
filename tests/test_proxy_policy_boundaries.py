from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

import proxy
from gateway.admin_oidc import CookieConfig, session_from_claims


def request(path="/gateway/v1/receipts", *, who="a", headers=None):
    raw = [(b"x-test-person", who.encode())]
    raw.extend((k.lower().encode(), v.encode()) for k, v in (headers or {}).items())

    async def unreadable_body():
        raise AssertionError("denied request body was consumed")

    return Request({
        "type": "http", "http_version": "1.1", "method": "POST",
        "path": path, "raw_path": path.encode(), "query_string": b"",
        "scheme": "http", "server": ("testserver", 80),
        "client": ("203.0.113.10", 41000), "headers": raw,
    }, receive=unreadable_body)


def context(name):
    return proxy.AuthContext(identity=SimpleNamespace(
        name=name, key_hash=f"fixture-hash-{name}", app_id=f"app-{name}",
        profile=SimpleNamespace(
            id=f"profile-{name}",
            status_visibility=SimpleNamespace(level="standard"),
        ),
    ))


def install_identities(monkeypatch):
    contexts = {name: context(name) for name in ("a", "b")}
    monkeypatch.setattr(proxy, "authenticate_request",
                        lambda req, path: contexts[req.headers["x-test-person"]])
    monkeypatch.setattr(
        proxy, "_gateway_audience_resolver",
        lambda req: "audience-" + proxy._auth_context_from_scope(req).identity.name,
    )
    return contexts


def test_gateway_security_wiring_is_required():
    # Never skip when a required security component disappears.
    paths = {getattr(route, "path", "") for route in proxy.app.routes}
    assert "/gateway/v1/workflows" in paths
    assert callable(proxy._gateway_audience_resolver)
    dispatches = [
        item.kwargs.get("dispatch")
        for item in proxy.app.user_middleware
        if hasattr(item, "kwargs")
    ]
    assert proxy._gateway_auth_middleware in dispatches


@pytest.mark.parametrize("allow", [False, True])
def test_gateway_boundary_stops_denial_and_sets_scope_before_handler(monkeypatch, allow):
    expected = context("a")
    denied = proxy.auth_error("fixture denial")
    authentications = []
    handler_calls = []

    def authenticate(req, path):
        authentications.append(path)
        return expected if allow else denied

    monkeypatch.setattr(proxy, "authenticate_request", authenticate)
    monkeypatch.setattr(
        proxy, "_gateway_audience_resolver",
        lambda req: "fixture-audience"
        if req.scope.get("auth_context") is expected else "wrong",
    )
    isolated = FastAPI()
    isolated.middleware("http")(proxy._gateway_auth_middleware)

    @isolated.post("/gateway/v1/receipts")
    async def endpoint(req: Request):
        handler_calls.append(req.scope["auth_context"])
        return {
            "identity": req.scope["auth_context"].identity.name,
            "audience": req.scope["gateway_audience"],
        }

    with TestClient(isolated) as client:
        response = client.post("/gateway/v1/receipts", content=b"not-json")
    assert authentications == ["/gateway/v1/receipts"]
    if allow:
        assert response.status_code == 200
        assert response.json() == {"identity": "a", "audience": "fixture-audience"}
        assert handler_calls == [expected]
    else:
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.json()["error"]["type"] == "authentication_error"
        assert handler_calls == []

    if not allow:
        async def must_not_run(req):
            raise AssertionError("denied request reached the handler")
        assert asyncio.run(proxy._gateway_auth_middleware(
            request(), must_not_run,
        )) is denied


def test_overlapping_identities_survive_sibling_cancellation(monkeypatch):
    identities = install_identities(monkeypatch)

    async def scenario():
        entered = {name: asyncio.Event() for name in identities}
        release = asyncio.Event()
        requests = {name: request(who=name) for name in identities}
        finalized = []

        async def endpoint(req):
            name = req.headers["x-test-person"]
            assert req.scope["auth_context"] is identities[name]
            assert req.scope["gateway_audience"] == "audience-" + name
            entered[name].set()
            try:
                await release.wait()
                assert req.scope["auth_context"] is identities[name]
                assert req.scope["gateway_audience"] == "audience-" + name
                return JSONResponse({"person": name})
            finally:
                finalized.append(name)

        tasks = {
            name: asyncio.create_task(proxy._gateway_auth_middleware(req, endpoint))
            for name, req in requests.items()
        }
        try:
            # Deadline is a deadlock guard, not a timing-based assertion.
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in entered.values())), 5,
            )
            tasks["a"].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks["a"]
            assert not tasks["b"].done()
            release.set()
            response = await asyncio.wait_for(tasks["b"], 5)
            assert json.loads(response.body) == {"person": "b"}
            assert sorted(finalized) == ["a", "b"]
            assert requests["a"].scope["auth_context"] is identities["a"]
            assert requests["b"].scope["auth_context"] is identities["b"]
        finally:
            for task in tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("disk_failure", [False, True])
def test_native_policy_denial_is_owned_durable_attempt_and_never_dispatches(
    monkeypatch, disk_failure,
):
    auth = context("a")
    req = request("/gateway/v1/providers/fixture/private")
    req.scope["auth_context"] = auth
    monkeypatch.setattr(proxy, "_PROVIDER_NATIVE_TARGETS", {
        "fixture": {
            "backend": "fixture", "service": "fixture",
            "host": "http://never-contact.invalid", "enabled": True,
        },
    })
    monkeypatch.setattr(
        proxy, "_provider_native_invalid_paid_remote_response", lambda *args: None,
    )
    monkeypatch.setattr(proxy, "make_policy_request", lambda **kwargs: kwargs)
    decisions = []
    records = []

    def refuse(identity, policy_request, receipt):
        decisions.append((identity, policy_request, receipt))
        return SimpleNamespace(denied=True, reasons=("fixture policy refusal",))

    def record(receipt):
        records.append(receipt)
        if disk_failure:
            raise OSError("fixture disk full")
        return receipt.request_id

    forbidden = Mock(side_effect=AssertionError("denial reached provider/admission"))
    monkeypatch.setattr(proxy, "evaluate_and_apply", refuse)
    monkeypatch.setattr(proxy.receipt_store, "record", record)
    monkeypatch.setattr(proxy, "_dispatch_admission_response", forbidden)
    monkeypatch.setattr(proxy.httpx, "AsyncClient", forbidden)
    response = asyncio.run(proxy.provider_native_request(req, "fixture", "private"))

    assert response.status_code == 403
    assert json.loads(response.body)["error"] == {
        "message": "fixture policy refusal",
        "type": "permission_error", "code": "policy_denied",
    }
    assert len(decisions) == len(records) == 1
    identity, policy_request, receipt = decisions[0]
    assert identity is auth.identity
    assert policy_request == {
        "protocol": "provider-native", "intent": "provider.native",
        "backend": "fixture", "native_passthrough": True,
        "native_backend": "fixture", "native_operation": "POST /private",
    }
    assert records[0] is receipt
    assert receipt.status == "denied"
    assert receipt.app_id == "app-a"
    assert receipt.owner_key_hash == "fixture-hash-a"
    assert receipt.app_profile == "profile-a"
    forbidden.assert_not_called()


def test_user_action_requires_current_session_nonempty_csrf(monkeypatch):
    monkeypatch.setattr(proxy.ADMIN_AUTH, "signing_key", b"fixture-signing-key-32-bytes-long!")
    monkeypatch.setattr(proxy, "ADMIN_SESSION_SIGNING_KEY", None)
    cookie = CookieConfig(secure=False)
    a = session_from_claims({"sub": "fixture-a", "email": "a@example.test"}, role="none", cookie=cookie)
    b = session_from_claims({"sub": "fixture-b", "email": "b@example.test"}, role="none", cookie=cookie)
    token_a = proxy._csrf_token_for_oidc_session(a)
    token_b = proxy._csrf_token_for_oidc_session(b)
    assert token_a and token_b and token_a != token_b

    cases = [
        ({}, "user_action_header_required"),
        ({"x-azizam-user-action": "0", "x-azizam-csrf": token_a},
         "user_action_header_required"),
        ({"x-azizam-user-action": "1"}, "user_csrf_required"),
        ({"x-azizam-user-action": "1", "x-azizam-csrf": ""},
         "user_csrf_required"),
        ({"x-azizam-user-action": "1", "x-azizam-csrf": token_b},
         "user_csrf_required"),
    ]
    for headers, code in cases:
        denied = proxy._require_user_action_header(request(headers=headers), a)
        assert denied.status_code == 403
        assert json.loads(denied.body)["error"]["code"] == code
    valid = {"x-azizam-user-action": "1", "x-azizam-csrf": token_a}
    assert proxy._require_user_action_header(request(headers=valid), a) is None

    monkeypatch.setattr(proxy.ADMIN_AUTH, "signing_key", None)
    assert proxy._csrf_token_for_oidc_session(a) == ""
    for supplied in ("", token_a):
        denied = proxy._require_user_action_header(request(headers={
            "x-azizam-user-action": "1", "x-azizam-csrf": supplied,
        }), a)
        assert denied.status_code == 403
        assert json.loads(denied.body)["error"]["code"] == "user_csrf_required"


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
@pytest.mark.parametrize("sink_fails", [False, True])
def test_telemetry_never_masks_response_error_or_cancellation(
    monkeypatch, outcome, sink_fails,
):
    monkeypatch.setattr(proxy, "_FIREHOSE_NO_RECORD_EXACT", set())
    monkeypatch.setattr(proxy, "_FIREHOSE_NO_RECORD_PREFIXES", ())
    monkeypatch.setattr(proxy.ui_routes, "classify", lambda path: None)
    monkeypatch.setattr(
        proxy, "runtime_identity_for_app_credential_hash",
        lambda *args, **kwargs: SimpleNamespace(name="fixture-a"),
    )
    request_records = []
    event_records = []
    metric_records = []

    def record_request(*args):
        request_records.append(args)
        if sink_fails:
            raise OSError("fixture telemetry unavailable")

    monkeypatch.setattr(proxy.firehose_metrics, "record_request", record_request)
    monkeypatch.setattr(proxy.firehose_metrics, "record_event",
                        lambda *args, **kwargs: event_records.append((args, kwargs)))
    monkeypatch.setattr(proxy.metrics_sampler, "record_request",
                        lambda *args, **kwargs: metric_records.append((args, kwargs)))
    wanted = Response(b"fixture-result", status_code=201)
    failure = RuntimeError("fixture handler failed")

    async def endpoint(req):
        if outcome == "error":
            raise failure
        if outcome == "cancel":
            raise asyncio.CancelledError()
        return wanted

    call = proxy._firehose_request_counter(
        request("/v1/chat/completions", headers={"authorization": "Bearer fixture"}),
        endpoint,
    )
    if outcome == "success":
        assert asyncio.run(call) is wanted
    elif outcome == "error":
        with pytest.raises(RuntimeError) as raised:
            asyncio.run(call)
        assert raised.value is failure
    else:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(call)
    assert len(request_records) == 1
    name, path, status, latency = request_records[0]
    assert (name, path, status) == (
        "fixture-a", "/v1/chat/completions", 201 if outcome == "success" else 0,
    )
    assert latency >= 0
    assert len(metric_records) == (0 if sink_fails else 1)
    assert len(event_records) == (1 if outcome == "success" and not sink_fails else 0)

