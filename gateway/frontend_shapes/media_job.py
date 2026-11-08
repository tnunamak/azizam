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
