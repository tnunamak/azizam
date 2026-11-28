"""A list input can add one node per item on a graph edge (a chain), as data in the binding map.

The mapping form is generic: it names the node class, the input that takes the incoming link and where each item's
fields go. The gateway code knows no node class. It must be deterministic and must fail closed when the edge it names is
missing, as every other binding target does.
"""
import copy

import pytest

from gateway.manifests import ManifestError, ManifestIdentity, bind_backend_request

IDENTITY = ManifestIdentity("demo", "1", "hash", True, "local")

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


def bind(items, *targets, template=None, items_spec=ITEMS):
    return bind_backend_request(manifest(*targets, items=items_spec), "graph", template or graph(), {"adapters": items},
                                identity=IDENTITY)


def refusal_code(*args, **kwargs):
    with pytest.raises(ManifestError) as caught:
        bind(*args, **kwargs)
    return caught.value.code


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
    assert refusal_code([{"name": "a.safetensors", "weight": 1.0}], {**CHAIN, "node": "99"}) == "target_missing"


def test_a_missing_input_fails_closed():
    assert refusal_code([{"name": "a.safetensors", "weight": 1.0}], {**CHAIN, "input": "nope"}) == "target_missing"


def test_an_edge_that_holds_no_link_fails_closed():
    template = graph()
    template["7"]["inputs"]["model"] = "not-a-link"
    assert refusal_code([{"name": "a.safetensors", "weight": 1.0}], template=template) == "chain_edge_not_a_link"


def test_an_empty_list_still_fails_closed_on_a_missing_node():
    assert refusal_code([], {**CHAIN, "node": "99"}) == "target_missing"


def test_an_id_already_in_the_graph_fails_closed():
    template = graph()
    template["7-model-chain-0"] = {"class_type": "Other", "inputs": {}}
    assert refusal_code([{"name": "a.safetensors", "weight": 1.0}], template=template) == "chain_id_taken"


def test_an_item_field_the_mapping_does_not_place_is_refused():
    assert refusal_code([{"name": "a.safetensors", "weight": 1.0, "extra": 1}]) == "chain_item_invalid"


def test_a_value_that_is_not_a_list_of_objects_is_refused():
    assert refusal_code("a.safetensors") == "chain_item_invalid"


def test_a_field_left_out_takes_the_default_its_input_declares():
    request = bind([{"name": "a.safetensors"}]).request
    assert request["7-model-chain-0"]["inputs"]["strength"] == 1.0


def test_a_field_left_out_with_no_declared_default_is_refused_not_invented():
    bare = {"type": "object", "properties": {"name": {"type": "string"}, "weight": {"type": "number"}}}
    assert refusal_code([{"name": "a.safetensors"}], items_spec=bare) == "chain_item_invalid"


# ---- node_type_by_suffix: the loader class follows the chosen file's format ----------------------------------------

ENCODER = {"node": "1", "input": "model_name", "node_type_by_suffix": {".gguf": "GgufModelLoader"}}


def bind_file(value, *targets):
    spec = {"id": "demo", "version": "1", "inputs": {"file": {"type": "string"}},
            "bindings": {"graph": {"map": {"file": list(targets or [ENCODER])}}}}
    return bind_backend_request(spec, "graph", graph(), {"file": value}, identity=IDENTITY)


def test_a_suffix_choice_switches_the_node_class_and_keeps_its_inputs():
    node = bind_file("big-q8_0.gguf").request["1"]
    assert node == {"class_type": "GgufModelLoader", "inputs": {"model_name": "big-q8_0.gguf"}}


def test_a_name_with_no_listed_suffix_keeps_the_graphs_own_class():
    node = bind_file("usual.safetensors").request["1"]
    assert node == {"class_type": "ModelLoader", "inputs": {"model_name": "usual.safetensors"}}


def test_an_empty_choice_keeps_the_graphs_own_class():
    request = bind_file("").request
    assert request["1"]["class_type"] == "ModelLoader"


def test_the_switch_does_not_change_the_template():
    template = graph()
    bind_file("big.gguf")
    assert template == graph()


@pytest.mark.parametrize("bad", [
    {}, [".gguf"], {"gguf": "X"}, {"": "X"}, {".gguf": ""}, {".gguf": 3}, {".gguf": "  "}, "x",
])
def test_a_malformed_node_type_by_suffix_is_refused_with_a_named_code(bad):
    with pytest.raises(ManifestError) as caught:
        bind_file("a.gguf", {**ENCODER, "node_type_by_suffix": bad})
    assert caught.value.code == "node_type_by_suffix_invalid"
