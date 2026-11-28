"""Round-8 fixes for request history slice 5a (docs/design/REQUEST_RETENTION.md, slice 5a review fixes).

Each test names the round-8 finding it pins (R8-n). R8-1 adds a shape rule for URL path segments to the rule of round 7:
a segment that holds a colon, or that is 20 or more characters from ``[A-Za-z0-9_-]`` with a letter and a digit, becomes
``[removed]``. The tokens here are built from pieces at run time, so no literal token sits in this file.
"""
from __future__ import annotations

import json

import pytest

from gateway import request_history

HOST = "https://api.example.test"


def _run(pattern: str, length: int) -> str:
    """``pattern`` repeated, then cut to ``length`` characters."""
    return (pattern * length)[:length]


BOT_TOKEN = "bot" + _run("1234567890", 9) + ":" + _run("aB3_-", 35)
WEBHOOK_TOKEN = _run("Ab1", 32)
UUID = "-".join([_run("1a", 8), _run("2b", 4), _run("3c", 4), _run("d4", 4), _run("5e", 12)])


# ------------------------------------------------------------------ R8-1: token-shaped path segments


def test_r8_1_a_a_bot_style_segment_with_a_colon_is_replaced_and_the_other_segments_are_kept():
    kept, found = request_history.redact_json({"u": f"{HOST}/v1/{BOT_TOKEN}/getMe"})
    assert BOT_TOKEN not in json.dumps(kept)
    assert kept == {"u": f"{HOST}/v1/[removed]/getMe"}
    assert found == ["url_parts_removed"]


def test_r8_1_b_a_32_character_webhook_token_at_the_end_of_the_path_is_replaced():
    kept, found = request_history.redact_json({"u": f"https://hooks.example.test/services/{WEBHOOK_TOKEN}"})
    assert WEBHOOK_TOKEN not in json.dumps(kept)
    assert kept == {"u": "https://hooks.example.test/services/[removed]"}
    assert found == ["url_parts_removed"]


def test_r8_1_c_a_uuid_segment_is_replaced():
    kept, found = request_history.redact_json({"u": f"{HOST}/items/{UUID}"})
    assert UUID not in json.dumps(kept)
    assert kept == {"u": f"{HOST}/items/[removed]"}
    assert found == ["url_parts_removed"]


def test_r8_1_d_a_short_slug_is_kept():
    kept, found = request_history.redact_json({"u": f"{HOST}/v1/chat-completions"})
    assert kept == {"u": f"{HOST}/v1/chat-completions"}
    assert found == []


def test_r8_1_e_a_numeric_id_is_kept():
    kept, found = request_history.redact_json({"u": f"{HOST}/files/12345"})
    assert kept == {"u": f"{HOST}/files/12345"}
    assert found == []


@pytest.mark.parametrize(("segment", "removed"), [
    (_run("Ab1", 19), False),           # one character short of the threshold
    (_run("Ab1", 20), True),            # at the threshold: 20 characters, a letter and a digit
    (_run("abcDEF", 20), False),        # 20 characters, letters only
    (_run("1234567890", 20), False),    # 20 characters, digits only
    (_run("a1", 20) + ".txt", False),   # a dot is not in the set, so the segment is not a token
])
def test_r8_1_f_a_token_is_20_characters_from_the_set_with_a_letter_and_a_digit(segment, removed):
    kept, found = request_history.redact_json({"u": f"{HOST}/{segment}"})
    assert kept == {"u": f"{HOST}/" + ("[removed]" if removed else segment)}
    assert found == (["url_parts_removed"] if removed else [])


def test_r8_1_g_a_colon_makes_a_short_segment_a_token():
    kept, found = request_history.redact_json({"u": f"{HOST}/a:b/c"})
    assert kept == {"u": f"{HOST}/[removed]/c"}
    assert found == ["url_parts_removed"]


def test_r8_1_h_a_colon_in_the_port_is_not_a_path_segment():
    kept, found = request_history.redact_json({"u": "https://h.test:8443/v1/chat-completions"})
    assert kept == {"u": "https://h.test:8443/v1/chat-completions"}
    assert found == []


def test_r8_1_i_a_host_that_reads_as_a_token_is_not_a_path_segment():
    host = "https://" + _run("Ab1", 24) + ".example.test/v1"
    kept, found = request_history.redact_json({"u": host})
    assert kept == {"u": host}
    assert found == []


@pytest.mark.parametrize(("text", "kept_as"), [
    (f"see {HOST}/{BOT_TOKEN}/getMe now", f"see {HOST}/[removed]/getMe now"),
    (f"see {HOST}/items/{UUID}?page=2 now", f"see {HOST}/items/[removed]?[removed] now"),
])
def test_r8_1_j_a_token_segment_is_replaced_inside_a_longer_string(text, kept_as):
    kept, found = request_history.redact_json({"content": text})
    assert kept == {"content": kept_as}
    assert found == ["url_parts_removed"]
