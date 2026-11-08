"""A multipart body is bounded while it is read, before the form parser spools it (review c2d-D4-rows-review-1108,
finding 1). The bound never trusts Content-Length: the tests send a chunked body that says nothing about its size."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

from gateway.frontend_shapes import _wire
from gateway.request_spool import FileRequestSpool

from tests.test_frontend_shapes import (  # noqa: F401 - fixtures of the shape tests
    ROWS, no_deployment_default, shape_of, snapshot,
)

BOUNDARY = "bnd"
BLOCK = 64 * 1024


def _head(name: str, filename: str | None = None) -> bytes:
    disposition = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
    ctype = "Content-Type: image/png\r\n" if filename else ""
    return f"--{BOUNDARY}\r\nContent-Disposition: {disposition}\r\n{ctype}\r\n".encode()


def chunked_form(parts: list[tuple[str, str | None, int]]) -> tuple[Request, list[int]]:
    """A chunked multipart request with no Content-Length. ``parts`` is (name, filename or None, size). The second
    value counts the body bytes the reader took."""
    taken = [0]
    pieces: list[bytes] = []
    for name, filename, size in parts:
        pieces.append(_head(name, filename))
        remaining = size
        while remaining > 0:
            pieces.append(b"x" * min(BLOCK, remaining))
            remaining -= BLOCK
        pieces.append(b"\r\n")
    pieces.append(f"--{BOUNDARY}--\r\n".encode())
    queue = iter(pieces)

    async def receive() -> dict[str, Any]:
        piece = next(queue, None)
        if piece is None:
            return {"type": "http.disconnect"}
        taken[0] += len(piece)
        return {"type": "http.request", "body": piece, "more_body": True}

    scope = {"type": "http", "method": "POST", "path": "/", "query_string": b"", "headers": [
        (b"content-type", f"multipart/form-data; boundary={BOUNDARY}".encode())]}
    return Request(scope, receive), taken


def _split(request: Request, limits: _wire.FormLimits):
    return asyncio.run(_wire.split_form(request, limits))


LIMITS = _wire.FormLimits(max_files=1, max_file_bytes=BLOCK * 4, max_fields=3)


def test_an_oversized_file_is_refused_with_413_after_reading_only_up_to_the_budget() -> None:
    request, taken = chunked_form([("image", "a.png", BLOCK * 400)])
    result = _split(request, LIMITS)
    assert isinstance(result, Response) and result.status_code == 413
    assert json.loads(result.body)["error"]["code"] == "payload_too_large"
    assert taken[0] <= LIMITS.body_budget + 2 * BLOCK


def test_many_files_are_refused_without_reading_them_all() -> None:
    request, taken = chunked_form([(f"f{i}", f"{i}.png", 100) for i in range(500)])
    result = _split(request, LIMITS)
    assert isinstance(result, Response) and result.status_code in (400, 413)
    total = sum(len(_head(f"f{i}", f"{i}.png")) + 100 + 2 for i in range(500))
    assert taken[0] < total


def test_many_fields_are_refused_without_reading_them_all() -> None:
    request, taken = chunked_form([(f"k{i}", None, 10) for i in range(500)])
    result = _split(request, LIMITS)
    assert isinstance(result, Response) and result.status_code in (400, 413)
    total = sum(len(_head(f"k{i}")) + 10 + 2 for i in range(500))
    assert taken[0] < total


def test_a_form_inside_the_limits_is_read() -> None:
    request, _ = chunked_form([("prompt", None, 20), ("image", "a.png", BLOCK * 2)])
    fields, uploads = _split(request, LIMITS)
    assert list(fields) == ["prompt"] and [name for name, _ in uploads] == ["image"]


def test_the_shape_takes_its_file_limit_from_the_spool(tmp_path) -> None:
    spool = FileRequestSpool(tmp_path, max_bytes=BLOCK * 4)
    request, taken = chunked_form([("prompt", None, 5), ("image", "a.png", BLOCK * 400)])
    snap = snapshot("flux2-klein", "qwen-image-2.1-edit")
    shape = shape_of("images-edits")
    response = asyncio.run(shape.to_run_request(request, ROWS["images-edits"], snap, spool=spool))
    assert isinstance(response, Response) and response.status_code == 413
    assert taken[0] < BLOCK * 100
