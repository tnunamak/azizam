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


class ImageMediaJobHelpersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.secret_patch = patch.object(
            proxy,
            "_image_url_signing_secret",
            b"test-secret",
        )
        self.secret_patch.start()
        self.addCleanup(self.secret_patch.stop)

    def test_seeded_requests_get_stable_hashed_request_id(self) -> None:
        body = {
            "model": "flux2-klein-4b",
            "prompt": "private prompt",
            "extensions": {
                proxy.WORKFLOW_EXTENSION_NAMESPACE: {
                    "seed": 1234,
                }
            },
        }

        first, digest, kind = proxy._image_media_job_request_id(
            body=body,
            auth_context=proxy.NO_AUTH_CONTEXT,
            idempotency_key=None,
            fallback_request_id="req_one",
        )
        second, _, second_kind = proxy._image_media_job_request_id(
            body=body,
            auth_context=proxy.NO_AUTH_CONTEXT,
            idempotency_key=None,
            fallback_request_id="req_two",
        )

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("imgjob_seeded_"))
        self.assertEqual(kind, "seeded_request")
        self.assertEqual(second_kind, "seeded_request")
        self.assertNotIn("private prompt", first)
        self.assertNotIn("private prompt", digest)

    def test_unseeded_requests_use_fallback_without_idempotency_key(self) -> None:
        body = {"model": "flux2-klein-4b", "prompt": "private prompt"}

        request_id, digest, kind = proxy._image_media_job_request_id(
            body=body,
            auth_context=proxy.NO_AUTH_CONTEXT,
            idempotency_key=None,
            fallback_request_id="req_unique",
        )

        self.assertEqual(request_id, "req_unique")
        self.assertEqual(kind, "request")
        self.assertNotIn("private prompt", digest)

    def test_retry_decision_covers_vram_and_tile_composition(self) -> None:
        vram = Response(
            content=json.dumps({
                "error": {
                    "message": "Insufficient free VRAM",
                    "code": "vram_exhausted",
                }
            }),
            status_code=503,
            media_type="application/json",
            headers={"Retry-After": "120"},
        )
        tile = Response(
            content=json.dumps({
                "error": {
                    "message": "Gateway tile composition error: fetch failed",
                }
            }),
            status_code=502,
            media_type="application/json",
        )
        invalid = Response(
            content=json.dumps({"error": {"message": "prompt is required"}}),
            status_code=400,
            media_type="application/json",
        )

        self.assertEqual(
            proxy._image_media_job_retry_decision(vram, attempt=1, max_attempts=3),
            ("vram_exhausted", proxy.IMAGE_MEDIA_JOB_RETRY_MAX_SECONDS),
        )
        self.assertEqual(
            proxy._image_media_job_retry_decision(tile, attempt=1, max_attempts=3)[0],
            "tile_composition_error",
        )
        self.assertIsNone(
            proxy._image_media_job_retry_decision(invalid, attempt=1, max_attempts=3)
        )
        self.assertIsNone(
            proxy._image_media_job_retry_decision(vram, attempt=3, max_attempts=3)
        )


class ImageMediaJobExecutionTests(unittest.IsolatedAsyncioTestCase):
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
            patch.object(proxy, "IMAGE_MEDIA_JOB_MAX_ATTEMPTS", 3),
            patch.object(proxy, "IMAGE_MEDIA_JOB_RETRY_BASE_SECONDS", 0),
            patch.object(proxy, "IMAGE_MEDIA_JOB_RETRY_MAX_SECONDS", 0),
            patch.object(proxy, "_image_url_signing_secret", b"test-secret"),
            patch.object(proxy, "finish_image_route_receipt", lambda *_a, **_kw: None),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def _create_job(self, body: Mapping[str, Any]) -> str:
        job = media_jobs.MediaJob(
            intent="image.generate",
            workflow="image.generate.flux2-klein-4b",
            backend="comfyui",
            request_id="req_job",
            metadata={
                "mode": "async_image_generation",
                "request_hash": "abc123",
                "idempotency": "request",
            },
        )
        media_jobs.create(job, path=self.db)
        request = _StubJsonRequest(body)
        proxy._persist_image_media_job_payload(
            job.job_id,
            body=body,
            request=request,  # type: ignore[arg-type]
            auth_context=proxy.NO_AUTH_CONTEXT,
        )
        return job.job_id

    async def test_executor_forces_url_response_and_persists_assets(self) -> None:
        job_id = self._create_job({
            "model": "flux2-klein-4b",
            "prompt": "private prompt",
            "response_format": "b64_json",
        })

        async def fake_proxy_comfy_request(request, _auth_context, receipt=None):
            body = await request.json()
            self.assertEqual(body["response_format"], "url")
            return Response(
                content=json.dumps({
                    "created": 1,
                    "data": [{
                        "asset_id": "img_" + "a" * 64,
                        "asset_url": "http://gateway.test/v1/assets/img/content",
                        "url": "http://gateway.test/v1/images/img/content",
                        "media_type": "image/png",
                    }],
                }),
                status_code=200,
                media_type="application/json",
            )

        with patch.object(proxy, "proxy_comfy_request", fake_proxy_comfy_request):
            await proxy._run_image_media_job(job_id)

        stored = media_jobs.get(job_id, audience="internal", path=self.db)
        assert stored is not None
        self.assertEqual(stored["status"], media_jobs.STATUS_SUCCEEDED)
        self.assertEqual(stored["progress"], 100)
        self.assertEqual(stored["assets"][0]["asset_id"], "img_" + "a" * 64)
        self.assertFalse(proxy._image_media_job_payload_path(job_id).exists())

    async def test_executor_retries_transient_vram_error(self) -> None:
        job_id = self._create_job({
            "model": "flux2-klein-4b",
            "prompt": "private prompt",
        })
        calls = 0

        async def fake_proxy_comfy_request(_request, _auth_context, receipt=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                return Response(
                    content=json.dumps({
                        "error": {
                            "message": "Insufficient free VRAM",
                            "code": "vram_exhausted",
                        }
                    }),
                    status_code=503,
                    media_type="application/json",
                    headers={"Retry-After": "0"},
                )
            return Response(
                content=json.dumps({
                    "data": [{
                        "asset_id": "img_" + "b" * 64,
                        "url": "http://gateway.test/v1/images/img/content",
                        "media_type": "image/png",
                    }]
                }),
                status_code=200,
                media_type="application/json",
            )

        with patch.object(proxy, "proxy_comfy_request", fake_proxy_comfy_request):
            await proxy._run_image_media_job(job_id)

        stored = media_jobs.get(job_id, audience="internal", path=self.db)
        assert stored is not None
        self.assertEqual(calls, 2)
        self.assertEqual(stored["status"], media_jobs.STATUS_SUCCEEDED)
        events = media_jobs.events(job_id, audience="internal", path=self.db)
        self.assertIn("retry", [event["event_type"] for event in events])

    async def test_submit_route_creates_job_without_persisting_prompt_in_metadata(self) -> None:
        body = {
            "model": "flux2-klein-4b",
            "prompt": "private prompt",
            "extensions": {
                proxy.WORKFLOW_EXTENSION_NAMESPACE: {
                    "seed": 42,
                }
            },
        }
        scheduled: list[str] = []

        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy,
            "_schedule_image_media_job",
            lambda job_id: scheduled.append(job_id) or True,
        ):
            response = await proxy.gateway_image_generations_media_job(
                _StubJsonRequest(body, headers={"host": "gateway.test"})  # type: ignore[arg-type]
            )

        self.assertEqual(response.status_code, 202)
        payload = json.loads(response.body.decode("utf-8"))
        job_id = payload["job_id"]
        self.assertEqual(scheduled, [job_id])
        stored = media_jobs.get(job_id, audience="internal", path=self.db)
        assert stored is not None
        self.assertEqual(stored["status"], media_jobs.STATUS_QUEUED)
        self.assertNotIn("prompt", json.dumps(stored["metadata"]))

        private_payload = proxy._load_image_media_job_payload(job_id)
        assert private_payload is not None
        self.assertEqual(private_payload["body"]["prompt"], "private prompt")

        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy,
            "_schedule_image_media_job",
            lambda job_id: scheduled.append(job_id) or True,
        ):
            second = await proxy.gateway_image_generations_media_job(
                _StubJsonRequest(body, headers={"host": "gateway.test"})  # type: ignore[arg-type]
            )
        self.assertEqual(second.status_code, 202)
        self.assertEqual(json.loads(second.body.decode("utf-8"))["job_id"], job_id)

    async def test_generic_media_jobs_submit_delegates_image_generate_request(self) -> None:
        body = {
            "model": "flux2-klein-4b",
            "prompt": "private prompt",
        }
        scheduled: list[str] = []

        with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
            proxy,
            "_schedule_image_media_job",
            lambda job_id: scheduled.append(job_id) or True,
        ):
            response = await proxy._dispatch_gateway_media_job_submission(
                request=_StubJsonRequest({}),  # type: ignore[arg-type]
                payload={"intent": "image.generate", "request": body},
                audience="client",
            )

        assert response is not None
        self.assertEqual(response.status_code, 202)
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(scheduled, [payload["job_id"]])
        stored = media_jobs.get(payload["job_id"], audience="internal", path=self.db)
        assert stored is not None
        self.assertEqual(stored["intent"], "image.generate")

    async def test_generic_media_jobs_rejects_unsupported_executable_intent(self) -> None:
        with patch.object(proxy, "AUTH_ENABLED", False):
            response = await proxy._dispatch_gateway_media_job_submission(
                request=_StubJsonRequest({}),  # type: ignore[arg-type]
                payload={
                    "intent": "avatar.generate",
                    "request": {"model": "avatar-local", "prompt": "private"},
                },
                audience="client",
            )

        assert response is not None
        self.assertEqual(response.status_code, 400)
        body = json.loads(response.body.decode("utf-8"))
        self.assertEqual(body["error"]["code"], "unsupported_media_job_intent")
        self.assertEqual(
            body["error"]["supported_intents"],
            [
                "image.generate",
                "audio.generate",
                "music.generate",
                "sfx.generate",
                "video.generate",
            ],
        )

    async def test_content_route_returns_openai_image_payload_for_succeeded_job(self) -> None:
        job = media_jobs.MediaJob(
            intent="image.generate",
            request_id="req_done",
            status=media_jobs.STATUS_SUCCEEDED,
            progress=100,
            assets=[{
                "asset_id": "img_" + "c" * 64,
                "url": "http://gateway.test/v1/images/img/content",
                "media_type": "image/png",
            }],
            metadata={"mode": "async_image_generation"},
        )
        media_jobs.create(job, path=self.db)

        with patch.object(proxy, "AUTH_ENABLED", False):
            response = await proxy.gateway_media_job_content(
                job.job_id,
                _StubJsonRequest({}),  # type: ignore[arg-type]
            )

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(payload["job_id"], job.job_id)
        self.assertEqual(payload["data"][0]["asset_id"], "img_" + "c" * 64)
