from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from gateway import admin_store, media_delivery, media_jobs, media_metadata, me_media, provenance, receipt_store
from gateway.admin_oidc import AdminSession
from gateway.admin_oidc import CookieConfig, OIDCConfig, encode_session_cookie, session_from_claims
from gateway.receipts import RouteReceipt


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class MeMediaRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.admin_db = self.root / "admin.db"
        self.jobs_db = self.root / "media_jobs.db"
        self.receipts_db = self.root / "receipts.db"
        self.cache = self.root / "cache"
        self.cache.mkdir()
        admin_store.init_db(self.admin_db)
        media_jobs.init_db(self.jobs_db)
        receipt_store.init_db(self.receipts_db)
        self.alice = admin_store.upsert_oidc_user(
            "issuer", "alice-sub", email="alice@example.com", path=self.admin_db
        )
        self.bob = admin_store.upsert_oidc_user(
            "issuer", "bob-sub", email="bob@example.com", path=self.admin_db
        )
        self.owner = admin_store.upsert_oidc_user(
            "issuer", "owner-sub", email="owner@example.com", path=self.admin_db
        )
        admin_store.assign_admin_role(
            role="owner", subject_kind="user", subject_id=self.owner["id"], path=self.admin_db
        )
        self.app_a = admin_store.create_app(
            "Alice", kind="personal", owner_user_id=self.alice["id"], path=self.admin_db
        )
        self.app_b = admin_store.create_app(
            "Bob", kind="personal", owner_user_id=self.bob["id"], path=self.admin_db
        )
        self.cred_a = admin_store.create_credential_hash(
            subject_kind="app",
            subject_id=self.app_a["id"],
            app_id=self.app_a["id"],
            hash="sha256:alice",
            prefix="test_alice",
            path=self.admin_db,
        )
        self.cred_b = admin_store.create_credential_hash(
            subject_kind="app",
            subject_id=self.app_b["id"],
            app_id=self.app_b["id"],
            hash="sha256:bob",
            prefix="test_bob",
            path=self.admin_db,
        )
        self.current_user = self.alice
        app = FastAPI()
        app.include_router(
            me_media.create_me_media_router(
                session_provider=self._session,
                mutation_validator=self._mutation_validator,
                signed_url_builder=lambda request, asset_id, mode: f"http://testserver/v1/assets/{asset_id}/content?md={mode}&sig=test",
                asset_path_resolver=self._asset_path,
                asset_media_type_resolver=lambda asset_id, path: "image/png",
                asset_deleter=self._delete_unshared,
                asset_catalog_provider=self._catalog,
                asset_metadata_reader=self._asset_metadata,
                admin_db_path=self.admin_db,
                media_jobs_path=self.jobs_db,
                receipts_path=self.receipts_db,
            )
        )
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _session(self, _request: Request) -> AdminSession:
        return AdminSession(
            subject=self.current_user["id"],
            email=self.current_user["email"],
            role="none",
            issued_at=time.time(),
            expires_at=time.time() + 3600,
        )

    def _mutation_validator(self, request: Request, _session: AdminSession):
        if request.headers.get("x-ai-gateway-user-action") != "1":
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=403,
                content={"error": {"code": "user_action_header_required"}},
            )
        if request.headers.get("x-ai-gateway-csrf") != "valid-csrf":
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=403,
                content={"error": {"code": "user_csrf_required"}},
            )
        return None

    def _asset_path(self, asset_id: str) -> Path | None:
        digest = asset_id.split("_", 1)[1]
        return self.cache / f"{digest}.png"

    def _meta_path(self, asset_id: str) -> Path:
        digest = asset_id.split("_", 1)[1]
        return self.cache / f"{digest}.meta.json"

    def _asset_metadata(self, asset_id: str) -> dict:
        path = self._meta_path(asset_id)
        return json.loads(path.read_text()) if path.exists() else {}

    def _catalog(self) -> list[str]:
        ids = []
        for path in self.cache.glob("*.meta.json"):
            data = json.loads(path.read_text())
            ids.append(data["asset_id"])
        return ids

    def _delete_unshared(self, asset_id: str) -> bool:
        owners = set(self._asset_metadata(asset_id).get("owner_key_hashes") or [])
        if len(owners) > 1:
            return False
        for path in (self._asset_path(asset_id), self._meta_path(asset_id)):
            if path and path.exists():
                path.unlink()
        return True

    def _write_asset(self, asset_id: str, owner_hashes: list[str], prompt: str = "misty lake") -> None:
        record = provenance.build_provenance_record(
            kind="image",
            requested_model="image",
            resolved_model="local-image",
            seed=7,
            parameters={"prompt": prompt, "width": 512, "height": 512},
            workflow_id="wf.image",
            created_at=1_700_000_000,
            request_id="req_cache",
        )
        png = media_delivery.attach_provenance(PNG_1X1, ".png", record)
        png = media_metadata.embed_png_text(png, {"workflow": json.dumps({"nodes": [], "links": []})})
        self._asset_path(asset_id).write_bytes(png)
        self._meta_path(asset_id).write_text(
            json.dumps({"asset_id": asset_id, "owner_key_hashes": owner_hashes, "updated_at": 1_700_000_000})
        )

    def _receipt(self, app_id: str, request_id: str, *, cost: float | None = None) -> None:
        receipt = RouteReceipt(
            protocol="openai.images",
            intent="image.generate",
            path="/v1/images/generations",
            request_id=request_id,
            app_id=app_id,
            requested_model="image",
            resolved_model="local-image",
            status="succeeded",
        )
        receipt.accepted_at_ms = 1_700_000_000_000
        receipt.updated_at_ms = 1_700_000_001_000
        if cost is not None:
            receipt.metadata["litellm"] = {"response_cost": cost}
        receipt_store.record(receipt, created_at=int(time.time()), path=self.receipts_db)

    def _job(self, request_id: str, asset_id: str) -> None:
        media_jobs.create(
            media_jobs.MediaJob(
                request_id=request_id,
                intent="image.generate",
                workflow="wf.image",
                status=media_jobs.STATUS_SUCCEEDED,
                progress=100,
                assets=[{"asset_id": asset_id, "media_type": "image/png"}],
            ),
            path=self.jobs_db,
        )

    def test_media_list_detail_and_cross_user_404(self) -> None:
        alice_asset = "img_" + "a" * 64
        bob_asset = "img_" + "b" * 64
        self._write_asset(alice_asset, ["sha256:alice"])
        self._write_asset(bob_asset, ["sha256:bob"])
        self._receipt(self.app_a["id"], "req_alice")
        self._receipt(self.app_b["id"], "req_bob")
        self._job("req_alice", alice_asset)
        self._job("req_bob", bob_asset)

        response = self.client.get("/me/api/media")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([item["asset_id"] for item in response.json()["assets"]], [alice_asset])
        searched = self.client.get("/me/api/media?q=misty")
        self.assertEqual([item["asset_id"] for item in searched.json()["assets"]], [alice_asset])
        self.assertEqual(self.client.get(f"/me/api/media/{bob_asset}").status_code, 404)

        own = self.client.get(f"/me/api/media/{alice_asset}")
        self.assertEqual(own.status_code, 200, own.text)
        self.assertEqual(own.json()["asset"]["provenance"]["workflow"], "wf.image")
        self.assertEqual(own.json()["asset"]["provenance"]["seed"], 7)
        self.assertEqual(own.json()["asset"]["provenance"]["size"], {"width": 512, "height": 512})

    def test_owner_everyone_scope_is_role_gated_and_audited(self) -> None:
        alice_asset = "img_" + "c" * 64
        bob_asset = "img_" + "d" * 64
        self._write_asset(alice_asset, ["sha256:alice"])
        self._write_asset(bob_asset, ["sha256:bob"])
        self._receipt(self.app_a["id"], "req_alice")
        self._receipt(self.app_b["id"], "req_bob")
        self._job("req_alice", alice_asset)
        self._job("req_bob", bob_asset)

        denied = self.client.get("/me/api/media?scope=everyone")
        self.assertEqual(denied.status_code, 403)
        self.current_user = self.owner
        allowed = self.client.get("/me/api/media?scope=everyone")
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual({item["asset_id"] for item in allowed.json()["assets"]}, {alice_asset, bob_asset})
        audit = admin_store.list_audit(action_prefix="media.library.everyone", path=self.admin_db)
        self.assertEqual(len(audit), 1)

    def test_delete_tombstone_does_not_hide_shared_asset_from_other_user(self) -> None:
        asset_id = "img_" + "e" * 64
        self._write_asset(asset_id, ["sha256:alice", "sha256:bob"])
        self._receipt(self.app_a["id"], "req_alice")
        self._receipt(self.app_b["id"], "req_bob")
        self._job("req_alice", asset_id)
        self._job("req_bob", asset_id)

        self.assertEqual(self.client.delete(f"/me/api/media/{asset_id}").status_code, 403)
        self.assertEqual(
            self.client.delete(
                f"/me/api/media/{asset_id}",
                headers={"x-ai-gateway-user-action": "1", "x-ai-gateway-csrf": "bad"},
            ).status_code,
            403,
        )
        deleted = self.client.delete(
            f"/me/api/media/{asset_id}",
            headers={"x-ai-gateway-user-action": "1", "x-ai-gateway-csrf": "valid-csrf"},
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertTrue(deleted.json()["deleted"])
        self.assertFalse(deleted.json()["physical_removed"])
        self.assertTrue(self._asset_path(asset_id).exists())
        self.assertEqual(self.client.get("/me/api/media").json()["assets"], [])

        self.current_user = self.bob
        bob_list = self.client.get("/me/api/media")
        self.assertEqual(bob_list.status_code, 200)
        self.assertEqual([item["asset_id"] for item in bob_list.json()["assets"]], [asset_id])

    def test_cache_fallback_survives_without_job_or_receipt(self) -> None:
        asset_id = "img_" + "f" * 64
        self._write_asset(asset_id, ["sha256:alice"], prompt="orange boat")

        response = self.client.get("/me/api/media?q=orange")
        self.assertEqual(response.status_code, 200, response.text)
        assets = response.json()["assets"]
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["asset_id"], asset_id)
        self.assertEqual(assets[0]["prompt"], "orange boat")
        self.assertEqual(assets[0]["provenance"]["source"], "asset_cache")
        self.assertTrue(assets[0]["provenance"]["complete"])

    def test_usage_summary_scopes_by_user_and_owner_person_all(self) -> None:
        self._receipt(self.app_a["id"], "req_alice", cost=0.25)
        self._receipt(self.app_b["id"], "req_bob", cost=0.75)
        alice_usage = self.client.get("/me/api/usage/summary?period=month")
        self.assertEqual(alice_usage.status_code, 200, alice_usage.text)
        self.assertEqual(alice_usage.json()["requests"], 1)
        self.assertEqual(alice_usage.json()["remote_spend"], 0.25)

        self.assertEqual(
            self.client.get(f"/me/api/usage/summary?person={self.bob['id']}").status_code,
            403,
        )
        self.current_user = self.owner
        everyone = self.client.get("/me/api/usage/summary?person=all")
        self.assertEqual(everyone.status_code, 200, everyone.text)
        self.assertEqual(everyone.json()["requests"], 2)
        self.assertEqual(everyone.json()["remote_spend"], 1.0)

    def test_proxy_download_url_fetches_signed_content(self) -> None:
        import proxy

        asset_id = "img_" + "1" * 64
        old_admin_path = proxy.ADMIN_STORE.path
        old_jobs_path = media_jobs.DB_PATH
        old_receipts_path = receipt_store.DB_PATH
        old_cache = proxy.IMAGE_CACHE_DIR
        old_secret_cfg = proxy.IMAGE_SIGNED_URL_SECRET_CFG
        old_secret = proxy._image_url_signing_secret
        old_oidc = proxy.ADMIN_OIDC_CONFIG
        old_signing_key = proxy.ADMIN_AUTH.signing_key
        old_cookie = proxy.ADMIN_AUTH.cookie
        try:
            proxy.ADMIN_STORE.path = self.admin_db
            media_jobs.DB_PATH = self.jobs_db
            receipt_store.DB_PATH = self.receipts_db
            proxy.IMAGE_CACHE_DIR = self.cache
            proxy.IMAGE_SIGNED_URL_SECRET_CFG = "test-signing-secret"
            proxy._image_url_signing_secret = None
            cfg = OIDCConfig(
                enabled=True,
                scopes=("openid", "email"), allowed_email_domains=(), admin_emails=(),
                issuer="issuer",
                client_id="client",
                client_secret="secret",
                redirect_uri="http://testserver/callback",
                signing_key=b"s" * 32,
                cookie=CookieConfig(secure=False),
                authorization_endpoint="http://idp/auth",
                token_endpoint="http://idp/token",
                jwks_uri="http://idp/jwks",
            )
            proxy.ADMIN_OIDC_CONFIG = cfg
            proxy.ADMIN_AUTH.signing_key = cfg.signing_key
            proxy.ADMIN_AUTH.cookie = cfg.cookie
            self._write_asset(asset_id, ["sha256:alice"], prompt="signed route")
            self._receipt(self.app_a["id"], "req_signed")
            self._job("req_signed", asset_id)
            session = session_from_claims(
                {"sub": self.alice["id"], "email": self.alice["email"]},
                role="none",
                cookie=cfg.cookie,
            )
            client = TestClient(proxy.app)
            client.cookies.set(
                cfg.cookie.name,
                encode_session_cookie(session, signing_key=cfg.signing_key),
            )

            detail = client.get(f"/me/api/media/{asset_id}/download")
            self.assertEqual(detail.status_code, 200, detail.text)
            download_url = detail.json()["download_url"]
            content = client.get(download_url)
            self.assertEqual(content.status_code, 200, content.text)
            self.assertEqual(content.headers["content-type"], "image/png")
            self.assertEqual(media_metadata.png_text_chunks(content.content), {})
            full = client.get(f"/me/api/media/{asset_id}/download?metadata=full")
            self.assertEqual(full.status_code, 200)
            workflow_url = full.json()["download_url"]
            workflow_png = client.get(workflow_url)
            self.assertEqual(workflow_png.status_code, 200)
            self.assertIn("workflow", media_metadata.png_text_chunks(workflow_png.content))
            self.assertIn(provenance.PROVENANCE_KEY, media_metadata.png_text_chunks(workflow_png.content))
            self.assertEqual(client.get(workflow_url.replace("md=full", "md=none")).status_code, 403)
            self.assertEqual(client.delete(f"/me/api/media/{asset_id}").status_code, 403)
            removed = client.delete(f"/me/api/media/{asset_id}", headers={
                "x-ai-gateway-user-action": "1",
                "x-ai-gateway-csrf": proxy._csrf_token_for_oidc_session(session),
            })
            self.assertEqual(removed.status_code, 200, removed.text)
            self.assertTrue(removed.json()["physical_removed"])
            self.assertEqual(client.get(download_url).status_code, 404)
            client.cookies.clear()
            self.assertEqual(client.get(f"/me/api/media/{asset_id}/download").status_code, 401)
        finally:
            proxy.ADMIN_STORE.path = old_admin_path
            media_jobs.DB_PATH = old_jobs_path
            receipt_store.DB_PATH = old_receipts_path
            proxy.IMAGE_CACHE_DIR = old_cache
            proxy.IMAGE_SIGNED_URL_SECRET_CFG = old_secret_cfg
            proxy._image_url_signing_secret = old_secret
            proxy.ADMIN_OIDC_CONFIG = old_oidc
            proxy.ADMIN_AUTH.signing_key = old_signing_key
            proxy.ADMIN_AUTH.cookie = old_cookie

    def test_paging_filters_and_embedded_generation_time(self) -> None:
        for char, prompt in (("2", "orange boat"), ("3", "blue boat"), ("4", "red sky")):
            asset_id = "img_" + char * 64
            self._write_asset(asset_id, ["sha256:alice"], prompt)
            self._receipt(self.app_a["id"], "req_" + char)
            self._job("req_" + char, asset_id)
        first = self.client.get("/me/api/media?limit=1&q=boat&model=local-image&kind=image").json()
        self.assertEqual(len(first["assets"]), 1)
        item = first["assets"][0]
        self.assertEqual(item["seed"], 7)
        self.assertEqual(item["width"], 512)
        self.assertEqual(item["created_at"], 1_700_000_000)
        self.assertEqual(item["provenance"]["created_at"], item["created_at"])
        self.assertEqual(item["provenance"]["seed"], item["seed"])
        self.assertTrue(item["workflow_download_url"])
        second = self.client.get("/me/api/media?limit=1&q=boat&cursor=" + first["next_cursor"]).json()
        self.assertEqual(len(second["assets"]), 1)
        self.assertNotEqual(item["asset_id"], second["assets"][0]["asset_id"])
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(self.client.get("/me/api/media?start=2026-01-01").json()["assets"], [])
        self.assertEqual(self.client.get("/me/api/media?cursor=bad").status_code, 400)
        self.assertEqual(self.client.get("/me/api/media?start=invalid").status_code, 400)
        self.assertEqual(me_media._date_to_epoch("2023-11-14"), 1_699_920_000)

    def test_cross_user_delete_and_download_do_not_reveal_foreign_assets(self) -> None:
        asset_id = "img_" + "5" * 64
        self._write_asset(asset_id, ["sha256:bob"])
        headers = {"x-ai-gateway-user-action": "1", "x-ai-gateway-csrf": "valid-csrf"}
        self.assertEqual(self.client.delete(f"/me/api/media/{asset_id}", headers=headers).status_code, 404)
        self.assertEqual(self.client.get(f"/me/api/media/{asset_id}/download?metadata=full").status_code, 404)
        self.assertTrue(self._asset_path(asset_id).exists())

    def test_owner_detail_is_explicit_and_audited_but_cannot_delete_foreign(self) -> None:
        asset_id = "img_" + "6" * 64
        self._write_asset(asset_id, ["sha256:alice"])
        self.current_user = self.owner
        self.assertEqual(self.client.get(f"/me/api/media/{asset_id}").status_code, 404)
        detail = self.client.get(f"/me/api/media/{asset_id}?scope=everyone")
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(admin_store.list_audit(action_prefix="media.detail.everyone", path=self.admin_db))
        headers = {"x-ai-gateway-user-action": "1", "x-ai-gateway-csrf": "valid-csrf"}
        self.assertEqual(self.client.delete(f"/me/api/media/{asset_id}?scope=everyone", headers=headers).status_code, 404)
        admin_store.assign_admin_role(role="admin", subject_kind="user", subject_id=self.bob["id"], path=self.admin_db)
        self.current_user = self.bob
        self.assertEqual(self.client.get("/me/api/media?scope=everyone").status_code, 403)
        self.assertEqual(self.client.get("/me/api/usage/summary?person=all").status_code, 403)

    def test_deleted_shared_asset_stays_deleted_after_audit_churn(self) -> None:
        asset_id = "img_" + "7" * 64
        self._write_asset(asset_id, ["sha256:alice", "sha256:bob"])
        headers = {"x-ai-gateway-user-action": "1", "x-ai-gateway-csrf": "valid-csrf"}
        self.assertEqual(self.client.delete(f"/me/api/media/{asset_id}", headers=headers).status_code, 200)
        for index in range(1001):
            admin_store.record_audit("media.asset.delete", actor_id=self.bob["id"], actor_kind="user",
                                     target_id=f"other-{index}", path=self.admin_db)
        self.assertEqual(self.client.get(f"/me/api/media/{asset_id}").status_code, 404)
        self.current_user = self.bob
        self.assertEqual(self.client.get(f"/me/api/media/{asset_id}").status_code, 200)

    def test_disabled_apps_keep_owned_history(self) -> None:
        asset_id = "img_" + "8" * 64
        self._write_asset(asset_id, ["sha256:alice"])
        self._receipt(self.app_a["id"], "req_disabled")
        admin_store.disable_app(self.app_a["id"], path=self.admin_db)
        self.assertEqual(self.client.get(f"/me/api/media/{asset_id}").status_code, 200)
        self.assertEqual(self.client.get("/me/api/usage/summary").json()["requests"], 1)

    def test_jpeg_webp_embedded_provenance(self) -> None:
        from PIL import Image
        record = provenance.build_provenance_record(kind="image", created_at=1700000000,
                                                    parameters={"prompt": "EXIF boat"}, seed=4)
        exif = media_metadata.exif_bytes({provenance.PROVENANCE_KEY: json.dumps(record)})
        for suffix, fmt in ((".jpg", "JPEG"), (".webp", "WEBP")):
            path = self.cache / ("exif" + suffix)
            Image.new("RGB", (2, 2)).save(path, format=fmt, exif=exif)
            embedded = me_media._embedded_provenance(path)
            self.assertEqual(embedded["seed"], 4)
            self.assertEqual(embedded["parameters"]["prompt"], "EXIF boat")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
    def test_audio_video_embedded_provenance(self) -> None:
        record = provenance.build_provenance_record(kind="audio", created_at=1700000000,
                                                    parameters={"prompt": "container boat"}, seed=8)
        for suffix, source, codec in ((".wav", "sine=duration=0.1", "pcm_s16le"),
                                      (".mp4", "color=c=teal:s=16x16:d=0.1", "mpeg4")):
            path = self.cache / ("container" + suffix)
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", source,
                            "-c", codec, str(path)], check=True, capture_output=True, timeout=10)
            path.write_bytes(media_delivery.attach_provenance(path.read_bytes(), suffix, record,
                                                               workdir=self.root / "metadata"))
            embedded = me_media._embedded_provenance(path)
            self.assertEqual(embedded["seed"], 8)
            self.assertEqual(embedded["parameters"]["prompt"], "container boat")
            self.assertFalse(embedded["workflow_available"])

    def test_cache_only_media_counts_and_spend_absence_are_honest(self) -> None:
        asset_id = "img_" + "9" * 64
        self._write_asset(asset_id, ["sha256:alice"])
        record = provenance.build_provenance_record(kind="image", created_at=time.time(),
                                                    parameters={"prompt": "new image"})
        self._asset_path(asset_id).write_bytes(media_delivery.attach_provenance(PNG_1X1, ".png", record))
        usage = self.client.get("/me/api/usage/summary").json()
        self.assertEqual(usage["media_count"], 1)
        self.assertEqual(usage["requests"], 0)
        self.assertFalse(usage["remote_spend_available"])
        self.assertEqual(usage["remote_spend"], 0)
        self.assertEqual(usage["retention_days"], 14)
        self.assertIn("coverage_start", usage)
        self.assertEqual(usage["people"], [])

    def test_usage_excludes_children_and_preserves_unknown_costs(self) -> None:
        self._receipt(self.app_a["id"], "req_outer")
        receipt_store.record({"request_id": "req_child", "app_id": self.app_a["id"],
                              "parent_request_id": "req_outer", "status": "succeeded"}, path=self.receipts_db)
        usage = self.client.get("/me/api/usage/summary").json()
        self.assertEqual(usage["requests"], 1)
        self.assertIsNone(usage["by_model"][0]["remote_spend"])
        self.assertEqual(usage["by_model"][0]["capability"], "image")
        self.assertEqual(me_media._capability({"intent": "audio.transcribe"}), "transcribe")
        self.assertEqual(me_media._capability({"path": "/v1/audio/translations"}), "transcribe")
        self.assertIsNone(usage["receipts"][0]["remote_spend"])
        for invalid in ("NaN", "Infinity", "-1", "bad", True):
            self.assertIsNone(me_media._receipt_cost({"metadata": {"litellm": {"response_cost": invalid}}}))

    def test_expired_asset_is_not_listed_or_downloadable(self) -> None:
        asset_id = "img_" + "0" * 64
        self._receipt(self.app_a["id"], "req_expired")
        self._job("req_expired", asset_id)
        self.assertEqual(self.client.get("/me/api/media").json()["assets"], [])
        self.assertEqual(self.client.get(f"/me/api/media/{asset_id}/download").status_code, 404)


if __name__ == "__main__":
    unittest.main()
