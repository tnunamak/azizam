from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import admin_store, service_registry  # noqa: E402
from gateway.admin_api import AdminContext, create_admin_router  # noqa: E402
from gateway.admin_oidc import (  # noqa: E402
    CookieConfig,
    encode_session_cookie,
    session_from_claims,
)
from gateway.admin_runtime import (  # noqa: E402
    AdminAuthenticator,
    AdminSQLiteStore,
    EXTENSION_PACKAGE_INDEX_SETTING_KEY,
    OIDC_PROVIDER_SETTING_KEY,
    SERVICE_REGISTRY_SETTING_KEY,
    create_admin_service_account_key,
    runtime_identity_for_app_credential_hash,
    service_account_session_subject,
)
from gateway.runtime_auth import hash_api_key  # noqa: E402
from tests.registry_test_helpers import write_service_registry_for_test  # noqa: E402


def _install_registry_writer_test_helpers(store: AdminSQLiteStore, path: Path) -> None:
    """Keep legacy direct-runtime tests explicit about their current CAS token."""
    for name in ("add_service", "update_service", "remove_service", "set_service_enabled"):
        writer = getattr(store, name)

        def write(*, _writer=writer, **kwargs):
            current = admin_store.get_service_registry_with_generation(path=path)
            api_call = "expected_generation" in kwargs
            kwargs.pop("expected_generation", None)
            result = _writer(
                **kwargs,
                expected_generation=(
                    int(current["operator_settings_generation"]) if current else 0
                ),
            )
            return result if api_call else result["value"]

        setattr(store, name, write)


class AdminRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "admin.db"
        admin_store.init_db(self.db)
        self.store = AdminSQLiteStore(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _client(self, key: str) -> TestClient:
        app = FastAPI()
        app.include_router(
            create_admin_router(
                self.store,
                auth=AdminAuthenticator(self.store),
                profile_resolver=self.store.profile_for_id,
            )
        )
        client = TestClient(app)
        client.headers.update({"Authorization": f"Bearer {key}"})
        return client

    def _session_client(self, *, user_id: str, role: str = "owner") -> TestClient:
        signing_key = b"s" * 32
        cookie = CookieConfig(secure=False)
        app = FastAPI()
        app.include_router(
            create_admin_router(
                self.store,
                auth=AdminAuthenticator(
                    self.store,
                    signing_key=signing_key,
                    cookie=cookie,
                ),
                profile_resolver=self.store.profile_for_id,
            )
        )
        session = session_from_claims(
            {"sub": user_id, "email": "owner@example.com"},
            role=role,
            cookie=cookie,
        )
        client = TestClient(app)
        client.cookies.set(
            cookie.name,
            encode_session_cookie(session, signing_key=signing_key),
        )
        return client

    def _service_account_session_client(
        self,
        *,
        service_account_id: str,
        role: str = "owner",
    ) -> TestClient:
        signing_key = b"s" * 32
        cookie = CookieConfig(secure=False)
        app = FastAPI()
        app.include_router(
            create_admin_router(
                self.store,
                auth=AdminAuthenticator(
                    self.store,
                    signing_key=signing_key,
                    cookie=cookie,
                ),
                profile_resolver=self.store.profile_for_id,
            )
        )
        session = session_from_claims(
            {
                "sub": service_account_session_subject(service_account_id),
                "email": "owner-cli admin key",
            },
            role=role,
            cookie=cookie,
        )
        client = TestClient(app)
        client.cookies.set(
            cookie.name,
            encode_session_cookie(session, signing_key=signing_key),
        )
        return client

    def test_admin_key_can_create_app_and_app_key(self) -> None:
        admin_key, credential, assignment = create_admin_service_account_key(
            "owner-cli", path=self.db
        )
        self.assertTrue(admin_key.startswith("oak_"))
        self.assertNotIn("hash", credential)
        self.assertEqual(assignment["role"], "owner")

        client = self._client(admin_key)
        app_resp = client.post("/admin/api/apps", json={"name": "brother-laptop"})
        self.assertEqual(app_resp.status_code, 201, app_resp.text)
        app_id = app_resp.json()["app"]["id"]

        key_resp = client.post(
            f"/admin/api/apps/{app_id}/keys", json={"label": "Brother"}
        )
        self.assertEqual(key_resp.status_code, 201, key_resp.text)
        app_key = key_resp.json()["credential"]["api_key"]
        self.assertTrue(app_key.startswith("opk_"))

        stored = admin_store.find_credential_by_hash(
            hash_api_key(app_key), path=self.db
        )
        self.assertIsNotNone(stored)
        self.assertEqual(stored["subject_kind"], "app")  # type: ignore[index]

    def test_admin_key_can_update_profile_update_app_and_disable_app(self) -> None:
        admin_key, _, _ = create_admin_service_account_key(
            "owner-cli", path=self.db
        )
        client = self._client(admin_key)
        profile = client.post(
            "/admin/api/profiles",
            json={
                "name": "Local",
                "profile": {
                    "id": "local",
                    "protocols": ["openai.chat"],
                    "status_visibility": {"level": "basic"},
                },
            },
        ).json()["profile"]
        app_id = client.post(
            "/admin/api/apps",
            json={"name": "runtime-app", "default_profile_id": profile["id"]},
        ).json()["app"]["id"]

        profile_resp = client.patch(
            f"/admin/api/profiles/{profile['id']}",
            json={
                "name": "Local updated",
                "profile": {
                    "id": "local",
                    "protocols": ["openai.chat", "openai.images"],
                    "status_visibility": {"level": "standard"},
                },
            },
        )
        app_resp = client.patch(
            f"/admin/api/apps/{app_id}",
            json={"name": "runtime-app-updated", "default_profile_id": profile["id"]},
        )
        disabled_resp = client.delete(f"/admin/api/apps/{app_id}")

        self.assertEqual(profile_resp.status_code, 200, profile_resp.text)
        self.assertEqual(app_resp.status_code, 200, app_resp.text)
        self.assertEqual(disabled_resp.status_code, 200, disabled_resp.text)
        self.assertEqual(app_resp.json()["app"]["name"], "runtime-app-updated")
        self.assertIsNotNone(disabled_resp.json()["app"]["disabled_at"])
        actions = [event["action"] for event in admin_store.list_audit(path=self.db)]
        self.assertIn("profile.update", actions)
        self.assertIn("app.update", actions)
        self.assertIn("app.disable", actions)

    def test_admin_create_personal_app_creates_distinct_app_from_existing_one(
        self,
    ) -> None:
        admin_key, _, _ = create_admin_service_account_key(
            "owner-cli", path=self.db
        )
        user = admin_store.upsert_oidc_user("iss", "sub", path=self.db)
        existing = admin_store.ensure_personal_app_for_user(user["id"], path=self.db)

        resp = self._client(admin_key).post(
            "/admin/api/apps",
            json={
                "name": "OpenWhispr",
                "kind": "personal",
                "owner_user_id": user["id"],
            },
        )

        self.assertEqual(resp.status_code, 201, resp.text)
        created = resp.json()["app"]
        self.assertNotEqual(created["id"], existing["id"])
        self.assertEqual(created["name"], "OpenWhispr")
        self.assertEqual(created["kind"], "personal")
        self.assertEqual(created["owner_user_id"], user["id"])

        app_ids = {app["id"] for app in admin_store.list_apps(path=self.db)}
        self.assertIn(existing["id"], app_ids)
        self.assertIn(created["id"], app_ids)

        audit_event = next(
            event
            for event in admin_store.list_audit(path=self.db)
            if event["action"] == "app.create"
        )
        self.assertEqual(audit_event["target_id"], created["id"])

    def test_admin_create_service_app_unaffected_by_personal_app_fix(self) -> None:
        admin_key, _, _ = create_admin_service_account_key(
            "owner-cli", path=self.db
        )
        client = self._client(admin_key)

        first = client.post(
            "/admin/api/apps", json={"name": "svc-one"}
        )
        second = client.post(
            "/admin/api/apps", json={"name": "svc-two"}
        )

        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(second.status_code, 201, second.text)
        first_app = first.json()["app"]
        second_app = second.json()["app"]
        self.assertEqual(first_app["kind"], "service")
        self.assertEqual(second_app["kind"], "service")
        self.assertNotEqual(first_app["id"], second_app["id"])

    def test_default_local_profile_selection_inherits_builtin_default(self) -> None:
        admin_key, _, _ = create_admin_service_account_key(
            "owner-cli", path=self.db
        )
        client = self._client(admin_key)

        app_resp = client.post(
            "/admin/api/apps",
            json={"name": "runtime-app", "profile_id": "default-local"},
        )
        self.assertEqual(app_resp.status_code, 201, app_resp.text)
        app_id = app_resp.json()["app"]["id"]
        self.assertIsNone(app_resp.json()["app"]["default_profile_id"])

        profile = client.post(
            "/admin/api/profiles",
            json={
                "name": "Local",
                "profile": {
                    "id": "local",
                    "protocols": ["openai.chat"],
                },
            },
        ).json()["profile"]
        set_profile = client.patch(
            f"/admin/api/apps/{app_id}",
            json={"profile_id": profile["id"]},
        )
        self.assertEqual(set_profile.status_code, 200, set_profile.text)
        self.assertEqual(set_profile.json()["app"]["default_profile_id"], profile["id"])

        clear_profile = client.patch(
            f"/admin/api/apps/{app_id}",
            json={"profile_id": "default-local"},
        )
        self.assertEqual(clear_profile.status_code, 200, clear_profile.text)
        self.assertIsNone(clear_profile.json()["app"]["default_profile_id"])

    def test_ui_created_profile_without_inner_id_reports_its_own_identity(
        self,
    ) -> None:
        """A profile created via the admin UI never carries an inner ``id``
        (only the seeded builtins do). ``profile_for_id`` must still resolve
        it to its own outer app-profile id, not silently fall back to the
        global ``default-local`` identity — that would misattribute every
        policy decision made against it (see profile identity audit)."""
        admin_key, _, _ = create_admin_service_account_key(
            "owner-cli", path=self.db
        )
        client = self._client(admin_key)
        profile = client.post(
            "/admin/api/profiles",
            json={
                "name": "Household media profile",
                "profile": {
                    "allow": {"intents": ["chat.generate"]},
                },
            },
        ).json()["profile"]
        self.assertNotEqual(profile["id"], "default-local")

        resolved = self.store.profile_for_id(profile["id"])
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.id, profile["id"])

        simulated = client.post(
            "/admin/api/policy/simulate",
            json={
                "profile": profile["id"],
                "request": {"intent": "video.generate"},
            },
        )
        self.assertEqual(simulated.status_code, 200, simulated.text)
        body = simulated.json()
        self.assertEqual(body["profile_id"], profile["id"])
        self.assertNotEqual(body["profile_id"], "default-local")
        self.assertFalse(body["decision"]["allowed"])

    def test_disabling_app_immediately_blocks_existing_runtime_keys(self) -> None:
        admin_key, _, _ = create_admin_service_account_key(
            "owner-cli", path=self.db
        )
        client = self._client(admin_key)
        app_id = client.post(
            "/admin/api/apps", json={"name": "runtime-app"}
        ).json()["app"]["id"]
        app_key = client.post(f"/admin/api/apps/{app_id}/keys", json={}).json()[
            "credential"
        ]["api_key"]
        key_hash = hash_api_key(app_key)

        self.assertIsNotNone(
            runtime_identity_for_app_credential_hash(key_hash, path=self.db)
        )

        disabled = client.delete(f"/admin/api/apps/{app_id}")
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertIsNone(
            runtime_identity_for_app_credential_hash(key_hash, path=self.db)
        )

    def test_app_key_cannot_administer_gateway(self) -> None:
        admin_key, _, _ = create_admin_service_account_key(
            "owner-cli", path=self.db
        )
        client = self._client(admin_key)
        app_id = client.post(
            "/admin/api/apps", json={"name": "runtime-app"}
        ).json()["app"]["id"]
        app_key = client.post(f"/admin/api/apps/{app_id}/keys", json={}).json()[
            "credential"
        ]["api_key"]

        forbidden = self._client(app_key).get("/admin/api/overview")
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(forbidden.json()["error"]["code"], "admin_forbidden")

    def test_dashboard_can_create_admin_automation_key(self) -> None:
        admin_key, _, _ = create_admin_service_account_key(
            "owner-cli", path=self.db
        )
        client = self._client(admin_key)
        resp = client.post(
            "/admin/api/admin-keys",
            json={"name": "key-rotation-script", "role": "admin"},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        credential = resp.json()["credential"]
        automation_key = credential["api_key"]
        self.assertTrue(automation_key.startswith("oak_"))
        self.assertEqual(credential["subject_kind"], "service_account")
        self.assertEqual(credential["role"], "admin")
        self.assertNotIn("hash", credential)

        stored = admin_store.find_credential_by_hash(
            hash_api_key(automation_key), path=self.db
        )
        self.assertIsNotNone(stored)
        self.assertEqual(stored["subject_kind"], "service_account")  # type: ignore[index]
        self.assertIsNone(
            runtime_identity_for_app_credential_hash(
                hash_api_key(automation_key),
                path=self.db,
            )
        )

        admin_resp = self._client(automation_key).get("/admin/api/overview")
        self.assertEqual(admin_resp.status_code, 200, admin_resp.text)

    def test_cli_admin_key_helper_rejects_superadmin_role(self) -> None:
        with self.assertRaises(ValueError):
            create_admin_service_account_key(
                "too-powerful",
                role="superadmin",
                path=self.db,
            )

    def test_session_auth_uses_live_role_assignments(self) -> None:
        user = admin_store.upsert_oidc_user(
            "https://idp.example",
            "owner-subject",
            email="owner@example.com",
            display_name="Owner",
            path=self.db,
        )

        denied = self._session_client(user_id=user["id"]).get("/admin/api/overview")
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["error"]["code"], "admin_forbidden")

        admin_store.assign_admin_role(
            role="owner",
            subject_kind="user",
            subject_id=user["id"],
            granted_by="test",
            path=self.db,
        )
        allowed = self._session_client(user_id=user["id"]).get("/admin/api/overview")
        self.assertEqual(allowed.status_code, 200, allowed.text)

    def test_service_account_browser_session_uses_service_account_roles(self) -> None:
        _admin_key, credential, _assignment = create_admin_service_account_key(
            "owner-cli", path=self.db
        )
        client = self._service_account_session_client(
            service_account_id=str(credential["subject_id"])
        )
        allowed = client.get("/admin/api/overview")
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(admin_store.list_users(path=self.db), [])

    def test_admin_store_hides_legacy_local_admin_key_personal_apps(self) -> None:
        placeholder = admin_store.upsert_oidc_user(
            "local-admin-key",
            "service_account:svc_legacy",
            email="local-admin",
            display_name="Local admin key",
            path=self.db,
        )
        real_user = admin_store.upsert_oidc_user(
            "https://idp.example",
            "owner-subject",
            email="owner@example.com",
            display_name="Owner",
            path=self.db,
        )
        admin_store.ensure_personal_app_for_user(
            placeholder["id"],
            name="local-admin personal access",
            path=self.db,
        )
        admin_store.ensure_personal_app_for_user(
            real_user["id"],
            name="owner personal access",
            path=self.db,
        )
        service_account = admin_store.ensure_service_account(
            "shared-service", path=self.db
        )
        admin_store.create_app(
            "shared service",
            kind="service",
            owner_service_account_id=service_account["id"],
            path=self.db,
        )

        raw_names = {app["name"] for app in admin_store.list_apps(path=self.db)}
        self.assertIn("local-admin personal access", raw_names)
        visible_names = {app["name"] for app in self.store.list_apps()}
        self.assertNotIn("local-admin personal access", visible_names)
        self.assertIn("owner personal access", visible_names)
        self.assertIn("shared service", visible_names)

    def test_admin_store_app_key_builds_runtime_identity_with_profile(self) -> None:
        admin_key, _, _ = create_admin_service_account_key(
            "owner-cli", path=self.db
        )
        client = self._client(admin_key)
        profile = client.post(
            "/admin/api/profiles",
            json={
                "name": "brother-chat-only",
                "profile": {
                    "id": "brother-chat-only",
                    "protocols": ["openai.chat"],
                    "fallback": {"cloud_allowed": False},
                    "status_visibility": {"level": "basic"},
                },
            },
        ).json()["profile"]
        app_id = client.post(
            "/admin/api/apps",
            json={
                "name": "brother-personal",
                "profile_id": profile["id"],
            },
        ).json()["app"]["id"]
        app_key = client.post(f"/admin/api/apps/{app_id}/keys", json={}).json()[
            "credential"
        ]["api_key"]
        stored = admin_store.find_credential_by_hash(
            hash_api_key(app_key),
            path=self.db,
        )
        self.assertIsNotNone(stored)
        # Dashboard/default key minting inherits the App profile at runtime;
        # it does not freeze a copy onto the credential.
        self.assertIsNone(stored["profile_id"])  # type: ignore[index]

        identity = runtime_identity_for_app_credential_hash(
            hash_api_key(app_key),
            path=self.db,
        )
        self.assertIsNotNone(identity)
        self.assertEqual(identity.name, "brother-personal")  # type: ignore[union-attr]
        self.assertEqual(identity.profile.id, "brother-chat-only")  # type: ignore[union-attr]
        self.assertEqual(
            identity.profile.protocols,  # type: ignore[union-attr]
            frozenset({"openai.chat"}),
        )

        self.assertIsNone(
            runtime_identity_for_app_credential_hash(
                hash_api_key(admin_key),
                path=self.db,
            )
        )

    def test_invalid_personal_app_owner_returns_400(self) -> None:
        admin_key, _, _ = create_admin_service_account_key(
            "owner-cli", path=self.db
        )
        resp = self._client(admin_key).post(
            "/admin/api/apps",
            json={
                "name": "unknown-personal",
                "kind": "personal",
                "owner_user_id": "user_missing",
            },
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()["error"]["code"], "invalid_app")

    def test_invalid_key_profile_returns_400(self) -> None:
        admin_key, _, _ = create_admin_service_account_key(
            "owner-cli", path=self.db
        )
        client = self._client(admin_key)
        app_id = client.post(
            "/admin/api/apps", json={"name": "runtime-app"}
        ).json()["app"]["id"]
        resp = client.post(
            f"/admin/api/apps/{app_id}/keys",
            json={"profile_id": "prof_missing"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()["error"]["code"], "invalid_credential")

    def test_dashboard_snapshot_does_not_expose_hashes(self) -> None:
        admin_key, _, _ = create_admin_service_account_key(
            "owner-cli", path=self.db
        )
        client = self._client(admin_key)
        app_id = client.post(
            "/admin/api/apps", json={"name": "runtime-app"}
        ).json()["app"]["id"]
        client.post(f"/admin/api/apps/{app_id}/keys", json={})
        client.post(
            "/admin/api/runtime-profile-mappings",
            json={
                "source_kind": "domain",
                "source_value": "example.com",
                "profile_id": "default-local",
            },
        )

        snapshot = self.store.dashboard_snapshot(
            AdminContext(
                subject="svc_test",
                roles=frozenset({"owner"}),
                csrf_token="csrf-token",
            )
        )
        self.assertNotIn("hash", repr(snapshot))
        self.assertEqual(snapshot["actor"]["csrf_token"], "csrf-token")
        self.assertEqual(snapshot["counts"]["runtime_profile_mappings"], 1)
        self.assertEqual(
            snapshot["runtime_profile_mappings"][0]["source_kind"], "domain"
        )
        self.assertEqual(
            snapshot["access_model"]["nouns"],
            ["user", "app", "profile", "key", "runtime_mapping", "admin_role"],
        )
        self.assertEqual(
            snapshot["access_model"]["runtime_mappings"]["source_kinds"],
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
            "separate",
            snapshot["access_model"]["admin_roles"]["relationship"],
        )

    def test_dashboard_links_are_admin_or_public_routes(self) -> None:
        snapshot = self.store.dashboard_snapshot(
            AdminContext(subject="svc_test", roles=frozenset({"owner"}))
        )
        hrefs = {link["href"] for link in snapshot["discovery_links"]}
        self.assertIn("/", hrefs)
        self.assertIn("/.well-known/ai-gateway", hrefs)
        self.assertIn("/admin/api/overview", hrefs)
        self.assertNotIn("/gateway/v1/capabilities", hrefs)
        self.assertTrue(
            all(
                h == "/"
                or h.startswith("/admin/")
                or h.startswith("/.well-known/")
                for h in hrefs
            )
        )

    def test_operator_overrides_persist_and_are_audited(self) -> None:
        admin_key, _, _ = create_admin_service_account_key(
            "owner-cli", path=self.db
        )
        client = self._client(admin_key)

        access_resp = client.put(
            "/admin/api/operator/user-access-defaults",
            json={"self_service_apps": False},
        )
        self.assertEqual(access_resp.status_code, 200, access_resp.text)
        reopened = AdminSQLiteStore(self.db)
        overrides = reopened.operator_overrides()
        self.assertFalse(overrides["user_access_defaults"]["self_service_apps"])

        resp = client.put(
            "/admin/api/operator/service-availability",
            json={
                "openai": {
                    "state": "maintenance",
                    "reason": "planned work",
                    "retry_after_seconds": 300,
                }
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        overrides = reopened.operator_overrides()
        self.assertEqual(
            overrides["service_availability"]["openai"]["state"],
            "maintenance",
        )
        media_resp = client.put(
            "/admin/api/operator/media-job-admission",
            json={
                "reserved_tokens": ["gpu"],
                "disabled_intents": ["video.generate"],
            },
        )
        self.assertEqual(media_resp.status_code, 200, media_resp.text)
        overrides = reopened.operator_overrides()
        self.assertEqual(
            overrides["media_job_admission"]["reserved_tokens"],
            ["gpu"],
        )
        actions = [event["action"] for event in admin_store.list_audit(path=self.db)]
        self.assertIn("operator.user_access_defaults.update", actions)
        self.assertIn("operator.service_availability.update", actions)
        self.assertIn("operator.media_job_admission.update", actions)

    def test_service_registry_persists_and_is_audited(self) -> None:
        self.assertEqual(
            self.store.get_service_registry(),
            {
                "schema_version": 1,
                "services": {},
                "protocol_frontends": {},
                "workflows": {},
                "capabilities": {},
            },
        )

        admin_key, _, _ = create_admin_service_account_key(
            "owner-cli", path=self.db
        )
        client = self._client(admin_key)
        resp = client.put(
            "/admin/api/operator/service-registry",
            json={
                "services": {
                    "Local Comfy": {
                        "name": "Local ComfyUI",
                        "source": "ui",
                        "enabled": True,
                        "capabilities": ["image.generate"],
                        "config": {"base_url": "http://127.0.0.1:7890"},
                    }
                },
                "protocol_frontends": {
                    "Gateway Native": {
                        "name": "Gateway-native",
                        "protocol": "gateway",
                        "source": "builtin",
                        "routes": ["/gateway/v1"],
                        "intents": ["jobs.create"],
                    }
                },
            },
            headers={"If-Match": '"os-0"'},
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        registry = resp.json()["service_registry"]
        self.assertEqual(sorted(registry["services"]), ["local-comfy"])
        self.assertEqual(
            registry["services"]["local-comfy"]["config"]["base_url"],
            "http://127.0.0.1:7890",
        )

        reopened = AdminSQLiteStore(self.db)
        self.assertEqual(reopened.get_service_registry(), registry)
        stored = admin_store.get_operator_setting(
            SERVICE_REGISTRY_SETTING_KEY,
            path=self.db,
        )
        self.assertIsNotNone(stored)
        self.assertEqual(stored["value"], registry)  # type: ignore[index]
        actions = [event["action"] for event in admin_store.list_audit(path=self.db)]
        self.assertIn("operator.service_registry.update", actions)

    def test_extension_package_enable_persists_registry_index_and_audit(self) -> None:
        self.assertEqual(
            self.store.get_extension_package_index(),
            {"schema_version": 1, "sources": [], "packages": {}, "updated_at": 0},
        )
        package_dir = Path(self.tmp.name) / "package"
        (package_dir / "objects").mkdir(parents=True)
        (package_dir / "extension.yml").write_text(
            """
schema_version: 1
kind: extension_package
id: runtime-package
version: 0.1.0
objects:
  services:
    - objects/service.yml
  workflows:
    - objects/workflow.yml
  capabilities:
    - objects/capability.yml
""",
            encoding="utf-8",
        )
        (package_dir / "objects" / "service.yml").write_text(
            """
id: runtime-service
name: Runtime Service
config:
  base_url: http://127.0.0.1:9999
""",
            encoding="utf-8",
        )
        (package_dir / "objects" / "workflow.yml").write_text(
            """
schema_version: 1
id: image.generate.runtime
intent: image.generate
inputs:
  prompt:
    type: string
outputs:
  image:
    media_type: image/png
bindings:
  comfyui:
    workflow: workflows/runtime.json
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
id: runtime-capability
intent: image.generate
backend: comfyui
workflow: image.generate.runtime
input_modalities: [text]
output_modalities: [image]
execution: job
""",
            encoding="utf-8",
        )
        admin_key, _, _ = create_admin_service_account_key(
            "owner-cli", path=self.db
        )
        client = self._client(admin_key)
        source = {"type": "local_directory", "path": str(package_dir)}

        validate_resp = client.post(
            "/admin/api/extensions/validate",
            json={"source": source},
        )
        enable_resp = client.post(
            "/admin/api/extensions/enable",
            json={"source": source},
            headers={"If-Match": '"os-0"'},
        )

        self.assertEqual(validate_resp.status_code, 200, validate_resp.text)
        self.assertTrue(validate_resp.json()["review"]["ok"])
        self.assertEqual(enable_resp.status_code, 200, enable_resp.text)
        body = enable_resp.json()
        self.assertEqual(body["extension_package"]["id"], "runtime-package")
        self.assertIn("runtime-service", body["service_registry"]["services"])
        self.assertIn("image.generate.runtime", body["service_registry"]["workflows"])
        self.assertIn("runtime-capability", body["service_registry"]["capabilities"])

        reopened = AdminSQLiteStore(self.db)
        registry = reopened.get_service_registry()
        package_index = reopened.get_extension_package_index()
        self.assertIn("runtime-service", registry["services"])
        self.assertIn("image.generate.runtime", registry["workflows"])
        self.assertIn("runtime-capability", registry["capabilities"])
        self.assertIn("runtime-package", package_index["packages"])
        stored = admin_store.get_operator_setting(
            EXTENSION_PACKAGE_INDEX_SETTING_KEY,
            path=self.db,
        )
        self.assertIsNotNone(stored)
        self.assertEqual(stored["value"], package_index)  # type: ignore[index]
        actions = [event["action"] for event in admin_store.list_audit(path=self.db)]
        self.assertIn("operator.extension_package.enable", actions)

    def test_extension_package_index_setting_key_is_stable(self) -> None:
        self.assertEqual(
            EXTENSION_PACKAGE_INDEX_SETTING_KEY,
            "extension_package_index",
        )


class AdminOidcSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "admin.db"
        admin_store.init_db(self.db)
        self.store = AdminSQLiteStore(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _client(self, key: str) -> TestClient:
        app = FastAPI()
        app.include_router(
            create_admin_router(
                self.store,
                auth=AdminAuthenticator(self.store),
                profile_resolver=self.store.profile_for_id,
            )
        )
        client = TestClient(app)
        client.headers.update({"Authorization": f"Bearer {key}"})
        return client

    def test_get_returns_none_when_no_setting(self) -> None:
        self.assertIsNone(self.store.get_oidc_config_setting())

    def test_put_redacts_secret_and_preserves_on_blank(self) -> None:
        admin_key, _, _ = create_admin_service_account_key(
            "owner-cli", path=self.db
        )
        client = self._client(admin_key)

        resp = client.put(
            "/admin/api/oidc-config",
            json={
                "enabled": True,
                "issuer": "https://idp.example.com",
                "client_id": "client-A",
                "client_secret": "topsecret-A",
                "redirect_uri": "https://gw.example.com/admin/oidc/callback",
                "scopes": ["openid", "email", "profile"],
                "admin_emails": ["owner@example.com"],
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()["oidc_config"]
        self.assertEqual(body["issuer"], "https://idp.example.com")
        self.assertEqual(body["client_id"], "client-A")
        self.assertEqual(body["client_secret"], "<redacted>")
        self.assertTrue(body["client_secret_set"])
        self.assertNotIn("topsecret-A", resp.text)

        # Raw secret is preserved internally.
        raw = self.store.get_oidc_config_setting_raw()
        self.assertEqual(raw["client_secret"], "topsecret-A")

        # Update with blank/omitted secret must NOT clobber it.
        resp2 = client.put(
            "/admin/api/oidc-config",
            json={"client_secret": "", "client_id": "client-B"},
        )
        self.assertEqual(resp2.status_code, 200, resp2.text)
        raw = self.store.get_oidc_config_setting_raw()
        self.assertEqual(raw["client_secret"], "topsecret-A")
        self.assertEqual(raw["client_id"], "client-B")

        # Sending the redacted sentinel also preserves the existing secret.
        resp3 = client.put(
            "/admin/api/oidc-config",
            json={"client_secret": "<redacted>"},
        )
        self.assertEqual(resp3.status_code, 200, resp3.text)
        raw = self.store.get_oidc_config_setting_raw()
        self.assertEqual(raw["client_secret"], "topsecret-A")

        # Replacing with a real value works.
        resp4 = client.put(
            "/admin/api/oidc-config",
            json={"client_secret": "rotated-B"},
        )
        self.assertEqual(resp4.status_code, 200, resp4.text)
        raw = self.store.get_oidc_config_setting_raw()
        self.assertEqual(raw["client_secret"], "rotated-B")

    def test_put_list_clear_is_stored_and_omission_preserves(self) -> None:
        admin_key, _, _ = create_admin_service_account_key("owner-cli", path=self.db)
        client = self._client(admin_key)
        initial = {
            "enabled": False,
            "scopes": ["openid", "email"],
            "allowed_email_domains": ["example.com"],
            "admin_emails": ["owner@example.com"],
            "admin_groups": ["owners"],
            "admin_roles": ["operator"],
        }
        self.assertEqual(client.put("/admin/api/oidc-config", json=initial).status_code, 200)
        clear = {
            "scopes": [], "allowed_email_domains": [], "admin_emails": [],
            "admin_groups": [], "admin_roles": [], "issuer": "",
        }
        response = client.put("/admin/api/oidc-config", json=clear)
        self.assertEqual(response.status_code, 200, response.text)
        stored = self.store.get_oidc_config_setting_raw()
        self.assertIsNotNone(stored)
        for field in ("scopes", "allowed_email_domains", "admin_emails", "admin_groups", "admin_roles"):
            self.assertEqual(stored[field], [])  # type: ignore[index]
        self.assertEqual(stored["issuer"], "")  # type: ignore[index]
        self.assertEqual(client.put("/admin/api/oidc-config", json={"client_id": "changed"}).status_code, 200)
        preserved = self.store.get_oidc_config_setting_raw()
        self.assertEqual(preserved["admin_emails"], [])  # type: ignore[index]

    def test_put_null_clears_scalar_fields_and_preserves_secret(self) -> None:
        admin_key, _, _ = create_admin_service_account_key("owner-cli", path=self.db)
        client = self._client(admin_key)
        scalar_fields = (
            "issuer", "client_id", "redirect_uri", "authorization_endpoint",
            "token_endpoint", "jwks_uri", "groups_claim",
        )
        initial = {field: f"initial-{field}" for field in scalar_fields}
        initial["client_secret"] = "secret-A"
        self.assertEqual(client.put("/admin/api/oidc-config", json=initial).status_code, 200)
        response = client.put(
            "/admin/api/oidc-config",
            json={**{field: None for field in scalar_fields}, "client_secret": None},
        )
        self.assertEqual(response.status_code, 200, response.text)
        stored = self.store.get_oidc_config_setting_raw()
        self.assertIsNotNone(stored)
        for field in scalar_fields:
            self.assertEqual(stored[field], "")  # type: ignore[index]
            self.assertEqual(response.json()["oidc_config"][field], "")
        self.assertEqual(stored["client_secret"], "secret-A")  # type: ignore[index]
        self.assertEqual(response.json()["oidc_config"]["client_secret"], "<redacted>")

    def test_oidc_setting_and_audit_rollback_together(self) -> None:
        actor = AdminContext(
            subject="durable-service-id", actor_id="durable-service-id",
            actor_kind="service_account", actor_label="Release bot",
            roles=frozenset({"owner"}),
        )
        before = admin_store.get_operator_setting(OIDC_PROVIDER_SETTING_KEY, path=self.db)
        with patch.object(
            admin_store, "_record_audit_in_transaction",
            side_effect=RuntimeError("injected audit failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected audit failure"):
                self.store.update_oidc_config_setting(data={"enabled": False}, actor=actor)
        self.assertEqual(admin_store.get_operator_setting(OIDC_PROVIDER_SETTING_KEY, path=self.db), before)
        self.assertEqual(admin_store.list_audit(path=self.db), [])

    def test_get_endpoint_redacts_secret(self) -> None:
        admin_key, _, _ = create_admin_service_account_key(
            "owner-cli", path=self.db
        )
        client = self._client(admin_key)
        client.put(
            "/admin/api/oidc-config",
            json={
                "enabled": True,
                "issuer": "https://idp.example.com",
                "client_id": "c",
                "client_secret": "sekret",
                "redirect_uri": "https://gw.example.com/admin/oidc/callback",
            },
        )
        resp = client.get("/admin/api/oidc-config")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()["oidc_config"]
        self.assertEqual(body["client_secret"], "<redacted>")
        self.assertNotIn("sekret", resp.text)

    def test_put_rejects_unknown_fields(self) -> None:
        admin_key, _, _ = create_admin_service_account_key(
            "owner-cli", path=self.db
        )
        client = self._client(admin_key)
        resp = client.put(
            "/admin/api/oidc-config",
            json={"unknown_field": "x"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()["error"]["code"], "invalid_oidc_config")

    def test_put_rejects_enabled_without_required_fields(self) -> None:
        admin_key, _, _ = create_admin_service_account_key(
            "owner-cli", path=self.db
        )
        client = self._client(admin_key)
        resp = client.put(
            "/admin/api/oidc-config",
            json={"enabled": True, "issuer": "", "client_id": "c", "redirect_uri": "u"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)

        resp2 = client.put(
            "/admin/api/oidc-config",
            json={"enabled": True},
        )
        self.assertEqual(resp2.status_code, 400, resp2.text)
        self.assertIn("issuer is required", resp2.text)

    def test_audit_records_update(self) -> None:
        admin_key, _, _ = create_admin_service_account_key(
            "owner-cli", path=self.db
        )
        client = self._client(admin_key)
        client.put(
            "/admin/api/oidc-config",
            json={"client_id": "x"},
        )
        actions = [event["action"] for event in admin_store.list_audit(path=self.db)]
        self.assertIn("operator.oidc_provider_config.update", actions)

    def test_setting_key_is_stable(self) -> None:
        # External callers (config_sources, proxy.py) depend on this key.
        self.assertEqual(OIDC_PROVIDER_SETTING_KEY, "oidc_provider_config")

    def test_get_effective_oidc_contract_covers_bootstrap_override_and_merge(self) -> None:
        admin_key, _, _ = create_admin_service_account_key("owner-cli", path=self.db)
        cases = (
            ("bootstrap", {"issuer": "bootstrap", "client_id": "bootstrap-client"}),
            ("database", {"issuer": "database", "client_id": "database-client"}),
            ("merged", {"issuer": "bootstrap", "client_id": "database-client"}),
        )
        for summary, expected in cases:
            with self.subTest(summary=summary):
                app = FastAPI()
                app.include_router(
                    create_admin_router(
                        self.store,
                        auth=AdminAuthenticator(self.store),
                        effective_oidc_config_provider=lambda summary=summary, expected=expected: {
                            **expected,
                            "enabled": True,
                            "client_secret": "<redacted>",
                            "client_secret_set": True,
                            "source": summary,
                            "apply_mode": "live",
                            "provenance": {
                                "summary": summary,
                                "fields": {
                                    "issuer": "bootstrap" if expected["issuer"] == "bootstrap" else "database",
                                    "client_id": "bootstrap" if expected["client_id"] == "bootstrap-client" else "database",
                                },
                            },
                        },
                    )
                )
                response = TestClient(app).get(
                    "/admin/api/oidc-config",
                    headers={"Authorization": f"Bearer {admin_key}"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                config = response.json()["oidc_config"]
                self.assertEqual(config["issuer"], expected["issuer"])
                self.assertEqual(config["client_id"], expected["client_id"])
                self.assertEqual(config["provenance"]["summary"], summary)
                self.assertEqual(config["source"], summary)
                self.assertEqual(config["apply_mode"], "live")
                self.assertNotIn("topsecret", response.text)

    def test_audit_projects_human_service_and_system_actors_without_id_inference(self) -> None:
        human = admin_store.upsert_oidc_user(
            "https://idp.example", "owner", email="owner@example.com",
            display_name="Owner Human", path=self.db,
        )
        service = admin_store.ensure_service_account("release-bot", path=self.db)
        self.store.create_app(
            data={"name": "human-app"},
            actor=AdminContext(subject=human["id"], actor_kind="user", roles=frozenset({"owner"})),
        )
        self.store.create_app(
            data={"name": "service-app"},
            actor=AdminContext(subject=service["id"], actor_kind="service_account", roles=frozenset({"owner"})),
        )
        admin_store.record_audit(
            "system.seed", actor_kind="system", actor_id="startup", path=self.db,
        )
        admin_key, _, _ = create_admin_service_account_key("reader", path=self.db)
        records = self._client(admin_key).get("/admin/api/audit").json()["records"]
        service_record = next(
            record for record in records
            if record["action"] == "app.create" and record["actor_kind"] == "service_account"
        )
        self.assertEqual(service_record["actor_label"], "release-bot")
        human_record = next(
            record for record in records
            if record["action"] == "app.create" and record["actor_kind"] == "user"
        )
        self.assertEqual(human_record["actor_label"], "Owner Human")
        system_record = next(record for record in records if record["action"] == "system.seed")
        self.assertEqual(system_record["actor_kind"], "system")
        self.assertEqual(system_record["actor_label"], "System")

    def test_new_service_account_audit_uses_event_label_not_id_shape(self) -> None:
        actor = AdminContext(
            subject="opaque-automation-id", actor_id="opaque-automation-id",
            actor_kind="service_account", actor_label="Release bot",
            roles=frozenset({"owner"}),
        )
        self.store.create_app(data={"name": "opaque-actor-app"}, actor=actor)
        event = next(event for event in admin_store.list_audit(path=self.db)
                     if event["action"] == "app.create")
        self.assertEqual(event["actor_id"], "opaque-automation-id")
        self.assertEqual(event["actor_label"], "Release bot")


class ServicePatchRoundTripTests(unittest.TestCase):
    """PATCH /operator/services/{id} via AdminSQLiteStore.update_service.

    These tests go through the real AdminSQLiteStore (not the FakeAdminStore)
    to prove that a partial PATCH preserves all untouched fields — the merge
    logic lives in AdminSQLiteStore.update_service, not in admin_api.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "admin.db"
        admin_store.init_db(self.db)
        self.store = AdminSQLiteStore(self.db)
        self.ctx = AdminContext(subject="owner", roles=frozenset({"owner"}))
        _install_registry_writer_test_helpers(self.store, self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_new_model_mappings_use_current_policy_schema(self) -> None:
        registry = self.store.add_service(
            data={"name": "first-chat", "preset": "openai-compatible",
                  "base_url": "http://engine:18001/v1",
                  "models": [{"public": "chat", "upstream": "engine-model"}]},
            actor=self.ctx,
        )
        config = registry["services"]["first-chat"]["config"]
        self.assertEqual(config["model_policy_schema_version"], service_registry.MODEL_POLICY_SCHEMA_VERSION)
        self.assertEqual(config["models"], [{"model_group": "chat", "upstream_model": "engine-model"}])

    def test_add_preserves_explicit_model_policy_schema(self) -> None:
        registry = self.store.add_service(
            data={"name": "explicit-schema", "base_url": "http://engine:18001/v1",
                  "models": [{"public": "chat", "upstream": "engine-model"}],
                  "model_policy_schema_version": 0},
            actor=self.ctx,
        )
        self.assertEqual(registry["services"]["explicit-schema"]["config"]["model_policy_schema_version"], 0)

    def test_existing_unversioned_models_are_not_implicitly_migrated(self) -> None:
        seed = service_registry.build_service_registry_seed({"backends": {
            "legacy": {"protocol": "openai-compatible", "host": "engine", "port": 18001,
                       "models": [{"model_group": "chat", "upstream_model": "engine-model"}]},
        }})
        write_service_registry_for_test(seed, path=self.db)
        form = {"name": "legacy", "base_url": "http://engine:18001/v1",
                "models": [{"public": "chat", "upstream": "engine-model"}]}
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.store.add_service(data=form, actor=self.ctx)
        registry = self.store.get_service_registry()
        self.assertNotIn("model_policy_schema_version", registry["services"]["legacy"]["config"])
        registry = self.store.update_service(service_id="legacy", data={"note": "edit"}, actor=self.ctx)
        self.assertNotIn("model_policy_schema_version", registry["services"]["legacy"]["config"])

    def _seed_embeddings(self) -> None:
        """Add the canonical 'embeddings' service from the spec example."""
        self.store.add_service(
            data={
                "name": "embeddings",
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
                "models": [{"public": "nomic-embed-text-v1.5", "upstream": "nomic-embed-text-v1.5"}],
            },
            actor=self.ctx,
        )

    def test_managed_note_round_trips_through_patch_api_and_registry(self) -> None:
        """The API stores managed notes at config.note without dropping config."""
        seed = service_registry.build_service_registry_seed({"backends": {}})
        original_config = dict(seed["services"]["litellm"]["config"])
        write_service_registry_for_test(seed, path=self.db)
        admin_key, _, _ = create_admin_service_account_key("owner-cli", path=self.db)
        app = FastAPI()
        app.include_router(
            create_admin_router(
                self.store,
                auth=AdminAuthenticator(self.store),
                profile_resolver=self.store.profile_for_id,
            )
        )
        client = TestClient(app)
        client.headers.update({
            "Authorization": f"Bearer {admin_key}",
            "x-ai-gateway-admin-action": "1",
        })
        before = client.get("/admin/api/operator/service-registry")
        self.assertEqual(before.status_code, 200, before.text)
        response = client.patch(
            "/admin/api/operator/services/litellm",
            json={"note": "Operator-owned note"},
            headers={"If-Match": before.headers["ETag"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        service = response.json()["service_registry"]["services"]["litellm"]
        self.assertEqual(service["config"], {**original_config, "note": "Operator-owned note"})
        self.assertNotIn("note", service)
        persisted = self.store.get_service_registry()["services"]["litellm"]
        self.assertEqual(persisted["config"], service["config"])

    def test_partial_patch_preserves_health_endpoint_and_timeout(self) -> None:
        """PATCH that only changes name must not lose health_endpoint or timeout."""
        self._seed_embeddings()
        registry = self.store.update_service(
            service_id="embeddings",
            data={"name": "embeddings-renamed"},
            actor=self.ctx,
        )
        svc = registry["services"]["embeddings"]
        cfg = svc["config"]
        self.assertEqual(svc["name"], "embeddings-renamed")
        self.assertEqual(cfg.get("health_endpoint"), "/health",
                         "health_endpoint must survive a name-only PATCH")
        self.assertEqual(cfg.get("timeout"), 120,
                         "timeout must survive a name-only PATCH")

    def test_partial_patch_preserves_resources(self) -> None:
        """PATCH that only changes note must not lose resources sub-keys."""
        self._seed_embeddings()
        registry = self.store.update_service(
            service_id="embeddings",
            data={"note": "updated note"},
            actor=self.ctx,
        )
        svc = registry["services"]["embeddings"]
        cfg = svc["config"]
        res = cfg.get("resources") or {}
        self.assertEqual(res.get("max_parallel"), 4,
                         "max_parallel must survive a note-only PATCH")
        self.assertEqual(res.get("duration_class"), "interactive",
                         "duration_class must survive a note-only PATCH")
        self.assertEqual(res.get("gpu_required"), False,
                         "gpu_required must survive a note-only PATCH")
        self.assertEqual(cfg.get("note"), "updated note")

    def test_partial_patch_preserves_api_key_env(self) -> None:
        """api_key_env (a _ref key stored in config) survives a partial PATCH."""
        self.store.add_service(
            data={
                "name": "keyed-svc",
                "base_url": "http://h:9000/v1",
                "api_key_env": "MY_BACKEND_KEY",
            },
            actor=self.ctx,
        )
        registry = self.store.update_service(
            service_id="keyed-svc",
            data={"timeout": 30},
            actor=self.ctx,
        )
        svc = registry["services"]["keyed-svc"]
        cfg = svc["config"]
        self.assertEqual(cfg.get("api_key_env"), "MY_BACKEND_KEY",
                         "api_key_env must survive a timeout-only PATCH")
        self.assertEqual(cfg.get("timeout"), 30)

    def test_full_patch_round_trip_preserves_all_spec_fields(self) -> None:
        """The full 'embeddings' spec record survives a complete PATCH overwrite.

        Sends every field (same values) and verifies they all come back intact,
        proving there are no unmodeled fields that get silently dropped.
        """
        self._seed_embeddings()
        full_patch = {
            "name": "embeddings",
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
            "models": [{"public": "nomic-embed-text-v1.5", "upstream": "nomic-embed-text-v1.5"}],
        }
        registry = self.store.update_service(
            service_id="embeddings",
            data=full_patch,
            actor=self.ctx,
        )
        svc = registry["services"]["embeddings"]
        cfg = svc["config"]
        res = cfg.get("resources") or {}

        self.assertEqual(svc["name"], "embeddings")
        self.assertEqual(svc["role"], "required")
        self.assertEqual(svc["priority"], 100)
        self.assertEqual(sorted(svc["capabilities"]), sorted(["openai", "embeddings"]))
        self.assertEqual(cfg.get("health_endpoint"), "/health")
        self.assertEqual(cfg.get("timeout"), 120)
        self.assertTrue(cfg.get("native_passthrough"))
        self.assertEqual(cfg.get("note"), "Local embedding backend")
        self.assertEqual(res.get("duration_class"), "interactive")
        self.assertEqual(res.get("gpu_required"), False)
        self.assertEqual(res.get("max_parallel"), 4)
        self.assertEqual(res.get("vram_gb_estimate"), 0)
        stored_models = cfg.get("models") or []
        model_groups = [m["model_group"] for m in stored_models]
        self.assertIn("nomic-embed-text-v1.5", model_groups)

    def test_patch_timeout_only_updates_timeout(self) -> None:
        """PATCH with only timeout=60 must update timeout and leave other fields."""
        self._seed_embeddings()
        registry = self.store.update_service(
            service_id="embeddings",
            data={"timeout": 60},
            actor=self.ctx,
        )
        svc = registry["services"]["embeddings"]
        cfg = svc["config"]
        self.assertEqual(cfg.get("timeout"), 60)
        # Other fields must be intact
        self.assertEqual(cfg.get("health_endpoint"), "/health")
        self.assertEqual(cfg.get("native_passthrough"), True)
        self.assertEqual(svc["role"], "required")

    def _seed_comfyui(self) -> None:
        """Add a ComfyUI service with all protocol-specific fields."""
        self.store.add_service(
            data={
                "name": "ComfyUI Local",
                "host": "127.0.0.1",
                "port": 8188,
                "protocol": "comfyui",
                "capabilities": ["image", "generations", "videos"],
                "workflows_dir": "/data/comfyui/workflows",
                "video_workflows_dir": "/data/comfyui/video_workflows",
                "video_timeout": 300,
                "default_model": "flux_schnell",
            },
            actor=self.ctx,
        )

    def test_comfyui_name_only_patch_preserves_all_protocol_fields(self) -> None:
        """H1 regression: a name-only PATCH on a ComfyUI service must not drop
        workflows_dir, video_workflows_dir, video_timeout, or default_model.

        This is the exact bug-of-record: saving the generic edit form dropped
        these four fields because update_service used a hard-coded whitelist.
        """
        self._seed_comfyui()
        registry = self.store.update_service(
            service_id="comfyui-local",
            data={"name": "ComfyUI (renamed)"},
            actor=self.ctx,
        )
        svc = registry["services"]["comfyui-local"]
        cfg = svc["config"]
        self.assertEqual(svc["name"], "ComfyUI (renamed)", "name must update")
        self.assertEqual(
            cfg.get("workflows_dir"), "/data/comfyui/workflows",
            "workflows_dir must survive a name-only PATCH",
        )
        self.assertEqual(
            cfg.get("video_workflows_dir"), "/data/comfyui/video_workflows",
            "video_workflows_dir must survive a name-only PATCH",
        )
        self.assertEqual(
            cfg.get("video_timeout"), 300,
            "video_timeout must survive a name-only PATCH",
        )
        self.assertEqual(
            cfg.get("default_model"), "flux_schnell",
            "default_model must survive a name-only PATCH",
        )
        self.assertIn("generations", svc["capabilities"],
                      "generations capability must survive a name-only PATCH")
        self.assertIn("videos", svc["capabilities"],
                      "videos capability must survive a name-only PATCH")

    def test_comfyui_protocol_fields_round_trip_via_patch(self) -> None:
        """Protocol fields can be updated via PATCH and persist correctly."""
        self._seed_comfyui()
        registry = self.store.update_service(
            service_id="comfyui-local",
            data={
                "workflows_dir": "/new/workflows",
                "video_timeout": 600,
                "default_model": "wan_video",
            },
            actor=self.ctx,
        )
        svc = registry["services"]["comfyui-local"]
        cfg = svc["config"]
        self.assertEqual(cfg.get("workflows_dir"), "/new/workflows")
        self.assertEqual(cfg.get("video_timeout"), 600)
        self.assertEqual(cfg.get("default_model"), "wan_video")
        # video_workflows_dir was not in the PATCH — must still be intact
        self.assertEqual(
            cfg.get("video_workflows_dir"), "/data/comfyui/video_workflows",
            "video_workflows_dir must survive a partial PATCH that doesn't mention it",
        )

    def test_unknown_config_key_survives_partial_patch(self) -> None:
        """Generic regression: a config key not modeled by the form must survive.

        Seeds a service with an unknown_future_field in its config (simulating a
        future extension), then patches only the name.  The unknown key must not
        be dropped — this guards against any future whitelist regression.
        """
        # Add a service via the raw registry API so we can plant an unknown key.
        reg = self.store.get_service_registry()
        from gateway import service_registry as sr
        existing_config = {
            "host": "localhost",
            "port": 9000,
            "base_path": "/v1",
            "protocol": "openai-compatible",
            "unknown_future_field": "must-survive",
        }
        raw = {**existing_config, "name": "future-svc", "capabilities": ["chat"]}
        registry, _ = sr.upsert_service_in_registry(
            reg, service_id="future-svc", raw=raw, source="ui"
        )
        import gateway.admin_store as _admin_store
        write_service_registry_for_test(
            registry,
            updated_by="test",
            path=self.db,
        )

        # Name-only PATCH
        registry2 = self.store.update_service(
            service_id="future-svc",
            data={"name": "future-svc-renamed"},
            actor=self.ctx,
        )
        cfg = registry2["services"]["future-svc"]["config"]
        self.assertEqual(
            cfg.get("unknown_future_field"), "must-survive",
            "An unknown config key must survive a partial PATCH",
        )


class ServiceModelRoutabilityTests(unittest.TestCase):
    """End-to-end: operator create/edit service+model → routable LiteLLM config.

    These guard the seam where the gateway hands a service's model mapping to the
    LiteLLM subprocess.  start.sh runs a watcher that reloads LiteLLM whenever the
    config file at the gateway state dir (next to admin.db) changes, and the
    seeded `litellm` service record documents `config_path` as resolved relative
    to ADMIN_DB_PATH.parent.  If AdminSQLiteStore.write_litellm_config anchors the
    artifact anywhere else (e.g. the repo root), operator edits never reach the
    running LiteLLM subprocess and the new service/model is silently unroutable.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)
        self.db = self.state_dir / "admin.db"
        admin_store.init_db(self.db)
        self.store = AdminSQLiteStore(self.db)
        self.ctx = AdminContext(subject="owner", roles=frozenset({"owner"}))
        _install_registry_writer_test_helpers(self.store, self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _artifact_path(self) -> Path:
        # Bare config_path from the seeded litellm record resolves against the
        # state dir; this mirrors start.sh's LITELLM_CONFIG_PATH=data/litellm.config.yaml.
        return self.state_dir / "litellm.config.yaml"

    def test_added_service_with_model_writes_routable_litellm_config_to_state_dir(self) -> None:
        fired = {"n": 0}
        self.store.on_service_registry_changed = lambda: fired.__setitem__("n", fired["n"] + 1)

        self.store.add_service(
            data={
                "name": "text-generation-webui",
                "preset": "openai-compatible",
                "base_url": "http://127.0.0.1:7860/v1",
                "models": [{"public": "my-llama", "upstream": "TheBloke_Llama-3-8B"}],
            },
            actor=self.ctx,
        )

        artifact = self._artifact_path()
        self.assertTrue(
            artifact.exists(),
            f"LiteLLM config must be written to the gateway state dir ({artifact}), "
            "where start.sh's watcher reloads it; otherwise operator edits never "
            "reach the running LiteLLM subprocess.",
        )
        self.assertEqual(fired["n"], 1, "Registry-changed hot-reload callback must fire once")

        import yaml
        compiled = yaml.safe_load(artifact.read_text())
        model_list = compiled.get("model_list", [])
        entry = next((m for m in model_list if m.get("model_name") == "my-llama"), None)
        self.assertIsNotNone(entry, f"Expected a routable 'my-llama' entry in {model_list}")
        params = entry["litellm_params"]
        self.assertEqual(params["model"], "openai/TheBloke_Llama-3-8B")
        self.assertEqual(params["api_base"], "http://127.0.0.1:7860/v1")

    def test_artifact_anchored_to_state_dir_not_repo_root(self) -> None:
        """Regression: the artifact must follow the admin.db location, not the repo.

        Previously write_litellm_config hardcoded base_dir to the repo root, so
        operator edits landed at <repo>/litellm.config.yaml while the watcher read
        data/litellm.config.yaml — a silent divergence that left LiteLLM stale.
        """
        repo_root = Path(__file__).resolve().parent.parent
        repo_stray = repo_root / "litellm.config.yaml"
        pre_existing = repo_stray.exists()

        self.store.add_service(
            data={
                "name": "svc",
                "preset": "openai-compatible",
                "base_url": "http://127.0.0.1:9100/v1",
                "models": [{"public": "m", "upstream": "u"}],
            },
            actor=self.ctx,
        )

        self.assertTrue(self._artifact_path().exists())
        if not pre_existing:
            self.assertFalse(
                repo_stray.exists(),
                "write_litellm_config must not leak an artifact into the repo root; "
                "it must anchor to the gateway state dir (admin.db's directory).",
            )

    def test_edit_service_model_rewrites_litellm_config(self) -> None:
        """Editing a service's model mapping must update the on-disk LiteLLM config."""
        self.store.add_service(
            data={
                "name": "svc",
                "preset": "openai-compatible",
                "base_url": "http://127.0.0.1:9200/v1",
                "models": [{"public": "old-name", "upstream": "old-upstream"}],
            },
            actor=self.ctx,
        )
        self.store.update_service(
            service_id="svc",
            data={
                "base_url": "http://127.0.0.1:9200/v1",
                "models": [{"public": "new-name", "upstream": "new-upstream"}],
            },
            actor=self.ctx,
        )

        import yaml
        compiled = yaml.safe_load(self._artifact_path().read_text())
        names = [m.get("model_name") for m in compiled.get("model_list", [])]
        self.assertIn("new-name", names, "Edited model mapping must be reflected in LiteLLM config")
        self.assertNotIn("old-name", names, "Stale model mapping must be removed from LiteLLM config")


if __name__ == "__main__":
    unittest.main()
