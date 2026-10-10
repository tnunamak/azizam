from __future__ import annotations

import gzip

import brotli
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from gateway.ui_static import UIStaticFiles


def _app_with_ui(directory):
    app = FastAPI()
    app.mount("/ui", UIStaticFiles(directory=str(directory), html=True), name="ui")

    @app.get("/api/data")
    def api_data():
        return {"data": "uncompressed api payload " * 30}

    @app.get("/events")
    def events():
        return StreamingResponse(iter(["data: hello\n\n"]), media_type="text/event-stream")

    # This route intentionally uses Starlette's plain static handler to check
    # that the gateway change is scoped to the UI mount.
    app.mount("/other", StaticFiles(directory=str(directory)), name="other")
    return app


def test_hashed_chunk_is_brotli_compressed_and_immutable(tmp_path):
    asset = tmp_path / "_next/static/chunks/app-0123456789abcdef.js"
    asset.parent.mkdir(parents=True)
    original = b"const payload = 'static js';\n" * 200
    asset.write_bytes(original)
    client = TestClient(_app_with_ui(tmp_path))

    with client.stream(
        "GET",
        "/ui/_next/static/chunks/app-0123456789abcdef.js",
        headers={"Accept-Encoding": "gzip, br"},
    ) as response:
        encoded = b"".join(response.iter_raw())

    assert response.status_code == 200
    assert response.headers["content-encoding"] == "br"
    assert "Accept-Encoding" in response.headers["vary"]
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert brotli.decompress(encoded) == original

    with client.stream(
        "GET",
        "/ui/_next/static/chunks/app-0123456789abcdef.js",
        headers={"Accept-Encoding": "br;q=0, gzip;q=0.5, identity;q=1"},
    ) as response:
        identity = b"".join(response.iter_raw())
    assert "content-encoding" not in response.headers
    assert identity == original

    with client.stream(
        "GET",
        "/ui/_next/static/chunks/app-0123456789abcdef.js",
        headers={"Accept-Encoding": "br;q=0, gzip;q=1, identity;q=0"},
    ) as response:
        encoded_gzip = b"".join(response.iter_raw())
    assert response.headers["content-encoding"] == "gzip"
    assert gzip.decompress(encoded_gzip) == original

    refused = client.get(
        "/ui/_next/static/chunks/app-0123456789abcdef.js",
        headers={"Accept-Encoding": "br;q=0, gzip;q=0, identity;q=0"},
    )
    assert refused.status_code == 406


def test_html_revalidates_and_head_and_range_keep_file_semantics(tmp_path):
    page = tmp_path / "home/index.html"
    page.parent.mkdir()
    page.write_bytes(b"<html>page</html>")
    client = TestClient(_app_with_ui(tmp_path))

    first = client.get("/ui/home/", headers={"Accept-Encoding": "br"})
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-cache"
    assert first.headers["etag"] and first.headers["last-modified"]
    assert first.headers["content-encoding"] == "br"

    repeated = client.get("/ui/home/", headers={
        "Accept-Encoding": "br", "If-None-Match": first.headers["etag"],
    })
    assert repeated.status_code == 304
    assert repeated.headers["cache-control"] == "no-cache"

    head = client.head("/ui/home/", headers={"Accept-Encoding": "br"})
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == first.headers["content-length"]
    assert head.headers["content-encoding"] == "br"

    identity_head = client.head("/ui/home/", headers={"Accept-Encoding": "identity"})
    assert identity_head.status_code == 200
    assert identity_head.content == b""
    assert identity_head.headers["content-length"] == str(page.stat().st_size)
    assert "content-encoding" not in identity_head.headers

    ranged = client.get("/ui/home/", headers={
        "Accept-Encoding": "br", "Range": "bytes=0-4",
    })
    assert ranged.status_code == 206
    assert ranged.content == b"<html"
    assert "content-encoding" not in ranged.headers
    assert ranged.headers["content-range"].startswith("bytes 0-4/")

    refused_range = client.get("/ui/home/", headers={
        "Accept-Encoding": "br, gzip, identity;q=0", "Range": "bytes=0-4",
    })
    assert refused_range.status_code == 406


def test_api_streaming_and_non_ui_static_routes_are_untouched(tmp_path):
    asset = tmp_path / "sample.js"
    asset.write_text("const value = 'static';\n" * 30)
    client = TestClient(_app_with_ui(tmp_path))
    headers = {"Accept-Encoding": "gzip, br"}

    api = client.get("/api/data", headers=headers)
    assert "content-encoding" not in api.headers
    assert "cache-control" not in api.headers

    events = client.get("/events", headers=headers)
    assert events.headers["content-type"].startswith("text/event-stream")
    assert "content-encoding" not in events.headers
    assert "cache-control" not in events.headers

    other = client.get("/other/sample.js", headers=headers)
    assert "content-encoding" not in other.headers
    assert "cache-control" not in other.headers
