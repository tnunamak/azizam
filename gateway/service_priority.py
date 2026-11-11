"""The order of services that serve the same thing: the lower priority number goes first."""

from __future__ import annotations

from typing import Any, Mapping

DEFAULT_PRIORITY = 100


def service_priority(record: Mapping[str, Any] | None) -> int:
    """A service record's priority. A record that names none sorts at 100; 0 is 0, so a record can set 0 to go first.

    Non-integral floats and booleans are refused; an integral float is normalized to an int.
    """
    value = record.get("priority") if isinstance(record, Mapping) else None
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
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
        capabilities = record.get("capabilities")
        if capabilities is not None and not isinstance(capabilities, (list, tuple)):
            raise ValueError(
                f"service {service_id!r}: capabilities are a list or tuple, not {capabilities!r}"
            )
        caps = {
            str(value).strip().lower()
            for value in (capabilities or [])
            if str(value).strip()
        }
        matched = cap in caps or (compound and all(part in caps for part in parts))
        if matched:
            try:
                priority = service_priority(record)
            except ValueError as exc:
                raise ValueError(f"service {service_id!r}: {exc}") from exc
            candidates.append((priority, service_id))
    candidates.sort()
    if not candidates:
        return None
    service_id = candidates[0][1]
    return service_id, backends[service_id]
