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
