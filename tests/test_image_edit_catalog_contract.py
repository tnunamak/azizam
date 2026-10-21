"""Replay the catalog examples through the edit handler and a fake graph round."""
import asyncio
import io
from email.parser import BytesParser
from email.policy import default

import pytest
from PIL import Image

import proxy
from gateway import workflows_catalog
from gateway.workflow_openapi import _execution
from test_image_prompt_binding import image_round  # noqa: F401
from test_image_route_receipts import _MultipartRequest, _MultipartUpload
from tests.support.fake_upstream import fake_upstreams, scenario_from_data  # noqa: F401


EDIT_RECORDS = [record for record in proxy.GATEWAY_WORKFLOW_REGISTRY.records()
                if record.intent == "image.edit"]
assert EDIT_RECORDS, "no image.edit manifests discovered"


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
    example = example_request(record)
    names = image_names(record)
    # Distinct names from the fake upload endpoint make a swapped binding visible.
    parts = [(part["input"], part.get("field", key)) for key, part in example["files"].items()]
    fake = fake_upstreams.asgi(scenario_from_data({
        "protocol": "comfyui", "name": "catalog-edit-uploads",
        "exchanges": [{
            "id": f"picture-{index}", "required": index < len(names),
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
