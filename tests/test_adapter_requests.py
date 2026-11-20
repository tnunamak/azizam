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


def test_a_run_with_no_adapter_and_the_manifests_own_encoder_reads_no_file_list(world, engine_files):
    """Try sends an empty pick and the manifest's encoder; an engine that cannot list its files must not block that run."""
    serve(world)
    engine_files.listing_fails = True
    encoder = manifest_of(WORKFLOW)["inputs"]["text_encoder"]["enum"][0]
    response = run(WORKFLOW, {"prompt": "x", "loras": [], "text_encoder": encoder})
    assert response.status_code == 200, response.text
    assert engine_files.listed == 0


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


def test_the_image_route_with_no_adapter_and_the_manifests_own_encoder_reads_no_file_list(engine_files):
    engine_files.listing_fails = True
    encoder = manifest_of(WORKFLOW)["inputs"]["text_encoder"]["enum"][0]
    graph, response, receipt = image_call({"loras": [], "text_encoder": encoder})
    assert response is None, response and response.body
    assert engine_files.listed == 0
    assert receipt.metadata["adapters"]["text_encoder"] == encoder


def test_the_image_route_refuses_an_encoder_the_manifest_does_not_list_with_a_422(engine_files):
    graph, response, _ = image_call({"text_encoder": "not_listed_encoder.safetensors"})
    assert graph is None and response is not None and response.status_code == 422
    assert engine_files.listed == 0


def test_the_image_route_keeps_the_unknown_input_refusal_for_a_workflow_without_adapters():
    graph, response, _ = image_call({"loras": [{"name": "example_style_v1.safetensors"}]}, manifest=manifest_of(PLAIN_WORKFLOW))
    assert graph is None and response is not None and response.status_code == 400
    assert "unknown input" in json.loads(response.body)["error"]["message"]


def test_the_image_route_refuses_a_malformed_extension_block_with_a_400_not_a_500():
    receipt = RouteReceipt(protocol="openai", intent="image.generate", path="/v1/images/generations", request_id="req-bad-ext")
    with patch.object(proxy, "_graph_service", lambda _capability: SERVICE):
        graph, response = asyncio.run(proxy._apply_workflow_extensions_to_image_request(
            request_body={"prompt": "x", "extensions": {EXT: ["x"]}},
            manifest=manifest_of(WORKFLOW), backend_request=copy.deepcopy(template_of(manifest_of(WORKFLOW))),
            standard_fields=proxy.STANDARD_IMAGE_GENERATE_FIELDS, receipt=receipt))
    assert graph is None and response is not None and response.status_code == 400
    assert json.loads(response.body)["error"]["type"] == "invalid_request_error"


def test_a_run_naming_an_encoder_the_manifest_does_not_list_is_refused_with_a_422(world):
    engine = serve(world)
    response = run(WORKFLOW, {"prompt": "x", "text_encoder": "not_listed_encoder.safetensors"})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["problems"][0]["path"] == "/inputs/text_encoder"
    assert engine.calls == []


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
    row = adapter_row(rows, WORKFLOW)
    manifest = manifest_of(WORKFLOW)["inputs"]["loras"]
    assert row["type"] == "array" and row["max_items"] == manifest["max_items"]
    assert row["items"]["type"] == "object"
    properties = row["items"]["properties"]
    assert properties["name"]["enum"] == ["example_style_v1.safetensors"]
    assert "choice_labels" not in properties["name"]          # an operator's file has no label of its own
    for field in ("name", "weight"):
        declared = manifest["items"]["properties"][field]
        for key in ("label", "help", "minimum", "maximum", "default"):
            assert properties[field].get(key) == declared.get(key), (field, key)
    assert "adapter_files" not in row


def test_the_member_listing_offers_the_manifests_encoders(gateway, monkeypatch):
    rows = member_workflows(gateway, monkeypatch)
    encoder = next(r for r in rows[WORKFLOW]["inputs"] if r["name"] == "text_encoder")
    assert encoder["enum"] == list(manifest_of(WORKFLOW)["inputs"]["text_encoder"]["enum"])


def test_a_listing_when_the_engine_has_no_compatible_file_takes_no_items(gateway, monkeypatch):
    monkeypatch.setattr(adapter_choices, "default_list_files", lambda *a: ["example_no_header.safetensors"])
    row = adapter_row(member_workflows(gateway, monkeypatch), WORKFLOW)
    assert row["max_items"] == 0


def test_a_listing_when_the_engine_cannot_list_shows_no_files_and_is_still_served(gateway, monkeypatch, engine_files):
    engine_files.listing_fails = True
    rows = member_workflows(gateway, monkeypatch)
    assert "items" not in adapter_row(rows, WORKFLOW)


def test_a_workflow_that_takes_no_adapters_has_no_adapter_row(gateway, monkeypatch):
    rows = member_workflows(gateway, monkeypatch)
    assert adapter_row(rows, PLAIN_WORKFLOW) is None



# -------------------------------------------------------------------------------------- the shipped adapter workflows


# the node whose model input the chain takes over, and the link that input held before (the base loader's output)
ANCHORS = [("image.generate.krea2-turbo", "7", ["1", 0]),
           ("image.generate.krea2-turbo-snofs", "7", ["11", 0]),
           ("image.edit.qwen-image-2.1-edit", "5", ["1", 0]),
           ("image.generate.flux2-klein", "31", ["1", 0]),
           ("image.generate.flux2-klein-snofs", "31", ["11", 0])]


@pytest.mark.parametrize("workflow_id,anchor,base_link", ANCHORS)
def test_each_shipped_adapter_workflow_binds_a_chain_into_its_own_graph_and_a_bare_request_changes_nothing(workflow_id, anchor, base_link):
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
    assert moved == [anchor]
    first, last = list(added.values())[0], list(added)[-1]
    assert first["inputs"]["model"] == base_link == bare[anchor]["inputs"]["model"]
    assert chosen[anchor]["inputs"]["model"] == [last, 0]


def test_the_upload_sweep_visits_every_folder_a_workflow_declares_for_adapters():
    """After a restart no upload may name a folder, so the sweep learns the folders from the workflows."""
    assert "loras" in proxy._declared_adapter_folders()
    assert proxy._declared_adapter_folders() <= proxy.ADAPTER_LIBRARY.folders()


# ------------------------------------------------------------------------------------------- a member's own upload, end to end


def test_a_members_upload_goes_from_upload_to_choices_to_run_to_restart_sweep(world, engine_files, tmp_path, monkeypatch):
    """One Store, one process: the upload is checked and labelled, offered (with its label) to its owner only, named by
    asset id in a run whose graph carries the engine copy, held while the engine works, and after a restart the sweep
    removes the unheld orphan and keeps the held copy."""
    import hashlib
    import io

    from gateway import adapter_upload
    from tests.test_adapter_upload import TABLE, Store, safetensors

    monkeypatch.setitem(TABLE, "krea2", {"ss_base_model_version": ("krea2",)})
    krea = safetensors({"a": [2]}, metadata={"ss_base_model_version": "krea2"})
    orphan_bytes = safetensors({"o": [7]}, metadata={"ss_base_model_version": "krea2"})
    store = Store(tmp_path)
    monkeypatch.setattr(adapter_choices, "UPLOADS", store.library)
    caller = {"person": "robin"}
    monkeypatch.setattr(proxy, "_auth_person_id", lambda _auth: caller["person"])

    # 1. the file is checked on its header, then stored under the member's own name and label
    checked = adapter_upload.validate_stream(io.BytesIO(krea), len(krea), adapter_upload.Policy(True))
    assert checked.digest == hashlib.sha256(krea).hexdigest()
    mine = store.add("robin", krea, "my-style.safetensors")
    orphan = store.add("robin", orphan_bytes, "scratch.safetensors")
    assert store.sidecars[mine]["adapter"] == {"label": "my-style.safetensors", "content_sha256": checked.digest}

    # 2. the choices show the upload's id with its label to its owner, and show another member neither
    record = SimpleNamespace(id=WORKFLOW, contract=manifest_of(WORKFLOW))

    async def listing(person):
        got = await adapter_choices.offered([record], "comfyui", service_id="comfyui", origin="http://engine.invalid",
                                            list_files=adapter_choices.default_list_files,
                                            read_header=adapter_choices.default_read_header, person_id=person)
        return got[WORKFLOW]["loras"]["items"]["properties"]["name"]

    robins = asyncio.run(listing("robin"))
    assert mine in robins["enum"] and robins["choice_labels"][mine] == "my-style.safetensors"
    assert "example_style_v1.safetensors" in robins["enum"]            # the operator's file stays on the list
    sams = asyncio.run(listing("sam"))
    assert mine not in sams["enum"] and "choice_labels" not in sams
    assert mine not in json.dumps(sams) and "my-style.safetensors" not in json.dumps(sams)

    # 3. a run names the asset id; the engine's graph names the copy, which is held while the engine works
    seen = {}

    class WatchingEngine(FakeGraphEngine):
        def __call__(self, request):
            if request.url.path == "/prompt":
                seen["held_in_run"] = store.library.held_digests()
            return super().__call__(request)

    engine = serve_engine(world, WatchingEngine(manifest_of(WORKFLOW), png_bytes((9, 9, 9)), "image/png", "out.png"))
    response = run(WORKFLOW, {"prompt": "a red fox", "loras": [{"name": mine, "weight": 0.5}]})
    assert response.status_code == 200 and response.json()["status"] == "succeeded", response.text
    added = [n for n in engine.prompts[0]["prompt"].values() if n["class_type"] == "LoraLoaderModelOnly"]
    [copy_name] = [n["inputs"]["lora_name"] for n in added]
    assert copy_name != mine and mine not in json.dumps(engine.prompts[0])
    assert store.library.is_copy(copy_name) and (store.engine / copy_name).exists()
    assert seen["held_in_run"] == {checked.digest}
    assert store.library.held_digests() == set()                       # the run ended, so its hold ended
    caller["person"] = "sam"                                           # another member cannot name robin's upload
    assert run(WORKFLOW, {"prompt": "x", "loras": [{"name": mine}]}).status_code == 422
    caller["person"] = "robin"

    # the orphan's copy is made as a run would make it; then both uploads are deleted, the gateway restarts, and the
    # run that still holds the first copy resumes (a recovered run holds its copies again)
    orphan_copy = store.library.engine_names("robin", [orphan], folder="loras")[orphan]
    assert (store.engine / orphan_copy).exists()
    del store.sidecars[mine], store.sidecars[orphan]

    # 4. the restarted Library learns the folder from the workflows, not from any upload
    restarted = store.new_library(declared_folders=lambda: iter(proxy._declared_adapter_folders()))
    restarted.hold_copies("resumed-run", [copy_name])
    assert restarted.sweep_all() == [hashlib.sha256(orphan_bytes).hexdigest()]
    assert (store.engine / copy_name).exists() and not (store.engine / orphan_copy).exists()
