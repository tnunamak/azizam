"""Audio media-job shaping helpers.

This module is intentionally backend-agnostic. It turns OpenAI-ish audio
generation requests and results into the durable ``media_jobs`` shapes the
gateway already stores, while leaving route execution and asset-cache ownership
with the proxy layer.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol

from gateway import media_jobs

AUDIO_ASSET_KIND = "aud"
DEFAULT_AUDIO_MEDIA_TYPE = "audio/wav"
DEFAULT_AUDIO_SUFFIX = ".wav"
_AUDIO_INTENTS = frozenset({"audio.generate", "music.generate", "sfx.generate"})
_AUDIO_RESPONSE_FORMAT_SUFFIXES = {
    "wav": ".wav",
    "mp3": ".mp3",
    "flac": ".flac",
    "ogg": ".ogg",
    "opus": ".ogg",
    "m4a": ".m4a",
}
_AUDIO_MEDIA_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
}


class AudioAssetStore(Protocol):
    """Small contract route integration can satisfy with ``store_asset_bytes``."""

    def __call__(
        self,
        content: bytes,
        *,
        kind: str,
        suffix: str,
        owner_key_hash: str | None = None,
    ) -> str: ...


@dataclass(frozen=True)
class AudioArtifact:
    content: bytes
    media_type: str = DEFAULT_AUDIO_MEDIA_TYPE
    suffix: str = DEFAULT_AUDIO_SUFFIX


def infer_audio_intent(request_body: Mapping[str, Any]) -> str:
    """Return the generic durable-job intent for an audio generation request."""
    raw = str(request_body.get("type") or request_body.get("intent") or "").strip().lower()
    if raw in _AUDIO_INTENTS:
        return raw
    if raw in {"music", "song"}:
        return "music.generate"
    if raw in {"sfx", "sound_effect", "sound-effect", "sound_effects"}:
        return "sfx.generate"
    return "audio.generate"


def audio_job_from_request(
    request_body: Mapping[str, Any],
    *,
    request_id: str = "",
    workflow: str | None = None,
    backend: str | None = None,
    app_profile: str | None = None,
    api_key_name: str | None = None,
    parent_request_id: str | None = None,
) -> media_jobs.MediaJob:
    """Create a safe ``MediaJob`` record without persisting prompt text."""
    metadata = _safe_request_metadata(request_body)
    return media_jobs.MediaJob(
        intent=infer_audio_intent(request_body),
        workflow=workflow,
        backend=backend,
        request_id=request_id,
        app_profile=app_profile,
        api_key_name=api_key_name,
        parent_request_id=parent_request_id,
        metadata=metadata,
    )


def store_audio_artifacts(
    artifacts: Iterable[AudioArtifact],
    *,
    store_asset: AudioAssetStore,
    owner_key_hash: str | None = None,
) -> list[dict[str, Any]]:
    """Persist audio bytes through the supplied asset-store contract."""
    assets: list[dict[str, Any]] = []
    for index, artifact in enumerate(artifacts):
        if not artifact.content:
            continue
        suffix = _normalize_suffix(artifact.suffix)
        media_type = _normalize_media_type(artifact.media_type, suffix)
        asset_id = store_asset(
            artifact.content,
            kind=AUDIO_ASSET_KIND,
            suffix=suffix,
            owner_key_hash=owner_key_hash,
        )
        assets.append(
            {
                "asset_id": asset_id,
                "kind": "audio",
                "media_type": media_type,
                "size_bytes": len(artifact.content),
                "index": index,
            }
        )
    return assets


def audio_artifacts_from_result(
    result: bytes | bytearray | memoryview | Mapping[str, Any],
    *,
    response_format: str | None = None,
    media_type: str | None = None,
) -> list[AudioArtifact]:
    """Extract audio bytes from common OpenAI-ish audio generation results."""
    suffix = suffix_for_response_format(response_format)
    default_media_type = media_type or _AUDIO_MEDIA_TYPES.get(suffix, DEFAULT_AUDIO_MEDIA_TYPE)
    if isinstance(result, (bytes, bytearray, memoryview)):
        content = bytes(result)
        # Bytes are stored by what they are: the backend's container decides,
        # not the format the caller asked for (a FLAC workflow answers FLAC).
        actual = _suffix_from_content(content) or _suffix_from_media_type(media_type or "") or suffix
        return [AudioArtifact(content, _AUDIO_MEDIA_TYPES.get(actual, default_media_type), actual)]

    data = result.get("data") if isinstance(result, Mapping) else None
    if isinstance(data, list):
        artifacts: list[AudioArtifact] = []
        for item in data:
            if not isinstance(item, Mapping):
                continue
            artifact = _artifact_from_mapping(item, default_media_type, suffix)
            if artifact is not None:
                artifacts.append(artifact)
        return artifacts

    artifact = _artifact_from_mapping(result, default_media_type, suffix)
    return [artifact] if artifact is not None else []


def complete_audio_job_payload(assets: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Return an event payload for a successful audio media job."""
    assets_list = [dict(asset) for asset in assets]
    return {
        "phase": "completed",
        "asset_count": len(assets_list),
        "media_types": sorted(
            {
                str(asset.get("media_type"))
                for asset in assets_list
                if asset.get("media_type")
            }
        ),
    }


def suffix_for_response_format(response_format: str | None) -> str:
    raw = str(response_format or "").strip().lower().lstrip(".")
    return _AUDIO_RESPONSE_FORMAT_SUFFIXES.get(raw, DEFAULT_AUDIO_SUFFIX)


def _safe_request_metadata(request_body: Mapping[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key in ("model", "duration", "seed", "response_format", "type"):
        if key not in request_body:
            continue
        value = request_body.get(key)
        if value is not None and _safe_scalar(value):
            metadata[key] = value
    if "tags" in request_body and isinstance(request_body["tags"], list):
        safe_tags = [tag for tag in request_body["tags"] if _safe_scalar(tag)]
        metadata["tag_count"] = len(safe_tags)
    return metadata


def _artifact_from_mapping(
    item: Mapping[str, Any],
    default_media_type: str,
    default_suffix: str,
) -> AudioArtifact | None:
    media_type = str(item.get("media_type") or item.get("content_type") or default_media_type)
    suffix = _suffix_from_media_type(media_type) or default_suffix
    raw = item.get("audio") or item.get("b64_json") or item.get("data")
    if isinstance(raw, str):
        try:
            return AudioArtifact(base64.b64decode(raw, validate=True), media_type, suffix)
        except (binascii.Error, ValueError):
            return None
    if isinstance(raw, (bytes, bytearray, memoryview)):
        return AudioArtifact(bytes(raw), media_type, suffix)
    return None


def _normalize_suffix(suffix: str) -> str:
    suffix = str(suffix or DEFAULT_AUDIO_SUFFIX).strip().lower()
    if not suffix.startswith("."):
        suffix = f".{suffix}"
    if suffix not in _AUDIO_MEDIA_TYPES:
        return DEFAULT_AUDIO_SUFFIX
    return suffix


def _normalize_media_type(media_type: str, suffix: str) -> str:
    media_type = str(media_type or "").strip().lower()
    if media_type.startswith("audio/"):
        return media_type
    return _AUDIO_MEDIA_TYPES.get(suffix, DEFAULT_AUDIO_MEDIA_TYPE)


def _suffix_from_content(content: bytes) -> str | None:
    """The container suffix from the leading bytes, or None if unrecognized."""
    if content[:4] == b"RIFF" and content[8:12] == b"WAVE":
        return ".wav"
    if content[:4] == b"fLaC":
        return ".flac"
    if content[:4] == b"OggS":
        return ".ogg"
    if content[:3] == b"ID3" or content[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return ".mp3"
    if content[4:8] == b"ftyp":
        return ".m4a"
    return None


def _suffix_from_media_type(media_type: str) -> str | None:
    normalized = str(media_type or "").split(";", 1)[0].strip().lower()
    for suffix, candidate in _AUDIO_MEDIA_TYPES.items():
        if candidate == normalized:
            return suffix
    return None


def _safe_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))
