"""Provider admin gap contracts (azgap gw-providers): guard matrix, fail-closed removal,
stale registry generation, stale failed probes. Reuses helpers from test_provider_admin."""
from __future__ import annotations

import copy
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.admin_api import AdminContext  # noqa: E402
from gateway.provider_admin import create_provider_admin_router  # noqa: E402
from gateway.provider_listing import ProviderListingError  # noqa: E402
from test_provider_admin import FakeProviderAdminStore, _auth  # noqa: E402


_PROVIDER_MUTATIONS = [
    ("POST", ""),
    ("POST", "/-/test"),
    ("POST", "/openrouter/test"),
    ("PUT", "/openrouter/key"),
    ("DELETE", "/openrouter/key"),
    ("PUT", "/openrouter/models"),
    ("DELETE", "/openrouter"),
    ("PUT", "/openrouter/caps"),
    ("DELETE", "/openrouter/caps"),
    ("PUT", "/-/global-caps"),
    ("DELETE", "/-/global-caps"),
    ("POST", "/openrouter/reconciliation/1"),
]

@pytest.mark.parametrize(("method", "suffix"), _PROVIDER_MUTATIONS)
@pytest.mark.parametrize(
    ("kind", "expected_code"),
    [
        ("viewer", "admin_forbidden"),
        ("wrong_csrf", "admin_csrf_required"),
        ("legacy_no_action", "admin_action_header_required"),
    ],
)
def test_provider_mutation_guard_matrix(tmp_path, method, suffix, kind, expected_code):
    store = FakeProviderAdminStore()
    if kind == "viewer":
        ctx = AdminContext(subject="member", roles=frozenset({"viewer"}))
    else:
        ctx = AdminContext(
            subject="owner",
            roles=frozenset({"owner"}),
            auth_method="session",
            csrf_token="expected" if kind == "wrong_csrf" else "",
        )
    before = copy.deepcopy((store.registry, store.access_plans, store.provider_admin_state))
    app = FastAPI()
    ledger = tmp_path / "ledger.db"
    app.include_router(create_provider_admin_router(
        store, auth=lambda request: ctx,
        ledger_path=ledger, receipts_path=tmp_path / "receipts.db",
        spending_provider=lambda: {},
    ))
    probe = AsyncMock(side_effect=AssertionError("unauthorized provider probe"))
    with patch("gateway.provider_admin.provider_listing.probe_provider", new=probe):
        with TestClient(app) as client:
            response = client.request(
                method, "/admin/api/providers" + suffix,
                headers={"x-azizam-csrf": "wrong"},
                json={"daily_usd": 1, "actual_usd": "0"},
            )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == expected_code
    assert (store.registry, store.access_plans, store.provider_admin_state) == before
    assert store.audit == []
    assert not ledger.exists()
    probe.assert_not_awaited()
    store._test_dir.cleanup()

@pytest.mark.parametrize(
    ("token", "headers"),
    [
        ("expected", {"x-azizam-csrf": "expected"}),
        ("", {"x-azizam-admin-action": "1"}),
    ],
)
def test_provider_session_cap_write_accepts_required_header(tmp_path, token, headers):
    store = FakeProviderAdminStore()
    ctx = AdminContext(
        subject="owner", roles=frozenset({"owner"}),
        auth_method="session", csrf_token=token,
    )
    app = FastAPI()
    app.include_router(create_provider_admin_router(
        store, auth=lambda request: ctx,
        ledger_path=tmp_path / "ledger.db",
        spending_provider=lambda: {},
    ))
    with TestClient(app) as client:
        response = client.put(
            "/admin/api/providers/openrouter/caps",
            headers=headers, json={"daily_usd": 1},
        )
    assert response.status_code == 200
    assert response.json()["provider"]["caps"]["daily_usd"] == 1
    assert store.provider_admin_state["providers"]["openrouter"]["caps"] == {"daily_usd": 1.0}
    store._test_dir.cleanup()


def test_provider_remove_fails_closed_on_corrupt_ledger(tmp_path):
    store = FakeProviderAdminStore()
    ledger = tmp_path / "corrupt-ledger.db"
    original = b"This is not a SQLite database."
    ledger.write_bytes(original)
    store.provider_admin_state["providers"]["openrouter"] = {
        "caps": {"daily_usd": 2},
    }
    before = copy.deepcopy((store.registry, store.access_plans, store.provider_admin_state))
    app = FastAPI()
    app.include_router(create_provider_admin_router(
        store, auth=_auth, ledger_path=ledger,
        receipts_path=tmp_path / "receipts.db",
        spending_provider=lambda: {},
    ))
    with TestClient(app) as client:
        response = client.delete(
            "/admin/api/providers/openrouter",
            headers={"authorization": "Bearer admin"},
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_ledger_unavailable"
    assert (store.registry, store.access_plans, store.provider_admin_state) == before
    assert store.audit == []
    assert ledger.read_bytes() == original
    store._test_dir.cleanup()


@pytest.mark.parametrize(("method", "suffix"), [
    ("PUT", "/models"),
    ("DELETE", ""),
])
def test_provider_stale_generation_is_409_without_mutation(tmp_path, method, suffix):
    store = FakeProviderAdminStore()
    store.provider_admin_state["providers"]["openrouter"] = {
        "caps": {"daily_usd": 3},
    }
    before = copy.deepcopy((store.registry, store.access_plans, store.provider_admin_state))
    app = FastAPI()
    app.include_router(create_provider_admin_router(
        store, auth=_auth,
        ledger_path=tmp_path / "absent-ledger.db",
        spending_provider=lambda: {},
    ))
    body = {
        "expected_generation": 6,
        "models": [{
            "id": "new-model", "enabled": True,
            "price": {
                "input_usd_per_million_tokens": "1",
                "output_usd_per_million_tokens": "2",
            },
        }],
    }
    with patch(
        "gateway.provider_admin.admin_store.get_service_registry_with_generation",
        return_value={"operator_settings_generation": 7},
    ):
        with patch("gateway.provider_admin.secret_store.delete_provider_secret") as delete_secret:
            with TestClient(app) as client:
                response = client.request(
                    method, "/admin/api/providers/openrouter" + suffix,
                    headers={"authorization": "Bearer admin"}, json=body,
                )
            delete_secret.assert_not_called()
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "service_registry_conflict"
    assert (store.registry, store.access_plans, store.provider_admin_state) == before
    assert store.audit == []
    store._test_dir.cleanup()


def test_stale_failed_probe_preserves_newer_connection_state(tmp_path, monkeypatch):
    store = FakeProviderAdminStore()
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-probe-only-key")
    new_connection = {"reachable": True, "checked_at": 200}
    store.provider_admin_state["providers"]["openrouter"] = {
        "connection": {"reachable": False, "checked_at": 100},
    }

    async def obsolete_probe(config, api_key):
        assert config["base_url"] == "https://openrouter.ai/api/v1"
        assert api_key == "synthetic-probe-only-key"
        store.registry["services"]["openrouter"]["config"]["base_url"] = (
            "https://replacement.invalid/v1"
        )
        store.registry["services"]["openrouter"]["config"]["credential_origin"] = (
            "https://replacement.invalid:443"
        )
        store.provider_admin_state["providers"]["openrouter"]["connection"] = dict(new_connection)
        raise ProviderListingError("old endpoint failed")

    app = FastAPI()
    app.include_router(create_provider_admin_router(
        store, auth=_auth, ledger_path=tmp_path / "ledger.db",
        spending_provider=lambda: {},
    ))
    probe = AsyncMock(side_effect=obsolete_probe)
    with patch("gateway.provider_admin.provider_listing.probe_provider", new=probe):
        with TestClient(app) as client:
            failed = client.post(
                "/admin/api/providers/openrouter/test",
                headers={"authorization": "Bearer admin"},
            )
            latest = client.get(
                "/admin/api/providers/openrouter",
                headers={"authorization": "Bearer admin"},
            )
    assert failed.status_code == 400
    assert failed.json()["error"]["code"] == "provider_connection_failed"
    assert latest.status_code == 200
    assert latest.json()["provider"]["reachable"] is True
    assert latest.json()["provider"]["checked_at"] == 200
    assert store.provider_admin_state["providers"]["openrouter"]["connection"] == new_connection
    assert store.audit == []
    probe.assert_awaited_once()
    store._test_dir.cleanup()
