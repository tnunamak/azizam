"""Tests that ``/openapi.json`` honestly describes OpenAI-shaped media endpoints.

Background: most media endpoints in ``proxy.py`` are wired with
``request: Request`` so FastAPI cannot infer schemas. They previously showed up
as ``{"schema": {}}`` with no request body or media-type hints. This test suite
locks in the fact that each documented media endpoint exposes:

* a request body where the spec actually accepts one,
* a concrete success response with the right media type (binary for byte
  streams, JSON for OpenAI envelopes), and
* OpenAI-shaped error responses.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402


class OpenApiMediaSchemaHonestyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(proxy.app)
        cls.paths = cls.client.get("/openapi.json").json()["paths"]

    def _op(self, path: str, method: str = "post") -> dict:
        node = self.paths[path]
        self.assertIn(method, node, f"{path} missing method {method}")
        return node[method]

    def _ok_response(self, op: dict) -> dict:
        responses = op["responses"]
        # Prefer 200, fall back to 202 (videos submit).
        for code in ("200", "202"):
            if code in responses:
                return responses[code]
        self.fail("operation lacks a 2xx response")

    def _media_types(self, response: dict) -> list[str]:
        return sorted(response.get("content", {}).keys())

    def assertJsonRequestBody(self, op: dict) -> None:
        rb = op.get("requestBody")
        self.assertIsNotNone(rb, "operation missing requestBody")
        self.assertIn("application/json", rb["content"])
        schema = rb["content"]["application/json"]["schema"]
        self.assertEqual(schema.get("type"), "object")
        self.assertNotEqual(schema, {}, "JSON request body schema must not be empty")

    def assertMultipartRequestBody(self, op: dict) -> None:
        rb = op.get("requestBody")
        self.assertIsNotNone(rb, "operation missing requestBody")
        self.assertIn("multipart/form-data", rb["content"])
        schema = rb["content"]["multipart/form-data"]["schema"]
        self.assertEqual(schema.get("type"), "object")
        self.assertIn("properties", schema)

    def assertOpenAIErrorEnvelope(self, op: dict, status: str = "400") -> None:
        rsp = op["responses"].get(status)
        self.assertIsNotNone(rsp, f"missing {status} error response")
        schema = rsp["content"]["application/json"]["schema"]
        self.assertEqual(schema["type"], "object")
        self.assertIn("error", schema["properties"])
        error_props = schema["properties"]["error"]["properties"]
        for key in ("message", "type"):
            self.assertIn(key, error_props)

    def test_audio_speech_documents_json_body_and_binary_audio(self) -> None:
        for path in ("/v1/audio/speech", "/openai/v1/audio/speech"):
            with self.subTest(path=path):
                op = self._op(path)
                self.assertJsonRequestBody(op)
                ok = self._ok_response(op)
                self.assertIn("audio/wav", self._media_types(ok))
                self.assertIn("audio/mpeg", self._media_types(ok))
                self.assertOpenAIErrorEnvelope(op)

    def test_audio_transcriptions_documents_multipart_and_json_or_text(self) -> None:
        for path in ("/v1/audio/transcriptions", "/openai/v1/audio/transcriptions"):
            with self.subTest(path=path):
                op = self._op(path)
                self.assertMultipartRequestBody(op)
                ok = self._ok_response(op)
                self.assertIn("application/json", self._media_types(ok))
                self.assertOpenAIErrorEnvelope(op)

    def test_audio_translations_documents_multipart(self) -> None:
        for path in ("/v1/audio/translations", "/openai/v1/audio/translations"):
            with self.subTest(path=path):
                op = self._op(path)
                self.assertMultipartRequestBody(op)
                ok = self._ok_response(op)
                self.assertIn("application/json", self._media_types(ok))
                self.assertOpenAIErrorEnvelope(op)

    def test_voices_list_documents_voices_response(self) -> None:
        for path in (
            "/v1/voices",
            "/v1/audio/voices",
            "/openai/v1/voices",
            "/openai/v1/audio/voices",
        ):
            with self.subTest(path=path):
                op = self._op(path, "get")
                ok = self._ok_response(op)
                schema = ok["content"]["application/json"]["schema"]
                self.assertIn("voices", schema["properties"])

    def test_images_generations_documents_openai_image_envelope(self) -> None:
        for path in ("/v1/images/generations", "/openai/v1/images/generations"):
            with self.subTest(path=path):
                op = self._op(path)
                self.assertJsonRequestBody(op)
                ok = self._ok_response(op)
                schema = ok["content"]["application/json"]["schema"]
                self.assertIn("data", schema["properties"])
                items = schema["properties"]["data"]["items"]
                # Honest about which image fields the gateway returns.
                self.assertIn("url", items["properties"])
                self.assertIn("b64_json", items["properties"])
                self.assertOpenAIErrorEnvelope(op)

    def test_images_edits_documents_multipart_with_image_field(self) -> None:
        for path in ("/v1/images/edits", "/openai/v1/images/edits"):
            with self.subTest(path=path):
                op = self._op(path)
                self.assertMultipartRequestBody(op)
                schema = op["requestBody"]["content"]["multipart/form-data"][
                    "schema"
                ]
                self.assertIn("image", schema["properties"])
                self.assertIn("prompt", schema["properties"])
                ok = self._ok_response(op)
                self.assertIn("application/json", self._media_types(ok))

    def test_images_variations_documents_image_result_and_errors(self) -> None:
        for path in ("/v1/images/variations", "/openai/v1/images/variations"):
            with self.subTest(path=path):
                op = self._op(path)
                self.assertMultipartRequestBody(op)
                schema = op["requestBody"]["content"]["multipart/form-data"]["schema"]
                self.assertIn("image", schema["properties"])
                # No `prompt` field -- OpenAI's variations request has none,
                # unlike edits.
                self.assertNotIn("prompt", schema["properties"])
                ok = self._ok_response(op)
                self.assertIn("application/json", self._media_types(ok))
                self.assertIn("400", op["responses"])
                self.assertOpenAIErrorEnvelope(op)

    def test_images_signed_content_documents_binary_image_bytes(self) -> None:
        for path in (
            "/v1/images/{image_id}/content",
            "/openai/v1/images/{image_id}/content",
        ):
            with self.subTest(path=path):
                op = self._op(path, "get")
                ok = self._ok_response(op)
                types = self._media_types(ok)
                self.assertIn("image/png", types)
                self.assertIn("image/jpeg", types)

    def test_videos_create_documents_sora_shape(self) -> None:
        for path in ("/v1/videos", "/openai/v1/videos"):
            with self.subTest(path=path):
                op = self._op(path)
                self.assertJsonRequestBody(op)
                self.assertMultipartRequestBody(op)
                multipart_schema = op["requestBody"]["content"]["multipart/form-data"][
                    "schema"
                ]
                self.assertIn("input_reference", multipart_schema["properties"])
                ok = self._ok_response(op)
                schema = ok["content"]["application/json"]["schema"]
                self.assertIn("id", schema["properties"])
                self.assertIn("status", schema["properties"])

    def test_videos_status_documents_status_envelope(self) -> None:
        for path in ("/v1/videos/{video_id}", "/openai/v1/videos/{video_id}"):
            with self.subTest(path=path):
                op = self._op(path, "get")
                ok = self._ok_response(op)
                schema = ok["content"]["application/json"]["schema"]
                self.assertIn("status", schema["properties"])

    def test_videos_content_documents_mp4(self) -> None:
        for path in (
            "/v1/videos/{video_id}/content",
            "/openai/v1/videos/{video_id}/content",
        ):
            with self.subTest(path=path):
                op = self._op(path, "get")
                ok = self._ok_response(op)
                self.assertIn("video/mp4", self._media_types(ok))

    def test_audio_generate_documents_dual_envelope(self) -> None:
        for path in ("/v1/audio/generate", "/openai/v1/audio/generate"):
            with self.subTest(path=path):
                op = self._op(path)
                self.assertJsonRequestBody(op)
                ok = self._ok_response(op)
                types = self._media_types(ok)
                # Both JSON envelope and raw audio bytes are advertised so
                # clients know either may come back depending on workflow.
                self.assertIn("application/json", types)
                self.assertIn("audio/wav", types)

    def test_audio_models_documents_data_envelope(self) -> None:
        for path in ("/v1/audio/models", "/openai/v1/audio/models"):
            with self.subTest(path=path):
                op = self._op(path, "get")
                ok = self._ok_response(op)
                schema = ok["content"]["application/json"]["schema"]
                self.assertIn("data", schema["properties"])

    def test_no_documented_media_endpoint_has_empty_2xx_schema(self) -> None:
        """Regression: every public media endpoint advertises a concrete schema."""
        documented_media_paths = [
            "/v1/audio/speech",
            "/v1/audio/transcriptions",
            "/v1/audio/translations",
            "/v1/audio/voices",
            "/v1/voices",
            "/v1/images/generations",
            "/v1/images/edits",
            "/v1/images/{image_id}/content",
            "/v1/audio/generate",
            "/v1/audio/models",
            "/v1/videos",
            "/v1/videos/{video_id}",
            "/v1/videos/{video_id}/content",
        ]
        empty = []
        for path in documented_media_paths:
            for method, op in self.paths[path].items():
                if method not in {"get", "post", "put", "delete", "patch"}:
                    continue
                for code, rsp in op.get("responses", {}).items():
                    if not code.startswith("2"):
                        continue
                    content = rsp.get("content", {})
                    if not content:
                        empty.append(f"{method.upper()} {path} {code}: no content")
                        continue
                    for media_type, body in content.items():
                        schema = body.get("schema", {})
                        # An empty schema {} on a documented success response is the bug.
                        if schema == {}:
                            empty.append(
                                f"{method.upper()} {path} {code} {media_type}: empty schema"
                            )
        self.assertEqual(empty, [], "found endpoints with empty 2xx schemas")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
