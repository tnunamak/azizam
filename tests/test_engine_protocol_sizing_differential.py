"""Behavior oracle for exact each sizing.

Frozen reference: origin/main commit 45cc9bb535d3ff87df02b675db670e60a0962f08,
Git blob 326ecd2c203b100e02eec56c34a606173729ae35. The fixture is byte-for-byte
that revision's gateway/engine_protocol/mapping.py; do not regenerate it from
current production code.

Corpus mode: scripts/run-hermetic-pytest.sh
-p tests.test_engine_protocol_sizing_differential --mapping-differential
 tests/test_engine_protocol*.py
Use the repository's hermetic runner for pytest. Corpus mode compares both
compilers and every mapped input without replacing the program or its outputs,
so identity assertions in the original tests retain their meaning.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given, settings, strategies as st

from gateway.engine_protocol import mapping
from gateway.engine_protocol.budgets import Budgets
from gateway.engine_protocol.errors import ProtocolError

_REFERENCE_PATH = Path(__file__).parent / "fixtures/engine_protocol/mapping_reference.py"
_spec = importlib.util.spec_from_file_location("_engine_mapping_reference", _REFERENCE_PATH)
reference = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reference)
_CLOCK = SimpleNamespace(perf_counter=lambda: 0.0)


def wire(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def capture(call):
    try:
        return ("ok", call())
    except ProtocolError as error:
        return ("error", type(error), error.code, getattr(error, "budget", None), str(error))


def assert_same(actual, expected):
    assert actual[0] == expected[0], (actual, expected)
    if actual[0] == "error":
        assert actual == expected
    else:
        try:
            actual_wire, expected_wire = wire(actual[1]), wire(expected[1])
        except (TypeError, ValueError, RecursionError):
            # The executor can map opaque upload/asset objects before framing.
            assert actual[1] == expected[1]
        else:
            assert actual_wire == expected_wire


def compare(rules, source, *, base="empty", roots=mapping.REQUEST_ROOTS, budgets=Budgets()):
    before = copy.deepcopy(source)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(mapping, "time", _CLOCK)
        patch.setattr(reference, "time", _CLOCK)
        actual = capture(lambda: mapping.build(copy.deepcopy(rules), base=base, roots=roots,
                                              budgets=budgets).apply(source))
        expected = capture(lambda: reference.build(copy.deepcopy(rules), base=base, roots=roots,
                                                  budgets=budgets).apply(copy.deepcopy(before)))
    assert_same(actual, expected)
    assert source == before
    return actual


def each(children, source="/body/items", target="/data"):
    return {"each": {"from": source, "to": target, "map": children}}


def test_reference_blob_provenance():
    data = _REFERENCE_PATH.read_bytes()
    assert hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() == "326ecd2c203b100e02eec56c34a606173729ae35"


text = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=12)
scalar = st.one_of(st.none(), st.booleans(), st.integers(-10**12, 10**12),
                   st.floats(allow_nan=False, allow_infinity=False), text)
json_values = st.recursive(scalar, lambda child: st.one_of(st.lists(child, max_size=3),
                           st.dictionaries(text, child, max_size=3)), max_leaves=12)


@settings(max_examples=100, deadline=None, derandomize=True)
@given(value=json_values, key=text, base=st.sampled_from(["empty", "passthrough"]),
       limit=st.integers(0, 700), depth=st.integers(1, 10))
def test_all_rules_with_unicode_escaped_strings_and_exact_budgets(value, key, base, limit, depth):
    escaped_key = key.replace("~", "~0").replace("/", "~1")
    children = [
        {"copy": {"from": "/item", "to": "/copied"}},
        {"set": {"to": "/numeric", "value": [0, 1]}},
        {"set": {"to": "/numeric/0", "value": -0.0}},
        {"rename": {"from": "/numeric/1", "to": "/renamed"}},
        {"default": {"to": "/default", "value": value}},
        {"set": {"to": "/temporary", "value": True}},
        {"drop": {"path": "/temporary"}},
        {"enum": {"from": "/item", "to": "/enum", "table": {"yes": 1}, "unmapped": "pass"}},
        {"set": {"to": "/number", "value": 1.25e20}},
        {"clamp": {"path": "/number", "min": -1.5, "max": 3.75}},
        {"stringify": {"from": "/item", "to": "/" + escaped_key}},
        {"when": {"if": {"path": "/index", "in": [0, 2]},
                  "then": [{"set": {"to": "/branch", "value": "\"\\\n\té😀"}}],
                  "else": [{"default": {"to": "/branch", "value": False}}]}},
    ]
    compare([each(children)], {"body": {"items": [value, value]}, "params": {}, "files": {}},
            base=base, budgets=Budgets(max_request_bytes=limit, max_json_depth=depth))


@settings(max_examples=80, deadline=None, derandomize=True)
@given(rows=st.lists(st.lists(json_values, max_size=3), max_size=3),
       selected=st.integers(0, 15), limit=st.integers(0, 600),
       policy=st.sampled_from(["pass", "drop", "error"]))
def test_nested_each_when_cumulative_selection_and_enum_refusals(rows, selected, limit, policy):
    inner = each([{"enum": {"from": "/item", "to": "/v", "table": {"null": None}, "unmapped": policy}},
                  {"copy": {"from": "/index", "to": "/index"}}], "/item", "/children")
    rules = [each([{"when": {"if": {"path": "/item", "present": True}, "then": [inner]}}]),
             each([], target="/second")]
    compare(rules, {"body": {"items": rows}},
            budgets=Budgets(max_selected=selected, max_request_bytes=limit))


@pytest.mark.parametrize("write", [
    {"set": {"to": "/copy/n/0", "value": "larger😀"}},
    {"default": {"to": "/copy/new", "value": [1, 2]}},
    {"drop": {"path": "/copy/n"}},
    {"rename": {"from": "/copy/n", "to": "/copy/moved"}},
])
@pytest.mark.parametrize("limit", [20, 50, 1000])
def test_copy_metadata_survives_mutation_of_independent_copies(write, limit):
    children = [{"copy": {"from": "/item", "to": "/copy"}}, write,
                {"copy": {"from": "/item", "to": "/original"}}]
    compare([each(children)], {"body": {"items": [{"n": ["é"]}] * 2}},
            budgets=Budgets(max_request_bytes=limit))


@pytest.mark.parametrize("roots,name", [(mapping.REQUEST_ROOTS, "max_request_bytes"),
                                       (mapping.RESPONSE_ROOTS, "max_response_bytes")])
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_exact_utf8_output_and_depth_boundaries(roots, name, offset):
    rules = [each([{"copy": {"from": "/item", "to": "/é~0/~1"}}], target="/😀")]
    source = {"body": {"items": ["\"\\\n\té😀", -0.0, 1.234e-100]}}
    expected = {"😀": [{"é~": {"/": value}} for value in source["body"]["items"]]}
    result = compare(rules, source, roots=roots, budgets=Budgets(**{name: len(wire(expected)) + offset}))
    assert result[0] == ("error" if offset == -1 else "ok")
    result = compare(rules, source, roots=roots, budgets=Budgets(max_json_depth=4 + offset))
    assert result[0] == ("error" if offset == -1 else "ok")


def test_oversized_intermediate_each_cannot_be_hidden_by_later_overwrite():
    rules = [each([{"set": {"to": "/v", "value": "x" * 100}}]),
             {"set": {"to": "/data", "value": []}}]
    result = compare(rules, {"body": {"items": [0]}}, budgets=Budgets(max_request_bytes=30))
    assert result[0] == "error" and result[3] == "max_request_bytes"


# These tests deliberately inspect implementation calls or simulate elapsed time;
# a constant clock and a second evaluator would invalidate their assertions.
_CORPUS_EXCLUSIONS = {
    "test_stringify_and_enum_do_not_serialize_unbounded_containers": "serialization-call instrumentation",
    "test_chunked_string_accounting_bounds_deadline_overshoot": "elapsed-work clock instrumentation",
    "test_each_all_runtime_budgets_refuse_before_unbounded_growth": "includes a real-time deadline test",
    "test_large_integer_sizing_polls_between_expensive_values": "elapsed-work clock instrumentation",
    "test_mapping_s_stops_the_map_while_it_runs_not_after_it": "clone-count clock instrumentation",
    "test_mapping_s_stops_rules_inside_a_condition_too": "clone-count clock instrumentation",
    "test_a_map_within_mapping_s_still_runs_every_rule": "clone-count clock instrumentation",
    "test_each_deadline_stops_empty_maps_and_nested_rules": "clone-count clock instrumentation",
    "test_each_stops_growth_early_without_serializing_accumulating_list": "serialization-call instrumentation",
    "test_executor_overhead_stays_near_a_hand_written_mapping": "performance benchmark; replay doubles work",
    "test_each_does_not_reserialize_copied_vectors": "serialization-call instrumentation",
    "test_each_batches_clock_reads": "clock-call instrumentation",
}


def pytest_terminal_summary(terminalreporter):
    counts = getattr(terminalreporter.config, "_mapping_differential_counts", None)
    if counts is not None:
        terminalreporter.write_line(
            f"mapping differential: {counts['compiled']} compiles, {counts['applied']} applies, "
            f"{counts['excluded']} timing/instrumentation cases excluded")


def pytest_addoption(parser):
    parser.addoption("--mapping-differential", action="store_true", default=False,
                     help="compare protocol corpus compilation and application with frozen mapper")


@pytest.fixture(autouse=True)
def _mapping_corpus_oracle(request, monkeypatch):
    if not request.config.getoption("mapping_differential", default=False):
        return
    if not request.node.path.name.startswith("test_engine_protocol") or request.node.path.name == Path(__file__).name:
        return
    counts = getattr(request.config, "_mapping_differential_counts", None)
    if counts is None:
        counts = {"compiled": 0, "applied": 0, "excluded": 0}
        request.config._mapping_differential_counts = counts
    if request.node.originalname in _CORPUS_EXCLUSIONS:
        counts["excluded"] += 1
        request.node.user_properties.append(("mapping_differential_exclusion", _CORPUS_EXCLUSIONS[request.node.originalname]))
        return
    original_compile, original_apply = mapping.compile_program, mapping.Program.apply
    programs = {}

    def compile_both(rules, **kwargs):
        counts["compiled"] += 1
        old_kwargs = {**kwargs, "problems": list(kwargs["problems"])}
        old = reference.compile_program(copy.deepcopy(rules), **old_kwargs)
        new = original_compile(rules, **kwargs)
        assert kwargs["problems"] == old_kwargs["problems"]
        assert (old is None) == (new is None)
        if new is not None:
            programs[id(new)] = old
        return new

    def apply_both(program, source):
        old = programs.get(id(program))
        if old is None:
            return original_apply(program, source)
        counts["applied"] += 1
        try:
            before_wire = wire(source)
        except (TypeError, ValueError, RecursionError):
            before_wire = None
        try:
            snapshot = reference._clone(source)
        except RecursionError:
            # Very deep input is itself a refusal test; cloning cannot traverse it.
            snapshot = source
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(mapping, "time", _CLOCK)
            patch.setattr(reference, "time", _CLOCK)
            expected = capture(lambda: old.apply(snapshot))
            failure = None
            try:
                output = original_apply(program, source)
                actual = ("ok", output)
            except ProtocolError as error:
                failure = error
                actual = ("error", type(error), error.code, getattr(error, "budget", None), str(error))
        assert_same(actual, expected)
        if before_wire is not None:
            assert wire(source) == before_wire, "mapping changed its source"
        if failure is not None:
            raise failure
        return output

    monkeypatch.setattr(mapping, "compile_program", compile_both)
    monkeypatch.setattr(mapping.Program, "apply", apply_both)


def test_opaque_tuple_stringify_keeps_native_encoder_compatibility():
    value = 0
    for _ in range(990):
        value = (value,)
    # Avoid deepcopy's own earlier recursion limit: invoke the frozen and current
    # programs on the same immutable tuple graph under the same constant clock.
    rules = [each([{'stringify': {'from': '/item', 'to': '/v'}}])]
    source = {'body': {'items': [value]}}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(mapping, 'time', _CLOCK)
        patch.setattr(reference, 'time', _CLOCK)
        kwargs = dict(base='empty', roots=mapping.RESPONSE_ROOTS)
        assert_same(capture(lambda: mapping.build(rules, **kwargs).apply(source)),
                    capture(lambda: reference.build(rules, **kwargs).apply(source)))
