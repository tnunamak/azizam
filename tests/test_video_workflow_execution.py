from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import Response

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from tests.support.access import access_context  # noqa: E402
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: E402,F401


class _Upload:
    filename = "sample.mp4"
    content_type = "video/mp4"

    async def read(self) -> bytes:
        return b"input-video-bytes"


class _Form:
    def keys(self):
        return ("source_video", "multiplier")

    def get(self, name: str, default=None):
        return {"source_video": _Upload(), "multiplier": "2"}.get(name, default)


class _Request:
    async def form(self):
        return _Form()


class VideoWorkflowExecutionTests(unittest.IsolatedAsyncioTestCase):
    @pytest.fixture(autouse=True)
    def _inject(self, fake_upstreams, monkeypatch):
        self.fakes = fake_upstreams
        self.monkeypatch = monkeypatch

    def test_video_output_prefers_downstream_result_over_input_echo(self) -> None:
        for outputs in (
            {
                "1": {"images": [{"filename": "input.mp4", "type": "input"}]},
                "6": {"images": [{"filename": "rendered.mp4", "type": "output"}]},
            },
            {
                "6": {"images": [{"filename": "rendered.mp4", "type": "output"}]},
                "1": {"images": [{"filename": "input.mp4", "type": "input"}]},
            },
        ):
            with self.subTest(outputs=list(outputs)):
                output = proxy._extract_video_output({"outputs": outputs})
                self.assertEqual(output["filename"], "rendered.mp4")

    async def test_manifest_video_workflow_runs_against_fake_comfyui(self) -> None:
        fake = self.fakes.asgi(load_scenario("comfyui/video-manifest-run"))
        self.monkeypatch.setattr(proxy, "COMFY_HOST", fake.base_url)
        with patch.object(proxy.asyncio, "sleep", return_value=None):
            response = await proxy._execute_video_manifest_workflow(
                _Request(), "rife-interpolate", access_context(),  # type: ignore[arg-type]
            )

        self.assertIsInstance(response, Response)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b"rendered-video")
        self.assertEqual(response.media_type, "video/mp4")
        [upload] = fake.calls("upload")
        self.assertIn(b"input-video-bytes", upload.body)
        [submitted] = fake.calls("prompt")
        graph = submitted.json["prompt"]
        self.assertNotIn("_meta", graph)
        self.assertEqual(graph["1"]["inputs"]["file"], "uploaded-sample.mp4")
        self.assertEqual(graph["3"]["inputs"]["multiplier"], 2)
        self.assertEqual(graph["4"]["inputs"]["b"], 2)

    async def test_a_refused_source_upload_stops_before_any_submission(self) -> None:
        fake = self.fakes.asgi(load_scenario("comfyui/video-manifest-upload-failed"))
        self.monkeypatch.setattr(proxy, "COMFY_HOST", fake.base_url)
        response = await proxy._execute_video_manifest_workflow(
            _Request(), "rife-interpolate", proxy.NO_AUTH_CONTEXT,  # type: ignore[arg-type]
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(json.loads(response.body)["error"]["message"], "ComfyUI upload failed for source_video")
        self.assertEqual([request.exchange for request in fake.requests], ["upload"])


    async def test_an_out_of_memory_submit_on_an_idle_engine_frees_models_and_submits_again(self) -> None:
        fake = self.fakes.asgi(load_scenario("comfyui/video-manifest-oom-then-complete"))
        self.monkeypatch.setattr(proxy, "COMFY_HOST", fake.base_url)
        self.monkeypatch.setattr(proxy, "IMAGE_FREE_VRAM_ON_EXHAUSTION", True)
        with patch.object(proxy.asyncio, "sleep", return_value=None):
            response = await proxy._execute_video_manifest_workflow(
                _Request(), "rife-interpolate", proxy.NO_AUTH_CONTEXT,  # type: ignore[arg-type]
            )

        self.assertEqual(response.status_code, 200, response.body)
        self.assertEqual(response.body, b"rendered-video")
        self.assertEqual(
            [request.exchange for request in fake.requests],
            ["upload", "prompt-oom", "queue", "free", "prompt", "history", "view"],
        )


if __name__ == "__main__":
    unittest.main()
