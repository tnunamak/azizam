from __future__ import annotations

import os
import sys
import sqlite3
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import admin_store, service_registry, secret_store  # noqa: E402
from tests.support.access import identity as identity_of, plan_for  # noqa: E402
from gateway.admin_api import AdminAuthError, AdminContext  # noqa: E402
from gateway.provider_admin import (  # noqa: E402
    PROVIDER_ADMIN_SETTING_KEY,
    create_provider_admin_router,
    normalize_provider_admin_state,
    provider_admin_setting_from_store,
    provider_admin_setting_to_store,
    provider_ledger_spending_snapshot,
)
from gateway.providers import Cost, ProviderError, ProviderRef, reserve_provider_spend  # noqa: E402


def _auth(request: Request) -> AdminContext:
    token = request.headers.get("authorization", "")
    if token != "Bearer admin":
        raise AdminAuthError()
    return AdminContext(subject="usr_admin", roles=frozenset({"admin"}))


def _member_auth(request: Request) -> AdminContext:
    return AdminContext(subject="usr_member", roles=frozenset({"viewer"}))


class SqliteProviderAdminStore:
    def __init__(self, *, path: Path, registry: dict[str, Any], access_plans: list[dict[str, Any]]) -> None:
        self.path = path
        self.registry = registry
        self.access_plans = access_plans

    def get_service_registry(self) -> dict[str, Any]:
        return self.registry

    def list_access_plans(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.access_plans]

    def update_access_plan(
        self, *, plan_id: str, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any] | None:
        for index, row in enumerate(self.access_plans):
            if row["id"] == plan_id:
                self.access_plans[index] = {**row, "plan": dict(data)}
                return self.access_plans[index]
        return None

    def get_provider_admin_state(self) -> dict[str, Any]:
        return provider_admin_setting_from_store(path=self.path)

    def update_provider_admin_state(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        return provider_admin_setting_to_store(data, actor=actor, path=self.path)


class FakeProviderAdminStore:
    def __init__(self) -> None:
        self._test_dir = tempfile.TemporaryDirectory()
        self.path = Path(self._test_dir.name) / "admin.db"
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
                        "base_url": "https://openrouter.ai/api/v1",
                        "credential_origin": "https://openrouter.ai:443",
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
                        "base_url": "https://openrouter-canonical.test/v1",
                        "credential_origin": "https://openrouter-canonical.test:443",
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
                        "base_url": "https://api.typesafe.ai/v1",
                        "credential_origin": "https://api.typesafe.ai:443",
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
        self.access_plans = [
            {"id": "plan_local", "name": "Local only", "plan": {"paid_providers": []}},
            {"id": "plan_paid", "name": "Local and paid",
             "plan": {"paid_providers": ["openrouter", "openrouter-canonical", "typesafe"]}},
        ]
        self.provider_admin_state: dict[str, Any] = {
            "schema_version": 1,
            "providers": {},
            "global_caps": {},
        }
        self.audit: list[dict[str, Any]] = []

    def get_service_registry(self) -> dict[str, Any]:
        return self.registry

    def list_access_plans(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.access_plans]

    def update_access_plan(
        self, *, plan_id: str, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any] | None:
        for index, row in enumerate(self.access_plans):
            if row["id"] == plan_id:
                self.access_plans[index] = {**row, "plan": dict(data)}
                self.audit.append({"action": "access_plan.update", "actor": actor.subject})
                return self.access_plans[index]
        return None

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

    def add_service(
        self, *, data: Mapping[str, Any], actor: AdminContext, expected_generation: int
    ) -> dict[str, Any]:
        form = dict(data)
        service_id = str(form["id"])
        raw = service_registry.service_form_to_raw(form)
        self.registry.setdefault("services", {})[service_id] = {
            "id": service_id,
            "name": form.get("name") or service_id,
            "kind": form.get("kind") or "provider",
            "enabled": raw.get("enabled", True),
            "capabilities": raw.get("capabilities") or ["openai"],
            "config": raw,
            "secret_refs": raw.get("secret_refs") or {},
        }
        self.audit.append({"action": "operator.service.add", "actor": actor.subject})
        return {"value": self.registry}

    def update_service(
        self, *, service_id: str, data: Mapping[str, Any], actor: AdminContext, expected_generation: int
    ) -> dict[str, Any]:
        self.registry["services"][service_id] = service_registry.service_from_backend_config(
            service_id, service_registry.service_form_to_raw(data), source="ui",
        )
        self.audit.append({"action": "operator.service.update", "actor": actor.subject})
        return {"value": self.registry}

    def remove_service(
        self, *, service_id: str, actor: AdminContext, expected_generation: int
    ) -> dict[str, Any]:
        self.registry["services"].pop(service_id, None)
        self.audit.append({"action": "operator.service.remove", "actor": actor.subject})
        return {"value": self.registry}


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
    def test_catalog_endpoint_lists_presets_without_secrets(self) -> None:
        store = FakeProviderAdminStore()
        response = _client(store).get(
            "/admin/api/providers/-/catalog",
            headers={"authorization": "Bearer admin"},
        )

        self.assertEqual(response.status_code, 200)
        ids = {row["id"] for row in response.json()["providers"]}
        self.assertIn("openrouter", ids)
        self.assertIn("generic-openai", ids)
        self.assertNotIn("sk-", response.text)

    def test_add_provider_uses_catalog_data_and_secret_store_ref(self) -> None:
        store = FakeProviderAdminStore()
        store.registry["services"].pop("openrouter")
        client = _client(store)
        response = client.post(
            "/admin/api/providers",
            headers={"authorization": "Bearer admin"},
            json={
                "preset_id": "openrouter",
                "expected_generation": 0,
                "api_key": "sk-test-openrouter",
                "models": [{"id": "anthropic/claude-sonnet", "price": {"input_usd_per_million_tokens": "3", "output_usd_per_million_tokens": "15"}}],
            },
        )

        self.assertEqual(response.status_code, 201)
        service = store.registry["services"]["openrouter"]
        self.assertEqual(service["config"]["api_key_secret_id"], "provider:openrouter:api_key")
        self.assertEqual(service["config"]["model_listing"]["pricing_path"], "pricing")
        self.assertEqual(service["config"]["model_policy_schema_version"], 1)
        self.assertEqual(
            service["config"]["price"]["models"]["openrouter/anthropic/claude-sonnet"],
            {"input_usd_per_million_tokens": "3", "output_usd_per_million_tokens": "15"},
        )
        provider = response.json()["provider"]
        self.assertEqual(provider["key_status"]["state"], "set")
        self.assertTrue(provider["key_status"]["api_key_env"].startswith("AZIZAM_PROVIDER_SECRET_"))
        self.assertNotIn("sk-", response.text)

    def test_add_provider_preserves_validated_https_url_for_litellm_api_base(self) -> None:
        from gateway.litellm_substrate import compile_litellm_config_from_registry

        store = FakeProviderAdminStore()
        store.registry["services"].pop("openrouter")
        response = _client(store).post(
            "/admin/api/providers",
            headers={"authorization": "Bearer admin"},
            json={
                "preset_id": "openrouter",
                "base_url": "https://provider.test:8443/custom/v1",
                "expected_generation": 0,
                "api_key": "sk-test-openrouter",
                "models": [{"id": "upstream/model", "price": {"input_usd_per_million_tokens": "3", "output_usd_per_million_tokens": "15"}}],
            },
        )

        self.assertEqual(response.status_code, 201)
        config = store.registry["services"]["openrouter"]["config"]
        self.assertEqual(config["base_url"], "https://provider.test:8443/custom/v1")
        self.assertEqual(config["credential_origin"], "https://provider.test:8443")
        compiled = compile_litellm_config_from_registry({"services": {"openrouter": store.registry["services"]["openrouter"]}})
        self.assertEqual(
            compiled["model_list"][0]["litellm_params"]["api_base"],
            "https://provider.test:8443/custom/v1",
        )

    def test_add_provider_accepts_https_ipv6_base_url_and_binds_origin(self) -> None:
        store = FakeProviderAdminStore()
        store.registry["services"].pop("openrouter")
        response = _client(store).post(
            "/admin/api/providers",
            headers={"authorization": "Bearer admin"},
            json={
                "preset_id": "openrouter",
                "base_url": "https://[::1]:8443/custom/v1",
                "expected_generation": 0,
                "api_key": "sk-test-openrouter",
                "models": [{"id": "upstream/model", "price": {"input_usd_per_million_tokens": "3", "output_usd_per_million_tokens": "15"}}],
            },
        )

        self.assertEqual(response.status_code, 201, response.text)
        config = store.registry["services"]["openrouter"]["config"]
        self.assertEqual(config["base_url"], "https://[::1]:8443/custom/v1")
        self.assertEqual(config["credential_origin"], "https://[::1]:8443")
        self.assertEqual(
            secret_store.get_provider_secret(
                "provider:openrouter:api_key",
                db_path=store.path,
                expected_origin="https://[::1]:8443",
            ),
            "sk-test-openrouter",
        )

    def test_saved_gateway_check_refuses_stored_key_after_origin_retarget(self) -> None:
        store = FakeProviderAdminStore()
        service = store.registry["services"]["openrouter"]
        service["config"].pop("api_key_env", None)
        service["config"].update({
            "api_key_secret_id": "provider:openrouter:api_key",
            "base_url": "https://evil.test/api/v1",
            "credential_origin": "https://evil.test:443",
            "model_listing": {"method": "GET", "path": "/models", "items_path": "data", "id_path": "id"},
        })
        secret_store.put_provider_secret(
            "provider:openrouter:api_key",
            "sk-origin-bound",
            db_path=store.path,
            approved_origin="https://openrouter.ai/api/v1",
        )

        response = _client(store).post(
            "/admin/api/providers/openrouter/test",
            headers={"authorization": "Bearer admin"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "provider_connection_failed")
        self.assertIn("stored provider key is unavailable", response.json()["error"]["message"].lower())

    def test_saved_gateway_check_refuses_bound_env_key_after_origin_retarget(self) -> None:
        store = FakeProviderAdminStore()
        service = store.registry["services"]["openrouter"]
        service["config"].update({
            "api_key_env": "OPENROUTER_API_KEY",
            "base_url": "https://evil.test/api/v1",
            "credential_origin": "https://openrouter.ai:443",
            "model_listing": {"method": "GET", "path": "/models", "items_path": "data", "id_path": "id"},
        })

        async def should_not_probe(_config, _api_key):
            raise AssertionError("provider probe should not run with a stale env credential origin")

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-openrouter"}), patch(
            "gateway.provider_admin.provider_listing.probe_provider",
            new=AsyncMock(side_effect=should_not_probe),
        ):
            response = _client(store).post(
                "/admin/api/providers/openrouter/test",
                headers={"authorization": "Bearer admin"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "provider_connection_failed")
        self.assertIn("approved for a different endpoint", response.json()["error"]["message"])

    def test_bound_env_key_status_is_missing_after_origin_retarget(self) -> None:
        store = FakeProviderAdminStore()
        service = store.registry["services"]["openrouter"]
        service["config"].update({
            "api_key_env": "OPENROUTER_API_KEY",
            "base_url": "https://evil.test/api/v1",
            "credential_origin": "https://openrouter.ai:443",
        })

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-openrouter"}):
            response = _client(store).get(
                "/admin/api/providers/openrouter",
                headers={"authorization": "Bearer admin"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"]["key_status"]["state"], "missing")


    def test_env_key_status_is_missing_without_origin_metadata(self) -> None:
        store = FakeProviderAdminStore()
        service = store.registry["services"]["openrouter"]
        service["config"].pop("credential_origin", None)

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-openrouter"}):
            response = _client(store).get(
                "/admin/api/providers/openrouter",
                headers={"authorization": "Bearer admin"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"]["key_status"]["state"], "missing")



    def test_saved_gateway_check_does_not_repopulate_stale_success_after_key_rotation(self) -> None:
        store = FakeProviderAdminStore()
        store.registry["services"]["openrouter"]["config"].pop("api_key_env", None)
        store.registry["services"]["openrouter"]["config"]["api_key_secret_id"] = "provider:openrouter:api_key"
        store.registry["services"]["openrouter"]["config"]["base_url"] = "https://openrouter.ai/api/v1"
        secret_store.put_provider_secret(
            "provider:openrouter:api_key",
            "sk-original-4f2a",
            db_path=store.path,
            now=1234,
            approved_origin="https://openrouter.ai/api/v1",
        )
        client = _client(store)
        probe_entered = threading.Event()
        release_probe = threading.Event()

        async def blocking_probe(config, api_key):
            self.assertEqual(api_key, "sk-original-4f2a")
            probe_entered.set()
            self.assertTrue(release_probe.wait(5), "test did not release blocked probe")
            return {"ok": True, "reachable": True, "checked_at": 1700000000, "models": []}

        check_result: dict[str, object] = {}

        def run_check() -> None:
            check_result["response"] = client.post(
                "/admin/api/providers/openrouter/test",
                headers={"authorization": "Bearer admin"},
            )

        with patch("gateway.provider_admin.provider_listing.probe_provider", new=AsyncMock(side_effect=blocking_probe)):
            check_thread = threading.Thread(target=run_check)
            check_thread.start()
            self.assertTrue(probe_entered.wait(5), "saved check did not reach provider probe")
            with patch("gateway.secret_store.time.time", return_value=1234):
                rotated = client.put(
                    "/admin/api/providers/openrouter/key",
                    headers={"authorization": "Bearer admin"},
                    json={"api_key": "sk-rotated-4f2a"},
                )
            self.assertEqual(rotated.status_code, 200)
            release_probe.set()
            check_thread.join(5)

        self.assertFalse(check_thread.is_alive())
        self.assertEqual(check_result["response"].status_code, 200)
        provider = client.get(
            "/admin/api/providers/openrouter",
            headers={"authorization": "Bearer admin"},
        ).json()["provider"]
        self.assertIsNone(provider["reachable"])
        self.assertIsNone(provider["checked_at"])

    def test_remove_recreate_mutation_lock_preserves_replacement_secret(self) -> None:
        store = FakeProviderAdminStore()
        store.registry["services"]["openrouter"]["config"].pop("api_key_env", None)
        store.registry["services"]["openrouter"]["config"]["api_key_secret_id"] = "provider:openrouter:api_key"
        store.registry["services"]["openrouter"]["config"]["base_url"] = "https://openrouter.ai/api/v1"
        secret_store.put_provider_secret(
            "provider:openrouter:api_key",
            "sk-original-4f2a",
            db_path=store.path,
            now=1234,
            approved_origin="https://openrouter.ai/api/v1",
        )
        client = _client(store)
        delete_entered = threading.Event()
        release_delete = threading.Event()
        original_delete = secret_store.delete_provider_secret

        def blocking_delete(secret_id: str, *, db_path: Path):
            delete_entered.set()
            self.assertTrue(release_delete.wait(5), "test did not release blocked delete")
            return original_delete(secret_id, db_path=db_path)

        delete_result: dict[str, object] = {}
        create_result: dict[str, object] = {}

        def delete_provider() -> None:
            delete_result["response"] = client.request(
                "DELETE",
                "/admin/api/providers/openrouter",
                headers={"authorization": "Bearer admin"},
                json={},
            )

        def create_provider() -> None:
            create_result["response"] = client.post(
                "/admin/api/providers",
                headers={"authorization": "Bearer admin"},
                json={
                    "preset_id": "openrouter",
                    "api_key": "sk-replacement-openrouter",
                    "models": [
                        {
                            "id": "anthropic/claude-sonnet",
                            "price": {
                                "input_usd_per_million_tokens": "3",
                                "output_usd_per_million_tokens": "15",
                            },
                        }
                    ],
                },
            )

        with patch.object(secret_store, "delete_provider_secret", blocking_delete):
            delete_thread = threading.Thread(target=delete_provider)
            create_thread = threading.Thread(target=create_provider)
            delete_thread.start()
            self.assertTrue(delete_entered.wait(5), "remove did not reach secret deletion")
            create_thread.start()
            time.sleep(0.05)
            self.assertNotIn("response", create_result)
            release_delete.set()
            delete_thread.join(5)
            create_thread.join(5)

        self.assertFalse(delete_thread.is_alive())
        self.assertFalse(create_thread.is_alive())
        self.assertEqual(delete_result["response"].status_code, 200)
        self.assertEqual(create_result["response"].status_code, 201)
        self.assertEqual(
            secret_store.get_provider_secret("provider:openrouter:api_key", db_path=store.path),
            "sk-replacement-openrouter",
        )
        self.assertIn("openrouter", store.registry["services"])

    def test_set_rotate_and_delete_provider_key_never_returns_secret_value(self) -> None:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        db_path = Path(tmpdir.name) / "admin.db"
        store = FakeProviderAdminStore()
        store.path = db_path
        store.registry["services"]["openrouter"]["config"].pop("api_key_env", None)
        store.registry["services"]["openrouter"]["config"]["api_key_secret_id"] = "provider:openrouter:api_key"
        store.registry["services"]["openrouter"]["config"]["base_url"] = "https://openrouter.ai/api/v1"
        client = _client(store)

        first = client.put(
            "/admin/api/providers/openrouter/key",
            headers={"authorization": "Bearer admin"},
            json={"api_key": "sk-test-4f2a"},
        )
        second = client.put(
            "/admin/api/providers/openrouter/key",
            headers={"authorization": "Bearer admin"},
            json={"api_key": "sk-test-beef"},
        )
        deleted = client.delete(
            "/admin/api/providers/openrouter/key",
            headers={"authorization": "Bearer admin"},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["key_status"]["suffix"], "4f2a")
        self.assertEqual(second.json()["key_status"]["suffix"], "beef")
        self.assertEqual(deleted.json()["key_status"]["state"], "missing")
        self.assertNotIn("sk-test", first.text + second.text + deleted.text)

    def test_provider_test_requires_configured_key_and_reports_model_listing_only(self) -> None:
        store = FakeProviderAdminStore()
        client = _client(store)
        with patch.dict(os.environ, {}, clear=True):
            missing = client.post(
                "/admin/api/providers/openrouter/test",
                headers={"authorization": "Bearer admin"},
            )
        self.assertEqual(missing.status_code, 400)

        result = {"ok": True, "reachable": True, "checked_at": 1700000000, "models": []}
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-live-secret"}, clear=False), patch("gateway.provider_admin.provider_listing.probe_provider", new=AsyncMock(return_value=result)) as probe:
            ok = client.post(
                "/admin/api/providers/openrouter/test",
                headers={"authorization": "Bearer admin"},
            )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json(), result)
        probe.assert_awaited_once()
        self.assertNotIn("sk-live-secret", ok.text)

    def test_unsaved_test_models_update_and_remove_contract(self) -> None:
        store = FakeProviderAdminStore()
        client = _client(store)

        with patch("gateway.provider_admin.provider_listing.probe_provider", new=AsyncMock(return_value={"ok": True, "models": []})):
            unsaved = client.post(
                "/admin/api/providers/-/test",
                headers={"authorization": "Bearer admin"},
                json={"preset_id": "openrouter", "api_key": "sk-test-12345"},
            )
        models = client.put(
            "/admin/api/providers/openrouter/models",
            headers={"authorization": "Bearer admin"},
            json={"models": [{"id": "anthropic/claude-haiku", "enabled": True, "price": {"input_usd_per_million_tokens": "1", "output_usd_per_million_tokens": "5"}}]},
        )
        model_rows = store.registry["services"]["openrouter"]["config"]["models"]
        removed = client.request(
            "DELETE",
            "/admin/api/providers/openrouter",
            headers={"authorization": "Bearer admin"},
            json={},
        )

        self.assertEqual(unsaved.status_code, 200)
        self.assertEqual(models.status_code, 200)
        self.assertEqual(model_rows[0]["model_group"], "openrouter/anthropic/claude-haiku")
        self.assertTrue(model_rows[0]["enabled"])
        self.assertEqual(removed.status_code, 200)
        self.assertNotIn("openrouter", store.registry["services"])

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
            "source": "env",
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
        profile = plan_for("builders", paid=sorted({"openrouter"}))
        provider = ProviderRef(
            id="openrouter",
            base_url="https://openrouter.test",
            protocols=("openai-compatible",),
            api_key_env="OPENROUTER_API_KEY",
            cost_class="paid-remote",
            price={},
            config={"concurrency_limit": 3, "credential_origin": "https://openrouter.test:443"},
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

    def test_caps_are_crud_state_and_plans_that_can_use_the_provider_are_read_only(self) -> None:
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
        self.assertEqual(caps.json()["provider"]["access_plans"], [{"id": "plan_paid", "name": "Local and paid"}])
        self.assertNotIn("allowed_plans", caps.json()["provider"])

        cleared = client.delete(
            "/admin/api/providers/openrouter/caps",
            headers={"authorization": "Bearer admin"},
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.json()["provider"]["caps"], {})
        self.assertEqual(
            [event["action"] for event in store.audit].count("operator.provider_admin.update"),
            2,
        )

    def test_provider_with_no_plan_lists_no_plans(self) -> None:
        store = FakeProviderAdminStore()
        store.access_plans[1]["plan"]["paid_providers"] = ["typesafe"]
        response = _client(store).get(
            "/admin/api/providers/openrouter", headers={"authorization": "Bearer admin"})
        self.assertEqual(response.json()["provider"]["access_plans"], [])

    def test_allowed_plans_endpoint_is_gone(self) -> None:
        response = _client(FakeProviderAdminStore()).put(
            "/admin/api/providers/openrouter/allowed-plans",
            headers={"authorization": "Bearer admin"},
            json={"allowed_plans": ["plan_paid"]},
        )
        self.assertIn(response.status_code, (404, 405))

    def test_stored_allowed_plans_are_dropped_when_provider_state_is_read(self) -> None:
        persisted = normalize_provider_admin_state({
            "schema_version": 1,
            "providers": {"openrouter": {"caps": {"daily_usd": 2}, "allowed_plans": ["owner-local"]}},
            "global_caps": {},
        })
        self.assertEqual(persisted["providers"]["openrouter"], {"caps": {"daily_usd": 2.0}})

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
            access_plans=fake.access_plans,
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
        self.assertEqual(stored["value"]["providers"]["typesafe"], {"caps": {"daily_usd": 2.0}})

        identity = identity_of(plan_for("builders", paid=sorted({"typesafe"})))
        provider = ProviderRef(
            id="typesafe",
            base_url="https://typesafe.test",
            protocols=("openai-compatible",),
            api_key_env="TYPESAFE_API_KEY",
            cost_class="paid-remote",
            price={},
            config={"credential_origin": "https://typesafe.test:443"},
        )
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}, clear=False):
            reserve_provider_spend(
                identity,
                provider,
                Cost(Decimal("0.01"), "price-table", "typesafe"),
                path=ledger_db,
                admin_db_path=admin_db,
                now=1_700_000_000,
            )

    def test_provider_id_uses_execution_provider_id(self) -> None:
        store = FakeProviderAdminStore()
        client = _client(store)

        missing_service_id = client.get(
            "/admin/api/providers/openrouter-service",
            headers={"authorization": "Bearer admin"},
        )
        self.assertEqual(missing_service_id.status_code, 404)

        canonical = client.get(
            "/admin/api/providers/openrouter-canonical",
            headers={"authorization": "Bearer admin"},
        )
        self.assertEqual(canonical.status_code, 200)
        self.assertEqual(canonical.json()["provider"]["access_plans"], [{"id": "plan_paid", "name": "Local and paid"}])

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

    def test_rejects_non_admin(self) -> None:
        store = FakeProviderAdminStore()
        forbidden = _client(store, auth=_member_auth).put(
            "/admin/api/providers/openrouter/caps",
            json={"daily_usd": 2},
        )
        self.assertEqual(forbidden.status_code, 403)

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

        self.assertEqual(snapshot["providers"]["openrouter"], {"daily_usd": 1.5, "monthly_usd": 3.5, "pending_reconciliation_count": 0})
        self.assertNotIn("OpenRouter", snapshot["providers"])
        self.assertEqual(snapshot["providers"]["typesafe"], {"daily_usd": 0.0, "monthly_usd": 0.5, "pending_reconciliation_count": 0})
        self.assertEqual(snapshot["global"], {"daily_usd": 1.5, "monthly_usd": 4.0, "pending_reconciliation_count": 0})

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
