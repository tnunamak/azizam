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

Operation, service-capability and capability-intent metadata are read once from shipped registry rows.
Other lookups are injected; this module reads no owner config and makes no network calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

import yaml

from gateway.manifests import asset_leaf_specs
from gateway.stable_aliases import OPERATIONS as ALIAS_OPERATIONS

MODEL_TYPES: tuple[str, ...] = (
    "language", "embedding", "reranking", "image", "video", "audio", "decision",
)
INPUT_MODALITIES: tuple[str, ...] = ("text", "image", "audio", "video", "file")
OUTPUT_MODALITIES: tuple[str, ...] = ("text", "image", "audio", "video", "embedding")
TAGS: tuple[str, ...] = ("tool-use", "reasoning", "vision", "file-input")

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
    return ModelMetadata(type_, _ordered(inputs, INPUT_MODALITIES), _ordered(outputs, OUTPUT_MODALITIES))


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
            if type_ not in MODEL_TYPES:
                raise ValueError(f"{operation}: unknown type: {type_!r}")
            modalities: dict[str, list[str]] = {}
            for key, vocabulary in (("input_modalities", INPUT_MODALITIES),
                                    ("output_modalities", OUTPUT_MODALITIES)):
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
    if not isinstance(match_type, str) or match_type not in MODEL_TYPES:
        raise invalid("augmentation.match_type", f"unknown model type: {match_type!r}")
    inputs = augmentation.get("input_modalities")
    if (not isinstance(inputs, list) or not inputs
            or any(not isinstance(item, str) or item not in INPUT_MODALITIES for item in inputs)):
        raise invalid("augmentation.input_modalities", "must be a nonempty list of known input modalities")
    return ServiceCapabilityIndex(
        MappingProxyType(found),
        ServiceCapabilityAugmentation(capability, match_type, _ordered(inputs, INPUT_MODALITIES)),
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
        if not isinstance(type_, str) or type_ not in MODEL_TYPES:
            raise invalid(f"{at}.model_metadata.type", f"unknown type: {type_!r}")
        modalities: dict[str, list[str]] = {}
        for key, vocabulary in (("input_modalities", INPUT_MODALITIES),
                                ("output_modalities", OUTPUT_MODALITIES)):
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
    document = mapping(document, "document", {"schema_version", "exact", "families"})
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

# Major media type of a manifest output -> output modality.
_MEDIA_MAJOR_TO_MODALITY = {"image": "image", "video": "video", "audio": "audio", "text": "text"}


def type_of(input_modalities: Iterable[str], output_modalities: Iterable[str]) -> str | None:
    """The model type the modalities say. A media output is that medium; embeddings are embeddings; a text answer from
    some other medium (a transcript, a description) is typed by that input medium; plain text to text is a language
    model. None when nothing is declared."""
    inputs, outputs = set(input_modalities), set(output_modalities)
    produced = [m for m in OUTPUT_MODALITIES if m in outputs and m != INPUT_MODALITIES[0]]
    if produced:
        return produced[0]
    if INPUT_MODALITIES[0] in outputs:
        taken = [m for m in INPUT_MODALITIES[1:] if m in inputs and m in MODEL_TYPES]
        return taken[0] if taken else _LANGUAGE.type
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
            inputs.append(INPUT_MODALITIES[0])
    inputs.extend(_asset_input_modality(spec) or "" for spec in _asset_inputs(manifest))
    outputs = _manifest_output_modalities(manifest)
    ordered_in, ordered_out = _ordered(inputs, INPUT_MODALITIES), _ordered(outputs, OUTPUT_MODALITIES)
    type_ = type_of(ordered_in, ordered_out)
    return None if type_ is None else ModelMetadata(type_, ordered_in, ordered_out)


def from_capability(
    intent: str | None,
    input_modalities: Iterable[str] = (),
    output_modalities: Iterable[str] = (),
) -> ModelMetadata | None:
    """Metadata for one gateway capability: its intent's type and its declared modalities."""
    intent = str(intent or "")
    base = _CAPABILITY_INTENTS.get(intent) or next(
        (metadata for prefix, metadata in _CAPABILITY_INTENT_FAMILIES.items() if intent.startswith(prefix)), None
    )
    if base is None:
        return None
    return replace(
        base,
        input_modalities=_ordered(input_modalities, INPUT_MODALITIES) or base.input_modalities,
        output_modalities=_ordered(output_modalities, OUTPUT_MODALITIES) or base.output_modalities,
    )


def from_fields(fields: Mapping[str, Any] | None) -> ModelMetadata | None:
    """Read back the response fields `ModelMetadata.to_fields` wrote. None when untyped."""
    if not isinstance(fields, Mapping) or fields.get("type") not in MODEL_TYPES:
        return None
    architecture = fields.get("architecture") if isinstance(fields.get("architecture"), Mapping) else {}
    max_tokens = fields.get("max_tokens")
    return ModelMetadata(
        type=fields["type"],
        input_modalities=_ordered(architecture.get("input_modalities") or (), INPUT_MODALITIES),
        output_modalities=_ordered(architecture.get("output_modalities") or (), OUTPUT_MODALITIES),
        max_tokens=max_tokens if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) else None,
        tags=_ordered(fields.get("tags") or (), TAGS),
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
        input_modalities=_ordered((m for p in known for m in p.input_modalities), INPUT_MODALITIES),
        output_modalities=_ordered((m for p in known for m in p.output_modalities), OUTPUT_MODALITIES),
        max_tokens=caps.pop() if len(caps) == 1 else None,
        tags=_ordered((t for p in known for t in p.tags), TAGS),
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
        input_modalities=shared("input_modalities", INPUT_MODALITIES),
        output_modalities=shared("output_modalities", OUTPUT_MODALITIES),
        max_tokens=caps.pop() if len(caps) == 1 else None,
        tags=shared("tags", TAGS),
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
    Only `language` entries keep tags; for them the `vision` tag and `image`
    input are then made consistent.
    """
    result = derived or ModelMetadata()
    if isinstance(declared, Mapping):
        updates: dict[str, Any] = {}
        type_ = declared.get("type")
        if isinstance(type_, str) and type_ in MODEL_TYPES:
            updates["type"] = type_
        for key, vocab in (("input_modalities", INPUT_MODALITIES), ("output_modalities", OUTPUT_MODALITIES), ("tags", TAGS)):
            if isinstance(declared.get(key), (list, tuple)):
                updates[key] = _ordered(declared[key], vocab)
        max_tokens = declared.get("max_tokens")
        if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0:
            updates["max_tokens"] = max_tokens
        result = replace(result, **updates)
    if result.type is None:
        return None
    if result.type != "language":
        # Tags are chat-model capability flags. Media models state image input
        # through their modalities only.
        return replace(result, tags=())
    return _consistent_vision(result)


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
    """The modality an asset input accepts: the shared kind of its ``accepts`` list (a schema 1 ``media_type`` counts as a
    one-item list). None when it names no single kind."""
    accepted = spec.get("accepts") or ([spec["media_type"]] if spec.get("media_type") else ())
    majors = {str(item).split("/", 1)[0].strip().lower() for item in accepted if str(item).strip()}
    major = next(iter(majors)) if len(majors) == 1 else ""
    return major if major in INPUT_MODALITIES else None


def asset_media_type(spec: Mapping[str, Any]) -> str | None:
    """Media kind of one asset input, as ``image/*``. The document says it by what the input accepts: one type as it is,
    several as their shared kind; an input that accepts nothing it can name has no media type."""
    accepted = [str(item).strip() for item in spec.get("accepts") or () if str(item).strip()]
    if len(accepted) == 1:
        return accepted[0]
    majors = {item.split("/", 1)[0].lower() for item in accepted}
    if len(majors) == 1 and next(iter(majors)) in INPUT_MODALITIES:
        return f"{next(iter(majors))}/*"
    return None


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
        if major in _MEDIA_MAJOR_TO_MODALITY:
            found.append(_MEDIA_MAJOR_TO_MODALITY[major])
        elif media_type == "application/json":
            found.append("text")
    return found


def _with_inputs(metadata: ModelMetadata, extra: Iterable[str]) -> ModelMetadata:
    return replace(metadata, input_modalities=_ordered([*metadata.input_modalities, *extra], INPUT_MODALITIES))


def _consistent_vision(metadata: ModelMetadata) -> ModelMetadata:
    """For a language model, `vision` tag <=> `image` input. A declared tag adds the input and vice versa."""
    if "vision" in metadata.tags and "image" not in metadata.input_modalities:
        metadata = _with_inputs(metadata, ("image",))
    if "image" in metadata.input_modalities and "vision" not in metadata.tags:
        metadata = replace(metadata, tags=_ordered([*metadata.tags, "vision"], TAGS))
    return metadata
