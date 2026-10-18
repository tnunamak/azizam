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
