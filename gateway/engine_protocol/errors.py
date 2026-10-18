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
    """The engine answered with a non-success status. ``advice`` comes from the description's error rows."""

    code = "upstream_error"

    def __init__(self, status: int, message: str, *, body: bytes = b"", headers: dict[str, str] | None = None,
                 advice_code: str | None = None, advice: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.advice_code = advice_code
        self.advice = advice


class MalformedResponse(ProtocolError):
    """The engine answered 2xx but not in the shape its description promises (not JSON, no job id, an unknown status)."""

    code = "malformed_response"


class UnknownOperation(ProtocolError):
    code = "unknown_operation"


class UnsupportedByAdapter(ProtocolError):
    """The adapter does not implement this part of the interface (for example events, or jobs)."""

    code = "unsupported_by_adapter"
