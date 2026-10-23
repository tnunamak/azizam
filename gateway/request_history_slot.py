"""The receipt a request-history capture is waiting on. This module imports nothing, so the receipt store can use it.

The capture middleware opens a slot for each captured request. Receipts that the request records or finalizes report
here, so the capture knows which stored receipt to update with its retention outcome (REQUEST_RETENTION.md 13.3). A
receipt joins the slot only when its path is the request's path and it has no parent, so an inner hop does not claim
the caller's receipt.
"""
from __future__ import annotations

from contextvars import ContextVar, Token


class Slot:
    def __init__(self, path: str) -> None:
        self.path = path
        self.receipt_id: str | None = None


_CURRENT: ContextVar[Slot | None] = ContextVar("request_history_slot", default=None)


def open_slot(path: str) -> Token:
    return _CURRENT.set(Slot(path))


def close_slot(token: Token) -> None:
    _CURRENT.reset(token)


def current() -> Slot | None:
    return _CURRENT.get()


def observe(receipt_id: str | None, path: str | None, parent: str | None) -> None:
    """Called by the receipt store for each receipt it records or finalizes. Never raises."""
    slot = _CURRENT.get()
    if slot is None or slot.receipt_id is not None or not receipt_id or parent or path != slot.path:
        return
    slot.receipt_id = receipt_id
