"""What a frontend could not carry: the loss report (contract section 5).

A frontend declares ``strictness``. ``exact`` refuses a request field its translator cannot carry.
``lenient`` drops the field and records it. A translator that only approximates a field records
that too. The report goes into the receipt and into the ``x-azizam-loss`` response header. An
``identity`` frontend carries every field, so its report is always empty.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

EXACT = "exact"
LENIENT = "lenient"
STRICTNESS = (EXACT, LENIENT)
LOSS_HEADER = "x-azizam-loss"

_MAX_ENTRIES = 16
_MAX_TEXT = 48


def _text(value: Any) -> str:
    return str(value)[:_MAX_TEXT]


@dataclass
class LossReport:
    dropped: list[str] = field(default_factory=list)
    approximated: list[tuple[str, str]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.dropped and not self.approximated

    def as_dict(self) -> dict[str, Any]:
        """Bounded and JSON-safe: field names come from the caller's request body."""
        dropped = [_text(name) for name in self.dropped[:_MAX_ENTRIES]]
        approximated = [{"field": _text(name), "note": _text(note)} for name, note in self.approximated[:_MAX_ENTRIES]]
        report: dict[str, Any] = {"dropped": dropped, "approximated": approximated}
        if len(self.dropped) > _MAX_ENTRIES or len(self.approximated) > _MAX_ENTRIES:
            report["truncated"] = True
        return report

    def header_value(self) -> str:
        return json.dumps(self.as_dict(), separators=(",", ":"), ensure_ascii=True)


_current: ContextVar[LossReport | None] = ContextVar("frontend_loss", default=None)


def current() -> LossReport | None:
    return _current.get()


@contextmanager
def tracking(report: LossReport) -> Iterator[LossReport]:
    """Make ``report`` the one translators and receipts opened inside this block write to."""
    token = _current.set(report)
    try:
        yield report
    finally:
        _current.reset(token)


def approximated(name: str, note: str) -> None:
    """A translator calls this when it carried ``name`` only approximately. No effect outside a tracked request."""
    report = _current.get()
    if report is not None:
        report.approximated.append((name, note))
