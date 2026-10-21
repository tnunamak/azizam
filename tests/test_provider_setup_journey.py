"""Provider setup uses the same durable registry and paid admission as execution."""

from decimal import Decimal
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from gateway import admin_store, providers
from gateway.admin_api import AdminContext
from gateway.admin_runtime import AdminSQLiteStore
from gateway.litellm_substrate import compile_litellm_config_from_registry
from gateway.access_plans import plan_from_document
from gateway.provider_admin import create_provider_admin_router
from gateway.provider_sidecar import build_sidecar_env
from gateway.secret_store import get_provider_secret
from tests.support.access import identity, plan_document


OWNER = AdminContext(subject="test-owner", roles=frozenset({"owner"}))


def _owner(_request: Request) -> AdminContext:
    return OWNER


def _app_on(store: AdminSQLiteStore, plan_id: str):
    """An App whose two plans are the stored plan, as the runtime reads it."""
    record = store.get_access_plan(plan_id)
    assert record is not None
    return identity(plan_from_document(record["plan"], plan_id=plan_id))


def _paid(store: AdminSQLiteStore, plan_id: str) -> list[str]:
    return store.get_access_plan(plan_id)["plan"]["paid_providers"]


def test_ui_key_model_price_and_plan_survive_into_paid_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "admin.db"
    monkeypatch.setattr(admin_store, "DB_PATH", db)
    store = AdminSQLiteStore(db)
    store.init_db()
    family = plan_document("Family", caps=("chat",))
    plan = store.create_access_plan(data=family, actor=OWNER)
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
                "cache_read_input_usd_per_million_tokens": "0.30",
                "dimensions": {"cache_control": True},
            },
        }],
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
    denied_app = _app_on(store, plan["id"])
    estimate = providers.estimate_chat_cost(provider, {
        "model": "cloud-test/test/model",
        "messages": [{"role": "user", "content": "Check paid admission."}],
        "max_tokens": 100,
    })
    assert estimate.usd is not None and estimate.usd > 0
    ledger = tmp_path / "spend.db"
    with pytest.raises(providers.RemoteProviderNotAllowed):
        providers.reserve_provider_spend(denied_app, provider, estimate, path=ledger, admin_db_path=db)
    # The provider page lists plans read-only; the plan editor adds a paid provider.
    store.update_access_plan(plan_id=plan["id"], data={**family, "paid_providers": ["cloud-test"]}, actor=OWNER)
    listed = client.get("/admin/api/providers/cloud-test").json()["provider"]["access_plans"]
    assert [row["id"] for row in listed] == [plan["id"]]
    allowed_app = _app_on(store, plan["id"])
    reservation = providers.reserve_provider_spend(allowed_app, provider, estimate, path=ledger, admin_db_path=db)
    assert reservation.estimated_usd == estimate.usd
    assert reservation.provider == "cloud-test"
    deleted = client.delete("/admin/api/providers/cloud-test/key")
    assert deleted.status_code == 200
    provider = providers.provider_lookup(store.get_service_registry(), "cloud-test")
    assert provider is not None
    with pytest.raises(providers.ProviderKeyMissing):
        providers.require_remote_allowed(allowed_app, provider)
    # Exercise the real keyword-only store update/remove contracts, including
    # disabled models and lossless preservation of credential references.
    updated = client.put("/admin/api/providers/cloud-test/models", json={"models": [{
        "id": "cloud-test/test/model", "upstream_model": "test/model", "enabled": False,
        "price": {"input_usd_per_million_tokens": "3", "output_usd_per_million_tokens": "15"},
    }]})
    assert updated.status_code == 200, updated.json()
    assert updated.json()["provider"]["models"][0]["enabled"] is False
    assert not compile_litellm_config_from_registry(store.get_service_registry())["model_list"]
    assert store.get_service_registry()["services"]["cloud-test"]["config"]["api_key_secret_id"]
    preserved_price = store.get_service_registry()["services"]["cloud-test"]["config"]["price"]["models"]["cloud-test/test/model"]
    assert preserved_price["cache_read_input_usd_per_million_tokens"] == "0.30"
    assert preserved_price["dimensions"] == {"cache_control": True}
    removed = client.delete("/admin/api/providers/cloud-test")
    assert removed.status_code == 200, removed.json()
    assert client.get("/admin/api/providers/cloud-test").status_code == 404
    assert "cloud-test" not in _paid(store, plan["id"])


def test_setup_and_removal_share_the_paid_admission_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "admin.db"
    ledger = tmp_path / "spend.db"
    monkeypatch.setattr(admin_store, "DB_PATH", db)
    store = AdminSQLiteStore(db)
    store.init_db()
    plan = store.create_access_plan(data=plan_document("Journey", caps=("chat",), paid=["journey-cloud"]), actor=OWNER)
    app = FastAPI()
    app.include_router(create_provider_admin_router(
        store, auth=_owner, ledger_path=ledger, receipts_path=tmp_path / "receipts.db",
    ))
    with TestClient(app) as client:
        created = client.post("/admin/api/providers", json={
            "preset_id": "openrouter",
            "provider_id": "journey-cloud",
            "key_mode": "secret",
            "api_key": "synthetic-journey-key",
            "models": [{
                "id": "test/model",
                "upstream_model": "test/model",
                "enabled": True,
                "price": {
                    "input_usd_per_million_tokens": "1",
                    "output_usd_per_million_tokens": "1",
                },
            }],
        })
        assert created.status_code == 201, created.json()
        provider = providers.provider_lookup(store.get_service_registry(), "journey-cloud")
        assert provider is not None
        reservation = providers.reserve_provider_spend(
            _app_on(store, plan["id"]), provider,
            providers.Cost(Decimal("0.001"), "price-table", provider.id),
            path=ledger, admin_db_path=db,
        )

        blocked = client.delete("/admin/api/providers/journey-cloud")
        assert blocked.status_code == 409, blocked.json()
        assert blocked.json()["error"]["code"] == "provider_spend_pending"
        assert "journey-cloud" in store.get_service_registry()["services"]

        providers.release_provider_spend(reservation, path=ledger)
        removed = client.delete("/admin/api/providers/journey-cloud")
        assert removed.status_code == 200, removed.json()
        assert client.get("/admin/api/providers/journey-cloud").status_code == 404
        assert "journey-cloud" not in _paid(store, plan["id"])


@pytest.mark.parametrize("rate", ["NaN", "Infinity", "-1", True, ""])
def test_invalid_paid_prices_fail_before_secret_or_registry_writes(tmp_path: Path, rate: object) -> None:
    db = tmp_path / "admin.db"
    store = AdminSQLiteStore(db)
    store.init_db()
    app = FastAPI()
    app.include_router(create_provider_admin_router(store, auth=_owner))
    with TestClient(app) as client:
        result = client.post("/admin/api/providers", json={
            "preset_id": "openai", "api_key": "synthetic-price-key",
            "models": [{"id": "test-model", "enabled": True, "price": {
                "input_usd_per_million_tokens": rate, "output_usd_per_million_tokens": "1",
            }}],
        })
    assert result.status_code == 400
    assert result.json()["error"]["code"] == "missing_model_price"
    assert not store.get_service_registry()["services"]
    assert not (tmp_path / "provider-secrets.key").exists()


def test_concurrent_provider_creates_cannot_delete_the_winning_key(tmp_path: Path) -> None:
    db = tmp_path / "admin.db"
    store = AdminSQLiteStore(db)
    store.init_db()
    app = FastAPI()
    app.include_router(create_provider_admin_router(store, auth=_owner))
    def create(index: int):
        with TestClient(app) as client:
            return index, client.post("/admin/api/providers", json={
                "preset_id": "openai", "provider_id": "same-provider", "models": [],
                "api_key": f"synthetic-concurrent-key-{index}",
            })
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    assert sorted(result.status_code for _, result in results) == [201, 409]
    winner = next(index for index, result in results if result.status_code == 201)
    config = store.get_service_registry()["services"]["same-provider"]["config"]
    assert get_provider_secret(config["api_key_secret_id"], db_path=db) == f"synthetic-concurrent-key-{winner}"


def test_existing_canonical_provider_under_legacy_row_cannot_be_replaced(tmp_path: Path) -> None:
    db = tmp_path / "admin.db"
    store = AdminSQLiteStore(db)
    store.init_db()
    app = FastAPI()
    app.include_router(create_provider_admin_router(store, auth=_owner))
    with TestClient(app) as client:
        body = {"preset_id": "openai", "provider_id": "acme", "models": [],
                "api_key": "synthetic-original-key"}
        original = client.post("/admin/api/providers", json=body)
        assert original.status_code == 201, original.json()
        record = admin_store.get_service_registry_with_generation(path=db)
        registry = record["value"]
        service = registry["services"].pop("acme")
        service["id"] = "legacy-row"
        registry["services"]["legacy-row"] = service
        admin_store.write_service_registry(registry, path=db,
                                          expected_generation=record["operator_settings_generation"])
        conflicting = client.post("/admin/api/providers", json={
            **body, "api_key": "synthetic-replacement-key",
        })
        assert conflicting.status_code == 409, conflicting.json()
        assert get_provider_secret("provider:acme:api_key", db_path=db) == "synthetic-original-key"
        assert set(store.get_service_registry()["services"]) == {"legacy-row"}


@pytest.mark.parametrize("path", [
    "/admin/api/providers", "/admin/api/providers/-/catalog",
    "/admin/api/providers/cloud-test",
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
