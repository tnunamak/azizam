"""What a person or an app is told when a request fails.

One table maps each failure to the layer that stopped it and one next action
(principles P3 and P15, ``docs/design/VOICE.md`` rule 4). API clients keep the
stable ``code`` and HTTP status. Raw ids, parameter names, exception classes
and policy terms go in ``details``, never in ``message``.
"""

from __future__ import annotations

import math
import re
from string import Formatter
from typing import Any, Mapping

# The layers a person can tell apart. "unknown" says the evidence does not
# show where the request stopped.
LAYERS = frozenset({"request", "account", "library", "gateway", "engine", "model", "workflow", "unknown"})

UNKNOWN = "unknown"

_COPY: dict[str, tuple[str, str] | tuple[str, str, str]] = {
    "target_unknown": ("request", "This model isn't available. Choose another model.",
                       "Unknown target. Call capabilities to list valid targets."),
    "mode_unavailable": ("request", "This way of running the task isn't available. Choose another way.",
                         "The requested execution mode is unavailable for this target."),
    "response_invalid": ("gateway", "The gateway returned a result it couldn't read. Try again.",
                         "The gateway returned an unsupported result format."),
    "value_too_large": ("request", "A request value is too large. Choose a smaller value.",
                        "A request value must be at most {maximum}."),
    "value_too_small": ("request", "A request value is too small. Choose a larger value.",
                        "A request value must be at least {minimum}."),
    "input_string": ("request", "An input needs text. Enter text and try again.",
                      "An input must be a string."),
    "input_integer": ("request", "An input needs a whole number. Enter a whole number.",
                       "An input must be an integer."),
    "input_number": ("request", "An input needs a number. Enter a number.",
                      "An input must be a number."),
    "input_boolean": ("request", "An input needs a yes or no choice. Choose one.",
                       "An input must be a boolean."),
    "input_choice": ("request", "An input has an unsupported choice. Choose a supported value.",
                      "An input must match one of the supported choices."),
    "input_required": ("request", "A required input is missing. Add the missing input.",
                        "A required input is missing."),
    "input_unknown": ("request", "The request has an unknown input. Remove the unknown input.",
                       "The request contains an unknown input."),
    "file_base64_invalid": ("request", "The input file couldn't be read. Send a valid file again.",
                             "The input file has invalid base64 encoding."),
    "file_url_unsupported": ("request", "The file link can't be read. Send the file itself.",
                              "External file URLs are not fetched; use a data URI or a file from an earlier result."),
    "invalid_inputs": ("request", "The request has an invalid input. Check the inputs and try again.",
                       "One or more inputs do not match the required type or supported choices."),
    "input_file_invalid": ("request", "The input file couldn't be read. Send a valid file again.",
                           "The input must be a valid data URI or a file from an earlier result."),
    "dimensions_incomplete": ("request", "A picture dimension is missing. Send both dimensions.",
                              "Width and height must be supplied together."),
    "out_of_memory": ("engine", "The engine couldn't finish this request. Try a smaller request.",
                       "The engine ran out of memory. Try a smaller request."),
    "profile_denied": ("account", "Your account can't run this request. Ask the owner for access.",
                        "This profile may not run this request."),
    "canceled": ("request", "This task was canceled. Make it again if you still want it."),
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
    "media_folder_unavailable": ("library", "Azizam can't see its media folder right now, so nothing was removed. Try again when the folder is back."),
    "media_folder_empty": ("library", "Azizam's media folder is empty, which usually means it isn't mounted, so nothing was removed. Check the disk, then try again."),
    "media_folder_unconfirmed": ("library", "Azizam can't confirm this is the right media folder, so nothing was removed. Ask the owner to check the disk and confirm this folder."),
    "media_folder_owner_required": ("account", "Only the owner can confirm the media folder. Ask the owner to check the disk and confirm this folder."),
    "prompt_not_accessible": ("library", "Only the person who made this can download it with its prompt, "
                                         "and only while the prompt is kept. Download it without the prompt."),
    "job_not_found": ("library", "This task isn't in your account. Return to the list and choose another task."),
    "job_finished": ("library", "This task has already finished. Refresh to see the result."),
    "job_finishing": ("library", "This picture is already made, so it can't be canceled. "
                                 "Wait for it to appear in your Library."),
    "source_not_found": ("library", "That source picture isn't in your Library. Choose another picture."),
    "source_pending": ("library", "That picture isn't ready. Wait for it to finish."),
    "source_missing": ("library", "That task didn't produce a picture. Choose another source picture."),
    "input_file_unavailable": ("library", "The input file isn't available. "
                                          "Send the file again, or choose another one.",
                               "The file for input '{input}' isn't available. "
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
    "input_not_accepted": ("request", "This workflow doesn't take that input. Remove it and try again.",
                           "This workflow doesn't take input '{input}'. Remove it and try again."),
    "input_set_by_gateway": ("request", "The gateway sets that input itself. Remove it and try again.",
                             "The gateway sets input '{input}' itself. Remove it and try again."),
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
    "alias_misconfigured": ("gateway", "This model is set up incorrectly on this gateway. "
                                       "Ask the owner to check it.",
                            "Model '{model}' is set up incorrectly on this gateway. "
                            "Ask the owner to check it."),
    # What runs the work.
    "model_not_deployed": ("engine", "No engine or provider is running this model right now. "
                                     "Choose another model, or ask the owner to turn one on.",
                           "No engine or provider is running model '{model}' right now. "
                           "Choose another model, or ask the owner to turn one on."),
    "make_failed": ("unknown", "The picture couldn't be made, and we couldn't tell where it stopped. Try again."),
    "no_picture": ("model", "The model finished without a picture. Try again."),
    "input_mapping_invalid": ("workflow", "An input of this workflow is set up incorrectly. "
                                          "Ask the owner to check the workflow.",
                              "Input '{input}' of this workflow is set up incorrectly. "
                              "Ask the owner to check the workflow."),
}


# API copy may name what the client itself sent; member copy never does.
_IDENTIFIER_FIELDS = frozenset({"input", "model"})


def _public_value(name: str, value: Any) -> bool:
    if name in _IDENTIFIER_FIELDS:
        return isinstance(value, str) and 0 < len(value) <= 200 and value.isprintable()
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and abs(value) <= 10 ** 12 and math.isfinite(value))


def describe(key: str, *, audience: str = "ui", **values: Any) -> tuple[str, str]:
    """Render one shared entry.

    Numeric placeholders must be bounded numbers. Only API copy names an
    input or model, the client's own request terms.
    """
    entry = _COPY.get(key, _COPY[UNKNOWN])
    template = entry[2] if audience == "api" and len(entry) == 3 else entry[1]
    fields = {name for _, name, _, _ in Formatter().parse(template) if name}
    if not all(_public_value(name, values.get(name)) for name in fields):
        return _COPY[UNKNOWN][0], _COPY[UNKNOWN][1]
    return entry[0], template.format(**values)


def _reason(message: Any, code: Any, values: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Recognize evidence, never echo arbitrary upstream text or schema values."""
    preferred = [key for key in (code, values.get("copy_key")) if isinstance(key, str) and key in _COPY]
    values = {name: value for name, value in values.items()
              if name in {"maximum", "minimum", "max_chars", "max_images"}}
    if isinstance(message, str):
        if message in {"Canceled by requester", "Cancelled by requester"}:
            return "canceled", {}
        for key in dict.fromkeys([*preferred, *_COPY]):
            for audience in ("api", "ui"):
                layer, rendered = describe(key, audience=audience, **dict(values))
                if key != UNKNOWN and rendered != _COPY[UNKNOWN][1] and message == rendered:
                    return key, dict(values)
        # Legacy errors contain the useful reason alongside ids or exception
        # classes. Extract only the bounded fact and render it from the table.
        if re.search(r"\bout of memory\b", message, re.IGNORECASE):
            return "out_of_memory", {}
        if re.search(r"\bprofile\b.{0,160}\b(?:may not|not allowed to|cannot)\s+run\b",
                     message, re.IGNORECASE):
            return "profile_denied", {}
        limit = re.search(r"\bat (most|least) (-?\d{1,12}(?:\.\d{1,6})?)(?!\w|\.\d)", message)
        if limit:
            value = float(limit[2]) if "." in limit[2] else int(limit[2])
            return ("value_too_large", {"maximum": value}) if limit[1] == "most" else (
                "value_too_small", {"minimum": value})
        for pattern, key in (
            (r"\bmust be a string\b", "input_string"),
            (r"\bmust be an integer\b", "input_integer"),
            (r"\bmust be a number\b", "input_number"),
            (r"\bmust be a boolean\b", "input_boolean"),
            (r"\bmust be one of\b", "input_choice"),
            (r"\bmissing required input\b", "input_required"),
            (r"\bunknown input\b", "input_unknown"),
        ):
            if re.search(pattern, message):
                return key, {}
    if isinstance(code, str) and code in _COPY:
        return code, dict(values)
    return UNKNOWN, {}


def project_failure(error: Any, *, audience: str = "api") -> dict[str, Any]:
    """Project a gateway error or terminal-job error without losing diagnostics.

    API envelopes retain specific safe reasons. UI callers explicitly request
    layer-and-action copy. Code, type, decision and raw evidence stay separate
    from the sentence; malformed upstream details cannot replace our layer.
    """
    source = dict(error) if isinstance(error, Mapping) else {"message": error}
    upstream = source.get("details")
    details = dict(upstream) if isinstance(upstream, Mapping) else {}
    if upstream is not None and not isinstance(upstream, Mapping):
        details["upstream_details"] = upstream
    key, values = _reason(source.get("message"), source.get("code"), details)
    layer, message = describe(key, audience=audience, **values)
    if source.get("message") != message and source.get("message") is not None:
        details.setdefault("original_message", source["message"])
    details.update(values)
    details["copy_key"] = key
    details["layer"] = layer
    details["ui_message"] = describe(key, **values)[1]
    for name, value in source.items():
        if name not in {"message", "details", "code", "type", "decision"}:
            details[name] = value
    # Reserved projection fields are always ours, even with hostile diagnostics.
    details["copy_key"] = key
    details["layer"] = layer
    details["ui_message"] = describe(key, **values)[1]
    return {**{name: source[name] for name in ("code", "type", "decision") if name in source},
            "message": message, "details": details}


def project_job(view: Mapping[str, Any], *, audience: str = "ui") -> dict[str, Any]:
    """Copy a client job and normalize terminal failure text at its boundary."""
    result = dict(view)
    status = result.get("status")
    if status not in {"failed", "canceled", "cancelled"}:
        return result
    error = result.get("error")
    if not error and status in {"canceled", "cancelled"}:
        error = {"code": "canceled"}
    if not isinstance(error, Mapping) and isinstance(result.get("error_details"), Mapping):
        error = {"message": error, "details": result["error_details"]}
    projected = project_failure(error, audience=audience)
    result["error"] = projected["message"]
    result["error_details"] = projected["details"]
    for name in ("code", "type", "decision"):
        if name in projected:
            result["error_details"][name] = projected[name]
    return result


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
