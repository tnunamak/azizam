"""Human catalog labels and placement from operator-authored service data."""

from collections.abc import Mapping
from ipaddress import ip_address
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
    config = service.get("config")
    config = config if isinstance(config, Mapping) else {}
    sources = (service, config)
    name = next((str(source[field]).strip() for source in sources
                 for field in ("display_name", "name")
                 if source.get(field) and str(source[field]).strip() != service_id), None)
    if model_locality(service, None) != "local":
        return name
    if name:
        name = name.removesuffix(" (provider)").removesuffix(" (Provider)")
    # Capability vocabulary is protocol data, never an engine-name classifier.
    labels = []
    vocabulary = {
        "chat": "Chat", "completions": "Chat", "chat.completions": "Chat",
        "speech": "Speech", "audio.speech": "Speech", "speech.synthesis": "Speech",
        "transcriptions": "Transcription", "audio.transcriptions": "Transcription",
        "speech.transcription": "Transcription", "images": "Image", "image": "Image",
        "image.generate": "Image", "videos": "video", "video": "video", "video.generate": "video",
        "embeddings": "Embeddings", "text.embeddings": "Embeddings", "rerank": "Rerank",
        "decisions": "Decisions", "music": "Music", "sfx": "Sound effects",
    }
    capabilities = service.get("capabilities") or config.get("capabilities") or ()
    if isinstance(capabilities, str):
        capabilities = (capabilities,)
    for capability in capabilities:
        label = vocabulary.get(str(capability))
        if label and label not in labels:
            labels.append(label)
    if not labels:
        return name
    label = " & ".join(labels)
    if label == "video":
        label = "Video"
    identity = next((str(source[field]).strip() for source in sources
                     for field in ("engine_name", "engine")
                     if isinstance(source.get(field), str) and source[field].strip()), None)
    if not identity and name:
        # An explicitly named engine may carry an old capability subtitle.
        base, separator, subtitle = name.partition(" (")
        identity = base if separator and any(word in subtitle.lower() for word in
                    ("image", "video", "speech", "chat", "transcription")) else name
    if not identity or identity.casefold() == label.casefold():
        return label
    if identity.casefold().startswith(label.casefold() + " ("):
        return identity
    return f"{label} ({identity})"


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
    "chat": "Chat", "image": "Image", "video": "Video", "speech": "Speech",
    "transcribe": "Transcription", "music": "Music", "sfx": "Sound effects",
    "audio": "Audio", "decide": "Decisions", "embed": "Embeddings", "rerank": "Rerank",
}


def product_capability(capability: Any, metadata: Mapping[str, Any] | None = None) -> str:
    """Classify protocol shape with explicit operator taxonomy taking precedence.

    Audio generation remains audio unless its manifest declares music or sfx;
    model/engine names are never used to guess what a model can do.
    """
    metadata = metadata or {}
    declared = metadata.get("capability") or getattr(capability, "capability", None)
    if isinstance(declared, str) and declared in CAPABILITY_LABELS:
        return str(declared)
    tags = tuple(metadata.get("tags") or ()) + tuple(getattr(capability, "tags", ()))
    for tag in tags:
        value = str(tag).removeprefix("capability:")
        if value in CAPABILITY_LABELS:
            return value
    intent = capability if isinstance(capability, str) else capability.intent
    if intent.startswith(("chat.", "text.completions", "responses.")):
        return "chat"
    if intent == "text.embeddings":
        return "embed"
    if intent == "text.rerank":
        return "rerank"
    if intent.startswith(("speech.trans", "audio.trans")):
        return "transcribe"
    if intent.startswith(("speech.", "audio.speech")):
        return "speech"
    if intent.startswith(("decision", "systemone")):
        return "decide"
    return intent.split(".", 1)[0]


def capability_presentation(capability: Any, metadata: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Friendly summary plus the exact caller name; technical ids stay separate."""
    metadata = metadata or {}
    kind = product_capability(capability, metadata)
    label = CAPABILITY_LABELS.get(kind, "AI")
    name = next((str(value).strip() for value in (
        metadata.get("title"), metadata.get("display_name"), metadata.get("name"),
        getattr(capability, "title", None),
    ) if value and str(value).strip()), None)
    out = {"capability": kind, "title": f"{label} ({name})" if name and name != label else label}
    use_name = getattr(capability, "model", None) or getattr(capability, "workflow", None)
    if use_name:
        out["use_name"] = use_name
    return out
