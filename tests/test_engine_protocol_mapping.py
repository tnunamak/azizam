"""The fixed mapping language: copy, rename, constants, defaults, enum tables, presence conditionals."""

from __future__ import annotations

import json

import pytest

from gateway.engine_protocol import mapping, pointer
from gateway.engine_protocol.errors import BudgetExceeded, DescriptionError, MappingError


def run(rules, body=None, *, base="empty", params=None, files=None):
    program = mapping.build(rules, base=base, roots=mapping.REQUEST_ROOTS)
    return program.apply({"body": body if body is not None else {}, "params": params or {}, "files": files or {}})


# --- pointers --------------------------------------------------------------------------------


def test_pointer_escapes_and_wildcards() -> None:
    assert pointer.parse_pointer("/a~1b/c~0d") == ("a/b", "c~d")
    assert pointer.parse_pointer("/data/*/id", wildcard=True) == ("data", "*", "id")
    with pytest.raises(ValueError):
        pointer.parse_pointer("/data/*/id")


def test_get_reads_dicts_and_list_items_and_reports_missing() -> None:
    doc = {"a": {"b": [10, {"c": 5}]}}
    assert pointer.get(doc, ("a", "b", "0")) == 10
    assert pointer.get(doc, ("a", "b", "1", "c")) == 5
    for missing in (("a", "x"), ("a", "b", "2"), ("a", "b", "c"), ("a", "b", "0", "z"), ("a", "b", "-1")):
        assert pointer.get(doc, missing) is pointer.MISSING


def test_select_all_walks_every_list_item_in_order_and_is_bounded() -> None:
    doc = {"data": [{"id": "a"}, {"id": "b"}, {"nope": 1}, {"id": "c"}]}
    assert pointer.select_all(doc, ("data", "*", "id"), limit=10) == ["a", "b", "c"]
    with pytest.raises(BudgetExceeded):
        pointer.select_all(doc, ("data", "*", "id"), limit=2)


def test_depth_check_is_iterative() -> None:
    deep: list = []
    node = deep
    for _ in range(5000):
        child: list = []
        node.append(child)
        node = child
    assert pointer.depth_exceeds(deep, 32) is True
    assert pointer.depth_exceeds({"a": [{"b": 1}]}, 3) is False
    assert pointer.depth_exceeds({"a": [{"b": 1}]}, 2) is True


# --- rules -----------------------------------------------------------------------------------


def test_copy_moves_a_value_to_a_new_place_and_skips_an_absent_source() -> None:
    out = run([
        {"copy": {"from": "/body/model", "to": "/engine/name"}},
        {"copy": {"from": "/body/absent", "to": "/never"}},
    ], {"model": "m1"})
    assert out == {"engine": {"name": "m1"}}


def test_copy_copies_containers_so_later_writes_do_not_reach_the_source() -> None:
    body = {"opts": {"a": [1, 2]}}
    out = run([
        {"copy": {"from": "/body/opts", "to": "/opts"}},
        {"set": {"to": "/opts/extra", "value": True}},
    ], body)
    assert out["opts"] == {"a": [1, 2], "extra": True}
    assert body == {"opts": {"a": [1, 2]}}


def test_passthrough_starts_from_a_copy_of_the_body_and_rename_moves_within_it() -> None:
    body = {"model": "m", "max_tokens": 7, "keep": {"x": 1}}
    out = run([{"rename": {"from": "/max_tokens", "to": "/limits/max_new"}}], body, base="passthrough")
    assert out == {"model": "m", "keep": {"x": 1}, "limits": {"max_new": 7}}
    assert body["max_tokens"] == 7


def test_set_writes_a_constant_and_every_call_gets_its_own_copy() -> None:
    program = mapping.build([{"set": {"to": "/opts", "value": {"a": [1]}}}], base="empty", roots=mapping.REQUEST_ROOTS)
    first = program.apply({"body": {}, "params": {}, "files": {}})
    first["opts"]["a"].append(2)
    second = program.apply({"body": {}, "params": {}, "files": {}})
    assert second == {"opts": {"a": [1]}}


def test_default_fills_only_an_absent_place() -> None:
    rules = [{"default": {"to": "/n", "value": 1}}, {"default": {"to": "/stream", "value": False}}]
    assert run(rules, {"n": 4}, base="passthrough") == {"n": 4, "stream": False}
    assert run(rules, {"n": None}, base="passthrough") == {"n": None, "stream": False}


def test_drop_removes_a_place_and_ignores_an_absent_one() -> None:
    assert run([{"drop": {"path": "/a/b"}}, {"drop": {"path": "/zz"}}], {"a": {"b": 1, "c": 2}}, base="passthrough") == {"a": {"c": 2}}


def test_enum_maps_by_table_with_three_unmapped_policies() -> None:
    table = {"alloy": "v-1", "nova": "v-2"}

    def make(unmapped):
        return [{"enum": {"from": "/body/voice", "to": "/speaker", "table": table, "unmapped": unmapped}}]

    assert run(make("pass"), {"voice": "nova"}) == {"speaker": "v-2"}
    assert run(make("pass"), {"voice": "other"}) == {"speaker": "other"}
    assert run(make("drop"), {"voice": "other"}) == {}
    with pytest.raises(MappingError) as raised:
        run(make("error"), {"voice": "other"})
    assert "other" in str(raised.value)
    assert run(make("error"), {}) == {}  # absent is not unmapped


def test_enum_looks_up_json_scalars_by_their_json_spelling() -> None:
    rules = [{"enum": {"from": "/body/v", "to": "/o", "table": {"true": "yes", "1": "one", "null": "none"}, "unmapped": "error"}}]
    assert run(rules, {"v": True}) == {"o": "yes"}
    assert run(rules, {"v": 1}) == {"o": "one"}
    assert run(rules, {"v": None}) == {"o": "none"}


def test_clamp_limits_numbers_and_refuses_other_types() -> None:
    rules = [{"clamp": {"path": "/t", "min": 0, "max": 1}}]
    assert run(rules, {"t": 1.7}, base="passthrough") == {"t": 1}
    assert run(rules, {"t": -3}, base="passthrough") == {"t": 0}
    assert run(rules, {"t": 0.5}, base="passthrough") == {"t": 0.5}
    assert run(rules, {}, base="passthrough") == {}
    with pytest.raises(MappingError):
        run(rules, {"t": "hot"}, base="passthrough")
    with pytest.raises(MappingError):
        run(rules, {"t": True}, base="passthrough")


def test_stringify_puts_compact_json_inside_a_string() -> None:
    out = run([{"stringify": {"from": "/body/params", "to": "/action"}}], {"params": {"b": [1, "é"], "a": None}})
    assert out == {"action": '{"b":[1,"é"],"a":null}'}


def test_when_branches_on_presence_equality_and_membership() -> None:
    def rules(predicate):
        return [{"when": {"if": predicate,
                          "then": [{"set": {"to": "/branch", "value": "then"}}],
                          "else": [{"set": {"to": "/branch", "value": "else"}}]}}]

    assert run(rules({"path": "/body/seed", "present": True}), {"seed": 1})["branch"] == "then"
    assert run(rules({"path": "/body/seed", "present": True}), {})["branch"] == "else"
    assert run(rules({"path": "/body/seed", "present": False}), {})["branch"] == "then"
    assert run(rules({"path": "/body/m", "equals": "x"}), {"m": "x"})["branch"] == "then"
    assert run(rules({"path": "/body/m", "equals": "x"}), {})["branch"] == "else"
    assert run(rules({"path": "/body/m", "in": ["a", "b"]}), {"m": "b"})["branch"] == "then"
    assert run(rules({"path": "/body/m", "in": ["a", "b"]}), {"m": "c"})["branch"] == "else"


def test_equality_keeps_json_booleans_apart_from_numbers() -> None:
    rules = [{"when": {"if": {"path": "/body/s", "equals": True}, "then": [{"set": {"to": "/hit", "value": 1}}]}}]
    assert run(rules, {"s": True}) == {"hit": 1}
    assert run(rules, {"s": 1}) == {}
    rules = [{"when": {"if": {"path": "/body/s", "equals": 1}, "then": [{"set": {"to": "/hit", "value": 1}}]}}]
    assert run(rules, {"s": 1.0}) == {"hit": 1}
    assert run(rules, {"s": True}) == {}


def test_conditions_nest_and_their_rules_run_in_order() -> None:
    rules = [
        {"set": {"to": "/trace", "value": ["start"]}},
        {"when": {"if": {"path": "/body/a", "present": True}, "then": [
            {"when": {"if": {"path": "/body/b", "present": True}, "then": [{"set": {"to": "/both", "value": True}}]}},
        ]}},
    ]
    assert run(rules, {"a": 1, "b": 1}) == {"trace": ["start"], "both": True}
    assert run(rules, {"a": 1}) == {"trace": ["start"]}


def test_a_constant_skeleton_gives_arrays_their_shape_and_pointers_index_into_it() -> None:
    rules = [
        {"set": {"to": "/choices", "value": [{"index": 0, "message": {"role": "assistant", "content": None}}]}},
        {"copy": {"from": "/body/result/text", "to": "/choices/0/message/content"}},
    ]
    assert run(rules, {"result": {"text": "hi"}}) == {"choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}]}


def test_a_pointer_cannot_create_a_list_or_write_past_its_end() -> None:
    with pytest.raises(MappingError):
        run([{"set": {"to": "/choices/0/x", "value": 1}}])
    with pytest.raises(MappingError):
        run([{"set": {"to": "/a", "value": [1]}}, {"set": {"to": "/a/3", "value": 1}}])
    with pytest.raises(MappingError):
        run([{"set": {"to": "/a", "value": 5}}, {"set": {"to": "/a/b", "value": 1}}])


def test_sources_read_list_items_params_and_file_names() -> None:
    out = run([
        {"copy": {"from": "/body/messages/0/content", "to": "/prompt"}},
        {"copy": {"from": "/params/user", "to": "/user"}},
        {"when": {"if": {"path": "/files/audio", "present": True}, "then": [{"set": {"to": "/has_audio", "value": True}}]}},
    ], {"messages": [{"content": "first"}, {"content": "second"}]}, params={"user": "u1"}, files={"audio": object()})
    assert out == {"prompt": "first", "user": "u1", "has_audio": True}


def test_roots_depend_on_where_the_map_runs() -> None:
    mapping.build([{"copy": {"from": "/status", "to": "/s"}}], base="empty", roots=mapping.RESPONSE_ROOTS)
    with pytest.raises(DescriptionError):
        mapping.build([{"copy": {"from": "/status", "to": "/s"}}], base="empty", roots=mapping.REQUEST_ROOTS)


def test_output_is_deterministic() -> None:
    rules = [{"copy": {"from": "/body/a", "to": "/x/a"}}, {"set": {"to": "/x/b", "value": 2}}, {"copy": {"from": "/body/c", "to": "/y"}}]
    body = {"c": [1, 2], "a": "A"}
    assert {json.dumps(run(rules, body)) for _ in range(5)} == {'{"x": {"a": "A", "b": 2}, "y": [1, 2]}'}


def test_the_program_never_changes_its_input() -> None:
    body = {"a": {"b": [1, 2, {"c": 3}]}, "m": "x"}
    snapshot = json.dumps(body)
    run([{"drop": {"path": "/a/b"}}, {"set": {"to": "/m", "value": "y"}}, {"rename": {"from": "/a", "to": "/z"}}], body, base="passthrough")
    assert json.dumps(body) == snapshot


def test_a_body_that_is_not_an_object_cannot_be_the_base() -> None:
    program = mapping.build([], base="passthrough", roots=mapping.REQUEST_ROOTS)
    with pytest.raises(MappingError):
        program.apply({"body": [1], "params": {}, "files": {}})
