"""scripts/transition-proof against a fake gateway that serves the transition routes and a chat route with a cache.

Each test runs the real command and reads its exit code, its output and its result JSON. The fake's attributes
(tests/support/fake_upstream/transition_gateway.py) decide whether the cache survives, whether park or wake fails,
whether an operation hangs or the gate is held.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest

from scripts.transition_proof.manifest import InputError, load_cases, read_path
from scripts.transition_proof.runner import _bust_cache
from tests.support.fake_upstream.transition_gateway import DOMAIN, KEY, FakeTransitionGateway

CHAT_KEY = "chat-app-key-for-tests-fedcba9876543210"
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "transition-proof"
STANDARD = {"cached_tokens_path": "usage.prompt_tokens_details.cached_tokens", "prompt_tokens_path": "usage.prompt_tokens",
            "min_cached_fraction": 0.8, "max_timing_ratio": 0.5}
NO_RATIO = {**STANDARD, "max_timing_ratio": None}
ALTERNATE = {"cached_tokens_path": "timings.cache_n", "prompt_tokens_path": "timings.prompt_n",
             "min_cached_fraction": 0.8}


@pytest.fixture
def gateway() -> Iterator[FakeTransitionGateway]:
    fake = FakeTransitionGateway().start()
    try:
        yield fake
    finally:
        fake.stop()


def write_cases(directory: Path, cache_check: dict[str, Any] = STANDARD, *, labels: tuple[str, ...] = ("small",),
                estimate: bool = True) -> Path:
    cases = []
    for label in labels:
        body = {"model": "frozen-name", "stream": False, "seed": 1, "temperature": 0, "max_tokens": 8,
                "messages": [{"role": "system", "content": "You keep records."},
                             {"role": "user", "content": f"record {label} " * 400}]}
        raw = json.dumps(body).encode()
        (directory / f"{label}.json").write_bytes(raw)
        cases.append({"label": label, "file": f"{label}.json", "sha256": hashlib.sha256(raw).hexdigest(),
                      "followup": "What is record 7?", **({"count_estimate": {"input_tokens": 1000}} if estimate else {})})
    manifest = directory / "cases.json"
    manifest.write_text(json.dumps({"cache_check": cache_check, "cases": cases}))
    return manifest


def run(gateway: FakeTransitionGateway, tmp_path: Path, *extra: str, manifest: Path | None = None,
        key_file: Path | None = None, run_id: str = "t") -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    if key_file is None:
        key_file = tmp_path / "owner.key"
        key_file.write_text(KEY + "\n")
    out = tmp_path / "result.json"
    process = subprocess.run(
        [sys.executable, str(SCRIPT), "--base-url", gateway.base_url, "--domain", DOMAIN, "--model", "alias",
         "--cases", str(manifest or write_cases(tmp_path)), "--owner-key-file", str(key_file), "--out", str(out),
         "--poll-interval-s", "0.02", "--poll-timeout-s", "1", "--request-timeout-s", "10", "--run-id", run_id, *extra],
        capture_output=True, text=True, check=False, timeout=60)
    return process, json.loads(out.read_text()) if out.exists() else {}


def assertions(result: dict[str, Any], case: int = 0) -> dict[str, bool]:
    return {a["name"]: a["passed"] for a in result["cases"][case]["assertions"]}


def steps(result: dict[str, Any], case: int = 0) -> dict[str, dict[str, Any]]:
    return {s["name"]: s for s in result["cases"][case]["steps"]}


def test_kept_prefix_passes_with_every_step_recorded(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    process, result = run(gateway, tmp_path)
    assert process.returncode == 0, process.stdout + process.stderr
    assert result["status"] == "passed" and result["exit_code"] == 0 and result["assertions_failed"] == 0
    case = result["cases"][0]
    assert [s["name"] for s in case["steps"]] == ["seed", "park", "wake", "followup", "cold_baseline"]
    assert assertions(result) == {
        "seed_replied": True, "park_succeeded": True, "wake_succeeded": True, "gate_open_after_wake": True,
        "followup_replied": True, "cached_tokens_reported": True, "seed_prompt_tokens_known": True,
        "cached_prefix_kept": True, "cold_baseline_replied": True, "cold_baseline_is_cold": True,
        "followup_faster_than_cold": True}
    park, wake, followup = steps(result)["park"], steps(result)["wake"], steps(result)["followup"]
    assert (park["op_id"], park["idempotency_key"], park["states"]) == ("op-1", "proof-t-small-park", ["running", "succeeded"])
    assert (wake["op_id"], wake["idempotency_key"]) == ("op-2", "proof-t-small-wake")
    assert [t["gate"] for t in case["gate_trace"]] == ["open", "open", "open", "open"]
    assert [t["parked"] for t in case["gate_trace"]] == [None, "op-1", None, None]
    assert steps(result)["seed"]["reply_text"] == "answer" and steps(result)["seed"]["usage"]["prompt_tokens"] > 0
    assert followup["cached_tokens"] == steps(result)["seed"]["prompt_tokens"] and followup["seconds"] > 0
    assert gateway.paths()[:3] == ["POST /v1/chat/completions", "GET /admin/api/engines/domains/proof-domain/gate",
                                   "POST /admin/api/engines/domains/proof-domain/transitions"]
    sent = [r for r in gateway.requests if r["path"] == "/v1/chat/completions"]
    assert {r["body"]["model"] for r in sent} == {"alias"}  # the frozen body's own model name is never sent
    assert sent[1]["body"]["messages"][:-1] == sent[0]["body"]["messages"]
    assert sent[1]["body"]["messages"][-1] == {"role": "user", "content": "What is record 7?"}
    assert {r["authorization"] for r in gateway.requests} == {f"Bearer {KEY}"}


def test_request_mode_lets_the_followup_trigger_the_wake(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    process, result = run(gateway, tmp_path, "--wake-mode", "request", manifest=write_cases(tmp_path, NO_RATIO))
    assert process.returncode == 0, process.stdout + process.stderr
    posted = [r["body"]["kind"] for r in gateway.requests if r["method"] == "POST" and r["path"].endswith("/transitions")]
    assert posted == ["park"]  # the harness never posts the wake in this mode
    assert [(op["kind"], op["key"]) for op in gateway.ops.values()] == [("park", "proof-t-small-park"), ("wake", "wake-auto-1")]
    assert steps(result)["wake"]["mode"] == "request" and assertions(result)["gate_open_after_followup"]


def test_lost_prefix_fails_on_the_cache_assertion_only(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.cache = "lost"
    process, result = run(gateway, tmp_path)
    assert process.returncode == 1
    failed = {name for name, passed in assertions(result).items() if not passed}
    assert failed == {"cached_prefix_kept", "followup_faster_than_cold"}
    detail = next(a for a in result["cases"][0]["assertions"] if a["name"] == "cached_prefix_kept")
    assert detail["cached_tokens"] == 0 and detail["fraction"] == 0 and detail["denominator_source"] == "seed_reply"
    assert result["status"] == "failed" and "FAIL small cached_prefix_kept" in process.stdout


def test_cached_count_that_is_not_faster_fails_the_timing_ratio(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.slow_when_cached = True
    process, result = run(gateway, tmp_path)
    assert process.returncode == 1
    assert {name for name, passed in assertions(result).items() if not passed} == {"followup_faster_than_cold"}


def test_cache_paths_are_data_not_code(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.usage_shape = "alternate"
    process, result = run(gateway, tmp_path, manifest=write_cases(tmp_path, ALTERNATE))
    assert process.returncode == 0, process.stdout
    assert "cold_baseline" not in steps(result) or steps(result)["cold_baseline"]["status"] == "skipped"
    # The same reply read through the standard paths reports no count, so the run fails on that.
    process, result = run(gateway, tmp_path, manifest=write_cases(tmp_path, {**STANDARD, "max_timing_ratio": None}),
                          run_id="second")
    assert process.returncode == 1 and assertions(result)["cached_tokens_reported"] is False
    assert assertions(result)["cached_prefix_kept"] is False  # no count means no proof of a kept prefix


def test_a_failed_park_stops_before_wake_and_followup(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.park = "fail"
    process, result = run(gateway, tmp_path)
    assert process.returncode == 1
    assert assertions(result)["park_succeeded"] is False
    assert steps(result)["park"]["final_state"] == "failed_closed"
    assert [s["status"] for n, s in steps(result).items() if n in ("wake", "followup", "cold_baseline")] == ["skipped"] * 3
    assert [t["gate"] for t in result["cases"][0]["gate_trace"]] == ["open", "failed_closed"]
    assert sum(r["path"] == "/v1/chat/completions" for r in gateway.requests) == 1  # only the seed


def test_a_gate_that_stays_reconciling_is_reported_not_waited_out(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.park = "reconcile"
    process, result = run(gateway, tmp_path, "--poll-timeout-s", "30")
    assert process.returncode == 1
    park = steps(result)["park"]
    assert park["final_state"] == "reconciling" and "owner" in park["error"]
    assert result["cases"][0]["gate_trace"][-1] == {"at": "after_park", "gate": "reconciling", "parked": None}
    assert result["cases"][0]["status"] == "failed"


def test_a_wake_that_never_finishes_times_out(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.wake = "hang"
    process, result = run(gateway, tmp_path, "--poll-timeout-s", "0.3")
    assert process.returncode == 1
    wake = steps(result)["wake"]
    assert wake["states"] == ["running"] and "still running after 0.3s" in wake["error"]
    assert assertions(result)["wake_succeeded"] is False and "followup_replied" not in assertions(result)


def test_a_conflicting_operation_is_reported_with_its_id(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.park = "conflict"
    process, result = run(gateway, tmp_path)
    assert process.returncode == 1
    assert "op-other" in steps(result)["park"]["error"] and steps(result)["park"]["http_status"] == 409


def test_a_failed_case_leaves_later_cases_not_run(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.cache = "lost"
    process, result = run(gateway, tmp_path, manifest=write_cases(tmp_path, labels=("a", "b")))
    assert process.returncode == 1
    assert [(c["label"], c["status"]) for c in result["cases"]] == [("a", "failed"), ("b", "not_run")]


def test_every_case_runs_its_own_park_wake_cycle(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    process, result = run(gateway, tmp_path, manifest=write_cases(tmp_path, labels=("a", "b")))
    assert process.returncode == 0, process.stdout
    assert [c["status"] for c in result["cases"]] == ["passed", "passed"]
    assert result["assertions_total"] == 22


def test_a_body_that_changed_is_refused_before_anything_is_sent(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    manifest = write_cases(tmp_path, labels=("a", "b"))
    (tmp_path / "b.json").write_text((tmp_path / "b.json").read_text().replace("record", "recorD"))
    process, result = run(gateway, tmp_path, manifest=manifest)
    assert process.returncode == 2
    assert result["status"] == "invalid_input" and "case 'b'" in result["error"] and "sha256" in process.stderr
    assert gateway.requests == []  # not even the valid first case was sent


def test_a_missing_key_file_sends_nothing_and_names_no_secret(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    process, result = run(gateway, tmp_path, key_file=tmp_path / "absent.key")
    assert process.returncode == 2 and gateway.requests == []
    assert "owner key file" in process.stderr and result["status"] == "invalid_input"


def test_an_empty_key_file_is_refused(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    empty = tmp_path / "empty.key"
    empty.write_text("\n")
    process, _ = run(gateway, tmp_path, key_file=empty)
    assert process.returncode == 2 and gateway.requests == []


def test_the_key_never_reaches_output_or_results_even_when_the_gateway_echoes_it(
        gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.echo_auth = True
    gateway.park = "conflict"  # the 409 body repeats the Authorization header
    process, result = run(gateway, tmp_path)
    assert process.returncode == 1
    text = (tmp_path / "result.json").read_text()
    assert "[redacted]" in text, "the echo reached the result, so this test must prove the redaction"
    for surface in (process.stdout, process.stderr, text):
        assert KEY not in surface
    process, _ = run(gateway, tmp_path, "--dry-run")
    assert KEY not in process.stdout + process.stderr + (tmp_path / "result.json").read_text()


def test_dry_run_validates_and_sends_nothing(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    process, result = run(gateway, tmp_path, "--dry-run")
    assert process.returncode == 0 and gateway.requests == []
    assert result["status"] == "validated" and result["dry_run"] is True
    assert result["cases"][0]["messages"] == 2 and result["cases"][0]["cache_check"]["min_cached_fraction"] == 0.8


def test_dry_run_rejects_a_manifest_with_no_cache_check(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    process, result = run(gateway, tmp_path, "--dry-run", manifest=write_cases(tmp_path, {}))
    assert process.returncode == 2 and "cached_tokens_path" in result["error"] and gateway.requests == []


def test_a_gateway_that_is_down_is_a_failed_step_not_a_crash(tmp_path: Path) -> None:
    dead = FakeTransitionGateway().start()
    dead.stop()
    process, result = run(dead, tmp_path)
    assert process.returncode == 1 and "Traceback" not in process.stderr
    assert assertions(result) == {"seed_replied": False} and "ConnectError" in steps(result)["seed"]["error"]


def test_dotted_paths_read_lists_and_report_missing_as_none() -> None:
    data = {"a": {"b": [{"c": 4}]}}
    assert read_path(data, "a.b.0.c") == 4
    assert read_path(data, "a.b.1.c") is None and read_path(data, "a.x") is None and read_path(data, "a.b.c") is None


def test_cold_baseline_body_shares_no_prefix_with_the_original() -> None:
    for first in ("text", [{"type": "text", "text": "text"}]):
        body = {"messages": [{"role": "user", "content": first}, {"role": "user", "content": "q"}]}
        busted = _bust_cache(body, "r", "x")
        assert busted["messages"][0] != body["messages"][0] and busted["messages"][1:] == body["messages"][1:]
        assert body["messages"][0]["content"] == first  # the original is untouched


def test_a_streaming_body_is_refused(tmp_path: Path) -> None:
    manifest = write_cases(tmp_path)
    body = json.loads((tmp_path / "small.json").read_text())
    body["stream"] = True
    raw = json.dumps(body).encode()
    (tmp_path / "small.json").write_bytes(raw)
    data = json.loads(manifest.read_text())
    data["cases"][0]["sha256"] = hashlib.sha256(raw).hexdigest()
    manifest.write_text(json.dumps(data))
    with pytest.raises(InputError, match="stream"):
        load_cases(manifest)


# -- review findings ---------------------------------------------------------------------------------------


def post_transition(gateway: FakeTransitionGateway, kind: str, key: str) -> Any:
    return httpx.post(f"{gateway.base_url}/admin/api/engines/domains/{DOMAIN}/transitions",
                      json={"kind": kind, "idempotency_key": key}, headers={"Authorization": f"Bearer {KEY}"})


def settle(gateway: FakeTransitionGateway, op_id: str) -> None:
    while gateway.ops[op_id]["state"] == "running":
        httpx.get(f"{gateway.base_url}/admin/api/engines/domains/{DOMAIN}/transitions/{op_id}",
                  headers={"Authorization": f"Bearer {KEY}"})


def test_each_case_gets_its_own_keys_and_operations(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    process, result = run(gateway, tmp_path, manifest=write_cases(tmp_path, labels=("a", "b")))
    assert process.returncode == 0, process.stdout
    ops = [(steps(result, i)[name]["idempotency_key"], steps(result, i)[name]["op_id"])
           for i in (0, 1) for name in ("park", "wake")]
    assert [key for key, _ in ops] == ["proof-t-a-park", "proof-t-a-wake", "proof-t-b-park", "proof-t-b-wake"]
    assert len({op for _, op in ops}) == 4 and len(gateway.ops) == 4  # case b really parked and woke


def test_a_reused_key_returns_the_finished_operation_so_a_shared_key_would_pass_without_a_park(
        gateway: FakeTransitionGateway) -> None:
    """The old harness sent ``proof-{run}-park`` for every case. Show what that does against the real store's rule."""
    first = post_transition(gateway, "park", "proof-t-park").json()
    settle(gateway, first["op_id"])
    settle(gateway, post_transition(gateway, "wake", "proof-t-wake").json()["op_id"])
    ops_before = len(gateway.ops)
    again = post_transition(gateway, "park", "proof-t-park")  # case b under the old key scheme
    assert again.status_code == 202 and again.json()["op_id"] == first["op_id"]
    assert again.json()["state"] == "succeeded"  # a finished park is returned: case b would "pass" without parking
    assert len(gateway.ops) == ops_before and gateway.parked is False
    assert post_transition(gateway, "wake", "proof-t-park").status_code == 409  # same key, another kind


def test_a_seed_that_reports_zero_prompt_tokens_cannot_pass_with_the_cache_lost(
        gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.cache, gateway.prompt_tokens_reported = "lost", 0
    cache_check = {**STANDARD, "max_timing_ratio": None}
    process, result = run(gateway, tmp_path, manifest=write_cases(tmp_path, cache_check, estimate=False))
    assert process.returncode == 1
    found = assertions(result)
    assert found["seed_prompt_tokens_known"] is False and found["cached_prefix_kept"] is False
    detail = next(a for a in result["cases"][0]["assertions"] if a["name"] == "cached_prefix_kept")
    assert "cannot be computed" in detail["reason"]
    # With a manifest estimate, a count of 0 is unknown too, so the estimate is the denominator and 0 cached fails.
    process, result = run(gateway, tmp_path, manifest=write_cases(tmp_path, cache_check), run_id="estimated")
    detail = next(a for a in result["cases"][0]["assertions"] if a["name"] == "cached_prefix_kept")
    assert process.returncode == 1 and detail["denominator_source"] == "manifest_count_estimate"
    assert detail["passed"] is False and detail["seed_prompt_tokens"] == 1000
    assert "estimate" in detail["note"]  # the result says the fraction rests on an estimate


def test_a_missing_cached_count_fails_the_prefix_assertion_itself(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.usage_shape = "alternate"  # the standard paths find nothing
    process, result = run(gateway, tmp_path, manifest=write_cases(tmp_path, {**STANDARD, "max_timing_ratio": None}))
    assert process.returncode == 1 and assertions(result)["cached_prefix_kept"] is False


def test_a_key_across_the_300_char_cut_leaks_no_prefix(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    marker = json.dumps({"op_id": "op-other", "state": "running", "echo": "MARK"}).index("MARK")
    gateway.echo_auth, gateway.park = True, "conflict"
    gateway.echo_pad = 290 - marker - len("Authorization: Bearer ")  # the key starts 10 characters before the cut
    process, result = run(gateway, tmp_path)
    assert process.returncode == 1
    text = (tmp_path / "result.json").read_text()
    assert "[redacted]" in text
    for piece in (KEY, KEY[:6], KEY[-6:]):
        assert piece not in process.stdout + process.stderr + text


def test_a_key_file_with_more_than_one_token_is_refused_and_nothing_is_sent(
        gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    for index, content in enumerate((f"{KEY}\nsecond-line\n", f"{KEY} extra", f"{KEY[:10]}\n{KEY[10:]}")):
        key_file = tmp_path / f"multi{index}.key"
        key_file.write_text(content)
        process, result = run(gateway, tmp_path, key_file=key_file)
        assert process.returncode == 2 and gateway.requests == [] and result["status"] == "invalid_input"
        for surface in (process.stdout, process.stderr, (tmp_path / "result.json").read_text()):
            assert KEY[:6] not in surface and KEY[-6:] not in surface and "second-line" not in surface
    non_ascii = tmp_path / "unicode.key"
    non_ascii.write_text("clé-é", encoding="utf-8")
    process, _ = run(gateway, tmp_path, key_file=non_ascii)
    assert process.returncode == 2 and gateway.requests == []


def test_a_key_file_is_checked_in_a_dry_run_too(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    key_file = tmp_path / "multi.key"
    key_file.write_text("one\ntwo")
    process, _ = run(gateway, tmp_path, "--dry-run", key_file=key_file)
    assert process.returncode == 2


def manifest_with(tmp_path: Path, change: Any) -> Path:
    manifest = write_cases(tmp_path)
    data = json.loads(manifest.read_text())
    change(data)
    manifest.write_text(json.dumps(data))
    return manifest


BAD_MANIFESTS = {
    "count_estimate is a number": lambda d: d["cases"][0].update(count_estimate=5),
    "count_estimate is a string": lambda d: d["cases"][0].update(count_estimate="5"),
    "count_estimate tokens not a number": lambda d: d["cases"][0].update(count_estimate={"input_tokens": "many"}),
    "count_estimate tokens not positive": lambda d: d["cases"][0].update(count_estimate={"input_tokens": 0}),
    "cache_check is a list": lambda d: d.update(cache_check=[1]),
    "cache_check is an empty list": lambda d: d.update(cache_check=[]),
    "cache_check is a string": lambda d: d.update(cache_check="paths"),
    "case cache_check is a list": lambda d: d["cases"][0].update(cache_check=["x"]),
    "case cache_check is a string": lambda d: d["cases"][0].update(cache_check="x"),
    "file name has a null byte": lambda d: d["cases"][0].update(file="a\u0000b.json"),
    "file is not a string": lambda d: d["cases"][0].update(file=["small.json"]),
    "cases hold a number": lambda d: d.update(cases=[7]),
}


@pytest.mark.parametrize("name", list(BAD_MANIFESTS))
def test_a_malformed_manifest_is_exit_2_with_nothing_sent(gateway: FakeTransitionGateway, tmp_path: Path, name: str) -> None:
    process, result = run(gateway, tmp_path, manifest=manifest_with(tmp_path, BAD_MANIFESTS[name]))
    assert process.returncode == 2, process.stdout + process.stderr
    assert "Traceback" not in process.stderr and result["status"] == "invalid_input" and result["exit_code"] == 2
    assert gateway.requests == []


def test_a_manifest_that_is_not_an_object_or_is_too_deep_is_exit_2(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    for text in ("[1, 2]", "null", '"x"', "[" * 100000 + "]" * 100000):
        manifest = tmp_path / "bad.json"
        manifest.write_text(text)
        process, result = run(gateway, tmp_path, manifest=manifest)
        assert process.returncode == 2 and "Traceback" not in process.stderr and result["status"] == "invalid_input"
    assert gateway.requests == []


def test_a_503_in_request_mode_waits_for_retry_after_and_resends(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.retry_503 = 1
    process, result = run(gateway, tmp_path, "--wake-mode", "request", "--poll-timeout-s", "20",
                          manifest=write_cases(tmp_path, {**STANDARD, "max_timing_ratio": None}))
    assert process.returncode == 0, process.stdout + process.stderr
    follow = steps(result)["followup"]
    assert follow["retries"] == [{"http_status": 503, "retry_after": "1", "waited_s": 1.0}]
    assert follow["seconds"] >= 1.0 and assertions(result)["followup_replied"] and assertions(result)["cached_prefix_kept"]
    sent = [r for r in gateway.requests if r["path"] == "/v1/chat/completions"]
    assert len(sent) == 3 and sent[1]["body"] == sent[2]["body"]  # seed, the refused follow-up, the same follow-up again


def test_a_503_that_outlasts_the_deadline_is_a_failed_followup(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.retry_503 = 100
    process, result = run(gateway, tmp_path, "--wake-mode", "request", "--poll-timeout-s", "1.5",
                          manifest=write_cases(tmp_path, NO_RATIO))
    assert process.returncode == 1
    follow = steps(result)["followup"]
    assert follow["http_status"] == 503 and len(follow["retries"]) >= 1 and assertions(result)["followup_replied"] is False


def test_post_mode_does_not_retry_a_503(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.retry_503 = 1
    process, result = run(gateway, tmp_path)  # post mode: the wake was posted, so a 503 after it is a real failure
    assert process.returncode == 1 and "retries" not in steps(result)["followup"]


def test_a_failed_closed_operation_does_not_block_a_new_transition(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.park = "fail"
    process, _ = run(gateway, tmp_path)
    assert process.returncode == 1 and gateway.gate == "failed_closed"
    gateway.park = "succeed"  # G9: the owner's next transition is accepted
    process, result = run(gateway, tmp_path, run_id="again")
    assert process.returncode == 0, process.stdout
    assert steps(result)["park"]["http_status"] == 202 and steps(result)["park"]["op_id"] == "op-2"


def test_a_reconciling_operation_still_blocks_a_new_transition(gateway: FakeTransitionGateway) -> None:
    gateway.park = "reconcile"
    first = post_transition(gateway, "park", "k1").json()
    settle(gateway, first["op_id"])
    assert gateway.ops[first["op_id"]]["state"] == "reconciling"
    assert post_transition(gateway, "wake", "k2").status_code == 409


def test_a_gateway_that_rejects_the_key_leaks_no_key_pieces(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    wrong = tmp_path / "wrong.key"
    wrong.write_text("wrong-key-abcdefghij\n")
    process, result = run(gateway, tmp_path, key_file=wrong)
    assert process.returncode == 1 and result["cases"][0]["steps"][0]["http_status"] == 401


def _dead(gateway: FakeTransitionGateway) -> None:
    gateway.stop()


FAILURE_PATHS = {
    "conflict with echo": (lambda g: (setattr(g, "echo_auth", True), setattr(g, "park", "conflict")), [], 1),
    "echo across the cut": (lambda g: (setattr(g, "echo_auth", True), setattr(g, "park", "conflict"),
                                       setattr(g, "echo_pad", 215)), [], 1),
    "park fails": (lambda g: setattr(g, "park", "fail"), [], 1),
    "wake hangs": (lambda g: setattr(g, "wake", "hang"), ["--poll-timeout-s", "0.3"], 1),
    "gate stays reconciling": (lambda g: setattr(g, "park", "reconcile"), [], 1),
    "lost prefix": (lambda g: setattr(g, "cache", "lost"), [], 1),
    "gateway down": (_dead, [], 1),
    "dry run": (lambda g: None, ["--dry-run"], 0),
    "persistent 503": (lambda g: setattr(g, "retry_503", 100), ["--wake-mode", "request", "--poll-timeout-s", "1.2"], 1),
}


@pytest.mark.parametrize("name", list(FAILURE_PATHS))
def test_the_key_appears_in_no_output_channel_on_any_failure_path(
        gateway: FakeTransitionGateway, tmp_path: Path, name: str) -> None:
    setup, extra, code = FAILURE_PATHS[name]
    setup(gateway)
    manifest = write_cases(tmp_path, NO_RATIO) if "--wake-mode" in extra else None  # a ratio is refused in request mode
    process, _ = run(gateway, tmp_path, *extra, manifest=manifest)
    assert process.returncode == code, process.stdout + process.stderr
    channels = process.stdout + process.stderr + (tmp_path / "result.json").read_text()
    for piece in (KEY, KEY[:8], KEY[-8:], KEY[10:20]):
        assert piece not in channels


@pytest.mark.parametrize("content", ["a-b-c-d-secret-1\nsecond-secret-2\n", "secret-one secret-two"])
def test_the_key_appears_in_no_output_channel_when_the_key_file_is_refused(
        gateway: FakeTransitionGateway, tmp_path: Path, content: str) -> None:
    key_file = tmp_path / "bad.key"
    key_file.write_text(content)
    process, _ = run(gateway, tmp_path, key_file=key_file)
    assert process.returncode == 2 and gateway.requests == []
    channels = process.stdout + process.stderr + (tmp_path / "result.json").read_text()
    for piece in content.split():
        assert piece not in channels


# -- second review ------------------------------------------------------------------------------------------


def test_reusing_a_run_id_after_the_cache_is_lost_fails_instead_of_passing_without_a_park(
        gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    process, _ = run(gateway, tmp_path, run_id="same")
    assert process.returncode == 0, process.stdout
    gateway.cache = "lost"  # a real park now would drop the prefix, but the repeated keys never park again
    process, result = run(gateway, tmp_path, run_id="same")
    assert process.returncode == 1 and result["status"] == "failed"
    park = steps(result)["park"]
    assert "earlier run" in park["error"] and park["http_status"] == 202 and park["states"] == []
    assert assertions(result)["park_succeeded"] is False and "followup_replied" not in assertions(result)
    assert len(gateway.ops) == 2  # no new operation: the second run parked nothing


def test_an_unexpected_exception_replaces_an_old_passed_result_and_exits_1(
        gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    out = tmp_path / "result.json"
    out.write_text(json.dumps({"status": "passed", "exit_code": 0}))
    gateway.content = [{"text": 5}]  # a reply the runner cannot read as text
    process, result = run(gateway, tmp_path)
    assert process.returncode == 1 and "Traceback" not in process.stderr
    assert result["status"] == "failed" and result["exit_code"] == 1 and result["error"] == "unexpected TypeError"
    assert KEY not in process.stdout + process.stderr + out.read_text()


def test_retry_after_zero_waits_one_poll_interval_and_does_not_spin(
        gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.retry_503, gateway.retry_after = 1000, "0"
    process, result = run(gateway, tmp_path, "--wake-mode", "request", "--poll-timeout-s", "1",
                          "--poll-interval-s", "0.2", manifest=write_cases(tmp_path, NO_RATIO))
    assert process.returncode == 1
    sent = [r for r in gateway.requests if r["path"] == "/v1/chat/completions"]
    assert len(sent) <= 1 + 8  # the seed, then about one resend per 0.2 s for 1 s (a spin sends hundreds)
    assert all(r["waited_s"] >= 0.19 for r in steps(result)["followup"]["retries"][:-1])  # the last wait is cut to the deadline


def test_a_cold_baseline_that_reports_a_large_cached_count_fails_the_assertion(
        gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.cold_cached_reported = 1000  # of about 1300 prompt tokens; at most 20% may be cached
    process, result = run(gateway, tmp_path)
    assert process.returncode == 1
    assert {name for name, passed in assertions(result).items() if not passed} == {"cold_baseline_is_cold"}
    detail = next(a for a in result["cases"][0]["assertions"] if a["name"] == "cold_baseline_is_cold")
    assert detail["cached_tokens"] == 1000 and detail["cold_prompt_tokens"] > 1000 and detail["max_cached_tokens"] < 1000


@pytest.mark.parametrize("key", ['abc"def-ghijklmn', "abc\\def-ghijklmn", "abc;def-ghijklmn", "abc=def-ghijklmn",
                                 "abc def", "=abc", "abc\u00e9"])
def test_a_key_outside_the_bearer_token_characters_is_refused_before_anything_is_sent(
        gateway: FakeTransitionGateway, tmp_path: Path, key: str) -> None:
    key_file = tmp_path / "odd.key"
    key_file.write_text(key, encoding="utf-8")
    process, result = run(gateway, tmp_path, key_file=key_file)
    assert process.returncode == 2 and gateway.requests == [] and result["status"] == "invalid_input"
    assert "RFC 6750" in process.stderr
    for piece in ("abc", "def", "ghij"):
        assert piece not in process.stdout + process.stderr + (tmp_path / "result.json").read_text()


@pytest.mark.parametrize("key", ["abc.DEF_123~+/-", "abcdef==", "A1b2c3"])
def test_the_bearer_token_characters_are_accepted(gateway: FakeTransitionGateway, tmp_path: Path, key: str) -> None:
    key_file = tmp_path / "ok.key"
    key_file.write_text(key)
    process, _ = run(gateway, tmp_path, "--dry-run", key_file=key_file)
    assert process.returncode == 0, process.stderr


@pytest.mark.parametrize("extra", [(), ("--dry-run",)])
def test_a_timing_ratio_in_request_mode_is_refused_and_nothing_is_sent(
        gateway: FakeTransitionGateway, tmp_path: Path, extra: tuple[str, ...]) -> None:
    process, result = run(gateway, tmp_path, "--wake-mode", "request", *extra)  # the default manifest sets a ratio
    assert process.returncode == 2 and gateway.requests == [] and result["status"] == "invalid_input"
    assert "max_timing_ratio" in process.stderr and "wake" in process.stderr
    process, _ = run(gateway, tmp_path, "--wake-mode", "post", *extra)  # the same manifest is fine in post mode
    assert process.returncode == 0, process.stderr


def chat_key_file(tmp_path: Path, content: str = CHAT_KEY + "\n") -> Path:
    path = tmp_path / "chat.key"
    path.write_text(content)
    return path


def test_a_chat_key_goes_to_chat_and_the_owner_key_to_the_admin_routes(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.chat_key = CHAT_KEY  # the owner key is refused on chat, the chat key on the admin routes
    process, result = run(gateway, tmp_path, "--chat-key-file", str(chat_key_file(tmp_path)))
    assert process.returncode == 0 and result["status"] == "passed"
    sent = {r["path"].startswith("/v1/"): set() for r in gateway.requests}
    for request in gateway.requests:
        sent[request["path"].startswith("/v1/")].add(request["authorization"])
    assert sent == {True: {f"Bearer {CHAT_KEY}"}, False: {f"Bearer {KEY}"}}


def test_without_a_chat_key_every_request_carries_the_owner_key(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    process, result = run(gateway, tmp_path)
    assert process.returncode == 0 and result["status"] == "passed"
    assert any(r["path"].startswith("/v1/") for r in gateway.requests)
    assert {r["authorization"] for r in gateway.requests} == {f"Bearer {KEY}"}


@pytest.mark.parametrize("content", ["", f"{CHAT_KEY}\nsecond-line\n", f"{CHAT_KEY} extra", 'bad"quote' + CHAT_KEY])
@pytest.mark.parametrize("extra", [(), ("--dry-run",)])
def test_a_malformed_chat_key_file_is_refused_and_not_echoed(
        gateway: FakeTransitionGateway, tmp_path: Path, content: str, extra: tuple[str, ...]) -> None:
    process, result = run(gateway, tmp_path, *extra, "--chat-key-file", str(chat_key_file(tmp_path, content)))
    assert process.returncode == 2 and gateway.requests == [] and result["status"] == "invalid_input"
    assert "the chat key file" in process.stderr and "the chat key file" in result["error"]
    for surface in (process.stdout, process.stderr, (tmp_path / "result.json").read_text()):
        assert CHAT_KEY[:6] not in surface and CHAT_KEY[-6:] not in surface and "second-line" not in surface
        assert KEY not in surface


def test_a_missing_chat_key_file_is_refused_and_names_the_chat_key(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    process, _ = run(gateway, tmp_path, "--chat-key-file", str(tmp_path / "absent.key"))
    assert process.returncode == 2 and gateway.requests == [] and "the chat key file" in process.stderr


def test_both_keys_are_redacted_from_the_result_including_a_reply_that_echoes_them(
        gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.chat_key = CHAT_KEY
    gateway.content = f"owner {KEY} chat {CHAT_KEY}"
    gateway.echo_auth, gateway.park = True, "conflict"  # the 409 body repeats the owner Authorization header
    process, result = run(gateway, tmp_path, "--chat-key-file", str(chat_key_file(tmp_path)))
    assert process.returncode == 1
    text = (tmp_path / "result.json").read_text()
    assert result["cases"][0]["steps"][0]["reply_text"] == "owner [redacted] chat [redacted]"
    for surface in (process.stdout, process.stderr, text):
        assert KEY not in surface and CHAT_KEY not in surface



def test_a_chat_key_that_holds_the_owner_key_leaves_no_fragment(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    longer = KEY + "-and-more"
    gateway.chat_key = longer
    gateway.content = f"echo {longer} end"
    gateway.park = "conflict"
    process, result = run(gateway, tmp_path, "--chat-key-file", str(chat_key_file(tmp_path, longer + "\n")))
    text = (tmp_path / "result.json").read_text()
    assert "-and-more" not in text and KEY not in text, "the longer key is redacted whole, before the owner key"
