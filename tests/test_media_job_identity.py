"""Media jobs run as the person or App that asked for them.

A job is accepted on one request and runs later in a worker. The worker must
use the caller's own App, key name, access plan and person, rebuilt from the
admin store, never the worker's network position. These tests use a real
admin database (no patched identity lookup) and a fake image backend, so the
real plan check runs at submit time and again in the worker.
"""
from __future__ import annotations

import ipaddress
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi import Response

import proxy
from gateway import accounts, admin_store, media_jobs
from tests.support.access import store_plan

FLUX = "image.generate.flux2-klein-4b"
ASSET_ID = "img_" + "c" * 64


class _Request:
    """The request a core route sees after the middleware authenticated it."""

    method = "POST"
    query_params: dict[str, str] = {}

    def __init__(self, auth_context: proxy.AuthContext) -> None:
        self.scope = {"auth_context": auth_context}
        self.headers = {"host": "gateway.test"}
        self.url = SimpleNamespace(scheme="http", netloc="gateway.test")

    async def json(self) -> dict[str, Any]:
        return {}


class MediaJobIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.admin = root / "admin.db"
        self.jobs = root / "media_jobs.db"
        admin_store.init_db(self.admin)
        media_jobs.init_db(self.jobs)
        accounts.initialize(self.admin)
        self.person = admin_store.upsert_oidc_user(
            "https://idp.test", "ann", email="ann@example.test", path=self.admin)
        self.images = store_plan("Flux only", path=self.admin, caps=("image",), workflows=[FLUX])
        self.other = store_plan("Krea only", path=self.admin, caps=("image",),
                                workflows=["image.generate.krea2-turbo"])
        accounts.change_person(self.person["id"], "test", path=self.admin, plan_id=self.images["id"])
        self.session_app = admin_store.ensure_session_app_for_user(
            self.person["id"], name="Ann", path=self.admin)
        self.receipts: list[dict[str, Any]] = []

        def capture(receipt, **_kwargs):
            self.receipts.append(receipt.to_dict() if hasattr(receipt, "to_dict") else dict(receipt))
            return self.receipts[-1].get("request_id")

        for target, name, value in (
            (proxy.ADMIN_STORE, "path", self.admin),
            (media_jobs, "DB_PATH", self.jobs),
            (proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", root / "payloads"),
            (proxy, "IMAGE_MEDIA_JOB_MAX_ATTEMPTS", 1),
            (proxy, "COMFY_ENABLED", True),
            (proxy, "_image_url_signing_secret", b"test-secret"),
            (proxy.receipt_store, "record", capture),
            (proxy, "_schedule_image_media_job", lambda job_id: True),
            (proxy, "resolve_image_background_decision",
             AsyncMock(return_value=proxy.ImageBackgroundRemovalDecision(False))),
            (proxy, "_generate_images_with_adaptive_batching", self._fake_backend),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        overrides = patch.object(proxy.ADMIN_STORE, "operator_overrides",
                                 return_value={"service_availability": {}, "media_job_admission": {}})
        overrides.start()
        self.addCleanup(overrides.stop)
        self.backend_calls = 0

    async def _fake_backend(self, **_kwargs: Any) -> Response:
        self.backend_calls += 1
        return Response(
            content=json.dumps({"created": 1, "data": [{
                "asset_id": ASSET_ID, "url": "http://gateway.test/v1/assets/x", "media_type": "image/png"}]}),
            status_code=200, media_type="application/json")

    def mcp_context(self) -> proxy.AuthContext:
        """What an MCP OAuth access token authenticates as (``_authenticate_mcp_access_token``)."""
        identity = proxy._mcp_identity_for_person(self.person["id"])
        assert identity is not None and identity.denial is None
        return proxy.AuthContext(identity=replace(identity, name="ChatGPT (MCP)"))

    async def submit(self, auth_context: proxy.AuthContext, model: str = "flux2-klein-4b") -> str:
        response = await proxy._dispatch_gateway_media_job_submission(
            request=_Request(auth_context),  # type: ignore[arg-type]
            payload={"intent": "image.generate", "request": {"model": model, "prompt": "a red fox"}},
            audience="client",
        )
        assert response is not None
        self.assertEqual(response.status_code, 202, response.body)
        return json.loads(response.body)["job_id"]

    def worker_receipt(self, job_id: str) -> dict[str, Any]:
        rows = [r for r in self.receipts if r.get("job_id") == job_id and r.get("path") == "/v1/images/generations"]
        self.assertEqual(len(rows), 1, self.receipts)
        return rows[0]

    async def test_mcp_job_runs_as_the_caller_and_succeeds(self) -> None:
        job_id = await self.submit(self.mcp_context())

        await proxy._run_image_media_job(job_id)

        job = media_jobs.get(job_id, audience="internal", path=self.jobs)
        self.assertEqual(job["status"], media_jobs.STATUS_SUCCEEDED, job.get("error"))
        self.assertEqual(self.backend_calls, 1)
        receipt = self.worker_receipt(job_id)
        self.assertEqual(receipt["app_id"], self.session_app["id"])
        self.assertEqual(receipt["api_key_name"], "ChatGPT (MCP)")
        self.assertEqual(receipt["app_profile"], self.images["id"])
        self.assertEqual(receipt["policy_decision"], "allowed")
        self.assertNotEqual(receipt["source_kind"], "local-network")

    async def test_worker_checks_the_callers_current_plan(self) -> None:
        job_id = await self.submit(self.mcp_context())
        # The owner moves the person to a plan without this workflow before it runs.
        accounts.change_person(self.person["id"], "test", path=self.admin, plan_id=self.other["id"])

        await proxy._run_image_media_job(job_id)

        job = media_jobs.get(job_id, audience="internal", path=self.jobs)
        self.assertEqual(job["status"], media_jobs.STATUS_FAILED)
        self.assertEqual(self.backend_calls, 0)
        receipt = self.worker_receipt(job_id)
        self.assertEqual(receipt["app_id"], self.session_app["id"])
        self.assertEqual(receipt["app_profile"], self.other["id"])
        self.assertEqual(receipt["policy_decision"], "denied")
        # The gateway refused, so the job says so; nothing upstream ran.
        client = media_jobs.get(job_id, audience="client", path=self.jobs)
        self.assertEqual(client["error"], "This workflow isn't in your access plan.")
        self.assertEqual(client["error_category"], "authorization")
        self.assertEqual(client["error_code"], "not_in_access_plan")
        self.assertIs(client["retryable"], False)
        self.assertIsNone(client["upstream_http_status"])

    async def test_full_workflow_id_and_short_name_run_the_same(self) -> None:
        context = self.mcp_context()
        outcomes = []
        for model in ("flux2-klein-4b", FLUX):
            job_id = await self.submit(context, model=model)
            await proxy._run_image_media_job(job_id)
            job = media_jobs.get(job_id, audience="internal", path=self.jobs)
            receipt = self.worker_receipt(job_id)
            outcomes.append((job["status"], job["workflow"], receipt["app_id"], receipt["manifest_id"]))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(outcomes[0][0], media_jobs.STATUS_SUCCEEDED)

    async def test_disabled_person_job_does_not_run(self) -> None:
        job_id = await self.submit(self.mcp_context())
        accounts.change_person(self.person["id"], "test", path=self.admin, disabled=True)

        await proxy._run_image_media_job(job_id)

        job = media_jobs.get(job_id, audience="internal", path=self.jobs)
        self.assertEqual(job["status"], media_jobs.STATUS_FAILED)
        self.assertEqual(self.backend_calls, 0)
        self.assertEqual(job["error_category"], "authorization")
        self.assertIs(job["retryable"], False)

    # -- every worker rebuilds the same caller ------------------------------

    def payload_for(self, auth_context: proxy.AuthContext) -> dict[str, Any]:
        """The execution payload a submit route spools for a job."""
        captured: dict[str, Any] = {}
        with patch.object(proxy, "_spool_media_job_payload", lambda _job_id, payload: captured.update(payload)):
            proxy._persist_image_media_job_payload(
                "mj_test", body={"prompt": "x"}, request=_Request(auth_context),  # type: ignore[arg-type]
                auth_context=auth_context)
        return captured

    def test_every_kind_of_caller_is_rebuilt_as_itself(self) -> None:
        owner = self.person["id"]
        service = admin_store.create_app("daisy", kind="service", owner_user_id=owner,
                                         access_plan=self.images["id"], path=self.admin)
        admin_store.create_credential_hash(subject_kind="app", subject_id=service["id"], app_id=service["id"],
                                           hash=proxy.hash_api_key("oak_daisy"), prefix="oak_dai",
                                           created_by=owner, path=self.admin)
        lan = admin_store.create_app("Kitchen tablet", kind="service", owner_user_id=owner,
                                     access_plan=self.images["id"], path=self.admin)
        admin_store.add_trusted_address(lan["id"], "192.0.2.7", max_entries=10, path=self.admin)
        callers = {
            "mcp": self.mcp_context(),
            "key": proxy.AuthContext(identity=proxy.runtime_identity_for_app_credential_hash(
                proxy.hash_api_key("oak_daisy"), path=self.admin)),
            "trusted address": proxy.AuthContext(identity=proxy.runtime_identity_for_trusted_address(
                ipaddress.ip_address("192.0.2.7"), path=self.admin)),
        }
        for label, context in callers.items():
            with self.subTest(label):
                rebuilt = proxy._auth_context_from_image_job_payload(self.payload_for(context))
                assert rebuilt is not None and rebuilt.identity is not None
                for field in ("app_id", "person_id", "key_hash", "name"):
                    self.assertEqual(getattr(rebuilt.identity, field), getattr(context.identity, field), field)
                self.assertEqual(rebuilt.identity.access.plan_ids, context.identity.access.plan_ids)
        # Removing the address the owner trusted stops that App's queued work.
        payload = self.payload_for(callers["trusted address"])
        for entry in admin_store.list_trusted_addresses(lan["id"], path=self.admin):
            admin_store.delete_trusted_address(lan["id"], entry["id"], path=self.admin)
        self.assertIsNone(proxy._auth_context_from_image_job_payload(payload))

    async def test_audio_and_video_workers_run_as_the_caller(self) -> None:
        context = self.mcp_context()
        seen: dict[str, Any] = {}

        async def fake_audio(_request, auth_context, **_kwargs):
            seen["audio"] = auth_context
            return Response(content=b"{}", status_code=500, media_type="application/json")

        async def fake_video(_request, auth_context, **_kwargs):
            seen["video"] = auth_context
            return Response(content=b"{}", status_code=500, media_type="application/json")

        for intent, worker, fake_name, fake in (
            ("audio.generate", proxy._run_audio_media_job, "proxy_audio_generate", fake_audio),
            ("video.generate", proxy._run_video_media_job, "proxy_comfy_video_create", fake_video),
        ):
            job = media_jobs.MediaJob(intent=intent, request_id=f"req_{intent}", metadata={"mode": "test"})
            media_jobs.create(job, path=self.jobs)
            payload = {**self.payload_for(context), "body": {"model": "m", "prompt": "x"},
                       "headers": {}, "url": {}}
            with patch.object(proxy, fake_name, fake), \
                    patch.object(proxy, "_load_audio_media_job_payload", lambda _job_id: payload), \
                    patch.object(proxy, "_load_video_media_job_payload", lambda _job_id: payload):
                await worker(job.job_id)
        for kind in ("audio", "video"):
            with self.subTest(kind):
                identity = seen[kind].identity
                self.assertEqual(identity.app_id, self.session_app["id"])
                self.assertEqual(identity.person_id, self.person["id"])
                self.assertEqual(identity.name, "ChatGPT (MCP)")


if __name__ == "__main__":
    unittest.main()
