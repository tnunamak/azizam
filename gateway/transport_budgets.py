"""Size and time budgets of the plan runners, plan translators and small transport guards, from ``registry/transport_budgets.yml``.

The file is data and ``registry/transport_budgets.schema.json`` is its closed schema. This module holds no number of its
own: a missing file, a broken file or a missing key is an error, never a guess. A subscription kind's ``runner:`` section
may name a budget of ``plan_runner`` itself; ``runner_budget`` answers with the record's number, else the file's.

Sites that read a section at import, before any record is loaded: ``plan_translator`` (responses_plan),
``plan_usage_bounds`` (subscription_kinds), ``mcp_park``, ``turn_state``, ``browser_people`` and ``session_limit``.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import jsonschema
import yaml

_REGISTRY_DIR = Path(__file__).resolve().parent.parent / "registry"
BUNDLED_PATH = _REGISTRY_DIR / "transport_budgets.yml"
SCHEMA_PATH = _REGISTRY_DIR / "transport_budgets.schema.json"

# The budgets a subscription kind's runner section may name; the others belong to the runner as a whole.
RECORD_RUNNER_BUDGETS = ("line_limit_bytes", "kill_grace_seconds", "exit_wait_seconds", "discovery_timeout_seconds", "park_ttl_seconds")


@functools.lru_cache(maxsize=4)
def _read(document_bytes: bytes, schema_bytes: bytes) -> Mapping[str, Mapping[str, Any]]:
    document = yaml.safe_load(document_bytes.decode("utf-8")) or {}
    jsonschema.Draft202012Validator(json.loads(schema_bytes.decode("utf-8"))).validate(document)
    return MappingProxyType({name: MappingProxyType(dict(section)) for name, section in document.items() if name != "version"})


def section(name: str, path: Path = BUNDLED_PATH) -> Mapping[str, Any]:
    """The budgets of section *name*, checked against the schema; ``KeyError`` for a section the file does not hold."""
    return _read(path.read_bytes(), SCHEMA_PATH.read_bytes())[name]


def runner_budget(runner: Mapping[str, Any], key: str) -> Any:
    """A runner budget: the record's own number when its ``runner:`` section names it, else the file's default."""
    value = runner.get(key)
    return section("plan_runner")[key] if value is None else value


def check_runner_budgets(runner: Mapping[str, Any], *, source: Any) -> None:
    """Refuse a record whose ``runner:`` section names a budget that is not a positive number."""
    for key in RECORD_RUNNER_BUDGETS:
        if key not in runner:
            continue
        value = runner[key]
        whole = key == "line_limit_bytes"
        if type(value) is not int and (whole or type(value) is not float) or value <= 0:
            raise ValueError(f"{source}: runner.{key} must be a positive {'whole ' if whole else ''}number")
