"""Closed, immutable gateway display copy loaded from registry data."""

from __future__ import annotations

from pathlib import Path
from string import Formatter
from types import MappingProxyType
from typing import Any, Mapping, NamedTuple

import yaml

_GATEWAY_MESSAGES_PATH = Path(__file__).resolve().parents[1] / "registry" / "gateway_messages.yml"
_MESSAGE_FIELDS = frozenset({
    "metadata_description", "tool_call_advisory", "backend_label", "unknown_family",
    "prompt_namespace_description",
    "workflow_namespace_description",
    "chat_namespace_description",
    "routing_namespace_description",
    "long_video_not_supported", "video_extend_not_supported", "surface_existing_clients", "surface_new_setup",
    "current_loaded_model_description", "current_loaded_fast_model_description",
    "transition_unsupported", "engine_transitioning", "audio_translation_unsupported",
    "video_extend_shots_unsupported", "video_extend_not_configured", "voice_design_not_configured",
    "measurement_backend_unavailable", "mcp_reconnect_required", "definition_unavailable",
    "asset_delete_not_found_description", "protocol_failure_default", "protocol_upstream_refused",
    "stt_chunking_defaults_note", "stt_chunking_all_saved_note",
})
# Placeholders each of these flat templates takes (the rest are plain text, or checked by the explicit calls below).
_FLAT_SLOTS: Mapping[str, frozenset[str]] = {
    **{name: frozenset() for name in (
        "current_loaded_model_description", "current_loaded_fast_model_description",
        "transition_unsupported", "engine_transitioning", "audio_translation_unsupported",
        "video_extend_shots_unsupported", "video_extend_not_configured", "voice_design_not_configured",
        "measurement_backend_unavailable", "mcp_reconnect_required", "definition_unavailable",
        "protocol_failure_default", "protocol_upstream_refused",
        "stt_chunking_defaults_note", "stt_chunking_all_saved_note")},
    "asset_delete_not_found_description": frozenset({"error_code"}),
}
_TABLES_KEY = "tables"


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


def _read_document(path: str | Path) -> Mapping[str, Any]:
    document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise ValueError("gateway messages must be a mapping")
    return document


def load_gateway_messages(path: str | Path = _GATEWAY_MESSAGES_PATH) -> Mapping[str, str]:
    """Load closed, immutable display copy; data cannot select executable code."""
    return _messages_from(_read_document(path))


def _messages_from(document: Mapping[str, Any]) -> Mapping[str, str]:
    required = _MESSAGE_FIELDS | {"schema_version"}
    unknown = set(document) - required - {_TABLES_KEY}
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
    for name, slots in _FLAT_SLOTS.items():
        _validate_message_template(document[name], name, set(slots))
    return MappingProxyType({name: document[name] for name in _MESSAGE_FIELDS})


class _Table(NamedTuple):
    """Shape of one keyed table. Code reads some tables by literal key (``closed``: exactly these keys) and others by
    a key it is handed (``required``: these must exist, any others may)."""

    key_type: type
    keys: frozenset
    closed: bool
    failure: bool = False


_TABLES: Mapping[str, _Table] = {
    # Settings and variables the gateway refuses to start with: which ones is a decision, so the keys are closed.
    "removed_auth_settings": _Table(str, frozenset({
        "enabled", "lan_bypass_enabled", "lan_bypass_cidrs", "path_tokens", "ollama_compat_discovery"}), True),
    "removed_auth_env": _Table(str, frozenset({"AZIZAM_AUTH_ENABLED", "AZIZAM_LAN_BYPASS", "OLLAMA_COMPAT_DISCOVERY"}), True),
    "access_errors": _Table(str, frozenset({
        "key_required", "key_invalid", "app_disabled", "person_disabled", "person_signin_refused"}), True),
    "protocol_failures": _Table(str, frozenset({
        "invalid_description", "malformed_response", "mapping_failed", "transport_failed", "budget_exceeded",
        "engine_credential_unavailable", "unknown_operation", "unknown_protocol", "ambiguous_model", "engine_disabled",
        "protocol_dispatch_refused"}), True),
    # Sentences the callers pick by a reason, status or code they are handed.
    "client_reasons": _Table(str, frozenset({"proxy_error", "upstream_failed"}), False),
    "client_status_reasons": _Table(int, frozenset({400}), False),
    "client_code_reasons": _Table(str, frozenset({"model_not_found", "server_error"}), False),
    "public_failures": _Table(str, frozenset({"unknown"}), False, failure=True),
}
# The values a failure sentence may name, and the format a number may take (``{max_chars:,}``).
_FAILURE_SLOTS = frozenset({"maximum", "minimum", "max_chars", "max_images", "input", "model"})
_FAILURE_FIELDS = frozenset({"layer", "message", "api_message"})


def _validate_failure_template(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    try:
        parts = list(Formatter().parse(value))
    except ValueError as error:
        raise ValueError(f"{name}: invalid message template") from error
    for _literal, field, format_spec, conversion in parts:
        if field is not None and (field not in _FAILURE_SLOTS or format_spec not in ("", ",") or conversion is not None):
            raise ValueError(f"{name}: unsupported placeholder")


def _validate_table(table: str, spec: _Table, rows: Any) -> Mapping[Any, Any]:
    if not isinstance(rows, Mapping):
        raise ValueError(f"gateway messages: tables.{table} must be a mapping")
    for key in rows:
        if type(key) is not spec.key_type:
            raise ValueError(f"gateway messages: tables.{table}: keys must be {spec.key_type.__name__}")
    if spec.closed and set(rows) != spec.keys:
        raise ValueError(f"gateway messages: tables.{table}: keys must be exactly {sorted(spec.keys, key=str)}")
    if not spec.keys <= set(rows):
        raise ValueError(f"gateway messages: tables.{table}: missing keys {sorted(spec.keys - set(rows), key=str)}")
    result: dict[Any, Any] = {}
    for key, value in rows.items():
        name = f"tables.{table}.{key}"
        if not spec.failure:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty text")
            if "{" in value or "}" in value:
                raise ValueError(f"{name}: this table takes plain text, with no placeholders")
            result[key] = value
            continue
        if not isinstance(value, Mapping) or not {"layer", "message"} <= set(value) <= _FAILURE_FIELDS:
            raise ValueError(f"{name} must hold layer and message, and may hold api_message")
        if not isinstance(value["layer"], str) or not value["layer"].strip():
            raise ValueError(f"{name}.layer must be non-empty text")
        for field in ("message", "api_message"):
            if field in value:
                _validate_failure_template(value[field], f"{name}.{field}")
        result[key] = tuple(value[field] for field in ("layer", "message", "api_message") if field in value)
    return MappingProxyType(result)


def load_gateway_message_tables(path: str | Path = _GATEWAY_MESSAGES_PATH) -> Mapping[str, Mapping[Any, Any]]:
    """Load the keyed copy tables, each checked against its own shape. A failure row is ``(layer, message[, api_message])``."""
    return _tables_from(_read_document(path))


def _tables_from(document: Mapping[str, Any]) -> Mapping[str, Mapping[Any, Any]]:
    tables = document.get(_TABLES_KEY)
    if not isinstance(tables, Mapping):
        raise ValueError("gateway messages: tables must be a mapping")
    if set(tables) != set(_TABLES):
        raise ValueError(f"gateway messages: tables must be exactly {sorted(_TABLES)}")
    return MappingProxyType({name: _validate_table(name, spec, tables[name]) for name, spec in _TABLES.items()})


# Load once, as with other release registry data; malformed copy fails at startup.
_DOCUMENT = _read_document(_GATEWAY_MESSAGES_PATH)
GATEWAY_MESSAGES = _messages_from(_DOCUMENT)
GATEWAY_MESSAGE_TABLES = _tables_from(_DOCUMENT)
