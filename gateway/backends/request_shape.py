"""Privacy-safe request-shape facts for cancellation/reliability observability.

Captures only structural fields — never prompts, messages, tool
names/arguments/results, user content, keys, auth, or raw bodies. Everything
this module returns is safe to attach to ``RouteReceipt.metadata`` as-is:
``gateway.receipt_store`` already gates that field through
``gateway.redaction`` (forbidden-key vocabulary), so this module does not
duplicate that enforcement — it just never produces a forbidden key.

Reuses the existing receipt/logging surface rather than adding a parallel
observability product: callers attach the dict this returns under
``receipt.metadata["request_shape"]``, and it rides the same
accepted/outer-egress/internal-ingress/backend-egress/terminal boundaries the
route receipt already marks with ``finish()``/warnings.

Motivating case (see incident note on task 59337 / 92298): a request can
reach a backend with ``stream: false`` and an absent or ``-1`` token limit —
an unbounded non-streaming call with no cancellation-friendly path. That
combination must be directly queryable from receipts without inspecting
prompt content; ``is_unbounded_nonstream`` below is that query.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_UNBOUNDED_TOKEN_SENTINELS = (None, -1)
_OUTPUT_LIMIT_FIELDS = ("max_completion_tokens", "max_tokens", "n_predict")


def request_shape_fields(body: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return structural-only facts about an OpenAI-shaped request body.

    Safe by construction: reads only known structural keys (stream flag,
    numeric token limits, message/tool counts) and never copies message,
    tool-argument, or other free-text content into the result.
    """
    if not isinstance(body, Mapping):
        return {}

    fields: dict[str, Any] = {}

    stream = body.get("stream")
    fields["stream"] = bool(stream) if stream is not None else False

    for key in ("max_tokens", "max_completion_tokens", "n_predict"):
        if key in body:
            fields[key] = _coerce_numeric(body.get(key))

    messages = body.get("messages")
    if isinstance(messages, list):
        fields["message_count"] = len(messages)

    tools = body.get("tools")
    if isinstance(tools, list):
        fields["tool_count"] = len(tools)

    fields["is_unbounded_nonstream"] = is_unbounded_nonstream(fields)
    return fields


def is_unbounded_nonstream(fields: Mapping[str, Any]) -> bool:
    """True when ``stream`` is false/absent and no token limit bounds the call.

    A request in this shape has no natural termination signal the gateway can
    observe short of the backend finishing on its own, which is exactly the
    combination the audit flagged as invisible to the gateway's liveness
    record (``awaiting_upstream_headers`` with no forward progress evidence).
    """
    if fields.get("stream"):
        return False
    limits = [
        fields.get(key)
        for key in ("max_tokens", "max_completion_tokens", "n_predict")
        if key in fields
    ]
    if not limits:
        return True
    return all(limit in _UNBOUNDED_TOKEN_SENTINELS for limit in limits)


def effective_output_limit(body: Mapping[str, Any] | None) -> int | None:
    """Return the final explicit output cap from an outbound request shape.

    This is deliberately a closed scalar projection: it does not preserve the
    source field or inspect request content.  ``0`` is an explicit cap, while
    absent, invalid, and the documented ``-1`` unbounded sentinel are all
    represented as ``None``.
    """
    if not isinstance(body, Mapping):
        return None
    for key in _OUTPUT_LIMIT_FIELDS:
        if key not in body:
            continue
        value = body[key]
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        return value if value >= 0 else None
    return None


def request_body_byte_count(raw_body: bytes | None) -> int:
    return len(raw_body) if raw_body else 0


def _coerce_numeric(value: Any) -> Any:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


# The staged lifecycle a request-shape-bearing route may record. Bounded so a
# route bug (a retry loop, a duplicate call) cannot grow one receipt's
# metadata unboundedly — extra stage() calls past this count are dropped, not
# silently overwritten, so the cap itself is visible on inspection.
REQUEST_SHAPE_STAGES = (
    "accepted",
    "outer_egress",
    "internal_ingress",
    "backend_egress",
    "terminal",
)
MAX_RECORDED_STAGES = 8
MAX_STAGE_FIELD_COUNT = 16
MAX_STAGE_STRING_LENGTH = 64
MAX_STAGE_NUMERIC_MAGNITUDE = 10_000_000
MAX_STAGE_LIST_LENGTH = 16


def _bound_stage_value(value: Any) -> Any:
    """Cap one field value to a size a stage entry may safely carry.

    This is the boundary the observability contract requires independent of
    caller discipline — every current call site only ever passes small fixed
    enum-like strings/counts, but the cap must hold even if that changes,
    not merely happen to hold today.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value[:MAX_STAGE_STRING_LENGTH]
    if isinstance(value, int):
        return max(-MAX_STAGE_NUMERIC_MAGNITUDE, min(MAX_STAGE_NUMERIC_MAGNITUDE, value))
    if isinstance(value, float):
        return max(-float(MAX_STAGE_NUMERIC_MAGNITUDE), min(float(MAX_STAGE_NUMERIC_MAGNITUDE), value))
    if isinstance(value, (list, tuple)):
        return [_bound_stage_value(item) for item in value[:MAX_STAGE_LIST_LENGTH]]
    return value


def record_request_shape_stage(
    metadata: dict[str, Any],
    stage: str,
    body: Mapping[str, Any] | None,
    *,
    extra: Mapping[str, Any] | None = None,
) -> None:
    """Append a privacy-safe request-shape snapshot for ``stage`` to ``metadata``.

    Writes to ``metadata["request_shape_stages"]`` — a bounded list of
    ``{"stage": ..., **request_shape_fields(body), **extra}`` dicts — so a
    route can be queried for what it knew about a request's shape at each
    named boundary (accepted, outer egress to LiteLLM/backend, internal
    ingress at the second gateway hop, backend egress, and the terminal
    outcome) without a parallel observability subsystem: this rides the
    existing ``RouteReceipt.metadata``/receipt store surface.

    Silently caps at ``MAX_RECORDED_STAGES`` entries, ``MAX_STAGE_FIELD_COUNT``
    fields per entry, and per-value size (string length, numeric magnitude,
    list length) — a route bug must not turn one request's receipt into
    unbounded storage.

    This bounding is a producer-side courtesy, not the trust boundary itself
    — every current call site only ever writes the fixed vocabulary below,
    but a careless future caller could still write arbitrary keys/values
    into ``metadata["request_shape_stages"]`` directly. ``sanitize_request_shape_stages_for_admin``
    is the actual boundary: it re-parses this list from scratch against a
    closed schema before anything reaches the admin audience, independent of
    whatever this function (or any other internal caller) happened to write.
    """
    stages = metadata.setdefault("request_shape_stages", [])
    if len(stages) >= MAX_RECORDED_STAGES:
        return
    fields = request_shape_fields(body)
    if extra:
        fields.update(extra)
    entry = {"stage": stage, **fields}
    if len(entry) > MAX_STAGE_FIELD_COUNT:
        entry = dict(list(entry.items())[:MAX_STAGE_FIELD_COUNT])
    entry = {key: _bound_stage_value(value) for key, value in entry.items()}
    stages.append(entry)


# ---------------------------------------------------------------------------
# Admin trust boundary: a closed allowlist schema, re-parsed from scratch.
#
# ``record_request_shape_stage`` above is producer-side bounding — it caps
# what THIS module writes. It is not the trust boundary the admin audience
# actually needs: any other internal caller (a future route, a copy-paste
# mistake, drift) could write directly into ``metadata["request_shape"]`` /
# ``metadata["request_shape_stages"]`` with arbitrary keys, nested mappings,
# or unbounded values, and a blocklist-only check (gateway.redaction) would
# let anything through that isn't on the forbidden-key list — including
# structurally-nested secrets one level deeper than that module's single-
# level recursion reaches (see independent-gate-3.md finding 4:
# ``{'headers': {'authorization': ...}}`` inside a stage-list entry).
#
# The functions below instead re-derive the admin-visible shape from an
# EXACT allowlist of keys/types/lengths/enum values/count, discarding
# everything else — including any nested mapping or list, which this
# surface has no legitimate use for at all. "Discard unknown" rather than
# "reject unknown" so one route's malformed shape entry does not fail the
# whole receipt write; the sanitizer's job is containment, not validation
# enforcement (record()'s existing gateway.redaction check still rejects
# outright-forbidden top-level keys before anything is written).
# ---------------------------------------------------------------------------

_SHAPE_FIELD_TYPES: dict[str, type | tuple[type, ...]] = {
    "stream": bool,
    "max_tokens": (int, float),
    "max_completion_tokens": (int, float),
    "n_predict": (int, float),
    "message_count": int,
    "tool_count": int,
    "is_unbounded_nonstream": bool,
}

_STAGE_EXTRA_FIELD_TYPES: dict[str, type | tuple[type, ...]] = {
    "status_code": int,
    "body_bytes": int,
}

TERMINAL_REASONS = frozenset(
    {
        "cancelled",
        "completed",
        "connect_error",
        "disconnected",
        "failed",
        "idle_timeout",
        "internal_error",
        "read_error",
    }
)

MAX_ADMIN_STAGE_COUNT = MAX_RECORDED_STAGES

_REJECTED = object()  # sentinel: distinct from any legitimate field value, including None/False/0


def _sanitize_typed_value(value: Any, expected: type | tuple[type, ...]) -> Any:
    """Return ``value`` if it matches ``expected`` (bool never satisfies a
    non-bool numeric type — ``isinstance(True, int)`` is True in Python, and
    a stray boolean must not silently pass as a token count), else ``_REJECTED``."""
    if isinstance(value, bool) and expected is not bool:
        return _REJECTED
    if not isinstance(value, expected):
        return _REJECTED
    if isinstance(value, (int, float)):
        return max(-MAX_STAGE_NUMERIC_MAGNITUDE, min(MAX_STAGE_NUMERIC_MAGNITUDE, value))
    return value


def sanitize_request_shape_fields_for_admin(shape: Any) -> dict[str, Any]:
    """Re-parse a request-shape dict against the closed field allowlist.

    Every key not in ``_SHAPE_FIELD_TYPES`` is discarded, including any
    nested mapping or list — this surface only ever carries flat structural
    scalars, so a nested value is by definition not a legitimate shape
    field, not something to sanitize-and-keep.
    """
    if not isinstance(shape, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key, expected in _SHAPE_FIELD_TYPES.items():
        if key not in shape:
            continue
        sanitized = _sanitize_typed_value(shape[key], expected)
        if sanitized is not _REJECTED:
            out[key] = sanitized
    return out


def sanitize_request_shape_stages_for_admin(stages: Any) -> list[dict[str, Any]]:
    """Re-parse ``request_shape_stages`` against a closed per-entry schema.

    Each output entry contains only: ``stage`` (one of ``REQUEST_SHAPE_STAGES``),
    the flat shape fields (``_SHAPE_FIELD_TYPES``), and the flat extra fields
    routes attach (``status_code``: int, ``body_bytes``: int,
    ``terminal_reason``: one of ``TERMINAL_REASONS``). Any other key, any
    nested mapping, any nested list, and any entry beyond
    ``MAX_ADMIN_STAGE_COUNT`` is discarded — independent of whatever the
    producer-side bounding in ``record_request_shape_stage`` already did, so
    a caller that bypasses that helper entirely still cannot smuggle
    anything past this boundary.
    """
    if not isinstance(stages, list):
        return []
    out: list[dict[str, Any]] = []
    for raw_entry in stages[:MAX_ADMIN_STAGE_COUNT]:
        if not isinstance(raw_entry, Mapping):
            continue
        entry: dict[str, Any] = {}
        stage_value = raw_entry.get("stage")
        if isinstance(stage_value, str) and stage_value in REQUEST_SHAPE_STAGES:
            entry["stage"] = stage_value
        entry.update(sanitize_request_shape_fields_for_admin(raw_entry))
        for key, expected in _STAGE_EXTRA_FIELD_TYPES.items():
            if key not in raw_entry:
                continue
            sanitized = _sanitize_typed_value(raw_entry[key], expected)
            if sanitized is not _REJECTED:
                entry[key] = sanitized
        terminal_reason = raw_entry.get("terminal_reason")
        if isinstance(terminal_reason, str) and terminal_reason in TERMINAL_REASONS:
            entry["terminal_reason"] = terminal_reason
        if entry:
            out.append(entry)
    return out
