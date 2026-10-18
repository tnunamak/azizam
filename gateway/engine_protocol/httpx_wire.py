"""The httpx boundary shared by the reviewed in-tree adapters.

A reviewed adapter that wraps an older client sends the very requests that
client sent. These helpers turn what httpx raises and returns into the typed
values of the adapter interface, and nothing else: no policy, no engine names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import httpx

from gateway.engine_protocol.errors import TransportFailure
from gateway.engine_protocol.types import Reply

UNSET: Any = object()


def lower_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if headers is None:
        return {}
    return {name.lower(): value for name, value in headers.items()}


def failure(error: Exception) -> TransportFailure:
    """The typed failure for an httpx error: ``timeout``, ``unreachable`` or ``aborted``, keeping the original text."""
    if isinstance(error, httpx.TimeoutException):
        return TransportFailure("timeout", str(error))
    if isinstance(error, httpx.ConnectError):
        return TransportFailure("unreachable", str(error))
    return TransportFailure("aborted", str(error))


def supplied(**options: Any) -> dict[str, Any]:
    """The keyword arguments the caller actually gave: an older client call passes only what it names."""
    return {name: value for name, value in options.items() if value is not UNSET}


@dataclass(frozen=True)
class WireReply(Reply):
    """A ``Reply`` that also answers ``json()`` and ``read_text()`` the way the older client's response did.

    Nothing is parsed or decoded until it is asked for, so an answer is read at the same call, with the same errors, as
    before: ``json()`` raises the parse error where it is called. ``body`` stays empty. ``text`` is the response text of
    a non-200 answer when the operation was built to report it; ``read_text()`` returns it, or reads the response's
    text (which honours the charset it declares) on demand.
    """

    text: str | None = None
    source: Any = field(default=None, repr=False, compare=False)

    def json(self) -> Any:
        return self.source.json()

    def read_text(self) -> str:
        return self.text if self.text is not None else self.source.text


def reply_from(response: Any, *, want_text: bool = False) -> WireReply:
    """The typed reply for a response.

    Reads only ``status_code``, ``headers`` and ``content`` now; ``json()`` and ``read_text()`` read the response when
    asked. With ``want_text`` the text of a non-200 answer is read now, as the older error paths did.
    """
    return WireReply(
        status=response.status_code,
        headers=lower_headers(getattr(response, "headers", None)),
        raw=getattr(response, "content", None),
        text=response.text if want_text and response.status_code != 200 else None,
        source=response,
    )
