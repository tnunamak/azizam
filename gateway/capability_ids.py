"""Capability ids and the one mapping from a route intent to a capability.

A leaf module: the catalog (``model_presentation``) and the access-plan
evaluator (``access_plans``) both read it, so neither imports the other.
"""

from collections.abc import Iterable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

# Labels, presentation, optional policy protocols and capability spellings are shipped data.
# Callers share read-only tables loaded once at import, in file order.
CAPABILITY_WORDS_PATH = Path(__file__).resolve().parent.parent / "registry" / "capability_words.yml"


def _modality_words(document: Mapping[str, Any], path: Path) -> frozenset[str]:
    if "modality_words" not in document:
        return frozenset()
    words = document["modality_words"]
    if (not isinstance(words, list) or not words
            or any(not isinstance(word, str) or not word.strip() or word != word.strip() for word in words)):
        raise ValueError(f"{path.name}: modality_words must be a non-empty list of non-empty words")
    if len(set(words)) != len(words):
        raise ValueError(f"{path.name}: modality_words must not contain duplicates")
    return frozenset(words)


def load_modality_words(path: Path = CAPABILITY_WORDS_PATH) -> frozenset[str]:
    """The registered wire-facing modality vocabulary; not capability aliases or MIME types."""
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ValueError(f"{path.name}: modality_words could not be loaded: {error}") from error
    if not isinstance(document, dict):
        raise ValueError(f"{path.name}: modality_words requires a mapping document")
    return _modality_words(document, path)


def load_media_type_modalities(path: Path = CAPABILITY_WORDS_PATH) -> Mapping[str, str]:
    """Read explicit MIME-to-modality declarations; media words are validated together."""
    from gateway.strict_yaml import safe_load_strict

    document = safe_load_strict(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path.name}: media_type_modalities requires a mapping document")
    words = _modality_words(document, path)
    rows = document.get("media_type_modalities", {})
    if not isinstance(rows, dict):
        raise ValueError(f"{path.name}: media_type_modalities must be a mapping")
    for media_type, modality in rows.items():
        if (not isinstance(media_type, str) or len(media_type.split("/")) != 2
                or any(not part or part != part.strip() for part in media_type.split("/"))
                or not isinstance(modality, str) or modality not in words):
            raise ValueError(f"{path.name}: media_type_modalities has an invalid media type or modality")
    return MappingProxyType(dict(rows))


def _load_capability_tables(path: Path) -> tuple[
    Mapping[str, str], Mapping[str, str], Mapping[str, str], Mapping[str, str],
    Mapping[str, str], Mapping[str, str], Mapping[str, str],
    Mapping[str, str], Mapping[str, Any], Mapping[str, tuple[str, ...]], Mapping[str, int], frozenset[str],
    Mapping[str, str], Mapping[str, str],
    Mapping[str, str], Mapping[str, str], Mapping[str, str],
]:
    """Validate capability, presentation and classification rows together."""
    def invalid(word: object, reason: str) -> ValueError:
        return ValueError(f"{path.name}: word {word!r} {reason}")

    def invalid_row(row: object, reason: str) -> ValueError:
        return ValueError(f"{path.name}: row {row!r} in capabilities {reason}")

    def invalid_table(section: str, row: object, reason: str) -> ValueError:
        return ValueError(f"{path.name}: row {row!r} in {section} {reason}")

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise invalid("<document>", f"could not be loaded: {error}") from error
    if not isinstance(document, dict):
        raise invalid("<document>", "must be a mapping")
    for key in document:
        if key not in {"schema_version", "capabilities", "protocol_words", "route_intents",
                       "type_capabilities", "audio_rule", "modality_words", "media_type_modalities"}:
            raise invalid_table("document", key, "is an unknown top-level key")
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise invalid("schema_version", "must be the integer 1")

    if "modality_words" in document:
        _modality_words(document, path)

    rows = document.get("capabilities")
    if not isinstance(rows, list) or not rows:
        raise invalid_row("capabilities", "must be a non-empty list")
    member_labels: dict[str, str] = {}
    task_names: dict[str, str] = {}
    models_explainers: dict[str, str] = {}
    try_help: dict[str, str] = {}
    request_nouns: dict[str, str] = {}
    labels: dict[str, str] = {}
    policy_protocols: dict[str, str] = {}
    icons: dict[str, str] = {}
    aliases: dict[str, str] = {}
    summaries: dict[str, str] = {}
    hidden_by: dict[str, tuple[str, ...]] = {}
    display_order: dict[str, int] = {}
    alias_hides_names: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise invalid_row(index, "must be a mapping")
        for key in row:
            if key not in {"id", "label", "policy_protocol", "icon", "recommended_alias", "summary", "hidden_by", "display_order", "alias_hides_declared_names", "member_label", "task_name", "models_explainer", "try_help", "request_noun"}:
                raise invalid_row(index, f"has unknown key {key!r}")
        capability = row.get("id")
        if not isinstance(capability, str) or not capability.strip():
            raise invalid_row(index, "id must be a non-empty string")
        if capability in labels:
            raise invalid_row(index, f"has duplicate id {capability!r}")
        label = row.get("label")
        if not isinstance(label, str) or not label.strip():
            raise invalid_row(index, "label must be a non-empty string")
        for field in ("member_label", "task_name", "models_explainer", "try_help", "request_noun"):
            if field in row:
                value = row[field]
                if not isinstance(value, str) or not value.strip() or value != value.strip():
                    raise invalid_row(index, f"{field} must be a non-empty string without surrounding whitespace")
        member_labels[capability] = row.get("member_label", label)
        task_names[capability] = row.get("task_name", member_labels[capability])
        for field, table in (("models_explainer", models_explainers), ("try_help", try_help), ("request_noun", request_nouns)):
            if field in row:
                table[capability] = row[field]
        labels[capability] = label
        rank = row.get("display_order", index - 1)
        if type(rank) is not int or rank < 0:
            raise invalid_row(index, "display_order must be a non-negative integer")
        if rank in display_order.values():
            raise invalid_row(index, f"has duplicate display_order {rank!r}")
        display_order[capability] = rank
        if "policy_protocol" in row:
            protocol = row["policy_protocol"]
            if not isinstance(protocol, str):
                raise invalid_row(index, "policy_protocol must be a string")
            policy_protocols[capability] = protocol
        for field in ("icon", "recommended_alias", "summary"):
            if field in row and (not isinstance(row[field], str) or not row[field].strip()):
                raise invalid_row(index, f"{field} must be a non-empty string")
        icons[capability] = row.get("icon", capability)
        if "recommended_alias" in row:
            aliases[capability] = row["recommended_alias"]
        if "summary" in row:
            summaries[capability] = row["summary"]
        if "alias_hides_declared_names" in row:
            if type(row["alias_hides_declared_names"]) is not bool:
                raise invalid_row(index, "alias_hides_declared_names must be true or false")
            if row["alias_hides_declared_names"]:
                alias_hides_names.add(capability)
        if "hidden_by" in row:
            hidden = row["hidden_by"]
            if not isinstance(hidden, list):
                raise invalid_row(index, "hidden_by must be a list of capability ids")
            hidden_by[capability] = tuple(hidden)

    # References may point forward to a capability declared later in the file.
    for index, icon in enumerate(icons.values(), start=1):
        if icon not in labels:
            raise invalid_row(index, f"icon names unknown capability {icon!r}")

    for index, row in enumerate(rows, start=1):
        for capability in hidden_by.get(row["id"], ()):
            if not isinstance(capability, str) or capability not in labels:
                raise invalid_row(index, f"hidden_by names unknown capability {capability!r}")

    types = document.get("type_capabilities", {})
    if not isinstance(types, dict):
        raise invalid_table("type_capabilities", 0, "must be a mapping")
    for kind, capability in types.items():
        if not isinstance(kind, str) or not kind.strip():
            raise invalid_table("type_capabilities", kind, "type must be a non-empty string")
        if not isinstance(capability, str) or capability not in labels:
            raise invalid_table("type_capabilities", kind, f"names unknown capability {capability!r}")

    audio = {}
    if "audio_rule" in document:
        rule = document["audio_rule"]
        if not isinstance(rule, dict):
            raise invalid_table("audio_rule", 0, "must be a mapping")
        required = {"type", "input", "absent_output", "result", "subtypes", "fallback"}
        missing, unknown = required - rule.keys(), rule.keys() - required
        if missing:
            raise invalid_table("audio_rule", 0, f"missing fields: {', '.join(sorted(missing))}")
        if unknown:
            raise invalid_table("audio_rule", 0, f"unknown fields: {', '.join(sorted(map(str, unknown)))}")
        for field in ("type", "input", "absent_output", "result", "fallback"):
            value = rule[field]
            if not isinstance(value, str) or not value.strip():
                raise invalid_table("audio_rule", field, "must be a non-empty string")
            if field in {"result", "fallback"} and value not in labels:
                raise invalid_table("audio_rule", field, f"names unknown capability {value!r}")
            audio[field] = value
        subtypes = rule["subtypes"]
        if not isinstance(subtypes, list) or not subtypes:
            raise invalid_table("audio_rule", "subtypes", "must be a non-empty list of capability ids")
        for capability in subtypes:
            if not isinstance(capability, str) or capability not in labels:
                raise invalid_table("audio_rule", "subtypes", f"names unknown capability {capability!r}")
        audio["subtypes"] = tuple(subtypes)

    tables: list[dict[str, str]] = []
    for section in ("protocol_words", "route_intents"):
        words = document.get(section)
        if not isinstance(words, dict):
            raise invalid(section, "must be a mapping")
        table: dict[str, str] = {}
        for word, capability in words.items():
            if not isinstance(word, str) or not word.strip():
                raise invalid(word, f"in {section} must be a non-empty string")
            if not isinstance(capability, str) or capability not in labels:
                raise invalid(word, f"in {section} names unknown capability {capability!r}")
            table[word] = capability
        tables.append(table)
    protocol, extra_intents = tables
    for word, capability in extra_intents.items():
        if word in protocol and protocol[word] != capability:
            raise invalid(word, "has conflicting capabilities in protocol_words and route_intents")
    return (MappingProxyType(labels), MappingProxyType(policy_protocols),
            MappingProxyType(protocol), MappingProxyType({**protocol, **extra_intents}),
            MappingProxyType(icons), MappingProxyType(aliases), MappingProxyType(summaries),
            MappingProxyType(dict(types)), MappingProxyType(audio), MappingProxyType(hidden_by),
            MappingProxyType(display_order), frozenset(alias_hides_names),
            MappingProxyType(member_labels), MappingProxyType(task_names),
            MappingProxyType(models_explainers), MappingProxyType(try_help), MappingProxyType(request_nouns))


def _load_capability_data(path: Path) -> tuple[
    Mapping[str, str], Mapping[str, str], Mapping[str, str], Mapping[str, str]
]:
    """Preserve the four-map loader shape while validating every row field."""
    return _load_capability_tables(path)[:4]


def load_capability_presentation(path: Path = CAPABILITY_WORDS_PATH) -> tuple[
    Mapping[str, str], Mapping[str, str], Mapping[str, str]
]:
    """Read immutable icon, recommended-alias and summary maps from capability rows."""
    return _load_capability_tables(path)[4:7]


def load_capability_classification(path: Path = CAPABILITY_WORDS_PATH) -> tuple[
    Mapping[str, str], Mapping[str, Any], Mapping[str, tuple[str, ...]],
]:
    """Read immutable type, audio and suppression decisions; omitted sections are empty."""
    return _load_capability_tables(path)[7:10]


def load_capability_display_order(path: Path = CAPABILITY_WORDS_PATH) -> Mapping[str, int]:
    """Read immutable ranks for declared capabilities; omitted ranks use row order."""
    return _load_capability_tables(path)[10]


def load_capability_alias_name_hiding(path: Path = CAPABILITY_WORDS_PATH) -> frozenset[str]:
    """Capabilities whose alias rows show no title, display name or name the model itself declares."""
    return _load_capability_tables(path)[11]


def load_capability_member_words(path: Path = CAPABILITY_WORDS_PATH) -> tuple[Mapping[str, str], Mapping[str, str]]:
    """Read effective member labels and task names, in capability row order."""
    return _load_capability_tables(path)[12:14]


def load_capability_screen_words(path: Path = CAPABILITY_WORDS_PATH) -> tuple[
    Mapping[str, str], Mapping[str, str], Mapping[str, str],
]:
    """Read optional Models explainers, Try help and request nouns in row order."""
    return _load_capability_tables(path)[14:17]


def load_capability_words(path: Path = CAPABILITY_WORDS_PATH) -> tuple[Mapping[str, str], Mapping[str, str]]:
    """Read the word maps after validating their capability rows; preserve the two-map API."""
    _, _, protocol, intents = _load_capability_data(path)
    return protocol, intents


(CAPABILITY_LABELS, POLICY_PROTOCOLS, PROTOCOL_CAPABILITIES, INTENT_CAPABILITIES,
 CAPABILITY_ICON_KEYS, RECOMMENDED_ALIASES, CAPABILITY_DESCRIPTIONS,
 TYPE_CAPABILITIES, AUDIO_RULE, CAPABILITY_HIDDEN_BY, CAPABILITY_DISPLAY_ORDER, CAPABILITY_ALIAS_HIDES_NAMES,
 CAPABILITY_MEMBER_LABELS, CAPABILITY_TASK_NAMES) = _load_capability_tables(CAPABILITY_WORDS_PATH)[:14]

MODALITY_WORDS = load_modality_words()
MEDIA_TYPE_MODALITIES = load_media_type_modalities()

# The kinds a registered model may declare: the one capability it serves, with
# its builder label. The kinds they share with the catalog take the catalog's
# word. The catalog and engine pages use the same capability label.
# The catalog binds a kinded model only the capabilities of its kind. A model
# with no kind, or a kind not listed here, keeps every capability its engine
# offers. These are the families registered models bind.
MODEL_KIND_LABELS = {
    "chat": CAPABILITY_LABELS["chat"], "embed": CAPABILITY_LABELS["embed"],
    "speech": CAPABILITY_LABELS["speech"], "transcribe": CAPABILITY_LABELS["transcribe"],
}
MODEL_KINDS = tuple(MODEL_KIND_LABELS)


# The OpenAI-compatible endpoints the gateway's lifecycle hop serves (``/internal/litellm/local/{backend}/v1<path>``),
# by the capability that needs them. This is the one table: the hop routes by it, and the service registry refuses a
# transition-enabled member that declares a capability with no row here (that member would be reached only by an
# endpoint the hop answers with 404).
INNER_HOP_ENDPOINTS: dict[str, tuple[str, ...]] = {
    "chat": ("/chat/completions", "/completions"),
    "embed": ("/embeddings",),
}
# The admission intent and resource class of each inner-hop endpoint, as the outer routes name them.
INNER_HOP_ADMISSION: dict[str, tuple[str, str]] = {
    "/chat/completions": ("chat.generate", "chat"),
    "/completions": ("chat.generate", "chat"),
    "/embeddings": ("text.embeddings", "embeddings"),
}


def capability_of_word(word: str) -> str:
    """The capability a record's capability word stands for (an unknown word stands for itself)."""
    text = str(word or "").strip()
    return INTENT_CAPABILITIES.get(text, text)


def inner_hop_endpoint_served(endpoint: str) -> bool:
    """Whether any capability maps to the inner-hop endpoint ``endpoint`` (a path such as ``/embeddings``)."""
    return any(endpoint in paths for paths in INNER_HOP_ENDPOINTS.values())


def inner_hop_endpoints_for(words: Iterable[str]) -> frozenset[str]:
    """The inner-hop endpoints that a record with these capability words is reached by."""
    served: set[str] = set()
    for word in words:
        served.update(INNER_HOP_ENDPOINTS.get(capability_of_word(word), ()))
    return frozenset(served)


def capabilities_without_inner_endpoint(words: Iterable[str]) -> list[str]:
    """The capability words (as declared) that stand for a real capability no inner-hop endpoint serves.

    A word that maps to no capability (the ``openai`` compatibility marker, for one) asks for no endpoint, so it is
    not refused: only a word that resolves to a ``CAPABILITY_LABELS`` key without a row in ``INNER_HOP_ENDPOINTS`` is.
    """
    refused: list[str] = []
    for word in words:
        capability = capability_of_word(word)
        if capability in CAPABILITY_LABELS and capability not in INNER_HOP_ENDPOINTS:
            refused.append(word)
    return refused


def offered_model_kinds(words: Iterable[str]) -> list[str]:
    """The kinds that an engine's record capability words serve, in MODEL_KINDS order.

    One mapping from an engine's own words to a kind, so the definitions answer,
    the save check and the probe's suggestion read the engine the same way.
    """
    served = {PROTOCOL_CAPABILITIES.get(word) for word in words}
    return [kind for kind in MODEL_KINDS if kind in served]
