"""Closed, immutable gateway display copy loaded from registry data."""

from __future__ import annotations

from pathlib import Path
from string import Formatter
from types import MappingProxyType
from typing import Mapping

import yaml

_GATEWAY_MESSAGES_PATH = Path(__file__).resolve().parents[1] / "registry" / "gateway_messages.yml"
_MESSAGE_FIELDS = frozenset({
    "metadata_description", "tool_call_advisory", "backend_label", "unknown_family",
    "long_video_not_supported", "video_extend_not_supported", "surface_existing_clients", "surface_new_setup",
    "current_loaded_model_description", "current_loaded_fast_model_description",
})


def _validate_message_template(value: str, name: str, fields: set[str]) -> None:
    """Only exact, single-use placeholders; no attribute access or formatting."""
    try:
        parts = list(Formatter().parse(value))
    except ValueError as error:
        raise ValueError(f"{name}: invalid message template") from error
    seen: list[str] = []
    for _literal, field, format_spec, conversion in parts:
        if field is None:
            continue
        if field not in fields or format_spec or conversion is not None:
            raise ValueError(f"{name}: unsupported placeholder")
        seen.append(field)
    if set(seen) != fields or len(seen) != len(fields):
        raise ValueError(f"{name}: each required placeholder must appear exactly once")


def load_gateway_messages(path: str | Path = _GATEWAY_MESSAGES_PATH) -> Mapping[str, str]:
    """Load closed, immutable display copy; data cannot select executable code."""
    document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise ValueError("gateway messages must be a mapping")
    required = _MESSAGE_FIELDS | {"schema_version"}
    unknown = set(document) - required
    if unknown:
        raise ValueError(f"gateway messages: unknown keys {sorted(map(str, unknown))}")
    missing = required - set(document)
    if missing:
        raise ValueError(f"gateway messages: missing keys {sorted(missing)}")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("gateway messages: schema_version must be 1")
    for name in _MESSAGE_FIELDS:
        if not isinstance(document[name], str) or not document[name].strip():
            raise ValueError(f"{name} must be non-empty text")
    _validate_message_template(document["tool_call_advisory"], "tool_call_advisory", {"family", "backend_label"})
    _validate_message_template(document["backend_label"], "backend_label", {"backend_hint"})
    _validate_message_template(document["long_video_not_supported"], "long_video_not_supported", {"model", "max_seconds"})
    _validate_message_template(document["video_extend_not_supported"], "video_extend_not_supported", {"model"})
    _validate_message_template(document["surface_existing_clients"], "surface_existing_clients", {"convenience"})
    _validate_message_template(document["surface_new_setup"], "surface_new_setup", {"canonical"})
    for name in ("current_loaded_model_description", "current_loaded_fast_model_description"):
        _validate_message_template(document[name], name, set())
    return MappingProxyType({name: document[name] for name in _MESSAGE_FIELDS})


# Load once, as with other release registry data; malformed copy fails at startup.
GATEWAY_MESSAGES = load_gateway_messages()
