"""The media-job shape's wire copy: the message and code a failed job carries, by failure category.

Wire vocabulary, not engine knowledge: the categories come from the job store, and each copy names the outcome only.
The copy stays here so every job route (audio, image, video) answers a failed job with the same words.
"""
from __future__ import annotations

JOB_FAILURE_COPY = {
    "authorization": ("not_in_access_plan", "This workflow isn't in your access plan."),
    "invalid_input": ("invalid_input", "The gateway did not accept this job's inputs."),
    "capacity": ("capacity", "The gateway is too busy to run this job now. Try again later."),
    "timeout": ("timeout", "The backend did not finish this job in time."),
    "safety": ("content_policy_violation", "The backend refused this job's content."),
}


UPSTREAM_FAILURE_COPY = "Media generation failed (upstream HTTP {status})."
UPSTREAM_FAILURE_CODE = "upstream_error"


def stored_failure(mode: str, category: str, message: str, code: str, upstream_status: int | None) -> tuple[str, str]:
    """The text and code a failed job stores. ``mode`` is the row's ``job_failure_text``: ``adapter`` keeps the adapter's own
    words; ``category_copy`` stores the fixed sentence for the failure's category, because an engine's words may echo a
    prompt and the job row is read by listings and History. A category with no fixed sentence keeps the message."""
    if mode != "category_copy":
        return message, code
    if category == "upstream":
        return UPSTREAM_FAILURE_COPY.format(status=upstream_status or 502), UPSTREAM_FAILURE_CODE
    fixed = JOB_FAILURE_COPY.get(category)
    return (fixed[1], fixed[0]) if fixed is not None else (message, code)
