"""The native lane's rules as pure data: allow-list matching, validation, and reply refusals."""

from __future__ import annotations

import pytest

from gateway import native_lane

RECORD = {"native": {"allow": [
    {"method": "post", "path": "/v1/chat/completions", "class": "generate"},
    {"method": "GET", "path": "/v1/models/{model}", "class": "generate"},
    {"method": "DELETE", "path": "/v1/models/{model}", "class": "control"},
]}}


def test_a_record_with_no_native_block_allows_nothing():
    policy = native_lane.policy_for({})
    assert policy.entry_for("POST", "/v1/chat/completions") is None
    assert native_lane.policy_for({"native": {"allow": "yes"}}).allow == ()


def test_an_entry_matches_its_method_and_a_template_segment():
    policy = native_lane.policy_for(RECORD)
    assert policy.entry_for("POST", "/v1/chat/completions").klass == native_lane.GENERATE
    assert policy.entry_for("GET", "/v1/models/a").klass == native_lane.GENERATE
    assert policy.entry_for("DELETE", "/v1/models/a").klass == native_lane.CONTROL
    assert policy.entry_for("GET", "/v1/chat/completions") is None
    assert policy.entry_for("GET", "/v1/models/a/b") is None
    assert policy.entry_for("GET", "/v1/models") is None


def test_a_control_entry_wins_when_two_entries_name_the_same_call():
    policy = native_lane.policy_for({"native": {"allow": [
        {"method": "POST", "path": "/x/{a}", "class": "generate"},
        {"method": "POST", "path": "/x/load", "class": "control"},
    ]}})
    assert policy.entry_for("POST", "/x/load").klass == native_lane.CONTROL
    assert policy.entry_for("POST", "/x/other").klass == native_lane.GENERATE


@pytest.mark.parametrize("block", [
    {"allow": [{"method": "FETCH", "path": "/a", "class": "generate"}]},
    {"allow": [{"method": "GET", "path": "a", "class": "generate"}]},
    {"allow": [{"method": "GET", "path": "/a/*", "class": "generate"}]},
    {"allow": [{"method": "GET", "path": "/a", "class": "admin"}]},
    {"allow": [{"method": "GET", "path": "/a"}]},
    {"max_request_bytes": 0},
    {"max_response_bytes": "big"},
    ["not", "a", "mapping"],
])
def test_a_native_block_that_cannot_be_read_is_refused_at_validation(block):
    with pytest.raises(ValueError):
        native_lane.validate_native_block(block)
    assert native_lane.policy_for({"native": block}).allow == ()


def test_the_caps_default_and_can_be_set():
    assert native_lane.policy_for({}).max_request_bytes == native_lane.DEFAULT_MAX_REQUEST_BYTES
    policy = native_lane.policy_for({"native": {"max_request_bytes": 10, "max_response_bytes": 20}})
    assert (policy.max_request_bytes, policy.max_response_bytes) == (10, 20)


def test_a_reply_head_is_refused_for_a_redirect_or_a_declared_size_over_the_cap():
    policy = native_lane.policy_for({"native": {"max_response_bytes": 100}})
    assert native_lane.reply_refusal(policy, 200, "100", "g") is None
    assert native_lane.reply_refusal(policy, 200, None, "g") is None
    assert native_lane.reply_refusal(policy, 200, "101", "g")[1] == "native_response_too_large"
    for status in (301, 302, 303, 307, 308):
        assert native_lane.reply_refusal(policy, status, None, "g")[1] == "native_redirect_refused"


# --- contract section 6, tightened: query strings, methods, case, credentials --------------------

def _entry(**fields):
    return native_lane.AllowEntry(
        fields.get("method", "GET"), fields.get("path", "/v1/models"), fields.get("klass", "generate"),
        tuple(fields.get("query", ())),
    )


def test_a_template_with_no_query_keys_refuses_any_query_string():
    entry = _entry()
    assert native_lane.query_problem(entry, "") is None
    assert native_lane.query_problem(entry, "limit=1")[0] == "native_query_not_allowed"


def test_a_template_allows_only_the_query_keys_it_declares():
    entry = _entry(query=["limit", "after"])
    assert native_lane.query_problem(entry, "limit=5&after=abc") is None
    assert native_lane.query_problem(entry, "limit=5&debug=1")[0] == "native_query_not_allowed"
    assert native_lane.query_problem(entry, "Limit=5")[0] == "native_query_not_allowed"
    assert native_lane.query_problem(entry, "lim%69t=5") is None  # the key is read after decoding
    assert native_lane.query_problem(entry, "limit=5&limit=6")[0] == "native_query_not_allowed"
    assert native_lane.query_problem(entry, "limit") is None  # a key with no value is a key


@pytest.mark.parametrize("value", [
    "/etc/passwd", "../secret", "a/../b", "~/keys", "C:\\Users\\x", "c:/x", "\\\\server\\share", "file:///etc/hosts",
    "%2Fetc%2Fpasswd", "%2e%2e%2fx", "a\\b", "x\x00y", "./here", "model/name",
])
def test_a_generate_entry_refuses_a_file_path_like_query_value(value):
    from urllib.parse import urlencode
    entry = _entry(query=["model"])
    assert native_lane.query_problem(entry, urlencode({"model": value}))[0] == "native_query_value_refused"
    assert native_lane.query_problem(entry, f"model={value}")[0] == "native_query_value_refused"


def test_a_generate_entry_passes_an_ordinary_query_value():
    entry = _entry(query=["model", "stream"])
    for value in ("demo-chat", "qwen3.8:27b", "1.5", "true", "a b", "ünï"):
        assert native_lane.query_problem(entry, f"model={value}&stream=true") is None, value


def test_a_control_entry_may_pass_a_path_like_query_value():
    entry = _entry(klass="control", query=["file"])
    assert native_lane.query_problem(entry, "file=/models/a.gguf") is None
    assert native_lane.query_problem(entry, "other=1")[0] == "native_query_not_allowed"


def test_the_query_that_goes_out_is_rebuilt_from_the_checked_pairs():
    entry = _entry(query=["limit", "q"])
    assert native_lane.forwarded_query(entry, "limit=5&q=a%20b") == "limit=5&q=a+b"
    assert native_lane.forwarded_query(entry, "") == ""


@pytest.mark.parametrize("query", ["limit", [1], "limit", [""], ["a b"], ["a=b"], {"x": 1}])
def test_a_query_declaration_that_is_not_a_list_of_key_names_is_refused(query):
    with pytest.raises(ValueError, match="query"):
        native_lane.validate_native_block({"allow": [{"method": "GET", "path": "/a", "class": "generate", "query": query}]})


def test_the_query_keys_of_an_entry_read_into_the_policy():
    policy = native_lane.policy_for({"native": {"allow": [
        {"method": "GET", "path": "/v1/models", "class": "generate", "query": ["limit"]},
    ]}})
    assert policy.entry_for("GET", "/v1/models").query == ("limit",)


def test_head_is_not_an_allowed_method_because_the_route_does_not_serve_it():
    with pytest.raises(ValueError, match="method"):
        native_lane.validate_native_block({"allow": [{"method": "HEAD", "path": "/a", "class": "generate"}]})
    assert "HEAD" not in native_lane.METHODS


def test_the_allowed_methods_are_exactly_the_methods_the_native_route_serves():
    import proxy
    served = proxy.FRONTEND_TRANSLATORS.operation("native", "engine.native").methods
    assert native_lane.METHODS == served


CASE_ENTRIES = {"native": {"allow": [
    {"method": "POST", "path": "/v1/Models/Load", "class": "control"},
    {"method": "POST", "path": "/v1/models/{name}", "class": "generate"},
]}}


def test_a_request_that_matches_a_control_entry_only_by_letter_case_is_ambiguous_and_refused():
    policy = native_lane.policy_for(CASE_ENTRIES)
    exact = policy.resolve("POST", "/v1/Models/Load")
    assert exact.entry.klass == native_lane.CONTROL and not exact.ambiguous
    folded = policy.resolve("POST", "/v1/models/load")  # the generate entry matches it, the control entry by case only
    assert folded.ambiguous
    assert policy.resolve("POST", "/v1/models/other").entry.klass == native_lane.GENERATE
    assert not policy.resolve("POST", "/v1/models/other").ambiguous


def test_an_engine_declared_case_insensitive_matches_paths_case_folded_and_control_wins():
    policy = native_lane.policy_for({"native": {**CASE_ENTRIES["native"], "case_insensitive": True}})
    for path in ("/v1/Models/Load", "/v1/models/load", "/V1/MODELS/LOAD"):
        resolved = policy.resolve("POST", path)
        assert resolved.entry.klass == native_lane.CONTROL, path
        assert not resolved.ambiguous
    assert policy.resolve("POST", "/V1/Models/Other").entry.klass == native_lane.GENERATE


def test_case_insensitive_must_be_a_boolean():
    with pytest.raises(ValueError, match="case_insensitive"):
        native_lane.validate_native_block({"case_insensitive": "yes"})


def test_an_unmatched_path_resolves_to_nothing_and_is_not_ambiguous():
    resolved = native_lane.policy_for(CASE_ENTRIES).resolve("GET", "/v1/Models/Load")
    assert resolved.entry is None and not resolved.ambiguous


def test_a_class_credential_is_read_from_the_connection_block_per_class():
    record = {"connection": {"credentials": {
        "inference": {"secret_env": "A_KEY", "header": "x-api-key", "origin": "http://h:1"},
        "control": {"secret_env": "B_KEY", "header": "Authorization", "scheme": "Bearer", "origin": "http://h:1"},
    }}}
    inference = native_lane.credential_for(record, native_lane.GENERATE)
    control = native_lane.credential_for(record, native_lane.CONTROL)
    assert (inference.header, inference.scheme, inference.secret_env) == ("x-api-key", "", "A_KEY")
    assert (control.header, control.scheme, control.secret_env) == ("Authorization", "Bearer", "B_KEY")
    assert native_lane.credential_for({}, native_lane.GENERATE) is None
    assert native_lane.credential_for({"connection": {"credentials": {"control": None}}}, native_lane.CONTROL) is None


def test_a_credential_is_sent_only_to_the_origin_it_is_bound_to():
    record = {"connection": {"credentials": {"inference": {"secret_env": "A_KEY", "origin": "http://h:1"}}}}
    credential = native_lane.credential_for(record, native_lane.GENERATE)
    assert credential.bound_to("http://h:1/v1")
    assert credential.bound_to("HTTP://H:1")
    assert not credential.bound_to("http://h:2")
    assert not credential.bound_to("https://h:1")
    unbound = native_lane.credential_for(
        {"connection": {"credentials": {"inference": {"secret_env": "A_KEY"}}}}, native_lane.GENERATE)
    assert not unbound.bound_to("http://h:1")


@pytest.mark.parametrize("credentials", [
    {"inference": "key"},
    {"inference": {"header": "x-api-key"}},
    {"inference": {"secret_env": "A", "header": "bad header"}},
    {"inference": {"secret_env": "A", "scheme": "Bea rer"}},
    {"other": {"secret_env": "A"}},
    {"inference": {"secret_env": "A", "origin": "not a url"}},
])
def test_a_credential_block_that_cannot_be_read_is_refused_at_validation(credentials):
    with pytest.raises(ValueError, match="credential"):
        native_lane.validate_connection({"credentials": credentials})


def test_a_connection_without_credentials_is_valid_and_so_is_a_null_control_credential():
    native_lane.validate_connection(None)
    native_lane.validate_connection({"credentials": {"inference": None, "control": None}})
    native_lane.validate_connection({"credentials": {"inference": {"secret_env": "A", "origin": "http://h:1"}}})


def test_the_response_headers_that_belong_to_the_engine_are_not_relayed():
    kept = native_lane.relayed_headers({
        "content-type": "application/json", "x-engine": "1", "Set-Cookie": "a=b", "set-cookie2": "x",
        "Strict-Transport-Security": "max-age=1", "Access-Control-Allow-Origin": "*",
        "access-control-allow-credentials": "true", "WWW-Authenticate": "Basic", "alt-svc": "h3=:443",
        "content-security-policy": "default-src none", "clear-site-data": "*", "public-key-pins": "x",
        "transfer-encoding": "chunked", "content-length": "5", "connection": "close", "location": "/x",
        "server": "engine/1", "x-powered-by": "x",
    })
    assert kept == {"content-type": "application/json", "x-engine": "1"}
