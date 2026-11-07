"""Output selectors and adapter descriptors (S2.4, D5 and D8): the grammar, the load check and the save rules."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from gateway import output_selectors as sel
from gateway.engine_protocol import description
from gateway.engine_protocol.errors import DescriptionError
from gateway.manifests import ManifestError, validate_manifest

ROOT = Path(__file__).resolve().parent.parent
PROTOCOL = ROOT / "registry" / "protocols" / "node-graph.yml"

DESCRIPTOR: dict[str, Any] = {
    "version": 1,
    "identity": ["node", "key", "from"],
    "template_node": "node",
    "kind_field": "decode",
    "fields": {
        "node": {"type": "text", "required": True},
        "key": {"type": "enum", "required": True, "values": ["files", "others"]},
        "from": {"type": "enum", "required": True, "values": ["out_dir"]},
        "decode": {"type": "enum", "default": "none", "values": ["none", "json"], "kinds": {"none": "assets", "json": "json"}},
        "take": {"type": "enum", "default": "all", "values": ["all", "first", "single"]},
    },
}


def descriptor(**changes: Any) -> sel.SelectorDescriptor:
    return sel.parse_descriptor({**copy.deepcopy(DESCRIPTOR), **changes})


def selector(**changes: Any) -> dict[str, str]:
    return {"node": "9", "key": "files", "from": "out_dir", **changes}


def manifest(selectors: Any = "default", outputs: Any = None, inputs: Any = None, **block: Any) -> dict[str, Any]:
    binding: dict[str, Any] = {"workflow": "w.json", "map": {}, **block}
    if selectors == "default":
        selectors = {"first": selector()}
    if selectors is not None:
        binding["selectors"] = selectors
    return {"schema_version": 1, "id": "x.y", "intent": "x.y", "inputs": inputs or {},
            "outputs": outputs or {"first": {"media_type": "a/b"}}, "bindings": {"eng": binding}}


TEMPLATE = {"1": {"class_type": "A", "inputs": {"text": "hi"}}, "9": {"class_type": "B", "inputs": {"in": ["1", 0]}}}


# ------------------------------------------------------------------------------------------- the descriptor (D8)

def test_a_good_descriptor_loads_and_says_what_each_selector_yields():
    parsed = descriptor()
    assert parsed.identity == ("node", "key", "from") and parsed.template_node == "node"
    assert parsed.kind_of({"decode": "json"}) == "json" and parsed.kind_of({"decode": "none"}) == "assets"


@pytest.mark.parametrize("bad", [
    {"version": 2},
    {"version": True},
    {"fields": {}},
    {"fields": []},
    {"extra": 1},
    {"identity": []},
    {"identity": ["node", "node"]},
    {"identity": ["nope"]},
    {"identity": ["take"]},               # an optional field cannot say where a result is
    {"template_node": "key"},             # an enum is not a node name
    {"template_node": "nope"},
    {"kind_field": "take"},               # no kinds declared
    {"kind_field": "nope"},
    {"kind_field": None},                 # kinds declared on a field that is not the kind field
])
def test_a_malformed_descriptor_never_loads(bad: dict[str, Any]):
    with pytest.raises(sel.DescriptorError):
        descriptor(**bad)


@pytest.mark.parametrize("path,value", [
    (("fields", "key", "values"), []),
    (("fields", "key", "values"), ["a", "a"]),
    (("fields", "key", "values"), [1]),
    (("fields", "key", "type"), "number"),
    (("fields", "key", "required"), "yes"),
    (("fields", "key", "surprise"), 1),
    (("fields", "decode", "default"), "other"),
    (("fields", "decode", "kinds"), {"none": "assets"}),            # a value with no kind
    (("fields", "decode", "kinds"), {"none": "assets", "json": "mesh"}),   # not a result kind
    (("fields", "take", "default"), None),                           # an optional enum with no default
    (("fields", "node", "values"), ["a"]),                           # text takes no values
    (("fields", "node", "required"), False),                         # template_node must be required
])
def test_a_malformed_field_never_loads(path: tuple[str, ...], value: Any):
    doc = copy.deepcopy(DESCRIPTOR)
    target = doc
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value
    with pytest.raises(sel.DescriptorError):
        sel.parse_descriptor(doc)


def test_a_field_cannot_be_required_and_have_a_default():
    doc = copy.deepcopy(DESCRIPTOR)
    doc["fields"]["node"]["default"] = "1"
    with pytest.raises(sel.DescriptorError):
        sel.parse_descriptor(doc)


def test_the_node_graph_protocol_declares_its_selector_form_and_a_malformed_one_refuses_the_whole_description():
    text = PROTOCOL.read_text(encoding="utf-8")
    loaded = description.parse_description_text(text, source="node-graph.yml")
    assert loaded.result_selectors is not None and loaded.result_selectors.identity == ("node", "key", "from")
    broken = text.replace("identity: [node, key, from]", "identity: [node, nothing]")
    assert broken != text
    with pytest.raises(DescriptionError, match="result_selectors"):
        description.parse_description_text(broken, source="node-graph.yml")


def test_only_an_adapter_that_names_a_binding_takes_selectors():
    doc = yaml.safe_load(PROTOCOL.read_text(encoding="utf-8"))
    doc.pop("binding")
    with pytest.raises(DescriptionError, match="result_selectors"):
        description.parse_description(doc, source="x")


def test_the_shipped_descriptors_are_found_by_binding_key():
    found = sel.shipped_descriptors()
    assert set(found) == {"comfyui"}


# ------------------------------------------------------------------------ a binding against its descriptor (load)

def test_a_binding_is_checked_against_its_descriptor_and_defaults_are_filled():
    chosen = sel.check_binding(descriptor(), {"first": {}}, {"selectors": {"first": selector()}})
    assert chosen == {"first": {"node": "9", "key": "files", "from": "out_dir", "decode": "none", "take": "all"}}


def test_a_binding_with_no_selectors_loads_here_and_is_refused_by_the_save_and_run_rules():
    assert sel.check_binding(descriptor(), {"first": {}}, {}) == {}


@pytest.mark.parametrize("changes,code", [
    ({"nope": "x"}, "selector_malformed"),            # a field the adapter does not take
    ({"node": None}, "selector_malformed"),           # a required field missing
    ({"node": 9}, "selector_malformed"),
    ({"key": "unlisted"}, "selector_malformed"),
    ({"from": "elsewhere"}, "selector_malformed"),
    ({"take": "some"}, "selector_malformed"),
    ({"node": "x" * 200}, "selector_malformed"),
])
def test_a_selector_the_descriptor_does_not_accept_is_refused(changes: dict[str, Any], code: str):
    raw = {**selector(), **changes}
    raw = {k: v for k, v in raw.items() if v is not None}
    with pytest.raises(sel.SelectorError) as caught:
        sel.check_binding(descriptor(), {"first": {}}, {"selectors": {"first": raw}})
    assert caught.value.code == code


@pytest.mark.parametrize("selectors,code", [
    ([], "selector_malformed"),
    ({}, "selector_malformed"),
    ({"first": "not a mapping"}, "selector_malformed"),
    ({"first": selector(), "ghost": selector(node="3")}, "selector_unknown_result"),
])
def test_selectors_must_name_declared_results(selectors: Any, code: str):
    with pytest.raises(sel.SelectorError) as caught:
        sel.check_binding(descriptor(), {"first": {}}, {"selectors": selectors})
    assert caught.value.code == code


def test_every_declared_result_needs_a_selector_once_any_is_declared():
    with pytest.raises(sel.SelectorError) as caught:
        sel.check_binding(descriptor(), {"first": {}, "second": {}}, {"selectors": {"first": selector()}})
    assert caught.value.code == "selector_missing_result" and "second" in str(caught.value)


def test_two_results_that_read_the_same_place_are_duplicates_whatever_else_differs():
    with pytest.raises(sel.SelectorError) as caught:
        sel.check_binding(descriptor(), {"a": {}, "b": {}},
                          {"selectors": {"a": selector(), "b": selector(take="first", decode="json")}})
    assert caught.value.code == "selector_duplicate"
    sel.check_binding(descriptor(), {"a": {}, "b": {}}, {"selectors": {"a": selector(), "b": selector(node="10")}})


def test_the_selector_count_is_limited():
    outputs = {f"r{i}": {} for i in range(4)}
    block = {"selectors": {name: selector(node=str(i)) for i, name in enumerate(outputs)}}
    with pytest.raises(sel.SelectorError) as caught:
        sel.check_binding(descriptor(), outputs, block, max_selectors=3)
    assert caught.value.code == "selector_count"
    assert len(sel.check_binding(descriptor(), outputs, block, max_selectors=4)) == 4


def test_a_manifest_load_checks_its_selectors_against_the_adapter_descriptor():
    descriptors = {"eng": descriptor()}
    sel.check_manifest(manifest(), descriptors)
    sel.check_manifest(manifest(selectors=None), descriptors)    # a stored manifest with none loads; save and run refuse it
    with pytest.raises(sel.SelectorError):
        sel.check_manifest(manifest(selectors={"first": selector(key="unlisted")}), descriptors)
    with pytest.raises(sel.SelectorError, match="takes none"):
        sel.check_manifest(manifest(), {})                       # an adapter that declares no selector form


def test_validate_manifest_refuses_a_bad_selector_on_a_shipped_adapter():
    good = yaml.safe_load((ROOT / "manifests/workflows/sa3-small-sfx.yml").read_text(encoding="utf-8"))
    validate_manifest(good)
    bad = copy.deepcopy(good)
    bad["bindings"]["comfyui"]["selectors"]["audio"]["key"] = "unlisted"
    with pytest.raises(ManifestError):
        validate_manifest(bad)


# ------------------------------------------------------------------------------------------- refusal at save (D5)

def save(m: dict[str, Any], template: Any = TEMPLATE, parsed: Any = "default") -> dict[str, Any]:
    return sel.check_for_save(descriptor() if parsed == "default" else parsed, m, "eng", template=template)


def refused(m: dict[str, Any], code: str, **kw: Any) -> str:
    with pytest.raises(sel.SelectorError) as caught:
        save(m, **kw)
    assert caught.value.code == code
    return str(caught.value)


def test_a_good_new_contract_is_saved():
    assert save(manifest()) == {"first": {"node": "9", "key": "files", "from": "out_dir", "decode": "none", "take": "all"}}


def test_a_new_contract_with_no_selectors_is_refused_with_a_reason_that_says_what_to_add():
    message = refused(manifest(selectors=None), "selectors_missing")
    assert "declares no output selectors" in message and "eng" in message


def test_a_binding_of_an_adapter_that_takes_no_selectors_must_declare_none():
    assert save(manifest(selectors=None), parsed=None) == {}
    refused(manifest(), "selector_malformed", parsed=None)


def test_a_selector_that_names_a_node_the_template_lacks_is_refused():
    refused(manifest(selectors={"first": selector(node="404")}), "selector_unknown_node")


def test_a_selector_that_echoes_a_source_is_refused():
    # Node 1 has no edge into it: it holds its own settings, so what it lists is a source, not a result.
    message = refused(manifest(selectors={"first": selector(node="1")}), "selector_echoes_source")
    assert "source" in message


def test_a_selector_on_a_node_that_an_asset_input_writes_is_an_echo_of_that_input():
    template = {**TEMPLATE, "5": {"class_type": "C", "inputs": {"in": ["1", 0], "file": "placeholder"}}}
    m = manifest(selectors={"first": selector(node="5")}, inputs={"photo": {"type": "asset"}},
                 map={"photo": [{"node": "5", "input": "file"}]})
    refused(m, "selector_echoes_source", template=template)
    # A plain value written to the same node is not a source.
    plain = manifest(selectors={"first": selector(node="5")}, inputs={"photo": {"type": "string"}},
                     map={"photo": [{"node": "5", "input": "file"}]})
    assert save(plain, template=template)
    # The path form of a mapping names its node first.
    by_path = manifest(selectors={"first": selector(node="5")}, inputs={"photo": {"type": "image"}},
                       map={"photo": [{"path": "5.inputs.file"}]})
    refused(by_path, "selector_echoes_source", template=template)


def test_duplicate_selectors_are_refused_at_save():
    m = manifest(selectors={"a": selector(), "b": selector()}, outputs={"a": {"media_type": "a/b"}, "b": {"media_type": "c/d"}})
    refused(m, "selector_duplicate")


def test_two_outputs_of_one_type_with_no_selectors_are_refused_as_ambiguous():
    m = manifest(selectors=None, outputs={"a": {"media_type": "a/b"}, "b": {"media_type": "a/b"}})
    message = refused(m, "results_ambiguous")
    assert "a" in message and "nothing tells them apart" in message


def test_two_outputs_of_one_type_are_fine_when_each_has_its_own_selector():
    m = manifest(selectors={"a": selector(), "b": selector(node="1")}, outputs={"a": {"media_type": "a/b"}, "b": {"media_type": "a/b"}})
    refused(m, "selector_echoes_source")     # node 1 is a source, so it is the echo rule that stops it here
    template = {**TEMPLATE, "8": {"class_type": "B", "inputs": {"in": ["1", 0]}}}
    m = manifest(selectors={"a": selector(), "b": selector(node="8")}, outputs={"a": {"media_type": "a/b"}, "b": {"media_type": "a/b"}})
    assert set(save(m, template=template)) == {"a", "b"}


def test_ambiguous_outputs_names_the_results_that_share_a_type():
    assert sel.ambiguous_outputs({"a": {"media_type": "x/y"}, "b": {"media_type": "x/y"}, "c": {"media_type": "z/y"}}) == ["a", "b"]
    assert sel.ambiguous_outputs({"a": {"media_type": "x/y"}}) == []


# --------------------------------------------------------------------------------------------- the fetch gate (D6)

def limits(**changes: int) -> Any:
    return SimpleNamespace(max_concurrent_fetches=2, fetch_timeout_s=5, **changes)


def test_the_fetch_gate_holds_at_most_the_limit_in_flight():
    gate = sel.FetchGate()
    state = {"now": 0, "peak": 0}

    async def one() -> None:
        async with gate.slot(limits()):
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
            await asyncio.sleep(0.01)
            state["now"] -= 1

    async def main() -> None:
        await asyncio.gather(*(one() for _ in range(6)))

    asyncio.run(main())
    assert state["peak"] == 2


def test_a_fetch_that_runs_past_the_time_limit_is_refused_and_its_slot_is_freed():
    gate = sel.FetchGate()
    short = SimpleNamespace(max_concurrent_fetches=1, fetch_timeout_s=0.05)

    async def main() -> str:
        with pytest.raises(sel.FetchRefused) as caught:
            async with gate.slot(short):
                await asyncio.sleep(1)
        async with gate.slot(short):
            pass            # the slot came back
        return caught.value.code

    assert asyncio.run(main()) == "fetch_timeout"


def test_the_time_limit_covers_waiting_for_a_slot():
    gate = sel.FetchGate()
    short = SimpleNamespace(max_concurrent_fetches=1, fetch_timeout_s=0.05)

    async def main() -> str:
        async def hold() -> None:
            async with gate.slot(SimpleNamespace(max_concurrent_fetches=1, fetch_timeout_s=5)):
                await asyncio.sleep(0.3)

        holder = asyncio.ensure_future(hold())
        await asyncio.sleep(0.01)
        with pytest.raises(sel.FetchRefused) as caught:
            async with gate.slot(short):
                pass
        await holder
        return caught.value.code

    assert asyncio.run(main()) == "fetch_timeout"


@pytest.mark.parametrize("name", ["a/b", "a%2Fb", "..", ".x", "a b", "", "x" * 129])
def test_a_manifest_output_name_that_is_not_one_path_segment_is_refused(name: str):
    from gateway.manifests import RESULT_NAME_RULE, load_workflow_manifest

    manifest = copy.deepcopy(load_workflow_manifest(ROOT / "manifests" / "workflows" / "tangoflux.yml"))
    manifest["outputs"] = {name: {"media_type": "audio/wav"}}
    with pytest.raises(ManifestError, match="result name starts with a letter or digit") as refused:
        validate_manifest(manifest)
    assert RESULT_NAME_RULE in str(refused.value)
