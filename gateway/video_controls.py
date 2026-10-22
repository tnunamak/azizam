"""Gateway-level controls for ``POST /v1/videos``.

These are not workflow inputs: they change what the gateway does with the
finished media, so they live in their own extension namespace and never reach a
backend binding.

``extensions["com.azizam.video"]["audio"] = false`` asks the gateway to store
and return the finished clip without its audio track. The namespace and the
control names are data conventions; nothing here names an engine or a model.
"""

from __future__ import annotations

from typing import Any, Mapping

VIDEO_EXTENSION_NAMESPACE = "com.azizam.video"
AUDIO_CONTROL = "audio"
VIDEO_CONTROLS: frozenset[str] = frozenset({AUDIO_CONTROL})


class VideoControlError(ValueError):
    """A malformed ``extensions["com.azizam.video"]`` block."""

    def __init__(self, message: str, *, param: str) -> None:
        super().__init__(message)
        self.param = param


def parse_video_controls(body: Mapping[str, Any]) -> dict[str, Any]:
    """Return the validated gateway controls in ``body``.

    Absent controls mean the default behaviour (audio kept). A block that is
    not an object, an unknown control name, or a non-boolean value is refused
    before any asset is fetched or uploaded.
    """
    extensions = body.get("extensions") if isinstance(body, Mapping) else None
    if not isinstance(extensions, Mapping):
        return {}
    block = extensions.get(VIDEO_EXTENSION_NAMESPACE)
    if block is None:
        return {}
    if not isinstance(block, Mapping):
        raise VideoControlError(
            f"extensions.{VIDEO_EXTENSION_NAMESPACE} must be an object",
            param=f"extensions.{VIDEO_EXTENSION_NAMESPACE}",
        )
    for name in block:
        if name not in VIDEO_CONTROLS:
            raise VideoControlError(
                f"{name} is not a video control",
                param=f"extensions.{VIDEO_EXTENSION_NAMESPACE}.{name}",
            )
    audio = block.get(AUDIO_CONTROL, True)
    if not isinstance(audio, bool):
        raise VideoControlError(
            f"{AUDIO_CONTROL} must be true or false",
            param=f"extensions.{VIDEO_EXTENSION_NAMESPACE}.{AUDIO_CONTROL}",
        )
    return {AUDIO_CONTROL: audio}
