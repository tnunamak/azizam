"""Adapter pick-lists on the two request paths: the workflow-run route and the image route.

The engine is faked at the two reads the choices need (its file list and a file's header) and at its HTTP for the run
route. A caller picks files by name; only an installed file whose header names the workflow's architecture is offered;
anything else is a 422 that lists what is offered.
"""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import proxy
from gateway import adapter_choices, media_jobs
from gateway.receipts import RouteReceipt
from tests.media_test_bytes import png_bytes
from tests.support.access import identity
from tests.test_workflow_run_contracts import (  # noqa: F401  (world is a fixture)
    FakeGraphEngine, call, manifest_of, serve_engine, template_of, world)

WORKFLOW = "image.generate.krea2-turbo"
PLAIN_WORKFLOW = "image.generate.moss-image-fast"
EXT = "com.azizam.workflow"
FILES = ["example_style_v1.safetensors", "example_other_arch_v2.safetensors", "example_no_header.safetensors"]
HEADERS = {
    "example_style_v1.safetensors": {"ss_base_model_version": "krea2"},
    "example_other_arch_v2.safetensors": {"ss_base_model_version": "sdxl_base_v0-9"},
    "example_no_header.safetensors": None,
}


@pytest.fixture(autouse=True)
def engine_files(monkeypatch):
    """The engine lists FILES and answers each header from HEADERS. ``state`` lets a test break the listing."""
    adapter_choices.reset()
    state = SimpleNamespace(listing_fails=False, listed=0, headers_read=0)

    def list_files(origin, node_type, input_name):
        state.listed += 1
        if state.listing_fails:
            raise OSError("engine down")
        return list(FILES)

    def read_header(origin, folder, name):
        state.headers_read += 1
        return HEADERS[name]

    monkeypatch.setattr(adapter_choices, "default_list_files", list_files)
    monkeypatch.setattr(adapter_choices, "default_read_header", read_header)
    yield state
    adapter_choices.reset()


def run(workflow_id, inputs):
    return call("post", f"/gateway/v1/workflows/{workflow_id}/run", json={"inputs": inputs}, headers={"Prefer": "wait=20"})


def serve(world, workflow_id=WORKFLOW):
    return serve_engine(world, FakeGraphEngine(manifest_of(workflow_id), png_bytes((9, 9, 9)), "image/png", "out.png"))


# ------------------------------------------------------------------------------------------------ the workflow-run route


def test_a_run_with_an_offered_adapter_sends_the_engine_a_graph_with_that_adapter_in_the_chain(world):
    engine = serve(world)
    response = run(WORKFLOW, {"prompt": "a red fox", "loras": [{"name": "example_style_v1.safetensors", "weight": 0.5}]})
    assert response.status_code == 200 and response.json()["status"] == "succeeded", response.text
    graph = engine.prompts[0]["prompt"]
    added = [node for node in (graph or {}).values() if node["class_type"] == "LoraLoaderModelOnly"]
    assert [(n["inputs"]["lora_name"], n["inputs"]["strength_model"]) for n in added] == [("example_style_v1.safetensors", 0.5)]


def test_a_file_for_another_architecture_is_refused_and_the_answer_lists_the_offered_files(world):
    engine = serve(world)
    response = run(WORKFLOW, {"prompt": "x", "loras": [{"name": "example_other_arch_v2.safetensors"}]})
    assert response.status_code == 422, response.text
    problems = response.json()["error"]["problems"]
    assert problems[0]["path"] == "/inputs/loras/0/name"
    assert "example_style_v1.safetensors" in problems[0]["message"]
    assert "example_other_arch_v2.safetensors" not in problems[0]["message"]
    assert "example_no_header.safetensors" not in problems[0]["message"]
    assert engine.calls == []


def test_an_unknown_name_and_a_weight_out_of_bounds_are_each_refused(world):
    serve(world)
    unknown = run(WORKFLOW, {"prompt": "x", "loras": [{"name": "nope.safetensors"}]})
    heavy = run(WORKFLOW, {"prompt": "x", "loras": [{"name": "example_style_v1.safetensors", "weight": 4.5}]})
    assert unknown.status_code == 422 and heavy.status_code == 422
    assert heavy.json()["error"]["problems"][0]["path"] == "/inputs/loras/0/weight"


def test_an_engine_that_cannot_list_its_files_is_a_retryable_refusal_not_a_bad_name(world, engine_files):
    serve(world)
    engine_files.listing_fails = True
    response = run(WORKFLOW, {"prompt": "x", "loras": [{"name": "example_style_v1.safetensors"}]})
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "adapter_choices_unavailable"


def test_a_run_that_names_no_adapter_reads_nothing_from_the_engine_about_files(world, engine_files):
    serve(world)
    response = run(WORKFLOW, {"prompt": "x"})
    assert response.status_code == 200, response.text
    assert engine_files.listed == 0 and engine_files.headers_read == 0


def test_a_workflow_that_takes_no_adapters_keeps_the_unknown_input_refusal(world):
    serve(world, PLAIN_WORKFLOW)
    response = run(PLAIN_WORKFLOW, {"prompt": "x", "loras": [{"name": "example_style_v1.safetensors"}]})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["problems"][0]["code"] == "input_unknown"


def test_the_job_keeps_the_adapters_it_ran_with_by_file_name_and_weight(world):
    serve(world)
    response = run(WORKFLOW, {"prompt": "x", "loras": [{"name": "example_style_v1.safetensors", "weight": 0.5}]})
    job = media_jobs.get(response.json()["job_id"], path=world.media)
    assert job["metadata"]["adapters"] == {"loras": [{"name": "example_style_v1.safetensors", "weight": 0.5}]}


# --------------------------------------------------------------------------------------------------------- the image route

SERVICE = SimpleNamespace(service_id="comfyui", host="http://engine.invalid:7890", enabled=True, timeout=30, record={})


def image_call(extension_block, manifest=None):
    manifest = manifest or manifest_of(WORKFLOW)
    template = template_of(manifest)
    receipt = RouteReceipt(protocol="openai", intent="image.generate", path="/v1/images/generations", request_id="req-adapters")
    with patch.object(proxy, "_graph_service", lambda _capability: SERVICE):
        graph, response = asyncio.run(proxy._apply_workflow_extensions_to_image_request(
            request_body={"prompt": "x", "extensions": {EXT: extension_block}},
            manifest=manifest, backend_request=copy.deepcopy(template),
            standard_fields=proxy.STANDARD_IMAGE_GENERATE_FIELDS, receipt=receipt))
    return graph, response, receipt


def test_the_image_route_binds_an_offered_adapter_and_the_receipt_names_it():
    graph, response, receipt = image_call({"loras": [{"name": "example_style_v1.safetensors", "weight": -1.5}]})
    assert response is None
    added = [node for node in (graph or {}).values() if isinstance(node, dict) and node.get("class_type") == "LoraLoaderModelOnly"]
    assert [(n["inputs"]["lora_name"], n["inputs"]["strength_model"]) for n in added] == [("example_style_v1.safetensors", -1.5)]
    assert receipt.metadata["adapters"] == {"loras": [{"name": "example_style_v1.safetensors", "weight": -1.5}]}


def test_the_image_route_refuses_a_file_it_does_not_offer_with_a_422_that_lists_the_offered_ones():
    graph, response, receipt = image_call({"loras": [{"name": "example_other_arch_v2.safetensors"}]})
    assert graph is None and response is not None and response.status_code == 422
    message = json.loads(response.body)["error"]["message"]
    assert "example_style_v1.safetensors" in message and "example_other_arch_v2.safetensors" not in message.split("one of")[-1]


def test_the_image_route_says_so_when_the_engine_cannot_list_its_files(engine_files):
    engine_files.listing_fails = True
    graph, response, _ = image_call({"loras": [{"name": "example_style_v1.safetensors"}]})
    assert graph is None and response is not None and response.status_code == 503
    assert json.loads(response.body)["error"]["code"] == "adapter_choices_unavailable"


def test_the_image_route_reads_no_file_list_for_a_request_with_no_adapter_fields(engine_files):
    graph, response, receipt = image_call({})
    assert response is None and engine_files.listed == 0
    assert "adapters" not in receipt.metadata


def test_the_image_route_keeps_the_unknown_input_refusal_for_a_workflow_without_adapters():
    graph, response, _ = image_call({"loras": [{"name": "example_style_v1.safetensors"}]}, manifest=manifest_of(PLAIN_WORKFLOW))
    assert graph is None and response is not None and response.status_code == 400
    assert "unknown input" in json.loads(response.body)["error"]["message"]


# ------------------------------------------------------------------------------------------------------------- discovery

import httpx  # noqa: E402

from tests.test_event_loop_guard import gateway  # noqa: E402,F401  (fixture)


def member_workflows(gateway, monkeypatch):
    monkeypatch.setattr(proxy, "_graph_service", lambda _capability: SERVICE)

    async def get():
        transport = httpx.ASGITransport(app=proxy.app, client=("127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=20, cookies=gateway["cookies"]) as client:
            return await client.get("/me/api/workflows")

    answer = asyncio.run(get())
    assert answer.status_code == 200, answer.text
    return {row["id"]: row for row in answer.json()["workflows"]}


def adapter_row(workflows, workflow_id):
    return next((row for row in workflows[workflow_id]["inputs"] if row["name"] == "loras"), None)


def test_the_member_listing_shows_the_adapter_files_on_offer_now(gateway, monkeypatch):
    rows = member_workflows(gateway, monkeypatch)
    assert adapter_row(rows, WORKFLOW)["adapter_files"] == ["example_style_v1.safetensors"]


def test_a_listing_when_the_engine_cannot_list_shows_no_files_and_is_still_served(gateway, monkeypatch, engine_files):
    engine_files.listing_fails = True
    rows = member_workflows(gateway, monkeypatch)
    assert "adapter_files" not in adapter_row(rows, WORKFLOW)


def test_a_workflow_that_takes_no_adapters_has_no_adapter_row(gateway, monkeypatch):
    rows = member_workflows(gateway, monkeypatch)
    assert adapter_row(rows, PLAIN_WORKFLOW) is None



# -------------------------------------------------------------------------------------- the shipped adapter workflows


@pytest.mark.parametrize("workflow_id", ["image.generate.krea2-turbo", "image.generate.krea2-turbo-snofs", "image.edit.qwen-image-2.1-edit"])
def test_each_shipped_adapter_workflow_binds_a_chain_into_its_own_graph_and_a_bare_request_changes_nothing(workflow_id):
    from gateway.engine_protocol.node_graph import compile_workflow

    manifest = manifest_of(workflow_id)
    template = template_of(manifest)
    inputs = {"prompt": "x"}
    if workflow_id.startswith("image.edit"):
        inputs["source_image"] = {"data": "abc"}
    bare = compile_workflow(manifest, "comfyui", template, inputs)
    chosen = compile_workflow(manifest, "comfyui", template, {**inputs, "loras": [
        {"name": "example_style_v1.safetensors", "weight": 0.75}, {"name": "example_second_v1.safetensors"}]})
    added = {key: node for key, node in chosen.items() if key not in bare}
    assert [(n["class_type"], n["inputs"]["lora_name"], n["inputs"]["strength_model"]) for n in added.values()] == [
        ("LoraLoaderModelOnly", "example_style_v1.safetensors", 0.75), ("LoraLoaderModelOnly", "example_second_v1.safetensors", 1.0)]
    # the chain sits on one edge: nothing else in the graph moved except that edge's target
    moved = [key for key in bare if bare[key] != chosen[key]]
    assert len(moved) == 1
