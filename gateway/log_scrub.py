"""Keep credentials out of log lines (decisions D39, D42, D43 and D44).

**The rule (D43): the gateway never logs an upstream response body or an upstream error text.** A log line about an
upstream holds a status, a content type from a fixed list, a byte count and a fixed reason of our own (``describe_response``,
``describe_error``, ``describe_output``); never the text of a reply, a reason phrase or a header, never ``str()`` of an HTTP
error (it embeds the URL, and the URL can carry a credential), never what a subprocess wrote.

**The boundary (D44): deny-default at the one place every sink shares** (``gateway/log_egress.py``): every log record is
cut down to its formatted, scrubbed message and the type and location of its exception, whichever logger made it and
whichever handler takes it; ``print``, ``sys.stderr`` and warnings go through the same path. The scrubber below is the
secondary guard that path calls, for the lines that are not about an upstream and might still carry a value.

It removes every secret the process has registered, in each form the gateway can produce (raw, URL-encoded, JSON-escaped,
base64, and base64 of ``user:password`` for Basic auth), before it cuts the line to length, never after.

Registry. A value is registered under the *source* it came from, a name that is stable in the configuration: a stored
secret (its id; a ``ref:`` prefix is the same source), ``env:NAME``, or a fixed configuration name. Never a hash of the
value: sources are bounded by configuration, so memory does not grow with rotation. ``remember`` says what a source holds
now (the value it replaces is kept for the last ``_RETIRED_PER_SOURCE`` rotations, since an upstream may still echo it);
``add`` registers one of several concurrent values of a source; ``hold`` sets all of them at once; none of them evicts a
value in use. A secret that is deleted is forgotten (``forget``), after its deletion has committed. A value longer than
``_MAX_VALUE`` is not registered; while a source holds one, ``scrub`` withholds every line, because the gateway cannot
promise to remove a credential it did not keep. This module imports nothing from the gateway, so any module may register
without reaching the resolver.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
from collections import deque
from typing import Any, Iterable, Mapping
from urllib.parse import quote, quote_plus, urlsplit

_LOCK = threading.RLock()
_RETIRED_PER_SOURCE = 8
_MAX_VALUE = 64 * 1024
WITHHELD = "[line withheld: it may carry a credential that is too long to remove]"


class _Source:
    """What one configured source holds: its current values, the few it held before, and the too-long ones it holds now."""

    __slots__ = ("current", "retired", "oversize", "retired_oversize")

    def __init__(self) -> None:
        self.current: dict[str, tuple[str, ...]] = {}   # value -> the forms it can take in a line
        self.retired: "deque[tuple[str, tuple[str, ...]]]" = deque(maxlen=_RETIRED_PER_SOURCE)
        self.oversize: set[str] = set()                 # digests of values held but too long to keep
        self.retired_oversize: "deque[str]" = deque(maxlen=_RETIRED_PER_SOURCE)  # the same, for values it held before


_SOURCES: "dict[str, _Source]" = {}
_SNAPSHOT: "tuple[tuple[str, ...], tuple[str, ...], bool] | None" = None  # (long forms, short forms, withhold); None = stale


def _canonical(source: Any) -> str:
    """The registry key of a source. A stored secret is its id: ``ref:<id>`` and ``<id>`` are one source."""
    if not isinstance(source, str) or not source:
        raise ValueError("a registered value names its source")
    return source[4:] if source.startswith("ref:") and len(source) > 4 else source


def secret_source(secret_id: Any) -> str:
    """The source of a stored secret (the one name its registrations and its deletion share)."""
    return _canonical(str(secret_id))


def _hold(source: str, values: Iterable[str], *, keep_current: bool) -> bool:
    """Set (``keep_current`` False) or extend (True) what ``source`` holds. False when a value was too long to keep."""
    global _SNAPSHOT
    ok = True
    wanted: dict[str, tuple[str, ...]] = {}
    oversize: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        if len(value) > _MAX_VALUE:
            oversize.add(hashlib.sha256(value.encode("utf-8", "replace")).hexdigest())
            ok = False
        else:
            wanted[value] = ()
    with _LOCK:
        held = _SOURCES.get(source)
        if held is None:
            if not wanted and not oversize:
                return ok
            held = _SOURCES[source] = _Source()
        if keep_current:
            fresh = {value: _forms(value) for value in wanted if value not in held.current}
            if not fresh and oversize <= held.oversize:
                return ok
            held.current.update(fresh)
            held.oversize |= oversize
        else:
            if tuple(held.current) == tuple(wanted) and oversize == held.oversize:
                return ok
            previous = held.current
            held.current = {value: previous.get(value) or _forms(value) for value in wanted}
            retired = [item for item in held.retired if item[0] not in held.current]
            retired += [(value, forms) for value, forms in previous.items()
                        if value not in held.current and all(value != old for old, _ in retired)]
            held.retired = deque(retired[-_RETIRED_PER_SOURCE:], maxlen=_RETIRED_PER_SOURCE)
            gone = [digest for digest in held.oversize if digest not in oversize and digest not in held.retired_oversize]
            held.retired_oversize.extend(gone)
            held.retired_oversize = deque((d for d in held.retired_oversize if d not in oversize), maxlen=_RETIRED_PER_SOURCE)
            held.oversize = oversize
        _SNAPSHOT = None
    return ok


def remember(value: Any, source: str) -> bool:
    """Register ``value`` as what ``source`` holds now, for ``scrub``. False when it is too long to register.

    The value it replaces is kept for the last few rotations, so a repeated call with a new value never grows the registry.
    A source with several concurrent values uses ``add`` or ``hold``.
    """
    source = _canonical(source)
    if not isinstance(value, str) or not value:
        return True
    return _hold(source, [value], keep_current=False)


def add(value: Any, source: str) -> bool:
    """Register ``value`` as one of several concurrent values of ``source`` (nothing is evicted). False when too long."""
    source = _canonical(source)
    if not isinstance(value, str) or not value:
        return True
    return _hold(source, [value], keep_current=True)


def hold(source: str, values: Iterable[Any]) -> bool:
    """Set every value ``source`` holds now (a rotation or a restore). The ones it held before are kept for a few rotations."""
    return _hold(_canonical(source), [value for value in values if isinstance(value, str)], keep_current=False)


def forget(source: str) -> None:
    """Drop what a source held (its secret was deleted), and with it a withhold its too-long value caused."""
    global _SNAPSHOT
    with _LOCK:
        if _SOURCES.pop(_canonical(source), None) is not None:
            _SNAPSHOT = None


def clear() -> None:
    """Forget every value (a test)."""
    global _SNAPSHOT
    with _LOCK:
        _SOURCES.clear()
        _SNAPSHOT = None


def sources() -> "frozenset[str]":
    """The names of the sources that hold something (bounded by the configuration)."""
    with _LOCK:
        return frozenset(_SOURCES)


def env_secret(name: str, default: str = "") -> str:
    """The value of an environment variable that holds a credential (empty is unset), registered for ``scrub``."""
    value = os.environ.get(name) or default
    remember(value, f"env:{name}")
    return value


def _forms(value: str) -> tuple[str, ...]:
    """Every form of ``value`` the gateway can put in a line, longest first."""
    forms = {value, quote(value, safe=""), quote(value), quote_plus(value),
             json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]}
    raw = value.encode("utf-8")
    for encoder in (base64.b64encode, base64.urlsafe_b64encode):
        text = encoder(raw).decode("ascii")
        forms.update({text, text.rstrip("=")})  # a Basic header carries ``user:password`` as exactly this
    return tuple(sorted((form for form in forms if form), key=len, reverse=True))


def _snapshot() -> "tuple[tuple[str, ...], tuple[str, ...], bool]":
    """Every registered form, longest first, split at four characters; and whether a too-long value is held."""
    global _SNAPSHOT
    with _LOCK:
        if _SNAPSHOT is None:
            forms: set[str] = set()
            withhold = False
            for held in _SOURCES.values():
                withhold = withhold or bool(held.oversize) or bool(held.retired_oversize)
                for value_forms in (*held.current.values(), *(forms_ for _, forms_ in held.retired)):
                    forms.update(value_forms)
            ordered = sorted(forms, key=len, reverse=True)
            _SNAPSHOT = (tuple(form for form in ordered if len(form) >= 4), tuple(form for form in ordered if len(form) < 4), withhold)
        return _SNAPSHOT


def scrub(text: Any, *, limit: int = 300) -> str:
    """``text`` for a log line: every registered value is removed in each form, and only then is the line cut to ``limit``.

    A form of four characters or more is removed wherever it appears. A shorter one is removed only where it stands
    alone (between characters that are not letters or digits): anywhere else it would take letters out of every line.
    While a source holds a value too long to register every line is withheld.
    """
    long_forms, short_forms, withhold = _snapshot()
    if withhold:
        return WITHHELD
    out = text if isinstance(text, str) else str(text)
    for form in long_forms:
        if form in out:
            out = out.replace(form, "[removed]")
    for form in short_forms:
        out = re.sub(rf"(?<![A-Za-z0-9]){re.escape(form)}(?![A-Za-z0-9])", "[removed]", out)
    return out if len(out) <= limit else out[:limit] + "...[cut]"


# -- what a log line may say about an upstream (decision D43) -------------------------------------------------------------
# These are the allowlisted formatters of ``tests/test_no_upstream_text_in_logs.py``. Each keeps a status, a content type, a
# byte count or a class name, and never a piece of what an upstream sent.


# The media types a log line may name. An upstream chooses its own Content-Type text, so any other value is "other": a
# header an upstream wrote is upstream text.
MEDIA_TYPES = frozenset({
    "application/json", "application/x-ndjson", "application/octet-stream", "application/zip", "application/pdf",
    "application/x-www-form-urlencoded", "application/xml", "application/javascript",
    "text/plain", "text/html", "text/event-stream", "text/csv", "text/xml",
    "multipart/form-data", "multipart/mixed",
    "image/png", "image/jpeg", "image/webp", "image/gif",
    "audio/mpeg", "audio/wav", "audio/x-wav", "audio/ogg", "audio/flac", "audio/mp4", "audio/webm",
    "video/mp4", "video/webm",
})


def describe_media_type(headers: Any) -> str:
    """The media type of a reply when it is one of ``MEDIA_TYPES`` (parameters dropped), ``other`` when it is not, and
    ``no content type`` when there is none. Never the header text."""
    try:
        raw = str((headers or {}).get("content-type", "")).split(";")[0].strip().lower()
    except Exception:  # noqa: BLE001 - headers that cannot be read name nothing
        return "other"
    return "no content type" if not raw else raw if raw in MEDIA_TYPES else "other"


def describe_response(response: Any, reason: str = "") -> str:
    """``status 502, application/json, 123 bytes`` for an HTTP response; ``reason`` is the caller's own fixed text.

    The status is a number, the media type is named only from ``MEDIA_TYPES``, the size is a count. It carries no reason
    phrase and no header text: those are what the upstream chose to say.
    """
    headers = getattr(response, "headers", None) or {}
    ctype = describe_media_type(headers)
    try:
        size = len(response.content)
    except Exception:  # noqa: BLE001 - a streamed body is not read here
        length = headers.get("content-length")
        size = int(length) if isinstance(length, str) and length.isdigit() else None
    status = getattr(response, "status_code", None)
    text = f"status {status if isinstance(status, int) else '?'}, {ctype}, {'an unread body' if size is None else f'{size} bytes'}"
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
