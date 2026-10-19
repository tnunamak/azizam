"""Typed failures of the engine protocol layer.

Every failure carries a stable ``code`` (for receipts and advice rows) and a
sentence for the owner. None of them holds a credential: the executor scrubs
secrets before a message is built.
"""

from __future__ import annotations


class ProtocolError(Exception):
    """Base of every failure here. ``code`` is stable; the message is for people."""

    code = "protocol_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class DescriptionError(ProtocolError):
    """A protocol description cannot be used. The message lists every problem, one per line."""

    code = "invalid_description"

    def __init__(self, source: str, problems: list[str]) -> None:
        self.source = source
        self.problems = list(problems)
        head = f"{source}: the protocol description is not valid ({len(problems)} problem{'s' if len(problems) != 1 else ''})"
        super().__init__(head + ":\n  " + "\n  ".join(problems))


class MappingError(ProtocolError):
    """A mapping rule met a value it cannot map (a required source missing, an unmapped enum value)."""

    code = "mapping_failed"


class BudgetExceeded(ProtocolError):
    """A size, depth, count or time budget ran out. ``budget`` names which."""

    code = "budget_exceeded"

    def __init__(self, budget: str, message: str) -> None:
        super().__init__(message)
        self.budget = budget


class RedirectRefused(ProtocolError):
    """The engine answered with a redirect to another origin (or too many redirects)."""

    code = "redirect_refused"


class TransportFailure(ProtocolError):
    """No HTTP answer arrived. ``kind``: timeout, unreachable, blocked, tls, dns, aborted."""

    code = "transport_failed"

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class UpstreamError(ProtocolError):
    """The engine answered with a non-success status. ``advice`` comes from the description's error rows.

    ``message`` is the gateway's own sentence ("The engine answered 502."). What the engine said is kept apart, in
    ``excerpt`` and ``body``, and no ``str()``, ``repr()`` or log line of this error shows either (decision D44): the text
    is the engine's, and an engine echoes what it was sent. A message that contains the start of the body is refused here,
    so a caller cannot put the text back by building the message from it.
    """

    code = "upstream_error"

    def __init__(self, status: int, message: str, *, body: bytes = b"", headers: dict[str, str] | None = None,
                 advice_code: str | None = None, advice: str | None = None, excerpt: str = "") -> None:
        for said in (excerpt, body.decode("utf-8", "replace") if body else ""):
            said = " ".join(said.split())[:24]
            if len(said) >= 8 and said in " ".join(message.split()):
                raise ValueError("an upstream error message must not hold the upstream's text; pass it as excerpt")
        super().__init__(message)
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.advice_code = advice_code
        self.advice = advice
        self.excerpt = excerpt

    def __repr__(self) -> str:
        return f"{type(self).__name__}(status={self.status}, code={self.code!r})"


class MalformedResponse(ProtocolError):
    """The engine answered 2xx but not in the shape its description promises (not JSON, no job id, an unknown status)."""

    code = "malformed_response"


class UnknownOperation(ProtocolError):
    code = "unknown_operation"


class UnknownProtocol(UnknownOperation):
    code = "unknown_protocol"


class UnsupportedByAdapter(ProtocolError):
    """The adapter does not implement this part of the interface (for example events, or jobs)."""

    code = "unsupported_by_adapter"


def retryable_poll_failure(error: ProtocolError) -> bool:
    """Only transient transport failures and server HTTP failures may retry a poll."""
    if isinstance(error, TransportFailure):
        return error.kind not in {"dns", "tls", "blocked"}
    return isinstance(error, UpstreamError) and 500 <= error.status < 600
