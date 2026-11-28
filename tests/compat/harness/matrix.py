"""Compatibility-matrix data types and serializer.

The harness emits a machine-readable matrix per the schema in
``docs/SDK_COMPATIBILITY_HARNESS.md``. Probes return ``ProbeResult``
values; ``build_matrix`` aggregates them into a ``ProtocolMatrix``;
``matrix_to_json`` serializes one or more matrices.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional


class Result(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of a single test case within a protocol/SDK pairing."""

    case: str
    result: Result
    note: Optional[str] = None


@dataclass(frozen=True)
class ProtocolMatrix:
    """All probe results for one (protocol, sdk, sdk_version) tuple."""

    protocol: str
    sdk: str
    sdk_version: str
    gateway_version: str
    results: Mapping[str, Result]
    notes: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "protocol": self.protocol,
            "sdk": self.sdk,
            "sdk_version": self.sdk_version,
            "gateway_version": self.gateway_version,
            "results": {k: v.value for k, v in self.results.items()},
            "notes": dict(self.notes),
        }


_VALID_RESULTS = {r.value for r in Result}


def build_matrix(
    *,
    protocol: str,
    sdk: str,
    sdk_version: str,
    gateway_version: str,
    probes: Iterable[ProbeResult],
) -> ProtocolMatrix:
    """Aggregate probe results, enforcing case-name uniqueness.

    Duplicate case names indicate a probe-author bug: matrix consumers
    expect each case to appear exactly once.
    """

    results: Dict[str, Result] = {}
    notes: Dict[str, str] = {}
    seen: List[str] = []
    for probe in probes:
        if probe.case in results:
            raise ValueError(
                f"duplicate probe case {probe.case!r} for {protocol}/{sdk}; "
                f"already-seen cases: {seen}"
            )
        results[probe.case] = probe.result
        if probe.note:
            notes[probe.case] = probe.note
        seen.append(probe.case)

    return ProtocolMatrix(
        protocol=protocol,
        sdk=sdk,
        sdk_version=sdk_version,
        gateway_version=gateway_version,
        results=results,
        notes=notes,
    )


def matrix_to_json(
    matrices: Iterable[ProtocolMatrix],
    *,
    indent: int = 2,
) -> str:
    """Serialize one or more matrices to a stable JSON string."""

    payload = [m.to_dict() for m in matrices]
    return json.dumps(payload, indent=indent, sort_keys=True)


def validate_matrix_dict(data: Mapping[str, Any]) -> None:
    """Validate a deserialized matrix dict against the documented schema.

    Raises ``ValueError`` on the first violation. This is intentionally
    lightweight; the canonical schema lives in
    ``tests/compat/matrix/compatibility.schema.json`` for tools that
    want full JSON Schema validation.
    """

    required = ("protocol", "sdk", "sdk_version", "gateway_version", "results")
    for key in required:
        if key not in data:
            raise ValueError(f"matrix missing required key: {key!r}")
    if not isinstance(data["results"], Mapping):
        raise ValueError("matrix 'results' must be an object")
    for case, value in data["results"].items():
        if value not in _VALID_RESULTS:
            raise ValueError(
                f"case {case!r} has invalid result {value!r}; "
                f"expected one of {sorted(_VALID_RESULTS)}"
            )
    notes = data.get("notes", {})
    if not isinstance(notes, Mapping):
        raise ValueError("matrix 'notes' must be an object when present")
