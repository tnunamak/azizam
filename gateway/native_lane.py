"""The per-engine native lane's rules (contract section 6): what an engine record lets through.

An engine record sets ``native_passthrough: true`` to open the lane and lists what may pass in
``native.allow``. Each entry names a method, a path template (``{name}`` stands for one path segment)
and a class: ``generate`` is plan-scoped, ``control`` (load, settings, restart, file paths) is
owner-only and never implied by generation access. A call no entry names is refused. Request and
reply bytes are capped; ``native.max_request_bytes`` and ``native.max_response_bytes`` set the caps.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

GENERATE = "generate"
CONTROL = "control"
CLASSES = (GENERATE, CONTROL)
METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"})
DEFAULT_MAX_REQUEST_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_RESPONSE_BYTES = 256 * 1024 * 1024
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


@dataclass(frozen=True)
class AllowEntry:
    method: str
    path: str
    klass: str

    def matches(self, method: str, path: str) -> bool:
        return self.method == method.upper() and _template(self.path).fullmatch(path) is not None


@dataclass(frozen=True)
class NativePolicy:
    allow: tuple[AllowEntry, ...] = ()
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    def entry_for(self, method: str, path: str) -> AllowEntry | None:
        """The first entry that names this call. A control entry wins over a generate entry for the same call."""
        matching = [entry for entry in self.allow if entry.matches(method, path)]
        return next((entry for entry in matching if entry.klass == CONTROL), matching[0] if matching else None)


def _template(path: str) -> re.Pattern[str]:
    parts = re.split(r"(\{[A-Za-z_][A-Za-z0-9_]*\})", path)
    return re.compile("".join("[^/]+" if part.startswith("{") else re.escape(part) for part in parts))


def validate_native_block(block: Any) -> None:
    """Raise ValueError when an engine record's ``native`` block cannot be read as a policy."""
    if block is None:
        return
    if not isinstance(block, Mapping):
        raise ValueError("native must be a mapping")
    allow = block.get("allow", [])
    if not isinstance(allow, (list, tuple)):
        raise ValueError("native.allow must be a list")
    for entry in allow:
        if not isinstance(entry, Mapping):
            raise ValueError("each native.allow entry must be a mapping")
        if str(entry.get("method", "")).upper() not in METHODS:
            raise ValueError(f"native.allow method must be one of {sorted(METHODS)}")
        path = entry.get("path")
        if not isinstance(path, str) or not path.startswith("/") or "*" in path:
            raise ValueError("native.allow path must be an absolute path template without wildcards")
        if entry.get("class") not in CLASSES:
            raise ValueError(f"native.allow class must be one of {list(CLASSES)}")
    for name in ("max_request_bytes", "max_response_bytes"):
        value = block.get(name)
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError(f"native.{name} must be a positive integer")


def policy_for(record: Mapping[str, Any]) -> NativePolicy:
    """The policy an engine record declares. A record with no valid ``native`` block allows nothing."""
    block = record.get("native")
    try:
        validate_native_block(block)
    except ValueError:
        return NativePolicy()
    if not block:
        return NativePolicy()
    return NativePolicy(
        allow=tuple(
            AllowEntry(str(entry["method"]).upper(), entry["path"], entry["class"]) for entry in block.get("allow", ())
        ),
        max_request_bytes=block.get("max_request_bytes") or DEFAULT_MAX_REQUEST_BYTES,
        max_response_bytes=block.get("max_response_bytes") or DEFAULT_MAX_RESPONSE_BYTES,
    )


def reply_refusal(policy: NativePolicy, status_code: int, content_length: str | None, engine: str) -> tuple[int, str, str] | None:
    """(status, code, message) when the engine's reply head alone makes the gateway refuse it, else None."""
    if status_code in REDIRECT_STATUSES:
        return 502, "native_redirect_refused", (
            f"Engine {engine!r} answered with a redirect, which this lane never follows or relays."
        )
    if content_length and content_length.isdigit() and int(content_length) > policy.max_response_bytes:
        return 502, "native_response_too_large", (
            f"Engine {engine!r} answered with more than the {policy.max_response_bytes}-byte limit of this lane."
        )
    return None
