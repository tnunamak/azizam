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
from unittest.mock import patch

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


class OpenAICompatValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(proxy.app)
        _FakeTTSAsyncClient.requests = []
        self.patches = [
            patch.object(proxy, "AUTH_ENABLED", False),
            patch.object(proxy, "_enforce_runtime_route_policy", return_value=None),
            patch.object(proxy, "_current_loaded_model_policy_decision", return_value=None),
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

    def test_unknown_model_returns_model_not_found(self) -> None:
        resp = self.client.post(
            "/v1/chat/completions",
            json={
                "model": "nope-doesnt-exist",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        self.assertEqual(resp.status_code, 404)
        body = resp.json()
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        self.assertEqual(body["error"]["param"], "model")
        self.assertEqual(body["error"]["code"], "model_not_found")

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

    def test_image_variations_returns_precise_unsupported_error_without_backend_call(self) -> None:
        for path in ("/v1/images/variations", "/openai/v1/images/variations"):
            with self.subTest(path=path), patch.object(
                proxy,
                "proxy_comfy_request",
                side_effect=AssertionError("image variation must not forward to generation backend"),
            ), patch.object(
                proxy,
                "proxy_comfy_edit_request",
                side_effect=AssertionError("image variation must not forward to edit backend"),
            ):
                resp = self.client.post(
                    path,
                    files={"image": ("source.png", b"not-used", "image/png")},
                    data={"model": "dall-e-2"},
                )

            self.assertEqual(resp.status_code, 400)
            body = resp.json()
            self.assertEqual(body["error"]["type"], "invalid_request_error")
            self.assertEqual(body["error"]["code"], "unsupported_image_variations")
            self.assertIn("not configured or supported", body["error"]["message"])

    def test_image_variations_missing_auth_returns_auth_error(self) -> None:
        with patch.object(proxy, "AUTH_ENABLED", True), patch.object(
            proxy,
            "AUTH_REGISTRY",
            {},
        ), patch.object(proxy, "refresh_auth_registry", return_value=None):
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

    def test_translation_route_forwards_to_backend_when_capability_declared(self) -> None:
        captured = {}

        class FakeSyncClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def post(self, url, *, headers, data, files):
                captured["url"] = url
                captured["data"] = data
                captured["files"] = files

                class FakeResponse:
                    status_code = 200
                    headers = {"content-type": "application/json"}
                    content = b'{"text":"translated"}'

                return FakeResponse()

        with stt_translation_capability_enabled(), patch.object(proxy.httpx, "Client", FakeSyncClient):
            resp = self.client.post(
                "/v1/audio/translations",
                data={"model": "whisper-1"},
                files={"file": ("sample.wav", b"RIFF....WAVE", "audio/wav")},
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["text"], "translated")
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
                    "model": "tts-1",
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

    def test_tts_rejects_unsupported_response_format(self) -> None:
        resp = self.client.post(
            "/v1/audio/speech",
            json={
                "model": "tts-1",
                "voice": "alloy",
                "input": "hello",
                "response_format": "flac",
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


if __name__ == "__main__":
    unittest.main()
