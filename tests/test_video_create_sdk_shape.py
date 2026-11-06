"""SDK-shape compatibility tests for POST /v1/videos.

The openai-python SDK's ``client.videos.create(...)`` always sends a
``multipart/form-data`` request — even when ``input_reference`` is omitted —
so the gateway must accept that envelope, not only application/json. These
tests pin the request-parsing contract without requiring ComfyUI.
"""

from __future__ import annotations

import asyncio
import base64
import io
import sys
import unittest
from pathlib import Path

from starlette.requests import Request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from test_video_remix_replace import NEW_ID, RemixReplaceBase  # noqa: E402


def _multipart_request(parts: list[tuple[str, bytes, dict[str, str] | None]]) -> Request:
    """Build a Starlette Request that carries a real multipart/form-data body.

    Each ``part`` is (field_name, raw_bytes, extra_headers). When
    ``extra_headers`` contains ``filename`` / ``content-type`` the part is
    encoded as a file upload; otherwise it is encoded as a text field.
    """
    boundary = "----opxtestboundary"
    buf = io.BytesIO()
    for name, raw, extra in parts:
        extra = extra or {}
        buf.write(b"--" + boundary.encode() + b"\r\n")
        disposition = f'form-data; name="{name}"'
        if "filename" in extra:
            disposition += f'; filename="{extra["filename"]}"'
        buf.write(b"Content-Disposition: " + disposition.encode() + b"\r\n")
        if "content-type" in extra:
            buf.write(b"Content-Type: " + extra["content-type"].encode() + b"\r\n")
        buf.write(b"\r\n")
        buf.write(raw)
        buf.write(b"\r\n")
    buf.write(b"--" + boundary.encode() + b"--\r\n")
    body = buf.getvalue()

    sent = {"done": False}

    async def receive() -> dict:
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/videos",
        "raw_path": b"/v1/videos",
        "query_string": b"",
        "headers": [
            (b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
            (b"content-length", str(len(body)).encode()),
            (b"host", b"gateway.test"),
        ],
        "scheme": "http",
        "server": ("gateway.test", 80),
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope, receive)


class VideoCreateMultipartParsingTests(unittest.TestCase):
    """``client.videos.create`` always sends multipart — make sure we accept it."""

    def test_text_only_multipart_form_normalizes_to_json_body(self) -> None:
        # The exact shape openai-python emits when only prompt/model are passed:
        # all values are sent as text fields, no input_reference attachment.
        request = _multipart_request(
            [
                ("prompt", b"A small blue square on white.", None),
                ("model", b"ltx-2.3-distilled", None),
            ]
        )

        body = asyncio.run(proxy._read_video_create_form_body(request))

        self.assertEqual(body["prompt"], "A small blue square on white.")
        self.assertEqual(body["model"], "ltx-2.3-distilled")
        # No file uploads => no input_reference key surfaces.
        self.assertNotIn("input_reference", body)

    def test_input_reference_upload_becomes_data_uri(self) -> None:
        # SDK uploads input_reference as a file. We project that into the same
        # `data:` URI shape the existing input_reference resolver already accepts,
        # so the downstream pipeline does not need a new branch.
        png_bytes = b"\x89PNG\r\n\x1a\nfakepayload"
        request = _multipart_request(
            [
                ("prompt", b"ignore", None),
                (
                    "input_reference",
                    png_bytes,
                    {"filename": "ref.png", "content-type": "image/png"},
                ),
            ]
        )

        async def parse() -> tuple[dict, bool]:
            body = await proxy._read_video_create_form_body(request)
            form = await request.form()
            upload = form["input_reference"]
            return body, upload.file.closed

        body, upload_closed = asyncio.run(parse())

        self.assertTrue(body["input_reference"].startswith("data:image/png;base64,"))
        encoded = body["input_reference"].split(",", 1)[1]
        self.assertEqual(base64.b64decode(encoded), png_bytes)
        self.assertTrue(upload_closed, "multipart upload must be closed after normalization")


class VideoCreateRealSdkTests(RemixReplaceBase):
    """The real openai-python client talks to the real app over a fake ComfyUI."""

    def test_real_sdk_create_submits_multipart_and_parses_gateway_response(self) -> None:
        import openai

        sdk = openai.OpenAI(
            base_url="http://testserver/v1",
            api_key="test",
            http_client=self.client,
            max_retries=0,
        )
        try:
            video = sdk.videos.create(
                model="sora-2",
                prompt="a red kite over the sea",
                seconds="4",
                size="1280x720",
            )
        finally:
            sdk.close()

        self.assertEqual(len(self.comfy.submitted), 1)
        self.assertEqual(self.comfy.uploads, [])
        graph = self.comfy.submitted[0]
        self.assertEqual(graph["5"]["inputs"]["text"], "a red kite over the sea")
        self.assertEqual(graph["20"]["inputs"]["value"], 1280)
        self.assertEqual(graph["21"]["inputs"]["value"], 704)
        self.assertEqual(graph["22"]["inputs"]["value"], 97)
        self.assertEqual(video.id, NEW_ID)
        self.assertEqual(video.object, "video")
        self.assertEqual(video.model, "sora-2")
        self.assertEqual(video.status, "queued")
        self.assertEqual(video.progress, 0)
        self.assertIsInstance(video.created_at, int)
        self.assertEqual(video.size, "1280x704")
        self.assertEqual(video.seconds, "4")
        self.assertAlmostEqual(video.actual_seconds, 97 / 24, places=3)
        self.assertIsNone(video.remixed_from_video_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
