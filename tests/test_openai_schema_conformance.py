"""Offline conformance checks against pinned OpenAI OpenAPI response schemas.

The schemas are vendored from openai/openai-openapi commit
145fd3b2e30e3ac87a4ed915913f6fa1217974fa. The fixture contains only response
schemas and their transitive component references, so the test is small and
does not need network access.
"""

from __future__ import annotations

import json
import io
import base64
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
import _fake_comfy_video as rr_fake  # noqa: E402
import test_video_remix_replace as rr  # noqa: E402
from test_video_remix_replace import RemixReplaceBase  # noqa: E402,F401
from gateway import media_jobs, receipt_store, video_inputs, video_media_jobs  # noqa: E402
from tests.voice_test_audio import speechlike  # noqa: E402


def _rendered_mp4() -> bytes:
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "rendered.mp4"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
             "color=c=black:s=16x16:d=0.2:r=5", "-c:v", "mpeg4", str(output)],
            capture_output=True, check=True,
        )
        return output.read_bytes()


def _patch_route_stores(case: unittest.TestCase) -> None:
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    media_db = root / "media_jobs.db"
    receipts_db = root / "receipts.db"
    cache_dir = root / "asset-cache"
    media_jobs.init_db(media_db)
    receipt_store.init_db(receipts_db)
    for module, path in ((media_jobs, media_db), (receipt_store, receipts_db)):
        patcher = patch.object(module, "DB_PATH", path)
        patcher.start()
        case.addCleanup(patcher.stop)
    for name, value in (("IMAGE_CACHE_DIR", cache_dir), ("_image_url_signing_secret", b"test-secret")):
        patcher = patch.object(proxy, name, value)
        patcher.start()
        case.addCleanup(patcher.stop)


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
        _patch_route_stores(self)
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

    @patch.object(proxy, "_current_loaded_llm_model_ids", AsyncMock(return_value=set()))
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
        from PIL import Image

        class ImageRequest:
            headers: dict[str, str] = {}
            url = SimpleNamespace(scheme="http", netloc="gateway.test")

        image = Image.new("RGBA", (2, 1), (1, 2, 3, 255))
        raw_image = io.BytesIO()
        image.save(raw_image, format="PNG")
        with tempfile.TemporaryDirectory() as cache_dir, patch.object(
            proxy, "IMAGE_CACHE_DIR", Path(cache_dir)
        ), patch.object(proxy, "_image_url_signing_secret", b"test-secret"):
            response = proxy._build_image_response_payload(
                ImageRequest(),  # type: ignore[arg-type]
                [raw_image.getvalue()],
                "b64_json",
                {},
                output_format="png",
                size="1024x1024",
            )

        assert_openai_schema(self, "ImagesResponse", json.loads(response.body))

    def test_video_job_response_serializer_conforms(self) -> None:
        payload = video_media_jobs.video_generation_payload(
            {
                "job_id": "video_hermetic",
                "workflow": "video-ltx-2.5-t2v",
                "status": media_jobs.STATUS_RUNNING,
                "progress": 42,
                "created_at": 1_700_000_000,
                "updated_at": 1_700_000_030,
                "metadata": {
                    "model": "ltx-2.5",
                    "size": "1280x720",
                    "seconds": "5",
                    "prompt": "a short synthetic clip",
                },
                "assets": [],
            }
        )
        assert_openai_schema(self, "VideoResource", payload)

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
            content = speechlike(0.1)
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

        with patch.object(proxy.httpx, "AsyncClient", AudioClient), patch.object(
            proxy, "_ensure_speech_service_ready", AsyncMock(return_value=None)
        ):
            response = self.client.post(
                "/v1/audio/speech",
                json={"model": "tts-1", "voice": "alloy", "input": "hello", "response_format": "wav"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "audio/wav")
        self.assertTrue(response.content)


def _conforms_to_video_resource(test: unittest.TestCase, payload: dict) -> None:
    """Check a gateway video resource against OpenAI's VideoResource schema.

    The gateway reports the size it actually renders (for example 832x480).
    OpenAI's VideoSize enum lists only Sora sizes, so the size is checked as
    WxH text here and swapped for a Sora size before the schema check. Every
    other field is checked as published.
    """
    test.assertRegex(payload["size"], r"^\d+x\d+$")
    assert_openai_schema(test, "VideoResource", {**payload, "size": "1280x720"})


class RemixReplaceConformanceTests(RemixReplaceBase):
    """New remix, edit and character-replace paths return the OpenAI video shape."""

    def setUp(self) -> None:
        super().setUp()
        _patch_route_stores(self)

    def test_remix_resource_conforms_and_keeps_gateway_fields(self) -> None:
        r = self.client.post(f"/v1/videos/{rr.SOURCE_ID}/remix", json={"prompt": "make it snowy"})
        self.assertEqual(r.status_code, 200, r.text)
        _conforms_to_video_resource(self, r.json())
        self.assertEqual(r.json()["remixed_from_video_id"], rr.SOURCE_ID)

    def test_extension_and_storyboard_resources_conform(self) -> None:
        """Extensions reuse the VideoResource shape; `seconds` is the stitched total."""
        with patch.object(proxy.video_inputs, "probe_video", lambda *_a, **_k: video_inputs.VideoProbe(
                fps=24.0, duration=8.0, frames=193, width=960, height=512, has_audio=True)):
            r = self.client.post("/v1/videos/extensions",
                                 json={"video": {"id": rr.SOURCE_ID}, "prompt": "and then it rains", "seconds": 8})
        self.assertEqual(r.status_code, 200, r.text)
        _conforms_to_video_resource(self, r.json())
        self.assertEqual(r.json()["remixed_from_video_id"], rr.SOURCE_ID)
        self.assertEqual(r.json()["seconds"], "16.042")
        shots = self.client.post("/v1/videos", json={"model": "video-long", "shots": [
            {"prompt": "a", "seconds": 4}, {"prompt": "b", "seconds": 4}]})
        self.assertEqual(shots.status_code, 200, shots.text)
        _conforms_to_video_resource(self, shots.json())
        status = self.client.get(f"/v1/videos/{rr.NEW_ID}")
        _conforms_to_video_resource(self, status.json())

    def test_edit_resources_conform(self) -> None:
        upload = self.client.post(
            "/v1/videos/edits",
            data={"prompt": "to watercolor"},
            files={"video": ("clip.mp4", b"clip", "video/mp4")},
        )
        self.assertEqual(upload.status_code, 200, upload.text)
        _conforms_to_video_resource(self, upload.json())
        self.assertIsNone(upload.json()["remixed_from_video_id"])
        by_id = self.client.post("/v1/videos/edits", json={"prompt": "x", "video": {"id": rr.SOURCE_ID}})
        self.assertEqual(by_id.status_code, 200, by_id.text)
        _conforms_to_video_resource(self, by_id.json())

    def test_character_replace_resource_conforms_and_echoes_the_model(self) -> None:
        r = self.client.post(
            "/v1/videos",
            json={
                "model": "character-replace",
                "prompt": "a woman in a red raincoat",
                "extensions": {rr.NS: {
                    "source_video": {"video_id": rr.SOURCE_ID},
                    "reference_image": "data:image/png;base64," + base64.b64encode(rr.PNG).decode(),
                }},
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        _conforms_to_video_resource(self, body)
        self.assertEqual(body["model"], "character-replace")
        self.assertEqual(body["azizam_resolved_model"], "video-scail-2-replace")

    def test_job_lifecycle_is_monotonic_and_content_waits_for_completion(self) -> None:
        r = self.client.post(f"/v1/videos/{rr.SOURCE_ID}/remix", json={"prompt": "x"})
        video_id = r.json()["id"]
        seen: list[tuple[str, int]] = []

        def poll() -> dict:
            resp = self.client.get(f"/v1/videos/{video_id}")
            self.assertEqual(resp.status_code, 200, resp.text)
            body = resp.json()
            _conforms_to_video_resource(self, body)
            self.assertEqual(body["model"], "video-remix")
            self.assertEqual(body["remixed_from_video_id"], rr.SOURCE_ID)
            seen.append((body["status"], body["progress"]))
            return body

        poll()
        for percent in (12, 40, 85):
            proxy._video_progress[rr.NEW_PROMPT_ID].update(status="running", percent=percent, node="18")
            self.assertEqual(poll()["status"], "in_progress")
            content = self.client.get(f"/v1/videos/{video_id}/content")
            self.assertEqual(content.status_code, 404)
            self.assertEqual(content.json()["error"]["code"], "video_not_ready")
        # A second sampler stage restarts its own bar; the reported progress must not drop.
        proxy._video_progress[rr.NEW_PROMPT_ID].update(status="running", percent=7, node="19")
        self.assertGreaterEqual(poll()["progress"], 85)
        self.comfy.history[rr.NEW_PROMPT_ID] = rr_fake.succeeded_history("remix_00001_.mp4")
        self.comfy.view_bytes["remix_00001_.mp4"] = _rendered_mp4()
        done = poll()
        self.assertEqual((done["status"], done["progress"]), ("completed", 100))
        self.assertIsInstance(done["completed_at"], int)
        full = self.client.get(f"/v1/videos/{video_id}/content")
        self.assertEqual(full.status_code, 403)  # No retained maker identity for this caller.
        content = self.client.get(f"/v1/videos/{video_id}/content?metadata=none")
        self.assertEqual(content.status_code, 200)
        expected = proxy.media_metadata.strip_metadata(_rendered_mp4(), ".mp4")
        self.assertEqual(content.content, expected)
        self.assertEqual(content.headers["content-type"], "video/mp4")
        self.assertEqual([s for s, _ in seen][0], "queued")
        progress = [p for _, p in seen]
        self.assertEqual(progress, sorted(progress))

    def test_failed_job_reports_failed_status_with_an_error_object(self) -> None:
        r = self.client.post(f"/v1/videos/{rr.SOURCE_ID}/remix", json={"prompt": "x"})
        video_id = r.json()["id"]
        self.comfy.history[rr.NEW_PROMPT_ID] = {
            "status": {"completed": False, "status_str": "error", "messages": [
                ["execution_error", {"exception_message": "out of memory", "node_id": "18"}],
            ]},
            "outputs": {},
        }
        body = self.client.get(f"/v1/videos/{video_id}").json()
        _conforms_to_video_resource(self, body)
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error"]["code"], "video_generation_failed")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
