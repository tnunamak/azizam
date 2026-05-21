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


class AudioMediaJobIntegrationTests(unittest.IsolatedAsyncioTestCase):
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
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    async def test_generic_submit_creates_audio_job_without_prompt_or_lyrics_metadata(self) -> None:
        body = {
            "model": "tangoflux",
            "prompt": "private prompt",
            "lyrics": "private lyrics",
            "duration": 8,
            "response_format": "mp3",
        }
        scheduled: list[str] = []

        with patch.object(
            proxy,
            "_schedule_audio_media_job",
            lambda job_id: scheduled.append(job_id) or True,
        ):
            response = await proxy._dispatch_gateway_media_job_submission(
                request=_StubJsonRequest({}),  # type: ignore[arg-type]
                payload={"intent": "music.generate", "parameters": body},
                audience="client",
            )

        assert response is not None
        self.assertEqual(response.status_code, 202)
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(scheduled, [payload["job_id"]])
        stored = media_jobs.get(payload["job_id"], audience="internal", path=self.db)
        assert stored is not None
        self.assertEqual(stored["intent"], "music.generate")
        self.assertEqual(stored["status"], media_jobs.STATUS_QUEUED)
        self.assertNotIn("private prompt", json.dumps(stored["metadata"]))
        self.assertNotIn("private lyrics", json.dumps(stored["metadata"]))

        private_payload = proxy._load_audio_media_job_payload(payload["job_id"])
        assert private_payload is not None
        self.assertEqual(private_payload["body"]["prompt"], "private prompt")
        self.assertEqual(private_payload["body"]["lyrics"], "private lyrics")

    async def test_generic_submit_rejects_unknown_audio_model(self) -> None:
        response = await proxy._dispatch_gateway_media_job_submission(
            request=_StubJsonRequest({}),  # type: ignore[arg-type]
            payload={
                "intent": "audio.generate",
                "request": {"model": "not-a-real-audio-model", "prompt": "tone"},
            },
            audience="client",
        )

        assert response is not None
        self.assertEqual(response.status_code, 400)
        body = json.loads(response.body.decode("utf-8"))
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        self.assertIn("Unknown audio model", body["error"]["message"])

    async def test_direct_audio_submit_uses_resource_admission(self) -> None:
        body = {
            "model": "tangoflux",
            "prompt": "private prompt",
            "duration": 8,
        }

        with patch.object(proxy, "COMFY_ENABLED", False):
            response = await proxy._submit_audio_media_job(
                request=_StubJsonRequest(body),  # type: ignore[arg-type]
                intent="audio.generate",
                body=body,
            )

        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(
            payload["error"]["code"],
            "resource_admission_denied",
        )
        self.assertEqual(media_jobs.recent(path=self.db), [])

    async def test_direct_audio_submit_uses_media_job_admission_overrides(self) -> None:
        body = {
            "model": "tangoflux",
            "prompt": "private prompt",
            "duration": 8,
        }
        overrides = {
            "service_availability": {},
            "media_job_admission": {
                "disabled_intents": ["audio.generate"],
                "retry_after_seconds": 17,
            },
        }

        with patch.object(proxy, "MEDIA_JOB_ADMISSION_CFG", {}), patch.object(
            proxy.ADMIN_STORE,
            "operator_overrides",
            return_value=overrides,
        ):
            response = await proxy._submit_audio_media_job(
                request=_StubJsonRequest(body),  # type: ignore[arg-type]
                intent="audio.generate",
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

    async def test_direct_audio_submit_uses_service_availability_overrides(self) -> None:
        body = {
            "model": "tangoflux",
            "prompt": "private prompt",
            "duration": 8,
        }
        overrides = {
            "service_availability": {
                "comfyui": {
                    "state": "maintenance",
                    "reason": "reserved for audio maintenance",
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
            response = await proxy._submit_audio_media_job(
                request=_StubJsonRequest(body),  # type: ignore[arg-type]
                intent="audio.generate",
                body=body,
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["retry-after"], "45")
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(payload["error"]["message"], "reserved for audio maintenance")
        self.assertEqual(media_jobs.recent(path=self.db), [])

    async def test_executor_uses_audio_route_and_stores_returned_bytes_as_audio_asset(self) -> None:
        body = {
            "model": "tangoflux",
            "prompt": "private prompt",
            "type": "sfx",
            "response_format": "mp3",
        }
        with patch.object(proxy, "_schedule_audio_media_job", lambda _job_id: True):
            response = await proxy._dispatch_gateway_media_job_submission(
                request=_StubJsonRequest({}),  # type: ignore[arg-type]
                payload={"intent": "sfx.generate", "request": body},
                audience="client",
            )
        assert response is not None
        job_id = json.loads(response.body.decode("utf-8"))["job_id"]

        async def fake_proxy_audio_generate(request, _auth_context):
            replayed = await request.json()
            self.assertEqual(replayed["prompt"], "private prompt")
            return Response(
                content=b"audio bytes",
                status_code=200,
                media_type="audio/mpeg",
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
            return "aud_" + "a" * 64

        with patch.object(proxy, "proxy_audio_generate", fake_proxy_audio_generate), patch.object(
            proxy,
            "store_asset_bytes",
            fake_store_asset_bytes,
        ):
            await proxy._run_audio_media_job(job_id)

        stored = media_jobs.get(job_id, audience="internal", path=self.db)
        assert stored is not None
        self.assertEqual(stored["status"], media_jobs.STATUS_SUCCEEDED)
        self.assertEqual(stored["progress"], 100)
        self.assertEqual(stored["assets"][0]["asset_id"], "aud_" + "a" * 64)
        self.assertEqual(stored["assets"][0]["media_type"], "audio/mpeg")
        self.assertEqual(stored_assets[0]["content"], b"audio bytes")
        self.assertEqual(stored_assets[0]["kind"], "aud")
        self.assertEqual(stored_assets[0]["suffix"], ".mp3")
        self.assertIsNone(proxy._load_audio_media_job_payload(job_id))
        events = media_jobs.events(job_id, audience="internal", path=self.db)
        self.assertIn("completed", [event["event_type"] for event in events])

    async def test_content_route_returns_signed_audio_asset_payload(self) -> None:
        asset_id = "aud_" + "b" * 64
        job = media_jobs.MediaJob(
            intent="music.generate",
            request_id="aud_done",
            status=media_jobs.STATUS_SUCCEEDED,
            progress=100,
            assets=[{
                "asset_id": asset_id,
                "media_type": "audio/mpeg",
                "size_bytes": 11,
                "index": 0,
            }],
            metadata={"mode": "async_audio_generation"},
        )
        media_jobs.create(job, path=self.db)

        response = await proxy.gateway_media_job_content(
            job.job_id,
            _StubJsonRequest({}),  # type: ignore[arg-type]
        )

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(payload["object"], "audio.generation")
        self.assertEqual(payload["job_id"], job.job_id)
        self.assertEqual(payload["data"][0]["asset_id"], asset_id)
        self.assertEqual(payload["data"][0]["media_type"], "audio/mpeg")
        self.assertIn(f"/v1/assets/{asset_id}/content", payload["data"][0]["url"])

    def test_resume_pending_audio_jobs_reschedules_jobs_with_payloads(self) -> None:
        body = {"model": "tangoflux", "prompt": "private prompt"}
        job = media_jobs.MediaJob(
            intent="audio.generate",
            request_id="aud_resume",
            status=media_jobs.STATUS_QUEUED,
            metadata={"mode": "async_audio_generation"},
        )
        media_jobs.create(job, path=self.db)
        proxy._persist_audio_media_job_payload(
            job.job_id,
            body=body,
            request=_StubJsonRequest(body),  # type: ignore[arg-type]
            auth_context=proxy.NO_AUTH_CONTEXT,
        )
        scheduled: list[str] = []

        with patch.object(
            proxy,
            "_schedule_audio_media_job",
            lambda job_id: scheduled.append(job_id) or True,
        ):
            resumed = proxy._resume_pending_audio_media_jobs()

        self.assertEqual(resumed, 1)
        self.assertEqual(scheduled, [job.job_id])

    def test_resume_pending_audio_jobs_marks_missing_payload_failed(self) -> None:
        job = media_jobs.MediaJob(
            intent="sfx.generate",
            request_id="aud_missing_payload",
            status=media_jobs.STATUS_RUNNING,
            metadata={"mode": "async_audio_generation"},
        )
        media_jobs.create(job, path=self.db)

        resumed = proxy._resume_pending_audio_media_jobs()

        self.assertEqual(resumed, 0)
        stored = media_jobs.get(job.job_id, audience="internal", path=self.db)
        assert stored is not None
        self.assertEqual(stored["status"], media_jobs.STATUS_FAILED)
        self.assertEqual(
            stored["error"],
            "Audio media job payload missing after proxy restart",
        )


if __name__ == "__main__":
    unittest.main()
