"""Catalog identities must agree with policy requests from real HTTP routes."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import proxy
from gateway.admin_api import AdminAuthError, AdminContext
from gateway.category_api import create_category_router
from gateway import category_enforcement
from test_route_policy_inventory import gateway, _activate_decoy_snapshot


@pytest.mark.parametrize("role,status", [("owner", 200), ("admin", 403), ("member", 403), ("anonymous", 401)])
def test_catalog_owner_only_without_snapshot(tmp_path, role, status):
    calls = []
    rows = [{"stem": "image.example", "label": "Example image", "kind": "workflow",
             "capabilities": ["image.generate"]}]

    def auth(request):
        if role == "anonymous":
            raise AdminAuthError("Sign in", status_code=401)
        return AdminContext(subject="tester", roles=frozenset({role}))

    def catalog():
        calls.append(True)
        return rows

    path = tmp_path / "categories.db"
    app = FastAPI()
    app.include_router(create_category_router(auth=auth, store_path=path, service_catalog=catalog))
    with TestClient(app) as client:
        response = client.get("/admin/api/categories/services")
        assert response.status_code == status
        if status == 200:
            assert response.json() == {"services": rows}
            rows.append({"stem": "new-model", "label": "New model", "kind": "model",
                         "capabilities": ["chat.completions"]})
            assert client.get("/admin/api/categories/services").json() == {"services": rows}
            assert len(calls) == 2
        else:
            assert calls == []
    assert not path.exists()


@pytest.mark.parametrize("gateway", ["categories"], indirect=True)
def test_catalog_stems_match_real_image_and_aliased_chat_requests(gateway, monkeypatch):
    monkeypatch.setitem(proxy.config, "model_aliases", {"chat": {"stable": {"friendly-chat": "catalog-chat"}}})
    monkeypatch.setattr(proxy.ADMIN_STORE, "get_service_registry", lambda: {"services": {
        "example": {"enabled": True, "capabilities": ["chat", "embeddings"], "config": {
            "base_url": "http://example.invalid/v1", "models": [{"id": "catalog-chat"}],
        }},
    }})
    _activate_decoy_snapshot(gateway, {"chat.completions", "image.generate"})
    seen = []
    check = category_enforcement.CategoryGate.check

    def capture(self, identity, request):
        seen.append(request)
        return check(self, identity, request)

    monkeypatch.setattr(category_enforcement.CategoryGate, "check", capture)
    # Use the actual proxy router and owner auth, not a separate catalog fixture.
    # Special methods are resolved on the class.
    monkeypatch.setattr(type(proxy.ADMIN_AUTH), "__call__", lambda self, request: AdminContext(subject="owner", roles=frozenset({"owner"})))
    response = gateway.client.get("/admin/api/categories/services", headers=gateway.headers)
    assert response.status_code == 200, response.text
    catalog = {entry["stem"]: entry for entry in response.json()["services"]}
    for path, body, intent, expected in [
        ("/v1/images/generations", {"model": "flux2-klein-4b", "prompt": "test"},
         "image.generate", "image.generate.flux2-klein-4b"),
        ("/v1/chat/completions", {"model": "friendly-chat", "messages": [{"role": "user", "content": "test"}]},
         "chat.completions", "catalog-chat"),
    ]:
        seen.clear()
        result = gateway.client.post(path, json=body, headers=gateway.headers)
        assert result.status_code == 403, result.text
        targets = {request.workflow or request.model for request in seen if request.intent == intent}
        assert expected in targets, targets
        assert expected in catalog
        assert intent in catalog[expected]["capabilities"]
    assert catalog["catalog-chat"]["kind"] == "model"
    assert catalog["image.generate.flux2-klein-4b"]["kind"] == "workflow"
    assert "friendly-chat" not in catalog
    assert gateway.calls == []


def test_catalog_uses_public_routes_and_omits_disabled_models(monkeypatch):
    registry = {"services": {
        "route": {"id": "route", "enabled": True, "capabilities": ["chat"], "config": {
            "protocol": "openai-compatible", "base_url": "http://example.invalid/v1",
            "models": [{"id": "enabled-model", "display_name": "Reviewed chat"},
                       {"id": "disabled-model", "enabled": False}],
        }},
        "cloud-route": {"id": "cloud-route", "kind": "provider", "enabled": True, "capabilities": ["decisions"], "config": {
            "remote_provider": True, "provider_id": "example-provider", "api_key_env": "CATALOG_TEST_KEY",
            "protocol": "openai-compatible", "base_url": "https://example.invalid/v1",
            "models": [{"id": "cloud-model"}],
        }},
        "disabled-service": {"id": "disabled-service", "enabled": False, "capabilities": ["chat"],
                             "config": {"models": [{"id": "offline-model"}]}},
    }}
    monkeypatch.setattr(proxy.ADMIN_STORE, "get_service_registry", lambda: registry)
    monkeypatch.setattr(proxy, "_gateway_aliases_provider", lambda: {})
    monkeypatch.setattr(proxy, "config", {})
    entries = {row["stem"]: row for row in proxy._category_service_catalog()}
    assert entries["enabled-model"] == {"stem": "enabled-model", "label": "Reviewed chat",
                                         "kind": "model", "capabilities": ["chat.completions"]}
    assert "example-provider/cloud-model" in entries
    assert "decision.systemone" in entries["example-provider/cloud-model"]["capabilities"]
    assert "cloud-model" not in entries
    assert "disabled-model" not in entries
    assert "offline-model" not in entries


def test_catalog_includes_protocol_only_chat_and_decision_targets(monkeypatch):
    from gateway.capabilities import Capability

    monkeypatch.setattr(proxy.ADMIN_STORE, "get_service_registry", lambda: {"services": {
        "protocol-only": {"enabled": True, "config": {
            "protocol": "openai-compatible", "base_url": "http://example.invalid/v1",
            "models": [{"id": "protocol-chat"}],
        }},
    }})
    monkeypatch.setattr(proxy, "_gateway_aliases_provider", lambda: {})
    monkeypatch.setattr(proxy, "config", {})
    monkeypatch.setattr(proxy.DECISION_RUNTIME, "capabilities", lambda: [
        Capability(intent="decision.systemone", model="choose", backend="decision-service"),
    ])
    entries = {row["stem"]: row for row in proxy._category_service_catalog()}
    assert entries["protocol-chat"]["capabilities"] == ["chat.completions"]
    assert entries["choose"] == {"stem": "choose", "label": "choose", "kind": "model",
                                "capabilities": ["decision.systemone"]}
