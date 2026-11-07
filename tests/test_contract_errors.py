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
    validate_manifest,
)
from gateway.mesh_manifest import MeshManifestError
from tests.contract_error_corpus import (
    BAD_BINDING,
    BAD_INPUT,
    CASES,
    EXPECTED_ERRORS,
    FIRST_MESSAGES,
    base_manifest,
    check_manifest,
    descriptor,
    edited,
)

RAISED_BEFORE = {"ManifestError": ManifestError, "SelectorError": sel.SelectorError, "MeshManifestError": MeshManifestError}


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
