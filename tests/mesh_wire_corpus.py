"""A fixed corpus of requests to POST /v1/meshes/generate, and a runner that records what crosses the wire.

``run(harness)`` sends each request to the route built by a ``Harness`` of ``tests/test_mesh_route.py`` and records the
answer's status, headers and body bytes, and the request the engine received (its multipart boundary replaced by a fixed
word, because the sender picks a random one). ``tests/fixtures/mesh_wire_baseline.json`` holds the record taken before the
mesh validator was deleted, from the route that read the old validator's manifest. A test replays the corpus against the
route that reads the registry's record and compares: the wire must be byte-identical (CODE_TO_DATA_S3, S3.9).

The module holds no expectation of its own: a corpus is requests, and the baseline is what the route answered.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from fastapi.testclient import TestClient

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128
JPEG = b"\xff\xd8\xff\xe0" + b"\x01" * 96
WEBP = b"RIFF\x40\x00\x00\x00WEBPVP8 " + b"\x02" * 48
GIF = b"GIF89a" + b"\x00" * 32
BOUNDARY = "s39-fixed-boundary"
MESH_PATH = "/v1/meshes/generate"
MIB = 1 << 20

# (name, value, filename, content type); filename None is a text field
Part = tuple[str, bytes | str, str | None, str | None]


def text(name: str, value: str) -> Part:
    return (name, value, None, None)


def pic(data: bytes, *, name: str = "image", filename: str = "photo.png", media: str = "image/png") -> Part:
    return (name, data, filename, media)


SUCCESS: dict[str, list[Part]] = {
    "png-seed": [text("seed", "42"), pic(PNG)],
    "jpeg-claimed-png-nothing-else": [pic(JPEG, filename="Holiday Photo.JPG", media="image/png")],
    "webp-all-options": [text("seed", "0"), text("resolution", "1536"), text("bg_removal", "birefnet"), pic(WEBP, filename="a.webp", media="image/webp")],
    "seed-max-and-padding": [text("seed", " 4294967295 "), text("resolution", "512"), pic(PNG, filename="../x.png")],
    "image-first-then-fields": [pic(PNG), text("bg_removal", "threshold"), text("seed", "7")],
}

ERRORS: dict[str, list[Part]] = {
    "unknown-field": [text("surprise", "x"), pic(PNG)],
    "unknown-field-before-missing-image": [text("surprise", "x")],
    "missing-image": [text("seed", "1")],
    "unmapped-object": [text("extensions", "{}"), pic(PNG)],
    "unmapped-object-bad-json": [text("extensions", "{nope"), pic(PNG)],
    "seed-negative": [text("seed", "-1"), pic(PNG)],
    "seed-over-maximum": [text("seed", "4294967296"), pic(PNG)],
    "seed-at-integer-ceiling-minus-one": [text("seed", "9223372036854775807"), pic(PNG)],
    "seed-at-integer-ceiling": [text("seed", "9223372036854775808"), pic(PNG)],
    "seed-negative-at-ceiling": [text("seed", "-9223372036854775808"), pic(PNG)],
    "seed-float-text": [text("seed", "1.5"), pic(PNG)],
    "seed-bool-text": [text("seed", "true"), pic(PNG)],
    "seed-plus-sign": [text("seed", "+1"), pic(PNG)],
    "seed-exponent": [text("seed", "1e3"), pic(PNG)],
    "seed-empty": [text("seed", ""), pic(PNG)],
    "resolution-not-allowed": [text("resolution", "768"), pic(PNG)],
    "bg-removal-not-allowed": [text("bg_removal", "auto"), pic(PNG)],
    "first-bad-field-wins": [text("seed", "-1"), text("resolution", "768"), pic(PNG)],
    "later-field-first-in-form-order": [text("resolution", "768"), text("seed", "-1"), pic(PNG)],
    "bad-scalar-and-missing-image": [text("seed", "-1")],
    "bad-scalar-and-unmapped": [text("extensions", "{}"), text("seed", "-1"), pic(PNG)],
    "unsupported-image": [pic(GIF, filename="x.gif", media="image/gif")],
    "unsupported-image-claimed-png": [pic(GIF, filename="x.png", media="image/png")],
    "empty-image": [pic(b"")],
    "image-over-the-limit": [pic(PNG + b"\x00" * (25 * MIB))],
    "image-sent-twice": [pic(PNG), pic(PNG)],
    "image-sent-as-text": [text("image", "not a file")],
    "second-file-part": [pic(PNG), pic(PNG, name="seed", filename="s.png")],
    "unexpected-file-part": [pic(PNG), pic(b"x", name="surprise", filename="s.bin", media="application/octet-stream")],
    "scalar-sent-twice": [text("seed", "1"), text("seed", "2"), pic(PNG)],
}

# Bodies that are not a well-formed multipart body at all, as (content type, raw body).
RAW: dict[str, tuple[str, bytes]] = {
    "not-multipart-body": (f"multipart/form-data; boundary={BOUNDARY}", b"not a multipart body"),
    "urlencoded-form": ("application/x-www-form-urlencoded", b"seed=1"),
}


def body_of(parts: list[Part]) -> bytes:
    out = bytearray()
    for name, value, filename, media in parts:
        out += f"--{BOUNDARY}\r\n".encode()
        disposition = f'Content-Disposition: form-data; name="{name}"' + (f'; filename="{filename}"' if filename is not None else "")
        out += disposition.encode() + b"\r\n"
        if media is not None:
            out += f"Content-Type: {media}\r\n".encode()
        out += b"\r\n" + (value if isinstance(value, bytes) else value.encode()) + b"\r\n"
    out += f"--{BOUNDARY}--\r\n".encode()
    return bytes(out)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _normalised_request(request: Any) -> dict[str, Any]:
    """The request the engine received, its random boundary replaced by a fixed word."""
    content_type = request.headers["content-type"]
    boundary = content_type.split("boundary=", 1)[1]
    return {
        "method": request.method,
        "url": str(request.url),
        "content_type": content_type.replace(boundary, "BOUNDARY"),
        "body": _b64(request.content.replace(boundary.encode(), b"BOUNDARY")),
    }


def _record(harness: Any, response: Any) -> dict[str, Any]:
    return {
        "status": response.status_code,
        "content_type": response.headers.get("content-type"),
        "request_id": response.headers.get("x-request-id"),
        "retry_after": response.headers.get("retry-after"),
        "body": _b64(response.content),
        "receipts": [{"outcome": list(item["outcome"]) if item["outcome"] else None, "exc": item["exc"]} for item in harness.receipts],
        "engine_requests": [_normalised_request(request) for request in harness.downstream],
        "claims_left": harness.leases.active_claims(),
    }


def run(make_harness: Any) -> dict[str, dict[str, Any]]:
    """Every corpus request against a fresh harness. ``make_harness()`` returns a ``Harness`` of tests/test_mesh_route.py."""
    record: dict[str, dict[str, Any]] = {}
    sent: dict[str, tuple[str, bytes]] = {}
    for table in (SUCCESS, ERRORS):
        for name, parts in table.items():
            sent[name] = (f"multipart/form-data; boundary={BOUNDARY}", body_of(parts))
    sent.update(RAW)
    for name, (content_type, body) in sent.items():
        harness = make_harness()
        client = TestClient(harness.app())
        response = client.post(MESH_PATH, content=body, headers={"content-type": content_type})
        record[name] = _record(harness, response)
    return record


def dump(record: dict[str, dict[str, Any]]) -> str:
    return json.dumps(record, indent=1, sort_keys=True) + "\n"
