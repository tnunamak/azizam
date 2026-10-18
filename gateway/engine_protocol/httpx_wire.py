"""The httpx boundary shared by the reviewed in-tree adapters.

A reviewed adapter that wraps an older client sends the very requests that
client sent. These helpers turn what httpx raises and returns into the typed
values of the adapter interface, and nothing else: no policy, no engine names.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    """A ``Reply`` that also answers ``json()`` and ``text`` the way the older client's response did.

    ``body`` holds the parsed JSON when the operation expects JSON. A body that does not parse leaves ``body`` empty and
    ``json()`` raises the parse error at the call, as the response object did.
    """

    text: str | None = None
    json_error: Exception | None = None

    def json(self) -> Any:
        if self.json_error is not None:
            raise self.json_error
        return self.body


def reply_from(response: Any, *, parse_json: bool = True, want_text: bool = False) -> WireReply:
    """The typed reply for a response.

    Reads only ``status_code``, ``headers``, ``content`` and, on request, ``json()`` and (for a non-200 answer) ``text``.
    """
    body: Any = None
    json_error: Exception | None = None
    if parse_json:
        try:
            body = response.json()
        except Exception as error:  # noqa: BLE001 - the same error is raised again, at the call that asks for it
            json_error = error
    return WireReply(
        status=response.status_code,
        headers=lower_headers(getattr(response, "headers", None)),
        body=body,
        raw=getattr(response, "content", None),
        text=response.text if want_text and response.status_code != 200 else None,
        json_error=json_error,
    )
