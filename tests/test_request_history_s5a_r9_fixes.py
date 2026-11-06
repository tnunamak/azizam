"""Round-9 fixes for request history slice 5a (docs/design/REQUEST_RETENTION.md, slice 5a review fixes).

Each test names the round-9 finding it pins (R9-n). R9-1 is the token rule for a scheme: a scheme begins a new
token only at the start of the text, after whitespace, or after one of ``,;()[]{}<>"'`` and backtick. A ``://``
that begins no token makes its whole token ``[url removed]``. R9-2 sets trailing prose punctuation aside before a
path segment is checked for a token, and keeps it. R9-3 reads each path segment after percent-decoding. The secrets
and tokens here are built from pieces at run time, so no literal token or credential URL sits in this file.
"""
from __future__ import annotations

import json

import pytest

from gateway import request_history

HOST = "https://api.example.test"


def _run(pattern: str, length: int) -> str:
    """``pattern`` repeated, then cut to ``length`` characters."""
    return (pattern * length)[:length]


def _url(user_info: str, rest: str) -> str:
    """A URL with user info, built from pieces so that no literal ``user:pass@host`` sits in this file."""
    return "https://" + user_info + "@" + rest


MIXED = _run("aB3", 20)  # 20 characters with a letter and a digit: a token by R8-1
BOT_TOKEN = "bot" + _run("1234567890", 9) + ":" + _run("aB3_-", 35)
WEBHOOK_TOKEN = _run("Ab1", 32)
UUID = "-".join([_run("1a", 8), _run("2b", 4), _run("3c", 4), _run("d4", 4), _run("5e", 12)])


def _fully_encoded(token: str) -> str:
    """Every character of ``token`` as a percent-escape, so no plain character of it is left in the URL."""
    return "".join(f"%{ord(char):02X}" for char in token)


# ------------------------------------------------------------------ R9-1: a scheme starts a token after a delimiter


@pytest.mark.parametrize(("text", "kept_as"), [
    (f"https://a.test?token={MIXED}://b.test", "[url removed]"),
    (f"https://a.test/x/{MIXED}://b.test", "[url removed]"),
    (f"https://a.test/x#{MIXED}://b.test", "[url removed]"),
    (f"token={MIXED}://b.test", "[url removed]"),
    (f"see https://a.test?token={MIXED}://b.test now", "see [url removed] now"),
    (f"see https://a.test?token={MIXED}://b.test https://c.test/ok now", "see [url removed] https://c.test/ok now"),
])
def test_r9_1_a_a_value_before_a_scheme_is_not_read_as_a_scheme_and_its_token_is_replaced(text, kept_as):
    kept, found = request_history.redact_json({"content": text})
    assert MIXED not in json.dumps(kept)
    assert kept == {"content": kept_as}
    assert found == ["url_parts_removed"]


@pytest.mark.parametrize("delimiter", [",", ";", "(", ")", "[", "]", "{", "}", "<", ">", '"', "'", "`"])
def test_r9_1_b_each_delimiter_before_a_scheme_starts_a_new_url(delimiter):
    text = f"see {HOST}/a{delimiter}https://h.test/b now"
    kept, found = request_history.redact_json({"content": text})
    assert kept == {"content": text}
    assert found == []


@pytest.mark.parametrize("character", ["=", "/", ":", "*", "|", "&", "#", "%"])
def test_r9_1_c_a_scheme_after_any_other_character_does_not_start_a_url(character):
    kept, found = request_history.redact_json({"content": f"see {HOST}/a{character}https://h.test/b now"})
    assert kept == {"content": "see [url removed] now"}
    assert found == ["url_parts_removed"]


def test_r9_1_d_a_scheme_after_a_comma_in_a_query_starts_a_new_url():
    kept, found = request_history.redact_json({"u": "https://a.test/x?next=abc,https://b.test/y"})
    assert kept == {"u": "https://a.test/x?[removed]https://b.test/y"}
    assert found == ["url_parts_removed"]


# ------------------------------------------------------------------ R9-2: prose punctuation after a token segment


@pytest.mark.parametrize("punctuation", ['"', "'", "`", ")", "]", "}", ">", ".", ",", ";", "!"])
def test_r9_2_a_a_token_before_prose_punctuation_is_replaced_and_the_punctuation_is_kept(punctuation):
    kept, found = request_history.redact_json({"content": f"see {HOST}/items/{UUID}{punctuation} now"})
    assert kept == {"content": f"see {HOST}/items/[removed]{punctuation} now"}
    assert found == ["url_parts_removed"]


def test_r9_2_b_a_question_mark_after_a_token_is_the_query_start_so_it_is_not_kept():
    """urlsplit reads ``?`` as the start of a query, so an empty query leaves no ``?`` to keep."""
    kept, found = request_history.redact_json({"content": f"see {HOST}/items/{UUID}? now"})
    assert kept == {"content": f"see {HOST}/items/[removed] now"}
    assert found == ["url_parts_removed"]


@pytest.mark.parametrize(("tail", "kept_as"), [
    (".) now", "[removed].) now"),
    (".! now", "[removed].! now"),
    (".?x=1 now", "[removed].?[removed] now"),
])
def test_r9_2_c_a_run_of_punctuation_after_a_token_is_kept_and_a_query_follows_it(tail, kept_as):
    kept, found = request_history.redact_json({"content": f"see {HOST}/items/{UUID}{tail}"})
    assert kept == {"content": f"see {HOST}/items/{kept_as}"}
    assert found == ["url_parts_removed"]


def test_r9_2_d_a_dot_inside_a_segment_keeps_it_out_of_the_token_check():
    """Only trailing punctuation is set aside. A dot inside the segment makes the core a non-token (R8-1)."""
    segment = _run("Ab1", 20) + ".txt"
    kept, found = request_history.redact_json({"content": f"see {HOST}/{segment}. now"})
    assert kept == {"content": f"see {HOST}/{segment}. now"}
    assert found == []


@pytest.mark.parametrize("segment", ["chat-completions.", "12345,", "a1.", ("a1" * 9) + ";"])
def test_r9_2_e_a_segment_that_is_not_a_token_keeps_its_punctuation(segment):
    kept, found = request_history.redact_json({"content": f"see {HOST}/{segment} now"})
    assert kept == {"content": f"see {HOST}/{segment} now"}
    assert found == []


def test_r9_2_f_the_at_check_runs_before_any_punctuation_handling():
    url = _url("user:" + MIXED, "h.test/items/" + UUID + ".")
    kept, found = request_history.redact_json({"content": f"see {url} now"})
    assert MIXED not in json.dumps(kept)
    assert kept == {"content": "see [url removed] now"}
    assert found == ["url_parts_removed"]


# ------------------------------------------------------------------ R9-3: path segments are read after percent-decoding


def test_r9_3_a_a_percent_encoded_colon_counts_as_a_colon():
    kept, found = request_history.redact_json({"u": f"{HOST}/a%3Ab"})
    assert kept == {"u": f"{HOST}/[removed]"}
    assert found == ["url_parts_removed"]


def test_r9_3_b_a_bot_token_with_an_encoded_colon_is_replaced():
    encoded = BOT_TOKEN.replace(":", "%3A")
    kept, found = request_history.redact_json({"u": f"{HOST}/v1/{encoded}/getMe"})
    assert encoded not in json.dumps(kept)
    assert kept == {"u": f"{HOST}/v1/[removed]/getMe"}
    assert found == ["url_parts_removed"]


def test_r9_3_c_a_fully_encoded_webhook_token_is_replaced():
    encoded = _fully_encoded(WEBHOOK_TOKEN)
    kept, found = request_history.redact_json({"u": f"https://hooks.example.test/services/{encoded}"})
    assert encoded not in json.dumps(kept)
    assert kept == {"u": "https://hooks.example.test/services/[removed]"}
    assert found == ["url_parts_removed"]


def test_r9_3_d_an_encoded_token_before_encoded_punctuation_keeps_the_decoded_punctuation():
    encoded = _fully_encoded(WEBHOOK_TOKEN)
    text = f"see https://hooks.example.test/services/{encoded}%2E now"
    kept, found = request_history.redact_json({"content": text})
    assert kept == {"content": "see https://hooks.example.test/services/[removed]. now"}
    assert found == ["url_parts_removed"]


def test_r9_3_e_a_kept_segment_stays_encoded_as_written_and_an_invalid_escape_is_text():
    kept, found = request_history.redact_json({"u": f"{HOST}/caf%C3%A9/a%20b/%zz"})
    assert kept == {"u": f"{HOST}/caf%C3%A9/a%20b/%zz"}
    assert found == []
