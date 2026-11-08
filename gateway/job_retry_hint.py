"""How long a caller should wait before asking again, for a job that failed because a resource was busy.

The job store records a failure's code, category and ``retryable`` flag, not a delay. An adapter that knows the delay writes
it to the job as one ``retry_after`` event (``step`` holds the seconds); a wire that answers ``Retry-After`` reads the
newest such event. Both go through the job's own event log, so the delay is read with the same access as the job.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

from gateway import media_jobs

EVENT = "retry_after"


def write(job_id: str, seconds: int, *, path: Path) -> None:
    media_jobs.append_event(job_id, "retry_after", {"phase": "busy", "step": int(seconds)}, path=path)


def read(events: Iterable[Any]) -> int | None:
    """The newest delay among ``events`` (job-service events: ``type`` and ``payload``), or None."""
    found: int | None = None
    for event in events:
        if getattr(event, "type", None) != EVENT:
            continue
        payload: Mapping[str, Any] = getattr(event, "payload", None) or {}
        step = payload.get("step")
        if isinstance(step, int) and not isinstance(step, bool) and step > 0:
            found = step
    return found
