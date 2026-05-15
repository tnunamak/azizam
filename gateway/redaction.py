"""Shared redaction guard for receipt and media-job durable stores.

The gateway persists two kinds of diagnostic records that must never carry
prompts, image/audio/video bytes, raw request or response bodies, secrets, or
credentials:

- ``gateway.receipt_store`` — route-decision receipts.
- ``gateway.media_jobs`` — durable media-job records and event log.

Both surfaces used to maintain their own copy of the forbidden-key set. As new
modalities and adapters land, those sets drifted: media_jobs grew video/audio
keys, receipt_store missed them. Local extensions adding new persistence are
likely to drift further.

This module owns the single source of truth: one forbidden-key vocabulary plus
a small pure API for asserting a mapping is safe to persist.

Design notes
------------

- Pure: no I/O, no state. Easy to call from anywhere, easy to test.
- Key-only redaction. Values are never inspected and never surface in error
  messages — only key names do. Callers can safely log the raised exception
  without re-leaking the very payload we just refused.
- One level of nesting. The contract is "metadata bag of small breadcrumbs",
  not "arbitrary JSON tree". A single-level recursion catches the common
  ``metadata.payload = {"prompt": ...}`` mistake without becoming a content
  scanner. Deeper trees should be flattened by the caller.
- Audience filtering lives with each store. This module only answers the
  question "is this shape safe to persist?".
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

# Forbidden key vocabulary, in three groups so it stays readable:
#   - raw inputs/payloads (prompts, messages, bodies)
#   - media bytes/blobs across modalities
#   - secrets and credentials
#
# The set is lowercased; comparison is case-insensitive.
_RAW_PAYLOAD_KEYS = frozenset(
    {
        "prompt",
        "prompts",
        "input",
        "inputs",
        "messages",
        "body",
        "raw_body",
        "raw_request",
        "raw_response",
        "request_body",
        "response_body",
    }
)

_MEDIA_BYTE_KEYS = frozenset(
    {
        "image",
        "images",
        "image_b64",
        "image_bytes",
        "image_data",
        "video",
        "video_bytes",
        "audio",
        "audio_bytes",
        "media",
        "media_bytes",
        "bytes",
        "data_b64",
    }
)

_SECRET_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "api_token",
        "secret",
        "token",
        "password",
        "credentials",
        "cookie",
        "cookies",
    }
)

FORBIDDEN_KEYS: frozenset[str] = _RAW_PAYLOAD_KEYS | _MEDIA_BYTE_KEYS | _SECRET_KEYS

# Human-readable suffix appended to rejection errors. Mentions categories, not
# specific values — never echo a rejected payload back to a caller.
_REJECTION_REASON = (
    "(persisted diagnostics must not carry raw prompts, media bytes, "
    "request/response bodies, or secrets)"
)


def is_forbidden_key(key: Any) -> bool:
    """Return True if ``key`` (case-insensitive) is on the forbidden list."""
    try:
        return str(key).lower() in FORBIDDEN_KEYS
    except Exception:  # noqa: BLE001
        return False


def find_forbidden_keys(
    data: Mapping[str, Any] | None,
    *,
    recurse: bool = True,
) -> list[str]:
    """Return a sorted, deduplicated list of forbidden keys present in ``data``.

    When ``recurse`` is true, looks one level into nested mappings as well.
    Returns ``[]`` when ``data`` is None, empty, or not a mapping.
    """
    if not isinstance(data, Mapping):
        return []
    found: set[str] = set()
    for k, v in data.items():
        if is_forbidden_key(k):
            found.add(str(k).lower())
        if recurse and isinstance(v, Mapping):
            for nk in v.keys():
                if is_forbidden_key(nk):
                    found.add(str(nk).lower())
    return sorted(found)


def assert_safe_mapping(
    label: str,
    data: Any,
    *,
    recurse: bool = True,
) -> None:
    """Raise ``ValueError`` if ``data`` is not a mapping or contains forbidden keys.

    ``label`` identifies the offending field (e.g. ``"MediaJob.metadata"``) for
    the error message. Values are never included in the message — only key
    names — so the exception itself is safe to log.
    """
    if not isinstance(data, Mapping):
        raise ValueError(f"{label} must be a mapping")
    bad = find_forbidden_keys(data, recurse=False)
    if bad:
        raise ValueError(f"{label} contains forbidden keys: {bad} {_REJECTION_REASON}")
    if recurse:
        for v in data.values():
            if isinstance(v, Mapping):
                nested = find_forbidden_keys(v, recurse=False)
                if nested:
                    raise ValueError(
                        f"{label} contains forbidden nested keys: {nested} {_REJECTION_REASON}"
                    )


def assert_safe_top_level(
    label: str,
    data: Mapping[str, Any],
    *,
    ignore: Iterable[str] = (),
) -> None:
    """Raise ``ValueError`` if any top-level key in ``data`` is forbidden.

    Used by stores that persist a record whose own top-level keys could
    accidentally collide with the forbidden vocabulary (e.g. a receipt with a
    stray ``"prompt"`` field added by a sloppy extension).

    ``ignore`` lets callers exempt known-safe top-level keys that happen to
    share a name with a forbidden key family (none today, but kept for
    forward compatibility with adapters that might use e.g. ``"input"`` as a
    structural field name).
    """
    if not isinstance(data, Mapping):
        raise ValueError(f"{label} must be a mapping")
    ignored = {str(k).lower() for k in ignore}
    bad = sorted(
        str(k).lower()
        for k in data.keys()
        if is_forbidden_key(k) and str(k).lower() not in ignored
    )
    if bad:
        raise ValueError(
            f"{label} contains forbidden top-level keys: {bad} {_REJECTION_REASON}"
        )


__all__ = [
    "FORBIDDEN_KEYS",
    "is_forbidden_key",
    "find_forbidden_keys",
    "assert_safe_mapping",
    "assert_safe_top_level",
]
