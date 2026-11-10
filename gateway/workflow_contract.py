"""The workflow contract, schema version 2: what a workflow document may say, checked in two layers (CODE_TO_DATA_S3,
D1, D2, D4, D5, D8).

- The structural layer is the closed JSON Schema ``registry/workflow.schema.json``. It checks names, types, sizes and
  required fields, and refuses any key it does not list.
- The semantic layer is the code here. It checks what a schema cannot say without other data: a default fits its spec,
  an enum is typed, a generator is registered and its range is not empty, an accepted media type is one the reviewed
  sniffer can measure, an output count can exist.

``check_contract(doc)`` is the one entry point. It gathers every error, each with a path and a stable code, and a
check never stops at the first. A semantic check skips a part whose structure is already broken, so one cause does not
make a list of effects. This module names no model, engine, service, workflow or modality: the schema and the reviewed
tables passed in the context are all it knows.

A binding is opaque here. Its interior is closed by the adapter descriptor it names (S3.3).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

import jsonschema

from gateway.manifests import (
    SCHEMA_VERSION_MESSAGE, SUPPORTED_SCHEMA_VERSIONS, ContractError, ContractErrors, ErrorCollector, PathPart,
    media_bearing, validate_input_value, value_problems)
from gateway.generated_defaults import _generates_a_value
from gateway.result_bundle import ResultLimits
from gateway.workflow_call import SNIFFED_MEDIA_TYPES

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "registry" / "workflow.schema.json"
SECTIONS_DIR = SCHEMA_PATH.parent / "workflow_sections"
CONTRACT_VERSION = 2

MAX_INPUT_DEPTH = 4
ANNOTATION_BYTES = 8 * 1024

# Keys that decide who may run a workflow or at what cost. They live on the registry record, never in the document.
AUTHORITY_KEYS = frozenset({"trusted", "source", "exposure", "trust_tier", "resource_tier", "tags"})

MAX_WORDS = {"purpose": 8, "summary": 40}


@dataclass(frozen=True)
class Generator:
    """A reviewed way to fill an input the caller left empty. A document selects one by id and cannot add one."""

    types: tuple[str, ...]
    # True when the generator draws from a range it is given (``minimum`` and ``maximum``, both required).
    bounded: bool = False


GENERATORS: Mapping[str, Generator] = {"random_integer": Generator(types=("integer",), bounded=True)}


@dataclass(frozen=True)
class ContractContext:
    """The reviewed tables and the trusted deployment limits a check reads. A test passes its own."""

    generators: Mapping[str, Generator] = field(default_factory=lambda: GENERATORS)
    media_types: frozenset[str] = SNIFFED_MEDIA_TYPES
    max_assets: int = field(default_factory=lambda: ResultLimits().max_assets)
    annotation_bytes: int = ANNOTATION_BYTES


@dataclass(frozen=True)
class Report:
    """The result of one check: every error, sorted by path. ``ok`` is true when there are none."""

    errors: tuple[ContractError, ...] = ()
    primary: ContractError | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_list(self) -> list[dict[str, Any]]:
        return [error.as_dict() for error in self.errors]

    def raise_if_any(self) -> None:
        if self.errors:
            # The list is final (sorted, capped, marker included): raise it as it is, never through the cap again.
            raise ContractErrors(self.errors, self.primary, final=True)


def check_contract(doc: Any, context: ContractContext | None = None, extra_errors: Iterable[ContractError] = ()) -> Report:
    """Check a workflow document of schema version 2. Never raises for a bad document; read the report. ``extra_errors``
    (errors found outside the check, e.g. by a converter) join the others before the cap is applied."""
    context = context or ContractContext()
    if not isinstance(doc, Mapping):
        return report_from([ContractError((), "type_mismatch", "a workflow document must be a mapping")])
    version = doc.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS or isinstance(version, bool):
        # The rest would be read against a grammar this document does not use.
        return report_from([ContractError(("schema_version",), "schema_version_unsupported",
                                      SCHEMA_VERSION_MESSAGE)])
    structural = _structural_errors(doc)
    errors = ErrorCollector()
    errors.errors.extend(structural)
    _Semantic(context, [error.path for error in structural], errors).run(doc)
    return report_from([*errors.errors, *extra_errors])


def report_from(found: list[ContractError]) -> Report:
    """The report of a list of errors, sorted and capped as ``ContractErrors`` does it."""
    if not found:
        return Report()
    listed = ContractErrors(found)
    return Report(tuple(listed.errors), listed.primary)


# ------------------------------------------------------------------------------------------------ structural layer


@lru_cache(maxsize=1)
def _validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def _section_validators() -> Mapping[str, jsonschema.Draft202012Validator]:
    """The schema of the section a document of each capability may keep under a key named after that capability: one
    file per capability in ``registry/workflow_sections``, named for it. A capability without a file has no section,
    and a key of that name is an unknown field like any other."""
    return {path.name.removesuffix(".schema.json"): jsonschema.Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(SECTIONS_DIR.glob("*.schema.json"))}


def _section_name(doc: Mapping[str, Any]) -> str | None:
    """The key of the capability section ``doc`` keeps, or None: the capability has a section schema and the key is there."""
    capability = doc.get("capability")
    return capability if isinstance(capability, str) and capability in _section_validators() and capability in doc else None


_LIMIT_KEYWORDS = frozenset({
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength", "minItems", "maxItems",
    "minProperties", "maxProperties", "uniqueItems",
})


def _structural_errors(doc: Mapping[str, Any]) -> list[ContractError]:
    found: dict[tuple[PathPart, ...], ContractError] = {}
    section = _section_name(doc)
    # The capability section is read against its own schema; the rest of the document against the closed root schema.
    rest = {key: value for key, value in doc.items() if key != section}
    errors = [(error, ()) for error in _validator().iter_errors(rest)]
    if section is not None:
        errors += [(error, (section,)) for error in _section_validators()[section].iter_errors(doc[section])]
    for error, prefix in errors:
        for item in _from_schema_error(error, prefix):
            # One refusal per place: the schema may break two rules there, and fixing the first shows the second.
            found.setdefault(item.path, item)
    return list(found.values())


def _from_schema_error(error: jsonschema.ValidationError, prefix: tuple[PathPart, ...] = ()) -> list[ContractError]:
    at = prefix + tuple(error.path)
    keyword = error.validator
    schema = error.schema if isinstance(error.schema, Mapping) else {}
    if keyword == "additionalProperties" and isinstance(error.instance, Mapping):
        known = set(schema.get("properties", {}))
        return [ContractError(at + (key,), "unknown_field", _unknown_field_message(at, key))
                for key in error.instance if key not in known]
    if keyword == "required" and isinstance(error.instance, Mapping):
        return [ContractError(at + (name,), "required_missing", f"{_where(at)}is missing the required field {name}")
                for name in schema.get("required", ()) if name not in error.instance]
    if "propertyNames" in error.schema_path:
        return [ContractError(at + (error.instance,), "key_invalid", f"{_where(at)}has a key that does not follow the naming rule")]
    # A key called ``default`` or ``enum`` inside a map of specs is a name, not the keyword.
    named = len(at) > 1 and at[-2] in ("inputs", "properties")
    if keyword in ("type", "enum", "const") and at and at[-1] == "default" and not named:
        return [ContractError(at, "default_invalid", f"{_where(at[:-1])}has a default that does not fit its type")]
    if keyword in ("type", "enum", "const") and len(at) > 1 and at[-2] == "enum" and not (len(at) > 2 and at[-3] in ("inputs", "properties")):
        return [ContractError(at, "enum_value_invalid", "an enum value must have the type of its input")]
    if keyword == "type":
        return [ContractError(at, "type_mismatch", f"{_where(at)}must be {_type_words(schema.get('type'))}")]
    if keyword in ("enum", "const"):
        return [ContractError(at, "value_invalid", f"{_where(at)}holds a value that is not allowed here")]
    if keyword == "pattern":
        return [ContractError(at, "pattern_mismatch", f"{_where(at)}does not follow the rule for its text")]
    if keyword in _LIMIT_KEYWORDS:
        return [ContractError(at, "limit_exceeded", f"{_where(at)}is outside its size or range limit")]
    if keyword == "oneOf":
        return [ContractError(at, "shape_invalid", f"{_where(at)}needs exactly one of properties and opaque")]
    if keyword == "not":
        return [ContractError(at + ("generate",), "default_and_generate", f"{_where(at)}cannot have both a default and a generate rule")]
    return [ContractError(at, "schema_violation", f"{_where(at)}does not follow the schema")]


def _unknown_field_message(at: tuple[PathPart, ...], key: str) -> str:
    if not at and key in AUTHORITY_KEYS:
        return f"{key} is not a document field; it belongs to the registry record"
    return f"{_where(at)}has a field the schema does not list: {key}"


def _where(at: tuple[PathPart, ...]) -> str:
    return (".".join(str(part) for part in at) + " ") if at else "the document "


def _type_words(expected: Any) -> str:
    kinds = expected if isinstance(expected, list) else [expected]
    return " or ".join(f"a {kind}" for kind in kinds)


# ------------------------------------------------------------------------------------------------- semantic layer

_CHILD_KEYS = frozenset({"properties", "items", "annotations"})


class _Semantic:
    def __init__(self, context: ContractContext, broken: list[tuple[PathPart, ...]], errors: ErrorCollector) -> None:
        self.context = context
        self.broken = broken
        self.errors = errors
        self.annotation_bytes = 0
        self.families = {media_type.split("/", 1)[0] for media_type in context.media_types}

    def run(self, doc: Mapping[str, Any]) -> None:
        self._words(doc)
        self._annotations(doc.get("annotations"))
        inputs = doc.get("inputs")
        if isinstance(inputs, Mapping):
            for name, spec in inputs.items():
                self.spec(("inputs", name), spec, 1)
        outputs = doc.get("outputs")
        if isinstance(outputs, Mapping):
            for name, output in outputs.items():
                self.output(("outputs", name), output)
        self.capability_section(doc)
        if self.annotation_bytes > self.context.annotation_bytes:
            self.errors.add(("annotations",), "annotations_too_large",
                            f"all annotations together hold more than {self.context.annotation_bytes} bytes")

    # -- helpers

    def _own_errors(self, at: tuple[PathPart, ...]) -> bool:
        """True when the structure of the spec at ``at`` itself is already refused (not one of its children)."""
        size = len(at)
        return any(path[:size] == at and (len(path) <= size or path[size] not in _CHILD_KEYS) for path in self.broken)

    def _broken_below(self, at: tuple[PathPart, ...]) -> bool:
        return any(path[:len(at)] == at for path in self.broken)

    def _words(self, doc: Mapping[str, Any]) -> None:
        for name, limit in MAX_WORDS.items():
            value = doc.get(name)
            if isinstance(value, str) and len(value.split()) > limit:
                self.errors.add((name,), "too_many_words", f"{name} must contain at most {limit} words")

    def _annotations(self, value: Any) -> None:
        if isinstance(value, Mapping):
            self.annotation_bytes += len(json.dumps(value, sort_keys=True, default=str).encode("utf-8"))

    # -- inputs

    def spec(self, at: tuple[PathPart, ...], spec: Any, depth: int) -> None:
        if not isinstance(spec, Mapping):
            return
        self._annotations(spec.get("annotations"))
        if at and at[0] == "inputs" and depth > 1:
            for key in ("generate", "settable"):
                if (key == "generate" and key in spec) or (key == "settable" and spec.get(key) is False):
                    self.errors.add(at + (key,), "generator_nested_unsupported",
                                    "generated and gateway-only inputs are supported only at the top level")
        if depth > MAX_INPUT_DEPTH:
            self.errors.add(at, "nesting_too_deep", f"inputs nest at most {MAX_INPUT_DEPTH} levels")
            return
        kind = spec.get("type")
        properties = spec.get("properties")
        if kind == "object" and isinstance(properties, Mapping):
            for name, child in properties.items():
                self.spec(at + ("properties", name), child, depth + 1)
        if kind == "array":
            self.spec(at + ("items",), spec.get("items"), depth + 1)
        if self._own_errors(at):
            return
        if kind in ("integer", "number"):
            self._numeric(at, spec)
        if kind == "string":
            self._ordered(at, spec, "min_length", "max_length")
        if kind == "array":
            self._ordered(at, spec, "min_items", "max_items")
        if kind == "asset":
            self._accepts(at, spec)
        if "enum" in spec:
            self._enum(at, spec)
        if "default" in spec and not self._broken_below(at):
            for path, code, message in value_problems(spec, spec["default"], at + ("default",)):
                self.errors.add(path, "default_invalid" if code == "value_invalid" else code, message)
        if spec.get("settable") is False and "generate" not in spec:
            self.errors.add(at + ("settable",), "settable_without_generate", "an input a caller cannot set needs a generate rule")

    def _ordered(self, at: tuple[PathPart, ...], spec: Mapping[str, Any], low: str, high: str) -> None:
        if low in spec and high in spec and spec[low] > spec[high]:
            self.errors.add(at, "bounds_empty", f"{low} is above {high}")

    def _numeric(self, at: tuple[PathPart, ...], spec: Mapping[str, Any]) -> None:
        valid = True
        for key in ("minimum", "maximum", "step", "offset"):
            value = spec.get(key)
            if isinstance(value, float) and not math.isfinite(value):
                self.errors.add(at + (key,), "number_not_finite", f"{key} must be a finite number")
                valid = False
            elif spec["type"] == "integer" and key in ("minimum", "maximum") and value is not None \
                    and (isinstance(value, bool) or not isinstance(value, int)):
                self.errors.add(at + (key,), "integer_expected", f"{key} of an integer input must be an integer")
                valid = False
        self._ordered(at, spec, "minimum", "maximum")
        # A generator check reasons about the bounds and the grid; with one of them broken, the field error is the report.
        if "generate" in spec and valid:
            self._generator(at, spec)

    def _generator(self, at: tuple[PathPart, ...], spec: Mapping[str, Any]) -> None:
        rule = spec["generate"]
        generator = self.context.generators.get(rule.get("generator"))
        if generator is None:
            self.errors.add(at + ("generate", "generator"), "generator_unknown", "no reviewed generator has this id")
            return
        if spec["type"] not in generator.types:
            self.errors.add(at + ("generate",), "generator_type_mismatch", f"this generator fills {' or '.join(generator.types)} inputs only")
            return
        if not generator.bounded:
            return
        missing = False
        for bound in ("minimum", "maximum"):
            if bound not in rule:
                self.errors.add(at + ("generate", bound), "required_missing", f"the generate rule needs {bound}")
                missing = True
        if missing:
            return
        if not all(_is_number(rule[bound]) and _is_finite(rule[bound]) for bound in ("minimum", "maximum")):
            return  # the schema layer reports a bound that is not a finite number
        low = max(rule["minimum"], spec.get("minimum", rule["minimum"]))
        high = min(rule["maximum"], spec.get("maximum", rule["maximum"]))
        if not _generates_a_value(spec, low, high):
            self.errors.add(at + ("generate",), "generator_range_empty", "the generator's range and the input's bounds share no value")

    def _accepts(self, at: tuple[PathPart, ...], spec: Mapping[str, Any]) -> None:
        for index, accepted in enumerate(spec.get("accepts") or ()):
            top, _, sub = str(accepted).partition("/")
            known = top in self.families if sub == "*" else accepted in self.context.media_types
            if not known:
                self.errors.add(at + ("accepts", index), "accepts_unknown_type",
                                "the reviewed sniffer cannot measure this media type")

    def _enum(self, at: tuple[PathPart, ...], spec: Mapping[str, Any]) -> None:
        bare = {"type": spec.get("type")}
        for index, option in enumerate(spec["enum"]):
            if validate_input_value("enum", option, bare) is not None:
                self.errors.add(at + ("enum", index), "enum_value_invalid", "an enum value must have the type of its input")

    # -- the section a document keeps for its own capability

    def capability_section(self, doc: Mapping[str, Any]) -> None:
        """What the section's schema cannot say: a role names an input the document declares and binds, a pair of
        ``min_x`` and ``max_x`` is not empty, a ``default_x`` lies between them, and a default is not above its ``max``."""
        name = _section_name(doc)
        section = doc.get(name) if name is not None else None
        if not isinstance(section, Mapping) or self._broken_below((name,)):
            return
        inputs = doc.get("inputs") if isinstance(doc.get("inputs"), Mapping) else {}
        bindings = doc.get("bindings") if isinstance(doc.get("bindings"), Mapping) else {}
        bound = {key for body in bindings.values() if isinstance(body, Mapping) and isinstance(body.get("map"), Mapping)
                 for key in body["map"]}
        roles = section.get("roles")
        for role, input_name in (roles.items() if isinstance(roles, Mapping) else ()):
            if input_name not in inputs:
                self.errors.add((name, "roles", role), "role_input_missing", f"role {role} names an input the document does not declare")
            elif input_name not in bound:
                self.errors.add((name, "roles", role), "role_input_unbound", f"role {role} names an input that no binding maps")
        for group_name, group in section.items():
            if isinstance(group, Mapping):
                self._ranges((name, group_name), group)

    def _ranges(self, at: tuple[PathPart, ...], group: Mapping[str, Any]) -> None:
        for key in group:
            if not (isinstance(key, str) and key.startswith("min_") and "max_" + key[4:] in group):
                continue
            kind = key[4:]
            low, high = group[key], group["max_" + kind]
            if not (_is_number(low) and _is_number(high)):
                continue
            if low > high:
                self.errors.add(at, "bounds_empty", f"{key} is above max_{kind}")
            default = group.get("default_" + kind)
            if _is_number(default) and not low <= default <= high:
                self.errors.add(at + ("default_" + kind,), "default_invalid", f"default_{kind} lies outside {key} to max_{kind}")
        default, largest = group.get("default"), group.get("max")
        if isinstance(default, str) and isinstance(largest, str):
            try:
                (dw, dh), (mw, mh) = ([int(side) for side in text.split("x")] for text in (default, largest))
            except ValueError:
                return
            if dw > mw or dh > mh:
                self.errors.add(at + ("default",), "default_over_max", "the default is larger than the largest allowed")

    # -- outputs

    def output(self, at: tuple[PathPart, ...], output: Any) -> None:
        if not isinstance(output, Mapping):
            return
        self._annotations(output.get("annotations"))
        count = output.get("count")
        if isinstance(count, Mapping):
            # The schema accepts an integral float (3.0) as an integer, so the meaning is checked on that too.
            low, high, limit = _whole(count.get("min")), _whole(count.get("max")), self.context.max_assets
            if low is not None and high is not None and low > high:
                self.errors.add(at + ("count",), "count_invalid", "count min is above count max")
            for bound in ("min", "max"):
                value = _whole(count.get(bound))
                if value is not None and value > limit:
                    self.errors.add(at + ("count", bound), "count_over_limit", f"count {bound} is above the deployment limit of {limit}")
        schema = output.get("schema")
        if isinstance(schema, Mapping):
            self.spec(at + ("schema",), schema, 1)
            if media_bearing(schema):
                self.errors.add(at + ("schema",), "asset_in_result_schema", "a result schema describes data, not media")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_finite(value: Any) -> bool:
    """Finiteness is only a question for a float; an int of any size is finite (math.isfinite would overflow on it)."""
    return not isinstance(value, float) or math.isfinite(value)


def _whole(value: Any) -> int | None:
    """The integer a JSON Schema integer is: an int, or a float with no fraction. Anything else is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None
