"""Canonical frontend IDs and links use one injective deterministic mapping."""

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


def test_derived_graph_keeps_leading_zero_node_and_link_distinct():
    graph = {
        "1": {"class_type": "Source", "inputs": {}},
        "01": {"class_type": "Target", "inputs": {"value": ["1", 0]}},
    }
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
    assert source["id"] == 1
    assert target["id"] == 2
    assert target["properties"]["api_node_id"] == "01"
    assert ui["links"] == [[1, 1, 0, 2, 0, "VALUE"]]
    assert source["outputs"][0]["links"] == [1]
    assert target["inputs"][0]["link"] == 1
    assert ui["last_node_id"] == 2
    assert ui["last_link_id"] == 1
    assert validate_ui_workflow(graph, ui) == []
    assert derive_ui_workflow(dict(reversed(list(graph.items()))), definitions.get) == ui
