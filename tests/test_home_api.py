"""Home's policy, owner privacy, setup truth, and single-dispatch contract."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from gateway import admin_store, receipt_store
from gateway.admin_api import AdminAuthError, AdminContext
from gateway.admin_oidc import AdminSession
from gateway.capabilities import Capability, capability_with_policy_view
from gateway.home_api import create_home_router, _resolved_capabilities, _declared_local_fast
from gateway.local_capabilities import build_alias_capabilities
from gateway.profiles import load_profile
from gateway.runtime_auth import RuntimeIdentity, hash_api_key, make_policy_request
from gateway.user_runtime import UserPortalError, create_user_app


@pytest.fixture
def home_case(tmp_path):
    db, receipts = tmp_path / "admin.db", tmp_path / "receipts.db"
    admin_store.init_db(db)
    receipt_store.init_db(receipts)
    user = admin_store.upsert_oidc_user("https://example.test", "one", email="one@example.test", path=db)
    other = admin_store.upsert_oidc_user("https://example.test", "two", email="two@example.test", path=db)
    session = AdminSession(subject=user["id"], email=user["email"], role="owner", issued_at=1, expires_at=4_000_000_000)
    personal = admin_store.ensure_personal_app_for_user(user["id"], path=db)
    client_app = create_user_app(session, name="Open WebUI", path=db)["app"]
    outsider = admin_store.create_app("Other", kind="personal", owner_user_id=other["id"], path=db)
    def credential(app, key, **kwargs):
        return admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], hash=hash_api_key(key), prefix="masked", path=db, **kwargs)
    key = "test-home-key"
    cred = credential(client_app, key)
    other_cred = credential(outsider, "other-key")
    case = SimpleNamespace(db=db, receipts=receipts, user=user, session=session, personal=personal,
                           client_app=client_app, outsider=outsider, key=key, cred=cred,
                           other_cred=other_cred, calls=[], logged_in=True, owner=True,
                           health={"backends": [{"key": "service", "live_status": "up"}]},
                           fast=True, health_error=False, inherited_profile=None, prospective_profile=None,
                           self_service=True, capabilities_override=None, aliases_override=None,
                           registry={"services": {"service": {"enabled": True, "config": {"gateway_lifecycle": True}}}})
    def require_session(request):
        if not case.logged_in:
            raise UserPortalError("login_required", "Sign in.", status_code=401)
        return session
    def action(request, _session):
        if request.headers.get("x-ai-gateway-user-action") != "1" or request.headers.get("x-ai-gateway-csrf") != "csrf":
            return JSONResponse({"error": {"code": "csrf_required"}}, status_code=403)
    def admin_auth(request):
        if not case.logged_in:
            raise AdminAuthError()
        return AdminContext(subject=user["id"], roles=frozenset({"owner" if case.owner else "admin"}))
    def capabilities(identity):
        if case.capabilities_override is not None:
            return case.capabilities_override(identity)
        allowed = identity.profile.id != "no-chat"
        alias_caps = build_alias_capabilities({"chat": {"stable": [{"alias": "fast", "resolves_to": "demo"}]}}) if case.fast else []
        return [Capability(intent="chat.completions", backend="service", model="demo", policy_available=allowed),
                *alias_caps,
                Capability(intent="image.generate", backend="service", model="image", policy_available=False)]
    async def status():
        if case.health_error:
            raise RuntimeError("health unavailable")
        return case.health
    app = FastAPI()
    app.include_router(create_home_router(store=SimpleNamespace(path=db, get_service_registry=lambda: case.registry),
                                         receipts_path=lambda: receipts, require_session=require_session,
                                         require_action=action, csrf_token=lambda _session: "csrf",
                                         default_profile_id=lambda _session: case.prospective_profile, capabilities=capabilities,
                                         inherited_profile_id_for_app=lambda _app: case.inherited_profile,
                                         self_service_enabled=lambda: case.self_service,
                                         aliases=lambda: case.aliases_override or {"chat": {"stable": [{"alias": "fast", "resolves_to": "demo"}]}, "images": {"stable": [{"alias": "image", "resolves_to": "picture"}]}},
                                         public_base_url=lambda _request: "https://gateway.example",
                                         status_provider=status, admin_auth=admin_auth))
    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        case.calls.append((request.url.path, dict(request.headers), await request.json()))
        assert request.headers["authorization"] == f"Bearer {key}"
        assert not request.headers.get("cookie")
        admin_store.touch_credential_last_used(cred["id"], path=db)
        receipt_store.record({"request_id": "real-test", "app_id": client_app["id"], "status": "succeeded", "intent": "chat.completions"}, path=receipts)
        return JSONResponse({"choices": [{"message": {"content": "Hi"}}]}, headers={"x-ai-gateway-request-id": "real-test", "request-id": "provider-opaque-id"})
    @app.get("/v1/models")
    async def models(request: Request):
        case.calls.append((request.url.path, dict(request.headers), None))
        return {"object": "list", "data": []}
    case.http = TestClient(app, raise_server_exceptions=False)
    case.headers = {"x-ai-gateway-user-action": "1", "x-ai-gateway-csrf": "csrf"}
    yield case
    case.http.close()


def test_home_uses_allowed_capabilities_and_owned_last_five(home_case):
    c = home_case
    for i in range(7):
        receipt_store.record({"request_id": f"mine-{i}", "app_id": c.client_app["id"], "status": "succeeded"}, created_at=i + 10, path=c.receipts)
    receipt_store.record({"request_id": "other-person", "app_id": c.outsider["id"], "status": "succeeded"}, created_at=100, path=c.receipts)
    response = c.http.get("/me/api/home")
    assert response.status_code == 200
    data = response.json()
    assert len(data["receipts"]) == 5
    assert {row["request_id"] for row in data["receipts"]} == {f"mine-{i}" for i in range(2, 7)}
    assert [row["id"] for row in data["aliases"]] == ["fast"]
    assert all(row["status"] == "ready" for row in data["capabilities"])
    assert data["api_base_url"] == "https://gateway.example/v1"
    assert data["creations"] is None
    assert c.key not in response.text and "sha256:" not in response.text
    assert admin_store.get_credential(c.cred["id"], path=c.db)["last_used_at"] is None
    assert response.headers["cache-control"] == "no-store"


def test_home_unknown_health_is_not_green(home_case):
    home_case.health = {"backends": []}
    data = home_case.http.get("/me/api/home").json()
    assert data["status"]["state"] == "unavailable"
    assert not data["aliases"][0]["available"]
    assert "Not measured yet" in data["capabilities"][0]["reason"]


@pytest.mark.parametrize("state", ["busy", "starting"])
def test_home_status_preserves_observed_busy_or_starting(home_case, state):
    home_case.health = {"backends": [{"key": "service", "live_status": state}]}
    data = home_case.http.get("/me/api/home").json()
    assert data["status"]["state"] == state
    assert data["aliases"][0]["status"] == state
    assert not data["capabilities"][0]["available"]


def test_home_health_failure_is_an_error(home_case):
    home_case.health_error = True
    assert home_case.http.get("/me/api/home").status_code == 500


@pytest.mark.parametrize("path", ["/me/api/home", "/admin/api/setup-progress"])
def test_no_session_cannot_read_home_or_setup(home_case, path):
    home_case.logged_in = False
    assert home_case.http.get(path).status_code == 401


def test_connection_dispatches_once_and_returns_real_receipt(home_case):
    c = home_case
    response = c.http.post("/me/api/connection-test", json={"api_key": c.key, "credential_id": c.cred["id"]}, headers=c.headers)
    data = response.json()
    assert response.status_code == 200 and data["ok"]
    assert len(c.calls) == 1
    assert c.calls[0][0] == "/v1/chat/completions"
    assert c.calls[0][2]["max_tokens"] == 1 and c.calls[0][2]["stream"] is False
    assert data["request_id"] == "real-test"
    assert data["latency_ms"] >= 0
    assert receipt_store.get("real-test", path=c.receipts)["status"] == "succeeded"
    assert c.key not in response.text


def test_connection_without_fast_only_tests_models(home_case):
    c = home_case
    c.fast = False
    data = c.http.post("/me/api/connection-test", json={"api_key": c.key}, headers=c.headers).json()
    assert data["ok"] and data["model"] is None and data["request_id"] is None
    assert [call[0] for call in c.calls] == ["/v1/models"]
    assert "Chat was not tested" in data["reason"]


def test_connection_remote_or_unconfirmed_fast_never_generates(home_case):
    c = home_case
    c.registry = {"services": {"service": {"enabled": True, "config": {"models": [{"id": "fast", "upstream_model": "paid-model"}]}}}}
    data = c.http.post("/me/api/connection-test", json={"api_key": c.key}, headers=c.headers).json()
    assert data["ok"] and data["path"] == "/v1/models"
    assert [call[0] for call in c.calls] == ["/v1/models"]


@pytest.mark.parametrize("payload", [{"api_key": "other-key"}, {"api_key": "missing-key"}, {"api_key": "test-home-key", "credential_id": "wrong-id"}])
def test_connection_rejects_other_missing_and_mismatched_keys(home_case, payload):
    assert home_case.http.post("/me/api/connection-test", json=payload, headers=home_case.headers).status_code == 404
    assert home_case.calls == []


def test_connection_requires_session_and_csrf(home_case):
    c = home_case
    assert c.http.post("/me/api/connection-test", json={"api_key": c.key}).status_code == 403
    c.logged_in = False
    assert c.http.post("/me/api/connection-test", json={"api_key": c.key}, headers=c.headers).status_code == 401
    assert c.calls == []


def test_connection_rejects_revoked_and_expired_keys(home_case):
    c = home_case
    admin_store.revoke_credential(c.cred["id"], path=c.db)
    assert c.http.post("/me/api/connection-test", json={"api_key": c.key}, headers=c.headers).status_code == 404
    admin_store.create_credential_hash(subject_kind="app", subject_id=c.client_app["id"], hash=hash_api_key("expired"), prefix="masked", expires_at=1, path=c.db)
    assert c.http.post("/me/api/connection-test", json={"api_key": "expired"}, headers=c.headers).status_code == 404
    assert c.calls == []


def test_setup_is_owner_only_and_requires_observed_use(home_case):
    c = home_case
    c.owner = False
    assert c.http.get("/admin/api/setup-progress").status_code == 403
    c.owner = True
    data = c.http.get("/admin/api/setup-progress").json()
    assert not data["complete"]
    steps = {row["id"]: row for row in data["steps"]}
    assert steps["claim_owner"]["done"] and steps["connect_service"]["done"]
    assert not steps["connect_app"]["done"]
    c.http.post("/me/api/connection-test", json={"api_key": c.key}, headers=c.headers)
    assert c.http.get("/admin/api/setup-progress").json()["complete"]


def test_setup_default_app_used_does_not_complete_app_step(home_case):
    c = home_case
    cred = admin_store.create_credential_hash(subject_kind="app", subject_id=c.personal["id"], hash=hash_api_key("bootstrap"), prefix="masked", path=c.db)
    admin_store.touch_credential_last_used(cred["id"], path=c.db)
    admin_store.record_audit("app.self_create", actor_kind="user", target_kind="app", target_id=c.personal["id"], metadata={"via": "setup_local_owner"}, path=c.db)
    steps = c.http.get("/admin/api/setup-progress").json()["steps"]
    assert not next(step for step in steps if step["id"] == "connect_app")["done"]


def test_setup_explicit_first_app_can_complete_without_bootstrap(home_case):
    c = home_case
    admin_store.disable_app(c.personal["id"], path=c.db)
    admin_store.touch_credential_last_used(c.cred["id"], path=c.db)
    steps = c.http.get("/admin/api/setup-progress").json()["steps"]
    assert next(step for step in steps if step["id"] == "connect_app")["done"]


def test_setup_admin_created_personal_app_counts_when_used(home_case):
    c = home_case
    named = admin_store.create_app("Cursor", kind="personal", owner_user_id=c.user["id"], path=c.db)
    admin_store.record_audit("app.create", target_kind="app", target_id=named["id"], path=c.db)
    cred = admin_store.create_credential_hash(subject_kind="app", subject_id=named["id"], hash=hash_api_key("cursor-key"), prefix="masked", path=c.db)
    admin_store.touch_credential_last_used(cred["id"], path=c.db)
    steps = c.http.get("/admin/api/setup-progress").json()["steps"]
    assert next(step for step in steps if step["id"] == "connect_app")["done"]


def test_home_and_connection_use_credential_profile_override(home_case):
    c = home_case
    restricted = admin_store.create_profile("no-chat", {"id": "no-chat", "protocols": []}, path=c.db)
    c.prospective_profile = restricted["id"]
    admin_store.disable_app(c.personal["id"], path=c.db)
    admin_store.revoke_credential(c.cred["id"], path=c.db)
    admin_store.create_credential_hash(subject_kind="app", subject_id=c.client_app["id"], hash=hash_api_key("restricted-key"), prefix="masked", profile_id=restricted["id"], path=c.db)
    data = c.http.get("/me/api/home").json()
    assert data["capabilities"] == [] and data["aliases"] == []
    result = c.http.post("/me/api/connection-test", json={"api_key": "restricted-key"}, headers=c.headers).json()
    assert result["path"] == "/v1/models" and result["model"] is None
    assert [call[0] for call in c.calls] == ["/v1/models"]


def test_home_and_connection_use_live_app_inheritance_policy(home_case):
    c = home_case
    restricted = admin_store.create_profile("no-chat", {"id": "no-chat", "protocols": []}, path=c.db)
    c.inherited_profile = restricted["id"]
    c.prospective_profile = restricted["id"]
    data = c.http.get("/me/api/home").json()
    assert data["capabilities"] == [] and data["aliases"] == []
    result = c.http.post("/me/api/connection-test", json={"api_key": c.key}, headers=c.headers).json()
    assert result["path"] == "/v1/models"


def test_setup_receipt_success_is_not_limited_to_last_five(home_case):
    c = home_case
    receipt_store.record({"request_id": "old-success", "status": "succeeded"}, created_at=1, path=c.receipts)
    for i in range(10):
        receipt_store.record({"request_id": f"failure-{i}", "status": "failed"}, created_at=i + 2, path=c.receipts)
    steps = c.http.get("/admin/api/setup-progress").json()["steps"]
    assert next(step for step in steps if step["id"] == "test_request")["done"]


def test_real_alias_discovery_uses_concrete_policy_and_health_target():
    profile = load_profile({"id": "restricted", "protocols": ["openai"], "models": ["demo-chat"], "backends": ["local-service"]})
    identity = RuntimeIdentity(name="Test", key_hash="", profile=profile)
    aliases = build_alias_capabilities({"chat": {"stable": [{"alias": "fast", "resolves_to": "demo-chat"}]}})
    concrete = Capability(intent="chat.completions", backend="local-service", model="demo-chat", health="healthy")
    raw = [capability_with_policy_view(cap, profile, request=make_policy_request(protocol="openai.chat", intent=cap.intent, model=cap.model, backend=cap.backend)) for cap in [concrete, *aliases]]
    assert not raw[1].policy_available  # General catalog labels alias backend as gateway.
    fast = next(cap for cap in _resolved_capabilities(identity, lambda _identity: raw) if cap.model == "fast")
    assert fast.policy_available and fast.backend == "local-service" and fast.health == "healthy"
    registry = {"services": {"local-service": {"config": {"models": [{"id": "demo-chat", "local_lifecycle": True}]}}}}
    assert _declared_local_fast(fast, registry)
    assert not _declared_local_fast(fast, {"services": {"local-service": {"config": {}}}})


def test_alias_ambiguous_backends_fail_closed():
    identity = RuntimeIdentity(name="Test", key_hash="")
    aliases = build_alias_capabilities({"chat": {"stable": [{"alias": "fast", "resolves_to": "demo-chat"}]}})
    raw = [Capability(intent="chat.completions", backend=backend, model="demo-chat", health="healthy") for backend in ("one", "two")] + aliases
    fast = next(cap for cap in _resolved_capabilities(identity, lambda _identity: raw) if cap.model == "fast")
    assert not fast.policy_available


def test_alias_cannot_use_a_forbidden_resolved_backend():
    identity = RuntimeIdentity(name="Test", key_hash="", profile=load_profile({"id": "denied", "backends": ["different-service"]}))
    aliases = build_alias_capabilities({"chat": {"stable": [{"alias": "fast", "resolves_to": "demo-chat"}]}})
    raw = [Capability(intent="chat.completions", backend="forbidden-service", model="demo-chat", health="healthy"), *aliases]
    fast = next(cap for cap in _resolved_capabilities(identity, lambda _identity: raw) if cap.model == "fast")
    assert not fast.policy_available


def test_declared_healthy_capability_without_measured_service_is_unavailable():
    from gateway.home_api import _availability
    cap = Capability(intent="image.generate", backend="not-running", health="healthy")
    state, reason = _availability(cap, {})
    assert state == "unavailable" and "Not measured yet" in reason


def test_alias_resolves_registry_models_when_catalog_only_has_generic_endpoint():
    identity = RuntimeIdentity(name="Test", key_hash="", profile=load_profile({"id": "restricted", "models": ["demo-chat"], "backends": ["capture-engine"]}))
    aliases = build_alias_capabilities({"chat": {"stable": [{"alias": "fast", "resolves_to": "demo-chat"}]}})
    raw = [Capability(intent="chat.completions", backend="capture-engine"), *aliases]
    registry = {"services": {"capture-engine": {"enabled": True, "config": {"models": [{"id": "demo-chat", "upstream_model": "demo-chat", "local_lifecycle": True}]}}}}
    fast = next(cap for cap in _resolved_capabilities(identity, lambda _identity: raw, registry) if cap.model == "fast")
    assert fast.policy_available and fast.backend == "capture-engine"
    assert _declared_local_fast(fast, registry)


def test_connection_alias_precedence_never_uses_same_named_local_model(home_case):
    c = home_case
    views = {"chat": {"stable": [{"alias": "fast", "resolves_to": "paid-model"}]}}
    c.aliases_override = views
    c.registry = {"services": {
        "service": {"enabled": True, "config": {"gateway_lifecycle": True, "models": [{"id": "fast"}]}},
        "remote": {"enabled": True, "config": {"models": [{"id": "paid-model"}]}}
    }}
    c.capabilities_override = lambda _identity: [
        Capability(intent="chat.completions", backend="service", model="fast"),
        Capability(intent="chat.completions", backend="remote", model="paid-model"),
        *build_alias_capabilities(views),
    ]
    result = c.http.post("/me/api/connection-test", json={"api_key": c.key}, headers=c.headers).json()
    assert result["ok"] and result["path"] == "/v1/models"
    assert [call[0] for call in c.calls] == ["/v1/models"]
    data = c.http.get("/me/api/home").json()
    assert not data["aliases"][0]["available"]  # The healthy same-named local model is shadowed.


def test_workflow_and_service_aliases_preserve_permissions():
    identity = RuntimeIdentity(name="Test", key_hash="")
    views = {
        "images": {"stable": [{"alias": "image", "resolves_to": "draw"}]},
        "speech": {"stable": [{"alias": "tts", "resolves_to": "voice-option", "engine": "voice-option", "service": "voice-service"}]},
    }
    workflow = Capability(intent="image.generate", backend="image-service", workflow="draw")
    raw = [workflow, *build_alias_capabilities(views)]
    registry = {"services": {"image-service": {"enabled": True}, "voice-service": {"enabled": True}}}
    caps = _resolved_capabilities(identity, lambda _identity: raw, registry)
    aliases = {cap.model: cap for cap in caps if "alias" in cap.tags}
    assert aliases["image"].policy_available and aliases["image"].backend == "image-service"
    assert aliases["tts"].policy_available and aliases["tts"].backend == "voice-service"
    denied = RuntimeIdentity(name="Denied", key_hash="", profile=load_profile({"id": "deny", "backends": []}))
    denied_raw = [capability_with_policy_view(cap, denied.profile, request=make_policy_request(protocol="openai.audio", intent=cap.intent, model=cap.model, backend=cap.backend)) for cap in raw]
    assert not any(cap.policy_available for cap in _resolved_capabilities(denied, lambda _identity: denied_raw, registry) if "alias" in cap.tags)


def test_recipe_aliases_are_credential_scoped_and_new_app_uses_default(home_case):
    c = home_case
    restricted = admin_store.create_profile("no-chat", {"id": "no-chat", "protocols": []}, path=c.db)
    c.prospective_profile = restricted["id"]
    narrow = admin_store.create_credential_hash(subject_kind="app", subject_id=c.client_app["id"], hash=hash_api_key("narrow-key"), prefix="masked", profile_id=restricted["id"], path=c.db)
    data = c.http.get("/me/api/home").json()
    assert [alias["id"] for alias in data["aliases"]] == ["fast"]
    assert [alias["id"] for alias in data["credential_aliases"][c.cred["id"]]] == ["fast"]
    assert data["credential_aliases"][narrow["id"]] == []
    assert data["new_app_aliases"] == []
    assert c.other_cred["id"] not in data["credential_aliases"]


def test_capability_union_includes_available_new_app_default(home_case):
    c = home_case
    restricted = admin_store.create_profile("no-chat", {"id": "no-chat", "protocols": []}, path=c.db)
    c.inherited_profile = restricted["id"]
    data = c.http.get("/me/api/home").json()
    assert data["credential_aliases"][c.cred["id"]] == []
    assert [alias["id"] for alias in data["new_app_aliases"]] == ["fast"]
    assert data["capabilities"] and data["aliases"]
    c.self_service = False
    data = c.http.get("/me/api/home").json()
    assert data["new_app_aliases"] == [] and data["aliases"] == []


def test_public_chat_header_returns_own_durable_receipt_not_upstream_id(monkeypatch):
    import proxy
    from gateway.policy import DEFAULT_PROFILE

    class Client:
        async def aclose(self):
            pass

    class Upstream:
        status_code = 200
        headers = {"content-type": "application/json", "x-ai-gateway-request-id": "spoofed-upstream", "request-id": "provider-opaque"}
        async def aread(self):
            return b'{"choices":[{"message":{"role":"assistant","content":"Hi"},"finish_reason":"stop"}]}'
        async def aclose(self):
            pass

    captured = []
    async def send(request, **_kwargs):
        captured.append(request)
        return Client(), Upstream()
    async def prepare(_auth_context, **kwargs):
        return None, kwargs["body_json"]

    identity = RuntimeIdentity(name="Header test", key_hash=hash_api_key("header-test"), profile=DEFAULT_PROFILE, app_id="header-app")
    monkeypatch.setattr(proxy, "AUTH_ENABLED", True)
    monkeypatch.setattr(proxy, "runtime_identity_for_app_credential_hash", lambda *_args, **_kwargs: identity)
    monkeypatch.setattr(proxy.ADMIN_STORE, "get_service_registry", lambda: {"services": {
        "litellm": {"id": "litellm", "kind": "llm-substrate", "enabled": True,
                    "capabilities": ["llm.execution"], "config": {"base_url": "http://unit-test.invalid", "api_key_env": "HOME_HEADER_MASTER"}}
    }})
    monkeypatch.setenv("HOME_HEADER_MASTER", "unit-test-master")
    monkeypatch.setattr(proxy, "send_llm_execution_request", send)
    monkeypatch.setattr(proxy, "_prepare_litellm_local_llm_request", prepare)
    monkeypatch.setitem(proxy.config, "model_aliases", {})
    client = TestClient(proxy.app)
    try:
        response = client.post("/v1/chat/completions", headers={"authorization": "Bearer header-test"},
                               json={"model": "demo-chat", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 1})
    finally:
        client.close()
    assert response.status_code == 200, response.text
    request_id = response.headers["x-ai-gateway-request-id"]
    assert request_id not in {"spoofed-upstream", "provider-opaque"} and "," not in request_id
    assert response.headers["request-id"] == "provider-opaque"
    receipt = receipt_store.get(request_id)
    assert receipt["status"] == "succeeded" and receipt["app_id"] == "header-app"
    assert len(captured) == 1
