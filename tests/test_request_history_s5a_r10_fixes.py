"""Round 10 of request-history URL cleaning: the three residuals the round-9 builder reported.

Credential-shaped strings are built from pieces at runtime, so none sits in the source.
"""
from gateway import request_history

SECRET = "Abcdefghij" + "klmnopqr" + "1234"


def clean(text: str) -> str:
    found: set[str] = set()
    return request_history._redact_text(text, found)


def test_a_token_shaped_scheme_after_a_comma_is_removed():
    text = "https://a.test/x," + SECRET + "://b.test"
    assert SECRET.lower() not in str(clean(text)).lower()


def test_a_token_before_an_encoded_slash_is_removed():
    text = "see https://a.test/" + SECRET + "%2Fmore now"
    assert SECRET not in str(clean(text))


def test_a_token_beside_a_star_or_tilde_is_removed():
    for mark in ("*", "~"):
        assert SECRET not in str(clean("see https://a.test/" + SECRET + mark + " now"))


def test_short_schemes_and_plain_segments_are_kept():
    assert clean("see https://a.test/v1/chat-completions now") == "see https://a.test/v1/chat-completions now"
    assert clean("open chrome-extension://abc/page") == "open chrome-extension://abc/page"
