from __future__ import annotations

import re

import pytest

from gateway import public_failure
from gateway.public_failure import LAYERS, PublicFailure, describe

# Words a member never reads (docs/design/VOICE.md); they belong in details.
_BANNED = re.compile(r"\b(intent|asset|backend|payload|JSON|upstream|profile|alias|cursor|idempotency|"
                     r"exception|manifest|http)\b|[a-z]+_[a-z_]+|\(\w+Error\)", re.IGNORECASE)


@pytest.mark.parametrize("key", sorted(public_failure._COPY))
def test_every_failure_names_a_layer_and_ends_with_an_action(key: str) -> None:
    values = {"maximum": 3, "max_chars": 32000, "max_images": 10, "input": "image", "model": "pixel"}
    layer, message = describe(key, **values)
    assert layer in LAYERS
    assert not _BANNED.search(message), message
    assert "{" not in message
    sentences = [part for part in re.split(r"(?<=\.) ", message) if part]
    assert len(sentences) >= 2, "a fact, then one next action"


def test_unlisted_failure_is_reported_as_unknown_not_invented() -> None:
    assert describe("never_seen") == describe(public_failure.UNKNOWN)
    assert describe("never_seen")[0] == "unknown"


def test_failure_body_keeps_code_and_moves_diagnostics_to_details() -> None:
    failure = PublicFailure("unsupported_field", 400, field="steps")
    assert failure.status_code == 400
    assert failure.body() == {"error": {
        "code": "unsupported_field",
        "message": "This request has a setting the gateway doesn't accept. Refresh the page and try again.",
        "details": {"layer": "request", "field": "steps"},
    }}


def test_copy_key_separates_causes_that_share_a_code() -> None:
    failure = PublicFailure("source_not_ready", 409, copy="source_missing", job_status="failed")
    assert failure.code == "source_not_ready"
    assert failure.message.startswith("That task didn't produce a picture.")


@pytest.mark.parametrize("key", ["input_file_unavailable", "input_not_accepted",
                                  "input_set_by_gateway", "input_mapping_invalid",
                                  "alias_misconfigured", "model_not_deployed"])
def test_only_api_copy_names_the_clients_input_or_model(key):
    values = {"input": "input_private_id", "model": "model_private_id"}
    layer, ui = describe(key, **values)
    assert layer in LAYERS
    assert "private_id" not in ui
    # An API client needs to know which of its inputs or models failed.
    api_layer, api = describe(key, audience="api", **values)
    assert api_layer == layer
    assert re.search(r"'(input|model)_private_id'", api)
    for bad in ("", "x" * 201, "a\nb", 7, None):
        assert describe(key, audience="api", input=bad, model=bad) == describe("unknown")


@pytest.mark.parametrize("value", ["private_id", {}, [], None, True, float("inf"), 10 ** 1000])
def test_untrusted_copy_values_cannot_leak_or_raise(value):
    assert describe("too_many_jobs", maximum=value) == describe("unknown")
    projected = public_failure.project_failure(
        {"code": "too_many_jobs", "details": {"maximum": value, "audience": "api",
                                             "layer": "private_id"}})
    assert projected["message"] == describe("unknown")[1]
    assert projected["details"]["layer"] == "unknown"


@pytest.mark.parametrize("error", [
    {"code": "private_error"},
    {"message": "ValidationError: schema_text internal_id", "details": ["bad-shape"]},
    {"message": None},
])
def test_unknown_diagnostics_are_nested(error):
    projected = public_failure.project_failure(error)
    assert projected["message"] == describe("unknown")[1]
    assert "ValidationError" not in projected["message"]
    if "code" in error:
        assert projected["code"] == error["code"]
    if error.get("message"):
        assert projected["details"]["original_message"] == error["message"]
        assert projected["details"]["upstream_details"] == ["bad-shape"]


@pytest.mark.parametrize("person", [None, "self", "person_private_id"])
def test_member_self_scope_is_allowed(monkeypatch, tmp_path, person):
    from starlette.requests import Request
    from gateway import me_media
    actor = me_media._Actor(user={"id": "person_private_id"}, role="member")
    request = Request({"type": "http", "query_string": (f"person={person}" if person else "").encode()})
    monkeypatch.setattr(me_media, "_owned_app_ids", lambda *args: ["app_owned"])
    monkeypatch.setattr(me_media, "_member_media_scope", lambda user_id, *args, **kwargs: user_id)
    assert me_media._media_scope(request, actor, tmp_path) == "person_private_id"
    assert me_media._usage_app_scope(request, actor, tmp_path) == (["app_owned"], "me", ["person_private_id"])


@pytest.mark.parametrize("person", ["all", "another_person"])
def test_other_people_still_require_owner(tmp_path, person):
    from starlette.requests import Request
    from gateway import me_media
    actor = me_media._Actor(user={"id": "person_private_id"}, role="member")
    request = Request({"type": "http", "query_string": f"person={person}".encode()})
    for scope in (me_media._media_scope, me_media._usage_app_scope):
        with pytest.raises(PublicFailure) as caught:
            scope(request, actor, tmp_path)
        assert caught.value.code == "owner_required"
        assert caught.value.status_code == 403


@pytest.mark.parametrize("candidates,expected", [([], "alias_misconfigured"),
                                               (["missing"], "alias_misconfigured"),
                                               (["missing", "defined"], "model_not_deployed")])
def test_alias_candidates_distinguish_configuration_from_availability(candidates, expected):
    from gateway.stable_aliases import AliasUnavailableError, resolve_alias, validate_aliases
    config = {"speech": {"stable": {"tts": candidates},
                        "engines": {"defined": {"service": "tts", "model": "private_model_id"}}}}
    with pytest.raises(AliasUnavailableError) as caught:
        resolve_alias(config, "speech", "tts", engine_available=lambda *args: False)
    assert caught.value.code == expected
    assert "private_model_id" not in str(caught.value)
    if not candidates:
        assert any("must not be empty" in problem for problem in validate_aliases(config))


def test_make_client_view_projects_terminal_errors(monkeypatch):
    from gateway import me_make
    monkeypatch.setattr(me_make.media_jobs, "for_audience", lambda job, audience: dict(job))
    view = me_make._client_view({"status": "failed",
                                 "error": "RuntimeError: out of memory on engine_private_id"})
    assert view["error"] == describe("out_of_memory")[1]
    assert view["error_details"]["original_message"].startswith("RuntimeError")


def test_projection_is_idempotent_and_keeps_the_limit():
    original = {"message": "input 'private_id' must be at most 20"}
    api = public_failure.project_failure(original)
    ui = public_failure.project_failure(api, audience="ui")
    restored = public_failure.project_failure(ui)
    assert "at most 20" in restored["message"]
    assert restored["details"]["original_message"] == original["message"]


@pytest.mark.parametrize("message,expected", [
    ("input 'private_id' must be at most 20.", "at most 20"),
    ("input 'private_id' must be at least 0.5.", "at least 0.5"),
    ("input 'private_id' must be a string", "must be a string"),
    ("input 'private_id' must be an integer", "must be an integer"),
    ("input 'private_id' must be a number", "must be a number"),
    ("input 'private_id' must be a boolean", "must be a boolean"),
    ("missing required input 'private_id'", "required input is missing"),
    ("unknown input 'private_id'", "unknown input"),
])
def test_specific_safe_input_reason_survives(message, expected):
    projected = public_failure.project_failure({"message": message})
    assert expected in projected["message"]
    assert "private_id" not in projected["message"]


def test_shared_ui_copy_does_not_erase_specific_api_reason():
    api = public_failure.project_failure({"code": "file_base64_invalid"})
    ui = public_failure.project_failure(api, audience="ui")
    restored = public_failure.project_failure(ui)
    assert restored["message"] == describe("file_base64_invalid", audience="api")[1]
