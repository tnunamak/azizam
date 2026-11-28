"""Graph history keys and diagnostic context come from validated protocol data."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from gateway.engine_protocol import description
from gateway.engine_protocol.errors import ProtocolError, SelectionRefused, UnknownOperation
from gateway.engine_protocol.node_graph import NodeGraphAdapter
from gateway.engine_protocol.types import CanonicalRequest, JobRef, JobState
from gateway.manifests import ContractErrors, ManifestError, bind_backend_request
from tests.test_node_graph_adapter import CONN, history, run, session

ROOT = Path(__file__).resolve().parents[1]


def protocol_document():
    document = yaml.safe_load((ROOT / "registry/protocols/node-graph.yml").read_text(encoding="utf-8"))
    document["label"] = "Unfamiliar Loom"
    document["binding"] = "fixture_loom"
    document["result_selectors"]["fields"]["key"]["values"] = ["woven", "images", "bundles"]
    return document


def adapter_for(document=None):
    return NodeGraphAdapter(description.parse_description(
        protocol_document() if document is None else document, source="fixture.yml"))


def limits():
    return SimpleNamespace(max_history_bytes=10000, max_selectors=8, max_assets=8)


def finished(job, adapter):
    return run(adapter.poll(CONN, JobRef("graph.submit", "p1"), session=session(history(job))))


@pytest.mark.parametrize("case,code", [
    ("undeclared", "selector_malformed"),
    ("missing", "selectors_missing"),
    ("ambiguous", "result_ambiguous"),
    ("malformed", "result_malformed"),
    ("preview", "result_missing"),
])
def test_declared_history_keys_do_not_weaken_selection_refusals(case, code):
    adapter = adapter_for()
    selector = {"node": "20", "key": "woven", "from": "output_dir"}
    job = {"outputs": {"20": {"woven": [{"filename": "one.bin"}, {"filename": "two.bin"}],
                              "videos": [{"filename": "undeclared.mp4"}]}}}
    block = {"selectors": {"result": selector}}
    if case == "undeclared":
        selector["key"] = "videos"
    elif case == "missing":
        block = {}
    elif case == "ambiguous":
        selector["take"] = "single"
    elif case == "malformed":
        job["outputs"]["20"]["woven"] = [{"filename": "../escape.bin"}]
    else:
        job["outputs"]["20"]["woven"] = [{"filename": "preview.bin", "type": "temp"}]
    with pytest.raises(SelectionRefused) as raised:
        adapter.collect(job, block, {"result": {}}, limits=limits())
    assert raised.value.code == code


def test_history_key_vocabulary_is_validated_before_use():
    document = protocol_document()
    document["result_selectors"]["fields"]["key"]["values"] = "woven"
    with pytest.raises(ProtocolError):
        adapter_for(document)


@pytest.mark.parametrize("case", ["unknown_operation", "missing_manifest", "missing_node", "missing_inputs"])
def test_diagnostics_use_protocol_label_and_binding_context(case):
    adapter = adapter_for()
    if case in ("unknown_operation", "missing_manifest"):
        with pytest.raises(UnknownOperation) as raised:
            if case == "unknown_operation":
                run(adapter.invoke(CONN, "probe.nope", CanonicalRequest("probe.nope")))
            else:
                adapter.compile("probe.nope", CanonicalRequest("probe.nope"))
        assert type(raised.value) is UnknownOperation
        assert str(raised.value).startswith("The Unfamiliar Loom adapter ")
        assert "probe.nope" in str(raised.value)
        assert "node-graph" not in str(raised.value)
        if case == "missing_manifest":
            assert "graph.submit" in str(raised.value) and "manifest" in str(raised.value)
        return

    manifest = {
        "schema_version": 2, "capability": "image", "id": "fixture.run", "intent": "fixture.run",
        "inputs": {"source": {"type": "string"}}, "outputs": {},
        "bindings": {"fixture_loom": {"map": {"source": [{"node": "77", "input": "payload"}]}}},
    }
    request = {} if case == "missing_node" else {"77": {}}
    before = copy.deepcopy(request)
    with pytest.raises(ManifestError) as raised:
        bind_backend_request(manifest, "fixture_loom", request, {"source": "do-not-echo"})
    assert type(raised.value) is ContractErrors
    assert raised.value.code == "target_missing"
    assert raised.value.path == ("bindings", "fixture_loom", "map", "source", 0)
    assert raised.value.primary.pointer == "/bindings/fixture_loom/map/source/0"
    assert str(raised.value) == "binding target missing: node 77 input payload"
    assert "do-not-echo" not in str(raised.value) and request == before


def test_graph_handles_use_declared_output_keys():
    adapter = adapter_for()
    job = {"status": {"completed": True}, "outputs": {
        "20": {
            "bundles": [{"filename": "tail.bin"}],
            "videos": [{"filename": "undeclared.mp4"}],
            "images": [{"filename": "preview.png", "type": "temp"}],
            "woven": [{"filename": "first.bin", "subfolder": "nested/results", "type": "output"},
                      {"filename": "second.bin"}],
        },
        "3": {"woven": [{"filename": "third.bin", "subfolder": "", "type": "output"}],
              "audio": [{"filename": "undeclared.wav"}]},
        "invalid": "not an output block",
    }}
    status = finished(job, adapter)
    assert status.state is JobState.succeeded
    assert [json.loads(handle) for handle in status.assets] == [
        {"kind": "woven", "filename": "first.bin", "subfolder": "nested/results", "type": "output"},
        {"kind": "woven", "filename": "second.bin", "subfolder": "", "type": "output"},
        {"kind": "images", "filename": "preview.png", "subfolder": "", "type": "temp"},
        {"kind": "bundles", "filename": "tail.bin", "subfolder": "", "type": "output"},
        {"kind": "woven", "filename": "third.bin", "subfolder": "", "type": "output"},
    ]
    block = {"selectors": {
        "result": {"node": "20", "key": "woven", "from": "output_dir"},
        "tail": {"node": "20", "key": "bundles", "from": "output_dir"},
    }}
    picked = adapter.collect(job, block, {"result": {}, "tail": {}}, limits=limits())
    assert list(picked) == ["result", "tail"]
    assert picked["result"] == (
        {"filename": "first.bin", "subfolder": "nested/results", "type": "output"},
        {"filename": "second.bin", "subfolder": "", "type": "output"},
    )
    assert picked["tail"] == ({"filename": "tail.bin", "subfolder": "", "type": "output"},)


def test_legacy_default_handles_and_explicit_selector_absence():
    job = {"status": {"completed": True}, "outputs": {
        "9": {"videos": [{"filename": "v.mp4"}], "images": [{"filename": "a.png"}],
              "woven": [{"filename": "undeclared.bin"}]},
    }}
    adapter = NodeGraphAdapter()
    expected = [
        {"kind": "images", "filename": "a.png", "subfolder": "", "type": "output"},
        {"kind": "videos", "filename": "v.mp4", "subfolder": "", "type": "output"},
    ]
    assert [json.loads(handle) for handle in finished(job, adapter).assets] == expected
    assert [json.loads(handle) for handle in NodeGraphAdapter._handles(job)] == expected
    assert adapter.result_selectors is None and adapter.built_from is None
    document = protocol_document()
    del document["result_selectors"]
    unselecting = adapter_for(document)
    assert [json.loads(handle) for handle in finished(job, unselecting).assets] == expected
    with pytest.raises(SelectionRefused) as raised:
        unselecting.collect(job, {"selectors": {"result": {
            "node": "9", "key": "images", "from": "output_dir"}}}, {"result": {}}, limits=limits())
    assert raised.value.code == "selectors_unsupported"


def test_missing_history_selector_inherits_shipped_data(monkeypatch):
    from gateway.engine_protocol import node_graph
    document = protocol_document()
    defaults = description.parse_description(document, source="defaults.yml")
    monkeypatch.setattr(node_graph, "load_shipped_description", lambda protocol: defaults)
    del document["result_selectors"]
    adapter = adapter_for(document)
    status = finished({"status": {"completed": True}, "outputs": {"1": {"woven": [{"filename": "result.bin"}]}}}, adapter)
    assert status.state is JobState.succeeded
    assert [json.loads(handle)["kind"] for handle in status.assets] == ["woven"]


def test_poll_reports_undeclared_outputs_as_an_empty_result():
    # The run core turns an empty result into the pinned OpenAI answer (tests/test_s7_parity_video_texts.py
    # content_no_video_output: 502 "No videos generated"); a protocol error here would change that answer.
    status = finished({"status": {"completed": True}, "outputs": {"1": {"unrecognized": [{"filename": "result.bin"}]}}}, adapter_for())
    assert status.state is JobState.succeeded and status.assets == ()


def test_poll_keeps_empty_output_completion_compatible():
    status = finished({"status": {"completed": True}, "outputs": {}}, adapter_for())
    assert status.state is JobState.succeeded and status.assets == ()
