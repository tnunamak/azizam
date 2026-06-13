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

    def enable_app(
        self, *, app_id: str, actor: AdminContext
    ) -> dict[str, Any] | None:
        record = self.apps.get(app_id)
        if record is None:
            return None
        record["disabled_at"] = None
        self.audit.append(
            {
                "at": "2026-05-17T02:00:00Z",
                "actor": actor.subject,
                "action": "app.enable",
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
            "display_name": data.get("label") or "api key",
            "prefix": raw_key[:11],
            "created_at": "2026-05-17T00:00:00Z",
            "revoked_at": None,
            "last_used_at": None,
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

    def list_audit_page(
        self,
        *,
        limit: int = 50,
        actor_id: str | None = None,
        action_prefix: str | None = None,
        before_created_at: int | None = None,
        before_id: str | None = None,
        target_kind: str | None = None,
        target_id: str | None = None,
    ) -> dict[str, Any]:
        """Minimal in-memory implementation of cursor-paged audit for route tests."""
        rows = list(self.audit)
        # Apply keyset cursor (audit rows in this fake store have a 'created_at'
        # and 'id' field when the real store is used; the fake rows may only
        # have partial fields — fall back gracefully).
        if before_created_at is not None and before_id is not None:
            rows = [
                r for r in rows
                if (
                    int(r.get("created_at", 0)) < before_created_at
                    or (
                        int(r.get("created_at", 0)) == before_created_at
                        and str(r.get("id", "")) < before_id
                    )
                )
            ]
        if actor_id is not None:
            rows = [r for r in rows if r.get("actor_id") == actor_id or r.get("actor") == actor_id]
        if action_prefix is not None:
            rows = [r for r in rows if str(r.get("action", "")).startswith(action_prefix)]
        if target_kind is not None:
            rows = [r for r in rows if r.get("target_kind") == target_kind]
        if target_id is not None:
            rows = [r for r in rows if r.get("target_id") == target_id or r.get("target") == target_id]
        # Sort newest-first by (created_at DESC, id DESC); fall back to list order.
        rows_sorted = sorted(
            rows,
            key=lambda r: (int(r.get("created_at", 0)), str(r.get("id", ""))),
            reverse=True,
        )
        fetch = rows_sorted[: limit + 1]
        has_more = len(fetch) == limit + 1
        page = fetch[:limit]
        next_cursor: str | None = None
        if has_more and page:
            last = page[-1]
            next_cursor = f"{last.get('created_at', 0)}:{last.get('id', '')}"
        return {"records": page, "next_cursor": next_cursor}

    def get_oidc_config_setting(self) -> dict[str, Any] | None:
        raw: dict[str, Any] | None = getattr(self, "_oidc_config", None)
        if raw is None:
            return None
        redacted = dict(raw)
        if redacted.get("client_secret"):
            redacted["client_secret"] = "<redacted>"
            redacted["client_secret_set"] = True
        else:
            redacted.setdefault("client_secret", "")
            redacted["client_secret_set"] = False
        return redacted

    def update_oidc_config_setting(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        current: dict[str, Any] = getattr(self, "_oidc_config", {}) or {}
        merged = {**current, **data}
        # Never return client_secret in plain form — mirror real store behaviour.
        redacted = dict(merged)
        if "client_secret" in redacted and redacted["client_secret"]:
            redacted["client_secret"] = "<redacted>"
            redacted["client_secret_set"] = True
        else:
            redacted.setdefault("client_secret", "")
            redacted["client_secret_set"] = False
        self._oidc_config = merged  # type: ignore[attr-defined]
        return redacted


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

    def test_overview_includes_service_counts(self) -> None:
        client, store = _make_client()
        store.service_registry = {
            "schema_version": 1,
            "services": {
                "openai": {"id": "openai", "name": "OpenAI", "enabled": True},
                "local-llm": {"id": "local-llm", "name": "Local LLM", "enabled": False},
                "comfy": {"id": "comfy", "name": "ComfyUI", "enabled": True},
            },
            "protocol_frontends": {},
            "workflows": {},
            "capabilities": {},
        }
        resp = client.get("/admin/api/overview", headers=_auth())
        self.assertEqual(resp.status_code, 200, resp.text)
        counts = resp.json()["counts"]
        self.assertEqual(counts["services_total"], 3)
        self.assertEqual(counts["services_enabled"], 2)

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


class AdminSessionTests(unittest.TestCase):
    """GET /admin/api/session — the JSON bootstrap for browser clients."""

    def test_session_requires_auth(self) -> None:
        client, _ = _make_client()
        resp = client.get("/admin/api/session")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "admin_unauthenticated")

    def test_session_returns_actor_identity_for_key_auth(self) -> None:
        client, _ = _make_client()
        resp = client.get("/admin/api/session", headers=_auth())
        self.assertEqual(resp.status_code, 200, resp.text)
        actor = resp.json()["actor"]
        self.assertEqual(actor["subject"], "u_owner")
        self.assertEqual(actor["email"], "owner@example.com")
        self.assertEqual(actor["roles"], ["admin"])
        # Bearer-key callers don't need CSRF; the field is present but empty.
        self.assertEqual(actor["csrf_token"], "")

    def test_session_exposes_csrf_token_for_cookie_sessions(self) -> None:
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
        resp = TestClient(app).get("/admin/api/session")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["actor"]["csrf_token"], "csrf-token")


class AdminCredentialListingTests(unittest.TestCase):
    """GET /admin/api/credentials — flat scrubbed list for the keys surface."""

    def test_list_credentials_requires_auth(self) -> None:
        client, _ = _make_client()
        resp = client.get("/admin/api/credentials")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "admin_unauthenticated")

    def test_list_credentials_returns_scrubbed_flat_list(self) -> None:
        client, _ = _make_client()
        app_id = client.post(
            "/admin/api/apps", json={"name": "brother-laptop"}, headers=_auth()
        ).json()["app"]["id"]
        minted = client.post(
            f"/admin/api/apps/{app_id}/keys",
            json={"label": "first key"},
            headers=_auth(),
        ).json()["credential"]

        resp = client.get("/admin/api/credentials", headers=_auth())
        self.assertEqual(resp.status_code, 200, resp.text)
        creds = resp.json()["credentials"]
        self.assertEqual(len(creds), 1)
        record = creds[0]
        self.assertEqual(record["id"], minted["id"])
        self.assertEqual(record["app_id"], app_id)
        # The Apps & Keys surface renders these; the store always has them.
        self.assertIn("prefix", record)
        self.assertIn("created_at", record)
        self.assertIn("last_used_at", record)
        # List responses never carry raw key material (the display prefix is
        # fine — it's what SecretMask renders — but the full key must not be).
        self.assertNotIn("api_key", record)
        self.assertNotIn(minted["api_key"], resp.text)


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

    def test_enable_app_clears_disabled_at(self) -> None:
        client, store = _make_client()
        app_id = client.post(
            "/admin/api/apps", json={"name": "toggled"}, headers=_auth()
        ).json()["app"]["id"]
        # disable first
        client.delete(f"/admin/api/apps/{app_id}", headers=_auth())
        self.assertIsNotNone(store.apps[app_id]["disabled_at"])

        resp = client.post(f"/admin/api/apps/{app_id}/enable", headers=_auth())

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(resp.json()["app"]["disabled_at"])
        self.assertIn("app.enable", [event["action"] for event in store.audit])

    def test_enable_already_active_app_is_idempotent(self) -> None:
        client, _ = _make_client()
        app_id = client.post(
            "/admin/api/apps", json={"name": "active"}, headers=_auth()
        ).json()["app"]["id"]

        resp = client.post(f"/admin/api/apps/{app_id}/enable", headers=_auth())

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(resp.json()["app"]["disabled_at"])

    def test_enable_unknown_app_returns_404(self) -> None:
        client, _ = _make_client()
        resp = client.post("/admin/api/apps/app_missing/enable", headers=_auth())
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "app_not_found")

    def test_enable_requires_admin(self) -> None:
        client, _ = _make_client()
        resp = client.post("/admin/api/apps/app_x/enable")
        self.assertEqual(resp.status_code, 401)


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
        body = resp.json()
        # New response shape: {records, next_cursor}
        self.assertIn("records", body)
        entries = body["records"]
        self.assertTrue(any(e.get("action") == "app.create" for e in entries))

    def test_audit_page_size_respected(self) -> None:
        client, store = _make_client()
        # Populate the audit log directly with created_at so the fake store
        # sorts correctly.
        for i in range(10):
            store.audit.append({"action": f"event_{i}", "created_at": i, "id": f"aud_{i}"})
        resp = client.get("/admin/api/audit?limit=3", headers=_auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("records", body)
        self.assertEqual(len(body["records"]), 3)

    def test_audit_pagination_reaches_oldest_event(self) -> None:
        """Paging via next_cursor must reach events older than the first page."""
        client, store = _make_client()
        # Seed 260 audit events with distinct created_at + id values so all
        # pages can be distinguished.  The oldest event has action "oldest_event".
        for i in range(259):
            store.audit.append({
                "action": f"bulk_event_{i}",
                "created_at": 1000 + i,
                "id": f"aud_{i:04d}",
                "actor_id": "usr_bulk",
            })
        store.audit.append({
            "action": "oldest_event",
            "created_at": 999,
            "id": "aud_oldest",
            "actor_id": "usr_special",
        })
        # Page through with limit=50 until exhausted.
        seen_actions: list[str] = []
        cursor: str | None = None
        iterations = 0
        while True:
            url = "/admin/api/audit?limit=50"
            if cursor:
                url += f"&before={cursor}"
            resp = client.get(url, headers=_auth())
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            page = body["records"]
            seen_actions.extend(r.get("action", "") for r in page)
            cursor = body["next_cursor"]
            iterations += 1
            if cursor is None:
                break
            # Safety guard — should never need more than 10 pages for 260 rows.
            self.assertLess(iterations, 10, "cursor pagination did not terminate")
        self.assertIn("oldest_event", seen_actions)

    def test_audit_actor_id_filter_reaches_old_events(self) -> None:
        """actor_id filter must return matching events older than the first 200 rows.

        This is the regression test for the deception bug: filtering by actor
        client-side over a 200-row window silently omits older matching events.
        """
        client, store = _make_client()
        # Flood 210 events from a common actor so a naive 200-row window fills up.
        for i in range(210):
            store.audit.append({
                "action": f"noise_event_{i}",
                "created_at": 2000 + i,
                "id": f"aud_noise_{i:04d}",
                "actor_id": "usr_noise",
            })
        # Add a target event from a different actor at an older timestamp.
        store.audit.append({
            "action": "target_action",
            "created_at": 1999,
            "id": "aud_target",
            "actor_id": "usr_target",
        })
        # actor_id filter must find the target event even though it's older
        # than the 200 newest events.
        resp = client.get("/admin/api/audit?actor_id=usr_target", headers=_auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        actions = [r.get("action") for r in body["records"]]
        self.assertIn("target_action", actions)
        # Must NOT include the noise events.
        for a in actions:
            self.assertEqual(a, "target_action")

    def test_audit_action_prefix_filter(self) -> None:
        client, store = _make_client()
        store.audit.append({"action": "app.create", "created_at": 100, "id": "a1", "actor_id": "u"})
        store.audit.append({"action": "service.update", "created_at": 101, "id": "a2", "actor_id": "u"})
        store.audit.append({"action": "app.disable", "created_at": 102, "id": "a3", "actor_id": "u"})
        resp = client.get("/admin/api/audit?action_prefix=app.", headers=_auth())
        self.assertEqual(resp.status_code, 200)
        actions = [r.get("action") for r in resp.json()["records"]]
        self.assertIn("app.create", actions)
        self.assertIn("app.disable", actions)
        self.assertNotIn("service.update", actions)

    def test_audit_next_cursor_null_at_exhaustion(self) -> None:
        client, store = _make_client()
        for i in range(5):
            store.audit.append({"action": f"ev_{i}", "created_at": i, "id": f"aud_{i}"})
        # With limit=10 and only 5 rows, next_cursor must be null.
        resp = client.get("/admin/api/audit?limit=10", headers=_auth())
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["next_cursor"])

    def test_audit_filter_and_cursor_compose(self) -> None:
        """action_prefix filter must still work when a before cursor is supplied."""
        client, store = _make_client()
        for i in range(10):
            store.audit.append({
                "action": "app.create" if i % 2 == 0 else "service.update",
                "created_at": 1000 + i,
                "id": f"aud_{i:03d}",
                "actor_id": "u",
            })
        # First page — limit 3 with filter.
        resp1 = client.get("/admin/api/audit?action_prefix=app.&limit=3", headers=_auth())
        self.assertEqual(resp1.status_code, 200)
        body1 = resp1.json()
        # 5 matching events total; page 1 returns 3, next_cursor non-null.
        self.assertEqual(len(body1["records"]), 3)
        cursor = body1["next_cursor"]
        self.assertIsNotNone(cursor)
        # Second page.
        resp2 = client.get(
            f"/admin/api/audit?action_prefix=app.&limit=3&before={cursor}",
            headers=_auth(),
        )
        self.assertEqual(resp2.status_code, 200)
        body2 = resp2.json()
        # Remaining 2 events on page 2, no further cursor.
        self.assertEqual(len(body2["records"]), 2)
        self.assertIsNone(body2["next_cursor"])
        # All records should match the filter.
        for r in body2["records"]:
            self.assertEqual(r["action"], "app.create")

    def test_audit_bad_cursor_returns_400(self) -> None:
        client, _ = _make_client()
        resp = client.get("/admin/api/audit?before=notavalidcursor", headers=_auth())
        self.assertEqual(resp.status_code, 400)

    def test_audit_oidc_groups_claim_round_trips(self) -> None:
        """groups_claim field round-trips through GET/PUT oidc-config."""
        client, store = _make_client()
        # PUT with groups_claim.
        put_resp = client.put(
            "/admin/api/oidc-config",
            json={
                "enabled": False,
                "issuer": "https://idp.example.com",
                "client_id": "my-client",
                "client_secret": "s3cr3t",
                "redirect_uri": "https://gw.example.com/auth/callback",
                "groups_claim": "cognito:groups",
            },
            headers=_auth(),
        )
        self.assertEqual(put_resp.status_code, 200, put_resp.text)
        cfg_put = put_resp.json()["oidc_config"]
        self.assertEqual(cfg_put.get("groups_claim"), "cognito:groups")
        # client_secret must be redacted.
        self.assertNotEqual(cfg_put.get("client_secret"), "s3cr3t")

        # GET must return the stored groups_claim plainly (not redacted).
        get_resp = client.get("/admin/api/oidc-config", headers=_auth())
        self.assertEqual(get_resp.status_code, 200)
        cfg_get = get_resp.json()["oidc_config"]
        self.assertEqual(cfg_get.get("groups_claim"), "cognito:groups")
        # client_secret still must not be returned.
        self.assertNotEqual(cfg_get.get("client_secret"), "s3cr3t")

    def test_audit_oidc_groups_claim_default(self) -> None:
        """When groups_claim is omitted, the stored value is preserved (patch semantics)."""
        client, store = _make_client()
        # Save an initial config with a custom groups_claim.
        client.put(
            "/admin/api/oidc-config",
            json={
                "enabled": False,
                "issuer": "https://idp.example.com",
                "client_id": "c",
                "client_secret": "s",
                "redirect_uri": "https://gw/cb",
                "groups_claim": "my_groups",
            },
            headers=_auth(),
        )
        # Update a different field without touching groups_claim.
        patch_resp = client.put(
            "/admin/api/oidc-config",
            json={"enabled": False, "issuer": "https://other.example.com"},
            headers=_auth(),
        )
        self.assertEqual(patch_resp.status_code, 200)
        cfg = patch_resp.json()["oidc_config"]
        # groups_claim must be preserved from the earlier PUT.
        self.assertEqual(cfg.get("groups_claim"), "my_groups")


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

    def test_edit_provisioned_is_allowed(self) -> None:
        # EDITABILITY LAW: provisioned rows are editable — source is provenance,
        # not a permission gate. Only managed-substrate (litellm) is locked.
        client, _ = _make_client(self._store_with_provisioned())
        resp = client.patch(
            "/admin/api/operator/services/tabby",
            json={"base_url": "http://h:1/v1", "name": "TabbyAPI Updated"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 200)
        svc = resp.json()["service_registry"]["services"]["tabby"]
        self.assertEqual(svc["name"], "TabbyAPI Updated")

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

    def test_delete_provisioned_is_allowed(self) -> None:
        # EDITABILITY LAW: provisioned rows are removable after the one-time seed.
        # The DB is the source of truth; the YAML is never re-merged on reboot.
        client, _ = _make_client(self._store_with_provisioned())
        resp = client.delete("/admin/api/operator/services/tabby", headers=_auth())
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("tabby", resp.json()["service_registry"]["services"])

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

    def test_patch_service_accepts_models_list(self) -> None:
        """PATCH with a models list must be accepted and round-trip through the registry."""
        client, store = _make_client()
        # Create a service first.
        client.post(
            "/admin/api/operator/services",
            json={"name": "tgw", "preset": "openai-compatible", "base_url": "http://h:7860/v1"},
            headers=_auth(),
        )
        # Patch with a models list.
        resp = client.patch(
            "/admin/api/operator/services/tgw",
            json={
                "base_url": "http://h:7860/v1",
                "models": [
                    {"public": "llama-3-8b", "upstream": "Meta-Llama-3-8B"},
                    {"public": "mistral-7b", "upstream": "Mistral-7B-Instruct"},
                ],
            },
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 200)
        svc = resp.json()["service_registry"]["services"]["tgw"]
        stored_models = svc["config"].get("models", [])
        model_groups = [m["model_group"] for m in stored_models]
        self.assertIn("llama-3-8b", model_groups)
        self.assertIn("mistral-7b", model_groups)

    def test_patch_models_invalid_type_is_400(self) -> None:
        """A non-list models value must return 400."""
        client, _ = _make_client()
        client.post(
            "/admin/api/operator/services",
            json={"name": "tgw2", "base_url": "http://h:9/v1"},
            headers=_auth(),
        )
        resp = client.patch(
            "/admin/api/operator/services/tgw2",
            json={"base_url": "http://h:9/v1", "models": "not-a-list"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "invalid_service")

    def test_patch_full_fidelity_fields_accepted(self) -> None:
        """PATCH accepts all full-fidelity fields (health_endpoint, timeout,
        resources, note, native_passthrough, protocol, role, priority) and
        they appear in the returned service_registry config."""
        client, _ = _make_client()
        client.post(
            "/admin/api/operator/services",
            json={"name": "emb", "base_url": "http://127.0.0.1:5061/v1"},
            headers=_auth(),
        )
        resp = client.patch(
            "/admin/api/operator/services/emb",
            json={
                "base_url": "http://127.0.0.1:5061/v1",
                "health_endpoint": "/health",
                "timeout": 120,
                "protocol": "openai-compatible",
                "native_passthrough": True,
                "capabilities": ["openai", "embeddings"],
                "role": "required",
                "priority": 100,
                "note": "Local embedding backend",
                "resources": {
                    "duration_class": "interactive",
                    "gpu_required": False,
                    "max_parallel": 4,
                    "vram_gb_estimate": 0,
                },
            },
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        svc = resp.json()["service_registry"]["services"]["emb"]
        cfg = svc["config"]
        res = cfg.get("resources") or {}

        self.assertEqual(cfg.get("health_endpoint"), "/health")
        self.assertEqual(cfg.get("timeout"), 120)
        self.assertTrue(cfg.get("native_passthrough"))
        self.assertEqual(cfg.get("note"), "Local embedding backend")
        self.assertEqual(res.get("duration_class"), "interactive")
        self.assertEqual(res.get("max_parallel"), 4)
        self.assertEqual(res.get("gpu_required"), False)
        self.assertEqual(svc.get("role"), "required")
        self.assertEqual(svc.get("priority"), 100)


class AdminServicePresetsTests(unittest.TestCase):
    def test_presets_endpoint_returns_list_with_custom_first(self) -> None:
        """GET /operator/service-presets returns all presets; custom is first."""
        client, _ = _make_client()
        resp = client.get("/admin/api/operator/service-presets", headers=_auth())
        self.assertEqual(resp.status_code, 200)
        presets = resp.json()["presets"]
        self.assertIsInstance(presets, list)
        self.assertGreater(len(presets), 1)
        # Custom preset must be first.
        self.assertEqual(presets[0]["id"], "openai-compatible")

    def test_presets_each_have_required_fields(self) -> None:
        """Every preset must have id, label, base_path, default_port, capabilities."""
        client, _ = _make_client()
        resp = client.get("/admin/api/operator/service-presets", headers=_auth())
        for preset in resp.json()["presets"]:
            for field in ("id", "label", "base_path", "capabilities", "native_passthrough"):
                self.assertIn(field, preset, f"preset {preset.get('id')!r} missing {field!r}")

    def test_presets_requires_admin(self) -> None:
        client, _ = _make_client()
        resp = client.get("/admin/api/operator/service-presets")
        self.assertEqual(resp.status_code, 401)

    def test_presets_response_includes_capabilities_list(self) -> None:
        """GET /operator/service-presets includes capabilities [{id, label}] for dedup."""
        client, _ = _make_client()
        resp = client.get("/admin/api/operator/service-presets", headers=_auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("capabilities", body, "response must include 'capabilities' key")
        caps = body["capabilities"]
        self.assertIsInstance(caps, list)
        self.assertGreater(len(caps), 0, "capabilities list must be non-empty")
        for cap in caps:
            self.assertIn("id", cap, f"capability entry missing 'id': {cap!r}")
            self.assertIn("label", cap, f"capability entry missing 'label': {cap!r}")
            self.assertIsInstance(cap["id"], str)
            self.assertIsInstance(cap["label"], str)
        # Spot-check a few expected values
        ids = [c["id"] for c in caps]
        for expected in ("chat", "completions", "vision", "embeddings"):
            self.assertIn(expected, ids, f"expected capability {expected!r} missing")

    def test_managed_substrate_remove_is_blocked(self) -> None:
        """DELETE on the litellm managed-substrate row returns 403."""
        store = FakeAdminStore()
        store.service_registry = service_registry.build_service_registry_seed({"backends": {}})
        client, _ = _make_client(store)
        resp = client.delete("/admin/api/operator/services/litellm", headers=_auth())
        self.assertEqual(resp.status_code, 403)
        self.assertIn("managed substrate", resp.json()["error"]["message"])

    def test_managed_substrate_host_edit_is_blocked(self) -> None:
        """PATCH with a base_url on the litellm managed-substrate row returns 403."""
        store = FakeAdminStore()
        store.service_registry = service_registry.build_service_registry_seed({"backends": {}})
        client, _ = _make_client(store)
        resp = client.patch(
            "/admin/api/operator/services/litellm",
            json={"base_url": "http://127.0.0.1:9999/v1"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("managed substrate", resp.json()["error"]["message"])

    def test_managed_substrate_rename_is_allowed(self) -> None:
        """PATCH with only name on the litellm managed-substrate row returns 200."""
        store = FakeAdminStore()
        store.service_registry = service_registry.build_service_registry_seed({"backends": {}})
        client, _ = _make_client(store)
        resp = client.patch(
            "/admin/api/operator/services/litellm",
            json={"name": "LiteLLM Custom"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 200)
        svc = resp.json()["service_registry"]["services"]["litellm"]
        self.assertEqual(svc["name"], "LiteLLM Custom")
        # Config must still be derived (execution_substrate preserved).
        self.assertEqual(svc["config"]["execution_substrate"], "litellm")

    def test_presets_capabilities_include_generations_and_videos(self) -> None:
        """Capability vocab must include 'generations' and 'videos' (H2: no hidden caps)."""
        client, _ = _make_client()
        resp = client.get("/admin/api/operator/service-presets", headers=_auth())
        self.assertEqual(resp.status_code, 200)
        caps = resp.json()["capabilities"]
        ids = [c["id"] for c in caps]
        self.assertIn("generations", ids,
                      "generations must appear in the capability vocabulary")
        self.assertIn("videos", ids,
                      "videos must appear in the capability vocabulary")
        # Both must have non-empty human labels
        cap_by_id = {c["id"]: c for c in caps}
        self.assertTrue(cap_by_id["generations"]["label"],
                        "generations must have a non-empty label")
        self.assertTrue(cap_by_id["videos"]["label"],
                        "videos must have a non-empty label")

    def test_detect_models_comfyui_protocol_returns_not_ok(self) -> None:
        """POST /operator/services/detect-models for a comfyui-protocol service.

        ComfyUI does not expose an OpenAI-compatible /v1/models endpoint, so
        detect-models returns ok=False (cannot scan workflow dir via HTTP probe).
        The endpoint must still return 200 with a structured detected object.
        This documents the gap: detect-models does not support comfyui.
        """
        import asyncio

        async def _failing_probe(host: str, port: Any, path: str) -> dict:
            # Simulate the probe reaching an endpoint that isn't /models
            return {"ok": False, "status": 404, "error": "Not Found", "body": None}

        client, _ = _make_client(service_probe=_failing_probe)
        resp = client.post(
            "/admin/api/operator/services/detect-models",
            json={
                "name": "comfyui-local",
                "host": "127.0.0.1",
                "port": 8188,
                "protocol": "comfyui",
                "base_path": "",
            },
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        detected = resp.json()["detected"]
        self.assertIn("ok", detected)
        self.assertIn("models", detected)
        # A comfyui backend has no /models endpoint — probe returns not-ok.
        self.assertFalse(detected["ok"],
                         "detect-models for comfyui must return ok=False "
                         "(no OpenAI-compatible /models endpoint)")
        self.assertEqual(detected["models"], [])


if __name__ == "__main__":
    unittest.main()
