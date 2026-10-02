"""Offline conformance checks against pinned OpenAI OpenAPI response schemas.

The schemas are vendored from openai/openai-openapi commit
145fd3b2e30e3ac87a4ed915913f6fa1217974fa. The fixture contains only response
schemas and their transitive component references, so the test is small and
does not need network access.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402


SPEC_PATH = ROOT / "tests" / "fixtures" / "openai-openapi-responses.json"
SPEC = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
SPEC_URI = f"urn:openai-openapi:{SPEC['commit']}"
SPEC_DOCUMENT = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": SPEC_URI,
    "components": SPEC["components"],
}
SCHEMA_REGISTRY = Registry().with_resource(
    SPEC_URI, Resource.from_contents(SPEC_DOCUMENT)
)


def assert_openai_schema(test: unittest.TestCase, schema_name: str, payload: dict) -> None:
    schema = {"$ref": f"{SPEC_URI}#/components/schemas/{schema_name}"}
    errors = sorted(
        Draft202012Validator(schema, registry=SCHEMA_REGISTRY).iter_errors(payload),
        key=lambda error: list(error.absolute_path),
    )
    test.assertEqual(
        errors,
        [],
        "\n".join(
            f"{schema_name} at {list(error.absolute_path)}: {error.message}"
            for error in errors
        ),
    )


class OpenAISchemaConformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(proxy.app)
        self.auth = patch.object(proxy, "AUTH_ENABLED", False)
        self.policy = patch.object(proxy, "_enforce_runtime_route_policy", return_value=None)
        self.auth.start()
        self.policy.start()

    def tearDown(self) -> None:
        self.policy.stop()
        self.auth.stop()

    def test_vendor_is_pinned_and_selected_component_schemas_are_valid(self) -> None:
        self.assertEqual(SPEC["source"], "https://github.com/openai/openai-openapi")
        self.assertEqual(
            SPEC["commit"], "145fd3b2e30e3ac87a4ed915913f6fa1217974fa"
        )
        for schema_name in SPEC["roots"]:
            with self.subTest(schema=schema_name):
                Draft202012Validator.check_schema(
                    SPEC["components"]["schemas"][schema_name]
                )

    def test_models_route_response_conforms(self) -> None:
        with patch.object(
            proxy, "_litellm_runtime_config", return_value=SimpleNamespace(enabled=False)
        ), patch.object(proxy, "_annotate_model_context_windows", return_value=None):
            response = self.client.get("/v1/models")

        self.assertEqual(response.status_code, 200, response.text)
        assert_openai_schema(self, "ListModelsResponse", response.json())

    def test_chat_and_embedding_route_responses_conform(self) -> None:
        chat_payload = {
            "id": "chatcmpl_hermetic",
            "object": "chat.completion",
            "created": 1_700_000_000,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello"},
                    "finish_reason": "stop",
                    "logprobs": None,
                }
            ],
        }
        embeddings_payload = {
            "object": "list",
            "model": "test-embedding-model",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.25, -0.5]}],
            "usage": {"prompt_tokens": 2, "total_tokens": 2},
        }

        async def forward(_request, **_kwargs):
            body = chat_payload if _kwargs["request_path"] == "/v1/chat/completions" else embeddings_payload
            return proxy.Response(
                content=json.dumps(body), status_code=200, media_type="application/json"
            )

        with patch.object(proxy, "_litellm_runtime_config", return_value=SimpleNamespace(enabled=False)), patch.object(
            proxy, "_forward_litellm_proxy_request", side_effect=forward
        ):
            chat = self.client.post(
                "/v1/chat/completions",
                json={"model": "test-model", "messages": [{"role": "user", "content": "hello"}]},
            )
            embeddings = self.client.post(
                "/v1/embeddings", json={"model": "test-embedding-model", "input": "hello"}
            )

        self.assertEqual(chat.status_code, 200, chat.text)
        self.assertEqual(embeddings.status_code, 200, embeddings.text)
        assert_openai_schema(self, "CreateChatCompletionResponse", chat.json())
        assert_openai_schema(self, "CreateEmbeddingResponse", embeddings.json())

    def test_image_route_response_schema_accepts_gateway_shape(self) -> None:
        # This is a hermetic route response contract fixture. The image route's
        # dedicated tests exercise provider dispatch and binary/url result paths.
        payload = {
            "created": 1_700_000_000,
            "data": [{"b64_json": "aGVsbG8="}],
            "output_format": "png",
            "size": "1024x1024",
        }
        assert_openai_schema(self, "ImagesResponse", payload)

    def test_audio_transcription_response_schema_accepts_gateway_shape(self) -> None:
        async def dispatch(_request, _auth_context, *, operation):
            self.assertEqual(operation, "transcriptions")
            return proxy.JSONResponse({"text": "hermetic transcription"})

        with patch.object(proxy, "_dispatch_selected_stt_route", side_effect=dispatch):
            response = self.client.post(
                "/v1/audio/transcriptions",
                data={"model": "whisper-1"},
                files={"file": ("sample.wav", b"RIFFsynthetic", "audio/wav")},
            )

        self.assertEqual(response.status_code, 200, response.text)
        assert_openai_schema(
            self, "CreateTranscriptionResponseJson", response.json()
        )

    def test_speech_route_returns_spec_supported_binary_audio(self) -> None:
        class AudioResponse:
            status_code = 200
            content = b"synthetic-wav"
            headers = {"content-type": "audio/wav"}

            async def aread(self):
                return self.content

            async def aclose(self):
                return None

        class AudioClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, *args, **kwargs):
                return AudioResponse()

        with patch.object(proxy.httpx, "AsyncClient", AudioClient):
            response = self.client.post(
                "/v1/audio/speech",
                json={"model": "tts-1", "voice": "alloy", "input": "hello", "response_format": "wav"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "audio/wav")
        self.assertTrue(response.content)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
