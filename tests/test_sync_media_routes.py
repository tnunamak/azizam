from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from io import BytesIO
from unittest.mock import patch

from PIL import Image

from starlette.responses import Response, StreamingResponse
from starlette.testclient import TestClient

import proxy
from gateway import media_ingestion, media_jobs, receipt_store, provenance
from gateway.receipts import RouteReceipt


def _png_bytes() -> bytes:
    out = BytesIO()
    Image.new("RGBA", (1, 1), (1, 2, 3, 255)).save(out, format="PNG")
    return out.getvalue()


def _wav_bytes() -> bytes:
    import wave

    out = BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x00\x00" * 12)
    return out.getvalue()


class _FakeAsyncClient:
    posts: list[dict[str, object]] = []
    response_content = b""
    response_headers = {"content-type": "audio/wav"}

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, url: str, *, headers=None, content=None):
        self.posts.append({"url": url, "headers": dict(headers or {}), "content": content})
        return proxy.httpx.Response(200, content=self.response_content, headers=self.response_headers)


class _FakeVideoAsyncClient:
    prompt_id = "prompt_sync_video"
    video_content = b"video-bytes"

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def get(self, url: str, **kwargs):
        if url.endswith(f"/history/{self.prompt_id}"):
            return proxy.httpx.Response(
                200,
                json={
                    self.prompt_id: {
                        "status": {"completed": True},
                        "outputs": {
                            "save": {
                                "videos": [{"filename": "sync-video.mp4", "type": "output", "subfolder": ""}]
                            }
                        },
                    }
                },
            )
        if url.endswith("/view"):
            return proxy.httpx.Response(200, content=self.video_content, headers={"content-type": "video/mp4"})
        return proxy.httpx.Response(404, content=b"")


class _StubUrlRequest:
    def __init__(self, *, query_params=None, headers=None) -> None:
        self.query_params = dict(query_params or {})
        self.headers = dict(headers or {})
        self.url = SimpleNamespace(path="/v1/test", scheme="http", netloc="gateway.test")


class SyncMediaRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = self.root / "cache"
        self.media_db = self.root / "media_jobs.db"
        self.receipt_db = self.root / "receipts.db"
        media_jobs.init_db(self.media_db)
        receipt_store.init_db(self.receipt_db)
        patches = [
            patch.object(media_jobs, "DB_PATH", self.media_db),
            patch.object(receipt_store, "DB_PATH", self.receipt_db),
            patch.object(proxy, "IMAGE_CACHE_DIR", self.cache),
            patch.object(proxy, "_image_url_signing_secret", b"sync-media-test-secret"),
            patch.object(proxy, "AUTH_ENABLED", False),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_index_receipt_assets_sets_job_id_and_does_not_duplicate_existing_job(self) -> None:
        receipt = RouteReceipt(
            protocol="openai.images",
            intent="image.generate",
            request_id="req_sync_image",
            app_profile="owner-default",
            app_id="app_1",
            owner_key_hash="sha256:owner",
            api_key_name="Owner app",
        )
        asset_id = "img_" + "a" * 64

        first = media_ingestion.index_receipt_assets(
            receipt,
            [{"asset_id": asset_id, "media_type": "image/png", "width": 16, "height": 16}],
        )
        second = media_ingestion.index_receipt_assets(
            receipt,
            [{"asset_id": asset_id, "media_type": "image/png"}],
        )

        self.assertIsNotNone(first)
        self.assertIsNone(second)  # once linked to a trusted job id, indexing is executor-owned.
        assert first is not None
        self.assertEqual(receipt.job_id, first.job_id)
        rows = media_jobs.recent(path=self.media_db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["assets"][0]["asset_id"], asset_id)
        self.assertEqual(rows[0]["app_id"], "app_1")


    def test_index_receipt_assets_skips_when_executor_receipt_already_has_job_id(self) -> None:
        receipt = RouteReceipt(
            protocol="openai.images",
            intent="image.generate",
            request_id="req_async_attempt",
            job_id="mj_existing",
        )
        asset_id = "img_" + "d" * 64

        job = media_ingestion.index_receipt_assets(
            receipt,
            [{"asset_id": asset_id, "media_type": "image/png"}],
        )

        self.assertIsNone(job)
        self.assertEqual(media_jobs.recent(path=self.media_db), [])
        self.assertEqual(receipt.job_id, "mj_existing")

    def test_store_audio_response_stores_indexes_and_wraps_raw_pcm_as_wav(self) -> None:
        receipt = RouteReceipt(
            protocol="openai.audio",
            intent="speech.synthesis",
            request_id="req_sync_speech",
            path="/v1/audio/speech",
            owner_key_hash="sha256:owner",
            api_key_name="Speech app",
        )
        stored: dict[str, object] = {}

        def store_asset(content: bytes, *, kind: str, suffix: str, owner_key_hash: str | None) -> str:
            stored.update(content=content, kind=kind, suffix=suffix, owner_key_hash=owner_key_hash)
            return f"{kind}_{hashlib.sha256(content).hexdigest()}"

        assets = media_ingestion.store_audio_response(
            b"\x01\x00\x02\x00",
            "audio/pcm;rate=24000",
            receipt,
            store_asset=store_asset,
        )

        self.assertEqual(stored["kind"], "aud")
        self.assertEqual(stored["suffix"], ".wav")
        self.assertEqual(stored["owner_key_hash"], "sha256:owner")
        self.assertTrue(bytes(stored["content"]).startswith(b"RIFF"))
        self.assertEqual(assets[0]["media_type"], "audio/wav")
        row = media_jobs.get_by_request_id("req_sync_speech", path=self.media_db)
        assert row is not None
        self.assertEqual(receipt.job_id, row["job_id"])
        self.assertEqual(row["intent"], "speech.synthesis")
        self.assertEqual(row["assets"][0]["asset_id"], assets[0]["asset_id"])
        self.assertEqual(row["owner_key_hash"], "sha256:owner")

    def test_build_image_response_payload_indexes_current_delivery_receipt(self) -> None:
        receipt = RouteReceipt(
            protocol="openai.images",
            intent="image.generate",
            request_id="req_image_builder",
            app_profile="owner-default",
            app_id="app_image",
            owner_key_hash="sha256:owner",
        )
        proxy.media_delivery.begin(profile_default="full", receipt=receipt)
        request = _StubUrlRequest(headers={"host": "gateway.test"})

        response = proxy._build_image_response_payload(
            request,  # type: ignore[arg-type]
            [_png_bytes()],
            "url",
            {},
            proxy.NO_AUTH_CONTEXT,
        )

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body.decode("utf-8"))
        asset_id = payload["data"][0]["asset_id"]
        row = media_jobs.get_by_request_id("req_image_builder", path=self.media_db)
        assert row is not None
        self.assertEqual(row["assets"][0]["asset_id"], asset_id)
        self.assertEqual(row["assets"][0]["media_type"], "image/png")
        self.assertEqual(row["app_id"], "app_image")

    def test_signed_audio_content_supports_range_and_metadata_mode_signature(self) -> None:
        raw = b"RIFF" + b"0123456789abcdef" + b"WAVE"
        asset_id = proxy.store_asset_bytes(raw, kind="aud", suffix=".wav", owner_key_hash="sha256:owner")
        expires = int(time.time()) + 60
        sig_full = proxy._sign_asset_token(asset_id, expires, "full")
        request = _StubUrlRequest(
            query_params={"expires": str(expires), "md": "full", "sig": sig_full},
            headers={"range": "bytes=4-9"},
        )

        ranged = proxy._signed_asset_content_response(
            asset_id,
            request,  # type: ignore[arg-type]
            invalid_message="Invalid asset id",
            missing_message="Asset not found",
        )

        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(ranged.media_type, "audio/wav")
        self.assertEqual(ranged.body, raw[4:10])
        self.assertEqual(ranged.headers["content-range"], f"bytes 4-9/{len(raw)}")
        self.assertEqual(ranged.headers["accept-ranges"], "bytes")

        sig_none = proxy._sign_asset_token(asset_id, expires, "none")
        bad_request = _StubUrlRequest(query_params={"expires": str(expires), "md": "full", "sig": sig_none})
        bad = proxy._signed_asset_content_response(
            asset_id,
            bad_request,  # type: ignore[arg-type]
            invalid_message="Invalid asset id",
            missing_message="Asset not found",
        )
        self.assertEqual(bad.status_code, 403)



    def test_image_media_job_executor_uses_existing_job_without_sync_duplicate(self) -> None:
        import anyio

        body = {
            "model": "flux2-klein-4b",
            "prompt": "private prompt",
            "response_format": "b64_json",
        }
        job = media_jobs.MediaJob(
            intent="image.generate",
            workflow="image.generate.flux2-klein-4b",
            backend="comfyui",
            request_id="req_existing_image_job",
            status=media_jobs.STATUS_QUEUED,
            metadata={"mode": "async_image_generation"},
        )
        created = media_jobs.create(job, path=self.media_db)

        class PayloadRequest:
            method = "POST"
            query_params: dict[str, str] = {}
            scope: dict[str, object] = {}
            headers = {"host": "gateway.test"}
            url = SimpleNamespace(scheme="http", netloc="gateway.test")

            async def json(self):
                return dict(body)

        payload_dir = self.root / "image-payloads"
        seen_receipt_job_ids: list[str | None] = []

        async def fake_proxy_comfy_request(request, auth_context, receipt=None):
            self.assertIsNotNone(receipt)
            seen_receipt_job_ids.append(receipt.job_id)
            body_seen = await request.json()
            self.assertEqual(body_seen["response_format"], "url")
            proxy._begin_media_delivery(auth_context, receipt)
            return proxy._build_image_response_payload(
                request,
                [_png_bytes()],
                "url",
                {},
                auth_context,
            )

        with patch.object(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", payload_dir), \
             patch.object(proxy, "IMAGE_MEDIA_JOB_MAX_ATTEMPTS", 1), \
             patch.object(proxy, "finish_image_route_receipt", lambda *_args, **_kwargs: None), \
             patch.object(proxy, "proxy_comfy_request", fake_proxy_comfy_request):
            proxy._persist_image_media_job_payload(
                created.job_id,
                body=body,
                request=PayloadRequest(),  # type: ignore[arg-type]
                auth_context=proxy.NO_AUTH_CONTEXT,
            )
            anyio.run(proxy._run_image_media_job, created.job_id)

        self.assertEqual(seen_receipt_job_ids, [created.job_id])
        rows = media_jobs.recent(path=self.media_db, audience="internal")
        self.assertEqual(len(rows), 1)
        stored = media_jobs.get(created.job_id, audience="internal", path=self.media_db)
        assert stored is not None
        self.assertEqual(stored["status"], media_jobs.STATUS_SUCCEEDED)
        self.assertEqual(stored["progress"], 100)
        self.assertEqual(stored["request_id"], "req_existing_image_job")
        self.assertTrue(stored["assets"][0]["asset_id"].startswith("img_"))
        self.assertEqual(stored["assets"][0]["media_type"], "image/png")

    def test_public_audio_speech_route_indexes_buffered_response_without_changing_delivered_bytes(self) -> None:
        upstream = _wav_bytes()
        _FakeAsyncClient.posts = []
        _FakeAsyncClient.response_content = upstream
        captured_provenance: list[dict[str, object]] = []

        def record_provenance(content: bytes, suffix: str, record):
            captured_provenance.append(dict(record or {}))
            return content

        client = TestClient(proxy.app)
        with patch.object(proxy, "TTS_ENABLED", True), \
             patch.object(proxy, "host_for_capability", lambda _capability: "http://tts.test"), \
             patch.object(proxy, "_ensure_speech_service_ready", return_value=None), \
             patch.object(proxy, "resolve_model_for_operation", lambda operation, requested: proxy.ModelResolution(requested, "tts-backend", operation)), \
             patch.object(proxy.httpx, "AsyncClient", _FakeAsyncClient), \
             patch.object(media_ingestion.media_delivery, "attach_provenance", record_provenance):
            response = client.post(
                "/v1/audio/speech",
                json={
                    "model": "tts-1",
                    "voice": "alloy",
                    "input": "hello",
                    "response_format": "wav",
                    "extensions": {"com.azizam.workflow": {"metadata": "full"}},
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "audio/wav")
        self.assertEqual(response.content, upstream)
        self.assertEqual(len(_FakeAsyncClient.posts), 1)
        sent = json.loads(bytes(_FakeAsyncClient.posts[0]["content"]).decode("utf-8"))
        self.assertNotIn("extensions", sent)
        request_id = response.headers["x-request-id"]

        receipt = receipt_store.get(request_id, path=self.receipt_db)
        assert receipt is not None
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["intent"], "speech.synthesis")
        self.assertEqual(receipt["requested_model"], "tts-1")
        self.assertEqual(receipt["resolved_model"], "tts-backend")

        job = media_jobs.get_by_request_id(request_id, path=self.media_db)
        assert job is not None
        self.assertEqual(receipt["job_id"], job["job_id"])
        self.assertEqual(job["intent"], "speech.synthesis")
        self.assertEqual(job["status"], media_jobs.STATUS_SUCCEEDED)
        self.assertEqual(job["assets"][0]["kind"], "audio")
        self.assertEqual(job["assets"][0]["media_type"], "audio/wav")
        self.assertTrue(captured_provenance)
        self.assertEqual(captured_provenance[0]["schema"], provenance.PROVENANCE_SCHEMA)
        self.assertEqual(captured_provenance[0]["request_id"], request_id)
        self.assertEqual(captured_provenance[0]["kind"], "audio")
        self.assertEqual(captured_provenance[0]["requested_model"], "tts-1")
        self.assertEqual(captured_provenance[0]["resolved_model"], "tts-backend")

    def test_store_audio_response_uses_honest_aac_mime_and_skips_unknown_binary(self) -> None:
        receipt = RouteReceipt(protocol="openai.audio", intent="speech.synthesis", request_id="req_aac")
        stored: dict[str, object] = {}

        def store_asset(content: bytes, *, kind: str, suffix: str, owner_key_hash: str | None) -> str:
            stored.update(content=content, suffix=suffix)
            return f"{kind}_{hashlib.sha256(content).hexdigest()}"

        with patch.object(media_ingestion.media_delivery, "attach_provenance", lambda content, suffix, record: content):
            assets = media_ingestion.store_audio_response(b"aac-bytes", "audio/aac", receipt, store_asset=store_asset)
            self.assertEqual(stored["suffix"], ".aac")
            self.assertEqual(assets[0]["media_type"], "audio/aac")
            unknown = media_ingestion.store_audio_response(b"bytes", "application/x-unknown", receipt, store_asset=store_asset)
            self.assertEqual(unknown, [])
            octets = media_ingestion.store_audio_response(b"bytes", "application/octet-stream", receipt, store_asset=store_asset)
            self.assertEqual(octets, [])
            l16 = media_ingestion.store_audio_response(b"\x00\x01", "audio/l16", receipt, store_asset=store_asset)
            self.assertEqual(l16, [])
            opus_raw = media_ingestion.store_audio_response(b"not-ogg", "audio/opus", receipt, store_asset=store_asset)
            self.assertEqual(opus_raw, [])
            opus_ogg = media_ingestion.store_audio_response(b"OggS" + b"opus", "audio/opus", receipt, store_asset=store_asset)
            self.assertEqual(opus_ogg[0]["media_type"], "audio/ogg")


    def test_public_video_content_indexes_sync_delivery_asset(self) -> None:
        import anyio

        prompt_id = _FakeVideoAsyncClient.prompt_id
        video_id = proxy._video_id_from_prompt_id(prompt_id)
        auth_context = proxy.AuthContext(
            identity=proxy.AuthIdentity(
                name="Video app",
                key_hash="sha256:video-owner",
                app_id="app_video",
            )
        )
        proxy._video_job_records[prompt_id] = {
            "resolved_model": "ltx-test",
            "app_profile": auth_context.identity.profile.id,
            "app_id": "app_video",
            "api_key_name": "Video app",
            "owner_key_hash": "sha256:video-owner",
            "provenance": provenance.build_provenance_record(
                kind="video",
                request_id=video_id,
                requested_model="sora-test",
                resolved_model="ltx-test",
                workflow_id="video.generate.ltx-test",
                backend="comfyui",
            ),
        }
        self.addCleanup(lambda: proxy._video_job_records.pop(prompt_id, None))

        with patch.object(proxy, "COMFY_ENABLED", True), \
             patch.object(proxy.httpx, "AsyncClient", _FakeVideoAsyncClient), \
             patch.object(media_ingestion.media_delivery, "attach_provenance", lambda content, suffix, record: content):
            response = anyio.run(proxy.proxy_comfy_video_content, video_id, auth_context)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.media_type, "video/mp4")
        self.assertEqual(response.body, _FakeVideoAsyncClient.video_content)
        job = media_jobs.get_by_request_id(video_id, path=self.media_db)
        assert job is not None
        self.assertEqual(job["intent"], "video.generate")
        self.assertEqual(job["workflow"], "video.generate.ltx-test")
        self.assertEqual(job["backend"], "comfyui")
        self.assertEqual(job["app_profile"], "default-local")
        self.assertEqual(job["app_id"], "app_video")
        self.assertEqual(job["api_key_name"], "Video app")
        self.assertEqual(job["owner_key_hash"], "app:app_video")
        self.assertEqual(job["metadata"], {"mode": "sync_video_delivery"})
        self.assertEqual(job["assets"][0]["kind"], "video")
        self.assertEqual(job["assets"][0]["media_type"], "video/mp4")
        self.assertEqual(job["assets"][0]["filename"], "sync-video.mp4")
        self.assertTrue(job["assets"][0]["asset_id"].startswith("vid_"))


    def test_video_content_index_library_false_preserves_delivery_without_media_job(self) -> None:
        import anyio

        prompt_id = "prompt_sync_video_no_index"
        video_id = proxy._video_id_from_prompt_id(prompt_id)
        auth_context = proxy.AuthContext(
            identity=proxy.AuthIdentity(name="Video app", key_hash="sha256:video-owner", app_id="app_video")
        )
        old_prompt = _FakeVideoAsyncClient.prompt_id
        _FakeVideoAsyncClient.prompt_id = prompt_id
        proxy._video_job_records[prompt_id] = {
            "resolved_model": "ltx-test",
            "app_profile": auth_context.identity.profile.id,
            "app_id": "app_video",
            "api_key_name": "Video app",
            "owner_key_hash": "sha256:video-owner",
            "provenance": provenance.build_provenance_record(
                kind="video",
                request_id=video_id,
                requested_model="sora-test",
                resolved_model="ltx-test",
                workflow_id="video.generate.ltx-test",
                backend="comfyui",
            ),
        }
        self.addCleanup(lambda: proxy._video_job_records.pop(prompt_id, None))
        self.addCleanup(lambda: setattr(_FakeVideoAsyncClient, "prompt_id", old_prompt))

        with patch.object(proxy, "COMFY_ENABLED", True), \
             patch.object(proxy.httpx, "AsyncClient", _FakeVideoAsyncClient), \
             patch.object(media_ingestion.media_delivery, "attach_provenance", lambda content, suffix, record: content):
            async def fetch_without_index():
                return await proxy.proxy_comfy_video_content(video_id, auth_context, index_library=False)

            response = anyio.run(fetch_without_index)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, _FakeVideoAsyncClient.video_content)
        self.assertIsNone(media_jobs.get_by_request_id(video_id, path=self.media_db))

    def test_receipt_helpers_begin_finish_regular_and_streaming_responses(self) -> None:
        import anyio

        class JsonRequest(_StubUrlRequest):
            async def json(self):
                return {"model": "tts-1", "stream_format": "sse"}

        async def begin(intent: str):
            return await media_ingestion.start_receipt(
                request,  # type: ignore[arg-type]
                proxy.NO_AUTH_CONTEXT,
                intent=intent,
                profile="owner-default",
            )

        request = JsonRequest()
        receipt = anyio.run(begin, "audio.generate")
        response = media_ingestion.finish_response(receipt, Response(b"ok", status_code=200))
        self.assertEqual(response.headers["x-request-id"], receipt.request_id)
        stored = receipt_store.get(receipt.request_id, path=self.receipt_db)
        assert stored is not None
        self.assertEqual(stored["status"], "succeeded")
        self.assertEqual(stored["requested_model"], "tts-1")
        self.assertTrue(stored["stream"])
        self.assertEqual(stored["policy_decision"], "allowed")

        async def body():
            yield b"a"
            yield b"b"

        streaming_receipt = anyio.run(begin, "speech.synthesis")
        streaming = media_ingestion.finish_response(streaming_receipt, StreamingResponse(body(), status_code=200))

        async def drain() -> bytes:
            out = b""
            async for chunk in streaming.body_iterator:
                out += chunk
            return out

        self.assertEqual(anyio.run(drain), b"ab")
        stored_stream = receipt_store.get(streaming_receipt.request_id, path=self.receipt_db)
        assert stored_stream is not None
        self.assertEqual(stored_stream["status"], "succeeded")
        self.assertTrue(stored_stream["stream"])
