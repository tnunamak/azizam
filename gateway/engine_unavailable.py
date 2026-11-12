"""Telling "the engine could not be reached" apart from every other upstream failure.

A job that fails because no engine answered gets the code ``engine_unavailable`` (category ``upstream``), and carries
``last_healthy_at`` when the gateway's own engine health record (``gateway.availability`` evidence) knows when the engine
last answered. This module only classifies a failure; it adds no probe.

What the code can tell apart:

* **Refused or unreachable** (``TransportFailure`` kind ``unreachable``, or the runner's ``upstream_unavailable``): nothing
  accepted the connection. The engine is down or restarting, so asking again later can work: retryable.
* **A proxy's 502, 503 or 504** (``UpstreamError`` with that status and a body that is empty, the standard phrase, an
  HTML page or a proxy's own words): the proxy in front of the engine could not reach it, before the engine answered. The
  same restart case: retryable.
* **A name that does not resolve** (kind ``dns``): the address is wrong. Asking again will not help: not retryable.

A connect timeout is not told apart from a read timeout by the failure the engine layer raises, so a timeout stays a
``timeout`` failure. An engine's own 5xx (a 500 from its code) stays an ordinary upstream error.
"""
from __future__ import annotations

from dataclasses import dataclass

from gateway.engine_protocol.errors import TransportFailure, UpstreamError

CODE = "engine_unavailable"
CATEGORY = "upstream"

# What the runner calls a connection that no engine accepted.
_RUNNER_UNREACHABLE_CODE = "upstream_unavailable"
_PROXY_PHRASES = {502: "bad gateway", 503: "service unavailable", 504: "gateway timeout"}
_PROXY_WORDS = ("bad gateway", "service unavailable", "gateway time-out", "gateway timeout", "no available server",
                "upstream connect error", "connection refused")
_BODY_LOOK = 2048


@dataclass(frozen=True)
class Verdict:
    retryable: bool
    reason: str     # "refused", "dns" or "proxy"


def _text(error: UpstreamError) -> str:
    raw = error.body[:_BODY_LOOK].decode("utf-8", "replace") if error.body else ""
    return " ".join((raw or error.excerpt or "").split()).lower()


def _proxy_before_engine(error: UpstreamError) -> bool:
    """A 502, 503 or 504 whose body is a proxy's: empty, an HTML error page, or a standard gateway phrase."""
    if error.status not in _PROXY_PHRASES:
        return False
    text = _text(error)
    return not text or text.startswith("<") or any(word in text for word in _PROXY_WORDS)


def classify(error: BaseException | None) -> Verdict | None:
    """The verdict for a failure, or ``None`` when it is not "the engine could not be reached"."""
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if getattr(error, "code", None) == _RUNNER_UNREACHABLE_CODE and getattr(error, "upstream_status", None) == 502:
            return Verdict(True, "refused")   # a 503 with this code is an engine that answered "busy": not this case
        if isinstance(error, TransportFailure):
            if error.kind == "unreachable":
                return Verdict(True, "refused")
            if error.kind == "dns":
                return Verdict(False, "dns")
        if isinstance(error, UpstreamError) and _proxy_before_engine(error):
            return Verdict(True, "proxy")
        error = error.__cause__ or error.__context__
    return None
