"""Round-3 fixes for request history slice 5a (docs/design/REQUEST_RETENTION.md, slice 5a review fixes).

Each test names the round-2 finding it pins (R2-n). The round-2 probes are in ``tests/test_review_retention_s5a_r2_probes.py``;
these tests cover what the probes do not.
"""
from __future__ import annotations

import json
from urllib.parse import urlencode

from tests.test_review_retention_s5a_probes import SENTENCE, _drive, _kept, member_db  # noqa: F401 - fixture

FORGET = {"com.azizam.prompts": {"mode": "forget"}}
FORGET_FIELD = "extensions[com.azizam.prompts][mode]"


# ------------------------------------------------------------------ R2-1: the control in a body of another media type


def test_r2_1_a_form_body_with_a_forget_field_keeps_no_row(member_db):
    db, media, person = member_db
    raw = urlencode({"model": "m", "input": SENTENCE, FORGET_FIELD: "forget"}).encode()
    _drive(db, media, person, chunks=[raw], content_type="application/x-www-form-urlencoded")
    assert _kept(media) == []


def test_r2_1_a_form_body_whose_extensions_field_holds_a_json_forget_keeps_no_row(member_db):
    db, media, person = member_db
    raw = urlencode({"model": "m", "input": SENTENCE, "extensions": json.dumps(FORGET)}).encode()
    _drive(db, media, person, chunks=[raw], content_type="application/x-www-form-urlencoded")
    assert _kept(media) == []


def test_r2_1_a_form_field_with_an_invalid_mode_is_a_forget_and_keeps_no_row(member_db):
    db, media, person = member_db
    raw = urlencode({"model": "m", "input": SENTENCE, FORGET_FIELD: "keep"}).encode()
    _drive(db, media, person, chunks=[raw], content_type="application/x-www-form-urlencoded")
    assert _kept(media) == []


def test_r2_1_a_body_with_no_content_type_that_carries_a_json_forget_keeps_no_row(member_db):
    db, media, person = member_db
    raw = json.dumps({"model": "m", "input": SENTENCE, "extensions": FORGET}).encode()
    _drive(db, media, person, chunks=[raw], content_type="")
    assert _kept(media) == []


def test_r2_1_a_body_over_the_record_limit_in_another_container_leaves_no_row(member_db):
    """Its control cannot be read in full, so nothing is kept on it (REQUEST_RETENTION.md, unreadable control)."""
    db, media, person = member_db
    raw = b"x" * (1024 * 1024 + 1)
    _drive(db, media, person, chunks=[raw], content_type="application/octet-stream")
    assert _kept(media) == []


def test_r2_1_control_a_form_body_without_a_control_is_kept_with_its_body_unsupported(member_db):
    db, media, person = member_db
    raw = urlencode({"model": "m", "input": SENTENCE}).encode()
    _drive(db, media, person, chunks=[raw], content_type="application/x-www-form-urlencoded")
    rows = _kept(media)
    assert len(rows) == 1 and SENTENCE not in rows[0] and "unsupported_container" in rows[0]


def test_r2_1_control_a_text_plain_json_body_without_a_forget_is_kept_with_its_body_unsupported(member_db):
    """A body the route parses is still not kept as content (rule 7). Only its control is read."""
    db, media, person = member_db
    raw = json.dumps({"model": "m", "input": SENTENCE}).encode()
    _drive(db, media, person, chunks=[raw], content_type="text/plain")
    rows = _kept(media)
    assert len(rows) == 1 and SENTENCE not in rows[0] and "unsupported_container" in rows[0]
