"""The receipt a request-history capture is waiting on. This module imports nothing, so the receipt store can use it.

The capture middleware opens a slot for each captured request. Receipts that the request records or finalizes report
here, so the capture knows which stored receipt to update with its retention outcome (REQUEST_RETENTION.md 13.3). A
receipt joins the slot only when its path is the request's path and it has no parent, so an inner hop does not claim
the caller's receipt.
"""
from __future__ import annotations

import threading
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, Callable


class Slot:
    def __init__(self, path: str, forget: bool = False, media_path: Path | None = None) -> None:
        self.path = path
        self.receipt_id: str | None = None
        #: The request carries ``X-Azizam-Prompts: forget``. The middleware strips the header, so the routes read it here.
        self.forget = forget
        self.media_path = Path(media_path) if media_path is not None else None
        self._lock = threading.Lock()
        self._finalized = False
        self._library_prompts: list[tuple[dict[str, Any], Callable | None]] = []

    def defer_library_prompt(
        self,
        record: dict[str, Any],
        *,
        database_path: Path,
        on_result: Callable | None,
    ) -> bool:
        """Hold a prompt for the capture writer when it shares the media database."""
        if self.media_path is None or Path(database_path) != self.media_path:
            return False
        with self._lock:
            if self._finalized:
                return False
            self._library_prompts.append((dict(record), on_result))
            return True

    def take_library_prompts(self) -> list[tuple[dict[str, Any], Callable | None]]:
        """Close staging and hand pending prompts to the one request-finalization writer."""
        with self._lock:
            self._finalized = True
            prompts, self._library_prompts = self._library_prompts, []
            return prompts


_CURRENT: ContextVar[Slot | None] = ContextVar("request_history_slot", default=None)


def open_slot(path: str, *, forget: bool = False, media_path: Path | None = None) -> Token:
    return _CURRENT.set(Slot(path, forget, media_path))


def close_slot(token: Token) -> None:
    _CURRENT.reset(token)


def current() -> Slot | None:
    return _CURRENT.get()


def prompts_forget() -> bool:
    """Whether the request in progress asked, in its header, that nothing about it be kept (REQUEST_RETENTION.md 3.4)."""
    slot = _CURRENT.get()
    return slot is not None and slot.forget


def defer_library_prompt(
    record: dict[str, Any],
    *,
    database_path: Path,
    on_result: Callable | None = None,
) -> bool:
    """Stage a Library prompt when the active request can commit it with request history."""
    slot = _CURRENT.get()
    return bool(slot and slot.defer_library_prompt(record, database_path=database_path, on_result=on_result))


def observe(receipt_id: str | None, path: str | None, parent: str | None) -> None:
    """Called by the receipt store for each receipt it records or finalizes. Never raises."""
    slot = _CURRENT.get()
    if slot is None or slot.receipt_id is not None or not receipt_id or parent or path != slot.path:
        return
    slot.receipt_id = receipt_id
