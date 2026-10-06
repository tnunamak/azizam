"""Human catalog labels and placement from operator-authored service data."""

from collections.abc import Mapping
from ipaddress import ip_address
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from gateway import model_metadata
from gateway.service_registry import is_litellm_service_record, is_remote_provider_config


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
    base_url = str(connection.get("base_url") or "").rstrip("/")
    if not base_url:
        host = str(connection.get("host") or "").strip()
        if not host:
            return None
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = connection.get("port")
        authority = f"{host}:{port}" if port is not None else host
        scheme = str(connection.get("scheme") or "http")
        base_path = str(connection.get("base_path") or "").strip("/")
        base_url = f"{scheme}://{authority}/{base_path}".rstrip("/")
    # Leading slash means origin-relative; relative paths use the declared base.
    return urljoin(f"{base_url}/", endpoint)


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
    name = name.removesuffix(" (provider)").removesuffix(" (Provider)")
    # Legacy seed names sometimes carry deployment notes in parentheses.
    # The engine line keeps hardware suffixes but moves notes to details.
    name = _strip_deployment_notes(name)
    if labels:
        name = re.sub(r"^(?:Local|Remote)\s+", "", name)
        name = re.sub(r"\s+(?:server|provider|engine)$", "", name, flags=re.IGNORECASE)
    suffixes = {"Transcription": "stt", "Speech": "tts"}
    for label, suffix in suffixes.items():
        if label in labels:
            name = re.sub(rf"-{suffix}$", "", name, flags=re.IGNORECASE)
    if re.fullmatch(r"[a-z][a-z0-9-]+", name):
        name = friendly_name(name)
    name = _preserve_engine_acronyms(name)
    if re.search(r"\((?:CPU|GPU|STT|TTS)\)$", name):
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


CAPABILITY_LABELS = {
    "chat": "Chat", "image": "Images", "video": "Video", "speech": "Speech",
    "transcribe": "Transcription", "music": "Music", "sfx": "Sound effects",
    "audio": "Audio", "decide": "Decisions", "embed": "Embeddings", "rerank": "Rerank",
}
# Generic audio generation has no honest music/speech subtype. Its neutral
# waveform glyph shares the sound-effects icon without changing its taxonomy.
CAPABILITY_ICON_KEYS = {key: key for key in CAPABILITY_LABELS if key != "audio"}
CAPABILITY_ICON_KEYS["audio"] = "sfx"
PROTOCOL_CAPABILITIES = {
    "llm.execution": "chat", "typesafe-systemone": "decide",
    "chat": "chat", "completions": "chat", "chat.completions": "chat", "vision": "chat",
    "speech": "speech", "tts": "speech", "audio.speech": "speech", "speech.synthesis": "speech",
    "transcriptions": "transcribe", "transcription": "transcribe", "stt": "transcribe",
    "audio.transcriptions": "transcribe", "speech.transcription": "transcribe",
    "speech.translation": "transcribe", "images": "image", "image": "image", "generations": "image",
    "image.generate": "image", "image.edit": "image", "videos": "video", "video": "video",
    "video.generate": "video", "embeddings": "embed", "text.embeddings": "embed",
    "rerank": "rerank", "text.rerank": "rerank", "decisions": "decide",
    "decision.systemone": "decide", "music": "music", "sfx": "sfx", "audio": "audio",
    "audio.generate": "audio",
}
PRIMARY_CHAT_NAMES = frozenset({"default", "fast", "coder", "code", "chat"})
# Product defaults are stable caller names, independent of catalog order,
# engine identity, and current health. Operators still control their targets.
RECOMMENDED_ALIASES = {
    "chat": "default", "image": "image", "video": "video", "music": "music",
    "sfx": "sfx", "speech": "tts", "transcribe": "stt", "decide": "decide",
    "embed": "embed", "rerank": "rerank",
}
CAPABILITY_DESCRIPTIONS = {
    "image": "Generate or edit images from your prompts.",
    "video": "Video from a prompt or image.",
    "music": "Songs and instrumentals from a prompt.",
    "sfx": "Sound effects from a description.",
    "speech": "Turn text into spoken audio.",
    "transcribe": "Turn recorded speech into text.",
    "decide": "Choose an outcome from your options.",
    "embed": "Create vectors for search and similarity.",
    "rerank": "Order search results by relevance.",
    "audio": "Generate audio from your prompts.",
}


def service_capability_labels(service: Mapping[str, Any]) -> list[str]:
    config = service.get("config")
    config = config if isinstance(config, Mapping) else {}
    capabilities = service.get("capabilities") or config.get("capabilities") or ()
    if isinstance(capabilities, str):
        capabilities = (capabilities,)
    keys = list(dict.fromkeys(PROTOCOL_CAPABILITIES[str(cap)] for cap in capabilities
                              if str(cap) in PROTOCOL_CAPABILITIES))
    if "audio" in keys and any(key in keys for key in ("speech", "transcribe", "music", "sfx")):
        keys.remove("audio")
    return [CAPABILITY_LABELS[key] for key in keys]


def service_presentation(service: Mapping[str, Any], service_id: str) -> dict[str, Any]:
    labels = service_capability_labels(service)
    first = labels[0] if labels else "AI"
    kind = next((key for key, value in CAPABILITY_LABELS.items() if value == first), "chat")
    # Derive an unnamed registry ID only as a last resort, without engine rules.
    name = engine_label(service, service_id) or friendly_name(service_id)
    return {"engine_label": name, "capability_title": first,
            "capability_labels": labels, "icon_key": CAPABILITY_ICON_KEYS[kind]}


# Presentation-metadata key holding a row's `/v1/models` contract fields
# (`type`, `architecture`, ...). When present it decides the classification.
LISTED_METADATA_KEY = "listed_model_metadata"
# `/v1/models` type -> product capability. `audio` is split below.
_TYPE_CAPABILITIES = {
    "language": "chat", "image": "image", "video": "video", "embedding": "embed",
    "reranking": "rerank", "decision": "decide",
}
_AUDIO_SUBTYPES = ("music", "sfx", "speech")


def product_capability(capability: Any, metadata: Mapping[str, Any] | None = None) -> str:
    """Classify through the `/v1/models` contract (gateway/model_metadata.py).

    The listed model's `type` and modalities decide, else the capability's
    intent and modalities read through the same contract. Audio that turns
    audio into text is `transcribe`; other audio takes a declared subtype
    (`capability: music`, a `capability:sfx` tag) or the speech intent, else
    stays generic `audio`. Never inferred from model IDs.
    """
    metadata = metadata or {}
    intent = capability if isinstance(capability, str) else capability.intent
    contract = model_metadata.from_fields(metadata.get(LISTED_METADATA_KEY)) or model_metadata.from_capability(
        intent,
        getattr(capability, "input_modalities", ()),
        getattr(capability, "output_modalities", ()),
    )
    if contract is None:
        return intent.split(".", 1)[0]
    if contract.type != "audio":
        return _TYPE_CAPABILITIES[contract.type]
    if "audio" in contract.input_modalities and "audio" not in contract.output_modalities:
        return "transcribe"
    tags = tuple(metadata.get("tags") or ()) + tuple(getattr(capability, "tags", ()))
    hints = (metadata.get("capability"), getattr(capability, "capability", None),
             *(str(tag).removeprefix("capability:") for tag in tags))
    subtype = next((hint for hint in hints if hint in _AUDIO_SUBTYPES), None)
    if subtype:
        return subtype
    return "speech" if intent.startswith(("speech.", "audio.speech")) else "audio"


def friendly_name(value: str) -> str:
    value = value.removeprefix("role:")
    return " ".join(word[:1].upper() + word[1:] for word in value.replace("_", " ").replace("-", " ").split())


def _preserve_engine_acronyms(value: str) -> str:
    """Keep common hardware acronyms in human labels from metadata-derived IDs."""
    replacements = {
        "Cpu": "CPU",
        "Gpu": "GPU",
        "Stt": "STT",
        "Tts": "TTS",
    }
    parts = [replacements.get(part, part) for part in value.split(" ")]
    if len(parts) > 1 and parts[-1] in {"CPU", "GPU"}:
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
    label = CAPABILITY_LABELS.get(kind, "AI")
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
    if title.casefold() in {"tts", "stt"}:
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
            description = CAPABILITY_DESCRIPTIONS.get(kind, "Use this configured AI capability.")
    description = re.split(r"(?<=[.!?])\s+", str(description).strip(), maxsplit=1)[0]
    description = description.rstrip(".") + "." if not description.endswith(("!", "?")) else description
    target = next((tag.removeprefix("resolves_to:") for tag in getattr(capability, "tags", ())
                   if tag.startswith("resolves_to:")), None)
    recommended = _presentation_label(metadata.get("recommended_model")) or capability_title or declared_title
    recommended = recommended or (_presentation_label(friendly_name(target)) if target else title)
    if recommended.casefold() in {"tts", "stt"}:
        recommended = title
    out = {"capability": kind, "capability_title": label, "icon_key": CAPABILITY_ICON_KEYS.get(kind, "chat"),
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
    return f"A separate chat profile named {profile_name}."


def _strip_deployment_notes(value: str) -> str:
    note = re.search(r"\(([^)]*)\)", value)
    if note and note.group(1).strip().upper() in {"CPU", "GPU"}:
        return value.split(";", 1)[0].strip()
    return value.split(" (", 1)[0].split(";", 1)[0].strip()


def _is_technical_description(value: str) -> bool:
    technical_patterns = (
        r":\d{2,5}\b",
        r"\b(?:port|host|localhost|127\.0\.0\.1)\b",
        r"\b(?:gguf|safetensors|checkpoint)\b",
        r"\b(?:contract|workflow|flac|khz)\b",
        r"\bcontext (?:of|window|length)\b",
        r"\bvia\s+(?:local|remote|backend|engine|server)\b",
        r"\b(?:served by|served via|running on)\b",
        r"[/\\][\w.-]+",
    )
    lowered = value.casefold()
    return any(re.search(pattern, lowered) for pattern in technical_patterns)


def group_product_capabilities(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One usable recommendation per taxonomy, retaining permitted caller names."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["capability"], []).append(row)
    rank = {"ready": 5, "idle": 4, "starting": 3, "busy": 2, "unknown": 1, "unavailable": 0}
    result = []
    for kind, candidates in grouped.items():
        def preference(row):
            return (rank.get(row.get("status", (row.get("availability") or {}).get("status")), 0),
                    row.get("use_name") in {"default", kind}, "alias" in row.get("tags", []),
                    bool(row.get("use_name")))
        best = dict(max(candidates, key=preference))
        names = list(dict.fromkeys(name for row in candidates for name in row.get("use_names", [])))
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
        state = next((status for status in ("unavailable", "unknown", "busy", "starting")
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
