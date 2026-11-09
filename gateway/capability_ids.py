"""Capability ids and the one mapping from a route intent to a capability.

A leaf module: the catalog (``model_presentation``) and the access-plan
evaluator (``access_plans``) both read it, so neither imports the other.
"""

from collections.abc import Iterable, Mapping
from pathlib import Path
from types import MappingProxyType

import yaml

# Labels, optional policy protocols and capability spellings are shipped data.
# Callers share read-only tables loaded once at import, in file order.
CAPABILITY_WORDS_PATH = Path(__file__).resolve().parent.parent / "registry" / "capability_words.yml"


def _load_capability_data(path: Path) -> tuple[
    Mapping[str, str], Mapping[str, str], Mapping[str, str], Mapping[str, str]
]:
    """Validate capability rows before resolving the two word maps."""
    def invalid(word: object, reason: str) -> ValueError:
        return ValueError(f"{path.name}: word {word!r} {reason}")

    def invalid_row(row: object, reason: str) -> ValueError:
        return ValueError(f"{path.name}: row {row!r} in capabilities {reason}")

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise invalid("<document>", f"could not be loaded: {error}") from error
    if not isinstance(document, dict):
        raise invalid("<document>", "must be a mapping")
    for key in document:
        if key not in {"schema_version", "capabilities", "protocol_words", "route_intents"}:
            raise invalid(key, "is an unknown top-level key")
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise invalid("schema_version", "must be the integer 1")

    rows = document.get("capabilities")
    if not isinstance(rows, list) or not rows:
        raise invalid_row("capabilities", "must be a non-empty list")
    labels: dict[str, str] = {}
    policy_protocols: dict[str, str] = {}
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise invalid_row(index, "must be a mapping")
        for key in row:
            if key not in {"id", "label", "policy_protocol"}:
                raise invalid_row(index, f"has unknown key {key!r}")
        capability = row.get("id")
        if not isinstance(capability, str) or not capability.strip():
            raise invalid_row(index, "id must be a non-empty string")
        if capability in labels:
            raise invalid_row(index, f"has duplicate id {capability!r}")
        label = row.get("label")
        if not isinstance(label, str) or not label.strip():
            raise invalid_row(index, "label must be a non-empty string")
        labels[capability] = label
        if "policy_protocol" in row:
            protocol = row["policy_protocol"]
            if not isinstance(protocol, str):
                raise invalid_row(index, "policy_protocol must be a string")
            policy_protocols[capability] = protocol

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
            MappingProxyType(protocol), MappingProxyType({**protocol, **extra_intents}))


def load_capability_words(path: Path = CAPABILITY_WORDS_PATH) -> tuple[Mapping[str, str], Mapping[str, str]]:
    """Read the word maps after validating their capability rows; preserve the two-map API."""
    _, _, protocol, intents = _load_capability_data(path)
    return protocol, intents


CAPABILITY_LABELS, POLICY_PROTOCOLS, PROTOCOL_CAPABILITIES, INTENT_CAPABILITIES = _load_capability_data(CAPABILITY_WORDS_PATH)

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
