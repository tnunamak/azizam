"""Every picture the catalog promises must reach the edit route.

The Try page, the MCP ``run`` tool and the printed curl/Python/JavaScript
examples all build one multipart request from the workflow catalog's field map.
This test takes that map's own example request for each ``image.edit`` workflow
and replays it through the real ``/v1/images/edits`` handler with a fake
ComfyUI round. A picture the catalog sends under a part name the route never
reads fails here instead of in someone's hands.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import Response

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import workflows_catalog  # noqa: E402
from gateway.workflow_openapi import _execution  # noqa: E402
from tests.support.access import identity  # noqa: E402
from test_image_route_receipts import _MultipartRequest, _MultipartUpload  # noqa: E402

EDIT_ROUTE = "/v1/images/edits"
OWNER_CONTEXT = proxy.AuthContext(identity=identity())

# One distinct colour per picture position, so a swapped binding is visible.
_PICTURE_COLOURS = [
    (255, 0, 0, 255),
    (0, 255, 0, 255),
    (0, 0, 255, 255),
]


def _png_bytes(color: tuple[int, int, int, int]) -> bytes:
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGBA", (2, 1), color).save(output, format="PNG")
    return output.getvalue()


def _edit_records() -> list[Any]:
    records = [
        record
        for record in proxy.GATEWAY_WORKFLOW_REGISTRY.records()
        if record.intent == "image.edit"
    ]
    assert records, "the manifest registry has no image.edit workflows"
    return records


def _sync_example(record: Any) -> dict[str, Any]:
    """The catalog's example request for this workflow, as a caller receives it."""
    sync = _execution(record, record.manifest.get("typical_seconds"))["sync"]
    assert sync, f"{record.id} offers no sync execution"
    routes = {"sync": sync, "recommended": "sync"}
    inputs = workflows_catalog._input_rows(record, routes)
    return workflows_catalog._example_request(record, inputs, sync, "http://gateway.test")


def _picture_inputs(example: dict[str, Any], sync: dict[str, Any]) -> list[tuple[str, str]]:
    """``(input name, multipart field)`` for each picture the example sends, in order."""
    pictures: list[tuple[str, str]] = []
    for part in example["files"].values():
        name = str(part["input"])
        placement = sync["fields"][name]
        field = str(placement["field"]) if placement.get("in") == "file" else name
        pictures.append((name, field))
    return pictures


class _UploadReply:
    status = 200

    def __init__(self, name: str) -> None:
        self._name = name

    def json(self) -> dict[str, str]:
        return {"name": self._name}


class _FakeGraphClient:
    """A ComfyUI connection that accepts uploads and records them in order."""

    uploads: list[dict[str, Any]] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeGraphClient":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def post(self, *_args: Any, **kwargs: Any) -> _UploadReply:
        type(self).uploads.append(kwargs.get("files") or {})
        return _UploadReply(f"uploaded-{len(type(self).uploads)}.png")


def _play(record: Any, example: dict[str, Any]) -> tuple[Any, Any, dict[str, Any], list[str]]:
    """Send the example through the handler; return response, receipt, graph, pictures."""
    sync = _execution(record, record.manifest.get("typical_seconds"))["sync"]
    pictures = _picture_inputs(example, sync)

    items: list[tuple[str, Any]] = list(example["form"].items())
    picture_bytes: dict[str, bytes] = {}
    for index, (name, field) in enumerate(pictures):
        content = _png_bytes(_PICTURE_COLOURS[index % len(_PICTURE_COLOURS)])
        picture_bytes[name] = content
        items.append((field, _MultipartUpload(f"{name}.png", content)))
    if (record.manifest.get("inputs") or {}).get("mask"):
        # The route validates the mask against the source, so the example's
        # made-up path is replaced with a real 2x1 image of the same size.
        items.append(("mask", _MultipartUpload("mask.png", _png_bytes((255, 255, 255, 255)))))

    captured: dict[str, Any] = {}

    async def fake_generate(**kwargs: Any) -> Response:
        captured.update(kwargs)
        return Response(
            content=json.dumps({"created": 1, "data": [{"b64_json": ""}]}),
            status_code=200,
            media_type="application/json",
        )

    _FakeGraphClient.uploads = []
    receipt = proxy.start_image_route_receipt(EDIT_ROUTE, OWNER_CONTEXT)
    with patch.object(proxy, "COMFY_ENABLED", True), patch.object(
        proxy.httpx, "AsyncClient", _FakeGraphClient
    ), patch.object(
        proxy, "configured_downstream_headers", lambda *_args, **_kwargs: {}
    ), patch.object(
        proxy,
        "resolve_image_background_decision",
        AsyncMock(return_value=proxy.ImageBackgroundRemovalDecision(False)),
    ), patch.object(
        proxy, "_generate_images_with_adaptive_batching", fake_generate
    ):
        response = _run(
            proxy.proxy_graph_image_edit_request(
                _MultipartRequest(items),  # type: ignore[arg-type]
                OWNER_CONTEXT,
                receipt=receipt,
            )
        )
    return response, receipt, captured, [name for name, _ in pictures]


def _run(coroutine: Any) -> Any:
    import asyncio

    return asyncio.run(coroutine)


EDIT_EXAMPLES = [
    pytest.param(record, id=record.id)
    for record in _edit_records()
    if _execution(record, record.manifest.get("typical_seconds"))["sync"].get("path") == EDIT_ROUTE
]


@pytest.mark.parametrize("record", EDIT_EXAMPLES)
def test_catalog_example_request_runs_on_the_edit_route(record: Any) -> None:
    example = _sync_example(record)
    response, receipt, captured, pictures = _play(record, example)

    assert response.status_code == 200, response.body.decode()
    assert receipt.resolved_model, "the route did not resolve a workflow"
    graph = proxy.load_graph_workflow(str(receipt.resolved_model))
    slots = proxy._image_edit_node_slots(graph.get("_meta") or {})
    assert len(slots) >= len(pictures), (
        f"{record.id}: the catalog sends {len(pictures)} picture(s) but the "
        f"resolved workflow {receipt.resolved_model} has {len(slots)} slot(s)"
    )
    # Pictures bind to the workflow's slots by submission order: the catalog's
    # order is the manifest's, source picture first.
    for index, slot in enumerate(slots[: len(pictures)]):
        assert captured["base_workflow"][slot]["inputs"]["image"] == f"uploaded-{index + 1}.png", (
            f"{record.id}: picture {index} ({pictures[index]}) did not bind to node {slot}"
        )


def test_multi_picture_examples_repeat_one_part_name() -> None:
    """Two pictures travel as two parts of one field, never a field of their own."""
    checked = 0
    for record in _edit_records():
        sync = _execution(record, record.manifest.get("typical_seconds"))["sync"]
        if sync.get("path") != EDIT_ROUTE:
            continue
        example = _sync_example(record)
        pictures = _picture_inputs(example, sync)
        if len(pictures) < 2:
            continue
        checked += 1
        assert [field for _, field in pictures] == ["image[]"] * len(pictures), record.id
        assert pictures[0][0] == "source_image", record.id
    assert checked, "no image.edit workflow sends more than one picture"
