from __future__ import annotations

import base64
from tests.support.app_keys import accountable_person
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

from gateway import admin_store, media_delivery, media_jobs, media_metadata, me_make, me_media, ownership, prompt_memory, provenance, receipt_store
from gateway.admin_oidc import AdminSession
from gateway.admin_oidc import CookieConfig, OIDCConfig, encode_session_cookie, session_from_claims
from gateway.receipts import RouteReceipt
from tests.support.access import service_app


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)




class MeMediaGapContractTests(unittest.TestCase):
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
            path=self.admin_db, created_by=accountable_person(self.admin_db, self.app_a["id"]))
        self.cred_b = admin_store.create_credential_hash(
            subject_kind="app",
            subject_id=self.app_b["id"],
            app_id=self.app_b["id"],
            hash="sha256:bob",
            prefix="test_bob",
            path=self.admin_db, created_by=accountable_person(self.admin_db, self.app_b["id"]))
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


    # --- gap contracts -------------------------------------------------

    def test_shared_owner_and_maker_guards_are_independent(self) -> None:
        asset_id = "img_" + "a" * 64
        cases = [
            (["app:a", "app:b"], ["person:a"], False),
            (["app:a"], ["person:a", "person:b"], False),
            (["app:a"], ["person:a"], True),
        ]
        for owners, makers, expected_removed in cases:
            with self.subTest(owners=owners, makers=makers):
                self._write_asset(asset_id, ["sha256:alice"], "only prompt")
                meta = json.loads(self._meta_path(asset_id).read_text())
                meta.update(owner_key_hashes=owners, maker_person_ids=makers)
                self._meta_path(asset_id).write_text(json.dumps(meta))
                delete = me_media.conservative_asset_deleter(
                    metadata_reader=self._asset_metadata,
                    asset_path_resolver=self._asset_path,
                    metadata_path_resolver=self._meta_path,
                    prompt_memory_path=lambda: self.prompt_db,
                )
                self.assertEqual(delete(asset_id), expected_removed)
                self.assertEqual(self._asset_path(asset_id).exists(), not expected_removed)
                self.assertEqual(self._meta_path(asset_id).exists(), not expected_removed)
                prompt = prompt_memory.get(asset_id, self.alice["id"], path=self.prompt_db)
                self.assertEqual(prompt is None, expected_removed)

    def test_deleted_asset_survives_same_person_audit_pagination(self) -> None:
        asset_id = "img_" + "b" * 64
        self._write_asset(asset_id, ["sha256:alice", "sha256:bob"])
        headers = {"x-azizam-user-action": "1", "x-azizam-csrf": "valid-csrf"}
        response = self.client.delete(f"/me/api/media/{asset_id}", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["physical_removed"])
        # Alice's own later history (more than two 500-row pages) must not
        # push her deletion out of the tombstone scan.
        later = int(time.time()) + 100
        for index in range(1001):
            admin_store.record_audit(
                "media.asset.delete", actor_kind="user", actor_id=self.alice["id"],
                target_kind="media_asset", target_id=f"later-{index:04d}",
                now=later, path=self.admin_db,
            )
        self.assertEqual(self.client.get(f"/me/api/media/{asset_id}").status_code, 404)
        self.assertEqual(self.client.get("/me/api/media").json()["assets"], [])
        self.current_user = self.bob
        self.assertEqual(self.client.get(f"/me/api/media/{asset_id}").status_code, 200)
        self.assertTrue(self._asset_path(asset_id).exists())

    def test_stale_owner_session_loses_everyone_access_immediately(self) -> None:
        asset_id = "img_" + "c" * 64
        self._write_asset(asset_id, ["sha256:alice"], "PRIVATE-PROMPT")
        self.current_user = self.owner
        paths = [
            "/me/api/media?scope=everyone",
            f"/me/api/media/{asset_id}?scope=everyone",
            f"/me/api/media/{asset_id}/download?scope=everyone",
            "/me/api/usage/summary?person=all",
        ]
        for path in paths:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, (path, response.text))
        assignments = admin_store.list_admin_roles(
            subject_kind="user", subject_id=self.owner["id"], path=self.admin_db
        )
        self.assertEqual(len(assignments), 1)
        self.assertTrue(admin_store.revoke_admin_role(assignments[0]["id"], path=self.admin_db))
        for path in paths:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 403, (path, response.text))
            self.assertEqual(response.json()["error"]["code"], "owner_required")
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertNotIn("PRIVATE-PROMPT", response.text)

    def test_jobs_paginate_ties_after_scoping_without_loss(self) -> None:
        expected = [f"mine-{i:03d}" for i in range(205)]
        for job_id in expected:
            media_jobs.create(media_jobs.MediaJob(
                job_id=job_id, intent="image.generate", app_id=self.app_a["id"],
                status="succeeded", created_at=1700000000, updated_at=1700000000,
            ), path=self.jobs_db)
        # Newer foreign rows would fill a globally paginated first page.
        for index in range(105):
            media_jobs.create(media_jobs.MediaJob(
                job_id=f"foreign-{index:03d}", intent="image.generate",
                app_id=self.app_b["id"], status="succeeded",
                created_at=1700000001, updated_at=1700000001,
            ), path=self.jobs_db)
        collected, lengths, cursor = [], [], None
        for _ in range(3):
            params = {"cursor": cursor} if cursor is not None else {}
            response = self.client.get("/me/api/jobs", params=params)
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            ids = [job["job_id"] for job in payload["jobs"]]
            collected.extend(ids)
            lengths.append(len(ids))
            cursor = payload["next_cursor"]
        self.assertEqual(lengths, [100, 100, 5])
        self.assertEqual(collected, sorted(expected, reverse=True))
        self.assertIsNone(cursor)

    def test_ffprobe_failures_and_output_limit_degrade_to_empty(self) -> None:
        from types import SimpleNamespace

        path = self.cache / "clip.mp4"
        path.write_bytes(b"not-a-real-container")
        record = provenance.build_provenance_record(
            kind="video", requested_model="video", resolved_model="local-video",
            seed=7, parameters={"prompt": "x"}, workflow_id="wf.video",
            created_at=1_700_000_000, request_id="req_probe",
        )

        def stdout_of_length(size: int) -> str:
            base = {"format": {"tags": {"azizam_provenance": json.dumps(record)}},
                    "streams": [{"tags": {"pad": ""}}]}
            base["streams"][0]["tags"]["pad"] = "x" * (size - len(json.dumps(base)))
            text = json.dumps(base)
            self.assertEqual(len(text), size)
            return text

        limit = 1024 * 1024
        with patch.object(me_media.subprocess, "run",
                          return_value=SimpleNamespace(returncode=0, stdout=stdout_of_length(limit))) as run:
            accepted = me_media._embedded_provenance(path)
        self.assertEqual(accepted["request_id"], "req_probe")
        self.assertIs(accepted["workflow_available"], False)
        self.assertEqual(run.call_args.kwargs["timeout"], 3)
        self.assertIs(run.call_args.kwargs["check"], False)
        failures = [
            dict(side_effect=subprocess.TimeoutExpired(cmd="ffprobe", timeout=3)),
            dict(side_effect=OSError("ffprobe unavailable")),
            dict(return_value=SimpleNamespace(returncode=1, stdout=stdout_of_length(limit))),
            dict(return_value=SimpleNamespace(returncode=0, stdout="{broken")),
            dict(return_value=SimpleNamespace(returncode=0, stdout=stdout_of_length(limit + 1))),
        ]
        for failure in failures:
            with self.subTest(failure=failure):
                with patch.object(me_media.subprocess, "run", **failure):
                    self.assertEqual(me_media._embedded_provenance(path), {})

    def test_png_read_limit_is_exactly_32_mib(self) -> None:
        asset_id = "img_" + "d" * 64
        self._write_asset(asset_id, ["sha256:alice"])
        path = self._asset_path(asset_id)
        limit = 32 * 1024 * 1024
        with path.open("r+b") as handle:
            handle.truncate(limit)
        at_limit = me_media._embedded_provenance(path)
        self.assertEqual(at_limit["request_id"], "req_cache")
        self.assertIs(at_limit["workflow_available"], True)
        with path.open("r+b") as handle:
            handle.truncate(limit + 1)
        self.assertEqual(me_media._embedded_provenance(path), {})


if __name__ == "__main__":
    unittest.main()
