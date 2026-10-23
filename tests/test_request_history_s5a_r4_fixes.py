"""Round-4 fixes for request history slice 5a (docs/design/REQUEST_RETENTION.md, slice 5a review fixes).

Each test names the round-4 finding it pins (R4-n). The round-2 probes are in
``tests/test_review_retention_s5a_r2_probes.py``; the round-3 fixes are in ``tests/test_request_history_s5a_r3_fixes.py``.
"""
from __future__ import annotations

import json
import threading

import pytest

from gateway import request_history, request_history_capture
from tests.test_review_retention_s5a_probes import _drive, _kept, member_db  # noqa: F401 - fixture

SECRET = "SYNTHETIC_MARKER"
MAX_SLOTS = request_history_capture.MAX_PENDING_CAPTURES
#: The RFC 3986 sub-delims, and the quote a pasted string may carry in a user name or password.
USER_INFO_DELIMITERS = "!$&'()*+,;=\""
#: The same, minus ``&``, which separates query items and so is not part of a value.
QUERY_DELIMITERS = "!$'()*+,;=\""


@pytest.fixture(autouse=True)
def own_admission_limit(monkeypatch):
    """Each test has its own admission limit, so a slot that a failing test leaves taken cannot reach the next test."""
    monkeypatch.setattr(request_history_capture, "_PENDING", threading.BoundedSemaphore(MAX_SLOTS))


# ------------------------------------------------------------------ R4-1: a URL's user info, sub-delims included


@pytest.mark.parametrize("delimiter", USER_INFO_DELIMITERS)
def test_r4_1_a_user_info_with_a_delimiter_is_removed_from_a_url_in_text(delimiter):
    kept, found = request_history.redact_json({"content": f"connect postgres://user:a{delimiter}{SECRET}@db/app now"})
    assert SECRET not in json.dumps(kept)
    assert kept == {"content": "connect postgres://[removed]@db/app now"}
    assert "url_parts_removed" in found


@pytest.mark.parametrize("delimiter", USER_INFO_DELIMITERS)
def test_r4_1_b_user_info_with_a_delimiter_is_removed_from_a_url_that_is_a_whole_value(delimiter):
    kept, _ = request_history.redact_json({"dsn": f"postgres://user:a{delimiter}{SECRET}@db/app"})
    assert kept == {"dsn": "postgres://[removed]@db/app"}


@pytest.mark.parametrize("url", [
    f"postgres://user:pa/{SECRET}@db/app",
    f"postgres://user:12/{SECRET}@db/app",
    f"postgres://user:pa?{SECRET}@db/app",
    f"postgres://user:pa#{SECRET}@db/app",
    f"postgres://user:pa@{SECRET}@db/app",
])
def test_r4_1_c_an_ambiguous_user_info_boundary_is_cut_at_the_last_at_sign(url):
    kept, _ = request_history.redact_json({"dsn": url, "content": f"reach {url} now"})
    assert SECRET not in json.dumps(kept)
    assert kept == {"dsn": "postgres://[removed]@db/app", "content": "reach postgres://[removed]@db/app now"}


@pytest.mark.parametrize("delimiter", QUERY_DELIMITERS)
def test_r4_1_d_a_query_value_with_a_delimiter_is_removed_from_a_url_in_text(delimiter):
    kept, _ = request_history.redact_json({"content": f"see https://h.test/a?token=abc{delimiter}{SECRET}&x=1 now"})
    assert SECRET not in json.dumps(kept)
    assert kept == {"content": "see https://h.test/a?token=[removed]&x=[removed] now"}


@pytest.mark.parametrize("delimiter", QUERY_DELIMITERS)
def test_r4_1_e_a_query_value_with_a_delimiter_is_removed_from_a_url_that_is_a_whole_value(delimiter):
    kept, _ = request_history.redact_json({"u": f"https://h.test/a?token=abc{delimiter}{SECRET}&x=1"})
    assert SECRET not in json.dumps(kept)
    assert kept == {"u": "https://h.test/a?token=[removed]&x=[removed]"}


def test_r4_1_f_a_quoted_user_info_in_a_message_keeps_no_secret_in_the_row(member_db):
    db, media, person = member_db
    raw = json.dumps({"content": f"connect postgres://user:'{SECRET}'@db/app"}).encode()
    _drive(db, media, person, chunks=[raw], content_type="application/json")
    rows = _kept(media)
    assert len(rows) == 1
    assert SECRET not in rows[0] and "postgres://[removed]@db/app" in rows[0]
