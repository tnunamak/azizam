from __future__ import annotations

import os
import sys
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import admin_store  # noqa: E402
from gateway.admin_api import AdminAuthError, AdminContext  # noqa: E402
from gateway.provider_admin import (  # noqa: E402
    PROVIDER_ADMIN_SETTING_KEY,
    create_provider_admin_router,
    normalize_provider_admin_state,
    provider_admin_setting_from_store,
    provider_admin_setting_to_store,
    provider_ledger_spending_snapshot,
)
from gateway.policy import DEFAULT_PROFILE, RemoteProviderPolicy  # noqa: E402
from gateway.providers import Cost, ProviderError, ProviderRef, reserve_provider_spend  # noqa: E402


def _auth(request: Request) -> AdminContext:
    token = request.headers.get("authorization", "")
    if token != "Bearer admin":
        raise AdminAuthError()
    return AdminContext(subject="usr_admin", roles=frozenset({"admin"}))


def _member_auth(request: Request) -> AdminContext:
    return AdminContext(subject="usr_member", roles=frozenset({"viewer"}))


class SqliteProviderAdminStore:
    def __init__(self, *, path: Path, registry: dict[str, Any], profiles: list[dict[str, Any]]) -> None:
        self.path = path
        self.registry = registry
        self.profiles = profiles

    def get_service_registry(self) -> dict[str, Any]:
        return self.registry

    def list_profiles(self) -> list[dict[str, Any]]:
        return list(self.profiles)

    def get_provider_admin_state(self) -> dict[str, Any]:
        return provider_admin_setting_from_store(path=self.path)

    def update_provider_admin_state(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        return provider_admin_setting_to_store(data, actor=actor, path=self.path)


class FakeProviderAdminStore:
    def __init__(self) -> None:
        self.registry = {
            "schema_version": 1,
            "services": {
                "openrouter": {
                    "id": "openrouter",
                    "capabilities": ["openai"],
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
                    "capabilities": ["openai"],
                    "name": "OpenRouter Service",
                    "enabled": True,
                    "config": {
                        "remote_provider": True,
                        "provider_type": "chat",
                        "provider_id": "openrouter-canonical",
                        "api_key_env": "OPENROUTER_CANONICAL_API_KEY",
                        "models": {
                            "anthropic/claude-haiku": "anthropic/claude-haiku",
                        },
                    },
                    "secret_refs": {"api_key_env": "OPENROUTER_CANONICAL_API_KEY"},
                },
                "typesafe": {
                    "id": "typesafe",
                    "capabilities": ["openai"],
                    "name": "TypeSafe",
                    "enabled": True,
                    "config": {
                        "remote_provider": True,
                        "provider_type": "chat",
                        "api_key_env": "TYPESAFE_API_KEY",
                        "default_model": "system-one",
                    },
                    "secret_refs": {"api_key_env": "TYPESAFE_API_KEY"},
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
    def test_caps_reject_non_finite_and_boolean_money(self) -> None:
        store = FakeProviderAdminStore()
        client = _client(store)
        for amount in ("NaN", "Infinity", True):
            with self.subTest(amount=amount):
                response = client.put("/admin/api/providers/-/global-caps",
                    headers={"authorization": "Bearer admin"}, json={"daily_usd": amount})
                self.assertEqual(response.status_code, 400)

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
            ["openrouter", "openrouter-canonical", "typesafe"],
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

    def test_model_summaries_use_compiler_canonical_provider_prefixed_names(self) -> None:
        store = FakeProviderAdminStore()
        response = _client(store, spending=lambda: {"providers": {}, "global": {}}).get(
            "/admin/api/providers",
            headers={"authorization": "Bearer admin"},
        )

        self.assertEqual(response.status_code, 200)
        providers = {row["id"]: row for row in response.json()["providers"]}
        self.assertEqual(
            providers["openrouter"]["models"],
            [{"model": "openrouter/anthropic/claude-sonnet", "upstream_model": "anthropic/claude-sonnet"}],
        )
        self.assertEqual(
            providers["openrouter-canonical"]["models"],
            [{"model": "openrouter-canonical/anthropic/claude-haiku", "upstream_model": "anthropic/claude-haiku"}],
        )
        self.assertEqual(
            providers["typesafe"]["models"],
            [{"model": "typesafe/system-one", "upstream_model": "system-one"}],
        )

    def test_provider_admin_projects_runtime_concurrency_guarantee_and_data_recommendations(self) -> None:
        store = FakeProviderAdminStore()
        store.registry["services"]["openrouter"]["config"]["concurrency_limit"] = 3
        store.provider_admin_state = normalize_provider_admin_state({
            "schema_version": 1,
            "providers": {"openrouter": {"concurrency": {"max_in_flight": 4}}},
            "global_caps": {},
        })
        client = _client(store, spending=lambda: {"providers": {}, "global": {}})

        openrouter = client.get(
            "/admin/api/providers/openrouter",
            headers={"authorization": "Bearer admin"},
        ).json()["provider"]
        typesafe = client.get(
            "/admin/api/providers/typesafe",
            headers={"authorization": "Bearer admin"},
        ).json()["provider"]

        self.assertEqual(openrouter["concurrency"], {"max_in_flight": 3, "source": "config"})
        self.assertEqual(typesafe["concurrency"], {"max_in_flight": 2, "source": "default"})
        self.assertEqual(
            openrouter["cap_guarantee"]["guarantee"],
            "A cap can be exceeded by at most the actual cost of requests already in flight when it was reached.",
        )
        self.assertEqual(
            openrouter["recommendations"],
            [
                {
                    "id": "provider_key_credit_limit",
                    "message": "Set the provider key credit limit equal to gateway cap when supported",
                }
            ],
        )

        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        ledger_db = Path(tmpdir.name) / "provider_spend.db"
        profile = DEFAULT_PROFILE.with_overrides(
            id="builders",
            remote=RemoteProviderPolicy(providers=frozenset({"openrouter"})),
        )
        provider = ProviderRef(
            id="openrouter",
            base_url="https://openrouter.test",
            protocols=("openai-compatible",),
            api_key_env="OPENROUTER_API_KEY",
            cost_class="paid-remote",
            price={},
            config={"concurrency_limit": 3},
        )
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-provider-key"}):
            for _ in range(3):
                reserve_provider_spend(
                    profile,
                    provider,
                    Cost(Decimal("0.01"), "price-table", "openrouter"),
                    path=ledger_db,
                    now=1_700_000_000,
                )
            with self.assertRaises(ProviderError):
                reserve_provider_spend(
                    profile,
                    provider,
                    Cost(Decimal("0.01"), "price-table", "openrouter"),
                    path=ledger_db,
                    now=1_700_000_000,
                )

        store.registry["services"]["openrouter"]["config"]["recommendations"] = [
            {"id": "custom", "message": "Use a team-owned provider key."}
        ]
        custom = client.get(
            "/admin/api/providers/openrouter",
            headers={"authorization": "Bearer admin"},
        ).json()["provider"]
        self.assertEqual(custom["recommendations"], [{"id": "custom", "message": "Use a team-owned provider key."}])

        store.registry["services"]["openrouter"]["config"]["recommendations"] = []
        disabled = client.get(
            "/admin/api/providers/openrouter",
            headers={"authorization": "Bearer admin"},
        ).json()["provider"]
        self.assertEqual(disabled["recommendations"], [])

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

    def test_persisting_provider_caps_preserves_absent_allowed_plans_restriction(self) -> None:
        persisted = normalize_provider_admin_state({
            "schema_version": 1,
            "providers": {"openrouter": {"caps": {"daily_usd": 2}}},
            "global_caps": {},
        })

        provider_policy = persisted["providers"]["openrouter"]
        self.assertEqual(provider_policy["caps"], {"daily_usd": 2.0})
        self.assertNotIn("allowed_plans", provider_policy)

    def test_provider_api_distinguishes_unrestricted_from_explicit_empty_allowed_plans(self) -> None:
        store = FakeProviderAdminStore()
        store.provider_admin_state = normalize_provider_admin_state({
            "schema_version": 1,
            "providers": {
                "openrouter": {"caps": {"daily_usd": 2}},
                "typesafe": {"allowed_plans": []},
            },
            "global_caps": {},
        })
        client = _client(store, spending=lambda: {"providers": {}, "global": {}})

        openrouter = client.get(
            "/admin/api/providers/openrouter",
            headers={"authorization": "Bearer admin"},
        )
        typesafe = client.get(
            "/admin/api/providers/typesafe",
            headers={"authorization": "Bearer admin"},
        )

        self.assertEqual(openrouter.status_code, 200)
        self.assertEqual(typesafe.status_code, 200)
        self.assertIsNone(openrouter.json()["provider"]["allowed_plans"])
        self.assertEqual(typesafe.json()["provider"]["allowed_plans"], [])

    def test_cap_only_router_update_persists_no_plan_restriction_and_reservation_still_allowed(self) -> None:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        admin_db = Path(tmpdir.name) / "admin.db"
        ledger_db = Path(tmpdir.name) / "provider_spend.db"
        admin_store.init_db(admin_db)
        fake = FakeProviderAdminStore()
        store = SqliteProviderAdminStore(
            path=admin_db,
            registry=fake.registry,
            profiles=fake.profiles,
        )
        client = _client(store, spending=lambda: {"providers": {}, "global": {}})

        response = client.put(
            "/admin/api/providers/typesafe/caps",
            headers={"authorization": "Bearer admin"},
            json={"daily_usd": 2},
        )

        self.assertEqual(response.status_code, 200)
        stored = admin_store.get_operator_setting(PROVIDER_ADMIN_SETTING_KEY, path=admin_db)
        assert stored is not None
        provider_policy = stored["value"]["providers"]["typesafe"]
        self.assertEqual(provider_policy["caps"], {"daily_usd": 2.0})
        self.assertNotIn("allowed_plans", provider_policy)

        profile = DEFAULT_PROFILE.with_overrides(
            id="builders",
            remote=RemoteProviderPolicy(providers=frozenset({"typesafe"})),
        )
        provider = ProviderRef(
            id="typesafe",
            base_url="https://typesafe.test",
            protocols=("openai-compatible",),
            api_key_env="TYPESAFE_API_KEY",
            cost_class="paid-remote",
            price={},
            config={},
        )
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}, clear=False):
            reserve_provider_spend(
                profile,
                provider,
                Cost(Decimal("0.01"), "price-table", "typesafe"),
                path=ledger_db,
                admin_db_path=admin_db,
                now=1_700_000_000,
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
            conn.execute(
                "INSERT INTO provider_spend VALUES (?, ?, ?, ?, ?, ?)",
                ("OpenRouter", "builders", "settled", "0.25", "0.25", 1_699_999_950),
            )
            conn.commit()

        snapshot = provider_ledger_spending_snapshot(path=path, now=1_700_000_000)

        self.assertEqual(snapshot["providers"]["openrouter"], {"daily_usd": 1.5, "monthly_usd": 3.5})
        self.assertNotIn("OpenRouter", snapshot["providers"])
        self.assertEqual(snapshot["providers"]["typesafe"], {"daily_usd": 0.0, "monthly_usd": 0.5})
        self.assertEqual(snapshot["global"], {"daily_usd": 1.5, "monthly_usd": 4.0})

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
