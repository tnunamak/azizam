"""The contract error type (CODE_TO_DATA_S3, S3.1, D8): every refusal has a path and a code, a check gathers all its errors,
and ``str()`` of the raised error is still the message the check raised first before the type existed."""

from __future__ import annotations

import pytest

from gateway import output_selectors as sel
from gateway.manifests import (
    MAX_CONTRACT_ERRORS,
    ContractError,
    ContractErrors,
    ManifestError,
    choice_key,
    validate_manifest,
)
from tests.contract_error_corpus import (
    BAD_INPUT,
    CASES,
    EXPECTED_ERRORS,
    FIRST_MESSAGES,
    base_manifest,
    check_manifest,
    descriptor,
    edited,
)

RAISED_BEFORE = {"ManifestError": ManifestError, "SelectorError": sel.SelectorError}


def raised(case: str) -> ContractErrors:
    with pytest.raises(ContractErrors) as caught:
        CASES[case]()
    return caught.value


def listed(error: ContractErrors) -> list[tuple[tuple[object, ...], str]]:
    return [(item.path, item.code) for item in error.errors]


def test_the_corpus_and_its_oracle_cover_the_same_cases():
    assert set(CASES) == set(FIRST_MESSAGES) == set(EXPECTED_ERRORS)


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_first_message_and_the_class_are_what_they_were(case):
    old_class, old_message = FIRST_MESSAGES[case]
    error = raised(case)
    assert str(error) == old_message
    assert isinstance(error, RAISED_BEFORE[old_class])


@pytest.mark.parametrize("case", sorted(CASES))
def test_every_error_has_a_path_and_a_code_and_the_list_is_the_expected_one(case):
    error = raised(case)
    assert listed(error) == EXPECTED_ERRORS[case]
    assert all(item.code and item.message for item in error.errors)
    assert error.primary in error.errors and error.primary.message == str(error) and error.code == error.primary.code


def test_a_document_with_a_bad_input_and_a_bad_binding_keeps_the_inputs_message_first():
    error = raised("multi-input-and-binding")
    assert str(error) == "input prompt label must be a non-empty string"
    assert error.primary.path == ("inputs", "prompt", "label")
    # Both are listed, sorted by path: the binding sorts before the input, so the primary is not the first entry.
    assert listed(error) == [
        (("bindings", "eng", "edge_only_inputs"), "reference_unknown"),
        (("inputs", "prompt", "label"), "value_empty"),
    ]
    assert error.errors[0] is not error.primary


def test_a_check_gathers_every_error_it_finds_and_does_not_stop_at_the_first():
    error = raised("multi-input-binding-selector")
    assert sorted(item.code for item in error.errors) == ["reference_unknown", "selector_malformed", "value_empty"]


def test_the_list_is_capped_with_a_last_entry_that_says_so():
    manifest = base_manifest()
    del manifest["bindings"]["eng"]["selectors"]
    manifest["inputs"] = {f"in{index:03d}": {"type": "string", "label": ""} for index in range(150)}
    with pytest.raises(ContractErrors) as caught:
        validate_manifest(manifest)
    error = caught.value
    assert len(error.errors) == MAX_CONTRACT_ERRORS + 1
    assert error.errors[-1].code == "too_many_errors" and error.errors[-1].message == "50 more errors are not listed"
    assert str(error) == "input in000 label must be a non-empty string"
    assert [item.code for item in error.errors[:-1]] == ["value_empty"] * MAX_CONTRACT_ERRORS


def test_paths_sort_with_indexes_by_number_and_a_pointer_escapes_its_parts():
    items = [ContractError(("a", 10), "c", "m"), ContractError(("a", 2), "c", "m"), ContractError(("a", "b/c~"), "c", "m")]
    error = ContractErrors(items)
    assert [item.path for item in error.errors] == [("a", 2), ("a", 10), ("a", "b/c~")]
    assert error.errors[2].pointer == "/a/b~1c~0"
    assert error.errors[0].as_dict() == {"path": ["a", 2], "pointer": "/a/2", "code": "c", "message": "m"}


def test_a_message_names_keys_and_not_the_values_of_fields():
    manifest = edited(BAD_INPUT)
    manifest["inputs"]["prompt"]["default"] = "do-not-echo-this-value"
    error = raised_from(check_manifest(manifest))
    assert "do-not-echo-this-value" not in " ".join(item.message for item in error.errors)


def raised_from(call) -> ContractErrors:
    with pytest.raises(ContractErrors) as caught:
        call()
    return caught.value


def test_a_caller_that_catches_the_old_classes_still_catches():
    with pytest.raises(sel.SelectorError) as caught:
        sel.check_binding(descriptor(), {"first": {}}, {"selectors": {"first": {"node": "9"}}})
    assert caught.value.code == "selector_malformed" and len(caught.value.errors) == 2
    assert [item.path for item in caught.value.errors] == [
        ("selectors", "first", "from"), ("selectors", "first", "key"),
    ]


def test_a_crash_after_a_collected_error_leaves_that_error_and_a_crash_alone_is_raised():
    from gateway.manifests import ErrorCollector

    errors = ErrorCollector()
    with errors.stop_at_a_crash_after_an_error():
        errors.add(("inputs", "a"), "type_mismatch", "input a is broken")
        set([[]])  # the effect of the broken input
    assert [item.code for item in errors.errors] == ["type_mismatch"]

    with pytest.raises(TypeError):
        with ErrorCollector().stop_at_a_crash_after_an_error():
            set([[]])


@pytest.fixture
def choices_manifest(monkeypatch):
    from gateway import binding_descriptors

    # Only the unrelated adapter registry is synthetic; both contract layers are real.
    descriptor = binding_descriptors.parse_binding_descriptor(
        binding="fixture", executor="adapter:fixture",
        binding_fields={"label": {"kind": "text"}},
    )
    monkeypatch.setattr(binding_descriptors, "cached_shipped_binding_descriptors",
                        lambda: {"fixture": descriptor})
    return {
        "schema_version": 2, "id": "fixture.choices", "version": "1",
        "intent": "fixture.choices", "capability": "fixture",
        "inputs": {"choice": {"type": "string", "enum": ["fallback"]}},
        "outputs": {"result": {"media_type": "text/plain"}},
        "bindings": {"fixture": {}},
    }


@pytest.mark.parametrize("declaration", [
    None, False, 7, "private-value", [], {},
    {"node_type": "private-node"},
    {"input": "private-input"},
    {"node_type": "private-node", "input": "private-input", "extra": "private-extra"},
    {"node_type": "", "input": "private-input"},
    {"node_type": "private-node", "input": ""},
    {"node_type": 7, "input": "private-input"},
    {"node_type": "private-node", "input": False},
    {"node_type": [], "input": "private-input"},
    {"node_type": "private-node", "input": {}},
], ids=["null", "bool", "number", "text", "list", "empty", "missing-input", "missing-node",
        "extra", "empty-node", "empty-input", "numeric-node", "boolean-input", "list-node", "object-input"])
def test_choices_from_invalid_shape(choices_manifest, declaration):
    from gateway.manifests import enforce_contract

    choices_manifest["inputs"]["choice"]["choices_from"] = declaration
    with pytest.raises(ContractErrors) as caught:
        enforce_contract(choices_manifest)
    assert listed(caught.value) == [(("inputs", "choice", "choices_from"), "choices_from_invalid")]
    message = str(caught.value)
    assert all(key in message for key in ("choices_from", "node_type", "input", "enum"))
    assert "private-" not in message


@pytest.mark.parametrize("spec", [
    {"type": kind, "enum": ["fallback"]}
    for kind in ("integer", "number", "boolean", "asset", "object", "array")
] + [
    {"type": "string"}, {"type": "string", "enum": []},
    {"type": "string", "enum": "private-enum"}, {"type": "string", "enum": None},
], ids=["integer", "number", "boolean", "asset", "object", "array",
        "missing-enum", "empty-enum", "text-enum", "null-enum"])
def test_choices_from_requires_string_enum(choices_manifest, spec):
    from gateway.manifests import enforce_contract

    choices_manifest["inputs"]["choice"] = {
        **spec, "choices_from": {"node_type": "private-node", "input": "private-input"},
    }
    with pytest.raises(ContractErrors) as caught:
        enforce_contract(choices_manifest)
    assert listed(caught.value) == [(("inputs", "choice", "choices_from"), "choices_from_invalid")]
    assert "private-" not in str(caught.value)


def test_choices_from_passes_full_contract_and_keeps_fallback(choices_manifest):
    from copy import deepcopy
    from gateway.manifests import enforce_contract

    original = deepcopy(choices_manifest)
    assert enforce_contract(choices_manifest) is None
    choices_manifest["inputs"]["choice"]["choices_from"] = {"node_type": "FixtureNode", "input": "selection"}
    declared = deepcopy(choices_manifest)
    assert enforce_contract(choices_manifest) is None
    assert choices_manifest == declared
    assert choices_manifest["inputs"]["choice"]["enum"] == ["fallback"]
    del choices_manifest["inputs"]["choice"]["choices_from"]
    assert enforce_contract(choices_manifest) is None
    assert choices_manifest == original


@pytest.mark.parametrize("declared, choices, allowed", [
    pytest.param(True, {"choice": ["3", "5"], "plain": ["5"]}, ["3", "5"], id="snapshot"),
    pytest.param(True, None, ["2", "3", "4", "6"], id="none"),
    pytest.param(True, {"other": ["3", "5"]}, ["2", "3", "4", "6"], id="other-name"),
    pytest.param(False, {"choice": ["3", "5"]}, ["2", "3", "4", "6"], id="undeclared"),
    pytest.param(True, {"choice": []}, ["2", "3", "4", "6"], id="empty"),
    pytest.param(True, {"choice": None}, ["2", "3", "4", "6"], id="null"),
    pytest.param(True, {"choice": "35"}, ["2", "3", "4", "6"], id="text"),
    pytest.param(True, {"choice": ["3", 5]}, ["2", "3", "4", "6"], id="non-string"),
])
def test_validate_inputs_choice_snapshot(choices_manifest, declared, choices, allowed):
    from copy import deepcopy
    from gateway.manifests import validate_inputs

    spec = {"type": "string", "enum": ["2", "3", "4", "6"], "max_length": 1}
    if declared:
        spec["choices_from"] = {"node_type": "FixtureNode", "input": "selection"}
    choices_manifest["inputs"] = {
        "choice": spec, "plain": {"type": "string", "enum": ["2"]},
    }
    original, snapshot = deepcopy(choices_manifest), deepcopy(choices)
    for value in ["2", "3", "4", "5", "6"]:
        result = validate_inputs(choices_manifest, {"choice": value, "plain": "2"}, choices=choices)
        if value in allowed:
            assert result.accepted == {"choice": value, "plain": "2"}
            assert result.rejected == {} and result.errors == []
        else:
            message = "choice must be one of: " + ", ".join(allowed)
            assert result.accepted == {"plain": "2"}
            assert result.rejected == {"choice": message}
            assert result.errors == [ContractError(("inputs", "choice"), "value_invalid", message)]
    # An undeclared sibling never borrows the snapshot, even when explicitly named.
    result = validate_inputs(choices_manifest, {"plain": "5"}, choices=choices)
    assert result.rejected == {"plain": "plain must be one of: 2"}
    assert result.errors == [ContractError(("inputs", "plain"), "value_invalid", "plain must be one of: 2")]
    # Type checking still precedes the enum override.
    result = validate_inputs(choices_manifest, {"choice": 3}, choices=choices)
    assert result.rejected == {"choice": "choice must be a string"}
    assert result.errors == [ContractError(("inputs", "choice"), "value_invalid", "choice must be a string")]
    assert choices_manifest == original and choices == snapshot
    # Omitted keyword retains the existing caller behavior, even after a snapshot call.
    assert validate_inputs(choices_manifest, {"choice": "2"}).accepted == {"choice": "2"}
    assert validate_inputs(choices_manifest, {"choice": "5"}).rejected == {
        "choice": "choice must be one of: 2, 3, 4, 6",
    }
    if declared:
        result = validate_inputs(choices_manifest, {"choice": "55"}, choices={"choice": ["55"]})
        assert result.rejected == {"choice": "choice must have at most 1 characters"}
        assert choices_manifest == original


def test_choice_labels_name_only_the_inputs_own_choices():
    def with_mode(**extra):
        doc = base_manifest()
        doc["inputs"]["mode"] = {"type": "string", "enum": ["auto", "sync"], **extra}
        return doc

    # Names for some or all of the choices are accepted; a choice without one shows as its value.
    validate_manifest(with_mode(choice_labels={"sync": "Wait for the result"}), {"eng": descriptor()})
    for bad in (
        with_mode(choice_labels={"later": "Later"}),  # not one of the choices
        with_mode(choice_labels={"auto": " "}),  # an empty name
        with_mode(choice_labels=["auto"]),  # not a map
    ):
        with pytest.raises(ContractErrors) as caught:
            validate_manifest(bad, {"eng": descriptor()})
        assert listed(caught.value) == [(("inputs", "mode", "choice_labels"), "choice_labels_invalid")]
    no_enum = base_manifest()
    no_enum["inputs"]["mode"] = {"type": "string", "choice_labels": {"auto": "Automatic"}}
    with pytest.raises(ContractErrors) as caught:
        validate_manifest(no_enum, {"eng": descriptor()})
    assert listed(caught.value) == [(("inputs", "mode", "choice_labels"), "choice_labels_invalid")]


def test_choice_label_keys_are_the_text_the_browser_gives_each_choice():
    # The web page looks a name up by String(value): 2.0 is "2" there, true is "true", 1e-07 is "1e-7".
    assert [choice_key(v) for v in ("auto", 512, 2.0, 2.5, True)] == ["auto", "512", "2", "2.5", "true"]
    assert choice_key(1e-07) is None  # an exponent form the two languages write differently is not nameable

    def with_scale(enum, labels):
        doc = base_manifest()
        doc["inputs"]["scale"] = {"type": "number", "enum": enum, "choice_labels": labels}
        return doc

    validate_manifest(with_scale([1.0, 2.5], {"1": "Same size", "2.5": "Two and a half times"}), {"eng": descriptor()})
    for enum, labels in (([1.0], {"1.0": "Same size"}), ([1e-07], {"1e-07": "Tiny"})):
        with pytest.raises(ContractErrors) as caught:
            validate_manifest(with_scale(enum, labels), {"eng": descriptor()})
        assert listed(caught.value) == [(("inputs", "scale", "choice_labels"), "choice_labels_invalid")]
