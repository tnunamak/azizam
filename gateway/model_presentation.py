"""Human catalog labels and placement from operator-authored service data."""

from collections.abc import Mapping
from ipaddress import ip_address
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

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
    # The engine line keeps only identity; notes belong in engine details.
    name = name.split(" (", 1)[0].split(";", 1)[0].strip()
    if labels:
        name = re.sub(r"^(?:Local|Remote)\s+", "", name)
        name = re.sub(r"\s+(?:server|provider|engine)$", "", name, flags=re.IGNORECASE)
    suffixes = {"Transcription": "stt", "Speech": "tts"}
    for label, suffix in suffixes.items():
        if label in labels:
            name = re.sub(rf"-{suffix}$", "", name, flags=re.IGNORECASE)
    if re.fullmatch(r"[a-z][a-z0-9-]+", name):
        name = friendly_name(name)
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


def product_capability(capability: Any, metadata: Mapping[str, Any] | None = None) -> str:
    """Classify declared protocol data; never infer capabilities from model IDs."""
    metadata = metadata or {}
    declared = metadata.get("capability") or getattr(capability, "capability", None)
    if isinstance(declared, str) and declared in CAPABILITY_LABELS:
        return declared
    tags = tuple(metadata.get("tags") or ()) + tuple(getattr(capability, "tags", ()))
    for tag in tags:
        value = str(tag).removeprefix("capability:")
        if value in CAPABILITY_LABELS:
            return value
    intent = capability if isinstance(capability, str) else capability.intent
    if intent in PROTOCOL_CAPABILITIES:
        return PROTOCOL_CAPABILITIES[intent]
    if intent.startswith(("chat.", "text.completions", "responses.")):
        return "chat"
    if intent.startswith(("speech.trans", "audio.trans")):
        return "transcribe"
    if intent.startswith(("speech.", "audio.speech")):
        return "speech"
    if intent.startswith(("decision", "systemone")):
        return "decide"
    return intent.split(".", 1)[0]


def friendly_name(value: str) -> str:
    value = value.removeprefix("role:")
    return " ".join(word[:1].upper() + word[1:] for word in value.replace("_", " ").replace("-", " ").split())


def capability_presentation(capability: Any, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One product contract: taxonomy, friendly identity and safe description."""
    metadata = metadata or {}
    kind = product_capability(capability, metadata)
    label = CAPABILITY_LABELS.get(kind, "AI")
    use_name = metadata.get("alias") or getattr(capability, "model", None) or getattr(capability, "workflow", None)
    declared_title = next((str(metadata[field]).strip() for field in ("title", "display_name", "name")
                           if metadata.get(field) and str(metadata[field]).strip()), None)
    # A chat alias/profile is an independent choice: its title is its name,
    # not the target's generic "Chat" or "Coding profile" label.
    remote = metadata.get("locality") == "remote" or "remote" in getattr(capability, "tags", ())
    alias = bool(metadata.get("alias") or "alias" in getattr(capability, "tags", ()))
    title = declared_title or (str(use_name) if remote and not alias and use_name else
                              friendly_name(str(use_name)) if kind == "chat" and use_name
                              else getattr(capability, "title", None))
    title = title or (friendly_name(str(use_name)) if use_name else label)
    description = metadata.get("description") or getattr(capability, "description", None)
    if not description:
        if kind == "chat":
            profile_name = str(use_name or "chat").removeprefix("role:")
            context = metadata.get("context_window") or metadata.get("context_length")
            upstream = metadata.get("upstream_model") or metadata.get("model_id")
            details = []
            if upstream and upstream != use_name:
                details.append(f"Uses {upstream}")
            if context:
                details.append(f"supports a context of {context} tokens")
            description = ", ".join(details) + "." if details else f"A separate chat profile named {profile_name}."
        else:
            description = {
                "image": "Generate or edit images from your prompts.", "video": "Generate video from your prompts.",
                "music": "Generate music from your prompts.", "sfx": "Generate sound effects from your prompts.",
                "speech": "Turn text into spoken audio.", "transcribe": "Turn recorded speech into text.",
                "decide": "Choose an outcome from the options you provide.", "embed": "Create vectors for search and similarity.",
                "rerank": "Order search results by relevance.", "audio": "Generate audio from your prompts.",
            }.get(kind, "Use this configured AI capability.")
    description = re.split(r"(?<=[.!?])\s+", str(description).strip(), maxsplit=1)[0]
    description = description.rstrip(".") + "." if not description.endswith(("!", "?")) else description
    target = next((tag.removeprefix("resolves_to:") for tag in getattr(capability, "tags", ())
                   if tag.startswith("resolves_to:")), None)
    recommended = metadata.get("recommended_model") or getattr(capability, "title", None) or declared_title
    recommended = recommended or (friendly_name(target) if target else title)
    out = {"capability": kind, "capability_title": label, "icon_key": CAPABILITY_ICON_KEYS.get(kind, "chat"),
           "title": title, "recommended_model": str(recommended), "summary": description,
           "use_names": [str(use_name)] if use_name else [],
           "secondary_profile": bool(metadata.get("secondary_profile", kind == "chat" and use_name
                                     and str(use_name).removeprefix("role:") not in PRIMARY_CHAT_NAMES))}
    if use_name:
        out["use_name"] = str(use_name)
    return out


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
        best["use_names"] = list(dict.fromkeys(name for row in candidates for name in row.get("use_names", [])))
        result.append(best)
    order = list(CAPABILITY_LABELS)
    return sorted(result, key=lambda row: order.index(row["capability"]) if row["capability"] in order else len(order))


def product_status_summary(rows: list[Mapping[str, Any]]) -> dict[str, str]:
    """Report the unavailable part, even when another capability is ready."""
    if not rows:
        return {"state": "unavailable", "reason": "No capabilities are available to your account yet."}
    issues = [row for row in rows if not row.get("available")]
    if not issues:
        if any(row.get("status") == "idle" for row in rows):
            return {"state": "ready", "reason": "Everything you can use is ready, including services that start on request."}
        return {"state": "ready", "reason": "Everything you can use is ready."}
    state = next((status for status in ("unavailable", "unknown", "busy", "starting")
                  if any(row["status"] == status for row in issues)), "unavailable")
    row = next(row for row in issues if row["status"] == state)
    states = {"unavailable": "unavailable", "unknown": "not measured yet", "starting": "starting", "busy": "busy"}
    verb = "are" if row["capability"] in {"image", "decide", "embed", "sfx"} else "is"
    # Evidence can contain private backend diagnostics. Member explanations use
    # declared public states; registry disablement/configuration is safe detail.
    availability = row.get("availability") or {}
    evidence = availability.get("evidence") or {}
    detail = {
        "disabled": "the service is disabled", "unconfigured": "the service has not been configured",
    }.get(evidence.get("kind"))
    detail = detail or {
        "unavailable": "the service is not responding", "unknown": "no current health observation is available",
        "starting": "the service is warming up", "busy": "the service is handling other requests",
    }[state]
    extra = f", with {len(issues) - 1} other capabilities affected" if len(issues) > 1 else ""
    return {"state": state, "reason": f"{row['capability_title']} {verb} {states[state]}: {detail}{extra}."}
