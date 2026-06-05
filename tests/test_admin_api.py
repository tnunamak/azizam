"""Tests for the admin-plane router."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import extension_packages, service_registry  # noqa: E402
from gateway.admin_api import (  # noqa: E402
    AdminAuthError,
    AdminContext,
    create_admin_router,
    derive_admin_attention,
)
from gateway.policy import AppProfile  # noqa: E402


class DeriveAdminAttentionTests(unittest.TestCase):
    def test_healthy_snapshot_yields_no_attention(self) -> None:
        snap = {
            "apps": [{"id": "a1", "name": "Good", "default_profile_id": "p1"}],
            "profiles": [{"id": "p1"}],
            "credentials": [{"app_id": "a1", "revoked_at": None}],
        }
        self.assertEqual(derive_admin_attention(snap), [])

    def test_flags_app_and_service_problems_sorted_by_severity(self) -> None:
        snap = {
            "apps": [
                {"id": "a1", "name": "Disabled", "disabled_at": 1, "default_profile_id": "p1"},
                {"id": "a2", "name": "MissingProf", "default_profile_id": "p_gone"},
                {"id": "a3", "name": "NoKey", "default_profile_id": "p1"},
                {"id": "a4", "name": "Healthy", "default_profile_id": "p1"},
            ],
            "profiles": [{"id": "p1"}],
            "credentials": [{"app_id": "a4", "revoked_at": None}],
            "services": [
                {"key": "comfy", "name": "ComfyUI", "status": "down"},
                {"key": "tabby", "name": "TabbyAPI", "status": "degraded"},
                {"key": "ok", "name": "Fine", "status": "up"},
            ],
        }
        items = derive_admin_attention(snap)
        # Problems sort before attention; healthy app + up service produce nothing.
        severities = [i["severity"] for i in items]
        self.assertEqual(severities, sorted(severities, key=lambda s: 0 if s == "problem" else 1))
        titles = " | ".join(i["title"] for i in items)
        self.assertIn("Disabled is disabled", titles)
        self.assertIn("MissingProf's profile is missing", titles)
        self.assertIn("NoKey has no active key", titles)
        self.assertIn("ComfyUI is down", titles)
        self.assertIn("TabbyAPI is degraded", titles)
        self.assertNotIn("Healthy", titles)
        self.assertNotIn("Fine", titles)
        # Each item routes to the section that fixes it.
        sections = {i["section"] for i in items}
        self.assertEqual(sections, {"apps-keys", "services"})


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
        self.runtime_mappings: dict[str, dict[str, Any]] = {}
        self.service_availability: dict[str, Any] = {}
        self.media_job_admission: dict[str, Any] = {}
        self.service_registry: dict[str, Any] = {
            "schema_version": 1,
            "services": {},
            "protocol_frontends": {},
            "workflows": {},
            "capabilities": {},
        }
        self.extension_package_index: dict[str, Any] = (
            extension_packages.empty_extension_package_index()
        )
        self.user_access_defaults: dict[str, bool] = {
            "self_service_apps": True,
            "auto_create_personal_app": True,
        }
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

    def update_app(
        self, *, app_id: str, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any] | None:
        record = self.apps.get(app_id)
        if record is None:
            return None
        if "name" in data:
            record["name"] = data["name"]
        if "default_profile_id" in data or "profile_id" in data:
            record["profile_id"] = data.get(
                "default_profile_id", data.get("profile_id")
            )
        self.audit.append(
            {
                "at": "2026-05-17T01:00:00Z",
                "actor": actor.subject,
                "action": "app.update",
                "target": app_id,
            }
        )
        return dict(record)

    def disable_app(
        self, *, app_id: str, actor: AdminContext
    ) -> dict[str, Any] | None:
        record = self.apps.get(app_id)
        if record is None:
            return None
        record["disabled_at"] = "2026-05-17T01:00:00Z"
        self.audit.append(
            {
                "at": "2026-05-17T01:00:00Z",
                "actor": actor.subject,
                "action": "app.disable",
                "target": app_id,
            }
        )
        return dict(record)

    def list_profiles(self) -> list[dict[str, Any]]:
        return list(self.profiles)

    def get_profile(self, profile_id: str) -> dict[str, Any] | None:
        return next(
            (profile for profile in self.profiles if profile["id"] == profile_id),
            None,
        )

    def create_profile(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        name = data.get("name") or data.get("label") or data.get("id") or "unnamed"
        if any(
            p.get("name") == name or p.get("label") == name for p in self.profiles
        ):
            raise ValueError(f"profile name {name!r} is already in use")
        record = {
            "id": data.get("id") or self._id("prof"),
            "name": name,
            "label": name,
        }
        self.profiles.append(record)
        return dict(record)

    def update_profile(
        self, *, profile_id: str, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any] | None:
        record = self.get_profile(profile_id)
        if record is None:
            return None
        new_name = data.get("name") or data.get("label")
        if new_name and any(
            p["id"] != profile_id and p.get("name") == new_name
            for p in self.profiles
        ):
            raise ValueError(f"profile name {new_name!r} is already in use")
        if "name" in data:
            record["name"] = data["name"]
        if "label" in data:
            record["label"] = data["label"]
        if "description" in data:
            record["description"] = data["description"]
        if "profile" in data:
            record["profile"] = data["profile"]
        self.audit.append(
            {
                "at": "2026-05-17T01:00:00Z",
                "actor": actor.subject,
                "action": "profile.update",
                "target": profile_id,
            }
        )
        return dict(record)

    def delete_profile(
        self, *, profile_id: str, actor: AdminContext
    ) -> dict[str, Any] | None:
        record = self.get_profile(profile_id)
        if record is None:
            return None
        self.profiles = [p for p in self.profiles if p["id"] != profile_id]
        self.audit.append(
            {
                "at": "2026-05-17T01:00:00Z",
                "actor": actor.subject,
                "action": "profile.delete",
                "target": profile_id,
            }
        )
        return dict(record)

    def list_runtime_profile_mappings(self) -> list[dict[str, Any]]:
        return [
            dict(mapping)
            for mapping in self.runtime_mappings.values()
            if not mapping.get("disabled_at")
        ]

    def create_runtime_profile_mapping(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        mapping_id = self._id("map")
        record = {
            "id": mapping_id,
            "source_kind": data.get("source_kind"),
            "source_value": data.get("source_value") or "*",
            "profile_id": data.get("profile_id") or "default-local",
            "created_at": "2026-05-17T00:00:00Z",
            "created_by": actor.subject,
            "disabled_at": None,
        }
        self.runtime_mappings[mapping_id] = record
        self.audit.append(
            {
                "at": "2026-05-17T00:00:00Z",
                "actor": actor.subject,
                "action": "runtime_profile_mapping.create",
                "target": mapping_id,
            }
        )
        return dict(record)

    def disable_runtime_profile_mapping(
        self, *, mapping_id: str, actor: AdminContext
    ) -> dict[str, Any] | None:
        record = self.runtime_mappings.get(mapping_id)
        if record is None:
            return None
        record["disabled_at"] = "2026-05-17T01:00:00Z"
        self.audit.append(
            {
                "at": "2026-05-17T01:00:00Z",
                "actor": actor.subject,
                "action": "runtime_profile_mapping.disable",
                "target": mapping_id,
            }
        )
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

    def create_admin_automation_key(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        cred_id = self._id("cred")
        raw_key = f"oak_test_{cred_id}_secret"
        stored = {
            "id": cred_id,
            "subject_kind": "service_account",
            "subject_id": self._id("svc"),
            "label": f"{data['name']} admin automation key",
            "prefix": raw_key[:18],
            "role": data.get("role") or "admin",
            "created_at": "2026-05-17T00:00:00Z",
            "revoked_at": None,
        }
        self.credentials[cred_id] = stored
        self.audit.append(
            {
                "at": "2026-05-17T00:00:00Z",
                "actor": actor.subject,
                "action": "admin_key.create",
                "target": cred_id,
            }
        )
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

    def operator_overrides(self) -> dict[str, Any]:
        return {
            "user_access_defaults": dict(self.user_access_defaults),
            "service_availability": dict(self.service_availability),
            "media_job_admission": dict(self.media_job_admission),
        }

    def update_user_access_defaults(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        self.user_access_defaults.update(data)
        self.audit.append(
            {
                "at": "2026-05-17T01:00:00Z",
                "actor": actor.subject,
                "action": "operator.user_access_defaults.update",
                "target": "user_access_defaults",
            }
        )
        return dict(self.user_access_defaults)

    def update_service_availability_overrides(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        self.service_availability = dict(data)
        self.audit.append(
            {
                "at": "2026-05-17T01:00:00Z",
                "actor": actor.subject,
                "action": "operator.service_availability.update",
                "target": "service_availability_overrides",
            }
        )
        return dict(self.service_availability)

    def update_media_job_admission_overrides(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        self.media_job_admission = dict(data)
        self.audit.append(
            {
                "at": "2026-05-17T01:00:00Z",
                "actor": actor.subject,
                "action": "operator.media_job_admission.update",
                "target": "media_job_admission_overrides",
            }
        )
        return dict(self.media_job_admission)

    def get_service_registry(self) -> dict[str, Any]:
        return dict(self.service_registry)

    def update_service_registry(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        self.service_registry = dict(data)
        self.audit.append(
            {
                "at": "2026-05-17T01:00:00Z",
                "actor": actor.subject,
                "action": "operator.service_registry.update",
                "target": "service_registry",
            }
        )
        return dict(self.service_registry)

    # The single-service CRUD methods delegate to the real registry ops so the
    # route tests exercise actual validation, guards, and error mapping.
    def add_service(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        service_id = service_registry.normalize_id(data.get("id") or data.get("name"))
        raw = service_registry.service_form_to_raw(data)
        registry, _ = service_registry.upsert_service_in_registry(
            self.service_registry, service_id=service_id, raw=raw, source="ui"
        )
        self.service_registry = registry
        return dict(registry)

    def update_service(
        self, *, service_id: str, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        raw = service_registry.service_form_to_raw(data)
        registry, _ = service_registry.upsert_service_in_registry(
            self.service_registry,
            service_id=service_id,
            raw=raw,
            source="ui",
            replace=True,
        )
        self.service_registry = registry
        return dict(registry)

    def remove_service(
        self, *, service_id: str, actor: AdminContext
    ) -> dict[str, Any]:
        self.service_registry = service_registry.remove_service_from_registry(
            self.service_registry, service_id
        )
        return dict(self.service_registry)

    def set_service_enabled(
        self, *, service_id: str, enabled: bool, actor: AdminContext
    ) -> dict[str, Any]:
        self.service_registry = service_registry.set_service_enabled(
            self.service_registry, service_id, enabled
        )
        return dict(self.service_registry)

    def get_extension_package_index(self) -> dict[str, Any]:
        return dict(self.extension_package_index)

    def validate_extension_package_source(
        self, *, source: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        del actor
        return extension_packages.validate_extension_package_source(source)

    def enable_extension_package(
        self, *, source: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        review = extension_packages.validate_extension_package_source(source)
        if not review.get("ok"):
            return {
                "extension_package": None,
                "review": review,
                "service_registry": self.service_registry,
                "extension_packages": self.extension_package_index,
            }
        imported = extension_packages.imported_registry_records(review)
        self.service_registry = service_registry.merge_imported_records(
            self.service_registry,
            services=imported["services"],
            protocol_frontends=imported["protocol_frontends"],
            workflows=imported["workflows"],
            capabilities=imported["capabilities"],
        )
        self.extension_package_index = extension_packages.merge_package_index(
            self.extension_package_index,
            review,
            source=source,
        )
        package_id = review["package"]["id"]
        self.audit.append(
            {
                "at": "2026-05-17T01:00:00Z",
                "actor": actor.subject,
                "action": "operator.extension_package.enable",
                "target": package_id,
            }
        )
        return {
            "extension_package": self.extension_package_index["packages"][package_id],
            "review": review,
            "service_registry": self.service_registry,
            "extension_packages": self.extension_package_index,
        }

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


async def _fake_probe(host, port, path):
    # Deterministic stand-in for proxy.probe_backend in route tests: a host of
    # "reachable.test" passes, anything else reports a connection error.
    if host == "reachable.test":
        body = {
            "data": [
                {"id": "gpt-test-1", "object": "model"},
                {"id": "gpt-test-2", "object": "model"},
            ]
        }
        return {"ok": True, "status": 200, "error": "", "url": f"http://{host}:{port}{path}", "body": body}
    return {"ok": False, "status": None, "error": "Connection refused", "url": f"http://{host}:{port}{path}", "body": None}


def _make_client(
    store: FakeAdminStore | None = None,
    *,
    profile_resolver=None,
    service_probe=_fake_probe,
) -> tuple[TestClient, FakeAdminStore]:
    store = store or FakeAdminStore()
    app = FastAPI()
    app.include_router(
        create_admin_router(
            store,
            auth=_admin_auth,
            profile_resolver=profile_resolver,
            service_probe=service_probe,
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
        client.post(
            "/admin/api/runtime-profile-mappings",
            json={
                "source_kind": "oidc_group",
                "source_value": "trusted-users",
                "profile_id": "default-local",
            },
            headers=_auth(),
        )
        resp = client.get("/admin/api/overview", headers=_auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["actor"]["subject"], "u_owner")
        self.assertEqual(body["actor"]["roles"], ["admin"])
        self.assertIn("apps", body["counts"])
        self.assertEqual(body["counts"]["runtime_profile_mappings"], 1)
        self.assertEqual(
            body["runtime_profile_mappings"][0]["source_kind"], "oidc_group"
        )
        self.assertEqual(
            body["access_model"]["nouns"],
            ["user", "app", "profile", "key", "runtime_mapping", "admin_role"],
        )
        self.assertEqual(
            body["access_model"]["runtime_mappings"]["source_kinds"],
            [
                "oidc_group",
                "oidc_role",
                "email",
                "domain",
                "gateway_role",
                "all_users",
            ],
        )
        self.assertIn(
            "separately",
            body["access_model"]["admin_roles"]["relationship"],
        )
        # The seam exposes the "attention" list (things that want a look) so a
        # future Next.js admin overview can consume it. Empty here = all healthy.
        self.assertIn("attention", body)
        self.assertIsInstance(body["attention"], list)

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

    def test_cookie_session_mutations_require_csrf_when_available(self) -> None:
        def session_auth(_request: Request) -> AdminContext:
            return AdminContext(
                subject="u_owner",
                email="owner@example.com",
                roles=frozenset({"owner"}),
                auth_method="session",
                csrf_token="csrf-token",
            )

        app = FastAPI()
        app.include_router(create_admin_router(FakeAdminStore(), auth=session_auth))
        client = TestClient(app)

        action_only = client.post(
            "/admin/api/apps",
            json={"name": "brother-laptop"},
            headers={"x-ai-gateway-admin-action": "1"},
        )
        self.assertEqual(action_only.status_code, 403, action_only.text)
        self.assertEqual(action_only.json()["error"]["code"], "admin_csrf_required")

        allowed = client.post(
            "/admin/api/apps",
            json={"name": "brother-laptop"},
            headers={
                "x-ai-gateway-admin-action": "1",
                "x-ai-gateway-csrf": "csrf-token",
            },
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

    def test_update_app_name_and_profile(self) -> None:
        client, store = _make_client()
        app_id = client.post(
            "/admin/api/apps", json={"name": "old"}, headers=_auth()
        ).json()["app"]["id"]

        resp = client.patch(
            f"/admin/api/apps/{app_id}",
            json={"name": "new", "default_profile_id": "default-local"},
            headers=_auth(),
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["app"]["name"], "new")
        self.assertIn("app.update", [event["action"] for event in store.audit])

    def test_update_app_rejects_unknown_profile(self) -> None:
        client, _ = _make_client()
        app_id = client.post(
            "/admin/api/apps", json={"name": "old"}, headers=_auth()
        ).json()["app"]["id"]

        resp = client.patch(
            f"/admin/api/apps/{app_id}",
            json={"default_profile_id": "missing"},
            headers=_auth(),
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "invalid_app")

    def test_delete_app_disables_it(self) -> None:
        client, store = _make_client()
        app_id = client.post(
            "/admin/api/apps", json={"name": "old"}, headers=_auth()
        ).json()["app"]["id"]

        resp = client.delete(f"/admin/api/apps/{app_id}", headers=_auth())

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNotNone(resp.json()["app"]["disabled_at"])
        self.assertIn("app.disable", [event["action"] for event in store.audit])


class AdminAutomationKeyFlowTests(unittest.TestCase):
    def test_admin_can_create_admin_automation_key(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/admin-keys",
            json={"name": "key-rotation-script", "role": "admin"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        cred = resp.json()["credential"]
        self.assertTrue(cred["api_key"].startswith("oak_"))
        self.assertEqual(cred["subject_kind"], "service_account")
        self.assertEqual(cred["role"], "admin")

        audit_resp = client.get("/admin/api/audit", headers=_auth())
        self.assertNotIn("oak_test_", audit_resp.text)
        self.assertIn("admin_key.create", audit_resp.text)

    def test_admin_automation_key_requires_name(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/admin-keys",
            json={},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "invalid_admin_key")

    def test_admin_automation_key_rejects_superadmin_role(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/admin-keys",
            json={"name": "too-powerful", "role": "superadmin"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "invalid_admin_key")
        self.assertIn("admin, owner", resp.json()["error"]["message"])

    def test_admin_role_cannot_mint_owner_key(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/admin-keys",
            json={"name": "too-powerful", "role": "owner"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "admin_role_forbidden")


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

    def test_update_profile_validates_and_mutates(self) -> None:
        client, store = _make_client()
        profile_id = "default-local"

        resp = client.patch(
            f"/admin/api/profiles/{profile_id}",
            json={
                "name": "Default local updated",
                "description": "local-only",
                "profile": {
                    "id": "default-local",
                    "protocols": ["openai"],
                    "status_visibility": {"level": "basic"},
                },
            },
            headers=_auth(),
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["profile"]["name"], "Default local updated")
        self.assertIn("profile.update", [event["action"] for event in store.audit])

    def test_update_profile_rejects_invalid_profile(self) -> None:
        client, _ = _make_client()
        resp = client.patch(
            "/admin/api/profiles/default-local",
            json={"profile": {"id": "bad", "status_visibility": {"level": "bad"}}},
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

    def test_get_profile_by_id(self) -> None:
        client, store = _make_client()
        profile_id = store.profiles[0]["id"]
        resp = client.get(f"/admin/api/profiles/{profile_id}", headers=_auth())
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["profile"]["id"], profile_id)

    def test_get_profile_by_id_not_found(self) -> None:
        client, _ = _make_client()
        resp = client.get("/admin/api/profiles/no-such-profile", headers=_auth())
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "profile_not_found")

    def test_delete_profile(self) -> None:
        client, store = _make_client()
        profile_id = store.profiles[0]["id"]
        resp = client.delete(f"/admin/api/profiles/{profile_id}", headers=_auth())
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["profile"]["id"], profile_id)
        self.assertIsNone(store.get_profile(profile_id))

    def test_delete_profile_not_found(self) -> None:
        client, _ = _make_client()
        resp = client.delete("/admin/api/profiles/no-such-profile", headers=_auth())
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "profile_not_found")

    def test_create_profile_name_conflict_returns_409(self) -> None:
        client, store = _make_client()
        existing_name = store.profiles[0].get("name") or store.profiles[0].get("label")
        resp = client.post(
            "/admin/api/profiles",
            json={
                "name": existing_name,
                "profile": {
                    "id": "conflict-test",
                    "protocols": ["openai"],
                    "status_visibility": {"level": "basic"},
                },
            },
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "profile_name_conflict")

    def test_update_profile_name_conflict_returns_409(self) -> None:
        client, store = _make_client()
        if len(store.profiles) < 2:
            self.skipTest("need at least 2 profiles in fixture")
        target_id = store.profiles[0]["id"]
        taken_name = store.profiles[1].get("name") or store.profiles[1].get("label")
        resp = client.patch(
            f"/admin/api/profiles/{target_id}",
            json={"name": taken_name},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "profile_name_conflict")


class AdminRuntimeProfileMappingTests(unittest.TestCase):
    def test_create_list_and_disable_runtime_profile_mapping(self) -> None:
        client, _ = _make_client()
        created = client.post(
            "/admin/api/runtime-profile-mappings",
            json={
                "source_kind": "oidc_group",
                "source_value": "trusted-users",
                "profile_id": "default-local",
            },
            headers=_auth(),
        )
        self.assertEqual(created.status_code, 201, created.text)
        mapping = created.json()["mapping"]
        self.assertEqual(mapping["source_kind"], "oidc_group")

        listed = client.get(
            "/admin/api/runtime-profile-mappings",
            headers=_auth(),
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(
            [row["id"] for row in listed.json()["mappings"]],
            [mapping["id"]],
        )

        disabled = client.delete(
            f"/admin/api/runtime-profile-mappings/{mapping['id']}",
            headers=_auth(),
        )
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertIsNotNone(disabled.json()["mapping"]["disabled_at"])

    def test_runtime_profile_mapping_rejects_unknown_profile(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/runtime-profile-mappings",
            json={
                "source_kind": "oidc_group",
                "source_value": "trusted-users",
                "profile_id": "missing",
            },
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()["error"]["code"], "invalid_runtime_mapping")


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


class AdminOperatorOverrideTests(unittest.TestCase):
    def test_admin_can_write_and_read_operator_overrides(self) -> None:
        client, store = _make_client()
        access_resp = client.put(
            "/admin/api/operator/user-access-defaults",
            json={"self_service_apps": False},
            headers=_auth(),
        )
        service_resp = client.put(
            "/admin/api/operator/service-availability",
            json={
                "overrides": {
                    "openai": {
                        "state": "degraded",
                        "reason": "provider incident",
                        "retry_after_seconds": 120,
                    }
                }
            },
            headers=_auth(),
        )
        media_resp = client.put(
            "/admin/api/operator/media-job-admission",
            json={
                "disabled_intents": ["video.generate"],
                "disabled_resource_classes": ["gpu"],
                "reserved_tokens": ["gpu"],
                "max_queue_depth": 5,
                "retry_after_seconds": 30,
            },
            headers=_auth(),
        )
        list_resp = client.get("/admin/api/operator/overrides", headers=_auth())

        self.assertEqual(service_resp.status_code, 200, service_resp.text)
        self.assertEqual(media_resp.status_code, 200, media_resp.text)
        self.assertEqual(access_resp.status_code, 200, access_resp.text)
        self.assertEqual(list_resp.status_code, 200, list_resp.text)
        overrides = list_resp.json()["overrides"]
        self.assertFalse(overrides["user_access_defaults"]["self_service_apps"])
        self.assertEqual(
            overrides["service_availability"]["openai"]["state"],
            "degraded",
        )
        self.assertEqual(
            overrides["media_job_admission"]["disabled_intents"],
            ["video.generate"],
        )
        self.assertEqual(
            [event["action"] for event in store.audit[-3:]],
            [
                "operator.user_access_defaults.update",
                "operator.service_availability.update",
                "operator.media_job_admission.update",
            ],
        )

    def test_service_availability_updates_merge_and_clear_defaults(self) -> None:
        client, store = _make_client()
        store.service_availability = {
            "openai": {
                "state": "maintenance",
                "reason": "planned",
                "retry_after_seconds": 60,
            },
            "comfyui": {
                "state": "disabled",
                "reason": None,
                "retry_after_seconds": None,
            },
        }

        update_resp = client.put(
            "/admin/api/operator/service-availability",
            json={
                "overrides": {
                    "openai": {
                        "state": "degraded",
                        "reason": "slow",
                    }
                }
            },
            headers=_auth(),
        )
        clear_resp = client.put(
            "/admin/api/operator/service-availability",
            json={"overrides": {"openai": {"state": "available"}}},
            headers=_auth(),
        )

        self.assertEqual(update_resp.status_code, 200, update_resp.text)
        self.assertEqual(clear_resp.status_code, 200, clear_resp.text)
        self.assertNotIn("openai", store.service_availability)
        self.assertEqual(
            store.service_availability["comfyui"]["state"],
            "disabled",
        )

    def test_admin_can_read_service_registry(self) -> None:
        client, store = _make_client()
        store.service_registry = {
            "schema_version": 1,
            "services": {
                "openai": {
                    "id": "openai",
                    "name": "OpenAI",
                    "kind": "provider",
                    "source": "provisioned",
                    "enabled": True,
                    "capabilities": ["chat.completions"],
                }
            },
            "protocol_frontends": {},
        }

        resp = client.get(
            "/admin/api/operator/service-registry",
            headers=_auth(),
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        registry = resp.json()["service_registry"]
        self.assertEqual(registry["services"]["openai"]["name"], "OpenAI")
        self.assertEqual(registry["protocol_frontends"], {})

    def test_admin_can_update_service_registry(self) -> None:
        client, store = _make_client()

        resp = client.put(
            "/admin/api/operator/service-registry",
            json={
                "services": {
                    "Open AI": {
                        "name": "OpenAI",
                        "source": "provisioned",
                        "capabilities": ["chat.completions"],
                        "config": {"base_url": "https://api.openai.com/v1"},
                    }
                },
                "protocol_frontends": {
                    "OpenAI API": {
                        "name": "OpenAI-compatible API",
                        "protocol": "openai",
                        "routes": ["/v1/chat/completions"],
                        "intents": ["chat.completions"],
                        "canonical": True,
                    }
                },
            },
            headers=_auth(),
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        registry = resp.json()["service_registry"]
        self.assertEqual(sorted(registry["services"]), ["open-ai"])
        self.assertEqual(registry["services"]["open-ai"]["id"], "open-ai")
        self.assertEqual(
            registry["services"]["open-ai"]["config"]["base_url"],
            "https://api.openai.com/v1",
        )
        self.assertEqual(
            registry["protocol_frontends"]["openai-api"]["routes"],
            ["/v1/chat/completions"],
        )
        self.assertEqual(store.service_registry, registry)
        self.assertIn(
            "operator.service_registry.update",
            [event["action"] for event in store.audit],
        )

    def test_admin_can_get_generated_litellm_config(self) -> None:
        client, store = _make_client()
        store.service_registry = {
            "schema_version": 1,
            "services": {
                "litellm": {
                    "id": "litellm",
                    "name": "LiteLLM",
                    "kind": "llm-substrate",
                    "enabled": True,
                    "capabilities": ["llm.execution"],
                    "config": {
                        "base_url": "http://127.0.0.1:4011",
                        "execution_substrate": "litellm",
                    },
                },
                "local-llama": {
                    "id": "local-llama",
                    "name": "Local llama.cpp",
                    "kind": "provider",
                    "enabled": True,
                    "capabilities": ["openai"],
                    "config": {
                        "protocol": "openai-compatible",
                        "base_url": "http://127.0.0.1:8081/v1",
                        "api_key_env": "LOCAL_LLM_KEY",
                        "models": [
                            {
                                "id": "assistant-default",
                                "upstream_model": "qwen-27b",
                            }
                        ],
                    },
                }
            },
            "protocol_frontends": {},
            "workflows": {},
            "capabilities": {},
        }
        store.profiles = [
            {
                "id": "family",
                "profile": {"id": "family", "protocols": ["openai.chat"]},
            }
        ]

        resp = client.get(
            "/admin/api/operator/litellm-config",
            headers=_auth(),
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        config = resp.json()["litellm_config"]
        self.assertEqual(config["model_list"][0]["model_name"], "assistant-default")
        self.assertEqual(
            config["model_list"][0]["litellm_params"]["api_base"],
            "http://127.0.0.1:8081/v1",
        )
        self.assertEqual(
            config["model_list"][0]["litellm_params"]["api_key"],
            "os.environ/LOCAL_LLM_KEY",
        )
        self.assertNotIn("metadata", config)

    def test_admin_can_list_validate_and_enable_extension_package(self) -> None:
        client, store = _make_client()
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package_dir = root / "package"
            (package_dir / "objects").mkdir(parents=True)
            (package_dir / "extension.yml").write_text(
                """
schema_version: 1
kind: extension_package
id: demo-package
version: 0.1.0
objects:
  services:
    - objects/service.yml
  protocol_frontends:
    - objects/frontend.yml
  workflows:
    - objects/workflow.yml
  capabilities:
    - objects/capability.yml
""",
                encoding="utf-8",
            )
            (package_dir / "objects" / "service.yml").write_text(
                """
id: demo-service
name: Demo Service
config:
  base_url: http://127.0.0.1:9999
""",
                encoding="utf-8",
            )
            (package_dir / "objects" / "frontend.yml").write_text(
                """
id: demo-api
name: Demo API
protocol: demo
routes:
  - /demo/v1
""",
                encoding="utf-8",
            )
            (package_dir / "objects" / "workflow.yml").write_text(
                """
schema_version: 1
id: image.generate.demo
intent: image.generate
inputs:
  prompt:
    type: string
outputs:
  image:
    media_type: image/png
bindings:
  comfyui:
    workflow: workflows/demo.json
    map:
      prompt:
        - node: "6"
          input: text
""",
                encoding="utf-8",
            )
            (package_dir / "objects" / "capability.yml").write_text(
                """
kind: capability
id: demo-capability
intent: image.generate
backend: comfyui
workflow: image.generate.demo
input_modalities: [text]
output_modalities: [image]
execution: job
""",
                encoding="utf-8",
            )
            source = {"type": "local_directory", "path": str(package_dir)}

            list_resp = client.get("/admin/api/extensions", headers=_auth())
            validate_resp = client.post(
                "/admin/api/extensions/validate",
                json={"source": source},
                headers=_auth(),
            )
            enable_resp = client.post(
                "/admin/api/extensions/enable",
                json={"source": source},
                headers=_auth(),
            )

        self.assertEqual(list_resp.status_code, 200, list_resp.text)
        self.assertEqual(validate_resp.status_code, 200, validate_resp.text)
        self.assertTrue(validate_resp.json()["review"]["ok"])
        self.assertEqual(enable_resp.status_code, 200, enable_resp.text)
        body = enable_resp.json()
        self.assertEqual(body["extension_package"]["id"], "demo-package")
        self.assertIn("demo-service", body["service_registry"]["services"])
        self.assertIn("demo-api", body["service_registry"]["protocol_frontends"])
        self.assertIn("image.generate.demo", body["service_registry"]["workflows"])
        self.assertIn("demo-capability", body["service_registry"]["capabilities"])
        self.assertIn("demo-package", store.extension_package_index["packages"])
        self.assertIn(
            "operator.extension_package.enable",
            [event["action"] for event in store.audit],
        )

    def test_enable_extension_package_rejects_invalid_package(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/extensions/enable",
            json={"source": {"type": "local_directory", "path": "/no/such/package"}},
            headers=_auth(),
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "invalid_extension_package")

    def test_extension_enable_pins_reviewed_resolved_git_ref(self) -> None:
        client, store = _make_client()
        calls: list[dict[str, Any]] = []

        def fake_review(source: Mapping[str, Any], **_: Any) -> dict[str, Any]:
            calls.append(dict(source))
            return {
                "package": {
                    "schema_version": 1,
                    "kind": "extension_package",
                    "id": "git-reviewed-package",
                    "version": "0.1.0",
                },
                "package_hash": "a" * 64,
                "services": [],
                "protocol_frontends": [],
                "capabilities": [],
                "workflows": [],
                "adapters": [],
                "translators": [],
                "code_objects": [],
                "requires_code_trust": False,
                "warnings": [],
                "errors": [],
                "ok": True,
                "schema_version": 1,
                "kind": "extension_package",
                "id": "git-reviewed-package",
                "version": "0.1.0",
                "object_counts": {
                    "services": 0,
                    "protocol_frontends": 0,
                    "capabilities": 0,
                    "workflows": 0,
                },
                "resolved_source": {
                    "type": "git",
                    "url": "/tmp/repo",
                    "ref": "main",
                    "resolved_ref": source.get("resolved_ref") or "newer-commit",
                },
            }

        with patch.object(
            extension_packages,
            "validate_extension_package_source",
            side_effect=fake_review,
        ):
            resp = client.post(
                "/admin/api/extensions/enable",
                json={
                    "source": {"type": "git", "url": "/tmp/repo", "ref": "main"},
                    "review": {
                        "resolved_source": {
                            "type": "git",
                            "url": "/tmp/repo",
                            "ref": "main",
                            "resolved_ref": "reviewed-commit",
                        }
                    },
                },
                headers=_auth(),
            )

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(calls, [
            {
                "type": "git",
                "url": "/tmp/repo",
                "ref": "main",
                "resolved_ref": "reviewed-commit",
            }
        ])
        self.assertEqual(
            store.extension_package_index["packages"]["git-reviewed-package"]["source"][
                "resolved_ref"
            ],
            "reviewed-commit",
        )

    def test_operator_override_write_requires_admin(self) -> None:
        client, _ = _make_client()
        resp = client.put(
            "/admin/api/operator/media-job-admission",
            json={"disabled_intents": []},
        )
        self.assertEqual(resp.status_code, 401)

    def test_operator_override_validation_rejects_bad_values(self) -> None:
        client, _ = _make_client()
        access_resp = client.put(
            "/admin/api/operator/user-access-defaults",
            json={"self_service_apps": "maybe"},
            headers=_auth(),
        )
        service_resp = client.put(
            "/admin/api/operator/service-availability",
            json={"openai": {"state": "paused"}},
            headers=_auth(),
        )
        media_resp = client.put(
            "/admin/api/operator/media-job-admission",
            json={"reserved_tokens": "gpu"},
            headers=_auth(),
        )

        self.assertEqual(access_resp.status_code, 400)
        self.assertEqual(
            access_resp.json()["error"]["code"],
            "invalid_operator_override",
        )
        self.assertEqual(service_resp.status_code, 400)
        self.assertEqual(
            service_resp.json()["error"]["code"],
            "invalid_operator_override",
        )
        self.assertEqual(media_resp.status_code, 400)
        self.assertEqual(
            media_resp.json()["error"]["code"],
            "invalid_operator_override",
        )


class AdminServiceCrudTests(unittest.TestCase):
    def _store_with_provisioned(self) -> FakeAdminStore:
        store = FakeAdminStore()
        store.service_registry = service_registry.build_service_registry_seed(
            {
                "backends": {
                    "tabby": {
                        "name": "TabbyAPI",
                        "host": "127.0.0.1",
                        "port": 5050,
                        "capabilities": ["vision"],
                        "source": "provisioned",
                    }
                }
            }
        )
        return store

    def test_add_service_creates_ui_record(self) -> None:
        client, store = _make_client(self._store_with_provisioned())
        resp = client.post(
            "/admin/api/operator/services",
            json={"name": "My vLLM", "preset": "vllm", "base_url": "http://10.0.0.5:8000/v1"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 201)
        services = resp.json()["service_registry"]["services"]
        self.assertIn("my-vllm", services)
        self.assertEqual(services["my-vllm"]["source"], "ui")
        self.assertEqual(services["my-vllm"]["config"]["port"], 8000)

    def test_add_service_requires_name_and_url(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/operator/services", json={"name": "x"}, headers=_auth()
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "invalid_service")

    def test_add_service_rejects_inline_api_key(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/operator/services",
            json={"name": "x", "base_url": "http://h:1/v1", "api_key": "sk-secret"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("api_key_env", resp.json()["error"]["message"])

    def test_add_duplicate_is_conflict(self) -> None:
        client, _ = _make_client()
        payload = {"name": "dup", "base_url": "http://h:1/v1"}
        self.assertEqual(
            client.post("/admin/api/operator/services", json=payload, headers=_auth()).status_code,
            201,
        )
        resp = client.post("/admin/api/operator/services", json=payload, headers=_auth())
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "service_conflict")

    def test_update_service_edits_ui_record(self) -> None:
        client, _ = _make_client()
        client.post(
            "/admin/api/operator/services",
            json={"name": "edit-me", "base_url": "http://h:1/v1", "capabilities": ["chat"]},
            headers=_auth(),
        )
        resp = client.patch(
            "/admin/api/operator/services/edit-me",
            json={"base_url": "http://h:1/v1", "capabilities": ["chat", "completions"], "role": "standby"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 200)
        svc = resp.json()["service_registry"]["services"]["edit-me"]
        self.assertEqual(svc["capabilities"], ["chat", "completions"])
        self.assertEqual(svc["role"], "standby")

    def test_edit_provisioned_is_forbidden(self) -> None:
        client, _ = _make_client(self._store_with_provisioned())
        resp = client.patch(
            "/admin/api/operator/services/tabby",
            json={"base_url": "http://h:1/v1"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "service_not_editable")

    def test_delete_ui_service_then_404(self) -> None:
        client, _ = _make_client()
        client.post(
            "/admin/api/operator/services",
            json={"name": "gone", "base_url": "http://h:1/v1"},
            headers=_auth(),
        )
        self.assertEqual(
            client.delete("/admin/api/operator/services/gone", headers=_auth()).status_code,
            200,
        )
        resp = client.delete("/admin/api/operator/services/gone", headers=_auth())
        self.assertEqual(resp.status_code, 404)

    def test_delete_provisioned_is_forbidden(self) -> None:
        client, _ = _make_client(self._store_with_provisioned())
        resp = client.delete("/admin/api/operator/services/tabby", headers=_auth())
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "service_not_editable")

    def test_toggle_enabled_on_any_source(self) -> None:
        client, _ = _make_client(self._store_with_provisioned())
        resp = client.post(
            "/admin/api/operator/services/tabby/enabled",
            json={"enabled": False},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(
            resp.json()["service_registry"]["services"]["tabby"]["enabled"]
        )

    def test_test_connection_reports_reachability(self) -> None:
        client, _ = _make_client()
        ok = client.post(
            "/admin/api/operator/services/test",
            json={"base_url": "http://reachable.test:8000/v1"},
            headers=_auth(),
        )
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.json()["probe"]["ok"])

        bad = client.post(
            "/admin/api/operator/services/test",
            json={"base_url": "http://nope.test:9999/v1"},
            headers=_auth(),
        )
        self.assertEqual(bad.status_code, 200)
        self.assertFalse(bad.json()["probe"]["ok"])
        self.assertIn("Connection refused", bad.json()["probe"]["error"])

    def test_detect_models_returns_model_ids_from_reachable_host(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/operator/services/detect-models",
            json={"base_url": "http://reachable.test:8000/v1"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 200)
        detected = resp.json()["detected"]
        self.assertTrue(detected["ok"])
        self.assertEqual(detected["error"], "")
        self.assertEqual(detected["models"], ["gpt-test-1", "gpt-test-2"])

    def test_detect_models_returns_ok_false_for_unreachable_host(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/operator/services/detect-models",
            json={"base_url": "http://nope.test:9999/v1"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 200)
        detected = resp.json()["detected"]
        self.assertFalse(detected["ok"])
        self.assertIn("Connection refused", detected["error"])
        self.assertEqual(detected["models"], [])

    def test_detect_models_503_when_probe_not_injected(self) -> None:
        client, _ = _make_client(service_probe=None)
        resp = client.post(
            "/admin/api/operator/services/detect-models",
            json={"base_url": "http://reachable.test:8000/v1"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["error"]["code"], "probe_unavailable")

    def test_service_routes_require_admin(self) -> None:
        client, _ = _make_client()
        for method, url in (
            ("POST", "/admin/api/operator/services"),
            ("PATCH", "/admin/api/operator/services/x"),
            ("DELETE", "/admin/api/operator/services/x"),
            ("POST", "/admin/api/operator/services/x/enabled"),
            ("POST", "/admin/api/operator/services/test"),
            ("POST", "/admin/api/operator/services/detect-models"),
        ):
            resp = client.request(method, url, json={})
            self.assertEqual(resp.status_code, 401, f"{method} {url}")


if __name__ == "__main__":
    unittest.main()
