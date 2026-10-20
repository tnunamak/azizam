from __future__ import annotations

import base64
import json
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from gateway import admin_store, mcp_oauth_store, media_delivery, media_jobs, media_metadata, me_make, me_media, ownership, prompt_memory, provenance, receipt_store
from gateway.admin_oidc import AdminSession
from gateway.admin_oidc import CookieConfig, OIDCConfig, encode_session_cookie, session_from_claims
from gateway.receipts import RouteReceipt
from tests.support.access import service_app


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
        self.prompt_db = self.jobs_db
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
            "Alice", kind="personal", owner_user_id=self.alice["id"], access_plan="person", path=self.admin_db
        )
        self.app_b = admin_store.create_app(
            "Bob", kind="personal", owner_user_id=self.bob["id"], access_plan="person", path=self.admin_db
        )
        self.service_account = admin_store.ensure_service_account("svc_daisy", path=self.admin_db)
        self.service_app = service_app("Daisy", path=self.admin_db)
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

    def test_job_notifications_are_scoped_even_for_owner(self) -> None:
        for app in (self.app_a, self.app_b):
            media_jobs.create(media_jobs.MediaJob(intent="video.generate", app_id=app["id"]), path=self.jobs_db)
        rows = self.client.get("/me/api/jobs?scope=everyone").json()["jobs"]
        self.assertEqual(len(rows), 1)
        foreign = next(job for job in media_jobs.recent(path=self.jobs_db) if job["app_id"] == self.app_b["id"])
        self.assertEqual(self.client.get(f"/me/api/jobs/{foreign['job_id']}").status_code, 404)
        self.current_user = self.owner
        self.assertEqual(self.client.get("/me/api/jobs").json()["jobs"], [])
        self.assertEqual(self.client.get(f"/me/api/jobs/{foreign['job_id']}").status_code, 404)

    def test_job_watch_excludes_history_and_keeps_active_and_timestamp_ties(self) -> None:
        now = int(time.time())
        for key, status, updated in (("old", "succeeded", 1), ("active", "running", 1),
                                     ("recent", "succeeded", now)):
            media_jobs.create(media_jobs.MediaJob(job_id=key, intent="image.generate",
                app_id=self.app_a["id"], status=status, created_at=updated, updated_at=updated), path=self.jobs_db)
        with patch("gateway.me_media.time.time", return_value=now):
            first = self.client.get("/me/api/jobs?watch=1").json()
        self.assertEqual({job["job_id"] for job in first["jobs"]}, {"active", "recent"})
        self.assertEqual(first["polled_at"], now)
        # An old active job finishes at exactly the watermark's second.
        media_jobs.update_status("active", "succeeded", now=now, path=self.jobs_db)
        page = self.client.get(f"/me/api/jobs?watch=1&since={now}").json()
        self.assertEqual({job["job_id"] for job in page["jobs"]}, {"active", "recent"})
        self.assertEqual(len(self.client.get("/me/api/jobs").json()["jobs"]), 3)
        self.assertEqual(self.client.get("/me/api/jobs?watch=1&since=bad").status_code, 400)

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
        status: str = "succeeded",
        owner_key_hash: str | None = None,
        connection_id: str | None = None,
        api_key_name: str | None = None,
    ) -> None:
        receipt = RouteReceipt(
            protocol="openai.images",
            intent=intent,
            path=route_path,
            request_id=request_id,
            app_id=app_id,
            requested_model=requested_model,
            resolved_model=resolved_model,
            status=status,
            owner_key_hash=owner_key_hash,
            connection_id=connection_id,
            api_key_name=api_key_name,
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

    def test_item_without_recorded_origin_or_time_stays_unknown(self) -> None:
        # No receipt, no job, no provenance and an owner key no App holds:
        # nothing shows where it came from or when it was made.
        asset_id = "img_" + "f" * 64
        self._asset_path(asset_id).write_bytes(PNG_1X1)
        self._meta_path(asset_id).write_text(json.dumps({"asset_id": asset_id, "owner_key_hashes": ["sha256:nobody"]}))
        self.current_user = self.owner
        item = next(asset for asset in self.client.get("/me/api/media?scope=everyone").json()["assets"]
                    if asset["asset_id"] == asset_id)
        self.assertEqual((item["source"]["kind"], item["source"]["label"]), ("unknown", "Source unknown"))
        self.assertIsNone(item["created_at"], "file time is not creation time")
        self.assertIsNone(item["provenance"]["created_at"])
        self.assertEqual(item["file_modified_at"], int(self._asset_path(asset_id).stat().st_mtime))
        self.assertEqual(item["title"], "Picture by Source unknown")
        self.assertNotIn("1970", item["title"])

    def test_legacy_receipt_without_source_kind_is_not_called_local_network(self) -> None:
        receipt_store.record({"request_id": "req_legacy", "status": "succeeded", "intent": "image.generate"},
                             path=self.receipts_db)
        source = me_media._source_projection(receipt_store.get("req_legacy", audience="internal",
                                                               path=self.receipts_db) | {"source_kind": None},
                                             self.admin_db)
        self.assertEqual(source["kind"], "unknown")
        recorded = me_media._source_projection({"request_id": "r", "source_kind": "local-network",
                                                "source_label": "Local network · no key"}, self.admin_db)
        self.assertEqual(recorded["kind"], "local-network")

    def test_client_hint_is_only_a_recorded_hint_never_guessed_from_key_names(self) -> None:
        self.assertIsNone(me_media._client_hint({"api_key_name": "daisy kitchen"}))
        self.assertIsNone(me_media._client_hint({"metadata": {"user_agent": "curl on a raspberry pi"}}))
        self.assertEqual(me_media._client_hint({"client_hint": "SillyTavern"}), "SillyTavern")

    def test_usage_defaults_to_your_own_work_for_every_role(self) -> None:
        self._receipt(self.app_a["id"], "req_alice")
        self._receipt(self.app_b["id"], "req_bob")
        self.current_user = self.owner
        default = self.client.get("/me/api/usage/summary").json()
        self.assertEqual((default["scope"], default["requests"]), ("me", 0))
        everyone = self.client.get("/me/api/usage/summary", params={"person": "all"}).json()
        self.assertEqual((everyone["scope"], everyone["requests"]), ("everyone", 2))
        people = {row["id"]: row["label"] for row in everyone["people"]}
        self.assertEqual(people["self"], "You")

    def test_usage_receipts_show_retention_detail_only_to_maker(self) -> None:
        entry = {"kind": "conversation", "decision": "kept", "reason": "kept",
                 "kept_until_ms": 1792300000000, "parts": ["accepted"], "limited_by": "member_choice"}
        receipt_store.record({"request_id": "req_private_usage", "status": "succeeded",
                              "intent": "chat.completions", "app_id": self.app_a["id"],
                              "maker_person_id": self.alice["id"], "retention": [entry]},
                             path=self.receipts_db)
        mine = self.client.get("/me/api/usage/summary")
        assert mine.status_code == 200
        assert mine.json()["receipts"][0]["retention"] == [entry]
        self.current_user = self.owner
        everyone = self.client.get("/me/api/usage/summary?person=all")
        assert everyone.status_code == 200
        assert everyone.json()["receipts"][0]["retention"] == [
            {"kind": "conversation", "decision": "kept"}]

    def test_media_defaults_to_your_own_work_for_every_role(self) -> None:
        self._write_asset("img_" + "1" * 64, ["sha256:bob"])
        self.current_user = self.owner
        default = self.client.get("/me/api/media").json()
        self.assertEqual((default["scope"], default["assets"]), ("me", []))
        self.assertEqual(default["default_scope"], "self")
        everyone = self.client.get("/me/api/media", params={"scope": "everyone"}).json()
        self.assertEqual(len(everyone["assets"]), 1)
        self.assertEqual(everyone["default_scope"], "explicit")

    def _complete_history(self) -> None:
        with sqlite3.connect(self.receipts_db) as conn:
            conn.execute("UPDATE receipt_history SET floor = 0")

    def test_usage_notes_name_what_is_counted_without_implementation_nouns(self) -> None:
        self._complete_history()
        usage = self.client.get("/me/api/usage/summary").json()
        self.assertEqual(usage["remote_spend_coverage"],
                         "This total includes recorded request costs only; other charges may be missing.")
        self.assertNotIn("ledger", usage["remote_spend_coverage"])
        self.assertEqual(usage["coverage_note"], "Request totals include saved activity only. "
                                                 "Creation totals also include files still in your Library.")

    def test_usage_separates_aggregate_completeness_from_the_short_receipt_list(self) -> None:
        self._complete_history()
        for index in range(me_media.USAGE_RECEIPT_LIMIT + 1):
            self._receipt(self.app_a["id"], f"req_many_{index}")
        usage = self.client.get("/me/api/usage/summary").json()
        self.assertEqual(usage["requests"], me_media.USAGE_RECEIPT_LIMIT + 1)
        self.assertEqual(len(usage["receipts"]), me_media.USAGE_RECEIPT_LIMIT)
        self.assertTrue(usage["receipt_list_has_more"])
        self.assertTrue(usage["aggregate_complete"])
        self.assertFalse(usage["truncated"])

    def test_usage_counts_a_job_whose_receipt_is_a_child_request_this_month(self) -> None:
        asset_id = "img_" + "2" * 64
        child = RouteReceipt(protocol="openai.images", intent="image.generate", path="/v1/images/generations",
                             request_id="req_child", app_id=self.app_a["id"], parent_request_id="req_parent",
                             status="succeeded")
        receipt_store.record(child, path=self.receipts_db)
        self._job("req_child", asset_id)
        usage = self.client.get("/me/api/usage/summary").json()
        # receipt_store.get carries the stored time, so the item is counted.
        self.assertEqual(usage["media_count"], 1)

    def test_recent_order_indexes_preserve_results_and_avoid_sort(self) -> None:
        for index in range(12):
            media_jobs.create(media_jobs.MediaJob(
                job_id=f"mj_order_{index:02d}", request_id=f"req_order_{index}",
                intent="image.generate", created_at=100, updated_at=200 + index // 3,
                status=media_jobs.STATUS_SUCCEEDED if index % 2 else media_jobs.STATUS_FAILED,
            ), path=self.jobs_db)
        with sqlite3.connect(str(self.jobs_db)) as conn:
            conn.execute("DROP INDEX IF EXISTS media_jobs_updated_order")
            conn.execute("DROP INDEX IF EXISTS media_jobs_status_updated_order")
        expected = {
            status: media_jobs.recent(limit=5, status=status, path=self.jobs_db)
            for status in (None, media_jobs.STATUS_SUCCEEDED)
        }
        # Exercise the existing-database migration as well as a fresh schema.
        media_jobs.init_db(self.jobs_db)
        for status, index_name in (
            (None, "media_jobs_updated_order"),
            (media_jobs.STATUS_SUCCEEDED, "media_jobs_status_updated_order"),
        ):
            with self.subTest(status=status):
                actual = media_jobs.recent(limit=5, status=status, path=self.jobs_db)
                self.assertEqual(json.dumps(actual), json.dumps(expected[status]))
                where = " WHERE status = ?" if status else ""
                params = (status, 5) if status else (5,)
                with sqlite3.connect(str(self.jobs_db)) as conn:
                    plan = conn.execute(
                        "EXPLAIN QUERY PLAN SELECT job_json FROM media_jobs" + where
                        + " ORDER BY updated_at DESC, job_id DESC LIMIT ?", params,
                    ).fetchall()
                details = " ".join(str(row[3]) for row in plan)
                self.assertIn(index_name, details)
                self.assertNotIn("USE TEMP B-TREE", details)

    def test_usage_job_receipt_join_is_linear_and_byte_identical(self) -> None:
        count = 12
        for index in range(count):
            request_id = f"req_join_{index:02d}"
            self._receipt(self.app_a["id"], request_id, cost=0.25)
            self._job(request_id, "img_" + f"{index:064x}")
        self._receipt(self.app_b["id"], "req_join_bob", cost=4.0)
        self._job("req_join_bob", "img_" + "f" * 64)
        request_id_reads = 0

        class CountingReceipt(dict):
            def get(self, key, default=None):
                nonlocal request_id_reads
                if key == "request_id":
                    request_id_reads += 1
                return super().get(key, default)

        loads = json.loads

        def tracked_loads(*args, **kwargs):
            value = loads(*args, **kwargs)
            return CountingReceipt(value) if isinstance(value, dict) and "protocol" in value else value

        with patch.object(me_media.time, "time", return_value=int(time.time())):
            expected = self.client.get("/me/api/usage/summary")
            with patch.object(me_media.json, "loads", side_effect=tracked_loads):
                actual = self.client.get("/me/api/usage/summary")
            self.assertEqual(actual.status_code, 200, actual.text)
            self.assertEqual(actual.content, expected.content)
            self.assertLessEqual(request_id_reads, 2 * count)
        self.assertEqual(actual.json()["requests"], count)
        self.assertEqual(actual.json()["media_count"], count)
        self.current_user = self.bob
        bob = self.client.get("/me/api/usage/summary").json()
        self.assertEqual(bob["requests"], 1)
        self.assertEqual(bob["media_count"], 1)

    def test_multi_asset_job_projects_source_once_without_cross_request_cache(self) -> None:
        assets = ["img_" + f"{index:064x}" for index in range(3)]
        self._receipt(self.app_a["id"], "req_multi_source")
        media_jobs.create(media_jobs.MediaJob(
            request_id="req_multi_source", intent="image.generate",
            status=media_jobs.STATUS_SUCCEEDED,
            assets=[{"asset_id": asset_id} for asset_id in assets],
        ), path=self.jobs_db)
        expected = self.client.get("/me/api/media")
        # The list reads the apps in one query and never asks per job or per asset.
        with patch.object(admin_store, "get_app", wraps=admin_store.get_app) as lookup:
            actual = self.client.get("/me/api/media")
            self.assertEqual(actual.status_code, 200, actual.text)
            self.assertEqual(actual.content, expected.content)
            lookup.assert_not_called()
        # Nothing is kept between requests: a renamed app shows at once.
        renamed = [{**self.app_a, "name": "Renamed"}]
        with patch.object(admin_store, "list_apps", return_value=renamed):
            refreshed = self.client.get("/me/api/media")
            self.assertTrue(all(item["source"]["label"] == "Renamed" for item in refreshed.json()["assets"]))
        self.current_user = self.bob
        self.assertEqual(self.client.get("/me/api/media").json()["assets"], [])

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
        self.assertEqual(own.json()["asset"]["prompt"], "misty lake")
        self.assertEqual(own.json()["asset"]["title"], "misty lake")

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
        self.assertTrue(all(item["prompt"] is None and item["title"].startswith("Picture by ")
                            and "misty lake" not in item["title"] for item in allowed.json()["assets"]))
        foreign = self.client.get(f"/me/api/media?person={self.alice['id']}")
        self.assertEqual(foreign.status_code, 200, foreign.text)
        self.assertTrue(all(item["prompt"] is None and "misty lake" not in item["title"]
                            for item in foreign.json()["assets"]))
        audit = admin_store.list_audit(action_prefix="media.library.everyone", path=self.admin_db)
        self.assertEqual(len(audit), 1)

    def test_owner_sees_own_prompt_in_instance_library_only(self) -> None:
        owner_app = admin_store.create_app(
            "Owner personal", kind="personal", owner_user_id=self.owner["id"], access_plan="person", path=self.admin_db,
        )
        asset_id = "img_" + "e" * 64
        self._write_asset(asset_id, ["sha256:alice"], prompt="maker words")
        prompt_memory.put(asset_id, self.owner["id"], "owner words", kept_until=None, path=self.prompt_db)
        self._receipt(owner_app["id"], "req_owner")
        self._job("req_owner", asset_id)
        self.current_user = self.owner
        own = self.client.get("/me/api/media")
        self.assertEqual(own.status_code, 200, own.text)
        asset = next(item for item in own.json()["assets"] if item["asset_id"] == asset_id)
        self.assertEqual(asset["prompt"], "owner words")
        self.assertEqual(asset["title"], "owner words")

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
        for removed in ("/me/api/media?source=local-network", "/me/api/usage/summary?source=local-network"):
            self.assertEqual(self.client.get(removed).status_code, 400, removed)
        self.assertEqual(self.client.get(f"/me/api/media?source=app:{self.app_b['id']}").status_code, 403)
        self.assertEqual(self.client.get(f"/me/api/usage/summary?source=app:{self.app_b['id']}").status_code, 403)

        self.current_user = self.owner
        all_media = self.client.get("/me/api/media?scope=everyone")
        self.assertEqual(all_media.status_code, 200, all_media.text)
        source_ids = {source["id"] for source in all_media.json()["sources"]}
        self.assertNotIn("local-network", source_ids)
        self.assertIn(f"app:{self.service_app['id']}", source_ids)
        service = self.client.get(f"/me/api/media?scope=everyone&source=app:{self.service_app['id']}")
        self.assertEqual([item["asset_id"] for item in service.json()["assets"]], [service_asset])
        self.assertEqual(self.client.get("/me/api/media?scope=everyone&source=local-network").status_code, 400)
        service_usage = self.client.get(f"/me/api/usage/summary?person=all&source=app:{self.service_app['id']}")
        self.assertEqual(service_usage.status_code, 200, service_usage.text)
        self.assertEqual(service_usage.json()["media_count"], 1)
        self.assertEqual(self.client.get("/me/api/usage/summary?person=all&source=local-network").status_code, 400)

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
        owner_usage = self.client.get("/me/api/usage/summary?person=all")
        self.assertEqual(owner_usage.status_code, 200, owner_usage.text)
        self.assertEqual(owner_usage.json()["media_count"], 1)

    def test_job_with_released_request_id_keeps_its_receipt_link(self) -> None:
        # After a replay release (or erasure of a seeded ID) only the creating request's ID remains.
        self._receipt(self.app_a["id"], "req_parent")
        media_jobs.create(media_jobs.MediaJob(
            request_id="", parent_request_id="req_parent", intent="image.generate",
            status=media_jobs.STATUS_SUCCEEDED, assets=[{"asset_id": "img_" + "7" * 64}],
        ), path=self.jobs_db)
        assets = self.client.get("/me/api/media").json()["assets"]
        self.assertEqual([item["asset_id"] for item in assets], ["img_" + "7" * 64])
        self.assertEqual(assets[0]["model"], "local-image")
        self.assertEqual(assets[0]["source"]["app_id"], self.app_a["id"])
        self.assertEqual(self.client.get("/me/api/usage/summary").json()["media_count"], 1)
        self.current_user = self.bob
        self.assertEqual(self.client.get("/me/api/media").json()["assets"], [])
        self.assertEqual(self.client.get("/me/api/usage/summary").json()["media_count"], 0)

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
                "source_kind": "local-network",
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
        owner_media = self.client.get("/me/api/media?scope=everyone")
        self.assertEqual(owner_media.status_code, 200, owner_media.text)
        sources = {item["asset_id"]: item["source"] for item in owner_media.json()["assets"]}
        self.assertEqual(sources[keyed_asset]["label"], "Alice")
        self.assertEqual(sources[keyed_asset]["recovered_from"], "asset_owner_key")
        self.assertEqual(sources[service_asset]["label"], "Daisy")
        self.assertEqual(sources[service_asset]["recovered_from"], "media_job")
        usage = self.client.get(f"/me/api/usage/summary?person=all&source=app:{self.app_a['id']}")
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

    def test_usage_failed_requests_counts_period_before_receipt_limit(self) -> None:
        now = datetime.now(timezone.utc)
        start = int(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
        for index, status in enumerate(("failed", "refused", "timed_out", "error", "backend_failed", "rejected")):
            self._receipt(self.app_a["id"], f"req_failure_{index}", status=status, created_at=start)
        self._receipt(self.app_a["id"], "req_old_failure", status="failed", created_at=start - 1)
        for index in range(55):
            self._receipt(self.app_a["id"], f"req_success_{index}", created_at=start + 1)

        response = self.client.get("/me/api/usage/summary")
        self.assertEqual(response.status_code, 200, response.text)
        summary = response.json()
        self.assertEqual(summary["requests"], 61)
        self.assertEqual(summary["failed_requests"], 6)
        self.assertEqual(len(summary["receipts"]), 50)
        self.assertTrue(all(row["status"] == "succeeded" for row in summary["receipts"]))

    def test_usage_failed_requests_respects_member_owner_person_and_source(self) -> None:
        self._receipt(self.app_a["id"], "req_alice_failed", status="failed")
        self._receipt(None, "req_alice_key_failed", status="timed_out", owner_key_hash="sha256:alice")
        self._receipt(self.app_b["id"], "req_bob_failed", status="refused")
        self._receipt(self.app_b["id"], "req_conflicting_failed", status="failed", owner_key_hash="sha256:alice")
        self._receipt(None, "req_network_failed", status="timed_out")
        self.assertEqual(self.client.get("/me/api/usage/summary").json()["failed_requests"], 2)
        self.assertEqual(self.client.get(f"/me/api/usage/summary?person={self.bob['id']}").status_code, 403)
        self.current_user = self.owner
        self.assertEqual(self.client.get("/me/api/usage/summary?person=all").json()["failed_requests"], 5)
        self.assertEqual(self.client.get("/me/api/usage/summary?person=all").json()["failed_requests"], 5)
        self.assertEqual(self.client.get("/me/api/usage/summary?person=self").json()["failed_requests"], 0)
        self.assertEqual(self.client.get(f"/me/api/usage/summary?person={self.alice['id']}").json()["failed_requests"], 2)
        self.assertEqual(self.client.get(f"/me/api/usage/summary?person=all&source=app:{self.app_b['id']}").json()["failed_requests"], 2)

    def test_usage_failed_requests_is_null_when_outcomes_are_not_computable(self) -> None:
        self._receipt(self.app_a["id"], "req_known_failure", status="failed")
        self._receipt(self.app_a["id"], "req_unknown_outcome")
        for status in (None, "", "incomplete", "stale_incomplete", "unrecognized"):
            with self.subTest(status=status):
                with sqlite3.connect(str(self.receipts_db)) as conn:
                    conn.execute(
                        "UPDATE receipts SET receipt_json = json_set(receipt_json, '$.status', ?) WHERE request_id = ?",
                        (status, "req_unknown_outcome"),
                    )
                response = self.client.get("/me/api/usage/summary")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["requests"], 2)
                self.assertIsNone(response.json()["failed_requests"])
        with sqlite3.connect(str(self.receipts_db)) as conn:
            conn.execute(
                "UPDATE receipts SET receipt_json = json_remove(receipt_json, '$.status') WHERE request_id = ?",
                ("req_unknown_outcome",),
            )
        self.assertIsNone(self.client.get("/me/api/usage/summary").json()["failed_requests"])
        self.current_user = self.bob
        self.assertEqual(self.client.get("/me/api/usage/summary").json()["failed_requests"], 0)

    def test_usage_failed_requests_is_zero_for_known_nonfailed_outcomes(self) -> None:
        for index, status in enumerate(("succeeded", "success", "ok", "accepted", "running", "in_progress", "cancelled", "disconnected")):
            self._receipt(self.app_a["id"], f"req_nonfailure_{index}", status=status)
        response = self.client.get("/me/api/usage/summary")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["failed_requests"], 0)

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
        self.assertEqual(self.client.get("/me/api/usage/summary").json()["scope"], "me")
        instance_usage = self.client.get("/me/api/usage/summary?person=all")
        self.assertEqual(instance_usage.status_code, 200, instance_usage.text)
        self.assertEqual(instance_usage.json()["requests"], 4)
        self.assertEqual(instance_usage.json()["remote_spend"], 4.0)
        self.assertNotIn("local-network", {source["id"] for source in instance_usage.json()["sources"]})
        self.assertNotIn("local-network", {person["id"] for person in instance_usage.json()["people"]})
        self.assertIn(f"app:{self.service_app['id']}", {source["id"] for source in instance_usage.json()["sources"]})
        self.assertEqual(
            self.client.get(f"/me/api/usage/summary?person=all&source=app:{self.service_app['id']}").json()["requests"], 1
        )
        everyone = self.client.get("/me/api/usage/summary?person=all")
        self.assertEqual(everyone.status_code, 200, everyone.text)
        self.assertEqual(everyone.json()["requests"], 4)
        self.assertEqual(everyone.json()["remote_spend"], 4.0)

    def _connect(self, person: dict, client_name: str = "ChatGPT", *, revoke: bool = False):
        """One MCP sign-in connection of ``person``, and the session App it runs as."""
        store = mcp_oauth_store.Store(lambda: self.admin_db)
        client = store.put_client(
            client_id=f"client_{client_name}_{person['id']}", kind="registered", name=client_name,
            redirect_uris=["https://client.example/cb"], grant_types=["authorization_code"],
            auth_method="none", secret_hash=None, metadata={})
        grant = store.create_grant(person_id=person["id"], client=client, redirect_uri="https://client.example/cb",
                                   resource="https://gw.test/mcp", scope="mcp", session=None)
        if revoke:
            store.revoke_grant(grant.id, "person_revoked")
        session = admin_store.ensure_session_app_for_user(person["id"], name="Azizam", path=self.admin_db)
        return grant, session

    def test_a_connection_is_its_own_usage_source_apart_from_the_persons_browser(self) -> None:
        grant, session = self._connect(self.alice)
        self._receipt(session["id"], "req_browser", api_key_name="Azizam")
        for index in range(3):
            self._receipt(session["id"], f"req_chatgpt_{index}", api_key_name="ChatGPT (MCP)", connection_id=grant.id)
        usage = self.client.get("/me/api/usage/summary").json()
        by_id = {source["id"]: source for source in usage["sources"]}
        connection = by_id[f"connection:{grant.id}"]
        self.assertEqual((connection["label"], connection["kind"], connection["owner_user_id"], connection["requests"]),
                         ("ChatGPT", "connection", self.alice["id"], 3))
        self.assertEqual(by_id[f"app:{session['id']}"]["requests"], 1)
        self.assertEqual(usage["requests"], 4)
        labels = {row["request_id"]: row["source_label"] for row in usage["receipts"]}
        self.assertEqual(labels["req_chatgpt_0"], "ChatGPT")
        self.assertEqual(labels["req_browser"], "Azizam")
        # Filtering by the connection shows only its requests; by the App, only the browser's.
        only_connection = self.client.get(f"/me/api/usage/summary?source=connection:{grant.id}").json()
        self.assertEqual(only_connection["requests"], 3)
        self.assertEqual({row["source"]["kind"] for row in only_connection["receipts"]}, {"connection"})
        only_app = self.client.get(f"/me/api/usage/summary?source=app:{session['id']}").json()
        self.assertEqual([row["request_id"] for row in only_app["receipts"]], ["req_browser"])

    def test_connections_belong_to_the_person_who_connected_them(self) -> None:
        alice_grant, alice_session = self._connect(self.alice)
        bob_grant, bob_session = self._connect(self.bob, "Claude")
        self._receipt(alice_session["id"], "req_alice", api_key_name="ChatGPT (MCP)", connection_id=alice_grant.id)
        self._receipt(bob_session["id"], "req_bob", api_key_name="Claude (MCP)", connection_id=bob_grant.id)
        mine = self.client.get("/me/api/usage/summary").json()
        self.assertEqual([s["id"] for s in mine["sources"] if s["kind"] == "connection"],
                         [f"connection:{alice_grant.id}"])
        self.assertEqual(self.client.get(f"/me/api/usage/summary?source=connection:{bob_grant.id}").status_code, 403)
        self.current_user = self.owner
        everyone = self.client.get("/me/api/usage/summary?person=all").json()
        self.assertEqual({s["id"]: s["owner_user_id"] for s in everyone["sources"] if s["kind"] == "connection"},
                         {f"connection:{alice_grant.id}": self.alice["id"], f"connection:{bob_grant.id}": self.bob["id"]})
        bobs = self.client.get(f"/me/api/usage/summary?person=all&source=connection:{bob_grant.id}").json()
        self.assertEqual([row["request_id"] for row in bobs["receipts"]], ["req_bob"])
        # An owner looking at one person does not see another person's connection requests there.
        alices = self.client.get(f"/me/api/usage/summary?person={self.alice['id']}&source=connection:{bob_grant.id}")
        self.assertEqual(alices.status_code, 403)

    def test_a_disconnected_connection_stays_listed_only_while_it_has_requests(self) -> None:
        ended, session = self._connect(self.alice, "Old client", revoke=True)
        used, _session = self._connect(self.alice, "Used client", revoke=True)
        self._receipt(session["id"], "req_used", api_key_name="Used client (MCP)", connection_id=used.id)
        sources = {s["id"]: s for s in self.client.get("/me/api/usage/summary").json()["sources"]}
        self.assertNotIn(f"connection:{ended.id}", sources)
        self.assertTrue(sources[f"connection:{used.id}"]["disabled"])
        self.assertEqual(sources[f"connection:{used.id}"]["requests"], 1)

    def test_keyless_local_network_is_no_longer_a_source_or_person(self) -> None:
        self._receipt(None, "req_unattributed")
        self.current_user = self.owner
        for path in ("/me/api/usage/summary?person=all", "/me/api/media?scope=everyone"):
            body = self.client.get(path).json()
            self.assertNotIn("local-network", {source["id"] for source in body["sources"]}, path)
            self.assertNotIn("Local network · no key", json.dumps(body["sources"]), path)
        self.assertNotIn("local-network", {p["id"] for p in self.client.get("/me/api/usage/summary").json()["people"]})
        self.assertEqual(self.client.get("/me/api/usage/summary?source=local-network").status_code, 400)

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
                scopes=("openid", "email"), admin_emails=(),
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
        detail = self.client.get(f"/me/api/media/{asset_id}?scope=everyone")
        self.assertEqual(detail.status_code, 200)
        self.assertIsNone(detail.json()["asset"]["prompt"])
        self.assertIsNone(detail.json()["asset"]["workflow_download_url"])
        self.assertEqual(self.client.get(f"/me/api/media/{asset_id}/download?scope=everyone&metadata=full").status_code, 403)
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
        # One transaction for the churn: a commit per row made this test take
        # minutes on a busy disk without changing the rows it writes.
        with admin_store._conn(self.admin_db) as conn:
            for index in range(1001):
                admin_store._record_audit_in_transaction(
                    conn, "media.asset.delete", actor_kind="user", actor_id=self.bob["id"],
                    actor_label=None, target_kind=None, target_id=f"other-{index}",
                    metadata=None, now=admin_store._now())
            conn.commit()
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
        self.assertIsNone(usage["retention_days"])
        self.assertIn("coverage_start", usage)
        self.assertEqual(usage["people"], [])

    def test_usage_history_floor_marks_pruned_upgrade_as_partial(self) -> None:
        now = int(datetime(2026, 10, 25, 12, tzinfo=timezone.utc).timestamp())
        earliest = now - 14 * 86400
        self.receipts_db.unlink()
        with sqlite3.connect(self.receipts_db) as conn:
            conn.executescript(receipt_store._SCHEMA)
            conn.execute("INSERT INTO schema_version VALUES (2)")
        receipt_store.record({"request_id": "survivor", "app_id": self.app_a["id"]},
                             created_at=earliest, path=self.receipts_db)
        with patch.object(receipt_store.time, "time", return_value=now), patch.object(me_media, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.fromtimestamp(now, timezone.utc)
            receipt_store.init_db(self.receipts_db)
            for days in (30, 90, 365, None):
                admin_store.put_operator_setting(receipt_store.RETENTION_SETTING_KEY,
                                                {"retention_days": days}, path=self.admin_db)
                response = self.client.get("/me/api/usage/summary")
                self.assertEqual(response.status_code, 200)
                usage = response.json()
                self.assertEqual(usage["coverage_start"], datetime.fromtimestamp(earliest, timezone.utc).isoformat())
                self.assertIn("partial", usage["coverage_note"].lower())
                self.assertEqual(usage["requests"], 1)
            next_month = int(datetime(2026, 11, 5, tzinfo=timezone.utc).timestamp())
            with patch.object(receipt_store.time, "time", return_value=next_month):
                clock.now.return_value = datetime.fromtimestamp(next_month, timezone.utc)
                usage = self.client.get("/me/api/usage/summary").json()
                self.assertEqual(usage["coverage_start"], "2026-11-01T00:00:00+00:00")
                self.assertNotIn("partial", usage["coverage_note"].lower())

    def test_usage_fresh_install_coverage_starts_at_install(self) -> None:
        self.receipts_db.unlink()
        now = int(datetime(2026, 10, 25, 12, tzinfo=timezone.utc).timestamp())
        with patch.object(receipt_store.time, "time", return_value=now), patch.object(me_media, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.fromtimestamp(now, timezone.utc)
            receipt_store.init_db(self.receipts_db)
            usage = self.client.get("/me/api/usage/summary").json()
        self.assertEqual(usage["coverage_start"], "2026-10-25T12:00:00+00:00")
        self.assertIn("partial", usage["coverage_note"].lower())

    def test_usage_retention_increase_keeps_deleted_history_partial(self) -> None:
        self.receipts_db.unlink()
        now = int(datetime(2026, 10, 31, 12, tzinfo=timezone.utc).timestamp())
        installed = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
        with patch.object(receipt_store.time, "time", return_value=installed):
            receipt_store.init_db(self.receipts_db)
        self._receipt(self.app_a["id"], "deleted", created_at=now - 31 * 86400)
        self._receipt(self.app_a["id"], "kept", created_at=now)
        with patch.object(receipt_store.time, "time", return_value=now), patch.object(me_media, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.fromtimestamp(now, timezone.utc)
            preview = receipt_store.retention_preview(30, path=self.receipts_db)
            deleted = receipt_store.save_retention(30, preview,
                lambda: admin_store.put_operator_setting(receipt_store.RETENTION_SETTING_KEY,
                    {"retention_days": 30}, path=self.admin_db), path=self.receipts_db)
            self.assertEqual(deleted, 1)
            for days in (90, 365, None):
                receipt_store.save_retention(days, receipt_store.retention_preview(days, path=self.receipts_db),
                    lambda: admin_store.put_operator_setting(receipt_store.RETENTION_SETTING_KEY,
                        {"retention_days": days}, path=self.admin_db), path=self.receipts_db)
                usage = self.client.get("/me/api/usage/summary").json()
                self.assertEqual(usage["coverage_start"], "2026-10-01T12:00:00+00:00")
                self.assertIn("partial", usage["coverage_note"].lower())
                self.assertEqual(usage["requests"], 1)

    def test_usage_reports_saved_retention_without_changing_member_scope(self) -> None:
        self._receipt(self.app_a["id"], "req_mine")
        self._receipt(self.app_b["id"], "req_theirs")
        for days in (365, 90, 30, None):
            admin_store.put_operator_setting(receipt_store.RETENTION_SETTING_KEY,
                                            {"retention_days": days}, path=self.admin_db)
            usage = self.client.get("/me/api/usage/summary").json()
            self.assertEqual(usage["retention_days"], days)
            self.assertEqual(usage["requests"], 1)
            self.assertEqual([r["request_id"] for r in usage["receipts"]], ["req_mine"])

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
        # Text to speech reads as its own task, never as music or sounds.
        self.assertEqual(me_media._capability({"intent": "audio.speech"}), "voice")
        self.assertEqual(me_media._capability({"intent": "speech.synthesis"}), "voice")
        self.assertEqual(me_media._capability({"path": "/v1/audio/speech"}), "voice")
        self.assertEqual(me_media._capability({"intent": "audio.music"}), "audio")
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

    def test_usage_model_rows_hide_host_paths_and_count_paid_requests(self) -> None:
        gguf = "/media/windows/AI Models/LLMs/GGUF/Qwen3.8-27B-Uncensored-Q4_K_M.gguf"
        self._receipt(self.app_a["id"], "req_local_path", intent="chat.completions",
                      route_path="/v1/chat/completions", requested_model="default", resolved_model=gguf)
        self._receipt(self.app_a["id"], "req_paid_unknown", intent="chat.completions",
                      route_path="/v1/chat/completions", requested_model="remote-chat",
                      resolved_model="remote-chat", cost_class="paid-remote")
        usage = self.client.get("/me/api/usage/summary")
        self.assertEqual(usage.status_code, 200, usage.text)
        body = usage.json()
        rows = {row["model"]: row for row in body["by_model"]}
        self.assertIn("Qwen3.8-27B-Uncensored-Q4_K_M.gguf", rows)
        self.assertNotIn(gguf, rows)
        self.assertNotIn("/media/", json.dumps(body))
        self.assertEqual(rows["Qwen3.8-27B-Uncensored-Q4_K_M.gguf"]["paid_requests"], 0)
        self.assertEqual(rows["remote-chat"]["unknown_paid_requests"], 1)

    def test_usage_counts_speech_as_voice_apart_from_music_and_sounds(self) -> None:
        # Usage names rows by task: speech is "Read text aloud", not "Make music or sounds".
        self._receipt(self.app_a["id"], "req_speech", intent="audio.speech",
                      route_path="/v1/audio/speech", requested_model="voice", resolved_model="voice")
        self._receipt(self.app_a["id"], "req_speech_path", intent="",
                      route_path="/v1/audio/speech", requested_model="tts-1", resolved_model="tts-1")
        self._receipt(self.app_a["id"], "req_music", intent="audio.music",
                      route_path="/v1/audio/music", requested_model="music", resolved_model="music")
        usage = self.client.get("/me/api/usage/summary")
        self.assertEqual(usage.status_code, 200, usage.text)
        rows = {row["model"]: row["capability"] for row in usage.json()["by_model"]}
        self.assertEqual(rows["voice"], "voice")
        self.assertEqual(rows["tts-1"], "voice")
        self.assertEqual(rows["music"], "audio")
        day = usage.json()["by_day"][-1]
        self.assertEqual(day.get("voice"), 2)
        self.assertEqual(day.get("audio"), 1)

    def test_usage_sources_carry_request_counts_and_disabled_state(self) -> None:
        old = admin_store.create_app(
            "Alice", kind="personal", owner_user_id=self.alice["id"], access_plan="person", path=self.admin_db
        )
        admin_store.disable_app(old["id"], path=self.admin_db)
        self._receipt(self.app_a["id"], "req_src_1")
        self._receipt(self.app_a["id"], "req_src_2")
        self.current_user = self.owner
        usage = self.client.get(f"/me/api/usage/summary?person=all&source=app:{self.app_b['id']}")
        self.assertEqual(usage.status_code, 200, usage.text)
        sources = {row["id"]: row for row in usage.json()["sources"]}
        # Counts cover the whole scope, not just the selected source.
        self.assertEqual(sources[f"app:{self.app_a['id']}"]["requests"], 2)
        self.assertEqual(sources[f"app:{self.app_b['id']}"]["requests"], 0)
        self.assertTrue(sources[f"app:{old['id']}"]["disabled"])
        self.assertFalse(sources[f"app:{self.app_a['id']}"]["disabled"])
        self.assertIsNotNone(sources[f"app:{old['id']}"]["created_at"])


# ---------------------------------------------------------------------------
# Session make API acceptance contract.
# Run with pytest tests/test_me_media.py -k MeMakeApiAcceptanceTests, or
# python -m unittest tests.test_me_media.MeMakeApiAcceptanceTests.
#
# Wire contract: POST/GET /me/api/make/jobs; GET /{job_id};
# POST /{job_id}/cancel. POST JSON: intent, model, prompt, optional
# source_asset_id/source_job_id, and the existing prompt-memory extension
# extensions["com.azizam.prompts"] = {"mode": "forget"}. Responses:
# {"job": {...}} / {"jobs": [...]}. First submissions return 202.
#
# Execution runs behind the router's make runtime. The fake runtime below is
# the backend boundary only: the job lifecycle (running, succeeded, failed,
# cancel, events, errors) is the code under test, so no test writes job
# status or job assets itself.
# ---------------------------------------------------------------------------


class _GatedMakeRuntime:
    """Deterministic stand-in for the gateway's media backend.

    Jobs run on their own threads, so they outlive the request (and the
    request's event loop) that scheduled them. ``release`` holds every
    backend call until a test lets it go.
    """

    def __init__(self, asset_path) -> None:
        import threading

        self._asset_path = asset_path
        self.payloads: dict[str, dict] = {}
        self.calls: list[dict] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.fail_with: BaseException | None = None
        self.threads: list = []
        self.scheduled: set[str] = set()
        self.limits = me_make.MakeLimits()
        self.canceled: set[str] = set()
        self.autostart = True
        self.pending: list = []
        self.refusal = None  # The operator's admission refusal, if any.

    def keep_payload(self, job_id: str, payload) -> None:
        self.payloads[job_id] = json.loads(json.dumps(payload))

    def load_payload(self, job_id: str):
        return self.payloads.get(job_id)

    def drop_payload(self, job_id: str) -> None:
        self.payloads.pop(job_id, None)

    def request_cancel(self, job_id: str) -> bool:
        self.canceled.add(job_id)
        return True

    def admission(self, intent: str):
        return self.refusal

    def owns_job(self, job_id: str) -> bool:
        return job_id in self.scheduled

    def schedule(self, job_id: str, run) -> None:
        self.scheduled.add(job_id)
        import asyncio
        import threading

        if not self.autostart:  # Stays queued until the test runs it.
            self.pending.append(run)
            return
        thread = threading.Thread(target=lambda: asyncio.run(run()), daemon=True)
        self.threads.append(thread)
        thread.start()

    async def execute(self, payload):
        import asyncio
        import hashlib

        self.calls.append(json.loads(json.dumps(payload)))
        self.started.set()
        if not await asyncio.to_thread(self.release.wait, 10):
            # Completing here would race the lifecycle assertions of a held job.
            raise AssertionError("the test never released this held make job")
        if payload["job_id"] in self.canceled:
            raise me_make.MakeCanceled()
        if self.fail_with is not None:
            raise self.fail_with
        output = "img_" + hashlib.sha256(payload["job_id"].encode()).hexdigest()
        self._asset_path(output).write_bytes(PNG_1X1)
        return [{"asset_id": output, "media_type": "image/png"}]

    def wait_idle(self, timeout: float = 10) -> None:
        for thread in list(self.threads):
            thread.join(timeout)
            if thread.is_alive():
                raise AssertionError("make job thread did not finish")


FORGET = {"com.azizam.prompts": {"mode": "forget"}}


class MeMakeApiAcceptanceTests(unittest.TestCase):
    # Reuse the exact existing session/storage fixtures without inheriting
    # (and therefore rerunning) every existing test method.
    _session = MeMediaRouterTests._session
    _mutation_validator = MeMediaRouterTests._mutation_validator
    _asset_path = MeMediaRouterTests._asset_path
    _meta_path = MeMediaRouterTests._meta_path
    _asset_metadata = MeMediaRouterTests._asset_metadata
    _catalog = MeMediaRouterTests._catalog
    _delete_unshared = MeMediaRouterTests._delete_unshared
    _write_asset = MeMediaRouterTests._write_asset

    make_url = "/me/api/make/jobs"

    def setUp(self) -> None:
        MeMediaRouterTests.setUp(self)
        self.runtime = _GatedMakeRuntime(self._asset_path)
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
                make_runtime=self.runtime,
            )
        )
        self.client = TestClient(app)

    def tearDown(self) -> None:
        # Let held jobs finish before their temporary stores disappear.
        self.runtime.release.set()
        self.runtime.wait_idle()
        MeMediaRouterTests.tearDown(self)

    def _make_headers(self, key: str) -> dict:
        return {
            "Idempotency-Key": key,
            "x-azizam-user-action": "1",
            "x-azizam-csrf": "valid-csrf",
        }

    def _picture(self, **overrides) -> dict:
        body = {"intent": "image.generate", "model": "image",
                "prompt": "a blue bird on a yellow branch"}
        body.update(overrides)
        return body

    def _submit_make(self, key: str, **overrides):
        response = self.client.post(
            self.make_url, json=self._picture(**overrides),
            headers=self._make_headers(key),
        )
        self.assertNotIn("authorization", response.request.headers)
        self.assertIn(response.status_code, (200, 202), response.text)
        job = response.json()["job"]
        self.assertTrue(job["job_id"].startswith("mj_"), job)
        self.assertIsNotNone(media_jobs.get(job["job_id"], path=self.jobs_db))
        return response, job

    def _finished(self, job_id: str) -> dict:
        self.runtime.wait_idle()
        stored = media_jobs.get(job_id, path=self.jobs_db)
        self.assertIn(stored["status"], media_jobs.TERMINAL_STATUSES, stored)
        return stored

    def _completed_output(self, key: str, **overrides) -> tuple[dict, str]:
        _, job = self._submit_make(key, **overrides)
        stored = self._finished(job["job_id"])
        self.assertEqual(stored["status"], media_jobs.STATUS_SUCCEEDED, stored)
        return job, stored["assets"][0]["asset_id"]

    def _make_list(self, **params) -> list:
        response = self.client.get(self.make_url, params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["jobs"]

    def _as(self, user):
        test = self

        class _Switch:
            def __enter__(self):
                self.previous = test.current_user
                test.current_user = user

            def __exit__(self, *exc):
                test.current_user = self.previous

        return _Switch()

    def _assert_discloses_nothing(self, response, *canaries: str) -> None:
        wire = response.text + json.dumps(dict(response.headers))
        for canary in canaries:
            self.assertNotIn(canary, wire)

    def test_make_member_picture_has_person_and_azizam_receipt(self) -> None:
        # Criterion 1: neither fixture-created App is named Azizam.
        self.assertFalse(any(
            app["name"] == "Azizam"
            for app in admin_store.list_apps(path=self.admin_db)
        ))
        response, job = self._submit_make("alice-first-picture")
        self.assertEqual(response.status_code, 202, response.text)
        stored = media_jobs.get(job["job_id"], path=self.jobs_db)
        self.assertEqual(stored["intent"], "image.generate")
        receipt = receipt_store.get(
            stored["request_id"], audience="internal", path=self.receipts_db,
        )
        self.assertIsNotNone(receipt, "Submission must persist a route receipt")
        self.assertEqual(receipt["app_id"], stored["app_id"])
        app = admin_store.get_app(receipt["app_id"], path=self.admin_db)
        self.assertEqual(app["name"], "Azizam")
        self.assertEqual(app["kind"], "personal")
        self.assertEqual(app["owner_user_id"], self.alice["id"])
        # The Azizam App is never a key: no credential is ever attached to it.
        self.assertEqual(admin_store.list_active_credentials(
            subject_kind="app", app_id=app["id"], path=self.admin_db), [])
        own = self.client.get(f"{self.make_url}/{job['job_id']}")
        self.assertEqual(own.status_code, 200, own.text)
        self.assertEqual(own.json()["job"]["job_id"], job["job_id"])
        # The backend runs as the session person, through the Azizam App.
        self._finished(job["job_id"])
        self.assertEqual(self.runtime.calls[0]["auth"],
                         {"app_id": app["id"], "person_id": self.alice["id"]})

        _, second = self._submit_make("alice-second-picture")
        self.assertEqual(
            media_jobs.get(second["job_id"], path=self.jobs_db)["app_id"],
            app["id"], "Repeated use must reuse the person's Azizam App",
        )
        with self._as(self.bob):
            _, bob = self._submit_make("bob-first-picture")
        bob_app = admin_store.get_app(
            media_jobs.get(bob["job_id"], path=self.jobs_db)["app_id"],
            path=self.admin_db,
        )
        self.assertEqual(bob_app["name"], "Azizam")
        self.assertEqual(bob_app["owner_user_id"], self.bob["id"])
        self.assertNotEqual(bob_app["id"], app["id"])

    def test_make_idempotency_reuses_job_only_for_same_person_and_key(self) -> None:
        # Criterion 2, also exercising own list and the optional status filter.
        _, first = self._submit_make("same-key")
        _, again = self._submit_make("same-key")
        _, different = self._submit_make("different-key")
        self.assertEqual(first["job_id"], again["job_id"])
        self.assertNotEqual(first["job_id"], different["job_id"])
        self.assertEqual(media_jobs.count(path=self.jobs_db), 2)
        self.runtime.wait_idle()
        self.runtime.release.clear()
        self.runtime.started.clear()
        _, held = self._submit_make("held-key")
        self.assertTrue(self.runtime.started.wait(5))
        own_ids = {job["job_id"] for job in self._make_list()}
        self.assertEqual(own_ids, {first["job_id"], different["job_id"], held["job_id"]})
        running = self._make_list(status=media_jobs.STATUS_RUNNING)
        self.assertEqual([job["job_id"] for job in running], [held["job_id"]])
        done = self._make_list(status=media_jobs.STATUS_SUCCEEDED)
        self.assertEqual({job["job_id"] for job in done}, {first["job_id"], different["job_id"]})
        self.runtime.release.set()
        with self._as(self.bob):
            _, bob = self._submit_make("same-key")
            self.assertNotIn(bob["job_id"], own_ids)
            self.assertEqual([job["job_id"] for job in self._make_list()], [bob["job_id"]])

    def test_make_changed_input_conflicts_before_capacity_check(self) -> None:
        self.runtime.autostart = False
        self.runtime.limits = me_make.MakeLimits(per_person=1, household=1)
        _, first = self._submit_make("same-key")
        for changed in ({"prompt": "a different bird"}, {"model": "other-image"}):
            self._refused("same-key", 409, "idempotency_conflict", **changed)
        _, retry = self._submit_make("same-key")
        self.assertEqual(retry["job_id"], first["job_id"])
        self.assertEqual(len(self.runtime.pending), 1)

    def test_make_parallel_conflicts_and_identical_retries(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        import threading

        self.runtime.autostart = False
        for different in (True, False):
            with self.subTest(different=different):
                barrier = threading.Barrier(2)
                original = media_jobs.create

                def synchronized_create(*args, **kwargs):
                    barrier.wait(timeout=10)
                    return original(*args, **kwargs)

                key = f"parallel-{different}"
                bodies = [self._picture(), self._picture(prompt="different") if different else self._picture()]

                def submit(body):
                    # Separate ASGI portals let both synchronous creates reach
                    # the barrier; no backend or socket is involved.
                    with TestClient(self.client.app, raise_server_exceptions=False) as client:
                        return client.post(self.make_url, json=body, headers=self._make_headers(key))

                with patch.object(media_jobs, "create", synchronized_create), ThreadPoolExecutor(max_workers=2) as pool:
                    responses = list(pool.map(submit, bodies))
                if different:
                    self.assertEqual(sorted(r.status_code for r in responses), [202, 409])
                    conflict = next(r for r in responses if r.status_code == 409)
                    self.assertEqual(conflict.json()["error"]["code"], "idempotency_conflict")
                else:
                    self.assertEqual([r.status_code for r in responses], [202, 202])
                    self.assertEqual(responses[0].json()["job"]["job_id"], responses[1].json()["job"]["job_id"])
                winner_index = next(i for i, response in enumerate(responses) if response.status_code == 202)
                winner = responses[winner_index].json()["job"]["job_id"]
                retry = self.client.post(self.make_url, json=bodies[winner_index],
                                         headers=self._make_headers(key))
                self.assertEqual(retry.json()["job"]["job_id"], winner)
        self.assertEqual(media_jobs.count(path=self.jobs_db), 2)
        self.assertEqual(len(self.runtime.pending), 2)

    def test_make_replay_refuses_missing_or_erased_proof(self) -> None:
        self.runtime.autostart = False
        for field in ("owner_scope", "input_hash"):
            _, job = self._submit_make(field)
            with sqlite3.connect(self.jobs_db) as conn:
                stored = json.loads(conn.execute("SELECT job_json FROM media_jobs WHERE job_id=?",
                                                (job["job_id"],)).fetchone()[0])
                stored.pop(field)
                conn.execute("UPDATE media_jobs SET job_json=? WHERE job_id=?", (json.dumps(stored), job["job_id"]))
            self._refused(field, 409, "idempotency_conflict")

    def test_make_cannot_read_another_person_job(self) -> None:
        # Criterion 3: job read isolation; the denial discloses nothing.
        canary = "bob-private-canary-5d1f"
        with self._as(self.bob):
            foreign, foreign_asset = self._completed_output("bob-private-job", prompt=canary)
        _, own = self._submit_make("alice-visible")
        response = self.client.get(f"{self.make_url}/{foreign['job_id']}")
        self.assertIn(response.status_code, (403, 404), response.text)
        self._assert_discloses_nothing(response, canary, foreign_asset, "/v1/assets/")
        listed = self.client.get(self.make_url)
        self.assertEqual([job["job_id"] for job in listed.json()["jobs"]], [own["job_id"]])
        self._assert_discloses_nothing(listed, canary, foreign_asset, foreign["job_id"])

    def test_make_cannot_cancel_another_person_job(self) -> None:
        # Criterion 3: a running foreign job is untouched and undisclosed.
        canary = "bob-running-canary-77aa"
        self.runtime.release.clear()
        with self._as(self.bob):
            _, foreign = self._submit_make("bob-running", prompt=canary)
        self.assertTrue(self.runtime.started.wait(5))
        before = media_jobs.get(foreign["job_id"], path=self.jobs_db)
        self.assertEqual(before["status"], media_jobs.STATUS_RUNNING)
        response = self.client.post(
            f"{self.make_url}/{foreign['job_id']}/cancel",
            headers=self._make_headers("forbidden-cancel"),
        )
        self.assertIn(response.status_code, (403, 404), response.text)
        self._assert_discloses_nothing(response, canary)
        self.assertEqual(media_jobs.get(foreign["job_id"], path=self.jobs_db), before)
        self.runtime.release.set()
        self.assertEqual(self._finished(foreign["job_id"])["status"], media_jobs.STATUS_SUCCEEDED)

    def test_make_owner_can_cancel_own_running_job(self) -> None:
        # A running job's work may not stop at once, so the job says
        # cancel_requested until it settles, then canceled with no output.
        self.runtime.release.clear()
        _, job = self._submit_make("cancel-mine")
        self.assertTrue(self.runtime.started.wait(5))
        url = f"{self.make_url}/{job['job_id']}"
        response = self.client.post(f"{url}/cancel", headers=self._make_headers("cancel-mine"))
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["job"]["status"], me_make.CANCEL_REQUESTED)
        self.assertEqual(self.runtime.payloads, {}, "the prompt is not kept for a canceled job")
        self.assertEqual(self.client.get(url).json()["job"]["status"], me_make.CANCEL_REQUESTED)
        self.assertEqual([item["status"] for item in self._make_list()], [me_make.CANCEL_REQUESTED])
        self.runtime.release.set()
        stored = self._finished(job["job_id"])
        self.assertEqual(stored["status"], media_jobs.STATUS_CANCELED)
        self.assertEqual(stored["assets"], [])
        self.assertEqual(self.client.get(url).json()["job"]["status"], media_jobs.STATUS_CANCELED)
        library = self.client.get("/me/api/media").json()
        self.assertNotIn(job["job_id"], json.dumps(library))
        finished = self.client.post(f"{url}/cancel", headers=self._make_headers("cancel-mine"))
        self.assertEqual(finished.status_code, 409, finished.text)

    def test_make_restart_reports_unconfirmed_cancel(self) -> None:
        self.runtime.release.clear()
        _, job = self._submit_make("cancel-then-restart")
        self.assertTrue(self.runtime.started.wait(5))
        self.client.post(f"{self.make_url}/{job['job_id']}/cancel", headers=self._make_headers("cancel-then-restart"))
        # A restarted process has no live worker ownership.
        restarted = _GatedMakeRuntime(self._asset_path)
        resumed = me_make.resume_jobs(runtime=restarted, jobs_path=self.jobs_db)
        self.assertEqual(resumed, 0)
        self.assertEqual(media_jobs.get(job["job_id"], path=self.jobs_db)["status"], media_jobs.STATUS_FAILED)

    def test_make_cancel_of_a_queued_job_is_immediate(self) -> None:
        import asyncio

        self.runtime.autostart = False
        _, job = self._submit_make("cancel-queued")
        response = self.client.post(f"{self.make_url}/{job['job_id']}/cancel",
                                    headers=self._make_headers("cancel-queued"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["job"]["status"], media_jobs.STATUS_CANCELED)
        for run in self.runtime.pending:
            asyncio.run(run())
        self.assertEqual(self.runtime.calls, [], "a canceled job never reaches the backend")
        self.assertEqual(media_jobs.get(job["job_id"], path=self.jobs_db)["status"], media_jobs.STATUS_CANCELED)

    def _refused(self, key: str, status: int, code: str, **overrides) -> dict:
        jobs_before = media_jobs.count(path=self.jobs_db)
        response = self.client.post(self.make_url, json=self._picture(**overrides),
                                    headers=self._make_headers(key))
        self.assertEqual(response.status_code, status, response.text)
        error = response.json()["error"]
        self.assertEqual(error["code"], code)
        self.assertEqual(media_jobs.count(path=self.jobs_db), jobs_before, "nothing is stored")
        return error

    def test_structured_refusal_records_evidence_without_creating_an_app(self) -> None:
        from fastapi import Response
        from gateway.resource_admission_http import advise_route_admission, admission_http_response
        decision = advise_route_admission(intent="image.generate", service_available=False,
                                         service_reasons=("private other-person quantity 98765",))
        advice = admission_http_response(decision, wait_deadline=456)
        self.runtime.refusal = Response(content=json.dumps(advice.body), status_code=503,
                                        media_type="application/json")
        self.runtime.refusal.resource_decision = decision.details.as_dict(decision.outcome.value)
        apps_before = admin_store.list_apps(path=self.admin_db)
        response = self.client.post(self.make_url, json=self._picture(), headers=self._make_headers("refused"))
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("other-person", response.text)
        self.assertEqual(admin_store.list_apps(path=self.admin_db), apps_before)
        self.assertEqual(media_jobs.count(path=self.jobs_db), 0)
        rows = receipt_store.recent(path=self.receipts_db, audience="internal")
        refused = [row for row in rows if row.get("resource_decision")]
        self.assertEqual(len(refused), 1)
        self.assertTrue(refused[0]["owner_key_hash"].startswith("user:"))
        self.assertIn("other-person", json.dumps(refused[0]["resource_decision"]))

    def test_make_meets_operator_admission_before_storing_anything(self) -> None:
        from fastapi import Response

        refusal = {"error": {"code": "resource_admission_denied", "message": "intent disabled: image.generate"}}
        self.runtime.refusal = Response(content=json.dumps(refusal), status_code=503,
                                         media_type="application/json", headers={"Retry-After": "30"})
        response = self.client.post(self.make_url, json=self._picture(), headers=self._make_headers("refused"))
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json(), refusal, "the same refusal regular media jobs get")
        self.assertEqual(response.headers["retry-after"], "30")
        self.assertEqual(media_jobs.count(path=self.jobs_db), 0)
        self.assertFalse(any(app["name"] == "Azizam" for app in admin_store.list_apps(path=self.admin_db)))
        self.assertEqual(self.runtime.payloads, {})
        self.assertEqual(self.runtime.calls, [])
        invalid = self.client.post(self.make_url, json=self._picture(prompt=""), headers=self._make_headers("bad"))
        self.assertEqual(invalid.status_code, 400, "an invalid request is told what is wrong first")
        self.runtime.refusal = None
        self._submit_make("admitted")

    def test_make_refuses_oversize_requests_before_storing_anything(self) -> None:
        self.runtime.limits = me_make.MakeLimits(max_prompt_chars=100)
        self._refused("long", 400, "prompt_too_long", prompt="x" * 101)
        for n in (0, 11, 1_000_000, True, "2"):
            with self.subTest(n=n):
                self._refused(f"n-{n}", 400, "invalid_n", n=n)
        self.assertEqual(self.runtime.payloads, {})
        self.assertEqual(self.runtime.calls, [])
        self._submit_make("fits", prompt="x" * 100, n=10)

    def test_make_limits_outstanding_jobs_per_person(self) -> None:
        self.runtime.limits = me_make.MakeLimits(per_person=2, household=10)
        self.runtime.release.clear()
        self._submit_make("first")
        self._submit_make("second")
        error = self._refused("third", 429, "too_many_jobs")
        self.assertIn("Wait for one to finish", error["message"])
        self.assertEqual(len(self.runtime.payloads), 2)
        with self._as(self.bob):
            self._submit_make("bob-first")
        self.runtime.release.set()
        self.runtime.wait_idle()
        self._submit_make("after-finish")

    def test_make_limits_outstanding_jobs_for_the_household(self) -> None:
        self.runtime.limits = me_make.MakeLimits(per_person=5, household=2)
        self.runtime.release.clear()
        self._submit_make("alice-first")
        with self._as(self.bob):
            self._submit_make("bob-first")
        response = self.client.post(self.make_url, json=self._picture(), headers=self._make_headers("alice-second"))
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "make_busy")
        self.assertEqual(response.headers["retry-after"], "60")
        self.assertEqual(len(self.runtime.payloads), 2)

    def test_make_cannot_use_another_person_asset_as_source(self) -> None:
        # Criterion 3: asset boundary. Same valid edit shape succeeds with an owned asset.
        canary = "bob-asset-canary-0c3e"
        own_asset, foreign_asset = "img_" + "a1" * 32, "img_" + "b1" * 32
        self._write_asset(own_asset, ["sha256:alice"])
        self._write_asset(foreign_asset, ["sha256:bob"], prompt=canary)
        _, own = self._submit_make("own-edit", intent="image.edit", source_asset_id=own_asset)
        self._finished(own["job_id"])
        self.assertEqual(self.runtime.calls[-1]["body"]["source_asset_id"], own_asset)
        before = media_jobs.count(path=self.jobs_db)
        response = self.client.post(
            self.make_url,
            json=self._picture(intent="image.edit", source_asset_id=foreign_asset),
            headers=self._make_headers("foreign-asset-edit"),
        )
        self.assertIn(response.status_code, (403, 404), response.text)
        self._assert_discloses_nothing(response, canary, "/v1/assets/")
        self.assertEqual(media_jobs.count(path=self.jobs_db), before)
        self.assertEqual(len(self.runtime.calls), 1)

    def test_make_cannot_use_another_person_job_as_source_even_if_output_is_shared(self) -> None:
        # Criterion 3: job boundary, independent of the asset boundary. Bob's
        # output is explicitly shared with Alice, so only a job-ownership check
        # can reject Bob's job as a source.
        canary = "bob-job-canary-41b9"
        with self._as(self.bob):
            foreign, shared_asset = self._completed_output("bob-source", prompt=canary)
        self._meta_path(shared_asset).write_text(json.dumps({
            "asset_id": shared_asset, "owner_key_hashes": ["sha256:bob", "sha256:alice"],
            "updated_at": 1_700_000_000,
        }))
        self.assertEqual(self.client.get(f"/me/api/media/{shared_asset}").status_code, 200)
        own, own_asset = self._completed_output("own-source")
        _, own_edit = self._submit_make("own-job-edit", intent="image.edit", source_job_id=own["job_id"])
        self._finished(own_edit["job_id"])
        self.assertEqual(self.runtime.calls[-1]["body"]["source_asset_id"], own_asset)
        calls = len(self.runtime.calls)
        before = media_jobs.count(path=self.jobs_db)
        response = self.client.post(
            self.make_url,
            json=self._picture(intent="image.edit", source_job_id=foreign["job_id"]),
            headers=self._make_headers("foreign-job-edit"),
        )
        self.assertIn(response.status_code, (403, 404), response.text)
        self._assert_discloses_nothing(response, canary, shared_asset)
        self.assertEqual(media_jobs.count(path=self.jobs_db), before)
        self.assertEqual(len(self.runtime.calls), calls)

    def test_make_produced_asset_is_isolated_to_its_maker(self) -> None:
        # Criterion 3: the asset a make job produces is readable and
        # downloadable by its maker only.
        _, output = self._completed_output("isolated-output")
        own = self.client.get(f"/me/api/media/{output}")
        self.assertEqual(own.status_code, 200, own.text)
        download = self.client.get(f"/me/api/media/{output}/download")
        self.assertEqual(download.status_code, 200, download.text)
        self.assertIn(output, download.json()["download_url"])
        with self._as(self.bob):
            for url in (f"/me/api/media/{output}", f"/me/api/media/{output}/download"):
                denied = self.client.get(url)
                self.assertEqual(denied.status_code, 404, denied.text)
                self._assert_discloses_nothing(denied, "/v1/assets/", "download_url")
            library = self.client.get("/me/api/media")
            self.assertNotIn(output, library.text)

    def test_make_durable_edit_runs_to_completion_after_client_disconnects(self) -> None:
        # Criterion 4: the client goes away before the response body arrives
        # while the backend is mid-job. The job still finishes on its own, and
        # its output lands in the maker's Library.
        import asyncio

        source = "img_" + "c1" * 32
        self._write_asset(source, ["sha256:alice"])
        self.runtime.release.clear()
        body = json.dumps(self._picture(intent="image.edit", source_asset_id=source)).encode()
        sent_status = []

        async def invoke_with_disconnect():
            consumed = False

            async def receive():
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return {"type": "http.disconnect"}

            async def send(message):
                if message["type"] == "http.response.start":
                    sent_status.append(message["status"])
                elif message["type"] == "http.response.body":
                    raise ConnectionError("synthetic browser disconnect before body delivery")

            headers = {"content-type": "application/json",
                       "content-length": str(len(body)),
                       **self._make_headers("disconnected-edit")}
            scope = {
                "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1", "method": "POST", "scheme": "http",
                "path": self.make_url, "raw_path": self.make_url.encode(),
                "query_string": b"", "root_path": "",
                "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
                "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
            }
            try:
                await self.client.app(scope, receive, send)
            except ConnectionError:
                pass

        # The request's own event loop ends here, with the backend still held.
        asyncio.run(invoke_with_disconnect())
        self.client.close()
        self.assertEqual(len(sent_status), 1)
        self.assertIn(sent_status[0], (200, 202))
        self.assertTrue(self.runtime.started.wait(5), "the job must start without the client")
        stored = media_jobs.recent(path=self.jobs_db)[0]
        self.assertEqual(stored["intent"], "image.edit")
        self.assertEqual(stored["status"], media_jobs.STATUS_RUNNING)
        self.assertEqual(stored["assets"], [])
        self.assertEqual(self.runtime.calls[0]["body"]["source_asset_id"], source)
        self.assertEqual(self.runtime.calls[0]["intent"], "image.edit")

        self.runtime.release.set()
        with TestClient(self.client.app) as reconnected:
            deadline = time.time() + 10
            while True:
                polled = reconnected.get(f"{self.make_url}/{stored['job_id']}")
                self.assertEqual(polled.status_code, 200, polled.text)
                if polled.json()["job"]["status"] in media_jobs.TERMINAL_STATUSES:
                    break
                self.assertLess(time.time(), deadline, "job never finished")
                time.sleep(0.02)
            self.assertEqual(polled.json()["job"]["status"], media_jobs.STATUS_SUCCEEDED)
            output = polled.json()["job"]["assets"][0]["asset_id"]
            library = reconnected.get("/me/api/media")
            self.assertEqual(library.status_code, 200, library.text)
            asset = next(item for item in library.json()["assets"] if item["asset_id"] == output)
            self.assertEqual(asset["provenance"]["job_id"], stored["job_id"])
            self.assertEqual(asset["source"]["label"], "Azizam")
        self.runtime.wait_idle()
        self.assertEqual(self.runtime.payloads, {})

    def test_make_never_exposes_key_secrets_on_any_path(self) -> None:
        # Criterion 5: real secret values, not field-name guesses. Alice holds a
        # real minted key; the backend holds its own provider secret and puts
        # it in a failure. Neither value may appear in any response body or
        # header on success, replay, list, detail, cancel, denial, invalid
        # request, or backend failure. Make calls may not mint a key at all.
        from unittest import mock
        from gateway import admin_runtime, user_runtime

        alice_key = user_runtime.mint_user_app_key(
            self._session(None), self.app_a["id"], path=self.admin_db,
        )["api_key"]
        backend_secret = "sk-backend-canary-e91f0c2d7a"
        minted: list[str] = []
        real_app_key = admin_runtime.generate_app_api_key
        real_admin_key = admin_runtime.generate_admin_api_key

        def record(real):
            def wrapper():
                value = real()
                minted.append(value)
                return value
            return wrapper

        credentials_before = len(admin_store.list_credentials(path=self.admin_db))
        with mock.patch.object(admin_runtime, "generate_app_api_key", record(real_app_key)), \
                mock.patch.object(user_runtime, "generate_app_api_key", record(real_app_key)), \
                mock.patch.object(admin_runtime, "generate_admin_api_key", record(real_admin_key)):
            response, job = self._submit_make("no-secret")
            repeat, _ = self._submit_make("no-secret")
            self.runtime.wait_idle()
            self.runtime.fail_with = RuntimeError(f"upstream rejected Bearer {backend_secret}")
            _, failed = self._submit_make("failing-secret")
            self.assertEqual(self._finished(failed["job_id"])["status"], media_jobs.STATUS_FAILED)
            self.runtime.fail_with = None
            with self._as(self.bob):
                _, foreign = self._submit_make("bob-secret")
            self.runtime.wait_idle()
            responses = [
                response, repeat,
                self.client.get(self.make_url),
                self.client.get(f"{self.make_url}/{job['job_id']}"),
                self.client.get(f"{self.make_url}/{failed['job_id']}"),
                self.client.post(f"{self.make_url}/{job['job_id']}/cancel",
                                 headers=self._make_headers("cancel-no-secret")),
                self.client.get(f"{self.make_url}/{foreign['job_id']}"),
                self.client.post(f"{self.make_url}/{foreign['job_id']}/cancel",
                                 headers=self._make_headers("cancel-foreign")),
                self.client.post(self.make_url, json=self._picture(intent="image.nope"),
                                 headers=self._make_headers("invalid")),
                self.client.post(self.make_url, json=self._picture()),
            ]
        self.assertEqual(minted, [], "a make call minted a key")
        self.assertEqual(len(admin_store.list_credentials(path=self.admin_db)), credentials_before)
        self.assertEqual([item.status_code for item in responses[2:5]], [200, 200, 200])
        self.assertEqual(responses[4].json()["job"]["status"], media_jobs.STATUS_FAILED)
        self.assertEqual(responses[8].status_code, 400, responses[8].text)
        self.assertEqual(responses[9].status_code, 403, responses[9].text)
        secrets = [alice_key, backend_secret, *minted]
        stored = json.dumps(media_jobs.get(failed["job_id"], audience="internal", path=self.jobs_db))
        stored += json.dumps(media_jobs.events(failed["job_id"], audience="internal", path=self.jobs_db))
        for secret in secrets:
            self.assertNotIn(secret, stored)
        for item in responses:
            with self.subTest(method=item.request.method, url=str(item.request.url)):
                self._assert_discloses_nothing(item, *secrets)

    def test_make_submit_and_cancel_require_both_mutation_headers(self) -> None:
        # Criterion 6: missing each header independently, plus neither header.
        _, job = self._submit_make("csrf-control")
        self.runtime.wait_idle()
        for omitted in (("x-azizam-user-action",), ("x-azizam-csrf",),
                        ("x-azizam-user-action", "x-azizam-csrf")):
            headers = self._make_headers("csrf-rejected")
            for key in omitted:
                headers.pop(key)
            for url in (self.make_url, f"{self.make_url}/{job['job_id']}/cancel"):
                with self.subTest(omitted=omitted, url=url):
                    before = media_jobs.get(job["job_id"], path=self.jobs_db)
                    count = media_jobs.count(path=self.jobs_db)
                    response = self.client.post(url, json=self._picture(), headers=headers)
                    self.assertEqual(response.status_code, 403, response.text)
                    expected = ("user_action_header_required" if "x-azizam-user-action" in omitted
                                else "user_csrf_required")
                    self.assertEqual(response.json()["error"]["code"], expected)
                    self.assertEqual(media_jobs.count(path=self.jobs_db), count)
                    self.assertEqual(media_jobs.get(job["job_id"], path=self.jobs_db), before)

    def test_make_forget_extension_keeps_prompt_out_of_storage_for_whole_lifecycle(self) -> None:
        # Criterion 7: inspect internal stores after success and after a
        # backend failure that echoes the prompt, not just at submission.
        import sqlite3

        prompt = "forget-canary-7b3e-private-blue-heron"
        failing = "forget-canary-9a2c-private-grey-heron"
        response, job = self._submit_make("forget-picture", prompt=prompt, extensions=FORGET)
        # The forget control passes through to the backend.
        self.runtime.wait_idle()
        self.assertEqual(self.runtime.calls[-1]["body"]["prompt"], prompt)
        self.assertEqual(self.runtime.calls[-1]["body"]["extensions"], FORGET)
        self.assertEqual(self._finished(job["job_id"])["status"], media_jobs.STATUS_SUCCEEDED)
        self.runtime.fail_with = RuntimeError(f"backend could not draw {failing}")
        _, failed = self._submit_make("forget-failing", prompt=failing, extensions=FORGET)
        self.assertEqual(self._finished(failed["job_id"])["status"], media_jobs.STATUS_FAILED)

        for job_id in (job["job_id"], failed["job_id"]):
            stored = media_jobs.get(job_id, audience="internal", path=self.jobs_db)
            events = media_jobs.events(job_id, audience="internal", path=self.jobs_db)
            self.assertTrue(events)
            receipt = receipt_store.get(stored["request_id"], audience="internal", path=self.receipts_db)
            self.assertIsNotNone(receipt)
            for canary in (prompt, failing):
                self.assertNotIn(canary, json.dumps(stored))
                self.assertNotIn(canary, json.dumps(events))
                self.assertNotIn(canary, json.dumps(receipt))
        for db in (self.jobs_db, self.receipts_db):
            with sqlite3.connect(db) as conn:
                dump = "\n".join(conn.iterdump())
            self.assertNotIn(prompt, dump)
            self.assertNotIn(failing, dump)
        # Execution payloads are released once the job is terminal.
        self.assertEqual(self.runtime.payloads, {})
        for job_id in (job["job_id"], failed["job_id"]):
            detail = self.client.get(f"{self.make_url}/{job_id}")
            self.assertEqual(detail.status_code, 200, detail.text)
            self._assert_discloses_nothing(detail, prompt, failing)
        self._assert_discloses_nothing(response, prompt)

    def test_make_rejects_a_malformed_forget_extension(self) -> None:
        response = self.client.post(
            self.make_url,
            json=self._picture(extensions={"com.azizam.prompts": {"mode": "maybe"}}),
            headers=self._make_headers("bad-forget"),
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(media_jobs.count(path=self.jobs_db), 0)



class MemberFailureCopyTests(unittest.TestCase):
    """Every member API failure names its layer and one next action.

    Codes and statuses stay stable for API clients; ids, parameter names and
    exception classes move to ``details`` (principles P3 and P15).
    """

    _session = MeMediaRouterTests._session
    _mutation_validator = MeMediaRouterTests._mutation_validator
    _asset_path = MeMediaRouterTests._asset_path
    _meta_path = MeMediaRouterTests._meta_path
    _asset_metadata = MeMediaRouterTests._asset_metadata
    _catalog = MeMediaRouterTests._catalog
    _delete_unshared = MeMediaRouterTests._delete_unshared
    _write_asset = MeMediaRouterTests._write_asset
    _receipt = MeMediaRouterTests._receipt
    _job = MeMediaRouterTests._job
    _make_headers = MeMakeApiAcceptanceTests._make_headers
    _picture = MeMakeApiAcceptanceTests._picture
    _submit_make = MeMakeApiAcceptanceTests._submit_make
    _finished = MeMakeApiAcceptanceTests._finished
    make_url = MeMakeApiAcceptanceTests.make_url
    setUp = MeMakeApiAcceptanceTests.setUp
    tearDown = MeMakeApiAcceptanceTests.tearDown

    def _error(self, response, status: int, code: str, layer: str) -> dict:
        self.assertEqual(response.status_code, status, response.text)
        error = response.json()["error"]
        self.assertEqual(error["code"], code)
        self.assertEqual(error["details"]["layer"], layer)
        return error

    def test_library_errors_give_the_layer_and_one_action(self) -> None:
        missing = self._error(self.client.get("/me/api/media/img_" + "c" * 64), 404, "asset_not_found", "library")
        self.assertEqual(missing["message"], "This item isn't in your Library. "
                                             "Return to the Library and choose another item.")
        self._error(self.client.get("/me/api/media", params={"cursor": "%%%"}), 400, "invalid_cursor", "request")
        self._write_asset("img_" + "e" * 64, ["sha256:alice"])  # Dates are checked against items.
        bad_date = self._error(self.client.get("/me/api/media", params={"start": "May 1"}), 400,
                               "invalid_date", "request")
        self.assertNotIn("YYYY", bad_date["message"])
        period = self._error(self.client.get("/me/api/usage/summary", params={"period": "week"}), 400,
                             "unsupported_period", "request")
        self.assertNotIn("period=", period["message"])
        source = self._error(self.client.get("/me/api/media", params={"source": "x"}), 400, "invalid_source", "request")
        self.assertNotIn("app:<id>", source["message"])
        owner = self._error(self.client.get("/me/api/media", params={"person": "all"}), 403, "owner_required", "account")
        self.assertIn("Choose your own work", owner["message"])

    def test_missing_file_is_not_called_expired(self) -> None:
        asset_id = "aud_" + "d" * 64
        self._receipt(self.app_a["id"], "req_gone")
        self._job("req_gone", asset_id, media_type="audio/mpeg")
        error = self._error(self.client.get(f"/me/api/media/{asset_id}/download"), 404, "asset_unavailable", "library")
        self.assertNotIn("expired", error["message"])
        self.assertIn("make it again", error["message"])

    def test_signed_out_and_unknown_person_say_sign_in_again(self) -> None:
        ghost = {"id": "usr_gone", "email": "gone@example.com"}
        self.current_user = ghost
        for response in (self.client.get("/me/api/media"),
                         self.client.post(self.make_url, json=self._picture(), headers=self._make_headers("g"))):
            error = self._error(response, 401, "session_user_not_found", "account")
            self.assertEqual(error["message"], "Your sign-in no longer matches an account. Sign in again.")

    def test_make_request_errors_keep_parameter_names_in_details(self) -> None:
        unknown = self._error(self.client.post(self.make_url, json=self._picture(steps=4), headers=self._make_headers("f")),
                              400, "unsupported_field", "request")
        self.assertNotIn("steps", unknown["message"])
        self.assertEqual(unknown["details"]["field"], "steps")
        intent = self._error(self.client.post(self.make_url, json=self._picture(intent="video.generate"),
                                              headers=self._make_headers("i")), 400, "unsupported_intent", "request")
        self.assertNotIn("intent", intent["message"])
        self.assertNotIn("image.generate", intent["message"])
        both = self._error(self.client.post(self.make_url, json=self._picture(intent="image.edit", source_asset_id="img_x",
                                                                           source_job_id="mj_x"),
                                            headers=self._make_headers("b")), 400, "invalid_source", "request")
        self.assertNotIn("source_asset_id", both["message"])
        edit = self._error(self.client.post(self.make_url, json=self._picture(intent="image.edit"),
                                            headers=self._make_headers("e")), 400, "invalid_source", "request")
        self.assertEqual(edit["message"], "Editing needs a source picture. Choose a picture to edit.")
        self.runtime.limits = me_make.MakeLimits(max_images=4, max_prompt_chars=1000)
        count = self._error(self.client.post(self.make_url, json=self._picture(n=9), headers=self._make_headers("n")),
                            400, "invalid_n", "request")
        self.assertEqual(count["message"], "That number of pictures isn't allowed. Choose from 1 to 4.")
        long = self._error(self.client.post(self.make_url, json=self._picture(prompt="x" * 1001),
                                            headers=self._make_headers("l")), 400, "prompt_too_long", "request")
        self.assertIn("1,000 characters", long["message"])

    def test_idempotency_conflict_names_the_action_not_the_policy(self) -> None:
        self._submit_make("same-key")
        error = self._error(self.client.post(self.make_url, json=self._picture(prompt="another"),
                                             headers=self._make_headers("same-key")),
                            409, "idempotency_conflict", "request")
        self.assertNotIn("owner, operation", error["message"])
        self.assertIn("Refresh the page", error["message"])

    def test_source_job_that_is_running_or_failed_is_told_apart(self) -> None:
        self.runtime.release.clear()
        _, running = self._submit_make("still-running")
        self.assertTrue(self.runtime.started.wait(5))
        pending = self._error(self.client.post(self.make_url, json=self._picture(intent="image.edit",
                                                                              source_job_id=running["job_id"]),
                                               headers=self._make_headers("edit-running")),
                              409, "source_not_ready", "library")
        self.assertEqual(pending["message"], "That picture isn't ready. Wait for it to finish.")
        self.runtime.fail_with = RuntimeError("boom")
        self.runtime.release.set()
        self._finished(running["job_id"])
        failed = self._error(self.client.post(self.make_url, json=self._picture(intent="image.edit",
                                                                             source_job_id=running["job_id"]),
                                              headers=self._make_headers("edit-failed")),
                             409, "source_not_ready", "library")
        self.assertEqual(failed["message"], "That task didn't produce a picture. Choose another source picture.")
        self.assertNotIn("yet", failed["message"])

    def test_stored_job_failures_never_carry_an_exception_class(self) -> None:
        self.runtime.fail_with = KeyError("secret-prompt")
        _, job = self._submit_make("crash")
        stored = self._finished(job["job_id"])
        self.assertEqual(stored["status"], media_jobs.STATUS_FAILED)
        self.assertNotIn("KeyError", stored["error"])
        self.assertEqual(stored["error"],
                         "The picture couldn't be made, and we couldn't tell where it stopped. Try again.")

    def test_backend_without_picture_names_the_model(self) -> None:
        async def nothing(payload):
            return []

        self.runtime.execute = nothing
        _, job = self._submit_make("empty")
        stored = self._finished(job["job_id"])
        self.assertEqual(stored["error"], "The model finished without a picture. Try again.")

    def test_queue_failure_is_stored_without_an_exception_class(self) -> None:
        def broken(job_id, payload):
            raise OSError("disk full")

        self.runtime.keep_payload = broken
        error = self._error(self.client.post(self.make_url, json=self._picture(), headers=self._make_headers("q")),
                            500, "make_queue_failed", "gateway")
        self.assertEqual(error["message"], "The gateway couldn't save this task to the queue. Try again.")
        stored = media_jobs.recent(5, path=self.jobs_db)[0]
        self.assertNotIn("OSError", stored["error"])
        self.assertEqual(stored["error"], error["message"])


class ConservativeAssetDeleterTests(unittest.TestCase):
    def test_owner_added_while_delete_waits_keeps_the_shared_asset(self) -> None:
        import threading
        from gateway.prompt_metadata_migration import CACHE_MUTATION_LOCK

        with tempfile.TemporaryDirectory() as tmp:
            asset = Path(tmp) / "asset.png"
            asset.write_bytes(b"only copy")
            metadata = {"owner_key_hashes": ["app:first"], "maker_person_ids": ["person-1"]}
            deleter = me_media.conservative_asset_deleter(
                metadata_reader=lambda asset_id: json.loads(json.dumps(metadata)),
                asset_path_resolver=lambda asset_id: asset,
                metadata_path_resolver=lambda asset_id: None,
                prompt_memory_path=lambda: Path(tmp) / "jobs.db",
            )
            results = []
            with CACHE_MUTATION_LOCK:  # asset storage is publishing a second maker
                delete = threading.Thread(target=lambda: results.append(deleter("img_" + "0" * 64)))
                delete.start()
                delete.join(timeout=0.3)
                metadata["owner_key_hashes"].append("app:second")
                metadata["maker_person_ids"].append("person-2")
            delete.join(timeout=10)
            self.assertEqual(results, [False])
            self.assertEqual(asset.read_bytes(), b"only copy")

if __name__ == "__main__":
    unittest.main()
