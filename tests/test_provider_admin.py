from __future__ import annotations

import os
import sys
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.admin_api import AdminAuthError, AdminContext  # noqa: E402
from gateway.provider_admin import (  # noqa: E402
    create_provider_admin_router,
    provider_ledger_spending_snapshot,
)


def _auth(request: Request) -> AdminContext:
    token = request.headers.get("authorization", "")
    if token != "Bearer admin":
        raise AdminAuthError()
    return AdminContext(subject="usr_admin", roles=frozenset({"admin"}))


def _member_auth(request: Request) -> AdminContext:
    return AdminContext(subject="usr_member", roles=frozenset({"viewer"}))


class FakeProviderAdminStore:
    def __init__(self) -> None:
        self.registry = {
            "schema_version": 1,
            "services": {
                "openrouter": {
                    "id": "openrouter",
                    "name": "OpenRouter",
                    "enabled": True,
                    "config": {
                        "remote_provider": True,
                        "provider_type": "chat",
                        "api_key_env": "OPENROUTER_API_KEY",
                        "models": [
                            {
                                "model_group": "openrouter/anthropic/claude-sonnet",
                                "upstream_model": "anthropic/claude-sonnet",
                            }
                        ],
                    },
                    "secret_refs": {"api_key_env": "OPENROUTER_API_KEY"},
                },
                "openrouter-service": {
                    "id": "openrouter-service",
                    "name": "OpenRouter Service",
                    "enabled": True,
                    "config": {
                        "remote_provider": True,
                        "provider_type": "chat",
                        "provider_id": "openrouter-canonical",
                        "api_key_env": "OPENROUTER_CANONICAL_API_KEY",
                    },
                    "secret_refs": {"api_key_env": "OPENROUTER_CANONICAL_API_KEY"},
                },
                "local-tabby": {
                    "id": "local-tabby",
                    "name": "Local Tabby",
                    "enabled": True,
                    "config": {"api_key_env": "TABBY_API_KEY"},
                },
            },
        }
        self.profiles = [
            {"id": "default-local", "name": "Default local"},
            {"id": "builders", "name": "Builders"},
            {"id": "prof_row", "name": "Row backed", "profile": {"id": "policy-plan"}},
        ]
        self.provider_admin_state: dict[str, Any] = {
            "schema_version": 1,
            "providers": {},
            "global_caps": {},
        }
        self.audit: list[dict[str, Any]] = []

    def get_service_registry(self) -> dict[str, Any]:
        return self.registry

    def list_profiles(self) -> list[dict[str, Any]]:
        return list(self.profiles)

    def get_provider_admin_state(self) -> dict[str, Any]:
        return self.provider_admin_state

    def update_provider_admin_state(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        self.provider_admin_state = dict(data)
        self.audit.append({
            "action": "operator.provider_admin.update",
            "actor": actor.subject,
        })
        return self.provider_admin_state


def _client(
    store: FakeProviderAdminStore,
    *,
    auth=_auth,
    spending=None,
) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_provider_admin_router(store, auth=auth, spending_provider=spending)
    )
    return TestClient(app)


class ProviderAdminApiTests(unittest.TestCase):
    def test_list_projects_remote_providers_and_key_status_without_secret_value(self) -> None:
        store = FakeProviderAdminStore()
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-live-secret"}, clear=False):
            response = _client(store).get(
                "/admin/api/providers",
                headers={"authorization": "Bearer admin"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            [p["id"] for p in body["providers"]],
            ["openrouter", "openrouter-canonical"],
        )
        provider = next(p for p in body["providers"] if p["id"] == "openrouter")
        self.assertEqual(provider["key_status"], {
            "state": "set",
            "api_key_env": "OPENROUTER_API_KEY",
        })
        self.assertEqual(
            provider["spend"],
            {
                "daily_usd": None,
                "monthly_usd": None,
                "status": "unknown",
                "error": "ledger_missing",
            },
        )
        self.assertIn({"id": "builders", "record_id": "builders", "name": "Builders"}, body["available_plans"])
        self.assertNotIn("sk-live-secret", response.text)
        self.assertNotIn("TABBY_API_KEY", response.text)

    def test_missing_env_reports_missing_key_state(self) -> None:
        store = FakeProviderAdminStore()
        with patch.dict(os.environ, {}, clear=True):
            response = _client(store).get(
                "/admin/api/providers/openrouter",
                headers={"authorization": "Bearer admin"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"]["key_status"]["state"], "missing")

    def test_caps_and_allowed_plans_are_crud_state(self) -> None:
        store = FakeProviderAdminStore()
        client = _client(
            store,
            spending=lambda: {
                "providers": {"openrouter": {"daily_usd": 1.25, "monthly_usd": 9.5}},
                "global": {"daily_usd": 1.25, "monthly_usd": 9.5},
            },
        )

        caps = client.put(
            "/admin/api/providers/openrouter/caps",
            headers={"authorization": "Bearer admin"},
            json={"daily_usd": 2, "monthly_usd": 20},
        )
        self.assertEqual(caps.status_code, 200)
        self.assertEqual(caps.json()["provider"]["caps"], {"daily_usd": 2.0, "monthly_usd": 20.0})
        self.assertEqual(caps.json()["provider"]["spend"]["daily_usd"], 1.25)

        plans = client.put(
            "/admin/api/providers/openrouter/allowed-plans",
            headers={"authorization": "Bearer admin"},
            json={"allowed_plans": ["builders"]},
        )
        self.assertEqual(plans.status_code, 200)
        self.assertEqual(plans.json()["provider"]["allowed_plans"], ["builders"])

        cleared = client.delete(
            "/admin/api/providers/openrouter/caps",
            headers={"authorization": "Bearer admin"},
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.json()["provider"]["caps"], {})
        self.assertEqual(
            [event["action"] for event in store.audit],
            [
                "operator.provider_admin.update",
                "operator.provider_admin.update",
                "operator.provider_admin.update",
            ],
        )

    def test_provider_id_uses_execution_provider_id_and_plan_stores_policy_id(self) -> None:
        store = FakeProviderAdminStore()
        client = _client(store)

        missing_service_id = client.get(
            "/admin/api/providers/openrouter-service",
            headers={"authorization": "Bearer admin"},
        )
        self.assertEqual(missing_service_id.status_code, 404)

        plans = client.put(
            "/admin/api/providers/openrouter-canonical/allowed-plans",
            headers={"authorization": "Bearer admin"},
            json={"allowed_plans": ["prof_row"]},
        )
        self.assertEqual(plans.status_code, 200)
        self.assertEqual(plans.json()["provider"]["allowed_plans"], ["policy-plan"])

    def test_global_caps_round_trip(self) -> None:
        store = FakeProviderAdminStore()
        client = _client(store)
        response = client.put(
            "/admin/api/providers/-/global-caps",
            headers={"authorization": "Bearer admin"},
            json={"daily_usd": 5, "monthly_usd": 50},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["global"]["caps"], {"daily_usd": 5.0, "monthly_usd": 50.0})

        cleared = client.delete(
            "/admin/api/providers/-/global-caps",
            headers={"authorization": "Bearer admin"},
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.json()["global"]["caps"], {})

    def test_rejects_non_admin_and_unknown_plan(self) -> None:
        store = FakeProviderAdminStore()
        forbidden = _client(store, auth=_member_auth).put(
            "/admin/api/providers/openrouter/caps",
            json={"daily_usd": 2},
        )
        self.assertEqual(forbidden.status_code, 403)

        unknown = _client(store).put(
            "/admin/api/providers/openrouter/allowed-plans",
            headers={"authorization": "Bearer admin"},
            json={"allowed_plans": ["missing-plan"]},
        )
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(unknown.json()["error"]["code"], "unknown_access_plan")

    def test_rejects_unknown_provider_and_bad_cap(self) -> None:
        store = FakeProviderAdminStore()
        client = _client(store)
        missing = client.put(
            "/admin/api/providers/not-there/caps",
            headers={"authorization": "Bearer admin"},
            json={"daily_usd": 2},
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"]["code"], "provider_not_found")

        bad_cap = client.put(
            "/admin/api/providers/openrouter/caps",
            headers={"authorization": "Bearer admin"},
            json={"daily_usd": -1},
        )
        self.assertEqual(bad_cap.status_code, 400)
        self.assertEqual(bad_cap.json()["error"]["code"], "invalid_spend_cap")

    def test_provider_ledger_spending_snapshot_projects_daily_and_monthly_totals(self) -> None:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        path = Path(tmpdir.name) / "provider-ledger.db"
        with closing(sqlite3.connect(path)) as conn:
            conn.execute(
                "CREATE TABLE provider_spend (provider TEXT, profile_id TEXT, status TEXT, estimated_usd TEXT, actual_usd TEXT, created_at INTEGER)"
            )
            conn.execute(
                "INSERT INTO provider_spend VALUES (?, ?, ?, ?, ?, ?)",
                ("openrouter", "builders", "settled", "1.00", "1.25", 1_699_999_900),
            )
            conn.execute(
                "INSERT INTO provider_spend VALUES (?, ?, ?, ?, ?, ?)",
                ("typesafe", "builders", "reserved", "0.50", None, 1_699_900_000),
            )
            conn.execute(
                "INSERT INTO provider_spend VALUES (?, ?, ?, ?, ?, ?)",
                ("openrouter", "builders", "settled", "2.00", "2.00", 1_699_913_500),
            )
            conn.execute(
                "INSERT INTO provider_spend VALUES (?, ?, ?, ?, ?, ?)",
                ("openrouter", "builders", "released", "99.00", None, 1_699_999_900),
            )
            conn.commit()

        snapshot = provider_ledger_spending_snapshot(path=path, now=1_700_000_000)

        self.assertEqual(snapshot["providers"]["openrouter"], {"daily_usd": 1.25, "monthly_usd": 3.25})
        self.assertEqual(snapshot["providers"]["typesafe"], {"daily_usd": 0.0, "monthly_usd": 0.5})
        self.assertEqual(snapshot["global"], {"daily_usd": 1.25, "monthly_usd": 3.75})

    def test_missing_ledger_reports_unknown_spend(self) -> None:
        store = FakeProviderAdminStore()
        response = _client(store, spending=lambda: {"available": False, "error": "ledger_missing"}).get(
            "/admin/api/providers/openrouter",
            headers={"authorization": "Bearer admin"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["provider"]["spend"],
            {
                "daily_usd": None,
                "monthly_usd": None,
                "status": "unknown",
                "error": "ledger_missing",
            },
        )


if __name__ == "__main__":
    unittest.main()
