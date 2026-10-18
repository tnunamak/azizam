"""The fixed mapping language: copy, rename, constants, defaults, enum tables, presence conditionals."""

from __future__ import annotations

import json

import pytest

from gateway.engine_protocol import mapping, pointer
from gateway.engine_protocol.budgets import Budgets
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


def test_a_map_without_writes_shares_the_body_and_a_map_with_writes_copies_it() -> None:
    body = {"a": {"b": [1]}, "model": "m"}
    bare = mapping.build([], base="passthrough", roots=mapping.REQUEST_ROOTS)
    assert bare.apply({"body": body, "params": {}, "files": {}}) is body
    writing = mapping.build([{"set": {"to": "/x", "value": 1}}], base="passthrough", roots=mapping.REQUEST_ROOTS)
    out = writing.apply({"body": body, "params": {}, "files": {}})
    assert out is not body and out["a"] is not body["a"] and "x" not in body


def test_a_copy_shares_a_container_only_when_no_rule_writes_below_it() -> None:
    body = {"opts": {"a": [1, 2]}}
    shared = mapping.build([{"copy": {"from": "/body/opts", "to": "/opts"}}], base="empty", roots=mapping.REQUEST_ROOTS)
    assert shared.apply({"body": body, "params": {}, "files": {}})["opts"] is body["opts"]
    for write in (
        {"set": {"to": "/opts/extra", "value": 1}},
        {"drop": {"path": "/opts/a"}},
        {"default": {"to": "/opts/d", "value": 1}},
        {"rename": {"from": "/opts/a", "to": "/opts/b"}},
        {"clamp": {"path": "/opts/n", "min": 0}},
        {"when": {"if": {"path": "/body/opts", "present": True}, "then": [{"set": {"to": "/opts/w", "value": 1}}]}},
    ):
        program = mapping.build([{"copy": {"from": "/body/opts", "to": "/opts"}}, write], base="empty", roots=mapping.REQUEST_ROOTS)
        snapshot = json.dumps(body)
        out = program.apply({"body": body, "params": {}, "files": {}})
        assert out["opts"] is not body["opts"], write
        assert json.dumps(body) == snapshot, write


def test_a_deep_source_is_a_budget_error_inside_the_map_not_a_recursion_error() -> None:
    node: dict = {"x": 1}
    for _ in range(5000):
        node = {"n": node}
    with pytest.raises(BudgetExceeded) as raised:
        run([{"set": {"to": "/flag", "value": True}}], node, base="passthrough")  # a write: the body is cloned
    assert raised.value.budget == "max_json_depth"
    with pytest.raises(BudgetExceeded):
        run([{"copy": {"from": "/body/n", "to": "/n"}}, {"set": {"to": "/n/flag", "value": True}}], node)  # a copy that must own its copy


class _Clock:
    """A clock that moves 0.1 s at every reading, and a count of how many clones the map made."""

    def __init__(self) -> None:
        self.now = 0.0
        self.clones = 0

    def perf_counter(self) -> float:
        self.now += 0.1
        return self.now


@pytest.fixture()
def clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(mapping, "time", clock)
    real = mapping._clone

    def counting(value):
        clock.clones += 1
        return real(value)
    monkeypatch.setattr(mapping, "_clone", counting)
    return clock


def _rules(count: int):
    return [{"set": {"to": f"/k{i}", "value": {"a": [1]}}} for i in range(count)]  # each clones its constant


def test_mapping_s_stops_the_map_while_it_runs_not_after_it(clock) -> None:
    program = mapping.build(_rules(40), base="empty", roots=mapping.REQUEST_ROOTS, budgets=Budgets(mapping_s=0.55))
    with pytest.raises(BudgetExceeded) as raised:
        program.apply({"body": {}, "params": {}, "files": {}})
    assert raised.value.budget == "mapping_s"
    assert clock.clones <= 8  # it did not run the remaining rules


def test_mapping_s_stops_rules_inside_a_condition_too(clock) -> None:
    rules = [{"when": {"if": {"path": "/body/x", "present": True}, "then": _rules(40)}}]
    program = mapping.build(rules, base="empty", roots=mapping.REQUEST_ROOTS, budgets=Budgets(mapping_s=0.55))
    with pytest.raises(BudgetExceeded) as raised:
        program.apply({"body": {"x": 1}, "params": {}, "files": {}})
    assert raised.value.budget == "mapping_s" and clock.clones <= 8


def test_a_map_within_mapping_s_still_runs_every_rule(clock) -> None:
    program = mapping.build(_rules(3), base="empty", roots=mapping.REQUEST_ROOTS, budgets=Budgets(mapping_s=5))
    assert sorted(program.apply({"body": {}, "params": {}, "files": {}})) == ["k0", "k1", "k2"]

def _each(children=None, source="/body/models", target="/data"):
    return {"each": {"from": source, "to": target, "map": children if children is not None else [
        {"copy": {"from": "/item/name", "to": "/id"}},
        {"copy": {"from": "/index", "to": "/index"}},
    ]}}


def test_each_maps_items_indexes_and_existing_roots():
    rules = [_each([
        {"copy": {"from": "/item/name", "to": "/id"}},
        {"copy": {"from": "/index", "to": "/index"}},
        {"copy": {"from": "/params/tag", "to": "/tag"}},
        {"copy": {"from": "/body/version", "to": "/version"}},
    ])]
    assert run(rules, {"models": [{"name": "a"}, {"name": "b"}], "version": 7}, params={"tag": "t"}) == {
        "data": [{"id": "a", "index": 0, "tag": "t", "version": 7},
                 {"id": "b", "index": 1, "tag": "t", "version": 7}]}


def test_each_missing_empty_scalar_items_and_non_list():
    assert run([_each()], {}) == {}
    assert run([_each()], {"models": []}) == {"data": []}
    assert run([_each([{"copy": {"from": "/item", "to": "/value"}}])],
               {"models": [0, False, None, "x"]}) == {"data": [{"value": x} for x in [0, False, None, "x"]]}
    for value in ({}, "abc", 1, True, None):
        with pytest.raises(MappingError):
            run([_each()], {"models": value})


def test_each_nested_scope_and_when_restore_parent_item_and_index():
    children = [
        _each([{"copy": {"from": "/item", "to": "/value"}},
               {"copy": {"from": "/index", "to": "/index"}}], "/item/children", "/children"),
        {"when": {"if": {"path": "/item/name", "present": True},
                  "then": [{"copy": {"from": "/item/name", "to": "/name"}}]}},
        {"copy": {"from": "/index", "to": "/index"}},
    ]
    assert run([_each(children)], {"models": [{"name": "a", "children": [4, 5]}, {"name": "b", "children": [6]}]}) == {
        "data": [{"children": [{"value": 4, "index": 0}, {"value": 5, "index": 1}], "name": "a", "index": 0},
                 {"children": [{"value": 6, "index": 0}], "name": "b", "index": 1}]}


def test_each_iteration_budget_is_cumulative_and_resets_after_failure():
    rules = [_each([], target="/a"), _each([], target="/b")]
    program = mapping.build(rules, base="empty", roots=mapping.REQUEST_ROOTS, budgets=Budgets(max_selected=3))
    with pytest.raises(BudgetExceeded) as raised:
        program.apply({"body": {"models": [1, 2]}})
    assert raised.value.budget == "max_selected"
    assert program.apply({"body": {"models": [1]}}) == {"a": [{}], "b": [{}]}
    nested = mapping.build([_each([_each([], "/item", "/children")])],
                           base="empty", roots=mapping.REQUEST_ROOTS, budgets=Budgets(max_selected=3))
    with pytest.raises(BudgetExceeded) as raised:
        nested.apply({"body": {"models": [[1, 2, 3]]}})
    assert raised.value.budget == "max_selected"


def test_each_combines_when_depth_and_total_rule_budgets():
    wrapped = {"when": {"if": {"path": "/body/models", "present": True}, "then": [_each()]}}
    with pytest.raises(DescriptionError):
        mapping.build([wrapped], base="empty", roots=mapping.REQUEST_ROOTS, budgets=Budgets(max_when_depth=1))
    mapping.build([wrapped], base="empty", roots=mapping.REQUEST_ROOTS, budgets=Budgets(max_when_depth=2))
    with pytest.raises(DescriptionError):
        mapping.build([_each()], base="empty", roots=mapping.REQUEST_ROOTS, budgets=Budgets(max_rules=2))
    mapping.build([_each()], base="empty", roots=mapping.REQUEST_ROOTS, budgets=Budgets(max_rules=3))


@pytest.mark.parametrize("rule", [
    {"each": {"from": "/body/models", "to": "/data"}},
    {"each": {"from": "/body/models", "to": "/data", "map": [], "filter": True}},
    {"each": {"from": "/body/models", "to": "/data", "map": {}}},
    {"each": {"from": "/item", "to": "/data", "map": []}},
    {"each": {"from": "/body/models/*", "to": "/data", "map": []}},
    {"copy": {"from": "/index", "to": "/index"}},
    {"copy": {"from": "/item", "to": "/item"}},
])
def test_each_closed_grammar_and_lexical_roots(rule):
    with pytest.raises(DescriptionError):
        mapping.build([rule], base="empty", roots=mapping.REQUEST_ROOTS)


def test_each_never_mutates_source_through_nested_or_later_writes():
    body = {"models": [{"name": "a", "opts": {"x": [1]}}]}
    before = json.dumps(body)
    rules = [_each([
        {"copy": {"from": "/item/opts", "to": "/opts"}},
        {"set": {"to": "/opts/flag", "value": True}},
        {"copy": {"from": "/item", "to": "/original"}},
    ]), {"set": {"to": "/data/0/original/name", "value": "changed"}}]
    assert run(rules, body)["data"][0]["original"]["name"] == "changed"
    assert json.dumps(body) == before
    # The result's writable array/object graph belongs to this invocation.
    out = run([_each([{"copy": {"from": "/item", "to": "/value"}}])], body)
    out["data"][0]["value"]["opts"]["x"].append(2)
    assert json.dumps(body) == before


def test_each_deadline_stops_empty_maps_and_nested_rules(clock):
    program = mapping.build([_each([])], base="empty", roots=mapping.REQUEST_ROOTS, budgets=Budgets(mapping_s=0.55))
    with pytest.raises(BudgetExceeded) as raised:
        program.apply({"body": {"models": list(range(100))}})
    assert raised.value.budget == "mapping_s"
    assert clock.now < 2
    program = mapping.build([_each(_rules(40))], base="empty", roots=mapping.REQUEST_ROOTS, budgets=Budgets(mapping_s=0.55))
    with pytest.raises(BudgetExceeded) as raised:
        program.apply({"body": {"models": [1]}})
    assert raised.value.budget == "mapping_s" and clock.clones <= 8


def test_each_checks_json_depth_for_source_and_assembled_output():
    program = mapping.build([_each([{"copy": {"from": "/item", "to": "/a/b"}}])],
                            base="empty", roots=mapping.REQUEST_ROOTS, budgets=Budgets(max_json_depth=3))
    with pytest.raises(BudgetExceeded) as raised:
        program.apply({"body": {"models": [1]}})
    assert raised.value.budget == "max_json_depth"
    deep = {"models": [{"a": {"b": {"c": 1}}}]}
    with pytest.raises(BudgetExceeded) as raised:
        program.apply({"body": deep})
    assert raised.value.budget == "max_json_depth"


@pytest.mark.parametrize("roots,name", [
    (mapping.REQUEST_ROOTS, "max_request_bytes"),
    (mapping.RESPONSE_ROOTS, "max_response_bytes"),
])
def test_each_output_byte_budget_utf8_boundary_and_envelope(roots, name):
    expected = {"data": [{"value": "é"}]}
    size = len(json.dumps(expected, ensure_ascii=False, separators=(",", ":")).encode())
    rules = [_each([{"copy": {"from": "/item", "to": "/value"}}])]
    source = {"body": {"models": ["é"]}}
    assert mapping.build(rules, base="empty", roots=roots,
                         budgets=Budgets(**{name: size})).apply(source) == expected
    with pytest.raises(BudgetExceeded) as raised:
        mapping.build(rules, base="empty", roots=roots,
                      budgets=Budgets(**{name: size - 1})).apply(source)
    assert raised.value.budget == name


def test_each_stops_growth_early_without_serializing_accumulating_list(monkeypatch):
    calls = []
    real_measure = mapping._JsonFacts.measure

    def measured(facts, value, level=1):
        # Count fresh mapped objects, excluding the source accounting pass and
        # cache hits when the final envelope reuses completed item facts.
        if type(value) is dict and ("v" in value or "data" in value) and id(value) not in facts.nodes:
            calls.append(value)
        return real_measure(facts, value, level)

    monkeypatch.setattr(mapping._JsonFacts, "measure", measured)
    rules = [_each([{"set": {"to": "/v", "value": "x" * 20}}])]
    program = mapping.build(rules, base="empty", roots=mapping.REQUEST_ROOTS,
                            budgets=Budgets(max_request_bytes=100))
    with pytest.raises(BudgetExceeded) as raised:
        program.apply({"body": {"models": list(range(100))}})
    assert raised.value.budget == "max_request_bytes"
    assert len(calls) <= 4
    assert all(type(value) is dict and "v" in value for value in calls)
    calls.clear()
    program = mapping.build(rules, base="empty", roots=mapping.REQUEST_ROOTS)
    assert len(program.apply({"body": {"models": list(range(100))}})["data"]) == 100
    assert len(calls) == 101  # each element measured once, then the final envelope


def test_each_owns_existing_root_values_and_nested_siblings():
    source = {"body": {"models": [1, 2]}, "params": {"opts": {"x": []}}}
    program = mapping.build([_each([{"copy": {"from": "/params/opts", "to": "/opts"}}])],
                            base="empty", roots=mapping.REQUEST_ROOTS)
    out = program.apply(source)
    out["data"][0]["opts"]["x"].append(1)
    assert source["params"]["opts"]["x"] == []
    assert out["data"][1]["opts"]["x"] == []


def test_each_rejects_when_nested_beyond_shared_depth():
    rule = _each([{"when": {"if": {"path": "/item", "present": True}, "then": []}}])
    with pytest.raises(DescriptionError):
        mapping.build([rule], base="empty", roots=mapping.REQUEST_ROOTS, budgets=Budgets(max_when_depth=1))
