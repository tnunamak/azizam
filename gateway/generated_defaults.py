"""Generated defaults: the reviewed generators and the one place that resolves them (CODE_TO_DATA_S3 D6).

A workflow document may declare ``generate: {generator: <id>, ...}`` on an input. This module holds the reviewed code
behind each id and ``resolve_generated``, which fills such an input when the caller gave no value:

- **Once.** A value is drawn only when the caller gave none, and the route that accepts a job draws it once, after
  idempotency resolution and before the job's envelope is written. The envelope's ``generated`` block keeps it with the
  generator id (``gateway/workflow_envelope.py``). A retry, a restart or a replay passes that block back as ``stored``
  and reads the value; it never draws again.
- **Range.** ``random_integer`` draws from its own required bounds intersected with the input's effective bounds.
- **Source.** A draw uses ``secrets`` by default. A caller can inject another source, so a test fixes the value.
- **Persistence.** Each generator says how its value persists (``value``, ``digest`` or ``none``). The envelope
  grammar reads that map from ``persistence``.

The save-time checks (``generator_unknown``, ``generator_range_empty``) live in ``gateway/workflow_contract.py``. This
module names no input, model, engine, workflow or modality: an input is whatever the document calls it.
"""

from __future__ import annotations

import math
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from gateway.manifests import ManifestError, validate_input_value

# A source of whole numbers: ``source(low, high)`` returns an integer with ``low <= n <= high``.
Source = Callable[[int, int], int]


def secure_source(low: int, high: int) -> int:
    """The default source: operating-system randomness, uniform over ``low..high``."""
    return low + secrets.randbelow(high - low + 1)


def integer_range(rule: Mapping[str, Any], spec: Mapping[str, Any]) -> tuple[int, int]:
    """The integers a ``random_integer`` rule may draw: its own bounds intersected with the input's bounds.

    The bounds may be fractional, so the integers run from the ceiling of the low bound to the floor of the high one.
    Raises ``ManifestError`` when no integer is inside both."""
    low = max(rule["minimum"], spec.get("minimum", rule["minimum"]))
    high = min(rule["maximum"], spec.get("maximum", rule["maximum"]))
    first, last = math.ceil(low), math.floor(high)
    if first > last:
        raise ManifestError("the generator's range and the input's bounds share no integer")
    return first, last


def _random_integer(rule: Mapping[str, Any], spec: Mapping[str, Any], source: Source) -> int:
    first, last = integer_range(rule, spec)
    return source(first, last)


@dataclass(frozen=True)
class ReviewedGenerator:
    """Reviewed code for one generator id. ``persistence`` is how a value is kept: ``value``, ``digest`` or ``none``."""

    persistence: str
    draw: Callable[[Mapping[str, Any], Mapping[str, Any], Source], Any]


REVIEWED: Mapping[str, ReviewedGenerator] = {"random_integer": ReviewedGenerator("value", _random_integer)}


def persistence() -> dict[str, str]:
    """The map ``parse_envelope`` needs: generator id to persistence class."""
    return {name: generator.persistence for name, generator in REVIEWED.items()}


def generated_inputs(inputs: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """The declared inputs that carry a generate rule, by name, in declaration order."""
    return {name: spec for name, spec in inputs.items() if isinstance(spec, Mapping) and isinstance(spec.get("generate"), Mapping)}


def caller_may_set(spec: Mapping[str, Any]) -> bool:
    """False for an input only the gateway fills (``settable: false``)."""
    return spec.get("settable") is not False


def resolve_generated(
    inputs: Mapping[str, Any],
    given: Mapping[str, Any],
    *,
    stored: Mapping[str, Mapping[str, Any]] | None = None,
    source: Source = secure_source,
) -> dict[str, dict[str, Any]]:
    """The envelope ``generated`` block for one request: an entry for each generated input the caller left empty.

    ``given`` holds the values the caller named. A generated input the caller named gets no entry, and a caller cannot
    name an input marked ``settable: false``. For the others, an entry in ``stored`` made by the same generator is read
    back as it is; otherwise the generator draws. An entry with a value that does not fit the input is refused with
    ``ManifestError``, as is a generator that no reviewed code backs."""
    block: dict[str, dict[str, Any]] = {}
    for name, spec in generated_inputs(inputs).items():
        if name in given and caller_may_set(spec):
            continue
        rule = spec["generate"]
        reviewed = REVIEWED.get(rule.get("generator"))
        if reviewed is None:
            raise ManifestError(f"input {name}: no reviewed generator has the id {rule.get('generator')!r}")
        kept = (stored or {}).get(name)
        if isinstance(kept, Mapping) and kept.get("generator") == rule["generator"] and "value" in kept:
            block[name] = {"generator": rule["generator"], "value": kept["value"]}
            continue
        value = reviewed.draw(rule, spec, source)
        if validate_input_value(name, value, dict(spec)) is not None:
            raise ManifestError(f"input {name}: the generated value does not fit the input")
        block[name] = {"generator": rule["generator"], "value": value}
    return block


def resolved_values(
    inputs: Mapping[str, Any],
    given: Mapping[str, Any],
    *,
    stored: Mapping[str, Mapping[str, Any]] | None = None,
    source: Source = secure_source,
) -> dict[str, Any]:
    """The value of every generated input for one request: the caller's own where it named one it may set, otherwise
    the generated value. The block itself is ``resolve_generated``."""
    own = {name: given[name] for name, spec in generated_inputs(inputs).items() if name in given and caller_may_set(spec)}
    return {**own, **values(resolve_generated(inputs, given, stored=stored, source=source))}


def values(block: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """The values of a ``generated`` block that kept one."""
    return {name: entry["value"] for name, entry in block.items() if "value" in entry}


def first_value(inputs: Mapping[str, Any], carried: Mapping[str, Any]) -> Any:
    """The value of the first generated input (declaration order) that ``carried`` holds, or None. A route that records one
    reproducibility value in a result's provenance reads it here, so no route names the input."""
    for name in generated_inputs(inputs):
        if name in carried:
            return carried[name]
    return None
