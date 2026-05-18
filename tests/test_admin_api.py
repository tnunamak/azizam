"""Tests for the admin-plane router."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Mapping

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.admin_api import (  # noqa: E402
    AdminAuthError,
    AdminContext,
    create_admin_router,
)
from gateway.policy import AppProfile  # noqa: E402


class FakeAdminStore:
    """In-memory admin store used to exercise the router."""

    def __init__(self) -> None:
        self.users: list[dict[str, Any]] = [
            {
                "id": "u_owner",
                "email": "owner@example.com",
                "name": "Owner",
                "roles": ["admin"],
            }
        ]
        self.apps: dict[str, dict[str, Any]] = {}
        self.profiles: list[dict[str, Any]] = [
            {"id": "default-local", "label": "Default local"}
        ]
        self.credentials: dict[str, dict[str, Any]] = {}
        self.audit: list[dict[str, Any]] = []
        self._next_id = 1

    def _id(self, prefix: str) -> str:
        ident = f"{prefix}_{self._next_id}"
        self._next_id += 1
        return ident

    def list_users(self) -> list[dict[str, Any]]:
        return list(self.users)

    def list_apps(self) -> list[dict[str, Any]]:
        return list(self.apps.values())

    def get_app(self, app_id: str) -> dict[str, Any] | None:
        return self.apps.get(app_id)

    def create_app(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        app_id = self._id("app")
        record = {
            "id": app_id,
            "name": data["name"],
            "owner": data.get("owner") or actor.subject,
            "profile_id": data.get("profile_id") or "default-local",
            "created_at": "2026-05-17T00:00:00Z",
        }
        self.apps[app_id] = record
        self.audit.append(
            {
                "at": "2026-05-17T00:00:00Z",
                "actor": actor.subject,
                "action": "app.create",
                "target": app_id,
            }
        )
        return dict(record)

    def list_profiles(self) -> list[dict[str, Any]]:
        return list(self.profiles)

    def create_profile(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        record = {
            "id": data.get("id") or self._id("prof"),
            "label": data.get("label") or data.get("id") or "unnamed",
        }
        self.profiles.append(record)
        return dict(record)

    def list_credentials(self) -> list[dict[str, Any]]:
        # Records intentionally never carry raw key material.
        return [
            {k: v for k, v in c.items() if k != "api_key"}
            for c in self.credentials.values()
        ]

    def create_credential(
        self,
        *,
        app_id: str,
        data: Mapping[str, Any],
        actor: AdminContext,
    ) -> dict[str, Any]:
        cred_id = self._id("cred")
        raw_key = f"sk-test-{cred_id}-secret"
        stored = {
            "id": cred_id,
            "app_id": app_id,
            "label": data.get("label") or "api key",
            "created_at": "2026-05-17T00:00:00Z",
            "revoked_at": None,
        }
        self.credentials[cred_id] = stored
        self.audit.append(
            {
                "at": "2026-05-17T00:00:00Z",
                "actor": actor.subject,
                "action": "credential.create",
                "target": cred_id,
            }
        )
        # Caller of create_credential gets the one-time raw key.
        return {**stored, "api_key": raw_key}

    def revoke_credential(
        self, *, credential_id: str, actor: AdminContext
    ) -> dict[str, Any] | None:
        record = self.credentials.get(credential_id)
        if record is None:
            return None
        record["revoked_at"] = "2026-05-17T01:00:00Z"
        self.audit.append(
            {
                "at": "2026-05-17T01:00:00Z",
                "actor": actor.subject,
                "action": "credential.revoke",
                "target": credential_id,
            }
        )
        return dict(record)

    def recent_audit(self, *, limit: int) -> list[dict[str, Any]]:
        return list(self.audit[-limit:])


def _admin_auth(request: Request) -> AdminContext:
    header = request.headers.get("authorization", "")
    if header == "Bearer admin-token":
        return AdminContext(
            subject="u_owner",
            email="owner@example.com",
            roles=frozenset({"admin"}),
        )
    if header == "Bearer viewer-token":
        return AdminContext(
            subject="u_viewer",
            email="viewer@example.com",
            roles=frozenset({"viewer"}),
        )
    raise AdminAuthError()


def _make_client(
    store: FakeAdminStore | None = None,
    *,
    profile_resolver=None,
) -> tuple[TestClient, FakeAdminStore]:
    store = store or FakeAdminStore()
    app = FastAPI()
    app.include_router(
        create_admin_router(
            store,
            auth=_admin_auth,
            profile_resolver=profile_resolver,
        )
    )
    return TestClient(app), store


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer admin-token"}


class AdminAuthTests(unittest.TestCase):
    def test_missing_token_returns_401_json(self) -> None:
        client, _ = _make_client()
        resp = client.get("/admin/api/overview")
        self.assertEqual(resp.status_code, 401)
        body = resp.json()
        self.assertEqual(body["error"]["code"], "admin_unauthenticated")

    def test_non_admin_token_returns_403_json(self) -> None:
        client, _ = _make_client()
        resp = client.get(
            "/admin/api/overview",
            headers={"Authorization": "Bearer viewer-token"},
        )
        self.assertEqual(resp.status_code, 403)
        body = resp.json()
        self.assertEqual(body["error"]["code"], "admin_forbidden")

    def test_authenticated_admin_can_read_overview(self) -> None:
        client, _ = _make_client()
        resp = client.get("/admin/api/overview", headers=_auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["actor"]["subject"], "u_owner")
        self.assertIn("apps", body["counts"])

    def test_owner_role_is_admin_capable(self) -> None:
        def owner_auth(_request: Request) -> AdminContext:
            return AdminContext(
                subject="u_owner",
                email="owner@example.com",
                roles=frozenset({"owner"}),
            )

        app = FastAPI()
        app.include_router(create_admin_router(FakeAdminStore(), auth=owner_auth))
        resp = TestClient(app).get("/admin/api/overview")
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_cookie_session_mutations_require_admin_action_header(self) -> None:
        def session_auth(_request: Request) -> AdminContext:
            return AdminContext(
                subject="u_owner",
                email="owner@example.com",
                roles=frozenset({"owner"}),
                auth_method="session",
            )

        app = FastAPI()
        app.include_router(create_admin_router(FakeAdminStore(), auth=session_auth))
        client = TestClient(app)

        blocked = client.post("/admin/api/apps", json={"name": "brother-laptop"})
        self.assertEqual(blocked.status_code, 403, blocked.text)
        self.assertEqual(
            blocked.json()["error"]["code"],
            "admin_action_header_required",
        )

        allowed = client.post(
            "/admin/api/apps",
            json={"name": "brother-laptop"},
            headers={"x-ai-gateway-admin-action": "1"},
        )
        self.assertEqual(allowed.status_code, 201, allowed.text)


class AdminAppKeyFlowTests(unittest.TestCase):
    def test_create_app_then_mint_key_returns_one_time_key(self) -> None:
        client, store = _make_client()

        app_resp = client.post(
            "/admin/api/apps",
            json={"name": "brother-laptop"},
            headers=_auth(),
        )
        self.assertEqual(app_resp.status_code, 201, app_resp.text)
        app_id = app_resp.json()["app"]["id"]

        key_resp = client.post(
            f"/admin/api/apps/{app_id}/keys",
            json={"label": "first key"},
            headers=_auth(),
        )
        self.assertEqual(key_resp.status_code, 201, key_resp.text)
        cred = key_resp.json()["credential"]
        self.assertIn("api_key", cred)
        self.assertTrue(cred["api_key"].startswith("sk-test-"))
        self.assertEqual(cred["app_id"], app_id)

        # List endpoints must never return raw key material.
        list_resp = client.get("/admin/api/users", headers=_auth())
        self.assertNotIn("sk-test-", list_resp.text)
        # The fake store's list_credentials strips api_key; verify the admin
        # overview/audit endpoints also don't leak it.
        audit_resp = client.get("/admin/api/audit", headers=_auth())
        self.assertEqual(audit_resp.status_code, 200)
        self.assertNotIn("sk-test-", audit_resp.text)

    def test_create_key_for_unknown_app_returns_404(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/apps/app_missing/keys",
            json={},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "app_not_found")

    def test_create_app_requires_name(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/apps", json={}, headers=_auth()
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "invalid_app")


class AdminCredentialRevocationTests(unittest.TestCase):
    def test_revoke_marks_credential_revoked(self) -> None:
        client, store = _make_client()
        client.post(
            "/admin/api/apps",
            json={"name": "brother-laptop"},
            headers=_auth(),
        )
        app_id = next(iter(store.apps))
        key_resp = client.post(
            f"/admin/api/apps/{app_id}/keys",
            json={},
            headers=_auth(),
        )
        cred_id = key_resp.json()["credential"]["id"]

        revoke_resp = client.post(
            f"/admin/api/credentials/{cred_id}/revoke",
            headers=_auth(),
        )
        self.assertEqual(revoke_resp.status_code, 200, revoke_resp.text)
        body = revoke_resp.json()
        self.assertIsNotNone(body["credential"]["revoked_at"])
        self.assertNotIn("api_key", body["credential"])

    def test_revoke_unknown_credential_returns_404(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/credentials/cred_missing/revoke",
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "credential_not_found")


class AdminProfileTests(unittest.TestCase):
    def test_create_profile_with_valid_block(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/profiles",
            json={
                "id": "brother",
                "label": "Brother's laptop",
                "profile": {
                    "id": "brother",
                    "protocols": ["openai"],
                    "status_visibility": {"level": "basic"},
                },
            },
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 201, resp.text)

    def test_create_profile_rejects_invalid_visibility(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/profiles",
            json={
                "profile": {
                    "id": "bad",
                    "status_visibility": {"level": "not-a-level"},
                }
            },
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "invalid_profile")

    def test_list_profiles_returns_stored_profiles(self) -> None:
        client, store = _make_client()
        resp = client.get("/admin/api/profiles", headers=_auth())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            [p["id"] for p in resp.json()["profiles"]],
            [p["id"] for p in store.profiles],
        )


class AdminPolicySimulationTests(unittest.TestCase):
    def test_inline_profile_allows_matching_request(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/policy/simulate",
            json={
                "profile": {
                    "id": "brother",
                    "protocols": ["openai"],
                },
                "request": {"protocol": "openai"},
            },
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["profile_id"], "brother")
        self.assertTrue(body["decision"]["allowed"])
        self.assertEqual(body["decision"]["decision"], "allowed")

    def test_inline_profile_denies_disallowed_protocol(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/policy/simulate",
            json={
                "profile": {
                    "id": "brother",
                    "protocols": ["openai"],
                },
                "request": {"protocol": "anthropic"},
            },
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["decision"]["allowed"])
        self.assertEqual(body["decision"]["decision"], "denied")

    def test_simulate_accepts_capability_selector_metadata(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/policy/simulate",
            json={
                "profile": {
                    "id": "family-chat",
                    "allow": {
                        "intents": ["chat.completions"],
                        "exposure": ["default"],
                        "trust_tiers": ["stable"],
                        "resource_tiers": ["interactive"],
                        "tags": ["coder"],
                    },
                },
                "request": {
                    "intent": "chat.completions",
                    "capability_exposure": "default",
                    "capability_trust_tier": "stable",
                    "capability_resource_tier": "interactive",
                    "capability_tags": ["coder", "local"],
                },
            },
            headers=_auth(),
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["decision"]["allowed"])

    def test_resolver_can_supply_profile_by_id(self) -> None:
        registry = {
            "brother": AppProfile(id="brother", protocols=frozenset({"openai"})),
        }
        client, _ = _make_client(profile_resolver=registry.get)
        resp = client.post(
            "/admin/api/policy/simulate",
            json={"profile": "brother", "request": {"protocol": "openai"}},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["profile_id"], "brother")
        self.assertTrue(body["decision"]["allowed"])

    def test_resolver_404_for_unknown_id(self) -> None:
        client, _ = _make_client(profile_resolver=lambda _id: None)
        resp = client.post(
            "/admin/api/policy/simulate",
            json={"profile": "missing", "request": {}},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "profile_not_found")

    def test_simulate_requires_admin(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/policy/simulate",
            json={"profile": {}, "request": {}},
        )
        self.assertEqual(resp.status_code, 401)


class AdminAuditTests(unittest.TestCase):
    def test_audit_returns_recent_entries(self) -> None:
        client, store = _make_client()
        client.post(
            "/admin/api/apps",
            json={"name": "brother-laptop"},
            headers=_auth(),
        )
        resp = client.get("/admin/api/audit", headers=_auth())
        self.assertEqual(resp.status_code, 200)
        entries = resp.json()["audit"]
        self.assertEqual(entries[-1]["action"], "app.create")

    def test_audit_limit_is_capped(self) -> None:
        client, store = _make_client()
        # Populate the audit log directly.
        for i in range(10):
            store.audit.append({"action": f"event_{i}"})
        resp = client.get("/admin/api/audit?limit=3", headers=_auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["limit"], 3)
        self.assertEqual(len(body["audit"]), 3)


if __name__ == "__main__":
    unittest.main()
