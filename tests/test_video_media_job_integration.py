from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from unittest.mock import patch

from fastapi import Response

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import media_jobs  # noqa: E402


class _StubJsonRequest:
    method = "POST"
    scope: dict[str, Any] = {}
    query_params: dict[str, str] = {}

    def __init__(
        self,
        body: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._body = dict(body)
        self.headers = dict(headers or {"host": "gateway.test"})
        self.url = SimpleNamespace(scheme="http", netloc="gateway.test")

    async def json(self) -> dict[str, Any]:
        return dict(self._body)


class VideoMediaJobIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "media_jobs.db"
        self.payload_dir = self.root / "payloads"
        media_jobs.init_db(self.db)

        self.patches = [
            patch.object(media_jobs, "DB_PATH", self.db),
            patch.object(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", self.payload_dir),
            patch.object(proxy, "AUTH_ENABLED", False),
            patch.object(proxy, "_VIDEO_MEDIA_JOB_POLL_INTERVAL_SECONDS", 0),
            patch.object(proxy, "COMFY_VIDEO_TIMEOUT", 5),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    async def test_generic_submit_creates_video_job_without_prompt_metadata(self) -> None:
        body = {
            "model": "ltx-2.3",
            "prompt": "private video prompt",
            "seconds": 2,
            "size": "1024x576",
            "seed": 77,
        }
        scheduled: list[str] = []

        with patch.object(
            proxy,
            "_schedule_video_media_job",
            lambda job_id: scheduled.append(job_id) or True,
        ):
            response = await proxy._dispatch_gateway_media_job_submission(
                request=_StubJsonRequest({}),  # type: ignore[arg-type]
                payload={"intent": "video.generate", "request": body},
                audience="client",
            )

        assert response is not None
        self.assertEqual(response.status_code, 202)
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(scheduled, [payload["job_id"]])
        stored = media_jobs.get(payload["job_id"], audience="internal", path=self.db)
        assert stored is not None
        self.assertEqual(stored["intent"], "video.generate")
        self.assertEqual(stored["status"], media_jobs.STATUS_QUEUED)
        self.assertEqual(stored["metadata"]["mode"], "async_video_generation")
        self.assertNotIn("private video prompt", json.dumps(stored["metadata"]))

        private_payload = proxy._load_video_media_job_payload(payload["job_id"])
        assert private_payload is not None
        self.assertEqual(private_payload["body"]["prompt"], "private video prompt")

    async def test_direct_video_submit_uses_resource_admission(self) -> None:
        body = {
            "model": "ltx-2.3",
            "prompt": "private video prompt",
            "seconds": 2,
        }

        with patch.object(proxy, "COMFY_ENABLED", False):
            response = await proxy._submit_video_media_job(
                request=_StubJsonRequest(body),  # type: ignore[arg-type]
                body=body,
            )

        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(
            payload["error"]["code"],
            "resource_admission_denied",
        )
        self.assertEqual(media_jobs.recent(path=self.db), [])

    async def test_direct_video_submit_uses_media_job_admission_overrides(self) -> None:
        body = {
            "model": "ltx-2.3",
            "prompt": "private video prompt",
            "seconds": 2,
        }
        overrides = {
            "service_availability": {},
            "media_job_admission": {
                "disabled_intents": ["video.generate"],
                "retry_after_seconds": 17,
            },
        }

        with patch.object(proxy, "MEDIA_JOB_ADMISSION_CFG", {}), patch.object(
            proxy.ADMIN_STORE,
            "operator_overrides",
            return_value=overrides,
        ):
            response = await proxy._submit_video_media_job(
                request=_StubJsonRequest(body),  # type: ignore[arg-type]
                body=body,
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["retry-after"], "17")
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(
            payload["error"]["code"],
            "resource_admission_denied",
        )
        self.assertEqual(media_jobs.recent(path=self.db), [])

    async def test_direct_video_submit_uses_service_availability_overrides(self) -> None:
        body = {
            "model": "ltx-2.3",
            "prompt": "private video prompt",
            "seconds": 2,
        }
        overrides = {
            "service_availability": {
                "comfyui": {
                    "state": "maintenance",
                    "reason": "reserved for video maintenance",
                    "retry_after_seconds": 45,
                }
            },
            "media_job_admission": {},
        }

        with patch.object(proxy, "COMFY_ENABLED", True), patch.object(
            proxy.ADMIN_STORE,
            "operator_overrides",
            return_value=overrides,
        ):
            response = await proxy._submit_video_media_job(
                request=_StubJsonRequest(body),  # type: ignore[arg-type]
                body=body,
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["retry-after"], "45")
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(payload["error"]["message"], "reserved for video maintenance")
        self.assertEqual(media_jobs.recent(path=self.db), [])

    async def test_executor_polls_video_route_and_stores_rendered_bytes(self) -> None:
        body = {"model": "ltx-2.3", "prompt": "private video prompt", "seconds": 2}
        with patch.object(proxy, "_schedule_video_media_job", lambda _job_id: True):
            response = await proxy._dispatch_gateway_media_job_submission(
                request=_StubJsonRequest({}),  # type: ignore[arg-type]
                payload={"intent": "video.generate", "request": body},
                audience="client",
            )
        assert response is not None
        job_id = json.loads(response.body.decode("utf-8"))["job_id"]

        async def fake_create(request, _auth_context):
            replayed = await request.json()
            self.assertEqual(replayed["prompt"], "private video prompt")
            return Response(
                content=json.dumps({
                    "id": "video_prompt_123",
                    "status": "queued",
                    "progress": 0,
                }),
                status_code=200,
                media_type="application/json",
            )

        status_calls = 0

        async def fake_status(_video_id, _auth_context):
            nonlocal status_calls
            status_calls += 1
            if status_calls == 1:
                return Response(
                    content=json.dumps({"status": "running", "progress": 44}),
                    status_code=200,
                    media_type="application/json",
                )
            return Response(
                content=json.dumps({"status": "succeeded", "progress": 100}),
                status_code=200,
                media_type="application/json",
            )

        async def fake_content(_video_id, _auth_context, *, index_library=True, metadata_mode=None):
            self.assertFalse(index_library)
            self.assertEqual(metadata_mode, proxy.provenance.METADATA_NONE)
            return Response(
                content=b"video bytes",
                status_code=200,
                media_type="video/mp4",
                headers={"Content-Disposition": 'inline; filename="clip.mp4"'},
            )

        stored_assets: list[dict[str, Any]] = []

        def fake_store_asset_bytes(
            content: bytes,
            *,
            kind: str = "img",
            suffix: str = ".png",
            owner_key_hash: str | None = None,
        ) -> str:
            stored_assets.append(
                {
                    "content": content,
                    "kind": kind,
                    "suffix": suffix,
                    "owner_key_hash": owner_key_hash,
                }
            )
            return "vid_" + "c" * 64

        with patch.object(proxy, "proxy_comfy_video_create", fake_create), patch.object(
            proxy,
            "proxy_comfy_video_status",
            fake_status,
        ), patch.object(
            proxy,
            "proxy_comfy_video_content",
            fake_content,
        ), patch.object(
            proxy,
            "store_asset_bytes",
            fake_store_asset_bytes,
        ):
            await proxy._run_video_media_job(job_id)

        stored = media_jobs.get(job_id, audience="internal", path=self.db)
        assert stored is not None
        self.assertEqual(stored["status"], media_jobs.STATUS_SUCCEEDED)
        self.assertEqual(stored["progress"], 100)
        self.assertEqual(stored["assets"][0]["asset_id"], "vid_" + "c" * 64)
        self.assertEqual(stored["assets"][0]["filename"], "clip.mp4")
        self.assertIn("/v1/assets/vid_", stored["assets"][0]["content_url"])
        self.assertEqual(stored_assets[0]["content"], b"video bytes")
        self.assertEqual(stored_assets[0]["kind"], "vid")
        self.assertEqual(stored_assets[0]["suffix"], ".mp4")
        self.assertIsNone(proxy._load_video_media_job_payload(job_id))
        events = media_jobs.events(job_id, audience="internal", path=self.db)
        self.assertIn("completed", [event["event_type"] for event in events])

    async def test_content_route_returns_signed_video_asset_payload(self) -> None:
        asset_id = "vid_" + "d" * 64
        job = media_jobs.MediaJob(
            intent="video.generate",
            request_id="vid_done",
            status=media_jobs.STATUS_SUCCEEDED,
            progress=100,
            assets=[{
                "asset_id": asset_id,
                "filename": "clip.mp4",
                "media_type": "video/mp4",
                "size_bytes": 11,
                "index": 0,
            }],
            metadata={"mode": "async_video_generation"},
        )
        media_jobs.create(job, path=self.db)

        response = await proxy.gateway_media_job_content(
            job.job_id,
            _StubJsonRequest({}),  # type: ignore[arg-type]
        )

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(payload["object"], "video.generation")
        self.assertEqual(payload["job_id"], job.job_id)
        self.assertEqual(payload["data"][0]["asset_id"], asset_id)
        self.assertEqual(payload["data"][0]["media_type"], "video/mp4")
        self.assertIn(f"/v1/assets/{asset_id}/content", payload["data"][0]["url"])

    def test_resume_pending_video_jobs_reschedules_jobs_with_payloads(self) -> None:
        body = {"model": "ltx-2.3", "prompt": "private video prompt"}
        job = media_jobs.MediaJob(
            intent="video.generate",
            request_id="vid_resume",
            status=media_jobs.STATUS_QUEUED,
            metadata={"mode": "async_video_generation"},
        )
        media_jobs.create(job, path=self.db)
        proxy._persist_video_media_job_payload(
            job.job_id,
            body=body,
            request=_StubJsonRequest(body),  # type: ignore[arg-type]
            auth_context=proxy.NO_AUTH_CONTEXT,
        )
        scheduled: list[str] = []

        with patch.object(
            proxy,
            "_schedule_video_media_job",
            lambda job_id: scheduled.append(job_id) or True,
        ):
            resumed = proxy._resume_pending_video_media_jobs()

        self.assertEqual(resumed, 1)
        self.assertEqual(scheduled, [job.job_id])

    def test_resume_pending_video_jobs_marks_missing_payload_failed(self) -> None:
        job = media_jobs.MediaJob(
            intent="video.generate",
            request_id="vid_missing_payload",
            status=media_jobs.STATUS_RUNNING,
            metadata={"mode": "async_video_generation"},
        )
        media_jobs.create(job, path=self.db)

        resumed = proxy._resume_pending_video_media_jobs()

        self.assertEqual(resumed, 0)
        stored = media_jobs.get(job.job_id, audience="internal", path=self.db)
        assert stored is not None
        self.assertEqual(stored["status"], media_jobs.STATUS_FAILED)
        self.assertEqual(
            stored["error"],
            "Video media job payload missing after proxy restart",
        )


if __name__ == "__main__":
    unittest.main()
