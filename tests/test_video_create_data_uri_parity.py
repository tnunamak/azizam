"""Pin large text data-URI parity without HTTP, credentials, or media services."""
import asyncio
import base64
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import Response

from gateway.frontend_shapes.video_create import _parse_body
from gateway.video_form import read_video_form_body


def data_uri(mime: str) -> str:
    # Exactly 3 MiB before encoding, comfortably above the generic 1 MiB part cap.
    raw = bytes(range(256)) * (3 * 1024 * 1024 // 256)
    assert len(raw) == 3 * 1024 * 1024
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def request_from_bytes(body: bytes, content_type: str) -> Request:
    offset = 0

    async def receive():
        nonlocal offset
        if offset >= len(body):
            return {"type": "http.disconnect"}
        end = min(offset + 64 * 1024, len(body))
        chunk = body[offset:end]
        offset = end
        return {"type": "http.request", "body": chunk, "more_body": offset < len(body)}

    return Request({
        "type": "http", "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": "/v1/videos", "raw_path": b"/v1/videos",
        "query_string": b"",
        "headers": [(b"content-type", content_type.encode("ascii")),
                    (b"content-length", str(len(body)).encode("ascii"))],
    }, receive)


def multipart_text(name: str, value: str) -> tuple[bytes, str]:
    boundary = "synthetic-video-parity-boundary"
    # Deliberately no filename: a data URI is a text part, not an uploaded file.
    body = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n'
            "\r\n" + value + f"\r\n--{boundary}--\r\n").encode("ascii")
    return body, f"multipart/form-data; boundary={boundary}"


async def assert_parity(body: bytes, content_type: str, expected: dict) -> None:
    # Each parser gets its own request/stream; neither sees the other's cached form.
    shape = await _parse_body(request_from_bytes(body, content_type))
    legacy = await read_video_form_body(request_from_bytes(body, content_type))
    assert not isinstance(shape, Response)
    assert not isinstance(legacy, Response)
    assert isinstance(shape, dict)
    assert isinstance(legacy, dict)
    assert shape == expected
    assert legacy == expected
    assert shape == legacy


def test_large_multipart_image_reference_parity():
    value = data_uri("image/png")
    body, content_type = multipart_text("input_reference", value)
    asyncio.run(assert_parity(body, content_type, {"input_reference": value}))


def test_large_multipart_nested_video_source_parity():
    value = data_uri("video/mp4")
    body, content_type = multipart_text("video[source]", value)
    asyncio.run(assert_parity(body, content_type, {"video": {"source": value}}))


def test_large_urlencoded_image_reference_parity():
    value = data_uri("image/png")
    # Real form encoding protects base64 '+' characters from being read as spaces.
    body = urlencode({"input_reference": value}).encode("ascii")
    asyncio.run(assert_parity(body, "application/x-www-form-urlencoded",
                              {"input_reference": value}))
