"""Generated defaults: the reviewed generators and the one place that resolves them (CODE_TO_DATA_S3 D6).

A workflow document may declare ``generate: {generator: <id>, ...}`` on an input. This module holds the reviewed code
behind each id and ``resolve_generated``, which fills such an input when the caller gave no value:

- **Once.** A value is drawn only when the caller gave none, and the route that accepts a job draws it once, after
  idempotency resolution (the image route has one; audio acceptance has none and makes a new job, with a new draw, for
  each request) and before the job's envelope is written. The envelope's ``generated`` block keeps it with the
  generator id (``gateway/workflow_envelope.py``). A retry, a restart or a replay passes that block back as ``stored``
  and reads the value; it never draws again.
- **Range.** ``random_integer`` draws from its own required bounds intersected with the input's effective bounds.
- **Source.** A draw uses ``secrets`` by default. A caller can inject another source, so a test fixes the value.
- **Persistence.** Each generator says how its value persists (``value``, ``digest`` or ``none``). The envelope
  grammar reads that map from ``persistence``.
- **Three states of stored state.** A job's ``stored`` block can hold an entry that is readable, one that erasure
  emptied (``erased``), or one that cannot be read. An input with no entry at all belongs to a job made before values
  were kept (or one the caller named), and keeps the old draw so that job survives a deploy. An erased or unreadable
  entry never draws: the job must not run (``GeneratedValueUnavailable``).

The save-time checks (``generator_unknown``, ``generator_range_empty``) live in ``gateway/workflow_contract.py``. This
module names no input, model, engine, workflow or modality: an input is whatever the document calls it.
"""

from __future__ import annotations

import math
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from gateway.manifests import ManifestError, exact_number, validate_input_value

ERASED = "erased"  # the key an erased entry carries in place of ``value``


class GeneratedValueUnavailable(Exception):
    """A job's stored generated value is gone (``erased``) or cannot be read. The job fails; it never draws again."""

    def __init__(self, message: str, *, erased: bool) -> None:
        super().__init__(message)
        self.erased = erased


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


@dataclass(frozen=True)
class IntegerDomain:
    """An ordered integer domain. A grid costs O(1); only a declared enum stores individual values."""

    first: int
    period: int
    count: int
    members: tuple[int, ...] | None = None

    def value(self, index: int) -> int:
        if not 0 <= index < self.count:
            raise ManifestError("generated domain index is outside its range")
        return self.members[index] if self.members is not None else self.first + self.period * index

    def index(self, value: int) -> int:
        if self.members is not None:
            try:
                return self.members.index(value)
            except ValueError as exc:
                raise ManifestError("batch base is outside the input's enum") from exc
        if (value - self.first) % self.period:
            raise ManifestError("batch base is outside the input's grid")
        return (value - self.first) // self.period


def legal_integer_domain(rule: Mapping[str, Any], spec: Mapping[str, Any]) -> IntegerDomain:
    """The ordered integers in the rule/input intersection, enum and exact step/offset grid.

    Unknown step/offset semantics retain the old feasibility fallback: the plain interval, not a proof of emptiness.
    No interval is enumerated, including the full 32-bit range.
    """
    low = max(rule["minimum"], spec.get("minimum", rule["minimum"]))
    high = min(rule["maximum"], spec.get("maximum", rule["maximum"]))
    first, last = math.ceil(exact_number(low)), math.floor(exact_number(high))
    if first > last:
        return IntegerDomain(first, 1, 0)
    period = 1
    step = spec.get("step")
    if step is not None:
        step_exact, offset_exact = exact_number(step), exact_number(spec.get("offset", 0))
        if step_exact is None or offset_exact is None or step_exact <= 0:
            return IntegerDomain(first, 1, last - first + 1)
        found = _first_on_grid(first, step_exact, offset_exact)
        if found is None or found > last:
            return IntegerDomain(first, 1, 0)
        first, period = found, step_exact.numerator
    enum = spec.get("enum")
    if isinstance(enum, list):
        members = tuple(sorted({v for v in enum if isinstance(v, int) and not isinstance(v, bool)
                                and first <= v <= last and (v - first) % period == 0}))
        return IntegerDomain(members[0] if members else first, period, len(members), members)
    return IntegerDomain(first, period, (last - first) // period + 1)


def _first_on_grid(first: int, step: Fraction, offset: Fraction) -> int | None:
    """The least integer n >= first with n = offset + k*step for an integer k (the grid ``on_grid`` tests), or None
    when no integer is on the grid. With D the common denominator, D*n = A (mod P) for the integers A = offset*D and P = step*D."""
    denominator = math.lcm(step.denominator, offset.denominator)
    target, period = int(offset * denominator), int(step * denominator)
    divisor = math.gcd(denominator, period)
    if target % divisor:
        return None
    modulus = period // divisor
    residue = (target // divisor) * pow(denominator // divisor, -1, modulus) % modulus
    return first + (residue - first) % modulus


def _generates_a_value(spec: Mapping[str, Any], low: Any, high: Any) -> bool:
    """Compatibility entry point: feasibility and sampling use the same domain."""
    if exact_number(low) is None or exact_number(high) is None:
        return True
    return legal_integer_domain({"minimum": low, "maximum": high}, spec).count > 0


def declared_range(rule: Mapping[str, Any], spec: Mapping[str, Any]) -> tuple[int, int]:
    """The whole numbers the input itself allows: its own bounds, or the rule's where the input names none."""
    first = math.ceil(spec.get("minimum", rule["minimum"]))
    last = math.floor(spec.get("maximum", rule["maximum"]))
    if first > last:
        raise ManifestError("the input's bounds hold no integer")
    return first, last


def offset_values(inputs: Mapping[str, Any], carried: Mapping[str, int], offset: int) -> dict[str, int]:
    """The value each generated input takes ``offset`` steps after its stored base, wrapped inside the input's range.

    ``value_n = low + ((base - low + offset) mod (high - low + 1))`` for ``low..high`` the input's declared range. On a
    step grid or an enum, ``offset`` counts the input's legal values instead. An
    image batch binds one base for the whole job and offsets it by the images made so far, so every round gets a
    value the input accepts, however many rounds run."""
    declared = generated_inputs(inputs)
    shifted: dict[str, int] = {}
    for name, value in carried.items():
        spec = declared.get(name)
        if spec is None:
            shifted[name] = value
            continue
        low, high = declared_range(spec["generate"], spec)
        domain = legal_integer_domain({"minimum": low, "maximum": high}, spec)
        if not domain.count:
            raise ManifestError("the input has no legal integer for a batch")
        shifted[name] = domain.value((domain.index(value) + offset) % domain.count)
    return shifted


def _random_integer(rule: Mapping[str, Any], spec: Mapping[str, Any], source: Source) -> int:
    domain = legal_integer_domain(rule, spec)
    if not domain.count:
        raise ManifestError("the generator and input share no legal integer")
    if domain.members is None and domain.period == 1:
        # A plain range asks the source for the value itself, as it always has.
        return source(domain.first, domain.first + domain.count - 1)
    # A grid or an enum asks for a position in its ordered legal values, so every draw is one the input accepts.
    return domain.value(source(0, domain.count - 1))


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


def _read_stored(name: str, kept: Any, generator: str) -> dict[str, Any]:
    """The stored entry for one input, read back as it is, or ``GeneratedValueUnavailable`` when it is erased, was made
    by another generator or has no value. Nothing is drawn in its place."""
    if isinstance(kept, Mapping) and kept.get(ERASED) is True:
        raise GeneratedValueUnavailable(f"input {name}: the value the gateway generated for this job was erased", erased=True)
    if not isinstance(kept, Mapping) or kept.get("generator") != generator or "value" not in kept:
        raise GeneratedValueUnavailable(f"input {name}: the stored generated value cannot be read", erased=False)
    return {"generator": generator, "value": kept["value"]}


def _check_stored_value(name: str, value: Any, rule: Mapping[str, Any], spec: Mapping[str, Any]) -> None:
    """A stored value must still fit the input (type, bounds, step grid) and the rule's own bounds. One that does not
    is unreadable: the job fails and nothing is drawn in its place."""
    fits = (isinstance(value, int) and not isinstance(value, bool)
            and validate_input_value(name, value, dict(spec)) is None)
    if fits:
        first, last = integer_range(rule, spec)
        fits = first <= value <= last
    if not fits:
        raise GeneratedValueUnavailable(f"input {name}: the stored generated value does not fit the input", erased=False)


def erased_block(block: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """The block erasure leaves: each entry keeps its generator and loses its value, so a later read can tell an
    erased value from a job that never kept one."""
    return {name: {"generator": entry.get("generator") if isinstance(entry, Mapping) else None, ERASED: True}
            for name, entry in block.items()}


def resolve_generated(
    inputs: Mapping[str, Any],
    given: Mapping[str, Any],
    *,
    stored: Mapping[str, Mapping[str, Any]] | None = None,
    source: Source | None = None,
) -> dict[str, dict[str, Any]]:
    """The envelope ``generated`` block for one request: an entry for each generated input the caller left empty.

    ``given`` holds the values the caller named. A generated input the caller named gets no entry, and a caller cannot
    name an input marked ``settable: false``. For the others, an entry in ``stored`` is read back as it is (an erased or
    unreadable one raises ``GeneratedValueUnavailable``); an input with no entry draws, which is the route that accepts the
    job, or a job made before values were kept. A drawn value that does not fit the input is refused with
    ``ManifestError``, as is a generator that no reviewed code backs."""
    source = source or secure_source  # looked up per call, so a test replaces ``secure_source``
    block: dict[str, dict[str, Any]] = {}
    for name, spec in generated_inputs(inputs).items():
        if name in given and caller_may_set(spec):
            continue
        rule = spec["generate"]
        reviewed = REVIEWED.get(rule.get("generator"))
        if reviewed is None:
            raise ManifestError(f"input {name}: no reviewed generator has the id {rule.get('generator')!r}")
        if stored is not None and name in stored:
            entry = _read_stored(name, stored[name], rule["generator"])
            _check_stored_value(name, entry["value"], rule, spec)
            block[name] = entry
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
    source: Source | None = None,
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
