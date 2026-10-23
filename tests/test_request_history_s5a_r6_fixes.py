"""Round-6 fixes for request history slice 5a (docs/design/REQUEST_RETENTION.md, slice 5a review fixes).

Each test names the round-6 finding it pins (R6-n). The round-4 fixes, whose user info rule round 6 replaces, are in
``tests/test_request_history_s5a_r4_fixes.py``.
"""
from __future__ import annotations

import json

import pytest

from gateway import request_history

SECRET = "SYNTHETIC_MARKER"


# ------------------------------------------------------------------ R6-1: the authority ends at the first /, ? or #


@pytest.mark.parametrize(("url", "kept_as"), [
    (f"https://h.test/#token=prefix@{SECRET}", "https://h.test/"),
    (f"https://h.test/?token=prefix@{SECRET}", "https://h.test/?token=[removed]"),
    (f"https://h.test?token=prefix@{SECRET}", "https://h.test?token=[removed]"),
    (f"https://h.test#token=prefix@{SECRET}", "https://h.test"),
    (f"https://h.test/a?x=1&token=prefix@{SECRET}#frag@{SECRET}", "https://h.test/a?x=[removed]&token=[removed]"),
    (f"https://u:pw@h.test/a?token=prefix@{SECRET}", "https://[removed]@h.test/a?token=[removed]"),
])
def test_r6_1_a_an_at_sign_after_the_authority_never_makes_its_text_a_host(url, kept_as):
    kept, found = request_history.redact_json({"u": url})
    assert SECRET not in json.dumps(kept)
    assert kept == {"u": kept_as}
    assert "url_parts_removed" in found


@pytest.mark.parametrize(("url", "sentence"), [
    (f"https://h.test/#token=prefix@{SECRET}", "see https://h.test/ now"),
    (f"https://h.test/?token=prefix@{SECRET}", "see https://h.test/?token=[removed] now"),
])
def test_r6_1_b_the_same_rule_holds_for_a_url_inside_a_sentence(url, sentence):
    kept, _ = request_history.redact_json({"content": f"see {url} now"})
    assert SECRET not in json.dumps(kept)
    assert kept == {"content": sentence}


@pytest.mark.parametrize("url", [
    f"https://u:pa?{SECRET}@h.test/x",
    f"https://u:pa#{SECRET}@h.test/x",
    f"https://u:pa/{SECRET}@h.test/x",
    f"https://u:12/{SECRET}@h.test/x",
    f"https://u:p@{SECRET}@h.test/x",
    f"https://h.test\\@{SECRET}/x",
    f"https://u:p@h.test:notaport/{SECRET}",
    f"https://h.test:99999/{SECRET}",
    f"https://[::1/{SECRET}",
])
def test_r6_1_c_a_url_that_cannot_be_split_is_replaced_whole_and_no_part_of_it_is_kept(url):
    kept, found = request_history.redact_json({"dsn": url, "content": f"reach {url} now"})
    assert SECRET not in json.dumps(kept)
    assert kept == {"dsn": "[url removed]", "content": "reach [url removed] now"}
    assert "url_parts_removed" in found


@pytest.mark.parametrize(("url", "kept_as"), [
    ("https://user:pw@h.test:8443/a?x=1", "https://[removed]@h.test:8443/a?x=[removed]"),
    ("https://u:p@[::1]:8080/x", "https://[removed]@[::1]:8080/x"),
    ("https://u:p@h.test/x@y", "https://[removed]@h.test/x@y"),
    ("https://h.test:8443/a?x=1", "https://h.test:8443/a?x=[removed]"),
])
def test_r6_1_d_user_info_before_the_authority_ends_is_removed_and_the_host_is_kept(url, kept_as):
    kept, _ = request_history.redact_json({"u": url})
    assert kept == {"u": kept_as}
