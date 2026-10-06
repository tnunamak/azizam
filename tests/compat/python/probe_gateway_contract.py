"""Raw HTTP compatibility probes for audit-critical gateway contracts.

These probes intentionally avoid SDK dependencies so they can gate the
gateway's OpenAI/Anthropic wire contracts directly. They are skipped unless
``AZIZAM_BASE_URL`` and ``AZIZAM_API_KEY`` are configured.
"""

from __future__ import annotations

import json
import os
import unittest
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tests.compat.harness import ConfigError, load_config


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


@unittest.skipUnless(
    os.environ.get("AZIZAM_BASE_URL"),
    "AZIZAM_BASE_URL not set; live gateway contract probe disabled",
)
class TestGatewayContractHTTP(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.cfg = load_config()
        except ConfigError as exc:  # pragma: no cover - env-dependent
            self.skipTest(str(exc))

    def _request(
        self,
        path: str,
        *,
        body: Any = None,
        raw_body: bytes | None = None,
        content_type: str = "application/json",
        timeout_s: float | None = None,
    ) -> HttpResponse:
        if body is not None and raw_body is not None:
            raise ValueError("pass either body or raw_body, not both")
        data = raw_body if raw_body is not None else json.dumps(body).encode("utf-8")
        req = Request(
            f"{self.cfg.base_url}{path}",
            data=data,
            headers={
                "Authorization": f"Bearer {self.cfg.api_key}",
                "Content-Type": content_type,
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=timeout_s or self.cfg.request_timeout_s) as res:
                return HttpResponse(res.status, dict(res.headers), res.read())
        except HTTPError as exc:
            return HttpResponse(exc.code, dict(exc.headers), exc.read())

    def test_chat_completion_has_visible_content(self) -> None:
        res = self._request(
            "/v1/chat/completions",
            body={
                "model": "default",
                "messages": [{"role": "user", "content": "Reply with only: pong"}],
                "temperature": 0,
                "max_tokens": 512,
            },
        )
        self.assertEqual(res.status, 200, res.body[:500])
        data = res.json()
        message = data["choices"][0]["message"]
        self.assertIsInstance(message.get("content"), str)
        self.assertTrue(
            message["content"].strip(),
            "OpenAI chat replies must put visible text in choices[0].message.content",
        )

    def test_chat_stream_has_content_delta_and_done_frame(self) -> None:
        res = self._request(
            "/v1/chat/completions",
            body={
                "model": "default",
                "messages": [{"role": "user", "content": "Reply with only: pong"}],
                "temperature": 0,
                "max_tokens": 512,
                "stream": True,
            },
        )
        self.assertEqual(res.status, 200, res.body[:500])
        frames = [
            line.removeprefix("data: ").strip()
            for line in res.body.decode("utf-8", errors="replace").splitlines()
            if line.startswith("data:")
        ]
        self.assertIn("[DONE]", frames, "OpenAI SSE streams must end with data: [DONE]")
        content_delta = ""
        reasoning_delta = ""
        for frame in frames:
            if frame == "[DONE]":
                continue
            chunk = json.loads(frame)
            delta = chunk["choices"][0].get("delta") or {}
            content_delta += delta.get("content") or ""
            reasoning_delta += delta.get("reasoning_content") or ""
        self.assertTrue(
            content_delta.strip(),
            "visible streamed text must arrive as choices[].delta.content",
        )
        self.assertFalse(
            reasoning_delta.strip() and not content_delta.strip(),
            "gateway streamed only reasoning_content and no visible content",
        )

    def test_malformed_json_returns_400_error_object(self) -> None:
        res = self._request(
            "/v1/chat/completions",
            raw_body=b"not valid json{{{",
        )
        self.assertEqual(res.status, 400, res.body[:500])
        error = res.json().get("error")
        self.assertIsInstance(error, dict)
        self.assertIn(error.get("type"), {"invalid_request_error", "invalid_json"})

    def test_missing_model_is_rejected_before_default_routing(self) -> None:
        res = self._request(
            "/v1/chat/completions",
            body={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 16},
        )
        self.assertEqual(res.status, 400, res.body[:500])
        error = res.json().get("error")
        self.assertIsInstance(error, dict)
        self.assertEqual(error.get("param"), "model")

    def test_unknown_model_is_rejected(self) -> None:
        res = self._request(
            "/v1/chat/completions",
            body={
                "model": "nope-doesnt-exist",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 16,
            },
        )
        self.assertIn(res.status, {400, 404}, res.body[:500])
        error = res.json().get("error")
        self.assertIsInstance(error, dict)
        self.assertIn(error.get("code"), {"model_not_found", "invalid_request_error"})

    def test_anthropic_messages_has_text_content_block(self) -> None:
        res = self._request(
            "/v1/messages",
            body={
                "model": "default",
                "messages": [{"role": "user", "content": "Reply with only: pong"}],
                "max_tokens": 512,
            },
        )
        self.assertEqual(res.status, 200, res.body[:500])
        data = res.json()
        content = data.get("content")
        self.assertIsInstance(content, list)
        text_blocks = [
            block
            for block in content
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
        ]
        self.assertTrue(
            text_blocks,
            "Anthropic messages must include at least one non-empty text content block",
        )

    def test_audio_transcription_empty_body_error_is_openai_shaped(self) -> None:
        res = self._request(
            "/v1/audio/transcriptions",
            raw_body=b"",
            content_type="multipart/form-data; boundary=empty",
        )
        self.assertEqual(res.status, 400, res.body[:500])
        error = res.json().get("error")
        self.assertIsInstance(error, dict)
        self.assertIsInstance(error.get("message"), str)
        self.assertTrue(error.get("type") or error.get("code"))

    def test_audio_translation_empty_body_error_is_openai_shaped_unsupported(self) -> None:
        res = self._request(
            "/v1/audio/translations",
            raw_body=b"",
            content_type="multipart/form-data; boundary=empty",
        )
        self.assertEqual(res.status, 400, res.body[:500])
        error = res.json().get("error")
        self.assertIsInstance(error, dict)
        self.assertEqual(error.get("type"), "invalid_request_error")
        self.assertEqual(error.get("code"), "unsupported_audio_translation")

    def test_chat_non_object_body_returns_protocol_shaped_400(self) -> None:
        """A JSON list at the top level is a protocol-shape violation: the
        gateway must reject it with the OpenAI error envelope, not forward
        garbage to the backend."""
        res = self._request("/v1/chat/completions", raw_body=b"[]")
        self.assertEqual(res.status, 400, res.body[:500])
        error = res.json().get("error")
        self.assertIsInstance(error, dict)
        self.assertEqual(error.get("type"), "invalid_request_error")
        self.assertIsInstance(error.get("message"), str)
        self.assertTrue(error.get("message"))

    def test_responses_hosted_tool_returns_protocol_shaped_400(self) -> None:
        """The gateway has no first-party hosted-tool surface, so hosted tools
        on /v1/responses must be refused with a deterministic OpenAI-shaped
        error envelope."""
        res = self._request(
            "/v1/responses",
            body={
                "model": "default",
                "input": "hi",
                "tools": [{"type": "web_search"}],
            },
        )
        self.assertEqual(res.status, 400, res.body[:500])
        error = res.json().get("error")
        self.assertIsInstance(error, dict)
        self.assertEqual(error.get("code"), "hosted_tool_not_supported")
        param = error.get("param") or ""
        self.assertTrue(
            param.startswith("tools["),
            f"expected tools[...] param pointer, got {param!r}",
        )

    def test_audio_speech_unsupported_response_format_is_openai_shaped(self) -> None:
        """TTS must reject unsupported response_format values with an OpenAI
        envelope (regression for the audit finding that `response_format`
        was silently ignored)."""
        res = self._request(
            "/v1/audio/speech",
            body={
                "model": "default",
                "voice": "alloy",
                "input": "compatibility probe",
                "response_format": "m4a",
            },
        )
        if res.status == 503:
            # TTS not enabled on this gateway instance; the regression check
            # cannot run without the TTS surface present.
            self.skipTest("TTS not enabled on this gateway")
        self.assertEqual(res.status, 400, res.body[:500])
        error = res.json().get("error")
        self.assertIsInstance(error, dict)
        self.assertEqual(error.get("code"), "unsupported_response_format")
        self.assertEqual(error.get("param"), "response_format")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
