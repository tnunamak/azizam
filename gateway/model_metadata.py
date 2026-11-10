"""Self-describing model-list metadata (`type`, `architecture`, `max_tokens`, `tags`).

Implements the model metadata contract documented in docs/api/MODEL_METADATA.md:
every OpenAI-shaped `/v1/models` entry the gateway can classify carries a
Vercel-style `type`, OpenRouter-style `architecture.{input,output}_modalities`,
and, only when known, `max_tokens` and `tags`.

Derivation is engine-agnostic. It reads only gateway vocabulary that already
exists: workflow intents and manifest I/O, stable-alias operations, the origin
of an entry (LLM catalog, current-loaded, decision runtime), and the
capabilities a service-registry record declares. Operator declarations under
the `model_metadata` key are DATA and always win over derived values.

The vocabulary contract is read from the published models schema; derivation rules,
operation, service-capability and capability-intent metadata come from registry rows.
Other lookups are injected; this module reads no owner config and makes no network calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

import yaml

from gateway.manifests import asset_input_modalities, asset_leaf_specs
from gateway.product_contracts import _MODEL_SCHEMA_PATH, load_model_metadata_vocabularies
from gateway.stable_aliases import OPERATIONS as ALIAS_OPERATIONS

_METADATA_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "registry" / "capability_intent_metadata.yml"


@dataclass(frozen=True)
class ModalityTypeRule:
    modality: str
    type: str


@dataclass(frozen=True)
class TagInputImplication:
    type: str
    tag: str
    input_modality: str


@dataclass(frozen=True)
class MetadataContract:
    types: tuple[str, ...]
    input_modalities: tuple[str, ...]
    output_modalities: tuple[str, ...]
    tags: tuple[str, ...]
    tag_types: tuple[str, ...]
    scalar_input: str
    fallback_output: str
    default_type: str
    output_priority: tuple[ModalityTypeRule, ...]
    input_priority: tuple[ModalityTypeRule, ...]
    implications: tuple[TagInputImplication, ...]


class _MetadataContractLoader(yaml.SafeLoader):
    """Safe YAML with duplicate/non-string mapping keys refused at every depth."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[str, Any]:
        found: dict[str, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise ValueError("contract mapping keys must be strings")
            if key in found:
                raise ValueError(f"duplicate contract field: {key!r}")
            found[key] = self.construct_object(value_node, deep=deep)
        return found


def load_metadata_contract(
    path: str | Path = _METADATA_CONTRACT_PATH,
    *,
    schema_path: str | Path = _MODEL_SCHEMA_PATH,
) -> MetadataContract:
    """Read ordered schema vocabularies and validate the separate semantic rules."""
    path = Path(path)
    vocabularies = load_model_metadata_vocabularies(schema_path)

    def invalid(at: str, reason: str) -> ValueError:
        return ValueError(f"{path}: {at}: {reason}")

    def mapping(value: Any, at: str, keys: set[str]) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise invalid(at, "must be a mapping")
        unknown, missing = set(value) - keys, keys - set(value)
        if unknown:
            raise invalid(at, f"unknown fields: {sorted(map(str, unknown))}")
        if missing:
            raise invalid(at, f"missing fields: {sorted(missing)}")
        return value

    def roster(value: Any, at: str, *, empty: bool = False) -> tuple[str, ...]:
        if not isinstance(value, list) or (not value and not empty):
            raise invalid(at, "must be an ordered list" if empty else "must be a nonempty ordered list")
        found: list[str] = []
        for item in value:
            if not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", item):
                raise invalid(at, f"values must be lowercase data names, not paths: {item!r}")
            if item in found:
                raise invalid(at, f"duplicate value: {item!r}")
            found.append(item)
        return tuple(found)

    def reference(
        value: Any, at: str, vocabulary: tuple[str, ...], *, source: str | Path = schema_path,
    ) -> str:
        if not isinstance(value, str) or value not in vocabulary:
            raise invalid(at, f"unknown value: {value!r}; not allowed by {source}")
        return value

    try:
        document = yaml.load(path.read_text(encoding="utf-8"), Loader=_MetadataContractLoader)
    except (OSError, UnicodeError, yaml.YAMLError, ValueError) as exc:
        raise invalid("document", str(exc)) from exc
    document = mapping(document, "document", {"schema_version", "exact", "families", "contract"})["contract"]
    document = mapping(document, "document", {
        "schema_version", "tag_types", "derivation", "implications",
    })
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise invalid("schema_version", "must be 1")
    types = vocabularies["types"]
    inputs = vocabularies["input_modalities"]
    outputs = vocabularies["output_modalities"]
    tags = vocabularies["tags"]
    tag_types = roster(document["tag_types"], "tag_types", empty=True)
    for type_ in tag_types:
        reference(type_, "tag_types", types)
    derivation = mapping(document["derivation"], "derivation", {
        "scalar_input", "fallback_output", "default_type", "output_priority", "input_priority",
    })
    scalar_input = reference(derivation["scalar_input"], "derivation.scalar_input", inputs)
    fallback_output = reference(derivation["fallback_output"], "derivation.fallback_output", outputs)
    default_type = reference(derivation["default_type"], "derivation.default_type", types)

    def priority(key: str, vocabulary: tuple[str, ...]) -> tuple[ModalityTypeRule, ...]:
        rows = derivation[key]
        if not isinstance(rows, list):
            raise invalid(f"derivation.{key}", "must be an ordered list")
        found: list[ModalityTypeRule] = []
        seen: set[str] = set()
        for index, row in enumerate(rows):
            at = f"derivation.{key}[{index}]"
            row = mapping(row, at, {"modality", "type"})
            modality = reference(row["modality"], f"{at}.modality", vocabulary)
            type_ = reference(row["type"], f"{at}.type", types)
            if modality in seen:
                raise invalid(at, f"duplicate modality: {modality!r}")
            if key == "output_priority" and modality == fallback_output:
                raise invalid(at, "fallback output cannot also be a priority output")
            seen.add(modality)
            found.append(ModalityTypeRule(modality, type_))
        return tuple(found)

    output_priority = priority("output_priority", outputs)
    input_priority = priority("input_priority", inputs)
    rows = document["implications"]
    if not isinstance(rows, list):
        raise invalid("implications", "must be an ordered list")
    implications: list[TagInputImplication] = []
    for index, row in enumerate(rows):
        at = f"implications[{index}]"
        row = mapping(row, at, {"type", "tag", "input_modality"})
        type_ = reference(row["type"], f"{at}.type", types)
        implication = TagInputImplication(
            reference(type_, f"{at}.type", tag_types, source=f"contract.tag_types in {path}"),
            reference(row["tag"], f"{at}.tag", tags),
            reference(row["input_modality"], f"{at}.input_modality", inputs),
        )
        if implication in implications:
            raise invalid(at, "duplicate implication")
        implications.append(implication)
    return MetadataContract(
        types, inputs, outputs, tags, tag_types, scalar_input, fallback_output, default_type,
        output_priority, input_priority, tuple(implications),
    )


# One immutable release contract. Compatibility exports preserve the public roster API.
METADATA_CONTRACT = load_metadata_contract()
MODEL_TYPES = METADATA_CONTRACT.types
INPUT_MODALITIES = METADATA_CONTRACT.input_modalities
OUTPUT_MODALITIES = METADATA_CONTRACT.output_modalities
TAGS = METADATA_CONTRACT.tags

# Operator declaration key on a role, a `models:` entry, or a service-registry
# model record. Its value holds any subset of DECLARABLE_FIELDS.
DECLARATION_KEY = "model_metadata"
DECLARABLE_FIELDS: tuple[str, ...] = (
    "type", "input_modalities", "output_modalities", "max_tokens", "tags",
)

# Top-level response fields this module writes.
OUTPUT_FIELDS: tuple[str, ...] = ("type", "architecture", "max_tokens", "tags")

ORIGIN_LLM_CATALOG = "llm-catalog"
ORIGIN_DECISION = "decision"


@dataclass(frozen=True)
class ModelMetadata:
    type: str | None = None
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    max_tokens: int | None = None
    tags: tuple[str, ...] = field(default=())

    def to_fields(self) -> dict[str, Any]:
        """The response fields. Unknown values are omitted, never guessed."""
        out: dict[str, Any] = {}
        if self.type is None:
            return out
        out["type"] = self.type
        out["architecture"] = {
            "input_modalities": list(self.input_modalities),
            "output_modalities": list(self.output_modalities),
        }
        if self.max_tokens is not None:
            out["max_tokens"] = self.max_tokens
        if self.tags:
            out["tags"] = list(self.tags)
        return out


def _ordered(values: Iterable[Any], vocabulary: tuple[str, ...]) -> tuple[str, ...]:
    """Keep known vocabulary values, deduplicated, in the vocabulary's order."""
    present = {str(v).strip().lower() for v in values if isinstance(v, str)}
    return tuple(v for v in vocabulary if v in present)


def _md(type_: str, inputs: Iterable[str], outputs: Iterable[str]) -> ModelMetadata:
    return ModelMetadata(
        type_, _ordered(inputs, METADATA_CONTRACT.input_modalities),
        _ordered(outputs, METADATA_CONTRACT.output_modalities),
    )


# Stable-alias operation -> metadata, declared once on its registered route.
_OPERATION_ROWS_PATH = Path(__file__).resolve().parents[1] / "registry" / "protocol_frontends.yml"


def load_operation_metadata(path: str | Path = _OPERATION_ROWS_PATH) -> Mapping[str, ModelMetadata]:
    """Read and validate shipped operation rows; fail closed rather than invent metadata.

    The frontend-shape loader exposes only the separate ``frontends`` rows.
    Read ``records[].registered_routes`` with the same ``yaml.safe_load``.
    Roster order is observable through ``operations_of``; YAML row order is not.
    """
    document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, Mapping) or not isinstance(document.get("records"), list):
        raise ValueError("operation metadata requires a records list")
    found: dict[str, ModelMetadata] = {}
    for record in document["records"]:
        if not isinstance(record, Mapping):
            raise ValueError("operation metadata record must be a mapping")
        routes = record.get("registered_routes", [])
        if not isinstance(routes, list):
            raise ValueError("registered_routes must be a list")
        for row in routes:
            if not isinstance(row, Mapping):
                raise ValueError("registered route must be a mapping")
            if "operation" not in row and "model_metadata" not in row:
                continue
            if "operation" not in row or "model_metadata" not in row:
                raise ValueError("registered route needs both operation and model_metadata")
            operation = row["operation"]
            if not isinstance(operation, str) or operation not in ALIAS_OPERATIONS:
                raise ValueError(f"unknown operation: {operation!r}")
            if operation in found:
                raise ValueError(f"duplicate operation: {operation}")
            metadata = row["model_metadata"]
            if not isinstance(metadata, Mapping):
                raise ValueError(f"{operation}: model_metadata must be a mapping")
            type_ = metadata.get("type")
            if type_ not in METADATA_CONTRACT.types:
                raise ValueError(f"{operation}: unknown type: {type_!r}")
            modalities: dict[str, list[str]] = {}
            for key, vocabulary in (("input_modalities", METADATA_CONTRACT.input_modalities),
                                    ("output_modalities", METADATA_CONTRACT.output_modalities)):
                values = metadata.get(key)
                if not isinstance(values, list):
                    raise ValueError(f"{operation}: {key} must be a list")
                if any(not isinstance(value, str) or value not in vocabulary for value in values):
                    raise ValueError(f"{operation}: unknown {key}: {values!r}")
                modalities[key] = values
            found[operation] = _md(type_, modalities["input_modalities"], modalities["output_modalities"])
    missing = tuple(operation for operation in ALIAS_OPERATIONS if operation not in found)
    if missing:
        raise ValueError(f"missing operations: {', '.join(missing)}")
    return MappingProxyType({operation: found[operation] for operation in ALIAS_OPERATIONS})


# Build once from immutable release data; values are frozen and modalities tuples.
OPERATION_METADATA = load_operation_metadata()

_LANGUAGE = OPERATION_METADATA["chat"]
_DECISION = OPERATION_METADATA["decisions"]

# Service-registry capabilities map to operation metadata in release-file order.
_SERVICE_CAPABILITY_ROWS_PATH = Path(__file__).resolve().parents[1] / "registry" / "service_capability_metadata.yml"


@dataclass(frozen=True)
class ServiceCapabilityAugmentation:
    capability: str
    match_type: str
    input_modalities: tuple[str, ...]


@dataclass(frozen=True)
class ServiceCapabilityIndex:
    rows: Mapping[str, ModelMetadata]
    augmentation: ServiceCapabilityAugmentation


def load_service_capability_metadata(
    path: str | Path = _SERVICE_CAPABILITY_ROWS_PATH,
) -> ServiceCapabilityIndex:
    """Load an immutable first-match index; malformed release data is never ignored."""
    path = Path(path)

    def invalid(at: str, reason: str) -> ValueError:
        return ValueError(f"{path}: {at}: {reason}")

    def mapping(value: Any, at: str, keys: set[str]) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise invalid(at, "must be a mapping")
        unknown = set(value) - keys
        if unknown:
            raise invalid(at, f"unknown keys: {', '.join(sorted(map(str, unknown)))}")
        return value

    def name(value: Any, at: str, seen: Mapping[str, Any]) -> str:
        if not isinstance(value, str) or not value.strip():
            raise invalid(at, "must be a nonempty string")
        normalized = value.strip().lower()
        if normalized in seen:
            raise invalid(at, f"duplicate capability after normalization: {normalized!r}")
        if value != normalized:
            raise invalid(at, "must already be stripped and lowercase")
        return value

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise invalid("document", f"cannot read service capability metadata: {exc}") from exc
    document = mapping(document, "document", {"schema_version", "capabilities", "augmentation"})
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise invalid("schema_version", "must be 1")
    rows = document.get("capabilities")
    if not isinstance(rows, list) or not rows:
        raise invalid("capabilities", "must be a nonempty ordered list")
    found: dict[str, ModelMetadata] = {}
    for index, row in enumerate(rows):
        at = f"capabilities[{index}]"
        row = mapping(row, at, {"capability", "operation"})
        capability = name(row.get("capability"), f"{at}.capability", found)
        operation = row.get("operation")
        if not isinstance(operation, str) or operation not in OPERATION_METADATA:
            raise invalid(f"{at}.operation", f"unknown operation: {operation!r}")
        found[capability] = OPERATION_METADATA[operation]

    augmentation = mapping(
        document.get("augmentation"), "augmentation", {"capability", "match_type", "input_modalities"},
    )
    capability = name(augmentation.get("capability"), "augmentation.capability", {})
    if capability in found:
        raise invalid("augmentation.capability", "must not also be a matching row")
    match_type = augmentation.get("match_type")
    if not isinstance(match_type, str) or match_type not in METADATA_CONTRACT.types:
        raise invalid("augmentation.match_type", f"unknown model type: {match_type!r}")
    inputs = augmentation.get("input_modalities")
    if (not isinstance(inputs, list) or not inputs
            or any(not isinstance(item, str) or item not in METADATA_CONTRACT.input_modalities for item in inputs)):
        raise invalid("augmentation.input_modalities", "must be a nonempty list of known input modalities")
    return ServiceCapabilityIndex(
        MappingProxyType(found),
        ServiceCapabilityAugmentation(capability, match_type, _ordered(inputs, METADATA_CONTRACT.input_modalities)),
    )


# Loaded once, like OPERATION_METADATA; owner configuration cannot replace release data.
SERVICE_CAPABILITY_METADATA = load_service_capability_metadata()

# Capability intents and ordered prefix families are immutable release data.
_CAPABILITY_INTENT_ROWS_PATH = Path(__file__).resolve().parents[1] / "registry" / "capability_intent_metadata.yml"


@dataclass(frozen=True)
class CapabilityIntentIndex:
    exact: Mapping[str, ModelMetadata]
    families: Mapping[str, ModelMetadata]


def load_capability_intent_metadata(
    path: str | Path = _CAPABILITY_INTENT_ROWS_PATH,
) -> CapabilityIntentIndex:
    """Load exact matches and first-match families once, without intent-specific code."""
    path = Path(path)

    def invalid(at: str, reason: str) -> ValueError:
        return ValueError(f"{path}: {at}: {reason}")

    def mapping(value: Any, at: str, keys: set[str]) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise invalid(at, "must be a mapping")
        unknown = set(value) - keys
        if unknown:
            raise invalid(at, f"unknown keys: {', '.join(sorted(map(str, unknown)))}")
        return value

    def metadata(row: Mapping[str, Any], at: str) -> ModelMetadata:
        if ("operation" in row) == ("model_metadata" in row):
            raise invalid(at, "needs exactly one of operation or model_metadata")
        if "operation" in row:
            operation = row["operation"]
            if not isinstance(operation, str) or operation not in OPERATION_METADATA:
                raise invalid(f"{at}.operation", f"unknown operation: {operation!r}")
            return OPERATION_METADATA[operation]
        literal = mapping(
            row["model_metadata"], f"{at}.model_metadata",
            {"type", "input_modalities", "output_modalities"},
        )
        type_ = literal.get("type")
        if not isinstance(type_, str) or type_ not in METADATA_CONTRACT.types:
            raise invalid(f"{at}.model_metadata.type", f"unknown type: {type_!r}")
        modalities: dict[str, list[str]] = {}
        for key, vocabulary in (("input_modalities", METADATA_CONTRACT.input_modalities),
                                ("output_modalities", METADATA_CONTRACT.output_modalities)):
            values = literal.get(key)
            if (not isinstance(values, list)
                    or any(not isinstance(value, str) or value not in vocabulary for value in values)):
                raise invalid(f"{at}.model_metadata.{key}", "must be a list of known modalities")
            modalities[key] = values
        return _md(type_, modalities["input_modalities"], modalities["output_modalities"])

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise invalid("document", f"cannot read capability intent metadata: {exc}") from exc
    document = mapping(document, "document", {"schema_version", "exact", "families", "contract"})
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise invalid("schema_version", "must be 1")

    def rows(section: str, name_key: str) -> Mapping[str, ModelMetadata]:
        entries = document.get(section)
        if not isinstance(entries, list):
            raise invalid(section, "must be an ordered list")
        found: dict[str, ModelMetadata] = {}
        for index, row in enumerate(entries):
            at = f"{section}[{index}]"
            row = mapping(row, at, {name_key, "operation", "model_metadata"})
            name = row.get(name_key)
            if not isinstance(name, str) or not name.strip():
                raise invalid(f"{at}.{name_key}", "must be a nonempty string")
            if name in found:
                raise invalid(f"{at}.{name_key}", f"duplicate {name_key}: {name!r}")
            found[name] = metadata(row, at)
        return MappingProxyType(found)

    return CapabilityIntentIndex(rows("exact", "intent"), rows("families", "prefix"))


# Loaded once; mapping insertion order preserves first-match family precedence.
_CAPABILITY_INTENT_INDEX = load_capability_intent_metadata()
_CAPABILITY_INTENTS = _CAPABILITY_INTENT_INDEX.exact
_CAPABILITY_INTENT_FAMILIES = _CAPABILITY_INTENT_INDEX.families


def _capability_types_by_io() -> dict[tuple[frozenset[str], frozenset[str]], str]:
    """Unique types of exact declared modality pairs, derived from both legacy tables."""
    candidates: dict[tuple[frozenset[str], frozenset[str]], set[str]] = {}
    for metadata in (
        *_CAPABILITY_INTENTS.values(),
        *_CAPABILITY_INTENT_FAMILIES.values(),
    ):
        pair = (frozenset(metadata.input_modalities), frozenset(metadata.output_modalities))
        if all(pair) and metadata.type is not None:
            candidates.setdefault(pair, set()).add(metadata.type)
    return {pair: next(iter(types)) for pair, types in candidates.items() if len(types) == 1}


_CAPABILITY_TYPES_BY_IO = _capability_types_by_io()

def type_of(input_modalities: Iterable[str], output_modalities: Iterable[str]) -> str | None:
    """Use explicit output precedence, then input precedence for the fallback output."""
    inputs, outputs = set(input_modalities), set(output_modalities)
    contract = METADATA_CONTRACT
    for rule in contract.output_priority:
        if rule.modality in outputs:
            return rule.type
    if contract.fallback_output in outputs:
        return next(
            (rule.type for rule in contract.input_priority if rule.modality in inputs),
            contract.default_type,
        )
    return None


def from_contract(manifest: Mapping[str, Any] | None) -> ModelMetadata | None:
    """Metadata for a workflow from its contract's real inputs and outputs. The document says what it takes (the media
    types its asset inputs accept, a text input) and what it makes (its outputs' media types); nothing is assumed."""
    if not isinstance(manifest, Mapping):
        return None
    inputs: list[str] = []
    declared = manifest.get("inputs")
    if isinstance(declared, Mapping):
        if any(isinstance(spec, Mapping) and spec.get("type") == "string" for spec in declared.values()):
            inputs.append(METADATA_CONTRACT.scalar_input)
    inputs.extend(_asset_input_modality(spec) or "" for spec in _asset_inputs(manifest))
    outputs = _manifest_output_modalities(manifest)
    ordered_in = _ordered(inputs, METADATA_CONTRACT.input_modalities)
    ordered_out = _ordered(outputs, METADATA_CONTRACT.output_modalities)
    type_ = type_of(ordered_in, ordered_out)
    return None if type_ is None else ModelMetadata(type_, ordered_in, ordered_out)


def from_capability(
    intent: str | None,
    input_modalities: Iterable[str] = (),
    output_modalities: Iterable[str] = (),
) -> ModelMetadata | None:
    """Use an unambiguous declared I/O pair, else the legacy intent or modality fallback."""
    inputs = _ordered(input_modalities, METADATA_CONTRACT.input_modalities)
    outputs = _ordered(output_modalities, METADATA_CONTRACT.output_modalities)
    type_ = _CAPABILITY_TYPES_BY_IO.get((frozenset(inputs), frozenset(outputs)))
    if type_ is not None:
        return ModelMetadata(type_, inputs, outputs)
    intent = str(intent or "")
    base = _CAPABILITY_INTENTS.get(intent) or next(
        (metadata for prefix, metadata in _CAPABILITY_INTENT_FAMILIES.items() if intent.startswith(prefix)), None
    )
    if base is None:
        type_ = type_of(inputs, outputs)
        return None if type_ is None else ModelMetadata(type_, inputs, outputs)
    return replace(
        base,
        input_modalities=inputs or base.input_modalities,
        output_modalities=outputs or base.output_modalities,
    )


def from_fields(fields: Mapping[str, Any] | None) -> ModelMetadata | None:
    """Read back the response fields `ModelMetadata.to_fields` wrote. None when untyped."""
    if not isinstance(fields, Mapping) or fields.get("type") not in METADATA_CONTRACT.types:
        return None
    architecture = fields.get("architecture") if isinstance(fields.get("architecture"), Mapping) else {}
    max_tokens = fields.get("max_tokens")
    return ModelMetadata(
        type=fields["type"],
        input_modalities=_ordered(architecture.get("input_modalities") or (), METADATA_CONTRACT.input_modalities),
        output_modalities=_ordered(architecture.get("output_modalities") or (), METADATA_CONTRACT.output_modalities),
        max_tokens=max_tokens if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) else None,
        tags=_ordered(fields.get("tags") or (), METADATA_CONTRACT.tags),
    )


def from_operation(operation: str) -> ModelMetadata | None:
    return OPERATION_METADATA.get(operation)


def operations_of(type_: Any, input_modalities: Any = None, among: Iterable[str] | None = None) -> tuple[str, ...]:
    """The stable-alias operations (of ``among``, default all) whose metadata has this model ``type_``.

    Known input modalities narrow the match to the operations that take exactly those inputs; when none matches
    them, or they are unknown, every operation of the type is returned."""
    if not isinstance(type_, str):
        return ()
    of_type = [op for op in (OPERATION_METADATA if among is None else among)
               if op in OPERATION_METADATA and OPERATION_METADATA[op].type == type_]
    if isinstance(input_modalities, (list, tuple)) and input_modalities:
        wanted = set(input_modalities)
        narrowed = [op for op in of_type if set(OPERATION_METADATA[op].input_modalities) == wanted]
        if narrowed:
            return tuple(narrowed)
    return tuple(of_type)


def from_service_capabilities(capabilities: Iterable[str] | None) -> ModelMetadata | None:
    caps = {str(c).strip().lower() for c in (capabilities or ()) if isinstance(c, str)}
    index = SERVICE_CAPABILITY_METADATA
    for capability, metadata in index.rows.items():
        if capability in caps:
            augmentation = index.augmentation
            if metadata.type == augmentation.match_type and augmentation.capability in caps:
                return _with_inputs(metadata, augmentation.input_modalities)
            return metadata
    return None


def from_origin(origin: str | None) -> ModelMetadata | None:
    if origin == ORIGIN_DECISION:
        return _DECISION
    if origin == ORIGIN_LLM_CATALOG:
        return _LANGUAGE
    return None


def merge(parts: Iterable[ModelMetadata | None]) -> ModelMetadata | None:
    """Combine the metadata of every operation an alias resolves to.

    The alias takes the type its targets share; with no shared type the type
    is unknown and the result is None. Modalities and tags are unioned (the
    alias accepts whatever any of its routes accept); max_tokens survives only
    when every target agrees.
    """
    known = [p for p in parts if p is not None and p.type]
    if not known or len({p.type for p in known}) != 1:
        return None
    caps = {p.max_tokens for p in known}
    return ModelMetadata(
        type=known[0].type,
        input_modalities=_ordered((m for p in known for m in p.input_modalities), METADATA_CONTRACT.input_modalities),
        output_modalities=_ordered((m for p in known for m in p.output_modalities), METADATA_CONTRACT.output_modalities),
        max_tokens=caps.pop() if len(caps) == 1 else None,
        tags=_ordered((t for p in known for t in p.tags), METADATA_CONTRACT.tags),
    )


def common(parts: Iterable[ModelMetadata | None]) -> ModelMetadata | None:
    """What every part claims: their shared type, and only the modalities and
    tags all of them have. max_tokens survives only when every part agrees.
    """
    known = [p for p in parts if p is not None and p.type]
    if not known or len({p.type for p in known}) != 1:
        return None
    caps = {p.max_tokens for p in known}

    def shared(attr: str, vocabulary: tuple[str, ...]) -> tuple[str, ...]:
        sets = [set(getattr(p, attr)) for p in known]
        return _ordered(set.intersection(*sets), vocabulary)

    return ModelMetadata(
        type=known[0].type,
        input_modalities=shared("input_modalities", METADATA_CONTRACT.input_modalities),
        output_modalities=shared("output_modalities", METADATA_CONTRACT.output_modalities),
        max_tokens=caps.pop() if len(caps) == 1 else None,
        tags=shared("tags", METADATA_CONTRACT.tags),
    )


def loaded_model_metadata(loaded_ids: Iterable[str], sources: MetadataSources) -> ModelMetadata | None:
    """Metadata of the loaded LLM, from the declarations of the ids it is served under.

    Only ids with an operator declaration count; with several, the result is
    what they all claim. None when no loaded id is declared, so callers keep
    their conservative default rather than guess.
    """
    parts = []
    for model_id in sorted({str(i) for i in loaded_ids if i}):
        declared = sources.declared(model_id)
        if declared is None:
            continue
        derived = from_service_capabilities(sources.service_capabilities(model_id)) or _LANGUAGE
        parts.append(apply_declared(derived, declared))
    return common(parts)


def without_reasoning(metadata: ModelMetadata) -> ModelMetadata:
    """For a route whose request policy turns thinking off."""
    return replace(metadata, tags=tuple(t for t in metadata.tags if t != "reasoning"))


def apply_declared(derived: ModelMetadata | None, declared: Mapping[str, Any] | None) -> ModelMetadata | None:
    """Overlay operator-declared values on derived ones. Declared wins per field.

    Values outside the contract vocabulary are ignored rather than emitted.
    Tag-bearing types and bidirectional tag/input implications are declared
    by the release contract, independently of vocabulary ordering.
    """
    result = derived or ModelMetadata()
    if isinstance(declared, Mapping):
        updates: dict[str, Any] = {}
        type_ = declared.get("type")
        if isinstance(type_, str) and type_ in METADATA_CONTRACT.types:
            updates["type"] = type_
        for key, vocab in (
            ("input_modalities", METADATA_CONTRACT.input_modalities),
            ("output_modalities", METADATA_CONTRACT.output_modalities),
            ("tags", METADATA_CONTRACT.tags),
        ):
            if isinstance(declared.get(key), (list, tuple)):
                updates[key] = _ordered(declared[key], vocab)
        max_tokens = declared.get("max_tokens")
        if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0:
            updates["max_tokens"] = max_tokens
        result = replace(result, **updates)
    if result.type is None:
        return None
    if result.type not in METADATA_CONTRACT.tag_types:
        return replace(result, tags=())
    return _consistent_implications(result)


@dataclass(frozen=True)
class MetadataSources:
    """Lookups the caller injects. Each returns None when it knows nothing."""

    declared: Callable[[str], Mapping[str, Any] | None] = lambda _model_id: None
    service_capabilities: Callable[[str], Iterable[str] | None] = lambda _model_id: None
    manifest: Callable[[str], Mapping[str, Any] | None] = lambda _workflow_id: None
    # Manifest of the workflow a public model id runs, for alias targets the
    # list does not carry as entries (e.g. video.edit workflows).
    workflow_for_model: Callable[[str], Mapping[str, Any] | None] = lambda _model_id: None
    origins: Mapping[str, str] = field(default_factory=dict)
    # What a dynamic alias (`current`, `current-fast`) inherits from the loaded
    # model, keyed by alias id. It wins over the alias's own declaration, which
    # stays the fallback for when nothing declared is loaded.
    inherited: Mapping[str, ModelMetadata] = field(default_factory=dict)


def base_metadata(entry: Mapping[str, Any], sources: MetadataSources) -> ModelMetadata | None:
    """Metadata for an entry from its own signals plus its declaration, ignoring alias targets."""
    model_id = str(entry.get("id") or "")
    if model_id in sources.inherited:
        return sources.inherited[model_id]
    intent = entry.get("azizam_intent")
    derived = None
    if intent:
        derived = from_contract(sources.manifest(str(entry.get("azizam_workflow_id") or ""))) or from_capability(intent)
    if derived is None and sources.origins.get(model_id) == ORIGIN_DECISION:
        derived = _DECISION
    if derived is None:
        derived = from_service_capabilities(sources.service_capabilities(model_id))
    if derived is None and entry.get("azizam_current_loaded"):
        derived = _LANGUAGE
    if derived is None:
        derived = from_origin(sources.origins.get(model_id))
    return apply_declared(derived, sources.declared(model_id))


def annotate_entries(
    entries: Iterable[Any],
    sources: MetadataSources,
    *,
    catalog: Iterable[Any] | None = None,
) -> None:
    """Add the contract fields to each OpenAI-shaped entry in place.

    `catalog` is the full, unfiltered list used to resolve alias targets, so a
    caller's profile cannot change what an alias reports. It defaults to
    `entries`.
    """
    entries = [e for e in entries if isinstance(e, dict)]
    pool = [dict(e) for e in (entries if catalog is None else catalog) if isinstance(e, Mapping)]
    by_id: dict[str, ModelMetadata | None] = {}
    for item in [*pool, *entries]:
        model_id = str(item.get("id") or "")
        if model_id and by_id.get(model_id) is None:
            by_id[model_id] = base_metadata(item, sources)
    for entry in entries:
        found = _metadata_for(entry, by_id, sources)
        if found is not None:
            entry.update(found.to_fields())


def metadata_index(entries: Iterable[Any], sources: MetadataSources) -> dict[str, dict[str, Any]]:
    """{model id: contract fields} for catalogs that are not OpenAI-shaped lists.

    Entries sharing an id are merged first, as `/v1/models` merges an alias
    into the listed entry of the same id.
    """
    merged: dict[str, dict[str, Any]] = {}
    for e in entries:
        if isinstance(e, Mapping) and e.get("id"):
            merged.setdefault(str(e["id"]), {}).update(e)
    copies = list(merged.values())
    annotate_entries(copies, sources)
    return {
        str(c["id"]): {key: c[key] for key in OUTPUT_FIELDS if key in c}
        for c in copies
    }


def _metadata_for(
    entry: Mapping[str, Any],
    by_id: Mapping[str, ModelMetadata | None],
    sources: MetadataSources,
) -> ModelMetadata | None:
    model_id = str(entry.get("id") or "")
    own = by_id.get(model_id)
    resolves_to = entry.get("azizam_resolves_to")
    if not isinstance(resolves_to, Mapping):
        return own
    targets = [(op, str(t)) for op, t in resolves_to.items() if t and str(t) != model_id]
    if not targets:
        return own
    # An alias is typed by what it resolves to: the target's own metadata when
    # the gateway lists it, else its workflow manifest, else the operation's default.
    alias = merge(
        by_id.get(target) or _workflow_target(target, sources) or from_operation(op)
        for op, target in targets
    )
    return apply_declared(alias, sources.declared(model_id)) if alias else own


def _workflow_target(model_id: str, sources: MetadataSources) -> ModelMetadata | None:
    manifest = sources.workflow_for_model(model_id)
    if not isinstance(manifest, Mapping):
        return None
    return apply_declared(from_contract(manifest), sources.declared(model_id))


def _asset_inputs(manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    inputs = manifest.get("inputs")
    if not isinstance(inputs, Mapping):
        return []
    return [leaf for spec in inputs.values() for leaf in asset_leaf_specs(spec)]


def _asset_input_modality(spec: Mapping[str, Any]) -> str | None:
    """One declared asset family, only when it is a single model input kind."""
    modalities = asset_input_modalities(spec)
    return modalities[0] if len(modalities) == 1 and modalities[0] in METADATA_CONTRACT.input_modalities else None


def asset_media_type(spec: Mapping[str, Any]) -> str | None:
    """One declared media type as written, or the shared family of several.

    Generic or mixed families have no common media type. A single explicit type
    (including */*) is preserved; a schema 1 media_type is also a declaration.
    """
    accepted = spec.get("accepts") or ([spec["media_type"]] if spec.get("media_type") else ())
    media_types = [accepted] if isinstance(accepted, str) else accepted
    accepted = [str(item).strip() for item in media_types if str(item).strip()]
    if len(accepted) == 1:
        return accepted[0]
    modality = _asset_input_modality(spec)
    return f"{modality}/*" if accepted and modality else None


def _manifest_output_modalities(manifest: Mapping[str, Any]) -> list[str]:
    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping):
        return []
    found: list[str] = []
    for spec in outputs.values():
        if not isinstance(spec, Mapping):
            continue
        media_type = str(spec.get("media_type") or "").strip().lower()
        major = media_type.split("/", 1)[0]
        # The IANA top-level type of a declared output names its modality when the
        # contract's output vocabulary has that word.
        if major in OUTPUT_MODALITIES:
            found.append(major)
        elif media_type == "application/json":
            found.append("text")
    return found


def _with_inputs(metadata: ModelMetadata, extra: Iterable[str]) -> ModelMetadata:
    return replace(
        metadata, input_modalities=_ordered(
            [*metadata.input_modalities, *extra], METADATA_CONTRACT.input_modalities,
        ),
    )


def _consistent_implications(metadata: ModelMetadata) -> ModelMetadata:
    """Close type-scoped tag/input equivalences; additions are finite and ordered."""
    inputs, tags = set(metadata.input_modalities), set(metadata.tags)
    rules = tuple(rule for rule in METADATA_CONTRACT.implications if rule.type == metadata.type)
    while True:
        size = len(inputs) + len(tags)
        for rule in rules:
            if rule.tag in tags or rule.input_modality in inputs:
                inputs.add(rule.input_modality)
                tags.add(rule.tag)
        if len(inputs) + len(tags) == size:
            break
    updates: dict[str, Any] = {}
    # Preserve unchanged fields exactly, as operator declarations already order them.
    if inputs != set(metadata.input_modalities):
        updates["input_modalities"] = _ordered(inputs, METADATA_CONTRACT.input_modalities)
    if tags != set(metadata.tags):
        updates["tags"] = _ordered(tags, METADATA_CONTRACT.tags)
    return replace(metadata, **updates) if updates else metadata
