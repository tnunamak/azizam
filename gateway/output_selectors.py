"""Output selectors: where each named result of a workflow comes from (S2 design, D5 and D8).

A binding says, for every named result of its contract, where in the engine's answer the result is. The *form* of that
statement belongs to the adapter that reads the answer: an adapter declares the selector form it accepts, as data (its
protocol description's ``result_selectors`` block). This module holds the one grammar for that data, the checks, and
nothing that knows an engine.

An adapter descriptor lists the fields of a selector. Each field is ``text`` or an ``enum``; it can be required or have a
default. Three things in the descriptor carry meaning for core:

- ``identity``: the fields that say *where* a result is. Two selectors with the same identity are duplicates.
- ``template_node``: the field that names a node of the binding's template, so a save can check that the node is a
  producer and not an echo of an input.
- ``kind_field``: the enum field that says which result kind (``json`` or ``assets``) the selector yields; each of its
  values maps to a kind.

A binding checked against its descriptor means: only declared fields, each of the right type, required ones present,
defaults filled, every named result a declared output, every output covered, no more selectors than the limit, and no
two selectors with the same identity. The same shape check runs when a manifest loads and when one is saved. A save adds
the contract rules (``check_for_save``): a binding of an adapter that takes selectors must have them, a selector must not
echo a source, and two outputs that cannot be told apart are refused. A malformed descriptor never loads
(``parse_descriptor`` raises ``DescriptorError``).

``FetchGate`` bounds how many result fetches run at once and how long one takes (D6).

The module names no workflow, engine, model, intent or modality.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import weakref
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from gateway.manifests import MEDIA_INPUT_TYPES, ManifestError

DESCRIPTOR_VERSION = 1
FIELD_TYPES = ("text", "enum")
RESULT_KINDS = ("json", "assets")
MAX_FIELDS = 16
MAX_ENUM_VALUES = 64
MAX_TEXT = 128
DEFAULT_MAX_SELECTORS = 16
CONFIG_SECTION = "workflow_execution"
LEGACY_FLAG = "legacy_output_collection"
_DESCRIPTOR_KEYS = frozenset({"version", "fields", "identity", "template_node", "kind_field"})
_FIELD_KEYS = frozenset({"type", "required", "default", "values", "kinds"})
PROTOCOL_DIR = Path(__file__).resolve().parent.parent / "registry" / "protocols"


class DescriptorError(ValueError):
    """An adapter's selector descriptor is malformed. The message names the problem; there is nothing to load."""


class SelectorError(ManifestError):
    """A binding's selectors break the grammar or a contract rule. ``code`` names the rule."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------------------------------------- the descriptor

@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str
    required: bool
    default: str | None
    values: tuple[str, ...]
    kinds: Mapping[str, str]


@dataclass(frozen=True)
class SelectorDescriptor:
    """The selector form of one adapter, parsed and checked."""

    fields: Mapping[str, FieldSpec]
    identity: tuple[str, ...]
    template_node: str | None
    kind_field: str | None

    def kind_of(self, selector: Mapping[str, Any]) -> str:
        """The result kind a normalized selector yields. A descriptor with no ``kind_field`` yields assets."""
        if self.kind_field is None:
            return "assets"
        return self.fields[self.kind_field].kinds[selector[self.kind_field]]


def _bad(message: str) -> DescriptorError:
    return DescriptorError(f"result_selectors: {message}")


def _word(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT or value != value.strip():
        raise _bad(f"{what} must be a short non-empty string")
    return value


def parse_descriptor(doc: Any) -> SelectorDescriptor:
    """Check ``doc`` (the ``result_selectors`` block of a protocol description) and compile it. Raises ``DescriptorError``."""
    if not isinstance(doc, Mapping):
        raise _bad("must be a mapping")
    unknown = sorted(str(key) for key in doc if key not in _DESCRIPTOR_KEYS)
    if unknown:
        raise _bad(f"unknown keys {unknown}")
    if doc.get("version") != DESCRIPTOR_VERSION or isinstance(doc.get("version"), bool):
        raise _bad(f"version must be {DESCRIPTOR_VERSION}")
    raw_fields = doc.get("fields")
    if not isinstance(raw_fields, Mapping) or not raw_fields:
        raise _bad("fields must be a non-empty mapping")
    if len(raw_fields) > MAX_FIELDS:
        raise _bad(f"at most {MAX_FIELDS} fields")
    fields: dict[str, FieldSpec] = {}
    for raw_name, spec in raw_fields.items():
        name = _word(raw_name, "a field name")
        if not isinstance(spec, Mapping):
            raise _bad(f"field {name} must be a mapping")
        extra = sorted(str(key) for key in spec if key not in _FIELD_KEYS)
        if extra:
            raise _bad(f"field {name} has unknown keys {extra}")
        kind = spec.get("type")
        if kind not in FIELD_TYPES:
            raise _bad(f"field {name} type must be one of {list(FIELD_TYPES)}")
        required = spec.get("required", False)
        if not isinstance(required, bool):
            raise _bad(f"field {name} required must be true or false")
        values: tuple[str, ...] = ()
        if kind == "enum":
            raw_values = spec.get("values")
            if not isinstance(raw_values, list) or not raw_values or len(raw_values) > MAX_ENUM_VALUES:
                raise _bad(f"field {name} needs between 1 and {MAX_ENUM_VALUES} values")
            values = tuple(_word(value, f"a value of field {name}") for value in raw_values)
            if len(set(values)) != len(values):
                raise _bad(f"field {name} repeats a value")
        elif "values" in spec:
            raise _bad(f"field {name} is text and takes no values")
        default = spec.get("default")
        if default is not None:
            default = _word(default, f"the default of field {name}")
            if required:
                raise _bad(f"field {name} cannot be both required and have a default")
            if kind == "enum" and default not in values:
                raise _bad(f"the default of field {name} is not one of its values")
        elif kind == "enum" and not required:
            raise _bad(f"optional enum field {name} needs a default")
        kinds_doc = spec.get("kinds")
        kinds: dict[str, str] = {}
        if kinds_doc is not None:
            if kind != "enum" or not isinstance(kinds_doc, Mapping) or set(kinds_doc) != set(values):
                raise _bad(f"field {name} kinds must map each of its values")
            if any(target not in RESULT_KINDS for target in kinds_doc.values()):
                raise _bad(f"field {name} kinds must be among {list(RESULT_KINDS)}")
            kinds = dict(kinds_doc)
        fields[name] = FieldSpec(name, kind, required, default, values, kinds)
    identity_doc = doc.get("identity")
    if not isinstance(identity_doc, list) or not identity_doc:
        raise _bad("identity must name at least one field")
    identity = tuple(_word(name, "an identity entry") for name in identity_doc)
    if len(set(identity)) != len(identity) or any(name not in fields or not fields[name].required for name in identity):
        raise _bad("identity must name distinct, required, declared fields")
    template_node = doc.get("template_node")
    if template_node is not None:
        template_node = _word(template_node, "template_node")
        if template_node not in fields or fields[template_node].type != "text" or not fields[template_node].required:
            raise _bad("template_node must name a required text field")
    kind_field = doc.get("kind_field")
    if kind_field is not None:
        kind_field = _word(kind_field, "kind_field")
        if kind_field not in fields or not fields[kind_field].kinds:
            raise _bad("kind_field must name an enum field that declares kinds")
    for name, spec in fields.items():
        if spec.kinds and name != kind_field:
            raise _bad(f"field {name} declares kinds but is not the kind_field")
    return SelectorDescriptor(fields=fields, identity=identity, template_node=template_node, kind_field=kind_field)


def shipped_descriptors(directory: Path | None = None) -> dict[str, SelectorDescriptor]:
    """The selector descriptor of every shipped protocol description, by the binding key the description names.

    A description with no binding or no ``result_selectors`` block is not listed: its adapter takes no selectors. A
    malformed block raises ``DescriptorError``; the description does not load either (``description.parse_description``).
    """
    found: dict[str, SelectorDescriptor] = {}
    for path in sorted((directory or PROTOCOL_DIR).glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(doc, Mapping) and doc.get("binding") and "result_selectors" in doc:
            found[str(doc["binding"])] = parse_descriptor(doc["result_selectors"])
    return found


_SHIPPED: dict[Path, dict[str, SelectorDescriptor]] = {}


def _cached_shipped() -> dict[str, SelectorDescriptor]:
    if PROTOCOL_DIR not in _SHIPPED:
        _SHIPPED[PROTOCOL_DIR] = shipped_descriptors()
    return _SHIPPED[PROTOCOL_DIR]


# ------------------------------------------------------------------------------------------------------ selectors

def normalize(descriptor: SelectorDescriptor, name: str, raw: Any) -> dict[str, str]:
    """One selector, checked against the descriptor, with defaults filled. Raises ``SelectorError``."""
    if not isinstance(raw, Mapping):
        raise SelectorError("selector_malformed", f"the selector of result {name} must be a mapping")
    unknown = sorted(str(key) for key in raw if key not in descriptor.fields)
    if unknown:
        raise SelectorError("selector_malformed", f"the selector of result {name} has fields its adapter does not take: {unknown}")
    out: dict[str, str] = {}
    for field_name, spec in descriptor.fields.items():
        value = raw.get(field_name)
        if value is None:
            if spec.required:
                raise SelectorError("selector_malformed", f"the selector of result {name} needs {field_name}")
            out[field_name] = spec.default  # type: ignore[assignment]
            continue
        if not isinstance(value, str) or not value or len(value) > MAX_TEXT:
            raise SelectorError("selector_malformed", f"{field_name} of result {name} must be a short non-empty string")
        if spec.type == "enum" and value not in spec.values:
            raise SelectorError("selector_malformed", f"{field_name} of result {name} must be one of {list(spec.values)}")
        out[field_name] = value
    return out


def _identity(descriptor: SelectorDescriptor, selector: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(selector[name] for name in descriptor.identity)


def check_binding(descriptor: SelectorDescriptor, outputs: Mapping[str, Any], block: Mapping[str, Any], *,
                  max_selectors: int = DEFAULT_MAX_SELECTORS) -> dict[str, dict[str, str]]:
    """The normalized selectors of one binding, by result name, or ``{}`` if the binding declares none.

    Declared selectors must name declared outputs, cover every output, stay within ``max_selectors`` and not repeat an
    identity. Raises ``SelectorError``. This is the shape check that runs at load and at save.
    """
    raw = block.get("selectors")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping) or not raw:
        raise SelectorError("selector_malformed", "selectors must be a non-empty mapping of result name to selector")
    if len(raw) > max_selectors:
        raise SelectorError("selector_count", f"a binding has {len(raw)} selectors; the limit is {max_selectors}")
    for name in raw:
        if name not in outputs:
            raise SelectorError("selector_unknown_result", f"selectors name result {name}, which the contract does not declare")
    missing = [name for name in outputs if name not in raw]
    if missing:
        raise SelectorError("selector_missing_result", f"result {missing[0]} has no selector; give every declared result one")
    selectors = {str(name): normalize(descriptor, str(name), spec) for name, spec in raw.items()}
    seen: dict[tuple[str, ...], str] = {}
    for name, selector in selectors.items():
        place = _identity(descriptor, selector)
        if place in seen:
            raise SelectorError("selector_duplicate", f"results {seen[place]} and {name} read the same place; "
                                                      "each result needs its own selector")
        seen[place] = name
    return selectors


def _declared_type(spec: Any) -> str:
    return str(spec.get("media_type") or "") if isinstance(spec, Mapping) else ""


def ambiguous_outputs(outputs: Mapping[str, Any]) -> list[str]:
    """The names of declared outputs that share a media type with another one. Nothing in the answer tells such outputs
    apart except a selector of its own."""
    by_type: dict[str, list[str]] = {}
    for name, spec in outputs.items():
        by_type.setdefault(_declared_type(spec), []).append(str(name))
    return [name for names in by_type.values() if len(names) > 1 for name in names]


def _links(inputs: Any) -> bool:
    """Whether a node's inputs hold an edge from another node: a ``[node id, output index]`` pair."""
    if not isinstance(inputs, Mapping):
        return False
    return any(isinstance(value, list) and len(value) == 2 and isinstance(value[0], (str, int))
               and isinstance(value[1], int) for value in inputs.values())


def _mapped_node(mapping: Any) -> str | None:
    if not isinstance(mapping, Mapping):
        return None
    if "node" in mapping:
        return str(mapping["node"])
    path = mapping.get("path")
    return str(path).split(".", 1)[0] if isinstance(path, str) and path else None


def check_echo(descriptor: SelectorDescriptor, selectors: Mapping[str, Mapping[str, str]], *, template: Mapping[str, Any],
               block: Mapping[str, Any], inputs: Mapping[str, Any]) -> None:
    """Refuse a selector whose node is not a producer: absent from the template, with no edge into it (it holds only its
    own settings), or written from an input that carries media (it would hand the source back as a result)."""
    if descriptor.template_node is None:
        return
    source_nodes: set[str] = set()
    binding_map = block.get("map")
    if isinstance(binding_map, Mapping):
        for input_name, mappings in binding_map.items():
            spec = inputs.get(input_name)
            if isinstance(spec, Mapping) and spec.get("type") in MEDIA_INPUT_TYPES and isinstance(mappings, list):
                source_nodes.update(node for node in map(_mapped_node, mappings) if node is not None)
    for name, selector in selectors.items():
        node_id = selector[descriptor.template_node]
        node = template.get(node_id)
        if not isinstance(node, Mapping):
            raise SelectorError("selector_unknown_node", f"the selector of result {name} names a node the template does not have")
        if node_id in source_nodes or not _links(node.get("inputs")):
            raise SelectorError("selector_echoes_source", f"the selector of result {name} reads a node that holds a source, "
                                                          "not one that makes a result")


def check_for_save(descriptor: SelectorDescriptor | None, manifest: Mapping[str, Any], binding_key: str, *,
                   template: Mapping[str, Any] | None = None, max_selectors: int = DEFAULT_MAX_SELECTORS,
                   ) -> dict[str, dict[str, str]]:
    """The contract rules for a new or changed binding (D5). Raises ``SelectorError`` with a clear reason.

    ``descriptor`` is the selector form of the binding's adapter, ``None`` if the adapter takes none: then the binding
    must declare none. Otherwise the binding must declare selectors, the shape check passes, and, when the template is
    given, no selector reads a node that is not a producer. Outputs that share a media type and cannot be told apart are
    refused with their own reason.
    """
    block = (manifest.get("bindings") or {}).get(binding_key)
    if not isinstance(block, Mapping):
        raise SelectorError("selector_malformed", f"the manifest has no binding {binding_key}")
    if descriptor is None:
        if "selectors" in block:
            raise SelectorError("selector_malformed", f"binding {binding_key} has selectors, but its adapter takes none")
        return {}
    outputs = manifest.get("outputs") or {}
    if "selectors" not in block:
        ambiguous = ambiguous_outputs(outputs)
        if ambiguous:
            raise SelectorError("results_ambiguous", f"results {ambiguous} have the same type and nothing tells them apart; "
                                                     f"give binding {binding_key} a selector for each")
        raise SelectorError("selectors_missing", f"binding {binding_key} declares no output selectors; a new contract must say "
                                                 "where each result comes from")
    selectors = check_binding(descriptor, outputs, block, max_selectors=max_selectors)
    if template is not None:
        check_echo(descriptor, selectors, template=template, block=block, inputs=manifest.get("inputs") or {})
    return selectors


def check_manifest(manifest: Mapping[str, Any], descriptors: Mapping[str, SelectorDescriptor] | None = None) -> None:
    """The load-time check of every binding that declares selectors, against its adapter's descriptor. A binding with
    none is a legacy contract and loads. Raises ``SelectorError``."""
    bindings = manifest.get("bindings")
    outputs = manifest.get("outputs")
    if not isinstance(bindings, Mapping) or not isinstance(outputs, Mapping):
        return
    known = _cached_shipped() if descriptors is None else descriptors
    for key, block in bindings.items():
        if not isinstance(block, Mapping) or "selectors" not in block:
            continue
        descriptor = known.get(key)
        if descriptor is None:
            raise SelectorError("selector_malformed", f"binding {key} has selectors, but its adapter takes none")
        check_binding(descriptor, outputs, block)


# ---------------------------------------------------------------------------------------------- the legacy flag

def legacy_collection_enabled(config: Mapping[str, Any]) -> bool:
    """Whether a binding with no selectors may still be read by the adapter's legacy collection. On unless the trusted
    runtime config says ``workflow_execution.legacy_output_collection: false``."""
    section = config.get(CONFIG_SECTION) if isinstance(config, Mapping) else None
    value = section.get(LEGACY_FLAG) if isinstance(section, Mapping) else None
    if value is not None and not isinstance(value, bool) and repr(value) not in _WARNED_FLAGS:
        # A value that is not true or false (a quoted "false", a 0) does not turn the collection off. Say so once.
        _WARNED_FLAGS.add(repr(value))
        logging.getLogger(__name__).warning(
            "%s.%s is %r, not true or false: the legacy output collection stays on", CONFIG_SECTION, LEGACY_FLAG, value)
    return value is not False


_WARNED_FLAGS: set[str] = set()


# ------------------------------------------------------------------------------------------------- the fetch gate

class FetchRefused(RuntimeError):
    """A fetch broke a deployment limit (D6). ``code`` names which."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FetchGate:
    """At most ``max_concurrent_fetches`` result fetches at once in one event loop, each bounded by ``fetch_timeout_s``.

    The time covers waiting for a slot too, so a full gate cannot hold a run for longer than the limit.
    """

    def __init__(self) -> None:
        self._slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, tuple[int, asyncio.Semaphore]] = weakref.WeakKeyDictionary()

    def _semaphore(self, limit: int) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        held = self._slots.get(loop)
        if held is None or held[0] != limit:
            held = (limit, asyncio.Semaphore(limit))
            self._slots[loop] = held
        return held[1]

    @contextlib.asynccontextmanager
    async def slot(self, limits: Any) -> AsyncGenerator[None]:
        """Hold a fetch slot for the body. ``limits`` has ``max_concurrent_fetches`` and ``fetch_timeout_s``."""
        semaphore = self._semaphore(limits.max_concurrent_fetches)
        try:
            async with asyncio.timeout(limits.fetch_timeout_s):
                async with semaphore:
                    yield
        except TimeoutError:
            raise FetchRefused("fetch_timeout", f"fetching the results took longer than {limits.fetch_timeout_s} s") from None


FETCH_GATE = FetchGate()

