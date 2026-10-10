"""Security gate: the unmatched-path catch-all never reaches a backend.

The catch-all route used to accept a ``mapped_route`` handler parameter.
FastAPI exposes such a parameter as a query parameter, so any authenticated
caller could append ``?mapped_route=true`` and skip the only guard in front of
the native passthrough executor. That executor forwards arbitrary paths to the
local LLM backend with gateway-injected backend credentials, can start a model
load, and skips access-plan policy and execution receipts.

These tests drive the production app through TestClient. Every backend side
effect is a tripwire; no network request is made.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import proxy
from gateway import admin_store, receipt_store
from gateway.admin_oidc import AdminSession
from gateway.admin_runtime import generate_app_api_key
from gateway.user_runtime import mint_user_app_key


BYPASS_QUERIES = [
    "mapped_route=true",
    "mapped_route=1",
    "mapped_route=yes",
    "mapped_route=on",
    "mapped_route=false&mapped_route=true",
    "mapped_route=junk",
]

# (method, path): each reached the native executor before the fix.
UNMATCHED_ROUTES = [
    ("GET", "/not-a-route"),
    ("GET", "/v1/model/list"),  # backend admin key was injected for this path
    ("POST", "/v1/model/load"),
    ("POST", "/v1/chat/completions/"),  # prefix match started the model loader
    ("POST", "/v1/completions/extra"),
]

# Known OpenAI routes behind a prefix that is not a configured path token.
PREFIXED_KNOWN_ROUTES = [
    "/foo/v1/chat/completions",
    "/v1/v1/chat/completions",
    "/api/v1/completions",
]

CHAT_BODY = {"model": "test-model", "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    paths = {}
    for module, name in ((admin_store, "admin"), (receipt_store, "receipts")):
        path = tmp_path / f"{name}.db"
        module.init_db(path)
        connect = module._conn
        monkeypatch.setattr(module, "_conn", lambda _path, fn=connect, db=path: fn(db))
        monkeypatch.setattr(module, "DB_PATH", path)
        paths[name] = path

    monkeypatch.setattr(proxy.ADMIN_STORE, "path", paths["admin"])
    monkeypatch.setattr(proxy, "AUTH_ENABLED", True)
    monkeypatch.setattr(proxy, "AUTH_PUBLIC_PATHS", ["/health"])
    monkeypatch.setattr(proxy, "ALLOW_QUERY_PARAM_KEY", False)
    monkeypatch.setattr(proxy, "_client_is_lan", lambda _request: False)
    monkeypatch.setattr(proxy, "PATH_TOKENS", {})

    calls: list[str] = []

    def tripwire(name, result=None):
        async def _called(*_args, **_kwargs):
            calls.append(name)
            if isinstance(result, BaseException):
                raise result
            return result
        return _called

    # Health and availability succeed so that, without the guard, the request
    # proceeds all the way to the backend send.
    monkeypatch.setattr(proxy, "_service_availability_response", lambda *_a, **_k: None)
    monkeypatch.setattr(proxy, "check_service_health", tripwire("check_service_health", True))
    monkeypatch.setattr(proxy, "is_model_loaded", tripwire("is_model_loaded", False))
    monkeypatch.setattr(proxy, "load_model", tripwire("load_model", True))
    monkeypatch.setattr(
        proxy, "send_llm_execution_request",
        tripwire("send_llm_execution_request", RuntimeError("backend reached")),
    )
    monkeypatch.setattr(
        proxy, "_forward_current_loaded_openai_request",
        tripwire("_forward_current_loaded_openai_request", RuntimeError("backend reached")),
    )
    owner_cls = proxy.ReceiptLifecycleOwner

    def receipt_owner(*args, **kwargs):
        calls.append("ReceiptLifecycleOwner")
        return owner_cls(*args, **kwargs)

    monkeypatch.setattr(proxy, "ReceiptLifecycleOwner", receipt_owner)

    restricted = admin_store.create_profile(
        "Restricted", {"id": "restricted", "allow": {"model_groups": []}}, path=paths["admin"]
    )
    user = admin_store.upsert_oidc_user(
        "https://catchall.test", "member", email="member@example.test", path=paths["admin"]
    )
    session = AdminSession(
        subject=user["id"], email=user["email"], role="none", issued_at=1, expires_at=4_000_000_000,
    )
    member_app = admin_store.create_app(
        "Phone", kind="personal", owner_user_id=user["id"],
        default_profile_id=restricted["id"], path=paths["admin"],
    )
    member_key = mint_user_app_key(session, member_app["id"], path=paths["admin"])["api_key"]

    service = admin_store.create_service_account("Robot", path=paths["admin"])
    service_app = admin_store.create_app(
        "Robot app", kind="service", owner_service_account_id=service["id"], path=paths["admin"],
    )
    app_key = generate_app_api_key()
    admin_store.create_credential_hash(
        subject_kind="app", subject_id=service_app["id"], app_id=service_app["id"],
        hash=proxy.hash_api_key(app_key), prefix=app_key[:12], path=paths["admin"],
    )

    return SimpleNamespace(
        client=TestClient(proxy.app),
        calls=calls,
        paths=paths,
        restricted=restricted,
        member_app=member_app,
        service_app=service_app,
        headers={
            "member": {"Authorization": f"Bearer {member_key}"},
            "app": {"Authorization": f"Bearer {app_key}"},
        },
    )


def _receipt_count(gateway) -> int:
    with receipt_store._conn(gateway.paths["receipts"]) as conn:
        return conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]


def _send(gateway, method, path, *, headers=None, query=""):
    url = f"{path}?{query}" if query else path
    kwargs = {"json": CHAT_BODY} if method == "POST" else {}
    return gateway.client.request(method, url, headers=headers or {}, **kwargs)


def _assert_not_found_without_dispatch(gateway, response, receipts_before):
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "not_found"
    assert gateway.calls == []
    assert _receipt_count(gateway) == receipts_before


def test_identities_resolve_to_the_expected_apps_and_plan(gateway):
    from starlette.requests import Request

    def identity(headers):
        raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        request = Request({
            "type": "http", "method": "GET", "path": "/x", "query_string": b"",
            "headers": raw, "client": ("203.0.113.9", 1), "server": ("t", 80), "scheme": "http",
        })
        auth = proxy.authenticate_request(request, "/x")
        assert isinstance(auth, proxy.AuthContext)
        return auth.identity

    member = identity(gateway.headers["member"])
    assert member.app_id == gateway.member_app["id"]
    assert member.profile.id == "restricted"
    assert identity(gateway.headers["app"]).app_id == gateway.service_app["id"]


@pytest.mark.parametrize("who", ["member", "app"])
@pytest.mark.parametrize("method,path", UNMATCHED_ROUTES)
@pytest.mark.parametrize("query", BYPASS_QUERIES)
def test_mapped_route_query_cannot_open_the_catch_all(gateway, who, method, path, query):
    before = _receipt_count(gateway)
    response = _send(gateway, method, path, headers=gateway.headers[who], query=query)
    _assert_not_found_without_dispatch(gateway, response, before)


@pytest.mark.parametrize("who", ["member", "app"])
@pytest.mark.parametrize("path", PREFIXED_KNOWN_ROUTES)
@pytest.mark.parametrize("query", ["", "mapped_route=true"])
def test_known_route_behind_unknown_prefix_does_not_dispatch(gateway, who, path, query):
    before = _receipt_count(gateway)
    response = _send(gateway, "POST", path, headers=gateway.headers[who], query=query)
    _assert_not_found_without_dispatch(gateway, response, before)


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer oak_not_a_real_key"}])
@pytest.mark.parametrize("query", ["", *BYPASS_QUERIES])
def test_unauthenticated_catch_all_requests_get_401(gateway, headers, query):
    response = _send(gateway, "GET", "/not-a-route", headers=headers, query=query)
    assert response.status_code == 401, response.text
    assert response.json()["error"]["type"] == "authentication_error"
    assert gateway.calls == []


# Per-device path tokens (/<token>/v1/...) were only ever recognised by the
# catch-all, which then returned 404 for every stripped path. Zero tokens are
# configured in the shipped config. They stay explicitly unsupported: a valid
# token authenticates but never dispatches; the token never reaches a backend.

@pytest.mark.parametrize("method,path", [
    ("POST", "/v1/chat/completions"), ("POST", "/v1/completions"),
    ("POST", "/v1/embeddings"), ("GET", "/v1/models"), ("GET", "/v1/model/list"),
])
@pytest.mark.parametrize("query", ["", "mapped_route=true"])
def test_valid_path_token_prefix_is_not_found_and_never_dispatches(
    gateway, monkeypatch, method, path, query,
):
    monkeypatch.setattr(proxy, "PATH_TOKENS", {"tok_device": "device"})
    litellm_calls = []

    async def no_litellm(*_args, **_kwargs):
        litellm_calls.append(1)
        raise RuntimeError("LiteLLM reached")

    monkeypatch.setattr(proxy, "_handle_openai_litellm_request", no_litellm)
    before = _receipt_count(gateway)
    response = _send(gateway, method, f"/tok_device{path}", query=query)
    _assert_not_found_without_dispatch(gateway, response, before)
    assert litellm_calls == []
    assert "tok_device" not in response.text


@pytest.mark.parametrize("prefix", ["tok_wrong", "tok_devic", "tok_device_x"])
def test_unknown_path_token_prefix_is_unauthenticated(gateway, monkeypatch, prefix):
    monkeypatch.setattr(proxy, "PATH_TOKENS", {"tok_device": "device"})
    response = _send(gateway, "POST", f"/{prefix}/v1/chat/completions", query="mapped_route=true")
    assert response.status_code == 401, response.text
    assert gateway.calls == []


def test_mapped_route_is_not_a_catch_all_parameter():
    route = next(
        r for r in proxy.app.routes if getattr(r, "path", None) == "/{path:path}"
    )
    assert [p.name for p in route.dependant.query_params] == []
