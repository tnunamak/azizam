"""A list input can add one node per item on a graph edge (a chain), as data in the binding map.

The mapping form is generic: it names the node class, the input that takes the incoming link and where each item's
fields go. The gateway code knows no node class. It must be deterministic and must fail closed when the edge it names is
missing, as every other binding target does.
"""
import copy

import pytest

from gateway.manifests import ManifestError, bind_backend_request

CHAIN = {
    "node": "7",
    "input": "model",
    "chain": {"node_type": "AdapterLoader", "link": "model", "fields": {"name": "adapter_name", "weight": "strength"}},
}


def graph():
    return {
        "1": {"class_type": "ModelLoader", "inputs": {"model_name": "base.safetensors"}},
        "7": {"class_type": "Sampler", "inputs": {"model": ["1", 0], "steps": 8}},
        "8": {"class_type": "Decode", "inputs": {"samples": ["7", 0]}},
    }


ITEMS = {"type": "object", "properties": {"name": {"type": "string", "required": True},
                                          "weight": {"type": "number", "default": 1.0}}}


def manifest(*targets, items=ITEMS):
    return {
        "id": "demo", "version": "1", "inputs": {"adapters": {"type": "array", "items": items}},
        "bindings": {"graph": {"map": {"adapters": list(targets or [CHAIN])}}},
    }


def bind(items, *targets, template=None):
    return bind_backend_request(manifest(*targets), "graph", template or graph(), {"adapters": items})


def test_two_items_make_a_chain_in_order_and_the_sampler_takes_the_last():
    request = bind([{"name": "a.safetensors", "weight": 0.5}, {"name": "b.safetensors", "weight": -1.25}]).request
    first, second = request["7-model-chain-0"], request["7-model-chain-1"]
    assert first == {"class_type": "AdapterLoader",
                     "inputs": {"model": ["1", 0], "adapter_name": "a.safetensors", "strength": 0.5}}
    assert second == {"class_type": "AdapterLoader",
                      "inputs": {"model": ["7-model-chain-0", 0], "adapter_name": "b.safetensors", "strength": -1.25}}
    assert request["7"]["inputs"]["model"] == ["7-model-chain-1", 0]
    assert request["7"]["inputs"]["steps"] == 8 and request["8"] == graph()["8"]


def test_one_item_and_the_link_it_replaces():
    request = bind([{"name": "a.safetensors", "weight": 1.0}]).request
    assert request["7"]["inputs"]["model"] == ["7-model-chain-0", 0]
    assert request["7-model-chain-0"]["inputs"]["model"] == ["1", 0]


def test_an_empty_list_changes_nothing():
    assert bind([]).request == graph()


def test_the_binding_does_not_change_its_template_and_is_repeatable():
    template = graph()
    before = copy.deepcopy(template)
    items = [{"name": "a.safetensors", "weight": 1.0}, {"name": "b.safetensors", "weight": 2.0}]
    one = bind(items, template=template).request
    assert template == before
    assert one == bind(items, template=template).request


def test_a_missing_node_fails_closed():
    missing = {**CHAIN, "node": "99"}
    with pytest.raises(ManifestError) as caught:
        bind([{"name": "a.safetensors", "weight": 1.0}], missing)
    assert "target_missing" in repr(getattr(caught.value, "errors", caught.value)) or "missing" in str(caught.value)


def test_a_missing_input_fails_closed():
    missing = {**CHAIN, "input": "nope"}
    with pytest.raises(ManifestError):
        bind([{"name": "a.safetensors", "weight": 1.0}], missing)


def test_an_edge_that_holds_no_link_fails_closed():
    template = graph()
    template["7"]["inputs"]["model"] = "not-a-link"
    with pytest.raises(ManifestError):
        bind([{"name": "a.safetensors", "weight": 1.0}], template=template)


def test_an_empty_list_still_fails_closed_on_a_missing_node():
    with pytest.raises(ManifestError):
        bind([], {**CHAIN, "node": "99"})


def test_an_id_already_in_the_graph_fails_closed():
    template = graph()
    template["7-model-chain-0"] = {"class_type": "Other", "inputs": {}}
    with pytest.raises(ManifestError):
        bind([{"name": "a.safetensors", "weight": 1.0}], template=template)


def test_an_item_field_the_mapping_does_not_place_is_refused():
    with pytest.raises(ManifestError):
        bind([{"name": "a.safetensors", "weight": 1.0, "extra": 1}])


def test_a_value_that_is_not_a_list_of_objects_is_refused():
    with pytest.raises(ManifestError):
        bind("a.safetensors")


def test_a_field_left_out_takes_the_default_its_input_declares():
    request = bind([{"name": "a.safetensors"}]).request
    assert request["7-model-chain-0"]["inputs"]["strength"] == 1.0


def test_a_field_left_out_with_no_declared_default_is_refused_not_invented():
    bare = {"type": "object", "properties": {"name": {"type": "string"}, "weight": {"type": "number"}}}
    with pytest.raises(ManifestError):
        bind_backend_request(manifest(items=bare), "graph", graph(), {"adapters": [{"name": "a.safetensors"}]})
