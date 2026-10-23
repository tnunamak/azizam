"""The receipt a request-history capture is waiting on. This module imports nothing, so the receipt store can use it.

The capture middleware opens a slot for each captured request. Receipts that the request records or finalizes report
here, so the capture knows which stored receipt to update with its retention outcome (REQUEST_RETENTION.md 13.3). A
receipt joins the slot only when its path is the request's route and it has no parent, so an inner hop does not claim
the caller's receipt.
"""
from __future__ import annotations

from contextvars import ContextVar, Token


class Slot:
    def __init__(self, path: str, forget: bool = False) -> None:
        self.path = path
        self.receipt_id: str | None = None
        #: The request carries ``X-Azizam-Prompts: forget``. The middleware strips the header, so the routes read it here.
        self.forget = forget


_CURRENT: ContextVar[Slot | None] = ContextVar("request_history_slot", default=None)


def open_slot(path: str, *, forget: bool = False) -> Token:
    return _CURRENT.set(Slot(path, forget))


def close_slot(token: Token) -> None:
    _CURRENT.reset(token)


def current() -> Slot | None:
    return _CURRENT.get()


def prompts_forget() -> bool:
    """Whether the request in progress asked, in its header, that nothing about it be kept (REQUEST_RETENTION.md 3.4)."""
    slot = _CURRENT.get()
    return slot is not None and slot.forget


def _canonical(path: str) -> str:
    """The route a path names. The OpenAI mount serves the same route as /v1 (``/openai/v1/x`` is ``/v1/x``)."""
    return path.removeprefix("/openai") if path.startswith("/openai/") else path


def observe(receipt_id: str | None, path: str | None, parent: str | None) -> None:
    """Called by the receipt store for each receipt it records or finalizes. Never raises.

    A receipt names the canonical route, so a request that came through the /openai mount is matched on that route.
    """
    slot = _CURRENT.get()
    if slot is None or slot.receipt_id is not None or not receipt_id or parent or not path:
        return
    if _canonical(path) != _canonical(slot.path):
        return
    slot.receipt_id = receipt_id
