"""Asset content types: an engine's Content-Type is a claim; the bytes and an allowlist decide what is served."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from gateway.engine_protocol import assets as media
from gateway.engine_protocol import description, executor, transport
from gateway.engine_protocol.assets import MemoryAssetStore
from gateway.engine_protocol.errors import MalformedResponse
from gateway.engine_protocol.types import CanonicalRequest, Connection
from tests.support.fake_upstream import HandlerTransport

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 12
GIF = b"GIF89a" + b"\x00" * 10
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 "
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt "
FLAC = b"fLaC\x00\x00\x00\x22" + b"\x00" * 8
OGG = b"OggS\x00\x02" + b"\x00" * 10
MP3_ID3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 6
MP3_FRAME = b"\xff\xfb\x90\x64" + b"\x00" * 12
AAC = b"\xff\xf1\x50\x80" + b"\x00" * 12
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00"
M4A = b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00"
MOV = b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00"
AVIF = b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00"
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 12
HTML = b"<!doctype html><script>alert(1)</script>"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>'


@pytest.mark.parametrize("declared, head, served", [
    ("image/png", PNG, "image/png"),
    ("IMAGE/PNG; charset=binary", PNG, "image/png"),
    ("image/png", JPEG, "image/jpeg"),            # the bytes win over the label
    (None, PNG, "image/png"),
    ("application/octet-stream", PNG, "image/png"),
    ("image/jpeg", JPEG, "image/jpeg"), ("image/gif", GIF, "image/gif"), ("image/webp", WEBP, "image/webp"),
    ("image/avif", AVIF, "image/avif"),
    ("audio/wav", WAV, "audio/wav"), ("audio/x-wav", WAV, "audio/x-wav"), ("audio/flac", FLAC, "audio/flac"),
    ("audio/ogg", OGG, "audio/ogg"), ("audio/opus", OGG, "audio/opus"),
    ("audio/mpeg", MP3_ID3, "audio/mpeg"), ("audio/mpeg", MP3_FRAME, "audio/mpeg"), ("audio/mp3", MP3_ID3, "audio/mp3"),
    ("audio/aac", AAC, "audio/aac"), (None, AAC, "audio/aac"),
    ("video/mp4", MP4, "video/mp4"), ("audio/mp4", MP4, "audio/mp4"), ("audio/mp4", M4A, "audio/mp4"), (None, M4A, "audio/mp4"),
    ("video/quicktime", MOV, "video/quicktime"),
    ("video/webm", WEBM, "video/webm"), ("audio/webm", WEBM, "audio/webm"),
    ("application/octet-stream", b"\x01\x02\x03", "application/octet-stream"),
    (None, b"\x01\x02\x03", "application/octet-stream"),
    ("audio/pcm", b"\x01\x02\x03\x04", "audio/pcm"),
    ("application/octet-stream", b"", "application/octet-stream"),
])
def test_the_served_type_follows_the_bytes_within_the_allowlist(declared, head, served) -> None:
    assert media.served_media_type(declared, head) == served


@pytest.mark.parametrize("declared, head", [
    ("text/html", HTML), ("text/html", PNG), ("image/svg+xml", SVG), ("application/javascript", b"alert(1)"), ("text/plain", b"hello"),
    ("application/xhtml+xml", HTML), ("application/pdf", b"%PDF-1.7"),
    ("image/png", HTML), ("image/png", SVG), ("image/png", b""), ("audio/wav", b"not a wave file"), ("audio/mpeg", HTML),
    ("application/octet-stream", HTML), ("application/octet-stream", SVG), (None, b"  \n<?xml version='1.0'?>"),
    ("image/png\r\nX-Evil: 1", PNG), ("image/png\nx", PNG), ("image/ png", PNG), ("../../etc", PNG), ("", HTML),
])
def test_a_type_outside_the_allowlist_or_bytes_that_do_not_match_it_are_refused(declared, head) -> None:
    assert media.served_media_type(declared, head) is None


def test_only_the_base_type_is_ever_served() -> None:
    served = media.served_media_type('image/png; charset="x\r\nSet-Cookie: a=b"', PNG)
    assert served == "image/png"


def test_no_active_type_is_on_the_allowlist() -> None:
    for risky in ("text/html", "image/svg+xml", "application/javascript", "text/javascript", "application/xhtml+xml", "text/xml", "application/xml",
                  "text/css", "application/pdf", "text/plain", "application/x-shockwave-flash"):
        assert risky not in media.ALLOWED_MEDIA_TYPES


# --- through the executor ----------------------------------------------------------------------

BASE = "http://engine.test:8080"
BIN = {"get": {"class": "generate", "request": {"method": "GET", "path": "/f"}, "response": {"framing": "binary"}}}


def fetch(content: bytes | list[bytes], content_type: str | None):
    store = MemoryAssetStore()
    headers = {"content-type": content_type} if content_type is not None else {}
    body = httpx.Response(200, content=content, headers=headers) if isinstance(content, bytes) else None

    class Pieces(httpx.AsyncByteStream):
        async def __aiter__(self):
            for piece in content:
                yield piece

        async def aclose(self) -> None:
            pass
    answer = body if body is not None else httpx.Response(200, stream=Pieces(), headers=headers)
    d = description.parse_description({"protocol_version": 1, "id": "t", "executor": "http-v1", "operations": BIN}, source="t.yml")
    ex = executor.HttpV1Executor(d, transport=transport.HttpxTransport.for_tests(HandlerTransport(lambda r: answer)), assets=store)
    reply = asyncio.run(ex.invoke(Connection(address=BASE), CanonicalRequest(op="get")))
    return reply.asset, store


def test_a_saved_asset_carries_the_sniffed_type_not_the_claimed_one() -> None:
    asset, _ = fetch(JPEG, "image/png")
    assert asset.content_type == "image/jpeg"


def test_the_first_bytes_may_arrive_one_at_a_time() -> None:
    asset, store = fetch([PNG[i:i + 1] for i in range(len(PNG))] + [b"rest"], "image/png")
    assert asset.content_type == "image/png" and store.get(asset) == PNG + b"rest"


@pytest.mark.parametrize("declared, content", [("text/html", HTML), ("image/svg+xml", SVG), ("image/png", HTML), ("text/html", PNG)])
def test_an_answer_the_allowlist_refuses_is_a_malformed_response_and_nothing_is_saved(declared: str, content: bytes) -> None:
    store = MemoryAssetStore()
    d = description.parse_description({"protocol_version": 1, "id": "t", "executor": "http-v1", "operations": BIN}, source="t.yml")
    answer = httpx.Response(200, content=content, headers={"content-type": declared})
    ex = executor.HttpV1Executor(d, transport=transport.HttpxTransport.for_tests(HandlerTransport(lambda r: answer)), assets=store)
    with pytest.raises(MalformedResponse) as raised:
        asyncio.run(ex.invoke(Connection(address=BASE), CanonicalRequest(op="get")))
    assert store.count() == 0 and "html" not in str(raised.value).lower() and "svg" not in str(raised.value).lower()
