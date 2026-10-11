"""What a person or an app is told when a request fails.

One table maps each failure to the layer that stopped it and one next action
(principles P3 and P15, ``docs/design/VOICE.md`` rule 4). API clients keep the
stable ``code`` and HTTP status. Raw ids, parameter names, exception classes
and policy terms go in ``details``, never in ``message``.
"""

from __future__ import annotations

from typing import Any

# The layers a person can tell apart. "unknown" says the evidence does not
# show where the request stopped.
LAYERS = frozenset({"request", "account", "library", "gateway", "engine", "model", "workflow", "unknown"})

UNKNOWN = "unknown"

_COPY: dict[str, tuple[str, str]] = {
    UNKNOWN: ("unknown", "We couldn't tell where this request stopped. Try again."),
    # The person's sign-in and what their account may see or do.
    "not_authenticated": ("account", "You're signed out. Sign in again."),
    "session_user_not_found": ("account", "Your sign-in no longer matches an account. Sign in again."),
    "owner_required": ("account", "Only the owner can see other people's work. Choose your own work."),
    "source_not_allowed": ("account", "You can't see this app's work. Choose one of your apps."),
    "make_disabled": ("account", "Making is turned off for your account. Ask the owner to turn it on."),
    "too_many_jobs": ("account", "You already have {maximum} things being made. "
                                 "Wait for one to finish, then try again."),
    # Items in the Library and the tasks that make them.
    "asset_not_found": ("library", "This item isn't in your Library. Return to the Library and choose another item."),
    "asset_unavailable": ("library", "This file is no longer available in the Library. "
                                     "Check a downloaded copy, or make it again."),
    "prompt_not_accessible": ("library", "Only the person who made this can download it with its prompt, "
                                         "and only while the prompt is kept. Download it without the prompt."),
    "job_not_found": ("library", "This task isn't in your account. Return to the list and choose another task."),
    "job_finished": ("library", "This task has already finished. Refresh to see the result."),
    "job_finishing": ("library", "This picture is already made, so it can't be canceled. "
                                 "Wait for it to appear in your Library."),
    "source_not_found": ("library", "That source picture isn't in your Library. Choose another picture."),
    "source_pending": ("library", "That picture isn't ready. Wait for it to finish."),
    "source_missing": ("library", "That task didn't produce a picture. Choose another source picture."),
    "input_file_unavailable": ("library", "The file for input '{input}' isn't available. "
                                          "Send the file again, or choose another one."),
    # The request itself: usually a stale page or a client bug.
    "invalid_since": ("request", "This list can't be refreshed from that point. Refresh the page."),
    "invalid_cursor": ("request", "This page of results can't be loaded. Refresh the list."),
    "invalid_limit": ("request", "This page of results can't be loaded. Refresh the list."),
    "invalid_status": ("request", "This task filter isn't recognized. Refresh the page."),
    "invalid_source": ("request", "This app filter isn't recognized. Choose an app from the list."),
    "invalid_date": ("request", "This date isn't recognized. Choose the date again."),
    "invalid_metadata": ("request", "This download option isn't offered. Choose Download again."),
    "unsupported_period": ("request", "Usage is shown for the current month only. Refresh the page."),
    "invalid_json": ("request", "The gateway couldn't read this request. Refresh the page and try again."),
    "unsupported_field": ("request", "This request has a setting the gateway doesn't accept. "
                                     "Refresh the page and try again."),
    "unsupported_intent": ("request", "This page can only make or edit pictures. Refresh the page and try again."),
    "invalid_prompt_mode": ("request", "The prompt setting isn't recognized. Refresh the page and try again."),
    "model_required": ("request", "No model is chosen. Choose a model."),
    "prompt_required": ("request", "The description is empty. Describe what to make."),
    "prompt_too_long": ("request", "The description is too long. Keep it to {max_chars:,} characters or fewer."),
    "invalid_n": ("request", "That number of pictures isn't allowed. Choose from 1 to {max_images}."),
    "edit_needs_source": ("request", "Editing needs a source picture. Choose a picture to edit."),
    "generate_takes_no_source": ("request", "A new picture doesn't use a source picture. Remove the source picture."),
    "one_source": ("request", "Two source pictures were sent. Choose one."),
    "idempotency_conflict": ("request", "This submission was already used for a different request. "
                                        "Refresh the page before you submit again."),
    "input_not_accepted": ("request", "This workflow doesn't take input '{input}'. Remove it and try again."),
    "input_set_by_gateway": ("request", "The gateway sets input '{input}' itself. Remove it and try again."),
    # The gateway: its queue, its restarts, its configuration.
    "make_unavailable": ("gateway", "Making pictures isn't set up on this gateway. Ask the owner to set it up."),
    "make_busy": ("gateway", "The gateway is busy making other things. Try again in a minute."),
    "make_queue_failed": ("gateway", "The gateway couldn't save this task to the queue. Try again."),
    "input_lost": ("gateway", "The gateway lost this task's details before it ran. Make it again."),
    "restart_while_canceling": ("gateway", "The gateway restarted while this task was being canceled. "
                                           "Make it again if you still want it."),
    "restart_before_finish": ("gateway", "The gateway restarted before this task finished. Make it again."),
    "restart_while_running": ("gateway", "The gateway restarted while this task was running, and it wasn't run "
                                         "again. Check your Library, then make it again if the picture is missing."),
    "alias_misconfigured": ("gateway", "Model '{model}' is set up incorrectly on this gateway. "
                                       "Ask the owner to check it."),
    # What runs the work.
    "model_not_deployed": ("engine", "No engine or provider is running model '{model}' right now. "
                                     "Choose another model, or ask the owner to turn one on."),
    "make_failed": ("unknown", "The picture couldn't be made, and we couldn't tell where it stopped. Try again."),
    "no_picture": ("model", "The model finished without a picture. Try again."),
    "input_mapping_invalid": ("workflow", "Input '{input}' of this workflow is set up incorrectly. "
                                          "Ask the owner to check the workflow."),
}


def describe(key: str, **values: Any) -> tuple[str, str]:
    """Return ``(layer, message)`` for a failure. An unlisted key is unknown."""
    layer, template = _COPY.get(key, _COPY[UNKNOWN])
    return layer, template.format(**values)


class PublicFailure(Exception):
    """A failure whose message is safe to show; ``details`` are for developers.

    ``copy`` picks the sentence when one code covers several causes; it
    defaults to ``code``. ``details`` also fill the sentence's placeholders.
    """

    def __init__(self, code: str, status_code: int, *, copy: str | None = None,
                 retry_after: int | None = None, **details: Any) -> None:
        self.layer, self.message = describe(copy or code, **details)
        super().__init__(self.message)
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after
        self.details = {"layer": self.layer, **details}

    def body(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}
