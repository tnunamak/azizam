from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import admin_store  # noqa: E402
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
)
from gateway.runtime_auth import hash_api_key  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
