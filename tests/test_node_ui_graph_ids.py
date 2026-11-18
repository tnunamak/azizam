"""Canonical frontend IDs and links use one injective deterministic mapping."""

import pytest

from gateway.engine_protocol.node_ui_graph import (
    _frontend_ids,
    derive_ui_workflow,
    validate_ui_workflow,
)


def test_leading_zero_does_not_alias_canonical_id():
    assert _frontend_ids(dict.fromkeys(["1", "01"])) == {"1": 1, "01": 2}


def test_noncanonical_integer_spellings_get_distinct_positive_ids():
    keys = ["1", " 1", "+1", "-1", "1_0"]
    ids = _frontend_ids(dict.fromkeys(keys))
    assert ids == {"1": 1, " 1": 2, "+1": 3, "-1": 4, "1_0": 5}
    assert len(set(ids.values())) == len(keys)
    assert all(value > 0 for value in ids.values())


def test_readable_ids_follow_highest_canonical_id_in_sorted_order():
    assert _frontend_ids(dict.fromkeys(["s1_text", "3", "s0_text"])) == {
        "3": 3, "s0_text": 4, "s1_text": 5,
    }


def test_mapping_is_independent_of_input_order_and_preserves_canonical_ids():
    keys = ["10", "2", "1", "01", "s1_text", "s0_text"]
    expected = {"10": 10, "2": 2, "1": 1, "01": 11, "s0_text": 12, "s1_text": 13}
    assert _frontend_ids(dict.fromkeys(keys)) == expected
    assert _frontend_ids(dict.fromkeys(reversed(keys))) == expected


def test_zero_empty_and_non_ascii_digits_are_allocated_positive_ids():
    keys = ["0", "00", "", "\u0661", "\uff11", "\u00b2"]
    ids = _frontend_ids(dict.fromkeys(keys))
    assert ids == {key: index for index, key in enumerate(sorted(keys), 1)}
    assert _frontend_ids({}) == {}


def _linked_graph(source_key, target_key):
    return {
        source_key: {"class_type": "Source", "inputs": {}},
        target_key: {"class_type": "Target", "inputs": {"value": [source_key, 0]}},
    }


@pytest.mark.parametrize("source_key,target_key,source_id,target_id", [
    pytest.param("1", "01", 1, 2, id="leading-zero"),
    pytest.param("9007199254740992", "readable", 1, 2, id="oversized"),
    pytest.param("9007199254740990", "a", 9007199254740990, 9007199254740991, id="last-safe"),
])
def test_derived_graph_keeps_leading_zero_node_and_link_distinct(source_key, target_key, source_id, target_id):
    graph = _linked_graph(source_key, target_key)
    definitions = {
        "Source": {"input": {}, "output": ["VALUE"]},
        "Target": {"input": {"required": {"value": ["VALUE", {}]}}, "output": []},
    }
    looked_up = []

    def object_info_for(name):
        looked_up.append(name)
        return definitions.get(name)

    ui = derive_ui_workflow(graph, object_info_for)
    assert ui is not None
    assert looked_up == ["Source", "Target"]
    assert len(ui["nodes"]) == 2
    source, target = ui["nodes"]
    assert source["id"] == source_id
    assert target["id"] == target_id
    assert target["properties"]["api_node_id"] == target_key
    assert ui["links"] == [[1, source_id, 0, target_id, 0, "VALUE"]]
    assert source["outputs"][0]["links"] == [1]
    assert target["inputs"][0]["link"] == 1
    assert ui["last_node_id"] == target_id
    assert ui["last_link_id"] == 1
    assert validate_ui_workflow(graph, ui) == []
    assert derive_ui_workflow(dict(reversed(list(graph.items()))), definitions.get) == ui


@pytest.mark.parametrize("keys,expected", [
    pytest.param(["9007199254740992", "9007199254740993"], {"9007199254740992": 1, "9007199254740993": 2}, id="oversized-pair"),
    pytest.param(["9007199254740991"], {"9007199254740991": 9007199254740991}, id="maximum-safe"),
    pytest.param(["7", "b", "a"], {"7": 7, "a": 8, "b": 9}, id="ordinary"),
    pytest.param(["999999999999999999999999", "c", "b", "a"], {"999999999999999999999999": 1, "a": 2, "b": 3, "c": 4}, id="huge-readable"),
    pytest.param(["9007199254740990", "a"], {"9007199254740990": 9007199254740990, "a": 9007199254740991}, id="last-safe"),
])
def test_frontend_ids_are_exact_javascript_numbers(keys, expected):
    ids = _frontend_ids(dict.fromkeys(keys))
    assert ids == expected
    assert _frontend_ids(dict.fromkeys(reversed(keys))) == expected
    assert len(set(map(float, ids.values()))) == len(keys)
    assert all(1 <= value <= 9007199254740991 and int(float(value)) == value for value in ids.values())


@pytest.mark.parametrize("keys", [
    pytest.param(["9007199254740991", "a", "b"], id="two-readable"),
    pytest.param(["9007199254740991", "readable"], id="one-readable"),
])
def test_frontend_id_overflow_cannot_be_derived(keys):
    assert _frontend_ids(dict.fromkeys(keys)) is None
    template = _linked_graph("source", "target")["source"]
    graph = {key: template for key in keys}
    assert derive_ui_workflow(graph, lambda _: {"input": {}, "output": ["VALUE"]}) is None
    assert validate_ui_workflow(graph, {"nodes": [], "links": []}) == [
        "node ids exceed the frontend safe integer range",
    ]
