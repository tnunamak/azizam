"""The family of an OpenAI model id, read from registry/openai_model_families.yml.

The file holds OpenAI's naming conventions as data: exact ids and ordered match rules. This module only validates the
file and applies it; it holds no model name.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

FAMILIES_FILE = Path(__file__).resolve().parent.parent / "registry" / "openai_model_families.yml"
_TESTS = ("prefix", "suffix", "contains")


class FamiliesFileError(ValueError):
    """The families file does not have the documented shape."""


@dataclass(frozen=True)
class Families:
    exact: Mapping[str, str]
    rules: tuple[tuple[str, tuple[tuple[tuple[str, str], ...], ...]], ...]

    def category_for(self, model_id: str) -> str | None:
        """The family of a normalized model id, or None when no row names it."""
        if model_id in self.exact:
            return self.exact[model_id]
        for category, groups in self.rules:
            if all(any(_holds(model_id, test, value) for test, value in group) for group in groups):
                return category
        return None


def _holds(model_id: str, test: str, value: str) -> bool:
    if test == "prefix":
        return model_id.startswith(value)
    if test == "suffix":
        return model_id.endswith(value)
    return value in model_id


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise FamiliesFileError(f"{where}: expected a nonempty string")
    return value


def parse(data: Any) -> Families:
    """Validate the loaded YAML document and return it as a ``Families``."""
    if not isinstance(data, Mapping) or data.get("schema_version") != 1:
        raise FamiliesFileError("expected a mapping with schema_version: 1")
    if set(data) - {"schema_version", "exact", "rules"}:
        raise FamiliesFileError(f"unknown keys: {sorted(set(data) - {'schema_version', 'exact', 'rules'})}")
    exact = data.get("exact") or {}
    if not isinstance(exact, Mapping):
        raise FamiliesFileError("exact: expected a mapping")
    exact = {_text(k, "exact key"): _text(v, f"exact.{k}") for k, v in exact.items()}
    rules = []
    for index, rule in enumerate(data.get("rules") or []):
        where = f"rules[{index}]"
        if not isinstance(rule, Mapping) or set(rule) != {"category", "match"}:
            raise FamiliesFileError(f"{where}: expected exactly category and match")
        groups = rule["match"]
        if not isinstance(groups, list) or not groups:
            raise FamiliesFileError(f"{where}.match: expected a nonempty list of groups")
        parsed_groups = []
        for g_index, group in enumerate(groups):
            if not isinstance(group, list) or not group:
                raise FamiliesFileError(f"{where}.match[{g_index}]: expected a nonempty list of tests")
            tests = []
            for test in group:
                if not isinstance(test, Mapping) or len(test) != 1 or next(iter(test)) not in _TESTS:
                    raise FamiliesFileError(f"{where}.match[{g_index}]: each test is one of {', '.join(_TESTS)}")
                (name, value), = test.items()
                tests.append((name, _text(value, f"{where}.match[{g_index}].{name}")))
            parsed_groups.append(tuple(tests))
        rules.append((_text(rule["category"], f"{where}.category"), tuple(parsed_groups)))
    return Families(exact=dict(exact), rules=tuple(rules))


def load(path: Path = FAMILIES_FILE) -> Families:
    with open(path, encoding="utf-8") as handle:
        return parse(yaml.safe_load(handle))


FAMILIES = load()
