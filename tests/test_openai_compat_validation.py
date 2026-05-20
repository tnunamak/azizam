"""Focused OpenAI compatibility validation tests for the proxy edge."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402


class OpenAICompatValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(proxy.app)
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

    def test_tts_rejects_explicit_non_wav_response_format(self) -> None:
        resp = self.client.post(
            "/v1/audio/speech",
            json={
                "model": "tts-1",
                "voice": "alloy",
                "input": "hello",
                "response_format": "mp3",
            },
        )

        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        self.assertEqual(body["error"]["param"], "response_format")
        self.assertEqual(body["error"]["code"], "unsupported_response_format")


if __name__ == "__main__":
    unittest.main()
