"""Form text is bounded on its own, and a deeply nested JSON body is the route's own refusal (review F2, items 2 and 3)."""

from __future__ import annotations

import asyncio
import json

import pytest
from starlette.responses import Response

from gateway import request_spool
from gateway.frontend_shapes import _wire
from gateway.request_spool import FileRequestSpool
from gateway.workflow_runtime_types import RunRefused

from tests.test_c2d_f_form_bounds import BLOCK, chunked_form
from tests.test_frontend_shapes import json_request, make_request, multipart_request

DEEP = b"[" * 10_000 + b"]" * 10_000
MIB = 1 << 20


def test_form_text_total_cannot_spend_unused_file_budget() -> None:
    """One file slot of 4 MiB leaves room in the body budget; two 600,000-byte text fields still hit the text bound."""
    limits = _wire.FormLimits(file_slots=1, max_file_bytes=4 * MIB)
    request, taken = chunked_form([("a", None, 600_000), ("b", None, 600_000)])
    result = asyncio.run(_wire.split_form(request, limits))
    assert isinstance(result, Response) and result.status_code == 413
    assert json.loads(result.body)["error"]["code"] == "payload_too_large"
    assert taken[0] < 600_000 * 2 + 2 * BLOCK


def test_one_text_field_inside_the_text_bound_is_read() -> None:
    limits = _wire.FormLimits(file_slots=1, max_file_bytes=4 * MIB)
    request, _ = chunked_form([("a", None, 600_000)])
    result = asyncio.run(_wire.split_form(request, limits))
    assert isinstance(result, tuple) and len(result[0]["a"]) == 600_000


def test_deep_json_returns_shape_error() -> None:
    result = asyncio.run(_wire.json_object(make_request(DEEP)))
    assert isinstance(result, Response) and result.status_code == 400


def _read(request, tmp_path):
    return asyncio.run(request_spool.read_run_request(
        request, "wf", spool=FileRequestSpool(tmp_path, max_bytes=MIB), library_ref=lambda _id: None,
        idempotency_key=None, wait_s=0))


def test_deep_generic_json_returns_refusal(tmp_path) -> None:
    result = _read(make_request(DEEP), tmp_path)
    assert isinstance(result, RunRefused) and result.http_status == 422


def test_deep_multipart_inputs_return_refusal(tmp_path) -> None:
    result = _read(multipart_request({"inputs": DEEP.decode()}, []), tmp_path)
    assert isinstance(result, RunRefused) and result.http_status == 422
