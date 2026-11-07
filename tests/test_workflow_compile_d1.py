"""D1: workflow data selects validation and compilation. One named test per criterion of DISPATCH.md, section D1.

- criterion 1: ``test_one_operation_fills_declared_defaults_validates_then_binds`` and
  ``test_unknown_and_out_of_range_inputs_are_refused_by_name``
- criterion 2: ``test_public_aliases_are_read_at_the_route_edge``, ``test_the_adapter_reads_no_alias`` and
  ``test_an_unset_seed_is_drawn_at_the_route_edge_only_when_the_manifest_declares_one``
- criterion 3: ``test_the_one_difference_is_yue2_prompt_which_the_route_edge_maps_to_tags``. The map-against-``_meta``
  comparison retired with the templates' ``_meta``; the goldens in ``tests/test_audio_graph_parity.py`` hold the route
  to the graphs the ``_meta`` path sent
- criterion 4: ``test_a_second_graph_bound_workflow_compiles_through_the_same_operation``
- criterion 5: ``test_a_made_up_graph_workflow_runs_through_the_runner_with_data_only``
- criterion 6: dropped in review (no compile cache); see ``test_validate_and_submit_each_compile_the_request_they_are_given``
- criterion 7 is in ``tests/test_operation_runner_boundary.py``.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from gateway import generated_defaults
from gateway.engine_protocol import description, jobs, node_graph
from gateway.engine_protocol.assets import MemoryAssetStore
from gateway.engine_protocol.audio_graph import AudioLimits, InvalidAudioRequest, effective_manifest, manifest_inputs
from gateway.engine_protocol.node_graph import GRAPH_SUBMIT, InputRefused, NodeGraphAdapter, compile_workflow
from gateway.engine_protocol.types import CanonicalRequest, Connection, Timeouts
from gateway.manifests import load_workflow_manifest
from tests.support.fake_upstream import HandlerTransport, fake_upstreams  # noqa: F401 - fixture
from tests.test_operation_runner import Runtime, _new_job, build, drive, jobs_db  # noqa: F401 - fixtures

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "engine_protocol" / "d1"
AUDIO_WORKFLOWS = ("tangoflux", "sa3-small-sfx", "ace-step-1.5-xl-turbo", "yue2-song")
TIMEOUTS = Timeouts(connect_s=10, first_byte_s=30, total_s=30)
PNG = b"\x89PNG\r\n\x1a\n" + bytes(56)


def _manifest(name: str) -> dict[str, Any]:
    return load_workflow_manifest(ROOT / "manifests" / "workflows" / f"{name}.yml")


def _template(manifest: dict[str, Any]) -> dict[str, Any]:
    return json.loads((ROOT / manifest["bindings"]["comfyui"]["workflow"]).read_text(encoding="utf-8"))


def _map_targets(manifest: dict[str, Any], role: str) -> set[tuple[str, str]]:
    """Where the manifest's map writes a declared input, as (node id, node input)."""
    entries = manifest["bindings"]["comfyui"].get("map", {}).get(role, []) if role in manifest["inputs"] else []
    return {(str(entry["node"]), str(entry["input"])) for entry in entries}


# --- criterion 1: one compile path for every manifest-backed workflow -----------------------------------------------


def test_one_operation_fills_declared_defaults_validates_then_binds():
    manifest = _manifest("sa3-small-sfx")  # declares duration default 10; prompt is required
    graph = compile_workflow(manifest, "comfyui", _template(manifest), {"prompt": "a bell"})
    assert graph["3"]["inputs"]["text"] == "a bell", "the prompt map did not bind"
    assert graph["5"]["inputs"]["seconds"] == 10, "the declared default did not fill"
    assert "_meta" not in graph, "the template's metadata reached the submitted graph"


def test_unknown_and_out_of_range_inputs_are_refused_by_name():
    manifest = _manifest("tangoflux")
    template = _template(manifest)
    with pytest.raises(InputRefused) as too_long:
        compile_workflow(manifest, "comfyui", template, {"prompt": "x", "duration": 999})
    assert too_long.value.param == "duration"
    with pytest.raises(InputRefused) as unknown:
        compile_workflow(manifest, "comfyui", template, {"prompt": "x", "flavour": "sour"})
    assert unknown.value.param == "flavour"
    with pytest.raises(InputRefused) as missing:
        compile_workflow(manifest, "comfyui", template, {"duration": 4})
    assert missing.value.param == "prompt"


# --- criterion 2: the aliases belong to the route edge ----------------------------------------------------------------

LIMITS = AudioLimits(default_duration=30, min_seconds=1, max_seconds=30)


def test_public_aliases_are_read_at_the_route_edge():
    manifest = _manifest("tangoflux")
    inputs = manifest_inputs({"input": "rain on a tin roof", "seed": 7, "type": "sfx", "response_format": "native"},
                             manifest=effective_manifest(manifest, LIMITS))
    assert inputs == {"prompt": "rain on a tin roof", "duration": 30.0, "seed": 7}
    with pytest.raises(InvalidAudioRequest) as refused:
        manifest_inputs({"prompt": "x", "type": "opera"}, manifest=effective_manifest(manifest, LIMITS))
    assert refused.value.param == "type"


def test_the_adapter_reads_no_alias():
    """The same aliases, sent straight to the compile path, are refused by name: the adapter does not read them."""
    manifest = _manifest("tangoflux")
    template = _template(manifest)
    for body, param in (({"input": "x", "duration": 4}, "input"), ({"prompt": "x", "type": "sfx"}, "type")):
        with pytest.raises(InputRefused) as refused:
            compile_workflow(manifest, "comfyui", template, body)
        assert refused.value.param == param


def test_an_unset_seed_is_drawn_at_the_route_edge_only_when_the_manifest_declares_one(monkeypatch):
    drawn: list[tuple[int, int]] = []

    def fixed(low: int, high: int) -> int:
        drawn.append((low, high))
        return 777

    monkeypatch.setattr(generated_defaults, "secure_source", fixed)
    manifest = _manifest("tangoflux")
    assert manifest_inputs({"prompt": "x", "duration": 4}, manifest=effective_manifest(manifest, LIMITS))["seed"] == 777
    assert drawn == [(0, 2**32 - 1)]
    given = manifest_inputs({"prompt": "x", "seed": 5}, manifest=effective_manifest(manifest, LIMITS))
    assert given["seed"] == 5 and len(drawn) == 1, "a seed the request named was drawn over"
    no_seed_input = dict(manifest, inputs={k: v for k, v in manifest["inputs"].items() if k != "seed"})
    assert "seed" not in manifest_inputs({"prompt": "x", "duration": 4}, manifest=effective_manifest(no_seed_input, LIMITS))
    assert len(drawn) == 1, "a manifest without a seed input drew one"


# --- criterion 3: the yue2 prompt reaches the node the retired _meta named --------------------------------------------


def test_the_one_difference_is_yue2_prompt_which_the_route_edge_maps_to_tags():
    """yue2-song declares no prompt input. The ``_meta`` route wrote the prompt into node 2; the edge sends it as the
    ``tags`` input, whose map writes node 2. Both reach the same node, so the submitted graph is unchanged."""
    manifest = _manifest("yue2-song")
    assert "prompt" not in manifest["inputs"]
    assert _map_targets(manifest, "tags") == {("2", "value")}
    inputs = manifest_inputs({"prompt": "a sad song"}, manifest=effective_manifest(manifest, AudioLimits(default_duration=120, min_seconds=10, max_seconds=240)))
    assert inputs["tags"] == "a sad song" and "prompt" not in inputs


# --- criterion 4: a second graph-bound workflow, data only ------------------------------------------------------------


def test_a_second_graph_bound_workflow_compiles_through_the_same_operation(fake_upstreams):
    manifest = _manifest("flux2-klein-4b")  # an image workflow that already exists
    inputs = {"prompt": "a fox in snow", "width": 512, "height": 512, "steps": 4, "seed": 11}
    posted: list[Any] = []

    def answer(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"prompt_id": "image-1"})

    fake_upstreams.router.add_transport("images.test:8188", HandlerTransport(answer))
    doc = description.load_registry(description.REGISTRY_DIR)["node-graph"]
    adapter = NodeGraphAdapter(doc, workflow_root=ROOT, client_id="d1")
    conn = Connection(address="http://images.test:8188", timeouts=TIMEOUTS)
    ref = asyncio.run(adapter.submit(conn, "image.generate", CanonicalRequest(
        op="image.generate", body=dict(inputs), params={"manifest": manifest})))
    assert ref.id == "image-1" and len(posted) == 1
    graph = posted[0]["prompt"]
    for name, value in inputs.items():
        for target in manifest["bindings"]["comfyui"]["map"][name]:
            assert graph[str(target["node"])]["inputs"][target["input"]] == value, name
    assert graph == compile_workflow(manifest, "comfyui", _template(manifest), inputs)


# --- criterion 5: a made-up graph workflow, data plus the runner ------------------------------------------------------


def test_a_made_up_graph_workflow_runs_through_the_runner_with_data_only(fake_upstreams, jobs_db, tmp_path):  # noqa: F811
    posts: list[dict[str, Any]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/prompt":
            posts.append(json.loads(request.content))
            graph = posts[-1]["prompt"]
            if graph["2"]["inputs"].get("smiles") != "CCO" or graph["2"]["inputs"].get("top_k") != 3:
                return httpx.Response(400, json={"error": "a bound input is missing or wrong"})
            return httpx.Response(200, json={"prompt_id": "smell-1"})
        if request.method == "GET" and path == "/history/smell-1":
            entry = {"status": {"completed": True, "status_str": "success", "messages": []},
                     "outputs": {"3": {"images": [{"filename": "smell.png", "subfolder": "", "type": "output"}]}}}
            return httpx.Response(200, json={"smell-1": entry})
        if request.method == "GET" and path == "/view":
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        return httpx.Response(404)

    host = "smell-graph.test:7890"
    fake_upstreams.router.add_transport(host, HandlerTransport(answer))
    manifest = load_workflow_manifest(FIXTURES / "smell-graph.yml")
    doc = description.load_registry(description.REGISTRY_DIR)["node-graph"]
    store = MemoryAssetStore()
    adapter = NodeGraphAdapter(doc, assets=store, workflow_root=FIXTURES, client_id="d1")
    runtime = Runtime(adapter, Connection(address=f"http://{host}", timeouts=TIMEOUTS), {"manifest": manifest},
                      (0.5, 5.0))
    job_id = _new_job(jobs_db)
    bundle = asyncio.run(jobs.run(runtime, "smell.infer", {"smiles": "CCO"}, job_id=job_id))
    assert len(posts) == 1, "the fixture workflow was not submitted once"
    (result,) = bundle.results.values()   # a version 2 adapter returns the bundle the run published
    assert [ref.content_type for ref in result.assets] == ["image/png"]


# --- criterion 6 (dropped in review): no compile cache. validate and submit each compile the request they are given.


def test_validate_and_submit_each_compile_the_request_they_are_given(fake_upstreams, jobs_db, tmp_path, monkeypatch):  # noqa: F811
    calls: list[str] = []
    real = node_graph.compile_workflow

    def counting(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append("compile")
        return real(*args, **kwargs)

    monkeypatch.setattr(node_graph, "compile_workflow", counting)
    harness = build("graph", fake_upstreams, jobs_db, tmp_path, script=["done"])
    outcome = drive(harness, _new_job(jobs_db))
    assert outcome["assets"], "the job did not complete"
    assert harness.facts["submits"] == 1
    assert calls == ["compile", "compile"], f"expected one compile in validate and one in submit, got {len(calls)}"


def test_a_finished_graph_is_not_compiled(fake_upstreams, monkeypatch):  # noqa: F811
    calls: list[str] = []
    real = node_graph.compile_workflow
    monkeypatch.setattr(node_graph, "compile_workflow",
                        lambda *a, **k: (calls.append("compile"), real(*a, **k))[1])
    doc = description.load_registry(description.REGISTRY_DIR)["node-graph"]
    adapter = NodeGraphAdapter(doc, workflow_root=ROOT, client_id="d1")
    conn = Connection(address="http://direct.test:7890", timeouts=TIMEOUTS)
    fake_upstreams.router.add_transport("direct.test:7890", HandlerTransport(
        lambda _r: httpx.Response(200, json={"prompt_id": "d"})))
    finished = CanonicalRequest(op=GRAPH_SUBMIT, body={"prompt": {}})
    asyncio.run(adapter.submit(conn, GRAPH_SUBMIT, finished))
    assert calls == [], "a finished graph was compiled"


# --- D1 review fixes: no compile cache, finite numbers, every accepted input has an effect, one effective spec ------

def _doc() -> Any:
    return description.load_registry(description.REGISTRY_DIR)["node-graph"]


def _capture(fake_upstreams, host: str, posted: list[Any]) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"prompt_id": "p"})

    fake_upstreams.router.add_transport(host, HandlerTransport(answer))


def test_submit_compiles_the_body_it_is_given_not_the_one_validate_saw(fake_upstreams):
    manifest = _manifest("tangoflux")
    posted: list[Any] = []
    _capture(fake_upstreams, "mutable.test:7890", posted)
    conn = Connection(address="http://mutable.test:7890", timeouts=TIMEOUTS)
    adapter = NodeGraphAdapter(_doc(), workflow_root=ROOT, client_id="one")
    request = CanonicalRequest(op="audio.generate", body={"prompt": "bell", "duration": 3, "seed": 1},
                               params={"manifest": manifest})
    adapter.validate(conn, "audio.generate", request)
    request.body = {"prompt": "rain", "duration": 5, "seed": 2}
    asyncio.run(adapter.submit(conn, "audio.generate", request))
    assert posted[0]["prompt"] == compile_workflow(manifest, "comfyui", _template(manifest),
                                                   {"prompt": "rain", "duration": 5, "seed": 2})


def test_a_body_changed_to_an_out_of_range_value_is_refused_at_submit(fake_upstreams):
    manifest = _manifest("tangoflux")
    posted: list[Any] = []
    _capture(fake_upstreams, "mutable.test:7890", posted)
    conn = Connection(address="http://mutable.test:7890", timeouts=TIMEOUTS)
    adapter = NodeGraphAdapter(_doc(), workflow_root=ROOT, client_id="one")
    request = CanonicalRequest(op="audio.generate", body={"prompt": "bell", "duration": 3},
                               params={"manifest": manifest})
    adapter.validate(conn, "audio.generate", request)
    request.body = {"prompt": "rain", "duration": 999}
    with pytest.raises(InputRefused) as refused:
        asyncio.run(adapter.submit(conn, "audio.generate", request))
    assert refused.value.param == "duration"
    assert posted == [], "a refused body reached the engine"


def test_each_adapter_sends_its_own_client_id(fake_upstreams):
    manifest = _manifest("tangoflux")
    posted: list[Any] = []
    _capture(fake_upstreams, "clients.test:7890", posted)
    conn = Connection(address="http://clients.test:7890", timeouts=TIMEOUTS)
    first = NodeGraphAdapter(_doc(), workflow_root=ROOT, client_id="client-a")
    second = NodeGraphAdapter(_doc(), workflow_root=ROOT, client_id="client-b")
    request = CanonicalRequest(op="audio.generate", body={"prompt": "bell", "duration": 3, "seed": 1},
                               params={"manifest": manifest})
    first.validate(conn, "audio.generate", request)
    asyncio.run(second.submit(conn, "audio.generate", request))
    asyncio.run(first.submit(conn, "audio.generate", request))
    assert [body["client_id"] for body in posted] == ["client-b", "client-a"]


def test_a_dict_input_bound_into_the_graph_is_copied_not_aliased():
    manifest = _manifest("flux2-klein-4b-edit")
    picture = {"data": "abc"}
    graph = compile_workflow(manifest, "comfyui", _template(manifest), {"prompt": "x", "source_image": picture})
    picture["data"] = "changed after compile"
    assert graph["10"]["inputs"]["image"] == {"data": "abc"}, "the caller's dict reached the compiled graph"


def test_an_accepted_input_with_no_mapping_is_refused_by_name():
    manifest = _manifest("tangoflux")
    template = _template(manifest)
    unmapped = copy.deepcopy(manifest)
    del unmapped["bindings"]["comfyui"]["map"]["guidance"]
    with pytest.raises(InputRefused) as refused:
        compile_workflow(unmapped, "comfyui", template, {"prompt": "x", "guidance": 3.5})
    assert refused.value.param == "guidance"
    emptied = copy.deepcopy(manifest)
    emptied["bindings"]["comfyui"]["map"]["guidance"] = []
    with pytest.raises(InputRefused) as empty:
        compile_workflow(emptied, "comfyui", template, {"prompt": "x", "guidance": 3.5})
    assert empty.value.param == "guidance"


def test_an_edge_only_input_is_refused_by_the_graph_compile_and_its_default_is_not_filled():
    manifest = _manifest("flux2-klein-4b-edit")
    with pytest.raises(InputRefused) as refused:
        compile_workflow(manifest, "comfyui", _template(manifest),
                         {"prompt": "x", "source_image": {"data": "abc"}, "mask": {"data": "m"}})
    assert refused.value.param == "mask"
    video = _manifest("video-scail-2-replace")
    assert video["inputs"]["reference_fit"]["default"] is True
    # Every route-applied input is marked; compiling without the route's pictures must not refuse on a default.
    assert set(video["bindings"]["comfyui"]["edge_only_inputs"]) >= {"reference_fit", "source_point_x", "source_point_y"}


def test_every_declared_input_of_a_graph_bound_manifest_is_mapped_or_marked_edge_only():
    gaps: dict[str, list[str]] = {}
    for path in sorted((ROOT / "manifests" / "workflows").glob("*.yml")):
        manifest = load_workflow_manifest(path)
        block = manifest["bindings"].get("comfyui")
        if not isinstance(block, dict):
            continue
        mapped = block.get("map") or {}
        edge_only = set(block.get("edge_only_inputs") or [])
        assert not {name for name in edge_only if mapped.get(name)}, f"{path.name} marks a mapped input edge-only"
        missing = [name for name in manifest["inputs"] if not mapped.get(name) and name not in edge_only]
        if missing:
            gaps[path.name] = missing
    assert gaps == {}


def test_another_bindings_edge_only_marker_does_not_break_the_selected_binding():
    manifest = copy.deepcopy(_manifest("flux2-klein-4b-edit"))
    manifest["bindings"]["alternate"] = copy.deepcopy(manifest["bindings"]["comfyui"])
    graph = compile_workflow(manifest, "comfyui", _template(manifest), {"prompt": "x", "source_image": {"data": "abc"}})
    assert graph


def test_the_video_replace_workflow_compiles_without_its_route_applied_inputs():
    video = _manifest("video-scail-2-replace")
    required = {name: spec for name, spec in video["inputs"].items()
                if spec.get("required") and name not in video["bindings"]["comfyui"]["edge_only_inputs"]}
    supplied = {name: ({"data": "abc"} if spec.get("type") in ("asset", "image") else
                       ("x" if spec.get("type", "string") == "string" else 1)) for name, spec in required.items()}
    assert compile_workflow(video, "comfyui", _template(video), supplied)
