"""Remove the secret values this process has handled from text that is about to be logged (decisions D39 and D42).

A server can echo a credential it was sent, in a reply body, a header or an error. The lines the drivers and probes log
from an upstream pass through ``scrub``, which removes every value registered here. The registry holds values only; it
reads no secret. Whatever resolves or reads one registers it: ``runtime_secrets`` for the references it resolves, and
each module that reads a provider or engine key or a key named by an environment variable (an architecture test lists
them). This module imports nothing from the gateway, so any module may register without reaching the resolver.

Bounds. A value is kept while its source exists: a source is a secret reference or ``env:NAME``. A source whose value
changes keeps its last ``_VALUES_PER_SOURCE`` values (an upstream may still echo the one before). The registry is bounded
three ways, and the least recently used source goes first, never the one being registered:

* ``_SOURCE_LIMIT`` sources (a store with more secrets in use than that is not a store this was written for);
* ``_TOTAL_BYTES`` of values in all;
* ``_MAX_VALUE`` characters of any one value. A value longer than that is registered by its first ``_MAX_VALUE``
  characters: those are removed from a line wherever they appear and the remainder is left to the line's own length cap
  (``scrub(limit=...)``), which every caller sets far below ``_MAX_VALUE``. No credential the gateway sends is that long.
"""

from __future__ import annotations

import collections
import os
import re
import threading
from typing import Any
from urllib.parse import quote, quote_plus

_RESOLVED: "collections.OrderedDict[str, tuple[str, ...]]" = collections.OrderedDict()
_LOCK = threading.Lock()
_SOURCE_LIMIT = 65536
_VALUES_PER_SOURCE = 3
_MAX_VALUE = 8192
_TOTAL_BYTES = 4 * 1024 * 1024
_held_bytes = 0


def _size(values: tuple[str, ...]) -> int:
    return sum(len(value) for value in values)


def remember(value: Any, source: str | None = None) -> None:
    """Register a secret value for ``scrub``. ``source`` names where it came from (default: the value itself)."""
    global _held_bytes
    if not isinstance(value, str) or not value:
        return
    value = value[:_MAX_VALUE]
    key = source or "value:" + value[:64] + str(len(value))
    with _LOCK:
        before = _RESOLVED.get(key, ())
        held = before
        if value not in held:
            held = (value, *held)[:_VALUES_PER_SOURCE]
        _RESOLVED[key] = held
        _held_bytes += _size(held) - _size(before)
        _RESOLVED.move_to_end(key)
        while len(_RESOLVED) > 1 and (len(_RESOLVED) > _SOURCE_LIMIT or _held_bytes > _TOTAL_BYTES):
            _, dropped = _RESOLVED.popitem(last=False)
            _held_bytes -= _size(dropped)


def clear() -> None:
    """Forget every value (a test)."""
    global _held_bytes
    with _LOCK:
        _RESOLVED.clear()
        _held_bytes = 0


def env_secret(name: str, default: str = "") -> str:
    """The value of an environment variable that holds a credential (empty is unset), registered for ``scrub``."""
    value = os.environ.get(name) or default
    remember(value, f"env:{name}")
    return value


def scrub(text: Any, *, limit: int = 300) -> str:
    """``text`` for a log line: every registered value is removed (as registered and URL-encoded), and the result is cut
    to ``limit`` characters.

    A value of four characters or more is removed wherever it appears. A shorter one is removed only where it stands alone
    (between characters that are not letters or digits): anywhere else it would take letters out of every line.
    """
    out = str(text)
    with _LOCK:
        values = sorted({value for held in _RESOLVED.values() for value in held}, key=len, reverse=True)
    for value in values:
        for form in dict.fromkeys((value, quote(value, safe=""), quote_plus(value), quote(value))):
            if not form:
                continue
            if len(form) >= 4:
                out = out.replace(form, "[removed]")
            else:
                out = re.sub(rf"(?<![A-Za-z0-9]){re.escape(form)}(?![A-Za-z0-9])", "[removed]", out)
    return out if len(out) <= limit else out[:limit] + "...[cut]"
