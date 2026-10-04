"""LAN client placeholders preserve anonymous access without exempting real keys."""
from __future__ import annotations

import ipaddress

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.requests import Request
from starlette.responses import Response

import proxy
from gateway import admin_store
from gateway.admin_api import AdminAuthError, AdminContext
from gateway.admin_oidc import AdminSession
from gateway.settings_api import create_settings_router
from gateway.user_runtime import mint_user_app_key


PLACEHOLDERS = ["lan", "none", "sk-local", "dummy", "EMPTY", "not-needed"]


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", path)
    monkeypatch.setattr(proxy, "AUTH_ENABLED", True)
    monkeypatch.setattr(proxy, "AUTH_PUBLIC_PATHS", ["/health"])
    monkeypatch.setattr(proxy, "LAN_BYPASS_ENABLED", True)
    monkeypatch.setattr(proxy, "LAN_BYPASS_CIDRS", [ipaddress.ip_network("192.168.0.0/16")])
    monkeypatch.setattr(proxy, "TRUSTED_PROXIES", [ipaddress.ip_network("192.168.1.4/32")])
    monkeypatch.setattr(proxy, "CLOUDFLARE_MODE", False)
    monkeypatch.setattr(proxy, "ALLOW_QUERY_PARAM_KEY", False)
    monkeypatch.setattr(proxy, "PATH_TOKENS", {})
    return path


def request(key, *, peer="192.168.1.25", forwarded=None, header="authorization"):
    headers = [(b"host", b"gateway.example")]
    if key is not None:
        value = f"Bearer {key}" if header == "authorization" else key
        headers.append((header.encode(), value.encode()))
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    return Request({
        "type": "http", "method": "POST", "scheme": "https", "path": "/v1/chat/completions",
        "query_string": b"", "headers": headers, "client": (peer, 41000),
        "server": ("gateway.example", 443),
    })


@pytest.mark.parametrize("key", PLACEHOLDERS)
@pytest.mark.parametrize("header", ["authorization", "x-api-key"])
def test_default_placeholder_on_lan_is_anonymous(gateway, key, header):
    auth = proxy.authenticate_request(request(key, header=header), "/v1/chat/completions")
    assert isinstance(auth, proxy.AuthContext)
    assert auth.identity.name == "lan-bypass"


@pytest.mark.parametrize("key", PLACEHOLDERS)
def test_placeholder_public_is_invalid_even_on_delegated_routes(gateway, key):
    public = request(key, peer="203.0.113.10")
    auth = proxy.authenticate_request(public, "/v1/chat/completions")
    assert isinstance(auth, Response)
    assert auth.status_code == 401
    delegated = proxy.authenticate_runtime_or_litellm_request(public, "/v1/chat/completions", allow_litellm_delegation=True)
    assert isinstance(delegated, Response)
    assert delegated.status_code == 401


@pytest.mark.parametrize("forwarded", [None, "garbage", "192.168.1.25, 203.0.113.10"])
def test_placeholder_cannot_use_unresolved_or_public_proxy_peer(gateway, forwarded):
    auth = proxy.authenticate_request(request("lan", peer="192.168.1.4", forwarded=forwarded), "/v1/models")
    assert isinstance(auth, Response)
    assert auth.status_code == 401


def test_forwarded_real_lan_client_can_use_placeholder(gateway):
    auth = proxy.authenticate_request(request("lan", peer="192.168.1.4", forwarded="192.168.1.25"), "/v1/models")
    assert isinstance(auth, proxy.AuthContext)
    assert auth.identity.name == "lan-bypass"


def test_placeholder_does_not_enable_disabled_lan_access(gateway, monkeypatch):
    monkeypatch.setattr(proxy, "LAN_BYPASS_ENABLED", False)
    auth = proxy.authenticate_request(request("lan"), "/v1/models")
    assert isinstance(auth, Response)
    assert auth.status_code == 401


def test_valid_member_key_and_revocation_keep_identity_boundary(gateway):
    user = admin_store.upsert_oidc_user("local", "member", email="member@example.test", path=gateway)
    app = admin_store.create_app("Member access", kind="personal", owner_user_id=user["id"], path=gateway)
    session = AdminSession(subject=user["id"], email=user["email"], role="none", issued_at=1, expires_at=4_000_000_000)
    key = mint_user_app_key(session, app["id"], path=gateway)
    member = request(key["api_key"])
    auth = proxy.authenticate_request(member, "/v1/models")
    assert isinstance(auth, proxy.AuthContext)
    assert auth.identity.app_id == app["id"]
    assert auth.identity.name != "lan-bypass"
    admin_store.revoke_credential(key["id"], path=gateway)
    revoked = proxy.authenticate_request(member, "/v1/models")
    assert isinstance(revoked, Response)
    assert revoked.status_code == 401


def test_owner_edit_applies_live_to_lan_and_preserves_public_denial(gateway):
    def owner(req):
        if req.headers.get("test-owner") != "1":
            raise AdminAuthError()
        return AdminContext(subject="owner", roles=frozenset({"owner"}), auth_method="session", csrf_token="csrf-test")

    app = FastAPI()
    app.include_router(create_settings_router(auth=owner, admin_path=gateway, config_provider=lambda: {}))
    client = TestClient(app)
    url = "/admin/api/settings/lan-placeholder-keys"
    headers = {"test-owner": "1", "x-ai-gateway-csrf": "csrf-test"}
    assert client.get(url, headers=headers).json()["lan_placeholder_keys"] == PLACEHOLDERS
    assert client.put(url, headers=headers, json={"lan_placeholder_keys": ["client-placeholder"]}).status_code == 200
    custom = proxy.authenticate_request(request("client-placeholder"), "/v1/models")
    assert isinstance(custom, proxy.AuthContext)
    assert custom.identity.name == "lan-bypass"
    for raw in (request("lan"), request("client-placeholder", peer="203.0.113.10")):
        denied = proxy.authenticate_request(raw, "/v1/models")
        assert isinstance(denied, Response)
        assert denied.status_code == 401
    assert client.put(url, headers=headers, json={"lan_placeholder_keys": []}).status_code == 200
    disabled = proxy.authenticate_request(request("client-placeholder"), "/v1/models")
    assert isinstance(disabled, Response)
    assert disabled.status_code == 401


def test_lan_sk_local_is_absent_for_virtual_key_policy(gateway):
    assert proxy._request_uses_litellm_virtual_key(request("sk-local")) is False


def test_placeholder_matching_is_case_sensitive(gateway):
    denied = proxy.authenticate_request(request("empty"), "/v1/models")
    assert isinstance(denied, Response)
    assert denied.status_code == 401


@pytest.mark.parametrize("policy", [{"lan_placeholder_keys": []}, {"lan_placeholder_keys": "invalid"}])
def test_public_sk_local_never_delegates_when_policy_changes(gateway, policy):
    admin_store.put_operator_setting("lan_placeholder_keys", policy, path=gateway)
    auth = proxy.authenticate_runtime_or_litellm_request(
        request("sk-local", peer="203.0.113.10"), "/v1/chat/completions", allow_litellm_delegation=True,
    )
    assert isinstance(auth, Response)
    assert auth.status_code == 401


@pytest.mark.parametrize("source", ["x-api-key", "api-key", "query"])
def test_placeholder_does_not_hide_valid_or_revoked_member_key(gateway, monkeypatch, source):
    user = admin_store.upsert_oidc_user("local", "member", email="member@example.test", path=gateway)
    app = admin_store.create_app("Member access", kind="personal", owner_user_id=user["id"], path=gateway)
    session = AdminSession(subject=user["id"], email=user["email"], role="none", issued_at=1, expires_at=4_000_000_000)
    key = mint_user_app_key(session, app["id"], path=gateway)
    mixed = request("dummy")
    if source == "query":
        monkeypatch.setattr(proxy, "ALLOW_QUERY_PARAM_KEY", True)
        mixed.scope["query_string"] = f"api_key={key['api_key']}".encode()
    else:
        mixed.scope["headers"].append((source.encode(), key["api_key"].encode()))
    auth = proxy.authenticate_request(mixed, "/v1/models")
    assert isinstance(auth, proxy.AuthContext)
    assert auth.identity.app_id == app["id"]
    admin_store.revoke_credential(key["id"], path=gateway)
    denied = proxy.authenticate_request(mixed, "/v1/models")
    assert isinstance(denied, Response)
    assert denied.status_code == 401
