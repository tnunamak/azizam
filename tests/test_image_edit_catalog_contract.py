"""Replay the catalog examples through the edit handler and a fake graph round."""
import asyncio
import io
import json
import base64
from pathlib import Path
from email.parser import BytesParser
from email.policy import default

import pytest
from PIL import Image

import proxy
from gateway import workflows_catalog
from gateway.workflow_openapi import _execution
from gateway.workflow_call import build_request
from test_image_prompt_binding import image_round  # noqa: F401
from test_image_route_receipts import _MultipartRequest, _MultipartUpload
from tests.support.fake_upstream import fake_upstreams, scenario_from_data  # noqa: F401


EDIT_RECORDS = [record for record in proxy.GATEWAY_WORKFLOW_REGISTRY.records()
                if record.intent == "image.edit"
                and _execution(record, None)["sync"]["path"] == "/v1/images/edits"]
assert EDIT_RECORDS, "no image.edit manifests discovered"
SELECT_EDIT_WORKFLOW = proxy._select_edit_workflow


def example_request(record):
    sync = _execution(record, record.manifest.get("typical_seconds"))["sync"]
    assert sync["path"] == "/v1/images/edits", record.id
    inputs = workflows_catalog._input_rows(record, {"sync": sync, "recommended": "sync"})
    return workflows_catalog._example_request(record, inputs, sync, "http://gateway.test")


def image_names(record):
    bound = {name for binding in record.manifest["bindings"].values()
             for name in binding.get("route_bound_inputs", [])}
    return ["source_image", *(name for name, spec in record.manifest["inputs"].items()
                               if name != "source_image" and name in bound and spec["type"] == "asset")]


@pytest.mark.parametrize("record", EDIT_RECORDS, ids=lambda record: record.id)
def test_catalog_example_request_runs_on_the_edit_route(record, image_round, fake_upstreams, monkeypatch):
    monkeypatch.setattr(proxy, "_select_edit_workflow", SELECT_EDIT_WORKFLOW)
    example = example_request(record)
    names = image_names(record)
    # Distinct names from the fake upload endpoint make a swapped binding visible.
    parts = [(part["input"], part.get("field", key)) for key, part in example["files"].items()]
    fake = fake_upstreams.asgi(scenario_from_data({
        "protocol": "comfyui", "name": "catalog-edit-uploads",
        "provenance": {"source": "hand-written", "reference": "ComfyUI POST /upload/image"},
        "exchanges": [{
            "id": f"picture-{index}", "required": False,
            "match": {"method": "POST", "path": "/upload/image"},
            "response": {"json": {"name": f"uploaded-{index}.png", "subfolder": "", "type": "input"}},
        } for index in range(len(parts))],
    }))
    monkeypatch.setattr(proxy, "COMFY_HOST", fake.base_url)
    items = list(example["form"].items())
    colors = {}
    for index, (name, field) in enumerate(parts):
        colors[name] = (index * 20 + 20, 80, 160, 255)
        content = io.BytesIO()
        Image.new("RGBA", (1280, 960), colors[name]).save(content, format="PNG")
        items.append((field, _MultipartUpload(f"{name}.png", content.getvalue())))
    response = asyncio.run(proxy.proxy_graph_image_edit_request(_MultipartRequest(items), proxy.NO_AUTH_CONTEXT))
    assert response.status_code == 200, response.body.decode()
    assert len(image_round) == 1
    graph = image_round[0]
    binding = next(binding for binding in record.manifest["bindings"].values() if binding.get("workflow"))
    # The manifest itself identifies each input's node, independent of catalog order.
    for index, name in enumerate(names):
        for target in binding["map"][name]:
            assert graph[str(target["node"])]["inputs"][target["input"]] == f"uploaded-{index}.png", name
        # Read the real uploaded multipart bytes to also prove picture identity.
        upload = fake.requests[index]
        message = BytesParser(policy=default).parsebytes(
            f"Content-Type: {upload.headers['content-type']}\r\n\r\n".encode() + upload.body)
        picture = next(part for part in message.iter_parts() if part.get_filename())
        with Image.open(io.BytesIO(picture.get_payload(decode=True))) as image:
            assert image.convert("RGBA").getpixel((0, 0)) == colors[name], name


def test_multi_picture_examples_repeat_one_part_name():
    checked = 0
    for record in EDIT_RECORDS:
        names = image_names(record)
        if len(names) < 2:
            continue
        checked += 1
        parts = example_request(record)["files"]
        actual = [(part["input"], part.get("field", key)) for key, part in parts.items()
                  if part["input"] in names]
        assert actual == [(name, "image[]") for name in names], record.id
    assert checked, "no multi-picture edit workflow discovered"


@pytest.mark.parametrize("record", EDIT_RECORDS, ids=lambda record: record.id)
def test_adapter_request_keeps_repeated_parts_in_manifest_order(record):
    names = image_names(record)
    payloads = {}
    for index, name in enumerate(names):
        content = io.BytesIO()
        Image.new("RGB", (2, 1), (index * 30, 80, 160)).save(content, format="PNG")
        payloads[name] = content.getvalue()
    inputs = {name: "data:image/png;base64," + base64.b64encode(payloads[name]).decode()
              for name in reversed(names)}
    sync = _execution(record, None)["sync"]
    built = asyncio.run(build_request(lambda *_args: None, None, None, {"intent": record.intent}, sync, inputs, wrap_intent=None))
    assert [(field, content) for field, (_, content, _) in built.files] == [
        ("image[]", payloads[name]) for name in names]


@pytest.mark.parametrize("filename,identifier", [
    ("workflows-image-edit.json", "image.edit.flux2-klein-4b"),
    ("workflows-image-edit-multiref.json", "image.edit.qwen-image-2.1-edit-multiref"),
])
def test_try_fixture_matches_the_real_catalog_contract(filename, identifier):
    record = next(record for record in EDIT_RECORDS if record.id == identifier)
    fixture = json.loads((Path(__file__).resolve().parents[1] / "web/tests/fixtures" / filename).read_text())["workflows"][0]
    sync = _execution(record, record.manifest.get("typical_seconds"))["sync"]
    assert fixture["openapi"]["execution"]["sync"] == sync
    assert fixture["inputs"] == workflows_catalog._input_rows(record, {"sync": sync, "recommended": "sync"})
    assert fixture["example_request"] == example_request(record) | {
        "curl": fixture["example_request"]["curl"],
        "python": fixture["example_request"]["python"],
        "javascript": fixture["example_request"]["javascript"],
    }
