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
from gateway.admin_runtime import (  # noqa: E402
    AdminAuthenticator,
    AdminSQLiteStore,
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

        snapshot = self.store.dashboard_snapshot(
            AdminContext(
                subject="svc_test",
                roles=frozenset({"owner"}),
                csrf_token="csrf-token",
            )
        )
        self.assertNotIn("hash", repr(snapshot))
        self.assertEqual(snapshot["actor"]["csrf_token"], "csrf-token")

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


if __name__ == "__main__":
    unittest.main()
