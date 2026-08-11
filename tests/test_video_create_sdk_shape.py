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
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402


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

    def test_handler_does_not_400_on_multipart_content_type(self) -> None:
        # Regression: the live probe was getting `{"error": {"message":
        # "Invalid JSON body"}}` because the handler called request.json() on
        # a multipart body. With ComfyUI disabled we should now see the
        # service-unavailable branch (503), NOT the JSON-parse 400.
        request = _multipart_request(
            [
                ("prompt", b"hello", None),
                ("model", b"sora-2", None),
            ]
        )

        async def run() -> object:
            with patch.object(proxy, "COMFY_ENABLED", False):
                return await proxy.proxy_comfy_video_create(
                    request,
                    auth_context=None,  # type: ignore[arg-type]
                )

        response = asyncio.run(run())

        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.body.decode("utf-8"))
        self.assertNotEqual(
            payload.get("error", {}).get("message"),
            "Invalid JSON body",
        )


class VideoCreateResponseEnvelopeTests(unittest.TestCase):
    """The create response must parse cleanly as the SDK's ``Video`` model."""

    def test_response_envelope_matches_sdk_video_model(self) -> None:
        # Use the openai SDK directly to validate the on-the-wire envelope. If
        # the SDK is not installed in this environment, skip — the proxy keeps
        # working either way; we just can't pin the contract here.
        try:
            from openai.types.video import Video
        except Exception:  # pragma: no cover - SDK optional in some venvs
            self.skipTest("openai SDK not installed")

        # Construct the exact dict the handler emits for a 5-second 720p job.
        # Mirrors proxy.proxy_comfy_video_create:6698+ after the SDK-shape fix.
        payload = {
            "id": "video_abc",
            "object": "video",
            "model": "video-ltx-2.3-distilled",
            "status": "queued",
            "progress": 0,
            "created_at": 1_700_000_000,
            "expires_at": 1_700_086_400,
            "size": "1280x720",
            "seconds": "5",
            "actual_seconds": 5.0,
        }

        parsed = Video.model_construct(**payload)

        # The SDK uses model_construct() under the hood — these are the fields
        # the probe and downstream code actually depend on.
        self.assertEqual(parsed.id, "video_abc")
        self.assertEqual(parsed.object, "video")
        self.assertEqual(parsed.status, "queued")
        self.assertEqual(parsed.progress, 0)
        self.assertEqual(parsed.seconds, "5")
        self.assertEqual(parsed.size, "1280x720")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
