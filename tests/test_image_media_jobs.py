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
from tests.support.access import authenticated, access_context  # noqa: E402
from gateway import media_jobs  # noqa: E402


class _StubJsonRequest:
    method = "POST"
    # The auth context the middleware would have put on the request.
    scope: dict[str, Any] = {"auth_context": access_context()}
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

    def test_comfy_models_are_freed_only_when_queue_is_idle(self) -> None:
        import asyncio
        class FakeResponse:
            status_code = 200

            def __init__(self, payload: Mapping[str, Any]) -> None:
                self.payload = payload

            def json(self) -> Mapping[str, Any]:
                return self.payload

        class FakeClient:
            def __init__(self, running: bool) -> None:
                self.running = running
                self.posts: list[tuple[str, Mapping[str, Any]]] = []

            async def get(self, _url: str, **_kwargs: Any) -> FakeResponse:
                return FakeResponse({"queue_running": [1] if self.running else [], "queue_pending": []})

            async def post(self, url: str, **kwargs: Any) -> FakeResponse:
                self.posts.append((url, kwargs["json"]))
                return FakeResponse({})

        idle = FakeClient(False)
        with patch.object(proxy.asyncio, "sleep", return_value=None):
            self.assertTrue(asyncio.run(proxy._free_graph_models_if_idle(idle)))
        self.assertEqual(len(idle.posts), 1)
        self.assertEqual(idle.posts[0][1], {"unload_models": True, "free_memory": True})

        busy = FakeClient(True)
        self.assertFalse(asyncio.run(proxy._free_graph_models_if_idle(busy)))
        self.assertEqual(busy.posts, [])

        class PendingClient(FakeClient):
            async def get(self, _url: str, **_kwargs: Any) -> FakeResponse:
                return FakeResponse({"queue_running": [], "queue_pending": [1]})

        pending = PendingClient(False)
        self.assertFalse(asyncio.run(proxy._free_graph_models_if_idle(pending)))
        self.assertEqual(pending.posts, [])

        class UnknownQueueClient(FakeClient):
            async def get(self, _url: str, **_kwargs: Any) -> FakeResponse:
                return FakeResponse({"queue_running": []})

        unknown = UnknownQueueClient(False)
        self.assertFalse(asyncio.run(proxy._free_graph_models_if_idle(unknown)))
        self.assertEqual(unknown.posts, [])

        class DroppedClient(FakeClient):
            async def get(self, _url: str, **_kwargs: Any) -> FakeResponse:
                raise proxy.httpx.ReadTimeout("slow")

        import contextlib
        import io

        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            self.assertFalse(asyncio.run(proxy._free_graph_models_if_idle(DroppedClient(False))))
        # The log line names the httpx error, as it did before the call moved behind the adapter.
        self.assertIn("model unload failed: ReadTimeout", printed.getvalue())

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
            auth_context=access_context(),
            idempotency_key=None,
            fallback_request_id="req_one",
        )
        second, _, second_kind = proxy._image_media_job_request_id(
            body=body,
            auth_context=access_context(),
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
            auth_context=access_context(),
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
            # The worker rebuilds the submitting App from its key, as in production.
            patch.object(
                proxy,
                "runtime_identity_for_app_credential_hash",
                lambda key_hash, **_kw: access_context().identity if key_hash == "sha256:test" else None,
            ),
            patch.object(
                proxy.ADMIN_STORE,
                "operator_overrides",
                return_value={"service_availability": {}, "media_job_admission": {}},
            ),
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
            auth_context=access_context(),
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

        with patch.object(proxy, "proxy_graph_image_request", fake_proxy_comfy_request):
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

        with patch.object(proxy, "proxy_graph_image_request", fake_proxy_comfy_request):
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

        with authenticated(), patch.object(
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

        with authenticated(), patch.object(
            proxy,
            "_schedule_image_media_job",
            lambda job_id: scheduled.append(job_id) or True,
        ):
            second = await proxy.gateway_image_generations_media_job(
                _StubJsonRequest(body, headers={"host": "gateway.test"})  # type: ignore[arg-type]
            )
        self.assertEqual(second.status_code, 202)
        self.assertEqual(json.loads(second.body.decode("utf-8"))["job_id"], job_id)

    async def test_seeded_resubmission_after_replay_window_runs_again(self) -> None:
        body = {
            "model": "flux2-klein-4b",
            "prompt": "private prompt",
            "extensions": {proxy.WORKFLOW_EXTENSION_NAMESPACE: {"seed": 7}},
        }

        async def submit() -> str:
            with authenticated(), patch.object(
                proxy, "_schedule_image_media_job", lambda job_id: True,
            ):
                response = await proxy.gateway_image_generations_media_job(
                    _StubJsonRequest(body, headers={"host": "gateway.test"})  # type: ignore[arg-type]
                )
            self.assertEqual(response.status_code, 202)
            return json.loads(response.body.decode("utf-8"))["job_id"]

        first = await submit()
        with media_jobs._conn(self.db) as conn:
            conn.execute(
                "UPDATE media_jobs SET status = ?, updated_at = updated_at - ? WHERE job_id = ?",
                (media_jobs.STATUS_FAILED, media_jobs.REPLAY_WINDOW_S + 1, first),
            )
            conn.commit()
        second = await submit()
        self.assertNotEqual(second, first)
        self.assertIsNotNone(media_jobs.get(first, audience="internal", path=self.db))

    async def test_generic_media_jobs_submit_delegates_image_generate_request(self) -> None:
        body = {
            "model": "flux2-klein-4b",
            "prompt": "private prompt",
        }
        scheduled: list[str] = []

        with authenticated(), patch.object(
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

    async def test_generic_media_jobs_rejects_when_comfy_resource_is_disabled(self) -> None:
        body = {
            "model": "flux2-klein-4b",
            "prompt": "private prompt",
        }

        with authenticated(), patch.object(
            proxy,
            "COMFY_ENABLED",
            False,
        ):
            response = await proxy._dispatch_gateway_media_job_submission(
                request=_StubJsonRequest({}),  # type: ignore[arg-type]
                payload={"intent": "image.generate", "request": body},
                audience="client",
            )

        assert response is not None
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["retry-after"], "30")
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(
            payload["error"]["code"],
            "resource_admission_denied",
        )
        self.assertEqual(
            payload["error"]["message"],
            "This service is not available right now.",
        )
        self.assertEqual(media_jobs.recent(path=self.db), [])

    async def test_generic_media_jobs_rejects_when_configured_queue_is_full(self) -> None:
        existing = media_jobs.MediaJob(
            intent="video.generate",
            request_id="existing_media",
            status=media_jobs.STATUS_QUEUED,
            metadata={"mode": "async_video_generation"},
        )
        media_jobs.create(existing, path=self.db)
        body = {
            "model": "flux2-klein-4b",
            "prompt": "private prompt",
        }

        with authenticated(), patch.object(
            proxy,
            "MEDIA_JOB_ADMISSION_CFG",
            {"max_queue_depth": 1, "queue_retry_after_seconds": 9},
        ):
            response = await proxy._dispatch_gateway_media_job_submission(
                request=_StubJsonRequest({}),  # type: ignore[arg-type]
                payload={"intent": "image.generate", "request": body},
                audience="client",
            )

        assert response is not None
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["retry-after"], "9")
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(
            payload["error"]["message"],
            "Shared capacity is busy.",
        )
        self.assertEqual(len(media_jobs.recent(path=self.db)), 1)

    async def test_media_job_admission_uses_admin_runtime_overrides(self) -> None:
        body = {
            "model": "flux2-klein-4b",
            "prompt": "private prompt",
        }
        overrides = {
            "service_availability": {},
            "media_job_admission": {
                "disabled_intents": ["image.generate"],
                "retry_after_seconds": 17,
                "reserved_tokens": ["gpu"],
            },
        }

        with authenticated(), patch.object(
            proxy,
            "MEDIA_JOB_ADMISSION_CFG",
            {},
        ), patch.object(
            proxy.ADMIN_STORE,
            "operator_overrides",
            return_value=overrides,
        ):
            response = await proxy._dispatch_gateway_media_job_submission(
                request=_StubJsonRequest({}),  # type: ignore[arg-type]
                payload={"intent": "image.generate", "request": body},
                audience="client",
            )

        assert response is not None
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["retry-after"], "17")
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(
            payload["error"]["code"],
            "resource_admission_denied",
        )
        self.assertEqual(media_jobs.recent(path=self.db), [])

    async def test_media_job_admission_uses_admin_service_mode(self) -> None:
        body = {
            "model": "flux2-klein-4b",
            "prompt": "private prompt",
        }
        overrides = {
            "service_availability": {
                "comfyui": {
                    "state": "maintenance",
                    "reason": "reserved for LLM work",
                    "retry_after_seconds": 45,
                }
            },
            "media_job_admission": {},
        }

        with authenticated(), patch.object(
            proxy,
            "COMFY_ENABLED",
            True,
        ), patch.object(
            proxy.ADMIN_STORE,
            "operator_overrides",
            return_value=overrides,
        ):
            response = await proxy._dispatch_gateway_media_job_submission(
                request=_StubJsonRequest({}),  # type: ignore[arg-type]
                payload={"intent": "image.generate", "request": body},
                audience="client",
            )

        assert response is not None
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["retry-after"], "45")
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(payload["error"]["message"], "This service is not available right now.")
        self.assertEqual(media_jobs.recent(path=self.db), [])

    async def test_queue_admission_observes_at_least_the_configured_depth(self) -> None:
        for index in range(3):
            media_jobs.create(
                media_jobs.MediaJob(
                    intent="image.generate",
                    request_id=f"existing_media_{index}",
                    status=media_jobs.STATUS_QUEUED,
                    metadata={"mode": "async_image_generation"},
                ),
                path=self.db,
            )
        body = {
            "model": "flux2-klein-4b",
            "prompt": "private prompt",
        }

        with authenticated(), patch.object(
            proxy,
            "MEDIA_JOB_ADMISSION_CFG",
            {
                "max_queue_depth": 3,
                "scan_limit": 1,
                "queue_retry_after_seconds": 9,
            },
        ):
            response = await proxy._dispatch_gateway_media_job_submission(
                request=_StubJsonRequest({}),  # type: ignore[arg-type]
                payload={"intent": "image.generate", "request": body},
                audience="client",
            )

        assert response is not None
        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(
            payload["error"]["message"],
            "Shared capacity is busy.",
        )
        self.assertEqual(len(media_jobs.recent(path=self.db)), 3)

    async def test_generic_media_jobs_rejects_unsupported_executable_intent(self) -> None:
        with authenticated():
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
            app_id="app_test",  # the App the test key belongs to
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

        with authenticated():
            response = await proxy.gateway_media_job_content(
                job.job_id,
                _StubJsonRequest({}),  # type: ignore[arg-type]
            )

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(payload["job_id"], job.job_id)
        self.assertEqual(payload["data"][0]["asset_id"], "img_" + "c" * 64)
