"""Generated-media provenance: the metadata control and the provenance record.

Two jobs, both pure (no I/O, no FastAPI, no ``proxy`` import):

1. The delivery control. A request may carry
   ``extensions["com.azizam.workflow"].metadata = "full" | "none"``. The
   gateway always STORES generated media with full metadata. The control (or,
   when absent, the app profile default in ``profile.media_metadata.default``)
   decides whether the bytes DELIVERED to the client keep that metadata.

2. The provenance record: a small JSON document that says how an asset was
   made (requested model and alias, what it resolved to, seed, parameters,
   workflow id/version/hash, timestamp, request id and receipt link). It is
   embedded next to ComfyUI's own ``prompt`` and ``workflow`` data.

The record is built from an allow-list. Secrets, API keys, auth headers and
uploaded media must never reach it, so every value passes
:func:`sanitize_provenance` before it is embedded.
"""

from __future__ import annotations

from gateway.compat import read_field

import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping

WORKFLOW_EXTENSION_NAMESPACE = "com.azizam.workflow"

# Name of the control inside ``extensions[com.azizam.workflow]``.
METADATA_CONTROL = "metadata"
METADATA_FULL = "full"
METADATA_NONE = "none"
METADATA_MODES: tuple[str, ...] = (METADATA_FULL, METADATA_NONE)

# Controls the gateway itself consumes. Workflow validators must not treat
# them as workflow inputs (they are not in any manifest) and must not bind
# them into a backend request.
RESERVED_GATEWAY_CONTROLS: frozenset[str] = frozenset({METADATA_CONTROL})

# Metadata keys the gateway writes into media bytes.
PROVENANCE_KEY = "azizam_provenance"
# Permanent compatibility for metadata already embedded in generated assets.
PROVENANCE_KEYS = (PROVENANCE_KEY, "vividfish_provenance")
PROVENANCE_SCHEMA = "azizam.provenance/1"


def read_provenance_value(values: Mapping[str, Any]) -> Any:
    return next((values[key] for key in PROVENANCE_KEYS if key in values), None)


def read_provenance_lines(text: str, separator: str) -> str | None:
    for key in PROVENANCE_KEYS:
        for line in text.splitlines():
            if line.startswith(key + separator):
                return line.split(separator, 1)[1]
    return None

_METADATA_ERROR = (
    f"{METADATA_CONTROL} must be one of " + ", ".join(repr(m) for m in METADATA_MODES)
)


class MetadataModeError(ValueError):
    """Raised when the metadata control carries an unusable value."""


def parse_metadata_mode(value: Any) -> str:
    """Return ``"full"`` or ``"none"`` for a control value, or raise."""
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in METADATA_MODES:
            return normalized
    raise MetadataModeError(_METADATA_ERROR)


def requested_metadata_mode(body: Mapping[str, Any] | None) -> str | None:
    """Read the per-request control from a parsed request body.

    Returns ``None`` when the request did not set it. Raises
    :class:`MetadataModeError` for a bad value or a malformed envelope.
    """
    if not isinstance(body, Mapping):
        return None
    container = body.get("extensions")
    if container is None:
        return None
    if not isinstance(container, Mapping):
        raise MetadataModeError("extensions must be a mapping")
    block = read_field(container, WORKFLOW_EXTENSION_NAMESPACE)
    if block is None:
        return None
    if not isinstance(block, Mapping):
        raise MetadataModeError(
            f"extensions.{WORKFLOW_EXTENSION_NAMESPACE} must be a mapping"
        )
    if METADATA_CONTROL not in block:
        return None
    return parse_metadata_mode(block[METADATA_CONTROL])


def resolve_metadata_mode(requested: str | None, profile_default: str | None) -> str:
    """Request value wins; else the profile default; else ``none`` (safe)."""
    if requested in METADATA_MODES:
        return str(requested)
    if profile_default in METADATA_MODES:
        return str(profile_default)
    return METADATA_NONE


# ---------------------------------------------------------------------------
# Provenance record
# ---------------------------------------------------------------------------

_MAX_STRING = 2000
_MAX_ITEMS = 64
_MAX_DEPTH = 4

# Parameter names that must never be embedded, whatever their value.
_SECRET_NAME = re.compile(
    r"(api[_-]?key|secret|token|password|passwd|authorization|bearer|cookie|"
    r"credential|signature|\bsig\b|private[_-]?key)",
    re.IGNORECASE,
)
# Value shapes that look like credentials or inline media.
_SECRET_VALUE = re.compile(
    r"(\bsk-[A-Za-z0-9_-]{8,}|\bBearer\s+\S+|\bgh[pousr]_[A-Za-z0-9]{16,}|"
    r"\bxox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|[?&](?:sig|api_key|key|token)=\S+)",
    re.IGNORECASE,
)
_DATA_URI = re.compile(r"^\s*data:[^,]{0,80};base64,", re.IGNORECASE)
_LONG_BASE64 = re.compile(r"^[A-Za-z0-9+/=\s]{512,}$")
_REDACTED = "[redacted]"


def _sanitize_value(value: Any, depth: int) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if _DATA_URI.match(value) or _LONG_BASE64.match(value):
            return "[media omitted]"
        if _SECRET_VALUE.search(value):
            return _REDACTED
        return value if len(value) <= _MAX_STRING else value[:_MAX_STRING] + "..."
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "[media omitted]"
    if depth >= _MAX_DEPTH:
        return "[truncated]"
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_ITEMS:
                break
            name = str(key)
            out[name] = (
                _REDACTED if _SECRET_NAME.search(name) else _sanitize_value(item, depth + 1)
            )
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_sanitize_value(item, depth + 1) for item in list(value)[:_MAX_ITEMS]]
    return _sanitize_value(str(value), depth)


def sanitize_provenance(value: Any) -> Any:
    """Return a JSON-safe copy of ``value`` with secrets and media removed."""
    return _sanitize_value(value, 0)


def _iso(timestamp: float | int | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def build_provenance_record(
    *,
    receipt: Any = None,
    kind: str,
    created_at: float | int | None = None,
    requested_model: str | None = None,
    model_alias: str | None = None,
    resolved_model: str | None = None,
    workflow_id: str | None = None,
    workflow_version: str | None = None,
    workflow_hash: str | None = None,
    backend: str | None = None,
    seed: int | None = None,
    parameters: Mapping[str, Any] | None = None,
    request_id: str | None = None,
    receipt_path: str | None = None,
    index: int | None = None,
) -> dict[str, Any]:
    """Assemble one provenance record from known, non-secret facts.

    ``receipt`` (a ``RouteReceipt``) fills every field the caller leaves
    unset. Only the allow-listed fields below are read from it: nothing
    else on the receipt (key names, hashes, input values) is copied.
    """

    def from_receipt(name: str) -> Any:
        return getattr(receipt, name, None) if receipt is not None else None

    requested_model = requested_model or from_receipt("requested_model")
    resolved_model = resolved_model or from_receipt("resolved_model")
    if model_alias is None and requested_model and resolved_model and requested_model != resolved_model:
        model_alias = requested_model  # the client used an alias; record both names
    request_id = request_id or from_receipt("request_id")
    if receipt_path is None and request_id:
        receipt_path = f"/gateway/v1/receipts/{request_id}"
    record: dict[str, Any] = {
        "schema": PROVENANCE_SCHEMA,
        "generator": "azizam-gateway",
        "kind": kind,
        "created_at": _iso(created_at if created_at is not None else _now()),
        "request_id": request_id,
        "receipt": receipt_path,
        "requested_model": requested_model,
        "model_alias": model_alias,
        "resolved_model": resolved_model,
        "workflow_id": workflow_id
        or from_receipt("manifest_id")
        or from_receipt("resolved_workflow"),
        "workflow_version": workflow_version or from_receipt("manifest_version"),
        "workflow_hash": workflow_hash or from_receipt("manifest_hash"),
        "backend": backend or from_receipt("backend"),
        "seed": seed,
        "parameters": dict(parameters) if parameters else None,
        "index": index,
    }
    cleaned = sanitize_provenance({k: v for k, v in record.items() if v is not None})
    return cleaned


def _now() -> float:
    import time

    return time.time()


def provenance_json(record: Mapping[str, Any]) -> str:
    """Compact, sorted JSON for embedding. Re-sanitizes as a last gate."""
    return json.dumps(
        sanitize_provenance(record), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
