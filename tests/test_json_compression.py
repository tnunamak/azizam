"""gzip for the app's own JSON API: which answers get it, and that the bytes are unchanged."""
from __future__ import annotations

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
