"""Human catalog labels and placement from operator-authored service data."""

from collections.abc import Callable, Mapping
from ipaddress import ip_address
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any
from urllib.parse import urljoin, urlsplit

import yaml

from gateway import model_metadata
from gateway.capability_ids import (
    CAPABILITY_DESCRIPTIONS, CAPABILITY_ICON_KEYS, CAPABILITY_LABELS, INTENT_CAPABILITIES,
    PROTOCOL_CAPABILITIES, RECOMMENDED_ALIASES,
    TYPE_CAPABILITIES, AUDIO_RULE, CAPABILITY_HIDDEN_BY,
)
from gateway.service_registry import LEGACY_RESIDENCY_KEY, engine_base_address, host_port_origin, is_litellm_service_record, is_remote_provider_config


_PRESENTATION_WORDS_PATH = Path(__file__).resolve().parents[1] / "registry/presentation_words.yml"
_PRESENTATION_LIST_KEYS = (
    "provider_suffixes", "engine_prefixes", "engine_tails", "parenthesized_hardware",
    "hardware_label_suffixes", "bare_titles", "technical_patterns",
)
_PRESENTATION_KEYS = (
    "schema_version", *_PRESENTATION_LIST_KEYS, "capability_engine_suffixes", "acronyms", "unclassified",
)
_UNCLASSIFIED_KEYS = ("capability", "label", "icon", "service_capability", "summary")


def _load_presentation_words(path: Path) -> Mapping[str, Any]:
    """Read ordered vocabulary once; refuse malformed data with file/key evidence."""
    def fail(key, reason):
        raise ValueError(f"{path}: {key}: {reason}")

    def string(value, key):
        if not isinstance(value, str) or not value.strip():
            fail(key, "expected a non-empty string")
        return value

    def mapping(value, key):
        if not isinstance(value, dict) or not value:
            fail(key, "expected a non-empty mapping")
        for name in value:
            string(name, key)
        return value

    def keys(value, allowed, key):
        for name in value:
            if name not in allowed:
                fail(f"{key}.{name}" if key else name, "unknown key")
        for name in allowed:
            if name not in value:
                fail(f"{key}.{name}" if key else name, "missing key")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        fail("$", str(exc))
    raw = mapping(raw, "$")
    keys(raw, _PRESENTATION_KEYS, "")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        fail("schema_version", "expected 1")
    loaded: dict[str, Any] = {"schema_version": 1}
    for key in _PRESENTATION_LIST_KEYS:
        value = raw[key]
        if not isinstance(value, list) or not value:
            fail(key, "expected a non-empty list")
        loaded[key] = tuple(string(word, f"{key}[{index}]") for index, word in enumerate(value))
        seen = set()
        for index, word in enumerate(loaded[key]):
            if word in seen:
                fail(f"{key}[{index}]", "duplicate word")
            seen.add(word)
    for key in ("capability_engine_suffixes", "acronyms", "unclassified"):
        value = mapping(raw[key], key)
        if key == "unclassified":
            keys(value, _UNCLASSIFIED_KEYS, key)
        for name, word in value.items():
            string(word, f"{key}.{name}")
            if key == "capability_engine_suffixes" and name not in CAPABILITY_LABELS:
                fail(f"{key}.{name}", "unknown capability")
        loaded[key] = MappingProxyType(dict(value))
    if loaded["unclassified"]["service_capability"] not in CAPABILITY_ICON_KEYS:
        fail("unclassified.service_capability", "unknown capability")
    patterns = []
    for index, pattern in enumerate(loaded["technical_patterns"]):
        try:
            patterns.append(re.compile(pattern))
        except (re.error, OverflowError, RecursionError) as exc:
            fail(f"technical_patterns[{index}]", str(exc))
    loaded["technical_patterns"] = tuple(patterns)
    return MappingProxyType(loaded)


PRESENTATION_WORDS = _load_presentation_words(_PRESENTATION_WORDS_PATH)


def unclassified_capability_words() -> tuple[str, str]:
    """Read the generic capability label and icon from the loaded vocabulary."""
    row = PRESENTATION_WORDS["unclassified"]
    return row["label"], row["icon"]


# Words are literal; only technical_patterns contains authored regex syntax.
_ENGINE_PREFIX = re.compile(r"^(?:" + "|".join(map(re.escape, PRESENTATION_WORDS["engine_prefixes"])) + r")\s+")
_ENGINE_TAIL = re.compile(r"\s+(?:" + "|".join(map(re.escape, PRESENTATION_WORDS["engine_tails"])) + r")$", re.IGNORECASE)
_ENGINE_SUFFIXES = MappingProxyType({
    key: re.compile(r"-" + re.escape(suffix) + r"$", re.IGNORECASE)
    for key, suffix in PRESENTATION_WORDS["capability_engine_suffixes"].items()
})
_HARDWARE_LABEL_SUFFIX = re.compile(
    r"\((?:" + "|".join(map(re.escape, PRESENTATION_WORDS["hardware_label_suffixes"])) + r")\)$"
)


def health_base(service: Mapping[str, Any]) -> str | None:
    """The address a relative health path is joined against: the one address dispatch dials, plus a host-and-port record's ``base_path``."""
    config = service.get("config")
    connection = {**(config if isinstance(config, Mapping) else {}), **service}
    address = engine_base_address(connection)
    if not address:
        return None
    base_path = str(connection.get("base_path") or "").strip("/")
    if base_path and address == host_port_origin(connection):
        return f"{address}/{base_path}"
    return address


def join_health(base: str, endpoint: str) -> str:
    """The health address: an absolute URL as written, else the path joined to the base (a leading slash means origin-relative)."""
    if endpoint.startswith(("http://", "https://")) or not base:
        return endpoint
    return urljoin(f"{base}/", endpoint)


def service_health_endpoint(service: Mapping[str, Any]) -> str | None:
    """Resolve only a declared health path over either registry connection shape."""
    config = service.get("config")
    connection = {**(config if isinstance(config, Mapping) else {}), **service}
    endpoint = connection.get("health_endpoint")
    if not endpoint:
        return None
    endpoint = str(endpoint)
    if endpoint.startswith(("http://", "https://")):
        return endpoint
    base_url = health_base(service)
    if not base_url:
        return None
    return join_health(base_url, endpoint)


def starts_on_demand(service: Mapping[str, Any]) -> bool:
    """True when the service record declares ``residency: on_demand``.

    ``residency`` is the legacy string (in the record's config, or flat on a projected backend), or a mapping
    (the schema-2 block that names the domain and its transition). A record can carry both; the mapping never
    hides the string, which the runtime projection keeps under ``LEGACY_RESIDENCY_KEY``.
    """
    config = service.get("config")
    config = config if isinstance(config, Mapping) else {}
    for value in (service.get("residency"), service.get(LEGACY_RESIDENCY_KEY), config.get("residency")):
        if isinstance(value, str) and value.strip():
            return value.strip().lower() == "on_demand"
    return False


def declared_health_success(service: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    """Match a declared JSON path with a value other than null or false."""
    config = service.get("config")
    connection = {**(config if isinstance(config, Mapping) else {}), **service}
    path = connection.get("health_success_path")
    if not isinstance(path, str) or not path.strip():
        return False
    value: Any = payload
    for key in path.split("."):
        if not isinstance(value, Mapping) or key not in value:
            return False
        value = value[key]
    return value is not None and value is not False


def engine_label(service: Mapping[str, Any], service_id: str | None) -> str | None:
    """Use declared identity and capability data, never registry notes."""
    config = service.get("config")
    sources = (service, config if isinstance(config, Mapping) else {})
    name = next((str(source[field]).strip() for field in ("display_name", "engine_name", "engine", "name")
                 for source in sources
                 if isinstance(source.get(field), str) and source[field].strip()
                 and source[field].strip() != service_id), None)
    labels = service_capability_labels(service)
    if not name:
        return " & ".join(labels) or None
    for suffix in PRESENTATION_WORDS["provider_suffixes"]:
        name = name.removesuffix(suffix)
    # Legacy seed names sometimes carry deployment notes in parentheses.
    # The engine line keeps hardware suffixes but moves notes to details.
    name = _strip_deployment_notes(name)
    if labels:
        name = _ENGINE_PREFIX.sub("", name)
        name = _ENGINE_TAIL.sub("", name)
    for key, pattern in _ENGINE_SUFFIXES.items():
        if CAPABILITY_LABELS[key] in labels:
            name = pattern.sub("", name)
    if re.fullmatch(r"[a-z][a-z0-9-]+", name):
        name = friendly_name(name)
    name = _preserve_engine_acronyms(name)
    if _HARDWARE_LABEL_SUFFIX.search(name):
        return name
    if not labels or name.casefold() in {label.casefold() for label in labels}:
        return name
    return f"{name} ({' & '.join(labels).lower()})"


def model_locality(service: Mapping[str, Any], model: Mapping[str, Any] | None) -> str:
    config = service.get("config")
    sources = (model or {}, service, config if isinstance(config, Mapping) else {})
    for source in sources:
        value = str(source.get("locality") or source.get("placement") or "").strip().lower()
        if value in {"local", "remote"}:
            return value
    if any(is_remote_provider_config(source) for source in sources):
        return "remote"
    # A routing substrate's own address says nothing about its model placement.
    if is_litellm_service_record(service.get("id"), service) or any(
        source.get("execution_substrate") or source.get("managed")
        or str(source.get("kind") or "") == "llm-substrate"
        for source in sources
    ):
        return "unknown"
    for source in (service, sources[2]):
        endpoint = str(source.get("base_url") or source.get("url") or "")
        try:
            host = urlsplit(endpoint).hostname if endpoint else str(source.get("host") or "")
        except ValueError:
            continue
        if not host:
            continue
        if host.lower().rstrip(".") == "localhost":
            return "local"
        try:
            if ip_address(host).is_loopback:
                return "local"
        except ValueError:
            pass
    # A LAN host, container name, or public URL does not prove model placement.
    return "unknown"


# Capability labels, icons, aliases and summaries come from capability_words.yml.
PRIMARY_CHAT_NAMES = frozenset({"default", "fast", "coder", "code", "chat"})


def service_capability_labels(service: Mapping[str, Any]) -> list[str]:
    config = service.get("config")
    config = config if isinstance(config, Mapping) else {}
    capabilities = service.get("capabilities") or config.get("capabilities") or ()
    if isinstance(capabilities, str):
        capabilities = (capabilities,)
    keys = list(dict.fromkeys(PROTOCOL_CAPABILITIES[str(cap)] for cap in capabilities
                              if str(cap) in PROTOCOL_CAPABILITIES))
    keys = [key for key in keys if not any(other in keys for other in CAPABILITY_HIDDEN_BY.get(key, ()))]
    return [CAPABILITY_LABELS[key] for key in keys]


def service_presentation(service: Mapping[str, Any], service_id: str) -> dict[str, Any]:
    labels = service_capability_labels(service)
    first = labels[0] if labels else PRESENTATION_WORDS["unclassified"]["label"]
    kind = next((key for key, value in CAPABILITY_LABELS.items() if value == first),
                PRESENTATION_WORDS["unclassified"]["service_capability"])
    # Derive an unnamed registry ID only as a last resort, without engine rules.
    name = engine_label(service, service_id) or friendly_name(service_id)
    return {"engine_label": name, "capability_title": first,
            "capability_labels": labels, "icon_key": CAPABILITY_ICON_KEYS[kind]}


# Presentation-metadata key holding a row's `/v1/models` contract fields
# (`type`, `architecture`, ...). When present it decides the classification.
LISTED_METADATA_KEY = "listed_model_metadata"
# The wire value for a capability no contract classifies. It is not a product
# capability (not in CAPABILITY_LABELS), so it takes the generic label and icon,
# and no intent word leaks into the catalog.
UNCLASSIFIED_CAPABILITY = PRESENTATION_WORDS["unclassified"]["capability"]


def product_capability(capability: Any, metadata: Mapping[str, Any] | None = None) -> str:
    """Classify through the `/v1/models` contract (gateway/model_metadata.py).

    The listed model's `type` and modalities decide, else the capability's
    intent and modalities read through the same contract. Audio that turns
    audio into text is `transcribe`; other audio takes a declared subtype
    (`capability: music`, a `capability:sfx` tag) or the speech intent, else
    stays generic `audio`. With no contract it is the declared product
    capability (a manifest's `capability: mesh`), else UNCLASSIFIED_CAPABILITY.
    Never inferred from model IDs.
    """
    metadata = metadata or {}
    intent = capability if isinstance(capability, str) else capability.intent
    contract = model_metadata.from_fields(metadata.get(LISTED_METADATA_KEY)) or model_metadata.from_capability(
        intent,
        getattr(capability, "input_modalities", ()),
        getattr(capability, "output_modalities", ()),
    )
    if contract is None:
        declared = metadata.get("capability") or getattr(capability, "capability", None)
        return declared if isinstance(declared, str) and declared in CAPABILITY_LABELS else UNCLASSIFIED_CAPABILITY
    if contract.type != AUDIO_RULE.get("type"):
        return TYPE_CAPABILITIES[contract.type]
    if AUDIO_RULE["input"] in contract.input_modalities and AUDIO_RULE["absent_output"] not in contract.output_modalities:
        return AUDIO_RULE["result"]
    tags = tuple(metadata.get("tags") or ()) + tuple(getattr(capability, "tags", ()))
    hints = (metadata.get("capability"), getattr(capability, "capability", None),
             *(str(tag).removeprefix("capability:") for tag in tags))
    subtype = next((hint for hint in hints if hint in AUDIO_RULE["subtypes"]), None)
    if subtype:
        return subtype
    # No declared subtype: the shipped capability words (registry/capability_words.yml) name it.
    word = INTENT_CAPABILITIES.get(intent)
    return word if word in AUDIO_RULE["subtypes"] else AUDIO_RULE["fallback"]


def friendly_name(value: str) -> str:
    value = value.removeprefix("role:")
    return " ".join(word[:1].upper() + word[1:] for word in value.replace("_", " ").replace("-", " ").split())


def _preserve_engine_acronyms(value: str) -> str:
    """Keep common hardware acronyms in human labels from metadata-derived IDs."""
    replacements = PRESENTATION_WORDS["acronyms"]
    parts = [replacements.get(part, part) for part in value.split(" ")]
    if len(parts) > 1 and parts[-1] in PRESENTATION_WORDS["parenthesized_hardware"]:
        return f"{' '.join(parts[:-1])} ({parts[-1]})"
    return " ".join(parts)


def _presentation_label(value: Any) -> str | None:
    if value is None:
        return None
    label = str(value).strip()
    if not label:
        return None
    return _preserve_engine_acronyms(label)


def capability_presentation(capability: Any, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One product contract: taxonomy, friendly identity and safe description."""
    metadata = metadata or {}
    kind = product_capability(capability, metadata)
    label = CAPABILITY_LABELS.get(kind, PRESENTATION_WORDS["unclassified"]["label"])
    use_name = metadata.get("alias") or getattr(capability, "model", None) or getattr(capability, "workflow", None)
    declared_title = next((_presentation_label(metadata[field]) for field in ("title", "display_name", "name")
                           if metadata.get(field) and str(metadata[field]).strip()), None)
    capability_title = _presentation_label(getattr(capability, "title", None))
    # A chat alias/profile is an independent choice: its title is its name,
    # not the target's generic "Chat" or "Coding profile" label.
    remote = metadata.get("locality") == "remote" or "remote" in getattr(capability, "tags", ())
    alias = bool(metadata.get("alias") or "alias" in getattr(capability, "tags", ()))
    if declared_title:
        title = declared_title
    elif remote and not alias and use_name:
        title = str(use_name)
    elif kind == "chat" and use_name:
        title = _presentation_label(friendly_name(str(use_name)))
    else:
        title = capability_title
    title = title or (_presentation_label(friendly_name(str(use_name))) if use_name else label)
    if title.casefold() in PRESENTATION_WORDS["bare_titles"]:
        title = label
    description = metadata.get("description") or getattr(capability, "description", None)
    if description:
        description = re.split(r"(?<=[.!?])\s+", str(description).strip(), maxsplit=1)[0]
    if description and (_is_technical_description(str(description)) or len(str(description).split()) > 8):
        description = None
    if not description:
        if kind == "chat":
            profile_name = str(use_name or "chat").removeprefix("role:")
            description = _chat_profile_description(profile_name)
        else:
            description = CAPABILITY_DESCRIPTIONS.get(kind, PRESENTATION_WORDS["unclassified"]["summary"])
    description = re.split(r"(?<=[.!?])\s+", str(description).strip(), maxsplit=1)[0]
    description = description.rstrip(".") + "." if not description.endswith(("!", "?")) else description
    target = next((tag.removeprefix("resolves_to:") for tag in getattr(capability, "tags", ())
                   if tag.startswith("resolves_to:")), None)
    recommended = _presentation_label(metadata.get("recommended_model")) or capability_title or declared_title
    recommended = recommended or (_presentation_label(friendly_name(target)) if target else title)
    if recommended.casefold() in PRESENTATION_WORDS["bare_titles"]:
        recommended = title
    out = {"capability": kind, "capability_title": label,
           "icon_key": CAPABILITY_ICON_KEYS.get(kind, PRESENTATION_WORDS["unclassified"]["icon"]),
           "title": title, "recommended_model": str(recommended), "summary": description,
           "use_names": [str(use_name)] if use_name else [],
           "secondary_profile": bool(metadata.get("secondary_profile", (not remote or alias) and kind == "chat" and use_name
                                     and str(use_name).removeprefix("role:") not in PRIMARY_CHAT_NAMES))}
    if use_name:
        out["use_name"] = str(use_name)
    return out


def _chat_profile_description(profile_name: str) -> str:
    normalized = profile_name.strip().casefold().replace("_", "-")
    if normalized in {"default", "chat"}:
        return "Thinks through questions before answering."
    if normalized in {"fast", "quick"}:
        return "Answers quickly for everyday prompts."
    if normalized in {"coder", "code"} or "coder" in normalized:
        return "Helps with coding tasks and technical questions."
    if normalized in {"rp", "roleplay"} or "roleplay" in normalized:
        return "Uses a roleplay-tuned chat profile."
    return "A chat model."


def _strip_deployment_notes(value: str) -> str:
    note = re.search(r"\(([^)]*)\)", value)
    if note and note.group(1).strip().upper() in PRESENTATION_WORDS["parenthesized_hardware"]:
        return value.split(";", 1)[0].strip()
    return value.split(" (", 1)[0].split(";", 1)[0].strip()


def _is_technical_description(value: str) -> bool:
    lowered = value.casefold()
    return any(pattern.search(lowered) for pattern in PRESENTATION_WORDS["technical_patterns"])


def _request_shape(row: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """What a request to this row must carry: its required input modalities and, in order, the kind of each required file."""
    return (tuple(row.get("required_input_modalities") or ()),
            tuple(str(item.get("modality")) for item in row.get("required_media_inputs") or () if isinstance(item, Mapping)))


def group_product_capabilities(rows: list[dict[str, Any]], *,
                               selection_rank: Callable[[str], float] = lambda _name: float("inf")) -> list[dict[str, Any]]:
    """One usable recommendation per taxonomy, retaining permitted caller names.

    The recommendation is an available row first, then a row a person can choose by name, then the lowest
    ``selection_rank`` of its name (the data's place among rivals for one route; unranked last), then the best status.
    When the recommendation needs a file, the names offered with it are those whose requests carry the same inputs, so
    the composer that asks for that file can run every one of them: a 3D model made from a picture and a step that edits
    a 3D model file are not offered as choices of each other. A recommendation that needs no file keeps every name of
    its kind, because its composer attaches files as the person goes (image edit, image-to-video).
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["capability"], []).append(row)
    rank = {"ready": 6, "idle": 5, "starting": 4, "busy": 3, "degraded": 2, "unknown": 1, "unavailable": 0}
    result = []
    for kind, candidates in grouped.items():
        def preference(row):
            status = row.get("status", (row.get("availability") or {}).get("status"))
            name = row.get("use_name")
            return (status in {"ready", "idle"}, bool(name), -selection_rank(str(name)) if name else float("-inf"),
                    rank.get(status, 0), name in {"default", kind}, "alias" in row.get("tags", []))
        best = dict(max(candidates, key=preference))
        shape = _request_shape(best)
        names = list(dict.fromkeys(name for row in candidates if not shape[1] or _request_shape(row) == shape
                                   for name in row.get("use_names", [])))
        # The public role spelling covers its internal role: identity only when
        # that public spelling was independently permitted in this projection.
        best["use_names"] = [name for name in names
                             if not name.startswith("role:") or name.removeprefix("role:") not in names]
        result.append(best)
    order = list(CAPABILITY_LABELS)
    return sorted(result, key=lambda row: order.index(row["capability"]) if row["capability"] in order else len(order))


def product_status_summary(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Return status facts for the client presentation layer to phrase."""
    issues = [row for row in rows if not row.get("available")]
    if not rows:
        reason_code = "no_capabilities"
        state = "unavailable"
    elif not issues:
        state = "ready"
        reason_code = "ready_on_request" if any(row.get("status") == "idle" for row in rows) else "ready"
    else:
        state = next((status for status in ("unavailable", "degraded", "unknown", "busy", "starting")
                      if any(row["status"] == status for row in issues)), "unavailable")
        selected = next(row for row in issues if row["status"] == state)
        evidence = (selected.get("availability") or {}).get("evidence") or {}
        reason_code = {
            ("unavailable", "disabled"): "unavailable_disabled",
            ("unavailable", "unconfigured"): "unavailable_unconfigured",
        }.get((state, evidence.get("kind")), state)
    return {
        "state": state,
        "reason_code": reason_code,
        "affected_capability_ids": list(dict.fromkeys(
            str(row["capability"]) for row in issues if row.get("capability")
        )),
        "counts": {"total": len(rows), "available": len(rows) - len(issues), "affected": len(issues)},
    }
