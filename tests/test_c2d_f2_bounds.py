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


def _chunked_urlencoded(fields: list[tuple[str, int]]):
    """A chunked application/x-www-form-urlencoded request with no Content-Length, and the body bytes it gave."""
    from starlette.requests import Request
    taken = [0]
    pieces: list[bytes] = []
    for index, (name, size) in enumerate(fields):
        pieces.append((("&" if index else "") + name + "=").encode())
        remaining = size
        while remaining > 0:
            pieces.append(b"x" * min(BLOCK, remaining))
            remaining -= BLOCK
    queue = iter(pieces)

    async def receive():
        piece = next(queue, None)
        if piece is None:
            return {"type": "http.disconnect"}
        taken[0] += len(piece)
        return {"type": "http.request", "body": piece, "more_body": piece is not pieces[-1]}

    scope = {"type": "http", "method": "POST", "path": "/", "query_string": b"", "headers": [
        (b"content-type", b"application/x-www-form-urlencoded")]}
    return Request(scope, receive), taken


@pytest.mark.parametrize("fields", [[("a", 600_000), ("b", 600_000)], [("a", 2 * MIB)],
                                    [(f"f{i}", 30_000) for i in range(40)]],
                         ids=["two-fields", "one-huge-field", "many-small-fields"])
def test_urlencoded_text_cannot_spend_unused_file_budget(fields) -> None:
    """A urlencoded body is all text: past the text bound it is refused while it is read, whatever file allowance the
    route has (the multipart parser is not used for it, so the bound is on the stream)."""
    limits = _wire.FormLimits(file_slots=1, max_file_bytes=4 * MIB)
    request, taken = _chunked_urlencoded(fields)
    result = asyncio.run(_wire.split_form(request, limits))
    assert isinstance(result, Response) and result.status_code == 413
    assert json.loads(result.body)["error"]["code"] == "payload_too_large"
    assert taken[0] <= _wire.FORM_PART_BYTES + 2 * BLOCK


def test_urlencoded_text_inside_the_text_bound_is_read() -> None:
    limits = _wire.FormLimits(file_slots=1, max_file_bytes=4 * MIB)
    request, _ = _chunked_urlencoded([("a", 300_000), ("b", 300_000)])
    result = asyncio.run(_wire.split_form(request, limits))
    assert isinstance(result, tuple) and len(result[0]["a"]) == 300_000 and len(result[0]["b"]) == 300_000


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


# ---- oracle gaps (items 4-6) ----------------------------------------------------------------------------------------

from tests.support import adapter_differential as ad  # noqa: E402
from tests.support import adapter_differential_deltas as listed  # noqa: E402
from tests.support.adapter_differential_deltas import Delta, DeltaError  # noqa: E402


def test_payload_http_date_prompt_is_compared_exactly() -> None:
    first = 'prompt: "Mon, 02 Nov 2026 10:00:00 GMT"'
    second = 'prompt: "Tue, 03 Nov 2026 11:11:11 GMT"'
    assert ad.Normalizer().text(first, payload=True) != ad.Normalizer().text(second, payload=True)
    # outside a payload the date is a clock reading of this run, still masked
    assert ad.Normalizer().text(first) == ad.Normalizer().text(second)


def _body_golden(text: str) -> dict:
    return {"caller": [{"response": {"body": {"text": text}}}]}


def test_delta_cannot_replace_serialized_json_body_subtree() -> None:
    golden = _body_golden('{"status": "queued"}')
    whole = Delta("envelope_v2", "$.caller[0].response.body.text", '{"status": "queued"}',
                  '{"status": "running"}', "the envelope is version 2")
    with pytest.raises(DeltaError, match="JSON"):
        listed.apply_deltas(golden, [whole])
    leaf = Delta("envelope_v2", "$.caller[0].response.body.text#$.status", "queued", "running", "the envelope is version 2")
    assert json.loads(listed.apply_deltas(golden, [leaf])["caller"][0]["response"]["body"]["text"]) == {"status": "running"}
    plain = Delta("envelope_v2", "$.caller[0].response.body.text", "hello", "world", "the envelope is version 2")
    assert listed.apply_deltas(_body_golden("hello"), [plain])["caller"][0]["response"]["body"]["text"] == "world"


@pytest.mark.parametrize("path, golden", [
    ("$.flag", {"flag": False}),
    ("$.body.text#$.flag", {"body": {"text": '{"flag": false}'}}),
])
def test_delta_old_value_requires_the_exact_json_type(path, golden) -> None:
    with pytest.raises(DeltaError, match="unmatched"):
        listed.apply_deltas(golden, [Delta("envelope_v2", path, 0, 1, "the envelope is version 2")])
    out = listed.apply_deltas(golden, [Delta("envelope_v2", path, False, True, "the envelope is version 2")])
    assert out is not None
