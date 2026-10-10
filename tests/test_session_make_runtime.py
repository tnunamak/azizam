"""The gateway's session make runtime: it runs make jobs through the image routes."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import Response

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import admin_store, me_make, media_jobs, prompt_memory, receipt_store  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n-source-picture-bytes"
SOURCE = "img_" + "5e" * 32
OUTPUT = "img_" + "0a" * 32


class SessionMakeRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.admin = root / "admin.db"
        admin_store.init_db(self.admin)
        media_jobs.init_db(root / "jobs.db")
        receipt_store.init_db(root / "receipts.db")
        for target, name, value in (
            (proxy.ADMIN_STORE, "path", self.admin),
            (media_jobs, "DB_PATH", root / "jobs.db"),
            (receipt_store, "DB_PATH", root / "receipts.db"),
            (proxy, "IMAGE_CACHE_DIR", root / "cache"),
            (proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", root / "payloads"),
            (proxy, "_EPHEMERAL_MEDIA_JOB_PAYLOADS", {}),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        (root / "cache").mkdir()
        proxy.asset_cache_path(SOURCE).write_bytes(PNG)
        self.root = root
        self.user = admin_store.upsert_oidc_user("issuer", "maker", email="maker@example.test", path=self.admin)
        self.app = admin_store.ensure_session_app_for_user(self.user["id"], name="Azizam", path=self.admin)
        self.runtime = proxy._SessionMakeRuntime()
        # The source picture is in the person's Library: an earlier job of theirs made it.
        source_job = media_jobs.MediaJob(intent="image.generate", request_id="req_source",
                                         app_id=self.app["id"], status=media_jobs.STATUS_SUCCEEDED,
                                         assets=[{"asset_id": SOURCE, "media_type": "image/png"}])
        media_jobs.create(source_job, path=root / "jobs.db")

    def _payload(self, intent: str = "image.edit", **body) -> dict:
        body = {"model": "image", "prompt": "private make prompt", **body}
        if intent == "image.edit":
            body.setdefault("source_asset_id", SOURCE)
        return {"job_id": "mj_runtime", "intent": intent, "body": body,
                "auth": {"app_id": self.app["id"], "person_id": self.user["id"]},
                "created_at": 1_700_000_000}

    def _ok(self) -> Response:
        return Response(content=json.dumps({"data": [{"asset_id": OUTPUT, "url": "http://x/y"}]}),
                        status_code=200, media_type="application/json")

    def test_edit_replays_the_source_picture_as_the_session_person(self) -> None:
        seen = {}

        async def fake_edit(request, auth_context, receipt=None):
            form = await request.form()
            seen["fields"] = {key: value for key, value in form.multi_items() if not hasattr(value, "read")}
            upload = form.get("image")
            seen["image"] = await upload.read()
            seen["identity"] = auth_context.identity
            seen["receipt_app"] = receipt.app_id
            return self._ok()

        payload = self._payload(extensions={prompt_memory.NAMESPACE: {"mode": "forget"}})
        with patch.object(proxy, "proxy_comfy_edit_request", fake_edit):
            assets = asyncio.run(self.runtime.execute(payload))
        self.assertEqual([asset["asset_id"] for asset in assets], [OUTPUT])
        self.assertEqual(seen["image"], PNG)
        self.assertEqual(seen["fields"]["prompt"], "private make prompt")
        self.assertEqual(seen["fields"]["response_format"], "url")
        self.assertNotIn("source_asset_id", seen["fields"])
        self.assertEqual(json.loads(seen["fields"]["extensions"]),
                         {prompt_memory.NAMESPACE: {"mode": "forget"}})
        self.assertEqual(seen["identity"].app_id, self.app["id"])
        self.assertEqual(seen["identity"].person_id, self.user["id"])
        self.assertEqual(seen["receipt_app"], self.app["id"])
        self.assertEqual(payload["body"]["source_asset_id"], SOURCE, "the stored payload is not mutated")

    def test_edit_refuses_a_source_the_person_deleted_after_asking(self) -> None:
        async def fake_edit(request, auth_context, receipt=None):
            raise AssertionError("a deleted source must never be read")

        admin_store.record_audit("media.asset.delete", actor_kind="user", actor_id=self.user["id"],
                                 target_kind="media_asset", target_id=SOURCE, path=self.admin)
        self.assertTrue(proxy.asset_cache_path(SOURCE).exists(), "a shared copy can outlive the delete")
        with patch.object(proxy, "proxy_comfy_edit_request", fake_edit):
            with self.assertRaises(me_make.MakeBackendError) as raised:
                asyncio.run(self.runtime.execute(self._payload()))
        self.assertEqual(raised.exception.http_status, 404)
        self.assertIn("no longer in your Library", raised.exception.public_message)

    def test_edit_refuses_a_cached_source_outside_the_persons_library(self) -> None:
        stranger_picture = "img_" + "7d" * 32
        proxy.asset_cache_path(stranger_picture).write_bytes(PNG)
        async def fake_edit(request, auth_context, receipt=None):
            raise AssertionError("another person's picture must never be read")

        with patch.object(proxy, "proxy_comfy_edit_request", fake_edit):
            with self.assertRaises(me_make.MakeBackendError) as raised:
                asyncio.run(self.runtime.execute(self._payload(source_asset_id=stranger_picture)))
        self.assertEqual(raised.exception.http_status, 404)

    def test_generate_runs_the_json_image_route(self) -> None:
        seen = {}

        async def fake_generate(request, auth_context, receipt=None):
            seen["body"] = await request.json()
            return self._ok()

        with patch.object(proxy, "proxy_comfy_request", fake_generate):
            assets = asyncio.run(self.runtime.execute(self._payload("image.generate")))
        self.assertEqual(assets[0]["asset_id"], OUTPUT)
        self.assertEqual(seen["body"], {"model": "image", "prompt": "private make prompt",
                                        "response_format": "url"})

    def test_only_the_persons_own_enabled_session_app_can_run(self) -> None:
        other = admin_store.create_app("Mine", kind="personal", owner_user_id=self.user["id"], path=self.admin)
        stranger = admin_store.upsert_oidc_user("issuer", "stranger", email="s@example.test", path=self.admin)
        cases = {
            "ordinary App": {"app_id": other["id"], "person_id": self.user["id"]},
            "another person": {"app_id": self.app["id"], "person_id": stranger["id"]},
        }
        for label, auth in cases.items():
            with self.subTest(label):
                with self.assertRaises(me_make.MakeBackendError) as raised:
                    asyncio.run(self.runtime.execute({**self._payload(), "auth": auth}))
                self.assertEqual(raised.exception.http_status, 403)
        admin_store.disable_app(self.app["id"], path=self.admin)
        with self.assertRaises(me_make.MakeBackendError):
            asyncio.run(self.runtime.execute(self._payload()))

    def test_backend_failure_keeps_only_transport_evidence(self) -> None:
        async def failing(request, auth_context, receipt=None):
            return Response(content=json.dumps({"error": {"message": "cannot draw private make prompt"}}),
                            status_code=500, media_type="application/json")

        with patch.object(proxy, "proxy_comfy_request", failing):
            with self.assertRaises(me_make.MakeBackendError) as raised:
                asyncio.run(self.runtime.execute(self._payload("image.generate")))
        self.assertNotIn("private make prompt", raised.exception.public_message)
        self.assertEqual(raised.exception.http_status, 500)

    def test_forget_payload_never_reaches_disk(self) -> None:
        payload = self._payload(prompt="forget spool sentinel",
                                extensions={prompt_memory.NAMESPACE: {"mode": "forget"}})
        self.runtime.keep_payload("mj_forget", payload)
        self.assertEqual(self.runtime.load_payload("mj_forget")["body"]["prompt"], "forget spool sentinel")
        on_disk = [path for path in self.root.rglob("*") if path.is_file()]
        self.assertTrue(all(b"forget spool sentinel" not in path.read_bytes() for path in on_disk))
        self.runtime.drop_payload("mj_forget")
        self.assertIsNone(self.runtime.load_payload("mj_forget"))

    def test_remembered_payload_survives_a_restart(self) -> None:
        self.runtime.keep_payload("mj_keep", self._payload())
        proxy._EPHEMERAL_MEDIA_JOB_PAYLOADS.clear()
        self.assertEqual(self.runtime.load_payload("mj_keep")["auth"]["app_id"], self.app["id"])


class SessionAppStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.admin = Path(tmp.name) / "admin.db"
        admin_store.init_db(self.admin)
        self.user = admin_store.upsert_oidc_user("issuer", "p", email="p@example.test", path=self.admin)

    def test_session_app_is_created_once_and_is_never_the_key_home(self) -> None:
        first = admin_store.ensure_session_app_for_user(self.user["id"], name="Azizam", path=self.admin)
        again = admin_store.ensure_session_app_for_user(self.user["id"], name="Azizam", path=self.admin)
        self.assertEqual(first["id"], again["id"])
        home = admin_store.ensure_personal_app_for_user(self.user["id"], path=self.admin)
        self.assertNotEqual(home["id"], first["id"])

    def test_self_service_cannot_see_or_key_the_session_app(self) -> None:
        from gateway import user_runtime
        from gateway.admin_oidc import AdminSession

        session = AdminSession(subject=self.user["id"], email="p@example.test", role="none",
                               issued_at=0, expires_at=2**31)
        app = admin_store.ensure_session_app_for_user(self.user["id"], name="Azizam", path=self.admin)
        snapshot = user_runtime.build_user_portal_snapshot(session, path=self.admin)
        self.assertNotIn(app["id"], [item["id"] for item in snapshot["apps"]])
        with self.assertRaises(user_runtime.UserPortalError):
            user_runtime.mint_user_app_key(session, app["id"], path=self.admin)
        self.assertEqual(admin_store.list_credentials(path=self.admin), [])

    def _legacy_session_app_key(self, app_id: str, key_hash: str) -> str:
        """Write a key the way a gateway without the session-App guard could."""
        import sqlite3

        conn = sqlite3.connect(self.admin)
        try:
            conn.execute("DROP TRIGGER IF EXISTS credentials_never_on_session_apps")
            conn.execute(
                "INSERT INTO credentials(id, subject_kind, subject_id, app_id, hash, prefix, created_at) "
                "VALUES ('cred_legacy', 'app', ?, ?, ?, 'opk_legacy', 1)", (app_id, app_id, key_hash),
            )
            conn.commit()
        finally:
            conn.close()
        return "cred_legacy"

    def test_no_one_can_create_a_key_for_a_session_app(self) -> None:
        from gateway.admin_api import AdminContext
        from gateway.admin_runtime import AdminSQLiteStore

        app = admin_store.ensure_session_app_for_user(self.user["id"], name="Azizam", path=self.admin)
        with self.assertRaises(ValueError):
            admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"],
                                               hash="h" * 64, prefix="opk_x", path=self.admin)
        for actor in ("admin", self.user["id"]):
            with self.subTest(actor=actor), self.assertRaises(ValueError):
                AdminSQLiteStore(self.admin).create_credential(
                    app_id=app["id"], data={},
                    actor=AdminContext(subject=actor, actor_id=actor, roles=frozenset({"admin"})),
                )
        self.assertEqual(admin_store.list_credentials(path=self.admin), [])
        ordinary = admin_store.create_app("Mine", kind="personal", owner_user_id=self.user["id"], path=self.admin)
        admin_store.create_credential_hash(subject_kind="app", subject_id=ordinary["id"],
                                           hash="o" * 64, prefix="opk_o", path=self.admin)

    def test_a_session_app_key_never_authenticates(self) -> None:
        from gateway.admin_runtime import runtime_identity_for_app_credential_hash

        app = admin_store.ensure_session_app_for_user(self.user["id"], name="Azizam", path=self.admin)
        self._legacy_session_app_key(app["id"], "s" * 64)
        self.assertIsNone(runtime_identity_for_app_credential_hash("s" * 64, path=self.admin))

    def test_startup_revokes_existing_session_app_keys(self) -> None:
        app = admin_store.ensure_session_app_for_user(self.user["id"], name="Azizam", path=self.admin)
        ordinary = admin_store.create_app("Mine", kind="personal", owner_user_id=self.user["id"], path=self.admin)
        kept = admin_store.create_credential_hash(subject_kind="app", subject_id=ordinary["id"],
                                                  hash="o" * 64, prefix="opk_o", path=self.admin)
        legacy = self._legacy_session_app_key(app["id"], "s" * 64)
        admin_store.init_db(self.admin)
        credentials = {item["id"]: item for item in admin_store.list_credentials(path=self.admin)}
        self.assertIsNotNone(credentials[legacy]["revoked_at"])
        self.assertIsNone(credentials[kept["id"]]["revoked_at"])
        with self.assertRaises(ValueError):
            admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"],
                                               hash="n" * 64, prefix="opk_n", path=self.admin)


if __name__ == "__main__":
    unittest.main()
