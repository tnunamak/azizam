"""The secret floor for kept request history (docs/design/REQUEST_RETENTION.md section 3.5, rules 2, 4, 5, 7 and 8).

Each rule has its own test, so removing one rule fails at least one test here. Rule 1 (content kinds exist only on
data-plane routes) is tested by the capture tests. Rule 3 (outbound credentials) needs the `sent` part, which slice 5
does not capture. Rule 6 (pasted secrets) cannot be tested: the design says no detector exists.
"""
from __future__ import annotations

import json

import pytest

from gateway import request_history as rh

LIMIT = 1_048_576


def _capture(body: bytes, content_type: str | None, *, chunk: int | None = None):
    capture = rh.BodyCapture(content_type, limit=LIMIT)
    step = chunk or max(len(body), 1)
    for start in range(0, len(body), step):
        capture.feed(body[start:start + step])
    return capture.result()


@pytest.mark.parametrize("name", [
    "api_key", "apiKey", "x-api-key", "clientSecret", "password", "passwd", "authorization",
    "access_token", "refresh_token", "id_token", "auth_token", "credentials", "secret",
])
def test_rule_8_a_credential_named_field_is_removed_at_depth_three(name):
    redacted, omissions = rh.redact_json({"a": {"b": {"c": {name: "SECRET-VALUE-1", "model": "m"}}}})
    assert redacted["a"]["b"]["c"][name] == "[removed]"
    assert redacted["a"]["b"]["c"]["model"] == "m"
    assert "SECRET-VALUE-1" not in json.dumps(redacted)
    assert "credential_field" in omissions


@pytest.mark.parametrize("name", ["max_tokens", "top_k", "token", "tokens"])
def test_rule_8_a_name_that_is_not_a_credential_segment_is_kept(name):
    redacted, omissions = rh.redact_json({name: 5})
    assert redacted == {name: 5}
    assert "credential_field" not in omissions


def test_rule_8_names_are_compared_in_snake_kebab_and_camel_case():
    assert rh.is_credential_name("client_secret")
    assert rh.is_credential_name("ClientSecret")
    assert rh.is_credential_name("x-api-key")
    assert rh.is_credential_name("X-Api-Key")
    assert not rh.is_credential_name("max_tokens")
    assert not rh.is_credential_name("api")


def test_rule_4_a_url_in_a_body_keeps_scheme_host_path_and_query_names_only():
    url = "https://user:pw-secret@cdn.example.test:8443/v1/img/a.png?sig=SIGNED-SECRET&expires=1700000000#frag-secret"
    redacted, omissions = rh.redact_json({"image_url": url})
    text = json.dumps(redacted)
    assert redacted["image_url"] == "https://cdn.example.test:8443/v1/img/a.png?sig=[removed]&expires=[removed]"
    assert "pw-secret" not in text
    assert "SIGNED-SECRET" not in text
    assert "frag-secret" not in text
    assert "url_parts_removed" in omissions


def test_rule_4_a_url_with_no_userinfo_or_query_is_unchanged():
    redacted, omissions = rh.redact_json({"u": "https://example.test/a/b"})
    assert redacted == {"u": "https://example.test/a/b"}
    assert "url_parts_removed" not in omissions


def test_rule_5_a_data_url_and_a_large_base64_string_are_not_copied():
    big = "A" * (64 * 1024 + 4)
    redacted, omissions = rh.redact_json({"image": "data:image/png;base64,QUJD", "blob": big, "small": "AAAA"})
    assert redacted["image"] == {"omitted": "media", "media_type": "image/png", "bytes": 3}
    assert redacted["blob"]["omitted"] == "media"
    assert redacted["blob"]["bytes"] == (64 * 1024 + 4) * 3 // 4
    assert redacted["small"] == "AAAA"
    assert "media" in omissions


def test_rule_5_long_text_that_is_not_base64_is_kept():
    text = "hello world " * 10_000
    redacted, _ = rh.redact_json({"prompt": text})
    assert redacted == {"prompt": text}


def test_rule_2_only_allowlisted_request_headers_are_kept():
    headers = {
        "Authorization": "Bearer opk_SECRET-ONE", "X-Api-Key": "SECRET-TWO", "Cookie": "session=SECRET-THREE",
        "X-Azizam-Internal-Identity": "SECRET-FOUR", "Content-Type": "application/json", "Accept": "application/json",
        "X-Azizam-Prompts": "forget", "X-Custom-Thing": "x",
    }
    kept = rh.allowed_request_headers(headers)
    assert set(kept) <= {"content-type", "accept", "x-azizam-prompts"}
    assert kept["content-type"] == "application/json"
    assert "SECRET" not in json.dumps(kept)


def test_rule_7_a_body_the_gateway_does_not_parse_is_recorded_as_omitted_not_copied():
    part, omissions = _capture(b"raw audio bytes", "audio/wav")
    assert part == {"omitted": "unsupported_container", "media_type": "audio/wav", "bytes": 15}
    assert "unsupported_container" in omissions


def test_rule_7_a_json_array_is_not_an_object_so_it_is_not_copied():
    part, omissions = _capture(b'["a", "b"]', "application/json")
    assert part["omitted"] == "unsupported_container"
    assert "unsupported_container" in omissions


def test_a_json_object_is_kept_whether_it_arrives_in_one_piece_or_one_byte_at_a_time():
    body = json.dumps({"model": "m", "messages": [{"role": "user", "content": "a kept sentence"}]}).encode()
    whole, _ = _capture(body, "application/json")
    bytewise, _ = _capture(body, "application/json", chunk=1)
    assert whole == bytewise == {"model": "m", "messages": [{"role": "user", "content": "a kept sentence"}]}


def test_a_body_over_the_record_limit_is_omitted_with_its_size_and_marked_incomplete():
    capture = rh.BodyCapture("application/json", limit=2048)
    capture.feed(b'{"prompt": "' + b"x" * 4000 + b'"}')
    part, omissions = capture.result()
    assert part["omitted"] == "record_size"
    assert part["bytes"] > 2048
    assert "record_size" in omissions


def test_rule_5_and_7_multipart_fields_are_kept_and_file_parts_are_listed_by_name_type_and_size():
    body = (
        b"--xyz\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\na cat\r\n"
        b"--xyz\r\nContent-Disposition: form-data; name=\"image\"; filename=\"secret-name.png\"\r\n"
        b"Content-Type: image/png\r\n\r\n" + b"\x89PNG" * 10 + b"\r\n--xyz--\r\n"
    )
    part, omissions = _capture(body, "multipart/form-data; boundary=xyz", chunk=7)
    assert part["fields"] == {"prompt": "a cat"}
    assert part["files"] == [{"field": "image", "media_type": "image/png", "bytes": 40}]
    text = json.dumps(part)
    assert "secret-name" not in text
    assert "PNG" not in text
    assert "unsupported_container" not in omissions
