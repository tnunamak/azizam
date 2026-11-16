"""Strict YAML parsing refuses repeated keys without changing ordinary data."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.strict_yaml import StrictLoader, safe_load_strict  # noqa: E402


@pytest.mark.parametrize(
    "text",
    [
        "inputs: first\ninputs: second\n",
        "row:\n  inputs: first\n  inputs: second\n",
        "rows:\n  - inputs: first\n    inputs: second\n",
    ],
    ids=["top-level", "nested", "list-item"],
)
def test_refuses_a_duplicate_key_and_names_it(text: str) -> None:
    with pytest.raises(yaml.constructor.ConstructorError, match="duplicate key \'inputs\'"):
        safe_load_strict(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "schema_version: 1\nrows:\n  - name: ordinary\n    enabled: true\n    values: [null, 2, 3.5, text]\n",
    ],
    ids=["empty", "ordinary"],
)
def test_ordinary_yaml_matches_safe_load(text: str) -> None:
    expected = yaml.safe_load(text)
    assert safe_load_strict(text) == expected
    assert yaml.load(text, Loader=StrictLoader) == expected


@pytest.mark.parametrize(
    "text",
    [
        "defaults: &defaults {in: body}\nrow:\n  <<: *defaults\n  field: prompt\n",
        "placement: &placement {in: body}\nname: &name {field: prompt}\nrow:\n  <<: [*placement, *name]\n",
    ],
    ids=["mapping-merge", "sequence-merge"],
)
def test_refuses_merge_keys_at_the_merge_key_mark(text: str) -> None:
    with pytest.raises(yaml.constructor.ConstructorError, match="merge keys are not allowed") as caught:
        safe_load_strict(text)
    expected_line = next(i for i, line in enumerate(text.splitlines()) if line.lstrip().startswith("<<:"))
    assert caught.value.problem_mark.line == expected_line


def test_refuses_an_unhashable_key_as_a_yaml_error() -> None:
    with pytest.raises(yaml.YAMLError, match="a mapping key must be a plain value") as caught:
        safe_load_strict("? [one, two]\n: value\n")
    assert isinstance(caught.value, yaml.constructor.ConstructorError)


@pytest.mark.parametrize(
    "text",
    [
        "1: a\ntrue: b\n",
        "1: a\n1.0: b\n",
        "1: a\n1: b\n",
        "a: 1\na: 2\n",
        "row: {nested: {a: 1, a: 2}}\n",
    ],
    ids=["int-bool", "int-float", "int-int", "string-string", "nested-flow"],
)
def test_refuses_python_equal_keys(text: str) -> None:
    with pytest.raises(yaml.constructor.ConstructorError, match="duplicate key"):
        safe_load_strict(text)


@pytest.mark.parametrize("second", ["true", "1.0"], ids=["bool", "float"])
def test_cross_type_duplicate_names_both_source_lines(second: str) -> None:
    text = f"row:\n  1: a\n  {second}: b\n"
    with pytest.raises(yaml.constructor.ConstructorError, match="duplicate key") as caught:
        safe_load_strict(text)
    message = str(caught.value)
    assert "first key '1' at line 2" in message
    assert f"conflicting key {second!r} at line 3" in message
    assert caught.value.problem_mark.line == 2


def test_integer_and_quoted_string_keys_remain_distinct() -> None:
    loaded = safe_load_strict("1: a\n'1': b\n")
    assert len(loaded) == 2
    assert loaded[1] == "a"
    assert loaded["1"] == "b"


def test_quoted_merge_spelling_is_an_ordinary_key() -> None:
    assert safe_load_strict("'<<': literal\n") == {"<<": "literal"}
