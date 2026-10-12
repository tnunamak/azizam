"""Scenario files: what a fake upstream expects and how it answers.

A scenario is JSON data. It lists exchanges in the order the test expects
them. Each exchange has a request matcher, a response, and call-count rules.
Loading validates the file so a typo fails at load, not as a silent miss.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .protocols import PROTOCOLS, Protocol

SCENARIO_DIR = Path(__file__).parent / "scenarios"
_PLACEHOLDER = re.compile(r"\{\{([a-z_][a-z0-9_]*)\}\}")
_PART_KEYS = frozenset({"text", "hex", "event", "done", "wait", "disconnect"})
_MATCH_KEYS = frozenset({"method", "path", "query", "headers", "json"})
_RESPONSE_KEYS = frozenset({"status", "headers", "wait_before_headers", "json", "body", "disconnect_before_headers"})
_EXCHANGE_KEYS = frozenset({"id", "match", "response", "required", "times", "repeat"})
_SCENARIO_KEYS = frozenset({"protocol", "name", "description", "provenance", "exchanges"})
ABSENT = {"$absent": True}


class ScenarioError(ValueError):
    pass


@dataclass(frozen=True)
class Exchange:
    id: str
    match: Mapping[str, Any]
    response: Mapping[str, Any]
    required: bool = True
    times: int = 1
    repeat: bool = False


@dataclass(frozen=True)
class Scenario:
    protocol: Protocol
    name: str
    provenance: Mapping[str, Any]
    exchanges: tuple[Exchange, ...]
    description: str = ""
    source: str = field(default="<memory>", compare=False)


def load_scenario(name: str, **values: Any) -> Scenario:
    """Load ``scenarios/<name>.json``, filling ``{{placeholders}}`` from ``values``."""
    path = SCENARIO_DIR / f"{name}.json"
    return scenario_from_data(json.loads(path.read_text(encoding="utf-8")), source=str(path), **values)


def scenario_from_data(data: Mapping[str, Any], *, source: str = "<memory>", **values: Any) -> Scenario:
    data = _fill(data, values, source)
    _keys(data, _SCENARIO_KEYS, {"protocol", "name", "provenance", "exchanges"}, source)
    protocol = PROTOCOLS.get(data["protocol"])
    if protocol is None:
        raise ScenarioError(f"{source}: unknown protocol {data['protocol']!r}")
    provenance = data["provenance"]
    if not isinstance(provenance, Mapping) or provenance.get("source") not in {"hand-written", "transcript"}:
        raise ScenarioError(f"{source}: provenance.source must be 'hand-written' or 'transcript'")
    if not str(provenance.get("reference") or "").strip():
        raise ScenarioError(f"{source}: provenance.reference must name the document or transcript")
    exchanges = tuple(_exchange(raw, protocol, source) for raw in data["exchanges"])
    if not exchanges:
        raise ScenarioError(f"{source}: a scenario needs at least one exchange")
    ids = [exchange.id for exchange in exchanges]
    if len(set(ids)) != len(ids):
        raise ScenarioError(f"{source}: exchange ids must be unique")
    return Scenario(protocol, data["name"], provenance, exchanges, data.get("description", ""), source)


def _exchange(raw: Mapping[str, Any], protocol: Protocol, source: str) -> Exchange:
    _keys(raw, _EXCHANGE_KEYS, {"id", "match", "response"}, source)
    where = f"{source}: exchange {raw['id']!r}"
    match = raw["match"]
    _keys(match, _MATCH_KEYS, {"method", "path"}, where)
    if not protocol.allows(match["path"]):
        raise ScenarioError(f"{where}: path {match['path']!r} is not part of protocol {protocol.name!r}")
    response = raw["response"]
    _keys(response, _RESPONSE_KEYS, set(), where)
    if "json" in response and "body" in response:
        raise ScenarioError(f"{where}: a response has either json or body, not both")
    for part in response.get("body", ()):
        if not isinstance(part, Mapping) or len(part) != 1 or not set(part) <= _PART_KEYS:
            raise ScenarioError(f"{where}: body part {part!r} must have exactly one of {sorted(_PART_KEYS)}")
        if "event" in part and protocol.framing == "none":
            raise ScenarioError(f"{where}: protocol {protocol.name!r} has no event framing")
    times = raw.get("times", 1)
    if not isinstance(times, int) or times < 1:
        raise ScenarioError(f"{where}: times must be a positive integer")
    if "headers" in match:
        match = {**match, "headers": {str(k).lower(): v for k, v in match["headers"].items()}}
    return Exchange(
        id=raw["id"],
        match=match,
        response=response,
        required=bool(raw.get("required", True)),
        times=times,
        repeat=bool(raw.get("repeat", False)),
    )


def _keys(value: Any, allowed: frozenset[str], required: set[str], where: str) -> None:
    if not isinstance(value, Mapping):
        raise ScenarioError(f"{where}: expected an object, got {value!r}")
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown or missing:
        raise ScenarioError(f"{where}: unknown keys {sorted(unknown)}, missing keys {sorted(missing)}")


def _fill(value: Any, values: Mapping[str, Any], source: str) -> Any:
    if isinstance(value, str):
        whole = _PLACEHOLDER.fullmatch(value)
        if whole:
            return _value(whole.group(1), values, source)
        return _PLACEHOLDER.sub(lambda m: str(_value(m.group(1), values, source)), value)
    if isinstance(value, Mapping):
        return {key: _fill(item, values, source) for key, item in value.items()}
    if isinstance(value, list):
        return [_fill(item, values, source) for item in value]
    return value


def _value(name: str, values: Mapping[str, Any], source: str) -> Any:
    if name not in values:
        raise ScenarioError(f"{source}: placeholder {{{{{name}}}}} has no value")
    return values[name]


def matches(expected: Any, actual: Any) -> bool:
    """Subset match: objects match on the keys given, lists and scalars exactly."""
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return False
        for key, item in expected.items():
            if item == ABSENT:
                if key in actual:
                    return False
            elif key not in actual or not matches(item, actual[key]):
                return False
        return True
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(
            matches(e, a) for e, a in zip(expected, actual)
        )
    return expected == actual and type(expected) is type(actual)
