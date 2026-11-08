"""The reviewed code a binding descriptor can name: role implementations and target checks (CODE_TO_DATA_S3, D7).

A protocol description is data. It lists the roles a binding may hold and the adapter whose rules check the binding's
engine-side references. The code is registered here, by id, and nowhere else:

- A *role implementation* is the reviewed code a role id stands for. A document names a role by id; it never carries
  code. A role the description lists but no code registered is not run: the caller asks ``role_implementation`` and gets
  ``None``, and refuses the work as ``binding_unavailable`` before any effect.
- A *selector cardinality* hook says how many items one normalized selector yields when it succeeds
  (``selector_cardinality(selector) -> (min, max)``, ``max`` ``None`` for no bound of its own). It is registered under the
  adapter's id. Core checks only that a declared output ``count`` meets that interval (``count_unreachable``).
- A *target check* is a reviewed ``check_targets`` hook, registered under a name that a description gives
  (``check_targets`` in the description). Core checks only the type and the bound of an engine-side reference; the hook
  checks what the reference points at.

The module names no workflow, engine, model, intent or modality.
"""

from __future__ import annotations

import enum
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_LOCK = threading.Lock()


class Entry(enum.Enum):
    """The route entries that call a role's code. A role's code takes the keywords of one entry, so a role registered for
    one entry is not the code another entry reviewed."""

    GENERATION = enum.auto()
    EDIT = enum.auto()
    ANALYSIS = enum.auto()


@dataclass(frozen=True)
class RoleImplementation:
    """The reviewed code behind one role id. ``serves`` is the entry it runs for; ``run`` is that entry's call."""

    role: str
    serves: Entry
    run: Callable[..., Any]


_ROLES: dict[str, RoleImplementation] = {}
_TARGET_CHECKS: dict[str, Callable[..., None]] = {}
_CARDINALITIES: dict[str, Callable[[Any], tuple[int, int | None]]] = {}


def register_role(role: str, serves: Entry, run: Callable[..., Any]) -> None:
    """Register (or replace) the implementation of ``role`` for the entry ``serves``."""
    with _LOCK:
        _ROLES[role] = RoleImplementation(role, serves, run)


def unregister_role(role: str) -> RoleImplementation | None:
    """Remove a role's implementation and return it. For tests and for an operator who takes a role out."""
    with _LOCK:
        return _ROLES.pop(role, None)


def role_implementation(role: str, serves: Entry | None = None) -> RoleImplementation | None:
    """The registered implementation of ``role``, or ``None``. With ``serves``, one registered for another route does
    not answer: it is not the code that entry reviewed."""
    with _LOCK:
        found = _ROLES.get(role)
    if found is None or (serves is not None and found.serves != serves):
        return None
    return found


def register_target_check(name: str, check: Callable[..., None]) -> None:
    """Register (or replace) the ``check_targets`` hook called ``name``."""
    with _LOCK:
        _TARGET_CHECKS[name] = check


def target_check(name: str | None) -> Callable[..., None] | None:
    with _LOCK:
        return _TARGET_CHECKS.get(name) if name else None


def register_selector_cardinality(adapter_id: str, cardinality: Callable[[Any], tuple[int, int | None]]) -> None:
    """Register (or replace) the ``selector_cardinality`` hook of the adapter called ``adapter_id``."""
    with _LOCK:
        _CARDINALITIES[adapter_id] = cardinality


def selector_cardinality(adapter_id: str | None) -> Callable[[Any], tuple[int, int | None]] | None:
    with _LOCK:
        return _CARDINALITIES.get(adapter_id) if adapter_id else None
