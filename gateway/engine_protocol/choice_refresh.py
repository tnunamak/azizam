"""Refresh engine-derived choice snapshots; callers decide when to run it."""
from __future__ import annotations

from gateway.engine_protocol.choice_store import ChoiceStore, choices_from_node_info

DEFAULT_MAX_AGE = 24 * 3600.0
SUPERSEDED = 'superseded by a newer snapshot'


def is_due(store: ChoiceStore, service, node_type, input, *, now: float, max_age: float = DEFAULT_MAX_AGE) -> bool:
    """True when the snapshot is missing or at least ``max_age`` seconds old."""
    snapshot = store.read(service, node_type, input)
    return snapshot is None or snapshot.stale(now=now, max_age=max_age)


def _reason(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:200]


def refresh_service(store: ChoiceStore, service, declarations, *, fetch, now: float):
    """Refresh each declared ``{node_type, input}`` for one service.

    ``fetch(service, node_type)`` must return a freshly fetched node definition
    or raise; a cached or on-disk fallback is not a successful fetch. A failed
    fetch or invalid list keeps the previous entry and records the failure.
    A refresh older than the stored snapshot changes nothing and reports
    ``SUPERSEDED``. Returns ``{(node_type, input): None | failure reason}``.
    """
    definitions: dict = {}
    report = {}
    for declaration in declarations:
        node_type, input_name = declaration['node_type'], declaration['input']
        try:
            if node_type not in definitions:
                try:
                    definitions[node_type] = (True, fetch(service, node_type))
                except Exception as exc:  # noqa: BLE001 - any failed fetch keeps the last-known-good entry
                    definitions[node_type] = (False, exc)
            ok, value = definitions[node_type]
            if not ok:
                raise value
            choices = choices_from_node_info(value, input_name)
            stored = store.write_clearing_failure(service, node_type, input_name, choices, fetched_at=now)
            report[(node_type, input_name)] = None if stored else SUPERSEDED
        except Exception as exc:  # noqa: BLE001
            reason = _reason(exc)
            recorded = store.record_failure(service, node_type, input_name, reason, at=now)
            report[(node_type, input_name)] = reason if recorded else SUPERSEDED
    return report
