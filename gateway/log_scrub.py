"""Keep credentials out of log lines (decisions D39, D42 and D43).

**The rule (D43): the gateway never logs an upstream response body or an upstream error text.** A log line about an
upstream holds a status, a content type, a byte count and a fixed reason of our own (``describe_response``,
``describe_error``, ``describe_output``); never the text of a reply, never ``str()`` of an HTTP error (it embeds the URL,
and the URL can carry a credential), never what a subprocess wrote. ``tests/test_no_upstream_text_in_logs.py`` finds a log
call that takes such a value and fails for it. That is what keeps a credential out of the log.

The scrubber below is the backup, for the lines that are not about an upstream and might still carry a value. It removes
every secret the process has registered, in each form the gateway can produce (raw, URL-encoded, JSON-escaped, base64, and
base64 of ``user:password`` for Basic auth), before it cuts the line to length, never after.

Registry. A value is registered under the *source* it came from: a secret reference, or ``env:NAME``, or a fixed
configuration name. Sources are bounded by configuration (the secrets stored and the variables named), so nothing is
evicted: a value in use is never forgotten. A source keeps its last ``_VALUES_PER_SOURCE`` values (an upstream may still
echo the one before a rotation). A secret that is deleted is forgotten (``forget``). A value longer than ``_MAX_VALUE`` is
not registered; its source is then *unloggable*: ``scrub`` withholds every line, because the gateway cannot promise to
remove a credential it did not keep. This module imports nothing from the gateway, so any module may register without
reaching the resolver.
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
from typing import Any, Mapping
from urllib.parse import quote, quote_plus, urlsplit

_RESOLVED: "dict[str, tuple[str, ...]]" = {}
_UNLOGGABLE: set[str] = set()
_LOCK = threading.Lock()
_VALUES_PER_SOURCE = 3
_MAX_VALUE = 64 * 1024
WITHHELD = "[line withheld: it may carry a credential that is too long to remove]"


def remember(value: Any, source: str) -> bool:
    """Register a secret value for ``scrub`` under the source it came from. False when it is too long to register."""
    if not isinstance(value, str) or not value:
        return True
    if not isinstance(source, str) or not source:
        raise ValueError("a registered value names its source")
    with _LOCK:
        if len(value) > _MAX_VALUE:
            _UNLOGGABLE.add(source)
            return False
        _UNLOGGABLE.discard(source)
        held = _RESOLVED.get(source, ())
        if value not in held:
            _RESOLVED[source] = (value, *held)[:_VALUES_PER_SOURCE]
    return True


def forget(source: str) -> None:
    """Drop what a source held (its secret was deleted)."""
    with _LOCK:
        _RESOLVED.pop(source, None)
        _UNLOGGABLE.discard(source)


def clear() -> None:
    """Forget every value (a test)."""
    with _LOCK:
        _RESOLVED.clear()
        _UNLOGGABLE.clear()


def env_secret(name: str, default: str = "") -> str:
    """The value of an environment variable that holds a credential (empty is unset), registered for ``scrub``."""
    value = os.environ.get(name) or default
    remember(value, f"env:{name}")
    return value


def _forms(value: str) -> list[str]:
    """Every form of ``value`` the gateway can put in a line, longest first."""
    forms = {value, quote(value, safe=""), quote(value), quote_plus(value),
             json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]}
    raw = value.encode("utf-8")
    for encoder in (base64.b64encode, base64.urlsafe_b64encode):
        text = encoder(raw).decode("ascii")
        forms.update({text, text.rstrip("=")})  # a Basic header carries ``user:password`` as exactly this
    return sorted((form for form in forms if form), key=len, reverse=True)


def scrub(text: Any, *, limit: int = 300) -> str:
    """``text`` for a log line: every registered value is removed in each form, and only then is the line cut to ``limit``.

    A form of four characters or more is removed wherever it appears. A shorter one is removed only where it stands
    alone (between characters that are not letters or digits): anywhere else it would take letters out of every line.
    While a source is unloggable (it held a value too long to register) every line is withheld.
    """
    with _LOCK:
        values = sorted({value for held in _RESOLVED.values() for value in held}, key=len, reverse=True)
        unloggable = bool(_UNLOGGABLE)
    if unloggable:
        return WITHHELD
    out = str(text)
    for value in values:
        for form in _forms(value):
            if len(form) >= 4:
                out = out.replace(form, "[removed]")
            else:
                out = re.sub(rf"(?<![A-Za-z0-9]){re.escape(form)}(?![A-Za-z0-9])", "[removed]", out)
    return out if len(out) <= limit else out[:limit] + "...[cut]"


# -- what a log line may say about an upstream (decision D43) -------------------------------------------------------------
# These are the allowlisted formatters of ``tests/test_no_upstream_text_in_logs.py``. Each keeps a status, a content type, a
# byte count or a class name, and never a piece of what an upstream sent.


def describe_response(response: Any, reason: str = "") -> str:
    """``status 502, application/json, 123 bytes`` for an HTTP response; ``reason`` is the caller's own fixed text."""
    headers = getattr(response, "headers", None) or {}
    ctype = str(headers.get("content-type", "")).split(";")[0].strip()[:64] or "no content type"
    try:
        size = len(response.content)
    except Exception:  # noqa: BLE001 - a streamed body is not read here
        length = headers.get("content-length")
        size = int(length) if isinstance(length, str) and length.isdigit() else None
    text = f"status {getattr(response, 'status_code', '?')}, {ctype}, {'an unread body' if size is None else f'{size} bytes'}"
    return f"{reason}: {text}" if reason else text


def describe_bytes(data: Any, reason: str = "") -> str:
    """``N bytes withheld`` for text or bytes an upstream or a subprocess produced."""
    size = len(data.encode("utf-8", "replace")) if isinstance(data, str) else len(data) if data is not None else 0
    text = f"{size} bytes withheld"
    return f"{reason}: {text}" if reason else text


def describe_error(exc: BaseException) -> str:
    """The class of an error and, for an HTTP error, the origin it was sent to (never the path, the query or the text)."""
    name = type(exc).__name__
    try:
        request = getattr(exc, "request", None)
        url = getattr(request, "url", None)
        parts = urlsplit(str(url)) if url is not None else None
        if parts is not None and parts.hostname:
            port = f":{parts.port}" if parts.port else ""
            return f"{name} ({parts.scheme}://{parts.hostname}{port})"
    except Exception:  # noqa: BLE001 - an error that cannot say where it went says only what it is
        pass
    return name


def describe_origin(url: Any) -> str:
    """``scheme://host:port`` of an address, for a line that must say where without saying what (no user info, path or query)."""
    try:
        parts = urlsplit(str(url))
        if parts.hostname:
            return f"{parts.scheme}://{parts.hostname}{f':{parts.port}' if parts.port else ''}"
    except ValueError:
        pass
    return "an address"
