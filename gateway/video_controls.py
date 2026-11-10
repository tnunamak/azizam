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

from gateway.extension_controls import ControlValidationError, control_group, parse_control_group

_DELIVERY_GROUP = control_group("clip_delivery")
VIDEO_EXTENSION_NAMESPACE = _DELIVERY_GROUP.namespace
AUDIO_CONTROL = _DELIVERY_GROUP.controls["retain_sound"].field
VIDEO_CONTROLS: frozenset[str] = _DELIVERY_GROUP.public_fields


class VideoControlError(ValueError):
    """A malformed gateway delivery-control block."""

    def __init__(self, message: str, *, param: str) -> None:
        super().__init__(message)
        self.param = param


def parse_video_controls(body: Mapping[str, Any]) -> dict[str, Any]:
    """Return validated public controls while preserving the legacy exception."""
    try:
        return parse_control_group(body, _DELIVERY_GROUP)
    except ControlValidationError as exc:
        raise VideoControlError(str(exc), param=exc.param) from exc
