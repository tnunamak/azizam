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

The module is pure: callers inject lookups, nothing here reads config, the
registry, or the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping

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

# Top-level response fields this module writes. None uses a vivid_fish name.
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


# Workflow manifest intent -> default metadata. Manifest I/O refines it below.
_INTENTS: dict[str, ModelMetadata] = {
    "image.generate": _md("image", ["text"], ["image"]),
    "image.edit": _md("image", ["text", "image"], ["image"]),
    "image.analyze": _md("image", ["image"], ["text"]),
    "video.generate": _md("video", ["text"], ["video"]),
    "video.edit": _md("video", ["text", "video"], ["video"]),
    "audio.generate": _md("audio", ["text"], ["audio"]),
    "text.embeddings": _md("embedding", ["text"], ["embedding"]),
}

# Stable-alias operation (gateway/stable_aliases.OPERATIONS) -> metadata.
_OPERATIONS: dict[str, ModelMetadata] = {
    "chat": _md("language", ["text"], ["text"]),
    "embeddings": _md("embedding", ["text"], ["embedding"]),
    "images": _md("image", ["text"], ["image"]),
    "image_edits": _md("image", ["text", "image"], ["image"]),
    "videos": _md("video", ["text"], ["video"]),
    "speech": _md("audio", ["text"], ["audio"]),
    "transcriptions": _md("audio", ["audio"], ["text"]),
    "audio_generate": _md("audio", ["text"], ["audio"]),
    "rerank": _md("reranking", ["text"], ["text"]),
    "decisions": _md("decision", ["text"], ["text"]),
}

_LANGUAGE = _OPERATIONS["chat"]
_DECISION = _OPERATIONS["decisions"]

# Service-registry capability -> metadata, first match wins. These are the
# gateway's own capability names (`backends.<id>.capabilities`), not engines.
_SERVICE_CAPABILITIES: tuple[tuple[str, ModelMetadata], ...] = (
    ("chat", _LANGUAGE),
    ("completions", _LANGUAGE),
    ("embeddings", _OPERATIONS["embeddings"]),
    ("rerank", _OPERATIONS["rerank"]),
    ("decisions", _DECISION),
)

# Major media type of a manifest output -> output modality.
_MEDIA_MAJOR_TO_MODALITY = {"image": "image", "video": "video", "audio": "audio", "text": "text"}


def from_intent(intent: str | None, manifest: Mapping[str, Any] | None = None) -> ModelMetadata | None:
    """Metadata for a workflow from its intent, refined by the manifest's real I/O."""
    base = _INTENTS.get(str(intent or ""))
    if base is None:
        return None
    if not isinstance(manifest, Mapping):
        return base
    inputs = list(base.input_modalities)
    if intent == "video.generate" and _asset_inputs(manifest):
        # A generator's asset input is its conditioning image unless the
        # manifest declares another media type for it.
        inputs.extend(_asset_input_modality(spec, default="image") for spec in _asset_inputs(manifest))
    outputs = _manifest_output_modalities(manifest) or list(base.output_modalities)
    return replace(
        base,
        input_modalities=_ordered(inputs, INPUT_MODALITIES),
        output_modalities=_ordered(outputs, OUTPUT_MODALITIES),
    )


def from_operation(operation: str) -> ModelMetadata | None:
    return _OPERATIONS.get(operation)


def from_service_capabilities(capabilities: Iterable[str] | None) -> ModelMetadata | None:
    caps = {str(c).strip().lower() for c in (capabilities or ()) if isinstance(c, str)}
    for capability, metadata in _SERVICE_CAPABILITIES:
        if capability in caps:
            if metadata.type == "language" and "vision" in caps:
                return _with_inputs(metadata, ("image",))
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


def apply_declared(derived: ModelMetadata | None, declared: Mapping[str, Any] | None) -> ModelMetadata | None:
    """Overlay operator-declared values on derived ones. Declared wins per field.

    Values outside the contract vocabulary are ignored rather than emitted.
    The `vision` tag and `image` input are then made consistent.
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


def base_metadata(entry: Mapping[str, Any], sources: MetadataSources) -> ModelMetadata | None:
    """Metadata for an entry from its own signals plus its declaration, ignoring alias targets."""
    model_id = str(entry.get("id") or "")
    intent = entry.get("vivid_fish_intent")
    derived = None
    if intent:
        derived = from_intent(intent, sources.manifest(str(entry.get("vivid_fish_workflow_id") or "")))
    if derived is None and sources.origins.get(model_id) == ORIGIN_DECISION:
        derived = _DECISION
    if derived is None:
        derived = from_service_capabilities(sources.service_capabilities(model_id))
    if derived is None and entry.get("vivid_fish_current_loaded"):
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
    pool = [e for e in (entries if catalog is None else catalog) if isinstance(e, Mapping)]
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
    resolves_to = entry.get("vivid_fish_resolves_to")
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
    return apply_declared(from_intent(manifest.get("intent"), manifest), sources.declared(model_id))


def _asset_inputs(manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    inputs = manifest.get("inputs")
    if not isinstance(inputs, Mapping):
        return []
    return [spec for spec in inputs.values() if isinstance(spec, Mapping) and spec.get("type") == "asset"]


def _asset_input_modality(spec: Mapping[str, Any], *, default: str) -> str:
    major = str(spec.get("media_type") or "").split("/", 1)[0].strip().lower()
    return major if major in INPUT_MODALITIES else default


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
    """`vision` tag <=> `image` input. A declared tag adds the input and vice versa."""
    if "vision" in metadata.tags and "image" not in metadata.input_modalities:
        metadata = _with_inputs(metadata, ("image",))
    if "image" in metadata.input_modalities and "vision" not in metadata.tags:
        metadata = replace(metadata, tags=_ordered([*metadata.tags, "vision"], TAGS))
    return metadata
