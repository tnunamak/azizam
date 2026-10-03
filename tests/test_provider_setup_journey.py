"""Provider setup uses the same durable registry and paid admission as execution."""

from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from gateway import admin_store, providers
from gateway.admin_api import AdminContext
from gateway.admin_runtime import AdminSQLiteStore
from gateway.litellm_substrate import compile_litellm_config_from_registry
from gateway.profiles import load_profile
from gateway.provider_admin import create_provider_admin_router
from gateway.provider_sidecar import build_sidecar_env


OWNER = AdminContext(subject="test-owner", roles=frozenset({"owner"}))


def _owner(_request: Request) -> AdminContext:
    return OWNER


def test_ui_key_model_price_and_plan_survive_into_paid_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "admin.db"
    monkeypatch.setattr(admin_store, "DB_PATH", db)
    store = AdminSQLiteStore(db)
    store.init_db()
    profile_doc = {
        "id": "family",
        "protocols": ["openai"],
        "remote": {"providers": [], "budget_usd_per_day": "2.00"},
    }
    plan = store.create_profile(data={"name": "Family", "profile": profile_doc}, actor=OWNER)
    app = FastAPI()
    app.include_router(create_provider_admin_router(store, auth=_owner))
    client = TestClient(app)
    created = client.post("/admin/api/providers", json={
        "preset_id": "openrouter",
        "provider_id": "cloud-test",
        "key_mode": "secret",
        "api_key": "synthetic-provider-key-4f2a",
        "models": [{
            "id": "test/model",
            "upstream_model": "test/model",
            "enabled": True,
            "price": {
                "input_usd_per_million_tokens": "3.00",
                "output_usd_per_million_tokens": "15.00",
            },
        }],
        "allowed_plans": [],
        "caps": {"daily_usd": 5, "monthly_usd": 50},
    })
    assert created.status_code == 201, created.json()
    # Rotation uses the same API regardless of how the initial key was submitted.
    rotated = client.put("/admin/api/providers/cloud-test/key", json={
        "api_key": "synthetic-provider-key-4f2a",
    })
    assert rotated.status_code == 200, rotated.json()
    listing = client.get("/admin/api/providers").json()
    assert "synthetic-provider-key-4f2a" not in str(listing)
    assert "synthetic-provider-key-4f2a" not in str(created.json())
    assert "synthetic-provider-key-4f2a" not in str(admin_store.list_audit(path=db))
    registry = store.get_service_registry()
    compiled = compile_litellm_config_from_registry(registry)
    entry = next(row for row in compiled["model_list"] if row["model_name"] == "cloud-test/test/model")
    assert entry["litellm_params"]["api_key"].startswith("os.environ/")
    assert "synthetic-provider-key-4f2a" not in (tmp_path / "litellm.config.yaml").read_text()
    assert "synthetic-provider-key-4f2a" not in str(compiled)
    child_env = build_sidecar_env(db_path=db, base_env={})
    assert child_env[entry["litellm_params"]["api_key"].removeprefix("os.environ/")] == "synthetic-provider-key-4f2a"
    provider = providers.provider_lookup(registry, "cloud-test")
    assert provider is not None
    denied_plan = load_profile(profile_doc)
    estimate = providers.estimate_chat_cost(provider, {
        "model": "cloud-test/test/model",
        "messages": [{"role": "user", "content": "Check paid admission."}],
        "max_tokens": 100,
    })
    assert estimate.usd is not None and estimate.usd > 0
    ledger = tmp_path / "spend.db"
    with pytest.raises(providers.RemoteProviderNotAllowed):
        providers.reserve_provider_spend(denied_plan, provider, estimate, path=ledger, admin_db_path=db)
    allowed = client.put("/admin/api/providers/cloud-test/allowed-plans", json={"allowed_plans": [plan["id"]]})
    assert allowed.status_code == 200, allowed.json()
    persisted = store.get_profile(plan["id"])
    assert persisted is not None
    allowed_plan = load_profile(persisted["profile"])
    assert "cloud-test" in allowed_plan.remote.providers
    assert allowed_plan.remote.budget_usd_per_day == Decimal("2.00")
    reservation = providers.reserve_provider_spend(allowed_plan, provider, estimate, path=ledger, admin_db_path=db)
    assert reservation.estimated_usd == estimate.usd
    assert reservation.provider == "cloud-test"
    deleted = client.delete("/admin/api/providers/cloud-test/key")
    assert deleted.status_code == 200
    provider = providers.provider_lookup(store.get_service_registry(), "cloud-test")
    assert provider is not None
    with pytest.raises(providers.ProviderKeyMissing):
        providers.require_remote_allowed(allowed_plan, provider)


@pytest.mark.parametrize("path", [
    "/admin/api/providers", "/admin/api/providers/-/catalog",
    "/admin/api/providers/cloud-test", "/admin/api/providers/cloud-test/key",
])
def test_provider_owner_data_is_hidden_from_members(tmp_path: Path, path: str) -> None:
    store = AdminSQLiteStore(tmp_path / "admin.db")
    store.init_db()
    member = AdminContext(subject="member", roles=frozenset({"viewer"}))
    app = FastAPI()
    app.include_router(create_provider_admin_router(store, auth=lambda _request: member))
    with TestClient(app) as client:
        assert client.get(path).status_code == 403


@pytest.mark.parametrize(("method", "path"), [
    ("POST", "/admin/api/providers"),
    ("POST", "/admin/api/providers/-/test"),
    ("PUT", "/admin/api/providers/cloud-test/key"),
    ("DELETE", "/admin/api/providers/cloud-test/key"),
    ("PUT", "/admin/api/providers/cloud-test/models"),
    ("DELETE", "/admin/api/providers/cloud-test"),
])
def test_provider_mutations_reject_a_session_without_csrf(
    tmp_path: Path, method: str, path: str,
) -> None:
    store = AdminSQLiteStore(tmp_path / "admin.db")
    store.init_db()
    owner_session = AdminContext(
        subject="owner", roles=frozenset({"owner"}),
        auth_method="session", csrf_token="test-csrf-token",
    )
    app = FastAPI()
    app.include_router(create_provider_admin_router(store, auth=lambda _request: owner_session))
    with TestClient(app) as client:
        response = client.request(method, path, json={"api_key": "synthetic-provider-key-4f2a"})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "admin_csrf_required"
    assert store.get_service_registry()["services"] == {}
    assert not (tmp_path / "provider-secrets.key").exists()
