"""Golden copy for every public failure code (gateway/public_failure.py).

The literals below are the sentences people and API clients read. They are written out here, not
read from `_COPY`, so renaming a code, moving it to another layer or rewording it fails a test.
Regenerate only on purpose, when the copy changes, and review the diff like a product change.
"""

from __future__ import annotations

import pytest

from gateway.public_failure import describe

# code: (layer, member sentence, API sentence when it differs)
GOLDEN = {
    'target_unknown': ('request',
        "This model isn't available. Choose another model.",
        'Unknown target. Call capabilities to list valid targets.'),
    'mode_unavailable': ('request',
        "This way of running the task isn't available. Choose another way.",
        'The requested execution mode is unavailable for this target.'),
    'response_invalid': ('gateway',
        "The gateway returned a result it couldn't read. Try again.",
        'The gateway returned an unsupported result format.'),
    'value_too_large': ('request',
        'A request value is too large. Choose a smaller value.',
        'A request value must be at most {maximum}.'),
    'value_too_small': ('request',
        'A request value is too small. Choose a larger value.',
        'A request value must be at least {minimum}.'),
    'input_string': ('request',
        'An input needs text. Enter text and try again.',
        'An input must be a string.'),
    'input_integer': ('request',
        'An input needs a whole number. Enter a whole number.',
        'An input must be an integer.'),
    'input_number': ('request',
        'An input needs a number. Enter a number.',
        'An input must be a number.'),
    'input_boolean': ('request',
        'An input needs a yes or no choice. Choose one.',
        'An input must be a boolean.'),
    'input_choice': ('request',
        'An input has an unsupported choice. Choose a supported value.',
        'An input must match one of the supported choices.'),
    'input_required': ('request',
        'A required input is missing. Add the missing input.',
        'A required input is missing.'),
    'input_unknown': ('request',
        'The request has an unknown input. Remove the unknown input.',
        'The request contains an unknown input.'),
    'file_base64_invalid': ('request',
        "The input file couldn't be read. Send a valid file again.",
        'The input file has invalid base64 encoding.'),
    'file_url_unsupported': ('request',
        "The file link can't be read. Send the file itself.",
        'External file URLs are not fetched; use a data URI or a file from an earlier result.'),
    'invalid_inputs': ('request',
        'The request has an invalid input. Check the inputs and try again.',
        'One or more inputs do not match the required type or supported choices.'),
    'input_file_invalid': ('request',
        "The input file couldn't be read. Send a valid file again.",
        'The input must be a valid data URI or a file from an earlier result.'),
    'dimensions_incomplete': ('request',
        'A picture dimension is missing. Send both dimensions.',
        'Width and height must be supplied together.'),
    'out_of_memory': ('engine',
        "The engine couldn't finish this request. Try a smaller request.",
        'The engine ran out of memory. Try a smaller request.'),
    'profile_denied': ('account',
        "Your account can't run this request. Ask the owner for access.",
        'This profile may not run this request.'),
    'canceled': ('request',
        'This task was canceled. Make it again if you still want it.'),
    'unknown': ('unknown',
        "We couldn't tell where this request stopped. Try again."),
    'not_authenticated': ('account',
        "You're signed out. Sign in again."),
    'session_user_not_found': ('account',
        'Your sign-in no longer matches an account. Sign in again.'),
    'owner_required': ('account',
        "Only the owner can see other people's work. Choose your own work."),
    'source_not_allowed': ('account',
        "You can't see this app's work. Choose one of your apps."),
    'make_disabled': ('account',
        'Making is turned off for your account. Ask the owner to turn it on.'),
    'too_many_jobs': ('account',
        'You already have {maximum} things being made. Wait for one to finish, then try again.'),
    'asset_not_found': ('library',
        "This item isn't in your Library. Return to the Library and choose another item."),
    'asset_unavailable': ('library',
        'This file is no longer available in the Library. Check a downloaded copy, or make it again.'),
    'media_folder_unavailable': ('library',
        "Azizam can't see its media folder right now, so nothing was removed. Try again when the folder is back."),
    'media_folder_empty': ('library',
        "Azizam's media folder is empty, which usually means it isn't mounted, so nothing was removed. Check the disk, then try again."),
    'prompt_not_accessible': ('library',
        'Only the person who made this can download it with its prompt, and only while the prompt is kept. Download it without the prompt.'),
    'job_not_found': ('library',
        "This task isn't in your account. Return to the list and choose another task."),
    'job_finished': ('library',
        'This task has already finished. Refresh to see the result.'),
    'job_finishing': ('library',
        "This picture is already made, so it can't be canceled. Wait for it to appear in your Library."),
    'source_not_found': ('library',
        "That source picture isn't in your Library. Choose another picture."),
    'source_pending': ('library',
        "That picture isn't ready. Wait for it to finish."),
    'source_missing': ('library',
        "That task didn't produce a picture. Choose another source picture."),
    'input_file_unavailable': ('library',
        "The input file isn't available. Send the file again, or choose another one.",
        "The file for input '{input}' isn't available. Send the file again, or choose another one."),
    'invalid_since': ('request',
        "This list can't be refreshed from that point. Refresh the page."),
    'invalid_cursor': ('request',
        "This page of results can't be loaded. Refresh the list."),
    'invalid_limit': ('request',
        "This page of results can't be loaded. Refresh the list."),
    'invalid_status': ('request',
        "This task filter isn't recognized. Refresh the page."),
    'invalid_source': ('request',
        "This app filter isn't recognized. Choose an app from the list."),
    'invalid_date': ('request',
        "This date isn't recognized. Choose the date again."),
    'invalid_metadata': ('request',
        "This download option isn't offered. Choose Download again."),
    'unsupported_period': ('request',
        'Usage is shown for the current month only. Refresh the page.'),
    'invalid_json': ('request',
        "The gateway couldn't read this request. Refresh the page and try again."),
    'unsupported_field': ('request',
        "This request has a setting the gateway doesn't accept. Refresh the page and try again."),
    'unsupported_intent': ('request',
        'This page can only make or edit pictures. Refresh the page and try again.'),
    'invalid_prompt_mode': ('request',
        "The prompt setting isn't recognized. Refresh the page and try again."),
    'model_required': ('request',
        'No model is chosen. Choose a model.'),
    'prompt_required': ('request',
        'The description is empty. Describe what to make.'),
    'prompt_too_long': ('request',
        'The description is too long. Keep it to {max_chars:,} characters or fewer.'),
    'invalid_n': ('request',
        "That number of pictures isn't allowed. Choose from 1 to {max_images}."),
    'edit_needs_source': ('request',
        'Editing needs a source picture. Choose a picture to edit.'),
    'generate_takes_no_source': ('request',
        "A new picture doesn't use a source picture. Remove the source picture."),
    'one_source': ('request',
        'Two source pictures were sent. Choose one.'),
    'idempotency_conflict': ('request',
        'This submission was already used for a different request. Refresh the page before you submit again.'),
    'input_not_accepted': ('request',
        "This workflow doesn't take that input. Remove it and try again.",
        "This workflow doesn't take input '{input}'. Remove it and try again."),
    'input_set_by_gateway': ('request',
        'The gateway sets that input itself. Remove it and try again.',
        "The gateway sets input '{input}' itself. Remove it and try again."),
    'make_unavailable': ('gateway',
        "Making pictures isn't set up on this gateway. Ask the owner to set it up."),
    'make_busy': ('gateway',
        'The gateway is busy making other things. Try again in a minute.'),
    'make_queue_failed': ('gateway',
        "The gateway couldn't save this task to the queue. Try again."),
    'input_lost': ('gateway',
        "The gateway lost this task's details before it ran. Make it again."),
    'restart_while_canceling': ('gateway',
        'The gateway restarted while this task was being canceled. Make it again if you still want it.'),
    'restart_before_finish': ('gateway',
        'The gateway restarted before this task finished. Make it again.'),
    'restart_while_running': ('gateway',
        "The gateway restarted while this task was running, and it wasn't run again. Check your Library, then make it again if the picture is missing."),
    'alias_misconfigured': ('gateway',
        'This model is set up incorrectly on this gateway. Ask the owner to check it.',
        "Model '{model}' is set up incorrectly on this gateway. Ask the owner to check it."),
    'model_not_deployed': ('engine',
        'No engine or provider is running this model right now. Choose another model, or ask the owner to turn one on.',
        "No engine or provider is running model '{model}' right now. Choose another model, or ask the owner to turn one on."),
    'make_failed': ('unknown',
        "The picture couldn't be made, and we couldn't tell where it stopped. Try again."),
    'no_picture': ('model',
        'The model finished without a picture. Try again.'),
    'input_mapping_invalid': ('workflow',
        'An input of this workflow is set up incorrectly. Ask the owner to check the workflow.',
        "Input '{input}' of this workflow is set up incorrectly. Ask the owner to check the workflow."),
}

_VALUES = {"model": "m-1", "input": "cfg", "maximum": 5, "minimum": 2, "max_chars": 100, "max_images": 4}


def _fields(template: str) -> dict[str, object]:
    from string import Formatter
    return {name: _VALUES[name] for _, name, _, _ in Formatter().parse(template) if name}


def test_the_table_has_exactly_the_golden_codes():
    from gateway.public_failure import _COPY
    assert set(_COPY) == set(GOLDEN)


@pytest.mark.parametrize("code", sorted(GOLDEN))
def test_each_code_renders_its_golden_layer_and_sentence(code):
    layer, member, *api = GOLDEN[code]
    api_template = api[0] if api else member
    assert describe(code, **_fields(member)) == (layer, member.format(**_fields(member)))
    assert describe(code, audience="ui", **_fields(member)) == (layer, member.format(**_fields(member)))
    assert describe(code, audience="api", **_fields(api_template)) == (layer, api_template.format(**_fields(api_template)))


# --- describe(): which values may be shown -------------------------------------------------

UNKNOWN_LAYER, UNKNOWN_SENTENCE = GOLDEN["unknown"][:2]


def test_member_copy_is_the_default_audience_and_never_names_the_model():
    assert describe("alias_misconfigured", model="gpt") == (
        "gateway", "This model is set up incorrectly on this gateway. Ask the owner to check it.")
    assert describe("alias_misconfigured", audience="api", model="gpt") == (
        "gateway", "Model 'gpt' is set up incorrectly on this gateway. Ask the owner to check it.")


@pytest.mark.parametrize("model,shown", [
    ("x", True), ("m" * 200, True), ("", False), ("m" * 201, False), ("line\nbreak", False), (7, False), (None, False),
])
def test_an_api_identifier_is_shown_only_when_short_printable_text(model, shown):
    layer, message = describe("alias_misconfigured", audience="api", model=model)
    assert (layer, message) == (
        ("gateway", f"Model '{model}' is set up incorrectly on this gateway. Ask the owner to check it.")
        if shown else (UNKNOWN_LAYER, UNKNOWN_SENTENCE))


@pytest.mark.parametrize("value,shown", [
    (0, True), (5, True), (-3, True), (2.5, True), (10 ** 12, True), (-(10 ** 12), True), (10 ** 12 + 1, False),
    (-(10 ** 12) - 1, False), (True, False), (float("inf"), False), (float("nan"), False), ("5", False), (None, False),
])
def test_a_number_is_shown_only_when_bounded_and_finite(value, shown):
    result = describe("value_too_large", audience="api", maximum=value)
    assert result == (("request", f"A request value must be at most {value}.") if shown
                      else (UNKNOWN_LAYER, UNKNOWN_SENTENCE))


def test_an_unknown_key_renders_the_unknown_copy():
    assert describe("no_such_code") == (UNKNOWN_LAYER, UNKNOWN_SENTENCE)


# --- project_failure(): recognizing evidence, never echoing it ------------------------------

from gateway.public_failure import PublicFailure, project_failure, project_job  # noqa: E402

CANCELED = "This task was canceled. Make it again if you still want it."
UNKNOWN_UI = "We couldn't tell where this request stopped. Try again."


@pytest.mark.parametrize("message", ["Canceled by requester", "Cancelled by requester"])
def test_a_requester_cancellation_is_recognized_in_both_spellings(message):
    assert project_failure({"message": message}) == {
        "message": CANCELED,
        "details": {"copy_key": "canceled", "layer": "request", "original_message": message, "ui_message": CANCELED},
    }


@pytest.mark.parametrize("message,key,layer,api_message,ui_message,extra", [
    ("CUDA out of memory. Tried to allocate", "out_of_memory", "engine",
     "The engine ran out of memory. Try a smaller request.",
     "The engine couldn't finish this request. Try a smaller request.", {}),
    ("profile 'kid' may not run this request", "profile_denied", "account",
     "This profile may not run this request.",
     "Your account can't run this request. Ask the owner for access.", {}),
    ("must be at most 5", "value_too_large", "request", "A request value must be at most 5.",
     "A request value is too large. Choose a smaller value.", {"maximum": 5}),
    ("needs at least 2.5 units", "value_too_small", "request", "A request value must be at least 2.5.",
     "A request value is too small. Choose a larger value.", {"minimum": 2.5}),
    ("at most -3", "value_too_large", "request", "A request value must be at most -3.",
     "A request value is too large. Choose a smaller value.", {"maximum": -3}),
    ("x must be a string", "input_string", "request", "An input must be a string.",
     "An input needs text. Enter text and try again.", {}),
    ("x must be an integer", "input_integer", "request", "An input must be an integer.",
     "An input needs a whole number. Enter a whole number.", {}),
    ("x must be a number", "input_number", "request", "An input must be a number.",
     "An input needs a number. Enter a number.", {}),
    ("x must be a boolean", "input_boolean", "request", "An input must be a boolean.",
     "An input needs a yes or no choice. Choose one.", {}),
    ("field x must be one of a, b", "input_choice", "request", "An input must match one of the supported choices.",
     "An input has an unsupported choice. Choose a supported value.", {}),
    ("missing required input", "input_required", "request", "A required input is missing.",
     "A required input is missing. Add the missing input.", {}),
    ("saw unknown input zz", "input_unknown", "request", "The request contains an unknown input.",
     "The request has an unknown input. Remove the unknown input.", {}),
])
def test_legacy_error_text_is_reduced_to_its_bounded_fact(message, key, layer, api_message, ui_message, extra):
    assert project_failure({"message": message}) == {
        "message": api_message,
        "details": {"copy_key": key, "layer": layer, "original_message": message, "ui_message": ui_message, **extra},
    }


def test_a_code_picks_the_sentence_and_other_source_fields_stay_apart_from_it():
    projected = project_failure({
        "message": "x", "code": "model_not_deployed", "type": "t", "decision": "d", "extra": 1,
        "details": {"copy_key": "evil", "layer": "evil", "ui_message": "evil", "k": "v"}}, audience="ui")
    ui = "No engine or provider is running this model right now. Choose another model, or ask the owner to turn one on."
    assert projected == {
        "code": "model_not_deployed", "type": "t", "decision": "d", "message": ui,
        "details": {"copy_key": "model_not_deployed", "layer": "engine", "original_message": "x",
                    "ui_message": ui, "extra": 1, "k": "v"},
    }


def test_the_code_of_the_failure_beats_a_message_that_happens_to_match_another_code():
    projected = project_failure({"message": GOLDEN["no_picture"][1], "code": "model_not_deployed"})
    assert projected["details"]["copy_key"] == "no_picture"
    projected = project_failure({"message": GOLDEN["no_picture"][1], "details": {"copy_key": "model_not_deployed"}})
    assert projected["details"]["copy_key"] == "no_picture"


def test_a_code_or_copy_key_chooses_between_two_keys_with_the_same_sentence():
    shared = GOLDEN["invalid_cursor"][1]
    assert shared == GOLDEN["invalid_limit"][1]
    by_code = project_failure({"message": shared, "code": "invalid_limit"})
    assert by_code["details"]["copy_key"] == "invalid_limit"
    by_copy_key = project_failure({"message": shared, "details": {"copy_key": "invalid_limit"}})
    assert by_copy_key["details"]["copy_key"] == "invalid_limit"
    assert project_failure({"message": shared, "code": "invalid_cursor"})["details"]["copy_key"] == "invalid_cursor"
    assert project_failure({"message": shared, "code": "no_such_code"})["details"]["copy_key"] == "invalid_cursor"


def test_only_the_bounded_numeric_details_reach_the_sentence():
    projected = project_failure({"message": "m", "code": "value_too_large",
                                 "details": {"maximum": 9, "model": "gpt", "max_chars": 4}})
    assert projected["message"] == "A request value must be at most 9."
    assert projected["details"]["maximum"] == 9
    large = project_failure({"message": "m", "code": "value_too_large", "details": {"maximum": "9"}})
    assert large["message"] == UNKNOWN_UI  # a string is not a bounded number


def test_diagnostics_that_are_not_a_mapping_are_kept_apart_and_a_missing_message_is_not_recorded():
    projected = project_failure({"message": "x", "details": ["a"]})
    assert projected == {
        "message": UNKNOWN_UI,
        "details": {"copy_key": "unknown", "layer": "unknown", "original_message": "x", "ui_message": UNKNOWN_UI,
                    "upstream_details": ["a"]},
    }
    assert project_failure({"message": None, "code": "nope"}) == {
        "code": "nope", "message": UNKNOWN_UI,
        "details": {"copy_key": "unknown", "layer": "unknown", "ui_message": UNKNOWN_UI},
    }


def test_a_bare_string_error_is_the_message():
    projected = project_failure("boom")
    assert projected["details"]["original_message"] == "boom"
    assert projected["message"] == UNKNOWN_UI


# --- project_job() --------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["canceled", "cancelled"])
def test_a_canceled_job_without_an_error_gets_the_canceled_sentence(status):
    assert project_job({"status": status}) == {
        "status": status, "error": CANCELED,
        "error_details": {"copy_key": "canceled", "layer": "request", "ui_message": CANCELED, "code": "canceled"},
    }


def test_a_failed_job_keeps_its_original_error_in_the_details():
    assert project_job({"status": "failed", "error": "boom"}) == {
        "status": "failed", "error": UNKNOWN_UI,
        "error_details": {"original_message": "boom", "copy_key": "unknown", "layer": "unknown", "ui_message": UNKNOWN_UI},
    }


def test_a_job_that_has_not_failed_is_copied_unchanged():
    view = {"status": "running", "error": "boom"}
    assert project_job(view) == view and project_job(view) is not view


def test_a_failed_job_with_a_structured_error_carries_code_type_and_decision():
    result = project_job({"status": "failed", "error": {"code": "no_picture", "type": "t", "decision": "d"}})
    assert result["error"] == GOLDEN["no_picture"][1]
    assert result["error_details"]["code"] == "no_picture"
    assert (result["error_details"]["type"], result["error_details"]["decision"]) == ("t", "d")


def test_a_failed_job_error_details_mapping_is_projected_as_the_error():
    result = project_job({"status": "failed", "error": None, "error_details": {"k": "v"}})
    assert result["error_details"] == {
        "k": "v", "copy_key": "unknown", "layer": "unknown", "ui_message": UNKNOWN_UI}


# --- PublicFailure ---------------------------------------------------------------------------

def test_a_public_failure_body_carries_code_message_layer_and_details():
    failure = PublicFailure("make_failed", 502, retry_after=3, note="n")
    assert str(failure) == GOLDEN["make_failed"][1]
    assert (failure.code, failure.status_code, failure.retry_after) == ("make_failed", 502, 3)
    assert failure.body() == {"error": {
        "code": "make_failed", "message": GOLDEN["make_failed"][1],
        "details": {"layer": "unknown", "note": "n"}}}


def test_a_public_failure_copy_overrides_the_code_for_the_sentence():
    failure = PublicFailure("some_http_code", 400, copy="no_picture")
    assert (failure.layer, failure.message) == ("model", GOLDEN["no_picture"][1])
    assert failure.retry_after is None


def test_the_original_message_is_recorded_only_when_the_sentence_changed():
    same = project_failure({"message": GOLDEN["no_picture"][1], "code": "no_picture"}, audience="ui")
    assert "original_message" not in same["details"]
    changed = project_failure({"message": "raw upstream text", "code": "no_picture"}, audience="ui")
    assert changed["details"]["original_message"] == "raw upstream text"


def test_a_message_that_equals_a_rendered_sentence_is_recognized_in_both_audiences():
    api = project_failure({"message": "A request value must be at most 7.", "details": {"maximum": 7}})
    assert api["details"]["copy_key"] == "value_too_large" and api["details"]["maximum"] == 7
    member = project_failure({"message": GOLDEN["no_picture"][1]})
    assert member["details"]["copy_key"] == "no_picture"


def test_bounded_numbers_in_details_reach_the_api_sentence_for_a_code():
    projected = project_failure({"message": "m", "code": "value_too_small", "details": {"minimum": 3}})
    assert projected["message"] == "A request value must be at least 3."
