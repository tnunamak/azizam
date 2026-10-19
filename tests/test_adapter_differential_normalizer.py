"""The differential harness's normaliser removes volatile values and nothing else.

A harness that masks too much cannot prove a refactor changed nothing; one that masks
too little fails on every run. These tests pin both sides: each rule replaces what it
names, and a change to any other byte still changes the trace.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

import pytest

from tests.support import adapter_differential as ad

REQ_A = "req_" + "a1" * 16
REQ_B = "req_" + "b2" * 16
JOB_A = "mj_" + "c3" * 16
UUID_A = "6f1c2f9e-6b0a-4c3e-9a57-0d1e5a7a8b11"
UUID_B = "7a1c2f9e-6b0a-4c3e-9a57-0d1e5a7a8b22"
HEX32_A = "d4" * 16
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "adapter_differential"


def norm(**kw) -> ad.Normalizer:
    return ad.Normalizer(**kw)


# --------------------------------------------------------------------------- what is replaced

def test_ids_are_replaced_by_order_of_first_appearance_per_kind():
    n = norm()
    text = f"{REQ_A} {JOB_A} {REQ_B} {REQ_A} {UUID_A} {HEX32_A} {UUID_B} {UUID_A}"
    assert n.text(text) == "<req:1> <job:1> <req:2> <req:1> <uuid:1> <hex32:1> <uuid:2> <uuid:1>"


def test_a_second_trace_numbers_ids_from_one_again():
    assert norm().text(REQ_A) == norm().text(REQ_B) == "<req:1>"


@pytest.mark.parametrize("prefix", ["mj", "vidjob", "imgjob", "audjob"])
def test_job_id_prefixes(prefix):
    assert norm().text(f"{prefix}_{'ef' * 16}") == "<job:1>"


def test_times_and_signatures():
    n = norm()
    assert n.text("at 2026-10-18T15:25:38Z and 2026-10-18 15:25:38.123456+00:00") == "at <iso-time> and <iso-time>"
    assert n.text("Sun, 18 Oct 2026 15:25:38 GMT") == "<http-date>"
    url = "http://gateway.test/v1/assets/img_x/content?expires=1792359200&md=none&sig=6d26e374062c1687fb4bf347909a97ab"
    assert n.text(url) == "http://gateway.test/v1/assets/img_x/content?expires=<expires>&md=none&sig=<sig>"


def test_temp_roots_become_a_token(tmp_path):
    n = norm(tmp_roots=[tmp_path])
    assert n.text(f"{tmp_path}/cache/a.png and {tmp_path.resolve()}/x") == "<tmp>/cache/a.png and <tmp>/x"


def test_time_keys_in_json_text_and_in_parsed_values():
    text = '{"created": 1792355600, "accepted_at_ms":1792355600123, "retry_after": 7, "latency_ms": 12.5}'
    assert norm().text(text) == '{"created": "<time>", "accepted_at_ms":"<time>", "retry_after": 7, "latency_ms": "<duration>"}'
    parsed = norm().value({"created": 1, "finished_at": 2, "wait_deadline": 3.5, "latency_ms": 9, "n": 3, "finished_at_ms": None})
    assert parsed == {"created": "<time>", "finished_at": "<time>", "wait_deadline": "<time>",
                      "latency_ms": "<duration>", "n": 3, "finished_at_ms": None}


def test_digests_over_random_ids_are_masked_only_where_they_are_random():
    h = "ab" * 32
    n = norm()
    assert n.value({"input_hash": h, "idempotency": "request", "request_hash": h}) == {
        "input_hash": "<digest>", "idempotency": "request", "request_hash": "<random-hash>"}
    # a seeded or idempotency-keyed request hash is a digest of the request: compared exactly
    for kind in ("seeded_request", "idempotency_key"):
        assert n.value({"idempotency": kind, "request_hash": h})["request_hash"] == h
    # the same rules in JSON text
    assert norm().text(f'{{"request_hash":"{h}","metadata":{{}}}}') == '{"request_hash":"<random-hash>","metadata":{}}'
    keyed = f'{{"request_hash":"{h}","idempotency":"seeded_request"}}'
    assert norm().text(keyed) == keyed


# --------------------------------------------------------------------------- what is NOT replaced

@pytest.mark.parametrize("kept", [
    "x" + "a1" * 15 + "a",            # 31 hex characters
    "a1" * 16 + "b",                   # 33 hex characters
    hashlib.sha256(b"payload").hexdigest(),   # a content digest (asset ids are made of these)
    "img_" + hashlib.sha256(b"payload").hexdigest(),
    "req_" + "a1" * 15,                # too short to be a request id
    "request id req_unknown",
    "tmp/cache/a.png",                 # a relative path that only looks like a temp path
    "sig=abc without a query separator",
    "2026-10-18",                      # a date without a time
    "expires_in=3600",                 # a lifetime, not an expiry instant
    "1792355600",                      # a bare number outside a time key
])
def test_other_text_is_untouched(kept):
    assert norm(tmp_roots=["/nonexistent-root"]).text(kept) == kept


def test_numbers_under_other_keys_are_untouched():
    value = {"n": 3, "size": 1024, "progress": 50, "retry_after": 7, "expires_in": 3600, "attempt_number": 2,
             "timeout_ms": 30000, "created_by": "someone", "status": 200, "seed": 192058881, "created_at_note": 5}
    assert norm().value(value) == value
    text = json.dumps(value)
    assert norm().text(text) == text


def test_null_time_stays_null_so_unset_versus_set_still_differs():
    assert norm().value({"finished_at": None}) != norm().value({"finished_at": 5})


# --------------------------------------------------------------------------- a changed byte changes the trace

def trace_text(body: str, ct="application/json") -> str:
    return json.dumps(ad.describe_body(body.encode(), ct, norm()), sort_keys=True)


def test_one_changed_character_changes_the_body_description():
    base = '{"prompt": "a red fox", "seed": 7, "id": "%s"}' % REQ_A
    assert trace_text(base) == trace_text(base.replace(REQ_A, REQ_B))  # a volatile id: same trace
    for changed in (base.replace("red", "rEd"), base.replace('"seed": 7', '"seed": 8'), base.replace(", ", ",")):
        assert trace_text(changed) != trace_text(base)


def test_key_order_and_spacing_are_part_of_the_wire_bytes():
    assert trace_text('{"a": 1, "b": 2}') != trace_text('{"b": 2, "a": 1}')
    assert trace_text('{"a":1}') != trace_text('{"a": 1}')


def test_sorting_json_keys_is_opt_in_and_keeps_separator_style():
    a, b = '{"a": 1, "b": {"y": 1, "x": 2}}', '{"b": {"x": 2, "y": 1}, "a": 1}'
    unsorted_a = ad.describe_body(a.encode(), "application/json", norm())
    unsorted_b = ad.describe_body(b.encode(), "application/json", norm())
    assert unsorted_a != unsorted_b
    sorted_a = ad.describe_body(a.encode(), "application/json", norm(), unordered_json=True)
    sorted_b = ad.describe_body(b.encode(), "application/json", norm(), unordered_json=True)
    assert sorted_a == sorted_b
    assert ad.sort_json_keys('{"b":1,"a":2}') == '{"a":2,"b":1}'
    assert ad.sort_json_keys('{"b": 1, "a": 2}') == '{"a": 2, "b": 1}'


def test_a_changed_value_still_shows_inside_a_sorted_body():
    a = ad.describe_body(b'{"a": 1}', "application/json", norm(), unordered_json=True)
    b = ad.describe_body(b'{"a": 2}', "application/json", norm(), unordered_json=True)
    assert a != b


def test_identical_ids_versus_different_ids_stay_distinguishable():
    same = norm().text(f"{REQ_A} {REQ_A}")
    different = norm().text(f"{REQ_A} {REQ_B}")
    assert same != different


def test_binary_is_compared_by_sha256_and_length():
    one = ad.describe_body(b"\x89PNG\x00\x01", "image/png", norm())
    two = ad.describe_body(b"\x89PNG\x00\x02", "image/png", norm())
    assert one["kind"] == "binary" and one["len"] == 6 and one["sha256"] == hashlib.sha256(b"\x89PNG\x00\x01").hexdigest()
    assert one != two


def multipart(boundary: str, payload: bytes = b"\x89PNG\x01") -> tuple[bytes, str]:
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\nhello {REQ_A}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"a.png\"\r\n"
            f"Content-Type: image/png\r\n\r\n").encode() + payload + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def describe_multipart(boundary: str, payload: bytes = b"\x89PNG\x01") -> dict:
    body, ct = multipart(boundary, payload)
    return ad.describe_body(body, ct, norm())


def test_the_random_multipart_boundary_is_replaced_in_header_and_body():
    one, two = describe_multipart("a" * 32), describe_multipart("b" * 32)
    assert one == two
    assert one["boundary"] == ad.BOUNDARY_TOKEN
    assert "a" * 32 not in json.dumps(one)
    headers = ad.describe_request_headers([("Content-Type", f"multipart/form-data; boundary={'a' * 32}")], norm())
    assert headers == {"content-type": f"multipart/form-data; boundary={ad.BOUNDARY_TOKEN}"}


def test_a_changed_multipart_part_still_changes_the_description():
    assert describe_multipart("a" * 32, b"\x89PNG\x01") != describe_multipart("a" * 32, b"\x89PNG\x02")
    body, ct = multipart("a" * 32)
    changed = body.replace(b'name="image"', b'name="mask"')
    assert ad.describe_body(changed, ct, norm()) != ad.describe_body(body, ct, norm())
    assert ad.describe_body(body.replace(b"hello", b"hullo"), ct, norm()) != ad.describe_body(body, ct, norm())


def test_request_headers_drop_only_the_user_agent_and_hide_credentials():
    raw = [("User-Agent", "python-httpx/0.28"), ("Authorization", "Bearer sk-secret"), ("X-Request-Id", REQ_A),
           ("Content-Type", "application/json"), ("Accept-Encoding", "gzip"), ("Host", "comfy.fk.test")]
    out = ad.describe_request_headers(raw, norm())
    assert out == {"accept-encoding": "gzip", "authorization": "<present>", "content-type": "application/json",
                   "host": "comfy.fk.test", "x-request-id": "<req:1>"}
    assert "sk-secret" not in json.dumps(out)
    assert ad.describe_request_headers([("Accept-Encoding", "gzip")], norm()) != ad.describe_request_headers(
        [("Accept-Encoding", "identity")], norm())


def test_response_headers_drop_date_and_server_and_keep_the_rest():
    raw = [("Date", "Sun, 18 Oct 2026 15:25:38 GMT"), ("Server", "uvicorn"), ("Content-Type", "audio/wav"),
           ("Content-Length", "10"), ("Retry-After", "7"), ("X-Image-Rounds", "1"), ("Cache-Control", "no-store"),
           ("Last-Modified", "Sun, 18 Oct 2026 15:25:38 GMT")]
    assert ad.describe_response_headers(raw, norm()) == {
        "cache-control": "no-store", "content-length": "10", "content-type": "audio/wav",
        "last-modified": "<http-date>", "retry-after": "7", "x-image-rounds": "1"}


def test_query_pairs_are_sorted_and_signed_values_normalised():
    assert ad.sorted_query("type=output&filename=a.png") == [["filename", "a.png"], ["type", "output"]]
    signed = ad.sorted_query("expires=1792359200&md=none&sig=" + "ab" * 32, norm())
    assert signed == [["expires", "<expires>"], ["md", "none"], ["sig", "<sig>"]]
    assert ad.sorted_query("filename=a.png", norm()) != ad.sorted_query("filename=b.png", norm())


def test_content_length_follows_the_normalised_text_only_when_normalisation_changed_the_length():
    body_raw = b'{"created": 1792355600, "k": "a"}'
    desc = ad.describe_body(body_raw, "application/json", norm())
    headers = {"content-length": str(len(body_raw))}
    ad.settle_content_length(headers, body_raw, desc)
    assert headers["content-length"] == f"<{len(desc['text'].encode())} after normalisation>"
    # a changed non-volatile byte count still shows
    longer = ad.describe_body(body_raw.replace(b'"a"', b'"abc"'), "application/json", norm())
    other = {"content-length": "x"}
    ad.settle_content_length(other, body_raw.replace(b'"a"', b'"abc"'), longer)
    assert other["content-length"] != headers["content-length"]
    # nothing normalised: the real header stays
    plain = b'{"k": "a"}'
    kept = {"content-length": str(len(plain))}
    ad.settle_content_length(kept, plain, ad.describe_body(plain, "application/json", norm()))
    assert kept["content-length"] == str(len(plain))


def test_speech_sse_audio_is_compared_as_joined_audio_and_the_other_events_exactly():
    import base64

    def event(payload: dict) -> bytes:
        return b"data: " + json.dumps(payload, separators=(",", ":")).encode() + b"\n\n"

    def stream(*pieces: bytes, done: str = "speech.audio.done") -> bytes:
        return b"".join(event({"type": "speech.audio.delta", "audio": base64.b64encode(p).decode()}) for p in pieces) + event({"type": done})

    one = ad.describe_sse_audio(stream(b"\x01\x02\x03\x04"), norm())
    cut = ad.describe_sse_audio(stream(b"\x01\x02", b"\x03\x04"), norm())
    assert one == cut                                  # where the audio was cut is a timing artefact
    assert ad.describe_sse_audio(stream(b"\x01\x02\x03\x05"), norm()) != one              # other audio is not
    assert ad.describe_sse_audio(stream(b"\x01\x02\x03\x04", done="speech.audio.fin"), norm()) != one  # nor is another event


# --------------------------------------------------------------------------- stores and assets

def test_sqlite_dump_keeps_row_order_parses_json_and_normalises(tmp_path):
    db = tmp_path / "s.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, request_id TEXT, created_at INTEGER, doc TEXT, n INTEGER)")
    conn.execute("INSERT INTO jobs VALUES (1, ?, 1792355600, ?, 5)", (REQ_B, json.dumps({"finished_at": 9, "k": REQ_A})))
    conn.execute("INSERT INTO jobs VALUES (2, ?, 1792355601, ?, 6)", (REQ_A, json.dumps({"finished_at": None, "k": "v"})))
    conn.commit()
    conn.close()
    rows = ad.dump_sqlite(db, norm(tmp_roots=[tmp_path]))["jobs"]
    assert [r["id"] for r in rows] == [1, 2]
    assert rows[0] == {"id": 1, "request_id": "<req:1>", "created_at": "<time>",
                       "doc": {"finished_at": "<time>", "k": "<req:2>"}, "n": 5}
    assert rows[1]["doc"] == {"finished_at": None, "k": "v"} and rows[1]["n"] == 6


def test_asset_listing_is_by_content_not_by_path(tmp_path):
    (tmp_path / "x.png").write_bytes(b"png-bytes")
    (tmp_path / "x.meta.json").write_text(json.dumps({"updated_at": 1792355600, "asset_id": "img_" + "0" * 64}))
    listing = ad.describe_assets(tmp_path, norm(tmp_roots=[tmp_path]))
    png = next(item for item in listing if item["suffix"] == ".png")
    assert png["sha256"] == hashlib.sha256(b"png-bytes").hexdigest() and png["size"] == 9
    meta = next(item for item in listing if item["suffix"] == ".json")
    assert meta["json"]["updated_at"] == "<time>" and meta["json"]["asset_id"] == "img_" + "0" * 64
    (tmp_path / "x.png").write_bytes(b"png-bytez")
    assert ad.describe_assets(tmp_path, norm()) != listing


# --------------------------------------------------------------------------- golden compare

def test_compare_golden_equal_different_missing_update_and_dump(tmp_path):
    trace = {"scenario": "demo", "schema": 1, "caller": [{"a": 1}, {"b": ["x", "y"]}]}
    fixtures = tmp_path / "fixtures"
    # missing
    assert "is missing" in ad.compare_golden("demo", trace, fixture_dir=fixtures, environ={})
    # update writes, then equal
    assert ad.compare_golden("demo", trace, fixture_dir=fixtures, environ={ad.UPDATE_ENV: "1"}) == ""
    assert ad.compare_golden("demo", trace, fixture_dir=fixtures, environ={}) == ""
    # a changed leaf is reported at its path with the surrounding entry
    changed = json.loads(json.dumps(trace))
    changed["caller"][1]["b"][1] = "z"
    message = ad.compare_golden("demo", changed, fixture_dir=fixtures, environ={})
    assert "$.caller[1].b[1]" in message and '"y"' in message and '"z"' in message
    # a changed body text reports the first differing JSON value and character
    golden = {"scenario": "t", "k": {"text": '{"a": 1, "b": 2}'}}
    other = {"scenario": "t", "k": {"text": '{"a": 1, "b": 3}'}}
    (fixtures / "t.json").write_text(ad.dumps(golden))
    text_message = ad.compare_golden("t", other, fixture_dir=fixtures, environ={})
    assert "first differing JSON value at $.b" in text_message and "offset" in text_message
    # a missing and an added key are both differences
    assert ad.compare_golden("t", {"scenario": "t"}, fixture_dir=fixtures, environ={})
    assert ad.compare_golden("t", {**golden, "extra": 1}, fixture_dir=fixtures, environ={})
    # dump mode writes the trace beside comparing
    out = tmp_path / "out"
    assert ad.compare_golden("demo", trace, fixture_dir=fixtures, environ={ad.OUT_ENV: str(out)}) == ""
    assert json.loads((out / "demo.json").read_text()) == trace


def test_first_difference_distinguishes_type_length_and_order():
    assert ad.first_difference({"a": [1, 2]}, {"a": [1, 2]}) is None
    assert ad.first_difference({"a": [1, 2]}, {"a": [2, 1]})[0] == "$.a[0]"
    assert ad.first_difference({"a": [1]}, {"a": [1, 2]})[0] == "$.a[1]"
    assert ad.first_difference({"a": 1}, {"a": "1"})[0] == "$.a"
    assert ad.first_difference({"a": True}, {"a": 1})[0] == "$.a"


def test_junit_extraction_round_trip(tmp_path):
    trace = {"scenario": "demo", "schema": 1, "caller": []}
    xml = tmp_path / "r.xml"
    value = json.dumps(trace).replace('"', "&quot;")
    xml.write_text(f'<testsuites><testsuite><properties><property name="adapter_trace.demo" value="{value}"/>'
                   f'<property name="other" value="x"/></properties></testsuite></testsuites>')
    assert ad.extract_junit(xml, tmp_path / "out") == ["demo.json"]
    assert json.loads((tmp_path / "out" / "demo.json").read_text()) == trace


# --------------------------------------------------------------------------- the committed goldens

def golden_files() -> list[Path]:
    return sorted(FIXTURES.glob("*.json"))


def scenario_names() -> set[str]:
    names: set[str] = set()
    for source in Path(__file__).resolve().parent.glob("test_adapter_differential*.py"):
        if source.name.endswith("normalizer.py"):
            continue
        names.update(re.findall(r'^\s*play\(\s*"([a-z0-9_]+)"', source.read_text(), re.MULTILINE))
    return names


def test_every_scenario_has_a_golden_and_every_golden_a_scenario():
    assert {path.stem for path in golden_files()} == scenario_names()
    assert len(scenario_names()) >= 40


def test_goldens_hold_no_volatile_values():
    """Whatever the normaliser should have removed is gone from every committed golden."""
    patterns = {
        "dashed uuid": re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
        "request id": re.compile(r"req_[0-9a-f]{32}"),
        "job id": re.compile(r"(?:mj|vidjob|imgjob|audjob)_[0-9a-f]{32}"),
        "temp path": re.compile(r"/dev/shm|azizam-pytest|/\.tmp/|/tmp/pytest"),
        "signature": re.compile(r"sig=[0-9a-f]{16,}"),
        "iso time": re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"),
        "epoch under a time key": re.compile(r'"(?:created|[a-z_]+_at|[a-z_]+_at_ms|expires)\\?"\s*:\s*\\?\s*1[0-9]{9}'),
        "multipart boundary": re.compile(r"boundary=(?!<boundary>)[0-9A-Za-z'()+_,./:=?-]{8,}"),
        "credential": re.compile(r"Bearer |sk-[A-Za-z0-9]{8}|opk_|oak_"),
    }
    found = []
    for path in golden_files():
        text = path.read_text(encoding="utf-8")
        found += [f"{path.name}: {label}" for label, pattern in patterns.items() if pattern.search(text)]
        document = json.loads(text)
        assert document["schema"] == ad.TRACE_SCHEMA and document["scenario"] == path.stem
    assert not found, found
