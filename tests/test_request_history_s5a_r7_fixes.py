"""Round-7 fixes for request history slice 5a (docs/design/REQUEST_RETENTION.md, slice 5a review fixes).

Each test names the round-7 finding it pins (R7-n). R7-1 replaces the URL rule of rounds 4 and 6. The tests that pinned
the old output are updated in place, in their round-4, round-6 and review files.
"""
from __future__ import annotations

import json
import time

import pytest

from gateway import request_history
from tests.test_request_history_s5a_r6_fixes import _unwrapped

SECRET = "SYNTHETIC_MARKER"


def _url(user_info: str, rest: str) -> str:
    """A URL with user info, built from pieces so that no literal ``user:pass@host`` sits in this file."""
    return "https://" + user_info + "@" + rest


def _escaped_slashes(text: str) -> str:
    """A JSON string literal of ``text`` in which each slash is written as ``\\u002f``, so the encoded text has no ``://``."""
    return json.dumps(text).replace("/", "\\u002f")


# ------------------------------------------------------------------ R7-1: one rule for every URL


def test_r7_1_a_two_urls_joined_by_a_comma_lose_their_user_info():
    joined = f"{_url('u:p', 'a.test/x')},{_url('v:' + SECRET, 'b.test/y')}"
    whole, found = request_history.redact_json({"dsn": joined})
    assert SECRET not in json.dumps(whole)
    assert whole == {"dsn": "[url removed][url removed]"}
    assert found == ["url_parts_removed"]
    inside, _ = request_history.redact_json({"content": f"see {joined} now"})
    assert SECRET not in json.dumps(inside)
    assert inside == {"content": "see [url removed][url removed] now"}


def test_r7_1_b_each_scheme_starts_a_new_url_and_the_text_between_is_kept():
    text = "see https://h.test/a,https://h.test/b now"
    kept, found = request_history.redact_json({"content": text})
    assert kept == {"content": text}
    assert found == []


@pytest.mark.parametrize(("text", "kept_as"), [
    ('see "https://h.test/a" now', 'see "https://h.test/a" now'),
    ("see (https://h.test/a) now", "see (https://h.test/a) now"),
    ("see <https://h.test/a> now", "see <https://h.test/a> now"),
    ('see "https://h.test/a?x=1" now', 'see "https://h.test/a?[removed] now'),
])
def test_r7_1_c_quotes_and_brackets_beside_a_url_keep_what_is_not_removed(text, kept_as):
    kept, _ = request_history.redact_json({"content": text})
    assert kept == {"content": kept_as}


@pytest.mark.parametrize("delimiter", ['"', "'", "`", "<", ">"])
def test_r7_1_d_a_quote_or_bracket_inside_a_query_value_does_not_end_the_url(delimiter):
    """Ending the URL there would leave the rest of the query value as plain text, and that text would be kept."""
    kept, _ = request_history.redact_json({
        "content": f"see https://h.test/a?token=abc{delimiter}{SECRET}&x=1 now",
        "u": f"https://h.test/a?token=abc{delimiter}{SECRET}&x=1",
    })
    assert SECRET not in json.dumps(kept)
    assert kept == {"content": "see https://h.test/a?[removed] now", "u": "https://h.test/a?[removed]"}


@pytest.mark.parametrize("delimiter", ['"', "'", "`", "<", ">"])
def test_r7_1_e_a_quote_or_bracket_inside_user_info_removes_the_whole_url(delimiter):
    url = _url(f"user:{delimiter}{SECRET}{delimiter}", "db/app")
    kept, found = request_history.redact_json({"content": f"connect {url} now", "dsn": url})
    assert SECRET not in json.dumps(kept)
    assert kept == {"content": "connect [url removed] now", "dsn": "[url removed]"}
    assert found == ["url_parts_removed"]


def test_r7_1_f_mailto_has_no_scheme_separator_so_this_rule_does_not_touch_it():
    text = "write mailto:someone@example.test or https://h.test/x now"
    kept, found = request_history.redact_json({"content": text, "u": "mailto:someone@example.test"})
    assert kept == {"content": text, "u": "mailto:someone@example.test"}
    assert found == []


@pytest.mark.parametrize(("text", "kept_as"), [
    (f"https://h.test/a%40{SECRET}", "[url removed]"),
    (f"see https://h.test/a%40{SECRET} now", "see [url removed] now"),
])
def test_r7_1_g_a_percent_encoded_at_sign_removes_the_whole_url(text, kept_as):
    kept, found = request_history.redact_json({"content": text})
    assert SECRET not in json.dumps(kept)
    assert kept == {"content": kept_as}
    assert found == ["url_parts_removed"]


def test_r7_1_h_a_query_and_a_fragment_keep_only_their_markers():
    kept, found = request_history.redact_json({"u": "https://h.test:8443/a/b?x=1&y=2#frag"})
    assert kept == {"u": "https://h.test:8443/a/b?[removed]#[removed]"}
    assert found == ["url_parts_removed"]


@pytest.mark.parametrize("url", [
    "https://h.test:99999/x",
    "https://h.test:notaport/x",
    "https://[::1/x",
    "https:///x",
    "https://ünicode.test/x",
])
def test_r7_1_i_a_url_that_cannot_be_split_into_scheme_and_host_is_replaced_whole(url):
    kept, found = request_history.redact_json({"u": url, "content": f"see {url} now"})
    assert kept == {"u": "[url removed]", "content": "see [url removed] now"}
    assert found == ["url_parts_removed"]


@pytest.mark.parametrize(("url", "kept_as"), [
    ("https://h.test/api_key/abc", "https://h.test/[removed]/abc"),
    ("https://h.test/a/password", "https://h.test/a/[removed]"),
])
def test_r7_1_j_a_path_segment_named_as_a_credential_is_replaced(url, kept_as):
    kept, found = request_history.redact_json({"u": url})
    assert kept == {"u": kept_as}
    assert found == ["url_parts_removed"]


def test_r7_1_k_punctuation_after_a_query_goes_with_the_query_and_after_a_path_is_kept():
    kept, _ = request_history.redact_json({"a": "see https://h.test/a?x=1, then", "b": "see https://h.test/a."})
    assert kept == {"a": "see https://h.test/a?[removed] then", "b": "see https://h.test/a."}


def test_r7_1_l_a_scheme_inside_a_query_starts_a_new_url():
    kept, _ = request_history.redact_json({"u": "https://h.test/a?next=" + _url("u:" + SECRET, "x.test/")})
    assert SECRET not in json.dumps(kept)
    assert kept == {"u": "https://h.test/a?[removed][url removed]"}


@pytest.mark.parametrize("text", [("a" * 100_000) + " " + ("a" * 100_000), ("a1" * 100_000) + " x"])
def test_r7_1_m_a_long_run_of_letters_is_read_in_linear_time(text):
    """Each character is read once. A scheme search at every letter took about four seconds for 80 KB of letters."""
    started = time.perf_counter()
    kept, found = request_history.redact_json({"content": text})
    assert time.perf_counter() - started < 5
    assert kept == {"content": text}
    assert found == []


def test_r7_1_n_many_schemes_in_one_value_are_each_replaced_in_linear_time():
    started = time.perf_counter()
    kept, _ = request_history.redact_json({"content": "a://" * 20_000})
    assert time.perf_counter() - started < 5
    assert kept == {"content": "[url removed]" * 20_000}


# ------------------------------------------------------------------ R7-2: a literal's decoded text is cleaned as text


@pytest.mark.parametrize("levels", [0, 1, 2])
def test_r7_2_a_a_url_with_escaped_slashes_in_a_literal_is_removed_at_every_level(levels):
    """The encoded text has no ``://``, so only the decoded text shows the URL. Each wrapper is a literal too."""
    value = _escaped_slashes(f"see {_url('user:' + SECRET, 'h.test/x')} now")
    for _ in range(levels):
        value = json.dumps(value)
    kept, found = request_history.redact_json({"x": value})
    assert SECRET not in json.dumps(kept)
    assert _unwrapped(kept["x"], levels) == "see [url removed] now"
    assert found == ["url_parts_removed"]


def test_r7_2_b_a_literal_holding_a_data_url_is_omitted_as_media():
    kept, found = request_history.redact_json({"x": json.dumps("data:image/png;base64," + "A" * 100)})
    assert kept == {"x": {"omitted": "media", "media_type": "image/png", "bytes": 75}}
    assert found == ["media"]


# ------------------------------------------------------------------ R7-3: a string is checked against the budget before it is parsed


def test_r7_3_a_a_string_the_budget_cannot_hold_is_not_parsed(monkeypatch):
    parsed: list[str] = []
    real_loads = json.loads

    def record(text, *args, **kwargs):
        parsed.append(text)
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(json, "loads", record)
    text = json.dumps({"password": SECRET, "pad": "x" * 200})
    kept, found = request_history.redact_json({"x": text}, budget=request_history.WorkBudget(max_bytes=64))
    assert kept == {"x": {"omitted": "json_budget"}}
    assert found == ["json_budget"]
    assert text not in parsed
    assert SECRET not in json.dumps(kept)


def test_r7_3_b_a_string_that_fits_is_parsed_and_its_length_is_spent():
    budget = request_history.WorkBudget(max_bytes=10)
    kept, found = request_history.redact_json({"x": '{"a":1}', "y": '{"b":2}'}, budget=budget)
    assert kept == {"x": '{"a":1}', "y": {"omitted": "json_budget"}}
    assert found == ["json_budget"]
    assert budget.decoded_bytes == len('{"a":1}')


def test_r7_3_c_bracketed_prose_longer_than_the_budget_left_is_omitted_and_otherwise_costs_nothing():
    """A string is checked before it is parsed, and prose that starts like JSON cannot be told apart before that. Prose
    that fits is kept as text and spends nothing."""
    prose = "[note] " + "x" * 50
    kept, found = request_history.redact_json({"content": prose}, budget=request_history.WorkBudget(max_bytes=20))
    assert kept == {"content": {"omitted": "json_budget"}}
    assert found == ["json_budget"]
    budget = request_history.WorkBudget(max_bytes=1000)
    kept, found = request_history.redact_json({"content": prose}, budget=budget)
    assert kept == {"content": prose}
    assert found == []
    assert budget.decoded_bytes == 0
