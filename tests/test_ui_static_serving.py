from __future__ import annotations

import gzip
import os
import time
from datetime import datetime, timezone

import brotli
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from gateway.ui_routes import UiRoute


async def _no_admin_guard(request, name):
    raise AssertionError("no admin page is requested here")


def _app_with_ui(directory):
    app = FastAPI()
    (directory / "index.html").write_bytes(b"<html>home</html>")

    @app.get("/api/data")
    def api_data():
        return {"data": "uncompressed api payload " * 30}

    @app.get("/events")
    def events():
        return StreamingResponse(iter(["data: hello\n\n"]), media_type="text/event-stream")

    # This route intentionally uses Starlette's plain static handler to check
    # that the gateway change is scoped to the UI mount.
    app.mount("/other", StaticFiles(directory=str(directory)), name="other")
    # The gateway serves the export through its UI route, last before the catch-all.
    app.router.routes.append(UiRoute(directory, _no_admin_guard))
    return app


def test_hashed_chunk_is_brotli_compressed_and_immutable(tmp_path):
    asset = tmp_path / "_next/static/chunks/app-0123456789abcdef.js"
    asset.parent.mkdir(parents=True)
    original = b"const payload = 'static js';\n" * 200
    asset.write_bytes(original)
    client = TestClient(_app_with_ui(tmp_path))

    with client.stream(
        "GET",
        "/_next/static/chunks/app-0123456789abcdef.js",
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
        "/_next/static/chunks/app-0123456789abcdef.js",
        headers={"Accept-Encoding": "br;q=0, gzip;q=0.5, identity;q=1"},
    ) as response:
        identity = b"".join(response.iter_raw())
    assert "content-encoding" not in response.headers
    assert identity == original

    with client.stream(
        "GET",
        "/_next/static/chunks/app-0123456789abcdef.js",
        headers={"Accept-Encoding": "br;q=0, gzip;q=1, identity;q=0"},
    ) as response:
        encoded_gzip = b"".join(response.iter_raw())
    assert response.headers["content-encoding"] == "gzip"
    assert gzip.decompress(encoded_gzip) == original

    refused = client.get(
        "/_next/static/chunks/app-0123456789abcdef.js",
        headers={"Accept-Encoding": "br;q=0, gzip;q=0, identity;q=0"},
    )
    assert refused.status_code == 406

    with client.stream(
        "GET",
        "/_next/static/chunks/app-0123456789abcdef.js",
        headers={"Accept-Encoding": "br ;q=1, gzip;q=0, identity;q=0"},
    ) as response:
        encoded_spaced_quality = b"".join(response.iter_raw())
    assert response.headers["content-encoding"] == "br"
    assert brotli.decompress(encoded_spaced_quality) == original

    spaced_identity_refused = client.get(
        "/_next/static/chunks/app-0123456789abcdef.js",
        headers={"Accept-Encoding": "br;q=0, gzip;q=0, identity ;q=0"},
    )
    assert spaced_identity_refused.status_code == 406

    duplicate_headers_refused = client.get(
        "/_next/static/chunks/app-0123456789abcdef.js",
        headers=[
            ("Accept-Encoding", "gzip;q=0"),
            ("Accept-Encoding", "identity;q=0, br;q=0"),
        ],
    )
    assert duplicate_headers_refused.status_code == 406

    image = tmp_path / "_next/static/media/image.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"image bytes")
    image_refused = client.get("/_next/static/media/image.png", headers={"Accept-Encoding": "*;q=0"})
    assert image_refused.status_code == 406
    range_refused = client.get("/_next/static/media/image.png", headers={
        "Accept-Encoding": "identity;q=0", "Range": "bytes=0-4",
    })
    assert range_refused.status_code == 406


def test_html_revalidates_and_head_and_range_keep_file_semantics(tmp_path, monkeypatch):
    page = tmp_path / "library/index.html"
    page.parent.mkdir()
    page.write_bytes(b"<html>page</html>")
    client = TestClient(_app_with_ui(tmp_path))

    first = client.get("/library/", headers={"Accept-Encoding": "br"})
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-cache"
    assert first.headers["etag"] and first.headers["last-modified"]
    assert first.headers["content-encoding"] == "br"

    repeated = client.get("/library/", headers={
        "Accept-Encoding": "br", "If-None-Match": first.headers["etag"],
    })
    assert repeated.status_code == 304
    assert repeated.headers["cache-control"] == "no-cache"

    repeated_headers = client.get("/library/", headers=[
        ("Accept-Encoding", "br"),
        ("If-None-Match", '"stale-tag"'),
        ("If-None-Match", first.headers["etag"]),
    ])
    assert repeated_headers.status_code == 304

    comma_in_opaque_tag = client.get("/library/", headers={
        "If-None-Match": '"other,*,tag"',
    })
    assert comma_in_opaque_tag.status_code == 200

    head = client.head("/library/", headers={"Accept-Encoding": "br"})
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == first.headers["content-length"]
    assert head.headers["content-encoding"] == "br"

    identity_head = client.head("/library/", headers={"Accept-Encoding": "identity"})
    assert identity_head.status_code == 200
    assert identity_head.content == b""
    assert identity_head.headers["content-length"] == str(page.stat().st_size)
    assert "content-encoding" not in identity_head.headers

    ranged = client.get("/library/", headers={
        "Accept-Encoding": "br", "Range": "bytes=0-4",
    })
    assert ranged.status_code == 206
    assert ranged.content == b"<html"
    assert "content-encoding" not in ranged.headers
    assert ranged.headers["content-range"].startswith("bytes 0-4/")

    refused_range = client.get("/library/", headers={
        "Accept-Encoding": "br, gzip, identity;q=0", "Range": "bytes=0-4",
    })
    assert refused_range.status_code == 406

    original_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Etc/GMT+7")
    time.tzset()
    try:
        modified = datetime(2026, 10, 10, 18, 0, 1, tzinfo=timezone.utc).timestamp()
        os.utime(page, (modified, modified))
        newer_than_request_date = client.get("/library/", headers={
            "If-Modified-Since": "Sat Oct 10 18:00:00 2026",
        })
        assert newer_than_request_date.status_code == 200
        at_request_date = client.get("/library/", headers={
            "If-Modified-Since": "Sat Oct 10 18:00:01 2026",
        })
        assert at_request_date.status_code == 304
    finally:
        if original_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", original_tz)
        time.tzset()


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
