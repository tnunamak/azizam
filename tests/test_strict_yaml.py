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
def test_merge_keys_match_safe_load(text: str) -> None:
    assert safe_load_strict(text) == yaml.safe_load(text)


def test_refuses_an_unhashable_key_as_a_yaml_error() -> None:
    with pytest.raises(yaml.YAMLError, match="a mapping key must be a plain value") as caught:
        safe_load_strict("? [one, two]\n: value\n")
    assert isinstance(caught.value, yaml.constructor.ConstructorError)
