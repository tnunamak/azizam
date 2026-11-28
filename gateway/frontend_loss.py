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
from typing import Any, Iterable, Iterator, Mapping

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
    # Why the request could not be checked at all (a multipart body, or a body that is not a JSON object).
    unchecked: str | None = None
    # Why an exact frontend refused the whole body: it is not a JSON object.
    invalid: str | None = None

    @property
    def empty(self) -> bool:
        return not self.dropped and not self.approximated and not self.unchecked and not self.invalid

    def as_dict(self) -> dict[str, Any]:
        """Bounded and JSON-safe: field names come from the caller's request body."""
        dropped = [_text(name) for name in self.dropped[:_MAX_ENTRIES]]
        approximated = [{"field": _text(name), "note": _text(note)} for name, note in self.approximated[:_MAX_ENTRIES]]
        report: dict[str, Any] = {"dropped": dropped, "approximated": approximated}
        if self.unchecked:
            report["unchecked"] = _text(self.unchecked)
        if self.invalid:
            report["invalid"] = _text(self.invalid)
        if len(self.dropped) > _MAX_ENTRIES or len(self.approximated) > _MAX_ENTRIES:
            report["truncated"] = True
        return report

    def header_value(self) -> str:
        return json.dumps(self.as_dict(), separators=(",", ":"), ensure_ascii=True)


class _Node:
    __slots__ = ("children", "whole")

    def __init__(self) -> None:
        self.children: dict[str, _Node] = {}
        self.whole = False


_LIST = "[]"


class Carries:
    """The request fields a row's translator handles, as paths.

    ``name`` carries everything under ``name``. ``a.b`` carries only ``b`` inside the object ``a``, and
    ``items[].role`` carries only ``role`` inside each object of the list ``items``: a translator that
    flattens a container lists the parts it reads. ``split`` keeps what is carried and names the rest.
    """

    def __init__(self, paths: Iterable[str]) -> None:
        self._root = _Node()
        for path in paths:
            node = self._root
            for part in _parts(path):
                node = node.children.setdefault(part, _Node())
            node.whole = True

    def split(self, payload: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """(the payload without what is not carried, the paths of what was left out)."""
        lost: list[str] = []
        kept = self._walk(payload, self._root, "", lost)
        return kept, list(dict.fromkeys(lost))

    def _walk(self, value: Any, node: _Node, path: str, lost: list[str]) -> Any:
        if node.whole:
            return value
        if isinstance(value, Mapping):
            kept: dict[str, Any] = {}
            for key, item in value.items():
                child = node.children.get(key)
                if child is None:
                    lost.append(f"{path}{key}")
                else:
                    kept[key] = self._walk(item, child, f"{path}{key}.", lost)
            return kept
        if isinstance(value, list) and _LIST in node.children:
            inner = node.children[_LIST]
            return [self._walk(item, inner, f"{path[:-1]}[].", lost) for item in value]
        # A scalar where the translator reads a container: it refuses that itself.
        return value


def _parts(path: str) -> list[str]:
    parts: list[str] = []
    for segment in path.split("."):
        name = segment
        lists = 0
        while name.endswith(_LIST):
            name, lists = name[: -len(_LIST)], lists + 1
        if name:
            parts.append(name)
        parts.extend([_LIST] * lists)
    return parts


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
