"""Round-5 fixes for request history slice 5a (docs/design/REQUEST_RETENTION.md, slice 5a review fixes).

Each test names the round-5 finding it pins (R5-n). The round-4 fixes are in
``tests/test_request_history_s5a_r4_fixes.py``.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from gateway import request_history
from tests.test_review_retention_s5a_probes import _drive, _kept, member_db  # noqa: F401 - fixture

SECRET = "SYNTHETIC_MARKER"


def _nested(depth: int, leaf: Any) -> Any:
    """``leaf`` inside ``depth`` lists, built without recursion."""
    for _ in range(depth):
        leaf = [leaf]
    return leaf


def _nested_text(depth: int, inner: str) -> str:
    return "[" * depth + inner + "]" * depth


def _message(role: str, content: Any) -> dict:
    return {"messages": [{"role": role, "content": content}]}


def _content_of(kept: dict) -> Any:
    return kept["messages"][0]["content"]


def _tool_call(arguments: str) -> dict:
    return {"messages": [{"role": "assistant", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "fetch", "arguments": arguments}}]}]}


def _arguments_of(kept: dict) -> Any:
    return kept["messages"][0]["tool_calls"][0]["function"]["arguments"]


# ------------------------------------------------------------------ R5-1: JSON carried in a string is cleaned as JSON


def test_r5_1_a_a_tool_message_whose_content_is_json_loses_its_credential_field():
    kept, found = request_history.redact_json(_message("tool", f'{{"password":"{SECRET}"}}'))
    assert SECRET not in json.dumps(kept)
    assert _content_of(kept) == '{"password":"[removed]"}'
    assert found == ["credential_field"]


def test_r5_1_b_a_tool_result_output_that_is_json_loses_its_credential_field():
    body = {"input": [{"type": "function_call_output", "output": f'{{"api_key": "{SECRET}", "ok": 1}}'}]}
    kept, found = request_history.redact_json(body)
    assert SECRET not in json.dumps(kept)
    assert kept["input"][0]["output"] == '{"api_key":"[removed]","ok":1}'
    assert found == ["credential_field"]


def test_r5_1_c_json_in_a_content_part_under_any_field_name_is_cleaned():
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": f'{{"client_secret": "{SECRET}", "lang": "en"}}'}]}]}
    kept, found = request_history.redact_json(body)
    assert SECRET not in json.dumps(kept)
    assert kept["messages"][0]["content"][0]["text"] == '{"client_secret":"[removed]","lang":"en"}'
    assert found == ["credential_field"]


@pytest.mark.parametrize(("text", "cleaned"), [
    (f'  \n\t{{"password": "{SECRET}"}}  \n', '{"password":"[removed]"}'),
    (f'[{{"password": "{SECRET}"}}, 2]', '[{"password":"[removed]"},2]'),
])
def test_r5_1_d_json_text_with_surrounding_whitespace_or_an_array_is_stored_compact(text, cleaned):
    kept, found = request_history.redact_json(_message("tool", text))
    assert _content_of(kept) == cleaned
    assert found == ["credential_field"]


def test_r5_1_e_json_inside_a_json_string_is_cleaned_too():
    outer = json.dumps({"tool_output": json.dumps({"password": SECRET})})
    kept, found = request_history.redact_json(_message("tool", outer))
    assert SECRET not in json.dumps(kept)
    assert json.loads(_content_of(kept)) == {"tool_output": '{"password":"[removed]"}'}
    assert found == ["credential_field"]


@pytest.mark.parametrize("depth", [100, 2000])
def test_r5_1_f_json_in_a_string_nested_past_the_depth_limit_is_omitted(depth):
    kept, found = request_history.redact_json(_message("tool", _nested_text(depth, f'{{"password":"{SECRET}"}}')))
    assert SECRET not in json.dumps(kept)
    assert _content_of(kept) == {"omitted": "json_depth"}
    assert found == ["json_depth"]


def test_r5_1_g_json_in_a_string_nested_under_the_limit_is_cleaned_as_json():
    kept, found = request_history.redact_json(_message("tool", _nested_text(30, f'{{"password": "{SECRET}", "ok": 1}}')))
    assert SECRET not in json.dumps(kept)
    assert json.loads(_content_of(kept)) == _nested(30, {"password": "[removed]", "ok": 1})
    assert found == ["credential_field"]


def test_r5_1_h_tool_arguments_are_stored_compact():
    kept, _ = request_history.redact_json(_tool_call(f'{{"password": "{SECRET}", "ok": 1}}'))
    assert _arguments_of(kept) == '{"password":"[removed]","ok":1}'


def test_r5_1_i_prose_that_starts_with_a_bracket_but_does_not_parse_is_cleaned_as_text():
    kept, found = request_history.redact_json(_message("user", "[note] see https://u:SYNTHETIC_MARKER@h.test/x now"))
    assert SECRET not in json.dumps(kept)
    assert _content_of(kept) == "[note] see https://[removed]@h.test/x now"
    assert found == ["url_parts_removed"]


def test_r5_1_j_a_stored_tool_message_with_json_content_keeps_no_credential(member_db):
    db, media, person = member_db
    body = {"model": "m", "messages": [{"role": "tool", "content": f'{{"password":"{SECRET}"}}'}]}
    _drive(db, media, person, chunks=[json.dumps(body).encode()], content_type="application/json")
    rows = _kept(media)
    assert len(rows) == 1
    assert SECRET not in rows[0] and r'{\"password\":\"[removed]\"}' in rows[0]
