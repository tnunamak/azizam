"""The owner's workflow run switch and record exposure form one closure rule."""

from typing import Any


def closed_reason(record: Any, run_state: str | None = None) -> str | None:
    if record is None:
        return None
    facts = getattr(record, "facts", None)
    state = run_state if run_state is not None else getattr(record, "state", "active")
    if (facts is not None and facts.exposure == "disabled") or state == "disabled":
        return f"workflow {record.id} is disabled"
    return None
