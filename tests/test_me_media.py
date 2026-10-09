from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from gateway import admin_store, media_delivery, media_jobs, media_metadata, me_media, ownership, prompt_memory, provenance, receipt_store
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
        self.prompt_db = self.root / "prompt_memory.db"
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
        self.service_account = admin_store.ensure_service_account("svc_daisy", path=self.admin_db)
        self.service_app = admin_store.create_app(
            "Daisy", kind="service", owner_service_account_id=self.service_account["id"], path=self.admin_db
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
                prompt_memory_path=self.prompt_db,
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
        if request.headers.get("x-azizam-user-action") != "1":
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=403,
                content={"error": {"code": "user_action_header_required"}},
            )
        if request.headers.get("x-azizam-csrf") != "valid-csrf":
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

    def _write_asset(self, asset_id: str, owner_hashes: list[str], prompt: str = "misty lake", request_id: str = "req_cache", created_at: float | int = 1_700_000_000) -> None:
        record = provenance.build_provenance_record(
            kind="image",
            requested_model="image",
            resolved_model="local-image",
            seed=7,
            parameters={"prompt": prompt, "width": 512, "height": 512},
            workflow_id="wf.image",
            created_at=created_at,
            request_id=request_id,
        )
        png = media_delivery.attach_provenance(PNG_1X1, ".png", record)
        png = media_metadata.embed_png_text(png, {"workflow": json.dumps({"nodes": [], "links": []})})
        self._asset_path(asset_id).write_bytes(png)
        self._meta_path(asset_id).write_text(
            json.dumps({"asset_id": asset_id, "owner_key_hashes": owner_hashes, "updated_at": 1_700_000_000})
        )
        if "sha256:alice" in owner_hashes:
            prompt_memory.put(asset_id, self.alice["id"], prompt, kept_until=None, path=self.prompt_db)
        if "sha256:bob" in owner_hashes:
            prompt_memory.put(asset_id, self.bob["id"], prompt, kept_until=None, path=self.prompt_db)

    def _receipt(
        self,
        app_id: str | None,
        request_id: str,
        *,
        cost: float | None = None,
        created_at: int | None = None,
        intent: str = "image.generate",
        route_path: str = "/v1/images/generations",
        requested_model: str | None = "image",
        resolved_model: str | None = "local-image",
        cost_class: str | None = None,
        owner_key_hash: str | None = None,
    ) -> None:
        receipt = RouteReceipt(
            protocol="openai.images",
            intent=intent,
            path=route_path,
            request_id=request_id,
            app_id=app_id,
            requested_model=requested_model,
            resolved_model=resolved_model,
            status="succeeded",
            owner_key_hash=owner_key_hash,
        )
        receipt.accepted_at_ms = 1_700_000_000_000
        receipt.updated_at_ms = 1_700_000_001_000
        if cost is not None:
            receipt.metadata["litellm"] = {"response_cost": cost}
        if cost_class is not None:
            receipt.cost = {"cost_class": cost_class, "usd": cost}
        receipt_store.record(receipt, created_at=int(time.time()) if created_at is None else created_at, path=self.receipts_db)

    def _job(self, request_id: str, asset_id: str, *, workflow: str = "wf.image", media_type: str = "image/png") -> None:
        media_jobs.create(
            media_jobs.MediaJob(
                request_id=request_id,
                intent="image.generate",
                workflow=workflow,
                status=media_jobs.STATUS_SUCCEEDED,
                progress=100,
                assets=[{"asset_id": asset_id, "media_type": media_type}],
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

    def test_media_sources_filter_jobs_and_cache_assets(self) -> None:
        alice_asset = "img_" + "2" * 64
        bob_asset = "img_" + "3" * 64
        service_asset = "img_" + "4" * 64
        local_asset = "img_" + "a2" * 32
        recovered_asset = "img_" + "b2" * 32
        shared_asset = "img_" + "c2" * 32
        now = time.time()
        self._write_asset(alice_asset, ["sha256:alice"], request_id="req_alice", created_at=now)
        self._write_asset(bob_asset, ["sha256:bob"], request_id="req_bob", created_at=now)
        self._write_asset(service_asset, [], request_id="req_service_cache", created_at=now)
        self._write_asset(local_asset, [], request_id="req_local_cache", created_at=now)
        self._write_asset(recovered_asset, ["sha256:alice"], request_id="req_missing", created_at=now)
        self._write_asset(shared_asset, ["sha256:bob", "sha256:alice"], request_id="req_shared_missing", created_at=now)
        admin_store.revoke_credential(self.cred_a["id"], path=self.admin_db)
        self._receipt(self.app_a["id"], "req_alice")
        self._receipt(self.app_b["id"], "req_bob")
        self._receipt(self.service_app["id"], "req_service_cache")
        self._receipt(None, "req_local_cache")
        self._job("req_alice", alice_asset)
        self._job("req_bob", bob_asset)

        own = self.client.get(f"/me/api/media?source=app:{self.app_a['id']}")
        self.assertEqual(own.status_code, 200, own.text)
        self.assertEqual({item["asset_id"] for item in own.json()["assets"]}, {alice_asset, recovered_asset, shared_asset})
        source_by_asset = {item["asset_id"]: item["source"] for item in own.json()["assets"]}
        self.assertEqual(source_by_asset[recovered_asset]["label"], "Alice")
        self.assertEqual(source_by_asset[shared_asset]["label"], "Alice")
        self.assertNotEqual(source_by_asset[shared_asset]["app_id"], self.app_b["id"])
        self.assertEqual([source["id"] for source in own.json()["sources"]], [f"app:{self.app_a['id']}"])
        self.assertEqual(self.client.get("/me/api/media?source=local-network").status_code, 403)
        self.assertEqual(self.client.get(f"/me/api/media?source=app:{self.app_b['id']}").status_code, 403)
        self.assertEqual(self.client.get("/me/api/usage/summary?source=local-network").status_code, 403)
        self.assertEqual(self.client.get(f"/me/api/usage/summary?source=app:{self.app_b['id']}").status_code, 403)

        self.current_user = self.owner
        all_media = self.client.get("/me/api/media")
        self.assertEqual(all_media.status_code, 200, all_media.text)
        source_ids = {source["id"] for source in all_media.json()["sources"]}
        self.assertIn("local-network", source_ids)
        self.assertIn(f"app:{self.service_app['id']}", source_ids)
        service = self.client.get(f"/me/api/media?source=app:{self.service_app['id']}")
        self.assertEqual([item["asset_id"] for item in service.json()["assets"]], [service_asset])
        local = self.client.get("/me/api/media?source=local-network")
        self.assertEqual([item["asset_id"] for item in local.json()["assets"]], [local_asset])
        person_local = self.client.get("/me/api/media?person=local-network")
        self.assertEqual([item["asset_id"] for item in person_local.json()["assets"]], [local_asset])
        service_usage = self.client.get(f"/me/api/usage/summary?source=app:{self.service_app['id']}")
        self.assertEqual(service_usage.status_code, 200, service_usage.text)
        self.assertEqual(service_usage.json()["media_count"], 1)
        local_usage = self.client.get("/me/api/usage/summary?source=local-network")
        self.assertEqual(local_usage.status_code, 200, local_usage.text)
        self.assertEqual(local_usage.json()["media_count"], 1)

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
                headers={"x-azizam-user-action": "1", "x-azizam-csrf": "bad"},
            ).status_code,
            403,
        )
        deleted = self.client.delete(
            f"/me/api/media/{asset_id}",
            headers={"x-azizam-user-action": "1", "x-azizam-csrf": "valid-csrf"},
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

    def test_usage_cache_respects_embedded_foreign_receipt_scope(self) -> None:
        asset_id = "img_" + "9" * 64
        self._write_asset(
            asset_id,
            ["sha256:alice"],
            prompt="foreign receipt",
            request_id="req_foreign_embedded",
            created_at=time.time(),
        )
        self._receipt(self.app_b["id"], "req_foreign_embedded")

        media = self.client.get("/me/api/media")
        self.assertEqual(media.status_code, 200, media.text)
        self.assertEqual(media.json()["assets"], [])
        usage = self.client.get("/me/api/usage/summary")
        self.assertEqual(usage.status_code, 200, usage.text)
        self.assertEqual(usage.json()["media_count"], 0)

        self.current_user = self.owner
        owner_usage = self.client.get("/me/api/usage/summary")
        self.assertEqual(owner_usage.status_code, 200, owner_usage.text)
        self.assertEqual(owner_usage.json()["media_count"], 1)

    def test_usage_job_fallback_excludes_previous_month_receipt_assets(self) -> None:
        now = datetime.now(timezone.utc)
        start = int(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
        previous_month = start - 60
        old_asset = "img_" + "6" * 64
        self._write_asset(
            old_asset,
            ["sha256:alice"],
            request_id="req_previous_month",
            created_at=previous_month,
        )
        self._receipt(self.app_a["id"], "req_previous_month", created_at=previous_month)
        self._job("req_previous_month", old_asset)

        usage = self.client.get(f"/me/api/usage/summary?source=app:{self.app_a['id']}")
        self.assertEqual(usage.status_code, 200, usage.text)
        self.assertEqual(usage.json()["requests"], 0)
        self.assertEqual(usage.json()["media_count"], 0)

    def test_local_network_source_label_preserves_no_key_with_client_hint(self) -> None:
        source = me_media._source_projection(
            {
                "app_id": None,
                "source_label": "Local network · no key",
                "client_hint": "Open WebUI",
            },
            self.admin_db,
        )
        self.assertEqual(source["label"], "Local network · no key · Open WebUI")
        self.assertEqual(source["client_hint"], "Open WebUI")

    def test_owner_missing_receipt_jobs_recover_source_from_job_identity(self) -> None:
        keyed_asset = "img_" + "8" * 64
        service_asset = "img_" + "7" * 64
        self._write_asset(keyed_asset, ["sha256:alice"], request_id="req_missing_keyed", created_at=time.time())
        self._write_asset(service_asset, [], request_id="req_missing_service", created_at=time.time())
        media_jobs.create(
            media_jobs.MediaJob(
                request_id="req_missing_keyed",
                intent="video.generate",
                workflow="wf.video",
                status=media_jobs.STATUS_SUCCEEDED,
                progress=100,
                assets=[{"asset_id": keyed_asset, "media_type": "video/mp4"}],
                owner_key_hash="sha256:alice",
            ),
            path=self.jobs_db,
        )
        media_jobs.create(
            media_jobs.MediaJob(
                request_id="req_missing_service",
                intent="image.generate",
                workflow="wf.image",
                status=media_jobs.STATUS_SUCCEEDED,
                progress=100,
                assets=[{"asset_id": service_asset, "media_type": "image/png"}],
                app_id=self.service_app["id"],
            ),
            path=self.jobs_db,
        )

        member_media = self.client.get("/me/api/media")
        self.assertEqual(member_media.status_code, 200, member_media.text)
        self.assertEqual([item["asset_id"] for item in member_media.json()["assets"]], [keyed_asset])

        self.current_user = self.owner
        owner_media = self.client.get("/me/api/media")
        self.assertEqual(owner_media.status_code, 200, owner_media.text)
        sources = {item["asset_id"]: item["source"] for item in owner_media.json()["assets"]}
        self.assertEqual(sources[keyed_asset]["label"], "Alice")
        self.assertEqual(sources[keyed_asset]["recovered_from"], "asset_owner_key")
        self.assertEqual(sources[service_asset]["label"], "Daisy")
        self.assertEqual(sources[service_asset]["recovered_from"], "media_job")
        usage = self.client.get(f"/me/api/usage/summary?source=app:{self.app_a['id']}")
        self.assertEqual(usage.status_code, 200, usage.text)
        self.assertEqual(usage.json()["media_count"], 1)

    def test_principal_owned_media_includes_rotated_app_and_personal_user_credentials(self) -> None:
        app_principal = ownership.principal_key("app", self.app_a["id"])
        user_principal = ownership.principal_key("user", self.alice["id"])
        self.assertIsNotNone(app_principal)
        self.assertIsNotNone(user_principal)
        retired = admin_store.create_credential_hash(
            subject_kind="app", subject_id=self.app_a["id"], app_id=self.app_a["id"],
            hash="sha256:alice-retired", prefix="test_retired", path=self.admin_db,
        )
        admin_store.revoke_credential(retired["id"], path=self.admin_db)
        admin_store.create_credential_hash(
            subject_kind="app", subject_id=self.app_a["id"], app_id=self.app_a["id"],
            hash="sha256:alice-current", prefix="test_current", path=self.admin_db,
        )
        admin_store.create_credential_hash(
            subject_kind="user", subject_id=self.alice["id"],
            hash="sha256:alice-personal", prefix="test_personal", path=self.admin_db,
        )
        app_cache = "img_" + "1" * 64
        legacy_cache = "img_" + "2" * 64
        app_job = "img_" + "3" * 64
        user_job = "img_" + "4" * 64
        app_cache_only = "img_" + "5" * 64
        user_cache_only = "img_" + "6" * 64
        self._write_asset(app_cache, [app_principal], request_id="req_app_principal", created_at=time.time())
        self._write_asset(legacy_cache, ["sha256:alice-retired"], request_id="req_legacy_key", created_at=time.time())
        self._write_asset(app_job, [app_principal], request_id="req_app_job", created_at=time.time())
        self._write_asset(user_job, [user_principal], request_id="req_user_job", created_at=time.time())
        self._write_asset(app_cache_only, [app_principal], request_id="req_app_cache_only", created_at=time.time())
        self._write_asset(user_cache_only, [user_principal], request_id="req_user_cache_only", created_at=time.time())
        self._receipt(None, "req_app_principal", owner_key_hash=app_principal)
        self._receipt(None, "req_legacy_key", owner_key_hash="sha256:alice-retired")
        self._receipt(None, "req_user_job", owner_key_hash=user_principal)
        media_jobs.create(media_jobs.MediaJob(
            request_id="req_app_principal", intent="image.generate", workflow="wf.image",
            status=media_jobs.STATUS_SUCCEEDED, progress=100,
            assets=[{"asset_id": app_cache}], owner_key_hash=app_principal,
        ), path=self.jobs_db)
        media_jobs.create(media_jobs.MediaJob(
            request_id="req_legacy_key", intent="image.generate", workflow="wf.image",
            status=media_jobs.STATUS_SUCCEEDED, progress=100,
            assets=[{"asset_id": legacy_cache}], owner_key_hash="sha256:alice-retired",
        ), path=self.jobs_db)
        media_jobs.create(media_jobs.MediaJob(
            request_id="req_app_job", intent="image.generate", workflow="wf.image",
            status=media_jobs.STATUS_SUCCEEDED, progress=100,
            assets=[{"asset_id": app_job}], owner_key_hash=app_principal,
        ), path=self.jobs_db)
        media_jobs.create(media_jobs.MediaJob(
            request_id="req_user_job", intent="image.generate", workflow="wf.image",
            status=media_jobs.STATUS_SUCCEEDED, progress=100,
            assets=[{"asset_id": user_job}], owner_key_hash=user_principal,
        ), path=self.jobs_db)

        alice_media = self.client.get("/me/api/media")
        self.assertEqual(alice_media.status_code, 200, alice_media.text)
        self.assertEqual(
            {item["asset_id"] for item in alice_media.json()["assets"]},
            {app_cache, legacy_cache, app_job, user_job, app_cache_only, user_cache_only},
        )
        self.assertEqual(
            me_media._source_projection(None, self.admin_db, metadata={"owner_key_hash": app_principal}, visible_app_ids=[self.app_a["id"]])["app_id"],
            self.app_a["id"],
        )
        self.assertEqual(self.client.get("/me/api/usage/summary").json()["requests"], 3)
        self.assertEqual(
            self.client.get(f"/me/api/usage/summary?source=app:{self.app_a['id']}").json()["requests"],
            2,
        )

        self.current_user = self.bob
        self.assertEqual(self.client.get("/me/api/media").json()["assets"], [])
        self.assertEqual(self.client.get("/me/api/usage/summary").json()["requests"], 0)

    def test_app_attribution_overrides_conflicting_owner_fallback(self) -> None:
        scope = me_media._member_media_scope(self.alice["id"], self.admin_db)
        record = {"app_id": self.app_b["id"], "owner_key_hash": self.cred_a["hash"]}
        self.assertFalse(me_media._receipt_in_scope(record, scope))
        self.assertFalse(me_media._job_in_scope(record, scope))
        self._receipt(self.app_b["id"], "req_conflicting_owner", owner_key_hash=self.cred_a["hash"])
        self.assertEqual(self.client.get("/me/api/usage/summary").json()["requests"], 0)

    def test_usage_summary_scopes_by_user_and_owner_person_all(self) -> None:
        self._receipt(self.app_a["id"], "req_alice", cost=0.25)
        self._receipt(self.app_b["id"], "req_bob", cost=0.75)
        self._receipt(self.service_app["id"], "req_service", cost=1.0)
        self._receipt(None, "req_local", cost=2.0)
        alice_usage = self.client.get("/me/api/usage/summary?period=month")
        self.assertEqual(alice_usage.status_code, 200, alice_usage.text)
        self.assertEqual(alice_usage.json()["requests"], 1)
        self.assertEqual(alice_usage.json()["remote_spend"], 0.25)
        self.assertEqual([source["id"] for source in alice_usage.json()["sources"]], [f"app:{self.app_a['id']}"])

        self.assertEqual(
            self.client.get(f"/me/api/usage/summary?person={self.bob['id']}").status_code,
            403,
        )
        self.current_user = self.owner
        owner_default = self.client.get("/me/api/usage/summary")
        self.assertEqual(owner_default.status_code, 200, owner_default.text)
        self.assertEqual(owner_default.json()["requests"], 4)
        self.assertEqual(owner_default.json()["remote_spend"], 4.0)
        self.assertIn("local-network", {source["id"] for source in owner_default.json()["sources"]})
        self.assertIn(f"app:{self.service_app['id']}", {source["id"] for source in owner_default.json()["sources"]})
        self.assertEqual(
            self.client.get("/me/api/usage/summary?source=local-network").json()["requests"], 1
        )
        self.assertEqual(
            self.client.get(f"/me/api/usage/summary?source=app:{self.service_app['id']}").json()["requests"], 1
        )
        everyone = self.client.get("/me/api/usage/summary?person=all")
        self.assertEqual(everyone.status_code, 200, everyone.text)
        self.assertEqual(everyone.json()["requests"], 4)
        self.assertEqual(everyone.json()["remote_spend"], 4.0)

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
                "x-azizam-user-action": "1",
                "x-azizam-csrf": proxy._csrf_token_for_oidc_session(session),
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

    def test_workflow_filter_is_exact_and_stays_user_scoped(self) -> None:
        alice_asset = "img_" + "f" * 64
        alice_other_asset = "img_" + "a1" * 32
        bob_asset = "img_" + "b1" * 32
        self._write_asset(alice_asset, ["sha256:alice"], "alice first")
        self._write_asset(alice_other_asset, ["sha256:alice"], "alice other")
        self._write_asset(bob_asset, ["sha256:bob"], "bob first")
        self._receipt(self.app_a["id"], "req_alice_workflow")
        self._receipt(self.app_a["id"], "req_alice_other_workflow")
        self._receipt(self.app_b["id"], "req_bob_workflow")
        self._job("req_alice_workflow", alice_asset, workflow="wf.image")
        self._job("req_alice_other_workflow", alice_other_asset, workflow="wf.image.other")
        self._job("req_bob_workflow", bob_asset, workflow="wf.image")

        own = self.client.get("/me/api/media?workflow=wf.image")
        self.assertEqual(own.status_code, 200, own.text)
        self.assertEqual([item["asset_id"] for item in own.json()["assets"]], [alice_asset])
        self.assertEqual(self.client.get("/me/api/media?workflow=wf").json()["assets"], [])

        self.current_user = self.owner
        everyone = self.client.get("/me/api/media?scope=everyone&workflow=wf.image")
        self.assertEqual(everyone.status_code, 200, everyone.text)
        self.assertEqual(
            {item["asset_id"] for item in everyone.json()["assets"]},
            {alice_asset, bob_asset},
        )

    def test_cross_user_delete_and_download_do_not_reveal_foreign_assets(self) -> None:
        asset_id = "img_" + "5" * 64
        self._write_asset(asset_id, ["sha256:bob"])
        headers = {"x-azizam-user-action": "1", "x-azizam-csrf": "valid-csrf"}
        self.assertEqual(self.client.delete(f"/me/api/media/{asset_id}", headers=headers).status_code, 404)
        self.assertEqual(self.client.get(f"/me/api/media/{asset_id}/download?metadata=full").status_code, 404)
        self.assertTrue(self._asset_path(asset_id).exists())

    def test_owner_detail_is_explicit_and_audited_but_cannot_delete_foreign(self) -> None:
        asset_id = "img_" + "6" * 64
        self._write_asset(asset_id, ["sha256:alice"])
        self.current_user = self.owner
        detail = self.client.get(f"/me/api/media/{asset_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(admin_store.list_audit(action_prefix="media.detail.everyone", path=self.admin_db))
        headers = {"x-azizam-user-action": "1", "x-azizam-csrf": "valid-csrf"}
        self.assertEqual(self.client.delete(f"/me/api/media/{asset_id}?scope=everyone", headers=headers).status_code, 404)
        admin_store.assign_admin_role(role="admin", subject_kind="user", subject_id=self.bob["id"], path=self.admin_db)
        self.current_user = self.bob
        self.assertEqual(self.client.get("/me/api/media?scope=everyone").status_code, 403)
        self.assertEqual(self.client.get("/me/api/usage/summary?person=all").status_code, 403)

    def test_deleted_shared_asset_stays_deleted_after_audit_churn(self) -> None:
        asset_id = "img_" + "7" * 64
        self._write_asset(asset_id, ["sha256:alice", "sha256:bob"])
        headers = {"x-azizam-user-action": "1", "x-azizam-csrf": "valid-csrf"}
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

    def test_usage_summary_omits_route_when_model_is_not_recorded(self) -> None:
        self.assertEqual(me_media._capability({"intent": "default"}), "chat")
        self.assertEqual(me_media._capability({"intent": "stt"}), "transcribe")
        self._receipt(
            self.app_a["id"],
            "req_transcribe_default",
            intent="audio.transcriptions",
            route_path="/v1/audio/transcriptions",
            requested_model=None,
            resolved_model=None,
        )
        self._receipt(
            self.app_a["id"],
            "req_chat_default",
            intent="chat.completions",
            route_path="/v1/chat/completions",
            requested_model=None,
            resolved_model=None,
        )
        self._receipt(
            self.app_a["id"],
            "req_unknown",
            intent="",
            route_path="/v1/unknown",
            requested_model=None,
            resolved_model=None,
        )
        self._receipt(
            self.app_a["id"],
            "req_paid",
            cost=0.25,
            intent="chat.completions",
            route_path="/v1/chat/completions",
            requested_model="paid-chat",
            resolved_model="paid-chat",
        )

        usage = self.client.get("/me/api/usage/summary").json()
        rows = {(row["capability"], row["model"]): row for row in usage["by_model"]}

        self.assertEqual(rows[("transcribe", "")]["requests"], 1)
        self.assertEqual(rows[("chat", "")]["requests"], 1)
        self.assertEqual(rows[("other", "")]["requests"], 1)
        self.assertNotIn("/v1/audio/transcriptions", str(usage["by_model"]))
        self.assertNotIn("/v1/chat/completions", str(usage["by_model"]))
        self.assertEqual(usage["paid_requests"], 1)
        self.assertEqual(usage["remote_spend"], 0.25)

    def test_usage_summary_keeps_unknown_paid_spend_distinct_from_no_paid_requests(self) -> None:
        self._receipt(
            self.app_a["id"],
            "req_unknown_paid",
            cost=None,
            cost_class="paid-remote",
            intent="chat.completions",
            route_path="/v1/chat/completions",
            requested_model="paid-chat",
            resolved_model="paid-chat",
        )

        usage = self.client.get("/me/api/usage/summary").json()

        self.assertEqual(usage["paid_requests"], 0)
        self.assertEqual(usage["unknown_paid_requests"], 1)
        self.assertFalse(usage["remote_spend_available"])

    def test_missing_audio_job_asset_is_listed_unavailable_and_not_downloadable(self) -> None:
        asset_id = "aud_" + "0" * 64
        self._receipt(self.app_a["id"], "req_expired")
        self._job("req_expired", asset_id, media_type="audio/mpeg")
        assets = self.client.get("/me/api/media").json()["assets"]
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["asset_id"], asset_id)
        self.assertEqual(assets[0]["kind"], "audio")
        self.assertEqual(assets[0]["media_type"], "audio/mpeg")
        self.assertFalse(assets[0]["available"])
        self.assertIsNone(assets[0]["download_url"])
        self.assertEqual(assets[0]["unavailable_reason"], "Asset file is missing or expired.")
        self.assertEqual(self.client.get(f"/me/api/media/{asset_id}/download").status_code, 404)


if __name__ == "__main__":
    unittest.main()
