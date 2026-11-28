"""Turn state: a routing value an upstream issues at the start of a turn, sent back on the later requests of that turn.

Some upstreams return a value in a response header at the start of a turn and expect the client to send it back, in
the same header, on every later request of that turn (tool-call follow-ups, retries), and never on a request of
another turn. Only the first value of a turn counts: a later response of the same turn cannot replace it.

The gateway keeps one record per conversation, in memory only. A record is never persisted, logged or put in a
receipt. The store is bounded: at most ``max_conversations`` records (the least recently used goes first), and a
record unused for ``idle_seconds`` is dropped.

Which request continues a turn is a rule of the wire format, so the request protocol supplies it (``continues``). The
store adds one check that does not depend on the format: a request continues the previous request's turn only when its
input starts with that request's whole input, unchanged. So a conversation that went on without the gateway seeing it,
or another conversation with the same key, starts a new turn instead of getting the old turn's value.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from gateway import transport_budgets

_MAX_VALUE_LENGTH = transport_budgets.section("turn_state")["max_value_length"]   # registry/transport_budgets.yml


@dataclass
class _Turn:
    turn_id: int
    length: int        # items in the input of the conversation's last request
    digest: str        # hash of those items
    value: str | None  # the turn's value, once a response returned one
    used: float


def _digests(items: Sequence[Any], at: int) -> tuple[str, str]:
    """(hash of the first ``at`` items, hash of all items), in one pass."""
    hasher = hashlib.sha256()
    prefix = hasher.hexdigest() if at == 0 else ""
    for index, item in enumerate(items, 1):
        hasher.update(hashlib.sha256(json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                                default=str).encode("utf-8", "replace")).digest())
        if index == at:
            prefix = hasher.copy().hexdigest()
    return prefix, hasher.hexdigest()


def usable(value: Any) -> bool:
    """A value the gateway can send back as a header: visible ASCII, not empty, not too long."""
    return (isinstance(value, str) and 0 < len(value) <= _MAX_VALUE_LENGTH
            and all("!" <= char <= "~" for char in value))


def scope_of(*parts: Any) -> str:
    """One conversation of one caller on one connection, as a digest: the store keeps no raw identifier."""
    return hashlib.sha256(json.dumps(parts, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


class TurnStates:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic, max_conversations: int = 4096,
                 idle_seconds: float = 3600.0) -> None:
        self._clock, self._max, self._idle = clock, max_conversations, idle_seconds
        self._turns: OrderedDict[str, _Turn] = OrderedDict()
        self._lock = threading.Lock()
        self._next_id = 0

    def __len__(self) -> int:
        with self._lock:
            self._expire(self._clock())
            return len(self._turns)

    def _expire(self, now: float) -> None:
        while self._turns:
            oldest = next(iter(self._turns.values()))
            if now - oldest.used <= self._idle:
                break
            self._turns.popitem(last=False)

    def outgoing(self, scope: str, items: Sequence[Any],
                 continues: Callable[[Sequence[Any], int], bool]) -> tuple[int, bool, str | None]:
        """Record this request and decide its turn: (turn id, whether it continues the last turn, value to send).

        ``continues(items, start)`` is the protocol's rule, given the items and the index of the first item added since
        the conversation's last request. A new turn sends no value; its first returned value is kept by ``incoming``.
        """
        with self._lock:
            self._expire(self._clock())
            last = self._turns.get(scope)
        at = last.length if last is not None and last.length <= len(items) else -1
        prefix, whole = _digests(items, at)  # outside the lock: an input can be large
        now = self._clock()
        with self._lock:
            if self._turns.get(scope) is not last:
                last = None  # another request of this conversation was recorded meanwhile: start a new turn
            if last is not None and at >= 0 and prefix == last.digest and continues(items, at):
                turn_id, carried, value = last.turn_id, True, last.value
            else:
                self._next_id += 1
                turn_id, carried, value = self._next_id, False, None
            self._turns[scope] = _Turn(turn_id, len(items), whole, value, now)
            self._turns.move_to_end(scope)
            while len(self._turns) > self._max:
                self._turns.popitem(last=False)
            return turn_id, carried, value

    def incoming(self, scope: str, turn_id: int, value: Any) -> None:
        """Keep a response's value as its turn's, unless the turn already has one or another turn has begun since."""
        if not usable(value):
            return
        with self._lock:
            turn = self._turns.get(scope)
            if turn is not None and turn.turn_id == turn_id and turn.value is None:
                turn.value = value
