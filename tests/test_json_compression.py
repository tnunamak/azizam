"""gzip for the app's own JSON API: which answers get it, and that the bytes are unchanged."""
from __future__ import annotations

import asyncio
import gzip

import pytest

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.testclient import TestClient

from gateway.json_compression import JsonCompressionMiddleware

BIG = {"rows": [{"id": f"row-{n}", "name": "x" * 30} for n in range(200)]}


def _client() -> TestClient:
    app = FastAPI()
    app.add_middleware(JsonCompressionMiddleware)

    @app.get("/me/api/big")
    def big():
        return JSONResponse(BIG)

    @app.get("/me/api/small")
    def small():
        return JSONResponse({"ok": True})

    @app.get("/admin/api/text")
    def text():
        return PlainTextResponse("x" * 5000)

    @app.get("/me/api/stream")
    def stream():
        return StreamingResponse(iter([b'data: {"a": 1}\n\n'] * 200), media_type="text/event-stream")

    @app.get("/v1/models")
    def models():
        return JSONResponse(BIG)

    @app.post("/me/api/big")
    def post_big():
        return JSONResponse(BIG)

    @app.get("/me/api/missing")
    def missing():
        return JSONResponse({"error": "x" * 3000}, status_code=404)

    return TestClient(app)


def test_a_large_json_answer_is_gzipped_and_decodes_to_the_same_bytes() -> None:
    plain = _client().get("/me/api/big", headers={"accept-encoding": "identity"})
    packed = _client().get("/me/api/big", headers={"accept-encoding": "gzip"})
    assert packed.headers["content-encoding"] == "gzip" and packed.headers["vary"] == "Accept-Encoding"
    assert int(packed.headers["content-length"]) < len(plain.content) / 3
    assert packed.json() == plain.json() == BIG
    assert "content-encoding" not in plain.headers


def test_what_should_not_be_compressed_is_not() -> None:
    client = _client()
    gz = {"accept-encoding": "gzip"}
    assert "content-encoding" not in client.get("/me/api/small", headers=gz).headers  # under 1 KB
    assert "content-encoding" not in client.get("/admin/api/text", headers=gz).headers  # not JSON
    assert "content-encoding" not in client.get("/v1/models", headers=gz).headers  # not the app's own API
    assert "content-encoding" not in client.post("/me/api/big", headers=gz).headers  # reads only
    assert "content-encoding" not in client.get("/me/api/missing", headers=gz).headers  # errors stay plain
    stream = client.get("/me/api/stream", headers=gz)
    assert "content-encoding" not in stream.headers and stream.text.count("data:") == 200


def _capture(accepts, *, size=2048, method="GET", status=200,
             content_type=b"application/json", encoded=None, path="/me/api/big"):
    body = b'"' + b"x" * (size - 2) + b'"'
    headers = [(b"content-type", content_type), (b"content-length", str(size).encode()),
               (b"etag", b'"os-7"'), (b"vary", b"Origin")]
    if encoded is not None:
        headers.append((b"content-encoding", encoded))
    messages = [
        {"type": "http.response.start", "status": status, "headers": headers},
        {"type": "http.response.body", "body": body[:500], "more_body": True},
        {"type": "http.response.body", "body": body[500:], "more_body": False},
    ]
    captured = []

    async def app(scope, receive, send):
        for message in messages:
            await send(message)

    async def receive():
        raise AssertionError("response-only middleware must not read requests")

    async def send(message):
        captured.append(message)

    asyncio.run(JsonCompressionMiddleware(app)(
        {"type": "http", "method": method, "path": path,
         "headers": [(b"accept-encoding", value) for value in accepts]}, receive, send))
    return body, messages, captured


@pytest.mark.parametrize("accepts,compressed", [
    ([b"gzip;q=0"], False),
    ([b"GZIP"], True),
    ([b"deflate, gzip;q=0.5"], True),
    ([b"gzip", b"br"], True),
    ([b"*"], True),
    ([b"not-gzip"], False),
    ([b"x-gzip"], True),
    ([b"gzip;q=0, *;q=1"], False),
    ([b"x-gzip;q=0, *"], False),
    ([b"gzip;q=bogus, *"], False),
    ([b"gzip;q=NaN"], False),
    ([b"gzip;q=2"], False),
    ([b"gzip;q=-1"], False),
    ([b"gzip;q=0.0001"], False),
    ([b"gzip;q"], False),
    ([b"gzip;q=1;q=0"], False),
    ([b" GzIp ; Q=0.500 "], True),
    ([b"br, *;q=0.5"], True),
    ([b"*;q=0"], False),
    ([], False),
])
def test_encoding_negotiation_and_identity_vary(accepts, compressed):
    body, _, messages = _capture(accepts)
    headers = messages[0]["headers"]
    encodings = [value for key, value in headers if key.lower() == b"content-encoding"]
    assert encodings == ([b"gzip"] if compressed else [])
    wire = b"".join(message.get("body", b"") for message in messages[1:])
    assert (gzip.decompress(wire) if compressed else wire) == body
    assert (b"content-length", str(len(wire)).encode()) in headers
    assert (b"vary", b"Origin") in headers
    assert (b"vary", b"Accept-Encoding") in headers
    assert [(key, value) for key, value in headers if key == b"etag"] == [(b"etag", b'"os-7"')]


@pytest.mark.parametrize("size,compressed", [(1023, False), (1024, True)])
def test_exact_compression_threshold_and_strong_etag(size, compressed):
    body, _, messages = _capture([b"gzip"], size=size)
    headers = messages[0]["headers"]
    wire = b"".join(message.get("body", b"") for message in messages[1:])
    encodings = [value for key, value in headers if key.lower() == b"content-encoding"]
    assert encodings == ([b"gzip"] if compressed else [])
    assert (gzip.decompress(wire) if compressed else wire) == body
    assert (b"vary", b"Accept-Encoding") in headers
    assert (b"etag", b'"os-7"') in headers


@pytest.mark.parametrize("options", [
    {"method": "HEAD"}, {"status": 404}, {"encoded": b"br"},
    {"content_type": b"text/event-stream"}, {"path": "/v1/models"},
])
@pytest.mark.parametrize("accepts", [[b"gzip"], [b"gzip;q=0"]])
def test_excluded_responses_keep_every_message_and_header(options, accepts):
    _, original, captured = _capture(accepts, **options)
    assert captured == original


@pytest.mark.parametrize("accepts", [b"identity", b"gzip;q=0"], ids=["identity", "gzip-refused"])
@pytest.mark.parametrize("with_length", [False, True], ids=["no-length", "length"])
@pytest.mark.parametrize("vary", [
    [],
    [(b"vary", b"Origin")],
    [(b"vary", b"Origin"), (b"VaRy", b"Cookie, aCcEpT-EnCoDiNg")],
], ids=["no-vary", "origin", "already-negotiated"])
def test_refused_gzip_delivers_start_and_chunk_before_final_body(accepts, with_length, vary):
    async def exercise():
        final_allowed = asyncio.Event()
        waiting_for_final = asyncio.Event()
        captured = []
        first = b'{"value":"' + b"x" * 2048
        last = b'"}'
        headers = [(b"content-type", b"application/json"), (b"etag", b'"os-7"'),
                   (b"x-fixture", b"unchanged"), *vary]
        if with_length:
            headers.append((b"content-length", str(len(first) + len(last)).encode()))
        start = {"type": "http.response.start", "status": 200, "headers": headers}
        chunk = {"type": "http.response.body", "body": first, "more_body": True}
        final = {"type": "http.response.body", "body": last, "more_body": False}

        async def app(scope, receive, send):
            await send(start)
            await send(chunk)
            waiting_for_final.set()
            await final_allowed.wait()
            await send(final)

        async def receive():
            raise AssertionError("response-only middleware must not read requests")

        async def send(message):
            captured.append(message)

        task = asyncio.create_task(JsonCompressionMiddleware(app)(
            {"type": "http", "method": "GET", "path": "/me/api/streaming-json",
             "headers": [(b"accept-encoding", accepts)]}, receive, send))
        try:
            await asyncio.wait_for(waiting_for_final.wait(), timeout=2)
            assert not final_allowed.is_set()
            expected_headers = list(headers)
            if not any(b"accept-encoding" == token.strip().lower()
                       for _, value in vary for token in value.split(b",")):
                expected_headers.append((b"vary", b"Accept-Encoding"))
            assert captured == [{**start, "headers": expected_headers}, chunk]
            assert start["headers"] == headers
        finally:
            final_allowed.set()
            await asyncio.wait_for(task, timeout=2)
        assert captured == [{**start, "headers": expected_headers}, chunk, final]

    asyncio.run(exercise())
