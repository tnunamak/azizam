"""Focused OpenAI compatibility validation tests for the proxy edge."""

from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
import wave
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402


@contextmanager
def stt_translation_capability_enabled():
    original = proxy.config["backends"]["stt"]["capabilities"]
    proxy.config["backends"]["stt"]["capabilities"] = [*original, "translations"]
    try:
        yield
    finally:
        proxy.config["backends"]["stt"]["capabilities"] = original


class _FakeTTSAsyncClient:
    requests: list[dict[str, object]] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, url, headers=None, content=None):
        self.__class__.requests.append(
            {
                "url": url,
                "headers": dict(headers or {}),
                "content": content,
            }
        )
        return proxy.httpx.Response(
            200,
            content=b"backend-wav",
            headers={"content-type": "audio/wav"},
        )


class _FakeSTTAsyncClient:
    requests: list[dict[str, object]] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def build_request(self, method, url, *, headers, data, files):
        self.__class__.requests.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "data": data,
                "files": files,
            }
        )
        return object()

    async def send(self, _request, *, stream):
        return proxy.httpx.Response(
            200,
            content=b'{"text":"translated"}',
            headers={"content-type": "application/json"},
        )

    async def aclose(self):
        return None


class OpenAICompatValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(proxy.app)
        _FakeTTSAsyncClient.requests = []
        _FakeSTTAsyncClient.requests = []
        self.patches = [
            patch.object(proxy, "AUTH_ENABLED", False),
            patch.object(proxy, "_enforce_runtime_route_policy", return_value=None),
            patch.object(proxy, "_current_loaded_model_policy_decision", return_value=None),
            # tts-1 resolves to an on-demand engine; never start a real unit from a test.
            patch.object(proxy, "_ensure_speech_service_ready", AsyncMock(return_value=None)),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patches):
            patcher.stop()

    def test_malformed_json_returns_openai_400_before_backend_selection(self) -> None:
        resp = self.client.post(
            "/v1/chat/completions",
            content=b"not valid json{{{",
            headers={"content-type": "application/json"},
        )

        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        self.assertEqual(body["error"]["code"], "invalid_json")

    def test_missing_model_returns_openai_error(self) -> None:
        resp = self.client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )

        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        self.assertEqual(body["error"]["param"], "model")
        self.assertEqual(body["error"]["code"], "missing_required_parameter")

    def test_embeddings_dimensions_invalid_value_returns_openai_400(self) -> None:
        resp = self.client.post(
            "/openai/v1/embeddings",
            json={"model": "text-embedding-3-small", "input": "ping", "dimensions": 0},
        )

        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        self.assertEqual(body["error"]["param"], "dimensions")

    def test_transcription_without_file_uses_openai_error_object(self) -> None:
        resp = self.client.post("/v1/audio/transcriptions", data={"model": "whisper-1"})

        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertIsInstance(body["error"], dict)
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        self.assertEqual(body["error"]["param"], "file")
        self.assertEqual(body["error"]["code"], "missing_file")

    def test_translation_route_returns_precise_unsupported_error(self) -> None:
        resp = self.client.post("/v1/audio/translations", data={"model": "whisper-1"})

        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        self.assertEqual(body["error"]["param"], "file")
        self.assertEqual(body["error"]["code"], "unsupported_audio_translation")
        self.assertIn("not supported", body["error"]["message"])

    def test_openai_translation_alias_returns_precise_unsupported_error(self) -> None:
        resp = self.client.post("/openai/v1/audio/translations", data={"model": "whisper-1"})

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "unsupported_audio_translation")

    def test_image_variations_routes_through_edit_backend_not_generation_backend(self) -> None:
        """/v1/images/variations is now implemented (routes through an
        edit-capable workflow with a fixed generic prompt) -- it must still
        never forward to the generation backend, but it now DOES reach the
        edit backend, unlike the old always-unsupported stub."""
        for path in ("/v1/images/variations", "/openai/v1/images/variations"):
            captured: dict[str, object] = {}

            async def fake_edit_request(request, auth_context, receipt=None):
                form = await request.form()
                captured["prompt"] = form.get("prompt")
                captured["model"] = form.get("model")
                return proxy.Response(
                    content=json.dumps({"created": 1, "data": []}),
                    status_code=200,
                    media_type="application/json",
                )

            with self.subTest(path=path), patch.object(
                proxy,
                "proxy_comfy_request",
                side_effect=AssertionError("image variation must not forward to generation backend"),
            ), patch.object(
                proxy,
                "proxy_comfy_edit_request",
                fake_edit_request,
            ):
                resp = self.client.post(
                    path,
                    files={"image": ("source.png", b"not-used", "image/png")},
                    data={"model": "dall-e-2"},
                )

            self.assertEqual(resp.status_code, 200)
            # dall-e-2 (an OpenAI public model name, not a local workflow id)
            # resolves to the configured variations default, not a literal
            # (nonexistent) "dall-e-2-edit" workflow.
            self.assertEqual(captured["model"], proxy.IMAGE_VARIATIONS_DEFAULT_WORKFLOW)
            self.assertEqual(captured["prompt"], proxy.IMAGE_VARIATIONS_PROMPT)

    def test_image_variations_unsupported_field_still_rejected_before_backend_call(self) -> None:
        """OpenAI's variations request has no `prompt` field; sending one must
        still be rejected the same way OpenAI itself would, without reaching
        either backend function."""
        with patch.object(
            proxy,
            "proxy_comfy_request",
            side_effect=AssertionError("image variation must not forward to generation backend"),
        ), patch.object(
            proxy,
            "proxy_comfy_edit_request",
            side_effect=AssertionError("image variation must not forward to edit backend"),
        ):
            resp = self.client.post(
                "/v1/images/variations",
                files={"image": ("source.png", b"not-used", "image/png")},
                data={"prompt": "not a real variations field"},
            )

        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"]["code"], "unknown_parameter")
        self.assertEqual(body["error"]["param"], "prompt")

    def test_image_variations_missing_auth_returns_auth_error(self) -> None:
        with patch.object(proxy, "AUTH_ENABLED", True):
            resp = self.client.post(
                "/v1/images/variations",
                files={"image": ("source.png", b"not-used", "image/png")},
                data={"model": "dall-e-2"},
            )

        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["type"], "authentication_error")

    def test_image_variations_policy_denied_stays_403(self) -> None:
        denied = proxy.PolicyDecision(
            proxy.DENIED,
            reasons=("image variations denied by test policy",),
        )
        with patch.object(proxy, "_apply_image_route_policy", return_value=denied), patch.object(
            proxy,
            "proxy_comfy_request",
            side_effect=AssertionError("image variation must not forward to generation backend"),
        ), patch.object(
            proxy,
            "proxy_comfy_edit_request",
            side_effect=AssertionError("image variation must not forward to edit backend"),
        ):
            resp = self.client.post(
                "/v1/images/variations",
                files={"image": ("source.png", b"not-used", "image/png")},
                data={"model": "dall-e-2"},
            )

        self.assertEqual(resp.status_code, 403)
        body = resp.json()
        self.assertEqual(body["error"]["type"], "permission_error")
        self.assertEqual(body["error"]["code"], "policy_denied")

    def test_advertised_unsupported_openai_api_stubs_do_not_404(self) -> None:
        cases = [
            ("GET", "/v1/assistants"),
            ("POST", "/openai/v1/assistants"),
            ("GET", "/v1/files"),
            ("POST", "/openai/v1/files"),
            ("GET", "/v1/files/file_123"),
            ("DELETE", "/openai/v1/files/file_123"),
            ("GET", "/v1/batches"),
            ("POST", "/openai/v1/batches"),
            ("GET", "/v1/batches/batch_123"),
            ("POST", "/openai/v1/batches/batch_123/cancel"),
            ("GET", "/v1/fine_tuning/jobs"),
            ("POST", "/openai/v1/fine_tuning/jobs"),
            ("GET", "/v1/realtime"),
            ("POST", "/openai/v1/realtime/sessions"),
        ]

        for method, path in cases:
            with self.subTest(method=method, path=path):
                resp = self.client.request(method, path, json={} if method == "POST" else None)

            self.assertEqual(resp.status_code, 400)
            body = resp.json()
            self.assertEqual(body["error"]["type"], "invalid_request_error")
            self.assertEqual(body["error"]["code"], "unsupported_operation")
            self.assertIn("not implemented", body["error"]["message"])

    def test_unsupported_openai_api_stubs_still_require_auth(self) -> None:
        with patch.object(proxy, "AUTH_ENABLED", True):
            resp = self.client.get("/v1/files")

        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["type"], "authentication_error")

    def test_translation_route_forwards_to_backend_when_capability_declared(self) -> None:
        with stt_translation_capability_enabled(), patch.object(proxy.httpx, "AsyncClient", _FakeSTTAsyncClient):
            resp = self.client.post(
                "/v1/audio/translations",
                data={"model": "parakeet"},  # the always-on CPU engine; whisper-1 now leads with an on-demand GPU engine
                files={"file": ("sample.wav", b"RIFF....WAVE", "audio/wav")},
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["text"], "translated")
        captured = _FakeSTTAsyncClient.requests[0]
        self.assertTrue(captured["url"].endswith("/v1/audio/translations"))
        self.assertEqual(captured["data"]["model"], proxy.STT_BACKEND_MODEL)
        self.assertEqual(captured["files"][0][0], "file")

    def test_tts_accepts_mp3_response_format(self) -> None:
        with patch.object(proxy.httpx, "AsyncClient", _FakeTTSAsyncClient), patch.object(
            proxy,
            "convert_wav_to_mp3",
            return_value=b"converted-mp3",
        ):
            resp = self.client.post(
                "/v1/audio/speech",
                json={
                    "model": "omnivoice",  # a non-streaming engine: the gateway converts its WAV
                    "voice": "alloy",
                    "input": "hello",
                    "response_format": "mp3",
                },
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "audio/mpeg")
        self.assertEqual(resp.content, b"converted-mp3")
        self.assertEqual(len(_FakeTTSAsyncClient.requests), 1)
        downstream = json.loads(_FakeTTSAsyncClient.requests[0]["content"])
        self.assertEqual(downstream["response_format"], "wav")
        self.assertEqual(downstream["model"], proxy.TTS_BACKEND_MODEL)

    def test_tts_accepts_additional_openai_audio_formats(self) -> None:
        formats = {
            "opus": "audio/ogg",
            "aac": "audio/aac",
            "flac": "audio/flac",
            "pcm": "audio/pcm",
        }
        for audio_format, content_type in formats.items():
            with self.subTest(audio_format=audio_format), patch.object(
                proxy.httpx, "AsyncClient", _FakeTTSAsyncClient
            ), patch.object(
                proxy,
                "convert_wav_to_audio_format",
                return_value=b"converted-audio",
            ):
                resp = self.client.post(
                    "/v1/audio/speech",
                    json={
                        "model": "omnivoice",  # a non-streaming engine: the gateway converts its WAV
                        "voice": "alloy",
                        "input": "hello",
                        "response_format": audio_format,
                    },
                )

                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.headers["content-type"], content_type)
                self.assertEqual(resp.content, b"converted-audio")

    def test_tts_rejects_unsupported_response_format(self) -> None:
        resp = self.client.post(
            "/v1/audio/speech",
            json={
                "model": "tts-1",
                "voice": "alloy",
                "input": "hello",
                "response_format": "m4a",
            },
        )

        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        self.assertEqual(body["error"]["param"], "response_format")
        self.assertEqual(body["error"]["code"], "unsupported_response_format")

    def test_wav_to_mp3_conversion_uses_local_ffmpeg(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".wav") as wav_path:
            with wave.open(wav_path.name, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(8000)
                wav_file.writeframes(struct.pack("<" + "h" * 800, *([0] * 800)))

            wav_path.flush()
            wav_path.seek(0)
            mp3 = proxy.convert_wav_to_mp3(wav_path.read())

        self.assertTrue(mp3.startswith(b"ID3") or mp3.startswith(b"\xff"))

    def test_wav_to_openai_audio_formats_uses_local_ffmpeg(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".wav") as wav_path:
            with wave.open(wav_path.name, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(24000)
                wav_file.writeframes(struct.pack("<" + "h" * 800, *([0] * 800)))

            wav_path.flush()
            wav_path.seek(0)
            wav_content = wav_path.read()

        signatures = {
            "opus": b"OggS",
            "aac": b"\xff",
            "flac": b"fLaC",
        }
        for audio_format, signature in signatures.items():
            with self.subTest(audio_format=audio_format):
                converted = proxy.convert_wav_to_audio_format(wav_content, audio_format)
                self.assertTrue(converted.startswith(signature))
        pcm = proxy.convert_wav_to_audio_format(wav_content, "pcm")
        self.assertEqual(len(pcm), 800 * 2)


if __name__ == "__main__":
    unittest.main()
