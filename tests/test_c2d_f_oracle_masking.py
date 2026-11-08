"""Seven ways the golden oracle called two different answers equal (review c2d-oracle-masking-review-1108). Each test
puts two different answers through the oracle and requires it to tell them apart."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from tests.support import adapter_differential as ad
from tests.support import adapter_differential_deltas as listed
from tests.support.adapter_differential_deltas import Delta, DeltaError


def norm() -> ad.Normalizer:
    return ad.Normalizer()


def compared(golden: dict, actual: dict, name: str = "x") -> str:
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / f"{name}.json").write_text(json.dumps(golden), encoding="utf-8")
        return ad.compare_golden(name, actual, environ={}, fixture_dir=Path(directory), deltas=[])


# 1 -- a type change is a difference --------------------------------------------------------------------------------

@pytest.mark.parametrize(("before", "after"), [(False, 0), (True, 1), (0, 0.0), (1, 1.0)])
def test_a_value_of_another_type_is_not_equal_to_the_golden(before, after):
    golden = {"receipts": {"receipts": [{"receipt_json": {"stream": before}}]}}
    actual = {"receipts": {"receipts": [{"receipt_json": {"stream": after}}]}}
    assert compared(golden, actual) != ""


def test_the_repeat_run_check_of_the_rig_is_type_aware():
    assert ad.traces_differ({"a": False}, {"a": 0}) and not ad.traces_differ({"a": 1}, {"a": 1})


# 2 -- a delta names one value, in a place the design allows ---------------------------------------------------------

def test_a_delta_cannot_replace_the_engine_requests_of_a_trace():
    golden = {"engine_requests": [{"method": "POST"}], "caller": []}
    delta = Delta("envelope_v2", "$.engine_requests", [{"method": "POST"}], [], "the envelope moved")
    with pytest.raises(DeltaError):
        listed.apply_deltas(golden, [delta])


def test_a_delta_cannot_replace_the_response_headers_of_a_call():
    golden = {"caller": [{"response": {"headers": {"content-type": "application/json"}}}]}
    delta = Delta("graph_template_meta", "$.caller[0].response.headers", {"content-type": "application/json"}, {}, "meta")
    with pytest.raises(DeltaError):
        listed.apply_deltas(golden, [delta])


@pytest.mark.parametrize("path", ["$.caller[0].response.headers.content-type", "$.engine_requests[0].method",
                                  "$.assets[0].suffix", "$.receipts.receipts[0].accepted_inputs"])
def test_a_delta_cannot_reach_a_place_the_design_keeps_identical(path):
    golden = {"caller": [{"response": {"headers": {"content-type": "a"}}}], "engine_requests": [{"method": "a"}],
              "assets": [{"suffix": "a"}], "receipts": {"receipts": [{"accepted_inputs": "a"}]}}
    with pytest.raises(DeltaError):
        listed.apply_deltas(golden, [Delta("envelope_v2", path, "a", "b", "r")])


def test_a_delta_on_one_value_in_a_body_still_applies():
    golden = {"caller": [{"response": {"status": 200}}]}
    out = listed.apply_deltas(golden, [Delta("bounded_wait", "$.caller[0].response.status", 200, 202, "a wait")])
    assert out["caller"][0]["response"]["status"] == 202


# 3 -- clock checks read the raw JSON text and the raw SQL row ---------------------------------------------------------

@pytest.mark.parametrize("body", [
    '{"created_at": 1792355601, "finished_at": 1792355600}',            # finished before it was created
    '{"created_at": 1792355600000, "finished_at": 1792355601000}',      # milliseconds under a seconds key
])
def test_clock_order_and_unit_are_checked_on_the_raw_json_text(body):
    n = norm()
    ad.describe_body(body.encode(), "application/json", n)
    assert n.problems


def test_a_good_clock_in_json_text_records_no_problem():
    n = norm()
    ad.describe_body(b'{"created_at": 1792355600, "finished_at": 1792355601}', "application/json", n)
    assert n.problems == []


def test_clock_order_is_checked_on_the_raw_sql_row_before_its_cells_are_masked(tmp_path):
    path = tmp_path / "jobs.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE jobs (id TEXT, created_at_s REAL, updated_at_s REAL)")
    conn.execute("INSERT INTO jobs VALUES ('a', 1792355601, 1792355600)")
    conn.commit()
    conn.close()
    n = norm()
    ad.dump_sqlite(path, n)
    assert n.problems


# 4 -- the job prefixes are compared -----------------------------------------------------------------------------------

def test_a_consistent_change_of_job_prefix_is_a_difference():
    one, two = norm(), norm()
    tail = "a" * 32
    assert one.text(f"imgjob_{tail}") == two.text(f"mj_{tail}")          # the masked text cannot tell them apart
    trace = {"caller": []}
    first, second = ad.with_job_prefixes(dict(trace), one), ad.with_job_prefixes(dict(trace), two)
    assert first["job_prefixes"] == ["imgjob"] and second["job_prefixes"] == ["mj"]
    assert compared(first, second) != ""


def test_a_golden_recorded_without_prefixes_is_compared_on_everything_else():
    golden = {"caller": [1]}
    assert compared(golden, {"caller": [1], "job_prefixes": ["mj"]}) == ""
    assert compared(golden, {"caller": [2], "job_prefixes": ["mj"]}) != ""


# 5 -- text the caller wrote is not a clock reading --------------------------------------------------------------------

def test_a_date_in_the_prompt_of_an_engine_request_is_compared_exactly():
    one = ad.describe_body(b'{"prompt": "Draw 2026-11-08T10:00:00Z"}', "application/json", norm(), payload=True)
    two = ad.describe_body(b'{"prompt": "Draw 2027-11-08T10:00:00Z"}', "application/json", norm(), payload=True)
    assert one != two


def test_a_generated_time_under_a_time_key_is_still_masked_in_a_payload():
    one = ad.describe_body(b'{"created_at": "2026-11-08T10:00:00Z"}', "application/json", norm(), payload=True)
    two = ad.describe_body(b'{"created_at": "2027-01-01T00:00:00Z"}', "application/json", norm(), payload=True)
    assert one == two


# 6 -- decoded media bytes ----------------------------------------------------------------------------------------------

def test_two_different_media_payloads_that_look_like_ids_are_different_bodies():
    one = ad.describe_body(json.dumps({"data": [{"b64_json": "a" * 32}]}).encode(), "application/json", norm())
    two = ad.describe_body(json.dumps({"data": [{"b64_json": "b" * 32}]}).encode(), "application/json", norm())
    assert one != two


# 7 -- a repeated special header keeps its count -------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["Authorization", "Cookie", "X-Api-Key", "Proxy-Authorization", "Api-Key"])
def test_a_repeated_credential_header_keeps_its_count(name):
    once = ad.describe_request_headers([(name, "a")], norm())
    twice = ad.describe_request_headers([(name, "a"), (name, "b")], norm())
    assert once != twice
    assert list(twice.values()) == [["<present>", "<present>"]]


@pytest.mark.parametrize("name", ["Expires", "Last-Modified"])
def test_a_repeated_time_header_keeps_its_count(name):
    once = ad.describe_response_headers([(name, "Sun, 01 Jan 2023 00:00:00 GMT")], norm())
    twice = ad.describe_response_headers([(name, "Sun, 01 Jan 2023 00:00:00 GMT"),
                                          (name, "Mon, 02 Jan 2023 00:00:00 GMT")], norm())
    assert once != twice
