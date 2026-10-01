from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import Response

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402


class _Upload:
    filename = "sample.mp4"
    content_type = "video/mp4"

    async def read(self) -> bytes:
        return b"input-video-bytes"


class _Form:
    def get(self, name: str, default=None):
        return {"source_video": _Upload(), "multiplier": "2"}.get(name, default)


class _Request:
    async def form(self):
        return _Form()


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, content: bytes = b"") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content
        self.text = ""

    def json(self):
        return self._payload


class VideoWorkflowExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_manifest_video_workflow_runs_against_fake_comfyui(self) -> None:
        requests: list[tuple[str, dict]] = []

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, url, **kwargs):
                if url.endswith("/upload/image"):
                    self_upload = kwargs["files"]["image"]
                    self_outer.assertEqual(self_upload[1], b"input-video-bytes")
                    return _FakeResponse(200, {"name": "sample.mp4"})
                if url.endswith("/prompt"):
                    requests.append((url, kwargs["json"]))
                    return _FakeResponse(200, {"prompt_id": "prompt-1"})
                raise AssertionError(f"unexpected POST {url}")

            async def get(self, url, **kwargs):
                if url.endswith("/history/prompt-1"):
                    return _FakeResponse(200, {"prompt-1": {
                        "status": {"completed": True},
                        "outputs": {"6": {"videos": [{"filename": "rife.mp4", "type": "output"}]}},
                    }})
                if url.endswith("/view"):
                    return _FakeResponse(200, content=b"rendered-video")
                raise AssertionError(f"unexpected GET {url}")

        self_outer = self
        with patch.object(proxy.httpx, "AsyncClient", FakeClient), patch.object(proxy.asyncio, "sleep", return_value=None):
            response = await proxy._execute_video_manifest_workflow(
                _Request(), "rife-interpolate", proxy.NO_AUTH_CONTEXT,  # type: ignore[arg-type]
            )

        self.assertIsInstance(response, Response)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b"rendered-video")
        self.assertEqual(response.media_type, "video/mp4")
        graph = requests[0][1]["prompt"]
        self.assertEqual(graph["1"]["inputs"]["file"], "sample.mp4")
        self.assertEqual(graph["3"]["inputs"]["multiplier"], 2)
        self.assertEqual(graph["4"]["inputs"]["b"], 2)


if __name__ == "__main__":
    unittest.main()
