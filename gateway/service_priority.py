"""The order of services that serve the same thing: the lower priority number goes first."""

from __future__ import annotations

from typing import Any, Mapping

DEFAULT_PRIORITY = 100


def service_priority(record: Mapping[str, Any] | None) -> int:
    """A service record's priority. A record that names none sorts at 100; 0 is 0, so a record can set 0 to go first.

    A value that is not a whole number raises, as it did before; so does a boolean, which ``int`` would read as 0 or 1.
    """
    value = record.get("priority") if isinstance(record, Mapping) else None
    if isinstance(value, bool):
        raise ValueError(f"a service priority is a whole number, not {value!r}")
    return DEFAULT_PRIORITY if value is None or value == "" else int(value)


def selected_service(
    backends: Mapping[str, Any], capability: str,
) -> tuple[str, Mapping[str, Any]] | None:
    """Select by capability, then priority and id, including disabled or addressless winners.

    Compound capabilities match either their full spelling or all dot-separated
    parts. Selection is pure and never falls through when the winning service is off.
    """
    cap = capability.strip().lower()
    parts = cap.split(".")
    compound = len(parts) > 1
    candidates: list[tuple[int, str]] = []
    for service_id, record in backends.items():
        if not isinstance(record, Mapping):
            continue
        caps = {
            str(value).strip().lower()
            for value in (record.get("capabilities") or [])
            if str(value).strip()
        }
        matched = cap in caps or (compound and all(part in caps for part in parts))
        if matched:
            candidates.append((service_priority(record), service_id))
    candidates.sort()
    if not candidates:
        return None
    service_id = candidates[0][1]
    return service_id, backends[service_id]
